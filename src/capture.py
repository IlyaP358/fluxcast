import glob
import os
import re
import subprocess
import shutil
import sys
import time
from typing import Optional, NamedTuple

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


def _auto_capture_backend_order() -> list[str]:
    session = detect_session()
    if session.is_hyprland:
        return ["wf-recorder", "x11grab"]
    if session.is_x11:
        return ["x11grab", "wf-recorder"]
    if session.is_wayland:
        # x11grab under Wayland often captures an empty Xwayland root and appears black.
        return ["wf-recorder"]
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
    if backend == "wf-recorder" and session.is_wayland and not session.is_hyprland:
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
) -> "subprocess.Popen[bytes]":
    backends = [backend] if backend != "auto" else _auto_capture_backend_order()
    errors: list[str] = []

    for idx, candidate in enumerate(backends):
        describe_capture_selection(candidate)
        try:
            if candidate == "x11grab":
                return _start_capture_x11grab(monitor, fps, bitrate, output_resolution)
            return _start_capture_wf_recorder(monitor, fps, bitrate, output_resolution)
        except CaptureStartError as exc:
            errors.append(f"{candidate}: {exc}")
            if idx < len(backends) - 1:
                print(f"[FluxCast] Capture backend {candidate} failed, trying fallback...")

    detail = "; ".join(errors) if errors else "unknown capture error"
    session = detect_session()
    if session.is_wayland and not session.is_hyprland:
        detail += (
            "; KDE/GNOME Wayland desktop capture via portal is not enabled in this build yet. "
            "Use X11 session for desktop capture or test WFD with --wfd-test-pattern."
        )
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


def stop_capture(process: "Optional[subprocess.Popen[bytes]]") -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


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
