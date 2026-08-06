import re
import shutil
import subprocess
from typing import Optional

from ..config import WFDNotReady
from ..constants import NM_DEST, _DEVICE_NAME
from ..ie import _wfd_ie_device_info, _wfd_ie_device_name
from ..proc import _run


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

def _gdbus_call(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    if not shutil.which("gdbus"):
        raise WFDNotReady("gdbus is required for NetworkManager Wi-Fi P2P discovery.")
    return _run(["gdbus", "call", "--system", *args], timeout=timeout)

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

def _variant_byte_array(data: bytes) -> str:
    return "@ay [" + ", ".join(f"byte 0x{byte:02x}" for byte in data) + "]"


def _wfd_source_ie(rtsp_port: int) -> bytes:
    if rtsp_port <= 0 or rtsp_port > 65535:
        raise WFDNotReady(f"Invalid WFD RTSP port: {rtsp_port}")
    # Build WFD IE with Device Info and Device Name subelements.
    return _wfd_ie_device_info(rtsp_port) + _wfd_ie_device_name(_DEVICE_NAME)
