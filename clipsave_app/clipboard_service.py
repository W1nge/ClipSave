from __future__ import annotations

import hashlib
import os
import time
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from .app_paths import AppPaths
from .clipboard_capture_store import ClipboardCaptureStore, validate_clipboard_image
from .clipboard_persistence import ClipboardPersistenceWorker, ClipboardTask
from .constants import (
    MARKDOWN_DIR,
    MAX_CLIPBOARD_IMAGE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
    PICTURE_DIR,
)
from .database import LibraryDatabase
from .native_clipboard_reader import ClipboardBusy as _ClipboardBusy, NativeClipboardReader
from .storage import open_managed_binary
from .windows_clipboard import WindowsClipboardNotifier


class ClipboardService(QObject):
    CLIPBOARD_READ_ATTEMPTS = 3
    POLL_INTERVAL_MS = 700
    EVENT_FALLBACK_INTERVAL_MS = 5000
    CLIPBOARD_RETRY_DELAYS_MS = (25, 50, 100, 200, 400, 800, 1000)
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
    reused = Signal(int)
    failed = Signal(str)
    state_changed = Signal(bool)
    _persistence_succeeded = Signal(object, object)
    _persistence_failed = Signal(object, str)

    def __init__(self, database: LibraryDatabase, parent=None, *, paths: AppPaths | None = None):
        super().__init__(parent)
        self.database = database
        self.paths = paths
        self._notifier_window = parent
        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_INTERVAL_MS)
        self.timer.timeout.connect(self._poll_if_monitoring)
        self.notifier = WindowsClipboardNotifier(self)
        self.notifier.changed.connect(self._queue_clipboard_poll)
        self._monitoring_enabled = False
        self._polling = False
        self._poll_requested = False
        self._clipboard_retry_attempt = 0
        self._clipboard_retry_timer = QTimer(self)
        self._clipboard_retry_timer.setSingleShot(True)
        self._clipboard_retry_timer.timeout.connect(self._poll_if_monitoring)
        self.last_text = ""
        self.last_image_key = ""
        self.last_clipboard_sequence: int | None = None
        self._pending_capture_sequences: set[int] = set()
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
        self._capture_store = ClipboardCaptureStore(
            database,
            picture_dir=lambda: self.picture_dir,
            markdown_dir=lambda: self.markdown_dir,
            open_managed_binary=lambda *args, **kwargs: self._open_managed_binary(
                *args,
                **kwargs,
            ),
        )

    def set_notifier_window(self, window) -> None:
        """Bind the window used to register native clipboard notifications.

        This is intentionally independent of QObject ownership: process-lifetime
        services may be owned by ApplicationRuntime while listening on a window HWND.
        """
        if self._notifier_window is window:
            return
        self._notifier_window = window
        if not self._monitoring_enabled:
            return
        self.notifier.stop()
        notifier_active = self.notifier.start(window)
        self.timer.setInterval(
            self.EVENT_FALLBACK_INTERVAL_MS
            if notifier_active
            else self.POLL_INTERVAL_MS
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
        notifier_active = self.notifier.start(self._notifier_window)
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

    def _queue_clipboard_poll(self) -> None:
        if not self._monitoring_enabled or not self._accepting_tasks:
            return
        if self._polling:
            self._poll_requested = True
            return
        # Finish WM_CLIPBOARDUPDATE dispatch before asking the owner for data.
        # Reading inline can enter OLE's retry/message loop before Qt has even
        # processed the change. A new change also expedites any busy retry.
        if not self._clipboard_retry_timer.isActive() or self._clipboard_retry_timer.remainingTime() > 0:
            self._clipboard_retry_timer.start(0)

    @staticmethod
    def _validate_image(image: QImage) -> None:
        validate_clipboard_image(image)

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
        if self._polling:
            self._poll_requested = True
            return
        self._polling = True
        try:
            if self._pending_capture_sequences:
                pending_sequence = self.clipboard_sequence()
                if (
                    pending_sequence is not None
                    and pending_sequence in self._pending_capture_sequences
                ):
                    return
            snapshot = self._read_stable_snapshot()
            self._clipboard_retry_attempt = 0
            if snapshot is None:
                return
            kind, value, sequence = snapshot
            if kind == "image":
                image = value
                self._validate_image(image)
                if sequence is not None:
                    self._pending_capture_sequences.add(sequence)
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
                if sequence is not None:
                    self._pending_capture_sequences.add(sequence)
                self._enqueue_task("text", text, sequence)
        except _ClipboardBusy:
            if not self._clipboard_retry_timer.isActive():
                index = min(self._clipboard_retry_attempt, len(self.CLIPBOARD_RETRY_DELAYS_MS) - 1)
                self._clipboard_retry_attempt = index + 1
                self._clipboard_retry_timer.start(self.CLIPBOARD_RETRY_DELAYS_MS[index])
        except Exception as exc:
            self._clipboard_retry_attempt = 0
            self.failed.emit(str(exc))
        finally:
            self._polling = False
            if self._poll_requested:
                self._poll_requested = False
                self._queue_clipboard_poll()

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
                if not self._sequence_is_stale(task.sequence):
                    self.last_image_key = key
            return key
        self.save_text(task.value)
        return task.value

    def _sequence_is_stale(self, task_sequence: int | None) -> bool:
        """Return True when task_sequence is an older, wrap-aware sequence.

        ``suppress_text``/``suppress_image`` record the clipboard sequence of
        newer content; a persistence task read before that suppression must
        not regress the dedup state when it finishes late.
        """
        current = self.last_clipboard_sequence
        if task_sequence is None or current is None or task_sequence == current:
            return False
        behind = (current - task_sequence) % (1 << 32)
        return 0 < behind < (1 << 31)

    def _release_pending(self, task: ClipboardTask) -> None:
        self._persistence_worker.release_pending(task)

    def _finish_task(self, task: ClipboardTask, result) -> None:
        self._release_pending(task)
        with self._persistence_state_lock:
            if not self._sequence_is_stale(task.sequence):
                if task.kind == "image":
                    self.last_image_key = result
                else:
                    self.last_text = result
                if task.sequence is not None:
                    self.last_clipboard_sequence = task.sequence
            if task.sequence is not None:
                self._pending_capture_sequences.discard(task.sequence)

    def _finish_task_without_signal(self, task: ClipboardTask, result) -> None:
        self._release_pending(task)
        with self._persistence_state_lock:
            if not self._sequence_is_stale(task.sequence):
                if task.kind == "text" and result is not None:
                    self.last_text = result
                if task.sequence is not None:
                    self.last_clipboard_sequence = task.sequence
            if task.sequence is not None:
                self._pending_capture_sequences.discard(task.sequence)

    def _fail_task(self, task: ClipboardTask, message: str) -> None:
        self._release_pending(task)
        with self._persistence_state_lock:
            if task.sequence is not None:
                self._pending_capture_sequences.discard(task.sequence)
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
            snapshot = self._read_clipboard_snapshot()
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
        raise _ClipboardBusy("Clipboard changed while copying its contents")

    def _read_clipboard_snapshot(self) -> tuple[str, QImage | str] | None:
        if os.name != "nt":
            return self._read_qt_clipboard_snapshot()
        # Qt's OLE MIME object can be unavailable or stale while another
        # application owns the clipboard. Detect and copy native formats
        # directly; an OLE format-query failure must not suppress acquisition
        # or prevent ClipboardBusy from reaching the retry timer.
        errors: list[ValueError] = []
        try:
            paths = self._snapshot_clipboard_file_paths()
            if paths is not None:
                return "text", paths
        except ValueError as exc:
            errors.append(exc)
        try:
            image = self._native_clipboard_image_snapshot()
            if image is not None:
                return "image", self._decode_native_clipboard_image(*image)
        except ValueError as exc:
            errors.append(exc)
        try:
            text = self._native_clipboard_text_snapshot()
            if text is not None:
                return "text", text
        except ValueError as exc:
            errors.append(exc)
        if errors:
            raise errors[-1]
        return None

    def _read_qt_clipboard_snapshot(self) -> tuple[str, QImage | str] | None:
        clipboard = self._clipboard()
        mime = clipboard.mimeData()
        if mime is None:
            raise _ClipboardBusy("Qt clipboard data is temporarily unavailable")
        errors: list[ValueError] = []
        if mime.hasUrls():
            try:
                paths = self._snapshot_clipboard_file_paths()
                if paths is not None:
                    return "text", paths
            except ValueError as exc:
                errors.append(exc)
        if mime.hasImage():
            try:
                return "image", self._snapshot_clipboard_image(clipboard)
            except ValueError as exc:
                errors.append(exc)
        if mime.hasText():
            try:
                return "text", self._snapshot_clipboard_text(clipboard)
            except ValueError as exc:
                errors.append(exc)
        if errors:
            raise errors[-1]
        return None

    def save_image(self, image: QImage) -> bool:
        result = self._capture_store.save_image(image)
        if result.item_id is not None and not self._suppress_worker_signals:
            (self.captured if result.created else self.reused).emit(result.item_id)
        return True

    def save_text(self, text: str) -> bool:
        result = self._capture_store.save_text(text)
        if result.item_id is not None and not self._suppress_worker_signals:
            (self.captured if result.created else self.reused).emit(result.item_id)
        if result.warning and not self._suppress_worker_signals:
            self.failed.emit(result.warning)
        return True

    def suppress_text(self, text: str) -> None:
        with self._persistence_state_lock:
            self.last_text = text
            self.last_clipboard_sequence = self.clipboard_sequence()

    def suppress_image(self, image: QImage) -> None:
        with self._persistence_state_lock:
            self.last_image_key = self.image_key(image)
            self.last_clipboard_sequence = self.clipboard_sequence()
