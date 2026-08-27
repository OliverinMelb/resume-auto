import json
import sqlite3
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from resume_agent.discovery_artifacts import DiscoveryArtifacts
from resume_agent.discovery_schemas import (
    DiscoveryRequest,
    DiscoveredJob,
    discovered_job_fingerprint,
)
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


def job(source, number, *, url_number=None, title=None, **overrides):
    url_number = number if url_number is None else url_number
    host = "www.zhipin.com/job_detail" if source == "boss" else "www.nowcoder.com/jobs/detail"
    suffix = ".html" if source == "boss" else ""
    payload = {
        "source": source,
        "source_job_id": f"{source}-{number}",
        "canonical_url": f"https://{host}/{url_number}{suffix}",
        "title": title or f"AI Engineer {number}",
        "company": f"Company {number}",
        "detail": f"Build agent system {number}",
        "detail_status": "complete",
        "search_keyword": "AI Agent",
        "verified_at": datetime(2026, 8, 26, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return DiscoveredJob(
        **payload
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
        if isinstance(outcome, SourcePollResult):
            return outcome
        status, jobs, error_code = outcome
        return SourcePollResult(
            self.source, status, tuple(jobs), checkpoint, error_code=error_code
        )


class SharedPollGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.release = threading.Event()
        self.entered = 0
        self.active = 0
        self.max_active = 0

    def enter(self):
        with self.condition:
            self.entered += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        if not self.release.wait(timeout=5):
            raise AssertionError("poll gate was not released")
        with self.condition:
            self.active -= 1
            self.condition.notify_all()


class BlockingPollSource(FakeSource):
    def __init__(self, source, jobs, gate):
        super().__init__(source, [("completed", jobs, None)])
        self.gate = gate

    def poll(self, checkpoint):
        self.poll_calls += 1
        self.gate.enter()
        return SourcePollResult(
            self.source, "completed", tuple(self.outcomes[0][1]), checkpoint
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

    def orchestrator(self, adapters, *, repository=None, lease_duration_seconds=300):
        from resume_agent.discovery_orchestrator import JobDiscoveryOrchestrator

        return JobDiscoveryOrchestrator(
            repository or self.repository,
            self.runs_root,
            adapters,
            lease_duration_seconds=lease_duration_seconds,
        )

    def start_and_poll(self, adapters, **request_overrides):
        orchestrator = self.orchestrator(adapters)
        started = orchestrator.start(
            discovery_request(self.candidate_id, **request_overrides)
        )
        return orchestrator, started, orchestrator.poll(started["id"])

    def ready_run(self, jobs):
        by_source = {
            source: [item for item in jobs if item.source == source]
            for source in ("boss", "nowcoder")
        }
        return self.start_and_poll(
            {
                "boss": completed("boss", by_source["boss"]),
                "nowcoder": completed("nowcoder", by_source["nowcoder"]),
            }
        )

    def test_import_results_maps_without_invention_and_is_idempotent(self):
        discovered = [
            job(
                "boss",
                1,
                salary_raw="20-30K",
                score=91.5,
                qualified=True,
                search_keyword="Agent",
            ),
            job("nowcoder", 2, qualified=False),
            job("boss", 3, company="", qualified=None),
        ]
        orchestrator, _, ready = self.ready_run(discovered)

        imported = orchestrator.import_results(ready["id"])
        retried = orchestrator.import_results(ready["id"])

        self.assertEqual(imported, retried)
        self.assertEqual(imported["status"], "IMPORTED")
        self.assertEqual(len(imported["imported_job_ids"]), 3)
        rows = self.repository.get_jobs(imported["imported_job_ids"])
        by_external_id = {row["external_job_id"]: row for row in rows}
        first = by_external_id["boss-1"]
        self.assertEqual(first["source"], "boss")
        self.assertEqual(first["title"], discovered[0].title)
        self.assertEqual(first["detail"], discovered[0].detail)
        self.assertEqual(first["company"], discovered[0].company)
        self.assertEqual(first["salary"], "20-30K")
        self.assertEqual(first["score"], 91.5)
        self.assertEqual(first["qualified"], 1)
        self.assertEqual(first["search_keyword"], "Agent")
        self.assertEqual(
            first["source_logged_at"], "2026-08-26T00:00:00+00:00"
        )
        self.assertEqual(
            first["job_fingerprint"], discovered_job_fingerprint(discovered[0])
        )
        self.assertEqual(len(self.repository.list_jobs(self.candidate_id)), 3)
        artifact_dir = Path(imported["artifact_dir"])
        source_results = (artifact_dir / "source-results.json").read_text("utf-8")
        report = (artifact_dir / "discovery-report.md").read_text("utf-8")
        self.assertIn(discovered[0].canonical_url, source_results)
        self.assertNotIn(discovered[0].canonical_url, report)
        self.assertNotIn(discovered[0].detail, report)

    def test_import_results_rejects_invalid_saved_results_without_mutation(self):
        orchestrator, _, ready = self.ready_run(
            [job("boss", 1), job("nowcoder", 2), job("boss", 3)]
        )
        artifact_dir = Path(ready["artifact_dir"])
        (artifact_dir / "source-results.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "results artifact"):
            orchestrator.import_results(ready["id"])

        self.assertEqual(
            self.repository.get_discovery_run(ready["id"])["status"],
            "READY_FOR_IMPORT",
        )
        self.assertEqual(self.repository.list_jobs(self.candidate_id), [])
        self.assertFalse((artifact_dir / "discovery-report.md").exists())

    def test_import_results_defensively_deduplicates_valid_saved_results(self):
        discovered = [job("boss", 1), job("nowcoder", 2), job("boss", 3)]
        orchestrator, _, ready = self.ready_run(discovered)
        artifacts = DiscoveryArtifacts.open(self.runs_root, ready["id"])
        artifacts.write_results([discovered[0], *discovered, discovered[1]])

        imported = orchestrator.import_results(ready["id"])

        self.assertEqual(len(imported["imported_job_ids"]), 3)
        self.assertEqual(len(self.repository.list_jobs(self.candidate_id)), 3)
        report = (Path(ready["artifact_dir"]) / "discovery-report.md").read_text(
            "utf-8"
        )
        self.assertIn("Saved jobs: 5", report)
        self.assertIn("Deduplicated jobs: 3", report)

    def test_insufficient_complete_agent_jobs_fail_without_import_or_rewrite(self):
        discovered = [
            job("boss", 1),
            job("nowcoder", 2),
            job("boss", 3, detail_status="partial"),
        ]
        orchestrator, _, ready = self.ready_run(discovered)
        pipeline = Mock()

        failed = orchestrator.import_results(ready["id"])

        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(
            failed["error_code"], "INSUFFICIENT_COMPLETE_AGENT_JOBS"
        )
        self.assertEqual(failed["imported_job_ids"], [])
        self.assertEqual(self.repository.list_jobs(self.candidate_id), [])
        with self.assertRaisesRegex(ValueError, "not importable"):
            orchestrator.import_results(ready["id"])

        second_orchestrator, _, second_ready = self.ready_run(
            [
                job("boss", 11),
                job("nowcoder", 12),
                job("boss", 13, detail_status="partial"),
            ]
        )
        with self.assertRaisesRegex(ValueError, "at least 3"):
            second_orchestrator.build_broad_rewrite(
                second_ready["id"], self.candidate_id, ["resume-1"], pipeline
            )
        pipeline.run.assert_not_called()

    def test_create_job_if_absent_serializes_concurrent_retries(self):
        payload = {
            "candidate_id": self.candidate_id,
            "external_job_id": "boss-1",
            "source": "boss",
            "title": "AI Agent Engineer",
            "salary": "",
            "detail": "Build Agent systems",
            "company": "Company",
            "qualified": True,
            "search_keyword": "Agent",
            "source_logged_at": "2026-08-26T00:00:00+00:00",
        }
        fingerprint = "a" * 64
        barrier = threading.Barrier(6)
        results = []
        errors = []

        def create(repository):
            try:
                barrier.wait(timeout=5)
                results.append(
                    repository.create_job_if_absent(payload, fingerprint)
                )
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(
                target=create,
                args=(Repository(self.repository.database_path),),
            )
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({row["id"] for row, _ in results}), 1)
        self.assertEqual(len(self.repository.list_jobs(self.candidate_id)), 1)
        with self.repository._connect() as connection:
            indexes = connection.execute("PRAGMA index_list(jobs)").fetchall()
        self.assertFalse(
            any(
                row["name"] == "idx_jobs_fingerprint" and row["unique"]
                for row in indexes
            )
        )

    def test_concurrent_imports_converge_without_duplicate_jobs_or_ids(self):
        discovered = [job("boss", 1), job("nowcoder", 2), job("boss", 3)]
        orchestrator, _, ready = self.ready_run(discovered)
        barrier = threading.Barrier(2)

        class GatedRepository(Repository):
            def create_job_if_absent(self, payload, fingerprint):
                barrier.wait(timeout=5)
                return super().create_job_if_absent(payload, fingerprint)

        contenders = [
            self.orchestrator(
                {},
                repository=GatedRepository(self.repository.database_path),
            )
            for _ in range(2)
        ]
        results = []
        errors = []

        def import_run(contender):
            try:
                results.append(contender.import_results(ready["id"]))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=import_run, args=(contender,))
            for contender in contenders
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(
            [run["status"] for run in results], ["IMPORTED", "IMPORTED"]
        )
        self.assertEqual(
            results[0]["imported_job_ids"], results[1]["imported_job_ids"]
        )
        self.assertEqual(len(self.repository.list_jobs(self.candidate_id)), 3)

    def test_broad_rewrite_checks_candidate_then_preserves_selected_order(self):
        discovered = [job("boss", 1), job("nowcoder", 2), job("boss", 3)]
        orchestrator, _, ready = self.ready_run(discovered)
        other_candidate = self.repository.create_candidate(
            "Other", ["AI Engineer"], ""
        )["id"]
        mismatch_pipeline = Mock()

        with patch.object(orchestrator, "import_results") as import_results:
            with self.assertRaisesRegex(ValueError, "candidate"):
                orchestrator.build_broad_rewrite(
                    ready["id"], other_candidate, ["resume-1"], mismatch_pipeline
                )
        import_results.assert_not_called()
        mismatch_pipeline.run.assert_not_called()

        pipeline = Mock()
        pipeline.run.return_value = {"id": "rewrite-1"}
        rewrite = orchestrator.build_broad_rewrite(
            ready["id"], self.candidate_id, ["resume-1"], pipeline
        )
        imported = self.repository.get_discovery_run(ready["id"])

        self.assertEqual(rewrite, {"id": "rewrite-1"})
        pipeline.run.assert_called_once()
        request = pipeline.run.call_args.args[0]
        self.assertEqual(request.job_ids, imported["imported_job_ids"])
        self.assertEqual(request.candidate_id, self.candidate_id)
        self.assertEqual(request.source_resume_ids, ["resume-1"])
        self.assertRegex(
            request.output_name, r"^Candidate-Agent综合版-\d{8}\.pdf$"
        )

    def test_broad_rewrite_uses_safe_fallback_for_missing_display_name(self):
        candidate = self.repository.create_candidate(None, ["AI Engineer"], "")
        discovered = [job("boss", 11), job("nowcoder", 12), job("boss", 13)]
        by_source = {
            source: [item for item in discovered if item.source == source]
            for source in ("boss", "nowcoder")
        }
        orchestrator = self.orchestrator(
            {
                "boss": completed("boss", by_source["boss"]),
                "nowcoder": completed("nowcoder", by_source["nowcoder"]),
            }
        )
        started = orchestrator.start(discovery_request(candidate["id"]))
        ready = orchestrator.poll(started["id"])
        pipeline = Mock(return_value={"id": "rewrite-2"})
        pipeline.run.return_value = {"id": "rewrite-2"}

        orchestrator.build_broad_rewrite(
            ready["id"], candidate["id"], ["resume-2"], pipeline
        )

        output_name = pipeline.run.call_args.args[0].output_name
        self.assertRegex(output_name, r"^候选人-Agent综合版-\d{8}\.pdf$")

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

    def test_non_json_safe_poll_checkpoint_keeps_prior_checkpoint_and_results(self):
        boss = FakeSource(
            "boss",
            [
                ("collecting", [job("boss", 1)], None),
                ("completed", [job("boss", 2), job("boss", 3)], None),
            ],
        )
        nowcoder = FakeSource(
            "nowcoder",
            [("collecting", [], None)],
        )
        orchestrator = self.orchestrator({"boss": boss, "nowcoder": nowcoder})
        started = orchestrator.start(discovery_request(self.candidate_id))
        first = orchestrator.poll(started["id"])
        previous_nowcoder_checkpoint = first["source_checkpoints"]["nowcoder"]
        nowcoder.outcomes = deque(
            [
                SourcePollResult(
                    "nowcoder",
                    "completed",
                    (job("nowcoder", 99),),
                    SourceCheckpoint(
                        "nowcoder",
                        ("nowcoder-advanced",),
                        {"invalid_number": float("nan")},
                    ),
                )
            ]
        )

        second = orchestrator.poll(started["id"])
        results_path = Path(second["artifact_dir"]) / "source-results.json"
        result_ids = [
            row["source_job_id"]
            for row in json.loads(results_path.read_text("utf-8"))
        ]

        self.assertEqual(second["status"], "PARTIAL")
        self.assertEqual(second["source_statuses"]["boss"], "completed")
        self.assertEqual(second["source_statuses"]["nowcoder"], "failed")
        self.assertEqual(
            second["source_checkpoints"]["nowcoder"], previous_nowcoder_checkpoint
        )
        self.assertEqual(result_ids, ["boss-1", "boss-2", "boss-3"])
        self.assertNotIn("NaN", results_path.read_text("utf-8"))
        audit = (Path(second["artifact_dir"]) / "audit.md").read_text("utf-8")
        self.assertIn('"error_code": "INVALID_SOURCE_CHECKPOINT"', audit)
        self.assertNotIn("NaN", audit)

        repeated = orchestrator.poll(started["id"])
        self.assertEqual(repeated["status"], "PARTIAL")
        self.assertEqual((boss.poll_calls, nowcoder.poll_calls), (2, 2))

    def test_start_audit_io_failure_returns_durable_collecting_checkpoint(self):
        boss = completed("boss", [])
        nowcoder = completed("nowcoder", [])
        orchestrator = self.orchestrator({"boss": boss, "nowcoder": nowcoder})
        original_append = DiscoveryArtifacts.append_audit

        def fail_after_remote_start(artifacts, **event):
            if event["event_code"] != "RUN_CREATED":
                raise OSError("candidate@example.com?token=private")
            return original_append(artifacts, **event)

        with patch.object(DiscoveryArtifacts, "append_audit", fail_after_remote_start):
            started = orchestrator.start(discovery_request(self.candidate_id))

        persisted = self.repository.get_discovery_run(started["id"])
        self.assertEqual(started["status"], "COLLECTING")
        self.assertEqual(persisted["status"], "COLLECTING")
        self.assertEqual(set(persisted["source_checkpoints"]), {"boss", "nowcoder"})
        self.assertEqual(
            persisted["source_statuses"],
            {"boss": "collecting", "nowcoder": "collecting"},
        )
        audit = (Path(started["artifact_dir"]) / "audit.md").read_text("utf-8")
        self.assertNotIn("candidate@example.com", audit)
        self.assertEqual((boss.start_calls, nowcoder.start_calls), (1, 1))

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

    def test_repeated_partial_poll_is_idempotent_and_does_not_repoll(self):
        boss = failed("boss")
        nowcoder = completed(
            "nowcoder", [job("nowcoder", 1), job("nowcoder", 2), job("nowcoder", 3)]
        )
        orchestrator = self.orchestrator({"boss": boss, "nowcoder": nowcoder})
        started = orchestrator.start(discovery_request(self.candidate_id))
        first = orchestrator.poll(started["id"])

        repeated = orchestrator.poll(started["id"])

        self.assertEqual(first["status"], "PARTIAL")
        self.assertEqual(repeated["status"], "PARTIAL")
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

    def test_imported_runs_are_not_pollable(self):
        boss = completed("boss", [job("boss", 1)])
        nowcoder = completed("nowcoder", [job("nowcoder", 2), job("nowcoder", 3)])
        orchestrator, started, ready = self.start_and_poll(
            {"boss": boss, "nowcoder": nowcoder}
        )
        imported = self.repository.transition_discovery_run(
            ready["id"], {"READY_FOR_IMPORT"}, "IMPORTED"
        )
        self.assertEqual(imported["status"], "IMPORTED")

        with self.assertRaisesRegex(ValueError, "not pollable"):
            orchestrator.poll(started["id"])
        self.assertEqual((boss.poll_calls, nowcoder.poll_calls), (1, 1))

    def test_two_orchestrators_serialize_poll_publication_across_repositories(self):
        gate = SharedPollGate()
        first_source = BlockingPollSource(
            "boss", [job("boss", 1), job("boss", 2), job("boss", 3)], gate
        )
        second_source = BlockingPollSource(
            "boss", [job("boss", 10), job("boss", 11), job("boss", 12)], gate
        )
        first = self.orchestrator({"boss": first_source})
        second_repository = Repository(self.repository.database_path)
        second = self.orchestrator(
            {"boss": second_source}, repository=second_repository
        )
        started = first.start(
            discovery_request(self.candidate_id, sources=["boss"], keywords=["AI Agent"])
        )
        start_barrier = threading.Barrier(3)
        outcomes = []
        outcomes_condition = threading.Condition()

        def poll(label, orchestrator):
            start_barrier.wait(timeout=5)
            try:
                value = orchestrator.poll(started["id"])
            except Exception as error:
                value = error
            with outcomes_condition:
                outcomes.append((label, value))
                outcomes_condition.notify_all()

        threads = [
            threading.Thread(target=poll, args=("first", first), daemon=True),
            threading.Thread(target=poll, args=("second", second), daemon=True),
        ]
        for thread in threads:
            thread.start()
        start_barrier.wait(timeout=5)
        with gate.condition:
            self.assertTrue(
                gate.condition.wait_for(lambda: gate.entered >= 1, timeout=5)
            )
        with outcomes_condition:
            outcomes_condition.wait_for(lambda: len(outcomes) >= 1, timeout=1)
        gate.release.set()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        successes = [(label, value) for label, value in outcomes if isinstance(value, dict)]
        failures = [(label, value) for label, value in outcomes if isinstance(value, Exception)]
        self.assertEqual(gate.max_active, 1)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertRegex(str(failures[0][1]), "busy")
        winner_label, winner = successes[0]
        self.assertEqual(winner["status"], "READY_FOR_IMPORT")
        expected_ids = (
            ["boss-1", "boss-2", "boss-3"]
            if winner_label == "first"
            else ["boss-10", "boss-11", "boss-12"]
        )
        results = json.loads(
            (Path(winner["artifact_dir"]) / "source-results.json").read_text("utf-8")
        )
        self.assertEqual([row["source_job_id"] for row in results], expected_ids)
        self.assertEqual(
            second_repository.get_discovery_run(started["id"])["status"],
            "READY_FOR_IMPORT",
        )
        self.assertEqual(first_source.start_calls + second_source.start_calls, 1)
        self.assertEqual(first_source.poll_calls + second_source.poll_calls, 1)

        loser = second if winner_label == "first" else first
        repeated = loser.poll(started["id"])
        self.assertEqual(repeated["status"], "READY_FOR_IMPORT")
        self.assertEqual(first_source.poll_calls + second_source.poll_calls, 1)

    def test_poll_publication_exception_releases_lease(self):
        boss = completed("boss", [job("boss", 1)])
        orchestrator = self.orchestrator({"boss": boss})
        started = orchestrator.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )
        with patch.object(
            DiscoveryArtifacts,
            "write_results",
            side_effect=OSError("private artifact failure"),
        ):
            with self.assertRaisesRegex(ValueError, "results") as raised:
                orchestrator.poll(started["id"])
        self.assertNotIn("private artifact", str(raised.exception))

        contender = Repository(self.repository.database_path)
        self.assertTrue(
            contender.claim_discovery_poll(
                started["id"],
                "recovery-owner",
                "2026-08-27T00:10:00+00:00",
                "2026-08-27T00:00:00+00:00",
            )
        )
        self.assertTrue(
            contender.release_discovery_poll(started["id"], "recovery-owner")
        )

    def test_expired_poll_lease_reconciles_pre_cas_results(self):
        jobs = [job("boss", 1), job("boss", 2), job("boss", 3)]
        boss = completed("boss", jobs)
        orchestrator = self.orchestrator({"boss": boss})
        started = orchestrator.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )
        self.assertTrue(
            self.repository.claim_discovery_poll(
                started["id"],
                "crashed-owner",
                "2000-01-01T00:01:00+00:00",
                "2000-01-01T00:00:00+00:00",
            )
        )
        DiscoveryArtifacts.open(self.runs_root, started["id"]).write_results(jobs)

        recovered = orchestrator.poll(started["id"])

        results = json.loads(
            (Path(recovered["artifact_dir"]) / "source-results.json").read_text("utf-8")
        )
        self.assertEqual(recovered["status"], "READY_FOR_IMPORT")
        self.assertEqual(len(results), 3)
        self.assertEqual(boss.poll_calls, 1)
        self.assertFalse(
            self.repository.release_discovery_poll(started["id"], "crashed-owner")
        )

    def test_poll_owner_token_never_leaves_internal_database_columns(self):
        owner_token = "private-poll-owner-token"
        boss = completed("boss", [job("boss", 1), job("boss", 2), job("boss", 3)])
        orchestrator = self.orchestrator({"boss": boss})
        started = orchestrator.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )

        with patch(
            "resume_agent.discovery_orchestrator.secrets.token_hex",
            return_value=owner_token,
        ):
            polled = orchestrator.poll(started["id"])

        artifact_text = "\n".join(
            path.read_text("utf-8")
            for path in Path(polled["artifact_dir"]).iterdir()
            if path.is_file()
        )
        self.assertNotIn(owner_token, artifact_text)
        self.assertNotIn(owner_token, json.dumps(orchestrator.get(started["id"])))
        self.assertNotIn("poll_owner_token", polled)
        self.assertNotIn("poll_lease_expires_at", polled)

    def test_expired_lease_cannot_be_stolen_during_fenced_publication(self):
        publication_entered = threading.Event()
        release_publication = threading.Event()
        second_claim_attempted = threading.Event()
        writes = []
        writes_lock = threading.Lock()
        original_write = DiscoveryArtifacts.write_results

        def blocking_write(artifacts, jobs):
            ids = [item.source_job_id for item in jobs]
            with writes_lock:
                writes.append(ids)
            if threading.current_thread().name == "discovery-owner-a":
                publication_entered.set()
                if not release_publication.wait(timeout=5):
                    raise AssertionError("publication was not released")
            return original_write(artifacts, jobs)

        class NotifyingRepository(Repository):
            def claim_discovery_poll(self, *args, **kwargs):
                second_claim_attempted.set()
                return super().claim_discovery_poll(*args, **kwargs)

        first_jobs = [job("boss", 1), job("boss", 2), job("boss", 3)]
        second_jobs = [job("boss", 10), job("boss", 11), job("boss", 12)]
        first_source = completed("boss", first_jobs)
        second_source = completed("boss", second_jobs)
        first = self.orchestrator(
            {"boss": first_source}, lease_duration_seconds=1
        )
        second_repository = NotifyingRepository(self.repository.database_path)
        second = self.orchestrator(
            {"boss": second_source},
            repository=second_repository,
            lease_duration_seconds=1,
        )
        started = first.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )
        first._lease_expiration = lambda now: "2000-01-01T00:00:00+00:00"
        outcomes = []
        outcome_lock = threading.Lock()

        def run_poll(label, orchestrator):
            try:
                value = orchestrator.poll(started["id"])
            except Exception as error:
                value = error
            with outcome_lock:
                outcomes.append((label, value))

        with patch.object(DiscoveryArtifacts, "write_results", blocking_write):
            owner_a = threading.Thread(
                target=run_poll,
                args=("first", first),
                name="discovery-owner-a",
                daemon=True,
            )
            owner_a.start()
            self.assertTrue(publication_entered.wait(timeout=5))
            owner_b = threading.Thread(
                target=run_poll,
                args=("second", second),
                name="discovery-owner-b",
                daemon=True,
            )
            owner_b.start()
            self.assertTrue(second_claim_attempted.wait(timeout=5))
            release_publication.set()
            owner_a.join(timeout=5)
            owner_b.join(timeout=5)
            self.assertFalse(owner_a.is_alive())
            self.assertFalse(owner_b.is_alive())

        final_run = self.repository.get_discovery_run(started["id"])
        results = json.loads(
            (Path(final_run["artifact_dir"]) / "source-results.json").read_text(
                "utf-8"
            )
        )
        self.assertEqual(final_run["status"], "READY_FOR_IMPORT")
        self.assertEqual(
            [row["source_job_id"] for row in results],
            ["boss-1", "boss-2", "boss-3"],
        )
        self.assertEqual(writes, [["boss-1", "boss-2", "boss-3"]])
        self.assertEqual(first_source.poll_calls, 1)
        self.assertEqual(second_source.poll_calls, 0)
        self.assertTrue(all(isinstance(value, dict) for _, value in outcomes))

    def test_published_artifact_survives_db_update_rollback_and_reconciles(self):
        jobs = [job("boss", 1), job("boss", 2), job("boss", 3)]
        boss = completed("boss", jobs)
        orchestrator = self.orchestrator({"boss": boss})
        started = orchestrator.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )
        owner = "crashing-publication-owner"
        self.assertTrue(
            self.repository.claim_discovery_poll(
                started["id"],
                owner,
                "2000-01-01T00:01:00+00:00",
                "2000-01-01T00:00:00+00:00",
            )
        )
        checkpoint = started["source_checkpoints"]["boss"]
        artifacts = DiscoveryArtifacts.open(self.runs_root, started["id"])
        with self.repository._connect() as connection:
            connection.execute(
                f"""
                CREATE TRIGGER fail_discovery_publication
                BEFORE UPDATE OF status ON discovery_runs
                WHEN NEW.id = '{started["id"]}'
                BEGIN
                    SELECT RAISE(ABORT, 'private commit failure');
                END;
                """
            )

        with self.assertRaisesRegex(ValueError, "publication") as raised:
            self.repository.publish_discovery_transition(
                started["id"],
                "COLLECTING",
                "READY_FOR_IMPORT",
                expected_poll_owner=owner,
                source_checkpoints={"boss": checkpoint},
                source_statuses={"boss": "completed"},
                error_code=None,
                publish_results=lambda: artifacts.write_results(jobs),
            )
        self.assertNotIn("private commit failure", str(raised.exception))
        self.assertEqual(
            self.repository.get_discovery_run(started["id"])["status"], "COLLECTING"
        )
        self.assertEqual(
            len(json.loads(artifacts.path("source-results.json").read_text("utf-8"))),
            3,
        )
        with self.repository._connect() as connection:
            connection.execute("DROP TRIGGER fail_discovery_publication")

        recovered = orchestrator.poll(started["id"])
        self.assertEqual(recovered["status"], "READY_FOR_IMPORT")
        self.assertEqual(
            len(json.loads(artifacts.path("source-results.json").read_text("utf-8"))),
            3,
        )

    def test_release_failure_never_masks_poll_success_or_primary_error(self):
        success_source = completed(
            "boss", [job("boss", 1), job("boss", 2), job("boss", 3)]
        )
        success = self.orchestrator({"boss": success_source})
        success_run = success.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )
        with patch.object(
            self.repository,
            "release_discovery_poll",
            side_effect=sqlite3.OperationalError("private release failure"),
        ):
            result = success.poll(success_run["id"])
        self.assertEqual(result["status"], "READY_FOR_IMPORT")

        failure_source = completed("boss", [job("boss", 4)])
        failure = self.orchestrator({"boss": failure_source})
        failure_run = failure.start(
            discovery_request(self.candidate_id, sources=["boss"])
        )
        with (
            patch.object(
                DiscoveryArtifacts,
                "write_results",
                side_effect=OSError("primary private failure"),
            ),
            patch.object(
                self.repository,
                "release_discovery_poll",
                side_effect=sqlite3.OperationalError("secondary private failure"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "results") as primary:
                failure.poll(failure_run["id"])
        self.assertNotIn("secondary private failure", str(primary.exception))

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
