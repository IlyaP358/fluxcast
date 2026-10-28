import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDNotReady  # noqa: E402
from wfd.ie import (  # noqa: E402
    _wfd_ie_device_info,
    wfd_wpa_cli_device_info_hex,
    wfd_wpa_cli_local_ip_hex,
)
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

    def test_wpa_cli_hex_includes_length_so_lg_parses_rtsp_port(self):
        hexstr = wfd_wpa_cli_device_info_hex(7236)
        self.assertEqual(hexstr, "000600101c4400c8")
        stored = bytes.fromhex("00" + hexstr)  # wpa_cli prepends subelem id
        self.assertEqual(stored[1:3], bytes([0x00, 0x06]))
        self.assertEqual((stored[3] << 8) | stored[4], 0x0010)
        self.assertEqual((stored[5] << 8) | stored[6], 7236)
        self.assertEqual(wfd_wpa_cli_local_ip_hex(), "000501c0a83101")

    def test_rejects_out_of_range_port(self):
        with self.assertRaises(WFDNotReady):
            _wfd_source_ie(0)
        with self.assertRaises(WFDNotReady):
            _wfd_source_ie(70000)


class OperChannelTest(unittest.TestCase):
    """--wfd-p2p-channel forces the operating channel via the same
    P2PDeviceConfig struct as GOIntent. Reg class follows the band:
    81 for 2.4GHz, 115/125 for non-DFS 5GHz UNII-1 / UNII-3.
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

    def test_maps_5ghz_unii1_reg_class(self):
        with mock.patch.object(device, "_p2p_device_iface_paths",
                                return_value=["/fi/w1/wpa_supplicant1/Interfaces/1"]), \
             mock.patch.object(device, "_gdbus_call", return_value=_completed()) as call:
            ok = device._set_p2p_oper_channel("wlan0", 36)

        self.assertTrue(ok)
        payload = call.call_args[0][0][-1]
        self.assertIn("'OperChannel': <uint32 36>", payload)
        self.assertIn("'OperRegClass': <uint32 115>", payload)

    def test_maps_5ghz_unii3_reg_class(self):
        with mock.patch.object(device, "_p2p_device_iface_paths",
                                return_value=["/fi/w1/wpa_supplicant1/Interfaces/1"]), \
             mock.patch.object(device, "_gdbus_call", return_value=_completed()) as call:
            ok = device._set_p2p_oper_channel("wlan0", 149)

        self.assertTrue(ok)
        payload = call.call_args[0][0][-1]
        self.assertIn("'OperChannel': <uint32 149>", payload)
        self.assertIn("'OperRegClass': <uint32 125>", payload)

    def test_returns_false_without_a_p2p_interface(self):
        with mock.patch.object(device, "_p2p_device_iface_paths", return_value=[]):
            ok = device._set_p2p_oper_channel("wlan0", 6)
        self.assertFalse(ok)

    def test_returns_false_for_unsupported_channel(self):
        ok = device._set_p2p_oper_channel("wlan0", 99)
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
        self.assertIn(PEER_MAC, host_arg)

    def test_dedicated_go_without_physical_uses_untagged_range(self):
        with mock.patch.object(wpas_ip, "mark_unmanaged"), \
             mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()), \
             mock.patch.object(wpas_ip, "_wait_for_link_running", return_value=True), \
             mock.patch.object(wpas_ip.shutil, "which", return_value="/usr/sbin/dnsmasq"), \
             mock.patch.object(wpas_ip, "_associated_sta_macs", return_value=["66:ab:14:3d:6e:e4"]), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(wpas_ip.subprocess, "Popen") as popen, \
             mock.patch.object(wpas_ip.subprocess, "run", return_value=_completed()), \
             mock.patch.object(wpas_ip, "_wait_for_lease", return_value="192.168.49.5"):
            wpas_ip._run_as_go("p2p-0", "62:ab:14:3d:6e:e4")
        cmd = popen.call_args[0][0]
        range_arg = next(a for a in cmd if a.startswith("--dhcp-range="))
        self.assertNotIn("tag:wfdsink", range_arg)
        self.assertIn("192.168.49.2", range_arg)
        self.assertFalse(any(a.startswith("--interface=wlp") for a in cmd))

    def test_dhcp_host_includes_associated_interface_mac(self):
        sta = "66:ab:14:3d:6e:e4"
        with mock.patch.object(wpas_ip, "mark_unmanaged"), \
             mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()) as sudo, \
             mock.patch.object(wpas_ip, "_wait_for_link_running", return_value=True), \
             mock.patch.object(wpas_ip.shutil, "which", side_effect=lambda n: "/usr/sbin/dnsmasq" if n == "dnsmasq" else "/usr/bin/arping"), \
             mock.patch.object(wpas_ip, "_associated_sta_macs", return_value=[sta]), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(wpas_ip.subprocess, "Popen") as popen, \
             mock.patch.object(wpas_ip.subprocess, "run", return_value=_completed()), \
             mock.patch.object(wpas_ip, "_wait_for_lease", return_value="192.168.49.5"):
            wpas_ip._run_as_go("p2p-0", "62:ab:14:3d:6e:e4", physical_iface="wlp0s20f0u4")
        cmd = popen.call_args[0][0]
        hosts = [a for a in cmd if a.startswith("--dhcp-host=")]
        self.assertTrue(any("62:ab:14:3d:6e:e4" in a for a in hosts))
        self.assertTrue(any("66:ab:14:3d:6e:e4" in a for a in hosts))
        joined = [" ".join(c.args[0]) for c in sudo.call_args_list if c.args]
        self.assertTrue(any("arp_notify=1" in j for j in joined))

    def test_announce_go_ipv4_sets_arp_notify(self):
        with mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()) as sudo, \
             mock.patch.object(wpas_ip.shutil, "which", return_value="/usr/bin/arping"):
            wpas_ip._announce_go_ipv4("p2p-0", "192.168.49.1")
        cmds = [" ".join(c.args[0]) for c in sudo.call_args_list if c.args]
        self.assertTrue(any("arp_notify=1" in c for c in cmds))
        self.assertTrue(any("addr change 192.168.49.1/24" in c for c in cmds))
        self.assertTrue(any("arping" in c and "-U" in c for c in cmds))


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


class WaitForLeaseTest(unittest.TestCase):
    """Lease wait must survive root-owned files and MAC dash vs colon."""

    def test_reads_lease_when_mac_uses_dashes(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "leases"
            lease.write_text("123 aa-bb-cc-dd-ee-ff 192.168.49.9 * *\n")
            with mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()):
                ip = wpas_ip._wait_for_lease(str(lease), "AA:BB:CC:DD:EE:FF", timeout=0.2)
            self.assertEqual(ip, "192.168.49.9")

    def test_parses_dhcpack_from_log(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "leases"
            log = Path(tmp) / "dnsmasq.log"
            lease.write_text("")
            log.write_text(
                "dnsmasq-dhcp: DHCPACK(p2p-wlan0-5) 192.168.49.10 46:d2:44:e4:37:2f sink\n"
            )
            with mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()):
                ip = wpas_ip._wait_for_lease(
                    str(lease), PEER_MAC, timeout=0.2, dnsmasq_log=str(log)
                )
            self.assertEqual(ip, "192.168.49.10")

    def test_lease_file_any_p2p_subnet_ip(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "leases"
            lease.write_text(
                "123 66:ab:14:3d:6e:e4 192.168.49.178 LGwebOSTV *\n"
            )
            with mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()):
                ip = wpas_ip._wait_for_lease(
                    str(lease), "62:ab:14:3d:6e:e4", timeout=0.2
                )
            self.assertEqual(ip, "192.168.49.178")

    def test_parses_dhcpack_for_p2p_interface_mac(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "leases"
            log = Path(tmp) / "dnsmasq.log"
            lease.write_text("")
            log.write_text(
                "dnsmasq-dhcp: DHCPDISCOVER(p2p-0) 66:ab:14:3d:6e:e4 no address available\n"
                "dnsmasq-dhcp: DHCPACK(p2p-0) 192.168.49.7 66:ab:14:3d:6e:e4 sink\n"
            )
            with mock.patch.object(wpas_ip, "_sudo_run", return_value=_completed()):
                ip = wpas_ip._wait_for_lease(
                    str(lease),
                    "62:ab:14:3d:6e:e4",
                    timeout=0.2,
                    dnsmasq_log=str(log),
                    extra_macs=["66:ab:14:3d:6e:e4"],
                )
            self.assertEqual(ip, "192.168.49.7")


class NmP2pSplitTest(unittest.TestCase):
    def test_p2p_ifaces_are_split(self):
        self.assertTrue(wpas_ip.is_p2p_split_iface("p2p-dev-wlp0s20f3"))
        self.assertTrue(wpas_ip.is_p2p_split_iface("p2p-wlp0s20-0"))

    def test_sta_is_not_split(self):
        self.assertFalse(wpas_ip.is_p2p_split_iface("wlp0s20f3"))
        self.assertFalse(wpas_ip.is_p2p_split_iface("wlan0"))

    def test_mark_managed_skips_p2p(self):
        calls: list = []

        def fake_sudo(cmd, timeout=5.0):
            calls.append(list(cmd))
            return _completed()

        with mock.patch.object(wpas_ip, "_sudo_run", side_effect=fake_sudo):
            wpas_ip.mark_managed("p2p-dev-wlp0s20f3")
            wpas_ip.mark_unmanaged("wlp0s20f3")
            wpas_ip.mark_unmanaged("p2p-wlp0s20-1")
        joined = [" ".join(c) for c in calls]
        self.assertFalse(any("device set wlp0s20f3" in j for j in joined))
        self.assertTrue(any("device set p2p-wlp0s20-1 managed no" in j for j in joined))


class P2pDevIfnameTest(unittest.TestCase):
    def test_truncation_candidates_include_16(self):
        # IFNAMSIZ 16: p2p-dev-wlp0s20f3 is 18 chars; live ctrl is p2p-dev-wlp0s20
        full = "p2p-dev-wlp0s20f3"
        self.assertEqual(full[:15], "p2p-dev-wlp0s20")
        self.assertEqual(len("p2p-dev-wlp0s20"), 15)


class ConnectFreqPolicyTest(unittest.TestCase):
    def test_force_when_peer_on_same_primary(self):
        self.assertTrue(wpas.should_force_connect_freq(5220, 5220))

    def test_no_force_when_lg_listen_5745(self):
        self.assertFalse(wpas.should_force_connect_freq(5220, 5745))

    def test_force_when_peer_freq_unknown(self):
        self.assertTrue(wpas.should_force_connect_freq(5220, 0))

    def test_no_force_when_force_unset(self):
        self.assertFalse(wpas.should_force_connect_freq(0, 5745))


class P2pClientCarrierTest(unittest.TestCase):
    def test_parse_group_started_persistent_ip(self):
        line = (
            'P2P-GROUP-STARTED p2p-wlp0s20-1 client ssid="DIRECT-rq" '
            "freq=5220 go_dev_addr=62:ab:14:3d:6e:e4 [PERSISTENT] "
            "ip_addr=192.168.49.10 ip_mask=255.255.255.0 go_ip_addr=192.168.49.1"
        )
        self.assertEqual(
            wpas_ip.parse_p2p_group_started_addrs(line),
            ("192.168.49.10", "255.255.255.0", "192.168.49.1"),
        )

    def test_parse_group_started_without_ip(self):
        line = 'P2P-GROUP-STARTED p2p-wlp0s20-0 GO ssid="DIRECT-xx" freq=2437'
        self.assertIsNone(wpas_ip.parse_p2p_group_started_addrs(line))

    def test_wait_carrier_missing_iface(self):
        with self.assertRaises(WFDNotReady) as ctx:
            wpas.wait_for_p2p_carrier("p2p-does-not-exist-xyz", timeout=0.2)
        self.assertIn("disappeared", str(ctx.exception))


class StopGoDnsmasqTest(unittest.TestCase):
    def test_pkills_all_fluxcast_dnsmasq_without_lease_file(self):
        wpas_ip._go_dnsmasq_lease_file = None
        with mock.patch.object(wpas_ip, "_sudo_run") as sudo:
            wpas_ip._stop_go_dnsmasq()
        cmds = [" ".join(c.args[0]) for c in sudo.call_args_list]
        self.assertTrue(any("dnsmasq.*fluxcast-dnsmasq" in c for c in cmds))
        self.assertIsNone(wpas_ip._go_dnsmasq_lease_file)

    def test_pkills_lease_file_and_all_fluxcast_dnsmasq(self):
        wpas_ip._go_dnsmasq_lease_file = "/tmp/fluxcast-dnsmasq-p2p-wlp0s20-0.leases"
        with mock.patch.object(wpas_ip, "_sudo_run") as sudo:
            wpas_ip._stop_go_dnsmasq()
        cmds = [" ".join(c.args[0]) for c in sudo.call_args_list]
        self.assertTrue(any("fluxcast-dnsmasq-p2p-wlp0s20-0.leases" in c for c in cmds))
        self.assertTrue(any("dnsmasq.*fluxcast-dnsmasq" in c for c in cmds))
        self.assertIsNone(wpas_ip._go_dnsmasq_lease_file)


if __name__ == "__main__":
    unittest.main()
