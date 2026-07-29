from __future__ import annotations

import csv
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

import create_job_assessment_report as base


ROOT = Path(__file__).resolve().parents[1]
JOBS_PATH = ROOT / "csv_export" / "jobs.csv"
OUTPUT_PATH = (
    ROOT
    / "output"
    / "pdf"
    / "marketing_business_trainee_job_assessment_20260729.pdf"
)

REPORT_DATE = "2026-07-29"
REPORT_NAME = "市场营销候选人"
REPORT_ROLE = "市场营销 / 商科管培"


def contains(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def count_groups(text: str, groups: list[list[str]]) -> int:
    return sum(contains(text, group) for group in groups)


def clean_company(value: str) -> str:
    return re.sub(r"^\s*公司名称\s*", "", value or "").strip()


@dataclass
class MarketingJob:
    row: dict[str, str]
    title: str
    detail: str
    company: str
    search_keyword: str
    direction: str
    bucket: str
    score: int
    salary: tuple[float, float] | None


def parse_salary(value: str) -> tuple[float, float] | None:
    if "K" not in (value or "").upper():
        return None
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K",
        value or "",
        re.I,
    )
    if not match:
        return None
    low, high = map(float, match.groups())
    if low <= 0 or high < low:
        return None
    return low, high


def infer_direction(title: str, detail: str) -> str:
    text = f"{title} {detail}"
    if "管培" in title:
        if contains(title, [r"电商", r"亚马逊", r"直播", r"增长"]):
            return "内容 / 增长 / 电商"
        if contains(title, [r"策略", r"分析", r"商业拓展", r"产品"]):
            return "营销运营 / 商业分析"
        return "商科 / 运营管培"
    if contains(
        text,
        [
            r"品牌",
            r"产品营销",
            r"市场策划",
            r"整合营销",
            r"市场推广",
            r"新品上市",
        ],
    ):
        return "品牌 / 产品营销"
    if contains(
        text,
        [
            r"电商",
            r"亚马逊",
            r"1688",
            r"小红书",
            r"抖音",
            r"内容运营",
            r"直播运营",
            r"增长",
        ],
    ):
        return "内容 / 增长 / 电商"
    if contains(
        text,
        [
            r"数据分析",
            r"市场分析",
            r"策略分析",
            r"商业分析",
            r"产品经理",
            r"运营管培",
            r"商业拓展",
        ],
    ):
        return "营销运营 / 商业分析"
    if "管培" in text:
        return "商科 / 运营管培"
    return "销售及其他"


def score_job(row: dict[str, str]) -> MarketingJob:
    title = re.sub(r"\s+", " ", row.get("title", "")).strip()
    detail = re.sub(r"\s+", " ", row.get("detail", "")).strip()
    text = f"{title} {detail}"
    company = clean_company(row.get("company", ""))
    search_keyword = row.get("search_keyword", "").strip()
    direction = infer_direction(title, detail)

    title_core = contains(
        title,
        [
            r"品牌",
            r"产品营销",
            r"市场专员",
            r"市场策划",
            r"市场分析",
            r"电商运营",
            r"内容运营",
            r"商业拓展",
            r"策略分析",
            r"产品经理",
        ],
    )
    title_adjacent = contains(
        title,
        [
            r"运营",
            r"分析",
            r"管培",
            r"商业",
            r"市场",
            r"产品",
            r"BD",
        ],
    )

    role_score = 25 if title_core else 16 if title_adjacent else 5
    campaign_score = min(
        20,
        count_groups(
            text,
            [
                [r"品牌", r"brand"],
                [r"营销", r"推广", r"传播"],
                [r"活动", r"campaign", r"策划"],
                [r"新品", r"上市", r"launch"],
            ],
        )
        * 5,
    )
    product_score = min(
        15,
        count_groups(
            text,
            [
                [r"产品", r"品类"],
                [r"用户", r"消费者", r"客户洞察"],
                [r"竞品", r"市场调研", r"定位"],
            ],
        )
        * 5,
    )
    content_score = min(
        10,
        count_groups(
            text,
            [
                [r"内容", r"文案", r"创意"],
                [r"小红书", r"抖音", r"社媒", r"新媒体", r"KOL", r"KOC"],
            ],
        )
        * 5,
    )
    commerce_score = min(
        10,
        count_groups(
            text,
            [
                [r"电商", r"亚马逊", r"1688", r"平台运营"],
                [r"GMV", r"转化", r"流量", r"投放", r"ROI"],
            ],
        )
        * 5,
    )
    data_score = min(
        10,
        count_groups(
            text,
            [
                [r"数据", r"分析", r"报表"],
                [r"复盘", r"指标", r"A/B", r"效果评估"],
            ],
        )
        * 5,
    )
    project_score = min(
        5,
        count_groups(
            text,
            [
                [r"项目", r"落地", r"执行"],
                [r"跨部门", r"协同", r"统筹", r"协调"],
            ],
        )
        * 3,
    )
    base_score = 0
    if contains(text, [r"本科", r"硕士", r"研究生"]):
        base_score += 3
    if contains(text, [r"英语", r"英文", r"国际", r"海外"]):
        base_score += 2

    score = (
        role_score
        + campaign_score
        + product_score
        + content_score
        + commerce_score
        + data_score
        + project_score
        + base_score
    )

    hard_irrelevant = contains(
        text,
        [
            r"融资租赁",
            r"保险",
            r"证券",
            r"理财",
            r"房地产",
            r"房产经纪",
            r"门店店长",
            r"储备店长",
            r"餐饮",
            r"火锅",
            r"生产管培",
            r"供应链管培",
            r"技术管培",
            r"PACK研发",
            r"系统开发",
        ],
    )
    pure_sales = contains(
        text,
        [
            r"陌拜",
            r"电销",
            r"电话营销",
            r"面销",
            r"销售提成",
            r"邀约意向",
            r"客户开发",
            r"招生结果",
        ],
    ) or (
        "销售" in title
        and not contains(title, [r"市场", r"品牌", r"产品", r"运营", r"分析"])
    )
    education_risk = contains(text, [r"清北", r"211学历", r"985", r"双一流"])
    management_risk = contains(
        text,
        [r"带领团队", r"团队管理经验", r"销售管理策略", r"管理层培养"],
    )

    if hard_irrelevant:
        score -= 35
    if pure_sales:
        score -= 20
    if education_risk:
        score -= 18
    if management_risk:
        score -= 8
    score = max(0, min(100, score))

    if hard_irrelevant or pure_sales:
        bucket = "低优先"
    elif title_core and score >= 55:
        bucket = "核心方向"
    elif title_adjacent and score >= 45:
        bucket = "扩展方向"
    else:
        bucket = "低优先"

    return MarketingJob(
        row=row,
        title=title,
        detail=detail,
        company=company,
        search_keyword=search_keyword,
        direction=direction,
        bucket=bucket,
        score=score,
        salary=parse_salary(row.get("salary", "")),
    )


def load_jobs() -> list[MarketingJob]:
    with JOBS_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        return [score_job(row) for row in csv.DictReader(handle)]


def percentile(values: list[float], point: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    position = (len(ordered) - 1) * point
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def relevant_jobs(jobs: list[MarketingJob]) -> list[MarketingJob]:
    return [job for job in jobs if job.bucket in {"核心方向", "扩展方向"}]


def high_fit_jobs(jobs: list[MarketingJob]) -> list[MarketingJob]:
    return [job for job in relevant_jobs(jobs) if job.score >= 70]


def salary_midpoints(jobs: list[MarketingJob]) -> list[float]:
    return [
        (job.salary[0] + job.salary[1]) / 2
        for job in jobs
        if job.salary is not None
    ]


def salary_stats(jobs: list[MarketingJob]) -> dict[str, float | int]:
    relevant = relevant_jobs(jobs)
    high_fit = high_fit_jobs(jobs)
    valid = [job for job in relevant if job.salary]
    high_fit_salary = salary_midpoints(high_fit)
    analysis_values = (
        high_fit_salary if len(high_fit_salary) >= 5 else salary_midpoints(relevant)
    )
    keyword_counts = {
        key: sum(job.search_keyword == key for job in jobs)
        for key in ("市场营销", "商科管培")
    }
    return {
        "all_jobs": len(jobs),
        "marketing_jobs": keyword_counts["市场营销"],
        "trainee_jobs": keyword_counts["商科管培"],
        "relevant_jobs": len(relevant),
        "high_fit_jobs": len(high_fit),
        "salary_samples": len(valid),
        "p25": percentile(analysis_values, 0.25),
        "median": percentile(analysis_values, 0.5),
        "p75": percentile(analysis_values, 0.75),
        "mean": statistics.mean(analysis_values) if analysis_values else 0,
    }


def styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "body": ParagraphStyle(
            "marketing_body",
            parent=sample["BodyText"],
            fontName="ReportSans",
            fontSize=10,
            leading=16,
            textColor=base.INK,
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "marketing_small",
            parent=sample["BodyText"],
            fontName="ReportSans",
            fontSize=8,
            leading=12,
            textColor=base.MUTED,
        ),
        "title": ParagraphStyle(
            "marketing_title",
            parent=sample["Heading1"],
            fontName="ReportSans",
            fontSize=25,
            leading=34,
            textColor=base.NAVY,
            spaceAfter=6,
        ),
        "h1": ParagraphStyle(
            "marketing_h1",
            parent=sample["Heading1"],
            fontName="ReportSans",
            fontSize=17,
            leading=24,
            textColor=base.NAVY,
            spaceAfter=7,
        ),
        "h2": ParagraphStyle(
            "marketing_h2",
            parent=sample["Heading2"],
            fontName="ReportSans",
            fontSize=12,
            leading=17,
            textColor=base.BLUE,
            spaceBefore=5,
            spaceAfter=4,
        ),
        "card": ParagraphStyle(
            "marketing_card",
            parent=sample["BodyText"],
            fontName="ReportSans",
            fontSize=9,
            leading=14,
            textColor=base.INK,
            alignment=TA_CENTER,
        ),
        "callout": ParagraphStyle(
            "marketing_callout",
            parent=sample["BodyText"],
            fontName="ReportSans",
            fontSize=11,
            leading=17,
            textColor=base.NAVY,
        ),
    }


def p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def rich(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


class MarketingDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str):
        super().__init__(
            filename,
            pagesize=A4,
            leftMargin=17 * mm,
            rightMargin=17 * mm,
            topMargin=17 * mm,
            bottomMargin=15 * mm,
            title="市场营销候选人岗位评估报告",
            author="OpenAI Codex",
        )
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="normal",
        )
        self.addPageTemplates(
            [PageTemplate(id="report", frames=frame, onPage=self._draw_page)]
        )

    def _draw_page(self, canvas, doc) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(base.GRID)
        canvas.setLineWidth(0.5)
        canvas.line(
            self.leftMargin,
            height - 11 * mm,
            width - self.rightMargin,
            height - 11 * mm,
        )
        canvas.setFont("ReportSans", 7.5)
        canvas.setFillColor(base.MUTED)
        canvas.drawString(
            self.leftMargin,
            height - 8.5 * mm,
            f"{REPORT_NAME} | {REPORT_ROLE}岗位评估",
        )
        canvas.drawString(
            self.leftMargin,
            8 * mm,
            f"数据来源：候选人简历图片与岗位 CSV，生成日期：{REPORT_DATE}",
        )
        canvas.drawRightString(width - self.rightMargin, 8 * mm, f"{doc.page}")
        canvas.restoreState()


def section(text: str, s: dict[str, ParagraphStyle]) -> list:
    return [
        p(text, s["h1"]),
        HRFlowable(width="100%", thickness=1.1, color=base.TEAL, spaceAfter=8),
    ]


def callout(
    title: str,
    body: str,
    s: dict[str, ParagraphStyle],
    background=base.PALE_BLUE,
) -> Table:
    table = Table(
        [
            [rich(f"<b>{escape(title)}</b>", s["h2"])],
            [p(body, s["callout"])],
        ],
        colWidths=[174 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def metric_cards(
    stats: dict[str, float | int], s: dict[str, ParagraphStyle]
) -> Table:
    data = [
        [
            rich("<b>岗位样本</b>", s["card"]),
            rich("<b>相关岗位</b>", s["card"]),
            rich("<b>高匹配岗位</b>", s["card"]),
            rich("<b>有效薪资样本</b>", s["card"]),
        ],
        [
            rich(
                f"<font size='20' color='{base.NAVY}'>{stats['all_jobs']}</font><br/>条",
                s["card"],
            ),
            rich(
                f"<font size='20' color='{base.TEAL}'>{stats['relevant_jobs']}</font><br/>条",
                s["card"],
            ),
            rich(
                f"<font size='20' color='{base.ORANGE}'>{stats['high_fit_jobs']}</font><br/>条",
                s["card"],
            ),
            rich(
                f"<font size='20' color='{base.BLUE}'>{stats['salary_samples']}</font><br/>条",
                s["card"],
            ),
        ],
    ]
    table = Table(data, colWidths=[43.5 * mm] * 4, rowHeights=[10 * mm, 19 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), base.LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def salary_recommendation(stats: dict[str, float | int]) -> tuple[int, int, int, int, int, int]:
    median = float(stats["median"])
    p25 = float(stats["p25"])
    p75 = float(stats["p75"])
    if not median:
        return 8, 12, 10, 15, 15, 20
    steady_low = max(5, int(round(p25 - 2)))
    steady_high = max(steady_low + 3, int(round(median)))
    main_low = max(steady_low + 1, int(round(p25)))
    main_high = max(main_low + 4, int(round(p75)))
    stretch_low = max(main_high - 2, int(round(p75)))
    stretch_high = max(stretch_low + 5, int(round(p75 + 5)))
    return (
        steady_low,
        steady_high,
        main_low,
        main_high,
        stretch_low,
        stretch_high,
    )


def salary_band_chart(stats: dict[str, float | int]) -> Drawing:
    bands = salary_recommendation(stats)
    rows = [
        ("稳妥接受", bands[0], bands[1], base.BLUE),
        ("主投谈薪", bands[2], bands[3], base.TEAL),
        ("冲刺区间", bands[4], bands[5], base.ORANGE),
    ]
    maximum = max(30, bands[-1] + 5)
    width = 174 * mm
    height = 59 * mm
    drawing = Drawing(width, height)
    left = 35 * mm
    right = width - 7 * mm
    chart_width = right - left
    tick_step = 5
    for tick in range(0, maximum + 1, tick_step):
        x = left + chart_width * tick / maximum
        drawing.add(
            Line(
                x,
                8 * mm,
                x,
                height - 7 * mm,
                strokeColor=colors.HexColor("#E4E9ED"),
                strokeWidth=0.5,
            )
        )
        drawing.add(
            String(
                x - 2 * mm,
                3 * mm,
                str(tick),
                fontName="ReportSans",
                fontSize=7,
                fillColor=base.MUTED,
            )
        )
    for index, (label, low, high, color) in enumerate(rows):
        y = height - (15 + index * 14) * mm
        drawing.add(
            String(
                0,
                y + 1.5 * mm,
                label,
                fontName="ReportSans",
                fontSize=9,
                fillColor=base.INK,
            )
        )
        drawing.add(
            Rect(
                left,
                y,
                chart_width,
                7 * mm,
                fillColor=base.LIGHT,
                strokeColor=None,
            )
        )
        x = left + chart_width * low / maximum
        bar_width = chart_width * (high - low) / maximum
        drawing.add(
            Rect(x, y, bar_width, 7 * mm, fillColor=color, strokeColor=None)
        )
        drawing.add(
            String(
                x + 2 * mm,
                y + 2 * mm,
                f"{low}-{high}K",
                fontName="ReportSans",
                fontSize=8,
                fillColor=colors.white,
            )
        )
    drawing.add(
        String(
            right - 18 * mm,
            3 * mm,
            "税前月薪 / RMB",
            fontName="ReportSans",
            fontSize=7,
            fillColor=base.MUTED,
        )
    )
    return drawing


def horizontal_chart(
    items: list[tuple[str, float, str, colors.Color]],
    maximum: float,
    height_per_row: float = 13,
) -> Drawing:
    width = 174 * mm
    height = (12 + len(items) * height_per_row) * mm
    drawing = Drawing(width, height)
    label_width = 47 * mm
    value_width = 25 * mm
    chart_width = width - label_width - value_width
    for index, (label, value, display, color) in enumerate(items):
        y = height - (12 + index * height_per_row) * mm
        drawing.add(
            String(
                0,
                y + 1.8 * mm,
                label,
                fontName="ReportSans",
                fontSize=9,
                fillColor=base.INK,
            )
        )
        drawing.add(
            Rect(
                label_width,
                y,
                chart_width,
                7 * mm,
                fillColor=base.LIGHT,
                strokeColor=None,
            )
        )
        drawing.add(
            Rect(
                label_width,
                y,
                chart_width * min(value, maximum) / maximum,
                7 * mm,
                fillColor=color,
                strokeColor=None,
            )
        )
        drawing.add(
            String(
                label_width + chart_width + 3 * mm,
                y + 1.8 * mm,
                display,
                fontName="ReportSans",
                fontSize=8,
                fillColor=base.MUTED,
            )
        )
    return drawing


def signal_chart(jobs: list[MarketingJob]) -> Drawing:
    relevant = relevant_jobs(jobs)
    patterns = [
        ("品牌 / 活动 / 整合营销", [r"品牌", r"活动", r"营销", r"推广"], base.TEAL),
        ("产品 / 用户 / 市场洞察", [r"产品", r"用户", r"市场分析", r"竞品"], base.BLUE),
        ("内容 / 社媒 / 创意", [r"内容", r"小红书", r"抖音", r"新媒体", r"创意"], base.ORANGE),
        ("电商 / 增长 / 投放", [r"电商", r"增长", r"投放", r"转化", r"流量"], base.GREEN),
        ("数据分析 / 复盘", [r"数据", r"分析", r"复盘", r"指标"], base.RED),
    ]
    items = []
    for label, terms, color in patterns:
        count = sum(contains(f"{job.title} {job.detail}", terms) for job in relevant)
        ratio = count / len(relevant) * 100 if relevant else 0
        items.append((label, ratio, f"{ratio:.0f}%", color))
    return horizontal_chart(items, 100)


def capability_chart() -> Drawing:
    items = [
        ("整合营销与活动落地", 94, "核心优势", base.TEAL),
        ("品牌 / 产品营销", 92, "核心优势", base.BLUE),
        ("内容与社媒运营", 88, "较强", base.GREEN),
        ("电商与增长实践", 86, "较强", base.ORANGE),
        ("投放与数据优化", 82, "较强", base.RED),
        ("管培所需管理深度", 68, "需验证", colors.HexColor("#9BA8B1")),
    ]
    return horizontal_chart(items, 100)


def direction_salary_rows(
    jobs: list[MarketingJob],
) -> list[tuple[str, float, float, str, colors.Color]]:
    colors_by_direction = {
        "品牌 / 产品营销": base.BLUE,
        "内容 / 增长 / 电商": base.TEAL,
        "营销运营 / 商业分析": base.ORANGE,
        "商科 / 运营管培": base.GREEN,
    }
    rows = []
    for direction in colors_by_direction:
        values = salary_midpoints(
            [
                job
                for job in relevant_jobs(jobs)
                if job.direction == direction and job.salary
            ]
        )
        if not values:
            continue
        low = percentile(values, 0.25)
        high = percentile(values, 0.75)
        if high - low < 3:
            low = max(0, low - 1.5)
            high += 1.5
        rows.append(
            (
                direction,
                low,
                high,
                f"{low:.0f}-{high:.0f}K",
                colors_by_direction[direction],
            )
        )
    return rows


def role_salary_chart(jobs: list[MarketingJob]) -> Drawing:
    rows = direction_salary_rows(jobs)
    width = 174 * mm
    height = (13 + len(rows) * 15) * mm
    drawing = Drawing(width, height)
    label_width = 47 * mm
    value_width = 25 * mm
    chart_width = width - label_width - value_width
    maximum = max(30, max((row[2] for row in rows), default=25) + 5)
    for index, (label, low, high, display, color) in enumerate(rows):
        y = height - (13 + index * 15) * mm
        drawing.add(
            String(
                0,
                y + 1.8 * mm,
                label,
                fontName="ReportSans",
                fontSize=9,
                fillColor=base.INK,
            )
        )
        drawing.add(
            Rect(
                label_width,
                y,
                chart_width,
                7 * mm,
                fillColor=base.LIGHT,
                strokeColor=None,
            )
        )
        x = label_width + chart_width * low / maximum
        drawing.add(
            Rect(
                x,
                y,
                chart_width * (high - low) / maximum,
                7 * mm,
                fillColor=color,
                strokeColor=None,
            )
        )
        drawing.add(
            String(
                label_width + chart_width + 3 * mm,
                y + 1.8 * mm,
                display,
                fontName="ReportSans",
                fontSize=8,
                fillColor=base.MUTED,
            )
        )
    return drawing


def build_story(
    jobs: list[MarketingJob],
    stats: dict[str, float | int],
    s: dict[str, ParagraphStyle],
) -> list:
    bands = salary_recommendation(stats)
    story: list = [
        Spacer(1, 18 * mm),
        rich("<font color='#2C8A84'>客户版岗位评估 / 2026</font>", s["body"]),
        Spacer(1, 3 * mm),
        p(REPORT_NAME, s["title"]),
        p(REPORT_ROLE, s["h1"]),
        Spacer(1, 4 * mm),
        p(
            "基于候选人简历和岗位样本的匿名聚合评估。报告展示薪资区间、岗位趋势与能力画像，不披露具体公司或岗位信息。",
            s["body"],
        ),
        Spacer(1, 7 * mm),
        callout(
            "建议薪资预期",
            (
                f"当前样本口径下，建议将税前月薪 {bands[2]}-{bands[3]}K 作为主要谈薪区间；"
                f"{bands[0]}-{bands[1]}K 属于较稳妥的接受范围；具备成熟品牌、新品上市、"
                f"电商增长或高质量管培轮岗的岗位可尝试 {bands[4]}-{bands[5]}K。"
            ),
            s,
            base.PALE_GREEN,
        ),
        Spacer(1, 7 * mm),
        salary_band_chart(stats),
        Spacer(1, 7 * mm),
        metric_cards(stats, s),
        Spacer(1, 6 * mm),
        p(
            "口径说明：薪资为人民币税前月薪，不含提成、奖金、股票和福利。高薪异常岗位、纯销售、门店餐饮、金融保险及技术研发岗位不用于核心薪资判断。",
            s["small"],
        ),
        PageBreak(),
    ]

    story.extend(section("一、市场与岗位样本概览", s))
    story.extend(
        [
            p(
                (
                    f"本次样本共 {stats['all_jobs']} 条，其中市场营销关键词 {stats['marketing_jobs']} 条、"
                    f"商科管培关键词 {stats['trainee_jobs']} 条。规则识别出 {stats['relevant_jobs']} 条与候选人"
                    f"能力相关的岗位，其中 {stats['high_fit_jobs']} 条达到高匹配标准。高匹配或相关岗位的"
                    f"薪资中位值约为 {stats['median']:.1f}K/月。"
                ),
                s["body"],
            ),
            Spacer(1, 4 * mm),
            Table(
                [
                    [
                        rich(
                            f"<font size='20' color='{base.BLUE}'>{stats['p25']:.1f}K</font><br/>相关样本 P25",
                            s["card"],
                        ),
                        rich(
                            f"<font size='20' color='{base.TEAL}'>{stats['median']:.1f}K</font><br/>相关样本中位",
                            s["card"],
                        ),
                        rich(
                            f"<font size='20' color='{base.ORANGE}'>{stats['p75']:.1f}K</font><br/>相关样本 P75",
                            s["card"],
                        ),
                    ]
                ],
                colWidths=[58 * mm] * 3,
                rowHeights=[26 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), base.LIGHT),
                        ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
                        ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ]
                ),
            ),
            Spacer(1, 7 * mm),
            p("相关岗位常见要求", s["h2"]),
            signal_chart(jobs),
            Spacer(1, 5 * mm),
            callout(
                "样本解读",
                "“市场营销”搜索结果混入大量销售、招生与金融岗位；“商科管培”结果则混入门店、餐饮、生产和技术研发项目。对候选人更有价值的是品牌与产品营销、内容增长、电商运营、商业分析及具有真实轮岗培养机制的管培项目。",
                s,
                base.PALE_ORANGE,
            ),
            PageBreak(),
        ]
    )

    story.extend(section("二、候选人能力画像", s))
    story.extend(
        [
            p(
                "候选人的优势是“品牌与产品营销 + 内容传播 + 电商增长 + 数据复盘”的复合实践，而不是纯陌生销售。百度实习截至报告生成日仍在进行中，因此只采用简历中已经明确写出的项目和结果。",
                s["body"],
            ),
            Spacer(1, 4 * mm),
            capability_chart(),
            Spacer(1, 7 * mm),
            Table(
                [
                    [
                        rich("<b>可直接用于谈薪的证据</b>", s["h2"]),
                        rich("<b>目前影响薪资上限的缺口</b>", s["h2"]),
                    ],
                    [
                        p(
                            "百度整合营销与新品上市；搜索投放峰值 ROI 9.9；A/B 测试和竞品优化；阳狮品牌整合传播与电商大促；江苏卫视活动和内容转化 500+ 用户；跨境店铺月营收超过 5 万元、日均订单 200+。",
                            s["body"],
                        ),
                        p(
                            "多段经历仍以实习或个人项目为主；缺少正式全职品牌岗位年限、年度预算规模和稳定 GMV 增长曲线；商科管培所需的团队管理、财务建模和跨职能轮岗证据仍需在面试中验证。",
                            s["body"],
                        ),
                    ],
                ],
                colWidths=[87 * mm, 87 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, -1), base.PALE_GREEN),
                        ("BACKGROUND", (1, 0), (1, -1), base.PALE_ORANGE),
                        ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
                        ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 9),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                ),
            ),
            Spacer(1, 7 * mm),
            callout(
                "定位建议",
                "对外定位建议使用“品牌 / 产品营销候选人”，突出整合 Campaign、新品上市、内容矩阵、电商转化和投放优化。商科管培作为扩展方向，只选择包含市场、产品、运营或商业分析轮岗的项目。",
                s,
            ),
            PageBreak(),
        ]
    )

    story.extend(section("三、岗位方向与投递策略", s))
    story.extend(
        [
            p(
                "以下区间按相关岗位的可解析薪资样本估算，用于判断投递重点。部分方向样本较少，区间不代表具体公司承诺。",
                s["body"],
            ),
            Spacer(1, 4 * mm),
            role_salary_chart(jobs),
            Spacer(1, 7 * mm),
            Table(
                [
                    [
                        rich("<b>优先级</b>", s["h2"]),
                        rich("<b>岗位方向</b>", s["h2"]),
                        rich("<b>建议</b>", s["h2"]),
                    ],
                    [
                        p("第一优先", s["body"]),
                        p("品牌 / 产品营销", s["body"]),
                        p(
                            "突出百度新品上市、整合营销、投放 ROI、竞品分析和阳狮品牌传播项目。",
                            s["body"],
                        ),
                    ],
                    [
                        p("第二优先", s["body"]),
                        p("内容 / 增长 / 电商运营", s["body"]),
                        p(
                            "突出小红书、抖音、内容矩阵、A/B 测试、用户转化和跨境店铺经营结果。",
                            s["body"],
                        ),
                    ],
                    [
                        p("扩展方向", s["body"]),
                        p("营销运营 / 商业分析 / 管培", s["body"]),
                        p(
                            "只投递具有真实轮岗、明确导师和市场或产品模块的项目，回避泛销售及门店储备岗。",
                            s["body"],
                        ),
                    ],
                    [
                        p("谨慎投递", s["body"]),
                        p("纯销售、金融、餐饮门店及技术管培", s["body"]),
                        p(
                            "与候选人现有营销资产偏离明显，高薪字段也可能依赖提成或与职责不匹配。",
                            s["body"],
                        ),
                    ],
                ],
                colWidths=[28 * mm, 64 * mm, 82 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), base.NAVY),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, base.LIGHT],
                        ),
                        ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
                        ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                ),
            ),
        ]
    )
    return story


def main() -> None:
    base.register_fonts()
    jobs = load_jobs()
    stats = salary_stats(jobs)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document = MarketingDocTemplate(str(OUTPUT_PATH))
    document.build(build_story(jobs, stats, styles()))
    print(f"output={OUTPUT_PATH}")
    print(
        f"jobs={stats['all_jobs']} marketing={stats['marketing_jobs']} "
        f"trainee={stats['trainee_jobs']} relevant={stats['relevant_jobs']} "
        f"high_fit={stats['high_fit_jobs']} salary_samples={stats['salary_samples']}"
    )
    print(
        f"salary_p25={stats['p25']:.1f} median={stats['median']:.1f} "
        f"p75={stats['p75']:.1f}"
    )
    for job in sorted(jobs, key=lambda item: item.score, reverse=True)[:12]:
        print(
            f"{job.score:03d}|{job.bucket}|{job.direction}|"
            f"{job.title}|{job.row.get('salary', '')}"
        )


if __name__ == "__main__":
    main()
