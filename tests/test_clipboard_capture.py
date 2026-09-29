import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from clipsave_app.clipboard_service import ClipboardService
from clipsave_app.app_paths import AppPaths
from clipsave_app.database import LibraryDatabase
from clipsave_app.native_clipboard_reader import ClipboardBusy


class WindowsClipboardCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        paths = AppPaths.build(base_dir=Path(self.temporary.name) / 'app',
                               local_root=Path(self.temporary.name) / 'profile')
        self.database = LibraryDatabase(paths=paths)
        self.service = ClipboardService(self.database, paths=paths)
        self.service.last_clipboard_sequence = 10
        self.service.clipboard_sequence = Mock(return_value=11)
        self.service._snapshot_clipboard_file_paths = Mock(return_value=None)
        self.service._native_clipboard_image_snapshot = Mock(return_value=None)
        self.service._native_clipboard_text_snapshot = Mock(return_value='External application 中文')
        # OLE can temporarily return no advertised formats even though the
        # Win32 clipboard contains text. It must not gate native acquisition.
        self.clipboard = Mock()
        self.clipboard.mimeData.return_value.hasUrls.return_value = False
        self.clipboard.mimeData.return_value.hasImage.return_value = False
        self.clipboard.mimeData.return_value.hasText.return_value = False
        self.service._clipboard = Mock(return_value=self.clipboard)

    def tearDown(self):
        self.service.shutdown()
        self.database.close()
        self.temporary.cleanup()

    def test_external_text_is_captured_when_qt_advertises_no_formats(self):
        saved = []
        self.service.save_text = lambda text: saved.append(text) or True
        with patch('clipsave_app.clipboard_service.os.name', 'nt'):
            self.service.poll()
        self.assertTrue(self.service.wait_for_idle(1))
        self.assertEqual(saved, ['External application 中文'])
        self.service._clipboard.assert_not_called()
        self.assertEqual(self.service.last_clipboard_sequence, 11)

    def test_external_png_does_not_require_qt_mime_detection(self):
        source = QImage(3, 2, QImage.Format.Format_RGB32)
        source.fill(QColor('#129ace'))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(source.save(buffer, 'PNG'))
        self.service._native_clipboard_image_snapshot.return_value = ('PNG', bytes(buffer.data()))
        with patch('clipsave_app.clipboard_service.os.name', 'nt'):
            snapshot = self.service._read_stable_snapshot()
        self.assertIsNotNone(snapshot)
        kind, image, sequence = snapshot
        self.assertEqual((kind, image.size(), sequence), ('image', source.size(), 11))
        self.assertEqual(image.pixelColor(0, 0), source.pixelColor(0, 0))
        self.service._clipboard.assert_not_called()

    def test_busy_native_read_retries_same_sequence_then_saves(self):
        saved, errors = [], []
        self.service.save_text = lambda text: saved.append(text) or True
        self.service.failed.connect(errors.append)
        self.service._monitoring_enabled = True
        self.service._snapshot_clipboard_file_paths.side_effect = [ClipboardBusy('held by writer'), None]
        with patch('clipsave_app.clipboard_service.os.name', 'nt'):
            self.service.poll()
            self.assertEqual(self.service.last_clipboard_sequence, 10)
            self.assertTrue(self.service._clipboard_retry_timer.isActive())
            QTest.qWait(80)
        self.assertTrue(self.service.wait_for_idle(1))
        self.assertEqual(saved, ['External application 中文'])
        self.assertEqual(errors, [])
        self.assertEqual(self.service.last_clipboard_sequence, 11)

    def test_notification_defers_read_until_native_message_dispatch_finishes(self):
        self.service._monitoring_enabled = True
        self.service.poll = Mock()
        self.service.notifier.changed.emit()
        self.service.poll.assert_not_called()
        QTest.qWait(5)
        self.service.poll.assert_called_once_with()

    def test_files_keep_priority_over_image_and_text(self):
        paths = 'C:/Test/one.txt\nC:/Test/two.png'
        self.service._snapshot_clipboard_file_paths.return_value = paths
        with patch('clipsave_app.clipboard_service.os.name', 'nt'):
            self.assertEqual(self.service._read_stable_snapshot(), ('text', paths, 11))
        self.service._native_clipboard_image_snapshot.assert_not_called()
        self.service._native_clipboard_text_snapshot.assert_not_called()

    def test_invalid_image_falls_back_to_native_text(self):
        self.service._native_clipboard_image_snapshot.return_value = ('PNG', b'invalid image')
        with patch('clipsave_app.clipboard_service.os.name', 'nt'):
            self.assertEqual(self.service._read_stable_snapshot(), ('text', 'External application 中文', 11))
        self.service._clipboard.assert_not_called()

    def test_busy_clipboard_keeps_retrying_after_initial_backoff(self):
        self.service._clipboard_retry_attempt = len(self.service.CLIPBOARD_RETRY_DELAYS_MS)
        with patch.object(self.service, '_read_stable_snapshot', side_effect=ClipboardBusy('still busy')):
            self.service.poll()
        self.assertTrue(self.service._clipboard_retry_timer.isActive())
        self.assertEqual(self.service._clipboard_retry_timer.interval(), 1000)
        self.assertEqual(self.service.last_clipboard_sequence, 10)

    def test_new_change_expedites_busy_retry_and_does_not_reenter_read(self):
        self.service._monitoring_enabled = True
        self.service._clipboard_retry_timer.start(1000)
        depth, max_depth, reads = 0, 0, 0

        def read():
            nonlocal depth, max_depth, reads
            depth += 1
            max_depth = max(max_depth, depth)
            reads += 1
            if reads == 1:
                self.service.notifier.changed.emit()
                self.app.processEvents()
            depth -= 1
            return None

        self.service._read_stable_snapshot = read
        self.service.notifier.changed.emit()
        self.assertEqual(self.service._clipboard_retry_timer.interval(), 0)
        QTest.qWait(20)
        self.assertEqual(reads, 2)
        self.assertEqual(max_depth, 1)

    def test_changing_native_sequence_retries_without_committing_stale_data(self):
        self.service.clipboard_sequence = Mock(side_effect=[11, 12, 12, 12])
        self.service._native_clipboard_text_snapshot.side_effect = ['old', 'new']
        with patch('clipsave_app.clipboard_service.os.name', 'nt'):
            self.assertEqual(self.service._read_stable_snapshot(), ('text', 'new', 12))

    def test_native_buffer_reaches_database_without_any_qt_clipboard_access(self):
        # Exercise the real bounded native readers through ctypes buffers,
        # then the actual persistence worker, files and SQLite transaction.
        for name in ('_snapshot_clipboard_file_paths', '_native_clipboard_image_snapshot',
                     '_native_clipboard_text_snapshot'):
            delattr(self.service, name)
        expected = 'External Win32 buffer 中文\nsecond line'
        payload = expected.encode('utf-16-le') + b'\0\0'
        buffer = ctypes.create_string_buffer(payload)
        user, kernel, shell = Mock(), Mock(), Mock()
        user.OpenClipboard.return_value = True
        user.IsClipboardFormatAvailable.side_effect = lambda fmt: fmt == 13
        user.EnumClipboardFormats.return_value = 0
        user.GetClipboardData.return_value = 123
        kernel.GetLastError.return_value = 0
        kernel.GlobalSize.return_value = len(payload)
        kernel.GlobalLock.return_value = ctypes.addressof(buffer)
        with patch('clipsave_app.clipboard_service.os.name', 'nt'), patch.object(
            ClipboardService, '_windows_clipboard_apis', return_value=(user, kernel)
        ), patch.object(ClipboardService, '_windows_shell_api', return_value=shell):
            self.service.poll()
        self.assertTrue(self.service.wait_for_idle(1))
        rows = self.database.connection.execute('SELECT content FROM items').fetchall()
        self.assertEqual([row['content'] for row in rows], [expected])
        self.assertEqual(user.OpenClipboard.call_count, user.CloseClipboard.call_count)
        kernel.GlobalUnlock.assert_called_once_with(123)
        self.service._clipboard.assert_not_called()
        self.assertEqual(self.service.last_clipboard_sequence, 11)
