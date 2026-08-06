import shutil
import subprocess
from typing import Optional

from .proc import _run


def _safe_source_port(requested: int, sink_port: int, sink_rtcp_port: int = 0) -> int:
    blocked = {sink_port}
    if sink_rtcp_port:
        blocked.add(sink_rtcp_port)
    else:
        blocked.add(sink_port + 1)

    port = requested
    if port % 2:
        port += 1
    while port in blocked or port + 1 in blocked:
        port += 2
    return port

def _rtp_url(tv_ip: str, sink_port: int, source_port: int, local_ip: str) -> str:
    # Bind both RTP and RTCP to the ports advertised in the RTSP SETUP reply.
    # ffmpeg's pkt_size is the whole UDP payload, including the 12-byte RTP
    # header. WFD receivers expect seven 188-byte TS packets per RTP payload:
    # 12 + (7 * 188) = 1328 bytes.
    return (
        f"rtp://{tv_ip}:{sink_port}"
        f"?localaddr={local_ip}"
        f"&local_rtpport={source_port}"
        f"&local_rtcpport={source_port + 1}"
        "&pkt_size=1328"
    )

def _interface_for_ip(local_ip: str) -> Optional[str]:
    if not shutil.which("ip"):
        return None
    try:
        result = _run(["ip", "-o", "-4", "addr", "show"], timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    needle = f" {local_ip}/"
    for line in result.stdout.splitlines():
        if needle not in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            return parts[1].split("@", 1)[0]
    return None

def _netdev_tx_bytes(interface: Optional[str]) -> Optional[int]:
    if not interface:
        return None
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError:
        return None

    prefix = interface + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        _, _, counters = stripped.partition(":")
        fields = counters.split()
        if len(fields) >= 16:
            try:
                return int(fields[8])
            except ValueError:
                return None
    return None

def _ffmpeg_sender_args(show_stats: bool = False) -> list[str]:
    args = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning",
    ]
    if show_stats:
        args.append("-stats")
    return args
