from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QWidget


class _AutoScrollMarker(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(255, 255, 255, 105), 1))
            painter.setBrush(QColor(24, 25, 28, 205))
            painter.drawEllipse(self.rect().adjusted(2, 2, -2, -2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(220, 226, 235, 220))
            painter.drawPolygon(
                QPolygon([QPoint(16, 7), QPoint(12, 12), QPoint(20, 12)])
            )
            painter.drawPolygon(
                QPolygon([QPoint(16, 25), QPoint(12, 20), QPoint(20, 20)])
            )
        finally:
            painter.end()


class MiddleAutoScrollController(QObject):
    """Browser-style middle-click auto-scroll for an item view."""

    DEAD_ZONE = 12
    MAX_SPEED = 1600.0

    def __init__(self, view) -> None:
        super().__init__(view)
        self.view = view
        self.viewport = view.viewport()
        self.anchor = QPoint()
        self.active = False
        self._scroll_remainder = 0.0
        self._original_mouse_tracking = self.viewport.hasMouseTracking()
        self.viewport.installEventFilter(self)
        view.installEventFilter(self)
        self.marker = _AutoScrollMarker(self.viewport)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def cancel(self) -> None:
        if not self.active:
            return
        self.active = False
        self._scroll_remainder = 0.0
        self._timer.stop()
        self.marker.hide()
        self.viewport.unsetCursor()
        # Hover effects depend on move events; only request them while the
        # auto-scroll marker is active and restore the view's own setting.
        self.viewport.setMouseTracking(self._original_mouse_tracking)

    def _start(self, point: QPoint) -> None:
        self.cancel()
        self.active = True
        self.anchor = point
        self._scroll_remainder = 0.0
        self.viewport.setMouseTracking(True)
        self.marker.move(point.x() - 16, point.y() - 16)
        self.marker.show()
        self.marker.raise_()
        self.viewport.setCursor(Qt.CursorShape.SizeVerCursor)
        self._timer.start()

    def eventFilter(self, watched, event) -> bool:
        viewport = getattr(self, "viewport", None)
        if viewport is None:
            return False
        kind = event.type()
        if watched is viewport and kind == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.MiddleButton:
                if self.active:
                    self.cancel()
                else:
                    self._start(event.position().toPoint())
                event.accept()
                return True
            if self.active:
                self.cancel()
                event.accept()
                return True
        if self.active and kind == QEvent.Type.Wheel:
            self.cancel()
            return False
        if self.active and kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.cancel()
            event.accept()
            return True
        if self.active and kind in {
            QEvent.Type.FocusOut,
            QEvent.Type.Hide,
            QEvent.Type.WindowDeactivate,
        }:
            self.cancel()
        return False

    def _tick(self) -> None:
        if not self.active or not self.view.isVisible():
            self.cancel()
            return
        point = self.viewport.mapFromGlobal(QCursor.pos())
        delta = point.y() - self.anchor.y()
        magnitude = abs(delta) - self.DEAD_ZONE
        if magnitude <= 0:
            return
        speed = min(self.MAX_SPEED, 4.5 * magnitude + 0.035 * magnitude * magnitude)
        if delta < 0:
            speed = -speed
        bar = self.view.verticalScrollBar()
        movement = speed * self._timer.interval() / 1000.0 + self._scroll_remainder
        whole_pixels = math.trunc(movement)
        self._scroll_remainder = movement - whole_pixels
        if whole_pixels:
            previous = bar.value()
            bar.setValue(previous + whole_pixels)
            if bar.value() == previous:
                self._scroll_remainder = 0.0
