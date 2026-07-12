from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

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


class LibraryDatabase:
    SCHEMA_VERSION = 2
    BUSY_TIMEOUT_MS = 30_000
    BACKUP_LIMIT = 3
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

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.repair_report = {"duplicate_path_groups": 0, "duplicate_path_rows": 0}
        self.recovery_report = {
            "action": "none",
            "backup_path": None,
            "preserved_paths": [],
            "backup_error": None,
        }
        self.needs_library_rescan = False
        if self.path.exists() and not self._startup_quick_check():
            self._recover_corrupt_database()
        self.connection = self._open_connection()
        try:
            self.create_schema()
        except UnsupportedSchemaVersion:
            self.connection.close()
            raise
        except (sqlite3.Error, InvalidDatabaseSchema):
            self.connection.close()
            if self.recovery_report["action"] != "none":
                raise
            self._recover_corrupt_database()
            self.connection = self._open_connection()
            try:
                self.create_schema()
            except BaseException:
                self.connection.close()
                raise
        try:
            self.create_backup()
        except (OSError, sqlite3.Error) as exc:
            self.recovery_report["backup_error"] = str(exc)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=self.BUSY_TIMEOUT_MS / 1000,
        )
        connection.row_factory = sqlite3.Row
        with self._lock:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @property
    def backup_dir(self) -> Path:
        return self.path.with_name(f"{self.path.name}.backups")

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    @classmethod
    def _quick_check(cls, path: Path, tolerate_errors: bool = False) -> bool:
        connection = None
        try:
            connection = sqlite3.connect(path, timeout=cls.BUSY_TIMEOUT_MS / 1000)
            rows = connection.execute("PRAGMA quick_check").fetchall()
            return bool(rows) and all(str(row[0]).lower() == "ok" for row in rows)
        except sqlite3.Error as exc:
            error_code = getattr(exc, "sqlite_errorcode", None)
            corruption_codes = {
                getattr(sqlite3, "SQLITE_CORRUPT", 11),
                getattr(sqlite3, "SQLITE_NOTADB", 26),
            }
            message = str(exc).lower()
            if tolerate_errors or error_code in corruption_codes or "malformed" in message or "not a database" in message:
                return False
            raise
        finally:
            if connection is not None:
                connection.close()

    def _startup_quick_check(self) -> bool:
        staging = Path(tempfile.mkdtemp(prefix=f".{self.path.name}.quick-check-", dir=self.path.parent))
        staged_database = staging / self.path.name
        try:
            for source in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                if source.exists():
                    shutil.copy2(source, staging / source.name)
            return self._quick_check(staged_database)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @classmethod
    def _backup_is_usable(cls, path: Path) -> bool:
        if not path.is_file() or not cls._quick_check(path, tolerate_errors=True):
            return False
        connection = None
        try:
            connection = sqlite3.connect(path, timeout=cls.BUSY_TIMEOUT_MS / 1000)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if version > cls.SCHEMA_VERSION or not cls.REQUIRED_COLUMNS.keys() <= tables:
                return False
            for table, required in cls.REQUIRED_COLUMNS.items():
                columns = {
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                compatible = required - ({"resolved_path"} if table == "items" else set())
                if not compatible <= columns:
                    return False
            return True
        except (RuntimeError, TypeError, ValueError, sqlite3.Error):
            return False
        finally:
            if connection is not None:
                connection.close()

    def _preserve_corrupt_files(self) -> list[Path]:
        timestamp = self._timestamp()
        preserved = []
        for source in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not source.exists():
                continue
            destination = source.with_name(f"{source.name}.corrupt-{timestamp}")
            try:
                os.replace(source, destination)
            except OSError as exc:
                raise RuntimeError(
                    f"Could not preserve corrupt database file {source} as {destination}"
                ) from exc
            preserved.append(destination)
        return preserved

    @staticmethod
    def _backup_copy(source_path: Path, destination_path: Path) -> None:
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    def _restore_backup(self, backup_path: Path) -> None:
        temporary = self.path.with_name(f".{self.path.name}.restore-{self._timestamp()}.tmp")
        try:
            self._backup_copy(backup_path, temporary)
            if not self._quick_check(temporary, tolerate_errors=True):
                raise sqlite3.DatabaseError(f"Restored backup failed quick_check: {backup_path}")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
            Path(f"{temporary}-wal").unlink(missing_ok=True)
            Path(f"{temporary}-shm").unlink(missing_ok=True)

    def _recover_corrupt_database(self) -> None:
        preserved = self._preserve_corrupt_files()
        self.recovery_report["preserved_paths"] = [str(path) for path in preserved]
        backups = sorted(self.backup_dir.glob("backup-*.db"), reverse=True)
        for backup in backups:
            if not self._backup_is_usable(backup):
                continue
            try:
                self._restore_backup(backup)
            except (OSError, sqlite3.Error):
                continue
            self.recovery_report["action"] = "restored"
            self.recovery_report["backup_path"] = str(backup)
            self.needs_library_rescan = True
            return
        self.recovery_report["action"] = "rebuilt"
        self.needs_library_rescan = True

    def create_backup(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        existing_backups = sorted(self.backup_dir.glob("backup-*.db"), reverse=True)
        timestamp = self._timestamp()
        target = self.backup_dir / f"backup-{timestamp}.db"
        temporary = self.backup_dir / f".backup-{timestamp}.tmp"
        try:
            destination = sqlite3.connect(temporary)
            try:
                with self._lock:
                    self.connection.backup(destination)
            finally:
                destination.close()
            if not self._backup_is_usable(temporary):
                raise sqlite3.DatabaseError("New database backup failed validation")
            if existing_backups:
                try:
                    if self.file_hash(temporary) == self.file_hash(existing_backups[0]):
                        return existing_backups[0]
                except OSError:
                    pass
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
            Path(f"{temporary}-wal").unlink(missing_ok=True)
            Path(f"{temporary}-shm").unlink(missing_ok=True)
        backups = sorted(self.backup_dir.glob("backup-*.db"), reverse=True)
        for expired in backups[self.BACKUP_LIMIT :]:
            expired.unlink(missing_ok=True)
        return target

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def create_schema(self) -> None:
        with self._lock:
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"Database schema version {version} is newer than supported version {self.SCHEMA_VERSION}"
                )
            if version == self.SCHEMA_VERSION:
                self._validate_schema()
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('image','text','markdown')),
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    path TEXT,
                    resolved_path TEXT,
                    mime TEXT,
                    content_hash TEXT UNIQUE,
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
                );
                CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
                CREATE INDEX IF NOT EXISTS idx_items_collection ON items(collection_id);
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS item_tags (
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY(item_id, tag_id)
                );
                """
            )
            columns = {
                row[1] for row in self.connection.execute('PRAGMA table_info("items")').fetchall()
            }
            if "resolved_path" not in columns:
                self.connection.execute("ALTER TABLE items ADD COLUMN resolved_path TEXT")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_resolved_path ON items(resolved_path)"
            )
            self.repair_report = self._repair_resolved_paths_locked()
            self._validate_schema()
            if version < self.SCHEMA_VERSION:
                self.connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self.connection.commit()

    def _validate_schema(self) -> None:
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = self.REQUIRED_COLUMNS.keys() - tables
        if missing_tables:
            raise InvalidDatabaseSchema(f"Database schema is missing tables: {sorted(missing_tables)}")
        for table, required in self.REQUIRED_COLUMNS.items():
            columns = {
                row[1]
                for row in self.connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            missing_columns = required - columns
            if missing_columns:
                raise InvalidDatabaseSchema(
                    f"Database schema table {table!r} is missing columns: {sorted(missing_columns)}"
                )

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

    @staticmethod
    def _remove_failed_copy(path: Path | None) -> None:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def scan_legacy_files(self, cancel_event: threading.Event | None = None) -> int:
        added = 0
        image_patterns = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp", "*.gif")
        for pattern in image_patterns:
            for path in PICTURE_DIR.rglob(pattern):
                if cancel_event is not None and cancel_event.is_set():
                    return added
                try:
                    added += int(self.import_file(path, "image"))
                except Exception:
                    continue
        for path in MARKDOWN_DIR.rglob("*.md"):
            if cancel_event is not None and cancel_event.is_set():
                return added
            try:
                added += int(self.import_file(path, "markdown"))
            except Exception:
                continue
        return added

    def scan_unindexed_images(self, cancel_event: threading.Event | None = None) -> int:
        with self._lock:
            indexed_paths = {
                row[0]
                for row in self.connection.execute(
                    "SELECT resolved_path FROM items WHERE kind='image' AND resolved_path IS NOT NULL"
                ).fetchall()
            }
        added = 0
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp", "*.gif"):
            for path in PICTURE_DIR.rglob(pattern):
                if cancel_event is not None and cancel_event.is_set():
                    return added
                try:
                    path_key = self._path_key(path)
                    if path_key in indexed_paths:
                        continue
                    if self.import_file(path, "image"):
                        added += 1
                        indexed_paths.add(path_key)
                except Exception:
                    continue
        return added

    def import_file(self, path: Path, kind: str | None = None, copy_to_library: bool = False) -> bool:
        copied_path: Path | None = None
        try:
            path = path.resolve()
            if not path.exists() or not path.is_file():
                return False
            kind = kind or ("markdown" if path.suffix.lower() == ".md" else "image")
            if kind not in {"image", "markdown"}:
                return False
            source_stat = path.stat()
            if source_stat.st_size > MAX_IMPORT_BYTES or (
                kind == "markdown" and source_stat.st_size > MAX_MARKDOWN_BYTES
            ):
                return False

            width = height = None
            if kind == "image":
                try:
                    with Image.open(path) as image:
                        width, height = image.size
                        if width * height > MAX_IMAGE_PIXELS:
                            return False
                except (OSError, ValueError):
                    return False

            source_hash = self.file_hash(path)
            if copy_to_library and not is_under_local_store(path):
                target_dir = MARKDOWN_DIR / "Imported" if kind == "markdown" else PICTURE_DIR / "Imported"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / path.name
                stem, suffix = path.stem, path.suffix
                index = 2
                while target.exists():
                    try:
                        if self.file_hash(target) == source_hash:
                            break
                    except OSError:
                        pass
                    target = target_dir / f"{stem} ({index}){suffix}"
                    index += 1
                if path != target.resolve():
                    if not target.exists():
                        shutil.copy2(path, target)
                        copied_path = target
                    if self.file_hash(target) != source_hash:
                        self._remove_failed_copy(copied_path)
                        return False
                path = target.resolve()

            stat = path.stat()
            content = ""
            if kind == "markdown":
                content = path.read_text(encoding="utf-8", errors="replace")
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
            duplicate_copy = False
            with self._transaction():
                existing_path = self.connection.execute(
                    """
                    SELECT i.id, i.content_hash
                    FROM items i
                    WHERE i.resolved_path = ?
                    ORDER BY
                        CASE WHEN i.content_hash = ? THEN 0 ELSE 1 END,
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
                    if existing_path["content_hash"] != source_hash:
                        duplicate = self.connection.execute(
                            "SELECT id FROM items WHERE content_hash = ? AND id != ?",
                            (source_hash, existing_path["id"]),
                        ).fetchone()
                        if duplicate:
                            self.connection.execute(
                                "UPDATE items SET path=NULL, resolved_path=NULL, missing=1 WHERE id=?",
                                (existing_path["id"],),
                            )
                        else:
                            self.connection.execute(
                                """
                                UPDATE items
                                SET kind=?, title=?, content=?, path=?, resolved_path=?, mime=?,
                                    content_hash=?, updated_at=?, file_size=?, width=?, height=?,
                                    source=?, external=?, missing=0
                                WHERE id=?
                                """,
                                (
                                    kind,
                                    path.name,
                                    content,
                                    str(path),
                                    resolved_path,
                                    mime,
                                    source_hash,
                                    created,
                                    stat.st_size,
                                    width,
                                    height,
                                    source,
                                    external,
                                    existing_path["id"],
                                ),
                            )
                    else:
                        self.connection.execute(
                            """
                            UPDATE items
                            SET kind=?, title=?, content=?, path=?, resolved_path=?, mime=?,
                                updated_at=?, file_size=?, width=?, height=?, source=?, external=?, missing=0
                            WHERE id=?
                            """,
                            (
                                kind,
                                path.name,
                                content,
                                str(path),
                                resolved_path,
                                mime,
                                created,
                                stat.st_size,
                                width,
                                height,
                                source,
                                external,
                                existing_path["id"],
                            ),
                        )
                else:
                    existing_hash = self.connection.execute(
                        "SELECT id FROM items WHERE content_hash = ?", (source_hash,)
                    ).fetchone()
                    if existing_hash:
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
                self._remove_failed_copy(copied_path)
            return added
        except (OSError, ValueError):
            self._remove_failed_copy(copied_path)
            return False
        except BaseException:
            self._remove_failed_copy(copied_path)
            raise

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
            digest = self.file_hash(path)
            with Image.open(path) as image:
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    return None
            stat = path.stat()
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
                    stat.st_size,
                    width,
                    height,
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def has_content_hash(self, content_hash: str) -> bool:
        with self._lock:
            return self.connection.execute(
                "SELECT 1 FROM items WHERE content_hash=? LIMIT 1", (content_hash,)
            ).fetchone() is not None

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
    ) -> list[sqlite3.Row]:
        clauses = ["i.missing = 0"]
        parameters: list[object] = []
        joins = ""
        if query:
            clauses.append("(i.title LIKE ? OR i.content LIKE ? OR i.ocr_text LIKE ? OR i.ai_description LIKE ? OR i.notes LIKE ?)")
            token = f"%{query}%"
            parameters.extend([token] * 5)
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
        sql = f"""
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
            {joins}
            WHERE {' AND '.join(clauses)}
            GROUP BY i.id
            ORDER BY {orders.get(sort, orders['newest'])}
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
            rows = self.connection.execute("SELECT id,path FROM items WHERE path IS NOT NULL").fetchall()
        updates = []
        for row in rows:
            if cancel_event is not None and cancel_event.is_set():
                return
            updates.append((int(not Path(row["path"]).exists()), row["id"]))
        if cancel_event is not None and cancel_event.is_set():
            return
        with self._transaction():
            for missing, item_id in updates:
                self.connection.execute(
                    "UPDATE items SET missing=? WHERE id=?",
                    (missing, item_id),
                )

    def close(self) -> None:
        with self._lock:
            self.connection.close()
