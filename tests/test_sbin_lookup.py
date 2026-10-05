"""iw and wpa_cli live in /usr/sbin, which is not on a normal user's PATH on
Debian and Ubuntu (#163). Every lookup has to find them there anyway, and
every call has to run the path it found rather than the bare name.
"""
import contextlib
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diagnostics  # noqa: E402
from wfd.config import WFDNotReady  # noqa: E402
from wfd.p2p import peers, wpas_ip  # noqa: E402

SBIN = {"/usr/sbin/iw", "/usr/sbin/wpa_cli"}

IW_DEV = "phy#0\n\tInterface wlp2s0\n\t\ttype managed\n"


@contextlib.contextmanager
def debian_user_path():
    """PATH without /usr/sbin, both tools installed there."""
    with mock.patch("diagnostics.shutil.which", return_value=None), \
         mock.patch("diagnostics.os.path.isfile", side_effect=lambda p: p in SBIN), \
         mock.patch("diagnostics.os.access", side_effect=lambda p, mode: p in SBIN):
        yield


def _completed(cmd, stdout=""):
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


class DoctorFindsSbinToolsTest(unittest.TestCase):
    def test_doctor_rows_point_at_usr_sbin(self):
        with debian_user_path(), \
             mock.patch("diagnostics._run", side_effect=lambda cmd, **kw: _completed(cmd)):
            report = diagnostics.run_diagnostics(skip_firewall=True)
        by_name = {check.name: check for check in report.checks}
        self.assertEqual(by_name["iw"].status, diagnostics.STATUS_OK)
        self.assertEqual(by_name["iw"].detail, "/usr/sbin/iw")
        self.assertEqual(by_name["wpa_cli"].status, diagnostics.STATUS_OK)
        self.assertEqual(by_name["wpa_cli"].detail, "/usr/sbin/wpa_cli")

    def test_iw_p2p_check_runs_the_resolved_path(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(cmd, "phy#0\n\tInterface p2p-dev-wlp2s0\n\t\ttype P2P-device\n")

        with debian_user_path(), mock.patch("diagnostics._run", side_effect=fake_run):
            check = diagnostics._iw_p2p_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)
        self.assertEqual(calls, [["/usr/sbin/iw", "dev"]])

    def test_iw_phy_facts_run_the_resolved_path(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(cmd, "Wiphy phy0\n\tSupported interface modes:\n\t\t * P2P-client\n")

        with debian_user_path(), mock.patch("diagnostics._run", side_effect=fake_run):
            p2p_capable, _ = diagnostics._iw_phy_p2p_facts()
        self.assertTrue(p2p_capable)
        self.assertEqual(calls, [["/usr/sbin/iw", "phy"]])


class WfdFindsSbinToolsTest(unittest.TestCase):
    def test_default_wifi_interface_is_found(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(cmd, IW_DEV)

        with debian_user_path(), mock.patch.object(peers, "_run", side_effect=fake_run):
            self.assertEqual(peers._default_wifi_interface(), "wlp2s0")
        self.assertEqual(calls, [["/usr/sbin/iw", "dev"]])

    def test_active_scan_runs_the_resolved_wpa_cli(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(cmd)

        with debian_user_path(), \
             mock.patch.object(peers, "_nm_scan", side_effect=WFDNotReady("no NM in this test")), \
             mock.patch.object(peers, "_run", side_effect=fake_run), \
             mock.patch.object(peers.time, "sleep"):
            self.assertEqual(peers.active_scan(interface="wlp2s0", timeout=1), [])
        self.assertTrue(calls)
        self.assertTrue(all(cmd[0] == "/usr/sbin/wpa_cli" for cmd in calls), calls)

    def test_active_scan_still_says_when_wpa_cli_is_missing(self):
        with mock.patch("diagnostics.shutil.which", return_value=None), \
             mock.patch("diagnostics.os.path.isfile", return_value=False), \
             mock.patch.object(peers, "_nm_scan", side_effect=WFDNotReady("no NM in this test")):
            with self.assertRaisesRegex(WFDNotReady, "wpa_cli is required"):
                peers.active_scan(interface="wlp2s0", timeout=1)

    def test_p2p_role_runs_the_resolved_iw(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _completed(cmd, "Interface p2p-wlp2s0-0\n\ttype P2P-client\n")

        with debian_user_path(), mock.patch.object(wpas_ip.subprocess, "run", side_effect=fake_run):
            self.assertEqual(wpas_ip.get_p2p_role("p2p-wlp2s0-0"), "P2P-client")
        self.assertEqual(calls[0][0], "/usr/sbin/iw")


if __name__ == "__main__":
    unittest.main()
