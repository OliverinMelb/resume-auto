import importlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from resume_agent.repository import Repository
from resume_agent.service import ResumeAgentService


class ResumeAgentP0Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.repository = Repository(root / "resume_agent_test.db")
        self.legacy_log = root / "job_actions.jsonl"
        self.decision_log = root / "job_decisions.jsonl"
        self.service = ResumeAgentService(
            self.repository,
            self.legacy_log,
            self.decision_log,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _candidate(self):
        return self.repository.create_candidate(
            display_name="测试候选人",
            target_roles=["AI应用工程师"],
            notes="只允许使用真实经历",
        )

    def test_builds_prompt_without_modifying_resume_or_calling_model(self):
        candidate = self._candidate()
        source_resume = self.repository.create_source_resume(
            candidate_id=candidate["id"],
            label="原始简历",
            content="在真实项目中使用 Python 开发自动化工具。",
        )
        first_job = self.repository.create_job(
            {
                "candidate_id": candidate["id"],
                "source": "manual",
                "title": "AI应用工程师",
                "salary": "20-30K",
                "detail": "负责 Python、智能体和工作流开发。",
                "company": "示例公司A",
            }
        )
        second_job = self.repository.create_job(
            {
                "candidate_id": candidate["id"],
                "source": "manual",
                "title": "大模型应用工程师",
                "salary": "",
                "detail": "要求 Python、RAG、知识库和接口开发。",
                "company": "示例公司B",
            }
        )
        report = self.service.create_research(
            {
                "candidate_id": candidate["id"],
                "target_role": "AI应用工程师",
                "job_ids": [first_job["id"], second_job["id"]],
                "manual_findings": [
                    {
                        "category": "核心技能",
                        "text": "两条JD均明确要求Python",
                        "source_job_ids": [first_job["id"], second_job["id"]],
                        "source_type": "explicit",
                    }
                ],
                "notes": "不要把岗位要求写成候选人事实",
            }
        )

        result = self.service.build_prompt(
            {
                "candidate_id": candidate["id"],
                "source_resume_id": source_resume["id"],
                "research_report_id": report["id"],
                "extra_instructions": "保持一页",
            }
        )

        self.assertEqual(result["status"], "prompt_ready")
        self.assertFalse(result["model_called"])
        self.assertFalse(result["resume_modified"])
        self.assertIn(source_resume["content"], result["prompt"])
        self.assertIn("禁止新增公司、职位、任职时间", result["prompt"])
        self.assertIn("岗位需求，不代表候选人拥有这些能力", result["prompt"])
        self.assertIn("不能执行", result["prompt"])
        self.assertIn("两条JD均明确要求Python", result["prompt"])
        self.assertGreaterEqual(
            next(
                item["job_count"]
                for item in report["keyword_counts"]
                if item["keyword"] == "Python"
            ),
            2,
        )

    def test_research_rejects_finding_with_unselected_job_source(self):
        candidate = self._candidate()
        job = self.repository.create_job(
            {
                "candidate_id": candidate["id"],
                "source": "manual",
                "title": "供应链经理",
                "detail": "负责供应商和交付管理。",
            }
        )

        with self.assertRaisesRegex(ValueError, "未选择的岗位"):
            self.service.create_research(
                {
                    "candidate_id": candidate["id"],
                    "target_role": "供应链经理",
                    "job_ids": [job["id"]],
                    "manual_findings": [
                        {
                            "category": "要求",
                            "text": "需要团队管理",
                            "source_job_ids": ["job_not_selected"],
                            "source_type": "explicit",
                        }
                    ],
                }
            )

    def test_multiple_resumes_build_candidate_profile_prompt_without_api(self):
        candidate = self._candidate()
        first = self.repository.create_source_resume(
            candidate["id"],
            "技术版",
            "2024年负责 Python 自动化工具开发。\n项目结果：节省人工时间。",
        )
        second = self.repository.create_source_resume(
            candidate["id"],
            "产品版",
            "2024年负责 Python 自动化工具开发。\n参与需求分析和项目交付。",
        )

        result = self.service.build_candidate_profile_prompt(
            {
                "candidate_id": candidate["id"],
                "source_resume_ids": [first["id"], second["id"]],
                "extra_instructions": "找出冲突，不要合并数字",
            }
        )

        self.assertFalse(result["model_called"])
        self.assertFalse(result["resume_modified"])
        self.assertEqual(result["evidence_index"]["resume_count"], 2)
        self.assertIn("稳定事实表", result["prompt"])
        self.assertIn(first["id"], result["prompt"])
        self.assertIn(second["id"], result["prompt"])
        repeated = result["evidence_index"]["exact_repeated_lines"]
        self.assertTrue(
            any("Python 自动化工具开发" in item["text"] for item in repeated)
        )

    def test_imports_qualified_job_decisions_and_builds_market_report(self):
        candidate = self._candidate()
        records = [
            {
                "loggedAt": "2026-07-27T09:00:00",
                "searchKeyword": "AI应用工程师",
                "score": 88,
                "threshold": 50,
                "qualified": True,
                "rawJob": (
                    "# 职位名称\nAI应用工程师\n\n"
                    "# 薪资范围\n20-30K·13薪\n\n"
                    "# 职位描述\n负责 Python、智能体和工作流开发"
                ),
            },
            {
                "loggedAt": "2026-07-27T09:10:00",
                "searchKeyword": "AI应用工程师",
                "score": 30,
                "qualified": False,
                "rawJob": (
                    "# 职位名称\n算法工程师\n\n"
                    "# 薪资范围\n30-50K\n\n"
                    "# 职位描述\n负责模型训练"
                ),
            },
        ]
        self.decision_log.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
            + "\n",
            encoding="utf-8",
        )

        imported = self.service.import_decisions(
            {
                "candidate_id": candidate["id"],
                "qualified_only": True,
                "threshold": 50,
                "search_keywords": ["AI应用工程师"],
            }
        )
        repeated_import = self.service.import_decisions(
            {
                "candidate_id": candidate["id"],
                "qualified_only": True,
                "threshold": 50,
                "search_keywords": ["AI应用工程师"],
            }
        )

        self.assertEqual(imported["counters"]["imported"], 1)
        self.assertEqual(imported["counters"]["below_threshold"], 1)
        self.assertEqual(repeated_import["counters"]["duplicates"], 1)

        report = self.service.market_report(
            candidate_id=candidate["id"],
            qualified_only=True,
            search_keyword="AI应用工程师",
        )
        self.assertEqual(report["sample"]["job_count"], 1)
        self.assertEqual(
            report["observed_monthly_midpoint_cny"]["median"], 25000
        )
        self.assertEqual(
            report["observed_annual_midpoint_cny"]["median"], 325000
        )

    def test_delivery_stats_merge_legacy_and_manual_events_without_double_counting(self):
        candidate = self._candidate()
        self.repository.create_delivery_event(
            {
                "candidate_id": candidate["id"],
                "event_type": "interview",
                "event_at": "2026-07-27T10:00:00+00:00",
                "source": "manual",
            }
        )
        self.repository.create_delivery_event(
            {
                "candidate_id": candidate["id"],
                "event_type": "offer",
                "event_at": "2026-07-28T10:00:00+00:00",
                "source": "manual",
            }
        )
        records = [
            {"loggedAt": "2026-07-27T09:00:00", "action": "job_decision_consumed"},
            {"loggedAt": "2026-07-27T09:01:00", "action": "greet_queued"},
            {"loggedAt": "2026-07-27T09:02:00", "action": "greet_message_sent"},
            {"loggedAt": "2026-07-27T09:03:00", "action": "greet_sent"},
            {"loggedAt": "2026-07-27T09:04:00", "action": "resume_sent"},
        ]
        self.legacy_log.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
            + "\nnot-json\n",
            encoding="utf-8",
        )

        stats = self.service.delivery_stats(
            candidate_id=None,
            date_from=None,
            date_to=None,
            include_legacy=True,
        )

        self.assertEqual(stats["legacy_counts"]["greet_sent"], 1)
        self.assertEqual(stats["funnel"]["inferred_recruiter_replies_from_resume_sent"], 1)
        self.assertEqual(stats["funnel"]["interviews"], 1)
        self.assertEqual(stats["funnel"]["offers"], 1)
        self.assertEqual(stats["data_quality"]["malformed_legacy_lines"], 1)
        self.assertEqual(stats["conversion_rates"]["offer_per_interview"], 1.0)

    def test_candidate_filter_excludes_unattributed_legacy_events(self):
        candidate = self._candidate()
        self.legacy_log.write_text(
            json.dumps(
                {"loggedAt": "2026-07-27T09:00:00", "action": "greet_sent"}
            )
            + "\n",
            encoding="utf-8",
        )

        stats = self.service.delivery_stats(
            candidate_id=candidate["id"],
            date_from=None,
            date_to=None,
            include_legacy=True,
        )

        self.assertEqual(stats["legacy_counts"], {})
        self.assertEqual(stats["funnel"]["contacted_or_submitted"], 0)

    def test_main_mounts_subrouter_without_changing_old_routes(self):
        from main import app

        paths = {route.path for route in app.routes}
        self.assertIn("/resume-agent/v1/health", paths)
        self.assertIn("/resume-agent/v1/prompts/resume-rewrite", paths)
        self.assertIn("/resume-agent/v1/prompts/candidate-profile", paths)
        self.assertIn("/resume-agent/v1/imports/job-decisions", paths)
        self.assertIn("/resume-agent/v1/market-report", paths)
        self.assertIn("/resume-agent/v1/delivery-stats", paths)
        self.assertIn("/resume-agent/v1/rewrite-runs", paths)
        self.assertIn("/resume-agent/v1/rewrite-runs/{run_id}", paths)
        self.assertIn("/resume-agent/v1/rewrite-runs/{run_id}/facts", paths)
        self.assertIn("/get-job-score", paths)
        self.assertIn("/log-action", paths)

    def test_subrouter_health_and_candidate_api(self):
        from main import app

        router_module = importlib.import_module("resume_agent.router")
        original_repository = router_module._repository
        original_service = router_module._service
        original_pipeline = router_module._rewrite_pipeline

        class UnavailablePipeline:
            def available(self):
                return False

        router_module._repository = self.repository
        router_module._service = self.service
        router_module._rewrite_pipeline = UnavailablePipeline()
        try:
            client = TestClient(app)
            health = client.get("/resume-agent/v1/health")
            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()["model_enabled"])
            self.assertFalse(health.json()["resume_modification_enabled"])

            created = client.post(
                "/resume-agent/v1/candidates",
                json={
                    "display_name": "API测试",
                    "target_roles": ["供应链"],
                },
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["target_roles"], ["供应链"])
        finally:
            router_module._repository = original_repository
            router_module._service = original_service
            router_module._rewrite_pipeline = original_pipeline

    def test_subrouter_rejects_non_loopback_client(self):
        router_module = importlib.import_module("resume_agent.router")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/resume-agent/v1/health",
                "headers": [],
                "client": ("192.168.1.20", 12345),
                "server": ("127.0.0.1", 8000),
                "scheme": "http",
                "query_string": b"",
            }
        )
        with self.assertRaises(HTTPException) as context:
            router_module.require_local_request(request)
        self.assertEqual(context.exception.status_code, 403)

    def test_readme_documents_linked_broad_and_targeted_modes(self):
        text = Path("readme.md").read_text("utf-8")
        self.assertIn("rewrite_resume.bat discover", text)
        self.assertIn("rewrite_resume.bat broad", text)
        self.assertIn("rewrite_resume.bat targeted", text)
        self.assertIn("在建项目（待验收）", text)
        self.assertNotIn("必须先创建 target-jd.md", text)


if __name__ == "__main__":
    unittest.main()
