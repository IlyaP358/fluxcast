import re
from typing import Optional

from ..constants import _DEVICE_NAME
from .dbus import _gdbus_call, _nm_get_string, _object_paths
from .peers import _default_wifi_interface


def _p2p_device_iface_paths(iface: Optional[str]) -> list[str]:
    """Return wpa_supplicant interface object paths, best P2P candidate first.

    The p2p-dev-<iface> control interface is preferred, then the physical
    interface, then anything else. Returns [] if wpa_supplicant can't be
    queried, so callers degrade to a warning instead of raising.

    privileged=True: wpa_supplicant's D-Bus policy grants Properties.Get
    only to wheel/sudo, not netdev - without the sudo fallback, every
    caller of this function (which is most of the wpas backend) would see
    an empty list and a misleading "P2P interface not found" instead of
    the real cause.
    """
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_root = "/fi/w1/wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    try:
        list_result = _gdbus_call([
            "--dest", wpa_dest,
            "--object-path", wpa_root,
            "--method", "org.freedesktop.DBus.Properties.Get",
            wpa_dest, "Interfaces",
        ], timeout=3.0, privileged=True)
    except Exception:
        return []

    if list_result.returncode != 0:
        return []

    iface_paths = _object_paths(list_result.stdout)
    if not iface_paths:
        return []

    physical = iface or _default_wifi_interface()
    p2p_dev = f"p2p-dev-{physical}" if physical and not physical.startswith("p2p-dev-") else physical

    def _priority(path: str) -> int:
        ifname = _nm_get_string(path, wpa_iface, "Ifname")
        if ifname == p2p_dev:
            return 0
        if ifname == physical:
            return 1
        return 2

    return sorted(iface_paths, key=_priority)

def _set_p2p_device_name(iface: Optional[str], name: str = _DEVICE_NAME) -> None:
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    paths = _p2p_device_iface_paths(iface)
    if not paths:
        print("[FluxCast WFD] Warning: could not set P2P device name (cosmetic, connection will proceed).")
        return

    for iface_path in paths:
        try:
            result = _gdbus_call([
                "--dest", wpa_dest,
                "--object-path", iface_path,
                "--method", "org.freedesktop.DBus.Properties.Set",
                f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
                f"<{{'DeviceName': <'{name}'>}}>",
            ], timeout=3.0, privileged=True)
            if result.returncode == 0:
                print(f"[FluxCast WFD] P2P device name set to '{name}'.")
                return
        except Exception:
            pass

    print("[FluxCast WFD] Warning: could not set P2P device name (cosmetic, connection will proceed).")

def _read_p2p_go_intent(iface_path: str) -> Optional[int]:
    """Read the current P2P GO intent from a wpa_supplicant interface, or None.

    privileged=True for the same reason as _p2p_device_iface_paths - and
    the failure mode here is quieter than a missing interface: a None
    return makes _set_p2p_go_intent's caller think there was nothing to
    restore, so a netdev-only caller without the sudo fallback would leave
    the GO intent changed for good instead of restoring it on cleanup.
    """
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"
    try:
        result = _gdbus_call([
            "--dest", wpa_dest,
            "--object-path", iface_path,
            "--method", "org.freedesktop.DBus.Properties.Get",
            f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
        ], timeout=3.0, privileged=True)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"'GOIntent':\s*<uint32\s+(\d+)>", result.stdout)
    return int(match.group(1)) if match else None

def _set_p2p_go_intent(iface: Optional[str], value: int,
                       restoring: bool = False) -> Optional[int]:
    #Set the wpa_supplicant P2P group-owner intent (0-15)
    
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    paths = _p2p_device_iface_paths(iface)
    if not paths:
        if not restoring:
            print("[FluxCast WFD] Warning: could not set P2P GO intent (connection will proceed with the default).")
        return None

    for iface_path in paths:
        previous = _read_p2p_go_intent(iface_path)
        try:
            result = _gdbus_call([
                "--dest", wpa_dest,
                "--object-path", iface_path,
                "--method", "org.freedesktop.DBus.Properties.Set",
                f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
                f"<{{'GOIntent': <uint32 {value}>}}>",
            ], timeout=3.0, privileged=True)
            if result.returncode == 0:
                if restoring:
                    print(f"[FluxCast WFD] Restored P2P GO intent to {value}.")
                else:
                    print(f"[FluxCast WFD] P2P GO intent set to {value} "
                          f"(lower intent lets the TV be the group owner).")
                return previous
        except Exception:
            pass

    if not restoring:
        print("[FluxCast WFD] Warning: could not set P2P GO intent (connection will proceed with the default).")
    return None

def _set_p2p_oper_channel(iface: Optional[str], channel: int, reg_class: int = 81) -> bool:
    """Force the operating channel wpa_supplicant picks when we end up as GO.

    Some WFD sinks only support Wi-Fi Direct on 2.4GHz. Left to its own
    devices, a driver may still form the group on 5GHz, and a sink like
    that will simply never associate - GO Negotiation completes normally,
    but the sink never shows up at the 802.11 level at all. Forcing a
    2.4GHz channel here works around that.

    reg_class 81 is the standard worldwide "2.4GHz, 20MHz spacing, channels
    1-13" global operating class (WFA/IEEE 802.11 Annex E) - the same value
    Android's own Wi-Fi P2P framework uses. This reuses the same
    P2PDeviceConfig struct as GOIntent above; wpa_supplicant merges
    whichever keys are present rather than requiring the whole struct on
    every call.
    """
    wpa_dest = "fi.w1.wpa_supplicant1"
    wpa_iface = "fi.w1.wpa_supplicant1.Interface"

    paths = _p2p_device_iface_paths(iface)
    if not paths:
        print("[FluxCast WFD] Warning: could not set P2P operating channel "
              "(connection will proceed on whatever channel the driver picks).")
        return False

    for iface_path in paths:
        try:
            result = _gdbus_call([
                "--dest", wpa_dest,
                "--object-path", iface_path,
                "--method", "org.freedesktop.DBus.Properties.Set",
                f"{wpa_iface}.P2PDevice", "P2PDeviceConfig",
                f"<{{'OperRegClass': <uint32 {reg_class}>, "
                f"'OperChannel': <uint32 {channel}>}}>",
            ], timeout=3.0, privileged=True)
            if result.returncode == 0:
                print(f"[FluxCast WFD] P2P operating channel forced to channel "
                      f"{channel} (2.4GHz, reg class {reg_class}).")
                return True
        except Exception:
            pass

    print("[FluxCast WFD] Warning: could not set P2P operating channel "
          "(connection will proceed on whatever channel the driver picks).")
    return False
