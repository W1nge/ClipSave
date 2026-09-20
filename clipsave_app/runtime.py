from __future__ import annotations

from dataclasses import dataclass

from .app_paths import AppPaths
from .constants import APP_PATHS
from .database import LibraryDatabase
from .services import ClipboardService, shutdown_ai_ocr_task_executor
from .settings import Settings


@dataclass(slots=True)
class ApplicationRuntime:
    """Composition root and owner for process-lifetime application resources."""

    paths: AppPaths
    database: LibraryDatabase
    settings: Settings
    clipboard_service: ClipboardService
    _core_closed: bool = False

    @classmethod
    def create(cls, paths: AppPaths = APP_PATHS) -> "ApplicationRuntime":
        database = LibraryDatabase(paths=paths)
        try:
            settings = Settings(paths.settings_path)
            clipboard_service = ClipboardService(database, paths=paths)
        except BaseException:
            database.close()
            raise
        return cls(paths, database, settings, clipboard_service)

    def close_core(self, *, executor_timeout: float = 2.0) -> None:
        if self._core_closed:
            return
        shutdown_ai_ocr_task_executor(timeout=max(0.0, executor_timeout))
        self.database.close()
        self._core_closed = True

    def close(self, *, timeout: float = 2.0) -> bool:
        clipboard_stopped = self.clipboard_service.shutdown(timeout=max(0.0, timeout))
        if clipboard_stopped:
            self.close_core(executor_timeout=timeout)
        return clipboard_stopped
