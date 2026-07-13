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
    QPoint,
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
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
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
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QStyle,
    QTableView,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .constants import TYPE_LABELS
from .item_models import AssetItemModel, format_local_timestamp, normalized_thumbnail_path
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
        try:
            pixmap = OrderedDict.__getitem__(self, key)
        except KeyError:
            return None
        self._keys_by_path[key.path] = key
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


def lucide_icon(name: str, color: str | None = None, size: int = 20, fill: str = "none") -> QIcon:
    color = color or theme_icon_color()
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


def dark_theme_active() -> bool:
    app = QApplication.instance()
    return bool(app and app.property("darkTheme"))


def theme_icon_color() -> str:
    return "#e6e6e6" if dark_theme_active() else "#354052"


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
    screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    physical_size = max(1, round(size * dpr))
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.setDevicePixelRatio(dpr)
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
        self.glyph = GLYPHS.get(glyph, glyph)
        self.refresh_theme()
        self.setIconSize(QSize(18, 18))
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_glyph(self, glyph: str) -> None:
        self.glyph = GLYPHS.get(glyph, glyph)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.setIcon(lucide_icon(self.glyph, theme_icon_color()))


class FluentComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._arrow_theme: bool | None = None
        self._arrow_icon = QIcon()

    def _refresh_arrow(self) -> None:
        dark = dark_theme_active()
        if dark == self._arrow_theme:
            return
        self._arrow_theme = dark
        self._arrow_icon = lucide_icon(
            "chevron-down",
            "#a7adb7" if dark else "#6f7b8d",
            14,
        )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        self._refresh_arrow()
        painter = QPainter(self)
        self._arrow_icon.paint(
            painter,
            QRect(self.width() - 28, 0, 28, self.height()),
            Qt.AlignmentFlag.AlignCenter,
        )
        painter.end()


class ToggleSwitch(QCheckBox):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("ToggleSwitch")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRect(0, (self.height() - 22) // 2, 40, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        if not self.isEnabled():
            track_color = QColor("#55585e" if dark_theme_active() else "#b7bcc4")
        else:
            track_color = QColor("#2f7df6") if self.isChecked() else QColor("#737b87")
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 11, 11)
        knob_x = track.right() - 18 if self.isChecked() else track.left() + 3
        painter.setBrush(QColor("#9a9da3") if not self.isEnabled() else QColor("#ffffff"))
        painter.drawEllipse(knob_x, track.top() + 3, 16, 16)
        if not self.isEnabled():
            text_color = QColor("#777b82")
        else:
            text_color = QColor("#f2f2f2") if dark_theme_active() else QColor("#172033")
        painter.setPen(text_color)
        painter.drawText(
            self.rect().adjusted(52, 0, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )
        painter.end()


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
        dpr = self.devicePixelRatioF()
        source = QPixmap(
            max(1, round(self.width() * dpr)),
            max(1, round(self.height() * dpr)),
        )
        source.setDevicePixelRatio(dpr)
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
        text_rect = source.rect().adjusted(16, 0, 0, 0)
        source_painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text,
        )
        source_painter.end()

        target_height = max(1, round(self.height() * self.vertical_scale))
        compressed = source.scaled(
            max(1, round(self.width() * dpr)),
            max(1, round(target_height * dpr)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        compressed.setDevicePixelRatio(dpr)
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


class CopyToast(QFrame):
    WIDTH = 268
    HEIGHT = 58
    MARGIN = 20
    DISPLAY_MS = 2300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CopyToast")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("已复制到剪贴板")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 10, 16, 10)
        layout.setSpacing(12)
        icon = QLabel()
        icon.setObjectName("CopyToastIcon")
        icon.setFixedSize(34, 34)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(lucide_icon("check", "#ffffff", 18).pixmap(QSize(18, 18)))
        layout.addWidget(icon)
        message = QLabel("已复制到剪贴板")
        message.setObjectName("CopyToastText")
        layout.addWidget(message, 1)
        self.icon_label = icon
        self.message_label = message

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._motion = QPropertyAnimation(self, b"pos", self)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._animation_finished)
        self._hiding = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self.DISPLAY_MS)
        self._hide_timer.timeout.connect(self._begin_hide)
        self.hide()

    def target_position(self) -> QPoint:
        parent = self.parentWidget()
        if parent is None:
            return QPoint()
        return QPoint(
            max(self.MARGIN, parent.width() - self.width() - self.MARGIN),
            max(self.MARGIN, parent.height() - self.height() - self.MARGIN),
        )

    def reposition(self) -> None:
        self._motion.stop()
        self.move(self.target_position())

    def show_confirmation(self) -> None:
        self._hide_timer.stop()
        self._motion.stop()
        self._fade.stop()
        target = self.target_position()
        first_show = not self.isVisible()
        self._hiding = False
        if first_show:
            self.move(target + QPoint(0, 10))
            self._opacity_effect.setOpacity(0.0)
            self.show()
        else:
            self.move(target)
        self.raise_()
        self._motion.setDuration(170)
        self._motion.setStartValue(self.pos())
        self._motion.setEndValue(target)
        self._fade.setDuration(150)
        self._fade.setStartValue(self._opacity_effect.opacity())
        self._fade.setEndValue(1.0)
        self._motion.start()
        self._fade.start()
        self._hide_timer.start()

    def _begin_hide(self) -> None:
        if not self.isVisible():
            return
        self._motion.stop()
        self._fade.stop()
        target = self.target_position()
        self._hiding = True
        self._motion.setDuration(140)
        self._motion.setStartValue(self.pos())
        self._motion.setEndValue(target + QPoint(0, 7))
        self._fade.setDuration(130)
        self._fade.setStartValue(self._opacity_effect.opacity())
        self._fade.setEndValue(0.0)
        self._motion.start()
        self._fade.start()

    def _animation_finished(self) -> None:
        if self._hiding:
            self.hide()


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
        painter.fillRect(self.rect(), QColor("#202020" if dark_theme_active() else "#f6f6f6"))
        painter.end()

    def reveal_temporarily(self) -> None:
        self.set_active(True)
        self.hide_timer.start()

    def _slider_pressed(self) -> None:
        self.hide_timer.stop()
        self.set_active(True)

    def enterEvent(self, event) -> None:
        self.hide_timer.stop()
        self.set_active(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.reveal_temporarily()


@dataclass
class _WheelRemainder:
    pixel_x: float = 0.0
    pixel_y: float = 0.0
    angle_x: float = 0.0
    angle_y: float = 0.0


def _half_speed_wheel_event(
    event: QWheelEvent, remainder: _WheelRemainder | None = None
) -> QWheelEvent:
    remainder = remainder or _WheelRemainder()
    pixel_delta = event.pixelDelta()
    angle_delta = event.angleDelta()
    pixel_x = pixel_delta.x() / 2 + remainder.pixel_x
    pixel_y = pixel_delta.y() / 2 + remainder.pixel_y
    angle_x = angle_delta.x() / 2 + remainder.angle_x
    angle_y = angle_delta.y() / 2 + remainder.angle_y
    scaled_pixel = QPoint(int(pixel_x), int(pixel_y))
    scaled_angle = QPoint(int(angle_x), int(angle_y))
    remainder.pixel_x = pixel_x - scaled_pixel.x()
    remainder.pixel_y = pixel_y - scaled_pixel.y()
    remainder.angle_x = angle_x - scaled_angle.x()
    remainder.angle_y = angle_y - scaled_angle.y()
    return QWheelEvent(
        event.position(),
        event.globalPosition(),
        scaled_pixel,
        scaled_angle,
        event.buttons(),
        event.modifiers(),
        event.phase(),
        event.inverted(),
        event.source(),
        event.device(),
    )


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
        self.maximize_button.set_glyph("copy" if maximized else "square")
        label = "还原" if maximized else "最大化"
        self.maximize_button.setToolTip(label)
        self.maximize_button.setAccessibleName(label)

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


def _available_dialog_size(widget: QWidget, margin: int = 32) -> QSize:
    screen = widget.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return QSize(1_000_000, 1_000_000)
    available = screen.availableGeometry()
    return QSize(max(1, available.width() - margin), max(1, available.height() - margin))


def _fit_dialog_size(widget: QWidget, preferred: QSize, margin: int = 32) -> QSize:
    available = _available_dialog_size(widget, margin)
    return QSize(min(preferred.width(), available.width()), min(preferred.height(), available.height()))


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


class FluentMessageDialog(QDialog):
    _ICONS = {
        "information": ("info", "#21a8fb"),
        "warning": ("triangle-alert", "#f5a623"),
        "critical": ("circle-x", "#d13438"),
        "question": ("circle-alert", "#21a8fb"),
    }

    def __init__(
        self,
        title: str,
        message: str,
        parent=None,
        *,
        kind: str = "information",
        accept_text: str = "确定",
        cancel_text: str | None = None,
        destructive: bool = False,
        default_accept: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setProperty("messageDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar(title)
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("DialogContent")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 20)
        content_layout.setSpacing(14)

        glyph, color = self._ICONS.get(kind, self._ICONS["information"])
        icon = QLabel()
        icon.setObjectName("MessageIcon")
        icon.setFixedSize(24, 24)
        icon.setPixmap(lucide_icon(glyph, color, 22).pixmap(22, 22))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        content_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        self.message_label = QLabel(message)
        self.message_label.setObjectName("MessageText")
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        message_container = QWidget()
        message_layout = QVBoxLayout(message_container)
        message_layout.setContentsMargins(0, 0, 0, 0)
        message_layout.addWidget(self.message_label)
        message_layout.addStretch(1)

        dialog_width = _fit_dialog_size(self, QSize(480, 200)).width()
        message_width = min(360, max(80, dialog_width - 110))
        text_height = self.message_label.fontMetrics().boundingRect(
            QRect(0, 0, message_width, 10_000),
            Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            message,
        ).height()
        message_height = max(56, min(260, text_height + 8))
        message_scroll = QScrollArea()
        message_scroll.setObjectName("MessageScroll")
        message_scroll.setFrameShape(QFrame.Shape.NoFrame)
        message_scroll.setWidgetResizable(True)
        message_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        message_scroll.setVerticalScrollBar(AutoHideScrollBar())
        message_scroll.setFixedHeight(message_height)
        message_scroll.setWidget(message_container)
        content_layout.addWidget(message_scroll, 1)
        root.addWidget(content, 1)

        footer = QFrame()
        footer.setObjectName("DialogFooter")
        footer.setFixedHeight(58)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 12)
        footer_layout.addStretch(1)
        self.cancel_button = None
        if cancel_text is not None:
            self.cancel_button = QPushButton(cancel_text)
            self.cancel_button.clicked.connect(self.reject)
            footer_layout.addWidget(self.cancel_button)
        self.accept_button = QPushButton(accept_text)
        self.accept_button.setObjectName("Danger" if destructive else "Primary")
        self.accept_button.clicked.connect(self.accept)
        footer_layout.addWidget(self.accept_button)
        root.addWidget(footer)

        default_button = self.accept_button if default_accept else self.cancel_button
        if default_button is not None:
            default_button.setDefault(True)
            default_button.setFocus()
        preferred_size = QSize(480, 46 + 40 + message_height + 58 + 2)
        dialog_size = _fit_dialog_size(self, preferred_size)
        content_height = max(32, dialog_size.height() - 46 - 58 - 42)
        message_scroll.setFixedHeight(min(message_height, content_height))
        self.setFixedSize(dialog_size)


class FluentMessageBox:
    StandardButton = QMessageBox.StandardButton

    @staticmethod
    def _exec(dialog: FluentMessageDialog) -> int:
        try:
            return dialog.exec()
        finally:
            dialog.deleteLater()

    @classmethod
    def question(
        cls,
        parent,
        title: str,
        message: str,
        _buttons=None,
        default_button=QMessageBox.StandardButton.No,
    ):
        destructive = title.startswith("删除")
        result = cls._exec(
            FluentMessageDialog(
                title,
                message,
                parent,
                kind="question",
                accept_text="删除" if destructive else "确定",
                cancel_text="取消",
                destructive=destructive,
                default_accept=default_button == QMessageBox.StandardButton.Yes,
            )
        )
        return (
            QMessageBox.StandardButton.Yes
            if result == QDialog.DialogCode.Accepted
            else QMessageBox.StandardButton.No
        )

    @classmethod
    def information(cls, parent, title: str, message: str, *_args, **_kwargs):
        cls._exec(FluentMessageDialog(title, message, parent, kind="information"))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def warning(cls, parent, title: str, message: str, *_args, **_kwargs):
        cls._exec(FluentMessageDialog(title, message, parent, kind="warning"))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def critical(cls, parent, title: str, message: str, *_args, **_kwargs):
        cls._exec(FluentMessageDialog(title, message, parent, kind="critical"))
        return QMessageBox.StandardButton.Ok


class NavButton(QPushButton):
    triggered = Signal(str)

    def __init__(
        self,
        key: str,
        glyph: str,
        label: str,
        count: int | None = None,
        parent=None,
        prominent_icon: bool = False,
    ):
        super().__init__(parent)
        self.key = key
        self.glyph = GLYPHS.get(glyph, glyph)
        self.label = label
        self.count = count
        self.collapsed = False
        self.custom_icon: QIcon | None = None
        self.prominent_icon = prominent_icon
        self._rendered_icon_key: tuple | None = None
        self.setObjectName("NavButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(label)
        self.setIconSize(QSize(20, 20))
        self.clicked.connect(lambda: self.triggered.emit(self.key))
        self.refresh_text()

    def refresh_text(self) -> None:
        if self.collapsed:
            text = ""
            tooltip = self.label
        else:
            gap = "    "
            suffix = f"{gap}{self.count:,}" if self.count is not None else ""
            text = f"{gap}{self.label}{suffix}"
            tooltip = ""
        if self.text() != text:
            self.setText(text)
        if self.toolTip() != tooltip:
            self.setToolTip(tooltip)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        if self.custom_icon is not None:
            icon_key = ("custom", self.custom_icon.cacheKey())
            icon = self.custom_icon
        else:
            inactive = (
                theme_icon_color()
                if self.prominent_icon
                else "#c6ccd5" if dark_theme_active() else "#4d596b"
            )
            color = "#64b5f6" if self.property("active") else inactive
            icon_key = ("lucide", self.glyph, color)
            icon = None
        if icon_key == self._rendered_icon_key:
            return
        self._rendered_icon_key = icon_key
        if icon is None:
            icon = lucide_icon(self.glyph, color)
        self.setIcon(icon)

    def set_custom_icon(self, icon: QIcon) -> None:
        self.custom_icon = icon
        self._rendered_icon_key = None
        self.refresh_text()

    def set_collapsed(self, value: bool) -> None:
        if self.collapsed == value:
            self._refresh_icon()
            return
        self.collapsed = value
        self.refresh_text()

    def set_active(self, value: bool) -> None:
        changed = bool(self.property("active")) != value
        if changed:
            self.setProperty("active", value)
        self.refresh_text()
        if changed:
            self.style().unpolish(self)
            self.style().polish(self)


class Sidebar(QWidget):
    BRAND_AREA_HEIGHT = 54
    navigation_requested = Signal(str, object)
    add_collection_requested = Signal()
    add_tag_requested = Signal()
    delete_collection_requested = Signal(int, str)
    delete_tag_requested = Signal(int, str)
    settings_requested = Signal()
    collapsed_changed = Signal(bool)
    width_animation_started = Signal()
    width_animation_finished = Signal()

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
        self.collection_delete_buttons: dict[int, IconButton] = {}
        self.tag_delete_buttons: dict[int, IconButton] = {}
        self.collection_rows: dict[int, QWidget] = {}
        self.tag_rows: dict[int, QWidget] = {}
        self._all_tags = []
        self._tags_expanded = False
        self.tags_more_button: NavButton | None = None
        self.footer_buttons: list[NavButton] = []
        self.animation = QPropertyAnimation(self, b"maximumWidth", self)
        self.animation.setDuration(190)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._finish_width_animation)
        self._width_animation_active = False
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(10, 72, 10, 12)
        self.layout_root.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.collapse_button = NavButton(
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
            ("image", "image", "图片", counts.get("image", 0)),
            ("text", "text", "文字", counts.get("text", 0)),
            ("markdown", "markdown", "Markdown", counts.get("markdown", 0)),
            ("date", "calendar", "按日期打开", None),
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
        self.collection_delete_buttons = {}
        self.collection_rows = {}
        for row in collections:
            button = NavButton(f"collection:{row['id']}", "folder", row["name"], row["amount"])
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
            button = NavButton(f"tag:{row['id']}", "tag", row["name"], row["amount"])
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
            more = NavButton("tags:more", glyph, label, count)
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
        self.collapsed = value
        self.collapse_button.label = "展开侧栏" if value else "收起侧栏"
        self.collapse_button.glyph = "panel-left-open" if value else "panel-left-close"
        self.collapse_button.set_collapsed(value)
        self.collection_heading.setVisible(not value)
        self.tag_heading.setVisible(not value)
        for button in [*self.nav_buttons.values(), *self.collection_buttons, *self.tag_buttons, *self.footer_buttons]:
            button.set_collapsed(value)
        for button in [*self.collection_delete_buttons.values(), *self.tag_delete_buttons.values()]:
            button.setVisible(not value)
        for row in [*self.collection_rows.values(), *self.tag_rows.values()]:
            row.layout().setContentsMargins(0, 0, 0 if value else 4, 0)
        start = self.width()
        end = 72 if value else 242
        self.animation.stop()
        self.setMinimumWidth(72)
        if animate:
            self._width_animation_active = True
            self.width_animation_started.emit()
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
        dark = dark_theme_active()
        painter.setPen(QPen(QColor("#4da3ff") if selected else QColor("#4a4a4a" if dark else "#dfe4eb"), 1))
        painter.setBrush(QColor("#26384d") if selected and dark else QColor("#eef4ff") if selected else QColor("#292929" if dark else "#ffffff"))
        painter.drawRoundedRect(card, 6, 6)

        muted = QColor("#a7adb7" if dark else "#7a8699")
        primary = QColor("#f2f2f2" if dark else "#172033")
        left = card.left() + 10
        right = card.right() - 10
        kind = TYPE_LABELS.get(record["kind"], record["kind"])
        painter.setPen(muted)
        painter.drawText(QRect(left, card.top() + 10, 90, 24), Qt.AlignmentFlag.AlignVCenter, kind)
        created_at = format_local_timestamp(record["created_at"])
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
        painter.setBrush(
            QColor("#303030" if record["kind"] == "image" else "#262626")
            if dark
            else QColor("#edf1f7") if record["kind"] == "image" else QColor("#f7f9fc")
        )
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
            painter.setPen(QColor("#dedede" if dark else "#354052"))
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
        self.setObjectName("AssetGrid")
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self.delegate = AssetGridDelegate(self)
        self.setItemDelegate(self.delegate)
        self.items = self._asset_model.items
        self.selected_id: int | None = None
        self.columns = 0
        self.preview_loading_enabled = True
        self.rebuild_pending = False
        self._layout_updates_suspended = False
        self._layout_update_pending = False
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
        self._wheel_remainder = _WheelRemainder()
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
        if self._layout_updates_suspended:
            self._layout_update_pending = True
            return
        self._update_grid_size()
        self._thumbnail_refresh_timer.start()

    def wheelEvent(self, event) -> None:
        scaled_event = _half_speed_wheel_event(event, self._wheel_remainder)
        super().wheelEvent(scaled_event)
        event.setAccepted(scaled_event.isAccepted())

    def set_layout_updates_suspended(self, suspended: bool) -> None:
        if suspended == self._layout_updates_suspended:
            return
        self._layout_updates_suspended = suspended
        if not suspended and self._layout_update_pending:
            self._layout_update_pending = False
            self._update_grid_size()
            self._thumbnail_refresh_timer.start()
            self.viewport().update()

    def _update_grid_size(self) -> None:
        available = max(210, self.viewport().width())
        columns = max(1, available // 245)
        gap = 12
        layout_width = max(210, available - 1)
        card_width = max(210, (layout_width - columns * gap) // columns)
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
            painter.setPen(QColor("#a7adb7" if dark_theme_active() else "#7a8699"))
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
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and index.column() == 0
            and self._favorite_delegate.favorite_rect(self.visualRect(index)).contains(
                event.position().toPoint()
            )
        ):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and self.delegate.favorite_rect(self.visualRect(index)).contains(event.position().toPoint()):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _AssetTableFavoriteDelegate(QStyledItemDelegate):
    @staticmethod
    def favorite_rect(cell: QRect) -> QRect:
        return QRect(cell.right() - 34, cell.top(), 34, cell.height())

    def paint(self, painter: QPainter, option, index) -> None:
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        base_option = QStyleOptionViewItem(option)
        self.initStyleOption(base_option, index)
        title = base_option.text
        base_option.text = ""
        widget = base_option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            base_option,
            painter,
            widget,
        )
        painter.save()
        selected = bool(base_option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(
            base_option.palette.highlightedText().color()
            if selected
            else base_option.palette.text().color()
        )
        text_rect = base_option.rect.adjusted(8, 0, -38, 0)
        title = base_option.fontMetrics.elidedText(
            title, Qt.TextElideMode.ElideRight, max(0, text_rect.width())
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, title)
        favorite = bool(index.data(AssetItemModel.FavoriteRole))
        painter.setPen(QColor("#21a8fb") if favorite else QColor("#8a8a8a"))
        font = QFont(base_option.font)
        font.setPointSizeF(max(10.0, font.pointSizeF() + 1.0))
        painter.setFont(font)
        painter.drawText(
            self.favorite_rect(base_option.rect),
            Qt.AlignmentFlag.AlignCenter,
            "★" if favorite else "☆",
        )
        painter.restore()


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
        self.setObjectName("AssetTable")
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self._favorite_delegate = _AssetTableFavoriteDelegate(self)
        self.setItemDelegateForColumn(0, self._favorite_delegate)
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
        self._wheel_remainder = _WheelRemainder()
        self._favorite_press_row = -1

    def wheelEvent(self, event) -> None:
        scaled_event = _half_speed_wheel_event(event, self._wheel_remainder)
        super().wheelEvent(scaled_event)
        event.setAccepted(scaled_event.isAccepted())

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

    def select_item(self, item_id: int) -> None:
        self.selected_id = item_id
        self.sync_selection_from_selected_id()
        self.item_selected.emit(item_id)

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

    def mousePressEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and index.column() == 0
            and self._favorite_delegate.favorite_rect(self.visualRect(index)).contains(
                event.position().toPoint()
            )
        ):
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
                and index.column() == 0
                and self._favorite_delegate.favorite_rect(self.visualRect(index)).contains(
                    event.position().toPoint()
                )
            ):
                record = index.data(AssetItemModel.ItemRole)
                self.select_item(int(record["id"]))
                self.favorite_requested.emit(int(record["id"]), not bool(record["favorite"]))
            event.accept()
            return
        super().mouseReleaseEvent(event)


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


def _wrap_detail_text(value: object, interval: int = 24) -> str:
    return str(value)


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
        self.setMaximumWidth(340)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.current_item = None
        self._thumbnail_generation = 0
        self._thumbnail_loader = _ThumbnailDecodeQueue(self)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self._image_source_pixmap = QPixmap()
        self._tag_names: list[str] = []
        self._tag_colors: list[str] = []
        self._tags_expanded = False
        self.tags_more_button: QPushButton | None = None
        self.content_widget = QWidget()
        self.content_widget.setObjectName("DetailPanelContent")
        self.content_widget.setMinimumWidth(0)
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
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.preview_stack = QStackedWidget()
        self.image_preview = QLabel()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumHeight(190)
        self.image_preview.setMinimumWidth(0)
        self.image_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.image_preview.setObjectName("DetailPreview")
        self.text_preview = _SafeMarkdownBrowser()
        self.text_preview.setOpenExternalLinks(False)
        self.text_preview.setMinimumHeight(190)
        self.preview_stack.addWidget(self.image_preview)
        self.preview_stack.addWidget(self.text_preview)
        layout.addWidget(self.preview_stack, 1)
        self.meta = QLabel()
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setWordWrap(True)
        self.meta.setMinimumWidth(0)
        self.meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.meta)

        collection_row = QHBoxLayout()
        collection_row.addWidget(QLabel("集合"))
        self.collection_combo = FluentComboBox()
        self.collection_combo.currentIndexChanged.connect(self._collection_changed)
        collection_row.addWidget(self.collection_combo, 1)
        layout.addLayout(collection_row)

        tag_title = QHBoxLayout()
        tag_title.addWidget(QLabel("标签"))
        tag_title.addStretch()
        add_tag = IconButton("add", "添加标签")
        self.add_tag_button = add_tag
        add_tag.clicked.connect(lambda: self.current_item and self.add_tag_requested.emit(self.current_item["id"]))
        tag_title.addWidget(add_tag)
        layout.addLayout(tag_title)
        self.tags_box = QGridLayout()
        self.tags_box.setHorizontalSpacing(4)
        self.tags_box.setVerticalSpacing(4)
        self.tags_box.setColumnStretch(0, 1)
        self.tags_box.setColumnStretch(1, 1)
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
        self.ocr_text.setTextFormat(Qt.TextFormat.PlainText)
        self.ocr_text.setWordWrap(True)
        self.ocr_text.setMinimumWidth(0)
        self.ocr_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        self.ai_description.setTextFormat(Qt.TextFormat.PlainText)
        self.ai_description.setWordWrap(True)
        self.ai_description.setMinimumWidth(0)
        self.ai_description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        if previous_item_id != item["id"]:
            self._tags_expanded = False
        self.current_item = item
        self._image_source_pixmap = QPixmap()
        self.image_preview.clear()
        self.text_preview.clear()
        self.type_badge.setText(TYPE_LABELS.get(item["kind"], item["kind"]))
        self.title.setText(_wrap_detail_text(item["title"]))
        self.title.setToolTip(item["title"])
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
        path_text = f"\n路径  {_wrap_detail_text(item['path'])}" if item["path"] else ""
        self.meta.setText(f"类型  {TYPE_LABELS.get(item['kind'], item['kind'])}\n{dimensions}大小  {human_size(item['file_size'])}\n时间  {format_local_timestamp(item['created_at'])}\n来源  {item['source']}{path_text}")
        self.meta.setToolTip(item["path"] or "")
        self.collection_combo.blockSignals(True)
        index = self.collection_combo.findData(item["collection_id"])
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)
        self.collection_combo.setEnabled(True)
        self.add_tag_button.setEnabled(True)
        self._set_tags(item["tag_names"] or "", item["tag_colors"] or "")
        self.ai_description.setText(
            _wrap_detail_text(item["ai_description"]) if item["ai_description"] else "尚未生成"
        )
        self.ocr_text.setText(
            _wrap_detail_text(item["ocr_text"]) if item["ocr_text"] else "尚未识别"
        )
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
        self.notes.setEnabled(True)
        self._loaded_notes = loaded_notes
        return True

    def clear_item(self) -> None:
        self.flush_notes()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.current_item = None
        self._image_source_pixmap = QPixmap()
        self.type_badge.setText("详情")
        self.title.setText("选择一项查看详情")
        self.title.setToolTip("")
        self.image_preview.clear()
        self.text_preview.clear()
        self.preview_stack.setCurrentWidget(self.text_preview)
        self.meta.clear()
        self.meta.setToolTip("")
        self._set_tags("", "")
        self._tags_expanded = False
        self.ai_description.setText("尚未生成")
        self.ocr_text.setText("尚未识别")
        self.ai_button.setEnabled(False)
        self.ai_button.setText("生成描述")
        self.ocr_button.setEnabled(False)
        self.ocr_button.setText("识别文字")
        self.collection_combo.blockSignals(True)
        self.collection_combo.setCurrentIndex(0)
        self.collection_combo.blockSignals(False)
        self.collection_combo.setEnabled(False)
        self.add_tag_button.setEnabled(False)
        self.notes.blockSignals(True)
        self.notes.clear()
        self.notes.blockSignals(False)
        self.notes.setEnabled(False)
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
        self._image_source_pixmap = QPixmap(pixmap)
        self._refresh_image_preview()

    def _refresh_image_preview(self) -> None:
        if self._image_source_pixmap.isNull():
            return
        available_width = max(
            1,
            min(self.image_preview.width(), self.viewport().width() - 32),
        )
        available_height = max(1, min(230, self.image_preview.height()))
        self.image_preview.setPixmap(
            self._image_source_pixmap.scaled(
                available_width,
                available_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._image_source_pixmap.isNull():
            self._refresh_image_preview()

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
        self._tag_names = names.split("\x1f") if names else []
        self._tag_colors = colors.split("\x1f") if colors else []
        self.tags_more_button = None
        visible_names = self._tag_names if self._tags_expanded else self._tag_names[:4]
        for index, name in enumerate(visible_names):
            button = QPushButton()
            button.setObjectName("TagChip")
            button.setText(button.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 108))
            button.setMinimumWidth(0)
            button.setMaximumWidth(130)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            color = self._tag_colors[index] if index < len(self._tag_colors) else "#64748b"
            button.setIcon(QIcon(color_dot(color)))
            button.setToolTip(f"{name}\n点击移除标签")
            button.clicked.connect(lambda _checked=False, tag=name: self.current_item and self.remove_tag_requested.emit(self.current_item["id"], tag))
            self.tags_box.addWidget(button, index // 2, index % 2)
        if len(self._tag_names) > 4:
            more = QPushButton()
            more.setObjectName("TagMoreButton")
            if self._tags_expanded:
                more.setText("收起标签")
                more.setToolTip("仅显示前四个标签")
            else:
                more.setText(f"更多标签  +{len(self._tag_names) - 4}")
                more.setToolTip("显示全部标签")
            more.clicked.connect(self._toggle_tags_expanded)
            self.tags_box.addWidget(more, (len(visible_names) + 1) // 2, 0, 1, 2)
            self.tags_more_button = more

    def _toggle_tags_expanded(self) -> None:
        self._tags_expanded = not self._tags_expanded
        self._set_tags("\x1f".join(self._tag_names), "\x1f".join(self._tag_colors))

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
        self.resize(_fit_dialog_size(self, QSize(920, 700)))
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
        self.setProperty("dateDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(_fit_dialog_size(self, QSize(420, 560)))
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
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
        self.setProperty("settingsDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        preferred_size = QSize(620, 560)
        dialog_size = _fit_dialog_size(self, preferred_size)
        self.setFixedSize(dialog_size)
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar("设置")
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("DialogContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 8, 24, 8)
        layout.setSpacing(4)
        heading = QLabel("常规")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self.close_to_tray = FluentComboBox()
        self.close_to_tray.addItem("关闭窗口时最小化到托盘", True)
        self.close_to_tray.addItem("关闭窗口时退出", False)
        self.close_to_tray.setCurrentIndex(0 if settings.get("close_to_tray", True) else 1)
        layout.addWidget(self.close_to_tray)
        self.start_with_windows = ToggleSwitch("开机时自动启动 ClipSave")
        self.start_with_windows.setChecked(settings.get("start_with_windows", False))
        self.start_with_windows.setToolTip("登录 Windows 后自动启动 ClipSave")
        layout.addWidget(self.start_with_windows)
        self.follow_system_theme = ToggleSwitch("跟随 Windows 深浅色主题")
        self.follow_system_theme.setChecked(settings.get("follow_system_theme", True))
        layout.addWidget(self.follow_system_theme)
        self.dark_theme_switch = ToggleSwitch("使用深色主题")
        self.dark_theme_switch.setChecked(settings.get("theme_mode", "light") == "dark")
        self.dark_theme_switch.setEnabled(not self.follow_system_theme.isChecked())
        self.follow_system_theme.toggled.connect(
            lambda checked: self.dark_theme_switch.setEnabled(not checked)
        )
        layout.addWidget(self.dark_theme_switch)
        hotkey_state = getattr(parent, "global_hotkey_registered", None)
        if hotkey_state is False:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V（注册失败，可能已被占用）"
        elif hotkey_state is True:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V（已启用）"
        else:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V"
        self.hotkey_status = QLabel(hotkey_text)
        layout.addWidget(self.hotkey_status)
        layout.addSpacing(6)
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
        layout.addSpacing(6)
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
        if dialog_size != preferred_size:
            scroll = QScrollArea()
            scroll.setObjectName("DialogScroll")
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBar(AutoHideScrollBar())
            scroll.setWidget(content)
            root.addWidget(scroll, 1)
        else:
            root.addWidget(content, 1)

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
            "start_with_windows": self.start_with_windows.isChecked(),
            "follow_system_theme": self.follow_system_theme.isChecked(),
            "theme_mode": "dark" if self.dark_theme_switch.isChecked() else "light",
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
