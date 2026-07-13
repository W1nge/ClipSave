from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import tempfile
from collections import Counter
from pathlib import Path

from send2trash import send2trash

from .constants import LIBRARY_DIR, MAINTENANCE_DIR
from .database import LibraryDatabase
from .storage import delete_managed_file, is_under_local_store, iter_safe_files, recycle_managed_file
from .storage import open_managed_binary


CONFIRMATION_PHRASE = "DELETE_INDEXED_DUPLICATES"
PERMANENT_CONFIRMATION_PHRASE = "PERMANENTLY_DELETE_INDEXED_DUPLICATES"
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_RECORDS = 200_000


def _write_manifest_atomic(path: Path, report: dict) -> None:
    records = report.get("orphans")
    if not isinstance(records, list) or len(records) > MAX_MANIFEST_RECORDS:
        raise ValueError("Maintenance manifest has too many records")
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("Maintenance manifest exceeds the configured size limit")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).resolve(strict=False))))


def _file_snapshot(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": LibraryDatabase.file_hash(path),
    }


def scan_orphans(
    database: LibraryDatabase,
    library_dir: Path = LIBRARY_DIR,
    output_dir: Path = MAINTENANCE_DIR,
) -> tuple[Path, dict]:
    indexed = []
    referenced_paths = set()
    indexed_hashes = set()
    for row in database.indexed_files():
        path = Path(row["path"])
        try:
            if not path.is_file():
                continue
            snapshot = _file_snapshot(path)
        except OSError:
            continue
        if snapshot["sha256"] != row["content_hash"]:
            continue
        indexed.append(snapshot)
        referenced_paths.add(_path_key(path))
        indexed_hashes.add(snapshot["sha256"])

    orphan_records = []
    errors = []
    truncated = False
    for path in iter_safe_files(library_dir):
        if len(orphan_records) >= MAX_MANIFEST_RECORDS:
            truncated = True
            errors.append(
                {
                    "path": str(library_dir),
                    "error": "Scan stopped at the maintenance manifest record limit",
                }
            )
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if _path_key(path) in referenced_paths:
                continue
            orphan_records.append(_file_snapshot(path))
        except (OSError, RuntimeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})

    orphan_hash_counts = Counter(record["sha256"] for record in orphan_records)
    for record in orphan_records:
        digest = record["sha256"]
        if digest in indexed_hashes:
            record["classification"] = "indexed_duplicate"
        elif orphan_hash_counts[digest] > 1:
            record["classification"] = "orphan_duplicate"
        else:
            record["classification"] = "unindexed_unique"

    counts = Counter(record["classification"] for record in orphan_records)
    bytes_by_class = Counter()
    for record in orphan_records:
        bytes_by_class[record["classification"]] += record["size"]
    report = {
        "format_version": 1,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "library_dir": str(library_dir.resolve()),
        "indexed_file_count": len(indexed),
        "orphan_file_count": len(orphan_records),
        "truncated": truncated,
        "summary": {
            key: {"files": counts[key], "bytes": bytes_by_class[key]}
            for key in ("indexed_duplicate", "orphan_duplicate", "unindexed_unique")
        },
        "orphans": orphan_records,
        "errors": errors,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    token = secrets.token_hex(6)
    output_path = output_dir / f"orphan_manifest_{stamp}_{token}.json"
    _write_manifest_atomic(output_path, report)
    return output_path, report


def clean_indexed_duplicates(
    database: LibraryDatabase,
    manifest_path: Path,
    confirmation: str,
    permanent: bool = False,
) -> dict:
    expected_confirmation = PERMANENT_CONFIRMATION_PHRASE if permanent else CONFIRMATION_PHRASE
    if confirmation != expected_confirmation:
        raise ValueError("Confirmation phrase does not match")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Maintenance manifest is too large")
    report = json.loads(manifest_path.read_text(encoding="utf-8"))
    if report.get("format_version") != 1:
        raise ValueError("Unsupported maintenance manifest")
    records = report.get("orphans")
    if not isinstance(records, list) or len(records) > MAX_MANIFEST_RECORDS:
        raise ValueError("Maintenance manifest has an invalid orphan list")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Maintenance manifest contains an invalid record")
    for record in records:
        if record.get("classification") != "indexed_duplicate":
            continue
        if (
            not isinstance(record.get("path"), str)
            or not record["path"]
            or type(record.get("size")) is not int
            or record["size"] < 0
            or type(record.get("mtime_ns")) is not int
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            raise ValueError("Maintenance manifest contains an invalid deletion record")

    manifest_library = report.get("library_dir")
    if not isinstance(manifest_library, str) or _path_key(manifest_library) != _path_key(LIBRARY_DIR):
        raise ValueError("Maintenance manifest belongs to a different library")
    library_root = Path(manifest_library).resolve()

    result = {
        "mode": "permanent" if permanent else "recycle_bin",
        "deleted": 0,
        "deleted_bytes": 0,
        "skipped": 0,
        "errors": [],
    }
    for record in records:
        if record.get("classification") != "indexed_duplicate":
            continue
        path_text = record["path"]
        try:
            path = Path(path_text)
            if not is_under_local_store(path) or not path.is_file() or path.is_symlink():
                result["skipped"] += 1
                continue
            stat = path.stat()
            if stat.st_size != record.get("size") or stat.st_mtime_ns != record.get("mtime_ns"):
                result["skipped"] += 1
                continue
            digest = LibraryDatabase.file_hash(path)
            if digest != record.get("sha256"):
                result["skipped"] += 1
                continue
            with database._lock:
                indexed = database.indexed_file_for_hash(digest)
                if indexed is None:
                    result["skipped"] += 1
                    continue
                indexed_path = Path(indexed["path"])
                if (
                    not is_under_local_store(indexed_path)
                    or _path_key(indexed_path) == _path_key(path)
                ):
                    result["skipped"] += 1
                    continue
                with open_managed_binary(
                    indexed_path, "rb", library_root, identity_locked=True
                ) as keeper:
                    keeper_stat = os.fstat(keeper.fileno())
                    if LibraryDatabase._stream_hash(keeper) != digest:
                        result["skipped"] += 1
                        continue
                    current_indexed = database.indexed_file_for_hash(digest)
                    if (
                        current_indexed is None
                        or current_indexed["id"] != indexed["id"]
                        or _path_key(current_indexed["path"]) != _path_key(indexed_path)
                    ):
                        result["skipped"] += 1
                        continue
                    final_stat = path.stat()
                    if (
                        keeper_stat.st_size != record["size"]
                        or not is_under_local_store(path)
                        or final_stat.st_size != stat.st_size
                        or final_stat.st_mtime_ns != stat.st_mtime_ns
                        or getattr(final_stat, "st_ino", None) != getattr(stat, "st_ino", None)
                    ):
                        result["skipped"] += 1
                        continue
                    if permanent:
                        delete_managed_file(
                            path,
                            library_root,
                            expected_sha256=digest,
                            expected_size=stat.st_size,
                        )
                    else:
                        recycle_managed_file(
                            path,
                            library_root,
                            send2trash,
                            expected_sha256=digest,
                            expected_size=stat.st_size,
                        )
            result["deleted"] += 1
            result["deleted_bytes"] += stat.st_size
        except Exception as exc:
            result["errors"].append({"path": path_text, "error": str(exc)})
    return result
