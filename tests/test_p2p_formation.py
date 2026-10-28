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
from wfd.p2p.formation import (
    freq_to_p2p_class_channel,
    PAYLOAD_CSA_24,
    PAYLOAD_STAY,
    POST_CSA_STA_WAIT_S,
    go_intents_for_radio,
    ap_sta_connect_count,
    csa_dhcp_still_waiting,
    csa_sta_followed,
    go_sta_connected_in_delta,
    go_sta_is_present,
    go_sta_ready_for_l3,
    go_width_flags_for_radio,
    plan_payload_channel,
    listen_freq_for_wps,
    parse_iw_phy_radio,
    peer_advertises_24ghz,
    peer_advertised_freqs,
    radio_can_stream_5ghz,
    radio_meets_wfd_24ghz,
    should_csa_go_to_2ghz,
    wfd_radio_reject_reason,
    usb_discovery_steps,
)
from wfd.p2p.usb_dedicated import (
    _apply_5ghz_oper_preference,
    _cached_listen_mhz,
    _ensure_wpa_conf,
    _is_usb_wifi,
    _persistent_go_ids,
    _usb_go_intents,
    _wpa_bin,
    iface_is_sta_uplink,
    needs_dedicated_wpa,
    parse_default_route_iface,
    mark_p2p_csa_nofollow,
    mark_p2p_invite_ok,
    mark_p2p_persistent_ok,
    peer_has_csa_nofollow,
    peer_has_invite_ok,
    peer_has_persistent_ok,
    run_mid_session_csa,
    schedule_mid_session_csa,
    should_abandon_rtsp_wait,
    should_invite_before_rtsp,
    wpa_bound_to_iface,
    _peer_macs_from_neg_log,
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

    def test_wps_keeps_cached_5ghz_after_24_find(self):
        self.assertEqual(
            listen_freq_for_wps({"listen_freq": "2462"}, 5745),
            5745,
        )
        self.assertEqual(
            listen_freq_for_wps({"listen_freq": "5745"}, 2437),
            5745,
        )
        self.assertEqual(
            listen_freq_for_wps({"listen_freq": "2437"}, None),
            2437,
        )
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


class DedicatedWpaGateTest(unittest.TestCase):
    def test_parse_ip_route_default(self):
        text = (
            "default via 192.168.10.1 dev wlp0s20f3 proto dhcp "
            "src 192.168.10.106 metric 600\n"
        )
        self.assertEqual(parse_default_route_iface(text, ""), "wlp0s20f3")

    def test_parse_proc_net_route(self):
        proc = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\t"
            "Mask\tMTU\tWindow\tIRTT\n"
            "wlp0s20f3\t00000000\t010AA8C0\t0003\t0\t0\t600\t"
            "00000000\t0\t0\t0\n"
        )
        self.assertEqual(parse_default_route_iface("", proc), "wlp0s20f3")

    def test_uplink_never_dedicated_even_with_usb_wpa_env(self):
        env = {
            "FLUXCAST_USB_WPA": "/opt/wpa_supplicant",
            "FLUXCAST_DEDICATED_WPA": "1",
        }
        self.assertFalse(
            needs_dedicated_wpa(
                "wlp0s20f3", env=env, uplink="wlp0s20f3", managed="yes"
            )
        )

    def test_usb_secondary_is_dedicated(self):
        self.assertTrue(
            needs_dedicated_wpa(
                "wlp0s20f0u4", env={}, uplink="wlp0s20f3", managed="yes"
            )
        )

    def test_usb_that_is_uplink_is_not_dedicated(self):
        self.assertFalse(
            needs_dedicated_wpa(
                "wlp0s20f0u4", env={}, uplink="wlp0s20f0u4", managed="yes"
            )
        )

    def test_empty_nmcli_fail_closed(self):
        self.assertFalse(
            needs_dedicated_wpa(
                "wlp0s20f3", env={}, uplink="wlan1", managed=""
            )
        )

    def test_unmanaged_non_uplink_is_dedicated(self):
        self.assertTrue(
            needs_dedicated_wpa(
                "wlan1", env={}, uplink="wlp0s20f3", managed="no"
            )
        )

    def test_usb_wpa_env_alone_does_not_force_sta(self):
        env = {"FLUXCAST_USB_WPA": "/home/colin/vendor/wpa_supplicant"}
        self.assertFalse(
            needs_dedicated_wpa(
                "wlp0s20f3", env=env, uplink="wlp0s20f3", managed="yes"
            )
        )

    def test_iface_is_sta_uplink(self):
        self.assertTrue(iface_is_sta_uplink("wlp0s20f3", uplink="wlp0s20f3"))
        self.assertFalse(iface_is_sta_uplink("wlp0s20f0u4", uplink="wlp0s20f3"))
        self.assertFalse(iface_is_sta_uplink("wlp0s20f3", uplink=""))

    def test_restore_sta_uplink_remanages_and_ups_connection(self):
        from wfd.p2p import usb_dedicated as ud

        calls: list[list[str]] = []

        def fake_run(cmd, timeout=10.0):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        snap = {
            "iface": "wlp0s20f3",
            "connection": "ChrisNet_Wireless-5GHz",
            "managed": "yes",
            "default_iface": "wlp0s20f3",
            "was_uplink": True,
        }
        with mock.patch.object(ud, "_run", side_effect=fake_run), mock.patch.object(
            ud, "_phy_of", return_value="phy0"
        ), mock.patch.object(ud, "load_sta_snapshot", return_value=snap):
            ud.restore_sta_uplink("wlp0s20f3")
        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("device set wlp0s20f3 managed yes" in j for j in joined))
        self.assertTrue(
            any("connection up ChrisNet_Wireless-5GHz" in j for j in joined)
        )


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
Available Antennas: TX 0x1 RX 0x1
Band 1:
        HT TX/RX MCS rate indexes supported: 0-7
        Frequencies:
                * 2412.0 MHz [1] (14.0 dBm)
                * 2437.0 MHz [6] (14.0 dBm)
                * 2462.0 MHz [11] (14.0 dBm)
Band 2:
        HT TX/RX MCS rate indexes supported: 0-7
        Frequencies:
                * 5180.0 MHz [36] (16.0 dBm)
                * 5745.0 MHz [149] (16.0 dBm)
"""

PHY_2SS = """
Wiphy phy2
Available Antennas: TX 0x3 RX 0x3
Band 1:
        HT TX/RX MCS rate indexes supported: 0-15
        Frequencies:
                * 2412.0 MHz [1] (20.0 dBm)
                * 2437.0 MHz [6] (20.0 dBm)
                * 2462.0 MHz [11] (20.0 dBm)
Band 2:
        HT TX/RX MCS rate indexes supported: 0-15
        VHT Capabilities (0x31800120):
                short GI (40 MHz)
                short GI (80 MHz)
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


class P2pClassChannelTest(unittest.TestCase):
    def test_24_social(self):
        self.assertEqual(freq_to_p2p_class_channel(2412), ("81", "1"))
        self.assertEqual(freq_to_p2p_class_channel(2437), ("81", "6"))
        self.assertEqual(freq_to_p2p_class_channel(2462), ("81", "11"))

    def test_5ghz_does_not_collapse_to_class_81(self):
        self.assertEqual(freq_to_p2p_class_channel(5180), ("115", "36"))
        self.assertEqual(freq_to_p2p_class_channel(5745), ("124", "149"))

    def test_peer_channel_list_and_listen(self):
        freqs = peer_advertised_freqs(
            {
                "listen_freq": "5745",
                "channel_list": "81:1,6,11 115:36,40,44,48 124:149",
            }
        )
        self.assertIn(2437, freqs)
        self.assertIn(5745, freqs)
        self.assertIn(5180, freqs)


class UsbGo5GhzPreferenceTest(unittest.TestCase):
    def _run_pref(self, go_5ghz, peer_info, phy=PHY_DUAL_BAND):
        cli = mock.Mock()
        survey = SimpleNamespace(stdout=SURVEY_CH6_QUIET)
        with mock.patch(
            "wfd.p2p.usb_dedicated._iw_phy_text", return_value=phy
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

    def test_5ghz_listen_keeps_peer_channel_in_go_set(self):
        sets = self._run_pref(False, {"listen_freq": "5745"})
        self.assertIn(("set", "p2p_no_go_freq", ""), sets)
        self.assertIn(("set", "p2p_oper_reg_class", "124"), sets)
        self.assertIn(("set", "p2p_oper_channel", "149"), sets)

    def test_go_5ghz_without_peer_5ghz_keeps_24_oper_without_exclude(self):
        sets = self._run_pref(True, {"listen_freq": "2437"})
        self.assertIn(("set", "p2p_no_go_freq", ""), sets)
        self.assertIn(("set", "p2p_oper_reg_class", "81"), sets)
        self.assertIn(("set", "p2p_oper_channel", "6"), sets)

    def test_2ss_radio_keeps_5ghz_when_peer_ads_it(self):
        sets = self._run_pref(
            False,
            {"listen_freq": "2437", "oper_freq": "5745"},
            phy=PHY_2SS,
        )
        self.assertIn(("set", "p2p_no_go_freq", ""), sets)
        self.assertIn(("set", "p2p_oper_reg_class", "124"), sets)
        self.assertIn(("set", "p2p_oper_channel", "149"), sets)


class RadioCapabilityTest(unittest.TestCase):
    def test_parse_1ss_and_2ss(self):
        one = parse_iw_phy_radio(PHY_DUAL_BAND)
        self.assertEqual(one["nss"], 1)
        self.assertEqual(one["antennas_tx"], 1)
        self.assertFalse(radio_can_stream_5ghz(one))
        two = parse_iw_phy_radio(PHY_2SS)
        self.assertEqual(two["nss"], 2)
        self.assertEqual(two["antennas_tx"], 2)
        self.assertTrue(radio_can_stream_5ghz(two))

    def test_width_20_on_1ss_ht40_on_2ss(self):
        self.assertEqual(go_width_flags_for_radio(parse_iw_phy_radio(PHY_DUAL_BAND)), ("0", "0"))
        self.assertEqual(go_width_flags_for_radio(parse_iw_phy_radio(PHY_2SS)), ("1", "1"))

    def test_csa_1ss_5ghz_not_2ss(self):
        weak = parse_iw_phy_radio(PHY_DUAL_BAND)
        strong = parse_iw_phy_radio(PHY_2SS)
        self.assertTrue(
            should_csa_go_to_2ghz(
                go_mhz=5745, radio=weak, sta_signal_dbm=None,
                peer_has_24=True, prefer_5ghz=False,
            )
        )
        self.assertFalse(
            should_csa_go_to_2ghz(
                go_mhz=5745, radio=strong, sta_signal_dbm=-50,
                peer_has_24=True, prefer_5ghz=False,
            )
        )
        self.assertFalse(
            should_csa_go_to_2ghz(
                go_mhz=2437, radio=weak, sta_signal_dbm=None,
                peer_has_24=True, prefer_5ghz=False,
            )
        )
        # 1ss CSAs even at a momentarily decent RSSI; --wfd-go-5ghz stays.
        self.assertTrue(
            should_csa_go_to_2ghz(
                go_mhz=5745, radio=weak, sta_signal_dbm=-65,
                peer_has_24=True, prefer_5ghz=False,
            )
        )
        self.assertTrue(
            should_csa_go_to_2ghz(
                go_mhz=5745, radio=weak, sta_signal_dbm=-65,
                peer_has_24=True, prefer_5ghz=True,
            )
        )
        self.assertFalse(
            should_csa_go_to_2ghz(
                go_mhz=5745, radio=weak, sta_signal_dbm=-65,
                peer_has_24=False, prefer_5ghz=False,
            )
        )

    def test_listen_only_5ghz_snapshot_still_has_24(self):
        self.assertTrue(peer_advertises_24ghz({"listen_freq": "5745"}))
        self.assertTrue(
            peer_advertises_24ghz(
                {"listen_freq": "5745", "channel_list": "81:1,6,11 124:149"}
            )
        )
        self.assertFalse(
            peer_advertises_24ghz(
                {"listen_freq": "5745", "channel_list": "124:149"}
            )
        )

    def test_go_sta_present_from_log_and_dump(self):
        self.assertFalse(go_sta_is_present("", ""))
        self.assertTrue(go_sta_is_present("p2p-0: AP-STA-CONNECTED 66:ab", ""))
        self.assertFalse(
            go_sta_is_present(
                "AP-STA-CONNECTED 66:ab\nAP-STA-DISCONNECTED 66:ab",
                "",
            )
        )
        self.assertTrue(
            go_sta_is_present(
                "AP-STA-CONNECTED 66:ab\nAP-STA-DISCONNECTED 66:ab\n"
                "AP-STA-CONNECTED 66:ab",
                "",
            )
        )
        self.assertTrue(
            go_sta_is_present("", "Station 66:ab:14:3d:6e:e4 (on p2p-0)\n")
        )

    def test_l3_rejects_stale_inactive_sta(self):
        dump_stale = (
            "Station 66:ab:14:3d:6e:e4 (on p2p-0)\n"
            "\tinactive time:\t20831 ms\n"
        )
        dump_live = (
            "Station 66:ab:14:3d:6e:e4 (on p2p-0)\n"
            "\tinactive time:\t1164 ms\n"
        )
        self.assertFalse(go_sta_ready_for_l3("AP-STA-CONNECTED 66:ab", dump_stale))
        self.assertTrue(go_sta_ready_for_l3("AP-STA-CONNECTED 66:ab", dump_live))
        self.assertGreaterEqual(POST_CSA_STA_WAIT_S, 24.0)
        self.assertLessEqual(POST_CSA_STA_WAIT_S, 40.0)

    def test_post_csa_rejoin_needs_connect_in_delta(self):
        self.assertFalse(go_sta_connected_in_delta(""))
        self.assertFalse(
            go_sta_connected_in_delta("AP-STA-DISCONNECTED 66:ab")
        )
        self.assertTrue(
            go_sta_connected_in_delta(
                "AP-STA-DISCONNECTED 66:ab\nAP-STA-CONNECTED 66:ab"
            )
        )
        self.assertTrue(go_sta_connected_in_delta("AP-STA-CONNECTED 66:ab"))
        self.assertFalse(
            go_sta_connected_in_delta(
                "AP-STA-CONNECTED 66:ab\nAP-STA-DISCONNECTED 66:ab"
            )
        )
        self.assertEqual(
            ap_sta_connect_count(
                "p2p-0: AP-STA-CONNECTED 66:ab\nAP-STA-CONNECTED 66:ab\n"
            ),
            1,
        )

    def test_csa_follow_requires_same_assoc_on_target(self):
        self.assertTrue(
            csa_sta_followed(
                go_mhz=2437, target_mhz=2437, disconnected=False,
                inactive_ms=800, sta_present=True,
            )
        )
        self.assertFalse(
            csa_sta_followed(
                go_mhz=2437, target_mhz=2437, disconnected=True,
                inactive_ms=800, sta_present=True,
            )
        )
        self.assertFalse(
            csa_sta_followed(
                go_mhz=2437, target_mhz=2437, disconnected=False,
                inactive_ms=8000, sta_present=True,
            )
        )
        self.assertFalse(
            csa_sta_followed(
                go_mhz=2437, target_mhz=2437, disconnected=False,
                inactive_ms=2500, sta_present=True,
            )
        )
        self.assertFalse(
            csa_sta_followed(
                go_mhz=5745, target_mhz=2437, disconnected=False,
                inactive_ms=400, sta_present=True,
            )
        )

    def test_csa_dhcp_waits_for_second_four_way(self):
        # First join, 8s quiet, still inside 32s churn window.
        self.assertTrue(
            csa_dhcp_still_waiting(
                connected=True,
                seconds_since_last_connect=8.0,
                seconds_since_first_connect=10.0,
                extra_four_ways=0,
            )
        )
        # First join survived the churn window.
        self.assertFalse(
            csa_dhcp_still_waiting(
                connected=True,
                seconds_since_last_connect=8.0,
                seconds_since_first_connect=32.0,
                extra_four_ways=0,
            )
        )
        # Second 4-way just happened.
        self.assertTrue(
            csa_dhcp_still_waiting(
                connected=True,
                seconds_since_last_connect=2.0,
                seconds_since_first_connect=30.0,
                extra_four_ways=1,
            )
        )
        self.assertFalse(
            csa_dhcp_still_waiting(
                connected=True,
                seconds_since_last_connect=8.0,
                seconds_since_first_connect=36.0,
                extra_four_ways=1,
            )
        )

    def test_payload_plan_csa_requires_sta(self):
        weak = parse_iw_phy_radio(PHY_DUAL_BAND)
        kwargs = dict(
            go_mhz=5745,
            radio=weak,
            sta_signal_dbm=None,
            peer_has_24=True,
            prefer_5ghz=False,
        )
        self.assertEqual(
            plan_payload_channel(**kwargs, sta_present=False),
            PAYLOAD_STAY,
        )
        self.assertEqual(
            plan_payload_channel(**kwargs, sta_present=True),
            PAYLOAD_CSA_24,
        )
        strong = parse_iw_phy_radio(PHY_2SS)
        self.assertEqual(
            plan_payload_channel(
                go_mhz=5745,
                radio=strong,
                sta_signal_dbm=-50,
                peer_has_24=True,
                prefer_5ghz=False,
                sta_present=True,
            ),
            PAYLOAD_STAY,
        )

    def test_weak_5ghz_phy_rejected_strong_allowed(self):
        weak = parse_iw_phy_radio(PHY_DUAL_BAND)
        strong = parse_iw_phy_radio(PHY_2SS)
        self.assertTrue(radio_meets_wfd_24ghz(weak))
        # 5 GHz listen + usable 2.4 payload: allow (form 5 GHz, CSA to 2.4).
        self.assertIsNone(wfd_radio_reject_reason(weak, 5745, peer_has_24=True))
        self.assertIsNotNone(
            wfd_radio_reject_reason(weak, 5745, peer_has_24=False)
        )
        self.assertIsNone(wfd_radio_reject_reason(weak, 2437))
        self.assertIsNone(
            wfd_radio_reject_reason(weak, 5745, allow_weak=True)
        )
        self.assertIsNone(wfd_radio_reject_reason(strong, 5745))

    def test_intents_follow_radio_not_bus(self):
        self.assertEqual(go_intents_for_radio(0, {"nss": 1, "antennas_tx": 1}), [15])
        self.assertEqual(go_intents_for_radio(0, {"nss": 2, "antennas_tx": 2}), [0, 15])


class UsbDiscoveryStepsTest(unittest.TestCase):
    def test_5ghz_cache_finds_listen_channel_first(self):
        steps = usb_discovery_steps(5745)
        self.assertEqual(steps[0]["mode"], "find_freq")
        self.assertEqual(steps[0]["freq"], 5745)
        self.assertEqual(steps[1]["mode"], "find_social")
        self.assertGreater(
            [s["mode"] for s in steps].index("listen"),
            [s["mode"] for s in steps].index("find_freq"),
        )

    def test_unknown_cache_finds_then_listen_24(self):
        steps = usb_discovery_steps(None)
        self.assertEqual(steps[0]["mode"], "find_social")
        self.assertEqual(steps[1]["mode"], "listen")
        self.assertEqual(steps[1]["freq"], 2437)
        self.assertLess(sum(int(s["seconds"]) for s in steps), 40)


class PersistentPlayMarkerTest(unittest.TestCase):
    def test_play_marker_roundtrip(self):
        import tempfile
        from pathlib import Path
        from wfd.p2p import usb_dedicated as u

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(u, "_state_dir", return_value=Path(tmp)):
                self.assertFalse(peer_has_persistent_ok("AA:BB:CC:DD:EE:FF"))
                mark_p2p_persistent_ok("AA:BB:CC:DD:EE:FF")
                self.assertTrue(peer_has_persistent_ok("aa:bb:cc:dd:ee:ff"))

    def test_invite_ok_is_separate_from_play(self):
        import tempfile
        from pathlib import Path
        from wfd.p2p import usb_dedicated as u

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(u, "_state_dir", return_value=Path(tmp)):
                mark_p2p_persistent_ok("AA:BB:CC:DD:EE:FF")
                self.assertFalse(peer_has_invite_ok("AA:BB:CC:DD:EE:FF"))
                mark_p2p_invite_ok("AA:BB:CC:DD:EE:FF")
                self.assertTrue(peer_has_invite_ok("aa:bb:cc:dd:ee:ff"))

    def test_csa_nofollow_is_separate_from_play_and_invite(self):
        import tempfile
        from pathlib import Path
        from wfd.p2p import usb_dedicated as u

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(u, "_state_dir", return_value=Path(tmp)):
                mark_p2p_persistent_ok("AA:BB:CC:DD:EE:FF")
                mark_p2p_invite_ok("AA:BB:CC:DD:EE:FF")
                self.assertFalse(peer_has_csa_nofollow("AA:BB:CC:DD:EE:FF"))
                mark_p2p_csa_nofollow("AA:BB:CC:DD:EE:FF")
                self.assertTrue(peer_has_csa_nofollow("aa:bb:cc:dd:ee:ff"))
                self.assertFalse(peer_has_csa_nofollow("11:22:33:44:55:66"))

    def test_csa_nofollow_skips_schedule_and_run(self):
        import tempfile
        from pathlib import Path
        from wfd.p2p import usb_dedicated as u

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(u, "_state_dir", return_value=Path(tmp)):
                mark_p2p_csa_nofollow("AA:BB:CC:DD:EE:FF")
                with mock.patch.object(u.threading, "Thread") as th:
                    schedule_mid_session_csa(peer_mac="aa:bb:cc:dd:ee:ff")
                    th.assert_not_called()
                    schedule_mid_session_csa(peer_mac="11:22:33:44:55:66")
                    th.assert_called_once()
                with mock.patch.object(u, "_operating_go_iface") as go:
                    self.assertFalse(
                        run_mid_session_csa(peer_mac="AA:BB:CC:DD:EE:FF")
                    )
                    go.assert_not_called()


class GoAclMacTest(unittest.TestCase):
    def test_neg_log_device_and_iface_macs(self):
        delta = (
            "P2P-GO-NEG-SUCCESS role=GO freq=5745 ht40=0 "
            "peer_dev=62:ab:14:3d:6e:e4 peer_iface=66:ab:14:3d:6e:e4 "
            "wps_method=PBC"
        )
        self.assertEqual(
            _peer_macs_from_neg_log(delta),
            ["62:ab:14:3d:6e:e4", "66:ab:14:3d:6e:e4"],
        )


class RtspJoinWatchTest(unittest.TestCase):
    def test_invite_when_sta_left_before_m1(self):
        self.assertTrue(
            should_invite_before_rtsp(
                has_rtsp=False,
                sta_present=False,
                lease_age_s=5.0,
                invites_sent=0,
            )
        )
        self.assertFalse(
            should_invite_before_rtsp(
                has_rtsp=True,
                sta_present=False,
                lease_age_s=5.0,
                invites_sent=0,
            )
        )
        self.assertFalse(
            should_invite_before_rtsp(
                has_rtsp=False,
                sta_present=True,
                lease_age_s=5.0,
                invites_sent=0,
            )
        )
        self.assertFalse(
            should_invite_before_rtsp(
                has_rtsp=False,
                sta_present=False,
                lease_age_s=5.0,
                invites_sent=2,
            )
        )
        self.assertFalse(
            should_invite_before_rtsp(
                has_rtsp=False,
                sta_present=False,
                lease_age_s=1.0,
                invites_sent=0,
            )
        )

    def test_abandon_wait_after_timeout_without_m1(self):
        self.assertFalse(
            should_abandon_rtsp_wait(has_rtsp=False, lease_age_s=10.0)
        )
        self.assertTrue(
            should_abandon_rtsp_wait(has_rtsp=False, lease_age_s=35.0)
        )
        self.assertFalse(
            should_abandon_rtsp_wait(has_rtsp=True, lease_age_s=40.0)
        )


class PersistentGroupConfTest(unittest.TestCase):
    def test_parse_persistent_ids(self):
        text = (
            "network id / ssid / bssid / flags\n"
            "0\tDIRECT-xx-kenya\tany\t[DISABLED][P2P-PERSISTENT]\n"
            "1\toffice\tany\t[CURRENT]\n"
        )
        self.assertEqual(_persistent_go_ids(text), [0])

    def test_write_wpa_conf_falls_back_to_privileged(self):
        import tempfile
        from pathlib import Path
        from wfd.p2p import usb_dedicated as u

        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "usb-wpa.conf"
            conf.write_text("old\n")
            conf.chmod(0o444)
            with mock.patch.object(u, "run_privileged", return_value=mock.Mock(returncode=0, stdout="")) as rp:
                u._write_wpa_conf(conf, "new\n")
            self.assertTrue(any(c[0][0][:1] == ["cp"] or "cp" in c[0][0] for c in rp.call_args_list) or rp.called)

    def test_ensure_wpa_conf_keeps_networks(self):
        import tempfile
        from pathlib import Path
        from wfd.p2p import usb_dedicated as u

        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "usb-wpa.conf"
            conf.write_text(
                "ctrl_interface=/old\ndevice_name=old\n\n"
                "network={\n\tssid=\"DIRECT-ab\"\n\tdisabled=2\n\tmode=3\n}\n"
            )
            with mock.patch.object(u, "_conf_path", return_value=conf):
                u._ensure_wpa_conf()
            text = conf.read_text()
            self.assertIn("update_config=1", text)
            self.assertIn("persistent_reconnect=1", text)
            self.assertIn('ssid="DIRECT-ab"', text)
            self.assertIn("disabled=2", text)


class UsbGoIntentTest(unittest.TestCase):
    def test_1ss_stays_go(self):
        weak = {"nss": 1, "antennas_tx": 1}
        self.assertEqual(_usb_go_intents(0, weak), [15])
        self.assertEqual(_usb_go_intents(15, weak), [15])

    def test_2ss_honors_requested_intent(self):
        strong = {"nss": 2, "antennas_tx": 2, "has_5ghz": True}
        self.assertEqual(_usb_go_intents(0, strong), [0, 15])
        self.assertEqual(_usb_go_intents(15, strong), [15, 0])

    def test_cached_listen_from_env(self):
        with mock.patch.dict("os.environ", {"FLUXCAST_PEER_LISTEN_MHZ": "5745"}):
            self.assertEqual(_cached_listen_mhz("62:ab:14:3d:6e:e4"), 5745)


if __name__ == "__main__":
    unittest.main()
