import json
import os
import tempfile
import unittest
import ctypes
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from clipsave_app.resize_diagnostics import ResizeTrace
from clipsave_app.windows_frame import WINDOWPOS


class _TraceWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.window_effects_controller = SimpleNamespace(backdrop_window_hwnd=None)
        self._root = QWidget(self)

    def centralWidget(self):
        return self._root


class ResizeTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_trace_is_opt_in_memory_buffered_and_contains_only_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            window = _TraceWindow()
            trace = ResizeTrace(window, output_dir=Path(directory))
            trace.watch(window._root, "root")
            window.show()
            self.app.processEvents()
            trace.begin()
            trace.record("test.geometry")
            window._root.resize(80, 60)
            self.app.processEvents()
            self.assertFalse(list(Path(directory).iterdir()))
            trace.end()
            path = trace.flush(trace.session)
            self.assertIsNotNone(path)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["event"], "trace.header")
            self.assertEqual(rows[2]["event"], "test.geometry")
            self.assertIn("qt_window", rows[2])
            self.assertNotIn("content", rows[2])
            self.assertTrue(any(row["event"] == "root.resize" for row in rows))
            window.close()

    def test_native_windowpos_records_proposed_and_actual_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            window = _TraceWindow()
            trace = ResizeTrace(window, output_dir=Path(directory))
            message = wintypes.MSG()
            message.message = 0x0231  # WM_ENTERSIZEMOVE
            number = trace.before_native(b"windows_generic_MSG", ctypes.addressof(message))
            trace.after_native(number)

            proposed = WINDOWPOS()
            proposed.x, proposed.y, proposed.cx, proposed.cy = 10, 20, 900, 600
            message.message = 0x0047  # WM_WINDOWPOSCHANGED
            message.lParam = ctypes.addressof(proposed)
            number = trace.before_native(b"windows_generic_MSG", ctypes.addressof(message))
            trace.after_native(number)

            message.message = 0x0232  # WM_EXITSIZEMOVE
            number = trace.before_native(b"windows_generic_MSG", ctypes.addressof(message))
            trace.after_native(number)
            path = trace.flush(trace.session)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            before = next(row for row in rows if row["event"] == "win.windowposchanged.before")
            self.assertEqual(before["proposed"][:4], [10, 20, 900, 600])
            self.assertIn("helper_physical", before)
            window.close()
