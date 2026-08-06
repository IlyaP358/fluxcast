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
    # For LG, use 0.5x bitrate (500ms buffer); for Samsung/others use 2x.
    # Tighter values cause VBV underflow with ultrafast at high resolutions.
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
    if pixels <= 640 * 480:
        return 2000 if fps <= 30 else 3000
    if pixels <= 1280 * 720:
        return 5000 if fps <= 30 else 7000
    if pixels <= 1920 * 1080:
        return 8000 if fps <= 30 else 14000
    # 1200p+ at ultrafast needs headroom to avoid compression artifacts
    return 14000 if fps <= 30 else 20000

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
