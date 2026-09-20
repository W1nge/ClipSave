from __future__ import annotations

from .thumbnail_service import ThumbnailCacheKey, ThumbnailDecodeQueue


class ThumbnailSession:
    """Own thumbnail queue generation/cancellation lifecycle for one consumer."""

    def __init__(self, queue: ThumbnailDecodeQueue) -> None:
        self.queue = queue
        self.generation = 0

    def invalidate(self, *, cancel_queued: bool = True) -> int:
        self.generation += 1
        if cancel_queued:
            self.queue.cancel_queued()
        return self.generation

    def request(self, key: ThumbnailCacheKey) -> bool:
        return self.queue.request(key, self.generation)

    def is_current(self, generation: int) -> bool:
        return generation == self.generation

    def close(self, timeout_ms: int = 2000) -> bool:
        self.invalidate(cancel_queued=False)
        return self.queue.close(timeout_ms)

    def pause_and_wait(self, timeout_ms: int = 2000) -> bool:
        self.invalidate(cancel_queued=False)
        return self.queue.pause_and_wait(timeout_ms)

    def resume(self) -> None:
        self.queue.resume()
