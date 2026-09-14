"""VAAPI rate-control -p args for wf-recorder h264_vaapi."""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class VaapiRcParamsTest(unittest.TestCase):
    def tearDown(self):
        for k in (
            "FLUXCAST_WFD_VAAPI_RC",
            "FLUXCAST_WFD_VAAPI_QP",
            "FLUXCAST_WFD_VAAPI_BITRATE",
            "FLUXCAST_WFD_VAAPI_GOP",
            "FLUXCAST_WFD_VAAPI_QUALITY",
        ):
            os.environ.pop(k, None)

    def _harness(self, fps: int = 30):
        from wfd.media.wlroots import WlrootsMixin

        class H(WlrootsMixin):
            def __init__(self):
                self.config = SimpleNamespace(fps=fps, bitrate="12M")

        return H()

    def test_cqp_default(self):
        h = self._harness()
        params, desc = h._vaapi_rc_wf_params({"gop": 30, "effective_bitrate": "12M"})
        self.assertIn("rc_mode=CQP", params)
        self.assertIn("qp=18", params)
        self.assertIn("gop_size=30", params)
        self.assertIn("CQP", desc)

    def test_cbr_sets_b_before_rc_mode(self):
        os.environ["FLUXCAST_WFD_VAAPI_RC"] = "CBR"
        os.environ["FLUXCAST_WFD_VAAPI_BITRATE"] = "28M"
        os.environ["FLUXCAST_WFD_VAAPI_GOP"] = "60"
        h = self._harness()
        params, desc = h._vaapi_rc_wf_params({"gop": 30, "effective_bitrate": "12M"})
        self.assertIn("b=28000000", params)
        self.assertIn("rc_mode=CBR", params)
        self.assertLess(params.index("b=28000000"), params.index("rc_mode=CBR"))
        self.assertIn("gop_size=60", params)
        self.assertNotIn("qp=", " ".join(params))
        self.assertIn("CBR", desc)

    def test_quality_override(self):
        os.environ["FLUXCAST_WFD_VAAPI_QUALITY"] = "2"
        h = self._harness()
        params, _desc = h._vaapi_rc_wf_params({"gop": 30, "effective_bitrate": "12M"})
        self.assertIn("quality=2", params)


if __name__ == "__main__":
    unittest.main()
