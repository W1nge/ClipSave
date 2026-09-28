from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, QTimer
from PySide6.QtGui import QMoveEvent, QResizeEvent


class ResizeFrameController(QObject):
    """Present native resize geometry, QWidget pixels and Acrylic as one frame.

    QWindow.geometry() already reflects the platform window when its queued
    QWidget resize event has not run yet. Letting a card UpdateRequest flush in
    that gap submits the old bitmap using the new HWND size. During a native
    drag this controller owns resize/expose delivery and coalesces it with
    widget updates, then follows the completed foreground with its backdrop.
    """

    def __init__(
        self, window, *, flush_widgets: Callable[[QEvent], object],
        sync_backdrop: Callable[[], None],
    ) -> None:
        super().__init__(window)
        self.window = window
        self.flush_widgets = flush_widgets
        self.sync_backdrop = sync_backdrop
        self.active = False
        self.rendering = False
        self._forwarding = False
        self._handle = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.render_frame)

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        if active:
            handle = self.window.windowHandle()
            if handle is None:
                return
            if handle is not self._handle:
                if self._handle is not None:
                    self._handle.removeEventFilter(self)
                self._handle = handle
                handle.installEventFilter(self)
            self.active = True
        else:
            # Consume the last native geometry before returning ownership to
            # Qt, including when the mouse is released before the queued tick.
            self.render_frame()
            self.active = False
            self._timer.stop()

    def request_frame(self) -> None:
        if self.active and not self.rendering and not self._timer.isActive():
            self._timer.start()

    def eventFilter(self, watched, event) -> bool:
        if (
            self.active and not self._forwarding and watched is self._handle
            and event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Expose)
            and self.window.isVisible() and self._handle.isExposed()
        ):
            self.request_frame()
            return True
        return False

    def render_frame(self, event: QEvent | None = None) -> None:
        if not self.active or self.rendering or not self.window.isVisible():
            return
        handle = self._handle
        target = QRect(handle.geometry())
        if target.isEmpty():
            return
        self._timer.stop()
        self.rendering = True
        try:
            self._forwarding = True
            if self.window.pos() != target.topLeft():
                QCoreApplication.sendEvent(
                    handle, QMoveEvent(target.topLeft(), self.window.pos()),
                )
            resized = self.window.size() != target.size()
            if resized:
                # QWidgetWindow's normal resize handler updates the widget
                # tree AND paints its resized backing store synchronously.
                # Deliver the already-committed size; setGeometry() here would
                # round-trip through Win32 and generate another resize cycle.
                QCoreApplication.sendEvent(
                    handle, QResizeEvent(target.size(), self.window.size()),
                )
            self._forwarding = False
            if not resized:
                self.flush_widgets(event or QEvent(QEvent.Type.UpdateRequest))
            if handle.geometry() == target and self.window.size() == target.size():
                self.sync_backdrop()
        finally:
            self._forwarding = False
            self.rendering = False
        # Native callbacks can change platform geometry during a flush. Never
        # advance the helper to that newer size until its foreground is ready.
        if handle.geometry() != target:
            self.request_frame()
