from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QTimer,
)
from .windows_clipboard import WindowsClipboardNotifier as WindowsClipboardNotifier


__all__ = [
    "AIService",
    "_AIServiceRequestError",
    "_ClipboardBusy",
    "BackdropBackend",
    "BackdropResult",
    "BoundedTaskExecutor",
    "ClipboardService",
    "FileSnapshot",
    "ImageFileSnapshot",
    "OperationCancelled",
    "QApplication",
    "QCoreApplication",
    "QTimer",
    "TaskCapacityExceeded",
    "TaskHandle",
    "WindowsBackdropPolicy",
    "WindowsClipboardNotifier",
    "ai_ocr_task_executor",
    "apply_windows_backdrop",
    "preflight_current_file",
    "preflight_image_file",
    "register_windows_power_saving_notification",
    "release_windows_backdrop",
    "shutdown_ai_ocr_task_executor",
    "unregister_windows_power_saving_notification",
    "time",
]
from .task_executor import (
    BoundedTaskExecutor as BoundedTaskExecutor,
    TaskCapacityExceeded as TaskCapacityExceeded,
    TaskHandle as TaskHandle,
    ai_ocr_task_executor as ai_ocr_task_executor,
    shutdown_ai_ocr_task_executor as shutdown_ai_ocr_task_executor,
)
from PySide6.QtWidgets import QApplication

from .ai_service import (
    AIService as _BaseAIService,
    AIServiceRequestError,
)
from .clipboard_service import ClipboardService as _BaseClipboardService
from .native_clipboard_reader import ClipboardBusy as _ClipboardBusy
from .constants import (
    MARKDOWN_DIR,
    PICTURE_DIR,
)
from .file_preflight import (
    FileSnapshot as FileSnapshot,
    ImageFileSnapshot as ImageFileSnapshot,
    OperationCancelled as OperationCancelled,
    preflight_current_file as preflight_current_file,
    preflight_image_file as preflight_image_file,
)
from .storage import open_managed_binary
from .windows_backdrop import (
    attach_windows_composition_backdrop,
    detach_windows_composition_backdrop,
    windows_composition_backdrop_error,
)


class AIService(_BaseAIService):
    """Compatibility facade retaining services-level dependency patch seams."""

    def _picture_root(self) -> Path:
        if self.picture_root is not None:
            return self.picture_root
        return PICTURE_DIR

    def _preflight_image(self, path: Path) -> ImageFileSnapshot:
        return preflight_image_file(path)


_AIServiceRequestError = AIServiceRequestError


class ClipboardService(_BaseClipboardService):
    """Compatibility facade retaining services-level storage patch seams."""

    def _default_picture_dir(self) -> Path:
        return PICTURE_DIR

    def _default_markdown_dir(self) -> Path:
        return MARKDOWN_DIR

    def _open_managed_binary(self, *args, **kwargs):
        return open_managed_binary(*args, **kwargs)






class BackdropBackend(Enum):
    SOLID = "solid"
    LEGACY_BLUR = "legacy_blur"
    WIN10_EFFECT_ACRYLIC = "win10_effect_acrylic"
    DESKTOP_ACRYLIC = "desktop_acrylic"


@dataclass(frozen=True)
class BackdropResult:
    backend: BackdropBackend
    success: bool
    native_error: int | None = None


@dataclass(frozen=True)
class WindowsBackdropPolicy:
    high_contrast: bool
    transparency_enabled: bool
    energy_saver: bool = False

    @property
    def allows_transparency(self) -> bool:
        return self.transparency_enabled and not self.high_contrast

    @property
    def allows_app_managed_backdrop(self) -> bool:
        return self.allows_transparency and not self.energy_saver

class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("accent_state", ctypes.c_int),
        ("accent_flags", ctypes.c_int),
        ("gradient_color", ctypes.c_uint32),
        ("animation_id", ctypes.c_int),
    ]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_GUID_POWER_SAVING_STATUS = _Guid(
    0xE00958C0,
    0xC213,
    0x4ACE,
    (ctypes.c_ubyte * 8)(0xAC, 0x77, 0xFE, 0xCC, 0xED, 0x2E, 0xEE, 0xA5),
)


class _WindowCompositionAttributeData(ctypes.Structure):
    _fields_ = [
        ("attribute", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
    ]


class _HighContrastW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwFlags", wintypes.DWORD),
        ("lpszDefaultScheme", wintypes.LPWSTR),
    ]


@lru_cache(maxsize=1)
def _windows_effect_apis():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    user32.SetWindowCompositionAttribute.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(_WindowCompositionAttributeData),
    ]
    user32.SetWindowCompositionAttribute.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.RegisterPowerSettingNotification.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_Guid),
        wintypes.DWORD,
    ]
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
    user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL
    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    return user32, dwmapi


@lru_cache(maxsize=1)
def _windows_power_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SystemPowerStatus)]
    kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
    return kernel32


def _dwm_attribute_result(
    dwmapi, hwnd: int, attribute: int, value: ctypes._SimpleCData
) -> int:
    return int(
        dwmapi.DwmSetWindowAttribute(
            hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
        )
    )


def _dwm_attribute(dwmapi, hwnd: int, attribute: int, value: ctypes._SimpleCData) -> bool:
    return _dwm_attribute_result(dwmapi, hwnd, attribute, value) >= 0


def _last_windows_error() -> int | None:
    get_last_error = getattr(ctypes, "get_last_error", None)
    if get_last_error is None:
        return None
    try:
        error = int(get_last_error())
    except (TypeError, ValueError):
        return None
    return error or None


def _windows_transparency_effects_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, "EnableTransparency")
            return bool(int(value))
    except (ImportError, OSError, TypeError, ValueError):
        # Missing/temporarily unreadable policy should not unexpectedly disable a
        # material that Windows was already rendering successfully.
        return True


def _windows_energy_saver_enabled() -> bool:
    try:
        status = _SystemPowerStatus()
        if not _windows_power_api().GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return bool(status.SystemStatusFlag)
    except (AttributeError, OSError, TypeError, ValueError):
        # A failed query must not unexpectedly force an opaque fallback.
        return False


def _windows_backdrop_policy(user32) -> WindowsBackdropPolicy:
    high_contrast = _HighContrastW()
    high_contrast.cbSize = ctypes.sizeof(high_contrast)
    try:
        queried = bool(
            user32.SystemParametersInfoW(
                0x0042,  # SPI_GETHIGHCONTRAST
                ctypes.sizeof(high_contrast),
                ctypes.byref(high_contrast),
                0,
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        queried = False
    return WindowsBackdropPolicy(
        high_contrast=queried and bool(high_contrast.dwFlags & 0x00000001),
        transparency_enabled=_windows_transparency_effects_enabled(),
        energy_saver=_windows_energy_saver_enabled(),
    )


def register_windows_power_saving_notification(hwnd: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        user32, _dwmapi = _windows_effect_apis()
        handle = user32.RegisterPowerSettingNotification(
            hwnd,
            ctypes.byref(_GUID_POWER_SAVING_STATUS),
            0,  # DEVICE_NOTIFY_WINDOW_HANDLE
        )
        return int(handle) if handle else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def unregister_windows_power_saving_notification(handle: int | None) -> bool:
    if os.name != "nt" or not handle:
        return True
    try:
        user32, _dwmapi = _windows_effect_apis()
        return bool(user32.UnregisterPowerSettingNotification(handle))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _set_windows_accent_state(
    user32,
    hwnd: int,
    state: int,
    *,
    gradient_color: int = 0,
) -> bool:
    policy = _AccentPolicy(state, 0, gradient_color, 0)
    data = _WindowCompositionAttributeData(
        19, ctypes.addressof(policy), ctypes.sizeof(policy)
    )
    return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))


def _disable_windows_backdrop(user32, dwmapi, hwnd: int, build: int) -> BackdropResult:
    native_error = None
    modern_disabled = detach_windows_composition_backdrop()
    if not modern_disabled:
        native_error = windows_composition_backdrop_error()
    system_disabled = True
    if build >= 22621:
        no_backdrop = ctypes.c_int(1)  # DWMSBT_NONE
        backdrop_result = _dwm_attribute_result(dwmapi, hwnd, 38, no_backdrop)
        system_disabled = backdrop_result >= 0
        if not system_disabled:
            native_error = backdrop_result

    legacy_disabled = _set_windows_accent_state(user32, hwnd, 0)
    if not legacy_disabled:
        native_error = _last_windows_error() or native_error

    return BackdropResult(
        backend=BackdropBackend.SOLID,
        success=modern_disabled and system_disabled and legacy_disabled,
        native_error=native_error,
    )


def release_windows_backdrop() -> bool:
    if os.name != "nt":
        return True
    return detach_windows_composition_backdrop()


def apply_windows_backdrop(
    window,
    dark: bool = False,
    *,
    composition_window=None,
    allow_unowned_composition: bool = True,
) -> BackdropResult:
    if os.name != "nt":
        return BackdropResult(BackdropBackend.SOLID, False)
    try:
        hwnd = int(window.winId())
        if composition_window is None:
            # The dedicated helper HWND renders the Win10 composition acrylic;
            # without it the effect graph must never attach to the Qt-owned
            # window (it covers the client content), so callers can opt out
            # and fall through to legacy blur / solid instead.
            composition_hwnd = hwnd if allow_unowned_composition else None
        elif isinstance(composition_window, int):
            composition_hwnd = int(composition_window)
        else:
            composition_hwnd = int(composition_window.winId())
        user32, dwmapi = _windows_effect_apis()
        build = sys.getwindowsversion().build
        backdrop_applied = False
        backend = BackdropBackend.SOLID
        native_error = None
        system_policy = _windows_backdrop_policy(user32)
        if not system_policy.allows_transparency:
            result = _disable_windows_backdrop(user32, dwmapi, hwnd, build)
            backdrop_applied = result.success
            backend = result.backend
            native_error = result.native_error
        else:
            # DWMWA_SYSTEMBACKDROP_TYPE is supported starting with Windows 11 22H2.
            if build >= 22621:
                # The Win11 DWM system-backdrop path is preferred over the
                # Win10 app-managed composition path. Avoid stacking two
                # backdrop owners if an HWND is being reconfigured after a
                # fallback path.
                detach_windows_composition_backdrop()
                backdrop = ctypes.c_int(3)
                backdrop_result = _dwm_attribute_result(dwmapi, hwnd, 38, backdrop)
                backdrop_applied = backdrop_result >= 0
                if backdrop_applied:
                    backend = BackdropBackend.DESKTOP_ACRYLIC
                else:
                    native_error = backdrop_result
            if not backdrop_applied:
                if system_policy.allows_app_managed_backdrop and composition_hwnd is not None:
                    # Keep one GPU-composited Acrylic effect active for the
                    # entire lifetime of the Win10 HWND, including live
                    # move/resize. The bridge renders HostBackdrop through a
                    # GaussianBlur effect behind Qt's client content.
                    if build >= 17763:
                        detach_windows_composition_backdrop()
                        _set_windows_accent_state(user32, hwnd, 0)
                        backdrop_applied = attach_windows_composition_backdrop(
                            composition_hwnd, dark
                        )
                        if backdrop_applied:
                            backend = BackdropBackend.WIN10_EFFECT_ACRYLIC
                            native_error = None
                        else:
                            native_error = (
                                windows_composition_backdrop_error() or native_error
                            )

                if not backdrop_applied and system_policy.allows_app_managed_backdrop:
                    # Compatibility fallback when the composition bridge is
                    # missing, unsupported, or rejects this HWND.
                    detach_windows_composition_backdrop()
                    backdrop_applied = _set_windows_accent_state(
                        user32,
                        hwnd,
                        3,  # ACCENT_ENABLE_BLURBEHIND
                        gradient_color=0x00FFFFFF,
                    )
                    if backdrop_applied:
                        backend = BackdropBackend.LEGACY_BLUR
                        native_error = None
                    else:
                        native_error = _last_windows_error() or native_error

                if not backdrop_applied and not system_policy.allows_app_managed_backdrop:
                    # AccentPolicy effects are app-managed. Respect Battery/Energy Saver
                    # by explicitly disabling them. A successful Windows 11 system
                    # backdrop above remains eligible while saver mode is active.
                    result = _disable_windows_backdrop(user32, dwmapi, hwnd, build)
                    backdrop_applied = result.success
                    backend = result.backend
                    native_error = result.native_error
        corner = ctypes.c_int(2)
        _dwm_attribute(dwmapi, hwnd, 33, corner)
        if build >= 22000:
            no_border = ctypes.c_uint32(0xFFFFFFFE)
            _dwm_attribute(dwmapi, hwnd, 34, no_border)
        dark_mode = ctypes.c_int(1 if dark else 0)
        _dwm_attribute(dwmapi, hwnd, 20, dark_mode)
        return BackdropResult(
            backend=backend,
            success=backdrop_applied,
            native_error=native_error,
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        native_error = getattr(exc, "winerror", None) or _last_windows_error()
        return BackdropResult(
            backend=BackdropBackend.SOLID,
            success=False,
            native_error=native_error,
        )
