import unittest
from unittest.mock import Mock

from clipsave_app.monitoring_controller import (
    MonitoringController,
    MonitoringToggleStatus,
)


class _Settings:
    def __init__(self, active=False):
        self.values = {"monitoring": active}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class MonitoringControllerTests(unittest.TestCase):
    def test_success_updates_setting_and_runtime(self):
        settings = _Settings(False)
        service = Mock()
        service.timer.isActive.side_effect = [False, True]
        controller = MonitoringController(settings, service)

        result = controller.toggle()

        self.assertTrue(result.succeeded)
        self.assertTrue(result.active)
        self.assertTrue(settings.get("monitoring"))
        service.start.assert_called_once_with()

    def test_runtime_failure_rolls_back_setting_and_runtime(self):
        settings = _Settings(False)
        service = Mock()
        service.timer.isActive.return_value = False
        service.start.side_effect = RuntimeError("start failed")
        controller = MonitoringController(settings, service)

        result = controller.toggle()

        self.assertIs(result.status, MonitoringToggleStatus.RUNTIME_FAILED)
        self.assertFalse(settings.get("monitoring"))
        service.stop.assert_called_once_with()

    def test_setting_failure_never_changes_runtime(self):
        settings = Mock()
        settings.get.return_value = False
        settings.set.side_effect = OSError("disk full")
        service = Mock()
        service.timer.isActive.return_value = False
        controller = MonitoringController(settings, service)

        result = controller.toggle()

        self.assertIs(result.status, MonitoringToggleStatus.SETTING_SAVE_FAILED)
        service.start.assert_not_called()
        service.stop.assert_not_called()
