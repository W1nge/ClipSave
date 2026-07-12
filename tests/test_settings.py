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
        self.assertTrue(settings.get("follow_system_theme"))
        self.assertEqual(settings.get("theme_mode"), "light")

    def test_invalid_and_unknown_values_are_ignored(self):
        self.path.write_text(
            json.dumps(
                {
                    "monitoring": "yes",
                    "sidebar_collapsed": 1,
                    "sort": "sideways",
                    "theme": "dark",
                    "hotkey": "Ctrl+Shift+V",
                    "unknown": "value",
                }
            ),
            encoding="utf-8",
        )

        settings = Settings(self.path)

        self.assertFalse(settings.get("monitoring"))
        self.assertFalse(settings.get("sidebar_collapsed"))
        self.assertEqual(settings.get("sort"), "newest")
        self.assertIsNone(settings.get("theme"))
        self.assertIsNone(settings.get("hotkey"))
        self.assertIsNone(settings.get("unknown"))

    def test_corrupt_primary_uses_backup_but_disables_monitoring(self):
        backup = self.path.with_name(f"{self.path.name}.bak")
        backup.write_text(json.dumps({"monitoring": True, "sort": "oldest"}), encoding="utf-8")
        self.path.write_text("{broken", encoding="utf-8")

        settings = Settings(self.path)

        self.assertEqual(settings.get("sort"), "oldest")
        self.assertFalse(settings.get("monitoring"))
        self.assertTrue(backup.exists())

    def test_save_atomically_keeps_previous_valid_backup(self):
        self.path.write_text(json.dumps({"view_mode": "grid", "monitoring": False}), encoding="utf-8")
        settings = Settings(self.path)

        settings.set("view_mode", "list")

        current = json.loads(self.path.read_text(encoding="utf-8"))
        backup = json.loads(settings.backup_path.read_text(encoding="utf-8"))
        self.assertEqual(current["view_mode"], "list")
        self.assertEqual(backup["view_mode"], "grid")

    def test_failed_primary_replace_leaves_previous_file_and_memory_intact(self):
        original = {"view_mode": "grid", "monitoring": False}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        settings = Settings(self.path)
        real_replace = os.replace

        def fail_primary_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return real_replace(source, destination)

        with patch("clipsave_app.settings.os.replace", side_effect=fail_primary_replace):
            with self.assertRaises(OSError):
                settings.set("view_mode", "list")

        self.assertEqual(settings.get("view_mode"), "grid")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_set_rejects_invalid_types(self):
        settings = Settings(self.path)

        with self.assertRaises(TypeError):
            settings.set("monitoring", 1)
        with self.assertRaises(KeyError):
            settings.set("unknown", "value")
        with self.assertRaises(TypeError):
            settings.set("follow_system_theme", "yes")

    def test_current_sort_options_pass_validation(self):
        settings = Settings(self.path)

        for sort in ("newest", "oldest", "name", "size", "type"):
            with self.subTest(sort=sort):
                settings.set("sort", sort)
                self.assertEqual(settings.get("sort"), sort)

    def test_manual_theme_options_pass_validation(self):
        settings = Settings(self.path)
        settings.set("follow_system_theme", False)
        settings.set("theme_mode", "dark")
        self.assertFalse(settings.get("follow_system_theme"))
        self.assertEqual(settings.get("theme_mode"), "dark")
        with self.assertRaises(TypeError):
            settings.set("theme_mode", "system")

    def test_setting_same_value_does_not_rewrite_file(self):
        settings = Settings(self.path)
        with patch.object(settings, "save") as save:
            settings.set("monitoring", False)
        save.assert_not_called()

    def test_oversized_settings_file_is_rejected_without_loading_it(self):
        self.path.write_bytes(b" " * (1024 * 1024 + 1))
        settings = Settings(self.path)
        self.assertFalse(settings.get("monitoring"))
        self.assertEqual(settings.get("view_mode"), "grid")

    def test_settings_reject_unbounded_strings(self):
        settings = Settings(self.path)
        settings.set("ai_api_key", "previous")
        with self.assertRaises(TypeError):
            settings.update({"ai_api_key": "x" * 20_000, "close_to_tray": False})
        self.assertEqual(settings.get("ai_api_key"), "previous")
        self.assertTrue(settings.get("close_to_tray"))


if __name__ == "__main__":
    unittest.main()
