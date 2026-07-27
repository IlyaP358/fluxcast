import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wfd  # noqa: E402


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FfmpegProgressArgsTest(unittest.TestCase):
    def test_progress_stats_are_disabled_by_default(self):
        args = wfd._ffmpeg_sender_args()

        self.assertEqual(
            args,
            ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"],
        )

    def test_progress_stats_can_be_enabled(self):
        args = wfd._ffmpeg_sender_args(show_stats=True)

        self.assertIn("-stats", args)


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


if __name__ == "__main__":
    unittest.main()
