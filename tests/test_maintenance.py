import json
import os
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

    def test_scans_in_same_second_create_distinct_manifests(self):
        output = self.root / "reports"

        first, _ = scan_orphans(self.database, self.library, output)
        second, _ = scan_orphans(self.database, self.library, output)

        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_manifest_replace_failure_leaves_no_temporary_file(self):
        output = self.root / "reports"
        with patch("clipsave_app.maintenance.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                scan_orphans(self.database, self.library, output)

        self.assertEqual(list(output.glob("*.tmp")), [])
        self.assertEqual(list(output.glob("*.json")), [])

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
        def recycle(path):
            recycled.append(path)
            Path(path).unlink()

        with (
            patch("clipsave_app.maintenance.LIBRARY_DIR", self.library),
            patch("clipsave_app.storage.LIBRARY_DIR", self.library),
            patch("clipsave_app.maintenance.send2trash", side_effect=recycle),
        ):
            result = clean_indexed_duplicates(self.database, manifest, CONFIRMATION_PHRASE)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(recycled), 1)
        recycled_path = Path(recycled[0])

        self.assertEqual(recycled_path.name, duplicate.name)
        self.assertEqual(
            recycled_path.parent.parent,
            duplicate.parent.resolve(),
        )
        self.assertTrue(recycled_path.parent.name.startswith(".clipsave-recycle-"))
        self.assertFalse(duplicate.exists())

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

    def test_cleanup_rejects_manifest_from_another_library(self):
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["library_dir"] = str(self.root / "another-library")
        manifest.write_text(json.dumps(data), encoding="utf-8")

        with patch("clipsave_app.maintenance.LIBRARY_DIR", self.library):
            with self.assertRaisesRegex(ValueError, "different library"):
                clean_indexed_duplicates(self.database, manifest, CONFIRMATION_PHRASE)

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

    def test_cleanup_rejects_all_malformed_records_before_deleting_anything(self):
        indexed = self.library / "indexed.png"
        duplicate = self.library / "indexed-copy.png"
        Image.new("RGB", (8, 8), "red").save(indexed)
        self.database.import_file(indexed, "image")
        duplicate.write_bytes(indexed.read_bytes())
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["orphans"].append(
            {"classification": "indexed_duplicate", "path": []}
        )
        manifest.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid deletion record"):
            clean_indexed_duplicates(
                self.database,
                manifest,
                PERMANENT_CONFIRMATION_PHRASE,
                permanent=True,
            )

        self.assertTrue(duplicate.exists())

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

    def test_cleanup_requires_an_in_library_indexed_keeper(self):
        external = self.root / "external.png"
        Image.new("RGB", (8, 8), "red").save(external)
        self.database.import_file(external, "image")
        duplicate = self.library / "external-copy.png"
        duplicate.write_bytes(external.read_bytes())
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")

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

    @unittest.skipUnless(os.name == "nt", "identity locking is Windows-only")
    def test_cleanup_holds_keeper_identity_lock_through_deletion(self):
        indexed = self.library / "indexed.png"
        duplicate = self.library / "indexed-copy.png"
        Image.new("RGB", (8, 8), "red").save(indexed)
        duplicate.write_bytes(indexed.read_bytes())
        self.database.import_file(indexed, "image")
        manifest, _report = scan_orphans(self.database, self.library, self.root / "reports")
        from clipsave_app import maintenance

        real_delete = maintenance.delete_managed_file
        mutation_blocked = []

        def delete_while_keeper_is_locked(*args, **kwargs):
            with self.assertRaises(OSError):
                indexed.write_bytes(b"replacement")
            mutation_blocked.append(True)
            return real_delete(*args, **kwargs)

        with (
            patch("clipsave_app.maintenance.LIBRARY_DIR", self.library),
            patch("clipsave_app.storage.LIBRARY_DIR", self.library),
            patch(
                "clipsave_app.maintenance.delete_managed_file",
                side_effect=delete_while_keeper_is_locked,
            ),
        ):
            result = clean_indexed_duplicates(
                self.database,
                manifest,
                PERMANENT_CONFIRMATION_PHRASE,
                permanent=True,
            )

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(mutation_blocked, [True])
        self.assertTrue(indexed.exists())
        self.assertFalse(duplicate.exists())

    def test_cleanup_rejects_oversized_or_malformed_manifests(self):
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b" " * (32 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            clean_indexed_duplicates(self.database, oversized, CONFIRMATION_PHRASE)

        malformed = self.root / "malformed.json"
        malformed.write_text(json.dumps({"format_version": 1, "orphans": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid orphan list"):
            clean_indexed_duplicates(self.database, malformed, CONFIRMATION_PHRASE)


if __name__ == "__main__":
    unittest.main()
