import sys
import time
from typing import Optional

try:
    import pychromecast
except ImportError:
    print("[FluxCast] ERROR: pychromecast is not installed. "
          "Run: pip install pychromecast")
    sys.exit(1)


def discover_devices(timeout: int = 10) -> list:
    print(f"[FluxCast] Searching for Cast devices (timeout={timeout}s)…")
    chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
    pychromecast.discovery.stop_discovery(browser)
    return chromecasts


def connect_by_ip(ip: str, port: int = 8009):
    print(f"[FluxCast] Connecting directly to Cast device at {ip}:{port}…")
    try:
        known = [f"{ip}:{port}"] if port != 8009 else [ip]
        chromecasts, browser = pychromecast.get_listed_chromecasts(known_hosts=known)
        pychromecast.discovery.stop_discovery(browser)
    except Exception as exc:
        print(f"[FluxCast] ERROR: Discovery failed — {exc}")
        sys.exit(1)

    if not chromecasts:
        print(f"[FluxCast] ERROR: No Cast device responded at {ip}:{port}.")
        print("[FluxCast] Make sure the TV is ON, on the same network, "
              "and Cast is enabled in Settings → General → External Device Manager.")
        sys.exit(1)

    cast = chromecasts[0]
    cast.wait(timeout=10)
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
            if cc.cast_info.friendly_name.lower() == needle:
                return cc
        print(f"[FluxCast] WARNING: Device '{device_name}' not found; falling back to picker.")

    print("\n[FluxCast] Found Cast device(s):")
    for i, cc in enumerate(devices):
        name = cc.cast_info.friendly_name
        model = cc.cast_info.model_name
        host = cc.cast_info.host
        print(f"  [{i}] {name}  ({model})  —  {host}")

    default_idx = 0
    for i, cc in enumerate(devices):
        if "samsung" in cc.cast_info.model_name.lower():
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
    mc = device.media_controller
    mc.play_media(stream_url, "video/mp2t")
    mc.block_until_active(timeout=15)
    print(f"[FluxCast] Cast started → {device.cast_info.friendly_name}")


def stop_cast(device: Optional[object]) -> None:
    if device is None:
        return
    try:
        device.media_controller.stop()
        device.disconnect(timeout=5)
    except Exception:
        pass
