import json
import tempfile
import unittest
from types import MappingProxyType
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
            artifacts.append_audit(
                run_id="rewrite_run_abc",
                status="RUNNING",
                event_code="RUN_STARTED",
            )
            self.assertEqual(
                json.loads(
                    (artifacts.directory / "input-snapshot.json").read_text("utf-8")
                ),
                {"name": "曹申傲"},
            )
            self.assertEqual(
                json.loads((artifacts.directory / "audit.md").read_text("utf-8")),
                {
                    "run_id": "rewrite_run_abc",
                    "status": "RUNNING",
                    "event_code": "RUN_STARTED",
                },
            )
            with self.assertRaises(ValueError):
                artifacts.write_text("../escape.txt", "no")

    def test_audit_accepts_only_structured_whitelisted_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                RunArtifacts.create(root, "../outside")

            artifacts = RunArtifacts.create(root, "rewrite_run_abc")
            artifacts.append_audit(
                run_id="rewrite_run_abc",
                status="FAILED",
                event_code="FACT_GATE_BLOCKED",
                duration_ms=42,
                content_hash="a" * 64,
                error_safe_hash="b" * 64,
                error_code="FACTS_REQUIRED",
                question_ids=["metric_1"],
                source_ids=["source_resume_1", "job_1"],
            )
            event = json.loads((artifacts.directory / "audit.md").read_text("utf-8"))
            self.assertEqual(
                set(event),
                {
                    "run_id",
                    "status",
                    "event_code",
                    "duration_ms",
                    "content_hash",
                    "error_safe_hash",
                    "error_code",
                    "question_ids",
                    "source_ids",
                },
            )
            self.assertNotIn("曹申傲", json.dumps(event, ensure_ascii=False))
            with self.assertRaises(TypeError):
                artifacts.append_audit(
                    run_id="rewrite_run_abc",
                    status="FAILED",
                    event_code="FAILED",
                    message="完整简历和 JD 原文",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id="rewrite_run_abc",
                    status="FAILED",
                    event_code="contains free text",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id="rewrite_run_other",
                    status="FAILED",
                    event_code="FACT_GATE_BLOCKED",
                )
            artifacts.remove_if_exists("resume.pdf")

    def test_text_and_json_writers_reject_binary_and_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = RunArtifacts.create(Path(directory), "rewrite_run_abc")
            with self.assertRaises(ValueError):
                artifacts.write_text("resume.pdf", "not a PDF")
            with self.assertRaises(ValueError):
                artifacts.write_json("resume.pdf", {"not": "a PDF"})
            with self.assertRaises(ValueError):
                artifacts.write_text("audit.md", "candidate resume text")

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
                ),
                MappingProxyType(
                    {
                        "question_id": "metric_2",
                        "answer": "完成 10 次交付",
                        "source_note": "发布记录",
                    }
                ),
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
        self.assertIn("完成 10 次交付", task)

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
