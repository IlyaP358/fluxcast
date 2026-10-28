"""Direct wpa_supplicant P2P backend.

An alternative to nm.py: instead of asking NetworkManager to bring up the
P2P link, this module talks to wpa_supplicant's own D-Bus P2PDevice
interface directly (Find, Connect, GroupStarted). NetworkManager is never
asked to manage the connection, so it stays out of the way entirely.

The payoff is control that NetworkManager's own API doesn't expose: we can
set the WFD Device Info IE and the P2P operating channel ourselves, and
watch each step of the negotiation up close when something needs debugging.

On a shared STA radio, NM keeps the uplink (wlp*) and must not own P2P
(p2p-dev-*, p2p-*); see meta/99-fluxcast-p2p.conf. USB dedicated wpa is
still a second phy.

See wpas_ip.py for everything that happens once a group exists - role
detection and IP configuration live there to keep this file focused.

Discovery, GO-intent handling, and device naming already have solid
implementations elsewhere in this codebase (peers.py, device.py); this
module's job is just to drive the connection itself, replacing
_connect_peer / _wait_for_nm_activation from nm.py.
"""

import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import WFDNotReady
from ..constants import WFD_RTSP_PORT
from ..ie import wfd_wpa_cli_device_info_hex, wfd_wpa_cli_local_ip_hex
from ..privileged import run_privileged
from .dbus import (
    WPA_DEST, _gdbus_call, _object_paths, _variant_byte_array, _wfd_source_ie,
    _wpas_get_property, _wpas_get_string,
)
from .device import _p2p_device_iface_paths, _set_p2p_go_intent, _set_p2p_oper_channel
from .peers import _default_wifi_interface
from .wpas_ip import (
    configure_ip, get_p2p_role, mark_unmanaged,
    parse_p2p_group_started_addrs, release_ip_config,
)
WPA_IFACE = "fi.w1.wpa_supplicant1.Interface"
WPA_P2P_IFACE = "fi.w1.wpa_supplicant1.Interface.P2PDevice"


def _wpas_find_peer_path(iface_path: str, mac: str) -> Optional[str]:
    """Resolve a MAC address to its wpa_supplicant P2P peer object path.

    wpa_supplicant names peer objects after their MAC address directly, e.g.
    .../Peers/46d244e4372f for 46:D2:44:E4:37:2F (lowercase, colons
    stripped), so we can match on the object path itself rather than doing
    an extra Properties.Get round-trip per peer to read DeviceAddress.
    """
    peers_raw = _wpas_get_property(iface_path, WPA_P2P_IFACE, "Peers", privileged=True)
    target = mac.lower().replace(":", "")
    for peer_path in _object_paths(peers_raw):
        suffix = peer_path.rsplit("/", 1)[-1].lower()
        if suffix == target:
            return peer_path
    return None


def _set_wfd_ies(rtsp_port: int) -> None:
    """Tell wpa_supplicant our WFD Device Info subelement before it ever
    negotiates or advertises anything.

    WFDIEs is a property of the root fi.w1.wpa_supplicant1 service object
    (global, not per-interface). The nm.py backend sets this correctly by
    passing the same bytes (_wfd_source_ie, built from ie.py's "Source"
    device-info bitmap) to NetworkManager as the 'wfd-ies' connection
    setting, which NM then forwards to wpa_supplicant on our behalf. Since
    this backend never goes through NetworkManager, nothing was writing
    this property here, and wpa_supplicant would just keep whatever value
    happened to be set beforehand - including, during testing, one that
    declared us a Sink rather than a Source. Setting it ourselves here,
    before Find/Connect, means this backend always advertises correctly and
    never depends on any prior manual setup.

    Unlike the read-only lookups elsewhere here, this one raises on
    failure, so without the sudo fallback a denied Set aborts every
    connection attempt outright.
    """
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", "/fi/w1/wpa_supplicant1",
        "--method", "org.freedesktop.DBus.Properties.Set",
        WPA_DEST, "WFDIEs",
        f"<{_variant_byte_array(_wfd_source_ie(rtsp_port))}>",
    ], timeout=5.0, privileged=True)
    if result.returncode != 0:
        raise WFDNotReady(
            f"Failed to set WFD Device Info IE: {(result.stderr or result.stdout).strip()}"
        )


def enable_wifi_display(physical_iface: Optional[str], rtsp_port: int) -> None:
    """Turn on wpa ``wifi_display`` + WFD subelements on the P2P ifaces.

    D-Bus WFDIEs alone is not enough: if ``wifi_display=0``, Connect is a
    generic Wi-Fi Direct pair (TV shows Wi-Fi Direct, no RTSP/Miracast).
    """
    if not physical_iface:
        return
    ifaces = [physical_iface, p2p_dev_ctrl_ifname(physical_iface)]
    hex0 = wfd_wpa_cli_device_info_hex(rtsp_port)
    hex8 = wfd_wpa_cli_local_ip_hex()
    enabled = False
    for iface in ifaces:
        r = run_privileged(["wpa_cli", "-i", iface, "set", "wifi_display", "1"], timeout=5.0)
        if r.returncode != 0:
            print(
                f"[FluxCast WFD] wifi_display set on {iface} failed: "
                f"{(r.stderr or r.stdout or '').strip()}"
            )
            continue
        run_privileged(
            ["wpa_cli", "-i", iface, "wfd_subelem_set", "0", hex0], timeout=5.0
        )
        run_privileged(
            ["wpa_cli", "-i", iface, "wfd_subelem_set", "8", hex8], timeout=5.0
        )
        chk = run_privileged(
            ["wpa_cli", "-i", iface, "get", "wifi_display"], timeout=5.0
        )
        val = (chk.stdout or "").strip().splitlines()
        shown = val[-1] if val else "?"
        print(f"[FluxCast WFD] wifi_display={shown} on {iface} (need 1 for Miracast)")
        if shown.strip() == "1":
            enabled = True
    if not enabled:
        print(
            "[FluxCast WFD] Warning: wifi_display is not 1; the sink may "
            "treat this as plain Wi-Fi Direct instead of Miracast"
        )


def flush_stale_p2p_groups(physical_iface: Optional[str]) -> None:
    """Drop leftover persistent DIRECT-* groups before a WFD Connect.

    Rejoining a previous Wi-Fi Direct persistent group looks like a
    generic P2P pair on the TV (no RTSP) and often DEAUTH_LEAVING right
    after P2P-GROUP-STARTED.
    """
    if not physical_iface:
        return
    for cmd in (
        ["p2p_cancel"],
        ["p2p_flush"],
        ["set", "persistent_reconnect", "0"],
    ):
        run_privileged(["wpa_cli", "-i", physical_iface, *cmd], timeout=5.0)
    listed = run_privileged(
        ["wpa_cli", "-i", physical_iface, "list_networks"], timeout=5.0
    )
    removed = 0
    for line in (listed.stdout or "").splitlines()[1:]:
        low = line.lower()
        if "direct-" not in low and "[p2p-persistent]" not in low:
            continue
        nid = line.split()[0]
        if not nid.isdigit():
            continue
        run_privileged(
            ["wpa_cli", "-i", physical_iface, "remove_network", nid], timeout=5.0
        )
        removed += 1
    if removed:
        print(f"[FluxCast WFD] Removed {removed} persistent DIRECT-* network(s)")
        run_privileged(
            ["wpa_cli", "-i", physical_iface, "save_config"], timeout=5.0
        )


def _find_peers(iface_path: str, timeout: int) -> None:
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", iface_path,
        "--method", f"{WPA_P2P_IFACE}.Find",
        f"{{'Timeout': <int32 {timeout}>}}",
    ], timeout=timeout + 3.0, privileged=True)
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())


def _stop_find(iface_path: str) -> None:
    try:
        _gdbus_call([
            "--dest", WPA_DEST,
            "--object-path", iface_path,
            "--method", f"{WPA_P2P_IFACE}.StopFind",
        ], timeout=3.0, privileged=True)
    except Exception:
        pass


def _wait_for_peer(iface_path: str, peer_mac: str, timeout: int = 20) -> str:
    """Find() kicks off a background scan and returns almost immediately -
    it doesn't block until discovery actually finishes. Polling for the
    peer is more reliable than sleeping a fixed duration, since real-world
    sinks can take well over ten seconds of air time to show up.
    """
    _find_peers(iface_path, timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        peer_path = _wpas_find_peer_path(iface_path, peer_mac)
        if peer_path:
            _stop_find(iface_path)
            return peer_path
        time.sleep(2)
    raise WFDNotReady(
        f"Peer {peer_mac} not in wpa_supplicant's peer list after {timeout}s. "
        "Confirm the sink is still in discoverable mode and re-run."
    )


def _channel_to_freq_mhz(channel: int) -> int:
    """Map a P2P channel number to center frequency in MHz."""
    if 1 <= channel <= 13:
        return 2407 + 5 * channel
    if channel == 14:
        return 2484
    if 32 <= channel <= 177:
        return 5000 + 5 * channel
    raise ValueError(f"unsupported P2P channel for freq mapping: {channel}")


def _wpas_connect(iface_path: str, peer_path: str, go_intent: int = 0,
                   wps_method: str = "pbc",
                   frequency_mhz: Optional[int] = None) -> None:
    # Optional Connect 'frequency' (MHz) hard-forces GO channel when MCC allows.
    freq_part = ""
    if frequency_mhz is not None:
        freq_part = f"'frequency': <int32 {int(frequency_mhz)}>, "
    args = (
        "{"
        f"'peer': <objectpath '{peer_path}'>, "
        f"'wps_method': <'{wps_method}'>, "
        f"'go_intent': <int32 {go_intent}>, "
        f"{freq_part}"
        "'persistent': <false>"
        "}"
    )
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", iface_path,
        "--method", f"{WPA_P2P_IFACE}.Connect",
        args,
    ], timeout=10.0, privileged=True)
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())


def _wpas_cli_connect(
    p2p_dev: str,
    peer_mac: str,
    go_intent: int,
    frequency_mhz: Optional[int],
) -> None:
    """Connect via P2P-device ctrl socket (not STA D-Bus Interfaces/0)."""
    extra = [f"freq={int(frequency_mhz)}"] if frequency_mhz else []
    print(f"[FluxCast WFD] wpa_cli -i {p2p_dev} p2p_connect …")
    result = _wpa_cli_iface(
        p2p_dev,
        "p2p_connect",
        peer_mac.lower(),
        "pbc",
        f"go_intent={go_intent}",
        *extra,
        timeout=15.0,
    )
    out = (result.stdout or result.stderr or "").strip()
    print(f"[FluxCast WFD] p2p_connect: {out or '(no output)'} rc={result.returncode}")
    if result.returncode != 0 or "FAIL" in out.upper():
        raise WFDNotReady(f"p2p_connect on {p2p_dev} failed: {out}")


def _list_wpas_interfaces() -> set[str]:
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", "/fi/w1/wpa_supplicant1",
        "--method", "org.freedesktop.DBus.Properties.Get",
        WPA_DEST, "Interfaces",
    ], privileged=True)  # wpas-only, so always escalates - see _gdbus_call
    if result.returncode != 0:
        return set()
    return set(_object_paths(result.stdout))


def _wait_for_go_peer_associated(data_iface: str, peer_mac: str,
                                  timeout: float = 35.0) -> None:
    """Wait for AP-STA-CONNECTED before IP setup (flush during WPS kills the GO)."""
    print(f"[FluxCast WFD] Waiting for AP-STA-CONNECTED ({peer_mac} on {data_iface})...")
    deadline = time.monotonic() + timeout
    peer = peer_mac.lower().replace("-", ":")
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    while time.monotonic() < deadline:
        info = subprocess.run(
            ["iw", "dev", data_iface, "info"],
            capture_output=True, text=True, timeout=5.0,
        )
        if info.returncode != 0 or "type P2P-GO" not in (info.stdout or ""):
            raise WFDNotReady(
                f"P2P group interface {data_iface} disappeared before "
                "AP-STA-CONNECTED (group formation likely failed)."
            )
        journal = subprocess.run(
            [
                "journalctl", "-b", "--since", since,
                "_COMM=wpa_supplicant", "--no-pager",
            ],
            capture_output=True, text=True, timeout=5.0,
        )
        text = (journal.stdout or "").lower()
        if "p2p-group-formation-failure" in text:
            raise WFDNotReady(
                "P2P-GROUP-FORMATION-FAILURE before AP-STA-CONNECTED"
            )
        if "ap-sta-connected" in text and peer in text:
            print(f"[FluxCast WFD] AP-STA-CONNECTED for {peer_mac} on {data_iface}.")
            return
        time.sleep(0.4)
    raise WFDNotReady(
        f"Timed out waiting for AP-STA-CONNECTED ({peer_mac} on {data_iface})."
    )


def _wpa_cli_iface(iface: str, *args: str, timeout: float = 5.0):
    return run_privileged(["wpa_cli", "-i", iface, *args], timeout=timeout)


def _wpa_ctrl_alive(iface: str) -> bool:
    r = _wpa_cli_iface(iface, "ping", timeout=3.0)
    return r.returncode == 0 and "PONG" in (r.stdout or "").upper()


def _nm_drop_wifi_p2p_placeholder(physical_iface: str) -> None:
    """Drop NM's wifi-p2p *device object* so it cannot handle GroupStarted.

    Does not delete the kernel P2P-device wdev or unmanage STA. NM may
    recreate an unmanaged placeholder; that is OK.
    """
    names = [f"p2p-dev-{physical_iface}", p2p_dev_ctrl_ifname(physical_iface)]
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        r = run_privileged(["nmcli", "device", "delete", name], timeout=8.0)
        out = (r.stdout or r.stderr or "").strip()
        if r.returncode == 0:
            print(f"[FluxCast WFD] Dropped NM wifi-p2p placeholder {name}")
        elif out and "not found" not in out.lower() and "No such" not in out:
            print(f"[FluxCast WFD] nmcli device delete {name}: {out}")


def p2p_dev_ctrl_ifname(physical_iface: str) -> str:
    """P2P-device ctrl ifname. Kernel IFNAMSIZ is 16; NM's p2p-dev-<sta>
    is often truncated (p2p-dev-wlp0s20f3 → p2p-dev-wlp0s20)."""
    full = f"p2p-dev-{physical_iface}"
    seen: list[str] = []
    for name in (full, full[:16], full[:15]):
        if name and name not in seen:
            seen.append(name)
    for name in seen:
        if _wpa_ctrl_alive(name):
            return name
    return seen[-1] if seen else full


def ensure_p2p_device(physical_iface: Optional[str]) -> None:
    """Ensure a P2P-device wdev for WFD IEs without giving it to NM.

    STA stays NM-managed. p2p-dev-* is unmanaged via 99-fluxcast-p2p.conf.
    Recreate via wpa interface_add if the ctrl socket is missing — do not
    nmcli managed yes (that undoes the split).
    """
    if not physical_iface:
        return
    p2p_dev = p2p_dev_ctrl_ifname(physical_iface)
    if _wpa_ctrl_alive(p2p_dev):
        print(f"[FluxCast WFD] P2P device {p2p_dev} is live (NM does not own it)")
        _nm_drop_wifi_p2p_placeholder(physical_iface)
        return
    print(f"[FluxCast WFD] Recreating {p2p_dev} (wpa P2P-device was missing)")
    # Per-iface wpa_cli has no interface_add (UNKNOWN COMMAND). D-Bus
    # CreateInterface is how NM-spawned wpa grows a P2P-device object.
    created = run_privileged(
        [
            "gdbus", "call", "--system",
            "--dest", WPA_DEST,
            "--object-path", "/fi/w1/wpa_supplicant1",
            "--method", f"{WPA_DEST}.CreateInterface",
            f"{{'Ifname': <'{p2p_dev}'>, 'Driver': <'nl80211'>}}",
        ],
        timeout=8.0,
    )
    print(
        f"[FluxCast WFD] CreateInterface {p2p_dev}: "
        f"{(created.stdout or created.stderr or '').strip() or '(no output)'} "
        f"rc={created.returncode}"
    )
    time.sleep(0.3)
    if _wpa_ctrl_alive(p2p_dev):
        print(f"[FluxCast WFD] P2P device {p2p_dev} recreated")
    else:
        print(
            f"[FluxCast WFD] Warning: {p2p_dev} still has no wpa ctrl; "
            "WFD IEs will go on the STA iface"
        )


def peer_oper_or_listen_mhz(physical_iface: Optional[str], peer_mac: str) -> int:
    """Peer oper_freq, else listen_freq, from wpa_cli p2p_peer."""
    if not physical_iface or not peer_mac:
        return 0
    mac = peer_mac.lower()
    text = ""
    for iface in (p2p_dev_ctrl_ifname(physical_iface), physical_iface):
        r = _wpa_cli_iface(iface, "p2p_peer", mac, timeout=5.0)
        if r.returncode == 0 and (r.stdout or "").strip():
            text = r.stdout or ""
            break
    listen = oper = 0
    for line in text.splitlines():
        if line.startswith("listen_freq="):
            try:
                listen = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("oper_freq="):
            try:
                oper = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    if 2400 <= oper <= 6000:
        return oper
    if 2400 <= listen <= 6000:
        return listen
    return 0


def should_force_connect_freq(force_mhz: int, peer_mhz: int) -> bool:
    """SCC-force only when the peer already lives on that primary.

    Forcing ChrisNet ch 44 (5220) onto an LG whose Miracast listen is
    5745 makes webOS start Smart Share (Wi-Fi Direct) instead of
    Screen Share (WFD).
    """
    if force_mhz <= 0:
        return False
    if peer_mhz <= 0:
        return True
    return abs(peer_mhz - force_mhz) < 10


def wait_for_p2p_carrier(iface: str, timeout: float = 20.0) -> None:
    """Wait until the group netdev has carrier / operstate=up.

    The D-Bus group iface appears during WPS, *before* the 4-way. Starting
    dhcpcd then races WPS and NM, which locally DEAUTH_LEAVING and leaves
    the netdev DOWN.
    """
    sysdir = Path(f"/sys/class/net/{iface}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not sysdir.exists():
            raise WFDNotReady(
                f"{iface} disappeared before carrier "
                "(P2P group torn down — often NM ignoring GroupStarted)"
            )
        try:
            oper = (sysdir / "operstate").read_text().strip()
        except OSError:
            oper = ""
        try:
            carrier = (sysdir / "carrier").read_text().strip()
        except OSError:
            carrier = "0"
        # dormant + carrier=1 is WPS-in-progress, not a finished BSS.
        if oper == "up":
            print(f"[FluxCast WFD] {iface} carrier up (operstate={oper})")
            return
        try:
            journal = subprocess.run(
                [
                    "journalctl", "-b", "--since", "30 sec ago",
                    "_COMM=wpa_supplicant", "--no-pager",
                ],
                capture_output=True, text=True, timeout=3.0, check=False,
            )
            jtxt = journal.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            jtxt = ""
        if f"P2P-GROUP-REMOVED {iface}" in jtxt and "FREQ_CONFLICT" in jtxt:
            raise WFDNotReady(
                f"{iface} P2P-GROUP-REMOVED reason=FREQ_CONFLICT "
                "(STA and P2P on different channels; wpa keeps STA and "
                "deauths the group). Pin P2P to the STA channel (SCC) "
                "or use a second radio."
            )
        if (
            f"P2P-GROUP-STARTED {iface}" in jtxt
            and "nl80211: deinit ifname=" + iface
            not in jtxt.split(f"P2P-GROUP-STARTED {iface}")[-1]
        ):
            print(f"[FluxCast WFD] {iface} GROUP-STARTED (waiting for operstate=up)")
        time.sleep(0.2)
    raise WFDNotReady(
        f"{iface} never got carrier (operstate stayed down; no 4-way / GROUP-STARTED)"
    )


def _wait_for_group_interface(before: set[str], timeout: float = 40.0) -> str:
    """Detect the new wpa_supplicant Interface object GO Negotiation creates,
    then read its Ifname to get the real OS network interface name.

    Do not mark the group unmanaged here: that nmcli call during WPS locally
    deauths the client (reason 3 DEAUTH_LEAVING). Unmanage p2p-dev *before*
    Connect, then the group iface after carrier.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new_paths = _list_wpas_interfaces() - before
        for path in new_paths:
            ifname = _wpas_get_string(path, WPA_IFACE, "Ifname", privileged=True)
            if ifname:
                return ifname
        time.sleep(0.5)
    raise WFDNotReady(
        "Timed out waiting for wpa_supplicant to form the P2P group "
        f"(no new data interface after {timeout:.0f}s)."
    )


def connect_via_wpa_supplicant(interface: Optional[str], peer_mac: str,
                                go_intent: int = 0,
                                rtsp_port: int = WFD_RTSP_PORT,
                                p2p_channel: Optional[int] = None) -> str:
    """Full connect flow bypassing NetworkManager. Returns the data interface
    name once it has a real IP address, ready for the RTSP server to use.
    """
    physical_iface = interface or _default_wifi_interface()
    ensure_p2p_device(physical_iface)
    paths = _p2p_device_iface_paths(interface, privileged=True)
    if not paths:
        raise WFDNotReady("wpa_supplicant P2P interface not found.")
    iface_path = paths[0]
    print(f"[FluxCast WFD] P2P Connect via {iface_path}")

    # Must happen before Find/Connect - see _set_wfd_ies's docstring.
    _set_wfd_ies(rtsp_port)
    enable_wifi_display(physical_iface, rtsp_port)
    flush_stale_p2p_groups(physical_iface)

    force_freq: Optional[int] = None
    if p2p_channel is not None:
        try:
            force_freq = _channel_to_freq_mhz(p2p_channel)
        except ValueError as exc:
            print(f"[FluxCast WFD] Warning: {exc}; Connect will not hard-force freq")
            force_freq = None

    # GO intent only matters for GO Negotiation (Connect()), so it's set
    # right before that call rather than up here alongside discovery.
    # Setting it earlier raced wpa_supplicant's P2P state machine: Find()
    # would report success, but Peers stayed empty.
    previous_intent = None
    try:
        peer_path = _wait_for_peer(iface_path, peer_mac)

        previous_intent = _set_p2p_go_intent(interface, go_intent, privileged=True)
        _wpa_cli_iface(physical_iface, "set", "p2p_ignore_shared_freq", "0")
        _wpa_cli_iface(physical_iface, "set", "p2p_group_idle", "0")
        peer_mhz = peer_oper_or_listen_mhz(physical_iface, peer_mac)
        # On the STA phy, wpa deletes a different-channel P2P group
        # immediately (P2P-GROUP-REMOVED FREQ_CONFLICT / DEAUTH_LEAVING)
        # because STA has priority. USB (no STA on that phy) can follow
        # the peer's 5745. Same-phy wpas must SCC with ChrisNet.
        sta_shared = False
        try:
            from .usb_dedicated import iface_is_sta_uplink

            sta_shared = iface_is_sta_uplink(physical_iface or "")
        except Exception:
            sta_shared = False
        if sta_shared and force_freq:
            print(
                f"[FluxCast WFD] STA-shared wpas: pinning P2P to {force_freq} MHz "
                f"(SCC with uplink). Peer listen/oper {peer_mhz or '?'} MHz "
                "would FREQ_CONFLICT and DEAUTH the group."
            )
            _set_p2p_oper_channel(interface, p2p_channel)
        elif force_freq is not None and not should_force_connect_freq(force_freq, peer_mhz):
            print(
                f"[FluxCast WFD] Not forcing {force_freq} MHz; "
                f"peer listen/oper is {peer_mhz} MHz"
            )
            force_freq = None
        elif force_freq is not None:
            _set_p2p_oper_channel(interface, p2p_channel)
        # Do not unmanage p2p-dev: that destroyed NM's P2P device (unavailable,
        # no ctrl socket) so WFD IEs never left the P2P-device wdev.
        interfaces_before = _list_wpas_interfaces()
        print(f"[FluxCast WFD] Connecting to {peer_mac} directly via wpa_supplicant "
              "(NetworkManager not involved in this step)...")
        if force_freq is not None:
            print(f"[FluxCast WFD] Connect frequency={force_freq} MHz "
                  f"(channel {p2p_channel})")
        p2p_dev = p2p_dev_ctrl_ifname(physical_iface) if physical_iface else ""
        if p2p_dev and _wpa_ctrl_alive(p2p_dev):
            print(f"[FluxCast WFD] Connect on P2P-device {p2p_dev} (not STA D-Bus)")
            _wpas_cli_connect(p2p_dev, peer_mac, go_intent, force_freq)
        else:
            print(f"[FluxCast WFD] Connect via D-Bus {iface_path} (no P2P-device ctrl)")
            _wpas_connect(
                iface_path, peer_path, go_intent=go_intent, frequency_mhz=force_freq
            )

        data_iface = _wait_for_group_interface(interfaces_before)
        role = get_p2p_role(data_iface)
        print(f"[FluxCast WFD] P2P group formed on {data_iface}; our role: {role}")
        if p2p_channel is not None and role != "P2P-GO":
            # The operating channel is the Group Owner's call - forcing it
            # on our end does nothing when the sink ends up as GO instead,
            # which is the common case at the default go_intent=0. Warn
            # rather than silently doing nothing, but never raise go_intent
            # automatically: 0 is a deliberate fix for some sinks (#72),
            # and overriding it here would break those.
            print("[FluxCast WFD] Warning: --wfd-p2p-channel has no effect - "
                  f"we ended up as {role}, not the Group Owner, so the sink "
                  "picked the channel instead. Pair this with "
                  "--wfd-go-intent 15 if you need the channel forced.")

        if role == "P2P-GO":
            _wait_for_go_peer_associated(data_iface, peer_mac)
            mark_unmanaged(data_iface)
        else:
            wait_for_p2p_carrier(data_iface)
            # Do not nmcli-unmanage the client group iface: that locally
            # DEAUTH_LEAVING + nl80211 deinit right after GROUP-STARTED.

        try:
            configure_ip(data_iface, peer_mac, role, physical_iface)
        except Exception:
            # session.py's cleanup (release_wpa_supplicant_connection) only
            # runs once this function successfully returns a value, so a
            # failure here needs to clean up after itself - otherwise a
            # leftover dnsmasq instance can port-conflict with the next run.
            release_ip_config(data_iface)
            raise

        print(f"[FluxCast WFD] {data_iface} is up and IP-configured. "
              "Handing off to the RTSP server.")
        # Keep GO intent as used for this group until release_*; restoring
        # P2PDeviceConfig here races GROUP-STARTED and can DEAUTH the BSS.
        previous_intent = None
        return data_iface
    except Exception:
        raise
    finally:
        if previous_intent is not None:
            _set_p2p_go_intent(interface, previous_intent, restoring=True,
                               privileged=True)


def release_wpa_supplicant_connection(interface: Optional[str], data_iface: str) -> None:
    """Undo connect_via_wpa_supplicant(): release IP and GroupRemove.

    Do not remanage p2p-dev-* / p2p-* — NM owns STA only (99-fluxcast-p2p.conf).
    """
    release_ip_config(data_iface)

    paths = _p2p_device_iface_paths(interface, privileged=True)
    if paths:
        try:
            _gdbus_call([
                "--dest", WPA_DEST,
                "--object-path", paths[0],
                "--method", f"{WPA_P2P_IFACE}.GroupRemove",
                f"'{data_iface}'",
            ], timeout=10.0, privileged=True)
            print("[FluxCast WFD] wpa_supplicant P2P group removed.")
        except Exception as exc:
            print(f"[FluxCast WFD] Warning: GroupRemove failed: {exc}")
