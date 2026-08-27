import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from resume_agent.codex_runner import CodexCliAdapter, CodexRunError
from resume_agent.rewrite_schemas import CodexRewriteResult


VALID_RESULT = {
    "verdict": "match",
    "scorecard": [],
    "risks": [],
    "missing_information": [],
    "rewritten_resume_markdown": "# Revised resume\n",
    "rewrite_reasons": [],
    "depth_recommendations": [],
    "interviewer_probes": [],
    "claim_audit": [],
    "gap_acceptance_plans": [],
}

REQUIRED_RESULT_FIELDS = {
    "verdict",
    "scorecard",
    "risks",
    "missing_information",
    "rewritten_resume_markdown",
    "rewrite_reasons",
    "depth_recommendations",
    "interviewer_probes",
    "claim_audit",
    "gap_acceptance_plans",
}


def pending_result_payload():
    return {
        **VALID_RESULT,
        "claim_audit": [
            {
                "claim": "构建本地 Agent 评测集",
                "source_resume_ids": [],
                "source_quotes": [],
                "job_ids": ["job_" + "b" * 32],
                "change_type": "strengthened",
                "fact_status": "unverified",
                "measurement_basis": None,
                "claim_status": "in_progress",
                "gap_id": "gap-123456789abc",
                "acceptance_id": None,
            }
        ],
        "gap_acceptance_plans": [
            {
                "gap_id": "gap-123456789abc",
                "jd_requirements": ["Agent评测"],
                "project_scope": "构建本地评测集",
                "required_artifacts": ["tests/eval_cases.json"],
                "test_commands": ["python -m unittest -v"],
                "acceptance_metrics": ["保存逐案例通过记录"],
                "interview_topics": ["评测集设计"],
            }
        ],
    }


class CodexRunnerTests(unittest.TestCase):
    def test_uses_stdin_ephemeral_read_only_schema_and_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            output = root / "result.json"

            def fake_run(command, **kwargs):
                output.write_text(json.dumps(VALID_RESULT), encoding="utf-8")
                self.assertEqual(command[0:2], ["codex", "exec"])
                self.assertEqual(kwargs["input"], "private task")
                self.assertTrue(kwargs["text"])
                self.assertTrue(kwargs["capture_output"])
                self.assertFalse(kwargs["shell"])
                self.assertEqual(kwargs["timeout"], 15)
                self.assertIn("--ephemeral", command)
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertNotIn("danger-full-access", command)
                self.assertEqual(
                    command,
                    [
                        "codex", "exec", "--ephemeral", "--sandbox", "read-only",
                        "--output-schema", str(schema), "-o", str(output),
                        "-C", str(root), "-",
                    ],
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("resume_agent.codex_runner.subprocess.run", side_effect=fake_run):
                result = CodexCliAdapter("codex", 15).run(
                    "private task", schema, output, root
                )

            self.assertEqual(result.verdict, "match")

    def test_timeout_has_safe_error_code_and_removes_output(self):
        adapter = CodexCliAdapter("codex", 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}", encoding="utf-8")
            output.write_text("stale result", encoding="utf-8")

            with patch(
                "resume_agent.codex_runner.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["codex"], 1, stderr="secret stderr"),
            ):
                with self.assertRaises(CodexRunError) as raised:
                    adapter.run("secret resume", schema, output, root)

            self._assert_safe_error(raised.exception, "CODEX_TIMEOUT", "secret stderr")
            self.assertFalse(output.exists())

    def test_missing_executable_has_safe_error_code(self):
        adapter = CodexCliAdapter("missing-codex", 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}", encoding="utf-8")

            with patch(
                "resume_agent.codex_runner.subprocess.run",
                side_effect=FileNotFoundError("secret system path"),
            ):
                with self.assertRaises(CodexRunError) as raised:
                    adapter.run("secret resume", schema, output, root)

            self._assert_safe_error(
                raised.exception, "CODEX_UNAVAILABLE", "secret system path"
            )

    def test_nonzero_process_error_never_exposes_stderr(self):
        adapter = CodexCliAdapter("codex", 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}", encoding="utf-8")

            with patch(
                "resume_agent.codex_runner.subprocess.run",
                return_value=subprocess.CompletedProcess(["codex"], 7, "", "secret stderr"),
            ):
                with self.assertRaises(CodexRunError) as raised:
                    adapter.run("secret resume", schema, output, root)

            self._assert_safe_error(
                raised.exception, "CODEX_PROCESS_ERROR", "secret stderr"
            )
            self.assertFalse(output.exists())

    def test_missing_output_has_safe_error_code(self):
        adapter = CodexCliAdapter("codex", 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}", encoding="utf-8")

            with patch(
                "resume_agent.codex_runner.subprocess.run",
                return_value=subprocess.CompletedProcess(["codex"], 0, "", ""),
            ):
                with self.assertRaises(CodexRunError) as raised:
                    adapter.run("secret resume", schema, output, root)

            self._assert_safe_error(raised.exception, "INVALID_MODEL_RESULT", "secret resume")

    def test_extra_model_field_is_rejected_and_output_removed(self):
        adapter = CodexCliAdapter("codex", 1)
        invalid_result = {**VALID_RESULT, "unapproved": "extra"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}", encoding="utf-8")

            def fake_run(command, **kwargs):
                output.write_text(json.dumps(invalid_result), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("resume_agent.codex_runner.subprocess.run", side_effect=fake_run):
                with self.assertRaises(CodexRunError) as raised:
                    adapter.run("private task", schema, output, root)

            self._assert_safe_error(raised.exception, "INVALID_MODEL_RESULT", "unapproved")
            self.assertFalse(output.exists())

    def test_malformed_json_is_safely_rejected_and_output_removed(self):
        adapter = CodexCliAdapter("codex", 1)
        malformed_json = '{"verdict": "SECRET-MODEL-OUTPUT"'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}", encoding="utf-8")

            def fake_run(command, **kwargs):
                output.write_text(malformed_json, encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("resume_agent.codex_runner.subprocess.run", side_effect=fake_run):
                with self.assertRaises(CodexRunError) as raised:
                    adapter.run("private task", schema, output, root)

            self._assert_safe_error(
                raised.exception, "INVALID_MODEL_RESULT", "SECRET-MODEL-OUTPUT"
            )
            self.assertFalse(output.exists())

    @patch("resume_agent.codex_runner.shutil.which", return_value=None)
    def test_available_is_false_when_executable_is_not_on_path(self, mocked_which):
        adapter = CodexCliAdapter("codex", 1)

        self.assertFalse(adapter.available())
        mocked_which.assert_called_once_with("codex")

    def _assert_safe_error(self, error, code, secret):
        self.assertEqual(error.code, code)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class CodexSchemaTests(unittest.TestCase):
    def test_codex_result_requires_claim_status_and_gap_plans(self):
        payload = pending_result_payload()

        result = CodexRewriteResult.model_validate(payload)

        self.assertEqual(result.claim_audit[0].claim_status, "in_progress")
        self.assertEqual(result.gap_acceptance_plans[0].gap_id, "gap-123456789abc")

        without_claim_status = pending_result_payload()
        without_claim_status["claim_audit"][0].pop("claim_status")
        without_gap_plans = pending_result_payload()
        without_gap_plans.pop("gap_acceptance_plans")
        for incomplete in (without_claim_status, without_gap_plans):
            with self.subTest(incomplete=incomplete):
                with self.assertRaises(ValidationError):
                    CodexRewriteResult.model_validate(incomplete)

    def test_in_progress_claim_requires_gap_and_forbids_acceptance(self):
        for changes in (
            {"gap_id": None},
            {"acceptance_id": "gap_acceptance_" + "c" * 32},
        ):
            with self.subTest(changes=changes):
                payload = pending_result_payload()
                payload["claim_audit"][0].update(changes)
                with self.assertRaises(ValidationError):
                    CodexRewriteResult.model_validate(payload)

    def test_completed_claim_gap_references_are_both_empty_or_accepted(self):
        payload = pending_result_payload()
        payload["claim_audit"][0].update(
            {
                "claim_status": "verified",
                "acceptance_id": "gap_acceptance_" + "c" * 32,
            }
        )
        payload["gap_acceptance_plans"] = []
        accepted = CodexRewriteResult.model_validate(payload)
        self.assertEqual(
            accepted.claim_audit[0].acceptance_id,
            "gap_acceptance_" + "c" * 32,
        )

        for changes in (
            {"gap_id": "gap-123456789abc", "acceptance_id": None},
            {"gap_id": None, "acceptance_id": "gap_acceptance_" + "c" * 32},
        ):
            with self.subTest(changes=changes):
                payload = pending_result_payload()
                payload["claim_audit"][0].update(
                    {"claim_status": "reframed", **changes}
                )
                payload["gap_acceptance_plans"] = []
                with self.assertRaises(ValidationError):
                    CodexRewriteResult.model_validate(payload)

    def test_gap_plans_match_in_progress_gap_ids_exactly_once(self):
        mismatched = pending_result_payload()
        mismatched["gap_acceptance_plans"][0]["gap_id"] = "gap-aaaaaaaaaaaa"
        duplicate = pending_result_payload()
        duplicate["gap_acceptance_plans"].append(
            dict(duplicate["gap_acceptance_plans"][0])
        )

        for payload in (mismatched, duplicate):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    CodexRewriteResult.model_validate(payload)

    def test_checked_in_schema_matches_the_strict_pydantic_contract(self):
        schema_path = Path(__file__).parent / "resume_agent" / "codex_resume_result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        validated = CodexRewriteResult.model_validate(VALID_RESULT)

        self.assertEqual(validated.verdict, "match")
        self.assertEqual(schema, CodexRewriteResult.model_json_schema())
        self.assertEqual(set(schema["required"]), REQUIRED_RESULT_FIELDS)
        self.assertEqual(set(schema["properties"]), REQUIRED_RESULT_FIELDS)
        self.assertIn("claim_status", schema["$defs"]["ClaimAudit"]["required"])
        self.assertEqual(
            set(schema["$defs"]["GapAcceptancePlan"]["required"]),
            {
                "gap_id",
                "jd_requirements",
                "project_scope",
                "required_artifacts",
                "test_commands",
                "acceptance_metrics",
            },
        )
        self._assert_all_objects_forbid_extra_properties(schema)

    def _assert_all_objects_forbid_extra_properties(self, value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                self.assertFalse(value.get("additionalProperties", True))
            for child in value.values():
                self._assert_all_objects_forbid_extra_properties(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_all_objects_forbid_extra_properties(child)


if __name__ == "__main__":
    unittest.main()
