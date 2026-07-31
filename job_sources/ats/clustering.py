"""Deterministic clustering for existing ATS detection results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .detector import AtsDetection, AtsName


ReviewStatus = Literal["confirmed", "candidate", "unknown", "blocked"]


@dataclass(frozen=True)
class AtsCluster:
    """One review-safe group of detection results."""

    review_status: ReviewStatus
    ats: AtsName
    version_hint: str | None
    members: tuple[str, ...]
    min_confidence: float
    max_confidence: float
    evidence: tuple[str, ...]
    supports_json_ld: bool
    requires_browser: bool
    blocked_reasons: tuple[str, ...]


def cluster_ats_detections(
    detections: Mapping[str, AtsDetection],
) -> tuple[AtsCluster, ...]:
    """Group detection results without inspecting or fetching their sources."""
    buckets: dict[
        tuple[ReviewStatus, AtsName, str | None],
        list[tuple[str, AtsDetection]],
    ] = {}
    for member, detection in detections.items():
        if detection.blocked_reason is not None:
            review_status: ReviewStatus = "blocked"
        elif detection.ats == "unknown" or detection.confidence < 0.5:
            review_status = "unknown"
        elif detection.confidence >= 0.9:
            review_status = "confirmed"
        else:
            review_status = "candidate"
        key = (review_status, detection.ats, detection.version_hint)
        buckets.setdefault(key, []).append((member, detection))

    status_order = {
        "confirmed": 0,
        "candidate": 1,
        "unknown": 2,
        "blocked": 3,
    }
    clusters = []
    for key in sorted(
        buckets,
        key=lambda item: (
            status_order[item[0]],
            item[1],
            item[2] is not None,
            item[2] or "",
        ),
    ):
        review_status, ats, version_hint = key
        items = buckets[key]
        confidence_values = [
            detection.confidence for _, detection in items
        ]
        clusters.append(
            AtsCluster(
                review_status=review_status,
                ats=ats,
                version_hint=version_hint,
                members=tuple(sorted(member for member, _ in items)),
                min_confidence=min(confidence_values),
                max_confidence=max(confidence_values),
                evidence=tuple(
                    sorted(
                        {
                            evidence
                            for _, detection in items
                            for evidence in detection.evidence
                        }
                    )
                ),
                supports_json_ld=any(
                    detection.supports_json_ld
                    for _, detection in items
                ),
                requires_browser=any(
                    detection.requires_browser
                    for _, detection in items
                ),
                blocked_reasons=tuple(
                    sorted(
                        {
                            detection.blocked_reason
                            for _, detection in items
                            if detection.blocked_reason is not None
                        }
                    )
                ),
            )
        )
    return tuple(clusters)
