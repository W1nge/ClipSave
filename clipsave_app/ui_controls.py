from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QPropertyAnimation,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollBar,
    QStyle,
    QTextEdit,
    QWidget,
)

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


_TEXT_MENU_ACTION_ICONS = {
    "edit-undo": "undo-2",
    "edit-redo": "redo-2",
    "edit-cut": "scissors",
    "edit-copy": "copy",
    "edit-paste": "clipboard",
    "edit-delete": "trash-2",
    "select-all": "list-checks",
}


class ThemedTextContextMenuMixin:
    def _create_themed_context_menu(self) -> QMenu:
        menu = self.createStandardContextMenu()
        menu.setObjectName("TextContextMenu")
        for action in menu.actions():
            action.setIcon(QIcon())
            glyph = _TEXT_MENU_ACTION_ICONS.get(action.objectName())
            if glyph:
                action.setIcon(lucide_icon(glyph, theme_icon_color(), 16))
        return menu

    def contextMenuEvent(self, event) -> None:
        menu = self._create_themed_context_menu()
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()
        event.accept()


class ThemedLineEdit(ThemedTextContextMenuMixin, QLineEdit):
    pass


class ThemedTextEdit(ThemedTextContextMenuMixin, QTextEdit):
    pass


class ThemedSelectableLabel(QLabel):
    def _create_themed_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setObjectName("TextContextMenu")
        copy_action = menu.addAction(
            lucide_icon("copy", theme_icon_color(), 16),
            "复制",
        )
        copy_action.setObjectName("edit-copy")
        copy_action.setEnabled(self.hasSelectedText())
        copy_action.triggered.connect(
            lambda: QApplication.clipboard().setText(self.selectedText())
        )
        select_all = menu.addAction(
            lucide_icon("list-checks", theme_icon_color(), 16),
            "全选",
        )
        select_all.setObjectName("select-all")
        select_all.setEnabled(bool(self.text()))
        select_all.triggered.connect(
            lambda: self.setSelection(0, len(self.text()))
        )
        return menu

    def contextMenuEvent(self, event) -> None:
        menu = self._create_themed_context_menu()
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()
        event.accept()


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

    def sizeHint(self) -> QSize:
        base = super().sizeHint()
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(max(base.width(), text_width + 56), max(30, base.height()))

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
        text_rect = source.rect().adjusted(16, 0, 0, 0)
        baseline_rect = QRect(text_rect)
        clip_text = "Clip" if self.text == "ClipSave" else self.text
        save_text = "Save" if self.text == "ClipSave" else ""
        source_painter.setPen(QColor("#ffffff" if dark_theme_active() else "#333b47"))
        source_painter.drawText(baseline_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, clip_text)
        clip_width = source_painter.fontMetrics().horizontalAdvance(clip_text)
        if save_text:
            baseline_rect.setLeft(baseline_rect.left() + clip_width)
            source_painter.setPen(self.color)
            source_painter.drawText(baseline_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, save_text)
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
    def __init__(
        self,
        orientation=Qt.Orientation.Vertical,
        parent=None,
        *,
        track_width: int | None = None,
        light_background: str = "#f6f6f6",
        dark_background: str = "#202020",
        align_to_edge: bool = False,
        always_visible: bool = False,
    ):
        super().__init__(orientation, parent)
        self.setObjectName("AutoHideScrollBar")
        self._light_background = light_background
        self._dark_background = dark_background
        self._align_to_edge = align_to_edge
        self._always_visible = always_visible
        if track_width is not None:
            thickness = max(1, int(track_width))
            if orientation == Qt.Orientation.Vertical:
                self.setFixedWidth(thickness)
            else:
                self.setFixedHeight(thickness)
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
        painter = QPainter(self)
        dark = dark_theme_active()
        painter.fillRect(
            self.rect(), QColor(self._dark_background if dark else self._light_background)
        )
        if (self.active or self._always_visible) and self.maximum() > self.minimum():
            handle = self._handle_rect()
            if handle.isValid():
                if self.isSliderDown() or self.underMouse():
                    color = QColor("#969696" if dark else "#768191")
                else:
                    color = QColor("#777777" if dark else "#9ca5b2")
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                radius = min(handle.width(), handle.height()) / 2.0
                painter.drawRoundedRect(QRectF(handle), radius, radius)
        painter.end()

    def _handle_rect(self) -> QRect:
        vertical = self.orientation() == Qt.Orientation.Vertical
        track_length = self.height() if vertical else self.width()
        track_thickness = self.width() if vertical else self.height()
        if track_length <= 0 or track_thickness <= 0:
            return QRect()

        value_range = self.maximum() - self.minimum()
        page_step = max(0, self.pageStep())
        proportional_length = (
            round(track_length * page_step / (value_range + page_step))
            if page_step
            else 0
        )
        handle_length = min(track_length, max(32, proportional_length))
        available = max(0, track_length - handle_length)
        position = QStyle.sliderPositionFromValue(
            self.minimum(),
            self.maximum(),
            self.value(),
            available,
            self.invertedAppearance(),
        )
        handle_thickness = min(
            max(3, round(track_thickness * 10 / 14)), track_thickness
        )
        inset = (
            track_thickness - handle_thickness
            if self._align_to_edge
            else (track_thickness - handle_thickness) // 2
        )
        if vertical:
            return QRect(inset, position, handle_thickness, handle_length)
        return QRect(position, inset, handle_length, handle_thickness)

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
class WheelRemainder:
    pixel_x: float = 0.0
    pixel_y: float = 0.0
    angle_x: float = 0.0
    angle_y: float = 0.0


def half_speed_wheel_event(
    event: QWheelEvent, remainder: WheelRemainder | None = None
) -> QWheelEvent:
    remainder = remainder or WheelRemainder()
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


_ThemedTextContextMenuMixin = ThemedTextContextMenuMixin
_WheelRemainder = WheelRemainder
_half_speed_wheel_event = half_speed_wheel_event



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
            icon = self._render_lucide_icon(self.glyph, color)
        self.setIcon(icon)

    def _render_lucide_icon(self, glyph: str, color: str) -> QIcon:
        return lucide_icon(glyph, color)

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
