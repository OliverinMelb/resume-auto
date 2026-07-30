"""Source-independent projection into the project's existing JD evaluator."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from config import Config
from core import evaluateSingleRouteDelivery

from .contracts import NormalizedJob


EvaluationResult = Mapping[str, object]
JobEvaluator = Callable[[str], EvaluationResult]


def build_job_evaluation_text(job: NormalizedJob) -> str:
    """Keep the exact three-section shape consumed by the legacy evaluator."""
    description = job.description or "\n".join(
        (*job.responsibilities, *job.requirements)
    )
    return "\n\n".join(
        (
            f"# 职位名称\n{job.title}",
            f"# 薪资范围\n{job.salary_text or '未提供'}",
            f"# 职位描述\n{description or '未提供'}",
        )
    )


def evaluate_normalized_job(
    job: NormalizedJob,
    *,
    evaluator: JobEvaluator = evaluateSingleRouteDelivery,
    threshold: float | None = None,
) -> NormalizedJob:
    """Attach ranking metadata without filtering the normalized job."""
    result = evaluator(build_job_evaluation_text(job))
    score_value = result.get("score")
    if isinstance(score_value, bool) or not isinstance(
        score_value,
        (int, float),
    ):
        raise ValueError("existing JD evaluator returned an invalid score")
    score = float(score_value)
    effective_threshold = (
        float(Config.frontend.get("thread", 50))
        if threshold is None
        else threshold
    )
    return replace(
        job,
        score=score,
        qualified=score >= effective_threshold,
    )
