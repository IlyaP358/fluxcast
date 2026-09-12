"""Optional hardware H.264 encoder selection for WFD desktop capture.

Default encode path stays historical libx264 so existing FluxCast users see no
pipeline change. Set FLUXCAST_WFD_ENCODER to vaapi, qsv, or auto (VAAPI then
QSV then libx264) to opt into GPU encode.

Automatic battery / power-saver encode bias only engages when GPU encode was
opted in (vaapi / qsv / auto) or FLUXCAST_WFD_ENCODE_BIAS is set explicitly.
Default libx264 sessions keep historical bitrate and presets. When a GPU
request falls back to libx264, bias still applies because the request was GPU.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .outputs import monitor_scale


@dataclass(frozen=True)
class EncodePlan:
    name: str  # vaapi | qsv | libx264
    pre_input: list[str]
    vf: list[str]
    video_args: list[str]
    note: str


def _ffmpeg_has_encoder(name: str) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = (result.stdout or "") + (result.stderr or "")
    # Match encoder table rows like "V....D h264_vaapi"
    return f" {name} " in f" {blob} " or f"\n{name} " in blob or f" {name}\n" in blob


def _vaapi_device() -> str:
    override = os.environ.get("FLUXCAST_WFD_VAAPI_DEVICE", "").strip()
    if override:
        return override
    for path in ("/dev/dri/renderD128", "/dev/dri/renderD129"):
        if os.path.exists(path):
            return path
    return "/dev/dri/renderD128"


def _vaapi_usable() -> bool:
    """True when ffmpeg has h264_vaapi and the chosen render node exists."""
    if not _ffmpeg_has_encoder("h264_vaapi"):
        return False
    return os.path.exists(_vaapi_device())


def capture_encode_mode() -> str:
    """How desktop capture should produce H.264.

    ``pipe`` / ``raw`` / empty — historical wf-recorder rawvideo → ffmpeg encode.
    ``vaapi`` / ``dmabuf`` — wf-recorder h264_vaapi with DMA-BUF (required).
    ``auto`` — prefer DMA-BUF when VAAPI is usable and GPU encode was requested.
    """
    raw = os.environ.get("FLUXCAST_WFD_CAPTURE_ENCODE", "").strip().lower()
    if raw in ("pipe", "raw", "cpu", "hwupload"):
        return "pipe"
    if raw in ("vaapi", "dmabuf", "gpu"):
        return "vaapi"
    if raw == "auto":
        return "auto"
    return "pipe"




def prefer_wf_recorder_vaapi_dmabuf(monitor=None) -> bool:
    """True when capture should try wf-recorder -c h264_vaapi (DMA-BUF).

    Scaled outputs (scale != 1) currently glitch with wf-recorder's
    DMA-BUF + scale_vaapi path (logical vs physical size). Keep the raw pipe
    + ffmpeg hwupload path for those until the filter chain is proven.
    """
    mode = capture_encode_mode()
    if mode == "pipe":
        return False
    if not _vaapi_usable():
        return False
    # Monitor NamedTuple has no scale — probe the compositor when needed.
    scale = 1.0
    if monitor is not None:
        try:
            scale = float(getattr(monitor, "scale", None) or 0) or 0.0
        except (TypeError, ValueError):
            scale = 0.0
        if scale <= 0:
            name = str(getattr(monitor, "name", "") or "")
            scale = monitor_scale(name)
        if abs(scale - 1.0) > 0.01:
            return False
    if mode == "vaapi":
        return True
    # auto: prefer DMA-BUF when GPU encode is requested and scale allows.
    if mode == "auto":
        return _requested_gpu_encode()
    return False


def vaapi_quality_for_bias(bias: Optional[str] = None) -> str:
    """ffmpeg/wf-recorder h264_vaapi -quality (higher = faster/worse).

    Efficient used to force ``7`` (very blocky on Miracast TVs). GPU encode is
    cheap enough that ``5`` still saves work without looking like a slideshow.
    """
    b = bias or power_bias()
    return "5" if b == "efficient" else "4"


def _sysfs_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _is_system_supply(base: str) -> bool:
    """Ignore Device-scoped supplies (HID UPS, mouse, etc.).

    Missing scope is treated as System — older kernels omit the attribute on
    the laptop pack / AC adapter.
    """
    scope = _sysfs_text(os.path.join(base, "scope"))
    if scope is None:
        return True
    return scope.lower() == "system"


def _on_mains_power() -> bool:
    """True when system AC is online, or no system battery is present."""
    supply = "/sys/class/power_supply"
    try:
        names = os.listdir(supply)
    except OSError:
        return True
    saw_system_battery = False
    for name in names:
        base = os.path.join(supply, name)
        kind = (_sysfs_text(os.path.join(base, "type")) or "").lower()
        if not kind or not _is_system_supply(base):
            continue
        if kind == "mains":
            if _sysfs_text(os.path.join(base, "online")) == "1":
                return True
        elif kind == "battery":
            saw_system_battery = True
            status = (_sysfs_text(os.path.join(base, "status")) or "").lower()
            if status in ("charging", "full"):
                return True
    # Desktop / no system pack → treat as mains so we do not trim bitrate.
    return not saw_system_battery


def _power_profile() -> str:
    ctl = shutil.which("powerprofilesctl")
    if not ctl:
        return "unknown"
    try:
        result = subprocess.run(
            [ctl, "get"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout or "").strip().lower() or "unknown"


def power_bias() -> str:
    """full | efficient — efficient on battery or power-saver profile.

    Automatic battery / power-saver detection only engages when GPU encode was
    opted in (FLUXCAST_WFD_ENCODER=vaapi|qsv|auto) or the caller set
    FLUXCAST_WFD_ENCODE_BIAS explicitly. Default libx264 sessions keep
    historical bitrate and presets.
    """
    override = os.environ.get("FLUXCAST_WFD_ENCODE_BIAS", "").strip().lower()
    if override in ("full", "efficient"):
        return override
    encoder = _requested_encoder()
    if encoder in ("libx264", "x264", "software", "sw"):
        return "full"
    if not _on_mains_power():
        return "efficient"
    if _power_profile() == "power-saver":
        return "efficient"
    return "full"


def _level_to_idc(level: str) -> str:
    """Convert FluxCast levels like '3.1' / '4.0' to VAAPI level_idc '31' / '40'.

    Empty / whitespace-only input defaults to H.264 Level 3.1 (``31``), the
    common WFD HD floor. Digits-only values are returned as-is (already idc).
    Any other unparseable non-empty string raises ``ValueError``.
    """
    text = (level or "").strip()
    if not text:
        return "31"
    if text.isdigit():
        return text
    try:
        major_s, minor_s = text.split(".", 1)
        major, minor = int(major_s), int(minor_s)
    except ValueError as exc:
        raise ValueError(
            f"unsupported H.264 level {level!r}; expected 'M.m' (e.g. '3.1') or idc digits"
        ) from exc
    if major < 0 or minor < 0 or minor > 9:
        raise ValueError(
            f"unsupported H.264 level {level!r}; minor digit must be 0-9"
        )
    return f"{major}{minor}"


def _map_h264_profile(h264_profile: str) -> str:
    """Normalize to constrained_baseline | main | high for hardware encoders."""
    value = (h264_profile or "baseline").strip().lower()
    if value in ("baseline", "constrained_baseline", "cbp"):
        return "constrained_baseline"
    if value == "main":
        return "main"
    if value == "high":
        return "high"
    return "constrained_baseline"


def _qsv_profile(h264_profile: str) -> str:
    """QSV accepts baseline/main/high; map constrained_baseline → baseline."""
    mapped = _map_h264_profile(h264_profile)
    if mapped == "constrained_baseline":
        return "baseline"
    return mapped


def _requested_encoder() -> str:
    # Default libx264 preserves the historical WFD pipeline for existing users.
    # Opt into GPU with FLUXCAST_WFD_ENCODER=vaapi|qsv|auto.
    return os.environ.get("FLUXCAST_WFD_ENCODER", "libx264").strip().lower() or "libx264"


def _requested_gpu_encode() -> bool:
    return _requested_encoder() not in ("libx264", "x264", "software", "sw")


def apply_bitrate_bias(bitrate_text: str, bias: str) -> str:
    """On efficient bias, mildly trim bitrate (GPU encode is cheap; heavy trim looks blocky on TVs)."""
    if bias != "efficient":
        return bitrate_text
    text = bitrate_text.strip().lower()
    try:
        if text.endswith("m"):
            return f"{max(1.0, float(text[:-1]) * 0.90):g}M"
        if text.endswith("k"):
            return f"{max(500, int(float(text[:-1]) * 0.90))}k"
    except ValueError:
        return bitrate_text
    return bitrate_text


def probe_encoder(prefer: str = "libx264") -> str:
    prefer = (prefer or "libx264").strip().lower()
    if prefer in ("libx264", "x264", "software", "sw"):
        return "libx264"
    if prefer == "vaapi":
        # Same device check as auto — missing render node must not select VAAPI.
        return "vaapi" if _vaapi_usable() else "libx264"
    if prefer == "qsv":
        return "qsv" if _ffmpeg_has_encoder("h264_qsv") else "libx264"
    if prefer != "auto":
        return "libx264"
    # Explicit auto only: VAAPI, then QSV, then software.
    if _vaapi_usable():
        return "vaapi"
    if _ffmpeg_has_encoder("h264_qsv"):
        return "qsv"
    return "libx264"


def build_encode_plan(
    *,
    h264_profile: str,
    level: str,
    fps: int,
    gop: int,
    bitrate: str,
    bufsize: str,
    vf_scale: Optional[str],
    output_height: Optional[int] = None,
    input_pix_fmt: str = "yuv420p",
) -> EncodePlan:
    """Build ffmpeg argv fragments for the chosen encoder.

    vf_scale is either None (source matches output; format only) or a full
    software letterbox/scale filter string from encoding._letterbox_vf.

    input_pix_fmt: pipe pixel format. ``nv12`` skips CPU ``format=nv12`` before
    hwupload (paired with wf-recorder ``-x nv12``).

    output_height selects the historical libx264 preset when bias is full:
    ultrafast above 1080p, veryfast otherwise (matches pre-hw-encode FluxCast).
    """
    bias = power_bias()
    choice = probe_encoder(_requested_encoder())
    level_idc = _level_to_idc(level)
    wanted_gpu = _requested_gpu_encode()
    pix = (input_pix_fmt or "yuv420p").strip().lower()

    if choice == "vaapi":
        device = _vaapi_device()
        # Upload after any CPU scale/pad so the encoder sees NV12 on the GPU.
        # Already-NV12 pipes skip the expensive CPU format conversion.
        if vf_scale:
            vf = f"{vf_scale},format=nv12,hwupload"
        elif pix == "nv12":
            vf = "hwupload"
        else:
            vf = "format=nv12,hwupload"
        # higher -quality = faster/worse (ffmpeg h264_vaapi). Keep async_depth
        # at 2 — dropping to 1 reduces parallelism and can raise system power.
        #
        # Do NOT use -low_power here: Intel's LP entrypoint only supports CQP,
        # and CQP+low_power measured ~2.5× ffmpeg CPU vs normal CBR VAAPI on
        # this hardware. Efficient bias = faster quality + trimmed bitrate.
        quality = vaapi_quality_for_bias(bias)
        return EncodePlan(
            name="vaapi",
            pre_input=["-vaapi_device", device],
            vf=["-vf", vf],
            video_args=[
                "-c:v", "h264_vaapi",
                "-profile:v", _map_h264_profile(h264_profile),
                "-level", level_idc,
                "-bf", "0",
                "-g", str(gop),
                "-keyint_min", str(gop),
                "-r", str(fps),
                # Explicit CBR: bare -b:v can land on AVBR and undershoot badly
                # on static desktops (few hundred kb/s vs multi-Mbps target).
                "-rc_mode", "CBR",
                "-b:v", bitrate,
                "-maxrate", bitrate,
                "-bufsize", bufsize,
                "-quality", quality,
                "-async_depth", "2",
            ],
            note=f"h264_vaapi on {device} ({bias} power bias)",
        )

    if choice == "qsv":
        if vf_scale:
            vf = f"{vf_scale},format=nv12,hwupload=extra_hw_frames=64"
        else:
            vf = "format=nv12,hwupload=extra_hw_frames=64"
        preset = "faster" if bias == "efficient" else "balanced"
        return EncodePlan(
            name="qsv",
            pre_input=["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"],
            vf=["-vf", vf],
            video_args=[
                "-c:v", "h264_qsv",
                "-profile:v", _qsv_profile(h264_profile),
                "-level", level_idc,
                "-bf", "0",
                "-g", str(gop),
                "-keyint_min", str(gop),
                "-r", str(fps),
                "-b:v", bitrate,
                "-maxrate", bitrate,
                "-bufsize", bufsize,
                "-preset", preset,
            ],
            note=f"h264_qsv ({bias} power bias)",
        )

    # Software path — same shape as the historical FluxCast argv.
    # full bias: ultrafast above 1080p (CPU headroom), veryfast otherwise.
    # efficient bias: always ultrafast.
    if bias == "efficient":
        preset = "ultrafast"
    elif output_height is not None and output_height > 1080:
        preset = "ultrafast"
    else:
        preset = "veryfast"
    if wanted_gpu:
        note = f"libx264 {preset} ({bias} power bias; no usable GPU encoder)"
    else:
        note = f"libx264 {preset} ({bias} power bias)"
    return EncodePlan(
        name="libx264",
        pre_input=[],
        vf=["-vf", vf_scale] if vf_scale else ["-vf", "format=yuv420p"],
        video_args=[
            "-c:v", "libx264",
            "-preset", preset,
            "-tune", "zerolatency",
            "-profile:v", h264_profile,
            "-level:v", level,
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-bf", "0",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", bufsize,
            "-x264-params", "repeat-headers=1:aud=1",
        ],
        note=note,
    )
