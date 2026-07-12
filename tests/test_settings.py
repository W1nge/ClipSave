import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipsave_app.settings import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_disable_monitoring(self):
        settings = Settings(self.path)

        self.assertFalse(settings.get("monitoring"))

    def test_invalid_and_unknown_values_are_ignored(self):
        self.path.write_text(
            json.dumps(
                {
                    "monitoring": "yes",
                    "sidebar_collapsed": 1,
                    "sort": "sideways",
                    "theme": "dark",
                    "unknown": "value",
                }
            ),
            encoding="utf-8",
        )

        settings = Settings(self.path)

        self.assertFalse(settings.get("monitoring"))
        self.assertFalse(settings.get("sidebar_collapsed"))
        self.assertEqual(settings.get("sort"), "newest")
        self.assertEqual(settings.get("theme"), "dark")
        self.assertIsNone(settings.get("unknown"))

    def test_corrupt_primary_uses_backup_but_disables_monitoring(self):
        backup = self.path.with_name(f"{self.path.name}.bak")
        backup.write_text(json.dumps({"monitoring": True, "theme": "dark"}), encoding="utf-8")
        self.path.write_text("{broken", encoding="utf-8")

        settings = Settings(self.path)

        self.assertEqual(settings.get("theme"), "dark")
        self.assertFalse(settings.get("monitoring"))
        self.assertTrue(backup.exists())

    def test_save_atomically_keeps_previous_valid_backup(self):
        self.path.write_text(json.dumps({"theme": "light", "monitoring": False}), encoding="utf-8")
        settings = Settings(self.path)

        settings.set("theme", "dark")

        current = json.loads(self.path.read_text(encoding="utf-8"))
        backup = json.loads(settings.backup_path.read_text(encoding="utf-8"))
        self.assertEqual(current["theme"], "dark")
        self.assertEqual(backup["theme"], "light")

    def test_failed_primary_replace_leaves_previous_file_and_memory_intact(self):
        original = {"theme": "light", "monitoring": False}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        settings = Settings(self.path)
        real_replace = os.replace

        def fail_primary_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return real_replace(source, destination)

        with patch("clipsave_app.settings.os.replace", side_effect=fail_primary_replace):
            with self.assertRaises(OSError):
                settings.set("theme", "dark")

        self.assertEqual(settings.get("theme"), "light")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_set_rejects_invalid_types(self):
        settings = Settings(self.path)

        with self.assertRaises(TypeError):
            settings.set("monitoring", 1)
        with self.assertRaises(KeyError):
            settings.set("unknown", "value")

    def test_current_sort_options_pass_validation(self):
        settings = Settings(self.path)

        for sort in ("newest", "oldest", "name", "size", "type"):
            with self.subTest(sort=sort):
                settings.set("sort", sort)
                self.assertEqual(settings.get("sort"), sort)

    def test_setting_same_value_does_not_rewrite_file(self):
        settings = Settings(self.path)
        with patch.object(settings, "save") as save:
            settings.set("monitoring", False)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
