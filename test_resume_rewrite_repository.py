import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from resume_agent.repository import Repository
from resume_agent.rewrite_schemas import FactAnswer, RewriteRequest


class RewriteRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp_dir.name) / "test.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_request_rejects_unsafe_output_name(self):
        with self.assertRaises(ValidationError):
            RewriteRequest(
                candidate_id="candidate_1",
                job_ids=["job_1"],
                source_resume_ids=["source_resume_1"],
                output_name="..\\stolen.pdf",
            )

    def test_request_rejects_reserved_windows_name_with_extension(self):
        with self.assertRaises(ValidationError):
            RewriteRequest(
                candidate_id="candidate_1",
                job_ids=["job_1"],
                source_resume_ids=["source_resume_1"],
                output_name="COM1.txt",
            )

    def test_request_rejects_empty_job_ids(self):
        with self.assertRaises(ValidationError):
            RewriteRequest(
                candidate_id="candidate_1",
                job_ids=[],
                source_resume_ids=["source_resume_1"],
                output_name="resume.pdf",
            )

    def test_rewrite_run_round_trips_json_fields_and_transitions(self):
        candidate = self.repository.create_candidate("Candidate", ["AI Agent"], "")
        request = RewriteRequest(
            candidate_id=candidate["id"],
            job_ids=["job_1"],
            source_resume_ids=["source_resume_1"],
            output_name="Candidate AI-Agent Resume.pdf",
            fact_answers=[
                FactAnswer(
                    question_id="metric-86-success",
                    answer="86% from 86 successful runs in a fixed 100-run regression set.",
                    source_note="Local regression record, confirmed by candidate.",
                )
            ],
        )
        run = self.repository.create_rewrite_run(
            request.model_dump(), "analysis/runs/rewrite_run_1"
        )
        self.assertEqual(run["status"], "QUEUED")
        self.assertEqual(run["fact_answers"][0]["question_id"], "metric-86-success")
        self.assertEqual(run["blocking_questions"], [])

        running = self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True
        )
        self.assertEqual(running["attempt_count"], 1)
        with self.assertRaises(ValueError):
            self.repository.transition_rewrite_run(run["id"], {"QUEUED"}, "READY")

    def test_recovery_lists_expired_lease_and_conditionally_clears_owner(self):
        candidate = self.repository.create_candidate("Candidate", ["AI Agent"], "")
        request = RewriteRequest(
            candidate_id=candidate["id"],
            job_ids=["job_1"],
            source_resume_ids=["source_resume_1"],
            output_name="resume.pdf",
        )
        run = self.repository.create_rewrite_run(
            request.model_dump(), "analysis/runs/stale"
        )
        self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True,
            owner_token="session-a",
            lease_expires_at="2000-01-01T00:00:00+00:00",
        )
        recoverable = self.repository.list_recoverable_rewrite_runs(
            now="2026-08-26T00:00:00+00:00",
            legacy_stale_before="2026-08-25T00:00:00+00:00",
        )
        self.assertEqual([item["id"] for item in recoverable], [run["id"]])
        self.assertFalse(
            self.repository.recover_interrupted_rewrite_run(
                run["id"],
                expected_owner_token="different-session",
                expected_lease_expires_at="2000-01-01T00:00:00+00:00",
            )
        )
        self.assertTrue(
            self.repository.recover_interrupted_rewrite_run(
                run["id"],
                expected_owner_token="session-a",
                expected_lease_expires_at="2000-01-01T00:00:00+00:00",
            )
        )
        recovered = self.repository.get_rewrite_run(run["id"])
        self.assertEqual((recovered["status"], recovered["error_code"]), ("FAILED", "INTERRUPTED"))
        self.assertIsNone(recovered["owner_token"])
        self.assertIsNone(recovered["lease_expires_at"])


if __name__ == "__main__":
    unittest.main()
