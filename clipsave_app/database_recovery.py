from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable

from . import storage
from . import database_backup as _database_backup
from .sqlite_leaf_lock import SQLiteLeafLock


BackupValidation = _database_backup.BackupValidation
DatabaseBackupStore = _database_backup.DatabaseBackupStore
backup_schema_version = _database_backup.backup_schema_version
backup_sort_key = _database_backup.backup_sort_key
backup_validation_state = _database_backup.backup_validation_state
managed_backup_hash = _database_backup.managed_backup_hash
remove_untrusted_publication = _database_backup.remove_untrusted_publication


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def is_corruption_error(exc: sqlite3.Error) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        if primary_code in {
            getattr(sqlite3, "SQLITE_CORRUPT", 11),
            getattr(sqlite3, "SQLITE_NOTADB", 26),
        }:
            return True
    message = str(exc).lower()
    return "malformed" in message or "not a database" in message


def verify_locked_file_identity(path: Path, handle: BinaryIO) -> os.stat_result:
    handle_stat = os.fstat(handle.fileno())
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"Managed SQLite file changed during open: {path}") from exc
    if (
        not stat.S_ISREG(handle_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or handle_stat.st_nlink != 1
        or path_stat.st_nlink != 1
        or not os.path.samestat(handle_stat, path_stat)
    ):
        raise RuntimeError(
            f"Managed SQLite file identity changed or has multiple hard links: {path}"
        )
    return handle_stat


def quick_check(
    path: Path,
    *,
    busy_timeout_ms: int,
    corruption_check: Callable[[sqlite3.Error], bool] = is_corruption_error,
    tolerate_errors: bool = False,
) -> bool:
    connection = None
    leaf_lock = None
    try:
        with storage.hold_managed_directory(path.parent):
            leaf_lock = SQLiteLeafLock.acquire(
                path, path.parent, create=False, writable=False
            )
            leaf_lock.verify()
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=busy_timeout_ms / 1000,
            )
            leaf_lock.verify()
            rows = connection.execute("PRAGMA quick_check").fetchall()
            leaf_lock.verify()
            return bool(rows) and all(str(row[0]).lower() == "ok" for row in rows)
    except sqlite3.Error as exc:
        if tolerate_errors or corruption_check(exc):
            return False
        raise
    finally:
        if connection is not None:
            connection.close()
        if leaf_lock is not None:
            leaf_lock.close()


def read_schema_version_read_only(path: Path, *, busy_timeout_ms: int) -> int:
    connection = None
    leaf_lock = SQLiteLeafLock.acquire(
        path,
        path.parent,
        create=False,
        writable=False,
    )
    try:
        leaf_lock.verify()
        try:
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=busy_timeout_ms / 1000,
            )
            leaf_lock.verify()
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            if connection is not None:
                connection.close()
    finally:
        leaf_lock.close()


class DatabaseRecoveryManager:
    """Own backup/recovery state and file-lifecycle orchestration for a database."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.recovery_report = {
            "action": "none",
            "backup_path": None,
            "preserved_paths": [],
            "backup_error": None,
        }
        self.needs_library_rescan = False
        self.backups = DatabaseBackupStore(owner, self.recovery_report)

    def sorted_backups(self) -> list[Path]:
        return self.backups.sorted_backups()

    def next_backup_sequence(self) -> int:
        return self.backups.next_backup_sequence()

    def startup_quick_check(self) -> bool:
        db = self.owner
        with storage.hold_managed_directory(db.path.parent):
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{db.path.name}.quick-check-",
                    dir=db.path.parent,
                )
            )
            staged_database = staging / db.path.name
            try:
                for source in (
                    db.path,
                    Path(f"{db.path}-wal"),
                    Path(f"{db.path}-shm"),
                ):
                    if source.exists():
                        shutil.copy2(source, staging / source.name)
                return db._quick_check(staged_database)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def preserve_corrupt_files(self) -> list[Path]:
        db = self.owner
        value = db._timestamp()
        preserved: list[Path] = []
        for source in (db.path, Path(f"{db.path}-wal"), Path(f"{db.path}-shm")):
            if not source.exists():
                continue
            destination = source.with_name(f"{source.name}.corrupt-{value}")
            try:
                os.replace(source, destination)
            except OSError as exc:
                for completed in reversed(preserved):
                    original_name = completed.name.rsplit(f".corrupt-{value}", 1)[0]
                    original = completed.with_name(original_name)
                    try:
                        os.replace(completed, original)
                    except OSError:
                        pass
                raise RuntimeError(
                    f"Could not preserve corrupt database file {source} as {destination}"
                ) from exc
            preserved.append(destination)
        return preserved

    def preserve_stale_sidecars(self) -> list[Path]:
        db = self.owner
        value = db._timestamp()
        preserved: list[Path] = []
        for source in (Path(f"{db.path}-wal"), Path(f"{db.path}-shm")):
            if not source.exists():
                continue
            destination = source.with_name(f"{source.name}.orphan-{value}")
            try:
                os.replace(source, destination)
            except OSError as exc:
                for completed in reversed(preserved):
                    original_name = completed.name.rsplit(f".orphan-{value}", 1)[0]
                    try:
                        os.replace(completed, completed.with_name(original_name))
                    except OSError:
                        pass
                raise RuntimeError(
                    f"Could not preserve stale database sidecar {source} as {destination}"
                ) from exc
            preserved.append(destination)
        return preserved

    def restore_backup(self, backup_path: Path) -> None:
        db = self.owner
        temporary = db.path.with_name(
            f".{db.path.name}.restore-{db._timestamp()}.tmp"
        )
        try:
            storage.validate_managed_write_path(backup_path, backup_path.parent)
            storage.validate_managed_write_path(temporary, db.path.parent)
            with storage.open_managed_binary(
                backup_path,
                "rb",
                db.backup_dir,
                identity_locked=True,
            ) as source, storage.open_managed_binary(
                temporary,
                "xb",
                db.path.parent,
            ) as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            storage.validate_managed_write_path(temporary, db.path.parent)
            with db._hold_atomic_replace_source(
                temporary,
                db.path.parent,
            ) as identity_lock:
                if db._backup_validation_state(temporary) is not BackupValidation.VALID:
                    raise sqlite3.DatabaseError(
                        f"Restored backup failed validation: {backup_path}"
                    )
                storage.validate_managed_write_path(db.path, db.path.parent)
                for sidecar in (Path(f"{db.path}-wal"), Path(f"{db.path}-shm")):
                    if sidecar.exists():
                        storage.delete_managed_file(sidecar, db.path.parent)
                identity_lock.verify()
                os.replace(temporary, db.path)
                try:
                    identity_lock.verify(db.path)
                except BaseException:
                    db._remove_untrusted_publication(db.path, db.path.parent)
                    raise
        finally:
            for cleanup in (
                temporary,
                Path(f"{temporary}-wal"),
                Path(f"{temporary}-shm"),
            ):
                try:
                    if cleanup.exists():
                        storage.delete_managed_file(cleanup, db.path.parent)
                except (OSError, RuntimeError):
                    pass

    def recover_corrupt_database(self) -> None:
        db = self.owner
        preserved = db._preserve_corrupt_files()
        self.recovery_report["preserved_paths"] = [str(path) for path in preserved]
        for backup in db._sorted_backups():
            if not db._backup_is_usable(backup, tolerate_transient_errors=False):
                continue
            db._restore_backup(backup)
            self.recovery_report["action"] = "restored"
            self.recovery_report["backup_path"] = str(backup)
            self.needs_library_rescan = True
            return
        self.recovery_report["action"] = "rebuilt"
        self.needs_library_rescan = True

    def recover_missing_database(self) -> None:
        db = self.owner
        preserved = db._preserve_stale_sidecars()
        self.recovery_report["preserved_paths"].extend(str(path) for path in preserved)
        for backup in db._sorted_backups():
            if not db._backup_is_usable(backup, tolerate_transient_errors=False):
                continue
            db._restore_backup(backup)
            self.recovery_report["action"] = "restored"
            self.recovery_report["backup_path"] = str(backup)
            self.needs_library_rescan = True
            return
        self.recovery_report["action"] = "rebuilt"
        self.needs_library_rescan = True

    def create_backup(self) -> Path:
        return self.backups.create_backup()

    def create_backup_inner(self) -> Path:
        return self.backups.create_backup_inner()

    def create_backup_locked(self) -> Path:
        return self.backups.create_backup_locked()

    def open_backup_source(self) -> sqlite3.Connection:
        return self.backups.open_backup_source()

    def create_backup_if_changed(self) -> Path | None:
        return self.backups.create_backup_if_changed()

    def backup_state(self) -> dict[str, object]:
        return self.backups.backup_state()

    def record_backup_error(self, message: str) -> None:
        self.backups.record_backup_error(message)

    def recovery_state(self) -> dict[str, object]:
        db = self.owner
        with db._lock:
            return {
                **self.recovery_report,
                "preserved_paths": list(self.recovery_report["preserved_paths"]),
                "needs_library_rescan": self.needs_library_rescan,
            }
