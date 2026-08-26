import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from resume_agent.rewrite_schemas import CodexRewriteResult, MissingInformation


PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
DATE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?(?!\d)"
    r"|(?<!\d)(?:19|20)\d{2}年(?:\d{1,2}月(?:\d{1,2}日?)?)?"
)
VERSION = re.compile(
    r"\b(?:Python|Java|Node(?:\.js)?)\s+v?\d+(?:\.\d+){0,3}\b", re.I
)
METRIC = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\d+(?:\.\d+)?"
    r"(?:\s*(?:-|–|—|~|～|至|到)\s*\d+(?:\.\d+)?)?"
    r"\s*(?:%|\+|ms|毫秒|s|秒|分钟|小时|个(?:节点|字段)?|项|轮|维|"
    r"种(?:状态)?|字段|节点|倍|次)"
    r"(?![A-Za-z0-9])",
    re.I,
)
QUESTION_ID = re.compile(r"^(?:metric|gap|conflict)-[0-9a-f]{12}$")
CONFLICT_TERMS = ("冲突", "矛盾", "不一致", "conflict", "inconsistent", "contradict")


@dataclass(frozen=True)
class FactGateResult:
    blockers: list[MissingInformation]
    audit_markdown: str


class InvalidModelResult(ValueError):
    pass


def stable_metric_question_id(claim: str, metric: str) -> str:
    normalized = _normalize_for_hash(f"{claim}|{metric}")
    return "metric-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def extract_business_metrics(markdown: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for raw_line in markdown.splitlines():
        claim = raw_line.strip().lstrip("-#* ").strip()
        if not claim:
            continue
        scrubbed = PHONE.sub("", claim)
        scrubbed = DATE.sub("", scrubbed)
        scrubbed = VERSION.sub("", scrubbed)
        matches.extend(
            (claim, match.group(0).strip()) for match in METRIC.finditer(scrubbed)
        )
    return matches


def deduplicate_questions(
    items: Sequence[MissingInformation],
) -> list[MissingInformation]:
    unique: dict[str, MissingInformation] = {}
    for item in items:
        unique.setdefault(item.question_id, item)
    return list(unique.values())


def render_fact_audit(
    result: CodexRewriteResult,
    blockers: Sequence[MissingInformation],
    declared_items: Sequence[MissingInformation] = (),
) -> str:
    lines = ["## 事实门禁", f"- Claim audit 数量：{len(result.claim_audit)}"]
    blocker_ids = {item.question_id for item in blockers}
    declared_ids = set()
    for item in declared_items:
        declared_ids.add(item.question_id)
        if item.question_id in blocker_ids:
            status = "阻断"
        elif item.blocking:
            status = "已回答"
        else:
            status = "非阻断"
        lines.append(f"- {status} `{item.question_id}`：{item.reason}")
    for item in blockers:
        if item.question_id not in declared_ids:
            lines.append(f"- 阻断 `{item.question_id}`：{item.reason}")
    if not blockers:
        lines.append("- 结果：通过")
    return "\n".join(lines) + "\n"


def validate_rewrite_facts(
    result: CodexRewriteResult,
    source_resumes: Sequence[Any],
    jobs: Sequence[Any],
    fact_answers: Sequence[Any],
) -> FactGateResult:
    allowed_resumes = {
        _field(item, "id"): _field(item, "content") for item in source_resumes
    }
    allowed_jobs = {_field(item, "id") for item in jobs}
    answers = _valid_answers_by_id(fact_answers)

    for audit in result.claim_audit:
        if not set(audit.source_resume_ids) <= allowed_resumes.keys():
            raise InvalidModelResult("claim_audit 引用了未选择的简历")
        if not set(audit.job_ids) <= allowed_jobs:
            raise InvalidModelResult("claim_audit 引用了未选择的岗位")
        mapped_sources = [
            allowed_resumes[source_id] for source_id in audit.source_resume_ids
        ]
        for quote in audit.source_quotes:
            if not quote.strip() or not any(quote in source for source in mapped_sources):
                raise InvalidModelResult("claim_audit 原文证据不存在于所映射的简历")
        basis = _clean_text(audit.measurement_basis)
        if basis and not _basis_has_evidence(
            basis, allowed_resumes.values(), answers.values()
        ):
            raise InvalidModelResult(
                "claim_audit measurement_basis 不存在于所选简历或候选人答案"
            )

    declared_items = [
        _normalize_declared_missing(item) for item in result.missing_information
    ]
    blockers = [
        item
        for item in declared_items
        if item.blocking and not _answer_has_evidence(answers.get(item.question_id))
    ]

    for claim, metric in extract_business_metrics(result.rewritten_resume_markdown):
        audits = [
            item
            for item in result.claim_audit
            if metric in item.claim and _claims_overlap(claim, item.claim)
        ]
        question_id = stable_metric_question_id(claim, metric)
        answered = _answer_has_evidence(answers.get(question_id))
        basis_in_source = (
            len(audits) == 1
            and bool(_clean_text(audits[0].measurement_basis))
            and any(
                _contains_basis(
                    allowed_resumes[source_id], audits[0].measurement_basis or ""
                )
                for source_id in audits[0].source_resume_ids
            )
        )
        valid_audit = len(audits) == 1 and (
            (audits[0].fact_status == "source_verified" and basis_in_source)
            or (audits[0].fact_status == "user_confirmed" and answered)
        )
        if not valid_audit and not answered:
            blockers.append(
                MissingInformation(
                    question_id=question_id,
                    question=f"请说明“{claim}”中 {metric} 的统计口径和可核验来源",
                    blocking=True,
                    reason="业务指标缺少唯一 claim_audit 或可核验 measurement_basis",
                )
            )

    unique_blockers = deduplicate_questions(blockers)
    return FactGateResult(
        blockers=unique_blockers,
        audit_markdown=render_fact_audit(result, unique_blockers, declared_items),
    )


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item[name]
    return getattr(item, name)


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_for_hash(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _stable_declared_question_id(item: MissingInformation) -> str:
    text = f"{item.question} {item.reason}".lower()
    prefix = "conflict" if any(term in text for term in CONFLICT_TERMS) else "gap"
    normalized = _normalize_for_hash(f"{item.question}|{item.reason}")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _normalize_declared_missing(item: MissingInformation) -> MissingInformation:
    return MissingInformation(
        question_id=_stable_declared_question_id(item),
        question=item.question,
        blocking=item.blocking,
        reason=item.reason,
    )


def _valid_answers_by_id(fact_answers: Sequence[Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for item in fact_answers:
        question_id = _clean_text(_field(item, "question_id"))
        if QUESTION_ID.fullmatch(question_id):
            answers[question_id] = item
    return answers


def _answer_has_evidence(answer: Any) -> bool:
    return bool(
        answer is not None
        and _clean_text(_field(answer, "answer"))
        and _clean_text(_field(answer, "source_note"))
    )


def _contains_basis(container: str, basis: str) -> bool:
    compact_container = re.sub(r"\s+", "", container)
    compact_basis = re.sub(r"\s+", "", basis)
    return bool(compact_basis and compact_basis in compact_container)


def _basis_has_evidence(
    basis: str, source_texts: Iterable[str], answers: Iterable[Any]
) -> bool:
    if any(_contains_basis(source, basis) for source in source_texts):
        return True
    return any(
        _answer_has_evidence(answer)
        and (
            _contains_basis(_field(answer, "answer"), basis)
            or _contains_basis(_field(answer, "source_note"), basis)
        )
        for answer in answers
    )


def _claims_overlap(markdown_claim: str, audit_claim: str) -> bool:
    normalized_markdown = _normalize_for_hash(markdown_claim)
    normalized_audit = _normalize_for_hash(audit_claim)
    return bool(
        normalized_markdown
        and normalized_audit
        and (
            normalized_markdown in normalized_audit
            or normalized_audit in normalized_markdown
        )
    )
