import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clipsave_app.smoke_runtime import SmokeLifecycle


class SmokeLifecycleTests(unittest.TestCase):
    def make_lifecycle(self, ready_path: Path):
        app = Mock()
        window = Mock()
        window.quit_application.return_value = False
        database = Mock()
        database.connection.execute.return_value.fetchone.return_value = ("ok",)
        lifecycle = SmokeLifecycle(
            app,
            window,
            database,
            ready_path,
            0,
            failure=lambda _window, _errors: None,
            backdrop_status=lambda _window: "backdrop_success=True\n",
            background_idle=lambda _window: False,
        )
        return lifecycle, app, window

    def test_ready_retry_limit_writes_timeout_and_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            lifecycle, app, _window = self.make_lifecycle(Path(temp) / "ready.txt")
            lifecycle.ready_attempts = lifecycle.MAX_ATTEMPTS - 1

            with patch("clipsave_app.smoke_runtime.QTimer.singleShot") as single_shot:
                lifecycle._mark_ready()

            app.exit.assert_called_once_with(1)
            single_shot.assert_not_called()
            self.assertIn(
                "smoke_ready_timeout=True",
                lifecycle.status_path.read_text(encoding="utf-8"),
            )

    def test_quit_retry_limit_writes_timeout_and_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            lifecycle, app, window = self.make_lifecycle(Path(temp) / "ready.txt")
            lifecycle.quit_attempts = lifecycle.MAX_ATTEMPTS - 1

            with patch("clipsave_app.smoke_runtime.QTimer.singleShot") as single_shot:
                lifecycle._quit()

            window.quit_application.assert_called_once_with()
            app.exit.assert_called_once_with(1)
            single_shot.assert_not_called()
            self.assertIn(
                "smoke_quit_timeout=True",
                lifecycle.status_path.read_text(encoding="utf-8"),
            )
