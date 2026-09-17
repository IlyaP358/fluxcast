import os
from typing import Optional

from .config import WFDCEAMode, WFDMediaConfig, WFDVideoFormat
from .constants import (
    WFD_CEA_640P60,
    WFD_CEA_480P60,
    WFD_CEA_480I60,
    WFD_CEA_576P50,
    WFD_CEA_576I50,
    WFD_CEA_720P30,
    WFD_CEA_720P60,
    WFD_CEA_1080P30,
    WFD_CEA_1080P60,
    WFD_CEA_1080I60,
    WFD_CEA_720P25,
    WFD_CEA_720P50,
    WFD_CEA_1080P25,
    WFD_CEA_1080P50,
    WFD_CEA_1080I50,
    WFD_CEA_720P24,
    WFD_CEA_1080P24,
    WFD_VESA_1200P30,
    WFD_VESA_1200P60,
    WFD_LEVEL_31,
    WFD_LEVEL_32,
    WFD_LEVEL_40,
    WFD_LEVEL_42,
    WFD_LEVEL_50,
    WFD_LEVEL_51,
)
from .encoding import _parse_resolution


def _cea_native(index: int) -> str:
    """WFD ``native`` byte for CEA table row ``index``: ``(index << 3) | 0``."""
    return f"{(index << 3) & 0xFF:02x}"


def _cea_mode(
    name: str,
    bit: int,
    index: int,
    width: int,
    height: int,
    fps: int,
    *,
    interlaced: bool = False,
) -> WFDCEAMode:
    return WFDCEAMode(
        name,
        bit,
        _cea_native(index),
        width,
        height,
        fps,
        table="cea",
        interlaced=interlaced,
    )


# Classic WFD CEA table (bits 0–16). Interlaced entries are listed for
# capability / UI but never selected in M4 (see ``_choose_cea_mode``).
WFD_CEA_MODES: dict[int, WFDCEAMode] = {
    WFD_CEA_640P60: _cea_mode("640x480p60", WFD_CEA_640P60, 0, 640, 480, 60),
    WFD_CEA_480P60: _cea_mode("720x480p60", WFD_CEA_480P60, 1, 720, 480, 60),
    WFD_CEA_480I60: _cea_mode(
        "720x480i60", WFD_CEA_480I60, 2, 720, 480, 60, interlaced=True
    ),
    WFD_CEA_576P50: _cea_mode("720x576p50", WFD_CEA_576P50, 3, 720, 576, 50),
    WFD_CEA_576I50: _cea_mode(
        "720x576i50", WFD_CEA_576I50, 4, 720, 576, 50, interlaced=True
    ),
    WFD_CEA_720P30: _cea_mode("1280x720p30", WFD_CEA_720P30, 5, 1280, 720, 30),
    WFD_CEA_720P60: _cea_mode("1280x720p60", WFD_CEA_720P60, 6, 1280, 720, 60),
    WFD_CEA_1080P30: _cea_mode("1920x1080p30", WFD_CEA_1080P30, 7, 1920, 1080, 30),
    WFD_CEA_1080P60: _cea_mode("1920x1080p60", WFD_CEA_1080P60, 8, 1920, 1080, 60),
    WFD_CEA_1080I60: _cea_mode(
        "1920x1080i60", WFD_CEA_1080I60, 9, 1920, 1080, 60, interlaced=True
    ),
    WFD_CEA_720P25: _cea_mode("1280x720p25", WFD_CEA_720P25, 10, 1280, 720, 25),
    WFD_CEA_720P50: _cea_mode("1280x720p50", WFD_CEA_720P50, 11, 1280, 720, 50),
    WFD_CEA_1080P25: _cea_mode("1920x1080p25", WFD_CEA_1080P25, 12, 1920, 1080, 25),
    WFD_CEA_1080P50: _cea_mode("1920x1080p50", WFD_CEA_1080P50, 13, 1920, 1080, 50),
    WFD_CEA_1080I50: _cea_mode(
        "1920x1080i50", WFD_CEA_1080I50, 14, 1920, 1080, 50, interlaced=True
    ),
    WFD_CEA_720P24: _cea_mode("1280x720p24", WFD_CEA_720P24, 15, 1280, 720, 24),
    WFD_CEA_1080P24: _cea_mode("1920x1080p24", WFD_CEA_1080P24, 16, 1920, 1080, 24),
}

WFD_VESA_MODES: dict[int, WFDCEAMode] = {
    WFD_VESA_1200P30: WFDCEAMode(
        "1920x1200p30", WFD_VESA_1200P30, "00", 1920, 1200, 30, table="vesa"
    ),
    WFD_VESA_1200P60: WFDCEAMode(
        "1920x1200p60", WFD_VESA_1200P60, "00", 1920, 1200, 60, table="vesa"
    ),
}


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
    """Pick WFD H.264 profile byte for M4 from the sink advertisement.

    Miracast ``wfd_video_formats`` profile field is a bitmask:
      ``0x01`` — Constrained Baseline (CBP)
      ``0x02`` — Constrained High (CHP)

    Prefer CHP when the sink offers it (better CABAC / compression at the
    same bitrate). Fall back to CBP for CBP-only sinks or unparseable values.
    """
    try:
        value = int((profile_hex or "01").strip(), 16)
    except ValueError:
        return "01"
    if value & 0x02:
        return "02"
    return "01"


def _encoder_h264_profile(sink_format: Optional[WFDVideoFormat]) -> str:
    """ffmpeg/x264 profile name matching the WFD profile M4 will advertise."""
    if sink_format is None:
        return "baseline"
    chosen = _choose_profile(sink_format.profile)
    # CHP → high (still -bf 0 for low-latency Miracast). CBP → baseline.
    return "high" if chosen == "02" else "baseline"


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
    pixels = mode.width * mode.height
    if pixels <= 720 * 576:
        return WFD_LEVEL_31 if mode.fps <= 30 else WFD_LEVEL_32
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


_mode_force_warned = False


def _mode_supported(
    mode: WFDCEAMode,
    *,
    cea_supported: int,
    vesa_supported: int,
    max_level: Optional[int],
    allow_interlaced: bool,
) -> bool:
    """Whether a mode is negotiable from sink CEA/VESA masks.

    ``max_level`` is accepted for API compatibility but **not** used as a hard
    gate: cheap sinks (e.g. Realtek 8192CU) often set CEA bits for 1080p60
    while advertising an H.264 level bitmap that omits 4.2. Smart View still
    selects 1080p60 from the CEA mask; we follow that. M4 still announces the
    level required by the chosen mode via ``_wfd_level_for_mode``.
    """
    del max_level  # CEA/VESA bits are authoritative for mode presence.
    if mode.interlaced and not allow_interlaced:
        return False
    if mode.table == "vesa":
        if not (vesa_supported & mode.bit):
            return False
    else:
        if not (cea_supported & mode.bit):
            return False
    return True


def _score_mode(
    mode: WFDCEAMode,
    *,
    resolution: Optional[tuple[int, int]],
    fps: int,
) -> tuple:
    """Higher is better. Prefer matching size/rate; never prefer interlaced here."""
    want_w, want_h = resolution if resolution else (1920, 1080)
    # Prefer not upscaling beyond source when possible.
    size_pen = abs(mode.width - want_w) + abs(mode.height - want_h)
    if mode.width > want_w + 16 or mode.height > want_h + 16:
        size_pen += 5000
    fps_pen = abs(mode.fps - fps)
    # Mild preference for progressive HD over SD when sizes are similar.
    hd_bonus = 0
    if mode.height >= 720:
        hd_bonus = 100
    if mode.height >= 1080:
        hd_bonus = 200
    return (-size_pen, -fps_pen, hd_bonus, mode.width * mode.height, mode.fps)


def _mode_policy() -> str:
    """RTSP mode pick policy.

    ``best_advertised`` (default): highest progressive sink mode by pixel-rate
    (width×height×fps). ``match_settings``: honor ``--fps`` / ``--output-res``.
    """
    raw = (os.environ.get("FLUXCAST_WFD_MODE_POLICY") or "best_advertised").strip().lower()
    if raw in ("match_settings", "settings", "prefer_settings", "preferred"):
        return "match_settings"
    return "best_advertised"


def _best_advertised_mode(modes: list[WFDCEAMode]) -> Optional[WFDCEAMode]:
    """Pick the richest progressive mode (pixel-rate, then pixels, then fps)."""
    if not modes:
        return None
    return max(
        modes,
        key=lambda m: (m.width * m.height * m.fps, m.width * m.height, m.fps),
    )


def _choose_cea_mode(
    config: WFDMediaConfig,
    sink_format: Optional[WFDVideoFormat],
) -> WFDCEAMode:
    cea_supported = sink_format.cea_mask if sink_format else (
        WFD_CEA_720P30 | WFD_CEA_720P60 | WFD_CEA_1080P30 | WFD_CEA_1080P60
    )
    vesa_supported = sink_format.vesa_mask if sink_format else 0
    max_level = _max_wfd_level(sink_format.level) if sink_format else WFD_LEVEL_42
    resolution = _desired_resolution(config)
    fps = int(config.fps or 30)

    all_modes = {**WFD_CEA_MODES, **WFD_VESA_MODES}

    def supports(mode: WFDCEAMode, *, allow_interlaced: bool = False) -> bool:
        return _mode_supported(
            mode,
            cea_supported=cea_supported,
            vesa_supported=vesa_supported,
            max_level=max_level,
            allow_interlaced=allow_interlaced,
        )

    progressive = [m for m in all_modes.values() if supports(m)]

    # Default: negotiate the best progressive mode the sink advertised.
    # Capture/encode then follow that mode (see RTSP handler _start_media).
    if sink_format is not None and _mode_policy() == "best_advertised":
        best = _best_advertised_mode(progressive)
        if best is not None:
            return best

    # match_settings (or unknown sink): prefer buckets near --fps / --output-res.
    wants_1200 = resolution is not None and resolution[0] >= 1920 and resolution[1] > 1080
    wants_480 = resolution is not None and resolution[0] <= 720 and resolution[1] <= 480
    wants_576 = resolution is not None and resolution[1] == 576
    wants_720 = resolution is not None and resolution[0] <= 1280 and resolution[1] <= 720
    wants_24 = 22 <= fps <= 26
    wants_25_50 = fps in (25, 50) or (not wants_24 and 48 <= fps <= 52)
    wants_60 = fps > 30 and not wants_25_50 and not wants_24

    preferred: list[int] = []
    if wants_1200:
        preferred = (
            [WFD_VESA_1200P60, WFD_VESA_1200P30, WFD_CEA_1080P60, WFD_CEA_1080P30]
            if wants_60
            else [WFD_VESA_1200P30, WFD_VESA_1200P60, WFD_CEA_1080P30, WFD_CEA_1080P60]
        )
    elif wants_576 or wants_25_50:
        preferred = [
            WFD_CEA_1080P50,
            WFD_CEA_720P50,
            WFD_CEA_1080P25,
            WFD_CEA_720P25,
            WFD_CEA_576P50,
            WFD_CEA_1080P30,
            WFD_CEA_720P30,
        ]
    elif wants_24:
        preferred = [
            WFD_CEA_1080P24,
            WFD_CEA_720P24,
            WFD_CEA_1080P30,
            WFD_CEA_720P30,
        ]
    elif wants_480:
        preferred = (
            [WFD_CEA_480P60, WFD_CEA_640P60, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60
            else [WFD_CEA_480P60, WFD_CEA_640P60, WFD_CEA_720P30, WFD_CEA_720P60]
        )
    elif wants_720:
        preferred = (
            [WFD_CEA_720P60, WFD_CEA_720P50, WFD_CEA_720P30, WFD_CEA_720P25, WFD_CEA_720P24]
            if wants_60
            else [
                WFD_CEA_720P30,
                WFD_CEA_720P25,
                WFD_CEA_720P24,
                WFD_CEA_720P60,
                WFD_CEA_720P50,
            ]
        )
    else:
        preferred = (
            [
                WFD_CEA_1080P60,
                WFD_CEA_1080P50,
                WFD_CEA_1080P30,
                WFD_CEA_720P60,
                WFD_CEA_720P30,
            ]
            if wants_60
            else [
                WFD_CEA_1080P30,
                WFD_CEA_1080P25,
                WFD_CEA_1080P24,
                WFD_CEA_720P30,
                WFD_CEA_1080P60,
                WFD_CEA_720P60,
            ]
        )

    for bit in preferred:
        mode = all_modes.get(bit)
        if mode is not None and supports(mode):
            return mode

    if progressive:
        progressive.sort(
            key=lambda m: _score_mode(m, resolution=resolution, fps=fps),
            reverse=True,
        )
        return progressive[0]

    # Nothing advertised — force the best mode for the source monitor.
    # Modern sinks often accept modes beyond what they advertise.
    if wants_1200:
        forced = (
            [WFD_VESA_1200P60, WFD_VESA_1200P30, WFD_CEA_1080P60, WFD_CEA_1080P30]
            if wants_60
            else [WFD_VESA_1200P30, WFD_VESA_1200P60, WFD_CEA_1080P30, WFD_CEA_1080P60]
        )
    elif wants_720:
        forced = (
            [WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60
            else [WFD_CEA_720P30, WFD_CEA_720P60]
        )
    else:
        forced = (
            [WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60
            else [WFD_CEA_1080P30, WFD_CEA_1080P60, WFD_CEA_720P30, WFD_CEA_720P60]
        )
    mode = all_modes[forced[0]]
    global _mode_force_warned
    if not _mode_force_warned:
        _mode_force_warned = True
        print(
            f"[FluxCast WFD RTSP] WARNING: Sink lacks advertised support for "
            f"{mode.name}; forcing it (most sinks accept it)."
        )
    return mode


def _selected_video_format(
    config: WFDMediaConfig,
    sink_format: Optional[WFDVideoFormat],
) -> str:
    mode = _choose_cea_mode(config, sink_format)

    profile = _choose_profile(sink_format.profile) if sink_format else "01"
    wfd_level = _wfd_level_for_mode(mode)
    # Bump level for resolutions exceeding standard 1080p limits
    if mode.width * mode.height > 1920 * 1080:
        wfd_level = WFD_LEVEL_50 if mode.fps <= 30 else WFD_LEVEL_51
    level = f"{wfd_level:02x}"

    # Place the mode bit in the correct mask field (CEA vs VESA)
    if mode.table == "vesa":
        cea_mask = 0
        vesa_mask = mode.bit
    else:
        cea_mask = mode.bit
        vesa_mask = 0

    return (
        f"{mode.native} 00 {profile} {level} {cea_mask:08x} "
        f"{vesa_mask:08x} 00000000 00 0000 0000 00 none none"
    )


def _h264_level_for_mode(config: WFDMediaConfig) -> str:
    resolution = _parse_resolution(config.output_resolution) or (1920, 1080)
    width, height = resolution
    if width <= 1280 and height <= 720:
        return "3.1" if config.fps <= 30 else "3.2"
    # 1920x1200@60fps: 120x75 = 9000 MBs > level 4.2 limit (8704 MBs),
    # MB rate 540000 > 4.2 limit (522240). Need level 5.0+.
    if width * height > 1920 * 1080:
        return "5.0" if config.fps <= 30 else "5.1"
    return "4.0" if config.fps <= 30 else "4.2"
