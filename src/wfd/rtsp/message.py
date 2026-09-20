import re
import select
import time
from dataclasses import dataclass
from typing import Optional

from ..config import WFDNotReady


RTSP_MAX_LINE = 8192
RTSP_MAX_HEADERS = 64
RTSP_MAX_HEADER_BYTES = 32768
RTSP_MAX_BODY = 65536
RTSP_MESSAGE_TIMEOUT = 30.0
RTSP_IDLE_TIMEOUT = 30.0
RTSP_WRITE_TIMEOUT = 8.0
RTSP_MAX_PENDING = 16


class _RTSPReader:
    """Socket reader with one deadline for the whole message, not each recv."""
    def __init__(self, sock, idle_timeout=RTSP_IDLE_TIMEOUT):
        self.sock = sock
        self.idle_timeout = idle_timeout
        self.buffer = bytearray()
        self.deadline = None

    def begin_message(self):
        self.deadline = time.monotonic() + RTSP_MESSAGE_TIMEOUT if self.buffer else None

    def _receive(self):
        timeout = self.idle_timeout
        if self.deadline is not None:
            timeout = self.deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("RTSP message deadline exceeded")
        if not select.select([self.sock], [], [], timeout)[0]:
            raise TimeoutError("RTSP read timed out")
        chunk = self.sock.recv(RTSP_MAX_LINE)
        if chunk and self.deadline is None:
            self.deadline = time.monotonic() + RTSP_MESSAGE_TIMEOUT
        self.buffer.extend(chunk)
        return bool(chunk)

    def readline(self, limit):
        result = bytearray()
        while len(result) < limit:
            if not self.buffer and not self._receive():
                break
            end = self.buffer.find(b"\n")
            count = min(end + 1 if end >= 0 else len(self.buffer), limit - len(result))
            result.extend(self.buffer[:count])
            del self.buffer[:count]
            if result.endswith(b"\n"):
                break
        return bytes(result)

    def read(self, size):
        if not self.buffer and not self._receive():
            return b""
        chunk = bytes(self.buffer[:size])
        del self.buffer[:len(chunk)]
        return chunk


WFD_NEGOTIATION_METHODS = frozenset(
    {"OPTIONS", "GET_PARAMETER", "SET_PARAMETER", "SETUP", "PLAY", "TEARDOWN"}
)


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
    if isinstance(rfile, _RTSPReader):
        rfile.begin_message()
    lines = []
    header_bytes = 0
    while True:
        raw = rfile.readline(RTSP_MAX_LINE + 1)
        if not raw:
            if lines:
                raise WFDNotReady("Truncated RTSP headers")
            return None
        if len(raw) > RTSP_MAX_LINE or not raw.endswith(b"\n"):
            raise WFDNotReady("RTSP line exceeds limit or is truncated")
        header_bytes += len(raw)
        if header_bytes > RTSP_MAX_HEADER_BYTES:
            raise WFDNotReady("RTSP headers exceed byte limit")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            break
        if len(lines) >= RTSP_MAX_HEADERS + 1:
            raise WFDNotReady("Too many RTSP headers")
        lines.append(line)

    if not lines:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not sep or not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", key):
            raise WFDNotReady("Invalid RTSP header")
        # Samsung sends Content-Length twice; identical duplicates are safe.
        if key in headers and headers[key] != value:
            raise WFDNotReady(f"Conflicting RTSP header: {key}")
        headers[key] = value

    length = headers.get("content-length", "0")
    if not re.fullmatch(r"[0-9]{1,5}", length):
        raise WFDNotReady("Invalid RTSP Content-Length")
    content_length = int(length)
    if content_length > RTSP_MAX_BODY:
        raise WFDNotReady("RTSP body exceeds limit")

    body = bytearray()
    while len(body) < content_length:
        chunk = rfile.read(content_length - len(body))
        if not chunk:
            raise WFDNotReady("Truncated RTSP body")
        body.extend(chunk)

    return RTSPMessage(
        start=lines[0],
        headers=headers,
        raw_headers=lines[1:],
        body=body.decode("utf-8", errors="replace"),
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
        r"(?:^|\s)RTP/AVP/(?:UDP|TCP);unicast\s+([0-9]{1,5})\s+([0-9]{1,5})\s+mode=play(?:$|[;\s])",
        value,
        re.IGNORECASE,
    )
    if match:
        ports = int(match.group(1)), int(match.group(2))
        return ports if all(port <= 65535 for port in ports) else None
    return None

def _parse_transport_client_ports(value: str) -> Optional[tuple[int, int]]:
    fields = [field.strip() for field in value.split(";")]
    ports = [field.partition("=")[2].strip() for field in fields
             if field.partition("=")[0].strip().lower() == "client_port"]
    if len(ports) != 1:
        return None
    match = re.fullmatch(r"([0-9]{1,5})(?:-([0-9]{1,5}))?", ports[0])
    if match is None:
        return None
    pair = int(match.group(1)), int(match.group(2) or "0")
    return pair if all(port <= 65535 for port in pair) else None
