from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer


class SmokeLifecycle:
    """Own the executable smoke-ready/status/quit protocol around the Qt loop."""

    MAX_ATTEMPTS = 80
    RETRY_MS = 250

    def __init__(
        self,
        app,
        window,
        database,
        ready_path: Path,
        hold_ms: int,
        *,
        failure: Callable[[object, list[str]], str | None],
        backdrop_status: Callable[[object], str],
        background_idle: Callable[[object], bool],
    ) -> None:
        self.app = app
        self.window = window
        self.database = database
        self.ready_path = Path(ready_path)
        self.status_path = self.ready_path.with_name(f"{self.ready_path.name}.status")
        self.hold_ms = max(0, int(hold_ms))
        self.failure = failure
        self.backdrop_status = backdrop_status
        self.background_idle = background_idle
        self.original_excepthook = sys.excepthook
        self.uncaught_exceptions: list[str] = []
        self.ready_attempts = 0
        self.quit_attempts = 0

    def install(self) -> None:
        sys.excepthook = self._exception_hook

    def start(self) -> None:
        QTimer.singleShot(self.RETRY_MS, self._mark_ready)

    def finalize(self, exit_code: int) -> int:
        failure = self.failure(self.window, self.uncaught_exceptions)
        if failure:
            exit_code = exit_code or 1
        try:
            with self.status_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"event_loop_exited={exit_code}\n")
        except OSError:
            pass
        sys.excepthook = self.original_excepthook
        return exit_code

    def _exception_hook(self, exception_type, exception, traceback) -> None:
        self.uncaught_exceptions.append(
            f"{exception_type.__name__}: {exception}"
        )
        self.original_excepthook(exception_type, exception, traceback)

    def _quit(self) -> None:
        self.quit_attempts += 1
        quit_started = self.window.quit_application()
        try:
            self.status_path.write_text(
                self.backdrop_status(self.window)
                + f"quit_returned={quit_started}\nclosing={self.window._closing}\n"
                f"quit_in_progress={self.window._quit_in_progress}\n"
                f"quit_attempts={self.quit_attempts}\n",
                encoding="utf-8",
                newline="\n",
            )
        except OSError:
            pass
        if quit_started:
            return
        if self.quit_attempts < self.MAX_ATTEMPTS:
            QTimer.singleShot(self.RETRY_MS, self._quit)
            return
        try:
            with self.status_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("smoke_quit_timeout=True\n")
        except OSError:
            pass
        self.app.exit(1)

    def _mark_ready(self) -> None:
        self.ready_attempts += 1
        failure = self.failure(self.window, self.uncaught_exceptions)
        if failure:
            try:
                self.status_path.write_text(
                    failure + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            except OSError:
                pass
            self.app.exit(1)
            return
        try:
            check = self.database.connection.execute("PRAGMA quick_check").fetchone()[0]
            if (
                self.window.isVisible()
                and self.background_idle(self.window)
                and str(check).lower() == "ok"
            ):
                self.ready_path.parent.mkdir(parents=True, exist_ok=True)
                self.status_path.write_text(
                    self.backdrop_status(self.window) + "smoke_ready=True\n",
                    encoding="utf-8",
                    newline="\n",
                )
                temporary = self.ready_path.with_name(f".{self.ready_path.name}.tmp")
                temporary.write_text("ready\n", encoding="ascii", newline="\n")
                os.replace(temporary, self.ready_path)
                QTimer.singleShot(self.hold_ms, self._quit)
                return
        except (OSError, sqlite3.Error):
            pass
        if self.ready_attempts < self.MAX_ATTEMPTS:
            QTimer.singleShot(self.RETRY_MS, self._mark_ready)
            return
        try:
            self.status_path.write_text(
                self.backdrop_status(self.window)
                + f"smoke_ready_timeout=True\nattempts={self.ready_attempts}\n",
                encoding="utf-8",
                newline="\n",
            )
        except OSError:
            pass
        self.app.exit(1)
