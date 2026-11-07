import sys
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

try:
    import upnpclient
except ImportError:
    print("[FluxCast] ERROR: upnpclient is not installed. "
          "Run: pip install upnpclient")
    sys.exit(1)


HLS_PROTOCOL_INFO = "http-get:*:application/x-mpegurl:*"
GENERIC_VIDEO_PROTOCOL_INFO = "http-get:*:video/mpeg:*"
TS_PROTOCOL_INFO = (
    "http-get:*:video/mpeg:"
    "DLNA.ORG_PN=AVC_TS_MP_HD_AAC;"
    "DLNA.ORG_OP=01;"
    "DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01700000000000000000000000000000"
)

# MIME types a renderer may advertise when it understands HLS.  Many devices
# play HLS without ever listing it in ConnectionManager.GetProtocolInfo, so a
# miss here is a hint, not a rejection - start_cast() retries with generic
# video/mpeg metadata before giving up.
HLS_CONTENT_TYPES = {
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "video/mp2t",
}

PLAYING_STATES = {"PLAYING", "TRANSITIONING"}
STOPPED_STATES = {"STOPPED", "NO_MEDIA_PRESENT"}
# A paused renderer still owns the session; treating it as idle would stop the
# cast as soon as the viewer pauses.
ACTIVE_STATES = PLAYING_STATES | {"PAUSED_PLAYBACK"}


def _sink_entries(device) -> list[str]:
    """Return the renderer's advertised Sink protocolInfo entries."""
    try:
        sink = device.ConnectionManager.GetProtocolInfo().get("Sink") or ""
    except Exception:
        return []
    return [entry.strip() for entry in sink.split(",") if entry.strip()]


def _entry_content_type(entry: str) -> str:
    fields = entry.split(":")
    return fields[2].lower() if len(fields) >= 3 else ""


def _looks_like_samsung(device) -> bool:
    name = (getattr(device, "friendly_name", "") or "").lower()
    model = (getattr(device, "model_name", "") or "").lower()
    return "samsung" in name or "samsung" in model


def _protocol_info(stream_url: str, device=None) -> str:
    path = urlsplit(stream_url).path
    entries = _sink_entries(device) if device is not None else []
    # Samsung's renderer is the one the HLS path was tuned for and it plays
    # HLS without advertising it, so keep the historical metadata there.
    samsung = device is not None and _looks_like_samsung(device)

    if path.endswith(".m3u8"):
        if (
            not samsung
            and entries
            and not any(
                _entry_content_type(entry) in HLS_CONTENT_TYPES
                for entry in entries
            )
        ):
            # The renderer did not advertise HLS.  It may still understand it;
            # advertising the URL as plain video/mpeg gets past renderers that
            # validate protocolInfo against their own sink list.
            return GENERIC_VIDEO_PROTOCOL_INFO
        return HLS_PROTOCOL_INFO

    if (
        not samsung
        and entries
        and not any("AVC_TS_MP_HD_AAC" in entry for entry in entries)
    ):
        # No matching DLNA profile: an unqualified video/mpeg resource is
        # accepted by far more renderers than a profile they never listed.
        return GENERIC_VIDEO_PROTOCOL_INFO
    return TS_PROTOCOL_INFO


def transport_state(device) -> Optional[str]:
    """Current AVTransport state, or None when the device cannot be asked."""
    try:
        info = device.AVTransport.GetTransportInfo(InstanceID=0)
    except Exception:
        return None
    return (info or {}).get("CurrentTransportState")


def discover_devices(timeout: int = 5) -> list:
    print(f"[FluxCast] Searching for UPnP/DLNA Cast devices (timeout={timeout}s)…")
    devices = upnpclient.discover(timeout=timeout)
    renderers = [d for d in devices if hasattr(d, "AVTransport")]
    
    unique_renderers = {}
    for r in renderers:
        if r.udn not in unique_renderers:
            unique_renderers[r.udn] = r
            
    return list(unique_renderers.values())


def prompt_device(devices: list, device_name: Optional[str] = None):
    if not devices:
        print("[FluxCast] ERROR: No DLNA renderers found on the network.")
        print("[FluxCast] Make sure the TV is ON and connected to the same Wi-Fi/LAN.")
        sys.exit(1)

    if device_name is not None:
        needle = device_name.lower()
        for dev in devices:
            if dev.friendly_name.lower() == needle:
                return dev
        print(f"[FluxCast] WARNING: Device '{device_name}' not found; falling back to picker.")

    print("\n[FluxCast] Found DLNA device(s):")
    for i, dev in enumerate(devices):
        name = dev.friendly_name
        model = dev.model_name
        print(f"  [{i}] {name}  ({model})")

    default_idx = 0
    for i, dev in enumerate(devices):
        if "samsung" in dev.model_name.lower() or "samsung" in dev.friendly_name.lower():
            default_idx = i
            break

    raw = input(f"Select device [{default_idx}]: ").strip()
    try:
        idx = int(raw) if raw else default_idx
        return devices[idx]
    except (ValueError, IndexError):
        print(f"[FluxCast] Invalid choice, using device {default_idx}.")
        return devices[default_idx]


def _build_didl_metadata(
    stream_url: str,
    device=None,
    protocol_info: Optional[str] = None,
) -> str:
    """
    DIDL-Lite XML metadata payload for DLNA AVTransport.
    """
    from xml.sax.saxutils import escape

    if protocol_info is None:
        protocol_info = _protocol_info(stream_url, device)

    didl = f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" 
    xmlns:dc="http://purl.org/dc/elements/1.1/" 
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" 
    xmlns:sec="http://www.sec.co.kr/">
        <item id="1" parentID="0" restricted="1">
            <upnp:class>object.item.videoItem</upnp:class>
            <dc:title>FluxCast Screen Mirroring</dc:title>
            <res protocolInfo="{protocol_info}">{escape(stream_url)}</res>
        </item>
    </DIDL-Lite>"""
    return didl


def _wait_for_playback(
    device,
    is_active: Optional[Callable[[], bool]] = None,
    timeout: float = 8.0,
    interval: float = 0.25,
) -> bool:
    """Wait until the renderer reports playing or starts pulling the stream."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_active is not None and is_active():
            return True
        if transport_state(device) in PLAYING_STATES:
            return True
        time.sleep(interval)
    return False


def _play(device) -> None:
    device.AVTransport.Play(InstanceID=0, Speed="1")


def _set_uri(device, stream_url: str, metadata: str) -> None:
    device.AVTransport.SetAVTransportURI(
        InstanceID=0,
        CurrentURI=stream_url,
        CurrentURIMetaData=metadata,
    )


def start_cast(
    device,
    stream_url: str,
    is_active: Optional[Callable[[], bool]] = None,
) -> bool:
    """Ask a renderer to play *stream_url* and verify that it really started.

    Returns True only when the renderer reaches PLAYING or its HTTP client
    starts fetching the stream.  Some DMRs ignore the first Play or reject a
    protocolInfo they did not advertise, so a couple of compatible retries are
    attempted before reporting failure.
    """
    print(f"[FluxCast] Sending stream URL to {device.friendly_name} via UPnP/DLNA…")

    attempts: list[tuple[str, str]] = []
    try:
        primary = _build_didl_metadata(stream_url, device)
    except Exception:
        primary = _build_didl_metadata(stream_url)
    attempts.append(("DIDL-Lite metadata", primary))

    generic = _build_didl_metadata(
        stream_url, protocol_info=GENERIC_VIDEO_PROTOCOL_INFO
    )
    if generic != primary:
        attempts.append(("generic video/mpeg metadata", generic))

    # A few renderers behave better when the resource carries no metadata at
    # all; keep that as the final compatibility attempt.
    attempts.append(("empty metadata", ""))

    for index, (label, metadata) in enumerate(attempts, start=1):
        try:
            try:
                device.AVTransport.Stop(InstanceID=0)
            except Exception:
                pass
            time.sleep(0.2)

            _set_uri(device, stream_url, metadata)
            # Some DMRs drop a Play that arrives in the same breath as SetURI.
            time.sleep(0.5)
            _play(device)

            if _wait_for_playback(device, is_active=is_active, timeout=8.0):
                print(
                    "[FluxCast] Output signal sent! The TV should open its "
                    "native video player."
                )
                return True

            # Play was ignored once; give the renderer another nudge.
            _play(device)
            if _wait_for_playback(device, is_active=is_active, timeout=3.0):
                print(
                    "[FluxCast] Output signal sent! The TV should open its "
                    "native video player."
                )
                return True

            print(
                f"[FluxCast] Renderer did not start playback "
                f"(attempt {index}/{len(attempts)}, {label})."
            )
        except Exception as exc:
            print(
                f"[FluxCast] WARNING: DLNA start attempt {index}/"
                f"{len(attempts)} failed ({label}): {exc}"
            )

    print(
        "[FluxCast] ERROR: The renderer accepted the URL but never started "
        "playback."
    )
    print(
        "[FluxCast] Make sure the TV is on its DLNA/media player screen, then "
        "try again. If it keeps failing, try the other DLNA transport "
        "(--transport hls or --transport live-ts)."
    )
    return False


def watch_playback(
    device,
    idle_limit: int = 6,
    interval: float = 3.0,
) -> None:
    """Block until the renderer stops playing.

    A renderer that returns to its menu either stops answering
    GetTransportInfo or reports STOPPED.  ``idle_limit`` consecutive idle
    polls are required so one missed poll does not tear down a cast that is
    still running.
    """
    misses = 0
    while True:
        time.sleep(interval)
        state = transport_state(device)
        if state in ACTIVE_STATES:
            misses = 0
        elif state is None or state in STOPPED_STATES:
            misses += 1
        else:
            misses = 0
        if misses >= idle_limit:
            print("\n[FluxCast] Renderer is no longer playing.")
            return


def stop_cast(device: Optional[object]) -> None:
    if device is None:
        return
    try:
        device.AVTransport.Stop(InstanceID=0)
    except Exception:
        pass
