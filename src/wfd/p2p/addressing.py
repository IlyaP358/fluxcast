import re
import shutil
import socket
import time
from typing import Optional, Set

from ..proc import _run


def _is_p2p_group_iface(iface: str) -> bool:
    return iface.startswith("p2p-") and not iface.startswith("p2p-dev-")


def _local_ipv4s() -> Set[str]:
    """IPv4 addresses assigned to this host (skip when scanning neigh for the TV)."""
    found: Set[str] = set()
    if not shutil.which("ip"):
        return found
    try:
        result = _run(["ip", "-4", "-o", "addr", "show"], timeout=3.0)
        if result.returncode != 0:
            return found
        for line in result.stdout.splitlines():
            parts = line.split()
            # N: IFACE    inet ADDR/NN ...
            if len(parts) >= 4 and parts[2] == "inet":
                addr = parts[3].split("/", 1)[0]
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", addr):
                    found.add(addr)
    except Exception:
        pass
    return found


def _macs_related(a: str, b: str) -> bool:
    """True if MACs match or look like P2P device vs interface address twins.

    LG (and others) often use a discovery P2P device address and a different
    group-interface address that share the lower five octets (or at least the
    NIC-specific last three) while the first octet's admin bits differ.
    """
    def norm(m: str) -> list[int]:
        return [int(x, 16) for x in m.lower().replace("-", ":").split(":")]

    try:
        aa, bb = norm(a), norm(b)
    except Exception:
        return False
    if aa == bb:
        return True
    if len(aa) != 6 or len(bb) != 6:
        return False
    # Same NIC-specific bytes; first octet may differ in locally-admin bits.
    if aa[1:] == bb[1:]:
        return True
    # Same last 3 bytes + same OUI middle (AB:14 style) — rare twin form.
    if aa[3:] == bb[3:] and aa[1:3] == bb[1:3]:
        return True
    return False


def _get_peer_ip_from_arp(peer_mac: str) -> Optional[str]:
    """Return the IP for peer_mac from the kernel ARP/neighbour table."""
    if not shutil.which("ip"):
        return None
    local = _local_ipv4s()
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        mac = peer_mac.lower().replace("-", ":")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1] == "dev":
                iface = parts[2]
                ip = parts[0]
                # ... lladdr XX:XX:...
                ll = ""
                if "lladdr" in parts:
                    ll = parts[parts.index("lladdr") + 1].lower()
                if not _is_p2p_group_iface(iface):
                    continue
                if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) or ip in local:
                    continue
                if mac in line.lower() or (ll and _macs_related(mac, ll)):
                    return ip
    except Exception:
        pass
    return None


def _get_peer_ip_from_p2p_iface() -> Optional[str]:
    """Fallback: find TV IP from ARP on any active P2P group interface.

    Some TVs (LG webOS in issue #44) randomize their MAC between the P2P discovery phase and
    the actual group connection, so the scanned MAC never matches the ARP entry.
    Scanning the p2p-* group interface directly avoids the MAC comparison entirely.
    """
    local = _local_ipv4s()
    try:
        result = _run(["ip", "neigh", "show"], timeout=3.0)
        if result.returncode != 0:
            return None
        candidates: list[str] = []
        for line in result.stdout.splitlines():
            # Format: IP dev IFACE lladdr MAC STATE
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "dev":
                iface = parts[2]
                ip = parts[0]
                if _is_p2p_group_iface(iface) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                    if ip not in local:
                        candidates.append(ip)
        # Prefer classic WFD GO address when we are .10
        for ip in candidates:
            if ip.endswith(".1"):
                return ip
        return candidates[0] if candidates else None
    except Exception:
        pass
    # Last resort: if we hold 192.168.49.10, try .1 even before ARP fills.
    for addr in local:
        if addr.startswith("192.168.49.") and addr != "192.168.49.1":
            return "192.168.49.1"
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
