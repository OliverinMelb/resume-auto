import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether

from resume_agent import resume_renderer
from resume_agent.resume_renderer import PRESETS, ReportLabAdapter, RenderError


class ResumeRendererTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.renderer = ReportLabAdapter()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_short_chinese_resume_is_one_page_and_text_extracts(self):
        markdown = (
            "# \\u66f9\\u7533\\u5112\n\n"
            "## \\u9879\\u76ee\\u7ecf\\u5386\n"
            "- \\u4f7f\\u7528 LangGraph \\u6784\\u5efa Agent \\u5de5\\u4f5c\\u6d41"
        )
        result = self.renderer.render(
            markdown, self.root / "resume.pdf", self.root / "preview"
        )
        self.assertEqual(result.page_count, 1)
        self.assertIn("\\u66f9\\u7533\\u5112", result.extracted_text)
        self.assertTrue((self.root / "preview" / "page-1.png").exists())

    def test_markdown_table_is_rejected(self):
        with self.assertRaisesRegex(RenderError, "UNSUPPORTED_MARKDOWN"):
            self.renderer.render(
                "| Name | Skill |\n|---|---|",
                self.root / "resume.pdf",
                self.root / "preview",
            )

    def test_markdown_table_without_outer_pipes_is_rejected(self):
        with self.assertRaisesRegex(RenderError, "UNSUPPORTED_MARKDOWN"):
            self.renderer.render(
                "Name | Skill\n--- | ---",
                self.root / "resume.pdf",
                self.root / "preview",
            )

    def test_renderer_never_emits_more_than_two_pages(self):
        markdown = "# Resume\n" + "\n".join(
            f"- Evidence {index} with a long project detail " * 8
            for index in range(200)
        )
        with self.assertRaisesRegex(RenderError, "PAGE_LIMIT_EXCEEDED"):
            self.renderer.render(
                markdown, self.root / "resume.pdf", self.root / "preview"
            )
        self.assertFalse((self.root / "resume.pdf").exists())

    def test_page_limit_failure_preserves_an_existing_destination_pdf(self):
        destination = self.root / "resume.pdf"
        destination.write_bytes(b"previously-validated-pdf")
        markdown = "# Resume\n" + "\n".join(
            f"- Evidence {index} with a long project detail " * 8
            for index in range(200)
        )
        with self.assertRaisesRegex(RenderError, "PAGE_LIMIT_EXCEEDED"):
            self.renderer.render(markdown, destination, self.root / "preview")
        self.assertEqual(destination.read_bytes(), b"previously-validated-pdf")

    def test_empty_content_is_rejected(self):
        with self.assertRaisesRegex(RenderError, "EMPTY_CONTENT"):
            self.renderer.render("  \n\n", self.root / "resume.pdf", self.root / "preview")

    def test_publication_failure_restores_existing_pdf_and_preview_atomically(self):
        destination = self.root / "resume.pdf"
        preview = self.root / "preview"
        destination.write_bytes(b"old-pdf")
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"old-page-one")
        (preview / "page-2.png").write_bytes(b"old-page-two")
        original_replace = __import__("os").replace
        failed = False

        def fail_final_pdf_publish(source, target):
            nonlocal failed
            if not failed and Path(target) == destination:
                failed = True
                raise PermissionError(f"private renderer path: {self.root}")
            return original_replace(source, target)

        with patch(
            "resume_agent.resume_renderer.os.replace", side_effect=fail_final_pdf_publish
        ):
            with self.assertRaises(RenderError) as raised:
                self.renderer.render("# Resume\n- Current evidence", destination, preview)

        self.assertTrue(failed)
        self.assertEqual(raised.exception.code, "RENDER_ERROR")
        self.assertNotIn(str(self.root), raised.exception.safe_message)
        self.assertEqual(destination.read_bytes(), b"old-pdf")
        self.assertEqual(
            {path.name: path.read_bytes() for path in preview.iterdir()},
            {"page-1.png": b"old-page-one", "page-2.png": b"old-page-two"},
        )
        self.assertEqual({path.name for path in self.root.iterdir()}, {"resume.pdf", "preview"})

    def test_publication_failure_leaves_absent_targets_absent(self):
        destination = self.root / "resume.pdf"
        preview = self.root / "preview"
        original_replace = __import__("os").replace
        failed = False

        def fail_final_pdf_publish(source, target):
            nonlocal failed
            if not failed and Path(target) == destination:
                failed = True
                raise PermissionError(f"private renderer path: {self.root}")
            return original_replace(source, target)

        with patch(
            "resume_agent.resume_renderer.os.replace", side_effect=fail_final_pdf_publish
        ):
            with self.assertRaises(RenderError) as raised:
                self.renderer.render("# Resume\n- Current evidence", destination, preview)

        self.assertTrue(failed)
        self.assertEqual(raised.exception.code, "RENDER_ERROR")
        self.assertNotIn(str(self.root), raised.exception.safe_message)
        self.assertFalse(destination.exists())
        self.assertFalse(preview.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rollback_restore_failure_requires_recovery_and_keeps_old_pdf_backup(self):
        destination = self.root / "resume.pdf"
        preview = self.root / "preview"
        destination.write_bytes(b"old-pdf")
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"old-page-one")
        (preview / "page-2.png").write_bytes(b"old-page-two")
        original_replace = __import__("os").replace
        stage_publish_failed = False
        restore_failed = False

        def fail_publish_then_restore(source, target):
            nonlocal stage_publish_failed, restore_failed
            source_path = Path(source)
            target_path = Path(target)
            if not stage_publish_failed and target_path == destination:
                stage_publish_failed = True
                raise PermissionError(f"private staged path: {self.root}")
            if (
                stage_publish_failed
                and not restore_failed
                and target_path == destination
                and ".pdf-backup-" in source_path.name
            ):
                restore_failed = True
                raise PermissionError(f"private backup path: {self.root}")
            return original_replace(source, target)

        with patch(
            "resume_agent.resume_renderer.os.replace", side_effect=fail_publish_then_restore
        ):
            with self.assertRaises(RenderError) as raised:
                self.renderer.render("# Resume\n- Current evidence", destination, preview)

        self.assertTrue(stage_publish_failed)
        self.assertTrue(restore_failed)
        self.assertEqual(raised.exception.code, "RENDER_RECOVERY_REQUIRED")
        self.assertNotIn(str(self.root), raised.exception.safe_message)
        backups = list(self.root.glob(".resume.pdf.pdf-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old-pdf")

    def test_committed_publish_survives_preview_backup_cleanup_failure(self):
        destination = self.root / "resume.pdf"
        preview = self.root / "preview"
        destination.write_bytes(b"old-pdf")
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"old-preview")
        original_remove = resume_renderer._remove_path
        pdf_backup_deleted = False
        preview_cleanup_failed = False

        def fail_after_pdf_backup_cleanup(path):
            nonlocal pdf_backup_deleted, preview_cleanup_failed
            candidate = Path(path)
            if ".pdf-backup-" in candidate.name:
                original_remove(candidate)
                pdf_backup_deleted = True
                return
            if pdf_backup_deleted and ".preview-backup-" in candidate.name:
                preview_cleanup_failed = True
                raise PermissionError(f"private cleanup path: {self.root}")
            original_remove(candidate)

        with patch(
            "resume_agent.resume_renderer._remove_path",
            side_effect=fail_after_pdf_backup_cleanup,
        ):
            try:
                result = self.renderer.render(
                    "# Resume\n- Current evidence", destination, preview
                )
            except RenderError as error:
                self.fail(f"committed cleanup failure rolled publication back: {error.code}")

        self.assertTrue(pdf_backup_deleted)
        self.assertTrue(preview_cleanup_failed)
        self.assertEqual(result.page_count, 1)
        self.assertTrue(destination.exists())
        self.assertNotEqual(destination.read_bytes(), b"old-pdf")
        self.assertTrue((preview / "page-1.png").exists())
        self.assertEqual(list(self.root.glob(".resume.pdf.pdf-backup-*")), [])
        preview_backups = list(self.root.glob(".preview.preview-backup-*"))
        self.assertEqual(len(preview_backups), 1)
        self.assertEqual(
            (preview_backups[0] / "page-1.png").read_bytes(), b"old-preview"
        )

    def test_successful_republish_removes_all_transaction_backups(self):
        destination = self.root / "resume.pdf"
        preview = self.root / "preview"
        destination.write_bytes(b"old-pdf")
        preview.mkdir()
        (preview / "page-1.png").write_bytes(b"old-preview")

        self.renderer.render("# Resume\n- Current evidence", destination, preview)

        self.assertEqual(
            [path for path in self.root.iterdir() if "backup" in path.name], []
        )

    def test_real_document_width_keeps_a_bullet_that_fits_on_one_page_together(self):
        preset = PRESETS[0]
        real_width = A4[0] - 2 * preset.margin_mm * mm
        real_height = A4[1] - 2 * preset.margin_mm * mm
        story = self.renderer._flowables(
            "- " + "evidence " * 550,
            preset,
            self.renderer._register_font(),
            real_height,
        )
        measured = story[0]._content[0] if isinstance(story[0], KeepTogether) else story[0]
        _, real_height_used = measured.wrap(real_width, real_height)
        self.assertLess(real_height_used, real_height)
        self.assertIsInstance(story[0], KeepTogether)

    def test_truly_tall_bullet_is_allowed_to_wrap(self):
        preset = PRESETS[0]
        real_height = A4[1] - 2 * preset.margin_mm * mm
        story = self.renderer._flowables(
            "- " + "evidence " * 1400,
            preset,
            self.renderer._register_font(),
            real_height,
        )
        self.assertNotIsInstance(story[0], KeepTogether)

    def test_two_page_resume_selects_the_readable_two_page_preset(self):
        markdown = "# Resume\n\n## Experience\n" + "\n".join(
            f"- Project {index}: built, tested, and operated a reliable Agent workflow."
            for index in range(58)
        )
        result = self.renderer.render(
            markdown, self.root / "resume.pdf", self.root / "preview"
        )
        self.assertEqual(result.page_count, 2)
        self.assertEqual(
            [path.name for path in result.preview_paths], ["page-1.png", "page-2.png"]
        )

    def test_repeated_wrapped_bullet_and_link_text_extract_in_order(self):
        long_evidence = "wrapped evidence " * 35
        markdown = (
            "# Resume\n\n"
            f"- {long_evidence}[Portfolio](https://example.com/portfolio)\n"
            "- Repeated evidence\n"
            "- Repeated evidence"
        )
        result = self.renderer.render(
            markdown, self.root / "resume.pdf", self.root / "preview"
        )
        expected = [
            "Resume",
            f"{long_evidence.strip()} Portfolio (https://example.com/portfolio)",
            "Repeated evidence",
            "Repeated evidence",
        ]
        cursor = 0
        extracted = " ".join(result.extracted_text.split())
        for line in expected:
            position = extracted.find(line, cursor)
            self.assertNotEqual(position, -1)
            cursor = position + len(line)

    def test_blank_trailing_page_is_rejected_during_pdf_validation(self):
        valid_pdf = self.root / "valid.pdf"
        self.renderer.render("# Resume\n- Evidence", valid_pdf, self.root / "valid-preview")
        invalid_pdf = self.root / "invalid.pdf"
        with pymupdf.open(valid_pdf) as document:
            document.new_page()
            document.save(invalid_pdf)
        with self.assertRaises(RenderError) as raised:
            self.renderer._validate_and_preview(
                invalid_pdf, self.root / "invalid-preview", ["Resume", "Evidence"]
            )
        self.assertEqual(raised.exception.code, "RENDER_VALIDATION_FAILED")

    def test_font_falls_back_to_next_usable_configured_font(self):
        fallback_name = "ResumeFallbackFontForTest"
        with (
            patch.object(resume_renderer, "FONT_NAME", fallback_name),
            patch.object(
                resume_renderer,
                "FONT_PATHS",
                (self.root / "missing-font.ttf", Path(r"C:\Windows\Fonts\simhei.ttf")),
            ),
        ):
            self.assertEqual(self.renderer._register_font(), fallback_name)

    def test_missing_fonts_return_a_safe_error(self):
        with (
            patch.object(resume_renderer, "FONT_NAME", "NoFontForTest"),
            patch.object(
                resume_renderer,
                "FONT_PATHS",
                (self.root / "missing-one.ttf", self.root / "missing-two.ttf"),
            ),
        ):
            with self.assertRaises(RenderError) as raised:
                self.renderer._register_font()
        self.assertEqual(raised.exception.code, "FONT_UNAVAILABLE")
        self.assertNotIn(str(self.root), raised.exception.safe_message)


if __name__ == "__main__":
    unittest.main()
