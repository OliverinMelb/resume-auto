"""Bounded, atomic runtime state for the Eleduck source."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import (
    ClassifiedItem,
    Classification,
    EleduckCursor,
    FeedItem,
    PostCategory,
)
from .parser import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    EleduckSourceError,
    canonicalize_post_url,
)


SCHEMA_VERSION = 1
MAX_STATE_ITEMS = 256
MAX_CURSOR_ITEMS = 1_024
MAX_DECISIONS = 512
ReviewDecision = Literal["recruitment", "non_job"]


class EleduckStateError(EleduckSourceError):
    """Stored source state is invalid or could not be replaced safely."""


@dataclass(frozen=True)
class EleduckReviewDecision:
    identity: str
    decision: ReviewDecision
    decided_at: str
    item: ClassifiedItem
    candidate_id: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class EleduckSourceState:
    cursor: EleduckCursor = EleduckCursor()
    pending_reviews: tuple[ClassifiedItem, ...] = ()
    non_job_items: tuple[ClassifiedItem, ...] = ()
    decisions: tuple[EleduckReviewDecision, ...] = ()
    last_research_at: str | None = None


class EleduckStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> EleduckSourceState:
        if not self.path.exists():
            return EleduckSourceState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _decode_state(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EleduckStateError("Eleduck state is invalid") from exc

    def save(self, state: EleduckSourceState) -> None:
        payload = _encode_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise EleduckStateError("Eleduck state could not be saved") from exc


def _encode_state(state: EleduckSourceState) -> dict[str, object]:
    if len(state.cursor.seen_identities) > MAX_CURSOR_ITEMS:
        raise EleduckStateError("Eleduck state exceeds the item limit")
    if len(state.pending_reviews) > MAX_STATE_ITEMS:
        raise EleduckStateError("Eleduck state exceeds the item limit")
    if len(state.non_job_items) > MAX_STATE_ITEMS:
        raise EleduckStateError("Eleduck state exceeds the item limit")
    if len(state.decisions) > MAX_DECISIONS:
        raise EleduckStateError("Eleduck state exceeds the decision limit")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cursor": {"seen_identities": list(state.cursor.seen_identities)},
        "pending_reviews": [_encode_item(value) for value in state.pending_reviews],
        "non_job_items": [_encode_item(value) for value in state.non_job_items],
        "decisions": [_encode_decision(value) for value in state.decisions],
        "last_research_at": state.last_research_at,
    }
    _decode_state(payload)
    return payload


def _decode_state(payload: object) -> EleduckSourceState:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported state")
    _require_keys(
        payload,
        {
            "schema_version",
            "cursor",
            "pending_reviews",
            "non_job_items",
            "decisions",
            "last_research_at",
        },
    )
    cursor_payload = payload.get("cursor")
    if not isinstance(cursor_payload, dict):
        raise ValueError("invalid cursor")
    _require_keys(cursor_payload, {"seen_identities"})
    identities = cursor_payload.get("seen_identities")
    if not isinstance(identities, list) or len(identities) > MAX_CURSOR_ITEMS:
        raise ValueError("invalid cursor")
    if not all(
        isinstance(value, str) and 0 < len(value) <= 500 for value in identities
    ):
        raise ValueError("invalid cursor")
    pending = _decode_items(payload.get("pending_reviews"))
    non_jobs = _decode_items(payload.get("non_job_items"))
    decisions_payload = payload.get("decisions", [])
    if not isinstance(decisions_payload, list) or len(decisions_payload) > MAX_DECISIONS:
        raise ValueError("invalid decisions")
    decisions = tuple(_decode_decision(value) for value in decisions_payload)
    if len({value.identity for value in decisions}) != len(decisions):
        raise ValueError("duplicate decisions")
    last_research_at = payload.get("last_research_at")
    if last_research_at is not None and (
        not isinstance(last_research_at, str) or len(last_research_at) > 100
    ):
        raise ValueError("invalid timestamp")
    return EleduckSourceState(
        cursor=EleduckCursor(tuple(identities)),
        pending_reviews=pending,
        non_job_items=non_jobs,
        decisions=decisions,
        last_research_at=last_research_at,
    )


def _decode_items(value: object) -> tuple[ClassifiedItem, ...]:
    if not isinstance(value, list) or len(value) > MAX_STATE_ITEMS:
        raise ValueError("invalid items")
    items = tuple(_decode_item(entry) for entry in value)
    if len({entry.item.identity for entry in items}) != len(items):
        raise ValueError("duplicate identities")
    return items


def _encode_item(value: ClassifiedItem) -> dict[str, object]:
    item = value.item
    classification = value.classification
    return {
        "identity": item.identity,
        "guid": item.guid,
        "canonical_url": item.canonical_url,
        "title": item.title,
        "description": item.description,
        "published_at": item.published_at,
        "pub_date_raw": item.pub_date_raw,
        "category": classification.category.value,
        "matched_rule_ids": list(classification.matched_rule_ids),
        "reason": classification.reason,
        "candidate_categories": [
            category.value for category in classification.candidate_categories
        ],
    }


def _encode_decision(value: EleduckReviewDecision) -> dict[str, object]:
    return {
        "identity": value.identity,
        "decision": value.decision,
        "decided_at": value.decided_at,
        "item": _encode_item(value.item),
        "candidate_id": value.candidate_id,
        "job_id": value.job_id,
    }


def _decode_decision(value: object) -> EleduckReviewDecision:
    if not isinstance(value, dict):
        raise ValueError("invalid decision")
    _require_keys(
        value,
        {"identity", "decision", "decided_at", "item", "candidate_id", "job_id"},
    )
    decision = _required_string(value.get("decision"))
    if decision not in {"recruitment", "non_job"}:
        raise ValueError("invalid decision")
    item = _decode_item(value.get("item"))
    identity = _required_string(value.get("identity"))
    if identity != item.item.identity:
        raise ValueError("decision identity mismatch")
    return EleduckReviewDecision(
        identity=_required_string(identity, max_length=500),
        decision=decision,
        decided_at=_required_string(value.get("decided_at"), max_length=100),
        item=item,
        candidate_id=_optional_string(value.get("candidate_id"), max_length=200),
        job_id=_optional_string(value.get("job_id"), max_length=200),
    )


def _decode_item(value: object) -> ClassifiedItem:
    if not isinstance(value, dict):
        raise ValueError("invalid item")
    _require_keys(
        value,
        {
            "identity",
            "guid",
            "canonical_url",
            "title",
            "description",
            "published_at",
            "pub_date_raw",
            "category",
            "matched_rule_ids",
            "reason",
            "candidate_categories",
        },
    )
    item = FeedItem(
        guid=_optional_string(value.get("guid"), max_length=300),
        canonical_url=canonicalize_post_url(
            _required_string(value.get("canonical_url"), max_length=500)
        ),
        title=_required_string(
            value.get("title"), allow_empty=True, max_length=MAX_TITLE_CHARS
        ),
        description=_required_string(
            value.get("description"),
            allow_empty=True,
            max_length=MAX_DESCRIPTION_CHARS,
        ),
        published_at=_optional_string(value.get("published_at"), max_length=100),
        pub_date_raw=_optional_string(value.get("pub_date_raw"), max_length=120),
    )
    if value.get("identity") != item.identity:
        raise ValueError("identity mismatch")
    rules = value.get("matched_rule_ids")
    candidates = value.get("candidate_categories", [])
    if not isinstance(rules, list) or len(rules) > 64 or not all(
        isinstance(rule, str) and 0 < len(rule) <= 100 for rule in rules
    ):
        raise ValueError("invalid rules")
    if not isinstance(candidates, list):
        raise ValueError("invalid candidates")
    if len(candidates) > len(PostCategory):
        raise ValueError("too many candidate categories")
    classification = Classification(
        category=PostCategory(_required_string(value.get("category"))),
        matched_rule_ids=tuple(rules),
        reason=_required_string(
            value.get("reason"), allow_empty=True, max_length=500
        ),
        candidate_categories=tuple(PostCategory(category) for category in candidates),
    )
    return ClassifiedItem(item=item, classification=classification)


def _required_string(
    value: object,
    *,
    allow_empty: bool = False,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("invalid text")
    if max_length is not None and len(value) > max_length:
        raise ValueError("text exceeds limit")
    return value


def _optional_string(value: object, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    return _required_string(value, max_length=max_length)


def _require_keys(value: dict[object, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("unexpected state fields")
