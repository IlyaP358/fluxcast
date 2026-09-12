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
from ..hw_encode import apply_bitrate_bias, build_encode_plan, power_bias
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
        src_res = f"{monitor.width}x{monitor.height}"
        out_res = self.config.output_resolution or src_res
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (monitor.width, monitor.height)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        bias = power_bias()
        # On battery / power-saver, do not force the clarity floor upward.
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

        level = _h264_level_for_mode(self.config)
        vf_scale = None if out_res == src_res else _letterbox_vf(out_res)
        plan = build_encode_plan(
            h264_profile=self.config.h264_profile,
            level=level,
            fps=self.config.fps,
            gop=gop,
            bitrate=effective_bitrate,
            bufsize=_vbv_bufsize(effective_bitrate, self.config),
            vf_scale=vf_scale,
        )

        # Keep historical wf-recorder -D (continuous / no-damage) by default so
        # existing FluxCast sessions do not change cadence. Opt into damage-
        # aware capture with FLUXCAST_WFD_WF_RECORDER_DAMAGE=1 (omit -D; ffmpeg
        # still enforces CFR with -r below).
        damage_aware = os.environ.get("FLUXCAST_WFD_WF_RECORDER_DAMAGE", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        wf_cmd = [
            wf_recorder,
            "-y",
        ]
        if not damage_aware:
            wf_cmd.append("-D")
        wf_cmd += [
            "-r", str(self.config.fps),
            "-o", monitor.name,
            "-c", "rawvideo",
            "-m", "nut",
            "-p", "pix_fmt=yuv420p",
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

        print(f"[FluxCast WFD Media] Capturing screen : {monitor.name} ({src_res})")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if out_res != src_res:
            print(f"[FluxCast WFD Media] Scaling output  : {out_res}")
        print(f"[FluxCast WFD Media] Video encoder   : {plan.note}")
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

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
