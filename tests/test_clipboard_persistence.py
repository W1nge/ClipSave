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

    def test_concurrent_duplicate_sequence_is_reserved_once(self):
        blocker = threading.Event()
        worker = ClipboardPersistenceWorker(
            queue_size=4,
            memory_budget_bytes=1024,
            wait_interval_seconds=0.01,
            process_task=lambda task: blocker.wait(1.0) or task.value,
            on_success=lambda task, _result: worker.release_pending(task),
            on_failure=lambda task, _message: worker.release_pending(task),
            on_suppressed_finish=lambda task, _result: worker.release_pending(task),
            report_failure=lambda _message: None,
        )
        gate = threading.Barrier(3)

        def enqueue() -> None:
            gate.wait()
            worker.enqueue("text", "same", 7)

        threads = [threading.Thread(target=enqueue) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(worker.next_task_token, 2)
        self.assertEqual(worker.pending_sequences, {7})
        self.assertEqual(worker.pending_bytes, 4)
        blocker.set()
        self.assertTrue(worker.idle_event.wait(1.0))
        self.assertTrue(worker.shutdown(1.0, wait_for_idle=lambda _timeout: True))

    def test_concurrent_memory_reservation_cannot_exceed_budget(self):
        blocker = threading.Event()
        failures = []
        worker = ClipboardPersistenceWorker(
            queue_size=4,
            memory_budget_bytes=4,
            wait_interval_seconds=0.01,
            process_task=lambda task: blocker.wait(1.0) or task.value,
            on_success=lambda task, _result: worker.release_pending(task),
            on_failure=lambda task, _message: worker.release_pending(task),
            on_suppressed_finish=lambda task, _result: worker.release_pending(task),
            report_failure=failures.append,
        )
        gate = threading.Barrier(3)

        def enqueue(value: str, sequence: int) -> None:
            gate.wait()
            worker.enqueue("text", value, sequence)

        threads = [
            threading.Thread(target=enqueue, args=("aaaa", 1)),
            threading.Thread(target=enqueue, args=("bbbb", 2)),
        ]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(worker.next_task_token, 2)
        self.assertEqual(worker.pending_bytes, 4)
        self.assertEqual(len(worker.pending_sequences), 1)
        self.assertTrue(any("内存过高" in message for message in failures))
        blocker.set()
        self.assertTrue(worker.idle_event.wait(1.0))
        self.assertTrue(worker.shutdown(1.0, wait_for_idle=lambda _timeout: True))
