import unittest

from PySide6.QtGui import QFont, QTextOption
from PySide6.QtWidgets import QApplication

from clipsave_app.asset_text_layout import (
    plain_text_layout_signature,
    plain_text_layout_source,
    plain_text_wrap_mode,
)


class AssetTextLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_machine_values_wrap_anywhere(self):
        self.assertEqual(
            plain_text_wrap_mode("https://example.com/a/very/long/path"),
            QTextOption.WrapMode.WrapAnywhere,
        )
        self.assertEqual(
            plain_text_wrap_mode("ordinary natural language sentence"),
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere,
        )

    def test_embedded_machine_span_keeps_original_boundaries(self):
        content = "Read https://example.com/path and continue."
        transformed, boundaries = plain_text_layout_source(content)

        self.assertEqual(transformed.replace("\u200b", ""), content)
        self.assertEqual(boundaries[-1], len(content))

    def test_non_bmp_text_uses_qt_utf16_offsets(self):
        content = "hello 😀 world"
        transformed, boundaries = plain_text_layout_source(content)

        self.assertEqual(len(boundaries), len(transformed.encode("utf-16-le")) // 2 + 1)
        self.assertEqual(boundaries[-1], len(content))
        self.assertEqual(
            plain_text_layout_signature(content, 500, 180, QFont()),
            ((0, len(content)),),
        )

    def test_non_bmp_text_with_machine_span_and_newline(self):
        content = "Read https://example.com/😀\nnext line"
        transformed, boundaries = plain_text_layout_source(content)

        self.assertEqual(len(boundaries), len(transformed.encode("utf-16-le")) // 2 + 1)
        self.assertEqual(boundaries[-1], len(content))
        self.assertEqual(
            plain_text_layout_signature(content, 500, 180, QFont()),
            ((0, content.index("\n") + 1), (content.index("\n") + 1, len("next line"))),
        )

    def test_signature_is_stable_for_same_inputs(self):
        font = QFont()
        first = plain_text_layout_signature("https://example.com/abcdef", 160, 180, font)
        second = plain_text_layout_signature("https://example.com/abcdef", 160, 180, font)
        self.assertEqual(first, second)
