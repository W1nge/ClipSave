from __future__ import annotations

import base64
import ctypes
import datetime as dt
import hashlib
import io
import json
import math
import os
import queue
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QByteArray,
    QBuffer,
    QCoreApplication,
    QEventLoop,
    QIODevice,
    QObject,
    QTimer,
    Signal,
)
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from .constants import (
    MARKDOWN_DIR,
    MAX_AI_RESPONSE_BYTES,
    MAX_CLIPBOARD_IMAGE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
    MAX_EMBEDDING_DIMENSIONS,
    MAX_IMAGE_PIXELS,
    PICTURE_DIR,
)
from .database import LibraryDatabase
from .storage import (
    delete_managed_file,
    open_managed_binary,
    validate_managed_write_path,
)


class OperationCancelled(RuntimeError):
    pass


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Operation cancelled")


class TaskCapacityExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    size_bytes: int
    modified_ns: int
    device: int
    inode: int

    def is_current(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return False
        return (
            stat.st_size == self.size_bytes
            and stat.st_mtime_ns == self.modified_ns
            and stat.st_dev == self.device
            and stat.st_ino == self.inode
        )

    def require_current(self) -> None:
        if not self.is_current():
            raise RuntimeError("文件在处理期间已被移动或修改，请重试。")


@dataclass(frozen=True)
class ImageFileSnapshot(FileSnapshot):
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def decoded_bytes(self) -> int:
        return self.pixels * 4


def preflight_current_file(path: Path, *, max_file_bytes: int | None = None) -> FileSnapshot:
    resolved = Path(path).resolve()
    try:
        stat = resolved.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"文件不存在或已被移动: {resolved}") from exc
    except OSError as exc:
        raise OSError(f"无法访问文件: {resolved}") from exc
    if not resolved.is_file():
        raise ValueError(f"路径不是文件: {resolved}")
    if max_file_bytes is not None and stat.st_size > max_file_bytes:
        raise ValueError("文件过大，已拒绝处理。")
    return FileSnapshot(resolved, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)


def preflight_image_file(
    path: Path,
    *,
    max_pixels: int = MAX_IMAGE_PIXELS,
    max_file_bytes: int | None = None,
) -> ImageFileSnapshot:
    snapshot = preflight_current_file(path, max_file_bytes=max_file_bytes)
    try:
        with Image.open(snapshot.path) as image:
            width, height = image.size
            image.load()
    except (OSError, ValueError) as exc:
        raise ValueError("图片文件无法读取或格式无效。") from exc
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效，已拒绝处理。")
    if width * height > max_pixels:
        raise ValueError("图片尺寸过大，已拒绝处理。")
    snapshot.require_current()
    return ImageFileSnapshot(
        snapshot.path,
        snapshot.size_bytes,
        snapshot.modified_ns,
        snapshot.device,
        snapshot.inode,
        width,
        height,
    )


@dataclass
class TaskHandle:
    cancel_event: threading.Event
    done_event: threading.Event
    exception: BaseException | None = None

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done_event.wait(timeout)


@dataclass(frozen=True)
class _BoundedTask:
    target: Callable[[threading.Event], None]
    handle: TaskHandle
    reserved_bytes: int


class BoundedTaskExecutor:
    def __init__(self, *, max_active: int = 2, max_queued: int = 4, memory_budget_bytes: int = 512 * 1024 * 1024):
        if max_active < 1 or max_queued < 0 or memory_budget_bytes < 1:
            raise ValueError("Invalid bounded executor limits")
        self.max_active = max_active
        self.max_queued = max_queued
        self.memory_budget_bytes = memory_budget_bytes
        self._queue: queue.Queue[_BoundedTask | None] = queue.Queue(max_queued + max_active)
        self._lock = threading.Lock()
        self._reserved_bytes = 0
        self._pending_count = 0
        self._active_count = 0
        self._accepting = True
        self._shutdown_sentinels_enqueued = 0
        self._workers = [
            threading.Thread(target=self._worker_loop, name=f"ClipSaveAIOrOCR-{index + 1}", daemon=True)
            for index in range(max_active)
        ]
        for worker in self._workers:
            worker.start()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_count

    @property
    def queued_count(self) -> int:
        with self._lock:
            return self._pending_count - self._active_count

    def submit(
        self,
        target: Callable[[threading.Event], None],
        *,
        estimated_bytes: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> TaskHandle:
        if estimated_bytes < 0:
            raise ValueError("estimated_bytes must not be negative")
        handle = TaskHandle(cancel_event or threading.Event(), threading.Event())
        with self._lock:
            if not self._accepting:
                raise RuntimeError("Task executor is shut down")
            if self._pending_count >= self.max_queued + self.max_active:
                raise TaskCapacityExceeded("AI/OCR 任务队列已满，请稍后重试。")
            if self._reserved_bytes + estimated_bytes > self.memory_budget_bytes:
                raise TaskCapacityExceeded("AI/OCR 任务占用内存过高，请稍后重试。")
            self._reserved_bytes += estimated_bytes
            self._pending_count += 1
            try:
                self._queue.put_nowait(_BoundedTask(target, handle, estimated_bytes))
            except queue.Full:
                self._reserved_bytes -= estimated_bytes
                self._pending_count -= 1
                raise TaskCapacityExceeded("AI/OCR 任务队列已满，请稍后重试。")
        return handle

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            with self._lock:
                self._active_count += 1
            try:
                if not task.handle.cancelled:
                    task.target(task.handle.cancel_event)
            except BaseException as exc:
                task.handle.exception = exc
            finally:
                with self._lock:
                    self._reserved_bytes = max(0, self._reserved_bytes - task.reserved_bytes)
                    self._active_count -= 1
                    self._pending_count -= 1
                task.handle.done_event.set()
                self._queue.task_done()

    def shutdown(self, *, cancel_pending: bool = True, timeout: float = 2.0) -> bool:
        with self._lock:
            if not self._accepting and all(not worker.is_alive() for worker in self._workers):
                return all(not worker.is_alive() for worker in self._workers)
            self._accepting = False
        if cancel_pending:
            with self._queue.mutex:
                for task in self._queue.queue:
                    if task is not None:
                        task.handle.cancel()
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            sentinels_needed = len(self._workers) - self._shutdown_sentinels_enqueued
        for _worker in range(sentinels_needed):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                self._queue.put(None, timeout=remaining)
            except queue.Full:
                return False
            with self._lock:
                self._shutdown_sentinels_enqueued += 1
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            worker.join(remaining)
        return all(not worker.is_alive() for worker in self._workers)


_AI_OCR_EXECUTOR: BoundedTaskExecutor | None = None
_AI_OCR_EXECUTOR_LOCK = threading.Lock()


def ai_ocr_task_executor() -> BoundedTaskExecutor:
    global _AI_OCR_EXECUTOR
    with _AI_OCR_EXECUTOR_LOCK:
        if _AI_OCR_EXECUTOR is None:
            _AI_OCR_EXECUTOR = BoundedTaskExecutor()
        return _AI_OCR_EXECUTOR


def shutdown_ai_ocr_task_executor(timeout: float = 2.0) -> bool:
    global _AI_OCR_EXECUTOR
    with _AI_OCR_EXECUTOR_LOCK:
        executor = _AI_OCR_EXECUTOR
    if executor is None:
        return True
    stopped = executor.shutdown(timeout=timeout)
    if stopped:
        with _AI_OCR_EXECUTOR_LOCK:
            if _AI_OCR_EXECUTOR is executor:
                _AI_OCR_EXECUTOR = None
    return stopped


class WindowsClipboardNotifier(QObject, QAbstractNativeEventFilter):
    WM_CLIPBOARDUPDATE = 0x031D
    changed = Signal()

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._hwnd: int | None = None
        self._installed = False

    @staticmethod
    def _user32():
        return ctypes.windll.user32

    @property
    def active(self) -> bool:
        return self._installed

    def start(self, window) -> bool:
        if os.name != "nt" or self._installed:
            return self._installed
        app = QCoreApplication.instance()
        if app is None or window is None:
            return False
        try:
            hwnd = int(window.winId())
            if not hwnd or not self._user32().AddClipboardFormatListener(hwnd):
                return False
            app.installNativeEventFilter(self)
        except (AttributeError, OSError, TypeError):
            return False
        self._hwnd = hwnd
        self._installed = True
        return True

    def stop(self) -> None:
        if not self._installed:
            return
        app = QCoreApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)
        try:
            self._user32().RemoveClipboardFormatListener(self._hwnd)
        except (AttributeError, OSError, TypeError):
            pass
        self._hwnd = None
        self._installed = False

    def nativeEventFilter(self, _event_type, message):
        if self._installed and self._is_clipboard_message(message):
            self.changed.emit()
        return False, 0

    def _is_clipboard_message(self, message) -> bool:
        try:
            from ctypes import wintypes

            native = wintypes.MSG.from_address(int(message))
            return int(native.hWnd) == self._hwnd and native.message == self.WM_CLIPBOARDUPDATE
        except (TypeError, ValueError, OSError):
            return False


@dataclass(frozen=True)
class _ClipboardTask:
    token: int
    kind: str
    value: QImage | str
    sequence: int | None
    estimated_bytes: int


class ClipboardService(QObject):
    CLIPBOARD_READ_ATTEMPTS = 3
    POLL_INTERVAL_MS = 700
    EVENT_FALLBACK_INTERVAL_MS = 5000
    WAIT_INTERVAL_SECONDS = 0.01
    PROCESS_EVENTS_MAX_MS = 2
    PERSISTENCE_QUEUE_SIZE = 4
    PERSISTENCE_MEMORY_BUDGET = MAX_CLIPBOARD_IMAGE_BYTES + MAX_CLIPBOARD_TEXT_BYTES
    REGISTERED_IMAGE_FORMATS = {"PNG", "image/png"}
    CF_DIB = 8
    CF_UNICODETEXT = 13
    CF_DIBV5 = 17

    captured = Signal(int)
    failed = Signal(str)
    state_changed = Signal(bool)
    _persistence_succeeded = Signal(object, object)
    _persistence_failed = Signal(object, str)

    def __init__(self, database: LibraryDatabase, parent=None):
        super().__init__(parent)
        self.database = database
        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.poll)
        self.notifier = WindowsClipboardNotifier(self)
        self.notifier.changed.connect(self.poll)
        self.last_text = ""
        self.last_image_key = ""
        self.last_clipboard_sequence: int | None = None
        self._tasks: queue.Queue[_ClipboardTask] = queue.Queue(self.PERSISTENCE_QUEUE_SIZE)
        self._pending_sequences: set[int] = set()
        self._pending_without_sequence = False
        self._pending_bytes = 0
        self._next_task_token = 1
        self._accepting_tasks = True
        self._shutdown_complete = False
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._persistence_state_lock = threading.Lock()
        self._worker_lifecycle_lock = threading.Lock()
        self._worker_stop_requested = False
        self._worker_exited = threading.Event()
        self._worker = threading.Thread(target=self._persistence_loop, name="ClipSavePersistence", daemon=True)
        self._persistence_succeeded.connect(self._finish_task)
        self._persistence_failed.connect(self._fail_task)
        self._worker.start()

    def start(self) -> None:
        self.last_clipboard_sequence = self.clipboard_sequence()
        if self.last_clipboard_sequence is not None:
            self._start_monitoring()
            return
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime.hasImage():
            try:
                image = self._snapshot_clipboard_image(clipboard)
                self.last_image_key = self.image_key(image) if not image.isNull() else ""
            except ValueError as exc:
                self.last_image_key = ""
                self.failed.emit(str(exc))
        elif mime.hasText():
            try:
                self.last_text = self._snapshot_clipboard_text(clipboard)
            except ValueError as exc:
                self.last_text = ""
                self.failed.emit(str(exc))
        self._start_monitoring()

    def _start_monitoring(self) -> None:
        notifier_window = self.parent()
        notifier_active = self.notifier.start(notifier_window)
        self.timer.setInterval(self.EVENT_FALLBACK_INTERVAL_MS if notifier_active else self.POLL_INTERVAL_MS)
        self.timer.start()
        self.state_changed.emit(True)

    def stop(self) -> None:
        self.timer.stop()
        self.notifier.stop()
        self.state_changed.emit(False)

    def toggle(self) -> None:
        self.stop() if self.timer.isActive() else self.start()

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

    @staticmethod
    def image_key(image: QImage) -> str:
        ClipboardService._validate_image(image)
        normalized = image.convertToFormat(QImage.Format.Format_RGBA8888)
        if normalized.sizeInBytes() > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("图片占用内存过大，已拒绝保存。")
        digest = hashlib.blake2b(normalized.constBits(), digest_size=16).hexdigest()
        return f"{digest}:{normalized.width()}:{normalized.height()}"

    @staticmethod
    def _clipboard():
        return QApplication.clipboard()

    @staticmethod
    def clipboard_sequence() -> int | None:
        if os.name != "nt":
            return None
        try:
            user32, _kernel32 = ClipboardService._windows_clipboard_apis()
            return int(user32.GetClipboardSequenceNumber())
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _windows_clipboard_apis():
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
        user32.EnumClipboardFormats.restype = wintypes.UINT
        user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClipboardFormatNameW.restype = ctypes.c_int
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.SetLastError.argtypes = [wintypes.DWORD]
        kernel32.SetLastError.restype = None
        return user32, kernel32

    @staticmethod
    def _native_clipboard_format_size(format_id: int) -> int | None:
        if os.name != "nt":
            return None
        try:
            user32, kernel32 = ClipboardService._windows_clipboard_apis()
            if not user32.OpenClipboard(None):
                return None
            try:
                if not user32.IsClipboardFormatAvailable(format_id):
                    return None
                handle = user32.GetClipboardData(format_id)
                if not handle:
                    return None
                size = int(kernel32.GlobalSize(handle))
                return size or None
            finally:
                user32.CloseClipboard()
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _native_registered_image_payloads() -> list[tuple[str, bytes]] | None:
        if os.name != "nt":
            return None
        try:
            user32, kernel32 = ClipboardService._windows_clipboard_apis()
            if not user32.OpenClipboard(None):
                return None
            try:
                descriptors = ClipboardService._registered_image_descriptors_locked(user32, kernel32)
                results = []
                for name, handle, size in descriptors:
                    payload = ClipboardService._copy_clipboard_payload_locked(kernel32, handle, size)
                    ClipboardService._validate_registered_image_header(name, size, payload[:32])
                    results.append((name, payload))
                return results
            finally:
                user32.CloseClipboard()
        except ValueError:
            raise
        except (AttributeError, OSError, TypeError):
            return None

    @staticmethod
    def _registered_image_descriptors_locked(user32, kernel32) -> list[tuple[str, object, int]]:
        results = []
        format_id = 0
        while True:
            kernel32.SetLastError(0)
            format_id = int(user32.EnumClipboardFormats(format_id))
            if not format_id:
                if kernel32.GetLastError() != 0:
                    raise ValueError("Unable to enumerate clipboard formats")
                return results
            name_buffer = ctypes.create_unicode_buffer(128)
            if not user32.GetClipboardFormatNameW(format_id, name_buffer, len(name_buffer)):
                continue
            name = name_buffer.value
            if name not in ClipboardService.REGISTERED_IMAGE_FORMATS:
                continue
            handle = user32.GetClipboardData(format_id)
            if not handle:
                raise ValueError("Unable to inspect registered clipboard image data")
            size = int(kernel32.GlobalSize(handle))
            if size <= 0:
                raise ValueError("Invalid registered clipboard image data")
            if size > MAX_CLIPBOARD_IMAGE_BYTES:
                raise ValueError("Clipboard image payload is too large")
            results.append((name, handle, size))

    @staticmethod
    def _copy_clipboard_payload_locked(kernel32, handle, size: int) -> bytes:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ValueError("Unable to inspect clipboard image data")
        try:
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)

    @staticmethod
    def _native_clipboard_image_snapshot() -> tuple[str, bytes] | None:
        if os.name != "nt":
            return None
        try:
            user32, kernel32 = ClipboardService._windows_clipboard_apis()
            if not user32.OpenClipboard(None):
                return None
            try:
                registered = ClipboardService._registered_image_descriptors_locked(user32, kernel32)
                dibs = []
                for format_id, name in (
                    (ClipboardService.CF_DIBV5, "DIBV5"),
                    (ClipboardService.CF_DIB, "DIB"),
                ):
                    if not user32.IsClipboardFormatAvailable(format_id):
                        continue
                    handle = user32.GetClipboardData(format_id)
                    if not handle:
                        raise ValueError("Unable to inspect clipboard DIB data")
                    size = int(kernel32.GlobalSize(handle))
                    if size <= 0:
                        raise ValueError("Invalid clipboard DIB data")
                    if size > MAX_CLIPBOARD_IMAGE_BYTES:
                        raise ValueError("Clipboard image payload is too large")
                    dibs.append((name, handle, size))
                candidates = registered + dibs
                if not candidates:
                    return None
                name, handle, size = candidates[0]
                payload = ClipboardService._copy_clipboard_payload_locked(kernel32, handle, size)
                if name in ClipboardService.REGISTERED_IMAGE_FORMATS:
                    ClipboardService._validate_registered_image_header(name, size, payload[:32])
                else:
                    ClipboardService._dib_as_bmp(name, payload)
                return name, payload
            finally:
                user32.CloseClipboard()
        except ValueError:
            raise
        except (AttributeError, OSError, TypeError):
            return None

    @staticmethod
    def _native_clipboard_text_snapshot() -> str | None:
        if os.name != "nt":
            return None
        try:
            user32, kernel32 = ClipboardService._windows_clipboard_apis()
            if not user32.OpenClipboard(None):
                return None
            try:
                if not user32.IsClipboardFormatAvailable(ClipboardService.CF_UNICODETEXT):
                    return None
                handle = user32.GetClipboardData(ClipboardService.CF_UNICODETEXT)
                if not handle:
                    raise ValueError("Unable to inspect clipboard text data")
                size = int(kernel32.GlobalSize(handle))
                if size <= 0 or size % 2:
                    raise ValueError("Invalid clipboard Unicode text data")
                if size > MAX_CLIPBOARD_TEXT_BYTES:
                    raise ValueError("剪贴板文字过大，已拒绝读取。")
                payload = ClipboardService._copy_clipboard_payload_locked(kernel32, handle, size)
                terminator = next(
                    (offset for offset in range(0, len(payload), 2) if payload[offset:offset + 2] == b"\x00\x00"),
                    None,
                )
                if terminator is None:
                    raise ValueError("Invalid clipboard Unicode text data")
                text = payload[:terminator].decode("utf-16-le")
                if len(text.encode("utf-8")) > MAX_CLIPBOARD_TEXT_BYTES:
                    raise ValueError("剪贴板文字过大，已拒绝读取。")
                return text
            finally:
                user32.CloseClipboard()
        except ValueError:
            raise
        except (AttributeError, OSError, TypeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _validate_registered_image_header(name: str, size: int, header: bytes) -> None:
        if size > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("Clipboard image payload is too large")
        if name in {"PNG", "image/png"}:
            if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                raise ValueError("Invalid registered PNG clipboard data")
            width = int.from_bytes(header[16:20], "big")
            height = int.from_bytes(header[20:24], "big")
            pixels = width * height
            if width <= 0 or height <= 0:
                raise ValueError("Invalid clipboard image dimensions")
            if pixels > MAX_IMAGE_PIXELS or pixels * 4 > MAX_CLIPBOARD_IMAGE_BYTES:
                raise ValueError("Clipboard image dimensions are too large")

    @staticmethod
    def _dib_as_bmp(name: str, payload: bytes) -> bytes:
        if len(payload) < 12:
            raise ValueError("Invalid clipboard DIB data")
        header_size = int.from_bytes(payload[:4], "little")
        if name == "DIBV5" and header_size != 124:
            raise ValueError("Invalid clipboard DIBV5 header")
        if header_size == 12:
            width = int.from_bytes(payload[4:6], "little")
            height = int.from_bytes(payload[6:8], "little")
            planes = int.from_bytes(payload[8:10], "little")
            bits_per_pixel = int.from_bytes(payload[10:12], "little")
            compression = 0
            palette_entry_size = 3
            colors_used = 1 << bits_per_pixel if bits_per_pixel <= 8 else 0
            masks_size = 0
        elif header_size in {40, 52, 56, 108, 124} and len(payload) >= header_size:
            width = int.from_bytes(payload[4:8], "little", signed=True)
            height = abs(int.from_bytes(payload[8:12], "little", signed=True))
            planes = int.from_bytes(payload[12:14], "little")
            bits_per_pixel = int.from_bytes(payload[14:16], "little")
            compression = int.from_bytes(payload[16:20], "little")
            colors_used = int.from_bytes(payload[32:36], "little")
            palette_entry_size = 4
            masks_size = 12 if header_size == 40 and compression == 3 else 0
            if header_size == 40 and compression == 6:
                masks_size = 16
            if not colors_used and bits_per_pixel <= 8:
                colors_used = 1 << bits_per_pixel
        else:
            raise ValueError("Unsupported clipboard DIB header")
        if width <= 0 or height <= 0 or planes != 1:
            raise ValueError("Invalid clipboard image dimensions")
        if bits_per_pixel not in {1, 4, 8, 16, 24, 32}:
            raise ValueError("Unsupported clipboard DIB bit depth")
        if compression not in {0, 3, 6}:
            raise ValueError("Unsupported clipboard DIB compression")
        if compression in {3, 6} and bits_per_pixel not in {16, 32}:
            raise ValueError("Invalid clipboard DIB bitfields")
        if compression == 6 and header_size == 52:
            raise ValueError("Invalid clipboard DIB alpha bitfields")
        pixels = width * height
        if pixels > MAX_IMAGE_PIXELS or pixels * 4 > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("Clipboard image dimensions are too large")
        pixel_offset = header_size + masks_size + colors_used * palette_entry_size
        row_bytes = ((width * bits_per_pixel + 31) // 32) * 4
        if pixel_offset > len(payload) or row_bytes * height > len(payload) - pixel_offset:
            raise ValueError("Truncated clipboard DIB data")
        file_size = len(payload) + 14
        bitmap_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset + 14)
        return bitmap_header + payload

    @staticmethod
    def _decode_native_clipboard_image(name: str, payload: bytes) -> QImage:
        if name in ClipboardService.REGISTERED_IMAGE_FORMATS:
            ClipboardService._validate_registered_image_header(name, len(payload), payload[:32])
            image = QImage.fromData(payload, "PNG")
        elif name in {"DIB", "DIBV5"}:
            image = QImage.fromData(ClipboardService._dib_as_bmp(name, payload), "BMP")
        else:
            raise ValueError("Unsupported native clipboard image format")
        if image.isNull():
            raise ValueError("Invalid native clipboard image data")
        ClipboardService._validate_image(image)
        return image

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

    def _reject_oversized_native_clipboard(self, kind: str) -> list[tuple[str, bytes]] | None:
        if kind == "text":
            self._native_clipboard_text_snapshot()
            return
        snapshot = self._native_clipboard_image_snapshot()
        if snapshot is None and os.name == "nt":
            raise ValueError("Unknown native clipboard image source")
        return [snapshot] if snapshot is not None else None

    def poll(self) -> None:
        try:
            snapshot = self._read_stable_snapshot()
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
        except Exception as exc:
            self.failed.emit(str(exc))

    def _enqueue_task(self, kind: str, value: QImage | str, sequence: int | None) -> None:
        if not self._accepting_tasks:
            return
        if sequence is not None:
            if sequence in self._pending_sequences:
                return
        elif self._pending_without_sequence:
            return
        estimated_bytes = (
            max(value.sizeInBytes(), value.width() * value.height() * 4)
            if kind == "image"
            else len(value.encode("utf-8"))
        )
        if self._pending_bytes + estimated_bytes > self.PERSISTENCE_MEMORY_BUDGET:
            self.failed.emit("剪贴板保存队列占用内存过高；如果内容仍在剪贴板中，ClipSave 会稍后重试。")
            return
        task = _ClipboardTask(self._next_task_token, kind, value, sequence, estimated_bytes)
        self._next_task_token += 1
        with self._persistence_state_lock:
            self._idle_event.clear()
            try:
                self._tasks.put_nowait(task)
            except queue.Full:
                if self._tasks.unfinished_tasks == 0:
                    self._idle_event.set()
                self.failed.emit("剪贴板保存队列已满；如果内容仍在剪贴板中，ClipSave 会稍后重试。")
                return
        if sequence is None:
            self._pending_without_sequence = True
        else:
            self._pending_sequences.add(sequence)
        self._pending_bytes += estimated_bytes

    def _persistence_loop(self) -> None:
        while True:
            try:
                task = self._tasks.get(timeout=self.WAIT_INTERVAL_SECONDS)
            except queue.Empty:
                with self._worker_lifecycle_lock:
                    if self._worker_stop_requested:
                        self._worker_exited.set()
                        return
                continue
            try:
                if task.kind == "image":
                    key = self.image_key(task.value)
                    if key != self.last_image_key:
                        self.save_image(task.value)
                    result = key
                else:
                    self.save_text(task.value)
                    result = task.value
                self._persistence_succeeded.emit(task, result)
            except Exception as exc:
                self._persistence_failed.emit(task, str(exc))
            finally:
                with self._persistence_state_lock:
                    self._tasks.task_done()
                    if self._tasks.unfinished_tasks == 0:
                        self._idle_event.set()

    def _release_pending(self, task: _ClipboardTask) -> None:
        self._pending_bytes = max(0, self._pending_bytes - task.estimated_bytes)
        if task.sequence is None:
            self._pending_without_sequence = False
        else:
            self._pending_sequences.discard(task.sequence)

    def _finish_task(self, task: _ClipboardTask, result) -> None:
        self._release_pending(task)
        if task.kind == "image":
            self.last_image_key = result
        else:
            self.last_text = result
        if task.sequence is not None:
            self.last_clipboard_sequence = task.sequence

    def _fail_task(self, task: _ClipboardTask, message: str) -> None:
        self._release_pending(task)
        self.failed.emit(message)

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        app = QApplication.instance()
        while True:
            remaining = deadline - time.monotonic()
            if app is not None and remaining >= 0.001:
                event_budget_ms = min(self.PROCESS_EVENTS_MAX_MS, max(1, int(remaining * 1000)))
                app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, event_budget_ms)
            if self._idle_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._idle_event.wait(min(remaining, self.WAIT_INTERVAL_SECONDS))

    def shutdown(self, timeout: float = 10.0) -> bool:
        if self._shutdown_complete:
            return True
        if not self._worker.is_alive():
            self._shutdown_complete = True
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        self.prepare_for_shutdown()
        if not self.wait_for_idle(max(0.0, deadline - time.monotonic())):
            return False
        with self._worker_lifecycle_lock:
            self._worker_stop_requested = True
        while self._worker.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._worker.join(min(remaining, self.WAIT_INTERVAL_SECONDS))
        self._shutdown_complete = not self._worker.is_alive()
        return self._shutdown_complete

    def prepare_for_shutdown(self) -> None:
        """Immediately pause monitoring and reject newly queued persistence work."""
        self.stop()
        self._accepting_tasks = False

    def resume_after_failed_shutdown(self, restart_monitoring: bool) -> None:
        if self._shutdown_complete:
            return
        with self._worker_lifecycle_lock:
            self._worker_stop_requested = False
            if self._worker_exited.is_set() or not self._worker.is_alive():
                self._worker_exited = threading.Event()
                self._worker = threading.Thread(
                    target=self._persistence_loop,
                    name="ClipSavePersistence",
                    daemon=True,
                )
                self._worker.start()
        self._accepting_tasks = True
        if restart_monitoring and not self.timer.isActive():
            self._start_monitoring()
            QTimer.singleShot(0, self.poll)

    def _read_stable_snapshot(self) -> tuple[str, QImage | str, int | None] | None:
        for _attempt in range(self.CLIPBOARD_READ_ATTEMPTS):
            sequence_before = self.clipboard_sequence()
            if sequence_before is not None and sequence_before == self.last_clipboard_sequence:
                return None
            clipboard = self._clipboard()
            mime = clipboard.mimeData()
            if mime.hasImage():
                snapshot: tuple[str, QImage | str] | None = (
                    "image",
                    self._snapshot_clipboard_image(clipboard),
                )
            elif mime.hasText():
                snapshot = ("text", self._snapshot_clipboard_text(clipboard))
            else:
                snapshot = None
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

    @staticmethod
    def _remove_new_image(
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
                    PICTURE_DIR,
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
        now = dt.datetime.now()
        folder = PICTURE_DIR / f"{now:%Y-%m-%d}"
        validate_managed_write_path(folder / "capture.tmp", PICTURE_DIR)
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
            with open_managed_binary(path, "xb", PICTURE_DIR) as handle:
                handle.write(payload)
            payload_hash = hashlib.sha256(payload).hexdigest()
            with open_managed_binary(
                path, "rb", PICTURE_DIR, identity_locked=True
            ) as owned_file:
                owned_hash = hashlib.sha256()
                owned_size = 0
                while chunk := owned_file.read(1024 * 1024):
                    owned_hash.update(chunk)
                    owned_size += len(chunk)
                if owned_size != len(payload) or owned_hash.hexdigest() != payload_hash:
                    raise RuntimeError("Captured image changed before it could be indexed")
                item_id = self.database.add_image(path, now)
                if item_id:
                    self.captured.emit(item_id)
                    return True
                owner = self._database_image_owner(payload_hash)
                if owner is not None and owner["resolved_path"] == self.database._path_key(path):
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
            return True
        raise RuntimeError("图片文件已写入，但数据库未能保存该记录。")

    def save_text(self, text: str) -> bool:
        byte_size = len(text.encode("utf-8"))
        if byte_size > MAX_CLIPBOARD_TEXT_BYTES:
            raise ValueError(f"剪贴板文字超过 {MAX_CLIPBOARD_TEXT_BYTES // (1024 * 1024)} MiB，已拒绝保存。")
        now = dt.datetime.now()
        item_id = self.database.add_text(text, now)
        if not item_id:
            return True
        daily = MARKDOWN_DIR / f"clipboard_{now:%Y-%m-%d}.md"
        warning = ""
        try:
            entry = f"\n\n---\n\n**{now:%H:%M:%S}**\n\n{text}\n".encode("utf-8")
            try:
                with open_managed_binary(daily, "xb", MARKDOWN_DIR) as handle:
                    handle.write(f"# ClipSave {now:%Y-%m-%d}\n".encode("utf-8"))
                    handle.write(entry)
            except FileExistsError:
                with open_managed_binary(daily, "ab", MARKDOWN_DIR) as handle:
                    handle.write(entry)
        except (OSError, RuntimeError, UnicodeError) as exc:
            warning = f"文字已保存到数据库，但写入每日 Markdown 失败: {exc}"
        self.captured.emit(item_id)
        if warning:
            self.failed.emit(warning)
        return True

    def suppress_text(self, text: str) -> None:
        self.last_text = text
        self.last_clipboard_sequence = self.clipboard_sequence()

    def suppress_image(self, image: QImage) -> None:
        self.last_image_key = self.image_key(image)
        self.last_clipboard_sequence = self.clipboard_sequence()


class AIService:
    def __init__(self, base_url: str, api_key: str, vision_model: str, embedding_model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.vision_model = vision_model
        self.embedding_model = embedding_model

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.vision_model)

    def _post(self, path: str, payload: dict, cancel_event: threading.Event | None = None) -> dict:
        _raise_if_cancelled(cancel_event)
        deadline = time.monotonic() + 10.0
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                chunks: list[bytes] = []
                remaining = MAX_AI_RESPONSE_BYTES + 1
                while remaining:
                    _raise_if_cancelled(cancel_event)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("AI service request timed out")
                    chunk = response.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                if len(raw) > MAX_AI_RESPONSE_BYTES:
                    raise RuntimeError("AI 服务响应过大，已停止读取。")
        except urllib.error.HTTPError as exc:
            detail = exc.read(301).decode("utf-8", errors="replace")
            raise RuntimeError(f"AI 服务返回 {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("AI 服务连接超时或不可用。") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("AI 服务返回了无效的 JSON。") from exc
        if not isinstance(result, dict):
            raise RuntimeError("AI 服务响应结构无效。")
        return result

    @staticmethod
    def _description_from_response(result: dict) -> str:
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeError("AI 服务响应缺少 choices。")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise RuntimeError("AI 服务响应缺少文字内容。")
        content = message["content"].strip()
        if not content:
            raise RuntimeError("AI 服务返回了空描述。")
        return content

    @staticmethod
    def _embedding_from_response(result: dict) -> list[float]:
        data = result.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError("AI 服务响应缺少 embedding 数据。")
        embedding = data[0].get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError("AI 服务返回了无效的 embedding。")
        if len(embedding) > MAX_EMBEDDING_DIMENSIONS:
            raise RuntimeError("AI 服务返回的 embedding 维度过大。")
        values: list[float] = []
        for value in embedding:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError("AI 服务返回了非数字 embedding。")
            number = float(value)
            if not math.isfinite(number):
                raise RuntimeError("AI 服务返回了非有限 embedding。")
            values.append(number)
        return values

    def describe_image(self, path: Path, cancel_event: threading.Event | None = None) -> str:
        _raise_if_cancelled(cancel_event)
        snapshot = preflight_image_file(path)
        with Image.open(snapshot.path) as image:
            snapshot.require_current()
            image.thumbnail((1024, 1024))
            with io.BytesIO() as stream:
                image.convert("RGB").save(stream, "JPEG", quality=82)
                encoded = base64.b64encode(stream.getvalue()).decode("ascii")
        _raise_if_cancelled(cancel_event)
        result = self._post(
            "/chat/completions",
            {
                "model": self.vision_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用简体中文简洁描述这张图片，包含主体、场景、可见文字和适合搜索的关键词。只输出描述。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                    ],
                }],
                "temperature": 0.1,
            },
            cancel_event,
        )
        _raise_if_cancelled(cancel_event)
        return self._description_from_response(result)

    def embed(self, text: str, cancel_event: threading.Event | None = None) -> list[float] | None:
        if not self.embedding_model:
            return None
        _raise_if_cancelled(cancel_event)
        result = self._post("/embeddings", {"model": self.embedding_model, "input": text}, cancel_event)
        _raise_if_cancelled(cancel_event)
        return self._embedding_from_response(result)

    @staticmethod
    def similarity(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return -1.0
        if not all(math.isfinite(value) for value in left) or not all(math.isfinite(value) for value in right):
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
        return dot / norm if norm else -1.0


def apply_windows_acrylic(window) -> bool:
    if os.name != "nt":
        return False
    hwnd = int(window.winId())
    try:
        # Windows 11 exposes a native transient backdrop attribute.
        build = sys.getwindowsversion().build
        if build >= 22000:
            backdrop = ctypes.c_int(3)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        else:
            # Blur-behind avoids the severe resize/drag stalls caused by Win10 acrylic.
            class AccentPolicy(ctypes.Structure):
                _fields_ = [
                    ("accent_state", ctypes.c_int),
                    ("accent_flags", ctypes.c_int),
                    ("gradient_color", ctypes.c_uint32),
                    ("animation_id", ctypes.c_int),
                ]

            class WindowCompositionAttributeData(ctypes.Structure):
                _fields_ = [
                    ("attribute", ctypes.c_int),
                    ("data", ctypes.c_void_p),
                    ("size", ctypes.c_size_t),
                ]

            # The sidebar and toolbar provide their own tint. Keeping the native
            # layer untinted avoids stacking two white alpha layers.
            policy = AccentPolicy(3, 0, 0x00FFFFFF, 0)
            data = WindowCompositionAttributeData(19, ctypes.addressof(policy), ctypes.sizeof(policy))
            ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        corner = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        dark = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        return True
    except (AttributeError, OSError):
        return False
