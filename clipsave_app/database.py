from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import sqlite3
import shutil
from pathlib import Path
from typing import Iterable

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


class LibraryDatabase:
    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.create_schema()

    def create_schema(self) -> None:
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
        self.connection.commit()

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

    def scan_legacy_files(self) -> int:
        added = 0
        image_patterns = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp", "*.gif")
        for pattern in image_patterns:
            for path in PICTURE_DIR.rglob(pattern):
                added += int(self.import_file(path, "image"))
        for path in MARKDOWN_DIR.rglob("*.md"):
            added += int(self.import_file(path, "markdown"))
        return added

    def import_file(self, path: Path, kind: str | None = None, copy_to_library: bool = False) -> bool:
        path = path.resolve()
        if not path.exists() or not path.is_file():
            return False
        kind = kind or ("markdown" if path.suffix.lower() == ".md" else "image")
        source_size = path.stat().st_size
        if source_size > MAX_IMPORT_BYTES or (kind == "markdown" and source_size > MAX_MARKDOWN_BYTES):
            return False
        width = height = None
        if kind == "image":
            try:
                with Image.open(path) as image:
                    width, height = image.size
                    if width * height > MAX_IMAGE_PIXELS:
                        return False
            except OSError:
                return False
        if copy_to_library and not is_under_local_store(path):
            target_dir = MARKDOWN_DIR / "Imported" if kind == "markdown" else PICTURE_DIR / "Imported"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            stem, suffix = path.stem, path.suffix
            index = 2
            while target.exists() and self.file_hash(target) != self.file_hash(path):
                target = target_dir / f"{stem} ({index}){suffix}"
                index += 1
            if path.resolve() != target.resolve():
                shutil.copy2(path, target)
            path = target.resolve()
        content_hash = self.file_hash(path)
        managed_local = is_under_local_store(path)
        existing = self.connection.execute("SELECT id FROM items WHERE content_hash = ?", (content_hash,)).fetchone()
        if existing:
            self.connection.execute(
                "UPDATE items SET path = ?, missing = 0, external = ? WHERE id = ?",
                (str(path), int(not managed_local), existing["id"]),
            )
            self.connection.commit()
            return False

        stat = path.stat()
        created = dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        content = ""
        if kind == "markdown":
            content = path.read_text(encoding="utf-8", errors="replace")
        self.connection.execute(
            """
            INSERT INTO items(kind,title,content,path,mime,content_hash,created_at,updated_at,
                              file_size,width,height,source,external)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                kind,
                path.name,
                content,
                str(path),
                mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                content_hash,
                created,
                created,
                stat.st_size,
                width,
                height,
                "导入文件" if copy_to_library else "现有文件",
                int(not managed_local),
            ),
        )
        self.connection.commit()
        return True

    def add_text(self, text: str, created_at: dt.datetime | None = None) -> int | None:
        created_at = created_at or dt.datetime.now()
        digest = self.text_hash(text)
        existing = self.connection.execute("SELECT id FROM items WHERE content_hash = ?", (digest,)).fetchone()
        if existing:
            return None
        title = next((line.strip() for line in text.splitlines() if line.strip()), "剪贴板文字")[:80]
        cursor = self.connection.execute(
            """INSERT INTO items(kind,title,content,content_hash,created_at,updated_at,file_size)
               VALUES('text',?,?,?,?,?,?)""",
            (title, text, digest, created_at.isoformat(timespec="seconds"), created_at.isoformat(timespec="seconds"), len(text.encode("utf-8"))),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def add_image(self, path: Path, created_at: dt.datetime | None = None) -> int | None:
        created_at = created_at or dt.datetime.now()
        digest = self.file_hash(path)
        existing = self.connection.execute("SELECT id FROM items WHERE content_hash = ?", (digest,)).fetchone()
        if existing:
            return None
        with Image.open(path) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                path.unlink(missing_ok=True)
                return None
        cursor = self.connection.execute(
            """
            INSERT INTO items(kind,title,path,mime,content_hash,created_at,updated_at,file_size,width,height)
            VALUES('image',?,?,?,?,?,?,?,?,?)
            """,
            (
                path.name,
                str(path.resolve()),
                "image/png",
                digest,
                created_at.isoformat(timespec="seconds"),
                created_at.isoformat(timespec="seconds"),
                path.stat().st_size,
                width,
                height,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

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
            "newest": "i.created_at DESC",
            "oldest": "i.created_at ASC",
            "name": "i.title COLLATE NOCASE ASC",
            "size": "i.file_size DESC",
            "type": "i.kind ASC, i.created_at DESC",
        }
        sql = f"""
            SELECT i.*, c.name AS collection_name,
                   GROUP_CONCAT(DISTINCT t.name) AS tag_names,
                   GROUP_CONCAT(DISTINCT t.color) AS tag_colors
            FROM items i
            LEFT JOIN collections c ON c.id = i.collection_id
            LEFT JOIN item_tags it ON it.item_id = i.id
            LEFT JOIN tags t ON t.id = it.tag_id
            {joins}
            WHERE {' AND '.join(clauses)}
            GROUP BY i.id
            ORDER BY {orders.get(sort, orders['newest'])}
        """
        return list(self.connection.execute(sql, parameters).fetchall())

    def get_item(self, item_id: int) -> sqlite3.Row | None:
        rows = self.query_items()
        return next((row for row in rows if row["id"] == item_id), None)

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT kind, COUNT(*) amount FROM items WHERE missing=0 GROUP BY kind").fetchall()
        result = {"all": 0, "image": 0, "text": 0, "markdown": 0, "favorite": 0}
        for row in rows:
            result[row["kind"]] = row["amount"]
            result["all"] += row["amount"]
        result["favorite"] = self.connection.execute("SELECT COUNT(*) FROM items WHERE favorite=1 AND missing=0").fetchone()[0]
        return result

    def days(self) -> list[tuple[str, int]]:
        return [(row["day"], row["amount"]) for row in self.connection.execute(
            "SELECT substr(created_at,1,10) day, COUNT(*) amount FROM items WHERE missing=0 GROUP BY day ORDER BY day DESC"
        )]

    def set_favorite(self, item_id: int, value: bool) -> None:
        self.connection.execute("UPDATE items SET favorite=? WHERE id=?", (int(value), item_id))
        self.connection.commit()

    def set_notes(self, item_id: int, notes: str) -> None:
        self.connection.execute("UPDATE items SET notes=?, updated_at=? WHERE id=?", (notes, dt.datetime.now().isoformat(timespec="seconds"), item_id))
        self.connection.commit()

    def collections(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT c.*, COUNT(i.id) amount FROM collections c LEFT JOIN items i ON i.collection_id=c.id GROUP BY c.id ORDER BY c.name"
        ))

    def create_collection(self, name: str) -> int:
        self.connection.execute("INSERT OR IGNORE INTO collections(name,created_at) VALUES(?,?)", (name, dt.datetime.now().isoformat(timespec="seconds")))
        self.connection.commit()
        return int(self.connection.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()[0])

    def set_collection(self, item_id: int, collection_id: int | None) -> None:
        self.connection.execute("UPDATE items SET collection_id=? WHERE id=?", (collection_id, item_id))
        self.connection.commit()

    def tags(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT t.*, COUNT(it.item_id) amount FROM tags t LEFT JOIN item_tags it ON it.tag_id=t.id GROUP BY t.id ORDER BY t.name"
        ))

    def add_tag(self, item_id: int, name: str) -> int:
        name = name.strip()
        existing = self.connection.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if existing:
            tag_id = int(existing[0])
        else:
            color = TAG_COLORS[self.connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] % len(TAG_COLORS)]
            cursor = self.connection.execute("INSERT INTO tags(name,color) VALUES(?,?)", (name, color))
            tag_id = int(cursor.lastrowid)
        self.connection.execute("INSERT OR IGNORE INTO item_tags(item_id,tag_id) VALUES(?,?)", (item_id, tag_id))
        self.connection.commit()
        return tag_id

    def remove_tag(self, item_id: int, tag_name: str) -> None:
        self.connection.execute(
            "DELETE FROM item_tags WHERE item_id=? AND tag_id=(SELECT id FROM tags WHERE name=?)", (item_id, tag_name)
        )
        self.connection.commit()

    def remove_item(self, item_id: int) -> None:
        self.connection.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.connection.commit()

    def update_ai(self, item_id: int, description: str, embedding: Iterable[float] | None = None) -> None:
        self.connection.execute(
            "UPDATE items SET ai_description=?, embedding=? WHERE id=?",
            (description, json.dumps(list(embedding)) if embedding is not None else None, item_id),
        )
        self.connection.commit()

    def update_ocr(self, item_id: int, text: str) -> None:
        self.connection.execute("UPDATE items SET ocr_text=? WHERE id=?", (text, item_id))
        self.connection.commit()

    def embedded_items(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT id,embedding FROM items WHERE embedding IS NOT NULL AND missing=0"))

    def mark_missing_files(self) -> None:
        for row in self.connection.execute("SELECT id,path FROM items WHERE path IS NOT NULL"):
            self.connection.execute("UPDATE items SET missing=? WHERE id=?", (int(not Path(row["path"]).exists()), row["id"]))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
