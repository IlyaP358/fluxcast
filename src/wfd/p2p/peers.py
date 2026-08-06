import re
import shutil
import subprocess
import time
from typing import Optional

from ..config import WFDNotReady
from ..constants import _DEVICE_NAME
from ..ie import WFDPeer, _wfd_ie_device_info, _wfd_ie_device_name
from ..proc import _run
from .nm import _nm_scan


def _select_peer(peers: list[WFDPeer], selector: Optional[str]) -> WFDPeer:
    if not peers:
        raise WFDNotReady("No Wi-Fi Direct peers found. Put the TV into Screen Share/Wireless Display mode.")
    if selector is None:
        print_scan(peers)
        try:
            raw = input("Select WFD peer [0]: ").strip()
        except EOFError:
            raw = ""
        selector = raw or "0"

    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(peers):
            return peers[index]
        raise WFDNotReady(f"Peer index out of range: {selector}")

    normalized = selector.lower()
    for peer in peers:
        if normalized in peer.address.lower() or normalized in peer.name.lower():
            return peer
    raise WFDNotReady(f"No peer matched selector: {selector}")

def _scan_and_select(interface: Optional[str], selector: Optional[str],
                     timeout: int, attempts: int = 3) -> WFDPeer:
    """Scans and resolves peer. If no selector, does one scan and opens prompt.
    With selector, retries non-deterministic scans
    until resolved or raises original error.
    """
    if selector is None:
        peers = active_scan(interface=interface, timeout=timeout)
        return _select_peer(peers, None)

    last_error: Optional[WFDNotReady] = None
    for attempt in range(1, attempts + 1):
        peers = active_scan(interface=interface, timeout=timeout)
        try:
            return _select_peer(peers, selector)
        except WFDNotReady as exc:
            last_error = exc
            if attempt < attempts:
                print(f"[FluxCast WFD] peer '{selector}' not in scan "
                      f"{attempt}/{attempts}; rescanning...")
    assert last_error is not None
    raise last_error

def _default_wifi_interface() -> Optional[str]:
    if not shutil.which("iw"):
        return None

    try:
        result = _run(["iw", "dev"], timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return None

    current_iface = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            current_iface = stripped.split(maxsplit=1)[1]
        elif stripped == "type managed" and current_iface:
            return current_iface
    return None

def _parse_peer_name(details: str) -> str:
    for line in details.splitlines():
        stripped = line.strip()
        if stripped.startswith("device_name="):
            return stripped.partition("=")[2]
    return ""

def active_scan(interface: Optional[str] = None, timeout: int = 8) -> list[WFDPeer]:
    """Run an active Wi-Fi Direct peer scan.
    """
    try:
        return _nm_scan(interface=interface, timeout=timeout)
    except WFDNotReady as nm_error:
        print(f"[FluxCast WFD] NetworkManager scan unavailable: {nm_error}")

    if not shutil.which("wpa_cli"):
        raise WFDNotReady("wpa_cli is required for active Wi-Fi Direct scans.")

    iface = interface or _default_wifi_interface()
    if not iface:
        raise WFDNotReady("Could not detect a managed Wi-Fi interface for wpa_cli.")

    print(f"[FluxCast WFD] Starting Wi-Fi Direct scan on {iface} for {timeout}s...")
    try:
        start = _run(["wpa_cli", "-i", iface, "p2p_find", str(timeout)], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WFDNotReady(f"Could not start p2p_find: {exc}") from exc
    if start.returncode != 0:
        error = (start.stderr or start.stdout).strip()
        if "Permission denied" in error:
            raise WFDNotReady(
                "wpa_cli cannot access the supplicant control interface. "
                "This usually needs root, a ctrl_interface group, or a "
                "NetworkManager D-Bus connection path. Raw error: " + error
            )
        raise WFDNotReady(error)

    time.sleep(max(1, timeout))

    try:
        peers_result = _run(["wpa_cli", "-i", iface, "p2p_peers"], timeout=5.0)
    finally:
        try:
            _run(["wpa_cli", "-i", iface, "p2p_stop_find"], timeout=3.0)
        except Exception:
            pass

    if peers_result.returncode != 0:
        raise WFDNotReady((peers_result.stderr or peers_result.stdout).strip())

    peers = []
    for raw in peers_result.stdout.splitlines():
        address = raw.strip()
        if not re.fullmatch(r"[0-9a-fA-F:]{17}", address):
            continue

        details = ""
        try:
            details_result = _run(["wpa_cli", "-i", iface, "p2p_peer", address], timeout=5.0)
            if details_result.returncode == 0:
                details = details_result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        peers.append(WFDPeer(
            address=address,
            name=_parse_peer_name(details),
            details=details,
            source="wpa_cli",
        ))

    return peers

def print_scan(peers: list[WFDPeer]) -> None:
    if not peers:
        print("[FluxCast WFD] No Wi-Fi Direct peers found.")
        return

    print("[FluxCast WFD] Wi-Fi Direct peer(s):")
    for idx, peer in enumerate(peers):
        name = f"  {peer.name}" if peer.name else ""
        source = f" via {peer.source}" if peer.source else ""
        print(f"  [{idx}] {peer.address}{name}{source}")
        if "wfd_subelems" in peer.details or "wfd_dev_info" in peer.details:
            print("      WFD capability data detected")
        elif "wfd_ies=" in peer.details:
            print("      WFD capability data detected")
