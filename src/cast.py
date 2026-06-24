import sys
import uuid
from typing import Optional

try:
    import pychromecast
    from pychromecast.const import CAST_TYPE_CHROMECAST
    from pychromecast.discovery import discover_chromecasts
    from pychromecast.models import CastInfo, HostServiceInfo
except ImportError:
    print("[FluxCast] ERROR: pychromecast is not installed. "
          "Run: pip install pychromecast")
    sys.exit(1)


class _MediaListener:
    #Surface receiver-side playback state so failures stop being silent hangs.
    def __init__(self):
        self._last = None

    def new_media_status(self, status):
        key = (status.player_state, status.idle_reason)
        if key == self._last:
            return
        self._last = key
        if status.player_state == "IDLE" and status.idle_reason:
            print(f"[FluxCast] TV playback: IDLE (reason={status.idle_reason})")
        else:
            print(f"[FluxCast] TV playback: {status.player_state}")

    def load_media_failed(self, item, error_code):
        print(f"[FluxCast] ERROR: TV failed to load media (error_code={error_code})")


class _ConnectionListener:
    #Make connection drops / retries visible.
    def __init__(self):
        self._last = None

    def new_connection_status(self, status):
        if status.status == self._last:
            return
        self._last = status.status
        print(f"[FluxCast] Cast connection: {status.status}")


def _direct_chromecast(host: str, port: int, device_uuid, model: str | None,
                       name: str | None, cast_type: str | None,
                       manufacturer: str | None):
    cast_info = CastInfo(
        services={HostServiceInfo(host, port)},
        uuid=device_uuid,
        model_name=model,
        friendly_name=name,
        host=host,
        port=port,
        cast_type=cast_type or CAST_TYPE_CHROMECAST,
        manufacturer=manufacturer,
    )
    return pychromecast.Chromecast(cast_info=cast_info)


def discover_devices(timeout: int = 10) -> list:
    print(f"[FluxCast] Searching for Cast devices (timeout={timeout}s)…")
    cast_infos, browser = discover_chromecasts(timeout=timeout)
    browser.stop_discovery()
    return [
        _direct_chromecast(
            info.host, info.port, info.uuid,
            info.model_name, info.friendly_name,
            info.cast_type, info.manufacturer,
        )
        for info in cast_infos
        if info.host
    ]


def connect_by_ip(ip: str, port: int = 8009):
    print(f"[FluxCast] Connecting directly to Cast device at {ip}:{port}…")
    fake_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"{ip}:{port}")
    cast = _direct_chromecast(ip, port, fake_uuid, "Chromecast", ip,
                              CAST_TYPE_CHROMECAST, None)
    try:
        cast.wait(timeout=10)
    except pychromecast.RequestTimeout:
        cast.disconnect()
        print(f"[FluxCast] ERROR: No Cast device responded at {ip}:{port}.")
        print("[FluxCast] Make sure the TV is ON, on the same network, "
              "and Cast is enabled in Settings → General → External Device Manager.")
        sys.exit(1)
    print(f"[FluxCast] Connected: {cast.cast_info.friendly_name}")
    return cast


def prompt_device(devices: list, device_name: Optional[str] = None):
    if not devices:
        print("[FluxCast] ERROR: No Cast devices found on the network.")
        print("[FluxCast] TIP: Use --tv-ip <IP> to connect directly, e.g.:")
        print("[FluxCast]      python main.py --protocol cast --tv-ip 192.168.100.XXX")
        sys.exit(1)

    if device_name is not None:
        needle = device_name.lower()
        for cc in devices:
            if (cc.cast_info.friendly_name or "").lower() == needle:
                return cc
        print(f"[FluxCast] WARNING: Device '{device_name}' not found; falling back to picker.")

    print("\n[FluxCast] Found Cast device(s):")
    for i, cc in enumerate(devices):
        name = cc.cast_info.friendly_name or cc.cast_info.host
        model = cc.cast_info.model_name or "Unknown"
        host = cc.cast_info.host
        print(f"  [{i}] {name}  ({model})  —  {host}")

    default_idx = 0
    for i, cc in enumerate(devices):
        if "samsung" in (cc.cast_info.model_name or "").lower():
            default_idx = i
            break

    raw = input(f"Select device [{default_idx}]: ").strip()
    try:
        idx = int(raw) if raw else default_idx
        return devices[idx]
    except (ValueError, IndexError):
        print(f"[FluxCast] Invalid choice, using device {default_idx}.")
        return devices[default_idx]


def start_cast(device, stream_url: str) -> None:
    device.wait()
    device.register_connection_listener(_ConnectionListener())
    mc = device.media_controller
    mc.register_status_listener(_MediaListener())
    content_type = (
        "application/x-mpegURL" if stream_url.endswith(".m3u8") else "video/mpeg"
    )
    mc.play_media(stream_url, content_type)
    mc.block_until_active(timeout=15)
    print(f"[FluxCast] Cast started → {device.cast_info.friendly_name}")
    print("[FluxCast] Waiting for the TV to report playback status (watch lines below)…")


def stop_cast(device: Optional[object]) -> None:
    if device is None:
        return
    try:
        device.media_controller.stop()
        device.disconnect(timeout=5)
    except Exception:
        pass
