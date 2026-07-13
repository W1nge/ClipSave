from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .services import ImageFileSnapshot, OperationCancelled, preflight_image_file, require_snapshot_hash


class WindowsOCRService:
    OCR_TIMEOUT_SECONDS = 60.0

    @staticmethod
    def _winrt_types():
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage import FileAccessMode, StorageFile

        return BitmapDecoder, OcrEngine, FileAccessMode, StorageFile

    @staticmethod
    def _close(resource) -> None:
        if resource is None:
            return
        close = getattr(resource, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @classmethod
    async def recognize_async(
        cls,
        source: Path | ImageFileSnapshot,
        cancel_event: threading.Event | None = None,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Operation cancelled")
        snapshot = source if isinstance(source, ImageFileSnapshot) else preflight_image_file(source)
        if expected_sha256 is not None:
            require_snapshot_hash(snapshot, expected_sha256)
        try:
            return await asyncio.wait_for(
                cls._recognize_snapshot_async(snapshot, cancel_event),
                timeout=cls.OCR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Windows OCR timed out") from exc

    @classmethod
    async def _recognize_snapshot_async(
        cls,
        snapshot: ImageFileSnapshot,
        cancel_event: threading.Event | None,
    ) -> str:
        BitmapDecoder, OcrEngine, FileAccessMode, StorageFile = cls._winrt_types()
        stream = None
        bitmap = None
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Operation cancelled")
            snapshot.require_current()
            file = await StorageFile.get_file_from_path_async(str(snapshot.path))
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Operation cancelled")
            stream = await file.open_async(FileAccessMode.READ)
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Operation cancelled")
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                raise RuntimeError("Windows 没有安装可用的 OCR 语言包。")
            result = await engine.recognize_async(bitmap)
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled("Operation cancelled")
            snapshot.require_current()
            return result.text.strip()
        finally:
            cls._close(bitmap)
            cls._close(stream)

    @classmethod
    def recognize(
        cls,
        source: Path | ImageFileSnapshot,
        cancel_event: threading.Event | None = None,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("recognize() 不能在运行中的事件循环内调用；请 await recognize_async()。")
        return asyncio.run(
            cls.recognize_async(
                source,
                cancel_event,
                expected_sha256=expected_sha256,
            )
        )
