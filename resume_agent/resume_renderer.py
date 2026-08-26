"""ATS-friendly PDF rendering for validated resume Markdown."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape

import pymupdf
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer


FONT_PATHS = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
)
FONT_NAME = "ResumeCJK"
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEPARATOR = re.compile(r"^\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+$")
HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BULLET = re.compile(r"^[-*+]\s+(.+?)\s*$")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class RenderPreset:
    body_size: float
    leading: float
    section_space: float
    margin_mm: float


PRESETS = (
    RenderPreset(10.0, 13.0, 6.0, 14.0),
    RenderPreset(9.5, 12.0, 4.5, 12.0),
    RenderPreset(8.8, 11.0, 3.5, 11.0),
)


@dataclass(frozen=True)
class RenderResult:
    page_count: int
    extracted_text: str
    preview_paths: list[Path]


class RenderError(Exception):
    def __init__(self, code: str, safe_message: str):
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


class ResumeRenderer(Protocol):
    def render(self, markdown: str, pdf_path: Path, preview_dir: Path) -> RenderResult:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


class ReportLabAdapter:
    """Renders the small Markdown subset accepted by the resume rewrite flow."""

    def available(self) -> bool:
        return any(path.is_file() for path in FONT_PATHS)

    def render(self, markdown: str, pdf_path: Path, preview_dir: Path) -> RenderResult:
        self._validate_markdown(markdown)
        font_name = self._register_font()
        pdf_path = Path(pdf_path)
        preview_dir = Path(preview_dir)
        expected_lines = _normalized_markdown_lines(markdown)

        with tempfile.TemporaryDirectory(prefix="resume-render-") as directory:
            work_dir = Path(directory)
            selected: Path | None = None
            two_page_candidate: Path | None = None

            for index, preset in enumerate(PRESETS):
                candidate = work_dir / f"preset-{index}.pdf"
                self._build_pdf(markdown, candidate, preset, font_name)
                page_count = self._page_count(candidate)
                if page_count == 1:
                    selected = candidate
                    break
                if page_count == 2 and two_page_candidate is None:
                    two_page_candidate = candidate

            if selected is None:
                selected = two_page_candidate
            if selected is None:
                raise RenderError(
                    "PAGE_LIMIT_EXCEEDED",
                    "The resume cannot fit within two readable pages.",
                )

            extracted_text, preview_files = self._validate_and_preview(
                selected, work_dir / "preview", expected_lines
            )
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            preview_dir.mkdir(parents=True, exist_ok=True)
            final_previews: list[Path] = []
            for temporary_preview in preview_files:
                final_preview = preview_dir / temporary_preview.name
                os.replace(temporary_preview, final_preview)
                final_previews.append(final_preview)
            stale_preview = preview_dir / "page-2.png"
            if len(final_previews) == 1 and stale_preview.exists():
                stale_preview.unlink()
            os.replace(selected, pdf_path)
            return RenderResult(
                page_count=len(final_previews),
                extracted_text=extracted_text,
                preview_paths=final_previews,
            )

    def _register_font(self) -> str:
        if FONT_NAME in pdfmetrics.getRegisteredFontNames():
            return FONT_NAME
        for path in FONT_PATHS:
            if not path.is_file():
                continue
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
                return FONT_NAME
            except Exception:
                continue
        raise RenderError("FONT_UNAVAILABLE", "No supported Windows CJK font is available.")

    def _validate_markdown(self, markdown: str) -> None:
        if not isinstance(markdown, str) or not markdown.strip():
            raise RenderError("EMPTY_CONTENT", "Resume Markdown must not be empty.")
        for line in markdown.splitlines():
            if TABLE_ROW.match(line) or TABLE_SEPARATOR.match(line):
                raise RenderError("UNSUPPORTED_MARKDOWN", "Markdown tables are not supported.")
            if HTML_TAG.search(line):
                raise RenderError("UNSUPPORTED_MARKDOWN", "HTML is not supported.")
            if IMAGE.search(line):
                raise RenderError("UNSUPPORTED_MARKDOWN", "Images are not supported.")
        if not _normalized_markdown_lines(markdown):
            raise RenderError("EMPTY_CONTENT", "Resume Markdown must contain text.")

    def _build_pdf(
        self, markdown: str, pdf_path: Path, preset: RenderPreset, font_name: str
    ) -> None:
        margin = preset.margin_mm * mm
        document = SimpleDocTemplate(
            str(pdf_path),
            pagesize=A4,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=margin,
            bottomMargin=margin,
            title="Resume",
            author="Resume Agent",
        )
        story = self._flowables(markdown, preset, font_name, document.height)
        document.build(story)

    def _flowables(
        self,
        markdown: str,
        preset: RenderPreset,
        font_name: str,
        available_height: float,
    ) -> list[object]:
        body = ParagraphStyle(
            "ResumeBody",
            fontName=font_name,
            fontSize=preset.body_size,
            leading=preset.leading,
            alignment=TA_LEFT,
            spaceAfter=preset.section_space / 2,
            allowWidows=1,
            allowOrphans=1,
        )
        title = ParagraphStyle(
            "ResumeTitle",
            parent=body,
            fontSize=preset.body_size + 5.0,
            leading=preset.leading + 4.0,
            spaceBefore=0,
            spaceAfter=preset.section_space,
        )
        section = ParagraphStyle(
            "ResumeSection",
            parent=body,
            fontSize=preset.body_size + 1.6,
            leading=preset.leading + 1.5,
            spaceBefore=preset.section_space,
            spaceAfter=preset.section_space / 2,
        )
        bullet = ParagraphStyle(
            "ResumeBullet",
            parent=body,
            leftIndent=12,
            firstLineIndent=-9,
        )

        story: list[object] = []
        paragraph_lines: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph_lines:
                return
            story.append(Paragraph(_paragraph_markup(" ".join(paragraph_lines)), body))
            paragraph_lines.clear()

        for line in markdown.splitlines():
            stripped = line.strip()
            if not stripped:
                flush_paragraph()
                continue
            heading = HEADING.match(stripped)
            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                style = title if level == 1 else section
                story.append(Paragraph(_paragraph_markup(heading.group(2)), style))
                continue
            bullet_match = BULLET.match(stripped)
            if bullet_match:
                flush_paragraph()
                flowable = Paragraph("- " + _paragraph_markup(bullet_match.group(1)), bullet)
                width, height = flowable.wrap(1, available_height)
                if width and height <= available_height:
                    story.append(KeepTogether([flowable, Spacer(1, preset.section_space / 3)]))
                else:
                    story.append(flowable)
                continue
            paragraph_lines.append(stripped)
        flush_paragraph()
        return story

    @staticmethod
    def _page_count(pdf_path: Path) -> int:
        with pymupdf.open(pdf_path) as document:
            return len(document)

    def _validate_and_preview(
        self, pdf_path: Path, preview_dir: Path, expected_lines: list[str]
    ) -> tuple[str, list[Path]]:
        preview_dir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf_path) as document:
            if not 1 <= len(document) <= 2:
                raise RenderError("PAGE_LIMIT_EXCEEDED", "The resume exceeds two pages.")
            extracted_pages: list[str] = []
            preview_paths: list[Path] = []
            for index, page in enumerate(document):
                page_text = page.get_text("text")
                if not _normalize_plain_text(page_text):
                    raise RenderError("RENDER_VALIDATION_FAILED", "The PDF has an empty page.")
                if "\ufffd" in page_text or "\u25a1" in page_text:
                    raise RenderError(
                        "RENDER_VALIDATION_FAILED", "The PDF contains missing glyphs."
                    )
                page_rect = page.rect
                for block in page.get_text("blocks"):
                    rectangle = pymupdf.Rect(block[:4])
                    if not page_rect.contains(rectangle):
                        raise RenderError(
                            "RENDER_VALIDATION_FAILED",
                            "A text block falls outside the page boundary.",
                        )
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                if not any(component < 250 for component in pixmap.samples):
                    raise RenderError("RENDER_VALIDATION_FAILED", "The preview page is blank.")
                preview_path = preview_dir / f"page-{index + 1}.png"
                pixmap.save(str(preview_path))
                preview_paths.append(preview_path)
                extracted_pages.append(page_text)

        extracted_text = "\n".join(extracted_pages)
        self._assert_text_preserved(expected_lines, extracted_text)
        return extracted_text, preview_paths

    @staticmethod
    def _assert_text_preserved(expected_lines: list[str], extracted_text: str) -> None:
        searchable = _normalize_plain_text(extracted_text)
        cursor = 0
        for line in expected_lines:
            position = searchable.find(line, cursor)
            if position < 0:
                raise RenderError(
                    "RENDER_VALIDATION_FAILED",
                    "The PDF text does not preserve all resume content.",
                )
            cursor = position + len(line)


def _paragraph_markup(text: str) -> str:
    def replace_link(match: re.Match[str]) -> str:
        label, url = match.groups()
        return f"{label} ({url})"

    return escape(LINK.sub(replace_link, text))


def _normalized_markdown_lines(markdown: str) -> list[str]:
    lines: list[str] = []
    for line in markdown.splitlines():
        text = line.strip()
        heading = HEADING.match(text)
        if heading:
            text = heading.group(2)
        else:
            bullet = BULLET.match(text)
            if bullet:
                text = bullet.group(1)
        text = LINK.sub(lambda match: f"{match.group(1)} ({match.group(2)})", text)
        normalized = _normalize_plain_text(text)
        if normalized:
            lines.append(normalized)
    return lines


def _normalize_plain_text(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()
