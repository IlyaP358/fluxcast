import random
import socketserver
import threading
import time
from dataclasses import replace
from typing import Optional

from ..config import WFDCEAMode, WFDMediaConfig, WFDNotReady, WFDVideoFormat
from ..constants import WFD_AUDIO_AAC, WFD_AUDIO_LPCM_48K, WFD_UIBC_PORT
from ..dump import schedule_ts_dump_report
from ..encoding import _parse_resolution
from ..latency import _append_latency_log
from ..media.pipeline import WFDMediaPipeline
from ..modes import (
    _choose_cea_mode, _encoder_h264_profile, _parse_sink_video_format,
    _selected_video_format,
)
from ..net import _netdev_tx_bytes, _safe_source_port
from .message import (
    RTSPMessage, _parse_parameters, _parse_rtp_ports,
    _parse_transport_client_ports, _read_rtsp_message, _sink_advertises_uibc,
)


class _WFDRTSPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self.local_ip = self.request.getsockname()[0]
        self.next_cseq = 1
        self.pending: dict[str, str] = {}
        self.session_id = str(random.randint(1_000_000, 9_999_999))
        self._write_lock = threading.Lock()
        self._keepalive_active = True
        self.sink_rtp_port: Optional[int] = None
        self.sink_rtcp_port: int = 0
        self.source_rtp_port = self.media_config.source_port
        self.sink_video_format: Optional[WFDVideoFormat] = None
        self.negotiated_no_audio = False
        self.m3_sent = False
        self.media: Optional[WFDMediaPipeline] = None
        self.connected_at = time.monotonic()
        self.play_accepted_at: Optional[float] = None
        self.setup_ms: Optional[float] = None
        self.first_tx_reported = False

        if hasattr(self.server, "parent_server"):
            self.server.parent_server.has_connected_client = True  # type: ignore[attr-defined]

        print(f"[FluxCast WFD RTSP] TV connected from {peer}; local={self.local_ip}")
        _append_latency_log(
            self.media_config.latency_log_path,
            "rtsp_connected",
            peer=peer,
            local_ip=self.local_ip,
        )
        try:
            self._send_m1_options()
            while True:
                msg = _read_rtsp_message(self.rfile)
                if msg is None:
                    print(f"[FluxCast WFD RTSP] TV disconnected from {peer}")
                    return
                self._log_message(msg)
                if msg.is_response:
                    self._handle_response(msg)
                else:
                    self._handle_request(msg)
        except WFDNotReady as exc:
            print(f"[FluxCast WFD RTSP] ERROR: {exc}")
        except OSError as exc:
            print(f"[FluxCast WFD RTSP] Socket closed: {exc}")
        finally:
            self._keepalive_active = False
            self._stop_media()

    @property
    def media_config(self) -> WFDMediaConfig:
        return self.server.media_config  # type: ignore[attr-defined]

    @property
    def rtsp_port(self) -> int:
        return self.server.server_address[1]  # type: ignore[attr-defined]

    def _rtsp_control_uri(self) -> str:
        return "rtsp://localhost/wfd1.0"

    def _rtsp_presentation_uri(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/wfd1.0"

    def _cea_mode(self) -> WFDCEAMode:
        return _choose_cea_mode(self.media_config, self.sink_video_format)

    def _video_format(self) -> str:
        return _selected_video_format(self.media_config, self.sink_video_format)

    def _audio_codecs(self) -> str:
        if self.media_config.no_audio or self.negotiated_no_audio:
            return "none"
        if "microsoft" in self.media_config.peer_name.lower():
            return WFD_AUDIO_LPCM_48K
        return WFD_AUDIO_AAC

    def _send_bytes(self, text: str) -> None:
        with self._write_lock:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

    def _send_request(
        self,
        name: str,
        method: str,
        uri: str,
        headers: Optional[dict[str, str]] = None,
        body: str = "",
    ) -> None:
        cseq = str(self.next_cseq)
        self.next_cseq += 1
        self.pending[cseq] = name

        output = [
            f"{method} {uri} RTSP/1.0",
            f"CSeq: {cseq}",
        ]
        if headers and "Session" in headers:
            output.append(f"Session: {headers['Session']}")
        for key, value in (headers or {}).items():
            if key == "Session":
                continue
            output.append(f"{key}: {value}")
        if body:
            output.append("Content-Type: text/parameters")
            output.append(f"Content-Length: {len(body.encode('utf-8'))}")
        output.append("")
        output.append(body)
        self._send_bytes("\r\n".join(output))
        print(f"[FluxCast WFD RTSP] -> {name}: {method} (CSeq {cseq})")
        if body:
            for line in body.splitlines():
                if line.startswith("wfd_"):
                    print(f"[FluxCast WFD RTSP]   {line}")

    def _send_response(
        self,
        msg: RTSPMessage,
        status: str = "200 OK",
        headers: Optional[dict[str, str]] = None,
        body: str = "",
    ) -> None:
        output = [
            f"RTSP/1.0 {status}",
            f"CSeq: {msg.cseq}",
        ]
        if headers and "Session" in headers:
            output.append(f"Session: {headers['Session']}")
        output.append("Server: FluxCast-WFD/0.1")
        for key, value in (headers or {}).items():
            if key == "Session":
                continue
            output.append(f"{key}: {value}")
        if body:
            output.append("Content-Type: text/parameters")
        output.append(f"Content-Length: {len(body.encode('utf-8'))}")
        output.append("")
        output.append(body)
        self._send_bytes("\r\n".join(output))
        print(f"[FluxCast WFD RTSP] -> response {status} for {msg.method or msg.status}")

    def _send_m1_options(self) -> None:
        self._send_request(
            "M1_OPTIONS",
            "OPTIONS",
            "*",
            headers={"Require": "org.wfa.wfd1.0"},
        )

    def _send_m3_get_parameters(self) -> None:
        if self.m3_sent:
            return
        self.m3_sent = True
        body = (
            "wfd_content_protection\r\n"
            "wfd_video_formats\r\n"
            "wfd_audio_codecs\r\n"
            "wfd_client_rtp_ports\r\n"
        )
        if self.media_config.uibc:
            body += "wfd_uibc_capability\r\n"
        self._send_request(
            "M3_GET_PARAMETER",
            "GET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _send_m4_set_parameters(self) -> None:
        if not self.sink_rtp_port:
            raise WFDNotReady("TV did not provide a valid RTP port in M3.")
        sink_rtcp_port = self.sink_rtcp_port if self.sink_rtcp_port > 0 else 0
        body = (
            "wfd_content_protection: none\r\n"
            f"wfd_video_formats: {self._video_format()}\r\n"
            f"wfd_audio_codecs: {self._audio_codecs()}\r\n"
            f"wfd_presentation_URL: {self._rtsp_presentation_uri()}/streamid=0 none\r\n"
            "wfd_client_rtp_ports: RTP/AVP/UDP;unicast "
            f"{self.sink_rtp_port} {sink_rtcp_port} mode=play\r\n"
        )
        if self.media_config.uibc and getattr(self, "sink_supports_uibc", False):
            from drivers import uibc
            body += (
                f"wfd_uibc_capability: {uibc.build_uibc_capability(WFD_UIBC_PORT)}\r\n"
                "wfd_uibc_setting: enable\r\n"
            )
        self._send_request(
            "M4_SET_PARAMETER",
            "SET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _send_m5_trigger_setup(self) -> None:
        body = "wfd_trigger_method: SETUP\r\n"
        self._send_request(
            "M5_TRIGGER_SETUP",
            "SET_PARAMETER",
            self._rtsp_control_uri(),
            body=body,
        )

    def _handle_response(self, msg: RTSPMessage) -> None:
        name = self.pending.pop(msg.cseq, "UNKNOWN")
        if not msg.status.startswith("200"):
            if name == "M16_KEEPALIVE":
                # LG (or any TV) rejected our keepalive, STOP RESCHEDULING
                # but keep the stream alive
                print(
                    f"[FluxCast WFD RTSP] M16 keepalive rejected: {msg.status} "
                    "— disabling keepalive, stream continues."
                )
                self._keepalive_active = False
                return
            raise WFDNotReady(f"RTSP {name} failed: {msg.start}")

        print(f"[FluxCast WFD RTSP] <- response for {name}: {msg.status}")
        if name == "M3_GET_PARAMETER":
            params = _parse_parameters(msg.body)
            ports = _parse_rtp_ports(params.get("wfd_client_rtp_ports", ""))
            if not ports or ports[0] <= 0:
                raise WFDNotReady(
                    "TV M3 response did not include a usable wfd_client_rtp_ports value."
                )
            self.sink_rtp_port, self.sink_rtcp_port = ports
            self.source_rtp_port = _safe_source_port(
                self.media_config.source_port,
                self.sink_rtp_port,
                self.sink_rtcp_port,
            )
            self.sink_video_format = _parse_sink_video_format(
                params.get("wfd_video_formats", "")
            )
            if self.media_config.uibc:
                self.sink_supports_uibc = _sink_advertises_uibc(params)
                if not self.sink_supports_uibc:
                    print(
                        "[FluxCast WFD RTSP] TV did not advertise UIBC support; "
                        "input back-channel stays disabled for this session."
                    )
            audio = params.get("wfd_audio_codecs", "")
            _is_microsoft = "microsoft" in self.media_config.peer_name.lower()
            if (
                audio
                and not self.media_config.no_audio
                and "AAC" not in audio.upper()
                and not _is_microsoft
            ):
                self.negotiated_no_audio = True
                print(
                    "[FluxCast WFD RTSP] TV did not advertise AAC; "
                    "falling back to video-only WFD."
                )
            if _is_microsoft and audio:
                print(f"[FluxCast WFD RTSP] Microsoft adapter audio caps: {audio}")
            mode = self._cea_mode()
            print(
                f"[FluxCast WFD RTSP] TV RTP port: {self.sink_rtp_port}; "
                f"source port: {self.source_rtp_port}; audio={audio or 'unknown'}"
            )
            print(f"[FluxCast WFD RTSP] Negotiated media mode: {mode.name}")
            print(f"[FluxCast WFD RTSP] Selected video format: {self._video_format()}")
            self._send_m4_set_parameters()
        elif name == "M4_SET_PARAMETER":
            self._send_m5_trigger_setup()

    def _handle_request(self, msg: RTSPMessage) -> None:
        method = msg.method
        if method == "OPTIONS":
            self._send_response(
                msg,
                headers={
                    "Public": (
                        "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, "
                        "GET_PARAMETER, SET_PARAMETER"
                    )
                },
            )
            self._send_m3_get_parameters()
            return

        if method == "GET_PARAMETER":
            requested = msg.body.lower()
            lines = []
            if "wfd_video_formats" in requested:
                lines.append(f"wfd_video_formats: {self._video_format()}\r\n")
            if "wfd_audio_codecs" in requested:
                lines.append(f"wfd_audio_codecs: {self._audio_codecs()}\r\n")
            if "wfd_content_protection" in requested:
                lines.append("wfd_content_protection: none\r\n")
            body = "".join(lines)
            self._send_response(msg, headers=self._session_header(), body=body)
            return

        if method == "SET_PARAMETER":
            if "wfd_idr_request" in msg.body:
                # IDR will arrive naturally within the next keyframe interval (~1s).
                # restart_video() is only meaningful with intra-refresh=true (no IDR
                # frames); with it removed, restarting kills a healthy pipeline.
                print("[FluxCast WFD RTSP] Sink requested IDR; next keyframe satisfies it.")
            self._send_response(msg, headers=self._session_header())
            return

        if method == "SETUP":
            ports = _parse_transport_client_ports(msg.headers.get("transport", ""))
            if ports:
                self.sink_rtp_port, self.sink_rtcp_port = ports
            if not self.sink_rtp_port:
                self.sink_rtp_port = 19000
                self.sink_rtcp_port = 0

            self.source_rtp_port = _safe_source_port(
                self.media_config.source_port,
                self.sink_rtp_port,
                self.sink_rtcp_port,
            )
            source_port = self.source_rtp_port
            if self.sink_rtcp_port:
                transport = (
                    "RTP/AVP/UDP;unicast;"
                    f"client_port={self.sink_rtp_port}-{self.sink_rtcp_port};"
                    f"server_port={source_port}-{source_port + 1}"
                )
            else:
                transport = (
                    "RTP/AVP/UDP;unicast;"
                    f"client_port={self.sink_rtp_port};"
                    f"server_port={source_port}"
                )
            self._send_response(
                msg,
                headers={
                    "Transport": transport,
                    "Session": f"{self.session_id};timeout=30",
                },
            )
            print(f"[FluxCast WFD RTSP] SETUP complete; RTP sink port={self.sink_rtp_port}")
            return

        if method == "PLAY":
            self._send_response(
                msg,
                headers={
                    **self._session_header(),
                    "Range": "npt=now-",
                },
            )
            # Schedule RTSP M16 keepalive NOW, before _start_media() blocks
            # on the portal dialog (8-13 s). LG WebOS resets the TCP connection
            # ~40-45 s after PLAY. Starting the 20 seconds timer here gives a safe
            # 20 second head start regardless of portal dialog speed.
            # Microsoft adapter sends TEARDOWN in response to M16 GET_PARAMETER.
            if "microsoft" not in self.media_config.peer_name.lower():
                self._schedule_rtsp_keepalive(20.0)
            else:
                print("[FluxCast WFD RTSP] Microsoft adapter detected — M16 keepalive disabled.")
            self._start_media()
            return

        if method == "PAUSE":
            self._send_response(msg, headers=self._session_header())
            self._stop_media()
            return

        if method == "TEARDOWN":
            self._send_response(
                msg,
                headers={
                    **self._session_header(),
                    "Connection": "close",
                },
            )
            self._stop_media()
            return

        self._send_response(msg, status="405 Method Not Allowed")

    def _session_header(self) -> dict[str, str]:
        return {"Session": f"{self.session_id};timeout=30"}

    def _start_media(self) -> None:
        if not self.sink_rtp_port:
            raise WFDNotReady("Cannot start media before the TV RTP port is known.")
        if self.media is None:
            mode = self._cea_mode()
            effective_config = replace(
                self.media_config,
                source_port=self.source_rtp_port,
                output_resolution=mode.resolution,
                fps=mode.fps,
                no_audio=self.media_config.no_audio or self.negotiated_no_audio,
                h264_profile=_encoder_h264_profile(self.sink_video_format),
            )
            # Say so when the sink has no mode matching an explicit --output-res,
            # instead of silently streaming something else (#84).
            requested = _parse_resolution(self.media_config.output_resolution)
            if requested is not None and requested != (mode.width, mode.height):
                print(
                    f"[FluxCast WFD RTSP] Requested {requested[0]}x{requested[1]} has no "
                    f"matching WFD mode on this sink; using {mode.name} instead."
                )
            print(
                f"[FluxCast WFD RTSP] Starting media as {mode.name} "
                f"(H.264 {effective_config.h264_profile}); "
                f"RTP source port {self.source_rtp_port}"
            )
            if effective_config.aosp_pmt_pid:
                print("[FluxCast WFD RTSP] AOSP-compatible MPEG-TS requested: PMT 0x0100, PSI version 1 (#84).")
            schedule_ts_dump_report(effective_config.dump_ts_path)
            _append_latency_log(
                self.media_config.latency_log_path,
                "media_starting",
                mode=mode.name,
                tv_ip=self.client_address[0],
                sink_rtp_port=self.sink_rtp_port,
                source_rtp_port=self.source_rtp_port,
            )
            self.media = WFDMediaPipeline(
                effective_config,
                tv_ip=self.client_address[0],
                local_ip=self.local_ip,
                sink_rtp_port=self.sink_rtp_port,
            )
            if hasattr(self.server, "parent_server"):
                self.server.parent_server._register_media(self.media)  # type: ignore[attr-defined]
            self.media.start()
            print("[FluxCast WFD RTSP] PLAY accepted; media stream started.")
            if self.media_config.uibc and getattr(self, "sink_supports_uibc", False):
                self._maybe_start_uibc(mode)
                self._schedule_uibc_enable(1.5)
            self.play_accepted_at = time.monotonic()
            self.setup_ms = round((self.play_accepted_at - self.connected_at) * 1000.0, 1)
            _append_latency_log(
                self.media_config.latency_log_path,
                "play_accepted",
                setup_ms=self.setup_ms,
            )
            self._schedule_probe(0.7)

    def _maybe_start_uibc(self, mode) -> None:
        parent = getattr(self.server, "parent_server", None)
        if parent is None or parent._uibc_server is not None:
            return
        try:
            from drivers import uibc
        except Exception as exc:
            print(f"[FluxCast WFD UIBC] disabled (import failed: {exc})")
            return
        monitor = self.media_config.monitor
        if monitor is not None:
            mon_w, mon_h, mon_x, mon_y = (
                monitor.width, monitor.height, monitor.x, monitor.y,
            )
        else:
            mon_w, mon_h, mon_x, mon_y = mode.width, mode.height, 0, 0
        parent._uibc_server = uibc.start_uibc(
            WFD_UIBC_PORT, mode.width, mode.height, mon_w, mon_h, mon_x, mon_y,
        )
        if parent._uibc_server is not None:
            print(
                f"[FluxCast WFD UIBC] input server listening on port "
                f"{WFD_UIBC_PORT} (sink {mode.width}x{mode.height} -> "
                f"screen {mon_w}x{mon_h}+{mon_x}+{mon_y})"
            )

    def _schedule_uibc_enable(self, delay: float) -> None:
        from drivers import uibc
        uibc.schedule_post_play_enable(self, delay)

    def _schedule_probe(self, delay: float) -> None:
        probe = threading.Timer(delay, self._probe_tx)
        probe.daemon = True
        probe.start()

    def _schedule_rtsp_keepalive(self, delay: float = 25.0) -> None:
        """Schedule the next RTSP M16 GET_PARAMETER keepalive."""
        t = threading.Timer(delay, self._send_rtsp_keepalive)
        t.daemon = True
        t.start()

    def _send_rtsp_keepalive(self) -> None:
        """Send RTSP GET_PARAMETER (M16) on the existing TCP connection."""
        if not self._keepalive_active:
            return
        media = self.media
        # Only stop the chain if processes have already EXITED.
        # If media is None (portal dialog still open), keep sending keepalives.
        if media is not None and not all(p.poll() is None for p in media.processes):
            return
        try:
            self._send_request(
                "M16_KEEPALIVE",
                "GET_PARAMETER",
                self._rtsp_presentation_uri(),
                headers={"Session": f"{self.session_id};timeout=30"},
            )
            print("[FluxCast WFD RTSP] M16 keepalive sent")
            self._schedule_rtsp_keepalive(25.0)
        except OSError:
            pass  # Socket dead -> DONT RESCHEDULE

    def _probe_tx(self) -> None:
        media = self.media
        if media is None:
            return

        states = []
        for proc in media.processes:
            status = "running" if proc.poll() is None else f"exited={proc.returncode}"
            states.append(f"pid={proc.pid}:{status}")

        if states and all(proc.poll() is None for proc in media.processes):
            current = _netdev_tx_bytes(media.tx_interface)
            delta = None
            if media.tx_baseline is not None and current is not None:
                delta = max(0, current - media.tx_baseline)
            if (
                not self.first_tx_reported
                and delta is not None
                and delta > 0
                and self.play_accepted_at is not None
            ):
                self.first_tx_reported = True
                sender_startup_ms = round((time.monotonic() - self.play_accepted_at) * 1000.0, 1)
                print(
                    f"[FluxCast WFD Media] Latency probe: first RTP bytes after PLAY in "
                    f"{sender_startup_ms} ms"
                )
                sender_path_latency_ms = None
                if self.setup_ms is not None:
                    sender_path_latency_ms = round(self.setup_ms + sender_startup_ms, 1)
                    print(
                        "[FluxCast WFD Media] Latency probe: sender-path latency "
                        f"(RTSP connect -> first RTP) {sender_path_latency_ms} ms"
                    )
                _append_latency_log(
                    self.media_config.latency_log_path,
                    "latency_probe",
                    sender_startup_ms=sender_startup_ms,
                    setup_ms=self.setup_ms,
                    sender_path_latency_ms=sender_path_latency_ms,
                )
            print(
                f"[FluxCast WFD Media] Sender health: "
                f"{', '.join(states)}; {media.tx_summary()}"
            )
            _append_latency_log(
                self.media_config.latency_log_path,
                "sender_health",
                processes=states,
                tx_summary=media.tx_summary(),
            )
            self._schedule_probe(5.0)
            return

        detail = ", ".join(states) if states else "no sender process"
        print(
            f"[FluxCast WFD Media] WARNING: RTP sender is not healthy "
            f"({detail}; {media.tx_summary()})"
        )

    def _stop_media(self) -> None:
        if self.media is not None:
            print("[FluxCast WFD Media] Stopping RTP stream...")
            if hasattr(self.server, "parent_server"):
                self.server.parent_server._unregister_media(self.media)  # type: ignore[attr-defined]
            self.media.stop()
            self.media = None

    def _log_message(self, msg: RTSPMessage) -> None:
        arrow = "<- response" if msg.is_response else "<- request"
        print(f"[FluxCast WFD RTSP] {arrow}: {msg.start}")
        for line in msg.raw_headers:
            lower = line.lower()
            if lower.startswith(("cseq:", "transport:", "session:", "content-type:", "content-length:")):
                print(f"[FluxCast WFD RTSP]   {line}")
        if msg.body:
            for line in msg.body.splitlines():
                if line.startswith("wfd_"):
                    print(f"[FluxCast WFD RTSP]   {line}")
