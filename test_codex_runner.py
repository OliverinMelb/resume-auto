import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

            self.assertEqual(raised.exception.code, "CODEX_TIMEOUT")
            self.assertNotIn("secret stderr", str(raised.exception))
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

            self.assertEqual(raised.exception.code, "CODEX_UNAVAILABLE")
            self.assertNotIn("secret system path", str(raised.exception))

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

            self.assertEqual(raised.exception.code, "CODEX_PROCESS_ERROR")
            self.assertNotIn("secret stderr", str(raised.exception))
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

            self.assertEqual(raised.exception.code, "INVALID_MODEL_RESULT")

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

            self.assertEqual(raised.exception.code, "INVALID_MODEL_RESULT")
            self.assertFalse(output.exists())

    @patch("resume_agent.codex_runner.shutil.which", return_value=None)
    def test_available_is_false_when_executable_is_not_on_path(self, mocked_which):
        adapter = CodexCliAdapter("codex", 1)

        self.assertFalse(adapter.available())
        mocked_which.assert_called_once_with("codex")


class CodexSchemaTests(unittest.TestCase):
    def test_checked_in_schema_matches_the_strict_pydantic_contract(self):
        schema_path = Path(__file__).parent / "resume_agent" / "codex_resume_result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        validated = CodexRewriteResult.model_validate(VALID_RESULT)

        self.assertEqual(validated.verdict, "match")
        self.assertEqual(set(schema["required"]), REQUIRED_RESULT_FIELDS)
        self.assertEqual(set(schema["properties"]), REQUIRED_RESULT_FIELDS)
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
