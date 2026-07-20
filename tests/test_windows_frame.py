import ctypes
import unittest
from unittest.mock import Mock, patch

from clipsave_app import windows_frame


class WindowsFrameTests(unittest.TestCase):
    def test_getminmaxinfo_uses_monitor_work_area_relative_to_monitor(self):
        minimum = windows_frame.MINMAXINFO()
        minimum.ptMinTrackSize = windows_frame.POINT(800, 440)
        user32 = Mock()
        user32.MonitorFromWindow.return_value = 456

        def fill_monitor_info(_monitor, pointer):
            info = ctypes.cast(
                pointer, ctypes.POINTER(windows_frame.MONITORINFO)
            ).contents
            info.rcMonitor = windows_frame.wintypes.RECT(-1920, 7, 0, 1087)
            info.rcWork = windows_frame.wintypes.RECT(-1858, 7, 0, 1087)
            return 1

        user32.GetMonitorInfoW.side_effect = fill_monitor_info
        user32.DefWindowProcW.return_value = 0
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            handled, result = windows_frame.handle_getminmaxinfo(
                123, 0, ctypes.addressof(minimum), (900, 500)
            )

        self.assertEqual((handled, result), (True, 0))
        self.assertEqual((minimum.ptMaxPosition.x, minimum.ptMaxPosition.y), (62, 0))
        self.assertEqual((minimum.ptMaxSize.x, minimum.ptMaxSize.y), (1858, 1080))
        self.assertEqual((minimum.ptMinTrackSize.x, minimum.ptMinTrackSize.y), (900, 500))
        user32.DefWindowProcW.assert_called_once_with(
            123, windows_frame.WM_GETMINMAXINFO, 0, ctypes.addressof(minimum)
        )

    def test_getminmaxinfo_handles_taskbar_on_right_without_offset(self):
        minimum = windows_frame.MINMAXINFO()
        user32 = Mock()
        user32.MonitorFromWindow.return_value = 456

        def fill_monitor_info(_monitor, pointer):
            info = ctypes.cast(
                pointer, ctypes.POINTER(windows_frame.MONITORINFO)
            ).contents
            info.rcMonitor = windows_frame.wintypes.RECT(0, 0, 1920, 1080)
            info.rcWork = windows_frame.wintypes.RECT(0, 0, 1843, 1080)
            return 1

        user32.GetMonitorInfoW.side_effect = fill_monitor_info
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            handled, _ = windows_frame.handle_getminmaxinfo(
                123, 0, ctypes.addressof(minimum)
            )

        self.assertTrue(handled)
        self.assertEqual((minimum.ptMaxPosition.x, minimum.ptMaxPosition.y), (0, 0))
        self.assertEqual((minimum.ptMaxSize.x, minimum.ptMaxSize.y), (1843, 1080))

    def test_getminmaxinfo_handles_taskbar_on_top(self):
        minimum = windows_frame.MINMAXINFO()
        user32 = Mock()
        user32.MonitorFromWindow.return_value = 456

        def fill_monitor_info(_monitor, pointer):
            info = ctypes.cast(
                pointer, ctypes.POINTER(windows_frame.MONITORINFO)
            ).contents
            info.rcMonitor = windows_frame.wintypes.RECT(100, -1080, 2020, 0)
            info.rcWork = windows_frame.wintypes.RECT(100, -1032, 2020, 0)
            return 1

        user32.GetMonitorInfoW.side_effect = fill_monitor_info
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            handled, _ = windows_frame.handle_getminmaxinfo(
                123, 0, ctypes.addressof(minimum)
            )

        self.assertTrue(handled)
        self.assertEqual((minimum.ptMaxPosition.x, minimum.ptMaxPosition.y), (0, 48))
        self.assertEqual((minimum.ptMaxSize.x, minimum.ptMaxSize.y), (1920, 1032))

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

    def test_native_window_maximized_state_uses_win32(self):
        user32 = Mock()
        user32.IsZoomed.return_value = 1
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            self.assertTrue(windows_frame.native_window_is_maximized(123))

        user32.IsZoomed.assert_called_once_with(123)

    def test_restore_native_window_uses_sw_restore(self):
        user32 = Mock()
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            self.assertTrue(windows_frame.restore_native_window(123))

        user32.ShowWindow.assert_called_once_with(123, windows_frame.SW_RESTORE)

    def test_native_window_helpers_are_disabled_off_windows(self):
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=False):
            self.assertIsNone(windows_frame.native_window_is_maximized(123))
            self.assertFalse(windows_frame.restore_native_window(123))

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

    def test_nccalcsize_preserves_restore_rect_while_zoom_state_lingers(self):
        params = windows_frame.NCCALCSIZE_PARAMS()
        params.rgrc[0] = windows_frame.wintypes.RECT(120, 90, 1120, 790)
        user32 = Mock()
        user32.IsZoomed.return_value = 1
        user32.MonitorFromRect.return_value = 456

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

        self.assertEqual((handled, result), (True, 0))
        rect = params.rgrc[0]
        self.assertEqual((rect.left, rect.top, rect.right, rect.bottom), (120, 90, 1120, 790))

    def test_ncactivate_updates_state_without_redrawing_native_frame(self):
        user32 = Mock()
        user32.DefWindowProcW.return_value = 1

        with patch.object(windows_frame, "is_windows_qt_platform", return_value=True), patch.object(
            windows_frame, "_user32", return_value=user32
        ):
            handled, result = windows_frame.handle_ncactivate(123, 0)

        self.assertEqual((handled, result), (True, 1))
        user32.DefWindowProcW.assert_called_once_with(
            123, windows_frame.WM_NCACTIVATE, 0, -1
        )

    def test_ncactivate_is_not_intercepted_off_windows(self):
        with patch.object(windows_frame, "is_windows_qt_platform", return_value=False):
            self.assertEqual(windows_frame.handle_ncactivate(123, 1), (False, 0))


if __name__ == "__main__":
    unittest.main()
