import json
import os
import ctypes
from ctypes import wintypes
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image as PILImage
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import clipsave_app.services as services_module
from clipsave_app.constants import (
    MAX_AI_RESPONSE_BYTES,
    MAX_CLIPBOARD_IMAGE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
    MAX_EMBEDDING_DIMENSIONS,
)
from clipsave_app.database import LibraryDatabase
from clipsave_app.services import (
    AIService,
    BoundedTaskExecutor,
    ClipboardService,
    OperationCancelled,
    TaskCapacityExceeded,
    WindowsClipboardNotifier,
    ai_ocr_task_executor,
    apply_windows_acrylic,
    preflight_image_file,
    shutdown_ai_ocr_task_executor,
)


class FakeMimeData:
    def __init__(self, *, has_image: bool = False, has_text: bool = False, has_urls: bool = False):
        self._has_image = has_image
        self._has_text = has_text
        self._has_urls = has_urls

    def hasImage(self):
        return self._has_image

    def hasText(self):
        return self._has_text

    def hasUrls(self):
        return self._has_urls


class FakeClipboard:
    def __init__(self, *, texts=None, image=None):
        self.texts = iter(texts or [])
        self.current_text = ""
        self._image = image

    def mimeData(self):
        return FakeMimeData(has_image=self._image is not None, has_text=self._image is None)

    def text(self):
        self.current_text = next(self.texts)
        return self.current_text

    def image(self):
        return self._image


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.read_limit = None
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit):
        self.read_limit = limit
        if callable(self.data):
            return self.data(limit)
        chunk = self.data[self.offset:self.offset + limit]
        self.offset += len(chunk)
        return chunk

    def read1(self, limit):
        return self.read(limit)


class ClipboardServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = LibraryDatabase(Path(self.temp.name) / "test.db")
        self.service = ClipboardService(self.database)

    def tearDown(self):
        self.service.shutdown()
        self.database.close()
        self.temp.cleanup()

    def test_image_key_uses_stable_pixel_content(self):
        image = QImage(32, 24, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#21a8fb"))
        self.assertEqual(self.service.image_key(image), self.service.image_key(image.copy()))

    def test_image_key_rejects_memory_limit_before_conversion(self):
        image = Mock()
        image.isNull.return_value = False
        image.width.return_value = 70_000_000
        image.height.return_value = 1
        image.sizeInBytes.return_value = 1
        image.convertToFormat.side_effect = AssertionError("conversion must not run")

        with self.assertRaisesRegex(ValueError, "内存"):
            self.service.image_key(image)
        image.convertToFormat.assert_not_called()

    def test_unchanged_clipboard_sequence_skips_polling(self):
        self.service.last_clipboard_sequence = 42
        self.service.clipboard_sequence = lambda: 42
        self.service.save_text = lambda _text: self.fail("unchanged clipboard was processed")
        self.service.save_image = lambda _image: self.fail("unchanged clipboard was processed")
        self.service.poll()
        self.assertTrue(self.service.wait_for_idle())

    def test_clipboard_busy_retries_without_reporting_user_error(self):
        failures = []
        self.service.failed.connect(failures.append)
        with patch.object(
            self.service,
            "_read_stable_snapshot",
            side_effect=services_module._ClipboardBusy("busy"),
        ):
            self.service.poll()

        self.assertEqual(failures, [])
        self.assertTrue(self.service._clipboard_retry_timer.isActive())

    def test_stop_cancels_clipboard_retry_and_ignores_late_callback(self):
        self.service._monitoring_enabled = True
        with patch.object(
            self.service,
            "_read_stable_snapshot",
            side_effect=services_module._ClipboardBusy("busy"),
        ):
            self.service.poll()

        self.assertTrue(self.service._clipboard_retry_timer.isActive())
        self.service.stop()
        self.assertFalse(self.service._clipboard_retry_timer.isActive())

        with patch.object(self.service, "poll") as poll:
            self.service._poll_if_monitoring()
        poll.assert_not_called()

    def test_worker_updates_image_dedupe_key_before_gui_signal_delivery(self):
        image = QImage(16, 16, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#123456"))
        saved = []
        self.service.save_image = lambda _image: saved.append(True) or True

        self.service._enqueue_task("image", image, 101)
        self.service._enqueue_task("image", image.copy(), 102)

        self.assertTrue(self.service.wait_for_idle(1))
        self.assertEqual(saved, [True])

    def test_resume_after_failed_shutdown_does_not_rebaseline_clipboard(self):
        self.service.last_text = "before shutdown"
        self.service.last_clipboard_sequence = 17
        with patch.object(self.service, "_start_monitoring") as start_monitoring, patch.object(
            self.service, "poll"
        ) as poll, patch("clipsave_app.services.QTimer.singleShot", side_effect=lambda _delay, callback: callback()):
            start_monitoring.side_effect = lambda: setattr(self.service, "_monitoring_enabled", True)
            self.service.resume_after_failed_shutdown(True)

        start_monitoring.assert_called_once_with()
        poll.assert_called_once_with()
        self.assertEqual(self.service.last_text, "before shutdown")
        self.assertEqual(self.service.last_clipboard_sequence, 17)

    def test_shutdown_fully_joins_worker_in_bounded_slices(self):
        worker = self.service._worker
        with patch.object(worker, "join", wraps=worker.join) as join:
            self.assertTrue(self.service.shutdown(timeout=0.1))

        self.assertGreaterEqual(join.call_count, 1)
        for call in join.call_args_list:
            self.assertGreater(call.args[0], 0)
            self.assertLessEqual(call.args[0], self.service.WAIT_INTERVAL_SECONDS)
        self.assertFalse(worker.is_alive())

    def test_failed_shutdown_can_restart_worker_and_accept_tasks(self):
        original_worker = self.service._worker

        self.assertFalse(self.service.shutdown(timeout=0.0))
        original_worker.join(1)
        self.assertFalse(original_worker.is_alive())

        saved = []
        self.service.save_text = lambda text: saved.append(text) or True
        self.service.resume_after_failed_shutdown(False)
        self.service._enqueue_task("text", "after timeout", 18)

        self.assertTrue(self.service.wait_for_idle(1))
        self.assertEqual(saved, ["after timeout"])
        self.assertIsNot(self.service._worker, original_worker)

    def test_wait_for_idle_caps_each_wait_to_remaining_deadline(self):
        idle = Mock()
        idle.is_set.return_value = False
        idle.wait.return_value = False
        service = Mock(
            _idle_event=idle,
            WAIT_INTERVAL_SECONDS=0.01,
            PROCESS_EVENTS_MAX_MS=2,
        )

        with patch("clipsave_app.services.QApplication.instance", return_value=None), patch(
            "clipsave_app.services.time.monotonic",
            side_effect=[100.0, 100.001, 100.001, 100.006, 100.006],
        ):
            self.assertFalse(ClipboardService.wait_for_idle(service, timeout=0.005))

        self.assertAlmostEqual(idle.wait.call_args.args[0], 0.004)

    def test_blank_text_advances_sequence_without_saving(self):
        clipboard = FakeClipboard(texts=["  \n"])
        sequences = iter([10, 10])
        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
        self.service._snapshot_clipboard_text = lambda source: source.text()
        self.service.clipboard_sequence = lambda: next(sequences)
        self.service.save_text = lambda _text: self.fail("blank text should not be saved")

        self.service.poll()
        self.assertTrue(self.service.wait_for_idle())

        self.assertEqual(self.service.last_text, "  \n")
        self.assertEqual(self.service.last_clipboard_sequence, 10)

    def test_start_uses_windows_sequence_without_hashing_current_image(self):
        clipboard = Mock()
        clipboard.text.return_value = "current"
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: 42
        self.service.image_key = Mock(side_effect=AssertionError("image should not be hashed"))

        with patch("clipsave_app.services.QApplication.clipboard", return_value=clipboard):
            self.service.start()
            self.service.stop()

        self.assertEqual(self.service.last_clipboard_sequence, 42)
        self.service.image_key.assert_not_called()
        clipboard.text.assert_not_called()
        clipboard.image.assert_not_called()

    def test_start_rejects_native_text_without_qt_reread(self):
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_text=True)
        self.service.clipboard_sequence = lambda: None
        with patch("clipsave_app.services.QApplication.clipboard", return_value=clipboard), patch.object(
            self.service,
            "_snapshot_clipboard_text",
            side_effect=ValueError("剪贴板文字过大，已拒绝读取。"),
        ):
            self.service.start()
            self.service.stop()

        clipboard.text.assert_not_called()

    def test_start_uses_event_notifications_with_slow_polling_fallback(self):
        clipboard = Mock()
        clipboard.text.return_value = "current"
        self.service.clipboard_sequence = lambda: 42
        self.service.notifier.start = Mock(return_value=True)

        with patch("clipsave_app.services.QApplication.clipboard", return_value=clipboard):
            self.service.start()

        self.service.notifier.start.assert_called_once_with(self.service.parent())
        self.assertEqual(self.service.timer.interval(), self.service.EVENT_FALLBACK_INTERVAL_MS)
        self.assertTrue(self.service.timer.isActive())

    def test_clipboard_notification_runs_stable_snapshot_path(self):
        self.service.poll = Mock()
        self.service._monitoring_enabled = True

        self.service.notifier.changed.emit()

        self.service.poll.assert_called_once_with()

    def test_sequence_change_during_read_retries_latest_snapshot(self):
        clipboard = FakeClipboard(texts=["first", "  second\n"])
        sequences = iter([10, 11, 11, 11])
        saved = []
        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
        self.service._snapshot_clipboard_text = lambda source: source.text()
        self.service.clipboard_sequence = lambda: next(sequences)
        self.service.save_text = lambda text: saved.append(text) or True

        self.service.poll()
        self.assertTrue(self.service.wait_for_idle())

        self.assertEqual(saved, ["  second\n"])
        self.assertEqual(self.service.last_text, "  second\n")
        self.assertEqual(self.service.last_clipboard_sequence, 11)

    def test_invalid_image_candidate_falls_back_to_valid_text(self):
        clipboard = FakeClipboard(texts=["fallback text"])
        clipboard.mimeData = lambda: FakeMimeData(has_image=True, has_text=True)
        sequences = iter([20, 20])
        saved = []
        self.service.last_clipboard_sequence = 19
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: next(sequences)
        self.service._snapshot_clipboard_image = Mock(
            side_effect=ValueError("invalid image")
        )
        self.service._snapshot_clipboard_text = lambda source: source.text()
        self.service.save_text = lambda text: saved.append(text) or True

        self.service.poll()
        self.assertTrue(self.service.wait_for_idle())

        self.assertEqual(saved, ["fallback text"])

    def test_clipboard_sequence_wrap_advances_after_successful_image_save(self):
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.service.last_clipboard_sequence = 0xFFFFFFFF
        self.service._clipboard = lambda: FakeClipboard(image=image)
        self.service.clipboard_sequence = lambda: 0
        self.service.save_image = Mock(return_value=True)

        with patch.object(self.service, "_snapshot_clipboard_image", return_value=image):
            self.service.poll()
            self.assertTrue(self.service.wait_for_idle())
            self.service.poll()
            self.assertTrue(self.service.wait_for_idle())

        self.service.save_image.assert_called_once()
        self.assertEqual(self.service.last_clipboard_sequence, 0)

    def test_native_clipboard_size_is_rejected_before_materializing_payload(self):
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.IsClipboardFormatAvailable.return_value = 1
        user32.GetClipboardData.return_value = 123
        kernel32.GlobalSize.return_value = MAX_CLIPBOARD_TEXT_BYTES + 2

        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService, "_windows_clipboard_apis", return_value=(user32, kernel32)
        ), patch("clipsave_app.services.ctypes.string_at") as string_at:
            with self.assertRaisesRegex(ValueError, "过大"):
                self.service._native_clipboard_text_snapshot()

        kernel32.GlobalLock.assert_not_called()
        string_at.assert_not_called()
        user32.CloseClipboard.assert_called_once_with()

    def test_native_unicode_text_is_copied_and_decoded_under_one_open(self):
        payload = "fixed text\0changed later".encode("utf-16-le")
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.IsClipboardFormatAvailable.return_value = 1
        user32.GetClipboardData.return_value = 123
        kernel32.GlobalSize.return_value = len(payload)
        kernel32.GlobalLock.return_value = 456

        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService, "_windows_clipboard_apis", return_value=(user32, kernel32)
        ), patch("clipsave_app.services.ctypes.string_at", return_value=payload):
            self.assertEqual(self.service._native_clipboard_text_snapshot(), "fixed text")

        user32.OpenClipboard.assert_called_once_with(None)
        user32.CloseClipboard.assert_called_once_with()
        kernel32.GlobalLock.assert_called_once_with(123)
        kernel32.GlobalUnlock.assert_called_once_with(123)

    def test_native_unicode_text_accepts_odd_allocation_size(self):
        payload = "hello\0".encode("utf-16-le") + b"x"
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.IsClipboardFormatAvailable.return_value = 1
        user32.GetClipboardData.return_value = 123
        kernel32.GlobalSize.return_value = len(payload)
        kernel32.GlobalLock.return_value = 456

        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService, "_windows_clipboard_apis", return_value=(user32, kernel32)
        ), patch("clipsave_app.services.ctypes.string_at", return_value=payload):
            self.assertEqual(self.service._native_clipboard_text_snapshot(), "hello")

    def test_native_file_drop_extracts_paths_without_opening_files(self):
        paths = (r"C:\Work\report.txt", r"D:\图片\reference.png")
        user32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.IsClipboardFormatAvailable.side_effect = (
            lambda format_id: format_id == ClipboardService.CF_HDROP
        )
        user32.GetClipboardData.return_value = 123
        shell32 = Mock()

        def drag_query(drop_handle, index, buffer, _length):
            self.assertEqual(drop_handle, 123)
            if index == ClipboardService.DRAG_QUERY_FILE_COUNT:
                return len(paths)
            value = paths[index]
            if buffer is None:
                return len(value)
            buffer.value = value
            return len(value)

        shell32.DragQueryFileW.side_effect = drag_query
        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService,
            "_windows_clipboard_apis",
            return_value=(user32, Mock()),
        ), patch.object(
            ClipboardService, "_windows_shell_api", return_value=shell32
        ):
            self.assertEqual(
                self.service._native_clipboard_file_paths_snapshot(), paths
            )

        user32.OpenClipboard.assert_called_once_with(None)
        user32.GetClipboardData.assert_called_once_with(ClipboardService.CF_HDROP)
        user32.CloseClipboard.assert_called_once_with()

    def test_native_file_drop_rejects_excessive_path_count_before_allocation(self):
        user32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.IsClipboardFormatAvailable.return_value = 1
        user32.GetClipboardData.return_value = 123
        shell32 = Mock()
        shell32.DragQueryFileW.return_value = ClipboardService.MAX_FILE_PATHS + 1

        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService,
            "_windows_clipboard_apis",
            return_value=(user32, Mock()),
        ), patch.object(
            ClipboardService, "_windows_shell_api", return_value=shell32
        ), patch("clipsave_app.services.ctypes.create_unicode_buffer") as create_buffer:
            with self.assertRaisesRegex(ValueError, "too many"):
                self.service._native_clipboard_file_paths_snapshot()

        create_buffer.assert_not_called()
        user32.CloseClipboard.assert_called_once_with()

    def test_file_drop_snapshot_takes_priority_over_image_and_text_formats(self):
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(
            has_image=True,
            has_text=True,
            has_urls=True,
        )
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = Mock(side_effect=[10, 10])

        with patch.object(
            self.service,
            "_snapshot_clipboard_file_paths",
            return_value="C:\\one.txt\nD:\\two.png",
        ), patch.object(
            self.service,
            "_snapshot_clipboard_image",
            side_effect=AssertionError("file copies must not be decoded as images"),
        ), patch.object(
            self.service,
            "_snapshot_clipboard_text",
            side_effect=AssertionError("file copies must not use CF_UNICODETEXT"),
        ):
            self.assertEqual(
                self.service._read_stable_snapshot(),
                ("text", "C:\\one.txt\nD:\\two.png", 10),
            )

    def test_file_drop_paths_are_saved_as_one_text_card_without_failure(self):
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_text=True, has_urls=True)
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = Mock(side_effect=[10, 10])
        failures = []
        self.service.failed.connect(failures.append)
        markdown_dir = Path(self.temp.name) / "Markdown"
        markdown_dir.mkdir()
        path_text = "C:\\Work\\report.txt\nD:\\图片\\reference.png"

        with patch.object(
            self.service, "_snapshot_clipboard_file_paths", return_value=path_text
        ), patch("clipsave_app.services.MARKDOWN_DIR", markdown_dir):
            self.service.poll()
            self.assertTrue(self.service.wait_for_idle())

        rows = self.database.query_items(kind="text")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], path_text)
        self.assertEqual(failures, [])
        clipboard.text.assert_not_called()

    def test_stable_text_snapshot_does_not_reread_qt_clipboard_text(self):
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_text=True)
        clipboard.text.side_effect = AssertionError("Qt clipboard text must not be reread")
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = Mock(side_effect=[10, 10])

        with patch.object(self.service, "_snapshot_clipboard_text", return_value="fixed text"):
            self.assertEqual(self.service._read_stable_snapshot(), ("text", "fixed text", 10))

        clipboard.text.assert_not_called()

    def test_registered_png_dimensions_are_rejected_before_qt_decode(self):
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_image=True)
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: 10
        header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (100000).to_bytes(4, "big") * 2
        with patch.object(self.service, "_native_clipboard_image_snapshot", return_value=("PNG", header)):
            with self.assertRaisesRegex(ValueError, "dimensions"):
                self.service._read_stable_snapshot()
        clipboard.image.assert_not_called()

    def test_registered_png_payload_is_rejected_before_qt_decode(self):
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_image=True)
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: 10
        header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1).to_bytes(4, "big") * 2
        with patch.object(
            self.service,
            "_native_clipboard_image_snapshot",
            return_value=("PNG", header + b"x" * (MAX_CLIPBOARD_IMAGE_BYTES + 1 - len(header))),
        ):
            with self.assertRaisesRegex(ValueError, "payload"):
                self.service._read_stable_snapshot()
        clipboard.image.assert_not_called()

    def test_registered_png_is_decoded_from_validated_native_copy(self):
        image = QImage(2, 3, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#21a8fb"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "PNG"))
        payload = bytes(buffer.data())
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_image=True)
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: 10
        with patch.object(self.service, "_native_clipboard_image_snapshot", return_value=("PNG", payload)):
            kind, decoded, sequence = self.service._read_stable_snapshot()

        self.assertEqual((kind, decoded.width(), decoded.height(), sequence), ("image", 2, 3, 10))
        clipboard.image.assert_not_called()

    def test_unknown_native_image_source_is_rejected(self):
        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            self.service, "_native_clipboard_image_snapshot", return_value=None
        ):
            with self.assertRaisesRegex(ValueError, "Unknown native"):
                self.service._reject_oversized_native_clipboard("image")

    def test_clipboard_sequence_calls_configured_win32_api(self):
        user32 = Mock()
        user32.GetClipboardSequenceNumber.return_value = 0xFFFFFFFF
        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService, "_windows_clipboard_apis", return_value=(user32, Mock())
        ):
            self.assertEqual(self.service.clipboard_sequence(), 0xFFFFFFFF)
        user32.GetClipboardSequenceNumber.assert_called_once_with()

    def test_registered_payload_cap_is_checked_before_global_lock(self):
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.EnumClipboardFormats.side_effect = [0xC001, 0]
        user32.GetClipboardData.return_value = 123
        kernel32.GlobalSize.return_value = MAX_CLIPBOARD_IMAGE_BYTES + 1
        kernel32.GetLastError.return_value = 0

        def set_png_name(_format_id, buffer, _length):
            buffer.value = "PNG"
            return 3

        user32.GetClipboardFormatNameW.side_effect = set_png_name
        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService, "_windows_clipboard_apis", return_value=(user32, kernel32)
        ), patch("clipsave_app.services.ctypes.string_at") as string_at:
            with self.assertRaisesRegex(ValueError, "payload"):
                self.service._native_registered_image_payloads()

        kernel32.GlobalLock.assert_not_called()
        string_at.assert_not_called()
        user32.CloseClipboard.assert_called_once_with()

    def test_native_dib_snapshot_uses_one_open_clipboard_window(self):
        source = QImage(2, 2, QImage.Format.Format_RGB32)
        source.fill(QColor("#21a8fb"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(source.save(buffer, "BMP"))
        dib = bytes(buffer.data())[14:]
        user32 = Mock()
        kernel32 = Mock()
        user32.OpenClipboard.return_value = 1
        user32.EnumClipboardFormats.return_value = 0
        user32.IsClipboardFormatAvailable.side_effect = lambda format_id: format_id == ClipboardService.CF_DIB
        user32.GetClipboardData.return_value = 123
        kernel32.GetLastError.return_value = 0
        kernel32.GlobalSize.return_value = len(dib)
        kernel32.GlobalLock.return_value = 456

        with patch("clipsave_app.services.os.name", "nt"), patch.object(
            ClipboardService, "_windows_clipboard_apis", return_value=(user32, kernel32)
        ), patch("clipsave_app.services.ctypes.string_at", return_value=dib):
            self.assertEqual(self.service._native_clipboard_image_snapshot(), ("DIB", dib))

        user32.OpenClipboard.assert_called_once_with(None)
        user32.CloseClipboard.assert_called_once_with()
        kernel32.GlobalLock.assert_called_once_with(123)

    def test_dib_is_decoded_from_fixed_native_snapshot_not_qt_clipboard(self):
        source = QImage(3, 2, QImage.Format.Format_RGB32)
        source.fill(QColor("#21a8fb"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(source.save(buffer, "BMP"))
        dib = bytes(buffer.data())[14:]
        clipboard = Mock()
        clipboard.mimeData.return_value = FakeMimeData(has_image=True)
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: 10

        with patch.object(self.service, "_native_clipboard_image_snapshot", return_value=("DIB", dib)):
            kind, decoded, sequence = self.service._read_stable_snapshot()

        self.assertEqual((kind, decoded.width(), decoded.height(), sequence), ("image", 3, 2, 10))
        clipboard.image.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows ctypes signatures are Windows-only")
    def test_windows_clipboard_apis_use_pointer_sized_handles(self):
        from ctypes import wintypes

        user32, kernel32 = self.service._windows_clipboard_apis()

        self.assertIs(user32.GetClipboardData.restype, wintypes.HANDLE)
        self.assertIs(user32.GetClipboardSequenceNumber.restype, wintypes.DWORD)
        self.assertIs(kernel32.GlobalLock.restype, wintypes.LPVOID)
        self.assertIs(kernel32.GlobalSize.restype, ctypes.c_size_t)
        self.assertIs(user32.EnumClipboardFormats.restype, wintypes.UINT)
        self.assertIs(kernel32.GetLastError.restype, wintypes.DWORD)
        self.assertIs(
            self.service._windows_shell_api().DragQueryFileW.restype,
            wintypes.UINT,
        )

    def test_prepare_for_shutdown_pauses_and_resume_reenables_tasks(self):
        self.service.timer.start()
        self.service.prepare_for_shutdown()

        self.assertFalse(self.service.timer.isActive())
        self.assertFalse(self.service._accepting_tasks)
        self.service.resume_after_failed_shutdown(False)
        self.assertTrue(self.service._accepting_tasks)

    def test_failed_save_does_not_advance_clipboard_state(self):
        clipboard = FakeClipboard(texts=["new text"])
        sequences = iter([10, 10])
        failures = []
        self.service.last_text = "old text"
        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
        self.service._snapshot_clipboard_text = lambda source: source.text()
        self.service.clipboard_sequence = lambda: next(sequences)
        self.service.save_text = Mock(side_effect=RuntimeError("database unavailable"))
        self.service.failed.connect(failures.append)

        self.service.poll()
        self.assertTrue(self.service.wait_for_idle())

        self.assertEqual(self.service.last_text, "old text")
        self.assertEqual(self.service.last_clipboard_sequence, 9)
        self.assertEqual(failures, ["database unavailable"])

    def test_poll_persists_on_worker_without_blocking_ui_thread(self):
        clipboard = FakeClipboard(texts=["background save"])
        sequences = iter([10, 10])
        started = threading.Event()
        release = threading.Event()

        def slow_save(_text):
            started.set()
            release.wait(2)
            return True

        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
        self.service._snapshot_clipboard_text = lambda source: source.text()
        self.service.clipboard_sequence = lambda: next(sequences)
        self.service.save_text = slow_save

        before = time.perf_counter()
        self.service.poll()
        elapsed = time.perf_counter() - before

        self.assertLess(elapsed, 0.1)
        self.assertTrue(started.wait(1))
        self.assertEqual(self.service.last_clipboard_sequence, 9)
        release.set()
        self.assertTrue(self.service.wait_for_idle())
        self.assertEqual(self.service.last_text, "background save")
        self.assertEqual(self.service.last_clipboard_sequence, 10)

    def test_idle_event_stays_clear_until_every_accepted_task_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_save(_text):
            started.set()
            release.wait(2)
            return True

        with patch.object(self.service, "save_text", side_effect=blocked_save):
            self.service._enqueue_task("text", "accepted", 91)
            self.assertTrue(started.wait(1))
            self.assertFalse(self.service._idle_event.is_set())
            release.set()
            self.assertTrue(self.service.wait_for_idle(1))

        self.assertTrue(self.service._idle_event.is_set())

    def test_persistence_queue_enforces_total_memory_budget(self):
        failures = []
        self.service.failed.connect(failures.append)
        self.service._pending_bytes = self.service.PERSISTENCE_MEMORY_BUDGET

        self.service._enqueue_task("text", "new", 10)

        self.assertEqual(self.service._tasks.qsize(), 0)
        self.assertTrue(any("内存过高" in message for message in failures))

    def test_text_preserves_whitespace_and_enforces_byte_limit(self):
        markdown_dir = Path(self.temp.name) / "Markdown"
        markdown_dir.mkdir()
        raw = "\n  保留空白  \t\n"
        with patch("clipsave_app.services.MARKDOWN_DIR", markdown_dir):
            self.assertTrue(self.service.save_text(raw))
        self.assertEqual(self.database.query_items()[0]["content"], raw)

        oversized = "界" * (MAX_CLIPBOARD_TEXT_BYTES // 3 + 1)
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            self.service.save_text(oversized)
        self.assertEqual(len(self.database.query_items()), 1)

    def test_markdown_failure_warns_but_still_emits_captured(self):
        markdown_dir = Path(self.temp.name) / "Markdown"
        markdown_dir.mkdir()
        captured = []
        failures = []
        self.service.captured.connect(captured.append)
        self.service.failed.connect(failures.append)

        with (
            patch("clipsave_app.services.MARKDOWN_DIR", markdown_dir),
            patch("clipsave_app.services.open_managed_binary", side_effect=OSError("disk full")),
        ):
            self.assertTrue(self.service.save_text("persist me"))

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(self.database.query_items()), 1)
        self.assertIn("disk full", failures[0])

    def test_daily_markdown_uses_exclusive_create_then_handle_append(self):
        markdown_dir = Path(self.temp.name) / "Markdown"
        markdown_dir.mkdir()
        with patch("clipsave_app.services.MARKDOWN_DIR", markdown_dir):
            self.assertTrue(self.service.save_text("first entry"))
            self.assertTrue(self.service.save_text("second entry"))

        daily = next(markdown_dir.glob("clipboard_*.md"))
        content = daily.read_text(encoding="utf-8")
        self.assertEqual(content.count("# ClipSave"), 1)
        self.assertIn("first entry", content)
        self.assertIn("second entry", content)

    def test_duplicate_image_and_database_failure_remove_new_png(self):
        picture_dir = Path(self.temp.name) / "Pictures"
        image_a = QImage(16, 16, QImage.Format.Format_RGBA8888)
        image_a.fill(QColor("#ff0000"))
        image_b = QImage(16, 16, QImage.Format.Format_RGBA8888)
        image_b.fill(QColor("#0000ff"))

        with patch("clipsave_app.services.PICTURE_DIR", picture_dir):
            self.assertTrue(self.service.save_image(image_a))
            self.assertTrue(self.service.save_image(image_b))
            self.assertTrue(self.service.save_image(image_a))
            self.assertEqual(len(list(picture_dir.rglob("*.png"))), 2)
            self.assertEqual(len(self.database.query_items()), 2)

            with patch.object(self.database, "add_image", side_effect=RuntimeError("db failed")):
                with self.assertRaisesRegex(RuntimeError, "db failed"):
                    self.service.save_image(image_b.copy())
            self.assertEqual(len(list(picture_dir.rglob("*.png"))), 2)

            image_c = QImage(16, 16, QImage.Format.Format_RGBA8888)
            image_c.fill(QColor("#00ff00"))
            with patch.object(self.database, "add_image", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "数据库未能保存"):
                    self.service.save_image(image_c)
            self.assertEqual(len(list(picture_dir.rglob("*.png"))), 2)

    def test_scanner_owned_capture_path_is_not_deleted_as_duplicate(self):
        picture_dir = Path(self.temp.name) / "Pictures"
        image = QImage(16, 16, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#00aa55"))
        real_add_image = self.database.add_image

        def scanner_wins(path, created_at=None):
            self.assertTrue(self.database.import_file(path, "image"))
            return real_add_image(path, created_at)

        captured = []
        self.service.captured.connect(captured.append)
        with patch("clipsave_app.services.PICTURE_DIR", picture_dir), patch.object(
            self.database, "add_image", side_effect=scanner_wins
        ):
            self.assertTrue(self.service.save_image(image))

        rows = self.database.query_items(kind="image")
        self.assertEqual(len(rows), 1)
        self.assertTrue(Path(rows[0]["path"]).exists())
        self.assertEqual(len(list(picture_dir.rglob("*.png"))), 1)
        self.assertEqual(captured, [rows[0]["id"]])

    def test_capture_identity_lock_blocks_replacement_during_scanner_race(self):
        picture_dir = Path(self.temp.name) / "Pictures"
        original = QImage(16, 16, QImage.Format.Format_RGBA8888)
        original.fill(QColor("#00aa55"))
        replacement = QImage(16, 16, QImage.Format.Format_RGBA8888)
        replacement.fill(QColor("#aa0055"))
        real_add_image = self.database.add_image
        replacement_attempts = []

        def scanner_wins(path, created_at=None):
            replacement_attempts.append(replacement.save(str(path)))
            self.assertTrue(self.database.import_file(path, "image"))
            return real_add_image(path, created_at)

        with patch("clipsave_app.services.PICTURE_DIR", picture_dir), patch.object(
            self.database, "add_image", side_effect=scanner_wins
        ):
            self.assertTrue(self.service.save_image(original))

        row = self.database.query_items(kind="image")[0]
        self.assertEqual(replacement_attempts, [False])
        self.assertTrue(Path(row["path"]).exists())
        self.assertEqual(row["content_hash"], self.database.file_hash(Path(row["path"])))

    def test_capture_identity_lock_blocks_replacement_before_database_insert(self):
        picture_dir = Path(self.temp.name) / "Pictures"
        original = QImage(16, 16, QImage.Format.Format_RGBA8888)
        original.fill(QColor("#009944"))
        replacement = QImage(16, 16, QImage.Format.Format_RGBA8888)
        replacement.fill(QColor("#990044"))
        real_add_image = self.database.add_image
        replacement_attempts = []

        def attempt_replacement(path, created_at=None):
            replacement_attempts.append(replacement.save(str(path)))
            return real_add_image(path, created_at)

        with patch("clipsave_app.services.PICTURE_DIR", picture_dir), patch.object(
            self.database, "add_image", side_effect=attempt_replacement
        ):
            self.assertTrue(self.service.save_image(original))

        row = self.database.query_items(kind="image")[0]
        self.assertEqual(replacement_attempts, [False])
        self.assertEqual(row["content_hash"], self.database.file_hash(Path(row["path"])))


class AIServiceTests(unittest.TestCase):
    def test_preflight_rejects_truncated_image_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "truncated.jpg"
            PILImage.new("RGB", (64, 64), "red").save(path, "JPEG")
            payload = path.read_bytes()
            path.write_bytes(payload[: max(100, len(payload) // 2)])

            with self.assertRaises(ValueError):
                preflight_image_file(path)

    def test_local_http_service_can_be_configured_without_api_key(self):
        service = AIService("http://127.0.0.1:11434/v1", "", "vision", "embedding")
        response = FakeResponse(json.dumps({"ok": True}).encode("utf-8"))

        with patch.object(service, "_open_request", return_value=response) as open_request:
            self.assertEqual(service._post("/test", {}), {"ok": True})

        request = open_request.call_args.args[0]
        self.assertTrue(service.configured)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertLessEqual(response.read_limit, 64 * 1024)

    def test_embedding_provider_identity_excludes_url_credentials_and_query(self):
        first = AIService(
            "https://user:secret@example.com/v1?tenant=one", "", "vision", "embedding"
        )
        second = AIService("https://example.com/v1?tenant=two", "", "vision", "embedding")

        self.assertEqual(first.embedding_provider, second.embedding_provider)
        self.assertNotIn("secret", first.embedding_provider)
        self.assertTrue(first.embedding_provider.startswith("openai-compatible:"))

    def test_ai_open_timeout_uses_remaining_overall_deadline(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        response = FakeResponse(b'{"ok": true}')
        with patch(
            "clipsave_app.services.time.monotonic",
            side_effect=[100.0, 100.25, 100.5, 100.75],
        ), patch.object(service, "_open_request", return_value=response) as open_request:
            self.assertEqual(service._post("/test", {}), {"ok": True})

        self.assertAlmostEqual(open_request.call_args.kwargs["timeout"], 89.75)

    def test_ai_response_size_and_shape_are_validated(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        response = FakeResponse(lambda limit: b"x" * limit)
        with patch.object(service, "_open_request", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "响应过大"):
                service._post("/test", {})

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "image.png"
            PILImage.new("RGB", (4, 4), "white").save(path)
            service._post = lambda _path, _payload, _cancel_event=None: {"choices": []}
            with patch("clipsave_app.services.PICTURE_DIR", Path(temp)):
                with self.assertRaisesRegex(RuntimeError, "choices"):
                    service.describe_image(path)

    def test_embedding_must_be_numeric_finite_and_bounded(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        service._post = lambda _path, _payload, _cancel_event=None: {"data": [{"embedding": [1, 2.5]}]}
        self.assertEqual(service.embed("query"), [1.0, 2.5])

        service._post = lambda _path, _payload, _cancel_event=None: {"data": [{"embedding": [1, float("nan")]}]}
        with self.assertRaisesRegex(RuntimeError, "非有限"):
            service.embed("query")

        service._post = lambda _path, _payload, _cancel_event=None: {
            "data": [{"embedding": [0] * (MAX_EMBEDDING_DIMENSIONS + 1)}]
        }
        with self.assertRaisesRegex(RuntimeError, "维度过大"):
            service.embed("query")
        self.assertEqual(service.similarity([1.0, float("inf")], [1.0, 2.0]), -1.0)

    def test_cancelled_ai_request_stops_before_network_access(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        cancel_event = threading.Event()
        cancel_event.set()
        with patch.object(service, "_open_request") as open_request:
            with self.assertRaises(OperationCancelled):
                service._post("/test", {}, cancel_event)
        open_request.assert_not_called()

    def test_ai_response_uses_interruptible_single_read_chunks(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        cancel_event = threading.Event()

        class InterruptibleResponse(FakeResponse):
            def read(self, _limit):
                raise AssertionError("buffer-filling read must not be used")

            def read1(self, limit):
                cancel_event.set()
                return b"x" * min(limit, 8)

        with patch.object(
            service, "_open_request", return_value=InterruptibleResponse(b"")
        ):
            with self.assertRaises(OperationCancelled):
                service._post("/test", {}, cancel_event)

    def test_cancelled_ai_body_read_closes_active_response(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        cancel_event = threading.Event()
        closed = threading.Event()

        class BlockingResponse(FakeResponse):
            def read1(self, _limit):
                closed.wait(1)
                raise OSError("response closed")

            def close(self):
                closed.set()

        response = BlockingResponse(b"")
        with patch.object(service, "_open_request", return_value=response):
            def cancel():
                time.sleep(0.05)
                cancel_event.set()

            threading.Thread(target=cancel, daemon=True).start()
            with self.assertRaises(OperationCancelled):
                service._post("/test", {}, cancel_event)
        self.assertTrue(closed.is_set())

    def test_cross_origin_ai_redirect_is_refused_before_forwarding_headers(self):
        service = AIService("https://api.example/v1", "secret", "vision", "")
        request = urllib.request.Request(
            "https://api.example/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
        )
        handler = service._SameOriginRedirectHandler()
        with self.assertRaisesRegex(RuntimeError, "cross-origin"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://other.example/collect",
            )

    def test_describe_image_rejects_replacement_after_snapshot(self):
        service = AIService("http://localhost/v1", "", "vision", "")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "image.png"
            PILImage.new("RGB", (8, 8), "red").save(path)
            snapshot = preflight_image_file(path)
            replacement = root / "replacement.png"
            PILImage.new("RGB", (8, 8), "blue").save(replacement)
            os.replace(replacement, path)
            with patch("clipsave_app.services.PICTURE_DIR", root), patch.object(
                service, "_post"
            ) as post:
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    service.describe_image(snapshot)
            post.assert_not_called()

    def test_describe_image_rejects_index_hash_mismatch_before_network(self):
        service = AIService("http://localhost/v1", "", "vision", "")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "image.png"
            PILImage.new("RGB", (8, 8), "red").save(path)
            snapshot = preflight_image_file(path)
            with patch("clipsave_app.services.PICTURE_DIR", root), patch.object(
                service, "_post"
            ) as post:
                with self.assertRaisesRegex(RuntimeError, "indexed item"):
                    service.describe_image(
                        snapshot,
                        expected_sha256="0" * 64,
                    )
            post.assert_not_called()

    def test_describe_image_preflights_pixels_before_posting(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        with patch("clipsave_app.services.preflight_image_file", side_effect=ValueError("图片尺寸过大")) as preflight:
            with self.assertRaisesRegex(ValueError, "尺寸过大"):
                service.describe_image(Path("image.png"))
        preflight.assert_called_once_with(Path("image.png"))


class WindowsClipboardNotifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_registers_and_unregisters_window_listener(self):
        notifier = WindowsClipboardNotifier()
        user32 = Mock()
        user32.AddClipboardFormatListener.return_value = 1
        app = Mock()
        window = Mock()
        window.winId.return_value = 123

        with (
            patch("clipsave_app.services.os.name", "nt"),
            patch.object(notifier, "_user32", return_value=user32),
            patch("clipsave_app.services.QCoreApplication.instance", return_value=app),
        ):
            self.assertTrue(notifier.start(window))
            notifier.stop()

        user32.AddClipboardFormatListener.assert_called_once_with(123)
        user32.RemoveClipboardFormatListener.assert_called_once_with(123)
        app.installNativeEventFilter.assert_called_once_with(notifier)
        app.removeNativeEventFilter.assert_called_once_with(notifier)

    def test_wm_clipboardupdate_emits_changed_for_registered_window(self):
        from ctypes import wintypes

        notifier = WindowsClipboardNotifier()
        notifier._hwnd = 123
        notifier._installed = True
        changed = []
        notifier.changed.connect(lambda: changed.append(True))
        message = wintypes.MSG()
        message.hWnd = 123
        message.message = notifier.WM_CLIPBOARDUPDATE

        handled, result = notifier.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message))

        self.assertEqual((handled, result), (False, 0))
        self.assertEqual(changed, [True])


class PreflightTests(unittest.TestCase):
    def test_image_snapshot_tracks_pixels_and_detects_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "image.png"
            PILImage.new("RGB", (12, 8), "white").save(path)
            snapshot = preflight_image_file(path)

            self.assertEqual(snapshot.pixels, 96)
            self.assertEqual(snapshot.decoded_bytes, 384)
            PILImage.new("RGB", (4, 4), "black").save(path)
            with self.assertRaisesRegex(RuntimeError, "修改"):
                snapshot.require_current()

    def test_image_preflight_rejects_pixel_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "image.png"
            PILImage.new("RGB", (11, 11), "white").save(path)
            with self.assertRaisesRegex(ValueError, "尺寸过大"):
                preflight_image_file(path, max_pixels=100)


class AcrylicTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows effects are Windows-only")
    def test_windows_effect_api_signatures_initialize(self):
        user32, dwmapi = services_module._windows_effect_apis()
        self.assertEqual(user32.SetWindowCompositionAttribute.restype, wintypes.BOOL)
        self.assertEqual(dwmapi.DwmSetWindowAttribute.restype, ctypes.c_long)

    def test_windows_10_uses_stable_blur_behind_for_both_themes(self):
        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("accent_state", ctypes.c_int),
                ("accent_flags", ctypes.c_int),
                ("gradient_color", ctypes.c_uint32),
                ("animation_id", ctypes.c_int),
            ]

        class CompositionData(ctypes.Structure):
            _fields_ = [
                ("attribute", ctypes.c_int),
                ("data", ctypes.c_void_p),
                ("size", ctypes.c_size_t),
            ]

        captured = []

        def set_composition(_hwnd, data_pointer):
            data = ctypes.cast(data_pointer, ctypes.POINTER(CompositionData)).contents
            policy = ctypes.cast(data.data, ctypes.POINTER(AccentPolicy)).contents
            captured.append(
                (data.attribute, policy.accent_state, policy.accent_flags, policy.gradient_color)
            )
            return 1

        user32 = Mock()
        user32.SetWindowCompositionAttribute.side_effect = set_composition
        dwmapi = Mock()
        dwmapi.DwmSetWindowAttribute.return_value = 0
        version = Mock(build=19044)
        window = Mock()
        window.winId.return_value = 123

        with patch("clipsave_app.services.os.name", "nt"), patch(
            "clipsave_app.services.sys.getwindowsversion", return_value=version
        ), patch("clipsave_app.services._windows_effect_apis", return_value=(user32, dwmapi)):
            self.assertTrue(apply_windows_acrylic(window, False))
            self.assertTrue(apply_windows_acrylic(window, True))

        self.assertEqual(
            captured,
            [
                (19, 3, 0, 0x00FFFFFF),
                (19, 3, 0, 0x00FFFFFF),
            ],
        )

    def test_windows_11_22h2_uses_system_backdrop_and_checks_hresult(self):
        user32 = Mock()
        dwmapi = Mock()
        dwmapi.DwmSetWindowAttribute.return_value = 0
        version = Mock(build=22621)
        window = Mock()
        window.winId.return_value = 456

        with patch("clipsave_app.services.os.name", "nt"), patch(
            "clipsave_app.services.sys.getwindowsversion", return_value=version
        ), patch(
            "clipsave_app.services._windows_effect_apis", return_value=(user32, dwmapi)
        ):
            self.assertTrue(apply_windows_acrylic(window, True))

        attributes = [call.args[1] for call in dwmapi.DwmSetWindowAttribute.call_args_list]
        self.assertEqual(attributes, [38, 33, 34, 20])
        user32.SetWindowCompositionAttribute.assert_not_called()

    def test_failed_backdrop_falls_back_and_reports_total_failure(self):
        user32 = Mock()
        user32.SetWindowCompositionAttribute.return_value = 0
        dwmapi = Mock()
        dwmapi.DwmSetWindowAttribute.return_value = -1
        version = Mock(build=22621)
        window = Mock()
        window.winId.return_value = 789

        with patch("clipsave_app.services.os.name", "nt"), patch(
            "clipsave_app.services.sys.getwindowsversion", return_value=version
        ), patch(
            "clipsave_app.services._windows_effect_apis", return_value=(user32, dwmapi)
        ):
            self.assertFalse(apply_windows_acrylic(window, False))

        user32.SetWindowCompositionAttribute.assert_called_once()


class BoundedTaskExecutorTests(unittest.TestCase):
    def test_global_executor_can_shutdown_and_be_recreated(self):
        self.assertTrue(shutdown_ai_ocr_task_executor())
        first = ai_ocr_task_executor()
        self.assertTrue(shutdown_ai_ocr_task_executor())
        second = ai_ocr_task_executor()
        try:
            self.assertIsNot(first, second)
        finally:
            self.assertTrue(shutdown_ai_ocr_task_executor())

    def test_shutdown_can_retry_after_timeout_before_sentinels_are_queued(self):
        executor = BoundedTaskExecutor(max_active=1, max_queued=1, memory_budget_bytes=100)
        started = threading.Event()
        release = threading.Event()

        def blocking(_cancel_event):
            started.set()
            release.wait(2)

        executor.submit(blocking)
        executor.submit(lambda _cancel_event: None)
        self.assertTrue(started.wait(1))
        self.assertFalse(executor.shutdown(timeout=0.0))
        release.set()
        self.assertTrue(executor.shutdown(timeout=2.0))
        self.assertTrue(all(not worker.is_alive() for worker in executor._workers))

    def test_active_queue_limit_and_queued_cancellation(self):
        executor = BoundedTaskExecutor(max_active=1, max_queued=1, memory_budget_bytes=100)
        started = threading.Event()
        release = threading.Event()
        queued_ran = threading.Event()

        def blocking(_cancel_event):
            started.set()
            release.wait(2)

        first = executor.submit(blocking, estimated_bytes=40)
        self.assertTrue(started.wait(1))
        second = executor.submit(lambda _cancel_event: queued_ran.set(), estimated_bytes=40)
        with self.assertRaises(TaskCapacityExceeded):
            executor.submit(lambda _cancel_event: None)

        second.cancel()
        release.set()
        self.assertTrue(first.wait(1))
        self.assertTrue(second.wait(1))
        self.assertFalse(queued_ran.is_set())
        self.assertEqual(executor.reserved_bytes, 0)
        self.assertTrue(executor.shutdown())

    def test_memory_budget_is_global_across_active_and_queued_tasks(self):
        executor = BoundedTaskExecutor(max_active=1, max_queued=2, memory_budget_bytes=50)
        release = threading.Event()
        handle = executor.submit(lambda _cancel_event: release.wait(2), estimated_bytes=40)
        with self.assertRaisesRegex(TaskCapacityExceeded, "内存"):
            executor.submit(lambda _cancel_event: None, estimated_bytes=11)
        release.set()
        self.assertTrue(handle.wait(1))
        self.assertTrue(executor.shutdown())


if __name__ == "__main__":
    unittest.main()
