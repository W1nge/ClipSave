from __future__ import annotations

import threading
from collections.abc import Callable

from .file_preflight import OperationCancelled
from .task_executor import TaskCapacityExceeded, ai_ocr_task_executor


class ImageWorkBusy(RuntimeError):
    pass


def image_task_estimate(item) -> int:
    try:
        width = max(0, int(item["width"] or 0))
        height = max(0, int(item["height"] or 0))
        return width * height * 4
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


class ImageWorkCoordinator:
    """Serialize identical image operations and share the global AI/OCR budget."""

    WAIT_SLICE_SECONDS = 0.05

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[tuple[int, str], object] = {}

    def is_active(self, item_id: int, operation: str) -> bool:
        with self._lock:
            return (int(item_id), str(operation)) in self._owners

    def claim(self, item_id: int, operation: str, owner: object) -> bool:
        key = (int(item_id), str(operation))
        with self._lock:
            if key in self._owners:
                return False
            self._owners[key] = owner
            return True

    def release(self, item_id: int, operation: str, owner: object) -> None:
        key = (int(item_id), str(operation))
        with self._lock:
            if self._owners.get(key) is owner:
                self._owners.pop(key, None)

    def run_bounded(
        self,
        item_id: int,
        operation: str,
        target: Callable[[threading.Event], object],
        *,
        estimated_bytes: int,
        cancel_event: threading.Event,
    ) -> object:
        """Run one bulk-stage operation through the shared bounded executor.

        Capacity and identical-operation contention are treated as transient for
        bulk work: wait in the outer bulk thread without reserving executor
        memory, and remain cancellable throughout.
        """

        while True:
            if cancel_event.is_set():
                raise OperationCancelled("Operation cancelled")
            if self.is_active(item_id, operation):
                if cancel_event.wait(self.WAIT_SLICE_SECONDS):
                    raise OperationCancelled("Operation cancelled")
                continue

            owner = object()
            result: list[object] = []

            def bounded_target(task_cancel_event: threading.Event) -> None:
                if task_cancel_event.is_set():
                    raise OperationCancelled("Operation cancelled")
                if not self.claim(item_id, operation, owner):
                    raise ImageWorkBusy("相同图片任务正在运行，请稍后重试。")
                try:
                    result.append(target(task_cancel_event))
                finally:
                    self.release(item_id, operation, owner)

            try:
                handle = ai_ocr_task_executor().submit(
                    bounded_target,
                    estimated_bytes=max(0, int(estimated_bytes)),
                    cancel_event=cancel_event,
                )
            except TaskCapacityExceeded:
                if cancel_event.wait(self.WAIT_SLICE_SECONDS):
                    raise OperationCancelled("Operation cancelled")
                continue

            while not handle.wait(self.WAIT_SLICE_SECONDS):
                if cancel_event.is_set():
                    handle.cancel()

            if cancel_event.is_set() or handle.cancelled:
                raise OperationCancelled("Operation cancelled")
            if isinstance(handle.exception, ImageWorkBusy):
                if cancel_event.wait(self.WAIT_SLICE_SECONDS):
                    raise OperationCancelled("Operation cancelled")
                continue
            if handle.exception is not None:
                raise handle.exception
            if not result:
                raise RuntimeError("Image task completed without a result")
            return result[0]
