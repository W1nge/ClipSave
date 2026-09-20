from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path


def logical_database_hash(path: Path) -> str:
    digest = hashlib.sha256()
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        digest.update(str(connection.execute("PRAGMA user_version").fetchone()[0]).encode("ascii"))
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


def sqlite_quick_check(path: Path) -> bool:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
        return bool(rows) and all(str(row[0]).lower() == "ok" for row in rows)
    finally:
        connection.close()


def copy_database_snapshot(source: Path, destination: Path, temporary: Path) -> None:
    source_connection = None
    destination_connection = None
    try:
        source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        destination_connection.commit()
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        if not sqlite_quick_check(temporary):
            raise RuntimeError(f"Migrated ClipSave database failed integrity validation: {source}")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if destination.exists():
            raise FileExistsError(destination)
        os.replace(temporary, destination)
        if not sqlite_quick_check(destination):
            raise RuntimeError(f"Migrated ClipSave database failed final validation: {destination}")
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        for cleanup in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
            cleanup.unlink(missing_ok=True)


def archive_identical_database(legacy_database: Path, active_database: Path) -> None:
    try:
        identical = logical_database_hash(legacy_database) == logical_database_hash(active_database)
    except (OSError, sqlite3.Error):
        identical = False
    if not identical:
        raise RuntimeError(
            "Both the legacy and current ClipSave databases exist. "
            f"They were left unchanged to prevent history loss: {legacy_database} ; {active_database}"
        )
    preserved = legacy_database.with_name(f"{legacy_database.name}.migrated-duplicate")
    index = 2
    while preserved.exists():
        preserved = legacy_database.with_name(
            f"{legacy_database.name}.migrated-duplicate-{index}"
        )
        index += 1
    moves = [(legacy_database, preserved)]
    for suffix in ("-wal", "-shm"):
        source = Path(f"{legacy_database}{suffix}")
        if source.exists():
            moves.append((source, Path(f"{preserved}{suffix}")))
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            os.replace(source, destination)
            completed.append((source, destination))
    except OSError:
        for source, destination in reversed(completed):
            try:
                os.replace(destination, source)
            except OSError:
                pass
        raise


def _path_key(path: Path | str) -> str:
    value = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(value)))


def rebind_migrated_paths(database_path: Path, moves: list[tuple[Path, Path]]) -> int:
    if not database_path.is_file() or not moves:
        return 0
    mapping = {_path_key(source): (str(destination), _path_key(destination)) for source, destination in moves}
    connection = sqlite3.connect(database_path)
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(items)").fetchall()
        }
        if "path" not in columns:
            return 0
        selected_columns = "id,path"
        if "resolved_path" in columns:
            selected_columns += ",resolved_path"
        rows = connection.execute(
            f"SELECT {selected_columns} FROM items WHERE path IS NOT NULL"
        ).fetchall()
        updated = 0
        with connection:
            for row in rows:
                try:
                    old_key = _path_key(row[2] if len(row) > 2 and row[2] else row[1])
                except (OSError, RuntimeError, ValueError):
                    continue
                replacement = mapping.get(old_key)
                if replacement is None:
                    continue
                assignments = ["path=?"]
                parameters: list[object] = [replacement[0]]
                if "resolved_path" in columns:
                    assignments.append("resolved_path=?")
                    parameters.append(replacement[1])
                if "missing" in columns:
                    assignments.append("missing=0")
                parameters.append(row[0])
                connection.execute(
                    f"UPDATE items SET {','.join(assignments)} WHERE id=?",
                    parameters,
                )
                updated += 1
        if not sqlite_quick_check(database_path):
            raise RuntimeError(f"Migrated ClipSave database failed path validation: {database_path}")
        return updated
    except sqlite3.Error as exc:
        raise RuntimeError(f"Unable to update migrated ClipSave paths: {database_path}") from exc
    finally:
        connection.close()
