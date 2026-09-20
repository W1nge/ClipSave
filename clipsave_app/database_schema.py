from __future__ import annotations

import sqlite3
from collections.abc import Callable


SCHEMA_VERSION = 5

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
        "embedding_provider",
        "embedding_model",
        "embedding_dimensions",
        "embedding_revision",
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
    "idx_items_embedding_profile": (
        False,
        (
            "embedding_provider",
            "embedding_model",
            "embedding_revision",
            "embedding_dimensions",
            "id",
        ),
        True,
    ),
}


class UnsupportedSchemaVersion(RuntimeError):
    pass


class InvalidDatabaseSchema(RuntimeError):
    pass


def validate_connection_schema(connection: sqlite3.Connection, version: int) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = REQUIRED_COLUMNS.keys() - tables
    if missing_tables:
        raise InvalidDatabaseSchema(
            f"Database schema is missing tables: {sorted(missing_tables)}"
        )
    table_info = {
        table: connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        for table in REQUIRED_COLUMNS
    }
    for table, required in REQUIRED_COLUMNS.items():
        columns = {row[1] for row in table_info[table]}
        compatible = required - (
            {"resolved_path"} if version == 1 and table == "items" else set()
        )
        if version < 4 and table == "items":
            compatible -= {
                "embedding_provider",
                "embedding_model",
                "embedding_dimensions",
                "embedding_revision",
            }
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
            "kind",
            "title",
            "content",
            "created_at",
            "updated_at",
            "file_size",
            "source",
            "favorite",
            "notes",
            "ocr_text",
            "ai_description",
            "external",
            "missing",
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
        if any(
            actual_defaults.get(name) != value
            for name, value in expected_defaults.items()
        ):
            raise InvalidDatabaseSchema(
                "Database schema items table has invalid defaults"
            )

    item_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    item_sql = (
        "".join(str(item_sql_row[0] or "").lower().split()) if item_sql_row else ""
    )
    if "check(kindin('image','text','markdown'))" not in item_sql:
        raise InvalidDatabaseSchema(
            "Database schema items.kind is missing its CHECK constraint"
        )

    _require_unique_columns(connection, "collections", ("name",))
    _require_unique_columns(connection, "tags", ("name",))
    if version < 3:
        _require_unique_columns(connection, "items", ("content_hash",))

    foreign_keys = {
        table: {
            (row[3], row[2], row[4], str(row[6]).upper())
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
        }
        for table in ("items", "item_tags")
    }
    if (
        "collection_id",
        "collections",
        "id",
        "SET NULL",
    ) not in foreign_keys["items"]:
        raise InvalidDatabaseSchema(
            "Database schema is missing the items collection foreign key"
        )
    required_item_tag_keys = {
        ("item_id", "items", "id", "CASCADE"),
        ("tag_id", "tags", "id", "CASCADE"),
    }
    if not required_item_tag_keys <= foreign_keys["item_tags"]:
        raise InvalidDatabaseSchema(
            "Database schema is missing item_tags cascade foreign keys"
        )

    if version >= 2:
        ordinary = {
            name: spec
            for name, spec in REQUIRED_INDEXES.items()
            if not spec[2]
            and (version >= 4 or name != "idx_items_embedding_profile")
        }
        _validate_required_indexes(connection, ordinary)
    if version >= 3:
        current_indexes = (
            REQUIRED_INDEXES
            if version >= 4
            else {
                name: spec
                for name, spec in REQUIRED_INDEXES.items()
                if name != "idx_items_embedding_profile"
            }
        )
        _validate_required_indexes(connection, current_indexes)
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
        if (
            "wherekind='text'andcontent_hashisnotnullandmissing=0"
            not in index_sql.get("idx_items_live_text_hash", "")
        ):
            raise InvalidDatabaseSchema(
                "Database schema has an invalid live text hash index"
            )
        file_index = index_sql.get("idx_items_live_file_hash", "")
        if (
            "wherekindin('image','markdown')" not in file_index
            or "content_hashisnotnull" not in file_index
            or "missing=0" not in file_index
        ):
            raise InvalidDatabaseSchema(
                "Database schema has an invalid live file hash index"
            )

    if version >= 4:
        item_types = {row[1]: str(row[2]).upper() for row in table_info["items"]}
        expected_embedding_types = {
            "embedding_provider": "TEXT",
            "embedding_model": "TEXT",
            "embedding_dimensions": "INTEGER",
            "embedding_revision": "INTEGER",
        }
        if any(
            item_types.get(name) != expected
            for name, expected in expected_embedding_types.items()
        ):
            raise InvalidDatabaseSchema(
                "Database schema has invalid embedding metadata types"
            )

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise InvalidDatabaseSchema(
            "Database schema contains foreign-key violations"
        )


def _require_unique_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not int(row[2]):
            continue
        indexed = tuple(
            index_row[2]
            for index_row in connection.execute(
                f'PRAGMA index_info("{row[1]}")'
            ).fetchall()
        )
        if indexed == columns:
            return
    raise InvalidDatabaseSchema(
        f"Database schema table {table!r} is missing a unique constraint on {columns}"
    )


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
            raise InvalidDatabaseSchema(
                f"Database schema is missing required index {name!r}"
            )
        actual_columns = tuple(
            row[2]
            for row in connection.execute(
                f'PRAGMA index_info("{name}")'
            ).fetchall()
        )
        if actual_columns != columns:
            raise InvalidDatabaseSchema(
                f"Database schema index {name!r} has invalid columns"
            )
    created_xinfo = connection.execute(
        'PRAGMA index_xinfo("idx_items_created")'
    ).fetchall()
    created_key_columns = [row for row in created_xinfo if int(row[5])]
    if created_key_columns and not int(created_key_columns[0][3]):
        raise InvalidDatabaseSchema(
            "Database schema created_at index must be descending"
        )


def create_current_schema(connection: sqlite3.Connection) -> None:
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
            embedding_provider TEXT,
            embedding_model TEXT,
            embedding_dimensions INTEGER,
            embedding_revision INTEGER,
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
        """CREATE INDEX idx_items_embedding_profile
           ON items(embedding_provider, embedding_model, embedding_revision,
                    embedding_dimensions, id)
           WHERE embedding IS NOT NULL AND missing=0""",
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
        connection.execute(statement)


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
