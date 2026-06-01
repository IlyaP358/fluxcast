from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

log = logging.getLogger(__name__)

_BSDP_PORT = 15600
_BSDP_MCAST = "239.255.255.250"
_BSDP_SEARCH = b"SEARCH BSDP/0.1" # prefix to match


class BSDPResponder:
    def __init__(self, local_ip: str, rtsp_port: int = 7236) -> None:
        self._local_ip = local_ip
        self._rtsp_port = rtsp_port
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        self._sock.bind(("", _BSDP_PORT))
        try:
            mreq = struct.pack("4s4s",
                               socket.inet_aton(_BSDP_MCAST),
                               socket.inet_aton(self._local_ip))
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass  # multicast join optional, broadcast + unicast still works
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("[BSDP] Listening on UDP %d for Samsung Screen Mirroring discovery", _BSDP_PORT)
        print(f"[WFD LAN] BSDP responder active on port {_BSDP_PORT} — Samsung TV will find us.")

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _loop(self) -> None:
        while self._running and self._sock:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if _BSDP_SEARCH in data:
                print(f"[WFD LAN] BSDP search from {addr[0]} — responding with {self._local_ip}:{self._rtsp_port}")
                log.info("[BSDP] search from %s", addr[0])
                self._respond(addr)

    def _respond(self, addr: tuple) -> None:
        response = (
            "RESPONSE BSDP/0.1\r\n"
            "DEVICE=1\r\n"
            "SERVICE=1\r\n"
            f"IP={self._local_ip}\r\n"
            f"PORT={self._rtsp_port}\r\n"
            "NAME=FluxCast\r\n"
            "\r\n"
        ).encode()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(response, addr)
        except OSError as e:
            log.warning("[BSDP] respond error: %s", e)


# ── SSDP ──────────────────────────────────────────────────────────────────────
_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900
_SSDP_TTL = 4
_SSDP_MAX_AGE = 1800

_SSDP_ST = "urn:schemas-wifialliance-org:device:WFDDevice:1"

# ── mDNS ──────────────────────────────────────────────────────────────────────
_MDNS_SERVICE_TYPE = "_wfd._tcp.local."


# ── Device description XML (served at the SSDP LOCATION URL) ─────────────────
def _device_xml(device_uuid: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        "<specVersion><major>1</major><minor>1</minor></specVersion>"
        "<device>"
        f"<deviceType>{_SSDP_ST}</deviceType>"
        "<friendlyName>FluxCast</friendlyName>"
        "<manufacturer>FluxCast</manufacturer>"
        "<modelName>FluxCast WFD Source</modelName>"
        f"<UDN>uuid:{device_uuid}</UDN>"
        "</device>"
        "</root>"
    ).encode()


class _DescHandler(BaseHTTPRequestHandler):
    device_uuid: str = ""

    def do_GET(self) -> None:
        body = _device_xml(self.device_uuid)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class _DescServer:
    def __init__(self, local_ip: str, device_uuid: str) -> None:
        _DescHandler.device_uuid = device_uuid
        self._srv = HTTPServer((local_ip, 0), _DescHandler)
        self.port: int = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()


class SSDPAdvertiser:
    """Sends SSDP NOTIFY announcements and responds to M-SEARCH from Samsung TVs."""

    def __init__(self, local_ip: str, desc_url: str, device_uuid: str) -> None:
        self._local_ip = local_ip
        self._desc_url = desc_url
        self._uuid = device_uuid
        self._running = False
        self._sock: Optional[socket.socket] = None
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._announce_thread = threading.Thread(target=self._announce_loop, daemon=True)

    def start(self) -> None:
        self._running = True
        self._sock = self._make_socket()
        self._listen_thread.start()
        self._announce_thread.start()
        self._send_notify(alive=True)
        log.info("[WFD LAN] SSDP advertiser started on %s", self._local_ip)

    def stop(self) -> None:
        self._running = False
        try:
            self._send_notify(alive=False)
        except Exception:
            pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", _SSDP_PORT))
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(_SSDP_ADDR),
            socket.inet_aton(self._local_ip),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self._local_ip)
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, _SSDP_TTL)
        sock.settimeout(1.0)
        return sock

    def _listen_loop(self) -> None:
        while self._running and self._sock:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            msg = data.decode(errors="replace")
            if msg.startswith("M-SEARCH") and self._relevant(msg):
                log.info("[WFD LAN] M-SEARCH from %s:%d — responding", *addr)
                self._respond(addr)

    def _relevant(self, msg: str) -> bool:
        lower = msg.lower()
        return "st: ssdp:all" in lower or f"st: {_SSDP_ST.lower()}" in lower

    def _respond(self, addr: tuple) -> None:
        response = "\r\n".join([
            "HTTP/1.1 200 OK",
            f"CACHE-CONTROL: max-age={_SSDP_MAX_AGE}",
            "EXT:",
            f"LOCATION: {self._desc_url}",
            "SERVER: Linux UPnP/1.1 FluxCast/1.0",
            f"ST: {_SSDP_ST}",
            f"USN: uuid:{self._uuid}::{_SSDP_ST}",
            "CONTENT-LENGTH: 0",
            "\r\n",
        ])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(response.encode(), addr)

    def _announce_loop(self) -> None:
        while self._running:
            time.sleep(_SSDP_MAX_AGE // 3)
            if self._running:
                self._send_notify(alive=True)

    def _send_notify(self, alive: bool) -> None:
        nts = "ssdp:alive" if alive else "ssdp:byebye"
        lines = [
            "NOTIFY * HTTP/1.1",
            f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}",
            f"NT: {_SSDP_ST}",
            f"NTS: {nts}",
            f"USN: uuid:{self._uuid}::{_SSDP_ST}",
        ]
        if alive:
            lines += [
                f"CACHE-CONTROL: max-age={_SSDP_MAX_AGE}",
                f"LOCATION: {self._desc_url}",
                "SERVER: Linux UPnP/1.1 FluxCast/1.0",
            ]
        lines += ["", ""]
        msg = "\r\n".join(lines).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, _SSDP_TTL)
            s.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self._local_ip)
            )
            s.sendto(msg, (_SSDP_ADDR, _SSDP_PORT))


class MDNSAdvertiser:
    """Advertises WFD Source via mDNS/Zeroconf for LG, Android, and others."""

    def __init__(self, local_ip: str, rtsp_port: int) -> None:
        self._local_ip = local_ip
        self._rtsp_port = rtsp_port
        self._zc = None
        self._info = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.warning(
                "[WFD LAN] zeroconf not installed; mDNS advertisement disabled. "
                "Install with: pip install zeroconf"
            )
            return

        self._zc = Zeroconf()
        self._info = ServiceInfo(
            _MDNS_SERVICE_TYPE,
            f"FluxCast.{_MDNS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(self._local_ip)],
            port=self._rtsp_port,
            properties={b"wfd_device_type": b"00 0000 0001"},
        )
        self._zc.register_service(self._info)
        log.info("[WFD LAN] mDNS advertiser started (service: %s)", _MDNS_SERVICE_TYPE)

    def stop(self) -> None:
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception:
                pass


def get_local_ip() -> Optional[str]:
    """Return the router LAN IP, skipping loopback and P2P subnets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        # 192.168.49.x is the NM Wi-Fi Direct P2P subnet — skip it
        if ip.startswith("192.168.49."):
            return None
        return ip
    except OSError:
        return None


# ── Main driver ───────────────────────────────────────────────────────────────
class WFDLANDriver:
    def __init__(self, rtsp_port: int = 7236) -> None:
        self._rtsp_port = rtsp_port
        self._local_ip: Optional[str] = None
        self._desc_srv: Optional[_DescServer] = None
        self._ssdp: Optional[SSDPAdvertiser] = None
        self._mdns: Optional[MDNSAdvertiser] = None
        self._bsdp: Optional[BSDPResponder] = None
        self._device_uuid = str(uuid.uuid4())

    @property
    def local_ip(self) -> Optional[str]:
        return self._local_ip

    def start(self) -> None:
        self._local_ip = get_local_ip()
        if not self._local_ip:
            raise RuntimeError(
                "Could not determine LAN IP address. "
                "Make sure the machine is connected to a router."
            )

        self._desc_srv = _DescServer(self._local_ip, self._device_uuid)
        self._desc_srv.start()
        desc_url = f"http://{self._local_ip}:{self._desc_srv.port}/wfd.xml"

        self._ssdp = SSDPAdvertiser(self._local_ip, desc_url, self._device_uuid)
        self._ssdp.start()

        self._mdns = MDNSAdvertiser(self._local_ip, self._rtsp_port)
        self._mdns.start()

        # Samsung LAN discovery — must come LAST so RTSP port is known
        self._bsdp = BSDPResponder(self._local_ip, self._rtsp_port)
        self._bsdp.start()

        print(f"[FluxCast WFD LAN] Advertising on {self._local_ip}:{self._rtsp_port}")
        print("[FluxCast WFD LAN] Put TV in Screen Mirror / Wireless Display mode.")

    def stop(self) -> None:
        for component in (self._ssdp, self._mdns, self._bsdp, self._desc_srv):
            if component:
                try:
                    component.stop()
                except Exception:
                    pass
        log.info("[WFD LAN] Driver stopped.")


# ── Session entry point ───────────────────────────────────────────────────────
def run_lan_session(args) -> None:
    """Full WFD-over-LAN session: advertise via BSDP + SSDP, wait for TV."""
    from wfd import (
        WFD_RTSP_PORT,
        WFDMediaConfig,
        WFDNotReady,
        WFDRTSPServer,
        _is_hyprland_session,
        _is_wayland_session,
    )

    rtsp_port = getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT)

    test_pattern = getattr(args, "wfd_test_pattern", False)
    monitor = None
    selected_backend = getattr(args, "wfd_capture_backend", "auto")
    portal_mode = selected_backend == "portal" or (
        selected_backend == "auto"
        and _is_wayland_session()
        and not _is_hyprland_session()
    )
    if not test_pattern:
        if portal_mode:
            print("[WFD LAN] Portal backend: monitor selection via desktop portal dialog.")
        else:
            wfd_monitor_name = getattr(args, "wfd_monitor", None)
            if wfd_monitor_name:
                from capture import gather_monitors
                all_monitors = gather_monitors()
                monitor = next((m for m in all_monitors if m.name == wfd_monitor_name), None)
                if monitor is None:
                    available = ", ".join(m.name for m in all_monitors) or "none"
                    raise WFDNotReady(
                        f"Monitor '{wfd_monitor_name}' not found. Available: {available}"
                    )
            else:
                from capture import prompt_monitor
                monitor = prompt_monitor()

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=args.output_res,
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=getattr(args, "wfd_no_audio", False),
        test_pattern=test_pattern,
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        media_pipeline=getattr(args, "wfd_media_pipeline", "auto"),
        capture_backend=selected_backend,
    )

    rtsp = WFDRTSPServer(media_config=media_config, port=rtsp_port)
    driver = WFDLANDriver(rtsp_port=rtsp_port)

    try:
        rtsp.start()
        driver.start()
        print("[WFD LAN] Waiting for TV. Press Ctrl+C to stop.")
        while True:
            import time as _time
            _time.sleep(1)
    except KeyboardInterrupt:
        print("\n[WFD LAN] Stopping session...")
    finally:
        rtsp.stop_all_media()
        rtsp.stop()
        driver.stop()
