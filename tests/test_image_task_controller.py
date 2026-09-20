import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from clipsave_app.image_task_controller import ImageTaskController
from clipsave_app.task_supervisor import TaskSupervisor


class FakeSnapshot:
    def require_current(self):
        return None


class FakeService:
    def describe_image(self, _snapshot, _cancel, *, expected_sha256):
        return f"description:{expected_sha256[:4]}"

    def ocr_image(self, _snapshot, _cancel, *, expected_sha256):
        return f"ocr:{expected_sha256[:4]}"

    def expand_search_query(self, query, _cancel):
        return [query, f"{query} expanded"]


class FakeDatabase:
    def __init__(self, *, saved=True, error=None, item=None):
        self.saved = saved
        self.error = error
        self.item = item
        self.calls = []

    def get_item(self, _item_id):
        return self.item

    def update_ai_if_current(self, item_id, expected_hash, text):
        self.calls.append(("ai", item_id, expected_hash, text))
        if self.error is not None:
            raise self.error
        return self.saved

    def update_ocr_if_current(self, item_id, expected_hash, text):
        self.calls.append(("ocr", item_id, expected_hash, text))
        if self.error is not None:
            raise self.error
        return self.saved


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


class ImageTaskControllerTests(unittest.TestCase):
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

    @patch("clipsave_app.image_task_controller.preflight_image_file", return_value=FakeSnapshot())
    @patch("clipsave_app.image_task_controller.ai_ocr_task_executor", return_value=ImmediateExecutor())
    def test_image_operation_owns_request_and_emits_result(self, _executor, _preflight):
        controller = ImageTaskController(TaskSupervisor())
        results = []
        controller.ai_succeeded.connect(lambda *args: results.append(args))
        item = {"id": 7, "path": "image.png", "content_hash": "a" * 64}

        request = controller.start_image_operation(
            item,
            FakeService(),
            operation="ai",
            automatic=True,
            estimated_bytes=16,
        )
        self.app.processEvents()

        self.assertEqual(controller.ai_requests[7], request)
        self.assertIn(7, controller.automatic_ai_items)
        self.assertEqual(results[0][2], 7)
        self.assertEqual(results[0][3], "description:aaaa")

    @patch("clipsave_app.image_task_controller.ai_ocr_task_executor", return_value=ImmediateExecutor())
    def test_expanded_search_has_controller_owned_request(self, _executor):
        controller = ImageTaskController(TaskSupervisor())
        results = []
        controller.expanded_search_succeeded.connect(lambda *args: results.append(args))

        request = controller.start_expanded_search("query", FakeService())
        self.app.processEvents()

        self.assertEqual(controller.expanded_search_request, request)
        self.assertEqual(results[0][2], "query")
        self.assertEqual(results[0][3], ["query", "query expanded"])

    def test_shutdown_cleanup_keeps_requests_until_tasks_really_finish(self):
        supervisor = TaskSupervisor()
        controller = ImageTaskController(supervisor)
        ai_token = object()
        search_token = object()
        ai_handle = ManualHandle()
        search_handle = ManualHandle()
        supervisor.track_bounded(ai_token, ai_handle)
        supervisor.track_bounded(search_token, search_handle)
        controller.ai_requests[7] = (ai_token, QObject(controller))
        controller.automatic_ai_items.add(7)
        controller.expanded_search_request = (search_token, QObject(controller))

        cancelled = controller.cancel_for_shutdown()

        self.assertEqual(cancelled, {ai_token, search_token})
        self.assertTrue(ai_handle.cancelled)
        self.assertTrue(search_handle.cancelled)
        cleanup = controller.cleanup_cancelled(cancelled)
        self.assertTrue(cleanup.pending)
        self.assertIn(7, controller.ai_requests)
        self.assertIsNotNone(controller.expanded_search_request)

        ai_handle.done_event.set()
        search_handle.done_event.set()
        cleanup = controller.cleanup_cancelled(cancelled)

        self.assertFalse(cleanup.pending)
        self.assertEqual(cleanup.ai_item_ids, (7,))
        self.assertTrue(cleanup.expanded_search_finished)
        self.assertNotIn(7, controller.ai_requests)
        self.assertNotIn(7, controller.automatic_ai_items)
        self.assertIsNone(controller.expanded_search_request)
        self.assertNotIn(ai_token, supervisor.bounded_tasks)
        self.assertNotIn(search_token, supervisor.bounded_tasks)

    def test_cancel_automatic_only_removes_automatic_requests(self):
        supervisor = TaskSupervisor()
        controller = ImageTaskController(supervisor)
        automatic_token = object()
        manual_token = object()
        automatic_handle = ManualHandle()
        manual_handle = ManualHandle()
        supervisor.track_bounded(automatic_token, automatic_handle)
        supervisor.track_bounded(manual_token, manual_handle)
        controller.ocr_requests[3] = (automatic_token, QObject(controller))
        controller.ocr_requests[4] = (manual_token, QObject(controller))
        controller.automatic_ocr_items.add(3)

        cancelled_ids = controller.cancel_automatic("ocr")

        self.assertEqual(cancelled_ids, (3,))
        self.assertTrue(automatic_handle.cancelled)
        self.assertFalse(manual_handle.cancelled)
        self.assertNotIn(3, controller.ocr_requests)
        self.assertIn(4, controller.ocr_requests)

    def test_finish_operation_rejects_stale_marker_and_returns_automatic_flag(self):
        controller = ImageTaskController(TaskSupervisor())
        token = object()
        marker = QObject(controller)
        controller.ai_requests[8] = (token, marker)
        controller.automatic_ai_items.add(8)

        matched, automatic = controller.finish_operation(
            8,
            "ai",
            token,
            QObject(controller),
        )
        self.assertFalse(matched)
        self.assertFalse(automatic)
        self.assertIn(8, controller.ai_requests)

        matched, automatic = controller.finish_operation(
            8,
            "ai",
            token,
            marker,
        )
        self.assertTrue(matched)
        self.assertTrue(automatic)
        self.assertNotIn(8, controller.ai_requests)
        self.assertNotIn(8, controller.automatic_ai_items)

    def test_finish_expanded_search_only_claims_current_request(self):
        controller = ImageTaskController(TaskSupervisor())
        token = object()
        marker = QObject(controller)
        controller.expanded_search_request = (token, marker)

        self.assertFalse(
            controller.finish_expanded_search(token, QObject(controller))
        )
        self.assertIsNotNone(controller.expanded_search_request)
        self.assertTrue(controller.finish_expanded_search(token, marker))
        self.assertIsNone(controller.expanded_search_request)

    def test_commit_operation_owns_cas_persistence_and_request_finish(self):
        database = FakeDatabase(saved=True)
        controller = ImageTaskController(TaskSupervisor(), database=database)
        token = object()
        marker = QObject(controller)
        controller.ai_requests[8] = (token, marker)
        controller.automatic_ai_items.add(8)

        result = controller.commit_operation(
            8,
            "ai",
            token,
            marker,
            "a" * 64,
            "description",
        )

        self.assertTrue(result.matched)
        self.assertTrue(result.automatic)
        self.assertTrue(result.saved)
        self.assertFalse(result.stale_content)
        self.assertIsNone(result.error)
        self.assertEqual(
            database.calls,
            [("ai", 8, "a" * 64, "description")],
        )
        self.assertNotIn(8, controller.ai_requests)

    def test_commit_operation_reports_stale_content_and_database_errors(self):
        stale_database = FakeDatabase(saved=False)
        stale = ImageTaskController(TaskSupervisor(), database=stale_database)
        stale_token = object()
        stale_marker = QObject(stale)
        stale.ocr_requests[3] = (stale_token, stale_marker)

        stale_result = stale.commit_operation(
            3,
            "ocr",
            stale_token,
            stale_marker,
            "b" * 64,
            "text",
        )

        self.assertTrue(stale_result.matched)
        self.assertFalse(stale_result.saved)
        self.assertTrue(stale_result.stale_content)

        failed_database = FakeDatabase(error=OSError("db failed"))
        failed = ImageTaskController(TaskSupervisor(), database=failed_database)
        failed_token = object()
        failed_marker = QObject(failed)
        failed.ai_requests[4] = (failed_token, failed_marker)

        failed_result = failed.commit_operation(
            4,
            "ai",
            failed_token,
            failed_marker,
            "c" * 64,
            "description",
        )

        self.assertTrue(failed_result.matched)
        self.assertEqual(failed_result.error, "db failed")
        self.assertNotIn(4, failed.ai_requests)

    def test_prepare_image_operation_owns_validation_duplicate_and_estimate(self):
        item = {
            "id": 7,
            "kind": "image",
            "path": "image.png",
            "content_hash": "a" * 64,
            "width": 20,
            "height": 10,
        }
        database = FakeDatabase(item=item)
        controller = ImageTaskController(TaskSupervisor(), database=database)
        service = FakeService()
        service.configured = True

        result = controller.prepare_image_operation(
            7,
            service,
            operation="ai",
            automatic=False,
        )

        self.assertTrue(result.ready)
        self.assertIs(result.item, item)
        self.assertEqual(result.estimated_bytes, 800)

        token = object()
        marker = QObject(controller)
        controller.ai_requests[7] = (token, marker)
        duplicate = controller.prepare_image_operation(
            7,
            service,
            operation="ai",
            automatic=True,
        )
        self.assertEqual(duplicate.state.value, "already_running")

    def test_automatic_operations_for_item_owns_result_and_request_checks(self):
        item = {
            "id": 9,
            "kind": "image",
            "path": "image.png",
            "content_hash": "b" * 64,
            "ocr_text": "",
            "ai_description": "",
        }
        controller = ImageTaskController(
            TaskSupervisor(),
            database=FakeDatabase(item=item),
        )
        service = FakeService()
        service.configured = True

        self.assertEqual(
            controller.automatic_operations_for_item(
                9,
                service,
                auto_ocr=True,
                auto_description=True,
            ),
            ("ocr", "ai"),
        )

        controller.ocr_requests[9] = (object(), QObject(controller))
        item["ai_description"] = "already done"
        self.assertEqual(
            controller.automatic_operations_for_item(
                9,
                service,
                auto_ocr=True,
                auto_description=True,
            ),
            (),
        )
