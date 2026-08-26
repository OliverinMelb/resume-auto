import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from os import environ
from pathlib import Path
from unittest.mock import patch

from resume_agent.repository import Repository
from resume_agent.rewrite_cli import bootstrap_request, export_ready_pdf, main
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

    def test_resume_requires_exact_fact_answer_array_and_status_codes(self):
        facts = self.root / "facts.json"
        facts.write_text(json.dumps([{"question_id": "gap-aaaaaaaaaaaa", "answer": "Verified", "source_note": "Candidate confirmed"}]), "utf-8")
        pipeline = FakePipeline({"id": "rewrite_run_abcdef", "status": "FAILED", "blocking_questions": []})
        stdout = io.StringIO()
        with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(stdout):
            code = main(["resume", "--run-id", "rewrite_run_abcdef", "--facts-json", str(facts)])
        self.assertEqual(code, 1)
        self.assertEqual(pipeline.received_answers, [FactAnswer(question_id="gap-aaaaaaaaaaaa", answer="Verified", source_note="Candidate confirmed")])
        facts.write_text(json.dumps({"fact_answers": []}), "utf-8")
        with patch("resume_agent.rewrite_cli._build_pipeline", return_value=pipeline), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["resume", "--run-id", "rewrite_run_abcdef", "--facts-json", str(facts)]), 1)

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


if __name__ == "__main__":
    unittest.main()
