"""Optional hardware H.264 encoder selection for WFD desktop capture.

Default encode path stays historical libx264 so existing FluxCast users see no
pipeline change. Set FLUXCAST_WFD_ENCODER to vaapi, qsv, or auto (VAAPI then
QSV then libx264) to opt into GPU encode.

Automatic battery / saver-profile encode throttling only engages when GPU
encode was opted in (vaapi / qsv / auto) or ``FLUXCAST_WFD_POWER_PLAN`` /
legacy ``FLUXCAST_WFD_ENCODE_BIAS`` is set. Default libx264 sessions keep
historical bitrate and presets. When a GPU request falls back to libx264,
throttling still applies because the request was GPU.

Power plans use stable ``power_plan_N`` ids with OS profile strings as names
(see ``wfd.power_plan``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .outputs import monitor_scale
from .power_plan import (
    active_power_plan,
    encode_throttled,
    on_mains_power,
)

# Re-exports / aliases for call sites and tests.
_on_mains_power = on_mains_power


def power_bias() -> str:
    """Deprecated shim: ``efficient`` if throttled else ``full``."""
    return "efficient" if encode_throttled() else "full"


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

    Prefer :func:`capture_encode_preference` for Omarchy RENDER METHOD
    (``dmabuf`` / ``vaapi`` / ``cpu``); this legacy helper still drives env-only
    callers.
    """
    pref = _capture_encode_preference_raw()
    if pref == "cpu":
        return "pipe"
    if pref == "vaapi":
        return "pipe"
    if pref == "dmabuf":
        return "vaapi"
    raw = os.environ.get("FLUXCAST_WFD_CAPTURE_ENCODE", "").strip().lower()
    if raw in ("pipe", "raw", "cpu", "hwupload"):
        return "pipe"
    if raw in ("vaapi", "dmabuf", "gpu"):
        return "vaapi"
    if raw == "auto":
        return "auto"
    return "pipe"


def _capture_encode_preference_raw() -> str:
    """Return ``dmabuf``|``vaapi``|``cpu``|``\"\"`` from file/env (no legacy map)."""
    path = (os.environ.get("FLUXCAST_WFD_CAPTURE_ENCODE_FILE") or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip().lower()
            if raw in ("dmabuf", "vaapi", "cpu"):
                return raw
        except OSError:
            pass
    raw = (os.environ.get("FLUXCAST_WFD_CAPTURE_ENCODE_PREF") or "").strip().lower()
    if raw in ("dmabuf", "vaapi", "cpu"):
        return raw
    return ""


def capture_encode_preference() -> str:
    """RENDER METHOD preference: ``dmabuf`` | ``vaapi`` | ``cpu``.

    Reads ``FLUXCAST_WFD_CAPTURE_ENCODE_FILE`` (live-updatable), then
    ``FLUXCAST_WFD_CAPTURE_ENCODE_PREF``, then derives from legacy
    ``FLUXCAST_WFD_CAPTURE_ENCODE`` / ``FLUXCAST_WFD_ENCODER``.
    """
    raw = _capture_encode_preference_raw()
    if raw:
        return raw
    enc = (os.environ.get("FLUXCAST_WFD_ENCODER") or "").strip().lower()
    if enc in ("libx264", "x264", "software", "sw"):
        return "cpu"
    mode = (os.environ.get("FLUXCAST_WFD_CAPTURE_ENCODE") or "").strip().lower()
    if mode in ("pipe", "raw", "hwupload"):
        return "vaapi" if enc not in ("libx264", "x264", "software", "sw", "") else "cpu"
    if mode in ("vaapi", "dmabuf", "gpu", "auto"):
        return "dmabuf"
    # Omarchy default when GPU encode was requested via encoder=auto.
    if enc in ("auto", "vaapi", "qsv"):
        return "dmabuf"
    return "cpu"


def capture_encode_attempts(preference: Optional[str] = None) -> list[str]:
    """Ordered RENDER METHOD attempts ending at CPU for GPU prefs."""
    pref = preference or capture_encode_preference()
    if pref == "dmabuf":
        return ["dmabuf", "vaapi", "cpu"]
    if pref == "vaapi":
        return ["vaapi", "cpu"]
    return ["cpu"]




def prefer_wf_recorder_vaapi_dmabuf(monitor=None) -> bool:
    """True when capture should try wf-recorder -c h264_vaapi (DMA-BUF).

    Scaled outputs are allowed: whole-output screencopy still yields physical
    mode-sized DMA buffers (logical region in the log is expected). Proven at
    integer and fractional scales with ``out_range=tv`` / CQP when geometry is
    stable (mid-session scale/position thrash can still black the sink).

    Escape hatches:
    - RENDER METHOD ``vaapi`` / ``cpu`` (or ``FLUXCAST_WFD_CAPTURE_ENCODE=pipe``)
    - ``FLUXCAST_WFD_DMABUF_ALLOW_SCALED=0`` — pipe only when scale != 1
    """
    pref = capture_encode_preference()
    if pref in ("vaapi", "cpu"):
        return False
    if pref != "dmabuf":
        # Legacy path without RENDER METHOD preference.
        mode = capture_encode_mode()
        if mode == "pipe":
            return False
        if mode == "auto" and not _requested_gpu_encode():
            return False
        if mode not in ("vaapi", "auto"):
            return False
    if not _vaapi_usable():
        return False
    # Monitor NamedTuple has no scale — probe the compositor when needed.
    if monitor is not None:
        try:
            scale = float(getattr(monitor, "scale", None) or 0) or 0.0
        except (TypeError, ValueError):
            scale = 0.0
        if scale <= 0:
            name = str(getattr(monitor, "name", "") or "")
            scale = monitor_scale(name)
        if abs(scale - 1.0) > 0.01:
            allow = (os.environ.get("FLUXCAST_WFD_DMABUF_ALLOW_SCALED", "") or "").strip().lower()
            # Default allow; only an explicit deny forces the pipe fallback.
            if allow in ("0", "false", "no", "off", "never"):
                return False
    return True


def vaapi_quality_for_plan(*, throttled: Optional[bool] = None) -> str:
    """ffmpeg/wf-recorder h264_vaapi -quality (higher = faster/worse).

    Throttled used to force ``7`` (very blocky on Miracast TVs). GPU encode is
    cheap enough that ``5`` still saves work without looking like a slideshow.

    Override with ``FLUXCAST_WFD_VAAPI_QUALITY`` (1–8) for A/B tests.
    """
    env_q = (os.environ.get("FLUXCAST_WFD_VAAPI_QUALITY", "") or "").strip()
    if env_q:
        try:
            return str(max(1, min(8, int(env_q))))
        except ValueError:
            pass
    if throttled is None:
        throttled = encode_throttled()
    return "5" if throttled else "4"


# Deprecated name kept for callers/tests not yet updated.
def vaapi_quality_for_bias(bias: Optional[str] = None) -> str:
    if bias is None:
        return vaapi_quality_for_plan()
    return vaapi_quality_for_plan(throttled=(bias == "efficient"))


def _vaapi_rc_mode(*, default: str = "CBR") -> str:
    """Rate control for pipe-path ffmpeg h264_vaapi.

    Honors ``FLUXCAST_WFD_VAAPI_RC`` (CQP / CBR / VBR / …) the same way the
    DMA-BUF wf-recorder path does. When unset, ``default`` applies — pipe
    historically used CBR (bare ``-b:v`` could land on AVBR and undershoot).
    """
    rc = (os.environ.get("FLUXCAST_WFD_VAAPI_RC", "") or "").strip().upper()
    if rc in ("CQP", "CBR", "VBR", "AVBR", "QVBR", "ICQ"):
        return rc
    return default


def _vaapi_qp() -> str:
    qp = (os.environ.get("FLUXCAST_WFD_VAAPI_QP", "") or "18").strip() or "18"
    try:
        return str(max(1, min(51, int(qp))))
    except ValueError:
        return "18"


def _vaapi_gop(gop: int) -> int:
    """Prefer ``FLUXCAST_WFD_VAAPI_GOP`` (movie preset = 60) over caller default."""
    raw = (os.environ.get("FLUXCAST_WFD_VAAPI_GOP", "") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, int(gop))


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


def apply_bitrate_plan(bitrate_text: str, *, throttled: bool) -> str:
    """When throttled, mildly trim bitrate (GPU encode is cheap; heavy trim looks blocky on TVs)."""
    if not throttled:
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


def apply_bitrate_bias(bitrate_text: str, bias: str) -> str:
    """Deprecated: prefer ``apply_bitrate_plan``."""
    return apply_bitrate_plan(bitrate_text, throttled=(bias == "efficient"))


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
    encoder_override: Optional[str] = None,
) -> EncodePlan:
    """Build ffmpeg argv fragments for the chosen encoder.

    vf_scale is either None (source matches output; format only) or a full
    software letterbox/scale filter string from encoding._letterbox_vf.

    input_pix_fmt: pipe pixel format. ``nv12`` skips CPU ``format=nv12`` before
    hwupload (paired with wf-recorder ``-x nv12``).

    encoder_override: force ``vaapi`` / ``qsv`` / ``libx264`` for RENDER METHOD
    fallbacks (ignores ``FLUXCAST_WFD_ENCODER`` for this plan only).

    output_height selects the historical libx264 preset when not throttled:
    ultrafast above 1080p, veryfast otherwise (matches pre-hw-encode FluxCast).
    """
    plan = active_power_plan()
    throttled = encode_throttled(plan)
    plan_note = plan.label()
    requested = (encoder_override or _requested_encoder()).strip().lower()
    choice = probe_encoder(requested)
    level_idc = _level_to_idc(level)
    wanted_gpu = requested not in ("libx264", "x264", "software", "sw")
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
        # this hardware. Throttled plan = faster quality + trimmed bitrate.
        quality = vaapi_quality_for_plan(throttled=throttled)
        # Pipe-path latency: async_depth 1 reduces parallelism but cuts encoder
        # queuing. Override with FLUXCAST_WFD_VAAPI_ASYNC_DEPTH (default 2).
        _async = (os.environ.get("FLUXCAST_WFD_VAAPI_ASYNC_DEPTH", "") or "2").strip() or "2"
        try:
            _async_i = str(max(1, min(4, int(_async))))
        except ValueError:
            _async_i = "2"
        rc = _vaapi_rc_mode(default="CBR")
        gop_i = _vaapi_gop(gop)
        video_args = [
            "-c:v", "h264_vaapi",
            "-profile:v", _map_h264_profile(h264_profile),
            "-level", level_idc,
            "-bf", "0",
            "-g", str(gop_i),
            "-keyint_min", str(gop_i),
            "-r", str(fps),
            "-rc_mode", rc,
        ]
        if rc == "CQP":
            # Sharp/static: pure QP. Note: Intel ignores -maxrate in CQP, so
            # busy scenes can spike 30–40 Mbps — fine on wide P2P, harsh on
            # 20 MHz / ~72 Mbps Miracast links (use QVBR there).
            qp = _vaapi_qp()
            video_args += ["-qp", qp]
            rc_note = f"CQP qp={qp} gop={gop_i}"
        elif rc == "QVBR":
            # Quality-defined VBR: keep qp as quality floor and honor maxrate.
            # Prefer FLUXCAST_WFD_VAAPI_BITRATE as the peak cap when set.
            qp = _vaapi_qp()
            cap = (os.environ.get("FLUXCAST_WFD_VAAPI_BITRATE", "") or "").strip() or bitrate
            video_args += [
                "-qp", qp,
                "-b:v", bitrate,
                "-maxrate", cap,
                "-bufsize", bufsize,
            ]
            rc_note = f"QVBR qp={qp} b={bitrate} max={cap} gop={gop_i}"
        else:
            # Explicit bitrate mode: bare -b:v can land on AVBR and undershoot
            # badly on static desktops (few hundred kb/s vs multi-Mbps target).
            video_args += [
                "-b:v", bitrate,
                "-maxrate", bitrate,
                "-bufsize", bufsize,
            ]
            rc_note = f"{rc} {bitrate} gop={gop_i}"
        video_args += [
            "-quality", quality,
            "-async_depth", _async_i,
        ]
        return EncodePlan(
            name="vaapi",
            pre_input=["-vaapi_device", device],
            vf=["-vf", vf],
            video_args=video_args,
            note=f"h264_vaapi on {device} ({plan_note}; {rc_note})",
        )

    if choice == "qsv":
        if vf_scale:
            vf = f"{vf_scale},format=nv12,hwupload=extra_hw_frames=64"
        else:
            vf = "format=nv12,hwupload=extra_hw_frames=64"
        # "balanced" here is the QSV preset name, not a FluxCast power plan.
        preset = "faster" if throttled else "balanced"
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
            note=f"h264_qsv ({plan_note})",
        )

    # Software path — same shape as the historical FluxCast argv.
    # Unthrottled: ultrafast above 1080p (CPU headroom), veryfast otherwise.
    # Throttled: always ultrafast.
    if throttled:
        preset = "ultrafast"
    elif output_height is not None and output_height > 1080:
        preset = "ultrafast"
    else:
        preset = "veryfast"
    if wanted_gpu:
        note = f"libx264 {preset} ({plan_note}; no usable GPU encoder)"
    else:
        note = f"libx264 {preset} ({plan_note})"
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
