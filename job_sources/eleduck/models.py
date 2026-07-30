"""Source-local models for the public Eleduck RSS adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from job_sources.contracts import NormalizedJob


class PostCategory(str, Enum):
    RECRUITMENT = "recruitment"
    JOB_SEEKING = "job_seeking"
    OUTSOURCING_REQUEST = "outsourcing_request"
    PARTNER_SEEKING = "partner_seeking"
    ADVERTISEMENT = "advertisement"
    DISCUSSION = "discussion"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class FeedItem:
    guid: str | None
    canonical_url: str
    title: str
    description: str
    published_at: str | None
    pub_date_raw: str | None

    @property
    def identity(self) -> str:
        if self.guid:
            return f"guid:{self.guid}"
        return f"url:{self.canonical_url}"

    @property
    def source_job_id(self) -> str:
        return self.guid or self.canonical_url


@dataclass(frozen=True)
class Classification:
    category: PostCategory
    matched_rule_ids: tuple[str, ...]
    reason: str
    candidate_categories: tuple[PostCategory, ...] = ()


@dataclass(frozen=True)
class ClassifiedItem:
    item: FeedItem
    classification: Classification


@dataclass(frozen=True)
class EleduckCursor:
    seen_identities: tuple[str, ...] = ()


@dataclass(frozen=True)
class EleduckCollectionResult:
    accepted_jobs: tuple[NormalizedJob, ...] = ()
    review_items: tuple[ClassifiedItem, ...] = ()
    non_job_items: tuple[ClassifiedItem, ...] = ()
    next_cursor: EleduckCursor = field(default_factory=EleduckCursor)
    discovered_count: int = 0
    deduplicated_count: int = 0
    invalid_item_count: int = 0
    complete: bool = True
    stop_reason: str = "feed_end"
