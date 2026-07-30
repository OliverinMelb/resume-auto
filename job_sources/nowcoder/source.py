"""Public HTTP detail loading and normalization for the Nowcoder source."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from threading import Lock
from time import monotonic, sleep
from collections.abc import Callable
from typing import Any, Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from job_sources.contracts import (
    CollectionMode,
    CollectionResult,
    JobQuery,
    NormalizedJob,
)
from job_sources.evaluation import evaluate_normalized_job

from .models import BrowserSearchResult, NowcoderCollectionProgress


SourceKind = Literal["campus", "internship", "social"]
EmploymentTypeFilter = Literal["full_time", "intern"]
TextLoader = Callable[[str], str]

BASE_URL = "https://www.nowcoder.com"
LIST_PATHS: dict[SourceKind, str] = {
    "campus": "/jobs/school/jobs",
    "internship": "/jobs/intern/center",
    "social": "/jobs/fulltime/center",
}
MAX_LIST_ITEMS = 20
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_CACHE_TTL_SECONDS = 60
MAX_CACHE_ENTRIES = 256
MAX_SEARCH_DETAIL_CHECKS = 20
MIN_REQUEST_INTERVAL_SECONDS = 1.0
USER_AGENT = "czc-job/0.1 public-job-research"
SEARCH_SECTION_ORDER: tuple[SourceKind, ...] = (
    "internship",
    "campus",
    "social",
)

_JOB_PATH_PATTERN = re.compile(r"^/jobs/detail/(?P<job_id>\d+)(?:/)?$")
_JOB_ID_PATTERN = re.compile(r"^\d+$")
_GRADUATION_YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_VERIFICATION_MARKERS = (
    "安全验证",
    "验证码加载中",
    "/verification",
)
_PUBLIC_DETAIL_RAW_KEYS = {
    "companyNameText",
    "createTime",
    "deliverBegin",
    "deliverEnd",
    "durationDays",
    "durationMonths",
    "eduLevel",
    "ext",
    "graduationYear",
    "globalJobId",
    "id",
    "jobDetailUrl",
    "jobCity",
    "jobCityList",
    "jobId",
    "jobName",
    "jobOffer",
    "commitment",
    "description",
    "platform",
    "recruitType",
    "redirectExternalUrl",
    "salary",
    "salaryShow",
    "salaryText",
    "updateTime",
}
_PUBLIC_COMPANY_RAW_KEYS = {
    "companyName",
    "industryName",
    "picUrl",
    "scale",
    "shortName",
}


class NowcoderSourceError(RuntimeError):
    """Base error for the read-only Nowcoder source."""


class InvalidSourceRequest(ValueError):
    """The caller supplied an unsupported kind or identifier."""


class UpstreamHttpError(NowcoderSourceError):
    """A public Nowcoder page could not be fetched."""


class SourceAccessRestrictedError(UpstreamHttpError):
    """Nowcoder asked the user to pause or complete normal access checks."""


class UpstreamChangedError(NowcoderSourceError):
    """The public page no longer contains the expected data."""


class VerificationRequiredError(NowcoderSourceError):
    """Nowcoder returned a verification page; the source must stop."""


@dataclass(frozen=True)
class JobReference:
    source: str
    source_job_id: str
    source_url: str
    title: str
    source_section: SourceKind
    type_verified: bool


@dataclass(frozen=True)
class JobSearchResult:
    jobs: tuple[NormalizedJob, ...]
    scanned_references: int
    checked_details: int
    failed_details: int
    section_errors: dict[str, str]
    detail_check_limit_reached: bool


class _JobLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.records: list[tuple[str, str, str]] = []
        self._current: dict[str, Any] | None = None
        self._in_title = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ):
        attr_map = {name: value or "" for name, value in attrs}
        if self._current is not None:
            if tag == "span" and "job-name" in attr_map.get("class", "").split():
                self._in_title = True
            return
        if tag != "a":
            return
        href = attr_map.get("href", "")
        parsed = urlsplit(urljoin(BASE_URL, href))
        match = _JOB_PATH_PATTERN.fullmatch(parsed.path)
        if match is None:
            return
        self._current = {
            "job_id": match.group("job_id"),
            "url": urljoin(BASE_URL, parsed.path),
            "title_parts": [],
            "fallback_parts": [],
        }

    def handle_data(self, data: str):
        if self._current is None:
            return
        text = _normalize_text(data)
        if not text:
            return
        self._current["fallback_parts"].append(text)
        if self._in_title:
            self._current["title_parts"].append(text)

    def handle_endtag(self, tag: str):
        if self._current is None:
            return
        if tag == "span" and self._in_title:
            self._in_title = False
        if tag != "a":
            return
        title_parts = self._current["title_parts"]
        fallback_parts = self._current["fallback_parts"]
        title = _normalize_text(" ".join(title_parts or fallback_parts[:1]))
        self.records.append(
            (
                self._current["job_id"],
                self._current["url"],
                title,
            )
        )
        self._current = None
        self._in_title = False


class _TextBlockParser(HTMLParser):
    _BLOCK_TAGS = {"br", "div", "li", "p"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ):
        if tag == "br":
            self._flush()

    def handle_data(self, data: str):
        text = _normalize_text(data)
        if text:
            self._parts.append(text)

    def handle_endtag(self, tag: str):
        if tag in self._BLOCK_TAGS:
            self._flush()

    def close(self):
        super().close()
        self._flush()

    def _flush(self):
        text = _normalize_text(" ".join(self._parts))
        if text:
            self.blocks.append(text)
        self._parts.clear()


class NowcoderPublicSource:
    source = "nowcoder"

    def __init__(self, load_text: TextLoader | None = None):
        self._uses_public_http = load_text is None
        self._load_text = load_text or _load_public_html
        self._cache: dict[str, tuple[float, str]] = {}
        self._request_lock = Lock()
        self._last_request_at = 0.0

    def list_jobs(
        self,
        kind: SourceKind = "internship",
        limit: int = MAX_LIST_ITEMS,
    ) -> list[JobReference]:
        if kind not in LIST_PATHS:
            raise InvalidSourceRequest(f"unsupported Nowcoder job kind: {kind}")
        if not 1 <= limit <= MAX_LIST_ITEMS:
            raise InvalidSourceRequest(
                f"limit must be between 1 and {MAX_LIST_ITEMS}"
            )
        page_url = urljoin(BASE_URL, LIST_PATHS[kind])
        html = self._fetch(page_url)
        _raise_for_verification(html)
        parser = _JobLinkParser()
        parser.feed(html)
        parser.close()
        records = parser.records or _state_job_records(html)
        if not records:
            raise UpstreamChangedError(
                f"Nowcoder {kind} page contained no public job links"
            )

        jobs: list[JobReference] = []
        seen_ids: set[str] = set()
        for job_id, source_url, title in records:
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            jobs.append(
                JobReference(
                    source=self.source,
                    source_job_id=job_id,
                    source_url=source_url,
                    title=title,
                    source_section=kind,
                    type_verified=False,
                )
            )
            if len(jobs) >= limit:
                break
        return jobs

    def get_job(self, source_job_id: str) -> NormalizedJob:
        if not _JOB_ID_PATTERN.fullmatch(source_job_id):
            raise InvalidSourceRequest("Nowcoder source_job_id must contain digits only")
        source_url = f"{BASE_URL}/jobs/detail/{source_job_id}"
        html = self._fetch(source_url)
        return self.get_job_from_html(source_job_id, html)

    def get_job_from_html(
        self,
        source_job_id: str,
        html: str,
    ) -> NormalizedJob:
        """Normalize a public detail page loaded by HTTP or visible fallback."""
        _raise_for_verification(html)
        state = _extract_initial_state(html)
        job_detail_state = _mapping_at(state, "store", "jobDetail")
        detail = job_detail_state.get("detail")
        if not isinstance(detail, Mapping) or not detail:
            raise UpstreamChangedError(
                "Nowcoder detail page did not contain a public job payload"
            )
        detail_ids = {
            value
            for key in ("globalJobId", "id", "jobId")
            if (value := _optional_str(detail.get(key)))
        }
        if detail_ids and source_job_id not in detail_ids:
            raise UpstreamChangedError(
                "Nowcoder detail payload did not match the requested job"
            )
        company = job_detail_state.get("jobCompany")
        company_mapping = company if isinstance(company, Mapping) else {}
        return _normalize_job(
            source_job_id=source_job_id,
            source_url=f"{BASE_URL}/jobs/detail/{source_job_id}",
            detail=detail,
            company=company_mapping,
        )

    def search_jobs(
        self,
        keyword: str,
        employment_type: EmploymentTypeFilter,
        limit: int = 10,
    ) -> JobSearchResult:
        normalized_keyword = _normalize_text(keyword).casefold()
        if not normalized_keyword:
            raise InvalidSourceRequest("Nowcoder search keyword cannot be empty")
        if len(normalized_keyword) > 50:
            raise InvalidSourceRequest(
                "Nowcoder search keyword cannot exceed 50 characters"
            )
        if employment_type not in {"full_time", "intern"}:
            raise InvalidSourceRequest(
                f"unsupported employment type: {employment_type}"
            )
        if not 1 <= limit <= MAX_LIST_ITEMS:
            raise InvalidSourceRequest(
                f"limit must be between 1 and {MAX_LIST_ITEMS}"
            )

        references: list[JobReference] = []
        seen_ids: set[str] = set()
        section_errors: dict[str, str] = {}
        for section in SEARCH_SECTION_ORDER:
            try:
                section_jobs = self.list_jobs(
                    kind=section,
                    limit=MAX_LIST_ITEMS,
                )
            except (
                VerificationRequiredError,
                SourceAccessRestrictedError,
            ):
                raise
            except NowcoderSourceError as error:
                section_errors[section] = type(error).__name__
                continue
            for reference in section_jobs:
                if reference.source_job_id in seen_ids:
                    continue
                seen_ids.add(reference.source_job_id)
                references.append(reference)

        matching_references = [
            reference
            for reference in references
            if normalized_keyword in reference.title.casefold()
        ]
        jobs: list[NormalizedJob] = []
        checked_details = 0
        failed_details = 0
        for reference in matching_references:
            if checked_details >= MAX_SEARCH_DETAIL_CHECKS:
                break
            checked_details += 1
            try:
                job = self.get_job(reference.source_job_id)
            except (
                VerificationRequiredError,
                SourceAccessRestrictedError,
            ):
                raise
            except NowcoderSourceError:
                failed_details += 1
                continue
            if job.employment_type != employment_type:
                continue
            jobs.append(job)
            if len(jobs) >= limit:
                break

        return JobSearchResult(
            jobs=tuple(jobs),
            scanned_references=len(references),
            checked_details=checked_details,
            failed_details=failed_details,
            section_errors=section_errors,
            detail_check_limit_reached=(
                len(matching_references) > checked_details
                and len(jobs) < limit
            ),
        )

    def health_check(self) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            jobs = self.list_jobs(kind="internship", limit=MAX_LIST_ITEMS)
            return {
                "source": self.source,
                "success": True,
                "fetched_count": len(jobs),
                "error_message": None,
                "checked_at": checked_at,
            }
        except Exception as error:
            return {
                "source": self.source,
                "success": False,
                "fetched_count": 0,
                "error_message": (
                    f"{type(error).__name__}: {_safe_error_message(error)}"
                ),
                "checked_at": checked_at,
            }

    def _fetch(self, url: str) -> str:
        with self._request_lock:
            now = monotonic()
            cached = self._cache.get(url)
            if cached is not None and cached[0] > now:
                return cached[1]
            if self._uses_public_http and self._last_request_at:
                remaining = (
                    MIN_REQUEST_INTERVAL_SECONDS
                    - (now - self._last_request_at)
                )
                if remaining > 0:
                    sleep(remaining)
            self._last_request_at = monotonic()
            html = self._load_text(url)
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                expired_urls = [
                    cached_url
                    for cached_url, (expires_at, _) in self._cache.items()
                    if expires_at <= monotonic()
                ]
                for expired_url in expired_urls:
                    self._cache.pop(expired_url, None)
                if len(self._cache) >= MAX_CACHE_ENTRIES:
                    oldest_url = min(
                        self._cache,
                        key=lambda cached_url: self._cache[cached_url][0],
                    )
                    self._cache.pop(oldest_url, None)
            self._cache[url] = (
                monotonic() + DEFAULT_CACHE_TTL_SECONDS,
                html,
            )
            return html


class NowcoderSearchCollector(Protocol):
    def collect(
        self,
        query: JobQuery,
        *,
        target: int | None,
        max_pages: int,
        should_stop: Callable[[], bool],
    ) -> BrowserSearchResult: ...


class NowcoderDetailSource(Protocol):
    def get_job(self, source_job_id: str) -> NormalizedJob: ...


class NowcoderSourceAdapter:
    """Combines browser-filtered references with public detail loading."""

    source = "nowcoder"

    def __init__(
        self,
        *,
        search_collector: NowcoderSearchCollector,
        detail_source: NowcoderDetailSource | None = None,
        evaluate_job: Callable[
            [NormalizedJob],
            NormalizedJob,
        ] = evaluate_normalized_job,
    ):
        self._search_collector = search_collector
        self._detail_source = detail_source or NowcoderPublicSource()
        self._evaluate_job = evaluate_job

    def collect(
        self,
        query: JobQuery,
        *,
        mode: CollectionMode,
        should_stop: Callable[[], bool],
    ) -> CollectionResult:
        return self._collect(
            query,
            mode=mode,
            should_stop=should_stop,
            resume=None,
            on_progress=None,
        )

    def collect_resumable(
        self,
        query: JobQuery,
        *,
        mode: CollectionMode,
        should_stop: Callable[[], bool],
        resume: NowcoderCollectionProgress | None,
        on_progress: Callable[[NowcoderCollectionProgress], None],
    ) -> CollectionResult:
        return self._collect(
            query,
            mode=mode,
            should_stop=should_stop,
            resume=resume,
            on_progress=on_progress,
        )

    def _collect(
        self,
        query: JobQuery,
        *,
        mode: CollectionMode,
        should_stop: Callable[[], bool],
        resume: NowcoderCollectionProgress | None,
        on_progress: Callable[[NowcoderCollectionProgress], None] | None,
    ) -> CollectionResult:
        if mode not in {"quick", "research"}:
            raise InvalidSourceRequest(
                f"unsupported Nowcoder collection mode: {mode}"
            )
        resume_browser = (
            BrowserSearchResult(
                references=resume.references,
                pages_read=resume.pages_read,
                discovered_count=resume.discovered_count,
                complete=resume.browser_complete,
                stop_reason=resume.browser_stop_reason,
            )
            if resume is not None
            else None
        )
        existing_jobs = list(resume.jobs if resume is not None else ())

        def browser_progress(value: BrowserSearchResult):
            if on_progress is not None:
                on_progress(
                    NowcoderCollectionProgress(
                        references=value.references,
                        jobs=tuple(existing_jobs),
                        pages_read=value.pages_read,
                        discovered_count=value.discovered_count,
                        browser_complete=value.complete,
                        browser_stop_reason=value.stop_reason,
                        failed_detail_ids=(
                            resume.failed_detail_ids if resume else ()
                        ),
                    )
                )

        collect_resumable = getattr(
            self._search_collector,
            "collect_resumable",
            None,
        )
        if callable(collect_resumable) and mode == "research":
            browser_result = collect_resumable(
                query,
                target=None,
                max_pages=100,
                should_stop=should_stop,
                resume_result=resume_browser,
                on_progress=browser_progress,
            )
        else:
            browser_result = self._search_collector.collect(
                query,
                target=30 if mode == "quick" else None,
                max_pages=3 if mode == "quick" else 100,
                should_stop=should_stop,
            )

        jobs_by_id = {job.source_job_id: job for job in existing_jobs}
        failed_detail_ids: set[str] = set()
        for reference in browser_result.references:
            if should_stop():
                break
            if reference.source_job_id in jobs_by_id:
                continue
            try:
                job = self._get_job(reference.source_job_id)
            except (
                VerificationRequiredError,
                SourceAccessRestrictedError,
            ):
                raise
            except NowcoderSourceError:
                failed_detail_ids.add(reference.source_job_id)
                if on_progress is not None:
                    on_progress(
                        NowcoderCollectionProgress(
                            references=browser_result.references,
                            jobs=tuple(jobs_by_id.values()),
                            pages_read=browser_result.pages_read,
                            discovered_count=browser_result.discovered_count,
                            browser_complete=browser_result.complete,
                            browser_stop_reason=browser_result.stop_reason,
                            failed_detail_ids=tuple(
                                sorted(failed_detail_ids)
                            ),
                        )
                    )
                continue
            scored_job = self._evaluate_job(job)
            jobs_by_id[scored_job.source_job_id] = scored_job
            failed_detail_ids.discard(scored_job.source_job_id)
            if on_progress is not None:
                on_progress(
                    NowcoderCollectionProgress(
                        references=browser_result.references,
                        jobs=tuple(jobs_by_id.values()),
                        pages_read=browser_result.pages_read,
                        discovered_count=browser_result.discovered_count,
                        browser_complete=browser_result.complete,
                        browser_stop_reason=browser_result.stop_reason,
                        failed_detail_ids=tuple(
                            sorted(failed_detail_ids)
                        ),
                    )
                )

        jobs = tuple(jobs_by_id.values())
        partial_details = sum(
            job.detail_status == "partial" for job in jobs
        )
        complete = (
            browser_result.complete
            and not failed_detail_ids
            and len(jobs) == len(browser_result.references)
            and not should_stop()
        )
        return CollectionResult(
            source=self.source,
            query=query,
            jobs=jobs,
            pages_read=browser_result.pages_read,
            discovered_count=browser_result.discovered_count,
            deduplicated_count=len(browser_result.references),
            detail_success_count=len(jobs) - partial_details,
            partial_detail_count=partial_details,
            detail_failure_count=len(failed_detail_ids),
            complete=complete,
            stop_reason=(
                "user_stop"
                if should_stop()
                else (
                    "detail_failures"
                    if failed_detail_ids
                    else browser_result.stop_reason
                )
            ),
        )

    def _get_job(self, source_job_id: str) -> NormalizedJob:
        try:
            return self._detail_source.get_job(source_job_id)
        except (VerificationRequiredError, SourceAccessRestrictedError):
            raise
        except NowcoderSourceError:
            load_detail_html = getattr(
                self._search_collector,
                "load_detail_html",
                None,
            )
            normalize_html = getattr(
                self._detail_source,
                "get_job_from_html",
                None,
            )
            if not callable(load_detail_html) or not callable(normalize_html):
                raise
            html = load_detail_html(source_job_id)
            return normalize_html(source_job_id, html)


def _load_public_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            if not _is_nowcoder_url(final_url):
                raise UpstreamHttpError("Nowcoder redirected outside its public site")
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise UpstreamHttpError("Nowcoder response exceeded the size limit")
            charset = response.headers.get_content_charset() or "utf-8"
            return content.decode(charset, errors="replace")
    except HTTPError as error:
        if error.code in {403, 429}:
            raise SourceAccessRestrictedError(
                f"Nowcoder public page returned HTTP {error.code}"
            ) from error
        raise UpstreamHttpError(
            f"Nowcoder public page returned HTTP {error.code}"
        ) from error
    except URLError as error:
        reason = getattr(error, "reason", None)
        reason_name = type(reason).__name__ if reason is not None else "network error"
        raise UpstreamHttpError(
            f"Nowcoder public page request failed: {reason_name}"
        ) from error
    except TimeoutError as error:
        raise UpstreamHttpError("Nowcoder public page request timed out") from error


def _normalize_job(
    source_job_id: str,
    source_url: str,
    detail: Mapping[str, Any],
    company: Mapping[str, Any],
) -> NormalizedJob:
    title = _optional_str(detail.get("jobName"))
    if not title:
        raise UpstreamChangedError("Nowcoder detail payload did not contain a title")

    ext = _parse_ext(detail.get("ext"))
    responsibilities = _text_blocks(ext.get("infos"))
    requirements = _text_blocks(ext.get("requirements"))
    aggregate_description = _text_blocks(detail.get("description"))
    description_parts = (
        [*responsibilities, *requirements]
        if responsibilities or requirements
        else list(aggregate_description)
    )
    locations = _locations(detail)
    recruit_type = _optional_int(detail.get("recruitType"))
    recruitment_channel, employment_type = _employment(recruit_type)
    if employment_type is None:
        recruitment_channel, employment_type = _employment_from_commitment(
            detail.get("commitment")
        )
    graduation_years = _graduation_years(detail.get("graduationYear"))
    campus = (
        {"graduation_years": graduation_years}
        if graduation_years
        else None
    )
    internship = _internship(detail) if employment_type == "intern" else None
    official_apply_url = _external_url(
        detail.get("redirectExternalUrl")
    ) or _external_url(detail.get("jobDetailUrl"))
    upstream_platform = _optional_str(detail.get("platform"))
    is_aggregate = any(
        detail.get(key) is not None
        for key in ("globalJobId", "jobDetailUrl", "platform")
    )
    company_name = _optional_str(company.get("companyName")) or _optional_str(
        detail.get("companyNameText")
    )
    raw_detail = _select_public_detail_raw_data(detail)
    raw_company = _select_public_scalar_data(
        company,
        _PUBLIC_COMPANY_RAW_KEYS,
    )

    return NormalizedJob(
        source="nowcoder",
        source_job_id=source_job_id,
        source_url=source_url,
        official_apply_url=official_apply_url,
        title=title,
        company=company_name,
        locations=locations,
        recruitment_channel=recruitment_channel,
        employment_type=employment_type,
        work_mode=_work_mode(title, locations),
        description="\n".join(description_parts) if description_parts else None,
        responsibilities=responsibilities,
        requirements=requirements,
        salary_text=_optional_str(detail.get("salaryShow"))
        or _optional_str(detail.get("salaryText"))
        or _optional_str(detail.get("salary")),
        published_at=_milliseconds_to_iso(detail.get("createTime")),
        expires_at=_milliseconds_to_iso(detail.get("deliverEnd")),
        last_verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        campus=campus,
        internship=internship,
        raw_data={
            "detail": raw_detail,
            "company": raw_company,
        },
        detail_status="partial" if is_aggregate else "complete",
        upstream_platform=upstream_platform,
    )


def _extract_initial_state(html: str) -> Mapping[str, Any]:
    marker = "window.__INITIAL_STATE__="
    marker_index = html.find(marker)
    if marker_index < 0:
        raise UpstreamChangedError(
            "Nowcoder detail page did not contain initial state"
        )
    payload_start = marker_index + len(marker)
    script_end = html.find("</script>", payload_start)
    if script_end < 0:
        raise UpstreamChangedError("Nowcoder initial state script was incomplete")
    payload = html[payload_start:script_end]
    cleanup_index = payload.find(";(function")
    if cleanup_index >= 0:
        payload = payload[:cleanup_index]
    else:
        payload = payload.strip().removesuffix(";")
    try:
        state = json.loads(payload)
    except json.JSONDecodeError as error:
        raise UpstreamChangedError(
            "Nowcoder initial state was not valid JSON"
        ) from error
    if not isinstance(state, Mapping):
        raise UpstreamChangedError("Nowcoder initial state was not an object")
    return state


def _state_job_records(html: str) -> list[tuple[str, str, str]]:
    try:
        state = _extract_initial_state(html)
    except UpstreamChangedError:
        return []
    app = state.get("app")
    job_list = _find_job_list_data(app)
    if job_list is None:
        return []
    records = []
    for item in job_list:
        if not isinstance(item, Mapping):
            continue
        nested = item.get("data")
        detail = nested if isinstance(nested, Mapping) else item
        job_id = _optional_str(detail.get("id"))
        title = _optional_str(detail.get("jobName"))
        if not job_id or not _JOB_ID_PATTERN.fullmatch(job_id):
            continue
        records.append(
            (
                job_id,
                f"{BASE_URL}/jobs/detail/{job_id}",
                title or "",
            )
        )
    return records


def _find_job_list_data(value: Any) -> list[Any] | None:
    if isinstance(value, Mapping):
        candidate = value.get("jobListData")
        if isinstance(candidate, list):
            return candidate
        for item in value.values():
            result = _find_job_list_data(item)
            if result is not None:
                return result
    return None


def _mapping_at(state: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = state
    for key in path:
        if not isinstance(current, Mapping):
            raise UpstreamChangedError(
                f"Nowcoder initial state changed before {key}"
            )
        current = current.get(key)
    if not isinstance(current, Mapping):
        raise UpstreamChangedError(
            f"Nowcoder initial state did not contain {'.'.join(path)}"
        )
    return current


def _parse_ext(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"infos": value}
    return parsed if isinstance(parsed, Mapping) else {}


def _text_blocks(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    parser = _TextBlockParser()
    parser.feed(value)
    parser.close()
    if parser.blocks:
        return tuple(dict.fromkeys(parser.blocks))
    text = _normalize_text(value)
    return (text,) if text else ()


def _locations(detail: Mapping[str, Any]) -> tuple[str, ...]:
    values = detail.get("jobCityList")
    candidates = values if isinstance(values, list) else [detail.get("jobCity")]
    result = []
    for value in candidates:
        text = _optional_str(value)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _employment(recruit_type: int | None) -> tuple[str, str | None]:
    if recruit_type == 1:
        return "campus", "full_time"
    if recruit_type == 2:
        return "campus", "intern"
    if recruit_type == 3:
        return "social", "full_time"
    return "unknown", None


def _employment_from_commitment(value: Any) -> tuple[str, str | None]:
    commitment = _optional_str(value)
    if not commitment:
        return "unknown", None
    normalized = commitment.casefold()
    if "实习" in normalized or "intern" in normalized:
        return "internship", "intern"
    if "兼职" in normalized or "part" in normalized:
        return "social", "part_time"
    return "social", "full_time"


def _internship(detail: Mapping[str, Any]) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    days_per_week = _optional_int(detail.get("durationDays"))
    duration_months = _optional_int(detail.get("durationMonths"))
    if days_per_week is not None:
        result["days_per_week"] = days_per_week
    if duration_months is not None:
        result["duration_months"] = duration_months
    job_offer = detail.get("jobOffer")
    if isinstance(job_offer, bool):
        result["conversion_opportunity"] = job_offer
    elif isinstance(job_offer, int):
        result["conversion_opportunity"] = job_offer > 0
    return result or None


def _graduation_years(value: Any) -> list[int]:
    text = _optional_str(value)
    if not text:
        return []
    return list(
        dict.fromkeys(int(match) for match in _GRADUATION_YEAR_PATTERN.findall(text))
    )


def _milliseconds_to_iso(value: Any) -> str | None:
    milliseconds = _optional_int(value)
    if milliseconds is None or milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(
            milliseconds / 1000,
            timezone.utc,
        ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _work_mode(title: str, locations: tuple[str, ...]) -> str | None:
    values = (title, *locations)
    return "remote" if any("远程" in value for value in values) else None


def _external_url(value: Any) -> str | None:
    url = _optional_str(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.hostname and parsed.hostname.lower().endswith("nowcoder.com"):
        return None
    return url


def _select_public_detail_raw_data(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = _select_public_scalar_data(
        value,
        _PUBLIC_DETAIL_RAW_KEYS - {"ext", "jobCityList"},
    )
    cities = value.get("jobCityList")
    if isinstance(cities, list):
        result["jobCityList"] = [
            city for city in cities if isinstance(city, str)
        ]
    ext = _parse_ext(value.get("ext"))
    public_ext = {
        key: item
        for key in ("infos", "requirements")
        if isinstance((item := ext.get(key)), str)
    }
    if public_ext:
        result["ext"] = public_ext
    return result


def _select_public_scalar_data(
    value: Mapping[str, Any],
    allowed_keys: set[str],
) -> dict[str, str | int | float | bool | None]:
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str)
        and key in allowed_keys
        and isinstance(item, (str, int, float, bool, type(None)))
    }


def _raise_for_verification(html: str):
    if any(marker in html for marker in _VERIFICATION_MARKERS):
        raise VerificationRequiredError(
            "Nowcoder verification page detected; automated collection stopped"
        )


def _is_nowcoder_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname == "nowcoder.com" or hostname.endswith(".nowcoder.com")


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    text = _normalize_text(str(value))
    return text or None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _safe_error_message(error: Exception) -> str:
    message = _normalize_text(str(error))
    return message[:300] or "unknown error"
