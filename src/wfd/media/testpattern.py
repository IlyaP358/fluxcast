import shutil
import subprocess
import time

from ..config import WFDNotReady
from ..encoding import (
    _bitrate_to_kbits, _calculate_gop, _parse_resolution, _vbv_bufsize,
)
from ..gst import (
    _gst_dump_branch, _gst_has_element, _gst_pick_aac_encoder, _gst_rtp_link,
    _gst_wfd_sender_available, _wfd_gst_prog_map,
)
from ..modes import _h264_level_for_mode
from ..net import _ffmpeg_sender_args


class TestPatternMixin:
    def _start_test_pattern(self) -> None:
        if not shutil.which("ffmpeg"):
            raise WFDNotReady("ffmpeg is required for WFD test-pattern streaming.")

        resolution = self.config.output_resolution or "1280x720"
        gop = _calculate_gop(self.config)
        _tp_h = (_parse_resolution(resolution) or (1280, 720))[1]
        cmd = [
            *_ffmpeg_sender_args(self.config.ffmpeg_stats),
            "-re",
            "-f", "lavfi",
            "-i", f"testsrc2=size={resolution}:rate={self.config.fps}",
        ]

        if not self.config.no_audio:
            cmd += [
                "-re",
                "-f", "lavfi",
                "-i", "sine=frequency=880:sample_rate=48000",
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
        else:
            cmd += ["-map", "0:v:0"]

        cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast" if _tp_h > 1080 else "veryfast",
            "-tune", "zerolatency",
            "-profile:v", self.config.h264_profile,
            "-level:v", _h264_level_for_mode(self.config),
            "-pix_fmt", "yuv420p",
            "-r", str(self.config.fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-bf", "0",
            "-b:v", self.config.bitrate,
            "-maxrate", self.config.bitrate,
            "-bufsize", _vbv_bufsize(self.config.bitrate, self.config),
            "-x264-params", "repeat-headers=1:aud=1",
        ]

        if not self.config.no_audio:
            cmd += [
                "-af", "aresample=async=1",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "48000",
                "-streamid", "1:4352",
            ]

        cmd += self._common_output_args()

        print(
            f"[FluxCast WFD Media] Starting test RTP stream to "
            f"{self.tv_ip}:{self.sink_rtp_port} from local port {self.config.source_port}"
        )
        proc = subprocess.Popen(cmd)  # stderr/stdout visible for debugging
        time.sleep(0.8)
        if proc.poll() is not None:
            raise WFDNotReady("ffmpeg test-pattern pipeline exited immediately.")
        self.processes = [proc]
    def _start_gst_test_pattern(self) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise WFDNotReady("gst-launch-1.0 is required for the GStreamer WFD test-pattern pipeline.")
        if not _gst_wfd_sender_available():
            raise WFDNotReady(
                "GStreamer WFD test-pattern pipeline needs mpegtsmux, rtpmp2tpay, and x264enc."
            )

        resolution = _parse_resolution(self.config.output_resolution) or (1280, 720)
        width, height = resolution
        bitrate_kbits = _bitrate_to_kbits(self.config.bitrate)
        gop = _calculate_gop(self.config)

        prog_map = _wfd_gst_prog_map(
            not self.config.no_audio, self.config.aosp_pmt_pid
        )

        # Inject in-band SPS/PPS before every IDR, like the portal path and
        # ffmpeg's repeat-headers=1. Without it a sink that starts decoding
        # late never receives the headers and stays black, which also made
        # this smoke mode a misleading control while debugging #84.
        h264_parse_chain = (
            ["!", "h264parse", "config-interval=-1"]
            if _gst_has_element("h264parse") else []
        )

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
            "videotestsrc", "is-live=true", "pattern=smpte",
            "!", f"video/x-raw,width={width},height={height},framerate={self.config.fps}/1",
            "!", "videoconvert",
            "!", "x264enc",
            "tune=zerolatency",
            f"speed-preset={'ultrafast' if height > 1080 else 'veryfast'}",
            f"bitrate={bitrate_kbits}",
            f"key-int-max={gop}",
            "bframes=0",
            "byte-stream=true",
            "aud=true",
            "sliced-threads=true",
            "vbv-buf-capacity=200",
            *h264_parse_chain,
            "!", f"video/x-h264,stream-format=byte-stream,alignment=au,profile={self.config.h264_profile}",
            "!", "queue",
            "!", "mux.sink_4113",
        ]

        if not self.config.no_audio:
            audio_encoder, audio_caps = _gst_pick_aac_encoder()
            cmd += [
                "audiotestsrc", "is-live=true", "wave=sine", "freq=880",
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
            f"[FluxCast WFD Media] Starting GStreamer test RTP stream to "
            f"{self.tv_ip}:{self.sink_rtp_port} from {self.local_ip}:{self.config.source_port}"
        )
        print(f"[FluxCast WFD Media] GST cmd: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd)  # stderr/stdout visible for debugging
        time.sleep(0.8)
        if proc.poll() is not None:
            raise WFDNotReady("GStreamer test-pattern pipeline exited immediately.")
        self.processes = [proc]
