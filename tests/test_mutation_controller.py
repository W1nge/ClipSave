import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from clipsave_app.database import ImportFileDetails
from clipsave_app.mutation_controller import LibraryMutationController
from clipsave_app.task_supervisor import TaskSupervisor


class FakeDatabase:
    def __init__(self):
        self.removed = []
        self.missing = []
        self.import_results = []

    def import_file(self, path, **_kwargs):
        if self.import_results:
            result = self.import_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return ImportFileDetails(True, False, False, None, "0" * 64)

    def remove_item(self, item_id):
        self.removed.append(item_id)

    def mark_item_missing(self, item_id):
        self.missing.append(item_id)


class ImmediateHandle:
    def __init__(self, target):
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        target(self.cancel_event)
        self.done_event.set()

    def cancel(self):
        self.cancel_event.set()

    def wait(self, timeout=None):
        return self.done_event.wait(timeout)


class ImmediateExecutor:
    def submit(self, target, **_kwargs):
        return ImmediateHandle(target)


class Snapshot:
    decoded_bytes = 16

    def __init__(self, path):
        self.path = path

    def require_current(self):
        return None


class MutationControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_import_counts_individual_failures_without_aborting_batch(self):
        database = FakeDatabase()
        database.import_results = [
            ImportFileDetails(True, False, False, None, "0" * 64),
            PermissionError("blocked"),
        ]
        controller = LibraryMutationController(database, TaskSupervisor())
        results = []
        controller.import_finished.connect(lambda _t, _m, result: results.append(result))

        controller.start_import(["good.md", "blocked.md"])

        self.assertTrue(self.wait_for(lambda: bool(results)))
        self.assertEqual(results[0]["added"], 1)
        self.assertEqual(len(results[0]["failed"]), 1)

    @patch("clipsave_app.mutation_controller.ai_ocr_task_executor", return_value=ImmediateExecutor())
    def test_copy_decodes_image_in_controller_worker(self, _executor):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "copy.png"
            image = QImage(4, 4, QImage.Format.Format_RGB32)
            image.fill(QColor("white"))
            self.assertTrue(image.save(str(path), "PNG"))
            controller = LibraryMutationController(FakeDatabase(), TaskSupervisor())
            results = []
            controller.copy_succeeded.connect(lambda *args: results.append(args))

            controller.start_copy_image(7, Snapshot(path))
            self.app.processEvents()

            self.assertEqual(results[0][2], 7)
            self.assertFalse(results[0][3].isNull())

    def test_delete_owns_recycle_and_database_mutation(self):
        database = FakeDatabase()
        controller = LibraryMutationController(database, TaskSupervisor())
        recycle = Mock()
        results = []
        controller.delete_finished.connect(lambda *args: results.append(args))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "managed.png"
            path.write_bytes(b"data")
            controller.start_delete(
                {
                    "id": 9,
                    "path": str(path),
                    "content_hash": "a" * 64,
                    "file_size": 4,
                },
                was_selected=False,
                detail_was_visible=False,
                is_managed=lambda _path: True,
                recycle=recycle,
                library_root=Path(temp),
            )
            self.assertTrue(self.wait_for(lambda: bool(results)))

        recycle.assert_called_once()
        self.assertEqual(database.removed, [9])
        self.assertEqual(results[0][3]["outcome"], "deleted")
