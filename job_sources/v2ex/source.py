"""Anonymous, bounded reader for the official V2EX jobs JSON Feed."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from job_sources.contracts import NormalizedJob


FEED_URL = "https://www.v2ex.com/feed/jobs.json"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_048_576
MAX_HTML_CHARS = 100_000
MAX_SAFE_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 500
MAX_CURSOR_IDENTITIES = 1_000
MAX_FEED_ITEMS = 200
_KNOWN_LOCATIONS = {
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "苏州",
    "成都",
    "重庆",
    "武汉",
    "西安",
    "天津",
    "长沙",
    "郑州",
    "青岛",
    "厦门",
    "福州",
    "合肥",
    "济南",
    "大连",
    "宁波",
    "无锡",
    "珠海",
    "东莞",
    "佛山",
    "昆明",
    "香港",
    "澳门",
    "台北",
}
_RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})"
)

Classification = Literal[
    "recruitment",
    "referral",
    "job_seeking",
    "outsourcing",
    "advertisement",
    "discussion",
    "uncertain",
]
FetchStatus = Literal["complete", "partial", "failed"]
Disposition = Literal["normalized", "review", "invalid"]


@dataclass(frozen=True)
class V2exCursor:
    seen_identities: tuple[str, ...] = ()

    def __post_init__(self):
        if len(self.seen_identities) > MAX_CURSOR_IDENTITIES:
            raise ValueError("V2EX cursor exceeds its identity limit")
        if len(set(self.seen_identities)) != len(self.seen_identities):
            raise ValueError("V2EX cursor identities must be unique")
        if any(
            not isinstance(value, str)
            or _safe_identity(value) is None
            for value in self.seen_identities
        ):
            raise ValueError("V2EX cursor contains an invalid identity")


@dataclass(frozen=True)
class V2exSignals:
    remote: bool = False
    internship: bool = False
    part_time: bool = False
    contract: bool = False
    foreign_company_or_team: bool = False
    locations: tuple[str, ...] = ()
    salary_text: str | None = None
    deadline_text: str | None = None


@dataclass(frozen=True)
class V2exItemOutcome:
    identity: str | None
    source_url: str | None
    classification: Classification
    confidence: Literal["high", "low"]
    disposition: Disposition
    risk_tags: tuple[str, ...]
    signals: V2exSignals
    safe_text: str
    job: NormalizedJob | None
    reason_codes: tuple[str, ...] = ()
    published_at: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None


@dataclass(frozen=True)
class V2exFetchResult:
    status: FetchStatus
    outcomes: tuple[V2exItemOutcome, ...]
    discovered_count: int
    next_cursor: V2exCursor
    duplicate_count: int = 0
    seen_count: int = 0
    error_code: str | None = None
    new_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    stale: bool = False


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "template", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_feed(request: Request, *, timeout: float):
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


class V2exJobSource:
    """Fetch and classify one bounded snapshot from the official Feed."""

    def __init__(
        self,
        *,
        opener: Callable[..., object] = _open_feed,
        now: Callable[[], datetime] | None = None,
    ):
        self._opener = opener
        self._now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self, cursor: V2exCursor | None = None) -> V2exFetchResult:
        previous = cursor or V2exCursor()
        request = Request(
            FEED_URL,
            headers={
                "Accept": "application/feed+json, application/json;q=0.9",
                "User-Agent": "czc-good-job-v2ex/1.0",
            },
            method="GET",
        )
        parsed_length: int | None = None
        try:
            with self._opener(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                status = getattr(response, "status", None)
                if status != 200:
                    return _failed_fetch(previous, _http_error_code(status))
                if response.geturl() != FEED_URL:
                    return _failed_fetch(previous, "unsafe_redirect")
                media_type = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if media_type not in {
                    "application/json",
                    "application/feed+json",
                }:
                    return _failed_fetch(previous, "invalid_content_type")
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError:
                        return _failed_fetch(
                            previous,
                            "invalid_content_length",
                        )
                    if (
                        parsed_length < 0
                        or parsed_length > MAX_RESPONSE_BYTES
                    ):
                        return _failed_fetch(
                            previous,
                            "response_too_large",
                        )
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            return _failed_fetch(previous, _http_error_code(exc.code))
        except (URLError, TimeoutError, OSError):
            return _failed_fetch(previous, "network_error")
        if not isinstance(body, bytes):
            return _failed_fetch(previous, "invalid_response_body")
        if len(body) > MAX_RESPONSE_BYTES:
            return _failed_fetch(previous, "response_too_large")
        if parsed_length is not None and parsed_length != len(body):
            return _failed_fetch(previous, "invalid_content_length")
        try:
            payload: object = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            return _failed_fetch(previous, "invalid_json")
        try:
            feed_items = _feed_items(payload)
        except ValueError:
            return _failed_fetch(previous, "invalid_feed")
        previously_seen = set(previous.seen_identities)
        feed_seen: set[str] = set()
        selected: list[object] = []
        duplicate_count = 0
        seen_count = 0
        for value in feed_items:
            identity = _candidate_identity(value)
            if identity is not None and identity in feed_seen:
                duplicate_count += 1
                continue
            if identity is not None:
                feed_seen.add(identity)
            if identity is not None and identity in previously_seen:
                seen_count += 1
                continue
            selected.append(value)
        observed_at = self._now()
        outcomes = tuple(
            _normalize_item(value, observed_at=observed_at)
            for value in selected
        )
        new_identities = tuple(
            outcome.identity
            for outcome in outcomes
            if outcome.identity is not None
        )
        identities = tuple(
            dict.fromkeys((*new_identities, *previous.seen_identities))
        )[:MAX_CURSOR_IDENTITIES]
        return V2exFetchResult(
            status=(
                "partial"
                if any(_outcome_is_partial(outcome) for outcome in outcomes)
                else "complete"
            ),
            outcomes=outcomes,
            discovered_count=len(feed_items),
            next_cursor=V2exCursor(identities),
            duplicate_count=duplicate_count,
            seen_count=seen_count,
        )


def _failed_fetch(cursor: V2exCursor, error_code: str) -> V2exFetchResult:
    return V2exFetchResult(
        status="failed",
        outcomes=(),
        discovered_count=0,
        next_cursor=cursor,
        error_code=error_code,
    )


def _outcome_is_partial(outcome: V2exItemOutcome) -> bool:
    if outcome.disposition == "invalid":
        return True
    return any(
        reason == "body_truncated"
        or reason == "title_truncated"
        or reason == "id_from_url"
        or reason == "url_from_id"
        or reason.startswith("invalid_date_")
        for reason in outcome.reason_codes
    )


def _http_error_code(status: object) -> str:
    if isinstance(status, int) and 300 <= status < 400:
        return "unsafe_redirect"
    if status == 403:
        return "http_forbidden"
    if status == 429:
        return "rate_limited"
    return "http_error"


def _feed_items(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise ValueError("feed must be an object")
    if payload.get("version") != "https://jsonfeed.org/version/1":
        raise ValueError("unsupported feed version")
    if not isinstance(payload.get("title"), str):
        raise ValueError("feed title must be a string")
    values = payload.get("items")
    if not isinstance(values, list):
        raise ValueError("feed items must be an array")
    if len(values) > MAX_FEED_ITEMS:
        raise ValueError("feed item limit exceeded")
    return values


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _normalize_item(
    value: object,
    *,
    observed_at: datetime,
) -> V2exItemOutcome:
    if not isinstance(value, dict):
        return _invalid_outcome("item_not_object")
    reason_codes: list[str] = []
    raw_id = _optional_string(value, "id")
    raw_url = _optional_string(value, "url")
    validated_url = _v2ex_topic_url(raw_url)
    validated_id_url = _v2ex_topic_url(raw_id)
    source_url = validated_url or validated_id_url
    if (
        "url" in value
        and value.get("url") is not None
        and validated_url is None
        and validated_id_url is not None
    ):
        reason_codes.append("url_from_id")
    safe_id = _safe_identity(raw_id)
    if safe_id is not None:
        item_id = safe_id
    elif _v2ex_topic_url(raw_url) is not None:
        item_id = raw_url
        reason_codes.append("id_from_url")
    else:
        return _invalid_outcome(
            "missing_identity",
            source_url=source_url,
            safe_text=_redact_contacts(_optional_string(value, "title") or ""),
        )
    if source_url is None:
        return _invalid_outcome(
            "invalid_source_url",
            identity=item_id,
            safe_text=_redact_contacts(_optional_string(value, "title") or ""),
        )
    title = _optional_string(value, "title") or ""
    content_html = _optional_string(value, "content_html")
    content_text = _optional_string(value, "content_text")
    if content_html is None and content_text is None:
        return _invalid_outcome(
            "missing_content",
            identity=item_id,
            source_url=source_url,
            safe_text=_redact_contacts(title),
        )
    html = content_html or ""
    html_truncated = len(html) > MAX_HTML_CHARS
    if content_html is not None:
        parser = _PlainTextParser()
        parser.feed(html[:MAX_HTML_CHARS])
        parsed_content = parser.text()
    else:
        parsed_content = content_text or ""
    title = _redact_contacts(title)
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rstrip()
        reason_codes.append("title_truncated")
    description = _redact_contacts(parsed_content)
    combined = f"{title}\n{description}"
    text_truncated = len(combined) > MAX_SAFE_TEXT_CHARS
    if text_truncated:
        combined = combined[:MAX_SAFE_TEXT_CHARS].rstrip()
        description_limit = max(
            0,
            MAX_SAFE_TEXT_CHARS - len(title) - 1,
        )
        description = description[:description_limit].rstrip()
    if html_truncated or text_truncated:
        reason_codes.append("body_truncated")
    signals = _extract_signals(combined)
    classification, confidence, classification_reasons = _classify(
        combined,
        signals,
    )
    reason_codes.extend(classification_reasons)
    published_at = _validated_rfc3339(
        value.get("date_published"),
        "date_published",
        reason_codes,
    )
    modified_at = _validated_rfc3339(
        value.get("date_modified"),
        "date_modified",
        reason_codes,
    )
    company_match = re.search(
        r"(?:公司|公司名称)\s*[:：]\s*([^\n，,；;]{2,80})",
        description,
    )
    company = company_match.group(1).strip() if company_match else None
    job = None
    if confidence == "high" and classification in {"recruitment", "referral"}:
        risk_tags = _risk_tags(combined, signals)
        if signals.internship:
            employment_type = "intern"
        elif signals.part_time:
            employment_type = "part_time"
        elif signals.contract:
            employment_type = "contract"
        elif re.search(r"(?:全职|full[- ]?time)", combined, re.IGNORECASE):
            employment_type = "full_time"
        else:
            employment_type = None
        job = NormalizedJob(
            source="v2ex",
            source_job_id=item_id,
            source_url=source_url,
            official_apply_url=None,
            title=title,
            company=company,
            locations=signals.locations,
            recruitment_channel=(
                "internship" if signals.internship else "social"
            ),
            employment_type=employment_type,
            work_mode="remote" if signals.remote else None,
            description=description,
            responsibilities=(),
            requirements=(),
            salary_text=signals.salary_text,
            published_at=published_at,
            expires_at=signals.deadline_text,
            last_verified_at=observed_at.isoformat(),
            campus=None,
            internship={"explicit": True} if signals.internship else None,
            raw_data={
                "feed_item_id": item_id,
                "classification": classification,
                "risk_tags": list(risk_tags),
                "date_modified": modified_at,
            },
            detail_status="partial",
        )
    risk_tags = _risk_tags(combined, signals)
    return V2exItemOutcome(
        identity=item_id,
        source_url=source_url,
        classification=classification,
        confidence=confidence,
        disposition="normalized" if job is not None else "review",
        risk_tags=risk_tags,
        signals=signals,
        safe_text=combined,
        job=job,
        reason_codes=tuple(reason_codes),
        published_at=published_at,
    )


def _candidate_identity(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    if isinstance(item_id, str):
        safe_id = _safe_identity(item_id.strip())
        if safe_id is not None:
            return safe_id
    source_url = value.get("url")
    if isinstance(source_url, str):
        return _v2ex_topic_url(source_url.strip())
    return None


def _invalid_outcome(
    reason: str,
    *,
    identity: str | None = None,
    source_url: str | None = None,
    safe_text: str = "",
) -> V2exItemOutcome:
    risk_tags = (
        ("community_post", "identity_unverified", "missing_deadline")
        if identity is not None or source_url is not None
        else ()
    )
    return V2exItemOutcome(
        identity=identity,
        source_url=source_url,
        classification="uncertain",
        confidence="low",
        disposition="invalid",
        risk_tags=risk_tags,
        signals=V2exSignals(),
        safe_text=safe_text[:MAX_SAFE_TEXT_CHARS],
        job=None,
        reason_codes=(reason,),
    )


def _v2ex_topic_url(value: str | None) -> str | None:
    if (
        value is None
        or len(value) > 512
        or any(ord(character) <= 32 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        invalid_authority = (
            parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
        )
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.v2ex.com"
        or invalid_authority
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/t/\d+", parsed.path)
    ):
        return None
    return value


def _safe_identity(value: str | None) -> str | None:
    if (
        value is None
        or not value
        or len(value) > 512
        or any(character.isspace() or ord(character) < 32 for character in value)
        or _redact_contacts(value) != value
        or _is_encoded_contact(value)
    ):
        return None
    return value


def _is_encoded_contact(value: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9+/]{12,}={0,2}", value):
        return False
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return _redact_contacts(decoded) != decoded


def _redact_contacts(text: str) -> str:
    patterns = (
        (
            r"(?i)(?:简历|履历|联系(?:方式)?|邮箱|邮件|e-?mail|"
            r"微信|wechat|wx|vx)[^\n]{0,40}?"
            r"[A-Z0-9+/]{16,}={0,2}"
        ),
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)",
        (
            r"(?i)(?:微信|微\s*信|vx|v信)(?:号)?\s*"
            r"(?:[:：]|\s)\s*[^\s，,；;。]{1,64}"
        ),
        r"(?i)QQ\s*[:：]?\s*[1-9]\d{4,11}",
    )
    result = text
    for pattern in patterns:
        result = re.sub(pattern, "[联系方式已移除]", result)
    return result


def _extract_signals(text: str) -> V2exSignals:
    labelled_locations = re.findall(
        r"(?:工作地点|地点|base)\s*[:：]\s*"
        r"([^\n，,；;。]{2,20})",
        text,
        re.IGNORECASE,
    )
    title = text.splitlines()[0] if text else ""
    bracket_values = tuple(
        value.strip()
        for value in re.findall(
            r"[\[【（(]([^\]】）)]{2,20})[\]】）)]",
            title,
        )
    )
    bracket_locations = (
        value
        for value in bracket_values
        if value in _KNOWN_LOCATIONS
    )
    locations = tuple(
        dict.fromkeys(
            value.strip()
            for value in (*labelled_locations, *bracket_locations)
        )
    )
    salary_match = re.search(
        r"(?:薪资|待遇)\s*[:：]\s*([^\n，,；;。]{2,40})",
        text,
        re.IGNORECASE,
    )
    if salary_match is None:
        salary_match = re.search(
            r"(?<![A-Za-z0-9])("
            r"\d{1,3}(?:\.\d+)?\s*[kK万千元]?"
            r"\s*[-~–—到至]\s*"
            r"\d{1,3}(?:\.\d+)?\s*[kK万千元]"
            r"\s*/\s*(?:月|年|天|日|小时)"
            r")",
            text,
        )
    deadline_match = re.search(
        r"(?:申请截止|截止日期|截止)\s*[:：]\s*"
        r"(\d{4}-\d{2}-\d{2})",
        text,
    )
    deadline_text = None
    if deadline_match is not None:
        candidate = deadline_match.group(1)
        try:
            date.fromisoformat(candidate)
        except ValueError:
            pass
        else:
            deadline_text = candidate
    return V2exSignals(
        remote=bool(
            re.search(
                r"(?:远程|remote|work from home|居家办公)",
                text,
                re.IGNORECASE,
            )
        ),
        internship=bool(
            re.search(r"(?:实习|internship|intern\b)", text, re.IGNORECASE)
        ),
        part_time=bool(
            re.search(r"(?:兼职|part[- ]?time)", text, re.IGNORECASE)
        ),
        contract=bool(
            re.search(r"(?:合同工|contractor|短期合同)", text, re.IGNORECASE)
        ),
        foreign_company_or_team=bool(
            re.search(
                r"(?:外企|海外团队|国外团队|国际团队|"
                r"international team|global team)",
                text,
                re.IGNORECASE,
            )
        ),
        locations=locations,
        salary_text=(
            salary_match.group(1).strip() if salary_match is not None else None
        ),
        deadline_text=deadline_text,
    )


def _classify(
    text: str,
    signals: V2exSignals,
) -> tuple[
    Classification,
    Literal["high", "low"],
    tuple[str, ...],
]:
    recruitment = bool(
        re.search(r"(?:招聘|诚聘|招募|招聘岗位)", text, re.IGNORECASE)
    )
    role = bool(
        re.search(
            r"(?:工程师|开发(?:者)?|产品经理|设计师|运营(?:专员|经理)?|"
            r"测试(?:工程师)?|算法(?:工程师)?|销售(?:经理|专员)?|"
            r"市场(?:经理|专员)?|客服|编辑|研究员|分析师|实习生|"
            r"engineer|developer|designer)",
            text,
            re.IGNORECASE,
        )
    )
    referral = bool(re.search(r"(?:内推|可内推|推荐岗位)", text))
    job_seeking = bool(
        re.search(
            r"(?:求职|找工作|寻找工作机会|个人简历|寻求工作机会)",
            text,
            re.IGNORECASE,
        )
    )
    outsourcing = _has_outsourcing_signal(text)
    advertisement = bool(
        re.search(
            r"(?:广告|推广|培训|招聘服务|课程.{0,8}优惠)",
            text,
            re.IGNORECASE,
        )
    )
    discussion = bool(
        re.search(
            r"(?:讨论|请教|大家如何看|怎么看|吐槽|经验交流)",
            text,
        )
    )
    second_job_fact = bool(
        signals.locations
        or signals.salary_text
        or signals.remote
        or signals.internship
        or signals.part_time
        or signals.contract
        or signals.foreign_company_or_team
        or
        re.search(
            r"(?:(?:公司|公司名称|工作地点|地点|base|薪资|待遇)"
            r"\s*[:：]|全职|实习|兼职|合同工|contractor|"
            r"remote|远程|海外团队|国外团队|国际团队)",
            text,
            re.IGNORECASE,
        )
    )
    conflicts = sum((job_seeking, outsourcing, advertisement))
    if recruitment and role and conflicts:
        return "uncertain", "low", ("conflicting_category_signals",)
    if referral and recruitment and role and second_job_fact:
        return "referral", "high", ()
    if recruitment and role and second_job_fact:
        return "recruitment", "high", ()
    if recruitment:
        return "uncertain", "low", ("insufficient_job_evidence",)
    if job_seeking:
        return "job_seeking", "low", ()
    if outsourcing:
        return "outsourcing", "low", ()
    if advertisement:
        return "advertisement", "low", ()
    if discussion:
        return "discussion", "low", ()
    return "uncertain", "low", ()


def _risk_tags(text: str, signals: V2exSignals) -> tuple[str, ...]:
    tags = ["community_post", "identity_unverified"]
    if _has_outsourcing_signal(text):
        tags.append("possible_outsourcing")
    if signals.deadline_text is None:
        tags.append("missing_deadline")
    return tuple(tags)


def _has_outsourcing_signal(text: str) -> bool:
    return bool(
        re.search(
            r"(?:外包|项目需求|预算.{0,12}找.{0,6}开发|接单)",
            text,
            re.IGNORECASE,
        )
    )


def _validated_rfc3339(
    value: object,
    field: str,
    reason_codes: list[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        reason_codes.append(f"invalid_{field}")
        return None
    text = value.strip()
    if _RFC3339_PATTERN.fullmatch(text) is None:
        reason_codes.append(f"invalid_{field}")
        return None
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        reason_codes.append(f"invalid_{field}")
        return None
    if parsed.tzinfo is None:
        reason_codes.append(f"invalid_{field}")
        return None
    return text


def _optional_string(value: dict[str, object], key: str) -> str | None:
    result = value.get(key)
    return result.strip() if isinstance(result, str) and result.strip() else None
