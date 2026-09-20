from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

from . import storage
from . import database_recovery as _database_recovery
from .app_paths import AppPaths
from .constants import (
    DATABASE_PATH,
    MARKDOWN_DIR,
    MAX_IMAGE_PIXELS,
    MAX_IMPORT_BYTES,
    MAX_MARKDOWN_BYTES,
    PICTURE_DIR,
    TAG_COLORS,
)
from .database_recovery import (
    BackupValidation as BackupValidation,
    DatabaseRecoveryManager,
)
from .database_schema import (
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    SCHEMA_VERSION,
    InvalidDatabaseSchema,
    UnsupportedSchemaVersion,
    create_current_schema,
    migrate_v1_to_v2,
    migrate_v2_to_v3,
    migrate_v3_to_v4,
    migrate_v4_to_v5,
    validate_connection_schema,
)
from .import_staging import prepare_import
from .library_models import CollectionSummary, LibraryItem, TagSummary
from .sqlite_leaf_lock import SQLiteLeafLock
from .storage import is_under_local_store


_SQLiteLeafLock = SQLiteLeafLock


class ImportFileResult(Enum):
    LOCALIZED = "localized"

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class ImportFileDetails:
    added: bool
    localized: bool
    duplicate: bool
    item_id: int | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class VerifiedIndexedFile:
    item_id: int
    path: Path
    size_bytes: int




class LibraryDatabase:
    SCHEMA_VERSION = SCHEMA_VERSION
    BUSY_TIMEOUT_MS = 30_000
    BACKUP_LIMIT = 3
    MAX_BACKUP_BYTES = 8 * 1024 * 1024 * 1024
    SUMMARY_CONTENT_LIMIT = 330
    MAX_SEARCH_TERMS = 16
    IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    REQUIRED_COLUMNS = REQUIRED_COLUMNS
    REQUIRED_INDEXES = REQUIRED_INDEXES

    def __init__(self, path: Path | None = None, *, paths: AppPaths | None = None):
        self.paths = paths
        if path is None:
            path = paths.database_path if paths is not None else DATABASE_PATH
        self.path = storage.normalized_absolute_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._mutation_generation = 0
        self._recovery = DatabaseRecoveryManager(self)
        self._database_leaf_lock: SQLiteLeafLock | None = None
        self._sidecar_leaf_locks: list[SQLiteLeafLock] = []
        self.last_scan_report = {
            "scanned": 0,
            "added": 0,
            "failed": 0,
            "errors": [],
        }
        storage.validate_managed_directory(self.backup_dir, self.path.parent)
        storage.validate_managed_write_path(self.path, self.path.parent)
        primary_exists = self.path.exists()
        primary_empty = primary_exists and self.path.stat().st_size == 0
        if primary_empty:
            self._recover_corrupt_database()
        elif primary_exists and not self._startup_quick_check():
            self._recover_corrupt_database()
        elif not primary_exists and (
            any(self.backup_dir.glob("backup-*.db"))
            or Path(f"{self.path}-wal").exists()
            or Path(f"{self.path}-shm").exists()
        ):
            self._recover_missing_database()
        if self.path.exists():
            schema_version = self._read_schema_version_read_only(self.path)
            if schema_version > self.SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"Database schema version {schema_version} is newer than supported version {self.SCHEMA_VERSION}"
                )
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
            self.record_backup_error(str(exc))

    @property
    def _picture_dir(self) -> Path:
        return self.paths.picture_dir if self.paths is not None else PICTURE_DIR

    @property
    def _markdown_dir(self) -> Path:
        return self.paths.markdown_dir if self.paths is not None else MARKDOWN_DIR

    @property
    def _library_dir(self) -> Path:
        return self.paths.library_dir if self.paths is not None else self._picture_dir.parent

    def _is_under_local_store(self, path: Path) -> bool:
        if self.paths is None:
            return is_under_local_store(path)
        return is_under_local_store(path, self.paths.library_dir)

    def _open_connection(self) -> sqlite3.Connection:
        with storage.hold_managed_directory(self.path.parent):
            storage.validate_managed_write_path(self.path, self.path.parent)
            leaf_lock = SQLiteLeafLock.acquire(
                self.path,
                self.path.parent,
                create=not self.path.exists(),
                writable=True,
            )
            sidecar_locks: list[SQLiteLeafLock] = []
            connection = None
            try:
                leaf_lock.verify()
                for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                    sidecar_locks.append(
                        SQLiteLeafLock.acquire(
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
    ) -> Iterator[SQLiteLeafLock]:
        """Keep a validated source identity open while allowing its atomic rename."""
        lock = SQLiteLeafLock.acquire(
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

    def _recovery_manager(self) -> DatabaseRecoveryManager:
        manager = getattr(self, "_recovery", None)
        if manager is None:
            manager = DatabaseRecoveryManager(self)
            self._recovery = manager
        return manager

    @property
    def recovery_report(self) -> dict[str, object]:
        return self._recovery_manager().recovery_report

    @property
    def needs_library_rescan(self) -> bool:
        return self._recovery_manager().needs_library_rescan

    @staticmethod
    def _backup_sort_key(path: Path) -> tuple[int, int, str]:
        return _database_recovery.backup_sort_key(path)

    def _sorted_backups(self) -> list[Path]:
        return self._recovery_manager().sorted_backups()

    def _next_backup_sequence(self) -> int:
        return self._recovery_manager().next_backup_sequence()

    @staticmethod
    def _timestamp() -> str:
        return _database_recovery.timestamp()

    @classmethod
    def _is_corruption_error(cls, exc: sqlite3.Error) -> bool:
        return _database_recovery.is_corruption_error(exc)

    @staticmethod
    def _verify_locked_file_identity(path: Path, handle) -> os.stat_result:
        return _database_recovery.verify_locked_file_identity(path, handle)

    @classmethod
    def _quick_check(cls, path: Path, tolerate_errors: bool = False) -> bool:
        return _database_recovery.quick_check(
            path,
            busy_timeout_ms=cls.BUSY_TIMEOUT_MS,
            corruption_check=cls._is_corruption_error,
            tolerate_errors=tolerate_errors,
        )

    def _startup_quick_check(self) -> bool:
        return self._recovery_manager().startup_quick_check()

    @classmethod
    def _backup_validation_state(cls, path: Path) -> BackupValidation:
        return _database_recovery.backup_validation_state(
            path,
            max_backup_bytes=cls.MAX_BACKUP_BYTES,
            busy_timeout_ms=cls.BUSY_TIMEOUT_MS,
            schema_version=cls.SCHEMA_VERSION,
            verify_identity=cls._verify_locked_file_identity,
            run_quick_check=cls._quick_check,
            corruption_check=cls._is_corruption_error,
        )

    @classmethod
    def _backup_is_usable(
        cls, path: Path, *, tolerate_transient_errors: bool = True
    ) -> bool:
        state = cls._backup_validation_state(path)
        if state is BackupValidation.UNKNOWN and not tolerate_transient_errors:
            raise OSError(f"Backup validation is temporarily unavailable: {path}")
        return state is BackupValidation.VALID

    @classmethod
    def _read_schema_version_read_only(cls, path: Path) -> int:
        return _database_recovery.read_schema_version_read_only(
            path,
            busy_timeout_ms=cls.BUSY_TIMEOUT_MS,
        )

    @classmethod
    def _backup_schema_version(cls, path: Path) -> int | None:
        return _database_recovery.backup_schema_version(
            path,
            max_backup_bytes=cls.MAX_BACKUP_BYTES,
            busy_timeout_ms=cls.BUSY_TIMEOUT_MS,
            verify_identity=cls._verify_locked_file_identity,
        )

    def _preserve_corrupt_files(self) -> list[Path]:
        return self._recovery_manager().preserve_corrupt_files()

    def _preserve_stale_sidecars(self) -> list[Path]:
        return self._recovery_manager().preserve_stale_sidecars()

    @staticmethod
    def _remove_untrusted_publication(path: Path, managed_root: Path) -> None:
        _database_recovery.remove_untrusted_publication(path, managed_root)

    def _restore_backup(self, backup_path: Path) -> None:
        self._recovery_manager().restore_backup(backup_path)

    def _recover_corrupt_database(self) -> None:
        self._recovery_manager().recover_corrupt_database()

    def _recover_missing_database(self) -> None:
        self._recovery_manager().recover_missing_database()

    def create_backup(self) -> Path:
        return self._recovery_manager().create_backup()

    def _create_backup(self) -> Path:
        return self._recovery_manager().create_backup_inner()

    def _create_backup_locked(self) -> Path:
        return self._recovery_manager().create_backup_locked()

    def _open_backup_source(self) -> sqlite3.Connection:
        return self._recovery_manager().open_backup_source()

    @classmethod
    def _managed_backup_hash(cls, path: Path) -> str:
        return _database_recovery.managed_backup_hash(
            path,
            max_backup_bytes=cls.MAX_BACKUP_BYTES,
            verify_identity=cls._verify_locked_file_identity,
            stream_hash=cls._stream_hash,
        )

    def create_backup_if_changed(self) -> Path | None:
        return self._recovery_manager().create_backup_if_changed()

    def backup_state(self) -> dict[str, object]:
        return self._recovery_manager().backup_state()

    def record_backup_error(self, message: str) -> None:
        self._recovery_manager().record_backup_error(message)

    def recovery_state(self) -> dict[str, object]:
        return self._recovery_manager().recovery_state()

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
                    create_current_schema(self.connection)
                    self.connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            else:
                if version == 0:
                    version = 1
                validate_connection_schema(self.connection, version)
                with self._transaction():
                    if version == 1:
                        migrate_v1_to_v2(
                            self.connection,
                            self._repair_resolved_paths_locked,
                        )
                        version = 2
                    if version == 2:
                        migrate_v2_to_v3(self.connection)
                        version = 3
                    if version == 3:
                        migrate_v3_to_v4(self.connection)
                        version = 4
                    if version == 4:
                        migrate_v4_to_v5(self.connection, self._utc_timestamp)
                        version = 5
                    self.connection.execute(f"PRAGMA user_version = {version}")
            self._repair_resolved_paths_locked()
            self._validate_schema()
            self.connection.commit()

    def _validate_schema(self) -> None:
        if not int(self.connection.execute("PRAGMA foreign_keys").fetchone()[0]):
            raise InvalidDatabaseSchema("Database connection does not enforce foreign keys")
        validate_connection_schema(self.connection, self.SCHEMA_VERSION)

    @staticmethod
    def _utc_timestamp(value: dt.datetime | str | None) -> str:
        if isinstance(value, dt.datetime):
            parsed = value
        else:
            text = str(value or "")
            try:
                parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )


    @staticmethod
    def path_key(path: Path | str) -> str:
        value = Path(path).expanduser().resolve(strict=False)
        return os.path.normcase(os.path.normpath(str(value)))

    @staticmethod
    def _embedding_values(
        embedding: Iterable[float] | None,
        provider: str | None = None,
        model: str | None = None,
        revision: int | None = None,
    ) -> tuple[str | None, str | None, str | None, int | None, int | None]:
        if embedding is None:
            return None, None, None, None, None
        values = list(embedding)
        if not values:
            raise ValueError("Embedding must contain at least one value")
        if provider is None and model is None and revision is None:
            return json.dumps(values), None, None, None, None
        if not provider or not model or type(revision) is not int or revision < 1:
            raise ValueError("Embedding metadata must include provider, model and a positive revision")
        return json.dumps(values), str(provider), str(model), len(values), revision

    def _repair_resolved_paths_locked(self) -> None:
        rows = self.connection.execute(
            "SELECT id, path FROM items WHERE path IS NOT NULL AND resolved_path IS NULL"
        ).fetchall()
        for row in rows:
            try:
                resolved_path = self.path_key(row["path"])
            except (OSError, RuntimeError, ValueError):
                continue
            self.connection.execute(
                "UPDATE items SET resolved_path=? WHERE id=?", (resolved_path, row["id"])
            )

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
        scanned = 0
        failures = 0
        errors: list[str] = []
        for path in storage.iter_safe_files(self._picture_dir, self.IMAGE_SUFFIXES):
            if cancel_event is not None and cancel_event.is_set():
                self.last_scan_report = {"scanned": scanned, "added": added, "failed": failures, "errors": errors}
                return added
            scanned += 1
            try:
                if self.import_file(path, "image"):
                    added += 1
            except Exception as exc:
                failures += 1
                if len(errors) < 8:
                    errors.append(f"{path.name}: {exc}")
        for path in storage.iter_safe_files(self._markdown_dir, (".md",)):
            if cancel_event is not None and cancel_event.is_set():
                self.last_scan_report = {"scanned": scanned, "added": added, "failed": failures, "errors": errors}
                return added
            scanned += 1
            try:
                if self.import_file(path, "markdown"):
                    added += 1
            except Exception as exc:
                failures += 1
                if len(errors) < 8:
                    errors.append(f"{path.name}: {exc}")
        self.last_scan_report = {"scanned": scanned, "added": added, "failed": failures, "errors": errors}
        return added

    def scan_unindexed_files(self, cancel_event: threading.Event | None = None) -> int:
        return self._scan_unindexed_roots(
            (
                (self._picture_dir, self.IMAGE_SUFFIXES, "image"),
                (self._markdown_dir, (".md",), "markdown"),
            ),
            cancel_event,
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
        scanned = 0
        failures = 0
        errors: list[str] = []
        for root, suffixes, kind in roots:
            for path in storage.iter_safe_files(root, suffixes):
                if cancel_event is not None and cancel_event.is_set():
                    self.last_scan_report = {"scanned": scanned, "added": added, "failed": failures, "errors": errors}
                    return added
                scanned += 1
                try:
                    path_key = self.path_key(path)
                    if path_key in indexed_paths:
                        continue
                    if self.import_file(path, kind):
                        added += 1
                        indexed_paths.add(path_key)
                except Exception as exc:
                    failures += 1
                    if len(errors) < 8:
                        errors.append(f"{path.name}: {exc}")
        self.last_scan_report = {"scanned": scanned, "added": added, "failed": failures, "errors": errors}
        return added

    def import_file(
        self,
        path: Path,
        kind: str | None = None,
        copy_to_library: bool = False,
        *,
        strict: bool = False,
        detailed: bool = False,
    ) -> bool | ImportFileResult | ImportFileDetails:
        staged = None
        try:
            staged = prepare_import(
                path,
                kind,
                copy_to_library,
                strict=strict,
                picture_dir=self._picture_dir,
                markdown_dir=self._markdown_dir,
                is_local=self._is_under_local_store,
                stream_hash=self._stream_hash,
                max_import_bytes=MAX_IMPORT_BYTES,
                max_markdown_bytes=MAX_MARKDOWN_BYTES,
                max_image_pixels=MAX_IMAGE_PIXELS,
            )
            if staged is None:
                return False
            created = self._utc_timestamp(staged.created_at)
            resolved_path = self.path_key(staged.path)
            values = (
                staged.kind,
                staged.path.name,
                staged.content,
                str(staged.path),
                resolved_path,
                staged.mime,
                staged.content_hash,
                created,
                created,
                staged.file_size,
                staged.width,
                staged.height,
                staged.source,
                staged.external,
            )

            added = False
            localized = False
            duplicate_copy = False
            result_item_id: int | None = None
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
                    (resolved_path, staged.content_hash),
                ).fetchone()
                if existing_path:
                    existing_hash = self._live_hash_owner_locked(
                        staged.content_hash,
                        staged.kind,
                        int(existing_path["id"]),
                    )
                    if existing_hash:
                        self.connection.execute(
                            "UPDATE items SET path=NULL, resolved_path=NULL, missing=1 WHERE id=?",
                            (existing_path["id"],),
                        )
                        if (
                            copy_to_library
                            and existing_hash["external"]
                            and staged.managed_local
                        ):
                            self._update_file_record_locked(int(existing_hash["id"]), values)
                            localized = True
                            result_item_id = int(existing_hash["id"])
                        else:
                            duplicate_copy = staged.created_copy
                    else:
                        result_item_id = int(existing_path["id"])
                        self._update_file_record_locked(
                            int(existing_path["id"]),
                            values,
                            update_hash=(
                                existing_path["content_hash"] != staged.content_hash
                            ),
                        )
                else:
                    existing_hash = self._live_hash_owner_locked(
                        staged.content_hash,
                        staged.kind,
                    )
                    if existing_hash:
                        if (
                            copy_to_library
                            and existing_hash["external"]
                            and staged.managed_local
                        ):
                            self._update_file_record_locked(int(existing_hash["id"]), values)
                            localized = True
                            result_item_id = int(existing_hash["id"])
                        else:
                            duplicate_copy = staged.created_copy
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
                        if added:
                            result_item_id = int(cursor.lastrowid)
                        duplicate_copy = not added and staged.created_copy
            if duplicate_copy:
                staged.discard_created_copy()
            if detailed:
                return ImportFileDetails(
                    added=added,
                    localized=localized,
                    duplicate=not added and not localized,
                    item_id=result_item_id if added or localized else None,
                    content_hash=staged.content_hash,
                )
            return ImportFileResult.LOCALIZED if localized else added
        except (OSError, ValueError):
            if staged is not None:
                staged.discard_created_copy()
            if strict:
                raise
            return False
        except BaseException:
            if staged is not None:
                staged.discard_created_copy()
            raise
        finally:
            if staged is not None:
                staged.close()

    def add_text(self, text: str, created_at: dt.datetime | None = None) -> int | None:
        created_at = created_at or dt.datetime.now(dt.timezone.utc)
        digest = self.text_hash(text)
        title = next((line.strip() for line in text.splitlines() if line.strip()), "剪贴板文字")[:80]
        timestamp = self._utc_timestamp(created_at)
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
        created_at = created_at or dt.datetime.now(dt.timezone.utc)
        try:
            if self._is_under_local_store(path):
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
        return self.add_verified_image(
            path,
            content_hash=digest,
            file_size=final_stat.st_size,
            width=width,
            height=height,
            created_at=created_at,
        )

    def add_verified_image(
        self,
        path: Path,
        *,
        content_hash: str,
        file_size: int,
        width: int,
        height: int,
        created_at: dt.datetime | None = None,
    ) -> int | None:
        if (
            len(content_hash) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in content_hash)
            or file_size < 0
            or width <= 0
            or height <= 0
            or width * height > MAX_IMAGE_PIXELS
        ):
            raise ValueError("Invalid verified image metadata")
        created_at = created_at or dt.datetime.now(dt.timezone.utc)
        timestamp = self._utc_timestamp(created_at)
        resolved_path = self.path_key(path)
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
                    content_hash.lower(),
                    timestamp,
                    timestamp,
                    file_size,
                    width,
                    height,
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

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
        query_terms: Iterable[str] | None = None,
    ) -> list[LibraryItem]:
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        clauses = ["i.missing = 0"]
        parameters: list[object] = []
        joins = ""
        if query_terms is None:
            search_terms = [query] if query else []
        else:
            if isinstance(query_terms, (str, bytes)):
                raise TypeError("query_terms must be an iterable of strings")
            search_terms = []
            seen_terms: set[str] = set()
            for value in query_terms:
                if not isinstance(value, str):
                    raise TypeError("query_terms must contain only strings")
                term = value.strip()
                if not term:
                    continue
                key = term.casefold()
                if key in seen_terms:
                    continue
                seen_terms.add(key)
                search_terms.append(term)
                if len(search_terms) > self.MAX_SEARCH_TERMS:
                    raise ValueError(f"query_terms cannot contain more than {self.MAX_SEARCH_TERMS} terms")
            if not search_terms and query:
                search_terms.append(query)
        if search_terms:
            term_clauses = []
            for term in search_terms:
                term_clauses.append(
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
                escaped_term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                token = f"%{escaped_term}%"
                parameters.extend([token] * 6)
            clauses.append(f"({' OR '.join(term_clauses)})")
        if kind:
            clauses.append("i.kind = ?")
            parameters.append(kind)
        if favorite:
            clauses.append("i.favorite = 1")
        if day:
            clauses.append("date(i.created_at, 'localtime') = ?")
            parameters.append(day)
        if recent_days:
            cutoff = self._utc_timestamp(
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=recent_days)
            )
            clauses.append("i.created_at >= ?")
            parameters.append(cutoff)
        if collection_id is not None:
            clauses.append("i.collection_id = ?")
            parameters.append(collection_id)
        if tag_id is not None:
            joins += " JOIN item_tags filter_tags ON filter_tags.item_id = i.id "
            clauses.append("filter_tags.tag_id = ?")
            parameters.append(tag_id)
        projection = "i.*"
        if summary_only:
            projection = f"""
                i.id, i.kind, i.title, substr(i.content,1,{self.SUMMARY_CONTENT_LIMIT}) AS content,
                i.path, i.resolved_path, i.mime, i.content_hash, i.created_at, i.updated_at,
                i.file_size, i.width, i.height, i.source, i.favorite, '' AS notes,
                '' AS ocr_text, '' AS ai_description, NULL AS embedding,
                i.embedding_provider, i.embedding_model, i.embedding_dimensions,
                i.embedding_revision, i.collection_id,
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
            ORDER BY {self._item_order(sort)}
            {pagination}
        """
        with self._lock:
            rows = self.connection.execute(sql, parameters).fetchall()
        return [LibraryItem.from_mapping(row) for row in rows]

    @staticmethod
    def _item_order(sort: str) -> str:
        orders = {
            "newest": "i.created_at DESC, i.id DESC",
            "oldest": "i.created_at ASC, i.id ASC",
            "name": "i.title COLLATE NOCASE ASC, i.id ASC",
            "size": "i.file_size DESC, i.id DESC",
            "type": "i.kind ASC, i.created_at DESC, i.id DESC",
        }
        return orders.get(sort, orders["newest"])

    def count_items(self, *, kind: str | None = None) -> int:
        clauses = ["missing = 0"]
        parameters: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        with self._lock:
            return int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM items WHERE {' AND '.join(clauses)}",
                    parameters,
                ).fetchone()[0]
            )

    def item_ids(self, *, kind: str | None = None, sort: str = "newest") -> list[int]:
        clauses = ["i.missing = 0"]
        parameters: list[object] = []
        if kind is not None:
            clauses.append("i.kind = ?")
            parameters.append(kind)
        with self._lock:
            return [
                int(row[0])
                for row in self.connection.execute(
                    f"""
                    SELECT i.id
                    FROM items i
                    WHERE {' AND '.join(clauses)}
                    ORDER BY {self._item_order(sort)}
                    """,
                    parameters,
                ).fetchall()
            ]

    def get_item(self, item_id: int) -> LibraryItem | None:
        with self._lock:
            row = self.connection.execute(
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
                WHERE i.id = ? AND i.missing = 0
                """,
                (item_id,),
            ).fetchone()
        return None if row is None else LibraryItem.from_mapping(row)

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
                "SELECT date(created_at, 'localtime') day, COUNT(*) amount FROM items WHERE missing=0 GROUP BY day ORDER BY day DESC"
            ).fetchall()]

    def set_favorite(self, item_id: int, value: bool) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET favorite=? WHERE id=?", (int(value), item_id))

    def set_notes(self, item_id: int, notes: str) -> None:
        with self._transaction():
            self.connection.execute(
                "UPDATE items SET notes=?, updated_at=? WHERE id=?",
                (notes, self._utc_timestamp(dt.datetime.now(dt.timezone.utc)), item_id),
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
                    self._utc_timestamp(dt.datetime.now(dt.timezone.utc)),
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

    def collections(self) -> list[CollectionSummary]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT c.*, COUNT(i.id) amount
                FROM collections c
                LEFT JOIN items i ON i.collection_id=c.id AND i.missing=0
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE, c.id
                """
            ).fetchall()
        return [CollectionSummary.from_mapping(row) for row in rows]

    def create_collection(self, name: str) -> int:
        with self._transaction():
            self.connection.execute(
                "INSERT OR IGNORE INTO collections(name,created_at) VALUES(?,?)",
                (name, self._utc_timestamp(dt.datetime.now(dt.timezone.utc))),
            )
            return int(self.connection.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()[0])

    def set_collection(self, item_id: int, collection_id: int | None) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET collection_id=? WHERE id=?", (collection_id, item_id))

    def delete_collection(self, collection_id: int) -> None:
        with self._transaction():
            self.connection.execute("DELETE FROM collections WHERE id=?", (collection_id,))

    def tags(self) -> list[TagSummary]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT t.*, COUNT(i.id) amount
                FROM tags t
                LEFT JOIN item_tags it ON it.tag_id=t.id
                LEFT JOIN items i ON i.id=it.item_id AND i.missing=0
                GROUP BY t.id
                ORDER BY t.name COLLATE NOCASE, t.id
                """
            ).fetchall()
        return [TagSummary.from_mapping(row) for row in rows]

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

    def delete_tag(self, tag_id: int) -> None:
        with self._transaction():
            self.connection.execute("DELETE FROM tags WHERE id=?", (tag_id,))

    def remove_item(self, item_id: int) -> None:
        with self._transaction():
            self.connection.execute("DELETE FROM items WHERE id=?", (item_id,))

    def mark_item_missing(self, item_id: int) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET missing=1 WHERE id=?", (item_id,))

    def update_ai(
        self,
        item_id: int,
        description: str,
        embedding: Iterable[float] | None = None,
        *,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_revision: int | None = None,
    ) -> None:
        embedding_json, provider, model, dimensions, revision = self._embedding_values(
            embedding, embedding_provider, embedding_model, embedding_revision
        )
        with self._transaction():
            self.connection.execute(
                """UPDATE items
                   SET ai_description=?, embedding=?, embedding_provider=?, embedding_model=?,
                       embedding_dimensions=?, embedding_revision=?
                   WHERE id=?""",
                (description, embedding_json, provider, model, dimensions, revision, item_id),
            )

    def update_ai_if_current(
        self,
        item_id: int,
        expected_content_hash: str,
        description: str,
        embedding: Iterable[float] | None = None,
        *,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_revision: int | None = None,
    ) -> bool:
        embedding_json, provider, model, dimensions, revision = self._embedding_values(
            embedding, embedding_provider, embedding_model, embedding_revision
        )
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE items
                SET ai_description=?, embedding=?, embedding_provider=?, embedding_model=?,
                    embedding_dimensions=?, embedding_revision=?
                WHERE id=? AND content_hash=? AND missing=0
                """,
                (
                    description,
                    embedding_json,
                    provider,
                    model,
                    dimensions,
                    revision,
                    item_id,
                    expected_content_hash,
                ),
            )
            return cursor.rowcount == 1

    def update_ocr(self, item_id: int, text: str) -> None:
        with self._transaction():
            self.connection.execute("UPDATE items SET ocr_text=? WHERE id=?", (text, item_id))

    def update_ocr_if_current(
        self, item_id: int, expected_content_hash: str, text: str
    ) -> bool:
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE items
                SET ocr_text=?
                WHERE id=? AND content_hash=? AND missing=0
                """,
                (text, item_id, expected_content_hash),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _embedding_filter(
        embedding_provider: str | None,
        embedding_model: str | None,
        embedding_dimensions: int | None,
        embedding_revision: int | None,
    ) -> tuple[list[str], list[object]]:
        clauses = ["embedding IS NOT NULL", "missing=0"]
        parameters: list[object] = []
        for column, value in (
            ("embedding_provider", embedding_provider),
            ("embedding_model", embedding_model),
            ("embedding_dimensions", embedding_dimensions),
            ("embedding_revision", embedding_revision),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        return clauses, parameters

    def embedded_item_count(
        self,
        *,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        embedding_revision: int | None = None,
    ) -> int:
        clauses, parameters = self._embedding_filter(
            embedding_provider, embedding_model, embedding_dimensions, embedding_revision
        )
        with self._lock:
            return int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM items WHERE {' AND '.join(clauses)}", parameters
                ).fetchone()[0]
            )

    def embedded_items_batch(
        self,
        after_id: int = 0,
        limit: int = 256,
        *,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        embedding_revision: int | None = None,
    ) -> list[sqlite3.Row]:
        bounded_limit = max(1, min(int(limit), 1024))
        clauses, parameters = self._embedding_filter(
            embedding_provider, embedding_model, embedding_dimensions, embedding_revision
        )
        clauses.append("id>?")
        parameters.extend((int(after_id), bounded_limit))
        with self._lock:
            return list(
                self.connection.execute(
                    f"""
                    SELECT id,embedding
                    FROM items
                    WHERE {' AND '.join(clauses)}
                    ORDER BY id
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
            )

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

    @contextmanager
    def hold_verified_indexed_file(
        self,
        digest: str,
        managed_root: Path,
    ) -> Iterator[VerifiedIndexedFile | None]:
        """Hold the DB owner stable while verifying its managed file by identity and hash."""
        with self._lock:
            indexed = self.indexed_file_for_hash(digest)
            if indexed is None:
                yield None
                return
            indexed_path = Path(indexed["path"])
            try:
                with storage.open_managed_binary(
                    indexed_path,
                    "rb",
                    managed_root,
                    identity_locked=True,
                ) as keeper:
                    keeper_stat = os.fstat(keeper.fileno())
                    if self._stream_hash(keeper) != digest:
                        yield None
                        return
                    current = self.indexed_file_for_hash(digest)
                    if (
                        current is None
                        or current["id"] != indexed["id"]
                        or self.path_key(current["path"]) != self.path_key(indexed_path)
                    ):
                        yield None
                        return
                    yield VerifiedIndexedFile(
                        item_id=int(indexed["id"]),
                        path=indexed_path,
                        size_bytes=int(keeper_stat.st_size),
                    )
            except (OSError, RuntimeError):
                yield None

    def mark_missing_files(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT id,path,kind,content_hash,file_size
                FROM items WHERE path IS NOT NULL
                """
            ).fetchall()
        replacements: list[tuple[int, Path, str]] = []
        for row in rows:
            if cancel_event is not None and cancel_event.is_set():
                return
            path = Path(row["path"])
            item_id = int(row["id"])
            kind = str(row["kind"])
            expected_hash = row["content_hash"]
            root = (
                self._markdown_dir if kind == "markdown" else self._picture_dir
            ) if self._is_under_local_store(path) else path.parent
            try:
                with storage.open_managed_binary(
                    path, "rb", root, identity_locked=True
                ) as current_file:
                    stat = os.fstat(current_file.fileno())
                    if stat.st_size > MAX_IMPORT_BYTES:
                        raise OSError("file is no longer importable")
                    current_hash = self._stream_hash(current_file)
            except (OSError, RuntimeError):
                self.mark_item_missing(item_id)
                continue
            if current_hash != expected_hash:
                replacements.append((item_id, path, kind))
                continue
            with self._transaction():
                owner = self._live_hash_owner_locked(expected_hash, kind, item_id)
                self.connection.execute(
                    "UPDATE items SET missing=? WHERE id=?",
                    (1 if owner is not None else 0, item_id),
                )
        for item_id, path, kind in replacements:
            if cancel_event is not None and cancel_event.is_set():
                return
            self.import_file(path, kind)
            root = (
                self._markdown_dir if kind == "markdown" else self._picture_dir
            ) if self._is_under_local_store(path) else path.parent
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
