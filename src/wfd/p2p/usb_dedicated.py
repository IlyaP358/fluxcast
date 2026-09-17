"""Dedicated wpa_supplicant on a USB wifi iface for Miracast P2P.

When the USB adapter shares NetworkManager's wpa_supplicant with an Intel
STA (AX201), P2P groups always form on the Intel phy. Taking the USB iface
away from NM and running our own wpa_supplicant gives the dongle its own
p2p_device_address so groups land on the USB phy.

Protocol note (Wi-Fi Direct Group Formation): a netdev with type P2P-GO is
NOT a completed group. wpa creates a *pending* GO iface before GO Neg
finishes. Success requires GO-NEG completion and P2P-GROUP-STARTED (channel
+ SSID / client COMPLETED). Our captures showed USB often sends GO Neg
Request with no Response, leaving a zombie pending GO (oper=down).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import WFDNotReady
from ..constants import WFD_RTSP_PORT
from .wpas_ip import configure_ip, get_p2p_role, mark_unmanaged

_CTRL = Path("/tmp/fluxcast-usb-wpa")
_CONF = _CTRL / "wpa.conf"
_PID = _CTRL / "wpa.pid"
_LOG = _CTRL / "wpa.log"


def _is_usb_wifi(iface: str) -> bool:
    if "u" in iface.split("s")[-1]:
        return True
    dev = Path(f"/sys/class/net/{iface}/device")
    try:
        target = os.readlink(dev)
    except OSError:
        return False
    return "usb" in target


def _run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", *cmd], capture_output=True, text=True, timeout=timeout
    )


def _wpa_cli(iface: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return _run(
        ["wpa_cli", "-p", str(_CTRL), "-i", iface, *args], timeout=timeout
    )


def ensure_dedicated_wpa(iface: str) -> None:
    """nmcli unmanaged + dedicated wpa_supplicant on iface."""
    _CTRL.mkdir(parents=True, exist_ok=True)
    _CONF.write_text(
        "\n".join(
            [
                f"ctrl_interface={_CTRL}",
                "ctrl_interface_group=0",
                "device_name=kenya-usb",
                "p2p_go_intent=0",
                "driver_param=use_p2p_group_interface=1",
                "",
            ]
        )
    )
    _run(["nmcli", "device", "set", iface, "managed", "no"], timeout=5.0)
    p2p_dev = f"p2p-dev-{iface}"
    _run(["nmcli", "device", "set", p2p_dev, "managed", "no"], timeout=5.0)
    # Parent must be IFF_UP or nl80211 ops fail with Network is down.
    _run(["ip", "link", "set", iface, "up"], timeout=5.0)

    if _PID.exists():
        try:
            pid = int(_PID.read_text().strip())
            os.kill(pid, 0)
            ping = _wpa_cli(iface, "ping", timeout=3.0)
            if ping.returncode == 0 and "PONG" in (ping.stdout or ""):
                _run(["ip", "link", "set", iface, "up"], timeout=5.0)
                return
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass

    for path in Path("/sys/class/net").glob("p2p-*"):
        _run(["iw", "dev", path.name, "del"], timeout=3.0)

    _run(
        [
            "wpa_supplicant",
            "-B",
            "-i",
            iface,
            "-c",
            str(_CONF),
            "-P",
            str(_PID),
            "-f",
            str(_LOG),
        ],
        timeout=10.0,
    )
    time.sleep(1.0)
    ping = _wpa_cli(iface, "ping", timeout=3.0)
    if ping.returncode != 0 or "PONG" not in (ping.stdout or ""):
        raise WFDNotReady(
            f"Dedicated USB wpa_supplicant failed to start on {iface}: "
            f"{(ping.stderr or ping.stdout).strip()}"
        )
    _wpa_cli(iface, "set", "wifi_display", "1", timeout=3.0)
    _run(["ip", "link", "set", iface, "up"], timeout=5.0)


def _phy_of(iface: str) -> str:
    link = Path(f"/sys/class/net/{iface}/phy80211")
    try:
        return Path(os.readlink(link)).name
    except OSError:
        return ""


def _iw_info(name: str) -> str:
    try:
        return subprocess.run(
            ["iw", "dev", name, "info"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _is_live_group(name: str, role: str) -> bool:
    """True only for a finished group BSS/client, not a pending GO shell."""
    info = _iw_info(name)
    if role == "P2P-GO":
        # Spec-complete GO has an operating channel and usually an SSID.
        if "type P2P-GO" not in info:
            return False
        if "channel" not in info and "ssid" not in info:
            return False
        dump = subprocess.run(
            ["iw", "dev", name, "station", "dump"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout
        return "Station" in dump
    if role == "P2P-client":
        if "type P2P-client" not in info:
            return False
        oper = Path(f"/sys/class/net/{name}/operstate").read_text().strip()
        if oper == "down":
            return False
        # Prefer wpa COMPLETED when ctrl exists.
        st = _wpa_cli(name, "status", timeout=3.0)
        if st.returncode == 0 and "wpa_state=COMPLETED" in (st.stdout or ""):
            return True
        return "ssid" in info or oper in ("up", "unknown")
    return False


def _is_zombie_pending_go(name: str) -> bool:
    info = _iw_info(name)
    return (
        "type P2P-GO" in info
        and "channel" not in info
        and "ssid" not in info
    )


def _teardown_usb_groups(usb_phy: str) -> None:
    for path in Path("/sys/class/net").glob("p2p-*"):
        if _phy_of(path.name) == usb_phy:
            _run(["iw", "dev", path.name, "del"], timeout=3.0)


def _wpa_log_tail(n: int = 80) -> str:
    try:
        if not _LOG.exists():
            return ""
        lines = _LOG.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _neg_outcome_from_log(since_len: int) -> str:
    """Return 'success', 'failure', or 'pending' from new wpa.log lines."""
    try:
        text = _LOG.read_text(errors="replace")
    except OSError:
        return "pending"
    new = text[since_len:] if since_len < len(text) else text
    if "P2P-GROUP-STARTED" in new or "P2P-GO-NEG-SUCCESS" in new:
        return "success"
    if (
        "P2P-GO-NEG-FAILURE" in new
        or "P2P-GROUP-FORMATION-FAILURE" in new
        or (
            "TX_WAIT_EXPIRE" in new
            and "Sending GO Negotiation Request" in new
            and "GO Negotiation Response" not in new
        )
    ):
        return "failure"
    # No GO Neg Response while stuck sending requests
    if "Sending GO Negotiation Request" in new and "GO Negotiation Response" not in new:
        if "TX_WAIT_EXPIRE" in new or "Action frame sequence done" in new:
            # Could still be retrying
            pass
    return "pending"


def connect_usb_dedicated(
    iface: str,
    peer_mac: str,
    go_intent: int = 0,
    rtsp_port: int = WFD_RTSP_PORT,
) -> str:
    """Form a finished P2P group on the USB phy; return data iface with IP.

    Tries client role (go_intent=0) first when intent is 0 — Miracast sources
    commonly let the sink be GO. Falls back to go_intent=15 if requested.
    """
    if not _is_usb_wifi(iface):
        raise WFDNotReady(f"{iface} does not look like a USB wifi device")

    ensure_dedicated_wpa(iface)
    peer = peer_mac.lower()
    usb_phy = _phy_of(iface)
    print(
        f"[FluxCast WFD] USB dedicated P2P on {iface} (phy={usb_phy}); "
        f"AX201 left on NetworkManager for internet"
    )

    _wpa_cli(iface, "set", "wifi_display", "1")
    port = max(1, min(int(rtsp_port), 65535))
    dev_info = f"{0x0011:04x}{port:04x}00c8"
    _wpa_cli(iface, "wfd_subelem_set", "0", dev_info)

    # Listen on social ch6 — matches hotyeah listen_freq=2437 in captures.
    _wpa_cli(iface, "set", "p2p_listen_reg_class", "81")
    _wpa_cli(iface, "set", "p2p_listen_channel", "6")
    _wpa_cli(iface, "set", "p2p_oper_reg_class", "81")
    _wpa_cli(iface, "set", "p2p_oper_channel", "6")

    intents = [go_intent]
    if go_intent == 0:
        intents.append(15)  # fallback: try become GO
    elif go_intent == 15:
        intents.append(0)  # fallback: try become client

    last_err = "USB P2P group formation failed"
    for intent in intents:
        _teardown_usb_groups(usb_phy)
        _wpa_cli(iface, "p2p_cancel", timeout=3.0)
        _wpa_cli(iface, "set", "p2p_go_intent", str(intent))
        _wpa_cli(iface, "p2p_find", "12")
        found = False
        for _ in range(20):
            peers = _wpa_cli(iface, "p2p_peers", timeout=5.0)
            if peer in (peers.stdout or "").lower():
                found = True
                break
            time.sleep(1)
        if not found:
            last_err = (
                f"Peer {peer_mac} not seen on dedicated USB P2P find. "
                "Wake the sink and retry."
            )
            continue

        log_mark = _LOG.stat().st_size if _LOG.exists() else 0
        print(
            f"[FluxCast WFD] USB p2p_connect {peer_mac} go_intent={intent} "
            f"freq=2437 (protocol: wait for GO-NEG + GROUP-STARTED)..."
        )
        # Prefer social channel peer is listening on.
        conn = _wpa_cli(
            iface,
            "p2p_connect",
            peer,
            "pbc",
            f"go_intent={intent}",
            "freq=2437",
            timeout=10.0,
        )
        conn_out = (conn.stdout or conn.stderr or "").strip()
        if conn.returncode != 0 or "FAIL" in conn_out.upper():
            # join existing GO if peer is already GO
            print(f"[FluxCast WFD] p2p_connect failed ({conn_out}); trying join")
            conn = _wpa_cli(
                iface, "p2p_connect", peer, "pbc", "join", timeout=10.0
            )
            conn_out = (conn.stdout or conn.stderr or "").strip()
            if conn.returncode != 0 or "FAIL" in conn_out.upper():
                last_err = f"USB p2p_connect failed: {conn_out}"
                continue

        deadline = time.time() + 40.0
        saw_pending_go = ""
        while time.time() < deadline:
            outcome = _neg_outcome_from_log(log_mark)
            if outcome == "failure":
                print(
                    "[FluxCast WFD] GO Negotiation/formation failed per wpa log "
                    "(no Neg Response / TX expire) — tearing pending GO"
                )
                _teardown_usb_groups(usb_phy)
                last_err = (
                    "Wi-Fi Direct GO Negotiation did not complete on USB "
                    "(no GO Neg Response / GROUP-STARTED). "
                    "AX201 path completes this handshake; SoftMAC USB does not."
                )
                break

            for path in Path("/sys/class/net").glob("p2p-*"):
                name = path.name
                if _phy_of(name) != usb_phy:
                    continue
                info = _iw_info(name)
                if "type P2P-GO" in info:
                    if _is_zombie_pending_go(name):
                        saw_pending_go = name
                        continue
                    if _is_live_group(name, "P2P-GO"):
                        print(f"[FluxCast WFD] USB GO {name}: live BSS + STA")
                        mark_unmanaged(name)
                        configure_ip(name, peer_mac, "P2P-GO", iface)
                        return name
                if "type P2P-client" in info and _is_live_group(name, "P2P-client"):
                    print(f"[FluxCast WFD] USB client {name}: associated")
                    mark_unmanaged(name)
                    configure_ip(name, peer_mac, "P2P-client", iface)
                    return name

            if outcome == "success":
                # GROUP-STARTED may race ahead of station dump; keep waiting briefly
                time.sleep(0.5)
                continue
            time.sleep(0.5)
        else:
            if saw_pending_go:
                last_err = (
                    f"Pending P2P-GO {saw_pending_go} never became a live BSS "
                    f"(no channel/SSID) — GO Negotiation likely incomplete"
                )
                _teardown_usb_groups(usb_phy)
            continue

        # failure branch already tore down; try next intent
        continue

    raise WFDNotReady(last_err)


def release_usb_dedicated(iface: Optional[str] = None) -> None:
    """Best-effort teardown of dedicated USB wpa + group ifaces."""
    for path in Path("/sys/class/net").glob("p2p-*"):
        _run(["iw", "dev", path.name, "del"], timeout=3.0)
    if _PID.exists():
        try:
            pid = int(_PID.read_text().strip())
            _run(["kill", str(pid)], timeout=3.0)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
        try:
            _PID.unlink()
        except OSError:
            pass
    if iface:
        _run(["nmcli", "device", "set", iface, "managed", "yes"], timeout=5.0)
