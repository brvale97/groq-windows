import tempfile
import unittest
from pathlib import Path

from PIL import Image

from branding import ICO_SIZES, draw_icon, write_icon


class WindowsIconFileTests(unittest.TestCase):
    def test_every_embedded_size_matches_a_direct_render(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_icon(Path(directory) / "app.ico")
            with Image.open(path) as icon:
                self.assertEqual(icon.ico.sizes(), {(size, size) for size in ICO_SIZES})
                for size in ICO_SIZES:
                    self.assertEqual(
                        icon.ico.getimage((size, size)).tobytes(),
                        draw_icon(size).tobytes(),
                        f"Icon at {size}px was resampled or lost transparency",
                    )


if __name__ == "__main__":
    unittest.main()
