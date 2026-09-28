"""Keyboard and IMM input for the accepted host, without changing presentation."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W

from PySide6.QtCore import QCoreApplication, QPoint, Qt
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QTextCharFormat
from PySide6.QtWidgets import QApplication


class CompositionForm(C.Structure):
    _fields_ = [('style', W.DWORD), ('point', W.POINT), ('area', W.RECT)]


class CandidateForm(C.Structure):
    _fields_ = [('index', W.DWORD), ('style', W.DWORD),
               ('point', W.POINT), ('area', W.RECT)]


_KEYS = {
    0x08: Qt.Key.Key_Backspace, 0x09: Qt.Key.Key_Tab, 0x0D: Qt.Key.Key_Return,
    0x10: Qt.Key.Key_Shift, 0x11: Qt.Key.Key_Control, 0x12: Qt.Key.Key_Alt,
    0x1B: Qt.Key.Key_Escape, 0x20: Qt.Key.Key_Space, 0x21: Qt.Key.Key_PageUp,
    0x22: Qt.Key.Key_PageDown, 0x23: Qt.Key.Key_End, 0x24: Qt.Key.Key_Home,
    0x25: Qt.Key.Key_Left, 0x26: Qt.Key.Key_Up, 0x27: Qt.Key.Key_Right,
    0x28: Qt.Key.Key_Down, 0x2D: Qt.Key.Key_Insert, 0x2E: Qt.Key.Key_Delete,
    0xBA: Qt.Key.Key_Semicolon, 0xBB: Qt.Key.Key_Plus, 0xBC: Qt.Key.Key_Comma,
    0xBD: Qt.Key.Key_Minus, 0xBE: Qt.Key.Key_Period, 0xBF: Qt.Key.Key_Slash,
    0xC0: Qt.Key.Key_QuoteLeft, 0xDB: Qt.Key.Key_BracketLeft,
    0xDC: Qt.Key.Key_Backslash, 0xDD: Qt.Key.Key_BracketRight,
    0xDE: Qt.Key.Key_Apostrophe,
}


class WindowsCompositionInput:
    def __init__(self, window, hwnd, user32, *, imm32=None):
        self.window, self.hwnd, self.user = window, int(hwnd), user32
        self.imm = imm32 if imm32 is not None else C.WinDLL('imm32', use_last_error=True)
        self.user.GetKeyState.argtypes = [C.c_int]
        self.user.GetKeyState.restype = C.c_short
        self.imm.ImmGetContext.argtypes = [W.HWND]
        self.imm.ImmGetContext.restype = W.HANDLE
        self.imm.ImmReleaseContext.argtypes = [W.HWND, W.HANDLE]
        self.imm.ImmReleaseContext.restype = W.BOOL
        self.imm.ImmGetCompositionStringW.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD]
        self.imm.ImmGetCompositionStringW.restype = C.c_long
        self.imm.ImmSetCompositionWindow.argtypes = [W.HANDLE, C.POINTER(CompositionForm)]
        self.imm.ImmSetCompositionWindow.restype = W.BOOL
        self.imm.ImmSetCandidateWindow.argtypes = [W.HANDLE, C.POINTER(CandidateForm)]
        self.imm.ImmSetCandidateWindow.restype = W.BOOL
        self.composing = False
        self._positioning = False
        self._last_position = None
        self._high_surrogate = None
        self.key_events = self.text_events = self.ime_events = 0
        window.search.cursorPositionChanged.connect(self.update_position)

    def target(self):
        return self.window.focusWidget()

    def activate(self):
        # The host may retain USER32 focus while Qt deactivates its hidden
        # backing window during a native modal resize. SetFocus(host) then
        # emits no WM_SETFOCUS, so restore logical activation independently.
        if QApplication.activeWindow() is not self.window:
            QApplication.setActiveWindow(self.window)

    def modifiers(self):
        result = Qt.KeyboardModifier.NoModifier
        for vk, modifier in ((0x10, Qt.KeyboardModifier.ShiftModifier),
                             (0x11, Qt.KeyboardModifier.ControlModifier),
                             (0x12, Qt.KeyboardModifier.AltModifier)):
            if self.user.GetKeyState(vk) & 0x8000:
                result |= modifier
        return result

    def handle(self, message, wp, lp):
        if message == 0x0007:  # WM_SETFOCUS
            self.activate()
            return None
        if message == 0x0008:  # WM_KILLFOCUS
            if QApplication.activeWindow() is self.window:
                QApplication.setActiveWindow(None)
            return None
        if message in (0x0100, 0x0101, 0x0102, 0x0104, 0x0105, 0x0109, 0x010D, 0x010F):
            self.activate()
        target = self.target()
        if target is None:
            return None
        if message in (0x0100, 0x0101, 0x0104, 0x0105):
            if wp == 0xE5:  # VK_PROCESSKEY belongs to the input method.
                return None
            modifiers = self.modifiers()
            if message in (0x0104, 0x0105) and wp in (0x73, 0x20):
                return None  # Keep native Alt+F4 and the system menu.
            if 0x30 <= wp <= 0x39 or 0x41 <= wp <= 0x5A:
                key = wp
            elif 0x70 <= wp <= 0x87:
                key = int(Qt.Key.Key_F1) + wp - 0x70
            elif 0x60 <= wp <= 0x69:
                key = int(Qt.Key.Key_0) + wp - 0x60
            else:
                key = int(_KEYS.get(wp, Qt.Key.Key_unknown))
            pressed = message in (0x0100, 0x0104)
            event = QKeyEvent(QKeyEvent.Type.KeyPress if pressed else QKeyEvent.Type.KeyRelease,
                             key, modifiers, '', pressed and bool(lp & (1 << 30)),
                             max(1, lp & 0xffff))
            QCoreApplication.sendEvent(target, event)
            self.key_events += 1
            self.update_position()
            return 0
        if message in (0x0102, 0x0109):  # WM_CHAR / WM_UNICHAR
            if message == 0x0109 and wp == 0xFFFF:
                return 1
            if wp < 32 or wp == 127:
                return 0  # Backspace/Return/Tab and shortcuts were handled above.
            if 0xD800 <= wp <= 0xDBFF:
                self._high_surrogate = wp
                return 0
            if 0xDC00 <= wp <= 0xDFFF and self._high_surrogate is not None:
                wp = 0x10000 + ((self._high_surrogate - 0xD800) << 10) + wp - 0xDC00
            self._high_surrogate = None
            event = QKeyEvent(QKeyEvent.Type.KeyPress, int(Qt.Key.Key_unknown),
                             Qt.KeyboardModifier.NoModifier, chr(wp),
                             bool(lp & (1 << 30)), max(1, lp & 0xffff))
            QCoreApplication.sendEvent(target, event)
            self.text_events += 1
            self.update_position()
            return 0
        if message == 0x0281:  # WM_IME_SETCONTEXT: Qt draws the inline preedit.
            self.update_position(force=True)
            return self.user.DefWindowProcW(self.hwnd, message, wp, lp & ~0x80000000)
        if message == 0x010D:  # WM_IME_STARTCOMPOSITION
            self.composing = True
            self.update_position(force=True)
            return 0
        if message == 0x010E:  # WM_IME_ENDCOMPOSITION
            QCoreApplication.sendEvent(target, QInputMethodEvent())
            self.composing = False
            return 0
        if message == 0x0286:  # WM_IME_CHAR must not insert the result twice.
            return 0
        if message == 0x0282:  # WM_IME_NOTIFY: preserve the OS candidate UI.
            # ImmSet*Window sends its own IMN_SET*POS notifications. Updating
            # position in response to those notifications recurses in USER32.
            if wp in (3, 5):  # IMN_CHANGECANDIDATE / IMN_OPENCANDIDATE
                self.update_position(force=True)
            return None
        if message != 0x010F:
            return None
        context = self.imm.ImmGetContext(self.hwnd)
        if not context:
            return None
        try:
            result = self._composition_text(context, 0x800) if lp & 0x800 else ''
            preedit = self._composition_text(context, 8) if lp & (8 | 16 | 128) else ''
            attributes = []
            if preedit:
                length = len(preedit.encode('utf-16-le')) // 2
                cursor = self.imm.ImmGetCompositionStringW(context, 128, None, 0)
                cursor = max(0, min(length, cursor))
                underline = QTextCharFormat()
                underline.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
                attributes = [
                    QInputMethodEvent.Attribute(QInputMethodEvent.AttributeType.TextFormat,
                                                0, length, underline),
                    QInputMethodEvent.Attribute(QInputMethodEvent.AttributeType.Cursor,
                                                cursor, 1, None),
                ]
            event = QInputMethodEvent(preedit, attributes)
            if result:
                event.setCommitString(result)
            QCoreApplication.sendEvent(target, event)
            self.ime_events += 1
        finally:
            self.imm.ImmReleaseContext(self.hwnd, context)
        self.update_position(force=True)
        return 0

    def _composition_text(self, context, index):
        length = self.imm.ImmGetCompositionStringW(context, index, None, 0)
        if length <= 0:
            return ''
        buffer = C.create_string_buffer(length)
        copied = self.imm.ImmGetCompositionStringW(context, index, buffer, length)
        return buffer.raw[:max(0, copied)].decode('utf-16-le', 'replace')

    def update_position(self, *_args, force=False):
        if self._positioning or (not self.composing and not force):
            return
        target = self.target()
        if target is None or not target.inputMethodQuery(Qt.InputMethodQuery.ImEnabled):
            return
        rect = target.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
        offset = target.mapTo(self.window, QPoint())
        scale = max(96, self.user.GetDpiForWindow(self.hwnd)) / 96
        left, top = round((rect.left() + offset.x()) * scale), round((rect.top() + offset.y()) * scale)
        right, bottom = left + max(1, round(rect.width() * scale)), top + max(1, round(rect.height() * scale))
        position = (left, top, right, bottom)
        if not force and position == self._last_position:
            return
        composition = CompositionForm(2, W.POINT(left, top), W.RECT())
        candidate = CandidateForm(0, 0x80, W.POINT(left, bottom), W.RECT(left, top, right, bottom))
        context = self.imm.ImmGetContext(self.hwnd)
        if context:
            self._positioning = True
            try:
                self.imm.ImmSetCompositionWindow(context, C.byref(composition))
                self.imm.ImmSetCandidateWindow(context, C.byref(candidate))
                self._last_position = position
            finally:
                self.imm.ImmReleaseContext(self.hwnd, context)
                self._positioning = False
