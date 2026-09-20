from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .ai_service import AIService
from .database import LibraryDatabase
from .file_preflight import OperationCancelled, preflight_image_file
from .image_work_coordinator import (
    ImageWorkBusy,
    ImageWorkCoordinator,
    image_task_estimate,
)
from .task_executor import TaskCapacityExceeded, ai_ocr_task_executor
from .task_supervisor import TaskSupervisor


@dataclass(frozen=True, slots=True)
class ImageTaskCleanup:
    pending: bool = False
    ai_item_ids: tuple[int, ...] = ()
    ocr_item_ids: tuple[int, ...] = ()
    expanded_search_finished: bool = False


@dataclass(frozen=True, slots=True)
class ImageOperationCompletion:
    matched: bool
    automatic: bool = False
    saved: bool = False
    stale_content: bool = False
    error: str | None = None


class ImageOperationPreparationState(Enum):
    READY = "ready"
    DATABASE_UNAVAILABLE = "database_unavailable"
    INVALID_ITEM = "invalid_item"
    SERVICE_UNCONFIGURED = "service_unconfigured"
    MISSING_HASH = "missing_hash"
    ALREADY_RUNNING = "already_running"


@dataclass(frozen=True, slots=True)
class ImageOperationPreparation:
    state: ImageOperationPreparationState
    item: object | None = None
    estimated_bytes: int = 0

    @property
    def ready(self) -> bool:
        return self.state is ImageOperationPreparationState.READY


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
        database: LibraryDatabase | None = None,
        start_bounded: Callable[..., object] | None = None,
        work_coordinator: ImageWorkCoordinator | None = None,
    ) -> None:
        super().__init__(parent)
        self.supervisor = supervisor
        self.database = database
        self._start_bounded = start_bounded
        self.work_coordinator = work_coordinator or ImageWorkCoordinator()
        self.ai_requests: dict[int, tuple[object, QObject]] = {}
        self.ocr_requests: dict[int, tuple[object, QObject]] = {}
        self.automatic_ai_items: set[int] = set()
        self.automatic_ocr_items: set[int] = set()
        self.expanded_search_request: tuple[object, QObject] | None = None

    def prepare_image_operation(
        self,
        item_id: int,
        service: AIService,
        *,
        operation: str,
        automatic: bool,
    ) -> ImageOperationPreparation:
        if self.database is None:
            return ImageOperationPreparation(
                ImageOperationPreparationState.DATABASE_UNAVAILABLE
            )
        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            return ImageOperationPreparation(ImageOperationPreparationState.INVALID_ITEM)
        if not service.configured:
            return ImageOperationPreparation(
                ImageOperationPreparationState.SERVICE_UNCONFIGURED,
                item=item,
            )
        if not item["content_hash"]:
            return ImageOperationPreparation(
                ImageOperationPreparationState.MISSING_HASH,
                item=item,
            )

        requests = self.ai_requests if operation == "ai" else self.ocr_requests
        if item_id in requests or self.work_coordinator.is_active(item_id, operation):
            return ImageOperationPreparation(
                ImageOperationPreparationState.ALREADY_RUNNING,
                item=item,
            )

        return ImageOperationPreparation(
            ImageOperationPreparationState.READY,
            item=item,
            estimated_bytes=image_task_estimate(item),
        )

    def automatic_operations_for_item(
        self,
        item_id: int,
        service: AIService,
        *,
        auto_ocr: bool,
        auto_description: bool,
    ) -> tuple[str, ...]:
        if self.database is None or not service.configured:
            return ()
        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            return ()
        operations: list[str] = []
        if (
            auto_ocr
            and not str(item["ocr_text"] or "").strip()
            and item_id not in self.ocr_requests
            and not self.work_coordinator.is_active(item_id, "ocr")
        ):
            operations.append("ocr")
        if (
            auto_description
            and not str(item["ai_description"] or "").strip()
            and item_id not in self.ai_requests
            and not self.work_coordinator.is_active(item_id, "ai")
        ):
            operations.append("ai")
        return tuple(operations)

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
            claimed = False
            handed_off = False
            try:
                if not self.work_coordinator.claim(item_id, operation, token):
                    raise ImageWorkBusy("相同图片任务正在运行，请稍后重试。")
                claimed = True
                image_snapshot = preflight_image_file(Path(item["path"]))
                processor = service.describe_image if is_ai else service.ocr_image
                try:
                    external = bool(item["external"])
                except (KeyError, IndexError, TypeError):
                    external = False
                processor_kwargs = {"expected_sha256": expected_hash}
                if external:
                    processor_kwargs["source_root"] = Path(item["path"]).parent
                result_text = processor(image_snapshot, cancel_event, **processor_kwargs)
                image_snapshot.require_current()
                if cancel_event.is_set():
                    return
                signal = self.ai_succeeded if is_ai else self.ocr_succeeded
                signal.emit(token, marker, item_id, result_text, expected_hash)
                handed_off = True
            except OperationCancelled:
                return
            except Exception as exc:
                if not cancel_event.is_set():
                    signal = self.ai_failed if is_ai else self.ocr_failed
                    signal.emit(token, marker, item_id, str(exc))
                    handed_off = True
            finally:
                if claimed and not handed_off:
                    self.work_coordinator.release(item_id, operation, token)

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
        self.work_coordinator.release(item_id, operation, token)
        is_ai = operation == "ai"
        requests = self.ai_requests if is_ai else self.ocr_requests
        automatic_items = self.automatic_ai_items if is_ai else self.automatic_ocr_items
        if requests.get(item_id) != (token, marker):
            return False, False
        requests.pop(item_id, None)
        automatic = item_id in automatic_items
        automatic_items.discard(item_id)
        return True, automatic

    def commit_operation(
        self,
        item_id: int,
        operation: str,
        token: object,
        marker: QObject,
        expected_content_hash: str,
        result_text: str,
    ) -> ImageOperationCompletion:
        if not self.is_current_operation(item_id, operation, token, marker):
            self.supervisor.finish_bounded(token)
            self.work_coordinator.release(item_id, operation, token)
            return ImageOperationCompletion(matched=False)
        if self.database is None:
            matched, automatic = self.finish_operation(
                item_id,
                operation,
                token,
                marker,
            )
            return ImageOperationCompletion(
                matched=matched,
                automatic=automatic,
                error="Image task database is unavailable",
            )

        update_result = (
            self.database.update_ai_if_current
            if operation == "ai"
            else self.database.update_ocr_if_current
        )
        try:
            saved = update_result(
                item_id,
                expected_content_hash,
                result_text,
            )
        except Exception as exc:
            matched, automatic = self.finish_operation(
                item_id,
                operation,
                token,
                marker,
            )
            return ImageOperationCompletion(
                matched=matched,
                automatic=automatic,
                error=str(exc),
            )

        matched, automatic = self.finish_operation(
            item_id,
            operation,
            token,
            marker,
        )
        return ImageOperationCompletion(
            matched=matched,
            automatic=automatic,
            saved=bool(saved),
            stale_content=not bool(saved),
        )

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
                operation = "ai" if requests is self.ai_requests else "ocr"
                self.work_coordinator.release(item_id, operation, token)
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
        for operation, requests in (
            ("ai", self.ai_requests),
            ("ocr", self.ocr_requests),
        ):
            for item_id, (token, _marker) in requests.items():
                self.work_coordinator.release(item_id, operation, token)
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

