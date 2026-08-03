"""Anonymous Nowcoder filter metadata and paginated search adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Lock
from time import monotonic, sleep
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


BASE_URL = "https://www.nowcoder.com"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 1.0
USER_AGENT = "czc-job/0.1 anonymous-job-research"
_GET_PATHS = {
    "/np-api/u/job/career-job",
    "/nccommon/city/all-with-china",
}
_POST_PATHS = {
    "/np-api/u/job/square-search",
    "/np-api/u/school-schedule/list-card",
}


class AnonymousUpstreamError(RuntimeError):
    """The anonymous Nowcoder interface returned an unusable response."""


class AnonymousTransportError(AnonymousUpstreamError):
    """The anonymous Nowcoder HTTP request failed."""


class AnonymousAccessRestrictedError(AnonymousTransportError):
    """Nowcoder asked the anonymous caller to slow down or verify."""


class NowcoderJsonTransport(Protocol):
    def get(self, path: str) -> Mapping[str, Any]: ...

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class UrllibNowcoderTransport:
    """Rate-limited JSON transport for fixed anonymous Nowcoder paths."""

    def __init__(
        self,
        *,
        minimum_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds cannot be negative")
        self._minimum_interval_seconds = minimum_interval_seconds
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._lock = Lock()
        self._last_request_at = 0.0

    def get(self, path: str) -> Mapping[str, Any]:
        if path not in _GET_PATHS:
            raise ValueError("unsupported anonymous Nowcoder GET path")
        return self._request(path, method="GET", body=None)

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if path not in _POST_PATHS:
            raise ValueError("unsupported anonymous Nowcoder POST path")
        return self._request(path, method="POST", body=body)

    def _request(
        self,
        path: str,
        *,
        method: str,
        body: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        encoded_body = (
            json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if body is not None
            else None
        )
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{BASE_URL}/jobs/school/jobs",
        }
        if encoded_body is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
            headers["Origin"] = BASE_URL
        request = Request(
            urljoin(BASE_URL, path),
            data=encoded_body,
            headers=headers,
            method=method,
        )
        with self._lock:
            now = monotonic()
            if self._last_request_at:
                remaining = (
                    self._minimum_interval_seconds
                    - (now - self._last_request_at)
                )
                if remaining > 0:
                    sleep(remaining)
            self._last_request_at = monotonic()
            try:
                with urlopen(
                    request,
                    timeout=self._timeout_seconds,
                ) as response:
                    final_url = response.geturl()
                    if not _is_nowcoder_url(final_url):
                        raise AnonymousTransportError(
                            "Nowcoder anonymous request redirected outside "
                            "its public site"
                        )
                    content = response.read(self._max_response_bytes + 1)
                    if len(content) > self._max_response_bytes:
                        raise AnonymousTransportError(
                            "Nowcoder anonymous response exceeded the size "
                            "limit"
                        )
                    charset = (
                        response.headers.get_content_charset() or "utf-8"
                    )
            except HTTPError as error:
                if error.code in {403, 429}:
                    raise AnonymousAccessRestrictedError(
                        f"Nowcoder anonymous request returned HTTP {error.code}"
                    ) from error
                raise AnonymousTransportError(
                    f"Nowcoder anonymous request returned HTTP {error.code}"
                ) from error
            except URLError as error:
                reason = getattr(error, "reason", None)
                reason_name = (
                    type(reason).__name__
                    if reason is not None
                    else "network error"
                )
                raise AnonymousTransportError(
                    "Nowcoder anonymous request failed: "
                    f"{reason_name}"
                ) from error
            except TimeoutError as error:
                raise AnonymousTransportError(
                    "Nowcoder anonymous request timed out"
                ) from error
        try:
            payload = json.loads(
                content.decode(charset, errors="strict")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AnonymousTransportError(
                "Nowcoder anonymous response was not valid JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise AnonymousTransportError(
                "Nowcoder anonymous response was not an object"
            )
        return payload


@dataclass(frozen=True)
class CareerJobOption:
    id: str
    label: str
    group: str
    name: str


@dataclass(frozen=True)
class NowcoderFilterOptions:
    career_jobs: tuple[CareerJobOption, ...]
    hot_cities: tuple[str, ...]
    cities: tuple[str, ...]


@dataclass(frozen=True)
class AnonymousJobSummary:
    id: str
    title: str


@dataclass(frozen=True)
class AnonymousJobPage:
    jobs: tuple[AnonymousJobSummary, ...]
    current_page: int
    total_pages: int


class NowcoderAnonymousApi:
    """Hides Nowcoder's volatile anonymous request contract."""

    def __init__(self, transport: NowcoderJsonTransport):
        self._transport = transport

    def get_filter_options(self) -> NowcoderFilterOptions:
        career_payload = self._transport.get(
            "/np-api/u/job/career-job"
        )
        city_payload = self._transport.get(
            "/nccommon/city/all-with-china"
        )
        career_data = _success_data(career_payload)
        city_data = _success_data(city_payload)
        career_tree = career_data.get("allCareerJob")
        hot_cities = city_data.get("hotCities")
        if (
            not isinstance(career_tree, list)
            or not isinstance(hot_cities, list)
        ):
            raise AnonymousUpstreamError(
                "Nowcoder filter metadata changed shape"
            )
        career_jobs = tuple(_career_job_leaves(career_tree))
        normalized_hot_cities = _unique_texts(hot_cities)
        if not career_jobs or not normalized_hot_cities:
            raise AnonymousUpstreamError(
                "Nowcoder filter metadata was empty"
            )
        return NowcoderFilterOptions(
            career_jobs=career_jobs,
            hot_cities=normalized_hot_cities,
            cities=normalized_hot_cities,
        )

    def search_page(
        self,
        *,
        recruitment_channel: str,
        city: str | None,
        career_job_id: str | None,
        keyword: str | None,
        page: int,
    ) -> AnonymousJobPage:
        recruit_types = {
            "campus": 1,
            "internship": 2,
            "social": 3,
        }
        recruit_type = recruit_types.get(recruitment_channel)
        if recruit_type is None:
            raise ValueError(
                f"unsupported recruitment channel: {recruitment_channel}"
            )
        if page < 1:
            raise ValueError("page must be positive")
        normalized_city = _text(city)
        normalized_keyword = _text(keyword)
        normalized_career_job_id = (
            _identifier(career_job_id)
            if career_job_id is not None
            else None
        )
        if career_job_id is not None and normalized_career_job_id is None:
            raise ValueError("career_job_id must contain digits only")
        payload = self._transport.post(
            "/np-api/u/job/square-search",
            {
                "careerJobId": normalized_career_job_id or "",
                "companyFinancing": None,
                "jobCity": (
                    [normalized_city] if normalized_city is not None else []
                ),
                "graduateYearList": None,
                "scaleList": None,
                "page": page,
                "query": normalized_keyword or "",
                "random": True,
                "recommend": False,
                "recruitType": recruit_type,
                "salaryMax": None,
                "salaryMin": None,
                "salaryType": 2,
                "pageSize": 20,
                "requestFrom": 1,
                "order": 0,
                "pageSource": 5001,
            },
        )
        data = _success_data(payload)
        current_page = data.get("currentPage")
        total_pages = data.get("totalPage")
        records = data.get("datas")
        if (
            not isinstance(current_page, int)
            or isinstance(current_page, bool)
            or current_page < 1
            or not isinstance(total_pages, int)
            or isinstance(total_pages, bool)
            or total_pages < 0
            or not isinstance(records, list)
        ):
            raise AnonymousUpstreamError(
                "Nowcoder anonymous search changed shape"
            )
        if total_pages > 0 and current_page > total_pages:
            raise AnonymousUpstreamError(
                "Nowcoder anonymous search returned invalid page bounds"
            )
        jobs: list[AnonymousJobSummary] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            nested_record = record.get("data")
            if isinstance(nested_record, Mapping):
                record = nested_record
            job_id = _identifier(record.get("id"))
            title = _text(record.get("jobName"))
            if job_id is None or title is None:
                continue
            jobs.append(AnonymousJobSummary(id=job_id, title=title))
        if records and not jobs:
            raise AnonymousUpstreamError(
                "Nowcoder anonymous search contained no usable jobs"
            )
        return AnonymousJobPage(
            jobs=tuple(jobs),
            current_page=current_page,
            total_pages=total_pages,
        )


def _success_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("code") != 0:
        raise AnonymousUpstreamError(
            "Nowcoder anonymous request was rejected"
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise AnonymousUpstreamError(
            "Nowcoder anonymous response contained no data"
        )
    return data


def _career_job_leaves(
    nodes: list[Any],
    parents: tuple[str, ...] = (),
):
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        name = _text(node.get("name"))
        node_id = _identifier(node.get("id"))
        children = node.get("value")
        if name is None or node_id is None or not isinstance(children, list):
            continue
        path = (*parents, name)
        if children:
            yield from _career_job_leaves(children, path)
        else:
            yield CareerJobOption(
                id=node_id,
                label=" / ".join(path),
                group=" / ".join(parents),
                name=name,
            )


def _unique_texts(values: list[Any]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if text is None or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _identifier(value: Any) -> str | None:
    text = _text(value)
    return text if text is not None and text.isdecimal() else None


def _text(value: Any) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _is_nowcoder_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == "www.nowcoder.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )
