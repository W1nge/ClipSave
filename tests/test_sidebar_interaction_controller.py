import unittest
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication

from clipsave_app.sidebar_interaction_controller import SidebarInteractionController


class SidebarInteractionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def make_controller(self):
        sidebar = Mock()
        sidebar.width.return_value = 242
        sidebar.collapse_progress = 0.0
        grid = Mock()
        settings = {"sidebar_collapsed": False}
        save_setting = Mock(side_effect=lambda key, value: settings.__setitem__(key, value))
        controller = SidebarInteractionController(
            sidebar=sidebar,
            grid=grid,
            body_layout=Mock(),
            settings_get=lambda key, default=None: settings.get(key, default),
            save_setting=save_setting,
            detail_animation_active=lambda: False,
            finish_detail_animation=Mock(),
            interactive_resize_active=lambda: False,
        )
        return controller, grid, save_setting

    def test_animation_orchestration_suspends_only_grid(self):
        controller, grid, _save = self.make_controller()

        controller.begin_animation()
        controller.update_animation(0.5)
        controller.end_animation()

        self.assertEqual(
            [call.args for call in grid.set_layout_updates_suspended.call_args_list],
            [(True,), (False,)],
        )
        grid.begin_sidebar_transition.assert_called_once_with(242, 0.0)
        grid.set_sidebar_transition_progress.assert_called_once_with(0.5)
        grid.finish_sidebar_transition.assert_called_once_with()

    def test_collapsed_setting_is_debounced_and_deduplicated(self):
        controller, _grid, save = self.make_controller()

        controller.queue_collapsed_setting(True)
        controller.queue_collapsed_setting(True)
        controller.flush_collapsed_setting(force=True)

        save.assert_called_once_with("sidebar_collapsed", True)
        self.assertIsNone(controller.pending_collapsed)
