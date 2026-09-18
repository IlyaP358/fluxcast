#!/usr/bin/env python3
"""Wi-Fi Direct group-formation helpers (hardware-agnostic)."""

from __future__ import annotations

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.p2p import formation
from wfd.p2p.usb_dedicated import (
    _apply_5ghz_oper_preference,
    _is_usb_wifi,
    _wpa_bin,
    wpa_bound_to_iface,
)


GO_NEG_RETRY_WINDOW = """
P2P: Sending GO Negotiation Request
nl80211: CMD_FRAME freq=2437 wait=500 no_cck=1 no_ack=0 offchanok=1
nl80211: Frame TX status event A1=aa:bb:cc:dd:ee:ff stype=13 cookie=0x92 ack=1
P2P: GO Negotiation Request TX callback: success=1
wlp0s20f0u4: Event TX_WAIT_EXPIRE (62) received
P2P: Sending GO Negotiation Request
nl80211: CMD_FRAME freq=2437 wait=500 no_cck=1 no_ack=0 offchanok=1
wlp0s20f0u4: Event TX_WAIT_EXPIRE (62) received
"""

PD_THEN_GO_NEG = """
wlp0s20f0u4: Control interface command 'P2P_PROV_DISC aa:bb:cc:dd:ee:ff pbc'
nl80211: CMD_FRAME freq=2437 wait=200 no_cck=1 no_ack=0 offchanok=1
wlp0s20f0u4: Event TX_WAIT_EXPIRE (62) received
P2P: Sending GO Negotiation Request
nl80211: CMD_FRAME freq=2437 wait=500 no_cck=1 no_ack=0 offchanok=1
P2P: GO Negotiation Request TX callback: success=1
wlp0s20f0u4: Event TX_WAIT_EXPIRE (62) received
"""


class NegOutcomeTest(unittest.TestCase):
    def test_tx_wait_expire_during_go_neg_retry_is_pending(self):
        self.assertEqual(formation.neg_outcome_from_log(GO_NEG_RETRY_WINDOW), "pending")

    def test_pd_tx_wait_does_not_fail_go_neg(self):
        self.assertEqual(formation.neg_outcome_from_log(PD_THEN_GO_NEG), "pending")

    def test_group_started_is_success(self):
        text = GO_NEG_RETRY_WINDOW + "\nP2P-GROUP-STARTED p2p-wlp0s20f0u4-0 GO\n"
        self.assertEqual(formation.neg_outcome_from_log(text), "success")

    def test_go_neg_success_event_is_success(self):
        text = "P2P-GO-NEG-SUCCESS\nP2P: Sending GO Negotiation Confirm\n"
        self.assertEqual(formation.neg_outcome_from_log(text), "success")

    def test_go_neg_failure_event_is_failure(self):
        text = GO_NEG_RETRY_WINDOW + "\nP2P-GO-NEG-FAILURE status=1\n"
        self.assertEqual(formation.neg_outcome_from_log(text), "failure")

    def test_group_formation_failure_is_failure(self):
        text = "P2P-GROUP-FORMATION-FAILURE\n"
        self.assertEqual(formation.neg_outcome_from_log(text), "failure")


class DescribeNegLogTest(unittest.TestCase):
    def test_acked_without_response(self):
        msg = formation.describe_neg_log(GO_NEG_RETRY_WINDOW)
        self.assertIn("ACKed", msg)
        self.assertIn("no Neg Response", msg)

    def test_tx_not_acked(self):
        text = (
            "P2P: Sending GO Negotiation Request\n"
            "P2P: GO Negotiation Request TX callback: success=0\n"
        )
        self.assertIn("not ACKed", formation.describe_neg_log(text))

    def test_sent_without_callback(self):
        text = "P2P: Sending GO Negotiation Request\n"
        self.assertIn("no Response", formation.describe_neg_log(text))


class PeerInfoTest(unittest.TestCase):
    def test_parse_and_listen_freq(self):
        info = formation.parse_p2p_peer_info(
            "device_name=tv-sink\nlisten_freq=2437\nwps_method=not-ready\n"
        )
        self.assertEqual(formation.peer_listen_freq(info), 2437)
        self.assertTrue(formation.peer_wps_not_ready(info))
        info["wps_method"] = "PBC"
        self.assertFalse(formation.peer_wps_not_ready(info))

    def test_listen_freq_falls_back(self):
        self.assertEqual(formation.peer_listen_freq({}), 2437)
        self.assertEqual(formation.freq_to_social_channel(2437), ("81", "6"))
        self.assertEqual(formation.freq_to_social_channel(2412), ("81", "1"))

    def test_iw_phy_caps_and_width_flags(self):
        vht80 = """
Wiphy phy1
Band 1:
        Frequencies:
                * 2412.0 MHz [1]
                * 2437.0 MHz [6]
Band 2:
        VHT Capabilities (0x31800120):
                short GI (40 MHz)
                short GI (80 MHz)
        Frequencies:
                * 5180.0 MHz [36]
                * 5745.0 MHz [149]
"""
        caps = formation.parse_iw_phy_caps(vht80)
        self.assertTrue(caps["has_5ghz"] and caps["has_vht80"] and caps["has_ht40"])
        self.assertEqual(formation.p2p_go_width_flags(caps), ("1", "1"))
        ht20 = "Band 1:\n        Frequencies:\n                * 2437.0 MHz [6]\n"
        caps20 = formation.parse_iw_phy_caps(ht20)
        self.assertFalse(caps20["has_5ghz"] or caps20["has_vht80"])
        self.assertEqual(formation.p2p_go_width_flags(caps20), ("0", "0"))
        self.assertEqual(formation.miracast_go_width_flags(), ("0", "0"))

    def test_peer_advertises_5ghz_from_oper_freq(self):
        self.assertFalse(formation.peer_advertises_5ghz({"listen_freq": "2437"}))
        self.assertTrue(
            formation.peer_advertises_5ghz(
                {"listen_freq": "2437", "oper_freq": "5785"}
            )
        )

    def test_first_quiet_5ghz_skips_sta_cell(self):
        phy = """
        Frequencies:
                * 5180.0 MHz [36] (20.0 dBm)
                * 5220.0 MHz [44] (20.0 dBm)
                * 5745.0 MHz [149] (20.0 dBm)
                * 5805.0 MHz [161] (20.0 dBm) (no IR)
"""
        chans = formation.parse_iw_phy_5ghz_channels(phy)
        self.assertEqual(chans, [(36, 5180), (44, 5220), (149, 5745)])
        occ = formation.parse_sta_occupied_mhz(
            "channel 44 (5220 MHz), width: 80 MHz, center1: 5210 MHz"
        )
        self.assertEqual(occ, (5170, 5250))
        pick = formation.first_quiet_5ghz_oper(chans, occ)
        self.assertEqual(pick, ("124", "149", 5745))

    def test_quietest_2ghz_from_survey(self):
        phy = """
                * 2412.0 MHz [1] (14.0 dBm)
                * 2437.0 MHz [6] (14.0 dBm)
                * 2462.0 MHz [11] (14.0 dBm)
"""
        survey = """
frequency: 2412 MHz
	channel active time: 100 ms
	channel busy time: 80 ms
frequency: 2437 MHz
	channel active time: 100 ms
	channel busy time: 10 ms
frequency: 2462 MHz
	channel active time: 100 ms
	channel busy time: 50 ms
"""
        chans = formation.parse_iw_phy_2ghz_channels(phy)
        busy = formation.parse_iw_survey_busy_ratio(survey)
        self.assertEqual(
            formation.first_quiet_2ghz_oper(chans, None, busy),
            ("81", "6", 2437),
        )

    def test_no_survey_prefers_social_6_not_1(self):
        chans = [(1, 2412), (6, 2437), (11, 2462)]
        self.assertEqual(
            formation.first_quiet_2ghz_oper(chans, None, None),
            ("81", "6", 2437),
        )

    def test_prefer_peer_listen_over_survey(self):
        chans = [(1, 2412), (6, 2437), (11, 2462)]
        busy = {2412: 0.1, 2437: 0.4, 2462: 0.05}
        self.assertEqual(
            formation.first_quiet_2ghz_oper(
                chans, None, busy, prefer_freq=2437
            ),
            ("81", "6", 2437),
        )

    def test_no_go_ranges_keep_listen_splits_around_it(self):
        freqs = [2412, 2437, 2462]
        self.assertEqual(
            formation.p2p_no_go_freq_ranges(freqs, {2437}),
            "2412,2462",
        )
        self.assertEqual(
            formation.p2p_no_go_freq_ranges(freqs, {2412}),
            "2437-2462",
        )

    def test_pending_go_shell(self):
        self.assertTrue(
            formation.is_pending_go_shell("type P2P-GO\naddr aa:bb\n")
        )
        self.assertFalse(
            formation.is_pending_go_shell(
                "type P2P-GO\nssid DIRECT-xx\nchannel 6 (2437 MHz)\n"
            )
        )


class WaitPeerWpsReadyTest(unittest.TestCase):
    def test_returns_when_pbc(self):
        replies = [
            SimpleNamespace(stdout="wps_method=not-ready\nlisten_freq=2437\n"),
            SimpleNamespace(stdout="wps_method=PBC\nlisten_freq=2437\n"),
        ]

        def fake_cli(*_args, **_kwargs):
            return replies.pop(0)

        with mock.patch.object(time, "sleep"):
            info = formation.wait_peer_wps_ready(fake_cli, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(info.get("wps_method"), "PBC")
        self.assertEqual(replies, [])

    def test_timeout_returns_last_not_ready(self):
        def fake_cli(*_args, **_kwargs):
            return SimpleNamespace(stdout="wps_method=not-ready\n")

        with mock.patch.object(time, "sleep"):
            info = formation.wait_peer_wps_ready(
                fake_cli, "aa:bb:cc:dd:ee:ff", timeout_s=0.0
            )
        self.assertTrue(formation.peer_wps_not_ready(info))


class UsbIfaceTest(unittest.TestCase):
    def test_udev_usb_name_is_usb(self):
        self.assertTrue(_is_usb_wifi("wlp0s20f0u4"))

    def test_ax201_sta_is_not_usb(self):
        self.assertFalse(_is_usb_wifi("wlp0s20f3"))


class CompetingWpaTest(unittest.TestCase):
    def test_bound_to_usb_iface(self):
        cmd = (
            "wpa_supplicant -B -i wlp0s20f0u4 -c /tmp/wpa-usb-p2p/wpa.conf "
            "-P /tmp/wpa-usb-p2p/wpa.pid"
        )
        self.assertTrue(wpa_bound_to_iface(cmd, "wlp0s20f0u4"))
        self.assertFalse(wpa_bound_to_iface(cmd, "wlp0s20f3"))

    def test_system_dbus_wpa_is_not_bound(self):
        cmd = "/usr/bin/wpa_supplicant -u -s -O /run/wpa_supplicant"
        self.assertFalse(wpa_bound_to_iface(cmd, "wlp0s20f0u4"))
        self.assertFalse(wpa_bound_to_iface(cmd, "wlp0s20f3"))

    def test_primary_sta_wpa_not_matched_as_usb(self):
        cmd = "wpa_supplicant -B -i wlp0s20f3 -c /etc/wpa.conf"
        self.assertTrue(wpa_bound_to_iface(cmd, "wlp0s20f3"))
        self.assertFalse(wpa_bound_to_iface(cmd, "wlp0s20f0u4"))

    def test_wpa_bin_env_override(self):
        with mock.patch.dict(
            os.environ, {"FLUXCAST_USB_WPA": "/opt/local/wpa_supplicant"}
        ):
            self.assertEqual(_wpa_bin(), "/opt/local/wpa_supplicant")


PHY_DUAL_BAND = """
Wiphy phy1
Band 1:
        Frequencies:
                * 2412.0 MHz [1] (14.0 dBm)
                * 2437.0 MHz [6] (14.0 dBm)
                * 2462.0 MHz [11] (14.0 dBm)
Band 2:
        Frequencies:
                * 5180.0 MHz [36] (20.0 dBm)
                * 5745.0 MHz [149] (20.0 dBm)
"""

SURVEY_CH6_QUIET = """
frequency: 2412 MHz
	channel active time: 100 ms
	channel busy time: 80 ms
frequency: 2437 MHz
	channel active time: 100 ms
	channel busy time: 10 ms
frequency: 2462 MHz
	channel active time: 100 ms
	channel busy time: 50 ms
"""


class UsbGo5GhzPreferenceTest(unittest.TestCase):
    def _run_pref(self, go_5ghz, peer_info):
        cli = mock.Mock()
        survey = SimpleNamespace(stdout=SURVEY_CH6_QUIET)
        with mock.patch(
            "wfd.p2p.usb_dedicated._iw_phy_text", return_value=PHY_DUAL_BAND
        ), mock.patch(
            "wfd.p2p.usb_dedicated._sta_occupied_mhz", return_value=None
        ), mock.patch(
            "wfd.p2p.usb_dedicated._phy_of", return_value="phy1"
        ), mock.patch(
            "wfd.p2p.usb_dedicated._run", return_value=survey
        ):
            _apply_5ghz_oper_preference(
                cli, "wlp0s20f0u4", peer_info, go_5ghz=go_5ghz
            )
        return [(c.args) for c in cli.call_args_list]

    def test_default_excludes_other_go_freqs_and_prefers_listen_24(self):
        sets = self._run_pref(False, {"listen_freq": "2437", "oper_freq": "5785"})
        self.assertIn(("set", "p2p_no_go_freq", "2412,2462,5180-5745"), sets)
        self.assertIn(("set", "p2p_pref_chan", "81:6"), sets)
        self.assertIn(("set", "p2p_oper_reg_class", "81"), sets)
        self.assertIn(("set", "p2p_oper_channel", "6"), sets)

    def test_env_2ghz_mhz_overrides_listen(self):
        with mock.patch.dict(
            "os.environ", {"FLUXCAST_WFD_GO_2GHZ_MHZ": "2462"}
        ):
            sets = self._run_pref(
                False, {"listen_freq": "2437", "oper_freq": "5785"}
            )
        self.assertIn(("set", "p2p_no_go_freq", "2412-2437,5180-5745"), sets)
        self.assertIn(("set", "p2p_pref_chan", "81:11"), sets)
        self.assertIn(("set", "p2p_oper_channel", "11"), sets)

    def test_go_5ghz_clears_exclude_and_prefers_5ghz_when_peer_ads(self):
        sets = self._run_pref(True, {"listen_freq": "2437", "oper_freq": "5785"})
        self.assertIn(("set", "p2p_no_go_freq", ""), sets)
        self.assertIn(("set", "p2p_pref_chan", ""), sets)
        self.assertNotIn(("set", "p2p_no_go_freq", "5180-5745"), sets)
        self.assertIn(("set", "p2p_oper_reg_class", "115"), sets)
        self.assertIn(("set", "p2p_oper_channel", "36"), sets)

    def test_go_5ghz_without_peer_5ghz_keeps_24_oper_without_exclude(self):
        sets = self._run_pref(True, {"listen_freq": "2437"})
        self.assertIn(("set", "p2p_no_go_freq", ""), sets)
        self.assertIn(("set", "p2p_oper_reg_class", "81"), sets)
        self.assertIn(("set", "p2p_oper_channel", "6"), sets)


if __name__ == "__main__":
    unittest.main()
