import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any

from resume_agent.rewrite_schemas import CodexRewriteResult, MissingInformation


PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
DATE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日?)?)?"
    r"|(?<!\d)(?:19|20)\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?(?!\d)"
)
VERSION = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*)"
    r"\s+v?\d+(?:\.\d+){0,3}\+?"
    r"(?![\w.])",
    re.I,
)
CALENDAR_MONTH = re.compile(
    r"(?<!\d)(?:0?[1-9]|1[0-2])\s*月(?=\s*(?:毕业|入学|入职|离职))"
    r"|(?:毕业时间\s*[:：]?|毕业于)\s*(?:0?[1-9]|1[0-2])\s*月"
)
METRIC = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])(?:[$¥]\s*|USD\s+)"
    r"\d+(?:\.\d+)?(?:[KMB])?(?![A-Za-z0-9])"
    r"|"
    r"(?<![A-Za-z0-9])"
    r"(?:约\s*)?\d+(?:\.\d+)?"
    r"(?:\s*(?:-|–|—|~|～|至|到)\s*\d+(?:\.\d+)?)?"
    r"\s*(?:%|\+|qps|tps|rps|ms|毫秒|s|秒|分钟|小时|年|个月|月|天|"
    r"(?:万|亿)(?:用户|人|家|条|次|个|元)?|元|人|家|条|"
    r"美元|个(?:节点|字段)|(?:余|多)?(?:用户|人|家|条|项|次|个)|"
    r"项|轮|维|种(?:状态)?|字段|节点|倍|次)"
    r"(?:以上|以下|左右)?"
    r"(?![A-Za-z0-9])"
    r"|(?:十余|百余|千余|数十|数百|数千|上千|近百|约百|百万|一亿)"
    r"(?:用户|人|家|项|次|条|元|年)"
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
)
SOURCE_NOTE_NEGATED_PREFIX = re.compile(
    r"(?:没有|无|未有|缺少|缺乏|不存在|无法提供|不可用)$"
)
SOURCE_NOTE_UNAVAILABLE_SUFFIX = re.compile(
    r"^(?:已)?(?:遗失|丢失|缺失|不存在|无法提供|不可用|待查)"
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
IN_PROGRESS_HEADING = "在建项目（待验收）"
IN_PROGRESS_MARKERS = ("在建", "正在", "进行中", "开发中", "实现中", "计划验收", "待验收")
COMPLETED_MARKERS = (
    "已完成",
    "已经完成",
    "已完工",
    "完成了",
    "主导完成",
    "已上线",
    "上线",
    "已落地",
    "落地",
    "已交付",
    "交付",
    "投入生产",
    "投入使用",
    "成功发布",
    "稳定运行",
    "正式投产",
    "生产",
)
FORBIDDEN_EXTERNAL_ACTORS = ("客户", "甲方", "生产用户", "真实用户")
BUSINESS_RESULT = re.compile(
    r"(?:准确率|召回率|成功率|转化率|可用性|效率|性能|吞吐|qps|成本|"
    r"耗时|时延|用户数|日活|月活|收入|业务量).{0,12}"
    r"(?:显著|明显|大幅)?(?:提升|提高|降低|减少|节省|达到|达成|增长)"
    r"|(?:显著|明显|大幅)?(?:提升|提高|降低|减少|节省|增长).{0,12}"
    r"(?:准确率|召回率|成功率|转化率|可用性|效率|性能|吞吐|qps|成本|"
    r"耗时|时延|用户数|日活|月活|收入|业务量)"
    r"|(?:效率|性能|吞吐|qps|用户数|日活|月活|收入|业务量).{0,8}"
    r"(?:翻倍|倍增)"
    r"|(?:成本|耗时|时延).{0,8}(?:减半|折半)"
    r"|(?:达到四个九|增长一倍)",
    re.I,
)
EXPLICIT_COMPLETED_ASSERTION = re.compile(
    r"(?:已|已经|现已).{0,6}(?:完成|完工|上线|落地|交付|投入使用|投产|发布|"
    r"通过|达标|实现|部署)"
    r"|(?:已经|现已).{0,8}(?:部署|运行).{0,6}(?:线上|生产)",
    re.I,
)
COMPLETED_ASSERTION = re.compile(
    r"(?:成功|正式).{0,4}(?:发布|上线|投产|交付|投入使用)"
    r"|(?:稳定运行|投入生产|服务真实用户|服务生产用户)"
    r"|(?:开发|研发|构建|建设)(?!.{0,6}(?:未|待)完成).{0,8}"
    r"(?:完成(?!度)|完工|完毕|实现|上线|落地|交付)"
    r"|(?<!未)(?<!待)(?<!未能)完成(?!度)(?:了)?"
    r"|(?:已经|已)?做完"
    r"|(?:开发|研发|构建|建设|实现)完(?!成|整)"
    r"|实现(?!.{0,4}(?:未|待)完成).{0,4}(?:完成(?!度)|完毕)"
    r"|(?:系统|服务|应用|平台|模块|能力).{0,8}"
    r"(?:运行于|部署(?:在|于)).{0,4}(?:线上|生产)"
    r"|(?:主导|负责|为客户).{0,16}(?:上线|落地|交付|投产|发布)",
    re.I,
)
COMPLETION_EVENT = re.compile(
    r"完成(?!度)|完工|完毕|做完|"
    r"(?:开发|研发|构建|建设|实现)完(?!成|整)|"
    r"投入使用|投入生产|稳定运行|运行于|部署|发布|上线|投产|"
    r"交付(?!物)|落地|实现|服务",
    re.I,
)
BARE_COMPLETION_TERMS = ("上线", "落地", "交付", "生产")
FUTURE_TERMS = ("计划", "待", "目标", "拟", "预计", "准备", "将", "验收")
FUTURE_SCOPE_BOUNDARIES = (
    "然而",
    "不过",
    "但是",
    "可是",
    "当前",
    "目前",
    "现已",
    "已经",
    "却",
    "但",
    "而",
)
TEST_CONTEXT_TERMS = ("回归", "测试", "用例", "样本", "验收")
HTML_MARKUP = re.compile(r"<!--[\s\S]*?-->|<[^>\n]*>")
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
    rewritten_resume_markdown: str


class InvalidModelResult(ValueError):
    pass


@dataclass(frozen=True)
class _DraftLine:
    claim: str
    normalized: str
    visual_key: str
    in_progress_section: bool = False


@dataclass(frozen=True)
class _MarkdownStructure:
    lines: list[_DraftLine]
    in_progress_heading_count: int


@dataclass(frozen=True)
class _PercentageRecord:
    numerator: float
    denominator: float
    start: int
    end: int


@dataclass(frozen=True)
class _BlockerSlot:
    kind: str
    labels: tuple[str, ...]


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


def validate_in_progress_claims(
    result: CodexRewriteResult,
) -> list[MissingInformation]:
    structure = _parse_markdown_structure(result.rewritten_resume_markdown)
    in_progress_audits = [
        audit for audit in result.claim_audit if audit.claim_status == "in_progress"
    ]
    plans = list(result.gap_acceptance_plans)
    section_lines = [line for line in structure.lines if line.in_progress_section]
    if not (
        structure.in_progress_heading_count
        or in_progress_audits
        or plans
        or section_lines
    ):
        return []

    blockers: list[MissingInformation] = []
    if structure.in_progress_heading_count != 1:
        blockers.append(
            _in_progress_blocker(
                "请保留唯一且独立的 Markdown 标题“在建项目（待验收）”",
                "在建项目必须位于唯一且明确的独立章节",
            )
        )
    if structure.in_progress_heading_count and not section_lines:
        blockers.append(
            _in_progress_blocker(
                "请在“在建项目（待验收）”章节填写待验收 claim",
                "在建项目章节不能为空",
            )
        )

    normalized_audits = [
        _visual_claim_key(audit.claim) for audit in result.claim_audit
    ]
    for line in section_lines:
        matches = [
            audit
            for audit, normalized in zip(result.claim_audit, normalized_audits)
            if normalized == line.visual_key
        ]
        if len(matches) != 1 or matches[0].claim_status != "in_progress":
            blockers.append(
                _in_progress_blocker(
                    f"请将“{line.claim}”绑定到唯一的 in_progress claim_audit",
                    "在建项目章节中的每条 claim 都必须标记为 in_progress",
                )
            )

    audit_gap_counts: dict[str | None, int] = {}
    for audit in in_progress_audits:
        audit_gap_counts[audit.gap_id] = audit_gap_counts.get(audit.gap_id, 0) + 1
    plan_gap_counts: dict[str, int] = {}
    for plan in plans:
        plan_gap_counts[plan.gap_id] = plan_gap_counts.get(plan.gap_id, 0) + 1

    for audit in in_progress_audits:
        claim = _visual_claim(audit.claim)
        occurrences = [
            line
            for line in structure.lines
            if line.visual_key == _visual_claim_key(claim)
        ]
        if len(occurrences) != 1 or not occurrences[0].in_progress_section:
            blockers.append(
                _in_progress_blocker(
                    f"请只在“在建项目（待验收）”章节保留“{claim}”",
                    "in_progress claim 必须且只能出现在独立在建项目章节",
                )
            )
        if (
            audit.gap_id is None
            or audit_gap_counts.get(audit.gap_id) != 1
            or plan_gap_counts.get(audit.gap_id) != 1
        ):
            blockers.append(
                _in_progress_blocker(
                    f"请为“{claim}”绑定一个未复用且完全匹配的 gap plan",
                    "in_progress claim 与 GapAcceptancePlan 必须一一匹配",
                )
            )

        policy_text = _policy_key(claim)
        if not any(_policy_key(marker) in policy_text for marker in IN_PROGRESS_MARKERS):
            blockers.append(
                _in_progress_blocker(
                    f"请为“{claim}”增加明确的进行中标记",
                    "在建项目 claim 缺少在建、正在、计划验收或待验收标记",
                )
            )
        forbidden = _forbidden_in_progress_state(claim)
        if forbidden is not None:
            blockers.append(
                _in_progress_blocker(
                    f"请移除“{claim}”中的完成态、生产态或外部主体表述",
                    f"在建项目不得声称完成、上线、交付、生产、客户或甲方事实：{forbidden}",
                )
            )
        if _has_unverified_in_progress_result(claim):
            blockers.append(
                _in_progress_blocker(
                    f"请移除“{claim}”中的未验收业务结果或指标",
                    "在建项目不得包含不可验证业务结果或指标",
                )
            )

    for plan in plans:
        if audit_gap_counts.get(plan.gap_id) != 1:
            blockers.append(
                _in_progress_blocker(
                    f"请将 gap plan {plan.gap_id} 绑定到唯一 in_progress claim",
                    "GapAcceptancePlan 与 in_progress claim 必须一一匹配",
                )
            )
    return deduplicate_questions(blockers)


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
    blockers.extend(validate_in_progress_claims(result))

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
        if audit.claim_status == "in_progress":
            continue
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
            if audit.claim_status == "in_progress":
                continue
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
                and any(
                    _contains_basis(quote, basis)
                    and _source_metric_record_matches(
                        quote, line.claim, metric, basis
                    )
                    for quote in metric_quotes
                )
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
        rewritten_resume_markdown=result.rewritten_resume_markdown,
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
        or not _has_explicit_confirmation(answer)
    ):
        return False
    answer_text = _clean_text(_field(answer, "answer"))
    source_note = _clean_text(_field(answer, "source_note"))
    if len(answer_text) < MIN_ANSWER_LENGTH or len(source_note) < MIN_SOURCE_NOTE_LENGTH:
        return False
    if _is_placeholder(answer_text) or _is_placeholder(source_note):
        return False
    if not _source_note_has_concrete_evidence(source_note):
        return False
    if metric is not None:
        return _answer_explains_metric(answer_text, metric, claim)
    if claim is not None:
        return _text_asserts_claim(answer_text, claim)
    if question is not None:
        return _answer_addresses_question(answer_text, question)
    return True


def _has_explicit_confirmation(answer: Any) -> bool:
    if _optional_field(answer, "user_confirmed") is not True:
        return False
    confirmed_at = _optional_field(answer, "confirmed_at")
    if isinstance(confirmed_at, datetime):
        parsed = confirmed_at
    elif isinstance(confirmed_at, str):
        try:
            parsed = datetime.fromisoformat(confirmed_at)
        except ValueError:
            return False
    else:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _optional_field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _source_note_has_concrete_evidence(source_note: str) -> bool:
    normalized = unicodedata.normalize("NFKC", source_note).lower()
    for term in SOURCE_NOTE_TERMS:
        for match in re.finditer(re.escape(term), normalized):
            clause = _span_clause(normalized, match.start(), match.end())
            local_start = match.start() - max(
                normalized.rfind(mark, 0, match.start())
                for mark in ("。", "，", ",", "；", ";", "！", "？")
            ) - 1
            local_end = local_start + len(term)
            prefix = _compact(clause[:local_start])
            suffix = _compact(clause[local_end:])
            if SOURCE_NOTE_NEGATED_PREFIX.search(prefix):
                continue
            if SOURCE_NOTE_UNAVAILABLE_SUFFIX.search(suffix):
                continue
            return True
    return False


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
    return _rendered_fact_text(_strip_markdown_structure(raw_line))


def _visual_claim(raw_line: str) -> str:
    return _rendered_visible_text(_strip_markdown_structure(raw_line))


def _strip_markdown_structure(raw_line: str) -> str:
    value = raw_line.strip()
    value = HEADING_PREFIX.sub("", value)
    value = LIST_PREFIX.sub("", value)
    value = CHECKBOX_PREFIX.sub("", value)
    return value


def _normalize_claim(value: str) -> str:
    return _normalize_for_hash(_display_claim(value))


def _visual_claim_key(value: str) -> str:
    return _normalize_for_hash(_visual_claim(value))


def _substantive_lines(markdown: str) -> list[_DraftLine]:
    return _parse_markdown_structure(markdown).lines


def _parse_markdown_structure(markdown: str) -> _MarkdownStructure:
    lines: list[_DraftLine] = []
    in_progress_heading_count = 0
    in_progress_level: int | None = None
    fenced = False
    for raw_line in markdown.splitlines():
        if re.match(r"^\s*```", raw_line):
            fenced = not fenced
            continue
        if not raw_line.strip() or STRUCTURAL_MARKDOWN.match(raw_line):
            continue
        is_heading = bool(HEADING.match(raw_line))
        claim = _display_claim(raw_line)
        visual_claim = _visual_claim(raw_line)
        if is_heading:
            claim = re.sub(r"\s+#+\s*$", "", claim).strip()
            visual_claim = re.sub(r"\s+#+\s*$", "", visual_claim).strip()
        normalized = _normalize_claim(claim)
        visual_key = _visual_claim_key(visual_claim)
        heading_level = _heading_level(raw_line) if is_heading and not fenced else None
        explicit_in_progress_heading = (
            heading_level is not None
            and _is_explicit_in_progress_heading(raw_line)
        )
        if heading_level is not None:
            if in_progress_level is not None and heading_level <= in_progress_level:
                in_progress_level = None
            if explicit_in_progress_heading:
                in_progress_heading_count += 1
                in_progress_level = heading_level
        if is_heading and (
            visual_key in SECTION_HEADINGS or explicit_in_progress_heading
        ):
            continue
        if normalized:
            lines.append(
                _DraftLine(
                    claim=claim,
                    normalized=normalized,
                    visual_key=visual_key,
                    in_progress_section=in_progress_level is not None,
                )
            )
    return _MarkdownStructure(
        lines=lines,
        in_progress_heading_count=in_progress_heading_count,
    )


def _heading_level(raw_line: str) -> int | None:
    match = re.match(r"^\s{0,3}(#{1,6})(?!#)", raw_line)
    return len(match.group(1)) if match else None


def _is_explicit_in_progress_heading(raw_line: str) -> bool:
    strict_heading = re.match(r"^\s{0,3}#{1,6}(?!#)(?:\s+|$)", raw_line)
    return bool(strict_heading) and _heading_key(raw_line) == _heading_key(
        IN_PROGRESS_HEADING
    )


def _heading_key(value: str) -> str:
    visual = re.sub(r"\s+#+\s*$", "", _visual_claim(value)).strip()
    return re.sub(r"\s+", "", _normalize_for_hash(visual))


def _policy_key(value: str) -> str:
    normalized = _rendered_visible_text(value)
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    ).lower()


def _rendered_visible_text(value: str) -> str:
    visible = value
    for _ in range(4):
        decoded = unescape(visible)
        if decoded == visible:
            break
        visible = decoded
    visible = _replace_markdown_links(visible, include_url=False)
    visible = HTML_MARKUP.sub("", visible)
    visible = unicodedata.normalize("NFKC", visible)
    visible = "".join(
        character for character in visible if not _is_default_ignorable(character)
    )
    visible = re.sub(r"[*_`]+", "", visible)
    return re.sub(r"\s+", " ", visible).strip()


def _rendered_fact_text(value: str) -> str:
    rendered = _replace_markdown_links(value, include_url=True)
    rendered = unicodedata.normalize("NFKC", rendered)
    rendered = "".join(
        character for character in rendered if not _is_default_ignorable(character)
    )
    return re.sub(r"\s+", " ", rendered).strip()


def _replace_markdown_links(value: str, *, include_url: bool) -> str:
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        image = (
            value[cursor] == "!"
            and cursor + 1 < len(value)
            and value[cursor + 1] == "["
        )
        label_start = cursor + 1 if image else cursor
        if value[label_start] != "[" or _is_escaped(value, label_start):
            output.append(value[cursor])
            cursor += 1
            continue
        label_end = _balanced_end(value, label_start, "[", "]")
        if (
            label_end is None
            or label_end + 1 >= len(value)
            or value[label_end + 1] != "("
        ):
            output.append(value[cursor])
            cursor += 1
            continue
        url_end = _balanced_end(value, label_end + 1, "(", ")")
        if url_end is None:
            output.append(value[cursor])
            cursor += 1
            continue
        label = value[label_start + 1:label_end]
        url = value[label_end + 2:url_end]
        if include_url:
            prefix = "!" if image else ""
            output.append(f"{prefix}{label} ({url})")
        else:
            output.append(label)
        cursor = url_end + 1
    return "".join(output)


def _balanced_end(
    value: str, start: int, opening: str, closing: str
) -> int | None:
    depth = 0
    for index in range(start, len(value)):
        if _is_escaped(value, index):
            continue
        if value[index] == opening:
            depth += 1
        elif value[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _is_default_ignorable(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character) == "Cf"
        or codepoint == 0x034F
        or 0x180B <= codepoint <= 0x180F
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xFFF0 <= codepoint <= 0xFFF8
        or 0x1BCA0 <= codepoint <= 0x1BCA3
        or 0x1D173 <= codepoint <= 0x1D17A
        or 0xE0000 <= codepoint <= 0xE0FFF
    )


def _forbidden_in_progress_state(claim: str) -> str | None:
    for clause in _policy_clauses(claim):
        external = next(
            (
                actor
                for actor in FORBIDDEN_EXTERNAL_ACTORS
                if _policy_key(actor) in clause
            ),
            None,
        )
        if external is not None:
            return external
        explicit_completed = EXPLICIT_COMPLETED_ASSERTION.search(clause)
        if explicit_completed:
            return explicit_completed.group(0)
        for completed in COMPLETED_ASSERTION.finditer(clause):
            event_starts = _completion_event_starts(completed)
            if any(
                not _has_future_target_before(
                    clause, event_start, after_previous_event=True
                )
                for event_start in event_starts
            ):
                return completed.group(0)
        for term in BARE_COMPLETION_TERMS:
            start = clause.find(term)
            while start >= 0:
                if not _is_future_or_artifact_reference(clause, term, start):
                    return term
                start = clause.find(term, start + len(term))
    return None


def _completion_event_starts(completed: re.Match[str]) -> list[int]:
    event_offsets = [
        event.start() for event in COMPLETION_EVENT.finditer(completed.group(0))
    ]
    return [
        completed.start() + event_offset
        for event_offset in (event_offsets or [0])
    ]


def _is_future_or_artifact_reference(clause: str, term: str, start: int) -> bool:
    suffix = clause[start + len(term):start + len(term) + 4]
    if term == "上线" and suffix.startswith("前"):
        return True
    if term == "交付" and suffix.startswith("物"):
        return True
    return _has_future_target_before(clause, start)


def _has_future_target_before(
    clause: str, event_start: int, *, after_previous_event: bool = False
) -> bool:
    prefix = clause[:event_start]
    previous_event_end = (
        max(
            (event.end() for event in COMPLETION_EVENT.finditer(prefix)),
            default=0,
        )
        if after_previous_event
        else 0
    )
    boundary_end = max([
        previous_event_end,
        *(
            position + len(boundary)
            for boundary in FUTURE_SCOPE_BOUNDARIES
            if (position := prefix.rfind(boundary)) >= 0
        ),
    ])
    local_prefix = prefix[boundary_end:]
    return any(marker in local_prefix for marker in FUTURE_TERMS)


def _has_unverified_in_progress_result(claim: str) -> bool:
    for clause in _visible_clauses(claim):
        policy_clause = _policy_key(clause)
        if BUSINESS_RESULT.search(policy_clause):
            return True
        metrics = extract_business_metrics(clause)
        if metrics and not _is_future_test_scale(policy_clause, metrics):
            return True
    return False


def _is_future_test_scale(
    policy_clause: str, metrics: Sequence[tuple[str, str]]
) -> bool:
    if not any(term in policy_clause for term in FUTURE_TERMS):
        return False
    if not any(term in policy_clause for term in TEST_CONTEXT_TERMS):
        return False
    return all(
        bool(re.search(r"(?:条|项|个|轮|次|例|组)", metric))
        for _, metric in metrics
    )


def _visible_clauses(value: str) -> list[str]:
    return [
        clause
        for clause in re.split(r"[，,。；;！？\n]", _rendered_visible_text(value))
        if clause.strip()
    ]


def _policy_clauses(value: str) -> list[str]:
    return [_policy_key(clause) for clause in _visible_clauses(value)]


def _in_progress_blocker(question: str, reason: str) -> MissingInformation:
    return _gap_blocker(question, reason)


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


DIRECT_ASSERTION_REJECTION = re.compile(
    r"(?:不属实|并非真实|不是真实|并非最终|不是最终|明显错误|错误值|"
    r"(?:说法|结果|口径|数值).{0,12}(?:错误|有误|废弃)|"
    r"未达到|未主导|不负责|从未|未曾|否认)"
)
COLLECTIVE_ACTORS = ("团队", "我们", "项目组", "部门")
PERSONAL_ACTORS = ("我", "本人", "候选人")


def _measurement_records(value: str) -> list[str]:
    return [
        record
        for record in re.split(r"[。；;！？\n]", unicodedata.normalize("NFKC", value))
        if record.strip()
    ]


def _suffix_is_typed_measurement(value: str) -> bool:
    suffix = value.lstrip("，,、 ")
    if not suffix:
        return True
    suffix = suffix.removeprefix("的")
    has_metric = bool(extract_business_metrics(suffix))
    has_percentage_record = bool(_percentage_records(suffix))
    compact_suffix = _compact(suffix)
    has_method = any(
        term in compact_suffix
        for term in (
            *MEASUREMENT_METHOD_TERMS,
            *QPS_MEASUREMENT_SOURCE_TERMS,
            *STATISTICS_WINDOW_TERMS,
            "成功",
            "完成",
            "命中",
            "通过",
        )
    )
    return (has_metric or has_percentage_record) and (
        has_method or has_percentage_record
    )


def _text_asserts_claim(container: str, claim: str) -> bool:
    compact_claim = _compact(_display_claim(claim))
    if not compact_claim:
        return False

    for raw_record in _measurement_records(_display_claim(container)):
        record = _compact(raw_record)
        start = record.find(compact_claim)
        while start >= 0:
            prefix = record[:start]
            suffix = record[start + len(compact_claim) :]
            prefix_is_direct = not prefix or prefix in PERSONAL_ACTORS
            has_collective_actor = any(actor in record for actor in COLLECTIVE_ACTORS)
            has_rejection = bool(DIRECT_ASSERTION_REJECTION.search(record))
            if (
                prefix_is_direct
                and _suffix_is_typed_measurement(suffix)
                and not has_collective_actor
                and not has_rejection
            ):
                return True
            start = record.find(compact_claim, start + 1)
    return False


def _nonmetric_claim_segments(claim: str | None) -> list[str]:
    if not claim:
        return []
    retained: list[str] = []
    for segment in re.split(r"[，,；;]", _display_claim(claim)):
        segment = segment.strip()
        if not segment:
            continue
        metrics = [metric for _, metric in extract_business_metrics(segment)]
        if not metrics:
            retained.append(segment)
            continue
        has_typed_context = bool(
            re.search(
                r"(?:成功率|准确率|覆盖率|命中率|通过率|峰值|吞吐量|达到|降至|提升至)",
                segment,
            )
        )
        if not has_typed_context:
            continue
        remainder = segment
        for metric in metrics:
            remainder = remainder.replace(metric, " ")
        previous = None
        while previous != remainder:
            previous = remainder
            remainder = re.sub(
                r"(?:成功率|准确率|覆盖率|命中率|通过率|峰值|吞吐量|"
                r"达到|降至|提升至|约为|为)\s*$",
                "",
                remainder,
            ).strip()
        if remainder and any(
            term in remainder
            for term in (
                "主导",
                "负责",
                "管理",
                "设计",
                "开发",
                "搭建",
                "实现",
                "迁移",
                "带领",
                "领导",
            )
        ):
            retained.append(remainder)
    return retained


UNRESOLVED_ANSWER_TERMS = (
    "尚未解决",
    "待核实",
    "不确定",
    "未知",
    "无法确认",
    "未确认",
)
EDUCATION_VALUES = ("博士", "硕士", "本科", "大专", "专科", "高中", "中专")
DEPLOYMENT_VALUES = (
    "windows",
    "linux",
    "macos",
    "容器",
    "kubernetes",
    "云",
    "本地",
    "服务器",
    "工作站",
)


def _blocker_slot(question: str) -> _BlockerSlot | None:
    normalized = _compact(question)
    if "最高学历" in normalized:
        return _BlockerSlot("highest_education", ("最高学历", "学历"))
    if "入职" in normalized and any(term in normalized for term in ("日期", "时间")):
        return _BlockerSlot("employment_start_date", ("入职日期", "入职时间", "入职于"))
    if (
        "项目" in normalized
        and any(term in normalized for term in ("日期", "时间"))
        and any(term in normalized for term in CONFLICT_TERMS)
    ):
        return _BlockerSlot("project_date_conflict", ("项目日期", "项目时间"))
    if any(term in normalized for term in ("部署环境", "运行环境", "部署")):
        return _BlockerSlot("deployment_environment", ("部署环境", "运行环境", "部署在"))
    return None


def _slot_value_pattern(slot: _BlockerSlot) -> re.Pattern[str]:
    if slot.kind == "highest_education":
        return re.compile("|".join(map(re.escape, EDUCATION_VALUES)), re.I)
    if slot.kind in {"employment_start_date", "project_date_conflict"}:
        return DATE
    if slot.kind == "deployment_environment":
        return re.compile("|".join(map(re.escape, DEPLOYMENT_VALUES)), re.I)
    return re.compile(r"(?!)")


def _slot_final_value_is_asserted(answer: str, slot: _BlockerSlot) -> bool:
    normalized = unicodedata.normalize("NFKC", answer)
    pattern = _slot_value_pattern(slot)
    active = False
    occurrences: list[tuple[int, int]] = []
    for clause_match in re.finditer(r"[^，,。；;！？\n]+", normalized):
        clause = clause_match.group(0)
        compact_clause = _compact(clause)
        has_slot_label = any(label in compact_clause for label in slot.labels)
        is_correction = compact_clause.startswith(
            ("而是", "实际为", "最终", "正确", "应为")
        )
        if has_slot_label:
            active = True
        elif not (active and is_correction):
            continue
        for value_match in pattern.finditer(clause):
            occurrences.append(
                (
                    clause_match.start() + value_match.start(),
                    clause_match.start() + value_match.end(),
                )
            )
    if not occurrences:
        return False
    start, end = occurrences[-1]
    return _span_is_asserted(normalized, start, end)


def _answer_addresses_question(answer: str, question: str) -> bool:
    normalized_answer = _compact(answer)
    if any(term in normalized_answer for term in UNRESOLVED_ANSWER_TERMS):
        return False
    slot = _blocker_slot(question)
    if slot is None or not any(label in normalized_answer for label in slot.labels):
        return False
    if not _slot_final_value_is_asserted(answer, slot):
        return False
    if slot.kind == "project_date_conflict":
        has_disambiguation = any(
            term in normalized_answer
            for term in ("为准", "正确日期", "实际日期", "实际项目日期", "确认")
        )
        return has_disambiguation
    return True


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


def _source_metric_record_matches(
    quote: str, claim: str, metric: str, basis: str
) -> bool:
    normalized_metric = unicodedata.normalize("NFKC", metric)
    if normalized_metric.rstrip().endswith("%"):
        return _percentage_record_matches(quote, normalized_metric, claim)
    if re.search(r"(?:qps|tps|rps)\s*$", normalized_metric, re.I):
        return _throughput_record_matches(
            quote, normalized_metric, claim, basis=basis
        )
    return True


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
    if any(
        not _text_asserts_claim(normalized_answer, segment)
        for segment in _nonmetric_claim_segments(claim)
    ):
        return False
    if normalized_metric.rstrip().endswith("%"):
        return _percentage_record_matches(
            normalized_answer, normalized_metric, claim
        )
    if re.search(r"(?:qps|tps|rps)\s*$", normalized_metric, re.I):
        return _throughput_record_matches(
            normalized_answer, normalized_metric, claim
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


def _answer_has_asserted_exact_metric(answer: str, metric: str) -> bool:
    if not _answer_has_exact_metric(answer, metric):
        return False
    compact_answer = _compact(answer)
    compact_metric = _compact(metric)
    prefix = r"(?<![\d.])" if compact_metric[0].isdigit() else ""
    suffix = r"(?![A-Za-z0-9])" if compact_metric[-1].isalnum() else ""
    events = list(
        re.finditer(prefix + re.escape(compact_metric) + suffix, compact_answer)
    )
    if not events:
        return False
    final = events[-1]
    return _span_is_asserted(compact_answer, final.start(), final.end())


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


def _throughput_record_matches(
    text: str,
    metric: str,
    claim: str | None,
    *,
    basis: str | None = None,
) -> bool:
    for record in _measurement_records(text):
        parts = [part for part in re.split(r"[，,]", record) if part.strip()]
        metric_indexes = [
            index
            for index, part in enumerate(parts)
            if _answer_has_exact_metric(part, metric)
        ]
        if not metric_indexes or not _answer_has_asserted_exact_metric(record, metric):
            continue
        index = metric_indexes[-1]
        selected = [parts[index]]
        adjacent = index - 1
        while adjacent >= 0 and _is_measurement_continuation(parts[adjacent]):
            selected.insert(0, parts[adjacent])
            adjacent -= 1
        adjacent = index + 1
        while adjacent < len(parts) and _is_measurement_continuation(
            parts[adjacent]
        ):
            selected.append(parts[adjacent])
            adjacent += 1
        candidate = "，".join(selected)
        compact_candidate = _compact(candidate)
        has_measurement_source = any(
            term in compact_candidate for term in QPS_MEASUREMENT_SOURCE_TERMS
        )
        has_statistics_window = (
            "窗口" in compact_candidate
            or any(
                term in compact_candidate for term in STATISTICS_WINDOW_TERMS
            )
            or bool(
                re.search(
                    r"持续\d+(?:\.\d+)?(?:秒|分钟|小时)",
                    compact_candidate,
                )
            )
        )
        has_foreign_subject = _has_foreign_measurement_subject(record)
        context_is_relevant = _metric_clause_is_relevant(
            candidate, claim, metric
        ) or (
            not has_foreign_subject
            and _metric_clause_is_relevant(record, claim, metric)
        )
        basis_is_local = not basis or _contains_basis(candidate, basis)
        if (
            context_is_relevant
            and has_measurement_source
            and has_statistics_window
            and basis_is_local
        ):
            return True
    return False


FOREIGN_MEASUREMENT_SUBJECTS = (
    "另一",
    "另一个",
    "其他项目",
    "其他服务",
    "其他接口",
    "平均年龄",
    "考试",
)


def _has_foreign_measurement_subject(value: str) -> bool:
    compact_value = _compact(value)
    return any(term in compact_value for term in FOREIGN_MEASUREMENT_SUBJECTS)


def _is_measurement_continuation(value: str) -> bool:
    compact_value = _compact(value)
    if _has_foreign_measurement_subject(compact_value):
        return False
    return (
        any(term in compact_value for term in QPS_MEASUREMENT_SOURCE_TERMS)
        or "窗口" in compact_value
        or any(term in compact_value for term in STATISTICS_WINDOW_TERMS)
        or bool(
            re.search(
                r"持续\d+(?:\.\d+)?(?:秒|分钟|小时)", compact_value
            )
        )
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
    if re.search(
        r"(?:并非真实|不是真实|并非最终|不是最终|不属实|废弃|"
        r"虚假|伪造|无效|作废|错误值|明显错误|口径.{0,8}错误|"
        r"有误|不正确|不对)",
        suffix,
    ):
        return False
    return True


def _span_measurement_record(answer: str, start: int, end: int) -> str:
    record_start = max(
        answer.rfind(mark, 0, start) for mark in ("。", "；", ";", "！", "？")
    ) + 1
    record_end_candidates = [
        position
        for mark in ("。", "；", ";", "！", "？")
        if (position := answer.find(mark, end)) >= 0
    ]
    record_end = min(record_end_candidates, default=len(answer))
    return answer[record_start:record_end]


def _percentage_records(answer: str) -> list[_PercentageRecord]:
    records: dict[tuple[int, int, float, float], _PercentageRecord] = {}
    patterns = (
        re.compile(
            r"(?P<num>\d+(?:\.\d+)?)\s*/\s*(?P<den>\d+(?:\.\d+)?)"
        ),
        re.compile(
            r"(?P<den>\d+(?:\.\d+)?)(?![\d.]|\s*%)"
            r"\s*(?:次|个|条|项|例|样本)?"
            r".{0,40}?(?:中|内|里).*?(?:成功|完成|命中|通过)\s*"
            r"(?P<num>\d+(?:\.\d+)?)"
        ),
        re.compile(
            r"(?P<den>\d+(?:\.\d+)?)(?![\d.]|\s*%)"
            r"\s*(?:次|个|条|项|例|样本)?"
            r".{0,40}?(?:中|内|里).*?(?:有\s*)?"
            r"(?P<num>\d+(?:\.\d+)?)\s*(?:次|个|条|项|例|样本)?"
            r"\s*(?:成功|完成|命中|通过)"
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(answer):
            record = _PercentageRecord(
                numerator=float(match.group("num")),
                denominator=float(match.group("den")),
                start=match.start(),
                end=match.end(),
            )
            records[
                (record.start, record.end, record.numerator, record.denominator)
            ] = record
    return sorted(records.values(), key=lambda item: (item.start, item.end))


def _percentage_record_matches(
    answer: str, metric: str, claim: str | None = None
) -> bool:
    target_match = re.search(r"\d+(?:\.\d+)?", metric)
    if target_match is None:
        return False
    target_text = target_match.group(0)
    target = float(target_text)
    decimal_places = len(target_text.partition(".")[2])
    tolerance = 0.5 * (10 ** -decimal_places) + 1e-9

    records = _percentage_records(answer)
    if not records:
        return False
    final = records[-1]
    if final.denominator <= 0 or not _span_is_asserted(
        answer, final.start, final.end
    ):
        return False
    measurement_record = _span_measurement_record(answer, final.start, final.end)
    final_clause = _span_clause(answer, final.start, final.end)
    context_is_relevant = (
        _metric_clause_is_relevant(final_clause, claim, metric)
        or (claim is not None and _text_asserts_claim(measurement_record, claim))
        or (
            _compact(final_clause).startswith(("而是", "实际", "正确值", "应为"))
            and _metric_clause_is_relevant(measurement_record, claim, metric)
        )
    )
    return context_is_relevant and abs(
        final.numerator / final.denominator * 100 - target
    ) <= tolerance
