#!/usr/bin/env python3
"""Smart default Wi-Fi iface selection for Miracast P2P."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from wfd.p2p import peers as peers_mod


class DefaultWifiInterfaceTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_INTERFACE", None)
        os.environ.pop("FLUXCAST_LIST_P2P_RADIOS", None)

    def test_env_override_wins(self):
        os.environ["FLUXCAST_WFD_INTERFACE"] = "wlan9"
        self.assertEqual(peers_mod._default_wifi_interface(), "wlan9")

    def test_prefers_idle_p2p_go(self):
        os.environ.pop("FLUXCAST_WFD_INTERFACE", None)
        os.environ["FLUXCAST_LIST_P2P_RADIOS"] = ""  # force inline path

        iw_dev = """
Interface wlp0s20f3
        type managed
Interface wlan1
        type managed
Interface p2p-wlp0s20f3-0
        type P2P-GO
"""

        def fake_run(cmd, timeout=3.0):
            class R:
                stdout = ""
                returncode = 0

            if cmd[:2] == ["iw", "dev"]:
                r = R()
                r.stdout = iw_dev
                return r
            if cmd[:2] == ["iw", "phy"]:
                phy = cmd[2]
                r = R()
                # phy0 = busy STA; phy1 = idle dongle with P2P-GO
                if phy == "phy1":
                    r.stdout = "Supported interface modes:\n\t * managed\n\t * P2P-GO\nBand 1:\n"
                else:
                    r.stdout = "Supported interface modes:\n\t * managed\n\t * P2P-GO\nBand 1:\n"
                return r
            if cmd[0] == "nmcli":
                r = R()
                r.stdout = (
                    "wlp0s20f3:wifi:connected:ChrisNet\n"
                    "wlan1:wifi:disconnected:\n"
                )
                return r
            return R()

        def fake_phy(iface):
            return {"wlp0s20f3": "phy0", "wlan1": "phy1"}.get(iface)

        with mock.patch.object(peers_mod, "_run", side_effect=fake_run):
            with mock.patch.object(peers_mod, "_iface_phy", side_effect=fake_phy):
                with mock.patch.object(peers_mod.shutil, "which", return_value="/usr/bin/iw"):
                    self.assertEqual(peers_mod._default_wifi_interface(), "wlan1")

    def test_falls_back_to_busy_go_when_no_idle(self):
        os.environ.pop("FLUXCAST_WFD_INTERFACE", None)

        iw_dev = """
Interface wlp0s20f3
        type managed
"""

        def fake_run(cmd, timeout=3.0):
            class R:
                stdout = ""
                returncode = 0

            if cmd[:2] == ["iw", "dev"]:
                r = R()
                r.stdout = iw_dev
                return r
            if cmd[:2] == ["iw", "phy"]:
                r = R()
                r.stdout = "Supported interface modes:\n\t * managed\n\t * P2P-GO\nBand 1:\n"
                return r
            if cmd[0] == "nmcli":
                r = R()
                r.stdout = "wlp0s20f3:wifi:connected:ChrisNet\n"
                return r
            return R()

        with mock.patch.object(peers_mod, "_run", side_effect=fake_run):
            with mock.patch.object(peers_mod, "_iface_phy", return_value="phy0"):
                with mock.patch.object(peers_mod.shutil, "which", return_value="/usr/bin/iw"):
                    self.assertEqual(peers_mod._default_wifi_interface(), "wlp0s20f3")


if __name__ == "__main__":
    unittest.main()
