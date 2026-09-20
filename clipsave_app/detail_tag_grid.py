from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QGridLayout, QPushButton, QSizePolicy, QWidget

from .ui_primitives import color_dot


class DetailTagGrid(QWidget):
    remove_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.names: list[str] = []
        self.colors: list[str] = []
        self.expanded = False
        self.more_button: QPushButton | None = None
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)

    def set_tags(self, names: str, colors: str) -> None:
        self.names = names.split("\x1f") if names else []
        self.colors = colors.split("\x1f") if colors else []
        self._rebuild()

    def reset_expanded(self) -> None:
        if not self.expanded:
            return
        self.expanded = False
        self._rebuild()

    def clear_tags(self) -> None:
        self.names = []
        self.colors = []
        self.expanded = False
        self._rebuild()

    def _toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self._rebuild()

    def _rebuild(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.more_button = None
        visible_names = self.names if self.expanded else self.names[:4]
        for index, name in enumerate(visible_names):
            button = QPushButton()
            button.setObjectName("TagChip")
            button.setText(
                button.fontMetrics().elidedText(
                    name,
                    Qt.TextElideMode.ElideRight,
                    108,
                )
            )
            button.setMinimumWidth(0)
            button.setMaximumWidth(130)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            color = self.colors[index] if index < len(self.colors) else "#64748b"
            button.setIcon(QIcon(color_dot(color)))
            button.setToolTip(f"{name}\n点击移除标签")
            button.clicked.connect(
                lambda _checked=False, tag=name: self.remove_requested.emit(tag)
            )
            self.grid.addWidget(button, index // 2, index % 2)
        if len(self.names) > 4:
            more = QPushButton()
            more.setObjectName("TagMoreButton")
            if self.expanded:
                more.setText("收起标签")
                more.setToolTip("仅显示前四个标签")
            else:
                more.setText(f"更多标签  +{len(self.names) - 4}")
                more.setToolTip("显示全部标签")
            more.clicked.connect(self._toggle_expanded)
            self.grid.addWidget(more, (len(visible_names) + 1) // 2, 0, 1, 2)
            self.more_button = more
