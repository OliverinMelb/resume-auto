import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from .research import analyze_jobs


MONTHLY_K_PATTERN = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*[-~—至]\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*[kK]"
    r"(?:\s*[·x×*]\s*(?P<months>\d+)\s*薪)?"
)
ANNUAL_WAN_PATTERN = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*[-~—至]\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*万\s*/?\s*年"
)
DAILY_PATTERN = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*[-~—至]\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*元\s*/?\s*天"
)


def parse_salary(salary: str) -> dict[str, Any] | None:
    normalized = salary.replace(",", "").strip()
    monthly_match = MONTHLY_K_PATTERN.search(normalized)
    if monthly_match:
        low = float(monthly_match.group("low")) * 1000
        high = float(monthly_match.group("high")) * 1000
        months = int(monthly_match.group("months") or 12)
        return {
            "kind": "monthly",
            "monthly_low_cny": low,
            "monthly_high_cny": high,
            "monthly_mid_cny": (low + high) / 2,
            "annual_low_cny": low * months,
            "annual_high_cny": high * months,
            "annual_mid_cny": (low + high) / 2 * months,
            "salary_months": months,
        }
    annual_match = ANNUAL_WAN_PATTERN.search(normalized)
    if annual_match:
        low = float(annual_match.group("low")) * 10_000
        high = float(annual_match.group("high")) * 10_000
        return {
            "kind": "annual",
            "annual_low_cny": low,
            "annual_high_cny": high,
            "annual_mid_cny": (low + high) / 2,
        }
    daily_match = DAILY_PATTERN.search(normalized)
    if daily_match:
        low = float(daily_match.group("low"))
        high = float(daily_match.group("high"))
        return {
            "kind": "daily",
            "daily_low_cny": low,
            "daily_high_cny": high,
            "daily_mid_cny": (low + high) / 2,
        }
    return None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return round(
        ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight,
        2,
    )


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "sample_count": len(values),
        "min": round(min(values), 2) if values else None,
        "p25": percentile(values, 0.25),
        "median": round(median(values), 2) if values else None,
        "p75": percentile(values, 0.75),
        "max": round(max(values), 2) if values else None,
    }


def build_market_report(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    parsed_records = []
    unparsed = []
    by_keyword: dict[str, list[float]] = defaultdict(list)
    for job in jobs:
        parsed = parse_salary(job.get("salary") or "")
        if not parsed:
            unparsed.append(
                {"job_id": job["id"], "salary": job.get("salary") or ""}
            )
            continue
        record = {"job_id": job["id"], "salary": job["salary"], **parsed}
        parsed_records.append(record)
        if parsed["kind"] == "monthly":
            by_keyword[job.get("search_keyword") or "unknown"].append(
                parsed["monthly_mid_cny"]
            )

    monthly_midpoints = [
        item["monthly_mid_cny"]
        for item in parsed_records
        if item["kind"] == "monthly"
    ]
    annual_midpoints = [
        item["annual_mid_cny"]
        for item in parsed_records
        if item["kind"] in {"monthly", "annual"}
    ]
    confidence = (
        "high"
        if len(monthly_midpoints) >= 30
        else "medium"
        if len(monthly_midpoints) >= 10
        else "low"
    )
    search_keyword_counts = Counter(
        job.get("search_keyword") or "unknown" for job in jobs
    )
    return {
        "status": "ready",
        "sample": {
            "job_count": len(jobs),
            "parseable_salary_count": len(parsed_records),
            "unparsed_salary_count": len(unparsed),
            "search_keyword_counts": dict(search_keyword_counts),
        },
        "observed_monthly_midpoint_cny": _distribution(monthly_midpoints),
        "observed_annual_midpoint_cny": _distribution(annual_midpoints),
        "estimated_market_monthly_cny": {
            "low": percentile(monthly_midpoints, 0.25),
            "center": round(median(monthly_midpoints), 2)
            if monthly_midpoints
            else None,
            "high": percentile(monthly_midpoints, 0.75),
            "confidence": confidence,
            "method": "qualified JD observed salary midpoint P25/P50/P75",
        },
        "salary_by_search_keyword": {
            keyword: _distribution(values)
            for keyword, values in sorted(by_keyword.items())
        },
        "requirement_keywords": analyze_jobs(jobs),
        "parsed_salaries": parsed_records,
        "unparsed_salaries": unparsed,
        "limitations": [
            "结果来自当前Boss搜索关键词、地区和筛选条件下的可见样本，不代表完整市场。",
            "历史日志没有searchKeyword时归入unknown。",
            "薪资统计是样本分布估计，不是模型承诺或Offer预测。",
        ],
    }
