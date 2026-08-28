"""Deterministic, restricted reports for a fact-gated resume rewrite."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote_to_bytes

from resume_agent.rewrite_schemas import CodexRewriteResult, GapAcceptancePlan
from resume_agent.research import KEYWORD_ALIASES


RESUME_ID_PATTERN = re.compile(r"^source_resume_[0-9a-f]{32}$")
JOB_ID_PATTERN = re.compile(r"^job_[0-9a-f]{32}$")
CANDIDATE_ID_PATTERN = re.compile(r"^candidate_[0-9a-f]{32}$")
ACCEPTANCE_ID_PATTERN = re.compile(r"^gap_acceptance_[0-9a-f]{32}$")
GAP_ID_PATTERN = re.compile(r"^gap-[0-9a-f]{12}$")
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.!#$%&'*+/=?^`{|}~-]+@"
    r"(?:[\w-]+\.)+[A-Za-z]{2,63}(?![\w.-])",
    re.IGNORECASE,
)
CN_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
CONTINUOUS_PHONE_PATTERN = re.compile(r"(?<!\d)\d{10,15}(?!\d)")
INTERNATIONAL_PHONE_PATTERN = re.compile(
    r"(?<![\w])\+\d(?:[\d ().-]{7,}\d)(?![\w])"
)
SEPARATED_PHONE_PATTERN = re.compile(
    r"(?<![\w])(?:\(\d{2,4}\)|\d{2,4})[ .-]\d{3,4}[ .-]\d{4}(?![\w])"
)
URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s<>()]+")
WINDOWS_PATH_PATTERN = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s,;，；]+"
)
USER_PATH_PATTERN = re.compile(r"(?<![\w])/(?:home|users)/[^\s,;，；]+", re.I)
UNIX_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s,;，；]+")
DIGEST_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])", re.I)
JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:"
    r"(?:[A-Za-z][A-Za-z0-9]*_)+(?:token|access_token|refresh_token|"
    r"session(?:_id)?|api_key|client_secret|password|passwd|secret)|"
    r"authorization|proxy-authorization|cookie|set-cookie|token|"
    r"access(?:[ _-]?token)|refresh(?:[ _-]?token)|"
    r"session(?:[ _-]?id)?|api(?:[ _-]?key)|client(?:[ _-]?secret)|"
    r"password|passwd|secret)(?![A-Za-z0-9_])\s*(?::|=)\s*"
    r"(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;，；]+)"
)
COMMAND_SECRET_PATTERN = re.compile(
    r"(?i)(--?(?:access[_-]?token|refresh[_-]?token|token|session(?:[_-]?id)?|"
    r"api[_-]?key|client[_-]?secret|password|passwd|secret))"
    r"(?:\s+|=)(?:\"[^\"]*\"|'[^']*'|[^\s,;，；]+)"
)
KNOWN_SECRET_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"AKIA[0-9A-Z]{16})(?![A-Za-z0-9_-])"
)
PERCENT_ESCAPE_PATTERN = re.compile(r"%[0-9A-Fa-f]{2}")
MALFORMED_PERCENT_PATTERN = re.compile(
    r"%(?![0-9A-Fa-f]{2})(?=[0-9A-Za-z]{1,2})"
)


class ReportValidationError(ValueError):
    """A deliberately context-free report validation failure."""

    def __init__(self) -> None:
        super().__init__("report input is invalid")


def _normalize_report_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"}
        or character in "\t\r\n"
    )


def _decode_percent_escapes(value: str) -> str | None:
    decoded = value
    for _ in range(4):
        if PERCENT_ESCAPE_PATTERN.search(decoded) is None:
            return None if MALFORMED_PERCENT_PATTERN.search(decoded) else decoded
        try:
            decoded = unquote_to_bytes(decoded).decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        decoded = _normalize_report_text(decoded)
    if (
        PERCENT_ESCAPE_PATTERN.search(decoded) is not None
        or MALFORMED_PERCENT_PATTERN.search(decoded) is not None
    ):
        return None
    return decoded


def sanitize_report_text(value: str) -> str:
    """Redact PII, credentials, URLs, paths and digests from report prose."""
    if not isinstance(value, str):
        raise ReportValidationError()
    normalized = _decode_percent_escapes(_normalize_report_text(value))
    if normalized is None:
        return "[REDACTED_UNSAFE]"
    normalized = CREDENTIAL_PATTERN.sub("[REDACTED_CREDENTIAL]", normalized)
    normalized = COMMAND_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED_CREDENTIAL]", normalized
    )
    normalized = KNOWN_SECRET_PATTERN.sub("[REDACTED_CREDENTIAL]", normalized)
    normalized = JWT_PATTERN.sub("[REDACTED_CREDENTIAL]", normalized)
    normalized = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", normalized)
    normalized = CN_PHONE_PATTERN.sub("[REDACTED_PHONE]", normalized)
    normalized = CONTINUOUS_PHONE_PATTERN.sub("[REDACTED_PHONE]", normalized)
    normalized = INTERNATIONAL_PHONE_PATTERN.sub("[REDACTED_PHONE]", normalized)
    normalized = SEPARATED_PHONE_PATTERN.sub("[REDACTED_PHONE]", normalized)
    normalized = URL_PATTERN.sub("[REDACTED_URL]", normalized)
    normalized = WINDOWS_PATH_PATTERN.sub("[REDACTED_PATH]", normalized)
    normalized = USER_PATH_PATTERN.sub("[REDACTED_PATH]", normalized)
    normalized = UNIX_PATH_PATTERN.sub("[REDACTED_PATH]", normalized)
    normalized = DIGEST_PATTERN.sub("[REDACTED_DIGEST]", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        raise ReportValidationError()
    return normalized.replace("<", "&lt;").replace(">", "&gt;")


def _validated_allowlist(
    values: Sequence[str], pattern: re.Pattern[str]
) -> set[str]:
    if isinstance(values, (str, bytes)):
        raise ReportValidationError()
    normalized = list(values)
    if (
        len(normalized) != len(set(normalized))
        or any(
            not isinstance(value, str) or not pattern.fullmatch(value)
            for value in normalized
        )
    ):
        raise ReportValidationError()
    return set(normalized)


def _validate_claim_sources(
    values: Sequence[str], allowed: set[str]
) -> list[str]:
    normalized = list(values)
    if len(normalized) != len(set(normalized)) or not set(normalized) <= allowed:
        raise ReportValidationError()
    return sorted(normalized)


def build_claim_ledger(
    result: CodexRewriteResult,
    allowed_resume_ids: Sequence[str],
    allowed_job_ids: Sequence[str],
    gap_acceptances: Sequence[dict[str, Any]],
    *,
    candidate_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return only the stable fields allowed in ``claim-ledger.json``."""
    allowed_resumes = _validated_allowlist(allowed_resume_ids, RESUME_ID_PATTERN)
    allowed_jobs = _validated_allowlist(allowed_job_ids, JOB_ID_PATTERN)
    accepted_pairs = _validated_acceptance_pairs(gap_acceptances, candidate_id)
    claims = [
        {
            "acceptance_id": audit.acceptance_id,
            "change_type": audit.change_type,
            "claim": sanitize_report_text(audit.claim),
            "claim_status": audit.claim_status,
            "gap_id": audit.gap_id,
            "source_job_ids": _validate_claim_sources(
                audit.job_ids, allowed_jobs
            ),
            "source_resume_ids": _validate_claim_sources(
                audit.source_resume_ids, allowed_resumes
            ),
        }
        for audit in result.claim_audit
    ]
    visual_claims = [item["claim"].casefold() for item in claims]
    if len(visual_claims) != len(set(visual_claims)):
        raise ReportValidationError()
    identities = [
        (
            item["claim"].casefold(),
            item["claim_status"],
            item["change_type"],
            tuple(item["source_resume_ids"]),
            tuple(item["source_job_ids"]),
            item["gap_id"],
            item["acceptance_id"],
        )
        for item in claims
    ]
    if len(identities) != len(set(identities)):
        raise ReportValidationError()
    for item in claims:
        if item["acceptance_id"] is not None and (
            item["acceptance_id"], item["gap_id"]
        ) not in accepted_pairs:
            raise ReportValidationError()
    claims.sort(
        key=lambda item: (
            item["claim"].casefold(),
            item["claim"],
            item["claim_status"],
            item["change_type"],
            tuple(item["source_resume_ids"]),
            tuple(item["source_job_ids"]),
            item["gap_id"] or "",
            item["acceptance_id"] or "",
        )
    )
    return {"claims": claims}


def _validated_acceptance_pairs(
    values: Sequence[dict[str, Any]], candidate_id: str | None
) -> set[tuple[str, str]]:
    if isinstance(values, (str, bytes)):
        raise ReportValidationError()
    normalized = list(values)
    if normalized and (
        not isinstance(candidate_id, str)
        or CANDIDATE_ID_PATTERN.fullmatch(candidate_id) is None
    ):
        raise ReportValidationError()
    pairs: list[tuple[str, str]] = []
    ids: list[str] = []
    gaps: list[str] = []
    for value in normalized:
        if not isinstance(value, dict):
            raise ReportValidationError()
        acceptance_id = value.get("id")
        acceptance_gap = value.get("gap_id")
        if (
            value.get("candidate_id") != candidate_id
            or not isinstance(acceptance_id, str)
            or ACCEPTANCE_ID_PATTERN.fullmatch(acceptance_id) is None
            or not isinstance(acceptance_gap, str)
            or GAP_ID_PATTERN.fullmatch(acceptance_gap) is None
        ):
            raise ReportValidationError()
        ids.append(acceptance_id)
        gaps.append(acceptance_gap)
        pairs.append((acceptance_id, acceptance_gap))
    if len(ids) != len(set(ids)) or len(gaps) != len(set(gaps)):
        raise ReportValidationError()
    return set(pairs)


def _safe_unique_lines(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ReportValidationError()
    raw = list(values)
    identities = [unicodedata.normalize("NFKC", value).casefold() for value in raw]
    if len(identities) != len(set(identities)):
        raise ReportValidationError()
    sanitized = [sanitize_report_text(value) for value in raw]
    deduplicated = {value.casefold(): value for value in sanitized}
    return sorted(
        deduplicated.values(), key=lambda value: (value.casefold(), value)
    )


def build_gap_roadmap(plans: Sequence[GapAcceptancePlan]) -> str:
    """Render only structured gap-plan fields; commands remain inert prose."""
    if isinstance(plans, (str, bytes)):
        raise ReportValidationError()
    normalized: list[GapAcceptancePlan] = []
    try:
        normalized = [GapAcceptancePlan.model_validate(plan) for plan in plans]
    except Exception:
        raise ReportValidationError() from None
    gap_ids = [plan.gap_id for plan in normalized]
    if len(gap_ids) != len(set(gap_ids)):
        raise ReportValidationError()

    sections = [
        "# 缺口路线图",
        "",
        "以下内容均为待验收计划；test_commands 仅作为命令建议文本展示，产品不会执行。",
    ]
    for plan in sorted(normalized, key=lambda item: item.gap_id):
        sections.extend(
            [
                "",
                f"## {plan.gap_id}",
                "- 状态：待验收",
                f"- JD 要求：{'；'.join(_safe_unique_lines(plan.jd_requirements))}",
                f"- 项目范围：{sanitize_report_text(plan.project_scope)}",
                f"- 所需产物：{'；'.join(_safe_unique_lines(plan.required_artifacts))}",
                f"- 验收标准：{'；'.join(_safe_unique_lines(plan.acceptance_metrics))}",
                f"- 面试主题：{'；'.join(_safe_unique_lines(plan.interview_topics)) or '无'}",
            ]
        )
        for command in _safe_unique_lines(plan.test_commands):
            sections.append(f"- 命令建议（未执行）：{command}")
    return "\n".join(sections) + "\n"


def _alias_in_text(alias: str, text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", alias).casefold()
    prefix = (
        r"(?<![a-z0-9])"
        if normalized and normalized[0].isascii() and normalized[0].isalnum()
        else ""
    )
    suffix = (
        r"(?![a-z0-9])"
        if normalized and normalized[-1].isascii() and normalized[-1].isalnum()
        else ""
    )
    return re.search(prefix + re.escape(normalized) + suffix, text) is not None


def _keyword_set(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return {
        canonical
        for canonical, aliases in KEYWORD_ALIASES.items()
        if any(_alias_in_text(alias, normalized) for alias in aliases)
    }


def _validated_report_sources(
    values: Sequence[dict[str, Any]],
    *,
    id_field_pattern: re.Pattern[str],
    text_fields: Sequence[str],
) -> tuple[list[str], str]:
    if isinstance(values, (str, bytes)):
        raise ReportValidationError()
    ids: list[str] = []
    texts: list[str] = []
    candidate_ids: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            raise ReportValidationError()
        source_id = value.get("id")
        candidate_id = value.get("candidate_id")
        if (
            not isinstance(source_id, str)
            or id_field_pattern.fullmatch(source_id) is None
            or not isinstance(candidate_id, str)
            or CANDIDATE_ID_PATTERN.fullmatch(candidate_id) is None
        ):
            raise ReportValidationError()
        ids.append(source_id)
        candidate_ids.append(candidate_id)
        for field in text_fields:
            text = value.get(field, "")
            if not isinstance(text, str):
                raise ReportValidationError()
            texts.append(text)
    if len(ids) != len(set(ids)) or len(set(candidate_ids)) > 1:
        raise ReportValidationError()
    return sorted(ids), "\n".join(texts)


def _render_keywords(keywords: set[str]) -> str:
    ordered = sorted(keywords, key=lambda value: (value.casefold(), value))
    return "、".join(ordered) if ordered else "无"


def _coverage_line(label: str, numerator: int, denominator: int) -> str:
    ratio = numerator / denominator if denominator else 0.0
    return f"- {label}：{numerator}/{denominator}（{ratio:.2%}）"


def build_ats_report(
    jobs: Sequence[dict[str, Any]],
    source_resumes: Sequence[dict[str, Any]],
    rewritten_resume_markdown: str,
) -> str:
    """Build deterministic lexical coverage; it is deliberately non-semantic."""
    job_ids, job_text = _validated_report_sources(
        jobs, id_field_pattern=JOB_ID_PATTERN, text_fields=("title", "detail")
    )
    resume_ids, resume_text = _validated_report_sources(
        source_resumes,
        id_field_pattern=RESUME_ID_PATTERN,
        text_fields=("content",),
    )
    del resume_ids
    candidate_ids = {
        value["candidate_id"] for value in [*list(jobs), *list(source_resumes)]
    }
    if len(candidate_ids) > 1 or not isinstance(rewritten_resume_markdown, str):
        raise ReportValidationError()

    jd_keywords = _keyword_set(job_text)
    original_keywords = _keyword_set(resume_text)
    rewritten_keywords = _keyword_set(rewritten_resume_markdown)
    original_matches = jd_keywords & original_keywords
    rewritten_matches = jd_keywords & rewritten_keywords
    missing = jd_keywords - rewritten_keywords
    selected_jobs = "、".join(job_ids) if job_ids else "无"
    return "\n".join(
        [
            "# ATS 确定性关键词报告",
            "",
            "本报告仅统计内置 alias 的字面命中，不是语义评分或通过承诺。",
            f"- 选用 JD：{selected_jobs}",
            f"- JD 样本关键词（{len(jd_keywords)}）：{_render_keywords(jd_keywords)}",
            (
                f"- 原简历关键词（{len(original_keywords)}）："
                f"{_render_keywords(original_keywords)}"
            ),
            (
                f"- 修改后关键词（{len(rewritten_keywords)}）："
                f"{_render_keywords(rewritten_keywords)}"
            ),
            _coverage_line(
                "JD 样本内置词典覆盖", len(jd_keywords), len(KEYWORD_ALIASES)
            ),
            _coverage_line("原简历覆盖", len(original_matches), len(jd_keywords)),
            _coverage_line("修改后覆盖", len(rewritten_matches), len(jd_keywords)),
            f"- 修改后未命中的 JD 确定性关键词：{_render_keywords(missing)}",
            "- 限制：未命中不等于缺失硬条件，命中也不证明候选人具备相应能力。",
            "",
        ]
    )
