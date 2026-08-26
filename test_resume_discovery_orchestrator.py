import json
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import unittest

from resume_agent.discovery_schemas import DiscoveryRequest, DiscoveredJob
from resume_agent.discovery_sources import SourceCheckpoint, SourcePollResult
from resume_agent.repository import Repository


def discovery_request(candidate_id, **overrides):
    payload = {
        "candidate_id": candidate_id,
        "sources": ["boss", "nowcoder"],
        "keywords": ["AI Agent", "RAG"],
        "city": "Beijing",
        "recruitment_channel": "social",
        "limit_per_source": 10,
        "boss_project_id": "private-project-id",
    }
    payload.update(overrides)
    return DiscoveryRequest(**payload)


def job(source, number, *, url_number=None, title=None):
    url_number = number if url_number is None else url_number
    host = "www.zhipin.com/job_detail" if source == "boss" else "www.nowcoder.com/jobs/detail"
    suffix = ".html" if source == "boss" else ""
    return DiscoveredJob(
        source=source,
        source_job_id=f"{source}-{number}",
        canonical_url=f"https://{host}/{url_number}{suffix}",
        title=title or f"AI Engineer {number}",
        company=f"Company {number}",
        detail=f"Build agent system {number}",
        detail_status="complete",
        search_keyword="AI Agent",
        verified_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )


class FakeSource:
    def __init__(self, source, outcomes, *, start_error=None):
        self.source = source
        self.outcomes = deque(outcomes)
        self.start_error = start_error
        self.start_calls = 0
        self.poll_calls = 0

    def start(self, request):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return SourceCheckpoint(
            self.source,
            (f"{self.source}-remote",),
            {"keywords_json": json.dumps(request.keywords), "limit_per_source": 10},
        )

    def poll(self, checkpoint):
        self.poll_calls += 1
        outcome = self.outcomes[0] if len(self.outcomes) == 1 else self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        status, jobs, error_code = outcome
        return SourcePollResult(
            self.source, status, tuple(jobs), checkpoint, error_code=error_code
        )


def completed(source, jobs):
    return FakeSource(source, [("completed", jobs, None)])


def failed(source, error_code="SOURCE_TASK_FAILED"):
    return FakeSource(source, [("failed", [], error_code)])


class JobDiscoveryOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = Repository(self.root / "resume-agent.db")
        self.candidate_id = self.repository.create_candidate(
            "Candidate", ["AI Engineer"], ""
        )["id"]
        self.runs_root = self.root / "runs"

    def tearDown(self):
        self.temp_dir.cleanup()

    def orchestrator(self, adapters):
        from resume_agent.discovery_orchestrator import JobDiscoveryOrchestrator

        return JobDiscoveryOrchestrator(self.repository, self.runs_root, adapters)

    def start_and_poll(self, adapters, **request_overrides):
        orchestrator = self.orchestrator(adapters)
        started = orchestrator.start(
            discovery_request(self.candidate_id, **request_overrides)
        )
        return orchestrator, started, orchestrator.poll(started["id"])

    def test_poll_status_matrix(self):
        cases = [
            (
                "all_success_enough",
                completed("boss", [job("boss", 1)]),
                completed("nowcoder", [job("nowcoder", 2), job("nowcoder", 3)]),
                "READY_FOR_IMPORT",
            ),
            (
                "all_success_not_enough",
                completed("boss", [job("boss", 1)]),
                completed("nowcoder", [job("nowcoder", 2)]),
                "FAILED",
            ),
            (
                "interaction_even_when_enough",
                FakeSource("boss", [("interaction_required", [], "SOURCE_INTERACTION_REQUIRED")]),
                completed("nowcoder", [job("nowcoder", 1), job("nowcoder", 2), job("nowcoder", 3)]),
                "COLLECTING",
            ),
            (
                "collecting_even_when_enough",
                FakeSource("boss", [("collecting", [], None)]),
                completed("nowcoder", [job("nowcoder", 1), job("nowcoder", 2), job("nowcoder", 3)]),
                "COLLECTING",
            ),
            (
                "one_failed_enough",
                failed("boss"),
                completed("nowcoder", [job("nowcoder", 1), job("nowcoder", 2), job("nowcoder", 3)]),
                "PARTIAL",
            ),
            (
                "one_failed_not_enough",
                failed("boss"),
                completed("nowcoder", [job("nowcoder", 1), job("nowcoder", 2)]),
                "FAILED",
            ),
            (
                "all_failed",
                failed("boss"),
                failed("nowcoder"),
                "FAILED",
            ),
            (
                "duplicates_reduce_below_minimum",
                completed("boss", [job("boss", 1), job("boss", 2)]),
                completed("nowcoder", [job("nowcoder", 3, url_number=2), job("nowcoder", 4)]),
                "READY_FOR_IMPORT",
            ),
        ]
        for name, boss, nowcoder, expected in cases:
            with self.subTest(name=name):
                # The duplicate case uses a same-content duplicate, because source URLs differ.
                if name == "duplicates_reduce_below_minimum":
                    duplicate = job("nowcoder", 3, title="AI Engineer 2")
                    duplicate = duplicate.model_copy(
                        update={"company": "Company 2", "detail": "Build agent system 2"}
                    )
                    nowcoder.outcomes = deque([("completed", [duplicate], None)])
                    expected = "FAILED"
                _, _, polled = self.start_and_poll({"boss": boss, "nowcoder": nowcoder})
                self.assertEqual(polled["status"], expected)

    def test_start_validates_candidate_and_adapter_availability_before_artifacts(self):
        adapters = {"boss": completed("boss", [])}
        orchestrator = self.orchestrator(adapters)

        with self.assertRaisesRegex(ValueError, "adapter"):
            orchestrator.start(discovery_request(self.candidate_id))
        self.assertFalse(self.runs_root.exists())

        request = discovery_request("candidate_missing", sources=["boss"])
        with self.assertRaises(Exception):
            orchestrator.start(request)
        self.assertFalse(self.runs_root.exists())

    def test_start_degrades_source_exceptions_and_never_exposes_exception_text(self):
        secret = "candidate@example.com?token=raw-secret"
        boss = FakeSource("boss", [], start_error=RuntimeError(secret))
        nowcoder = completed("nowcoder", [job("nowcoder", 1)])

        orchestrator = self.orchestrator({"boss": boss, "nowcoder": nowcoder})
        started = orchestrator.start(discovery_request(self.candidate_id))

        self.assertEqual(started["status"], "COLLECTING")
        self.assertEqual(started["source_statuses"], {"boss": "failed", "nowcoder": "collecting"})
        self.assertNotIn("boss", started["source_checkpoints"])
        audit = (Path(started["artifact_dir"]) / "audit.md").read_text("utf-8")
        self.assertNotIn(secret, audit)
        self.assertIn('"error_code": "SOURCE_UNAVAILABLE"', audit)

        polled = orchestrator.poll(started["id"])
        self.assertEqual(polled["status"], "FAILED")
        self.assertEqual(boss.poll_calls, 0)

    def test_all_start_exceptions_end_failed_instead_of_created(self):
        adapters = {
            "boss": FakeSource("boss", [], start_error=RuntimeError("raw-cookie")),
            "nowcoder": FakeSource("nowcoder", [], start_error=ValueError("raw-url")),
        }
        started = self.orchestrator(adapters).start(discovery_request(self.candidate_id))

        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(started["error_code"], "DISCOVERY_FAILED")
        self.assertNotEqual(self.repository.get_discovery_run(started["id"])["status"], "CREATED")

    def test_non_json_safe_checkpoint_degrades_without_stranding_created_run(self):
        boss = completed("boss", [])
        boss.start = lambda request: SourceCheckpoint(
            "boss", ("boss-remote",), {"invalid_number": float("nan")}
        )
        nowcoder = completed("nowcoder", [])

        started = self.orchestrator({"boss": boss, "nowcoder": nowcoder}).start(
            discovery_request(self.candidate_id)
        )

        self.assertEqual(started["status"], "COLLECTING")
        self.assertEqual(started["source_statuses"]["boss"], "failed")
        self.assertNotIn("boss", started["source_checkpoints"])

    def test_poll_exception_is_safe_and_preserves_previously_written_jobs(self):
        boss = FakeSource(
            "boss",
            [
                ("collecting", [job("boss", 1)], None),
                RuntimeError("phone=13800138000&cookie=secret"),
            ],
        )
        nowcoder = FakeSource(
            "nowcoder", [("collecting", [job("nowcoder", 2)], None)]
        )
        orchestrator = self.orchestrator({"boss": boss, "nowcoder": nowcoder})
        started = orchestrator.start(discovery_request(self.candidate_id))
        first = orchestrator.poll(started["id"])
        second = orchestrator.poll(started["id"])

        results = json.loads(
            (Path(second["artifact_dir"]) / "source-results.json").read_text("utf-8")
        )
        self.assertEqual([row["source_job_id"] for row in results], ["boss-1", "nowcoder-2"])
        self.assertEqual(second["source_statuses"]["boss"], "failed")
        audit = (Path(second["artifact_dir"]) / "audit.md").read_text("utf-8")
        self.assertNotIn("13800138000", audit)
        self.assertNotIn("cookie", audit.lower())
        self.assertIn('"error_code": "SOURCE_UNAVAILABLE"', audit)

    def test_repeated_ready_poll_is_idempotent_and_never_starts_or_polls_again(self):
        boss = completed("boss", [job("boss", 1)])
        nowcoder = completed("nowcoder", [job("nowcoder", 2), job("nowcoder", 3)])
        orchestrator = self.orchestrator({"boss": boss, "nowcoder": nowcoder})
        started = orchestrator.start(discovery_request(self.candidate_id))
        first = orchestrator.poll(started["id"])
        results_path = Path(first["artifact_dir"]) / "source-results.json"
        first_results = results_path.read_text("utf-8")

        second = orchestrator.poll(started["id"])

        self.assertEqual(second["status"], "READY_FOR_IMPORT")
        self.assertEqual(results_path.read_text("utf-8"), first_results)
        self.assertEqual((boss.start_calls, nowcoder.start_calls), (1, 1))
        self.assertEqual((boss.poll_calls, nowcoder.poll_calls), (1, 1))

    def test_failed_runs_are_not_pollable(self):
        boss = completed("boss", [job("boss", 1)])
        nowcoder = completed("nowcoder", [])
        orchestrator, started, failed_run = self.start_and_poll(
            {"boss": boss, "nowcoder": nowcoder}
        )
        self.assertEqual(failed_run["status"], "FAILED")

        with self.assertRaisesRegex(ValueError, "not pollable"):
            orchestrator.poll(started["id"])
        self.assertEqual((boss.poll_calls, nowcoder.poll_calls), (1, 1))

    def test_snapshot_and_audit_are_structured_and_redacted(self):
        private = "candidate@example.com 13800138000 secret-project"
        request = discovery_request(
            self.candidate_id,
            keywords=[private],
            city=private,
            boss_project_id=private,
        )
        started = self.orchestrator(
            {"boss": completed("boss", []), "nowcoder": completed("nowcoder", [])}
        ).start(request)
        artifact_dir = Path(started["artifact_dir"])
        snapshot_text = (artifact_dir / "input-snapshot.json").read_text("utf-8")
        audit_text = (artifact_dir / "audit.md").read_text("utf-8")

        self.assertNotIn(private, snapshot_text)
        self.assertNotIn("candidate@example.com", audit_text)
        snapshot = json.loads(snapshot_text)
        self.assertEqual(snapshot["keyword_count"], 1)
        self.assertTrue(snapshot["city_configured"])
        self.assertTrue(snapshot["boss_project_configured"])

    def test_get_validates_artifact_consistency_without_mutation(self):
        orchestrator = self.orchestrator(
            {"boss": completed("boss", []), "nowcoder": completed("nowcoder", [])}
        )
        started = orchestrator.start(discovery_request(self.candidate_id))
        audit_path = Path(started["artifact_dir"]) / "audit.md"
        before = audit_path.read_text("utf-8")

        fetched = orchestrator.get(started["id"])

        self.assertEqual(fetched, started)
        self.assertEqual(audit_path.read_text("utf-8"), before)

        with self.repository._connect() as connection:
            connection.execute(
                "UPDATE discovery_runs SET artifact_dir=? WHERE id=?",
                (str(self.root / "wrong"), started["id"]),
            )
        with self.assertRaisesRegex(ValueError, "artifact"):
            orchestrator.get(started["id"])


if __name__ == "__main__":
    unittest.main()
