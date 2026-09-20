from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QPixmap

from .item_models import normalized_thumbnail_path


THUMBNAIL_SIZE = QSize(360, 180)
THUMBNAIL_CACHE_LIMIT = 256


@dataclass(frozen=True, slots=True)
class ThumbnailCacheKey:
    path: str
    modified_ns: int
    size: int
    changed_ns: int = 0
    content_hash: str = ""


def thumbnail_cache_key(
    path: Path | str,
    content_hash: str | None = None,
) -> ThumbnailCacheKey | None:
    normalized = normalized_thumbnail_path(path)
    if normalized is None:
        return None
    try:
        stat = os.stat(normalized)
    except OSError:
        return None
    return ThumbnailCacheKey(
        normalized,
        stat.st_mtime_ns,
        stat.st_size,
        getattr(stat, "st_ctime_ns", 0),
        content_hash or "",
    )


class ThumbnailPixmapCache(OrderedDict):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = max(1, limit)
        self._keys_by_path: dict[str, ThumbnailCacheKey] = {}

    def lookup(self, key: ThumbnailCacheKey) -> QPixmap | None:
        previous = self._keys_by_path.get(key.path)
        if previous is not None and previous != key:
            self.discard_key(previous)
        try:
            pixmap = OrderedDict.__getitem__(self, key)
        except KeyError:
            return None
        self._keys_by_path[key.path] = key
        OrderedDict.move_to_end(self, key)
        return pixmap

    def __setitem__(self, key, pixmap) -> None:
        if isinstance(key, ThumbnailCacheKey):
            previous = self._keys_by_path.get(key.path)
            if previous is not None and previous != key:
                self.discard_key(previous)
            self._keys_by_path[key.path] = key
        if OrderedDict.__contains__(self, key):
            OrderedDict.__delitem__(self, key)
        OrderedDict.__setitem__(self, key, pixmap)
        while len(self) > self.limit:
            evicted, _pixmap = OrderedDict.popitem(self, last=False)
            if (
                isinstance(evicted, ThumbnailCacheKey)
                and self._keys_by_path.get(evicted.path) == evicted
            ):
                self._keys_by_path.pop(evicted.path, None)

    def discard_key(self, key: ThumbnailCacheKey) -> None:
        OrderedDict.pop(self, key, None)
        if self._keys_by_path.get(key.path) == key:
            self._keys_by_path.pop(key.path, None)

    def invalidate_path(self, path: Path | str) -> None:
        normalized = normalized_thumbnail_path(path)
        if normalized is None:
            return
        key = self._keys_by_path.pop(normalized, None)
        if key is not None:
            OrderedDict.pop(self, key, None)

    def clear(self) -> None:
        OrderedDict.clear(self)
        self._keys_by_path.clear()


THUMBNAIL_CACHE = ThumbnailPixmapCache(THUMBNAIL_CACHE_LIMIT)


def cached_thumbnail(
    path: Path | str,
    content_hash: str | None = None,
) -> tuple[ThumbnailCacheKey | None, QPixmap | None, bool]:
    key = thumbnail_cache_key(path, content_hash)
    if key is None:
        THUMBNAIL_CACHE.invalidate_path(path)
        return None, None, False
    pixmap = THUMBNAIL_CACHE.lookup(key)
    return key, pixmap, pixmap is not None


def thumbnail_pixmap(path: Path, content_hash: str | None = None) -> QPixmap:
    _key, pixmap, cached = cached_thumbnail(path, content_hash)
    if cached:
        return pixmap
    return QPixmap()


def cache_decoded_thumbnail(
    key: ThumbnailCacheKey,
    image: QImage,
) -> QPixmap | None:
    if thumbnail_cache_key(key.path, key.content_hash) != key:
        THUMBNAIL_CACHE.discard_key(key)
        return None
    if image.isNull():
        THUMBNAIL_CACHE.discard_key(key)
        return None
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        THUMBNAIL_CACHE.discard_key(key)
        return None
    THUMBNAIL_CACHE[key] = pixmap
    return pixmap
