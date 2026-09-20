"""
WFD LPCM MPEG-TS muxer for Microsoft Wireless Display Adapter.

Microsoft requires LPCM audio in MPEG-TS with stream_type=0x83 (WFD/WIDI LPCM)
and a 4-byte WIDI PES header (0xA0 0x06 0x00 0x09) before PCM samples.
GStreamer mpegtsmux hardcodes stream_type=0x8b (Blu-ray LPCM) and cannot
be patched at runtime --- hence this pure-Python muxer.
"""

from __future__ import annotations

import gc
import logging
import queue
import socket
import struct
import threading
import time
from typing import Optional

log = logging.getLogger("FluxCast.WFDLPCMMux")

#MPEG-TS constants

TS_PACKET_SIZE = 188
TS_SYNC = 0x47

PID_PAT  = 0x0000
PID_PMT  = 0x0100
# WFD PMT lists PCR_PID apart from elementary streams: PCR is the program
# clock reference (adaptation-only packets), video/audio are PES streams.
# Assigned PIDs: PCR 0x1000, H.264 0x1011, LPCM 0x1100.
PID_PCR  = 0x1000   # PCR
PID_VID  = 0x1011   # H.264
PID_AUD  = 0x1100   # LPCM

STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_LPCM = 0x83   # WFD / WIDI LPCM (NOT 0x8b which is Blu-ray)

PMT_PROG_NUM = 0x0001

# Audio parameters (fixed for WFD LPCM)
AUDIO_RATE     = 48_000
AUDIO_CHANNELS = 2
AUDIO_BITS     = 16
LPCM_FRAMES_PER_AU = 80
LPCM_AUS_PER_PES = 6
LPCM_FRAME_BYTES = AUDIO_CHANNELS * (AUDIO_BITS // 8)  # 4
LPCM_PCM_BYTES_PER_PES = LPCM_AUS_PER_PES * LPCM_FRAMES_PER_AU * LPCM_FRAME_BYTES
LPCM_PES_STUFFING = 2

# WFD LPCM sub-stream header (4 bytes) + fixed PCM body.
# 6 AUs × 80 stereo frames × 4 bytes/frame = 1920 bytes PCM.
# Header: 16-bit quantization, 48 kHz, stereo (2ch).
WFD_LPCM_HEADER = bytes([
    0xA0,
    LPCM_AUS_PER_PES,
    0x00,
    (0 << 6) | (2 << 3) | 1,
])

# PMT ES descriptors
AVC_VIDEO_DESCRIPTOR = bytes([
    40, 4,       # tag, length
    0x42,        # profile_idc = Baseline
    0x40,        # constraint_set1 (constrained baseline)
    0x1F,        # level_idc = 3.1
    0x3F,        # AVC_still/24h flags + reserved
])
AVC_TIMING_HRD_DESCRIPTOR = bytes([42, 2, 0x7E, 0x1F])
LPCM_ES_DESCRIPTOR = bytes([
    0x83,
    0x02,
    (2 << 5) | (3 << 1) | 0,  # 48 kHz, reserved, emphasis off
    (1 << 5) | 0x0F,          # stereo + reserved
])

# RTP
RTP_PT_MP2T  = 33 # RFC 2250 — MPEG-TS over RTP
RTP_CLOCK_HZ = 90_000 # 90 kHz clock
RTP_MAX_PAYLOAD = 1316 # 7 × 188 bytes

# PES stream IDs
PES_SID_VIDEO = 0xE0
PES_SID_AUDIO = 0xBD



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
    payload = bytes(payload)

    if adaptation is not None and not payload:
        # STUFFING
        if len(adaptation) > 183:
            raise ValueError("adaptation too large for adaptation-only packet")
        adapt_field = bytes([183]) + adaptation + bytes(183 - len(adaptation))
        adaptation_control = 0x02
    else:
        # Bytes available after the 4-byte TS header.
        free = 184
        adapt_body = bytes(adaptation) if adaptation is not None else b""
        # Space consumed by adaptation = 1 length byte + body (if any).
        if adapt_body or len(payload) < free:
            # How many bytes must the adaptation section occupy?
            # free = adapt_section_len + len(payload); adapt_section_len >= 1
            # when we need any stuffing or have an explicit adaptation body.
            need = free - len(payload)
            if need <= 0 and not adapt_body:
                adapt_field = b""
                adaptation_control = 0x01
            else:
                if need < 1:
                    # Payload alone would overflow; caller must chunk first.
                    raise ValueError("TS payload exceeds 184 bytes")
                # adapt_field_length byte + content (flags/PCR/stuff) = need
                content_len = need - 1
                if len(adapt_body) > content_len:
                    raise ValueError("adaptation body exceeds stuffing budget")
                if adapt_body:
                    content = adapt_body + bytes(
                        [0xFF] * (content_len - len(adapt_body))
                    )
                elif content_len == 0:
                    content = b""
                else:
                    # flags=0 then 0xFF stuffing (ISO 13818-1)
                    content = bytes([0x00]) + bytes([0xFF] * (content_len - 1))
                adapt_field = bytes([content_len]) + content
                adaptation_control = 0x03 if payload else 0x02
        else:
            adapt_field = b""
            adaptation_control = 0x01

    header = bytes([
        TS_SYNC,
        flags | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        (adaptation_control << 4) | (continuity & 0x0F),
    ])
    pkt = header + adapt_field + payload
    if len(pkt) != TS_PACKET_SIZE:
        raise AssertionError(
            f"TS packet size mismatch: {len(pkt)} "
            f"(adapt={len(adapt_field)} payload={len(payload)})"
        )
    return pkt


def _build_pat(continuity: int = 0) -> bytes:
    section = bytearray()
    section += bytes([
        0x00,                     # table_id = PAT
        0xB0, 0x0D,                # section_syntax=1, section_length=13
        0x00, 0x01,                # transport_stream_id = 1
        0xC3,                      # version=1, current_next=1
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
    vid_info = AVC_VIDEO_DESCRIPTOR + AVC_TIMING_HRD_DESCRIPTOR
    streams += bytes([
        STREAM_TYPE_H264,
        0xE0 | ((PID_VID >> 8) & 0x1F),
        PID_VID & 0xFF,
        0xF0 | ((len(vid_info) >> 8) & 0x0F),
        len(vid_info) & 0xFF,
    ])
    streams += vid_info

    # Audio: WFD LPCM (stream_type=0x83)
    aud_info = LPCM_ES_DESCRIPTOR
    streams += bytes([
        STREAM_TYPE_LPCM,
        0xE0 | ((PID_AUD >> 8) & 0x1F),
        PID_AUD & 0xFF,
        0xF0 | ((len(aud_info) >> 8) & 0x0F),
        len(aud_info) & 0xFF,
    ])
    streams += aud_info

    # PMT header
    section_length = 9 + len(streams) + 4  # 9=fixed header, 4=CRC
    section = bytearray()
    section += bytes([
        0x02,  # table_id = PMT
        0x80 | 0x30 | ((section_length >> 8) & 0x0F),
        section_length & 0xFF,
        (PMT_PROG_NUM >> 8) & 0xFF,
        PMT_PROG_NUM & 0xFF,
        0xC3,                      # version=1, current_next=1
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


def _pcr_adaptation(pcr_90k: int) -> bytes:
    """Flags + 6 PCR bytes (no length byte — caller adds adaptation_field_length)."""
    base = pcr_90k & 0x1FFFFFFFF
    ext = 0
    pcr_b = (base << 15) | (0x3F << 9) | ext
    return bytes([
        0x10,  # PCR_flag=1
        (pcr_b >> 40) & 0xFF,
        (pcr_b >> 32) & 0xFF,
        (pcr_b >> 24) & 0xFF,
        (pcr_b >> 16) & 0xFF,
        (pcr_b >> 8) & 0xFF,
        pcr_b & 0xFF,
    ])


def _pcr_packet(pcr_90k: int) -> bytes:
    """Dedicated PCR TS packet on PID 0x1000. Continuity stays 0."""
    return _ts_packet(
        PID_PCR,
        b"",
        pusi=True,
        continuity=0,
        adaptation=_pcr_adaptation(pcr_90k),
    )


def _pes_header(stream_id: int, pts_90k: int,
                payload_len: int, *, dts_90k: Optional[int] = None,
                stuffing: int = 0) -> bytes:
    """Build a PES header with PTS (and optional DTS / stuffing)."""
    has_dts = dts_90k is not None

    def _encode_ts(ts: int) -> bytes:
        ts &= 0x1FFFFFFFF
        b  = ((ts >> 30) & 0x07) << 1 | 0x01
        b1 = (ts >> 22) & 0xFF
        b2 = ((ts >> 15) & 0x7F) << 1 | 0x01
        b3 = (ts >>  7) & 0xFF
        b4 = (ts & 0x7F) << 1 | 0x01
        return bytes([b, b1, b2, b3, b4])

    pts_bytes = _encode_ts(pts_90k)
    # Marker nibble: 0x20 = '0010' for PTS-only, 0x30 = '0011' for PTS+DTS.
    pts_flag  = 0x30 if has_dts else 0x20
    pts_bytes = bytes([pts_flag | pts_bytes[0]]) + pts_bytes[1:]

    optional = pts_bytes
    if has_dts:
        dts_bytes = _encode_ts(dts_90k)
        dts_bytes = bytes([0x10 | dts_bytes[0]]) + dts_bytes[1:]
        optional += dts_bytes
    if stuffing:
        optional += bytes([0xFF] * stuffing)

    header_data_len = len(optional)
    pes_packet_len  = 3 + header_data_len + payload_len
    # PES packet length = 0 for unbounded video stream
    if stream_id == PES_SID_VIDEO:
        pes_packet_len = 0

    flags2 = 0xC0 if has_dts else 0x80
    # flags1: marker '10' + data_alignment_indicator=1
    flags1 = 0x84
    hdr = struct.pack(">I", 0x00000100 | stream_id)
    hdr += struct.pack(">H", pes_packet_len)
    hdr += bytes([flags1, flags2, header_data_len])
    hdr += optional
    return hdr


def _packetize_pes(pid: int, stream_id: int, pts_90k: int,
                   data: bytes, cc: list, *,
                   stuffing: int = 0) -> list[bytes]:
    """Split PES into TS packets. cc is a mutable list [counter]."""
    pes_hdr = _pes_header(stream_id, pts_90k, len(data), stuffing=stuffing)
    payload  = pes_hdr + data
    packets  = []
    first    = True

    while payload:
        max_payload = TS_PACKET_SIZE - 4  # 184 after header
        chunk    = payload[:max_payload]
        payload  = payload[max_payload:]
        pkt      = _ts_packet(pid, chunk, pusi=first, continuity=cc[0])
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


class LPCMAudioPacker:
    """Accumulate S16BE PCM into fixed WFD LPCM PES payloads.

    Emits (payload_bytes, start_frame_index) when a full 6×80-frame body is
    ready. PTS is derived from the sample index (not wall-clock arrival) so
    the sink does not hear stretch/chop from capture jitter.
    """

    __slots__ = ("_buf", "_frame_index")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._frame_index = 0  # stereo frames consumed at start of _buf

    def reset(self) -> None:
        self._buf.clear()
        self._frame_index = 0

    def feed(self, pcm: bytes) -> list[tuple[bytes, int]]:
        out: list[tuple[bytes, int]] = []
        if not pcm:
            return out
        # Keep stereo s16 frame alignment (4 bytes).
        odd = len(pcm) % LPCM_FRAME_BYTES
        if odd:
            pcm = pcm[: len(pcm) - odd]
        if not pcm:
            return out
        self._buf.extend(pcm)
        frames_per_pes = LPCM_PCM_BYTES_PER_PES // LPCM_FRAME_BYTES
        while len(self._buf) >= LPCM_PCM_BYTES_PER_PES:
            body = bytes(self._buf[:LPCM_PCM_BYTES_PER_PES])
            del self._buf[:LPCM_PCM_BYTES_PER_PES]
            payload = WFD_LPCM_HEADER + body
            out.append((payload, self._frame_index))
            self._frame_index += frames_per_pes
        return out


# ─────────────────────────── Main muxer class ────────────────────────────────
class WFDLPCMMuxer:
    """
    Custom MPEG-TS muxer that produces stream_type=0x83 LPCM audio
    required by the Microsoft Wireless Display Adapter.

    Call start() with GStreamer pipeline descriptions for video and audio.
    Both pipelines must end in appsink named 'sink'.
    """

    def __init__(
        self,
        dest_ip: str,
        dest_port: int,
        local_ip: str | None = None,
        local_port: int | None = None,
        bind_iface: str | None = None,
    ):
        self._dest   = (dest_ip, dest_port)
        self._bind_ip = local_ip
        self._bind_port = int(local_port) if local_port else None
        self._bind_iface = bind_iface
        self._sock   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Force RTP onto the P2P iface. Without this, after rebind packets can
        # leave via STA wifi while Sender health still counts frames_sent —
        # TV stays dark and p2p tx+ goes flat.
        if self._bind_iface:
            try:
                self._sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self._bind_iface.encode("utf-8") + b"\0",
                )
            except OSError as exc:
                log.warning(
                    "SO_BINDTODEVICE(%s) failed (need CAP_NET_RAW?): %s",
                    self._bind_iface,
                    exc,
                )
        # WFD RTSP advertises a fixed client_rtp_ports source port; sinks often
        # drop RTP that does not come from that port.
        if self._bind_port:
            try:
                self._sock.bind((self._bind_ip or "0.0.0.0", self._bind_port))
            except OSError as exc:
                self._sock.close()
                raise OSError(
                    f"LPCM muxer could not bind RTP source port {self._bind_port}: {exc}"
                ) from exc
        self._rtp    = _RTPFramer()
        self._running = False
        self._audio_packer = LPCMAudioPacker()
        self.udp_send_failures: int = 0

        # Continuity counters (mutable lists so helpers can mutate in-place)
        self._cc_pat = [0]
        self._cc_pmt = [0]
        self._cc_vid = [0]
        self._cc_aud = [0]

        # Video: maxsize=2; Full → drop-oldest in _on_new_video_sample.
        self._video_q: queue.Queue = queue.Queue(maxsize=2)
        # Audio: maxsize=2000; Full → drop chunk in _on_sample (put_nowait).
        self._audio_q: queue.Queue = queue.Queue(maxsize=2000)

        self._video_pipeline = None
        self._audio_pipeline = None
        self._mux_thread: Optional[threading.Thread] = None

        # Public stats — read from outside for monitoring/tests
        self.frames_sent: int = 0
        self.frames_dropped_video: int = 0
        self.audio_frames_sent: int = 0
        self.audio_chunks_dropped: int = 0
        # PCR jitter tracking (ms); updated each frame
        self.pcr_jitter_last_ms: float = 0.0
        self.pcr_jitter_max_ms: float = 0.0
        self._last_psi_mono: float = 0.0

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
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)

        def _on_sample(appsink):
            sample = appsink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                pts_ns = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0
                frame = (bytes(mapinfo.data), pts_ns)
                try:
                    self._video_q.put_nowait(frame)
                except queue.Full:
                    # Drop oldest, then enqueue the new frame.
                    try:
                        self._video_q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._video_q.put_nowait(frame)
                    except queue.Full:
                        pass
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
        sink.set_property("max-buffers", 32)
        sink.set_property("drop", False)

        def _on_sample(appsink):
            sample = appsink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                try:
                    self._audio_q.put_nowait(bytes(mapinfo.data))
                except queue.Full:
                    self.audio_chunks_dropped += 1
                buf.unmap(mapinfo)
            return Gst.FlowReturn.OK

        sink.connect("new-sample", _on_sample)
        return pipe

    # === Mux loop =============================================================

    def _ns_to_90k(self, ns: int) -> int:
        return int(ns * RTP_CLOCK_HZ / 1_000_000_000) & 0x1FFFFFFFF

    def _revive_socket(self) -> None:
        """Recreate the RTP UDP socket after EBADF / close races during rebind."""
        import errno as _errno

        old = self._sock
        self._sock = None
        try:
            if old is not None:
                old.close()
        except OSError:
            pass
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        iface = getattr(self, "_bind_iface", None)
        if iface:
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    iface.encode("utf-8") + b"\0",
                )
            except OSError as exc:
                log.warning("SO_BINDTODEVICE revive(%s) failed: %s", iface, exc)
        local_port = getattr(self, "_bind_port", None)
        local_ip = getattr(self, "_bind_ip", None)
        if local_port:
            try:
                sock.bind((local_ip or "0.0.0.0", int(local_port)))
            except OSError as exc:
                sock.close()
                log.error("LPCM muxer socket revive bind failed: %s", exc)
                return
        self._sock = sock
        log.warning("LPCM muxer RTP socket revived after send failure")

    def _send(self, ts_packets: list[bytes]) -> bool:
        """Send RTP. Returns False if the UDP socket is dead (caller should not
        count the frame as delivered)."""
        if not ts_packets:
            return True
        if self._sock is None:
            return False
        raw = b"".join(ts_packets)
        ok = True
        for pkt in self._rtp.frame(raw):
            try:
                self._sock.sendto(pkt, self._dest)
            except OSError as e:
                log.warning("UDP send error: %s", e)
                ok = False
                # EBADF: stop() closed under us, or fd recycled mid-rebind.
                if getattr(e, "errno", None) in (9, 1009):  # EBADF
                    self._revive_socket()
                break
        return ok

    def _drain_audio_packets(self) -> list[bytes]:
        """Return TS packets for any complete LPCM access units."""
        out: list[bytes] = []
        while not self._audio_q.empty():
            try:
                aud_data = self._audio_q.get_nowait()
            except queue.Empty:
                break
            for aud_payload, frame_index in self._audio_packer.feed(aud_data):
                # Sample-clock PTS.
                aud_pts_90k = (
                    (frame_index * RTP_CLOCK_HZ) // AUDIO_RATE + 9000
                ) & 0x1FFFFFFFF
                out += _packetize_pes(
                    PID_AUD, PES_SID_AUDIO, aud_pts_90k,
                    aud_payload, self._cc_aud,
                    stuffing=LPCM_PES_STUFFING,
                )
                self.audio_frames_sent += 1
        return out

    def _maybe_psi(self, now: float, wall_start: float, force: bool = False) -> list[bytes]:
        """PAT/PMT/PCR about every 100 ms (wall clock), not only on video frames."""
        out: list[bytes] = []
        if not force and (now - self._last_psi_mono) < 0.1:
            return out
        self._last_psi_mono = now
        pcr_90k = int((now - wall_start) * RTP_CLOCK_HZ) & 0x1FFFFFFFF
        out.append(_build_pat(self._cc_pat[0]))
        self._cc_pat[0] = (self._cc_pat[0] + 1) & 0x0F
        out.append(_build_pmt(self._cc_pmt[0]))
        self._cc_pmt[0] = (self._cc_pmt[0] + 1) & 0x0F
        out.append(_pcr_packet(pcr_90k))
        return out

    def _mux_loop(self) -> None:
        log.info("WFDLPCMMuxer mux loop started → %s:%d", *self._dest)

        # Disable GC inside the mux thread to avoid unpredictable pauses mid-packet.
        gc.disable()

        frame_counter = 0
        wall_start: Optional[float] = None
        pts_offset_90k: Optional[int] = None
        prev_send_time: float = time.monotonic()

        try:
            while self._running:
                # Manual gen-0 GC at a safe point between packets.
                if frame_counter % 60 == 0 and frame_counter > 0:
                    gc.collect(0)

                # Wait for next video frame
                have_video = False
                vid_data = b""
                vid_pts_ns = 0
                try:
                    vid_data, vid_pts_ns = self._video_q.get(timeout=0.02)
                    have_video = True
                except queue.Empty:
                    pass

                # PCR: wall clock computed RIGHT NOW, epoch = first frame arrival
                now = time.monotonic()
                if wall_start is None:
                    if not have_video:
                        # Frame captured before the first video frame — discard
                        try:
                            while True:
                                self._audio_q.get_nowait()
                        except queue.Empty:
                            pass
                        continue
                    wall_start = now
                    pts_offset_90k = self._ns_to_90k(vid_pts_ns)
                    prev_send_time = now
                    self._last_psi_mono = 0.0

                pcr_90k = int((now - wall_start) * RTP_CLOCK_HZ) & 0x1FFFFFFFF
                if have_video:
                    # PCR jitter: deviation from expected inter-frame interval
                    frame_interval_ms = 1000.0 / 30.0   # assume 30 fps
                    actual_delta_ms = (now - prev_send_time) * 1000.0
                    jitter_ms = abs(actual_delta_ms - frame_interval_ms)
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
                    ts_out += self._maybe_psi(now, wall_start)

                    # Video PES
                    ts_out += _packetize_pes(
                        PID_VID, PES_SID_VIDEO, pts_90k,
                        vid_data, self._cc_vid,
                    )
                    frame_counter += 1
                    if self._send(ts_out):
                        self.frames_sent += 1
                    else:
                        self.udp_send_failures += 1
                else:
                    # PAT + PMT
                    psi = self._maybe_psi(now, wall_start)
                    if psi:
                        if not self._send(psi):
                            self.udp_send_failures += 1

                # Drain pending audio
                aud_out = self._drain_audio_packets()
                if aud_out:
                    if not self._send(aud_out):
                        self.udp_send_failures += 1

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
            if self._mux_thread.is_alive():
                log.error(
                    "WFDLPCMMuxer mux thread still alive after join; "
                    "closing socket to unblock"
                )

        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
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
    Build a GStreamer pipeline string for S16BE 48kHz stereo capture via PipeWire.
    """
    return (
        f"pipewiresrc target-object={node_id} ! "
        f"audioconvert ! "
        f"audioresample ! "
        f"audio/x-raw,format=S16BE,rate=48000,channels=2,layout=interleaved ! "
        f"appsink name=sink"
    )
