import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from clipsave_app.constants import APP_NAME, BASE_DIR
from clipsave_app.startup import RUN_KEY, _startup_command, set_start_with_windows


@unittest.skipUnless(os.name == "nt", "Windows startup registration requires Windows")
class StartupTests(unittest.TestCase):
    def test_frozen_command_uses_current_executable(self):
        executable = r"C:\Program Files\ClipSave\ClipSave.exe"
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", executable
        ):
            command = _startup_command()

        self.assertEqual(command, subprocess.list2cmdline([str(Path(executable).resolve())]))

    def test_source_command_uses_repository_launcher(self):
        with patch.object(sys, "frozen", False, create=True), patch(
            "clipsave_app.startup.Path.is_file", return_value=False
        ):
            command = _startup_command()

        self.assertEqual(
            command,
            subprocess.list2cmdline(
                [str(Path(sys.executable).resolve()), str(BASE_DIR / "clipsave.py")]
            ),
        )

    @patch("winreg.SetValueEx")
    @patch("winreg.CreateKeyEx")
    def test_enable_writes_current_user_run_value(self, create_key, set_value):
        import winreg

        key = MagicMock()
        create_key.return_value.__enter__.return_value = key

        set_start_with_windows(True)

        create_key.assert_called_once_with(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        set_value.assert_called_once()
        self.assertEqual(set_value.call_args.args[0:2], (key, APP_NAME))
        self.assertEqual(set_value.call_args.args[4], _startup_command())

    @patch("winreg.DeleteValue")
    @patch("winreg.OpenKey")
    def test_disable_removes_current_user_run_value(self, open_key, delete_value):
        key = MagicMock()
        open_key.return_value.__enter__.return_value = key

        set_start_with_windows(False)

        self.assertEqual(open_key.call_args.args[1], RUN_KEY)
        delete_value.assert_called_once_with(key, APP_NAME)

    @patch("winreg.OpenKey", side_effect=FileNotFoundError)
    def test_disable_is_idempotent(self, _open_key):
        set_start_with_windows(False)


if __name__ == "__main__":
    unittest.main()
