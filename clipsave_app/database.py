from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

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
from .database_file_index_store import (
    DatabaseFileIndexStore,
    ImportFileDetails as ImportFileDetails,
    ImportFileResult as ImportFileResult,
    VerifiedIndexedFile as VerifiedIndexedFile,
)
from .database_query_store import DatabaseQueryStore
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
from .library_models import CollectionSummary, LibraryItem, TagSummary
from .sqlite_leaf_lock import SQLiteLeafLock
from .storage import is_under_local_store
from .usage_journal import append_usage, parse_usage


_SQLiteLeafLock = SQLiteLeafLock


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
        self._usage_only_changes = 0
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
        self._queries = DatabaseQueryStore(
            connection=lambda: self.connection,
            lock=self._lock,
            utc_timestamp=self._utc_timestamp,
            summary_content_limit=self.SUMMARY_CONTENT_LIMIT,
            max_search_terms=self.MAX_SEARCH_TERMS,
        )
        self._file_index = DatabaseFileIndexStore(
            connection=lambda: self.connection,
            lock=self._lock,
            transaction=lambda: self._transaction(),
            picture_dir=lambda: self._picture_dir,
            markdown_dir=lambda: self._markdown_dir,
            library_dir=lambda: self._library_dir,
            is_managed_path=self.is_managed_path,
            path_key=self.path_key,
            stream_hash=lambda handle: self._stream_hash(handle),
            utc_timestamp=self._utc_timestamp,
            set_scan_report=lambda report: setattr(self, "last_scan_report", report),
            scan_import_file=lambda *args, **kwargs: self.import_file(*args, **kwargs),
            image_suffixes=self.IMAGE_SUFFIXES,
            max_import_bytes=lambda: MAX_IMPORT_BYTES,
            max_markdown_bytes=lambda: MAX_MARKDOWN_BYTES,
            max_image_pixels=lambda: MAX_IMAGE_PIXELS,
        )

    @property
    def _picture_dir(self) -> Path:
        return self.paths.picture_dir if self.paths is not None else PICTURE_DIR

    @property
    def _markdown_dir(self) -> Path:
        return self.paths.markdown_dir if self.paths is not None else MARKDOWN_DIR

    @property
    def _usage_dir(self) -> Path:
        return self.paths.usage_dir if self.paths is not None else self.path.parent / "Usage"

    @property
    def _library_dir(self) -> Path:
        return self.paths.library_dir if self.paths is not None else self._picture_dir.parent

    @property
    def library_dir(self) -> Path:
        """Root directory whose files are managed by this database instance."""
        return self._library_dir

    @property
    def picture_dir(self) -> Path:
        return self._picture_dir

    def is_managed_path(self, path: Path) -> bool:
        """Return whether *path* belongs to this database's managed library."""
        return self._is_under_local_store(path)

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
                connection.execute("ATTACH DATABASE ':memory:' AS usage")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS usage.item_usage("
                    "item_id INTEGER PRIMARY KEY, last_used_at TEXT NOT NULL)"
                )
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
            # Changes to the attached in-memory usage table do not affect the
            # backed-up main database, so they must not mark backups dirty.
            changes_before = self.connection.total_changes - self._usage_only_changes
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
                if self.connection.total_changes - self._usage_only_changes != changes_before:
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
        self._load_usage_journal()

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
    def _utc_usage_timestamp(value: dt.datetime) -> str:
        if value.tzinfo is None:
            value = value.astimezone()
        return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )

    @staticmethod
    def path_key(path: Path | str) -> str:
        value = Path(path).expanduser().resolve(strict=False)
        return os.path.normcase(os.path.normpath(str(value)))

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



    def scan_legacy_files(self, cancel_event: threading.Event | None = None) -> int:
        return self._file_index.scan_legacy_files(cancel_event)

    def scan_unindexed_files(self, cancel_event: threading.Event | None = None) -> int:
        return self._file_index.scan_unindexed_files(cancel_event)


    def import_file(
        self,
        path: Path,
        kind: str | None = None,
        copy_to_library: bool = False,
        *,
        strict: bool = False,
        detailed: bool = False,
    ) -> bool | ImportFileResult | ImportFileDetails:
        return self._file_index.import_file(
            path,
            kind,
            copy_to_library,
            strict=strict,
            detailed=detailed,
        )

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

    def touch_item_used(self, item_id: int, when: dt.datetime | None = None) -> bool:
        moment = when or dt.datetime.now(dt.timezone.utc)
        with self._transaction():
            row = self.connection.execute(
                "SELECT created_at, content_hash, title FROM items WHERE id=? AND missing=0",
                (item_id,),
            ).fetchone()
            if row is None or not row["content_hash"]:
                return False
            self._remember_item_usage(item_id, moment)
        self._write_usage_journal_line(
            (row["created_at"], row["content_hash"], row["title"]), moment
        )
        return True

    def touch_text_by_hash(self, text: str, when: dt.datetime | None = None) -> int | None:
        moment = when or dt.datetime.now(dt.timezone.utc)
        digest = self.text_hash(text)
        with self._transaction():
            row = self.connection.execute(
                "SELECT id, created_at, title FROM items"
                " WHERE kind='text' AND content_hash=? AND missing=0",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            self._remember_item_usage(int(row["id"]), moment)
        self._write_usage_journal_line((row["created_at"], digest, row["title"]), moment)
        return int(row["id"])

    def _remember_item_usage(self, item_id: int, moment: dt.datetime) -> None:
        cursor = self.connection.execute(
            """
            INSERT INTO usage.item_usage(item_id, last_used_at)
            VALUES(?, ?)
            ON CONFLICT(item_id) DO UPDATE
            SET last_used_at=MAX(excluded.last_used_at, item_usage.last_used_at)
            """,
            (item_id, self._utc_usage_timestamp(moment)),
        )
        self._usage_only_changes += max(0, int(cursor.rowcount))

    def _write_usage_journal_line(
        self, event: tuple[str, str, str], moment: dt.datetime
    ) -> None:
        captured_at, content_hash, title = event
        try:
            append_usage(self._usage_dir, captured_at, content_hash, title, moment)
        except OSError:
            pass

    def _load_usage_journal(self) -> None:
        entries = parse_usage(self._usage_dir)
        if not entries:
            return
        with self._transaction():
            self.connection.execute(
                "CREATE TEMP TABLE usage_journal_load("
                "captured_at TEXT, hash_prefix TEXT, used_at TEXT)"
            )
            self.connection.executemany(
                "INSERT INTO usage_journal_load VALUES(?, ?, ?)", entries
            )
            self.connection.execute(
                """
                INSERT INTO usage.item_usage(item_id, last_used_at)
                SELECT i.id, MAX(l.used_at)
                FROM usage_journal_load l
                JOIN items i
                  ON i.created_at = l.captured_at
                 AND i.missing = 0
                 AND i.content_hash LIKE l.hash_prefix || '%'
                GROUP BY i.id
                """
            )
            self.connection.execute("DROP TABLE usage_journal_load")

    def add_image(self, path: Path, created_at: dt.datetime | None = None) -> int | None:
        return self._file_index.add_image(path, created_at)

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
        return self._file_index.add_verified_image(
            path,
            content_hash=content_hash,
            file_size=file_size,
            width=width,
            height=height,
            created_at=created_at,
        )

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
        return self._queries.query_items(
            query=query,
            kind=kind,
            favorite=favorite,
            day=day,
            recent_days=recent_days,
            collection_id=collection_id,
            tag_id=tag_id,
            sort=sort,
            summary_only=summary_only,
            limit=limit,
            offset=offset,
            query_terms=query_terms,
        )

    def count_items(self, *, kind: str | None = None) -> int:
        return self._queries.count_items(kind=kind)

    def count_query_items(
        self,
        query: str = "",
        kind: str | None = None,
        favorite: bool = False,
        day: str | None = None,
        recent_days: int | None = None,
        collection_id: int | None = None,
        tag_id: int | None = None,
        query_terms: Iterable[str] | None = None,
    ) -> int:
        return self._queries.count_query_items(
            query=query,
            kind=kind,
            favorite=favorite,
            day=day,
            recent_days=recent_days,
            collection_id=collection_id,
            tag_id=tag_id,
            query_terms=query_terms,
        )

    def item_ids(self, *, kind: str | None = None, sort: str = "newest") -> list[int]:
        return self._queries.item_ids(kind=kind, sort=sort)

    def get_item(self, item_id: int) -> LibraryItem | None:
        return self._queries.get_item(item_id)

    def counts(self) -> dict[str, int]:
        return self._queries.counts()

    def days(self) -> list[tuple[str, int]]:
        return self._queries.days()

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
        return self._queries.collections()

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
        return self._queries.tags()

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
            cursor = self.connection.execute(
                "DELETE FROM usage.item_usage WHERE item_id=?", (item_id,)
            )
            self._usage_only_changes += max(0, int(cursor.rowcount))

    def mark_item_missing(self, item_id: int) -> None:
        self._file_index.mark_item_missing(item_id)

    def update_ai(self, item_id: int, description: str) -> None:
        with self._transaction():
            self.connection.execute(
                """UPDATE items
                   SET ai_description=?, embedding=NULL, embedding_provider=NULL,
                       embedding_model=NULL, embedding_dimensions=NULL, embedding_revision=NULL
                   WHERE id=?""",
                (description, item_id),
            )

    def update_ai_if_current(
        self, item_id: int, expected_content_hash: str, description: str
    ) -> bool:
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE items
                SET ai_description=?, embedding=NULL, embedding_provider=NULL,
                    embedding_model=NULL, embedding_dimensions=NULL, embedding_revision=NULL
                WHERE id=? AND content_hash=? AND missing=0
                """,
                (description, item_id, expected_content_hash),
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

    def indexed_files(self) -> list[sqlite3.Row]:
        return self._file_index.indexed_files()

    def indexed_file_for_hash(self, digest: str) -> sqlite3.Row | None:
        return self._file_index.indexed_file_for_hash(digest)

    @contextmanager
    def hold_verified_indexed_file(
        self,
        digest: str,
        managed_root: Path,
    ) -> Iterator[VerifiedIndexedFile | None]:
        with self._file_index.hold_verified_indexed_file(
            digest,
            managed_root,
        ) as verified:
            yield verified

    def mark_missing_files(self, cancel_event: threading.Event | None = None) -> None:
        return self._file_index.mark_missing_files(cancel_event)

    def close(self) -> None:
        with self._lock:
            self._close_active_connection()
