import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ts_probe  # noqa: E402
import wfd  # noqa: E402


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class VideoModeTableTest(unittest.TestCase):
    KNOWN_INCONSISTENT = {"1920x1200p30", "1920x1200p60"}

    def test_native_byte_matches_the_mode_bit(self):
        mismatched = set()
        for bit, mode in {**wfd.WFD_CEA_MODES, **wfd.WFD_VESA_MODES}.items():
            native = int(mode.native, 16)
            table = 1 if mode.table == "vesa" else 0
            if (native & 0x07, native >> 3) != (table, bit.bit_length() - 1):
                mismatched.add(mode.name)

        self.assertEqual(mismatched, self.KNOWN_INCONSISTENT)

    def test_level_is_a_single_defined_bit(self):
        defined = {0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40}
        for mode in {**wfd.WFD_CEA_MODES, **wfd.WFD_VESA_MODES}.values():
            level = wfd._wfd_level_for_mode(mode)
            self.assertIn(level, defined, f"{mode.name} has an undefined level")


class FirewallPortTest(unittest.TestCase):
    def test_existing_port_skips_privileged_add(self):
        calls = []
        port = wfd.WFD_RTSP_PORT

        def fake_run(args, timeout=5.0):
            calls.append((args, timeout))
            return _completed("yes")

        with (
            mock.patch.object(wfd, "_firewalld_active", return_value=True),
            mock.patch.object(wfd, "_run", side_effect=fake_run),
        ):
            opened = wfd._open_wfd_firewall_port(port)

        self.assertFalse(opened)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"--query-port={port}/tcp", calls[0][0])

    def test_closed_port_is_opened_after_successful_query(self):
        calls = []
        port = wfd.WFD_RTSP_PORT

        def fake_run(args, timeout=5.0):
            calls.append((args, timeout))
            if f"--query-port={port}/tcp" in args:
                return _completed("no", returncode=1)
            return _completed("success")

        with (
            mock.patch.object(wfd, "_firewalld_active", return_value=True),
            mock.patch.object(wfd, "_run", side_effect=fake_run),
        ):
            opened = wfd._open_wfd_firewall_port(port)

        self.assertTrue(opened)
        self.assertIn(f"--query-port={port}/tcp", calls[0][0])
        self.assertIn(f"--add-port={port}/tcp", calls[1][0])

    def test_query_error_fails_closed_without_add(self):
        port = wfd.WFD_RTSP_PORT
        calls = []

        def fake_run(args, timeout=5.0):
            calls.append(args)
            return _completed("", returncode=1, stderr="Authorization failed")

        output = io.StringIO()
        with (
            mock.patch.object(wfd, "_firewalld_active", return_value=True),
            mock.patch.object(wfd, "_run", side_effect=fake_run),
            contextlib.redirect_stdout(output),
        ):
            opened = wfd._open_wfd_firewall_port(port)

        self.assertFalse(opened)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"--query-port={port}/tcp", calls[0])
        self.assertIn("Authorization failed", output.getvalue())
        self.assertIn("--wfd-no-firewall", output.getvalue())

    def test_query_timeout_fails_closed_without_add(self):
        calls = []
        port = wfd.WFD_RTSP_PORT

        def fake_run(args, timeout=5.0):
            calls.append(args)
            raise subprocess.TimeoutExpired(args, timeout)

        with (
            mock.patch.object(wfd, "_firewalld_active", return_value=True),
            mock.patch.object(wfd, "_run", side_effect=fake_run),
        ):
            opened = wfd._open_wfd_firewall_port(port)

        self.assertFalse(opened)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"--query-port={port}/tcp", calls[0])


class ProgMapTest(unittest.TestCase):
    def test_default_prog_map_is_unchanged(self):
        self.assertEqual(
            wfd._wfd_gst_prog_map(True), "program_map,sink_4113=1,sink_4352=1"
        )
        self.assertEqual(wfd._wfd_gst_prog_map(False), "program_map,sink_4113=1")

    def test_flag_pins_the_pmt_pid_as_uint(self):
        # PMT_%d is read as uint; a plain int is silently ignored.
        for with_audio in (True, False):
            self.assertIn(",PMT_1=(uint)256", wfd._wfd_gst_prog_map(with_audio, True))

    def test_pcr_pid_is_never_requested(self):
        for aosp in (True, False):
            for with_audio in (True, False):
                self.assertNotIn("PCR_1", wfd._wfd_gst_prog_map(with_audio, aosp))

    def test_media_config_defaults_to_off(self):
        self.assertFalse(wfd.WFDMediaConfig(monitor=None).aosp_pmt_pid)


class AospTablesVersionTest(unittest.TestCase):
    def _args(self, aosp):
        config = wfd.WFDMediaConfig(monitor=None, output_resolution="1280x720",
                                    aosp_pmt_pid=aosp)
        pipeline = wfd.WFDMediaPipeline(config, tv_ip="10.42.0.2", local_ip="10.42.0.1",
                                        sink_rtp_port=35034)
        return pipeline._common_output_args()

    def test_default_sends_no_version_override(self):
        args = self._args(False)
        self.assertNotIn("-tables_version", args)
        self.assertNotIn("-sdt_period", args)
        self.assertEqual(args[args.index("-mpegts_pmt_start_pid") + 1], "4096")

    def test_flag_stamps_version_1_like_aosp(self):
        # AOSP writes 0xc3 (version_number=1) in PAT and PMT; both muxers
        # default to 0, which a sink seeded at 0 can treat as already seen.
        args = self._args(True)
        self.assertEqual(args[args.index("-tables_version") + 1], "1")
        self.assertEqual(args[args.index("-mpegts_pmt_start_pid") + 1], "256")

    def test_version_args_precede_the_output(self):
        args = self._args(True)
        self.assertLess(args.index("-tables_version"), args.index("-f"))


class AspectRatioTest(unittest.TestCase):
    """A portrait monitor must be letterboxed into the WFD mode, not stretched."""

    def test_ffmpeg_filter_fits_without_stretching(self):
        vf = wfd._letterbox_vf("1280x720")
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2", vf)
        self.assertIn("setsar=1", vf)

    def _gst_commands(self):
        """Generated gst argv for every WFD pipeline that scales."""
        class Mon:
            name, width, height, x, y, display = "eDP-1", 1080, 1920, 0, 0, ":0"

        class Sess:
            session_handle, pw_node_id, pw_fd, restore_token = "/h", 7, 42, None
            source_type, position, size = 1, (0, 0), (1080, 1920)
            stream_label, runtime, bus = "m", None, None

        captured = []

        class Proc:
            returncode = 0
            pid = 4242

            def __init__(self):
                self.stdout = io.BytesIO()

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def communicate(self, *a, **k):
                return (b"", b"")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        config = wfd.WFDMediaConfig(monitor=Mon(), output_resolution="1280x720", fps=30,
                                    bitrate="4M", no_audio=True, peer_name="X")
        pipeline = wfd.WFDMediaPipeline(config, tv_ip="10.42.0.2", local_ip="10.42.0.1",
                                        sink_rtp_port=35034)
        pipeline.tx_interface = "lo"
        with (
            mock.patch.object(wfd.shutil, "which", side_effect=lambda n: "/usr/bin/" + n),
            mock.patch.object(wfd, "_gst_has_element", return_value=True),
            mock.patch.object(wfd, "_detect_audio_monitor", return_value="m"),
            mock.patch.object(wfd, "_gst_pipewiresrc_properties", return_value=set()),
            mock.patch.object(wfd, "_gst_x264enc_properties", return_value=set()),
            mock.patch.object(wfd, "_pipewiresrc_selector_attempts",
                              return_value=[("path", ["path=7"])]),
            mock.patch.object(wfd, "start_portal_capture", return_value=Sess()),
            mock.patch.object(wfd, "close_portal_capture"),
            mock.patch.object(wfd, "_process_written_bytes", return_value=None),
            mock.patch.object(wfd.subprocess, "Popen",
                              side_effect=lambda cmd, *a, **k: (captured.append(list(cmd)),
                                                                Proc())[1]),
            mock.patch.object(wfd.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            pipeline._start_desktop_gst_x11()
            pipeline._start_desktop_portal()
            config.aosp_pmt_pid = True
            pipeline._start_desktop_portal_ffmpeg()
        return [c for c in captured if c and c[0].startswith("gst-launch")]

    def test_generated_pipelines_never_stretch(self):
        commands = self._gst_commands()
        self.assertGreaterEqual(len(commands), 3, "expected one argv per gst pipeline")
        for cmd in commands:
            index = cmd.index("videoscale")
            caps = next(a for a in cmd[index:] if a.startswith("video/x-raw") and "width=" in a)
            self.assertIn("pixel-aspect-ratio=1/1", caps)

def _sequence(values):
    """Yield the given values, then repeat the last one forever."""
    state = list(values)
    def next_value(*_a, **_k):
        return state.pop(0) if len(state) > 1 else state[0]
    return next_value


class CapturePipeTest(unittest.TestCase):
    """The portal->ffmpeg pipe must not report success on a silent capture."""

    def _run(self, written, producer_alive=True, consumer_alive=True, min_bytes=1):
        # written may be a list (consumed in order) or a callable per poll.
        config = wfd.WFDMediaConfig(monitor=None, aosp_pmt_pid=True)
        pipeline = wfd.WFDMediaPipeline(config, tv_ip="10.42.0.2", local_ip="10.42.0.1",
                                        sink_rtp_port=35034)
        procs = []

        class Proc:
            def __init__(self, alive):
                self.stdout = io.BytesIO()
                self.pid = 1000 + len(procs)
                self._alive = alive
                self.terminated = False

            def poll(self):
                return None if self._alive else 1

            def terminate(self):
                self.terminated = True

        def fake_popen(cmd, *a, **k):
            proc = Proc(producer_alive if not procs else consumer_alive)
            procs.append(proc)
            return proc

        errors = []
        with (
            mock.patch.object(wfd.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(wfd, "_process_written_bytes",
                              side_effect=written if callable(written)
                              else _sequence(written)),
            mock.patch.object(wfd.time, "sleep"),
        ):
            ok = pipeline._spawn_capture_pipe(["gst"], ["ffmpeg"], errors, "path",
                                              min_bytes=min_bytes)
        return ok, errors, pipeline, procs

    def test_flowing_capture_is_accepted(self):
        ok, errors, pipeline, _ = self._run([0, 5_000_000], min_bytes=1_000_000)
        self.assertTrue(ok)
        self.assertEqual(errors, [])
        self.assertEqual(len(pipeline.processes), 2)

    def test_silent_capture_is_rejected_even_though_audio_keeps_flowing(self):
        # The regression this guards: RTP kept leaving the box because audio
        # was fine, so a dead video source looked healthy.
        ok, errors, pipeline, procs = self._run([0, 0], min_bytes=1_000_000)
        self.assertFalse(ok)
        self.assertIn("produced no frames", errors[0])
        self.assertEqual(pipeline.processes, [])
        self.assertTrue(all(p.terminated for p in procs))

    def test_partial_frame_is_not_enough(self):
        ok, errors, _, _ = self._run([0, 4096], min_bytes=1_000_000)
        self.assertFalse(ok)
        self.assertIn("produced no frames", errors[0])

    def test_dead_capture_process_is_rejected(self):
        ok, errors, pipeline, _ = self._run([0, 5_000_000], producer_alive=False)
        self.assertFalse(ok)
        self.assertIn("capture pipeline exited", errors[0])
        self.assertEqual(pipeline.processes, [])

    def test_portal_fd_is_inherited_by_the_capture_process(self):
        # subprocess closes everything above stderr, so without pass_fds the
        # PipeWire fd is gone in the child and capture yields nothing (#84).
        seen = {}

        class Proc:
            stdout = io.BytesIO()
            pid = 1

            def poll(self):
                return None

            def terminate(self):
                pass

        def fake_popen(cmd, *a, **kwargs):
            if "stdout" in kwargs:
                seen["pass_fds"] = kwargs.get("pass_fds")
            return Proc()

        config = wfd.WFDMediaConfig(monitor=None, aosp_pmt_pid=True)
        pipeline = wfd.WFDMediaPipeline(config, tv_ip="10.42.0.2", local_ip="10.42.0.1",
                                        sink_rtp_port=35034)
        with (
            mock.patch.object(wfd.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(wfd, "_process_written_bytes", return_value=None),
            mock.patch.object(wfd.time, "sleep"),
        ):
            pipeline._spawn_capture_pipe(["gst"], ["ffmpeg"], [], "path", pass_fds=(42,))

        self.assertEqual(seen.get("pass_fds"), (42,))

    def test_unreadable_proc_io_does_not_block_startup(self):
        # /proc may be unavailable; fall back to "process is alive".
        ok, errors, _, _ = self._run([None, None], min_bytes=1_000_000)
        self.assertTrue(ok)
        self.assertEqual(errors, [])


class TsDumpTest(unittest.TestCase):
    def test_pipeline_is_untouched_without_a_dump_path(self):
        self.assertEqual(
            wfd._gst_rtp_link(None), ["!", "rtpmp2tpay", "pt=33", "mtu=1328"]
        )
        self.assertEqual(wfd._gst_dump_branch(None), [])
        self.assertIsNone(wfd.WFDMediaConfig(monitor=None).dump_ts_path)

    def test_dump_path_tees_the_muxer_output(self):
        link = wfd._gst_rtp_link("/tmp/x.ts")
        branch = wfd._gst_dump_branch("/tmp/x.ts")
        self.assertEqual(link[:3], ["!", "tee", "name=tsdump"])
        self.assertIn("rtpmp2tpay", link)
        self.assertEqual(branch[0], "tsdump.")
        self.assertIn("location=/tmp/x.ts", branch)


class TsProbeTest(unittest.TestCase):
    def test_sps_parser_reads_profile_level_and_size(self):
        sps = bytes.fromhex("42c01e d8 0a 03 c9 fd 80 88 00 00 03 00 88 00 00 1e 47 8c 18 cd".replace(" ", ""))
        info = ts_probe._parse_sps(ts_probe._unescape_rbsp(sps))
        self.assertIsNotNone(info)
        self.assertEqual(info.profile_idc, 66)
        self.assertEqual(info.level_idc, 30)

    def test_pcr_outside_the_declared_pcr_pid_is_reported(self):
        report = ts_probe.TSReport(
            pmt_pcr_pid=0x1000, pcr_count=4, pcr_pids={0x1000: 2, 0x0000: 1, 0x1011: 1}
        )
        text = ts_probe.format_report(report)
        self.assertIn("PCR also on PIDs the PMT does not name", text)
        self.assertIn("0x0000:1", text)
        self.assertIn("0x1011:1", text)

    def test_pcr_only_on_the_declared_pid_is_not_reported(self):
        report = ts_probe.TSReport(pmt_pcr_pid=0x1011, pcr_count=2, pcr_pids={0x1011: 2})
        self.assertNotIn("PCR also on PIDs", ts_probe.format_report(report))

    def test_null_packets_do_not_count_as_continuity_errors(self):
        # cc is deliberately frozen; nulls must not be flagged.
        null = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184
        path = os.path.join(os.path.dirname(__file__), "_ts_probe_null.ts")
        with open(path, "wb") as handle:
            handle.write(null * 40)
        try:
            report = ts_probe.analyze(path)
            self.assertEqual(report.continuity_errors, {})
            self.assertEqual(report.packets, 40)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
