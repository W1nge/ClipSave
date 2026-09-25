"""Contract tests for the card markdown preview renderer.

The renderer's docstring and the CHANGELOG make three promises: user content
is always HTML-escaped, no image/resource markup is ever emitted, and the
rendered output is formatted rather than source markdown. These tests pin
those promises.
"""

from __future__ import annotations

import unittest

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from clipsave_app.card_markdown import (
    _DARK_COLORS,
    _render_html,
    build_card_markdown_document,
)


class CardMarkdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.font = QFont("Microsoft YaHei", 10)

    def build(self, content: str, dark: bool = True):
        return build_card_markdown_document(content, dark, self.font)

    def test_user_content_is_escaped_and_never_treated_as_html(self):
        document = self.build("<b>bold</b><img src='http://x/y.png'> & <script>")
        plain = document.toPlainText()
        self.assertIn("<b>bold</b>", plain)
        self.assertIn("<script>", plain)

    def test_no_image_or_resource_markup_is_emitted(self):
        document = self.build(
            "![ghost](https://evil.example/x.png)\n\n[link](https://ok.example)"
        )
        html_text = document.toHtml().lower()
        self.assertNotIn("<img", html_text)
        self.assertNotIn("evil.example/x.png", html_text)
        self.assertIn("href", html_text)

    def test_common_blocks_render_without_source_markers(self):
        document = self.build(
            "intro\n\n# Title\n\n## Section\n\n- [x] done\n- [ ] open item\n\n> quoted\n\n"
            "```python\ncode()\n```\n\n---\n\n完。\n"
        )
        plain = document.toPlainText()
        self.assertIn("intro", plain)
        self.assertIn("Title", plain)
        self.assertIn("Section", plain)
        self.assertIn("☑", plain)
        self.assertIn("□", plain)
        self.assertIn("done", plain)
        self.assertIn("quoted", plain)
        self.assertIn("code()", plain)
        self.assertNotIn("```", plain)
        self.assertNotIn("---", plain)
        self.assertNotIn("[x]", plain)

    def test_ordered_list_honours_start_number(self):
        html_text = _render_html("3. three\n4. four", _DARK_COLORS)
        self.assertIn('start="3"', html_text)

    def test_indented_fence_body_is_not_treated_as_closing_fence(self):
        document = self.build("```\ncode\n    ``` deeper\nstill code\n```\nafter")
        plain = document.toPlainText()
        self.assertIn("``` deeper", plain)
        self.assertIn("still code", plain)
        self.assertIn("after", plain)

    def test_autolink_trailing_entity_is_not_swallowed(self):
        document = self.build("see https://example.com/a>")
        html_text = document.toHtml().lower()
        self.assertIn('href="https://example.com/a"', html_text)
        self.assertNotIn("example.com/a&gt", html_text)


if __name__ == "__main__":
    unittest.main()
