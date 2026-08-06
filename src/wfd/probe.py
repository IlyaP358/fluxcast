import random
import socket
import time
from dataclasses import replace
from typing import Optional

from .config import WFDMediaConfig
from .constants import WFD_AUDIO_AAC, WFD_AUDIO_LPCM_48K
from .dump import schedule_ts_dump_report
from .ie import WFDPeer
from .media.pipeline import WFDMediaPipeline
from .modes import (
    _choose_cea_mode, _encoder_h264_profile, _parse_sink_video_format,
    _selected_video_format,
)
from .net import _safe_source_port
from .p2p.addressing import _wait_for_peer_ip
from .rtsp.message import (
    RTSPMessage, _parse_parameters, _parse_rtp_ports,
    _parse_transport_client_ports, _read_rtsp_message,
)
from .rtsp.rtsp_server import WFDRTSPServer


def _active_rtsp_probe(
    rtsp_server: WFDRTSPServer,
    peer: WFDPeer,
    media_config: WFDMediaConfig,
) -> None:
    # 4s: give passive-RTSP TVs a window to connect first, without burning too
    # much of the cca 15 s timeout that some TVs enforce after P2P activation.
    time.sleep(4.0)
    if rtsp_server.has_connected_client:
        return

    print("[FluxCast WFD RTSP] No passive connection; trying Source-initiated RTSP probe...")

    tv_ip = _wait_for_peer_ip(peer.address, timeout=10.0)
    if not tv_ip:
        print(
            f"[FluxCast WFD RTSP] Active probe: TV IP not found for MAC {peer.address} "
            "— ARP table empty; is the P2P link still up?"
        )
        return

    if rtsp_server.has_connected_client:
        return
    
    tv_port = peer.rtsp_port if 0 < peer.rtsp_port <= 65535 else 7236
    print(f"[FluxCast WFD RTSP] Active probe: TV={tv_ip}; connecting to RTSP port {tv_port}...")

    try:
        sock = socket.create_connection((tv_ip, tv_port), timeout=5.0)
    except ConnectionRefusedError:
        print(
            f"[FluxCast WFD RTSP] Active probe: TV port {tv_port} refused "
            "— Sink-only device; waiting for its passive connection to us."
        )
        return
    except OSError as exc:
        print(f"[FluxCast WFD RTSP] Active probe: connect error: {exc}")
        return

    print(f"[FluxCast WFD RTSP] Active probe: connected to TV RTSP at {tv_ip}:{tv_port}")
    sock.settimeout(8.0)
    rfile = sock.makefile("rb")
    wfile = sock.makefile("wb")
    local_ip: str = sock.getsockname()[0]
    local_uri = f"rtsp://{local_ip}:{rtsp_server.port}/wfd1.0"
    session_id = str(random.randint(1_000_000, 9_999_999))

    # Mutable session state shared by the nested helpers below.
    st: dict = {
        "cseq": 1,
        "pending": {},
        "sink_rtp_port": 0,
        "sink_rtcp_port": 0,
        "src_port": media_config.source_port,
        "sink_vfmt": None,
        "no_audio": media_config.no_audio,
    }

    def _send(name: str, method: str, uri: str, hdrs: Optional[dict] = None, body: str = "") -> None:
        cseq = str(st["cseq"])
        st["cseq"] += 1
        st["pending"][cseq] = name
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}"]
        for k, v in (hdrs or {}).items():
            lines.append(f"{k}: {v}")
        if body:
            lines += ["Content-Type: text/parameters", f"Content-Length: {len(body.encode())}"]
        lines += ["", body]
        wfile.write("\r\n".join(lines).encode())
        wfile.flush()
        print(f"[FluxCast WFD RTSP] Active probe -> {name}")

    def _reply(msg: RTSPMessage, status: str = "200 OK", extra: Optional[dict] = None, body: str = "") -> None:
        lines = [f"RTSP/1.0 {status}", f"CSeq: {msg.cseq}", f"Session: {session_id};timeout=30"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        lines += [f"Content-Length: {len(body.encode())}", "", body]
        wfile.write("\r\n".join(lines).encode())
        wfile.flush()

    media: Optional[WFDMediaPipeline] = None
    try:
        with sock:
            _send("M1_OPTIONS", "OPTIONS", "*", {"Require": "org.wfa.wfd1.0"})
            while True:
                msg = _read_rtsp_message(rfile)
                if msg is None:
                    print("[FluxCast WFD RTSP] Active probe: TV closed connection.")
                    break

                if msg.is_response:
                    name = st["pending"].pop(msg.cseq, "UNKNOWN")
                    if not msg.status.startswith("200"):
                        print(f"[FluxCast WFD RTSP] Active probe: {name} failed: {msg.status}")
                        break
                    print(f"[FluxCast WFD RTSP] Active probe <- OK for {name}")

                    if name == "M1_OPTIONS":
                        _send("M3_GET_PARAMETER", "GET_PARAMETER", local_uri,
                              body="wfd_content_protection\r\nwfd_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n")

                    elif name == "M3_GET_PARAMETER":
                        params = _parse_parameters(msg.body)
                        ports = _parse_rtp_ports(params.get("wfd_client_rtp_ports", ""))
                        if not ports or ports[0] <= 0:
                            print("[FluxCast WFD RTSP] Active probe: no valid RTP ports in M3.")
                            break
                        st["sink_rtp_port"], st["sink_rtcp_port"] = ports
                        st["sink_vfmt"] = _parse_sink_video_format(params.get("wfd_video_formats", ""))
                        audio = params.get("wfd_audio_codecs", "")
                        _probe_microsoft = "microsoft" in media_config.peer_name.lower()
                        if (
                            audio
                            and not media_config.no_audio
                            and "AAC" not in audio.upper()
                            and not _probe_microsoft
                        ):
                            st["no_audio"] = True
                        st["src_port"] = _safe_source_port(
                            media_config.source_port, st["sink_rtp_port"], st["sink_rtcp_port"])
                        vfmt = _selected_video_format(media_config, st["sink_vfmt"])
                        if st["no_audio"]:
                            afmt = "none"
                        elif _probe_microsoft:
                            afmt = WFD_AUDIO_LPCM_48K
                        else:
                            afmt = WFD_AUDIO_AAC
                        rtcp = st["sink_rtcp_port"] if st["sink_rtcp_port"] > 0 else 0
                        m4 = (
                            "wfd_content_protection: none\r\n"
                            f"wfd_video_formats: {vfmt}\r\n"
                            f"wfd_audio_codecs: {afmt}\r\n"
                            f"wfd_presentation_URL: {local_uri}/streamid=0 none\r\n"
                            f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast "
                            f"{st['sink_rtp_port']} {rtcp} mode=play\r\n"
                        )
                        _send("M4_SET_PARAMETER", "SET_PARAMETER", local_uri, body=m4)

                    elif name == "M4_SET_PARAMETER":
                        _send("M5_TRIGGER_SETUP", "SET_PARAMETER", local_uri,
                              body="wfd_trigger_method: SETUP\r\n")

                    elif name == "M5_TRIGGER_SETUP":
                        print("[FluxCast WFD RTSP] Active probe: M5 sent — awaiting TV SETUP...")
                        # TV should now send SETUP on this same TCP connection.

                else:
                    method = msg.method
                    print(f"[FluxCast WFD RTSP] Active probe <- TV request: {method}")

                    if method in ("GET_PARAMETER", "SET_PARAMETER"):
                        _reply(msg)

                    elif method == "OPTIONS":
                        _reply(msg, extra={
                            "Public": (
                                "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, "
                                "GET_PARAMETER, SET_PARAMETER"
                            )
                        })

                    elif method == "SETUP":
                        ports = _parse_transport_client_ports(msg.headers.get("transport", ""))
                        if ports:
                            st["sink_rtp_port"], st["sink_rtcp_port"] = ports
                        if not st["sink_rtp_port"]:
                            st["sink_rtp_port"] = 19000
                        st["src_port"] = _safe_source_port(
                            media_config.source_port, st["sink_rtp_port"], st["sink_rtcp_port"])
                        sp = st["src_port"]
                        sr = st["sink_rtp_port"]
                        sc = st["sink_rtcp_port"]
                        transport = (
                            f"RTP/AVP/UDP;unicast;client_port={sr}-{sc};server_port={sp}-{sp + 1}"
                            if sc else
                            f"RTP/AVP/UDP;unicast;client_port={sr};server_port={sp}"
                        )
                        _reply(msg, extra={
                            "Transport": transport,
                            "Session": f"{session_id};timeout=30",
                        })
                        print(f"[FluxCast WFD RTSP] Active probe: SETUP OK; sink RTP={sr}")

                    elif method == "PLAY":
                        _reply(msg, extra={"Range": "npt=now-"})
                        mode = _choose_cea_mode(media_config, st["sink_vfmt"])
                        eff_cfg = replace(
                            media_config,
                            source_port=st["src_port"],
                            output_resolution=mode.resolution,
                            fps=mode.fps,
                            no_audio=st["no_audio"],
                            h264_profile=_encoder_h264_profile(st["sink_vfmt"]),
                        )
                        media = WFDMediaPipeline(
                            eff_cfg,
                            tv_ip=tv_ip,
                            local_ip=local_ip,
                            sink_rtp_port=st["sink_rtp_port"],
                        )
                        rtsp_server.has_connected_client = True
                        print(
                            f"[FluxCast WFD RTSP] Active probe: PLAY — "
                            f"starting media ({mode.name})"
                        )
                        if eff_cfg.aosp_pmt_pid:
                            print("[FluxCast WFD RTSP] AOSP-compatible MPEG-TS requested: PMT 0x0100, PSI version 1 (#84).")
                        media.start()
                        schedule_ts_dump_report(eff_cfg.dump_ts_path)
                        # Keep-alive: respond to GET_PARAMETER/SET_PARAMETER heartbeats.
                        while True:
                            ka = _read_rtsp_message(rfile)
                            if ka is None:
                                break
                            if ka.method in ("GET_PARAMETER", "SET_PARAMETER"):
                                _reply(ka)
                            elif ka.method == "TEARDOWN":
                                _reply(ka, extra={"Connection": "close"})
                                break
                        return

                    elif method == "TEARDOWN":
                        _reply(msg, extra={"Connection": "close"})
                        break

                    else:
                        _reply(msg, status="405 Method Not Allowed")

    except OSError as exc:
        print(f"[FluxCast WFD RTSP] Active probe: I/O error: {exc}")
    finally:
        if media is not None:
            media.stop()
    print("[FluxCast WFD RTSP] Active probe: session ended.")
