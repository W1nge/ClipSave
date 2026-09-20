import unittest
from unittest.mock import Mock

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
