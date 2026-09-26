import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDNotReady  # noqa: E402
from wfd.ie import (  # noqa: E402
    WFD_DEVICE_TYPE_PRIMARY_SINK, WFD_DEVICE_TYPE_SOURCE, WFDPeer,
    _parse_wfd_ies_device_type, _wfd_capability_from_hex, _wfd_ie_device_info,
)
from wfd.p2p import dbus, nm, peers  # noqa: E402


def _gdbus_bytes(*values):
    return "(<@ay [" + ", ".join(f"byte 0x{v:02x}" for v in values) + "]>,)"


# What gdbus prints for a peer that advertises no Wi-Fi Display data at all.
# Empty array, non-empty string - the whole reason #124 existed.
EMPTY_WFD_IES = "(<@ay []>,)"

# Device Information subelements captured off real hardware: id 0, length 6,
# then the device-info bitmap, the RTSP port (0x1c44) and the throughput.
# A Samsung TV in Screen Share mode, bitmap 0x0111 -> Primary Sink.
SINK_WFD_IES = _gdbus_bytes(0x00, 0x00, 0x06, 0x01, 0x11, 0x1c, 0x44, 0x00, 0x36)
# A laptop running FluxCast, bitmap 0x0010 -> Source. Byte for byte what
# _wfd_ie_device_info builds, which is the point of the test below.
SOURCE_WFD_IES = _gdbus_bytes(0x00, 0x00, 0x06, 0x00, 0x10, 0x1c, 0x44, 0x00, 0xc8)


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class PrintScanCapabilityTest(unittest.TestCase):
    """#121 scanned two printers and both were listed as valid sinks, because
    print_scan decided capability by searching the human-readable details
    string. Capability is now carried on the peer itself, set by whichever
    scanner parsed the data.
    """

    def test_capable_peer_is_labelled(self):
        out = _capture(peers.print_scan, [
            WFDPeer(address="AA:BB:CC:DD:EE:FF", name="Samsung TV",
                    source="NetworkManager", wfd_capable=True),
        ])
        self.assertIn("WFD capability data detected", out)
        self.assertNotIn("probably not a Miracast sink", out)

    def test_non_capable_peer_is_reported_as_state_not_verdict(self):
        """A sink only advertises Wi-Fi Display while it is waiting for a
        connection, so the scan cannot tell "not a sink" apart from "a sink
        still on a normal input" - and the second is the common case. The
        line has to describe what was advertised and name the fix, not rule
        the device out.
        """
        out = _capture(peers.print_scan, [
            WFDPeer(address="5E:3A:45:D2:2F:3B", name="DIRECT-3b-HP M227f LaserJet",
                    source="NetworkManager", wfd_capable=False),
        ])
        self.assertIn("not advertising Wi-Fi Display", out)
        self.assertIn("Screen Share mode", out)
        self.assertNotIn("not a Miracast sink", out)
        self.assertNotIn("WFD capability data detected", out)

    def test_source_peer_is_distinguished_from_no_data(self):
        out = _capture(peers.print_scan, [
            WFDPeer(address="AA:BB:CC:DD:EE:FF", name="another laptop",
                    source="NetworkManager", wfd_capable=False,
                    wfd_device_type=WFD_DEVICE_TYPE_SOURCE),
        ])
        self.assertIn("as a source, not a sink", out)
        # Telling this user to press Screen Share would be nonsense.
        self.assertNotIn("Screen Share mode", out)

    def test_details_string_alone_does_not_imply_capability(self):
        # The old check matched "wfd_ies=" anywhere in details. Nothing may
        # reintroduce that coupling.
        out = _capture(peers.print_scan, [
            WFDPeer(address="AA:BB:CC:DD:EE:FF",
                    details="model=X; wfd_ies=(<@ay []>,); sink_rtsp_port=7236",
                    wfd_capable=False),
        ])
        self.assertIn("not advertising Wi-Fi Display", out)



class NmP2PDevicePathTest(unittest.TestCase):
    """NetworkManager P2P discovery, including the IWD virtual path."""

    DEVICE_PATH = "/org/freedesktop/NetworkManager/Devices/9"

    def _lookup(self, requested, iface, device_type):
        result = mock.Mock(
            returncode=0,
            stdout=f"(['{self.DEVICE_PATH}'],)",
            stderr="",
        )

        def fake_get_string(path, interface, prop):
            if prop == "Interface":
                return iface
            return ""

        def fake_get_property(path, interface, prop):
            if prop == "DeviceType":
                return device_type
            return ""

        output = io.StringIO()

        with mock.patch.object(
            nm, "_gdbus_call", return_value=result
        ), mock.patch.object(
            nm, "_nm_get_string", side_effect=fake_get_string
        ), mock.patch.object(
            nm, "_nm_get_property", side_effect=fake_get_property
        ), contextlib.redirect_stdout(output):
            found = nm._nm_p2p_device_path(requested)

        return found, output.getvalue()

    def test_normal_networkmanager_p2p_device(self):
        found, output = self._lookup(
            "wlan0",
            "p2p-wlan0-0",
            "(<uint32 30>,)",
        )

        self.assertEqual(found, self.DEVICE_PATH)
        self.assertEqual(output, "")

    def test_iwd_virtual_p2p_device(self):
        found, output = self._lookup(
            "wlan0",
            "/net/connman/iwd/0",
            "(<uint32 30>,)",
        )

        self.assertEqual(found, self.DEVICE_PATH)
        self.assertIn("--wfd-interface", output)
        self.assertIn("does not map directly", output)

    def test_failed_device_type_read_falls_back_to_name(self):
        found, output = self._lookup(
            "wlan0",
            "p2p-wlan0-0",
            "",
        )

        self.assertEqual(found, self.DEVICE_PATH)
        self.assertEqual(output, "")


class NmScanCapabilityTest(unittest.TestCase):
    """_nm_scan must decide capability from the parsed IE bytes, not from the
    raw gdbus string, which is truthy even for a peer with no WFD data.
    """

    def _scan_with_wfd_ies(self, wfd_ies_raw):
        def fake_get_property(path, interface, prop):
            if prop == "Peers":
                return "(<['/org/freedesktop/NetworkManager/WifiP2PPeer/1']>,)"
            if prop == "WfdIEs":
                return wfd_ies_raw
            return ""

        with mock.patch.object(nm, "_nm_p2p_device_path", return_value="/dev/0"), \
             mock.patch.object(nm, "_nm_get_string", return_value="peer"), \
             mock.patch.object(nm, "_nm_get_property", side_effect=fake_get_property), \
             mock.patch.object(nm, "_nm_start_find"), \
             mock.patch.object(nm, "_nm_stop_find"), \
             mock.patch.object(nm.time, "sleep"):
            return nm._nm_scan(None, 1)

    def test_empty_byte_array_is_not_capable(self):
        found = self._scan_with_wfd_ies(EMPTY_WFD_IES)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].wfd_capable)
        # and the misleading detail is dropped rather than shown as evidence
        self.assertNotIn("wfd_ies=", found[0].details)

    def test_failed_read_is_unknown_not_incapable(self):
        """_nm_get_property returns "" when the gdbus call fails, not only when
        the property is empty - a peer that ages out mid-scan hits this. That
        is unknown, not incapable: reporting False labels a real sink
        "probably not a Miracast sink", the mirror of the wpa_cli case below.
        """
        found = self._scan_with_wfd_ies("")
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].wfd_capable)
        out = _capture(peers.print_scan, found)
        self.assertNotIn("not advertising Wi-Fi Display", out)
        self.assertNotIn("WFD capability data detected", out)

    def test_real_sink_is_capable(self):
        found = self._scan_with_wfd_ies(SINK_WFD_IES)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].wfd_capable)
        self.assertEqual(found[0].wfd_device_type, WFD_DEVICE_TYPE_PRIMARY_SINK)
        self.assertIn("wfd_ies=", found[0].details)
        self.assertEqual(found[0].rtsp_port, 7236)

    def test_another_source_is_not_a_sink(self):
        """Presence of WFD IEs is not the question, device type is. A second
        machine running FluxCast advertises a full and valid set of them and
        is still not something to connect to.
        """
        found = self._scan_with_wfd_ies(SOURCE_WFD_IES)
        self.assertEqual(len(found), 1)
        self.assertIs(found[0].wfd_capable, False)
        self.assertEqual(found[0].wfd_device_type, WFD_DEVICE_TYPE_SOURCE)
        # The IEs are real, so they stay in details as evidence even though
        # the peer is not a sink.
        self.assertIn("wfd_ies=", found[0].details)

    def test_our_own_advertisement_would_not_be_called_a_sink(self):
        """Pinned against the builder rather than a literal, so that changing
        what FluxCast advertises cannot quietly make FluxCast look like a
        sink to the next FluxCast.
        """
        ours = _gdbus_bytes(*_wfd_ie_device_info(7236))
        found = self._scan_with_wfd_ies(ours)
        self.assertIs(found[0].wfd_capable, False)
        self.assertEqual(found[0].wfd_device_type, WFD_DEVICE_TYPE_SOURCE)


class WpaCliScanCapabilityTest(unittest.TestCase):
    """The wpa_cli path reports capability through p2p_peer's own fields."""

    def _scan_with_details(self, details):
        def fake_run(cmd, **kwargs):
            from subprocess import CompletedProcess
            if "p2p_peers" in cmd:
                return CompletedProcess(cmd, 0, stdout="aa:bb:cc:dd:ee:ff\n", stderr="")
            if "p2p_peer" in cmd:
                return CompletedProcess(cmd, 0, stdout=details, stderr="")
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        # active_scan only reaches the wpa_cli path when the NetworkManager
        # scan is unavailable.
        with mock.patch.object(peers, "_nm_scan",
                               side_effect=WFDNotReady("no NM in this test")), \
             mock.patch.object(peers, "_run", side_effect=fake_run), \
             mock.patch.object(peers.shutil, "which", return_value="/usr/bin/wpa_cli"), \
             mock.patch.object(peers.time, "sleep"):
            return peers.active_scan(interface="wlan0", timeout=1)

    def test_peer_without_wfd_fields_is_not_capable(self):
        found = self._scan_with_details("device_name=HL-L2350DW\npri_dev_type=3-0050F204-1\n")
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].wfd_capable)

    def test_peer_with_wfd_dev_info_is_capable(self):
        found = self._scan_with_details("device_name=TV\nwfd_dev_info=01111c440036\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].wfd_capable)
        self.assertEqual(found[0].wfd_device_type, WFD_DEVICE_TYPE_PRIMARY_SINK)

    def test_peer_advertising_as_a_source_is_not_capable(self):
        found = self._scan_with_details("device_name=laptop\nwfd_dev_info=00101c4400c8\n")
        self.assertEqual(len(found), 1)
        self.assertIs(found[0].wfd_capable, False)
        self.assertEqual(found[0].wfd_device_type, WFD_DEVICE_TYPE_SOURCE)

    def test_wfd_subelems_carries_the_same_answer(self):
        found = self._scan_with_details(
            "device_name=TV\nwfd_subelems=00000601111c440036\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].wfd_capable)
        self.assertEqual(found[0].wfd_device_type, WFD_DEVICE_TYPE_PRIMARY_SINK)

    def test_failed_detail_lookup_is_unknown_not_incapable(self):
        """A p2p_peer lookup that times out means we do not know. Reporting
        False there would tell the user a real sink is not advertising, and
        steer them away from the device that would have worked.
        """
        found = self._scan_with_details("")
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].wfd_capable)
        out = _capture(peers.print_scan, found)
        self.assertNotIn("not advertising Wi-Fi Display", out)
        self.assertNotIn("WFD capability data detected", out)


class WfdDeviceTypeParsingTest(unittest.TestCase):
    """Bits 0-1 of the Device Information bitmap, and the two hex layouts
    wpa_cli reports them in.
    """

    def test_device_type_comes_from_the_bitmap(self):
        sink = [0x00, 0x00, 0x06, 0x01, 0x11, 0x1c, 0x44, 0x00, 0x36]
        source = [0x00, 0x00, 0x06, 0x00, 0x10, 0x1c, 0x44, 0x00, 0xc8]
        self.assertEqual(_parse_wfd_ies_device_type(sink), WFD_DEVICE_TYPE_PRIMARY_SINK)
        self.assertEqual(_parse_wfd_ies_device_type(source), WFD_DEVICE_TYPE_SOURCE)

    def test_unreadable_bytes_are_unknown(self):
        self.assertIsNone(_parse_wfd_ies_device_type([0x0a, 0x00, 0x02, 0x41, 0x42]))

    def test_hex_is_read_as_subelements_or_as_a_bare_body(self):
        with_header = _wfd_capability_from_hex("00000601111c440036")
        bare_body = _wfd_capability_from_hex("01111c440036")
        self.assertEqual(with_header, (True, WFD_DEVICE_TYPE_PRIMARY_SINK))
        self.assertEqual(bare_body, (True, WFD_DEVICE_TYPE_PRIMARY_SINK))

    def test_hex_tolerates_a_0x_prefix_and_junk(self):
        self.assertEqual(_wfd_capability_from_hex("0x01111c440036"),
                         (True, WFD_DEVICE_TYPE_PRIMARY_SINK))
        self.assertEqual(_wfd_capability_from_hex("nonsense"), (None, None))
        self.assertEqual(_wfd_capability_from_hex(""), (None, None))


class PeerBlockerTest(unittest.TestCase):
    """#136 picked a peer that already owned a group and only paired by PIN,
    waited 35 seconds for a bare timeout, and had a working TV in the same
    list (#137).
    """

    # The two devices from #136's scan, with the numbers from its log.
    TLSC = "device_name=TLSC.D9B.6/.7\ngroup_capab=0x89\nconfig_methods=0x0008\n"
    WEBOS = "device_name=[LG] webOS TV\ngroup_capab=0x00\nconfig_methods=0x1188\n"

    def test_group_owner_and_pin_only_peer(self):
        self.assertEqual(peers._parse_peer_blockers(self.TLSC), (True, False))

    def test_reachable_peer(self):
        self.assertEqual(peers._parse_peer_blockers(self.WEBOS), (False, True))

    def test_absent_fields_are_unknown_not_false(self):
        self.assertEqual(peers._parse_peer_blockers("device_name=printer\n"), (None, None))

    def test_both_blockers_are_named(self):
        out = _capture(peers.print_scan, [
            WFDPeer(address="AA:BB:CC:DD:EE:FF", wfd_capable=True,
                    wfd_device_type=WFD_DEVICE_TYPE_PRIMARY_SINK,
                    is_group_owner=True, offers_push_button=False),
        ])
        self.assertIn("cannot join an existing group", out)
        self.assertIn("pairs by PIN only", out)

    def test_unknown_blockers_say_nothing(self):
        out = _capture(peers.print_scan, [
            WFDPeer(address="AA:BB:CC:DD:EE:FF", wfd_capable=True,
                    wfd_device_type=WFD_DEVICE_TYPE_PRIMARY_SINK),
        ])
        self.assertNotIn("group", out)
        self.assertNotIn("PIN", out)


class WpasPeerCapabilitiesTest(unittest.TestCase):
    """#104's policy restricts wpa_supplicant to admin groups, so on the
    default path a denial has to stay silent rather than prompt (#114).
    """

    def _read(self, responses, running=True):
        calls = []

        def fake_call(args, timeout=None, privileged=False):
            calls.append((args, privileged))
            for needle, stdout in responses.items():
                if needle in args:
                    return mock.Mock(returncode=0, stdout=stdout, stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="AccessDenied")

        with mock.patch.object(dbus, "_wpas_running", return_value=running), \
             mock.patch.object(dbus, "_gdbus_call", side_effect=fake_call):
            return dbus._wpas_peer_capabilities(), calls

    def test_absent_supplicant_is_never_activated(self):
        # wpa_supplicant is D-Bus activatable; a scan must not start it.
        found, calls = self._read({}, running=False)
        self.assertEqual((found, calls), ({}, []))

    def test_a_timeout_does_not_abort_the_scan(self):
        with mock.patch.object(dbus, "_wpas_running", return_value=True), \
             mock.patch.object(dbus, "_gdbus_call", side_effect=WFDNotReady("timed out")):
            self.assertEqual(dbus._wpas_peer_capabilities(), {})

    def test_never_escalates(self):
        _, calls = self._read({})
        self.assertTrue(calls)
        self.assertFalse(any(privileged for _, privileged in calls))

    def test_denied_read_is_empty_not_an_error(self):
        found, _ = self._read({})
        self.assertEqual(found, {})

    def test_reads_capabilities_keyed_by_bare_mac(self):
        found, _ = self._read({
            "Interfaces": "(<['/fi/w1/wpa_supplicant1/Interfaces/0']>,)",
            "Peers": "(<[objectpath '/fi/w1/wpa_supplicant1/Interfaces/0/Peers/aabbccddeeff']>,)",
            "groupcapability": "(<byte 0x89>,)",
            "config_method": "(<uint16 8>,)",
        })
        self.assertEqual(found, {"aabbccddeeff": (True, False)})

    def test_byte_properties_are_read_as_hex(self):
        # _variant_uint reads <byte 0x89> back as 89 rather than 137, which
        # would clear bit 0 and hide exactly the case this detects.
        self.assertEqual(dbus._variant_number("(<byte 0x89>,)"), 0x89)
        self.assertEqual(dbus._variant_number("(<uint16 4488>,)"), 4488)
        self.assertIsNone(dbus._variant_number(""))


    def test_config_method_is_read_once_and_never_the_plural(self):
        """wpa_supplicant's D-Bus reference names the peer property
        config_method, singular. An earlier version asked for both spellings,
        which cost a second round trip per peer for a property that does not
        exist.
        """
        _, calls = self._read({
            "Interfaces": "(<['/fi/w1/wpa_supplicant1/Interfaces/0']>,)",
            "Peers": "(<[objectpath '/fi/w1/wpa_supplicant1/Interfaces/0/Peers/aabbccddeeff']>,)",
            "groupcapability": "(<byte 0x89>,)",
            "config_method": "(<uint16 8>,)",
        })
        requested = [arg for args, _ in calls for arg in args]
        self.assertEqual(requested.count("config_method"), 1)
        self.assertNotIn("config_methods", requested)

    def test_budget_stops_early_and_keeps_what_it_read(self):
        """A supplicant that is slow but still answering never raises, so
        nothing but this budget stops the per-peer reads accumulating. A
        partial answer is correct: the peers it did not reach come back absent,
        which reads as unknown rather than as reachable.
        """
        two_peers = (
            "(<[objectpath '/fi/w1/wpa_supplicant1/Interfaces/0/Peers/aaaaaaaaaaaa', "
            "objectpath '/fi/w1/wpa_supplicant1/Interfaces/0/Peers/bbbbbbbbbbbb']>,)"
        )
        # deadline, interface check, first peer check, second peer check
        clock = iter([0.0, 0.1, 0.2, dbus._PEER_CAPABILITY_BUDGET + 1.0])
        with mock.patch.object(dbus.time, "monotonic", lambda: next(clock)):
            found, _ = self._read({
                "Interfaces": "(<['/fi/w1/wpa_supplicant1/Interfaces/0']>,)",
                "Peers": two_peers,
                "groupcapability": "(<byte 0x89>,)",
                "config_method": "(<uint16 8>,)",
            })
        self.assertEqual(found, {"aaaaaaaaaaaa": (True, False)})


class PeerCapabilityMissTest(unittest.TestCase):
    """The wpa map is keyed by wpa_supplicant's peer object path and looked up
    with NetworkManager's HwAddress. Those are usually the same device address,
    but not always - LG advertises one address during discovery and uses
    another in the group (#135). A miss there is silent, and silence is
    indistinguishable from "read it, learned nothing", so say it.
    """

    PEER_MAC = "aa:bb:cc:dd:ee:ff"

    def _scan(self, capabilities):
        def fake_get_property(path, interface, prop):
            if prop == "Peers":
                return "(<['/org/freedesktop/NetworkManager/WifiP2PPeer/1']>,)"
            return ""

        with mock.patch.object(nm, "_nm_p2p_device_path", return_value="/dev/0"), \
             mock.patch.object(nm, "_nm_get_string", return_value=self.PEER_MAC), \
             mock.patch.object(nm, "_nm_get_property", side_effect=fake_get_property), \
             mock.patch.object(nm, "_nm_start_find"), \
             mock.patch.object(nm, "_nm_stop_find"), \
             mock.patch.object(nm, "_wpas_peer_capabilities", return_value=capabilities), \
             mock.patch.object(nm.time, "sleep"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                found = nm._nm_scan(None, 1)
        return found, buf.getvalue()

    def test_a_miss_against_a_populated_map_is_reported(self):
        found, out = self._scan({"999999999999": (True, False)})
        self.assertIn("No wpa_supplicant peer matched", out)
        self.assertIsNone(found[0].is_group_owner)
        self.assertIsNone(found[0].offers_push_button)

    def test_a_match_says_nothing_and_carries_the_flags(self):
        found, out = self._scan({"aabbccddeeff": (True, False)})
        self.assertNotIn("No wpa_supplicant peer matched", out)
        self.assertIs(found[0].is_group_owner, True)
        self.assertIs(found[0].offers_push_button, False)

    def test_an_empty_map_stays_quiet(self):
        # The ordinary case on an iwd host, or where #104's policy denies the
        # reads. A line per peer there is noise for users this cannot help.
        found, out = self._scan({})
        self.assertNotIn("No wpa_supplicant peer matched", out)
        self.assertIsNone(found[0].is_group_owner)


if __name__ == "__main__":
    unittest.main()
