import glob
import os
import re
import subprocess
import shutil
import sys
import time
from typing import Any, Optional, NamedTuple

FFMPEG_PATH = shutil.which("ffmpeg") or "/usr/sbin/ffmpeg"


class Monitor(NamedTuple):
    display: str
    name: str # e.g. 'eDP-1'
    width: int
    height: int
    x: int # x offset within the combined framebuffer
    y: int
    refresh: float # Hz


class SessionInfo(NamedTuple):
    session_type: str
    desktop: str
    wm: str
    is_hyprland: bool
    is_wayland: bool
    is_x11: bool


class CaptureStartError(RuntimeError):
    pass


class CaptureResult(NamedTuple):
    process: "subprocess.Popen[bytes]"
    aux_process: "Optional[subprocess.Popen[bytes]]" = None
    portal_session: Any = None


def detect_session() -> SessionInfo:
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").strip()
    wm = (os.environ.get("XDG_SESSION_DESKTOP") or "").strip()
    hypr_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    is_hyprland = bool(hypr_sig) or "hyprland" in desktop.lower() or "hyprland" in wm.lower()
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or session_type == "wayland"
    is_x11 = bool(os.environ.get("DISPLAY")) and (session_type == "x11" or not is_wayland)
    if not session_type:
        session_type = "wayland" if is_wayland else ("x11" if is_x11 else "unknown")
    return SessionInfo(
        session_type=session_type,
        desktop=desktop or "unknown",
        wm=wm or "unknown",
        is_hyprland=is_hyprland,
        is_wayland=is_wayland,
        is_x11=is_x11,
    )


def _default_audio_monitor() -> str:
    pactl = shutil.which("pactl")
    if not pactl:
        return "default"
    try:
        result = subprocess.run(
            [pactl, "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "default"
    sink = result.stdout.strip()
    if sink:
        return sink + ".monitor"
    return "default"


def _gst_has_element(name: str) -> bool:
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", name],
            capture_output=True,
            timeout=10.0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _auto_capture_backend_order() -> list[str]:
    session = detect_session()
    if session.is_hyprland:
        return ["wf-recorder", "x11grab"]
    if session.is_x11:
        return ["x11grab", "wf-recorder"]
    if session.is_wayland:
        return ["portal", "wf-recorder"]
    return ["x11grab", "wf-recorder"]


def choose_capture_backend(preferred: str = "auto") -> str:
    if preferred != "auto":
        return preferred
    return _auto_capture_backend_order()[0]


def describe_capture_selection(backend: str) -> None:
    session = detect_session()
    print(
        "[FluxCast] Session detected: "
        f"type={session.session_type}, desktop={session.desktop}, wm={session.wm}"
    )
    if backend == "portal":
        print("[FluxCast] Capture backend: XDG portal (KDE/GNOME Wayland)")
    elif backend == "wf-recorder" and session.is_wayland and not session.is_hyprland:
        print(
            "[FluxCast] Capture backend: wf-recorder (best-effort on this Wayland session). "
            "If capture fails, use an X11 session or install portal stack for KDE/GNOME."
        )
    elif backend == "x11grab" and session.is_wayland:
        print(
            "[FluxCast] Capture backend: x11grab (forced). "
            "On Wayland this may produce a black stream."
        )
    else:
        print(f"[FluxCast] Capture backend: {backend}")


def _detect_live_displays() -> list:
    locks = glob.glob("/tmp/.X*-lock")
    displays = []
    for lock in sorted(locks):
        m = re.search(r"/\.X(\d+)-lock$", lock)
        if m:
            displays.append(f":{m.group(1)}")
    return displays or [":0"]


def _parse_xrandr(display: str) -> list:
    xrandr = shutil.which("xrandr")
    if not xrandr:
        return []

    env = os.environ.copy()
    env["DISPLAY"] = display
    try:
        result = subprocess.run(
            [xrandr, "--query"],
            capture_output=True, text=True, timeout=4, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    monitors: list = []
    lines = result.stdout.splitlines()
    for i, line in enumerate(lines):
        m = re.match(
            r"^(\S+)\s+connected\s+(?:primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)",
            line,
        )
        if not m:
            continue
        name, w, h, x, y = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        refresh = 0.0
        if i + 1 < len(lines):
            rm = re.search(r"([\d.]+)\*", lines[i + 1])
            if rm:
                refresh = float(rm.group(1))
        monitors.append(Monitor(
            display=display, name=name,
            width=w, height=h, x=x, y=y, refresh=refresh,
        ))
    return monitors


def gather_monitors() -> list:
    all_monitors: list = []
    for disp in _detect_live_displays():
        all_monitors.extend(_parse_xrandr(disp))
    return all_monitors


def prompt_monitor() -> Monitor:
    monitors = gather_monitors()

    if not monitors:
        print("[FluxCast] WARNING: xrandr returned no monitors. Falling back to :0 1920x1080.")
        return Monitor(display=":0", name="(unknown)", width=1920, height=1080,
                       x=0, y=0, refresh=60.0)

    current_display = os.environ.get("DISPLAY", "")

    print("\n[FluxCast] Available monitors to capture:")
    print(f"  {'#':<4} {'Monitor':<12} {'Display':<8} "
          f"{'Resolution':<14} {'Position':<14} {'Refresh'}")
    print(f"  {'-'*4} {'-'*12} {'-'*8} {'-'*14} {'-'*14} {'-'*8}")

    default_idx = 0
    for i, mon in enumerate(monitors):
        pos = f"{mon.x},{mon.y}"
        hz = f"{mon.refresh:.1f} Hz" if mon.refresh else "—"
        current = " ← active" if mon.display == current_display and i == 0 else ""
        print(f"  [{i}]  {mon.name:<12} {mon.display:<8} "
              f"{mon.width}x{mon.height:<8} {pos:<14} {hz}{current}")
        if mon.display == current_display:
            default_idx = i

    print()
    raw = input(f"Select monitor [{default_idx}]: ").strip()
    if raw == "":
        return monitors[default_idx]
    try:
        idx = int(raw)
        return monitors[idx]
    except (ValueError, IndexError):
        print(f"[FluxCast] Invalid choice, using monitor {default_idx}.")
        return monitors[default_idx]


def start_capture(
    monitor: Monitor,
    fps: int = 30,
    bitrate: str = "4M",
    output_resolution: Optional[str] = None,
    backend: str = "auto",
) -> "CaptureResult":
    backends = [backend] if backend != "auto" else _auto_capture_backend_order()
    errors: list[str] = []

    for idx, candidate in enumerate(backends):
        describe_capture_selection(candidate)
        try:
            if candidate == "portal":
                return _start_capture_portal(monitor, fps, bitrate, output_resolution)
            if candidate == "x11grab":
                proc = _start_capture_x11grab(monitor, fps, bitrate, output_resolution)
            else:
                proc = _start_capture_wf_recorder(monitor, fps, bitrate, output_resolution)
            return CaptureResult(process=proc)
        except CaptureStartError as exc:
            errors.append(f"{candidate}: {exc}")
            if idx < len(backends) - 1:
                print(f"[FluxCast] Capture backend {candidate} failed, trying fallback...")

    detail = "; ".join(errors) if errors else "unknown capture error"
    print(f"[FluxCast] ERROR: Could not start capture backend ({detail})")
    sys.exit(1)


def _start_capture_wf_recorder(
    monitor: Monitor,
    fps: int = 30,
    bitrate: str = "4M",
    output_resolution: Optional[str] = None,
) -> "subprocess.Popen[bytes]":
    if not shutil.which("wf-recorder"):
        raise CaptureStartError("wf-recorder not found in PATH")
    src_res = f"{monitor.width}x{monitor.height}"
    out_res = output_resolution or src_res
    cmd = [
        "wf-recorder",
        "-y",
        "-D",
        "-r", str(fps),
        "-a",
        "-C", "aac",
        "-P", "b:a=128k",
        "-c", "libx264",
        "-m", "hls",
        "-p", "preset=ultrafast",
        "-p", "tune=zerolatency",
        "-p", "hls_time=2",
        "-p", "hls_list_size=6",
        "-p", "hls_flags=append_list",
        "-p", "pix_fmt=yuv420p",
        "-p", "profile=main",
        "-f", "/tmp/fluxcast/stream.m3u8",
        "-o", monitor.name,
    ]

    print(f"[FluxCast] Capturing Wayland monitor : {monitor.name} ({src_res})")
    if out_res != src_res:
        print(f"[FluxCast] Scaling output to : {out_res}")

    hls_dir = "/tmp/fluxcast"
    if os.path.exists(hls_dir):
        shutil.rmtree(hls_dir)
    os.makedirs(hls_dir, exist_ok=True)

    try:
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise CaptureStartError(f"required executable not found: {e}") from e

    time.sleep(1.5)
    if process.poll() is not None:
        raise CaptureStartError("wf-recorder exited immediately")

    return process


def stop_capture(result: "Optional[CaptureResult]") -> None:
    if result is None:
        return
    for proc in (result.process, result.aux_process):
        if proc is None or proc.poll() is not None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if result.portal_session is not None:
        from portal_capture import close_portal_capture
        close_portal_capture(result.portal_session)


def _start_capture_portal(
    monitor: Monitor,
    fps: int = 30,
    bitrate: str = "4M",
    output_resolution: Optional[str] = None,
) -> "CaptureResult":
    if not shutil.which("gst-launch-1.0"):
        raise CaptureStartError("portal backend requires gst-launch-1.0")

    required = ("pipewiresrc", "videoconvert", "videoscale", "videorate",
                "x264enc", "h264parse", "hlssink2")
    missing = [e for e in required if not _gst_has_element(e)]
    if missing:
        raise CaptureStartError(
            "portal backend requires missing GStreamer elements: " + ", ".join(missing)
        )

    from portal_capture import (
        PortalCaptureError,
        close_portal_capture,
        start_portal_capture,
    )

    b = bitrate.upper().rstrip("B")
    if b.endswith("M"):
        bitrate_kbits = int(float(b[:-1]) * 1000)
    elif b.endswith("K"):
        bitrate_kbits = int(float(b[:-1]))
    else:
        bitrate_kbits = max(1, int(b) // 1000)

    out_w, out_h = monitor.width, monitor.height
    if output_resolution:
        try:
            ow, oh = output_resolution.lower().split("x")
            out_w, out_h = int(ow), int(oh)
        except (ValueError, AttributeError):
            pass

    hls_dir = "/tmp/fluxcast"
    if os.path.exists(hls_dir):
        shutil.rmtree(hls_dir)
    os.makedirs(hls_dir, exist_ok=True)

    audio_monitor = _default_audio_monitor()
    audio_enc = (
        "avenc_aac" if _gst_has_element("avenc_aac") else
        "faac" if _gst_has_element("faac") else None
    )
    has_audio = audio_enc is not None and _gst_has_element("pulsesrc")

    print("[FluxCast] Opening portal screen-share dialog (KDE/GNOME Wayland)...")
    try:
        session = start_portal_capture(
            timeout=120.0,
            preferred_position=(monitor.x, monitor.y),
            preferred_size=(monitor.width, monitor.height),
        )
    except PortalCaptureError as exc:
        raise CaptureStartError(f"portal capture setup failed: {exc}") from exc

    if session.size is not None and not output_resolution:
        out_w, out_h = session.size[0], session.size[1]

    video_chain = [
        "pipewiresrc",
        f"fd={session.pw_fd}",
        f"path={session.pw_node_id}",
        "do-timestamp=true",
        # always-copy=true!! immediately copy PipeWire buffer to CPU memory so
        # x264enc doesn't stall waiting for DMA-buf download mid-pipeline
        "always-copy=true",
        "!", "queue", "max-size-buffers=8", "max-size-time=0", "leaky=downstream",
        "!", "videorate", "skip-to-first=true",
        "!", f"video/x-raw,framerate={fps}/1",
        "!", "videoconvert",
        "!", "videoscale",
        "!", f"video/x-raw,width={out_w},height={out_h},format=I420",
        "!", "x264enc",
        "tune=zerolatency",
        "speed-preset=ultrafast",
        f"bitrate={bitrate_kbits}",
        f"key-int-max={fps}",
        "threads=0",
        "bframes=0",
        "byte-stream=true",
        "aud=true",
        "!", "h264parse", "config-interval=-1",
        "!", "video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline",
        "!", "sink.video",
    ]

    audio_chain: list[str] = []
    if has_audio:
        audio_chain = [
            "pulsesrc", f"device={audio_monitor}", "do-timestamp=true",
            "!", "audioconvert",
            "!", "audioresample",
            "!", "audio/x-raw,rate=48000,channels=2",
            "!", audio_enc, "bitrate=128000",
            "!", "aacparse",
            "!", "sink.audio",
        ]

    gst_cmd = [
        "gst-launch-1.0", "-e", "-q",
        "hlssink2", "name=sink",
        f"location={hls_dir}/stream%05d.ts",
        f"playlist-location={hls_dir}/stream.m3u8",
        "target-duration=2",
        "playlist-length=6",
        "max-files=8",
        *video_chain,
        *audio_chain,
    ]

    print(f"[FluxCast] Portal capture: node={session.pw_node_id}, output={out_w}x{out_h}@{fps}")
    if session.position and session.size:
        print(
            f"[FluxCast] Portal source: "
            f"pos={session.position[0]},{session.position[1]} "
            f"size={session.size[0]}x{session.size[1]}"
        )
    if has_audio:
        print(f"[FluxCast] Capturing audio: {audio_monitor}")

    try:
        gst_proc = subprocess.Popen(
            gst_cmd,
            stderr=subprocess.DEVNULL,
            pass_fds=(session.pw_fd,),
        )
    except FileNotFoundError as exc:
        close_portal_capture(session)
        raise CaptureStartError(f"gst-launch-1.0 not found: {exc}") from exc

    time.sleep(2.5)
    if gst_proc.poll() is not None:
        close_portal_capture(session)
        raise CaptureStartError("portal GStreamer HLS pipeline exited immediately")

    return CaptureResult(process=gst_proc, portal_session=session)


def _start_capture_x11grab(
    monitor: Monitor,
    fps: int = 30,
    bitrate: str = "4M",
    output_resolution: Optional[str] = None,
) -> "subprocess.Popen[bytes]":
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CaptureStartError("ffmpeg is required for x11grab backend")
    if not os.environ.get("DISPLAY"):
        raise CaptureStartError("DISPLAY is not set for x11grab")

    display = os.environ.get("DISPLAY", monitor.display or ":0")
    src_res = f"{monitor.width}x{monitor.height}"
    out_res = output_resolution or src_res
    audio_monitor = _default_audio_monitor()
    hls_dir = "/tmp/fluxcast"

    if os.path.exists(hls_dir):
        shutil.rmtree(hls_dir)
    os.makedirs(hls_dir, exist_ok=True)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-thread_queue_size", "1024",
        "-f", "x11grab",
        "-framerate", str(fps),
        "-video_size", src_res,
        "-i", f"{display}+{monitor.x},{monitor.y}",
        "-thread_queue_size", "1024",
        "-f", "pulse",
        "-i", audio_monitor,
        "-map", "0:v:0",
        "-map", "1:a:0",
    ]

    if out_res != src_res:
        cmd += ["-vf", f"scale={out_res.replace('x', ':')}:flags=bilinear,format=yuv420p"]
    else:
        cmd += ["-vf", "format=yuv420p"]

    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(max(1, fps)),
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", "2M",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-ar", "48000",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "append_list",
        "-hls_segment_filename", os.path.join(hls_dir, "stream%d.ts"),
        os.path.join(hls_dir, "stream.m3u8"),
    ]

    print(f"[FluxCast] Capturing X11 region : {display}+{monitor.x},{monitor.y} ({src_res})")
    if out_res != src_res:
        print(f"[FluxCast] Scaling output to : {out_res}")

    try:
        process = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise CaptureStartError(f"required executable not found: {exc}") from exc

    time.sleep(1.5)
    if process.poll() is not None:
        raise CaptureStartError("x11grab capture exited immediately")
    return process
