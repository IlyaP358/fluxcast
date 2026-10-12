"""Miracast over Infrastructure (MS-MICE) connection setup.

The source tells the sink over TCP 7250 that it is ready; the sink then
connects back to the source's RTSP port over the LAN and the usual WFD
RTSP/RTP session follows. No Wi-Fi Direct link is involved.
"""

import shutil
import socket
import struct
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .config import WFDNotReady
from .proc import _run

MICE_PORT = 7250
# --wfd-mice without a HOST: find the sink on the LAN instead.
MICE_AUTO = "auto"
_VERSION = 0x01
_SOURCE_READY = 0x01
_STOP_PROJECTION = 0x02
_TLV_FRIENDLY_NAME = 0x00
_TLV_RTSP_PORT = 0x02
_TLV_SOURCE_ID = 0x03


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack(">BH", kind, len(value)) + value


def _message(command: int, tlvs: bytes = b"") -> bytes:
    return struct.pack(">HBB", 4 + len(tlvs), _VERSION, command) + tlvs


def _name_tlv(name: str) -> bytes:
    return _tlv(_TLV_FRIENDLY_NAME, name[:128].encode("utf-16-le"))


def _source_id_tlv() -> bytes:
    # A stable source ID lets the sink recognise this machine across sessions.
    return _tlv(_TLV_SOURCE_ID, uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()).bytes)


def source_ready_message(name: str, rtsp_port: int) -> bytes:
    # LG webOS ignores SOURCE_READY unless the RTSP port precedes the source ID.
    return _message(
        _SOURCE_READY,
        _name_tlv(name)
        + _tlv(_TLV_RTSP_PORT, struct.pack(">H", rtsp_port))
        + _source_id_tlv(),
    )


def stop_projection_message(name: str) -> bytes:
    return _message(_STOP_PROJECTION, _name_tlv(name) + _source_id_tlv())


def sink_name(host: str) -> str:
    """Best-effort sink name: its mDNS hostname, falling back to the address."""
    if shutil.which("avahi-resolve-address"):
        try:
            result = _run(["avahi-resolve-address", host], timeout=3)
            fields = result.stdout.split()
            if result.returncode == 0 and len(fields) >= 2:
                return fields[1]
        except Exception:
            pass
    return host


def send_source_ready(host: str, rtsp_port: int, name: str) -> socket.socket:
    sock = socket.create_connection((host, MICE_PORT), timeout=5.0)
    sock.sendall(source_ready_message(name, rtsp_port))
    return sock


def send_stop_projection(sock: socket.socket, name: str) -> None:
    try:
        sock.sendall(stop_projection_message(name))
    finally:
        sock.close()


# MS-MICE sinks advertise _display._tcp, but LG webOS (checked on an OLED55C2)
# only does while its MICE listener runs, and that can stop until the TV's
# Wi-Fi reconnects; its Cast and AirPlay records stay. Those are candidates
# only; listening on TCP 7250 is what makes one a MICE sink.
# Ordered by whose instance name to show when a host advertises several.
_MDNS_TYPES = ("_display._tcp.local.", "_googlecast._tcp.local.", "_airplay._tcp.local.")


@dataclass
class MiceSink:
    address: str
    name: str
    ready: bool = False  # accepting connections on MICE_PORT right now


def _mdns_name(service_type: str, instance: str, properties: dict) -> str:
    if service_type.startswith("_googlecast."):
        friendly = (properties.get(b"fn") or b"").decode(errors="replace").strip()
        if friendly:
            return friendly
    return instance[: -len(service_type) - 1] if instance.endswith("." + service_type) else instance


def browse_candidates(browse_time: float = 3.0) -> Optional[list[MiceSink]]:
    """IPv4 hosts on the LAN that may be MICE sinks; None without python zeroconf."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return None

    seen: list[tuple[str, str]] = []

    class _Listener:
        def add_service(self, _zc, service_type, name):
            seen.append((service_type, name))

        def update_service(self, *_args):
            pass

        def remove_service(self, *_args):
            pass

    zc = Zeroconf()
    try:
        browsers = [ServiceBrowser(zc, t, _Listener()) for t in _MDNS_TYPES]
        time.sleep(browse_time)
        for browser in browsers:
            browser.cancel()
        by_address: dict[str, tuple[int, str]] = {}
        for service_type, instance in seen:
            info = zc.get_service_info(service_type, instance, timeout=1500)
            if info is None:
                continue
            rank = _MDNS_TYPES.index(service_type)
            name = _mdns_name(service_type, instance, info.properties or {})
            for address in info.parsed_addresses():
                if ":" in address:
                    continue
                if address not in by_address or rank < by_address[address][0]:
                    by_address[address] = (rank, name)
    finally:
        zc.close()
    return [MiceSink(address, name) for address, (_rank, name) in sorted(by_address.items())]


def _accepts_mice(address: str, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection((address, MICE_PORT), timeout=timeout).close()
        return True
    except OSError:
        return False


def probe_candidates(candidates: list[MiceSink]) -> list[MiceSink]:
    """Mark which candidates accept MICE connections; returns the ready ones."""
    if candidates:
        with ThreadPoolExecutor(max_workers=min(16, len(candidates))) as pool:
            for sink, ready in zip(candidates, pool.map(lambda s: _accepts_mice(s.address), candidates)):
                sink.ready = ready
    return [sink for sink in candidates if sink.ready]


def select_sink(sinks: list[MiceSink], selector: Optional[str]) -> MiceSink:
    """Pick by index, or by a substring of the address or name, like --wfd-peer."""
    if selector is None:
        if len(sinks) == 1:
            return sinks[0]
        for index, sink in enumerate(sinks):
            print(f"  [{index}] {sink.name} ({sink.address})")
        try:
            selector = input("Select MICE sink [0]: ").strip() or "0"
        except EOFError:
            selector = "0"
    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(sinks):
            return sinks[index]
        raise WFDNotReady(f"MICE sink index out of range: {selector}")
    wanted = selector.lower()
    for sink in sinks:
        if wanted in sink.address or wanted in sink.name.lower():
            return sink
    raise WFDNotReady(f"No MICE sink matched selector: {selector}")


def discover_sink(selector: Optional[str] = None, wait: float = 30.0,
                  poll_interval: float = 2.0) -> MiceSink:
    """Find a MICE sink on the LAN, waiting up to `wait` s for one to accept.

    A sink may only listen while its screen-share app is open, so candidates
    that refuse are re-probed until the deadline.
    """
    print("[FluxCast WFD] MICE: looking for sinks on the LAN (mDNS)...")
    candidates = browse_candidates()
    if candidates is None:
        raise WFDNotReady(
            "MICE discovery needs the python zeroconf package (it comes with "
            "pychromecast); install it or pass the sink with --wfd-mice HOST."
        )
    if not candidates:
        raise WFDNotReady(
            "MICE: no TVs or displays found on the LAN via mDNS. Check that the "
            "TV is on the same network, or pass it with --wfd-mice HOST."
        )
    deadline = time.monotonic() + wait
    announced = False
    while True:
        ready = probe_candidates(candidates)
        if ready:
            break
        if time.monotonic() >= deadline:
            names = ", ".join(f"{s.name} ({s.address})" for s in candidates)
            raise WFDNotReady(
                f"MICE: none of the devices found accepts Miracast over the LAN "
                f"(TCP {MICE_PORT}): {names}."
            )
        if not announced:
            print(f"[FluxCast WFD] MICE: found {len(candidates)} device(s), none "
                  f"accepting TCP {MICE_PORT} yet. If the TV used to accept, "
                  f"turn its Wi-Fi off and on; waiting up to {wait:.0f} s...")
            announced = True
        time.sleep(poll_interval)
    sink = select_sink(ready, selector)
    print(f"[FluxCast WFD] MICE: using {sink.name} ({sink.address})")
    return sink
