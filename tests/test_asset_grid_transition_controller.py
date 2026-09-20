import unittest
from unittest.mock import Mock

from PySide6.QtCore import QSize

from clipsave_app.asset_grid_transition_controller import AssetGridTransitionController


class AssetGridTransitionControllerTests(unittest.TestCase):
    def test_layout_geometry_matches_grid_contract(self):
        columns, grid_size = AssetGridTransitionController.layout_for_viewport_width(900)

        self.assertGreaterEqual(columns, 1)
        self.assertIsInstance(grid_size, QSize)
        self.assertGreater(grid_size.width(), 0)
        self.assertGreater(grid_size.height(), 0)

    def test_clear_releases_overlay_state(self):
        view = Mock()
        view.viewport.return_value.isVisible.return_value = False
        controller = AssetGridTransitionController(view)
        controller.active = True
        controller.overlay = Mock()

        controller.clear(repaint=True)

        self.assertFalse(controller.active)
        self.assertIsNone(controller.overlay)
        view.viewport.return_value.repaint.assert_not_called()
