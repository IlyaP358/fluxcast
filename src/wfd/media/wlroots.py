import os
import shutil
import subprocess
import threading
import time

from ..config import WFDNotReady
from ..wf_recorder import find_wf_recorder, wf_recorder_supports_icc
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _kbits_to_bitrate_text, _letterbox_vf,
    _parse_resolution, _quality_floor_kbits, _vbv_bufsize,
)
from ..env import _detect_audio_monitor
from ..hw_encode import (
    apply_bitrate_plan,
    build_encode_plan,
    capture_encode_attempts,
    capture_encode_mode,
    capture_encode_preference,
    prefer_wf_recorder_vaapi_dmabuf,
)
from ..power_plan import active_power_plan, encode_throttled
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
                # LPCM-only sinks cannot play AAC from the plain pipe mux; use
                # annex-B + WFDLPCMMuxer while keeping the raw NV12 pipe encode.
                if getattr(self.config, "prefer_lpcm", False) and not self.config.no_audio:
                    plan_name = self._start_wf_recorder_raw_pipe_lpcm(
                        wf_recorder, monitor, encoder_override=encoder
                    )
                else:
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
        plan = active_power_plan()
        throttled = encode_throttled(plan)
        if throttled:
            effective_kbits = requested_kbits
        else:
            effective_kbits = max(requested_kbits, floor_kbits)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        effective_bitrate = apply_bitrate_plan(effective_bitrate, throttled=throttled)
        if effective_kbits > requested_kbits and not throttled:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )
        return {
            "src_res": src_res,
            "out_res": out_res,
            "gop": gop,
            "parsed_out": parsed_out,
            "power_plan": plan.id,
            "power_plan_name": plan.name,
            "throttled": throttled,
            # Deprecated alias for older log formatters.
            "bias": plan.id,
            "effective_bitrate": effective_bitrate,
            "effective_kbits": _bitrate_to_kbits(effective_bitrate),
            "level": _h264_level_for_mode(self.config),
            "vf_scale": None if out_res == src_res else _letterbox_vf(out_res),
        }

    def _wf_damage_flag(self) -> list[str]:
        # Default: -D (continuous). FLUXCAST_WFD_WF_RECORDER_DAMAGE=1 omits -D.
        damage_aware = os.environ.get("FLUXCAST_WFD_WF_RECORDER_DAMAGE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        return [] if damage_aware else ["-D"]

    def _wf_capture_rate_args(self, wf_recorder: str) -> list[str]:
        """Capture cadence flags for this wf-recorder build.

        Stock wlr-screencopy DMA: omit ``-r`` (it appends ``fps=N`` after
        ``scale_vaapi`` and forces VAAPI→software conversion).

        ICC (ext-image-copy-capture): pass ``-r`` as the client request rate
        (defaults to 60 without it). Only when the binary advertises ICC.
        """
        if wf_recorder_supports_icc(wf_recorder):
            # -r matches config.fps (encode framerate). Override:
            # FLUXCAST_WFD_ICC_CAPTURE_FPS.
            icc_fps = (os.environ.get("FLUXCAST_WFD_ICC_CAPTURE_FPS") or "").strip()
            if not icc_fps:
                try:
                    icc_fps = str(int(self.config.fps))
                except (TypeError, ValueError, AttributeError):
                    icc_fps = "30"
            return ["-r", icc_fps]
        return []

    def _maybe_rebind_for_impairment(self, reason: str) -> None:
        """Restart desktop capture after DTS/pool spikes (debounced)."""
        now = time.monotonic()
        last = float(getattr(self, "_last_impairment_rebind", 0.0) or 0.0)
        if now - last < 20.0:
            return
        if getattr(self, "restarting", False):
            return
        self._last_impairment_rebind = now
        print(
            f"[FluxCast WFD Media] Auto-rebind after {reason} "
            "(buffer-pool / DTS stutter recovery)",
            flush=True,
        )
        try:
            self.restart_video()
        except Exception as exc:
            print(f"[FluxCast WFD Media] Auto-rebind failed: {exc}", flush=True)

    def _watch_sender_stderr(self, proc: subprocess.Popen, label: str) -> None:
        """Relay sender stderr and trigger rebind on known impairment lines."""

        def _run() -> None:
            if proc.stderr is None:
                return
            pool_hits = 0
            window_start = time.monotonic()
            try:
                for raw in iter(proc.stderr.readline, b""):
                    try:
                        line = raw.decode(errors="replace").rstrip()
                    except Exception:
                        continue
                    if line:
                        print(line, flush=True)
                    now = time.monotonic()
                    if now - window_start > 5.0:
                        pool_hits = 0
                        window_start = now
                    low = line.lower()
                    if "non monotonically" in low or "non-monotonically" in low:
                        self._maybe_rebind_for_impairment("dts")
                    elif "buffer pool full" in low:
                        pool_hits += 1
                        if pool_hits >= 2:
                            self._maybe_rebind_for_impairment("buffer-pool")
            except Exception:
                pass
            finally:
                try:
                    proc.stderr.close()
                except Exception:
                    pass

        threading.Thread(
            target=_run, name=f"fluxcast-{label}-stderr", daemon=True
        ).start()

    def _vaapi_rc_wf_params(self, meta: dict) -> tuple[list[str], str]:
        """Build wf-recorder ``-p`` rate-control args for h264_vaapi.

        Env:
          FLUXCAST_WFD_VAAPI_RC — CQP (default), CBR, or VBR
          FLUXCAST_WFD_VAAPI_QP — CQP quantizer (default 18)
          FLUXCAST_WFD_VAAPI_BITRATE — CBR/VBR target (e.g. 12M); falls back
            to the desktop bitrate plan
          FLUXCAST_WFD_VAAPI_GOP — GOP frames (default = stream fps ≈ 1s)
        """
        rc = (os.environ.get("FLUXCAST_WFD_VAAPI_RC", "") or "CQP").strip().upper()
        if rc not in ("CQP", "CBR", "VBR", "AVBR", "QVBR", "ICQ"):
            rc = "CQP"
        quality = (os.environ.get("FLUXCAST_WFD_VAAPI_QUALITY", "") or "4").strip() or "4"
        _gop_env = (os.environ.get("FLUXCAST_WFD_VAAPI_GOP", "") or "").strip()
        try:
            gop = max(1, int(_gop_env)) if _gop_env else max(meta["gop"], int(self.config.fps))
        except (TypeError, ValueError):
            gop = max(meta["gop"], int(self.config.fps))

        if rc == "CQP":
            qp = (os.environ.get("FLUXCAST_WFD_VAAPI_QP", "") or "18").strip() or "18"
            params = [
                "-p", f"rc_mode={rc}",
                "-p", f"qp={qp}",
                "-p", f"gop_size={gop}",
                "-p", f"quality={quality}",
                "-p", "bf=0",
                "-p", "profile=constrained_baseline",
                "-p", f"framerate={self.config.fps}",
            ]
            desc = f"{rc} qp={qp}, gop={gop}, quality={quality}"
        else:
            # Target bitrate: stream/config bitrate; VAAPI_BITRATE is the peak
            # cap for QVBR/VBR (matches pipe ffmpeg -b:v / -maxrate split).
            # Always set maxrate — without it, busy scenes can exceed a 20 MHz
            # Miracast P2P budget (~12 Mbps reliable peak).
            target = (os.environ.get("FLUXCAST_WFD_BITRATE", "") or "").strip()
            if not target:
                target = meta.get("effective_bitrate") or self.config.bitrate or "10M"
            peak = (os.environ.get("FLUXCAST_WFD_VAAPI_BITRATE", "") or "").strip()
            if not peak:
                peak = target
            # AVOption name is ``b`` (bits/s), not ``bitrate``. Set before
            # rc_mode so CBR validation sees a target.
            br_bits = _bitrate_to_kbits(target) * 1000
            peak_bits = _bitrate_to_kbits(peak) * 1000
            if peak_bits < br_bits:
                peak_bits = br_bits
                peak = target
            params = [
                "-p", f"b={br_bits}",
                "-p", f"rc_mode={rc}",
                "-p", f"gop_size={gop}",
                "-p", f"quality={quality}",
                "-p", "bf=0",
                "-p", "profile=constrained_baseline",
                "-p", f"framerate={self.config.fps}",
            ]
            if rc == "QVBR":
                qp = (os.environ.get("FLUXCAST_WFD_VAAPI_QP", "") or "18").strip() or "18"
                params.extend(["-p", f"qp={qp}", "-p", f"maxrate={peak_bits}"])
                desc = (
                    f"{rc} qp={qp} b={target} max={peak}, gop={gop}, quality={quality}"
                )
            elif rc in ("VBR", "AVBR"):
                params.extend(["-p", f"maxrate={peak_bits}"])
                desc = f"{rc} b={target} max={peak}, gop={gop}, quality={quality}"
            elif rc == "CBR":
                # HRD: maxrate == target.
                params.extend(["-p", f"maxrate={br_bits}"])
                desc = f"{rc} bitrate={target}, gop={gop}, quality={quality}"
            else:
                desc = f"{rc} bitrate={target}, gop={gop}, quality={quality}"
        return params, desc

    def _start_wf_recorder_lpcm(self, wf_recorder: str, monitor) -> None:
        """DMA-BUF H.264 + WFD LPCM mux for LPCM-only sinks (e.g. many TVs).

        ffmpeg mpegtsmux cannot emit WFD stream_type 0x83; reuse WFDLPCMMuxer
        with wf-recorder annex-B video and Pulse *sink monitor* PCM (same
        device string as ``wf-recorder --audio=``).
        """
        meta = self._desktop_bitrate_plan(monitor)
        rc_params, rc_desc = self._vaapi_rc_wf_params(meta)
        audio_monitor = self._normalize_sink_monitor(
            self.config.audio_device or _detect_audio_monitor() or ""
        )

        try:
            from drivers.wfd_lpcm_mux import WFDLPCMMuxer
        except ImportError as exc:
            raise WFDNotReady(f"WFDLPCMMuxer unavailable: {exc}") from exc

        out_w, out_h = meta["parsed_out"]
        device = os.environ.get("FLUXCAST_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"
        if meta["out_res"] != meta["src_res"]:
            vf = f"scale_vaapi=w={out_w}:h={out_h}:format=nv12:out_range=tv"
        else:
            vf = "scale_vaapi=format=nv12:out_range=tv"

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        os.set_inheritable(w_fd, True)
        # Enlarge pipe capacity so a slow muxer does not block the recorder.
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
            "(sink monitor via pw-cat — not mic)"
        )
        print(
            "[FluxCast WFD Media] Video encoder   : h264_vaapi DMA-BUF + "
            f"WFDLPCMMuxer (stream_type=0x83, {rc_desc})"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        # Bind RTP before spawning capture (fail cleanly without orphans).
        muxer = WFDLPCMMuxer(
            self.tv_ip,
            self.sink_rtp_port,
            local_ip=self.local_ip,
            local_port=self.config.source_port,
        )

        # appsink: max-buffers=1 drop=true (drop whole AUs; no intermediate queue).
        vid_pipeline = (
            f"fdsrc fd={r_fd} do-timestamp=true ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink sync=false max-buffers=1 drop=true"
        )
        # pw-cat → S16LE pipe; convert to S16BE for WFD LPCM.
        aud_pipeline = (
            f"fdsrc fd={ar_fd} do-timestamp=true ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=2,"
            "layout=interleaved ! "
            "audioconvert ! audio/x-raw,format=S16BE ! "
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

        # Same -D / DAMAGE policy as the DMA paths (_wf_damage_flag).
        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            *self._wf_capture_rate_args(wf_recorder),
            "-o", monitor.name,
            "-c", "h264_vaapi",
            "-d", device,
            "-b", "0",
            "-F", vf,
            *rc_params,
            "-m", "h264",
            "-f", "/dev/stdout",
        ]
        wf_proc = subprocess.Popen(
            wf_cmd,
            stdout=w_fd,
            stderr=subprocess.PIPE,
            pass_fds=(w_fd,),
        )
        os.close(w_fd)
        self._watch_sender_stderr(wf_proc, "wf-recorder-lpcm")

        # pw-cat --target binds the sink .monitor node (S16LE).
        # media.role=Abstract avoids Pulse stream-restore remapping.
        aud_cmd = self._lpcm_audio_capture_cmd(audio_monitor)
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

        Encode flags:
        - scale_vaapi → NV12 with out_range=tv (limited).
        - Stock DMA: omit ``-r``; ICC: ``-r`` via ``_wf_capture_rate_args``.
        - ``bf=0`` + constrained_baseline for Miracast bitstreams.
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        # RC from _vaapi_rc_wf_params (CQP default; CBR via FLUXCAST_WFD_VAAPI_RC).
        rc_params, rc_desc = self._vaapi_rc_wf_params(meta)
        out_w, out_h = meta["parsed_out"]
        device = os.environ.get("FLUXCAST_VAAPI_DEVICE", "").strip() or "/dev/dri/renderD128"

        # out_range=tv (limited) for Miracast sinks.
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
            # -b 0 = max b-frames (not bitrate).
            "-o", monitor.name,
            "-c", "h264_vaapi",
            "-d", device,
            "-b", "0",
            "-F", vf,
            *rc_params,
            "-m", "nut",
            "-f", "/dev/stdout",
        ]
        # AAC in the same nut container as video (single ffmpeg input).
        if not self.config.no_audio and audio_monitor:
            wf_cmd[1:1] = [f"--audio={audio_monitor}", "-C", "aac", "-R", "48000"]

        # Keep demux queue small — 4096 buffered nut packets can add hundreds of
        # ms of glass-to-glass lag. Override with FLUXCAST_WFD_THREAD_QUEUE_SIZE.
        _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "64").strip() or "64"
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-fflags", "+genpts",
            "-thread_queue_size", _tqs,
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
            f"DMA-BUF on {device} ({meta['power_plan']} / {meta['power_plan_name']}, "
            f"{rc_desc}, tv-range, no-bframes, proto={proto})"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)

    def _normalize_sink_monitor(self, audio_monitor: str) -> str:
        """Require a sink ``.monitor`` node (not a mic / default source)."""
        am = (audio_monitor or "").strip()
        if not am:
            raise WFDNotReady("LPCM path requires a Pulse/PipeWire audio monitor")
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
        return am

    def _lpcm_audio_capture_cmd(self, audio_monitor: str) -> list[str]:
        """pw-cat (preferred) or parec → S16LE for WFDLPCMMuxer."""
        if shutil.which("pw-cat") is not None:
            return [
                "pw-cat",
                "-r",
                "-a",
                "--target", audio_monitor,
                "--rate", "48000",
                "--channels", "2",
                "--format", "s16",
                "-P", "application.name=fluxcast-wfd-capture",
                "-P", f"media.name={audio_monitor}",
                "-P", "media.role=Abstract",
                "-P", "media.category=Capture",
                "-P", f"target.object={audio_monitor}",
                "-",
            ]
        return [
            "parec",
            f"--device={audio_monitor}",
            "--client-name=fluxcast-wfd-capture",
            f"--stream-name={audio_monitor}",
            "--rate=48000",
            "--channels=2",
            "--format=s16le",
            "--raw",
            "--property=media.role=Abstract",
            f"--property=module-stream-restore.id=fluxcast-wfd:{audio_monitor}",
        ]

    def _start_wf_recorder_raw_pipe_lpcm(
        self,
        wf_recorder: str,
        monitor,
        encoder_override: str | None = None,
    ) -> str:
        """Raw NV12 pipe → ffmpeg annex-B H.264 + pw-cat PCM → WFDLPCMMuxer.

        Same WFD LPCM (stream_type 0x83) contract as DMA-BUF LPCM, but video
        encode stays on the RENDER ENGINE ``vaapi`` / ``cpu`` pipe path.
        """
        meta = self._desktop_bitrate_plan(monitor)
        audio_monitor = self._normalize_sink_monitor(
            self.config.audio_device or _detect_audio_monitor() or ""
        )
        try:
            from drivers.wfd_lpcm_mux import WFDLPCMMuxer
        except ImportError as exc:
            raise WFDNotReady(f"WFDLPCMMuxer unavailable: {exc}") from exc

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

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        os.set_inheritable(w_fd, True)
        try:
            import fcntl

            pipe_sz = 1 << 20
            fcntl.fcntl(r_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
            fcntl.fcntl(w_fd, fcntl.F_SETPIPE_SZ, pipe_sz)
        except OSError:
            pass
        ar_fd, aw_fd = os.pipe()
        os.set_inheritable(ar_fd, True)
        os.set_inheritable(aw_fd, True)

        pipe_note = (
            f"pipe {input_pix}+hwupload"
            if plan.name in ("vaapi", "qsv")
            else f"pipe {input_pix}+software"
        )
        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        print(
            f"[FluxCast WFD Media] Capturing audio  : {audio_monitor} "
            "(sink monitor via pw-cat — not mic)"
        )
        print(
            f"[FluxCast WFD Media] Video encoder   : {plan.note} [{pipe_note}] + "
            "WFDLPCMMuxer (stream_type=0x83)"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        muxer = WFDLPCMMuxer(
            self.tv_ip,
            self.sink_rtp_port,
            local_ip=self.local_ip,
            local_port=self.config.source_port,
        )
        vid_pipeline = (
            f"fdsrc fd={r_fd} do-timestamp=true ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink sync=false max-buffers=1 drop=true"
        )
        aud_pipeline = (
            f"fdsrc fd={ar_fd} do-timestamp=true ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=2,"
            "layout=interleaved ! "
            "audioconvert ! audio/x-raw,format=S16BE ! "
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

        _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "64").strip() or "64"
        _pipe_ll = (os.environ.get("FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY", "") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if _pipe_ll and _tqs == "64":
            _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "8").strip() or "8"

        # Video-only annex-B into the LPCM muxer (no AAC / rtp_mpegts).
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            *plan.pre_input,
        ]
        if _pipe_ll:
            ffmpeg_cmd += [
                "-fflags", "nobuffer+genpts+flush_packets",
                "-flags", "low_delay",
                "-probesize", "32",
                "-analyzeduration", "0",
            ]
        else:
            ffmpeg_cmd += ["-fflags", "+genpts"]
        ffmpeg_cmd += [
            "-thread_queue_size", _tqs,
            "-f", "nut",
            "-i", "pipe:0",
            "-map", "0:v:0",
            *plan.vf,
            *plan.video_args,
            "-an",
            "-f", "h264",
            f"/dev/fd/{w_fd}",
        ]

        wf_proc = subprocess.Popen(wf_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if wf_proc.stdout is None:
            muxer.stop()
            os.close(r_fd)
            os.close(w_fd)
            os.close(ar_fd)
            os.close(aw_fd)
            raise WFDNotReady("wf-recorder did not expose stdout.")

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=wf_proc.stdout,
            stderr=subprocess.PIPE,
            pass_fds=(w_fd,),
        )
        try:
            wf_proc.stdout.close()
        except Exception:
            pass
        os.close(w_fd)
        self._watch_sender_stderr(wf_proc, "wf-recorder-pipe-lpcm")
        self._watch_sender_stderr(ffmpeg_proc, "ffmpeg-pipe-lpcm")

        aud_cmd = self._lpcm_audio_capture_cmd(audio_monitor)
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
            or ffmpeg_proc.poll() is not None
            or aud_proc.poll() is not None
            or not muxer._mux_thread
            or not muxer._mux_thread.is_alive()
        ):
            muxer.stop()
            for proc in (wf_proc, ffmpeg_proc, aud_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
            os.close(r_fd)
            os.close(ar_fd)
            raise WFDNotReady("WFD pipe LPCM muxer failed to stay up")

        self._lpcm_muxer = muxer
        self.processes = [wf_proc, ffmpeg_proc, aud_proc]
        self._lpcm_video_fd = r_fd
        self._lpcm_audio_fd = ar_fd
        print(
            f"[FluxCast WFD Media] LPCM muxer up (pipe) "
            f"(audio_frames={muxer.audio_frames_sent}, video_frames={muxer.frames_sent})"
        )
        return plan.name

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

        _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "64").strip() or "64"
        # VAAPI pipe low-latency input: skip demux probing / extra buffering.
        # Enable with FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY=1 (test before defaulting).
        _pipe_ll = (os.environ.get("FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY", "") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if _pipe_ll and _tqs == "64":
            _tqs = (os.environ.get("FLUXCAST_WFD_THREAD_QUEUE_SIZE", "") or "8").strip() or "8"
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            *plan.pre_input,
        ]
        if _pipe_ll:
            ffmpeg_cmd += [
                "-fflags", "nobuffer+genpts+flush_packets",
                "-flags", "low_delay",
                "-probesize", "32",
                "-analyzeduration", "0",
            ]
        else:
            ffmpeg_cmd += ["-fflags", "+genpts"]
        ffmpeg_cmd += [
            "-thread_queue_size", _tqs,
            "-f", "nut",
            "-i", "pipe:0",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", _tqs,
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
        """Spawn wf-recorder | ffmpeg with stage timing on the nut pipe."""
        import fcntl

        t_spawn = time.monotonic()
        fps = int(getattr(self.config, "fps", 30) or 30)
        # Rough glass-to-glass budget (ms) for logs — TV display buffer unknown.
        budget = {
            "capture_frame_ms": round(1000.0 / max(fps, 1), 1),
            "encode_budget_ms": round(2 * 1000.0 / max(fps, 1), 1),
            "net_budget_ms": 30.0,
            "tv_display_budget_ms": round(3 * 1000.0 / max(fps, 1), 1),
        }
        budget["sum_excl_queue_ms"] = round(
            budget["capture_frame_ms"]
            + budget["encode_budget_ms"]
            + budget["net_budget_ms"]
            + budget["tv_display_budget_ms"],
            1,
        )
        print(
            "[FluxCast WFD Media] Latency budget (est., excl. demux queue): "
            f"capture≈{budget['capture_frame_ms']}ms + encode≈{budget['encode_budget_ms']}ms "
            f"+ net≈{budget['net_budget_ms']}ms + TV≈{budget['tv_display_budget_ms']}ms "
            f"→ ~{budget['sum_excl_queue_ms']}ms @ {fps}fps",
            flush=True,
        )
        _append_latency_log(
            getattr(self.config, "latency_log_path", None),
            "latency_budget",
            fps=fps,
            **budget,
        )

        wf_proc = subprocess.Popen(wf_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if wf_proc.stdout is None:
            wf_proc.kill()
            raise WFDNotReady("wf-recorder did not expose stdout.")

        # Relay wf→ffmpeg so we can timestamp first captured nut bytes without
        # a large ffmpeg demux backlog owning the pipe.
        r_fd, w_fd = os.pipe()
        try:
            fcntl.fcntl(r_fd, fcntl.F_SETPIPE_SZ, 1 << 20)
            fcntl.fcntl(w_fd, fcntl.F_SETPIPE_SZ, 1 << 20)
        except OSError:
            pass

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdin=r_fd, stderr=subprocess.PIPE, pass_fds=(r_fd,)
        )
        os.close(r_fd)

        first_nut = {"t": None}

        def _relay() -> None:
            try:
                while True:
                    chunk = wf_proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    if first_nut["t"] is None:
                        first_nut["t"] = time.monotonic()
                        dt_ms = round((first_nut["t"] - t_spawn) * 1000.0, 1)
                        print(
                            f"[FluxCast WFD Media] Latency probe: first capture bytes "
                            f"(nut) {dt_ms} ms after sender spawn",
                            flush=True,
                        )
                        _append_latency_log(
                            getattr(self.config, "latency_log_path", None),
                            "first_capture_bytes",
                            ms_after_spawn=dt_ms,
                        )
                    try:
                        os.write(w_fd, chunk)
                    except BrokenPipeError:
                        break
            except Exception:
                pass
            finally:
                try:
                    wf_proc.stdout.close()
                except Exception:
                    pass
                try:
                    os.close(w_fd)
                except Exception:
                    pass

        threading.Thread(target=_relay, name="fluxcast-nut-relay", daemon=True).start()
        self._watch_sender_stderr(wf_proc, "wf-recorder")
        self._watch_sender_stderr(ffmpeg_proc, "ffmpeg")
        time.sleep(1.0)

        if wf_proc.poll() is not None:
            ffmpeg_proc.terminate()
            raise WFDNotReady("wf-recorder exited immediately during WFD streaming.")
        if ffmpeg_proc.poll() is not None:
            wf_proc.terminate()
            raise WFDNotReady("ffmpeg exited immediately during WFD streaming.")

        self.processes = [wf_proc, ffmpeg_proc]
