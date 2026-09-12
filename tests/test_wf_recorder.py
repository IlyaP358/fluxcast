import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.wf_recorder import find_wf_recorder  # noqa: E402


class FindWfRecorderTest(unittest.TestCase):
    @mock.patch("wfd.wf_recorder.shutil.which", return_value=None)
    def test_missing_recorder(self, _which):
        self.assertIsNone(find_wf_recorder())

    @mock.patch("wfd.wf_recorder.subprocess.run")
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/app/usr/bin/wf-recorder")
    def test_rejects_wrapper_when_system_recorder_is_missing(self, _which, run):
        run.return_value = subprocess.CompletedProcess([], 127, "", "not found")
        self.assertIsNone(find_wf_recorder())

    @mock.patch("wfd.wf_recorder.subprocess.run")
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder")
    def test_accepts_working_recorder(self, _which, run):
        run.return_value = subprocess.CompletedProcess([], 0, "wf-recorder 0.6.0", "")
        self.assertEqual(find_wf_recorder(), "/usr/bin/wf-recorder")

    @mock.patch("wfd.wf_recorder.subprocess.run")
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder")
    def test_accepts_recorder_without_version_flag(self, _which, run):
        # Older builds may not support --version; only exit 127 is a hard reject.
        run.return_value = subprocess.CompletedProcess([], 1, "", "unknown option")
        self.assertEqual(find_wf_recorder(), "/usr/bin/wf-recorder")


class WlrootsDamageFlagTest(unittest.TestCase):
    """Regression: default keeps -D; FLUXCAST_WFD_WF_RECORDER_DAMAGE=1 omits it."""

    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_DAMAGE", None)
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)
        os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE", None)
        os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE_FILE", None)
        os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE_PREF", None)

    def _capture_cmds(self, *, encoder="libx264", capture_encode=None, bias="full"):
        from types import SimpleNamespace

        from wfd.media.wlroots import WlrootsMixin

        captured = {"wf": None, "ffmpeg": None}

        class FakeProc:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.stdout = mock.Mock()
                self.pid = 1

            def poll(self):
                return None

            def terminate(self):
                return None

            def kill(self):
                return None

        def fake_popen(cmd, *args, **kwargs):
            if cmd and cmd[0] == "/usr/bin/wf-recorder":
                captured["wf"] = list(cmd)
            elif cmd and (cmd[0] == "ffmpeg" or str(cmd[0]).endswith("/ffmpeg")):
                captured["ffmpeg"] = list(cmd)
            return FakeProc(cmd, *args, **kwargs)

        class Harness(WlrootsMixin):
            def __init__(self):
                self.config = SimpleNamespace(
                    monitor=SimpleNamespace(name="HEADLESS-2", width=1920, height=1080, scale=1),
                    output_resolution="1920x1080",
                    audio_device=None,
                    no_audio=True,
                    bitrate="4M",
                    fps=30,
                    h264_profile="baseline",
                    ffmpeg_stats=False,
                    source_port=19000,
                    dump_ts_path=None,
                    aosp_pmt_pid=False,
                    peer_name="",
                )
                self.tv_ip = "10.42.0.2"
                self.sink_rtp_port = 50000
                self.processes = []

            def _common_output_args(self):
                return ["-f", "mpegts", f"udp://{self.tv_ip}:{self.sink_rtp_port}"]

        os.environ["FLUXCAST_WFD_ENCODER"] = encoder
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = bias
        if capture_encode is None:
            os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE", None)
        else:
            os.environ["FLUXCAST_WFD_CAPTURE_ENCODE"] = capture_encode
        harness = Harness()
        with mock.patch("wfd.media.wlroots.find_wf_recorder", return_value="/usr/bin/wf-recorder"):
            with mock.patch("wfd.media.wlroots.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("wfd.media.wlroots.time.sleep", return_value=None):
                    with mock.patch("wfd.media.wlroots.prefer_wf_recorder_vaapi_dmabuf") as pref:
                        from wfd import hw_encode
                        pref.side_effect = (
                            lambda monitor=None: hw_encode.prefer_wf_recorder_vaapi_dmabuf(monitor)
                        )
                        harness._start_desktop_wf_recorder()
        return captured

    def _capture_wf_cmd(self):
        return self._capture_cmds()["wf"]

    def test_default_includes_continuous_capture_flag(self):
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_DAMAGE", None)
        wf_cmd = self._capture_wf_cmd()
        self.assertIn("-D", wf_cmd)

    def test_damage_env_omits_continuous_capture_flag(self):
        os.environ["FLUXCAST_WFD_WF_RECORDER_DAMAGE"] = "1"
        wf_cmd = self._capture_wf_cmd()
        self.assertNotIn("-D", wf_cmd)

    def test_dmabuf_path_uses_wf_recorder_vaapi_and_ffmpeg_copy(self):
        with mock.patch("wfd.hw_encode._vaapi_usable", return_value=True):
            with mock.patch("wfd.hw_encode._requested_gpu_encode", return_value=True):
                with mock.patch("wfd.hw_encode.monitor_scale", return_value=1.0):
                    cmds = self._capture_cmds(encoder="auto", capture_encode="vaapi", bias="efficient")
        wf = " ".join(cmds["wf"])
        self.assertIn("h264_vaapi", cmds["wf"])
        self.assertIn("scale_vaapi=format=nv12:out_range=tv", wf)
        self.assertIn("bf=0", wf)
        self.assertIn("constrained_baseline", wf)
        # DMA uses CQP for steady desktop sharpness (bitrate modes undershoot).
        self.assertIn("rc_mode=CQP", wf)
        self.assertIn("qp=18", wf)
        self.assertIn("quality=4", wf)
        # 2s GOP (fps=30 → 60) to reduce IDR-driven quality dips.
        self.assertIn("gop_size=60", wf)
        self.assertNotIn("-r", cmds["wf"])  # -r appends fps= after vaapi and glitches
        self.assertNotIn("rawvideo", cmds["wf"])
        self.assertEqual(cmds["ffmpeg"][cmds["ffmpeg"].index("-c:v") + 1], "copy")
        self.assertNotIn("hwupload", " ".join(cmds["ffmpeg"]))

    def test_dmabuf_failure_falls_back_to_vaapi_pipe(self):
        """RENDER ENGINE dmabuf → on DMA failure try GPU · VAAPI pipe."""
        import tempfile
        from pathlib import Path
        from wfd.config import WFDNotReady

        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / "capture-encode"
            pref.write_text("dmabuf\n", encoding="utf-8")
            os.environ["FLUXCAST_WFD_CAPTURE_ENCODE_FILE"] = str(pref)
            os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
            with mock.patch("wfd.hw_encode._vaapi_usable", return_value=True):
                with mock.patch("wfd.hw_encode.monitor_scale", return_value=1.0):
                    with mock.patch(
                        "wfd.media.wlroots.WlrootsMixin._start_wf_recorder_vaapi_dmabuf",
                        side_effect=WFDNotReady("dma boom"),
                    ):
                        cmds = self._capture_cmds(encoder="auto", capture_encode=None)
        self.assertIn("rawvideo", cmds["wf"])
        self.assertIn("h264_vaapi", " ".join(cmds["ffmpeg"]))
        self.assertIn("hwupload", " ".join(cmds["ffmpeg"]))

    def test_cpu_preference_uses_libx264_pipe(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / "capture-encode"
            pref.write_text("cpu\n", encoding="utf-8")
            os.environ["FLUXCAST_WFD_CAPTURE_ENCODE_FILE"] = str(pref)
            os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
            with mock.patch("wfd.hw_encode._vaapi_usable", return_value=True):
                cmds = self._capture_cmds(encoder="auto", capture_encode=None)
        self.assertIn("rawvideo", cmds["wf"])
        self.assertIn("libx264", " ".join(cmds["ffmpeg"]))
        self.assertNotIn("h264_vaapi", " ".join(cmds["ffmpeg"]))


if __name__ == "__main__":
    unittest.main()
