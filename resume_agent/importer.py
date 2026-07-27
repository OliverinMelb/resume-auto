import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .repository import Repository


def parse_raw_job(raw_job: str) -> tuple[str, str, str]:
    sections = [
        section.strip()
        for section in re.split(r"\n\s*\n", raw_job)
        if section.strip()
    ]
    values: dict[str, str] = {}
    for section in sections:
        lines = section.splitlines()
        if not lines:
            continue
        heading = lines[0].strip().lstrip("#").strip()
        value = "\n".join(lines[1:]).strip()
        values[heading] = value
    title = values.get("职位名称", "")
    salary = values.get("薪资范围", "")
    detail = values.get("职位描述", "")
    return title, salary, detail


def _within_date_range(
    logged_at: str | None, date_from: str | None, date_to: str | None
) -> bool:
    if not logged_at:
        return date_from is None and date_to is None
    comparable = logged_at[:19]
    lower = (
        f"{date_from}T00:00:00" if date_from and len(date_from) == 10 else date_from
    )
    upper = f"{date_to}T23:59:59" if date_to and len(date_to) == 10 else date_to
    if lower and comparable < lower[:19]:
        return False
    if upper and comparable > upper[:19]:
        return False
    return True


def import_job_decisions(
    repository: Repository,
    decision_log_path: Path,
    candidate_id: str,
    qualified_only: bool,
    threshold: float,
    date_from: str | None,
    date_to: str | None,
    search_keywords: list[str],
) -> dict[str, Any]:
    repository.get_candidate(candidate_id)
    counters = {
        "lines": 0,
        "malformed": 0,
        "missing_job_content": 0,
        "outside_filters": 0,
        "below_threshold": 0,
        "duplicates": 0,
        "imported": 0,
    }
    imported_jobs: list[dict[str, Any]] = []
    if not decision_log_path.exists():
        return {
            "status": "decision_log_not_found",
            "path": str(decision_log_path),
            "counters": counters,
            "jobs": [],
        }

    allowed_keywords = set(search_keywords)
    with decision_log_path.open("r", encoding="utf-8") as file:
        for line in file:
            counters["lines"] += 1
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                counters["malformed"] += 1
                continue

            logged_at = record.get("loggedAt")
            search_keyword = record.get("searchKeyword")
            if not _within_date_range(logged_at, date_from, date_to):
                counters["outside_filters"] += 1
                continue
            if allowed_keywords and search_keyword not in allowed_keywords:
                counters["outside_filters"] += 1
                continue

            raw_job = record.get("rawJob")
            if not isinstance(raw_job, str) or not raw_job.strip():
                counters["missing_job_content"] += 1
                continue
            title, salary, detail = parse_raw_job(raw_job)
            title = title or str(record.get("title") or "").strip()
            detail = detail or str(record.get("detail") or "").strip()
            if not title or not detail:
                counters["missing_job_content"] += 1
                continue

            score = float(record.get("score") or 0)
            recorded_qualified = record.get("qualified")
            qualified = (
                recorded_qualified
                if isinstance(recorded_qualified, bool)
                else score >= threshold
            )
            if qualified_only and not qualified:
                counters["below_threshold"] += 1
                continue

            fingerprint = record.get("jobFingerprint") or hashlib.sha256(
                raw_job.strip().encode("utf-8")
            ).hexdigest()
            if repository.find_job_by_fingerprint(candidate_id, fingerprint):
                counters["duplicates"] += 1
                continue

            job = repository.create_job(
                {
                    "candidate_id": candidate_id,
                    "source": "job_decisions_jsonl",
                    "title": title,
                    "salary": salary,
                    "detail": detail,
                    "company": "",
                    "score": score,
                    "qualified": qualified,
                    "search_keyword": search_keyword,
                    "job_fingerprint": fingerprint,
                    "source_logged_at": logged_at,
                }
            )
            imported_jobs.append(job)
            counters["imported"] += 1

    return {
        "status": "imported",
        "path": str(decision_log_path),
        "counters": counters,
        "jobs": imported_jobs,
    }
