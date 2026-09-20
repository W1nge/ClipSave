import unittest
from unittest.mock import Mock, patch

from clipsave_app.shutdown_coordinator import ShutdownCoordinator, ShutdownFailure


class ShutdownCoordinatorTests(unittest.TestCase):
    def make_coordinator(self):
        database = Mock()
        clipboard = Mock()
        clipboard.timer.isActive.return_value = True
        clipboard.wait_for_idle.return_value = True
        clipboard.shutdown.return_value = True
        grid = Mock()
        detail = Mock()
        grid.wait_for_thumbnail_idle.return_value = True
        detail.wait_for_thumbnail_idle.return_value = True
        return ShutdownCoordinator(database, clipboard, grid, detail), database, clipboard, grid, detail

    def test_interactive_shutdown_orders_resources_and_backup(self):
        coordinator, database, clipboard, _grid, _detail = self.make_coordinator()

        result = coordinator.shutdown_interactive_resources()

        self.assertTrue(result.succeeded)
        clipboard.stop.assert_called_once_with()
        clipboard.wait_for_idle.assert_called_once_with(10.0)
        database.create_backup.assert_called_once_with()
        clipboard.shutdown.assert_called_once_with(timeout=10.0)

    def test_backup_failure_restores_clipboard_and_thumbnails(self):
        coordinator, database, clipboard, grid, detail = self.make_coordinator()
        database.create_backup.side_effect = OSError("disk full")

        result = coordinator.shutdown_interactive_resources()

        self.assertIs(result.failure, ShutdownFailure.BACKUP)
        database.record_backup_error.assert_called_once_with("disk full")
        clipboard.resume_after_failed_shutdown.assert_called_once_with(True)
        grid.resume_thumbnail_loader.assert_called_once_with()
        detail.resume_thumbnail_loader.assert_called_once_with()
        clipboard.shutdown.assert_not_called()

    def test_finalize_core_does_not_close_database_when_executor_shutdown_times_out(self):
        coordinator, database, _clipboard, grid, detail = self.make_coordinator()

        with patch(
            "clipsave_app.shutdown_coordinator.shutdown_ai_ocr_task_executor",
            return_value=False,
        ):
            self.assertFalse(coordinator.finalize_core(executor_timeout=0.0))

        grid.shutdown_thumbnail_loader.assert_not_called()
        detail.shutdown_thumbnail_loader.assert_not_called()
        database.close.assert_not_called()
