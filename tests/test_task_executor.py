import threading
import unittest

from clipsave_app.task_executor import BoundedTaskExecutor


class TaskExecutorRecoveryTests(unittest.TestCase):
    def test_failed_shutdown_can_resume_and_drain_again(self):
        executor = BoundedTaskExecutor(max_active=1, max_queued=2)
        started, release, ran = threading.Event(), threading.Event(), threading.Event()
        try:
            first = executor.submit(lambda _cancel: (started.set(), release.wait(2)))
            self.assertTrue(started.wait(1))
            self.assertFalse(executor.shutdown(timeout=0.02))
            executor.resume_after_failed_shutdown()
            second = executor.submit(lambda _cancel: ran.set())
            release.set()
            self.assertTrue(first.wait(1))
            self.assertTrue(second.wait(1))
            self.assertTrue(ran.is_set())
            self.assertTrue(executor.shutdown(timeout=1))
            self.assertEqual(executor._queue.unfinished_tasks, 0)
        finally:
            release.set()
            executor.shutdown(timeout=1)

    def test_resuming_replaces_worker_already_committed_to_exit(self):
        executor = BoundedTaskExecutor(max_active=2, max_queued=2)
        started, release = threading.Event(), threading.Event()
        retiring, finish_retiring, ran = threading.Event(), threading.Event(), threading.Event()
        original_done = executor._queue.task_done

        def delayed_exit():
            if threading.current_thread() in executor._retiring_workers:
                retiring.set()
                finish_retiring.wait(2)
            original_done()

        executor._queue.task_done = delayed_exit
        try:
            executor.submit(lambda _cancel: (started.set(), release.wait(2)))
            self.assertTrue(started.wait(1))
            self.assertFalse(executor.shutdown(timeout=0.03))
            self.assertTrue(retiring.wait(1))
            executor.resume_after_failed_shutdown()
            executor.submit(lambda _cancel: ran.set())
            self.assertTrue(ran.wait(1), 'A retiring worker was incorrectly counted as available')
        finally:
            release.set()
            finish_retiring.set()
            executor.shutdown(timeout=1)
