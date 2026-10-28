"""Validate names before privileged argv / state-file paths.

Iface, MAC, and IPv4 strings reach sudo (sysctl, iptables, arping, ip).
``all`` / ``default`` as an iface would change global sysctl. A MAC used
as a filename must not contain ``..``.
"""

from __future__ import annotations

import re
from pathlib import Path

_IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,14}$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_IPV4_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3}$"
)
_RESERVED_IFACE = frozenset({"all", "default", "lo"})


def safe_iface(name: str) -> str:
    n = (name or "").strip()
    if not _IFACE_RE.fullmatch(n):
        return ""
    if n.lower() in _RESERVED_IFACE or "/" in n or ".." in n:
        return ""
    return n


def safe_mac(mac: str) -> str:
    m = (mac or "").strip().replace("-", ":")
    if not _MAC_RE.fullmatch(m):
        return ""
    return m.lower()


def mac_file_key(mac: str) -> str:
    m = safe_mac(mac)
    return m.replace(":", "-") if m else ""


def safe_ipv4(ip: str) -> str:
    s = (ip or "").strip()
    if not _IPV4_RE.fullmatch(s):
        return ""
    return s


def safe_sysfs_write_path(path: str) -> str:
    """Allow only kernel sysfs files under module/usb/devices."""
    raw = (path or "").strip()
    if not raw.startswith("/sys/") or ".." in raw:
        return ""
    allowed = ("/sys/module/", "/sys/bus/usb/", "/sys/devices/")
    if not raw.startswith(allowed):
        return ""
    try:
        text = str(Path(raw).resolve())
    except OSError:
        return ""
    if not text.startswith(allowed):
        return ""
    return text
