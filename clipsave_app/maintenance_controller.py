from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from .database import LibraryDatabase
from .task_supervisor import TaskSupervisor


class LibraryMaintenanceController(QObject):
    scan_succeeded = Signal(object, object, int)
    scan_failed = Signal(object, object, str)
    backup_succeeded = Signal(object, object, str)
    backup_failed = Signal(object, object, str)

    def __init__(
        self,
        database: LibraryDatabase,
        supervisor: TaskSupervisor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.supervisor = supervisor
        self.startup_request: tuple[object, QObject] | None = None
        self.backup_request: tuple[object, QObject] | None = None

    def start_scan(self, full_scan: bool, reconcile_images: bool) -> tuple[object, QObject]:
        token = object()
        marker = QObject(self)
        request = (token, marker)
        self.startup_request = request

        def work(cancel_event: threading.Event) -> None:
            try:
                self.database.mark_missing_files(cancel_event)
                imported = 0
                if not cancel_event.is_set() and full_scan:
                    imported = self.database.scan_legacy_files(cancel_event)
                elif not cancel_event.is_set() and reconcile_images:
                    imported = self.database.scan_unindexed_files(cancel_event)
                if not cancel_event.is_set():
                    self.scan_succeeded.emit(token, marker, imported)
            except Exception as exc:
                if not cancel_event.is_set():
                    self.scan_failed.emit(token, marker, str(exc))

        self.supervisor.start_thread(token, work)
        return request

    def start_backup_if_dirty(self) -> tuple[object, QObject] | None:
        if self.backup_request is not None:
            return None
        if not self.database.backup_state()["dirty"]:
            return None
        return self.start_backup()

    def start_backup(self) -> tuple[object, QObject]:
        token = object()
        marker = QObject(self)
        request = (token, marker)
        self.backup_request = request

        def work(_cancel_event: threading.Event) -> None:
            try:
                path = self.database.create_backup_if_changed()
                self.backup_succeeded.emit(token, marker, str(path) if path else "")
            except Exception as exc:
                self.backup_failed.emit(token, marker, str(exc))

        self.supervisor.start_thread(token, work)
        return request

    def finish_scan(self, token: object, marker: QObject) -> bool:
        if self.startup_request != (token, marker):
            return False
        self.startup_request = None
        return True

    def finish_backup(self, token: object, marker: QObject) -> bool:
        if self.backup_request != (token, marker):
            return False
        self.backup_request = None
        return True

