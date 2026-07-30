"""Orchestration for the isolated, read-only Eleduck RSS source."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from job_sources.contracts import NormalizedJob

from .classifier import classify
from .models import (
    ClassifiedItem,
    EleduckCollectionResult,
    EleduckCursor,
    FeedItem,
    PostCategory,
)
from .parser import parse_feed
from .rss import load_public_feed


MAX_CURSOR_IDENTITIES = 256


class EleduckRssSource:
    def __init__(
        self,
        *,
        load_feed: Callable[[], bytes] = load_public_feed,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._load_feed = load_feed
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(
        self,
        cursor: EleduckCursor | None = None,
    ) -> EleduckCollectionResult:
        current_cursor = cursor or EleduckCursor()
        parsed = parse_feed(self._load_feed())
        seen_before = set(current_cursor.seen_identities)
        seen_this_feed: set[str] = set()
        identities_in_feed_order: list[str] = []
        accepted_jobs: list[NormalizedJob] = []
        review_items: list[ClassifiedItem] = []
        non_job_items: list[ClassifiedItem] = []
        deduplicated_count = 0
        fetched_at = _utc_iso(self._clock())

        for item in parsed.items:
            if item.identity in seen_this_feed:
                deduplicated_count += 1
                continue
            seen_this_feed.add(item.identity)
            identities_in_feed_order.append(item.identity)
            if item.identity in seen_before:
                deduplicated_count += 1
                continue
            classification = classify(item)
            classified = ClassifiedItem(item, classification)
            if classification.category is PostCategory.RECRUITMENT:
                accepted_jobs.append(_to_normalized_job(item, classified, fetched_at))
            elif classification.category is PostCategory.UNCERTAIN:
                review_items.append(classified)
            else:
                non_job_items.append(classified)

        complete = parsed.invalid_item_count == 0
        if complete:
            ordered = tuple(identities_in_feed_order) + current_cursor.seen_identities
            next_cursor = EleduckCursor(
                tuple(dict.fromkeys(ordered))[:MAX_CURSOR_IDENTITIES]
            )
        else:
            next_cursor = current_cursor
        return EleduckCollectionResult(
            accepted_jobs=tuple(accepted_jobs),
            review_items=tuple(review_items),
            non_job_items=tuple(non_job_items),
            next_cursor=next_cursor,
            discovered_count=len(parsed.items),
            deduplicated_count=deduplicated_count,
            invalid_item_count=parsed.invalid_item_count,
            complete=complete,
            stop_reason="feed_end" if complete else "invalid_items",
        )


def _to_normalized_job(
    item: FeedItem,
    classified: ClassifiedItem,
    fetched_at: str,
) -> NormalizedJob:
    classification = classified.classification
    return NormalizedJob(
        source="eleduck",
        source_job_id=item.source_job_id,
        source_url=item.canonical_url,
        official_apply_url=None,
        title=item.title,
        company=None,
        locations=(),
        recruitment_channel="social",
        employment_type=None,
        work_mode=None,
        description=item.description or None,
        responsibilities=(),
        requirements=(),
        salary_text=None,
        published_at=item.published_at,
        expires_at=None,
        last_verified_at=fetched_at,
        campus=None,
        internship=None,
        raw_data={
            "guid": item.guid,
            "canonical_url": item.canonical_url,
            "pub_date_raw": item.pub_date_raw,
            "published_at": item.published_at,
            "title": item.title,
            "description": item.description,
            "category": classification.category.value,
            "matched_rule_ids": classification.matched_rule_ids,
            "classification_reason": classification.reason,
        },
        detail_status="partial",
        upstream_platform="eleduck",
    )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
