import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

from clipsave_app.windows_backdrop import WindowsCompositionBackdropBridge


class CompositionFrameBridgeTests(unittest.TestCase):
    def setUp(self):
        self.platform = patch('clipsave_app.windows_backdrop.os.name', 'nt')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.bridge = WindowsCompositionBackdropBridge()
        self.bridge._owner_thread_id = threading.get_ident()
        self.bridge._attached_hwnd = 123
        self.native = SimpleNamespace(
            clipsave_acrylic_last_error=lambda: 0,
            clipsave_frame_present=Mock(return_value=1),
        )
        self.bridge._bridge = self.native

    def image(self):
        image = QImage(8, 4, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(20, 40, 60, 128))
        return image

    def test_upload_reads_exact_pixels_without_mutating_backing_cache_key(self):
        image = self.image()
        key = image.cacheKey()
        expected = bytes(image.constBits())

        def upload(pointer, width, height, stride):
            self.assertEqual((width, height, stride), (8, 4, image.bytesPerLine()))
            self.assertEqual(ctypes.string_at(pointer, stride * height), expected)
            return 1

        self.native.clipsave_frame_present.side_effect = upload
        self.assertTrue(self.bridge.present_frame(image))
        self.assertEqual(image.cacheKey(), key)

    def test_old_pixels_remain_alive_if_qt_replaces_the_backing_image_during_upload(self):
        image = self.image()
        expected = bytes(image.constBits())

        def upload(pointer, width, height, stride):
            replacement = QImage(16, 16, QImage.Format.Format_ARGB32_Premultiplied)
            replacement.fill(QColor('red'))
            image.swap(replacement)
            del replacement
            self.assertEqual(ctypes.string_at(pointer, stride * height), expected)
            return 1

        self.native.clipsave_frame_present.side_effect = upload
        self.assertTrue(self.bridge.present_frame(image))
        self.assertEqual(image.width(), 16)

    def test_wrong_thread_and_unsupported_format_never_reach_native_upload(self):
        image = self.image()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.bridge.present_frame(image)))
        worker.start()
        worker.join()
        self.assertEqual(result, [False])
        self.assertEqual(self.bridge.last_error, self.bridge.RPC_E_WRONG_THREAD)
        self.assertFalse(self.bridge.present_frame(QImage(8, 4, QImage.Format.Format_RGB32)))
        self.native.clipsave_frame_present.assert_not_called()

    def test_old_runtime_keeps_backdrop_attach_without_frame_capability(self):
        self.bridge._bridge = SimpleNamespace(
            clipsave_acrylic_is_supported=lambda: 1,
            clipsave_acrylic_attach=lambda hwnd, dark: 1,
            clipsave_acrylic_last_error=lambda: 0,
        )
        self.assertFalse(self.bridge.supports_frames())
        self.assertTrue(self.bridge.attach(456, True))
        self.assertFalse(self.bridge.present_frame(self.image()))
