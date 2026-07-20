from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

from PySide6.QtGui import QGuiApplication


WM_NCCALCSIZE = 0x0083
WM_NCACTIVATE = 0x0086
WM_GETMINMAXINFO = 0x0024
WS_THICKFRAME = 0x00040000
GWL_STYLE = -16

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SW_MAXIMIZE = 3
SW_RESTORE = 9

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


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", POINT),
        ("ptMaxSize", POINT),
        ("ptMaxPosition", POINT),
        ("ptMinTrackSize", POINT),
        ("ptMaxTrackSize", POINT),
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
    library.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    library.DefWindowProcW.restype = ctypes.c_ssize_t
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
    library.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    library.ShowWindow.restype = wintypes.BOOL
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


def native_window_is_maximized(hwnd: int) -> bool | None:
    if not is_windows_qt_platform() or not hwnd:
        return None
    try:
        return bool(_user32().IsZoomed(hwnd))
    except (AttributeError, OSError, ValueError):
        return None


def maximize_native_window(hwnd: int) -> bool:
    """Enter Win32 maximize state so Qt and the shell observe the same state."""
    if not is_windows_qt_platform() or not hwnd:
        return False
    try:
        _user32().ShowWindow(hwnd, SW_MAXIMIZE)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def restore_native_window(hwnd: int) -> bool:
    """Restore a Win32-maximized window without queuing a conflicting Qt state change."""
    if not is_windows_qt_platform() or not hwnd:
        return False
    try:
        _user32().ShowWindow(hwnd, SW_RESTORE)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def handle_getminmaxinfo(
    hwnd: int,
    wparam: int,
    lparam: int,
    minimum_track_size: tuple[int, int] | None = None,
) -> tuple[bool, int]:
    """Keep native maximize/snap bounds inside the monitor work area."""
    if not is_windows_qt_platform() or not hwnd or not lparam:
        return False, 0
    try:
        user32 = _user32()
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False, 0
        work_width = info.rcWork.right - info.rcWork.left
        work_height = info.rcWork.bottom - info.rcWork.top
        if work_width <= 0 or work_height <= 0:
            return False, 0

        # Preserve native tracking defaults, then replace only the maximize bounds.
        user32.DefWindowProcW(hwnd, WM_GETMINMAXINFO, wparam, lparam)
        target = MINMAXINFO.from_address(lparam)
        target.ptMaxPosition.x = info.rcWork.left - info.rcMonitor.left
        target.ptMaxPosition.y = info.rcWork.top - info.rcMonitor.top
        target.ptMaxSize.x = work_width
        target.ptMaxSize.y = work_height
        if minimum_track_size is not None:
            minimum_width, minimum_height = minimum_track_size
            target.ptMinTrackSize.x = max(target.ptMinTrackSize.x, minimum_width)
            target.ptMinTrackSize.y = max(target.ptMinTrackSize.y, minimum_height)
        return True, 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False, 0


def handle_nccalcsize(hwnd: int, wparam: int, lparam: int) -> tuple[bool, int]:
    """Remove the visual frame and keep maximized content inside its outer work-area bounds."""
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
            # IsZoomed can remain true while Windows is calculating the first
            # normal-sized frame during restore. Only replace a candidate that
            # still covers the monitor work area; otherwise the smaller outer
            # window would retain a maximized client area and clip its content.
            covers_work_area = (
                target.left <= info.rcWork.left
                and target.top <= info.rcWork.top
                and target.right >= info.rcWork.right
                and target.bottom >= info.rcWork.bottom
            )
            if covers_work_area:
                target.left = info.rcWork.left
                target.top = info.rcWork.top
                target.right = info.rcWork.right
                target.bottom = info.rcWork.bottom
        return True, 0
    except (OSError, ValueError):
        return False, 0


def handle_ncactivate(hwnd: int, wparam: int) -> tuple[bool, int]:
    """Update activation state without asking DWM to repaint its native frame."""
    if not is_windows_qt_platform() or not hwnd:
        return False, 0
    try:
        result = _user32().DefWindowProcW(hwnd, WM_NCACTIVATE, wparam, -1)
        return True, int(result)
    except (AttributeError, OSError, TypeError, ValueError):
        return False, 0
