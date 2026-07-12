from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import ntpath
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

from . import storage
from .constants import (
    DATABASE_PATH,
    MARKDOWN_DIR,
    MAX_IMAGE_PIXELS,
    MAX_IMPORT_BYTES,
    MAX_MARKDOWN_BYTES,
    PICTURE_DIR,
    TAG_COLORS,
)
from .storage import is_under_local_store


class UnsupportedSchemaVersion(RuntimeError):
    pass


class InvalidDatabaseSchema(RuntimeError):
    pass


class BackupValidation(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class ImportFileResult(Enum):
    LOCALIZED = "localized"

    def __bool__(self) -> bool:
        return False

    def __int__(self) -> int:
        return 0


class _SQLiteLeafLock:
    """Hold a managed database leaf stable while SQLite opens it by path."""

    def __init__(
        self,
        path: Path,
        managed_root: Path,
        handle: int,
        identity: tuple[int, int],
        *,
        created: bool,
        writable: bool,
        replaceable: bool,
    ):
        self.path = storage.normalized_absolute_path(path)
        self.managed_root = Path(managed_root)
        self.handle = handle
        self.identity = identity
        self.created = created
        self.writable = writable
        self.replaceable = replaceable

    @classmethod
    def acquire(
        cls,
        path: Path,
        managed_root: Path,
        *,
        create: bool,
        writable: bool,
        replaceable: bool = False,
    ) -> "_SQLiteLeafLock":
        candidate = Path(os.path.abspath(path))
        root = Path(os.path.abspath(managed_root))
        if replaceable and writable:
            raise ValueError("Replaceable SQLite identity locks must be read-only")
        storage.validate_managed_write_path(candidate, root)
        if os.name == "nt":
            access = storage._GENERIC_READ
            if writable:
                access |= storage._GENERIC_WRITE
            share_mode = storage._FILE_SHARE_READ | storage._FILE_SHARE_WRITE
            if replaceable:
                share_mode = storage._FILE_SHARE_READ | storage._FILE_SHARE_DELETE
            creator = None
            handle = None
            try:
                if create:
                    creator = storage._create_file(
                        candidate,
                        access | storage._FILE_READ_ATTRIBUTES,
                        storage._CREATE_NEW,
                        share_mode=share_mode,
                    )
                handle = storage._verified_windows_handle(
                    candidate,
                    root,
                    access,
                    storage._OPEN_EXISTING,
                    share_mode,
                )
            except BaseException:
                if handle is not None:
                    storage._close_handle(handle)
                if creator is not None:
                    storage._close_handle(creator)
                    creator = None
                if create:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                raise
            finally:
                if creator is not None:
                    storage._close_handle(creator)
            try:
                information = storage._file_information(handle)
            except BaseException:
                storage._close_handle(handle)
                raise
            identity = (
                int(information.volume_serial_number),
                (int(information.file_index_high) << 32)
                | int(information.file_index_low),
            )
            return cls(
                candidate,
                root,
                handle,
                identity,
                created=create,
                writable=writable,
                replaceable=replaceable,
            )

        flags = os.O_RDWR if writable else os.O_RDONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        handle = os.open(candidate, flags, 0o600)
        file_stat = os.fstat(handle)
        identity = (int(file_stat.st_dev), int(file_stat.st_ino))
        lock = cls(
            candidate,
            root,
            handle,
            identity,
            created=create,
            writable=writable,
            replaceable=replaceable,
        )
        try:
            lock.verify()
        except BaseException:
            lock.close()
            lock.remove_created_path()
            raise
        return lock

    def verify(self, path: Path | None = None) -> None:
        if self.handle is None:
            raise RuntimeError(f"SQLite leaf identity lock is closed: {self.path}")
        candidate = Path(os.path.abspath(path or self.path))
        storage.validate_managed_write_path(candidate, self.managed_root)
        if os.name == "nt":
            information = storage._file_information(self.handle)
            identity = (
                int(information.volume_serial_number),
                (int(information.file_index_high) << 32)
                | int(information.file_index_low),
            )
            expected_path = storage._normalized_requested_path(candidate)
            if storage._final_path_from_handle(self.handle) != expected_path:
                raise RuntimeError(
                    f"SQLite database leaf identity changed during open: {candidate}"
                )
            if int(information.number_of_links) != 1:
                raise RuntimeError(
                    f"SQLite database leaf has multiple hard links: {candidate}"
                )
            access = storage._GENERIC_READ
            if self.writable:
                access |= storage._GENERIC_WRITE
            probe = storage._verified_windows_handle(
                candidate,
                self.managed_root,
                access,
                storage._OPEN_EXISTING,
                (
                    storage._FILE_SHARE_READ | storage._FILE_SHARE_DELETE
                    if self.replaceable
                    else storage._FILE_SHARE_READ | storage._FILE_SHARE_WRITE
                ),
            )
            try:
                probe_information = storage._file_information(probe)
                probe_identity = (
                    int(probe_information.volume_serial_number),
                    (int(probe_information.file_index_high) << 32)
                    | int(probe_information.file_index_low),
                )
            finally:
                storage._close_handle(probe)
            if identity != self.identity or probe_identity != self.identity:
                raise RuntimeError(
                    f"SQLite database leaf identity changed during open: {candidate}"
                )
            return

        handle_stat = os.fstat(self.handle)
        try:
            path_stat = candidate.stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"SQLite database leaf changed during open: {candidate}"
            ) from exc
        if (
            not stat.S_ISREG(handle_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or handle_stat.st_nlink != 1
            or path_stat.st_nlink != 1
            or (int(handle_stat.st_dev), int(handle_stat.st_ino)) != self.identity
            or (int(path_stat.st_dev), int(path_stat.st_ino)) != self.identity
        ):
            raise RuntimeError(
                f"SQLite database leaf identity changed or has multiple hard links: {candidate}"
            )

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        if os.name == "nt":
            storage._close_handle(handle)
        else:
            os.close(handle)

    def remove_created_path(self) -> None:
        if not self.created:
            return
        try:
            path_stat = self.path.stat(follow_symlinks=False)
        except OSError:
            return
        expected_inode = self.identity[1]
        if int(path_stat.st_ino) == expected_inode:
            try:
                self.path.unlink()
            except OSError:
                pass


class LibraryDatabase:
    SCHEMA_VERSION = 3
    BUSY_TIMEOUT_MS = 30_000
    BACKUP_LIMIT = 3
    MAX_BACKUP_BYTES = 8 * 1024 * 1024 * 1024
    SUMMARY_CONTENT_LIMIT = 330
    IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    REQUIRED_COLUMNS = {
        "collections": {"id", "name", "created_at"},
        "items": {
            "id",
            "kind",
            "title",
            "content",
            "path",
            "resolved_path",
            "mime",
            "content_hash",
            "created_at",
            "updated_at",
            "file_size",
            "width",
            "height",
            "source",
            "favorite",
            "notes",
            "ocr_text",
            "ai_description",
            "embedding",
            "collection_id",
            "external",
            "missing",
        },
        "tags": {"id", "name", "color"},
        "item_tags": {"item_id", "tag_id"},
    }
    REQUIRED_INDEXES = {
        "idx_items_created": (False, ("created_at",), False),
        "idx_items_kind": (False, ("kind",), False),
        "idx_items_collection": (False, ("collection_id",), False),
        "idx_items_resolved_path": (False, ("resolved_path",), False),
        "idx_items_live_text_hash": (True, ("content_hash",), True),
        "idx_items_live_file_hash": (True, ("content_hash",), True),
    }

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = storage.normalized_absolute_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._backup_lock = threading.Lock()
        self._mutation_generation = 0
        self._last_backup_generation = -1
        self._last_backup_path: Path | None = None
        self._last_backup_at: str | None = None
        self._last_backup_error: str | None = None
        self._database_leaf_lock: _SQLiteLeafLock | None = None
        self._sidecar_leaf_locks: list[_SQLiteLeafLock] = []
        self.repair_report = {"duplicate_path_groups": 0, "duplicate_path_rows": 0}
        self.recovery_report = {
            "action": "none",
            "backup_path": None,
            "preserved_paths": [],
            "backup_error": None,
        }
        self.needs_library_rescan = False
        storage.validate_managed_directory(self.backup_dir, self.path.parent)
        storage.validate_managed_write_path(self.path, self.path.parent)
        primary_exists = self.path.exists()
        primary_empty = primary_exists and self.path.stat().st_size == 0
        if primary_empty:
            self._recover_corrupt_database()
        elif primary_exists and not self._startup_quick_check():
            self._recover_corrupt_database()
        elif not primary_exists and any(self.backup_dir.glob("backup-*.db")):
            self._recover_missing_database()
        self.connection = self._open_connection()
        try:
            self.create_schema()
        except UnsupportedSchemaVersion:
            self._close_active_connection()
            raise
        except sqlite3.Error as exc:
            self._close_active_connection()
            if not self._is_corruption_error(exc):
                raise
            if self.recovery_report["action"] != "none":
                raise
            self._recover_corrupt_database()
            self.connection = self._open_connection()
            try:
                self.create_schema()
            except BaseException:
                self._close_active_connection()
                raise
        except InvalidDatabaseSchema:
            self._close_active_connection()
            if self.recovery_report["action"] != "none":
                raise
            self._recover_corrupt_database()
            self.connection = self._open_connection()
            try:
                self.create_schema()
            except BaseException:
                self._close_active_connection()
                raise
        try:
            self.create_backup()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            self.recovery_report["backup_error"] = str(exc)
            self._last_backup_error = str(exc)

    def _open_connection(self) -> sqlite3.Connection:
        with storage.hold_managed_directory(self.path.parent):
            storage.validate_managed_write_path(self.path, self.path.parent)
            leaf_lock = _SQLiteLeafLock.acquire(
                self.path,
                self.path.parent,
                create=not self.path.exists(),
                writable=True,
            )
            sidecar_locks: list[_SQLiteLeafLock] = []
            connection = None
            try:
                leaf_lock.verify()
                for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                    sidecar_locks.append(
                        _SQLiteLeafLock.acquire(
                            sidecar,
                            self.path.parent,
                            create=not sidecar.exists(),
                            writable=True,
                        )
                    )
                connection = sqlite3.connect(
                    self.path,
                    check_same_thread=False,
                    timeout=self.BUSY_TIMEOUT_MS / 1000,
                )
                leaf_lock.verify()
                for sidecar_lock in sidecar_locks:
                    sidecar_lock.verify()
                connection.row_factory = sqlite3.Row
                with self._lock:
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
                    connection.execute("PRAGMA journal_mode = WAL")
                leaf_lock.verify()
                for sidecar_lock in sidecar_locks:
                    sidecar_lock.verify()
                self._database_leaf_lock = leaf_lock
                self._sidecar_leaf_locks = sidecar_locks
                return connection
            except BaseException:
                for sidecar_lock in reversed(sidecar_locks):
                    sidecar_lock.close()
                if connection is not None:
                    connection.close()
                for sidecar_lock in reversed(sidecar_locks):
                    sidecar_lock.remove_created_path()
                leaf_lock.close()
                leaf_lock.remove_created_path()
                raise

    def _assert_database_leaf(self) -> None:
        leaf_lock = self._database_leaf_lock
        if leaf_lock is None:
            raise RuntimeError("SQLite database leaf identity lock is unavailable")
        leaf_lock.verify()

    def _assert_active_database_files(self) -> None:
        self._assert_database_leaf()
        for sidecar_lock in self._sidecar_leaf_locks:
            sidecar_lock.verify()

    @staticmethod
    @contextmanager
    def _hold_atomic_replace_source(
        path: Path, managed_root: Path
    ) -> Iterator[_SQLiteLeafLock]:
        """Keep a validated source identity open while allowing its atomic rename."""
        lock = _SQLiteLeafLock.acquire(
            path,
            managed_root,
            create=False,
            writable=False,
            replaceable=True,
        )
        try:
            lock.verify()
            yield lock
        finally:
            lock.close()

    def _close_active_connection(self) -> None:
        connection = getattr(self, "connection", None)
        leaf_lock = self._database_leaf_lock
        sidecar_locks = self._sidecar_leaf_locks
        self._sidecar_leaf_locks = []
        try:
            for sidecar_lock in reversed(sidecar_locks):
                sidecar_lock.close()
            if connection is not None:
                connection.close()
        finally:
            self._database_leaf_lock = None
            if leaf_lock is not None:
                leaf_lock.close()

    @property
    def backup_dir(self) -> Path:
        return self.path.with_name(f"{self.path.name}.backups")

    @staticmethod
    def _backup_sort_key(path: Path) -> tuple[int, int, str]:
        match = re.match(r"backup-(\d{20})-", path.name)
        if match:
            return 1, int(match.group(1)), path.name
        try:
            modified = path.lstat().st_mtime_ns
        except OSError:
            modified = 0
        return 0, modified, path.name

    def _sorted_backups(self) -> list[Path]:
        return sorted(self.backup_dir.glob("backup-*.db"), key=self._backup_sort_key, reverse=True)

    def _next_backup_sequence(self) -> int:
        sequences = [
            key[1]
            for path in self.backup_dir.glob("backup-*.db")
            if (key := self._backup_sort_key(path))[0] == 1
        ]
        return max(sequences, default=0) + 1

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    @classmethod
    def _is_corruption_error(cls, exc: sqlite3.Error) -> bool:
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

    @staticmethod
    def _verify_locked_file_identity(path: Path, handle) -> os.stat_result:
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

    @classmethod
    def _quick_check(cls, path: Path, tolerate_errors: bool = False) -> bool:
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
                    timeout=cls.BUSY_TIMEOUT_MS / 1000,
                )
                leaf_lock.verify()
                rows = connection.execute("PRAGMA quick_check").fetchall()
                leaf_lock.verify()
                return bool(rows) and all(str(row[0]).lower() == "ok" for row in rows)
        except sqlite3.Error as exc:
            if tolerate_errors or cls._is_corruption_error(exc):
                return False
            raise
        finally:
            if connection is not None:
                connection.close()
            if leaf_lock is not None:
                leaf_lock.close()

    def _startup_quick_check(self) -> bool:
        with storage.hold_managed_directory(self.path.parent):
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{self.path.name}.quick-check-", dir=self.path.parent
                )
            )
            staged_database = staging / self.path.name
            try:
                for source in (
                    self.path,
                    Path(f"{self.path}-wal"),
                    Path(f"{self.path}-shm"),
                ):
                    if source.exists():
                        shutil.copy2(source, staging / source.name)
                return self._quick_check(staged_database)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    @classmethod
    def _backup_validation_state(cls, path: Path) -> BackupValidation:
        connection = None
        try:
            with storage.hold_managed_directory(path.parent), storage.open_managed_binary(
                path, "rb", path.parent, identity_locked=True
            ) as locked_file:
                file_stat = cls._verify_locked_file_identity(path, locked_file)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_size <= 0
                    or file_stat.st_size > cls.MAX_BACKUP_BYTES
                ):
                    return BackupValidation.INVALID
                if not cls._quick_check(path):
                    return BackupValidation.INVALID
                cls._verify_locked_file_identity(path, locked_file)
                connection = sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=cls.BUSY_TIMEOUT_MS / 1000,
                )
                cls._verify_locked_file_identity(path, locked_file)
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 1 or version > cls.SCHEMA_VERSION:
                    return BackupValidation.INVALID
                cls._validate_connection_schema(connection, version)
                cls._verify_locked_file_identity(path, locked_file)
                return BackupValidation.VALID
        except (InvalidDatabaseSchema, RuntimeError, TypeError, ValueError):
            return BackupValidation.INVALID
        except OSError:
            return BackupValidation.UNKNOWN
        except sqlite3.Error as exc:
            return (
                BackupValidation.INVALID
                if cls._is_corruption_error(exc)
                else BackupValidation.UNKNOWN
            )
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _backup_is_usable(
        cls, path: Path, *, tolerate_transient_errors: bool = True
    ) -> bool:
        state = cls._backup_validation_state(path)
        if state is BackupValidation.UNKNOWN and not tolerate_transient_errors:
            raise OSError(f"Backup validation is temporarily unavailable: {path}")
        return state is BackupValidation.VALID

    @classmethod
    def _backup_schema_version(cls, path: Path) -> int | None:
        connection = None
        try:
            with storage.hold_managed_directory(path.parent), storage.open_managed_binary(
                path, "rb", path.parent, identity_locked=True
            ) as locked_file:
                file_stat = cls._verify_locked_file_identity(path, locked_file)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_size <= 0
                    or file_stat.st_size > cls.MAX_BACKUP_BYTES
                ):
                    return None
                connection = sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=cls.BUSY_TIMEOUT_MS / 1000,
                )
                cls._verify_locked_file_identity(path, locked_file)
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                cls._verify_locked_file_identity(path, locked_file)
                return version
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return None
        finally:
            if connection is not None:
                connection.close()

    def _preserve_corrupt_files(self) -> list[Path]:
        timestamp = self._timestamp()
        preserved: list[Path] = []
        for source in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not source.exists():
                continue
            destination = source.with_name(f"{source.name}.corrupt-{timestamp}")
            try:
                os.replace(source, destination)
            except OSError as exc:
                for completed in reversed(preserved):
                    original_name = completed.name.rsplit(f".corrupt-{timestamp}", 1)[0]
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

    def _preserve_stale_sidecars(self) -> list[Path]:
        timestamp = self._timestamp()
        preserved: list[Path] = []
        for source in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not source.exists():
                continue
            destination = source.with_name(f"{source.name}.orphan-{timestamp}")
            try:
                os.replace(source, destination)
            except OSError as exc:
                for completed in reversed(preserved):
                    original_name = completed.name.rsplit(f".orphan-{timestamp}", 1)[0]
                    try:
                        os.replace(completed, completed.with_name(original_name))
                    except OSError:
                        pass
                raise RuntimeError(
                    f"Could not preserve stale database sidecar {source} as {destination}"
                ) from exc
            preserved.append(destination)
        return preserved

    @staticmethod
    def _remove_untrusted_publication(path: Path, managed_root: Path) -> None:
        """Remove a post-publication target whose identity no longer matches."""
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
                # Quarantine removes the trusted database/backup name even when a
                # same-user hardlink race prevents deletion. Atomic exclusion of
                # such link creation would require a custom SQLite VFS.
                raise RuntimeError(
                    f"Could not securely remove untrusted publication: {quarantine}"
                )

    @classmethod
    def _backup_copy(cls, source_path: Path, destination_path: Path) -> None:
        with storage.hold_managed_directory(source_path.parent), storage.hold_managed_directory(
            destination_path.parent
        ), storage.open_managed_binary(
            source_path, "rb", source_path.parent, identity_locked=True
        ) as source_file:
            storage.validate_managed_write_path(source_path, source_path.parent)
            storage.validate_managed_write_path(destination_path, destination_path.parent)
            cls._verify_locked_file_identity(source_path, source_file)
            destination_created = not destination_path.exists()
            destination_lock = _SQLiteLeafLock.acquire(
                destination_path,
                destination_path.parent,
                create=destination_created,
                writable=True,
            )
            source = None
            destination = None
            copied = False
            try:
                source = sqlite3.connect(
                    f"{source_path.resolve().as_uri()}?mode=ro", uri=True
                )
                cls._verify_locked_file_identity(source_path, source_file)
                destination = sqlite3.connect(destination_path)
                destination_lock.verify()
                source.backup(destination)
                destination_lock.verify()
                cls._verify_locked_file_identity(source_path, source_file)
                copied = True
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
                destination_lock.close()
                if destination_created and not copied:
                    destination_lock.remove_created_path()

    def _restore_backup(self, backup_path: Path) -> None:
        temporary = self.path.with_name(f".{self.path.name}.restore-{self._timestamp()}.tmp")
        try:
            storage.validate_managed_write_path(backup_path, backup_path.parent)
            storage.validate_managed_write_path(temporary, self.path.parent)
            with storage.open_managed_binary(
                backup_path, "rb", self.backup_dir, identity_locked=True
            ) as source, storage.open_managed_binary(
                temporary, "xb", self.path.parent
            ) as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            storage.validate_managed_write_path(temporary, self.path.parent)
            with self._hold_atomic_replace_source(
                temporary, self.path.parent
            ) as identity_lock:
                if self._backup_validation_state(temporary) is not BackupValidation.VALID:
                    raise sqlite3.DatabaseError(
                        f"Restored backup failed validation: {backup_path}"
                    )
                storage.validate_managed_write_path(self.path, self.path.parent)
                for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                    if sidecar.exists():
                        storage.delete_managed_file(sidecar, self.path.parent)
                identity_lock.verify()
                os.replace(temporary, self.path)
                try:
                    identity_lock.verify(self.path)
                except BaseException:
                    self._remove_untrusted_publication(self.path, self.path.parent)
                    raise
        finally:
            for cleanup in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
                try:
                    if cleanup.exists():
                        storage.delete_managed_file(cleanup, self.path.parent)
                except (OSError, RuntimeError):
                    pass

    def _recover_corrupt_database(self) -> None:
        preserved = self._preserve_corrupt_files()
        self.recovery_report["preserved_paths"] = [str(path) for path in preserved]
        backups = self._sorted_backups()
        for backup in backups:
            if not self._backup_is_usable(backup, tolerate_transient_errors=False):
                continue
            self._restore_backup(backup)
            self.recovery_report["action"] = "restored"
            self.recovery_report["backup_path"] = str(backup)
            self.needs_library_rescan = True
            return
        self.recovery_report["action"] = "rebuilt"
        self.needs_library_rescan = True

    def _recover_missing_database(self) -> None:
        preserved = self._preserve_stale_sidecars()
        self.recovery_report["preserved_paths"].extend(str(path) for path in preserved)
        for backup in self._sorted_backups():
            if not self._backup_is_usable(backup, tolerate_transient_errors=False):
                continue
            self._restore_backup(backup)
            self.recovery_report["action"] = "restored"
            self.recovery_report["backup_path"] = str(backup)
            self.needs_library_rescan = True
            return
        self.recovery_report["action"] = "rebuilt"
        self.needs_library_rescan = True

    def create_backup(self) -> Path:
        try:
            with self._backup_lock:
                return self._create_backup()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            with self._lock:
                self._last_backup_error = str(exc)
            raise

    def _create_backup(self) -> Path:
        storage.validate_managed_directory(self.backup_dir, self.path.parent)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with storage.hold_managed_directory(self.backup_dir, self.path.parent):
            return self._create_backup_locked()

    def _create_backup_locked(self) -> Path:
        existing_backups = self._sorted_backups()
        timestamp = self._timestamp()
        sequence = self._next_backup_sequence()
        target = self.backup_dir / f"backup-{sequence:020d}-{timestamp}.db"
        temporary = self.backup_dir / f".backup-{sequence:020d}-{timestamp}.tmp"
        result = target
        try:
            storage.validate_managed_write_path(temporary, self.backup_dir)
            storage.validate_managed_write_path(target, self.backup_dir)
            with storage.hold_managed_directory(self.backup_dir, self.path.parent):
                destination_lock = _SQLiteLeafLock.acquire(
                    temporary,
                    self.backup_dir,
                    create=True,
                    writable=True,
                )
                destination = None
                backup_completed = False
                try:
                    destination = sqlite3.connect(temporary)
                    destination_lock.verify()
                    with self._lock:
                        self._assert_database_leaf()
                        self.connection.backup(destination)
                        backed_up_generation = self._mutation_generation
                    destination_lock.verify()
                    backup_completed = True
                finally:
                    if destination is not None:
                        destination.close()
                    destination_lock.close()
                    if not backup_completed:
                        destination_lock.remove_created_path()
            storage.validate_managed_write_path(temporary, self.backup_dir)
            with self._hold_atomic_replace_source(
                temporary, self.backup_dir
            ) as identity_lock:
                if self._backup_validation_state(temporary) is not BackupValidation.VALID:
                    raise sqlite3.DatabaseError("New database backup failed validation")
                identical = False
                if existing_backups:
                    try:
                        identical = (
                            self._backup_validation_state(existing_backups[0])
                            is BackupValidation.VALID
                            and self._managed_backup_hash(temporary)
                            == self._managed_backup_hash(existing_backups[0])
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
                        self._remove_untrusted_publication(target, self.backup_dir)
                        raise
        finally:
            for cleanup in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
                try:
                    if cleanup.exists():
                        storage.delete_managed_file(cleanup, self.backup_dir)
                except (OSError, RuntimeError):
                    pass
        storage.validate_managed_directory(self.backup_dir, self.path.parent)
        backups = self._sorted_backups()
        validation = {
            backup: self._backup_validation_state(backup) for backup in backups
        }
        usable_backups = [
            backup
            for backup in backups
            if validation[backup] is BackupValidation.VALID
        ]
        for expired in usable_backups[self.BACKUP_LIMIT :]:
            storage.delete_managed_file(expired, self.backup_dir)
        invalid_current = [
            backup
            for backup in backups
            if validation[backup] is BackupValidation.INVALID
            and (
                (version := self._backup_schema_version(backup)) is None
                or version <= self.SCHEMA_VERSION
            )
        ]
        for expired in invalid_current[self.BACKUP_LIMIT :]:
            storage.delete_managed_file(expired, self.backup_dir)
        with self._lock:
            self._last_backup_generation = backed_up_generation
            self._last_backup_path = result
            self._last_backup_at = timestamp
            self._last_backup_error = None
        return result

    @classmethod
    def _managed_backup_hash(cls, path: Path) -> str:
        with storage.hold_managed_directory(path.parent), storage.open_managed_binary(
            path, "rb", path.parent, identity_locked=True
        ) as handle:
            file_stat = cls._verify_locked_file_identity(path, handle)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size <= 0
                or file_stat.st_size > cls.MAX_BACKUP_BYTES
            ):
                raise RuntimeError(f"Backup is not a bounded regular file: {path}")
            digest = cls._stream_hash(handle)
            cls._verify_locked_file_identity(path, handle)
            return digest

    def create_backup_if_changed(self) -> Path | None:
        with self._lock:
            if self._mutation_generation == self._last_backup_generation:
                return None
        return self.create_backup()

    @property
    def mutation_generation(self) -> int:
        with self._lock:
            return self._mutation_generation

    def backup_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "generation": self._mutation_generation,
                "backed_up_generation": self._last_backup_generation,
                "dirty": self._mutation_generation != self._last_backup_generation,
                "last_backup_path": str(self._last_backup_path) if self._last_backup_path else None,
                "last_backup_at": self._last_backup_at,
                "last_error": self._last_backup_error,
                "backup_count": len(list(self.backup_dir.glob("backup-*.db"))),
            }

    def recovery_state(self) -> dict[str, object]:
        with self._lock:
            return {
                **self.recovery_report,
                "preserved_paths": list(self.recovery_report["preserved_paths"]),
                "needs_library_rescan": self.needs_library_rescan,
            }

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._assert_active_database_files()
            changes_before = self.connection.total_changes
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                # A same-user process can add a hard link between checks; making that
                # atomic requires a custom SQLite VFS. Recheck after SQLite takes its
                # write lock and fail closed when the race is detected.
                self._assert_active_database_files()
            except BaseException:
                try:
                    self.connection.rollback()
                finally:
                    self._close_active_connection()
                raise
            try:
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                try:
                    self._assert_active_database_files()
                except BaseException:
                    try:
                        self.connection.rollback()
                    finally:
                        self._close_active_connection()
                    raise
                try:
                    self.connection.commit()
                except BaseException:
                    self.connection.rollback()
                    raise
                if self.connection.total_changes != changes_before:
                    self._mutation_generation += 1

    def create_schema(self) -> None:
        with self._lock:
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"Database schema version {version} is newer than supported version {self.SCHEMA_VERSION}"
                )
            tables = {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if version == 0 and not tables:
                with self._transaction():
                    self._create_current_schema_locked()
                    self.connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            else:
                if version == 0:
                    version = 1
                self._validate_connection_schema(self.connection, version)
                with self._transaction():
                    if version == 1:
                        self._migrate_v1_to_v2_locked()
                        version = 2
                    if version == 2:
                        self._migrate_v2_to_v3_locked()
                        version = 3
                    self.connection.execute(f"PRAGMA user_version = {version}")
            self.repair_report = self._repair_resolved_paths_locked()
            self._validate_schema()
            self.connection.commit()

    def _validate_schema(self) -> None:
        if not int(self.connection.execute("PRAGMA foreign_keys").fetchone()[0]):
            raise InvalidDatabaseSchema("Database connection does not enforce foreign keys")
        self._validate_connection_schema(self.connection, self.SCHEMA_VERSION)

    @classmethod
    def _validate_connection_schema(cls, connection: sqlite3.Connection, version: int) -> None:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = cls.REQUIRED_COLUMNS.keys() - tables
        if missing_tables:
            raise InvalidDatabaseSchema(f"Database schema is missing tables: {sorted(missing_tables)}")
        table_info = {
            table: connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            for table in cls.REQUIRED_COLUMNS
        }
        for table, required in cls.REQUIRED_COLUMNS.items():
            columns = {row[1] for row in table_info[table]}
            compatible = required - ({"resolved_path"} if version == 1 and table == "items" else set())
            missing_columns = compatible - columns
            if missing_columns:
                raise InvalidDatabaseSchema(
                    f"Database schema table {table!r} is missing columns: {sorted(missing_columns)}"
                )

        primary_keys = {
            "collections": {"id": 1},
            "items": {"id": 1},
            "tags": {"id": 1},
            "item_tags": {"item_id": 1, "tag_id": 2},
        }
        for table, expected in primary_keys.items():
            actual = {row[1]: int(row[5]) for row in table_info[table] if int(row[5])}
            if actual != expected:
                raise InvalidDatabaseSchema(
                    f"Database schema table {table!r} has invalid primary key columns"
                )

        required_not_null = {
            "collections": {"name", "created_at"},
            "items": {
                "kind", "title", "content", "created_at", "updated_at", "file_size",
                "source", "favorite", "notes", "ocr_text", "ai_description", "external", "missing",
            },
            "tags": {"name", "color"},
            "item_tags": {"item_id", "tag_id"},
        }
        for table, required in required_not_null.items():
            actual = {row[1] for row in table_info[table] if int(row[3])}
            if not required <= actual:
                raise InvalidDatabaseSchema(
                    f"Database schema table {table!r} is missing NOT NULL constraints"
                )

        if version >= 3:
            expected_defaults = {
                "content": "''",
                "file_size": "0",
                "source": "'剪贴板'",
                "favorite": "0",
                "notes": "''",
                "ocr_text": "''",
                "ai_description": "''",
                "external": "0",
                "missing": "0",
            }
            actual_defaults = {row[1]: row[4] for row in table_info["items"]}
            if any(actual_defaults.get(name) != value for name, value in expected_defaults.items()):
                raise InvalidDatabaseSchema("Database schema items table has invalid defaults")

        item_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone()
        item_sql = "".join(str(item_sql_row[0] or "").lower().split()) if item_sql_row else ""
        if "check(kindin('image','text','markdown'))" not in item_sql:
            raise InvalidDatabaseSchema("Database schema items.kind is missing its CHECK constraint")

        cls._require_unique_columns(connection, "collections", ("name",))
        cls._require_unique_columns(connection, "tags", ("name",))
        if version < 3:
            cls._require_unique_columns(connection, "items", ("content_hash",))

        foreign_keys = {
            table: {
                (row[3], row[2], row[4], str(row[6]).upper())
                for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            }
            for table in ("items", "item_tags")
        }
        if ("collection_id", "collections", "id", "SET NULL") not in foreign_keys["items"]:
            raise InvalidDatabaseSchema("Database schema is missing the items collection foreign key")
        required_item_tag_keys = {
            ("item_id", "items", "id", "CASCADE"),
            ("tag_id", "tags", "id", "CASCADE"),
        }
        if not required_item_tag_keys <= foreign_keys["item_tags"]:
            raise InvalidDatabaseSchema("Database schema is missing item_tags cascade foreign keys")

        if version >= 2:
            ordinary = {
                name: spec for name, spec in cls.REQUIRED_INDEXES.items() if not spec[2]
            }
            cls._validate_required_indexes(connection, ordinary)
        if version >= 3:
            cls._validate_required_indexes(connection, cls.REQUIRED_INDEXES)
            for row in connection.execute('PRAGMA index_list("items")').fetchall():
                if not int(row[2]) or int(row[4]):
                    continue
                columns = tuple(
                    index_row[2]
                    for index_row in connection.execute(
                        f'PRAGMA index_info("{row[1]}")'
                    ).fetchall()
                )
                if columns == ("content_hash",):
                    raise InvalidDatabaseSchema(
                        "Database schema still has a global content hash uniqueness constraint"
                    )
            index_sql = {
                row[0]: "".join(str(row[1] or "").lower().split())
                for row in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            if "wherekind='text'andcontent_hashisnotnullandmissing=0" not in index_sql.get(
                "idx_items_live_text_hash", ""
            ):
                raise InvalidDatabaseSchema("Database schema has an invalid live text hash index")
            file_index = index_sql.get("idx_items_live_file_hash", "")
            if (
                "wherekindin('image','markdown')" not in file_index
                or "content_hashisnotnull" not in file_index
                or "missing=0" not in file_index
            ):
                raise InvalidDatabaseSchema("Database schema has an invalid live file hash index")

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise InvalidDatabaseSchema("Database schema contains foreign-key violations")

    @staticmethod
    def _require_unique_columns(
        connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
    ) -> None:
        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            if not int(row[2]):
                continue
            indexed = tuple(
                index_row[2]
                for index_row in connection.execute(f'PRAGMA index_info("{row[1]}")').fetchall()
            )
            if indexed == columns:
                return
        raise InvalidDatabaseSchema(
            f"Database schema table {table!r} is missing a unique constraint on {columns}"
        )

    @staticmethod
    def _validate_required_indexes(
        connection: sqlite3.Connection,
        required: dict[str, tuple[bool, tuple[str, ...], bool]],
    ) -> None:
        indexes = {
            row[1]: (bool(row[2]), bool(row[4]))
            for row in connection.execute('PRAGMA index_list("items")').fetchall()
        }
        for name, (unique, columns, partial) in required.items():
            if indexes.get(name) != (unique, partial):
                raise InvalidDatabaseSchema(f"Database schema is missing required index {name!r}")
            actual_columns = tuple(
                row[2]
                for row in connection.execute(f'PRAGMA index_info("{name}")').fetchall()
            )
            if actual_columns != columns:
                raise InvalidDatabaseSchema(f"Database schema index {name!r} has invalid columns")
        created_xinfo = connection.execute(
            'PRAGMA index_xinfo("idx_items_created")'
        ).fetchall()
        created_key_columns = [row for row in created_xinfo if int(row[5])]
        if created_key_columns and not int(created_key_columns[0][3]):
            raise InvalidDatabaseSchema("Database schema created_at index must be descending")

    def _create_current_schema_locked(self) -> None:
        statements = (
            """CREATE TABLE collections (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('image','text','markdown')),
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                path TEXT,
                resolved_path TEXT,
                mime TEXT,
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                width INTEGER,
                height INTEGER,
                source TEXT NOT NULL DEFAULT '剪贴板',
                favorite INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                ocr_text TEXT NOT NULL DEFAULT '',
                ai_description TEXT NOT NULL DEFAULT '',
                embedding TEXT,
                collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
                external INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0
            )""",
            "CREATE INDEX idx_items_created ON items(created_at DESC)",
            "CREATE INDEX idx_items_kind ON items(kind)",
            "CREATE INDEX idx_items_collection ON items(collection_id)",
            "CREATE INDEX idx_items_resolved_path ON items(resolved_path)",
            """CREATE UNIQUE INDEX idx_items_live_text_hash ON items(content_hash)
               WHERE kind='text' AND content_hash IS NOT NULL AND missing=0""",
            """CREATE UNIQUE INDEX idx_items_live_file_hash ON items(content_hash)
               WHERE kind IN ('image','markdown') AND content_hash IS NOT NULL AND missing=0""",
            """CREATE TABLE tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL
            )""",
            """CREATE TABLE item_tags (
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY(item_id, tag_id)
            )""",
        )
        for statement in statements:
            self.connection.execute(statement)

    def _migrate_v1_to_v2_locked(self) -> None:
        columns = {
            row[1] for row in self.connection.execute('PRAGMA table_info("items")').fetchall()
        }
        if "resolved_path" not in columns:
            self.connection.execute("ALTER TABLE items ADD COLUMN resolved_path TEXT")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_items_collection ON items(collection_id)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_resolved_path ON items(resolved_path)"
        )
        self._repair_resolved_paths_locked()
        self.connection.execute("PRAGMA user_version = 2")

    def _migrate_v2_to_v3_locked(self) -> None:
        item_tags = self.connection.execute("SELECT item_id,tag_id FROM item_tags").fetchall()
        self.connection.execute(
            """CREATE TABLE items_v3 (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('image','text','markdown')),
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                path TEXT,
                resolved_path TEXT,
                mime TEXT,
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                width INTEGER,
                height INTEGER,
                source TEXT NOT NULL DEFAULT '剪贴板',
                favorite INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                ocr_text TEXT NOT NULL DEFAULT '',
                ai_description TEXT NOT NULL DEFAULT '',
                embedding TEXT,
                collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
                external INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0
            )"""
        )
        columns = (
            "id,kind,title,content,path,resolved_path,mime,content_hash,created_at,updated_at,"
            "file_size,width,height,source,favorite,notes,ocr_text,ai_description,embedding,"
            "collection_id,external,missing"
        )
        self.connection.execute(f"INSERT INTO items_v3({columns}) SELECT {columns} FROM items")
        self.connection.execute("DROP TABLE items")
        self.connection.execute("ALTER TABLE items_v3 RENAME TO items")
        self.connection.execute("CREATE INDEX idx_items_created ON items(created_at DESC)")
        self.connection.execute("CREATE INDEX idx_items_kind ON items(kind)")
        self.connection.execute("CREATE INDEX idx_items_collection ON items(collection_id)")
        self.connection.execute("CREATE INDEX idx_items_resolved_path ON items(resolved_path)")
        self.connection.execute(
            """CREATE UNIQUE INDEX idx_items_live_text_hash ON items(content_hash)
               WHERE kind='text' AND content_hash IS NOT NULL AND missing=0"""
        )
        self.connection.execute(
            """CREATE UNIQUE INDEX idx_items_live_file_hash ON items(content_hash)
               WHERE kind IN ('image','markdown') AND content_hash IS NOT NULL AND missing=0"""
        )
        self.connection.executemany(
            "INSERT INTO item_tags(item_id,tag_id) VALUES(?,?)",
            [(row[0], row[1]) for row in item_tags],
        )
        self.connection.execute("PRAGMA user_version = 3")

    @staticmethod
    def _path_key(path: Path | str) -> str:
        value = Path(path).expanduser().resolve(strict=False)
        return os.path.normcase(os.path.normpath(str(value)))

    def _repair_resolved_paths_locked(self, refresh_all: bool = False) -> dict[str, int]:
        where = "path IS NOT NULL" if refresh_all else "path IS NOT NULL AND resolved_path IS NULL"
        rows = self.connection.execute(
            f"SELECT id, path, resolved_path FROM items WHERE {where}"
        ).fetchall()
        for row in rows:
            try:
                resolved_path = self._path_key(row["path"])
            except (OSError, RuntimeError, ValueError):
                continue
            if row["resolved_path"] != resolved_path:
                self.connection.execute(
                    "UPDATE items SET resolved_path=? WHERE id=?", (resolved_path, row["id"])
                )
        duplicates = self.connection.execute(
            """
            SELECT COUNT(*) amount
            FROM items
            WHERE resolved_path IS NOT NULL
            GROUP BY resolved_path
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        return {
            "duplicate_path_groups": len(duplicates),
            "duplicate_path_rows": sum(int(row["amount"]) for row in duplicates),
        }

    def repair_paths(self) -> dict[str, int]:
        with self._transaction():
            self.repair_report = self._repair_resolved_paths_locked(refresh_all=True)
            return dict(self.repair_report)

    @staticmethod
    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _iter_safe_files(cls, root: Path, suffixes: Iterable[str]) -> Iterator[Path]:
        safe_iterator = getattr(storage, "iter_safe_files", None)
        if safe_iterator is not None:
            yield from safe_iterator(root, suffixes)
            return
        normalized_suffixes = {suffix.lower() for suffix in suffixes}
        pending = [Path(root)]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                    is_reparse_point = bool(
                        getattr(stat_result, "st_file_attributes", 0) & 0x0400
                    )
                    is_junction = bool(
                        getattr(entry, "is_junction", lambda: False)()
                    )
                    if entry.is_symlink() or is_reparse_point or is_junction:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in normalized_suffixes:
                        yield Path(entry.path)
                except OSError:
                    continue

    @staticmethod
    def _remove_failed_copy(
        path: Path | None,
        managed_root: Path | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> None:
        if path is not None and managed_root is not None:
            try:
                if path.exists():
                    storage.delete_managed_file(
                        path,
                        managed_root,
                        expected_sha256=expected_sha256,
                        expected_size=expected_size,
                    )
            except (OSError, RuntimeError):
                pass

    @staticmethod
    def _stream_hash(handle) -> str:
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()

    def _live_hash_owner_locked(
        self, digest: str, kind: str, exclude_id: int | None = None
    ) -> sqlite3.Row | None:
        if kind == "text":
            domain_clause = "kind='text'"
        else:
            domain_clause = "kind IN ('image','markdown')"
        parameters: list[object] = [digest]
        exclude_clause = ""
        if exclude_id is not None:
            exclude_clause = "AND id != ?"
            parameters.append(exclude_id)
        return self.connection.execute(
            f"""
            SELECT id,kind,path,resolved_path,external,missing
            FROM items
            WHERE content_hash=? AND missing=0 AND {domain_clause} {exclude_clause}
            ORDER BY id DESC
            LIMIT 1
            """,
            parameters,
        ).fetchone()

    def _update_file_record_locked(
        self, item_id: int, values: tuple[object, ...], update_hash: bool = True
    ) -> None:
        (
            kind,
            title,
            content,
            path,
            resolved_path,
            mime,
            content_hash,
            _created_at,
            updated_at,
            file_size,
            width,
            height,
            source,
            external,
        ) = values
        hash_assignment = "content_hash=:content_hash," if update_hash else ""
        self.connection.execute(
            f"""
            UPDATE items
            SET kind=:kind, title=:title, content=:content, path=:path,
                resolved_path=:resolved_path, mime=:mime, {hash_assignment}
                updated_at=:updated_at, file_size=:file_size, width=:width, height=:height,
                source=:source, external=:external, missing=0
            WHERE id=:item_id
            """,
            {
                "kind": kind,
                "title": title,
                "content": content,
                "path": path,
                "resolved_path": resolved_path,
                "mime": mime,
                "content_hash": content_hash,
                "updated_at": updated_at,
                "file_size": file_size,
                "width": width,
                "height": height,
                "source": source,
                "external": external,
                "item_id": item_id,
            },
        )

    def scan_legacy_files(self, cancel_event: threading.Event | None = None) -> int:
        added = 0
        for path in self._iter_safe_files(PICTURE_DIR, self.IMAGE_SUFFIXES):
            if cancel_event is not None and cancel_event.is_set():
                return added
            try:
                added += int(self.import_file(path, "image"))
            except Exception:
                continue
        for path in self._iter_safe_files(MARKDOWN_DIR, (".md",)):
            if cancel_event is not None and cancel_event.is_set():
                return added
            try:
                added += int(self.import_file(path, "markdown"))
            except Exception:
                continue
        return added

    def scan_unindexed_files(self, cancel_event: threading.Event | None = None) -> int:
        return self._scan_unindexed_roots(
            (
                (PICTURE_DIR, self.IMAGE_SUFFIXES, "image"),
                (MARKDOWN_DIR, (".md",), "markdown"),
            ),
            cancel_event,
        )

    def scan_unindexed_images(self, cancel_event: threading.Event | None = None) -> int:
        """Compatibility wrapper that retains the original image-only behavior."""
        return self._scan_unindexed_roots(
            ((PICTURE_DIR, self.IMAGE_SUFFIXES, "image"),), cancel_event
        )

    def _scan_unindexed_roots(
        self,
        roots: tuple[tuple[Path, tuple[str, ...], str], ...],
        cancel_event: threading.Event | None,
    ) -> int:
        kinds = tuple(kind for _root, _suffixes, kind in roots)
        placeholders = ",".join("?" for _kind in kinds)
        with self._lock:
            indexed_paths = {
                row[0]
                for row in self.connection.execute(
                    f"""
                    SELECT resolved_path FROM items
                    WHERE kind IN ({placeholders}) AND resolved_path IS NOT NULL
                    """,
                    kinds,
                ).fetchall()
            }
        added = 0
        for root, suffixes, kind in roots:
            for path in self._iter_safe_files(root, suffixes):
                if cancel_event is not None and cancel_event.is_set():
                    return added
                try:
                    path_key = self._path_key(path)
                    if path_key in indexed_paths:
                        continue
                    if self.import_file(path, kind):
                        added += 1
                        indexed_paths.add(path_key)
                except Exception:
                    continue
        return added

    def import_file(
        self,
        path: Path,
        kind: str | None = None,
        copy_to_library: bool = False,
        *,
        strict: bool = False,
    ) -> bool | ImportFileResult:
        copied_path: Path | None = None
        copied_root: Path | None = None
        copied_hash: str | None = None
        copied_size: int | None = None
        identity_handle = None
        try:
            path = storage.normalized_absolute_path(path.resolve())
            if not path.exists() or not path.is_file():
                if strict:
                    raise FileNotFoundError(f"Import file does not exist: {path}")
                return False
            kind = kind or ("markdown" if path.suffix.lower() == ".md" else "image")
            if kind not in {"image", "markdown"}:
                if strict:
                    raise ValueError(f"Unsupported import kind: {kind}")
                return False
            source_root = (
                MARKDOWN_DIR if kind == "markdown" else PICTURE_DIR
            ) if is_under_local_store(path) else path.parent
            with storage.open_managed_binary(
                path, "rb", source_root, identity_locked=True
            ) as source_snapshot:
                source_stat = os.fstat(source_snapshot.fileno())
                if source_stat.st_size > MAX_IMPORT_BYTES or (
                    kind == "markdown" and source_stat.st_size > MAX_MARKDOWN_BYTES
                ):
                    if strict:
                        raise ValueError("Import file exceeds the configured size limit")
                    return False
                source_hash = self._stream_hash(source_snapshot)
            if copy_to_library and not is_under_local_store(path):
                managed_root = MARKDOWN_DIR if kind == "markdown" else PICTURE_DIR
                target_dir = managed_root / "Imported"
                storage.validate_managed_write_path(target_dir / "import.tmp", managed_root)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / path.name
                stem, suffix = path.stem, path.suffix
                index = 2
                while True:
                    try:
                        with storage.open_managed_binary(target, "rb", managed_root) as existing:
                            if self._stream_hash(existing) == source_hash:
                                break
                    except FileNotFoundError:
                        temporary = target_dir / f".import-{secrets.token_hex(16)}{suffix}.tmp"
                        copied_path = temporary
                        copied_root = managed_root
                        try:
                            with storage.open_managed_binary(
                                path, "rb", source_root, identity_locked=True
                            ) as source, storage.open_managed_binary(
                                temporary, "xb", managed_root
                            ) as destination:
                                digest = hashlib.sha256()
                                copied_size = 0
                                while chunk := source.read(1024 * 1024):
                                    destination.write(chunk)
                                    digest.update(chunk)
                                    copied_size += len(chunk)
                            copied_hash = digest.hexdigest()
                            if copied_hash != source_hash or copied_size != source_stat.st_size:
                                self._remove_failed_copy(
                                    copied_path, copied_root, copied_hash, copied_size
                                )
                                if strict:
                                    raise RuntimeError("Imported file changed while it was being copied")
                                return False
                            storage.validate_managed_write_path(target, managed_root)
                            if os.name == "nt":
                                os.rename(temporary, target)
                            else:
                                os.link(temporary, target)
                                os.unlink(temporary)
                            copied_path = target
                            break
                        except FileExistsError:
                            self._remove_failed_copy(
                                copied_path, copied_root, copied_hash, copied_size
                            )
                            copied_path = None
                            copied_root = None
                            copied_hash = None
                            copied_size = None
                            continue
                    target = target_dir / f"{stem} ({index}){suffix}"
                    index += 1
                if path != target.resolve(strict=False):
                    try:
                        with storage.open_managed_binary(target, "rb", managed_root) as copied:
                            copied_hash = self._stream_hash(copied)
                    except (OSError, RuntimeError) as exc:
                        self._remove_failed_copy(copied_path, copied_root, copied_hash, copied_size)
                        if strict:
                            raise RuntimeError("Imported copy could not be verified") from exc
                        return False
                    if copied_hash != source_hash:
                        self._remove_failed_copy(copied_path, copied_root, copied_hash, copied_size)
                        if strict:
                            raise RuntimeError("Imported copy hash did not match the source")
                        return False
                path = storage.normalized_absolute_path(target)

            identity_root = (
                MARKDOWN_DIR if kind == "markdown" else PICTURE_DIR
            ) if is_under_local_store(path) else path.parent
            identity_handle = storage.open_managed_binary(
                path, "rb", identity_root, identity_locked=True
            )
            stat = os.fstat(identity_handle.fileno())
            identity_handle.seek(0)
            final_hash = self._stream_hash(identity_handle)
            if final_hash != source_hash or stat.st_size != source_stat.st_size:
                raise ValueError("Imported file changed before it could be indexed")
            width = height = None
            content = ""
            if kind == "image":
                try:
                    with storage.open_managed_binary(
                        path, "rb", identity_root, identity_locked=True
                    ) as image_source:
                        with Image.open(image_source) as image:
                            width, height = image.size
                            if width * height > MAX_IMAGE_PIXELS:
                                raise ValueError("Image file exceeds the configured pixel limit")
                            image.load()
                except (OSError, ValueError):
                    if strict:
                        raise ValueError(f"Image file is invalid or unreadable: {path}")
                    raise ValueError(f"Image file is invalid or unreadable: {path}")
            else:
                identity_handle.seek(0)
                content = identity_handle.read().decode("utf-8", errors="replace")
            managed_local = is_under_local_store(path)
            created = dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
            resolved_path = self._path_key(path)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            source = "导入文件" if copy_to_library else "现有文件"
            external = int(not managed_local)
            values = (
                kind,
                path.name,
                content,
                str(path),
                resolved_path,
                mime,
                source_hash,
                created,
                created,
                stat.st_size,
                width,
                height,
                source,
                external,
            )

            added = False
            localized = False
            duplicate_copy = False
            with self._transaction():
                existing_path = self.connection.execute(
                    """
                    SELECT i.id, i.content_hash, i.external, i.missing
                    FROM items i
                    WHERE i.resolved_path = ?
                    ORDER BY
                        CASE WHEN i.content_hash = ? THEN 0 ELSE 1 END,
                        CASE WHEN i.missing = 0 THEN 0 ELSE 1 END,
                        CASE WHEN i.favorite = 1 THEN 0 ELSE 1 END,
                        CASE WHEN i.notes != '' THEN 0 ELSE 1 END,
                        CASE WHEN i.collection_id IS NOT NULL THEN 0 ELSE 1 END,
                        CASE WHEN EXISTS(
                            SELECT 1 FROM item_tags it WHERE it.item_id = i.id
                        ) THEN 0 ELSE 1 END,
                        i.updated_at DESC,
                        i.id DESC
                    LIMIT 1
                    """,
                    (resolved_path, source_hash),
                ).fetchone()
                if existing_path:
                    existing_hash = self._live_hash_owner_locked(
                        source_hash, kind, int(existing_path["id"])
                    )
                    if existing_hash:
                        self.connection.execute(
                            "UPDATE items SET path=NULL, resolved_path=NULL, missing=1 WHERE id=?",
                            (existing_path["id"],),
                        )
                        if copy_to_library and existing_hash["external"] and managed_local:
                            self._update_file_record_locked(int(existing_hash["id"]), values)
                            localized = True
                        else:
                            duplicate_copy = copied_path is not None
                    else:
                        self._update_file_record_locked(
                            int(existing_path["id"]),
                            values,
                            update_hash=existing_path["content_hash"] != source_hash,
                        )
                else:
                    existing_hash = self._live_hash_owner_locked(source_hash, kind)
                    if existing_hash:
                        if copy_to_library and existing_hash["external"] and managed_local:
                            self._update_file_record_locked(int(existing_hash["id"]), values)
                            localized = True
                        else:
                            duplicate_copy = copied_path is not None
                    else:
                        cursor = self.connection.execute(
                            """
                            INSERT OR IGNORE INTO items(
                                kind,title,content,path,resolved_path,mime,content_hash,
                                created_at,updated_at,file_size,width,height,source,external
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            values,
                        )
                        added = cursor.rowcount == 1
                        duplicate_copy = not added and copied_path is not None
            if duplicate_copy:
                identity_handle.close()
                identity_handle = None
                self._remove_failed_copy(copied_path, copied_root, copied_hash, copied_size)
            return ImportFileResult.LOCALIZED if localized else added
        except (OSError, ValueError) as exc:
            if identity_handle is not None:
                identity_handle.close()
                identity_handle = None
            self._remove_failed_copy(copied_path, copied_root, copied_hash, copied_size)
            if strict:
                raise
            return False
        except BaseException:
            if identity_handle is not None:
                identity_handle.close()
                identity_handle = None
            self._remove_failed_copy(copied_path, copied_root, copied_hash, copied_size)
            raise
        finally:
            if identity_handle is not None:
                identity_handle.close()

    def add_text(self, text: str, created_at: dt.datetime | None = None) -> int | None:
        created_at = created_at or dt.datetime.now()
        digest = self.text_hash(text)
        title = next((line.strip() for line in text.splitlines() if line.strip()), "剪贴板文字")[:80]
        timestamp = created_at.isoformat(timespec="seconds")
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO items(
                    kind,title,content,content_hash,created_at,updated_at,file_size
                ) VALUES('text',?,?,?,?,?,?)
                """,
                (title, text, digest, timestamp, timestamp, len(text.encode("utf-8"))),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def add_image(self, path: Path, created_at: dt.datetime | None = None) -> int | None:
        created_at = created_at or dt.datetime.now()
        try:
            if is_under_local_store(path):
                source = storage.open_managed_binary(
                    path, "rb", storage.LIBRARY_DIR, identity_locked=True
                )
            else:
                source = path.open("rb")
            with source:
                initial_stat = os.fstat(source.fileno())
                digest = self._stream_hash(source)
                source.seek(0)
                with Image.open(source) as image:
                    width, height = image.size
                    if width * height > MAX_IMAGE_PIXELS:
                        return None
                    image.load()
                final_stat = os.fstat(source.fileno())
                identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                if any(
                    getattr(initial_stat, field, None) != getattr(final_stat, field, None)
                    for field in identity_fields
                ):
                    return None
        except (OSError, ValueError):
            return None
        timestamp = created_at.isoformat(timespec="seconds")
        resolved_path = self._path_key(path)
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO items(
                    kind,title,path,resolved_path,mime,content_hash,created_at,updated_at,
                    file_size,width,height
                ) VALUES('image',?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    path.name,
                    str(path.resolve()),
                    resolved_path,
                    mimetypes.guess_type(path.name)[0] or "image/png",
                    digest,
                    timestamp,
                    timestamp,
                    final_stat.st_size,
                    width,
                    height,
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def has_content_hash(self, content_hash: str, kind: str | None = None) -> bool:
        with self._lock:
            if kind is None:
                return self.connection.execute(
                    "SELECT 1 FROM items WHERE content_hash=? AND missing=0 LIMIT 1",
                    (content_hash,),
                ).fetchone() is not None
            return self._live_hash_owner_locked(content_hash, kind) is not None

    def query_items(
        self,
        query: str = "",
        kind: str | None = None,
        favorite: bool = False,
        day: str | None = None,
        recent_days: int | None = None,
        collection_id: int | None = None,
        tag_id: int | None = None,
        sort: str = "newest",
        summary_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        clauses = ["i.missing = 0"]
        parameters: list[object] = []
        joins = ""
        if query:
            clauses.append(
                """(
                    i.title LIKE ? ESCAPE '\\' OR i.content LIKE ? ESCAPE '\\'
                    OR i.ocr_text LIKE ? ESCAPE '\\' OR i.ai_description LIKE ? ESCAPE '\\'
                    OR i.notes LIKE ? ESCAPE '\\'
                    OR EXISTS(
                        SELECT 1
                        FROM item_tags search_link
                        JOIN tags search_tag ON search_tag.id=search_link.tag_id
                        WHERE search_link.item_id=i.id AND search_tag.name LIKE ? ESCAPE '\\'
                    )
                )"""
            )
            escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            token = f"%{escaped_query}%"
            parameters.extend([token] * 6)
        if kind:
            clauses.append("i.kind = ?")
            parameters.append(kind)
        if favorite:
            clauses.append("i.favorite = 1")
        if day:
            clauses.append("substr(i.created_at,1,10) = ?")
            parameters.append(day)
        if recent_days:
            cutoff = (dt.datetime.now() - dt.timedelta(days=recent_days)).isoformat(timespec="seconds")
            clauses.append("i.created_at >= ?")
            parameters.append(cutoff)
        if collection_id is not None:
            clauses.append("i.collection_id = ?")
            parameters.append(collection_id)
        if tag_id is not None:
            joins += " JOIN item_tags filter_tags ON filter_tags.item_id = i.id "
            clauses.append("filter_tags.tag_id = ?")
            parameters.append(tag_id)
        orders = {
            "newest": "i.created_at DESC, i.id DESC",
            "oldest": "i.created_at ASC, i.id ASC",
            "name": "i.title COLLATE NOCASE ASC, i.id ASC",
            "size": "i.file_size DESC, i.id DESC",
            "type": "i.kind ASC, i.created_at DESC, i.id DESC",
        }
        projection = "i.*"
        if summary_only:
            projection = f"""
                i.id, i.kind, i.title, substr(i.content,1,{self.SUMMARY_CONTENT_LIMIT}) AS content,
                i.path, i.resolved_path, i.mime, i.content_hash, i.created_at, i.updated_at,
                i.file_size, i.width, i.height, i.source, i.favorite, '' AS notes,
                '' AS ocr_text, '' AS ai_description, NULL AS embedding, i.collection_id,
                i.external, i.missing
            """
        pagination = ""
        if limit is not None:
            pagination = "LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        elif offset:
            pagination = "LIMIT -1 OFFSET ?"
            parameters.append(offset)
        sql = f"""
            SELECT {projection}, c.name AS collection_name,
                   (SELECT GROUP_CONCAT(name, char(31)) FROM (
                       SELECT tag.name AS name
                       FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                       WHERE link.item_id=i.id ORDER BY tag.id
                   )) AS tag_names,
                   (SELECT GROUP_CONCAT(color, char(31)) FROM (
                       SELECT tag.color AS color
                       FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                       WHERE link.item_id=i.id ORDER BY tag.id
                   )) AS tag_colors
            FROM items i
            LEFT JOIN collections c ON c.id = i.collection_id
            {joins}
            WHERE {' AND '.join(clauses)}
            ORDER BY {orders.get(sort, orders['newest'])}
            {pagination}
        """
        with self._lock:
            return list(self.connection.execute(sql, parameters).fetchall())

    def get_item(self, item_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(
                """
                SELECT i.*, c.name AS collection_name,
                       (SELECT GROUP_CONCAT(name, char(31)) FROM (
                           SELECT tag.name AS name
                           FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                           WHERE link.item_id=i.id ORDER BY tag.id
                       )) AS tag_names,
                       (SELECT GROUP_CONCAT(color, char(31)) FROM (
                           SELECT tag.color AS color
                           FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                           WHERE link.item_id=i.id ORDER BY tag.id
                       )) AS tag_colors
                FROM items i
                LEFT JOIN collections c ON c.id = i.collection_id
                LEFT JOIN item_tags it ON it.item_id = i.id
                LEFT JOIN tags t ON t.id = it.tag_id
                WHERE i.id = ? AND i.missing = 0
                GROUP BY i.id
                """,
                (item_id,),
            ).fetchone()

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT kind, COUNT(*) amount FROM items WHERE missing=0 GROUP BY kind"
            ).fetchall()
            result = {"all": 0, "image": 0, "text": 0, "markdown": 0, "favorite": 0}
            for row in rows:
                result[row["kind"]] = row["amount"]
                result["all"] += row["amount"]
            result["favorite"] = self.connection.execute(
                "SELECT COUNT(*) FROM items WHERE favorite=1 AND missing=0"
            ).fetchone()[0]
            return result

    def days(self) -> list[tuple[str, int]]:
        with self._lock:
            return [(row["day"], row["amount"]) for row in self.connection.execute(
                "SELECT substr(created_at,1,10) day, COUNT(*) amount FROM items WHERE missing=0 GROUP BY day ORDER BY day DESC"
            ).fetchall()]

    def set_favorite(self, item_id: int, value: bool) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET favorite=? WHERE id=?", (int(value), item_id))

    def set_notes(self, item_id: int, notes: str) -> None:
        with self._transaction():
            self.connection.execute(
                "UPDATE items SET notes=?, updated_at=? WHERE id=?",
                (notes, dt.datetime.now().isoformat(timespec="seconds"), item_id),
            )

    def set_notes_if_unchanged(
        self, item_id: int, expected_notes: str, new_notes: str
    ) -> bool:
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE items
                SET notes=?, updated_at=?
                WHERE id=? AND notes=?
                """,
                (
                    new_notes,
                    dt.datetime.now().isoformat(timespec="seconds"),
                    item_id,
                    expected_notes,
                ),
            )
            if cursor.rowcount == 1:
                return True
            row = self.connection.execute(
                "SELECT notes FROM items WHERE id=?", (item_id,)
            ).fetchone()
            return row is not None and (row["notes"] or "") == new_notes

    def collections(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(
                """
                SELECT c.*, COUNT(i.id) amount
                FROM collections c
                LEFT JOIN items i ON i.collection_id=c.id AND i.missing=0
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE, c.id
                """
            ).fetchall())

    def create_collection(self, name: str) -> int:
        with self._transaction():
            self.connection.execute(
                "INSERT OR IGNORE INTO collections(name,created_at) VALUES(?,?)",
                (name, dt.datetime.now().isoformat(timespec="seconds")),
            )
            return int(self.connection.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()[0])

    def set_collection(self, item_id: int, collection_id: int | None) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET collection_id=? WHERE id=?", (collection_id, item_id))

    def tags(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(
                """
                SELECT t.*, COUNT(i.id) amount
                FROM tags t
                LEFT JOIN item_tags it ON it.tag_id=t.id
                LEFT JOIN items i ON i.id=it.item_id AND i.missing=0
                GROUP BY t.id
                ORDER BY t.name COLLATE NOCASE, t.id
                """
            ).fetchall())

    def add_tag(self, item_id: int, name: str) -> int:
        name = name.strip()
        with self._transaction():
            existing = self.connection.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
            if existing:
                tag_id = int(existing[0])
            else:
                color = TAG_COLORS[
                    self.connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] % len(TAG_COLORS)
                ]
                self.connection.execute("INSERT OR IGNORE INTO tags(name,color) VALUES(?,?)", (name, color))
                tag_id = int(self.connection.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0])
            self.connection.execute(
                "INSERT OR IGNORE INTO item_tags(item_id,tag_id) VALUES(?,?)", (item_id, tag_id)
            )
            return tag_id

    def remove_tag(self, item_id: int, tag_name: str) -> None:
        with self._transaction():
            self.connection.execute(
                "DELETE FROM item_tags WHERE item_id=? AND tag_id=(SELECT id FROM tags WHERE name=?)",
                (item_id, tag_name),
            )

    def remove_item(self, item_id: int) -> None:
        with self._transaction():
            self.connection.execute("DELETE FROM items WHERE id=?", (item_id,))

    def mark_item_missing(self, item_id: int) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET missing=1 WHERE id=?", (item_id,))

    def update_ai(self, item_id: int, description: str, embedding: Iterable[float] | None = None) -> None:
        with self._transaction():
            self.connection.execute(
                "UPDATE items SET ai_description=?, embedding=? WHERE id=?",
                (description, json.dumps(list(embedding)) if embedding is not None else None, item_id),
            )

    def update_ocr(self, item_id: int, text: str) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET ocr_text=? WHERE id=?", (text, item_id))

    def embedded_items(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(
                "SELECT id,embedding FROM items WHERE embedding IS NOT NULL AND missing=0"
            ).fetchall())

    def indexed_files(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.connection.execute(
                    """
                    SELECT id,path,resolved_path,content_hash,file_size,updated_at
                    FROM items
                    WHERE path IS NOT NULL AND content_hash IS NOT NULL AND missing=0
                    """
                ).fetchall()
            )

    def indexed_file_for_hash(self, digest: str) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(
                """
                SELECT id,path,resolved_path,content_hash,file_size,updated_at
                FROM items
                WHERE content_hash=? AND path IS NOT NULL AND missing=0
                LIMIT 1
                """,
                (digest,),
            ).fetchone()

    def mark_missing_files(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT id,path,kind,content_hash,file_size
                FROM items WHERE path IS NOT NULL
                """
            ).fetchall()
        missing_candidates: list[tuple[int, Path, str, str]] = []
        present_candidates: list[tuple[int, Path, str, str]] = []
        replacements: list[tuple[int, Path, str]] = []
        for row in rows:
            if cancel_event is not None and cancel_event.is_set():
                return
            path = Path(row["path"])
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_size > MAX_IMPORT_BYTES:
                    missing_candidates.append(
                        (row["id"], path, row["kind"], row["content_hash"])
                    )
                    continue
                digest = self.file_hash(path)
            except (OSError, RuntimeError):
                missing_candidates.append(
                    (row["id"], path, row["kind"], row["content_hash"])
                )
                continue
            if digest == row["content_hash"]:
                present_candidates.append(
                    (row["id"], path, row["kind"], row["content_hash"])
                )
            else:
                replacements.append((row["id"], path, row["kind"]))
        for item_id, path, kind, expected_hash in [
            *missing_candidates,
            *present_candidates,
        ]:
            if cancel_event is not None and cancel_event.is_set():
                return
            root = (
                MARKDOWN_DIR if kind == "markdown" else PICTURE_DIR
            ) if is_under_local_store(path) else path.parent
            try:
                with storage.open_managed_binary(
                    path, "rb", root, identity_locked=True
                ) as current_file:
                    stat = os.fstat(current_file.fileno())
                    if stat.st_size > MAX_IMPORT_BYTES:
                        raise OSError("file is no longer importable")
                    if self._stream_hash(current_file) != expected_hash:
                        replacements.append((item_id, path, kind))
                        continue
                    with self._transaction():
                        owner = self._live_hash_owner_locked(
                            expected_hash, kind, item_id
                        )
                        self.connection.execute(
                            "UPDATE items SET missing=? WHERE id=?",
                            (1 if owner is not None else 0, item_id),
                        )
            except (OSError, RuntimeError):
                self.mark_item_missing(item_id)
        for item_id, path, kind in replacements:
            if cancel_event is not None and cancel_event.is_set():
                return
            self.import_file(path, kind)
            root = (
                MARKDOWN_DIR if kind == "markdown" else PICTURE_DIR
            ) if is_under_local_store(path) else path.parent
            try:
                with storage.open_managed_binary(
                    path, "rb", root, identity_locked=True
                ) as current_file:
                    stat = os.fstat(current_file.fileno())
                    if stat.st_size > MAX_IMPORT_BYTES:
                        raise OSError("replacement is no longer importable")
                    current_disk_hash = self._stream_hash(current_file)
                    with self._lock:
                        current = self.connection.execute(
                            "SELECT content_hash,missing FROM items WHERE id=?", (item_id,)
                        ).fetchone()
            except (OSError, RuntimeError):
                self.mark_item_missing(item_id)
                continue
            if current is not None and (
                current["missing"] or current["content_hash"] != current_disk_hash
            ):
                self.mark_item_missing(item_id)

    def close(self) -> None:
        with self._lock:
            self._close_active_connection()
