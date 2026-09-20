#!/usr/bin/env python3
"""VBV multiplier defaults and env override."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from wfd.encoding import _vbv_bufsize


class VbvBufsizeTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_VBV_MULTIPLIER", None)

    def test_default_half_for_all_peers(self):
        os.environ.pop("FLUXCAST_WFD_VBV_MULTIPLIER", None)
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="hotyeah-8D5117_P2P")),
            "6M",
        )
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="[LG] webOS TV")),
            "6M",
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

    def test_invalid_env_falls_back_to_half(self):
        os.environ["FLUXCAST_WFD_VBV_MULTIPLIER"] = "nope"
        self.assertEqual(
            _vbv_bufsize("12M", SimpleNamespace(peer_name="any")),
            "6M",
        )


if __name__ == "__main__":
    unittest.main()
