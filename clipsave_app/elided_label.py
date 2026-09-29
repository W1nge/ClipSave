from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QSizePolicy


class ElidedLabel(QLabel):
    """Keep the full plain text, but fit its painted form to the available width."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def display_text(self) -> str:
        return self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight,
            max(0, self.contentsRect().width() - self.margin() * 2),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        margin = self.margin()
        rect = self.contentsRect().adjusted(margin, margin, -margin, -margin)
        self.style().drawItemText(
            painter, rect, self.alignment(), self.palette(), self.isEnabled(),
            self.display_text(), self.foregroundRole(),
        )
