from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_temp(
    path: Path,
    data: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
) -> Path:
    """Write a durable JSON temp file beside its eventual destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=ensure_ascii,
                indent=indent,
                separators=separators,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def sync_directory(path: Path) -> None:
    """Persist directory-entry updates where the platform exposes directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
