import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from clipsave_app.database import LibraryDatabase
from clipsave_app.services import ClipboardService


class ClipboardServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = LibraryDatabase(Path(self.temp.name) / "test.db")
        self.service = ClipboardService(self.database)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_image_key_uses_stable_pixel_content(self):
        image = QImage(32, 24, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#21a8fb"))
        self.assertEqual(self.service.image_key(image), self.service.image_key(image.copy()))

    def test_unchanged_clipboard_sequence_skips_polling(self):
        self.service.last_clipboard_sequence = 42
        self.service.clipboard_sequence = lambda: 42
        self.service.save_text = lambda _text: self.fail("unchanged clipboard was processed")
        self.service.save_image = lambda _image: self.fail("unchanged clipboard was processed")
        self.service.poll()


if __name__ == "__main__":
    unittest.main()
