"""Normalized access to Nowcoder's public school recruitment schedule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from .anonymous import AnonymousUpstreamError, NowcoderJsonTransport


SCHEDULE_PATH = "/np-api/u/school-schedule/list-card"
MAX_PAGE_SIZE = 20
MAX_URL_LENGTH = 2048
CHINA_TIMEZONE = timezone(timedelta(hours=8))
CampaignStatus = Literal["open", "closed", "unknown"]


@dataclass(frozen=True)
class RecruitmentCampaign:
    """A company recruitment batch, deliberately separate from a job."""

    source: str
    source_campaign_id: str
    source_url: str
    official_apply_url: str | None
    source_information_url: str | None
    company_id: str
    company: str
    batch_id: str | None
    batch_name: str | None
    locations: tuple[str, ...]
    career_categories: tuple[str, ...]
    industries: tuple[str, ...]
    summary: str | None
    application_start_date: str | None
    application_end_date: str | None
    schedule_text: str | None
    status: CampaignStatus
    last_updated_at: str | None


@dataclass(frozen=True)
class CampaignPage:
    campaigns: tuple[RecruitmentCampaign, ...]
    current_page: int
    total_pages: int
    total_count: int


class NowcoderCampaignApi:
    """Hides the volatile schedule payload behind a small paginated seam."""

    source = "nowcoder"

    def __init__(self, transport: NowcoderJsonTransport):
        self._transport = transport

    def list_page(
        self,
        *,
        keyword: str | None,
        city: str | None,
        career_job_id: str | None,
        page: int,
        page_size: int = MAX_PAGE_SIZE,
    ) -> CampaignPage:
        if page < 1:
            raise ValueError("campaign page must be positive")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(
                f"campaign page_size must be between 1 and {MAX_PAGE_SIZE}"
            )
        normalized_keyword = _text(keyword)
        normalized_city = _text(city)
        normalized_career_job_id = _identifier(career_job_id)
        if normalized_keyword is not None and len(normalized_keyword) > 100:
            raise ValueError("campaign keyword cannot exceed 100 characters")
        if normalized_city is not None and len(normalized_city) > 100:
            raise ValueError("campaign city cannot exceed 100 characters")
        if career_job_id is not None and normalized_career_job_id is None:
            raise ValueError("campaign career_job_id must contain digits only")

        body: dict[str, Any] = {
            "query": normalized_keyword or "",
            "propertyId": "",
            "page": page,
            "pageSize": page_size,
            "tab": 1,
        }
        if normalized_city is not None:
            body["cities"] = [normalized_city]
        if normalized_career_job_id is not None:
            body["careerJobId"] = normalized_career_job_id
        payload = self._transport.post(SCHEDULE_PATH, body)
        data = _success_data(payload)
        current_page = _non_negative_int(data.get("currentPage"))
        total_pages = _non_negative_int(data.get("totalPage"))
        total_count = _non_negative_int(data.get("totalCount"))
        records = data.get("datas")
        if (
            current_page is None
            or current_page < 1
            or total_pages is None
            or total_count is None
            or not isinstance(records, list)
        ):
            raise AnonymousUpstreamError(
                "Nowcoder campaign schedule changed shape"
            )
        if total_pages > 0 and current_page > total_pages:
            raise AnonymousUpstreamError(
                "Nowcoder campaign schedule returned invalid page bounds"
            )

        campaigns = tuple(
            campaign
            for record in records
            if isinstance(record, Mapping)
            if (campaign := _normalize_campaign(record)) is not None
        )
        if records and not campaigns:
            raise AnonymousUpstreamError(
                "Nowcoder campaign schedule contained no usable campaigns"
            )
        return CampaignPage(
            campaigns=campaigns,
            current_page=current_page,
            total_pages=total_pages,
            total_count=total_count,
        )


def _normalize_campaign(
    record: Mapping[str, Any],
) -> RecruitmentCampaign | None:
    company_id = _identifier(record.get("companyId"))
    company = _text(record.get("name"))
    if company_id is None or company is None:
        return None
    batch_id = _identifier(record.get("batch"))
    official_apply_url = _first_public_url(
        record.get("customWangshenLink"),
        _mapping_value(record.get("adInfo"), "rawUrl"),
    )
    source_information_url = _first_public_url(
        record.get("sourceInformation")
    )
    end = record.get("end")
    status: CampaignStatus = (
        "open" if end == 0 else "closed" if end == 1 else "unknown"
    )
    campaign_suffix = batch_id or "unknown"
    return RecruitmentCampaign(
        source="nowcoder",
        source_campaign_id=f"{company_id}:{campaign_suffix}",
        source_url=(
            f"https://www.nowcoder.com/enterprise/{company_id}"
            "?channel=recruitmentSchedule&pageSource=5014"
        ),
        official_apply_url=official_apply_url,
        source_information_url=source_information_url,
        company_id=company_id,
        company=company,
        batch_id=batch_id,
        batch_name=_text(record.get("batchName")),
        locations=_texts(record.get("cityList")),
        career_categories=_texts(record.get("careerNameList")),
        industries=_texts(record.get("industryList")),
        summary=_text(record.get("companyEvaluation")),
        application_start_date=_millis_to_date(
            record.get("wangshenBeginDate")
        ),
        application_end_date=_millis_to_date(record.get("wangshenEndDate")),
        schedule_text=_text(record.get("wangshenTime")),
        status=status,
        last_updated_at=_millis_to_iso(
            record.get("wangshenUpdateTime") or record.get("updateTime")
        ),
    )


def _success_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("code") != 0:
        raise AnonymousUpstreamError(
            "Nowcoder campaign schedule request was rejected"
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise AnonymousUpstreamError(
            "Nowcoder campaign schedule response contained no data"
        )
    return data


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _identifier(value: Any) -> str | None:
    text = _text(value)
    return text if text is not None and text.isdecimal() else None


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        if text is None or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _non_negative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _first_public_url(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text is None or len(text) > MAX_URL_LENGTH:
            continue
        try:
            parsed = urlsplit(text)
            _ = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        ):
            return text
    return None


def _millis_to_datetime(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=CHINA_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return None


def _millis_to_date(value: Any) -> str | None:
    converted = _millis_to_datetime(value)
    return converted.date().isoformat() if converted is not None else None


def _millis_to_iso(value: Any) -> str | None:
    converted = _millis_to_datetime(value)
    return converted.isoformat(timespec="seconds") if converted is not None else None
