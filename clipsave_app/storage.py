from __future__ import annotations

import shutil
from pathlib import Path

from .constants import (
    BASE_DIR,
    DATA_DIR,
    LEGACY_DATA_DIR,
    LEGACY_MARKDOWN_DIR,
    LEGACY_PICTURE_DIR,
    LIBRARY_DIR,
    MARKDOWN_DIR,
    PICTURE_DIR,
)


def _copy_or_move_contents(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
        return 0
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for child in list(source.iterdir()):
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            continue
        destination = target / child.name
        if destination.exists():
            if child.is_dir():
                moved += _copy_or_move_contents(child, destination)
                try:
                    child.rmdir()
                except OSError:
                    pass
            else:
                # Keep both files if a same-named file already exists.
                stem, suffix = child.stem, child.suffix
                index = 2
                while destination.exists():
                    destination = target / f"{stem} (迁移 {index}){suffix}"
                    index += 1
                shutil.move(str(child), str(destination))
                moved += 1
        else:
            shutil.move(str(child), str(destination))
            moved += 1
    try:
        source.rmdir()
    except OSError:
        pass
    return moved


def migrate_legacy_layout() -> dict[str, int]:
    """Move the first-generation local store out of the install directory.

    The operation is local-only and preserves every source file. Existing
    database rows are repaired by the normal content-hash scan afterward.
    """
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result = {"pictures": 0, "markdown": 0, "data": 0}
    result["pictures"] = _copy_or_move_contents(LEGACY_PICTURE_DIR, PICTURE_DIR)
    result["markdown"] = _copy_or_move_contents(LEGACY_MARKDOWN_DIR, MARKDOWN_DIR)
    result["data"] = _copy_or_move_contents(LEGACY_DATA_DIR, DATA_DIR)
    legacy_history = BASE_DIR / "clipsave_history.json"
    if legacy_history.exists() and not (DATA_DIR / legacy_history.name).exists():
        shutil.move(str(legacy_history), str(DATA_DIR / legacy_history.name))
        result["data"] += 1
    return result


def is_under_local_store(path: Path) -> bool:
    try:
        path.resolve().relative_to(LIBRARY_DIR.resolve())
        return True
    except ValueError:
        return False
