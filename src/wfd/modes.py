from typing import Optional

from .config import WFDCEAMode, WFDMediaConfig, WFDVideoFormat
from .constants import (
    WFD_CEA_640P60, WFD_CEA_720P30, WFD_CEA_720P60,
    WFD_CEA_1080P30, WFD_CEA_1080P60,
    WFD_VESA_1200P30, WFD_VESA_1200P60,
    WFD_LEVEL_31, WFD_LEVEL_32, WFD_LEVEL_40,
    WFD_LEVEL_42, WFD_LEVEL_50, WFD_LEVEL_51,
)
from .encoding import _parse_resolution


WFD_CEA_MODES: dict[int, WFDCEAMode] = {
    WFD_CEA_640P60:  WFDCEAMode("640x480p60",    WFD_CEA_640P60,  "00", 640,  480, 60),
    WFD_CEA_720P30:  WFDCEAMode("1280x720p30",   WFD_CEA_720P30,  "28", 1280, 720, 30),
    WFD_CEA_720P60:  WFDCEAMode("1280x720p60",   WFD_CEA_720P60,  "30", 1280, 720, 60),
    WFD_CEA_1080P30: WFDCEAMode("1920x1080p30",  WFD_CEA_1080P30, "38", 1920, 1080, 30),
    WFD_CEA_1080P60: WFDCEAMode("1920x1080p60",  WFD_CEA_1080P60, "40", 1920, 1080, 60),
}

WFD_VESA_MODES: dict[int, WFDCEAMode] = {
    WFD_VESA_1200P30: WFDCEAMode("1920x1200p30", WFD_VESA_1200P30, "00", 1920, 1200, 30, table="vesa"),
    WFD_VESA_1200P60: WFDCEAMode("1920x1200p60", WFD_VESA_1200P60, "00", 1920, 1200, 60, table="vesa"),
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
    return "01"

def _encoder_h264_profile(sink_format: Optional[WFDVideoFormat]) -> str:
    """x264/ffmpeg profile name for the profile _choose_profile advertises """
    if sink_format is None:
        return "baseline"
    return "high" if _choose_profile(sink_format.profile) == "02" else "baseline"

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

_mode_force_warned = False

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
    wants_720 = resolution is None or (resolution[0] <= 1280 and resolution[1] <= 720)
    wants_1200 = resolution is not None and resolution[0] >= 1920 and resolution[1] > 1080
    wants_60 = config.fps > 30
    wants_480 = resolution is not None and resolution[0] <= 640 and resolution[1] <= 480

    all_modes = {**WFD_CEA_MODES, **WFD_VESA_MODES}

    def supports(bit: int) -> bool:
        mode = all_modes[bit]
        if mode.table == "vesa":
            if not (vesa_supported & bit):
                return False
        else:
            if not (cea_supported & bit):
                return False
        return max_level is None or _wfd_level_for_mode(mode) <= max_level

    # Build preference order: if monitor is 1200p, prefer VESA 1200p modes first
    if wants_1200:
        preferred = (
            [WFD_VESA_1200P60, WFD_VESA_1200P30,
             WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [
                WFD_VESA_1200P30, WFD_VESA_1200P60,
                WFD_CEA_1080P30, WFD_CEA_1080P60,
                WFD_CEA_720P30, WFD_CEA_720P60,
            ]
        )
    elif wants_480:
        preferred = (
            [WFD_CEA_640P60, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [WFD_CEA_640P60, WFD_CEA_720P30, WFD_CEA_720P60]
        )
    elif wants_720:
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
            return all_modes[bit]

    # Fallback: try any supported mode
    for bit in (
        WFD_CEA_720P30, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_1080P60,
    ):
        if supports(bit):
            return all_modes[bit]

    # Nothing advertised — force the best mode for the source monitor.
    # Modern sinks (Samsung tablets etc.) accept modes beyond what they
    # advertise; Windows Miracast does the same.
    if wants_1200:
        forced = (
            [WFD_VESA_1200P60, WFD_VESA_1200P30,
             WFD_CEA_1080P60, WFD_CEA_1080P30]
            if wants_60 else [
                WFD_VESA_1200P30, WFD_VESA_1200P60,
                WFD_CEA_1080P30, WFD_CEA_1080P60,
            ]
        )
    elif wants_720:
        forced = (
            [WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [WFD_CEA_720P30, WFD_CEA_720P60]
        )
    else:
        forced = (
            [WFD_CEA_1080P60, WFD_CEA_1080P30, WFD_CEA_720P60, WFD_CEA_720P30]
            if wants_60 else [
                WFD_CEA_1080P30, WFD_CEA_1080P60,
                WFD_CEA_720P30, WFD_CEA_720P60,
            ]
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
