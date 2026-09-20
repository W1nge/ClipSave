import unittest
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication

from clipsave_app.detail_animation_controller import DetailAnimationController


class _Window:
    def screen(self):
        return None


class DetailAnimationControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_desired_width_is_clamped_to_available_pane(self):
        detail = Mock()
        grid = Mock()
        splitter = Mock()
        splitter.width.return_value = 700
        splitter.handleWidth.return_value = 1
        controller = DetailAnimationController(
            _Window(),
            detail=detail,
            grid=grid,
            content_splitter=splitter,
            body_layout=Mock(),
            detail_button=Mock(),
            sidebar=Mock(),
            sidebar_animation_active=lambda: False,
            interactive_resize_active=lambda: False,
        )
        controller.saved_width = 600

        self.assertEqual(controller.desired_width(), 459)

    def test_set_progress_updates_splitter_and_grid_transition(self):
        detail = Mock()
        grid = Mock()
        splitter = Mock()
        splitter.width.return_value = 1000
        splitter.handleWidth.return_value = 1
        controller = DetailAnimationController(
            _Window(),
            detail=detail,
            grid=grid,
            content_splitter=splitter,
            body_layout=Mock(),
            detail_button=Mock(),
            sidebar=Mock(),
            sidebar_animation_active=lambda: False,
            interactive_resize_active=lambda: False,
        )
        controller.target_width = 400

        controller.set_progress(0.5)

        splitter.setSizes.assert_called_once_with([799, 200])
        grid.set_sidebar_transition_progress.assert_called_once_with(0.5)
