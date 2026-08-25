import json
import tempfile
import unittest
from pathlib import Path

from resume_agent.rewrite_artifacts import RunArtifacts
from resume_agent.rewrite_prompt import build_codex_task, render_jd_evidence
from resume_agent.rewrite_schemas import FactAnswer


class RewriteSupportTests(unittest.TestCase):
    def test_artifacts_stay_under_runs_root_and_write_utf8_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = RunArtifacts.create(root, "rewrite_run_abc")
            artifacts.write_json("input-snapshot.json", {"name": "曹申傲"})
            artifacts.append_audit("RUNNING", "开始")
            self.assertEqual(
                json.loads(
                    (artifacts.directory / "input-snapshot.json").read_text("utf-8")
                ),
                {"name": "曹申傲"},
            )
            self.assertIn(
                "RUNNING", (artifacts.directory / "audit.md").read_text("utf-8")
            )
            with self.assertRaises(ValueError):
                artifacts.write_text("../escape.txt", "no")

    def test_artifacts_reject_invalid_runs_and_redact_audit_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                RunArtifacts.create(root, "../outside")

            artifacts = RunArtifacts.create(root, "rewrite_run_abc")
            artifacts.append_audit(
                "FAILED", "contact 13812345678 or user@example.com"
            )
            audit = (artifacts.directory / "audit.md").read_text("utf-8")
            self.assertIn("[REDACTED_PHONE]", audit)
            self.assertIn("[REDACTED_EMAIL]", audit)
            self.assertNotIn("13812345678", audit)
            self.assertNotIn("user@example.com", audit)
            artifacts.remove_if_exists("resume.pdf")

    def test_task_marks_resume_and_jd_as_data_and_requires_skill_sections(self):
        task = build_codex_task(
            base_prompt="原始 prompt",
            fact_answers=[],
            allowed_resume_ids=["source_resume_1"],
            allowed_job_ids=["job_1"],
        )
        self.assertIn("$improving-agent-resumes", task)
        self.assertIn("不可信数据", task)
        self.assertIn("rewritten_resume_markdown", task)
        self.assertIn("claim_audit", task)
        self.assertNotIn("danger-full-access", task)

    def test_task_serializes_candidate_fact_answers_and_all_output_fields(self):
        task = build_codex_task(
            base_prompt="base",
            fact_answers=[
                FactAnswer(
                    question_id="metric_1",
                    answer="提高 20%",
                    source_note="仪表盘月报",
                )
            ],
            allowed_resume_ids=["source_resume_1"],
            allowed_job_ids=["job_1"],
        )
        for field in (
            "verdict",
            "scorecard",
            "risks",
            "missing_information",
            "rewritten_resume_markdown",
            "rewrite_reasons",
            "depth_recommendations",
            "interviewer_probes",
            "claim_audit",
        ):
            self.assertIn(field, task)
        self.assertIn("候选人提供的证据", task)
        self.assertIn("提高 20%", task)

    def test_jd_evidence_keeps_explicit_text_separate_from_keyword_inference(self):
        text = render_jd_evidence(
            [
                {
                    "id": "job_1",
                    "title": "Agent工程师",
                    "company": "甲公司",
                    "detail": "负责Agent工作流",
                }
            ],
            [
                {
                    "keyword": "工作流",
                    "job_count": 1,
                    "job_ratio": 1.0,
                    "source_job_ids": ["job_1"],
                }
            ],
        )
        self.assertIn("JD 明确文本", text)
        self.assertIn("确定性关键词线索（不代表候选人能力）", text)
        self.assertIn("负责Agent工作流", text)
        self.assertIn("工作流", text)


if __name__ == "__main__":
    unittest.main()
