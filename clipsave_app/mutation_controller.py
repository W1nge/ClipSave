from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from .database import ImportFileDetails, LibraryDatabase
from .services import TaskCapacityExceeded, ai_ocr_task_executor
from .task_supervisor import TaskSupervisor


class LibraryMutationController(QObject):
    import_finished = Signal(object, object, object)
    import_failed = Signal(object, object, str)
    copy_succeeded = Signal(object, object, int, object)
    copy_failed = Signal(object, object, str)
    delete_finished = Signal(object, object, int, object)
    delete_failed = Signal(object, object, int, str)

    def __init__(
        self,
        database: LibraryDatabase,
        supervisor: TaskSupervisor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.supervisor = supervisor
        self.import_request: tuple[object, QObject] | None = None
        self.copy_request: tuple[object, QObject, int] | None = None
        self.delete_requests: dict[int, tuple[object, QObject, bool, bool]] = {}
        self.pending_delete_item_ids: set[int] = set()

    def start_import(self, filenames: Sequence[str]) -> tuple[object, QObject]:
        if self.import_request is not None:
            raise RuntimeError("An import request is already active")
        token = object()
        marker = QObject(self)
        request = (token, marker)
        self.import_request = request
        paths = tuple(filenames)

        def work(cancel_event: threading.Event) -> None:
            try:
                added = 0
                localized = 0
                duplicates = 0
                processed = 0
                failed: list[tuple[str, str]] = []
                image_ids: list[int] = []
                for filename in paths:
                    if cancel_event.is_set():
                        break
                    try:
                        candidate = Path(filename)
                        import_result = self.database.import_file(
                            candidate,
                            copy_to_library=True,
                            strict=True,
                            detailed=True,
                        )
                        if not isinstance(import_result, ImportFileDetails):
                            raise RuntimeError("导入未返回详细结果")
                        if import_result.localized:
                            localized += 1
                        elif import_result.added:
                            added += 1
                        else:
                            duplicates += 1
                        if (
                            candidate.suffix.lower() != ".md"
                            and import_result.item_id is not None
                            and (import_result.added or import_result.localized)
                        ):
                            image_ids.append(import_result.item_id)
                    except Exception as exc:
                        failed.append((Path(filename).name, str(exc)))
                    finally:
                        processed += 1
                result = {
                    "total": len(paths),
                    "added": added,
                    "localized": localized,
                    "duplicates": duplicates,
                    "processed": processed,
                    "failed": failed,
                    "image_ids": list(dict.fromkeys(image_ids)),
                    "cancelled": cancel_event.is_set(),
                }
                self.import_finished.emit(token, marker, result)
            except Exception as exc:
                if not cancel_event.is_set():
                    self.import_failed.emit(token, marker, str(exc))

        try:
            self.supervisor.start_thread(token, work)
        except Exception:
            self.import_request = None
            raise
        return request

    def start_copy_image(self, item_id: int, snapshot) -> tuple[object, QObject, int]:
        self.cancel_copy()
        token = object()
        marker = QObject(self)
        request = (token, marker, item_id)
        self.copy_request = request

        def work(cancel_event: threading.Event) -> None:
            try:
                snapshot.require_current()
                image = QImage(str(snapshot.path))
                snapshot.require_current()
                if image.isNull():
                    raise ValueError("图片文件无法读取，可能已损坏或无权访问。")
                if not cancel_event.is_set():
                    self.copy_succeeded.emit(token, marker, item_id, image)
            except Exception as exc:
                if not cancel_event.is_set():
                    self.copy_failed.emit(token, marker, str(exc))

        try:
            handle = ai_ocr_task_executor().submit(
                work,
                estimated_bytes=snapshot.decoded_bytes,
            )
            self.supervisor.track_bounded(token, handle)
        except (TaskCapacityExceeded, RuntimeError):
            self.copy_request = None
            raise
        return request

    def cancel_copy(self) -> None:
        request = self.copy_request
        if request is None:
            return
        self.copy_request = None
        self.supervisor.cancel(request[0])

    def start_delete(
        self,
        item_snapshot: dict[str, object],
        *,
        was_selected: bool,
        detail_was_visible: bool,
        is_managed: Callable[[Path], bool],
        recycle: Callable[..., None],
        library_root: Path,
    ) -> tuple[object, QObject, bool, bool]:
        item_id = int(item_snapshot["id"])
        if item_id in self.delete_requests:
            raise RuntimeError("Delete request is already active")
        token = object()
        marker = QObject(self)
        request = (token, marker, was_selected, detail_was_visible)
        self.delete_requests[item_id] = request
        self.pending_delete_item_ids.add(item_id)

        def work(cancel_event: threading.Event) -> None:
            recycled = False
            stage = "preflight"
            try:
                path_value = item_snapshot.get("path")
                path = Path(str(path_value)) if path_value else None
                managed_file = bool(path is not None and is_managed(path))
                file_exists = bool(path is not None and path.exists())
                if cancel_event.is_set():
                    self.delete_finished.emit(token, marker, item_id, {"outcome": "cancelled"})
                    return
                if managed_file and file_exists and path is not None:
                    stage = "recycle"
                    if not is_managed(path):
                        raise RuntimeError("文件路径在确认期间发生变化，已取消删除。")
                    recycle(
                        path,
                        library_root,
                        expected_sha256=item_snapshot.get("content_hash"),
                        expected_size=item_snapshot.get("file_size"),
                    )
                    recycled = True
                if cancel_event.is_set() and not recycled:
                    self.delete_finished.emit(token, marker, item_id, {"outcome": "cancelled"})
                    return
                stage = "index"
                try:
                    self.database.remove_item(item_id)
                except Exception as exc:
                    if not recycled:
                        self.delete_finished.emit(
                            token,
                            marker,
                            item_id,
                            {"outcome": "failed", "stage": "index", "error": str(exc)},
                        )
                        return
                    mark_error = None
                    try:
                        self.database.mark_item_missing(item_id)
                    except Exception as mark_exc:
                        mark_error = str(mark_exc)
                    self.delete_finished.emit(
                        token,
                        marker,
                        item_id,
                        {
                            "outcome": "reconciled",
                            "error": str(exc),
                            "mark_error": mark_error,
                            "managed_file": managed_file,
                            "file_exists": file_exists,
                        },
                    )
                    return
                self.delete_finished.emit(
                    token,
                    marker,
                    item_id,
                    {
                        "outcome": "deleted",
                        "managed_file": managed_file,
                        "file_exists": file_exists,
                    },
                )
            except Exception as exc:
                self.delete_finished.emit(
                    token,
                    marker,
                    item_id,
                    {"outcome": "failed", "stage": stage, "error": str(exc)},
                )

        try:
            self.supervisor.start_thread(token, work)
        except Exception as exc:
            self.delete_requests.pop(item_id, None)
            self.pending_delete_item_ids.discard(item_id)
            self.delete_failed.emit(token, marker, item_id, str(exc))
        return request

