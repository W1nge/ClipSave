from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtGui import QImage


@dataclass(frozen=True)
class ClipboardTask:
    token: int
    kind: str
    value: QImage | str
    sequence: int | None
    estimated_bytes: int


class ClipboardPersistenceWorker:
    """Bounded persistence queue and worker lifecycle for clipboard captures."""

    def __init__(
        self,
        *,
        queue_size: int,
        memory_budget_bytes: int,
        wait_interval_seconds: float,
        process_task: Callable[[ClipboardTask], object],
        on_success: Callable[[ClipboardTask, object], None],
        on_failure: Callable[[ClipboardTask, str], None],
        on_suppressed_finish: Callable[[ClipboardTask, object], None],
        report_failure: Callable[[str], None],
    ) -> None:
        self.queue_size = max(1, int(queue_size))
        self.memory_budget_bytes = max(1, int(memory_budget_bytes))
        self.wait_interval_seconds = max(0.001, float(wait_interval_seconds))
        self.process_task = process_task
        self.on_success = on_success
        self.on_failure = on_failure
        self.on_suppressed_finish = on_suppressed_finish
        self.report_failure = report_failure

        self.tasks: queue.Queue[ClipboardTask] = queue.Queue(self.queue_size)
        self.pending_sequences: set[int] = set()
        self.pending_without_sequence = False
        self.pending_bytes = 0
        self.next_task_token = 1
        self.accepting_tasks = True
        self.shutdown_complete = False
        self.idle_event = threading.Event()
        self.idle_event.set()
        self.state_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()
        self.stop_requested = False
        self.suppress_signals = False
        self.worker_exited = threading.Event()
        self.worker: threading.Thread | None = None

    def ensure_started(self) -> None:
        with self.lifecycle_lock:
            worker = self.worker
            if worker is not None and worker.is_alive():
                return
            if self.shutdown_complete or not self.accepting_tasks:
                raise RuntimeError("Clipboard persistence is shut down")
            self.stop_requested = False
            self.worker_exited = threading.Event()
            worker = threading.Thread(
                target=self._loop,
                name="ClipSavePersistence",
                daemon=True,
            )
            self.worker = worker
            worker.start()

    def enqueue(self, kind: str, value: QImage | str, sequence: int | None) -> None:
        if not self.accepting_tasks:
            return
        self.ensure_started()
        if sequence is not None:
            if sequence in self.pending_sequences:
                return
        elif self.pending_without_sequence:
            return
        estimated_bytes = (
            max(value.sizeInBytes(), value.width() * value.height() * 4)
            if kind == "image"
            else len(value.encode("utf-8"))
        )
        if self.pending_bytes + estimated_bytes > self.memory_budget_bytes:
            self.report_failure(
                "剪贴板保存队列占用内存过高；如果内容仍在剪贴板中，ClipSave 会稍后重试。"
            )
            return
        task = ClipboardTask(
            self.next_task_token,
            kind,
            value,
            sequence,
            estimated_bytes,
        )
        self.next_task_token += 1
        with self.state_lock:
            self.idle_event.clear()
            if sequence is None:
                self.pending_without_sequence = True
            else:
                self.pending_sequences.add(sequence)
            self.pending_bytes += estimated_bytes
            try:
                self.tasks.put_nowait(task)
            except queue.Full:
                self.pending_bytes = max(0, self.pending_bytes - estimated_bytes)
                if sequence is None:
                    self.pending_without_sequence = False
                else:
                    self.pending_sequences.discard(sequence)
                if self.tasks.unfinished_tasks == 0:
                    self.idle_event.set()
                self.report_failure(
                    "剪贴板保存队列已满；如果内容仍在剪贴板中，ClipSave 会稍后重试。"
                )

    def release_pending(self, task: ClipboardTask) -> None:
        with self.state_lock:
            self.pending_bytes = max(0, self.pending_bytes - task.estimated_bytes)
            if task.sequence is None:
                self.pending_without_sequence = False
            else:
                self.pending_sequences.discard(task.sequence)
            if self.tasks.unfinished_tasks == 0 and self.pending_bytes == 0:
                self.idle_event.set()

    def prepare_for_shutdown(self) -> None:
        self.accepting_tasks = False

    def resume_after_failed_shutdown(self) -> None:
        if self.shutdown_complete:
            return
        with self.lifecycle_lock:
            self.suppress_signals = False
            self.stop_requested = False
            worker = self.worker
            if (
                worker is None
                or self.worker_exited.is_set()
                or not worker.is_alive()
            ):
                self.worker_exited = threading.Event()
                self.worker = threading.Thread(
                    target=self._loop,
                    name="ClipSavePersistence",
                    daemon=True,
                )
                self.worker.start()
        self.accepting_tasks = True

    def shutdown(
        self,
        timeout: float,
        *,
        wait_for_idle: Callable[[float], bool],
    ) -> bool:
        if self.shutdown_complete:
            return True
        worker = self.worker
        if worker is None or not worker.is_alive():
            self.prepare_for_shutdown()
            self.shutdown_complete = True
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        self.prepare_for_shutdown()
        if not wait_for_idle(max(0.0, deadline - time.monotonic())):
            self.suppress_signals = True
            return False
        with self.lifecycle_lock:
            self.stop_requested = True
        while worker.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.suppress_signals = True
                return False
            worker.join(min(remaining, self.wait_interval_seconds))
        self.shutdown_complete = not worker.is_alive()
        return self.shutdown_complete

    def _loop(self) -> None:
        while True:
            try:
                task = self.tasks.get(timeout=self.wait_interval_seconds)
            except queue.Empty:
                with self.lifecycle_lock:
                    if self.stop_requested:
                        self.worker_exited.set()
                        return
                continue
            try:
                result = self.process_task(task)
                if self.suppress_signals:
                    self.on_suppressed_finish(task, result)
                else:
                    self.on_success(task, result)
            except Exception as exc:
                if self.suppress_signals:
                    self.on_suppressed_finish(task, None)
                else:
                    self.on_failure(task, str(exc))
            finally:
                with self.state_lock:
                    self.tasks.task_done()
                    if self.tasks.unfinished_tasks == 0 and self.pending_bytes == 0:
                        self.idle_event.set()
