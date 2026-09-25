from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImage

from .constants import (
    MAX_CLIPBOARD_IMAGE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
    MAX_IMAGE_PIXELS,
)
from .database import LibraryDatabase
from .storage import delete_managed_file, validate_managed_write_path


@dataclass(frozen=True, slots=True)
class CaptureStoreResult:
    item_id: int | None = None
    warning: str = ""
    created: bool = True


def validate_clipboard_image(image: QImage) -> None:
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        raise ValueError("剪贴板图片无效，已拒绝保存。")
    pixels = image.width() * image.height()
    if pixels > MAX_IMAGE_PIXELS:
        raise ValueError("图片尺寸过大，已拒绝保存。")
    normalized_bytes = pixels * 4
    if (
        normalized_bytes > MAX_CLIPBOARD_IMAGE_BYTES
        or image.sizeInBytes() > MAX_CLIPBOARD_IMAGE_BYTES
    ):
        raise ValueError("图片占用内存过大，已拒绝保存。")


class ClipboardCaptureStore:
    """Own durable clipboard capture writes and database indexing."""

    def __init__(
        self,
        database: LibraryDatabase,
        *,
        picture_dir: Callable[[], Path],
        markdown_dir: Callable[[], Path],
        open_managed_binary: Callable[..., object],
    ) -> None:
        self.database = database
        self._picture_dir = picture_dir
        self._markdown_dir = markdown_dir
        self._open_managed_binary = open_managed_binary

    @property
    def picture_dir(self) -> Path:
        return self._picture_dir()

    @property
    def markdown_dir(self) -> Path:
        return self._markdown_dir()

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

    def save_image(self, image: QImage) -> CaptureStoreResult:
        validate_clipboard_image(image)
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
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(
                buffer,
                "PNG",
            ):
                raise OSError(f"无法保存图片: {path}")
            buffer.close()
            payload = bytes(encoded)
            if len(payload) > MAX_CLIPBOARD_IMAGE_BYTES:
                raise ValueError("图片 PNG 数据过大，已拒绝保存。")
            with self._open_managed_binary(path, "xb", self.picture_dir) as handle:
                handle.write(payload)
            payload_hash = hashlib.sha256(payload).hexdigest()
            with self._open_managed_binary(
                path,
                "rb",
                self.picture_dir,
                identity_locked=True,
            ) as owned_file:
                owned_hash = hashlib.sha256()
                owned_size = 0
                while chunk := owned_file.read(1024 * 1024):
                    owned_hash.update(chunk)
                    owned_size += len(chunk)
                if owned_size != len(payload) or owned_hash.hexdigest() != payload_hash:
                    raise RuntimeError(
                        "Captured image changed before it could be indexed"
                    )
                item_id = self.database.add_verified_image(
                    path,
                    content_hash=payload_hash,
                    file_size=owned_size,
                    width=image.width(),
                    height=image.height(),
                    created_at=now,
                )
                if item_id:
                    return CaptureStoreResult(item_id=int(item_id))
                owner = self.database.indexed_file_for_hash(payload_hash)
                if (
                    owner is not None
                    and owner["resolved_path"] == self.database.path_key(path)
                ):
                    self.database.touch_item_used(int(owner["id"]), now)
                    return CaptureStoreResult(item_id=int(owner["id"]))
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
            self.database.touch_item_used(int(owner["id"]), now)
            return CaptureStoreResult(item_id=int(owner["id"]), created=False)
        raise RuntimeError("图片文件已写入，但数据库未能保存该记录。")

    def save_text(self, text: str) -> CaptureStoreResult:
        byte_size = len(text.encode("utf-8"))
        if byte_size > MAX_CLIPBOARD_TEXT_BYTES:
            raise ValueError(
                f"剪贴板文字超过 {MAX_CLIPBOARD_TEXT_BYTES // (1024 * 1024)} MiB，已拒绝保存。"
            )
        now = dt.datetime.now().astimezone()
        item_id = self.database.add_text(text, now)
        if not item_id:
            existing_id = self.database.touch_text_by_hash(text, now)
            return CaptureStoreResult(item_id=existing_id, created=False)
        daily = self.markdown_dir / f"clipboard_{now:%Y-%m-%d}.md"
        warning = ""
        try:
            entry = f"\n\n---\n\n**{now:%H:%M:%S}**\n\n{text}\n".encode("utf-8")
            try:
                with self._open_managed_binary(
                    daily,
                    "xb",
                    self.markdown_dir,
                ) as handle:
                    handle.write(f"# ClipSave {now:%Y-%m-%d}\n".encode("utf-8"))
                    handle.write(entry)
            except FileExistsError:
                with self._open_managed_binary(
                    daily,
                    "ab",
                    self.markdown_dir,
                ) as handle:
                    handle.write(entry)
        except (OSError, RuntimeError, UnicodeError) as exc:
            warning = f"文字已保存到数据库，但写入每日 Markdown 失败: {exc}"
        return CaptureStoreResult(item_id=int(item_id), warning=warning)
