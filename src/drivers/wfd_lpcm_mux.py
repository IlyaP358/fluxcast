"""
WFD LPCM MPEG-TS muxer for Microsoft Wireless Display Adapter.

Microsoft requires LPCM audio in MPEG-TS with stream_type=0x83 (WFD/WIDI LPCM)
and a 4-byte WIDI PES header (0xA0 0x06 0x00 0x09) before PCM samples.
GStreamer mpegtsmux hardcodes stream_type=0x8b (Blu-ray LPCM) and cannot
be patched at runtime --- hence this pure-Python muxer.
"""

from __future__ import annotations

import gc
import socket
import struct
import threading
import time
import queue
import logging
from typing import Optional

log = logging.getLogger("FluxCast.WFDLPCMMux")

#MPEG-TS constants

TS_PACKET_SIZE = 188
TS_SYNC = 0x47

PID_PAT  = 0x0000
PID_PMT  = 0x0100
PID_PCR  = 0x1000   # same PID as video
PID_VID  = 0x1000   # H.264
PID_AUD  = 0x1100   # LPCM

STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_LPCM = 0x83   # WFD / WIDI LPCM (NOT 0x8b which is Blu-ray)

PMT_PROG_NUM = 0x0001

# 4-byte WIDI LPCM PES header that precedes raw PCM samples
# Documented in WFD/WIDI specification; Microsoft adapter REQUIRES it.
WIDI_LPCM_HEADER = bytes([0xA0, 0x06, 0x00, 0x09])

# RTP
RTP_PT_MP2T  = 33 # RFC 2250 — MPEG-TS over RTP
RTP_CLOCK_HZ = 90_000 # 90 kHz clock
RTP_MAX_PAYLOAD = 1316 # 7 × 188 bytes

# PES stream IDs
PES_SID_VIDEO = 0xE0
PES_SID_AUDIO = 0xBD

# Audio parameters (fixed for WFD LPCM)
AUDIO_RATE     = 48_000
AUDIO_CHANNELS = 2
AUDIO_BITS     = 16
AUDIO_FRAME_SAMPLES = 1024 # arbitrary chunk size for PES framing



# CRC-32/MPEG helper (SUMMA)
def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = (crc << 1) ^ 0x04C11DB7
            else:
                crc <<= 1
            crc &= 0xFFFFFFFF
    return crc

def _ts_packet(pid: int, payload: bytes, *,
               pusi: bool = False,
               continuity: int = 0,
               adaptation: Optional[bytes] = None) -> bytes:
    """
    Build one 188-byte TS packet.
    If payload+adaptation < 184 bytes it is stuffed with 0xFF.
    """
    flags = 0x40 if pusi else 0x00   # payload_unit_start_indicator

    if adaptation is not None:
        adaptation_control = 0x03    # adaptation + payload
        adapt_field = bytes([len(adaptation)]) + adaptation
        # pad adaptation to fill if needed
        fill_len = 184 - 1 - len(adaptation) - len(payload)
        if fill_len > 0:
            # extend adaptation with stuffing
            adapt_field = bytes([len(adaptation) + fill_len]) + adaptation + bytes(fill_len)
    else:
        adaptation_control = 0x01   # payload only
        adapt_field = b""

    header = bytes([
        TS_SYNC,
        flags | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        (adaptation_control << 4) | (continuity & 0x0F),
    ])

    pkt = header + adapt_field + payload
    # STUFFING
    if len(pkt) < TS_PACKET_SIZE:
        pkt += bytes(TS_PACKET_SIZE - len(pkt))
    assert len(pkt) == TS_PACKET_SIZE, f"TS packet size mismatch: {len(pkt)}"
    return pkt


def _build_pat(continuity: int = 0) -> bytes:
    section = bytearray()
    section += bytes([
        0x00,                     # table_id = PAT
        0xB0, 0x0D,                # section_syntax=1, section_length=13
        0x00, 0x01,                # transport_stream_id = 1
        0xC1,                      # version=0, current_next=1
        0x00,                      # section_number
        0x00,                      # last_section_number
        (PMT_PROG_NUM >> 8) & 0xFF,
        PMT_PROG_NUM & 0xFF,
        0xE0 | ((PID_PMT >> 8) & 0x1F),
        PID_PMT & 0xFF,
    ])
    crc = _crc32_mpeg(bytes(section))
    section += struct.pack(">I", crc)

    payload = bytes([0x00]) + bytes(section)  # pointer_field = 0
    return _ts_packet(PID_PAT, payload, pusi=True, continuity=continuity)

def _build_pmt(continuity: int = 0) -> bytes:
    # Build PMT section body (before CRC)
    streams = bytearray()

    # Video: H.264
    streams += bytes([
        STREAM_TYPE_H264,
        0xE0 | ((PID_VID >> 8) & 0x1F),
        PID_VID & 0xFF,
        0xF0, 0x00, # no ES info descriptors
    ])

    # Audio: WFD LPCM (stream_type=0x83)
    streams += bytes([
        STREAM_TYPE_LPCM,
        0xE0 | ((PID_AUD >> 8) & 0x1F),
        PID_AUD & 0xFF,
        0xF0, 0x00,
    ])

    # PMT header
    section_length = 9 + len(streams) + 4  # 9=fixed header, 4=CRC
    section = bytearray()
    section += bytes([
        0x02,  # table_id = PMT
        0x80 | 0x30 | ((section_length >> 8) & 0x0F),
        section_length & 0xFF,
        (PMT_PROG_NUM >> 8) & 0xFF,
        PMT_PROG_NUM & 0xFF,
        0xC1,                      # version=0, current_next=1
        0x00, 0x00,                # section/last_section
        0xE0 | ((PID_PCR >> 8) & 0x1F),
        PID_PCR & 0xFF,
        0xF0, 0x00,                # no program info descriptors
    ])
    section += streams
    crc = _crc32_mpeg(bytes(section))
    section += struct.pack(">I", crc)

    payload = bytes([0x00]) + bytes(section)
    return _ts_packet(PID_PMT, payload, pusi=True, continuity=continuity)


#PCR adaptation field

def _pcr_adaptation(pcr_90k: int) -> bytes:
    """6-byte PCR adaptation field extension (flags + 6 PCR bytes)."""
    # PCR = base (33 bits) * 300 + ext (9 bits)
    base = pcr_90k
    ext  = 0
    pcr_b = (base << 15) | (0x3F << 9) | ext
    return bytes([
        0x10, # PCR_flag=1, all others 0
        (pcr_b >> 40) & 0xFF,
        (pcr_b >> 32) & 0xFF,
        (pcr_b >> 24) & 0xFF,
        (pcr_b >> 16) & 0xFF,
        (pcr_b >>  8) & 0xFF,
        pcr_b & 0xFF,
    ])


# PES builder 

def _pes_header(stream_id: int, pts_90k: int,
                payload_len: int, *, dts_90k: Optional[int] = None) -> bytes:
    """Build a PES header with PTS (and optional DTS)."""
    has_dts = dts_90k is not None

    def _encode_ts(ts: int) -> bytes:
        ts &= 0x1FFFFFFFF
        b  = ((ts >> 30) & 0x07) << 1
        b1 = (ts >> 22) & 0xFF
        b2 = ((ts >> 15) & 0x7F) << 1
        b3 = (ts >>  7) & 0xFF
        b4 = (ts & 0x7F) << 1
        return bytes([b, b1, b2, b3, b4])

    pts_bytes = _encode_ts(pts_90k)
    pts_flag  = 0xC0 if has_dts else 0x80    # '10' or '11'
    pts_bytes = bytes([pts_flag | pts_bytes[0]]) + pts_bytes[1:]

    optional = pts_bytes
    if has_dts:
        dts_bytes = _encode_ts(dts_90k)
        dts_bytes = bytes([0x10 | dts_bytes[0]]) + dts_bytes[1:]
        optional += dts_bytes

    header_data_len = len(optional)
    pes_packet_len  = 3 + header_data_len + payload_len
    # PES packet length = 0 for unbounded video stream
    if stream_id == PES_SID_VIDEO:
        pes_packet_len = 0

    hdr = struct.pack(">I", 0x00000100 | stream_id)     # start code + stream_id
    hdr += struct.pack(">H", pes_packet_len)
    hdr += bytes([0x80])                                 # flags byte 1: marker bits
    hdr += bytes([0x80 if has_dts else 0x80])            # flags byte 2: PTS_DTS_flags
    # re-encode properly
    flags2 = 0xC0 if has_dts else 0x80
    hdr = hdr[:-1] + bytes([flags2])
    hdr += bytes([header_data_len])
    hdr += optional
    return hdr


def _packetize_pes(pid: int, stream_id: int, pts_90k: int,
                   data: bytes, cc: list,
                   pcr_90k: Optional[int] = None) -> list[bytes]:
    """Split PES into TS packets. cc is a mutable list [counter]."""
    pes_hdr = _pes_header(stream_id, pts_90k, len(data))
    payload  = pes_hdr + data
    packets  = []
    first    = True

    while payload:
        adapt = None
        if first and pcr_90k is not None:
            adapt = _pcr_adaptation(pcr_90k)

        # Maximum payload bytes in this TS packet
        max_payload = TS_PACKET_SIZE - 4  # 184 after header
        if adapt is not None:
            max_payload -= (1 + len(adapt))  # 1 byte for adapt_field_length

        chunk    = payload[:max_payload]
        payload  = payload[max_payload:]
        pkt      = _ts_packet(pid, chunk, pusi=first,
                              continuity=cc[0], adaptation=adapt)
        cc[0]    = (cc[0] + 1) & 0x0F
        packets.append(pkt)
        first = False

    return packets


# RTP framer

class _RTPFramer:
    def __init__(self, ssrc: int = 0xDEADBEEF):
        self._ssrc = ssrc
        self._seq  = 0
        self._start_time = time.monotonic()

    def _rtp_ts(self) -> int:
        elapsed = time.monotonic() - self._start_time
        return int(elapsed * RTP_CLOCK_HZ) & 0xFFFFFFFF

    def frame(self, ts_data: bytes) -> list[bytes]:
        """Split ts_data into RTP packets of ≤RTP_MAX_PAYLOAD bytes."""
        packets = []
        rtp_ts  = self._rtp_ts()
        offset  = 0

        while offset < len(ts_data):
            chunk = ts_data[offset:offset + RTP_MAX_PAYLOAD]
            offset += RTP_MAX_PAYLOAD

            is_last = offset >= len(ts_data)
            hdr = struct.pack(">BBHII",
                0x80,                               # V=2 P=0 X=0 CC=0
                (0x80 if is_last else 0x00) | RTP_PT_MP2T,
                self._seq & 0xFFFF,
                rtp_ts,
                self._ssrc,
            )
            self._seq += 1
            packets.append(hdr + chunk)

        return packets


# ─────────────────────────── Main muxer class ────────────────────────────────
class WFDLPCMMuxer:
    """
    Custom MPEG-TS muxer that produces stream_type=0x83 LPCM audio
    required by the Microsoft Wireless Display Adapter.

    Call start() with GStreamer pipeline descriptions for video and audio.
    Both pipelines must end in appsink named 'sink'.
    """

    def __init__(self, dest_ip: str, dest_port: int):
        self._dest   = (dest_ip, dest_port)
        self._sock   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp    = _RTPFramer()
        self._running = False

        # Continuity counters (mutable lists so helpers can mutate in-place)
        self._cc_pat = [0]
        self._cc_pmt = [0]
        self._cc_vid = [0]
        self._cc_aud = [0]

        self._video_q: queue.Queue = queue.Queue(maxsize=120)
        self._audio_q: queue.Queue = queue.Queue(maxsize=240)

        self._video_pipeline = None
        self._audio_pipeline = None
        self._mux_thread: Optional[threading.Thread] = None

        # Public stats — read from outside for monitoring/tests
        self.frames_sent: int = 0
        self.frames_dropped_video: int = 0
        self.audio_frames_sent: int = 0
        # PCR jitter tracking (ms); updated each frame
        self.pcr_jitter_last_ms: float = 0.0
        self.pcr_jitter_max_ms: float = 0.0

    # === GStreamer pipeline helpers ===========================================

    def _make_video_pipeline(self, pipeline_str: str):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst, GLib
            Gst.init(None)
        except Exception as e:
            raise RuntimeError(f"GStreamer not available: {e}")

        pipe = Gst.parse_launch(pipeline_str)
        sink = pipe.get_by_name("sink")
        if sink is None:
            raise RuntimeError("Video pipeline must contain appsink named 'sink'")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 4)
        sink.set_property("drop", True)

        def _on_sample(appsink):
            sample = appsink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                pts_ns = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0
                try:
                    self._video_q.put_nowait((bytes(mapinfo.data), pts_ns))
                except queue.Full:
                    self.frames_dropped_video += 1
                buf.unmap(mapinfo)
            return Gst.FlowReturn.OK

        sink.connect("new-sample", _on_sample)
        return pipe

    def _make_audio_pipeline(self, pipeline_str: str):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception as e:
            raise RuntimeError(f"GStreamer not available: {e}")

        pipe = Gst.parse_launch(pipeline_str)
        sink = pipe.get_by_name("sink")
        if sink is None:
            raise RuntimeError("Audio pipeline must contain appsink named 'sink'")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 8)
        sink.set_property("drop", False)

        def _on_sample(appsink):
            sample = appsink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                arrival = time.monotonic()
                self._audio_q.put((bytes(mapinfo.data), arrival), block=False)
                buf.unmap(mapinfo)
            return Gst.FlowReturn.OK

        sink.connect("new-sample", _on_sample)
        return pipe

    # === Mux loop =============================================================

    def _ns_to_90k(self, ns: int) -> int:
        return int(ns * RTP_CLOCK_HZ / 1_000_000_000) & 0x1FFFFFFFF

    def _send(self, ts_packets: list[bytes]) -> None:
        raw = b"".join(ts_packets)
        for pkt in self._rtp.frame(raw):
            try:
                self._sock.sendto(pkt, self._dest)
            except OSError as e:
                log.warning("UDP send error: %s", e)

    def _mux_loop(self) -> None:
        log.info("WFDLPCMMuxer mux loop started → %s:%d", *self._dest)

        # Disable GC inside the mux thread to avoid unpredictable pauses mid-packet.
        gc.disable()

        pat_pmt_interval = 30   # PAT+PMT every 30 video frames (~1 s at 30 fps)
        frame_counter    = 0
        wall_start:      Optional[float] = None
        pts_offset_90k:  Optional[int]   = None
        prev_send_time:  float           = time.monotonic()

        try:
            while self._running:
                # Manual gen-0 GC at a safe point between packets.
                if frame_counter % 60 == 0 and frame_counter > 0:
                    gc.collect(0)

                # Wait for next video frame
                try:
                    vid_data, vid_pts_ns = self._video_q.get(timeout=0.1)
                except queue.Empty:
                    continue

                # PCR: wall clock computed RIGHT NOW, epoch = first frame arrival
                now = time.monotonic()
                if wall_start is None:
                    wall_start     = now
                    pts_offset_90k = self._ns_to_90k(vid_pts_ns)
                    prev_send_time = now
                pcr_90k = int((now - wall_start) * RTP_CLOCK_HZ) & 0x1FFFFFFFF

                # PCR jitter: deviation from expected inter-frame interval
                frame_interval_ms = 1000.0 / 30.0   # assume 30 fps
                actual_delta_ms   = (now - prev_send_time) * 1000.0
                jitter_ms         = abs(actual_delta_ms - frame_interval_ms)
                self.pcr_jitter_last_ms = jitter_ms
                if jitter_ms > self.pcr_jitter_max_ms:
                    self.pcr_jitter_max_ms = jitter_ms
                prev_send_time = now


                gst_pts_90k = self._ns_to_90k(vid_pts_ns)
                if pts_offset_90k is None:
                    pts_offset_90k = gst_pts_90k
                pts_90k = (gst_pts_90k - pts_offset_90k) & 0x1FFFFFFFF

                if pts_90k < pcr_90k:
                    pts_90k = (pcr_90k + 9000) & 0x1FFFFFFFF

                ts_out: list[bytes] = []

                # PAT + PMT
                if frame_counter % pat_pmt_interval == 0:
                    ts_out.append(_build_pat(self._cc_pat[0]))
                    self._cc_pat[0] = (self._cc_pat[0] + 1) & 0x0F
                    ts_out.append(_build_pmt(self._cc_pmt[0]))
                    self._cc_pmt[0] = (self._cc_pmt[0] + 1) & 0x0F
                frame_counter += 1

                # Video PES — PCR injected on first TS packet of this frame
                ts_out += _packetize_pes(
                    PID_VID, PES_SID_VIDEO, pts_90k,
                    vid_data, self._cc_vid, pcr_90k=pcr_90k,
                )
                self.frames_sent += 1

                # Drain pending audio
                while not self._audio_q.empty():
                    try:
                        aud_data, aud_arrival = self._audio_q.get_nowait()
                    except queue.Empty:
                        break

                    if aud_arrival < wall_start:
                        # Frame captured before the first video frame — discard
                        self.audio_frames_sent += 1
                        continue
                    aud_elapsed_90k = int((aud_arrival - wall_start) * RTP_CLOCK_HZ)
                    aud_pts_90k = (aud_elapsed_90k + 9000) & 0x1FFFFFFFF
                    aud_payload = WIDI_LPCM_HEADER + aud_data

                    ts_out += _packetize_pes(
                        PID_AUD, PES_SID_AUDIO, aud_pts_90k,
                        aud_payload, self._cc_aud,
                    )
                    self.audio_frames_sent += 1

                self._send(ts_out)

        finally:
            gc.enable()

        log.info("WFDLPCMMuxer mux loop stopped")

    # ── Public API ──

    def start(self, video_pipeline: str, audio_pipeline: str) -> None:
        self._running = True

        try:
            self._video_pipeline = self._make_video_pipeline(video_pipeline)
            self._audio_pipeline = self._make_audio_pipeline(audio_pipeline)
        except Exception as e:
            self._running = False
            raise

        self._mux_thread = threading.Thread(
            target=self._mux_loop, name="WFDLPCMMuxer", daemon=True)
        self._mux_thread.start()

        self._video_pipeline.set_state(
            __import__("gi").repository.Gst.State.PLAYING)
        self._audio_pipeline.set_state(
            __import__("gi").repository.Gst.State.PLAYING)

        log.info("WFDLPCMMuxer started (video + audio pipelines running)")

    def stop(self) -> None:
        self._running = False

        try:
            import gi
            from gi.repository import Gst
            if self._video_pipeline:
                self._video_pipeline.set_state(Gst.State.NULL)
            if self._audio_pipeline:
                self._audio_pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

        if self._mux_thread:
            self._mux_thread.join(timeout=3.0)

        self._sock.close()
        log.info("WFDLPCMMuxer stopped")


# ─────────────────────────── Pipeline string helpers ─────────────────────────

def make_video_pipeline(node_id: int, width: int, height: int,
                        fps_n: int, fps_d: int, bitrate_kbps: int) -> str:
    """
    Build a GStreamer pipeline string for H.264 capture via PipeWire.
    """
    caps = (f"video/x-raw,width={width},height={height},"
            f"framerate={fps_n}/{fps_d}")
    return (
        f"pipewiresrc target-object={node_id} ! "
        f"{caps} ! "
        f"videoconvert ! "
        f"x264enc tune=zerolatency bitrate={bitrate_kbps} speed-preset=ultrafast "
        f"key-int-max={fps_n} ! "
        f"video/x-h264,stream-format=byte-stream,alignment=au ! "
        f"appsink name=sink"
    )


def make_audio_pipeline(node_id: int) -> str:
    """
    Build a GStreamer pipeline string for S16LE 48kHz stereo capture via PipeWire.
    """
    return (
        f"pipewiresrc target-object={node_id} ! "
        f"audioconvert ! "
        f"audioresample ! "
        f"audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved ! "
        f"appsink name=sink"
    )
