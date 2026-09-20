import unittest
from unittest.mock import Mock

from clipsave_app.native_clipboard_reader import ClipboardBusy, _opened_clipboard


class NativeClipboardReaderTests(unittest.TestCase):
    def test_opened_clipboard_always_closes_after_body_error(self):
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = True

        with self.assertRaisesRegex(ValueError, "boom"):
            with _opened_clipboard(lambda: (user32, kernel32)):
                raise ValueError("boom")

        user32.CloseClipboard.assert_called_once_with()

    def test_opened_clipboard_does_not_close_when_open_fails(self):
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = False

        with self.assertRaises(ClipboardBusy):
            with _opened_clipboard(lambda: (user32, kernel32)):
                pass

        user32.CloseClipboard.assert_not_called()
