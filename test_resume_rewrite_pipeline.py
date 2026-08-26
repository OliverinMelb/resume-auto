import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from resume_agent.codex_runner import CodexRunError
from resume_agent.repository import Repository
from resume_agent.rewrite_artifacts import RunArtifacts
from resume_agent.rewrite_factory import create_rewrite_pipeline
from resume_agent.rewrite_pipeline import ResumeRewritePipeline
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer, RewriteRequest
from resume_agent.resume_renderer import RenderError, RenderResult
from resume_agent.settings import ResumeAgentSettings


class FakeCodexRunner:
    def __init__(self, result):
        self.result = result
        self.run_count = 0

    def available(self):
        return True

    def run(self, task, schema_path, output_path, project_root):
        self.run_count += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TempOutputFailureRunner(FakeCodexRunner):
    def run(self, task, schema_path, output_path, project_root):
        output_path.write_text('{"private": "partial output"}', encoding="utf-8")
        raise CodexRunError("CODEX_PROCESS_ERROR", "private runner detail")


class BlockingCodexRunner(FakeCodexRunner):
    def __init__(self, result):
        super().__init__(result)
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, task, schema_path, output_path, project_root):
        self.run_count += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release blocking Codex runner")
        return self.result


class InsertionFailingRepository(Repository):
    def create_rewrite_run_with_id(self, run_id, payload, artifact_dir):
        raise sqlite3.IntegrityError("private database detail")


class FakeRenderer:
    def __init__(self, error=None, page_count=1, write_before_error=False):
        self.error = error
        self.page_count = page_count
        self.write_before_error = write_before_error

    def available(self):
        return True

    def render(self, markdown, pdf_path, preview_dir):
        if self.write_before_error:
            pdf_path.write_bytes(b"%PDF-partial")
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / "page-1.png").write_bytes(b"partial preview")
        if self.error:
            raise self.error
        pdf_path.write_bytes(b"%PDF-fake")
        return RenderResult(self.page_count, markdown, [])


class PublishAfterRecoveryRenderer(FakeRenderer):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def render(self, markdown, pdf_path, preview_dir):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release blocking renderer")
        pdf_path.write_bytes(b"%PDF-late")
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / "page-1.png").write_bytes(b"late-preview")
        return RenderResult(1, markdown, [preview_dir / "page-1.png"])


def result_for(resume_id, job_id):
    claim = "Built LangGraph agent workflows"
    return CodexRewriteResult.model_validate({
        "verdict": "match", "scorecard": [], "risks": [],
        "missing_information": [], "rewritten_resume_markdown": f"- {claim}",
        "rewrite_reasons": [], "depth_recommendations": [], "interviewer_probes": [],
        "claim_audit": [{
            "claim": claim, "source_resume_ids": [resume_id],
            "source_quotes": [claim], "job_ids": [job_id],
            "change_type": "unchanged", "fact_status": "source_verified",
            "measurement_basis": None,
        }],
    })


def metric_result(resume_id, job_id):
    claim = "Completed 86 of 100 fixed regression runs"
    return CodexRewriteResult.model_validate({
        "verdict": "match", "scorecard": [], "risks": [],
        "missing_information": [], "rewritten_resume_markdown": f"- {claim}",
        "rewrite_reasons": [], "depth_recommendations": [], "interviewer_probes": [],
        "claim_audit": [{
            "claim": claim, "source_resume_ids": [resume_id],
            "source_quotes": [claim], "job_ids": [job_id],
            "change_type": "unchanged", "fact_status": "user_confirmed",
            "measurement_basis": None,
        }],
    })


class RewritePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.repository = Repository(root / "test.db")
        self.runs_root = root / "analysis" / "runs"
        self.candidate = self.repository.create_candidate("Candidate", ["AI Agent"], "")
        self.resume = self.repository.create_source_resume(
            self.candidate["id"], "Agent", "Candidate\nBuilt LangGraph agent workflows\nCompleted 86 of 100 fixed regression runs"
        )
        self.job = self.repository.create_job({
            "candidate_id": self.candidate["id"], "source": "manual", "title": "AI Agent Engineer",
            "detail": "Build agent workflows", "company": "Example",
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, **changes):
        values = {"candidate_id": self.candidate["id"], "job_ids": [self.job["id"]],
                  "source_resume_ids": [self.resume["id"]], "output_name": "Candidate Resume.pdf"}
        values.update(changes)
        return RewriteRequest(**values)

    def make_pipeline(self, runner, renderer):
        return ResumeRewritePipeline(self.repository, self.runs_root,
                                     Path("resume_agent/codex_resume_result.schema.json"), Path.cwd(),
                                     runner, renderer)

    def test_ready_run_writes_all_official_artifacts(self):
        run = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer()).run(self.request())
        self.assertEqual(run["status"], "READY")
        for name in ["input-snapshot.json", "jd-evidence.md", "codex-task.md", "result.json", "audit.md", "resume.md", "resume.pdf"]:
            self.assertTrue((Path(run["artifact_dir"]) / name).exists(), name)
        self.assertFalse((Path(run["artifact_dir"]) / ".codex-output.tmp").exists())
        self.assertEqual(run["attempt_count"], 1)

    def test_second_factory_preserves_live_lease_and_recovers_only_expired_run(self):
        expired_id = "rewrite_run_" + "a" * 32
        expired_artifacts = RunArtifacts.create(self.runs_root, expired_id)
        expired = self.repository.create_rewrite_run_with_id(
            expired_id, self.request().model_dump(), str(expired_artifacts.directory)
        )
        self.repository.transition_rewrite_run(
            expired["id"], {"QUEUED"}, "RUNNING", increment_attempt=True
        )
        expired_artifacts.path("resume.pdf").write_bytes(b"%PDF-stale")
        preview = expired_artifacts.directory / ".preview"
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"stale-preview")
        connection = sqlite3.connect(self.repository.database_path)
        try:
            connection.execute(
                "UPDATE rewrite_runs SET updated_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", expired_id),
            )
            connection.commit()
        finally:
            connection.close()

        runner = BlockingCodexRunner(result_for(self.resume["id"], self.job["id"]))
        first_pipeline = self.make_pipeline(runner, FakeRenderer())
        errors = []
        worker = threading.Thread(
            target=lambda: self._capture_pipeline_run(first_pipeline, errors),
            daemon=True,
        )
        worker.start()
        self.assertTrue(runner.started.wait(timeout=2))
        connection = sqlite3.connect(self.repository.database_path)
        try:
            live_id = connection.execute(
                "SELECT id FROM rewrite_runs WHERE id != ? AND status = 'RUNNING'",
                (expired_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        live = self.repository.get_rewrite_run(live_id)

        second_repository = Repository(self.repository.database_path)
        settings = ResumeAgentSettings(
            database_path=self.repository.database_path,
            legacy_action_log_path=Path(self.temp_dir.name) / "actions.jsonl",
            legacy_decision_log_path=Path(self.temp_dir.name) / "decisions.jsonl",
            allow_remote=False,
            runs_root=self.runs_root,
            codex_timeout_seconds=30,
        )
        try:
            create_rewrite_pipeline(second_repository, settings)
            live_after_startup = second_repository.get_rewrite_run(live["id"])
            expired_after_startup = second_repository.get_rewrite_run(expired_id)
        finally:
            runner.release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "blocking pipeline thread did not stop")

        self.assertEqual(live_after_startup["status"], "RUNNING")
        self.assertEqual(expired_after_startup["status"], "FAILED")
        self.assertEqual(expired_after_startup["error_code"], "INTERRUPTED")
        self.assertFalse(expired_artifacts.path("resume.pdf").exists())
        self.assertFalse(preview.exists())
        recovery_events = [
            event for event in self._audit_events(expired_after_startup)
            if event["event_code"] == "RUN_RECOVERED"
        ]
        self.assertEqual(len(recovery_events), 1)
        self.assertTrue(recovery_events[0]["timestamp_utc"])

    def test_recovery_racing_with_render_cannot_leave_failed_run_with_pdf(self):
        renderer = PublishAfterRecoveryRenderer()
        pipeline = self.make_pipeline(
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), renderer
        )
        outcomes = []
        worker = threading.Thread(
            target=lambda: self._capture_pipeline_run(pipeline, outcomes), daemon=True
        )
        worker.start()
        self.assertTrue(renderer.started.wait(timeout=2))
        running = self._latest_run()
        connection = sqlite3.connect(self.repository.database_path)
        try:
            connection.execute(
                "UPDATE rewrite_runs SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", running["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        recovering = self.make_pipeline(FakeCodexRunner({}), FakeRenderer())
        try:
            self.assertEqual(recovering.recover_interrupted_runs(), 1)
        finally:
            renderer.release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "rendering pipeline thread did not stop")
        final = self.repository.get_rewrite_run(running["id"])
        self.assertEqual((final["status"], final["error_code"]), ("FAILED", "INTERRUPTED"))
        self._assert_no_deployable_artifacts(final)
        self.assertFalse((Path(final["artifact_dir"]) / ".preview").exists())

    def test_recovery_does_not_fail_run_with_unvalidated_artifact_directory(self):
        run_id = "rewrite_run_" + "b" * 32
        canonical = RunArtifacts.create(self.runs_root, run_id)
        run = self.repository.create_rewrite_run_with_id(
            run_id, self.request().model_dump(), str(canonical.directory)
        )
        self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True,
            owner_token="stale-owner",
            lease_expires_at="2000-01-01T00:00:00+00:00",
        )
        forged = Path(self.temp_dir.name) / "forged-recovery"
        forged.mkdir()
        sentinel = forged / "resume.pdf"
        sentinel.write_bytes(b"do-not-touch")
        connection = sqlite3.connect(self.repository.database_path)
        try:
            connection.execute(
                "UPDATE rewrite_runs SET artifact_dir = ? WHERE id = ?",
                (str(forged), run_id),
            )
            connection.commit()
        finally:
            connection.close()

        recovered = self.make_pipeline(FakeCodexRunner({}), FakeRenderer())
        self.assertEqual(recovered.recover_interrupted_runs(), 0)
        self.assertEqual(self.repository.get_rewrite_run(run_id)["status"], "RUNNING")
        self.assertEqual(sentinel.read_bytes(), b"do-not-touch")
        self.assertEqual(canonical.path("audit.md").read_text("utf-8"), "")

    def _capture_pipeline_run(self, pipeline, errors):
        try:
            pipeline.run(self.request())
        except Exception as error:  # pragma: no cover - asserted through state
            errors.append(error)

    def test_metric_gap_needs_facts_and_resume_continues_same_run(self):
        pipeline = self.make_pipeline(FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])), FakeRenderer())
        first = pipeline.run(self.request())
        self.assertEqual(first["status"], "NEEDS_FACTS")
        self.assertFalse((Path(first["artifact_dir"]) / "resume.pdf").exists())
        resumed = pipeline.resume(first["id"], [FactAnswer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")])
        self.assertEqual(resumed["id"], first["id"])
        self.assertEqual(resumed["status"], "READY")
        self.assertEqual(resumed["attempt_count"], 2)

    def test_needs_facts_resume_snapshot_and_audit_are_reconstructable(self):
        pipeline = self.make_pipeline(
            FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])),
            FakeRenderer(),
        )
        first = pipeline.run(self.request())
        artifact_dir = Path(first["artifact_dir"])
        first_snapshot = json.loads(
            (artifact_dir / "input-snapshot.json").read_text("utf-8")
        )
        self.assertIn("execution_version", first_snapshot)
        self.assertEqual(
            first_snapshot["execution_version"], "resume-rewrite-pipeline/v1"
        )
        self.assertRegex(first_snapshot["schema_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(first_snapshot["fact_answers"], [])
        self.assertEqual(first_snapshot["attempt_context"]["attempt_count"], 1)

        first_events = self._audit_events(first)
        self.assertEqual(
            [event["event_code"] for event in first_events],
            ["RUN_CREATED", "RUN_STARTED", "MODEL_COMPLETED", "FACTS_REQUIRED"],
        )
        self.assertTrue(all(event.get("timestamp_utc") for event in first_events))
        facts_required = first_events[-1]
        self.assertEqual(facts_required["gate_outcome"], "BLOCKED")
        self.assertEqual(facts_required["claim_count"], 1)
        self.assertEqual(
            facts_required["source_ids"], [self.resume["id"], self.job["id"]]
        )

        answer = FactAnswer(
            question_id=first["blocking_questions"][0]["question_id"],
            answer="Completed 86 of 100 fixed regression runs; 86/100.",
            source_note="本地回归记录，候选人确认",
        )
        resumed = pipeline.resume(first["id"], [answer])
        resumed_snapshot = json.loads(
            (artifact_dir / "input-snapshot.json").read_text("utf-8")
        )
        self.assertEqual(resumed_snapshot["fact_answers"], [answer.model_dump()])
        self.assertEqual(resumed_snapshot["attempt_context"]["attempt_count"], 2)
        self.assertEqual(
            [event["event_code"] for event in self._audit_events(resumed)],
            [
                "RUN_CREATED", "RUN_STARTED", "MODEL_COMPLETED", "FACTS_REQUIRED",
                "FACTS_ACCEPTED", "RUN_STARTED", "MODEL_COMPLETED",
                "RENDER_COMPLETED", "RUN_READY",
            ],
        )

    def test_rejects_resume_owned_by_another_candidate_before_running(self):
        other = self.repository.create_candidate("Other", [], "")
        other_resume = self.repository.create_source_resume(other["id"], "Other", "unrelated")
        with self.assertRaises(ValueError):
            self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer()).run(self.request(source_resume_ids=[other_resume["id"]]))
        connection = sqlite3.connect(self.repository.database_path)
        try:
            status, error_code = connection.execute(
                "SELECT status, error_code FROM rewrite_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((status, error_code), ("FAILED", "VALIDATION_ERROR"))

    def test_unknown_candidate_creates_no_run_or_artifact_directory(self):
        request = self.request(candidate_id="candidate_" + "f" * 32)
        with self.assertRaises(ValueError):
            self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer()).run(request)
        connection = sqlite3.connect(self.repository.database_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM rewrite_runs").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)
        self.assertFalse(self.runs_root.exists())

    def test_unknown_resume_marks_preallocated_run_failed_without_attempt(self):
        unknown_resume_id = "source_resume_" + "e" * 32
        with self.assertRaises(ValueError):
            self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer()).run(self.request(source_resume_ids=[unknown_resume_id]))
        run = self._latest_run()
        self.assertEqual((run["status"], run["error_code"], run["attempt_count"]), ("FAILED", "VALIDATION_ERROR", 0))
        self._assert_no_deployable_artifacts(run)
        self.assertNotIn(unknown_resume_id, (Path(run["artifact_dir"]) / "audit.md").read_text("utf-8"))

    def test_unknown_job_marks_preallocated_run_failed_without_attempt(self):
        unknown_job_id = "job_" + "d" * 32
        with self.assertRaises(ValueError):
            self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer()).run(self.request(job_ids=[unknown_job_id]))
        run = self._latest_run()
        self.assertEqual((run["status"], run["error_code"], run["attempt_count"]), ("FAILED", "VALIDATION_ERROR", 0))
        self._assert_no_deployable_artifacts(run)
        self.assertNotIn(unknown_job_id, (Path(run["artifact_dir"]) / "audit.md").read_text("utf-8"))

    def test_database_insert_failure_removes_preallocated_artifact_directory(self):
        root = Path(self.temp_dir.name) / "insert-failure"
        repository = InsertionFailingRepository(root / "test.db")
        candidate = repository.create_candidate("Candidate", ["AI Agent"], "")
        resume = repository.create_source_resume(candidate["id"], "Agent", "Built LangGraph agent workflows")
        job = repository.create_job({"candidate_id": candidate["id"], "source": "manual", "title": "AI Agent", "detail": "Build workflows", "company": ""})
        request = RewriteRequest(candidate_id=candidate["id"], job_ids=[job["id"]], source_resume_ids=[resume["id"]], output_name="resume.pdf")
        with self.assertRaises(ValueError):
            ResumeRewritePipeline(repository, root / "analysis" / "runs", Path("resume_agent/codex_resume_result.schema.json"), Path.cwd(), FakeCodexRunner(result_for(resume["id"], job["id"])), FakeRenderer()).run(request)
        runs_root = root / "analysis" / "runs"
        self.assertEqual(list(runs_root.iterdir()), [])

    def test_multi_job_requires_candidate_target_role(self):
        self.repository.get_candidate(self.candidate["id"])
        no_role = self.repository.create_candidate("No role", [], "")
        resume = self.repository.create_source_resume(no_role["id"], "A", "Built LangGraph agent workflows")
        job_one = self.repository.create_job({"candidate_id": no_role["id"], "source": "manual", "title": "One", "detail": "x", "company": ""})
        job_two = self.repository.create_job({"candidate_id": no_role["id"], "source": "manual", "title": "Two", "detail": "x", "company": ""})
        request = RewriteRequest(candidate_id=no_role["id"], job_ids=[job_one["id"], job_two["id"]], source_resume_ids=[resume["id"]], output_name="resume.pdf")
        with self.assertRaises(ValueError):
            self.make_pipeline(FakeCodexRunner(result_for(resume["id"], job_one["id"])), FakeRenderer()).run(request)

    def test_runner_error_fails_without_deployable_artifacts_or_private_error(self):
        failure = CodexRunError("CODEX_PROCESS_ERROR", "private secret text")
        run = self.make_pipeline(FakeCodexRunner(failure), FakeRenderer()).run(self.request())
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["error_code"], "CODEX_PROCESS_ERROR")
        self.assertEqual(run["error_summary"], "")
        self.assertFalse((Path(run["artifact_dir"]) / "resume.pdf").exists())
        self.assertNotIn("private secret text", (Path(run["artifact_dir"]) / "audit.md").read_text("utf-8"))
        events = self._audit_events(run)
        self.assertEqual(
            [event["event_code"] for event in events],
            ["RUN_CREATED", "RUN_STARTED", "RUN_FAILED"],
        )
        self.assertTrue(all(event.get("timestamp_utc") for event in events))

    def test_temporary_runner_output_is_removed_when_runner_raises(self):
        run = self.make_pipeline(TempOutputFailureRunner(None), FakeRenderer()).run(self.request())
        self.assertEqual(run["status"], "FAILED")
        self.assertFalse((Path(run["artifact_dir"]) / ".codex-output.tmp").exists())

    def test_renderer_error_fails_without_deployable_artifacts(self):
        run = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer(RenderError("RENDER_ERROR", "private renderer detail"))).run(self.request())
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["error_code"], "RENDER_ERROR")
        self.assertFalse((Path(run["artifact_dir"]) / "resume.pdf").exists())
        self.assertFalse((Path(run["artifact_dir"]) / "resume.md").exists())

    def test_renderer_partial_pdf_is_removed_when_rendering_fails(self):
        renderer = FakeRenderer(RenderError("RENDER_ERROR", "private detail"), write_before_error=True)
        run = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), renderer).run(self.request())
        self.assertEqual(run["status"], "FAILED")
        self.assertFalse((Path(run["artifact_dir"]) / "resume.pdf").exists())
        self.assertFalse((Path(run["artifact_dir"]) / ".preview").exists())

    def test_invalid_runner_result_fails_without_publishing_result_json(self):
        run = self.make_pipeline(FakeCodexRunner({"not": "a result"}), FakeRenderer()).run(self.request())
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["error_code"], "INVALID_MODEL_RESULT")
        self.assertFalse((Path(run["artifact_dir"]) / "result.json").exists())

    def test_audits_renderer_page_count_and_source_mapping(self):
        run = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer(page_count=2)).run(self.request())
        events = self._audit_events(run)
        self.assertEqual(
            [event["event_code"] for event in events],
            ["RUN_CREATED", "RUN_STARTED", "MODEL_COMPLETED", "RENDER_COMPLETED", "RUN_READY"],
        )
        render_event = next(event for event in events if event["event_code"] == "RENDER_COMPLETED")
        ready_event = next(event for event in events if event["event_code"] == "RUN_READY")
        self.assertEqual(render_event["page_count"], 2)
        self.assertEqual((render_event["gate_outcome"], render_event["claim_count"]), ("PASS", 1))
        self.assertEqual(ready_event["source_ids"], [self.resume["id"], self.job["id"]])

    def test_headline_is_allowed_only_when_audited_from_same_source(self):
        result = CodexRewriteResult.model_validate({
            "verdict": "match", "scorecard": [], "risks": [], "missing_information": [],
            "rewritten_resume_markdown": "# Candidate\n- Built LangGraph agent workflows",
            "rewrite_reasons": [], "depth_recommendations": [], "interviewer_probes": [],
            "claim_audit": [
                {"claim": "Candidate", "source_resume_ids": [self.resume["id"]], "source_quotes": ["Candidate"], "job_ids": [self.job["id"]], "change_type": "unchanged", "fact_status": "source_verified", "measurement_basis": None},
                {"claim": "Built LangGraph agent workflows", "source_resume_ids": [self.resume["id"]], "source_quotes": ["Built LangGraph agent workflows"], "job_ids": [self.job["id"]], "change_type": "unchanged", "fact_status": "source_verified", "measurement_basis": None},
            ],
        })
        run = self.make_pipeline(FakeCodexRunner(result), FakeRenderer()).run(self.request())
        self.assertEqual(run["status"], "READY")

    def test_resume_rejects_ready_run(self):
        pipeline = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer())
        run = pipeline.run(self.request())
        with self.assertRaises(ValueError):
            pipeline.resume(run["id"], [])

    def test_resume_rejects_answers_for_questions_not_currently_blocking(self):
        runner = FakeCodexRunner(metric_result(self.resume["id"], self.job["id"]))
        pipeline = self.make_pipeline(runner, FakeRenderer())
        first = pipeline.run(self.request())
        with self.assertRaises(ValueError):
            pipeline.resume(first["id"], [FactAnswer(question_id="gap-" + "a" * 12, answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")])
        unchanged = pipeline.get(first["id"])
        self.assertEqual((unchanged["status"], unchanged["attempt_count"], unchanged["fact_answers"]), ("NEEDS_FACTS", 1, []))
        self.assertEqual(runner.run_count, 1)

    def test_resume_rejects_duplicate_blocker_answers(self):
        pipeline = self.make_pipeline(FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])), FakeRenderer())
        first = pipeline.run(self.request())
        answer = FactAnswer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")
        with self.assertRaises(ValueError):
            pipeline.resume(first["id"], [answer, answer])
        unchanged = pipeline.get(first["id"])
        self.assertEqual((unchanged["status"], unchanged["attempt_count"], unchanged["fact_answers"]), ("NEEDS_FACTS", 1, []))

    def test_tampered_artifact_dir_fails_resume_without_writing_forged_path(self):
        pipeline = self.make_pipeline(FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])), FakeRenderer())
        first = pipeline.run(self.request())
        forged = Path(self.temp_dir.name) / "forged"
        forged.mkdir()
        sentinel = forged / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        connection = sqlite3.connect(self.repository.database_path)
        try:
            connection.execute("UPDATE rewrite_runs SET artifact_dir = ? WHERE id = ?", (str(forged), first["id"]))
            connection.commit()
        finally:
            connection.close()
        answer = FactAnswer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")
        with self.assertRaises(ValueError):
            pipeline.resume(first["id"], [answer])
        self.assertEqual(sentinel.read_text("utf-8"), "unchanged")
        self.assertEqual(self._latest_run()["status"], "FAILED")

    def test_symlinked_run_directory_fails_resume_without_escaping_runs_root(self):
        pipeline = self.make_pipeline(FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])), FakeRenderer())
        first = pipeline.run(self.request())
        artifact_dir = Path(first["artifact_dir"])
        escaped = Path(self.temp_dir.name) / "escaped-run"
        artifact_dir.rename(escaped)
        artifact_dir.symlink_to(escaped, target_is_directory=True)
        before = (escaped / "audit.md").read_text("utf-8")
        answer = FactAnswer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")
        with self.assertRaises(ValueError):
            pipeline.resume(first["id"], [answer])
        self.assertEqual((escaped / "audit.md").read_text("utf-8"), before)
        self.assertEqual(self._latest_run()["status"], "FAILED")

    def _latest_run(self):
        connection = sqlite3.connect(self.repository.database_path)
        try:
            run_id = connection.execute("SELECT id FROM rewrite_runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        finally:
            connection.close()
        return self.repository.get_rewrite_run(run_id)

    @staticmethod
    def _audit_events(run):
        return [
            json.loads(line)
            for line in (Path(run["artifact_dir"]) / "audit.md")
            .read_text("utf-8")
            .splitlines()
        ]

    def _assert_no_deployable_artifacts(self, run):
        directory = Path(run["artifact_dir"])
        for name in ("resume.md", "resume.pdf"):
            with self.subTest(name=name):
                self.assertFalse((directory / name).exists())

    def test_duplicate_ids_are_processed_once_in_request_order(self):
        pipeline = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer())
        run = pipeline.run(self.request(job_ids=[self.job["id"], self.job["id"]], source_resume_ids=[self.resume["id"], self.resume["id"]]))
        snapshot = json.loads((Path(run["artifact_dir"]) / "input-snapshot.json").read_text("utf-8"))
        self.assertEqual(snapshot["job_ids"], [self.job["id"]])
        self.assertEqual(snapshot["source_resume_ids"], [self.resume["id"]])


if __name__ == "__main__":
    unittest.main()
