from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from .elided_label import ElidedLabel
from .ui_primitives import color_dot


class DetailTagChip(QFrame):
    remove_requested = Signal(str)

    def __init__(self, name: str, color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("TagChip")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(4)
        dot = QLabel()
        dot.setPixmap(color_dot(color, 10))
        dot.setFixedSize(10, 10)
        layout.addWidget(dot)
        self.label = ElidedLabel(name)
        layout.addWidget(self.label, 1)
        self.remove_button = QPushButton("×")
        self.remove_button.setObjectName("TagRemoveButton")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.setToolTip(f"从当前条目移除标签：{name}")
        self.remove_button.setAccessibleName(self.remove_button.toolTip())
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(name))
        layout.addWidget(self.remove_button)

    def text(self) -> str:
        return self.label.text()


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
                item.widget().hide()
                item.widget().deleteLater()
        self.more_button = None
        visible_names = self.names if self.expanded else self.names[:4]
        for index, name in enumerate(visible_names):
            color = self.colors[index] if index < len(self.colors) else "#64748b"
            chip = DetailTagChip(name, color)
            chip.remove_requested.connect(self.remove_requested)
            self.grid.addWidget(chip, index // 2, index % 2)
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
