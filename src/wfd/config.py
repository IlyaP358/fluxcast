from dataclasses import dataclass
from typing import Optional


class WFDNotReady(RuntimeError):
    pass

@dataclass
class WFDMediaConfig:
    monitor: Optional[object]
    fps: int = 30
    bitrate: str = "4M"
    output_resolution: Optional[str] = None
    audio_device: Optional[str] = None
    no_audio: bool = False
    test_pattern: bool = False
    ffmpeg_stats: bool = False
    source_port: int = 19002
    media_pipeline: str = "auto"
    latency_log_path: Optional[str] = None
    capture_backend: str = "auto"
    peer_name: str = ""
    uibc: bool = False  # opt-in: accept touch/mouse input back from the sink (issue #37)
    # H.264 profile the encoders emit; must match the profile sent in M4 (#84).
    h264_profile: str = "baseline"
    aosp_pmt_pid: bool = False  # opt-in: PMT on AOSP's 0x0100 instead of the muxer default (#84)
    dump_ts_path: Optional[str] = None

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
    table: str = "cea"  # "cea" or "vesa"

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"
