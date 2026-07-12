import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from clipsave_app.database import LibraryDatabase
from clipsave_app.maintenance import (
    CONFIRMATION_PHRASE,
    PERMANENT_CONFIRMATION_PHRASE,
    clean_indexed_duplicates,
    scan_orphans,
)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / "Library"
        self.library.mkdir()
        self.database = LibraryDatabase(self.root / "test.db")

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_scan_classifies_indexed_duplicate_orphan_duplicate_and_unique(self):
        indexed = self.library / "indexed.png"
        Image.new("RGB", (8, 8), "red").save(indexed)
        self.database.import_file(indexed, "image")
        duplicate = self.library / "indexed-copy.png"
        duplicate.write_bytes(indexed.read_bytes())
        orphan_a = self.library / "orphan-a.txt"
        orphan_b = self.library / "orphan-b.txt"
        orphan_a.write_text("same orphan", encoding="utf-8")
        orphan_b.write_text("same orphan", encoding="utf-8")
        unique = self.library / "unique.txt"
        unique.write_text("unique", encoding="utf-8")

        manifest, report = scan_orphans(self.database, self.library, self.root / "reports")

        self.assertTrue(manifest.exists())
        self.assertEqual(report["summary"]["indexed_duplicate"]["files"], 1)
        self.assertEqual(report["summary"]["orphan_duplicate"]["files"], 2)
        self.assertEqual(report["summary"]["unindexed_unique"]["files"], 1)

    def test_cleanup_requires_confirmation_and_revalidates_manifest(self):
        indexed = self.library / "indexed.png"
        Image.new("RGB", (8, 8), "red").save(indexed)
        self.database.import_file(indexed, "image")
        duplicate = self.library / "indexed-copy.png"
        duplicate.write_bytes(indexed.read_bytes())
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")

        with self.assertRaises(ValueError):
            clean_indexed_duplicates(self.database, manifest, "wrong")

        recycled = []
        with (
            patch("clipsave_app.maintenance.LIBRARY_DIR", self.library),
            patch("clipsave_app.storage.LIBRARY_DIR", self.library),
            patch("clipsave_app.maintenance.send2trash", side_effect=lambda path: recycled.append(path)),
        ):
            result = clean_indexed_duplicates(self.database, manifest, CONFIRMATION_PHRASE)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(recycled, [str(duplicate.resolve())])

        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["orphans"][0]["size"] += 1
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with (
            patch("clipsave_app.maintenance.LIBRARY_DIR", self.library),
            patch("clipsave_app.storage.LIBRARY_DIR", self.library),
            patch("clipsave_app.maintenance.send2trash") as send,
        ):
            result = clean_indexed_duplicates(self.database, manifest, CONFIRMATION_PHRASE)
        self.assertEqual(result["skipped"], 1)
        send.assert_not_called()

    def test_permanent_cleanup_uses_distinct_confirmation_and_unlinks(self):
        indexed = self.library / "indexed.png"
        Image.new("RGB", (8, 8), "red").save(indexed)
        self.database.import_file(indexed, "image")
        duplicate = self.library / "indexed-copy.png"
        duplicate.write_bytes(indexed.read_bytes())
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")

        with self.assertRaises(ValueError):
            clean_indexed_duplicates(
                self.database, manifest, CONFIRMATION_PHRASE, permanent=True
            )
        with (
            patch("clipsave_app.maintenance.LIBRARY_DIR", self.library),
            patch("clipsave_app.storage.LIBRARY_DIR", self.library),
        ):
            result = clean_indexed_duplicates(
                self.database,
                manifest,
                PERMANENT_CONFIRMATION_PHRASE,
                permanent=True,
            )
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(duplicate.exists())

    def test_cleanup_never_deletes_path_that_became_indexed(self):
        indexed = self.library / "indexed.png"
        duplicate = self.library / "indexed-copy.png"
        Image.new("RGB", (8, 8), "red").save(indexed)
        duplicate.write_bytes(indexed.read_bytes())
        self.database.import_file(indexed, "image")
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")

        original_id = self.database.query_items(kind="image")[0]["id"]
        self.database.remove_item(original_id)
        self.assertTrue(self.database.import_file(duplicate, "image"))

        with (
            patch("clipsave_app.maintenance.LIBRARY_DIR", self.library),
            patch("clipsave_app.storage.LIBRARY_DIR", self.library),
        ):
            result = clean_indexed_duplicates(
                self.database,
                manifest,
                PERMANENT_CONFIRMATION_PHRASE,
                permanent=True,
            )

        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(duplicate.exists())


if __name__ == "__main__":
    unittest.main()
