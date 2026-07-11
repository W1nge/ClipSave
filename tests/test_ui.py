import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipsave_app.app import create_app_icon
from clipsave_app.database import LibraryDatabase
from clipsave_app.main_window import MainWindow
from clipsave_app.settings import Settings


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = LibraryDatabase(root / "test.db")
        self.database.add_text("first clipboard item")
        self.settings = Settings(root / "settings.json")
        self.settings.set("monitoring", False)
        self.window = MainWindow(self.database, self.settings, create_app_icon(), scan_on_start=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.force_quit = True
        self.window.close()
        self.temp.cleanup()

    def test_panels_and_navigation(self):
        self.assertEqual(self.window.windowTitle(), "")
        self.assertFalse(hasattr(self.window, "capture_state"))
        self.assertFalse(hasattr(self.window.sidebar, "brand_icon"))
        self.assertEqual(self.window.sidebar.brand.text(), "ClipSave")
        self.assertTrue(self.window.sidebar.brand.isVisible())
        self.assertFalse(self.window.detail.isVisible())
        self.window.sidebar.set_collapsed(True, animate=False)
        self.assertTrue(self.window.sidebar.collapsed)
        self.assertFalse(self.window.sidebar.brand.isVisible())
        self.window.open_day(self.database.days()[0][0])
        self.assertEqual(len(self.window.current_items), 1)
        self.window.select_item(self.window.current_items[0]["id"])
        self.window.toggle_detail()
        self.app.processEvents()
        self.assertTrue(self.window.detail.isVisible())


if __name__ == "__main__":
    unittest.main()
