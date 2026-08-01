"""Self-check of the MPEG-TS stream FluxCast transmits (issue #84)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47

STREAM_TYPES = {
    0x0F: "AAC (ADTS)",
    0x11: "AAC (LATM)",
    0x1B: "H.264",
    0x24: "H.265",
    0x81: "AC-3",
    0x83: "LPCM",
}

H264_PROFILES = {
    66: "Baseline/CBP",
    77: "Main",
    88: "Extended",
    100: "High",
    110: "High 10",
    122: "High 4:2:2",
    244: "High 4:4:4",
}


class _BitReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def bit(self) -> int:
        idx = self._pos >> 3
        if idx >= len(self._data):
            raise ValueError("out of data")
        value = (self._data[idx] >> (7 - (self._pos & 7))) & 1
        self._pos += 1
        return value

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        leading = 0
        while self.bit() == 0:
            leading += 1
            if leading > 32:
                raise ValueError("invalid exp-Golomb code")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.bits(leading)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _unescape_rbsp(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


@dataclass
class SPSInfo:
    profile_idc: int = 0
    constraint_flags: int = 0
    level_idc: int = 0
    width: Optional[int] = None
    height: Optional[int] = None

    @property
    def profile_name(self) -> str:
        name = H264_PROFILES.get(self.profile_idc, f"unknown({self.profile_idc})")
        # constraint_set1_flag on High is the spec's Constrained High Profile.
        if self.profile_idc == 100 and self.constraint_flags & 0x40:
            name = "Constrained High (CHP)"
        elif self.profile_idc == 66 and self.constraint_flags & 0x40:
            name = "Constrained Baseline (CBP)"
        return name


def _parse_sps(rbsp: bytes) -> Optional[SPSInfo]:
    try:
        info = SPSInfo()
        info.profile_idc = rbsp[0]
        info.constraint_flags = rbsp[1]
        info.level_idc = rbsp[2]
        reader = _BitReader(rbsp[3:])
        reader.ue()  # seq_parameter_set_id

        chroma_format_idc = 1
        if info.profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = reader.ue()
            if chroma_format_idc == 3:
                reader.bit()  # separate_colour_plane_flag
            reader.ue()  # bit_depth_luma_minus8
            reader.ue()  # bit_depth_chroma_minus8
            reader.bit()  # qpprime_y_zero_transform_bypass_flag
            if reader.bit():  # seq_scaling_matrix_present_flag
                count = 8 if chroma_format_idc != 3 else 12
                for i in range(count):
                    if reader.bit():
                        size = 16 if i < 6 else 64
                        last_scale = 8
                        next_scale = 8
                        for _ in range(size):
                            if next_scale != 0:
                                next_scale = (last_scale + reader.se() + 256) % 256
                            last_scale = next_scale or last_scale

        reader.ue()  # log2_max_frame_num_minus4
        pic_order_cnt_type = reader.ue()
        if pic_order_cnt_type == 0:
            reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
        elif pic_order_cnt_type == 1:
            reader.bit()
            reader.se()
            reader.se()
            for _ in range(reader.ue()):
                reader.se()

        reader.ue()  # max_num_ref_frames
        reader.bit()  # gaps_in_frame_num_value_allowed_flag
        width_mbs = reader.ue() + 1
        height_map_units = reader.ue() + 1
        frame_mbs_only = reader.bit()
        if not frame_mbs_only:
            reader.bit()  # mb_adaptive_frame_field_flag
        reader.bit()  # direct_8x8_inference_flag

        width = width_mbs * 16
        height = (2 - frame_mbs_only) * height_map_units * 16

        if reader.bit():  # frame_cropping_flag
            left = reader.ue()
            right = reader.ue()
            top = reader.ue()
            bottom = reader.ue()
            sub_w = 1 if chroma_format_idc == 3 else 2
            sub_h = 1 if chroma_format_idc >= 2 else 2
            if chroma_format_idc == 0:
                sub_w = sub_h = 1
            width -= (left + right) * sub_w
            height -= (top + bottom) * sub_h * (2 - frame_mbs_only)

        info.width = width
        info.height = height
        return info
    except (ValueError, IndexError):
        return None


def _starts_new_frame(slice_payload: bytes) -> bool:
    try:
        return _BitReader(_unescape_rbsp(slice_payload[:8])).ue() == 0
    except (ValueError, IndexError):
        return False


def _parse_pps_cabac(rbsp: bytes) -> Optional[bool]:
    try:
        reader = _BitReader(rbsp)
        reader.ue()  # pic_parameter_set_id
        reader.ue()  # seq_parameter_set_id
        return bool(reader.bit())
    except (ValueError, IndexError):
        return None


@dataclass
class TSReport:
    path: str = ""
    size_bytes: int = 0
    packets: int = 0
    sync_errors: int = 0
    pat_pmt_pid: Optional[int] = None
    pmt_pcr_pid: Optional[int] = None
    pmt_streams: list = field(default_factory=list)  # (stream_type, pid)
    pmt_versions: list = field(default_factory=list)  # [(pcr_pid, streams)]
    pid_counts: dict = field(default_factory=dict)
    continuity_errors: dict = field(default_factory=dict)
    pcr_count: int = 0
    pcr_pids: dict = field(default_factory=dict)
    first_packet_pids: list = field(default_factory=list)
    sps_count: int = 0
    pps_count: int = 0
    idr_count: int = 0
    aud_count: int = 0
    video_frames: int = 0
    max_gap_between_sps: int = 0  # in video access units
    sps: Optional[SPSInfo] = None
    cabac: Optional[bool] = None
    duration_s: Optional[float] = None


def _iter_packets(data: bytes):
    for offset in range(0, len(data) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        yield offset, data[offset:offset + TS_PACKET_SIZE]


def _parse_pmt(section: Optional[bytes]):
    """Return (pcr_pid, [(stream_type, pid), ...]) for one PMT section."""
    if not section or len(section) <= 12:
        return None
    length = ((section[1] & 0x0F) << 8) | section[2]
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    info_length = ((section[10] & 0x0F) << 8) | section[11]
    cursor = 12 + info_length
    end = min(3 + length - 4, len(section))
    streams = []
    while cursor + 4 < end:
        stream_type = section[cursor]
        es_pid = ((section[cursor + 1] & 0x1F) << 8) | section[cursor + 2]
        es_info = ((section[cursor + 3] & 0x0F) << 8) | section[cursor + 4]
        streams.append((stream_type, es_pid))
        cursor += 5 + es_info
    return (pcr_pid, tuple(streams))


def _section_payload(packet: bytes) -> Optional[bytes]:
    afc = (packet[3] >> 4) & 0x03
    offset = 4
    if afc in (2, 3):
        offset += 1 + packet[4]
    if not packet[1] & 0x40 or offset >= TS_PACKET_SIZE:
        return None
    offset += 1 + packet[offset]  # pointer_field
    return packet[offset:] if offset < TS_PACKET_SIZE else None


def analyze(path: str, max_bytes: int = 24 * 1024 * 1024) -> TSReport:
    report = TSReport(path=path)
    report.size_bytes = os.path.getsize(path)
    with open(path, "rb") as handle:
        data = handle.read(max_bytes)

    video_pid: Optional[int] = None
    last_cc: dict = {}
    video_payload = bytearray()
    first_pcr: Optional[int] = None
    last_pcr: Optional[int] = None
    aus_since_sps: int = 0

    for offset, packet in _iter_packets(data):
        if packet[0] != SYNC_BYTE:
            report.sync_errors += 1
            continue
        report.packets += 1
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        report.pid_counts[pid] = report.pid_counts.get(pid, 0) + 1
        if offset < TS_PACKET_SIZE * 7:
            report.first_packet_pids.append(pid)

        afc = (packet[3] >> 4) & 0x03
        if afc in (2, 3) and packet[4] > 0 and packet[5] & 0x10:
            report.pcr_count += 1
            report.pcr_pids[pid] = report.pcr_pids.get(pid, 0) + 1
            pcr_base = (
                (packet[6] << 25) | (packet[7] << 17) | (packet[8] << 9)
                | (packet[9] << 1) | (packet[10] >> 7)
            )
            if first_pcr is None:
                first_pcr = pcr_base
            last_pcr = pcr_base

        # cc only increments on payload packets and is undefined for nulls.
        if afc in (1, 3) and pid != 0x1FFF:
            cc = packet[3] & 0x0F
            previous = last_cc.get(pid)
            if previous is not None and cc != (previous + 1) % 16:
                report.continuity_errors[pid] = report.continuity_errors.get(pid, 0) + 1
            last_cc[pid] = cc

        if pid == 0x0000 and report.pat_pmt_pid is None:
            section = _section_payload(packet)
            if section and len(section) > 12:
                length = ((section[1] & 0x0F) << 8) | section[2]
                body = section[8:3 + length - 4]
                if len(body) >= 4:
                    report.pat_pmt_pid = ((body[2] & 0x1F) << 8) | body[3]
        elif pid == report.pat_pmt_pid:
            parsed = _parse_pmt(_section_payload(packet))
            if parsed is not None and parsed not in report.pmt_versions:
                report.pmt_versions.append(parsed)

    for pcr_pid, streams in report.pmt_versions:
        for stream_type, es_pid in streams:
            if stream_type in (0x1B, 0x24) and video_pid is None:
                video_pid = es_pid
    if report.pmt_versions:
        report.pmt_pcr_pid, report.pmt_streams = report.pmt_versions[0]

    if video_pid is not None:
        for _, packet in _iter_packets(data):
            if packet[0] != SYNC_BYTE:
                continue
            if (((packet[1] & 0x1F) << 8) | packet[2]) != video_pid:
                continue
            afc = (packet[3] >> 4) & 0x03
            if afc not in (1, 3):
                continue
            payload_offset = 4
            if afc == 3:
                payload_offset += 1 + packet[4]
            if payload_offset < TS_PACKET_SIZE:
                video_payload += packet[payload_offset:]

    if first_pcr is not None and last_pcr is not None and last_pcr >= first_pcr:
        report.duration_s = (last_pcr - first_pcr) / 90000.0

    stream = bytes(video_payload)
    index = 0
    length = len(stream)
    while True:
        start = stream.find(b"\x00\x00\x01", index)
        if start < 0:
            break
        nal_start = start + 3
        if nal_start >= length:
            break
        nal_type = stream[nal_start] & 0x1F
        next_start = stream.find(b"\x00\x00\x01", nal_start)
        nal_end = next_start if next_start > 0 else length
        payload = stream[nal_start + 1:nal_end]

        if nal_type == 7:
            report.sps_count += 1
            report.max_gap_between_sps = max(report.max_gap_between_sps, aus_since_sps)
            aus_since_sps = 0
            if report.sps is None:
                report.sps = _parse_sps(_unescape_rbsp(payload))
        elif nal_type == 8:
            report.pps_count += 1
            if report.cabac is None:
                report.cabac = _parse_pps_cabac(_unescape_rbsp(payload))
        elif nal_type == 9:
            report.aud_count += 1
        elif nal_type in (1, 5):
            # sliced-threads emits several slice NALs per picture.
            if _starts_new_frame(payload):
                report.video_frames += 1
                aus_since_sps += 1
                if nal_type == 5:
                    report.idr_count += 1

        index = nal_start
    return report


def format_report(report: TSReport) -> str:
    tag = "[FluxCast WFD TS]"
    lines = [f"{tag} ==== transmitted MPEG-TS self-check ===="]
    size_mb = report.size_bytes / (1024 * 1024)
    lines.append(
        f"{tag} dump {report.path} ({size_mb:.1f} MiB, {report.packets} TS packets"
        + (f", {report.duration_s:.1f}s by PCR" if report.duration_s else "")
        + ")"
    )
    if report.sync_errors:
        lines.append(f"{tag} WARNING sync byte errors: {report.sync_errors}")

    pmt = f"0x{report.pat_pmt_pid:04x}" if report.pat_pmt_pid is not None else "MISSING"
    lines.append(f"{tag} PAT -> PMT PID {pmt}   (AOSP/Miracast uses 0x0100)")
    if report.pmt_pcr_pid is not None:
        lines.append(
            f"{tag} PMT: PCR PID 0x{report.pmt_pcr_pid:04x}   (AOSP uses a dedicated 0x1000)"
        )
    for stream_type, pid in report.pmt_streams:
        name = STREAM_TYPES.get(stream_type, f"0x{stream_type:02x}")
        expected = ""
        if stream_type == 0x1B and pid != 0x1011:
            expected = "  (AOSP uses 0x1011)"
        elif stream_type in (0x0F, 0x11) and pid != 0x1100:
            expected = "  (AOSP uses 0x1100)"
        lines.append(f"{tag} PMT: {name} on PID 0x{pid:04x}{expected}")
    if not report.pmt_streams:
        lines.append(f"{tag} WARNING no PMT found -- a sink can never locate the video stream")

    if len(report.pmt_versions) > 1:
        lines.append(
            f"{tag} WARNING the PMT changed {len(report.pmt_versions)} times; a sink that reads "
            "only the first one never sees the streams added later:"
        )
        for index, (pcr_pid, streams) in enumerate(report.pmt_versions):
            described = ", ".join(
                f"{STREAM_TYPES.get(t, hex(t))}@0x{p:04x}" for t, p in streams
            ) or "(no streams)"
            lines.append(f"{tag}   PMT #{index + 1}: PCR 0x{pcr_pid:04x} | {described}")

    if report.pid_counts:
        counts = "  ".join(
            f"0x{pid:04x}:{count}"
            for pid, count in sorted(report.pid_counts.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"{tag} packets per PID: {counts}")

    first = " ".join(f"0x{pid:04x}" for pid in report.first_packet_pids)
    lines.append(f"{tag} first 7 packets (what the sink sees first): {first}")

    lines.append(f"{tag} PCR packets: {report.pcr_count}")
    stray_pcr = {
        pid: count for pid, count in report.pcr_pids.items()
        if report.pmt_pcr_pid is not None and pid != report.pmt_pcr_pid
    }
    if stray_pcr:
        where = "  ".join(f"0x{pid:04x}:{count}" for pid, count in sorted(stray_pcr.items()))
        lines.append(
            f"{tag} WARNING PCR also on PIDs the PMT does not name as PCR_PID: {where}"
        )
    if not report.pcr_count:
        lines.append(f"{tag} WARNING no PCR at all -- the sink has no clock to lock to")
    if report.continuity_errors:
        broken = ", ".join(
            f"0x{pid:04x}:{count}" for pid, count in sorted(report.continuity_errors.items())
        )
        lines.append(f"{tag} WARNING continuity counter errors: {broken}")
    else:
        lines.append(f"{tag} continuity counters: clean")

    if report.sps is not None:
        sps = report.sps
        resolution = (
            f"{sps.width}x{sps.height}" if sps.width and sps.height else "unknown"
        )
        lines.append(
            f"{tag} SPS: profile_idc={sps.profile_idc} ({sps.profile_name}) "
            f"constraint_flags=0x{sps.constraint_flags:02x} "
            f"level_idc={sps.level_idc} ({sps.level_idc / 10:.1f}) {resolution}"
        )
    else:
        lines.append(f"{tag} WARNING no SPS found in the video stream")

    if report.cabac is not None:
        lines.append(
            f"{tag} PPS: entropy coding = {'CABAC' if report.cabac else 'CAVLC'}"
        )

    lines.append(
        f"{tag} NAL counts: SPS={report.sps_count} PPS={report.pps_count} "
        f"IDR={report.idr_count} AUD={report.aud_count} frames={report.video_frames}"
    )
    if report.sps_count and report.max_gap_between_sps:
        lines.append(
            f"{tag} worst gap between SPS repeats: {report.max_gap_between_sps} frames"
        )
    if report.video_frames and not report.idr_count:
        lines.append(f"{tag} WARNING no IDR frame -- a sink joining late can never start")
    if report.sps_count <= 1 and report.video_frames > 1:
        lines.append(
            f"{tag} WARNING SPS/PPS sent only once -- a sink that starts late never gets headers"
        )
    lines.append(f"{tag} ==== end of self-check ====")
    return "\n".join(lines)
