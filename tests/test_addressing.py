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
    def test_scoped_kernel_row_inherits_the_queried_interface(self):
        row = f"{P2P_IP} lladdr {PEER_MAC} REACHABLE"

        self.assertIsNone(addressing._neighbor_identity(row))
        self.assertEqual(
            addressing._neighbor_identity(
                row,
                scoped_interface="p2p-wlan0-3",
            ),
            (P2P_IP, "p2p-wlan0-3", PEER_MAC.replace(":", "")),
        )

    def test_scoped_kernel_format_accepts_one_group_derived_mac(self):
        row = f"{P2P_IP} lladdr fa:2f:9e:8b:71:4d REACHABLE"
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(row)),
        ):
            self.assertTrue(
                addressing._is_expected_peer_ip(
                    P2P_IP,
                    "f2:2f:9e:8b:71:4d",
                    "p2p-wlan0-3",
                )
            )

    def test_incomplete_or_failed_neighbor_is_not_an_identity(self):
        for state in ("INCOMPLETE", "FAILED"):
            with self.subTest(state=state):
                self.assertIsNone(
                    addressing._neighbor_identity(
                        f"{P2P_IP} dev p2p-wlan0-3 lladdr {PEER_MAC} {state}"
                    )
                )

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

    def test_session_scoped_lookup_accepts_one_randomized_group_mac(self):
        neighbours = "\n".join(
            [
                "192.168.178.54 dev wlan0 lladdr 7a:11:22:33:44:55 REACHABLE",
                f"{P2P_IP} dev p2p-wlan0-3 lladdr 7a:11:22:33:44:55 REACHABLE",
            ]
        )

        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(neighbours)),
        ):
            peer_ip = addressing._get_peer_ip_from_arp(
                PEER_MAC, interface="wlan0"
            )

        self.assertEqual(peer_ip, P2P_IP)

    def test_exact_group_scope_rejects_a_sibling_p2p_interface(self):
        rows = "\n".join(
            [
                f"{P2P_IP} dev p2p-wlan0-3 lladdr 7a:11:22:33:44:55 REACHABLE",
                f"{P2P_IP} dev p2p-wlan0-4 lladdr 7a:aa:bb:cc:dd:ee STALE",
            ]
        )
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(
                addressing, "_run", return_value=_completed(rows)
            ) as run,
        ):
            self.assertTrue(
                addressing._is_expected_peer_ip(
                    P2P_IP, PEER_MAC, "p2p-wlan0-3"
                )
            )
            self.assertFalse(
                addressing._is_expected_peer_ip(
                    P2P_IP, PEER_MAC, "p2p-wlan0-5"
                )
            )

        run.assert_any_call(
            ["ip", "neigh", "show", "dev", "p2p-wlan0-3"],
            timeout=3.0,
        )
        run.assert_any_call(
            ["ip", "neigh", "show", "dev", "p2p-wlan0-5"],
            timeout=3.0,
        )

    def test_peer_lookup_queries_only_the_exact_group_when_available(self):
        # `ip neigh show dev IFACE` omits the redundant `dev IFACE` columns.
        row = f"{P2P_IP} lladdr {PEER_MAC} REACHABLE"
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(
                addressing, "_run", return_value=_completed(row)
            ) as run,
        ):
            self.assertEqual(
                addressing._get_peer_ip_from_arp(
                    PEER_MAC,
                    interface="p2p-wlan0-3",
                ),
                P2P_IP,
            )

        run.assert_called_once_with(
            ["ip", "neigh", "show", "dev", "p2p-wlan0-3"],
            timeout=3.0,
        )

    def test_receiver_identity_rejects_lan_and_other_p2p_interfaces(self):
        rows = "\n".join(
            [
                f"{P2P_IP} dev wlan0 lladdr {PEER_MAC} REACHABLE",
                f"{P2P_IP} dev p2p-wlan1-0 lladdr {PEER_MAC} REACHABLE",
            ]
        )
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(rows)),
        ):
            self.assertFalse(
                addressing._is_expected_peer_ip(
                    P2P_IP, PEER_MAC, "p2p-wlan0-3"
                )
            )

    def test_receiver_identity_accepts_exact_selected_peer(self):
        row = f"{P2P_IP} dev p2p-wlan0-3 lladdr {PEER_MAC} REACHABLE"
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(
                addressing, "_run", return_value=_completed(row)
            ) as run,
        ):
            self.assertTrue(
                addressing._is_expected_peer_ip(
                    P2P_IP, PEER_MAC, "p2p-wlan0-3"
                )
            )

        run.assert_called_once_with(
            ["ip", "neigh", "show", "dev", "p2p-wlan0-3"],
            timeout=3.0,
        )

    def test_unknown_session_interface_fails_closed_even_for_exact_mac(self):
        row = f"{P2P_IP} dev p2p-wlan0-3 lladdr {PEER_MAC} REACHABLE"
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(row)),
        ):
            self.assertFalse(
                addressing._is_expected_peer_ip(P2P_IP, PEER_MAC, None)
            )

    def test_randomized_mac_requires_a_session_interface(self):
        row = (
            f"{P2P_IP} dev p2p-wlan0-3 "
            "lladdr 7a:11:22:33:44:55 REACHABLE"
        )
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(row)),
        ):
            self.assertFalse(
                addressing._is_expected_peer_ip(P2P_IP, PEER_MAC, None)
            )

    def test_receiver_identity_rejects_ambiguous_randomized_peers(self):
        rows = "\n".join(
            [
                f"{P2P_IP} dev p2p-wlan0-3 lladdr 7a:11:22:33:44:55 REACHABLE",
                "10.42.0.183 dev p2p-wlan0-3 lladdr 7a:aa:bb:cc:dd:ee STALE",
            ]
        )
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(rows)),
        ):
            self.assertFalse(
                addressing._is_expected_peer_ip(
                    P2P_IP, PEER_MAC, "p2p-wlan0-3"
                )
            )

    def test_receiver_identity_rejects_unexpected_extra_neighbor_output(self):
        rows = "\n".join(
            [
                f"{P2P_IP} dev p2p-wlan0-3 lladdr 7a:11:22:33:44:55 REACHABLE",
                "10.42.0.183 dev p2p-wlan0-3 lladdr 7a:aa:bb:cc:dd:ee STALE",
            ]
        )
        with (
            mock.patch.object(addressing.shutil, "which", return_value="/usr/bin/ip"),
            mock.patch.object(addressing, "_run", return_value=_completed(rows)),
        ):
            self.assertFalse(
                addressing._is_expected_peer_ip(
                    P2P_IP, PEER_MAC, "p2p-wlan0-3"
                )
            )

    def test_receiver_identity_rejects_invalid_inputs_without_running_ip(self):
        with mock.patch.object(addressing, "_run") as run:
            self.assertFalse(
                addressing._is_expected_peer_ip("999.1.1.1", PEER_MAC, "wlan0")
            )
            self.assertFalse(
                addressing._is_expected_peer_ip(P2P_IP, "not-a-mac", "wlan0")
            )
            self.assertFalse(
                addressing._is_expected_peer_ip(P2P_IP, PEER_MAC, "bad interface")
            )
            self.assertFalse(
                addressing._is_expected_peer_ip(P2P_IP, PEER_MAC, "wlan0")
            )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
