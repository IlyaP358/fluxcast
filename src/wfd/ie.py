import re
from dataclasses import dataclass


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

def _parse_gdbus_byte_array(raw: str) -> list[int]:
    """Parse a gdbus @ay variant string into a list of integer byte values.

    NetworkManager returns WFD IEs via gdbus as a formatted string such as:
    ``<@ay [byte 0x00, byte 0x10, byte 0x1c, byte 0x00, byte 0x1c, ...]>`
    """
    return [int(h, 16) for h in re.findall(r"0x([0-9a-fA-F]+)", raw)]

def _parse_wfd_ies_rtsp_port(wfd_ies: list[int]) -> int:
    """
    Parse the WFD Information Element bytes to find the Sink's RTSP port.
    WFD Subelement ID 0: WFD Device Information (length 6)
    Bytes 3-4 of the subelement (offset 3 and 4 after ID and Length) 
    contain the RTSP port.
    """
    if not wfd_ies or len(wfd_ies) < 6:
        return 7236
    
    i = 0
    while i + 3 <= len(wfd_ies):
        sub_id = wfd_ies[i]
        sub_len = (wfd_ies[i+1] << 8) | wfd_ies[i+2]
        if sub_id == 0 and sub_len >= 6 and i + 3 + sub_len <= len(wfd_ies):
            # Port is at index i + 3 + 2 and i + 3 + 3
            port = (wfd_ies[i+5] << 8) | wfd_ies[i+6]
            return port if port > 0 else 7236
        i += 3 + sub_len
    return 7236
