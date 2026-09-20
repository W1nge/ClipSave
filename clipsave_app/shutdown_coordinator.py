from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from collections.abc import Callable

from .clipboard_service import ClipboardService
from .database import LibraryDatabase
from .runtime import ApplicationRuntime
from .task_executor import shutdown_ai_ocr_task_executor


class ShutdownFailure(Enum):
    THUMBNAILS = "thumbnails"
    CLIPBOARD_IDLE = "clipboard_idle"
    BACKUP = "backup"
    CLIPBOARD_WORKER = "clipboard_worker"


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    failure: ShutdownFailure | None = None
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.failure is None


class ShutdownCoordinator:
    """Own resource ordering/recovery while MainWindow keeps UI policy."""

    def __init__(
        self,
        database: LibraryDatabase,
        clipboard_service: ClipboardService,
        grid,
        detail,
        *,
        runtime: ApplicationRuntime | None = None,
    ) -> None:
        self.database = database
        self.clipboard_service = clipboard_service
        self.grid = grid
        self.detail = detail
        self.runtime = runtime

    def shutdown_interactive_resources(self, *, clipboard_timeout: float = 10.0) -> ShutdownResult:
        if not (self.grid.wait_for_thumbnail_idle() and self.detail.wait_for_thumbnail_idle()):
            self.resume_thumbnails()
            return ShutdownResult(ShutdownFailure.THUMBNAILS)

        monitoring_was_active = self.clipboard_service.timer.isActive()
        self.clipboard_service.stop()
        if not self.clipboard_service.wait_for_idle(clipboard_timeout):
            self._recover_interactive_resources(monitoring_was_active)
            return ShutdownResult(ShutdownFailure.CLIPBOARD_IDLE)

        try:
            self.database.create_backup()
        except Exception as exc:
            self.database.record_backup_error(str(exc))
            self._recover_interactive_resources(monitoring_was_active)
            return ShutdownResult(ShutdownFailure.BACKUP, str(exc))

        if not self.clipboard_service.shutdown(timeout=clipboard_timeout):
            self._recover_interactive_resources(monitoring_was_active)
            return ShutdownResult(ShutdownFailure.CLIPBOARD_WORKER)
        return ShutdownResult()

    def prepare_session_end(self) -> bool:
        monitoring_was_active = self.clipboard_service.timer.isActive()
        self.clipboard_service.prepare_for_shutdown()
        return monitoring_was_active

    def persist_session_notes(
        self,
        timeout: float,
        *,
        start_task: Callable[[object, Callable[[threading.Event], None]], object],
        cancel_task: Callable[[object], object],
        schedule_later: Callable[[int, Callable[[], None]], object],
        is_closing: Callable[[], bool],
    ) -> bool:
        note_updates = self.detail.pending_note_updates()
        if self.detail.current_item is not None:
            item_id = self.detail.current_item["id"]
            notes = self.detail.notes.toPlainText()
            if notes != self.detail.loaded_notes:
                note_updates[item_id] = (self.detail.loaded_notes, notes)
        if not note_updates:
            return True

        note_result: list[Exception | None] = []
        note_saved_ids: list[int] = []
        reconciled_note_ids: set[int] = set()
        note_state_lock = threading.Lock()
        note_done = threading.Event()
        note_token = object()

        def persist_notes(cancel_event: threading.Event) -> None:
            error = None
            try:
                for item_id, (expected_notes, notes) in note_updates.items():
                    if cancel_event.is_set():
                        return
                    if not self.database.set_notes_if_unchanged(
                        item_id,
                        expected_notes,
                        notes,
                    ):
                        raise RuntimeError("notes changed during session shutdown")
                    with note_state_lock:
                        note_saved_ids.append(item_id)
            except Exception as exc:
                error = exc
            finally:
                note_result.append(error)
                note_done.set()

        def reconcile_saved_notes() -> None:
            with note_state_lock:
                pending_ids = [
                    item_id
                    for item_id in note_saved_ids
                    if item_id not in reconciled_note_ids
                ]
                reconciled_note_ids.update(pending_ids)
            for item_id in pending_ids:
                _expected_notes, notes = note_updates[item_id]
                self.detail.mark_notes_saved(item_id, notes)

        def reconcile_late_note_saves() -> None:
            if is_closing():
                return
            if not note_done.is_set():
                schedule_later(25, reconcile_late_note_saves)
                return
            reconcile_saved_notes()

        start_task(note_token, persist_notes)
        notes_finished = note_done.wait(max(0.0, timeout))
        reconcile_saved_notes()
        if not notes_finished:
            cancel_task(note_token)
            schedule_later(0, reconcile_late_note_saves)
        return notes_finished and note_result == [None]

    def finish_session_end(self, timeout: float) -> bool:
        remaining = max(0.0, timeout)
        if not self.clipboard_service.wait_for_idle(remaining):
            return False
        return self.clipboard_service.shutdown(timeout=remaining)

    def abort_session_end(self, monitoring_was_active: bool) -> None:
        self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)

    def resume_thumbnails(self) -> None:
        self.grid.resume_thumbnail_loader()
        self.detail.resume_thumbnail_loader()

    def stop_compute_executor(self, timeout: float) -> bool:
        return shutdown_ai_ocr_task_executor(timeout=max(0.0, timeout))

    def finalize_core(
        self,
        *,
        executor_timeout: float,
        thumbnail_timeout_ms: int | None = None,
    ) -> bool:
        if not self.stop_compute_executor(executor_timeout):
            return False
        if thumbnail_timeout_ms is None:
            self.grid.shutdown_thumbnail_loader()
            self.detail.shutdown_thumbnail_loader()
        else:
            timeout_ms = max(0, int(thumbnail_timeout_ms))
            self.grid.shutdown_thumbnail_loader(timeout_ms=timeout_ms)
            self.detail.shutdown_thumbnail_loader(timeout_ms=timeout_ms)
        if self.runtime is None:
            self.database.close()
            return True
        else:
            return self.runtime.close_core(executor_timeout=0.0)

    def _recover_interactive_resources(self, monitoring_was_active: bool) -> None:
        self.resume_thumbnails()
        self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)
