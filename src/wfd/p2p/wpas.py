"""Direct wpa_supplicant P2P backend.

An alternative to nm.py: instead of asking NetworkManager to bring up the
P2P link, this module talks to wpa_supplicant's own D-Bus P2PDevice
interface directly (Find, Connect, GroupStarted). NetworkManager is never
asked to manage the connection, so it stays out of the way entirely.

The payoff is control that NetworkManager's own API doesn't expose: we can
set the WFD Device Info IE and the P2P operating channel ourselves, and
watch each step of the negotiation up close when something needs debugging.

See wpas_ip.py for everything that happens once a group exists - role
detection and IP configuration live there to keep this file focused.

Discovery, GO-intent handling, and device naming already have solid
implementations elsewhere in this codebase (peers.py, device.py); this
module's job is just to drive the connection itself, replacing
_connect_peer / _wait_for_nm_activation from nm.py.
"""

import time
from typing import Optional

from ..config import WFDNotReady
from ..constants import WFD_RTSP_PORT
from .dbus import _gdbus_call, _object_paths, _variant_byte_array, _variant_string, _wfd_source_ie
from .device import _p2p_device_iface_paths, _set_p2p_go_intent, _set_p2p_oper_channel
from .peers import _default_wifi_interface
from .wpas_ip import configure_ip, get_p2p_role, release_ip_config

WPA_DEST = "fi.w1.wpa_supplicant1"
WPA_IFACE = "fi.w1.wpa_supplicant1.Interface"
WPA_P2P_IFACE = "fi.w1.wpa_supplicant1.Interface.P2PDevice"


def _wpas_get_property(path: str, interface: str, prop: str) -> str:
    """Properties.Get scoped to wpa_supplicant's own D-Bus service.

    dbus.py's _nm_get_property is hardcoded to NetworkManager's D-Bus
    destination, which makes sense for nm.py but not here: every object
    path this module works with lives under wpa_supplicant's own service
    (fi.w1.wpa_supplicant1), a completely different one. This small wrapper
    just makes sure Peers/Ifname lookups actually ask the right service.

    privileged=True because wpa_supplicant's D-Bus policy grants
    Properties.Get only to wheel/sudo, not netdev - without the sudo
    fallback, a netdev-only caller (no wheel/sudo) would see every one of
    these silently return "", making peer discovery look like it just
    never finds anything instead of failing loudly.
    """
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.DBus.Properties.Get",
        interface,
        prop,
    ], privileged=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def _wpas_find_peer_path(iface_path: str, mac: str) -> Optional[str]:
    """Resolve a MAC address to its wpa_supplicant P2P peer object path.

    wpa_supplicant names peer objects after their MAC address directly, e.g.
    .../Peers/46d244e4372f for 46:D2:44:E4:37:2F (lowercase, colons
    stripped), so we can match on the object path itself rather than doing
    an extra Properties.Get round-trip per peer to read DeviceAddress.
    """
    peers_raw = _wpas_get_property(iface_path, WPA_P2P_IFACE, "Peers")
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

    privileged=True: wpa_supplicant's D-Bus policy grants Properties.Set
    only to wheel/sudo, not netdev - and unlike the read-only lookups
    elsewhere in this module, this one raises on failure, so a netdev-only
    caller without the sudo fallback would hit a hard WFDNotReady on every
    single connection attempt.
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


def _wpas_connect(iface_path: str, peer_path: str, go_intent: int = 0,
                   wps_method: str = "pbc") -> None:
    args = (
        "{"
        f"'peer': <objectpath '{peer_path}'>, "
        f"'wps_method': <'{wps_method}'>, "
        f"'go_intent': <int32 {go_intent}>, "
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


def _list_wpas_interfaces() -> set[str]:
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", "/fi/w1/wpa_supplicant1",
        "--method", "org.freedesktop.DBus.Properties.Get",
        WPA_DEST, "Interfaces",
    ], privileged=True)  # see _wpas_get_property's docstring
    if result.returncode != 0:
        return set()
    return set(_object_paths(result.stdout))


def _wait_for_group_interface(before: set[str], timeout: float = 40.0) -> str:
    """Detect the new wpa_supplicant Interface object GO Negotiation creates,
    then read its Ifname to get the real OS network interface name.

    wpa_supplicant also fires a GroupStarted signal carrying the interface
    object directly, which would be a more direct way to get this - but
    this module drives D-Bus via synchronous `gdbus call` subprocesses
    throughout (see dbus.py), and parsing `gdbus monitor` output reliably
    for a single signal is more fragile than polling the same
    Properties.Get calls used everywhere else here. Worth revisiting with a
    proper dbus_next signal subscription (already a project dependency) if
    this polling ever proves too slow in practice.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new_paths = _list_wpas_interfaces() - before
        for path in new_paths:
            ifname_raw = _wpas_get_property(path, WPA_IFACE, "Ifname")
            ifname = _variant_string(ifname_raw)
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
    paths = _p2p_device_iface_paths(interface)
    if not paths:
        raise WFDNotReady("wpa_supplicant P2P interface not found.")
    iface_path = paths[0]
    physical_iface = interface or _default_wifi_interface()

    # Must happen before Find/Connect - see _set_wfd_ies's docstring.
    _set_wfd_ies(rtsp_port)

    if p2p_channel is not None:
        _set_p2p_oper_channel(interface, p2p_channel)

    # GO intent only matters for GO Negotiation (Connect()), so it's set
    # right before that call rather than up here alongside discovery.
    # Setting it earlier raced wpa_supplicant's P2P state machine: Find()
    # would report success, but Peers stayed empty.
    previous_intent = None
    try:
        peer_path = _wait_for_peer(iface_path, peer_mac)

        previous_intent = _set_p2p_go_intent(interface, go_intent)
        interfaces_before = _list_wpas_interfaces()
        print(f"[FluxCast WFD] Connecting to {peer_mac} directly via wpa_supplicant "
              "(NetworkManager not involved in this step)...")
        _wpas_connect(iface_path, peer_path, go_intent=go_intent)

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
        return data_iface
    finally:
        if previous_intent is not None:
            _set_p2p_go_intent(interface, previous_intent, restoring=True)


def release_wpa_supplicant_connection(interface: Optional[str], data_iface: str) -> None:
    """Undo connect_via_wpa_supplicant(): release the IP configuration, tear
    down the P2P group, and hand the data interface back to NetworkManager.

    Mirrors nm.py's _disconnect_device, but for the raw-supplicant path -
    there is no NetworkManager active connection to deactivate here since
    NM was never involved in bringing this link up.
    """
    release_ip_config(data_iface)

    paths = _p2p_device_iface_paths(interface)
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
