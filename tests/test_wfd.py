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


if __name__ == "__main__":
    unittest.main()
