from __future__ import annotations

import re
import statistics
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, Spacer, Table, TableStyle

import create_job_assessment_report as base


OUTPUT_PATH = base.OUTPUT_PATH


def styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "body": ParagraphStyle(
            "client_body", parent=sample["BodyText"], fontName="ReportSans",
            fontSize=10, leading=16, textColor=base.INK, spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "client_small", parent=sample["BodyText"], fontName="ReportSans",
            fontSize=8, leading=12, textColor=base.MUTED,
        ),
        "title": ParagraphStyle(
            "client_title", parent=sample["Heading1"], fontName="ReportSans",
            fontSize=25, leading=34, textColor=base.NAVY, spaceAfter=6,
        ),
        "h1": ParagraphStyle(
            "client_h1", parent=sample["Heading1"], fontName="ReportSans",
            fontSize=17, leading=24, textColor=base.NAVY, spaceAfter=7,
        ),
        "h2": ParagraphStyle(
            "client_h2", parent=sample["Heading2"], fontName="ReportSans",
            fontSize=12, leading=17, textColor=base.BLUE, spaceBefore=5, spaceAfter=4,
        ),
        "number": ParagraphStyle(
            "client_number", parent=sample["BodyText"], fontName="ReportSans",
            fontSize=22, leading=27, textColor=base.NAVY, alignment=TA_CENTER,
        ),
        "card": ParagraphStyle(
            "client_card", parent=sample["BodyText"], fontName="ReportSans",
            fontSize=9, leading=14, textColor=base.INK, alignment=TA_CENTER,
        ),
        "callout": ParagraphStyle(
            "client_callout", parent=sample["BodyText"], fontName="ReportSans",
            fontSize=11, leading=17, textColor=base.NAVY,
        ),
    }


def p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def rich(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def section(text: str, s: dict[str, ParagraphStyle]) -> list:
    return [
        p(text, s["h1"]),
        HRFlowable(width="100%", thickness=1.1, color=base.TEAL, spaceAfter=8),
    ]


def parse_salary(value: str) -> tuple[float, float] | None:
    if "K" not in (value or "").upper():
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", value, re.I)
    if not match:
        return None
    low, high = map(float, match.groups())
    return low, high


def percentile(values: list[float], point: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    position = (len(ordered) - 1) * point
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def salary_stats(jobs: list[base.JobScore]) -> dict[str, float | int]:
    target = [job for job in jobs if job.bucket == "目标岗位"]
    valid = [(job, parse_salary(job.row.get("salary", ""))) for job in target]
    valid = [(job, salary) for job, salary in valid if salary]
    high_fit = [(job, salary) for job, salary in valid if job.fit_score >= 70]
    midpoints = [(salary[0] + salary[1]) / 2 for _, salary in high_fit]
    return {
        "all_jobs": len(jobs),
        "target_jobs": len(target),
        "salary_samples": len(valid),
        "high_fit_jobs": len([job for job in target if job.fit_score >= 70]),
        "p25": percentile(midpoints, 0.25),
        "median": percentile(midpoints, 0.5),
        "p75": percentile(midpoints, 0.75),
        "mean": statistics.mean(midpoints) if midpoints else 0,
    }


def metric_cards(stats: dict[str, float | int], s: dict[str, ParagraphStyle]) -> Table:
    data = [
        [
            rich("<b>岗位样本</b>", s["card"]),
            rich("<b>目标岗位</b>", s["card"]),
            rich("<b>高匹配岗位</b>", s["card"]),
            rich("<b>有效薪资样本</b>", s["card"]),
        ],
        [
            rich(f"<font size='20' color='{base.NAVY}'>{stats['all_jobs']}</font><br/>条", s["card"]),
            rich(f"<font size='20' color='{base.TEAL}'>{stats['target_jobs']}</font><br/>条", s["card"]),
            rich(f"<font size='20' color='{base.ORANGE}'>{stats['high_fit_jobs']}</font><br/>条", s["card"]),
            rich(f"<font size='20' color='{base.BLUE}'>{stats['salary_samples']}</font><br/>条", s["card"]),
        ],
    ]
    table = Table(data, colWidths=[43.5 * mm] * 4, rowHeights=[10 * mm, 19 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), base.LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def salary_band_chart() -> Drawing:
    width = 174 * mm
    height = 59 * mm
    drawing = Drawing(width, height)
    left = 35 * mm
    right = width - 7 * mm
    chart_width = right - left
    max_salary = 35
    rows = [
        ("稳妥接受", 15, 22, base.BLUE),
        ("主投谈薪", 18, 25, base.TEAL),
        ("冲刺区间", 25, 30, base.ORANGE),
    ]
    for tick in range(0, 36, 5):
        x = left + chart_width * tick / max_salary
        drawing.add(Line(x, 8 * mm, x, height - 7 * mm, strokeColor=colors.HexColor("#E4E9ED"), strokeWidth=0.5))
        drawing.add(String(x - 2 * mm, 3 * mm, str(tick), fontName="ReportSans", fontSize=7, fillColor=base.MUTED))
    for index, (label, low, high, color) in enumerate(rows):
        y = height - (15 + index * 14) * mm
        drawing.add(String(0, y + 1.5 * mm, label, fontName="ReportSans", fontSize=9, fillColor=base.INK))
        drawing.add(Rect(left, y, chart_width, 7 * mm, fillColor=base.LIGHT, strokeColor=None))
        x = left + chart_width * low / max_salary
        bar_width = chart_width * (high - low) / max_salary
        drawing.add(Rect(x, y, bar_width, 7 * mm, fillColor=color, strokeColor=None))
        drawing.add(String(x + 2 * mm, y + 2 * mm, f"{low}-{high}K", fontName="ReportSans", fontSize=8, fillColor=colors.white))
    drawing.add(String(right - 18 * mm, 3 * mm, "税前月薪 / RMB", fontName="ReportSans", fontSize=7, fillColor=base.MUTED))
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
        drawing.add(String(0, y + 1.8 * mm, label, fontName="ReportSans", fontSize=9, fillColor=base.INK))
        drawing.add(Rect(label_width, y, chart_width, 7 * mm, fillColor=base.LIGHT, strokeColor=None))
        drawing.add(Rect(label_width, y, chart_width * min(value, maximum) / maximum, 7 * mm, fillColor=color, strokeColor=None))
        drawing.add(String(label_width + chart_width + 3 * mm, y + 1.8 * mm, display, fontName="ReportSans", fontSize=8, fillColor=base.MUTED))
    return drawing


def signal_chart(jobs: list[base.JobScore]) -> Drawing:
    target = [job for job in jobs if job.bucket == "目标岗位"]
    patterns = [
        ("续约 / 续费 / 增购", [r"续约", r"续费", r"增购", r"续签"], base.TEAL),
        ("SaaS / 软件 / 云", [r"SaaS", r"软件", r"云", r"系统"], base.BLUE),
        ("实施 / 交付 / 上线", [r"实施", r"交付", r"上线"], base.ORANGE),
        ("数据 / 健康度", [r"数据", r"健康度", r"活跃度", r"指标"], base.GREEN),
        ("大客户 / 高层关系", [r"大客户", r"重点客户", r"高管", r"决策层"], base.RED),
    ]
    items = []
    for label, terms, color in patterns:
        count = sum(base.has_any(job.detail, terms) for job in target)
        ratio = count / len(target) * 100 if target else 0
        items.append((label, ratio, f"{ratio:.0f}%", color))
    return horizontal_chart(items, 100)


def capability_chart() -> Drawing:
    items = [
        ("复杂 B2B 客户经营", 92, "核心优势", base.TEAL),
        ("交付与跨部门推进", 90, "核心优势", base.BLUE),
        ("云 / ICT 价值沟通", 88, "核心优势", base.GREEN),
        ("续约风险管理", 82, "较强", base.ORANGE),
        ("高层客户关系", 82, "较强", base.RED),
        ("SaaS 指标体系表达", 62, "需补强", colors.HexColor("#9BA8B1")),
    ]
    return horizontal_chart(items, 100)


def role_salary_chart() -> Drawing:
    items = [
        ("云 / ICT 客户成功", 24, "18-30K", base.BLUE),
        ("SaaS 客户成功", 20, "15-25K", base.TEAL),
        ("续约 / 增购管理", 17, "12-22K", base.ORANGE),
        ("实施交付型客户成功", 16, "12-20K", base.GREEN),
    ]
    return horizontal_chart(items, 30, height_per_row=15)


def callout(title: str, body: str, s: dict[str, ParagraphStyle], background=base.PALE_BLUE) -> Table:
    table = Table([
        [rich(f"<b>{escape(title)}</b>", s["h2"])],
        [p(body, s["callout"])],
    ], colWidths=[174 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def build_story(jobs: list[base.JobScore], s: dict[str, ParagraphStyle]) -> list:
    stats = salary_stats(jobs)
    story: list = [
        Spacer(1, 18 * mm),
        rich("<font color='#2C8A84'>客户版岗位评估 / 2026</font>", s["body"]),
        Spacer(1, 3 * mm),
        p("Tingting You", s["title"]),
        p("客户成功经理 / 续约经理", s["h1"]),
        Spacer(1, 4 * mm),
        p("基于候选人简历和岗位样本的匿名聚合评估。报告仅展示薪资区间、岗位趋势与能力画像，不披露具体公司或岗位信息。", s["body"]),
        Spacer(1, 7 * mm),
        callout(
            "建议薪资预期",
            "中国大陆岗位口径下，建议将税前月薪 18-25K 作为主要谈薪区间；15-22K 属于较稳妥的可接受范围；具备成熟 SaaS、云或大型客户场景的高级岗位可尝试 25-30K。",
            s,
            base.PALE_GREEN,
        ),
        Spacer(1, 7 * mm),
        salary_band_chart(),
        Spacer(1, 7 * mm),
        metric_cards(stats, s),
        Spacer(1, 6 * mm),
        p("口径说明：薪资为人民币税前月薪，不含提成、奖金、股票和福利。报告基于当前岗位样本及简历证据估算，不代表招聘方承诺。", s["small"]),
        PageBreak(),
    ]

    story.extend(section("一、市场薪资概览", s))
    story.extend([
        p("高匹配岗位的薪资中位值约为 16K/月，上四分位约为 21K/月。候选人具备 100+ 企业实体管理、96% 客户留存、云网络升级和续约中断风险处理经验，因此建议谈薪定位略高于样本中位水平。", s["body"]),
        Spacer(1, 4 * mm),
        Table([
            [
                rich(f"<font size='20' color='{base.BLUE}'>{stats['p25']:.1f}K</font><br/>高匹配样本 P25", s["card"]),
                rich(f"<font size='20' color='{base.TEAL}'>{stats['median']:.1f}K</font><br/>高匹配样本中位", s["card"]),
                rich(f"<font size='20' color='{base.ORANGE}'>{stats['p75']:.1f}K</font><br/>高匹配样本 P75", s["card"]),
            ],
        ], colWidths=[58 * mm] * 3, rowHeights=[26 * mm], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), base.LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ])),
        Spacer(1, 7 * mm),
        p("目标岗位常见要求", s["h2"]),
        signal_chart(jobs),
        Spacer(1, 5 * mm),
        callout(
            "薪资判断",
            "如果岗位以售后维护或中小客户续费为主，薪资通常落在 10-18K；如果需要独立管理复杂 B2B 客户、交付、续约和增购，通常可争取 18-25K；高级 SaaS 或云客户成功岗位可能达到 25K 以上。",
            s,
            base.PALE_ORANGE,
        ),
        PageBreak(),
    ])

    story.extend(section("二、候选人能力画像", s))
    story.extend([
        p("候选人的优势不是普通客服，而是复杂企业客户经营、技术方案协调和续约风险控制。以下图表为基于简历证据的相对能力画像，仅用于确定求职定位。", s["body"]),
        Spacer(1, 4 * mm),
        capability_chart(),
        Spacer(1, 7 * mm),
        Table([
            [rich("<b>可直接用于谈薪的证据</b>", s["h2"]), rich("<b>目前影响薪资上限的缺口</b>", s["h2"])],
            [
                p("96% 客户留存率；管理 100+ 企业实体；推动云网络升级并降低 25% 成本；将发票周期缩短 45%；高层客户满意度 98%。", s["body"]),
                p("缺少明确 ARR、NDR、续约金额和增购金额；缺少标准化客户健康度、QBR 和 Renewal Forecast 表述；2023 年后的经历需要补充说明。", s["body"]),
            ],
        ], colWidths=[87 * mm, 87 * mm], style=TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), base.PALE_GREEN),
            ("BACKGROUND", (1, 0), (1, -1), base.PALE_ORANGE),
            ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ])),
        Spacer(1, 7 * mm),
        callout(
            "定位建议",
            "对外定位建议使用“复杂 B2B 客户成功 / 续约经理”，强调云与 ICT、客户留存、续约风险管理和高层客户关系，避免把自己描述成普通客服或泛客户经理。",
            s,
        ),
        PageBreak(),
    ])

    story.extend(section("三、岗位方向与投递策略", s))
    story.extend([
        p("不同岗位方向的市场薪资存在差异。以下区间为样本与岗位职责的综合估计，用于选择投递重点，不对应任何具体公司。", s["body"]),
        Spacer(1, 4 * mm),
        role_salary_chart(),
        Spacer(1, 7 * mm),
        Table([
            [rich("<b>优先级</b>", s["h2"]), rich("<b>岗位方向</b>", s["h2"]), rich("<b>建议</b>", s["h2"])],
            [p("第一优先", s["body"]), p("SaaS / 云 / 企业软件客户成功", s["body"]), p("主投 18-25K，突出客户留存、复杂交付和技术价值沟通。", s["body"])],
            [p("第二优先", s["body"]), p("续约 / 增购 / 大客户经营", s["body"]), p("主投 15-22K，突出续约风险干预、合同流程和高层关系。", s["body"])],
            [p("扩展方向", s["body"]), p("实施交付型客户成功", s["body"]), p("可投 12-20K，但需确认岗位是否承担续约和商业结果。", s["body"])],
        ], colWidths=[28 * mm, 64 * mm, 82 * mm], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), base.NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, base.LIGHT]),
            ("BOX", (0, 0), (-1, -1), 0.6, base.GRID),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, base.GRID),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])),
    ])
    return story


def main() -> None:
    base.register_fonts()
    jobs = base.load_jobs()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report_styles = styles()
    document = base.AssessmentDocTemplate(str(OUTPUT_PATH), report_styles)
    document.build(build_story(jobs, report_styles))
    stats = salary_stats(jobs)
    print(f"output={OUTPUT_PATH}")
    print(
        f"pages_target=4 jobs={stats['all_jobs']} target={stats['target_jobs']} "
        f"salary_samples={stats['salary_samples']} p25={stats['p25']:.1f} "
        f"median={stats['median']:.1f} p75={stats['p75']:.1f}"
    )


if __name__ == "__main__":
    main()
