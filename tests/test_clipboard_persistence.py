import threading
import unittest

from clipsave_app.clipboard_persistence import ClipboardPersistenceWorker


class ClipboardPersistenceWorkerTests(unittest.TestCase):
    def test_worker_is_lazy_and_processes_accepted_task(self):
        processed = []
        worker = ClipboardPersistenceWorker(
            queue_size=2,
            memory_budget_bytes=1024,
            wait_interval_seconds=0.01,
            process_task=lambda task: processed.append(task.value) or task.value,
            on_success=lambda task, result: worker.release_pending(task),
            on_failure=lambda task, _message: worker.release_pending(task),
            on_suppressed_finish=lambda task, _result: worker.release_pending(task),
            report_failure=lambda _message: None,
        )

        self.assertIsNone(worker.worker)
        worker.enqueue("text", "hello", 1)

        self.assertTrue(worker.idle_event.wait(1.0))
        self.assertEqual(processed, ["hello"])
        self.assertTrue(worker.shutdown(1.0, wait_for_idle=lambda _timeout: True))

    def test_memory_budget_rejects_without_starting_extra_work(self):
        failures = []
        blocker = threading.Event()
        worker = ClipboardPersistenceWorker(
            queue_size=1,
            memory_budget_bytes=4,
            wait_interval_seconds=0.01,
            process_task=lambda task: blocker.wait(1.0) or task.value,
            on_success=lambda task, _result: worker.release_pending(task),
            on_failure=lambda task, _message: worker.release_pending(task),
            on_suppressed_finish=lambda task, _result: worker.release_pending(task),
            report_failure=failures.append,
        )

        worker.pending_bytes = 4
        worker.enqueue("text", "x", 2)

        self.assertEqual(worker.tasks.qsize(), 0)
        self.assertTrue(any("内存过高" in message for message in failures))
