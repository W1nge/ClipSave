"""Focused checks of native-message translation using real Qt text widgets."""
import ctypes as C
import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from clipsave_app.windows_composition_input import (
    CandidateForm, CompositionForm, WindowsCompositionInput as SearchInput,
)


class Function:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


class Native:
    def __init__(self):
        self.keys = set()
        self.preedit = self.result = ''
        self.cursor = 0
        self.dpi = 144
        self.GetKeyState = Function(lambda key: 0x8000 if key in self.keys else 0)
        self.GetDpiForWindow = Function(lambda hwnd: self.dpi)
        self.DefWindowProcW = Function(lambda *args: 17)
        self.ImmGetContext = Function(lambda hwnd: 123)
        self.ImmReleaseContext = Function(lambda *args: True)
        self.ImmGetCompositionStringW = Function(self.composition)
        self.ImmSetCompositionWindow = Function(self.set_composition)
        self.ImmSetCandidateWindow = Function(self.set_candidate)

    def composition(self, context, index, buffer, length):
        if index == 128:
            return self.cursor
        data = (self.result if index == 0x800 else self.preedit).encode('utf-16-le')
        if buffer is not None:
            C.memmove(buffer, data, min(length, len(data)))
        return len(data)

    def set_composition(self, context, pointer):
        self.composition_form = CompositionForm.from_buffer_copy(C.string_at(pointer, C.sizeof(CompositionForm)))
        return True

    def set_candidate(self, context, pointer):
        self.candidate_form = CandidateForm.from_buffer_copy(C.string_at(pointer, C.sizeof(CandidateForm)))
        return True


class SearchInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.resize(500, 180)
        self.window.search = QLineEdit(self.window)
        self.window.search.setGeometry(30, 45, 250, 32)
        self.window.show()
        self.native = Native()
        self.input = SearchInput(self.window, 1, self.native, imm32=self.native)
        self.input.handle(7, 0, 0)
        self.window.search.setFocus()
        self.app.processEvents()
        self.assertIs(self.input.target(), self.window.search)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def type_text(self, text):
        for char in text:
            self.input.handle(0x102, ord(char), 1)

    def key(self, key):
        self.input.handle(0x100, key, 1)
        self.input.handle(0x101, key, 1 | (1 << 31))

    def test_latin_edit_selection_and_shortcuts(self):
        self.key(ord('A'))
        self.type_text('abc')
        self.assertEqual(self.window.search.text(), 'abc')
        self.key(0x25)
        self.key(8)
        self.assertEqual(self.window.search.text(), 'ac')
        self.native.keys.add(0x11)
        self.key(ord('A'))
        self.input.handle(0x102, 1, 1)
        self.native.keys.clear()
        self.assertEqual(self.window.search.selectedText(), 'ac')
        self.type_text('replacement')
        self.assertEqual(self.window.search.text(), 'replacement')

    def test_chinese_preedit_and_single_commit(self):
        changed = []
        self.window.search.textChanged.connect(changed.append)
        self.native.preedit, self.native.cursor = 'zhong', 5
        self.input.handle(0x10D, 0, 0)
        self.input.handle(0x10F, 0, 8 | 128)
        self.assertEqual(self.window.search.text(), '')
        self.assertEqual(changed, [])
        self.native.result = '中文'
        self.input.handle(0x10F, 0, 0x800)
        self.input.handle(0x286, ord('中'), 0)
        self.input.handle(0x10E, 0, 0)
        self.assertEqual(self.window.search.text(), '中文')
        self.assertEqual(changed, ['中文'])
        self.assertFalse(self.input.composing)

    def test_cancel_preedit_and_unicode_surrogate_pair(self):
        self.type_text('a')
        self.native.preedit, self.native.cursor = 'shi', 3
        self.input.handle(0x10D, 0, 0)
        self.input.handle(0x10F, 0, 8 | 128)
        self.input.handle(0x10E, 0, 0)
        self.input.handle(0x102, 0xD83D, 1)
        self.input.handle(0x102, 0xDE42, 1)
        self.assertEqual(self.window.search.text(), 'a🙂')

    def test_candidate_follows_cursor_at_host_dpi(self):
        self.type_text('hello')
        self.input.update_position(force=True)
        rect = self.window.search.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
        candidate = self.native.candidate_form
        self.assertEqual(candidate.style, 0x80)
        self.assertEqual(candidate.point.x, round((30 + rect.left()) * 1.5))
        self.assertEqual(candidate.point.y, round((45 + rect.top()) * 1.5) + round(rect.height() * 1.5))
        self.window.search.move(70, 65)
        self.input.update_position(force=True)
        self.assertEqual(self.native.candidate_form.point.x, candidate.point.x + 60)

    def test_focus_and_native_system_keys(self):
        self.assertIsNone(self.input.handle(0x104, 0x73, 1))
        self.assertIsNone(self.input.handle(0x100, 0xE5, 1))
        self.input.handle(8, 0, 0)
        self.assertIsNone(QApplication.activeWindow())
        self.input.handle(7, 0, 0)
        self.assertIs(QApplication.activeWindow(), self.window)

    def test_ime_position_notifications_do_not_reenter(self):
        calls = []
        def set_candidate(context, pointer):
            calls.append(1)
            self.input.handle(0x282, 9, 0)  # IMN_SETCANDIDATEPOS
            self.input.update_position(force=True)
            return True
        self.native.ImmSetCandidateWindow = Function(set_candidate)
        self.input.update_position(force=True)
        self.assertEqual(len(calls), 1)

    def test_input_restores_qt_focus_after_native_resize_deactivation(self):
        self.type_text('before')
        self.input.handle(8, 0, 0)
        self.assertFalse(self.window.search.hasFocus())
        self.window.resize(650, 250)
        self.type_text('after')
        self.assertTrue(self.window.search.hasFocus())
        self.assertEqual(self.window.search.text(), 'beforeafter')


if __name__ == '__main__':
    unittest.main()
