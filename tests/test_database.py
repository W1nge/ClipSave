import datetime as dt
import tempfile
import unittest
from pathlib import Path
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

    def test_import_copies_file_into_managed_local_library(self):
        source = self.root / "outside.png"
        Image.new("RGB", (80, 60), "#ffffff").save(source)
        managed = self.root / "managed-pictures"
        with patch("clipsave_app.database.PICTURE_DIR", managed):
            self.assertTrue(self.database.import_file(source, "image", copy_to_library=True))
        imported = self.database.query_items()[0]
        self.assertTrue(Path(imported["path"]).is_relative_to(managed))
        self.assertTrue(Path(imported["path"]).exists())
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
