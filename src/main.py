"""
FluxCast entry point

Usage:
    python main.py [OPTIONS]

    --protocol dlna|cast|wfd wfd = Miracast/Wi-Fi Display (default)
                             dlna = UPnP native screening fallback
                             cast = pychromecast (needs Chromecast built-in)
    --host HOST              LAN IP to advertise to the TV (default: auto-detect)
    --port PORT              HTTP server port (default: 8080)
    --output-res WxH         Scale output (e.g. 1920x1080); default: native
    --fps N                  Frames per second (default: 30)
    --bitrate Xm             Video bitrate (default: 4M)
    --discover-timeout N     mDNS/UPnP scan timeout (default: 5)
    --transport progressive-ts|hls|live-ts
                             progressive-ts = low-latency Samsung mode (default)
                             hls = stable Samsung HLS fallback
                             live-ts = experimental MPEG-TS mode
    --doctor                 Print passive Linux/WFD capability diagnostics
    --wfd-scan               Run an active Wi-Fi Direct peer scan via wpa_cli
    --wfd-peer PEER          WFD peer selector for --protocol wfd (index, MAC, name)
    --wfd-dry-run            Print WFD connection D-Bus call without activating it
    --wfd-test-pattern       Stream a generated test pattern instead of the desktop
    --wfd-latency-log PATH   Write WFD latency/session events to JSONL log file
    --tv-ip IP               (For cast protocol only: direct IP connection)
"""

import argparse
import glob
import os
import signal
import socket
import time
import sys
import termios

from capture import prompt_monitor, start_capture, stop_capture
from server import HLS_DIR, StreamServer


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FluxCast — stream your Arch Linux desktop to a Smart TV"
    )
    parser.add_argument("--protocol", default="wfd",
                        choices=["dlna", "cast", "wfd"],
                        help="Connection protocol: wfd (Miracast, default), "
                             "dlna (UPnP fallback), or cast (Chromecast built-in)")
    parser.add_argument("--tv-ip", default=None, dest="tv_ip",
                        help="TV IP address (only applicable for --protocol cast)")
    parser.add_argument("--host", default=None,
                        help="LAN IP to advertise in the stream URL (default: auto)")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    parser.add_argument("--output-res", default=None, dest="output_res",
                        help="Scale output to WxH, e.g. 1920x1080 (default: native)")
    parser.add_argument("--fps", type=int, default=30,
                        help="Frames per second (default: 30)")
    parser.add_argument("--bitrate", default="4M",
                        help="Video bitrate (default: 4M)")
    parser.add_argument("--discover-timeout", type=int, default=5,
                        dest="discover_timeout",
                        help="Discovery timeout in seconds (default: 5)")
    parser.add_argument("--capture-backend", default="auto", dest="capture_backend",
                        choices=["auto", "wf-recorder", "x11grab"],
                        help="Desktop capture backend for dlna/cast: auto (default), wf-recorder, or x11grab")
    parser.add_argument("--transport", default="progressive-ts",
                        choices=["progressive-ts", "hls", "live-ts"],
                        help="DLNA stream transport: progressive-ts for low latency "
                             "(default), hls as a stable fallback, or live-ts experimental")
    parser.add_argument("--doctor", action="store_true",
                        help="Print passive Linux/WFD capability diagnostics and exit")
    parser.add_argument("--doctor-json", action="store_true", dest="doctor_json",
                        help="Print diagnostics as JSON and exit")
    parser.add_argument("--wfd-scan", action="store_true", dest="wfd_scan",
                        help="Run active Wi-Fi Direct discovery and exit")
    parser.add_argument("--wfd-peer", default=None, dest="wfd_peer",
                        help="WFD peer selector for --protocol wfd: index, MAC, or name")
    parser.add_argument("--wfd-dry-run", action="store_true", dest="wfd_dry_run",
                        help="Print WFD connection D-Bus call without activating it")
    parser.add_argument("--wfd-test-pattern", action="store_true", dest="wfd_test_pattern",
                        help="For --protocol wfd, stream generated test video instead of the desktop")
    parser.add_argument("--wfd-media-pipeline", default="auto",
                        choices=["auto", "ffmpeg", "gst"],
                        dest="wfd_media_pipeline",
                        help="For --protocol wfd, RTP media sender: auto (gst for test-pattern, ffmpeg for desktop), ffmpeg, or gst")
    parser.add_argument("--wfd-capture-backend", default="auto", dest="wfd_capture_backend",
                        choices=["auto", "portal", "wf-recorder", "x11grab"],
                        help="Desktop capture backend for --protocol wfd: auto (default), portal, wf-recorder, or x11grab")
    parser.add_argument("--wfd-latency-log", nargs="?", const="/tmp/fluxcast-wfd-latency.jsonl",
                        default=None, dest="wfd_latency_log",
                        help="For --protocol wfd, JSONL file path for latency/session logging "
                             "(default: /tmp/fluxcast-wfd-latency.jsonl)")
    parser.add_argument("--wfd-no-audio", action="store_true", dest="wfd_no_audio",
                        help="For --protocol wfd, stream video only")
    parser.add_argument("--wfd-audio-device", default=None, dest="wfd_audio_device",
                        help="Pulse/PipeWire monitor source for --protocol wfd audio")
    parser.add_argument("--wfd-rtsp-port", type=int, default=7236, dest="wfd_rtsp_port",
                        help="RTSP port advertised in WFD IEs (default: 7236)")
    parser.add_argument("--wfd-rtp-source-port", type=int, default=19002, dest="wfd_rtp_source_port",
                        help="Local RTP source port for --protocol wfd (default: 19002)")
    parser.add_argument("--wfd-interface", default=None, dest="wfd_interface",
                        help="Wi-Fi interface to use for --wfd-scan, e.g. wlan0")
    parser.add_argument("--wfd-timeout", type=int, default=8, dest="wfd_timeout",
                        help="Wi-Fi Direct scan timeout in seconds (default: 8)")
    parser.add_argument("--wfd-monitor", default=None, dest="wfd_monitor",
                        help="Monitor name for WFD capture, e.g. eDP-1 (skips interactive picker)")
    parser.add_argument("--device-name", default=None, dest="device_name",
                        help="Pre-select DLNA/Cast device by friendly name (skips interactive picker)")
    parser.add_argument("--monitor", default=None, dest="monitor_name",
                        help="Pre-select monitor by name for DLNA/Cast capture, e.g. eDP-1 (skips picker)")
    parser.add_argument("--tray", action="store_true",
                        help="Launch system tray interface (no terminal needed)")
    return parser.parse_args()


# ── terminal helpers ──────────────────────────────────────────────────────────

def _save_term():
    try:
        if sys.stdin.isatty():
            return termios.tcgetattr(sys.stdin.fileno())
    except termios.error:
        pass
    return None


def _restore_term(saved) -> None:
    if saved is None:
        return
    try:
        if sys.stdin.isatty():
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    except termios.error:
        pass


def _wait_for_hls_segments(required_segments: int = 2, timeout: float = 15.0) -> bool:
    playlist = os.path.join(HLS_DIR, "stream.m3u8")
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        segments = []
        for path in glob.glob(os.path.join(HLS_DIR, "stream*.ts")):
            try:
                if os.path.getsize(path) > 0:
                    segments.append(path)
            except OSError:
                pass
        if os.path.exists(playlist) and os.path.getsize(playlist) > 0:
            try:
                with open(playlist, "r", encoding="utf-8", errors="replace") as file:
                    playlist_text = file.read()
            except OSError:
                playlist_text = ""
            listed_segments = playlist_text.count(".ts")
            if listed_segments >= required_segments and len(segments) >= required_segments:
                waited = time.monotonic() - start
                print(f" ready! ({len(segments)} segments, {waited:.1f}s)")
                return True
        time.sleep(0.2)

    print(" [TIMEOUT]")
    return False


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    if args.tray:
        from tray import run_tray
        run_tray()
        return

    if args.doctor or args.doctor_json:
        from diagnostics import print_report, run_diagnostics
        report = run_diagnostics()
        if args.doctor_json:
            print(report.to_json())
        else:
            print_report(report)
        return

    if args.wfd_scan:
        from wfd import WFDNotReady, active_scan, print_scan
        try:
            peers = active_scan(interface=args.wfd_interface, timeout=args.wfd_timeout)
        except WFDNotReady as exc:
            print(f"[FluxCast WFD] ERROR: {exc}")
            sys.exit(1)
        print_scan(peers)
        return

    if args.protocol == "wfd":
        from wfd import WFDNotReady, start_experimental_backend
        try:
            start_experimental_backend(args)
        except WFDNotReady as exc:
            print(f"[FluxCast WFD] ERROR: {exc}")
            sys.exit(1)
        return

    host = args.host or get_local_ip()
    session_id = f"session-{int(time.time())}"
    if args.transport == "live-ts":
        stream_name = "live.ts"
    elif args.transport == "progressive-ts":
        stream_name = "progressive.ts"
    else:
        stream_name = "stream.m3u8"
    stream_path = f"{session_id}/{stream_name}"
    stream_url = f"http://{host}:{args.port}/{stream_path}"

    _tty_state = _save_term() # save before ffmpeg corrupts it

    ffmpeg_procs = None
    stream_server = None
    tv = None

    def shutdown(signum=None, frame=None):
        print("\n[FluxCast] Stopping…")
        if tv is not None:
            try:
                if args.protocol == "dlna":
                    from dlna import stop_cast
                    stop_cast(tv)
                else:
                    import cast as cast_backend
                    cast_backend.stop_cast(tv)
            except Exception:
                pass
        if stream_server:
            stream_server.stop()
        stop_capture(ffmpeg_procs)
        _restore_term(_tty_state)
        print("[FluxCast] Stopped. Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    protocol_label = "Chromecast" if args.protocol == "cast" else "UPnP/DLNA"
    print("=" * 55)
    print(f"  FluxCast - Desktop → Smart TV via {protocol_label}")
    print("=" * 55)

    if args.monitor_name:
        from capture import gather_monitors
        all_mons = gather_monitors()
        monitor = next((m for m in all_mons if m.name == args.monitor_name), None)
        if monitor is None:
            available = ", ".join(m.name for m in all_mons) or "none"
            print(f"[FluxCast] ERROR: Monitor '{args.monitor_name}' not found. Available: {available}")
            shutdown()
    else:
        monitor = prompt_monitor()

    ffmpeg_procs = start_capture(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=args.output_res,
        backend=args.capture_backend,
    )
    print("[FluxCast] Screen capture started.")

    # HTTP server serves the HLS playlist and MPEG-TS segments from /tmp/fluxcast.
    stream_server = StreamServer(host="0.0.0.0", port=args.port)
    stream_server.start()
    print(f"[FluxCast] HTTP server: {stream_url}")
    print(f"[FluxCast] Session: {session_id}")
    print(f"[FluxCast] Transport: {args.transport}")

    print("[FluxCast] Waiting for HLS source to start…", end="", flush=True)
    if not _wait_for_hls_segments(required_segments=2, timeout=15.0):
        print("[FluxCast] ERROR: ffmpeg produced no playable HLS segments.")
        shutdown()
    print("[FluxCast] HLS source is producing segments ✓")

    if args.protocol == "dlna":
        from dlna import discover_devices, prompt_device, start_cast
        devices = discover_devices(timeout=args.discover_timeout)
        tv = prompt_device(devices, args.device_name)
        start_cast(tv, stream_url)

    else:  # cast protocol
        from cast import discover_devices, connect_by_ip, prompt_device, start_cast
        if args.tv_ip:
            tv = connect_by_ip(args.tv_ip)
        else:
            print("[FluxCast] Searching for Cast devices on the network…")
            devices = discover_devices(timeout=args.discover_timeout)
            tv = prompt_device(devices, args.device_name)
            print(f"[FluxCast] Found: {tv.cast_info.friendly_name}")
        start_cast(tv, stream_url)

    print("[FluxCast] Casting started. Press Ctrl+C to stop.")
    signal.pause()


if __name__ == "__main__":
    main()
