import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repository import utc_now


ALLOWED_ARTIFACTS = {
    "input-snapshot.json",
    "jd-evidence.md",
    "codex-task.md",
    "result.json",
    "audit.md",
    "resume.md",
    "resume.pdf",
}
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class RunArtifacts:
    directory: Path

    @classmethod
    def create(cls, root: Path, run_id: str) -> "RunArtifacts":
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("非法 run_id")
        root = root.resolve()
        directory = (root / run_id).resolve()
        if directory.parent != root:
            raise ValueError("运行目录越界")
        directory.mkdir(parents=True, exist_ok=False)
        artifacts = cls(directory)
        artifacts.write_text("audit.md", "# Resume rewrite audit\n")
        return artifacts

    @classmethod
    def open(cls, root: Path, run_id: str) -> "RunArtifacts":
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("非法 run_id")
        root = root.resolve()
        directory = (root / run_id).resolve()
        if directory.parent != root or not directory.is_dir():
            raise ValueError("运行目录不存在或越界")
        return cls(directory)

    def path(self, name: str) -> Path:
        if name not in ALLOWED_ARTIFACTS:
            raise ValueError(f"不允许的产物名: {name}")
        target = (self.directory / name).resolve()
        if target.parent != self.directory.resolve():
            raise ValueError("产物路径越界")
        return target

    def write_text(self, name: str, content: str) -> Path:
        target = self.path(name)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=self.directory,
                prefix=".tmp-",
            ) as file:
                file.write(content)
                file.flush()
                temporary = Path(file.name)
            temporary.replace(target)
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_json(self, name: str, payload: Any) -> Path:
        return self.write_text(
            name, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def append_audit(self, status: str, message: str) -> None:
        safe = PHONE_PATTERN.sub("[REDACTED_PHONE]", message)
        safe = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", safe)
        path = self.path("audit.md")
        existing = (
            path.read_text("utf-8")
            if path.exists()
            else "# Resume rewrite audit\n"
        )
        line = f"\n- {utc_now()} `{status}` {safe}\n"
        self.write_text("audit.md", existing + line)

    def remove_if_exists(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
