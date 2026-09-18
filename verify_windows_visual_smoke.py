from __future__ import annotations

import argparse
import ctypes
import subprocess
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageGrab, ImageStat


@dataclass(frozen=True)
class VisualMetrics:
    unique_sampled_colors: int
    sampled_pixels: int
    edge_pixels: int
    edge_mean: float
    rgb_stddev: tuple[float, float, float]

    @property
    def edge_ratio(self) -> float:
        return self.edge_pixels / max(1, self.sampled_pixels)

    @property
    def unique_color_ratio(self) -> float:
        return self.unique_sampled_colors / max(1, self.sampled_pixels)


def analyze_image(image: Image.Image) -> VisualMetrics:
    rgb = image.convert("RGB")
    gray = rgb.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    sampled = rgb.resize((max(1, rgb.width // 8), max(1, rgb.height // 8)))
    sampled_edges = edges.resize(sampled.size)
    sampled_colors = list(sampled.get_flattened_data())
    edge_values = list(sampled_edges.get_flattened_data())
    rgb_stat = ImageStat.Stat(rgb)
    edge_stat = ImageStat.Stat(edges)
    return VisualMetrics(
        unique_sampled_colors=len(set(sampled_colors)),
        sampled_pixels=len(sampled_colors),
        edge_pixels=sum(1 for value in edge_values if value >= 24),
        edge_mean=float(edge_stat.mean[0]),
        rgb_stddev=tuple(float(value) for value in rgb_stat.stddev),
    )


def looks_like_rendered_ui(metrics: VisualMetrics) -> bool:
    """Reject the flat composed frame produced when Acrylic covers all Qt UI.

    The thresholds are deliberately loose.  On the Win10 19044 validation host,
    the broken ContentIsland build measured edge_ratio ~= 0.002 and the fixed Qt
    composition build measured ~= 0.043.  The test only requires enough crisp
    structure and tonal variation to prove that the final desktop-composited
    window is not one smooth backdrop surface.
    """

    return (
        metrics.edge_ratio >= 0.008
        and metrics.unique_color_ratio >= 0.008
        and max(metrics.rgb_stddev, default=0.0) >= 7.0
    )


def _status_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _find_visible_window(pid: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    found: list[int] = []

    @callback_type
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) == pid:
                found.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.left, rect.top, rect.right, rect.bottom


def verify(
    exe: Path,
    *,
    expected_backend: str | None,
    output: Path,
    timeout_seconds: float,
) -> int:
    if not exe.is_file():
        raise FileNotFoundError(exe)

    with tempfile.TemporaryDirectory(prefix="clipsave-visual-smoke-") as temp_dir:
        temp = Path(temp_dir)
        profile = temp / "profile"
        ready = temp / "ready"
        status = temp / "ready.status"
        process = subprocess.Popen(
            [
                str(exe),
                "--smoke-profile",
                str(profile),
                "--smoke-ready-file",
                str(ready),
                "--smoke-hold-ms",
                "10000",
            ]
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline and not ready.exists():
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            if not ready.exists():
                print(f"visual_smoke=FAIL reason=ready-timeout exit={process.poll()}")
                return 2

            hwnd = 0
            window_deadline = min(deadline, time.monotonic() + 10.0)
            while time.monotonic() < window_deadline and not hwnd:
                hwnd = _find_visible_window(process.pid)
                if not hwnd:
                    time.sleep(0.1)
            if not hwnd:
                print("visual_smoke=FAIL reason=no-visible-window")
                return 3

            time.sleep(0.4)
            image = ImageGrab.grab(bbox=_window_rect(hwnd), all_screens=True).convert("RGB")
            output.parent.mkdir(parents=True, exist_ok=True)
            image.save(output)
            metrics = analyze_image(image)
            print(
                "visual_metrics="
                f"unique:{metrics.unique_sampled_colors}/{metrics.sampled_pixels} "
                f"edge:{metrics.edge_pixels}/{metrics.sampled_pixels} "
                f"edge_mean:{metrics.edge_mean:.3f} "
                "rgb_stddev:"
                + ",".join(f"{value:.3f}" for value in metrics.rgb_stddev)
            )

            if not looks_like_rendered_ui(metrics):
                print(f"visual_smoke=FAIL reason=flat-composed-frame screenshot={output}")
                return 4

            process.wait(timeout=15)
            if process.returncode != 0:
                print(f"visual_smoke=FAIL reason=app-exit exit={process.returncode}")
                return 5
            if not status.is_file():
                print("visual_smoke=FAIL reason=status-missing")
                return 6
            values = _status_values(status)
            if values.get("backdrop_success") != "True":
                print(f"visual_smoke=FAIL reason=backdrop status={values}")
                return 7
            if expected_backend and values.get("backdrop_backend") != expected_backend:
                print(
                    "visual_smoke=FAIL reason=backend "
                    f"expected={expected_backend} actual={values.get('backdrop_backend')}"
                )
                return 8
            print(
                "visual_smoke=PASS "
                f"backend={values.get('backdrop_backend')} screenshot={output}"
            )
            return 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--exe",
        type=Path,
        default=Path("build/release/ClipSave/ClipSave.exe"),
    )
    parser.add_argument("--expected-backend")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/windows-visual-smoke.png"),
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    return verify(
        args.exe.resolve(),
        expected_backend=args.expected_backend,
        output=args.output.resolve(),
        timeout_seconds=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
