"""Tests for wfd.privileged: sudo vs sudo -A, no sudo -n."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from wfd.config import WFDNotReady
import wfd.privileged as privileged
from wfd.privileged import (
    wrap_privileged,
    run_privileged,
    prime_privileged,
    _SUDOERS_BODY,
)


class WrapPrivilegedTest(unittest.TestCase):
    def setUp(self):
        privileged._ticket_ok = False

    def test_root_does_not_wrap(self):
        with mock.patch.object(os, "geteuid", return_value=0):
            cmd, env = wrap_privileged(["ip", "link", "set", "wlan0", "up"])
        self.assertEqual(cmd, ["ip", "link", "set", "wlan0", "up"])
        self.assertNotIn("-n", cmd)

    def test_existing_sudo_is_left_alone(self):
        with mock.patch.object(os, "geteuid", return_value=1000):
            cmd, _env = wrap_privileged(["sudo", "true"])
        self.assertEqual(cmd, ["sudo", "true"])

    def test_tty_uses_sudo_without_askpass_or_n(self):
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=True
        ):
            cmd, env = wrap_privileged(["nmcli", "device", "set", "wlan0", "managed", "no"])
        self.assertEqual(cmd[0], "sudo")
        self.assertNotIn("-A", cmd)
        self.assertNotIn("-n", cmd)
        self.assertEqual(cmd[1], "nmcli")

    def test_no_stdin_tty_with_askpass_uses_sudo_dash_a(self):
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=False
        ), mock.patch(
            "wfd.privileged._askpass_path", return_value="/usr/libexec/fluxcast-askpass"
        ):
            cmd, env = wrap_privileged(["gdbus", "call", "--system"])
        self.assertEqual(cmd[:2], ["sudo", "-A"])
        self.assertNotIn("-n", cmd)
        self.assertEqual(env.get("SUDO_ASKPASS"), "/usr/libexec/fluxcast-askpass")

    def test_session_tty_without_stdin_still_uses_askpass(self):
        """Panel can open /dev/tty while stdin is a log pipe."""
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=False
        ), mock.patch(
            "wfd.privileged._askpass_path", return_value="/usr/libexec/fluxcast-askpass"
        ), mock.patch.object(os, "open", return_value=3), mock.patch.object(os, "close"):
            cmd, env = wrap_privileged(["true"])
        self.assertEqual(cmd[:2], ["sudo", "-A"])
        self.assertEqual(env.get("SUDO_ASKPASS"), "/usr/libexec/fluxcast-askpass")

    def test_no_tty_without_askpass_still_sudo_not_n(self):
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=False
        ), mock.patch("wfd.privileged._askpass_path", return_value=None):
            cmd, _env = wrap_privileged(["true"])
        self.assertEqual(cmd, ["sudo", "true"])
        self.assertNotIn("-n", cmd)


    def test_after_prime_uses_sudo_n(self):
        privileged._ticket_ok = True
        with mock.patch.object(os, "geteuid", return_value=1000):
            cmd, _env = wrap_privileged(["nmcli", "device", "set", "wlan0", "managed", "no"])
        self.assertEqual(cmd[:2], ["sudo", "-n"])
        self.assertNotIn("-A", cmd)


class RunPrivilegedTest(unittest.TestCase):
    def setUp(self):
        privileged._ticket_ok = False

    def test_run_uses_wrapped_argv(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=True
        ), mock.patch("wfd.privileged.subprocess.run", return_value=completed) as run:
            run_privileged(["ip", "addr", "flush", "dev", "p2p-0"], timeout=3.0)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "sudo")
        self.assertEqual(argv[1], "ip")
        self.assertNotIn("-n", argv)

    def test_run_askpass_closes_stdin(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=False
        ), mock.patch(
            "wfd.privileged._askpass_path", return_value="/usr/libexec/fluxcast-askpass"
        ), mock.patch("wfd.privileged.subprocess.run", return_value=completed) as run:
            run_privileged(["true"], timeout=3.0)
        self.assertEqual(run.call_args[0][0][:2], ["sudo", "-A"])
        self.assertIs(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_prime_uses_sudo_v_on_tty(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=True
        ), mock.patch("wfd.privileged.shutil.which", return_value="/usr/bin/sudo"), mock.patch(
            "wfd.privileged._ensure_sudo_timestamp_policy"
        ), mock.patch(
            "wfd.privileged.subprocess.run", return_value=completed
        ) as run:
            prime_privileged()
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["sudo", "-v"])

    def test_prime_uses_sudo_a_v_without_stdin_tty(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=False
        ), mock.patch(
            "wfd.privileged._askpass_path", return_value="/usr/libexec/fluxcast-askpass"
        ), mock.patch("wfd.privileged.shutil.which", return_value="/usr/bin/sudo"), mock.patch(
            "wfd.privileged._ensure_sudo_timestamp_policy"
        ), mock.patch(
            "wfd.privileged.subprocess.run", return_value=completed
        ) as run:
            prime_privileged()
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["sudo", "-A", "-v"])
        self.assertIs(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertTrue(privileged._ticket_ok)

    def test_prime_raises_on_sudo_failure(self):
        completed = mock.Mock(
            returncode=1, stdout="", stderr="sudo: no password was provided"
        )
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch(
            "wfd.privileged._stdin_is_tty", return_value=False
        ), mock.patch(
            "wfd.privileged._askpass_path", return_value="/usr/libexec/fluxcast-askpass"
        ), mock.patch("wfd.privileged.shutil.which", return_value="/usr/bin/sudo"), mock.patch(
            "wfd.privileged.subprocess.run", return_value=completed
        ):
            with self.assertRaises(WFDNotReady) as ctx:
                prime_privileged()
        self.assertIn("no password was provided", str(ctx.exception))

    def test_sudoers_dropin_is_global_timestamp(self):
        self.assertIn("timestamp_type=global", _SUDOERS_BODY)
        self.assertIn("timestamp_timeout=8", _SUDOERS_BODY)
        self.assertNotIn("NOPASSWD", _SUDOERS_BODY)


if __name__ == "__main__":
    unittest.main()
