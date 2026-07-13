import json
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

    def test_ufw_port_denied_is_not_ok(self):
        status = f"Status: active\n{diagnostics.WFD_RTSP_PORT}/tcp  DENY  Anywhere"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(status)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)

    def test_ufw_without_root_is_skipped(self):
        err = "ERROR: You need to be root to run this script"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed("", returncode=1, stderr=err)):
            check = diagnostics._ufw_check()
        self.assertIsNone(check)

    def test_returns_worst_case_across_front_ends(self):
        def fake_run(args, timeout=3.0):
            if args[0] == "ufw":
                return _completed("Status: inactive")
            if "--state" in args:
                return _completed("running")
            return _completed("no", returncode=1)

        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: f"/usr/bin/{b}" if b in ("ufw", "firewall-cmd") else None), \
                mock.patch("diagnostics._run", side_effect=fake_run):
            check = diagnostics._firewall_check()
        self.assertEqual(check.name, "firewall (firewalld)")
        self.assertEqual(check.status, diagnostics.STATUS_WARN)


class SubnetConflictCheckTest(unittest.TestCase):
    def test_skips_when_ip_missing(self):
        with mock.patch("diagnostics.shutil.which", return_value=None):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.name, "P2P subnet")
        self.assertEqual(check.status, diagnostics.STATUS_SKIP)

    def test_no_overlap_is_ok(self):
        payload = json.dumps([
            {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_overlapping_interface_warns(self):
        payload = json.dumps([
            {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "192.168.49.1", "prefixlen": 24}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("docker0", check.detail)

    def test_containing_supernet_warns(self):
        # A VPN on 192.168.0.0/16 fully contains the P2P subnet.
        payload = json.dumps([
            {"ifname": "tun0", "addr_info": [{"family": "inet", "local": "192.168.0.5", "prefixlen": 16}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("tun0", check.detail)

    def test_own_p2p_interface_is_ignored(self):
        # FluxCast's own Wi-Fi Direct interface lives on the subnet by design.
        payload = json.dumps([
            {"ifname": "p2p-dev-wlan0", "addr_info": [{"family": "inet", "local": "192.168.49.1", "prefixlen": 24}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_timeout_is_handled(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", side_effect=subprocess.TimeoutExpired(cmd="ip", timeout=3.0)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)

    def test_malformed_json_is_handled(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed("not json")):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)


if __name__ == "__main__":
    unittest.main()
