"""WFD LPCM framing — Wi-Fi Display contract (AOSP-inspired, Linux muxer)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from drivers.wfd_lpcm_mux import (  # noqa: E402
    AUDIO_AU_TICKS,
    AVC_VIDEO_DESCRIPTOR,
    LPCM_ES_DESCRIPTOR,
    LPCM_FRAME_BYTES,
    LPCM_PCM_BYTES_PER_PES,
    LPCM_SAMPLES_PER_AU,
    LPCMAudioPacker,
    PID_VID,
    PROGRAM_LEAD_90K,
    STREAM_TYPE_LPCM,
    WFD_LPCM_HEADER,
    WFDLPCMMuxer,
    _build_pmt,
    _pcr_packet,
    ns_to_90k,
    quantize_video_delta,
    video_frame_ticks,
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

    def test_packer_skip_frames_advances_sample_clock(self):
        packer = LPCMAudioPacker()
        packer.skip_frames(480)
        out = packer.feed(bytes(LPCM_PCM_BYTES_PER_PES))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], 480)

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
        # Continuity counter is the low nibble and must advance per packet.
        self.assertEqual(pkt[3] & 0x0F, 0)
        nxt = _pcr_packet(180_000, continuity=1)
        self.assertEqual(nxt[3] & 0x0F, 1)


class TestProgramClock(unittest.TestCase):
    def test_ns_to_90k_is_exact(self):
        self.assertEqual(ns_to_90k(1_000_000_000), 90_000)
        self.assertEqual(ns_to_90k(16_666_667), 1500)
        self.assertEqual(video_frame_ticks(60), 1500)

    def test_video_delta_snaps_to_frame_grid(self):
        self.assertEqual(quantize_video_delta(0, 60), 0)
        self.assertEqual(quantize_video_delta(200, 60), 0)
        self.assertEqual(quantize_video_delta(1500, 60), 1500)
        self.assertEqual(quantize_video_delta(1600, 60), 1500)
        self.assertEqual(quantize_video_delta(3000, 60), 3000)

    def _muxer(self):
        return WFDLPCMMuxer("127.0.0.1", 9, fps=60)

    def test_video_pts_advances_one_frame_even_when_capture_bunches(self):
        m = self._muxer()
        m._pcr_90k = 50_000
        first = m._video_pts_for_ns(0)
        # Same millisecond, then many seconds later. Both are real pictures.
        second = m._video_pts_for_ns(1_000)
        third = m._video_pts_for_ns(5_000_000_000)
        self.assertEqual(first, PROGRAM_LEAD_90K)
        self.assertEqual(second, first + 1500)
        self.assertEqual(third, second + 1500)
        self.assertEqual(m.video_frames_dropped_late, 0)

    def test_audio_pts_steps_one_access_unit(self):
        m = self._muxer()
        m._pcr_90k = 0
        first = m._take_audio_pts(0)
        second = m._take_audio_pts(LPCM_SAMPLES_PER_AU)
        self.assertEqual(first, PROGRAM_LEAD_90K)
        self.assertEqual(second - first, AUDIO_AU_TICKS)
        self.assertFalse(m._audio_discontinuity)

    def test_late_audio_au_is_discarded_with_discontinuity(self):
        m = self._muxer()
        m._pcr_90k = 200_000
        m._audio_pts_origin = 0
        m._audio_au_base = 0
        pts = m._take_audio_pts(0)
        self.assertIsNone(pts)
        self.assertTrue(m._audio_discontinuity)
        self.assertEqual(m.audio_aus_discarded, 1)

    def test_discarded_chunk_sets_discontinuity(self):
        import queue
        m = self._muxer()
        m._audio_q = queue.Queue(maxsize=1)
        m._enqueue_audio(b"\x00" * LPCM_FRAME_BYTES)
        m._enqueue_audio(b"\x11" * LPCM_FRAME_BYTES)
        self.assertTrue(m._audio_discontinuity)

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
