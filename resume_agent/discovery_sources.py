"""Adapters for the local Boss and Nowcoder discovery research services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit

from resume_agent.discovery_http import LocalJsonClient
from resume_agent.discovery_schemas import (
    DiscoveryRequest,
    DiscoveredJob,
    canonicalize_job_url,
)


_STATE_VALUE = str | int | float | bool | None
_POLL_STATUS = Literal["collecting", "completed", "interaction_required", "failed"]
_BOSS_JOB_PATH = re.compile(r"^/job_detail/([^/]+)\.html$")
_NOWCODER_JOB_PATH = re.compile(r"^/jobs/detail/(\d+)$")


class SourceInteractionRequiredError(RuntimeError):
    """The source can continue only after a user completes a source interaction."""


@dataclass(frozen=True)
class SourceCheckpoint:
    source: Literal["boss", "nowcoder"]
    remote_ids: tuple[str, ...]
    state: dict[str, _STATE_VALUE]

    def __post_init__(self) -> None:
        if self.source not in {"boss", "nowcoder"}:
            raise ValueError("invalid source checkpoint")
        if not isinstance(self.remote_ids, tuple) or any(
            not isinstance(remote_id, str) or not remote_id for remote_id in self.remote_ids
        ):
            raise ValueError("invalid source checkpoint")
        if not isinstance(self.state, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, (str, int, float, bool, type(None)))
            for key, value in self.state.items()
        ):
            raise ValueError("invalid source checkpoint")
        object.__setattr__(self, "remote_ids", tuple(self.remote_ids))
        object.__setattr__(self, "state", dict(self.state))

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "remote_ids": list(self.remote_ids),
            "state": dict(self.state),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "SourceCheckpoint":
        try:
            if not isinstance(payload, dict):
                raise TypeError
            remote_ids = payload["remote_ids"]
            if not isinstance(remote_ids, (list, tuple)) or isinstance(remote_ids, str):
                raise TypeError
            state = payload.get("state", {})
            if not isinstance(state, dict):
                raise TypeError
            return cls(
                source=payload["source"],
                remote_ids=tuple(remote_ids),
                state=dict(state),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid source checkpoint") from None


@dataclass(frozen=True)
class SourcePollResult:
    source: str
    status: _POLL_STATUS
    jobs: tuple[DiscoveredJob, ...]
    checkpoint: SourceCheckpoint
    error_code: str | None = None


class DiscoverySourceAdapter(Protocol):
    source: Literal["boss", "nowcoder"]

    def start(self, request: DiscoveryRequest) -> SourceCheckpoint: ...

    def poll(self, checkpoint: SourceCheckpoint) -> SourcePollResult: ...

    def resolve_url(self, url: str, keyword: str) -> DiscoveredJob: ...


class BossResearchAdapter:
    source: Literal["boss"] = "boss"

    def __init__(self, client: LocalJsonClient):
        self.client = client

    def start(self, request: DiscoveryRequest) -> SourceCheckpoint:
        if not request.boss_project_id:
            raise ValueError("boss_project_id is required for Boss discovery")
        return SourceCheckpoint(
            source=self.source,
            remote_ids=(request.boss_project_id,),
            state=_checkpoint_state(request.keywords, request.limit_per_source),
        )

    def poll(self, checkpoint: SourceCheckpoint) -> SourcePollResult:
        keywords, limit = _decode_checkpoint(checkpoint, self.source, expected_ids=1)
        project_id = checkpoint.remote_ids[0]
        payload = self.client.request_json(
            "GET",
            "/resume-agent/v1/candidates/"
            f"{quote(project_id, safe='')}/jobs?qualified_only=true",
        )
        rows = payload.get("jobs")
        if not isinstance(rows, list):
            raise ValueError("invalid source response")
        wanted_keywords = {_normalized_keyword(keyword) for keyword in keywords}
        jobs = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            keyword = _text(row.get("search_keyword"))
            detail = _text(row.get("detail"))
            if not detail or _normalized_keyword(keyword) not in wanted_keywords:
                continue
            job = _map_boss_job(row, keyword)
            if job is not None:
                jobs.append(job)
            if len(jobs) == limit:
                break
        return SourcePollResult(self.source, "completed", tuple(jobs), checkpoint)

    def resolve_url(self, url: str, keyword: str) -> DiscoveredJob:
        if not _is_boss_job_url(url):
            raise ValueError("invalid Boss job URL")
        raise SourceInteractionRequiredError("SOURCE_INTERACTION_REQUIRED")


class NowcoderResearchAdapter:
    source: Literal["nowcoder"] = "nowcoder"

    def __init__(self, client: LocalJsonClient):
        self.client = client

    def start(self, request: DiscoveryRequest) -> SourceCheckpoint:
        task_ids: list[str] = []
        for keyword in request.keywords:
            payload = {
                "keyword": keyword,
                "recruitment_channel": request.recruitment_channel,
                "mode": "research",
                "acquisition_mode": request.nowcoder_acquisition_mode,
            }
            if request.city is not None:
                payload["city"] = request.city
            response = self.client.request_json("POST", "/job-sources/nowcoder/tasks", payload)
            task_id = response.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("invalid source response")
            task_ids.append(task_id)
        return SourceCheckpoint(
            source=self.source,
            remote_ids=tuple(task_ids),
            state=_checkpoint_state(request.keywords, request.limit_per_source),
        )

    def poll(self, checkpoint: SourceCheckpoint) -> SourcePollResult:
        keywords, limit = _decode_checkpoint(
            checkpoint, self.source, expected_ids=len(checkpoint.remote_ids)
        )
        if not checkpoint.remote_ids:
            raise ValueError("invalid source checkpoint")
        statuses = []
        for task_id in checkpoint.remote_ids:
            response = self.client.request_json(
                "GET", f"/job-sources/nowcoder/tasks/{quote(task_id, safe='')}"
            )
            status = response.get("status")
            statuses.append(status if isinstance(status, str) else "unknown")
        if any(status in {"waiting_for_login", "verification_required"} for status in statuses):
            return SourcePollResult(self.source, "interaction_required", (), checkpoint)
        if any(status in {"pending", "running"} for status in statuses):
            return SourcePollResult(self.source, "collecting", (), checkpoint)
        if not all(status == "completed" for status in statuses):
            return SourcePollResult(
                self.source, "failed", (), checkpoint, error_code="SOURCE_TASK_FAILED"
            )

        jobs: list[DiscoveredJob] = []
        for task_id, keyword in zip(checkpoint.remote_ids, keywords, strict=True):
            response = self.client.request_json(
                "GET", f"/job-sources/nowcoder/tasks/{quote(task_id, safe='')}/jobs"
            )
            rows = response.get("jobs")
            if not isinstance(rows, list):
                raise ValueError("invalid source response")
            for row in rows:
                if isinstance(row, dict):
                    job = _map_nowcoder_job(row, keyword)
                    if job is not None:
                        jobs.append(job)
                        if len(jobs) == limit:
                            return SourcePollResult(
                                self.source, "completed", tuple(jobs), checkpoint
                            )
        return SourcePollResult(self.source, "completed", tuple(jobs), checkpoint)

    def resolve_url(self, url: str, keyword: str) -> DiscoveredJob:
        job_id = _nowcoder_job_id(url)
        if job_id is None:
            raise ValueError("invalid Nowcoder job URL")
        response = self.client.request_json(
            "GET", f"/job-sources/nowcoder/jobs/{quote(job_id, safe='')}"
        )
        row = response.get("job", response)
        if not isinstance(row, dict):
            raise ValueError("invalid source response")
        job = _map_nowcoder_job(row, keyword)
        if job is None:
            raise ValueError("invalid source response")
        return job


def _checkpoint_state(keywords: list[str], limit_per_source: int) -> dict[str, _STATE_VALUE]:
    return {
        "keywords_json": json.dumps(keywords, ensure_ascii=False, separators=(",", ":")),
        "limit_per_source": limit_per_source,
    }


def _decode_checkpoint(
    checkpoint: SourceCheckpoint,
    source: Literal["boss", "nowcoder"],
    *,
    expected_ids: int,
) -> tuple[list[str], int]:
    if checkpoint.source != source or not checkpoint.remote_ids:
        raise ValueError("invalid source checkpoint")
    try:
        keywords_raw = checkpoint.state["keywords_json"]
        limit = checkpoint.state["limit_per_source"]
        if not isinstance(keywords_raw, str) or isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError
        keywords = json.loads(keywords_raw)
        if (
            not isinstance(keywords, list)
            or not keywords
            or any(not isinstance(keyword, str) or not _normalized_keyword(keyword) for keyword in keywords)
            or (source == "nowcoder" and len(keywords) != expected_ids)
            or not 1 <= limit <= 20
        ):
            raise TypeError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid source checkpoint") from None
    return keywords, limit


def _map_boss_job(row: dict, keyword: str) -> DiscoveredJob | None:
    external_id = _text(row.get("external_job_id"))
    title = _text(row.get("title"))
    detail = _text(row.get("detail"))
    verified_at = _utc_datetime(row.get("source_logged_at"))
    if not external_id or not title or not detail or verified_at is None:
        return None
    parsed = urlsplit(external_id)
    match = _BOSS_JOB_PATH.fullmatch(parsed.path)
    source_job_id = match.group(1) if match else external_id
    try:
        return DiscoveredJob(
            source="boss",
            source_job_id=source_job_id,
            canonical_url=canonicalize_job_url(external_id),
            title=title,
            company=_text(row.get("company")),
            salary_raw=_none_if_empty(row.get("salary")),
            detail=detail,
            detail_status="complete",
            search_keyword=keyword,
            verified_at=verified_at,
            score=_number_or_none(row.get("score")),
            qualified=_bool_or_none(row.get("qualified")),
        )
    except ValueError:
        return None


def _map_nowcoder_job(row: dict, keyword: str) -> DiscoveredJob | None:
    source_job_id = _text(row.get("source_job_id"))
    source_url = _text(row.get("source_url"))
    title = _text(row.get("title"))
    if not source_job_id or not source_url or not title:
        return None
    description = _text(row.get("description"))
    responsibilities = _text(row.get("responsibilities"))
    requirements = _text(row.get("requirements"))
    detail = "\n\n".join(part for part in (description, responsibilities, requirements) if part)
    raw_status = row.get("detail_status")
    detail_status: Literal["complete", "partial"]
    if raw_status == "complete" and detail:
        detail_status = "complete"
    elif raw_status == "partial":
        detail_status = "partial"
    elif description:
        detail_status = "complete"
    else:
        detail_status = "partial"
    if not detail:
        detail = title
    verified_at = _utc_datetime(row.get("last_verified_at"))
    if verified_at is None:
        return None
    try:
        return DiscoveredJob(
            source="nowcoder",
            source_job_id=source_job_id,
            canonical_url=canonicalize_job_url(source_url),
            title=title,
            company=_text(row.get("company")),
            location=_joined_locations(row.get("locations")),
            salary_raw=_none_if_empty(row.get("salary_text")),
            detail=detail,
            detail_status=detail_status,
            search_keyword=keyword,
            published_at=_utc_datetime(row.get("published_at")),
            verified_at=verified_at,
            score=_number_or_none(row.get("score")),
            qualified=_bool_or_none(row.get("qualified")),
            source_metadata=_safe_nowcoder_metadata(row),
        )
    except ValueError:
        return None


def _safe_nowcoder_metadata(row: dict) -> dict[str, _STATE_VALUE]:
    metadata: dict[str, _STATE_VALUE] = {}
    for key in ("recruitment_channel", "employment_type", "upstream_platform"):
        value = row.get(key)
        if isinstance(value, (str, int, float, bool, type(None))):
            metadata[key] = value
    return metadata


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _none_if_empty(value: object) -> str | None:
    text = _text(value)
    return text or None


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _normalized_keyword(value: str) -> str:
    return " ".join(value.split()).casefold()


def _utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _joined_locations(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    locations = []
    for item in value:
        item_text = _text(item)
        if item_text and item_text not in locations:
            locations.append(item_text)
    return ", ".join(locations) or None


def _is_boss_job_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"zhipin.com", "www.zhipin.com"}
        and parsed.username is None
        and parsed.password is None
        and _BOSS_JOB_PATH.fullmatch(parsed.path) is not None
    )


def _nowcoder_job_id(url: str) -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.nowcoder.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    match = _NOWCODER_JOB_PATH.fullmatch(parsed.path)
    return match.group(1) if match else None
