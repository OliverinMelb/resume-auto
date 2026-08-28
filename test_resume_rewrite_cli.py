import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from os import environ
from pathlib import Path
from unittest.mock import patch

from resume_agent.repository import Repository, new_id
from resume_agent.rewrite_cli import (
    _print_result,
    _redact_nested,
    bootstrap_request,
    export_ready_pdf,
    main,
)
from resume_agent.rewrite_schemas import FactAnswer


class FakePipeline:
    def __init__(self, result):
        self.result = result
        self.received_answers = None

    def run(self, request):
        self.request = request
        return self.result

    def resume(self, run_id, answers):
        self.received_answers = answers
        return self.result


class RewriteCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repository = Repository(self.root / "test.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_markdown_and_jd_paths_bootstrap_ids(self):
        resume = self.root / "resume.md"
        jd = self.root / "jd.md"
        resume.write_text("# Candidate\nAgent workflow experience", "utf-8")
        jd.write_text("# 职位名称\nAI Agent工程师\n\n# 职位描述\n负责 Agent 工作流", "utf-8")
        request = bootstrap_request(
            repository=self.repository,
            candidate_id=None,
            candidate_name="Candidate",
            target_role="AI Agent开发",
            resume_paths=[resume],
            jd_paths=[jd],
            output_name=None,
        )
        self.assertEqual(len(request.source_resume_ids), 1)
        self.assertEqual(len(request.job_ids), 1)
        self.assertIn("AI Agent工程师", request.output_name)

    def test_bootstrap_accepts_utf8_bom_and_reuses_exact_content_in_order(self):
        resume = self.root / "resume.txt"
        jd = self.root / "jd.txt"
        resume.write_text("Candidate resume", "utf-8-sig")
        jd.write_text("# 职位名称\nAgent Engineer\n\n# 职位描述\nBuild agents", "utf-8-sig")
        first = bootstrap_request(self.repository, None, "Candidate", "Agent", [resume], [jd], None)
        second = bootstrap_request(
            self.repository, first.candidate_id, None, None,
            [resume, resume], [jd, jd], None,
        )
        self.assertEqual(second.source_resume_ids, first.source_resume_ids)
        self.assertEqual(second.job_ids, first.job_ids)
        self.assertFalse(self.repository.get_source_resume(first.source_resume_ids[0])["content"].startswith("\ufeff"))

    def test_bootstrap_falls_back_to_target_role_and_full_jd_body(self):
        resume = self.root / "resume.md"
        jd = self.root / "jd.md"
        resume.write_text("Candidate", "utf-8")
        jd.write_text("Build reliable agents", "utf-8")
        request = bootstrap_request(self.repository, None, "Candidate", "Target Role", [resume], [jd], None)
        job = self.repository.get_job(request.job_ids[0])
        self.assertEqual(job["title"], "Target Role")
        self.assertEqual(job["detail"], "Build reliable agents")

    def test_bootstrap_rejects_pdf_resume_and_foreign_ids(self):
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        other = self.repository.create_candidate("Other", ["Agent"], "")
        other_resume = self.repository.create_source_resume(other["id"], "other.md", "private")
        job = self.repository.create_job({"candidate_id": candidate["id"], "source": "manual", "title": "Agent", "detail": "x"})
        pdf = self.root / "resume.pdf"
        pdf.write_bytes(b"not read")
        with self.assertRaises(ValueError):
            bootstrap_request(self.repository, candidate["id"], None, None, [pdf], [job["id"]], None)
        with self.assertRaises(ValueError):
            bootstrap_request(
                self.repository, candidate["id"], None, None, [], [], None,
                source_resume_ids=[other_resume["id"]], job_ids=[job["id"]],
            )

    def test_ready_export_never_overwrites_existing_pdf_and_preserves_pdf_suffix(self):
        source = self.root / "resume.pdf"
        source.write_bytes(b"new")
        existing = self.root / "Candidate Company 定制版.pdf"
        existing.write_bytes(b"old")
        exported = export_ready_pdf(source, self.root, existing.name, "rewrite_run_abcdef")
        self.assertEqual(existing.read_bytes(), b"old")
        self.assertEqual(exported.name, "Candidate Company 定制版-abcdef.pdf")
        self.assertEqual(exported.read_bytes(), b"new")

    def test_export_uses_deterministic_second_suffix_when_first_collision_exists(self):
        source = self.root / "resume.pdf"
        source.write_bytes(b"new")
        original = self.root / "Candidate Company 定制版.pdf"
        first_collision = self.root / "Candidate Company 定制版-abcdef.pdf"
        original.write_bytes(b"old")
        first_collision.write_bytes(b"older")
        exported = export_ready_pdf(source, self.root, original.name, "rewrite_run_abcdef")
        self.assertEqual(exported.name, "Candidate Company 定制版-abcdef-2.pdf")
        self.assertEqual(original.read_bytes(), b"old")
        self.assertEqual(first_collision.read_bytes(), b"older")

    def test_collision_suffix_keeps_final_export_name_within_windows_limit(self):
        source = self.root / "resume.pdf"
        source.write_bytes(b"new")
        name = f"{'a' * 176}.pdf"
        (self.root / name).write_bytes(b"old")
        exported = export_ready_pdf(source, self.root, name, "rewrite_run_abcdef")
        self.assertLessEqual(len(exported.name), 180)
        self.assertTrue(exported.name.endswith("-abcdef.pdf"))

    def test_company_section_has_priority_in_generated_output_name(self):
        resume = self.root / "resume.md"
        jd = self.root / "jd.md"
        resume.write_text("Candidate", "utf-8")
        jd.write_text("# 公司\nAcme: Labs\n\n# 职位名称\nAgent Engineer\n\n# 职位描述\nBuild agents", "utf-8")
        request = bootstrap_request(self.repository, None, "Candidate", "Agent", [resume], [jd], None)
        self.assertIn("Acme- Labs", request.output_name)
        self.assertNotIn("Agent Engineer-定制版.pdf", request.output_name)

    def test_export_rejects_traversal_and_symlink_escape(self):
        source = self.root / "resume.pdf"
        source.write_bytes(b"new")
        output = self.root / "output"
        outside = self.root / "outside"
        outside.mkdir()
        output.mkdir()
        link = output / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            export_ready_pdf(source, output, "../escape.pdf", "rewrite_run_abcdef")
        with self.assertRaises(ValueError):
            export_ready_pdf(source, output, "link/escape.pdf", "rewrite_run_abcdef")

    def test_export_rejects_symlinked_source_without_creating_output(self):
        external = self.root / "external.pdf"
        external.write_bytes(b"private")
        linked_source = self.root / "linked.pdf"
        output = self.root / "output"
        try:
            linked_source.symlink_to(external)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            export_ready_pdf(linked_source, output, "safe.pdf", "rewrite_run_abcdef")
        self.assertFalse(output.exists())

    def test_run_outputs_compact_json_and_needs_facts_exit_code_without_private_input(self):
        resume = self.root / "resume.md"
        jd = self.root / "jd.md"
        resume.write_text("PRIVATE RESUME PHONE 555-0100", "utf-8")
        jd.write_text("PRIVATE JD", "utf-8")
        pipeline = FakePipeline({"id": "rewrite_run_abcdef", "status": "NEEDS_FACTS", "blocking_questions": [{"question_id": "gap-aaaaaaaaaaaa"}]})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["run", "--candidate-name", "Candidate", "--target-role", "Agent", "--resume-path", str(resume), "--jd-path", str(jd)])
        self.assertEqual(code, 2)
        self.assertEqual(set(json.loads(stdout.getvalue())), {"id", "status", "blocking_questions", "exported_path"})
        self.assertNotIn("PRIVATE", stdout.getvalue() + stderr.getvalue())

    def test_print_result_recursively_redacts_phone_and_email_without_mutation(self):
        run = {
            "id": "rewrite_run_abcdef",
            "status": "NEEDS_FACTS",
            "blocking_questions": [{
                "question_id": "gap-aaaaaaaaaaaa",
                "question": "Email Candidate.Test+cv@example.com",
                "details": {
                    "contacts": ["+86 138-1234-5678", "13812345678"],
                },
            }],
        }
        original = deepcopy(run)
        exported = Path("Candidate.Test+cv@example.com") / "13812345678.pdf"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            _print_result(run, exported)

        rendered = stdout.getvalue()
        self.assertNotIn("Candidate.Test+cv@example.com", rendered)
        self.assertNotIn("+86 138-1234-5678", rendered)
        self.assertNotIn("13812345678", rendered)
        payload = json.loads(rendered)
        self.assertIn("[REDACTED_EMAIL]", payload["blocking_questions"][0]["question"])
        self.assertEqual(
            payload["blocking_questions"][0]["details"]["contacts"],
            ["[REDACTED_PHONE]", "[REDACTED_PHONE]"],
        )
        self.assertIn("[REDACTED_EMAIL]", payload["exported_path"])
        self.assertIn("[REDACTED_PHONE]", payload["exported_path"])
        self.assertEqual(run, original)

    def test_redaction_preserves_only_exact_current_business_ids(self):
        identifiers = [
            new_id(prefix) for prefix in (
                "candidate", "job", "source_resume", "gap_acceptance",
                "discovery_run", "rewrite_run",
            )
        ]
        ordinary_digest = "a" * 64
        unsafe_prefixed_digest = f"custom_{'b' * 32}"
        rendered = _redact_nested({
            "ids": identifiers,
            "by_id": {identifiers[0]: identifiers[1]},
            "ordinary_digest": ordinary_digest,
            "unsafe_prefixed_digest": unsafe_prefixed_digest,
        })
        self.assertEqual(rendered["ids"], identifiers)
        self.assertEqual(
            rendered["by_id"], {identifiers[0]: identifiers[1]}
        )
        self.assertEqual(rendered["ordinary_digest"], "[REDACTED_DIGEST]")
        self.assertNotEqual(
            rendered["unsafe_prefixed_digest"], unsafe_prefixed_digest
        )

    def test_resume_requires_exact_fact_answer_array_and_status_codes(self):
        facts = self.root / "facts.json"
        facts.write_text(json.dumps([{
            "question_id": "gap-aaaaaaaaaaaa",
            "answer": "Verified",
            "source_note": "Candidate confirmed from a deployment record",
            "user_confirmed": True,
            "confirmed_at": "2026-08-26T01:30:00Z",
        }]), "utf-8")
        pipeline = FakePipeline({"id": "rewrite_run_abcdef", "status": "FAILED", "blocking_questions": []})
        stdout = io.StringIO()
        with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(stdout):
            code = main(["resume", "--run-id", "rewrite_run_abcdef", "--facts-json", str(facts)])
        self.assertEqual(code, 1)
        self.assertEqual(pipeline.received_answers, [FactAnswer(
            question_id="gap-aaaaaaaaaaaa",
            answer="Verified",
            source_note="Candidate confirmed from a deployment record",
            user_confirmed=True,
            confirmed_at="2026-08-26T01:30:00Z",
        )])
        facts.write_text(json.dumps({"fact_answers": []}), "utf-8")
        with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["resume", "--run-id", "rewrite_run_abcdef", "--facts-json", str(facts)]), 1)

    def test_resume_keeps_legacy_fact_input_unconfirmed(self):
        facts = self.root / "facts.json"
        facts.write_text(json.dumps([{
            "question_id": "gap-aaaaaaaaaaaa",
            "answer": "Verified",
            "source_note": "Candidate confirmed",
        }]), "utf-8")
        pipeline = FakePipeline({
            "id": "rewrite_run_abcdef",
            "status": "NEEDS_FACTS",
            "blocking_questions": [],
        })
        with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(io.StringIO()):
            code = main(["resume", "--run-id", "rewrite_run_abcdef", "--facts-json", str(facts)])

        self.assertEqual(code, 2)
        self.assertFalse(pipeline.received_answers[0].user_confirmed)
        self.assertIsNone(pipeline.received_answers[0].confirmed_at)

    def test_resume_rejects_empty_oversized_and_duplicate_fact_arrays_before_pipeline(self):
        facts = self.root / "facts.json"
        pipeline = FakePipeline({"id": "rewrite_run_abcdef", "status": "READY", "blocking_questions": []})
        invalid_payloads = [
            [],
            [{"question_id": f"gap-{number:012d}", "answer": "Verified", "source_note": "Candidate confirmed"} for number in range(201)],
            [
                {"question_id": "gap-aaaaaaaaaaaa", "answer": "Verified", "source_note": "Candidate confirmed"},
                {"question_id": "gap-aaaaaaaaaaaa", "answer": "Again", "source_note": "Candidate confirmed"},
            ],
        ]
        for payload in invalid_payloads:
            with self.subTest(payload_size=len(payload)):
                facts.write_text(json.dumps(payload), "utf-8")
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(["resume", "--run-id", "rewrite_run_abcdef", "--facts-json", str(facts)]), 1)
                self.assertIsNone(pipeline.received_answers)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "FAILED")
                self.assertNotIn("Verified", stdout.getvalue() + stderr.getvalue())

    def test_parse_errors_are_compact_json_and_never_echo_secret_arguments(self):
        for args in (["resume"], ["run", "--unknown", "PRIVATE_TOKEN_123"]):
            with self.subTest(args=args[:2]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(args), 1)
                self.assertEqual(set(json.loads(stdout.getvalue())), {"id", "status", "blocking_questions", "exported_path"})
                self.assertNotIn("PRIVATE_TOKEN_123", stdout.getvalue() + stderr.getvalue())

    def test_run_preserves_interleaved_ids_and_paths_in_request_order(self):
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        stored_resume = self.repository.create_source_resume(candidate["id"], "old.md", "old")
        stored_job = self.repository.create_job({"candidate_id": candidate["id"], "source": "manual", "title": "Old", "detail": "old"})
        new_resume = self.root / "new.md"
        new_jd = self.root / "new-jd.md"
        new_resume.write_text("new", "utf-8")
        new_jd.write_text("# 职位名称\nNew\n\n# 职位描述\nnew", "utf-8")
        pipeline = FakePipeline({"id": "rewrite_run_abcdef", "status": "NEEDS_FACTS", "blocking_questions": []})
        with patch.dict(environ, {"RESUME_AGENT_DB_PATH": str(self.root / "test.db")}), patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                "run", "--candidate-id", candidate["id"],
                "--resume-path", str(new_resume), "--source-resume-id", stored_resume["id"],
                "--jd-path", str(new_jd), "--job-id", stored_job["id"],
            ]), 2)
        self.assertEqual(pipeline.request.source_resume_ids[1], stored_resume["id"])
        self.assertEqual(pipeline.request.job_ids[1], stored_job["id"])

    def test_run_rejects_candidate_id_combined_with_new_candidate_fields(self):
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        resume = self.repository.create_source_resume(candidate["id"], "resume.md", "resume")
        job = self.repository.create_job({"candidate_id": candidate["id"], "source": "manual", "title": "Agent", "detail": "job"})
        pipeline = FakePipeline({"id": "rewrite_run_abcdef", "status": "NEEDS_FACTS", "blocking_questions": []})
        with patch.dict(environ, {"RESUME_AGENT_DB_PATH": str(self.root / "test.db")}), patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main([
                "run", "--candidate-id", candidate["id"], "--candidate-name", "Other", "--target-role", "Other",
                "--source-resume-id", resume["id"], "--job-id", job["id"],
            ])
        self.assertEqual(code, 1)

    def test_device_name_variants_are_normalized_before_request_validation(self):
        resume = self.root / "resume.md"
        jd = self.root / "jd.md"
        resume.write_text("resume", "utf-8")
        jd.write_text("job", "utf-8")
        for name in ("CON .pdf", "NUL .pdf", "LPT1... .pdf"):
            with self.subTest(name=name):
                request = bootstrap_request(self.repository, None, "Candidate", "Role", [resume], [jd], name)
                self.assertTrue(request.output_name.startswith("resume-"))
                self.assertNotIn(" .pdf", request.output_name)

    def test_output_name_is_checked_before_bootstrap_mutates_and_long_names_are_capped(self):
        resume = self.root / "resume.md"
        jd = self.root / "jd.md"
        resume.write_text("resume", "utf-8")
        jd.write_text("job", "utf-8")
        with self.assertRaises(ValueError):
            bootstrap_request(self.repository, None, "Candidate", "Role", [resume], [jd], "../escape.pdf")
        self.assertFalse(self.repository.database_path.exists())
        long_name = "Candidate" * 40
        request = bootstrap_request(self.repository, None, long_name, "Role", [resume], [jd], None)
        self.assertLessEqual(len(request.output_name), 180)
        self.assertTrue(request.output_name.endswith(".pdf"))

    def _linked_cli(self, args, *, orchestrator=None, resolver=None, pipeline=None):
        orchestrator = orchestrator or SimpleNamespace()
        resolver = resolver or SimpleNamespace()
        pipeline = pipeline or FakePipeline({
            "id": "rewrite_run_linked", "status": "READY", "blocking_questions": []
        })
        dependencies = SimpleNamespace(
            repository=self.repository,
            orchestrator=orchestrator,
            target_resolver=resolver,
            rewrite_pipeline=pipeline,
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(environ, {"RESUME_AGENT_DB_PATH": str(self.root / "test.db")}), \
                patch("resume_agent.rewrite_cli._build_dependencies", return_value=dependencies), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        return code, json.loads(stdout.getvalue()), stdout.getvalue() + stderr.getvalue(), pipeline

    def test_discover_and_poll_use_stable_status_codes(self):
        discovery_run_id = new_id("discovery_run")
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")

        class Orchestrator:
            def start(self, request):
                self.request = request
                return {"id": discovery_run_id, "status": "COLLECTING"}

            def poll(self, run_id):
                self.run_id = run_id
                return {"id": run_id, "status": "PARTIAL"}

        orchestrator = Orchestrator()
        code, payload, _, _ = self._linked_cli([
            "discover", "--candidate-id", candidate["id"],
            "--sources", "boss", "nowcoder", "--keyword", "AI Agent",
            "--boss-project-id", "boss-project", "--city", "上海", "--limit", "10",
        ], orchestrator=orchestrator)
        self.assertEqual(code, 3)
        self.assertEqual(payload["id"], discovery_run_id)
        self.assertEqual(payload["status"], "COLLECTING")
        self.assertEqual(orchestrator.request.sources, ["boss", "nowcoder"])
        self.assertEqual(orchestrator.request.keywords, ["AI Agent"])

        code, payload, _, _ = self._linked_cli([
            "poll-discovery", "--discovery-run-id", discovery_run_id
        ], orchestrator=orchestrator)
        self.assertEqual(code, 0)
        self.assertEqual(payload["id"], discovery_run_id)
        self.assertEqual(payload["status"], "PARTIAL")
        self.assertEqual(orchestrator.run_id, discovery_run_id)

    def test_broad_imports_once_and_hands_jobs_to_one_rewrite(self):
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        resume = self.repository.create_source_resume(
            candidate["id"], "resume.md", "resume"
        )
        job_ids = [
            self.repository.create_job({
                "candidate_id": candidate["id"],
                "source": "test",
                "title": f"Agent {index}",
                "detail": "Build agents",
            })["id"]
            for index in range(3)
        ]
        discovery_run_id = new_id("discovery_run")
        rewrite_run_id = new_id("rewrite_run")

        class Orchestrator:
            def build_broad_rewrite(self, run_id, candidate_id, source_resume_ids, pipeline):
                self.call = (run_id, candidate_id, source_resume_ids, pipeline)
                pipeline.request = SimpleNamespace(job_ids=job_ids)
                return pipeline.result

        orchestrator = Orchestrator()
        artifact_dir = self.root / "broad-artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "resume.pdf").write_bytes(b"pdf")
        pipeline = FakePipeline({
            "id": rewrite_run_id, "status": "READY",
            "artifact_dir": str(artifact_dir), "output_name": "broad.pdf",
        })
        code, payload, _, _ = self._linked_cli([
            "broad", "--discovery-run-id", discovery_run_id,
            "--candidate-id", candidate["id"],
            "--source-resume-id", resume["id"],
        ], orchestrator=orchestrator, pipeline=pipeline)
        self.assertEqual(code, 0)
        self.assertEqual(payload["id"], rewrite_run_id)
        self.assertEqual(pipeline.request.job_ids, job_ids)
        self.assertEqual(orchestrator.call[:3], (
            discovery_run_id, candidate["id"], [resume["id"]]
        ))

    def test_targeted_requires_exactly_one_target_at_parser_and_runtime(self):
        target_file = self.root / "target.md"
        target_file.write_text("# 职位名称\nAgent\n\n# 职位描述\nBuild agents", "utf-8")
        for target_args in ([], ["--job-id", "job_1", "--jd-path", str(target_file)]):
            code, payload, rendered, _ = self._linked_cli([
                "targeted", "--candidate-id", "candidate_1",
                "--source-resume-id", "resume_1", *target_args,
            ])
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "FAILED")
            self.assertNotIn(str(target_file), rendered)

    def test_targeted_jd_path_uses_task6_safe_reader_and_resolver(self):
        class Resolver:
            def resolve(self, target, candidate_id):
                self.target = target
                self.candidate_id = candidate_id
                return "job_targeted"

        resolver = Resolver()
        pipeline = FakePipeline({"id": "rewrite_run_targeted", "status": "NEEDS_FACTS"})
        target_file = self.root / "target.md"
        target_file.write_text("# 职位名称\nAgent\n\n# 职位描述\nBuild agents", "utf-8")
        with patch("resume_agent.rewrite_cli.read_target_jd_file", return_value="SAFE JD") as reader:
            code, payload, _, _ = self._linked_cli([
                "targeted", "--candidate-id", "candidate_1",
                "--source-resume-id", "resume_1", "--jd-path", str(target_file),
            ], resolver=resolver, pipeline=pipeline)
        self.assertEqual(code, 2)
        reader.assert_called_once_with(target_file)
        self.assertEqual(resolver.target.jd_text, "SAFE JD")
        self.assertEqual(pipeline.request.job_ids, ["job_targeted"])

    def test_accept_gap_reads_bounded_json_never_executes_and_redacts_all_output(self):
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        private_path = self.root / "private" / "candidate@example.com"
        evidence = self.root / "evidence.json"
        evidence.write_text(json.dumps({
            "gap_id": "gap-aaaaaaaaaaaa",
            "project_path": str(private_path),
            "verified_claims": ["Contact candidate@example.com"],
            "artifact_hashes": {"report.txt": "a" * 64},
            "test_records": [{
                "command": "powershell -Command Remove-Item never-run",
                "exit_code": 0,
                "output_sha256": "b" * 64,
            }],
            "completed_at": "2026-08-26T01:30:00Z",
            "user_confirmed": True,
            "confirmed_at": "2026-08-26T01:31:00Z",
        }), "utf-8")
        with patch("subprocess.run", side_effect=AssertionError("must not execute")):
            code, payload, rendered, _ = self._linked_cli([
                "accept-gap", "--candidate-id", candidate["id"],
                "--evidence-json", str(evidence),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ACCEPTED")
        self.assertNotIn("candidate@example.com", rendered)
        self.assertNotIn(str(private_path), rendered)
        self.assertNotIn("Remove-Item", rendered)

    def test_accept_gap_rejects_oversized_json_before_repository_mutation(self):
        candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        evidence = self.root / "evidence.json"
        evidence.write_bytes(b"{" + b" " * 200_001 + b"}")
        code, payload, _, _ = self._linked_cli([
            "accept-gap", "--candidate-id", candidate["id"],
            "--evidence-json", str(evidence),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(self.repository.list_gap_acceptances(candidate["id"]), [])


if __name__ == "__main__":
    unittest.main()
