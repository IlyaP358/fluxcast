#!/usr/bin/env python3
"""VBV multiplier defaults and env override."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from wfd.encoding import (
    _vbv_bufsize,
    cap_kbits_to_link,
    latency_first_vbv_multiplier,
    link_allows_lpcm,
    parse_iw_station_air_mbps,
    should_honor_idr_request,
)


class VbvBufsizeTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_VBV_MULTIPLIER", None)
        os.environ.pop("FLUXCAST_WFD_AIR_MBPS", None)

    def test_default_latency_first_quarter(self):
        os.environ.pop("FLUXCAST_WFD_VBV_MULTIPLIER", None)
        os.environ.pop("FLUXCAST_WFD_AIR_MBPS", None)
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="hotyeah-8D5117_P2P")),
            "3M",
        )
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="[LG] webOS TV")),
            "3M",
        )
        self.assertEqual(
            _vbv_bufsize("2.4M", SimpleNamespace(peer_name="any")),
            "0.6M",
        )

    def test_env_override(self):
        os.environ["FLUXCAST_WFD_VBV_MULTIPLIER"] = "2.0"
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="any")),
            "24M",
        )
        os.environ["FLUXCAST_WFD_VBV_MULTIPLIER"] = "0.25"
        self.assertEqual(
            _vbv_bufsize("10.8M", SimpleNamespace(peer_name="any")),
            "2.7M",
        )

    def test_invalid_env_falls_back_to_latency_first(self):
        os.environ["FLUXCAST_WFD_VBV_MULTIPLIER"] = "nope"
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="any")),
            "3M",
        )

    def test_air_headroom_widens_when_tight(self):
        os.environ.pop("FLUXCAST_WFD_VBV_MULTIPLIER", None)
        os.environ["FLUXCAST_WFD_AIR_MBPS"] = "3"
        self.assertEqual(
            _vbv_bufsize("2.4M", SimpleNamespace(peer_name="any")),
            "1.2M",
        )
        self.assertEqual(latency_first_vbv_multiplier(2.4, 30.0), 0.25)
        self.assertEqual(latency_first_vbv_multiplier(8.0, 10.0), 0.5)


    def test_cap_and_lpcm_from_air(self):
        self.assertEqual(cap_kbits_to_link(8000, None), 8000)
        self.assertEqual(cap_kbits_to_link(8000, 10.0, audio_kbps=1536), 2964)
        self.assertTrue(link_allows_lpcm(20.0, 2400))
        self.assertFalse(link_allows_lpcm(5.0, 2400))
        self.assertFalse(
            link_allows_lpcm(
                20.0, 2400, go_mhz=5745, radio_can_5ghz=False
            )
        )
        self.assertTrue(
            link_allows_lpcm(
                20.0, 2400, go_mhz=5745, radio_can_5ghz=True
            )
        )

    def test_idr_coalesce_one_per_gop(self):
        self.assertTrue(should_honor_idr_request(0.0, 10.0))
        self.assertTrue(should_honor_idr_request(10.0, 11.0))
        self.assertFalse(should_honor_idr_request(10.0, 10.5))
        dump = (
            "tx packets: 1000\n"
            "tx retries: 100\n"
            "tx bitrate: 26.0 MBit/s MCS 3\n"
            "expected throughput: 20.0Mbps\n"
        )
        air = parse_iw_station_air_mbps(dump)
        self.assertIsNotNone(air)
        self.assertLess(air, 20.0)
        self.assertGreater(air, 15.0)


if __name__ == "__main__":
    unittest.main()
