from __future__ import annotations

import datetime as dt
import os
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QPropertyAnimation,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QTableView,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .constants import TYPE_LABELS
from .item_models import AssetItemModel, normalized_thumbnail_path
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


_THUMBNAIL_SIZE = QSize(360, 180)
_THUMBNAIL_CACHE_LIMIT = 256
_THUMBNAIL_WORKERS = 2
_THUMBNAIL_REQUEST_LIMIT = 32


@dataclass(frozen=True, slots=True)
class _ThumbnailCacheKey:
    path: str
    modified_ns: int
    size: int
    changed_ns: int = 0
    content_hash: str = ""


def _thumbnail_cache_key(
    path: Path | str, content_hash: str | None = None
) -> _ThumbnailCacheKey | None:
    normalized = normalized_thumbnail_path(path)
    if normalized is None:
        return None
    try:
        stat = os.stat(normalized)
    except OSError:
        return None
    return _ThumbnailCacheKey(
        normalized,
        stat.st_mtime_ns,
        stat.st_size,
        getattr(stat, "st_ctime_ns", 0),
        content_hash or "",
    )


class _ThumbnailPixmapCache(OrderedDict):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = max(1, limit)
        self._keys_by_path: dict[str, _ThumbnailCacheKey] = {}

    def lookup(self, key: _ThumbnailCacheKey) -> QPixmap | None:
        previous = self._keys_by_path.get(key.path)
        if previous is not None and previous != key:
            self.discard_key(previous)
        self._keys_by_path[key.path] = key
        try:
            pixmap = OrderedDict.__getitem__(self, key)
        except KeyError:
            return None
        OrderedDict.move_to_end(self, key)
        return pixmap

    def __setitem__(self, key, pixmap) -> None:
        if isinstance(key, _ThumbnailCacheKey):
            previous = self._keys_by_path.get(key.path)
            if previous is not None and previous != key:
                self.discard_key(previous)
            self._keys_by_path[key.path] = key
        if OrderedDict.__contains__(self, key):
            OrderedDict.__delitem__(self, key)
        OrderedDict.__setitem__(self, key, pixmap)
        while len(self) > self.limit:
            evicted, _pixmap = OrderedDict.popitem(self, last=False)
            if isinstance(evicted, _ThumbnailCacheKey) and self._keys_by_path.get(evicted.path) == evicted:
                self._keys_by_path.pop(evicted.path, None)

    def discard_key(self, key: _ThumbnailCacheKey) -> None:
        OrderedDict.pop(self, key, None)
        if self._keys_by_path.get(key.path) == key:
            self._keys_by_path.pop(key.path, None)

    def invalidate_path(self, path: Path | str) -> None:
        normalized = normalized_thumbnail_path(path)
        if normalized is None:
            return
        key = self._keys_by_path.pop(normalized, None)
        if key is not None:
            OrderedDict.pop(self, key, None)

    def clear(self) -> None:
        OrderedDict.clear(self)
        self._keys_by_path.clear()


_THUMBNAIL_CACHE = _ThumbnailPixmapCache(_THUMBNAIL_CACHE_LIMIT)


def _cached_thumbnail(
    path: Path | str, content_hash: str | None = None
) -> tuple[_ThumbnailCacheKey | None, QPixmap | None, bool]:
    key = _thumbnail_cache_key(path, content_hash)
    if key is None:
        _THUMBNAIL_CACHE.invalidate_path(path)
        return None, None, False
    pixmap = _THUMBNAIL_CACHE.lookup(key)
    return key, pixmap, pixmap is not None


def thumbnail_pixmap(path: Path, content_hash: str | None = None) -> QPixmap:
    _key, pixmap, cached = _cached_thumbnail(path, content_hash)
    if cached:
        return pixmap
    return QPixmap()


def _decode_thumbnail_image(key: _ThumbnailCacheKey) -> QImage:
    reader = QImageReader(key.path)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        reader.setScaledSize(source_size.scaled(_THUMBNAIL_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if not image.isNull() and (image.width() > _THUMBNAIL_SIZE.width() or image.height() > _THUMBNAIL_SIZE.height()):
        image = image.scaled(
            _THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


def _cache_decoded_thumbnail(key: _ThumbnailCacheKey, image: QImage) -> QPixmap | None:
    if _thumbnail_cache_key(key.path, key.content_hash) != key:
        _THUMBNAIL_CACHE.discard_key(key)
        return None
    if image.isNull():
        _THUMBNAIL_CACHE.discard_key(key)
        return None
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        _THUMBNAIL_CACHE.discard_key(key)
        return None
    _THUMBNAIL_CACHE[key] = pixmap
    return pixmap


class _ThumbnailDecodeSignals(QObject):
    finished = Signal(int, object, int, object)


class _ThumbnailDecodeTask(QRunnable):
    def __init__(self, request_id: int, key: _ThumbnailCacheKey, generation: int):
        super().__init__()
        self.request_id = request_id
        self.key = key
        self.generation = generation
        self.signals = _ThumbnailDecodeSignals()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        if self.cancelled:
            self.signals.finished.emit(self.request_id, self.key, self.generation, QImage())
            return
        try:
            image = _decode_thumbnail_image(self.key)
        except Exception:
            image = QImage()
        self.signals.finished.emit(self.request_id, self.key, self.generation, image)


class _ThumbnailDecodeQueue(QObject):
    decoded = Signal(object, object, int)
    capacity_available = Signal()

    def __init__(
        self,
        parent=None,
        *,
        max_workers: int = _THUMBNAIL_WORKERS,
        max_requests: int = _THUMBNAIL_REQUEST_LIMIT,
    ):
        super().__init__(parent)
        self.max_workers = max(1, max_workers)
        self.max_active = self.max_workers * 2
        self.max_requests = max(self.max_workers, max_requests)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(self.max_active)
        self._queued: deque[tuple[_ThumbnailCacheKey, int]] = deque()
        self._scheduled: set[tuple[int, _ThumbnailCacheKey]] = set()
        self._active: dict[int, tuple[_ThumbnailDecodeTask, tuple[int, _ThumbnailCacheKey]]] = {}
        self._next_request_id = 1
        self._closed = False
        self._paused = False

    @property
    def pending_count(self) -> int:
        return len(self._scheduled)

    @property
    def queued_count(self) -> int:
        return len(self._queued)

    def request(self, key: _ThumbnailCacheKey, generation: int) -> bool:
        if self._closed or self._paused:
            return False
        token = (generation, key)
        if token in self._scheduled:
            return True
        if len(self._scheduled) >= self.max_requests:
            return False
        self._scheduled.add(token)
        self._queued.append((key, generation))
        self._pump()
        return True

    def cancel_queued(self) -> None:
        while self._queued:
            key, generation = self._queued.popleft()
            self._scheduled.discard((generation, key))
        for task, _token in self._active.values():
            task.cancel()

    def close(self, timeout_ms: int = 2000) -> bool:
        self._closed = True
        self._paused = True
        self.cancel_queued()
        self._pool.clear()
        return self._pool.waitForDone(timeout_ms)

    def pause_and_wait(self, timeout_ms: int = 2000) -> bool:
        self._paused = True
        self.cancel_queued()
        self._pool.clear()
        return self._pool.waitForDone(timeout_ms)

    def resume(self) -> None:
        if self._closed:
            return
        self._paused = False
        self._pump()
        self.capacity_available.emit()

    def _pump(self) -> None:
        active_current = sum(
            1 for task, _token in self._active.values() if not task.cancelled
        )
        while (
            not self._closed
            and self._queued
            and active_current < self.max_workers
            and len(self._active) < self.max_active
        ):
            key, generation = self._queued.popleft()
            request_id = self._next_request_id
            self._next_request_id += 1
            token = (generation, key)
            task = _ThumbnailDecodeTask(request_id, key, generation)
            task.signals.finished.connect(self._job_finished, Qt.ConnectionType.QueuedConnection)
            self._active[request_id] = (task, token)
            self._pool.start(task)
            active_current += 1

    @Slot(int, object, int, object)
    def _job_finished(self, request_id: int, key: _ThumbnailCacheKey, generation: int, image: QImage) -> None:
        active = self._active.pop(request_id, None)
        if active is None:
            return
        self._scheduled.discard(active[1])
        if not self._closed and not active[0].cancelled:
            self.decoded.emit(key, image, generation)
        self._pump()
        if not self._paused and not active[0].cancelled:
            self.capacity_available.emit()


def lucide_icon(name: str, color: str = "#354052", size: int = 20, fill: str = "none") -> QIcon:
    icon = QIcon()
    for pixel_size in sorted({size, size * 2, size * 3}):
        svg = _render_icon(name, pixel_size, stroke=color, fill=fill, stroke_width="1.8")
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pixmap = QPixmap(pixel_size, pixel_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


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
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class ResizeHandle(QWidget):
    def __init__(self, edges: Qt.Edge, cursor: Qt.CursorShape, parent=None):
        super().__init__(parent)
        self.edges = edges
        self.setCursor(cursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemResize(self.edges):
                event.accept()
                return
        super().mousePressEvent(event)


class BrandLabel(QWidget):
    def __init__(self, text: str, color: str, vertical_scale: float = 0.8, parent=None):
        super().__init__(parent)
        self.text = text
        self.color = QColor(color)
        self.vertical_scale = vertical_scale
        self.setObjectName("BrandTitle")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event) -> None:
        source = QPixmap(self.size())
        source.fill(Qt.GlobalColor.transparent)
        source_painter = QPainter(source)
        source_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        source_painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = QFont("Segoe UI")
        font.setPixelSize(30)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 90)
        source_painter.setFont(font)
        source_painter.setPen(self.color)
        source_painter.drawText(source.rect(), Qt.AlignmentFlag.AlignCenter, self.text)
        source_painter.end()

        target_height = max(1, round(self.height() * self.vertical_scale))
        compressed = source.scaled(
            self.width(),
            target_height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(self)
        painter.drawPixmap(0, (self.height() - target_height) // 2, compressed)
        painter.end()


class CaptureStatusButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CaptureStatus")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(28, 28)
        self.setAccessibleName("本地自动捕获状态")
        self.active = True
        self.set_active(True)

    def set_active(self, active: bool) -> None:
        self.active = active
        color = "#20a464" if active else "#d44c4c"
        self.setIcon(QIcon(color_dot(color, 10)))
        self.setIconSize(QSize(10, 10))
        self.setToolTip("本地自动捕获已开启" if active else "本地自动捕获已暂停")


class AutoHideScrollBar(QScrollBar):
    def __init__(self, orientation=Qt.Orientation.Vertical, parent=None):
        super().__init__(orientation, parent)
        self.setObjectName("AutoHideScrollBar")
        self.active = False
        self.setProperty("active", False)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(1000)
        self.hide_timer.timeout.connect(lambda: self.set_active(False))
        self.valueChanged.connect(lambda _value: self.reveal_temporarily())
        self.sliderPressed.connect(self._slider_pressed)
        self.sliderReleased.connect(self.reveal_temporarily)

    def set_active(self, active: bool) -> None:
        visible = active and self.maximum() > self.minimum()
        self.active = visible
        self.setProperty("active", visible)
        self.update()

    def paintEvent(self, event) -> None:
        if self.active:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(244, 246, 249))
        painter.end()

    def reveal_temporarily(self) -> None:
        self.set_active(True)
        self.hide_timer.start()

    def _slider_pressed(self) -> None:
        self.hide_timer.stop()
        self.set_active(True)


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
        label = "还原" if maximized else "最大化"
        self.maximize_button.setToolTip(label)
        self.maximize_button.setAccessibleName(label)

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


class DraggableBar(QFrame):
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            window.showNormal() if window.isMaximized() else window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DialogTitleBar(DraggableBar):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("DialogTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(46)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 6, 0)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(heading)
        layout.addStretch(1)
        self.close_button = IconButton("close", "关闭")
        layout.addWidget(self.close_button)


class NavButton(QPushButton):
    triggered = Signal(str)

    def __init__(self, key: str, glyph: str, label: str, count: int | None = None, parent=None):
        super().__init__(parent)
        self.key = key
        self.glyph = GLYPHS.get(glyph, glyph)
        self.label = label
        self.count = count
        self.collapsed = False
        self.custom_icon: QIcon | None = None
        self.setObjectName("NavButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(label)
        self.clicked.connect(lambda: self.triggered.emit(self.key))
        self.refresh_text()

    def refresh_text(self) -> None:
        if self.collapsed:
            self.setText("")
            self.setToolTip(self.label)
            self.setStyleSheet("text-align:left;")
        else:
            gap = "    "
            suffix = f"{gap}{self.count:,}" if self.count is not None else ""
            self.setText(f"{gap}{self.label}{suffix}")
            self.setToolTip("")
            self.setStyleSheet("text-align:left;")
        self.setIcon(
            self.custom_icon
            or lucide_icon(self.glyph, "#2f6fca" if self.property("active") else "#4d596b")
        )
        self.setIconSize(QSize(20, 20))

    def set_custom_icon(self, icon: QIcon) -> None:
        self.custom_icon = icon
        self.refresh_text()

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
        self.animation = QPropertyAnimation(self, b"maximumWidth", self)
        self.animation.setDuration(190)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._finish_width_animation)
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(10, 72, 10, 12)
        self.layout_root.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.collapse_button = NavButton("collapse", "panel-left-close", "收起侧栏")
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_button, 1)
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
        for row in collections:
            button = NavButton(f"collection:{row['id']}", "folder", row["name"], row["amount"])
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda _key, ident=row["id"]: self.navigation_requested.emit("collection", ident))
            self.collection_box.addWidget(button)
            self.collection_buttons.append(button)
        self.set_active(getattr(self, "active_key", ""))

    def set_tags(self, tags) -> None:
        self._clear_layout(self.tag_box)
        self.tag_buttons = []
        for row in tags[:8]:
            button = NavButton(f"tag:{row['id']}", "tag", row["name"], row["amount"])
            button.set_custom_icon(QIcon(color_dot(row["color"])))
            button.set_collapsed(self.collapsed)
            button.triggered.connect(lambda _key, ident=row["id"]: self.navigation_requested.emit("tag", ident))
            self.tag_box.addWidget(button)
            self.tag_buttons.append(button)
        self.set_active(getattr(self, "active_key", ""))

    def set_active(self, key: str) -> None:
        self.active_key = key
        for button in [
            *self.nav_buttons.values(),
            *self.collection_buttons,
            *self.tag_buttons,
            *self.footer_buttons,
        ]:
            button.set_active(button.key == key)

    def set_collapsed(self, value: bool, animate: bool = True) -> None:
        self.collapsed = value
        self.collapse_button.label = "展开侧栏" if value else "收起侧栏"
        self.collapse_button.glyph = "panel-left-open" if value else "panel-left-close"
        self.collapse_button.set_collapsed(value)
        self.collection_heading.setVisible(not value)
        self.tag_heading.setVisible(not value)
        for button in [*self.nav_buttons.values(), *self.collection_buttons, *self.tag_buttons, *self.footer_buttons]:
            button.set_collapsed(value)
        start = self.width()
        end = 72 if value else 242
        self.animation.stop()
        self.setMinimumWidth(72)
        if animate:
            self.animation.setStartValue(start)
            self.animation.setEndValue(end)
            self.animation.start()
        else:
            self.setMaximumWidth(end)
            self._finish_width_animation()
        self.collapsed_changed.emit(value)

    def _finish_width_animation(self) -> None:
        self.setMaximumWidth(72 if self.collapsed else 242)
        self.setMinimumWidth(72 if self.collapsed else 200)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)


class AssetGridDelegate(QStyledItemDelegate):
    card_height = 252

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.favorite_on = lucide_icon("star", "#f4a100", 18, "#f4a100").pixmap(18, 18)
        self.favorite_off = lucide_icon("star", "#f4a100", 18).pixmap(18, 18)

    def sizeHint(self, option, index) -> QSize:
        return self.view.gridSize()

    @staticmethod
    def card_rect(rect: QRect) -> QRect:
        return rect.adjusted(6, 6, -6, -6)

    def favorite_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        return QRect(card.right() - 31, card.top() + 9, 24, 24)

    def paint(self, painter, option, index) -> None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        card = self.card_rect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#2f7df6") if selected else QColor("#dfe4eb"), 1))
        painter.setBrush(QColor("#eef4ff") if selected else QColor("#ffffff"))
        painter.drawRoundedRect(card, 6, 6)

        muted = QColor("#7a8699")
        primary = QColor("#172033")
        left = card.left() + 10
        right = card.right() - 10
        kind = TYPE_LABELS.get(record["kind"], record["kind"])
        painter.setPen(muted)
        painter.drawText(QRect(left, card.top() + 10, 90, 24), Qt.AlignmentFlag.AlignVCenter, kind)
        created_at = str(record["created_at"])
        time_text = created_at[11:16] if len(created_at) >= 16 else ""
        painter.drawText(
            QRect(right - 92, card.top() + 10, 54, 24),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            time_text,
        )
        favorite = self.favorite_on if record["favorite"] else self.favorite_off
        favorite_target = self.favorite_rect(option.rect)
        painter.drawPixmap(
            favorite_target.left() + 3,
            favorite_target.top() + 3,
            favorite,
        )

        preview = QRect(left, card.top() + 42, card.width() - 20, 150)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#edf1f7") if record["kind"] == "image" else QColor("#f7f9fc"))
        painter.drawRoundedRect(preview, 5, 5)
        path = record["path"] if record["kind"] == "image" else None
        if path and self.view.preview_loading_enabled and self.view.isVisible():
            try:
                content_hash = record["content_hash"]
            except (KeyError, IndexError):
                content_hash = None
            pixmap = self.view.thumbnail_for_index(index, path, content_hash)
            if pixmap is not None and not pixmap.isNull():
                scaled = pixmap.scaled(
                    preview.size() - QSize(12, 12),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                target = QRect(0, 0, scaled.width(), scaled.height())
                target.moveCenter(preview.center())
                painter.drawPixmap(target, scaled)
        elif record["kind"] != "image":
            content = str(record["content"] or "").strip() or str(record["title"])
            painter.setPen(QColor("#354052"))
            painter.drawText(
                preview.adjusted(10, 9, -10, -9),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                content[:330],
            )

        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(primary)
        title = painter.fontMetrics().elidedText(str(record["title"]), Qt.TextElideMode.ElideRight, card.width() - 20)
        painter.drawText(QRect(left, card.top() + 200, card.width() - 20, 24), Qt.AlignmentFlag.AlignVCenter, title)
        painter.setFont(option.font)
        painter.setPen(muted)
        if record["kind"] == "image" and record["width"]:
            detail = f"{record['width']} × {record['height']}   {human_size(record['file_size'])}"
        else:
            detail = f"{kind}   {human_size(record['file_size'])}"
        painter.drawText(QRect(left, card.top() + 225, card.width() - 20, 20), Qt.AlignmentFlag.AlignVCenter, detail)
        painter.restore()


class AssetGrid(QListView):
    item_selected = Signal(int)
    selection_cleared = Signal()
    item_activated = Signal(int)
    favorite_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBar(AutoHideScrollBar())
        self.setViewportMargins(14, 48, 14, 18)
        self.setSpacing(0)
        self.setStyleSheet("QListView { background: rgb(244,246,249); border: 0; outline: 0; }")
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self.delegate = AssetGridDelegate(self)
        self.setItemDelegate(self.delegate)
        self.items = self._asset_model.items
        self.selected_id: int | None = None
        self.columns = 0
        self.preview_loading_enabled = True
        self.rebuild_pending = False
        self._thumbnail_generation = 0
        self._thumbnail_loader = _ThumbnailDecodeQueue(self)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self._thumbnail_loader.capacity_available.connect(self.viewport().update)
        self._thumbnail_refresh_timer = QTimer(self)
        self._thumbnail_refresh_timer.setSingleShot(True)
        self._thumbnail_refresh_timer.setInterval(50)
        self._thumbnail_refresh_timer.timeout.connect(self._refresh_thumbnail_generation)
        self._favorite_press_row = -1
        self._suppress_selection_signal = False
        self.selectionModel().currentChanged.connect(self._index_selected)
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        self.doubleClicked.connect(self._index_activated)
        self.verticalScrollBar().valueChanged.connect(self._thumbnail_viewport_changed)
        self._update_grid_size()

    def set_items(self, items, selected_id: int | None = None) -> None:
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.selected_id = selected_id
        self._asset_model.set_items(items)
        self.items = self._asset_model.items
        self.rebuild_pending = not self.isVisible()
        self._restore_selection()
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_grid_size()
        self._thumbnail_refresh_timer.start()

    def _update_grid_size(self) -> None:
        available = max(210, self.viewport().width())
        columns = max(1, available // 245)
        gap = 12
        card_width = max(210, (available - columns * gap) // columns)
        self.columns = columns
        self.setGridSize(QSize(card_width + gap, AssetGridDelegate.card_height + gap))

    def set_preview_loading_enabled(self, enabled: bool) -> None:
        if enabled == self.preview_loading_enabled:
            return
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.preview_loading_enabled = enabled
        if enabled and self.isVisible():
            self.viewport().update()

    def hideEvent(self, event) -> None:
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.rebuild_pending:
            self.rebuild_pending = False
            self._update_grid_size()
        if self.preview_loading_enabled:
            self.viewport().update()

    def closeEvent(self, event) -> None:
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.close()
        super().closeEvent(event)

    def shutdown_thumbnail_loader(self, timeout_ms: int = 2000) -> bool:
        self._thumbnail_generation += 1
        return self._thumbnail_loader.close(timeout_ms)

    def wait_for_thumbnail_idle(self) -> bool:
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        return self._thumbnail_loader.pause_and_wait()

    def resume_thumbnail_loader(self) -> None:
        self._thumbnail_loader.resume()
        if self.preview_loading_enabled and self.isVisible():
            self.viewport().update()

    def thumbnail_for_index(
        self, index: QModelIndex, path: Path | str, content_hash: str | None = None
    ) -> QPixmap | None:
        key, pixmap, cached = _cached_thumbnail(path, content_hash)
        if cached:
            return pixmap
        if (
            key is None
            or not self.preview_loading_enabled
            or not self.isVisible()
            or not index.isValid()
            or not self.visualRect(index).intersects(self.viewport().rect())
        ):
            return None
        self._thumbnail_loader.request(key, self._thumbnail_generation)
        return None

    @Slot(object, object, int)
    def _thumbnail_decoded(self, key: _ThumbnailCacheKey, image: QImage, generation: int) -> None:
        if generation != self._thumbnail_generation:
            return
        model_generation = self._asset_model.generation
        if not self._asset_model.has_thumbnail_path(key.path, model_generation):
            return
        if _cache_decoded_thumbnail(key, image) is None:
            return
        self._asset_model.notify_thumbnail_changed(key.path, model_generation)

    def _thumbnail_viewport_changed(self, _value: int) -> None:
        self._thumbnail_refresh_timer.start()

    def _refresh_thumbnail_generation(self) -> None:
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._asset_model.rowCount() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor("#7a8699"))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, "没有找到符合条件的内容")
            painter.end()

    def clear_selection(self) -> None:
        self.selected_id = None
        self.clearSelection()
        self.setCurrentIndex(QModelIndex())

    def clear_selected_item(self) -> None:
        self.clear_selection()

    def select_item(self, item_id: int) -> None:
        self.selected_id = item_id
        self.sync_selection_from_selected_id()
        self.item_selected.emit(item_id)

    def sync_selection_from_selected_id(self) -> None:
        row = self._asset_model.row_for_id(self.selected_id)
        self._suppress_selection_signal = True
        try:
            if row < 0:
                self.clearSelection()
                self.setCurrentIndex(QModelIndex())
                return
            index = self._asset_model.index(row, 0)
            self.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
            self.setCurrentIndex(index)
        finally:
            self._suppress_selection_signal = False

    def _restore_selection(self) -> None:
        self.sync_selection_from_selected_id()

    def _index_selected(self, index, _previous=QModelIndex()) -> None:
        if self._suppress_selection_signal:
            return
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        self.selected_id = int(record["id"])
        self.item_selected.emit(self.selected_id)

    def _selection_changed(self, _selected=None, _deselected=None) -> None:
        if self._suppress_selection_signal or self.selectionModel().selectedIndexes():
            return
        self.selected_id = None
        self.selection_cleared.emit()

    def _index_activated(self, index) -> None:
        record = index.data(AssetItemModel.ItemRole)
        if record is not None:
            self.item_activated.emit(int(record["id"]))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid() and self.delegate.favorite_rect(self.visualRect(index)).contains(event.position().toPoint()):
                self._favorite_press_row = index.row()
                event.accept()
                return
        self._favorite_press_row = -1
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._favorite_press_row >= 0 and event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            pressed_row = self._favorite_press_row
            self._favorite_press_row = -1
            if (
                index.isValid()
                and index.row() == pressed_row
                and self.delegate.favorite_rect(self.visualRect(index)).contains(event.position().toPoint())
            ):
                record = index.data(AssetItemModel.ItemRole)
                self.select_item(int(record["id"]))
                self.favorite_requested.emit(int(record["id"]), not bool(record["favorite"]))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and self.delegate.favorite_rect(self.visualRect(index)).contains(event.position().toPoint()):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class AssetTable(QTableView):
    item_selected = Signal(int)
    selection_cleared = Signal()
    item_activated = Signal(int)
    favorite_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBar(AutoHideScrollBar())
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(False)
        self.setStyleSheet(
            "QTableView { background: rgb(244,246,249); alternate-background-color: rgb(240,243,247); "
            "border: 0; gridline-color: transparent; outline: 0; selection-color: #172033; "
            "selection-background-color: rgba(47,125,246,30); }"
            "QTableView::item { padding: 8px 10px; border-bottom: 1px solid rgba(115,129,150,24); }"
            "QTableView::item:selected { color: #172033; background: rgba(47,125,246,30); }"
        )
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(44)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setFixedHeight(40)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.setColumnWidth(1, 90)
        self.setColumnWidth(2, 170)
        self.setColumnWidth(3, 160)
        self.setColumnWidth(4, 90)
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        self.doubleClicked.connect(self._index_activated)
        self.selected_id: int | None = None
        self._suppress_selection_signal = False

    def set_items(self, items, selected_id: int | None = None) -> None:
        self.selected_id = selected_id
        self._suppress_selection_signal = True
        try:
            self._asset_model.set_items(items)
            self._apply_selected_id_to_view()
        finally:
            self._suppress_selection_signal = False

    def sync_selection_from_selected_id(self) -> None:
        self._suppress_selection_signal = True
        try:
            self._apply_selected_id_to_view()
        finally:
            self._suppress_selection_signal = False

    def _apply_selected_id_to_view(self) -> None:
        selected_row = self._asset_model.row_for_id(self.selected_id)
        if selected_row >= 0:
            self.selectRow(selected_row)
        else:
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())

    def clear_selected_item(self) -> None:
        self.selected_id = None
        self.clearSelection()
        self.setCurrentIndex(QModelIndex())

    def clear_selection(self) -> None:
        self.clear_selected_item()

    def _selection_changed(self, _selected=None, _deselected=None) -> None:
        if self._suppress_selection_signal:
            return
        rows = self.selectionModel().selectedRows()
        if rows:
            record = self._asset_model.item(rows[0].row())
            self.selected_id = int(record["id"])
            self.item_selected.emit(self.selected_id)
        else:
            self.selected_id = None
            self.selection_cleared.emit()

    def _index_activated(self, index) -> None:
        record = self._asset_model.item(index.row())
        if record is not None:
            self.item_activated.emit(int(record["id"]))


class _SafeMarkdownBrowser(QTextBrowser):
    _ALLOWED_RESOURCE_SCHEMES = {"qrc"}

    def loadResource(self, resource_type: int, name: QUrl):
        if name.isRelative() or name.isLocalFile() or name.scheme().lower() not in self._ALLOWED_RESOURCE_SCHEMES:
            return QByteArray()
        return super().loadResource(resource_type, name)


MAX_RICH_MARKDOWN_BYTES = 2 * 1024 * 1024


def _set_markdown_content(browser: QTextBrowser, content: str) -> None:
    if len(content.encode("utf-8")) > MAX_RICH_MARKDOWN_BYTES:
        browser.setPlainText(content)
    else:
        browser.setMarkdown(content)


def _startfile_or_warn(parent: QWidget, path: Path | str) -> None:
    try:
        os.startfile(str(path))
    except OSError as exc:
        QMessageBox.warning(parent, "Open failed", f"Could not open the requested location.\n\n{exc}")


class DetailPanel(QScrollArea):
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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(280)
        self.setMaximumWidth(370)
        self.current_item = None
        self._thumbnail_generation = 0
        self._thumbnail_loader = _ThumbnailDecodeQueue(self)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self.content_widget = QWidget()
        self.content_widget.setObjectName("DetailPanelContent")
        layout = QVBoxLayout(self.content_widget)
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
        self.text_preview = _SafeMarkdownBrowser()
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
        self._loaded_notes = ""
        self._note_drafts: dict[int, str] = {}
        self._note_draft_bases: dict[int, str] = {}
        layout.addWidget(self.notes)
        actions = QHBoxLayout()
        self.item_action_buttons = []
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
            self.item_action_buttons.append(button)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.setWidget(self.content_widget)
        self.clear_item()

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

    def set_item(self, item) -> bool:
        previous_item_id = self.current_item["id"] if self.current_item else None
        loaded_notes_before_flush = self._loaded_notes
        self.flush_notes()
        if previous_item_id is not None and (
            self.current_item is None or self.current_item["id"] != previous_item_id
        ):
            return False
        if self._loaded_notes != loaded_notes_before_flush:
            return False
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.current_item = item
        self.image_preview.clear()
        self.text_preview.clear()
        self.type_badge.setText(TYPE_LABELS.get(item["kind"], item["kind"]))
        self.title.setText(item["title"])
        if item["kind"] == "image" and item["path"] and Path(item["path"]).exists():
            self.preview_stack.setCurrentWidget(self.image_preview)
            try:
                content_hash = item["content_hash"]
            except (KeyError, IndexError):
                content_hash = None
            key, pixmap, cached = _cached_thumbnail(item["path"], content_hash)
            if cached:
                self._set_image_preview(pixmap)
            elif key is not None:
                self._thumbnail_loader.request(key, self._thumbnail_generation)
        else:
            if item["kind"] == "markdown":
                _set_markdown_content(self.text_preview, item["content"])
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
        is_image = item["kind"] == "image" and bool(item["path"]) and Path(item["path"]).exists()
        self.ai_button.setEnabled(is_image)
        self.ai_button.setText("重新生成" if item["ai_description"] else "生成描述")
        self.ocr_button.setEnabled(is_image)
        self.ocr_button.setText("重新识别" if item["ocr_text"] else "识别文字")
        for button in self.item_action_buttons:
            button.setEnabled(True)
        self.notes.blockSignals(True)
        loaded_notes = item["notes"] or ""
        self.notes.setPlainText(self._note_drafts.get(item["id"], loaded_notes))
        self.notes.blockSignals(False)
        self._loaded_notes = loaded_notes
        return True

    def clear_item(self) -> None:
        self.flush_notes()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.current_item = None
        self.type_badge.setText("详情")
        self.title.setText("选择一项查看详情")
        self.image_preview.clear()
        self.text_preview.clear()
        self.preview_stack.setCurrentWidget(self.text_preview)
        self.meta.clear()
        self._set_tags("", "")
        self.ai_description.setText("尚未生成")
        self.ocr_text.setText("尚未识别")
        self.ai_button.setEnabled(False)
        self.ai_button.setText("生成描述")
        self.ocr_button.setEnabled(False)
        self.ocr_button.setText("识别文字")
        self.collection_combo.blockSignals(True)
        self.collection_combo.setCurrentIndex(0)
        self.collection_combo.blockSignals(False)
        self.notes.blockSignals(True)
        self.notes.clear()
        self.notes.blockSignals(False)
        self._loaded_notes = ""
        for button in self.item_action_buttons:
            button.setEnabled(False)

    def closeEvent(self, event) -> None:
        self._thumbnail_generation += 1
        self._thumbnail_loader.close()
        super().closeEvent(event)

    def shutdown_thumbnail_loader(self, timeout_ms: int = 2000) -> bool:
        self._thumbnail_generation += 1
        return self._thumbnail_loader.close(timeout_ms)

    def wait_for_thumbnail_idle(self) -> bool:
        self._thumbnail_generation += 1
        return self._thumbnail_loader.pause_and_wait()

    def resume_thumbnail_loader(self) -> None:
        self._thumbnail_loader.resume()
        if self.current_item is not None:
            self.set_item(self.current_item)

    @Slot(object, object, int)
    def _thumbnail_decoded(self, key: _ThumbnailCacheKey, image: QImage, generation: int) -> None:
        if generation != self._thumbnail_generation or self.current_item is None:
            return
        if normalized_thumbnail_path(self.current_item["path"]) != key.path:
            return
        pixmap = _cache_decoded_thumbnail(key, image)
        if pixmap is not None:
            self._set_image_preview(pixmap)

    def _set_image_preview(self, pixmap: QPixmap | None) -> None:
        if pixmap is None or pixmap.isNull():
            return
        self.image_preview.setPixmap(
            pixmap.scaled(
                330,
                230,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_ai_busy(self, busy: bool, failed: bool = False) -> None:
        is_image = bool(
            self.current_item
            and self.current_item["kind"] == "image"
            and self.current_item["path"]
            and Path(self.current_item["path"]).exists()
        )
        self.ai_button.setEnabled(is_image and not busy)
        self.ai_button.setText("生成中…" if busy else "重试" if failed else "生成描述")

    def set_ocr_busy(self, busy: bool, failed: bool = False) -> None:
        is_image = bool(
            self.current_item
            and self.current_item["kind"] == "image"
            and self.current_item["path"]
            and Path(self.current_item["path"]).exists()
        )
        self.ocr_button.setEnabled(is_image and not busy)
        self.ocr_button.setText("识别中…" if busy else "重试" if failed else "识别文字")

    def _set_tags(self, names: str, colors: str) -> None:
        while self.tags_box.count():
            item = self.tags_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        name_list = names.split("\x1f") if names else []
        color_list = colors.split("\x1f") if colors else []
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
            self.flush_notes()
        return super().eventFilter(watched, event)

    def flush_notes(self) -> bool:
        if not self.current_item:
            return True
        item_id = self.current_item["id"]
        notes = self.notes.toPlainText()
        if notes == self._loaded_notes:
            self._note_drafts.pop(item_id, None)
            self._note_draft_bases.pop(item_id, None)
            return True
        if notes != self._loaded_notes:
            self._note_draft_bases.setdefault(item_id, self._loaded_notes)
            self._note_drafts[item_id] = notes
            self.notes_changed.emit(item_id, notes)
        return self._note_drafts.get(item_id) != notes

    def mark_notes_saved(self, item_id: int, notes: str) -> None:
        if self.current_item and self.current_item["id"] == item_id and self.notes.toPlainText() == notes:
            self._loaded_notes = notes
        if self._note_drafts.get(item_id) == notes:
            self._note_drafts.pop(item_id, None)
            self._note_draft_bases.pop(item_id, None)

    def pending_note_drafts(self) -> dict[int, str]:
        return dict(self._note_drafts)

    def pending_note_updates(self) -> dict[int, tuple[str, str]]:
        return {
            item_id: (self._note_draft_bases.get(item_id, ""), notes)
            for item_id, notes in self._note_drafts.items()
        }


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
            locate.clicked.connect(lambda: _startfile_or_warn(self, Path(path).parent))
            top.addWidget(locate)
            external = QPushButton("外部打开")
            external.clicked.connect(lambda: _startfile_or_warn(self, path))
            top.addWidget(external)
        layout.addLayout(top)
        browser = _SafeMarkdownBrowser()
        browser.setOpenExternalLinks(False)
        _set_markdown_content(browser, content)
        self.browser = browser
        layout.addWidget(browser)


class DateDialog(QDialog):
    day_selected = Signal(str)

    def __init__(self, days, parent=None):
        super().__init__(parent)
        self.setWindowTitle("按日期打开")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(420, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        title_bar = DialogTitleBar("按日期打开")
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("DialogContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(12)
        description = QLabel("选择一天，查看当天保存的全部内容")
        description.setObjectName("Muted")
        layout.addWidget(description)
        list_widget = QListWidget()
        list_widget.setObjectName("DateList")
        list_widget.setVerticalScrollBar(AutoHideScrollBar())
        list_widget.setSpacing(2)
        for day, amount in days:
            item = QListWidgetItem(f"{friendly_day(day)}    {amount} 项")
            item.setData(Qt.ItemDataRole.UserRole, day)
            item.setSizeHint(QSize(0, 44))
            list_widget.addItem(item)
        list_widget.itemActivated.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        list_widget.itemClicked.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(list_widget)
        root.addWidget(content, 1)

    def _choose(self, day: str) -> None:
        self.day_selected.emit(day)
        self.accept()


class SettingsDialog(QDialog):
    import_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("ClipSave 设置")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(620, 650)
        self.setMinimumSize(480, 400)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        title_bar = DialogTitleBar("设置")
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        scroll = QScrollArea()
        scroll.setObjectName("DialogScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBar(AutoHideScrollBar())
        content = QWidget()
        content.setObjectName("DialogContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(10)
        heading = QLabel("常规")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self.close_to_tray = QComboBox()
        self.close_to_tray.addItem("关闭窗口时最小化到托盘", True)
        self.close_to_tray.addItem("关闭窗口时退出", False)
        self.close_to_tray.setCurrentIndex(0 if settings.get("close_to_tray", True) else 1)
        layout.addWidget(self.close_to_tray)
        hotkey_state = getattr(parent, "global_hotkey_registered", None)
        if hotkey_state is False:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V（注册失败，可能已被占用）"
        elif hotkey_state is True:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V（已启用）"
        else:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V"
        self.hotkey_status = QLabel(hotkey_text)
        layout.addWidget(self.hotkey_status)
        layout.addSpacing(14)
        from .constants import DATA_DIR, LIBRARY_DIR

        storage_heading = QLabel("本地存储")
        storage_heading.setObjectName("SectionTitle")
        layout.addWidget(storage_heading)
        storage = QLabel(f"剪贴板文件：{LIBRARY_DIR}\n数据库和设置：{DATA_DIR}\n所有自动捕获内容仅写入本机。")
        storage.setWordWrap(True)
        storage.setObjectName("Muted")
        layout.addWidget(storage)
        self.import_button = QPushButton("导入文件")
        self.import_button.setObjectName("SettingsAction")
        self.import_button.setIcon(lucide_icon("plus"))
        self.import_button.clicked.connect(self.import_requested.emit)
        layout.addWidget(self.import_button)
        open_storage = QPushButton("打开本地资料库")
        open_storage.setObjectName("SettingsAction")
        open_storage.setIcon(lucide_icon("folder"))
        open_storage.clicked.connect(lambda: _startfile_or_warn(self, LIBRARY_DIR))
        self.open_storage_button = open_storage
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
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("DialogFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 12)
        footer_layout.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer_layout.addWidget(cancel)
        save = QPushButton("保存")
        save.setObjectName("Primary")
        save.clicked.connect(self.accept)
        footer_layout.addWidget(save)
        root.addWidget(footer)

    def accept(self) -> None:
        values = {
            "close_to_tray": self.close_to_tray.currentData(),
            "ai_base_url": self.base_url.text().strip(),
            "ai_api_key": self.api_key.text().strip(),
            "ai_vision_model": self.vision_model.text().strip(),
            "ai_embedding_model": self.embedding_model.text().strip(),
        }
        try:
            self.settings.update(values)
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "设置保存失败", str(exc))
            return
        super().accept()
