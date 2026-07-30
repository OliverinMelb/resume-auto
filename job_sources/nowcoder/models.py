"""Source-local models for Nowcoder browser collection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from job_sources.contracts import NormalizedJob

    from .source import JobReference


@dataclass(frozen=True)
class BrowserSearchResult:
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
    browser_complete: bool
    browser_stop_reason: str
    failed_detail_ids: tuple[str, ...] = ()
