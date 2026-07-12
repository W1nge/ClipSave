import unittest
import threading
from pathlib import Path
from unittest.mock import patch

from clipsave_app.ocr_service import WindowsOCRService
from clipsave_app.services import OperationCancelled


class Snapshot:
    path = Path("image.png").resolve()

    def require_current(self):
        return None


class Closable:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def fake_winrt_types(*, engine_available=True):
    stream = Closable()
    bitmap = Closable()

    class StorageFile:
        @staticmethod
        async def get_file_from_path_async(_path):
            class File:
                async def open_async(self, _mode):
                    return stream

            return File()

    class FileAccessMode:
        READ = "read"

    class BitmapDecoder:
        @staticmethod
        async def create_async(_stream):
            class Decoder:
                async def get_software_bitmap_async(self):
                    return bitmap

            return Decoder()

    class OcrEngine:
        @staticmethod
        def try_create_from_user_profile_languages():
            if not engine_available:
                return None

            class Engine:
                async def recognize_async(self, _bitmap):
                    class Result:
                        text = "  recognized text  "

                    return Result()

            return Engine()

    return (BitmapDecoder, OcrEngine, FileAccessMode, StorageFile), stream, bitmap


class WindowsOCRServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognize_async_closes_stream_and_bitmap(self):
        types, stream, bitmap = fake_winrt_types()
        with (
            patch("clipsave_app.ocr_service.preflight_image_file", return_value=Snapshot()),
            patch.object(WindowsOCRService, "_winrt_types", return_value=types),
        ):
            text = await WindowsOCRService.recognize_async(Path("image.png"))

        self.assertEqual(text, "recognized text")
        self.assertTrue(stream.closed)
        self.assertTrue(bitmap.closed)

    async def test_recognize_async_closes_resources_when_engine_is_unavailable(self):
        types, stream, bitmap = fake_winrt_types(engine_available=False)
        with (
            patch("clipsave_app.ocr_service.preflight_image_file", return_value=Snapshot()),
            patch.object(WindowsOCRService, "_winrt_types", return_value=types),
        ):
            with self.assertRaisesRegex(RuntimeError, "OCR 语言包"):
                await WindowsOCRService.recognize_async(Path("image.png"))

        self.assertTrue(stream.closed)
        self.assertTrue(bitmap.closed)

    async def test_sync_entry_rejects_running_event_loop(self):
        with self.assertRaisesRegex(RuntimeError, "recognize_async"):
            WindowsOCRService.recognize(Path("image.png"))

    async def test_cancelled_request_does_not_open_the_file(self):
        types, stream, bitmap = fake_winrt_types()
        cancel_event = threading.Event()
        cancel_event.set()
        with (
            patch("clipsave_app.ocr_service.preflight_image_file", return_value=Snapshot()),
            patch.object(WindowsOCRService, "_winrt_types", return_value=types),
        ):
            with self.assertRaises(OperationCancelled):
                await WindowsOCRService.recognize_async(Path("image.png"), cancel_event)
        self.assertFalse(stream.closed)
        self.assertFalse(bitmap.closed)

    async def test_preflight_failure_happens_before_loading_winrt(self):
        with (
            patch("clipsave_app.ocr_service.preflight_image_file", side_effect=ValueError("too many pixels")),
            patch.object(WindowsOCRService, "_winrt_types") as winrt_types,
        ):
            with self.assertRaisesRegex(ValueError, "too many pixels"):
                await WindowsOCRService.recognize_async(Path("image.png"))
        winrt_types.assert_not_called()


if __name__ == "__main__":
    unittest.main()
