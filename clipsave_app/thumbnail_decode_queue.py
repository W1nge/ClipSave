from __future__ import annotations

from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QImageReader

from .thumbnail_cache import THUMBNAIL_SIZE, ThumbnailCacheKey


THUMBNAIL_WORKERS = 2
THUMBNAIL_REQUEST_LIMIT = 32


def decode_thumbnail_image(key: ThumbnailCacheKey) -> QImage:
    reader = QImageReader(key.path)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        reader.setScaledSize(
            source_size.scaled(
                THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        )
    image = reader.read()
    if not image.isNull() and (
        image.width() > THUMBNAIL_SIZE.width()
        or image.height() > THUMBNAIL_SIZE.height()
    ):
        image = image.scaled(
            THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


class ThumbnailDecodeSignals(QObject):
    finished = Signal(int, object, int, object)


class ThumbnailDecodeTask(QRunnable):
    def __init__(
        self,
        request_id: int,
        key: ThumbnailCacheKey,
        generation: int,
        decode_image: Callable[[ThumbnailCacheKey], QImage],
    ):
        super().__init__()
        self.request_id = request_id
        self.key = key
        self.generation = generation
        self.decode_image = decode_image
        self.signals = ThumbnailDecodeSignals()
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
        max_workers: int = THUMBNAIL_WORKERS,
        max_requests: int = THUMBNAIL_REQUEST_LIMIT,
        decode_image: Callable[[ThumbnailCacheKey], QImage] = decode_thumbnail_image,
    ):
        super().__init__(parent)
        self.max_workers = max(1, max_workers)
        self.max_active = self.max_workers * 2
        self.max_requests = max(self.max_workers, max_requests)
        self.decode_image = decode_image
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(self.max_active)
        self._queued: deque[tuple[ThumbnailCacheKey, int]] = deque()
        self._scheduled: set[tuple[int, ThumbnailCacheKey]] = set()
        self._active: dict[
            int,
            tuple[ThumbnailDecodeTask, tuple[int, ThumbnailCacheKey]],
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

    def request(self, key: ThumbnailCacheKey, generation: int) -> bool:
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
            task = ThumbnailDecodeTask(
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
        key: ThumbnailCacheKey,
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
