import re
import shutil
import time
from typing import Optional

from ..proc import _run


def _is_p2p_group_iface(iface: str) -> bool:
    return iface.startswith("p2p-") and not iface.startswith("p2p-dev-")


def _get_peer_ip_from_arp(peer_mac: str) -> Optional[str]:
    """Return the IP for peer_mac from the kernel ARP/neighbour table."""
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        mac = peer_mac.lower().replace("-", ":")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "dev":
                iface = parts[2]
                if (
                    mac in line.lower()
                    and _is_p2p_group_iface(iface)
                    and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0])
                ):
                    return parts[0]
    except Exception:
        pass
    return None


def _get_peer_ip_from_p2p_iface() -> Optional[str]:
    """Fallback: find TV IP from ARP on any active P2P group interface.

    Some TVs (LG webOS in issue #44) randomize their MAC between the P2P discovery phase and
    the actual group connection, so. the scanned MAC never matches the ARP entry.
    Scanning the p2p-* group interface directly avoids the MAC comparison entirely.
    """
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            # Format: IP dev IFACE lladdr MAC STATE
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "dev":
                iface = parts[2]
                if _is_p2p_group_iface(iface):
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                        return parts[0]
    except Exception:
        pass
    return None


def _wait_for_peer_ip(peer_mac: str, timeout: float = 12.0) -> Optional[str]:
    """Poll ARP until the peer's IP appears (DHCP may take a few seconds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ip = _get_peer_ip_from_arp(peer_mac) or _get_peer_ip_from_p2p_iface()
        if ip:
            return ip
        time.sleep(0.75)
    return None
