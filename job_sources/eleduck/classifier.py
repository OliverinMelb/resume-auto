"""Deterministic, explainable classification for public Eleduck posts."""

from __future__ import annotations

import re

from .models import Classification, FeedItem, PostCategory


_STRONG_NEGATED_RECRUITMENT = re.compile(
    r"(?:暂不|不再|不会|无意|停止|暂停|取消|结束|已不|已经不)"
    r"[^。；，,：:！？?!\n]{0,8}(?:招聘|招人)"
    r"|(?:招聘|招人)"
    r"[^。；，,：:！？?!\n]{0,8}(?:结束|暂停|取消|招满)"
    r"|不(?:招聘|招人)"
    r"|(?:这|此|本文|本帖)?不(?:是|属于|算)(?:一次|一则|一个)?招聘"
    r"|非招聘"
)
_WEAK_NEGATED_RECRUITMENT = re.compile(
    r"(?:无需|不需要|不要求)"
    r"[^。；，,：:！？?!\n]{0,8}(?:招聘|招人)"
    r"(?!(?:方面|相关|工作)?经验)"
)
_DISCUSSION_MARKERS = (
    "请问",
    "如何",
    "怎么",
    "为什么",
    "为何",
    "是否",
    "靠谱吗",
    "大家觉得",
    "求交流",
    "讨论",
    "经验分享",
    "?",
    "？",
)
_INTERVIEW_MARKERS = ("聊聊", "访谈", "聊30分钟", "聊 30 分钟")


def classify(item: FeedItem) -> Classification:
    title = item.title.lower()
    text = f"{title} {item.description.lower()}"
    negated_recruitment = bool(
        _STRONG_NEGATED_RECRUITMENT.search(text)
        or _WEAK_NEGATED_RECRUITMENT.search(text)
    )
    title_is_discussion = _matches(title, _DISCUSSION_MARKERS)
    discussion_evidence = _matches(text, _DISCUSSION_MARKERS)
    recruitment_intent = (
        not negated_recruitment
        and not title_is_discussion
        and _matches(
            text,
            ("招聘", "招人", "直招", "急聘", "岗位开放", "欢迎投递"),
        )
    )
    job_structure = _matches(
        text,
        (
            "岗位职责",
            "任职要求",
            "职位要求",
            "薪资",
            "全职",
            "兼职",
            "工作地点",
            "工程师",
            "设计师",
            "产品经理",
            "助理",
            "专员",
            "运营",
            "新媒体",
        ),
    )
    candidates: list[PostCategory] = []
    rules: list[str] = []
    if recruitment_intent and job_structure:
        candidates.append(PostCategory.RECRUITMENT)
        rules.extend(("recruitment.intent", "recruitment.structure"))

    if _matches(text, ("求职", "找工作", "寻求岗位", "可入职")) and _matches(
        text,
        ("简历", "作品集", "经验", "擅长", "本人"),
    ):
        candidates.append(PostCategory.JOB_SEEKING)
        rules.extend(("job_seeking.intent", "job_seeking.candidate"))

    if _matches(
        text,
        ("外包", "项目需求", "开发需求", "找团队", "寻技术开发团队", "招标"),
    ) and _matches(
        text,
        ("交付", "报价", "预算", "开发团队", "项目成果"),
    ):
        candidates.append(PostCategory.OUTSOURCING_REQUEST)
        rules.extend(("outsourcing.object", "outsourcing.transaction"))

    if _matches(text, ("合伙人", "联合创始", "共同创业")) and _matches(
        text,
        ("股权", "投资", "融资", "共同承担", "创业"),
    ):
        candidates.append(PostCategory.PARTNER_SEEKING)
        rules.extend(("partner.subject", "partner.venture"))

    transactional_candidates = tuple(dict.fromkeys(candidates))
    if len(transactional_candidates) == 1:
        category = transactional_candidates[0]
        return Classification(category, tuple(rules), f"matched {category.value}")
    if len(transactional_candidates) > 1:
        return Classification(
            PostCategory.UNCERTAIN,
            tuple(rules) + ("uncertain.conflict",),
            "multiple transactional categories matched",
            transactional_candidates,
        )
    if negated_recruitment and (
        discussion_evidence or _matches(text, _INTERVIEW_MARKERS)
    ):
        return Classification(
            PostCategory.DISCUSSION,
            ("recruitment.negated", "discussion.exchange"),
            "recruitment is negated and discussion intent is explicit",
        )
    if negated_recruitment:
        return Classification(
            PostCategory.UNCERTAIN,
            ("recruitment.negated",),
            "recruitment language is explicitly negated",
        )

    fallback_candidates: list[PostCategory] = []
    fallback_rules: list[str] = []
    if _matches(
        text,
        ("推广", "优惠", "导流", "广告", "自荐帖", "产品自荐", "服务自荐"),
    ):
        fallback_candidates.append(PostCategory.ADVERTISEMENT)
        fallback_rules.append("advertisement.promotion")
    if discussion_evidence:
        fallback_candidates.append(PostCategory.DISCUSSION)
        fallback_rules.append("discussion.exchange")
    if len(fallback_candidates) == 1:
        category = fallback_candidates[0]
        return Classification(
            category,
            tuple(fallback_rules),
            f"matched {category.value}",
        )
    if len(fallback_candidates) > 1:
        return Classification(
            PostCategory.UNCERTAIN,
            tuple(fallback_rules) + ("uncertain.conflict",),
            "multiple non-transactional categories matched",
            tuple(fallback_candidates),
        )
    return Classification(
        PostCategory.UNCERTAIN,
        ("uncertain.insufficient_evidence",),
        "insufficient deterministic evidence",
    )


def _matches(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)
