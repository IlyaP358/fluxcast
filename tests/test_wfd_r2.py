import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig  # noqa: E402
from wfd.constants import WFD_CEA_1080P60  # noqa: E402
from wfd.modes import (  # noqa: E402
    _choose_cea_mode, _parse_sink_r2_video_formats, _parse_sink_video_format,
    _selected_r2_video_format, _use_r2_format,
)
from wfd.rtsp.handler import _WFDRTSPHandler  # noqa: E402
from wfd.rtsp.message import RTSPMessage  # noqa: E402

# wfd2_video_formats from an LG OLED55C21LA (C2): H.264 only, level 5.1, R2 CEA modes.
LG_C2 = (
    "20 01 01 0040 0000000F97FF 0001555575DF 000000000555 00 0000 0000 1F, "
    "01 02 0040 0000000F97FF 0001555575DF 000000000555 00 0000 0000 1F, "
    "01 20 0040 0000000F97FF 0001555575DF 000000000555 00 0000 0000 1F 00"
)
# wfd_video_formats from the same TV: R1 CEA mask has no 1080p60.
LG_C2_R1 = "40 00 01 10 000194FF 155575DF 00000555 00 0000 0000 1F none none"


class WFDR2Test(unittest.TestCase):
    def test_lg_c2_reply_offers_h264_1080p60(self):
        sink = _parse_sink_r2_video_formats(LG_C2)
        self.assertTrue(sink.cea_mask & WFD_CEA_1080P60)
        self.assertEqual(sink.level, "40")
        config = WFDMediaConfig(monitor=None, fps=60, bitrate="8M")
        self.assertEqual(_choose_cea_mode(config, sink).name, "1920x1080p60")

    def test_m4_tuple(self):
        sink = _parse_sink_r2_video_formats(LG_C2)
        config = WFDMediaConfig(monitor=None, fps=60, bitrate="8M")
        self.assertEqual(
            _selected_r2_video_format(config, sink),
            "00 01 01 0010 000000000100 000000000000 000000000000 00 0000 0000 00 00",
        )

    def test_none_is_not_r2(self):
        self.assertIsNone(_parse_sink_r2_video_formats("none"))
        self.assertIsNone(_parse_sink_r2_video_formats(""))

    def test_auto_r2_picks_r2_when_it_unlocks_1080p60(self):
        config = WFDMediaConfig(monitor=None, fps=60, bitrate="8M")
        r1 = _parse_sink_video_format(LG_C2_R1)
        r2 = _parse_sink_r2_video_formats(LG_C2)
        self.assertTrue(_use_r2_format(config, r1, r2))

    def test_auto_r2_keeps_r1_when_mode_is_the_same(self):
        config = WFDMediaConfig(monitor=None, fps=30, bitrate="8M")
        r1 = _parse_sink_video_format(LG_C2_R1)
        r2 = _parse_sink_r2_video_formats(LG_C2)
        self.assertFalse(_use_r2_format(config, r1, r2))

    def test_auto_r2_missing_formats(self):
        config = WFDMediaConfig(monitor=None, fps=60, bitrate="8M")
        r1 = _parse_sink_video_format(LG_C2_R1)
        r2 = _parse_sink_r2_video_formats(LG_C2)
        self.assertTrue(_use_r2_format(config, None, r2))
        self.assertFalse(_use_r2_format(config, r1, None))

    def test_m3_rejected_with_r2_is_retried_as_r1(self):
        handler = _WFDRTSPHandler.__new__(_WFDRTSPHandler)
        handler._write_lock = threading.RLock()
        handler.server = SimpleNamespace(media_config=WFDMediaConfig(monitor=None))
        handler.pending = {"2": "M3_GET_PARAMETER"}
        handler.m3_r2 = True
        handler.m3_sent = True
        handler._send_request = mock.Mock()
        reply = RTSPMessage("RTSP/1.0 451 Parameter Not Understood", {"cseq": "2"}, [])

        handler._handle_response(reply)

        self.assertFalse(handler.m3_r2)
        handler._send_request.assert_called_once()
        body = handler._send_request.call_args.kwargs["body"]
        self.assertIn("wfd_video_formats", body)
        self.assertNotIn("wfd2_video_formats", body)


if __name__ == "__main__":
    unittest.main()
