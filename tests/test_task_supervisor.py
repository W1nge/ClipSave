import threading
import time
import unittest

from clipsave_app.task_supervisor import TaskSupervisor


class FakeBoundedHandle:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def wait(self, timeout=None) -> bool:
        return self.done_event.wait(timeout)


class TaskSupervisorTests(unittest.TestCase):
    def test_regular_task_is_removed_after_completion(self):
        supervisor = TaskSupervisor()
        token = object()
        ran = threading.Event()

        supervisor.start_thread(token, lambda _cancel: ran.set())

        self.assertTrue(ran.wait(1.0))
        deadline = time.monotonic() + 1.0
        while token in supervisor.regular_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertNotIn(token, supervisor.regular_tasks)

    def test_wait_for_token_cancels_regular_task(self):
        supervisor = TaskSupervisor()
        token = object()

        def work(cancel_event: threading.Event) -> None:
            cancel_event.wait(1.0)

        supervisor.start_thread(token, work)
        self.assertTrue(supervisor.wait_for_token(token, 1.0))

    def test_cancel_all_can_ignore_unfinished_bounded_handle(self):
        supervisor = TaskSupervisor()
        token = object()
        handle = FakeBoundedHandle()
        supervisor.track_bounded(token, handle)

        self.assertTrue(supervisor.cancel_all_and_wait(0.05, require_bounded=False))
        self.assertTrue(handle.cancel_event.is_set())
        self.assertFalse(handle.done_event.is_set())

    def test_duplicate_token_is_rejected(self):
        supervisor = TaskSupervisor()
        token = object()
        blocker = threading.Event()

        def work(cancel_event: threading.Event) -> None:
            while not cancel_event.is_set() and not blocker.is_set():
                time.sleep(0.005)

        supervisor.start_thread(token, work)
        with self.assertRaises(ValueError):
            supervisor.track_bounded(token, FakeBoundedHandle())
        self.assertTrue(supervisor.wait_for_token(token, 1.0))
