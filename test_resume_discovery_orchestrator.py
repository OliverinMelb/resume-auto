import json
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from resume_agent.discovery_artifacts import DiscoveryArtifacts
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
