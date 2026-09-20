import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from clipsave_app.settings import Settings
from clipsave_app.settings_workflow import SettingsWorkflow


class SettingsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / "settings.json")
        self.workflow = SettingsWorkflow(self.settings)

    def test_reconcile_reports_effects_after_accepted_dialog(self):
        self.settings.update(
            {
                "auto_ocr": True,
                "auto_description": True,
            }
        )
        previous = self.workflow.snapshot()
        self.settings.update(
            {
                "follow_system_theme": False,
                "theme_mode": "dark",
                "start_with_windows": True,
                "auto_ocr": False,
                "auto_description": False,
            }
        )
        set_startup = Mock()

        effects = self.workflow.reconcile(
            previous,
            accepted=True,
            set_startup=set_startup,
        )

        set_startup.assert_called_once_with(True)
        self.assertTrue(effects.theme_changed)
        self.assertTrue(effects.cancel_auto_ocr)
        self.assertTrue(effects.cancel_auto_description)
        self.assertEqual(effects.startup_error, "")

    def test_startup_failure_restores_setting_and_reports_error(self):
        previous = self.workflow.snapshot()
        self.settings.set("start_with_windows", True)

        effects = self.workflow.reconcile(
            previous,
            accepted=True,
            set_startup=Mock(side_effect=OSError("access denied")),
        )

        self.assertFalse(self.settings.get("start_with_windows"))
        self.assertEqual(effects.startup_error, "access denied")

    def test_rejected_dialog_has_no_effects(self):
        previous = self.workflow.snapshot()
        set_startup = Mock()

        effects = self.workflow.reconcile(
            previous,
            accepted=False,
            set_startup=set_startup,
        )

        set_startup.assert_not_called()
        self.assertFalse(effects.theme_changed)
        self.assertFalse(effects.cancel_auto_ocr)
        self.assertFalse(effects.cancel_auto_description)
