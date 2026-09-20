import re
import shutil
import time
from ipaddress import IPv4Address
from typing import Optional

from ..proc import _run


def _is_p2p_group_iface(iface: str) -> bool:
    # wpa_supplicant and IWD use different names for group data interfaces.
    # This is only a shape check: authentication still requires the exact
    # interface published by the backend for the selected connection.
    return (
        iface.startswith("p2p-") and not iface.startswith("p2p-dev-")
    ) or re.fullmatch(r"wlan[0-9]+-p2p-(?:go|cl)[0-9]+", iface) is not None


def _normalized_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "")
    return compact.lower() if re.fullmatch(r"[0-9a-fA-F]{12}", compact) else ""


def _valid_interface(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value) is not None


def _valid_ipv4(value: str) -> bool:
    try:
        return str(IPv4Address(value)) == value
    except ValueError:
        return False


def _neighbor_identity(
    line: str,
    *,
    scoped_interface: Optional[str] = None,
) -> tuple[str, str, str] | None:
    """Return (IPv4 address, interface, normalized MAC) for a complete row."""
    fields = line.split()
    if "lladdr" not in fields:
        return None
    if {"FAILED", "INCOMPLETE"}.intersection(field.upper() for field in fields):
        return None
    try:
        interface = (
            fields[fields.index("dev") + 1]
            if "dev" in fields
            else scoped_interface
        )
        mac = fields[fields.index("lladdr") + 1]
    except (IndexError, ValueError):
        return None
    if interface is None:
        return None
    normalized = _normalized_mac(mac)
    if not _valid_ipv4(fields[0]) or not _valid_interface(interface) or not normalized:
        return None
    return fields[0], interface, normalized


def _on_session_group_interface(candidate: str, interface: str) -> bool:
    if _is_p2p_group_iface(interface):
        return candidate == interface
    return _is_p2p_group_iface(candidate) and candidate.startswith(
        f"p2p-{interface}-"
    )


def _is_expected_peer_ip(
    peer_ip: str,
    peer_mac: str,
    interface: Optional[str],
) -> bool:
    """Return whether peer_ip belongs to the selected receiver's P2P group.

    Some receivers use a group-interface MAC that differs from the discovery
    MAC. An exact MAC match on the session's P2P group is accepted. Otherwise,
    a single unambiguous neighbour on that exact group is accepted. Ambiguity
    or an unknown session interface fails closed.
    """
    expected_mac = _normalized_mac(peer_mac)
    if not _valid_ipv4(peer_ip) or not expected_mac:
        return False
    if (
        interface is None
        or not _valid_interface(interface)
        or not _is_p2p_group_iface(interface)
    ):
        return False
    if not shutil.which("ip"):
        return False
    command = ["ip", "neigh", "show", "dev", interface]
    try:
        result = _run(command, timeout=3.0)
    except Exception:
        return False
    if result.returncode != 0:
        return False

    candidates = {
        identity
        for line in result.stdout.splitlines()
        if (
            identity := _neighbor_identity(
                line,
                scoped_interface=interface,
            )
        ) is not None
        and identity[1] == interface
    }
    if any(
        identity[0] == peer_ip and identity[2] == expected_mac
        for identity in candidates
    ):
        return True
    return (
        len(candidates) == 1
        and next(iter(candidates))[0] == peer_ip
    )


def _get_peer_ip_from_arp(
    peer_mac: str,
    interface: Optional[str] = None,
) -> Optional[str]:
    """Return the IP for peer_mac from the kernel ARP/neighbour table."""
    if not shutil.which("ip"):
        return None
    if interface is not None and not _valid_interface(interface):
        return None
    try:
        command = ["ip", "neigh", "show"]
        scoped_interface = None
        if interface is not None and _is_p2p_group_iface(interface):
            command.extend(["dev", interface])
            scoped_interface = interface
        result = _run(command, timeout=3.0)
        if result.returncode != 0:
            return None
        mac = _normalized_mac(peer_mac)
        if not mac:
            return None
        candidates: set[tuple[str, str, str]] = set()
        for line in result.stdout.splitlines():
            identity = _neighbor_identity(
                line,
                scoped_interface=scoped_interface,
            )
            if identity is None or not _is_p2p_group_iface(identity[1]):
                continue
            if interface is not None and not _on_session_group_interface(
                identity[1], interface
            ):
                continue
            candidates.add(identity)

        exact_ips = {item[0] for item in candidates if item[2] == mac}
        if len(exact_ips) == 1:
            return exact_ips.pop()
        if interface is not None and len(candidates) == 1:
            return next(iter(candidates))[0]
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


def _wait_for_peer_ip(
    peer_mac: str,
    timeout: float = 12.0,
    *,
    interface: Optional[str] = None,
    allow_interface_fallback: bool = True,
) -> Optional[str]:
    """Poll ARP until the peer's IP appears (DHCP may take a few seconds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ip = _get_peer_ip_from_arp(peer_mac, interface=interface)
        if not ip and allow_interface_fallback:
            ip = _get_peer_ip_from_p2p_iface()
        if ip:
            return ip
        time.sleep(0.75)
    return None
