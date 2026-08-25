import json
import importlib.util
import ipaddress
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Optional

from wfd.wf_recorder import find_wf_recorder


STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

# RTSP port advertised in the WFD IEs; must reach the receiver for streaming.
WFD_RTSP_PORT = 7236

# NetworkManager brings Wi-Fi Direct (P2P) groups up on this hardcoded subnet.
# An existing LAN/VPN/docker interface that overlaps it breaks the P2P session
# in a way that is hard to diagnose, so --doctor surfaces the clash.
WFD_P2P_SUBNET = "192.168.49.0/24"


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


def _wf_recorder_check() -> Check:
    path = shutil.which("wf-recorder")
    if not path:
        return Check(
            "wf-recorder", STATUS_WARN, "Wayland/wlroots screen capture",
            "not found in PATH; install wf-recorder",
        )
    if find_wf_recorder():
        return Check("wf-recorder", STATUS_OK, "Wayland/wlroots screen capture", path)
    return Check(
        "wf-recorder", STATUS_WARN,
        "wf-recorder was found but could not be executed",
        "the AppImage wrapper needs the system wf-recorder package; install wf-recorder",
    )


def _gst_element_check(element: str, purpose: str, install_hint: str) -> Check:
    gst_inspect = shutil.which("gst-inspect-1.0")
    if not gst_inspect:
        return Check(
            f"gst element {element}",
            STATUS_WARN,
            f"{purpose} could not be verified",
            f"gst-inspect-1.0 not found; {install_hint}",
        )

    try:
        result = _run([gst_inspect, element], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            f"gst element {element}",
            STATUS_WARN,
            f"{purpose} could not be verified",
            str(exc),
        )

    if result.returncode == 0:
        return Check(
            f"gst element {element}",
            STATUS_OK,
            purpose,
            element,
        )

    detail = (result.stderr or result.stdout).strip()
    if detail:
        detail = detail.replace("\n", " | ") + "; " + install_hint
    else:
        detail = install_hint
    return Check(
        f"gst element {element}",
        STATUS_WARN,
        f"{purpose} is missing",
        detail,
    )


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
    wf_recorder = find_wf_recorder()
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


def _iw_phy_p2p_facts() -> tuple[Optional[bool], Optional[bool]]:
    """
    Gets P2P capability and STA/P2P concurrency via `iw phy` (no root).
    Returns (bool/None, bool/None); both are None if parsing fails.
    """
    if not shutil.which("iw"):
        return (None, None)
    try:
        result = _run(["iw", "phy"], timeout=4.0)
    except (OSError, subprocess.TimeoutExpired):
        return (None, None)
    if result.returncode != 0:
        return (None, None)
    text = result.stdout

    p2p_capable = bool(re.search(r"P2P-(client|GO|device)", text))

    # Capture the "valid interface combinations:" block: lines indented deeper
    # than the header, until the section ends.
    combo_lines: list[str] = []
    capturing = False
    header_indent = 0
    for line in text.splitlines():
        if "valid interface combinations:" in line:
            capturing = True
            header_indent = len(line) - len(line.lstrip())
            continue
        if capturing:
            if not line.strip():
                continue
            if (len(line) - len(line.lstrip())) <= header_indent:
                break
            combo_lines.append(line)
    # Each combination is a '*'-led entry (may wrap onto continuation lines).
    # Concurrent if a single entry allows both a managed and a P2P interface.
    concurrent = any(
        "managed" in chunk and re.search(r"P2P-(client|GO|device)", chunk)
        for chunk in "\n".join(combo_lines).split("*")
    )
    return (p2p_capable, concurrent)


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
        # No P2P-device instance: explain why and what to do, using the adapter's
        # advertised capability instead of a bare "no P2P-device shown".
        p2p_capable, concurrent = _iw_phy_p2p_facts()
        if p2p_capable is False:
            return Check(
                "iw P2P", STATUS_WARN,
                "this Wi-Fi adapter's driver does not support Wi-Fi Direct (P2P)",
                "WFD needs a P2P-capable adapter (most internal Wi-Fi, or a P2P-capable "
                "USB dongle).",
            )
        if concurrent is False:
            return Check(
                "iw P2P", STATUS_WARN,
                "this adapter can't run a Wi-Fi connection and Wi-Fi Direct at the same time",
                "disconnect Wi-Fi before starting WFD (keep Ethernet for internet), or use an "
                "adapter whose driver supports concurrent STA + P2P.",
            )
        return Check("iw P2P", STATUS_WARN, "Wi-Fi interfaces exist, but no P2P-device was shown", output)
    return Check("iw P2P", STATUS_WARN, "no Wi-Fi interfaces were shown", output)


def _subnet_conflict_check() -> Check:
    """Warn when an existing interface overlaps the Wi-Fi Direct P2P subnet.

    NetworkManager provisions Wi-Fi Direct groups on WFD_P2P_SUBNET. If a LAN,
    VPN, or docker interface already occupies that range, the P2P session fails
    in a way that is hard to diagnose, so surface the overlap here. This is a
    read-only check; it never changes any interface.
    """
    name = "P2P subnet"
    if not shutil.which("ip"):
        return Check(name, STATUS_SKIP, "ip was not found", "cannot inspect interface addresses")

    try:
        result = _run(["ip", "-j", "addr"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, STATUS_WARN, "could not query interface addresses", str(exc))

    if result.returncode != 0:
        return Check(name, STATUS_WARN, "ip addr query failed", (result.stderr or "").strip())

    try:
        interfaces = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return Check(name, STATUS_WARN, "could not parse ip addr output", str(exc))

    p2p_net = ipaddress.ip_network(WFD_P2P_SUBNET)
    conflicts = []
    for iface in interfaces:
        dev = iface.get("ifname", "?")
        # Wi-Fi Direct interfaces are expected to live on this subnet; a clash is
        # only meaningful when some *other* interface already occupies the range.
        if "p2p" in dev.lower():
            continue
        for addr in iface.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            local, prefixlen = addr.get("local"), addr.get("prefixlen")
            if local is None or prefixlen is None:
                continue
            try:
                net = ipaddress.ip_network(f"{local}/{prefixlen}", strict=False)
            except ValueError:
                continue
            if net.overlaps(p2p_net):
                conflicts.append(f"{dev}={local}/{prefixlen}")

    if conflicts:
        return Check(
            name,
            STATUS_WARN,
            f"an existing interface overlaps the Wi-Fi Direct subnet {WFD_P2P_SUBNET}",
            "; ".join(conflicts)
            + f"; move it off {WFD_P2P_SUBNET} (LAN/VPN/docker) to avoid a P2P clash",
        )
    return Check(
        name,
        STATUS_OK,
        f"no interface overlaps the Wi-Fi Direct subnet {WFD_P2P_SUBNET}",
        "",
    )


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


def _ufw_check() -> Optional[Check]:
    if not shutil.which("ufw"):
        return None

    try:
        result = _run(["ufw", "status"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("firewall (ufw)", STATUS_WARN, "could not query ufw status", str(exc))

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        # ufw status needs root; skip rather than warn when privileges are missing.
        if re.search(r"need to be root|permission denied", output, re.IGNORECASE):
            return None
        return Check("firewall (ufw)", STATUS_WARN, "ufw status query failed", output)

    if re.search(r"Status:\s*inactive", output, re.IGNORECASE):
        return Check("firewall (ufw)", STATUS_OK, "ufw is inactive; port not blocked", output)

    # Only treat the port as open when an explicit ALLOW rule covers its TCP
    # entry; a bare port match would also accept DENY/REJECT or udp-only rules.
    if re.search(rf"\b{WFD_RTSP_PORT}/tcp\b[^\n]*\bALLOW\b", output, re.IGNORECASE):
        return Check(
            "firewall (ufw)",
            STATUS_OK,
            f"ufw is active and allows port {WFD_RTSP_PORT}",
            output,
        )
    return Check(
        "firewall (ufw)",
        STATUS_WARN,
        f"ufw is active but port {WFD_RTSP_PORT} is not allowed",
        f"open it with: sudo ufw allow {WFD_RTSP_PORT}/tcp",
    )


def _firewalld_check() -> Optional[Check]:
    if not shutil.which("firewall-cmd"):
        return None

    try:
        state = _run(["firewall-cmd", "--state"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("firewall (firewalld)", STATUS_WARN, "could not query firewalld state", str(exc))

    # firewalld gates firewall-cmd through polkit, so on a headless/gated host
    # `--state` and `--query-port` can fail with an authorization error rather
    # than a real answer. Trust only what firewall-cmd literally prints: a
    # non-zero exit is not proof the firewall is down, so an auth failure must
    # never become a definitive OK-or-closed. Report "couldn't verify" instead.
    state_out = state.stdout.strip()
    state_all = (state.stdout + state.stderr).strip()
    if state_out != "running":
        if "not running" in state_all.lower():
            return Check(
                "firewall (firewalld)",
                STATUS_OK,
                "firewalld is not running; port not blocked",
                state_all,
            )
        return Check(
            "firewall (firewalld)",
            STATUS_WARN,
            "could not verify firewalld state (firewall-cmd did not report running/not running)",
            state_all,
        )

    try:
        query = _run(["firewall-cmd", f"--query-port={WFD_RTSP_PORT}/tcp"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("firewall (firewalld)", STATUS_WARN, "could not query firewalld port", str(exc))

    # `--query-port` prints `yes`/`no` (exit 0/1) for a real answer; anything
    # else — empty output, an auth error — means we could not check the port.
    query_out = query.stdout.strip()
    if query_out == "yes":
        return Check(
            "firewall (firewalld)",
            STATUS_OK,
            f"firewalld is running and allows port {WFD_RTSP_PORT}",
            (query.stdout + query.stderr).strip(),
        )
    if query_out == "no":
        return Check(
            "firewall (firewalld)",
            STATUS_WARN,
            f"firewalld is running but port {WFD_RTSP_PORT}/tcp is closed",
            f"open it with: sudo firewall-cmd --add-port={WFD_RTSP_PORT}/tcp --permanent && sudo firewall-cmd --reload",
        )
    return Check(
        "firewall (firewalld)",
        STATUS_WARN,
        f"could not verify whether firewalld allows port {WFD_RTSP_PORT}/tcp",
        (query.stdout + query.stderr).strip(),
    )


def _firewall_check() -> Check:
    """Warn when a host firewall is active but the WFD RTSP port is closed.

    A host may run more than one front-end (e.g. ufw inactive while firewalld
    is active and blocking), so every available probe is consulted and the
    worst-case result is reported rather than the first one found.
    """
    severity = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    results = [check for probe in (_ufw_check, _firewalld_check) if (check := probe()) is not None]
    if results:
        return max(results, key=lambda c: severity.get(c.status, 0))
    return Check(
        "firewall",
        STATUS_SKIP,
        "no supported firewall front-end detected",
        f"checked ufw and firewall-cmd; open port {WFD_RTSP_PORT}/tcp manually if a firewall blocks it",
    )


def _python_check() -> Check:
    return Check(
        "python",
        STATUS_OK,
        "runtime",
        f"{platform.python_version()} on {platform.system()} {platform.release()}",
    )


def run_diagnostics(skip_firewall: bool = False) -> DiagnosticReport:
    firewall_check = (
        Check("firewall", STATUS_SKIP, "disabled by --wfd-no-firewall")
        if skip_firewall
        else _firewall_check()
    )
    checks = [
        _python_check(),
        _command_check("ffmpeg", "video/audio transcoding", required=True),
        _wf_recorder_check(),
        _portal_process_check(),
        _command_check("pactl", "PulseAudio/PipeWire-Pulse audio monitor detection"),
        _command_check("xrandr", "X11 monitor detection fallback"),
        _command_check("nmcli", "NetworkManager Wi-Fi Direct control"),
        _command_check(
            "dnsmasq",
            "NetworkManager P2P DHCP server",
            extra_paths=["/usr/sbin/dnsmasq"],
        ),
        _command_check("iw", "kernel Wi-Fi interface inspection"),
        _command_check("wpa_cli", "active Wi-Fi Direct scan/control"),
        _command_check("gdbus", "passive wpa_supplicant D-Bus capability checks"),
        _command_check("gst-launch-1.0", "optional future WFD GStreamer pipeline"),
        _command_check("gst-inspect-1.0", "optional future WFD codec inspection"),
        _gst_element_check(
            "pipewiresrc",
            "Wayland portal GStreamer capture source",
            "install the GStreamer PipeWire plugin "
            "(Debian/Ubuntu: gstreamer1.0-pipewire; Arch/Fedora: gst-plugin-pipewire)",
        ),
        _gst_element_check(
            "x264enc",
            "Wayland portal GStreamer H.264 encoder",
            "install the GStreamer x264 plugin "
            "(Debian/Ubuntu: gstreamer1.0-plugins-ugly; Arch/Fedora: gst-plugins-ugly)",
        ),
        _python_module_check("dbus_next", "WFD portal capture control plane for KDE/GNOME Wayland"),
        _ffmpeg_encoders(),
        _display_capture_check(),
        _audio_check(),
        _nmcli_check(),
        _subnet_conflict_check(),
        _iw_p2p_check(),
        _supplicant_capability_check(),
        _supplicant_wfd_check(),
        firewall_check,
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
    dnsmasq_ok = by_name.get("dnsmasq", Check("", STATUS_WARN, "")).status == STATUS_OK
    wfd_candidate = network_hw_ok and media_ok and dnsmasq_ok

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
