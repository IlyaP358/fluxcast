import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tray_config  # noqa: E402


class TrayConfigTest(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self._temp_dir.name) / "config"

    def tearDown(self):
        self._temp_dir.cleanup()

    def _write(self, contents):
        self.config_path.write_text(contents, encoding="utf-8")

    def test_missing_and_empty_config_preserve_defaults(self):
        self.assertEqual(
            tray_config.load_profile("wfd", config_path=self.config_path),
            [],
        )
        self._write("\n")
        self.assertEqual(
            tray_config.load_profile("dlna", config_path=self.config_path),
            [],
        )

    def test_loads_only_selected_mode(self):
        self._write(
            """
[wfd]
output-res = 1920X1080
fps = 60
bitrate = 8M
wfd-no-audio = true
wfd-no-firewall = false

[dlna]
fps = invalid-in-unselected-section
"""
        )
        warnings = []

        args = tray_config.load_profile(
            "wfd", config_path=self.config_path, warn=warnings.append
        )

        self.assertEqual(
            args,
            [
                "--output-res", "1920x1080",
                "--fps", "60",
                "--bitrate", "8M",
                "--wfd-no-audio",
            ],
        )
        self.assertEqual(warnings, [])

    def test_does_not_inherit_options_from_default_section(self):
        self._write(
            """
[DEFAULT]
fps = 60

[wfd]
bitrate = 8M
"""
        )

        self.assertEqual(
            tray_config.load_profile("wfd", config_path=self.config_path),
            ["--bitrate", "8M"],
        )

    def test_loads_dlna_stream_options(self):
        self._write(
            """
[dlna]
transport = HLS
capture-backend = wf-recorder
host = 192.168.1.20
port = 9090
discover-timeout = 10
"""
        )

        self.assertEqual(
            tray_config.load_profile("dlna", config_path=self.config_path),
            [
                "--transport", "hls",
                "--capture-backend", "wf-recorder",
                "--host", "192.168.1.20",
                "--port", "9090",
                "--discover-timeout", "10",
            ],
        )

    def test_invalid_and_unknown_options_warn_and_fall_back(self):
        self._write(
            """
[wfd]
output-res = full-hd
fps = 0
bitrate = fast
wfd-rtsp-port = 70000
wfd-no-audio = sometimes
wfd-peer = living-room-tv
"""
        )
        warnings = []

        args = tray_config.load_profile(
            "wfd", config_path=self.config_path, warn=warnings.append
        )

        self.assertEqual(args, [])
        self.assertEqual(len(warnings), 6)
        self.assertTrue(any("unknown option 'wfd-peer'" in item for item in warnings))
        self.assertTrue(any("invalid value for 'fps'" in item for item in warnings))

    def test_malformed_config_warns_and_preserves_defaults(self):
        self._write("fps = 60\n")
        warnings = []

        args = tray_config.load_profile(
            "cast", config_path=self.config_path, warn=warnings.append
        )

        self.assertEqual(args, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not parse", warnings[0])

    def test_uses_xdg_config_home(self):
        with mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": "/tmp/fluxcast-test-config"}
        ):
            self.assertEqual(
                tray_config.get_config_path(),
                Path("/tmp/fluxcast-test-config/fluxcast/config"),
            )


if __name__ == "__main__":
    unittest.main()
