import datetime as dt
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from PIL import Image

from clipsave_app.database import LibraryDatabase


class LibraryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = LibraryDatabase(self.root / "test.db")

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_text_deduplication_and_day_filter(self):
        when = dt.datetime(2026, 7, 11, 9, 30)
        item_id = self.database.add_text("meeting notes", when)
        self.assertIsNotNone(item_id)
        self.assertIsNone(self.database.add_text("meeting notes", when))
        self.assertEqual(len(self.database.query_items(day="2026-07-11")), 1)
        self.assertEqual(len(self.database.query_items(day="2026-07-10")), 0)

    def test_image_metadata_import_and_search(self):
        path = self.root / "example.png"
        Image.new("RGB", (640, 480), "#2f7df6").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        self.assertFalse(self.database.import_file(path, "image"))
        result = self.database.query_items(query="example")
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["width"], result[0]["height"]), (640, 480))

    def test_collection_tags_favorite_and_notes(self):
        item_id = self.database.add_text("organize me")
        collection_id = self.database.create_collection("Work")
        self.database.set_collection(item_id, collection_id)
        self.database.add_tag(item_id, "Important")
        self.database.set_favorite(item_id, True)
        self.database.set_notes(item_id, "Remember this")
        result = self.database.query_items(favorite=True)[0]
        self.assertEqual(result["collection_name"], "Work")
        self.assertEqual(result["tag_names"], "Important")
        self.assertEqual(result["notes"], "Remember this")

    def test_tag_names_with_commas_keep_stable_color_pairing(self):
        item_id = self.database.add_text("tag pairing")
        for index in range(8):
            self.database.add_tag(item_id, f"Tag,{index}")

        result = self.database.get_item(item_id)
        names = result["tag_names"].split("\x1f")
        colors = result["tag_colors"].split("\x1f")

        self.assertEqual(names, [f"Tag,{index}" for index in range(8)])
        self.assertEqual(len(colors), len(names))
        self.assertEqual(colors[0], colors[7])

    def test_same_path_changing_to_existing_content_archives_stale_row(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("old", encoding="utf-8")
        second.write_text("shared", encoding="utf-8")
        self.assertTrue(self.database.import_file(first, "markdown"))
        self.assertTrue(self.database.import_file(second, "markdown"))

        first.write_text("shared", encoding="utf-8")
        self.assertFalse(self.database.import_file(first, "markdown"))

        visible = self.database.query_items(kind="markdown")
        self.assertEqual(len(visible), 1)
        self.assertEqual(Path(visible[0]["path"]), second.resolve())
        archived = self.database.connection.execute(
            "SELECT path,resolved_path,missing FROM items WHERE content_hash=?",
            (LibraryDatabase.text_hash("old"),),
        ).fetchone()
        self.assertIsNone(archived["path"])
        self.assertIsNone(archived["resolved_path"])
        self.assertEqual(archived["missing"], 1)

    def test_import_copies_file_into_managed_local_library(self):
        source = self.root / "outside.png"
        Image.new("RGB", (80, 60), "#ffffff").save(source)
        managed = self.root / "managed-pictures"
        with patch("clipsave_app.database.PICTURE_DIR", managed):
            self.assertTrue(self.database.import_file(source, "image", copy_to_library=True))
        imported = self.database.query_items()[0]
        self.assertTrue(Path(imported["path"]).resolve().is_relative_to(managed.resolve()))
        self.assertTrue(Path(imported["path"]).exists())
        self.assertTrue(source.exists())

    def test_add_image_rejects_large_external_image_without_deleting_it(self):
        source = self.root / "external-large.png"
        Image.new("RGB", (20, 20), "#ffffff").save(source)
        with patch("clipsave_app.database.MAX_IMAGE_PIXELS", 100):
            self.assertIsNone(self.database.add_image(source))
        self.assertTrue(source.exists())

    def test_markdown_change_at_same_path_updates_existing_record(self):
        path = self.root / "notes.md"
        path.write_text("first version", encoding="utf-8")
        self.assertTrue(self.database.import_file(path, "markdown"))
        original = self.database.query_items()[0]

        path.write_text("second version", encoding="utf-8")
        self.assertFalse(self.database.import_file(path, "markdown"))

        items = self.database.query_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], original["id"])
        self.assertEqual(items[0]["content"], "second version")
        self.assertEqual(items[0]["content_hash"], self.database.file_hash(path))

    def test_scan_updates_one_of_four_resolved_path_rows_without_deleting_history(self):
        path = self.root / "daily.md"
        path.write_text("version one", encoding="utf-8")
        self.assertTrue(self.database.import_file(path, "markdown"))
        resolved_path = self.database._path_key(path)
        timestamp = dt.datetime(2026, 7, 12, 8, 0).isoformat(timespec="seconds")
        inserted_ids = []
        with self.database._transaction():
            for number in range(2, 5):
                content = f"version {number}"
                cursor = self.database.connection.execute(
                    """
                    INSERT INTO items(
                        kind,title,content,path,resolved_path,mime,content_hash,
                        created_at,updated_at,file_size,favorite,notes
                    ) VALUES('markdown',?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        path.name,
                        content,
                        str(path.parent / "." / path.name),
                        resolved_path,
                        "text/markdown",
                        self.database.text_hash(content),
                        timestamp,
                        timestamp,
                        len(content),
                        int(number == 4),
                        "keep this note" if number == 4 else "",
                    ),
                )
                inserted_ids.append(int(cursor.lastrowid))
        tagged_id = inserted_ids[-1]
        tag_id = self.database.add_tag(tagged_id, "History")
        orphan = self.root / "untracked-orphan.bin"
        orphan.write_bytes(b"do not delete")

        report = self.database.repair_paths()
        self.assertEqual(report, {"duplicate_path_groups": 1, "duplicate_path_rows": 4})
        self.assertTrue(orphan.exists())
        path.write_text("current daily content", encoding="utf-8")
        self.assertFalse(self.database.import_file(path, "markdown"))

        rows = self.database.connection.execute(
            "SELECT * FROM items WHERE resolved_path=? ORDER BY id", (resolved_path,)
        ).fetchall()
        self.assertEqual(len(rows), 4)
        updated = next(row for row in rows if row["id"] == tagged_id)
        self.assertEqual(updated["content"], "current daily content")
        self.assertEqual(updated["favorite"], 1)
        self.assertEqual(updated["notes"], "keep this note")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM item_tags WHERE item_id=? AND tag_id=?", (tagged_id, tag_id)
            ).fetchone()[0],
            1,
        )

    def test_duplicate_content_does_not_redirect_existing_path(self):
        original = self.root / "original.md"
        duplicate = self.root / "duplicate.md"
        original.write_text("same content", encoding="utf-8")
        duplicate.write_text("same content", encoding="utf-8")

        self.assertTrue(self.database.import_file(original, "markdown"))
        self.assertFalse(self.database.import_file(duplicate, "markdown"))

        items = self.database.query_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(Path(items[0]["path"]), original.resolve())

    def test_duplicate_copied_file_is_cleaned_up_without_redirecting_record(self):
        first = self.root / "first.png"
        second = self.root / "second.png"
        Image.new("RGB", (40, 30), "#123456").save(first)
        second.write_bytes(first.read_bytes())
        managed = self.root / "managed-pictures"

        def is_managed(path):
            return Path(path).resolve().is_relative_to(managed.resolve())

        with (
            patch("clipsave_app.database.PICTURE_DIR", managed),
            patch("clipsave_app.database.is_under_local_store", side_effect=is_managed),
        ):
            self.assertTrue(self.database.import_file(first, "image", copy_to_library=True))
            stored_path = Path(self.database.query_items()[0]["path"])
            self.assertFalse(self.database.import_file(second, "image", copy_to_library=True))

        self.assertEqual(Path(self.database.query_items()[0]["path"]), stored_path)
        self.assertTrue(stored_path.exists())
        self.assertFalse((managed / "Imported" / second.name).exists())

    def test_concurrent_text_addition_is_atomic_and_deduplicated(self):
        databases = [self.database] + [LibraryDatabase(self.root / "test.db") for _ in range(3)]
        workers = len(databases)
        barrier = Barrier(workers)

        def add_same_text(database):
            barrier.wait()
            return database.add_text("concurrent text")

        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(add_same_text, databases))
        finally:
            for database in databases[1:]:
                database.close()

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.database.query_items()), 1)

    def test_collection_and_tag_counts_exclude_missing_items(self):
        path = self.root / "counted.png"
        Image.new("RGB", (10, 10), "#000000").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        item_id = self.database.query_items()[0]["id"]
        collection_id = self.database.create_collection("Archive")
        self.database.set_collection(item_id, collection_id)
        tag_id = self.database.add_tag(item_id, "Filed")

        path.unlink()
        self.database.mark_missing_files()

        collection = next(row for row in self.database.collections() if row["id"] == collection_id)
        tag = next(row for row in self.database.tags() if row["id"] == tag_id)
        self.assertEqual(collection["amount"], 0)
        self.assertEqual(tag["amount"], 0)

    def test_get_item_queries_by_id_and_sorting_has_stable_tiebreakers(self):
        when = dt.datetime(2026, 7, 12, 12, 0)
        first_id = self.database.add_text("same", when)
        second_id = self.database.add_text("other", when)

        with patch.object(self.database, "query_items", side_effect=AssertionError("not used")):
            self.assertEqual(self.database.get_item(first_id)["id"], first_id)
        self.assertEqual(
            [row["id"] for row in self.database.query_items(sort="newest")],
            [second_id, first_id],
        )
        self.assertEqual(
            [row["id"] for row in self.database.query_items(sort="oldest")],
            [first_id, second_id],
        )

    def test_scan_continues_after_individual_file_failure(self):
        pictures = self.root / "pictures"
        markdown = self.root / "markdown"
        pictures.mkdir()
        markdown.mkdir()
        bad = pictures / "bad.png"
        good = pictures / "good.png"
        bad.touch()
        good.touch()

        def import_one(path, kind, copy_to_library=False):
            if path.name == "bad.png":
                raise OSError("unreadable")
            return path.name == "good.png"

        with (
            patch("clipsave_app.database.PICTURE_DIR", pictures),
            patch("clipsave_app.database.MARKDOWN_DIR", markdown),
            patch.object(self.database, "import_file", side_effect=import_one),
        ):
            self.assertEqual(self.database.scan_legacy_files(), 1)

    def test_schema_version_and_busy_timeout_are_initialized(self):
        version = self.database.connection.execute("PRAGMA user_version").fetchone()[0]
        timeout = self.database.connection.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(version, LibraryDatabase.SCHEMA_VERSION)
        self.assertEqual(timeout, LibraryDatabase.BUSY_TIMEOUT_MS)

    def test_version_one_schema_backfills_resolved_paths_without_deleting_rows(self):
        path = self.root / "version-one.db"
        daily = self.root / "legacy-daily.md"
        daily.write_text("legacy", encoding="utf-8")
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            CREATE TABLE items (
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
                source TEXT NOT NULL DEFAULT 'clipboard',
                favorite INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                ocr_text TEXT NOT NULL DEFAULT '',
                ai_description TEXT NOT NULL DEFAULT '',
                embedding TEXT,
                collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
                external INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL);
            CREATE TABLE item_tags (
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY(item_id, tag_id)
            );
            PRAGMA user_version = 1;
            """
        )
        connection.execute(
            """
            INSERT INTO items(
                kind,title,content,path,mime,content_hash,created_at,updated_at,file_size
            ) VALUES('markdown',?,?,?,?,?,?,?,?)
            """,
            (
                daily.name,
                "legacy",
                str(daily.parent / "." / daily.name),
                "text/markdown",
                LibraryDatabase.text_hash("legacy"),
                "2026-07-12T08:00:00",
                "2026-07-12T08:00:00",
                6,
            ),
        )
        connection.commit()
        connection.close()

        migrated = LibraryDatabase(path)
        try:
            row = migrated.connection.execute("SELECT * FROM items").fetchone()
            self.assertEqual(row["resolved_path"], migrated._path_key(daily))
            self.assertEqual(migrated.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            self.assertEqual(
                migrated.connection.execute("PRAGMA user_version").fetchone()[0],
                LibraryDatabase.SCHEMA_VERSION,
            )
        finally:
            migrated.close()

    def test_newer_schema_version_is_rejected(self):
        path = self.root / "future.db"
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA user_version = {LibraryDatabase.SCHEMA_VERSION + 1}")
        connection.close()
        with self.assertRaises(RuntimeError):
            LibraryDatabase(path)

    def test_backups_rotate_and_remain_valid_sqlite_databases(self):
        for index in range(LibraryDatabase.BACKUP_LIMIT + 2):
            self.database.add_text(f"backup item {index}")
            self.database.create_backup()

        backups = sorted(self.database.backup_dir.glob("backup-*.db"), reverse=True)
        self.assertEqual(len(backups), LibraryDatabase.BACKUP_LIMIT)
        self.assertTrue(all(LibraryDatabase._backup_is_usable(path) for path in backups))
        connection = sqlite3.connect(backups[0])
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 5)
        finally:
            connection.close()

    def test_identical_backup_does_not_consume_rotation_slot(self):
        initial = sorted(self.database.backup_dir.glob("backup-*.db"), reverse=True)
        self.assertEqual(len(initial), 1)

        same_backup = self.database.create_backup()
        self.assertEqual(same_backup, initial[0])
        self.assertEqual(sorted(self.database.backup_dir.glob("backup-*.db"), reverse=True), initial)

        self.database.add_text("new backup state")
        changed_backup = self.database.create_backup()
        self.assertNotEqual(changed_backup, initial[0])
        self.assertEqual(len(list(self.database.backup_dir.glob("backup-*.db"))), 2)

    def test_corrupt_database_and_sidecars_are_preserved_before_backup_recovery(self):
        path = self.root / "recover.db"
        database = LibraryDatabase(path)
        database.add_text("recover me")
        expected_backup = database.create_backup()
        database.close()
        corrupt_database = b"not a sqlite database"
        corrupt_wal = b"preserve wal"
        corrupt_shm = b"preserve shm"
        path.write_bytes(corrupt_database)
        Path(f"{path}-wal").write_bytes(corrupt_wal)
        Path(f"{path}-shm").write_bytes(corrupt_shm)

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_report["action"], "restored")
            self.assertEqual(recovered.recovery_report["backup_path"], str(expected_backup))
            self.assertTrue(recovered.needs_library_rescan)
            self.assertEqual(recovered.query_items()[0]["content"], "recover me")
            preserved = [Path(value) for value in recovered.recovery_report["preserved_paths"]]
            database_copy = next(value for value in preserved if value.name.startswith(f"{path.name}.corrupt-"))
            wal_copy = next(value for value in preserved if value.name.startswith(f"{path.name}-wal.corrupt-"))
            shm_copy = next(value for value in preserved if value.name.startswith(f"{path.name}-shm.corrupt-"))
            self.assertEqual(database_copy.read_bytes(), corrupt_database)
            self.assertEqual(wal_copy.read_bytes(), corrupt_wal)
            self.assertEqual(shm_copy.read_bytes(), corrupt_shm)
        finally:
            recovered.close()

    def test_corruption_without_valid_backup_rebuilds_empty_database(self):
        path = self.root / "rebuild.db"
        path.write_bytes(b"broken")
        Path(f"{path}-wal").write_bytes(b"wal")
        Path(f"{path}-shm").write_bytes(b"shm")

        rebuilt = LibraryDatabase(path)
        try:
            self.assertEqual(rebuilt.recovery_report["action"], "rebuilt")
            self.assertTrue(rebuilt.needs_library_rescan)
            self.assertEqual(rebuilt.query_items(), [])
            self.assertEqual(len(rebuilt.recovery_report["preserved_paths"]), 3)
            self.assertTrue(LibraryDatabase._quick_check(path))
        finally:
            rebuilt.close()

    def test_recovery_skips_newer_incompatible_backup(self):
        path = self.root / "fallback.db"
        database = LibraryDatabase(path)
        database.add_text("older valid state")
        valid_backup = database.create_backup()
        database.add_text("newer state")
        corrupt_backup = database.create_backup()
        database.close()
        corrupt_backup.unlink()
        incompatible = sqlite3.connect(corrupt_backup)
        incompatible.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        incompatible.close()
        path.write_bytes(b"broken primary")

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_report["backup_path"], str(valid_backup))
            self.assertEqual([row["content"] for row in recovered.query_items()], ["older valid state"])
        finally:
            recovered.close()

    def test_structurally_invalid_database_restores_valid_backup(self):
        path = self.root / "structural.db"
        database = LibraryDatabase(path)
        database.add_text("preserved backup content")
        valid_backup = database.create_backup()
        database.close()

        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE items")
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {LibraryDatabase.SCHEMA_VERSION}")
        connection.commit()
        self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        connection.close()

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_report["action"], "restored")
            self.assertEqual(recovered.recovery_report["backup_path"], str(valid_backup))
            self.assertEqual(recovered.query_items()[0]["content"], "preserved backup content")
            self.assertTrue(recovered.recovery_report["preserved_paths"])
        finally:
            recovered.close()

    def test_unindexed_image_scan_recovers_crash_orphan_only(self):
        pictures = self.root / "pictures"
        pictures.mkdir()
        indexed = pictures / "indexed.png"
        orphan = pictures / "orphan.png"
        Image.new("RGB", (10, 10), "#123456").save(indexed)
        Image.new("RGB", (12, 8), "#654321").save(orphan)

        with patch("clipsave_app.database.PICTURE_DIR", pictures):
            self.assertTrue(self.database.import_file(indexed, "image"))
            self.assertEqual(self.database.scan_unindexed_images(), 1)
            self.assertEqual(self.database.scan_unindexed_images(), 0)

        paths = {Path(row["path"]) for row in self.database.query_items(kind="image")}
        self.assertEqual(paths, {indexed.resolve(), orphan.resolve()})


if __name__ == "__main__":
    unittest.main()
