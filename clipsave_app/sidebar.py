from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .ui_primitives import (
    AutoHideScrollBar,
    IconButton,
    NavButton,
    color_dot,
    dark_theme_active,
)


class Sidebar(QWidget):
    BRAND_AREA_HEIGHT = 54
    EXPANDED_WIDTH = 242
    COLLAPSED_WIDTH = 72
    ANIMATION_DURATION_MS = 190
    # The cached grid compositor can sustain the 120 Hz budget at native DPI.
    # Elapsed-time progress still skips frames when a slower display or DPI
    # configuration cannot finish one in time.
    MAX_ANIMATION_REFRESH_RATE = 120.0
    navigation_requested = Signal(str, object)
    add_collection_requested = Signal()
    add_tag_requested = Signal()
    delete_collection_requested = Signal(int, str)
    delete_tag_requested = Signal(int, str)
    settings_requested = Signal()
    collapsed_changed = Signal(bool)
    width_animation_started = Signal()
    width_animation_progress = Signal(float)
    width_animation_finished = Signal()

    def _nav_button(self, *args, **kwargs) -> NavButton:
        return NavButton(*args, **kwargs)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self.collapsed = False
        self.nav_buttons: dict[str, NavButton] = {}
        self.collection_buttons: list[NavButton] = []
        self.tag_buttons: list[NavButton] = []
        self.collection_delete_buttons: dict[int, IconButton] = {}
        self.tag_delete_buttons: dict[int, IconButton] = {}
        self.collection_rows: dict[int, QWidget] = {}
        self.tag_rows: dict[int, QWidget] = {}
        self._all_tags = []
        self._tags_expanded = False
        self.tags_more_button: NavButton | None = None
        self.footer_buttons: list[NavButton] = []
        self._animation_timer = QTimer(self)
        self._animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._animation_timer.timeout.connect(self._advance_width_animation)
        self._animation_elapsed = QElapsedTimer()
        self._animation_start_progress = 0.0
        self._animation_end_progress = 0.0
        self._animation_run_duration_ms = self.ANIMATION_DURATION_MS
        self._animation_refresh_rate = 60.0
        self._width_animation_active = False
        self._collapse_progress = 0.0
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(10, 72, 10, 12)
        self.layout_root.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.collapse_button = self._nav_button(
            "collapse",
            "panel-left-close",
            "收起侧栏",
            prominent_icon=True,
        )
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_button, 1)
        self.layout_root.addLayout(header)
        self.layout_root.addSpacing(10)

        self.primary_box = QVBoxLayout()
        self.primary_box.setSpacing(3)
        self.layout_root.addLayout(self.primary_box)

        self.classification_scroll = QScrollArea()
        self.classification_scroll.setObjectName("SidebarClassificationScroll")
        self.classification_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.classification_scroll.setWidgetResizable(True)
        self.classification_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.classification_scroll.setVerticalScrollBar(AutoHideScrollBar())
        classification_content = QWidget()
        classification_content.setObjectName("SidebarClassificationContent")
        classification_content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.classification_layout = QVBoxLayout(classification_content)
        self.classification_layout.setContentsMargins(0, 0, 0, 0)
        self.classification_layout.setSpacing(4)

        self.collection_heading = self._section_heading("集合", self.add_collection_requested)
        self.classification_layout.addWidget(self.collection_heading)
        self.collection_box = QVBoxLayout()
        self.collection_box.setSpacing(2)
        self.classification_layout.addLayout(self.collection_box)

        self.tag_heading = self._section_heading("标签", self.add_tag_requested)
        self.classification_layout.addWidget(self.tag_heading)
        self.tag_box = QVBoxLayout()
        self.tag_box.setSpacing(2)
        self.classification_layout.addLayout(self.tag_box)
        self.classification_layout.addStretch(1)
        self.classification_scroll.setWidget(classification_content)
        self.layout_root.addWidget(self.classification_scroll, 1)

        settings = self._nav_button("settings", "settings", "设置")
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
            ("image", "image", "图片", counts.get("image", 0)),
            ("text", "text", "文字", counts.get("text", 0)),
            ("markdown", "markdown", "Markdown", counts.get("markdown", 0)),
            ("date", "calendar", "按日期打开", None),
        ]
        for key, glyph, label, count in entries:
            button = self._nav_button(key, glyph, label, count)
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda selected, k=key: self.navigation_requested.emit(k, None))
            self.primary_box.addWidget(button)
            self.nav_buttons[key] = button
        self.set_active(getattr(self, "active_key", ""))

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def set_collections(self, collections) -> None:
        self._clear_layout(self.collection_box)
        self.collection_buttons = []
        self.collection_delete_buttons = {}
        self.collection_rows = {}
        for row in collections:
            button = self._nav_button(
                f"collection:{row['id']}",
                "folder",
                row["name"],
                row["amount"],
            )
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda _key, ident=row["id"]: self.navigation_requested.emit("collection", ident))
            delete_button = IconButton("delete", f"删除集合 {row['name']}")
            delete_button.setVisible(not self.collapsed)
            delete_button.clicked.connect(
                lambda _checked=False, ident=row["id"], name=row["name"]: self.delete_collection_requested.emit(
                    ident, name
                )
            )
            item_row = self._classification_row(button, delete_button)
            self.collection_box.addWidget(item_row)
            self.collection_buttons.append(button)
            self.collection_delete_buttons[row["id"]] = delete_button
            self.collection_rows[row["id"]] = item_row
        self.set_active(getattr(self, "active_key", ""))

    def set_tags(self, tags) -> None:
        self._all_tags = list(tags)
        if len(self._all_tags) <= 8:
            self._tags_expanded = False
        active_key = getattr(self, "active_key", "")
        if active_key.startswith("tag:"):
            active_id = active_key.partition(":")[2]
            if any(str(row["id"]) == active_id for row in self._all_tags[8:]):
                self._tags_expanded = True
        self._render_tags()

    def _render_tags(self) -> None:
        self._clear_layout(self.tag_box)
        self.tag_buttons = []
        self.tag_delete_buttons = {}
        self.tag_rows = {}
        visible_tags = self._all_tags if self._tags_expanded else self._all_tags[:8]
        for row in visible_tags:
            button = self._nav_button(
                f"tag:{row['id']}",
                "tag",
                row["name"],
                row["amount"],
            )
            button.set_custom_icon(QIcon(color_dot(row["color"])))
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda _key, ident=row["id"]: self.navigation_requested.emit("tag", ident))
            delete_button = IconButton("delete", f"删除标签 {row['name']}")
            delete_button.setVisible(not self.collapsed)
            delete_button.clicked.connect(
                lambda _checked=False, ident=row["id"], name=row["name"]: self.delete_tag_requested.emit(
                    ident, name
                )
            )
            item_row = self._classification_row(button, delete_button)
            self.tag_box.addWidget(item_row)
            self.tag_buttons.append(button)
            self.tag_delete_buttons[row["id"]] = delete_button
            self.tag_rows[row["id"]] = item_row
        self.tags_more_button = None
        if len(self._all_tags) > 8:
            if self._tags_expanded:
                label = "收起标签"
                count = None
                glyph = "chevron-up"
            else:
                label = "更多标签"
                count = len(self._all_tags) - 8
                glyph = "ellipsis"
            more = self._nav_button("tags:more", glyph, label, count)
            more.set_collapsed(self.collapsed)
            more.triggered.connect(lambda _key: self._toggle_tags_expanded())
            self.tag_box.addWidget(more)
            self.tag_buttons.append(more)
            self.tags_more_button = more
        self.set_active(getattr(self, "active_key", ""))

    def _toggle_tags_expanded(self) -> None:
        self._tags_expanded = not self._tags_expanded
        self._render_tags()

    def _classification_row(self, button: NavButton, delete_button: IconButton) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0 if self.collapsed else 4, 0)
        row.setSpacing(0)
        row.addWidget(button, 1)
        row.addWidget(delete_button)
        return widget

    def set_active(self, key: str) -> None:
        self.active_key = key
        self.collapse_button.refresh_text()
        for button in [
            *self.nav_buttons.values(),
            *self.collection_buttons,
            *self.tag_buttons,
            *self.footer_buttons,
        ]:
            button.set_active(button.key == key)

    def set_collapsed(self, value: bool, animate: bool = True) -> None:
        value = bool(value)
        if value == self.collapsed:
            if not animate:
                self._animation_timer.stop()
                self._set_collapse_progress(1.0 if value else 0.0)
                self._finish_width_animation()
            return
        self.collapsed = value
        # Expanded content is revealed while it is still clipped by the narrow
        # sidebar. Collapsing content remains laid out until the final frame so
        # child size hints cannot alter the outer width trajectory.
        if not value:
            self._apply_collapsed_content(False)
        start = self._collapse_progress
        end = 1.0 if value else 0.0
        self._animation_timer.stop()
        if animate:
            if not self._width_animation_active:
                self._width_animation_active = True
                self.width_animation_started.emit()
            self._start_width_animation(start, end)
        else:
            self._set_collapse_progress(end)
            self._finish_width_animation()
        self.collapsed_changed.emit(value)

    @property
    def collapse_progress(self) -> float:
        return self._collapse_progress

    @property
    def animation_refresh_rate(self) -> float:
        return self._animation_refresh_rate

    @property
    def animation_frame_interval_ms(self) -> int:
        return self._animation_timer.interval()

    def _display_refresh_rate(self) -> float:
        screen = self.screen()
        refresh_rate = float(screen.refreshRate()) if screen is not None else 60.0
        if refresh_rate < 30.0:
            refresh_rate = 60.0
        return min(self.MAX_ANIMATION_REFRESH_RATE, refresh_rate)

    def _start_width_animation(self, start: float, end: float) -> None:
        self._animation_start_progress = float(start)
        self._animation_end_progress = float(end)
        self._animation_run_duration_ms = self.ANIMATION_DURATION_MS
        self._animation_refresh_rate = min(
            self.MAX_ANIMATION_REFRESH_RATE, self._display_refresh_rate()
        )
        self._animation_timer.setInterval(
            max(1, round(1000.0 / self._animation_refresh_rate))
        )
        self._animation_elapsed.start()
        self._animation_timer.start()

    def _advance_width_animation(self) -> None:
        if not self._width_animation_active:
            self._animation_timer.stop()
            return
        elapsed_ms = self._animation_elapsed.nsecsElapsed() / 1_000_000.0
        fraction = min(1.0, elapsed_ms / self._animation_run_duration_ms)
        progress = self._animation_start_progress + (
            self._animation_end_progress - self._animation_start_progress
        ) * fraction
        self._set_collapse_progress(progress)
        if fraction >= 1.0:
            self._finish_width_animation()

    def _set_collapse_progress(self, progress: float) -> None:
        progress = max(0.0, min(1.0, progress))
        width = round(
            self.EXPANDED_WIDTH
            + (self.COLLAPSED_WIDTH - self.EXPANDED_WIDTH) * progress
        )
        width_changed = self.width() != width or self.minimumWidth() != width
        progress_changed = abs(progress - self._collapse_progress) >= 1e-6
        self._collapse_progress = progress
        if width_changed:
            self.setFixedWidth(width)
        endpoint = progress <= 0.0 or progress >= 1.0
        if progress_changed and (width_changed or endpoint):
            self.width_animation_progress.emit(progress)

    def _apply_collapsed_content(self, value: bool) -> None:
        self.collapse_button.label = "展开侧栏" if value else "收起侧栏"
        self.collapse_button.glyph = "panel-left-open" if value else "panel-left-close"
        self.collapse_button.set_collapsed(value)
        self.collection_heading.setVisible(not value)
        self.tag_heading.setVisible(not value)
        for button in [
            *self.nav_buttons.values(),
            *self.collection_buttons,
            *self.tag_buttons,
            *self.footer_buttons,
        ]:
            button.set_collapsed(value)
        for button in [
            *self.collection_delete_buttons.values(),
            *self.tag_delete_buttons.values(),
        ]:
            button.setVisible(not value)
        for row in [*self.collection_rows.values(), *self.tag_rows.values()]:
            row.layout().setContentsMargins(0, 0, 0 if value else 4, 0)

    def _finish_width_animation(self) -> None:
        self._animation_timer.stop()
        self._set_collapse_progress(1.0 if self.collapsed else 0.0)
        self._apply_collapsed_content(self.collapsed)
        if self._width_animation_active:
            self._width_animation_active = False
            self.width_animation_finished.emit()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)

    def brand_divider_rect(self) -> QRect:
        return QRect(0, self.BRAND_AREA_HEIGHT - 1, self.width(), 1)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(
            self.brand_divider_rect(),
            QColor("#3c3c3c")
            if dark_theme_active()
            else QColor(115, 129, 150, 38),
        )
        painter.end()
