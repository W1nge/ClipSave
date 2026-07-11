from __future__ import annotations

import json
from pathlib import Path

from .constants import SETTINGS_PATH


DEFAULTS = {
    "sidebar_collapsed": False,
    "monitoring": True,
    "view_mode": "grid",
    "sort": "newest",
    "theme": "light",
    "close_to_tray": True,
    "hotkey": "Ctrl+Alt+V",
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_vision_model": "",
    "ai_embedding_model": "",
}


class Settings:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self.data = DEFAULTS.copy()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
