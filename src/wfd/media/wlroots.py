import os
import shutil
import subprocess
import time

from ..config import WFDNotReady
from ..wf_recorder import find_wf_recorder, wf_recorder_supports_icc
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _kbits_to_bitrate_text, _letterbox_vf,
    _parse_resolution, _quality_floor_kbits, _vbv_bufsize,
)
from ..env import _detect_audio_monitor
from ..hw_encode import (
    apply_bitrate_bias,
    build_encode_plan,
    capture_encode_attempts,
    capture_encode_mode,
    capture_encode_preference,
    power_bias,
    prefer_wf_recorder_vaapi_dmabuf,
    vaapi_quality_for_bias,
)
from ..latency import _append_latency_log
from ..modes import _h264_level_for_mode
from ..net import _ffmpeg_sender_args


class WlrootsMixin:
    def _emit_capture_encode(
        self,
        *,
        capture_path: str,
        encoder: str,
        preference: str,
        fallback: bool,
    ) -> None:
        # Field name capture_path — latency helper already uses positional path=
        # for the log file location.
        _append_latency_log(
            getattr(self.config, "latency_log_path", None),
            "capture_encode",
            capture_path=capture_path,
            encoder=encoder,
            preference=preference,
            fallback=bool(fallback),
        )

    def _start_desktop_wf_recorder(self) -> None:
        wf_recorder = find_wf_recorder()
        if not wf_recorder:
            raise WFDNotReady("wf-recorder is required for WFD desktop streaming.")

        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("wf-recorder backend requires a selected monitor.")

        preference = capture_encode_preference()
        attempts = capture_encode_attempts(preference)
        last_exc: Exception | None = None

        for index, attempt in enumerate(attempts):
            fallback = index > 0
            try:
                if attempt == "dmabuf":
                    if not prefer_wf_recorder_vaapi_dmabuf(monitor):
                        raise WFDNotReady(
                            "DMA-BUF not available (VAAPI missing, scaled deny, "
                            "or RENDER ENGINE is not dmabuf)"
                        )
                    if getattr(self.config, "prefer_lpcm", False) and not self.config.no_audio:
                        self._start_wf_recorder_lpcm(wf_recorder, monitor)
                    else:
                        self._start_wf_recorder_vaapi_dmabuf(wf_recorder, monitor)
                    self._emit_capture_encode(
                        capture_path="dmabuf",
                        encoder="h264_vaapi",
                        preference=preference,
                        fallback=fallback,
                    )
                    if fallback:
                        print(
                            "[FluxCast WFD Media] RENDER ENGINE fell back to "
                            "GPU · DMA-BUF"
                        )
                    return

                encoder = "vaapi" if attempt == "vaapi" else "libx264"
                if attempt == "vaapi" and fallback:
                    print(
                        "[FluxCast WFD Media] DMA-BUF failed; trying GPU · VAAPI "
                        "(pipe hwupload)"
                    )
                elif attempt == "cpu" and fallback:
                    print(
                        "[FluxCast WFD Media] RENDER ENGINE fell back to CPU "
                        "(libx264)"
                    )
                plan_name = self._start_wf_recorder_raw_pipe(
                    wf_recorder, monitor, encoder_override=encoder
                )
                resolved = plan_name or encoder
                if resolved == "vaapi":
                    resolved = "h264_vaapi"
                elif resolved == "qsv":
                    resolved = "h264_qsv"
                self._emit_capture_encode(
                    capture_path="pipe",
                    encoder=resolved,
                    preference=preference,
                    fallback=fallback,
                )
                return
            except WFDNotReady as exc:
                last_exc = exc
                print(
                    f"[FluxCast WFD Media] RENDER ENGINE {attempt} failed ({exc})"
                )
                continue

        if last_exc is not None:
            raise last_exc
        raise WFDNotReady("No RENDER ENGINE capture path succeeded.")

    def _desktop_bitrate_plan(self, monitor):
        src_res = f"{monitor.width}x{monitor.height}"
        out_res = self.config.output_resolution or src_res
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (monitor.width, monitor.height)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        bias = power_bias()
        if bias == "efficient":
            effective_kbits = requested_kbits
        else:
            effective_kbits = max(requested_kbits, floor_kbits)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        effective_bitrate = apply_bitrate_bias(effective_bitrate, bias)
        if effective_kbits > requested_kbits and bias == "full":
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )
        return {
            "src_res": src_res,
            "out_res": out_res,
            "gop": gop,
            "parsed_out": parsed_out,
            "bias": bias,
            "effective_bitrate": effective_bitrate,
            "effective_kbits": _bitrate_to_kbits(effective_bitrate),
            "level": _h264_level_for_mode(self.config),
            "vf_scale": None if out_res == src_res else _letterbox_vf(out_res),
        }

    def _wf_damage_flag(self) -> list[str]:
        # Keep historical wf-recorder -D (continuous / no-damage) by default so
        # existing FluxCast sessions do not change cadence. Opt into damage-
        # aware capture with FLUXCAST_WFD_WF_RECORDER_DAMAGE=1 (omit -D).
        damage_aware = os.environ.get("FLUXCAST_WFD_WF_RECORDER_DAMAGE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        return [] if damage_aware else ["-D"]

    def _wf_capture_rate_args(self, wf_recorder: str) -> list[str]:
        """Capture cadence flags for this wf-recorder build.

        Stock wlr-screencopy DMA: do **not** pass ``-r`` — it appends ``fps=N``
        after ``scale_vaapi`` and forces a VAAPI→software conversion (glitchy).

        ext-image-copy-capture (PR #347): ``-r`` sets the client request rate
        without that fps filter; without it the PR defaults to 60. Pass ``-r``
        only when the binary advertises ICC/toplevel support.
        """
        if wf_recorder_supports_icc(wf_recorder):
            # Prefer 60 Hz capture for interactive latency on the Extend head.
            # Stream/encode may still be 30 fps (VAAPI -p framerate=); capturing
            # faster keeps keystrokes from waiting on a 33 ms capture tick.
            # Override with FLUXCAST_WFD_ICC_CAPTURE_FPS (e.g. 30 to match stream).
            icc_fps = (os.environ.get("FLUXCAST_WFD_ICC_CAPTURE_FPS") or "").strip()
            if not icc_fps:
                icc_fps = "60"
            return ["-r", icc_fps]
        return []

    def _start_wf_recorder_lpcm(self, wf_recorder: str, monitor) -> None:
        """DMA-BUF H.264 + WFD LPCM mux for LPCM-only sinks (e.g. many TVs).

        ffmpeg mpegtsmux cannot emit WFD stream_type 0x83; reuse WFDLPCMMuxer
        with wf-recorder annex-B video and Pulse *sink monitor* PCM (same
        device string as ``wf-recorder --audio=``).
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        if not audio_monitor:
            raise WFDNotReady("LPCM path requires a Pulse/PipeWire audio monitor")
        # Never capture a mic / default source — that loops TV speakers→mic→TV.
        am = audio_monitor.strip()
        if am in ("default", "auto") or "input" in am.lower():
            raise WFDNotReady(
                f"Refusing LPCM capture from {audio_monitor!r}; "
                "need a sink monitor like 'miracast.monitor'"
            )
        if not am.endswith(".monitor"):
            am = f"{am}.monitor"
            print(
                f"[FluxCast WFD Media] Audio device {audio_monitor!r} is not a "
                f"monitor; using {am!r}"
            )
        audio_monitor = am

        try:
            from drivers.wfd_lpcm_mux import WFDLPCMMuxer
        except ImportError as exc:
            raise WFDNotReady(f"WFDLPCMMuxer unavailable: {exc}") from exc

        qp = (os.environ.get("FLUXCAST_WFD_VAAPI_QP", "") or "18").strip() or "18"
        gop = max(meta["gop"], int(self.config.fps) * 2)
        out_w, out_h = meta["parsed_out"]
        device = os.environ.get("FLUXCAST_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"
        if meta["out_res"] != meta["src_res"]:
            vf = f"scale_vaapi=w={out_w}:h={out_h}:format=nv12:out_range=tv"
        else:
            vf = "scale_vaapi=format=nv12:out_range=tv"

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        os.set_inheritable(w_fd, True)
        # Large pipe so a slow muxer never blocks wf-recorder's encode thread
        # (that fills the ICC buffer pool and leaves keystrokes one frame behind).
        try:
            import fcntl

            pipe_sz = 1 << 20  # 1 MiB
            fcntl.fcntl(r_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
            fcntl.fcntl(w_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
        except OSError:
            pass
        ar_fd, aw_fd = os.pipe()
        os.set_inheritable(ar_fd, True)
        os.set_inheritable(aw_fd, True)

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        print(
            f"[FluxCast WFD Media] Capturing audio  : {audio_monitor} "
            "(Pulse sink monitor via ffmpeg — not mic)"
        )
        print(
            "[FluxCast WFD Media] Video encoder   : h264_vaapi DMA-BUF + "
            "WFDLPCMMuxer (stream_type=0x83)"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        # Bind RTP port *before* spawning capture so a bind failure cannot
        # leave orphan processes.
        muxer = WFDLPCMMuxer(
            self.tv_ip,
            self.sink_rtp_port,
            local_ip=self.local_ip,
            local_port=self.config.source_port,
        )

        # No blocking queue before parse — a full queue stalls fdsrc → pipe →
        # wf-recorder encode → ICC pool backup → keystrokes lag until pointer
        # motion. Drop only whole AUs at appsink.
        vid_pipeline = (
            f"fdsrc fd={r_fd} do-timestamp=true ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink sync=false max-buffers=1 drop=true"
        )
        # Pulse monitor via ffmpeg (same device API as AAC/wf-recorder).
        # pipewiresrc target-object=*.monitor is unreliable and can latch onto
        # the default source (mic) → speaker feedback on the TV.
        aud_pipeline = (
            f"fdsrc fd={ar_fd} do-timestamp=true ! "
            "audio/x-raw,format=S16BE,rate=48000,channels=2,"
            "layout=interleaved ! "
            "audiobuffersplit output-buffer-size=1920 ! "
            "appsink name=sink sync=false max-buffers=32 drop=false"
        )

        try:
            muxer.start(vid_pipeline, aud_pipeline)
        except Exception:
            os.close(r_fd)
            os.close(w_fd)
            os.close(ar_fd)
            os.close(aw_fd)
            muxer.stop()
            raise

        # Always continuous (-D) on the LPCM path. Damage-aware ICC capture
        # often skips frames when only terminal glyphs change — keys then
        # appear on the TV only after pointer motion (or a burst of typing)
        # creates "real" damage. Audio still flows during quiet video.
        wf_cmd = [
            wf_recorder,
            "-y",
            "-D",
            *self._wf_capture_rate_args(wf_recorder),
            "-o", monitor.name,
            "-c", "h264_vaapi",
            "-d", device,
            "-b", "0",
            "-F", vf,
            "-p", "rc_mode=CQP",
            "-p", f"qp={qp}",
            "-p", f"gop_size={gop}",
            "-p", "quality=4",
            "-p", "bf=0",
            "-p", "profile=constrained_baseline",
            "-p", f"framerate={self.config.fps}",
            "-m", "h264",
            "-f", "/dev/stdout",
        ]
        wf_proc = subprocess.Popen(
            wf_cmd,
            stdout=w_fd,
            stderr=None,
            pass_fds=(w_fd,),
        )
        os.close(w_fd)

        # Explicit Pulse sink-monitor → raw S16BE (never default source/mic).
        aud_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "pulse",
            "-i", audio_monitor,
            "-f", "s16be",
            "-ac", "2",
            "-ar", "48000",
            "-acodec", "pcm_s16be",
            "-",
        ]
        aud_proc = subprocess.Popen(
            aud_cmd,
            stdout=aw_fd,
            stderr=None,
            pass_fds=(aw_fd,),
        )
        os.close(aw_fd)

        time.sleep(2.0)
        if (
            wf_proc.poll() is not None
            or aud_proc.poll() is not None
            or not muxer._mux_thread
            or not muxer._mux_thread.is_alive()
        ):
            muxer.stop()
            for proc in (wf_proc, aud_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
            os.close(r_fd)
            os.close(ar_fd)
            raise WFDNotReady("WFD LPCM muxer failed to stay up")

        self._lpcm_muxer = muxer
        self.processes.append(wf_proc)
        self.processes.append(aud_proc)
        self._lpcm_video_fd = r_fd
        self._lpcm_audio_fd = ar_fd
        print(
            f"[FluxCast WFD Media] LPCM muxer up "
            f"(audio_frames={muxer.audio_frames_sent}, video_frames={muxer.frames_sent})"
        )

    def _start_wf_recorder_vaapi_dmabuf(self, wf_recorder: str, monitor) -> None:
        """Capture+encode on GPU (DMA-BUF); ffmpeg only remuxes to RTP.

        Colorspace notes (Intel + Hyprland):
        - DMA-BUF frames arrive as RGB/GBR drm primes; scale_vaapi must convert
          to NV12 with **tv/limited** range (full/pc looked glitchy on sinks).
        - Stock builds: do **not** pass ``-r`` (appends ``fps=`` after scale_vaapi).
          ICC builds: pass ``-r`` via ``_wf_capture_rate_args`` for capture cadence.
        - Force ``bf=0`` + constrained_baseline for Miracast-friendly bitstreams
          (default High+bframes produced visible “repeating pixel” artifacts).
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        # DMA encode is GPU-cheap. Prefer constant-quality (CQP) over bitrate
        # modes: on this Intel+wf-recorder DMA path, AVBR/CBR undershoot to a
        # few hundred kb/s on desktop content → blocky text and periodic dips
        # around IDR frames. Wi‑Fi P2P (~70 Mbps) has headroom for CQP peaks.
        # Override QP with FLUXCAST_WFD_VAAPI_QP (lower = sharper, bigger).
        qp = (os.environ.get("FLUXCAST_WFD_VAAPI_QP", "") or "18").strip() or "18"
        quality = "4"  # VAAPI speed/quality tradeoff (lower = slower/better)
        # 2s GOP cuts IDR spikes vs 1s (fps) without hurting Miracast recovery.
        gop = max(meta["gop"], int(self.config.fps) * 2)
        out_w, out_h = meta["parsed_out"]
        device = os.environ.get("FLUXCAST_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"

        # out_range=tv (limited) — not full/pc — for TV/Miracast sinks.
        if meta["out_res"] != meta["src_res"]:
            vf = f"scale_vaapi=w={out_w}:h={out_h}:format=nv12:out_range=tv"
        else:
            vf = "scale_vaapi=format=nv12:out_range=tv"

        icc = wf_recorder_supports_icc(wf_recorder)
        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            *self._wf_capture_rate_args(wf_recorder),
            # -b 0 is max b-frames (not bitrate) — required for Miracast sinks.
            "-o", monitor.name,
            "-c", "h264_vaapi",
            "-d", device,
            "-b", "0",
            "-F", vf,
            "-p", "rc_mode=CQP",
            "-p", f"qp={qp}",
            "-p", f"gop_size={gop}",
            "-p", f"quality={quality}",
            "-p", "bf=0",
            "-p", "profile=constrained_baseline",
            "-p", f"framerate={self.config.fps}",
            "-m", "nut",
            "-f", "/dev/stdout",
        ]
        # Bake AAC into the same nut as video. A second live pulse input in
        # ffmpeg stalls reading pipe:0 and fills wf-recorder's buffer pool.
        if not self.config.no_audio and audio_monitor:
            wf_cmd[1:1] = [f"--audio={audio_monitor}", "-C", "aac", "-R", "48000"]

        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-fflags", "+genpts",
            "-thread_queue_size", "4096",
            "-f", "nut",
            "-i", "pipe:0",
            "-map", "0:v:0",
            "-c:v", "copy",
        ]
        if not self.config.no_audio and audio_monitor:
            ffmpeg_cmd += [
                "-map", "0:a:0",
                "-c:a", "copy",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor} (via wf-recorder)")
        if meta["out_res"] != meta["src_res"]:
            print(f"[FluxCast WFD Media] Scaling output  : {meta['out_res']}")
        proto = "icc" if icc else "wlr-screencopy"
        print(
            f"[FluxCast WFD Media] Video encoder   : h264_vaapi via wf-recorder "
            f"DMA-BUF on {device} ({meta['bias']} power bias, "
            f"CQP qp={qp}, gop={gop}, quality={quality}, tv-range, no-bframes, "
            f"proto={proto})"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)

    def _start_wf_recorder_raw_pipe(
        self,
        wf_recorder: str,
        monitor,
        encoder_override: str | None = None,
    ) -> str:
        """Raw NV12 pipe → ffmpeg encode (VAAPI hwupload or libx264).

        Prefer ``-x nv12`` so we skip CPU ``format=nv12`` before hwupload.
        Falls back to yuv420p + format=nv12 if needed via encode plan.
        Used for RENDER ENGINE ``vaapi`` / ``cpu`` and as DMA-BUF fallback.

        Returns the resolved encode plan name (``vaapi`` / ``qsv`` / ``libx264``).
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        # NV12 from wf-recorder avoids CPU colorspace convert; only hwupload remains.
        input_pix = "nv12"
        plan = build_encode_plan(
            h264_profile=self.config.h264_profile,
            level=meta["level"],
            fps=self.config.fps,
            gop=meta["gop"],
            bitrate=meta["effective_bitrate"],
            bufsize=_vbv_bufsize(meta["effective_bitrate"], self.config),
            vf_scale=meta["vf_scale"],
            output_height=meta["parsed_out"][1],
            input_pix_fmt=input_pix,
            encoder_override=encoder_override,
        )

        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            "-r", str(self.config.fps),
            "-o", monitor.name,
            "-c", "rawvideo",
            "-m", "nut",
            "-x", "nv12",
            "-f", "/dev/stdout",
        ]

        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            *plan.pre_input,
            "-fflags", "+genpts",
            "-thread_queue_size", "4096",
            "-f", "nut",
            "-i", "pipe:0",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", "4096",
                "-f", "pulse",
                "-i", audio_monitor,
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
        else:
            ffmpeg_cmd += ["-map", "0:v:0"]

        ffmpeg_cmd += plan.vf
        ffmpeg_cmd += plan.video_args

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-af", "aresample=async=1",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if meta["out_res"] != meta["src_res"]:
            print(f"[FluxCast WFD Media] Scaling output  : {meta['out_res']}")
        pipe_note = (
            f"pipe {input_pix}+hwupload"
            if plan.name in ("vaapi", "qsv")
            else f"pipe {input_pix}+software"
        )
        print(
            f"[FluxCast WFD Media] Video encoder   : {plan.note} [{pipe_note}]"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)
        return plan.name

    def _spawn_wf_ffmpeg(self, wf_cmd: list[str], ffmpeg_cmd: list[str]) -> None:
        wf_proc = subprocess.Popen(wf_cmd, stdout=subprocess.PIPE, stderr=None)
        if wf_proc.stdout is None:
            wf_proc.kill()
            raise WFDNotReady("wf-recorder did not expose stdout.")

        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=wf_proc.stdout, stderr=None)
        wf_proc.stdout.close()
        time.sleep(1.0)

        if wf_proc.poll() is not None:
            ffmpeg_proc.terminate()
            raise WFDNotReady("wf-recorder exited immediately during WFD streaming.")
        if ffmpeg_proc.poll() is not None:
            wf_proc.terminate()
            raise WFDNotReady("ffmpeg exited immediately during WFD streaming.")

        self.processes = [wf_proc, ffmpeg_proc]
