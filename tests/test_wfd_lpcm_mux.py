"""WFD LPCM framing — Wi-Fi Display contract (AOSP-inspired, Linux muxer)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from drivers.wfd_lpcm_mux import (  # noqa: E402
    AVC_VIDEO_DESCRIPTOR,
    LPCM_ES_DESCRIPTOR,
    LPCM_PCM_BYTES_PER_PES,
    LPCMAudioPacker,
    PID_VID,
    STREAM_TYPE_LPCM,
    WFD_LPCM_HEADER,
    _build_pmt,
    _pcr_packet,
)


class TestWFDLPCMFraming(unittest.TestCase):
    def test_wfd_lpcm_header_48k_stereo_16bit(self):
        self.assertEqual(WFD_LPCM_HEADER, bytes([0xA0, 0x06, 0x00, 0x11]))
        self.assertEqual(LPCM_PCM_BYTES_PER_PES, 1920)

    def test_packer_emits_only_full_access_units(self):
        packer = LPCMAudioPacker()
        self.assertEqual(packer.feed(bytes(960)), [])
        out = packer.feed(bytes(960))
        self.assertEqual(len(out), 1)
        payload, frame_index = out[0]
        self.assertEqual(frame_index, 0)
        self.assertEqual(payload[:4], WFD_LPCM_HEADER)
        self.assertEqual(len(payload), 4 + LPCM_PCM_BYTES_PER_PES)

    def test_packer_multiple_aus_sample_index(self):
        packer = LPCMAudioPacker()
        out = packer.feed(bytes(LPCM_PCM_BYTES_PER_PES * 2 + 100))
        self.assertEqual(len(out), 2)
        self.assertTrue(all(x[0][:4] == WFD_LPCM_HEADER for x in out))
        frames_per = LPCM_PCM_BYTES_PER_PES // 4
        self.assertEqual(out[0][1], 0)
        self.assertEqual(out[1][1], frames_per)
        self.assertTrue(packer.feed(bytes(LPCM_PCM_BYTES_PER_PES - 100)))
        self.assertEqual(packer.feed(b""), [])

    def test_pmt_matches_aosp_layout(self):
        pmt = _build_pmt(0)
        self.assertIn(bytes([STREAM_TYPE_LPCM]), pmt)
        self.assertIn(LPCM_ES_DESCRIPTOR, pmt)
        self.assertIn(AVC_VIDEO_DESCRIPTOR, pmt)
        # Video elementary PID 0x1011 (AOSP), not 0x1000.
        self.assertEqual(PID_VID, 0x1011)
        self.assertIn(bytes([0xE0 | ((PID_VID >> 8) & 0x1F), PID_VID & 0xFF]), pmt)

    def test_pcr_packet_is_adaptation_only(self):
        pkt = _pcr_packet(90_000)
        self.assertEqual(len(pkt), 188)
        self.assertEqual(pkt[0], 0x47)
        # PID 0x1000
        self.assertEqual(((pkt[1] & 0x1F) << 8) | pkt[2], 0x1000)
        # adaptation_field_control == 0b10 (adaptation only)
        self.assertEqual((pkt[3] >> 4) & 0x3, 0x2)

    def test_short_ts_packet_uses_adaptation_stuffing(self):
        from drivers.wfd_lpcm_mux import _ts_packet

        pkt = _ts_packet(0x1011, b"\x00\x00\x01\xe0" + b"\x11" * 20, pusi=True)
        self.assertEqual(len(pkt), 188)
        afc = (pkt[3] >> 4) & 0x3
        self.assertEqual(afc, 0x3)  # adaptation + payload
        adapt_len = pkt[4]
        # Stuffing after adaptation must be 0xFF, not mistaken PES payload.
        self.assertTrue(all(b == 0xFF for b in pkt[5 + 1 : 5 + adapt_len]))


if __name__ == "__main__":
    unittest.main()
