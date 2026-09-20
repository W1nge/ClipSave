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


class ManualHandle:
    def __init__(self):
        self.done_event = threading.Event()
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout=None):
        return self.done_event.wait(timeout)


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

    def test_delete_start_failure_rolls_back_controller_state_and_raises(self):
        supervisor = TaskSupervisor()
        controller = LibraryMutationController(FakeDatabase(), supervisor)

        with patch.object(supervisor, "start_thread", side_effect=RuntimeError("no threads")):
            with self.assertRaisesRegex(RuntimeError, "no threads"):
                controller.start_delete(
                    {"id": 9, "path": None, "content_hash": None, "file_size": 0},
                    was_selected=True,
                    detail_was_visible=True,
                    is_managed=lambda _path: False,
                    recycle=Mock(),
                    library_root=Path("."),
                )

        self.assertNotIn(9, controller.delete_requests)
        self.assertNotIn(9, controller.pending_delete_item_ids)

    def test_shutdown_cleanup_waits_for_copy_and_returns_delete_restore(self):
        supervisor = TaskSupervisor()
        controller = LibraryMutationController(FakeDatabase(), supervisor)
        copy_token = object()
        delete_token = object()
        copy_handle = ManualHandle()
        supervisor.track_bounded(copy_token, copy_handle)
        controller.copy_request = (copy_token, object(), 5)
        controller.delete_requests[9] = (delete_token, object(), True, True)
        controller.pending_delete_item_ids.add(9)

        cancelled = controller.cancel_for_shutdown()

        self.assertEqual(cancelled, {copy_token, delete_token})
        self.assertTrue(copy_handle.cancelled)
        cleanup = controller.cleanup_cancelled(cancelled)
        self.assertTrue(cleanup.pending)
        self.assertEqual(len(cleanup.delete_restores), 1)
        restore = cleanup.delete_restores[0]
        self.assertEqual(restore.item_id, 9)
        self.assertTrue(restore.was_selected)
        self.assertTrue(restore.detail_was_visible)
        self.assertNotIn(9, controller.delete_requests)
        self.assertNotIn(9, controller.pending_delete_item_ids)
        self.assertIsNotNone(controller.copy_request)

        copy_handle.done_event.set()
        cleanup = controller.cleanup_cancelled(cancelled)

        self.assertFalse(cleanup.pending)
        self.assertTrue(cleanup.copy_finished)
        self.assertIsNone(controller.copy_request)
        self.assertNotIn(copy_token, supervisor.bounded_tasks)

    def test_clear_background_state_does_not_clear_active_import(self):
        controller = LibraryMutationController(FakeDatabase(), TaskSupervisor())
        import_request = (object(), object())
        controller.import_request = import_request
        controller.copy_request = (object(), object(), 1)
        controller.delete_requests[2] = (object(), object(), False, False)
        controller.pending_delete_item_ids.add(2)

        controller.clear_background_state()

        self.assertIs(controller.import_request, import_request)
        self.assertIsNone(controller.copy_request)
        self.assertFalse(controller.delete_requests)
        self.assertFalse(controller.pending_delete_item_ids)

    def test_finish_methods_claim_only_matching_requests(self):
        controller = LibraryMutationController(FakeDatabase(), TaskSupervisor())
        import_token, import_marker = object(), object()
        copy_token, copy_marker = object(), object()
        delete_token, delete_marker = object(), object()
        controller.import_request = (import_token, import_marker)
        controller.copy_request = (copy_token, copy_marker, 4)
        controller.delete_requests[9] = (delete_token, delete_marker, True, False)
        controller.pending_delete_item_ids.add(9)

        self.assertFalse(controller.finish_import(import_token, object()))
        self.assertFalse(controller.finish_copy(copy_token, object()))
        self.assertIsNone(controller.finish_delete(9, delete_token, object()))
        self.assertIsNotNone(controller.import_request)
        self.assertIsNotNone(controller.copy_request)
        self.assertIn(9, controller.delete_requests)

        self.assertTrue(controller.finish_import(import_token, import_marker))
        self.assertTrue(controller.finish_copy(copy_token, copy_marker))
        restore = controller.finish_delete(9, delete_token, delete_marker)
        self.assertIsNotNone(restore)
        self.assertTrue(restore.was_selected)
        self.assertFalse(restore.detail_was_visible)
        self.assertIsNone(controller.import_request)
        self.assertIsNone(controller.copy_request)
        self.assertNotIn(9, controller.delete_requests)
        self.assertNotIn(9, controller.pending_delete_item_ids)
