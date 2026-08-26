import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from resume_agent.rewrite_schemas import CodexRewriteResult


class CodexRunError(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


class CodexRunner(Protocol):
    def run(
        self,
        task: str,
        schema_path: Path,
        output_path: Path,
        project_root: Path,
    ) -> CodexRewriteResult:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


class CodexCliAdapter:
    def __init__(self, executable: str, timeout_seconds: int):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def run(
        self,
        task: str,
        schema_path: Path,
        output_path: Path,
        project_root: Path,
    ) -> CodexRewriteResult:
        output_path.unlink(missing_ok=True)
        command = [
            self.executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-C",
            str(project_root),
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                input=task,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                shell=False,
                check=False,
            )
        except FileNotFoundError as error:
            output_path.unlink(missing_ok=True)
            raise CodexRunError("CODEX_UNAVAILABLE", "Codex CLI not found") from error
        except subprocess.TimeoutExpired as error:
            output_path.unlink(missing_ok=True)
            raise CodexRunError("CODEX_TIMEOUT", "Codex timed out") from error

        if completed.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise CodexRunError("CODEX_PROCESS_ERROR", "Codex process failed")

        try:
            return CodexRewriteResult.model_validate_json(
                output_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            output_path.unlink(missing_ok=True)
            raise CodexRunError(
                "INVALID_MODEL_RESULT", "Codex output does not match the schema"
            ) from error
