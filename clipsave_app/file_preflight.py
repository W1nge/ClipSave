from __future__ import annotations

import stat as stat_module
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .constants import MAX_IMAGE_PIXELS


class OperationCancelled(RuntimeError):
    pass


def raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Operation cancelled")


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


def preflight_current_file(
    path: Path,
    *,
    max_file_bytes: int | None = None,
) -> FileSnapshot:
    resolved = Path(path).resolve()
    try:
        stat_result = resolved.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"文件不存在或已被移动: {resolved}") from exc
    except OSError as exc:
        raise OSError(f"无法访问文件: {resolved}") from exc
    if not stat_module.S_ISREG(stat_result.st_mode):
        raise ValueError(f"路径不是文件: {resolved}")
    if max_file_bytes is not None and stat_result.st_size > max_file_bytes:
        raise ValueError("文件过大，已拒绝处理。")
    return FileSnapshot(
        resolved,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_dev,
        stat_result.st_ino,
    )


def preflight_image_file(
    path: Path,
    *,
    max_pixels: int = MAX_IMAGE_PIXELS,
    max_file_bytes: int | None = None,
) -> ImageFileSnapshot:
    snapshot = preflight_current_file(path, max_file_bytes=max_file_bytes)
    try:
        image_context = Image.open(snapshot.path)
    except (OSError, ValueError) as exc:
        raise ValueError("图片文件无法读取或格式无效。") from exc
    with image_context as image:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸无效，已拒绝处理。")
        if width * height > max_pixels:
            raise ValueError("图片尺寸过大，已拒绝处理。")
        try:
            image.load()
        except (OSError, ValueError) as exc:
            raise ValueError("图片文件无法读取或格式无效。") from exc
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
