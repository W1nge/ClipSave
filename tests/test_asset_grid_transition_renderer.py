import unittest

from PySide6.QtCore import QRect
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from clipsave_app.asset_grid_transition_renderer import AssetGridTransitionRenderer


class AssetGridTransitionRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_clip_insets_text_but_not_images(self):
        preview = QRect(0, 0, 100, 80)
        self.assertEqual(
            AssetGridTransitionRenderer.preview_clip(preview, "image"),
            preview,
        )
        self.assertEqual(
            AssetGridTransitionRenderer.preview_clip(preview, "text"),
            preview.adjusted(10, 9, -10, -9),
        )

    def test_image_target_preserves_aspect_ratio(self):
        pixmap = QPixmap(200, 100)
        target = AssetGridTransitionRenderer.image_target(
            QRect(0, 0, 100, 100),
            pixmap,
        )
        self.assertAlmostEqual(target.width() / target.height(), 2.0, places=3)
