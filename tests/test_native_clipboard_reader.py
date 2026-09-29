import unittest
from unittest.mock import Mock, patch

from clipsave_app.native_clipboard_reader import ClipboardBusy, NativeClipboardReader, _opened_clipboard


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

    def test_advertised_text_without_data_is_retryable(self):
        user32, kernel32 = Mock(), Mock()
        user32.OpenClipboard.return_value = True
        user32.IsClipboardFormatAvailable.return_value = True
        user32.GetClipboardData.return_value = None
        with patch('clipsave_app.native_clipboard_reader.os.name', 'nt'):
            with self.assertRaises(ClipboardBusy):
                NativeClipboardReader.native_text_snapshot(lambda: (user32, kernel32))
        user32.CloseClipboard.assert_called_once_with()

    def test_advertised_dib_without_data_is_retryable(self):
        user32, kernel32 = Mock(), Mock()
        user32.OpenClipboard.return_value = True
        user32.EnumClipboardFormats.return_value = 0
        kernel32.GetLastError.return_value = 0
        user32.IsClipboardFormatAvailable.side_effect = lambda fmt: fmt == NativeClipboardReader.CF_DIB
        user32.GetClipboardData.return_value = None
        with patch('clipsave_app.native_clipboard_reader.os.name', 'nt'):
            with self.assertRaises(ClipboardBusy):
                NativeClipboardReader.native_image_snapshot(lambda: (user32, kernel32))
        user32.CloseClipboard.assert_called_once_with()

    def test_native_format_enumeration_failure_is_retryable(self):
        user32, kernel32 = Mock(), Mock()
        user32.OpenClipboard.return_value = True
        user32.EnumClipboardFormats.return_value = 0
        kernel32.GetLastError.return_value = 1418  # ERROR_CLIPBOARD_NOT_OPEN
        user32.IsClipboardFormatAvailable.return_value = False
        with patch('clipsave_app.native_clipboard_reader.os.name', 'nt'):
            with self.assertRaises(ClipboardBusy):
                NativeClipboardReader.native_image_snapshot(lambda: (user32, kernel32))
        user32.CloseClipboard.assert_called_once_with()
