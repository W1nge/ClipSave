from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .database import LibraryDatabase
from .runtime import ApplicationRuntime
from .services import ClipboardService, shutdown_ai_ocr_task_executor


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

    def finalize_core(
        self,
        *,
        executor_timeout: float,
        thumbnail_timeout_ms: int | None = None,
    ) -> None:
        if thumbnail_timeout_ms is None:
            self.grid.shutdown_thumbnail_loader()
            self.detail.shutdown_thumbnail_loader()
        else:
            timeout_ms = max(0, int(thumbnail_timeout_ms))
            self.grid.shutdown_thumbnail_loader(timeout_ms=timeout_ms)
            self.detail.shutdown_thumbnail_loader(timeout_ms=timeout_ms)
        if self.runtime is None:
            shutdown_ai_ocr_task_executor(timeout=max(0.0, executor_timeout))
            self.database.close()
        else:
            self.runtime.close_core(executor_timeout=executor_timeout)

    def _recover_interactive_resources(self, monitoring_was_active: bool) -> None:
        self.resume_thumbnails()
        self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)
