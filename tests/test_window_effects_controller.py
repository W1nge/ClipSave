import unittest
from unittest.mock import Mock, patch

from clipsave_app.services import BackdropBackend, BackdropResult
from clipsave_app.window_effects_controller import WindowEffectsController


class WindowEffectsControllerTests(unittest.TestCase):
    def test_pending_frame_backdrop_only_shrinks_inside_native_host(self):
        window = Mock()
        window.winId.return_value = 123
        window.isVisible.return_value = True
        controller = WindowEffectsController(
            window, platform_check=lambda: True, apply_backdrop=Mock(),
            register_power=Mock(), unregister_power=Mock(), sync_surface_style=Mock(),
        )
        controller.native_backdrop_result = BackdropResult(
            BackdropBackend.WIN10_EFFECT_ACRYLIC, True,
        )
        controller.backdrop_window_hwnd = 456
        previous = (100, 100, 1500, 1000)
        for host, expected in (
            ((100, 100, 1200, 800), (100, 100, 1100, 700)),
            ((300, 200, 1500, 1000), (300, 200, 1200, 800)),
            ((50, 50, 1700, 1100), None),
        ):
            with self.subTest(host=host), patch(
                "clipsave_app.window_effects_controller.window_rect",
                side_effect=lambda hwnd: host if hwnd == 123 else previous,
            ), patch(
                "clipsave_app.window_effects_controller.sync_backdrop_host_window",
            ) as sync:
                controller.constrain_geometry_to_host()
                if expected is None:
                    sync.assert_not_called()
                else:
                    sync.assert_called_once_with(
                        456, 123, *expected, visible=True, sync_z_order=False,
                    )

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

    def test_committed_resize_geometry_is_applied_without_z_order_mutation(self):
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
        ) as sync, patch(
            "clipsave_app.window_effects_controller.window_rect",
            return_value=(20, 30, 1220, 830),
        ):
            controller.sync_geometry_now()

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
