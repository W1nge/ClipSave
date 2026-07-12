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
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QTimer, Signal
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


class OperationCancelled(RuntimeError):
    pass


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Operation cancelled")


@dataclass(frozen=True)
class _ClipboardTask:
    token: int
    kind: str
    value: QImage | str
    sequence: int | None
    estimated_bytes: int


class ClipboardService(QObject):
    CLIPBOARD_READ_ATTEMPTS = 3
    PERSISTENCE_QUEUE_SIZE = 4
    PERSISTENCE_MEMORY_BUDGET = MAX_CLIPBOARD_IMAGE_BYTES + MAX_CLIPBOARD_TEXT_BYTES

    captured = Signal(int)
    failed = Signal(str)
    state_changed = Signal(bool)
    _persistence_succeeded = Signal(object, object)
    _persistence_failed = Signal(object, str)

    def __init__(self, database: LibraryDatabase, parent=None):
        super().__init__(parent)
        self.database = database
        self.timer = QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self.poll)
        self.last_text = ""
        self.last_image_key = ""
        self.last_clipboard_sequence: int | None = None
        self._tasks: queue.Queue[_ClipboardTask | None] = queue.Queue(self.PERSISTENCE_QUEUE_SIZE)
        self._pending_sequences: set[int] = set()
        self._pending_without_sequence = False
        self._pending_bytes = 0
        self._next_task_token = 1
        self._accepting_tasks = True
        self._shutdown_complete = False
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._worker = threading.Thread(target=self._persistence_loop, name="ClipSavePersistence", daemon=True)
        self._persistence_succeeded.connect(self._finish_task)
        self._persistence_failed.connect(self._fail_task)
        self._worker.start()

    def start(self) -> None:
        clipboard = QApplication.clipboard()
        self.last_text = clipboard.text()
        self.last_clipboard_sequence = self.clipboard_sequence()
        if self.last_clipboard_sequence is None:
            image = clipboard.image()
            try:
                self.last_image_key = self.image_key(image) if not image.isNull() else ""
            except ValueError as exc:
                self.last_image_key = ""
                self.failed.emit(str(exc))
        self.timer.start()
        self.state_changed.emit(True)

    def stop(self) -> None:
        self.timer.stop()
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
            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except (AttributeError, OSError):
            return None

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
        try:
            self._tasks.put_nowait(task)
        except queue.Full:
            self.failed.emit("剪贴板保存队列已满；如果内容仍在剪贴板中，ClipSave 会稍后重试。")
            return
        self._idle_event.clear()
        if sequence is None:
            self._pending_without_sequence = True
        else:
            self._pending_sequences.add(sequence)
        self._pending_bytes += estimated_bytes

    def _persistence_loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                self._tasks.task_done()
                self._idle_event.set()
                return
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
                self._tasks.task_done()
                if self._tasks.empty():
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
        if task.sequence is not None and (
            self.last_clipboard_sequence is None or task.sequence >= self.last_clipboard_sequence
        ):
            self.last_clipboard_sequence = task.sequence

    def _fail_task(self, task: _ClipboardTask, message: str) -> None:
        self._release_pending(task)
        self.failed.emit(message)

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        app = QApplication.instance()
        while time.monotonic() < deadline:
            if app is not None:
                app.processEvents()
            if self._idle_event.wait(0.01):
                if app is not None:
                    app.processEvents()
                return True
        return False

    def shutdown(self, timeout: float = 10.0) -> bool:
        if self._shutdown_complete:
            return True
        self.stop()
        self._accepting_tasks = False
        if not self.wait_for_idle(timeout):
            return False
        try:
            self._tasks.put_nowait(None)
        except queue.Full:
            return False
        self._worker.join(timeout)
        self._shutdown_complete = not self._worker.is_alive()
        return self._shutdown_complete

    def resume_after_failed_shutdown(self, restart_monitoring: bool) -> None:
        if self._shutdown_complete:
            return
        self._accepting_tasks = True
        if restart_monitoring and not self.timer.isActive():
            self.timer.start()
            self.state_changed.emit(True)

    def _read_stable_snapshot(self) -> tuple[str, QImage | str, int | None] | None:
        for _attempt in range(self.CLIPBOARD_READ_ATTEMPTS):
            sequence_before = self.clipboard_sequence()
            if sequence_before is not None and sequence_before == self.last_clipboard_sequence:
                return None
            clipboard = self._clipboard()
            mime = clipboard.mimeData()
            if mime.hasImage():
                snapshot: tuple[str, QImage | str] | None = ("image", clipboard.image())
            elif mime.hasText():
                snapshot = ("text", clipboard.text())
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
    def _remove_new_image(path: Path, original_error: BaseException | None = None) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            if original_error is not None:
                raise RuntimeError(f"保存图片失败，且无法清理临时文件: {path}") from cleanup_error
            raise OSError(f"无法清理重复图片文件: {path}") from cleanup_error

    def _database_has_image(self, path: Path) -> bool:
        digest = self.database.file_hash(path)
        return self.database.has_content_hash(digest)

    def save_image(self, image: QImage) -> bool:
        self._validate_image(image)
        now = dt.datetime.now()
        folder = PICTURE_DIR / f"{now:%Y-%m-%d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"image_{now:%Y%m%d_%H%M%S_%f}.png"
        try:
            if not image.save(str(path), "PNG"):
                raise OSError(f"无法保存图片: {path}")
            item_id = self.database.add_image(path, now)
        except BaseException as exc:
            self._remove_new_image(path, exc)
            raise
        if not item_id:
            try:
                duplicate = self._database_has_image(path)
            except BaseException as exc:
                self._remove_new_image(path, exc)
                raise
            self._remove_new_image(path)
            if duplicate:
                return True
            raise RuntimeError("图片文件已写入，但数据库未能保存该记录。")
        self.captured.emit(item_id)
        return True

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
            if not daily.exists():
                daily.write_text(f"# ClipSave {now:%Y-%m-%d}\n", encoding="utf-8")
            with daily.open("a", encoding="utf-8") as handle:
                handle.write(f"\n\n---\n\n**{now:%H:%M:%S}**\n\n{text}\n")
        except (OSError, UnicodeError) as exc:
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
            with urllib.request.urlopen(request, timeout=90) as response:
                chunks: list[bytes] = []
                remaining = MAX_AI_RESPONSE_BYTES + 1
                while remaining:
                    _raise_if_cancelled(cancel_event)
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
        with Image.open(path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片尺寸过大，已拒绝发送到 AI 服务。")
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
