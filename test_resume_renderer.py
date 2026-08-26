import tempfile
import unittest
from pathlib import Path

from resume_agent.resume_renderer import ReportLabAdapter, RenderError


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


if __name__ == "__main__":
    unittest.main()
