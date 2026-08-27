"""Deterministic eligibility filtering and representative JD selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

from resume_agent.discovery_schemas import (
    DiscoveredJob,
    discovered_job_fingerprint,
)
from resume_agent.job_family import known_job_family


RejectionReason = Literal[
    "partial_detail",
    "cross_family",
    "unknown_family",
]


@dataclass(frozen=True)
class RejectedJob:
    job: DiscoveredJob
    reason: RejectionReason


_AI_TITLE_PATTERNS = (
    re.compile(r"(?<![a-z0-9])ai(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])agent(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])rag(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])llm(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])workflow(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"artificial\s+intelligence", re.IGNORECASE),
    re.compile(r"智能体|大模型|人工智能|机器学习|深度学习|自然语言|工作流", re.IGNORECASE),
)
_EXPLICIT_DETAIL_PATTERNS = (
    re.compile(r"(?<![a-z0-9])agent(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])rag(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])llm(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])workflow(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"智能体|大模型|工作流", re.IGNORECASE),
)
def filter_complete_agent_jobs(
    jobs: Sequence[DiscoveredJob],
) -> tuple[list[DiscoveredJob], list[RejectedJob]]:
    """Partition discovered jobs into eligible Agent-family JDs and safe reasons."""
    if isinstance(jobs, (str, bytes)) or any(
        not isinstance(item, DiscoveredJob) for item in jobs
    ):
        raise ValueError("invalid discovered jobs")

    eligible: list[DiscoveredJob] = []
    rejected: list[RejectedJob] = []
    for item in jobs:
        if item.detail_status != "complete":
            rejected.append(RejectedJob(item, "partial_detail"))
            continue
        family = known_job_family(item.title)
        if family is not None and family != "ai_engineering":
            rejected.append(RejectedJob(item, "cross_family"))
            continue
        if _matches(_AI_TITLE_PATTERNS, item.title):
            eligible.append(item)
            continue
        if _matches(_EXPLICIT_DETAIL_PATTERNS, item.detail):
            eligible.append(item)
            continue
        rejected.append(RejectedJob(item, "unknown_family"))
    return eligible, rejected


def select_representative_jobs(
    jobs: Sequence[DiscoveredJob],
    *,
    minimum: int = 3,
    maximum: int = 20,
) -> tuple[list[DiscoveredJob], list[RejectedJob]]:
    """Greedily select a deterministic, source- and company-diverse JD set."""
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or not 3 <= minimum <= maximum <= 20
    ):
        raise ValueError("invalid selection bounds; require 3 <= minimum <= maximum <= 20")

    eligible, rejected = filter_complete_agent_jobs(jobs)
    remaining = list(eligible)
    selected: list[DiscoveredJob] = []
    seen_sources: set[str] = set()
    seen_companies: set[str] = set()
    while remaining and len(selected) < maximum:
        chosen = min(
            remaining,
            key=lambda item: _selection_key(
                item,
                seen_sources=seen_sources,
                seen_companies=seen_companies,
            ),
        )
        remaining.remove(chosen)
        selected.append(chosen)
        seen_sources.add(chosen.source)
        normalized_company = _normalize_company(chosen.company)
        if normalized_company:
            seen_companies.add(normalized_company)
    return selected, rejected


def _selection_key(
    job: DiscoveredJob,
    *,
    seen_sources: set[str],
    seen_companies: set[str],
) -> tuple:
    company = _normalize_company(job.company)
    return (
        0 if job.detail_status == "complete" else 1,
        0 if job.qualified is True else 1,
        0 if job.source not in seen_sources else 1,
        0 if company and company not in seen_companies else 1,
        -job.verified_at.timestamp(),
        -len(job.detail),
        discovered_job_fingerprint(job),
        job.source,
        job.source_job_id,
        job.canonical_url,
    )


def _matches(patterns: tuple[re.Pattern[str], ...], value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in patterns)


def _normalize_company(value: str) -> str:
    return " ".join(value.casefold().split())
