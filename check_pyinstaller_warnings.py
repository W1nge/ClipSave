from __future__ import annotations

import re
import sys
from pathlib import Path


ALLOWED_MISSING_MODULES = {
    "Foundation",
    "_frozen_importlib_external",
    "_posixshmem",
    "_posixsubprocess",
    "_scproxy",
    "annotationlib",
    "asyncio.DefaultEventLoopPolicy",
    "collections.abc",
    "defusedxml",
    "fcntl",
    "gi",
    "grp",
    "java",
    "java.lang",
    "multiprocessing.AuthenticationError",
    "multiprocessing.BufferTooShort",
    "multiprocessing.TimeoutError",
    "multiprocessing.get_context",
    "multiprocessing.get_start_method",
    "multiprocessing.set_start_method",
    "numpy",
    "olefile",
    "posix",
    "pwd",
    "pyimod02_importers",
    "pythoncom",
    "pywintypes",
    "readline",
    "resource",
    "termios",
    "vms_lib",
    "win32com",
    "win32com.server",
    "win32com.shell",
}


def unexpected_missing_modules(text: str) -> list[str]:
    missing = {
        match.group(1).strip("'\"")
        for match in re.finditer(r"^missing module named (.+?) - imported", text, re.MULTILINE)
    }
    return sorted(missing - ALLOWED_MISSING_MODULES, key=str.lower)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_pyinstaller_warnings.py WARN_FILE")
    warning_path = Path(sys.argv[1])
    unexpected = unexpected_missing_modules(warning_path.read_text(encoding="utf-8"))
    if unexpected:
        raise RuntimeError("Unexpected PyInstaller missing modules: " + ", ".join(unexpected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
