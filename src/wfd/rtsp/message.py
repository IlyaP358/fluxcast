import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RTSPMessage:
    start: str
    headers: dict[str, str]
    raw_headers: list[str]
    body: str = ""

    @property
    def is_response(self) -> bool:
        return self.start.startswith("RTSP/")

    @property
    def method(self) -> str:
        if self.is_response:
            return ""
        return self.start.split(maxsplit=1)[0] if self.start else ""

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "0")

    @property
    def status(self) -> str:
        if not self.is_response:
            return ""
        parts = self.start.split(maxsplit=2)
        return " ".join(parts[1:]) if len(parts) >= 2 else ""

def _read_rtsp_message(rfile) -> Optional[RTSPMessage]:
    lines = []
    while True:
        raw = rfile.readline(8192)
        if not raw:
            return None
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            break
        lines.append(line)

    if not lines:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    content_length = 0
    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError:
        content_length = 0

    body = ""
    if content_length > 0:
        body = rfile.read(content_length).decode("utf-8", errors="replace")

    return RTSPMessage(
        start=lines[0],
        headers=headers,
        raw_headers=lines[1:],
        body=body,
    )

def _parse_parameters(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            params[key.strip().lower()] = value.strip()
    return params

def _sink_advertises_uibc(params: dict[str, str]) -> bool:
    # Gate M4 uibc-enable on this. A strict sink can reject the whole
    # SET_PARAMETER (killing the session) if we enable UIBC it never advertised.
    val = (params.get("wfd_uibc_capability") or "").strip().lower()
    if not val or val == "none":
        return False
    return "generic" in val or "hidc" in val

def _parse_rtp_ports(value: str) -> Optional[tuple[int, int]]:
    match = re.search(
        r"RTP/AVP/(?:UDP|TCP);unicast\s+(\d+)\s+(\d+)\s+mode=play",
        value,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    return None

def _parse_transport_client_ports(value: str) -> Optional[tuple[int, int]]:
    match = re.search(r"client_port=(\d+)(?:-(\d+))?", value, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2) or "0")
    return None
