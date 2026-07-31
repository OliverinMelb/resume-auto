"""Evidence-based ATS detection for an already-fetched public HTML snapshot."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit


AtsName = Literal[
    "json_ld",
    "greenhouse",
    "lever",
    "moka",
    "beisen",
    "hotjob",
    "workday",
    "sap_successfactors",
    "unknown",
]


@dataclass(frozen=True)
class AtsDetection:
    """A bounded candidate result with caller-auditable evidence."""

    ats: AtsName
    confidence: float
    evidence: tuple[str, ...]
    version_hint: str | None
    supports_json_ld: bool
    requires_browser: bool
    blocked_reason: str | None


@dataclass(frozen=True)
class _VendorRule:
    ats: AtsName
    official_hosts: tuple[str, ...]
    official_host_suffixes: tuple[str, ...]
    resource_host_suffixes: tuple[str, ...]
    generator_marker: str
    version_hint: str
    corroborated_host_suffixes: tuple[str, ...] = ()


_RULES = (
    _VendorRule(
        ats="greenhouse",
        official_hosts=(
            "boards.greenhouse.io",
            "boards.eu.greenhouse.io",
            "job-boards.greenhouse.io",
            "boards-api.greenhouse.io",
        ),
        official_host_suffixes=(),
        resource_host_suffixes=("greenhouse.io",),
        generator_marker="greenhouse job board",
        version_hint="job_board",
    ),
    _VendorRule(
        ats="lever",
        official_hosts=("jobs.lever.co", "jobs.eu.lever.co"),
        official_host_suffixes=(),
        resource_host_suffixes=("lever.co",),
        generator_marker="lever postings",
        version_hint="postings",
    ),
    _VendorRule(
        ats="moka",
        official_hosts=(),
        official_host_suffixes=("mokahr.com",),
        resource_host_suffixes=("mokahr.com",),
        generator_marker="moka career site",
        version_hint="career_site",
    ),
    _VendorRule(
        ats="beisen",
        official_hosts=(),
        official_host_suffixes=("beisen.com",),
        resource_host_suffixes=("beisen.com",),
        generator_marker="beisen career site",
        version_hint="career_site",
        corroborated_host_suffixes=("zhiye.com",),
    ),
    _VendorRule(
        ats="hotjob",
        official_hosts=(),
        official_host_suffixes=("hotjob.cn",),
        resource_host_suffixes=("hotjob.cn",),
        generator_marker="wintalent hotjob",
        version_hint="legacy",
    ),
    _VendorRule(
        ats="workday",
        official_hosts=(),
        official_host_suffixes=("myworkdayjobs.com",),
        resource_host_suffixes=("myworkdayjobs.com",),
        generator_marker="workday external career site",
        version_hint="external_career_site",
    ),
    _VendorRule(
        ats="sap_successfactors",
        official_hosts=(),
        official_host_suffixes=(
            "successfactors.com",
            "successfactors.eu",
            "jobs.hr.cloud.sap.com",
            "jobs.hr.sapcloud.cn",
        ),
        resource_host_suffixes=(
            "successfactors.com",
            "successfactors.eu",
            "sapcloud.cn",
        ),
        generator_marker="sap successfactors career site builder",
        version_hint="career_site_builder",
    ),
)

_RULE_BY_ATS = {rule.ats: rule for rule in _RULES}


class _PageSnapshotParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.resources: list[str] = []
        self.generators: list[str] = []
        self.robots_directives: list[str] = []
        self.json_ld_blocks: list[str] = []
        self.visible_text: list[str] = []
        self.title_text: list[str] = []
        self.noscript_text: list[str] = []
        self.has_password_input = False
        self.has_app_root = False
        self.has_script = False
        self.hotjob_version: str | None = None
        self._json_ld_parts: list[str] | None = None
        self._hidden_depth = 0
        self._in_title = False
        self._in_noscript = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        attributes = {
            name.casefold(): value or ""
            for name, value in attrs
        }

        for name in ("src", "href"):
            value = attributes.get(name)
            if value:
                self.resources.append(value)

        if tag == "meta":
            name = attributes.get("name", "").casefold()
            content = attributes.get("content", "")
            if name == "generator":
                self.generators.append(content)
            if name in {"robots", "googlebot", "bingbot"}:
                self.robots_directives.append(content)

        if tag == "input" and attributes.get("type", "").casefold() == "password":
            self.has_password_input = True

        if attributes.get("id", "").casefold() in {"app", "root", "__next"}:
            self.has_app_root = True

        version = attributes.get("data-hotjob-version")
        if version:
            self.hotjob_version = version.casefold()

        if tag == "script":
            self.has_script = True
            script_type = attributes.get("type", "").split(";", 1)[0]
            if script_type.strip().casefold() == "application/ld+json":
                self._json_ld_parts = []
            else:
                self._hidden_depth += 1
        elif tag == "style":
            self._hidden_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "noscript":
            self._in_noscript = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "script":
            if self._json_ld_parts is not None:
                self.json_ld_blocks.append("".join(self._json_ld_parts))
                self._json_ld_parts = None
            elif self._hidden_depth:
                self._hidden_depth -= 1
        elif tag == "style" and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "noscript":
            self._in_noscript = False

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)
            return
        if self._in_title:
            self.title_text.append(data)
        if self._in_noscript:
            self.noscript_text.append(data)
        if not self._hidden_depth:
            self.visible_text.append(data)


def detect_ats(
    url: str,
    html: str,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> AtsDetection:
    """Detect an ATS from one caller-approved URL and its HTTP snapshot."""
    hostname = _http_hostname(url)
    if hostname is None:
        return _unknown_result(
            blocked_reason="invalid_url",
        )

    parser = _PageSnapshotParser()
    parser.feed(html or "")
    parser.close()

    supports_json_ld, malformed_json_ld = _json_ld_status(
        parser.json_ld_blocks
    )
    restriction = _blocked_reason(
        status_code=status_code,
        headers=headers,
        parser=parser,
    )

    official_rule = next(
        (rule for rule in _RULES if _is_official_host(hostname, rule)),
        None,
    )
    resource_candidates, resource_evidence = _resource_candidates(
        parser.resources
    )
    generator_candidates, generator_evidence = _generator_candidates(
        parser.generators
    )
    custom_candidates = _ordered_unique(
        (*resource_candidates, *generator_candidates)
    )

    if restriction is not None:
        if official_rule is not None:
            return AtsDetection(
                ats=official_rule.ats,
                confidence=0.97,
                evidence=(f"hostname:{hostname}",),
                version_hint=None,
                supports_json_ld=False,
                requires_browser=False,
                blocked_reason=restriction,
            )
        return _unknown_result(blocked_reason=restriction)

    if official_rule is not None:
        evidence = [f"hostname:{hostname}"]
        evidence.extend(
            item
            for ats, item in resource_evidence
            if ats == official_rule.ats
        )
        evidence.extend(
            item
            for ats, item in generator_evidence
            if ats == official_rule.ats
        )
        if supports_json_ld:
            evidence.append("json_ld:@type=JobPosting")
        elif malformed_json_ld:
            evidence.append("json_ld:malformed")
        return AtsDetection(
            ats=official_rule.ats,
            confidence=0.97,
            evidence=tuple(_ordered_unique(evidence)),
            version_hint=_version_hint(official_rule, parser),
            supports_json_ld=supports_json_ld,
            requires_browser=(
                restriction is None and _requires_browser(parser)
            ),
            blocked_reason=restriction,
        )

    if len(custom_candidates) > 1:
        conflict = f"conflict:{','.join(custom_candidates)}"
        evidence = [
            *(item for _, item in resource_evidence),
            *(item for _, item in generator_evidence),
            conflict,
        ]
        if supports_json_ld:
            evidence.append("json_ld:@type=JobPosting")
        elif malformed_json_ld:
            evidence.append("json_ld:malformed")
        return AtsDetection(
            ats="unknown",
            confidence=0.2,
            evidence=tuple(_ordered_unique(evidence)),
            version_hint=None,
            supports_json_ld=supports_json_ld,
            requires_browser=(
                restriction is None and _requires_browser(parser)
            ),
            blocked_reason=None,
        )

    if custom_candidates:
        ats = custom_candidates[0]
        rule = _RULE_BY_ATS[ats]
        matching_resource_evidence = [
            item for candidate, item in resource_evidence if candidate == ats
        ]
        matching_generator_evidence = [
            item for candidate, item in generator_evidence if candidate == ats
        ]
        evidence = [
            *matching_resource_evidence,
            *matching_generator_evidence,
        ]
        corroborated_hostname = bool(
            matching_resource_evidence
            and any(
                _host_matches(hostname, suffix)
                for suffix in rule.corroborated_host_suffixes
            )
        )
        if corroborated_hostname:
            evidence.insert(0, f"candidate_hostname:{hostname}")
        if supports_json_ld:
            evidence.append("json_ld:@type=JobPosting")
        elif malformed_json_ld:
            evidence.append("json_ld:malformed")
        has_two_kinds = bool(
            matching_resource_evidence and matching_generator_evidence
        )
        if corroborated_hostname:
            confidence = 0.75
        elif has_two_kinds:
            confidence = 0.65
        else:
            confidence = 0.55
        return AtsDetection(
            ats=ats,
            confidence=confidence,
            evidence=tuple(_ordered_unique(evidence)),
            version_hint=_version_hint(rule, parser),
            supports_json_ld=supports_json_ld,
            requires_browser=(
                restriction is None and _requires_browser(parser)
            ),
            blocked_reason=restriction,
        )

    if supports_json_ld:
        return AtsDetection(
            ats="json_ld",
            confidence=0.9,
            evidence=("json_ld:@type=JobPosting",),
            version_hint=None,
            supports_json_ld=True,
            requires_browser=False,
            blocked_reason=restriction,
        )

    evidence = ("json_ld:malformed",) if malformed_json_ld else ()
    return AtsDetection(
        ats="unknown",
        confidence=0.0,
        evidence=evidence,
        version_hint=None,
        supports_json_ld=False,
        requires_browser=restriction is None and _requires_browser(parser),
        blocked_reason=None,
    )


def _unknown_result(*, blocked_reason: str) -> AtsDetection:
    return AtsDetection(
        ats="unknown",
        confidence=0.0,
        evidence=(),
        version_hint=None,
        supports_json_ld=False,
        requires_browser=False,
        blocked_reason=blocked_reason,
    )


def _http_hostname(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return hostname.rstrip(".").casefold()


def _host_matches(hostname: str, expected: str) -> bool:
    return hostname == expected or hostname.endswith(f".{expected}")


def _is_official_host(hostname: str, rule: _VendorRule) -> bool:
    return hostname in rule.official_hosts or any(
        _host_matches(hostname, suffix)
        for suffix in rule.official_host_suffixes
    )


def _resource_candidates(
    resources: list[str],
) -> tuple[list[AtsName], list[tuple[AtsName, str]]]:
    candidates: list[AtsName] = []
    evidence: list[tuple[AtsName, str]] = []
    for resource in resources:
        try:
            parsed = urlsplit(resource)
            hostname = parsed.hostname
        except ValueError:
            continue
        if hostname:
            hostname = hostname.rstrip(".").casefold()
            for rule in _RULES:
                if any(
                    _host_matches(hostname, suffix)
                    for suffix in rule.resource_host_suffixes
                ):
                    candidates.append(rule.ats)
                    evidence.append(
                        (rule.ats, f"resource_host:{hostname}")
                    )
                    break
        elif "/wday/" in parsed.path.casefold():
            candidates.append("workday")
            evidence.append(("workday", "resource_path:/wday/"))
    return candidates, evidence


def _generator_candidates(
    generators: list[str],
) -> tuple[list[AtsName], list[tuple[AtsName, str]]]:
    candidates: list[AtsName] = []
    evidence: list[tuple[AtsName, str]] = []
    for generator in generators:
        normalized = " ".join(generator.casefold().split())
        for rule in _RULES:
            if rule.generator_marker in normalized:
                candidates.append(rule.ats)
                evidence.append(
                    (rule.ats, f"generator:{rule.generator_marker}")
                )
                break
    return candidates, evidence


def _version_hint(
    rule: _VendorRule,
    parser: _PageSnapshotParser,
) -> str | None:
    normalized_generators = {
        " ".join(value.casefold().split())
        for value in parser.generators
    }
    if any(
        rule.generator_marker in value
        for value in normalized_generators
    ):
        return rule.version_hint
    if rule.ats == "hotjob" and parser.hotjob_version == "legacy":
        return "legacy"
    return None


def _json_ld_status(blocks: list[str]) -> tuple[bool, bool]:
    malformed = False
    for block in blocks:
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            malformed = True
            continue
        if _contains_job_posting(payload):
            return True, malformed
    return False, malformed


def _contains_job_posting(value: object) -> bool:
    if isinstance(value, dict):
        type_value = value.get("@type")
        if type_value == "JobPosting":
            return True
        if isinstance(type_value, list) and "JobPosting" in type_value:
            return True
        return any(_contains_job_posting(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_job_posting(item) for item in value)
    return False


def _blocked_reason(
    *,
    status_code: int,
    headers: Mapping[str, str] | None,
    parser: _PageSnapshotParser,
) -> str | None:
    if status_code in {401, 403}:
        return f"http_{status_code}"
    if status_code == 429:
        return "rate_limited"

    header_values = {
        str(name).casefold(): str(value).casefold()
        for name, value in (headers or {}).items()
    }
    robot_values = [
        *parser.robots_directives,
        header_values.get("x-robots-tag", ""),
    ]
    if any(
        "noindex" in {
            token.strip()
            for token in value.casefold().replace(";", ",").split(",")
        }
        for value in robot_values
    ):
        return "noindex"

    page_text = " ".join(
        (*parser.title_text, *parser.visible_text)
    ).casefold()
    if any(
        marker in page_text
        for marker in ("captcha", "验证码", "安全验证", "人机验证")
    ):
        return "captcha_or_verification"
    if parser.has_password_input and any(
        marker in page_text
        for marker in (
            "sign in required",
            "login required",
            "请登录",
            "登录后",
        )
    ):
        return "login_required"
    return None


def _requires_browser(parser: _PageSnapshotParser) -> bool:
    noscript_text = " ".join(parser.noscript_text).casefold()
    explicit_javascript_message = (
        "enable javascript" in noscript_text
        or "启用 javascript" in noscript_text
        or "启用javascript" in noscript_text
    )
    visible_text = " ".join(parser.visible_text).strip()
    resource_locations: list[tuple[str, str]] = []
    for resource in parser.resources:
        try:
            parsed = urlsplit(resource)
        except ValueError:
            continue
        resource_locations.append(
            (
                (parsed.hostname or "").rstrip(".").casefold(),
                parsed.path.casefold(),
            )
        )
    has_workday_cxs_shell = any(
        _host_matches(hostname, "myworkdaycdn.com")
        and "cx-jobs" in path
        for hostname, path in resource_locations
    )
    has_successfactors_dynamic_shell = any(
        any(
            _host_matches(hostname, suffix)
            for suffix in ("successfactors.com", "successfactors.eu")
        )
        and (
            "/platform/js/j2w/" in path
            or "/platform/csb/" in path
        )
        for hostname, path in resource_locations
    )
    return (
        explicit_javascript_message
        or (
            parser.has_app_root
            and parser.has_script
            and len(visible_text) < 40
        )
        or (
            has_workday_cxs_shell
            and len(visible_text) < 200
        )
        or (
            has_successfactors_dynamic_shell
            and (
                "loading..." in visible_text.casefold()
                or len(visible_text) < 80
            )
        )
    )


def _ordered_unique(values):
    return list(dict.fromkeys(values))
