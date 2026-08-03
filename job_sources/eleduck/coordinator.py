"""Deep module coordinating Eleduck fetch, scoring, persistence, and state."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from job_sources.contracts import NormalizedJob
from job_sources.evaluation import evaluate_normalized_job

from .models import ClassifiedItem, EleduckCollectionResult, EleduckCursor
from .parser import EleduckSourceError
from .research import PersistedEleduckJob, eleduck_research_detail
from .source import normalized_job_from_classified
from .state import (
    EleduckReviewDecision,
    EleduckSourceState,
    EleduckStateError,
    EleduckStateStore,
    MAX_DECISIONS,
    MAX_STATE_ITEMS,
    ReviewDecision,
)


class EleduckIncompleteResult(EleduckSourceError):
    """A partial feed must never advance state or write research jobs."""


class EleduckReviewNotFound(LookupError):
    pass


class EleduckReviewConflict(ValueError):
    pass


class _ResearchSink(Protocol):
    def resolve_project(
        self,
        *,
        candidate_id: str | None,
        project_name: str | None,
        default_name: str,
        jobs: tuple[NormalizedJob, ...],
    ) -> str: ...

    def persist(
        self,
        candidate_id: str,
        jobs: tuple[NormalizedJob, ...],
    ) -> tuple[PersistedEleduckJob, ...]: ...


class _EleduckSource(Protocol):
    def collect(
        self,
        cursor: EleduckCursor | None = None,
    ) -> EleduckCollectionResult: ...


@dataclass(frozen=True)
class EleduckResearchOutcome:
    candidate_id: str | None
    job_ids: tuple[str, ...]
    accepted_count: int
    inserted_count: int
    deduplicated_count: int
    pending_review_count: int
    non_job_count: int
    cursor_size: int
    last_research_at: str


@dataclass(frozen=True)
class EleduckReviewQueue:
    pending: tuple[ClassifiedItem, ...]
    confirmed_recruitment: tuple[EleduckReviewDecision, ...]
    ignored_non_job: tuple[EleduckReviewDecision, ...]


class EleduckCoordinator:
    def __init__(
        self,
        *,
        source: _EleduckSource,
        store: EleduckStateStore,
        sink: _ResearchSink,
        evaluator: Callable[[NormalizedJob], NormalizedJob] = evaluate_normalized_job,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self._sink = sink
        self._evaluator = evaluator
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._write_lock = threading.Lock()

    def preview(self) -> EleduckCollectionResult:
        return self._source.collect()

    def research(
        self,
        *,
        candidate_id: str | None,
        project_name: str | None,
    ) -> EleduckResearchOutcome:
        with self._write_lock:
            state = self._store.load()
            result = self._source.collect(state.cursor)
            if not result.complete:
                raise EleduckIncompleteResult(
                    "Eleduck feed was incomplete; no research data was saved"
                )
            evaluated = tuple(self._evaluator(job) for job in result.accepted_jobs)
            persistable = tuple(
                job for job in evaluated if eleduck_research_detail(job)
            )
            resolved_candidate_id: str | None = None
            persisted: tuple[PersistedEleduckJob, ...] = ()
            if persistable:
                resolved_candidate_id = self._sink.resolve_project(
                    candidate_id=candidate_id,
                    project_name=project_name,
                    default_name=f"电鸭 JD 调研：{self._local_date()}",
                    jobs=persistable,
                )
                persisted = self._sink.persist(resolved_candidate_id, persistable)
            researched_at = _utc_iso(self._clock())
            decided_identities = {value.identity for value in state.decisions}
            pending = _merge_items(
                state.pending_reviews,
                result.review_items,
                excluded=decided_identities,
            )
            non_jobs = _merge_items(state.non_job_items, result.non_job_items)
            self._store.save(
                EleduckSourceState(
                    cursor=result.next_cursor,
                    pending_reviews=pending,
                    non_job_items=non_jobs,
                    decisions=state.decisions,
                    last_research_at=researched_at,
                )
            )
            return EleduckResearchOutcome(
                candidate_id=resolved_candidate_id,
                job_ids=tuple(value.job_id for value in persisted),
                accepted_count=len(evaluated),
                inserted_count=sum(value.created for value in persisted),
                deduplicated_count=sum(not value.created for value in persisted),
                pending_review_count=len(pending),
                non_job_count=len(non_jobs),
                cursor_size=len(result.next_cursor.seen_identities),
                last_research_at=researched_at,
            )

    def list_reviews(self) -> EleduckReviewQueue:
        state = self._store.load()
        return EleduckReviewQueue(
            pending=state.pending_reviews,
            confirmed_recruitment=tuple(
                value for value in state.decisions if value.decision == "recruitment"
            ),
            ignored_non_job=tuple(
                value for value in state.decisions if value.decision == "non_job"
            ),
        )

    def decide_review(
        self,
        identity: str,
        *,
        decision: ReviewDecision,
        candidate_id: str | None,
        project_name: str | None,
    ) -> EleduckReviewDecision:
        if decision not in {"recruitment", "non_job"}:
            raise ValueError("unsupported Eleduck review decision")
        with self._write_lock:
            state = self._store.load()
            existing = next(
                (value for value in state.decisions if value.identity == identity),
                None,
            )
            if existing is not None:
                if existing.decision == decision:
                    return existing
                raise EleduckReviewConflict(
                    "Eleduck review already has the opposite decision"
                )
            item = next(
                (value for value in state.pending_reviews if value.item.identity == identity),
                None,
            )
            if item is None:
                refreshed = self._source.collect()
                item = next(
                    (value for value in refreshed.review_items if value.item.identity == identity),
                    None,
                )
            if item is None:
                raise EleduckReviewNotFound("Eleduck review identity was not found")
            if len(state.decisions) >= MAX_DECISIONS:
                raise EleduckStateError("Eleduck review decision limit was reached")

            decided_at = _utc_iso(self._clock())
            resolved_candidate_id: str | None = None
            job_id: str | None = None
            if decision == "recruitment":
                job = normalized_job_from_classified(
                    item,
                    decided_at,
                    manual_review_decision=True,
                )
                evaluated = self._evaluator(job)
                if eleduck_research_detail(evaluated):
                    resolved_candidate_id = self._sink.resolve_project(
                        candidate_id=candidate_id,
                        project_name=project_name,
                        default_name=f"电鸭 JD 调研：{self._local_date()}",
                        jobs=(evaluated,),
                    )
                    persisted = self._sink.persist(
                        resolved_candidate_id,
                        (evaluated,),
                    )
                    if persisted:
                        job_id = persisted[0].job_id
            record = EleduckReviewDecision(
                identity=identity,
                decision=decision,
                decided_at=decided_at,
                item=item,
                candidate_id=resolved_candidate_id,
                job_id=job_id,
            )
            decisions = (*state.decisions, record)[-MAX_DECISIONS:]
            pending = tuple(
                value for value in state.pending_reviews if value.item.identity != identity
            )
            self._store.save(
                EleduckSourceState(
                    cursor=state.cursor,
                    pending_reviews=pending,
                    non_job_items=state.non_job_items,
                    decisions=decisions,
                    last_research_at=state.last_research_at,
                )
            )
            return record

    def _local_date(self) -> str:
        return self._clock().astimezone().date().isoformat()


def _merge_items(
    existing: tuple[ClassifiedItem, ...],
    current: tuple[ClassifiedItem, ...],
    *,
    excluded: set[str] | None = None,
) -> tuple[ClassifiedItem, ...]:
    excluded = excluded or set()
    merged = {value.item.identity: value for value in existing}
    for value in current:
        merged[value.item.identity] = value
    return tuple(
        value for identity, value in merged.items() if identity not in excluded
    )[-MAX_STATE_ITEMS:]


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
