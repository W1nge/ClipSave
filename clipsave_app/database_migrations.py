from __future__ import annotations

import sqlite3
from collections.abc import Callable


def migrate_v1_to_v2(
    connection: sqlite3.Connection,
    repair_resolved_paths: Callable[[], None],
) -> None:
    columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info("items")').fetchall()
    }
    if "resolved_path" not in columns:
        connection.execute("ALTER TABLE items ADD COLUMN resolved_path TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_collection ON items(collection_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_resolved_path ON items(resolved_path)"
    )
    repair_resolved_paths()
    connection.execute("PRAGMA user_version = 2")

def migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    item_tags = connection.execute("SELECT item_id,tag_id FROM item_tags").fetchall()
    connection.execute(
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
    connection.execute(
        f"INSERT INTO items_v3({columns}) SELECT {columns} FROM items"
    )
    connection.execute("DROP TABLE items")
    connection.execute("ALTER TABLE items_v3 RENAME TO items")
    connection.execute("CREATE INDEX idx_items_created ON items(created_at DESC)")
    connection.execute("CREATE INDEX idx_items_kind ON items(kind)")
    connection.execute("CREATE INDEX idx_items_collection ON items(collection_id)")
    connection.execute("CREATE INDEX idx_items_resolved_path ON items(resolved_path)")
    connection.execute(
        """CREATE UNIQUE INDEX idx_items_live_text_hash ON items(content_hash)
           WHERE kind='text' AND content_hash IS NOT NULL AND missing=0"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX idx_items_live_file_hash ON items(content_hash)
           WHERE kind IN ('image','markdown') AND content_hash IS NOT NULL AND missing=0"""
    )
    connection.executemany(
        "INSERT INTO item_tags(item_id,tag_id) VALUES(?,?)",
        [(row[0], row[1]) for row in item_tags],
    )
    connection.execute("PRAGMA user_version = 3")

def migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in connection.execute('PRAGMA table_info("items")').fetchall()
    }
    additions = (
        ("embedding_provider", "TEXT"),
        ("embedding_model", "TEXT"),
        ("embedding_dimensions", "INTEGER"),
        ("embedding_revision", "INTEGER"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_items_embedding_profile
           ON items(embedding_provider, embedding_model, embedding_revision,
                    embedding_dimensions, id)
           WHERE embedding IS NOT NULL AND missing=0"""
    )

def migrate_v4_to_v5(
    connection: sqlite3.Connection,
    utc_timestamp: Callable[[object], str],
) -> None:
    for table, key in (("collections", "id"), ("items", "id")):
        columns = (
            ("created_at", "updated_at")
            if table == "items"
            else ("created_at",)
        )
        rows = connection.execute(
            f"SELECT {key},{','.join(columns)} FROM {table}"
        ).fetchall()
        for row in rows:
            values = [
                utc_timestamp(row[index + 1])
                for index in range(len(columns))
            ]
            assignments = ",".join(f"{column}=?" for column in columns)
            connection.execute(
                f"UPDATE {table} SET {assignments} WHERE {key}=?",
                (*values, row[0]),
            )


def downgrade_v6_to_v5(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_items_last_used")
    connection.execute("ALTER TABLE items DROP COLUMN last_used_at")
    connection.execute("PRAGMA user_version = 5")
