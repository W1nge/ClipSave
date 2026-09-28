"""Opt-in, bounded geometry/event trace for Windows live-resize diagnosis.

Set CLIPSAVE_RESIZE_TRACE=1 before launching ClipSave. No clipboard records,
widget text, or image data are collected. Events stay in memory until the
drag ends so tracing does not add disk I/O to the resize hot path.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer

from .windows_frame import WINDOWPOS, window_rect


_NATIVE_MESSAGES = {
    0x0003: "move",
    0x0005: "size",
    0x000F: "paint",
    0x0046: "windowposchanging",
    0x0047: "windowposchanged",
    0x0214: "sizing",
    0x0231: "entersizemove",
    0x0232: "exitsizemove",
    0x02E0: "dpichanged",
}


class ResizeTrace(QObject):
    MAX_EVENTS = 12000

    def __init__(self, window, *, output_dir: Path | None = None) -> None:
        super().__init__(window)
        self.window = window
        self.output_dir = output_dir or Path(tempfile.gettempdir())
        self.events: deque[dict] = deque(maxlen=self.MAX_EVENTS)
        self.active = False
        self.ending = False
        self.session = 0
        self.started_ns = 0
        self.dropped = 0
        self._watched: dict[QObject, str] = {}

    def watch(self, widget: QObject, name: str) -> None:
        self._watched[widget] = name
        widget.installEventFilter(self)

    def begin(self) -> None:
        if self.active:
            if not self.ending:
                return
            self.flush(self.session)
        self.session += 1
        self.events.clear()
        self.dropped = 0
        self.started_ns = time.perf_counter_ns()
        self.active = True
        self.ending = False
        self.record("drag.begin")

    def end(self) -> None:
        if not self.active or self.ending:
            return
        self.record("drag.end")
        self.ending = True
        session = self.session
        QTimer.singleShot(250, lambda: self.flush(session))

    def before_native(self, event_type, message) -> int | None:
        if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return None
        try:
            msg = wintypes.MSG.from_address(int(message))
            number = int(msg.message)
        except (TypeError, ValueError):
            return None
        if number == 0x0231:
            self.begin()
        name = _NATIVE_MESSAGES.get(number)
        if not self.active or name is None:
            return number
        details = {}
        if number in (0x0046, 0x0047) and msg.lParam:
            pos = WINDOWPOS.from_address(int(msg.lParam))
            details["proposed"] = [pos.x, pos.y, pos.cx, pos.cy, pos.flags]
        elif number == 0x0214 and msg.lParam:
            rect = wintypes.RECT.from_address(int(msg.lParam))
            details["proposed"] = [rect.left, rect.top, rect.right, rect.bottom]
        self.record(f"win.{name}.before", **details)
        return number

    def after_native(self, number: int | None) -> None:
        name = _NATIVE_MESSAGES.get(number)
        if self.active and name is not None:
            self.record(f"win.{name}.after")
        if number == 0x0232:
            self.end()

    @staticmethod
    def _rect(rect) -> list[int]:
        return [rect.x(), rect.y(), rect.width(), rect.height()]

    def _widget_rect(self, widget) -> list[int] | None:
        if widget is None:
            return None
        origin = widget.mapTo(self.window, QPoint(0, 0))
        return [origin.x(), origin.y(), widget.width(), widget.height()]

    def record(self, name: str, *, capture_geometry: bool = True, **details) -> None:
        if not self.active:
            return
        if len(self.events) == self.MAX_EVENTS:
            self.dropped += 1
        item = {
            "ms": round((time.perf_counter_ns() - self.started_ns) / 1_000_000, 3),
            "event": name,
            **details,
        }
        if capture_geometry:
            window = self.window
            effects = window.window_effects_controller
            item.update({
                "host_physical": window_rect(int(window.winId())),
                "helper_physical": (
                    window_rect(effects.backdrop_window_hwnd)
                    if effects.backdrop_window_hwnd else None
                ),
                "qt_window": [window.width(), window.height()],
                "root": self._widget_rect(window.centralWidget()),
                "title": self._widget_rect(getattr(window, "window_title_bar", None)),
                "sidebar": self._widget_rect(getattr(window, "sidebar", None)),
                "topbar": self._widget_rect(getattr(window, "top_bar", None)),
                "library": self._widget_rect(getattr(window, "library_surface", None)),
                "grid": self._widget_rect(getattr(window, "grid", None)),
                "dpr": round(window.devicePixelRatioF(), 3),
            })
        self.events.append(item)

    def eventFilter(self, watched, event):
        if self.active:
            name = self._watched.get(watched)
            if name is not None and event.type() in (
                QEvent.Type.Paint, QEvent.Type.Resize,
            ):
                details = {}
                if event.type() == QEvent.Type.Paint:
                    details["dirty"] = self._rect(event.rect())
                self.record(
                    f"{name}.{event.type().name.lower()}",
                    capture_geometry=False,
                    widget=self._widget_rect(watched),
                    **details,
                )
        return False

    def flush(self, session: int) -> Path | None:
        if session != self.session or not self.ending or not self.events:
            return None
        self.active = False
        self.ending = False
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.output_dir / f"clipsave-resize-{stamp}-{os.getpid()}-{session}.jsonl"
        header = {
            "event": "trace.header",
            "version": 1,
            "dropped": self.dropped,
            "note": "Geometry and event timing only; no clipboard content",
        }
        try:
            with path.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(header, ensure_ascii=False) + "\n")
                for item in self.events:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"ClipSave resize trace could not be saved: {exc}", flush=True)
            return None
        self.events.clear()
        print(f"ClipSave resize trace saved: {path}", flush=True)
        return path
