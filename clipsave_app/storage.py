from __future__ import annotations

import os
import shutil
from pathlib import Path

from .constants import (
    BASE_DIR,
    DATA_DIR,
    LEGACY_DATA_DIR,
    LEGACY_MARKDOWN_DIR,
    LEGACY_PICTURE_DIR,
    LIBRARY_DIR,
    LOCAL_ROOT,
    MAINTENANCE_DIR,
    MARKDOWN_DIR,
    PICTURE_DIR,
    THUMB_DIR,
)


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first_resolved = _resolved(first)
        second_resolved = _resolved(second)
        return (
            first_resolved == second_resolved
            or first_resolved in second_resolved.parents
            or second_resolved in first_resolved.parents
        )
    except (OSError, RuntimeError):
        return True


def _copy_or_move_contents(source: Path, target: Path) -> int:
    if _is_link_or_junction(source) or _is_link_or_junction(target) or _paths_overlap(source, target):
        return 0
    if not source.exists() or not source.is_dir():
        return 0
    if target.exists() and not target.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(target):
        return 0
    moved = 0
    for child in list(source.iterdir()):
        if _is_link_or_junction(child):
            continue
        destination = target / child.name
        if _is_link_or_junction(destination):
            continue
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
    result = {"pictures": 0, "markdown": 0, "data": 0}
    if _is_link_or_junction(LIBRARY_DIR) or _is_link_or_junction(DATA_DIR):
        return result
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(LIBRARY_DIR) or _is_link_or_junction(DATA_DIR):
        return result
    result["pictures"] = _copy_or_move_contents(LEGACY_PICTURE_DIR, PICTURE_DIR)
    result["markdown"] = _copy_or_move_contents(LEGACY_MARKDOWN_DIR, MARKDOWN_DIR)
    result["data"] = _copy_or_move_contents(LEGACY_DATA_DIR, DATA_DIR)
    legacy_history = BASE_DIR / "clipsave_history.json"
    history_target = DATA_DIR / legacy_history.name
    if (
        legacy_history.exists()
        and not _is_link_or_junction(legacy_history)
        and not _is_link_or_junction(history_target)
        and not _paths_overlap(legacy_history, history_target)
        and not history_target.exists()
    ):
        shutil.move(str(legacy_history), str(history_target))
        result["data"] += 1
    return result


def validate_storage_layout() -> None:
    paths = (LOCAL_ROOT, DATA_DIR, LIBRARY_DIR, PICTURE_DIR, MARKDOWN_DIR, THUMB_DIR, MAINTENANCE_DIR)
    for path in paths:
        if _is_link_or_junction(path):
            raise RuntimeError(f"ClipSave 本地存储路径不能是符号链接或 Junction：{path}")
    root = Path(os.path.abspath(LOCAL_ROOT))
    for path in paths[1:]:
        try:
            Path(os.path.abspath(path)).relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"ClipSave 本地存储路径超出预期目录：{path}") from exc


def ensure_storage_directories() -> None:
    validate_storage_layout()
    for path in (DATA_DIR, LIBRARY_DIR, PICTURE_DIR, MARKDOWN_DIR, THUMB_DIR, MAINTENANCE_DIR):
        path.mkdir(parents=True, exist_ok=True)
    validate_storage_layout()


def is_under_local_store(path: Path) -> bool:
    try:
        root = Path(os.path.abspath(LIBRARY_DIR))
        candidate = Path(os.path.abspath(path))
        relative = candidate.relative_to(root)
        current = root
        if _is_link_or_junction(current):
            return False
        for part in relative.parts:
            current = current / part
            if _is_link_or_junction(current):
                return False
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False
