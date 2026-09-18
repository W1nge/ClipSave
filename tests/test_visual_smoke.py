import unittest

from PIL import Image, ImageDraw

from verify_windows_visual_smoke import analyze_image, looks_like_rendered_ui


class VisualSmokeAnalysisTests(unittest.TestCase):
    def test_flat_backdrop_without_ui_is_rejected(self):
        image = Image.new("RGB", (800, 500), (30, 55, 80))
        self.assertFalse(looks_like_rendered_ui(analyze_image(image)))

    def test_structured_ui_frame_is_accepted(self):
        image = Image.new("RGB", (800, 500), (30, 55, 80))
        draw = ImageDraw.Draw(image)
        for x in range(0, 800, 24):
            shade = 70 + (x // 24) % 120
            draw.rectangle((x, 0, min(799, x + 11), 499), fill=(shade, 110, 150))
        for y in range(20, 480, 36):
            draw.line((20, y, 780, y), fill=(245, 245, 245), width=2)
        self.assertTrue(looks_like_rendered_ui(analyze_image(image)))


if __name__ == "__main__":
    unittest.main()
