import unittest

from clipsave_app.native_clipboard_formats import (
    dib_as_bmp,
    validate_registered_image_header,
)


class NativeClipboardFormatTests(unittest.TestCase):
    def test_png_header_validation_rejects_invalid_signature(self):
        with self.assertRaisesRegex(ValueError, "Invalid registered PNG"):
            validate_registered_image_header("PNG", 24, b"x" * 24)

    def test_dib_as_bmp_builds_bitmap_file_header_for_valid_24bpp_dib(self):
        width = 1
        height = 1
        row = b"\x00\x00\x00\x00"
        header = (
            (40).to_bytes(4, "little")
            + width.to_bytes(4, "little", signed=True)
            + height.to_bytes(4, "little", signed=True)
            + (1).to_bytes(2, "little")
            + (24).to_bytes(2, "little")
            + (0).to_bytes(4, "little")
            + len(row).to_bytes(4, "little")
            + b"\x00" * 16
        )

        bmp = dib_as_bmp("DIB", header + row)

        self.assertEqual(bmp[:2], b"BM")
        self.assertEqual(int.from_bytes(bmp[10:14], "little"), 54)
        self.assertEqual(bmp[14:], header + row)
