import json
import importlib.util
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Optional


STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"


@dataclass
class Check:
    name: str
    status: str
    message: str
    detail: str = ""


@dataclass
class DiagnosticReport:
    checks: list[Check]
    wfd_candidate: bool
    summary: str

    def to_dict(self) -> dict:
        return {
            "checks": [asdict(check) for check in self.checks],
            "wfd_candidate": self.wfd_candidate,
            "summary": self.summary,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def _run(args: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _find_binary(binary: str, extra_paths: Optional[list[str]] = None) -> Optional[str]:
    path = shutil.which(binary)
    if path:
        return path
    for candidate in extra_paths or []:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _command_check(
    binary: str,
    purpose: str,
    required: bool = False,
    extra_paths: Optional[list[str]] = None,
) -> Check:
    path = _find_binary(binary, extra_paths=extra_paths)
    if path:
        return Check(binary, STATUS_OK, purpose, path)

    status = STATUS_FAIL if required else STATUS_WARN
    return Check(binary, status, purpose, "not found in PATH")


def _first_matching_command(commands: list[str]) -> Optional[str]:
    for command in commands:
        if shutil.which(command):
            return command
    return None


def _python_module_check(module: str, purpose: str, required: bool = False) -> Check:
    if importlib.util.find_spec(module) is not None:
        return Check(module, STATUS_OK, purpose, "python module is installed")
    status = STATUS_FAIL if required else STATUS_WARN
    return Check(
        module,
        status,
        purpose,
        "python module is missing; install with pip",
    )


def _ffmpeg_encoders() -> Check:
    ffmpeg = _first_matching_command(["ffmpeg", "/usr/sbin/ffmpeg"])
    if not ffmpeg:
        return Check("ffmpeg encoders", STATUS_FAIL, "ffmpeg is required", "not found")

    try:
        result = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("ffmpeg encoders", STATUS_FAIL, "could not query ffmpeg encoders", str(exc))

    encoders = result.stdout + result.stderr
    h264_candidates = [
        "libx264",
        "h264_vaapi",
        "h264_nvenc",
        "h264_qsv",
        "h264_v4l2m2m",
    ]
    found_h264 = [name for name in h264_candidates if name in encoders]
    found_aac = " aac " in encoders or "\naac " in encoders or "libfdk_aac" in encoders

    if found_h264 and found_aac:
        return Check(
            "ffmpeg encoders",
            STATUS_OK,
            "H.264 and AAC encoders are available",
            f"h264={', '.join(found_h264)}; aac=yes",
        )
    if found_h264:
        return Check(
            "ffmpeg encoders",
            STATUS_WARN,
            "H.264 encoder is available, AAC encoder was not detected",
            f"h264={', '.join(found_h264)}; aac=no",
        )
    return Check(
        "ffmpeg encoders",
        STATUS_FAIL,
        "no usable H.264 encoder detected",
        "need libx264 or a hardware H.264 encoder",
    )


_PORTAL_BACKENDS = [
    "xdg-desktop-portal-hyprland",
    "xdg-desktop-portal-kde",
    "xdg-desktop-portal-gnome",
    "xdg-desktop-portal-wlr",
    "xdg-desktop-portal-lxqt",
]


def _portal_process_check() -> Check:
    """Check whether xdg-desktop-portal and a backend are actually running."""

    def _pgrep(name: str) -> bool:
        try:
            r = subprocess.run(
                ["pgrep", "-f", name],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    running_backend = next((b for b in _PORTAL_BACKENDS if _pgrep(b)), None)
    if running_backend:
        return Check(
            "xdg-desktop-portal",
            STATUS_OK,
            "Wayland portal stack is running",
            running_backend,
        )

    if _pgrep("xdg-desktop-portal"):
        return Check(
            "xdg-desktop-portal",
            STATUS_WARN,
            "portal daemon running but no backend process detected",
            "is a portal backend installed and enabled?",
        )

    return Check(
        "xdg-desktop-portal",
        STATUS_WARN,
        "portal daemon not running",
        "not needed for wf-recorder (Hyprland/Sway); required for GNOME/KDE capture",
    )


def _display_capture_check() -> Check:
    wayland = os.environ.get("WAYLAND_DISPLAY")
    x11 = os.environ.get("DISPLAY")
    wf_recorder = _find_binary("wf-recorder")
    xrandr = shutil.which("xrandr")
    portal = _find_binary(
        "xdg-desktop-portal",
        extra_paths=["/usr/libexec/xdg-desktop-portal", "/usr/lib/xdg-desktop-portal"],
    )
    portal_kde = _find_binary(
        "xdg-desktop-portal-kde",
        extra_paths=["/usr/libexec/xdg-desktop-portal-kde", "/usr/lib/xdg-desktop-portal-kde"],
    )
    portal_gnome = _find_binary(
        "xdg-desktop-portal-gnome",
        extra_paths=["/usr/libexec/xdg-desktop-portal-gnome", "/usr/lib/xdg-desktop-portal-gnome"],
    )
    portal_wlr = _find_binary(
        "xdg-desktop-portal-wlr",
        extra_paths=["/usr/libexec/xdg-desktop-portal-wlr", "/usr/lib/xdg-desktop-portal-wlr"],
    )
    portal_hyprland = _find_binary(
        "xdg-desktop-portal-hyprland",
        extra_paths=["/usr/libexec/xdg-desktop-portal-hyprland", "/usr/lib/xdg-desktop-portal-hyprland"],
    )

    if wayland and wf_recorder:
        return Check(
            "screen capture",
            STATUS_OK,
            "Wayland capture path is available",
            f"WAYLAND_DISPLAY={wayland}; wf-recorder={wf_recorder}",
        )
    if wayland and portal and (portal_kde or portal_gnome or portal_wlr or portal_hyprland):
        backend = portal_kde or portal_gnome or portal_wlr or portal_hyprland
        return Check(
            "screen capture",
            STATUS_OK,
            "Wayland portal stack is available",
            f"WAYLAND_DISPLAY={wayland}; portal={portal}; backend={backend}",
        )
    if x11 and xrandr:
        return Check(
            "screen capture",
            STATUS_OK,
            "X11 capture path is available",
            f"DISPLAY={x11}; xrandr={xrandr}",
        )
    if wayland:
        detail = "Install wf-recorder or xdg-desktop-portal + desktop-specific backend."
        if not portal:
            detail = "xdg-desktop-portal not found; install portal stack for KDE/GNOME capture."
        return Check(
            "screen capture",
            STATUS_WARN,
            "Wayland detected, but capture backend is incomplete",
            detail,
        )
    if x11:
        return Check(
            "screen capture",
            STATUS_WARN,
            "X11 detected, but xrandr is missing",
            "monitor detection may fail",
        )
    return Check(
        "screen capture",
        STATUS_WARN,
        "no active WAYLAND_DISPLAY or DISPLAY detected",
        "run from the graphical desktop session",
    )


def _audio_check() -> Check:
    pactl = shutil.which("pactl")
    if not pactl:
        return Check("audio capture", STATUS_WARN, "pactl was not found", "audio monitor auto-detect may fail")

    try:
        result = _run([pactl, "get-default-sink"], timeout=2.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("audio capture", STATUS_WARN, "could not query default audio sink", str(exc))

    sink = result.stdout.strip()
    if result.returncode == 0 and sink:
        return Check("audio capture", STATUS_OK, "default audio monitor can be derived", sink + ".monitor")
    return Check("audio capture", STATUS_WARN, "default audio sink was not reported", result.stderr.strip())


def _nmcli_check() -> Check:
    if not shutil.which("nmcli"):
        return Check("NetworkManager", STATUS_WARN, "nmcli was not found", "Miracast P2P usually needs NetworkManager")

    try:
        version = _run(["nmcli", "--version"], timeout=2.0)
        devices = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("NetworkManager", STATUS_WARN, "could not query NetworkManager", str(exc))

    output = devices.stdout.strip()
    has_wifi = any(":wifi:" in line for line in output.splitlines())
    has_p2p = any("p2p" in line.lower() for line in output.splitlines())
    detail = "; ".join(part for part in [version.stdout.strip(), output] if part)
    if has_wifi and has_p2p:
        return Check("NetworkManager", STATUS_OK, "Wi-Fi and P2P devices are visible", detail)
    if has_wifi:
        return Check("NetworkManager", STATUS_WARN, "Wi-Fi is visible, but no P2P device was listed", detail)
    return Check("NetworkManager", STATUS_WARN, "no Wi-Fi device was listed by NetworkManager", detail)


def _iw_p2p_check() -> Check:
    if not shutil.which("iw"):
        return Check("iw P2P", STATUS_WARN, "iw was not found", "cannot inspect kernel Wi-Fi interfaces")

    try:
        result = _run(["iw", "dev"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("iw P2P", STATUS_WARN, "could not query iw dev", str(exc))

    output = result.stdout.strip()
    if result.returncode != 0:
        return Check("iw P2P", STATUS_WARN, "iw dev failed", result.stderr.strip())
    if re.search(r"\btype\s+P2P-device\b", output):
        return Check("iw P2P", STATUS_OK, "kernel exposes a P2P-device interface", output)
    if "Interface" in output:
        return Check("iw P2P", STATUS_WARN, "Wi-Fi interfaces exist, but no P2P-device was shown", output)
    return Check("iw P2P", STATUS_WARN, "no Wi-Fi interfaces were shown", output)


def _supplicant_capability_check() -> Check:
    if not shutil.which("gdbus"):
        return Check("wpa_supplicant P2P", STATUS_WARN, "gdbus was not found", "cannot query system D-Bus")

    args = [
        "gdbus", "call", "--system",
        "--dest", "fi.w1.wpa_supplicant1",
        "--object-path", "/fi/w1/wpa_supplicant1",
        "--method", "org.freedesktop.DBus.Properties.Get",
        "fi.w1.wpa_supplicant1",
        "Capabilities",
    ]
    try:
        result = _run(args, timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("wpa_supplicant P2P", STATUS_WARN, "could not query supplicant capabilities", str(exc))

    text = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return Check("wpa_supplicant P2P", STATUS_WARN, "supplicant capability query failed", text)
    if "p2p" in text:
        return Check("wpa_supplicant P2P", STATUS_OK, "wpa_supplicant reports P2P support", text)
    return Check("wpa_supplicant P2P", STATUS_FAIL, "wpa_supplicant does not report P2P support", text)


def _supplicant_wfd_check() -> Check:
    if not shutil.which("gdbus"):
        return Check("wpa_supplicant WFD", STATUS_WARN, "gdbus was not found", "cannot query WFDIE support")

    args = [
        "gdbus", "call", "--system",
        "--dest", "fi.w1.wpa_supplicant1",
        "--object-path", "/fi/w1/wpa_supplicant1",
        "--method", "org.freedesktop.DBus.Properties.Get",
        "fi.w1.wpa_supplicant1",
        "WFDIEs",
    ]
    try:
        result = _run(args, timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("wpa_supplicant WFD", STATUS_WARN, "could not query WFDIEs", str(exc))

    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return Check("wpa_supplicant WFD", STATUS_OK, "wpa_supplicant accepts Wi-Fi Display IEs", text)
    return Check(
        "wpa_supplicant WFD",
        STATUS_WARN,
        "WFDIE query failed; supplicant may lack CONFIG_WIFI_DISPLAY",
        text,
    )


def _python_check() -> Check:
    return Check(
        "python",
        STATUS_OK,
        "runtime",
        f"{platform.python_version()} on {platform.system()} {platform.release()}",
    )


def run_diagnostics() -> DiagnosticReport:
    checks = [
        _python_check(),
        _command_check("ffmpeg", "video/audio transcoding", required=True),
        _command_check("wf-recorder", "Wayland/wlroots screen capture"),
        _portal_process_check(),
        _command_check("pactl", "PulseAudio/PipeWire-Pulse audio monitor detection"),
        _command_check("xrandr", "X11 monitor detection fallback"),
        _command_check("nmcli", "NetworkManager Wi-Fi Direct control"),
        _command_check("iw", "kernel Wi-Fi interface inspection"),
        _command_check("wpa_cli", "active Wi-Fi Direct scan/control"),
        _command_check("gdbus", "passive wpa_supplicant D-Bus capability checks"),
        _command_check("gst-launch-1.0", "optional future WFD GStreamer pipeline"),
        _command_check("gst-inspect-1.0", "optional future WFD codec inspection"),
        _python_module_check("dbus_next", "WFD portal capture control plane for KDE/GNOME Wayland"),
        _ffmpeg_encoders(),
        _display_capture_check(),
        _audio_check(),
        _nmcli_check(),
        _iw_p2p_check(),
        _supplicant_capability_check(),
        _supplicant_wfd_check(),
    ]

    by_name = {check.name: check for check in checks}
    network_hw_ok = (
        by_name.get("NetworkManager", Check("", STATUS_SKIP, "")).status == STATUS_OK
        or by_name.get("iw P2P", Check("", STATUS_SKIP, "")).status == STATUS_OK
    )
    media_ok = (
        by_name.get("ffmpeg encoders", Check("", STATUS_SKIP, "")).status == STATUS_OK
        and by_name.get("screen capture", Check("", STATUS_SKIP, "")).status == STATUS_OK
    )
    wfd_candidate = network_hw_ok and media_ok

    if wfd_candidate:
        summary = (
            "Miracast/WFD looks possible via NetworkManager; raw supplicant "
            "access is optional for this backend."
        )
    elif network_hw_ok:
        summary = "Wi-Fi Direct hardware is visible, but media/capture readiness is incomplete."
    else:
        summary = "Miracast/WFD is not confirmed yet; check warn/fail rows above."
    return DiagnosticReport(checks=checks, wfd_candidate=wfd_candidate, summary=summary)


def print_report(report: DiagnosticReport) -> None:
    print("[FluxCast Doctor] System capability report")
    print(f"[FluxCast Doctor] {report.summary}")
    print()
    print(f"  {'Status':<6} {'Check':<22} Details")
    print(f"  {'-' * 6} {'-' * 22} {'-' * 42}")
    for check in report.checks:
        detail = check.detail.replace("\n", " | ")
        message = check.message
        if detail:
            message = f"{message} ({detail})"
        print(f"  {check.status:<6} {check.name:<22} {message}")
