import contextlib
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import session  # noqa: E402
from wfd.config import WFDNotReady
from wfd.p2p import nm, wpas  # noqa: E402


class NetworkManagerGroupInterfaceTest(unittest.TestCase):
    def test_timeout_distinguishes_activation_from_missing_group_interface(self):
        for state, message in (
            (1, "Timed out waiting for NetworkManager Wi-Fi Direct activation"),
            (2, "activated.*P2P group interface could not be determined"),
            (4, "deactivated the Wi-Fi Direct connection"),
        ):
            with (
                self.subTest(state=state),
                mock.patch.object(nm, "_nm_get_property", return_value="state"),
                mock.patch.object(nm, "_variant_uint", return_value=state),
                mock.patch.object(nm, "_nm_active_devices", return_value=["/device/1"]),
                mock.patch.object(nm, "_nm_group_interface", return_value=None),
                mock.patch.object(nm, "_nm_device_summary", return_value="device status"),
                mock.patch.object(nm.time, "monotonic", side_effect=[0, 0, 2]),
                mock.patch.object(nm.time, "sleep"),
            ):
                callback = mock.Mock()
                with self.assertRaisesRegex(WFDNotReady, message):
                    nm._wait_for_nm_activation("/active/1", timeout=1, on_group_interface=callback)
                callback.assert_not_called()

    def test_activation_without_callback_does_not_require_group_interface(self):
        with (
            mock.patch.object(nm, "_nm_get_property", return_value="state"),
            mock.patch.object(nm, "_variant_uint", return_value=2),
            mock.patch.object(nm, "_nm_active_devices", return_value=[]),
            mock.patch.object(nm, "_nm_group_interface", return_value=None),
        ):
            nm._wait_for_nm_activation("/active/1")

    def test_exact_ip_interface_is_read_from_active_device(self):
        def get_string(_path, _interface, prop):
            return "p2p-wlo1-2" if prop == "IpInterface" else "p2p-dev-wlo1"

        with mock.patch.object(nm, "_nm_get_string", side_effect=get_string):
            self.assertEqual(
                nm._nm_group_interface(["/device/1"]),
                "p2p-wlo1-2",
            )

    def test_control_and_invalid_interfaces_fail_closed(self):
        for value in (
            "p2p-dev-wlo1", "wlo1", "bad/interface", "/net/connman/iwd/0",
            "wlan0-p2p", "wlan0-p2p-go", "wlan0-p2p-cl0x", "wlan0-p2p-dev0",
        ):
            with self.subTest(value=value), mock.patch.object(
                nm, "_nm_get_string", return_value=value
            ):
                self.assertIsNone(nm._nm_group_interface(["/device/1"]))

    def test_iwd_data_interface_is_read_from_the_active_device(self):
        for group in ("wlan0-p2p-cl0", "wlan12-p2p-go3"):
            with self.subTest(group=group):
                def get_string(path, _interface, prop):
                    self.assertEqual(path, "/device/1")
                    return group if prop == "IpInterface" else "/net/connman/iwd/0"

                with mock.patch.object(nm, "_nm_get_string", side_effect=get_string):
                    self.assertEqual(nm._nm_group_interface(["/device/1"]), group)

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


class SessionGroupInterfaceTest(unittest.TestCase):
    def test_nm_session_preserves_backend_controls_and_publishes_group(self):
        for iwd in (False, True):
            with self.subTest(iwd=iwd), contextlib.ExitStack() as stack:
                group = "wlan0-p2p-cl0" if iwd else "p2p-wlan0-0"
                peer = SimpleNamespace(address="aa:bb:cc:dd:ee:ff", name="receiver")
                args = SimpleNamespace(
                    wfd_interface="wlan0", wfd_timeout=1, wfd_test_pattern=True,
                    wfd_no_firewall=True, fps=30, bitrate="2M", output_res="1280x720",
                )
                replacements = {
                    "run_diagnostics": mock.Mock(return_value=SimpleNamespace(wfd_candidate=True)),
                    "print_report": mock.Mock(),
                    "_nm_p2p_device_path": mock.Mock(return_value="/device/1"),
                    "_nm_p2p_uses_iwd": mock.Mock(return_value=iwd),
                    "_set_p2p_device_name": mock.Mock(),
                    "_scan_and_select": mock.Mock(return_value=peer),
                    "_set_p2p_go_intent": mock.Mock(return_value=7),
                    "_disconnect_device": mock.Mock(),
                    "_connect_peer": mock.Mock(return_value="/active/1"),
                    "_deactivate_connection": mock.Mock(),
                    "_wait_for_nm_activation": mock.Mock(
                        side_effect=lambda _path, *, on_group_interface: on_group_interface(group)
                    ),
                    "WFDRTSPServer": mock.Mock(),
                    "report_ts_dump": mock.Mock(),
                }
                for name, replacement in replacements.items():
                    stack.enter_context(mock.patch.object(session, name, replacement))
                stack.enter_context(mock.patch.object(session.threading, "Thread"))
                stack.enter_context(mock.patch.object(session.time, "sleep", side_effect=KeyboardInterrupt))
                session.start_experimental_backend(args)

                server = replacements["WFDRTSPServer"].return_value
                self.assertEqual(replacements["WFDRTSPServer"].call_args.kwargs["peer_address"], peer.address)
                replacements["_wait_for_nm_activation"].assert_called_once_with(
                    "/active/1", on_group_interface=server.set_group_interface
                )
                server.set_group_interface.assert_called_once_with(group)
                server.stop.assert_called_once_with()
                replacements["_deactivate_connection"].assert_called_once_with("/active/1")
                if iwd:
                    replacements["_set_p2p_device_name"].assert_not_called()
                    replacements["_set_p2p_go_intent"].assert_not_called()
                else:
                    replacements["_set_p2p_device_name"].assert_called_once_with("wlan0")
                    self.assertEqual(replacements["_set_p2p_go_intent"].call_args_list, [
                        mock.call("wlan0", 0), mock.call("wlan0", 7, restoring=True),
                    ])


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
