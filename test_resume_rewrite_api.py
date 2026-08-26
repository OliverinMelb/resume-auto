import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from resume_agent.repository import NotFoundError
from resume_agent.settings import PROJECT_ROOT, get_settings


class FakePipeline:
    def __init__(self, *, available=True, run_result=None, get_result=None, resume_result=None):
        self.available_value = available
        self.run_result = run_result or {"id": "rewrite_run_1", "status": "READY"}
        self.get_result = get_result or {"id": "rewrite_run_1", "status": "READY"}
        self.resume_result = resume_result or {"id": "rewrite_run_1", "status": "READY"}
        self.received_answers = None

    def available(self):
        return self.available_value

    def run(self, request):
        return self._result(self.run_result)

    def get(self, run_id):
        return self._result(self.get_result, run_id)

    def resume(self, run_id, answers):
        self.received_answers = answers
        return self._result(self.resume_result, run_id)

    @staticmethod
    def _result(result, run_id=None):
        if isinstance(result, Exception):
            raise result
        response = dict(result)
        if run_id is not None:
            response["id"] = run_id
        return response


class RewriteApiTests(unittest.TestCase):
    def setUp(self):
        self.router_module = importlib.import_module("resume_agent.router")
        self.original_pipeline = self.router_module._rewrite_pipeline

    def tearDown(self):
        self.router_module._rewrite_pipeline = self.original_pipeline

    def _client_with(self, pipeline):
        self.router_module._rewrite_pipeline = pipeline
        return TestClient(app)

    @staticmethod
    def _rewrite_payload():
        return {
            "candidate_id": "candidate_1",
            "job_ids": ["job_1"],
            "source_resume_ids": ["source_resume_1"],
            "output_name": "resume.pdf",
        }

    def test_routes_expose_pipeline_results_and_truthful_health(self):
        client = self._client_with(FakePipeline())

        health = client.get("/resume-agent/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["model_enabled"])
        self.assertTrue(health.json()["resume_modification_enabled"])

        created = client.post("/resume-agent/v1/rewrite-runs", json=self._rewrite_payload())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["status"], "READY")

        fetched = client.get("/resume-agent/v1/rewrite-runs/rewrite_run_1")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], "rewrite_run_1")

    def test_facts_endpoint_resumes_needs_facts_run(self):
        pipeline = FakePipeline(
            run_result={"id": "rewrite_run_1", "status": "NEEDS_FACTS"},
            resume_result={"id": "rewrite_run_1", "status": "READY"},
        )
        client = self._client_with(pipeline)

        created = client.post("/resume-agent/v1/rewrite-runs", json=self._rewrite_payload())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["status"], "NEEDS_FACTS")

        resumed = client.post(
            "/resume-agent/v1/rewrite-runs/rewrite_run_1/facts",
            json={"fact_answers": [{
                "question_id": "metric_1",
                "answer": "86",
                "source_note": "Candidate checked the deployment record",
                "user_confirmed": True,
                "confirmed_at": "2026-08-26T01:30:00Z",
            }]},
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "READY")
        self.assertTrue(pipeline.received_answers[0].user_confirmed)
        self.assertEqual(
            pipeline.received_answers[0].confirmed_at.utcoffset().total_seconds(), 0
        )
        self.assertEqual(
            client.get("/resume-agent/v1/rewrite-runs").status_code,
            405,
        )

    def test_failed_statuses_map_to_safe_gateway_errors(self):
        timeout_client = self._client_with(FakePipeline(run_result={
            "id": "rewrite_run_1", "status": "FAILED", "error_code": "CODEX_TIMEOUT",
            "artifact_dir": "C:/private/run",
        }))
        timeout = timeout_client.post("/resume-agent/v1/rewrite-runs", json=self._rewrite_payload())
        self.assertEqual(timeout.status_code, 504)
        self.assertNotIn("C:/private/run", timeout.text)

        failed_client = self._client_with(FakePipeline(get_result={
            "id": "rewrite_run_1", "status": "FAILED", "error_code": "RENDER_ERROR",
            "error_summary": "private renderer exception",
        }))
        failed = failed_client.get("/resume-agent/v1/rewrite-runs/rewrite_run_1")
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("private renderer exception", failed.text)

    def test_pipeline_validation_and_not_found_map_to_client_errors(self):
        invalid_client = self._client_with(FakePipeline(run_result=ValueError("bad request")))
        invalid = invalid_client.post("/resume-agent/v1/rewrite-runs", json=self._rewrite_payload())
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["detail"], "bad request")

        missing_client = self._client_with(FakePipeline(get_result=NotFoundError("run missing")))
        missing = missing_client.get("/resume-agent/v1/rewrite-runs/missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "run missing")

    def test_settings_default_and_timeout_bounds(self):
        with patch.dict(os.environ, {
            "RESUME_AGENT_RUNS_ROOT": "",
            "RESUME_AGENT_CODEX_TIMEOUT_SECONDS": "",
        }, clear=False):
            settings = get_settings()
        self.assertEqual(settings.runs_root, PROJECT_ROOT / "analysis" / "runs")
        self.assertEqual(settings.codex_timeout_seconds, 900)

        for value in ("29", "3601", "not-a-number"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"RESUME_AGENT_CODEX_TIMEOUT_SECONDS": value}, clear=False
            ):
                with self.assertRaises(ValueError):
                    get_settings()

        with patch.dict(os.environ, {
            "RESUME_AGENT_RUNS_ROOT": "custom-runs",
            "RESUME_AGENT_CODEX_TIMEOUT_SECONDS": "3600",
        }, clear=False):
            settings = get_settings()
        self.assertEqual(settings.runs_root, Path("custom-runs"))
        self.assertEqual(settings.codex_timeout_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
