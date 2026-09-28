from __future__ import annotations

from PySide6.QtCore import QByteArray, QUrl, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QTextBrowser, QTextEdit

from .ui_primitives import ThemedTextContextMenuMixin


class SafeMarkdownBrowser(ThemedTextContextMenuMixin, QTextBrowser):
    _ALLOWED_RESOURCE_SCHEMES = {"qrc"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)

    def loadResource(self, resource_type: int, name: QUrl):
        if (
            name.isRelative()
            or name.isLocalFile()
            or name.scheme().lower() not in self._ALLOWED_RESOURCE_SCHEMES
        ):
            return QByteArray()
        return super().loadResource(resource_type, name)


MAX_RICH_MARKDOWN_BYTES = 2 * 1024 * 1024


def set_markdown_content(browser: QTextBrowser, content: str) -> None:
    if len(content.encode("utf-8")) > MAX_RICH_MARKDOWN_BYTES:
        browser.setPlainText(content)
    else:
        # Captured text can contain fragments such as "cd <project>".  Raw
        # HTML parsing treats these as unclosed tags and hides everything that
        # follows in a daily Markdown export.  Keep GitHub Markdown features,
        # but render embedded HTML as literal text instead.
        browser.document().setMarkdown(
            content,
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )


_SafeMarkdownBrowser = SafeMarkdownBrowser
_set_markdown_content = set_markdown_content
