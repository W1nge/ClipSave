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


def _find_process_window(root_pid: int) -> int:
    return _find_visible_window(root_pid)


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
            for label, resize in (("move", False), ("resize", True)):
                _send(hwnd, WM_ENTERSIZEMOVE)
                time.sleep(0.1)
                values = _geometry_samples(hwnd, resize=resize, count=count)
                _send(hwnd, WM_EXITSIZEMOVE)
                time.sleep(0.15)
                _summarize(label, values)
                if any(value > 33.3 for value in values):
                    print(
                        "interactive_backdrop=FAIL "
                        f"reason={label}-stall"
                    )
                    return 3

            # The normal smoke status reports the resting material. The
            # transient fast composition path exists only between ENTER/EXIT
            # messages; unit tests cover its state-0 -> composition -> state-4
            # control flow while this gate measures real compositor cadence.
            if status.exists():
                values = _status_values(status)
                print(
                    "resting_backend="
                    f"{values.get('backdrop_backend')} "
                    f"success={values.get('backdrop_success')}"
                )
                if values.get("backdrop_backend") != "win10_native_acrylic":
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
