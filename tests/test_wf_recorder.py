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

    def _capture_wf_cmd(self):
        from types import SimpleNamespace

        from wfd.media.wlroots import WlrootsMixin

        captured = {}

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
            return FakeProc(cmd, *args, **kwargs)

        class Harness(WlrootsMixin):
            def __init__(self):
                self.config = SimpleNamespace(
                    monitor=SimpleNamespace(name="HEADLESS-2", width=1920, height=1080),
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

        os.environ["FLUXCAST_WFD_ENCODER"] = "libx264"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        harness = Harness()
        with mock.patch("wfd.media.wlroots.find_wf_recorder", return_value="/usr/bin/wf-recorder"):
            with mock.patch("wfd.media.wlroots.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("wfd.media.wlroots.time.sleep", return_value=None):
                    harness._start_desktop_wf_recorder()
        return captured["wf"]

    def test_default_includes_continuous_capture_flag(self):
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_DAMAGE", None)
        wf_cmd = self._capture_wf_cmd()
        self.assertIn("-D", wf_cmd)

    def test_damage_env_omits_continuous_capture_flag(self):
        os.environ["FLUXCAST_WFD_WF_RECORDER_DAMAGE"] = "1"
        wf_cmd = self._capture_wf_cmd()
        self.assertNotIn("-D", wf_cmd)


if __name__ == "__main__":
    unittest.main()
