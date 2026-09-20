from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .services import (
    AIService,
    OperationCancelled,
    TaskCapacityExceeded,
    ai_ocr_task_executor,
    preflight_image_file,
)
from .task_supervisor import TaskSupervisor


class ImageTaskController(QObject):
    ai_succeeded = Signal(object, object, int, str, str)
    ai_failed = Signal(object, object, int, str)
    ocr_succeeded = Signal(object, object, int, str, str)
    ocr_failed = Signal(object, object, int, str)
    expanded_search_succeeded = Signal(object, object, str, object)
    expanded_search_failed = Signal(object, object, str)

    def __init__(
        self,
        supervisor: TaskSupervisor,
        parent: QObject | None = None,
        *,
        start_bounded: Callable[..., object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.supervisor = supervisor
        self._start_bounded = start_bounded
        self.ai_requests: dict[int, tuple[object, QObject]] = {}
        self.ocr_requests: dict[int, tuple[object, QObject]] = {}
        self.automatic_ai_items: set[int] = set()
        self.automatic_ocr_items: set[int] = set()
        self.expanded_search_request: tuple[object, QObject] | None = None

    def start_image_operation(
        self,
        item,
        service: AIService,
        *,
        operation: str,
        automatic: bool,
        estimated_bytes: int,
    ) -> tuple[object, QObject]:
        is_ai = operation == "ai"
        requests = self.ai_requests if is_ai else self.ocr_requests
        automatic_items = self.automatic_ai_items if is_ai else self.automatic_ocr_items
        item_id = int(item["id"])
        expected_hash = str(item["content_hash"])
        token = object()
        marker = QObject(self)
        request = (token, marker)
        requests[item_id] = request
        if automatic:
            automatic_items.add(item_id)

        def work(cancel_event: threading.Event) -> None:
            try:
                image_snapshot = preflight_image_file(Path(item["path"]))
                processor = service.describe_image if is_ai else service.ocr_image
                result_text = processor(
                    image_snapshot,
                    cancel_event,
                    expected_sha256=expected_hash,
                )
                image_snapshot.require_current()
                if cancel_event.is_set():
                    return
                signal = self.ai_succeeded if is_ai else self.ocr_succeeded
                signal.emit(token, marker, item_id, result_text, expected_hash)
            except OperationCancelled:
                return
            except Exception as exc:
                if not cancel_event.is_set():
                    signal = self.ai_failed if is_ai else self.ocr_failed
                    signal.emit(token, marker, item_id, str(exc))

        try:
            self._submit_bounded(token, work, estimated_bytes=estimated_bytes)
        except (TaskCapacityExceeded, RuntimeError):
            requests.pop(item_id, None)
            automatic_items.discard(item_id)
            raise
        return request

    def start_expanded_search(
        self,
        query: str,
        service: AIService,
    ) -> tuple[object, QObject]:
        self.cancel_expanded_search()
        token = object()
        marker = QObject(self)
        request = (token, marker)
        self.expanded_search_request = request

        def work(cancel_event: threading.Event) -> None:
            try:
                terms = service.expand_search_query(query, cancel_event)
                if not cancel_event.is_set():
                    self.expanded_search_succeeded.emit(token, marker, query, terms)
            except OperationCancelled:
                return
            except Exception as exc:
                if not cancel_event.is_set():
                    self.expanded_search_failed.emit(token, marker, str(exc))

        try:
            self._submit_bounded(token, work)
        except (TaskCapacityExceeded, RuntimeError):
            self.expanded_search_request = None
            raise
        return request

    def cancel_expanded_search(self) -> None:
        request = self.expanded_search_request
        if request is None:
            return
        self.expanded_search_request = None
        self.supervisor.cancel(request[0])

    def _submit_bounded(
        self,
        token: object,
        target,
        *,
        estimated_bytes: int = 0,
    ) -> object:
        if self._start_bounded is not None:
            return self._start_bounded(
                token,
                target,
                estimated_bytes=estimated_bytes,
            )
        handle = ai_ocr_task_executor().submit(target, estimated_bytes=estimated_bytes)
        self.supervisor.track_bounded(token, handle)
        return handle

