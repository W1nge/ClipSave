from __future__ import annotations

import ctypes
from collections.abc import Callable
from ctypes import wintypes

from .windows_frame import (
    WM_DPICHANGED,
    WM_GETMINMAXINFO,
    WM_NCACTIVATE,
    WM_NCCALCSIZE,
    WM_WINDOWPOSCHANGING,
    WM_WINDOWPOSCHANGED,
)


WM_SETTINGCHANGE = 0x001A
WM_POWERBROADCAST = 0x0218
WM_THEMECHANGED = 0x031A
WM_DWMCOMPOSITIONCHANGED = 0x031E
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
WM_NCHITTEST = 0x0084


def windows_resize_hit_test(
    x: int,
    y: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    device_pixel_ratio: float,
    *,
    edge_width: int = 8,
    corner_size: int = 14,
) -> int | None:
    if x < left or x >= right or y < top or y >= bottom:
        return None
    edge = max(1, round(edge_width * device_pixel_ratio))
    corner = max(edge, round(corner_size * device_pixel_ratio))
    on_left = x < left + edge
    on_right = x >= right - edge
    on_top = y < top + edge
    on_bottom = y >= bottom - edge
    near_left = x < left + corner
    near_right = x >= right - corner
    near_top = y < top + corner
    near_bottom = y >= bottom - corner
    if (on_top and near_left) or (on_left and near_top):
        return 13  # HTTOPLEFT
    if (on_top and near_right) or (on_right and near_top):
        return 14  # HTTOPRIGHT
    if (on_bottom and near_left) or (on_left and near_bottom):
        return 16  # HTBOTTOMLEFT
    if (on_bottom and near_right) or (on_right and near_bottom):
        return 17  # HTBOTTOMRIGHT
    if on_left:
        return 10  # HTLEFT
    if on_right:
        return 11  # HTRIGHT
    if on_top:
        return 12  # HTTOP
    if on_bottom:
        return 15  # HTBOTTOM
    return None


class NativeWindowController:
    """Own Windows native-message routing while QMainWindow keeps the Qt entrypoint."""

    def __init__(
        self,
        window,
        *,
        native_events_enabled: Callable[[], bool],
        platform_check: Callable[[], bool],
        window_rect: Callable[[int], tuple[int, int, int, int] | None],
        dpi_scale: Callable[[int], float],
        handle_getminmaxinfo: Callable[..., tuple[bool, int]],
        handle_ncactivate: Callable[..., tuple[bool, int]],
        handle_nccalcsize: Callable[..., tuple[bool, int]],
        schedule_material_refresh: Callable[[], None],
        schedule_maximized_bounds_sync: Callable[[], None],
        sync_backdrop_from_windowpos: Callable[[int], None],
        sync_backdrop_geometry_now: Callable[[], None],
        sync_backdrop_window: Callable[[], None],
        schedule_soon: Callable[[Callable[[], None]], None],
        set_layout_updates_suspended: Callable[[bool], None],
        sidebar_animation_active: Callable[[], bool],
        detail_animation_active: Callable[[], bool],
        resize_hit_test: Callable[..., int | None],
        enable_resize_frame: Callable[[int], bool],
        clear_resize_handles: Callable[[], None],
        install_resize_handles: Callable[[], None],
        native_window_is_maximized: Callable[[int], bool | None],
        restore_native_window: Callable[[int], bool],
        maximize_native_window: Callable[[int], bool],
        synchronize_maximized_work_area: Callable[[int], object],
    ) -> None:
        self.window = window
        self.native_events_enabled = native_events_enabled
        self.platform_check = platform_check
        self.window_rect = window_rect
        self.dpi_scale = dpi_scale
        self.handle_getminmaxinfo = handle_getminmaxinfo
        self.handle_ncactivate = handle_ncactivate
        self.handle_nccalcsize = handle_nccalcsize
        self.schedule_material_refresh = schedule_material_refresh
        self.schedule_maximized_bounds_sync = schedule_maximized_bounds_sync
        self.sync_backdrop_from_windowpos = sync_backdrop_from_windowpos
        self.sync_backdrop_geometry_now = sync_backdrop_geometry_now
        self.sync_backdrop_window = sync_backdrop_window
        self.schedule_soon = schedule_soon
        self.set_layout_updates_suspended = set_layout_updates_suspended
        self.sidebar_animation_active = sidebar_animation_active
        self.detail_animation_active = detail_animation_active
        self.resize_hit_test = resize_hit_test
        self.enable_resize_frame = enable_resize_frame
        self.clear_resize_handles = clear_resize_handles
        self.install_resize_handles = install_resize_handles
        self.native_window_is_maximized = native_window_is_maximized
        self.restore_native_window = restore_native_window
        self.maximize_native_window = maximize_native_window
        self.synchronize_maximized_work_area = synchronize_maximized_work_area
        self.native_resize_frame_enabled = False
        self.native_resize_frame_hwnd: int | None = None
        self.interactive_resize_active = False
        self.maximized_bounds_sync_pending = False

    def ensure_native_resize_frame(self) -> None:
        if not self.platform_check():
            return
        hwnd = int(self.window.winId())
        if (
            self.native_resize_frame_enabled
            and self.native_resize_frame_hwnd == hwnd
        ):
            return
        enabled = self.enable_resize_frame(hwnd)
        self.native_resize_frame_enabled = enabled
        self.native_resize_frame_hwnd = hwnd if enabled else None
        if enabled:
            self.clear_resize_handles()
        else:
            self.install_resize_handles()

    def window_is_maximized(self) -> bool:
        if self.platform_check():
            native_state = self.native_window_is_maximized(int(self.window.winId()))
            if native_state is not None:
                return native_state
        return bool(self.window.isMaximized())

    def toggle_maximized(self) -> None:
        if self.window_is_maximized():
            hwnd = int(self.window.winId()) if self.platform_check() else 0
            if not self.restore_native_window(hwnd):
                self.window.showNormal()
            return
        hwnd = int(self.window.winId()) if self.platform_check() else 0
        if not self.maximize_native_window(hwnd):
            self.window.showMaximized()

    def sync_maximized_work_area(self) -> None:
        if self.platform_check():
            self.synchronize_maximized_work_area(int(self.window.winId()))

    def begin_interactive_resize(self) -> None:
        if self.interactive_resize_active:
            return
        self.interactive_resize_active = True
        self.set_layout_updates_suspended(True)

    def end_interactive_resize(self) -> None:
        if not self.interactive_resize_active:
            return
        self.interactive_resize_active = False
        if self.platform_check():
            self.schedule_soon(self.sync_backdrop_window)
        self.set_layout_updates_suspended(
            self.sidebar_animation_active() or self.detail_animation_active()
        )

    def handle_native_event(self, event_type, message) -> tuple[bool, int] | None:
        if not self.native_events_enabled() or event_type not in (
            b"windows_generic_MSG",
            b"windows_dispatcher_MSG",
        ):
            return None
        msg = wintypes.MSG.from_address(int(message))
        native_message = int(msg.message)

        if native_message in (
            WM_SETTINGCHANGE,
            WM_POWERBROADCAST,
            WM_THEMECHANGED,
            WM_DWMCOMPOSITIONCHANGED,
        ):
            self.schedule_material_refresh()

        if native_message == WM_GETMINMAXINFO:
            hwnd = int(msg.hWnd) or int(self.window.winId())
            scale = self.dpi_scale(hwnd)
            handled, result = self.handle_getminmaxinfo(
                hwnd,
                int(msg.wParam),
                int(msg.lParam),
                (
                    max(1, round(self.window.minimumWidth() * scale)),
                    max(1, round(self.window.minimumHeight() * scale)),
                ),
            )
            if handled:
                return True, result

        if native_message == WM_NCACTIVATE:
            handled, result = self.handle_ncactivate(
                int(msg.hWnd),
                int(msg.wParam),
            )
            if handled:
                return True, result

        if native_message == WM_NCCALCSIZE:
            handled, result = self.handle_nccalcsize(
                int(msg.hWnd),
                int(msg.wParam),
                int(msg.lParam),
            )
            if handled:
                return True, result

        interactive_resize = self.interactive_resize_active
        if native_message == WM_WINDOWPOSCHANGING and interactive_resize:
            self.sync_backdrop_from_windowpos(int(msg.lParam))

        if native_message in (WM_WINDOWPOSCHANGED, WM_DPICHANGED):
            self.schedule_maximized_bounds_sync()
            if interactive_resize:
                self.sync_backdrop_geometry_now()
            else:
                self.schedule_soon(self.sync_backdrop_window)

        if native_message == WM_ENTERSIZEMOVE:
            self.begin_interactive_resize()
        elif native_message == WM_EXITSIZEMOVE:
            self.end_interactive_resize()

        if native_message == WM_NCHITTEST and not (
            self.window.isMaximized() or self.window.isFullScreen()
        ):
            hwnd = int(msg.hWnd) or int(self.window.winId())
            rect = self.window_rect(hwnd)
            if rect is None:
                return None
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            hit = self.resize_hit_test(
                x,
                y,
                *rect,
                self.dpi_scale(hwnd),
            )
            if hit is not None:
                return True, hit
        return None
