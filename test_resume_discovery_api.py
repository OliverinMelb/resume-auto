import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from main import app
from resume_agent.repository import Repository


class FakePipeline:
    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return {"id": "rewrite_run_1", "status": "READY"}


class FakeOrchestrator:
    def __init__(self):
        self.start_calls = 0

    def start(self, request):
        self.start_calls += 1
        self.request = request
        return {"id": "discovery_run_1", "status": "COLLECTING"}

    def get(self, run_id):
        return {"id": run_id, "status": "COLLECTING"}

    def poll(self, run_id):
        return {"id": run_id, "status": "READY_FOR_IMPORT"}

    def build_broad_rewrite(self, run_id, candidate_id, source_resume_ids, pipeline):
        self.broad_call = (run_id, candidate_id, source_resume_ids, pipeline)
        return {"id": "rewrite_run_broad", "status": "READY"}


class FakeResolver:
    def resolve(self, target, candidate_id):
        self.call = (target, candidate_id)
        if target.jd_url and "interaction" in target.jd_url:
            raise ValueError("SOURCE_INTERACTION_REQUIRED")
        return "job_targeted"


class DiscoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repository = Repository(self.root / "test.db")
        self.candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        self.resume = self.repository.create_source_resume(
            self.candidate["id"], "resume.md", "resume"
        )
        self.orchestrator = FakeOrchestrator()
        self.resolver = FakeResolver()
        self.pipeline = FakePipeline()
        self.dependencies = SimpleNamespace(
            repository=self.repository,
            orchestrator=self.orchestrator,
            target_resolver=self.resolver,
            rewrite_pipeline=self.pipeline,
        )
        self.router_module = importlib.import_module("resume_agent.router")
        self.original = getattr(self.router_module, "_workflow_dependencies", None)
        self.router_module._workflow_dependencies = self.dependencies
        self.client = TestClient(app)
        self.payload = {
            "candidate_id": self.candidate["id"],
            "sources": ["boss", "nowcoder"],
            "keywords": ["AI Agent"],
            "boss_project_id": "boss-project",
        }

    def tearDown(self):
        if self.original is None:
            delattr(self.router_module, "_workflow_dependencies")
        else:
            self.router_module._workflow_dependencies = self.original
        self.directory.cleanup()

    def test_factory_builds_independent_source_clients_and_shared_core(self):
        from resume_agent.discovery_factory import create_discovery_dependencies
        from resume_agent.settings import ResumeAgentSettings

        settings = ResumeAgentSettings(
            database_path=self.root / "factory.db",
            legacy_action_log_path=self.root / "actions.jsonl",
            legacy_decision_log_path=self.root / "decisions.jsonl",
            allow_remote=False,
            runs_root=self.root / "runs",
            codex_timeout_seconds=30,
            boss_research_endpoint="http://127.0.0.1:8011",
            nowcoder_research_endpoint="http://127.0.0.1:8012",
            discovery_timeout_seconds=15,
        )
        pipeline = FakePipeline()
        built = create_discovery_dependencies(settings, rewrite_pipeline=pipeline)
        boss = built.orchestrator.adapters["boss"]
        nowcoder = built.orchestrator.adapters["nowcoder"]
        self.assertIsNot(boss.client, nowcoder.client)
        self.assertEqual(boss.client.base_url, "http://127.0.0.1:8011")
        self.assertEqual(nowcoder.client.base_url, "http://127.0.0.1:8012")
        self.assertIs(built.target_resolver.adapters["boss"], boss)
        self.assertIs(built.target_resolver.adapters["nowcoder"], nowcoder)
        self.assertIs(built.rewrite_pipeline, pipeline)

        invalid = ResumeAgentSettings(
            database_path=self.root / "invalid.db",
            legacy_action_log_path=self.root / "actions.jsonl",
            legacy_decision_log_path=self.root / "decisions.jsonl",
            allow_remote=False,
            runs_root=self.root / "runs",
            codex_timeout_seconds=30,
            discovery_timeout_seconds=0,
        )
        with self.assertRaisesRegex(ValueError, "discovery timeout"):
            create_discovery_dependencies(invalid, rewrite_pipeline=pipeline)

    def test_discovery_routes_are_local_and_share_core_orchestrator(self):
        response = self.client.post("/resume-agent/v1/discovery-runs", json=self.payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "COLLECTING")
        self.assertEqual(self.orchestrator.start_calls, 1)

        remote = TestClient(app, client=("203.0.113.10", 50000))
        denied = remote.post("/resume-agent/v1/discovery-runs", json=self.payload)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.orchestrator.start_calls, 1)

        spoofed = remote.post(
            "/resume-agent/v1/discovery-runs", json=self.payload,
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(spoofed.status_code, 403)

        ipv6 = TestClient(app, client=("::1", 50000))
        self.assertEqual(
            ipv6.post("/resume-agent/v1/discovery-runs", json=self.payload).status_code,
            201,
        )

    def test_discovery_get_poll_and_broad_routes_use_injected_orchestrator(self):
        fetched = self.client.get("/resume-agent/v1/discovery-runs/discovery_run_1")
        polled = self.client.post("/resume-agent/v1/discovery-runs/discovery_run_1/poll")
        broad = self.client.post(
            "/resume-agent/v1/discovery-runs/discovery_run_1/broad-rewrite",
            json={
                "candidate_id": self.candidate["id"],
                "source_resume_ids": [self.resume["id"]],
            },
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(broad.status_code, 200)
        self.assertEqual(self.orchestrator.broad_call[:3], (
            "discovery_run_1", self.candidate["id"], [self.resume["id"]]
        ))
        self.assertIs(self.orchestrator.broad_call[3], self.pipeline)

    def test_targeted_api_has_no_server_path_and_enforces_one_of(self):
        base = {
            "candidate_id": self.candidate["id"],
            "source_resume_ids": [self.resume["id"]],
        }
        invalid_payloads = [
            base,
            {**base, "job_id": "job_1", "jd_url": "https://www.nowcoder.com/jobs/detail/7"},
            {**base, "jd_path": "C:/private/secret.md"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/resume-agent/v1/targeted-rewrite", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("C:/private/secret.md", response.text)

        response = self.client.post("/resume-agent/v1/targeted-rewrite", json={
            **base, "jd_text": "# 职位名称\nAgent\n\n# 职位描述\nBuild agents",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.resolver.call[0].jd_text.splitlines()[0], "# 职位名称")
        self.assertEqual(self.pipeline.requests[0].job_ids, ["job_targeted"])
        self.assertEqual(self.pipeline.requests[0].candidate_id, self.candidate["id"])

    def test_interaction_required_and_conflicts_have_stable_safe_mapping(self):
        response = self.client.post("/resume-agent/v1/targeted-rewrite", json={
            "candidate_id": self.candidate["id"],
            "source_resume_ids": [self.resume["id"]],
            "jd_url": "https://www.nowcoder.com/jobs/detail/interaction",
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "SOURCE_INTERACTION_REQUIRED")

    def test_gap_acceptance_is_candidate_scoped_and_conflict_maps_409(self):
        evidence = {
            "gap_id": "gap-aaaaaaaaaaaa",
            "project_path": "C:/private/candidate@example.com/project",
            "verified_claims": ["Built the tested workflow"],
            "artifact_hashes": {"report.txt": "a" * 64},
            "test_records": [{
                "command": "python -m unittest",
                "exit_code": 0,
                "output_sha256": "b" * 64,
            }],
            "completed_at": "2026-08-26T01:30:00Z",
            "user_confirmed": True,
            "confirmed_at": "2026-08-26T01:31:00Z",
        }
        first = self.client.post("/resume-agent/v1/gap-acceptances", json={
            "candidate_id": self.candidate["id"], "evidence": evidence,
        })
        self.assertEqual(first.status_code, 201)
        self.assertNotIn("candidate@example.com", first.text)
        self.assertNotIn("C:/private", first.text)

        changed = dict(evidence)
        changed["verified_claims"] = ["Different claim"]
        conflict = self.client.post("/resume-agent/v1/gap-acceptances", json={
            "candidate_id": self.candidate["id"], "evidence": changed,
        })
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"], "GAP_ACCEPTANCE_CONFLICT")

    def test_validation_errors_do_not_echo_inputs_or_unknown_fields(self):
        response = self.client.post("/resume-agent/v1/discovery-runs", json={
            **self.payload,
            "keywords": ["candidate@example.com"],
            "cookie": "session=private-token",
        })
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("candidate@example.com", response.text)
        self.assertNotIn("private-token", response.text)

        oversized_keyword = self.client.post(
            "/resume-agent/v1/discovery-runs",
            json={**self.payload, "keywords": ["k" * 501]},
        )
        self.assertEqual(oversized_keyword.status_code, 422)

        oversized_resume_id = self.client.post(
            "/resume-agent/v1/targeted-rewrite",
            json={
                "candidate_id": self.candidate["id"],
                "source_resume_ids": ["r" * 201],
                "job_id": "job_1",
            },
        )
        self.assertEqual(oversized_resume_id.status_code, 422)


if __name__ == "__main__":
    unittest.main()
