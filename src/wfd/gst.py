import re
import shutil
import subprocess
from typing import Optional

from .config import WFDNotReady


def _gst_has_element(name: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0

def _wfd_gst_prog_map(with_audio: bool, aosp_pmt_pid: bool = False) -> str:
    # PMT_<program> must be uint-typed; a plain int is parsed as gint and
    # silently ignored, leaving the PMT on mpegtsmux's 0x0020 default.
    prog_map = "program_map,sink_4113=1"
    if with_audio:
        prog_map += ",sink_4352=1"
    if aosp_pmt_pid:
        prog_map += ",PMT_1=(uint)256"
    return prog_map

def _gst_rtp_link(dump_ts_path: Optional[str]) -> list[str]:
    payloader = ["!", "rtpmp2tpay", "pt=33", "mtu=1328"]
    if not dump_ts_path:
        return payloader
    return ["!", "tee", "name=tsdump", "!", "queue"] + payloader

def _gst_dump_branch(dump_ts_path: Optional[str]) -> list[str]:
    if not dump_ts_path:
        return []
    return [
        "tsdump.",
        "!", "queue", "leaky=downstream", "max-size-buffers=512",
        "!", "filesink", f"location={dump_ts_path}", "async=false",
    ]

def _gst_wfd_sender_available() -> bool:
    return (
        shutil.which("gst-launch-1.0") is not None
        and _gst_has_element("mpegtsmux")
        and _gst_has_element("rtpmp2tpay")
        and _gst_has_element("x264enc")
    )

def _gst_pipewiresrc_properties() -> set[str]:
    if not shutil.which("gst-inspect-1.0"):
        return set()
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", "pipewiresrc"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()

    props: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s{2}([a-z0-9_-]+)\s+:", line)
        if match:
            props.add(match.group(1))
    return props

# Cached at first call; x264enc version varies across distros.
_gst_x264enc_props_cache: Optional[set[str]] = None

def _gst_x264enc_properties() -> set[str]:
    """Return the set of property names supported by the installed x264enc.
    Cached after the first call.
    """
    global _gst_x264enc_props_cache
    if _gst_x264enc_props_cache is not None:
        return _gst_x264enc_props_cache
    if not shutil.which("gst-inspect-1.0"):
        _gst_x264enc_props_cache = set()
        return _gst_x264enc_props_cache
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", "x264enc"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        _gst_x264enc_props_cache = set()
        return _gst_x264enc_props_cache
    if result.returncode != 0:
        _gst_x264enc_props_cache = set()
        return _gst_x264enc_props_cache

    props: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s{2}([a-z0-9_-]+)\s+:", line)
        if match:
            props.add(match.group(1))
    _gst_x264enc_props_cache = props
    return props

def _pipewiresrc_selector_attempts(
    node_id: int,
    stream_label: str = "",
) -> list[tuple[str, list[str]]]:
    props = _gst_pipewiresrc_properties()
    attempts: list[tuple[str, list[str]]] = []
    has_autoconnect = "autoconnect" in props

    def _add(base_name: str, base_args: list[str]) -> None:
        # Try compositor-friendly selector mode first.
        if has_autoconnect:
            attempts.append((base_name, [*base_args, "autoconnect=true"]))
        else:
            attempts.append((base_name, base_args))
        # Then strict mode pinned to the selected node.
        if has_autoconnect:
            attempts.append((base_name + "+strict", [*base_args, "autoconnect=false"]))

    if "path" in props:
        _add("path", [f"path={node_id}"])
    # Keep target-object fallback disabled for now. On the tested KDE/PipeWire
    # stack this branch is unstable and can trigger gst-launch crashes. kurva...
    _ = stream_label
    if not attempts:
        raise WFDNotReady(
            "Portal backend could not target a specific PipeWire stream node: "
            "pipewiresrc has neither target-object nor path property."
        )
    return attempts

def _gst_pick_aac_encoder() -> tuple[str, list[str]]:
    """
    Pick a broadly available AAC encoder and a compatible raw-audio caps filter.
    """
    if _gst_has_element("fdkaacenc"):
        return "fdkaacenc", ["audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"]
    if _gst_has_element("avenc_aac"):
        return "avenc_aac", ["audio/x-raw,rate=48000,channels=2"]
    if _gst_has_element("voaacenc"):
        return "voaacenc", ["audio/x-raw,rate=48000,channels=2"]
    if _gst_has_element("faac"):
        return "faac", ["audio/x-raw,rate=48000,channels=2"]
    raise WFDNotReady(
        "No usable GStreamer AAC encoder found (tried fdkaacenc, avenc_aac, voaacenc, faac)."
    )
