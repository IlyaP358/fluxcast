"""WFD H.264 profile selection: honor sink CHP when advertised."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig, WFDVideoFormat  # noqa: E402
from wfd.constants import WFD_CEA_1080P30  # noqa: E402
from wfd.modes import (  # noqa: E402
    _choose_profile,
    _encoder_h264_profile,
    _selected_video_format,
)


def _sink(profile: str) -> WFDVideoFormat:
    return WFDVideoFormat(
        native="00",
        preferred="01",
        profile=profile,
        level="08",
        cea_mask=WFD_CEA_1080P30,
        vesa_mask=0,
        hh_mask=0,
    )


class ChooseProfileTest(unittest.TestCase):
    def test_cbp_only(self):
        self.assertEqual(_choose_profile("01"), "01")

    def test_chp_only(self):
        self.assertEqual(_choose_profile("02"), "02")

    def test_both_bits_prefers_chp(self):
        self.assertEqual(_choose_profile("03"), "02")

    def test_hotyeah_style_hex(self):
        # Live hotyeah M3: profile byte 02
        self.assertEqual(_choose_profile("02"), "02")

    def test_invalid_falls_back_to_cbp(self):
        self.assertEqual(_choose_profile("zz"), "01")
        self.assertEqual(_choose_profile(""), "01")


class EncoderProfileTest(unittest.TestCase):
    def test_none_sink_baseline(self):
        self.assertEqual(_encoder_h264_profile(None), "baseline")

    def test_chp_sink_maps_to_high(self):
        self.assertEqual(_encoder_h264_profile(_sink("02")), "high")

    def test_cbp_sink_maps_to_baseline(self):
        self.assertEqual(_encoder_h264_profile(_sink("01")), "baseline")

    def test_both_bits_maps_to_high(self):
        self.assertEqual(_encoder_h264_profile(_sink("03")), "high")


class SelectedVideoFormatTest(unittest.TestCase):
    def test_m4_advertises_chp_when_sink_offers_it(self):
        cfg = WFDMediaConfig(
            monitor=None,
            output_resolution="1920x1080",
            fps=30,
        )
        vfmt = _selected_video_format(cfg, _sink("02"))
        tokens = vfmt.split()
        # native preferred profile level ...
        self.assertEqual(tokens[2], "02", vfmt)

    def test_m4_advertises_cbp_for_cbp_only_sink(self):
        cfg = WFDMediaConfig(
            monitor=None, output_resolution="1920x1080", fps=30
        )
        vfmt = _selected_video_format(cfg, _sink("01"))
        self.assertEqual(vfmt.split()[2], "01", vfmt)


if __name__ == "__main__":
    unittest.main()
