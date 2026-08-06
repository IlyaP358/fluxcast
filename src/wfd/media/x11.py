import os
import shutil
import subprocess
import time

from ..config import WFDNotReady
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _kbits_to_bitrate_text, _letterbox_vf,
    _parse_resolution, _quality_floor_kbits, _vbv_bufsize,
)
from ..env import _detect_audio_monitor
from ..gst import (
    _gst_dump_branch, _gst_has_element, _gst_pick_aac_encoder, _gst_rtp_link,
    _wfd_gst_prog_map,
)
from ..modes import _h264_level_for_mode
from ..net import _ffmpeg_sender_args


class X11Mixin:
    def _start_desktop_x11grab(self) -> None:
        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("x11grab backend requires a selected monitor.")
        src_res = f"{monitor.width}x{monitor.height}"
        out_res = self.config.output_resolution or src_res
        audio_monitor = self.config.audio_device or _detect_audio_monitor()
        gop = _calculate_gop(self.config)
        parsed_out = _parse_resolution(out_res) or (monitor.width, monitor.height)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(parsed_out[0], parsed_out[1], self.config.fps)
        effective_kbits = max(requested_kbits, floor_kbits)
        effective_bitrate = _kbits_to_bitrate_text(effective_kbits)
        if effective_kbits > requested_kbits:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {effective_bitrate}"
            )

        display = os.environ.get("DISPLAY", monitor.display or ":0")
        ffmpeg_cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-thread_queue_size", "1024",
            "-f", "x11grab",
            "-framerate", str(self.config.fps),
            "-video_size", src_res,
            "-i", f"{display}+{monitor.x},{monitor.y}",
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

        if out_res == src_res:
            ffmpeg_cmd += ["-vf", "format=yuv420p"]
        else:
            ffmpeg_cmd += ["-vf", _letterbox_vf(out_res)]

        ffmpeg_cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast" if parsed_out[1] > 1080 else "veryfast",
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
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        ffmpeg_cmd += self._common_output_args()

        print(
            "[FluxCast WFD Media] Using x11grab backend for desktop capture "
            f"from {display}+{monitor.x},{monitor.y}"
        )
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        if out_res != src_res:
            print(f"[FluxCast WFD Media] Scaling output  : {out_res}")
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )

        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stderr=None)
        time.sleep(1.0)
        if ffmpeg_proc.poll() is not None:
            raise WFDNotReady("ffmpeg x11grab sender exited immediately during WFD streaming.")
        self.processes = [ffmpeg_proc]
    def _start_desktop_gst_x11(self) -> None:
        """X11 desktop capture using the proven test pattern 
        GStreamer MPEG-TS pipeline (opt-in, fixes #56).
        """
        
        if not shutil.which("gst-launch-1.0"):
            raise WFDNotReady("gst-x11 backend requires gst-launch-1.0.")
        monitor = self.config.monitor
        if monitor is None:
            raise WFDNotReady("gst-x11 backend requires a selected monitor.")

        required = ["ximagesrc", "videoconvert", "videoscale",
                    "x264enc", "mpegtsmux", "rtpmp2tpay", "udpsink"]
        if not self.config.no_audio:
            required += ["pulsesrc", "audioconvert", "audioresample", "aacparse"]
        missing = [name for name in required if not _gst_has_element(name)]
        if missing:
            raise WFDNotReady(
                "gst-x11 backend is missing GStreamer elements: " + ", ".join(missing)
                + " (ximagesrc/videoscale are in gst-plugins-good, x264enc in gst-plugins-ugly)."
            )

        src_w, src_h = monitor.width, monitor.height
        out_res = self.config.output_resolution or f"{src_w}x{src_h}"
        out_w, out_h = _parse_resolution(out_res) or (src_w, src_h)
        gop = _calculate_gop(self.config)
        requested_kbits = _bitrate_to_kbits(self.config.bitrate)
        floor_kbits = _quality_floor_kbits(out_w, out_h, self.config.fps)
        bitrate_kbits = max(requested_kbits, floor_kbits)
        if bitrate_kbits > requested_kbits:
            print(
                "[FluxCast WFD Media] Raising bitrate for desktop clarity: "
                f"{self.config.bitrate} -> {_kbits_to_bitrate_text(bitrate_kbits)}"
            )
        display = os.environ.get("DISPLAY", monitor.display or ":0")
        audio_monitor = self.config.audio_device or _detect_audio_monitor()

        prog_map = _wfd_gst_prog_map(
            not self.config.no_audio, self.config.aosp_pmt_pid
        )

        # identical to the test pipeline, except for the video (ximagesrc) and audio (pulsesrc) sources.
        cmd = [
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
            "ximagesrc",
            f"display-name={display}",
            "use-damage=false",
            "show-pointer=true",
            f"startx={monitor.x}", f"starty={monitor.y}",
            f"endx={monitor.x + src_w - 1}", f"endy={monitor.y + src_h - 1}",
            "!", f"video/x-raw,framerate={self.config.fps}/1",
            "!", "videoconvert",
            "!", "videoscale",
            "!", f"video/x-raw,width={out_w},height={out_h},pixel-aspect-ratio=1/1",
            "!", "videoconvert",
            "!", "video/x-raw,format=I420",
            "!", "x264enc",
            "tune=zerolatency",
            f"speed-preset={'ultrafast' if out_h > 1080 else 'veryfast'}",
            f"bitrate={bitrate_kbits}",
            f"key-int-max={gop}",
            "bframes=0",
            "byte-stream=true",
            "aud=true",
            "sliced-threads=true",
            "vbv-buf-capacity=200",
            "!", f"video/x-h264,stream-format=byte-stream,alignment=au,profile={self.config.h264_profile}",
            "!", "queue",
            "!", "mux.sink_4113",
        ]

        if not self.config.no_audio:
            audio_encoder, audio_caps = _gst_pick_aac_encoder()
            cmd += [
                "pulsesrc", f"device={audio_monitor}", "do-timestamp=true",
                "!", "audioconvert",
                "!", "audioresample",
                "!", *audio_caps,
                "!", audio_encoder, "bitrate=128000",
                "!", "aacparse",
                "!", "queue",
                "!", "mux.sink_4352",
            ]

        cmd += _gst_dump_branch(self.config.dump_ts_path)

        print(
            "[FluxCast WFD Media] Using gst-x11 backend for desktop capture "
            f"from {display}+{monitor.x},{monitor.y} ({src_w}x{src_h})"
        )
        if out_w != src_w or out_h != src_h:
            print(f"[FluxCast WFD Media] Scaling output  : {out_w}x{out_h}")
        if not self.config.no_audio:
            print(f"[FluxCast WFD Media] Capturing audio  : {audio_monitor}")
        print(
            f"[FluxCast WFD Media] RTP target      : "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )
        print(f"[FluxCast WFD Media] GST cmd: {' '.join(cmd)}")

        proc = subprocess.Popen(cmd)
        time.sleep(1.0)
        if proc.poll() is not None:
            raise WFDNotReady("gst-x11 GStreamer pipeline exited immediately during WFD streaming.")
        self.processes = [proc]
