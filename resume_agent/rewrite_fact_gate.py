import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from resume_agent.rewrite_schemas import CodexRewriteResult, MissingInformation


PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
DATE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日?)?)?"
    r"|(?<!\d)(?:19|20)\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?(?!\d)"
)
VERSION = re.compile(
    r"\b(?:Python|Java|Node(?:\.js)?|React)\s+v?\d+(?:\.\d+){0,3}\+?"
    r"(?![\w.])",
    re.I,
)
CALENDAR_MONTH = re.compile(
    r"(?<!\d)(?:0?[1-9]|1[0-2])\s*月(?=\s*(?:毕业|入学|入职|离职))"
)
METRIC = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])[$¥]\s*\d+(?:\.\d+)?(?:[KMB])?(?![A-Za-z0-9])"
    r"|"
    r"(?<![A-Za-z0-9])"
    r"(?:约\s*)?\d+(?:\.\d+)?"
    r"(?:\s*(?:-|–|—|~|～|至|到)\s*\d+(?:\.\d+)?)?"
    r"\s*(?:%|\+|qps|tps|rps|ms|毫秒|s|秒|分钟|小时|年|个月|月|天|"
    r"(?:万|亿)(?:用户|人|家|条|次|个|元)?|元|人|家|条|"
    r"美元|个(?:节点|字段)|(?:余)?(?:用户|人|家|条|项|次|个)|"
    r"项|轮|维|种(?:状态)?|字段|节点|倍|次)"
    r"(?:以上|以下|左右)?"
    r"(?![A-Za-z0-9])"
    r"|(?:十余|百余|千余|数十|数百|上千|近百|约百|百万|一亿)"
    r"(?:用户|人|家|项|次|条|元)"
    r")",
    re.I,
)
QUESTION_ID = re.compile(r"^(?:metric|gap|conflict)-[0-9a-f]{12}$")
CONFLICT_TERMS = ("冲突", "矛盾", "不一致", "conflict", "inconsistent", "contradict")
PLACEHOLDER_TERMS = (
    "不知道",
    "不清楚",
    "帮我补",
    "随便",
    "不记得",
)
PLACEHOLDER_EXACT = {"无", "不知道", "不清楚", "随便", "没有", "不记得"}
SOURCE_NOTE_TERMS = (
    "记录",
    "日志",
    "回归",
    "统计",
    "报告",
    "监控",
    "数据库",
    "仓库",
    "代码",
    "截图",
    "测量",
    "候选人确认",
)
QPS_MEASUREMENT_SOURCE_TERMS = ("压测", "监控", "负载测试", "性能测试")
STATISTICS_WINDOW_TERMS = (
    "统计窗口",
    "监控窗口",
    "采样",
    "平均",
    "p50",
    "p90",
    "p95",
    "p99",
)
MEASUREMENT_METHOD_TERMS = (
    "样本",
    "回归",
    "测试",
    "统计",
    "监控",
    "日志",
    "测量",
    "压测",
    "采样",
    "窗口",
)
OTHER_METRIC_ANSWER_TERMS = (
    "统计",
    "测量",
    "记录",
    "日志",
    "目录",
    "清单",
    "监控",
    "回归",
    "报告",
    "数据库",
    "仓库",
    "代码",
    "截图",
    "压测",
    "每秒",
    "窗口",
)
SECTION_HEADINGS = {
    "基本信息",
    "核心优势",
    "个人信息",
    "教育经历",
    "实习经历",
    "工作经历",
    "项目经历",
    "专业技能",
    "技能",
    "自我评价",
    "荣誉奖项",
    "证书",
    "联系方式",
    "求职意向",
}
MIN_ANSWER_LENGTH = 4
MIN_SOURCE_NOTE_LENGTH = 6
HEADING = re.compile(r"^\s{0,3}#{1,6}(?!#)")
HEADING_PREFIX = re.compile(r"^\s{0,3}#{1,6}(?!#)\s*")
STRUCTURAL_MARKDOWN = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|```.*)\s*$")
LIST_PREFIX = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+|>\s*)")
CHECKBOX_PREFIX = re.compile(r"^\[[ xX]\]\s+")


@dataclass(frozen=True)
class FactGateResult:
    blockers: list[MissingInformation]
    audit_markdown: str


class InvalidModelResult(ValueError):
    pass


@dataclass(frozen=True)
class _DraftLine:
    claim: str
    normalized: str


def stable_metric_question_id(claim: str, metric: str) -> str:
    normalized = _normalize_for_hash(f"{claim}|{metric}")
    return "metric-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def extract_business_metrics(markdown: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for line in _substantive_lines(markdown):
        claim = line.claim
        scrubbed = PHONE.sub("", claim)
        scrubbed = DATE.sub("", scrubbed)
        scrubbed = VERSION.sub("", scrubbed)
        scrubbed = CALENDAR_MONTH.sub("", scrubbed)
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
        if item.blocking
        and not _answer_has_evidence(
            answers.get(item.question_id),
            item.question_id,
            question=item.question,
        )
    ]

    draft_lines = _substantive_lines(result.rewritten_resume_markdown)
    audit_claims = [_normalize_claim(item.claim) for item in result.claim_audit]
    coverage = [
        [
            index
            for index, audit_claim in enumerate(audit_claims)
            if audit_claim == line.normalized
        ]
        for line in draft_lines
    ]

    covered_audits = {index for indexes in coverage for index in indexes}
    for line, indexes in zip(draft_lines, coverage):
        if not indexes:
            blockers.append(
                _gap_blocker(
                    f"请为“{line.claim}”提供唯一 claim_audit",
                    "成稿实质行缺少 claim_audit",
                )
            )
        elif len(indexes) > 1:
            blockers.append(
                _gap_blocker(
                    f"请合并“{line.claim}”的重复 claim_audit",
                    "成稿实质行存在重复 claim_audit",
                )
            )

    for index, audit in enumerate(result.claim_audit):
        if index not in covered_audits:
            display = _display_claim(audit.claim) or "(空 claim)"
            blockers.append(
                _gap_blocker(
                    f"请移除或修正未覆盖成稿的 claim_audit：“{display}”",
                    "claim_audit 未覆盖任何成稿实质行",
                )
            )

    metrics_by_claim: dict[str, list[str]] = {}
    for claim, metric in extract_business_metrics(result.rewritten_resume_markdown):
        metrics_by_claim.setdefault(_normalize_claim(claim), []).append(metric)

    for line, indexes in zip(draft_lines, coverage):
        if len(indexes) != 1:
            continue
        audit = result.claim_audit[indexes[0]]
        if metrics_by_claim.get(line.normalized):
            continue

        blocker = _claim_evidence_blocker(line.claim)
        if audit.fact_status == "source_verified":
            valid_claim = bool(audit.source_resume_ids) and bool(
                audit.source_quotes
            ) and any(
                _quote_contains_full_claim(quote, line.claim)
                for quote in audit.source_quotes
            )
        elif audit.fact_status == "user_confirmed":
            valid_claim = _answer_has_evidence(
                answers.get(blocker.question_id),
                blocker.question_id,
                claim=line.claim,
            )
        else:
            valid_claim = False
        if not valid_claim:
            blockers.append(blocker)

    for line, indexes in zip(draft_lines, coverage):
        for metric in metrics_by_claim.get(line.normalized, []):
            question_id = stable_metric_question_id(line.claim, metric)
            if len(indexes) != 1:
                blockers.append(
                    MissingInformation(
                        question_id=question_id,
                        question=(
                            f"请先为“{line.claim}”中的 {metric} 提供唯一 claim_audit"
                        ),
                        blocking=True,
                        reason="业务指标缺少唯一 claim_audit",
                    )
                )
                continue

            audit = result.claim_audit[indexes[0]]
            answer = answers.get(question_id)
            answered = (
                audit.fact_status == "user_confirmed"
                and _answer_has_evidence(
                    answer,
                    question_id,
                    metric=metric,
                    claim=line.claim,
                )
            )
            basis = _clean_text(audit.measurement_basis)
            answer_supports_basis = answered and (
                not basis or _answer_contains_basis(answer, basis)
            )
            metric_quotes = [
                quote
                for quote in audit.source_quotes
                if _quote_contains_metric(quote, metric)
                and _quote_contains_full_claim(quote, line.claim)
            ]
            source_evidence = (
                bool(metric_quotes)
                and _basis_is_informative(basis, metric)
                and any(_contains_basis(quote, basis) for quote in metric_quotes)
            )

            if audit.fact_status == "source_verified":
                valid_metric = source_evidence
            elif audit.fact_status == "user_confirmed":
                valid_metric = answer_supports_basis
            else:
                valid_metric = False

            if not valid_metric:
                blockers.append(
                    MissingInformation(
                        question_id=question_id,
                        question=(
                            f"请说明“{line.claim}”中 {metric} 的统计口径和可核验来源"
                        ),
                        blocking=True,
                        reason=(
                            "业务指标必须绑定唯一 claim_audit、同一 quote 中的值和口径，"
                            "或当前问题的有效候选人答案"
                        ),
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
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized.strip().lower())


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


def _answer_has_evidence(
    answer: Any,
    expected_question_id: str,
    metric: str | None = None,
    claim: str | None = None,
    question: str | None = None,
) -> bool:
    if (
        answer is None
        or _clean_text(_field(answer, "question_id")) != expected_question_id
    ):
        return False
    answer_text = _clean_text(_field(answer, "answer"))
    source_note = _clean_text(_field(answer, "source_note"))
    if len(answer_text) < MIN_ANSWER_LENGTH or len(source_note) < MIN_SOURCE_NOTE_LENGTH:
        return False
    if _is_placeholder(answer_text) or _is_placeholder(source_note):
        return False
    normalized_note = _normalize_for_hash(source_note)
    if not any(term in normalized_note for term in SOURCE_NOTE_TERMS):
        return False
    if metric is not None:
        return _answer_explains_metric(answer_text, metric, claim)
    if claim is not None:
        return _text_asserts_claim(answer_text, claim)
    if question is not None:
        return _answer_addresses_question(answer_text, question)
    return True


def _contains_basis(container: str, basis: str) -> bool:
    compact_container = _compact(container)
    compact_basis = _compact(basis)
    if not compact_basis:
        return False
    prefix = r"(?<![\d.])" if compact_basis[0].isdigit() else ""
    suffix = r"(?![\d.])" if compact_basis[-1].isdigit() else ""
    return bool(re.search(prefix + re.escape(compact_basis) + suffix, compact_container))


def _basis_has_evidence(
    basis: str, source_texts: Iterable[str], answers: Iterable[Any]
) -> bool:
    if any(_contains_basis(source, basis) for source in source_texts):
        return True
    return any(
        _answer_has_evidence(
            answer,
            _clean_text(_field(answer, "question_id")),
        )
        and (
            _contains_basis(_field(answer, "answer"), basis)
            or _contains_basis(_field(answer, "source_note"), basis)
        )
        for answer in answers
    )


def _display_claim(raw_line: str) -> str:
    value = unicodedata.normalize("NFKC", raw_line).strip()
    value = HEADING_PREFIX.sub("", value)
    value = LIST_PREFIX.sub("", value)
    value = CHECKBOX_PREFIX.sub("", value)
    value = re.sub(r"[*_`]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_claim(value: str) -> str:
    return _normalize_for_hash(_display_claim(value))


def _substantive_lines(markdown: str) -> list[_DraftLine]:
    lines: list[_DraftLine] = []
    for raw_line in markdown.splitlines():
        if not raw_line.strip() or STRUCTURAL_MARKDOWN.match(raw_line):
            continue
        is_heading = bool(HEADING.match(raw_line))
        claim = _display_claim(raw_line)
        normalized = _normalize_claim(claim)
        if is_heading and normalized in SECTION_HEADINGS:
            continue
        if normalized:
            lines.append(_DraftLine(claim=claim, normalized=normalized))
    return lines


def _gap_blocker(question: str, reason: str) -> MissingInformation:
    normalized = _normalize_for_hash(f"{question}|{reason}")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return MissingInformation(
        question_id=f"gap-{digest}",
        question=question,
        blocking=True,
        reason=reason,
    )


def _claim_evidence_blocker(claim: str) -> MissingInformation:
    return _gap_blocker(
        f"请完整确认“{claim}”并说明可核验来源",
        (
            "非数字 claim 缺少完整 source_verified quote 确定性关联，"
            "或有效 user_confirmed 答案"
        ),
    )


def _quote_contains_full_claim(quote: str, claim: str) -> bool:
    return _text_asserts_claim(quote, claim)


def _text_asserts_claim(container: str, claim: str) -> bool:
    compact_container = _compact(_display_claim(container))
    compact_claim = _compact(_display_claim(claim))
    if not compact_container or not compact_claim:
        return False

    start = compact_container.find(compact_claim)
    while start >= 0:
        clause_start = max(
            compact_container.rfind(mark, 0, start)
            for mark in ("。", "，", ",", "；", ";", "！", "？")
        ) + 1
        clause_end_candidates = [
            position
            for mark in ("。", "，", ",", "；", ";", "！", "？")
            if (position := compact_container.find(mark, start + len(compact_claim)))
            >= 0
        ]
        clause_end = min(clause_end_candidates, default=len(compact_container))
        prefix = compact_container[clause_start:start]
        suffix = compact_container[start + len(compact_claim) : clause_end]

        collective_positions = [
            prefix.rfind(actor) for actor in ("团队", "我们", "项目组", "部门")
        ]
        last_collective = max(collective_positions)
        personal_positions = [prefix.rfind(actor) for actor in ("本人", "候选人")]
        personal_positions.extend(
            match.start() for match in re.finditer(r"我(?!们)", prefix)
        )
        last_personal = max(personal_positions)
        collective_upgrade = last_collective >= 0 and last_personal < last_collective
        locally_negated = bool(
            re.search(r"(?:不|未|没有|并非|不是|从未|未曾|否认)$", prefix)
        ) or bool(re.match(r"(?:不属实|错误|有误|不正确|不对)", suffix))

        if not collective_upgrade and not locally_negated:
            return True
        start = compact_container.find(compact_claim, start + 1)
    return False


QUESTION_FACT_GROUPS = (
    (
        ("学历", "学位", "教育背景", "学校", "院校", "专业"),
        ("学历", "学位", "硕士", "本科", "博士", "大专", "学校", "院校", "专业"),
    ),
    (
        ("入职", "离职", "任职日期", "工作日期", "就职时间"),
        ("入职", "离职", "任职", "工作时间", "就职", "年", "月", "日"),
    ),
    (
        ("日期", "时间", "何时", "哪一年"),
        ("日期", "时间", "年", "月", "日"),
    ),
    (
        ("部署", "运行环境", "部署环境"),
        ("部署", "环境", "windows", "linux", "容器", "云", "本地"),
    ),
    (
        ("职责", "负责", "主导", "角色"),
        ("职责", "负责", "主导", "角色", "参与"),
    ),
)


def _answer_addresses_question(answer: str, question: str) -> bool:
    normalized_answer = _compact(answer)
    normalized_question = _compact(question)
    matched_groups = [
        answer_terms
        for question_terms, answer_terms in QUESTION_FACT_GROUPS
        if any(term in normalized_question for term in question_terms)
    ]
    if matched_groups:
        return any(
            any(term in normalized_answer for term in answer_terms)
            for answer_terms in matched_groups
        )

    reduced = normalized_question
    for boilerplate in (
        "请确认",
        "请补充",
        "请说明",
        "请提供",
        "是否",
        "哪一个正确",
        "存在冲突",
        "缺少事实依据",
    ):
        reduced = reduced.replace(boilerplate, "")
    anchors = re.findall(r"[a-z][a-z0-9.+#-]{1,}|[\u4e00-\u9fff]{2,}", reduced)
    return bool(anchors) and any(anchor in normalized_answer for anchor in anchors)


def _basis_is_informative(basis: str, metric: str) -> bool:
    compact_basis = _compact(basis)
    compact_metric = _compact(metric)
    if not compact_basis or compact_basis == compact_metric:
        return False

    normalized_metric = unicodedata.normalize("NFKC", metric).lower()
    if re.search(r"(?:qps|tps|rps)\s*$", normalized_metric, re.I):
        has_measurement_source = any(
            term in compact_basis for term in QPS_MEASUREMENT_SOURCE_TERMS
        )
        has_statistics_window = any(
            term in compact_basis for term in STATISTICS_WINDOW_TERMS
        )
        return has_measurement_source and has_statistics_window

    has_method = any(term in compact_basis for term in MEASUREMENT_METHOD_TERMS)
    if normalized_metric.rstrip().endswith("%"):
        has_sample_size = bool(
            re.search(r"\d+(?:\.\d+)?(?:次|个|条|项|例|样本)", compact_basis)
            or re.search(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", compact_basis)
        )
        return has_method and has_sample_size
    return has_method


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).lower())


def _quote_contains_metric(quote: str, metric: str) -> bool:
    expected = _compact(metric)
    return any(
        _compact(candidate) == expected
        for _, candidate in extract_business_metrics(quote)
    )


def _answer_contains_basis(answer: Any, basis: str) -> bool:
    return _contains_basis(_field(answer, "answer"), basis) or _contains_basis(
        _field(answer, "source_note"), basis
    )


def _is_placeholder(value: str) -> bool:
    normalized = _normalize_for_hash(value)
    return normalized in PLACEHOLDER_EXACT or any(
        term in normalized for term in PLACEHOLDER_TERMS
    )


def _answer_explains_metric(
    answer: str, metric: str, claim: str | None = None
) -> bool:
    normalized_metric = unicodedata.normalize("NFKC", metric)
    normalized_answer = unicodedata.normalize("NFKC", answer)
    if normalized_metric.rstrip().endswith("%"):
        return _percentage_answer_matches(
            normalized_answer, normalized_metric, claim
        )
    if re.search(r"(?:qps|tps|rps)\s*$", normalized_metric, re.I):
        compact_answer = _compact(normalized_answer)
        return (
            any(
                _metric_clause_is_relevant(clause, claim, normalized_metric)
                for clause in _clauses_with_exact_metric(
                    normalized_answer, normalized_metric
                )
            )
            and any(
                term in compact_answer for term in QPS_MEASUREMENT_SOURCE_TERMS
            )
            and any(term in compact_answer for term in STATISTICS_WINDOW_TERMS)
        )
    return any(
        _metric_clause_is_relevant(clause, claim, normalized_metric)
        and any(term in clause.lower() for term in OTHER_METRIC_ANSWER_TERMS)
        for clause in _clauses_with_exact_metric(normalized_answer, normalized_metric)
    )


def _answer_has_exact_metric(answer: str, metric: str) -> bool:
    expected = _compact(metric)
    return any(
        _compact(candidate) == expected
        for _, candidate in extract_business_metrics(answer)
    )


def _clauses_with_exact_metric(answer: str, metric: str) -> list[str]:
    return [
        clause
        for clause in re.split(r"[，,。；;！？\n]", answer)
        if _answer_has_exact_metric(clause, metric)
    ]


def _claim_context_anchors(claim: str | None, metric: str) -> list[str]:
    if not claim:
        return []
    compact_claim = _compact(claim)
    compact_claim = compact_claim.replace(_compact(metric), " ")
    for generic in (
        "成功率",
        "准确率",
        "峰值",
        "达到",
        "提升",
        "降低",
        "增长",
        "减少",
        "约为",
    ):
        compact_claim = compact_claim.replace(generic, " ")
    return [
        token
        for token in re.findall(
            r"[a-z][a-z0-9.+#-]{1,}|[\u4e00-\u9fff]{2,}", compact_claim
        )
        if token not in {"统计", "项目", "结果"}
    ]


def _metric_clause_is_relevant(
    clause: str, claim: str | None, metric: str
) -> bool:
    compact_clause = _compact(clause)
    anchors = _claim_context_anchors(claim, metric)
    if anchors:
        return all(anchor in compact_clause for anchor in anchors)
    return any(
        term in compact_clause
        for term in ("固定回归", "回归记录", "压测", "监控窗口")
    )


def _span_clause(answer: str, start: int, end: int) -> str:
    clause_start = max(
        answer.rfind(mark, 0, start) for mark in ("。", "，", ",", "；", ";", "！", "？")
    ) + 1
    clause_end_candidates = [
        position
        for mark in ("。", "，", ",", "；", ";", "！", "？")
        if (position := answer.find(mark, end)) >= 0
    ]
    clause_end = min(clause_end_candidates, default=len(answer))
    return answer[clause_start:clause_end]


def _span_is_asserted(answer: str, start: int, end: int) -> bool:
    clause_start = max(
        answer.rfind(mark, 0, start) for mark in ("。", "，", ",", "；", ";", "！", "？")
    ) + 1
    clause = _span_clause(answer, start, end)
    prefix = _compact(answer[clause_start:start])
    suffix = _compact(clause[end - clause_start :])
    if re.search(r"(?:不|未|没有|并非|不是|从未|未曾|未达到|未达)$", prefix):
        return False
    if re.match(r".{0,4}(?:错误|错误值|有误|不正确|不属实|不对)", suffix):
        return False
    return True


def _percentage_answer_matches(
    answer: str, metric: str, claim: str | None = None
) -> bool:
    target_match = re.search(r"\d+(?:\.\d+)?", metric)
    if target_match is None:
        return False
    target_text = target_match.group(0)
    target = float(target_text)
    decimal_places = len(target_text.partition(".")[2])
    tolerance = 0.5 * (10 ** -decimal_places) + 1e-9

    ratios: list[tuple[float, float, int, int]] = []
    for match in re.finditer(
        r"(?P<num>\d+(?:\.\d+)?)\s*/\s*(?P<den>\d+(?:\.\d+)?)",
        answer,
    ):
        ratios.append(
            (
                float(match.group("num")),
                float(match.group("den")),
                match.start(),
                match.end(),
            )
        )
    for match in re.finditer(
        r"(?P<den>\d+(?:\.\d+)?)\s*(?:次|个|条|项|例|样本)?"
        r".{0,40}?(?:中|内|里).*?(?:成功|完成|命中|通过)\s*"
        r"(?P<num>\d+(?:\.\d+)?)",
        answer,
    ):
        ratios.append(
            (
                float(match.group("num")),
                float(match.group("den")),
                match.start(),
                match.end(),
            )
        )
    for match in re.finditer(
        r"(?P<den>\d+(?:\.\d+)?)\s*(?:次|个|条|项|例|样本)?"
        r".{0,40}?(?:中|内|里).*?(?:有\s*)?"
        r"(?P<num>\d+(?:\.\d+)?)\s*(?:次|个|条|项|例|样本)?"
        r"\s*(?:成功|完成|命中|通过)",
        answer,
    ):
        ratios.append(
            (
                float(match.group("num")),
                float(match.group("den")),
                match.start(),
                match.end(),
            )
        )

    return any(
        denominator > 0
        and abs(numerator / denominator * 100 - target) <= tolerance
        and _span_is_asserted(answer, start, end)
        and (
            _metric_clause_is_relevant(
                _span_clause(answer, start, end), claim, metric
            )
            or (
                _compact(_span_clause(answer, start, end)).startswith(
                    ("而是", "实际", "正确值", "应为")
                )
                and _metric_clause_is_relevant(answer, claim, metric)
            )
        )
        for numerator, denominator, start, end in ratios
    )
