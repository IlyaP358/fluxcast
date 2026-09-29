import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDNotReady  # noqa: E402
from wfd.ie import _wfd_ie_device_info  # noqa: E402
from wfd.p2p import dbus, device, wpas, wpas_ip  # noqa: E402
from wfd.p2p.dbus import _wfd_source_ie  # noqa: E402


PEER_MAC = "46:d2:44:e4:37:2f"


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class WfdDeviceInfoIeTest(unittest.TestCase):
    """The Device Info subelement's device-type bits (0-1 of the info
    bitmap) declare our WFD role to the peer. Getting this wrong - as an
    earlier build of the wpas backend briefly did, by inheriting whatever a
    manual wpa_cli command had last set - means advertising ourselves as a
    Sink while negotiating for a client role, a contradiction a WFD-aware
    peer has no correct way to honor.
    """

    def test_declares_source_not_sink(self):
        ie = _wfd_ie_device_info(7236)
        info_bitmap = (ie[3] << 8) | ie[4]
        device_type = info_bitmap & 0b11
        self.assertEqual(device_type, 0, "device-type bits must be 00 (Source)")

    def test_encodes_rtsp_port_and_length(self):
        ie = _wfd_ie_device_info(7236)
        self.assertEqual(ie[0:3], bytes([0x00, 0x00, 0x06]))  # subelem 0, length 6
        port = (ie[5] << 8) | ie[6]
        self.assertEqual(port, 7236)

    def test_rejects_out_of_range_port(self):
        with self.assertRaises(WFDNotReady):
            _wfd_source_ie(0)
        with self.assertRaises(WFDNotReady):
            _wfd_source_ie(70000)


class OperChannelTest(unittest.TestCase):
    """--wfd-p2p-channel forces the operating channel via the same
    P2PDeviceConfig struct as GOIntent, using reg_class 81 (2.4GHz,
    channels 1-13).
    """

    def test_sends_the_requested_channel_and_reg_class(self):
        with mock.patch.object(device, "_p2p_device_iface_paths",
                                return_value=["/fi/w1/wpa_supplicant1/Interfaces/1"]), \
             mock.patch.object(device, "_gdbus_call", return_value=_completed()) as call:
            ok = device._set_p2p_oper_channel("wlan0", 6)

        self.assertTrue(ok)
        args = call.call_args[0][0]
        payload = args[-1]
        self.assertIn("'OperChannel': <uint32 6>", payload)
        self.assertIn("'OperRegClass': <uint32 81>", payload)

    def test_returns_false_without_a_p2p_interface(self):
        with mock.patch.object(device, "_p2p_device_iface_paths", return_value=[]):
            ok = device._set_p2p_oper_channel("wlan0", 6)
        self.assertFalse(ok)


class GdbusCallTimeoutTest(unittest.TestCase):
    """A stuck Find/Connect call should surface as WFDNotReady, not a raw
    subprocess.TimeoutExpired traceback.
    """

    def test_privileged_call_timeout_becomes_wfd_not_ready(self):
        with mock.patch.object(dbus, "_run",
                                side_effect=subprocess.TimeoutExpired(cmd="gdbus", timeout=5.0)):
            with self.assertRaises(WFDNotReady):
                dbus._gdbus_call(
                    ["--dest", "fi.w1.wpa_supplicant1", "--method",
                     "fi.w1.wpa_supplicant1.Interface.P2PDevice.Connect"],
                    privileged=True,
                )

    def test_unprivileged_call_timeout_becomes_wfd_not_ready(self):
        with mock.patch.object(dbus, "_run",
                                side_effect=subprocess.TimeoutExpired(cmd="gdbus", timeout=5.0)):
            with self.assertRaises(WFDNotReady):
                dbus._gdbus_call(["--dest", "fi.w1.wpa_supplicant1"])


class RunAsGoDnsmasqCommandTest(unittest.TestCase):
    """Regression test for a real bug caught in review: --dhcp-range needs
    tag:wfdsink, not set:wfdsink. set: labels whoever takes an address from
    the range - it doesn't restrict who's eligible for it. Combined with
    also listening on the physical interface, that turned dnsmasq into an
    open DHCP server for every device on that network, not just the sink.
    """

    def test_dhcp_range_requires_the_wfdsink_tag(self):
        with mock.patch.object(wpas_ip, "mark_unmanaged"), \
             mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()), \
             mock.patch.object(wpas_ip, "_wait_for_link_running", return_value=True), \
             mock.patch.object(wpas_ip.shutil, "which", return_value="/usr/sbin/dnsmasq"), \
             mock.patch.object(wpas_ip.os, "remove"), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(wpas_ip.subprocess, "Popen") as popen, \
             mock.patch.object(wpas_ip.subprocess, "run", return_value=_completed()), \
             mock.patch.object(wpas_ip, "_wait_for_lease", return_value="192.168.49.5"):
            wpas_ip._run_as_go("p2p-wlan0-5", PEER_MAC, physical_iface="wlan0")

        dnsmasq_cmd = popen.call_args[0][0]
        range_arg = next(a for a in dnsmasq_cmd if a.startswith("--dhcp-range="))
        host_arg = next(a for a in dnsmasq_cmd if a.startswith("--dhcp-host="))

        self.assertIn("tag:wfdsink", range_arg)
        self.assertNotIn("set:wfdsink", range_arg)
        self.assertIn("set:wfdsink", host_arg)


def _access_denied():
    return _completed(
        returncode=1,
        stderr="Error: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: "
               "Sender is not authorized to send message",
    )


IFACE_LIST = "(<[objectpath '/fi/w1/wpa_supplicant1/Interfaces/1']>,)"


class WpaSupplicantPropertyPrivilegeTest(unittest.TestCase):
    """Regression test for a real, already-shipped incident: #104 restricted
    wpa_supplicant's Properties.Get/Set to wheel/sudo, which silently broke
    every unprivileged call the wpas backend was making to them - peer
    discovery quietly found nothing, and _set_wfd_ies (which raises rather
    than warns) failed outright.

    The escalation is opt-in, though. device.py's helpers are shared with
    the default NetworkManager path, where session.py calls them before it
    ever picks a backend - escalating there would trade a cosmetic warning
    for a sudo prompt on every session (#113). So: wpas.py passes
    privileged=True, session.py takes the unprivileged default, and both
    halves are pinned below.
    """

    def setUp(self):
        # _gdbus_call raises WFDNotReady before reaching _run if gdbus is
        # missing, which would make these pass for the wrong reason.
        patcher = mock.patch.object(dbus.shutil, "which", return_value="/usr/bin/gdbus")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_escalated(self, run):
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1][0][0][0], "sudo")

    def _assert_no_escalation(self, run):
        self.assertEqual(run.call_count, 1)
        self.assertNotEqual(run.call_args_list[0][0][0][0], "sudo")

    def test_set_wfd_ies_retries_under_sudo(self):
        calls = [_access_denied(), _completed()]
        with mock.patch.object(dbus, "_run", side_effect=calls) as run:
            wpas._set_wfd_ies(7236)  # raises on failure - not raising is the assertion
        self._assert_escalated(run)

    def test_p2p_device_iface_paths_retries_under_sudo(self):
        calls = [_access_denied(), _completed(stdout=IFACE_LIST)]
        with mock.patch.object(dbus, "_run", side_effect=calls) as run, \
             mock.patch.object(device, "_wpas_get_string", return_value="wlan0"):
            paths = device._p2p_device_iface_paths("wlan0", privileged=True)
        self.assertEqual(paths, ["/fi/w1/wpa_supplicant1/Interfaces/1"])
        self._assert_escalated(run)

    def test_set_p2p_go_intent_retries_under_sudo(self):
        calls = [
            _access_denied(), _completed(stdout=IFACE_LIST),   # iface lookup
            _completed(stdout="(<{'GOIntent': <uint32 7>}>,)"),  # read previous
            _access_denied(), _completed(),                     # the Set itself
        ]
        with mock.patch.object(dbus, "_run", side_effect=calls) as run, \
             mock.patch.object(device, "_wpas_get_string", return_value="wlan0"):
            previous = device._set_p2p_go_intent("wlan0", 0, privileged=True)
        self.assertEqual(previous, 7)
        self.assertEqual([c[0][0][0] for c in run.call_args_list].count("sudo"), 2)

    def test_set_p2p_oper_channel_escalates_without_a_flag(self):
        # wpas.py is its only caller, so it has no privileged parameter.
        calls = [_access_denied(), _completed(stdout=IFACE_LIST), _completed()]
        with mock.patch.object(dbus, "_run", side_effect=calls) as run, \
             mock.patch.object(device, "_wpas_get_string", return_value="wlan0"):
            self.assertTrue(device._set_p2p_oper_channel("wlan0", 6))
        self.assertEqual(run.call_args_list[1][0][0][0], "sudo")

    def test_p2p_device_iface_paths_does_not_escalate_by_default(self):
        with mock.patch.object(dbus, "_run", return_value=_access_denied()) as run:
            paths = device._p2p_device_iface_paths("wlan0")
        self.assertEqual(paths, [])
        self._assert_no_escalation(run)

    def test_set_p2p_device_name_does_not_escalate_by_default(self):
        # session.py:73 calls this on both backends, before it branches.
        with mock.patch.object(dbus, "_run", return_value=_access_denied()) as run:
            device._set_p2p_device_name("wlan0")
        self._assert_no_escalation(run)


if __name__ == "__main__":
    unittest.main()
