import unittest

from build_version_info import version_tuple


class BuildVersionInfoTests(unittest.TestCase):
    def test_prerelease_suffix_does_not_change_fixed_file_version(self):
        self.assertEqual(version_tuple("0.2.0-rc1"), (0, 2, 0, 0))

    def test_version_is_padded_and_limited_to_four_components(self):
        self.assertEqual(version_tuple("1.2"), (1, 2, 0, 0))
        self.assertEqual(version_tuple("1.2.3.4.5"), (1, 2, 3, 4))
