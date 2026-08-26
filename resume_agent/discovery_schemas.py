"""Canonical models and deterministic identity helpers for discovered jobs."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Literal, get_args
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator

from resume_agent.rewrite_schemas import StrictModel


class DiscoveredJob(StrictModel):
    source: Literal["boss", "nowcoder"]
    source_job_id: str = Field(min_length=1, max_length=500)
    canonical_url: str = Field(min_length=1, max_length=8192)
    title: str = Field(min_length=1, max_length=500)
    company: str = Field(default="", max_length=500)
    location: str | None = Field(default=None, max_length=500)
    salary_raw: str | None = Field(default=None, max_length=200)
    detail: str = Field(min_length=1, max_length=100_000)
    detail_status: Literal["complete", "partial"]
    search_keyword: str = Field(min_length=1, max_length=500)
    published_at: datetime | None = None
    verified_at: datetime
    score: float | None = Field(default=None, ge=0, le=100)
    qualified: bool | None = None
    source_metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )

    @field_validator("verified_at", "published_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("discovery timestamps must be UTC")
        return value


class DiscoveryRequest(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    sources: list[Literal["boss", "nowcoder"]] = Field(min_length=1, max_length=2)
    keywords: list[str] = Field(min_length=1, max_length=10)
    target_family: Literal["ai_engineering"] = "ai_engineering"
    city: str | None = Field(default=None, max_length=100)
    recruitment_channel: Literal["internship", "campus", "social"] = "social"
    limit_per_source: int = Field(default=10, ge=1, le=20)
    boss_project_id: str | None = Field(default=None, max_length=200)
    nowcoder_acquisition_mode: Literal["public", "browser"] = "public"

    @field_validator("sources", "keywords")
    @classmethod
    def unique_in_order(cls, values: list[str]) -> list[str]:
        if len(values) != len(dict.fromkeys(values)):
            raise ValueError("discovery inputs must be unique")
        return values


DiscoveryRunStatus = Literal[
    "CREATED", "COLLECTING", "READY_FOR_IMPORT", "PARTIAL", "IMPORTED", "FAILED"
]
DISCOVERY_RUN_ID_PATTERN = re.compile(r"^discovery_run_[0-9a-f]{32}$")
DISCOVERY_RUN_STATUSES = frozenset(get_args(DiscoveryRunStatus))
DISCOVERY_STATUS_TRANSITIONS = {
    "CREATED": frozenset({"COLLECTING", "FAILED"}),
    "COLLECTING": frozenset(
        {"COLLECTING", "READY_FOR_IMPORT", "PARTIAL", "FAILED"}
    ),
    "READY_FOR_IMPORT": frozenset(
        {"READY_FOR_IMPORT", "PARTIAL", "IMPORTED", "FAILED"}
    ),
    "PARTIAL": frozenset({"READY_FOR_IMPORT", "PARTIAL", "IMPORTED", "FAILED"}),
    "IMPORTED": frozenset(),
    "FAILED": frozenset(),
}


_TRACKING_QUERY_PARAMETERS = {
    "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "_ga", "_gl",
}


def canonicalize_job_url(url: str) -> str:
    """Return a URL suitable for deterministic job identity comparisons."""
    parsed = urlsplit(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.username:
        credentials = parsed.username
        if parsed.password is not None:
            credentials = f"{credentials}:{parsed.password}"
        netloc = f"{credentials}@{netloc}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not name.casefold().startswith("utm_")
            and name.casefold() not in _TRACKING_QUERY_PARAMETERS
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, query, ""))


def discovered_job_fingerprint(job: DiscoveredJob) -> str:
    """Hash normalized content as the final discovered-job identity fallback."""
    content = "\n".join(
        _normalize_identity_text(value)
        for value in (job.company, job.title, job.detail)
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def deduplicate_discovered_jobs(jobs: list[DiscoveredJob]) -> list[DiscoveredJob]:
    """Keep the first input record for each URL, source ID, or content identity."""
    unique_jobs: list[DiscoveredJob] = []
    seen_urls: set[str] = set()
    seen_source_ids: set[tuple[str, str]] = set()
    seen_fingerprints: set[str] = set()
    for job in jobs:
        canonical_url = canonicalize_job_url(job.canonical_url)
        source_id = (job.source, job.source_job_id)
        fingerprint = discovered_job_fingerprint(job)
        if (
            canonical_url in seen_urls
            or source_id in seen_source_ids
            or fingerprint in seen_fingerprints
        ):
            continue
        seen_urls.add(canonical_url)
        seen_source_ids.add(source_id)
        seen_fingerprints.add(fingerprint)
        unique_jobs.append(job)
    return unique_jobs


def _normalize_identity_text(value: str) -> str:
    return " ".join(value.casefold().split())
