import unittest

from check_pyinstaller_warnings import unexpected_missing_modules


class PyInstallerWarningTests(unittest.TestCase):
    def test_known_optional_modules_are_allowed(self):
        text = (
            "missing module named pwd - imported by pathlib (optional)\n"
        )
        self.assertEqual(unexpected_missing_modules(text), [])

    def test_new_missing_module_fails_closed(self):
        text = "missing module named clipsave_required - imported by clipsave_app (top-level)\n"
        self.assertEqual(unexpected_missing_modules(text), ["clipsave_required"])


if __name__ == "__main__":
    unittest.main()
