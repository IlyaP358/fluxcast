"""Dedicated wpa_supplicant on a USB wifi iface for Miracast P2P.

When a USB (or other secondary) adapter shares NetworkManager's
wpa_supplicant with a primary STA radio, P2P groups often form on the STA
phy. Taking the secondary iface away from NM and running a dedicated
wpa_supplicant gives it its own p2p_device_address so groups land on that
phy. Primary STA / internet stays on NetworkManager.

Protocol note (Wi-Fi Direct Group Formation): a netdev with type P2P-GO is
NOT a completed group. wpa creates a *pending* GO iface before GO Neg
finishes. Success requires GO-NEG completion and P2P-GROUP-STARTED (channel
+ SSID / client COMPLETED). Pending GO shells (oper=down, no channel/SSID)
must be torn down, not treated as success.

Formation helpers (listen-freq, listen-then-PD, log outcome) live in
formation.py and are hardware-agnostic; this module only owns the dedicated
USB ctrl socket and phy binding.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import WFDNotReady
from ..constants import WFD_RTSP_PORT
from .formation import (
    describe_neg_log,
    first_quiet_2ghz_oper,
    first_quiet_5ghz_oper,
    parse_iw_survey_busy_ratio,
    freq_to_social_channel,
    is_pending_go_shell,
    neg_outcome_from_log,
    miracast_go_width_flags,
    parse_iw_phy_2ghz_channels,
    parse_iw_phy_5ghz_channels,
    p2p_no_go_freq_ranges,
    parse_iw_phy_caps,
    parse_p2p_peer_info,
    parse_sta_occupied_mhz,
    peer_advertises_5ghz,
    peer_listen_freq,
    peer_wps_not_ready,
    sync_before_go_neg,
    wait_peer_wps_ready,
)
from .wpas_ip import configure_ip, mark_unmanaged

_CTRL = Path("/tmp/fluxcast-usb-wpa")
_CONF = _CTRL / "wpa.conf"
_PID = _CTRL / "wpa.pid"
_LOG = _CTRL / "wpa.log"


def _wpa_bin() -> str:
    """wpa_supplicant for the dedicated USB P2P process.

    Override with FLUXCAST_USB_WPA (absolute path) when a local build is
    needed. Default is PATH so this stays portable; NetworkManager keeps
    the system daemon on the primary STA iface.
    """
    override = os.environ.get("FLUXCAST_USB_WPA", "").strip()
    if override:
        return override
    found = shutil.which("wpa_supplicant")
    return found or "wpa_supplicant"


def _chmod_wpa_log() -> None:
    """wpa runs as root; make the log readable for outcome detection."""
    try:
        if _LOG.exists():
            _run(["chmod", "a+r", str(_LOG)], timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _is_usb_wifi(iface: str) -> bool:
    """True if iface is USB-attached Wi-Fi (bus path or udev-style …uN name)."""
    if "u" in iface.split("s")[-1]:
        return True
    dev = Path(f"/sys/class/net/{iface}/device")
    try:
        target = os.readlink(dev)
    except OSError:
        return False
    return "usb" in target


def wpa_bound_to_iface(cmdline: str, iface: str) -> bool:
    """True if this wpa_supplicant cmdline is bound to iface via -i.

    The system D-Bus daemon (`wpa_supplicant -u`, no -i) is never a match.
    Two dedicated wpa processes on one iface race remain-on-channel and
    Action TX cookies.
    """
    if "wpa_supplicant" not in cmdline:
        return False
    parts = cmdline.split()
    for i, part in enumerate(parts):
        if part == "-i" and i + 1 < len(parts) and parts[i + 1] == iface:
            return True
        if part == f"-i{iface}":
            return True
    return False


def _wpa_pids_on_iface(iface: str, keep_pid: Optional[int] = None) -> list[int]:
    pids: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if keep_pid is not None and pid == keep_pid:
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
            if wpa_bound_to_iface(cmdline, iface):
                pids.append(pid)
    except OSError:
        pass
    return pids


def _stop_competing_wpa(iface: str, keep_pid: Optional[int] = None) -> None:
    """Stop other wpa_supplicant processes bound to this iface only."""
    pids = _wpa_pids_on_iface(iface, keep_pid=keep_pid)
    for pid in pids:
        print(
            f"[FluxCast WFD] Stopping extra wpa_supplicant pid={pid} on {iface}"
        )
        _run(["kill", str(pid)], timeout=3.0)
    if pids:
        time.sleep(0.4)


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
                "device_name=fluxcast-p2p",
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
                _stop_competing_wpa(iface, keep_pid=pid)
                _wpa_cli(iface, "set", "device_name", "fluxcast-p2p", timeout=3.0)
                _wpa_cli(iface, "set", "p2p_group_formation_timeout", "45", timeout=3.0)
                _run(["ip", "link", "set", iface, "up"], timeout=5.0)
                _chmod_wpa_log()
                return
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
        try:
            _PID.unlink()
        except OSError:
            pass

    _stop_competing_wpa(iface, keep_pid=None)
    for path in Path("/sys/class/net").glob("p2p-*"):
        _run(["iw", "dev", path.name, "del"], timeout=3.0)
    sock = _CTRL / iface
    try:
        sock.unlink()
    except OSError:
        pass
    time.sleep(0.3)

    try:
        _LOG.write_text("")
    except OSError:
        pass
    wpa_bin = _wpa_bin()
    started = _run(
        [
            wpa_bin,
            "-B",
            "-t",
            "-dd",
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
    if started.returncode != 0:
        raise WFDNotReady(
            f"Dedicated USB wpa_supplicant failed to exec on {iface}: "
            f"{(started.stderr or started.stdout or '').strip()}"
        )
    ping = None
    deadline = time.time() + 8.0
    while time.time() < deadline:
        time.sleep(0.25)
        if not sock.exists():
            continue
        ping = _wpa_cli(iface, "ping", timeout=2.0)
        if ping.returncode == 0 and "PONG" in (ping.stdout or ""):
            break
    else:
        err = ""
        if ping is not None:
            err = (ping.stderr or ping.stdout or "").strip()
        raise WFDNotReady(
            f"Dedicated USB wpa_supplicant failed to start on {iface}: {err}"
        )
    _wpa_cli(iface, "set", "wifi_display", "1", timeout=3.0)
    _wpa_cli(iface, "set", "p2p_group_formation_timeout", "45", timeout=3.0)
    _run(["ip", "link", "set", iface, "up"], timeout=5.0)
    _chmod_wpa_log()


def _iw_phy_text(iface: str) -> str:
    phy = _phy_of(iface)
    if not phy:
        return ""
    r = _run(["iw", "phy", phy, "info"], timeout=5.0)
    return r.stdout or ""


def _sta_occupied_mhz(skip_iface: str) -> tuple[int, int] | None:
    """Occupied MHz of a primary STA (not the USB P2P iface), if any."""
    for path in Path("/sys/class/net").iterdir():
        name = path.name
        if name == skip_iface or name.startswith("p2p-"):
            continue
        info = _iw_info(name)
        if "type managed" not in info:
            continue
        occ = parse_sta_occupied_mhz(info)
        if occ:
            return occ
    return None


def _apply_5ghz_oper_preference(
    _cli,
    iface: str,
    peer_info: dict[str, str],
    go_5ghz: bool = False,
) -> None:
    """USB GO channel set: drop 5 GHz by default; keep it if go_5ghz.

    wpa treats p2p_oper_* as a hint and, after rejecting VHT 5 GHz, picks
    the first 2.4 channel in the intersection (ch 1). Default USB path
    SET p2p_pref_chan plus p2p_no_go_freq covering every other phy freq
    so GO Neg can only land on the preferred 2.4 channel (peer listen
    when usable). Never p2p_connect freq=. USB dedicated path only.
    """
    phy_text = _iw_phy_text(iface)
    occ = _sta_occupied_mhz(iface)
    # iw 6.x: survey dump is `iw dev <iface> survey dump`, not phy.
    survey = _run(["iw", "dev", iface, "survey", "dump"], timeout=5.0).stdout or ""
    busy = parse_iw_survey_busy_ratio(survey)
    ghz2 = parse_iw_phy_2ghz_channels(phy_text)
    ghz5 = parse_iw_phy_5ghz_channels(phy_text)
    listen_freq = peer_listen_freq(peer_info, default=0)
    prefer_24 = listen_freq if 2400 <= listen_freq < 2500 else None
    env_mhz = os.environ.get("FLUXCAST_WFD_GO_2GHZ_MHZ", "").strip()
    if env_mhz.isdigit():
        mhz = int(env_mhz)
        if 2400 <= mhz < 2500 and any(f == mhz for _, f in ghz2):
            prefer_24 = mhz
            print(
                f"[FluxCast WFD] USB GO 2.4 override {mhz} MHz "
                "(FLUXCAST_WFD_GO_2GHZ_MHZ; not p2p_connect freq=)"
            )
    pick = None
    band = "2.4 GHz"
    if go_5ghz:
        # Clear leftover exclude / pref from a prior USB session on reused wpa.
        _cli("set", "p2p_no_go_freq", "")
        _cli("set", "p2p_pref_chan", "")
        print("[FluxCast WFD] USB GO 5 GHz kept (--wfd-go-5ghz)")
        if peer_advertises_5ghz(peer_info):
            pick = first_quiet_5ghz_oper(ghz5, occ)
            band = "5 GHz"
        if not pick:
            pick = first_quiet_2ghz_oper(
                ghz2, occ, busy, prefer_freq=prefer_24
            )
            band = "2.4 GHz"
    else:
        pick = first_quiet_2ghz_oper(ghz2, occ, busy, prefer_freq=prefer_24)
        band = "2.4 GHz"
        if not pick and peer_advertises_5ghz(peer_info):
            pick = first_quiet_5ghz_oper(ghz5, occ)
            band = "5 GHz"
    if not pick:
        print("[FluxCast WFD] No usable GO oper channel on this phy")
        return
    reg, ch, freq = pick
    if not go_5ghz:
        parts: list[str] = []
        no_24 = p2p_no_go_freq_ranges([f for _, f in ghz2], {freq})
        if no_24:
            parts.append(no_24)
        if ghz5:
            parts.append(
                f"{min(f for _, f in ghz5)}-{max(f for _, f in ghz5)}"
            )
        no_go = ",".join(parts)
        if no_go:
            _cli("set", "p2p_no_go_freq", no_go)
            print(
                f"[FluxCast WFD] USB GO channel set kept {freq} MHz; "
                f"p2p_no_go_freq {no_go} (not p2p_connect freq=)"
            )
        _cli("set", "p2p_pref_chan", f"{reg}:{ch}")
    _cli("set", "p2p_oper_reg_class", reg)
    _cli("set", "p2p_oper_channel", ch)
    extra = ""
    if freq in busy:
        extra = f", survey busy={busy[freq]*100:.0f}%"
    print(
        f"[FluxCast WFD] Oper preference {band} class {reg} ch {ch} "
        f"({freq} MHz{extra}), not forced on p2p_connect"
    )


def _apply_local_p2p_width(_cli, iface: str) -> dict[str, bool]:
    caps = parse_iw_phy_caps(_iw_phy_text(iface))
    ht40, vht = miracast_go_width_flags()
    _cli("set", "p2p_go_ht40", ht40)
    _cli("set", "p2p_go_vht", vht)
    print(
        f"[FluxCast WFD] Miracast GO width 20 MHz (ht40={ht40} vht={vht}); "
        f"phy 5ghz={int(bool(caps.get('has_5ghz')))} "
        "(channel still negotiated; not pinned)"
    )
    return caps


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
        st = _run(
            ["wpa_cli", "-p", str(_CTRL), "-i", name, "status"], timeout=3.0
        )
        if st.returncode == 0 and "wpa_state=COMPLETED" in (st.stdout or ""):
            return True
        return "ssid" in info or oper in ("up", "unknown")
    return False


def _teardown_phy_groups(phy: str) -> None:
    for path in Path("/sys/class/net").glob("p2p-*"):
        if _phy_of(path.name) == phy:
            _run(["iw", "dev", path.name, "del"], timeout=3.0)


def _live_group_on_phy(phy: str, peer_mac: str) -> Optional[str]:
    """Return data iface if a finished group (SSID + STA/client) is already up."""
    peer = peer_mac.lower()
    for path in Path("/sys/class/net").glob("p2p-*"):
        name = path.name
        if _phy_of(name) != phy:
            continue
        info = _iw_info(name)
        if "type P2P-GO" in info and _is_live_group(name, "P2P-GO"):
            dump = subprocess.run(
                ["iw", "dev", name, "station", "dump"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout or ""
            if peer.replace(":", "") in dump.lower().replace(":", "") or "Station" in dump:
                return name
        if "type P2P-client" in info and _is_live_group(name, "P2P-client"):
            return name
    return None


def _neg_log_delta(since_len: int) -> str:
    try:
        text = _LOG.read_text(errors="replace")
    except OSError:
        _chmod_wpa_log()
        try:
            text = _LOG.read_text(errors="replace")
        except OSError:
            return ""
    return text[since_len:] if since_len < len(text) else text


def connect_usb_dedicated(
    iface: str,
    peer_mac: str,
    go_intent: int = 0,
    rtsp_port: int = WFD_RTSP_PORT,
    go_5ghz: bool = False,
) -> str:
    """Form a finished P2P group on the USB phy; return data iface with IP.

    Tries client role (go_intent=0) first when intent is 0 — Miracast sources
    commonly let the sink be GO. Falls back to go_intent=15 if requested.
    go_5ghz keeps 5 GHz in the GO channel set; default drops it
    (p2p_no_go_freq). Never p2p_connect freq=.
    """
    if not _is_usb_wifi(iface):
        raise WFDNotReady(f"{iface} does not look like a USB wifi device")

    ensure_dedicated_wpa(iface)
    _chmod_wpa_log()
    peer = peer_mac.lower()
    phy = _phy_of(iface)
    print(
        f"[FluxCast WFD] Dedicated P2P on {iface} (phy={phy}); "
        "primary STA radio left on NetworkManager for internet"
    )
    live = _live_group_on_phy(phy, peer)
    if live:
        print(
            f"[FluxCast WFD] Reusing live USB group {live} "
            "(skip GO Neg — do not tear down a working BSS)"
        )
        mark_unmanaged(live)
        configure_ip(live, peer_mac, "P2P-GO" if "GO" in _iw_info(live) else "P2P-client", iface)
        return live

    def _cli(*args: str, timeout: float = 10.0):
        return _wpa_cli(iface, *args, timeout=timeout)

    _cli("set", "wifi_display", "1")
    port = max(1, min(int(rtsp_port), 65535))
    dev_info = f"{0x0011:04x}{port:04x}00c8"
    _cli("wfd_subelem_set", "0", dev_info)

    # Listen defaults; overridden to the peer listen_freq after find.
    # Do not set p2p_oper_*: a leftover 81/6 pin fights 5 GHz intersection.
    _cli("set", "p2p_listen_reg_class", "81")
    _cli("set", "p2p_listen_channel", "6")

    intents = [go_intent]
    if go_intent == 0:
        # USB SoftMAC: Confirm TX as client (intent 0) has been dropping the
        # pending Action; GO (15) is what produced GROUP-STARTED.
        intents = [15, 0]
    elif go_intent == 15:
        intents.append(0)

    last_err = "USB P2P group formation failed"
    for intent in intents:
        _teardown_phy_groups(phy)
        _cli("p2p_cancel", timeout=3.0)
        _cli("set", "p2p_go_intent", str(intent))
        _cli("p2p_find", "12")
        found = False
        for _ in range(20):
            peers = _cli("p2p_peers", timeout=5.0)
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

        peer_info = parse_p2p_peer_info(
            (_cli("p2p_peer", peer, timeout=5.0).stdout or "")
        )
        listen_freq = peer_listen_freq(peer_info, default=2437)
        if peer_wps_not_ready(peer_info):
            wps = peer_info.get("wps_method") or "?"
            print(
                f"[FluxCast WFD] Peer {peer_mac} wps_method={wps} — "
                "put the sink in Miracast/Screen Share receive mode now"
            )
            peer_info = wait_peer_wps_ready(_cli, peer, timeout_s=12.0)
            if peer_wps_not_ready(peer_info):
                print(
                    f"[FluxCast WFD] Peer {peer_mac} still WPS not-ready; "
                    "attempting GO Neg anyway"
                )
            else:
                print(
                    f"[FluxCast WFD] Peer {peer_mac} WPS ready "
                    f"({peer_info.get('wps_method')})"
                )
            listen_freq = peer_listen_freq(peer_info, default=listen_freq)
        listen_reg, listen_ch = freq_to_social_channel(listen_freq)
        _cli("set", "p2p_listen_reg_class", listen_reg)
        _cli("set", "p2p_listen_channel", listen_ch)
        _apply_local_p2p_width(_cli, iface)
        _apply_5ghz_oper_preference(_cli, iface, peer_info, go_5ghz=go_5ghz)

        sync_before_go_neg(_cli, peer, listen_freq)
        _chmod_wpa_log()
        # Mark after PD settle. TX_WAIT_EXPIRE in this window is not failure
        # (GO Neg retries, or a delayed PD wait-expire).
        log_mark = _LOG.stat().st_size if _LOG.exists() else 0
        print(
            f"[FluxCast WFD] p2p_connect {peer_mac} go_intent={intent} "
            f"listen={listen_freq} (oper class negotiated, not pinned) "
            "(wait for GO-NEG + GROUP-STARTED)..."
        )
        conn = _cli(
            "p2p_connect",
            peer,
            "pbc",
            f"go_intent={intent}",
            timeout=10.0,
        )
        conn_out = (conn.stdout or conn.stderr or "").strip()
        if conn.returncode != 0 or "FAIL" in conn_out.upper():
            print(f"[FluxCast WFD] p2p_connect failed ({conn_out}); trying join")
            conn = _cli("p2p_connect", peer, "pbc", "join", timeout=10.0)
            conn_out = (conn.stdout or conn.stderr or "").strip()
            if conn.returncode != 0 or "FAIL" in conn_out.upper():
                last_err = f"USB p2p_connect failed: {conn_out}"
                continue

        deadline = time.time() + 40.0
        saw_pending_go = ""
        saw_neg_resp = False
        while time.time() < deadline:
            delta = _neg_log_delta(log_mark)
            if (
                not saw_neg_resp
                and "Received GO Negotiation Response" in delta
            ):
                saw_neg_resp = True
                deadline = max(deadline, time.time() + 25.0)
                print(
                    "[FluxCast WFD] GO Neg Response received — "
                    "holding group until GROUP-STARTED (not tearing pending GO)"
                )
            if "WPS-PBC-ACTIVE" in delta or "AP-ENABLED" in delta:
                deadline = max(deadline, time.time() + 50.0)
            outcome = neg_outcome_from_log(delta)
            if outcome == "failure":
                diag = describe_neg_log(delta)
                print(
                    f"[FluxCast WFD] GO Negotiation failed per wpa log "
                    f"({diag}) — tearing pending GO"
                )
                _teardown_phy_groups(phy)
                last_err = (
                    "Wi-Fi Direct GO Negotiation did not complete on the "
                    f"dedicated USB path ({diag}). "
                    "Retry with the sink in receive mode, or use another P2P "
                    "iface if available."
                )
                break

            if "P2P-GROUP-FORMATION-FAILURE" in delta:
                last_err = (
                    "P2P group WPS formation failed "
                    "(STA associated but provisioning did not finish)"
                )
                break

            for path in Path("/sys/class/net").glob("p2p-*"):
                name = path.name
                if _phy_of(name) != phy:
                    continue
                info = _iw_info(name)
                if "type P2P-GO" in info:
                    if is_pending_go_shell(info):
                        saw_pending_go = name
                        continue
                    # GO beacons + STA assoc happen before WPS. DHCP/unpark
                    # during WPS made the sink deauth (FORMATION_FAILED).
                    if "P2P-GROUP-STARTED" not in delta:
                        continue
                    if _is_live_group(name, "P2P-GO"):
                        print(f"[FluxCast WFD] GO {name}: GROUP-STARTED")
                        mark_unmanaged(name)
                        configure_ip(name, peer_mac, "P2P-GO", iface)
                        return name
                if "type P2P-client" in info and _is_live_group(name, "P2P-client"):
                    if "P2P-GROUP-STARTED" not in delta:
                        continue
                    print(f"[FluxCast WFD] client {name}: GROUP-STARTED")
                    mark_unmanaged(name)
                    configure_ip(name, peer_mac, "P2P-client", iface)
                    return name

            if outcome == "success":
                time.sleep(0.5)
                continue
            time.sleep(0.5)
        else:
            diag = describe_neg_log(_neg_log_delta(log_mark))
            if saw_neg_resp:
                last_err = (
                    "GO Neg Response/Confirm exchanged but group BSS did not "
                    f"come up ({diag})"
                )
            elif saw_pending_go:
                last_err = (
                    f"Pending P2P-GO {saw_pending_go} never became a live BSS "
                    f"(no channel/SSID) — {diag}"
                )
            else:
                last_err = (
                    "Wi-Fi Direct GO Negotiation did not complete on the "
                    f"dedicated USB path ({diag})"
                )
            # After a successful Neg Response, do not immediately delete the
            # GO iface — Confirm TX is slow on USB SoftMAC.
            if not saw_neg_resp:
                _teardown_phy_groups(phy)
            continue

        continue

    raise WFDNotReady(last_err)


def release_usb_dedicated(iface: Optional[str] = None) -> None:
    """Best-effort teardown of dedicated USB wpa + group ifaces.

    If a live BSS+STA is up, leave it: tearing it down to 'start Extend'
    is how we lost the working test-pattern group and then failed Confirm TX.
    Explicit `miracast-ctl stop` (keep_workspaces=0) still deletes groups.
    """
    phy = _phy_of(iface) if iface else ""
    if phy and _live_group_on_phy(phy, ""):
        print(
            f"[FluxCast WFD] Keeping live USB group on {phy} "
            "(Extend can attach without re-running GO Neg)"
        )
        return
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
        p2p_dev = f"p2p-dev-{iface}"
        _run(["nmcli", "device", "set", p2p_dev, "managed", "yes"], timeout=5.0)
