from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable


class TaskCapacityExceeded(RuntimeError):
    pass


@dataclass
class TaskHandle:
    cancel_event: threading.Event
    done_event: threading.Event
    exception: BaseException | None = None

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done_event.wait(timeout)


@dataclass(frozen=True)
class _BoundedTask:
    target: Callable[[threading.Event], None]
    handle: TaskHandle
    reserved_bytes: int


class BoundedTaskExecutor:
    def __init__(
        self,
        *,
        max_active: int = 2,
        max_queued: int = 4,
        memory_budget_bytes: int = 512 * 1024 * 1024,
    ):
        if max_active < 1 or max_queued < 0 or memory_budget_bytes < 1:
            raise ValueError("Invalid bounded executor limits")
        self.max_active = max_active
        self.max_queued = max_queued
        self.memory_budget_bytes = memory_budget_bytes
        self._queue: queue.Queue[_BoundedTask | None] = queue.Queue(
            max_queued + max_active
        )
        self._lock = threading.Lock()
        self._reserved_bytes = 0
        self._pending_count = 0
        self._accepting = True
        self._shutdown_sentinels_enqueued = 0
        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"ClipSaveAIOrOCR-{index + 1}",
                daemon=True,
            )
            for index in range(max_active)
        ]
        for worker in self._workers:
            worker.start()

    def submit(
        self,
        target: Callable[[threading.Event], None],
        *,
        estimated_bytes: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> TaskHandle:
        if estimated_bytes < 0:
            raise ValueError("estimated_bytes must not be negative")
        handle = TaskHandle(cancel_event or threading.Event(), threading.Event())
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Task executor is shut down")
            if self._pending_count >= self.max_queued + self.max_active:
                raise TaskCapacityExceeded("AI/OCR 任务队列已满，请稍后重试。")
            if self._reserved_bytes + estimated_bytes > self.memory_budget_bytes:
                raise TaskCapacityExceeded("AI/OCR 任务占用内存过高，请稍后重试。")
            self._reserved_bytes += estimated_bytes
            self._pending_count += 1
            try:
                self._queue.put_nowait(
                    _BoundedTask(target, handle, estimated_bytes)
                )
            except queue.Full:
                self._reserved_bytes -= estimated_bytes
                self._pending_count -= 1
                raise TaskCapacityExceeded("AI/OCR 任务队列已满，请稍后重试。")
        return handle

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            try:
                if not task.handle.cancelled:
                    task.target(task.handle.cancel_event)
            except BaseException as exc:
                task.handle.exception = exc
            finally:
                with self._lock:
                    self._reserved_bytes = max(
                        0,
                        self._reserved_bytes - task.reserved_bytes,
                    )
                    self._pending_count -= 1
                task.handle.done_event.set()
                self._queue.task_done()

    def shutdown(
        self,
        *,
        cancel_pending: bool = True,
        timeout: float = 2.0,
    ) -> bool:
        with self._lock:
            if not self._accepting and all(
                not worker.is_alive() for worker in self._workers
            ):
                return True
            self._accepting = False
        if cancel_pending:
            with self._queue.mutex:
                for task in self._queue.queue:
                    if task is not None:
                        task.handle.cancel()
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            sentinels_needed = (
                len(self._workers) - self._shutdown_sentinels_enqueued
            )
        for _worker in range(sentinels_needed):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                self._queue.put(None, timeout=remaining)
            except queue.Full:
                return False
            with self._lock:
                self._shutdown_sentinels_enqueued += 1
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            worker.join(remaining)
        return all(not worker.is_alive() for worker in self._workers)


_AI_OCR_EXECUTOR: BoundedTaskExecutor | None = None
_AI_OCR_EXECUTOR_LOCK = threading.Lock()


def ai_ocr_task_executor() -> BoundedTaskExecutor:
    global _AI_OCR_EXECUTOR
    with _AI_OCR_EXECUTOR_LOCK:
        if _AI_OCR_EXECUTOR is None:
            _AI_OCR_EXECUTOR = BoundedTaskExecutor()
        return _AI_OCR_EXECUTOR


def shutdown_ai_ocr_task_executor(timeout: float = 2.0) -> bool:
    global _AI_OCR_EXECUTOR
    with _AI_OCR_EXECUTOR_LOCK:
        executor = _AI_OCR_EXECUTOR
    if executor is None:
        return True
    stopped = executor.shutdown(timeout=timeout)
    if stopped:
        with _AI_OCR_EXECUTOR_LOCK:
            if _AI_OCR_EXECUTOR is executor:
                _AI_OCR_EXECUTOR = None
    return stopped
