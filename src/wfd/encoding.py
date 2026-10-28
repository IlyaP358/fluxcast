import re
from typing import Optional

from .config import WFDMediaConfig


def _parse_resolution(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))

def _fit_inside(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int]:
    """Fit src's aspect ratio inside out_w x out_h. Width stays a multiple of 8
    so GStreamer leaves the I420 strides unpadded and a raw frame is exactly
    width*height*3/2 bytes, which is what ffmpeg's rawvideo assumes (#84)."""
    if src_w <= 0 or src_h <= 0:
        src_w, src_h = out_w, out_h
    scale = min(out_w / src_w, out_h / src_h)
    width = max(8, int(src_w * scale) // 8 * 8)
    height = max(2, min(out_h, int(width * src_h / src_w)) & ~1)
    return width, height

def _letterbox_vf(out_res: str) -> str:
    """ffmpeg filter that fits the source into out_res without stretching."""
    width, height = out_res.split("x")
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease:out_range=tv,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")

def latency_first_vbv_multiplier(
    encode_mbps: float,
    air_mbps: float | None = None,
) -> float:
    """HRD window in seconds. Input lag over QVBR undershoot.

    Intel low-delay AVC is 0.25 s (bufsize = bitrate/4). A 2.0 s buffer
    is quality-first and shows up as pointer lag. If air capacity is
    known and headroom is tight, use 0.5 s — still interactive.
    """
    enc = max(0.0, float(encode_mbps or 0.0))
    if air_mbps is not None and float(air_mbps) > 0 and enc > 0:
        headroom = float(air_mbps) / enc
        if headroom < 2.0:
            return 0.5
        if headroom < 4.0:
            return 0.35
        return 0.25
    if enc >= 2.0:
        return 0.25
    return 0.5


def _vbv_bufsize(bitrate_text: str, config: WFDMediaConfig) -> str:
    """Calculate VBV buffer size.

    Default: latency-first (~0.25 s at typical WFD bitrates). Override
    with ``FLUXCAST_WFD_VBV_MULTIPLIER``. ``FLUXCAST_WFD_AIR_MBPS``
    tightens the window when P2P air capacity is known.
    """
    import os

    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)([kKmMgG]?)\s*", bitrate_text)
    if not match:
        return bitrate_text

    amount = float(match.group(1))
    suffix = match.group(2)

    env_mult = (os.environ.get("FLUXCAST_WFD_VBV_MULTIPLIER", "") or "").strip()
    if env_mult:
        try:
            multiplier = max(0.05, float(env_mult))
        except ValueError:
            multiplier = 0.25
    else:
        air = None
        raw_air = (os.environ.get("FLUXCAST_WFD_AIR_MBPS", "") or "").strip()
        if raw_air:
            try:
                air = float(raw_air)
            except ValueError:
                air = None
        enc_mbps = _bitrate_to_kbits(bitrate_text) / 1000.0
        multiplier = latency_first_vbv_multiplier(enc_mbps, air)

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

def parse_iw_station_air_mbps(dump: str) -> float | None:
    """P2P air estimate from ``iw station dump`` (expected/TX, retry-adjusted)."""
    expected = tx = None
    m = re.search(r"expected throughput:\s*([\d.]+)", dump or "", re.I)
    if m:
        expected = float(m.group(1))
    m = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit", dump or "", re.I)
    if m:
        tx = float(m.group(1))
    pkts = retries = 0
    m = re.search(r"tx packets:\s*(\d+)", dump or "")
    if m:
        pkts = int(m.group(1))
    m = re.search(r"tx retries:\s*(\d+)", dump or "")
    if m:
        retries = int(m.group(1))
    denom = pkts + retries
    retry_frac = (retries / denom) if denom else 0.0
    phy = None
    for v in (expected, tx):
        if v is not None and v > 0:
            phy = v if phy is None else min(phy, v)
    if phy is None:
        return None
    return phy * (1.0 - min(0.5, retry_frac))


def cap_kbits_to_link(
    requested_kbits: int,
    air_mbps: float | None,
    *,
    audio_kbps: int = 0,
    payload_frac: float = 0.45,
) -> int:
    """Cap video kbps to a fraction of measured air minus audio.

    ``payload_frac`` leaves 802.11 header/retry room. Not a chip or TV table.
    """
    req = max(1, int(requested_kbits))
    if air_mbps is None or float(air_mbps) <= 0:
        return req
    room = float(air_mbps) * 1000.0 * float(payload_frac) - float(audio_kbps)
    return min(req, max(400, int(room)))


def should_honor_idr_request(
    last_ts: float,
    now: float,
    min_interval_s: float = 1.0,
) -> bool:
    """Honor at most one WFD IDR request per GOP (~1s).

    Sinks re-request IDR while the decoder is starved. Honoring every
    SET_PARAMETER floods the air with I-frames and keeps the picture dead.
    """
    if last_ts <= 0:
        return True
    return (float(now) - float(last_ts)) >= float(min_interval_s)


def link_allows_lpcm(
    air_mbps: float | None,
    video_kbits: int,
    *,
    lpcm_kbps: int = 1536,
    payload_frac: float = 0.45,
    go_mhz: int | None = None,
    radio_can_5ghz: bool | None = None,
) -> bool:
    """True if P2P air can carry requested video plus uncompressed LPCM.

    LPCM has no concealment. A 5 GHz GO on a phy that cannot carry 5 GHz
    WFD (1ss / weak EIRP) will starve the decoder even when a snapshot
    ``air_mbps`` looks large enough.
    """
    if go_mhz is not None and int(go_mhz) >= 5000 and radio_can_5ghz is False:
        return False
    if air_mbps is None or float(air_mbps) <= 0:
        return True
    need = (float(video_kbits) + float(lpcm_kbps)) / 1000.0
    return float(air_mbps) * float(payload_frac) >= need


def _go_ifaces(iface: str | None = None) -> list[str]:
    ifaces: list[str] = []
    if iface:
        ifaces.append(iface)
    try:
        from pathlib import Path
        for p in sorted(Path("/sys/class/net").glob("p2p-*")):
            if p.name not in ifaces:
                ifaces.append(p.name)
    except OSError:
        pass
    return ifaces


def read_go_oper_mhz(iface: str | None = None) -> int | None:
    """Operating MHz of a live P2P-GO, from ``iw dev info``."""
    import subprocess
    import re

    for name in _go_ifaces(iface):
        try:
            r = subprocess.run(
                ["iw", "dev", name, "info"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = r.stdout or ""
        if "type P2P-GO" not in text:
            continue
        m = re.search(r"channel\s+\d+\s+\((\d+)\s+MHz\)", text)
        if m:
            return int(m.group(1))
    return None


def read_go_radio_can_5ghz(iface: str | None = None) -> bool | None:
    """Whether the GO's phy can carry 5 GHz WFD payload."""
    from .p2p.formation import parse_iw_phy_radio, radio_can_stream_5ghz
    import re
    import subprocess

    for name in _go_ifaces(iface):
        try:
            r = subprocess.run(
                ["iw", "dev", name, "info"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = r.stdout or ""
        if "type P2P-GO" not in text:
            continue
        m = re.search(r"wiphy\s+(\d+)", text)
        if not m:
            return None
        try:
            pt = subprocess.run(
                ["iw", "phy", f"phy{m.group(1)}", "info"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return bool(radio_can_stream_5ghz(parse_iw_phy_radio(pt.stdout or "")))
    return None


def read_go_air_mbps(iface: str | None = None) -> float | None:
    """Live GO air from env or ``iw station dump`` on a P2P-GO iface."""
    import os
    import subprocess

    raw = (os.environ.get("FLUXCAST_WFD_AIR_MBPS") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    for name in _go_ifaces(iface):
        try:
            r = subprocess.run(
                ["iw", "dev", name, "station", "dump"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        air = parse_iw_station_air_mbps(r.stdout or "")
        if air:
            return air
    return None


def _quality_floor_kbits(width: int, height: int, fps: int) -> int:
    """
    Conservative quality floors for desktop readability at low latency.
    """
    pixels = width * height
    if pixels <= 640 * 480:
        return 2000 if fps <= 30 else 3000
    if pixels <= 1280 * 720:
        return 5000 if fps <= 30 else 7000
    if pixels <= 1920 * 1080:
        return 8000 if fps <= 30 else 14000
    # 1200p+ at ultrafast needs headroom to avoid compression artifacts
    return 14000 if fps <= 30 else 20000

def _calculate_gop(config: WFDMediaConfig) -> int:
    """IDR interval ≈ 1 second (fps frames). 1080p60 → GOP 60."""
    return max(1, int(config.fps or 30))
