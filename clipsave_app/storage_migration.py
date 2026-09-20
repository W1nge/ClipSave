from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .database_files import (
    archive_identical_database,
    rebind_migrated_paths,
)


@dataclass(frozen=True, slots=True)
class StorageMigrationPaths:
    base_dir: Path
    legacy_data_dir: Path
    legacy_picture_dir: Path
    legacy_markdown_dir: Path
    data_dir: Path
    library_dir: Path
    picture_dir: Path
    markdown_dir: Path


@dataclass(frozen=True, slots=True)
class StorageMigrationOps:
    is_link_or_junction: Callable[[Path], bool]
    paths_overlap: Callable[[Path, Path], bool]
    open_managed_binary: Callable[..., object]
    delete_managed_file: Callable[..., None]
    delete_source_if_identical: Callable[[Path, Path, Path, Path], bool]
    copy_verify_delete_file: Callable[[Path, Path, Path, Path], bool]
    copy_or_move_contents: Callable[[Path, Path], int]
    copy_legacy_database_snapshot: Callable[[Path, Path], None]


def _archive_identical_legacy_database(
    legacy_database: Path,
    active_database: Path,
) -> None:
    archive_identical_database(legacy_database, active_database)


def _managed_files_identical(
    source: Path,
    destination: Path,
    source_root: Path,
    destination_root: Path,
    ops: StorageMigrationOps,
) -> bool:
    try:
        with ops.open_managed_binary(
            source,
            "rb",
            source_root,
            identity_locked=True,
        ) as source_handle, ops.open_managed_binary(
            destination,
            "rb",
            destination_root,
            identity_locked=True,
        ) as destination_handle:
            source_stat = os.fstat(source_handle.fileno())
            destination_stat = os.fstat(destination_handle.fileno())
            if source_stat.st_size != destination_stat.st_size:
                return False
            source_digest = hashlib.sha256()
            destination_digest = hashlib.sha256()
            while chunk := source_handle.read(1024 * 1024):
                source_digest.update(chunk)
            while chunk := destination_handle.read(1024 * 1024):
                destination_digest.update(chunk)
            return source_digest.digest() == destination_digest.digest()
    except (OSError, RuntimeError):
        return False


def _copy_library_file_without_delete(
    source: Path,
    destination: Path,
    source_root: Path,
    destination_root: Path,
    ops: StorageMigrationOps,
) -> bool:
    if ops.is_link_or_junction(source) or ops.is_link_or_junction(destination):
        return False
    if destination.exists():
        return _managed_files_identical(
            source,
            destination,
            source_root,
            destination_root,
            ops,
        )
    created_destination = False
    copied_digest = hashlib.sha256()
    copied_size = 0
    try:
        with ops.open_managed_binary(
            source,
            "rb",
            source_root,
            identity_locked=True,
        ) as source_handle, ops.open_managed_binary(
            destination,
            "xb",
            destination_root,
        ) as destination_handle:
            created_destination = True
            source_stat = os.fstat(source_handle.fileno())
            if not stat.S_ISREG(source_stat.st_mode):
                raise RuntimeError(f"Migration source is not a regular file: {source}")
            while chunk := source_handle.read(1024 * 1024):
                destination_handle.write(chunk)
                copied_digest.update(chunk)
                copied_size += len(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        return _managed_files_identical(
            source,
            destination,
            source_root,
            destination_root,
            ops,
        )
    except (FileExistsError, OSError, RuntimeError):
        if created_destination:
            try:
                ops.delete_managed_file(
                    destination,
                    destination_root,
                    expected_sha256=copied_digest.hexdigest(),
                    expected_size=copied_size,
                )
            except (OSError, RuntimeError):
                pass
        return False


def _copy_library_contents_for_migration(
    source: Path,
    target: Path,
    source_root: Path,
    target_root: Path,
    moves: list[tuple[Path, Path]],
    ops: StorageMigrationOps,
) -> None:
    if (
        ops.is_link_or_junction(source)
        or ops.is_link_or_junction(target)
        or ops.paths_overlap(source, target)
    ):
        return
    if not source.exists() or not source.is_dir():
        return
    if target.exists() and not target.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    if ops.is_link_or_junction(target):
        return
    try:
        children = list(source.iterdir())
    except OSError:
        return
    for child in children:
        if ops.is_link_or_junction(child):
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_library_contents_for_migration(
                child,
                destination,
                source_root,
                target_root,
                moves,
                ops,
            )
            continue
        if not child.is_file() or ops.is_link_or_junction(destination):
            continue
        if destination.exists() and not _managed_files_identical(
            child,
            destination,
            source_root,
            target_root,
            ops,
        ):
            stem, suffix = child.stem, child.suffix
            index = 2
            while destination.exists():
                destination = target / f"{stem} (migrated {index}){suffix}"
                index += 1
        if _copy_library_file_without_delete(
            child,
            destination,
            source_root,
            target_root,
            ops,
        ):
            moves.append((child, destination))


def _remove_empty_migration_directories(
    path: Path,
    ops: StorageMigrationOps,
) -> None:
    if not path.exists() or not path.is_dir() or ops.is_link_or_junction(path):
        return
    try:
        children = list(path.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir() and not ops.is_link_or_junction(child):
            _remove_empty_migration_directories(child, ops)
    try:
        path.rmdir()
    except OSError:
        pass


def _finalize_library_migration(
    moves: list[tuple[Path, Path]],
    source_root: Path,
    destination_root: Path,
    ops: StorageMigrationOps,
) -> None:
    for source, destination in moves:
        try:
            ops.delete_source_if_identical(
                source,
                destination,
                source_root,
                destination_root,
            )
        except (OSError, RuntimeError):
            pass
    _remove_empty_migration_directories(source_root, ops)


def migrate_legacy_layout(
    paths: StorageMigrationPaths,
    ops: StorageMigrationOps,
) -> dict[str, int]:
    """Move the first-generation local store out of the install directory.

    Database snapshots include committed WAL content. Library files are copied
    first, rebound to their existing database rows, and only then removed from
    the legacy directory.
    """
    result = {"pictures": 0, "markdown": 0, "data": 0}
    if ops.is_link_or_junction(paths.library_dir) or ops.is_link_or_junction(
        paths.data_dir
    ):
        return result
    paths.library_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    if ops.is_link_or_junction(paths.library_dir) or ops.is_link_or_junction(
        paths.data_dir
    ):
        return result

    legacy_database = paths.legacy_data_dir / "clipsave.db"
    active_database = paths.data_dir / "clipsave.db"
    if legacy_database.is_file() and not active_database.exists():
        ops.copy_legacy_database_snapshot(legacy_database, active_database)
    if legacy_database.is_file() and active_database.is_file():
        _archive_identical_legacy_database(legacy_database, active_database)

    picture_moves: list[tuple[Path, Path]] = []
    markdown_moves: list[tuple[Path, Path]] = []
    _copy_library_contents_for_migration(
        paths.legacy_picture_dir,
        paths.picture_dir,
        paths.legacy_picture_dir,
        paths.picture_dir,
        picture_moves,
        ops,
    )
    _copy_library_contents_for_migration(
        paths.legacy_markdown_dir,
        paths.markdown_dir,
        paths.legacy_markdown_dir,
        paths.markdown_dir,
        markdown_moves,
        ops,
    )
    all_library_moves = picture_moves + markdown_moves
    rebind_migrated_paths(active_database, all_library_moves)
    _finalize_library_migration(
        picture_moves,
        paths.legacy_picture_dir,
        paths.picture_dir,
        ops,
    )
    _finalize_library_migration(
        markdown_moves,
        paths.legacy_markdown_dir,
        paths.markdown_dir,
        ops,
    )
    result["pictures"] = len(picture_moves)
    result["markdown"] = len(markdown_moves)
    result["data"] = ops.copy_or_move_contents(
        paths.legacy_data_dir,
        paths.data_dir,
    )

    legacy_history = paths.base_dir / "clipsave_history.json"
    history_target = paths.data_dir / legacy_history.name
    if (
        legacy_history.exists()
        and not ops.is_link_or_junction(legacy_history)
        and not ops.is_link_or_junction(history_target)
        and not ops.paths_overlap(legacy_history, history_target)
        and not history_target.exists()
    ):
        if ops.copy_verify_delete_file(
            legacy_history,
            history_target,
            paths.base_dir,
            paths.data_dir,
        ):
            result["data"] += 1
    return result
