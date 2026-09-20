from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable

from . import storage
from .database_schema import InvalidDatabaseSchema, validate_connection_schema
from .sqlite_leaf_lock import _SQLiteLeafLock


class BackupValidation(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


def backup_sort_key(path: Path) -> tuple[int, int, str]:
    match = re.match(r"backup-(\d{20})-", path.name)
    if match:
        return 1, int(match.group(1)), path.name
    try:
        modified = path.lstat().st_mtime_ns
    except OSError:
        modified = 0
    return 0, modified, path.name


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
            leaf_lock = _SQLiteLeafLock.acquire(
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


def backup_validation_state(
    path: Path,
    *,
    max_backup_bytes: int,
    busy_timeout_ms: int,
    schema_version: int,
    verify_identity: Callable[[Path, BinaryIO], os.stat_result],
    run_quick_check: Callable[[Path], bool],
    corruption_check: Callable[[sqlite3.Error], bool],
) -> BackupValidation:
    connection = None
    try:
        with storage.hold_managed_directory(path.parent), storage.open_managed_binary(
            path, "rb", path.parent, identity_locked=True
        ) as locked_file:
            file_stat = verify_identity(path, locked_file)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size <= 0
                or file_stat.st_size > max_backup_bytes
            ):
                return BackupValidation.INVALID
            if not run_quick_check(path):
                return BackupValidation.INVALID
            verify_identity(path, locked_file)
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=busy_timeout_ms / 1000,
            )
            verify_identity(path, locked_file)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < 1 or version > schema_version:
                return BackupValidation.INVALID
            validate_connection_schema(connection, version)
            verify_identity(path, locked_file)
            return BackupValidation.VALID
    except (InvalidDatabaseSchema, RuntimeError, TypeError, ValueError):
        return BackupValidation.INVALID
    except OSError:
        return BackupValidation.UNKNOWN
    except sqlite3.Error as exc:
        return (
            BackupValidation.INVALID
            if corruption_check(exc)
            else BackupValidation.UNKNOWN
        )
    finally:
        if connection is not None:
            connection.close()


def read_schema_version_read_only(path: Path, *, busy_timeout_ms: int) -> int:
    connection = None
    leaf_lock = _SQLiteLeafLock.acquire(
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


def backup_schema_version(
    path: Path,
    *,
    max_backup_bytes: int,
    busy_timeout_ms: int,
    verify_identity: Callable[[Path, BinaryIO], os.stat_result],
) -> int | None:
    connection = None
    try:
        with storage.hold_managed_directory(path.parent), storage.open_managed_binary(
            path, "rb", path.parent, identity_locked=True
        ) as locked_file:
            file_stat = verify_identity(path, locked_file)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size <= 0
                or file_stat.st_size > max_backup_bytes
            ):
                return None
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=busy_timeout_ms / 1000,
            )
            verify_identity(path, locked_file)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            verify_identity(path, locked_file)
            return version
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            connection.close()


def remove_untrusted_publication(path: Path, managed_root: Path) -> None:
    quarantine = path.with_name(f".{path.name}.untrusted-{secrets.token_hex(8)}")
    with storage.hold_managed_directory(managed_root):
        storage.validate_managed_write_path(path, managed_root)
        storage.validate_managed_write_path(quarantine, managed_root)
        try:
            os.replace(path, quarantine)
        except FileNotFoundError:
            return
        try:
            storage.delete_managed_file(quarantine, managed_root)
        except BaseException:
            raise RuntimeError(
                f"Could not securely remove untrusted publication: {quarantine}"
            )


def managed_backup_hash(
    path: Path,
    *,
    max_backup_bytes: int,
    verify_identity: Callable[[Path, BinaryIO], os.stat_result],
    stream_hash: Callable[[BinaryIO], str],
) -> str:
    with storage.hold_managed_directory(path.parent), storage.open_managed_binary(
        path, "rb", path.parent, identity_locked=True
    ) as handle:
        file_stat = verify_identity(path, handle)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_size > max_backup_bytes
        ):
            raise RuntimeError(f"Backup is not a bounded regular file: {path}")
        digest = stream_hash(handle)
        verify_identity(path, handle)
        return digest


class DatabaseRecoveryManager:
    """Own backup/recovery state and file-lifecycle orchestration for a database."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.backup_lock = threading.Lock()
        self.last_backup_generation = -1
        self.last_backup_path: Path | None = None
        self.last_backup_at: str | None = None
        self.last_backup_error: str | None = None
        self.recovery_report = {
            "action": "none",
            "backup_path": None,
            "preserved_paths": [],
            "backup_error": None,
        }
        self.needs_library_rescan = False

    def sorted_backups(self) -> list[Path]:
        db = self.owner
        return sorted(
            db.backup_dir.glob("backup-*.db"),
            key=db._backup_sort_key,
            reverse=True,
        )

    def next_backup_sequence(self) -> int:
        db = self.owner
        sequences = [
            key[1]
            for path in db.backup_dir.glob("backup-*.db")
            if (key := db._backup_sort_key(path))[0] == 1
        ]
        return max(sequences, default=0) + 1

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
        db = self.owner
        try:
            with self.backup_lock:
                return db._create_backup()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            with db._lock:
                self.last_backup_error = str(exc)
            raise

    def create_backup_inner(self) -> Path:
        db = self.owner
        storage.validate_managed_directory(db.backup_dir, db.path.parent)
        db.backup_dir.mkdir(parents=True, exist_ok=True)
        with storage.hold_managed_directory(db.backup_dir, db.path.parent):
            return db._create_backup_locked()

    def create_backup_locked(self) -> Path:
        db = self.owner
        existing_backups = db._sorted_backups()
        value = db._timestamp()
        sequence = db._next_backup_sequence()
        target = db.backup_dir / f"backup-{sequence:020d}-{value}.db"
        temporary = db.backup_dir / f".backup-{sequence:020d}-{value}.tmp"
        result = target
        try:
            storage.validate_managed_write_path(temporary, db.backup_dir)
            storage.validate_managed_write_path(target, db.backup_dir)
            with storage.hold_managed_directory(db.backup_dir, db.path.parent):
                destination_lock = _SQLiteLeafLock.acquire(
                    temporary,
                    db.backup_dir,
                    create=True,
                    writable=True,
                )
                destination = None
                source = None
                backup_completed = False
                try:
                    destination = sqlite3.connect(temporary)
                    destination_lock.verify()
                    with db._lock:
                        db._assert_database_leaf()
                        backed_up_generation = db._mutation_generation
                    source = db._open_backup_source()
                    source.backup(destination)
                    destination_lock.verify()
                    backup_completed = True
                finally:
                    if source is not None:
                        source.close()
                    if destination is not None:
                        destination.close()
                    destination_lock.close()
                    if not backup_completed:
                        destination_lock.remove_created_path()
            storage.validate_managed_write_path(temporary, db.backup_dir)
            with db._hold_atomic_replace_source(
                temporary,
                db.backup_dir,
            ) as identity_lock:
                if db._backup_validation_state(temporary) is not BackupValidation.VALID:
                    raise sqlite3.DatabaseError("New database backup failed validation")
                identical = False
                if existing_backups:
                    try:
                        identical = (
                            db._backup_validation_state(existing_backups[0])
                            is BackupValidation.VALID
                            and db._managed_backup_hash(temporary)
                            == db._managed_backup_hash(existing_backups[0])
                        )
                    except OSError:
                        pass
                if identical:
                    result = existing_backups[0]
                else:
                    identity_lock.verify()
                    os.replace(temporary, target)
                    try:
                        identity_lock.verify(target)
                    except BaseException:
                        db._remove_untrusted_publication(target, db.backup_dir)
                        raise
        finally:
            for cleanup in (
                temporary,
                Path(f"{temporary}-wal"),
                Path(f"{temporary}-shm"),
            ):
                try:
                    if cleanup.exists():
                        storage.delete_managed_file(cleanup, db.backup_dir)
                except (OSError, RuntimeError):
                    pass
        storage.validate_managed_directory(db.backup_dir, db.path.parent)
        backups = db._sorted_backups()
        validation = {backup: db._backup_validation_state(backup) for backup in backups}
        usable_backups = [
            backup for backup in backups if validation[backup] is BackupValidation.VALID
        ]
        for expired in usable_backups[db.BACKUP_LIMIT :]:
            storage.delete_managed_file(expired, db.backup_dir)
        invalid_current = [
            backup
            for backup in backups
            if validation[backup] is BackupValidation.INVALID
            and (
                (version := db._backup_schema_version(backup)) is None
                or version <= db.SCHEMA_VERSION
            )
        ]
        for expired in invalid_current[db.BACKUP_LIMIT :]:
            storage.delete_managed_file(expired, db.backup_dir)
        with db._lock:
            self.last_backup_generation = backed_up_generation
            self.last_backup_path = result
            self.last_backup_at = value
            self.last_backup_error = None
        return result

    def open_backup_source(self) -> sqlite3.Connection:
        db = self.owner
        source = sqlite3.connect(
            f"{db.path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=db.BUSY_TIMEOUT_MS / 1000,
        )
        source.execute(f"PRAGMA busy_timeout = {db.BUSY_TIMEOUT_MS}")
        return source

    def create_backup_if_changed(self) -> Path | None:
        db = self.owner
        with db._lock:
            if db._mutation_generation == self.last_backup_generation:
                return None
        return db.create_backup()

    def backup_state(self) -> dict[str, object]:
        db = self.owner
        with db._lock:
            return {
                "generation": db._mutation_generation,
                "backed_up_generation": self.last_backup_generation,
                "dirty": db._mutation_generation != self.last_backup_generation,
                "last_backup_path": (
                    str(self.last_backup_path) if self.last_backup_path else None
                ),
                "last_backup_at": self.last_backup_at,
                "last_error": self.last_backup_error,
                "backup_count": len(list(db.backup_dir.glob("backup-*.db"))),
            }

    def record_backup_error(self, message: str) -> None:
        db = self.owner
        with db._lock:
            value = str(message)
            self.last_backup_error = value
            self.recovery_report["backup_error"] = value

    def recovery_state(self) -> dict[str, object]:
        db = self.owner
        with db._lock:
            return {
                **self.recovery_report,
                "preserved_paths": list(self.recovery_report["preserved_paths"]),
                "needs_library_rescan": self.needs_library_rescan,
            }
