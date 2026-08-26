import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from resume_agent.codex_runner import CodexRunError
from resume_agent.repository import Repository
from resume_agent.rewrite_pipeline import ResumeRewritePipeline
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer, RewriteRequest
from resume_agent.resume_renderer import RenderError, RenderResult


class FakeCodexRunner:
    def __init__(self, result):
        self.result = result

    def available(self):
        return True

    def run(self, task, schema_path, output_path, project_root):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TempOutputFailureRunner(FakeCodexRunner):
    def run(self, task, schema_path, output_path, project_root):
        output_path.write_text('{"private": "partial output"}', encoding="utf-8")
        raise CodexRunError("CODEX_PROCESS_ERROR", "private runner detail")


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

    def test_metric_gap_needs_facts_and_resume_continues_same_run(self):
        pipeline = self.make_pipeline(FakeCodexRunner(metric_result(self.resume["id"], self.job["id"])), FakeRenderer())
        first = pipeline.run(self.request())
        self.assertEqual(first["status"], "NEEDS_FACTS")
        self.assertFalse((Path(first["artifact_dir"]) / "resume.pdf").exists())
        resumed = pipeline.resume(first["id"], [FactAnswer(question_id=first["blocking_questions"][0]["question_id"], answer="Completed 86 of 100 fixed regression runs; 86/100.", source_note="本地回归记录，候选人确认")])
        self.assertEqual(resumed["id"], first["id"])
        self.assertEqual(resumed["status"], "READY")
        self.assertEqual(resumed["attempt_count"], 2)

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
        self.assertEqual((status, error_code), ("FAILED", "INVALID_MODEL_RESULT"))

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
        events = [json.loads(line) for line in (Path(run["artifact_dir"]) / "audit.md").read_text("utf-8").splitlines()]
        render_event = next(event for event in events if event["event_code"] == "RENDER_COMPLETED")
        ready_event = next(event for event in events if event["event_code"] == "RUN_READY")
        self.assertEqual(render_event["page_count"], 2)
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

    def test_duplicate_ids_are_processed_once_in_request_order(self):
        pipeline = self.make_pipeline(FakeCodexRunner(result_for(self.resume["id"], self.job["id"])), FakeRenderer())
        run = pipeline.run(self.request(job_ids=[self.job["id"], self.job["id"]], source_resume_ids=[self.resume["id"], self.resume["id"]]))
        snapshot = json.loads((Path(run["artifact_dir"]) / "input-snapshot.json").read_text("utf-8"))
        self.assertEqual(snapshot["job_ids"], [self.job["id"]])
        self.assertEqual(snapshot["source_resume_ids"], [self.resume["id"]])


if __name__ == "__main__":
    unittest.main()
