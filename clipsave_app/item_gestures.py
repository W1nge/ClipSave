from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


class _ItemRightClickGesture:
    """Emit detail for one right click on any library item."""

    def __init__(self, view) -> None:
        self.view = view
        self._pressed_id: int | None = None

    def cancel(self) -> None:
        self._pressed_id = None

    def press(self, item_id: int | None) -> None:
        if item_id is None:
            self.cancel()
            return
        self._pressed_id = item_id

    def release(self, item_id: int | None) -> None:
        if item_id is None or item_id != self._pressed_id:
            self.cancel()
            return
        self._pressed_id = None
        self.view.detail_requested.emit(item_id)

class _ItemTripleClickGesture:
    """Keep double-click immediate while recognizing a following third click."""

    def __init__(self, view) -> None:
        self.view = view
        self._timer = QTimer(view)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._clear_pending)
        self._pending_id: int | None = None
        self._suppress_release = False

    def cancel(self) -> None:
        self._timer.stop()
        self._pending_id = None
        self._suppress_release = False

    def finish_pending(self) -> None:
        self._clear_pending()

    def press(self, item_id: int | None) -> bool:
        if self._pending_id is None:
            return False
        if item_id != self._pending_id:
            self._clear_pending()
            return False
        self._timer.stop()
        self._pending_id = None
        self._suppress_release = True
        self.view.open_requested.emit(item_id)
        return True

    def release(self) -> bool:
        if not self._suppress_release:
            return False
        self._suppress_release = False
        return True

    def double_click(self, item_id: int | None) -> bool:
        if item_id is None:
            return False
        self._pending_id = item_id
        self._timer.start(max(1, QApplication.doubleClickInterval()))
        self.view.item_activated.emit(item_id)
        return True

    def _clear_pending(self) -> None:
        self._pending_id = None
        self._timer.stop()

