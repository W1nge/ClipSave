from __future__ import annotations

import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from .database import LibraryDatabase
from .library_models import LibraryQuery
from .task_supervisor import TaskSupervisor


@dataclass(frozen=True, slots=True)
class LibraryRequest:
    token: object
    query: LibraryQuery
    offset: int = 0


@dataclass(frozen=True, slots=True)
class LibraryNavigationSnapshot:
    counts: object
    collections: object
    tags: object
    days: object


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    navigation: LibraryNavigationSnapshot
    items: object


@dataclass(frozen=True, slots=True)
class _LibraryWork:
    kind: str
    request: LibraryRequest
    page_size: int


class LibraryController(QObject):
    refresh_succeeded = Signal(object, object, object)
    refresh_failed = Signal(object, str)
    search_succeeded = Signal(object, object, object)
    search_failed = Signal(object, str)
    page_succeeded = Signal(object, object, int, object)
    page_failed = Signal(object, str)

    def __init__(
        self,
        database: LibraryDatabase,
        supervisor: TaskSupervisor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.supervisor = supervisor
        self.refresh_request: LibraryRequest | None = None
        self.search_request: LibraryRequest | None = None
        self.page_request: LibraryRequest | None = None
        self._work_lock = threading.Lock()
        self._pending_work: dict[str, _LibraryWork] = {}
        self._worker_token: object | None = None

    def query_items(self, query: LibraryQuery, limit: int, offset: int):
        return self.database.query_items(
            **query.as_database_kwargs(),
            summary_only=True,
            limit=limit,
            offset=offset,
        )

    def navigation_snapshot(self) -> LibraryNavigationSnapshot:
        return LibraryNavigationSnapshot(
            counts=self.database.counts(),
            collections=self.database.collections(),
            tags=self.database.tags(),
            days=self.database.days(),
        )

    def snapshot(self, query: LibraryQuery, page_size: int) -> LibrarySnapshot:
        return LibrarySnapshot(
            navigation=self.navigation_snapshot(),
            items=self.query_items(query, page_size, 0),
        )

    def refresh(self, query: LibraryQuery, page_size: int) -> None:
        self.cancel_refresh()
        self.cancel_search()
        self.cancel_page()
        request = LibraryRequest(object(), query)
        self.refresh_request = request
        self._schedule_work(_LibraryWork("refresh", request, page_size))

    def search(self, query: LibraryQuery, page_size: int) -> None:
        self.cancel_search()
        self.cancel_page()
        request = LibraryRequest(object(), query)
        self.search_request = request
        self._schedule_work(_LibraryWork("search", request, page_size))

    def load_page(self, query: LibraryQuery, offset: int, page_size: int) -> None:
        self.cancel_page()
        request = LibraryRequest(object(), query, offset)
        self.page_request = request
        self._schedule_work(_LibraryWork("page", request, page_size))

    def finish_refresh(self, token: object) -> bool:
        return self._finish("refresh_request", token)

    def finish_search(self, token: object) -> bool:
        return self._finish("search_request", token)

    def finish_page(self, token: object) -> bool:
        return self._finish("page_request", token)

    def _finish(self, attribute: str, token: object) -> bool:
        request = getattr(self, attribute)
        if request is None or request.token is not token:
            return False
        setattr(self, attribute, None)
        return True

    def cancel_refresh(self) -> None:
        self._cancel("refresh_request")

    def cancel_search(self) -> None:
        self._cancel("search_request")

    def cancel_page(self) -> None:
        self._cancel("page_request")

    def _cancel(self, attribute: str) -> None:
        request = getattr(self, attribute)
        if request is None:
            return
        setattr(self, attribute, None)
        kind = attribute.removesuffix("_request")
        with self._work_lock:
            pending = self._pending_work.get(kind)
            if pending is not None and pending.request is request:
                self._pending_work.pop(kind, None)

    def _schedule_work(self, work: _LibraryWork) -> None:
        worker_token = None
        with self._work_lock:
            self._pending_work.pop(work.kind, None)
            self._pending_work[work.kind] = work
            if self._worker_token is None:
                worker_token = object()
                self._worker_token = worker_token
        if worker_token is None:
            return
        try:
            self.supervisor.start_thread(
                worker_token,
                lambda cancel_event: self._drain_work(worker_token, cancel_event),
                name="ClipSaveLibraryQuery",
            )
        except Exception as exc:
            with self._work_lock:
                if self._worker_token is worker_token:
                    self._worker_token = None
                failed = list(self._pending_work.values())
                self._pending_work.clear()
            for pending in failed:
                self._emit_failure(pending, str(exc))
                attribute = f"{pending.kind}_request"
                if getattr(self, attribute) is pending.request:
                    setattr(self, attribute, None)

    def _drain_work(
        self,
        worker_token: object,
        cancel_event: threading.Event,
    ) -> None:
        try:
            while not cancel_event.is_set():
                with self._work_lock:
                    if not self._pending_work:
                        if self._worker_token is worker_token:
                            self._worker_token = None
                        return
                    _kind, work = next(iter(self._pending_work.items()))
                    self._pending_work.pop(work.kind, None)
                self._execute_work(work, cancel_event)
        finally:
            with self._work_lock:
                if self._worker_token is worker_token:
                    self._worker_token = None

    def _execute_work(
        self,
        work: _LibraryWork,
        cancel_event: threading.Event,
    ) -> None:
        try:
            if work.kind == "refresh":
                payload = self.snapshot(work.request.query, work.page_size)
                if not cancel_event.is_set() and self._is_current(work):
                    self.refresh_succeeded.emit(
                        work.request.token,
                        work.request.query,
                        payload,
                    )
                return
            items = self.query_items(
                work.request.query,
                work.page_size,
                work.request.offset,
            )
            if cancel_event.is_set() or not self._is_current(work):
                return
            if work.kind == "search":
                self.search_succeeded.emit(
                    work.request.token,
                    work.request.query,
                    items,
                )
            else:
                self.page_succeeded.emit(
                    work.request.token,
                    work.request.query,
                    work.request.offset,
                    items,
                )
        except Exception as exc:
            if not cancel_event.is_set() and self._is_current(work):
                self._emit_failure(work, str(exc))

    def _is_current(self, work: _LibraryWork) -> bool:
        return getattr(self, f"{work.kind}_request") is work.request

    def _emit_failure(self, work: _LibraryWork, message: str) -> None:
        if not self._is_current(work):
            return
        if work.kind == "refresh":
            self.refresh_failed.emit(work.request.token, message)
        elif work.kind == "search":
            self.search_failed.emit(work.request.token, message)
        else:
            self.page_failed.emit(work.request.token, message)
