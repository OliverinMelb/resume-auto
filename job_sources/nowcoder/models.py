"""Source-local models for Nowcoder collection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from job_sources.contracts import NormalizedJob

    from .source import JobReference


NowcoderAcquisitionMode = Literal["public", "browser"]


@dataclass(frozen=True)
class NowcoderSearchResult:
    references: tuple[JobReference, ...]
    pages_read: int
    discovered_count: int
    complete: bool
    stop_reason: str


@dataclass(frozen=True)
class NowcoderCollectionProgress:
    references: tuple[JobReference, ...]
    jobs: tuple[NormalizedJob, ...]
    pages_read: int
    discovered_count: int
    search_complete: bool
    search_stop_reason: str
    failed_detail_ids: tuple[str, ...] = ()
