from __future__ import annotations

from collections.abc import Callable

from .windows_frame import (
    create_backdrop_host_window,
    destroy_backdrop_host_window,
    hide_backdrop_host_window,
    sync_backdrop_host_window,
    window_rect,
)


class WindowEffectsController:
    """Own Win32 backdrop-helper and power-notification state for MainWindow."""

    def __init__(
        self,
        window,
        *,
        platform_check: Callable[[], bool],
        apply_backdrop: Callable[..., object],
        register_power: Callable[[int], int | None],
        unregister_power: Callable[[int], object],
        sync_surface_style: Callable[..., None],
    ) -> None:
        self.window = window
        self.platform_check = platform_check
        self.apply_backdrop_callback = apply_backdrop
        self.register_power = register_power
        self.unregister_power = unregister_power
        self.sync_surface_style = sync_surface_style
        self.native_backdrop_hwnd: int | None = None
        self.native_backdrop_result = None
        self.backdrop_window_hwnd: int | None = None
        self.power_notification_hwnd: int | None = None
        self.power_notification_handle: int | None = None
        self.material_refresh_pending = False

    def ensure_backdrop_window(self) -> int | None:
        if not self.platform_check():
            return None
        if self.backdrop_window_hwnd:
            return self.backdrop_window_hwnd
        hwnd = create_backdrop_host_window()
        if not hwnd:
            return None
        self.backdrop_window_hwnd = hwnd
        return hwnd

    def destroy_backdrop_window(self) -> None:
        hwnd = self.backdrop_window_hwnd
        self.backdrop_window_hwnd = None
        if hwnd:
            destroy_backdrop_host_window(hwnd)

    def hide_backdrop_window(self) -> None:
        hwnd = self.backdrop_window_hwnd
        if hwnd:
            hide_backdrop_host_window(hwnd)

    def _acrylic_active(self) -> bool:
        result = self.native_backdrop_result
        return bool(
            result is not None
            and result.backend.value == "win10_effect_acrylic"
            and result.success
        )

    def sync_window_rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        visible: bool | None = None,
    ) -> None:
        if not self._acrylic_active():
            self.hide_backdrop_window()
            return
        backdrop_hwnd = self.backdrop_window_hwnd
        if not backdrop_hwnd:
            return
        should_show = (
            self.window.isVisible() and not self.window.isMinimized()
            if visible is None
            else visible
        )
        if not should_show:
            hide_backdrop_host_window(backdrop_hwnd)
            return
        sync_backdrop_host_window(
            backdrop_hwnd,
            int(self.window.winId()),
            int(x),
            int(y),
            max(1, int(width)),
            max(1, int(height)),
            visible=True,
        )

    def sync_window(self, *, visible: bool | None = None) -> None:
        if not self.platform_check() or not self._acrylic_active():
            return
        hwnd = int(self.window.winId())
        rect = window_rect(hwnd)
        if rect is None:
            return
        left, top, right, bottom = rect
        self.sync_window_rect(
            left,
            top,
            max(1, right - left),
            max(1, bottom - top),
            visible=visible,
        )

    def sync_geometry_now(self) -> None:
        if not self._acrylic_active():
            return
        backdrop_hwnd = self.backdrop_window_hwnd
        host_hwnd = int(self.window.winId())
        if not backdrop_hwnd or self.window.isMinimized():
            return
        rect = window_rect(host_hwnd)
        if rect is None:
            return
        left, top, right, bottom = rect
        sync_backdrop_host_window(
            backdrop_hwnd,
            host_hwnd,
            left,
            top,
            max(1, right - left),
            max(1, bottom - top),
            visible=self.window.isVisible(),
            sync_z_order=False,
        )

    def constrain_geometry_to_host(self) -> None:
        """Trim an old backdrop on shrink; only a painted frame may expand it."""
        if not self._acrylic_active() or not self.backdrop_window_hwnd:
            return
        host_hwnd = int(self.window.winId())
        host = window_rect(host_hwnd)
        backdrop = window_rect(self.backdrop_window_hwnd)
        if host is None or backdrop is None:
            return
        bounds = (
            max(host[0], backdrop[0]), max(host[1], backdrop[1]),
            min(host[2], backdrop[2]), min(host[3], backdrop[3]),
        )
        if bounds == backdrop:
            return
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            self.hide_backdrop_window()
            return
        sync_backdrop_host_window(
            self.backdrop_window_hwnd, host_hwnd,
            left, top, right - left, bottom - top,
            visible=self.window.isVisible(), sync_z_order=False,
        )

    def apply_native_backdrop(
        self,
        *,
        force: bool = False,
        dark: bool | None = None,
    ) -> None:
        if not self.platform_check():
            return
        hwnd = int(self.window.winId())
        if not force and self.native_backdrop_hwnd == hwnd:
            return
        composition_window = self.ensure_backdrop_window()
        result = self.apply_backdrop_callback(
            self.window,
            self.window.dark_theme if dark is None else dark,
            composition_window=composition_window,
            allow_unowned_composition=False,
        )
        self.native_backdrop_result = result
        self.sync_surface_style(result=result, dark=dark)
        if result.success:
            self.native_backdrop_hwnd = hwnd
            if result.backend.value == "win10_effect_acrylic":
                self.sync_window()
            else:
                self.hide_backdrop_window()
        else:
            self.native_backdrop_hwnd = None
            self.hide_backdrop_window()

    def ensure_power_notification(self) -> None:
        if not self.platform_check():
            return
        hwnd = int(self.window.winId())
        if (
            self.power_notification_handle is not None
            and self.power_notification_hwnd == hwnd
        ):
            return
        self.release_power_notification()
        handle = self.register_power(hwnd)
        if handle is None:
            return
        self.power_notification_hwnd = hwnd
        self.power_notification_handle = handle

    def release_power_notification(self) -> None:
        handle = self.power_notification_handle
        self.power_notification_handle = None
        self.power_notification_hwnd = None
        if handle is not None:
            self.unregister_power(handle)
