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
from wfd.p2p import nm, peers  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
