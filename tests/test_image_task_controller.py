import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
