import datetime as dt
import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from PIL import Image

from clipsave_app.constants import MARKDOWN_DIR, PICTURE_DIR
from clipsave_app.database import BackupValidation, ImportFileResult, LibraryDatabase
from clipsave_app import storage as storage_module


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

    def test_set_notes_if_unchanged_is_atomic(self):
        item_id = self.database.add_text("session notes")

        self.assertTrue(
            self.database.set_notes_if_unchanged(item_id, "", "first session")
        )
        self.assertFalse(
            self.database.set_notes_if_unchanged(item_id, "", "stale shutdown")
        )
        self.assertTrue(
            self.database.set_notes_if_unchanged(
                item_id, "stale baseline", "first session"
            )
        )
        self.assertEqual(self.database.get_item(item_id)["notes"], "first session")
        self.assertFalse(
            self.database.set_notes_if_unchanged(999_999, "", "missing item")
        )

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

    def test_failed_managed_import_removes_partial_temporary_file(self):
        source = self.root / "outside.md"
        source.write_text("payload" * 100, encoding="utf-8")
        managed = self.root / "managed-markdown"
        real_open = storage_module.open_managed_binary
        source_opens = 0

        class FailingReader(io.BytesIO):
            def __init__(self, payload: bytes):
                super().__init__(payload)
                self.reads = 0

            def read(self, size=-1):
                self.reads += 1
                if self.reads > 1:
                    raise OSError("injected read failure")
                return super().read(min(size, 32))

        def flaky_open(path, mode="xb", managed_root=None, **kwargs):
            nonlocal source_opens
            if LibraryDatabase._path_key(path) == LibraryDatabase._path_key(source) and mode == "rb":
                source_opens += 1
                if source_opens == 2:
                    return FailingReader(source.read_bytes())
            return real_open(path, mode, managed_root, **kwargs)

        with patch("clipsave_app.database.MARKDOWN_DIR", managed), patch(
            "clipsave_app.database.storage.open_managed_binary", side_effect=flaky_open
        ):
            self.assertFalse(
                self.database.import_file(source, "markdown", copy_to_library=True)
            )

        imported_dir = managed / "Imported"
        self.assertEqual(list(imported_dir.iterdir()), [])
        self.assertEqual(self.database.query_items(), [])

    def test_invalid_copied_image_is_removed_before_returning_failure(self):
        source = self.root / "broken.png"
        source.write_bytes(b"not an image")
        managed = self.root / "managed-pictures"

        with patch("clipsave_app.database.PICTURE_DIR", managed):
            self.assertFalse(
                self.database.import_file(source, "image", copy_to_library=True)
            )

        self.assertEqual(list((managed / "Imported").iterdir()), [])
        self.assertEqual(self.database.query_items(), [])

    def test_failed_import_commit_removes_completed_managed_copy(self):
        source = self.root / "outside.md"
        source.write_text("commit must fail", encoding="utf-8")
        managed = self.root / "managed-markdown"
        real_connection = self.database.connection

        class CommitFailingConnection:
            def __getattr__(self, name):
                return getattr(real_connection, name)

            def commit(self):
                raise sqlite3.OperationalError("disk I/O error")

        self.database.connection = CommitFailingConnection()
        try:
            with patch("clipsave_app.database.MARKDOWN_DIR", managed):
                with self.assertRaisesRegex(sqlite3.OperationalError, "I/O"):
                    self.database.import_file(
                        source, "markdown", copy_to_library=True
                    )
        finally:
            self.database.connection = real_connection

        self.assertEqual(list((managed / "Imported").iterdir()), [])
        self.assertEqual(self.database.query_items(), [])

    def test_strict_import_raises_for_missing_and_invalid_files(self):
        with self.assertRaises(FileNotFoundError):
            self.database.import_file(
                self.root / "missing.md", "markdown", strict=True
            )
        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "invalid or unreadable"):
            self.database.import_file(invalid, "image", strict=True)

    def test_import_rejects_replacement_between_snapshot_and_commit(self):
        path = self.root / "changing-import.bmp"
        Image.new("RGB", (10, 10), "red").save(path)
        calls = 0

        def replace_before_final_lock(_path):
            nonlocal calls
            calls += 1
            if calls == 2:
                Image.new("RGB", (10, 10), "blue").save(path)
            return False

        with patch(
            "clipsave_app.database.is_under_local_store",
            side_effect=replace_before_final_lock,
        ):
            self.assertFalse(self.database.import_file(path, "image"))

        self.assertEqual(self.database.query_items(kind="image"), [])

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

    def test_failed_commit_rolls_back_and_allows_later_writes(self):
        real_connection = self.database.connection

        class CommitFailingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.fail_commit = True

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                if self.fail_commit:
                    self.fail_commit = False
                    raise sqlite3.OperationalError("disk I/O error")
                return self.connection.commit()

        proxy = CommitFailingConnection(real_connection)
        self.database.connection = proxy
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, "I/O"):
                self.database.add_text("failed commit")
            self.assertFalse(real_connection.in_transaction)
            self.assertIsNotNone(self.database.add_text("later write"))
        finally:
            self.database.connection = real_connection

        self.assertEqual([row["content"] for row in self.database.query_items()], ["later write"])

    def test_write_transaction_rejects_hardlinks_added_after_connection_open(self):
        for suffix in ("", "-wal", "-shm"):
            with self.subTest(suffix=suffix or "primary"):
                database = LibraryDatabase(
                    self.root / f"active-{suffix.removeprefix('-') or 'primary'}.db"
                )
                path = Path(f"{database.path}{suffix}")
                outside = self.root / f"outside-{path.name}"
                self.assertTrue(path.exists())
                try:
                    os.link(path, outside)
                except OSError:
                    database.close()
                    self.skipTest("hard links are unavailable")
                try:
                    with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
                        database.add_text(f"must not write through {path.name}")
                    self.assertFalse(database.connection.in_transaction)
                    self.assertEqual(database.query_items(), [])
                finally:
                    database.close()
                    outside.unlink(missing_ok=True)

    def test_transaction_reverifies_files_after_begin_and_closes_on_failure(self):
        checks = 0

        def verify():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise RuntimeError("identity changed after BEGIN IMMEDIATE")

        with patch.object(self.database, "_assert_active_database_files", side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, "after BEGIN"):
                self.database.add_text("must roll back")

        self.assertEqual(checks, 2)
        self.assertIsNone(self.database._database_leaf_lock)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.database.connection.execute("SELECT 1")
        connection = sqlite3.connect(self.database.path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        finally:
            connection.close()

    def test_transaction_reverifies_files_before_commit_and_closes_on_failure(self):
        checks = 0

        def verify():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise RuntimeError("identity changed before COMMIT")

        # Same-user hardlink creation cannot be made atomic here without a custom
        # SQLite VFS. These checkpoints ensure a detected race fails closed.
        with patch.object(self.database, "_assert_active_database_files", side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, "before COMMIT"):
                self.database.add_text("must not commit")

        self.assertEqual(checks, 3)
        self.assertIsNone(self.database._database_leaf_lock)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.database.connection.execute("SELECT 1")
        connection = sqlite3.connect(self.database.path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        finally:
            connection.close()

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

    def test_mark_missing_files_revives_restored_matching_file(self):
        path = self.root / "restored.png"
        Image.new("RGB", (10, 10), "red").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        item_id = self.database.query_items()[0]["id"]
        payload = path.read_bytes()
        path.unlink()
        self.database.mark_missing_files()
        self.assertIsNone(self.database.get_item(item_id))

        path.write_bytes(payload)
        self.database.mark_missing_files()

        self.assertEqual(self.database.get_item(item_id)["id"], item_id)

    def test_restored_missing_duplicate_stays_hidden_without_aborting_scan(self):
        missing_path = self.root / "missing-copy.png"
        live_path = self.root / "live-copy.png"
        Image.new("RGB", (10, 10), "red").save(missing_path)
        payload = missing_path.read_bytes()
        self.assertTrue(self.database.import_file(missing_path, "image"))
        missing_id = self.database.query_items()[0]["id"]
        missing_path.unlink()
        self.database.mark_missing_files()
        live_path.write_bytes(payload)
        self.assertTrue(self.database.import_file(live_path, "image"))
        missing_path.write_bytes(payload)

        self.database.mark_missing_files()

        missing_row = self.database.connection.execute(
            "SELECT missing FROM items WHERE id=?", (missing_id,)
        ).fetchone()
        self.assertEqual(missing_row["missing"], 1)
        self.assertEqual([row["path"] for row in self.database.query_items(kind="image")], [str(live_path.resolve())])

    def test_mark_missing_files_reindexes_same_path_replacement(self):
        path = self.root / "replaced.png"
        Image.new("RGB", (10, 10), "red").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        original = self.database.query_items()[0]

        Image.new("RGB", (14, 8), "blue").save(path)
        self.database.mark_missing_files()

        current = self.database.query_items(kind="image")
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["id"], original["id"])
        self.assertNotEqual(current[0]["content_hash"], original["content_hash"])
        self.assertEqual((current[0]["width"], current[0]["height"]), (14, 8))

    def test_mark_missing_files_hides_invalid_same_path_replacement(self):
        path = self.root / "invalid-replacement.png"
        Image.new("RGB", (10, 10), "red").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        original = self.database.query_items()[0]

        path.write_bytes(b"not an image")
        self.database.mark_missing_files()

        self.assertEqual(self.database.query_items(kind="image"), [])
        row = self.database.connection.execute(
            "SELECT content_hash,missing FROM items WHERE id=?", (original["id"],)
        ).fetchone()
        self.assertEqual(row["content_hash"], original["content_hash"])
        self.assertEqual(row["missing"], 1)

    def test_mark_missing_files_keeps_latest_valid_replacement_visible(self):
        path = self.root / "changing-replacement.png"
        Image.new("RGB", (10, 10), "red").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        Image.new("RGB", (12, 12), "blue").save(path)
        original_import = self.database.import_file

        def replace_again_before_import(import_path, kind, **kwargs):
            Image.new("RGB", (14, 8), "green").save(import_path)
            return original_import(import_path, kind, **kwargs)

        with patch.object(
            self.database, "import_file", side_effect=replace_again_before_import
        ):
            self.database.mark_missing_files()

        current = self.database.query_items(kind="image")
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["content_hash"], self.database.file_hash(path))
        self.assertEqual((current[0]["width"], current[0]["height"]), (14, 8))

    def test_mark_missing_files_does_not_revive_file_deleted_before_commit(self):
        path = self.root / "deleted-before-revive.png"
        Image.new("RGB", (10, 10), "red").save(path)
        self.assertTrue(self.database.import_file(path, "image"))
        item_id = self.database.query_items()[0]["id"]
        with self.database._transaction():
            self.database.connection.execute(
                "UPDATE items SET missing=1 WHERE id=?", (item_id,)
            )

        def delete_before_locked_revalidation(_path):
            path.unlink()
            return False

        with patch(
            "clipsave_app.database.is_under_local_store",
            side_effect=delete_before_locked_revalidation,
        ):
            self.database.mark_missing_files()

        row = self.database.connection.execute(
            "SELECT missing FROM items WHERE id=?", (item_id,)
        ).fetchone()
        self.assertEqual(row["missing"], 1)

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

    def test_database_rejects_hard_linked_wal_sidecar(self):
        path = self.root / "sidecar.db"
        database = LibraryDatabase(path)
        database.close()
        sentinel = self.root / "sentinel.bin"
        sentinel.write_bytes(b"x" * 8192)
        wal = Path(f"{path}-wal")
        try:
            os.link(sentinel, wal)
        except OSError:
            self.skipTest("hard links are unavailable")

        with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
            LibraryDatabase(path)

        self.assertEqual(sentinel.read_bytes(), b"x" * 8192)

    @unittest.skipUnless(os.name == "nt", "Windows file identity locking")
    def test_connect_time_sidecar_hardlink_swap_cannot_modify_external_files(self):
        path = self.root / "sidecar-swap.db"
        database = LibraryDatabase(path)
        database.close()
        wal = Path(f"{path}-wal")
        shm = Path(f"{path}-shm")
        outside_wal = self.root / "outside-wal.bin"
        outside_shm = self.root / "outside-shm.bin"
        outside_wal.write_bytes(b"external wal")
        outside_shm.write_bytes(b"external shm")
        real_connect = sqlite3.connect
        swaps_blocked = []

        def connect_while_sidecars_are_swapped(database_path, *args, **kwargs):
            if (
                storage_module.normalized_absolute_path(Path(database_path))
                == storage_module.normalized_absolute_path(path)
                and not swaps_blocked
            ):
                for sidecar, outside in ((wal, outside_wal), (shm, outside_shm)):
                    replacement = self.root / f"{sidecar.name}.replacement"
                    os.link(outside, replacement)
                    with self.assertRaises(OSError):
                        os.replace(replacement, sidecar)
                    replacement.unlink(missing_ok=True)
                swaps_blocked.append(True)
            return real_connect(database_path, *args, **kwargs)

        with patch(
            "clipsave_app.database.sqlite3.connect",
            side_effect=connect_while_sidecars_are_swapped,
        ):
            reopened = LibraryDatabase(path)
            reopened.close()

        self.assertEqual(swaps_blocked, [True])
        self.assertEqual(outside_wal.read_bytes(), b"external wal")
        self.assertEqual(outside_shm.read_bytes(), b"external shm")
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_connection_close_releases_sidecar_locks_for_sqlite_cleanup(self):
        path = self.root / "sidecar-cleanup.db"
        database = LibraryDatabase(path)
        database.add_text("force wal creation")
        self.assertTrue(Path(f"{path}-wal").exists())
        self.assertTrue(Path(f"{path}-shm").exists())

        database.close()

        self.assertFalse(Path(f"{path}-wal").exists())
        self.assertFalse(Path(f"{path}-shm").exists())

    def test_truncated_image_is_not_indexed(self):
        path = self.root / "truncated.jpg"
        Image.new("RGB", (64, 64), "red").save(path, "JPEG")
        payload = path.read_bytes()
        path.write_bytes(payload[: max(100, len(payload) // 2)])

        with self.assertRaisesRegex(ValueError, "invalid or unreadable"):
            self.database.import_file(path, "image", strict=True)

        self.assertEqual(self.database.query_items(kind="image"), [])

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

    def test_backup_finalization_rejects_temporary_swap_after_validation(self):
        self.database.add_text("backup publication race")
        backups_before = set(self.database.backup_dir.glob("backup-*.db"))
        attacker = self.database.backup_dir / "attacker.db"
        attacker.write_bytes(b"not the validated backup")
        real_validation = LibraryDatabase._backup_validation_state
        swapped = []

        def validation(candidate):
            candidate = Path(candidate)
            result = real_validation(candidate)
            if candidate.suffix == ".tmp" and result is BackupValidation.VALID and not swapped:
                displaced = candidate.with_name(f"{candidate.name}.displaced")
                os.replace(candidate, displaced)
                os.replace(attacker, candidate)
                swapped.append(displaced)
            return result

        with patch.object(
            LibraryDatabase, "_backup_validation_state", side_effect=validation
        ):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                self.database.create_backup()

        self.assertEqual(len(swapped), 1)
        self.assertEqual(set(self.database.backup_dir.glob("backup-*.db")), backups_before)
        self.assertEqual(list(self.database.backup_dir.glob(".backup-*.tmp")), [])
        swapped[0].unlink()

    def test_restore_rejects_temporary_swap_after_validation(self):
        target = self.root / "restore-swap.db"
        backup_dir = target.with_name(f"{target.name}.backups")
        backup_dir.mkdir()
        backup = backup_dir / "backup-00000000000000000001-source.db"
        LibraryDatabase._backup_copy(self.database.create_backup(), backup)
        attacker = self.root / "restore-attacker.db"
        attacker.write_bytes(b"not the validated restore")
        restorer = object.__new__(LibraryDatabase)
        restorer.path = target
        real_validation = LibraryDatabase._backup_validation_state
        swapped = []

        def validation(candidate):
            candidate = Path(candidate)
            result = real_validation(candidate)
            if (
                candidate.name.startswith(f".{target.name}.restore-")
                and result is BackupValidation.VALID
                and not swapped
            ):
                displaced = candidate.with_name(f"{candidate.name}.displaced")
                os.replace(candidate, displaced)
                os.replace(attacker, candidate)
                swapped.append(displaced)
            return result

        with patch.object(
            LibraryDatabase, "_backup_validation_state", side_effect=validation
        ):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                restorer._restore_backup(backup)

        self.assertEqual(len(swapped), 1)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(f".{target.name}.restore-*.tmp")), [])
        swapped[0].unlink()

    def test_restore_reverifies_destination_after_atomic_replacement(self):
        target = self.root / "restore-destination-swap.db"
        backup_dir = target.with_name(f"{target.name}.backups")
        backup_dir.mkdir()
        backup = backup_dir / "backup-00000000000000000001-source.db"
        LibraryDatabase._backup_copy(self.database.create_backup(), backup)
        attacker = self.root / "restore-destination-attacker.db"
        attacker_payload = b"replacement after atomic restore"
        attacker.write_bytes(attacker_payload)
        restorer = object.__new__(LibraryDatabase)
        restorer.path = target
        real_replace = os.replace
        swapped = []

        def replace_then_swap(source, destination):
            result = real_replace(source, destination)
            if Path(destination) == target and not swapped:
                displaced = target.with_name(f"{target.name}.displaced")
                real_replace(target, displaced)
                real_replace(attacker, target)
                swapped.append(displaced)
            return result

        with patch("clipsave_app.database.os.replace", side_effect=replace_then_swap):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                restorer._restore_backup(backup)

        self.assertEqual(len(swapped), 1)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(f".{target.name}.untrusted-*")), [])
        swapped[0].unlink()

    def test_backup_reverifies_destination_and_removes_swapped_publication(self):
        self.database.add_text("backup destination publication race")
        attacker = self.database.backup_dir / "backup-attacker.db"
        attacker.write_bytes(b"replacement after atomic backup publication")
        real_replace = os.replace
        swapped = []

        def replace_then_swap(source, destination):
            result = real_replace(source, destination)
            destination = Path(destination)
            if destination.name.startswith("backup-") and destination.suffix == ".db" and not swapped:
                displaced = destination.with_name(f"{destination.name}.displaced")
                real_replace(destination, displaced)
                real_replace(attacker, destination)
                swapped.append((destination, displaced))
            return result

        with patch("clipsave_app.database.os.replace", side_effect=replace_then_swap):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                self.database.create_backup()

        self.assertEqual(len(swapped), 1)
        target, displaced = swapped[0]
        self.assertFalse(target.exists())
        self.assertEqual(list(self.database.backup_dir.glob(".*.untrusted-*")), [])
        displaced.unlink()

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

    def test_missing_primary_restores_latest_valid_backup(self):
        path = self.root / "missing-primary.db"
        database = LibraryDatabase(path)
        database.add_text("must survive missing primary")
        expected_backup = database.create_backup()
        database.close()
        path.unlink()
        Path(f"{path}-wal").unlink(missing_ok=True)
        Path(f"{path}-shm").unlink(missing_ok=True)

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_state()["action"], "restored")
            self.assertEqual(recovered.recovery_state()["backup_path"], str(expected_backup))
            self.assertEqual(recovered.query_items()[0]["content"], "must survive missing primary")
        finally:
            recovered.close()

    def test_transient_restore_failure_does_not_rebuild_empty_database(self):
        for mode in ("corrupt", "missing"):
            with self.subTest(mode=mode):
                path = self.root / f"restore-{mode}.db"
                database = LibraryDatabase(path)
                database.add_text(f"preserve {mode}")
                backup = database.create_backup()
                database.close()
                if mode == "corrupt":
                    path.write_bytes(b"broken primary")
                else:
                    path.unlink()

                with patch.object(
                    LibraryDatabase,
                    "_restore_backup",
                    side_effect=OSError(28, "No space left on device"),
                ):
                    with self.assertRaises(OSError):
                        LibraryDatabase(path)

                self.assertFalse(path.exists())
                self.assertTrue(backup.exists())

    def test_newest_unknown_backup_aborts_recovery_without_fallback_or_rebuild(self):
        for mode in ("corrupt", "missing"):
            with self.subTest(mode=mode):
                path = self.root / f"unknown-newest-{mode}.db"
                database = LibraryDatabase(path)
                database.add_text("older recoverable state")
                older_backup = database.create_backup()
                database.add_text("newest uncertain state")
                newest_backup = database.create_backup()
                database.close()
                if mode == "corrupt":
                    path.write_bytes(b"broken primary")
                else:
                    path.unlink()

                real_validation = LibraryDatabase._backup_validation_state

                def validation(candidate):
                    if Path(candidate) == newest_backup:
                        return BackupValidation.UNKNOWN
                    return real_validation(candidate)

                with patch.object(
                    LibraryDatabase, "_backup_validation_state", side_effect=validation
                ):
                    with self.assertRaisesRegex(OSError, "temporarily unavailable"):
                        LibraryDatabase(path)

                self.assertFalse(path.exists())
                self.assertTrue(newest_backup.exists())
                self.assertTrue(older_backup.exists())

    def test_missing_primary_preserves_stale_wal_and_shm_before_restore(self):
        path = self.root / "missing-with-sidecars.db"
        database = LibraryDatabase(path)
        database.add_text("restore without stale sidecars")
        database.create_backup()
        database.close()
        path.unlink()
        wal = Path(f"{path}-wal")
        shm = Path(f"{path}-shm")
        wal.write_bytes(b"stale wal")
        shm.write_bytes(b"stale shm")

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_state()["action"], "restored")
            self.assertEqual(recovered.query_items()[0]["content"], "restore without stale sidecars")
            preserved = [Path(value) for value in recovered.recovery_state()["preserved_paths"]]
            preserved_wal = next(
                value for value in preserved if value.name.startswith(f"{path.name}-wal.orphan-")
            )
            preserved_shm = next(
                value for value in preserved if value.name.startswith(f"{path.name}-shm.orphan-")
            )
            self.assertEqual(preserved_wal.read_bytes(), b"stale wal")
            self.assertEqual(preserved_shm.read_bytes(), b"stale shm")
        finally:
            recovered.close()

    def test_non_corruption_schema_error_does_not_trigger_recovery(self):
        path = self.root / "busy-migration.db"
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY

        with patch.object(LibraryDatabase, "create_schema", side_effect=error), patch.object(
            LibraryDatabase, "_recover_corrupt_database"
        ) as recover:
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                LibraryDatabase(path)

        recover.assert_not_called()
        self.assertTrue(path.exists())

    def test_preserve_corrupt_files_rolls_back_partial_rename(self):
        path = self.root / "partial-preserve.db"
        wal = Path(f"{path}-wal")
        path.write_bytes(b"database")
        wal.write_bytes(b"wal")
        database = object.__new__(LibraryDatabase)
        database.path = path
        real_replace = os.replace
        calls = 0

        def fail_second_replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected sidecar failure")
            return real_replace(source, destination)

        with patch("clipsave_app.database.os.replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(RuntimeError, "Could not preserve"):
                database._preserve_corrupt_files()

        self.assertEqual(path.read_bytes(), b"database")
        self.assertEqual(wal.read_bytes(), b"wal")

    def test_zero_length_primary_is_preserved_and_restored(self):
        path = self.root / "empty-primary.db"
        database = LibraryDatabase(path)
        database.add_text("must survive truncation")
        expected_backup = database.create_backup()
        database.close()
        path.write_bytes(b"")

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_state()["action"], "restored")
            self.assertEqual(recovered.recovery_state()["backup_path"], str(expected_backup))
            self.assertEqual(recovered.query_items()[0]["content"], "must survive truncation")
            preserved = [Path(value) for value in recovered.recovery_state()["preserved_paths"]]
            self.assertTrue(any(value.name.startswith(f"{path.name}.corrupt-") for value in preserved))
        finally:
            recovered.close()

    def test_backup_sequence_survives_wall_clock_rollback(self):
        path = self.root / "clock-rollback.db"
        database = LibraryDatabase(path)
        database.add_text("older")
        with patch.object(database, "_timestamp", return_value="99990101T000000000000Z"):
            older_backup = database.create_backup()
        database.add_text("newer")
        with patch.object(database, "_timestamp", return_value="00010101T000000000000Z"):
            newer_backup = database.create_backup()
        database.close()
        path.write_bytes(b"broken")

        recovered = LibraryDatabase(path)
        try:
            self.assertNotEqual(older_backup, newer_backup)
            self.assertEqual(recovered.recovery_state()["backup_path"], str(newer_backup))
            self.assertEqual({row["content"] for row in recovered.query_items()}, {"older", "newer"})
        finally:
            recovered.close()

    @unittest.skipUnless(os.name == "nt", "Windows Junction behavior")
    def test_backup_directory_junction_is_rejected(self):
        path = self.root / "junction-backup.db"
        backup_dir = path.with_name(f"{path.name}.backups")
        outside = self.root / "outside-backups"
        outside.mkdir()
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(backup_dir), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"could not create Junction: {result.stderr or result.stdout}")
        try:
            with self.assertRaisesRegex(RuntimeError, "outside|reparse point"):
                LibraryDatabase(path)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            backup_dir.rmdir()

    def test_primary_database_leaf_link_is_rejected(self):
        outside = self.root / "outside.db"
        connection = sqlite3.connect(outside)
        connection.close()
        linked = self.root / "linked-primary.db"
        try:
            os.symlink(outside, linked)
        except (NotImplementedError, OSError):
            self.skipTest("file symlinks are unavailable")

        with self.assertRaisesRegex(RuntimeError, "reparse point"):
            LibraryDatabase(linked)

    def test_primary_database_hardlink_is_rejected(self):
        outside = self.root / "outside-hardlink.db"
        connection = sqlite3.connect(outside)
        connection.close()
        linked = self.root / "hardlinked-primary.db"
        try:
            os.link(outside, linked)
        except OSError:
            self.skipTest("hard links are unavailable")

        with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
            LibraryDatabase(linked)

    def test_recovery_skips_linked_backup_leaf(self):
        path = self.root / "linked-backup-recovery.db"
        database = LibraryDatabase(path)
        database.add_text("valid local backup")
        valid_backup = database.create_backup()
        database.close()

        outside = self.root / "outside-backup.db"
        LibraryDatabase._backup_copy(valid_backup, outside)
        linked_backup = path.with_name(f"{path.name}.backups") / (
            "backup-99999999999999999999-99990101T000000000000Z.db"
        )
        try:
            os.symlink(outside, linked_backup)
        except (NotImplementedError, OSError):
            self.skipTest("file symlinks are unavailable")
        path.write_bytes(b"broken")

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_state()["backup_path"], str(valid_backup))
            self.assertEqual(recovered.query_items()[0]["content"], "valid local backup")
        finally:
            recovered.close()

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

    def test_newer_schema_backups_are_never_rotated_by_older_release(self):
        path = self.root / "future-only.db"
        database = LibraryDatabase(path)
        database.add_text("future-only content")
        source_backup = database.create_backup()
        database.close()
        backup_dir = path.with_name(f"{path.name}.backups")
        future_backups = []
        for index in range(3):
            future = backup_dir / f"backup-{90 + index:020d}-future-{index}.db"
            LibraryDatabase._backup_copy(source_backup, future)
            connection = sqlite3.connect(future)
            connection.execute(f"PRAGMA user_version = {LibraryDatabase.SCHEMA_VERSION + 1}")
            connection.commit()
            connection.close()
            future_backups.append(future)
        for backup in backup_dir.glob("backup-*.db"):
            if backup not in future_backups:
                backup.unlink()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            candidate.unlink(missing_ok=True)

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_state()["action"], "rebuilt")
            self.assertEqual(recovered.query_items(), [])
            self.assertTrue(all(backup.exists() for backup in future_backups))
        finally:
            recovered.close()

    def test_corrupt_backups_are_capped_separately_from_usable_backups(self):
        backup_dir = self.database.backup_dir
        corrupt_backups = []
        for sequence in range(100, 105):
            backup = backup_dir / f"backup-{sequence:020d}-corrupt.db"
            backup.write_bytes(f"corrupt-{sequence}".encode("ascii"))
            corrupt_backups.append(backup)

        self.database.add_text("rotate corrupt backups")
        self.database.create_backup()

        remaining_corrupt = [backup for backup in corrupt_backups if backup.exists()]
        self.assertEqual(remaining_corrupt, corrupt_backups[-LibraryDatabase.BACKUP_LIMIT :])
        usable = [
            backup
            for backup in self.database._sorted_backups()
            if self.database._backup_is_usable(backup)
        ]
        self.assertLessEqual(len(usable), LibraryDatabase.BACKUP_LIMIT)

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

    def test_unindexed_file_scan_recovers_markdown_crash_orphan(self):
        pictures = self.root / "pictures"
        markdown = self.root / "markdown"
        pictures.mkdir()
        markdown.mkdir()
        orphan = markdown / "orphan.md"
        orphan.write_text("# recovered", encoding="utf-8")

        with patch("clipsave_app.database.PICTURE_DIR", pictures), patch(
            "clipsave_app.database.MARKDOWN_DIR", markdown
        ):
            self.assertEqual(self.database.scan_unindexed_files(), 1)
            self.assertEqual(self.database.scan_unindexed_files(), 0)

        rows = self.database.query_items(kind="markdown")
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]["path"]), orphan.resolve())

    def test_text_and_markdown_use_separate_hash_domains(self):
        markdown = self.root / "same.md"
        markdown.write_text("identical bytes", encoding="utf-8")

        self.assertTrue(self.database.import_file(markdown, "markdown"))
        text_id = self.database.add_text("identical bytes")

        self.assertIsNotNone(text_id)
        self.assertFalse(self.database.import_file(markdown, "markdown"))
        self.assertIsNone(self.database.add_text("identical bytes"))
        rows = self.database.connection.execute(
            "SELECT kind,content_hash FROM items ORDER BY kind"
        ).fetchall()
        self.assertEqual({row["kind"] for row in rows}, {"markdown", "text"})
        self.assertEqual(len({row["content_hash"] for row in rows}), 1)

    def test_version_two_migration_preserves_rows_metadata_and_tag_links(self):
        path = self.root / "version-two.db"
        legacy_file = self.root / "legacy.md"
        legacy_file.write_text("shared legacy content", encoding="utf-8")
        timestamp = "2026-07-12T08:00:00"
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
                resolved_path TEXT,
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
            CREATE INDEX idx_items_created ON items(created_at DESC);
            CREATE INDEX idx_items_kind ON items(kind);
            CREATE INDEX idx_items_collection ON items(collection_id);
            CREATE INDEX idx_items_resolved_path ON items(resolved_path);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL);
            CREATE TABLE item_tags (
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY(item_id, tag_id)
            );
            PRAGMA user_version = 2;
            """
        )
        connection.execute(
            "INSERT INTO collections(id,name,created_at) VALUES(7,'Legacy',?)", (timestamp,)
        )
        connection.execute("INSERT INTO tags(id,name,color) VALUES(9,'Keep','#123456')")
        connection.execute(
            """
            INSERT INTO items(
                id,kind,title,content,path,resolved_path,mime,content_hash,created_at,updated_at,
                file_size,width,height,source,favorite,notes,ocr_text,ai_description,embedding,
                collection_id,external,missing
            ) VALUES(42,'markdown','legacy.md','shared legacy content',?,?,?,?,?, ?,17,NULL,NULL,
                     'legacy source',1,'keep notes','keep ocr','keep ai','[0.1, 0.2]',7,1,0)
            """,
            (
                str(legacy_file),
                LibraryDatabase._path_key(legacy_file),
                "text/markdown",
                LibraryDatabase.text_hash("shared legacy content"),
                timestamp,
                timestamp,
            ),
        )
        connection.execute("INSERT INTO item_tags(item_id,tag_id) VALUES(42,9)")
        connection.commit()
        connection.close()

        migrated = LibraryDatabase(path)
        try:
            row = migrated.get_item(42)
            self.assertEqual(row["id"], 42)
            self.assertEqual(row["collection_id"], 7)
            self.assertEqual(row["tag_names"], "Keep")
            self.assertEqual(row["notes"], "keep notes")
            self.assertEqual(row["ocr_text"], "keep ocr")
            self.assertEqual(row["ai_description"], "keep ai")
            self.assertEqual(row["embedding"], "[0.1, 0.2]")
            self.assertEqual(row["created_at"], timestamp)
            self.assertEqual(row["external"], 1)
            self.assertEqual(
                migrated.connection.execute("PRAGMA user_version").fetchone()[0],
                LibraryDatabase.SCHEMA_VERSION,
            )
            self.assertIsNotNone(migrated.add_text("shared legacy content"))
            self.assertEqual(migrated.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            migrated.close()

    def test_missing_hash_does_not_own_live_text_domain(self):
        first_id = self.database.add_text("reusable")
        self.database.set_notes(first_id, "preserve missing metadata")
        with self.database._transaction():
            self.database.connection.execute("UPDATE items SET missing=1 WHERE id=?", (first_id,))

        second_id = self.database.add_text("reusable")

        self.assertIsNotNone(second_id)
        self.assertNotEqual(second_id, first_id)
        rows = self.database.connection.execute(
            "SELECT id,missing,notes FROM items WHERE content_hash=? ORDER BY id",
            (LibraryDatabase.text_hash("reusable"),),
        ).fetchall()
        self.assertEqual([(row["id"], row["missing"]) for row in rows], [(first_id, 1), (second_id, 0)])
        self.assertEqual(rows[0]["notes"], "preserve missing metadata")
        self.assertTrue(self.database.has_content_hash(LibraryDatabase.text_hash("reusable"), "text"))

    def test_same_path_update_can_claim_hash_owned_only_by_missing_row(self):
        current = self.root / "current.md"
        missing = self.root / "missing.md"
        current.write_text("old live content", encoding="utf-8")
        missing.write_text("new shared content", encoding="utf-8")
        self.assertTrue(self.database.import_file(current, "markdown"))
        current_id = self.database.query_items()[0]["id"]
        self.assertTrue(self.database.import_file(missing, "markdown"))
        with self.database._transaction():
            self.database.connection.execute(
                "UPDATE items SET missing=1 WHERE resolved_path=?",
                (self.database._path_key(missing),),
            )

        current.write_text("new shared content", encoding="utf-8")
        self.assertFalse(self.database.import_file(current, "markdown"))

        visible = self.database.query_items(kind="markdown")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["id"], current_id)
        self.assertEqual(visible[0]["content"], "new shared content")

    def test_copy_to_library_repoints_external_duplicate_and_preserves_metadata(self):
        source = self.root / "external.md"
        source.write_text("managed duplicate", encoding="utf-8")
        managed = self.root / "managed-markdown"

        def is_managed(path):
            return Path(path).resolve().is_relative_to(managed.resolve())

        with (
            patch("clipsave_app.database.MARKDOWN_DIR", managed),
            patch("clipsave_app.database.is_under_local_store", side_effect=is_managed),
        ):
            self.assertTrue(self.database.import_file(source, "markdown"))
            item_id = self.database.query_items()[0]["id"]
            collection_id = self.database.create_collection("Retained")
            self.database.set_collection(item_id, collection_id)
            self.database.add_tag(item_id, "Retained tag")
            self.database.set_favorite(item_id, True)
            self.database.set_notes(item_id, "Retained notes")
            self.database.update_ocr(item_id, "Retained OCR")
            self.database.update_ai(item_id, "Retained AI", [0.25, 0.75])
            before = self.database.get_item(item_id)

            result = self.database.import_file(source, "markdown", copy_to_library=True)
            self.assertFalse(result)
            self.assertIs(result, ImportFileResult.LOCALIZED)

        after = self.database.get_item(item_id)
        self.assertEqual(after["id"], item_id)
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["collection_id"], collection_id)
        self.assertEqual(after["tag_names"], "Retained tag")
        self.assertEqual(after["favorite"], 1)
        self.assertEqual(after["notes"], "Retained notes")
        self.assertEqual(after["ocr_text"], "Retained OCR")
        self.assertEqual(after["ai_description"], "Retained AI")
        self.assertEqual(after["embedding"], "[0.25, 0.75]")
        self.assertEqual(after["external"], 0)
        self.assertTrue(Path(after["path"]).is_relative_to(managed.resolve()))
        self.assertTrue(Path(after["path"]).exists())
        self.assertTrue(source.exists())

    def test_transient_backup_validation_is_unknown_and_never_rotated(self):
        backup_dir = self.database.backup_dir
        unknown_backups = []
        for sequence in range(200, 205):
            backup = backup_dir / f"backup-{sequence:020d}-unknown.db"
            backup.write_bytes(b"temporarily unavailable")
            unknown_backups.append(backup)
        real_validation = LibraryDatabase._backup_validation_state

        def validation(path):
            if Path(path) in unknown_backups:
                return BackupValidation.UNKNOWN
            return real_validation(path)

        self.database.add_text("rotate without deleting unknown backups")
        with patch.object(
            LibraryDatabase, "_backup_validation_state", side_effect=validation
        ):
            self.database.create_backup()

        self.assertTrue(all(backup.exists() for backup in unknown_backups))

    def test_transient_backup_validation_returns_unknown(self):
        backup = self.database.create_backup()
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY

        with patch.object(LibraryDatabase, "_quick_check", side_effect=error):
            state = LibraryDatabase._backup_validation_state(backup)

        self.assertIs(state, BackupValidation.UNKNOWN)

    def test_backup_usability_honors_transient_error_tolerance(self):
        backup = self.database.create_backup()
        with patch.object(
            LibraryDatabase,
            "_backup_validation_state",
            return_value=BackupValidation.UNKNOWN,
        ):
            self.assertFalse(
                LibraryDatabase._backup_is_usable(
                    backup, tolerate_transient_errors=True
                )
            )
            with self.assertRaisesRegex(OSError, "temporarily unavailable"):
                LibraryDatabase._backup_is_usable(
                    backup, tolerate_transient_errors=False
                )

    @unittest.skipUnless(os.name == "nt", "Windows directory identity locking")
    def test_primary_sqlite_open_holds_parent_directory_identity(self):
        parent = self.root / "database-parent"
        parent.mkdir()
        path = parent / "locked.db"
        moved_parent = self.root / "database-parent-moved"
        real_connect = sqlite3.connect
        replacement_attempted = []

        def connect_while_replacement_is_attempted(database, *args, **kwargs):
            if (
                storage_module.normalized_absolute_path(Path(database))
                == storage_module.normalized_absolute_path(path)
                and not replacement_attempted
            ):
                try:
                    os.replace(parent, moved_parent)
                except OSError:
                    pass
                replacement_attempted.append(True)
            return real_connect(database, *args, **kwargs)

        with patch("clipsave_app.database.sqlite3.connect", side_effect=connect_while_replacement_is_attempted):
            database = LibraryDatabase(path)
            database.close()
        self.assertEqual(replacement_attempted, [True])
        self.assertTrue(parent.exists())
        self.assertFalse(moved_parent.exists())

    @unittest.skipUnless(os.name == "nt", "Windows directory identity locking")
    def test_backup_sqlite_creation_rejects_directory_replacement(self):
        self.database.add_text("backup directory replacement")
        backup_dir = self.database.backup_dir
        moved_backup_dir = self.root / "moved-backups"
        real_connect = sqlite3.connect
        replacement_attempted = []

        def connect_while_replacement_is_attempted(database, *args, **kwargs):
            candidate = Path(database) if not isinstance(database, str) or not database.startswith("file:") else None
            if (
                candidate is not None
                and candidate.parent == backup_dir
                and candidate.suffix == ".tmp"
                and not replacement_attempted
            ):
                try:
                    os.replace(backup_dir, moved_backup_dir)
                except OSError:
                    pass
                replacement_attempted.append(True)
            return real_connect(database, *args, **kwargs)

        with patch("clipsave_app.database.sqlite3.connect", side_effect=connect_while_replacement_is_attempted):
            backup = self.database.create_backup()

        self.assertEqual(replacement_attempted, [True])
        self.assertTrue(backup.exists())
        self.assertTrue(backup_dir.exists())
        self.assertFalse(moved_backup_dir.exists())

    @unittest.skipUnless(os.name == "nt", "Windows file identity locking")
    def test_primary_sqlite_open_rejects_hardlink_added_during_connect(self):
        path = self.root / "connect-hardlink.db"
        database = LibraryDatabase(path)
        database.close()
        outside = self.root / "connect-hardlink-outside.db"
        real_connect = sqlite3.connect
        linked = []

        def connect_while_hardlink_is_added(database_path, *args, **kwargs):
            if (
                storage_module.normalized_absolute_path(Path(database_path))
                == storage_module.normalized_absolute_path(path)
                and not linked
            ):
                os.link(path, outside)
                linked.append(True)
            return real_connect(database_path, *args, **kwargs)

        try:
            with patch(
                "clipsave_app.database.sqlite3.connect",
                side_effect=connect_while_hardlink_is_added,
            ):
                with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
                    LibraryDatabase(path)
        finally:
            outside.unlink(missing_ok=True)

        self.assertEqual(linked, [True])

    @unittest.skipUnless(os.name == "nt", "Windows file identity locking")
    def test_new_primary_creation_rejects_hardlink_before_sqlite_initialization(self):
        path = self.root / "new-connect-hardlink.db"
        outside = self.root / "new-connect-hardlink-outside.db"
        real_connect = sqlite3.connect
        linked = []

        def connect_while_hardlink_is_added(database_path, *args, **kwargs):
            if (
                storage_module.normalized_absolute_path(Path(database_path))
                == storage_module.normalized_absolute_path(path)
                and not linked
            ):
                os.link(path, outside)
                linked.append(True)
            return real_connect(database_path, *args, **kwargs)

        with patch(
            "clipsave_app.database.sqlite3.connect",
            side_effect=connect_while_hardlink_is_added,
        ):
            with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
                LibraryDatabase(path)

        self.assertEqual(linked, [True])
        self.assertFalse(path.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(outside.stat().st_size, 0)

    @unittest.skipUnless(os.name == "nt", "Windows file identity locking")
    def test_live_primary_connection_blocks_leaf_replacement(self):
        replacement = self.root / "live-replacement.db"
        sqlite3.connect(replacement).close()

        with self.assertRaises(OSError):
            os.replace(replacement, self.database.path)

        self.database.add_text("connection remains on original leaf")
        self.assertEqual(
            self.database.query_items()[0]["content"],
            "connection remains on original leaf",
        )

    @unittest.skipUnless(os.name == "nt", "Windows file identity locking")
    def test_backup_creation_rejects_hardlink_added_during_connect(self):
        self.database.add_text("secure backup creation")
        outside = self.root / "backup-hardlink-outside.db"
        real_connect = sqlite3.connect
        linked = []

        def connect_while_hardlink_is_added(database_path, *args, **kwargs):
            candidate = (
                Path(database_path)
                if not isinstance(database_path, str)
                or not database_path.startswith("file:")
                else None
            )
            if (
                candidate is not None
                and candidate.parent == self.database.backup_dir
                and candidate.suffix == ".tmp"
                and not linked
            ):
                os.link(candidate, outside)
                linked.append(True)
            return real_connect(database_path, *args, **kwargs)

        with patch(
            "clipsave_app.database.sqlite3.connect",
            side_effect=connect_while_hardlink_is_added,
        ):
            with self.assertRaisesRegex(RuntimeError, "multiple hard links"):
                self.database.create_backup()

        self.assertEqual(linked, [True])
        self.assertEqual(outside.stat().st_size, 0)
        self.assertEqual(list(self.database.backup_dir.glob(".backup-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows file identity locking")
    def test_backup_hash_holds_validated_file_identity(self):
        backup = self.database.create_backup()
        real_stream_hash = LibraryDatabase._stream_hash
        mutation_blocked = []

        def hash_while_mutation_is_attempted(handle):
            with self.assertRaises(OSError):
                backup.write_bytes(b"replacement")
            mutation_blocked.append(True)
            return real_stream_hash(handle)

        with patch.object(LibraryDatabase, "_stream_hash", side_effect=hash_while_mutation_is_attempted):
            digest = LibraryDatabase._managed_backup_hash(backup)

        self.assertEqual(len(digest), 64)
        self.assertEqual(mutation_blocked, [True])

    def test_search_matches_tag_names(self):
        tagged_id = self.database.add_text("ordinary body")
        self.database.add_tag(tagged_id, "Quarterly Review")
        self.database.add_text("unrelated body")

        results = self.database.query_items(query="quarterly")

        self.assertEqual([row["id"] for row in results], [tagged_id])

    def test_summary_query_bounds_payload_and_supports_pagination(self):
        ids = []
        for index in range(3):
            item_id = self.database.add_text(f"row {index}\n" + (str(index) * 5_000))
            self.database.set_notes(item_id, f"notes {index}" * 500)
            self.database.update_ocr(item_id, f"ocr {index}" * 500)
            self.database.update_ai(item_id, f"ai {index}" * 500, [float(index)] * 1_000)
            ids.append(item_id)
        statements = []
        self.database.connection.set_trace_callback(statements.append)
        try:
            rows = self.database.query_items(
                sort="oldest", summary_only=True, limit=1, offset=1
            )
        finally:
            self.database.connection.set_trace_callback(None)

        self.assertEqual([row["id"] for row in rows], [ids[1]])
        row = rows[0]
        self.assertLessEqual(len(row["content"]), LibraryDatabase.SUMMARY_CONTENT_LIMIT)
        self.assertEqual(row["notes"], "")
        self.assertEqual(row["ocr_text"], "")
        self.assertEqual(row["ai_description"], "")
        self.assertIsNone(row["embedding"])
        self.assertNotIn("SELECT i.*", "\n".join(statements))
        full = self.database.get_item(ids[1])
        self.assertGreater(len(full["content"]), len(row["content"]))
        self.assertTrue(full["notes"])
        self.assertEqual(set(full.keys()), set(row.keys()))
        with self.assertRaises(ValueError):
            self.database.query_items(limit=-1)
        with self.assertRaises(ValueError):
            self.database.query_items(offset=-1)

    def test_backup_generation_creates_time_separated_periodic_snapshots(self):
        initial_state = self.database.backup_state()
        initial_path = Path(initial_state["last_backup_path"])
        self.assertFalse(initial_state["dirty"])
        self.assertIsNone(self.database.create_backup_if_changed())

        self.database.add_text("first periodic backup")
        first_generation = self.database.mutation_generation
        first_backup = self.database.create_backup_if_changed()
        self.assertIsNotNone(first_backup)
        self.assertNotEqual(first_backup, initial_path)
        self.assertIsNone(self.database.create_backup_if_changed())

        self.database.add_text("second periodic backup")
        second_backup = self.database.create_backup_if_changed()
        state = self.database.backup_state()
        self.assertIsNotNone(second_backup)
        self.assertNotEqual(second_backup, first_backup)
        self.assertGreater(state["generation"], first_generation)
        self.assertEqual(state["generation"], state["backed_up_generation"])
        self.assertFalse(state["dirty"])
        self.assertEqual(Path(state["last_backup_path"]), second_backup)

    def test_backup_state_reports_periodic_backup_failures(self):
        self.database.add_text("dirty before failed backup")
        with patch.object(self.database, "_create_backup", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.database.create_backup_if_changed()

        state = self.database.backup_state()
        self.assertTrue(state["dirty"])
        self.assertEqual(state["last_error"], "disk full")

    def test_recovery_state_returns_an_independent_status_snapshot(self):
        state = self.database.recovery_state()
        state["preserved_paths"].append("changed by caller")

        self.assertEqual(self.database.recovery_state()["action"], "none")
        self.assertEqual(self.database.recovery_state()["preserved_paths"], [])
        self.assertFalse(self.database.recovery_state()["needs_library_rescan"])

    def test_current_schema_backup_validation_rejects_missing_index_and_foreign_keys(self):
        valid = self.database.create_backup()
        invalid_index = self.root / "invalid-index.db"
        LibraryDatabase._backup_copy(valid, invalid_index)
        connection = sqlite3.connect(invalid_index)
        connection.execute("DROP INDEX idx_items_kind")
        connection.commit()
        connection.close()
        self.assertTrue(LibraryDatabase._quick_check(invalid_index))
        self.assertFalse(LibraryDatabase._backup_is_usable(invalid_index))

        invalid_foreign_keys = self.root / "invalid-foreign-keys.db"
        LibraryDatabase._backup_copy(valid, invalid_foreign_keys)
        connection = sqlite3.connect(invalid_foreign_keys)
        connection.execute("DROP TABLE item_tags")
        connection.execute(
            """CREATE TABLE item_tags (
                item_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY(item_id, tag_id)
            )"""
        )
        connection.commit()
        connection.close()
        self.assertTrue(LibraryDatabase._quick_check(invalid_foreign_keys))
        self.assertFalse(LibraryDatabase._backup_is_usable(invalid_foreign_keys))

        invalid_global_hash = self.root / "invalid-global-hash.db"
        LibraryDatabase._backup_copy(valid, invalid_global_hash)
        connection = sqlite3.connect(invalid_global_hash)
        connection.execute(
            "CREATE UNIQUE INDEX legacy_global_content_hash ON items(content_hash)"
        )
        connection.commit()
        connection.close()
        self.assertTrue(LibraryDatabase._quick_check(invalid_global_hash))
        self.assertFalse(LibraryDatabase._backup_is_usable(invalid_global_hash))

    def test_primary_missing_required_index_restores_valid_backup(self):
        path = self.root / "index-recovery.db"
        database = LibraryDatabase(path)
        database.add_text("restore indexed schema")
        expected_backup = database.create_backup()
        database.close()
        connection = sqlite3.connect(path)
        connection.execute("DROP INDEX idx_items_kind")
        connection.commit()
        connection.close()

        recovered = LibraryDatabase(path)
        try:
            self.assertEqual(recovered.recovery_state()["action"], "restored")
            self.assertEqual(recovered.recovery_state()["backup_path"], str(expected_backup))
            self.assertEqual(recovered.query_items()[0]["content"], "restore indexed schema")
        finally:
            recovered.close()

    def test_database_scans_delegate_to_storage_safe_iterator(self):
        picture = self.root / "safe.png"
        markdown = self.root / "safe.md"
        with (
            patch(
                "clipsave_app.database.storage.iter_safe_files",
                side_effect=[[picture], [markdown]],
            ) as iterator,
            patch.object(self.database, "import_file", return_value=True) as import_file,
        ):
            self.assertEqual(self.database.scan_legacy_files(), 2)

        self.assertEqual(iterator.call_count, 2)
        self.assertEqual(iterator.call_args_list[0].args[0], Path(PICTURE_DIR))
        self.assertEqual(iterator.call_args_list[1].args[0], Path(MARKDOWN_DIR))
        import_file.assert_any_call(picture, "image")
        import_file.assert_any_call(markdown, "markdown")


if __name__ == "__main__":
    unittest.main()
