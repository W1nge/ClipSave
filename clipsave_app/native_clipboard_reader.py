from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from contextlib import contextmanager
from functools import lru_cache

from .constants import (
    MAX_CLIPBOARD_IMAGE_BYTES,
    MAX_CLIPBOARD_TEXT_BYTES,
)
from .native_clipboard_formats import (
    REGISTERED_IMAGE_FORMATS,
    decode_native_image as decode_clipboard_image,
    dib_as_bmp as clipboard_dib_as_bmp,
    validate_registered_image_header as validate_clipboard_image_header,
)


class ClipboardBusy(RuntimeError):
    pass


@contextmanager
def _opened_clipboard(api_provider):
    user32, kernel32 = api_provider()
    if not user32.OpenClipboard(None):
        raise ClipboardBusy("Clipboard is temporarily busy")
    try:
        yield user32, kernel32
    finally:
        user32.CloseClipboard()


class NativeClipboardReader:
    REGISTERED_IMAGE_FORMATS = REGISTERED_IMAGE_FORMATS
    CF_DIB = 8
    CF_UNICODETEXT = 13
    CF_HDROP = 15
    CF_DIBV5 = 17
    MAX_FILE_PATHS = 1024
    MAX_FILE_PATH_CHARS = 32_767
    DRAG_QUERY_FILE_COUNT = 0xFFFFFFFF

    @staticmethod
    @lru_cache(maxsize=1)
    def windows_clipboard_apis():
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
        user32.EnumClipboardFormats.restype = wintypes.UINT
        user32.GetClipboardFormatNameW.argtypes = [
            wintypes.UINT,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        user32.GetClipboardFormatNameW.restype = ctypes.c_int
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.SetLastError.argtypes = [wintypes.DWORD]
        kernel32.SetLastError.restype = None
        return user32, kernel32

    @staticmethod
    @lru_cache(maxsize=1)
    def windows_shell_api():
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.DragQueryFileW.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        shell32.DragQueryFileW.restype = wintypes.UINT
        return shell32

    @classmethod
    def clipboard_sequence(cls, api_provider=None) -> int | None:
        if os.name != "nt":
            return None
        provider = api_provider or cls.windows_clipboard_apis
        try:
            user32, _kernel32 = provider()
            return int(user32.GetClipboardSequenceNumber())
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    @classmethod
    def registered_image_descriptors_locked(
        cls,
        user32,
        kernel32,
    ) -> list[tuple[str, object, int]]:
        results = []
        format_id = 0
        while True:
            kernel32.SetLastError(0)
            format_id = int(user32.EnumClipboardFormats(format_id))
            if not format_id:
                if kernel32.GetLastError() != 0:
                    raise ClipboardBusy("Clipboard formats are temporarily unavailable")
                return results
            name_buffer = ctypes.create_unicode_buffer(128)
            if not user32.GetClipboardFormatNameW(
                format_id,
                name_buffer,
                len(name_buffer),
            ):
                continue
            name = name_buffer.value
            if name not in cls.REGISTERED_IMAGE_FORMATS:
                continue
            handle = user32.GetClipboardData(format_id)
            if not handle:
                raise ClipboardBusy("Registered clipboard image data is not ready")
            size = int(kernel32.GlobalSize(handle))
            if size <= 0:
                raise ValueError("Invalid registered clipboard image data")
            if size > MAX_CLIPBOARD_IMAGE_BYTES:
                raise ValueError("Clipboard image payload is too large")
            results.append((name, handle, size))

    @staticmethod
    def copy_clipboard_payload_locked(kernel32, handle, size: int) -> bytes:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ValueError("Unable to inspect clipboard image data")
        try:
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)

    @classmethod
    def native_image_snapshot(cls, api_provider=None) -> tuple[str, bytes] | None:
        if os.name != "nt":
            return None
        provider = api_provider or cls.windows_clipboard_apis
        try:
            with _opened_clipboard(provider) as (user32, kernel32):
                errors: list[ValueError | ClipboardBusy] = []
                try:
                    registered = cls.registered_image_descriptors_locked(
                        user32,
                        kernel32,
                    )
                except (ValueError, ClipboardBusy) as exc:
                    registered = []
                    errors.append(exc)
                dibs = []
                for format_id, name in (
                    (cls.CF_DIBV5, "DIBV5"),
                    (cls.CF_DIB, "DIB"),
                ):
                    if not user32.IsClipboardFormatAvailable(format_id):
                        continue
                    try:
                        handle = user32.GetClipboardData(format_id)
                        if not handle:
                            raise ClipboardBusy("Clipboard DIB data is not ready")
                        size = int(kernel32.GlobalSize(handle))
                        if size <= 0:
                            raise ValueError("Invalid clipboard DIB data")
                        if size > MAX_CLIPBOARD_IMAGE_BYTES:
                            raise ValueError("Clipboard image payload is too large")
                        dibs.append((name, handle, size))
                    except (ValueError, ClipboardBusy) as exc:
                        errors.append(exc)
                candidates = registered + dibs
                for name, handle, size in candidates:
                    try:
                        payload = cls.copy_clipboard_payload_locked(
                            kernel32,
                            handle,
                            size,
                        )
                        if name in cls.REGISTERED_IMAGE_FORMATS:
                            cls.validate_registered_image_header(
                                name,
                                size,
                                payload[:32],
                            )
                        else:
                            cls.dib_as_bmp(name, payload)
                        return name, payload
                    except ValueError as exc:
                        errors.append(exc)
                if errors:
                    # Prefer another valid image representation above; if no
                    # candidate worked, a delayed owner/conversion can still
                    # supply data on the next read. This is not corrupt data.
                    for error in errors:
                        if isinstance(error, ClipboardBusy):
                            raise error
                    raise errors[-1]
                return None
        except ValueError:
            raise
        except (AttributeError, OSError, TypeError):
            return None

    @classmethod
    def native_text_snapshot(cls, api_provider=None) -> str | None:
        if os.name != "nt":
            return None
        provider = api_provider or cls.windows_clipboard_apis
        try:
            with _opened_clipboard(provider) as (user32, kernel32):
                if not user32.IsClipboardFormatAvailable(cls.CF_UNICODETEXT):
                    return None
                handle = user32.GetClipboardData(cls.CF_UNICODETEXT)
                if not handle:
                    raise ClipboardBusy("Clipboard text data is not ready")
                size = int(kernel32.GlobalSize(handle))
                if size <= 0:
                    raise ValueError("Invalid clipboard Unicode text data")
                if size > MAX_CLIPBOARD_TEXT_BYTES:
                    raise ValueError("剪贴板文字过大，已拒绝读取。")
                payload = cls.copy_clipboard_payload_locked(kernel32, handle, size)
                payload = payload[: len(payload) - (len(payload) % 2)]
                terminator = payload.find(b"\x00\x00")
                while 0 <= terminator and terminator % 2:
                    terminator = payload.find(b"\x00\x00", terminator + 1)
                if terminator < 0:
                    raise ValueError("Invalid clipboard Unicode text data")
                text = payload[:terminator].decode("utf-16-le")
                if len(text.encode("utf-8")) > MAX_CLIPBOARD_TEXT_BYTES:
                    raise ValueError("剪贴板文字过大，已拒绝读取。")
                return text
        except ValueError:
            raise
        except (AttributeError, OSError, TypeError, UnicodeDecodeError):
            return None

    @classmethod
    def native_file_paths_snapshot(
        cls,
        api_provider=None,
        shell_provider=None,
    ) -> tuple[str, ...] | None:
        if os.name != "nt":
            return None
        api = api_provider or cls.windows_clipboard_apis
        shell = shell_provider or cls.windows_shell_api
        try:
            shell32 = shell()
            with _opened_clipboard(api) as (user32, _kernel32):
                if not user32.IsClipboardFormatAvailable(cls.CF_HDROP):
                    return None
                drop_handle = user32.GetClipboardData(cls.CF_HDROP)
                if not drop_handle:
                    raise ClipboardBusy("Clipboard file paths are not ready")
                count = int(
                    shell32.DragQueryFileW(
                        drop_handle,
                        cls.DRAG_QUERY_FILE_COUNT,
                        None,
                        0,
                    )
                )
                if count <= 0:
                    return ()
                if count > cls.MAX_FILE_PATHS:
                    raise ValueError("Clipboard file selection contains too many paths")
                paths = []
                total_bytes = 0
                for index in range(count):
                    length = int(shell32.DragQueryFileW(drop_handle, index, None, 0))
                    if length <= 0 or length > cls.MAX_FILE_PATH_CHARS:
                        raise ValueError("Invalid clipboard file path")
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    copied = int(
                        shell32.DragQueryFileW(
                            drop_handle,
                            index,
                            buffer,
                            len(buffer),
                        )
                    )
                    if copied != length or not buffer.value:
                        raise ValueError("Unable to inspect clipboard file path")
                    path = buffer.value
                    total_bytes += len(path.encode("utf-8")) + (1 if paths else 0)
                    if total_bytes > MAX_CLIPBOARD_TEXT_BYTES:
                        raise ValueError("Clipboard file path list is too large")
                    paths.append(path)
                return tuple(paths)
        except ValueError:
            raise
        except (AttributeError, OSError, TypeError, UnicodeEncodeError):
            return None

    @classmethod
    def validate_registered_image_header(
        cls,
        name: str,
        size: int,
        header: bytes,
    ) -> None:
        validate_clipboard_image_header(name, size, header)

    @staticmethod
    def dib_as_bmp(name: str, payload: bytes) -> bytes:
        return clipboard_dib_as_bmp(name, payload)

    @classmethod
    def decode_native_image(
        cls,
        name: str,
        payload: bytes,
        *,
        validate_image,
    ):
        return decode_clipboard_image(
            name,
            payload,
            validate_image=validate_image,
        )
