import re
import random
import socket
import socketserver
import shutil
import subprocess
import threading
import time
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Optional

from diagnostics import print_report, run_diagnostics
from portal_capture import PortalCaptureError, PortalCaptureSession, close_portal_capture, start_portal_capture


WFD_RTSP_PORT = 7236

# WFD CEA resolution bitmask. The current backend intentionally negotiates
# only the !common! HD modes that Samsung TVs usually accept reliably.
WFD_CEA_720P30 = 0x00000020   # bit 5: 1280x720p30, mandatory HD mode
WFD_CEA_720P60 = 0x00000040   # bit 6: 1280x720p60
WFD_CEA_1080P30 = 0x00000080  # bit 7: 1920x1080p30
WFD_CEA_1080P60 = 0x00000100  # bit 8: 1920x1080p60
WFD_LEVEL_31 = 0x01
WFD_LEVEL_32 = 0x02
WFD_LEVEL_40 = 0x04
WFD_LEVEL_42 = 0x10
WFD_AUDIO_AAC = "AAC 00000001 00"
NM_DEST = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"


def _wfd_ie_device_info(rtsp_port: int) -> bytes:
    """
    WFD Subelement ID 0: WFD Device Information (6 bytes)
    Byte 0-1: Device Information bitmask
              (0x0010 = Source, 0x0000 = Coupled Sink not supported)
    Byte 2-3: Session Management Control Port (RTSP port)
    Byte 4-5: Device Throughput (max 100 Mbps)
    """
    return bytes([
        0x00, 0x00, 0x06,
        0x00, 0x10,  # Info: WFD Source, Session Available (No HDCP/Coupled Sink)
        (rtsp_port >> 8) & 0xff, rtsp_port & 0xff,
        0x00, 0xc8   # Throughput: 100 Mbps
    ])


def _wfd_ie_device_name(name: str) -> bytes:
    """
    WFD Subelement ID 10: WFD Device Name
    """
    encoded = name.encode("utf-8")
    length = len(encoded)
    return bytes([0x0a, (length >> 8) & 0xff, length & 0xff]) + encoded


@dataclass
class WFDPeer:
    address: str
    name: str = ""
    details: str = ""
    path: str = ""
    source: str = ""
    rtsp_port: int = 7236


def _parse_gdbus_byte_array(raw: str) -> list[int]:
    """Parse a gdbus @ay variant string into a list of integer byte values.

    NetworkManager returns WFD IEs via gdbus as a formatted string such as:
    ``<@ay [byte 0x00, byte 0x10, byte 0x1c, byte 0x00, byte 0x1c, ...]>`
    """
    return [int(h, 16) for h in re.findall(r"0x([0-9a-fA-F]+)", raw)]


def _parse_wfd_ies_rtsp_port(wfd_ies: list[int]) -> int:
    """
    Parse the WFD Information Element bytes to find the Sink's RTSP port.
    WFD Subelement ID 0: WFD Device Information (length 6)
    Bytes 3-4 of the subelement (offset 3 and 4 after ID and Length) 
    contain the RTSP port.
    """
    if not wfd_ies or len(wfd_ies) < 6:
        return 7236
    
    i = 0
    while i + 3 <= len(wfd_ies):
        sub_id = wfd_ies[i]
        sub_len = (wfd_ies[i+1] << 8) | wfd_ies[i+2]
        if sub_id == 0 and sub_len >= 6 and i + 3 + sub_len <= len(wfd_ies):
            # Port is at index i + 3 + 2 and i + 3 + 3
            port = (wfd_ies[i+5] << 8) | wfd_ies[i+6]
            return port if port > 0 else 7236
        i += 3 + sub_len
    return 7236


class WFDNotReady(RuntimeError):
    pass


@dataclass
class RTSPMessage:
    start: str
    headers: dict[str, str]
    raw_headers: list[str]
    body: str = ""

    @property
    def is_response(self) -> bool:
        return self.start.startswith("RTSP/")

    @property
    def method(self) -> str:
        if self.is_response:
            return ""
        return self.start.split(maxsplit=1)[0] if self.start else ""

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "0")

    @property
    def status(self) -> str:
        if not self.is_response:
            return ""
        parts = self.start.split(maxsplit=2)
        return " ".join(parts[1:]) if len(parts) >= 2 else ""


@dataclass
class WFDMediaConfig:
    monitor: Optional[object]
    fps: int = 30
    bitrate: str = "4M"
    output_resolution: Optional[str] = None
    audio_device: Optional[str] = None
    no_audio: bool = False
    test_pattern: bool = False
    source_port: int = 19002
    media_pipeline: str = "auto"
    latency_log_path: Optional[str] = None
    capture_backend: str = "auto"
    peer_name: str = ""


@dataclass
class WFDVideoFormat:
    native: str
    preferred: str
    profile: str
    level: str
    cea_mask: int
    vesa_mask: int
    hh_mask: int


@dataclass(frozen=True)
class WFDCEAMode:
    name: str
    bit: int
    native: str
    width: int
    height: int
    fps: int

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


WFD_CEA_MODES: dict[int, WFDCEAMode] = {
    WFD_CEA_720P30: WFDCEAMode("1280x720p30", WFD_CEA_720P30, "28", 1280, 720, 30),
    WFD_CEA_720P60: WFDCEAMode("1280x720p60", WFD_CEA_720P60, "30", 1280, 720, 60),
    WFD_CEA_1080P30: WFDCEAMode("1920x1080p30", WFD_CEA_1080P30, "38", 1920, 1080, 30),
    WFD_CEA_1080P60: WFDCEAMode("1920x1080p60", WFD_CEA_1080P60, "40", 1920, 1080, 60),
}


def _parse_resolution(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _detect_audio_monitor() -> str:
    try:
        sink = subprocess.check_output(
            ["pactl", "get-default-sink"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if sink:
            return sink + ".monitor"
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sinks"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "RUNNING" in line:
                return line.split("\t")[1] + ".monitor"
    except Exception:
        pass
    return "default"


def _is_hyprland_session() -> bool:
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    session = (os.environ.get("XDG_SESSION_DESKTOP") or "").lower()
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) or "hyprland" in desktop or "hyprland" in session


def _is_wayland_session() -> bool:
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    return bool(os.environ.get("WAYLAND_DISPLAY")) or session_type == "wayland"


def _is_x11_session() -> bool:
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    return bool(os.environ.get("DISPLAY")) and (session_type == "x11" or not _is_wayland_session())


def _wfd_capture_backend_order(config: WFDMediaConfig) -> list[str]:
    if config.capture_backend != "auto":
        return [config.capture_backend]
    if _is_hyprland_session():
        return ["wf-recorder", "x11grab"]
    if _is_x11_session():
        return ["x11grab", "wf-recorder"]
    if _is_wayland_session():
        if _is_hyprland_session():
            return ["wf-recorder", "x11grab"]
        # Prefer portal capture on KDE/GNOME Wayland.
        return ["portal", "wf-recorder"]
    return ["x11grab", "wf-recorder"]


def _gst_has_element(name: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _gst_wfd_sender_available() -> bool:
    return (
        shutil.which("gst-launch-1.0") is not None
        and _gst_has_element("mpegtsmux")
        and _gst_has_element("rtpmp2tpay")
        and _gst_has_element("x264enc")
    )


def _gst_pipewiresrc_properties() -> set[str]:
    if not shutil.which("gst-inspect-1.0"):
        return set()
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", "pipewiresrc"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()

    props: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s{2}([a-z0-9_-]+)\s+:", line)
        if match:
            props.add(match.group(1))
    return props


# Cached at first call; x264enc version varies across distros.
_gst_x264enc_props_cache: Optional[set[str]] = None


def _gst_x264enc_properties() -> set[str]:
    """Return the set of property names supported by the installed x264enc.
    Cached after the first call.
    """
    global _gst_x264enc_props_cache
    if _gst_x264enc_props_cache is not None:
        return _gst_x264enc_props_cache
    if not shutil.which("gst-inspect-1.0"):
        _gst_x264enc_props_cache = set()
        return _gst_x264enc_props_cache
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", "x264enc"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        _gst_x264enc_props_cache = set()
        return _gst_x264enc_props_cache
    if result.returncode != 0:
        _gst_x264enc_props_cache = set()
        return _gst_x264enc_props_cache

    props: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s{2}([a-z0-9_-]+)\s+:", line)
        if match:
            props.add(match.group(1))
    _gst_x264enc_props_cache = props
    return props



def _pipewiresrc_selector_attempts(
    node_id: int,
    stream_label: str = "",
) -> list[tuple[str, list[str]]]:
    props = _gst_pipewiresrc_properties()
    attempts: list[tuple[str, list[str]]] = []
    has_autoconnect = "autoconnect" in props

    def _add(base_name: str, base_args: list[str]) -> None:
        # Try compositor-friendly selector mode first.
        if has_autoconnect:
            attempts.append((base_name, [*base_args, "autoconnect=true"]))
        else:
            attempts.append((base_name, base_args))
        # Then strict mode pinned to the selected node.
        if has_autoconnect:
            attempts.append((base_name + "+strict", [*base_args, "autoconnect=false"]))

    if "path" in props:
        _add("path", [f"path={node_id}"])
    # Keep target-object fallback disabled for now. On the tested KDE/PipeWire
    # stack this branch is unstable and can trigger gst-launch crashes. kurva...
    _ = stream_label
    if not attempts:
        raise WFDNotReady(
            "Portal backend could not target a specific PipeWire stream node: "
            "pipewiresrc has neither target-object nor path property."
        )
    return attempts


def _gst_pick_aac_encoder() -> tuple[str, list[str]]:
    """
    Pick a broadly available AAC encoder and a compatible raw-audio caps filter.
    """
    if _gst_has_element("fdkaacenc"):
        return "fdkaacenc", ["audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"]
    if _gst_has_element("avenc_aac"):
        return "avenc_aac", ["audio/x-raw,rate=48000,channels=2"]
    if _gst_has_element("voaacenc"):
        return "voaacenc", ["audio/x-raw,rate=48000,channels=2"]
    if _gst_has_element("faac"):
        return "faac", ["audio/x-raw,rate=48000,channels=2"]
    raise WFDNotReady(
        "No usable GStreamer AAC encoder found (tried fdkaacenc, avenc_aac, voaacenc, faac)."
    )


def _vbv_bufsize(bitrate_text: str, config: WFDMediaConfig) -> str:
    """
    Calculate VBV buffer size.
    """
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)([kKmMgG]?)\s*", bitrate_text)
    if not match:
        return bitrate_text
    
    amount = float(match.group(1))
    suffix = match.group(2)
    
    is_lg = "LG" in config.peer_name.upper()
    # For LG, use 0.5x bitrate (500ms buffer) for others and my samsung 2x (2s buffer).
    multiplier = 0.5 if is_lg else 2.0
    
    amount *= multiplier
    amount_text = str(int(amount)) if amount.is_integer() else f"{amount:g}"
    return amount_text + suffix


def _bitrate_to_kbits(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)([kKmMgG]?)\s*", value)
    if not match:
        return 4000

    amount = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "g":
        amount *= 1_000_000
    elif suffix == "m":
        amount *= 1_000
    elif suffix == "k":
        amount *= 1
    else:
        amount /= 1_000
    return max(1, round(amount))


def _kbits_to_bitrate_text(value_kbits: int) -> str:
    if value_kbits % 1000 == 0:
        return f"{value_kbits // 1000}M"
    return f"{value_kbits}k"


def _quality_floor_kbits(width: int, height: int, fps: int) -> int:
    """
    Conservative quality floors for desktop readability at low latency.
    """
    pixels = width * height
    if pixels <= 1280 * 720:
        return 5000 if fps <= 30 else 7000
    if pixels <= 1920 * 1080:
        return 8000 if fps <= 30 else 12000
    return 12000 if fps <= 30 else 16000


def _calculate_gop(config: WFDMediaConfig) -> int:
    """
    Calculate Group of Pictures (GOP) size.
    LG TVs are strict and often require more frequent keyframes (IDR frames)
    to maintain a stable session, especially during initial buffering.
    """
    gop = max(1, config.fps)
    if "LG" in config.peer_name.upper():
        # For LG, use a 0.5s or 1s GOP but no more than 30 frames.
        return min(gop, 30)
    return gop


def _append_latency_log(path: Optional[str], event: str, **fields: object) -> None:
    if not path:
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "mono": round(time.monotonic(), 6),
        "event": event,
        **fields,
    }
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _parse_sink_video_format(value: str) -> Optional[WFDVideoFormat]:
    first_codec = value.split(",", 1)[0]
    tokens = first_codec.split()
    if len(tokens) < 11 or tokens[0].lower() == "none":
        return None
    try:
        return WFDVideoFormat(
            native=tokens[0],
            preferred=tokens[1],
            profile=tokens[2],
            level=tokens[3],
            cea_mask=int(tokens[4], 16),
            vesa_mask=int(tokens[5], 16),
            hh_mask=int(tokens[6], 16),
        )
    except ValueError:
        return None


def _choose_profile(profile_hex: str) -> str:
    try:
        profile_mask = int(profile_hex, 16)
    except ValueError:
        return profile_hex
    if profile_mask & 0x01:
        return "01"
    if profile_mask & 0x02:
        return "02"
    return f"{profile_mask & 0xff:02x}"


def _max_wfd_level(level_hex: str) -> Optional[int]:
    try:
        value = int(level_hex, 16)
    except ValueError:
        return None
    if value <= 0:
        return None
    highest = 1
    while highest << 1 <= value:
        highest <<= 1
    return highest


def _wfd_level_for_mode(mode: WFDCEAMode) -> int:
    if mode.width <= 1280 and mode.height <= 720:
        return WFD_LEVEL_31 if mode.fps <= 30 else WFD_LEVEL_32
    return WFD_LEVEL_40 if mode.fps <= 30 else WFD_LEVEL_42


def _desired_resolution(config: WFDMediaConfig) -> Optional[tuple[int, int]]:
    resolution = _parse_resolution(config.output_resolution)
    if resolution is not None:
        return resolution
    if config.monitor is not None:
        monitor = config.monitor
        return monitor.width, monitor.height
    return None


def _choose_cea_mode(
    config: WFDMediaConfig,
    sink_format: Optional[WFDVideoFormat],
) -> WFDCEAMode:
    supported = sink_format.cea_mask if sink_format else (
        WFD_CEA_720P30 | WFD_CEA_720P60 | WFD_CEA_1080P30 | WFD_CEA_1080P60
    )
    max_level = _max_wfd_level(sink_format.level) if sink_format else WFD_LEVEL_42
    resolution = _desired_resolution(config)
    wants_720 = resolution is None or (resolution[0] <= 1280 and resolution[1] <= 720)
    wants_60 = config.fps > 30

    def supports(bit: int) -> bool:
        if not (supported & bit):
            return False
        mode = WFD_CEA_MODES[bit]
        return max_level is None or _wfd_level_for_mode(mode) <= max_level

    if wants_720:
        preferred = (
            [WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [WFD_CEA_720P30, WFD_CEA_720P60]
        )
    else:
        preferred = (
            [WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [
                WFD_CEA_1080P30,
                WFD_CEA_720P30,
                WFD_CEA_1080P60,
                WFD_CEA_720P60,
            ]
        )

    for bit in preferred:
        if supports(bit):
            return WFD_CEA_MODES[bit]

    for bit in (
        WFD_CEA_720P30,
        WFD_CEA_1080P30,
        WFD_CEA_720P60,
        WFD_CEA_1080P60,
    ):
        if supports(bit):
            return WFD_CEA_MODES[bit]

    raise WFDNotReady(
        "The sink did not advertise a supported 720p/1080p CEA WFD mode. "
        f"CEA mask was 0x{supported:08x}, "
        f"H.264 level was {sink_format.level if sink_format else 'unknown'}."
    )


def _selected_video_format(
    config: WFDMediaConfig,
    sink_format: Optional[WFDVideoFormat],
) -> str:
    mode = _choose_cea_mode(config, sink_format)

    profile = _choose_profile(sink_format.profile) if sink_format else "01"
    level = f"{_wfd_level_for_mode(mode):02x}"
    return (
        f"{mode.native} 00 {profile} {level} {mode.bit:08x} "
        "00000000 00000000 00 0000 0000 00 none none"
    )


def _h264_level_for_mode(config: WFDMediaConfig) -> str:
    resolution = _parse_resolution(config.output_resolution) or (1920, 1080)
    width, height = resolution
    if width <= 1280 and height <= 720:
        return "3.1" if config.fps <= 30 else "3.2"
    return "4.0" if config.fps <= 30 else "4.2"


def _safe_source_port(requested: int, sink_port: int, sink_rtcp_port: int = 0) -> int:
    blocked = {sink_port}
    if sink_rtcp_port:
        blocked.add(sink_rtcp_port)
    else:
        blocked.add(sink_port + 1)

    port = requested
    if port % 2:
        port += 1
    while port in blocked or port + 1 in blocked:
        port += 2
    return port


def _rtp_url(tv_ip: str, sink_port: int, source_port: int, local_ip: str) -> str:
    # Bind both RTP and RTCP to the ports advertised in the RTSP SETUP reply.
    # ffmpeg's pkt_size is the whole UDP payload, including the 12-byte RTP
    # header. WFD receivers expect seven 188-byte TS packets per RTP payload:
    # 12 + (7 * 188) = 1328 bytes.
    return (
        f"rtp://{tv_ip}:{sink_port}"
        f"?localaddr={local_ip}"
        f"&local_rtpport={source_port}"
        f"&local_rtcpport={source_port + 1}"
        "&pkt_size=1328"
    )


def _interface_for_ip(local_ip: str) -> Optional[str]:
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "-o", "-4", "addr", "show"], timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    needle = f" {local_ip}/"
    for line in result.stdout.splitlines():
        if needle not in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            return parts[1].split("@", 1)[0]
    return None


def _netdev_tx_bytes(interface: Optional[str]) -> Optional[int]:
    if not interface:
        return None
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError:
        return None

    prefix = interface + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        _, _, counters = stripped.partition(":")
        fields = counters.split()
        if len(fields) >= 16:
            try:
                return int(fields[8])
            except ValueError:
                return None
    return None


class WFDMediaPipeline:
    def __init__(
        self,
        config: WFDMediaConfig,
        tv_ip: str,
        local_ip: str,
        sink_rtp_port: int,
    ) -> None:
        self.config = config
        self.tv_ip = tv_ip
        self.local_ip = local_ip
        self.sink_rtp_port = sink_rtp_port
        self.processes: list[subprocess.Popen[bytes]] = []
        self.tx_interface: Optional[str] = None
        self.tx_baseline: Optional[int] = None
        self.portal_session: Optional[PortalCaptureSession] = None

    def start(self) -> None:
        if self.processes:
            return

        self.tx_interface = _interface_for_ip(self.local_ip)
        self.tx_baseline = _netdev_tx_bytes(self.tx_interface)

        requested_pipeline = self.config.media_pipeline
        pipeline = requested_pipeline
        if pipeline == "auto":
            pipeline = "gst" if self.config.test_pattern and _gst_wfd_sender_available() else "ffmpeg"

        if pipeline == "gst":
            if not self.config.test_pattern:
                raise WFDNotReady("GStreamer WFD sender is currently implemented for --wfd-test-pattern only.")
            try:
                self._start_gst_test_pattern()
            except WFDNotReady:
                if requested_pipeline == "auto":
                    print("[FluxCast WFD Media] GStreamer sender failed to start; falling back to ffmpeg.")
                    self._start_test_pattern()
                else:
                    raise
        elif self.config.test_pattern:
            self._start_test_pattern()
        else:
            self._start_desktop()

    def tx_summary(self) -> str:
        current = _netdev_tx_bytes(self.tx_interface)
        if self.tx_baseline is None or current is None:
            return "tx=unknown"
        delta = max(0, current - self.tx_baseline)
        return f"tx+{delta // 1024} KiB on {self.tx_interface}"

    def stop(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        self.processes.clear()
        close_portal_capture(self.portal_session)
        self.portal_session = None

    def _rtp_output(self) -> str:
        return _rtp_url(self.tv_ip, self.sink_rtp_port, self.config.source_port, self.local_ip)

    def _common_output_args(self) -> list[str]:
        """
        Low-latency RTP/MPEG-TS output args.
        """
        return [
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-flush_packets", "1",
            # WFD receivers (notably Samsung) are sensitive to MPEG-TS layout.
            # Keep PMT/video/audio PID values aligned with the working gst path!!!
            # PMT PID 0x1000, video PID 0x1011, audio PID 0x1100.
            "-mpegts_pmt_start_pid", "4096",
            "-mpegts_start_pid", "4113",
            "-streamid", "0:4113",
            "-mpegts_flags", "resend_headers+pat_pmt_at_frames",
            "-pat_period", "0.1",
            "-pcr_period", "20",
            "-f", "rtp_mpegts",
            self._rtp_output(),
        ]

    def _start_test_pattern(self) -> None:
        if not shutil.which("ffmpeg"):
            raise WFDNotReady("ffmpeg is required for WFD test-pattern streaming.")

        resolution = self.config.output_resolution or "1280x720"
        gop = _calculate_gop(self.config)
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-loglevel", "warning",
            "-re",
            "-f", "lavfi",
            "-i", f"testsrc2=size={resolution}:rate={self.config.fps}",
        ]

        if not self.config.no_audio:
            cmd += [
                "-re",
                "-f", "lavfi",
                "-i", "sine=frequency=880:sample_rate=48000",
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
        else:
            cmd += ["-map", "0:v:0"]

        cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level:v", _h264_level_for_mode(self.config),
            "-pix_fmt", "yuv420p",
            "-r", str(self.config.fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-bf", "0",
            "-b:v", self.config.bitrate,
            "-maxrate", self.config.bitrate,
            "-bufsize", _vbv_bufsize(self.config.bitrate, self.config),
            "-x264-params", "repeat-headers=1:aud=1",
        ]

        if not self.config.no_audio:
            cmd += [
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        cmd += self._common_output_args()

        print(
            f"[FluxCast WFD Media] Starting test RTP stream to "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )
        proc = subprocess.Popen(cmd)  # stderr/stdout visible for debugging
        time.sleep(0.8)
        if proc.poll() is not None:
            raise WFDNotReady("ffmpeg test-pattern pipeline exited immediately.")
        self.processes = [proc]

    def _start_gst_test_pattern(self) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise WFDNotReady("gst-launch-1.0 is required for the GStreamer WFD test-pattern pipeline.")
        if not _gst_wfd_sender_available():
            raise WFDNotReady(
                "GStreamer WFD test-pattern pipeline needs mpegtsmux, rtpmp2tpay, and x264enc."
            )

        resolution = _parse_resolution(self.config.output_resolution) or (1280, 720)
        width, height = resolution
        bitrate_kbits = _bitrate_to_kbits(self.config.bitrate)
        gop = _calculate_gop(self.config)

        prog_map = "program_map,sink_4113=1"
        if not self.config.no_audio:
            prog_map += ",sink_4352=1"

        cmd = [
            "gst-launch-1.0", "-e", "-q",
            "mpegtsmux", "name=mux",
            "alignment=7",
            f"prog-map={prog_map}",
            "pat-interval=9000",
            "pmt-interval=9000",
            "pcr-interval=3600",
            "!", "rtpmp2tpay", "pt=33", "mtu=1328",
            "!", "udpsink",
            f"host={self.tv_ip}",
            f"port={self.sink_rtp_port}",
            f"bind-address={self.local_ip}",
            f"bind-port={self.config.source_port}",
            "sync=false",
            "async=false",
            "videotestsrc", "is-live=true", "pattern=smpte",
            "!", f"video/x-raw,width={width},height={height},framerate={self.config.fps}/1",
            "!", "videoconvert",
            "!", "x264enc",
            "tune=zerolatency",
            "speed-preset=ultrafast",
            f"bitrate={bitrate_kbits}",
            f"key-int-max={gop}",
            "bframes=0",
            "byte-stream=true",
            "aud=true",
            "sliced-threads=true",
            "vbv-buf-capacity=200",
            "!", "video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline",
            "!", "queue",
            "!", "mux.sink_4113",
        ]

        if not self.config.no_audio:
            audio_encoder, audio_caps = _gst_pick_aac_encoder()
            cmd += [
                "audiotestsrc", "is-live=true", "wave=sine", "freq=880",
                "!", "audioconvert",
                "!", "audioresample",
                "!", *audio_caps,
                "!", audio_encoder, "bitrate=128000",
                "!", "aacparse",
                "!", "queue",
                "!", "mux.sink_4352",
            ]

        print(
            f"[FluxCast WFD Media] Starting GStreamer test RTP stream to "
            f"{self.tv_ip}:{self.sink_rtp_port} from {self.local_ip}:{self.config.source_port}"
        )
        print(f"[FluxCast WFD Media] GST cmd: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd)  # stderr/stdout visible for debugging
        time.sleep(0.8)
        if proc.poll() is not None:
            raise WFDNotReady("GStreamer test-pattern pipeline exited immediately.")
        self.processes = [proc]

    def _start_desktop(self) -> None:
        if not shutil.which("ffmpeg"):
            raise WFDNotReady("ffmpeg is required for WFD desktop streaming.")

        backends = _wfd_capture_backend_order(self.config)
        errors: list[str] = []
        for idx, backend in enumerate(backends):
            try:
                if backend == "x11grab":
                    self._start_desktop_x11grab()
                elif backend == "portal":
                    self._start_desktop_portal()
                else:
                    self._start_desktop_wf_recorder()
                return
            except WFDNotReady as exc:
                errors.append(f"{backend}: {exc}")
                if idx < len(backends) - 1:
                    print(f"[FluxCast WFD Media] Backend {backend} failed, trying fallback...")
        detail = "; ".join(errors) if errors else "No usable capture backend"
        if self.config.capture_backend == "auto" and _is_wayland_session() and not _is_hyprland_session():
            detail += (
                "; KDE/GNOME Wayland desktop capture uses portal backend in this build. "
                "Install dbus-next + xdg-desktop-portal stack + gst-launch-1.0, "
                "then allow screen-share in the portal picker dialog."
            )
        raise WFDNotReady(detail)

    def _start_desktop_portal(self) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise WFDNotReady("Portal backend requires gst-launch-1.0 (pipewiresrc pipeline).")
        required = (
            "pipewiresrc", "videoconvert", "videoscale",
            "x264enc", "mpegtsmux", "rtpmp2tpay", "udpsink",
        )
        if not self.config.no_audio:
            required += ("pulsesrc", "audioconvert", "audioresample", "aacparse")
        missing = [name for name in required
                   if not _gst_has_element(name)]
        if missing:
            raise WFDNotReady(
                "Portal backend is missing required GStreamer elements: "
                + ", ".join(missing)
            )
        monitor = self.config.monitor
        if self.config.output_resolution:
            out_res = self.config.output_resolution
        elif monitor is not None:
            out_res = f"{monitor.width}x{monitor.height}"
        else:
            out_res = "1920x1080"
        src_res = out_res
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (1920, 1080)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        effective_kbits = max(requested_kbits, floor_kbits)

        is_lg = "LG" in self.config.peer_name.upper()
        if is_lg:
            effective_kbits = min(effective_kbits, 4000)

        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        if effective_kbits > requested_kbits:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )

        print("[FluxCast WFD Media] Opening portal screen-share dialog (KDE/GNOME Wayland)...")
        try:
            self.portal_session = start_portal_capture(
                timeout=120.0,
                preferred_position=(monitor.x, monitor.y) if monitor is not None else None,
                preferred_size=(monitor.width, monitor.height) if monitor is not None else None,
            )
        except PortalCaptureError as exc:
            raise WFDNotReady(f"portal capture setup failed: {exc}") from exc

        session = self.portal_session
        if session.source_type is not None and session.source_type != 1:
            close_portal_capture(self.portal_session)
            self.portal_session = None
            raise WFDNotReady(
                "Portal returned a non-monitor source (likely camera/window). "
                "In the portal picker choose a full monitor/screen."
            )
        if session.source_type is None:
            close_portal_capture(self.portal_session)
            self.portal_session = None
            raise WFDNotReady(
                "Portal stream metadata has no source_type, cannot verify monitor capture safely. "
                "In KDE portal picker select only a full screen and retry."
            )

        if session.size is not None:
            src_res = f"{session.size[0]}x{session.size[1]}"
        parsed_src = _parse_resolution(src_res) or (1920, 1080)
        out_dims = _parse_resolution(out_res) or parsed_src
        out_w, out_h = out_dims
        selector_attempts = _pipewiresrc_selector_attempts(
            session.pw_node_id,
            stream_label=session.stream_label,
        )
        bitrate_kbits = _bitrate_to_kbits(effective_bitrate)
        prog_map = "program_map,sink_4113=1"

        def _gst_video_chain(video_caps: str, selector_args: list[str]) -> list[str]:
            # Use more buffers for high-res 1440p capture and move videorate early
            props = _gst_pipewiresrc_properties()
            pipewire_args = [*selector_args]
            if "max-buffers" in props:
                pipewire_args.append("max-buffers=64")
            if "resend-last" in props:
                pipewire_args.append("resend-last=true")
            if "min-force-user-latency" in props:
                pipewire_args.append("min-force-user-latency=0")

            is_lg = "LG" in self.config.peer_name.upper()

            # x264enc configuration
            encoder_args = [
                "tune=zerolatency",
                "speed-preset=ultrafast",
                f"bitrate={bitrate_kbits}",
                f"key-int-max={gop}",
                "threads=0",
                "bframes=0",
                "byte-stream=true",
                "aud=true",
                "sliced-threads=true",
            ]

            # Check x264enc properties once; same gst-plugins-ugly version
            # either has all VBV params or none of them.
            x264_props = _gst_x264enc_properties()

            if is_lg:
                # LG Profile
                # gracefully SKIP it if the installed x264enc doesn't support it.
                lg_vbv: list[str] = []
                if "rc-lookahead" in x264_props:
                    lg_vbv.append("rc-lookahead=0")
                else:
                    print(
                        "[FluxCast WFD Media] LG profile: x264enc does not support "
                        "rc-lookahead; skipping lookahead optimization."
                    )
                if "vbv-maxrate" in x264_props:
                    lg_vbv.insert(0, f"vbv-maxrate={bitrate_kbits}")
                else:
                    print(
                        "[FluxCast WFD Media] LG profile: x264enc does not support "
                        "vbv-maxrate; skipping hard rate cap."
                    )
                if "vbv-buf-capacity" in x264_props:
                    lg_vbv.append("vbv-buf-capacity=100")
                else:
                    print(
                        "[FluxCast WFD Media] LG profile: x264enc does not support "
                        "vbv-buf-capacity; VBV buffer control unavailable."
                    )
                encoder_args += lg_vbv
            else:
                if "vbv-buf-capacity" in x264_props:
                    encoder_args += ["vbv-buf-capacity=200"]

            return [
                "pipewiresrc",
                f"fd={session.pw_fd}",
                *pipewire_args,
                "do-timestamp=true",
                "always-copy=false",
                "keepalive-time=33",
                "!", "queue", "max-size-buffers=64", "max-size-time=1000000000", "leaky=downstream",
                "!", "videorate", "skip-to-first=true",
                "!", f"video/x-raw,framerate={self.config.fps}/1",
                "!", "videoconvert",
                "!", "videoscale",
                "!", video_caps,
                "!", "videoconvert",
                "!", "video/x-raw,format=I420",
                "!", "x264enc",
                *encoder_args,
                "!", "video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline",
                "!", "queue",
                "!", "mux.sink_4113",
            ]

        gst_audio_chain: list[str] = []
        if not self.config.no_audio:
            audio_encoder, audio_caps = _gst_pick_aac_encoder()
            prog_map += ",sink_4352=1"
            gst_audio_chain = [
                "pulsesrc", f"device={audio_monitor}", "do-timestamp=true",
                "!", "audioconvert",
                "!", "audioresample",
                "!", *audio_caps,
                "!", audio_encoder, "bitrate=128000",
                "!", "aacparse",
                "!", "queue",
                "!", "mux.sink_4352",
            ]

        def _gst_cmd_for_caps(video_caps: str, selector_args: list[str]) -> list[str]:
            return [
                "gst-launch-1.0", "-e", "-q",
                "mpegtsmux", "name=mux",
                "alignment=7",
                f"prog-map={prog_map}",
                "pat-interval=9000",
                "pmt-interval=9000",
                "pcr-interval=3600",
                "!", "rtpmp2tpay", "pt=33", "mtu=1328",
                "!", "udpsink",
                f"host={self.tv_ip}",
                f"port={self.sink_rtp_port}",
                f"bind-address={self.local_ip}",
                f"bind-port={self.config.source_port}",
                "sync=false",
                "async=false",
                *_gst_video_chain(video_caps, selector_args),
                *gst_audio_chain,
            ]

        caps_strict = (
            f"video/x-raw,width={out_w},height={out_h},"
            f"framerate={self.config.fps}/1"
        )
        caps_no_fps = (
            f"video/x-raw,width={out_w},height={out_h}"
        )
        caps_relaxed = "video/x-raw"
        caps_attempts = [
            ("strict", caps_strict),
            ("no-fps", caps_no_fps),
            ("relaxed", caps_relaxed),
        ]

        print(f"[FluxCast WFD Media] Capturing via portal node : {session.pw_node_id}")
        print(
            "[FluxCast WFD Media] PipeWire selectors      : "
            + ", ".join(name for name, _ in selector_attempts)
        )
        print("[FluxCast WFD Media] Pipeline             : gstreamer (portal->rtp)")
        print(f"[FluxCast WFD Media] Portal source type      : {session.source_type}")
        if session.position and session.size:
            print(
                "[FluxCast WFD Media] Portal source geometry : "
                f"pos={session.position[0]},{session.position[1]} "
                f"size={session.size[0]}x{session.size[1]}"
            )
        if session.stream_label:
            print(f"[FluxCast WFD Media] Portal source id       : {session.stream_label}")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio       : {audio_monitor}")
        if out_dims != parsed_src:
            print(f"[FluxCast WFD Media] Scaling output       : {out_res}")
        print(
            f"[FluxCast WFD Media] RTP target           : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )
        gst_proc = None
        probe_alive_seconds = 3.0
        for selector_name, selector_args in selector_attempts:
            for attempt_name, attempt_caps in caps_attempts:
                print(
                    f"[FluxCast WFD Media] Portal attempt       : "
                    f"selector={selector_name}, caps={attempt_name}"
                )
                gst_cmd = _gst_cmd_for_caps(attempt_caps, selector_args)
                gst_proc = subprocess.Popen(gst_cmd, stderr=None, pass_fds=(session.pw_fd,))
                time.sleep(2.5)
                if gst_proc.poll() is not None:
                    print(
                        "[FluxCast WFD Media] Portal attempt failed; trying next "
                        "selector/caps combination..."
                    )
                    continue

                time.sleep(probe_alive_seconds)
                if gst_proc.poll() is not None:
                    print(
                        "[FluxCast WFD Media] Portal attempt died during TX probe; "
                        "trying next selector/caps combination..."
                    )
                    continue

                self.processes = [gst_proc]
                return

        close_portal_capture(self.portal_session)
        self.portal_session = None
        raise WFDNotReady("portal GStreamer RTP pipeline failed to negotiate formats.")

    def _start_desktop_wf_recorder(self) -> None:
        if not shutil.which("wf-recorder"):
            raise WFDNotReady("wf-recorder is required for WFD desktop streaming.")

        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("wf-recorder backend requires a selected monitor.")
        src_res = f"{monitor.width}x{monitor.height}"
        out_res = self.config.output_resolution or src_res
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (monitor.width, monitor.height)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        effective_kbits = max(requested_kbits, floor_kbits)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        if effective_kbits > requested_kbits:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )

        wf_cmd = [
            "wf-recorder",
            "-y",
            "-D",
            "-r", str(self.config.fps),
            "-o", monitor.name,
            "-c", "rawvideo",
            "-m", "nut",
            "-p", "pix_fmt=yuv420p",
            "-f", "/dev/stdout",
        ]

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-loglevel", "warning",
            "-fflags", "+genpts",
            "-thread_queue_size", "1024",
            "-f", "nut",
            "-i", "pipe:0",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", "1024",
                "-f", "pulse",
                "-i", audio_monitor,
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
        else:
            ffmpeg_cmd += ["-map", "0:v:0"]

        if out_res == src_res:
            ffmpeg_cmd += ["-vf", "format=yuv420p"]
        else:
            ffmpeg_cmd += ["-vf", f"scale={out_res.replace('x', ':')}:out_range=tv,format=yuv420p"]

        ffmpeg_cmd += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level:v", _h264_level_for_mode(self.config),
            "-pix_fmt", "yuv420p",
            "-r", str(self.config.fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-bf", "0",
            "-b:v", effective_bitrate,
            "-maxrate", effective_bitrate,
            "-bufsize", _vbv_bufsize(effective_bitrate, self.config),
            "-x264-params", "repeat-headers=1:aud=1",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({src_res})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if out_res != src_res:
            print(f"[FluxCast WFD Media] Scaling output  : {out_res}")
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        wf_proc = subprocess.Popen(wf_cmd, stdout=subprocess.PIPE, stderr=None)
        if wf_proc.stdout is None:
            wf_proc.kill()
            raise WFDNotReady("wf-recorder did not expose stdout.")

        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=wf_proc.stdout, stderr=None)
        wf_proc.stdout.close()
        time.sleep(1.0)

        if wf_proc.poll() is not None:
            ffmpeg_proc.terminate()
            raise WFDNotReady("wf-recorder exited immediately during WFD streaming.")
        if ffmpeg_proc.poll() is not None:
            wf_proc.terminate()
            raise WFDNotReady("ffmpeg exited immediately during WFD streaming.")

        self.processes = [wf_proc, ffmpeg_proc]

    def _start_desktop_x11grab(self) -> None:
        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("x11grab backend requires a selected monitor.")
        src_res = f"{monitor.width}x{monitor.height}"
        out_res = self.config.output_resolution or src_res
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (monitor.width, monitor.height)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        effective_kbits = max(requested_kbits, floor_kbits)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        if effective_kbits > requested_kbits:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )

        display = os.environ.get("DISPLAY", monitor.display or ":0")
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-loglevel", "warning",
            "-thread_queue_size", "1024",
            "-f", "x11grab",
            "-framerate", str(self.config.fps),
            "-video_size", src_res,
            "-i", f"{display}+{monitor.x},{monitor.y}",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", "1024",
                "-f", "pulse",
                "-i", audio_monitor,
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
        else:
            ffmpeg_cmd += ["-map", "0:v:0"]

        if out_res == src_res:
            ffmpeg_cmd += ["-vf", "format=yuv420p"]
        else:
            ffmpeg_cmd += ["-vf", f"scale={out_res.replace('x', ':')}:out_range=tv,format=yuv420p"]

        ffmpeg_cmd += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level:v", _h264_level_for_mode(self.config),
            "-pix_fmt", "yuv420p",
            "-r", str(self.config.fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-bf", "0",
            "-b:v", effective_bitrate,
            "-maxrate", effective_bitrate,
            "-bufsize", _vbv_bufsize(effective_bitrate, self.config),
            "-x264-params", "repeat-headers=1:aud=1",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(
            "[FluxCast WFD Media] Using x11grab backend for desktop capture "
            f"from {display}+{monitor.x},{monitor.y}"
        )
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if out_res != src_res:
            print(f"[FluxCast WFD Media] Scaling output  : {out_res}")
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stderr=None)
        time.sleep(1.0)
        if ffmpeg_proc.poll() is not None:
            raise WFDNotReady("ffmpeg x11grab sender exited immediately during WFD streaming.")
        self.processes = [ffmpeg_proc]


def _read_rtsp_message(rfile) -> Optional[RTSPMessage]:
    lines = []
    while True:
        raw = rfile.readline(8192)
        if not raw:
            return None
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            break
        lines.append(line)

    if not lines:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    content_length = 0
    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError:
        content_length = 0

    body = ""
    if content_length > 0:
        body = rfile.read(content_length).decode("utf-8", errors="replace")

    return RTSPMessage(
        start=lines[0],
        headers=headers,
        raw_headers=lines[1:],
        body=body,
    )


def _parse_parameters(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            params[key.strip().lower()] = value.strip()
    return params


def _parse_rtp_ports(value: str) -> Optional[tuple[int, int]]:
    match = re.search(
        r"RTP/AVP/(?:UDP|TCP);unicast\s+(\d+)\s+(\d+)\s+mode=play",
        value,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _parse_transport_client_ports(value: str) -> Optional[tuple[int, int]]:
    match = re.search(r"client_port=(\d+)(?:-(\d+))?", value, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2) or "0")
    return None


class _WFDRTSPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self.local_ip = self.request.getsockname()[0]
        self.next_cseq = 1
        self.pending: dict[str, str] = {}
        self.session_id = str(random.randint(1_000_000, 9_999_999))
        self._write_lock = threading.Lock()
        self._keepalive_active = True
        self.sink_rtp_port: Optional[int] = None
        self.sink_rtcp_port: int = 0
        self.source_rtp_port = self.media_config.source_port
        self.sink_video_format: Optional[WFDVideoFormat] = None
        self.negotiated_no_audio = False
        self.m3_sent = False
        self.media: Optional[WFDMediaPipeline] = None
        self.connected_at = time.monotonic()
        self.play_accepted_at: Optional[float] = None
        self.setup_ms: Optional[float] = None
        self.first_tx_reported = False

        if hasattr(self.server, "parent_server"):
            self.server.parent_server.has_connected_client = True  # type: ignore[attr-defined]

        print(f"[FluxCast WFD RTSP] TV connected from {peer}; local={self.local_ip}")
        _append_latency_log(
            self.media_config.latency_log_path,
            "rtsp_connected",
            peer=peer,
            local_ip=self.local_ip,
        )
        try:
            self._send_m1_options()
            while True:
                msg = _read_rtsp_message(self.rfile)
                if msg is None:
                    print(f"[FluxCast WFD RTSP] TV disconnected from {peer}")
                    return
                self._log_message(msg)
                if msg.is_response:
                    self._handle_response(msg)
                else:
                    self._handle_request(msg)
        except WFDNotReady as exc:
            print(f"[FluxCast WFD RTSP] ERROR: {exc}")
        except OSError as exc:
            print(f"[FluxCast WFD RTSP] Socket closed: {exc}")
        finally:
            self._keepalive_active = False
            self._stop_media()

    @property
    def media_config(self) -> WFDMediaConfig:
        return self.server.media_config  # type: ignore[attr-defined]

    @property
    def rtsp_port(self) -> int:
        return self.server.server_address[1]  # type: ignore[attr-defined]

    def _rtsp_control_uri(self) -> str:
        return "rtsp://localhost/wfd1.0"

    def _rtsp_presentation_uri(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/wfd1.0"

    def _cea_mode(self) -> WFDCEAMode:
        return _choose_cea_mode(self.media_config, self.sink_video_format)

    def _video_format(self) -> str:
        return _selected_video_format(self.media_config, self.sink_video_format)

    def _audio_codecs(self) -> str:
        if self.media_config.no_audio or self.negotiated_no_audio:
            return "none"
        return WFD_AUDIO_AAC

    def _send_bytes(self, text: str) -> None:
        with self._write_lock:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

    def _send_request(
        self,
        name: str,
        method: str,
        uri: str,
        headers: Optional[dict[str, str]] = None,
        body: str = "",
    ) -> None:
        cseq = str(self.next_cseq)
        self.next_cseq += 1
        self.pending[cseq] = name

        output = [
            f"{method} {uri} RTSP/1.0",
            f"CSeq: {cseq}",
        ]
        if headers and "Session" in headers:
            output.append(f"Session: {headers['Session']}")
        for key, value in (headers or {}).items():
            if key == "Session":
                continue
            output.append(f"{key}: {value}")
        if body:
            output.append("Content-Type: text/parameters")
            output.append(f"Content-Length: {len(body.encode('utf-8'))}")
        output.append("")
        output.append(body)
        self._send_bytes("\r\n".join(output))
        print(f"[FluxCast WFD RTSP] -> {name}: {method} (CSeq {cseq})")
        if body:
            for line in body.splitlines():
                if line.startswith("wfd_"):
                    print(f"[FluxCast WFD RTSP]   {line}")

    def _send_response(
        self,
        msg: RTSPMessage,
        status: str = "200 OK",
        headers: Optional[dict[str, str]] = None,
        body: str = "",
    ) -> None:
        output = [
            f"RTSP/1.0 {status}",
            f"CSeq: {msg.cseq}",
        ]
        if headers and "Session" in headers:
            output.append(f"Session: {headers['Session']}")
        output.append("Server: FluxCast-WFD/0.1")
        for key, value in (headers or {}).items():
            if key == "Session":
                continue
            output.append(f"{key}: {value}")
        if body:
            output.append("Content-Type: text/parameters")
        output.append(f"Content-Length: {len(body.encode('utf-8'))}")
        output.append("")
        output.append(body)
        self._send_bytes("\r\n".join(output))
        print(f"[FluxCast WFD RTSP] -> response {status} for {msg.method or msg.status}")

    def _send_m1_options(self) -> None:
        self._send_request(
            "M1_OPTIONS",
            "OPTIONS",
            "*",
            headers={"Require": "org.wfa.wfd1.0"},
        )

    def _send_m3_get_parameters(self) -> None:
        if self.m3_sent:
            return
        self.m3_sent = True
        body = (
            "wfd_video_formats\r\n"
            "wfd_audio_codecs\r\n"
            "wfd_client_rtp_ports\r\n"
        )
        self._send_request(
            "M3_GET_PARAMETER",
            "GET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _send_m4_set_parameters(self) -> None:
        if not self.sink_rtp_port:
            raise WFDNotReady("TV did not provide a valid RTP port in M3.")
        sink_rtcp_port = self.sink_rtcp_port if self.sink_rtcp_port > 0 else 0
        body = (
            f"wfd_video_formats: {self._video_format()}\r\n"
            f"wfd_audio_codecs: {self._audio_codecs()}\r\n"
            f"wfd_presentation_URL: {self._rtsp_presentation_uri()}/streamid=0 none\r\n"
            "wfd_client_rtp_ports: RTP/AVP/UDP;unicast "
            f"{self.sink_rtp_port} {sink_rtcp_port} mode=play\r\n"
        )
        self._send_request(
            "M4_SET_PARAMETER",
            "SET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _send_m5_trigger_setup(self) -> None:
        body = "wfd_trigger_method: SETUP\r\n"
        self._send_request(
            "M5_TRIGGER_SETUP",
            "SET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _handle_response(self, msg: RTSPMessage) -> None:
        name = self.pending.pop(msg.cseq, "UNKNOWN")
        if not msg.status.startswith("200"):
            if name == "M16_KEEPALIVE":
                # LG (or any TV) rejected our keepalive, STOP RESCHEDULING
                # but keep the stream alive
                print(
                    f"[FluxCast WFD RTSP] M16 keepalive rejected: {msg.status} "
                    "— disabling keepalive, stream continues."
                )
                self._keepalive_active = False
                return
            raise WFDNotReady(f"RTSP {name} failed: {msg.start}")

        print(f"[FluxCast WFD RTSP] <- response for {name}: {msg.status}")
        if name == "M3_GET_PARAMETER":
            params = _parse_parameters(msg.body)
            ports = _parse_rtp_ports(params.get("wfd_client_rtp_ports", ""))
            if not ports or ports[0] <= 0:
                raise WFDNotReady(
                    "TV M3 response did not include a usable wfd_client_rtp_ports value."
                )
            self.sink_rtp_port, self.sink_rtcp_port = ports
            self.source_rtp_port = _safe_source_port(
                self.media_config.source_port,
                self.sink_rtp_port,
                self.sink_rtcp_port,
            )
            self.sink_video_format = _parse_sink_video_format(
                params.get("wfd_video_formats", "")
            )
            audio = params.get("wfd_audio_codecs", "")
            if (
                audio
                and not self.media_config.no_audio
                and "AAC" not in audio.upper()
            ):
                self.negotiated_no_audio = True
                print(
                    "[FluxCast WFD RTSP] TV did not advertise AAC; "
                    "falling back to video-only WFD."
                )
            mode = self._cea_mode()
            print(
                f"[FluxCast WFD RTSP] TV RTP port: {self.sink_rtp_port}; "
                f"source port: {self.source_rtp_port}; audio={audio or 'unknown'}"
            )
            print(f"[FluxCast WFD RTSP] Negotiated media mode: {mode.name}")
            print(f"[FluxCast WFD RTSP] Selected video format: {self._video_format()}")
            self._send_m4_set_parameters()
        elif name == "M1_OPTIONS":
            self._send_m3_get_parameters()
        elif name == "M4_SET_PARAMETER":
            self._send_m5_trigger_setup()

    def _handle_request(self, msg: RTSPMessage) -> None:
        method = msg.method
        if method == "OPTIONS":
            self._send_response(
                msg,
                headers={
                    "Public": (
                        "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, "
                        "GET_PARAMETER, SET_PARAMETER"
                    )
                },
            )
            self._send_m3_get_parameters()
            return

        if method == "GET_PARAMETER":
            requested = msg.body.lower()
            lines = []
            if "wfd_video_formats" in requested:
                lines.append(f"wfd_video_formats: {self._video_format()}\r\n")
            if "wfd_audio_codecs" in requested:
                lines.append(f"wfd_audio_codecs: {self._audio_codecs()}\r\n")
            if "wfd_content_protection" in requested:
                lines.append("wfd_content_protection: none\r\n")
            body = "".join(lines)
            self._send_response(msg, headers=self._session_header(), body=body)
            return

        if method == "SET_PARAMETER":
            if "wfd_idr_request" in msg.body:
                print(
                    "[FluxCast WFD RTSP] Sink requested an IDR frame; "
                    "next GOP is <= 1s"
                )
                # Forcing a small GOP helps LG, but some models might still need
                # a direct signal to the encoder. For now, we rely on the
                # LG Profile (frequent GOP) which is already active.
            self._send_response(msg, headers=self._session_header())
            return

        if method == "SETUP":
            ports = _parse_transport_client_ports(msg.headers.get("transport", ""))
            if ports:
                self.sink_rtp_port, self.sink_rtcp_port = ports
            if not self.sink_rtp_port:
                self.sink_rtp_port = 19000
                self.sink_rtcp_port = 0

            self.source_rtp_port = _safe_source_port(
                self.media_config.source_port,
                self.sink_rtp_port,
                self.sink_rtcp_port,
            )
            source_port = self.source_rtp_port
            if self.sink_rtcp_port:
                transport = (
                    "RTP/AVP/UDP;unicast;"
                    f"client_port={self.sink_rtp_port}-{self.sink_rtcp_port};"
                    f"server_port={source_port}-{source_port + 1}"
                )
            else:
                transport = (
                    "RTP/AVP/UDP;unicast;"
                    f"client_port={self.sink_rtp_port};"
                    f"server_port={source_port}"
                )
            self._send_response(
                msg,
                headers={
                    "Transport": transport,
                    "Session": f"{self.session_id};timeout=30",
                },
            )
            print(f"[FluxCast WFD RTSP] SETUP complete; RTP sink port={self.sink_rtp_port}")
            return

        if method == "PLAY":
            self._send_response(
                msg,
                headers={
                    **self._session_header(),
                    "Range": "npt=now-",
                },
            )
            # Schedule RTSP M16 keepalive NOW, before _start_media() blocks
            # on the portal dialog (8-13 s). LG WebOS resets the TCP connection
            # ~40-45 s after PLAY. Starting the 20 seconds timer here gives a safe
            # 20 second head start regardless of portal dialog speed.
            self._schedule_rtsp_keepalive(20.0)
            self._start_media()
            return

        if method == "PAUSE":
            self._send_response(msg, headers=self._session_header())
            self._stop_media()
            return

        if method == "TEARDOWN":
            self._send_response(
                msg,
                headers={
                    **self._session_header(),
                    "Connection": "close",
                },
            )
            self._stop_media()
            return

        self._send_response(msg, status="405 Method Not Allowed")

    def _session_header(self) -> dict[str, str]:
        return {"Session": f"{self.session_id};timeout=30"}

    def _start_media(self) -> None:
        if not self.sink_rtp_port:
            raise WFDNotReady("Cannot start media before the TV RTP port is known.")
        if self.media is None:
            mode = self._cea_mode()
            effective_config = replace(
                self.media_config,
                source_port=self.source_rtp_port,
                output_resolution=mode.resolution,
                fps=mode.fps,
                no_audio=self.media_config.no_audio or self.negotiated_no_audio,
            )
            print(
                f"[FluxCast WFD RTSP] Starting media as {mode.name}; "
                f"RTP source port {self.source_rtp_port}"
            )
            _append_latency_log(
                self.media_config.latency_log_path,
                "media_starting",
                mode=mode.name,
                tv_ip=self.client_address[0],
                sink_rtp_port=self.sink_rtp_port,
                source_rtp_port=self.source_rtp_port,
            )
            self.media = WFDMediaPipeline(
                effective_config,
                tv_ip=self.client_address[0],
                local_ip=self.local_ip,
                sink_rtp_port=self.sink_rtp_port,
            )
            if hasattr(self.server, "parent_server"):
                self.server.parent_server._register_media(self.media)  # type: ignore[attr-defined]
            self.media.start()
            print("[FluxCast WFD RTSP] PLAY accepted; media stream started.")
            self.play_accepted_at = time.monotonic()
            self.setup_ms = round((self.play_accepted_at - self.connected_at) * 1000.0, 1)
            _append_latency_log(
                self.media_config.latency_log_path,
                "play_accepted",
                setup_ms=self.setup_ms,
            )
            self._schedule_probe(0.7)

    def _schedule_probe(self, delay: float) -> None:
        probe = threading.Timer(delay, self._probe_tx)
        probe.daemon = True
        probe.start()

    def _schedule_rtsp_keepalive(self, delay: float = 25.0) -> None:
        """Schedule the next RTSP M16 GET_PARAMETER keepalive."""
        t = threading.Timer(delay, self._send_rtsp_keepalive)
        t.daemon = True
        t.start()

    def _send_rtsp_keepalive(self) -> None:
        """Send RTSP GET_PARAMETER (M16) on the existing TCP connection."""
        if not self._keepalive_active:
            return
        media = self.media
        # Only stop the chain if processes have already EXITED.
        # If media is None (portal dialog still open), keep sending keepalives.
        if media is not None and not all(p.poll() is None for p in media.processes):
            return
        try:
            self._send_request(
                "M16_KEEPALIVE",
                "GET_PARAMETER",
                self._rtsp_presentation_uri(),
                headers={"Session": f"{self.session_id};timeout=30"},
            )
            print("[FluxCast WFD RTSP] M16 keepalive sent")
            self._schedule_rtsp_keepalive(25.0)
        except OSError:
            pass  # Socket dead -> DONT RESCHEDULE

    def _probe_tx(self) -> None:
        media = self.media
        if media is None:
            return

        states = []
        for proc in media.processes:
            status = "running" if proc.poll() is None else f"exited={proc.returncode}"
            states.append(f"pid={proc.pid}:{status}")

        if states and all(proc.poll() is None for proc in media.processes):
            current = _netdev_tx_bytes(media.tx_interface)
            delta = None
            if media.tx_baseline is not None and current is not None:
                delta = max(0, current - media.tx_baseline)
            if (
                not self.first_tx_reported
                and delta is not None
                and delta > 0
                and self.play_accepted_at is not None
            ):
                self.first_tx_reported = True
                sender_startup_ms = round((time.monotonic() - self.play_accepted_at) * 1000.0, 1)
                print(
                    f"[FluxCast WFD Media] Latency probe: first RTP bytes after PLAY in "
                    f"{sender_startup_ms} ms"
                )
                sender_path_latency_ms = None
                if self.setup_ms is not None:
                    sender_path_latency_ms = round(self.setup_ms + sender_startup_ms, 1)
                    print(
                        "[FluxCast WFD Media] Latency probe: sender-path latency "
                        f"(RTSP connect -> first RTP) {sender_path_latency_ms} ms"
                    )
                _append_latency_log(
                    self.media_config.latency_log_path,
                    "latency_probe",
                    sender_startup_ms=sender_startup_ms,
                    setup_ms=self.setup_ms,
                    sender_path_latency_ms=sender_path_latency_ms,
                )
            print(
                f"[FluxCast WFD Media] Sender health: "
                f"{', '.join(states)}; {media.tx_summary()}"
            )
            _append_latency_log(
                self.media_config.latency_log_path,
                "sender_health",
                processes=states,
                tx_summary=media.tx_summary(),
            )
            self._schedule_probe(5.0)
            return

        detail = ", ".join(states) if states else "no sender process"
        print(
            f"[FluxCast WFD Media] WARNING: RTP sender is not healthy "
            f"({detail}; {media.tx_summary()})"
        )

    def _stop_media(self) -> None:
        if self.media is not None:
            print("[FluxCast WFD Media] Stopping RTP stream...")
            if hasattr(self.server, "parent_server"):
                self.server.parent_server._unregister_media(self.media)  # type: ignore[attr-defined]
            self.media.stop()
            self.media = None

    def _log_message(self, msg: RTSPMessage) -> None:
        arrow = "<- response" if msg.is_response else "<- request"
        print(f"[FluxCast WFD RTSP] {arrow}: {msg.start}")
        for line in msg.raw_headers:
            lower = line.lower()
            if lower.startswith(("cseq:", "transport:", "session:", "content-type:", "content-length:")):
                print(f"[FluxCast WFD RTSP]   {line}")
        if msg.body:
            for line in msg.body.splitlines():
                if line.startswith("wfd_"):
                    print(f"[FluxCast WFD RTSP]   {line}")


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class WFDRTSPServer:
    def __init__(
        self,
        media_config: WFDMediaConfig,
        host: str = "0.0.0.0",
        port: int = WFD_RTSP_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.media_config = media_config
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.has_connected_client = False
        self._media_lock = threading.Lock()
        self._active_media: list[WFDMediaPipeline] = []

    def _register_media(self, media: WFDMediaPipeline) -> None:
        with self._media_lock:
            self._active_media.append(media)

    def _unregister_media(self, media: WFDMediaPipeline) -> None:
        with self._media_lock:
            try:
                self._active_media.remove(media)
            except ValueError:
                pass

    def stop_all_media(self) -> None:
        with self._media_lock:
            pipelines = list(self._active_media)
        for pipeline in pipelines:
            pipeline.stop()

    def start(self) -> None:
        self._server = _ThreadingTCPServer((self.host, self.port), _WFDRTSPHandler)
        self._server.media_config = self.media_config  # type: ignore[attr-defined]
        self._server.parent_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[FluxCast WFD RTSP] Server listening on {self.host}:{self.port}")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _object_paths(text: str) -> list[str]:
    return re.findall(r"'(/[^']+)'", text)


def _variant_string(text: str) -> str:
    match = re.search(r"<\'(.*)\' >", text)
    if match:
        return match.group(1)
    match = re.search(r"<\'(.*)\'", text)
    if match:
        return match.group(1)
    match = re.search(r"<\"(.*)\"", text)
    if match:
        return match.group(1)
    return ""


def _variant_uint(text: str) -> Optional[int]:
    matches = re.findall(r"(?:uint32\s+)?(\d+)", text)
    if not matches:
        return None
    return int(matches[-1])


def _variant_uint_tuple(text: str) -> tuple[Optional[int], Optional[int]]:
    matches = re.findall(r"(?:uint32\s+)?(\d+)", text)
    if len(matches) < 2:
        return None, None
    return int(matches[-2]), int(matches[-1])


NM_ACTIVE_STATE_NAMES = {
    0: "unknown",
    1: "activating",
    2: "activated",
    3: "deactivating",
    4: "deactivated",
}

NM_DEVICE_STATE_NAMES = {
    0: "unknown",
    10: "unmanaged",
    20: "unavailable",
    30: "disconnected",
    40: "prepare",
    50: "config",
    60: "need-auth",
    70: "ip-config",
    80: "ip-check",
    90: "secondaries",
    100: "activated",
    110: "deactivating",
    120: "failed",
}

NM_DEVICE_REASON_NAMES = {
    0: "none",
    1: "unknown",
    2: "now-managed",
    3: "now-unmanaged",
    4: "config-failed",
    5: "ip-config-unavailable",
    6: "ip-config-expired",
    7: "no-secrets",
    8: "supplicant-disconnect",
    9: "supplicant-config-failed",
    10: "supplicant-failed",
    11: "supplicant-timeout",
    15: "dhcp-start-failed",
    16: "dhcp-error",
    17: "dhcp-failed",
    18: "shared-start-failed",
    19: "shared-failed",
    38: "external-disconnect",
    39: "assume-failed",
    40: "supplicant-available",
    41: "modem-not-found",
    42: "bt-failed",
    53: "peer-not-found",
    54: "device-handler-failed",
}


def _gdbus_call(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    if not shutil.which("gdbus"):
        raise WFDNotReady("gdbus is required for NetworkManager Wi-Fi P2P discovery.")
    return _run(["gdbus", "call", "--system", *args], timeout=timeout)


def _nm_get_property(path: str, interface: str, prop: str) -> str:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.DBus.Properties.Get",
        interface,
        prop,
    ])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _nm_get_string(path: str, interface: str, prop: str) -> str:
    return _variant_string(_nm_get_property(path, interface, prop))


def _nm_device_summary(path: str) -> str:
    iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface")
    ip_iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "IpInterface")
    state = _variant_uint(_nm_get_property(path, "org.freedesktop.NetworkManager.Device", "State"))
    _, reason = _variant_uint_tuple(
        _nm_get_property(path, "org.freedesktop.NetworkManager.Device", "StateReason")
    )
    state_text = NM_DEVICE_STATE_NAMES.get(state or -1, str(state))
    reason_text = NM_DEVICE_REASON_NAMES.get(reason or -1, str(reason))
    if ip_iface and ip_iface != iface:
        return f"{iface}/{ip_iface}:{state_text}:{reason_text}"
    return f"{iface}:{state_text}:{reason_text}"


def _nm_active_devices(active_path: str) -> list[str]:
    raw = _nm_get_property(
        active_path,
        "org.freedesktop.NetworkManager.Connection.Active",
        "Devices",
    )
    return _object_paths(raw)


def _wait_for_nm_activation(active_path: str, timeout: float = 35.0) -> None:
    print("[FluxCast WFD] Waiting for NetworkManager P2P activation...")
    deadline = time.monotonic() + timeout
    last_status = ""

    while time.monotonic() < deadline:
        state_raw = _nm_get_property(
            active_path,
            "org.freedesktop.NetworkManager.Connection.Active",
            "State",
        )
        state = _variant_uint(state_raw)
        state_text = NM_ACTIVE_STATE_NAMES.get(state or -1, str(state))
        devices = _nm_active_devices(active_path)
        device_status = ", ".join(_nm_device_summary(path) for path in devices) or "no-device"
        status = f"{state_text}; {device_status}"

        if status != last_status:
            print(f"[FluxCast WFD] NM active connection: {status}")
            last_status = status

        if state == 2:
            print("[FluxCast WFD] P2P link is activated; waiting for RTSP session...")
            return
        if state == 4:
            raise WFDNotReady(
                "NetworkManager deactivated the Wi-Fi Direct connection before RTSP. "
                f"Last status: {status}"
            )

        time.sleep(0.5)

    raise WFDNotReady(
        "Timed out waiting for NetworkManager Wi-Fi Direct activation. "
        f"Last status: {last_status or 'unknown'}"
    )


def _nm_p2p_device_path(interface: Optional[str] = None) -> Optional[str]:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.DBus.Properties.Get",
        "org.freedesktop.NetworkManager",
        "Devices",
    ])
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())

    requested = interface or ""
    for path in _object_paths(result.stdout):
        iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface")
        if not iface or "p2p" not in iface.lower():
            continue
        if requested and requested not in iface:
            continue
        return path
    return None


def _nm_start_find(path: str, timeout: int) -> None:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.NetworkManager.Device.WifiP2P.StartFind",
        f"{{'timeout': <int32 {timeout}>}}",
    ])
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())


def _nm_stop_find(path: str) -> None:
    try:
        _gdbus_call([
            "--dest", NM_DEST,
            "--object-path", path,
            "--method", "org.freedesktop.NetworkManager.Device.WifiP2P.StopFind",
        ], timeout=3.0)
    except Exception:
        pass


def _nm_scan(interface: Optional[str], timeout: int) -> list[WFDPeer]:
    path = _nm_p2p_device_path(interface)
    if not path:
        raise WFDNotReady("NetworkManager did not expose a Wi-Fi P2P device.")

    iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface") or path
    print(f"[FluxCast WFD] Starting NetworkManager Wi-Fi Direct scan on {iface} for {timeout}s...")
    _nm_start_find(path, timeout)
    try:
        time.sleep(max(1, timeout))
        peers_raw = _nm_get_property(path, "org.freedesktop.NetworkManager.Device.WifiP2P", "Peers")
    finally:
        _nm_stop_find(path)

    peers = []
    for peer_path in _object_paths(peers_raw):
        name = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Name")
        address = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "HwAddress")
        model = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Model")
        manufacturer = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Manufacturer")
        wfd_ies_raw = _nm_get_property(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "WfdIEs")
        
        # wfd_ies_raw is the raw gdbus stdout string (e.g. "<@ay [byte 0x00, ...]>").
        # Parse it into a byte list, then extract the RTSP port from subelement 0.
        wfd_ies_list = _parse_gdbus_byte_array(wfd_ies_raw)
        sink_rtsp_port = _parse_wfd_ies_rtsp_port(wfd_ies_list)

        details = "; ".join(
            part for part in [
                f"model={model}" if model else "",
                f"manufacturer={manufacturer}" if manufacturer else "",
                f"wfd_ies={wfd_ies_raw}" if wfd_ies_raw else "",
                f"sink_rtsp_port={sink_rtsp_port}",
            ]
            if part
        )
        peers.append(WFDPeer(
            address=address or peer_path.rsplit("/", 1)[-1],
            name=name,
            details=details,
            path=peer_path,
            source="NetworkManager",
            rtsp_port=sink_rtsp_port,
        ))
    return peers


def _variant_byte_array(data: bytes) -> str:
    return "@ay [" + ", ".join(f"byte 0x{byte:02x}" for byte in data) + "]"


def _wfd_source_ie(rtsp_port: int) -> bytes:
    if rtsp_port <= 0 or rtsp_port > 65535:
        raise WFDNotReady(f"Invalid WFD RTSP port: {rtsp_port}")
    # Build WFD IE with Device Info and Device Name subelements.
    # I use a static name "FluxCast" to ensure consistency during P2P negotiation.
    # Some TVs like LG might be sensitive to hostname changes or special characters.
    return _wfd_ie_device_info(rtsp_port) + _wfd_ie_device_name("FluxCast")


def _connection_settings(peer: WFDPeer, rtsp_port: int) -> str:
    peer_address = peer.address
    return (
        "{"
        "'connection': {"
        "'id': <'FluxCast WFD'>, "
        "'type': <'wifi-p2p'>, "
        "'autoconnect': <false>"
        "}, "
        "'wifi-p2p': {"
        f"'peer': <'{peer_address}'>, "
        f"'wfd-ies': <{_variant_byte_array(_wfd_source_ie(rtsp_port))}>"
        "}, "
        "'ipv4': {'method': <'auto'>, 'never-default': <true>}, "
        "'ipv6': {'method': <'auto'>, 'never-default': <true>, 'may-fail': <true>}"
        "}"
    )


def _connect_peer(
    device_path: str,
    peer: WFDPeer,
    rtsp_port: int = WFD_RTSP_PORT,
    dry_run: bool = False,
) -> str:
    if not peer.path:
        raise WFDNotReady("NetworkManager peer object path is required for P2P connection.")

    settings = _connection_settings(peer, rtsp_port)
    # !!!Do not use bind-activation here!!!: the gdbus CLI process exits right after
    # the method call, and NetworkManager would tear the P2P link down with it.
    options = "{'persist': <'volatile'>}"
    args = [
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.NetworkManager.AddAndActivateConnection2",
        settings,
        device_path,
        peer.path,
        options,
    ]
    if dry_run:
        print("[FluxCast WFD] Dry-run AddAndActivateConnection2:")
        print("gdbus call --system " + " ".join(args))
        return "/"

    print(f"[FluxCast WFD] Connecting to {peer.name or peer.address} via NetworkManager...")
    result = _gdbus_call(args, timeout=30.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise WFDNotReady(text)

    paths = _object_paths(text)
    active = paths[-1] if paths else "/"
    print(f"[FluxCast WFD] NetworkManager activation started: {text}")
    return active


def _disconnect_device(device_path: str) -> None:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", device_path,
        "--method", "org.freedesktop.NetworkManager.Device.Disconnect",
    ], timeout=10.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        print("[FluxCast WFD] NetworkManager P2P device disconnected.")
    elif text and "Device.NotActive" not in text:
        print(f"[FluxCast WFD] NetworkManager disconnect warning: {text}")


def _deactivate_connection(active_path: str) -> None:
    if not active_path or active_path == "/":
        return
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.NetworkManager.DeactivateConnection",
        active_path,
    ], timeout=10.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        print("[FluxCast WFD] NetworkManager P2P connection deactivated.")
    elif text:
        print(f"[FluxCast WFD] NetworkManager deactivate warning: {text}")


def _select_peer(peers: list[WFDPeer], selector: Optional[str]) -> WFDPeer:
    if not peers:
        raise WFDNotReady("No Wi-Fi Direct peers found. Put the TV into Screen Share/Wireless Display mode.")
    if selector is None:
        print_scan(peers)
        try:
            raw = input("Select WFD peer [0]: ").strip()
        except EOFError:
            raw = ""
        selector = raw or "0"

    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(peers):
            return peers[index]
        raise WFDNotReady(f"Peer index out of range: {selector}")

    normalized = selector.lower()
    for peer in peers:
        if normalized in peer.address.lower() or normalized in peer.name.lower():
            return peer
    raise WFDNotReady(f"No peer matched selector: {selector}")


def _default_wifi_interface() -> Optional[str]:
    if not shutil.which("iw"):
        return None

    try:
        result = _run(["iw", "dev"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return None

    current_iface = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            current_iface = stripped.split(maxsplit=1)[1]
        elif stripped == "type managed" and current_iface:
            return current_iface
    return None


def _parse_peer_name(details: str) -> str:
    for line in details.splitlines():
        stripped = line.strip()
        if stripped.startswith("device_name="):
            return stripped.partition("=")[2]
    return ""


def active_scan(interface: Optional[str] = None, timeout: int = 8) -> list[WFDPeer]:
    """Run an active Wi-Fi Direct peer scan.
    """
    try:
        return _nm_scan(interface=interface, timeout=timeout)
    except WFDNotReady as nm_error:
        print(f"[FluxCast WFD] NetworkManager scan unavailable: {nm_error}")

    if not shutil.which("wpa_cli"):
        raise WFDNotReady("wpa_cli is required for active Wi-Fi Direct scans.")

    iface = interface or _default_wifi_interface()
    if not iface:
        raise WFDNotReady("Could not detect a managed Wi-Fi interface for wpa_cli.")

    print(f"[FluxCast WFD] Starting Wi-Fi Direct scan on {iface} for {timeout}s...")
    try:
        start = _run(["wpa_cli", "-i", iface, "p2p_find", str(timeout)], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WFDNotReady(f"Could not start p2p_find: {exc}") from exc
    if start.returncode != 0:
        error = (start.stderr or start.stdout).strip()
        if "Permission denied" in error:
            raise WFDNotReady(
                "wpa_cli cannot access the supplicant control interface. "
                "This usually needs root, a ctrl_interface group, or a "
                "NetworkManager D-Bus connection path. Raw error: " + error
            )
        raise WFDNotReady(error)

    time.sleep(max(1, timeout))

    try:
        peers_result = _run(["wpa_cli", "-i", iface, "p2p_peers"], timeout=5.0)
    finally:
        try:
            _run(["wpa_cli", "-i", iface, "p2p_stop_find"], timeout=3.0)
        except Exception:
            pass

    if peers_result.returncode != 0:
        raise WFDNotReady((peers_result.stderr or peers_result.stdout).strip())

    peers = []
    for raw in peers_result.stdout.splitlines():
        address = raw.strip()
        if not re.fullmatch(r"[0-9a-fA-F:]{17}", address):
            continue

        details = ""
        try:
            details_result = _run(["wpa_cli", "-i", iface, "p2p_peer", address], timeout=5.0)
            if details_result.returncode == 0:
                details = details_result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        peers.append(WFDPeer(
            address=address,
            name=_parse_peer_name(details),
            details=details,
            source="wpa_cli",
        ))

    return peers


def print_scan(peers: list[WFDPeer]) -> None:
    if not peers:
        print("[FluxCast WFD] No Wi-Fi Direct peers found.")
        return

    print("[FluxCast WFD] Wi-Fi Direct peer(s):")
    for idx, peer in enumerate(peers):
        name = f"  {peer.name}" if peer.name else ""
        source = f" via {peer.source}" if peer.source else ""
        print(f"  [{idx}] {peer.address}{name}{source}")
        if "wfd_subelems" in peer.details or "wfd_dev_info" in peer.details:
            print("      WFD capability data detected")
        elif "wfd_ies=" in peer.details:
            print("      WFD capability data detected")


def _get_peer_ip_from_arp(peer_mac: str) -> Optional[str]:
    """Return the IP for peer_mac from the kernel ARP/neighbour table."""
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        mac = peer_mac.lower().replace("-", ":")
        for line in result.stdout.splitlines():
            if mac in line.lower():
                parts = line.split()
                if parts and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                    return parts[0]
    except Exception:
        pass
    return None


def _wait_for_peer_ip(peer_mac: str, timeout: float = 12.0) -> Optional[str]:
    """Poll ARP until the peer's IP appears (DHCP may take a few seconds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ip = _get_peer_ip_from_arp(peer_mac)
        if ip:
            return ip
        time.sleep(0.75)
    return None


def _active_rtsp_probe(
    rtsp_server: WFDRTSPServer,
    peer: WFDPeer,
    media_config: WFDMediaConfig,
) -> None:
    # 7 s: P2P group up + DHCP (~3 s) + TV RTSP-initiation attempt (~3-4 s).
    time.sleep(7.0)
    if rtsp_server.has_connected_client:
        return

    print("[FluxCast WFD RTSP] No passive connection; trying Source-initiated RTSP probe...")

    tv_ip = _wait_for_peer_ip(peer.address, timeout=10.0)
    if not tv_ip:
        print(
            f"[FluxCast WFD RTSP] Active probe: TV IP not found for MAC {peer.address} "
            "— ARP table empty; is the P2P link still up?"
        )
        return

    tv_port = peer.rtsp_port if 0 < peer.rtsp_port <= 65535 else 7236
    print(f"[FluxCast WFD RTSP] Active probe: TV={tv_ip}; connecting to RTSP port {tv_port}...")

    try:
        sock = socket.create_connection((tv_ip, tv_port), timeout=5.0)
    except ConnectionRefusedError:
        print(
            f"[FluxCast WFD RTSP] Active probe: TV port {tv_port} refused "
            "— Sink-only device; waiting for its passive connection to us."
        )
        return
    except OSError as exc:
        print(f"[FluxCast WFD RTSP] Active probe: connect error: {exc}")
        return

    print(f"[FluxCast WFD RTSP] Active probe: connected to TV RTSP at {tv_ip}:{tv_port}")
    sock.settimeout(8.0)
    rfile = sock.makefile("rb")
    wfile = sock.makefile("wb")
    local_ip: str = sock.getsockname()[0]
    local_uri = f"rtsp://{local_ip}:{rtsp_server.port}/wfd1.0"
    session_id = str(random.randint(1_000_000, 9_999_999))

    # Mutable session state shared by the nested helpers below.
    st: dict = {
        "cseq": 1,
        "pending": {},
        "sink_rtp_port": 0,
        "sink_rtcp_port": 0,
        "src_port": media_config.source_port,
        "sink_vfmt": None,
        "no_audio": media_config.no_audio,
    }

    def _send(name: str, method: str, uri: str, hdrs: Optional[dict] = None, body: str = "") -> None:
        cseq = str(st["cseq"])
        st["cseq"] += 1
        st["pending"][cseq] = name
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}"]
        for k, v in (hdrs or {}).items():
            lines.append(f"{k}: {v}")
        if body:
            lines += ["Content-Type: text/parameters", f"Content-Length: {len(body.encode())}"]
        lines += ["", body]
        wfile.write("\r\n".join(lines).encode())
        wfile.flush()
        print(f"[FluxCast WFD RTSP] Active probe -> {name}")

    def _reply(msg: RTSPMessage, status: str = "200 OK", extra: Optional[dict] = None, body: str = "") -> None:
        lines = [f"RTSP/1.0 {status}", f"CSeq: {msg.cseq}", f"Session: {session_id};timeout=30"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        lines += [f"Content-Length: {len(body.encode())}", "", body]
        wfile.write("\r\n".join(lines).encode())
        wfile.flush()

    media: Optional[WFDMediaPipeline] = None
    try:
        with sock:
            _send("M1_OPTIONS", "OPTIONS", "*", {"Require": "org.wfa.wfd1.0"})
            while True:
                msg = _read_rtsp_message(rfile)
                if msg is None:
                    print("[FluxCast WFD RTSP] Active probe: TV closed connection.")
                    break

                if msg.is_response:
                    name = st["pending"].pop(msg.cseq, "UNKNOWN")
                    if not msg.status.startswith("200"):
                        print(f"[FluxCast WFD RTSP] Active probe: {name} failed: {msg.status}")
                        break
                    print(f"[FluxCast WFD RTSP] Active probe <- OK for {name}")

                    if name == "M1_OPTIONS":
                        _send("M3_GET_PARAMETER", "GET_PARAMETER", local_uri,
                              body="wfd_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n")

                    elif name == "M3_GET_PARAMETER":
                        params = _parse_parameters(msg.body)
                        ports = _parse_rtp_ports(params.get("wfd_client_rtp_ports", ""))
                        if not ports or ports[0] <= 0:
                            print("[FluxCast WFD RTSP] Active probe: no valid RTP ports in M3.")
                            break
                        st["sink_rtp_port"], st["sink_rtcp_port"] = ports
                        st["sink_vfmt"] = _parse_sink_video_format(params.get("wfd_video_formats", ""))
                        audio = params.get("wfd_audio_codecs", "")
                        if audio and not media_config.no_audio and "AAC" not in audio.upper():
                            st["no_audio"] = True
                        st["src_port"] = _safe_source_port(
                            media_config.source_port, st["sink_rtp_port"], st["sink_rtcp_port"])
                        vfmt = _selected_video_format(media_config, st["sink_vfmt"])
                        afmt = "none" if st["no_audio"] else WFD_AUDIO_AAC
                        rtcp = st["sink_rtcp_port"] if st["sink_rtcp_port"] > 0 else 0
                        m4 = (
                            f"wfd_video_formats: {vfmt}\r\n"
                            f"wfd_audio_codecs: {afmt}\r\n"
                            f"wfd_presentation_URL: {local_uri}/streamid=0 none\r\n"
                            f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast "
                            f"{st['sink_rtp_port']} {rtcp} mode=play\r\n"
                        )
                        _send("M4_SET_PARAMETER", "SET_PARAMETER", local_uri, body=m4)

                    elif name == "M4_SET_PARAMETER":
                        _send("M5_TRIGGER_SETUP", "SET_PARAMETER", local_uri,
                              body="wfd_trigger_method: SETUP\r\n")

                    elif name == "M5_TRIGGER_SETUP":
                        print("[FluxCast WFD RTSP] Active probe: M5 sent — awaiting TV SETUP...")
                        # TV should now send SETUP on this same TCP connection.

                else:
                    method = msg.method
                    print(f"[FluxCast WFD RTSP] Active probe <- TV request: {method}")

                    if method in ("GET_PARAMETER", "SET_PARAMETER"):
                        _reply(msg)

                    elif method == "OPTIONS":
                        _reply(msg, extra={
                            "Public": (
                                "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, "
                                "GET_PARAMETER, SET_PARAMETER"
                            )
                        })

                    elif method == "SETUP":
                        ports = _parse_transport_client_ports(msg.headers.get("transport", ""))
                        if ports:
                            st["sink_rtp_port"], st["sink_rtcp_port"] = ports
                        if not st["sink_rtp_port"]:
                            st["sink_rtp_port"] = 19000
                        st["src_port"] = _safe_source_port(
                            media_config.source_port, st["sink_rtp_port"], st["sink_rtcp_port"])
                        sp = st["src_port"]
                        sr = st["sink_rtp_port"]
                        sc = st["sink_rtcp_port"]
                        transport = (
                            f"RTP/AVP/UDP;unicast;client_port={sr}-{sc};server_port={sp}-{sp + 1}"
                            if sc else
                            f"RTP/AVP/UDP;unicast;client_port={sr};server_port={sp}"
                        )
                        _reply(msg, extra={
                            "Transport": transport,
                            "Session": f"{session_id};timeout=30",
                        })
                        print(f"[FluxCast WFD RTSP] Active probe: SETUP OK; sink RTP={sr}")

                    elif method == "PLAY":
                        _reply(msg, extra={"Range": "npt=now-"})
                        mode = _choose_cea_mode(media_config, st["sink_vfmt"])
                        eff_cfg = replace(
                            media_config,
                            source_port=st["src_port"],
                            output_resolution=mode.resolution,
                            fps=mode.fps,
                            no_audio=st["no_audio"],
                        )
                        media = WFDMediaPipeline(
                            eff_cfg,
                            tv_ip=tv_ip,
                            local_ip=local_ip,
                            sink_rtp_port=st["sink_rtp_port"],
                        )
                        rtsp_server.has_connected_client = True
                        print(
                            f"[FluxCast WFD RTSP] Active probe: PLAY — "
                            f"starting media ({mode.name})"
                        )
                        media.start()
                        # Keep-alive: respond to GET_PARAMETER/SET_PARAMETER heartbeats.
                        while True:
                            ka = _read_rtsp_message(rfile)
                            if ka is None:
                                break
                            if ka.method in ("GET_PARAMETER", "SET_PARAMETER"):
                                _reply(ka)
                            elif ka.method == "TEARDOWN":
                                _reply(ka, extra={"Connection": "close"})
                                break
                        return

                    elif method == "TEARDOWN":
                        _reply(msg, extra={"Connection": "close"})
                        break

                    else:
                        _reply(msg, status="405 Method Not Allowed")

    except OSError as exc:
        print(f"[FluxCast WFD RTSP] Active probe: I/O error: {exc}")
    finally:
        if media is not None:
            media.stop()
    print("[FluxCast WFD RTSP] Active probe: session ended.")




def start_experimental_backend(args) -> None:
    report = run_diagnostics()
    print_report(report)
    print()

    if not report.wfd_candidate:
        raise WFDNotReady(
            "Miracast/WFD is not ready on this machine yet. "
            "Fix the warn/fail rows above, then run --wfd-scan."
        )

    monitor = None
    if not getattr(args, "wfd_test_pattern", False) and not getattr(args, "wfd_dry_run", False):
        selected_backend = getattr(args, "wfd_capture_backend", "auto")
        portal_mode = selected_backend == "portal" or (
            selected_backend == "auto" and _is_wayland_session() and not _is_hyprland_session()
        )
        if portal_mode:
            print(
                "[FluxCast WFD] Portal backend: monitor selection will be done "
                "in the desktop portal dialog."
            )
        else:
            wfd_monitor_name = getattr(args, "wfd_monitor", None)
            if wfd_monitor_name:
                from capture import gather_monitors
                all_monitors = gather_monitors()
                monitor = next((m for m in all_monitors if m.name == wfd_monitor_name), None)
                if monitor is None:
                    available = ", ".join(m.name for m in all_monitors) or "none"
                    raise WFDNotReady(
                        f"Monitor '{wfd_monitor_name}' not found. Available: {available}"
                    )
            else:
                from capture import prompt_monitor
                monitor = prompt_monitor()

    peers = active_scan(interface=args.wfd_interface, timeout=args.wfd_timeout)
    peer = _select_peer(peers, getattr(args, "wfd_peer", None))
    device_path = _nm_p2p_device_path(args.wfd_interface)
    if not device_path:
        raise WFDNotReady("NetworkManager P2P device disappeared before connection.")

    if getattr(args, "wfd_dry_run", False):
        _connect_peer(
            device_path,
            peer,
            rtsp_port=getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT),
            dry_run=True,
        )
        return

    no_audio = getattr(args, "wfd_no_audio", False)
    if getattr(args, "wfd_test_pattern", False):
        if no_audio:
            print("[FluxCast WFD] Test pattern smoke mode is video-only (--wfd-no-audio).")
        else:
            print("[FluxCast WFD] Test pattern smoke mode includes AAC audio.")

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=args.output_res,
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=no_audio,
        test_pattern=getattr(args, "wfd_test_pattern", False),
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        media_pipeline=getattr(args, "wfd_media_pipeline", "auto"),
        latency_log_path=getattr(args, "wfd_latency_log", None),
        capture_backend=getattr(args, "wfd_capture_backend", "auto"),
        peer_name=peer.name,
    )
    if media_config.latency_log_path:
        print(f"[FluxCast WFD] Latency log file: {media_config.latency_log_path}")

    rtsp = WFDRTSPServer(
        media_config=media_config,
        port=getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT),
    )
    connected = False
    active_path = ""
    try:
        # Clear stale P2P device state from previous runs before new activation.
        try:
            _disconnect_device(device_path)
        except Exception:
            pass
        rtsp.start()
        active_path = _connect_peer(
            device_path,
            peer,
            rtsp_port=getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT),
        )
        connected = True
        _wait_for_nm_activation(active_path)

        # Active probe for newer TVs (Samsung 2024++, some LGs)
        # It runs in a background thread to not block the main loop.
        probe_thread = threading.Thread(
            target=_active_rtsp_probe,
            args=(rtsp, peer, media_config),
            daemon=True,
        )
        probe_thread.start()

        print("[FluxCast WFD] Waiting for TV RTSP/WFD session. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[FluxCast WFD] Stopping WFD session...")
    finally:
        rtsp.stop_all_media()
        rtsp.stop()
        if connected:
            _deactivate_connection(active_path)
            _disconnect_device(device_path)
