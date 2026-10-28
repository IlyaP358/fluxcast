"""Dedicated wpa_supplicant on a secondary wifi iface for Miracast P2P.

When a second adapter shares NetworkManager's wpa_supplicant with the
primary STA radio, P2P groups often form on the STA phy. A dedicated
wpa_supplicant on the unmanaged/secondary iface keeps groups on that phy.
Never dedicated-ize the IPv4 default-route iface (that steals internet).
``FLUXCAST_USB_WPA`` is a wpa binary path, not a dedicated-on/off switch.

P2P sequence (Wi-Fi Direct + WFD):

1. Formation — GO Neg + WPS/4-way on peer listen ∩ local Channel List.
   After GO-NEG-SUCCESS the GO ACL is the P2P *interface* address;
   sinks often probe as the *device* address — add both to accept_acl
   or WPS times out (FORMATION_FAILED). Success is GROUP-STARTED and
   AP-STA-CONNECTED. Pending GO shells are not a group.
2. L3 + RTSP PLAY on the formation channel (same 4-way).
3. After PLAY: optional 802.11 CSA on the *same* GO BSS if 5 GHz cannot
   carry WFD. Revert if the STA deauths (new 4-way). Persist nofollow
   per P2P device address so the next PLAY skips the hop. Never
   group_remove or restart wpa for a channel hop.

Channel/width/intent/CSA policy lives in formation.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from ..config import WFDNotReady
from ..privileged import run_privileged
from ..constants import WFD_RTSP_PORT, _DEVICE_NAME
from ..ie import wfd_wpa_cli_device_info_hex, wfd_wpa_cli_local_ip_hex
from .formation import (
    describe_neg_log,
    first_quiet_2ghz_oper,
    first_quiet_5ghz_oper,
    FORMATION_L3_TRY_S,
    FORMATION_STA_WAIT_S,
    PAYLOAD_CSA_24,
    PAYLOAD_CSA_24_MHZ,
    POST_CSA_CHURN_S,
    POST_CSA_SETTLE_S,
    POST_CSA_STA_WAIT_S,
    go_intents_for_radio,
    ap_sta_connect_count,
    csa_dhcp_still_waiting,
    go_sta_connected_in_delta,
    csa_sta_followed,
    go_sta_is_present,
    go_sta_ready_for_l3,
    go_width_flags_for_radio,
    plan_payload_channel,
    parse_iw_survey_busy_ratio,
    freq_to_social_channel,
    is_pending_go_shell,
    neg_outcome_from_log,
    parse_iw_phy_2ghz_channels,
    parse_iw_phy_5ghz_channels,
    parse_iw_phy_radio,
    p2p_no_go_freq_ranges,
    parse_p2p_peer_info,
    parse_sta_occupied_mhz,
    peer_advertises_24ghz,
    peer_advertises_5ghz,
    peer_listen_freq,
    listen_freq_for_wps,
    peer_wps_not_ready,
    radio_can_stream_5ghz,
    should_csa_go_to_2ghz,
    sync_before_go_neg,
    wfd_radio_reject_reason,
    usb_discovery_steps,
)
from .safety import mac_file_key, safe_iface, safe_mac
from .stability import apply_radio_stability
from .wpas_ip import WFD_P2P_SUBNET, _announce_go_ipv4, configure_ip, mark_unmanaged

_CTRL = Path("/tmp/fluxcast-usb-wpa")
_PID = _CTRL / "wpa.pid"
_LOG = _CTRL / "wpa.log"
_STA_SNAP = _CTRL / "sta-snapshot.json"


def _state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = root / "omarchy-miracast"
    d.mkdir(parents=True, exist_ok=True)
    return d


def persistent_ok_path(mac: str) -> Path:
    d = _state_dir() / "p2p-persistent-ok"
    d.mkdir(parents=True, exist_ok=True)
    key = mac_file_key(mac)
    if not key:
        return d / "_invalid"
    return d / key


def mark_p2p_persistent_ok(mac: str) -> None:
    """Record that a full PLAY completed with this P2P device address."""
    if not mac_file_key(mac):
        return
    try:
        persistent_ok_path(mac).write_text("play\n", encoding="utf-8")
    except OSError:
        pass


def peer_has_persistent_ok(mac: str) -> bool:
    return bool(mac) and persistent_ok_path(mac).is_file()


def invite_ok_path(mac: str) -> Path:
    d = _state_dir() / "p2p-invite-ok"
    d.mkdir(parents=True, exist_ok=True)
    key = mac_file_key(mac)
    if not key:
        return d / "_invalid"
    return d / key


def mark_p2p_invite_ok(mac: str) -> None:
    """Record that p2p_invite returned status=0 for this device address."""
    if not mac_file_key(mac):
        return
    try:
        invite_ok_path(mac).write_text("invite\n", encoding="utf-8")
    except OSError:
        pass


def peer_has_invite_ok(mac: str) -> bool:
    return bool(mac) and invite_ok_path(mac).is_file()


def csa_nofollow_path(mac: str) -> Path:
    d = _state_dir() / "p2p-csa-nofollow"
    d.mkdir(parents=True, exist_ok=True)
    key = mac_file_key(mac)
    if not key:
        return d / "_invalid"
    return d / key


def mark_p2p_csa_nofollow(mac: str) -> None:
    """Record that this P2P device did not follow 802.11 CSA IEs; skip hop on next PLAY."""
    if not mac_file_key(mac):
        return
    try:
        csa_nofollow_path(mac).write_text("nofollow\n", encoding="utf-8")
    except OSError:
        pass


def peer_has_csa_nofollow(mac: str) -> bool:
    return bool(mac) and csa_nofollow_path(mac).is_file()


def _conf_path() -> Path:
    """Durable wpa.conf so persistent P2P groups survive dedicated-wpa restarts."""
    return _state_dir() / "usb-wpa.conf"


def _read_wpa_conf(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        result = run_privileged(["cat", str(path)], timeout=5.0)
        return result.stdout or ""


def _write_wpa_conf(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
        return
    except OSError:
        pass
    tmp = Path("/tmp/fluxcast-usb-wpa.conf.new")
    tmp.write_text(text, encoding="utf-8")
    run_privileged(["cp", str(tmp), str(path)], timeout=5.0)
    run_privileged(["chmod", "0666", str(path)], timeout=5.0)


def _ensure_wpa_conf() -> Path:
    """Write/refresh headers; keep any saved network={...} persistent groups."""
    path = _conf_path()
    existing = _read_wpa_conf(path) if path.exists() else ""
    networks = ""
    idx = existing.find("network={")
    if idx >= 0:
        networks = existing[idx:].rstrip() + "\n"
    persist_mac = ""
    for line in existing.splitlines():
        if line.startswith("p2p_device_persistent_mac_addr="):
            persist_mac = line
            break
    headers = [
        f"ctrl_interface={_CTRL}",
        "ctrl_interface_group=0",
        f"device_name={_DEVICE_NAME}",
        "p2p_go_intent=15",
        "driver_param=use_p2p_group_interface=1",
        "update_config=1",
        "persistent_reconnect=1",
        "p2p_device_random_mac_addr=2",
    ]
    if persist_mac:
        headers.append(persist_mac)
    headers.append("")
    _write_wpa_conf(path, "\n".join(headers) + "\n" + networks)
    return path


def _persistent_go_ids(list_networks_out: str) -> list[int]:
    """network ids marked [P2P-PERSISTENT] in wpa_cli list_networks."""
    ids: list[int] = []
    for line in (list_networks_out or "").splitlines():
        if "P2P-PERSISTENT" not in line:
            continue
        nid = line.split()[0]
        if nid.isdigit():
            ids.append(int(nid))
    return ids


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


def _usb_go_intents(go_intent: int, radio: Optional[dict] = None) -> list[int]:
    """GO intent from this phy, not from USB vs PCI."""
    return go_intents_for_radio(go_intent, radio or {})


def _cached_listen_mhz(peer_mac: str) -> Optional[int]:
    env = os.environ.get("FLUXCAST_PEER_LISTEN_MHZ", "").strip()
    if env.isdigit():
        mhz = int(env)
        if mhz > 0:
            return mhz
    mac = peer_mac.replace("-", ":").upper()
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    peers_path = state / "omarchy-miracast" / "peers.json"
    try:
        import json

        for row in json.loads(peers_path.read_text()):
            if str(row.get("mac") or "").replace("-", ":").upper() != mac:
                continue
            for key in ("listenFreqMHz", "operFreqMHz"):
                val = row.get(key)
                if val in (None, "", 0, "0"):
                    continue
                try:
                    mhz = int(val)
                except (TypeError, ValueError):
                    continue
                if mhz > 0:
                    return mhz
    except (OSError, ValueError, TypeError):
        pass
    return None


def _ht_spatial_streams(iface: str) -> int:
    """HT/VHT spatial streams from iw phy. Default 1 if unknown."""
    return int(parse_iw_phy_radio(_iw_phy_text(iface)).get("nss") or 1)


def _sta_signal_dbm(iface: str) -> Optional[int]:
    text = _run(["iw", "dev", iface, "station", "dump"], timeout=3.0).stdout or ""
    m = re.search(r"signal:\s*(-\d+)", text)
    if m:
        return int(m.group(1))
    return None


def _wait_go_sta(
    iface: str,
    log_mark: int,
    timeout_s: float,
    *,
    for_l3: bool = False,
    require_log_connect: bool = False,
) -> bool:
    """Wait until a sink STA is associated (WPS/4-way), or L3-ready."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        delta = _neg_log_delta(log_mark)
        if require_log_connect:
            if go_sta_connected_in_delta(delta):
                return True
            time.sleep(0.25)
            continue
        dump = _run(["iw", "dev", iface, "station", "dump"], timeout=2.0).stdout or ""
        if for_l3:
            if go_sta_ready_for_l3(delta, dump):
                return True
        elif go_sta_is_present(delta, dump):
            return True
        time.sleep(0.25)
    return False


def _wait_csa_join_stable(iface: str, log_mark: int) -> bool:
    """First post-CSA CONNECT, then wait out a possible second 4-way."""
    if not _wait_go_sta(
        iface, log_mark, POST_CSA_STA_WAIT_S, require_log_connect=True
    ):
        return False
    first_t = time.time()
    last_connect_t = first_t
    last_n = ap_sta_connect_count(_neg_log_delta(log_mark))
    print(
        "[FluxCast WFD] Payload: waiting for 4-way to settle after CSA "
        f"(churn window {int(POST_CSA_CHURN_S)}s, settle "
        f"{int(POST_CSA_SETTLE_S)}s) before DHCP"
    )
    deadline = first_t + POST_CSA_CHURN_S + POST_CSA_SETTLE_S + 2.0
    while time.time() < deadline:
        delta = _neg_log_delta(log_mark)
        connected = go_sta_connected_in_delta(delta)
        n = ap_sta_connect_count(delta)
        if n > last_n:
            last_n = n
            last_connect_t = time.time()
            print(
                f"[FluxCast WFD] Payload: STA 4-way #{n} after CSA; "
                "delaying DHCP"
            )
        extra = max(0, n - 1)
        since_last = (
            (time.time() - last_connect_t) if connected else None
        )
        if not csa_dhcp_still_waiting(
            connected=connected,
            seconds_since_last_connect=since_last,
            seconds_since_first_connect=time.time() - first_t,
            extra_four_ways=extra,
        ):
            return connected
        time.sleep(0.25)
    return go_sta_connected_in_delta(_neg_log_delta(log_mark))


def _go_mhz_from_iw(info: str) -> int:
    mhz_m = re.search(r"\((\d{4}) MHz\)", info or "")
    return int(mhz_m.group(1)) if mhz_m else 0


def _apply_payload_csa(
    go_iface: str,
    target_mhz: int,
    *,
    count: int = 15,
    blocktx: bool = True,
) -> int:
    """802.11 CSA on a live GO. Does not remove the group or restart wpa.

    Mid-session: count=30, blocktx=False so STAs keep hearing CSA IEs.
    If the STA deauths, caller must revert — that is a new 4-way, not a hop.
    """
    csa_mark = _LOG.stat().st_size if _LOG.exists() else 0
    args = ["chan_switch", str(count), str(target_mhz)]
    if blocktx:
        args.append("blocktx")
    _wpa_cli(go_iface, *args, timeout=8.0)
    csa_until = time.time() + max(2.5, count * 0.12 + 1.0)
    while time.time() < csa_until:
        csa_delta = _neg_log_delta(csa_mark)
        if (
            "AP-CSA-FINISHED" in csa_delta
            or "CTRL-EVENT-CHANNEL-SWITCH" in csa_delta
        ):
            break
        time.sleep(0.1)
    return csa_mark


def _operating_go_iface() -> Optional[str]:
    for path in Path("/sys/class/net").glob("p2p-*"):
        info = _iw_info(path.name)
        if "type P2P-GO" in info and not is_pending_go_shell(info):
            return path.name
    return None


def _peer_macs_from_neg_log(delta: str) -> list[str]:
    """Device + interface addresses from P2P-GO-NEG-SUCCESS."""
    found: list[str] = []
    for key in ("peer_dev", "peer_iface"):
        m = re.search(
            rf"{key}=([0-9A-Fa-f]{{2}}(?::[0-9A-Fa-f]{{2}}){{5}})",
            delta or "",
        )
        if m:
            mac = safe_mac(m.group(1))
            if mac and mac not in found:
                found.append(mac)
    return found


def _allow_macs_on_go_acl(go: str, macs: list[str]) -> None:
    """P2P GO ACL is the interface address; sinks often probe as the device address."""
    go = safe_iface(go)
    if not go:
        return
    for raw in macs:
        mac = safe_mac(raw)
        if not mac:
            continue
        out = _wpa_cli(go, "accept_acl", "ADD_MAC", mac, timeout=3.0)
        if out.returncode == 0 and "FAIL" not in (out.stdout or "").upper():
            print(f"[FluxCast WFD] GO ACL allow {mac} on {go} (device+iface)")


def _phy_wifi_iface(go_iface: str) -> str:
    phy = _phy_of(go_iface)
    for path in Path("/sys/class/net").iterdir():
        if path.name.startswith("p2p-"):
            continue
        if _phy_of(path.name) == phy:
            return path.name
    return go_iface


def _refresh_wfd_go_ies(ifaces: list[str], dev_info: str, ipv4: str = "") -> None:
    """Re-publish WFD Device Info + Local IP on the P2P device and GO."""
    ip_hex = wfd_wpa_cli_local_ip_hex(ipv4 or f"{WFD_P2P_SUBNET}.1")
    seen: set[str] = set()
    for iface in ifaces:
        if not iface or iface in seen:
            continue
        seen.add(iface)
        _wpa_cli(iface, "set", "wifi_display", "1", timeout=3.0)
        _wpa_cli(iface, "wfd_subelem_set", "0", dev_info, timeout=3.0)
        _wpa_cli(iface, "wfd_subelem_set", "8", ip_hex, timeout=3.0)


def should_invite_before_rtsp(
    *,
    has_rtsp: bool,
    sta_present: bool,
    lease_age_s: float,
    invites_sent: int,
    max_invites: int = 2,
) -> bool:
    """STA left the GO before M1: reinvite on the same BSS."""
    if has_rtsp or sta_present:
        return False
    if invites_sent >= max_invites:
        return False
    return lease_age_s >= 2.0


def should_abandon_rtsp_wait(
    *,
    has_rtsp: bool,
    lease_age_s: float,
    timeout_s: float = 35.0,
) -> bool:
    """Give up the RTSP wait so a leftover GO is not left as phase=dhcp."""
    if has_rtsp:
        return False
    return lease_age_s >= timeout_s


def _sta_inactive_ms(dump: str) -> Optional[int]:
    m = re.search(r"inactive time:\s*(\d+)\s*ms", dump or "")
    return int(m.group(1)) if m else None


def _sta_tx_bytes(dump: str) -> Optional[int]:
    m = re.search(r"tx bytes:\s*(\d+)", dump or "")
    return int(m.group(1)) if m else None


def run_mid_session_csa(*, prefer_5ghz: bool = False, peer_mac: str = "") -> bool:
    """After PLAY: 802.11 CSA on the *same* GO BSS if 5 GHz cannot carry WFD.

    Spec: group already operating; no p2p_connect / group_remove / wpa restart.
    If the STA deauths or goes inactive (did not follow CSA IEs), revert to
    the formation channel immediately so RTSP TCP is not bound to a new 4-way.
    """
    if os.environ.get("FLUXCAST_WFD_NO_MID_CSA", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    mac = (peer_mac or "").strip()
    if peer_has_csa_nofollow(mac):
        print(
            f"[FluxCast WFD] Mid-session CSA skipped "
            f"(peer {mac} did not follow CSA IEs previously)"
        )
        return False
    go = _operating_go_iface()
    if not go:
        return False
    info = _iw_info(go)
    go_mhz = _go_mhz_from_iw(info)
    if go_mhz < 5000:
        return False
    phy_iface = _phy_wifi_iface(go)
    radio = parse_iw_phy_radio(_iw_phy_text(phy_iface))
    dump = _run(["iw", "dev", go, "station", "dump"], timeout=2.0).stdout or ""
    if not go_sta_is_present("", dump):
        print("[FluxCast WFD] Mid-session CSA skipped (no STA)")
        return False
    if not should_csa_go_to_2ghz(
        go_mhz=go_mhz,
        radio=radio,
        sta_signal_dbm=_sta_signal_dbm(go),
        peer_has_24=True,
        prefer_5ghz=prefer_5ghz,
    ):
        print(
            f"[FluxCast WFD] Mid-session CSA skipped (phy can carry {go_mhz} MHz)"
        )
        return False
    print(
        f"[FluxCast WFD] Mid-session CSA {go_mhz}→{PAYLOAD_CSA_24_MHZ} MHz "
        "after PLAY (same GO BSS; revert if STA does not follow)"
    )
    hold = Path("/tmp/fluxcast-mid-csa-hold")
    try:
        hold.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        hold = None
    def _revert(why: str) -> bool:
        print(f"[FluxCast WFD] Mid-session CSA: {why}; reverting to {go_mhz} MHz")
        _apply_payload_csa(go, go_mhz, count=10, blocktx=False)
        if mac:
            mark_p2p_csa_nofollow(mac)
            print(
                f"[FluxCast WFD] Mid-session CSA: marked nofollow for {mac} "
                "(skip hop on next PLAY)"
            )
        return False

    try:
        mark = _apply_payload_csa(
            go, PAYLOAD_CSA_24_MHZ, count=30, blocktx=False
        )
        # Need several live samples on 2437. One snapshot after the hop
        # was a false follow (then MCS 0, TCP ETIMEDOUT).
        deadline = time.time() + 8.0
        live_streak = 0
        followed = False
        prev_tx: Optional[int] = None
        while time.time() < deadline:
            delta = _neg_log_delta(mark)
            if "AP-STA-DISCONNECTED" in delta:
                return _revert("STA deauthed (new 4-way)")
            dump = (
                _run(["iw", "dev", go, "station", "dump"], timeout=2.0).stdout
                or ""
            )
            now_mhz = _go_mhz_from_iw(_iw_info(go))
            inactive = _sta_inactive_ms(dump)
            txb = _sta_tx_bytes(dump)
            if inactive is not None and inactive >= 4000:
                return _revert(
                    f"STA inactive {inactive}ms on {now_mhz} MHz"
                )
            grew = (
                prev_tx is not None
                and txb is not None
                and txb > prev_tx + 500
            )
            if prev_tx is None and txb is not None:
                prev_tx = txb
            elif txb is not None:
                prev_tx = txb
            if (
                csa_sta_followed(
                    go_mhz=now_mhz,
                    target_mhz=PAYLOAD_CSA_24_MHZ,
                    disconnected=False,
                    inactive_ms=inactive,
                    sta_present=go_sta_is_present(delta, dump),
                )
                and grew
            ):
                live_streak += 1
                if live_streak >= 8:
                    followed = True
                    break
            else:
                live_streak = 0
            time.sleep(0.25)
        if not followed:
            return _revert(
                f"STA did not stay live on {PAYLOAD_CSA_24_MHZ} MHz"
            )
        print(
            f"[FluxCast WFD] Mid-session CSA: STA followed to {PAYLOAD_CSA_24_MHZ} MHz "
            "(same association; next IDR within GOP)"
        )
        return True
    finally:
        if hold is not None:
            try:
                hold.unlink()
            except OSError:
                pass


def schedule_mid_session_csa(*, delay_s: float = 2.0, peer_mac: str = "") -> None:
    """Run CSA after PLAY so RTP/IDR are already flowing (daemon thread)."""
    mac = (peer_mac or "").strip()
    if peer_has_csa_nofollow(mac):
        print(
            f"[FluxCast WFD] Mid-session CSA skipped "
            f"(peer {mac} did not follow CSA IEs previously)"
        )
        return
    prefer = os.environ.get("FLUXCAST_WFD_GO_5GHZ", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    def _run() -> None:
        time.sleep(max(0.5, delay_s))
        try:
            run_mid_session_csa(prefer_5ghz=prefer, peer_mac=mac)
        except Exception as exc:
            print(f"[FluxCast WFD] Mid-session CSA failed: {exc}")

    threading.Thread(target=_run, daemon=True, name="wfd-mid-csa").start()


def watch_dedicated_rtsp_join(
    *,
    phy_iface: str,
    peer_mac: str,
    rtsp,
    timeout_s: float = 35.0,
) -> None:
    """Keep the GO joinable until sink-initiated RTSP, or abandon the wait.

    After DHCPACK many sinks SYN :7236 within ~10s. If the STA deauths
    (reason 3) with no M1, p2p_invite on the same persistent BSS and
    re-announce IPv4/WFD IEs. Do not start a new GO Neg.
    """
    mac = safe_mac(peer_mac)
    phy_iface = safe_iface(phy_iface)
    t0 = time.time()
    invites = 0
    refreshed = False
    go = _operating_go_iface()
    ipv4 = f"{WFD_P2P_SUBNET}.1"
    dev_info = wfd_wpa_cli_device_info_hex(WFD_RTSP_PORT)
    while True:
        if getattr(rtsp, "has_connected_client", False):
            return
        age = time.time() - t0
        if should_abandon_rtsp_wait(has_rtsp=False, lease_age_s=age, timeout_s=timeout_s):
            print(
                "[FluxCast WFD] Sink did not start RTSP after DHCP; "
                "abandoning wait (STA left or no M1)"
            )
            abandoned = getattr(rtsp, "join_abandoned", None)
            if abandoned is not None:
                abandoned.set()
            return
        go = safe_iface(go or _operating_go_iface() or "")
        dump = ""
        if go:
            dump = _run(["iw", "dev", go, "station", "dump"], timeout=2.0).stdout or ""
        sta = go_sta_is_present("", dump)
        if (
            not refreshed
            and age >= 3.0
            and sta
            and go
        ):
            refreshed = True
            _announce_go_ipv4(go, ipv4)
            _refresh_wfd_go_ies([phy_iface, go], dev_info, ipv4)
            print(
                "[FluxCast WFD] Re-announced GO IPv4 / WFD IEs "
                "(waiting for sink RTSP)"
            )
        if should_invite_before_rtsp(
            has_rtsp=False,
            sta_present=sta,
            lease_age_s=age,
            invites_sent=invites,
        ) and go and mac and phy_iface:
            inv = _wpa_cli(
                phy_iface,
                "p2p_invite",
                f"group={go}",
                f"peer={mac}",
                timeout=8.0,
            )
            out = (inv.stdout or inv.stderr or "").strip()
            invites += 1
            if inv.returncode == 0 and "FAIL" not in out.upper():
                print(
                    f"[FluxCast WFD] STA left before RTSP; "
                    f"p2p_invite group={go} peer={mac} ({invites})"
                )
                mark_p2p_invite_ok(mac)
            else:
                print(
                    f"[FluxCast WFD] p2p_invite after STA-left skipped "
                    f"({out or 'FAIL'})"
                )
            _announce_go_ipv4(go, ipv4)
            _refresh_wfd_go_ies([phy_iface, go], dev_info, ipv4)
        time.sleep(0.5)


def _finish_live_go(
    name: str,
    *,
    phy_iface: str,
    peer_info: dict[str, str],
    go_5ghz: bool,
    log_mark: int,
    peer_mac: str,
    dev_info: str,
    _cli,
) -> str:
    """Formation STA → DHCP on formation channel; CSA to 2.4 only if that lease fails."""
    mark_unmanaged(name)
    print(
        f"[FluxCast WFD] Formation: waiting for sink STA on {name} "
        "(WPS/4-way on negotiated oper channel)"
    )
    sta_ok = _wait_go_sta(name, log_mark, FORMATION_STA_WAIT_S)
    if not sta_ok:
        print(
            "[FluxCast WFD] No STA after GROUP-STARTED; skip payload CSA"
        )

    info = _iw_info(name)
    go_mhz = _go_mhz_from_iw(info)
    radio = parse_iw_phy_radio(_iw_phy_text(phy_iface))
    dump = _run(["iw", "dev", name, "station", "dump"], timeout=2.0).stdout or ""
    sta_present = sta_ok or go_sta_is_present(_neg_log_delta(log_mark), dump)
    action = plan_payload_channel(
        go_mhz=go_mhz,
        radio=radio,
        sta_signal_dbm=_sta_signal_dbm(name),
        peer_has_24=peer_advertises_24ghz(peer_info),
        prefer_5ghz=go_5ghz,
        sta_present=sta_present,
    )
    _refresh_wfd_go_ies([phy_iface, name], dev_info)

    # Spec path: DHCP/RTSP on the formation channel (one 4-way). Forcing
    # CSA before L3 is what produced the extra 4-way ~28s later and the
    # TV leaving. CSA only if this lease fails.
    print(
        f"[FluxCast WFD] L3: DHCP on formation channel {go_mhz or '?'} MHz "
        f"(up to {int(FORMATION_L3_TRY_S)}s; CSA only if no lease)"
    )
    try:
        configure_ip(
            name, peer_mac, "P2P-GO", physical_iface=None,
            lease_timeout=FORMATION_L3_TRY_S,
        )
        _refresh_wfd_go_ies([phy_iface, name], dev_info)
        _cli("save_config", timeout=5.0)
        print(
            "[FluxCast WFD] L3: lease on formation channel; no CSA"
        )
        return name
    except WFDNotReady as exc:
        print(f"[FluxCast WFD] L3: no lease on formation channel ({exc})")
        if action != PAYLOAD_CSA_24:
            raise WFDNotReady(
                f"No DHCP lease on {go_mhz} MHz and payload plan is stay"
            ) from exc

    time.sleep(1.0)
    print(
        f"[FluxCast WFD] Payload: CSA {go_mhz}→{PAYLOAD_CSA_24_MHZ} MHz "
        "after formation-channel DHCP failed"
    )
    formation_mhz = go_mhz
    csa_mark = _apply_payload_csa(name, PAYLOAD_CSA_24_MHZ)
    print(
        "[FluxCast WFD] Payload: waiting for AP-STA-CONNECTED after CSA "
        f"(up to {int(POST_CSA_STA_WAIT_S)}s) then DHCP immediately"
    )
    if _wait_go_sta(
        name, csa_mark, POST_CSA_STA_WAIT_S, require_log_connect=True
    ):
        print("[FluxCast WFD] L3: STA on 2.4 GHz; starting DHCP")
        configure_ip(
            name, peer_mac, "P2P-GO", physical_iface=None, lease_timeout=40.0
        )
        _cli("save_config", timeout=5.0)
        return name

    # STA did not rejoin on 2.4 (often stays on the formation channel with
    # no DISCONNECT). Hop back and DHCP there instead of failing the session.
    print(
        "[FluxCast WFD] Payload: no 2.4 GHz rejoin; reverting GO to "
        f"{formation_mhz} MHz (STA likely still on formation channel)"
    )
    _apply_payload_csa(name, formation_mhz)
    time.sleep(1.0)
    print(
        "[FluxCast WFD] L3: DHCP on formation channel after CSA revert"
    )
    configure_ip(
        name, peer_mac, "P2P-GO", physical_iface=None, lease_timeout=40.0
    )
    _cli("save_config", timeout=5.0)
    return name


def parse_default_route_iface(
    ip_route_text: str = "", proc_route_text: str = ""
) -> str:
    """First IPv4 default-route iface from ``ip route`` or ``/proc/net/route``."""
    for line in (ip_route_text or "").splitlines():
        if not line.startswith("default"):
            continue
        parts = line.split()
        if "dev" in parts:
            i = parts.index("dev")
            if i + 1 < len(parts):
                return parts[i + 1]
    for line in (proc_route_text or "").splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[1] == "00000000":
            return cols[0]
    return ""


def default_route_iface() -> str:
    """Live IPv4 default-route device, or empty if none / unreadable."""
    ip_text = ""
    try:
        ip_text = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        ip_text = ""
    proc_text = ""
    try:
        proc_text = Path("/proc/net/route").read_text(errors="replace")
    except OSError:
        proc_text = ""
    return parse_default_route_iface(ip_text, proc_text)


def iface_is_sta_uplink(iface: str, *, uplink: Optional[str] = None) -> bool:
    """True if iface currently owns the IPv4 default route (internet STA)."""
    if not iface:
        return False
    cur = uplink if uplink is not None else default_route_iface()
    return bool(cur) and cur == iface


def _nmcli_unpriv(args: list[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(
            ["nmcli", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip()


def _nm_managed_value(iface: str) -> str:
    return _nmcli_unpriv(
        ["-g", "GENERAL.NM-MANAGED", "device", "show", iface]
    ).lower()


def _nm_connection_name(iface: str) -> str:
    name = _nmcli_unpriv(
        ["-g", "GENERAL.CONNECTION", "device", "show", iface]
    )
    if name in ("", "--"):
        return ""
    return name


def snapshot_sta_uplink(iface: str) -> dict:
    """Record NM/STA state before dedicated wpa so restore can undo hijack."""
    snap = {
        "iface": iface,
        "connection": _nm_connection_name(iface),
        "managed": _nm_managed_value(iface) or "yes",
        "default_iface": default_route_iface(),
    }
    snap["was_uplink"] = bool(snap["default_iface"]) and snap["default_iface"] == iface
    try:
        _CTRL.mkdir(parents=True, exist_ok=True)
        _STA_SNAP.write_text(json.dumps(snap, indent=2) + "\n")
    except OSError as exc:
        print(f"[FluxCast WFD] Warning: could not write STA snapshot: {exc}")
    return snap


def load_sta_snapshot() -> dict:
    try:
        data = json.loads(_STA_SNAP.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def restore_sta_uplink(iface: Optional[str] = None) -> None:
    """Hand the radio back to NetworkManager and bring the saved STA up.

    Never restarts NetworkManager. Best-effort: used after dedicated-path
    failure/stop so a hijacked uplink does not need a reboot.
    """
    snap = load_sta_snapshot()
    target = safe_iface(iface or str(snap.get("iface") or ""))
    if not target:
        return
    try:
        from .wpas_ip import _stop_go_dnsmasq

        _stop_go_dnsmasq()
    except Exception:
        pass
    _run(["pkill", "-f", "dnsmasq.*fluxcast-dnsmasq"], timeout=5.0)

    phy = _phy_of(target)
    for path in Path("/sys/class/net").glob("p2p-*"):
        if phy and _phy_of(path.name) != phy:
            continue
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

    _run(["nmcli", "device", "set", target, "managed", "yes"], timeout=8.0)
    p2p_dev = f"p2p-dev-{target}"
    _run(["nmcli", "device", "set", p2p_dev, "managed", "yes"], timeout=5.0)

    conn = str(snap.get("connection") or "").strip()
    if conn:
        print(f"[FluxCast WFD] Restoring STA connection {conn!r} on {target}")
        up = _run(["nmcli", "connection", "up", conn], timeout=20.0)
        if up.returncode != 0:
            print(
                "[FluxCast WFD] nmcli connection up failed "
                f"({(up.stderr or up.stdout or '').strip()}); trying device connect"
            )
            _run(["nmcli", "device", "connect", target], timeout=20.0)
    elif snap.get("was_uplink"):
        print(f"[FluxCast WFD] Restoring NM device connect on uplink {target}")
        _run(["nmcli", "device", "connect", target], timeout=20.0)

    try:
        _STA_SNAP.unlink()
    except OSError:
        pass


def needs_dedicated_wpa(
    iface: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    uplink: Optional[str] = None,
    managed: Optional[str] = None,
) -> bool:
    """True when this iface must not share NetworkManager's STA wpa.

    Dedicated wpa is for a *secondary* radio (USB dongle, extra NIC,
    already-unmanaged non-uplink). It must never run on the IPv4
    default-route iface — that steals the STA phy from NM and drops
    internet.

    ``FLUXCAST_USB_WPA`` is a wpa_supplicant *binary path*, not a gate.
    Empty nmcli output is treated as managed (fail closed).
    """
    if not iface:
        return False
    environ = env if env is not None else os.environ
    flag = str(environ.get("FLUXCAST_DEDICATED_WPA", "")).strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if iface_is_sta_uplink(iface, uplink=uplink):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    if _is_usb_wifi(iface):
        return True
    managed_val = managed if managed is not None else _nm_managed_value(iface)
    return managed_val in ("no", "false", "0")


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
    return run_privileged(cmd, timeout=timeout)


def _wpa_cli(iface: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return _run(
        ["wpa_cli", "-p", str(_CTRL), "-i", iface, *args], timeout=timeout
    )


def ensure_dedicated_wpa(iface: str) -> None:
    """nmcli unmanaged + dedicated wpa_supplicant on iface."""
    iface = safe_iface(iface)
    if not iface:
        raise WFDNotReady("Invalid P2P wifi interface name")
    if iface_is_sta_uplink(iface):
        raise WFDNotReady(
            f"{iface} is the default-route STA radio; dedicated wpa would "
            "steal internet. Use NetworkManager P2P on this iface, or a "
            "secondary radio."
        )
    snapshot_sta_uplink(iface)
    try:
        _ensure_dedicated_wpa_inner(iface)
    except BaseException:
        print(
            f"[FluxCast WFD] Dedicated wpa failed on {iface}; restoring STA/NM"
        )
        restore_sta_uplink(iface)
        raise


def _ensure_dedicated_wpa_inner(iface: str) -> None:
    for line in apply_radio_stability(iface):
        print(f"[FluxCast WFD] {line}")
    _CTRL.mkdir(parents=True, exist_ok=True)
    conf = _ensure_wpa_conf()
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
                _wpa_cli(iface, "set", "device_name", _DEVICE_NAME, timeout=3.0)
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
            str(conf),
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
    """Hint GO operating channel from local phy ∩ peer (Wi-Fi Direct).

    Never ``p2p_connect freq=``. ``p2p_oper_*`` / ``p2p_pref_chan`` are
    hints; wpa still intersects Channel Lists. Keep 5 GHz in the GO set
    when the peer listens there (WPS stays on the Neg channel) or when
    this phy can carry 5 GHz WFD. Drop 5 GHz only if this radio cannot
    stream it *and* the peer is listening on 2.4.
    """
    phy_text = _iw_phy_text(iface)
    radio = parse_iw_phy_radio(phy_text)
    occ = _sta_occupied_mhz(iface)
    # iw 6.x: survey dump is `iw dev <iface> survey dump`, not phy.
    survey = _run(["iw", "dev", iface, "survey", "dump"], timeout=5.0).stdout or ""
    busy = parse_iw_survey_busy_ratio(survey)
    ghz2 = parse_iw_phy_2ghz_channels(phy_text)
    ghz5 = parse_iw_phy_5ghz_channels(phy_text)
    listen_freq = peer_listen_freq(peer_info, default=0)
    peer_5ghz = listen_freq >= 5000 or peer_advertises_5ghz(peer_info)
    # Formation on the peer listen channel (P2P). Payload 5 GHz only if
    # this phy can stream it, or the caller opted in, or WPS needs it.
    use_5ghz = bool(
        go_5ghz
        or listen_freq >= 5000
        or (radio_can_stream_5ghz(radio) and peer_5ghz)
    )
    prefer_24 = listen_freq if 2400 <= listen_freq < 2500 else (
        None if use_5ghz else 2437
    )
    env_mhz = os.environ.get("FLUXCAST_WFD_GO_2GHZ_MHZ", "").strip()
    if env_mhz.isdigit():
        mhz = int(env_mhz)
        if 2400 <= mhz < 2500 and any(f == mhz for _, f in ghz2):
            prefer_24 = mhz
            print(
                f"[FluxCast WFD] GO 2.4 override {mhz} MHz "
                "(FLUXCAST_WFD_GO_2GHZ_MHZ; not p2p_connect freq=)"
            )
    pick = None
    band = "2.4 GHz"
    if use_5ghz:
        # Clear leftover exclude / pref from a prior USB session on reused wpa.
        _cli("set", "p2p_no_go_freq", "")
        _cli("set", "p2p_pref_chan", "")
        if go_5ghz:
            why = "--wfd-go-5ghz"
        elif listen_freq >= 5000:
            why = f"peer listen {listen_freq} MHz"
        else:
            why = "phy can stream 5 GHz and peer advertises it"
        print(f"[FluxCast WFD] GO 5 GHz kept ({why})")
        prefer_5 = listen_freq if listen_freq >= 5000 else None
        if prefer_5 is None:
            try:
                op = int(peer_info.get("oper_freq") or 0)
            except (TypeError, ValueError):
                op = 0
            if op >= 5000:
                prefer_5 = op
        pick5 = first_quiet_5ghz_oper(ghz5, occ, prefer_freq=prefer_5)
        if pick5 and (peer_5ghz or peer_advertises_5ghz(peer_info)):
            pick = pick5
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
    if not use_5ghz:
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
                f"[FluxCast WFD] GO channel set kept {freq} MHz; "
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


def _apply_local_p2p_width(_cli, iface: str) -> dict[str, object]:
    radio = parse_iw_phy_radio(_iw_phy_text(iface))
    ht40, vht = go_width_flags_for_radio(radio)
    _cli("set", "p2p_go_ht40", ht40)
    _cli("set", "p2p_go_vht", vht)
    width = "20 MHz" if ht40 == "0" and vht == "0" else "HT40/VHT advertised"
    print(
        f"[FluxCast WFD] GO width {width} (ht40={ht40} vht={vht}); "
        f"nss={radio.get('nss')} 5ghz={int(bool(radio.get('has_5ghz')))} "
        "(wpa intersects peer Channel List; not pinned)"
    )
    return radio


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
    """Form a finished P2P group on this phy; return data iface with IP.

    Dedicated wpa on a secondary radio (not NM's STA daemon). Channel,
    width, and GO intent come from this phy ∩ the peer (Wi-Fi Direct /
    WFD). Never ``p2p_connect freq=``.
    """
    iface = safe_iface(iface)
    peer = safe_mac(peer_mac)
    if not iface:
        raise WFDNotReady("Invalid P2P wifi interface name")
    if not peer:
        raise WFDNotReady("Invalid P2P peer MAC")
    for line in apply_radio_stability(iface):
        print(f"[FluxCast WFD] {line}")
    phy = _phy_of(iface)
    # Reuse before ensure_dedicated_wpa — that helper kills leftover wpa and
    # deletes p2p-* , which tears down a BSS the sink is still on
    # (e.g. after a DHCP miss) and then p2p_find cannot see it.
    live = _live_group_on_phy(phy, peer)
    if live:
        print(
            f"[FluxCast WFD] Dedicated P2P on {iface} (phy={phy}); "
            "primary STA radio left on NetworkManager for internet"
        )
        print(
            f"[FluxCast WFD] Reusing live USB group {live} "
            "(skip GO Neg — do not tear down a working BSS)"
        )
        mark_unmanaged(live)
        _wpa_cli(iface, "set", "wifi_display", "1", timeout=3.0)
        _wpa_cli(
            iface, "wfd_subelem_set", "0",
            wfd_wpa_cli_device_info_hex(rtsp_port), timeout=3.0,
        )
        _wpa_cli(
            iface, "wfd_subelem_set", "8",
            wfd_wpa_cli_local_ip_hex(), timeout=3.0,
        )
        configure_ip(
            live,
            peer_mac,
            "P2P-GO" if "GO" in _iw_info(live) else "P2P-client",
            physical_iface=None,
        )
        return live

    idle_go = None
    for path in Path("/sys/class/net").glob("p2p-*"):
        if _phy_of(path.name) != phy:
            continue
        info = _iw_info(path.name)
        if "type P2P-GO" in info and not is_pending_go_shell(info):
            idle_go = path.name
            break
    if idle_go:
        # Wi-Fi Direct: an operating GO BSS is the group. WFD RTSP is bound
        # to that BSS. Do not restart wpa or p2p_group_remove while the
        # sink may still be joining (P2P Invitation / client join). A new
        # p2p_connect is a *different* DIRECT- SSID and aborts the TV UI.
        ping = _wpa_cli(iface, "ping", timeout=3.0)
        wpa_alive = ping.returncode == 0 and "PONG" in (ping.stdout or "")
        if wpa_alive:
            print(
                f"[FluxCast WFD] Keeping operating GO {idle_go} "
                "(P2P Invitation / wait for STA; not tearing the BSS)"
            )
            _chmod_wpa_log()
            _wpa_cli(iface, "set", "wifi_display", "1", timeout=3.0)
            _wpa_cli(
                iface, "wfd_subelem_set", "0",
                wfd_wpa_cli_device_info_hex(rtsp_port), timeout=3.0,
            )
            _wpa_cli(
                iface, "wfd_subelem_set", "8",
                wfd_wpa_cli_local_ip_hex(), timeout=3.0,
            )
            inv = _wpa_cli(
                iface,
                "p2p_invite",
                f"group={idle_go}",
                f"peer={peer}",
                timeout=8.0,
            )
            inv_out = (inv.stdout or inv.stderr or "").strip()
            if inv.returncode == 0 and "FAIL" not in inv_out.upper():
                print(
                    f"[FluxCast WFD] p2p_invite group={idle_go} peer={peer}"
                )
            else:
                print(
                    f"[FluxCast WFD] p2p_invite skipped ({inv_out or 'FAIL'}); "
                    "waiting for sink on existing BSS"
                )
            deadline = time.time() + 30.0
            while time.time() < deadline:
                if _is_live_group(idle_go, "P2P-GO"):
                    print(f"[FluxCast WFD] Sink on operating GO {idle_go}")
                    mark_unmanaged(idle_go)
                    configure_ip(
                        idle_go, peer_mac, "P2P-GO", physical_iface=None
                    )
                    return idle_go
                time.sleep(0.5)
            print(
                "[FluxCast WFD] Operating GO empty after invite/wait; "
                "removing group then new GO Neg"
            )
        else:
            print(
                f"[FluxCast WFD] GO {idle_go} has no live wpa; tearing down"
            )
        _teardown_phy_groups(phy)

    ensure_dedicated_wpa(iface)
    _chmod_wpa_log()
    print(
        f"[FluxCast WFD] Dedicated P2P on {iface} (phy={phy}); "
        "primary STA radio left on NetworkManager for internet"
    )

    def _cli(*args: str, timeout: float = 10.0):
        return _wpa_cli(iface, *args, timeout=timeout)

    _cli("set", "device_name", _DEVICE_NAME)
    _cli("set", "wifi_display", "1")
    port = max(1, min(int(rtsp_port), 65535))
    dev_info = wfd_wpa_cli_device_info_hex(port)
    _cli("wfd_subelem_set", "0", dev_info)
    _cli("wfd_subelem_set", "8", wfd_wpa_cli_local_ip_hex())

    # Listen defaults; overridden to the peer listen_freq after find.
    # Do not set p2p_oper_*: a leftover 81/6 pin fights 5 GHz intersection.
    _cli("set", "p2p_listen_reg_class", "81")
    _cli("set", "p2p_listen_channel", "6")

    cached_listen = _cached_listen_mhz(peer_mac)
    radio = parse_iw_phy_radio(_iw_phy_text(iface))
    allow_weak = os.environ.get(
        "FLUXCAST_WFD_ALLOW_WEAK_RADIO", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if cached_listen:
        early = wfd_radio_reject_reason(
            radio, cached_listen, allow_weak=allow_weak, peer_has_24=True
        )
        if early:
            raise WFDNotReady(f"{iface}: {early}")
    intents = _usb_go_intents(go_intent, radio)

    last_err = "USB P2P group formation failed"
    for intent in intents:
        _teardown_phy_groups(phy)
        _cli("p2p_cancel", timeout=3.0)
        _cli("set", "p2p_go_intent", str(intent))
        _run(["ip", "link", "set", iface, "up"], timeout=5.0)
        log_mark = _LOG.stat().st_size if _LOG.exists() else 0
        found = False
        compact_peer = peer.replace(":", "")

        def _peer_visible() -> bool:
            delta = _neg_log_delta(log_mark).lower()
            if f"p2p-device-found {peer}" in delta:
                return True
            if "p2p-go-neg-request" in delta and compact_peer in delta.replace(
                ":", ""
            ):
                return True
            peers = _cli("p2p_peers", timeout=1.0)
            return peer in (peers.stdout or "").lower()

        def _wait_visible(seconds: int) -> bool:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if _peer_visible():
                    return True
                time.sleep(0.4)
            return _peer_visible()

        # p2p_find interleaves scan + listen. Listen-only first (~35s)
        # missed a 5 GHz-listen sink that social find saw in ~4s.
        print(
            f"[FluxCast WFD] Discovering {peer_mac} as '{_DEVICE_NAME}' "
            "(find first; listen fallback) — open Screen Share / "
            "Miracast receive on the sink"
        )
        for step in usb_discovery_steps(cached_listen):
            mode = str(step["mode"])
            secs = int(step["seconds"])
            freq = step.get("freq")
            if mode == "listen":
                mhz = int(freq or 2437)
                listen_reg, listen_ch = freq_to_social_channel(mhz)
                _cli("set", "p2p_listen_reg_class", listen_reg)
                _cli("set", "p2p_listen_channel", listen_ch)
                print(f"[FluxCast WFD] Listening on {mhz} MHz ({secs}s)")
                _cli("p2p_listen", str(secs), timeout=5.0)
            elif mode == "find_social":
                listen_reg, listen_ch = freq_to_social_channel(2437)
                _cli("set", "p2p_listen_reg_class", listen_reg)
                _cli("set", "p2p_listen_channel", listen_ch)
                print(f"[FluxCast WFD] p2p_find {secs}s type=social")
                r = _cli("p2p_find", str(secs), "type=social", timeout=5.0)
                out = (r.stdout or r.stderr or "").strip()
                if r.returncode != 0 or "FAIL" in out.upper():
                    _cli("p2p_find", str(secs), timeout=5.0)
            elif mode == "find_freq":
                mhz = int(freq or 0)
                listen_reg, listen_ch = freq_to_social_channel(mhz)
                _cli("set", "p2p_listen_reg_class", listen_reg)
                _cli("set", "p2p_listen_channel", listen_ch)
                print(f"[FluxCast WFD] p2p_find {secs}s freq={mhz}")
                r = _cli("p2p_find", str(secs), f"freq={mhz}", timeout=5.0)
                out = (r.stdout or r.stderr or "").strip()
                if r.returncode != 0 or "FAIL" in out.upper():
                    _cli("p2p_find", str(secs), timeout=5.0)
            else:
                print(f"[FluxCast WFD] p2p_find {secs}s")
                _cli("p2p_find", str(secs), timeout=5.0)
            if _wait_visible(secs):
                found = True
                break
            _cli("p2p_stop_find", timeout=3.0)
        _cli("p2p_stop_find", timeout=3.0)
        if not found:
            last_err = (
                f"Peer {peer_mac} not seen. On the TV open Screen Share / "
                f"Miracast receive so it can find '{_DEVICE_NAME}'."
            )
            continue

        peer_info = parse_p2p_peer_info(
            (_cli("p2p_peer", peer, timeout=5.0).stdout or "")
        )
        live_listen = peer_listen_freq(peer_info, default=2437)
        listen_freq = listen_freq_for_wps(peer_info, cached_listen)
        if listen_freq != live_listen:
            print(
                f"[FluxCast WFD] Peer snapshot listen {live_listen} MHz "
                f"after 2.4 find; keeping scan-cache {listen_freq} MHz "
                "for WPS"
            )
            peer_info = dict(peer_info)
            peer_info["listen_freq"] = str(listen_freq)
        reject = wfd_radio_reject_reason(
            radio,
            listen_freq,
            allow_weak=allow_weak,
            peer_has_24=peer_advertises_24ghz(peer_info),
        )
        if reject:
            raise WFDNotReady(f"{iface}: {reject}")
        if peer_wps_not_ready(peer_info):
            wps = peer_info.get("wps_method") or "?"
            print(
                f"[FluxCast WFD] Peer {peer_mac} wps_method={wps}; "
                "GO Neg (PBC) without waiting for WPS-ready"
            )
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
        persistent_reinvoke = False
        # PLAY success does not mean the sink knows the persistent group
        # (INVITATION-RESULT status=8). Only reinvite after a prior
        # invite actually succeeded.
        if peer_has_invite_ok(peer_mac):
            ids = _persistent_go_ids(
                (_cli("list_networks", timeout=5.0).stdout or "")
            )
            if ids:
                nid = ids[0]
                print(
                    f"[FluxCast WFD] Reinvoking persistent group id={nid} "
                    "(previous invite succeeded)"
                )
                add = _cli("p2p_group_add", f"persistent={nid}", timeout=10.0)
                add_out = (add.stdout or add.stderr or "").strip()
                if add.returncode == 0 and "FAIL" not in add_out.upper():
                    inv = _cli(
                        "p2p_invite",
                        f"persistent={nid}",
                        f"peer={peer}",
                        timeout=10.0,
                    )
                    inv_out = (inv.stdout or inv.stderr or "").strip()
                    inv_ok = False
                    if inv.returncode == 0 and "FAIL" not in inv_out.upper():
                        until = time.time() + 2.5
                        while time.time() < until:
                            dlt = _neg_log_delta(log_mark)
                            if re.search(
                                r"P2P-INVITATION-RESULT status=0\b", dlt
                            ):
                                inv_ok = True
                                break
                            if re.search(
                                r"P2P-INVITATION-RESULT status=", dlt
                            ):
                                break
                            if _is_live_group(
                                next(
                                    (
                                        p.name
                                        for p in Path("/sys/class/net").glob("p2p-*")
                                        if _phy_of(p.name) == phy
                                    ),
                                    "",
                                ),
                                "P2P-GO",
                            ):
                                inv_ok = True
                                break
                            time.sleep(0.3)
                    if inv_ok:
                        persistent_reinvoke = True
                        mark_p2p_invite_ok(peer_mac)
                    else:
                        print(
                            "[FluxCast WFD] Persistent invite rejected; "
                            "falling back to GO Neg"
                        )
                        _cli("p2p_group_remove", "*", timeout=5.0)
                        _teardown_phy_groups(phy)
                        time.sleep(0.4)
        if not persistent_reinvoke:
            print(
                f"[FluxCast WFD] p2p_connect {peer_mac} pbc persistent "
                f"go_intent={intent} listen={listen_freq} "
                "(oper class negotiated, not pinned) "
                "(wait for GO-NEG + GROUP-STARTED)..."
            )
            conn = _cli(
                "p2p_connect",
                peer,
                "pbc",
                "persistent",
                f"go_intent={intent}",
                timeout=10.0,
            )
            conn_out = (conn.stdout or conn.stderr or "").strip()
            if conn.returncode != 0 or "FAIL" in conn_out.upper():
                print(f"[FluxCast WFD] p2p_connect failed ({conn_out}); trying join")
                conn = _cli(
                    "p2p_connect", peer, "pbc", "join", timeout=10.0
                )
                conn_out = (conn.stdout or conn.stderr or "").strip()
                if conn.returncode != 0 or "FAIL" in conn_out.upper():
                    last_err = f"USB p2p_connect failed: {conn_out}"
                    continue

        deadline = time.time() + 40.0
        saw_pending_go = ""
        saw_neg_resp = False
        acl_primed = False
        while time.time() < deadline:
            delta = _neg_log_delta(log_mark)
            if (
                not saw_neg_resp
                and "Received GO Negotiation Response" in delta
            ):
                saw_neg_resp = True
                deadline = time.time() + 70.0
                print(
                    "[FluxCast WFD] GO Neg Response received — "
                    "holding group until GROUP-STARTED (not tearing pending GO)"
                )
            if "WPS-PBC-ACTIVE" in delta or "AP-ENABLED" in delta:
                deadline = max(deadline, time.time() + 70.0)
            if (
                not acl_primed
                and (
                    "WPS-PBC-ACTIVE" in delta
                    or "P2P-GO-NEG-SUCCESS" in delta
                )
            ):
                go_for_acl = saw_pending_go
                if not go_for_acl:
                    for path in Path("/sys/class/net").glob("p2p-*"):
                        if _phy_of(path.name) == phy:
                            go_for_acl = path.name
                            break
                if go_for_acl:
                    macs = [peer, *(_peer_macs_from_neg_log(delta))]
                    _allow_macs_on_go_acl(go_for_acl, macs)
                    acl_primed = True
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
                still_go = False
                for path in Path("/sys/class/net").glob("p2p-*"):
                    if _phy_of(path.name) == phy and _is_live_group(
                        path.name, "P2P-GO"
                    ):
                        still_go = True
                        break
                if not still_go:
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
                    # GO beacons + STA assoc can finish before the
                    # GROUP-STARTED line is in the log; don't miss a live BSS.
                    if (
                        "P2P-GROUP-STARTED" not in delta
                        and not _is_live_group(name, "P2P-GO")
                    ):
                        continue
                    if _is_live_group(name, "P2P-GO"):
                        print(f"[FluxCast WFD] GO {name}: GROUP-STARTED")
                        return _finish_live_go(
                            name,
                            phy_iface=iface,
                            peer_info=peer_info,
                            go_5ghz=go_5ghz,
                            log_mark=log_mark,
                            peer_mac=peer_mac,
                            dev_info=dev_info,
                            _cli=_cli,
                        )
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

    If a live BSS+STA is up on a *secondary* radio, leave it: tearing it
    down to 'start Extend' is how we lost a working test-pattern group.
    If this iface is (or was) the STA uplink, always restore NM/STA —
    keeping a hijacked GO on the internet radio is how Wi-Fi stayed down
    until reboot. Explicit `miracast-ctl stop` still deletes groups.
    """
    snap = load_sta_snapshot()
    target = iface or str(snap.get("iface") or "")
    was_uplink = bool(snap.get("was_uplink")) or iface_is_sta_uplink(target)
    phy = _phy_of(target) if target else ""
    if (not was_uplink) and phy and _live_group_on_phy(phy, ""):
        print(
            f"[FluxCast WFD] Keeping live USB group on {phy} "
            "(Extend can attach without re-running GO Neg)"
        )
        return
    restore_sta_uplink(target or None)
