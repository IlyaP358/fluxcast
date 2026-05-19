import sys
import time
from typing import Optional
from urllib.parse import urlsplit

try:
    import upnpclient
except ImportError:
    print("[FluxCast] ERROR: upnpclient is not installed. "
          "Run: pip install upnpclient")
    sys.exit(1)


HLS_PROTOCOL_INFO = "http-get:*:application/x-mpegurl:*"
TS_PROTOCOL_INFO = (
    "http-get:*:video/mpeg:"
    "DLNA.ORG_PN=AVC_TS_MP_HD_AAC;"
    "DLNA.ORG_OP=01;"
    "DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01700000000000000000000000000000"
)


def _protocol_info(stream_url: str) -> str:
    path = urlsplit(stream_url).path
    if path.endswith(".m3u8"):
        return HLS_PROTOCOL_INFO
    return TS_PROTOCOL_INFO


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


def _build_didl_metadata(stream_url: str) -> str:
    """
    DIDL-Lite XML metadata payload for DLNA AVTransport.
    """
    from xml.sax.saxutils import escape
    
    didl = f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" 
    xmlns:dc="http://purl.org/dc/elements/1.1/" 
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" 
    xmlns:sec="http://www.sec.co.kr/">
        <item id="1" parentID="0" restricted="1">
            <upnp:class>object.item.videoItem</upnp:class>
            <dc:title>FluxCast Screen Mirroring</dc:title>
            <res protocolInfo="{_protocol_info(stream_url)}">{escape(stream_url)}</res>
        </item>
    </DIDL-Lite>"""
    return didl


def start_cast(device, stream_url: str) -> None:
    print(f"[FluxCast] Sending stream URL to {device.friendly_name} via UPnP/DLNA…")
    
    didl_metadata = _build_didl_metadata(stream_url)
    
    try:
        try:
            device.AVTransport.Stop(InstanceID=0)
            time.sleep(0.2)
        except Exception:
            pass

        # Load URL via DIDL-Lite
        device.AVTransport.SetAVTransportURI(
            InstanceID=0,
            CurrentURI=stream_url,
            CurrentURIMetaData=didl_metadata
        )
        device.AVTransport.Play(InstanceID=0, Speed="1")
        print("[FluxCast] Output signal sent! The TV should open its native video player.")
    except Exception as exc:
        print(f"[FluxCast] ERROR: Failed to start DLNA stream: {exc}")


def stop_cast(device: Optional[object]) -> None:
    if device is None:
        return
    try:
        device.AVTransport.Stop(InstanceID=0)
    except Exception:
        pass
