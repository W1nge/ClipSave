from __future__ import annotations

import asyncio
from pathlib import Path


class WindowsOCRService:
    @staticmethod
    async def _recognize(path: Path) -> str:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage import FileAccessMode, StorageFile

        file = await StorageFile.get_file_from_path_async(str(path.resolve()))
        stream = await file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("Windows 没有安装可用的 OCR 语言包。")
        result = await engine.recognize_async(bitmap)
        return result.text.strip()

    @classmethod
    def recognize(cls, path: Path) -> str:
        return asyncio.run(cls._recognize(path))
