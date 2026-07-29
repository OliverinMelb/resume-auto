from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
RESUME_PATH = ROOT / "data" / "Tingting YouResume20260727.pdf"
JOBS_PATH = ROOT / "csv_export" / "jobs.csv"
OUTPUT_PATH = ROOT / "output" / "pdf" / "tingting_you_job_assessment_20260729.pdf"

NAVY = colors.HexColor("#19324A")
BLUE = colors.HexColor("#2F6F8F")
TEAL = colors.HexColor("#2C8A84")
ORANGE = colors.HexColor("#D77A32")
RED = colors.HexColor("#B55243")
GREEN = colors.HexColor("#2D8060")
INK = colors.HexColor("#24313A")
MUTED = colors.HexColor("#697783")
LIGHT = colors.HexColor("#F2F5F7")
PALE_BLUE = colors.HexColor("#EAF2F6")
PALE_GREEN = colors.HexColor("#E8F3EE")
PALE_ORANGE = colors.HexColor("#FBF0E6")
PALE_RED = colors.HexColor("#F8EAE7")
GRID = colors.HexColor("#D5DEE4")


def register_fonts() -> None:
    candidates = [
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
        Path(r"C:\Windows\Fonts\Deng.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                pdfmetrics.registerFont(TTFont("ReportSans", str(path)))
                return
            except Exception:
                continue
    raise RuntimeError("No usable Chinese font found.")


def clean_company(value: str) -> str:
    return re.sub(r"^\s*公司名称\s*", "", value or "").strip()


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def display_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\ufffd", " ").replace("□", "-").replace("☐", "-")
    value = value.replace("•", "-").replace("▪", "-").replace("●", "-")
    return normalize(value)


def has_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def extract_resume_text() -> str:
    reader = PdfReader(str(RESUME_PATH))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


@dataclass
class JobScore:
    row: dict[str, str]
    company: str
    title: str
    detail: str
    bucket: str
    fit_score: int
    practical_score: int
    location: str
    feasibility: str
    match_reasons: list[str]
    gaps: list[str]
    source_id: int


def infer_location(title: str, detail: str, company: str) -> tuple[str, str, int]:
    title_text = title
    detail_text = detail
    text = f"{title_text} {detail_text} {company}"
    if has_any(text, [r"居家办公", r"远程", r"线上客户", r"不坐班"]):
        return "远程/线上", "中", 2
    cities = [
        "上海", "杭州", "深圳", "广州", "天津", "武汉", "大连", "合肥",
        "南京", "苏州", "北京", "重庆", "沈阳", "海南", "贵州",
    ]
    found = next((city for city in cities if city in title_text), None)
    if not found:
        location_hint = re.search(
            r"(?:工作地点|工作城市|办公地点|岗位地点|驻地|base)\s*[:：]?\s*"
            r"([^。；,，\n]{0,30})",
            detail_text,
            re.I,
        )
        if location_hint:
            found = next((city for city in cities if city in location_hint.group(1)), None)
    if found:
        return found, "高", 8
    return "地点未明确", "待确认", 0


def role_bucket(title: str, detail: str) -> str:
    text = f"{title} {detail}"
    if has_any(title, [r"亚马逊运营", r"续约销售"]):
        return "邻近岗位"
    hard_negative = has_any(
        title,
        [
            r"资产", r"门店", r"物业", r"租赁客户经理", r"车抵贷", r"车险",
            r"保险", r"证券", r"康养", r"留学移民", r"律师顾问", r"兼职",
            r"直播", r"广告客户经理", r"融资租赁", r"保函",
        ],
    )
    if has_any(text, [r"直播", r"出镜"]) and has_any(text, [r"获客", r"销售", r"留资", r"签单"]):
        hard_negative = True
    if hard_negative:
        return "非目标岗位"
    if has_any(title, [r"客户成功", r"续约", r"续费"]):
        return "目标岗位"
    if has_any(text, [r"全生命周期", r"客户留存", r"客户续费", r"客户增购"]):
        return "邻近岗位"
    if has_any(title, [r"客户经理", r"大客户", r"KA"]):
        return "邻近岗位"
    return "非目标岗位"


def score_job(row: dict[str, str], source_id: int) -> JobScore:
    title = normalize(row.get("title", ""))
    detail = normalize(row.get("detail", ""))
    company = clean_company(row.get("company", ""))
    text = f"{title} {detail}"
    bucket = role_bucket(title, detail)
    location, feasibility, location_penalty = infer_location(title, detail, company)

    role = 20 if bucket == "目标岗位" else 12 if bucket == "邻近岗位" else 3
    lifecycle = 0
    if has_any(text, [r"全生命周期", r"客户生命周期"]):
        lifecycle += 8
    elif has_any(text, [r"老客户", r"存量客户", r"客户维护"]):
        lifecycle += 5
    if has_any(text, [r"客户成功", r"客户健康", r"客户满意度", r"客户留存"]):
        lifecycle += 4
    lifecycle = min(lifecycle, 20)

    commercial = 0
    if has_any(text, [r"续约", r"续费", r"续签", r"renewal"]):
        commercial += 9
    if has_any(text, [r"增购", r"增值", r"交叉销售", r"转介绍"]):
        commercial += 6
    commercial = min(commercial, 15)

    technology = 0
    if has_any(text, [r"SaaS", r"软件", r"云", r"AI", r"数字化", r"企业服务"]):
        technology += 8
    if has_any(text, [r"CRM", r"ERP", r"OA", r"系统", r"平台"]):
        technology += 4
    if has_any(text, [r"ToB", r"企业客户", r"大客户"]):
        technology += 3
    technology = min(technology, 15)

    delivery = 0
    if has_any(text, [r"实施", r"交付", r"上线", r"项目管理", r"项目推进"]):
        delivery += 4
    if has_any(text, [r"跨部门", r"协调", r"联动", r"资源", r"问题闭环"]):
        delivery += 3
    if has_any(text, [r"培训", r"方案", r"需求", r"咨询"]):
        delivery += 3
    delivery = min(delivery, 10)

    data = 0
    if has_any(text, [r"数据", r"指标", r"健康度", r"活跃度", r"分析", r"报表"]):
        data += 6
    if has_any(text, [r"复盘", r"看板", r"NPS", r"留存率", r"满意度"]):
        data += 4
    data = min(data, 10)

    base = 0
    if has_any(text, [r"本科", r"全日制统招本科"]):
        base += 5
    if has_any(text, [r"3年以上", r"3 年以上", r"3-5年", r"2-4年", r"2年以上"]):
        base += 3
    if has_any(text, [r"英语", r"英文"]):
        base += 2
    base = min(base, 10)

    fit_score = role + lifecycle + commercial + technology + delivery + data + base
    gaps: list[str] = []
    reasons: list[str] = []

    if role >= 20:
        reasons.append("岗位名称直接对应客户成功或续约方向")
    if lifecycle >= 12:
        reasons.append("职责覆盖客户生命周期、留存或健康度管理")
    if commercial >= 9:
        reasons.append("明确包含续约、续费或增购结果")
    if technology >= 10:
        reasons.append("涉及 SaaS、软件、云或企业数字化场景")
    if delivery >= 7:
        reasons.append("需要项目交付、培训或跨部门问题闭环")
    if has_any(text, [r"Amazon", r"亚马逊", r"餐饮", r"人力资源", r"旅游", r"租赁系统", r"电商"]):
        gaps.append("存在较强的垂直行业经验要求")
    if has_any(text, [r"直播", r"陌拜", r"纯销售", r"高提成", r"自带客户资源"]):
        gaps.append("销售或拓客属性较重，需确认是否偏离客户成功")
    if has_any(text, [r"必须", r"硬性要求"]):
        gaps.append("JD 含硬性条件，需在面试前确认满足情况")
    if not has_any(text, [r"续约", r"续费", r"续签"]):
        gaps.append("JD 未明确写出续约结果，需确认商业目标")

    domain_penalty = 0
    if has_any(text, [r"Amazon", r"亚马逊", r"餐饮", r"人力资源", r"旅游", r"电商 SaaS"]):
        domain_penalty = 5
    sales_penalty = 0
    if bucket == "邻近岗位" and has_any(text, [r"开发", r"陌拜", r"高提成", r"拓客"]):
        sales_penalty = 5
    if bucket == "非目标岗位":
        fit_score = min(fit_score, 35)
        sales_penalty = max(sales_penalty, 10)
    practical_score = max(0, fit_score - location_penalty - domain_penalty - sales_penalty)

    return JobScore(
        row=row,
        company=company,
        title=title,
        detail=detail,
        bucket=bucket,
        fit_score=fit_score,
        practical_score=practical_score,
        location=location,
        feasibility=feasibility,
        match_reasons=reasons[:4],
        gaps=gaps[:3],
        source_id=source_id,
    )


def load_jobs() -> list[JobScore]:
    with JOBS_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [score_job(row, index) for index, row in enumerate(rows, start=1)]


def salary_low(value: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", value or "")
    return float(match.group(1)) if match else 0.0


def p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def rich(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


class AssessmentDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str, styles: dict[str, ParagraphStyle]):
        super().__init__(
            filename,
            pagesize=A4,
            leftMargin=17 * mm,
            rightMargin=17 * mm,
            topMargin=17 * mm,
            bottomMargin=15 * mm,
            title="Tingting You 岗位评估报告",
            author="OpenAI Codex",
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="normal")
        self.addPageTemplates([PageTemplate(id="report", frames=frame, onPage=self._draw_page)])
        self.styles_map = styles

    def _draw_page(self, canvas, doc) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(GRID)
        canvas.setLineWidth(0.5)
        canvas.line(self.leftMargin, height - 11 * mm, width - self.rightMargin, height - 11 * mm)
        canvas.setFont("ReportSans", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(self.leftMargin, height - 8.5 * mm, "Tingting You | 客户成功经理 / 续约经理岗位评估")
        canvas.drawRightString(width - self.rightMargin, 8 * mm, f"{doc.page}")
        canvas.drawString(self.leftMargin, 8 * mm, "数据来源：候选人简历 PDF 与岗位 CSV，生成日期：2026-07-29")
        canvas.restoreState()


def build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "body": ParagraphStyle(
            "body", parent=base["BodyText"], fontName="ReportSans", fontSize=9.2,
            leading=14, textColor=INK, spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "small", parent=base["BodyText"], fontName="ReportSans", fontSize=7.8,
            leading=11, textColor=INK,
        ),
        "tiny": ParagraphStyle(
            "tiny", parent=base["BodyText"], fontName="ReportSans", fontSize=6.8,
            leading=9, textColor=MUTED,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontName="ReportSans", fontSize=23,
            leading=31, textColor=NAVY, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName="ReportSans", fontSize=15,
            leading=21, textColor=NAVY, spaceBefore=8, spaceAfter=7,
        ),
        "h3": ParagraphStyle(
            "h3", parent=base["Heading3"], fontName="ReportSans", fontSize=11,
            leading=16, textColor=BLUE, spaceBefore=6, spaceAfter=4,
        ),
        "cover": ParagraphStyle(
            "cover", parent=base["BodyText"], fontName="ReportSans", fontSize=11,
            leading=18, textColor=MUTED, alignment=TA_LEFT,
        ),
        "cover_title": ParagraphStyle(
            "cover_title", parent=base["Heading1"], fontName="ReportSans", fontSize=28,
            leading=38, textColor=NAVY, spaceAfter=6,
        ),
        "callout": ParagraphStyle(
            "callout", parent=base["BodyText"], fontName="ReportSans", fontSize=10,
            leading=16, textColor=NAVY,
        ),
        "table": ParagraphStyle(
            "table", parent=base["BodyText"], fontName="ReportSans", fontSize=7.5,
            leading=10, textColor=INK,
        ),
        "table_header": ParagraphStyle(
            "table_header", parent=base["BodyText"], fontName="ReportSans", fontSize=7.4,
            leading=10, textColor=colors.white,
        ),
        "score": ParagraphStyle(
            "score", parent=base["BodyText"], fontName="ReportSans", fontSize=15,
            leading=18, textColor=NAVY, alignment=TA_CENTER,
        ),
        "center": ParagraphStyle(
            "center", parent=base["BodyText"], fontName="ReportSans", fontSize=8,
            leading=11, textColor=INK, alignment=TA_CENTER,
        ),
    }


def section_title(text: str, styles: dict[str, ParagraphStyle]) -> list:
    return [Paragraph(escape(text), styles["h2"]), HRFlowable(width="100%", thickness=1, color=TEAL, spaceAfter=8)]


def bullet(text: str, styles: dict[str, ParagraphStyle]) -> Paragraph:
    return rich(f"<font color='{TEAL}'>-</font> {escape(text)}", styles["body"])


def info_box(title: str, body: str, styles: dict[str, ParagraphStyle], background=PALE_BLUE) -> Table:
    content = [
        [rich(f"<b>{escape(title)}</b>", styles["h3"])],
        [p(body, styles["callout"])],
    ]
    table = Table(content, colWidths=[174 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def metric_cards(jobs: list[JobScore], styles: dict[str, ParagraphStyle]) -> Table:
    target = [job for job in jobs if job.bucket == "目标岗位"]
    exact = sorted(target, key=lambda job: job.practical_score, reverse=True)
    high = len([job for job in jobs if job.practical_score >= 60])
    data = [
        [rich("<b>岗位总量</b>", styles["table"]), rich("<b>目标岗位</b>", styles["table"]),
         rich("<b>高匹配岗位</b>", styles["table"]), rich("<b>薪资样本</b>", styles["table"])],
        [rich(f"<font size='16' color='{NAVY}'>{len(jobs)}</font><br/>条", styles["center"]),
         rich(f"<font size='16' color='{TEAL}'>{len(target)}</font><br/>条", styles["center"]),
         rich(f"<font size='16' color='{ORANGE}'>{high}</font><br/>条", styles["center"]),
         rich(f"<font size='16' color='{BLUE}'>{len([j for j in jobs if salary_low(j.row.get('salary', '')) > 0])}</font><br/>条", styles["center"])],
    ]
    table = Table(data, colWidths=[43.5 * mm] * 4, rowHeights=[10 * mm, 18 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def fit_matrix(styles: dict[str, ParagraphStyle]) -> Table:
    rows = [
        ["能力维度", "简历证据", "岗位价值"],
        ["客户生命周期与留存", "管理 100+ 子公司企业组合，季度客户留存率 96%。", "直接支撑健康度、流失风险和续约经营。"],
        ["续约与商业结果", "优化续约流程、发票周期和付款延展机制，保障年度续约与业务连续性。", "适合续费、续约风险干预和增购协同。"],
        ["ToB 云与 ICT 复杂场景", "云网络升级、MSTP 迁移、视频监控平台、百万美元级 ICT 项目。", "可快速理解复杂企业产品和技术价值。"],
        ["交付与跨部门推进", "协同售前、技术团队和第三方供应商，完成 PoC、验收与零宕机交付。", "适合实施交付型客户成功和复杂问题闭环。"],
        ["高层客户关系", "组织 7+ 次高层代表团访问和高管峰会，客户满意度 98%。", "适合大客户、关键决策人和 Executive Sponsorship。"],
        ["语言与跨文化沟通", "中文母语、日语商务流利、英语专业工作能力。", "适合国际化、跨区域和多方沟通场景。"],
    ]
    converted = []
    for row_index, row in enumerate(rows):
        if row_index == 0:
            converted.append([rich(f"<b>{escape(cell)}</b>", styles["table_header"]) for cell in row])
        else:
            converted.append([p(cell, styles["table"]) for cell in row])
    table = Table(converted, colWidths=[38 * mm, 70 * mm, 66 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
    ]))
    return table


def shortlist_table(shortlist: list[JobScore], styles: dict[str, ParagraphStyle]) -> Table:
    rows = [[
        rich("<b>优先级</b>", styles["table_header"]),
        rich("<b>岗位 / 公司</b>", styles["table_header"]),
        rich("<b>薪资</b>", styles["table_header"]),
        rich("<b>能力分</b>", styles["table_header"]),
        rich("<b>可行分</b>", styles["table_header"]),
        rich("<b>地点风险</b>", styles["table_header"]),
    ]]
    for index, job in enumerate(shortlist, start=1):
        label = "优先投递" if index <= 4 else "可尝试" if index <= 8 else "谨慎投递"
        rows.append([
            p(f"{index}  {label}", styles["table"]),
            p(f"{job.title}\n{job.company}", styles["table"]),
            p(job.row.get("salary", "未披露"), styles["table"]),
            p(f"{job.fit_score}/100", styles["table"]),
            p(f"{job.practical_score}/100", styles["table"]),
            p(f"{job.location}\n{job.feasibility}", styles["table"]),
        ])
    table = Table(rows, colWidths=[25 * mm, 61 * mm, 22 * mm, 19 * mm, 19 * mm, 30 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
    ]))
    return table


def job_detail_block(job: JobScore, rank: int, styles: dict[str, ParagraphStyle]) -> list:
    recommendation = "优先投递" if rank <= 4 else "可尝试" if rank <= 8 else "谨慎投递"
    color = GREEN if rank <= 4 else ORANGE if rank <= 8 else RED
    clean_detail = display_text(job.detail)
    if len(clean_detail) > 650:
        clean_detail = clean_detail[:650].rsplit("。", 1)[0] + "。"
    source = job.row.get("external_job_id", "")
    source_link = f"<link href='{escape(source)}' color='{BLUE}'>查看原始 JD</link>" if source else "原始链接未提供"
    summary = "；".join(job.match_reasons) if job.match_reasons else "岗位职责与候选人经历的直接交集有限"
    gap = "；".join(job.gaps) if job.gaps else "暂无明显硬性差距，仍需面试确认具体产品与客户类型"
    return [
        KeepTogether([
            rich(f"<font color='{color}'><b>{rank}. {escape(recommendation)}</b></font>　<b>{escape(job.title)}</b>", styles["h3"]),
            p(f"{job.company} | 薪资 {job.row.get('salary', '未披露')} | 地点 {job.location} | 能力分 {job.fit_score}/100 | 可行分 {job.practical_score}/100", styles["small"]),
            Table([
                [p("匹配判断", styles["table"]), p(summary, styles["table"])],
                [p("主要差距", styles["table"]), p(gap, styles["table"])],
                [p("JD 证据摘录", styles["table"]), p(clean_detail, styles["table"])],
                [p("来源", styles["table"]), rich(source_link, styles["table"])],
            ], colWidths=[26 * mm, 148 * mm], style=TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.5, GRID),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ])),
        ]),
        Spacer(1, 6),
    ]


def build_story(jobs: list[JobScore], resume_text: str, styles: dict[str, ParagraphStyle]) -> list:
    target_jobs = [job for job in jobs if job.bucket == "目标岗位"]
    shortlist = sorted(target_jobs, key=lambda job: (job.practical_score, job.fit_score), reverse=True)[:12]
    high_fit = sorted(jobs, key=lambda job: job.practical_score, reverse=True)
    exact_count = len(target_jobs)
    avg_fit = round(sum(job.fit_score for job in target_jobs) / exact_count) if exact_count else 0
    salary_values = [
        salary_low(job.row.get("salary", ""))
        for job in target_jobs
        if salary_low(job.row.get("salary", "")) and "K" in job.row.get("salary", "").upper()
    ]
    salary_range = f"{min(salary_values):g}-{max(salary_values):g}K" if salary_values else "未形成有效区间"

    story: list = []
    story.extend([
        Spacer(1, 24 * mm),
        rich("<font color='#2C8A84'>岗位评估报告 / 2026</font>", styles["cover"]),
        Spacer(1, 5 * mm),
        rich("Tingting You", styles["cover_title"]),
        rich("客户成功经理 / 续约经理", styles["h1"]),
        Spacer(1, 7 * mm),
        p("基于候选人英文简历与 144 条岗位信息的综合求职评估。报告面向候选人，重点回答：哪些岗位值得优先投递、哪些能力可以直接迁移、哪些风险需要在简历或面试中先补齐。", styles["cover"]),
        Spacer(1, 10 * mm),
        metric_cards(jobs, styles),
        Spacer(1, 10 * mm),
        info_box("结论先行", "候选人最适合以“复杂 B2B 客户成功 + 云/ICT 解决方案 + 续约风险管理”作为求职定位。能力上优先匹配 SaaS 客户成功、企业软件客户成功、续约增购和大客户生命周期管理岗位；实际投递前必须确认中国大陆工作地点、工作许可和到岗安排。", styles, PALE_GREEN),
        Spacer(1, 8 * mm),
        p("报告生成日期：2026-07-29 | 简历来源：Tingting YouResume20260727.pdf | 岗位来源：csv_export/jobs.csv", styles["tiny"]),
        PageBreak(),
    ])

    story.extend(section_title("一、执行摘要", styles))
    story.extend([
        p(f"本次采集包含 {len(jobs)} 条岗位，其中规则识别为目标岗位 {exact_count} 条。目标岗位的平均能力匹配分为 {avg_fit}/100，岗位薪资字段中可解析的下限区间约为 {salary_range}。由于简历显示候选人目前位于 Richmond, BC，而岗位主要来自中国大陆城市，报告将能力匹配与实际求职可行性分开展示。", styles["body"]),
        info_box("推荐求职主线", "第一主线：SaaS / 企业软件客户成功，强调客户生命周期、健康度、续约和增购。第二主线：云计算、ICT、数字化解决方案的大客户成功或续约管理。第三主线：实施交付型客户成功，但需在面试中确认商业目标和客户成功团队的职责边界。", styles),
        Spacer(1, 5 * mm),
        p("建议优先投递的岗位应同时具备三个条件：JD 明确写出续约/留存/增购结果；需要复杂 B 端客户和跨部门推进；没有候选人明显不具备的垂直行业硬门槛。", styles["body"]),
        Spacer(1, 3 * mm),
        p("最需要补强的不是基础客户服务能力，而是把已有经历翻译成 SaaS 客户成功语言：续约金额或 ARR、NDR/GRR、客户健康度指标、产品使用率、增购金额、客户分层和可复制的 CSM 流程。", styles["body"]),
        PageBreak(),
    ])

    story.extend(section_title("二、候选人画像与岗位迁移能力", styles))
    story.extend([
        p("候选人的核心优势集中在复杂企业客户经营，而不是纯客服或纯陌生销售。China Unicom Shenzhen 经历中，候选人曾负责大型集团及 100+ 子公司组合，维护 96% 客户留存率；同时具备云网络迁移、PoC 协调、百万美元级视频监控与活动转播平台交付、发票周期优化和付款风险干预经验。", styles["body"]),
        fit_matrix(styles),
        Spacer(1, 6 * mm),
        p("简历中的关键事实证据", styles["h3"]),
        bullet("96% quarterly account retention rate，直接对应客户留存和续约能力。", styles),
        bullet("100+ corporate subsidiaries，证明多实体、复杂关键客户组合管理经验。", styles),
        bullet("25% deployment and operational cost reduction，证明能把技术方案翻译成客户业务价值。", styles),
        bullet("45% invoice cycle reduction 和 payment extension whitelist，证明能处理续约中断和回款流程风险。", styles),
        bullet("98% client satisfaction、7+ executive visits，证明具备高层关系和 Executive Sponsorship 能力。", styles),
        PageBreak(),
    ])

    story.extend(section_title("三、岗位数据与评分方法", styles))
    score_rows = [
        ["评分维度", "权重", "判定依据"],
        ["目标岗位相关性", "20", "客户成功、续约、续费、客户生命周期等直接信号"],
        ["客户生命周期经验", "20", "留存、客户健康度、客户维护、风险预警"],
        ["续约与商业结果", "15", "续约、续费、增购、交叉销售、转介绍"],
        ["ToB / SaaS / 云技术", "15", "SaaS、软件、CRM、ERP、云、企业数字化"],
        ["交付与跨部门推进", "10", "实施、上线、培训、项目管理、问题闭环"],
        ["数据与运营", "10", "使用数据、指标、报表、复盘、NPS、健康度"],
        ["基本要求", "10", "本科、经验年限、英语或其他 JD 条件"],
        ["可行性修正", "单列", "地点、远程方式、垂直行业门槛和销售属性"],
    ]
    score_table_data = []
    for row_index, row in enumerate(score_rows):
        if row_index == 0:
            score_table_data.append([rich(f"<b>{escape(cell)}</b>", styles["table_header"]) for cell in row])
        else:
            score_table_data.append([p(cell, styles["table"]) for cell in row])
    story.extend([
        metric_cards(jobs, styles),
        Spacer(1, 5 * mm),
        p("岗位筛选先按标题和 JD 正文识别目标相关性，再按候选人可迁移能力评分。资产管理、门店租约、金融获客、保险、直播销售和纯陌拜销售等岗位，即使含有“续约”或“客户经理”字样，也不会被当作客户成功岗位。", styles["body"]),
        Table(score_table_data, colWidths=[42 * mm, 18 * mm, 114 * mm], repeatRows=1, style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("BOX", (0, 0), (-1, -1), 0.6, GRID),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])),
        Spacer(1, 7 * mm),
        info_box("解读方式", "能力分回答“候选人能否胜任岗位核心工作”；可行分在能力分基础上扣除地点、垂直行业和纯销售属性风险。CSV 没有独立地点字段，因此地点只依据岗位标题、公司名称和 JD 中明确出现的城市或工作方式判断。", styles, PALE_ORANGE),
        PageBreak(),
    ])

    story.extend(section_title("四、重点岗位 shortlist", styles))
    story.append(p("下表按综合可行分排序。排名靠前并不等于一定录用，而是代表在当前简历证据下最值得投入时间准备和投递。", styles["body"]))
    story.append(shortlist_table(shortlist, styles))
    story.append(Spacer(1, 5 * mm))
    story.append(info_box("投递顺序建议", "先投递前 4 个岗位并针对 JD 改写摘要和项目 bullet；随后投递第 5-8 个岗位作为扩展池；第 9-12 个岗位只有在地点、行业或销售属性确认可接受后再投。", styles, PALE_GREEN))
    story.append(PageBreak())

    story.extend(section_title("五、重点岗位逐岗评估", styles))
    for rank, job in enumerate(shortlist, start=1):
        story.extend(job_detail_block(job, rank, styles))
        if rank in {4, 8}:
            story.append(PageBreak())

    story.extend(section_title("六、简历与面试行动建议", styles))
    story.extend([
        rich("简历建议", styles["h3"]),
        bullet("把 Summary 从“Customer Success and Key Account Specialist”进一步改成“B2B Customer Success / Renewal Manager”，第一句直接写 96% GRR、100+ entities 和 cloud/ICT。", styles),
        bullet("补充可量化商业指标：年度续约金额、续约合同数量、增购金额、客户组合 ARR、平均合同周期、流失风险案例。没有数据时不要编造，可在面试前从历史记录中核实。", styles),
        bullet("将 Renewal Optimization 段落拆成“风险识别 - 跨部门动作 - 结果”，并修复 `intervention:Fast-tracked`、`cycle.Implemented` 等句间空格和跨页断句。", styles),
        bullet("增加 CSM 方法论关键词：客户分层、健康度评分、QBR、Success Plan、Onboarding、Adoption、GRR/NDR、Renewal Forecast。只有确有实际经验的词才加入。", styles),
        bullet("补充最近一段经历或解释 2023 年 6 月之后的时间线，降低招聘方对履历连续性的疑问。", styles),
        Spacer(1, 3 * mm),
        rich("面试中必须验证的问题", styles["h3"]),
        bullet("这个岗位的客户成功是否对续约率、GRR、NDR 或增购收入直接负责，还是主要负责售后和实施？", styles),
        bullet("客户组合规模、客单价、合同周期、续约窗口和客户健康度体系如何定义？", styles),
        bullet("岗位的地点、到岗、出差、远程和中国大陆工作许可要求是什么？", styles),
        bullet("客户成功与销售、实施、产品、技术支持之间的责任边界和绩效归属如何划分？", styles),
        Spacer(1, 5 * mm),
        info_box("最终定位", "候选人应避免把自己包装成普通客服或泛客户经理。最有竞争力的叙事是：能管理复杂 B2B 客户组合，理解云和 ICT 产品价值，能协调技术与商业团队，提前识别续约风险，并通过高层关系和流程优化守住客户收入。", styles, PALE_BLUE),
        PageBreak(),
    ])

    story.extend(section_title("附录：重点岗位来源", styles))
    source_rows = [[
        rich("<b>排名</b>", styles["table_header"]),
        rich("<b>岗位</b>", styles["table_header"]),
        rich("<b>公司</b>", styles["table_header"]),
        rich("<b>原始 JD</b>", styles["table_header"]),
    ]]
    for rank, job in enumerate(shortlist, start=1):
        url = job.row.get("external_job_id", "")
        link = f"<link href='{escape(url)}' color='{BLUE}'>打开岗位</link>" if url else "未提供"
        source_rows.append([p(str(rank), styles["table"]), p(job.title, styles["table"]), p(job.company, styles["table"]), rich(link, styles["table"])])
    story.append(Table(source_rows, colWidths=[15 * mm, 65 * mm, 55 * mm, 39 * mm], repeatRows=1, style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ])))
    story.extend([
        Spacer(1, 8 * mm),
        info_box("资料边界", "本报告只使用候选人提供的简历 PDF 和岗位 CSV。岗位是否仍在招聘、实际办公地点、工作许可、薪资结构和岗位真实性均需候选人在投递或面试阶段自行核实。", styles, PALE_ORANGE),
        Spacer(1, 8 * mm),
        p(f"简历文本提取状态：{'成功' if resume_text.strip() else '未提取到文本'}。岗位评分为规则分析结果，不是招聘方评分。", styles["tiny"]),
    ])
    return story


def main() -> None:
    register_fonts()
    resume_text = extract_resume_text()
    jobs = load_jobs()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    styles = build_styles()
    doc = AssessmentDocTemplate(str(OUTPUT_PATH), styles)
    doc.build(build_story(jobs, resume_text, styles))
    target = [job for job in jobs if job.bucket == "目标岗位"]
    shortlist = sorted(target, key=lambda job: (job.practical_score, job.fit_score), reverse=True)[:12]
    print(f"output={OUTPUT_PATH}")
    print(f"jobs={len(jobs)} target={len(target)} shortlist={len(shortlist)}")
    for rank, job in enumerate(shortlist, start=1):
        print(f"{rank:02d}|{job.practical_score}|{job.fit_score}|{job.title}|{job.company}|{job.location}")


if __name__ == "__main__":
    main()
