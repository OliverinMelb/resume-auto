import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
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
RUN_ID_PATTERN = re.compile(r"^rewrite_run_[0-9a-f]{32}$")
SOURCE_ID_PATTERN = re.compile(r"^(?:source_resume|job)_[0-9a-f]{32}$")
QUESTION_ID_PATTERN = re.compile(r"^(?:metric|gap|conflict)-[0-9a-f]{12}$")
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
AUDIT_STATUSES = {"QUEUED", "RUNNING", "NEEDS_FACTS", "READY", "FAILED"}
GATE_OUTCOMES = {"PASS", "BLOCKED"}
EVENT_CODES = {
    "RUN_CREATED",
    "RUN_STARTED",
    "MODEL_COMPLETED",
    "FACTS_REQUIRED",
    "FACTS_ACCEPTED",
    "RENDER_COMPLETED",
    "RUN_READY",
    "RUN_FAILED",
    "RUN_RECOVERED",
}
ERROR_CODES = {
    "CODEX_UNAVAILABLE",
    "CODEX_TIMEOUT",
    "CODEX_PROCESS_ERROR",
    "INVALID_MODEL_RESULT",
    "RENDER_ERROR",
    "RENDER_VALIDATION_ERROR",
    "INTERRUPTED",
    "VALIDATION_ERROR",
    "DATABASE_ERROR",
}
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
        page_count: int | None = None,
        content_hash: str | None = None,
        error_safe_hash: str | None = None,
        error_code: str | None = None,
        question_ids: Sequence[str] | None = None,
        source_ids: Sequence[str] | None = None,
        gate_outcome: str | None = None,
        claim_count: int | None = None,
    ) -> None:
        self._validate_run_id(run_id)
        if run_id != self.directory.name:
            raise ValueError("run_id 与运行目录不匹配")
        if status not in AUDIT_STATUSES:
            raise ValueError("不允许的审计状态")
        if event_code not in EVENT_CODES:
            raise ValueError("不允许的审计事件")
        if duration_ms is not None and (
            type(duration_ms) is not int or duration_ms < 0
        ):
            raise ValueError("duration_ms 必须是非负整数")
        if page_count is not None and (type(page_count) is not int or page_count < 1):
            raise ValueError("page_count must be a positive integer")
        self._validate_hash(content_hash, "content_hash")
        self._validate_hash(error_safe_hash, "error_safe_hash")
        if error_code is not None and error_code not in ERROR_CODES:
            raise ValueError("不允许的审计错误码")
        if gate_outcome is not None and gate_outcome not in GATE_OUTCOMES:
            raise ValueError("不允许的门禁结果")
        if claim_count is not None and (
            type(claim_count) is not int or claim_count < 0
        ):
            raise ValueError("claim_count 必须是非负整数")

        event: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "event_code": event_code,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if page_count is not None:
            event["page_count"] = page_count
        if content_hash is not None:
            event["content_hash"] = content_hash
        if error_safe_hash is not None:
            event["error_safe_hash"] = error_safe_hash
        if error_code is not None:
            event["error_code"] = error_code
        if question_ids is not None:
            event["question_ids"] = self._validate_ids(
                question_ids, QUESTION_ID_PATTERN, "question_ids"
            )
        if source_ids is not None:
            event["source_ids"] = self._validate_ids(
                source_ids, SOURCE_ID_PATTERN, "source_ids"
            )
        if gate_outcome is not None:
            event["gate_outcome"] = gate_outcome
        if claim_count is not None:
            event["claim_count"] = claim_count
        path = self.path("audit.md")
        existing = path.read_text("utf-8") if path.exists() else ""
        self._write_text(
            "audit.md", existing + json.dumps(event, ensure_ascii=True) + "\n"
        )

    @staticmethod
    def _validate_run_id(value: str) -> None:
        if not isinstance(value, str) or not RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("run_id 不合法")

    @staticmethod
    def _validate_hash(value: str | None, field_name: str) -> None:
        if value is not None and (
            not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value)
        ):
            raise ValueError(f"{field_name} 必须是 SHA-256 十六进制哈希")

    @classmethod
    def _validate_ids(
        cls,
        values: Sequence[str],
        pattern: re.Pattern[str],
        field_name: str,
    ) -> list[str]:
        if isinstance(values, (str, bytes)):
            raise ValueError(f"{field_name} 必须是 ID 序列")
        normalized = list(values)
        for value in normalized:
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise ValueError(f"{field_name} 包含不合法 ID")
        return normalized

    def remove_if_exists(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
