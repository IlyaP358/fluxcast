import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from ui import tray  # noqa: E402
except ImportError as exc:  # pystray/PIL missing on a headless builder
    tray = None
    _IMPORT_ERROR = exc


@unittest.skipIf(tray is None, "tray dependencies are unavailable")
class TrayResourceTest(unittest.TestCase):
    """The tray resolves assets/ and main.py relative to its own file, so
    moving the module to another directory silently breaks both without
    changing a character of the code (#84 refactor)."""

    def test_icon_path_exists_and_opens(self):
        self.assertTrue(os.path.isfile(tray._ICON_PATH),
                        f"tray icon missing at {tray._ICON_PATH}")
        from PIL import Image
        with Image.open(tray._ICON_PATH) as img:
            self.assertGreater(min(img.size), 0)

    def test_main_entry_point_exists(self):
        self.assertTrue(os.path.isfile(tray._MAIN),
                        f"tray launches casting via {tray._MAIN}, which is missing")

    def test_paths_resolve_into_the_source_root(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(tray.__file__)))
        for path in (tray._ICON_PATH, tray._MAIN):
            self.assertEqual(os.path.dirname(os.path.commonpath([root, path])),
                             os.path.dirname(root))


if __name__ == "__main__":
    unittest.main()
