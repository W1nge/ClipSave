import sys
import os
import ctypes
from ctypes import wintypes
from pathlib import Path


def _windows_local_appdata() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetFolderPathW.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    shell32.SHGetFolderPathW.restype = ctypes.c_long
    result = shell32.SHGetFolderPathW(None, 0x001C, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError(f"SHGetFolderPathW(CSIDL_LOCAL_APPDATA) failed: {result}")
    return Path(buffer.value)


def _local_appdata() -> Path:
    if os.name == "nt":
        return _windows_local_appdata()
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))


def _configured_local_root() -> Path:
    try:
        profile_index = sys.argv.index("--smoke-profile")
        ready_index = sys.argv.index("--smoke-ready-file")
        if ready_index + 1 >= len(sys.argv):
            raise IndexError
        return Path(sys.argv[profile_index + 1])
    except (ValueError, IndexError):
        return _local_appdata() / "ClipSave"


APP_NAME = "ClipSave"
APP_VERSION = "1.0.0"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
LOCAL_ROOT = _configured_local_root()
DATA_DIR = LOCAL_ROOT / "Data"
LIBRARY_DIR = LOCAL_ROOT / "Library"
PICTURE_DIR = LIBRARY_DIR / "Pictures"
MARKDOWN_DIR = LIBRARY_DIR / "Markdown"
LEGACY_DATA_DIR = BASE_DIR / "data"
LEGACY_PICTURE_DIR = BASE_DIR / "Picture"
LEGACY_MARKDOWN_DIR = BASE_DIR / "Markdown"
THUMB_DIR = DATA_DIR / "thumbnails"
DATABASE_PATH = DATA_DIR / "clipsave.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
MAINTENANCE_DIR = DATA_DIR / "maintenance"
INSTANCE_SERVER = "ClipSave.Desktop.Instance.v2"
MAX_IMPORT_BYTES = 250 * 1024 * 1024
MAX_MARKDOWN_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
MAX_CLIPBOARD_TEXT_BYTES = 4 * 1024 * 1024
MAX_CLIPBOARD_IMAGE_BYTES = 256 * 1024 * 1024
MAX_AI_RESPONSE_BYTES = 8 * 1024 * 1024

TYPE_LABELS = {
    "image": "图片",
    "text": "文字",
    "markdown": "Markdown",
}

TAG_COLORS = ["#2f7df6", "#27ae60", "#8b5cf6", "#f59e0b", "#ef6461", "#00a6a6", "#64748b"]
