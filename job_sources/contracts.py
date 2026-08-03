"""Small source-level contracts shared by job-source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


RecruitmentChannel = Literal["internship", "campus", "social"]
CollectionMode = Literal["quick", "research"]
JobSort = Literal["recommended", "latest"]


@dataclass(frozen=True)
class JobQuery:
    keyword: str | None
    recruitment_channel: RecruitmentChannel
    city: str | None = None
    graduation_year: str | None = None
    sort: JobSort = "recommended"

    def __post_init__(self):
        keyword = (self.keyword or "").strip()
        if len(keyword) > 100:
            raise ValueError("job keyword cannot exceed 100 characters")
        if self.recruitment_channel not in {
            "internship",
            "campus",
            "social",
        }:
            raise ValueError(
                f"unsupported recruitment channel: "
                f"{self.recruitment_channel}"
            )
        if self.sort not in {"recommended", "latest"}:
            raise ValueError(f"unsupported job sort: {self.sort}")
        object.__setattr__(self, "keyword", keyword)
        object.__setattr__(self, "city", _optional_text(self.city, 100))
        object.__setattr__(
            self,
            "graduation_year",
            _optional_text(self.graduation_year, 50),
        )


@dataclass(frozen=True)
class NormalizedJob:
    source: str
    source_job_id: str
    source_url: str
    official_apply_url: str | None
    title: str
    company: str | None
    locations: tuple[str, ...]
    recruitment_channel: str
    employment_type: str | None
    work_mode: str | None
    description: str | None
    responsibilities: tuple[str, ...]
    requirements: tuple[str, ...]
    salary_text: str | None
    published_at: str | None
    expires_at: str | None
    last_verified_at: str
    campus: dict[str, Any] | None
    internship: dict[str, Any] | None
    raw_data: dict[str, Any]
    detail_status: Literal["complete", "partial"] = "complete"
    upstream_platform: str | None = None
    score: float | None = None
    qualified: bool | None = None


@dataclass(frozen=True)
class CollectionResult:
    source: str
    query: JobQuery
    jobs: tuple[NormalizedJob, ...]
    pages_read: int
    discovered_count: int
    deduplicated_count: int
    detail_success_count: int
    partial_detail_count: int
    detail_failure_count: int
    complete: bool
    stop_reason: str


@dataclass(frozen=True)
class SourceHealth:
    source: str
    success: bool
    fetched_count: int
    error_message: str | None
    checked_at: str


def _optional_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > max_length:
        raise ValueError(f"value cannot exceed {max_length} characters")
    return text
