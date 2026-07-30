"""Sanitized atomic checkpoints for resumable Nowcoder research tasks."""

from __future__ import annotations

import json
import os
from pathlib import Path


class NowcoderCheckpointStore:
    def __init__(self, directory: Path):
        self._directory = directory

    def save(self, task_id: str, payload: dict[str, object]):
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._path(task_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def load_all(self) -> list[dict[str, object]]:
        if not self._directory.exists():
            return []
        checkpoints: list[dict[str, object]] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                checkpoints.append(value)
        return checkpoints

    def _path(self, task_id: str) -> Path:
        if not task_id.isalnum():
            raise ValueError("invalid Nowcoder task identifier")
        return self._directory / f"{task_id}.json"
