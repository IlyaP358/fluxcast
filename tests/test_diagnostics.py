import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diagnostics  # noqa: E402


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FirewallCheckTest(unittest.TestCase):
    def test_skips_when_no_firewall_tool(self):
        with mock.patch("diagnostics.shutil.which", return_value=None):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_SKIP)

    def test_ufw_inactive_is_ok(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed("Status: inactive")):
            check = diagnostics._firewall_check()
        self.assertEqual(check.name, "firewall (ufw)")
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_ufw_active_without_port_warns(self):
        status = "Status: active\nTo  Action  From\n22/tcp  ALLOW  Anywhere"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(status)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn(f"ufw allow {diagnostics.WFD_RTSP_PORT}/tcp", check.detail)

    def test_ufw_active_with_port_is_ok(self):
        status = f"Status: active\n{diagnostics.WFD_RTSP_PORT}/tcp  ALLOW  Anywhere"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(status)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_firewalld_running_without_port_warns(self):
        def fake_run(args, timeout=3.0):
            if "--state" in args:
                return _completed("running")
            return _completed("no", returncode=1)

        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._run", side_effect=fake_run):
            check = diagnostics._firewall_check()
        self.assertEqual(check.name, "firewall (firewalld)")
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn(f"{diagnostics.WFD_RTSP_PORT}/tcp", check.detail)

    def test_firewalld_not_running_is_ok(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._run", return_value=_completed("not running", returncode=252)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_query_timeout_is_handled(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", side_effect=subprocess.TimeoutExpired(cmd="ufw", timeout=3.0)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)


if __name__ == "__main__":
    unittest.main()
