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

        def work(cancel_event: threading.Event) -> None:
            try:
                payload = self.snapshot(query, page_size)
                if not cancel_event.is_set():
                    self.refresh_succeeded.emit(request.token, query, payload)
            except Exception as exc:
                if not cancel_event.is_set():
                    self.refresh_failed.emit(request.token, str(exc))

        self.supervisor.start_thread(request.token, work)

    def search(self, query: LibraryQuery, page_size: int) -> None:
        self.cancel_search()
        self.cancel_page()
        request = LibraryRequest(object(), query)
        self.search_request = request

        def work(cancel_event: threading.Event) -> None:
            try:
                items = self.query_items(query, page_size, 0)
                if not cancel_event.is_set():
                    self.search_succeeded.emit(request.token, query, items)
            except Exception as exc:
                if not cancel_event.is_set():
                    self.search_failed.emit(request.token, str(exc))

        self.supervisor.start_thread(request.token, work)

    def load_page(self, query: LibraryQuery, offset: int, page_size: int) -> None:
        self.cancel_page()
        request = LibraryRequest(object(), query, offset)
        self.page_request = request

        def work(cancel_event: threading.Event) -> None:
            try:
                items = self.query_items(query, page_size, offset)
                if not cancel_event.is_set():
                    self.page_succeeded.emit(request.token, query, offset, items)
            except Exception as exc:
                if not cancel_event.is_set():
                    self.page_failed.emit(request.token, str(exc))

        self.supervisor.start_thread(request.token, work)

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
        self.supervisor.cancel(request.token)
