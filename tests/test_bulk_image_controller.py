import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from clipsave_app.bulk_image_controller import BulkImageController
from clipsave_app.bulk_image_job import BulkImageResult


class FakeDatabase:
    def __init__(self):
        self.ids = [1, 2]

    def item_ids(self, *, kind, sort):
        self.last_query = (kind, sort)
        return list(self.ids)


class BulkImageControllerTests(unittest.TestCase):
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

    def test_start_failure_keeps_resumable_checkpoint_state(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = BulkImageController(
                FakeDatabase(),
                Path(temp) / "bulk.json",
                start_task=lambda _token, _work: (_ for _ in ()).throw(
                    RuntimeError("thread unavailable")
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                controller.start(object())

            state = controller.snapshot()
            self.assertFalse(state["active"])
            self.assertTrue(state["resumable"])
            self.assertEqual(state["processed"], 0)
            self.assertEqual(state["total"], 2)

    def test_worker_completion_clears_active_request(self):
        captured = {}

        def start_task(token, work):
            captured["token"] = token
            captured["work"] = work

        with tempfile.TemporaryDirectory() as temp:
            controller = BulkImageController(
                FakeDatabase(),
                Path(temp) / "bulk.json",
                start_task=start_task,
            )
            controller.start(object())
            self.assertIsNotNone(controller.request)

            controller._worker_finished.emit(
                captured["token"],
                BulkImageResult(2, 0, 0, 0, 0, cancelled=True),
            )
            self.assertTrue(self.wait_for(lambda: controller.request is None))

    def test_cancel_clears_request_and_uses_injected_task_canceller(self):
        captured = {}
        cancelled = []

        def start_task(token, work):
            captured["token"] = token
            captured["work"] = work

        with tempfile.TemporaryDirectory() as temp:
            controller = BulkImageController(
                FakeDatabase(),
                Path(temp) / "bulk.json",
                start_task=start_task,
                cancel_task=cancelled.append,
            )
            controller.start(object())

            token = controller.cancel()

            self.assertIs(token, captured["token"])
            self.assertEqual(cancelled, [token])
            self.assertIsNone(controller.request)
            self.assertTrue(controller.snapshot()["resumable"])
