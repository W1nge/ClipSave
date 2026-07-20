from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .constants import SETTINGS_PATH


DEFAULTS = {
    "sidebar_collapsed": False,
    "monitoring": False,
    "view_mode": "grid",
    "sort": "newest",
    "close_to_tray": True,
    "start_with_windows": False,
    "follow_system_theme": True,
    "theme_mode": "light",
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_vision_model": "",
    "ai_embedding_model": "",
    "auto_ocr": False,
    "auto_description": False,
}
MAX_SETTINGS_BYTES = 1024 * 1024
_STRING_LIMITS = {
    "ai_base_url": 8192,
    "ai_api_key": 16_384,
    "ai_vision_model": 512,
    "ai_embedding_model": 512,
}

_BOOLEAN_KEYS = {
    "sidebar_collapsed",
    "monitoring",
    "close_to_tray",
    "start_with_windows",
    "follow_system_theme",
    "auto_ocr",
    "auto_description",
}
_STRING_KEYS = {
    "ai_base_url",
    "ai_api_key",
    "ai_vision_model",
    "ai_embedding_model",
}
_CHOICES = {
    "view_mode": {"grid", "list"},
    "sort": {"newest", "oldest", "name", "size", "type"},
    "theme_mode": {"light", "dark"},
}


def _valid_value(key: str, value: Any) -> bool:
    if key in _BOOLEAN_KEYS:
        return type(value) is bool
    if key in _STRING_KEYS:
        return type(value) is str and len(value) <= _STRING_LIMITS[key]
    if key in _CHOICES:
        return type(value) is str and value in _CHOICES[key]
    return False


def _validated_settings(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if key in DEFAULTS and _valid_value(key, item)}


def _read_settings(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_SETTINGS_BYTES:
            return None
        return _validated_settings(json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None


def _write_json_temp(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _SettingsPublishedError(OSError):
    """The primary file was replaced before the final durability sync failed."""


class Settings:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self.backup_path = path.with_name(f"{path.name}.bak")
        self.data = DEFAULTS.copy()
        loaded = _read_settings(path)
        recovered = loaded is None
        if loaded is None:
            loaded = _read_settings(self.backup_path)
        if loaded is not None:
            self.data.update(loaded)
        if recovered:
            self.data["monitoring"] = False

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        if not _valid_value(key, value):
            raise TypeError(f"Invalid value for setting {key!r}")
        previous = self.data.get(key)
        if previous == value:
            return
        self.data[key] = value
        try:
            self.save()
        except _SettingsPublishedError:
            raise
        except Exception:
            self.data[key] = previous
            raise

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key not in DEFAULTS:
                raise KeyError(key)
            if not _valid_value(key, value):
                raise TypeError(f"Invalid value for setting {key!r}")
        previous = self.data.copy()
        self.data.update(values)
        try:
            self.save()
        except _SettingsPublishedError:
            raise
        except Exception:
            self.data.clear()
            self.data.update(previous)
            raise

    def save(self) -> None:
        data = DEFAULTS.copy()
        data.update(_validated_settings(self.data) or {})
        temporary_path = _write_json_temp(self.path, data)
        backup_temporary = None
        try:
            current = _read_settings(self.path)
            if current is not None:
                backup_data = DEFAULTS.copy()
                backup_data.update(current)
                backup_temporary = _write_json_temp(self.backup_path, backup_data)
                os.replace(backup_temporary, self.backup_path)
                backup_temporary = None
            os.replace(temporary_path, self.path)
            self.data = data
            try:
                _sync_directory(self.path.parent)
            except Exception as exc:
                loaded = _read_settings(self.path)
                self.data = DEFAULTS.copy()
                if loaded is not None:
                    self.data.update(loaded)
                raise _SettingsPublishedError(
                    "Settings were saved, but the directory sync failed"
                ) from exc
        finally:
            temporary_path.unlink(missing_ok=True)
            if backup_temporary is not None:
                backup_temporary.unlink(missing_ok=True)
