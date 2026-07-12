import tempfile
import unittest
from pathlib import Path

from build_manifest import build_manifest


class BuildManifestTests(unittest.TestCase):
    def test_manifest_is_utf8_without_bom_and_uses_lf_for_unicode_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "中文文件名.txt"
            payload.write_bytes(b"payload")
            manifest = root / "SHA256SUMS.txt"

            self.assertEqual(build_manifest(root, manifest), 1)

            raw = manifest.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", raw)
            self.assertIn("中文文件名.txt", raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
