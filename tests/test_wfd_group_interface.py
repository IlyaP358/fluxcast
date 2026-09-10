import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.p2p import nm, wpas  # noqa: E402


class NetworkManagerGroupInterfaceTest(unittest.TestCase):
    def test_exact_ip_interface_is_read_from_active_device(self):
        def get_string(_path, _interface, prop):
            return "p2p-wlo1-2" if prop == "IpInterface" else "p2p-dev-wlo1"

        with mock.patch.object(nm, "_nm_get_string", side_effect=get_string):
            self.assertEqual(
                nm._nm_group_interface(["/device/1"]),
                "p2p-wlo1-2",
            )

    def test_control_and_invalid_interfaces_fail_closed(self):
        for value in ("p2p-dev-wlo1", "wlo1", "bad/interface"):
            with self.subTest(value=value), mock.patch.object(
                nm, "_nm_get_string", return_value=value
            ):
                self.assertIsNone(nm._nm_group_interface(["/device/1"]))

    def test_activation_publishes_group_interface_before_return(self):
        callback = mock.Mock()
        with (
            mock.patch.object(nm, "_nm_get_property", return_value="state"),
            mock.patch.object(nm, "_variant_uint", return_value=2),
            mock.patch.object(nm, "_nm_active_devices", return_value=["/device/1"]),
            mock.patch.object(
                nm, "_nm_group_interface", return_value="p2p-wlo1-2"
            ),
            mock.patch.object(nm, "_nm_device_summary", return_value="activated"),
        ):
            nm._wait_for_nm_activation(
                "/active/1",
                on_group_interface=callback,
            )

        callback.assert_called_once_with("p2p-wlo1-2")

    def test_activation_waits_for_the_group_interface_to_appear(self):
        callback = mock.Mock()
        with (
            mock.patch.object(nm, "_nm_get_property", return_value="state"),
            mock.patch.object(nm, "_variant_uint", return_value=2),
            mock.patch.object(nm, "_nm_active_devices", return_value=["/device/1"]),
            mock.patch.object(
                nm,
                "_nm_group_interface",
                side_effect=[None, "p2p-wlo1-2"],
            ) as group,
            mock.patch.object(nm, "_nm_device_summary", return_value="activated"),
            mock.patch.object(nm.time, "sleep"),
        ):
            nm._wait_for_nm_activation(
                "/active/1",
                on_group_interface=callback,
            )

        self.assertEqual(group.call_count, 2)
        callback.assert_called_once_with("p2p-wlo1-2")


class SupplicantGroupInterfaceTest(unittest.TestCase):
    def test_connect_publishes_group_before_ip_configuration(self):
        events = []

        def publish(interface):
            events.append(("publish", interface))

        def configure(interface, *_args):
            events.append(("configure", interface))

        with (
            mock.patch.object(wpas, "_p2p_device_iface_paths", return_value=["/iface/1"]),
            mock.patch.object(wpas, "_default_wifi_interface", return_value="wlan0"),
            mock.patch.object(wpas, "_set_wfd_ies"),
            mock.patch.object(wpas, "_wait_for_peer", return_value="/peer/1"),
            mock.patch.object(wpas, "_set_p2p_go_intent", return_value=None),
            mock.patch.object(wpas, "_list_wpas_interfaces", return_value={"/iface/1"}),
            mock.patch.object(wpas, "_wpas_connect"),
            mock.patch.object(
                wpas, "_wait_for_group_interface", return_value="p2p-wlan0-3"
            ),
            mock.patch.object(wpas, "get_p2p_role", return_value="P2P-client"),
            mock.patch.object(wpas, "configure_ip", side_effect=configure),
        ):
            result = wpas.connect_via_wpa_supplicant(
                None,
                "aa:bb:cc:dd:ee:ff",
                on_group_interface=publish,
            )

        self.assertEqual(result, "p2p-wlan0-3")
        self.assertEqual(
            events,
            [
                ("publish", "p2p-wlan0-3"),
                ("configure", "p2p-wlan0-3"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
