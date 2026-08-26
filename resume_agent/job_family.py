"""Known job-title families used to prevent incompatible JD combinations."""

import re


def known_job_family(title: str) -> str | None:
    normalized = " ".join(title.casefold().split())
    ai_agent = (
        re.search(r"(?:^|[^a-z0-9])ai(?:[^a-z0-9]|$)", normalized)
        or any(
            marker in normalized
            for marker in ("agent", "llm", "人工智能", "智能体", "大模型")
        )
    )
    engineering = any(
        marker in normalized
        for marker in (
            "engineer", "developer", "development", "architect",
            "工程师", "开发", "架构",
        )
    )
    if ai_agent and engineering:
        return "ai_engineering"
    sales = (
        re.search(r"(?:^|[^a-z0-9])(?:sales|bd)(?:[^a-z0-9]|$)", normalized)
        or "business development" in normalized
        or "account executive" in normalized
        or any(marker in normalized for marker in ("销售", "商务拓展", "业务拓展"))
    )
    if sales:
        return "sales"
    finance_accounting = (
        re.search(
            r"(?:^|[^a-z0-9])(?:accountant|accounting|auditor|audit|finance|"
            r"financial|controller|bookkeeper)(?:[^a-z0-9]|$)",
            normalized,
        )
        or any(marker in normalized for marker in ("会计", "财务", "审计"))
    )
    if finance_accounting:
        return "finance_accounting"
    healthcare_nursing = (
        re.search(
            r"(?:^|[^a-z0-9])(?:nurse|nursing|physician|doctor|clinician|"
            r"medical|healthcare)(?:[^a-z0-9]|$)",
            normalized,
        )
        or "health care" in normalized
        or any(
            marker in normalized
            for marker in ("护士", "护理", "医生", "医师", "医疗", "临床")
        )
    )
    if healthcare_nursing:
        return "healthcare_nursing"
    return None
