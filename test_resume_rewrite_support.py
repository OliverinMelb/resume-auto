import json
import tempfile
import unittest
from datetime import datetime
from types import MappingProxyType
from pathlib import Path

from resume_agent.rewrite_artifacts import RunArtifacts
from resume_agent.rewrite_prompt import build_codex_task, render_jd_evidence
from resume_agent.rewrite_schemas import FactAnswer


RUN_ID = "rewrite_run_" + "a" * 32
OTHER_RUN_ID = "rewrite_run_" + "b" * 32
SOURCE_RESUME_ID = "source_resume_" + "c" * 32
JOB_ID = "job_" + "d" * 32
QUESTION_ID = "metric-" + "e" * 12


class RewriteSupportTests(unittest.TestCase):
    def test_artifacts_stay_under_runs_root_and_write_utf8_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = RunArtifacts.create(root, RUN_ID)
            artifacts.write_json("input-snapshot.json", {"name": "曹申傲"})
            artifacts.append_audit(
                run_id=RUN_ID,
                status="RUNNING",
                event_code="RUN_STARTED",
            )
            self.assertEqual(
                json.loads(
                    (artifacts.directory / "input-snapshot.json").read_text("utf-8")
                ),
                {"name": "曹申傲"},
            )
            event = json.loads((artifacts.directory / "audit.md").read_text("utf-8"))
            self.assertIn("timestamp_utc", event)
            timestamp = datetime.fromisoformat(event.pop("timestamp_utc"))
            self.assertIsNotNone(timestamp.tzinfo)
            self.assertEqual(
                event,
                {
                    "run_id": RUN_ID,
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

            with self.assertRaises(ValueError):
                RunArtifacts.create(root, "run_" + "a" * 32)

            artifacts = RunArtifacts.create(root, RUN_ID)
            self.assertEqual(RunArtifacts.open(root, RUN_ID), artifacts)
            artifacts.append_audit(
                run_id=RUN_ID,
                status="FAILED",
                event_code="FACTS_REQUIRED",
                duration_ms=42,
                content_hash="a" * 64,
                error_safe_hash="b" * 64,
                error_code="VALIDATION_ERROR",
                question_ids=[QUESTION_ID],
                source_ids=[SOURCE_RESUME_ID, JOB_ID],
                gate_outcome="BLOCKED",
                claim_count=2,
            )
            event = json.loads((artifacts.directory / "audit.md").read_text("utf-8"))
            self.assertEqual(
                set(event),
                {
                    "run_id",
                    "status",
                    "event_code",
                    "timestamp_utc",
                    "duration_ms",
                    "content_hash",
                    "error_safe_hash",
                    "error_code",
                    "question_ids",
                    "source_ids",
                    "gate_outcome",
                    "claim_count",
                },
            )
            self.assertNotIn("曹申傲", json.dumps(event, ensure_ascii=False))
            with self.assertRaises(TypeError):
                artifacts.append_audit(
                    run_id=RUN_ID,
                    status="FAILED",
                    event_code="RUN_FAILED",
                    message="完整简历和 JD 原文",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id=RUN_ID,
                    status="FAILED",
                    event_code="contains free text",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id=OTHER_RUN_ID,
                    status="FAILED",
                    event_code="FACTS_REQUIRED",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id=RUN_ID,
                    status="FAILED",
                    event_code="UNKNOWN_EVENT",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id=RUN_ID,
                    status="FAILED",
                    event_code="RUN_FAILED",
                    error_code="UNKNOWN_ERROR",
                )
            with self.assertRaises(ValueError):
                artifacts.append_audit(
                    run_id=RUN_ID,
                    status="FAILED",
                    event_code="RUN_FAILED",
                    gate_outcome="UNKNOWN",
                )
            for field, value in (
                ("question_ids", ["metric_123abc"]),
                ("source_ids", ["13812345678"]),
                ("source_ids", ["candidate_abcdef"]),
            ):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        artifacts.append_audit(
                            run_id=RUN_ID,
                            status="FAILED",
                            event_code="RUN_FAILED",
                            **{field: value},
                        )
            artifacts.remove_if_exists("resume.pdf")

    def test_text_and_json_writers_reject_binary_and_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = RunArtifacts.create(Path(directory), RUN_ID)
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
        self.assertIn("user_confirmed is true", task)
        self.assertIn("valid UTC timestamp", task)
        self.assertNotIn("danger-full-access", task)

    def test_task_serializes_candidate_fact_answers_and_all_output_fields(self):
        fact_answers = [
                FactAnswer(
                    question_id="metric_1",
                    answer="提高 20%",
                    source_note="仪表盘月报",
                    user_confirmed=True,
                    confirmed_at="2026-08-26T01:30:00Z",
                ),
                MappingProxyType(
                    {
                        "question_id": "metric_2",
                        "answer": "完成 10 次交付",
                        "source_note": "发布记录",
                        "user_confirmed": True,
                        "confirmed_at": "2026-08-26T01:30:00Z",
                    }
                ),
            ]
        try:
            task = build_codex_task(
                base_prompt="base",
                fact_answers=fact_answers,
                allowed_resume_ids=["source_resume_1"],
                allowed_job_ids=["job_1"],
            )
        except TypeError as error:
            self.fail(f"typed confirmation was not JSON serializable: {error}")
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
