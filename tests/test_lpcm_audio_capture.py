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
        self.args = list(cmd)  # subprocess.run CompletedProcess expects .args
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


def _media_cmds(cmds: list[list[str]]) -> list[list[str]]:
    """Keep capture/encode Popen argv; drop power-profile probes."""
    allow = {"wf-recorder", "ffmpeg", "pw-cat", "parec"}
    keep = []
    for cmd in cmds:
        if not cmd:
            continue
        base = os.path.basename(cmd[0])
        if base in allow or base.endswith("ffmpeg") or base.endswith("wf-recorder"):
            keep.append(cmd)
    return keep


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
        cmds = _media_cmds(captured["cmds"])
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
        aud_cmd = _media_cmds(captured["cmds"])[1]
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
        aud_cmd = _media_cmds(captured["cmds"])[1]
        self.assertEqual(aud_cmd[aud_cmd.index("--target") + 1], "miracast.monitor")
        self.assertIn("media.name=miracast.monitor", aud_cmd)

    def test_refuses_mic_like_audio_device(self):
        from wfd.config import WFDNotReady

        with self.assertRaises(WFDNotReady):
            self._run_lpcm(
                which_map={"pw-cat": "/usr/bin/pw-cat"},
                audio_device="alsa_input.pci-0000_00_1f.3.analog-stereo",
            )


class PipeLpcmAudioCaptureCmdTest(unittest.TestCase):
    """RENDER ENGINE vaapi/cpu + LPCM-only sink → annex-B pipe + WFDLPCMMuxer."""

    def tearDown(self):
        for key in (
            "FLUXCAST_WFD_VAAPI_PIPE_LOW_LATENCY",
            "FLUXCAST_WFD_THREAD_QUEUE_SIZE",
            "FLUXCAST_WFD_VBV_MULTIPLIER",
            "FLUXCAST_WFD_VAAPI_QUALITY",
        ):
            os.environ.pop(key, None)

    def test_pipe_lpcm_uses_annexb_ffmpeg_and_pw_cat(self):
        from wfd.hw_encode import EncodePlan
        from wfd.media.wlroots import WlrootsMixin

        captured = {"cmds": [], "muxer": None}

        def fake_popen(cmd, *args, **kwargs):
            captured["cmds"].append(list(cmd))
            return _FakeProc(cmd, *args, **kwargs)

        class Harness(WlrootsMixin):
            def __init__(self):
                self.config = SimpleNamespace(
                    monitor=SimpleNamespace(name="HEADLESS-2", width=1920, height=1080, scale=1),
                    output_resolution="1920x1080",
                    audio_device="miracast.monitor",
                    no_audio=False,
                    bitrate="12M",
                    fps=30,
                    h264_profile="baseline",
                    ffmpeg_stats=False,
                    source_port=19002,
                    dump_ts_path=None,
                    aosp_pmt_pid=True,
                    peer_name="",
                    prefer_lpcm=True,
                    latency_log_path=None,
                )
                self.tv_ip = "10.42.0.159"
                self.local_ip = "10.42.0.1"
                self.sink_rtp_port = 42030
                self.processes = []
                self._lpcm_muxer = None
                self._lpcm_video_fd = None
                self._lpcm_audio_fd = None

        plan = EncodePlan(
            name="vaapi",
            pre_input=["-vaapi_device", "/dev/dri/renderD128"],
            vf=["-vf", "hwupload"],
            video_args=[
                "-c:v", "h264_vaapi",
                "-rc_mode", "CBR",
                "-b:v", "12M",
                "-bufsize", "6M",
                "-quality", "5",
            ],
            note="h264_vaapi test",
        )

        def muxer_factory(*args, **kwargs):
            m = _FakeMuxer(*args, **kwargs)
            captured["muxer"] = m
            return m

        harness = Harness()

        def fake_which(name):
            if name == "pw-cat":
                return "/usr/bin/pw-cat"
            return f"/usr/bin/{name}"

        with mock.patch("wfd.media.wlroots.shutil.which", side_effect=fake_which):
            with mock.patch("wfd.media.wlroots.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("wfd.media.wlroots.time.sleep", return_value=None):
                    with mock.patch(
                        "wfd.media.wlroots.build_encode_plan",
                        return_value=plan,
                    ):
                        with mock.patch(
                            "drivers.wfd_lpcm_mux.WFDLPCMMuxer",
                            side_effect=muxer_factory,
                        ):
                            name = harness._start_wf_recorder_raw_pipe_lpcm(
                                "/usr/bin/wf-recorder",
                                harness.config.monitor,
                                encoder_override="vaapi",
                            )

        self.assertEqual(name, "vaapi")
        cmds = _media_cmds(captured["cmds"])
        self.assertEqual(len(cmds), 3)
        wf_cmd, ff_cmd, aud_cmd = cmds
        self.assertEqual(wf_cmd[0], "/usr/bin/wf-recorder")
        self.assertIn("rawvideo", wf_cmd)
        self.assertTrue(ff_cmd[0] == "ffmpeg" or ff_cmd[0].endswith("/ffmpeg"))
        self.assertIn("-f", ff_cmd)
        self.assertIn("h264", ff_cmd)
        self.assertIn("-an", ff_cmd)
        self.assertNotIn("aac", ff_cmd)
        self.assertTrue(any(str(x).startswith("/dev/fd/") for x in ff_cmd))
        self.assertEqual(aud_cmd[0], "pw-cat")
        self.assertEqual(aud_cmd[aud_cmd.index("--target") + 1], "miracast.monitor")
        self.assertIsNotNone(harness._lpcm_muxer)
        self.assertEqual(len(harness.processes), 3)
        _vid, aud_pipeline = captured["muxer"].started
        self.assertIn("format=S16LE", aud_pipeline)
        self.assertIn("format=S16BE", aud_pipeline)


if __name__ == "__main__":
    unittest.main()