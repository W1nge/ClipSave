from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ImageTaskCleanup:
    pending: bool = False
    ai_item_ids: tuple[int, ...] = ()
    ocr_item_ids: tuple[int, ...] = ()
    expanded_search_finished: bool = False


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

    def cancel_operation(self, item_id: int, operation: str) -> bool:
        is_ai = operation == "ai"
        requests = self.ai_requests if is_ai else self.ocr_requests
        automatic_items = self.automatic_ai_items if is_ai else self.automatic_ocr_items
        automatic_items.discard(item_id)
        request = requests.pop(item_id, None)
        if request is None:
            return False
        self.supervisor.cancel(request[0])
        return True

    def is_current_operation(
        self,
        item_id: int,
        operation: str,
        token: object,
        marker: QObject,
    ) -> bool:
        requests = self.ai_requests if operation == "ai" else self.ocr_requests
        return requests.get(item_id) == (token, marker)

    def finish_operation(
        self,
        item_id: int,
        operation: str,
        token: object,
        marker: QObject,
    ) -> tuple[bool, bool]:
        self.supervisor.finish_bounded(token)
        is_ai = operation == "ai"
        requests = self.ai_requests if is_ai else self.ocr_requests
        automatic_items = self.automatic_ai_items if is_ai else self.automatic_ocr_items
        if requests.get(item_id) != (token, marker):
            return False, False
        requests.pop(item_id, None)
        automatic = item_id in automatic_items
        automatic_items.discard(item_id)
        return True, automatic

    def finish_expanded_search(self, token: object, marker: QObject) -> bool:
        self.supervisor.finish_bounded(token)
        if self.expanded_search_request != (token, marker):
            return False
        self.expanded_search_request = None
        return True

    def cancel_automatic(self, operation: str) -> tuple[int, ...]:
        is_ai = operation == "ai"
        automatic_items = self.automatic_ai_items if is_ai else self.automatic_ocr_items
        item_ids = tuple(automatic_items)
        for item_id in item_ids:
            self.cancel_operation(item_id, operation)
        return item_ids

    def cancel_item(self, item_id: int) -> None:
        self.cancel_operation(item_id, "ai")
        self.cancel_operation(item_id, "ocr")

    def cancel_for_shutdown(self) -> set[object]:
        tokens: set[object] = set()
        if self.expanded_search_request is not None:
            token, _marker = self.expanded_search_request
            self.supervisor.cancel(token)
            tokens.add(token)
        for requests in (self.ai_requests, self.ocr_requests):
            for token, _marker in requests.values():
                self.supervisor.cancel(token)
                tokens.add(token)
        return tokens

    def cleanup_cancelled(self, cancelled_tokens: set[object]) -> ImageTaskCleanup:
        pending = False
        completed_ai: list[int] = []
        completed_ocr: list[int] = []

        for requests, automatic_items, completed in (
            (self.ai_requests, self.automatic_ai_items, completed_ai),
            (self.ocr_requests, self.automatic_ocr_items, completed_ocr),
        ):
            for item_id, request in list(requests.items()):
                token, _marker = request
                if token not in cancelled_tokens:
                    continue
                if not self.supervisor.token_done(token):
                    pending = True
                    continue
                requests.pop(item_id, None)
                automatic_items.discard(item_id)
                self.supervisor.finish_bounded(token)
                completed.append(item_id)

        expanded_finished = False
        if self.expanded_search_request is not None:
            token, _marker = self.expanded_search_request
            if token in cancelled_tokens:
                if self.supervisor.token_done(token):
                    self.expanded_search_request = None
                    self.supervisor.finish_bounded(token)
                    expanded_finished = True
                else:
                    pending = True

        return ImageTaskCleanup(
            pending=pending,
            ai_item_ids=tuple(completed_ai),
            ocr_item_ids=tuple(completed_ocr),
            expanded_search_finished=expanded_finished,
        )

    def clear_state(self) -> None:
        self.ai_requests.clear()
        self.ocr_requests.clear()
        self.automatic_ai_items.clear()
        self.automatic_ocr_items.clear()
        self.expanded_search_request = None

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

