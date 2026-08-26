from datetime import datetime, timezone
from email.message import Message
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request
import unittest

from resume_agent.discovery_http import LocalJsonClient, UrllibJsonTransport
from resume_agent.discovery_schemas import DiscoveryRequest
from resume_agent.discovery_sources import (
    BossResearchAdapter,
    NowcoderResearchAdapter,
    SourceCheckpoint,
    SourceInteractionRequiredError,
)


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request_json(self, method, url, payload, timeout_seconds):
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.calls.append((method, path, payload, timeout_seconds))
        response = self.responses[(method, path)]
        if isinstance(response, list):
            return response.pop(0)
        return response


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RecordingOpener:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return JsonResponse(self.payload)


def discovery_request(**overrides):
    payload = {
        "candidate_id": "candidate-1",
        "sources": ["boss", "nowcoder"],
        "keywords": ["AI Agent", "RAG"],
        "city": "Beijing",
        "recruitment_channel": "social",
        "limit_per_source": 2,
        "boss_project_id": "boss project/1",
    }
    payload.update(overrides)
    return DiscoveryRequest(**payload)


class LocalJsonClientTests(unittest.TestCase):
    def test_local_client_accepts_only_loopback_http_hosts(self):
        for endpoint in (
            "https://localhost:8000",
            "http://jobs.example.com",
            "http://localhost.evil.example",
            "http://127.0.0.1.nip.io",
            "http://127.0.0.1@jobs.example.com",
            "http://127.0.0.2:8000",
            "http://127.12.34.56:8000",
            "http://[::ffff:127.0.0.1]:8000",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    LocalJsonClient(endpoint)

        for endpoint in (
            "http://localhost:8000",
            "http://LOCALHOST:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
        ):
            with self.subTest(endpoint=endpoint):
                LocalJsonClient(endpoint)

    def test_local_client_passes_injected_transport_an_absolute_local_path(self):
        transport = FakeTransport({("POST", "/tasks?mode=research"): {"ok": True}})
        client = LocalJsonClient("http://127.0.0.1:8000/", transport=transport)

        self.assertEqual(
            client.request_json("POST", "/tasks?mode=research", {"keyword": "AI Agent"}),
            {"ok": True},
        )
        self.assertEqual(
            transport.calls,
            [("POST", "/tasks?mode=research", {"keyword": "AI Agent"}, 30)],
        )
        for path in ("tasks", "//jobs.example.com/tasks"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "absolute and local"):
                    client.request_json("GET", path)

    def test_default_transport_disables_proxies_and_refuses_redirects(self):
        transport = UrllibJsonTransport()
        proxy_handlers = [
            handler for handler in transport.opener.handlers if isinstance(handler, ProxyHandler)
        ]
        self.assertEqual(transport.proxy_handler.proxies, {})
        self.assertFalse(any(handler.proxies for handler in proxy_handlers))

        headers = Message()
        headers["Location"] = "http://jobs.example.com/redirected"
        request = Request("http://127.0.0.1:8000/jobs")
        with self.assertRaises(HTTPError) as raised:
            transport.redirect_handler.http_error_302(
                request, None, 302, "Found", headers
            )
        self.assertEqual(raised.exception.code, 302)
        self.assertEqual(raised.exception.url, request.full_url)

    def test_transport_uses_an_injected_opener_without_a_network_call(self):
        opener = RecordingOpener({"ok": True})
        transport = UrllibJsonTransport(opener=opener)

        self.assertEqual(
            transport.request_json("POST", "http://127.0.0.1:8000/jobs", {"id": 7}, 9),
            {"ok": True},
        )
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8000/jobs")
        self.assertEqual(request.data, b'{"id": 7}')
        self.assertEqual(timeout, 9)


class SourceCheckpointTests(unittest.TestCase):
    def test_checkpoint_round_trips_without_retaining_caller_mutable_state(self):
        state = {"keywords_json": '["AI Agent"]', "limit_per_source": 2}
        checkpoint = SourceCheckpoint("boss", ("boss-1",), state)
        serialized = checkpoint.to_dict()
        restored = SourceCheckpoint.from_dict(serialized)

        state["limit_per_source"] = 99
        serialized["state"]["keywords_json"] = "[]"
        self.assertEqual(restored.state, {"keywords_json": '["AI Agent"]', "limit_per_source": 2})
        self.assertEqual(checkpoint.state["limit_per_source"], 2)

    def test_checkpoint_rejects_malformed_payload_without_echoing_it(self):
        with self.assertRaisesRegex(ValueError, "invalid source checkpoint"):
            SourceCheckpoint.from_dict({"source": "boss", "remote_ids": "bad", "state": {}})


class BossResearchAdapterTests(unittest.TestCase):
    def test_start_encodes_keywords_and_project_without_a_network_call(self):
        transport = FakeTransport()
        adapter = BossResearchAdapter(LocalJsonClient("http://localhost:8001", transport=transport))

        checkpoint = adapter.start(discovery_request())

        self.assertEqual(checkpoint.remote_ids, ("boss project/1",))
        self.assertEqual(checkpoint.state, {"keywords_json": '["AI Agent","RAG"]', "limit_per_source": 2})
        self.assertEqual(transport.calls, [])

    def test_start_requires_a_boss_project_id(self):
        adapter = BossResearchAdapter(LocalJsonClient("http://localhost:8001", transport=FakeTransport()))

        with self.assertRaisesRegex(ValueError, "boss_project_id"):
            adapter.start(discovery_request(boss_project_id=None))

    def test_poll_maps_only_complete_keyword_matching_candidate_jobs(self):
        transport = FakeTransport({
            ("GET", "/resume-agent/v1/candidates/boss%20project%2F1/jobs?qualified_only=true"): {
                "jobs": [
                    {
                        "external_job_id": "https://www.zhipin.com/job_detail/7.html?utm_source=x#top",
                        "title": "AI Agent Engineer",
                        "company": "Example Tech",
                        "salary": "25-40K",
                        "detail": "Build Agent workflows and RAG evaluations",
                        "search_keyword": "ai agent",
                        "source_logged_at": "2026-08-26T09:00:00+08:00",
                        "score": 85.0,
                        "qualified": True,
                    },
                    {
                        "external_job_id": "https://www.zhipin.com/job_detail/8.html",
                        "title": "Unrelated",
                        "company": "Example Tech",
                        "salary": "20K",
                        "detail": "Unrelated detail",
                        "search_keyword": "Python",
                        "source_logged_at": "2026-08-26T01:00:00Z",
                        "score": None,
                        "qualified": True,
                    },
                    {
                        "external_job_id": "https://www.zhipin.com/job_detail/9.html",
                        "title": "Missing detail",
                        "company": "Example Tech",
                        "salary": "20K",
                        "detail": "",
                        "search_keyword": "RAG",
                        "source_logged_at": "2026-08-26T01:00:00Z",
                        "score": None,
                        "qualified": True,
                    },
                ]
            }
        })
        adapter = BossResearchAdapter(LocalJsonClient("http://127.0.0.1:8001", transport=transport))

        result = adapter.poll(adapter.start(discovery_request()))

        self.assertEqual(result.status, "completed")
        self.assertEqual([job.source_job_id for job in result.jobs], ["7"])
        job = result.jobs[0]
        self.assertEqual(job.canonical_url, "https://www.zhipin.com/job_detail/7.html")
        self.assertEqual(job.detail_status, "complete")
        self.assertEqual(job.search_keyword, "ai agent")
        self.assertEqual(job.verified_at, datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(job.source_metadata, {})

    def test_poll_rejects_malformed_checkpoint_state_without_payload_leakage(self):
        adapter = BossResearchAdapter(LocalJsonClient("http://localhost:8001", transport=FakeTransport()))

        with self.assertRaisesRegex(ValueError, "invalid source checkpoint") as raised:
            adapter.poll(SourceCheckpoint("boss", ("project",), {"keywords_json": "not-json", "limit_per_source": 2}))
        self.assertNotIn("not-json", str(raised.exception))

    def test_resolve_url_requires_interaction_for_a_valid_boss_job_url(self):
        transport = FakeTransport()
        adapter = BossResearchAdapter(LocalJsonClient("http://localhost:8001", transport=transport))

        with self.assertRaisesRegex(SourceInteractionRequiredError, "SOURCE_INTERACTION_REQUIRED"):
            adapter.resolve_url("https://www.zhipin.com/job_detail/7.html?x=1", "AI Agent")
        self.assertEqual(transport.calls, [])


class NowcoderResearchAdapterTests(unittest.TestCase):
    def test_start_posts_one_research_task_per_keyword_with_supported_fields_only(self):
        transport = FakeTransport({
            ("POST", "/job-sources/nowcoder/tasks"): [{"task_id": "task one/1"}, {"task_id": "task-2"}],
        })
        adapter = NowcoderResearchAdapter(LocalJsonClient("http://[::1]:8002", transport=transport))

        checkpoint = adapter.start(discovery_request(nowcoder_acquisition_mode="browser"))

        self.assertEqual(checkpoint.remote_ids, ("task one/1", "task-2"))
        self.assertEqual(checkpoint.state, {"keywords_json": '["AI Agent","RAG"]', "limit_per_source": 2})
        self.assertEqual(
            transport.calls,
            [
                ("POST", "/job-sources/nowcoder/tasks", {"keyword": "AI Agent", "recruitment_channel": "social", "city": "Beijing", "mode": "research", "acquisition_mode": "browser"}, 30),
                ("POST", "/job-sources/nowcoder/tasks", {"keyword": "RAG", "recruitment_channel": "social", "city": "Beijing", "mode": "research", "acquisition_mode": "browser"}, 30),
            ],
        )

    def test_poll_reports_interaction_before_other_task_states_and_does_not_fetch_jobs(self):
        transport = FakeTransport({
            ("GET", "/job-sources/nowcoder/tasks/task-1"): {"status": "running"},
            ("GET", "/job-sources/nowcoder/tasks/task-2"): {"status": "verification_required"},
        })
        adapter = NowcoderResearchAdapter(LocalJsonClient("http://localhost:8002", transport=transport))
        checkpoint = SourceCheckpoint("nowcoder", ("task-1", "task-2"), {"keywords_json": '["AI Agent","RAG"]', "limit_per_source": 2})

        result = adapter.poll(checkpoint)

        self.assertEqual(result.status, "interaction_required")
        self.assertEqual(result.jobs, ())
        self.assertEqual([call[1] for call in transport.calls], ["/job-sources/nowcoder/tasks/task-1", "/job-sources/nowcoder/tasks/task-2"])

    def test_poll_maps_completed_task_jobs_with_complete_and_partial_details(self):
        transport = FakeTransport({
            ("GET", "/job-sources/nowcoder/tasks/task-a"): {"status": "completed"},
            ("GET", "/job-sources/nowcoder/tasks/task-b"): {"status": "completed"},
            ("GET", "/job-sources/nowcoder/tasks/task-a/jobs"): {"jobs": [
                {
                    "source_job_id": "n-7",
                    "source_url": "https://www.nowcoder.com/jobs/detail/7?utm_source=x#top",
                    "title": "RAG Engineer",
                    "company": "Now Example",
                    "locations": ["Beijing", "Remote"],
                    "description": "Build retrieval services.",
                    "responsibilities": "Own production workflows.",
                    "requirements": "Python and RAG.",
                    "salary_text": "30K",
                    "published_at": "2026-08-25T20:00:00Z",
                    "last_verified_at": "2026-08-26T09:00:00+08:00",
                    "detail_status": "complete",
                    "score": 91.5,
                    "qualified": True,
                    "recruitment_channel": "social",
                    "employment_type": "full-time",
                    "upstream_platform": "nowcoder",
                    "raw_data": {"cookie": "must not survive"},
                }
            ]},
            ("GET", "/job-sources/nowcoder/tasks/task-b/jobs"): {"jobs": [
                {
                    "source_job_id": "n-8",
                    "source_url": "https://www.nowcoder.com/jobs/detail/8",
                    "title": "Agent Platform Engineer",
                    "company": "Now Example",
                    "locations": [],
                    "description": "",
                    "responsibilities": "",
                    "requirements": "",
                    "salary_text": None,
                    "published_at": None,
                    "last_verified_at": "2026-08-26T01:00:00Z",
                    "detail_status": "partial",
                    "score": None,
                    "qualified": None,
                    "recruitment_channel": "social",
                    "employment_type": "full-time",
                    "upstream_platform": "nowcoder",
                    "raw_data": {"token": "must not survive"},
                }
            ]},
        })
        adapter = NowcoderResearchAdapter(LocalJsonClient("http://localhost:8002", transport=transport))
        checkpoint = SourceCheckpoint("nowcoder", ("task-a", "task-b"), {"keywords_json": '["RAG","AI Agent"]', "limit_per_source": 2})

        result = adapter.poll(checkpoint)

        self.assertEqual(result.status, "completed")
        self.assertEqual([job.detail_status for job in result.jobs], ["complete", "partial"])
        complete, partial = result.jobs
        self.assertEqual(complete.detail, "Build retrieval services.\n\nOwn production workflows.\n\nPython and RAG.")
        self.assertEqual(complete.location, "Beijing, Remote")
        self.assertEqual(complete.canonical_url, "https://www.nowcoder.com/jobs/detail/7")
        self.assertEqual(complete.published_at, datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(complete.verified_at, datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc))
        self.assertNotIn("raw_data", complete.source_metadata)
        self.assertEqual(complete.source_metadata, {"recruitment_channel": "social", "employment_type": "full-time", "upstream_platform": "nowcoder"})
        self.assertEqual(partial.detail, "Agent Platform Engineer")
        self.assertEqual(partial.search_keyword, "AI Agent")

    def test_poll_reports_failed_only_when_no_collecting_or_interactive_task_remains(self):
        transport = FakeTransport({
            ("GET", "/job-sources/nowcoder/tasks/task-1"): {"status": "failed"},
            ("GET", "/job-sources/nowcoder/tasks/task-2"): {"status": "paused"},
        })
        adapter = NowcoderResearchAdapter(LocalJsonClient("http://localhost:8002", transport=transport))
        checkpoint = SourceCheckpoint("nowcoder", ("task-1", "task-2"), {"keywords_json": '["AI Agent","RAG"]', "limit_per_source": 2})

        result = adapter.poll(checkpoint)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "SOURCE_TASK_FAILED")

    def test_resolve_url_fetches_only_a_valid_nowcoder_job_and_uses_supplied_keyword(self):
        transport = FakeTransport({
            ("GET", "/job-sources/nowcoder/jobs/7"): {
                "source_job_id": "n-7",
                "source_url": "https://www.nowcoder.com/jobs/detail/7",
                "title": "RAG Engineer",
                "company": "Now Example",
                "locations": ["Beijing"],
                "description": "Build retrieval services.",
                "responsibilities": "",
                "requirements": "Python.",
                "salary_text": "30K",
                "published_at": None,
                "last_verified_at": "2026-08-26T01:00:00Z",
                "detail_status": "complete",
                "score": None,
                "qualified": True,
                "recruitment_channel": "social",
                "employment_type": "full-time",
                "upstream_platform": "nowcoder",
                "raw_data": {"session": "must not survive"},
            }
        })
        adapter = NowcoderResearchAdapter(LocalJsonClient("http://localhost:8002", transport=transport))

        job = adapter.resolve_url("https://www.nowcoder.com/jobs/detail/7?from=x#top", "RAG")

        self.assertEqual(job.source_job_id, "n-7")
        self.assertEqual(job.search_keyword, "RAG")
        with self.assertRaisesRegex(ValueError, "Nowcoder job URL"):
            adapter.resolve_url("https://www.nowcoder.com/jobs/detail/not-a-number", "RAG")


if __name__ == "__main__":
    unittest.main()
