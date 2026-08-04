"""Public job collection for Beisen portals hosted below zhiye.com."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from job_sources.contracts import NormalizedJob


_PAGE_SIZE = 50
_SCAN_LIMIT = 200
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_DISPLAY_FIELDS = (
    "Org",
    "Category",
    "LocId",
    "Salary",
    "Kind",
    "HeadCount",
    "Station",
    "PostDate",
    "EndTime",
    "Degree",
    "YearsOfWorking",
    "DetailAddress",
)
_CHANNELS = {
    "1": ("social", "social"),
    "2": ("campus", "campus"),
    "3": ("internship", "intern"),
}


class BeisenFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class BeisenCollection:
    jobs: tuple[NormalizedJob, ...]
    discovered_count: int
    scanned_count: int
    complete: bool


def collect_beisen_jobs(
    hostname: str,
    *,
    company: str | None,
    verified_at: str,
) -> BeisenCollection:
    """Read and normalize at most 200 public jobs from one validated host."""
    endpoint = f"https://{hostname}/api/Jobad/GetJobAdPageList"
    raw_jobs: list[object] = []
    discovered_count: int | None = None
    reached_upstream_end = False

    for page_index in range(_SCAN_LIMIT // _PAGE_SIZE):
        payload = _request_page(endpoint, hostname, page_index)
        count = payload.get("Count")
        data = payload.get("Data")
        if (
            payload.get("Code") != 200
            or isinstance(payload.get("Code"), bool)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(data, list)
        ):
            raise BeisenFetchError("collection_failed")
        if discovered_count is None:
            discovered_count = count
        elif count != discovered_count:
            raise BeisenFetchError("collection_failed")

        remaining = _SCAN_LIMIT - len(raw_jobs)
        raw_jobs.extend(data[:remaining])
        if len(data) < _PAGE_SIZE or len(raw_jobs) >= discovered_count:
            reached_upstream_end = True
            break

    normalized_jobs: list[NormalizedJob] = []
    for value in raw_jobs:
        try:
            normalized_jobs.append(
                _normalize_job(
                    hostname,
                    value,
                    company=company,
                    verified_at=verified_at,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            continue

    total = discovered_count if discovered_count is not None else 0
    return BeisenCollection(
        jobs=tuple(normalized_jobs),
        discovered_count=total,
        scanned_count=len(raw_jobs),
        complete=reached_upstream_end and total <= _SCAN_LIMIT,
    )


def _request_page(endpoint: str, hostname: str, page_index: int) -> dict:
    body = json.dumps(
        {
            "PageIndex": page_index,
            "PageSize": _PAGE_SIZE,
            "KeyWords": "",
            "SpecialType": 0,
            "DisplayFields": _DISPLAY_FIELDS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        request = Request(
            endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": f"https://{hostname}",
                "Referer": f"https://{hostname}/jobs",
                "X-Requested-With": "xmlhttprequest",
                "langType": "zh_CN",
            },
            method="POST",
        )
        opener = build_opener(_RejectRedirectHandler())
        with opener.open(request, timeout=10.0) as response:
            if response.status != 200:
                raise ValueError("unexpected status")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("unexpected content type")
            if response.geturl() != endpoint:
                raise ValueError("redirected response")
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(response_body) > _MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        payload = json.loads(response_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unexpected payload")
        return payload
    except BeisenFetchError:
        raise
    except Exception:
        raise BeisenFetchError("collection_failed") from None


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _normalize_job(
    hostname: str,
    value: object,
    *,
    company: str | None,
    verified_at: str,
) -> NormalizedJob:
    if not isinstance(value, dict):
        raise TypeError("invalid job")
    job_id = _job_id(value.get("JobAdId"))
    title = _required_text(value.get("JobAdName"), max_length=1_000)
    category_id = _optional_identifier(value.get("CategoryId"))
    category = _optional_text(value.get("Category"), max_length=500)
    channel, route = _channel_and_route(category_id, title, category)
    source_url = (
        f"https://{hostname}/{route}/detail?"
        f"{urlencode({'jobAdId': job_id})}"
    )
    duty = _optional_plain_text(value.get("Duty"))
    requirement = _optional_plain_text(value.get("Require"))
    description_parts = tuple(
        part for part in (duty, requirement) if part is not None
    )
    locations = _locations(value.get("LocNames"))

    return NormalizedJob(
        source="beisen",
        source_job_id=f"{hostname}:{job_id}",
        source_url=source_url,
        official_apply_url=source_url,
        title=title,
        company=company or hostname.split(".", 1)[0],
        locations=locations,
        recruitment_channel=channel,
        employment_type=_optional_text(value.get("Kind"), max_length=200),
        work_mode=None,
        description="\n\n".join(description_parts) or None,
        responsibilities=(duty,) if duty else (),
        requirements=(requirement,) if requirement else (),
        salary_text=_optional_text(value.get("Salary"), max_length=1_000),
        published_at=_timestamp(value.get("PostDate")),
        expires_at=_timestamp(value.get("EndTime")),
        last_verified_at=verified_at,
        campus={} if channel == "campus" else None,
        internship={} if channel == "internship" else None,
        raw_data={
            "organization": _optional_text(value.get("Org"), max_length=500),
            "category": category,
            "category_id": category_id,
            "head_count": _optional_text(value.get("HeadCount"), max_length=100),
            "station": _optional_text_or_list(
                value.get("Station"),
                max_length=500,
            ),
            "degree": _optional_text(value.get("Degree"), max_length=200),
            "years_of_working": _optional_text(
                value.get("YearsOfWorking"),
                max_length=200,
            ),
            "detail_address": _optional_text(
                value.get("DetailAddress"),
                max_length=1_000,
            ),
            "change_date": _timestamp(value.get("ChangeDate")),
        },
        upstream_platform="beisen",
    )


def _channel_and_route(
    category_id: str | None,
    *values: str | None,
) -> tuple[str, str]:
    if category_id in _CHANNELS:
        return _CHANNELS[category_id]
    text = " ".join(value for value in values if value).casefold()
    if re.search(r"\bintern(?:ship)?s?\b", text) or "实习" in text:
        return "internship", "intern"
    if (
        re.search(r"\b(?:graduate|campus|university|new[- ]grad)\b", text)
        or "校招" in text
        or "校园" in text
    ):
        return "campus", "campus"
    return "social", "social"


def _job_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("invalid job id")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("invalid job id")
    return normalized


def _optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("invalid identifier")
    normalized = str(value).strip()
    if len(normalized) > 100:
        raise ValueError("invalid identifier")
    return normalized or None


def _required_text(value: object, *, max_length: int) -> str:
    normalized = _optional_text(value, max_length=max_length)
    if normalized is None:
        raise TypeError("invalid text")
    return normalized


def _optional_text(value: object, *, max_length: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise TypeError("invalid text")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError("invalid text")
    return normalized or None


def _locations(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("invalid locations")
    locations: list[str] = []
    for item in value:
        location = _optional_text(item, max_length=500)
        if location and location not in locations:
            locations.append(location)
    return tuple(locations)


def _optional_text_or_list(
    value: object,
    *,
    max_length: int,
) -> str | tuple[str, ...] | None:
    if not isinstance(value, list):
        return _optional_text(value, max_length=max_length)
    items: list[str] = []
    for item in value:
        text = _optional_text(item, max_length=max_length)
        if text and text not in items:
            items.append(text)
    return tuple(items) or None


def _timestamp(value: object) -> str | None:
    text = _optional_text(value, max_length=100)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.year in {1, 2222}:
        return None
    return parsed.isoformat(timespec="seconds")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if text := data.strip():
            self.parts.append(text)


def _optional_plain_text(value: object) -> str | None:
    source = _optional_text(value, max_length=100_000)
    if source is None:
        return None
    parser = _TextExtractor()
    parser.feed(unescape(unescape(source)))
    normalized = " ".join(parser.parts).strip()
    return normalized or None


__all__ = ["BeisenCollection", "BeisenFetchError", "collect_beisen_jobs"]
