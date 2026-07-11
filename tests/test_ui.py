import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from clipsave_app.app import create_app_icon
from clipsave_app.database import LibraryDatabase
from clipsave_app.main_window import MainWindow
from clipsave_app.settings import Settings
from clipsave_app.widgets import DateDialog, DraggableBar, SettingsDialog


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
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(self.window.window_title_bar.height(), 32)
        self.assertEqual(self.window.brand_label.text, "ClipSave")
        self.assertEqual(self.window.brand_label.objectName(), "BrandTitle")
        self.assertEqual(self.window.brand_label.geometry().width(), 200)
        self.assertEqual(self.window.brand_label.geometry().height(), 86)
        self.assertEqual(self.window.brand_label.vertical_scale, 0.9)
        self.window.window_title_bar.maximize_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())
        self.window.window_title_bar.maximize_button.click()
        self.app.processEvents()
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(hasattr(self.window, "capture_state"))
        self.assertFalse(hasattr(self.window.sidebar, "brand_icon"))
        self.assertFalse(hasattr(self.window.sidebar, "brand"))
        self.assertTrue(self.window.detail.testAttribute(Qt.WidgetAttribute.WA_StyledBackground))
        self.assertEqual(self.window.library_header.height(), 44)
        self.assertIsInstance(self.window.top_bar, DraggableBar)
        self.assertEqual(len(self.window.resize_handles), 8)
        self.assertEqual(self.window.resize_handles["right"].geometry().width(), 6)
        scroll_bar = self.window.grid.verticalScrollBar()
        self.assertEqual(scroll_bar.objectName(), "AutoHideScrollBar")
        self.assertFalse(bool(scroll_bar.property("active")))
        scroll_bar.setRange(0, 100)
        scroll_bar.setValue(1)
        self.assertTrue(bool(scroll_bar.property("active")))
        self.assertEqual(self.window.sidebar.collapse_button.text().strip(), "收起侧栏")
        self.assertFalse(self.window.detail.isVisible())
        self.window.sidebar.set_collapsed(True, animate=False)
        self.assertTrue(self.window.sidebar.collapsed)
        self.assertEqual(self.window.sidebar.collapse_button.text(), "")
        self.assertEqual(self.window.sidebar.collapse_button.toolTip(), "展开侧栏")
        self.window.sidebar.set_collapsed(False, animate=False)
        self.assertEqual(self.window.sidebar.collapse_button.text().strip(), "收起侧栏")

        self.window.open_sort_menu()
        self.app.processEvents()
        self.assertIsNotNone(self.window.sort_menu)
        self.assertTrue(self.window.sort_menu.isVisible())
        self.window.open_sort_menu()
        self.app.processEvents()
        self.assertIsNone(self.window.sort_menu)
        self.window.open_day(self.database.days()[0][0])
        self.assertEqual(len(self.window.current_items), 1)
        self.window.select_item(self.window.current_items[0]["id"])
        self.window.toggle_detail()
        self.app.processEvents()
        self.assertTrue(self.window.detail.isVisible())

        dialog = SettingsDialog(self.settings, self.window)
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(dialog.objectName(), "FluentDialog")
        self.assertEqual(dialog.import_button.text(), "导入文件")
        import_requests = []
        dialog.import_requested.connect(lambda: import_requests.append(True))
        dialog.import_button.click()
        self.assertEqual(import_requests, [True])
        dialog.close()

        date_dialog = DateDialog(self.database.days(), self.window)
        self.assertTrue(date_dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(date_dialog.objectName(), "FluentDialog")
        date_dialog.close()
        self.assertFalse(self.window.table.showGrid())
        self.assertTrue(self.window.table.alternatingRowColors())


if __name__ == "__main__":
    unittest.main()
