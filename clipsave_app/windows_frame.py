from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

from PySide6.QtGui import QGuiApplication


WM_NCCALCSIZE = 0x0083
WS_THICKFRAME = 0x00040000
GWL_STYLE = -16

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

MONITOR_DEFAULTTONEAREST = 0x00000002


class WINDOWPOS(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("hwndInsertAfter", wintypes.HWND),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("cy", ctypes.c_int),
        ("flags", wintypes.UINT),
    ]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [
        ("rgrc", wintypes.RECT * 3),
        ("lppos", ctypes.POINTER(WINDOWPOS)),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def is_windows_qt_platform() -> bool:
    return os.name == "nt" and QGuiApplication.platformName().lower() == "windows"


@lru_cache(maxsize=1)
def _user32():
    library = ctypes.WinDLL("user32", use_last_error=True)
    long_ptr = ctypes.c_ssize_t
    library.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    library.GetWindowLongPtrW.restype = long_ptr
    library.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
    library.SetWindowLongPtrW.restype = long_ptr
    library.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    library.SetWindowPos.restype = wintypes.BOOL
    library.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    library.GetWindowRect.restype = wintypes.BOOL
    library.GetDpiForWindow.argtypes = [wintypes.HWND]
    library.GetDpiForWindow.restype = wintypes.UINT
    library.IsZoomed.argtypes = [wintypes.HWND]
    library.IsZoomed.restype = wintypes.BOOL
    library.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    library.MonitorFromWindow.restype = wintypes.HMONITOR
    library.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    library.MonitorFromRect.restype = wintypes.HMONITOR
    library.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    library.GetMonitorInfoW.restype = wintypes.BOOL
    return library


def enable_native_resize_frame(hwnd: int) -> bool:
    """Restore the Win32 sizing frame while Qt continues to draw the whole window."""
    if not is_windows_qt_platform() or not hwnd:
        return False
    try:
        user32 = _user32()
        ctypes.set_last_error(0)
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        if style == 0 and ctypes.get_last_error():
            return False
        desired_style = style | WS_THICKFRAME
        if desired_style != style:
            ctypes.set_last_error(0)
            previous = user32.SetWindowLongPtrW(hwnd, GWL_STYLE, desired_style)
            if previous == 0 and ctypes.get_last_error():
                return False
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags):
                return False
        return bool(user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_THICKFRAME)
    except (AttributeError, OSError, ValueError):
        return False


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not is_windows_qt_platform() or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except (AttributeError, OSError, ValueError):
        return None


def window_dpi_scale(hwnd: int) -> float:
    if not is_windows_qt_platform() or not hwnd:
        return 1.0
    try:
        dpi = int(_user32().GetDpiForWindow(hwnd))
    except (AttributeError, OSError, ValueError):
        return 1.0
    return dpi / 96.0 if dpi > 0 else 1.0


def handle_nccalcsize(hwnd: int, wparam: int, lparam: int) -> tuple[bool, int]:
    """Remove the visual native frame and keep maximized content inside the work area."""
    if not is_windows_qt_platform() or not hwnd or not lparam:
        return False, 0
    try:
        user32 = _user32()
        if wparam:
            target = NCCALCSIZE_PARAMS.from_address(lparam).rgrc[0]
        else:
            target = wintypes.RECT.from_address(lparam)

        if user32.IsZoomed(hwnd):
            monitor = user32.MonitorFromRect(ctypes.byref(target), MONITOR_DEFAULTTONEAREST)
            if not monitor:
                monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return False, 0
            target.left = info.rcWork.left
            target.top = info.rcWork.top
            target.right = info.rcWork.right
            target.bottom = info.rcWork.bottom
        return True, 0
    except (OSError, ValueError):
        return False, 0
