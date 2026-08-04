"""Bounded cross-tenant search over direct public ATS collectors."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from job_sources.contracts import NormalizedJob

from .discovery import AtsBoard
from .listings import AtsJobCollection, collect_ats_jobs


@dataclass(frozen=True)
class AtsBoardFailure:
    provider: str
    tenant: str
    reason: str


@dataclass(frozen=True)
class AtsJobSearchReport:
    jobs: tuple[NormalizedJob, ...]
    failures: tuple[AtsBoardFailure, ...]
    searched_boards: tuple[AtsBoard, ...]
    searched_count: int
    succeeded_count: int
    failed_count: int
    discovered_count: int
    matched_count: int


def search_ats_boards(
    boards: Iterable[AtsBoard],
    *,
    keyword: str,
    limit: int = 50,
    max_boards: int = 20,
    max_workers: int = 4,
    collector: Callable[..., AtsJobCollection] | None = None,
) -> AtsJobSearchReport:
    """Search public jobs from a bounded set of already-discovered boards."""
    normalized_keyword = keyword.strip() if isinstance(keyword, str) else ""
    if not normalized_keyword or len(normalized_keyword) > 100:
        raise ValueError("invalid_request")
    for value, maximum in ((limit, 50), (max_boards, 20), (max_workers, 4)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError("invalid_request")

    unique: dict[tuple[str, str], AtsBoard] = {}
    for board in boards:
        if not isinstance(board, AtsBoard):
            raise ValueError("invalid_request")
        unique.setdefault((board.provider, board.tenant.casefold()), board)
    selected = tuple(unique.values())[:max_boards]
    collect = collector or collect_ats_jobs
    successes: list[tuple[AtsBoard, AtsJobCollection]] = []
    failures: list[AtsBoardFailure] = []

    def collect_one(board: AtsBoard) -> AtsJobCollection:
        return collect(
            board.career_url,
            company=board.company,
            keyword=normalized_keyword,
            limit=limit,
            score=True,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {executor.submit(collect_one, board): board for board in selected}
        for future in as_completed(pending):
            board = pending[future]
            try:
                successes.append((board, future.result()))
            except Exception:
                failures.append(
                    AtsBoardFailure(board.provider, board.tenant, "collection_failed")
                )

    deduplicated: dict[str, NormalizedJob] = {}
    for _, collection in successes:
        for job in collection.jobs:
            identity = _canonical_job_identity(job)
            current = deduplicated.get(identity)
            if current is None or _job_sort_key(job) < _job_sort_key(current):
                deduplicated[identity] = job
    jobs = tuple(sorted(deduplicated.values(), key=_job_sort_key)[:limit])
    ordered_failures = tuple(
        sorted(failures, key=lambda failure: (failure.provider, failure.tenant.casefold()))
    )
    return AtsJobSearchReport(
        jobs=jobs,
        failures=ordered_failures,
        searched_boards=selected,
        searched_count=len(selected),
        succeeded_count=len(successes),
        failed_count=len(ordered_failures),
        discovered_count=sum(item.discovered_count for _, item in successes),
        matched_count=sum(item.matched_count for _, item in successes),
    )


def _canonical_job_identity(job: NormalizedJob) -> str:
    value = job.official_apply_url or job.source_url
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
        authority = hostname if port in {None, 443} else f"{hostname}:{port}"
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit((scheme, authority, path, parsed.query, ""))
    except (TypeError, ValueError):
        return value.casefold()


def _job_sort_key(job: NormalizedJob) -> tuple[float, str, str, str, str, str]:
    score = float(job.score) if job.score is not None else float("-inf")
    return (
        -score,
        (job.company or "").casefold(),
        job.title.casefold(),
        _canonical_job_identity(job),
        job.source.casefold(),
        job.source_job_id.casefold(),
    )


__all__ = [
    "AtsBoardFailure",
    "AtsJobSearchReport",
    "search_ats_boards",
]
