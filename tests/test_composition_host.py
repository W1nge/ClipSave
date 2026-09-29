"""Native integration regression; uses a hidden host and a temporary library."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QPoint, Qt, QTimer
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

    def test_detail_wheel_native_coordinates_and_resize(self):
        from clipsave_app.windows_frame import _user32
        user = _user32()
        real_show = user.ShowWindow

        class HiddenUser:
            def __getattr__(self, name):
                if name == 'ShowWindow':
                    return lambda hwnd, command: real_show(hwnd, 0)
                return getattr(user, name)

        with tempfile.TemporaryDirectory(prefix='clipsave-wheel-test-') as temporary:
            paths = AppPaths.build(base_dir=Path(__file__).resolve().parents[1], local_root=Path(temporary))
            database = LibraryDatabase(paths=paths)
            item_id = database.add_text('Scrollable preview paragraph.\n' * 100)
            settings = Settings(paths.settings_path)
            settings.set('monitoring', False)
            with patch('clipsave_app.windows_composition_host._user32', HiddenUser):
                window = CompositionMainWindow(database, settings, QIcon(),
                    paths=paths, scan_on_start=False, reconcile_on_start=False)
            window.tray.hide()
            host = window._composition_host
            self.assertIsNotNone(host)
            try:
                window.show()
                window.show_item_detail(item_id)
                QTest.qWait(350)
                detail = window.detail
                user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
                user.SendMessageW.restype = C.c_ssize_t

                def wheel(widget):
                    point = widget.mapTo(window, widget.rect().center())
                    scale = user.GetDpiForWindow(host.hwnd) / 96
                    point = W.POINT(round(point.x() * scale), round(point.y() * scale))
                    user.ClientToScreen(host.hwnd, C.byref(point))
                    position = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
                    user.SendMessageW(host.hwnd, 0x20A, ((-120) & 0xffff) << 16, position)

                for x, width in ((150, 980), (-400, 900)):
                    user.SetWindowPos(host.hwnd, None, x, 100, width, 600, 0x14)
                    QTest.qWait(100)
                    inner, outer = detail.text_preview.verticalScrollBar(), detail.verticalScrollBar()
                    outer.setValue(0)
                    inner.setValue(0)
                    wheel(detail.preview_caption)
                    self.assertGreater(outer.value(), 0)
                    outer.setValue(0)
                    wheel(detail.text_preview.viewport())
                    self.assertGreater(inner.value(), 0)
                    self.assertEqual(outer.value(), 0)
                    inner.setValue(inner.maximum())
                    wheel(detail.text_preview.viewport())
                    self.assertGreater(outer.value(), 0)
                    detail.set_collections([dict(id=1, name='Work'), dict(id=2, name='Reference')])
                    combo = detail.collection_combo
                    detail.ensureWidgetVisible(combo)
                    self.app.processEvents()
                    point = combo.mapTo(window, combo.rect().center())
                    scale = user.GetDpiForWindow(host.hwnd) / 96
                    position = (round(point.x() * scale) & 0xffff) | ((round(point.y() * scale) & 0xffff) << 16)
                    user.SendMessageW(host.hwnd, 0x201, 1, position)
                    user.SendMessageW(host.hwnd, 0x202, 0, position)
                    popup = combo.collection_popup
                    # Inspect this synchronous native click before pumping OS
                    # activation events: this fixture deliberately hides the
                    # host, so the desktop can immediately deactivate it and
                    # correctly dismiss the popup. Dismissal is tested below
                    # and in the detail widget suite.
                    self.assertTrue(popup.isVisible())
                    self.assertFalse(popup.isWindow())
                    self.assertFalse(popup.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
                    anchor = combo.mapTo(window, QPoint())
                    self.assertEqual(popup.x(), anchor.x())
                    self.assertTrue(popup.y() == anchor.y() + combo.height()
                                    or popup.y() + popup.height() == anchor.y())
                    self.assertTrue(window.rect().contains(popup.geometry()))
                    self.assertEqual(popup.width(), combo.width())
                    selection = []
                    detail.collection_changed.disconnect()
                    detail.collection_changed.connect(lambda *args: selection.append(args))
                    index = combo.model().index(1, 0)
                    local = combo.collection_list.visualRect(index).center()
                    point = combo.collection_list.viewport().mapTo(window, local)
                    scale = user.GetDpiForWindow(host.hwnd) / 96
                    position = (round(point.x() * scale) & 0xffff) | ((round(point.y() * scale) & 0xffff) << 16)
                    self.assertIs(window.childAt(point), combo.collection_list.viewport())
                    user.SendMessageW(host.hwnd, 0x201, 1, position)
                    user.SendMessageW(host.hwnd, 0x202, 0, position)
                    self.assertEqual(combo.currentIndex(), 1)
                    self.assertFalse(popup.isVisible())
                    self.assertEqual(selection, [(item_id, 1)])
                    combo.setCurrentIndex(0)
                    selected = combo.currentIndex()
                    before = outer.value()
                    wheel(combo)
                    self.assertEqual(combo.currentIndex(), selected)
                    self.assertGreater(outer.value(), before)
                self.assertEqual(host.errors, [])
            finally:
                window._close_composition()
                window.force_quit = True
                # Exercise resource cleanup without exiting the QApplication
                # shared by later tests (exit also aborts their nested loops).
                with patch.object(QApplication, 'exit'):
                    window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

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

                # Native resize messages can arrive faster than presentation.
                # They must not keep postponing the next frame until release.
                frame_interval = host.frame_timer.interval()
                host.frame_timer.setInterval(32)
                before_resize = host.frame_count
                resize_events = []
                layout_widths = set()
                resize_timer = QTimer()
                resize_timer.setTimerType(Qt.TimerType.PreciseTimer)
                resize_timer.setInterval(4)
                loop = QEventLoop()

                def resize_continuously():
                    width = 960 + len(resize_events) % 100
                    user.SetWindowPos(host.hwnd, None, 150, 100, width, 650, 0x14)
                    resize_events.append(width)
                    layout_widths.add(window.width())

                resize_timer.timeout.connect(resize_continuously)
                window.native_window_controller.begin_interactive_resize()
                resize_timer.start()
                QTimer.singleShot(400, loop.quit)
                try:
                    loop.exec()
                finally:
                    resize_timer.stop()
                    frames_during_resize = host.frame_count - before_resize
                    window.native_window_controller.end_interactive_resize()
                    host.frame_timer.setInterval(frame_interval)
                self.assertGreater(len(resize_events), 10)
                self.assertGreaterEqual(
                    frames_during_resize, 3,
                    f'Only {frames_during_resize} frames during {len(resize_events)} continuous size changes',
                )
                self.assertGreater(len(layout_widths), 2, 'Qt layout did not follow the drag')

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

                # Metadata refresh replaces the very button currently under
                # the cursor. Exercise the native message boundary, then click
                # the replacement navigation button and the search box.
                old_button = window.sidebar.nav_buttons['all']
                point = old_button.mapTo(window, old_button.rect().center())
                lp = point.x() | (point.y() << 16)
                user.SendMessageW(host.hwnd, 0x200, 0, lp)
                window.sidebar.set_primary({'all': 10})
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
                navigation = []
                window.sidebar.navigation_requested.connect(
                    lambda key, ident: navigation.append((key, ident)))
                new_button = window.sidebar.nav_buttons['all']
                point = new_button.mapTo(window, new_button.rect().center())
                lp = point.x() | (point.y() << 16)
                user.SendMessageW(host.hwnd, 0x201, 1, lp)
                user.SendMessageW(host.hwnd, 0x202, 0, lp)
                self.assertEqual(navigation, [('all', None)])
                QTest.qWait(250)
                click_search()
                before_typing = window.search.text()
                type_key(0x31)
                QTest.qWait(60)
                self.assertEqual(window.search.text(), before_typing + '1')

                # The detail header sits outside the scrolling viewport, but
                # still needs native hit testing and focus-loss note saving.
                window.search.clear()
                window.search_timer.stop()
                window.refresh_library()
                detail_id = window.current_items[0]['id']
                window.show_item_detail(detail_id)
                QTest.qWait(350)
                detail = window.detail
                detail.info_toggle.click()
                QTest.qWait(80)
                self.assertGreater(detail.verticalScrollBar().maximum(), 0)
                point = detail.tags_empty.mapTo(window, detail.tags_empty.rect().center())
                scale = user.GetDpiForWindow(host.hwnd) / 96
                screen_point = W.POINT(round(point.x() * scale), round(point.y() * scale))
                user.ClientToScreen(host.hwnd, C.byref(screen_point))
                wheel_position = (screen_point.x & 0xffff) | ((screen_point.y & 0xffff) << 16)
                user.SendMessageW(host.hwnd, 0x20A, ((-120) & 0xffff) << 16, wheel_position)
                self.assertGreater(detail.verticalScrollBar().value(), 0,
                                   'Wheel over a detail label must reach the outer scroll area')
                copied = []
                detail.copy_requested.disconnect()
                detail.copy_requested.connect(copied.append)

                def click_widget(widget):
                    point = widget.mapTo(window, widget.rect().center())
                    scale = user.GetDpiForWindow(host.hwnd) / 96
                    lp = round(point.x() * scale) | (round(point.y() * scale) << 16)
                    user.SendMessageW(host.hwnd, 0x201, 1, lp)
                    user.SendMessageW(host.hwnd, 0x202, 0, lp)

                detail.ensureWidgetVisible(detail.notes)
                QTest.qWait(60)
                click_widget(detail.notes.viewport())
                type_key(0x41)
                QTest.qWait(60)
                self.assertEqual(detail.notes.toPlainText().lower(), 'a')
                detail.verticalScrollBar().setValue(detail.verticalScrollBar().maximum())
                click_widget(detail.copy_button)
                QTest.qWait(100)
                self.assertEqual(copied, [detail_id])
                self.assertEqual(database.get_item(detail_id)['notes'].lower(), 'a')
                user.SetWindowPos(host.hwnd, None, 150, 100, 980, 620, 0x14)
                QTest.qWait(80)
                click_widget(detail.copy_button)
                self.assertEqual(copied, [detail_id, detail_id])
                self.assertEqual(host.errors, [])
            finally:
                window._close_composition()
                window.force_quit = True
                with patch.object(QApplication, 'exit'):
                    window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
