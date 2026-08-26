import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
AUDIT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
AUDIT_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
AUDIT_STATUSES = {"QUEUED", "RUNNING", "NEEDS_FACTS", "READY", "FAILED"}
TEXT_ARTIFACTS = ALLOWED_ARTIFACTS - {"audit.md", "resume.pdf"}


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
        artifacts._write_text("audit.md", "")
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
        if name not in TEXT_ARTIFACTS:
            raise ValueError("该产物不允许文本写入")
        return self._write_text(name, content)

    def _write_text(self, name: str, content: str) -> Path:
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

    def append_audit(
        self,
        *,
        run_id: str,
        status: str,
        event_code: str,
        duration_ms: int | None = None,
        content_hash: str | None = None,
        error_safe_hash: str | None = None,
        error_code: str | None = None,
        question_ids: Sequence[str] | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> None:
        self._validate_audit_id(run_id, "run_id")
        if run_id != self.directory.name:
            raise ValueError("run_id 与运行目录不匹配")
        if status not in AUDIT_STATUSES:
            raise ValueError("不允许的审计状态")
        self._validate_audit_code(event_code, "event_code")
        if duration_ms is not None and (
            type(duration_ms) is not int or duration_ms < 0
        ):
            raise ValueError("duration_ms 必须是非负整数")
        self._validate_hash(content_hash, "content_hash")
        self._validate_hash(error_safe_hash, "error_safe_hash")
        if error_code is not None:
            self._validate_audit_code(error_code, "error_code")

        event: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "event_code": event_code,
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if content_hash is not None:
            event["content_hash"] = content_hash
        if error_safe_hash is not None:
            event["error_safe_hash"] = error_safe_hash
        if error_code is not None:
            event["error_code"] = error_code
        if question_ids is not None:
            event["question_ids"] = self._validate_audit_ids(
                question_ids, "question_ids"
            )
        if source_ids is not None:
            event["source_ids"] = self._validate_audit_ids(
                source_ids, "source_ids"
            )
        path = self.path("audit.md")
        existing = path.read_text("utf-8") if path.exists() else ""
        self._write_text(
            "audit.md", existing + json.dumps(event, ensure_ascii=True) + "\n"
        )

    @staticmethod
    def _validate_audit_id(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not AUDIT_ID_PATTERN.fullmatch(value):
            raise ValueError(f"{field_name} 不合法")

    @staticmethod
    def _validate_audit_code(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not AUDIT_CODE_PATTERN.fullmatch(value):
            raise ValueError(f"{field_name} 不合法")

    @staticmethod
    def _validate_hash(value: str | None, field_name: str) -> None:
        if value is not None and (
            not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value)
        ):
            raise ValueError(f"{field_name} 必须是 SHA-256 十六进制哈希")

    @classmethod
    def _validate_audit_ids(
        cls, values: Sequence[str], field_name: str
    ) -> list[str]:
        if isinstance(values, (str, bytes)):
            raise ValueError(f"{field_name} 必须是 ID 序列")
        normalized = list(values)
        for value in normalized:
            cls._validate_audit_id(value, field_name)
        return normalized

    def remove_if_exists(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
