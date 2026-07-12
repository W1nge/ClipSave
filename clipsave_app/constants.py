import sys
import os
from pathlib import Path


APP_NAME = "ClipSave"
APP_VERSION = "0.2.0"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
LOCAL_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ClipSave"
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
MAX_EMBEDDING_DIMENSIONS = 65_536

TYPE_LABELS = {
    "image": "图片",
    "text": "文字",
    "markdown": "Markdown",
}

TAG_COLORS = ["#2f7df6", "#27ae60", "#8b5cf6", "#f59e0b", "#ef6461", "#00a6a6", "#64748b"]
