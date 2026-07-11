from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QEasingCurve, QEvent, QSize, Qt, QPropertyAnimation, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .constants import TYPE_LABELS
from lucide import _render_icon


GLYPHS = {
    "menu": "panel-left-close",
    "all": "inbox",
    "image": "image",
    "text": "file-type",
    "markdown": "file-text",
    "favorite": "star",
    "recent": "history",
    "calendar": "calendar-days",
    "folder": "folder",
    "tag": "tag",
    "settings": "settings",
    "search": "search",
    "grid": "layout-grid",
    "list": "list",
    "filter": "funnel",
    "info": "info",
    "add": "plus",
    "close": "x",
    "copy": "copy",
    "delete": "trash-2",
    "open": "external-link",
    "pause": "pause",
    "play": "play",
    "more": "ellipsis",
    "sparkles": "sparkles",
    "scan": "scan-text",
}


def lucide_icon(name: str, color: str = "#354052", size: int = 20, fill: str = "none") -> QIcon:
    svg = _render_icon(name, size, stroke=color, fill=fill, stroke_width="1.8")
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def human_size(value: int) -> str:
    amount = float(value or 0)
    for suffix in ("B", "KB", "MB", "GB"):
        if amount < 1024 or suffix == "GB":
            return f"{amount:.0f} {suffix}" if suffix == "B" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return "0 B"


def friendly_day(value: str) -> str:
    try:
        day = dt.date.fromisoformat(value[:10])
    except ValueError:
        return value[:10]
    today = dt.date.today()
    if day == today:
        return "今天"
    if day == today - dt.timedelta(days=1):
        return "昨天"
    return f"{day:%Y年%m月%d日}  {['周一','周二','周三','周四','周五','周六','周日'][day.weekday()]}"


def color_dot(color: str, size: int = 12) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return pixmap


class IconButton(QPushButton):
    def __init__(self, glyph: str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("IconButton")
        self.setIcon(lucide_icon(GLYPHS.get(glyph, glyph)))
        self.setIconSize(QSize(18, 18))
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class CaptureStatusButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CaptureStatus")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(28, 28)
        self.active = True
        self.set_active(True)

    def set_active(self, active: bool) -> None:
        self.active = active
        color = "#20a464" if active else "#d44c4c"
        self.setIcon(QIcon(color_dot(color, 10)))
        self.setIconSize(QSize(10, 10))
        self.setToolTip("本地自动捕获已开启" if active else "本地自动捕获已暂停")


class WindowTitleBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WindowTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        self.minimize_button = self._window_button("minus", "最小化")
        self.maximize_button = self._window_button("square", "最大化")
        self.close_button = self._window_button("x", "关闭")
        self.close_button.setObjectName("CloseWindowButton")
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    @staticmethod
    def _window_button(glyph: str, tooltip: str) -> IconButton:
        button = IconButton(glyph, tooltip)
        button.setObjectName("WindowButton")
        button.setFixedSize(46, 32)
        button.setIconSize(QSize(14, 14))
        return button

    def update_maximize_state(self, maximized: bool) -> None:
        self.maximize_button.setIcon(lucide_icon("copy" if maximized else "square", size=14))
        self.maximize_button.setToolTip("还原" if maximized else "最大化")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            window.showNormal() if window.isMaximized() else window.showMaximized()
            self.update_maximize_state(window.isMaximized())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class NavButton(QPushButton):
    triggered = Signal(str)

    def __init__(self, key: str, glyph: str, label: str, count: int | None = None, parent=None):
        super().__init__(parent)
        self.key = key
        self.glyph = GLYPHS.get(glyph, glyph)
        self.label = label
        self.count = count
        self.collapsed = False
        self.setObjectName("NavButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda: self.triggered.emit(self.key))
        self.refresh_text()

    def refresh_text(self) -> None:
        if self.collapsed:
            self.setText("")
            self.setToolTip(self.label)
            self.setStyleSheet("text-align:left;")
        else:
            suffix = f"    {self.count:,}" if self.count is not None else ""
            self.setText(f"{self.label}{suffix}")
            self.setToolTip("")
            self.setStyleSheet("text-align:left;")
        self.setIcon(lucide_icon(self.glyph, "#2f6fca" if self.property("active") else "#4d596b"))
        self.setIconSize(QSize(18, 18))

    def set_collapsed(self, value: bool) -> None:
        self.collapsed = value
        self.refresh_text()

    def set_active(self, value: bool) -> None:
        self.setProperty("active", value)
        self.refresh_text()
        self.style().unpolish(self)
        self.style().polish(self)


class Sidebar(QWidget):
    navigation_requested = Signal(str, object)
    add_collection_requested = Signal()
    add_tag_requested = Signal()
    settings_requested = Signal()
    collapsed_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(72)
        self.setMaximumWidth(242)
        self.collapsed = False
        self.nav_buttons: dict[str, NavButton] = {}
        self.collection_buttons: list[NavButton] = []
        self.tag_buttons: list[NavButton] = []
        self.footer_buttons: list[NavButton] = []
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(10, 12, 10, 12)
        self.layout_root.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(7)
        self.collapse_button = IconButton("menu", "收起侧栏")
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_button)
        self.brand = QLabel("ClipSave")
        self.brand.setObjectName("Title")
        header.addWidget(self.brand)
        header.addStretch()
        self.layout_root.addLayout(header)
        self.layout_root.addSpacing(10)

        self.primary_box = QVBoxLayout()
        self.primary_box.setSpacing(3)
        self.layout_root.addLayout(self.primary_box)

        self.collection_heading = self._section_heading("集合", self.add_collection_requested)
        self.layout_root.addWidget(self.collection_heading)
        self.collection_box = QVBoxLayout()
        self.collection_box.setSpacing(2)
        self.layout_root.addLayout(self.collection_box)

        self.tag_heading = self._section_heading("标签", self.add_tag_requested)
        self.layout_root.addWidget(self.tag_heading)
        self.tag_box = QVBoxLayout()
        self.tag_box.setSpacing(2)
        self.layout_root.addLayout(self.tag_box)
        self.layout_root.addStretch(1)

        settings = NavButton("settings", "settings", "设置")
        settings.triggered.connect(lambda _key: self.settings_requested.emit())
        self.layout_root.addWidget(settings)
        self.footer_buttons.append(settings)

    def _section_heading(self, text: str, signal: Signal) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(10, 14, 4, 3)
        label = QLabel(text)
        label.setObjectName("Muted")
        row.addWidget(label)
        row.addStretch()
        add = IconButton("add", f"新建{text}")
        add.clicked.connect(signal.emit)
        row.addWidget(add)
        widget.heading_label = label
        widget.add_button = add
        return widget

    def set_primary(self, counts: dict[str, int]) -> None:
        while self.primary_box.count():
            item = self.primary_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.nav_buttons.clear()
        entries = [
            ("all", "all", "全部内容", counts.get("all", 0)),
            ("favorite", "favorite", "收藏", counts.get("favorite", 0)),
            ("recent", "recent", "最近使用", None),
            ("date", "calendar", "按日期打开", None),
            ("image", "image", "图片", counts.get("image", 0)),
            ("text", "text", "文字", counts.get("text", 0)),
            ("markdown", "markdown", "Markdown", counts.get("markdown", 0)),
        ]
        for key, glyph, label, count in entries:
            button = NavButton(key, glyph, label, count)
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda selected, k=key: self.navigation_requested.emit(k, None))
            self.primary_box.addWidget(button)
            self.nav_buttons[key] = button

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def set_collections(self, collections) -> None:
        self._clear_layout(self.collection_box)
        self.collection_buttons = []
        for row in collections:
            button = NavButton(f"collection:{row['id']}", "folder", row["name"], row["amount"])
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda _key, ident=row["id"]: self.navigation_requested.emit("collection", ident))
            self.collection_box.addWidget(button)
            self.collection_buttons.append(button)

    def set_tags(self, tags) -> None:
        self._clear_layout(self.tag_box)
        self.tag_buttons = []
        for row in tags[:8]:
            button = NavButton(f"tag:{row['id']}", "tag", row["name"], row["amount"])
            button.setIcon(QIcon(color_dot(row["color"])))
            button.triggered.connect(lambda _key, ident=row["id"]: self.navigation_requested.emit("tag", ident))
            self.tag_box.addWidget(button)
            self.tag_buttons.append(button)

    def set_active(self, key: str) -> None:
        for button_key, button in self.nav_buttons.items():
            button.set_active(button_key == key)

    def set_collapsed(self, value: bool, animate: bool = True) -> None:
        self.collapsed = value
        self.brand.setVisible(not value)
        self.collapse_button.setIcon(lucide_icon("panel-left-open" if value else "panel-left-close"))
        self.collection_heading.setVisible(not value)
        self.tag_heading.setVisible(not value)
        for button in [*self.nav_buttons.values(), *self.collection_buttons, *self.tag_buttons, *self.footer_buttons]:
            button.set_collapsed(value)
        start = self.width()
        end = 72 if value else 242
        self.setMinimumWidth(72 if value else 200)
        if animate:
            self.animation = QPropertyAnimation(self, b"maximumWidth", self)
            self.animation.setDuration(190)
            self.animation.setStartValue(start)
            self.animation.setEndValue(end)
            self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.animation.start()
        else:
            self.setMaximumWidth(end)
        self.collapse_button.setToolTip("展开侧栏" if value else "收起侧栏")
        self.collapsed_changed.emit(value)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)


class AssetCard(QFrame):
    selected = Signal(int)
    activated = Signal(int)
    favorite_requested = Signal(int, bool)

    def __init__(self, item, width: int, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("Card")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(252)
        self.setMinimumWidth(190)
        self.setMaximumWidth(max(210, width))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 9, 9, 9)
        layout.setSpacing(7)

        head = QHBoxLayout()
        kind = QLabel(TYPE_LABELS.get(item["kind"], item["kind"]))
        kind.setObjectName("Muted")
        head.addWidget(kind)
        head.addStretch()
        time_text = item["created_at"][11:16] if len(item["created_at"]) >= 16 else ""
        time = QLabel(time_text)
        time.setObjectName("Muted")
        head.addWidget(time)
        favorite = IconButton("favorite", "取消收藏" if item["favorite"] else "收藏")
        favorite.setIcon(lucide_icon("star", "#f4a100", 18, "#f4a100" if item["favorite"] else "none"))
        favorite.clicked.connect(lambda: self.favorite_requested.emit(item["id"], not bool(item["favorite"])))
        head.addWidget(favorite)
        layout.addLayout(head)

        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setFixedHeight(150)
        preview.setWordWrap(True)
        preview.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        if item["kind"] == "image" and item["path"] and Path(item["path"]).exists():
            pixmap = QPixmap(item["path"])
            preview.setPixmap(pixmap.scaled(QSize(max(180, width - 20), 150), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            preview.setStyleSheet("background:rgba(237,241,247,150); border-radius:5px;")
        else:
            content = item["content"].strip() or item["title"]
            preview.setText(content[:330])
            preview.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            preview.setStyleSheet("background:rgba(247,249,252,180); padding:10px; border-radius:5px;")
        layout.addWidget(preview)

        title = QLabel(item["title"])
        title.setToolTip(item["title"])
        title.setWordWrap(False)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        font = title.font()
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        layout.addWidget(title)
        detail = QLabel(self._detail_text())
        detail.setObjectName("Muted")
        layout.addWidget(detail)

    def _detail_text(self) -> str:
        if self.item["kind"] == "image" and self.item["width"]:
            return f"{self.item['width']} × {self.item['height']}   {human_size(self.item['file_size'])}"
        return f"{TYPE_LABELS.get(self.item['kind'], '')}   {human_size(self.item['file_size'])}"

    def set_selected(self, value: bool) -> None:
        self.setProperty("selected", value)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.item["id"])
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.item["id"])
        super().mouseDoubleClickEvent(event)


class AssetGrid(QScrollArea):
    item_selected = Signal(int)
    item_activated = Signal(int)
    favorite_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.layout_root = QVBoxLayout(self.container)
        self.layout_root.setContentsMargins(20, 16, 20, 24)
        self.layout_root.setSpacing(16)
        self.layout_root.addStretch()
        self.setWidget(self.container)
        self.items = []
        self.cards: dict[int, AssetCard] = {}
        self.selected_id: int | None = None
        self.columns = 0

    def set_items(self, items) -> None:
        self.items = list(items)
        self.rebuild()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        columns = max(1, self.viewport().width() // 245)
        if columns != self.columns:
            self.columns = columns
            self.rebuild()

    def _clear(self) -> None:
        while self.layout_root.count():
            item = self.layout_root.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_nested(item.layout())
        self.cards.clear()

    def _clear_nested(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_nested(item.layout())

    def rebuild(self) -> None:
        self._clear()
        if not self.items:
            empty = QLabel("没有找到符合条件的内容")
            empty.setObjectName("Muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(320)
            self.layout_root.addWidget(empty)
            self.layout_root.addStretch()
            return
        columns = self.columns or max(1, self.viewport().width() // 245)
        card_width = max(210, (self.viewport().width() - 48 - (columns - 1) * 12) // columns)
        grouped: dict[str, list] = {}
        today = dt.date.today()
        yesterday = today - dt.timedelta(days=1)
        for item in self.items:
            raw_day = item["created_at"][:10]
            try:
                parsed = dt.date.fromisoformat(raw_day)
            except ValueError:
                parsed = None
            group = "today" if parsed == today else "yesterday" if parsed == yesterday else "earlier"
            grouped.setdefault(group, []).append(item)
        for group, records in grouped.items():
            label = {"today": "今天", "yesterday": "昨天", "earlier": "更早"}[group]
            heading = QLabel(f"{label}   {len(records)}")
            heading.setObjectName("SectionTitle")
            self.layout_root.addWidget(heading)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(12)
            for index, item in enumerate(records):
                card = AssetCard(item, card_width)
                card.selected.connect(self.select_item)
                card.activated.connect(self.item_activated)
                card.favorite_requested.connect(self.favorite_requested)
                grid.addWidget(card, index // columns, index % columns)
                self.cards[item["id"]] = card
            for col in range(columns):
                grid.setColumnStretch(col, 1)
            self.layout_root.addLayout(grid)
        self.layout_root.addStretch()
        if self.selected_id in self.cards:
            self.cards[self.selected_id].set_selected(True)

    def select_item(self, item_id: int) -> None:
        if self.selected_id in self.cards:
            self.cards[self.selected_id].set_selected(False)
        self.selected_id = item_id
        if item_id in self.cards:
            self.cards[item_id].set_selected(True)
        self.item_selected.emit(item_id)


class AssetTable(QTableWidget):
    item_selected = Signal(int)
    item_activated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(["名称", "类型", "标签", "捕获时间", "大小"])
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().hide()
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.setColumnWidth(1, 90)
        self.setColumnWidth(2, 170)
        self.setColumnWidth(3, 160)
        self.setColumnWidth(4, 90)
        self.itemSelectionChanged.connect(self._selection_changed)
        self.cellDoubleClicked.connect(lambda row, _column: self.item_activated.emit(int(self.item(row, 0).data(Qt.ItemDataRole.UserRole))))

    def set_items(self, items) -> None:
        self.setRowCount(len(items))
        for row_index, record in enumerate(items):
            values = [record["title"], TYPE_LABELS.get(record["kind"], record["kind"]), record["tag_names"] or "", record["created_at"].replace("T", " "), human_size(record["file_size"])]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, record["id"])
                self.setItem(row_index, column, cell)
        self.resizeRowsToContents()

    def _selection_changed(self) -> None:
        rows = self.selectionModel().selectedRows()
        if rows:
            item = self.item(rows[0].row(), 0)
            self.item_selected.emit(int(item.data(Qt.ItemDataRole.UserRole)))


class DetailPanel(QWidget):
    close_requested = Signal()
    copy_requested = Signal(int)
    open_requested = Signal(int)
    delete_requested = Signal(int)
    favorite_requested = Signal(int, bool)
    notes_changed = Signal(int, str)
    add_tag_requested = Signal(int)
    remove_tag_requested = Signal(int, str)
    collection_changed = Signal(int, object)
    ai_requested = Signal(int)
    ocr_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailPanel")
        self.setMinimumWidth(300)
        self.setMaximumWidth(370)
        self.current_item = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        top = QHBoxLayout()
        self.type_badge = QLabel("详情")
        self.type_badge.setObjectName("Muted")
        top.addWidget(self.type_badge)
        top.addStretch()
        close = IconButton("close", "收起详情")
        close.clicked.connect(self.close_requested)
        top.addWidget(close)
        layout.addLayout(top)
        self.title = QLabel("选择一项查看详情")
        self.title.setObjectName("Title")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.preview_stack = QStackedWidget()
        self.image_preview = QLabel()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumHeight(190)
        self.image_preview.setStyleSheet("background:rgba(235,239,245,160); border-radius:6px;")
        self.text_preview = QTextBrowser()
        self.text_preview.setOpenExternalLinks(False)
        self.text_preview.setMinimumHeight(190)
        self.preview_stack.addWidget(self.image_preview)
        self.preview_stack.addWidget(self.text_preview)
        layout.addWidget(self.preview_stack, 1)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.meta)

        collection_row = QHBoxLayout()
        collection_row.addWidget(QLabel("集合"))
        self.collection_combo = QComboBox()
        self.collection_combo.currentIndexChanged.connect(self._collection_changed)
        collection_row.addWidget(self.collection_combo, 1)
        layout.addLayout(collection_row)

        tag_title = QHBoxLayout()
        tag_title.addWidget(QLabel("标签"))
        tag_title.addStretch()
        add_tag = IconButton("add", "添加标签")
        add_tag.clicked.connect(lambda: self.current_item and self.add_tag_requested.emit(self.current_item["id"]))
        tag_title.addWidget(add_tag)
        layout.addLayout(tag_title)
        self.tags_box = QHBoxLayout()
        layout.addLayout(self.tags_box)

        ocr_row = QHBoxLayout()
        ocr_row.addWidget(QLabel("OCR 文字"))
        ocr_row.addStretch()
        self.ocr_button = QPushButton("识别文字")
        self.ocr_button.setIcon(lucide_icon("scan-text"))
        self.ocr_button.clicked.connect(lambda: self.current_item and self.ocr_requested.emit(self.current_item["id"]))
        ocr_row.addWidget(self.ocr_button)
        layout.addLayout(ocr_row)
        self.ocr_text = QLabel("尚未识别")
        self.ocr_text.setObjectName("Muted")
        self.ocr_text.setWordWrap(True)
        self.ocr_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.ocr_text)

        ai_row = QHBoxLayout()
        ai_row.addWidget(QLabel("AI 描述"))
        ai_row.addStretch()
        self.ai_button = QPushButton("生成描述")
        self.ai_button.setIcon(lucide_icon("sparkles"))
        self.ai_button.clicked.connect(lambda: self.current_item and self.ai_requested.emit(self.current_item["id"]))
        ai_row.addWidget(self.ai_button)
        layout.addLayout(ai_row)
        self.ai_description = QLabel("尚未生成")
        self.ai_description.setObjectName("Muted")
        self.ai_description.setWordWrap(True)
        self.ai_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.ai_description)

        layout.addWidget(QLabel("备注"))
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("添加备注…")
        self.notes.setMaximumHeight(90)
        self.notes.installEventFilter(self)
        layout.addWidget(self.notes)
        actions = QHBoxLayout()
        for glyph, tooltip, signal in [
            ("open", "打开", self.open_requested),
            ("copy", "复制", self.copy_requested),
            ("favorite", "收藏", None),
            ("delete", "删除", self.delete_requested),
        ]:
            button = IconButton(glyph, tooltip)
            if signal:
                button.clicked.connect(lambda _checked=False, s=signal: self.current_item and s.emit(self.current_item["id"]))
            else:
                button.clicked.connect(lambda: self.current_item and self.favorite_requested.emit(self.current_item["id"], not bool(self.current_item["favorite"])))
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

    def set_collections(self, collections) -> None:
        selected = self.collection_combo.currentData()
        self.collection_combo.blockSignals(True)
        self.collection_combo.clear()
        self.collection_combo.addItem("未分类", None)
        for collection in collections:
            self.collection_combo.addItem(collection["name"], collection["id"])
        index = self.collection_combo.findData(selected)
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)

    def set_item(self, item) -> None:
        self.current_item = item
        self.type_badge.setText(TYPE_LABELS.get(item["kind"], item["kind"]))
        self.title.setText(item["title"])
        if item["kind"] == "image" and item["path"] and Path(item["path"]).exists():
            pixmap = QPixmap(item["path"])
            self.image_preview.setPixmap(pixmap.scaled(330, 230, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.preview_stack.setCurrentWidget(self.image_preview)
        else:
            if item["kind"] == "markdown":
                self.text_preview.setMarkdown(item["content"])
            else:
                self.text_preview.setPlainText(item["content"])
            self.preview_stack.setCurrentWidget(self.text_preview)
        dimensions = f"{item['width']} × {item['height']}\n" if item["width"] else ""
        path_text = f"\n路径  {item['path']}" if item["path"] else ""
        self.meta.setText(f"类型  {TYPE_LABELS.get(item['kind'], item['kind'])}\n{dimensions}大小  {human_size(item['file_size'])}\n时间  {item['created_at'].replace('T',' ')}\n来源  {item['source']}{path_text}")
        self.collection_combo.blockSignals(True)
        index = self.collection_combo.findData(item["collection_id"])
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)
        self._set_tags(item["tag_names"] or "", item["tag_colors"] or "")
        self.ai_description.setText(item["ai_description"] or "尚未生成")
        self.ocr_text.setText(item["ocr_text"] or "尚未识别")
        self.notes.blockSignals(True)
        self.notes.setPlainText(item["notes"] or "")
        self.notes.blockSignals(False)

    def _set_tags(self, names: str, colors: str) -> None:
        while self.tags_box.count():
            item = self.tags_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        name_list = names.split(",") if names else []
        color_list = colors.split(",") if colors else []
        for index, name in enumerate(name_list[:4]):
            button = QPushButton(name)
            button.setObjectName("TagChip")
            color = color_list[index] if index < len(color_list) else "#64748b"
            button.setIcon(QIcon(color_dot(color)))
            button.setToolTip("点击移除标签")
            button.clicked.connect(lambda _checked=False, tag=name: self.current_item and self.remove_tag_requested.emit(self.current_item["id"], tag))
            self.tags_box.addWidget(button)
        self.tags_box.addStretch()

    def _collection_changed(self, _index: int) -> None:
        if self.current_item:
            self.collection_changed.emit(self.current_item["id"], self.collection_combo.currentData())

    def eventFilter(self, watched, event) -> bool:
        if watched is self.notes and event.type() == QEvent.Type.FocusOut and self.current_item:
            self.notes_changed.emit(self.current_item["id"], self.notes.toPlainText())
        return super().eventFilter(watched, event)


class MarkdownDialog(QDialog):
    def __init__(self, title: str, content: str, path: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(920, 700)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("Title")
        top.addWidget(heading)
        top.addStretch()
        if path:
            locate = QPushButton("在资源管理器中显示")
            locate.clicked.connect(lambda: os.startfile(str(Path(path).parent)))
            top.addWidget(locate)
            external = QPushButton("外部打开")
            external.clicked.connect(lambda: os.startfile(path))
            top.addWidget(external)
        layout.addLayout(top)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        if path:
            browser.setSearchPaths([str(Path(path).parent)])
        browser.setMarkdown(content)
        layout.addWidget(browser)


class DateDialog(QDialog):
    day_selected = Signal(str)

    def __init__(self, days, parent=None):
        super().__init__(parent)
        self.setWindowTitle("按日期打开")
        self.resize(360, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择一天查看当天保存的全部内容"))
        list_widget = QListWidget()
        for day, amount in days:
            item = QListWidgetItem(f"{friendly_day(day)}    {amount} 项")
            item.setData(Qt.ItemDataRole.UserRole, day)
            list_widget.addItem(item)
        list_widget.itemActivated.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        list_widget.itemClicked.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(list_widget)

    def _choose(self, day: str) -> None:
        self.day_selected.emit(day)
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("ClipSave 设置")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        heading = QLabel("常规")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self.close_to_tray = QComboBox()
        self.close_to_tray.addItem("关闭窗口时最小化到托盘", True)
        self.close_to_tray.addItem("关闭窗口时退出", False)
        self.close_to_tray.setCurrentIndex(0 if settings.get("close_to_tray", True) else 1)
        layout.addWidget(self.close_to_tray)
        layout.addWidget(QLabel("全局唤醒快捷键：Ctrl + Alt + V"))
        layout.addSpacing(14)
        from .constants import DATA_DIR, LIBRARY_DIR

        storage_heading = QLabel("本地存储")
        storage_heading.setObjectName("SectionTitle")
        layout.addWidget(storage_heading)
        storage = QLabel(f"剪贴板文件：{LIBRARY_DIR}\n数据库和设置：{DATA_DIR}\n所有自动捕获内容仅写入本机。")
        storage.setWordWrap(True)
        storage.setObjectName("Muted")
        layout.addWidget(storage)
        open_storage = QPushButton("打开本地资料库")
        open_storage.clicked.connect(lambda: os.startfile(str(LIBRARY_DIR)))
        layout.addWidget(open_storage)
        layout.addSpacing(14)
        ai_heading = QLabel("OpenAI-compatible AI 服务（独立的主动功能）")
        ai_heading.setObjectName("SectionTitle")
        layout.addWidget(ai_heading)
        self.base_url = QLineEdit(settings.get("ai_base_url", ""))
        self.base_url.setPlaceholderText("Base URL，例如 https://example.com/v1")
        layout.addWidget(self.base_url)
        self.api_key = QLineEdit(settings.get("ai_api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API Key（仅保存在本机）")
        layout.addWidget(self.api_key)
        self.vision_model = QLineEdit(settings.get("ai_vision_model", ""))
        self.vision_model.setPlaceholderText("视觉模型名称")
        layout.addWidget(self.vision_model)
        self.embedding_model = QLineEdit(settings.get("ai_embedding_model", ""))
        self.embedding_model.setPlaceholderText("向量模型名称（可选）")
        layout.addWidget(self.embedding_model)
        privacy = QLabel("自动剪贴板捕获不会联网；AI 仅在你主动点击生成或语义搜索时使用。")
        privacy.setObjectName("Muted")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        self.settings.data.update({
            "close_to_tray": self.close_to_tray.currentData(),
            "ai_base_url": self.base_url.text().strip(),
            "ai_api_key": self.api_key.text().strip(),
            "ai_vision_model": self.vision_model.text().strip(),
            "ai_embedding_model": self.embedding_model.text().strip(),
        })
        self.settings.save()
        super().accept()
