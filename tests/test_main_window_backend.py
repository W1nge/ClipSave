import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QObject

from clipsave_app.bulk_checkpoint import checkpoint_path
from clipsave_app.main_window_backend import MainWindowBackend


class MainWindowBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_backend_owns_one_task_supervisor_for_all_async_controllers(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Mock()
            settings.path = Path(temp) / "settings.json"
            parent = QObject()
            backend = MainWindowBackend(
                Mock(),
                settings,
                parent=parent,
                start_regular=Mock(),
                cancel_regular=Mock(),
                start_bounded=Mock(),
            )

            self.assertIs(backend.library.supervisor, backend.tasks)
            self.assertIs(backend.maintenance.supervisor, backend.tasks)
            self.assertIs(backend.images.supervisor, backend.tasks)
            self.assertIs(backend.mutations.supervisor, backend.tasks)
            self.assertIs(
                backend.images.work_coordinator,
                backend.bulk_images.work_coordinator,
            )
            self.assertEqual(
                backend.bulk_images.checkpoint_path,
                checkpoint_path(settings.path),
            )

            clipboard = Mock()
            grid = Mock()
            detail = Mock()
            monitoring, shutdown = backend.attach_runtime(
                clipboard,
                grid,
                detail,
            )
            self.assertIs(backend.monitoring, monitoring)
            self.assertIs(backend.shutdown, shutdown)
            self.assertIs(monitoring.clipboard_service, clipboard)
            self.assertIs(shutdown.grid, grid)
            self.assertIs(shutdown.detail, detail)

    def test_backend_is_self_sufficient_without_window_task_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Mock()
            settings.path = Path(temp) / "settings.json"
            backend = MainWindowBackend(
                Mock(),
                settings,
                parent=QObject(),
            )
            token = object()
            ran = threading.Event()

            backend.start_regular(token, lambda _cancel: ran.set())

            self.assertTrue(ran.wait(1.0))
            self.assertTrue(backend.tasks.wait_for_token(token, 1.0))
            self.assertIsNone(backend.images._start_bounded)

    def test_backend_owns_background_shutdown_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Mock()
            settings.path = Path(temp) / "settings.json"
            backend = MainWindowBackend(
                Mock(),
                settings,
                parent=QObject(),
                start_regular=Mock(),
                cancel_regular=Mock(),
                start_bounded=Mock(),
            )
            with (
                patch.object(backend.library, "cancel_refresh") as cancel_refresh,
                patch.object(backend.library, "cancel_search") as cancel_search,
                patch.object(backend.library, "cancel_page") as cancel_page,
                patch.object(
                    backend.images,
                    "cancel_for_shutdown",
                    return_value={"image"},
                ),
                patch.object(
                    backend.mutations,
                    "cancel_for_shutdown",
                    return_value={"mutation"},
                ),
                patch.object(backend.bulk_images, "cancel", return_value="bulk"),
            ):
                cancelled = backend.cancel_background_requests()

            cancel_refresh.assert_called_once_with()
            cancel_search.assert_called_once_with()
            cancel_page.assert_called_once_with()
            self.assertEqual(cancelled, {"image", "mutation", "bulk"})
