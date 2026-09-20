import tempfile
import unittest
from pathlib import Path

from clipsave_app.app_paths import AppPaths
from clipsave_app.runtime import ApplicationRuntime


class ApplicationRuntimeTests(unittest.TestCase):
    def test_runtime_owns_paths_database_settings_and_lazy_clipboard_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = AppPaths.build(base_dir=root / "app", local_root=root / "profile")
            runtime = ApplicationRuntime.create(paths)
            try:
                self.assertIs(runtime.database.paths, paths)
                self.assertEqual(runtime.settings.path, paths.settings_path)
                self.assertIs(runtime.clipboard_service.paths, paths)
                self.assertIsNone(runtime.clipboard_service._worker)
                self.assertIsNone(runtime.clipboard_service.parent())
            finally:
                self.assertTrue(runtime.close())

    def test_close_core_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = ApplicationRuntime.create(
                AppPaths.build(base_dir=root / "app", local_root=root / "profile")
            )
            runtime.close_core(executor_timeout=0.0)
            runtime.close_core(executor_timeout=0.0)
            self.assertTrue(runtime._core_closed)
