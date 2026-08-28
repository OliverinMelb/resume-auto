import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_agent.codex_runner import CodexRunError
from resume_agent.repository import Repository
from resume_agent.rewrite_artifacts import RunArtifacts
from resume_agent.rewrite_factory import create_rewrite_pipeline
from resume_agent.rewrite_pipeline import ResumeRewritePipeline
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer, RewriteRequest
from resume_agent.resume_renderer import RenderError, RenderResult
from resume_agent.settings import ResumeAgentSettings


CONFIRMED_AT = "2026-08-26T01:30:00Z"


def confirmed_answer(**values):
    return FactAnswer(
        user_confirmed=True,
        confirmed_at=CONFIRMED_AT,
        **values,
    )


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


class OfficialArtifactWritingRunner(FakeCodexRunner):
    def run(self, task, schema_path, output_path, project_root):
        official_dir = Path(output_path).parent
        (official_dir / "resume.md").write_text("stale", encoding="utf-8")
        (official_dir / "resume.pdf").write_bytes(b"%PDF-stale")
        preview = official_dir / ".preview"
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "page-1.png").write_bytes(b"stale-preview")
        return super().run(task, schema_path, output_path, project_root)


class OfficialArtifactFailureRunner(FakeCodexRunner):
    def __init__(self, error):
        super().__init__(None)
        self.error = error

    def run(self, task, schema_path, output_path, project_root):
        self.run_count += 1
        official_dir = Path(output_path).parent
        (official_dir / "resume.md").write_text("stale", encoding="utf-8")
        (official_dir / "resume.pdf").write_bytes(b"%PDF-stale")
        preview = official_dir / ".preview"
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "page-1.png").write_bytes(b"stale-preview")
        raise self.error


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


class ReadyTransitionFailingRepository(Repository):
    def transition_rewrite_run(self, run_id, expected_statuses, new_status, **changes):
        if new_status == "READY":
            raise sqlite3.OperationalError("private ready transition failure")
        return super().transition_rewrite_run(
            run_id, expected_statuses, new_status, **changes
        )

    def fenced_rewrite_transition(self, run_id, **changes):
        if changes["new_status"] == "READY":
            raise sqlite3.OperationalError("private ready transition failure")
        return super().fenced_rewrite_transition(run_id, **changes)


class CommitThenReadyErrorRepository(Repository):
    def __init__(self, database_path, error):
        super().__init__(database_path)
        self.error = error

    def transition_rewrite_run(self, run_id, expected_statuses, new_status, **changes):
        updated = super().transition_rewrite_run(
            run_id, expected_statuses, new_status, **changes
        )
        if new_status == "READY":
            raise self.error
        return updated

    def fenced_rewrite_transition(self, run_id, **changes):
        updated = super().fenced_rewrite_transition(run_id, **changes)
        if changes["new_status"] == "READY":
            raise self.error
        return updated


class MutationThenCommitFailureRepository(Repository):
    def __init__(self, database_path, failing_statuses):
        super().__init__(database_path)
        self.failing_statuses = set(failing_statuses)

    def fenced_rewrite_transition(self, run_id, **changes):
        if changes["new_status"] in self.failing_statuses:
            changes["mutate_artifacts"]()
            raise sqlite3.OperationalError("private commit failure")
        return super().fenced_rewrite_transition(run_id, **changes)


class FencedMutationRaceRepository(Repository):
    def __init__(self, database_path):
        super().__init__(database_path)
        self.before_mutation = threading.Event()
        self.release_mutation = threading.Event()
        self._armed = False

    def arm(self):
        self._armed = True

    def _pause_once(self):
        if not self._armed:
            return
        self._armed = False
        self.before_mutation.set()
        if not self.release_mutation.wait(timeout=5):
            raise AssertionError("test did not release fenced mutation")

    def get_rewrite_run(self, run_id):
        current = super().get_rewrite_run(run_id)
        self._pause_once()
        return current

    def fenced_rewrite_transition(self, *args, **kwargs):
        self._pause_once()
        return super().fenced_rewrite_transition(*args, **kwargs)


class ReadyBeforeRecoveryClaimRepository(Repository):
    def __init__(self, database_path, artifacts):
        super().__init__(database_path)
        self.artifacts = artifacts

    def claim_interrupted_rewrite_run(self, run_id, **claim):
        self.transition_rewrite_run(
            run_id,
            {"RUNNING"},
            "READY",
            expected_owner_token=claim["expected_owner_token"],
        )
        self.artifacts.append_audit(
            run_id=run_id,
            status="READY",
            event_code="RUN_READY",
        )
        return super().claim_interrupted_rewrite_run(run_id, **claim)


class FakeRenderer:
    def __init__(self, error=None, page_count=1, write_before_error=False):
        self.error = error
        self.page_count = page_count
        self.write_before_error = write_before_error
        self.call_count = 0
        self.markdown = None
        self.pdf_path = None
        self.preview_dir = None

    def available(self):
        return True

    def render(self, markdown, pdf_path, preview_dir):
        self.call_count += 1
        self.markdown = markdown
        self.pdf_path = Path(pdf_path)
        self.preview_dir = Path(preview_dir)
        if self.write_before_error:
            pdf_path.write_bytes(b"%PDF-partial")
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / "page-1.png").write_bytes(b"partial preview")
        if self.error:
            raise self.error
        pdf_path.write_bytes(b"%PDF-fake")
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_paths = []
        for page in range(1, self.page_count + 1):
            preview_path = preview_dir / f"page-{page}.png"
            preview_path.write_bytes(b"fake-preview")
            preview_paths.append(preview_path)
        return RenderResult(self.page_count, markdown, preview_paths)


class DerivedOfficialPathInterruptRenderer(FakeRenderer):
    def render(self, markdown, pdf_path, preview_dir):
        stage = Path(pdf_path).parent
        official_dir = stage.parent
        self.official_dir = official_dir
        (official_dir / "resume.md").write_text(markdown, encoding="utf-8")
        (official_dir / "resume.pdf").write_bytes(b"%PDF-derived")
        official_preview = official_dir / ".preview"
        official_preview.mkdir(parents=True, exist_ok=True)
        (official_preview / "page-1.png").write_bytes(b"derived-preview")
        pdf_path.write_bytes(b"%PDF-staged")
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / "page-1.png").write_bytes(b"staged-preview")
        raise self.error


class InvalidPreviewContractRenderer(FakeRenderer):
    def __init__(self, *, missing_declared_preview=False):
        super().__init__()
        self.missing_declared_preview = missing_declared_preview

    def render(self, markdown, pdf_path, preview_dir):
        pdf_path.write_bytes(b"%PDF-fake")
        declared = preview_dir / "page-1.png"
        if self.missing_declared_preview:
            return RenderResult(1, markdown, [declared])
        return RenderResult(1, markdown, [])


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


class BlockingFailureRenderer(FakeRenderer):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def render(self, markdown, pdf_path, preview_dir):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release failing renderer")
        raise RuntimeError("private renderer failure")


class RecreatingStageAfterRecoveryRenderer(PublishAfterRecoveryRenderer):
    def render(self, markdown, pdf_path, preview_dir):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release recreating renderer")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        (pdf_path.parent / "resume.md").write_text(markdown, encoding="utf-8")
        pdf_path.write_bytes(b"%PDF-recreated")
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / "page-1.png"
        preview_path.write_bytes(b"recreated-preview")
        return RenderResult(1, markdown, [preview_path])


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
            "measurement_basis": None, "claim_status": "verified",
        }],
        "gap_acceptance_plans": [],
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
            "measurement_basis": None, "claim_status": "verified",
        }],
        "gap_acceptance_plans": [],
    })


def in_progress_result(resume_id, job_id, *, claim=None, test_commands=None):
    claim = claim or "在建：正在实现LangGraph多Agent路由，计划以回归集验收"
    return CodexRewriteResult.model_validate({
        "verdict": "match", "scorecard": [], "risks": [],
        "missing_information": [],
        "rewritten_resume_markdown": f"## 在建项目（待验收）\n- {claim}",
        "rewrite_reasons": [], "depth_recommendations": [], "interviewer_probes": [],
        "claim_audit": [{
            "claim": claim, "source_resume_ids": [], "source_quotes": [],
            "job_ids": [job_id], "change_type": "strengthened",
            "fact_status": "unverified", "measurement_basis": None,
            "claim_status": "in_progress", "gap_id": "gap-aaaaaaaaaaaa",
            "acceptance_id": None,
        }],
        "gap_acceptance_plans": [{
            "gap_id": "gap-aaaaaaaaaaaa",
            "jd_requirements": ["LangGraph 多 Agent 路由"],
            "project_scope": "实现本地路由原型并以固定回归集验收",
            "required_artifacts": ["代码仓库", "回归报告"],
            "test_commands": test_commands or ["python -m unittest -v"],
            "acceptance_metrics": ["固定回归集全部通过"],
            "interview_topics": ["路由设计"],
        }],
    })


def accepted_result(resume_id, job_id, acceptance_id, *, claim=None, gap_id=None):
    claim = claim or "实现本地 Agent 评测集"
    gap_id = gap_id or "gap-123456789abc"
    return CodexRewriteResult.model_validate({
        "verdict": "match", "scorecard": [], "risks": [],
        "missing_information": [],
        "rewritten_resume_markdown": f"## 项目经历\n- {claim}",
        "rewrite_reasons": [], "depth_recommendations": [], "interviewer_probes": [],
        "claim_audit": [{
            "claim": claim, "source_resume_ids": [], "source_quotes": [],
            "job_ids": [job_id], "change_type": "strengthened",
            "fact_status": "user_confirmed", "measurement_basis": None,
            "claim_status": "verified", "gap_id": gap_id,
            "acceptance_id": acceptance_id,
        }],
        "gap_acceptance_plans": [],
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
        self.assertTrue(
            (Path(run["artifact_dir"]) / ".preview" / "page-1.png").is_file()
        )
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

    def test_recovered_run_rejects_late_renderer_publication(self):
        renderer = RecreatingStageAfterRecoveryRenderer()
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

        self.assertFalse(worker.is_alive(), "late renderer thread did not stop")
        final = self.repository.get_rewrite_run(running["id"])
        self.assertEqual((final["status"], final["error_code"]), ("FAILED", "INTERRUPTED"))
        self._assert_no_deployable_artifacts(final)
        self.assertEqual(list(Path(final["artifact_dir"]).glob(".render-stage-*")), [])

    def test_ready_winner_before_recovery_claim_keeps_artifacts_and_audit(self):
        run_id = "rewrite_run_" + "c" * 32
        artifacts = RunArtifacts.create(self.runs_root, run_id)
        run = self.repository.create_rewrite_run_with_id(
            run_id, self.request().model_dump(), str(artifacts.directory)
        )
        self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True,
            owner_token="finishing-owner",
            lease_expires_at="2000-01-01T00:00:00+00:00",
        )
        artifacts.path("resume.md").write_text("ready resume", "utf-8")
        artifacts.path("resume.pdf").write_bytes(b"%PDF-ready")
        preview = artifacts.directory / ".preview"
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"ready-preview")

        racing_repository = ReadyBeforeRecoveryClaimRepository(
            self.repository.database_path, artifacts
        )
        recovering = ResumeRewritePipeline(
            racing_repository,
            self.runs_root,
            Path("resume_agent/codex_resume_result.schema.json"),
            Path.cwd(),
            FakeCodexRunner({}),
            FakeRenderer(),
        )

        self.assertEqual(recovering.recover_interrupted_runs(), 0)
        final = racing_repository.get_rewrite_run(run_id)
        self.assertEqual(final["status"], "READY")
        self.assertEqual(artifacts.path("resume.md").read_text("utf-8"), "ready resume")
        self.assertEqual(artifacts.path("resume.pdf").read_bytes(), b"%PDF-ready")
        self.assertEqual((preview / "page-1.png").read_bytes(), b"ready-preview")
        event_codes = [event["event_code"] for event in self._audit_events(final)]
        self.assertIn("RUN_READY", event_codes)
        self.assertNotIn("RUN_RECOVERED", event_codes)

    def test_recovery_cleanup_failure_requeues_claim_before_failed_state(self):
        run_id = "rewrite_run_" + "d" * 32
        artifacts = RunArtifacts.create(self.runs_root, run_id)
        run = self.repository.create_rewrite_run_with_id(
            run_id, self.request().model_dump(), str(artifacts.directory)
        )
        expired_lease = "2000-01-01T00:00:00+00:00"
        self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True,
            owner_token="stale-owner", lease_expires_at=expired_lease,
        )
        artifacts.path("resume.pdf").write_bytes(b"%PDF-stale")
        preview = artifacts.directory / ".preview"
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"stale-preview")
        recovering = self.make_pipeline(FakeCodexRunner({}), FakeRenderer())

        with patch(
            "resume_agent.rewrite_pipeline.shutil.rmtree",
            side_effect=OSError("preview is locked"),
        ):
            try:
                recovered_count = recovering.recover_interrupted_runs()
            except OSError as error:
                self.fail(f"cleanup failure escaped recovery: {error}")

        self.assertEqual(recovered_count, 0)
        retryable = self.repository.get_rewrite_run(run_id)
        self.assertEqual(retryable["status"], "RUNNING")
        self.assertNotEqual(retryable["owner_token"], "stale-owner")
        self.assertEqual(retryable["lease_expires_at"], expired_lease)
        self.assertTrue(preview.exists())
        self.assertNotIn(
            "RUN_RECOVERED",
            [event["event_code"] for event in self._audit_events(retryable)],
        )

        self.assertEqual(recovering.recover_interrupted_runs(), 1)
        final = self.repository.get_rewrite_run(run_id)
        self.assertEqual((final["status"], final["error_code"]), ("FAILED", "INTERRUPTED"))
        self._assert_no_deployable_artifacts(final)
        self.assertFalse(preview.exists())

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

    def test_foreign_pipeline_cannot_fail_owned_running_run(self):
        runner = BlockingCodexRunner(result_for(self.resume["id"], self.job["id"]))
        owner = self.make_pipeline(runner, FakeRenderer())
        worker = threading.Thread(
            target=lambda: self._capture_pipeline_run(owner, []), daemon=True
        )
        worker.start()
        self.assertTrue(runner.started.wait(timeout=2))
        running = self._latest_run()
        canonical_dir = running["artifact_dir"]
        forged = Path(self.temp_dir.name) / "foreign-forged"
        forged.mkdir()
        connection = sqlite3.connect(self.repository.database_path)
        try:
            connection.execute(
                "UPDATE rewrite_runs SET artifact_dir = ? WHERE id = ?",
                (str(forged), running["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        foreign = self.make_pipeline(FakeCodexRunner({}), FakeRenderer())
        try:
            with self.assertRaises(ValueError):
                foreign.get(running["id"])
            self.assertEqual(
                self.repository.get_rewrite_run(running["id"])["status"], "RUNNING"
            )
        finally:
            connection = sqlite3.connect(self.repository.database_path)
            try:
                connection.execute(
                    "UPDATE rewrite_runs SET artifact_dir = ? WHERE id = ?",
                    (canonical_dir, running["id"]),
                )
                connection.commit()
            finally:
                connection.close()
            runner.release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "owned pipeline thread did not stop")

    def test_stale_same_owner_generation_cannot_fail_new_recovery_claim(self):
        pipeline = self.make_pipeline(FakeCodexRunner({}), FakeRenderer())
        run_id = "rewrite_run_" + "e" * 32
        artifacts = RunArtifacts.create(self.runs_root, run_id)
        queued = self.repository.create_rewrite_run_with_id(
            run_id, self.request().model_dump(), str(artifacts.directory)
        )
        stale = self.repository.transition_rewrite_run(
            queued["id"], {"QUEUED"}, "RUNNING",
            owner_token=pipeline._owner_token,
            lease_expires_at="2099-01-01T00:00:01+00:00",
        )
        self.repository.transition_rewrite_run(
            queued["id"], {"RUNNING"}, "RUNNING",
            owner_token=pipeline._owner_token,
            lease_expires_at="2099-01-01T00:00:02+00:00",
            expected_owner_token=pipeline._owner_token,
            expected_lease_expires_at=stale["lease_expires_at"],
        )

        with self.assertRaises(ValueError):
            pipeline._fail_artifact_location(stale)

        current = self.repository.get_rewrite_run(run_id)
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(
            current["lease_expires_at"], "2099-01-01T00:00:02+00:00"
        )

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
        resumed = pipeline.resume(first["id"], [confirmed_answer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")])
        self.assertEqual(resumed["id"], first["id"])
        self.assertEqual(resumed["status"], "READY")
        self.assertEqual(resumed["attempt_count"], 2)

    def test_legacy_unconfirmed_fact_cannot_unlock_metric(self):
        pipeline = self.make_pipeline(
            FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])),
            FakeRenderer(),
        )
        first = pipeline.run(self.request())
        resumed = pipeline.resume(first["id"], [FactAnswer(
            question_id=first["blocking_questions"][0]["question_id"],
            answer="Completed 86 of 100 fixed regression runs; 86/100.",
            source_note="本地回归记录，候选人确认",
        )])

        self.assertEqual(resumed["status"], "NEEDS_FACTS")
        self.assertFalse(resumed["fact_answers"][0]["user_confirmed"])
        self.assertIsNone(resumed["fact_answers"][0]["confirmed_at"])
        self.assertFalse((Path(resumed["artifact_dir"]) / "resume.pdf").exists())

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

        answer = confirmed_answer(
            question_id=first["blocking_questions"][0]["question_id"],
            answer="Completed 86 of 100 fixed regression runs; 86/100.",
            source_note="本地回归记录，候选人确认",
        )
        resumed = pipeline.resume(first["id"], [answer])
        resumed_snapshot = json.loads(
            (artifact_dir / "input-snapshot.json").read_text("utf-8")
        )
        self.assertEqual(resumed_snapshot["fact_answers"], [answer.model_dump(mode="json")])
        self.assertTrue(resumed_snapshot["fact_answers"][0]["user_confirmed"])
        self.assertEqual(
            resumed_snapshot["fact_answers"][0]["confirmed_at"], CONFIRMED_AT
        )
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

    def test_obvious_cross_direction_job_titles_block_before_model_and_attempt(self):
        sales_job = self.repository.create_job({
            "candidate_id": self.candidate["id"], "source": "manual",
            "title": "Sales Manager", "detail": "Lead enterprise sales",
            "company": "Example",
        })
        runner = FakeCodexRunner(result_for(self.resume["id"], self.job["id"]))
        pipeline = self.make_pipeline(runner, FakeRenderer())
        run = pipeline.run(self.request(job_ids=[self.job["id"], sales_job["id"]]))

        self.assertEqual((run["status"], run["attempt_count"], runner.run_count), ("NEEDS_FACTS", 0, 0))
        self.assertEqual(len(run["blocking_questions"]), 1)
        blocker_id = run["blocking_questions"][0]["question_id"]
        self.assertRegex(blocker_id, r"^gap-[0-9a-f]{12}$")
        artifact_dir = Path(run["artifact_dir"])
        snapshot = json.loads((artifact_dir / "input-snapshot.json").read_text("utf-8"))
        self.assertEqual(snapshot["preflight_blocker_ids"], [blocker_id])
        for name in ("result.json", "resume.md", "resume.pdf"):
            self.assertFalse((artifact_dir / name).exists(), name)
        self.assertEqual(
            [event["event_code"] for event in self._audit_events(run)],
            ["RUN_CREATED", "FACTS_REQUIRED"],
        )

        unchanged = pipeline.resume(
            run["id"],
            [confirmed_answer(
                question_id=blocker_id,
                answer="Keep both jobs",
                source_note="Candidate requested the same divergent selection",
            )],
        )
        self.assertEqual(
            (unchanged["status"], unchanged["attempt_count"], runner.run_count),
            ("NEEDS_FACTS", 0, 0),
        )
        self.assertEqual(unchanged["fact_answers"], [])

    def test_known_cross_family_job_titles_block_before_model(self):
        for first_title, second_title in (
            ("AI Agent Engineer", "Accountant"),
            ("AI Agent Engineer", "Registered Nurse"),
            ("人工智能工程师", "财务会计"),
            ("大模型开发工程师", "护士"),
            ("Sales Manager", "Accountant"),
            ("审计师", "临床护士"),
        ):
            with self.subTest(first_title=first_title, second_title=second_title):
                first_job = self.repository.create_job({
                    "candidate_id": self.candidate["id"], "source": "manual",
                    "title": first_title, "detail": "Known family role",
                    "company": "Example",
                })
                second_job = self.repository.create_job({
                    "candidate_id": self.candidate["id"], "source": "manual",
                    "title": second_title, "detail": "Different known family role",
                    "company": "Example",
                })
                runner = FakeCodexRunner(
                    result_for(self.resume["id"], first_job["id"])
                )

                run = self.make_pipeline(runner, FakeRenderer()).run(
                    self.request(job_ids=[first_job["id"], second_job["id"]])
                )

                self.assertEqual(
                    (run["status"], run["attempt_count"], runner.run_count),
                    ("NEEDS_FACTS", 0, 0),
                )
                self.assertEqual(len(run["blocking_questions"]), 1)

    def test_coherent_and_unknown_engineering_job_titles_reach_model(self):
        llm_job = self.repository.create_job({
            "candidate_id": self.candidate["id"], "source": "manual",
            "title": "LLM Platform Engineer", "detail": "Build model platform",
            "company": "Example",
        })
        backend_job = self.repository.create_job({
            "candidate_id": self.candidate["id"], "source": "manual",
            "title": "Backend Engineer", "detail": "Build platform services",
            "company": "Example",
        })
        for jobs in (
            [self.job["id"], llm_job["id"]],
            [self.job["id"], backend_job["id"]],
        ):
            with self.subTest(jobs=jobs):
                runner = FakeCodexRunner(result_for(self.resume["id"], self.job["id"]))
                run = self.make_pipeline(runner, FakeRenderer()).run(
                    self.request(job_ids=jobs)
                )
                self.assertEqual((run["status"], runner.run_count), ("READY", 1))

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

    def test_runner_unexpected_exception_fails_safely_and_cleans_official_artifacts(self):
        pipeline = self.make_pipeline(
            OfficialArtifactFailureRunner(RuntimeError("private runner bug")),
            FakeRenderer(),
        )

        try:
            run = pipeline.run(self.request())
        except RuntimeError as error:
            self.fail(f"unexpected runner exception escaped pipeline: {error}")

        self.assertEqual(
            (run["status"], run["error_code"]),
            ("FAILED", "CODEX_PROCESS_ERROR"),
        )
        self._assert_no_deployable_artifacts(run)
        self.assertFalse((Path(run["artifact_dir"]) / ".preview").exists())
        audit_text = (Path(run["artifact_dir"]) / "audit.md").read_text("utf-8")
        self.assertNotIn("private runner bug", audit_text)
        self.assertEqual(self._audit_events(run)[-1]["event_code"], "RUN_FAILED")

    def test_runner_base_exception_cleans_official_artifacts_then_reraises(self):
        for interrupt in (KeyboardInterrupt("stop runner"), SystemExit("stop runner")):
            with self.subTest(interrupt=type(interrupt).__name__):
                pipeline = self.make_pipeline(
                    OfficialArtifactFailureRunner(interrupt), FakeRenderer()
                )

                with self.assertRaises(type(interrupt)) as caught:
                    pipeline.run(self.request())

                self.assertIs(caught.exception, interrupt)
                run = self._latest_run()
                self.assertEqual(
                    (run["status"], run["error_code"]),
                    ("FAILED", "CODEX_PROCESS_ERROR"),
                )
                self._assert_no_deployable_artifacts(run)
                self.assertFalse((Path(run["artifact_dir"]) / ".preview").exists())

    def test_runner_cleanup_failure_keeps_owned_run_running_without_terminal_audit(self):
        pipeline = self.make_pipeline(
            OfficialArtifactFailureRunner(RuntimeError("private runner bug")),
            FakeRenderer(),
        )
        original_unlink = Path.unlink

        def locked_official_pdf(path, missing_ok=False):
            if path.name == "resume.pdf" and path.parent.name.startswith(
                "rewrite_run_"
            ):
                raise OSError("official PDF is locked")
            return original_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", locked_official_pdf):
            try:
                run = pipeline.run(self.request())
            except RuntimeError as error:
                self.fail(f"unexpected runner exception escaped pipeline: {error}")

        self.assertEqual((run["status"], run["error_code"]), ("RUNNING", None))
        self.assertIsNotNone(run["owner_token"])
        self.assertIsNotNone(run["lease_expires_at"])
        self.assertTrue((Path(run["artifact_dir"]) / "resume.pdf").exists())
        self.assertNotIn(
            "RUN_FAILED",
            [event["event_code"] for event in self._audit_events(run)],
        )

    def test_needs_facts_commit_failure_does_not_write_terminal_audit(self):
        repository = MutationThenCommitFailureRepository(
            self.repository.database_path, {"NEEDS_FACTS"}
        )
        pipeline = ResumeRewritePipeline(
            repository,
            self.runs_root,
            Path("resume_agent/codex_resume_result.schema.json"),
            Path.cwd(),
            FakeCodexRunner(
                in_progress_result(
                    self.resume["id"], self.job["id"], claim="为客户上线RAG系统"
                )
            ),
            FakeRenderer(),
        )

        run = pipeline.run(self.request())

        self.assertEqual(run["status"], "RUNNING")
        self.assertNotIn(
            "FACTS_REQUIRED",
            [event["event_code"] for event in self._audit_events(run)],
        )

    def test_failed_commit_failure_does_not_write_terminal_audit(self):
        repository = MutationThenCommitFailureRepository(
            self.repository.database_path, {"FAILED"}
        )
        pipeline = ResumeRewritePipeline(
            repository,
            self.runs_root,
            Path("resume_agent/codex_resume_result.schema.json"),
            Path.cwd(),
            FakeCodexRunner(
                CodexRunError("CODEX_PROCESS_ERROR", "private runner detail")
            ),
            FakeRenderer(),
        )

        run = pipeline.run(self.request())

        self.assertEqual(run["status"], "RUNNING")
        self.assertNotIn(
            "RUN_FAILED",
            [event["event_code"] for event in self._audit_events(run)],
        )

    def test_recovery_commit_failure_does_not_write_terminal_audit(self):
        run_id = "rewrite_run_" + "e" * 32
        artifacts = RunArtifacts.create(self.runs_root, run_id)
        run = self.repository.create_rewrite_run_with_id(
            run_id, self.request().model_dump(), str(artifacts.directory)
        )
        expired_lease = "2000-01-01T00:00:00+00:00"
        self.repository.transition_rewrite_run(
            run_id,
            {"QUEUED"},
            "RUNNING",
            increment_attempt=True,
            owner_token="stale-owner",
            lease_expires_at=expired_lease,
        )
        repository = MutationThenCommitFailureRepository(
            self.repository.database_path, {"FAILED"}
        )
        pipeline = ResumeRewritePipeline(
            repository,
            self.runs_root,
            Path("resume_agent/codex_resume_result.schema.json"),
            Path.cwd(),
            FakeCodexRunner({}),
            FakeRenderer(),
        )

        self.assertEqual(pipeline.recover_interrupted_runs(), 0)

        current = repository.get_rewrite_run(run_id)
        self.assertEqual(current["status"], "RUNNING")
        self.assertNotIn(
            "RUN_RECOVERED",
            [event["event_code"] for event in self._audit_events(current)],
        )

    def test_valid_in_progress_gate_publishes_exact_validated_markdown(self):
        result = in_progress_result(self.resume["id"], self.job["id"])
        renderer = FakeRenderer()

        run = self.make_pipeline(FakeCodexRunner(result), renderer).run(self.request())

        self.assertEqual(run["status"], "READY")
        markdown_path = Path(run["artifact_dir"]) / "resume.md"
        self.assertEqual(markdown_path.read_text("utf-8"), result.rewritten_resume_markdown)
        self.assertEqual(renderer.markdown, result.rewritten_resume_markdown)
        self.assertEqual(renderer.call_count, 1)

    def test_invalid_in_progress_gate_needs_facts_without_deployable_artifacts(self):
        result = in_progress_result(
            self.resume["id"], self.job["id"], claim="为客户上线RAG系统"
        )
        renderer = FakeRenderer()

        run = self.make_pipeline(FakeCodexRunner(result), renderer).run(self.request())

        self.assertEqual(run["status"], "NEEDS_FACTS")
        self.assertTrue(run["blocking_questions"])
        self._assert_no_deployable_artifacts(run)
        self.assertEqual(renderer.call_count, 0)

    def test_gap_plan_test_commands_are_data_and_never_executed(self):
        marker = Path(self.temp_dir.name) / "model-command-was-executed"
        command = f"Set-Content -LiteralPath '{marker}' -Value unsafe"
        result = in_progress_result(
            self.resume["id"], self.job["id"], test_commands=[command]
        )

        run = self.make_pipeline(FakeCodexRunner(result), FakeRenderer()).run(
            self.request()
        )

        self.assertEqual(run["status"], "READY")
        self.assertFalse(marker.exists())

    def test_accepted_gap_enters_snapshot_prompt_and_can_be_promoted(self):
        marker = Path(self.temp_dir.name) / "acceptance-command-was-executed"
        command = f"Set-Content -LiteralPath '{marker}' -Value unsafe"
        accepted = self.repository.create_gap_acceptance(
            self.candidate["id"],
            {
                "gap_id": "gap-123456789abc",
                "project_path": str(Path(self.temp_dir.name) / "missing-project"),
                "verified_claims": ["实现本地 Agent 评测集"],
                "artifact_hashes": {"report.json": "a" * 64},
                "test_records": [{
                    "command": command,
                    "exit_code": 0,
                    "output_sha256": "b" * 64,
                }],
                "completed_at": "2026-08-26T03:00:00Z",
                "user_confirmed": True,
                "confirmed_at": "2026-08-26T03:01:00Z",
            },
        )
        result = accepted_result(
            self.resume["id"], self.job["id"], accepted["id"]
        )

        run = self.make_pipeline(FakeCodexRunner(result), FakeRenderer()).run(
            self.request()
        )

        self.assertEqual(run["status"], "READY")
        artifact_dir = Path(run["artifact_dir"])
        snapshot = json.loads((artifact_dir / "input-snapshot.json").read_text("utf-8"))
        self.assertEqual(snapshot["accepted_gap_ids"], [accepted["id"]])
        self.assertEqual(snapshot["gap_acceptances"], [accepted])
        task = (artifact_dir / "codex-task.md").read_text("utf-8")
        self.assertIn(accepted["id"], task)
        self.assertIn("实现本地 Agent 评测集", task)
        self.assertIn("Set-Content -LiteralPath", task)
        self.assertIn(marker.name, task)
        self.assertIn("不可执行", task)
        self.assertFalse(marker.exists())

    def test_other_candidate_acceptance_is_not_loaded_or_accepted(self):
        other = self.repository.create_candidate("Other", ["AI Agent"], "")
        accepted = self.repository.create_gap_acceptance(
            other["id"],
            {
                "gap_id": "gap-123456789abc",
                "project_path": "C:/work/other-project",
                "verified_claims": ["实现本地 Agent 评测集"],
                "artifact_hashes": {"report.json": "a" * 64},
                "test_records": [{
                    "command": "python -m unittest -v",
                    "exit_code": 0,
                    "output_sha256": "b" * 64,
                }],
                "completed_at": "2026-08-26T03:00:00Z",
                "user_confirmed": True,
                "confirmed_at": "2026-08-26T03:01:00Z",
            },
        )
        result = accepted_result(
            self.resume["id"], self.job["id"], accepted["id"]
        )

        run = self.make_pipeline(FakeCodexRunner(result), FakeRenderer()).run(
            self.request()
        )

        self.assertEqual(run["status"], "FAILED")
        snapshot = json.loads(
            (Path(run["artifact_dir"]) / "input-snapshot.json").read_text("utf-8")
        )
        self.assertEqual(snapshot["accepted_gap_ids"], [])
        self.assertEqual(snapshot["gap_acceptances"], [])
        self._assert_no_deployable_artifacts(run)

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

    def test_renderer_runtime_error_fails_without_staged_or_official_artifacts(self):
        renderer = FakeRenderer(
            RuntimeError("private renderer bug"), write_before_error=True
        )
        pipeline = self.make_pipeline(
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), renderer
        )

        try:
            run = pipeline.run(self.request())
        except RuntimeError as error:
            self.fail(f"ordinary renderer exception escaped pipeline: {error}")

        self.assertEqual((run["status"], run["error_code"]), ("FAILED", "RENDER_ERROR"))
        self._assert_no_deployable_artifacts(run)
        artifact_dir = Path(run["artifact_dir"])
        self.assertFalse((artifact_dir / ".preview").exists())
        self.assertEqual(list(artifact_dir.glob(".render-stage-*")), [])
        self.assertEqual(self._audit_events(run)[-1]["event_code"], "RUN_FAILED")

    def test_renderer_receives_isolated_staging_targets_before_atomic_publish(self):
        renderer = FakeRenderer()
        result = result_for(self.resume["id"], self.job["id"])

        run = self.make_pipeline(FakeCodexRunner(result), renderer).run(self.request())

        self.assertEqual(run["status"], "READY")
        artifact_dir = Path(run["artifact_dir"])
        self.assertNotEqual(renderer.pdf_path, artifact_dir / "resume.pdf")
        self.assertNotEqual(renderer.preview_dir, artifact_dir / ".preview")
        self.assertEqual(renderer.pdf_path.name, "resume.pdf")
        self.assertEqual(renderer.preview_dir.name, "preview")
        self.assertEqual(renderer.pdf_path.parent, renderer.preview_dir.parent)
        self.assertEqual(renderer.pdf_path.parent.parent, artifact_dir)
        self.assertTrue(renderer.pdf_path.parent.name.startswith(".render-stage-"))
        self.assertFalse(renderer.pdf_path.parent.exists())
        self.assertEqual(list(artifact_dir.glob(".render-stage-*")), [])
        self.assertEqual((artifact_dir / "resume.pdf").read_bytes(), b"%PDF-fake")
        self.assertEqual(
            (artifact_dir / "resume.md").read_text("utf-8"),
            result.rewritten_resume_markdown,
        )

    def test_ready_transition_error_fails_and_removes_published_artifacts(self):
        repository = ReadyTransitionFailingRepository(
            self.repository.database_path
        )
        pipeline = ResumeRewritePipeline(
            repository,
            self.runs_root,
            Path("resume_agent/codex_resume_result.schema.json"),
            Path.cwd(),
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])),
            FakeRenderer(),
        )

        run = pipeline.run(self.request())

        self.assertEqual((run["status"], run["error_code"]), ("FAILED", "RENDER_ERROR"))
        self._assert_no_deployable_artifacts(run)
        artifact_dir = Path(run["artifact_dir"])
        self.assertFalse((artifact_dir / ".preview").exists())
        self.assertEqual(list(artifact_dir.glob(".render-stage-*")), [])
        self.assertEqual(self._audit_events(run)[-1]["event_code"], "RUN_FAILED")

    def test_ready_commit_then_ordinary_error_preserves_ready_delivery(self):
        errors = (
            ValueError("private post-commit value error"),
            sqlite3.OperationalError("private post-commit database error"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                repository = CommitThenReadyErrorRepository(
                    self.repository.database_path, error
                )
                pipeline = ResumeRewritePipeline(
                    repository,
                    self.runs_root,
                    Path("resume_agent/codex_resume_result.schema.json"),
                    Path.cwd(),
                    FakeCodexRunner(
                        result_for(self.resume["id"], self.job["id"])
                    ),
                    FakeRenderer(),
                )

                run = pipeline.run(self.request())

                self.assertEqual((run["status"], run["error_code"]), ("READY", None))
                artifact_dir = Path(run["artifact_dir"])
                self.assertTrue((artifact_dir / "resume.md").is_file())
                self.assertTrue((artifact_dir / "resume.pdf").is_file())
                self.assertTrue((artifact_dir / ".preview" / "page-1.png").is_file())
                self.assertNotIn(
                    "RUN_FAILED",
                    [event["event_code"] for event in self._audit_events(run)],
                )

    def test_old_owner_cleanup_cannot_delete_new_ready_delivery(self):
        repository = FencedMutationRaceRepository(self.repository.database_path)
        renderer = BlockingFailureRenderer()
        pipeline = ResumeRewritePipeline(
            repository,
            self.runs_root,
            Path("resume_agent/codex_resume_result.schema.json"),
            Path.cwd(),
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])),
            renderer,
        )
        outcomes = []
        worker = threading.Thread(
            target=lambda: self._capture_pipeline_run(pipeline, outcomes), daemon=True
        )
        worker.start()
        self.assertTrue(renderer.started.wait(timeout=2))
        running = self._latest_run()
        repository.arm()
        renderer.release.set()
        self.assertTrue(repository.before_mutation.wait(timeout=2))

        winner = Repository(self.repository.database_path)
        winner.transition_rewrite_run(
            running["id"],
            {"RUNNING"},
            "RUNNING",
            owner_token="winner-owner",
            lease_expires_at="2099-01-01T00:00:00+00:00",
            expected_owner_token=running["owner_token"],
        )
        artifact_dir = Path(running["artifact_dir"])
        (artifact_dir / "resume.md").write_text("winner resume", encoding="utf-8")
        (artifact_dir / "resume.pdf").write_bytes(b"%PDF-winner")
        preview = artifact_dir / ".preview"
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"winner-preview")
        winner.transition_rewrite_run(
            running["id"],
            {"RUNNING"},
            "READY",
            expected_owner_token="winner-owner",
        )
        repository.release_mutation.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "old owner thread did not stop")
        final = winner.get_rewrite_run(running["id"])
        self.assertEqual((final["status"], final["error_code"]), ("READY", None))
        self.assertEqual((artifact_dir / "resume.md").read_text("utf-8"), "winner resume")
        self.assertEqual((artifact_dir / "resume.pdf").read_bytes(), b"%PDF-winner")
        self.assertEqual((preview / "page-1.png").read_bytes(), b"winner-preview")

    def test_post_ready_audit_interrupt_preserves_delivery_then_reraises(self):
        interrupt = KeyboardInterrupt("post-ready audit interrupted")
        original_append_audit = RunArtifacts.append_audit

        def interrupt_ready_audit(artifacts, **event):
            if event.get("event_code") == "RUN_READY":
                raise interrupt
            return original_append_audit(artifacts, **event)

        pipeline = self.make_pipeline(
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])),
            FakeRenderer(),
        )

        with patch.object(RunArtifacts, "append_audit", interrupt_ready_audit):
            with self.assertRaises(KeyboardInterrupt) as caught:
                pipeline.run(self.request())

        self.assertIs(caught.exception, interrupt)
        run = self._latest_run()
        self.assertEqual((run["status"], run["error_code"]), ("READY", None))
        artifact_dir = Path(run["artifact_dir"])
        self.assertTrue((artifact_dir / "resume.md").is_file())
        self.assertTrue((artifact_dir / "resume.pdf").is_file())
        self.assertTrue((artifact_dir / ".preview" / "page-1.png").is_file())

    def test_post_ready_render_audit_failure_preserves_delivery(self):
        original_append_audit = RunArtifacts.append_audit

        def fail_render_audit(artifacts, **event):
            if event.get("event_code") == "RENDER_COMPLETED":
                raise OSError("private audit write failure")
            return original_append_audit(artifacts, **event)

        pipeline = self.make_pipeline(
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])),
            FakeRenderer(),
        )

        with patch.object(RunArtifacts, "append_audit", fail_render_audit):
            run = pipeline.run(self.request())

        self.assertEqual((run["status"], run["error_code"]), ("READY", None))
        artifact_dir = Path(run["artifact_dir"])
        self.assertTrue((artifact_dir / "resume.md").is_file())
        self.assertTrue((artifact_dir / "resume.pdf").is_file())
        self.assertTrue((artifact_dir / ".preview" / "page-1.png").is_file())
        self.assertNotIn(
            "RUN_FAILED",
            [event["event_code"] for event in self._audit_events(run)],
        )

    def test_publication_cleanup_failure_keeps_owned_run_running(self):
        renderer = DerivedOfficialPathInterruptRenderer(
            RuntimeError("private renderer failure")
        )
        pipeline = self.make_pipeline(
            FakeCodexRunner(result_for(self.resume["id"], self.job["id"])),
            renderer,
        )
        original_unlink = Path.unlink

        def locked_official_pdf(path, missing_ok=False):
            if path.name == "resume.pdf" and path.parent.name.startswith(
                "rewrite_run_"
            ):
                raise OSError("official PDF is locked")
            return original_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", locked_official_pdf):
            run = pipeline.run(self.request())

        self.assertEqual((run["status"], run["error_code"]), ("RUNNING", None))
        self.assertIsNotNone(run["owner_token"])
        self.assertIsNotNone(run["lease_expires_at"])
        artifact_dir = Path(run["artifact_dir"])
        self.assertTrue((artifact_dir / "resume.pdf").exists())
        self.assertNotIn(
            "RUN_FAILED",
            [event["event_code"] for event in self._audit_events(run)],
        )

    def test_needs_facts_cleanup_failure_keeps_owned_run_running(self):
        result = in_progress_result(
            self.resume["id"], self.job["id"], claim="实现LangGraph路由"
        )
        pipeline = self.make_pipeline(
            OfficialArtifactWritingRunner(result), FakeRenderer()
        )
        original_unlink = Path.unlink

        def locked_official_pdf(path, missing_ok=False):
            if path.name == "resume.pdf" and path.parent.name.startswith(
                "rewrite_run_"
            ):
                raise OSError("official PDF is locked")
            return original_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", locked_official_pdf):
            try:
                run = pipeline.run(self.request())
            except OSError as error:
                self.fail(f"cleanup failure escaped pipeline: {error}")

        self.assertEqual((run["status"], run["error_code"]), ("RUNNING", None))
        self.assertTrue((Path(run["artifact_dir"]) / "resume.pdf").exists())
        self.assertNotIn(
            "FACTS_REQUIRED",
            [event["event_code"] for event in self._audit_events(run)],
        )

    def test_renderer_base_exception_cleans_derived_official_paths_then_reraises(self):
        for interrupt in (KeyboardInterrupt("stop"), SystemExit("stop")):
            with self.subTest(interrupt=type(interrupt).__name__):
                renderer = DerivedOfficialPathInterruptRenderer(interrupt)
                pipeline = self.make_pipeline(
                    FakeCodexRunner(
                        result_for(self.resume["id"], self.job["id"])
                    ),
                    renderer,
                )

                with self.assertRaises(type(interrupt)) as caught:
                    pipeline.run(self.request())

                self.assertIs(caught.exception, interrupt)
                artifact_dir = renderer.official_dir
                for name in ("resume.md", "resume.pdf"):
                    self.assertFalse((artifact_dir / name).exists())
                self.assertFalse((artifact_dir / ".preview").exists())
                self.assertEqual(list(artifact_dir.glob(".render-stage-*")), [])

    def test_renderer_preview_contract_rejects_zero_or_missing_declared_previews(self):
        renderers = (
            InvalidPreviewContractRenderer(),
            InvalidPreviewContractRenderer(missing_declared_preview=True),
        )
        for renderer in renderers:
            with self.subTest(missing=renderer.missing_declared_preview):
                run = self.make_pipeline(
                    FakeCodexRunner(
                        result_for(self.resume["id"], self.job["id"])
                    ),
                    renderer,
                ).run(self.request())

                self.assertEqual(
                    (run["status"], run["error_code"]),
                    ("FAILED", "RENDER_ERROR"),
                )
                self._assert_no_deployable_artifacts(run)
                artifact_dir = Path(run["artifact_dir"])
                self.assertFalse((artifact_dir / ".preview").exists())
                self.assertEqual(list(artifact_dir.glob(".render-stage-*")), [])

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
                {"claim": "Candidate", "source_resume_ids": [self.resume["id"]], "source_quotes": ["Candidate"], "job_ids": [self.job["id"]], "change_type": "unchanged", "fact_status": "source_verified", "measurement_basis": None, "claim_status": "verified"},
                {"claim": "Built LangGraph agent workflows", "source_resume_ids": [self.resume["id"]], "source_quotes": ["Built LangGraph agent workflows"], "job_ids": [self.job["id"]], "change_type": "unchanged", "fact_status": "source_verified", "measurement_basis": None, "claim_status": "verified"},
            ],
            "gap_acceptance_plans": [],
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
            pipeline.resume(first["id"], [confirmed_answer(question_id="gap-" + "a" * 12, answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")])
        unchanged = pipeline.get(first["id"])
        self.assertEqual((unchanged["status"], unchanged["attempt_count"], unchanged["fact_answers"]), ("NEEDS_FACTS", 1, []))
        self.assertEqual(runner.run_count, 1)

    def test_resume_rejects_duplicate_blocker_answers(self):
        pipeline = self.make_pipeline(FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])), FakeRenderer())
        first = pipeline.run(self.request())
        answer = confirmed_answer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")
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
        answer = confirmed_answer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")
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
        answer = confirmed_answer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")
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
