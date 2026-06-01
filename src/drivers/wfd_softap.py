"""
WFD Autonomous P2P Group Owner backend via hostapd.

Creates a Wi-Fi Direct AP with P2P Group Owner + WFD IEs injected into
beacon/probe-response frames. Samsung and other P2P-only TVs see this as
a valid Miracast Group Owner and connect, no kernel P2P!!!!!!!!!! (Wi-Fi Direct)
support required from the driver.

Internet stays connected: the driver creates a virtual AP interface
(fluxcast_ap0) on the same physical radio, so the main Wi-Fi interface
keeps its router connection. If the hardware does not support concurrent
STA+AP, it falls back to taking over the physical interface (internet
unavailable during streaming — user is warned before this happens).

Requirements: hostapd
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from typing import Optional

from wfd import _wfd_source_ie, WFD_RTSP_PORT

# ── SoftAP network config ─────────────────────────────────────────────────────
_AP_IP = "192.168.49.1"
_AP_NETMASK = "24"
_DHCP_RANGE_START = "192.168.49.2"
_DHCP_RANGE_END = "192.168.49.10"
_AP_CHANNEL = 6 # 2.4 GHz channel 6
_AP_SSID = "DIRECT-FC"


# ── IE construction ───────────────────────────────────────────────────────────
def _get_mac(iface: str) -> bytes:
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return bytes(int(b, 16) for b in f.read().strip().split(":"))
    except (OSError, ValueError):
        return bytes(6)


def _p2p_attr(attr_id: int, data: bytes) -> bytes:
    return bytes([attr_id]) + struct.pack("<H", len(data)) + data


def _build_p2p_ie(mac: bytes) -> bytes:
    """P2P IE advertising Autonomous Group Owner capability."""
    OUI = bytes([0x50, 0x6F, 0x9A, 0x09])

    # P2P Capability: Device=0x25, Group=0x21 (GO bit + Persistent bit)
    cap = _p2p_attr(0x02, bytes([0x25, 0x21]))

    # P2P Device Info
    primary_dev_type = bytes([0x00, 0x0A, 0x00, 0x50, 0xF2, 0x04, 0x00, 0x05])
    name = "FluxCast".encode()
    wps_name = struct.pack(">HH", 0x1011, len(name)) + name
    dev_info = _p2p_attr(0x0D,
        mac +
        struct.pack(">H", 0x0188) + # config methods: push-button + display
        primary_dev_type +
        bytes([0x00]) + # no secondary device types
        wps_name
    )

    payload = OUI + cap + dev_info
    return bytes([0xDD, len(payload)]) + payload


def _build_wfd_ie(rtsp_port: int) -> bytes:
    """Wrap WFD subelements from wfd.py in a vendor IE header."""
    WFD_OUI = bytes([0x50, 0x6F, 0x9A, 0x0A])
    subelems = _wfd_source_ie(rtsp_port)
    payload = WFD_OUI + subelems
    return bytes([0xDD, len(payload)]) + payload


def _vendor_elements_hex(iface: str, rtsp_port: int) -> str:
    mac = _get_mac(iface)
    return (_build_p2p_ie(mac) + _build_wfd_ie(rtsp_port)).hex()


def _hostapd_conf(iface: str, ctrl_path: str, vendor_hex: str) -> str:
    return "\n".join([
        f"interface={iface}",
        "driver=nl80211",
        f"ssid={_AP_SSID}",
        "hw_mode=g",
        f"channel={_AP_CHANNEL}",
        "ieee80211n=1",
        "wmm_enabled=1",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        "wpa_passphrase=fluxcast1",
        "wps_state=2",
        "eap_server=1",
        "device_name=FluxCast",
        "manufacturer=FluxCast",
        "model_name=FluxCast WFD Source",
        "model_number=1",
        "serial_number=1",
        "device_type=10-0050F204-5",
        "os_version=01020300",
        "config_methods=push_button virtual_push_button",
        f"ctrl_interface={ctrl_path}",
        "ctrl_interface_group=0",
        f"vendor_elements={vendor_hex}",
        "",
    ])


def _dnsmasq_conf(iface: str) -> str:
    return "\n".join([
        f"interface={iface}",
        "bind-interfaces",
        "except-interface=lo",
        "port=0",
        f"dhcp-range={_DHCP_RANGE_START},{_DHCP_RANGE_END},255.255.255.0,1h",
        f"dhcp-option=3,{_AP_IP}",   # default gateway
        "no-resolv",
        "no-poll",
        "log-dhcp",
        "",
    ])

_VIRTUAL_AP_IFACE = "fluxcast_ap0"

_PATCHED_HOSTAPD = os.path.join(os.path.dirname(__file__), "bin", "hostapd")


def _hostapd_bin() -> str:
    """Use patched binary if available, fall back to system hostapd."""
    if os.path.isfile(_PATCHED_HOSTAPD) and os.access(_PATCHED_HOSTAPD, os.X_OK):
        return _PATCHED_HOSTAPD
    return "hostapd"


def _run(cmd: list[str], check: bool = True, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def _nm_set_managed(iface: str, managed: bool) -> None:
    state = "yes" if managed else "no"
    try:
        _run(["nmcli", "device", "set", iface, "managed", state])
    except Exception:
        pass


def _ip_addr_add(iface: str) -> None:
    _run(["ip", "addr", "flush", "dev", iface], check=False)
    _run(["ip", "addr", "add", f"{_AP_IP}/{_AP_NETMASK}", "dev", iface])
    _run(["ip", "link", "set", iface, "up"])


def _ip_addr_flush(iface: str) -> None:
    try:
        _run(["ip", "addr", "flush", "dev", iface], check=False)
    except Exception:
        pass


def _get_phy(iface: str) -> Optional[str]:
    """Return the physical radio name (e.g. 'phy0') for a Wi-Fi interface."""
    try:
        with open(f"/sys/class/net/{iface}/phy80211/name") as f:
            return f.read().strip()
    except OSError:
        pass
    try:
        result = _run(["iw", "dev", iface, "info"], check=False)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("wiphy "):
                return "phy" + line.split()[1]
    except Exception:
        pass
    return None


def _try_create_virtual_ap(phy: str, vif: str) -> bool:
    """Create a virtual AP interface on phy. Returns True on success."""
    _run(["iw", "dev", vif, "del"], check=False)
    result = _run(["iw", "phy", phy, "interface", "add", vif, "type", "__ap"], check=False)
    if result.returncode != 0:
        return False
    # Give the kernel a moment to expose the new netdev
    time.sleep(0.3)
    return os.path.exists(f"/sys/class/net/{vif}")


def _delete_virtual_ap(vif: str) -> None:
    try:
        _run(["iw", "dev", vif, "del"], check=False)
    except Exception:
        pass


# ── Main driver ───────────────────────────────────────────────────────────────
class WFDSoftAPDriver:

    def __init__(self, iface: str, rtsp_port: int = WFD_RTSP_PORT) -> None:
        self._iface = iface # physical Wi-Fi interface (wlan0 / wld0)
        self._rtsp_port = rtsp_port
        self._ap_iface: Optional[str] = None # actual interface hostapd runs on
        self._virtual_ap: bool = False # True = created virtual, False = took over physical
        self._tmpdir: Optional[str] = None
        self._hostapd: Optional[subprocess.Popen] = None
        self._dnsmasq: Optional[subprocess.Popen] = None
        self._ctrl_path: Optional[str] = None
        self._pbc_thread: Optional[threading.Thread] = None
        atexit.register(self.stop)

    # ── Public API ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._check_requirements()

        phy = _get_phy(self._iface)
        if phy and _try_create_virtual_ap(phy, _VIRTUAL_AP_IFACE):
            self._ap_iface = _VIRTUAL_AP_IFACE
            self._virtual_ap = True

            _nm_set_managed(_VIRTUAL_AP_IFACE, managed=False)
            print(f"[WFD SoftAP] Virtual AP interface {_VIRTUAL_AP_IFACE} created on {phy}.")
            print("[WFD SoftAP] Internet via Wi-Fi stays connected.")
        else:
            self._ap_iface = self._iface
            self._virtual_ap = False
            print(f"[WFD SoftAP] Hardware does not support concurrent STA+AP on {self._iface}.")
            print("[WFD SoftAP] WARNING: Wi-Fi internet will be unavailable while streaming.")
            _nm_set_managed(self._iface, managed=False)

        self._tmpdir = tempfile.mkdtemp(prefix="fluxcast_softap_")
        os.chmod(self._tmpdir, 0o755)   # allow non-root to read logs
        self._ctrl_path = os.path.join(self._tmpdir, "ctrl")
        os.makedirs(self._ctrl_path, exist_ok=True)

        vendor_hex = _vendor_elements_hex(self._iface, self._rtsp_port)
        hostapd_conf_path = os.path.join(self._tmpdir, "hostapd.conf")
        dnsmasq_conf_path = os.path.join(self._tmpdir, "dnsmasq.conf")
        dnsmasq_pid_path  = os.path.join(self._tmpdir, "dnsmasq.pid")

        with open(hostapd_conf_path, "w") as f:
            f.write(_hostapd_conf(self._ap_iface, self._ctrl_path, vendor_hex))
        with open(dnsmasq_conf_path, "w") as f:
            f.write(_dnsmasq_conf(self._ap_iface))

        _ip_addr_add(self._ap_iface)

        hostapd_log = os.path.join(self._tmpdir, "hostapd.log")
        self._hostapd = subprocess.Popen(
            [_hostapd_bin(), "-d", hostapd_conf_path],
            stdout=open(hostapd_log, "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2.0)
        if self._hostapd.poll() is not None:
            try:
                with open(hostapd_log) as f:
                    detail = f.read().strip()[-600:]
            except OSError:
                detail = "(no log)"
            raise RuntimeError(
                f"hostapd exited immediately.\n{detail}\n"
                f"Check that {self._ap_iface} supports AP mode and no other process uses it."
            )
        print(f"[WFD SoftAP] hostapd log: {hostapd_log}")

        dnsmasq_log = os.path.join(self._tmpdir, "dnsmasq.log")
        self._dnsmasq = subprocess.Popen(
            ["dnsmasq", "--no-daemon", f"--conf-file={dnsmasq_conf_path}",
             f"--pid-file={dnsmasq_pid_path}"],
            stdout=open(dnsmasq_log, "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)
        if self._dnsmasq.poll() is not None:
            try:
                detail = open(dnsmasq_log).read().strip()[-400:]
            except OSError:
                detail = "(no log)"
            raise RuntimeError(f"dnsmasq failed to start.\n{detail}")
        print(f"[WFD SoftAP] dnsmasq log: {dnsmasq_log}")

        self._pbc_thread = threading.Thread(target=self._pbc_loop, daemon=True)
        self._pbc_thread.start()

        print(f"[WFD SoftAP] AP '{_AP_SSID}' active on {self._ap_iface} "
              f"(channel {_AP_CHANNEL}).")
        print("[WFD SoftAP] Put TV in Screen Mirror / Wireless Display mode.")

    def stop(self) -> None:
        for proc in (self._hostapd, self._dnsmasq):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        if self._ap_iface:
            _ip_addr_flush(self._ap_iface)

        if self._virtual_ap:
            _delete_virtual_ap(_VIRTUAL_AP_IFACE)
        else:
            _nm_set_managed(self._iface, managed=True)

        if self._tmpdir and os.path.isdir(self._tmpdir):
            try:
                shutil.rmtree(self._tmpdir)
            except Exception:
                pass

        self._hostapd = None
        self._dnsmasq = None
        self._ap_iface = None
        self._tmpdir = None
        if self._virtual_ap:
            print("[WFD SoftAP] Stopped; virtual interface removed.")
        else:
            print("[WFD SoftAP] Stopped; Wi-Fi interface returned to NetworkManager.")

    # ── Internal ──────────────────────────────────────────────────────────────
    def _check_requirements(self) -> None:
        missing = [t for t in ("hostapd", "dnsmasq", "ip", "nmcli") if not shutil.which(t)]
        if missing:
            raise RuntimeError(
                f"Missing required tools: {', '.join(missing)}.\n"
                "  Arch:   sudo pacman -S hostapd\n"
                "  Fedora: sudo dnf install hostapd\n"
                "  Ubuntu: sudo apt install hostapd"
            )

    def _pbc_loop(self) -> None:
        """Keep WPS PBC window open by re-triggering it every 110 seconds."""
        while self._hostapd and self._hostapd.poll() is None:
            try:
                _run(
                    ["hostapd_cli", "-p", self._ctrl_path, "wps_pbc", "any"],
                    check=False,
                    timeout=5.0,
                )
            except Exception:
                pass
            time.sleep(110)


# ── Session entry point ───────────────────────────────────────────────────────
def run_softap_session(args) -> None:
    from wfd import (
        WFD_RTSP_PORT,
        WFDMediaConfig,
        WFDNotReady,
        WFDRTSPServer,
        _default_wifi_interface,
        _is_hyprland_session,
        _is_wayland_session,
    )

    iface = getattr(args, "wfd_interface", None) or _default_wifi_interface()
    if not iface:
        raise WFDNotReady(
            "Could not detect a Wi-Fi interface. Use --wfd-interface wlan0."
        )
    rtsp_port = getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT)

    test_pattern = getattr(args, "wfd_test_pattern", False)
    monitor = None
    selected_backend = getattr(args, "wfd_capture_backend", "auto")
    portal_mode = selected_backend == "portal" or (
        selected_backend == "auto"
        and _is_wayland_session()
        and not _is_hyprland_session()
    )
    if not test_pattern:
        if portal_mode:
            print("[WFD SoftAP] Portal backend: monitor selection will be done "
                  "in the desktop portal dialog.")
        else:
            wfd_monitor_name = getattr(args, "wfd_monitor", None)
            if wfd_monitor_name:
                from capture import gather_monitors
                all_monitors = gather_monitors()
                monitor = next((m for m in all_monitors if m.name == wfd_monitor_name), None)
                if monitor is None:
                    available = ", ".join(m.name for m in all_monitors) or "none"
                    raise WFDNotReady(
                        f"Monitor '{wfd_monitor_name}' not found. Available: {available}"
                    )
            else:
                from capture import prompt_monitor
                monitor = prompt_monitor()

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=args.output_res,
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=getattr(args, "wfd_no_audio", False),
        test_pattern=test_pattern,
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        media_pipeline=getattr(args, "wfd_media_pipeline", "auto"),
        capture_backend=selected_backend,
    )

    rtsp = WFDRTSPServer(media_config=media_config, port=rtsp_port)
    driver = WFDSoftAPDriver(iface=iface, rtsp_port=rtsp_port)

    try:
        rtsp.start()
        driver.start()
        print("[WFD SoftAP] Waiting for TV RTSP/WFD session. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[WFD SoftAP] Stopping session...")
    finally:
        rtsp.stop_all_media()
        rtsp.stop()
        driver.stop()
