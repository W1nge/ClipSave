from __future__ import annotations

import base64
import ctypes
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from .constants import MARKDOWN_DIR, MAX_IMAGE_PIXELS, PICTURE_DIR
from .database import LibraryDatabase


_TRANSPARENT_SMALL_ICON = None


class ClipboardService(QObject):
    captured = Signal(int)
    failed = Signal(str)
    state_changed = Signal(bool)

    def __init__(self, database: LibraryDatabase, parent=None):
        super().__init__(parent)
        self.database = database
        self.timer = QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self.poll)
        self.last_text = ""
        self.last_image_key = ""

    def start(self) -> None:
        clipboard = QApplication.clipboard()
        self.last_text = clipboard.text()
        image = clipboard.image()
        self.last_image_key = self.image_key(image) if not image.isNull() else ""
        self.timer.start()
        self.state_changed.emit(True)

    def stop(self) -> None:
        self.timer.stop()
        self.state_changed.emit(False)

    def toggle(self) -> None:
        self.stop() if self.timer.isActive() else self.start()

    @staticmethod
    def image_key(image: QImage) -> str:
        return f"{image.cacheKey()}:{image.width()}:{image.height()}"

    def poll(self) -> None:
        try:
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime.hasImage():
                image = clipboard.image()
                key = self.image_key(image)
                if not image.isNull() and key != self.last_image_key:
                    self.last_image_key = key
                    self.save_image(image)
                    return
            if mime.hasText():
                text = clipboard.text().strip()
                if text and text != self.last_text:
                    self.last_text = text
                    self.save_text(text)
        except Exception as exc:
            self.failed.emit(str(exc))

    def save_image(self, image: QImage) -> None:
        if image.width() * image.height() > MAX_IMAGE_PIXELS:
            raise ValueError("图片尺寸过大，已拒绝保存。")
        now = dt.datetime.now()
        folder = PICTURE_DIR / f"{now:%Y-%m-%d}"
        folder.mkdir(exist_ok=True)
        path = folder / f"image_{now:%Y%m%d_%H%M%S_%f}.png"
        if not image.save(str(path), "PNG"):
            raise OSError(f"无法保存图片: {path}")
        item_id = self.database.add_image(path, now)
        if item_id:
            self.captured.emit(item_id)

    def save_text(self, text: str) -> None:
        now = dt.datetime.now()
        item_id = self.database.add_text(text, now)
        if not item_id:
            return
        daily = MARKDOWN_DIR / f"clipboard_{now:%Y-%m-%d}.md"
        if not daily.exists():
            daily.write_text(f"# ClipSave {now:%Y-%m-%d}\n", encoding="utf-8")
        with daily.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n---\n\n**{now:%H:%M:%S}**\n\n{text}\n")
        self.captured.emit(item_id)

    def suppress_text(self, text: str) -> None:
        self.last_text = text

    def suppress_image(self, image: QImage) -> None:
        self.last_image_key = self.image_key(image)


class AIService:
    def __init__(self, base_url: str, api_key: str, vision_model: str, embedding_model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.vision_model = vision_model
        self.embedding_model = embedding_model

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.vision_model)

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AI 服务返回 {exc.code}: {detail[:300]}") from exc

    def describe_image(self, path: Path) -> str:
        with Image.open(path) as image:
            image.thumbnail((1024, 1024))
            import io

            stream = io.BytesIO()
            image.convert("RGB").save(stream, "JPEG", quality=82)
        encoded = base64.b64encode(stream.getvalue()).decode("ascii")
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
        )
        return result["choices"][0]["message"]["content"].strip()

    def embed(self, text: str) -> list[float] | None:
        if not self.embedding_model:
            return None
        result = self._post("/embeddings", {"model": self.embedding_model, "input": text})
        return list(result["data"][0]["embedding"])

    @staticmethod
    def similarity(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
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
        # Keep native window controls and taskbar branding, but make the small
        # titlebar icon fully transparent. Passing NULL makes Windows show its
        # generic fallback icon instead.
        global _TRANSPARENT_SMALL_ICON
        if _TRANSPARENT_SMALL_ICON is None:
            mask = (ctypes.c_ubyte * 32)(*([0xFF] * 32))
            pixels = (ctypes.c_ubyte * 32)(*([0x00] * 32))
            create_icon = ctypes.windll.user32.CreateIcon
            create_icon.restype = ctypes.c_void_p
            _TRANSPARENT_SMALL_ICON = create_icon(None, 16, 16, 1, 1, mask, pixels)
        ctypes.windll.user32.SetWindowTextW(hwnd, "")
        ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, _TRANSPARENT_SMALL_ICON)
        ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 2, _TRANSPARENT_SMALL_ICON)
        return True
    except (AttributeError, OSError):
        return False
