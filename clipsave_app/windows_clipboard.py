from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Signal


class WindowsClipboardNotifier(QObject, QAbstractNativeEventFilter):
    WM_CLIPBOARDUPDATE = 0x031D
    changed = Signal()

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._hwnd: int | None = None
        self._installed = False

    @staticmethod
    @lru_cache(maxsize=1)
    def _user32():
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.AddClipboardFormatListener.restype = wintypes.BOOL
        user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
        return user32

    @property
    def active(self) -> bool:
        return self._installed

    def start(self, window) -> bool:
        if os.name != "nt" or self._installed:
            return self._installed
        app = QCoreApplication.instance()
        if app is None or window is None:
            return False
        try:
            hwnd = int(window.winId())
            if not hwnd or not self._user32().AddClipboardFormatListener(hwnd):
                return False
            app.installNativeEventFilter(self)
        except (AttributeError, OSError, TypeError):
            return False
        self._hwnd = hwnd
        self._installed = True
        return True

    def stop(self) -> None:
        if not self._installed:
            return
        app = QCoreApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)
        try:
            self._user32().RemoveClipboardFormatListener(self._hwnd)
        except (AttributeError, OSError, TypeError):
            pass
        self._hwnd = None
        self._installed = False

    def nativeEventFilter(self, _event_type, message):
        if self._installed and self._is_clipboard_message(message):
            self.changed.emit()
        return False, 0

    def _is_clipboard_message(self, message) -> bool:
        try:
            native = wintypes.MSG.from_address(int(message))
            return (
                int(native.hWnd) == self._hwnd
                and native.message == self.WM_CLIPBOARDUPDATE
            )
        except (TypeError, ValueError, OSError):
            return False
