import os
import subprocess

from .config import WFDMediaConfig


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
