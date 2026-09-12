import os
import shutil
import subprocess
import time

from ..config import WFDNotReady
from ..wf_recorder import find_wf_recorder
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _kbits_to_bitrate_text, _letterbox_vf,
    _parse_resolution, _quality_floor_kbits, _vbv_bufsize,
)
from ..env import _detect_audio_monitor
from ..hw_encode import (
    apply_bitrate_bias,
    build_encode_plan,
    capture_encode_mode,
    power_bias,
    prefer_wf_recorder_vaapi_dmabuf,
    vaapi_quality_for_bias,
)
from ..modes import _h264_level_for_mode
from ..net import _ffmpeg_sender_args


class WlrootsMixin:
    def _start_desktop_wf_recorder(self) -> None:
        wf_recorder = find_wf_recorder()
        if not wf_recorder:
            raise WFDNotReady("wf-recorder is required for WFD desktop streaming.")

        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("wf-recorder backend requires a selected monitor.")

        if prefer_wf_recorder_vaapi_dmabuf(monitor):
            try:
                self._start_wf_recorder_vaapi_dmabuf(wf_recorder, monitor)
                return
            except WFDNotReady as exc:
                print(
                    "[FluxCast WFD Media] wf-recorder VAAPI/DMA-BUF path failed "
                    f"({exc}); falling back to raw pipe + ffmpeg encode"
                )
        else:
            from ..hw_encode import hypr_monitor_scale

            scale = hypr_monitor_scale(str(getattr(monitor, "name", "") or ""))
            if (
                abs(scale - 1.0) > 0.01
                and capture_encode_mode() in ("auto", "vaapi")
                and (os.environ.get("FLUXCAST_WFD_DMABUF_ALLOW_SCALED", "") or "")
                .strip()
                .lower()
                in ("0", "false", "no", "off", "never")
            ):
                print(
                    "[FluxCast WFD Media] Skipping wf-recorder DMA-BUF on scaled "
                    f"output (scale={scale:g}, DMABUF_ALLOW_SCALED denied); "
                    "using pipe hwupload"
                )

        self._start_wf_recorder_raw_pipe(wf_recorder, monitor)

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

    def _start_wf_recorder_vaapi_dmabuf(self, wf_recorder: str, monitor) -> None:
        """Capture+encode on GPU (DMA-BUF); ffmpeg only remuxes to RTP.

        Colorspace notes (Intel + Hyprland):
        - DMA-BUF frames arrive as RGB/GBR drm primes; scale_vaapi must convert
          to NV12 with **tv/limited** range (full/pc looked glitchy on sinks).
        - Do **not** pass ``-r``: wf-recorder appends ``fps=N`` after scale_vaapi,
          which forces a vaapi→software conversion and breaks/glitches the graph.
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

        wf_cmd = [
            wf_recorder,
            "-y",
            *self._wf_damage_flag(),
            # No -r here (see docstring). Framerate comes from encoder params.
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

        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-fflags", "+genpts",
            "-thread_queue_size", "1024",
            "-f", "nut",
            "-i", "pipe:0",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", "1024",
                "-f", "pulse",
                "-i", audio_monitor,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-af", "aresample=async=1",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]
        else:
            ffmpeg_cmd += ["-map", "0:v:0", "-c:v", "copy"]

        ffmpeg_cmd += self._common_output_args()

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({meta['src_res']})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if meta["out_res"] != meta["src_res"]:
            print(f"[FluxCast WFD Media] Scaling output  : {meta['out_res']}")
        print(
            f"[FluxCast WFD Media] Video encoder   : h264_vaapi via wf-recorder "
            f"DMA-BUF on {device} ({meta['bias']} power bias, "
            f"CQP qp={qp}, gop={gop}, quality={quality}, tv-range, no-bframes)"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)

    def _start_wf_recorder_raw_pipe(self, wf_recorder: str, monitor) -> None:
        """Raw NV12 pipe → ffmpeg hwupload → VAAPI encode (glitch-free).

        Prefer ``-x nv12`` so we skip CPU ``format=nv12`` before hwupload.
        Falls back to yuv420p + format=nv12 if needed via encode plan.
        Prefer DMA-BUF when opted in; this pipe path is the fallback (and the
        escape hatch when ``FLUXCAST_WFD_DMABUF_ALLOW_SCALED=0``).
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
            "-thread_queue_size", "1024",
            "-f", "nut",
            "-i", "pipe:0",
        ]

        if not self.config.no_audio:
            ffmpeg_cmd += [
                "-thread_queue_size", "1024",
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
        print(
            f"[FluxCast WFD Media] Video encoder   : {plan.note} "
            f"[pipe {input_pix}+hwupload]"
        )
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        self._spawn_wf_ffmpeg(wf_cmd, ffmpeg_cmd)

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
