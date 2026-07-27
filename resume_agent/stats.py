import json
from collections import Counter
from pathlib import Path
from typing import Any


LEGACY_ACTION_MAP = {
    "job_decision_consumed": "evaluated",
    "job_below_threshold": "rejected_by_rule",
    "chat_rejected_below_threshold": "rejected_by_rule",
    "job_skip": "skipped",
    "job_already_talked": "already_contacted",
    "greet_queued": "greet_queued",
    "greet_sent": "greet_sent",
    "chat_greet_sent": "greet_sent",
    "greet_failed": "greet_failed",
    "greet_queue_failed": "greet_failed",
    "chat_greet_failed": "greet_failed",
    "resume_sent": "resume_sent",
}


def _within_range(
    value: str | None, date_from: str | None, date_to: str | None
) -> bool:
    if not value:
        return True
    comparable = value[:19]
    lower = (
        f"{date_from}T00:00:00" if date_from and len(date_from) == 10 else date_from
    )
    upper = f"{date_to}T23:59:59" if date_to and len(date_to) == 10 else date_to
    if lower and comparable < lower[:19]:
        return False
    if upper and comparable > upper[:19]:
        return False
    return True


def read_legacy_actions(
    path: Path, date_from: str | None = None, date_to: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    events = []
    malformed = 0
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                malformed += 1
                continue
            normalized = LEGACY_ACTION_MAP.get(record.get("action"))
            if not normalized:
                continue
            logged_at = record.get("loggedAt")
            if not _within_range(logged_at, date_from, date_to):
                continue
            events.append(
                {
                    "event_type": normalized,
                    "event_at": logged_at,
                    "source": "legacy_job_actions",
                    "title": record.get("title"),
                }
            )
    return events, malformed


def build_stats(
    manual_events: list[dict[str, Any]],
    legacy_events: list[dict[str, Any]],
    malformed_legacy_lines: int,
) -> dict[str, Any]:
    manual_counts = Counter(event["event_type"] for event in manual_events)
    legacy_counts = Counter(event["event_type"] for event in legacy_events)
    combined = manual_counts + legacy_counts

    evaluated = legacy_counts["evaluated"]
    greeted = legacy_counts["greet_sent"]
    inferred_replies = legacy_counts["resume_sent"]
    explicit_replies = manual_counts["recruiter_replied"]
    applications = manual_counts["application_submitted"] + greeted
    interviews = manual_counts["interview"]
    offers = manual_counts["offer"]
    reply_signal = max(explicit_replies, inferred_replies)

    def rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "counts": dict(sorted(combined.items())),
        "legacy_counts": dict(sorted(legacy_counts.items())),
        "manual_counts": dict(sorted(manual_counts.items())),
        "funnel": {
            "evaluated": evaluated,
            "contacted_or_submitted": applications,
            "explicit_recruiter_replies": explicit_replies,
            "inferred_recruiter_replies_from_resume_sent": inferred_replies,
            "interviews": interviews,
            "offers": offers,
        },
        "conversion_rates": {
            "contacted_per_evaluated": rate(applications, evaluated),
            "reply_per_contacted": rate(reply_signal, applications),
            "interview_per_reply": rate(interviews, reply_signal),
            "offer_per_interview": rate(offers, interviews),
        },
        "data_quality": {
            "malformed_legacy_lines": malformed_legacy_lines,
            "legacy_events_have_candidate_id": False,
            "legacy_reply_count_is_inferred": True,
            "counts_are_events_not_deduplicated_jobs": True,
        },
    }
