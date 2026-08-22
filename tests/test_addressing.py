import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.p2p import addressing  # noqa: E402


PEER_MAC = "00:51:ed:35:0f:ae"
P2P_IP = "10.42.0.182"


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class PeerIpAddressingTest(unittest.TestCase):
    def test_mac_lookup_ignores_infrastructure_interface(self):
        neighbours = "\n".join(
            [
                f"192.168.178.54 dev wlan0 lladdr {PEER_MAC} REACHABLE",
                f"{P2P_IP} dev p2p-wlan0-3 lladdr {PEER_MAC} REACHABLE",
            ]
        )

        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(neighbours)),
        ):
            peer_ip = addressing._get_peer_ip_from_arp(PEER_MAC)

        self.assertEqual(peer_ip, P2P_IP)

    def test_mac_lookup_excludes_p2p_device_interface(self):
        neighbours = (
            f"192.168.49.1 dev p2p-dev-wlan0 lladdr {PEER_MAC} REACHABLE"
        )

        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(neighbours)),
        ):
            peer_ip = addressing._get_peer_ip_from_arp(PEER_MAC)

        self.assertIsNone(peer_ip)

    def test_randomized_mac_falls_back_to_p2p_group_interface(self):
        randomized_mac = "7a:11:22:33:44:55"
        neighbours = "\n".join(
            [
                f"192.168.178.54 dev wlan0 lladdr {PEER_MAC} REACHABLE",
                f"{P2P_IP} dev p2p-wlan0-3 lladdr {randomized_mac} REACHABLE",
            ]
        )

        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(neighbours)),
        ):
            direct_ip = addressing._get_peer_ip_from_arp(PEER_MAC)
            fallback_ip = addressing._get_peer_ip_from_p2p_iface()
            waited_ip = addressing._wait_for_peer_ip(PEER_MAC, timeout=0.1)

        self.assertIsNone(direct_ip)
        self.assertEqual(fallback_ip, P2P_IP)
        self.assertEqual(waited_ip, P2P_IP)


if __name__ == "__main__":
    unittest.main()
