from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from pathlib import Path

from send2trash import send2trash

from .constants import LIBRARY_DIR, MAINTENANCE_DIR
from .database import LibraryDatabase
from .storage import is_under_local_store


CONFIRMATION_PHRASE = "DELETE_INDEXED_DUPLICATES"
PERMANENT_CONFIRMATION_PHRASE = "PERMANENTLY_DELETE_INDEXED_DUPLICATES"


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
    for path in library_dir.rglob("*"):
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
        "summary": {
            key: {"files": counts[key], "bytes": bytes_by_class[key]}
            for key in ("indexed_duplicate", "orphan_duplicate", "unindexed_unique")
        },
        "orphans": orphan_records,
        "errors": errors,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"orphan_manifest_{stamp}.json"
    temporary = output_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output_path)
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
    report = json.loads(manifest_path.read_text(encoding="utf-8"))
    if report.get("format_version") != 1:
        raise ValueError("Unsupported maintenance manifest")

    result = {
        "mode": "permanent" if permanent else "recycle_bin",
        "deleted": 0,
        "deleted_bytes": 0,
        "skipped": 0,
        "errors": [],
    }
    for record in report.get("orphans", []):
        if record.get("classification") != "indexed_duplicate":
            continue
        path = Path(record.get("path", ""))
        try:
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
            indexed = database.indexed_file_for_hash(digest)
            if indexed is None:
                result["skipped"] += 1
                continue
            indexed_path = Path(indexed["path"])
            if _path_key(indexed_path) == _path_key(path):
                result["skipped"] += 1
                continue
            if not indexed_path.is_file() or LibraryDatabase.file_hash(indexed_path) != digest:
                result["skipped"] += 1
                continue
            if permanent:
                path.unlink()
            else:
                send2trash(str(path))
            result["deleted"] += 1
            result["deleted_bytes"] += stat.st_size
        except Exception as exc:
            result["errors"].append({"path": str(path), "error": str(exc)})
    return result
