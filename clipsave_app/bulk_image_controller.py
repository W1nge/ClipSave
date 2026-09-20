from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .bulk_checkpoint import (
    BulkImageCheckpoint,
    clear_checkpoint,
    load_checkpoint,
    new_checkpoint,
    save_checkpoint,
)
from .bulk_image_job import BulkImageJob, BulkImageResult
from .database import LibraryDatabase
from .image_work_coordinator import ImageWorkCoordinator


@dataclass(frozen=True, slots=True)
class BulkImageCompletion:
    details: dict[str, object]
    error: str
    completed_all: bool
    checkpoint: BulkImageCheckpoint | None


class BulkImageController(QObject):
    progress_changed = Signal(int, int, int, str)
    finished = Signal(object)

    _worker_progress = Signal(object, int, int, int, str)
    _worker_finished = Signal(object, object)

    def __init__(
        self,
        database: LibraryDatabase,
        checkpoint_path: Path,
        *,
        start_task: Callable[[object, Callable[[threading.Event], None]], object],
        cancel_task: Callable[[object], None] | None = None,
        work_coordinator: ImageWorkCoordinator | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.checkpoint_path = Path(checkpoint_path)
        self._start_task = start_task
        self._cancel_task = cancel_task
        self.work_coordinator = work_coordinator or ImageWorkCoordinator()
        self.request: object | None = None
        self.progress_state = self._initial_progress_state()
        self._worker_progress.connect(self._on_worker_progress)
        self._worker_finished.connect(self._on_worker_finished)

    @staticmethod
    def progress_state_for(
        checkpoint: BulkImageCheckpoint | None,
        *,
        active: bool = False,
        phase: str = "",
        error: str = "",
    ) -> dict[str, object]:
        if checkpoint is None:
            return {
                "active": False,
                "resumable": False,
                "processed": 0,
                "total": 0,
                "phase": phase,
                "error": error,
            }
        return {
            "active": active,
            "resumable": checkpoint.processed < checkpoint.total,
            "processed": checkpoint.processed,
            "total": checkpoint.total,
            "phase": phase,
            "error": error,
        }

    def snapshot(self) -> dict[str, object]:
        return dict(self.progress_state)

    def cancel(self) -> object | None:
        token = self.request
        if token is None:
            return None
        self.request = None
        if self._cancel_task is not None:
            self._cancel_task(token)
        checkpoint = self.load_checkpoint()
        self.progress_state = self.progress_state_for(
            checkpoint,
            phase="已暂停，可继续处理" if checkpoint is not None else "",
        )
        return token

    def load_checkpoint(self) -> BulkImageCheckpoint | None:
        try:
            checkpoint = load_checkpoint(self.checkpoint_path)
        except (OSError, ValueError) as exc:
            self.progress_state = self.progress_state_for(
                None,
                phase="断点不可用",
                error=str(exc),
            )
            return None
        return checkpoint

    def start(self, service) -> BulkImageCheckpoint:
        if self.request is not None:
            raise RuntimeError("当前已有批量图片处理任务正在运行。")

        checkpoint = self.load_checkpoint()
        if checkpoint is not None and checkpoint.processed >= checkpoint.total:
            try:
                clear_checkpoint(self.checkpoint_path)
            except OSError as exc:
                raise RuntimeError(f"无法清理旧断点：{exc}") from exc
            checkpoint = None

        if checkpoint is None:
            image_ids = self.database.item_ids(kind="image", sort="oldest")
            if not image_ids:
                raise LookupError("NO_IMAGES")
            checkpoint = new_checkpoint(image_ids)
            try:
                clear_checkpoint(self.checkpoint_path)
                save_checkpoint(self.checkpoint_path, checkpoint)
            except OSError as exc:
                raise RuntimeError(f"无法保存批处理断点：{exc}") from exc

        token = object()
        self.request = token
        self.progress_state = self.progress_state_for(
            checkpoint,
            active=True,
            phase="正在继续处理" if checkpoint.processed else "正在准备",
        )

        def work(cancel_event: threading.Event) -> None:
            job = BulkImageJob(
                self.database,
                service,
                self.checkpoint_path,
                self.work_coordinator,
            )
            result = job.run(
                checkpoint,
                cancel_event,
                lambda processed, total, item_id, phase: self._worker_progress.emit(
                    token,
                    processed,
                    total,
                    item_id,
                    phase,
                ),
            )
            self._worker_finished.emit(token, result)

        try:
            self._start_task(token, work)
        except Exception as exc:
            self.request = None
            self.progress_state = self.progress_state_for(
                checkpoint,
                phase="已暂停，可继续处理",
                error=str(exc),
            )
            raise
        return checkpoint

    @Slot(object, int, int, int, str)
    def _on_worker_progress(
        self,
        token: object,
        processed: int,
        total: int,
        item_id: int,
        phase: str,
    ) -> None:
        if self.request is not token:
            return
        self.progress_state.update(
            {
                "active": True,
                "resumable": True,
                "processed": processed,
                "total": total,
                "phase": phase,
                "error": "",
            }
        )
        self.progress_changed.emit(processed, total, item_id, phase)

    @Slot(object, object)
    def _on_worker_finished(self, token: object, result: object) -> None:
        if self.request is not token:
            return
        self.request = None
        details = (
            {
                "total": result.total,
                "processed": result.processed,
                "completed": result.completed,
                "skipped": result.skipped,
                "failed": result.failed,
                "cancelled": result.cancelled,
                "error": result.error,
            }
            if isinstance(result, BulkImageResult)
            else (result if isinstance(result, dict) else {})
        )
        checkpoint = self.load_checkpoint()
        error = str(details.get("error") or "")
        completed_all = (
            checkpoint is not None and checkpoint.processed >= checkpoint.total
        )
        if completed_all:
            try:
                clear_checkpoint(self.checkpoint_path)
            except OSError as exc:
                error = error or f"无法清理已完成的批处理断点：{exc}"
            checkpoint = None

        if checkpoint is not None:
            self.progress_state = self.progress_state_for(
                checkpoint,
                phase="已暂停，可继续处理",
                error=error,
            )
        else:
            self.progress_state = self.progress_state_for(
                None,
                phase="已完成" if completed_all else "",
                error=error,
            )
        self.finished.emit(
            BulkImageCompletion(
                details=details,
                error=error,
                completed_all=completed_all,
                checkpoint=checkpoint,
            )
        )

    def _initial_progress_state(self) -> dict[str, object]:
        checkpoint = self.load_checkpoint()
        if checkpoint is not None and checkpoint.processed >= checkpoint.total:
            try:
                clear_checkpoint(self.checkpoint_path)
            except OSError:
                pass
            checkpoint = None
        return self.progress_state_for(
            checkpoint,
            phase="已暂停，可继续处理" if checkpoint is not None else "",
        )
