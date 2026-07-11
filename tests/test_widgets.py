import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from clipsave_app.widgets import _THUMBNAIL_CACHE, thumbnail_pixmap


class ThumbnailPixmapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _THUMBNAIL_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        _THUMBNAIL_CACHE.clear()
        self.temp.cleanup()

    def test_thumbnail_is_scaled_and_cached(self):
        path = Path(self.temp.name) / "large.png"
        image = QImage(1440, 900, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(path)))

        first = thumbnail_pixmap(path)
        second = thumbnail_pixmap(path)

        self.assertFalse(first.isNull())
        self.assertLessEqual(first.width(), 360)
        self.assertLessEqual(first.height(), 180)
        self.assertEqual(first.cacheKey(), second.cacheKey())
        self.assertEqual(len(_THUMBNAIL_CACHE), 1)


if __name__ == "__main__":
    unittest.main()
