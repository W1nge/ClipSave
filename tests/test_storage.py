import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipsave_app import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_rejects_same_source_and_target(self):
        source = self.root / "same"
        source.mkdir()
        item = source / "keep.txt"
        item.write_text("keep", encoding="utf-8")

        moved = storage._copy_or_move_contents(source, source)

        self.assertEqual(moved, 0)
        self.assertTrue(item.exists())

    def test_migration_rejects_source_or_target_reparse_points(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        item = source / "keep.txt"
        item.write_text("keep", encoding="utf-8")

        for rejected in (source, target):
            with self.subTest(rejected=rejected):
                with patch.object(storage, "_is_link_or_junction", side_effect=lambda path: path == rejected):
                    self.assertEqual(storage._copy_or_move_contents(source, target), 0)
                self.assertTrue(item.exists())

    def test_migration_skips_existing_reparse_destination(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        item = source / "keep.txt"
        item.write_text("keep", encoding="utf-8")
        destination = target / item.name

        with patch.object(storage, "_is_link_or_junction", side_effect=lambda path: path == destination):
            moved = storage._copy_or_move_contents(source, target)

        self.assertEqual(moved, 0)
        self.assertTrue(item.exists())

    def test_local_store_requires_lexical_and_resolved_containment(self):
        library = self.root / "Library"
        library.mkdir()
        inside = library / "Pictures" / "future.png"
        outside = self.root / "Library-other" / "outside.png"

        with patch.object(storage, "LIBRARY_DIR", library):
            self.assertTrue(storage.is_under_local_store(inside))
            self.assertFalse(storage.is_under_local_store(outside))

    def test_local_store_rejects_symlink_escape(self):
        library = self.root / "Library"
        outside = self.root / "outside"
        library.mkdir()
        outside.mkdir()
        link = library / "linked"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("directory symlinks are unavailable")

        with patch.object(storage, "LIBRARY_DIR", library):
            self.assertFalse(storage.is_under_local_store(link / "item.png"))

    def test_storage_layout_rejects_reparse_root(self):
        local_root = self.root / "ClipSave"
        paths = {
            "LOCAL_ROOT": local_root,
            "DATA_DIR": local_root / "Data",
            "LIBRARY_DIR": local_root / "Library",
            "PICTURE_DIR": local_root / "Library" / "Pictures",
            "MARKDOWN_DIR": local_root / "Library" / "Markdown",
            "THUMB_DIR": local_root / "Data" / "thumbnails",
            "MAINTENANCE_DIR": local_root / "Data" / "maintenance",
        }
        with (
            patch.multiple(storage, **paths),
            patch.object(storage, "_is_link_or_junction", side_effect=lambda path: path == local_root),
        ):
            with self.assertRaisesRegex(RuntimeError, "Junction"):
                storage.validate_storage_layout()

    def test_storage_directories_are_created_only_after_validation(self):
        local_root = self.root / "ClipSave"
        paths = {
            "LOCAL_ROOT": local_root,
            "DATA_DIR": local_root / "Data",
            "LIBRARY_DIR": local_root / "Library",
            "PICTURE_DIR": local_root / "Library" / "Pictures",
            "MARKDOWN_DIR": local_root / "Library" / "Markdown",
            "THUMB_DIR": local_root / "Data" / "thumbnails",
            "MAINTENANCE_DIR": local_root / "Data" / "maintenance",
        }
        with patch.multiple(storage, **paths):
            storage.ensure_storage_directories()
        for path in paths.values():
            self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
