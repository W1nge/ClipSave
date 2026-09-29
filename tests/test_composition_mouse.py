"""Mouse routing must survive Qt replacing widgets under the cursor."""
import os
from types import SimpleNamespace
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from clipsave_app.sidebar import Sidebar


@unittest.skipUnless(os.name == 'nt', 'Windows mouse message routing')
class CompositionMouseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from clipsave_app.windows_composition_host import WindowsCompositionHost
        self.window = QWidget()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.resize(500, 850)
        layout = QVBoxLayout(self.window)
        self.sidebar = Sidebar(self.window)
        self.sidebar.set_primary({'all': 6})
        layout.addWidget(self.sidebar)
        self.button = QPushButton('Persistent target')
        layout.addWidget(self.button)
        self.clicks = []
        self.button.clicked.connect(lambda: self.clicks.append(True))
        self.window.show()
        self.app.processEvents()
        self.native_capture = None
        self.host = WindowsCompositionHost.__new__(WindowsCompositionHost)
        self.host.window = self.window
        self.host.hwnd = 42
        self.host.qt_hover = self.host.qt_capture = None
        self.host._hover_position = self.host._hover_global = QPointF()
        self.host._mouse_leave_tracking = False
        self.host.errors = []
        self.tracking_requests = []
        self.host.user = SimpleNamespace(
            GetDpiForWindow=lambda hwnd: 96,
            SetFocus=lambda hwnd: None,
            SetCapture=self.set_capture,
            GetCapture=lambda: self.native_capture,
            ReleaseCapture=lambda: self.set_capture(None),
            TrackMouseEvent=lambda request: self.tracking_requests.append(True) or True,
            DefWindowProcW=lambda *args: 0,
        )
        self.host.input = SimpleNamespace(
            modifiers=lambda: Qt.KeyboardModifier.NoModifier,
            activate=lambda: None, update_position=lambda: None,
            handle=lambda *args: None,
        )

    def set_capture(self, hwnd):
        self.native_capture = hwnd

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def mouse(self, widget, message, buttons=0):
        point = widget.mapTo(self.window, widget.rect().center())
        position = (point.x() & 0xffff) | ((point.y() & 0xffff) << 16)
        self.host.forward_mouse(message, buttons, position)

    def rebuild_sidebar(self):
        self.sidebar.set_primary({'all': 7})
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_detail_wheel_bubbles_and_nested_preview_hands_off_at_its_edge(self):
        from clipsave_app.detail_panel import DetailPanel
        from tests.test_detail_panel import record
        self.sidebar.hide()
        self.button.hide()
        panel = DetailPanel()
        self.window.layout().addWidget(panel)
        self.window.resize(360, 450)
        panel.set_item(record(content='Long preview paragraph.\n' * 100))
        self.app.processEvents()
        self.app.processEvents()
        # Coordinates here are already client-relative; the native integration
        # separately tests the real ScreenToClient conversion.
        self.host.user.ScreenToClient = lambda *args: None

        def wheel(widget, delta=-120, message=0x20A):
            self.mouse(widget, message, (delta & 0xffff) << 16)

        inner = panel.text_preview.verticalScrollBar()
        outer = panel.verticalScrollBar()
        self.assertGreater(inner.maximum(), 0)
        self.assertGreater(outer.maximum(), 0)
        wheel(panel.preview_caption)
        self.assertGreater(outer.value(), 0)
        outer.setValue(0)
        wheel(panel.text_preview.viewport())
        self.assertGreater(inner.value(), 0)
        self.assertEqual(outer.value(), 0)
        inner.setValue(inner.maximum())
        wheel(panel.text_preview.viewport())
        self.assertGreater(outer.value(), 0)
        outer.setValue(0)
        panel.set_item(record())
        panel.set_collections([{'id': 1, 'name': 'Work'}, {'id': 2, 'name': 'Reference'}])
        self.app.processEvents()
        self.app.processEvents()
        wheel(panel.text_preview.viewport())
        self.assertGreater(outer.value(), 0, 'Short previews must not trap the wheel')
        panel.ensureWidgetVisible(panel.notes)
        self.app.processEvents()
        before = outer.value()
        wheel(panel.notes.viewport(), 120)
        self.assertLess(outer.value(), before, 'Empty notes must pass upward scrolling to the panel')
        panel.notes.setPlainText('Long note.\n' * 80)
        panel.ensureWidgetVisible(panel.notes)
        self.app.processEvents()
        notes_scroll = panel.notes.verticalScrollBar()
        notes_scroll.setValue(0)
        before = outer.value()
        wheel(panel.notes.viewport())
        self.assertGreater(notes_scroll.value(), 0)
        self.assertEqual(outer.value(), before)
        panel.notes.clear()
        panel.ensureWidgetVisible(panel.collection_combo)
        self.app.processEvents()
        outer.setValue(0)
        selected = panel.collection_combo.currentIndex()
        changes = []
        panel.collection_changed.connect(lambda *args: changes.append(args))
        wheel(panel.collection_combo)
        self.assertEqual(panel.collection_combo.currentIndex(), selected)
        self.assertEqual(changes, [])
        self.assertGreater(outer.value(), 0)
        panel.close()

    def hover_pixel(self, widget):
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.UpdateRequest)
        image = self.window.backingStore().paintDevice()
        return image.pixelColor(widget.mapTo(self.window, QPoint(3, 3))).name()

    def test_hover_exit_repaints_the_cached_pixels_of_the_previous_button(self):
        self.window.setStyleSheet(
            'QPushButton { background: black; border: 0; } '
            'QPushButton:hover { background: red; }')
        self.app.processEvents()
        first = self.sidebar.nav_buttons['all']
        self.mouse(first, 0x200)
        self.assertEqual(self.hover_pixel(first), '#ff0000')
        self.mouse(self.button, 0x200)
        self.assertFalse(first.underMouse())
        self.assertEqual(self.hover_pixel(first), '#000000')
        self.assertEqual(self.hover_pixel(self.button), '#ff0000')

    def test_leaving_window_and_deactivating_clear_hover_pixels(self):
        self.window.setStyleSheet(
            'QPushButton { background: black; border: 0; } '
            'QPushButton:hover { background: red; }')
        self.app.processEvents()
        for message in (0x2A3, 6):
            with self.subTest(message=message):
                self.mouse(self.button, 0x200)
                self.assertEqual(self.hover_pixel(self.button), '#ff0000')
                self.host.wndproc(self.host.hwnd, message, 0, 0)
                self.assertIsNone(self.host.qt_hover)
                self.assertFalse(self.button.underMouse())
                self.assertEqual(self.hover_pixel(self.button), '#000000')
                self.assertEqual(self.host.errors, [])
        self.assertEqual(len(self.tracking_requests), 2)

    def test_sidebar_refresh_under_hover_does_not_poison_later_clicks(self):
        self.mouse(self.sidebar.nav_buttons['all'], 0x200)
        old_hover = self.host.qt_hover
        self.rebuild_sidebar()
        self.assertFalse(isValid(old_hover))
        self.mouse(self.button, 0x201, 1)
        self.mouse(self.button, 0x202)
        self.assertEqual(self.clicks, [True])

    def test_deleted_pressed_widget_does_not_keep_mouse_capture(self):
        self.mouse(self.sidebar.nav_buttons['all'], 0x201, 1)
        old_capture = self.host.qt_capture
        self.assertEqual(self.native_capture, self.host.hwnd)
        self.rebuild_sidebar()
        self.assertFalse(isValid(old_capture))
        self.mouse(self.button, 0x200)
        self.assertIsNone(self.host.qt_capture)
        self.assertIsNone(self.native_capture)
        self.mouse(self.button, 0x202)
        self.assertEqual(self.clicks, [])
        self.mouse(self.button, 0x201, 1)
        self.mouse(self.button, 0x202)
        self.assertEqual(self.clicks, [True])

    def test_stale_press_does_not_release_capture_owned_by_a_popup(self):
        self.mouse(self.sidebar.nav_buttons['all'], 0x201, 1)
        self.rebuild_sidebar()
        self.native_capture = 99
        self.mouse(self.button, 0x200)
        self.assertIsNone(self.host.qt_capture)
        self.assertEqual(self.native_capture, 99)


if __name__ == '__main__':
    unittest.main()
