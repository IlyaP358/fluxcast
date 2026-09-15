"""Layer 4 — FluxCast ↔ ICC wf-recorder integration.

Locks how FluxCast selects an ICC binary and which capture flags it passes
(``-D``, ``-r``) for LPCM and DMA paths. Optional live binary smoke skips
when ``FLUXCAST_WFD_WF_RECORDER_BIN`` is unset or not an ICC build.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.wf_recorder import find_wf_recorder, wf_recorder_supports_icc  # noqa: E402


class IccProtoSelectionTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_BIN", None)
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_PROTO", None)
        from wfd import wf_recorder as wr

        wr._icc_cache.clear()

    @mock.patch("wfd.wf_recorder.wf_recorder_supports_icc", return_value=True)
    @mock.patch("wfd.wf_recorder._usable_recorder", return_value=True)
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/opt/wf-recorder-icc")
    def test_proto_icc_accepts_icc_binary(self, _which, _usable, _icc):
        os.environ["FLUXCAST_WFD_WF_RECORDER_PROTO"] = "icc"
        self.assertEqual(find_wf_recorder(), "/opt/wf-recorder-icc")

    @mock.patch("wfd.wf_recorder.wf_recorder_supports_icc", return_value=False)
    @mock.patch("wfd.wf_recorder._usable_recorder", return_value=True)
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder")
    def test_proto_icc_rejects_stock_binary(self, _which, _usable, _icc):
        os.environ["FLUXCAST_WFD_WF_RECORDER_PROTO"] = "icc"
        self.assertIsNone(find_wf_recorder())

    @mock.patch("wfd.wf_recorder.wf_recorder_supports_icc", return_value=False)
    @mock.patch("wfd.wf_recorder._usable_recorder", return_value=True)
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder")
    def test_proto_auto_accepts_stock(self, _which, _usable, _icc):
        os.environ["FLUXCAST_WFD_WF_RECORDER_PROTO"] = "auto"
        self.assertEqual(find_wf_recorder(), "/usr/bin/wf-recorder")

    @mock.patch("wfd.wf_recorder.wf_recorder_supports_icc", return_value=True)
    @mock.patch("wfd.wf_recorder._usable_recorder", return_value=True)
    def test_bin_override_preferred_with_proto_icc(self, _usable, _icc):
        os.environ["FLUXCAST_WFD_WF_RECORDER_PROTO"] = "icc"
        os.environ["FLUXCAST_WFD_WF_RECORDER_BIN"] = "/home/colin/src/wf-recorder/build/wf-recorder"
        with mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder"):
            self.assertEqual(
                find_wf_recorder(),
                "/home/colin/src/wf-recorder/build/wf-recorder",
            )


class IccCaptureRateArgsTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ICC_CAPTURE_FPS", None)

    def _harness(self, fps: int = 30):
        from wfd.media.wlroots import WlrootsMixin

        class H(WlrootsMixin):
            def __init__(self):
                self.config = SimpleNamespace(fps=fps)

        return H()

    def test_stock_gets_no_rate_flag(self):
        with mock.patch("wfd.media.wlroots.wf_recorder_supports_icc", return_value=False):
            self.assertEqual(self._harness()._wf_capture_rate_args("/usr/bin/wf-recorder"), [])

    def test_icc_follows_config_fps_30(self):
        with mock.patch("wfd.media.wlroots.wf_recorder_supports_icc", return_value=True):
            self.assertEqual(self._harness(30)._wf_capture_rate_args("/opt/icc"), ["-r", "30"])

    def test_icc_follows_config_fps_60(self):
        with mock.patch("wfd.media.wlroots.wf_recorder_supports_icc", return_value=True):
            self.assertEqual(self._harness(60)._wf_capture_rate_args("/opt/icc"), ["-r", "60"])

    def test_icc_fps_override(self):
        os.environ["FLUXCAST_WFD_ICC_CAPTURE_FPS"] = "60"
        with mock.patch("wfd.media.wlroots.wf_recorder_supports_icc", return_value=True):
            # Override wins even when profile/stream is 30.
            self.assertEqual(self._harness(30)._wf_capture_rate_args("/opt/icc"), ["-r", "60"])


class LpcmIccFlagsTest(unittest.TestCase):
    """LPCM path always uses -D; -r only when the binary is ICC."""

    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ICC_CAPTURE_FPS", None)
        os.environ.pop("FLUXCAST_VAAPI_DEVICE", None)
        os.environ.pop("FLUXCAST_WFD_VAAPI_QP", None)

    def _run_lpcm(self, *, icc: bool):
        from wfd.media.wlroots import WlrootsMixin

        captured = {"cmds": []}

        class FakeMuxer:
            def __init__(self, *a, **k):
                self._mux_thread = SimpleNamespace(is_alive=lambda: True)
                self.audio_frames_sent = 0
                self.frames_sent = 0

            def start(self, *a, **k):
                return None

            def stop(self):
                return None

        class FakeProc:
            def __init__(self, cmd, *a, **k):
                import io

                self.cmd = list(cmd)
                self.args = list(cmd)
                self.pid = 1
                self.returncode = 0
                # Empty stderr so wlroots sender watchers exit; communicate for
                # power-plan gdbus probes that share the patched Popen.
                self.stderr = io.BytesIO(b"") if k.get("stderr") is not None else None

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

        def fake_popen(cmd, *a, **k):
            captured["cmds"].append(list(cmd))
            return FakeProc(cmd)

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
                )
                self.tv_ip = "10.42.0.159"
                self.local_ip = "10.42.0.1"
                self.sink_rtp_port = 42030
                self.processes = []
                self._lpcm_muxer = None
                self._lpcm_video_fd = None
                self._lpcm_audio_fd = None

        harness = Harness()

        def fake_which(name):
            if name == "pw-cat":
                return "/usr/bin/pw-cat"
            return f"/usr/bin/{name}"

        with mock.patch("wfd.media.wlroots.shutil.which", side_effect=fake_which):
            with mock.patch("wfd.media.wlroots.subprocess.Popen", side_effect=fake_popen):
                with mock.patch("wfd.media.wlroots.time.sleep", return_value=None):
                    with mock.patch(
                        "wfd.media.wlroots.wf_recorder_supports_icc",
                        return_value=icc,
                    ):
                        with mock.patch(
                            "drivers.wfd_lpcm_mux.WFDLPCMMuxer",
                            side_effect=FakeMuxer,
                        ):
                            harness._start_wf_recorder_lpcm(
                                "/opt/wf-recorder" if icc else "/usr/bin/wf-recorder",
                                harness.config.monitor,
                            )
        for cmd in captured["cmds"]:
            base = os.path.basename(cmd[0]) if cmd else ""
            if "wf-recorder" in base:
                return cmd
        raise AssertionError(f"wf-recorder not in cmds: {captured['cmds']!r}")

    def test_lpcm_stock_has_d_no_r(self):
        wf = self._run_lpcm(icc=False)
        self.assertIn("-D", wf)
        self.assertNotIn("-r", wf)

    def test_lpcm_icc_has_d_and_r_matching_config_fps(self):
        wf = self._run_lpcm(icc=True)
        self.assertIn("-D", wf)
        self.assertIn("-r", wf)
        # LPCM harness config.fps is 30 (typical UI profile).
        self.assertEqual(wf[wf.index("-r") + 1], "30")


class LiveIccBinarySmokeTest(unittest.TestCase):
    """Optional: probe a real ICC wf-recorder when BIN is set (or common path)."""

    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_BIN", None)
        os.environ.pop("FLUXCAST_WFD_WF_RECORDER_PROTO", None)
        from wfd import wf_recorder as wr

        wr._icc_cache.clear()

    def _candidate(self) -> str | None:
        env = (os.environ.get("FLUXCAST_WFD_WF_RECORDER_BIN") or "").strip()
        if env and os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        # Common local build path used by Omarchy PoC docs.
        for path in (
            os.path.expanduser("~/src/wf-recorder/build/wf-recorder"),
            "/home/colin/src/wf-recorder/build/wf-recorder",
        ):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def test_live_icc_binary_detected_and_selectable(self):
        path = self._candidate()
        if not path:
            self.skipTest("no ICC wf-recorder binary found (set FLUXCAST_WFD_WF_RECORDER_BIN)")

        ver = subprocess.run(
            [path, "-v"], capture_output=True, text=True, timeout=3.0
        )
        blob = ((ver.stdout or "") + (ver.stderr or "")).lower()
        self.assertTrue(
            wf_recorder_supports_icc(path),
            f"{path} not detected as ICC (--toplevel / ext-copy-capture)",
        )
        # Version string from our fork branch is a useful breadcrumb for operators.
        self.assertTrue(
            ("ext-copy" in blob) or ("--toplevel" in blob) or wf_recorder_supports_icc(path)
        )

        os.environ["FLUXCAST_WFD_WF_RECORDER_BIN"] = path
        os.environ["FLUXCAST_WFD_WF_RECORDER_PROTO"] = "icc"
        from wfd import wf_recorder as wr

        wr._icc_cache.clear()
        self.assertEqual(find_wf_recorder(), path)


if __name__ == "__main__":
    unittest.main()
