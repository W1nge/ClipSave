import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import Mock

from clipsave_app.native_window_controller import (
    NativeWindowController,
    windows_resize_hit_test,
)
from clipsave_app.windows_frame import (
    WM_GETMINMAXINFO,
    WM_NCACTIVATE,
    WM_WINDOWPOSCHANGING,
    WM_WINDOWPOSCHANGED,
)


class _Window:
    _interactive_resize_active = False

    def winId(self):
        return 123

    def minimumWidth(self):
        return 800

    def minimumHeight(self):
        return 440

    def isMaximized(self):
        return False

    def isFullScreen(self):
        return False


class NativeWindowControllerTests(unittest.TestCase):
    def make_controller(self):
        callbacks = {
            "window_rect": Mock(return_value=(0, 0, 1000, 700)),
            "dpi_scale": Mock(return_value=1.5),
            "handle_getminmaxinfo": Mock(return_value=(True, 0)),
            "handle_ncactivate": Mock(return_value=(True, 7)),
            "handle_nccalcsize": Mock(return_value=(False, 0)),
            "schedule_material_refresh": Mock(),
            "schedule_maximized_bounds_sync": Mock(),
            "sync_backdrop_geometry_now": Mock(),
            "sync_backdrop_window": Mock(),
            "schedule_soon": Mock(side_effect=lambda callback: callback()),
            "set_layout_updates_suspended": Mock(),
            "set_grid_interactive_resize": Mock(),
            "sidebar_animation_active": Mock(return_value=False),
            "detail_animation_active": Mock(return_value=False),
            "resize_hit_test": Mock(return_value=None),
            "enable_resize_frame": Mock(return_value=True),
            "clear_resize_handles": Mock(),
            "install_resize_handles": Mock(),
            "native_window_is_maximized": Mock(return_value=False),
            "restore_native_window": Mock(return_value=True),
            "maximize_native_window": Mock(return_value=True),
            "synchronize_maximized_work_area": Mock(),
        }
        controller = NativeWindowController(
            _Window(),
            native_events_enabled=lambda: True,
            platform_check=lambda: True,
            **callbacks,
        )
        return controller, callbacks

    def test_ncactivate_is_routed_to_injected_handler(self):
        controller, callbacks = self.make_controller()
        message = wintypes.MSG()
        message.hWnd = 123
        message.message = WM_NCACTIVATE

        result = controller.handle_native_event(
            b"windows_generic_MSG",
            ctypes.addressof(message),
        )

        self.assertEqual(result, (True, 7))
        callbacks["handle_ncactivate"].assert_called_once_with(123, 0)

    def test_getminmaxinfo_scales_minimum_window_size(self):
        controller, callbacks = self.make_controller()
        message = wintypes.MSG()
        message.hWnd = 123
        message.message = WM_GETMINMAXINFO
        message.lParam = 456

        result = controller.handle_native_event(
            b"windows_generic_MSG",
            ctypes.addressof(message),
        )

        self.assertEqual(result, (True, 0))
        callbacks["handle_getminmaxinfo"].assert_called_once_with(
            123,
            0,
            456,
            (1200, 660),
        )

    def test_resize_hit_test_preserves_l_shaped_corner_contract(self):
        bounds = (0, 0, 1000, 700, 1.0)
        self.assertEqual(windows_resize_hit_test(4, 10, *bounds), 13)
        self.assertIsNone(windows_resize_hit_test(10, 10, *bounds))

    def test_native_resize_frame_state_is_owned_by_controller(self):
        controller, callbacks = self.make_controller()

        controller.ensure_native_resize_frame()
        controller.ensure_native_resize_frame()

        callbacks["enable_resize_frame"].assert_called_once_with(123)
        callbacks["clear_resize_handles"].assert_called_once_with()
        callbacks["install_resize_handles"].assert_not_called()

    def test_backdrop_follows_only_committed_host_bounds(self):
        controller, callbacks = self.make_controller()
        controller.begin_interactive_resize()
        callbacks["set_grid_interactive_resize"].assert_called_once_with(True)
        proposed = wintypes.RECT(20, 30, 1220, 830)
        message = wintypes.MSG()
        message.hWnd = 123
        message.message = 0x0214  # WM_SIZING
        message.lParam = ctypes.addressof(proposed)

        controller.handle_native_event(
            b"windows_generic_MSG",
            ctypes.addressof(message),
        )
        message.message = WM_WINDOWPOSCHANGING
        controller.handle_native_event(
            b"windows_generic_MSG",
            ctypes.addressof(message),
        )
        callbacks["sync_backdrop_geometry_now"].assert_not_called()

        message.message = WM_WINDOWPOSCHANGED
        controller.handle_native_event(
            b"windows_generic_MSG",
            ctypes.addressof(message),
        )
        callbacks["sync_backdrop_geometry_now"].assert_called_once_with()
        controller.end_interactive_resize()
        self.assertEqual(
            [call.args for call in callbacks["set_grid_interactive_resize"].call_args_list],
            [(True,), (False,)],
        )
