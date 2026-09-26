import re
import shutil
import subprocess
import time
from typing import Optional

from ..config import WFDNotReady
from ..constants import NM_DEST, _DEVICE_NAME
from ..ie import _wfd_ie_device_info, _wfd_ie_device_name
from ..proc import _run

WPA_DEST = "fi.w1.wpa_supplicant1"


def _object_paths(text: str) -> list[str]:
    return re.findall(r"'(/[^']+)'", text)

def _variant_string(text: str) -> str:
    match = re.search(r"<\'(.*)\' >", text)
    if match:
        return match.group(1)
    match = re.search(r"<\'(.*)\'", text)
    if match:
        return match.group(1)
    match = re.search(r"<\"(.*)\"", text)
    if match:
        return match.group(1)
    return ""

def _variant_uint(text: str) -> Optional[int]:
    matches = re.findall(r"(?:uint32\s+)?(\d+)", text)
    if not matches:
        return None
    return int(matches[-1])

def _variant_uint_tuple(text: str) -> tuple[Optional[int], Optional[int]]:
    matches = re.findall(r"(?:uint32\s+)?(\d+)", text)
    if len(matches) < 2:
        return None, None
    return int(matches[-2]), int(matches[-1])

def _variant_number(text: str) -> Optional[int]:
    """Scalar variant, hex or decimal. _variant_uint reads <byte 0x8b> as 8."""
    hexadecimal = re.search(r"0x([0-9a-fA-F]+)", text)
    if hexadecimal:
        return int(hexadecimal.group(1), 16)
    decimal = re.search(r"(?:byte|u?int(?:16|32|64))\s+(\d+)", text)
    return int(decimal.group(1)) if decimal else None

NM_DEVICE_TYPE_WIFI_P2P = 30

WPA_P2P_IFACE = "fi.w1.wpa_supplicant1.Interface.P2PDevice"
WPA_PEER_IFACE = "fi.w1.wpa_supplicant1.Peer"

P2P_GROUP_OWNER = 0x01    # group capability bit 0: peer already owns a group
WPS_PUSH_BUTTON = 0x0080  # the only WPS method either backend uses

# Whole-function budget for the wpa_supplicant cross-reference below. A hard
# hung supplicant already aborts on the first read, because _gdbus_call raises
# on timeout - but one that is slow and still answering never raises, and
# nothing else caps the per-peer reads. The scan this decorates is 8s by
# default and the activation timeout it exists to pre-empt is 35s.
_PEER_CAPABILITY_BUDGET = 5.0


NM_ACTIVE_STATE_NAMES = {
    0: "unknown",
    1: "activating",
    2: "activated",
    3: "deactivating",
    4: "deactivated",
}

NM_DEVICE_STATE_NAMES = {
    0: "unknown",
    10: "unmanaged",
    20: "unavailable",
    30: "disconnected",
    40: "prepare",
    50: "config",
    60: "need-auth",
    70: "ip-config",
    80: "ip-check",
    90: "secondaries",
    100: "activated",
    110: "deactivating",
    120: "failed",
}

NM_DEVICE_REASON_NAMES = {
    0: "none",
    1: "unknown",
    2: "now-managed",
    3: "now-unmanaged",
    4: "config-failed",
    5: "ip-config-unavailable",
    6: "ip-config-expired",
    7: "no-secrets",
    8: "supplicant-disconnect",
    9: "supplicant-config-failed",
    10: "supplicant-failed",
    11: "supplicant-timeout",
    15: "dhcp-start-failed",
    16: "dhcp-error",
    17: "dhcp-failed",
    18: "shared-start-failed",
    19: "shared-failed",
    38: "external-disconnect",
    39: "assume-failed",
    40: "supplicant-available",
    41: "modem-not-found",
    42: "bt-failed",
    53: "peer-not-found",
    54: "device-handler-failed",
}

def _gdbus_call(args: list[str], timeout: float = 5.0,
                 privileged: bool = False) -> subprocess.CompletedProcess[str]:
    """privileged=True marks a call that needs elevated D-Bus access: the
    P2PDevice actions (Find/Connect/StopFind/GroupRemove) and the
    Properties.Get/Set calls the wpas backend makes. Our D-Bus policy
    (meta/zz-dev.fluxcast.wpa-supplicant.conf) grants Properties.Get/Set to
    wheel/sudo; everything else falls back to sudo below.

    wpa_supplicant has no polkit integration, so unlike NetworkManager it
    can't prompt for authorization at call time - the policy grant is
    static, decided by the caller's uid/gid before the call is ever made.
    Where it applies, the right caller needs no sudo at all here; everyone
    else escalates, and only after actually seeing the bus reject the call.

    Retrying under sudo after an AccessDenied is safe (not a double-fire of
    a stateful action): dbus-daemon enforces this policy at the message-
    routing layer, before the call ever reaches wpa_supplicant, so a denied
    first attempt has no observable side effect to duplicate.
    """
    if not shutil.which("gdbus"):
        raise WFDNotReady("gdbus is required for NetworkManager Wi-Fi P2P discovery.")
    cmd = ["gdbus", "call", "--system", *args]
    method = args[args.index("--method") + 1] if "--method" in args else "gdbus call"
    try:
        if not privileged:
            return _run(cmd, timeout=timeout)

        result = _run(cmd, timeout=timeout)
        if result.returncode == 0 or "AccessDenied" not in (result.stderr or result.stdout or ""):
            return result
        # No -n: sudo's password prompt goes straight to the controlling
        # terminal (/dev/tty) regardless of stdout/stderr capture here, so
        # this still works interactively. Falls through instantly if the
        # session's sudo timestamp is already cached from an earlier command.
        return _run(["sudo", *cmd], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WFDNotReady(f"{method} timed out after {timeout:.0f}s") from exc

def _nm_get_property(path: str, interface: str, prop: str) -> str:
    result = _gdbus_call([
        "--dest", NM_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.DBus.Properties.Get",
        interface,
        prop,
    ])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()

def _nm_get_string(path: str, interface: str, prop: str) -> str:
    return _variant_string(_nm_get_property(path, interface, prop))

def _wpas_get_property(path: str, interface: str, prop: str,
                       privileged: bool = False) -> str:
    """Properties.Get scoped to wpa_supplicant's own service.

    _nm_get_property is hardcoded to NetworkManager's destination, so it
    can't read a /fi/w1/wpa_supplicant1/... path - the call lands on the
    wrong service and comes back as an unknown object.

    privileged defaults off so the NetworkManager path never escalates;
    only the wpas backend passes True.
    """
    result = _gdbus_call([
        "--dest", WPA_DEST,
        "--object-path", path,
        "--method", "org.freedesktop.DBus.Properties.Get",
        interface,
        prop,
    ], privileged=privileged)
    if result.returncode != 0:
        return ""
    return result.stdout

def _wpas_get_string(path: str, interface: str, prop: str,
                     privileged: bool = False) -> str:
    return _variant_string(_wpas_get_property(path, interface, prop, privileged=privileged))

def _wpas_running() -> bool:
    # wpa_supplicant is D-Bus activatable, so asking it anything would start
    # it. Not something a scan should do on a host running iwd instead.
    try:
        owned = _run([
            "gdbus", "call", "--system", "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus",
            "--method", "org.freedesktop.DBus.NameHasOwner", WPA_DEST,
        ], timeout=3.0)
    except Exception:
        return False
    return owned.returncode == 0 and "true" in owned.stdout

def _wpas_peer_capabilities() -> dict[str, tuple[Optional[bool], Optional[bool]]]:
    """MAC (lowercase, no separators) -> (is_group_owner, offers_push_button).

    NetworkManager's peer objects carry neither, so the default backend cannot
    see that a peer is unreachable until the connection has timed out (#137).
    Never escalates: unreadable means unknown, not a password prompt (#104).

    Returns whatever it finished within _PEER_CAPABILITY_BUDGET. A partial
    answer is the right one: a peer it did not reach is simply absent, and an
    absent peer reads back as unknown rather than as reachable.
    """
    if not _wpas_running():
        return {}
    deadline = time.monotonic() + _PEER_CAPABILITY_BUDGET
    capabilities: dict[str, tuple[Optional[bool], Optional[bool]]] = {}
    try:
        listed = _gdbus_call([
            "--dest", WPA_DEST,
            "--object-path", "/fi/w1/wpa_supplicant1",
            "--method", "org.freedesktop.DBus.Properties.Get",
            WPA_DEST, "Interfaces",
        ], timeout=3.0)
        if listed.returncode != 0:
            return {}
        for iface_path in _object_paths(listed.stdout):
            if time.monotonic() >= deadline:
                break
            peers_raw = _wpas_get_property(iface_path, WPA_P2P_IFACE, "Peers")
            for peer_path in _object_paths(peers_raw):
                if time.monotonic() >= deadline:
                    break
                group = _variant_number(
                    _wpas_get_property(peer_path, WPA_PEER_IFACE, "groupcapability")
                )
                # Singular. wpa_supplicant's D-Bus reference names this peer
                # property config_method, type q; the plural does not exist,
                # and asking for it cost a second round trip per peer.
                methods = _variant_number(
                    _wpas_get_property(peer_path, WPA_PEER_IFACE, "config_method")
                )
                capabilities[peer_path.rsplit("/", 1)[-1].lower()] = (
                    None if group is None else bool(group & P2P_GROUP_OWNER),
                    None if methods is None else bool(methods & WPS_PUSH_BUTTON),
                )
    except Exception:
        # A timeout here raises WFDNotReady, which _nm_scan's caller reads as
        # "NetworkManager is unusable" and falls back to wpa_cli. Keep whatever
        # was already read rather than discarding it with the exception.
        return capabilities
    return capabilities

def _variant_byte_array(data: bytes) -> str:
    return "@ay [" + ", ".join(f"byte 0x{byte:02x}" for byte in data) + "]"


def _wfd_source_ie(rtsp_port: int) -> bytes:
    if rtsp_port <= 0 or rtsp_port > 65535:
        raise WFDNotReady(f"Invalid WFD RTSP port: {rtsp_port}")
    # Build WFD IE with Device Info and Device Name subelements.
    return _wfd_ie_device_info(rtsp_port) + _wfd_ie_device_name(_DEVICE_NAME)
