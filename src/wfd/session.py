import threading
import time

from diagnostics import print_report, run_diagnostics

from .config import WFDMediaConfig, WFDNotReady
from .constants import WFD_RTSP_PORT, WFD_UIBC_PORT
from .dump import report_ts_dump
from .env import _is_hyprland_session, _is_wayland_session
from .firewall import _close_wfd_firewall_port, _open_wfd_firewall_port
from .p2p.device import _set_p2p_device_name, _set_p2p_go_intent
from .p2p.nm import (
    _connect_peer, _deactivate_connection, _disconnect_device,
    _nm_p2p_device_path, _wait_for_nm_activation,
)
from .p2p.peers import _scan_and_select
from .p2p.wpas import connect_via_wpa_supplicant, release_wpa_supplicant_connection
from .probe import _active_rtsp_probe
from .rtsp.rtsp_server import WFDRTSPServer


def _cleanup_step(label: str, action) -> None:
    """Run one teardown step without letting it skip the ones after it.

    Pressing Ctrl+C again while a session is being torn down used to abort the
    rest of the cleanup, which left the P2P connection up and the GO intent
    still lowered, so the next run needed a NetworkManager restart (#86).
    """
    try:
        action()
    except KeyboardInterrupt:
        print(f"[FluxCast WFD] Interrupted during {label}; finishing cleanup anyway.")
    except Exception as exc:
        print(f"[FluxCast WFD] Cleanup step '{label}' failed: {exc}")

def start_experimental_backend(args) -> None:
    report = run_diagnostics(skip_firewall=getattr(args, "wfd_no_firewall", False))
    print_report(report)
    print()

    if not report.wfd_candidate:
        raise WFDNotReady(
            "Miracast/WFD is not ready on this machine yet. "
            "Fix the warn/fail rows above, then run --wfd-scan."
        )

    monitor = None
    if not getattr(args, "wfd_test_pattern", False) and not getattr(args, "wfd_dry_run", False):
        selected_backend = getattr(args, "wfd_capture_backend", "auto")
        portal_mode = selected_backend == "portal" or (
            selected_backend == "auto" and _is_wayland_session() and not _is_hyprland_session()
        )
        if portal_mode:
            print(
                "[FluxCast WFD] Portal backend: monitor selection will be done "
                "in the desktop portal dialog."
            )
        else:
            wfd_monitor_name = getattr(args, "monitor_name", None)
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

    _set_p2p_device_name(args.wfd_interface)
    peer = _scan_and_select(
        args.wfd_interface, getattr(args, "wfd_peer", None), args.wfd_timeout
    )
    device_path = _nm_p2p_device_path(args.wfd_interface)
    if not device_path:
        raise WFDNotReady("NetworkManager P2P device disappeared before connection.")

    if getattr(args, "wfd_dry_run", False):
        _connect_peer(
            device_path,
            peer,
            rtsp_port=getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT),
            dry_run=True,
        )
        return

    no_audio = getattr(args, "wfd_no_audio", False)
    if getattr(args, "wfd_test_pattern", False):
        if no_audio:
            print("[FluxCast WFD] Test pattern smoke mode is video-only (--wfd-no-audio).")
        else:
            print("[FluxCast WFD] Test pattern smoke mode includes AAC audio.")

    media_config = WFDMediaConfig(
        monitor=monitor,
        fps=args.fps,
        bitrate=args.bitrate,
        output_resolution=args.output_res,
        audio_device=getattr(args, "wfd_audio_device", None),
        no_audio=no_audio,
        test_pattern=getattr(args, "wfd_test_pattern", False),
        ffmpeg_stats=getattr(args, "wfd_ffmpeg_stats", False),
        source_port=getattr(args, "wfd_rtp_source_port", 19002),
        media_pipeline=getattr(args, "wfd_media_pipeline", "auto"),
        latency_log_path=getattr(args, "wfd_latency_log", None),
        capture_backend=getattr(args, "wfd_capture_backend", "auto"),
        peer_name=peer.name,
        uibc=getattr(args, "wfd_uibc", False),
        aosp_pmt_pid=getattr(args, "wfd_aosp_pmt_pid", False),
        dump_ts_path=getattr(args, "wfd_dump_ts", None),
    )
    if media_config.dump_ts_path:
        print(f"[FluxCast WFD] Dumping transmitted MPEG-TS to: {media_config.dump_ts_path}")
    if media_config.latency_log_path:
        print(f"[FluxCast WFD] Latency log file: {media_config.latency_log_path}")

    rtsp_port = getattr(args, "wfd_rtsp_port", WFD_RTSP_PORT)
    rtsp = WFDRTSPServer(
        media_config=media_config,
        port=rtsp_port,
    )
    firewall_opened = False
    uibc_firewall_opened = False
    active_path = ""
    previous_go_intent = None
    p2p_backend = getattr(args, "wfd_p2p_backend", "nm")
    wpas_data_iface = None
    try:
        # Clear stale P2P device state from previous runs before new activation.
        try:
            _disconnect_device(device_path)
        except Exception:
            pass
        rtsp.start()
        if p2p_backend == "wpas":
            # Bypasses NetworkManager's AddAndActivateConnection2 entirely -
            # see wpas.py's module docstring for why. connect_via_wpa_supplicant
            # handles GO-intent lowering internally, so it isn't done here.
            wpas_data_iface = connect_via_wpa_supplicant(
                args.wfd_interface, peer.address,
                go_intent=getattr(args, "wfd_go_intent", 0),
                rtsp_port=rtsp_port,
                p2p_channel=getattr(args, "wfd_p2p_channel", None),
            )
        else:
            # Lower our GO intent before negotiation so the TV becomes the group
            # owner; most Miracast sinks only start the RTSP session in that role.
            previous_go_intent = _set_p2p_go_intent(
                args.wfd_interface, getattr(args, "wfd_go_intent", 0)
            )
            active_path = _connect_peer(
                device_path,
                peer,
                rtsp_port=rtsp_port,
            )
            _wait_for_nm_activation(active_path)

        if not getattr(args, "wfd_no_firewall", False):
            firewall_opened = _open_wfd_firewall_port(rtsp_port)
            if getattr(args, "wfd_uibc", False):
                uibc_firewall_opened = _open_wfd_firewall_port(WFD_UIBC_PORT)

        # Active probe for newer TVs (Samsung 2024++, some LGs)
        # It runs in a background thread to not block the main loop.
        probe_thread = threading.Thread(
            target=_active_rtsp_probe,
            args=(rtsp, peer, media_config),
            daemon=True,
        )
        probe_thread.start()

        print("[FluxCast WFD] Waiting for TV RTSP/WFD session. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[FluxCast WFD] Stopping WFD session...")
    finally:
        _cleanup_step("media shutdown", rtsp.stop_all_media)
        _cleanup_step("TS dump report",
                      lambda: report_ts_dump(media_config.dump_ts_path))
        _cleanup_step("RTSP server shutdown", rtsp.stop)
        if firewall_opened:
            _cleanup_step("firewall close", lambda: _close_wfd_firewall_port(rtsp_port))
        if uibc_firewall_opened:
            _cleanup_step("UIBC firewall close", lambda: _close_wfd_firewall_port(WFD_UIBC_PORT))
        if active_path:
            _cleanup_step("connection deactivate",
                          lambda: _deactivate_connection(active_path))
        if wpas_data_iface:
            _cleanup_step(
                "wpa_supplicant connection release",
                lambda: release_wpa_supplicant_connection(
                    args.wfd_interface, wpas_data_iface
                ),
            )
        _cleanup_step("P2P device disconnect", lambda: _disconnect_device(device_path))
        if previous_go_intent is not None:
            _cleanup_step(
                "GO intent restore",
                lambda: _set_p2p_go_intent(
                    args.wfd_interface, previous_go_intent, restoring=True
                ),
            )
