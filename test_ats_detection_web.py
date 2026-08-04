import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_sources.ats import (
    AtsBatchItem,
    AtsBatchReport,
    cluster_ats_detections,
    detect_ats,
)
from job_sources.ats.listings import AtsJobCollection
from job_sources.contracts import NormalizedJob
from job_sources.router import router as job_sources_router


def report_for(detection) -> AtsBatchReport:
    item = AtsBatchItem(member_id="item-000001", detection=detection)
    return AtsBatchReport(
        items=(item,),
        clusters=cluster_ats_detections({item.member_id: detection}),
    )


def job_collection() -> AtsJobCollection:
    job = NormalizedJob(
        source="greenhouse",
        source_job_id="example:7",
        source_url="https://boards.greenhouse.io/example/jobs/7",
        official_apply_url="https://boards.greenhouse.io/example/jobs/7",
        title="Backend Engineer",
        company="Example Inc.",
        locations=("Remote",),
        recruitment_channel="social",
        employment_type=None,
        work_mode="remote",
        description="Build APIs",
        responsibilities=(),
        requirements=("Python",),
        salary_text=None,
        published_at=None,
        expires_at=None,
        last_verified_at="2026-08-04T12:00:00+00:00",
        campus=None,
        internship=None,
        raw_data={"departments": ("Engineering",)},
        upstream_platform="greenhouse",
        score=92.0,
        qualified=True,
    )
    return AtsJobCollection(
        ats="greenhouse",
        jobs=(job,),
        discovered_count=1,
        scanned_count=1,
        matched_count=1,
        complete=True,
        stop_reason="upstream_end",
    )


def board_catalog():
    from job_sources.ats.discovery import AtsBoard, AtsBoardCatalog

    return AtsBoardCatalog(
        boards=(
            AtsBoard(
                "greenhouse",
                "example",
                "https://job-boards.greenhouse.io/example",
            ),
        ),
        crawl="CC-MAIN-2026-30",
        discovered_at="2026-08-04T12:00:00+00:00",
        cached=False,
    )


def job_search_report():
    from job_sources.ats.search import AtsJobSearchReport

    return AtsJobSearchReport(
        jobs=job_collection().jobs,
        failures=(),
        searched_boards=board_catalog().boards,
        searched_count=1,
        succeeded_count=1,
        failed_count=0,
        discovered_count=1,
        matched_count=1,
    )


class AtsDetectionWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(job_sources_router)
        cls.client = TestClient(app)

    def test_allowed_local_origin_receives_privacy_safe_report(self):
        sensitive_url = "https://jobs.lever.co/example?token=secret"
        detection = detect_ats(sensitive_url, "")
        report = report_for(detection)

        with patch(
            "job_sources.ats.router.detect_ats_urls",
            return_value=report,
        ) as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                content=json.dumps({"urls": [sensitive_url]}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://localhost:3000",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:3000",
        )
        self.assertEqual(
            response.json()["items"][0]["member_id"],
            "item-000001",
        )
        self.assertNotIn(sensitive_url, response.text)
        self.assertNotIn("token", response.text)
        self.assertNotIn("secret", response.text)
        detect_batch.assert_called_once_with((sensitive_url,), max_workers=4)

    def test_exact_local_origin_preflight_is_allowed(self):
        response = self.client.options(
            "/job-sources/ats/detect",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://127.0.0.1:3000",
        )
        self.assertEqual(response.headers["access-control-allow-methods"], "POST")
        self.assertEqual(
            response.headers["access-control-allow-headers"],
            "content-type",
        )
        self.assertEqual(response.headers["vary"], "Origin")

    def test_non_local_browser_origin_is_rejected_before_detection(self):
        sensitive_url = "https://example.com/jobs?token=secret"
        with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                content=json.dumps({"urls": [sensitive_url]}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://attacker.example",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "origin_not_allowed"})
        self.assertNotIn(sensitive_url, response.text)
        self.assertNotIn("token", response.text)
        self.assertNotIn("secret", response.text)
        detect_batch.assert_not_called()

    def test_non_local_network_client_is_rejected_by_router_dependency(self):
        with TestClient(
            self.client.app,
            client=("203.0.113.1", 50_000),
        ) as remote_client:
            response = remote_client.post(
                "/job-sources/ats/detect",
                json={"urls": ["https://example.com/jobs"]},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "Resume Agent默认只允许本机访问"},
        )

    def test_non_local_origin_preflight_is_rejected(self):
        response = self.client.options(
            "/job-sources/ats/detect",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "origin_not_allowed"})
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_non_json_request_is_rejected_before_reading_values(self):
        sensitive_url = "https://example.com/jobs?token=secret"
        with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                content=json.dumps({"urls": [sensitive_url]}).encode("utf-8"),
                headers={
                    "Content-Type": "text/plain",
                    "Origin": "http://localhost:3000",
                },
            )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json(), {"detail": "unsupported_media_type"})
        self.assertNotIn(sensitive_url, response.text)
        self.assertNotIn("token", response.text)
        self.assertNotIn("secret", response.text)
        detect_batch.assert_not_called()

    def test_oversized_request_body_is_rejected_before_detection(self):
        sensitive_url = "https://example.com/jobs?token=secret&value=" + (
            "a" * (133 * 1_024)
        )
        body = json.dumps({"urls": [sensitive_url]}).encode("utf-8")
        self.assertGreater(len(body), 132 * 1_024)

        with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://localhost:3000",
                },
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "input_too_large"})
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:3000",
        )
        self.assertNotIn(sensitive_url[:100], response.text)
        self.assertNotIn("token", response.text)
        self.assertNotIn("secret", response.text)
        detect_batch.assert_not_called()

    def test_malformed_json_is_rejected_without_echoing_the_body(self):
        body = b'{"urls":["https://example.com/jobs?token=secret"'
        with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://localhost:3000",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "invalid_request"})
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:3000",
        )
        self.assertNotIn("example.com", response.text)
        self.assertNotIn("token", response.text)
        self.assertNotIn("secret", response.text)
        detect_batch.assert_not_called()

    def test_unpaired_unicode_surrogate_is_rejected_as_invalid_request(self):
        body = json.dumps({"urls": ["\ud800"]}).encode("ascii")

        with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "invalid_request"})
        detect_batch.assert_not_called()

    def test_invalid_request_shapes_are_rejected_before_detection(self):
        invalid_payloads = (
            {},
            {"urls": []},
            {"urls": ["   "]},
            {"urls": [123]},
            {"urls": "https://example.com/jobs"},
            {"urls": ["https://example.com/jobs"], "workers": 8},
            {"urls": [f"https://example.com/{index}" for index in range(21)]},
        )

        for payload in invalid_payloads:
            with self.subTest(payload_type=type(payload.get("urls")).__name__):
                with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
                    response = self.client.post(
                        "/job-sources/ats/detect",
                        content=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "invalid_request"})
                self.assertNotIn("example.com", response.text)
                detect_batch.assert_not_called()

    def test_concurrent_web_batch_is_rejected_without_starting_detection(self):
        detection = detect_ats("https://jobs.lever.co/example", "")
        report = report_for(detection)
        first_started = Event()
        release_first = Event()
        call_lock = Lock()
        call_count = 0

        def controlled_detection(urls, *, max_workers):
            nonlocal call_count
            with call_lock:
                call_count += 1
                call_number = call_count
            if call_number == 1:
                first_started.set()
                release_first.wait(timeout=5)
            return report

        def post_request():
            with TestClient(self.client.app) as client:
                return client.post(
                    "/job-sources/ats/detect",
                    json={"urls": ["https://jobs.lever.co/example"]},
                )

        with patch(
            "job_sources.ats.router.detect_ats_urls",
            side_effect=controlled_detection,
        ) as detect_batch:
            with ThreadPoolExecutor(max_workers=1) as executor:
                first_response = executor.submit(post_request)
                self.assertTrue(first_started.wait(timeout=2))
                try:
                    second_response = post_request()
                finally:
                    release_first.set()
                completed_first = first_response.result(timeout=5)

        self.assertEqual(completed_first.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(
            second_response.json(),
            {"detail": "detection_in_progress"},
        )
        self.assertEqual(detect_batch.call_count, 1)

    def test_detection_failure_is_safe_and_releases_the_web_batch(self):
        sensitive_url = "https://example.com/jobs?token=secret"
        origin = "http://localhost:3000"

        with patch(
            "job_sources.ats.router.detect_ats_urls",
            side_effect=RuntimeError(sensitive_url),
        ):
            failed_response = self.client.post(
                "/job-sources/ats/detect",
                json={"urls": [sensitive_url]},
                headers={"Origin": origin},
            )

        detection = detect_ats("https://jobs.lever.co/example", "")
        with patch(
            "job_sources.ats.router.detect_ats_urls",
            return_value=report_for(detection),
        ):
            retry_response = self.client.post(
                "/job-sources/ats/detect",
                json={"urls": ["https://jobs.lever.co/example"]},
                headers={"Origin": origin},
            )

        self.assertEqual(failed_response.status_code, 502)
        self.assertEqual(failed_response.json(), {"detail": "detection_failed"})
        self.assertEqual(
            failed_response.headers["access-control-allow-origin"],
            origin,
        )
        self.assertNotIn(sensitive_url, failed_response.text)
        self.assertNotIn("token", failed_response.text)
        self.assertNotIn("secret", failed_response.text)
        self.assertEqual(retry_response.status_code, 200, retry_response.text)

    def test_serialization_failure_is_safe_and_releases_the_web_batch(self):
        url = "https://jobs.lever.co/example"
        detection = detect_ats(url, "")
        report = report_for(detection)

        with (
            patch("job_sources.ats.router.detect_ats_urls", return_value=report),
            patch(
                "job_sources.ats.router.serialize_ats_batch_report",
                side_effect=RuntimeError("sensitive serialization detail"),
            ),
        ):
            failed_response = self.client.post(
                "/job-sources/ats/detect",
                json={"urls": [url]},
            )

        with patch(
            "job_sources.ats.router.detect_ats_urls",
            return_value=report,
        ):
            retry_response = self.client.post(
                "/job-sources/ats/detect",
                json={"urls": [url]},
            )

        self.assertEqual(failed_response.status_code, 502)
        self.assertEqual(failed_response.json(), {"detail": "detection_failed"})
        self.assertNotIn("sensitive", failed_response.text)
        self.assertEqual(retry_response.status_code, 200, retry_response.text)

    def test_no_origin_local_client_is_allowed_and_url_is_trimmed(self):
        url = "https://jobs.lever.co/example"
        detection = detect_ats(url, "")
        with patch(
            "job_sources.ats.router.detect_ats_urls",
            return_value=report_for(detection),
        ) as detect_batch:
            response = self.client.post(
                "/job-sources/ats/detect",
                json={"urls": [f"  {url}  "]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("access-control-allow-origin", response.headers)
        detect_batch.assert_called_once_with((url,), max_workers=4)

    def test_url_byte_bounds_are_rejected_before_detection(self):
        oversized_url = "https://example.com/" + ("a" * 8_173)
        self.assertEqual(len(oversized_url.encode("utf-8")), 8_193)
        total_url = "https://x.co/" + ("a" * 6_542)
        total_urls = [total_url for _ in range(20)]
        self.assertGreater(
            sum(len(url.encode("utf-8")) for url in total_urls),
            128 * 1_024,
        )

        for urls in ([oversized_url], total_urls):
            with self.subTest(url_count=len(urls)):
                with patch("job_sources.ats.router.detect_ats_urls") as detect_batch:
                    response = self.client.post(
                        "/job-sources/ats/detect",
                        json={"urls": urls},
                    )

                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json(), {"detail": "input_too_large"})
                self.assertNotIn("example.com", response.text)
                detect_batch.assert_not_called()

    def test_collects_scored_jobs_for_an_allowed_local_origin(self):
        origin = "http://localhost:3000"
        with patch(
            "job_sources.ats.router.collect_ats_jobs",
            return_value=job_collection(),
        ) as collect_jobs:
            response = self.client.post(
                "/job-sources/ats/jobs",
                json={
                    "url": "https://boards.greenhouse.io/example",
                    "company": "Example Inc.",
                    "keyword": "python",
                    "limit": 10,
                },
                headers={"Origin": origin},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], origin)
        payload = response.json()
        self.assertEqual(payload["ats"], "greenhouse")
        self.assertEqual(payload["jobs"][0]["title"], "Backend Engineer")
        self.assertEqual(payload["jobs"][0]["score"], 92.0)
        collect_jobs.assert_called_once_with(
            "https://boards.greenhouse.io/example",
            company="Example Inc.",
            keyword="python",
            limit=10,
            score=True,
        )

    def test_rejects_invalid_or_oversized_job_collection_requests(self):
        invalid_payloads = (
            {},
            {"url": "https://jobs.lever.co/example"},
            {
                "url": "https://jobs.lever.co/example",
                "company": None,
                "keyword": None,
                "limit": 0,
            },
            {
                "url": 7,
                "company": None,
                "keyword": None,
                "limit": 20,
            },
        )
        with patch("job_sources.ats.router.collect_ats_jobs") as collect_jobs:
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    response = self.client.post(
                        "/job-sources/ats/jobs",
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"detail": "invalid_request"})

            oversized = json.dumps(
                {
                    "url": "https://jobs.lever.co/example",
                    "company": "x" * (17 * 1_024),
                    "keyword": None,
                    "limit": 20,
                }
            ).encode("utf-8")
            response = self.client.post(
                "/job-sources/ats/jobs",
                content=oversized,
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "input_too_large"})
        collect_jobs.assert_not_called()

    def test_job_collection_failure_is_safe_and_lock_is_released(self):
        request = {
            "url": "https://jobs.lever.co/example",
            "company": None,
            "keyword": None,
            "limit": 20,
        }
        with patch(
            "job_sources.ats.router.collect_ats_jobs",
            side_effect=RuntimeError("secret upstream response"),
        ):
            failed = self.client.post("/job-sources/ats/jobs", json=request)
        with patch(
            "job_sources.ats.router.collect_ats_jobs",
            return_value=job_collection(),
        ):
            retried = self.client.post("/job-sources/ats/jobs", json=request)

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed.json(), {"detail": "collection_failed"})
        self.assertNotIn("secret", failed.text)
        self.assertEqual(retried.status_code, 200, retried.text)

    def test_concurrent_job_collection_is_rejected(self):
        from job_sources.ats import router as ats_router_module

        request = {
            "url": "https://jobs.lever.co/example",
            "company": None,
            "keyword": None,
            "limit": 20,
        }
        self.assertTrue(ats_router_module._WEB_JOB_LOCK.acquire(blocking=False))
        try:
            with patch("job_sources.ats.router.collect_ats_jobs") as collect_jobs:
                response = self.client.post("/job-sources/ats/jobs", json=request)
        finally:
            ats_router_module._WEB_JOB_LOCK.release()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "collection_in_progress"})
        collect_jobs.assert_not_called()

    def test_discovers_public_ats_boards_without_company_urls(self):
        with patch(
            "job_sources.ats.router.discover_ats_boards",
            return_value=board_catalog(),
        ) as discover:
            response = self.client.post(
                "/job-sources/ats/boards/discover",
                json={"limit": 25, "refresh": True},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["crawl"], "CC-MAIN-2026-30")
        self.assertFalse(payload["cached"])
        self.assertEqual(payload["boards"][0]["tenant"], "example")
        discover.assert_called_once_with(limit=25, refresh=True)

    def test_searches_discovered_catalog_with_keyword_only(self):
        with (
            patch(
                "job_sources.ats.router.discover_ats_boards",
                return_value=board_catalog(),
            ) as discover,
            patch(
                "job_sources.ats.router.search_ats_boards",
                return_value=job_search_report(),
            ) as search,
        ):
            response = self.client.post(
                "/job-sources/ats/jobs/search",
                json={
                    "keyword": " frontend ",
                    "limit": 30,
                    "max_boards": 10,
                    "refresh": False,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["jobs"][0]["title"], "Backend Engineer")
        self.assertEqual(payload["searched_count"], 1)
        self.assertEqual(payload["searched_boards"][0]["tenant"], "example")
        discover.assert_called_once_with(limit=50, refresh=False)
        search.assert_called_once_with(
            board_catalog().boards,
            keyword="frontend",
            limit=30,
            max_boards=10,
            max_workers=4,
        )

    def test_rejects_invalid_zero_url_discovery_and_search_requests(self):
        invalid_discovery = (
            {},
            {"limit": 0, "refresh": False},
            {"limit": 51, "refresh": False},
            {"limit": 20, "refresh": "yes"},
            {"limit": 20, "refresh": False, "url": "https://example.com"},
        )
        invalid_search = (
            {},
            {"keyword": "", "limit": 20, "max_boards": 10, "refresh": False},
            {"keyword": "x", "limit": 0, "max_boards": 10, "refresh": False},
            {"keyword": "x", "limit": 20, "max_boards": 21, "refresh": False},
            {
                "keyword": "x",
                "limit": 20,
                "max_boards": 10,
                "refresh": False,
                "urls": ["https://example.com"],
            },
        )
        with (
            patch("job_sources.ats.router.discover_ats_boards") as discover,
            patch("job_sources.ats.router.search_ats_boards") as search,
        ):
            for payload in invalid_discovery:
                response = self.client.post(
                    "/job-sources/ats/boards/discover",
                    json=payload,
                )
                self.assertEqual(response.status_code, 400, payload)
                self.assertEqual(response.json(), {"detail": "invalid_request"})
            for payload in invalid_search:
                response = self.client.post(
                    "/job-sources/ats/jobs/search",
                    json=payload,
                )
                self.assertEqual(response.status_code, 400, payload)
                self.assertEqual(response.json(), {"detail": "invalid_request"})

        discover.assert_not_called()
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
