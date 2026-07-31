"""Bounded batch detection for caller-supplied ATS URLs."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
import json
from itertools import islice
from typing import Literal

from .clustering import AtsCluster, cluster_ats_detections
from .detector import AtsDetection
from .http import AtsHttpPolicy, detect_ats_url


@dataclass(frozen=True)
class AtsBatchItem:
    """One ordered batch result identified without exposing its URL."""

    member_id: str
    detection: AtsDetection


@dataclass(frozen=True)
class AtsBatchReport:
    """Ordered detections and their review-safe clusters."""

    items: tuple[AtsBatchItem, ...]
    clusters: tuple[AtsCluster, ...]


def detect_ats_urls(
    urls: Iterable[str],
    *,
    policy: AtsHttpPolicy = AtsHttpPolicy(),
    max_workers: int = 4,
) -> AtsBatchReport:
    """Detect an explicit URL list and cluster the ordered results."""
    ordered_urls = tuple(islice(urls, 101))
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or not 1 <= max_workers <= 8
    ):
        raise ValueError("max_workers must be between 1 and 8")
    if len(ordered_urls) > 100:
        raise ValueError("a batch accepts at most 100 URLs")
    unique_urls = tuple(dict.fromkeys(ordered_urls))
    detections_by_url: dict[str, AtsDetection] = {}
    if unique_urls:
        worker_count = min(max_workers, len(unique_urls))
        detect_one = partial(detect_ats_url, policy=policy)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            detections_by_url = dict(
                zip(unique_urls, executor.map(detect_one, unique_urls))
            )
    items = tuple(
        AtsBatchItem(
            member_id=f"item-{index:06d}",
            detection=detections_by_url[url],
        )
        for index, url in enumerate(ordered_urls, start=1)
    )
    clusters = cluster_ats_detections(
        {item.member_id: item.detection for item in items}
    )
    return AtsBatchReport(items=items, clusters=clusters)


def serialize_ats_batch_report(
    report: AtsBatchReport,
    *,
    format: Literal["json", "jsonl"] = "json",
) -> str:
    """Serialize a report without adding input URLs or transport details."""
    if format == "json":
        return _json_dumps(asdict(report))
    if format == "jsonl":
        records = (
            *(
                {
                    "record_type": "detection",
                    "payload": asdict(item),
                }
                for item in report.items
            ),
            *(
                {
                    "record_type": "cluster",
                    "payload": asdict(cluster),
                }
                for cluster in report.clusters
            ),
        )
        return "\n".join(_json_dumps(record) for record in records)
    raise ValueError("format must be 'json' or 'jsonl'")


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
