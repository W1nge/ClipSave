from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QEventLoop, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from .app_paths import AppPaths
from .clipboard_persistence import ClipboardPersistenceWorker, ClipboardTask
from .constants import (
    MARKDOWN_DIR,
    MAX_CLIPBOARD_IMAGE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
    MAX_IMAGE_PIXELS,
    PICTURE_DIR,
)
from .database import LibraryDatabase
from .native_clipboard_reader import ClipboardBusy as _ClipboardBusy, NativeClipboardReader
from .storage import delete_managed_file, open_managed_binary, validate_managed_write_path
from .windows_clipboard import WindowsClipboardNotifier


class ClipboardService(QObject):
    CLIPBOARD_READ_ATTEMPTS = 3
    POLL_INTERVAL_MS = 700
    EVENT_FALLBACK_INTERVAL_MS = 5000
    CLIPBOARD_RETRY_DELAYS_MS = (25, 50, 100, 200)
    WAIT_INTERVAL_SECONDS = 0.01
    PROCESS_EVENTS_MAX_MS = 2
    PERSISTENCE_QUEUE_SIZE = 4
    PERSISTENCE_MEMORY_BUDGET = MAX_CLIPBOARD_IMAGE_BYTES + MAX_CLIPBOARD_TEXT_BYTES
    REGISTERED_IMAGE_FORMATS = NativeClipboardReader.REGISTERED_IMAGE_FORMATS
    CF_DIB = NativeClipboardReader.CF_DIB
    CF_UNICODETEXT = NativeClipboardReader.CF_UNICODETEXT
    CF_HDROP = NativeClipboardReader.CF_HDROP
    CF_DIBV5 = NativeClipboardReader.CF_DIBV5
    MAX_FILE_PATHS = NativeClipboardReader.MAX_FILE_PATHS
    MAX_FILE_PATH_CHARS = NativeClipboardReader.MAX_FILE_PATH_CHARS
    DRAG_QUERY_FILE_COUNT = NativeClipboardReader.DRAG_QUERY_FILE_COUNT

    captured = Signal(int)
    failed = Signal(str)
    state_changed = Signal(bool)
    _persistence_succeeded = Signal(object, object)
    _persistence_failed = Signal(object, str)

    def __init__(self, database: LibraryDatabase, parent=None, *, paths: AppPaths | None = None):
        super().__init__(parent)
        self.database = database
        self.paths = paths
        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_INTERVAL_MS)
        self.timer.timeout.connect(self._poll_if_monitoring)
        self.notifier = WindowsClipboardNotifier(self)
        self.notifier.changed.connect(self._poll_if_monitoring)
        self._monitoring_enabled = False
        self._clipboard_retry_attempt = 0
        self._clipboard_retry_timer = QTimer(self)
        self._clipboard_retry_timer.setSingleShot(True)
        self._clipboard_retry_timer.timeout.connect(self._poll_if_monitoring)
        self.last_text = ""
        self.last_image_key = ""
        self.last_clipboard_sequence: int | None = None
        self._persistence_succeeded.connect(self._finish_task)
        self._persistence_failed.connect(self._fail_task)
        self._persistence_worker = ClipboardPersistenceWorker(
            queue_size=self.PERSISTENCE_QUEUE_SIZE,
            memory_budget_bytes=self.PERSISTENCE_MEMORY_BUDGET,
            wait_interval_seconds=self.WAIT_INTERVAL_SECONDS,
            process_task=self._persist_clipboard_task,
            on_success=self._persistence_succeeded.emit,
            on_failure=self._persistence_failed.emit,
            on_suppressed_finish=self._finish_task_without_signal,
            report_failure=self.failed.emit,
        )

    @property
    def _worker(self):
        return self._persistence_worker.worker

    @property
    def _tasks(self):
        return self._persistence_worker.tasks

    @property
    def _pending_bytes(self) -> int:
        return self._persistence_worker.pending_bytes

    @_pending_bytes.setter
    def _pending_bytes(self, value: int) -> None:
        self._persistence_worker.pending_bytes = int(value)

    @property
    def _accepting_tasks(self) -> bool:
        return self._persistence_worker.accepting_tasks

    @_accepting_tasks.setter
    def _accepting_tasks(self, value: bool) -> None:
        self._persistence_worker.accepting_tasks = bool(value)

    @property
    def _idle_event(self):
        return self._persistence_worker.idle_event

    @property
    def _persistence_state_lock(self):
        return self._persistence_worker.state_lock

    @property
    def _suppress_worker_signals(self) -> bool:
        return self._persistence_worker.suppress_signals

    @_suppress_worker_signals.setter
    def _suppress_worker_signals(self, value: bool) -> None:
        self._persistence_worker.suppress_signals = bool(value)

    @property
    def picture_dir(self) -> Path:
        return self.paths.picture_dir if self.paths is not None else self._default_picture_dir()

    @property
    def markdown_dir(self) -> Path:
        return self.paths.markdown_dir if self.paths is not None else self._default_markdown_dir()

    def _default_picture_dir(self) -> Path:
        return PICTURE_DIR

    def _default_markdown_dir(self) -> Path:
        return MARKDOWN_DIR

    def _open_managed_binary(self, *args, **kwargs):
        return open_managed_binary(*args, **kwargs)

    def _ensure_worker_started(self) -> None:
        self._persistence_worker.ensure_started()

    def start(self) -> None:
        self._ensure_worker_started()
        self.last_clipboard_sequence = self.clipboard_sequence()
        if self.last_clipboard_sequence is not None:
            self._start_monitoring()
            return
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        file_paths = None
        if mime.hasUrls():
            try:
                file_paths = self._snapshot_clipboard_file_paths()
            except _ClipboardBusy:
                file_paths = None
            except ValueError as exc:
                self.failed.emit(str(exc))
        if file_paths is not None:
            self.last_text = file_paths
        elif mime.hasImage():
            try:
                image = self._snapshot_clipboard_image(clipboard)
                self.last_image_key = self.image_key(image) if not image.isNull() else ""
            except _ClipboardBusy:
                self.last_image_key = ""
            except ValueError as exc:
                self.last_image_key = ""
                self.failed.emit(str(exc))
        elif mime.hasText():
            try:
                self.last_text = self._snapshot_clipboard_text(clipboard)
            except _ClipboardBusy:
                self.last_text = ""
            except ValueError as exc:
                self.last_text = ""
                self.failed.emit(str(exc))
        self._start_monitoring()

    def _start_monitoring(self) -> None:
        self._monitoring_enabled = True
        notifier_window = self.parent()
        notifier_active = self.notifier.start(notifier_window)
        self.timer.setInterval(self.EVENT_FALLBACK_INTERVAL_MS if notifier_active else self.POLL_INTERVAL_MS)
        self.timer.start()
        self.state_changed.emit(True)

    def stop(self) -> None:
        self._monitoring_enabled = False
        self._clipboard_retry_timer.stop()
        self._clipboard_retry_attempt = 0
        self.timer.stop()
        self.notifier.stop()
        self.state_changed.emit(False)

    def toggle(self) -> None:
        self.stop() if self.timer.isActive() else self.start()

    def _poll_if_monitoring(self) -> None:
        if self._monitoring_enabled and self._accepting_tasks:
            self.poll()

    @staticmethod
    def _validate_image(image: QImage) -> None:
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            raise ValueError("剪贴板图片无效，已拒绝保存。")
        pixels = image.width() * image.height()
        if pixels > MAX_IMAGE_PIXELS:
            raise ValueError("图片尺寸过大，已拒绝保存。")
        normalized_bytes = pixels * 4
        if normalized_bytes > MAX_CLIPBOARD_IMAGE_BYTES or image.sizeInBytes() > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("图片占用内存过大，已拒绝保存。")

    @classmethod
    def image_key(cls, image: QImage) -> str:
        cls._validate_image(image)
        normalized = image.convertToFormat(QImage.Format.Format_RGBA8888)
        if normalized.sizeInBytes() > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("图片占用内存过大，已拒绝保存。")
        digest = hashlib.blake2b(normalized.constBits(), digest_size=16).hexdigest()
        return f"{digest}:{normalized.width()}:{normalized.height()}"

    @staticmethod
    def _clipboard():
        return QApplication.clipboard()

    @classmethod
    def clipboard_sequence(cls) -> int | None:
        return NativeClipboardReader.clipboard_sequence(
            cls._windows_clipboard_apis
        )

    @staticmethod
    @lru_cache(maxsize=1)
    def _windows_clipboard_apis():
        return NativeClipboardReader.windows_clipboard_apis()

    @staticmethod
    @lru_cache(maxsize=1)
    def _windows_shell_api():
        return NativeClipboardReader.windows_shell_api()

    @staticmethod
    def _registered_image_descriptors_locked(user32, kernel32) -> list[tuple[str, object, int]]:
        return NativeClipboardReader.registered_image_descriptors_locked(
            user32,
            kernel32,
        )

    @staticmethod
    def _copy_clipboard_payload_locked(kernel32, handle, size: int) -> bytes:
        return NativeClipboardReader.copy_clipboard_payload_locked(
            kernel32,
            handle,
            size,
        )

    @classmethod
    def _native_clipboard_image_snapshot(cls) -> tuple[str, bytes] | None:
        return NativeClipboardReader.native_image_snapshot(
            cls._windows_clipboard_apis
        )

    @classmethod
    def _native_clipboard_text_snapshot(cls) -> str | None:
        return NativeClipboardReader.native_text_snapshot(
            cls._windows_clipboard_apis
        )

    @classmethod
    def _native_clipboard_file_paths_snapshot(cls) -> tuple[str, ...] | None:
        return NativeClipboardReader.native_file_paths_snapshot(
            cls._windows_clipboard_apis,
            cls._windows_shell_api,
        )

    @staticmethod
    def _validate_registered_image_header(name: str, size: int, header: bytes) -> None:
        NativeClipboardReader.validate_registered_image_header(name, size, header)

    @staticmethod
    def _dib_as_bmp(name: str, payload: bytes) -> bytes:
        return NativeClipboardReader.dib_as_bmp(name, payload)

    @classmethod
    def _decode_native_clipboard_image(
        cls,
        name: str,
        payload: bytes,
    ) -> QImage:
        return NativeClipboardReader.decode_native_image(
            name,
            payload,
            validate_image=cls._validate_image,
        )

    def _snapshot_clipboard_image(self, clipboard) -> QImage:
        if os.name != "nt":
            return clipboard.image()
        snapshot = self._native_clipboard_image_snapshot()
        if snapshot is None:
            raise ValueError("Unable to safely inspect the clipboard image")
        return self._decode_native_clipboard_image(*snapshot)

    def _snapshot_clipboard_text(self, clipboard) -> str:
        if os.name != "nt":
            text = clipboard.text()
        else:
            text = self._native_clipboard_text_snapshot()
            if text is None:
                raise ValueError("Unable to safely inspect the clipboard text")
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_TEXT_BYTES:
            raise ValueError("剪贴板文字过大，已拒绝读取。")
        return text

    def _snapshot_clipboard_file_paths(self) -> str | None:
        paths = self._native_clipboard_file_paths_snapshot()
        if paths is None:
            return None
        text = "\n".join(paths)
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_TEXT_BYTES:
            raise ValueError("Clipboard file path list is too large")
        return text

    def poll(self) -> None:
        try:
            snapshot = self._read_stable_snapshot()
            self._clipboard_retry_attempt = 0
            if snapshot is None:
                return
            kind, value, sequence = snapshot
            if kind == "image":
                image = value
                self._validate_image(image)
                self._enqueue_task("image", QImage(image), sequence)
                return
            if kind == "text":
                text = value
                if not text.strip():
                    self.last_text = text
                    self.last_clipboard_sequence = sequence
                    return
                if text == self.last_text:
                    self.last_clipboard_sequence = sequence
                    return
                self._enqueue_task("text", text, sequence)
        except _ClipboardBusy:
            if (
                self._clipboard_retry_attempt < len(self.CLIPBOARD_RETRY_DELAYS_MS)
                and not self._clipboard_retry_timer.isActive()
            ):
                index = self._clipboard_retry_attempt
                self._clipboard_retry_attempt += 1
                self._clipboard_retry_timer.start(self.CLIPBOARD_RETRY_DELAYS_MS[index])
            elif not self._clipboard_retry_timer.isActive():
                self._clipboard_retry_attempt = 0
        except Exception as exc:
            self._clipboard_retry_attempt = 0
            self.failed.emit(str(exc))

    def _enqueue_task(self, kind: str, value: QImage | str, sequence: int | None) -> None:
        self._persistence_worker.enqueue(kind, value, sequence)

    def _persist_clipboard_task(self, task: ClipboardTask):
        if task.kind == "image":
            key = self.image_key(task.value)
            with self._persistence_state_lock:
                is_duplicate = key == self.last_image_key
            if not is_duplicate:
                self.save_image(task.value)
            with self._persistence_state_lock:
                self.last_image_key = key
            return key
        self.save_text(task.value)
        return task.value

    def _release_pending(self, task: ClipboardTask) -> None:
        self._persistence_worker.release_pending(task)

    def _finish_task(self, task: ClipboardTask, result) -> None:
        self._release_pending(task)
        if task.kind == "image":
            self.last_image_key = result
        else:
            self.last_text = result
        if task.sequence is not None:
            self.last_clipboard_sequence = task.sequence

    def _finish_task_without_signal(self, task: ClipboardTask, result) -> None:
        self._release_pending(task)
        with self._persistence_state_lock:
            if task.kind == "text" and result is not None:
                self.last_text = result
            if task.sequence is not None:
                self.last_clipboard_sequence = task.sequence

    def _fail_task(self, task: ClipboardTask, message: str) -> None:
        self._release_pending(task)
        self.failed.emit(message)

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        app = QApplication.instance()
        while True:
            remaining = deadline - time.monotonic()
            if app is not None and remaining >= 0.001:
                event_budget_ms = min(self.PROCESS_EVENTS_MAX_MS, max(1, int(remaining * 1000)))
                flags = (
                    QEventLoop.ProcessEventsFlag.AllEvents
                    | QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                )
                app.processEvents(flags, event_budget_ms)
            if self._idle_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._idle_event.wait(min(remaining, self.WAIT_INTERVAL_SECONDS))

    def shutdown(self, timeout: float = 10.0) -> bool:
        self.prepare_for_shutdown()
        return self._persistence_worker.shutdown(
            timeout,
            wait_for_idle=self.wait_for_idle,
        )

    def prepare_for_shutdown(self) -> None:
        """Immediately pause monitoring and reject newly queued persistence work."""
        self.stop()
        self._persistence_worker.prepare_for_shutdown()

    def resume_after_failed_shutdown(self, restart_monitoring: bool) -> None:
        if self._persistence_worker.shutdown_complete:
            return
        self._persistence_worker.resume_after_failed_shutdown()
        if restart_monitoring and not self.timer.isActive():
            self._start_monitoring()
            QTimer.singleShot(0, self._poll_if_monitoring)

    def _read_stable_snapshot(self) -> tuple[str, QImage | str, int | None] | None:
        for _attempt in range(self.CLIPBOARD_READ_ATTEMPTS):
            sequence_before = self.clipboard_sequence()
            if sequence_before is not None and sequence_before == self.last_clipboard_sequence:
                return None
            clipboard = self._clipboard()
            mime = clipboard.mimeData()
            snapshot: tuple[str, QImage | str] | None
            candidate_errors: list[ValueError] = []
            file_paths = None
            if mime.hasUrls():
                try:
                    file_paths = self._snapshot_clipboard_file_paths()
                except ValueError as exc:
                    candidate_errors.append(exc)
            if file_paths is not None:
                snapshot = (
                    "text",
                    file_paths,
                )
            elif mime.hasImage():
                try:
                    snapshot = (
                        "image",
                        self._snapshot_clipboard_image(clipboard),
                    )
                except ValueError as exc:
                    candidate_errors.append(exc)
                    snapshot = None
            else:
                snapshot = None
            if snapshot is None and mime.hasText():
                try:
                    snapshot = ("text", self._snapshot_clipboard_text(clipboard))
                except ValueError as exc:
                    candidate_errors.append(exc)
            if snapshot is None and candidate_errors:
                raise candidate_errors[-1]
            sequence_after = self.clipboard_sequence()
            if (
                sequence_before is not None
                and sequence_after is not None
                and sequence_before != sequence_after
            ):
                continue
            if snapshot is None:
                return None
            sequence = sequence_after if sequence_after is not None else sequence_before
            return snapshot[0], snapshot[1], sequence
        raise RuntimeError("读取剪贴板时内容持续变化，请稍后重试。")

    def _remove_new_image(
        self,
        path: Path,
        original_error: BaseException | None = None,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> None:
        try:
            if path.exists():
                delete_managed_file(
                    path,
                    self.picture_dir,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
        except (OSError, RuntimeError) as cleanup_error:
            if original_error is not None:
                return
            raise OSError(f"无法清理重复图片文件: {path}") from cleanup_error

    def _database_image_owner(self, digest: str):
        return self.database.indexed_file_for_hash(digest)

    def save_image(self, image: QImage) -> bool:
        self._validate_image(image)
        now = dt.datetime.now().astimezone()
        folder = self.picture_dir / f"{now:%Y-%m-%d}"
        validate_managed_write_path(folder / "capture.tmp", self.picture_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"image_{now:%Y%m%d_%H%M%S_%f}.png"
        payload: bytes | None = None
        payload_hash: str | None = None
        owner = None
        try:
            encoded = QByteArray()
            buffer = QBuffer(encoded)
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
                raise OSError(f"无法保存图片: {path}")
            buffer.close()
            payload = bytes(encoded)
            if len(payload) > MAX_CLIPBOARD_IMAGE_BYTES:
                raise ValueError("图片 PNG 数据过大，已拒绝保存。")
            with self._open_managed_binary(path, "xb", self.picture_dir) as handle:
                handle.write(payload)
            payload_hash = hashlib.sha256(payload).hexdigest()
            with self._open_managed_binary(
                path, "rb", self.picture_dir, identity_locked=True
            ) as owned_file:
                owned_hash = hashlib.sha256()
                owned_size = 0
                while chunk := owned_file.read(1024 * 1024):
                    owned_hash.update(chunk)
                    owned_size += len(chunk)
                if owned_size != len(payload) or owned_hash.hexdigest() != payload_hash:
                    raise RuntimeError("Captured image changed before it could be indexed")
                item_id = self.database.add_verified_image(
                    path,
                    content_hash=payload_hash,
                    file_size=owned_size,
                    width=image.width(),
                    height=image.height(),
                    created_at=now,
                )
                if item_id:
                    if not self._suppress_worker_signals:
                        self.captured.emit(item_id)
                    return True
                owner = self._database_image_owner(payload_hash)
                if owner is not None and owner["resolved_path"] == self.database.path_key(path):
                    if not self._suppress_worker_signals:
                        self.captured.emit(owner["id"])
                    return True
        except BaseException as exc:
            self._remove_new_image(
                path,
                exc,
                expected_sha256=payload_hash,
                expected_size=len(payload) if payload is not None else None,
            )
            raise
        self._remove_new_image(
            path,
            expected_sha256=payload_hash,
            expected_size=len(payload),
        )
        if owner is not None:
            if not self._suppress_worker_signals:
                self.captured.emit(owner["id"])
            return True
        raise RuntimeError("图片文件已写入，但数据库未能保存该记录。")

    def save_text(self, text: str) -> bool:
        byte_size = len(text.encode("utf-8"))
        if byte_size > MAX_CLIPBOARD_TEXT_BYTES:
            raise ValueError(f"剪贴板文字超过 {MAX_CLIPBOARD_TEXT_BYTES // (1024 * 1024)} MiB，已拒绝保存。")
        now = dt.datetime.now().astimezone()
        item_id = self.database.add_text(text, now)
        if not item_id:
            return True
        daily = self.markdown_dir / f"clipboard_{now:%Y-%m-%d}.md"
        warning = ""
        try:
            entry = f"\n\n---\n\n**{now:%H:%M:%S}**\n\n{text}\n".encode("utf-8")
            try:
                with self._open_managed_binary(daily, "xb", self.markdown_dir) as handle:
                    handle.write(f"# ClipSave {now:%Y-%m-%d}\n".encode("utf-8"))
                    handle.write(entry)
            except FileExistsError:
                with self._open_managed_binary(daily, "ab", self.markdown_dir) as handle:
                    handle.write(entry)
        except (OSError, RuntimeError, UnicodeError) as exc:
            warning = f"文字已保存到数据库，但写入每日 Markdown 失败: {exc}"
        if not self._suppress_worker_signals:
            self.captured.emit(item_id)
        if warning and not self._suppress_worker_signals:
            self.failed.emit(warning)
        return True

    def suppress_text(self, text: str) -> None:
        self.last_text = text
        self.last_clipboard_sequence = self.clipboard_sequence()

    def suppress_image(self, image: QImage) -> None:
        self.last_image_key = self.image_key(image)
        self.last_clipboard_sequence = self.clipboard_sequence()
