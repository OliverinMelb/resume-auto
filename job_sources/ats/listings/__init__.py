"""Bounded public-job collection for supported ATS career sites."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from job_sources.contracts import NormalizedJob
from job_sources.evaluation import evaluate_normalized_job

from ..discovery import AtsProvider
from . import beisen

_TENANT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_DNS_LABEL_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class InvalidJobCollectionRequest(ValueError):
    pass


class UnsupportedJobBoardError(ValueError):
    pass


class JobListingFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class AtsJobCollection:
    ats: AtsProvider
    jobs: tuple[NormalizedJob, ...]
    discovered_count: int
    scanned_count: int
    matched_count: int
    complete: bool
    stop_reason: str


def collect_ats_jobs(
    career_url: str,
    *,
    company: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    score: bool = True,
) -> AtsJobCollection:
    """Collect public jobs behind one supported ATS career URL."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 50
    ):
        raise InvalidJobCollectionRequest("invalid_request")
    normalized_keyword = _optional_input(keyword, max_length=100)
    normalized_company = _optional_input(company, max_length=200)
    try:
        parsed = urlsplit(career_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise UnsupportedJobBoardError("unsupported_job_board") from None
    greenhouse_hosts = {
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
    }
    lever_hosts = {"jobs.lever.co", "jobs.eu.lever.co"}
    ashby_hosts = {"jobs.ashbyhq.com"}
    smartrecruiters_hosts = {
        "careers.smartrecruiters.com",
        "jobs.smartrecruiters.com",
    }
    workable_hosts = {"apply.workable.com"}
    hostname = parsed.hostname
    is_beisen = _is_beisen_host(hostname)
    if (
        parsed.scheme != "https"
        or (
            hostname
            not in {
                *greenhouse_hosts,
                *lever_hosts,
                *ashby_hosts,
                *smartrecruiters_hosts,
                *workable_hosts,
            }
            and not is_beisen
        )
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise UnsupportedJobBoardError("unsupported_job_board")
    tenant = parsed.path.strip("/").split("/", 1)[0]
    if not is_beisen and not _TENANT_PATTERN.fullmatch(tenant):
        raise UnsupportedJobBoardError("unsupported_job_board")
    if hostname in workable_hosts and tenant.casefold() == "j":
        raise UnsupportedJobBoardError("unsupported_job_board")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scanned_count: int
    if is_beisen:
        ats = "beisen"
        try:
            beisen_collection = beisen.collect_beisen_jobs(
                hostname,
                company=normalized_company,
                verified_at=now,
            )
        except beisen.BeisenFetchError:
            raise JobListingFetchError("collection_failed") from None
        scanned_jobs = list(beisen_collection.jobs)
        scanned_count = beisen_collection.scanned_count
        total = beisen_collection.discovered_count
        complete = beisen_collection.complete
        normalize_job = None
    elif hostname in greenhouse_hosts:
        ats: AtsProvider = "greenhouse"
        payload = _request_json(
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{tenant}/jobs?content=true"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise JobListingFetchError("collection_failed")
        raw_jobs = payload["jobs"]
        normalize_job = _normalize_greenhouse_job
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise JobListingFetchError("collection_failed")
        total = meta.get("total", len(raw_jobs))
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise JobListingFetchError("collection_failed")
        complete = len(raw_jobs) <= 200 and total <= 200
        scanned_jobs = raw_jobs[:200]
        scanned_count = len(scanned_jobs)
    elif hostname in lever_hosts:
        ats = "lever"
        api_host = (
            "api.eu.lever.co"
            if parsed.hostname == "jobs.eu.lever.co"
            else "api.lever.co"
        )
        raw_jobs = []
        complete = False
        skip = 0
        while len(raw_jobs) < 200:
            page = _request_json(
                f"https://{api_host}/v0/postings/{tenant}"
                f"?mode=json&skip={skip}&limit=50"
            )
            if not isinstance(page, list):
                raise JobListingFetchError("collection_failed")
            remaining = 200 - len(raw_jobs)
            raw_jobs.extend(page[:remaining])
            if len(page) < 50:
                complete = True
                break
            skip += 50
        normalize_job = _normalize_lever_job
        total = len(raw_jobs)
        scanned_jobs = raw_jobs[:200]
        scanned_count = len(scanned_jobs)
    elif hostname in ashby_hosts:
        ats = "ashby"
        payload = _request_json(
            "https://api.ashbyhq.com/posting-api/job-board/"
            f"{tenant}"
        )
        if (
            not isinstance(payload, dict)
            or payload.get("apiVersion") != "1"
            or not isinstance(payload.get("jobs"), list)
        ):
            raise JobListingFetchError("collection_failed")
        raw_jobs = payload["jobs"]
        total = len(raw_jobs)
        scanned_jobs = raw_jobs[:200]
        scanned_count = len(scanned_jobs)
        complete = total <= 200
        normalize_job = _normalize_ashby_job
    elif hostname in smartrecruiters_hosts:
        ats = "smartrecruiters"
        query_values = []
        if normalized_keyword:
            query_values.append(("q", normalized_keyword))
        query_values.extend((("limit", "100"), ("offset", "0")))
        payload = _request_json(
            "https://api.smartrecruiters.com/v1/companies/"
            f"{tenant}/postings?{urlencode(query_values)}"
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("content"), list
        ):
            raise JobListingFetchError("collection_failed")
        total = payload.get("totalFound")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise JobListingFetchError("collection_failed")
        summaries = payload["content"][:50]
        raw_jobs = []
        for summary in summaries:
            if not isinstance(summary, dict):
                raw_jobs.append(summary)
                continue
            try:
                posting_id = _vendor_job_id(summary.get("id"))
            except (TypeError, ValueError):
                raw_jobs.append(None)
                continue
            detail = _request_json(
                "https://api.smartrecruiters.com/v1/companies/"
                f"{tenant}/postings/{posting_id}"
            )
            try:
                detail_posting_id = _vendor_job_id(
                    detail.get("id") if isinstance(detail, dict) else None
                )
            except (TypeError, ValueError):
                raw_jobs.append(None)
                continue
            raw_jobs.append(
                detail if detail_posting_id == posting_id else None
            )
        scanned_jobs = raw_jobs
        scanned_count = len(summaries)
        complete = total <= len(summaries)
        normalize_job = _normalize_smartrecruiters_job
    else:
        ats = "workable"
        payload = _request_json(
            f"https://www.workable.com/api/accounts/{tenant}?details=true",
            allowed_redirect_origins=(
                ("https", "apply.workable.com", 443),
            ),
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("jobs"), list
        ):
            raise JobListingFetchError("collection_failed")
        account_name = payload.get("name")
        if account_name is not None and not isinstance(account_name, str):
            raise JobListingFetchError("collection_failed")
        if normalized_company is None and isinstance(account_name, str):
            normalized_company = account_name.strip() or None
        raw_jobs = payload["jobs"]
        total = len(raw_jobs)
        scanned_jobs = raw_jobs[:200]
        scanned_count = len(scanned_jobs)
        complete = total <= 200
        normalize_job = _normalize_workable_job
    normalized_jobs: list[NormalizedJob] = []
    seen_ids: set[str] = set()
    for item in scanned_jobs:
        try:
            job = (
                item
                if normalize_job is None
                else normalize_job(
                    tenant,
                    item,
                    company=normalized_company,
                    verified_at=now,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if job.source_job_id in seen_ids:
            continue
        seen_ids.add(job.source_job_id)
        if normalized_keyword and not _matches_keyword(job, normalized_keyword):
            continue
        normalized_jobs.append(job)
    limited_jobs = normalized_jobs[:limit]
    if score:
        try:
            evaluated_jobs = tuple(
                evaluate_normalized_job(job) for job in limited_jobs
            )
            jobs = tuple(
                job
                for _, job in sorted(
                    enumerate(evaluated_jobs),
                    key=lambda item: (-float(item[1].score), item[0]),
                )
            )
        except Exception:
            raise JobListingFetchError("collection_failed") from None
    else:
        jobs = tuple(limited_jobs)
    return AtsJobCollection(
        ats=ats,
        jobs=jobs,
        discovered_count=total,
        scanned_count=scanned_count,
        matched_count=len(normalized_jobs),
        complete=complete,
        stop_reason="upstream_end" if complete else "scan_limit",
    )


def _is_beisen_host(hostname: str | None) -> bool:
    if not hostname or not hostname.endswith(".zhiye.com"):
        return False
    if len(hostname) > 253:
        return False
    labels = hostname.removesuffix(".zhiye.com").split(".")
    return bool(labels) and all(_DNS_LABEL_PATTERN.fullmatch(label) for label in labels)


def _optional_input(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidJobCollectionRequest("invalid_request")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise InvalidJobCollectionRequest("invalid_request")
    return normalized or None


def _matches_keyword(job: NormalizedJob, keyword: str) -> bool:
    needle = keyword.casefold()
    values = (
        job.title,
        job.description or "",
        *job.locations,
        *job.responsibilities,
        *job.requirements,
        *_vendor_categories(job),
    )
    return needle in " ".join(values).casefold()


def _vendor_categories(job: NormalizedJob) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("departments", "offices"):
        items = job.raw_data.get(key, ())
        if isinstance(items, (list, tuple)):
            values.extend(item for item in items if isinstance(item, str))
    categories = job.raw_data.get("categories", {})
    if isinstance(categories, dict):
        values.extend(
            value for value in categories.values() if isinstance(value, str)
        )
    values.extend(
        job.raw_data[key]
        for key in ("organization", "category")
        if isinstance(job.raw_data.get(key), str)
    )
    return tuple(values)


def _request_json(
    url: str,
    *,
    allowed_redirect_origins: tuple[tuple[str, str, int], ...] = (),
):
    try:
        expected_origin = _origin(url)
        allowed_origins = {expected_origin, *allowed_redirect_origins}
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "czc-good-job-ats-collector/1.0",
            },
            method="GET",
        )
        opener = build_opener(_AllowedOriginRedirectHandler(allowed_origins))
        with opener.open(request, timeout=10.0) as response:
            if response.status != 200:
                raise ValueError("unexpected status")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("unexpected content type")
            if _origin(response.geturl()) not in allowed_origins:
                raise ValueError("unexpected response origin")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise JobListingFetchError("collection_failed") from None


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


class _AllowedOriginRedirectHandler(HTTPRedirectHandler):
    max_redirections = 2
    max_repeats = 2

    def __init__(
        self,
        allowed_origins: set[tuple[str, str, int]],
    ) -> None:
        super().__init__()
        self.allowed_origins = allowed_origins

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved_url = urljoin(req.full_url, newurl)
        if _origin(resolved_url) not in self.allowed_origins:
            raise HTTPError(resolved_url, code, "redirect rejected", headers, fp)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            resolved_url,
        )


def _normalize_greenhouse_job(
    tenant: str,
    value: dict,
    *,
    company: str | None,
    verified_at: str,
) -> NormalizedJob:
    if not isinstance(value, dict):
        raise TypeError("invalid job")
    job_id = _vendor_job_id(value.get("id"))
    title = _required_text(value.get("title"))
    source_url = _public_https_url(value.get("absolute_url"))
    location_value = value.get("location")
    if location_value is None:
        location = None
    elif isinstance(location_value, dict):
        location = location_value.get("name")
        if location is not None and not isinstance(location, str):
            raise TypeError("invalid location")
    else:
        raise TypeError("invalid location")
    locations = (location,) if isinstance(location, str) and location else ()
    description = _plain_text(value.get("content", ""))
    departments = tuple(
        item["name"]
        for item in value.get("departments", ())
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )
    offices = tuple(
        item["name"]
        for item in value.get("offices", ())
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )
    remote_text = " ".join((title, *locations)).casefold()
    recruitment_channel = _recruitment_channel(
        title,
        *departments,
        *offices,
    )
    return NormalizedJob(
        source="greenhouse",
        source_job_id=f"{tenant}:{job_id}",
        source_url=source_url,
        official_apply_url=source_url,
        title=title,
        company=company or tenant,
        locations=locations,
        recruitment_channel=recruitment_channel,
        employment_type=None,
        work_mode="remote" if "remote" in remote_text else None,
        description=description or None,
        responsibilities=(),
        requirements=(),
        salary_text=None,
        published_at=None,
        expires_at=None,
        last_verified_at=verified_at,
        campus={} if recruitment_channel == "campus" else None,
        internship={} if recruitment_channel == "internship" else None,
        raw_data={
            "internal_job_id": _optional_public_scalar(
                value.get("internal_job_id")
            ),
            "updated_at": _optional_public_scalar(value.get("updated_at")),
            "departments": departments,
            "offices": offices,
        },
        upstream_platform="greenhouse",
    )


def _normalize_lever_job(
    tenant: str,
    value: dict,
    *,
    company: str | None,
    verified_at: str,
) -> NormalizedJob:
    if not isinstance(value, dict):
        raise TypeError("invalid job")
    job_id = _vendor_job_id(value.get("id"))
    title = _required_text(value.get("text"))
    source_url = _public_https_url(value.get("hostedUrl"))
    apply_value = value.get("applyUrl")
    apply_url = None if apply_value is None else _public_https_url(apply_value)
    categories = value.get("categories", {})
    if not isinstance(categories, dict):
        raise TypeError("invalid categories")
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        locations = tuple(
            dict.fromkeys(
                item for item in all_locations if isinstance(item, str) and item
            )
        )
    else:
        location = categories.get("location")
        locations = (location,) if isinstance(location, str) and location else ()
    commitment = categories.get("commitment")
    channel_text = " ".join(
        item
        for item in (title, commitment)
        if isinstance(item, str)
    ).casefold()
    recruitment_channel = _recruitment_channel(channel_text)
    requirements = tuple(
        _plain_text(item.get("content", ""))
        for item in value.get("lists", ())
        if isinstance(item, dict)
        and "require" in str(item.get("text", "")).casefold()
        and _plain_text(item.get("content", ""))
    )
    salary = value.get("salaryRange")
    salary_text = None
    if isinstance(salary, dict) and all(
        salary.get(key) is not None
        for key in ("currency", "interval", "min", "max")
    ):
        salary_text = (
            f"{salary['currency']} {salary['min']}–{salary['max']} "
            f"per {salary['interval']}"
        )
    selected_categories = {
        key: categories[key]
        for key in ("commitment", "department", "team")
        if isinstance(categories.get(key), str)
    }
    published_at = _lever_timestamp(value.get("createdAt"))
    return NormalizedJob(
        source="lever",
        source_job_id=f"{tenant}:{job_id}",
        source_url=source_url,
        official_apply_url=apply_url,
        title=title,
        company=company or tenant,
        locations=locations,
        recruitment_channel=recruitment_channel,
        employment_type=commitment.casefold() if isinstance(commitment, str) else None,
        work_mode=value.get("workplaceType"),
        description=value.get("descriptionPlain") or None,
        responsibilities=(),
        requirements=requirements,
        salary_text=salary_text,
        published_at=published_at,
        expires_at=None,
        last_verified_at=verified_at,
        campus={} if recruitment_channel == "campus" else None,
        internship={} if recruitment_channel == "internship" else None,
        raw_data={
            "categories": selected_categories,
            "country": _optional_public_scalar(value.get("country")),
            "workplace_type": _optional_public_scalar(
                value.get("workplaceType")
            ),
        },
        upstream_platform="lever",
    )


def _normalize_ashby_job(
    tenant: str,
    value: dict,
    *,
    company: str | None,
    verified_at: str,
) -> NormalizedJob:
    if not isinstance(value, dict) or value.get("isListed") is not True:
        raise ValueError("unlisted job")
    title = _required_text(value.get("title"))
    source_url = _public_https_url(value.get("jobUrl"))
    apply_url = _public_https_url(value.get("applyUrl"))
    parsed_source = urlsplit(source_url)
    parsed_apply = urlsplit(apply_url)
    source_segments = [segment for segment in parsed_source.path.split("/") if segment]
    apply_segments = [segment for segment in parsed_apply.path.split("/") if segment]
    if (
        parsed_source.hostname != "jobs.ashbyhq.com"
        or len(source_segments) != 2
        or source_segments[0].casefold() != tenant.casefold()
        or parsed_apply.hostname != "jobs.ashbyhq.com"
        or len(apply_segments) != 3
        or apply_segments[0].casefold() != tenant.casefold()
        or apply_segments[1] != source_segments[1]
        or apply_segments[-1] != "application"
    ):
        raise ValueError("invalid job URL")
    job_id = _vendor_job_id(source_segments[-1])
    locations: list[str] = []
    location = value.get("location")
    if isinstance(location, str) and location.strip():
        locations.append(location.strip())
    secondary = value.get("secondaryLocations", ())
    if not isinstance(secondary, list):
        raise TypeError("invalid locations")
    for item in secondary:
        if isinstance(item, dict) and isinstance(item.get("location"), str):
            if normalized := item["location"].strip():
                locations.append(normalized)
    normalized_locations = tuple(dict.fromkeys(locations))
    employment = value.get("employmentType")
    if employment is not None and not isinstance(employment, str):
        raise TypeError("invalid employment type")
    workplace = value.get("workplaceType")
    if workplace is not None and not isinstance(workplace, str):
        raise TypeError("invalid workplace type")
    work_mode = workplace.casefold() if workplace else None
    if work_mode is None and value.get("isRemote") is True:
        work_mode = "remote"
    description = value.get("descriptionPlain")
    if description is not None and not isinstance(description, str):
        raise TypeError("invalid description")
    published_at = value.get("publishedAt")
    if published_at is not None and not isinstance(published_at, str):
        raise TypeError("invalid publication time")
    compensation = value.get("compensation")
    salary_text = None
    if isinstance(compensation, dict):
        salary = compensation.get("scrapeableCompensationSalarySummary")
        if isinstance(salary, str) and salary.strip():
            salary_text = salary.strip()
    department = value.get("department")
    team = value.get("team")
    selected_categories = {
        key: category
        for key, category in (("department", department), ("team", team))
        if isinstance(category, str) and category
    }
    recruitment_channel = _recruitment_channel(
        title,
        employment or "",
        *(selected_categories.values()),
    )
    return NormalizedJob(
        source="ashby",
        source_job_id=f"{tenant}:{job_id}",
        source_url=source_url,
        official_apply_url=apply_url,
        title=title,
        company=company or tenant,
        locations=normalized_locations,
        recruitment_channel=recruitment_channel,
        employment_type=employment.casefold() if employment else None,
        work_mode=work_mode,
        description=description.strip() if isinstance(description, str) and description.strip() else None,
        responsibilities=(),
        requirements=(),
        salary_text=salary_text,
        published_at=published_at.strip() if isinstance(published_at, str) and published_at.strip() else None,
        expires_at=None,
        last_verified_at=verified_at,
        campus={} if recruitment_channel == "campus" else None,
        internship={} if recruitment_channel == "internship" else None,
        raw_data=selected_categories,
        upstream_platform="ashby",
    )


def _normalize_smartrecruiters_job(
    tenant: str,
    value: dict,
    *,
    company: str | None,
    verified_at: str,
) -> NormalizedJob:
    if not isinstance(value, dict) or value.get("active") is False:
        raise ValueError("inactive or invalid job")
    posting_id = _vendor_job_id(value.get("id"))
    title = _required_text(value.get("name"))
    source_url = _smartrecruiters_job_url(
        value.get("postingUrl"), tenant, posting_id
    )
    apply_url = _smartrecruiters_job_url(
        value.get("applyUrl"), tenant, posting_id
    )
    company_value = value.get("company", {})
    if not isinstance(company_value, dict):
        raise TypeError("invalid company")
    company_identifier = company_value.get("identifier")
    if company_identifier is not None and (
        not isinstance(company_identifier, str)
        or company_identifier.casefold() != tenant.casefold()
    ):
        raise ValueError("invalid company")
    upstream_company = company_value.get("name")
    if upstream_company is not None and not isinstance(upstream_company, str):
        raise TypeError("invalid company")
    location_value = value.get("location", {})
    if not isinstance(location_value, dict):
        raise TypeError("invalid location")
    full_location = location_value.get("fullLocation")
    if full_location is not None and not isinstance(full_location, str):
        raise TypeError("invalid location")
    locations = (
        (full_location.strip(),)
        if isinstance(full_location, str) and full_location.strip()
        else ()
    )
    department = _optional_label(value.get("department"))
    job_function = _optional_label(value.get("function"))
    employment = _optional_label(value.get("typeOfEmployment"))
    sections_value = value.get("jobAd", {})
    if not isinstance(sections_value, dict):
        raise TypeError("invalid job ad")
    sections = sections_value.get("sections", {})
    if not isinstance(sections, dict):
        raise TypeError("invalid sections")
    description_parts = []
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        text = section.get("text")
        if isinstance(text, str) and (plain_text := _plain_text(text)):
            description_parts.append(plain_text)
    published_at = value.get("releasedDate")
    if published_at is not None and not isinstance(published_at, str):
        raise TypeError("invalid publication time")
    work_mode = None
    if location_value.get("remote") is True:
        work_mode = "remote"
    elif location_value.get("hybrid") is True:
        work_mode = "hybrid"
    recruitment_channel = _recruitment_channel(
        title,
        department or "",
        job_function or "",
        employment or "",
    )
    return NormalizedJob(
        source="smartrecruiters",
        source_job_id=f"{tenant}:{posting_id}",
        source_url=source_url,
        official_apply_url=apply_url,
        title=title,
        company=company or upstream_company or tenant,
        locations=locations,
        recruitment_channel=recruitment_channel,
        employment_type=employment.casefold() if employment else None,
        work_mode=work_mode,
        description=" ".join(description_parts) or None,
        responsibilities=(),
        requirements=(),
        salary_text=None,
        published_at=published_at.strip() if published_at else None,
        expires_at=None,
        last_verified_at=verified_at,
        campus={} if recruitment_channel == "campus" else None,
        internship={} if recruitment_channel == "internship" else None,
        raw_data={
            "uuid": _optional_public_scalar(value.get("uuid")),
            "ref_number": _optional_public_scalar(value.get("refNumber")),
            "department": department,
            "function": job_function,
        },
        upstream_platform="smartrecruiters",
    )


def _smartrecruiters_job_url(
    value: object,
    tenant: str,
    posting_id: str,
) -> str:
    url = _public_https_url(value)
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.hostname != "jobs.smartrecruiters.com"
        or len(segments) != 2
        or segments[0].casefold() != tenant.casefold()
        or not (
            segments[1] == posting_id
            or segments[1].startswith(f"{posting_id}-")
        )
    ):
        raise ValueError("invalid job URL")
    return url


def _optional_label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("invalid label")
    label = value.get("label")
    if label is None:
        return None
    if not isinstance(label, str):
        raise TypeError("invalid label")
    return label.strip() or None


def _normalize_workable_job(
    tenant: str,
    value: dict,
    *,
    company: str | None,
    verified_at: str,
) -> NormalizedJob:
    if not isinstance(value, dict):
        raise TypeError("invalid job")
    shortcode = _vendor_job_id(value.get("shortcode"))
    title = _required_text(value.get("title"))
    source_url = _first_workable_job_url(
        (value.get("shortlink"), value.get("url")),
        tenant,
        shortcode,
        application=False,
    )
    apply_url = _workable_job_url(
        value.get("application_url"),
        tenant,
        shortcode,
        application=True,
    )
    location_values = value.get("locations")
    locations: list[str] = []
    if location_values is not None:
        if not isinstance(location_values, list):
            raise TypeError("invalid locations")
        for location in location_values:
            if not isinstance(location, dict) or location.get("hidden") is True:
                continue
            parts = [
                location.get("city"),
                location.get("region"),
                location.get("country"),
            ]
            if any(part is not None and not isinstance(part, str) for part in parts):
                raise TypeError("invalid location")
            rendered = ", ".join(
                part.strip()
                for part in parts
                if isinstance(part, str) and part.strip()
            )
            if rendered:
                locations.append(rendered)
    if not locations:
        parts = [value.get("city"), value.get("state"), value.get("country")]
        if any(part is not None and not isinstance(part, str) for part in parts):
            raise TypeError("invalid location")
        rendered = ", ".join(
            part.strip()
            for part in parts
            if isinstance(part, str) and part.strip()
        )
        if rendered:
            locations.append(rendered)
    employment = value.get("employment_type")
    if employment is not None and not isinstance(employment, str):
        raise TypeError("invalid employment type")
    department = value.get("department")
    job_function = value.get("function")
    if any(
        item is not None and not isinstance(item, str)
        for item in (department, job_function)
    ):
        raise TypeError("invalid category")
    description_value = value.get("description")
    if description_value is not None and not isinstance(description_value, str):
        raise TypeError("invalid description")
    description = (
        _plain_text(description_value)
        if isinstance(description_value, str)
        else ""
    )
    published_at = value.get("published_on") or value.get("created_at")
    if published_at is not None and not isinstance(published_at, str):
        raise TypeError("invalid publication time")
    recruitment_channel = _recruitment_channel(
        title,
        employment or "",
        department or "",
        job_function or "",
    )
    return NormalizedJob(
        source="workable",
        source_job_id=f"{tenant}:{shortcode}",
        source_url=source_url,
        official_apply_url=apply_url,
        title=title,
        company=company or tenant,
        locations=tuple(dict.fromkeys(locations)),
        recruitment_channel=recruitment_channel,
        employment_type=employment.casefold() if employment else None,
        work_mode="remote" if value.get("telecommuting") is True else None,
        description=description or None,
        responsibilities=(),
        requirements=(),
        salary_text=None,
        published_at=published_at.strip() if published_at else None,
        expires_at=None,
        last_verified_at=verified_at,
        campus={} if recruitment_channel == "campus" else None,
        internship={} if recruitment_channel == "internship" else None,
        raw_data={
            "code": _optional_public_scalar(value.get("code")),
            "department": department.strip() if department else None,
            "function": job_function.strip() if job_function else None,
            "industry": _optional_public_scalar(value.get("industry")),
            "experience": _optional_public_scalar(value.get("experience")),
        },
        upstream_platform="workable",
    )


def _first_workable_job_url(
    values: tuple[object, ...],
    tenant: str,
    shortcode: str,
    *,
    application: bool,
) -> str:
    for value in values:
        try:
            return _workable_job_url(
                value,
                tenant,
                shortcode,
                application=application,
            )
        except (TypeError, ValueError):
            continue
    raise ValueError("invalid job URL")


def _workable_job_url(
    value: object,
    tenant: str,
    shortcode: str,
    *,
    application: bool,
) -> str:
    url = _public_https_url(value)
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    global_segments = ["j", shortcode]
    tenant_segments = [tenant, "j", shortcode]
    expected = (
        tenant_segments
        if segments and segments[0].casefold() == tenant.casefold()
        else global_segments
    )
    if application:
        expected = [*expected, "apply"]
    if (
        parsed.hostname != "apply.workable.com"
        or len(segments) != len(expected)
        or any(
            actual.casefold() != wanted.casefold()
            for actual, wanted in zip(segments, expected)
        )
    ):
        raise ValueError("invalid job URL")
    return url


def _recruitment_channel(*values: str) -> str:
    text = " ".join(value for value in values if isinstance(value, str)).casefold()
    if re.search(r"\bintern(?:ship)?s?\b", text):
        return "internship"
    if (
        re.search(
            r"\b(?:graduate|graduates|campus|university|new[- ]grad)\b",
            text,
        )
        or "校招" in text
    ):
        return "campus"
    return "social"


def _vendor_job_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("invalid job id")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("invalid job id")
    return normalized


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise TypeError("invalid text")
    if len(normalized) > 1_000:
        raise ValueError("invalid text")
    return normalized


def _public_https_url(value: object) -> str:
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise TypeError("invalid URL")
    if len(normalized) > 8_192:
        raise ValueError("invalid URL")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("invalid URL")
    return normalized


def _lever_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("invalid timestamp")
    try:
        return datetime.fromtimestamp(
            value / 1_000,
            timezone.utc,
        ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        raise ValueError("invalid timestamp") from None


def _optional_public_scalar(value: object):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if text := data.strip():
            self.parts.append(text)


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(unescape(unescape(value)))
    return " ".join(parser.parts)


__all__ = [
    "AtsJobCollection",
    "AtsProvider",
    "InvalidJobCollectionRequest",
    "JobListingFetchError",
    "UnsupportedJobBoardError",
    "collect_ats_jobs",
]
