from __future__ import annotations

import http.client
import http.server
import os
import plistlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional


_AIRPLAY_DEFAULT_PORT = 7000

def find_samsung_airplay(timeout: float = 6.0) -> Optional[tuple[str, int]]:
    """Return (ip, port) of first Samsung AirPlay TV found via mDNS."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf

        found: list[tuple[str, int]] = []
        ready = threading.Event()

        class _H:
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info:
                    addrs = info.parsed_addresses()
                    if addrs:
                        found.append((addrs[0], info.port))
                        ready.set()

            def remove_service(self, *_): pass
            def update_service(self, *_): pass

        zc = Zeroconf()
        ServiceBrowser(zc, "_airplay._tcp.local.", _H())
        ready.wait(timeout)
        zc.close()
        return found[0] if found else None
    except ImportError:
        return None


class HLSStreamServer:
    def __init__(self, local_ip: str, port: int = 9876,
                 test_pattern: bool = False,
                 bitrate: str = "4M") -> None:
        self._ip = local_ip
        self._port = port
        self._test = test_pattern
        self._bitrate = bitrate
        self._tmpdir: Optional[str] = None
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._wfr: Optional[subprocess.Popen] = None   # wf-recorder process
        self._http: Optional[http.server.HTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self._ip}:{self._port}/stream.m3u8"

    def start(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="fluxcast_ap_")
        os.chmod(self._tmpdir, 0o755)
        playlist = os.path.join(self._tmpdir, "stream.m3u8")

        if self._test:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
                "-vcodec", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-b:v", self._bitrate,
                "-f", "hls",
                "-hls_time", "1", "-hls_list_size", "5",
                "-hls_flags", "delete_segments+append_list",
                playlist,
            ]
            self._ffmpeg = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            # wf-recorder → MPEG-TS → ffmpeg stdin → HLS
            # --muxer=mpegts makes output streamable (no seeking needed)
            self._wfr = subprocess.Popen(
                ["wf-recorder", "-c", "libx264",
                 "--muxer=mpegts",
                 "-p", "preset=ultrafast",
                 "-p", "tune=zerolatency",
                 "-f", "/dev/stdout"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            self._ffmpeg = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", "pipe:0",
                    "-c:v", "copy",
                    "-f", "hls",
                    "-hls_time", "1", "-hls_list_size", "5",
                    "-hls_flags", "delete_segments+append_list",
                    playlist,
                ],
                stdin=self._wfr.stdout,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._wfr.stdout.close()

        # Wait for first HLS segment to appear
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if os.path.exists(playlist):
                break
            time.sleep(0.3)
        else:
            raise RuntimeError(
                "ffmpeg did not produce HLS output. "
                "Try --wfd-test-pattern to verify the pipeline."
            )

        if self._ffmpeg.poll() is not None:
            raise RuntimeError("ffmpeg exited immediately.")

        # HTTP server for HLS files
        tmpdir = self._tmpdir

        class _H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=tmpdir, **kw)
            def log_message(self, fmt, *args):
                print(f"[AirPlay HTTP] {self.address_string()} {fmt % args}")

        self._http = http.server.HTTPServer(("", self._port), _H)
        self._http_thread = threading.Thread(
            target=self._http.serve_forever, daemon=True
        )
        self._http_thread.start()
        print(f"[AirPlay] HLS stream ready: {self.url}")

    def stop(self) -> None:
        for proc in (self._ffmpeg, self._wfr):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        if self._http:
            try:
                self._http.shutdown()
            except Exception:
                pass
        if self._tmpdir and os.path.isdir(self._tmpdir):
            try:
                shutil.rmtree(self._tmpdir)
            except Exception:
                pass

class AirPlayClient:
    """Sends AirPlay play/stop commands to Samsung TV."""

    def __init__(self, tv_ip: str, tv_port: int = _AIRPLAY_DEFAULT_PORT) -> None:
        self._ip = tv_ip
        self._port = tv_port
        self._session = str(uuid.uuid4()).upper()
        self._device_id = ":".join(f"{b:02x}" for b in uuid.uuid4().bytes[:6])
        self._conn: Optional[http.client.HTTPConnection] = None

    def connect(self) -> None:
        self._conn = http.client.HTTPConnection(self._ip, self._port, timeout=10)
        try:
            info = self._get("/info")
            if isinstance(info, dict):
                name = info.get("name", self._ip)
                feats = info.get("features", 0)
                model = info.get("model", "?")
                print(f"[AirPlay] Connected to: {name} ({model})")
                print(f"[AirPlay] features=0x{feats:08x}  "
                      f"pin_required={bool(feats & 0x800)}  "
                      f"screen_mirror={bool(feats & 0x20000)}  "
                      f"video={bool(feats & 0x2)}")
            else:
                print(f"[AirPlay] Connected to: {self._ip}")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"[AirPlay] /info: {e}")

    def play(self, url: str) -> None:
        body = f"Content-Location: {url}\nStart-Position: 0.000000\n".encode()
        self._post("/play", body, "text/parameters")
        print(f"[AirPlay] Play → {url}")

    def stop(self) -> None:
        try:
            self._post("/stop", b"", "")
        except Exception:
            pass
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

    def _headers(self, body_len: int = 0, ctype: str = "") -> dict:
        h = {
            "User-Agent": "AirPlay/417.0.0",
            "X-Apple-Session-ID": self._session,
            "X-Apple-Device-ID": self._device_id,
            "DACP-ID": self._session.replace("-", "")[:16].upper(),
            "Active-Remote": "1",
            "Content-Length": str(body_len),
        }
        if ctype:
            h["Content-Type"] = ctype
        return h

    def _get(self, path: str) -> object:
        self._conn.request("GET", path, headers=self._headers())
        resp = self._conn.getresponse()
        data = resp.read()
        if resp.status == 401:
            raise RuntimeError(
                "AirPlay requires PIN authentication.\n"
                "  → On TV: a PIN should appear. We don't support PIN yet."
            )
        if resp.status == 403:
            raise RuntimeError("AirPlay connection refused by TV.")
        try:
            return plistlib.loads(data)
        except Exception:
            return {}

    def _post(self, path: str, body: bytes, ctype: str) -> None:
        self._conn.request(
            "POST", path, body=body,
            headers=self._headers(len(body), ctype)
        )
        resp = self._conn.getresponse()
        data = resp.read()
        print(f"[AirPlay] {path} → HTTP {resp.status}")
        if data:
            try:
                parsed = plistlib.loads(data)
                print(f"[AirPlay] response: {parsed}")
            except Exception:
                print(f"[AirPlay] response raw: {data[:200]}")

def _get_local_ip() -> Optional[str]:
    import socket as _sock
    try:
        with _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def run_airplay_session(args) -> None:
    from wfd import WFDNotReady

    local_ip = _get_local_ip()
    if not local_ip:
        raise WFDNotReady("Cannot determine LAN IP.")

    print("[AirPlay] Searching for Samsung TV...")
    result = find_samsung_airplay(timeout=6.0)
    if not result:
        raise WFDNotReady(
            "Samsung TV not found via AirPlay mDNS.\n"
            "  → TV: Settings → General → Apple AirPlay Settings → AirPlay: ON"
        )

    tv_ip, tv_port = result
    print(f"[AirPlay] Found TV: {tv_ip}:{tv_port}")

    test_pattern = getattr(args, "wfd_test_pattern", False)
    bitrate = getattr(args, "bitrate", "4M")

    stream = HLSStreamServer(local_ip, port=9876,
                             test_pattern=test_pattern, bitrate=bitrate)
    client = AirPlayClient(tv_ip, tv_port)

    try:
        stream.start()
        client.connect()
        client.play(stream.url)
        print("[AirPlay] Streaming. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[AirPlay] Stopping...")
    finally:
        client.stop()
        stream.stop()
