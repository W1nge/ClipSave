import tempfile
import unittest
from pathlib import Path

from clipsave_app.app_paths import AppPaths
from clipsave_app.database import LibraryDatabase


class AppPathsTests(unittest.TestCase):
    def test_build_derives_all_runtime_paths_from_two_roots(self):
        paths = AppPaths.build(base_dir=Path("C:/app"), local_root=Path("C:/profile"))

        self.assertEqual(paths.data_dir, Path("C:/profile/Data"))
        self.assertEqual(paths.picture_dir, Path("C:/profile/Library/Pictures"))
        self.assertEqual(paths.markdown_dir, Path("C:/profile/Library/Markdown"))
        self.assertEqual(paths.database_path, Path("C:/profile/Data/clipsave.db"))
        self.assertEqual(paths.legacy_picture_dir, Path("C:/app/Picture"))

    def test_database_uses_explicit_paths_when_no_database_path_is_given(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = AppPaths.build(base_dir=root / "app", local_root=root / "profile")
            database = LibraryDatabase(paths=paths)
            try:
                self.assertEqual(database.path, paths.database_path.resolve())
                self.assertEqual(database._picture_dir, paths.picture_dir)
                self.assertEqual(database._markdown_dir, paths.markdown_dir)
            finally:
                database.close()
