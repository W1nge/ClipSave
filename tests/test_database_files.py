import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clipsave_app.database_files import rebind_migrated_paths, sqlite_quick_check


class DatabaseFileTests(unittest.TestCase):
    def test_rebind_migrated_paths_updates_supported_item_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database_path = root / "library.db"
            old = root / "old.png"
            new = root / "new.png"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "CREATE TABLE items(id INTEGER PRIMARY KEY,path TEXT,resolved_path TEXT,missing INTEGER)"
                )
                connection.execute(
                    "INSERT INTO items(path,resolved_path,missing) VALUES(?,?,1)",
                    (str(old), str(old.resolve())),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(rebind_migrated_paths(database_path, [(old, new)]), 1)

            connection = sqlite3.connect(database_path)
            try:
                row = connection.execute(
                    "SELECT path,resolved_path,missing FROM items"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], str(new))
            self.assertEqual(
                row[1],
                os.path.normcase(os.path.normpath(str(new.resolve()))),
            )
            self.assertEqual(row[2], 0)
            self.assertTrue(sqlite_quick_check(database_path))
