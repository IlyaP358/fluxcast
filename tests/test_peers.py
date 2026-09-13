import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDNotReady  # noqa: E402
from wfd.ie import WFDPeer  # noqa: E402
from wfd.p2p import nm, peers  # noqa: E402


# What gdbus prints for a peer that advertises no Wi-Fi Display data at all.
# Empty array, non-empty string - the whole reason #124 existed.
EMPTY_WFD_IES = "(<@ay []>,)"

# A real sink's Device Information subelement: id 0, length 6, then the
# device-info bitmap and an RTSP port of 7236 (0x1c44).
SINK_WFD_IES = (
    "(<@ay [byte 0x00, byte 0x00, byte 0x06, byte 0x00, byte 0x11, "
    "byte 0x1c, byte 0x44, byte 0x00, byte 0x00]>,)"
)


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

    def test_non_capable_peer_is_called_out(self):
        out = _capture(peers.print_scan, [
            WFDPeer(address="5E:3A:45:D2:2F:3B", name="DIRECT-3b-HP M227f LaserJet",
                    source="NetworkManager", wfd_capable=False),
        ])
        self.assertIn("probably not a Miracast sink", out)
        self.assertNotIn("WFD capability data detected", out)

    def test_details_string_alone_does_not_imply_capability(self):
        # The old check matched "wfd_ies=" anywhere in details. Nothing may
        # reintroduce that coupling.
        out = _capture(peers.print_scan, [
            WFDPeer(address="AA:BB:CC:DD:EE:FF",
                    details="model=X; wfd_ies=(<@ay []>,); sink_rtsp_port=7236",
                    wfd_capable=False),
        ])
        self.assertIn("probably not a Miracast sink", out)


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
        self.assertNotIn("probably not a Miracast sink", out)
        self.assertNotIn("WFD capability data detected", out)

    def test_real_sink_is_capable(self):
        found = self._scan_with_wfd_ies(SINK_WFD_IES)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].wfd_capable)
        self.assertIn("wfd_ies=", found[0].details)
        self.assertEqual(found[0].rtsp_port, 7236)


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
        found = self._scan_with_details("device_name=TV\nwfd_dev_info=00061c440000\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].wfd_capable)

    def test_failed_detail_lookup_is_unknown_not_incapable(self):
        """A p2p_peer lookup that times out means we do not know. Reporting
        False there would label a real sink "probably not a Miracast sink"
        and steer the user away from the device that would have worked.
        """
        found = self._scan_with_details("")
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].wfd_capable)
        out = _capture(peers.print_scan, found)
        self.assertNotIn("probably not a Miracast sink", out)
        self.assertNotIn("WFD capability data detected", out)


if __name__ == "__main__":
    unittest.main()
