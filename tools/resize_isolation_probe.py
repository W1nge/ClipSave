"""Isolated live-resize probe for ClipSave's window and grid.

This launcher creates a temporary library with synthetic text-only records and
never enables clipboard monitoring. It samples the QWidget backing store after
UpdateRequest handling and at a throttled pre-Qt WM_SIZE point; it does not call
QWidget.grab(), render(), or repaint().
The backing-store samples are deliberately distinct from desktop/compositor
pixels, which this probe does not capture.

Run one mode per process and compare the resulting JSON files:

    python tools/resize_isolation_probe.py --mode baseline
    python tools/resize_isolation_probe.py --mode plain-grid
    python tools/resize_isolation_probe.py --mode no-helper

Use --auto-smoke --offscreen only to check probe wiring. Its QWidget.resize()
sequence is not a reproduction of a native Windows edge drag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODES = ("baseline", "plain-grid", "no-helper")
MAX_RECORDS = 1800
AUTO_SMOKE_SIZES = ((1180, 740), (1420, 860), (920, 600), (1280, 760))
NATIVE_MESSAGES = {
    0x0005: "WM_SIZE",
    0x0046: "WM_WINDOWPOSCHANGING",
    0x0047: "WM_WINDOWPOSCHANGED",
    0x0214: "WM_SIZING",
    0x0231: "WM_ENTERSIZEMOVE",
    0x0232: "WM_EXITSIZEMOVE",
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="baseline")
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=30.0,
        help="Seconds to keep the probe open; 0 waits until its window is closed.",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=50,
        help="Minimum time between backing-store samples (50 ms minimum).",
    )
    parser.add_argument("--rows", type=int, default=100, help="Synthetic fixture rows (1-1000).")
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON report path (defaults to a unique file in the system temp folder).",
    )
    parser.add_argument(
        "--auto-smoke",
        action="store_true",
        help="Run a short QWidget.resize() sequence and exit; not a native drag test.",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Use Qt's offscreen platform; intended for wiring checks only.",
    )
    args = parser.parse_args(argv)
    if not 0.0 <= args.duration_seconds <= 1800.0:
        parser.error("--duration-seconds must be between 0 and 1800")
    if not 50 <= args.interval_ms <= 5000:
        parser.error("--interval-ms must be between 50 and 5000")
    if not 1 <= args.rows <= 1000:
        parser.error("--rows must be between 1 and 1000")
    if args.auto_smoke and not args.offscreen:
        parser.error("--auto-smoke requires --offscreen")
    return args


def _default_report_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.gettempdir()) / (
        f"clipsave-resize-probe-{stamp}-{os.getpid()}.json"
    )


def _rect_in_window(widget: Any, window: Any, QPoint: Any) -> list[int] | None:
    if widget is None:
        return None
    origin = widget.mapTo(window, QPoint(0, 0))
    return [origin.x(), origin.y(), widget.width(), widget.height()]


def _sample_report(args: argparse.Namespace, output_path: Path) -> int:
    # Avoid inheriting the production trace's high-volume event logging.
    os.environ.pop("CLIPSAVE_RESIZE_TRACE", None)
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    # Add the repository root because direct execution puts only tools/ on
    # sys.path. Imports happen after --offscreen is applied.
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
    from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen
    from PySide6.QtWidgets import QApplication, QWidget

    from clipsave_app.app_paths import AppPaths
    from clipsave_app.database import LibraryDatabase
    from clipsave_app.main_window import MainWindow
    from clipsave_app.settings import Settings
    from clipsave_app.windows_frame import is_windows_qt_platform, window_rect

    class ProbeMainWindow(MainWindow):
        """Observe native resize messages and Qt update requests without changing behavior."""

        def __init__(self, *args, **kwargs):
            self.native_event_counts = {name: 0 for name in NATIVE_MESSAGES.values()}
            self._resize_probe = None
            super().__init__(*args, **kwargs)

        def event(self, event):  # noqa: N802 - Qt virtual name
            update_request = event.type() == QEvent.Type.UpdateRequest
            if update_request and self._resize_probe is not None:
                self._resize_probe.note_update_request()
            handled = super().event(event)
            if update_request and self._resize_probe is not None:
                handle = self.windowHandle()
                if self.isVisible() and handle is not None and handle.isExposed():
                    self._resize_probe.note_visible_update_request()
                self._resize_probe.capture("after-UpdateRequest-handler", stream="qt")
            return handled

        def closeEvent(self, event):  # noqa: N802 - Qt virtual name
            if self._resize_probe is not None:
                self._resize_probe.disable_capture()
            super().closeEvent(event)
            if event.isAccepted():
                QApplication.instance().quit()

        def nativeEvent(self, event_type, message):  # noqa: N802 - Qt virtual name
            native_message = None
            if os.name == "nt" and event_type in (
                b"windows_generic_MSG",
                b"windows_dispatcher_MSG",
            ):
                from ctypes import wintypes

                native_message = int(wintypes.MSG.from_address(int(message)).message)
                name = NATIVE_MESSAGES.get(native_message)
                if name is not None:
                    self.native_event_counts[name] += 1
            result = super().nativeEvent(event_type, message)
            # QWidget resize/layout events have not yet been dispatched for this
            # WM_SIZE. Keep it distinct from the later UpdateRequest sample.
            if native_message == 0x0005 and self._resize_probe is not None:
                self._resize_probe.capture(
                    "WM_SIZE-before-Qt-widget-event-dispatch", stream="native"
                )
            if native_message == 0x0232 and self._resize_probe is not None:
                QTimer.singleShot(250, self._resize_probe.write_checkpoint)
            return result

    class PlaceholderGrid(QWidget):
        """Plain block cards in the same QStackedWidget slot as the real grid."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("ResizeProbePlaceholderGrid")
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.setAutoFillBackground(False)

        def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual name
            painter = QPainter(self)
            painter.setClipRect(event.rect())
            painter.fillRect(event.rect(), QColor("#e7e5e1"))
            pen = QPen(QColor("#b8b5ae"))
            pen.setWidth(1)
            painter.setPen(pen)
            margin = 24
            gap = 18
            card_width = 208
            card_height = 150
            available_width = max(1, self.width() - 2 * margin)
            columns = max(1, (available_width + gap) // (card_width + gap))
            for index in range(24):
                row, column = divmod(index, columns)
                x = margin + column * (card_width + gap)
                y = 62 + row * (card_height + gap)
                rect = QRect(x, y, card_width, card_height)
                if rect.top() >= self.height():
                    break
                painter.fillRect(rect, QColor("#faf9f6"))
                painter.drawRect(rect.adjusted(0, 0, -1, -1))
                painter.fillRect(
                    QRect(x + 14, y + 14, max(8, card_width - 28), 10),
                    QColor("#d3d0c9"),
                )
                painter.fillRect(
                    QRect(x + 14, y + 36, max(8, card_width - 52), 7),
                    QColor("#dedbd5"),
                )
                painter.fillRect(
                    QRect(x + 14, y + 53, max(8, card_width - 70), 7),
                    QColor("#dedbd5"),
                )

    class Probe:
        def __init__(self, window, mode: str, interval_ms: int):
            self.window = window
            self.mode = mode
            self.interval_ns = interval_ms * 1_000_000
            self.started_ns = time.perf_counter_ns()
            self.last_sample_ns_by_stream = {"qt": 0, "native": 0}
            self.records: list[dict[str, Any]] = []
            self.update_requests = 0
            self.visible_update_requests = 0
            self.capture_enabled = True
            self.paint_samples_enabled = False
            self.paint_samples_ever_armed = False
            self.paint_samples_arm_reason = "waiting-for-visible-exposed-update-and-sized-backing-store"
            self.dropped = 0
            self.placeholder = None
            self.helper_disabled = False
            self.helper_disable_state: dict[str, Any] | None = None

        def add_placeholder(self) -> None:
            placeholder = PlaceholderGrid(self.window.view_stack)
            self.window.view_stack.addWidget(placeholder)
            self.window.view_stack.setCurrentWidget(placeholder)
            self.placeholder = placeholder

        def note_visible_update_request(self) -> None:
            self.visible_update_requests += 1

        def arm_paint_samples(self) -> bool:
            if self.paint_samples_enabled:
                return True
            window = self.window
            handle = window.windowHandle()
            if not window.isVisible() or handle is None or not handle.isExposed():
                self.paint_samples_arm_reason = "window-not-visible-and-exposed"
                return False
            if self.visible_update_requests <= 0:
                self.paint_samples_arm_reason = "no-completed-visible-UpdateRequest"
                return False
            store = window.backingStore()
            if store is None:
                self.paint_samples_arm_reason = "no-backing-store"
                return False
            size = store.size()
            if size.width() <= 0 or size.height() <= 0:
                self.paint_samples_arm_reason = "backing-store-size-empty"
                return False
            self.paint_samples_enabled = True
            self.paint_samples_ever_armed = True
            self.paint_samples_arm_reason = "armed-after-visible-UpdateRequest-and-sized-backing-store"
            return True

        def disable_capture(self) -> None:
            self.capture_enabled = False
            self.paint_samples_enabled = False

        @staticmethod
        def _backdrop_state(window) -> dict[str, Any]:
            controller = window.window_effects_controller
            result = controller.native_backdrop_result
            return {
                "backend": getattr(getattr(result, "backend", None), "value", None),
                "success": getattr(result, "success", None),
                "native_error": getattr(result, "native_error", None),
                "helper_hwnd": controller.backdrop_window_hwnd,
                "style_sha256": hashlib.sha256(
                    window.styleSheet().encode("utf-8")
                ).hexdigest(),
            }

        def disable_helper(self) -> None:
            if self.helper_disabled:
                return
            controller = self.window.window_effects_controller
            before = self._backdrop_state(self.window)
            # Keep the applied backdrop result and stylesheet intact. Stop only
            # helper geometry/visibility sync, then hide the existing helper.
            controller.sync_window = lambda *args, **kwargs: None
            controller.sync_window_rect = lambda *args, **kwargs: None
            controller.sync_geometry_now = lambda: None
            self.window._sync_windows_backdrop_geometry_now = lambda *args, **kwargs: None
            self.window._sync_windows_backdrop_window = lambda *args, **kwargs: None
            controller.hide_backdrop_window()
            after = self._backdrop_state(self.window)
            self.helper_disabled = True
            self.helper_disable_state = {
                "before": before,
                "after": after,
                "material_result_unchanged": (
                    before["backend"], before["success"], before["native_error"]
                ) == (
                    after["backend"], after["success"], after["native_error"]
                ),
                "stylesheet_unchanged": (
                    before["style_sha256"] == after["style_sha256"]
                ),
                "helper_hidden_by_probe": True,
            }

        def _regions(self) -> dict[str, Any]:
            window = self.window
            grid_surface = self.placeholder if self.placeholder is not None else window.grid
            return {
                "title": window.window_title_bar,
                "sidebar": window.sidebar,
                "topbar": window.top_bar,
                "library": window.library_surface,
                "grid_surface": grid_surface,
            }

        def _geometry(self) -> dict[str, Any]:
            window = self.window
            controller = window.window_effects_controller
            host = None
            helper = None
            if os.name == "nt":
                try:
                    host = window_rect(int(window.winId()))
                    if controller.backdrop_window_hwnd:
                        helper = window_rect(controller.backdrop_window_hwnd)
                except (OSError, TypeError, ValueError):
                    pass
            regions = self._regions()
            geometry = {
                name: _rect_in_window(widget, window, QPoint)
                for name, widget in regions.items()
            }
            geometry["root"] = _rect_in_window(window.centralWidget(), window, QPoint)
            geometry["view_stack"] = _rect_in_window(window.view_stack, window, QPoint)
            geometry["grid_widget_hidden"] = not window.grid.isVisible()
            if self.placeholder is not None:
                placeholder_rect = geometry["grid_surface"]
                actual_grid_rect = _rect_in_window(window.grid, window, QPoint)
                geometry["actual_grid"] = actual_grid_rect
                geometry["placeholder_matches_stack_slot"] = (
                    placeholder_rect == geometry["view_stack"]
                )
            return {
                "host_physical_rect": host,
                "helper_physical_rect": helper,
                "qt_window_size": [window.width(), window.height()],
                "device_pixel_ratio": round(window.devicePixelRatioF(), 4),
                "regions": geometry,
            }

        @staticmethod
        def _pixel_metrics(image, rect: list[int], dpr: float, stride: int = 3) -> dict[str, Any]:
            x, y, width, height = rect
            dpr = dpr if dpr > 0 else 1.0
            totals = [0, 0, 0, 0]
            sampled = inside = outside = nonzero = opaque = white = brand_blue = 0
            for logical_y in range(0, max(0, height), stride):
                py = math.floor((y + logical_y) * dpr)
                for logical_x in range(0, max(0, width), stride):
                    px = math.floor((x + logical_x) * dpr)
                    sampled += 1
                    if not (0 <= px < image.width() and 0 <= py < image.height()):
                        outside += 1
                        continue
                    inside += 1
                    color = image.pixelColor(px, py)
                    red, green, blue, alpha = color.red(), color.green(), color.blue(), color.alpha()
                    totals[0] += red
                    totals[1] += green
                    totals[2] += blue
                    totals[3] += alpha
                    nonzero += alpha > 0
                    opaque += alpha >= 240
                    white += alpha >= 128 and red >= 175 and green >= 175 and blue >= 175
                    brand_blue += (
                        alpha >= 128 and red <= 110 and green >= 90
                        and blue >= 150 and blue >= green
                    )
            return {
                "logical_rect": rect,
                "sampled_pixels": sampled,
                "in_bounds_samples": inside,
                "out_of_bounds_samples": outside,
                "alpha_nonzero_samples": nonzero,
                "opaque_samples": opaque,
                "bright_white_samples": white,
                "brand_blue_samples": brand_blue,
                "mean_rgba": [round(value / inside, 1) for value in totals] if inside else None,
            }

        def _feature_regions(self) -> dict[str, list[int]]:
            window = self.window
            brand = _rect_in_window(window.brand_label, window, QPoint)
            collapse = _rect_in_window(window.sidebar.collapse_button, window, QPoint)
            result: dict[str, list[int]] = {}
            if brand is not None:
                result["brand_label_glyphs"] = [
                    brand[0] + 10, brand[1] + 14,
                    max(1, brand[2] - 20), max(1, brand[3] - 28),
                ]
            if collapse is not None:
                result["sidebar_collapse_icon"] = [
                    collapse[0] + 14,
                    collapse[1] + max(0, (collapse[3] - 20) // 2),
                    20, 20,
                ]
            return result

        def capture(self, trigger: str, *, stream: str = "qt", force: bool = False, step=None) -> None:
            if not self.capture_enabled:
                return
            now = time.perf_counter_ns()
            if not force and now - self.last_sample_ns_by_stream[stream] < self.interval_ns:
                return
            if len(self.records) >= MAX_RECORDS:
                self.dropped += 1
                return
            self.last_sample_ns_by_stream[stream] = now
            window = self.window
            # QBackingStore.paintDevice() may be an unallocated platform image
            # during initial show. Never touch it until the visible-store gate.
            store = window.backingStore() if self.paint_samples_enabled else None
            device = store.paintDevice() if store is not None else None
            image = device if isinstance(device, QImage) else None
            record: dict[str, Any] = {
                "ms": round((now - self.started_ns) / 1_000_000, 3),
                "trigger": trigger,
                "stream": stream,
                "update_requests_seen": self.update_requests,
                "native_event_counts": dict(window.native_event_counts),
                "qimage_backing_store_available": image is not None,
                "paint_samples_enabled": self.paint_samples_enabled,
                "paint_samples_arm_reason": self.paint_samples_arm_reason,
                "backing_store_image_usable": image is not None and not image.isNull(),
                "pixel_metrics_mapping_valid": bool(
                    image is not None
                    and not image.isNull()
                    and abs(image.devicePixelRatioF() - window.devicePixelRatioF()) < 0.001
                ),
                "backing_store_type": type(store).__name__ if store is not None else None,
                "paint_device_type": type(device).__name__ if device is not None else None,
                "qt_window_size": [window.width(), window.height()],
                "device_pixel_ratio": round(window.devicePixelRatioF(), 4),
                "auto_resize_step": step,
                "geometry": self._geometry(),
            }
            if image is not None:
                record["backing_store_image"] = {
                    "size": [image.width(), image.height()],
                    "format": image.format().name,
                    "image_device_pixel_ratio": round(image.devicePixelRatioF(), 4),
                    "pixel_mapping_dpr": round(window.devicePixelRatioF(), 4),
                    "pixel_mapping_dpr_matches_image": (
                        abs(image.devicePixelRatioF() - window.devicePixelRatioF()) < 0.001
                    ),
                    "expected_device_size_from_qt_dpr": [
                        round(window.width() * window.devicePixelRatioF()),
                        round(window.height() * window.devicePixelRatioF()),
                    ],
                    "size_matches_qt_dpr": (
                        image.width() == round(window.width() * window.devicePixelRatioF())
                        and image.height() == round(window.height() * window.devicePixelRatioF())
                    ),
                }
                pixel_regions = {}
                dpr = window.devicePixelRatioF()
                for name, rect in self._feature_regions().items():
                    pixel_regions[name] = self._pixel_metrics(
                        image, rect, dpr, stride=1 if name == "sidebar_collapse_icon" else 3
                    )
                pixel_regions["right_edge"] = self._pixel_metrics(
                    image, [max(0, window.width() - 3), 0, 3, window.height()], dpr, stride=4
                )
                pixel_regions["bottom_edge"] = self._pixel_metrics(
                    image, [0, max(0, window.height() - 3), window.width(), 3], dpr, stride=4
                )
                record["backing_store_pixel_metrics"] = pixel_regions
            else:
                record["backing_store_image"] = None
                record["backing_store_pixel_metrics"] = None
            self.records.append(record)

        def note_update_request(self) -> None:
            self.update_requests += 1

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("ClipSave Resize Probe")
    app.setQuitOnLastWindowClosed(False)

    with tempfile.TemporaryDirectory(prefix="clipsave-resize-probe-profile-") as temp_name:
        temp_root = Path(temp_name)
        paths = AppPaths.build(base_dir=repo_root, local_root=temp_root)
        database = LibraryDatabase(paths=paths)
        settings = Settings(paths.settings_path)
        settings.set("monitoring", False)
        settings.set("close_to_tray", False)
        settings.set("follow_system_theme", False)
        settings.set("theme_mode", "dark")
        settings.set("sidebar_collapsed", False)
        for index in range(args.rows):
            database.add_text(
                f"Resize probe fixture {index + 1:02d}\n"
                "Synthetic local-only text used to populate the grid.\n"
                "No clipboard or user library data is read."
            )

        window = ProbeMainWindow(
            database,
            settings,
            QIcon(),
            scan_on_start=False,
            reconcile_on_start=False,
            paths=paths,
        )
        window.setWindowTitle(f"ClipSave Resize Probe - {args.mode}")
        window.resize(1280, 780)
        probe = Probe(window, args.mode, args.interval_ms)
        window._resize_probe = probe
        if args.mode == "plain-grid":
            probe.add_placeholder()

        metadata = {
            "report_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": args.mode,
            "window_title": window.windowTitle(),
            "qt_platform": QApplication.platformName(),
            "os_name": os.name,
            "pyside_version": pyside_version,
            "clipboard_monitoring_enabled": settings.get("monitoring", False),
            "clipboard_timer_active": window.clipboard_service.timer.isActive(),
            "synthetic_fixture_rows": args.rows,
            "fixture_database_is_temporary": True,
            "resize_driver": (
                "QWidget.resize sequence; wiring smoke only, not native drag"
                if args.auto_smoke
                else "manual user edge drag; native WM loop is observed indirectly"
            ),
            "visual_source": (
                "Attempts direct QImage QBackingStore.paintDevice numeric feature/edge "
                "counts after WM_SIZE and UpdateRequest; metrics are unavailable for "
                "non-QImage devices; no desktop/compositor capture"
            ),
            "max_records": MAX_RECORDS,
            "sample_interval_ms": args.interval_ms,
        }
        if args.mode == "plain-grid":
            metadata["plain_grid"] = {
                "real_grid_hidden_in_view_stack": True,
                "placeholder_occupies_same_stack_slot": True,
                "placeholder_description": "plain painted cards without record text or thumbnails",
            }

        report: dict[str, Any] = {
            "metadata": metadata,
            "initial_backdrop": Probe._backdrop_state(window),
            "samples": probe.records,
        }
        if args.mode == "no-helper":
            report["helper_disabled"] = None

        completed = False
        started_wall = time.monotonic()

        def write_checkpoint() -> None:
            if completed or not probe.capture_enabled:
                return
            probe.capture("after-native-drag", force=True)
            report["checkpoint"] = {
                "native_event_counts": dict(window.native_event_counts),
                "update_requests_seen": probe.update_requests,
                "current_backdrop": Probe._backdrop_state(window),
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"Resize probe checkpoint: {output_path}", flush=True)

        probe.write_checkpoint = write_checkpoint

        def start_capture() -> None:
            if args.mode == "no-helper":
                result = window.window_effects_controller.native_backdrop_result
                attempts = getattr(start_capture, "attempts", 0)
                if is_windows_qt_platform() and result is None and attempts < 20:
                    start_capture.attempts = attempts + 1
                    QTimer.singleShot(100, start_capture)
                    return
                probe.disable_helper()
                report["helper_disabled"] = probe.helper_disable_state
            if not probe.arm_paint_samples():
                attempts = getattr(start_capture, "paint_attempts", 0)
                if attempts < 40:
                    start_capture.paint_attempts = attempts + 1
                    QTimer.singleShot(100, start_capture)
                    return
            probe.capture("startup-settled", force=True)
            if args.auto_smoke:
                print("Resize probe offscreen auto-smoke running; this is not a native drag test.")
            else:
                print(
                    "Resize probe ready: drag an edge of the named window; "
                    + (f"it closes in {args.duration_seconds:g}s."
                       if args.duration_seconds else "close its window when finished.")
                )
            if args.auto_smoke:
                run_auto_step(0)
                return
            if args.duration_seconds:
                QTimer.singleShot(round(args.duration_seconds * 1000), finish)

        def run_auto_step(index: int) -> None:
            if index >= len(AUTO_SMOKE_SIZES):
                finish()
                return
            size = AUTO_SMOKE_SIZES[index]
            window.resize(*size)
            QTimer.singleShot(
                max(args.interval_ms, 80),
                lambda: capture_auto_step(index, size),
            )

        def capture_auto_step(index: int, size: tuple[int, int]) -> None:
            probe.capture(
                "after-qwidget-resize-step",
                force=True,
                step={"index": index, "size": list(size)},
            )
            run_auto_step(index + 1)

        def finish() -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            probe.capture("before-close", force=True)
            probe.disable_capture()
            window.force_quit = True
            window.close()
            app.quit()

        try:
            window.show()
            QTimer.singleShot(250, start_capture)
            app.exec()
        finally:
            if not completed:
                probe.disable_capture()
                window.force_quit = True
                window.close()
            report["samples"] = probe.records
            report["summary"] = {
                "sample_count": len(probe.records),
                "dropped_samples_after_cap": probe.dropped,
                "update_requests_seen": probe.update_requests,
                "visible_update_requests_seen": probe.visible_update_requests,
                "paint_samples_armed": probe.paint_samples_ever_armed,
                "paint_samples_arm_reason": probe.paint_samples_arm_reason,
                "elapsed_seconds": round(time.monotonic() - started_wall, 3),
                "clipboard_monitoring_enabled": settings.get("monitoring", False),
                "clipboard_timer_active_at_close": window.clipboard_service.timer.isActive(),
                "final_backdrop": Probe._backdrop_state(window),
                "helper_disabled": probe.helper_disable_state,
            }
            try:
                database.close()
            except (OSError, RuntimeError):
                pass
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"Resize probe report: {output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_path = args.output or _default_report_path()
    try:
        return _sample_report(args, output_path)
    except Exception as exc:
        print(f"Resize probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
