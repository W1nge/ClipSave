import unittest
from unittest.mock import Mock, patch

from clipsave_app.services import BackdropBackend, BackdropResult
from clipsave_app.window_effects_controller import WindowEffectsController


class WindowEffectsControllerTests(unittest.TestCase):
    def test_power_notification_tracks_hwnd_and_releases_previous_handle(self):
        window = Mock()
        window.winId.side_effect = [123, 123, 456]
        registered = []
        released = []
        handles = iter([55, 66])
        controller = WindowEffectsController(
            window,
            platform_check=lambda: True,
            apply_backdrop=Mock(),
            register_power=lambda hwnd: registered.append(hwnd) or next(handles),
            unregister_power=lambda handle: released.append(handle),
            sync_surface_style=Mock(),
        )

        controller.ensure_power_notification()
        controller.ensure_power_notification()
        controller.ensure_power_notification()
        controller.release_power_notification()

        self.assertEqual(registered, [123, 456])
        self.assertEqual(released, [55, 66])

    def test_proposed_resize_geometry_is_applied_without_z_order_mutation(self):
        window = Mock()
        window.winId.return_value = 123
        window.isMinimized.return_value = False
        window.isVisible.return_value = True
        controller = WindowEffectsController(
            window,
            platform_check=lambda: True,
            apply_backdrop=Mock(),
            register_power=Mock(),
            unregister_power=Mock(),
            sync_surface_style=Mock(),
        )
        controller.native_backdrop_result = BackdropResult(
            BackdropBackend.WIN10_EFFECT_ACRYLIC,
            True,
        )
        controller.backdrop_window_hwnd = 456

        with patch(
            "clipsave_app.window_effects_controller.sync_backdrop_host_window"
        ) as sync:
            controller.sync_proposed_rect(20, 30, 1220, 830)

        sync.assert_called_once_with(
            456,
            123,
            20,
            30,
            1200,
            800,
            visible=True,
            sync_z_order=False,
        )
