import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig  # noqa: E402
from wfd.constants import (  # noqa: E402
    WFD_CEA_720P30, WFD_CEA_720P60, WFD_CEA_1080P30, WFD_CEA_1080P60, WFD_LEVEL_42,
)
from wfd.modes import _choose_cea_mode, _parse_sink_video_format  # noqa: E402

# A typical TV sink: advertises 720p and 1080p at 30/60 fps, H.264 level 4.2,
# and no VESA or handheld modes.
CEA_MODES = WFD_CEA_720P30 | WFD_CEA_720P60 | WFD_CEA_1080P30 | WFD_CEA_1080P60


def _sink(cea_modes=CEA_MODES, max_h="none", max_v="none"):
    return _parse_sink_video_format(
        # native preferred profile level cea vesa hh latency min-slice slice-enc fr-ctrl max-h max-v
        f"00 00 02 {WFD_LEVEL_42:02x} {cea_modes:08x} 00000000 00000000 00 0000 0000 00 {max_h} {max_v}"
    )


SINK = _sink()


class UnknownSourceSizeModeTest(unittest.TestCase):
    def test_unknown_size_gets_1080p(self):
        config = WFDMediaConfig(monitor=None, fps=30, bitrate="4M")
        self.assertEqual(_choose_cea_mode(config, SINK).resolution, "1920x1080")

    def test_test_pattern_keeps_720p(self):
        config = WFDMediaConfig(monitor=None, fps=30, bitrate="4M", test_pattern=True)
        self.assertEqual(_choose_cea_mode(config, SINK).resolution, "1280x720")


class SinkMaxResolutionTest(unittest.TestCase):
    def test_max_fields_are_parsed_as_hex(self):
        self.assertEqual((SINK.max_hres, SINK.max_vres), (None, None))
        sink = _sink(max_h="0500", max_v="02d0")
        self.assertEqual((sink.max_hres, sink.max_vres), (1280, 720))

    def test_max_resolution_caps_an_advertised_mode(self):
        config = WFDMediaConfig(monitor=None, fps=30, bitrate="4M")
        sink = _sink(max_h="0500", max_v="02d0")
        self.assertEqual(_choose_cea_mode(config, sink).resolution, "1280x720")

    def test_max_resolution_caps_a_forced_mode(self):
        config = WFDMediaConfig(monitor=None, fps=30, bitrate="4M")
        sink = _sink(cea_modes=0, max_h="0500", max_v="02d0")
        self.assertEqual(_choose_cea_mode(config, sink).resolution, "1280x720")


if __name__ == "__main__":
    unittest.main()
