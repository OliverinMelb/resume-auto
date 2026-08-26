"""Contained, deterministic artifacts for resumable JD discovery runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from resume_agent.discovery_schemas import DiscoveredJob


DISCOVERY_ARTIFACTS = {
    "input-snapshot.json",
    "source-results.json",
    "discovery-report.md",
    "audit.md",
}
DISCOVERY_RUN_ID = re.compile(r"^discovery_run_[0-9a-f]{32}$")
JSON_ARTIFACTS = {"input-snapshot.json", "source-results.json"}
TEXT_ARTIFACTS = {"discovery-report.md"}
AUDIT_STATUSES = {
    "CREATED",
    "COLLECTING",
    "READY_FOR_IMPORT",
    "PARTIAL",
    "IMPORTED",
    "FAILED",
}
AUDIT_EVENT_CODES = {
    "RUN_CREATED",
    "COLLECTION_STARTED",
    "SOURCE_STARTED",
    "SOURCE_POLLED",
    "SOURCE_COMPLETED",
    "SOURCE_INTERACTION_REQUIRED",
    "SOURCE_FAILED",
    "RESULTS_WRITTEN",
    "RUN_READY",
    "RUN_PARTIAL",
    "RUN_IMPORTED",
    "RUN_FAILED",
}
AUDIT_SOURCES = {"boss", "nowcoder"}
AUDIT_ERROR_CODES = {
    "SOURCE_UNAVAILABLE",
    "SOURCE_TASK_FAILED",
    "SOURCE_INTERACTION_REQUIRED",
    "INVALID_SOURCE_RESPONSE",
    "INVALID_SOURCE_CHECKPOINT",
    "DISCOVERY_FAILED",
    "VALIDATION_ERROR",
    "IMPORT_FAILED",
    "INSUFFICIENT_JOBS",
    "JOB_FAMILY_MISMATCH",
}
AUDIT_FIELDS = {
    "run_id",
    "status",
    "event_code",
    "timestamp_utc",
    "source",
    "count",
    "error_code",
}


@dataclass(frozen=True)
class DiscoveryArtifacts:
    root: Path
    run_id: str
    directory: Path

    @classmethod
    def create(cls, root: Path, run_id: str) -> "DiscoveryArtifacts":
        cls._validate_run_id(run_id)
        resolved_root = Path(root).resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        directory = resolved_root / run_id
        if directory.is_symlink():
            raise ValueError("invalid discovery run directory")
        try:
            directory.mkdir(exist_ok=False)
        except (FileExistsError, NotADirectoryError):
            raise ValueError("invalid discovery run directory") from None
        artifacts = cls(resolved_root, run_id, directory)
        artifacts._validate_directory()
        artifacts._atomic_write("audit.md", "")
        return artifacts

    @classmethod
    def open(cls, root: Path, run_id: str) -> "DiscoveryArtifacts":
        cls._validate_run_id(run_id)
        root_path = Path(root)
        if not root_path.is_dir():
            raise ValueError("discovery run directory does not exist")
        resolved_root = root_path.resolve()
        directory = resolved_root / run_id
        artifacts = cls(resolved_root, run_id, directory)
        artifacts._validate_directory()
        return artifacts

    def path(self, name: str) -> Path:
        if name not in DISCOVERY_ARTIFACTS:
            raise ValueError("unknown discovery artifact")
        directory = self._validate_directory()
        target = directory / name
        if target.is_symlink():
            raise ValueError("invalid discovery artifact path")
        if target.exists():
            try:
                resolved = target.resolve(strict=True)
            except OSError:
                raise ValueError("invalid discovery artifact path") from None
            if resolved.parent != directory:
                raise ValueError("invalid discovery artifact path")
        return target

    def write_text(self, name: str, content: str) -> Path:
        if name not in DISCOVERY_ARTIFACTS:
            raise ValueError("unknown discovery artifact")
        if not isinstance(content, str):
            raise ValueError("artifact content must be UTF-8 text")
        if name not in TEXT_ARTIFACTS:
            raise ValueError("artifact is not writable as UTF-8 text")
        return self._atomic_write(name, content)

    def write_json(self, name: str, payload: Any) -> Path:
        if name not in DISCOVERY_ARTIFACTS or name not in JSON_ARTIFACTS:
            raise ValueError("unknown discovery JSON artifact")
        try:
            content = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ) + "\n"
        except (TypeError, ValueError):
            raise ValueError("invalid discovery JSON artifact") from None
        return self._atomic_write(name, content)

    def write_results(self, jobs: Sequence[DiscoveredJob]) -> Path:
        if isinstance(jobs, (str, bytes)) or any(
            not isinstance(job, DiscoveredJob) for job in jobs
        ):
            raise ValueError("invalid discovery results")
        return self.write_json(
            "source-results.json",
            [job.model_dump(mode="json") for job in jobs],
        )

    def append_audit(
        self,
        *,
        run_id: str,
        status: str,
        event_code: str,
        source: str | None = None,
        count: int | None = None,
        error_code: str | None = None,
    ) -> None:
        self._validate_run_id(run_id)
        if run_id != self.run_id:
            raise ValueError("invalid discovery audit run id")
        if status not in AUDIT_STATUSES:
            raise ValueError("invalid discovery audit status")
        if event_code not in AUDIT_EVENT_CODES:
            raise ValueError("invalid discovery audit event code")
        if source is not None and source not in AUDIT_SOURCES:
            raise ValueError("invalid discovery audit source")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("invalid discovery audit count")
        if error_code is not None and error_code not in AUDIT_ERROR_CODES:
            raise ValueError("invalid discovery audit error code")

        event: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "event_code": event_code,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if source is not None:
            event["source"] = source
        if count is not None:
            event["count"] = count
        if error_code is not None:
            event["error_code"] = error_code
        self._validate_audit_event(event)

        audit_path = self.path("audit.md")
        existing = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
        for line in existing.splitlines():
            try:
                previous = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError("invalid discovery audit file") from None
            self._validate_audit_event(previous)
        self._atomic_write(
            "audit.md", existing + json.dumps(event, sort_keys=True) + "\n"
        )

    def _validate_directory(self) -> Path:
        try:
            root = Path(self.root).resolve(strict=True)
        except OSError:
            raise ValueError("invalid discovery run directory") from None
        directory = Path(self.directory)
        if (
            not DISCOVERY_RUN_ID.fullmatch(self.run_id)
            or directory.name != self.run_id
            or directory.parent != root
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            raise ValueError("invalid discovery run directory")
        try:
            resolved_directory = directory.resolve(strict=True)
        except OSError:
            raise ValueError("invalid discovery run directory") from None
        if resolved_directory != directory or resolved_directory.parent != root:
            raise ValueError("invalid discovery run directory")
        return directory

    def _atomic_write(self, name: str, content: str) -> Path:
        if not isinstance(content, str):
            raise ValueError("artifact content must be UTF-8 text")
        target = self.path(name)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=self._validate_directory(),
                prefix=".tmp-",
            ) as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
                temporary = Path(file.name)
            os.replace(temporary, target)
            temporary = None
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not DISCOVERY_RUN_ID.fullmatch(run_id):
            raise ValueError("invalid discovery run id")

    @classmethod
    def _validate_audit_event(cls, event: Any) -> None:
        if not isinstance(event, dict) or not set(event) <= AUDIT_FIELDS:
            raise ValueError("invalid discovery audit event")
        required = {"run_id", "status", "event_code", "timestamp_utc"}
        if not required <= set(event):
            raise ValueError("invalid discovery audit event")
        cls._validate_run_id(event["run_id"])
        if event["status"] not in AUDIT_STATUSES:
            raise ValueError("invalid discovery audit status")
        if event["event_code"] not in AUDIT_EVENT_CODES:
            raise ValueError("invalid discovery audit event code")
        timestamp = event["timestamp_utc"]
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            raise ValueError("invalid discovery audit timestamp")
        if (
            parsed_timestamp.tzinfo is None
            or parsed_timestamp.utcoffset() != timedelta(0)
        ):
            raise ValueError("invalid discovery audit timestamp")
        if "source" in event and event["source"] not in AUDIT_SOURCES:
            raise ValueError("invalid discovery audit source")
        if "count" in event and (
            type(event["count"]) is not int or event["count"] < 0
        ):
            raise ValueError("invalid discovery audit count")
        if "error_code" in event and event["error_code"] not in AUDIT_ERROR_CODES:
            raise ValueError("invalid discovery audit error code")
