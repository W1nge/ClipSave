from __future__ import annotations

import os
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QImageReader, QPixmap

from .item_models import normalized_thumbnail_path


_THUMBNAIL_SIZE = QSize(360, 180)
_THUMBNAIL_CACHE_LIMIT = 256
_THUMBNAIL_WORKERS = 2
_THUMBNAIL_REQUEST_LIMIT = 32


@dataclass(frozen=True, slots=True)
class _ThumbnailCacheKey:
    path: str
    modified_ns: int
    size: int
    changed_ns: int = 0
    content_hash: str = ""


def _thumbnail_cache_key(
    path: Path | str,
    content_hash: str | None = None,
) -> _ThumbnailCacheKey | None:
    normalized = normalized_thumbnail_path(path)
    if normalized is None:
        return None
    try:
        stat = os.stat(normalized)
    except OSError:
        return None
    return _ThumbnailCacheKey(
        normalized,
        stat.st_mtime_ns,
        stat.st_size,
        getattr(stat, "st_ctime_ns", 0),
        content_hash or "",
    )


class _ThumbnailPixmapCache(OrderedDict):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = max(1, limit)
        self._keys_by_path: dict[str, _ThumbnailCacheKey] = {}

    def lookup(self, key: _ThumbnailCacheKey) -> QPixmap | None:
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
        if isinstance(key, _ThumbnailCacheKey):
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
                isinstance(evicted, _ThumbnailCacheKey)
                and self._keys_by_path.get(evicted.path) == evicted
            ):
                self._keys_by_path.pop(evicted.path, None)

    def discard_key(self, key: _ThumbnailCacheKey) -> None:
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


_THUMBNAIL_CACHE = _ThumbnailPixmapCache(_THUMBNAIL_CACHE_LIMIT)


def _cached_thumbnail(
    path: Path | str,
    content_hash: str | None = None,
) -> tuple[_ThumbnailCacheKey | None, QPixmap | None, bool]:
    key = _thumbnail_cache_key(path, content_hash)
    if key is None:
        _THUMBNAIL_CACHE.invalidate_path(path)
        return None, None, False
    pixmap = _THUMBNAIL_CACHE.lookup(key)
    return key, pixmap, pixmap is not None


def thumbnail_pixmap(path: Path, content_hash: str | None = None) -> QPixmap:
    _key, pixmap, cached = _cached_thumbnail(path, content_hash)
    if cached:
        return pixmap
    return QPixmap()


def _decode_thumbnail_image(key: _ThumbnailCacheKey) -> QImage:
    reader = QImageReader(key.path)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        reader.setScaledSize(
            source_size.scaled(
                _THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        )
    image = reader.read()
    if not image.isNull() and (
        image.width() > _THUMBNAIL_SIZE.width()
        or image.height() > _THUMBNAIL_SIZE.height()
    ):
        image = image.scaled(
            _THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


def _cache_decoded_thumbnail(
    key: _ThumbnailCacheKey,
    image: QImage,
) -> QPixmap | None:
    if _thumbnail_cache_key(key.path, key.content_hash) != key:
        _THUMBNAIL_CACHE.discard_key(key)
        return None
    if image.isNull():
        _THUMBNAIL_CACHE.discard_key(key)
        return None
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        _THUMBNAIL_CACHE.discard_key(key)
        return None
    _THUMBNAIL_CACHE[key] = pixmap
    return pixmap


class _ThumbnailDecodeSignals(QObject):
    finished = Signal(int, object, int, object)


class _ThumbnailDecodeTask(QRunnable):
    def __init__(
        self,
        request_id: int,
        key: _ThumbnailCacheKey,
        generation: int,
        decode_image: Callable[[_ThumbnailCacheKey], QImage],
    ):
        super().__init__()
        self.request_id = request_id
        self.key = key
        self.generation = generation
        self.decode_image = decode_image
        self.signals = _ThumbnailDecodeSignals()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        if self.cancelled:
            self.signals.finished.emit(
                self.request_id,
                self.key,
                self.generation,
                QImage(),
            )
            return
        try:
            image = self.decode_image(self.key)
        except Exception:
            image = QImage()
        self.signals.finished.emit(
            self.request_id,
            self.key,
            self.generation,
            image,
        )


class ThumbnailDecodeQueue(QObject):
    decoded = Signal(object, object, int)
    capacity_available = Signal()

    def __init__(
        self,
        parent=None,
        *,
        max_workers: int = _THUMBNAIL_WORKERS,
        max_requests: int = _THUMBNAIL_REQUEST_LIMIT,
        decode_image: Callable[[_ThumbnailCacheKey], QImage] = _decode_thumbnail_image,
    ):
        super().__init__(parent)
        self.max_workers = max(1, max_workers)
        self.max_active = self.max_workers * 2
        self.max_requests = max(self.max_workers, max_requests)
        self.decode_image = decode_image
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(self.max_active)
        self._queued: deque[tuple[_ThumbnailCacheKey, int]] = deque()
        self._scheduled: set[tuple[int, _ThumbnailCacheKey]] = set()
        self._active: dict[
            int,
            tuple[_ThumbnailDecodeTask, tuple[int, _ThumbnailCacheKey]],
        ] = {}
        self._next_request_id = 1
        self._closed = False
        self._paused = False

    @property
    def pending_count(self) -> int:
        return len(self._scheduled)

    @property
    def queued_count(self) -> int:
        return len(self._queued)

    def request(self, key: _ThumbnailCacheKey, generation: int) -> bool:
        if self._closed or self._paused:
            return False
        token = (generation, key)
        if token in self._scheduled:
            return True
        if len(self._scheduled) >= self.max_requests:
            return False
        self._scheduled.add(token)
        self._queued.append((key, generation))
        self._pump()
        return True

    def cancel_queued(self) -> None:
        while self._queued:
            key, generation = self._queued.popleft()
            self._scheduled.discard((generation, key))
        for task, _token in self._active.values():
            task.cancel()

    def close(self, timeout_ms: int = 2000) -> bool:
        self._closed = True
        self._paused = True
        self.cancel_queued()
        self._pool.clear()
        return self._pool.waitForDone(timeout_ms)

    def pause_and_wait(self, timeout_ms: int = 2000) -> bool:
        self._paused = True
        self.cancel_queued()
        self._pool.clear()
        return self._pool.waitForDone(timeout_ms)

    def resume(self) -> None:
        if self._closed:
            return
        self._paused = False
        self._pump()
        self.capacity_available.emit()

    def _pump(self) -> None:
        active_current = sum(
            1
            for task, _token in self._active.values()
            if not task.cancelled
        )
        while (
            not self._closed
            and self._queued
            and active_current < self.max_workers
            and len(self._active) < self.max_active
        ):
            key, generation = self._queued.popleft()
            request_id = self._next_request_id
            self._next_request_id += 1
            token = (generation, key)
            task = _ThumbnailDecodeTask(
                request_id,
                key,
                generation,
                self.decode_image,
            )
            task.signals.finished.connect(
                self._job_finished,
                Qt.ConnectionType.QueuedConnection,
            )
            self._active[request_id] = (task, token)
            self._pool.start(task)
            active_current += 1

    @Slot(int, object, int, object)
    def _job_finished(
        self,
        request_id: int,
        key: _ThumbnailCacheKey,
        generation: int,
        image: QImage,
    ) -> None:
        active = self._active.pop(request_id, None)
        if active is None:
            return
        self._scheduled.discard(active[1])
        if not self._closed and not active[0].cancelled:
            self.decoded.emit(key, image, generation)
        self._pump()
        if not self._paused and not active[0].cancelled:
            self.capacity_available.emit()
