import shutil
import subprocess
import time

from capture.portal_capture import (
    PortalCaptureError, close_portal_capture, start_portal_capture,
)
from ..config import WFDNotReady
from ..dump import _process_written_bytes
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _fit_inside, _kbits_to_bitrate_text,
    _parse_resolution, _quality_floor_kbits, _vbv_bufsize,
)
from ..env import _detect_audio_monitor
from ..gst import (
    _gst_dump_branch, _gst_has_element, _gst_pick_aac_encoder,
    _gst_pipewiresrc_properties, _gst_rtp_link, _gst_x264enc_properties,
    _pipewiresrc_selector_attempts, _wfd_gst_prog_map,
)
from ..modes import _h264_level_for_mode
from ..net import _ffmpeg_sender_args


class PortalMixin:
    def _open_portal_session(self, monitor):
        print("[FluxCast WFD Media] Opening portal screen-share dialog (KDE/GNOME Wayland)...")
        try:
            self.portal_session = start_portal_capture(
                timeout=120.0,
                preferred_position=(monitor.x, monitor.y) if monitor is not None else None,
                preferred_size=(monitor.width, monitor.height) if monitor is not None else None,
            )
        except PortalCaptureError as exc:
            raise WFDNotReady(f"portal capture setup failed: {exc}") from exc

        session = self.portal_session
        # source_type: 1=MONITOR, 2=WINDOW, 4=VIRTUAL ("Share virtual screen")
        if session.source_type is not None and session.source_type not in (1, 4):
            close_portal_capture(self.portal_session)
            self.portal_session = None
            raise WFDNotReady(
                "Portal returned a window or camera source. "
                "In the portal picker choose a full monitor or 'Share virtual screen'."
            )
        return session
    def _start_desktop_portal_ffmpeg(self) -> None:
        """
        Portal capture with ffmpeg doing the encode and mux.

        mpegtsmux cannot stamp the PAT/PMT version_number, so sinks that need
        AOSP's value are unreachable through the all-GStreamer portal path
        (#84). Here gst only captures and scales, then hands raw I420 to
        ffmpeg over a pipe, the same shape the wf-recorder backend uses.
        """
        if not shutil.which("gst-launch-1.0"):
            raise WFDNotReady("Portal backend requires gst-launch-1.0 (pipewiresrc pipeline).")
        if not shutil.which("ffmpeg"):
            raise WFDNotReady("ffmpeg is required for the portal->ffmpeg pipeline.")
        missing = [name for name in ("pipewiresrc", "videoconvert", "videoscale", "videorate")
                   if not _gst_has_element(name)]
        if missing:
            raise WFDNotReady(
                "Portal backend is missing required GStreamer elements: " + ", ".join(missing)
            )

        monitor = self.config.monitor
        if self.config.output_resolution:
            out_res = self.config.output_resolution
        elif monitor is not None:
            out_res = f"{monitor.width}x{monitor.height}"
        else:
            out_res = "1920x1080"
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        out_w, out_h = _parse_resolution(out_res) or (1920, 1080)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        effective_kbits = max(requested_kbits,
                              _quality_floor_kbits(out_w, out_h, self.config.fps))
        if "LG" in self.config.peer_name.upper():
            effective_kbits = min(effective_kbits, 4000)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        if effective_kbits > requested_kbits:
            print("[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                  f"{self.config.bitrate} -> {effective_bitrate}")

        session = self._open_portal_session(monitor)
        attempts = _pipewiresrc_selector_attempts(session.pw_node_id,
                                                  stream_label=session.stream_label)

        # Scale to a size we compute ourselves and let ffmpeg add the bars,
        # instead of relying on how videoscale resolves the aspect ratio (#84).
        src_w, src_h = session.size or (out_w, out_h)
        cap_w, cap_h = _fit_inside(src_w, src_h, out_w, out_h)

        props = _gst_pipewiresrc_properties()
        extra_src = [flag for flag, prop in (
            ("max-buffers=64", "max-buffers"),
            ("resend-last=true", "resend-last"),
            ("min-force-user-latency=0", "min-force-user-latency"),
        ) if prop in props]

        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-fflags", "+genpts",
            "-thread_queue_size", "1024",
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-s", f"{cap_w}x{cap_h}",
            "-r", str(self.config.fps),
            "-i", "pipe:0",
        ]
        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", "1024",
                "-f", "pulse", "-i", audio_monitor,
                "-map", "0:v:0", "-map", "1:a:0",
            ]
        else:
            ffmpeg_cmd += ["-map", "0:v:0"]
        if (cap_w, cap_h) != (out_w, out_h):
            ffmpeg_cmd += ["-vf", f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,"
                                  "setsar=1,format=yuv420p"]
        else:
            ffmpeg_cmd += ["-vf", "format=yuv420p"]
        ffmpeg_cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast" if out_h > 1080 else "veryfast",
            "-tune", "zerolatency",
            "-profile:v", self.config.h264_profile,
            "-level:v", _h264_level_for_mode(self.config),
            "-pix_fmt", "yuv420p",
            "-r", str(self.config.fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-bf", "0",
            "-b:v", effective_bitrate,
            "-maxrate", effective_bitrate,
            "-bufsize", _vbv_bufsize(effective_bitrate, self.config),
            "-x264-params", "repeat-headers=1:aud=1",
        ]
        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-af", "aresample=async=1",
                "-c:a", "aac", "-profile:a", "aac_low",
                "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-streamid", "1:4352",
            ]
        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing via portal node : {session.pw_node_id}")
        print(f"[FluxCast WFD Media] Pipeline             : portal->ffmpeg (AOSP TS)")
        source = f"{session.size[0]}x{session.size[1]}" if session.size else "unknown"
        print(f"[FluxCast WFD Media] Portal source        : {source}")
        print(f"[FluxCast WFD Media] Scaling output       : {cap_w}x{cap_h} "
              f"padded to {out_w}x{out_h}")

        errors: list[str] = []
        frame_bytes = cap_w * cap_h * 3 // 2
        # caps=video/x-raw excludes memory:DMABuf, which videoconvert cannot
        # always consume on GNOME.
        memory_variants = [("sysmem", ["!", "capsfilter", "caps=video/x-raw"]),
                           ("any", [])]
        for label, selector in attempts:
            for mem_label, mem_caps in memory_variants:
                gst_cmd = [
                    "gst-launch-1.0", "-q",
                    "pipewiresrc", f"fd={session.pw_fd}", *selector, *extra_src,
                    "do-timestamp=true", "keepalive-time=33",
                    *mem_caps,
                    "!", "queue", "max-size-buffers=64", "leaky=downstream",
                    "!", "videoconvert",
                    "!", "videoscale",
                    "!", "videorate", "skip-to-first=true",
                    # pixel-aspect-ratio=1/1 stops videoscale from encoding the
                    # aspect ratio as PAR instead of actually scaling (#84).
                    "!", f"video/x-raw,format=I420,width={cap_w},height={cap_h},"
                         f"framerate={self.config.fps}/1,pixel-aspect-ratio=1/1",
                    "!", "fdsink", "fd=1", "sync=false",
                ]
                attempt = f"{label}/{mem_label}"
                print(f"[FluxCast WFD Media] Portal attempt       : {attempt}")
                if self._spawn_capture_pipe(gst_cmd, ffmpeg_cmd, errors, attempt,
                                            min_bytes=frame_bytes,
                                            pass_fds=(session.pw_fd,)):
                    return
        raise WFDNotReady(
            "portal capture delivered no video frames (" + "; ".join(errors) + ")"
        )
    def _spawn_capture_pipe(self, producer_cmd, consumer_cmd, errors, label,
                            min_bytes: int = 1, pass_fds: tuple = ()) -> bool:
        """
        Run producer | consumer and confirm the capture really emits frames.

        Interface byte counters are not enough: audio keeps RTP flowing even
        when the capture is dead, which is how a silent portal passed for
        success. /proc/<pid>/io measures the capture process itself (#84).
        """
        # pass_fds is required: subprocess closes every descriptor above
        # stderr, so the portal's PipeWire fd would not exist in the child.
        producer = subprocess.Popen(producer_cmd, stdout=subprocess.PIPE,
                                    pass_fds=pass_fds)
        if producer.stdout is None:
            producer.kill()
            errors.append(f"{label}: capture pipeline exposed no stdout")
            return False
        consumer = subprocess.Popen(consumer_cmd, stdin=producer.stdout)
        producer.stdout.close()
        self.processes = [consumer, producer]

        before = _process_written_bytes(producer.pid)
        reason = ""
        for _ in range(12):
            time.sleep(0.25)
            if producer.poll() is not None:
                reason = "capture pipeline exited"
                break
            if consumer.poll() is not None:
                reason = "encoder exited"
                break
            after = _process_written_bytes(producer.pid)
            if before is None or after is None or after - before >= min_bytes:
                return True
        else:
            after = _process_written_bytes(producer.pid)
            reason = f"capture produced no frames ({after - before} B in 3s)"

        for proc in (consumer, producer):
            if proc.poll() is None:
                proc.terminate()
        self.processes = []
        errors.append(f"{label}: {reason}")
        print(f"[FluxCast WFD Media] Portal attempt {label} failed: {reason}")
        return False
    def _start_desktop_portal(self) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise WFDNotReady("Portal backend requires gst-launch-1.0 (pipewiresrc pipeline).")
        required = (
            "pipewiresrc", "videoconvert", "videoscale",
            "x264enc", "mpegtsmux", "rtpmp2tpay", "udpsink",
        )
        if not self.config.no_audio:
            required += ("pulsesrc", "audioconvert", "audioresample", "aacparse")
        missing = [name for name in required
                   if not _gst_has_element(name)]
        if missing:
            raise WFDNotReady(
                "Portal backend is missing required GStreamer elements: "
                + ", ".join(missing)
            )
        monitor = self.config.monitor
        if self.config.output_resolution:
            out_res = self.config.output_resolution
        elif monitor is not None:
            out_res = f"{monitor.width}x{monitor.height}"
        else:
            out_res = "1920x1080"
        src_res = out_res
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (1920, 1080)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        effective_kbits = max(requested_kbits, floor_kbits)

        is_lg = "LG" in self.config.peer_name.upper()
        if is_lg:
            effective_kbits = min(effective_kbits, 4000)

        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        if effective_kbits > requested_kbits:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )

        session = self._open_portal_session(monitor)

        if session.size is not None:
            src_res = f"{session.size[0]}x{session.size[1]}"
        parsed_src = _parse_resolution(src_res) or (1920, 1080)
        out_dims = _parse_resolution(out_res) or parsed_src
        out_w, out_h = out_dims
        selector_attempts = _pipewiresrc_selector_attempts(
            session.pw_node_id,
            stream_label=session.stream_label,
        )
        bitrate_kbits = _bitrate_to_kbits(effective_bitrate)
        prog_map = _wfd_gst_prog_map(
            not self.config.no_audio, self.config.aosp_pmt_pid
        )
        has_h264parse = _gst_has_element("h264parse")

        def _gst_video_chain(video_caps: str, selector_args: list[str]) -> list[str]:
            # Use more buffers for high-res 1440p capture and move videorate early
            props = _gst_pipewiresrc_properties()
            pipewire_args = [*selector_args]
            if "max-buffers" in props:
                pipewire_args.append("max-buffers=64")
            if "resend-last" in props:
                pipewire_args.append("resend-last=true")
            if "min-force-user-latency" in props:
                pipewire_args.append("min-force-user-latency=0")

            is_lg = "LG" in self.config.peer_name.upper()

            # x264enc configuration
            encoder_args = [
                "tune=zerolatency",
                f"speed-preset={'ultrafast' if parsed_out[1] > 1080 else 'veryfast'}",
                f"bitrate={bitrate_kbits}",
                f"key-int-max={gop}",
                #"intra-refresh=true",
                "threads=0",
                "bframes=0",
                "byte-stream=true",
                "aud=true",
                "sliced-threads=true",
            ]

            # Check x264enc properties once; same gst-plugins-ugly version
            # either has all VBV params or none of them.
            x264_props = _gst_x264enc_properties()

            if "repeat-headers" in x264_props:
                encoder_args.append("repeat-headers=true")

            opt_parts: list[str] = []
            if "option-string" in x264_props:
                opt_parts += ["scenecut=0", f"min-keyint={gop}"]
            else:
                print(
                    "[FluxCast WFD Media] Portal: scenecut cannot be disabled "
                    "(option-string unavailable); update gst-plugins-ugly to fix periodic artifacts."
                )

            if is_lg:
                # Limit VBV rate via GObject/fallback to prevent IDR spikes overflowing LG's buffer.
                lg_vbv: list[str] = []

                if "rc-lookahead" in x264_props:
                    lg_vbv.append("rc-lookahead=0")

                if "vbv-maxrate" in x264_props:
                    lg_vbv.insert(0, f"vbv-maxrate={bitrate_kbits}")
                elif "option-string" in x264_props:
                    opt_parts.append(f"vbv-maxrate={bitrate_kbits}")

                if "vbv-buf-capacity" in x264_props:
                    lg_vbv.append("vbv-buf-capacity=100")
                elif "option-string" in x264_props:
                    opt_parts.append(f"vbv-bufsize={bitrate_kbits // 10}")

                encoder_args += lg_vbv
                if "vbv-maxrate" not in x264_props and "option-string" not in x264_props:
                    print(
                        "[FluxCast WFD Media] LG profile: VBV rate cap unavailable; "
                        "update gst-plugins-ugly for better LG compatibility."
                    )
            else:
                if "vbv-buf-capacity" in x264_props:
                    encoder_args.append("vbv-buf-capacity=200")
                # vbv-maxrate not exposed as GObject property; mirrors ffmpeg's -maxrate.
                if "option-string" in x264_props:
                    opt_parts.append(f"vbv-maxrate={bitrate_kbits}")

            if opt_parts:
                encoder_args.append("option-string=" + ":".join(opt_parts))

            # Inject in-band SPS/PPS before every IDR (mirrors ffmpeg repeat-headers=1).
            h264_parse_chain = ["!", "h264parse", "config-interval=-1"] if has_h264parse else []

            return [
                "pipewiresrc",
                f"fd={session.pw_fd}",
                *pipewire_args,
                "do-timestamp=true",
                "always-copy=false",
                "keepalive-time=33",
                "!", "queue", "max-size-buffers=64", "max-size-time=1000000000", "leaky=downstream",
                "!", "videorate", "skip-to-first=true",
                "!", f"video/x-raw,framerate={self.config.fps}/1",
                "!", "videoconvert",
                "!", "videoscale",
                "!", video_caps,
                "!", "videoconvert",
                "!", "video/x-raw,format=I420",
                "!", "x264enc",
                *encoder_args,
                *h264_parse_chain,
                "!", f"video/x-h264,stream-format=byte-stream,alignment=au,profile={self.config.h264_profile}",
                "!", "queue",
                "!", "mux.sink_4113",
            ]

        gst_audio_chain: list[str] = []
        if not self.config.no_audio:
            audio_encoder, audio_caps = _gst_pick_aac_encoder()
            gst_audio_chain = [
                "pulsesrc", f"device={audio_monitor}", "do-timestamp=true",
                "!", "audioconvert",
                "!", "audioresample",
                "!", *audio_caps,
                "!", audio_encoder, "bitrate=128000",
                "!", "aacparse",
                "!", "queue",
                "!", "mux.sink_4352",
            ]

        def _gst_cmd_for_caps(video_caps: str, selector_args: list[str]) -> list[str]:
            return [
                "gst-launch-1.0", "-e", "-q",
                "mpegtsmux", "name=mux",
                "alignment=7",
                f"prog-map={prog_map}",
                "pat-interval=9000",
                "pmt-interval=9000",
                "pcr-interval=3600",
                *_gst_rtp_link(self.config.dump_ts_path),
                "!", "udpsink",
                f"host={self.tv_ip}",
                f"port={self.sink_rtp_port}",
                f"bind-address={self.local_ip}",
                f"bind-port={self.config.source_port}",
                "sync=false",
                "async=false",
                *_gst_video_chain(video_caps, selector_args),
                *gst_audio_chain,
                *_gst_dump_branch(self.config.dump_ts_path),
            ]

        # pixel-aspect-ratio=1/1 lets videoscale add-borders letterbox
        # instead of stretching a non-16:9 monitor (#84).
        caps_strict = (
            f"video/x-raw,width={out_w},height={out_h},"
            f"framerate={self.config.fps}/1,pixel-aspect-ratio=1/1"
        )

        caps_no_fps = (
            f"video/x-raw,width={out_w},height={out_h},pixel-aspect-ratio=1/1"
        )
        caps_attempts = [
            ("strict", caps_strict),
            ("no-fps", caps_no_fps),
        ]

        print(f"[FluxCast WFD Media] Capturing via portal node : {session.pw_node_id}")
        print(
            "[FluxCast WFD Media] PipeWire selectors      : "
            + ", ".join(name for name, _ in selector_attempts)
        )
        print("[FluxCast WFD Media] Pipeline             : gstreamer (portal->rtp)")
        print(f"[FluxCast WFD Media] Portal source type      : {session.source_type}")
        if session.position and session.size:
            print(
                "[FluxCast WFD Media] Portal source geometry : "
                f"pos={session.position[0]},{session.position[1]} "
                f"size={session.size[0]}x{session.size[1]}"
            )
        if session.stream_label:
            print(f"[FluxCast WFD Media] Portal source id       : {session.stream_label}")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio       : {audio_monitor}")
        if out_dims != parsed_src:
            print(f"[FluxCast WFD Media] Scaling output       : {out_res}")
        print(
            f"[FluxCast WFD Media] RTP target           : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )
        # ====== Microsoft Wireless Display Adapter: LPCM muxer ====================
        # Microsoft adapter requires MPEG-TS stream_type=0x83 (WFD LPCM) with a
        # 4-byte WIDI PES header. GStreamer mpegtsmux hardcodes 0x8b (Blu-ray LPCM)
        # and cannot be changed at runtime, so we use a pure-Python MPEG-TS muxer.
        if "microsoft" in self.config.peer_name.lower() and not self.config.no_audio:
            print("[FluxCast WFD Media] Microsoft adapter detected — using LPCM MPEG-TS muxer")
            try:
                from drivers.wfd_lpcm_mux import WFDLPCMMuxer
            except ImportError as _ie:
                print(f"[FluxCast WFD Media] WFDLPCMMuxer import failed ({_ie}); "
                      "falling back to standard gst-launch pipeline (no LPCM audio)")
            else:
                for selector_name, selector_args in selector_attempts:
                    for attempt_name, attempt_caps in caps_attempts:
                        print(
                            f"[FluxCast WFD Media] LPCM muxer attempt   : "
                            f"selector={selector_name}, caps={attempt_name}"
                        )

                        vid_chain = _gst_video_chain(attempt_caps, selector_args)
                        vid_chain[-1] = "appsink name=sink sync=false"
                        vid_pipeline = " ".join(vid_chain)

                        aud_pipeline = (
                            f"pulsesrc device={audio_monitor} do-timestamp=true ! "
                            "audioconvert ! audioresample ! "
                            "audio/x-raw,format=S16BE,rate=48000,channels=2,"
                            "layout=interleaved ! appsink name=sink sync=false"
                        )

                        muxer = WFDLPCMMuxer(self.tv_ip, self.sink_rtp_port)
                        try:
                            muxer.start(vid_pipeline, aud_pipeline)
                        except Exception as exc:
                            print(
                                f"[FluxCast WFD Media] LPCM muxer attempt failed ({exc}); "
                                "trying next combination..."
                            )
                            continue
                        # Brief probe: give GStreamer a moment then check mux thread alive
                        time.sleep(2.5)
                        if not muxer._mux_thread or not muxer._mux_thread.is_alive():
                            print(
                                "[FluxCast WFD Media] LPCM muxer thread died; "
                                "trying next combination..."
                            )
                            muxer.stop()
                            continue

                        time.sleep(3.0)
                        if not muxer._mux_thread.is_alive():
                            print(
                                "[FluxCast WFD Media] LPCM muxer died during TX probe; "
                                "trying next combination..."
                            )
                            muxer.stop()
                            continue

                        self._lpcm_muxer = muxer
                        self._portal_pw_fd = session.pw_fd
                        print("[FluxCast WFD Media] LPCM muxer running with MPEG-TS stream_type=0x83")
                        return

                close_portal_capture(self.portal_session)
                self.portal_session = None
                raise WFDNotReady(
                    "portal LPCM muxer pipeline failed to start for Microsoft adapter."
                )

        gst_proc = None
        probe_alive_seconds = 3.0
        for selector_name, selector_args in selector_attempts:
            for attempt_name, attempt_caps in caps_attempts:
                print(
                    f"[FluxCast WFD Media] Portal attempt       : "
                    f"selector={selector_name}, caps={attempt_name}"
                )
                gst_cmd = _gst_cmd_for_caps(attempt_caps, selector_args)
                gst_proc = subprocess.Popen(gst_cmd, stderr=None, pass_fds=(session.pw_fd,))
                time.sleep(2.5)
                if gst_proc.poll() is not None:
                    print(
                        "[FluxCast WFD Media] Portal attempt failed; trying next "
                        "selector/caps combination..."
                    )
                    continue

                time.sleep(probe_alive_seconds)
                if gst_proc.poll() is not None:
                    print(
                        "[FluxCast WFD Media] Portal attempt died during TX probe; "
                        "trying next selector/caps combination..."
                    )
                    continue

                self.processes = [gst_proc]
                self._portal_gst_cmd = gst_cmd
                self._portal_pw_fd = session.pw_fd
                return

        close_portal_capture(self.portal_session)
        self.portal_session = None
        raise WFDNotReady("portal GStreamer RTP pipeline failed to negotiate formats.")
