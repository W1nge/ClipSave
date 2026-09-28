"""Native integration regression; uses a hidden host and a temporary library."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from clipsave_app.app_paths import AppPaths
from clipsave_app.composition_main_window import CompositionMainWindow, composition_window_class
from clipsave_app.database import LibraryDatabase
from clipsave_app.settings import Settings
from clipsave_app.windows_backdrop import WindowsCompositionBackdropBridge


class CompositionHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.name != 'nt' or cls.app.platformName() != 'windows':
            raise unittest.SkipTest('Native Windows Qt runtime required')
        bridge = WindowsCompositionBackdropBridge()
        if not bridge.is_supported() or not bridge.supports_frames() or not bridge.supports_material_control():
            raise unittest.SkipTest('Build the current native composition runtime first')

    def test_resize_search_popup_lifecycle_and_material(self):
        from clipsave_app.windows_frame import _user32
        user = _user32()
        real_show = user.ShowWindow
        class HiddenUser:
            def __getattr__(self, name):
                # Still exercise minimize/restore on the existing hidden HWND;
                # suppress only showing it on the user's desktop.
                if name == 'ShowWindow':
                    return lambda hwnd, command: real_show(hwnd, 0)
                return getattr(user, name)
        with tempfile.TemporaryDirectory(prefix='clipsave-composition-test-') as temporary:
            paths = AppPaths.build(base_dir=Path(__file__).resolve().parents[1], local_root=Path(temporary))
            database = LibraryDatabase(paths=paths)
            settings = Settings(paths.settings_path)
            for key, value in {'monitoring': False, 'close_to_tray': True,
                               'follow_system_theme': False, 'theme_mode': 'dark'}.items():
                settings.set(key, value)
            for number in range(10):
                database.add_text(f'fixture {number:02d}')
            with patch('clipsave_app.windows_composition_host._user32', HiddenUser):
                window = CompositionMainWindow(database, settings, QIcon(),
                    paths=paths, scan_on_start=False, reconcile_on_start=False)
            window.tray.hide()
            host = window._composition_host
            self.assertIsNotNone(host, 'Unexpected fallback instead of composition')
            try:
                window.show()
                QTest.qWait(300)
                self.assertGreater(host.frame_count, 0)
                self.assertEqual(host.errors, [])
                user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
                user.SendMessageW.restype = C.c_ssize_t
                user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
                user.PostMessageW.restype = W.BOOL

                def click_search():
                    point = window.search.mapTo(window, window.search.rect().center())
                    scale = user.GetDpiForWindow(host.hwnd)/96
                    lp = round(point.x()*scale) | (round(point.y()*scale) << 16)
                    user.SendMessageW(host.hwnd, 0x201, 1, lp)
                    user.SendMessageW(host.hwnd, 0x202, 0, lp)

                def type_key(key):
                    user.PostMessageW(host.hwnd, 0x100, key, 1)
                    user.PostMessageW(host.hwnd, 0x101, key, 1 | (1 << 31))

                click_search()
                type_key(0x30)
                type_key(0x37)
                QTest.qWait(500)
                self.assertEqual(window.search.text(), '07')
                self.assertEqual(window.grid.model().rowCount(), 1)
                user.SendMessageW(host.hwnd, 0x231, 0, 0)
                self.assertTrue(window.native_window_controller.interactive_resize_active)
                for width, height in ((950, 650), (1120, 730), (900, 600)):
                    user.SetWindowPos(host.hwnd, None, 150, 100, width, height, 0x14)
                    QTest.qWait(50)
                user.SendMessageW(host.hwnd, 0x232, 0, 0)
                self.assertFalse(window.native_window_controller.interactive_resize_active)
                QApplication.setActiveWindow(None)
                click_search()
                type_key(8)
                type_key(0x38)
                QTest.qWait(500)
                self.assertEqual(window.search.text(), '08')
                self.assertTrue(window.search.hasFocus())
                self.assertEqual(window.grid.model().rowCount(), 1)
                self.assertEqual(window.width(), 900)
                self.assertEqual(window.height(), 600)
                self.assertEqual(window.pos(), QPoint(150, 100))

                # Right click must retain the application's detail gesture.
                requests = []
                window.grid.detail_requested.disconnect()
                window.grid.detail_requested.connect(requests.append)
                card = window.grid.visualRect(window.grid.model().index(0, 0))
                point = window.grid.viewport().mapTo(window, card.center())
                lp = point.x() | (point.y() << 16)
                user.SendMessageW(host.hwnd, 0x204, 2, lp)
                user.SendMessageW(host.hwnd, 0x205, 0, lp)
                self.assertEqual(len(requests), 1)

                dialog = QDialog(window)
                dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                dialog.show()
                self.assertEqual(user.GetWindowLongPtrW(int(dialog.winId()), -8), host.hwnd)
                dialog.close()
                dialog.deleteLater()
                self.assertTrue(host.bridge.set_material(False, False))
                host.last_cache_key = None
                host.frame_tick()
                self.assertTrue(host.bridge.set_material(True, True))
                user.SendMessageW(host.hwnd, 5, 1, 0)  # Native minimize notification.
                self.assertFalse(host.frame_timer.isActive())
                with patch.object(host.user, 'IsIconic', return_value=True):
                    window.showMinimized()
                    self.assertFalse(host.frame_timer.isActive())
                window.hide()
                self.assertFalse(host.frame_timer.isActive())
                window.bring_to_front()
                click_search()
                type_key(8)
                QTest.qWait(60)
                self.assertEqual(window.search.text(), '0')
                self.assertTrue(host.frame_timer.isActive())
                self.assertEqual(host.errors, [])
            finally:
                window._close_composition()
                window.force_quit = True
                window.close()
                window.deleteLater()
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
