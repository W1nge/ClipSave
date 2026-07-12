import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image as PILImage
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from clipsave_app.constants import (
    MAX_AI_RESPONSE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
    MAX_EMBEDDING_DIMENSIONS,
)
from clipsave_app.database import LibraryDatabase
from clipsave_app.services import AIService, ClipboardService, OperationCancelled


class FakeMimeData:
    def __init__(self, *, has_image: bool = False, has_text: bool = False):
        self._has_image = has_image
        self._has_text = has_text

    def hasImage(self):
        return self._has_image

    def hasText(self):
        return self._has_text


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

    def test_blank_text_advances_sequence_without_saving(self):
        clipboard = FakeClipboard(texts=["  \n"])
        sequences = iter([10, 10])
        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
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

    def test_sequence_change_during_read_retries_latest_snapshot(self):
        clipboard = FakeClipboard(texts=["first", "  second\n"])
        sequences = iter([10, 11, 11, 11])
        saved = []
        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
        self.service.clipboard_sequence = lambda: next(sequences)
        self.service.save_text = lambda text: saved.append(text) or True

        self.service.poll()
        self.assertTrue(self.service.wait_for_idle())

        self.assertEqual(saved, ["  second\n"])
        self.assertEqual(self.service.last_text, "  second\n")
        self.assertEqual(self.service.last_clipboard_sequence, 11)

    def test_failed_save_does_not_advance_clipboard_state(self):
        clipboard = FakeClipboard(texts=["new text"])
        sequences = iter([10, 10])
        failures = []
        self.service.last_text = "old text"
        self.service.last_clipboard_sequence = 9
        self.service._clipboard = lambda: clipboard
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
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
        ):
            self.assertTrue(self.service.save_text("persist me"))

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(self.database.query_items()), 1)
        self.assertIn("disk full", failures[0])

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


class AIServiceTests(unittest.TestCase):
    def test_local_http_service_can_be_configured_without_api_key(self):
        service = AIService("http://127.0.0.1:11434/v1", "", "vision", "embedding")
        response = FakeResponse(json.dumps({"ok": True}).encode("utf-8"))

        with patch("clipsave_app.services.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(service._post("/test", {}), {"ok": True})

        request = urlopen.call_args.args[0]
        self.assertTrue(service.configured)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertLessEqual(response.read_limit, 64 * 1024)

    def test_ai_response_size_and_shape_are_validated(self):
        service = AIService("http://localhost/v1", "", "vision", "embedding")
        response = FakeResponse(lambda limit: b"x" * limit)
        with patch("clipsave_app.services.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "响应过大"):
                service._post("/test", {})

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "image.png"
            PILImage.new("RGB", (4, 4), "white").save(path)
            service._post = lambda _path, _payload, _cancel_event=None: {"choices": []}
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
        with patch("clipsave_app.services.urllib.request.urlopen") as urlopen:
            with self.assertRaises(OperationCancelled):
                service._post("/test", {}, cancel_event)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
