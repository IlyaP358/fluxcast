import re
from dataclasses import dataclass
from typing import Optional


# Bits 0-1 of the Device Information bitmap in WFD subelement 0: what the
# device says it is. Everything except Source can receive a stream.
WFD_DEVICE_TYPE_SOURCE = 0
WFD_DEVICE_TYPE_PRIMARY_SINK = 1
WFD_DEVICE_TYPE_SECONDARY_SINK = 2
WFD_DEVICE_TYPE_DUAL_ROLE = 3


def _wfd_ie_device_info(rtsp_port: int) -> bytes:
    """
    WFD Subelement ID 0: WFD Device Information (6 bytes)
    Byte 0-1: Device Information bitmask
              (0x0010 = Source, 0x0000 = Coupled Sink not supported)
    Byte 2-3: Session Management Control Port (RTSP port)
    Byte 4-5: Device Throughput (max 100 Mbps)
    """
    return bytes([
        0x00, 0x00, 0x06,
        0x00, 0x10,  # Info: WFD Source, Session Available (No HDCP/Coupled Sink)
        (rtsp_port >> 8) & 0xff, rtsp_port & 0xff,
        0x00, 0xc8   # Throughput: 100 Mbps
    ])

def _wfd_ie_device_name(name: str) -> bytes:
    """
    WFD Subelement ID 10: WFD Device Name
    """
    encoded = name.encode("utf-8")
    length = len(encoded)
    return bytes([0x0a, (length >> 8) & 0xff, length & 0xff]) + encoded

@dataclass
class WFDPeer:
    address: str
    name: str = ""
    details: str = ""
    path: str = ""
    source: str = ""
    rtsp_port: int = 7236
    # Whether the peer advertised itself as something we can stream to: True,
    # False, or None when the scan could not tell. Set by each scanner from
    # the data it parsed, rather than re-derived by searching `details` later
    # - that string is formatted for humans, and matching on it is what made
    # every P2P peer look like a sink (#124).
    wfd_capable: Optional[bool] = None
    # The peer's advertised WFD device type, or None when it advertised no
    # Wi-Fi Display data at all. This is what separates the two ways
    # wfd_capable can be False: a printer that never mentions Wi-Fi Display,
    # and another source - another laptop, or another FluxCast - that does.
    wfd_device_type: Optional[int] = None

def _parse_gdbus_byte_array(raw: str) -> list[int]:
    """Parse a gdbus @ay variant string into a list of integer byte values.

    NetworkManager returns WFD IEs via gdbus as a formatted string such as:
    ``<@ay [byte 0x00, byte 0x10, byte 0x1c, byte 0x00, byte 0x1c, ...]>`
    """
    return [int(h, 16) for h in re.findall(r"0x([0-9a-fA-F]+)", raw)]

def _parse_hex_bytes(value: str) -> list[int]:
    """Decode a wpa_cli hex field such as "00061c440000" into byte values."""
    value = value.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    if not value or len(value) % 2:
        return []
    try:
        return list(bytes.fromhex(value))
    except ValueError:
        return []

def _wfd_device_info_body(wfd_ies: list[int]) -> Optional[list[int]]:
    """Return the body of WFD Subelement ID 0 (Device Information), or None.

    The subelement is a 3-byte header (id, then a 16-bit length) followed by
    6 bytes: the Device Information bitmap, the RTSP port, and the device
    throughput. Everything that reads that subelement walks the stream
    through here so the readers cannot disagree about where it starts.
    """
    if not wfd_ies or len(wfd_ies) < 6:
        return None

    i = 0
    while i + 3 <= len(wfd_ies):
        sub_id = wfd_ies[i]
        sub_len = (wfd_ies[i+1] << 8) | wfd_ies[i+2]
        if sub_id == 0 and sub_len >= 6 and i + 3 + sub_len <= len(wfd_ies):
            return wfd_ies[i+3:i+3+sub_len]
        i += 3 + sub_len
    return None

def _parse_wfd_ies_rtsp_port(wfd_ies: list[int]) -> int:
    """Parse the WFD Information Element bytes to find the Sink's RTSP port.

    The port is the third and fourth byte of the Device Information body,
    after the 16-bit device-info bitmap.
    """
    body = _wfd_device_info_body(wfd_ies)
    if body is None:
        return 7236
    port = (body[2] << 8) | body[3]
    return port if port > 0 else 7236

def _parse_wfd_ies_device_type(wfd_ies: list[int]) -> Optional[int]:
    """Parse the peer's WFD device type: bits 0-1 of the Device Information
    bitmap, the two bytes immediately before the RTSP port. None when there
    is no readable Device Information subelement.
    """
    body = _wfd_device_info_body(wfd_ies)
    if body is None:
        return None
    return ((body[0] << 8) | body[1]) & 0b11

def _wfd_capability(wfd_ies: list[int]) -> tuple[Optional[bool], Optional[int]]:
    """Decide from a peer's WFD IE bytes whether we can stream to it.

    Advertising Wi-Fi Display is not the same as being a sink. Our own
    Device Information subelement (_wfd_ie_device_info) says Source, so a
    second machine running FluxCast advertises a full, valid set of WFD IEs
    and is still not something to connect to - which is what a plain
    presence check reported it as.

    Returns (can_receive_a_stream, device_type):

    - no WFD data at all -> (False, None), the printer case
    - WFD data we cannot read -> (None, None), unknown rather than a verdict
    - a readable device type -> (it is not a Source, that type)
    """
    if not wfd_ies:
        return False, None
    device_type = _parse_wfd_ies_device_type(wfd_ies)
    if device_type is None:
        return None, None
    return device_type != WFD_DEVICE_TYPE_SOURCE, device_type

def _wfd_capability_from_hex(value: str) -> tuple[Optional[bool], Optional[int]]:
    """_wfd_capability for wpa_cli's hex fields.

    Which blob a field holds depends on the field: `wfd_subelems=` carries
    whole subelements, 3-byte headers included, while `wfd_dev_info=` is
    reported as the 6-byte Device Information body on its own. Rather than
    hardcode which is which, read it as a subelement stream first and fall
    back to treating a bare 6-byte value as that body.
    """
    data = _parse_hex_bytes(value)
    if not data:
        return None, None
    capable, device_type = _wfd_capability(data)
    if device_type is None and len(data) == 6:
        return _wfd_capability([0x00, 0x00, 0x06] + data)
    return capable, device_type
