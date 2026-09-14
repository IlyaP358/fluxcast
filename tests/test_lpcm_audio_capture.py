"""LPCM path must capture miracast.monitor via pw-cat (not ffmpeg Lavf).

Pulse stream-restore keys on application.name=Lavf* and remapped ffmpeg
-f pulse onto Speakers.monitor — picture OK, TV speakers silent.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class _FakeMuxer:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = None
        self._mux_thread = SimpleNamespace(is_alive=lambda: True)
        self.audio_frames_sent = 0
        self.frames_sent = 0
        self.stopped = False

    def start(self, vid_pipeline: str, aud_pipeline: str) -> None:
        self.started = (vid_pipeline, aud_pipeline)

    def stop(self) -> None:
        self.stopped = True


class _FakeProc:
    def __init__(self, cmd, *args, **kwargs):
        import io

        self.cmd = list(cmd)
        self.args = args
        self.kwargs = kwargs
        self.pid = 1
        self.returncode = 0
        self.stdout = kwargs.get("stdout") or ""
        # Empty byte stream so stderr watcher thread exits immediately.
        err = kwargs.get("stderr")
        self.stderr = io.BytesIO(b"") if err is not None else None

    def poll(self):
        return None

    def terminate(self):
        return None

    def kill(self):
        return None

    def wait(self, timeout=None):
        return 0

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LpcmAudioCaptureCmdTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_VAAPI_DEVICE", None)
        os.environ.pop("FLUXCAST_WFD_VAAPI_QP", None)

    def _run_lpcm(self, *, which_map: dict[str, str | None], audio_device="miracast.monitor"):
        from wfd.media.wlroots import WlrootsMixin

        captured = {"cmds": [], "muxer": None}

        def fake_which(name):
            if name in which_map:
                return which_map[name]
            return f"/usr/bin/{name}"

        def fake_popen(cmd, *args, **kwargs):
            captured["cmds"].append(list(cmd))
            return _FakeProc(cmd, *args, **kwargs)

        class Harness(WlrootsMixin):
            def __init__(self):
                self.config = SimpleNamespace(
                    monitor=SimpleNamespace(name="HEADLESS-2", width=1920, height=1080, scale=1),
                    output_resolution="1920x1080",
                    audio_device=audio_device,
                    no_audio=False,
                    bitrate="12M",
                    fps=30,
                    h264_profile="baseline",
                    ffmpeg_stats=False,
                    source_port=19002,
                    dump_ts_path=None,
                    aosp_pmt_pid=True,
                    peer_name="",
                )
                self.tv_ip = "10.42.0.159"
                self.local_ip = "10.42.0.1"
                self.sink_rtp_port = 42030
                self.processes = []
                self._lpcm_muxer = None
                self._lpcm_video_fd = None
                self._lpcm_audio_fd = None

        harness = Harness()
        muxer_cls = _FakeMuxer

        def muxer_factory(*args, **kwargs):
            m = muxer_cls(*args, **kwargs)
            captured["muxer"] = m
            return m

        with mock.patch("wfd.media.wlroots.shutil.which", side_effect=fake_which):
            with mock.patch("wfd.media.wlroots.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("wfd.media.wlroots.time.sleep", return_value=None):
                    with mock.patch(
                        "wfd.media.wlroots.wf_recorder_supports_icc",
                        return_value=False,
                    ):
                        with mock.patch(
                            "drivers.wfd_lpcm_mux.WFDLPCMMuxer",
                            side_effect=muxer_factory,
                        ):
                            harness._start_wf_recorder_lpcm(
                                "/usr/bin/wf-recorder",
                                harness.config.monitor,
                            )
        return harness, captured

    def test_prefers_pw_cat_target_not_ffmpeg_pulse(self):
        harness, captured = self._run_lpcm(which_map={"pw-cat": "/usr/bin/pw-cat"})
        cmds = captured["cmds"]
        self.assertEqual(len(cmds), 2)
        wf_cmd, aud_cmd = cmds
        self.assertEqual(wf_cmd[0], "/usr/bin/wf-recorder")
        self.assertIn("-D", wf_cmd)
        self.assertEqual(aud_cmd[0], "pw-cat")
        self.assertIn("-r", aud_cmd)
        self.assertIn("-a", aud_cmd)
        self.assertEqual(aud_cmd[aud_cmd.index("--target") + 1], "miracast.monitor")
        self.assertIn("application.name=fluxcast-wfd-capture", aud_cmd)
        self.assertIn("media.role=Abstract", aud_cmd)
        self.assertNotIn("media.role=Video", aud_cmd)
        self.assertNotIn("ffmpeg", aud_cmd)
        self.assertNotEqual(aud_cmd[0], "ffmpeg")
        self.assertEqual(harness.processes[1].cmd, aud_cmd)
        muxer = captured["muxer"]
        self.assertIsNotNone(muxer)
        self.assertIsNotNone(muxer.started)
        _vid, aud_pipeline = muxer.started
        self.assertIn("format=S16LE", aud_pipeline)
        self.assertIn("audioconvert ! audio/x-raw,format=S16BE", aud_pipeline)
        self.assertNotIn("pipewiresrc", aud_pipeline)

    def test_falls_back_to_parec_when_pw_cat_missing(self):
        _harness, captured = self._run_lpcm(which_map={"pw-cat": None})
        aud_cmd = captured["cmds"][1]
        self.assertEqual(aud_cmd[0], "parec")
        self.assertIn("--device=miracast.monitor", aud_cmd)
        self.assertIn("--client-name=fluxcast-wfd-capture", aud_cmd)
        self.assertIn("--format=s16le", aud_cmd)
        self.assertIn("--raw", aud_cmd)
        self.assertNotIn("ffmpeg", aud_cmd)
        _vid, aud_pipeline = captured["muxer"].started
        self.assertIn("format=S16LE", aud_pipeline)
        self.assertIn("audioconvert ! audio/x-raw,format=S16BE", aud_pipeline)

    def test_appends_monitor_suffix_when_missing(self):
        _harness, captured = self._run_lpcm(
            which_map={"pw-cat": "/usr/bin/pw-cat"},
            audio_device="miracast",
        )
        aud_cmd = captured["cmds"][1]
        self.assertEqual(aud_cmd[aud_cmd.index("--target") + 1], "miracast.monitor")
        self.assertIn("media.name=miracast.monitor", aud_cmd)

    def test_refuses_mic_like_audio_device(self):
        from wfd.config import WFDNotReady

        with self.assertRaises(WFDNotReady):
            self._run_lpcm(
                which_map={"pw-cat": "/usr/bin/pw-cat"},
                audio_device="alsa_input.pci-0000_00_1f.3.analog-stereo",
            )


if __name__ == "__main__":
    unittest.main()
