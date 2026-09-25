from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image

from . import storage
from .import_staging import prepare_import


_CAPTURE_EXPORT_PATTERN = re.compile(r"^clipboard_\d{4}-\d{2}-\d{2}\.md$")


def _is_capture_export(path: Path) -> bool:
    return bool(_CAPTURE_EXPORT_PATTERN.match(path.name))


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


class DatabaseFileIndexStore:
    """File-backed item import, scan and reconciliation lifecycle."""

    def __init__(
        self,
        *,
        connection: Callable[[], sqlite3.Connection],
        lock: AbstractContextManager,
        transaction: Callable[[], AbstractContextManager],
        picture_dir: Callable[[], Path],
        markdown_dir: Callable[[], Path],
        library_dir: Callable[[], Path],
        is_managed_path: Callable[[Path], bool],
        path_key: Callable[[Path | str], str],
        stream_hash: Callable[[object], str],
        utc_timestamp: Callable[[dt.datetime | str | None], str],
        set_scan_report: Callable[[dict[str, object]], None],
        scan_import_file: Callable[..., object] | None,
        image_suffixes: tuple[str, ...],
        max_import_bytes: Callable[[], int],
        max_markdown_bytes: Callable[[], int],
        max_image_pixels: Callable[[], int],
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._transaction = transaction
        self._picture_dir = picture_dir
        self._markdown_dir = markdown_dir
        self._library_dir = library_dir
        self._is_managed_path = is_managed_path
        self._path_key = path_key
        self._stream_hash = stream_hash
        self._utc_timestamp = utc_timestamp
        self._set_scan_report = set_scan_report
        self._scan_import_file = scan_import_file
        self.image_suffixes = image_suffixes
        self._max_import_bytes = max_import_bytes
        self._max_markdown_bytes = max_markdown_bytes
        self._max_image_pixels = max_image_pixels

    @staticmethod
    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _live_hash_owner_locked(
        self,
        digest: str,
        kind: str,
        exclude_id: int | None = None,
    ) -> sqlite3.Row | None:
        domain_clause = "kind='text'" if kind == "text" else "kind IN ('image','markdown')"
        parameters: list[object] = [digest]
        exclude_clause = ""
        if exclude_id is not None:
            exclude_clause = "AND id != ?"
            parameters.append(exclude_id)
        return self._connection().execute(
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
        self,
        item_id: int,
        values: tuple[object, ...],
        update_hash: bool = True,
    ) -> None:
        (
            kind, title, content, path, resolved_path, mime, content_hash,
            _created_at, updated_at, file_size, width, height, source, external,
        ) = values
        hash_assignment = "content_hash=:content_hash," if update_hash else ""
        self._connection().execute(
            f"""
            UPDATE items
            SET kind=:kind, title=:title, content=:content, path=:path,
                resolved_path=:resolved_path, mime=:mime, {hash_assignment}
                updated_at=:updated_at, file_size=:file_size, width=:width, height=:height,
                source=:source, external=:external, missing=0
            WHERE id=:item_id
            """,
            {
                "kind": kind, "title": title, "content": content, "path": path,
                "resolved_path": resolved_path, "mime": mime,
                "content_hash": content_hash, "updated_at": updated_at,
                "file_size": file_size, "width": width, "height": height,
                "source": source, "external": external, "item_id": item_id,
            },
        )

    def _report(self, scanned: int, added: int, failed: int, errors: list[str]) -> None:
        self._set_scan_report(
            {"scanned": scanned, "added": added, "failed": failed, "errors": errors}
        )

    def scan_legacy_files(self, cancel_event: threading.Event | None = None) -> int:
        added = scanned = failures = 0
        errors: list[str] = []
        for root, suffixes, kind in (
            (self._picture_dir(), self.image_suffixes, "image"),
            (self._markdown_dir(), (".md",), "markdown"),
        ):
            for path in storage.iter_safe_files(root, suffixes):
                if cancel_event is not None and cancel_event.is_set():
                    self._report(scanned, added, failures, errors)
                    return added
                if kind == "markdown" and _is_capture_export(path):
                    continue
                scanned += 1
                try:
                    importer = self._scan_import_file or self.import_file
                    if importer(path, kind):
                        added += 1
                except Exception as exc:
                    failures += 1
                    if len(errors) < 8:
                        errors.append(f"{path.name}: {exc}")
        self._report(scanned, added, failures, errors)
        return added

    def scan_unindexed_files(self, cancel_event: threading.Event | None = None) -> int:
        roots = (
            (self._picture_dir(), self.image_suffixes, "image"),
            (self._markdown_dir(), (".md",), "markdown"),
        )
        kinds = tuple(kind for _root, _suffixes, kind in roots)
        placeholders = ",".join("?" for _kind in kinds)
        with self._lock:
            indexed_paths = {
                row[0]
                for row in self._connection().execute(
                    f"SELECT resolved_path FROM items WHERE kind IN ({placeholders}) AND resolved_path IS NOT NULL",
                    kinds,
                ).fetchall()
            }
        added = scanned = failures = 0
        errors: list[str] = []
        for root, suffixes, kind in roots:
            for path in storage.iter_safe_files(root, suffixes):
                if cancel_event is not None and cancel_event.is_set():
                    self._report(scanned, added, failures, errors)
                    return added
                if kind == "markdown" and _is_capture_export(path):
                    continue
                scanned += 1
                try:
                    key = self._path_key(path)
                    if key in indexed_paths:
                        continue
                    importer = self._scan_import_file or self.import_file
                    if importer(path, kind):
                        added += 1
                        indexed_paths.add(key)
                except Exception as exc:
                    failures += 1
                    if len(errors) < 8:
                        errors.append(f"{path.name}: {exc}")
        self._report(scanned, added, failures, errors)
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
                path, kind, copy_to_library, strict=strict,
                picture_dir=self._picture_dir(), markdown_dir=self._markdown_dir(),
                is_local=self._is_managed_path, stream_hash=self._stream_hash,
                max_import_bytes=int(self._max_import_bytes()),
                max_markdown_bytes=int(self._max_markdown_bytes()),
                max_image_pixels=int(self._max_image_pixels()),
            )
            if staged is None:
                return False
            created = self._utc_timestamp(staged.created_at)
            resolved_path = self._path_key(staged.path)
            values = (
                staged.kind, staged.path.name, staged.content, str(staged.path),
                resolved_path, staged.mime, staged.content_hash, created, created,
                staged.file_size, staged.width, staged.height, staged.source,
                staged.external,
            )
            added = localized = duplicate_copy = False
            result_item_id: int | None = None
            with self._transaction():
                connection = self._connection()
                existing_path = connection.execute(
                    """
                    SELECT i.id, i.content_hash, i.external, i.missing
                    FROM items i WHERE i.resolved_path = ?
                    ORDER BY
                        CASE WHEN i.content_hash = ? THEN 0 ELSE 1 END,
                        CASE WHEN i.missing = 0 THEN 0 ELSE 1 END,
                        CASE WHEN i.favorite = 1 THEN 0 ELSE 1 END,
                        CASE WHEN i.notes != '' THEN 0 ELSE 1 END,
                        CASE WHEN i.collection_id IS NOT NULL THEN 0 ELSE 1 END,
                        CASE WHEN EXISTS(SELECT 1 FROM item_tags it WHERE it.item_id = i.id) THEN 0 ELSE 1 END,
                        i.updated_at DESC, i.id DESC
                    LIMIT 1
                    """,
                    (resolved_path, staged.content_hash),
                ).fetchone()
                if existing_path:
                    existing_hash = self._live_hash_owner_locked(
                        staged.content_hash, staged.kind, int(existing_path["id"])
                    )
                    if existing_hash:
                        connection.execute(
                            "UPDATE items SET path=NULL, resolved_path=NULL, missing=1 WHERE id=?",
                            (existing_path["id"],),
                        )
                        if copy_to_library and existing_hash["external"] and staged.managed_local:
                            self._update_file_record_locked(int(existing_hash["id"]), values)
                            localized = True
                            result_item_id = int(existing_hash["id"])
                        else:
                            duplicate_copy = staged.created_copy
                    else:
                        result_item_id = int(existing_path["id"])
                        self._update_file_record_locked(
                            int(existing_path["id"]), values,
                            update_hash=existing_path["content_hash"] != staged.content_hash,
                        )
                else:
                    existing_hash = self._live_hash_owner_locked(staged.content_hash, staged.kind)
                    if existing_hash:
                        if copy_to_library and existing_hash["external"] and staged.managed_local:
                            self._update_file_record_locked(int(existing_hash["id"]), values)
                            localized = True
                            result_item_id = int(existing_hash["id"])
                        else:
                            duplicate_copy = staged.created_copy
                    else:
                        cursor = connection.execute(
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
                    added=added, localized=localized,
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

    def add_image(self, path: Path, created_at: dt.datetime | None = None) -> int | None:
        created_at = created_at or dt.datetime.now(dt.timezone.utc)
        try:
            source = (
                storage.open_managed_binary(
                    path, "rb", self._library_dir(), identity_locked=True
                )
                if self._is_managed_path(path)
                else path.open("rb")
            )
            with source:
                initial_stat = os.fstat(source.fileno())
                digest = self._stream_hash(source)
                source.seek(0)
                with Image.open(source) as image:
                    width, height = image.size
                    if width * height > int(self._max_image_pixels()):
                        return None
                    image.load()
                final_stat = os.fstat(source.fileno())
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                if any(
                    getattr(initial_stat, field, None) != getattr(final_stat, field, None)
                    for field in fields
                ):
                    return None
        except (OSError, ValueError):
            return None
        return self.add_verified_image(
            path, content_hash=digest, file_size=final_stat.st_size,
            width=width, height=height, created_at=created_at,
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
            or any(c not in "0123456789abcdefABCDEF" for c in content_hash)
            or file_size < 0 or width <= 0 or height <= 0
            or width * height > int(self._max_image_pixels())
        ):
            raise ValueError("Invalid verified image metadata")
        created_at = created_at or dt.datetime.now(dt.timezone.utc)
        timestamp = self._utc_timestamp(created_at)
        resolved_path = self._path_key(path)
        with self._transaction():
            cursor = self._connection().execute(
                """
                INSERT OR IGNORE INTO items(
                    kind,title,path,resolved_path,mime,content_hash,created_at,updated_at,
                    file_size,width,height
                ) VALUES('image',?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    path.name, str(path.resolve()), resolved_path,
                    mimetypes.guess_type(path.name)[0] or "image/png",
                    content_hash.lower(), timestamp, timestamp,
                    file_size, width, height,
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def indexed_files(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection().execute(
                """
                SELECT id,path,resolved_path,content_hash,file_size,updated_at
                FROM items WHERE path IS NOT NULL AND content_hash IS NOT NULL AND missing=0
                """
            ).fetchall())

    def indexed_file_for_hash(self, digest: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection().execute(
                """
                SELECT id,path,resolved_path,content_hash,file_size,updated_at
                FROM items WHERE content_hash=? AND path IS NOT NULL AND missing=0 LIMIT 1
                """, (digest,),
            ).fetchone()

    @contextmanager
    def hold_verified_indexed_file(
        self, digest: str, managed_root: Path
    ) -> Iterator[VerifiedIndexedFile | None]:
        with self._lock:
            indexed = self.indexed_file_for_hash(digest)
            if indexed is None:
                yield None
                return
            indexed_path = Path(indexed["path"])
            try:
                with storage.open_managed_binary(
                    indexed_path, "rb", managed_root, identity_locked=True
                ) as keeper:
                    keeper_stat = os.fstat(keeper.fileno())
                    if self._stream_hash(keeper) != digest:
                        yield None
                        return
                    current = self.indexed_file_for_hash(digest)
                    if (
                        current is None or current["id"] != indexed["id"]
                        or self._path_key(current["path"]) != self._path_key(indexed_path)
                    ):
                        yield None
                        return
                    yield VerifiedIndexedFile(
                        item_id=int(indexed["id"]), path=indexed_path,
                        size_bytes=int(keeper_stat.st_size),
                    )
            except (OSError, RuntimeError):
                yield None

    def mark_item_missing(self, item_id: int) -> None:
        with self._transaction():
            self._connection().execute("UPDATE items SET missing=1 WHERE id=?", (item_id,))

    def mark_missing_files(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            rows = self._connection().execute(
                "SELECT id,path,kind,content_hash,file_size FROM items WHERE path IS NOT NULL"
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
                self._markdown_dir() if kind == "markdown" else self._picture_dir()
            ) if self._is_managed_path(path) else path.parent
            try:
                with storage.open_managed_binary(path, "rb", root, identity_locked=True) as current_file:
                    file_stat = os.fstat(current_file.fileno())
                    if file_stat.st_size > int(self._max_import_bytes()):
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
                self._connection().execute(
                    "UPDATE items SET missing=? WHERE id=?",
                    (1 if owner is not None else 0, item_id),
                )
        for item_id, path, kind in replacements:
            if cancel_event is not None and cancel_event.is_set():
                return
            importer = self._scan_import_file or self.import_file
            importer(path, kind)
            root = (
                self._markdown_dir() if kind == "markdown" else self._picture_dir()
            ) if self._is_managed_path(path) else path.parent
            try:
                with storage.open_managed_binary(path, "rb", root, identity_locked=True) as current_file:
                    file_stat = os.fstat(current_file.fileno())
                    if file_stat.st_size > int(self._max_import_bytes()):
                        raise OSError("replacement is no longer importable")
                    current_disk_hash = self._stream_hash(current_file)
                    with self._lock:
                        current = self._connection().execute(
                            "SELECT content_hash,missing FROM items WHERE id=?", (item_id,)
                        ).fetchone()
            except (OSError, RuntimeError):
                self.mark_item_missing(item_id)
                continue
            if current is not None and (
                current["missing"] or current["content_hash"] != current_disk_hash
            ):
                self.mark_item_missing(item_id)
