import ctypes
import unittest
from unittest.mock import Mock, patch

from clipsave_app import windows_frame


class WindowsFrameTests(unittest.TestCase):
    def test_enable_native_resize_frame_restores_style_and_refreshes_frame(self):
        user32 = Mock()
        user32.GetWindowLongPtrW.side_effect = [0x10000000, 0x10040000]
        user32.SetWindowLongPtrW.return_value = 0x10000000
        user32.SetWindowPos.return_value = 1

        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            self.assertTrue(windows_frame.enable_native_resize_frame(123))

        user32.SetWindowLongPtrW.assert_called_once_with(
            123, windows_frame.GWL_STYLE, 0x10040000
        )
        flags = user32.SetWindowPos.call_args.args[-1]
        self.assertTrue(flags & windows_frame.SWP_FRAMECHANGED)
        self.assertTrue(flags & windows_frame.SWP_NOMOVE)
        self.assertTrue(flags & windows_frame.SWP_NOSIZE)

    def test_enable_native_resize_frame_does_not_refresh_an_existing_style(self):
        user32 = Mock()
        user32.GetWindowLongPtrW.return_value = 0x10040000
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            self.assertTrue(windows_frame.enable_native_resize_frame(123))

        user32.SetWindowLongPtrW.assert_not_called()
        user32.SetWindowPos.assert_not_called()

    def test_enable_native_resize_frame_reports_style_update_failure(self):
        user32 = Mock()
        user32.GetWindowLongPtrW.return_value = 0x10000000
        user32.SetWindowLongPtrW.return_value = 0

        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ), patch("ctypes.get_last_error", return_value=5):
            self.assertFalse(windows_frame.enable_native_resize_frame(123))

        user32.SetWindowPos.assert_not_called()

    def test_nccalcsize_uses_monitor_work_area_when_maximized(self):
        params = windows_frame.NCCALCSIZE_PARAMS()
        params.rgrc[0] = windows_frame.wintypes.RECT(0, 0, 1920, 1080)
        user32 = Mock()
        user32.IsZoomed.return_value = 1
        user32.MonitorFromRect.return_value = 456
        user32.MonitorFromWindow.return_value = 456

        def fill_monitor_info(_monitor, pointer):
            info = ctypes.cast(
                pointer, ctypes.POINTER(windows_frame.MONITORINFO)
            ).contents
            info.rcWork = windows_frame.wintypes.RECT(0, 0, 1920, 1040)
            return 1

        user32.GetMonitorInfoW.side_effect = fill_monitor_info
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            handled, result = windows_frame.handle_nccalcsize(
                123, 1, ctypes.addressof(params)
            )

        self.assertTrue(handled)
        self.assertEqual(result, 0)
        rect = params.rgrc[0]
        self.assertEqual((rect.left, rect.top, rect.right, rect.bottom), (0, 0, 1920, 1040))
        user32.MonitorFromRect.assert_called_once()
        user32.MonitorFromWindow.assert_not_called()

    def test_nccalcsize_preserves_proposed_rect_when_not_maximized(self):
        rect = windows_frame.wintypes.RECT(10, 20, 810, 620)
        user32 = Mock()
        user32.IsZoomed.return_value = 0
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            handled, result = windows_frame.handle_nccalcsize(
                123, 0, ctypes.addressof(rect)
            )

        self.assertEqual((handled, result), (True, 0))
        self.assertEqual((rect.left, rect.top, rect.right, rect.bottom), (10, 20, 810, 620))


if __name__ == "__main__":
    unittest.main()
