import time
from typing import Optional

from ..config import WFDNotReady
from ..constants import NM_DEST, NM_PATH, WFD_RTSP_PORT
from ..ie import WFDPeer, _parse_gdbus_byte_array, _parse_wfd_ies_rtsp_port
from .dbus import (
    NM_ACTIVE_STATE_NAMES, NM_DEVICE_REASON_NAMES, NM_DEVICE_STATE_NAMES,
    _gdbus_call, _nm_get_property, _nm_get_string, _object_paths,
    _variant_byte_array, _variant_uint, _variant_uint_tuple, _wfd_source_ie,
)


def _nm_device_summary(path: str) -> str:
    iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface")
    ip_iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "IpInterface")
    state = _variant_uint(_nm_get_property(path, "org.freedesktop.NetworkManager.Device", "State"))
    _, reason = _variant_uint_tuple(
        _nm_get_property(path, "org.freedesktop.NetworkManager.Device", "StateReason")
    )
    state_text = NM_DEVICE_STATE_NAMES.get(state or -1, str(state))
    reason_text = NM_DEVICE_REASON_NAMES.get(reason or -1, str(reason))
    if ip_iface and ip_iface != iface:
        return f"{iface}/{ip_iface}:{state_text}:{reason_text}"
    return f"{iface}:{state_text}:{reason_text}"

def _nm_active_devices(active_path: str) -> list[str]:
    raw = _nm_get_property(
        active_path,
        "org.freedesktop.NetworkManager.Connection.Active",
        "Devices",
    )
    return _object_paths(raw)

def _wait_for_nm_activation(active_path: str, timeout: float = 35.0) -> None:
    print("[FluxCast WFD] Waiting for NetworkManager P2P activation...")
    deadline = time.monotonic() + timeout
    last_status = ""

    while time.monotonic() < deadline:
        state_raw = _nm_get_property(
            active_path,
            "org.freedesktop.NetworkManager.Connection.Active",
            "State",
        )
        state = _variant_uint(state_raw)
        state_text = NM_ACTIVE_STATE_NAMES.get(state or -1, str(state))
        devices = _nm_active_devices(active_path)
        device_status = ", ".join(_nm_device_summary(path) for path in devices) or "no-device"
        status = f"{state_text}; {device_status}"

        if status != last_status:
            print(f"[FluxCast WFD] NM active connection: {status}")
            last_status = status

        if state == 2:
            print("[FluxCast WFD] P2P link is activated; waiting for RTSP session...")
            return
        if state == 4:
            raise WFDNotReady(
                "NetworkManager deactivated the Wi-Fi Direct connection before RTSP. "
                f"Last status: {status}"
            )

        time.sleep(0.5)

    raise WFDNotReady(
        "Timed out waiting for NetworkManager Wi-Fi Direct activation. "
        f"Last status: {last_status or 'unknown'}"
    )

def _nm_p2p_device_path(interface: Optional[str] = None) -> Optional[str]:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.DBus.Properties.Get",
        "org.freedesktop.NetworkManager",
        "Devices",
    ])
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())

    requested = interface or ""
    for path in _object_paths(result.stdout):
        iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface")
        if not iface or "p2p" not in iface.lower():
            continue
        if requested and requested not in iface:
            continue
        return path
    return None

def _nm_start_find(path: str, timeout: int) -> None:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.NetworkManager.Device.WifiP2P.StartFind",
        f"{{'timeout': <int32 {timeout}>}}",
    ])
    if result.returncode != 0:
        raise WFDNotReady((result.stderr or result.stdout).strip())

def _nm_stop_find(path: str) -> None:
    try:
        _gdbus_call([
            "--dest", NM_DEST,
            "--object-path", path,
            "--method", "org.freedesktop.NetworkManager.Device.WifiP2P.StopFind",
        ], timeout=3.0)
    except Exception:
        pass

def _nm_scan(interface: Optional[str], timeout: int) -> list[WFDPeer]:
    path = _nm_p2p_device_path(interface)
    if not path:
        raise WFDNotReady("NetworkManager did not expose a Wi-Fi P2P device.")

    iface = _nm_get_string(path, "org.freedesktop.NetworkManager.Device", "Interface") or path
    print(f"[FluxCast WFD] Starting NetworkManager Wi-Fi Direct scan on {iface} for {timeout}s...")
    _nm_start_find(path, timeout)
    try:
        time.sleep(max(1, timeout))
        peers_raw = _nm_get_property(path, "org.freedesktop.NetworkManager.Device.WifiP2P", "Peers")
    finally:
        _nm_stop_find(path)

    peers = []
    for peer_path in _object_paths(peers_raw):
        name = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Name")
        address = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "HwAddress")
        model = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Model")
        manufacturer = _nm_get_string(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "Manufacturer")
        wfd_ies_raw = _nm_get_property(peer_path, "org.freedesktop.NetworkManager.WifiP2PPeer", "WfdIEs")
        
        # wfd_ies_raw is the raw gdbus stdout string (e.g. "<@ay [byte 0x00, ...]>").
        # Parse it into a byte list, then extract the RTSP port from subelement 0.
        wfd_ies_list = _parse_gdbus_byte_array(wfd_ies_raw)
        sink_rtsp_port = _parse_wfd_ies_rtsp_port(wfd_ies_list)

        details = "; ".join(
            part for part in [
                f"model={model}" if model else "",
                f"manufacturer={manufacturer}" if manufacturer else "",
                f"wfd_ies={wfd_ies_raw}" if wfd_ies_raw else "",
                f"sink_rtsp_port={sink_rtsp_port}",
            ]
            if part
        )
        peers.append(WFDPeer(
            address=address or peer_path.rsplit("/", 1)[-1],
            name=name,
            details=details,
            path=peer_path,
            source="NetworkManager",
            rtsp_port=sink_rtsp_port,
        ))
    return peers

def _connection_settings(peer: WFDPeer, rtsp_port: int) -> str:
    peer_address = peer.address
    return (
        "{"
        "'connection': {"
        "'id': <'FluxCast WFD'>, "
        "'type': <'wifi-p2p'>, "
        "'autoconnect': <false>"
        "}, "
        "'wifi-p2p': {"
        f"'peer': <'{peer_address}'>, "
        f"'wfd-ies': <{_variant_byte_array(_wfd_source_ie(rtsp_port))}>"
        "}, "
        "'ipv4': {'method': <'auto'>, 'never-default': <true>}, "
        "'ipv6': {'method': <'auto'>, 'never-default': <true>, 'may-fail': <true>}"
        "}"
    )

def _connect_peer(
    device_path: str,
    peer: WFDPeer,
    rtsp_port: int = WFD_RTSP_PORT,
    dry_run: bool = False,
) -> str:
    if not peer.path:
        raise WFDNotReady("NetworkManager peer object path is required for P2P connection.")

    settings = _connection_settings(peer, rtsp_port)
    # !!!Do not use bind-activation here!!!: the gdbus CLI process exits right after
    # the method call, and NetworkManager would tear the P2P link down with it.
    options = "{'persist': <'volatile'>}"
    args = [
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.NetworkManager.AddAndActivateConnection2",
        settings,
        device_path,
        peer.path,
        options,
    ]
    if dry_run:
        print("[FluxCast WFD] Dry-run AddAndActivateConnection2:")
        print("gdbus call --system " + " ".join(args))
        return "/"

    print(f"[FluxCast WFD] Connecting to {peer.name or peer.address} via NetworkManager...")
    result = _gdbus_call(args, timeout=30.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise WFDNotReady(text)

    paths = _object_paths(text)
    active = paths[-1] if paths else "/"
    print(f"[FluxCast WFD] NetworkManager activation started: {text}")
    return active

def _disconnect_device(device_path: str) -> None:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", device_path,
        "--method", "org.freedesktop.NetworkManager.Device.Disconnect",
    ], timeout=10.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        print("[FluxCast WFD] NetworkManager P2P device disconnected.")
    elif text and "Device.NotActive" not in text:
        print(f"[FluxCast WFD] NetworkManager disconnect warning: {text}")

def _deactivate_connection(active_path: str) -> None:
    if not active_path or active_path == "/":
        return
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", NM_PATH,
        "--method", "org.freedesktop.NetworkManager.DeactivateConnection",
        active_path,
    ], timeout=10.0)
    text = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        print("[FluxCast WFD] NetworkManager P2P connection deactivated.")
    elif text:
        print(f"[FluxCast WFD] NetworkManager deactivate warning: {text}")
