from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .constants import APP_NAME, BASE_DIR


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        arguments = [str(Path(sys.executable).resolve())]
    else:
        python = Path(sys.executable).resolve()
        pythonw = python.with_name("pythonw.exe")
        arguments = [
            str(pythonw if pythonw.is_file() else python),
            str(BASE_DIR / "clipsave.py"),
        ]
    return subprocess.list2cmdline(arguments)


def set_start_with_windows(enabled: bool) -> None:
    if os.name != "nt":
        raise OSError("开机自启动仅支持 Windows。")

    import winreg

    if enabled:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _startup_command())
        return

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
