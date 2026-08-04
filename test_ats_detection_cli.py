import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


_REPO_ROOT = Path(__file__).parent


def run_cli(*arguments: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "job_sources.ats.cli", *arguments],
        cwd=_REPO_ROOT,
        input=input_bytes,
        capture_output=True,
        check=False,
    )


class AtsDetectionCliTests(unittest.TestCase):
    def test_file_input_emits_privacy_safe_json_report(self):
        sensitive_url = "http://127.0.0.1/private?access_token=secret"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory, "urls.txt")
            input_path.write_text(sensitive_url + "\n", encoding="utf-8")

            result = run_cli("--input", str(input_path))

        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        self.assertEqual(result.returncode, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["items"][0]["member_id"], "item-000001")
        self.assertEqual(
            report["items"][0]["detection"]["blocked_reason"],
            "unsafe_host",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn(sensitive_url, stdout)
        self.assertNotIn("access_token", stdout)
        self.assertNotIn("secret", stdout)

    def test_stdin_input_emits_jsonl_detection_before_cluster(self):
        sensitive_url = "http://127.0.0.1/private?token=secret"

        result = run_cli(
            "--input",
            "-",
            "--format",
            "jsonl",
            input_bytes=("\n" + sensitive_url + "\n\n").encode("utf-8"),
        )

        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        self.assertEqual(result.returncode, 0, stderr)
        records = tuple(json.loads(line) for line in stdout.splitlines())
        self.assertEqual(
            tuple(record["record_type"] for record in records),
            ("detection", "cluster"),
        )
        self.assertEqual(stderr, "")
        self.assertNotIn(sensitive_url, stdout)
        self.assertNotIn("token", stdout)
        self.assertNotIn("secret", stdout)

    def test_invalid_arguments_do_not_echo_rejected_values(self):
        sensitive_value = "https://example.com/jobs?token=secret"

        result = run_cli(
            "--input",
            "-",
            "--unexpected=" + sensitive_value,
            input_bytes=b"",
        )

        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.splitlines(), ["ats-detect: invalid_arguments"])
        self.assertNotIn(sensitive_value, stderr)
        self.assertNotIn("token", stderr)
        self.assertNotIn("secret", stderr)

    def test_empty_input_is_rejected_with_a_stable_diagnostic(self):
        result = run_cli("--input", "-", input_bytes=b"\n  \n")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr.decode("utf-8").splitlines(),
            ["ats-detect: input_empty"],
        )

    def test_unavailable_input_does_not_echo_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory, "token-secret.txt")
            result = run_cli("--input", str(missing_path))

        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.splitlines(), ["ats-detect: input_unavailable"])
        self.assertNotIn(str(missing_path), stderr)
        self.assertNotIn("token", stderr)
        self.assertNotIn("secret", stderr)

    def test_non_utf8_input_is_rejected_safely(self):
        result = run_cli("--input", "-", input_bytes=b"\xffsecret")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr.decode("utf-8").splitlines(),
            ["ats-detect: input_invalid_utf8"],
        )
        self.assertNotIn(b"secret", result.stderr)

    def test_more_than_one_hundred_urls_is_rejected_before_detection(self):
        input_bytes = (
            "\n".join(f"http://127.0.0.1/{index}" for index in range(101)) + "\n"
        ).encode("utf-8")
        result = run_cli("--input", "-", input_bytes=input_bytes)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr.decode("utf-8").splitlines(),
            ["ats-detect: input_too_many"],
        )
        self.assertNotIn(b"127.0.0.1", result.stderr)

    def test_url_line_over_eight_kib_is_rejected(self):
        oversized_url = b"https://example.com/" + (b"a" * 8_173)
        self.assertEqual(len(oversized_url), 8_193)
        result = run_cli("--input", "-", input_bytes=oversized_url + b"\n")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr.decode("utf-8").splitlines(),
            ["ats-detect: input_too_large"],
        )
        self.assertNotIn(b"example.com", result.stderr)

    def test_total_input_over_one_hundred_twenty_eight_kib_is_rejected(self):
        url = "http://127.0.0.1/" + ("a" * 1_400)
        input_bytes = ("\n".join(url for _ in range(100)) + "\n").encode("utf-8")
        self.assertGreater(len(input_bytes), 128 * 1_024)
        result = run_cli("--input", "-", input_bytes=input_bytes)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr.decode("utf-8").splitlines(),
            ["ats-detect: input_too_large"],
        )
        self.assertNotIn(b"127.0.0.1", result.stderr)

    def test_worker_count_must_be_between_one_and_eight(self):
        for worker_count in ("0", "9"):
            with self.subTest(worker_count=worker_count):
                result = run_cli(
                    "--input",
                    "-",
                    "--workers",
                    worker_count,
                    input_bytes=b"http://127.0.0.1/jobs\n",
                )

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"invalid_arguments", result.stderr)

    def test_detection_failure_returns_one_without_echoing_the_exception(self):
        sensitive_url = "http://127.0.0.1/jobs?token=secret"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "job_sources.ats.cli",
                "--input",
                "-",
            ],
            cwd=_REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIsNotNone(process.stdin)
        self.assertIsNotNone(process.stdout)
        self.assertIsNotNone(process.stderr)
        process.stdout.close()
        process.stdin.write((sensitive_url + "\n").encode("utf-8"))
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8")
        process.stderr.close()
        exit_code = process.wait(timeout=10)

        self.assertEqual(exit_code, 1, stderr)
        self.assertEqual(stderr.splitlines(), ["ats-detect: detection_failed"])
        self.assertNotIn(sensitive_url, stderr)
        self.assertNotIn("token", stderr)
        self.assertNotIn("secret", stderr)


if __name__ == "__main__":
    unittest.main()
