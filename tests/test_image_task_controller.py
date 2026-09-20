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
