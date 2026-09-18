from __future__ import annotations

import argparse
import ctypes
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from verify_windows_visual_smoke import _find_visible_window, _status_values, _window_rect


WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GW_HWNDNEXT = 2


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_tree(root_pid: int) -> list[int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return [root_pid]
    children: dict[int, list[int]] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    result: list[int] = []
    queue = [root_pid]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        queue.extend(children.get(pid, ()))
    return result


def _find_process_window(root_pid: int) -> int:
    for pid in _process_tree(root_pid):
        hwnd = _find_visible_window(pid)
        if hwnd:
            return hwnd
    return 0


def _find_backdrop_window(host_hwnd: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    host_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId(host_hwnd, ctypes.byref(host_pid))

    required = WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    found = 0
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _lparam):
        nonlocal found
        value = int(hwnd)
        if value == host_hwnd or not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != host_pid.value:
            return True
        ex_style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        if ex_style & required == required:
            found = value
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found


def _rect_delta(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> int:
    return max(abs(a - b) for a, b in zip(first, second))


def _geometry_lock_samples(
    host_hwnd: int,
    backdrop_hwnd: int,
    *,
    resize: bool,
    count: int,
) -> list[int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    left, top, right, bottom = _window_rect(host_hwnd)
    width = right - left
    height = bottom - top
    flags = SWP_NOACTIVATE | SWP_NOZORDER
    deltas: list[int] = []
    for index in range(count):
        phase = 1 if index % 2 else 0
        x = left + (phase if not resize else 0)
        y = top
        w = width + (phase if resize else 0)
        h = height
        if not user32.SetWindowPos(host_hwnd, 0, x, y, w, h, flags):
            raise ctypes.WinError(ctypes.get_last_error())
        deltas.append(
            _rect_delta(_window_rect(host_hwnd), _window_rect(backdrop_hwnd))
        )
    user32.SetWindowPos(host_hwnd, 0, left, top, width, height, flags)
    return deltas


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return float(ordered[index])


def _summarize(label: str, values: list[float]) -> None:
    print(
        f"{label}: "
        f"mean={statistics.fmean(values):.3f}ms "
        f"p50={_percentile(values, 0.50):.3f}ms "
        f"p95={_percentile(values, 0.95):.3f}ms "
        f"p99={_percentile(values, 0.99):.3f}ms "
        f">16.7={sum(value > 16.7 for value in values)}/{len(values)} "
        f">33.3={sum(value > 33.3 for value in values)}/{len(values)}"
    )


def _geometry_samples(
    hwnd: int,
    *,
    resize: bool,
    count: int,
) -> list[float]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    dwmapi.DwmFlush.argtypes = []
    dwmapi.DwmFlush.restype = ctypes.c_long

    left, top, right, bottom = _window_rect(hwnd)
    width = right - left
    height = bottom - top
    flags = SWP_NOACTIVATE | SWP_NOZORDER
    values: list[float] = []
    for index in range(count):
        phase = 1 if index % 2 else 0
        x = left + (phase if not resize else 0)
        y = top
        w = width + (phase if resize else 0)
        h = height
        started = time.perf_counter()
        if not user32.SetWindowPos(hwnd, 0, x, y, w, h, flags):
            raise ctypes.WinError(ctypes.get_last_error())
        hr = int(dwmapi.DwmFlush())
        if hr < 0:
            raise OSError(f"DwmFlush failed: 0x{hr & 0xFFFFFFFF:08X}")
        values.append((time.perf_counter() - started) * 1000.0)

    user32.SetWindowPos(hwnd, 0, left, top, width, height, flags)
    dwmapi.DwmFlush()
    return values


def _send(hwnd: int, message: int) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW(hwnd, message, 0, 0)


def verify(command: list[str], *, count: int, timeout: float) -> int:
    with tempfile.TemporaryDirectory(
        prefix="clipsave-interactive-backdrop-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        temp = Path(temp_dir)
        profile = temp / "profile"
        ready = temp / "ready"
        status = temp / "ready.status"
        process = subprocess.Popen(
            [
                *command,
                "--smoke-profile",
                str(profile),
                "--smoke-ready-file",
                str(ready),
                "--smoke-hold-ms",
                "30000",
            ]
        )
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and not ready.exists():
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            hwnd = 0
            while time.monotonic() < deadline and not hwnd:
                hwnd = _find_process_window(process.pid)
                if not hwnd:
                    time.sleep(0.05)
            if not ready.exists() or not hwnd:
                print(
                    "interactive_backdrop=FAIL "
                    f"reason=startup ready={ready.exists()} hwnd={hwnd}"
                )
                return 2

            time.sleep(0.5)
            backdrop_hwnd = _find_backdrop_window(hwnd)
            if not backdrop_hwnd:
                print("interactive_backdrop=FAIL reason=missing-backdrop-hwnd")
                return 3
            initial_delta = _rect_delta(
                _window_rect(hwnd),
                _window_rect(backdrop_hwnd),
            )
            print(
                f"backdrop_hwnd={backdrop_hwnd} "
                f"initial_rect_delta={initial_delta}px"
            )
            if initial_delta:
                print(
                    "interactive_backdrop=FAIL "
                    f"reason=initial-geometry-delta delta={initial_delta}px"
                )
                return 3

            for label, resize in (("move", False), ("resize", True)):
                _send(hwnd, WM_ENTERSIZEMOVE)
                time.sleep(0.1)
                values = _geometry_samples(hwnd, resize=resize, count=count)
                deltas = _geometry_lock_samples(
                    hwnd,
                    backdrop_hwnd,
                    resize=resize,
                    count=min(count, 120),
                )
                _send(hwnd, WM_EXITSIZEMOVE)
                time.sleep(0.15)
                _summarize(label, values)
                print(
                    f"{label}_geometry_lock: "
                    f"max_delta={max(deltas, default=0)}px "
                    f"nonzero={sum(delta != 0 for delta in deltas)}/{len(deltas)}"
                )
                if any(value > 33.3 for value in values):
                    print(
                        "interactive_backdrop=FAIL "
                        f"reason={label}-stall"
                    )
                    return 3
                if any(deltas):
                    print(
                        "interactive_backdrop=FAIL "
                        f"reason={label}-geometry-lag "
                        f"max_delta={max(deltas)}px"
                    )
                    return 3

            # ENTER/EXIT messages exercise the real live move/resize loop, but
            # the Win10 material itself stays attached continuously. This gate
            # therefore measures compositor cadence without changing backdrop
            # implementation mid-interaction.
            if status.exists():
                values = _status_values(status)
                print(
                    "resting_backend="
                    f"{values.get('backdrop_backend')} "
                    f"success={values.get('backdrop_success')}"
                )
                if values.get("backdrop_backend") != "win10_effect_acrylic":
                    print(
                        "interactive_backdrop=FAIL reason=resting-backend "
                        f"actual={values.get('backdrop_backend')}"
                    )
                    return 4
            print("interactive_backdrop=PASS")
            return 0
        finally:
            _terminate_process_tree(process)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=240)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        command = [sys.executable, "clipsave.py"]
    return verify(command, count=max(1, args.count), timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
