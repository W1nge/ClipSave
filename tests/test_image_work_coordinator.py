import threading
import time
import unittest
from unittest.mock import patch

from clipsave_app.image_work_coordinator import ImageWorkCoordinator


class ImmediateHandle:
    def __init__(self, cancel_event):
        self.cancel_event = cancel_event
        self.done_event = threading.Event()
        self.exception = None

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def cancel(self):
        self.cancel_event.set()

    def wait(self, timeout=None):
        return self.done_event.wait(timeout)


class ImmediateExecutor:
    def __init__(self):
        self.estimates = []

    def submit(self, target, *, estimated_bytes=0, cancel_event=None):
        cancel_event = cancel_event or threading.Event()
        self.estimates.append(estimated_bytes)
        handle = ImmediateHandle(cancel_event)
        try:
            if not handle.cancelled:
                target(cancel_event)
        except BaseException as exc:
            handle.exception = exc
        finally:
            handle.done_event.set()
        return handle


class ImageWorkCoordinatorTests(unittest.TestCase):
    def test_bounded_run_uses_shared_executor_memory_reservation(self):
        coordinator = ImageWorkCoordinator()
        executor = ImmediateExecutor()
        cancel_event = threading.Event()

        with patch(
            "clipsave_app.image_work_coordinator.ai_ocr_task_executor",
            return_value=executor,
        ):
            result = coordinator.run_bounded(
                7,
                "ocr",
                lambda _cancel: "done",
                estimated_bytes=1234,
                cancel_event=cancel_event,
            )

        self.assertEqual(result, "done")
        self.assertEqual(executor.estimates, [1234])
        self.assertFalse(coordinator.is_active(7, "ocr"))

    def test_duplicate_operation_waits_until_current_owner_releases(self):
        coordinator = ImageWorkCoordinator()
        executor = ImmediateExecutor()
        owner = object()
        self.assertTrue(coordinator.claim(9, "ai", owner))
        target_started = threading.Event()

        def release_owner():
            time.sleep(0.05)
            coordinator.release(9, "ai", owner)

        releaser = threading.Thread(target=release_owner, daemon=True)
        releaser.start()
        started = time.monotonic()
        with patch(
            "clipsave_app.image_work_coordinator.ai_ocr_task_executor",
            return_value=executor,
        ):
            result = coordinator.run_bounded(
                9,
                "ai",
                lambda _cancel: target_started.set() or "description",
                estimated_bytes=64,
                cancel_event=threading.Event(),
            )
        elapsed = time.monotonic() - started
        releaser.join(1.0)

        self.assertEqual(result, "description")
        self.assertTrue(target_started.is_set())
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertFalse(coordinator.is_active(9, "ai"))
