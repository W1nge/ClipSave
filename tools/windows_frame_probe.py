from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QApplication

from clipsave_app.app import _configure_windows_dpi_awareness, create_app_icon
from clipsave_app.database import LibraryDatabase
from clipsave_app.main_window import MainWindow
from clipsave_app.settings import Settings
from clipsave_app.windows_frame import GWL_STYLE, WS_THICKFRAME


WM_NCHITTEST = 0x0084
WM_SETCURSOR = 0x0020
WM_MOUSEMOVE = 0x0200
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
WM_SIZING = 0x0214
HTCLIENT = 1
SIZING_HITS = {10, 11, 12, 13, 14, 15, 16, 17}
MONITOR_DEFAULTTONEAREST = 2

CURSORS = {
    10: 32644,  # IDC_SIZEWE
    11: 32644,
    12: 32645,  # IDC_SIZENS
    15: 32645,
    13: 32642,  # IDC_SIZENWSE
    17: 32642,
    14: 32643,  # IDC_SIZENESW
    16: 32643,
}


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class ProbeWindow(MainWindow):
    def __init__(self, *args, **kwargs):
        self.native_message_counts = {
            WM_ENTERSIZEMOVE: 0,
            WM_SIZING: 0,
            WM_EXITSIZEMOVE: 0,
        }
        super().__init__(*args, **kwargs)

    def nativeEvent(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message in self.native_message_counts:
                self.native_message_counts[msg.message] += 1
        return super().nativeEvent(event_type, message)


def _configure_user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetCursor.argtypes = []
    user32.GetCursor.restype = wintypes.HANDLE
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.mouse_event.restype = None
    return user32


def _wait(app: QApplication, milliseconds: int = 180) -> None:
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _window_rect(user32, hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.left, rect.top, rect.right, rect.bottom


def _client_screen_rect(user32, hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    top_left = wintypes.POINT(rect.left, rect.top)
    bottom_right = wintypes.POINT(rect.right, rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)) or not user32.ClientToScreen(
        hwnd, ctypes.byref(bottom_right)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return top_left.x, top_left.y, bottom_right.x, bottom_right.y


def _point_lparam(x: int, y: int) -> int:
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def _exercise_right_edge_drag(app: QApplication, user32, window, hwnd: int) -> dict:
    before = _window_rect(user32, hwnd)
    old_cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old_cursor))
    start_x = before[2] - 2
    start_y = (before[1] + before[3]) // 2
    target_x = start_x + 120
    user32.SetForegroundWindow(hwnd)
    window.raise_()
    window.activateWindow()
    _wait(app, 120)

    def drive_mouse():
        try:
            user32.SetCursorPos(start_x, start_y)
            time.sleep(0.12)
            user32.mouse_event(0x0002, 0, 0, 0, None)  # MOUSEEVENTF_LEFTDOWN
            time.sleep(0.15)
            user32.SetCursorPos(target_x, start_y)
            time.sleep(0.24)
            user32.mouse_event(0x0004, 0, 0, 0, None)  # MOUSEEVENTF_LEFTUP
        finally:
            time.sleep(0.08)
            user32.SetCursorPos(old_cursor.x, old_cursor.y)

    controller = threading.Thread(target=drive_mouse, name="ClipSaveFrameProbe", daemon=True)
    controller.start()
    _wait(app, 1100)
    controller.join(1.0)
    after = _window_rect(user32, hwnd)
    counts = window.native_message_counts
    return {
        "before": before,
        "after": after,
        "entered_size_move": counts[WM_ENTERSIZEMOVE] > 0,
        "received_sizing": counts[WM_SIZING] > 0,
        "exited_size_move": counts[WM_EXITSIZEMOVE] > 0,
        "opposite_edges_stable": before[0] == after[0] and before[1] == after[1] and before[3] == after[3],
        "right_edge_moved": after[2] > before[2],
    }


def run_probe() -> dict:
    if os.name != "nt":
        raise RuntimeError("The native frame probe only runs on Windows")
    os.environ.pop("QT_QPA_PLATFORM", None)
    _configure_windows_dpi_awareness()
    app = QApplication.instance() or QApplication([])
    if app.platformName().lower() != "windows":
        raise RuntimeError(f"Expected the Windows Qt platform, got {app.platformName()!r}")

    user32 = _configure_user32()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        database = LibraryDatabase(root / "probe.db")
        settings = Settings(root / "settings.json")
        settings.set("monitoring", False)
        window = ProbeWindow(database, settings, create_app_icon(), scan_on_start=False)
        window.setGeometry(160, 120, 1000, 700)
        window.show()
        _wait(app)
        hwnd = int(window.winId())

        style = int(user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
        normal_window_rect = _window_rect(user32, hwnd)
        normal_client_rect = _client_screen_rect(user32, hwnd)
        left, top, right, bottom = normal_window_rect
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        points = {
            10: (left + 2, center_y),
            11: (right - 3, center_y),
            12: (center_x, top + 2),
            15: (center_x, bottom - 3),
            13: (left + 2, top + 2),
            14: (right - 3, top + 2),
            16: (left + 2, bottom - 3),
            17: (right - 3, bottom - 3),
        }
        hit_results = {}
        cursor_results = {}
        for expected_hit, (x, y) in points.items():
            actual_hit = int(user32.SendMessageW(hwnd, WM_NCHITTEST, 0, _point_lparam(x, y)))
            hit_results[str(expected_hit)] = actual_hit
            user32.SendMessageW(hwnd, WM_SETCURSOR, hwnd, expected_hit | (WM_MOUSEMOVE << 16))
            actual_cursor = int(user32.GetCursor() or 0)
            expected_cursor = int(user32.LoadCursorW(None, CURSORS[expected_hit]) or 0)
            cursor_results[str(expected_hit)] = actual_cursor == expected_cursor

        native_drag = _exercise_right_edge_drag(app, user32, window, hwnd)
        normal_window_rect = _window_rect(user32, hwnd)
        normal_client_rect = _client_screen_rect(user32, hwnd)

        window.showMaximized()
        _wait(app, 260)
        qt_maximized = window.isMaximized()
        is_zoomed = bool(user32.IsZoomed(hwnd))
        maximized_window_rect = _window_rect(user32, hwnd)
        maximized_client_rect = _client_screen_rect(user32, hwnd)
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        work_rect = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
        max_hit = int(
            user32.SendMessageW(
                hwnd,
                WM_NCHITTEST,
                0,
                _point_lparam(maximized_client_rect[0] + 2, maximized_client_rect[1] + 2),
            )
        )

        window.showNormal()
        _wait(app, 220)
        restored_rect = _window_rect(user32, hwnd)
        result = {
            "hwnd": hwnd,
            "style": hex(style),
            "thickframe": bool(style & WS_THICKFRAME),
            "native_frame_enabled": window._native_resize_frame_enabled,
            "normal_window_rect": normal_window_rect,
            "normal_client_rect": normal_client_rect,
            "client_fills_normal_window": normal_window_rect == normal_client_rect,
            "hit_results": hit_results,
            "all_hit_results_correct": all(int(key) == value for key, value in hit_results.items()),
            "cursor_results": cursor_results,
            "all_cursors_correct": all(cursor_results.values()),
            "native_drag": native_drag,
            "native_drag_succeeded": all(
                native_drag[key]
                for key in (
                    "entered_size_move",
                    "received_sizing",
                    "exited_size_move",
                    "opposite_edges_stable",
                    "right_edge_moved",
                )
            ),
            "qt_maximized": qt_maximized,
            "win32_is_zoomed": is_zoomed,
            "maximized_window_rect": maximized_window_rect,
            "maximized_client_rect": maximized_client_rect,
            "monitor_work_rect": work_rect,
            "maximized_client_matches_work_area": maximized_client_rect == work_rect,
            "maximized_edge_hit": max_hit,
            "maximized_edge_is_not_resizable": max_hit not in SIZING_HITS,
            "restored_rect": restored_rect,
            "restore_preserved_geometry": restored_rect == normal_window_rect,
        }
        window.force_quit = True
        window.close()
        database.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe ClipSave's real Win32 frameless window")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_probe()
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    required = (
        result["thickframe"],
        result["native_frame_enabled"],
        result["client_fills_normal_window"],
        result["all_hit_results_correct"],
        result["all_cursors_correct"],
        result["native_drag_succeeded"],
        result["qt_maximized"],
        result["maximized_client_matches_work_area"],
        result["maximized_edge_is_not_resizable"],
        result["restore_preserved_geometry"],
    )
    return 0 if all(required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
