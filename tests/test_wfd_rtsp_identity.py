import os
import socket
import socketserver
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig
from wfd.rtsp.handler import _WFDRTSPHandler
from wfd.rtsp.rtsp_server import _ThreadingTCPServer, WFDRTSPServer


PEER_MAC = "00:51:ed:35:0f:ae"
PEER_IP = "10.42.0.182"


class RTSPClientClaimTest(unittest.TestCase):
    def setUp(self):
        self.server = WFDRTSPServer(
            WFDMediaConfig(monitor=None),
            peer_address=PEER_MAC,
            interface="p2p-wlan0-3",
        )

    def test_only_one_concurrent_client_claim_wins(self):
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=2)
            return self.server.claim_client(PEER_IP)

        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip", return_value=True
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(lambda _index: claim(), range(2)))

        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertFalse(self.server.has_connected_client)

    def test_claim_is_verified_confirmed_and_released_by_generation(self):
        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip", return_value=True
        ) as verify:
            claim = self.server.claim_client(PEER_IP)
            self.assertIsNotNone(claim)
            assert claim is not None
            self.assertIsNone(self.server.claim_client(PEER_IP))
            self.assertFalse(self.server.has_connected_client)
            self.assertTrue(self.server.confirm_client(PEER_IP, claim))
            self.assertTrue(self.server.has_connected_client)
            self.server.release_client(PEER_IP, claim + 1)
            self.assertTrue(self.server.has_connected_client)
            self.server.release_client(PEER_IP, claim)
            self.assertFalse(self.server.has_connected_client)

        self.assertEqual(verify.call_count, 2)
        verify.assert_called_with(PEER_IP, PEER_MAC, "p2p-wlan0-3")

    def test_unverified_client_cannot_claim(self):
        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip", return_value=False
        ):
            self.assertIsNone(self.server.claim_client("192.168.1.20"))

    def test_each_claim_revalidates_the_receiver_identity(self):
        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip", return_value=True
        ) as verify:
            self.assertTrue(self.server.authenticate_client(PEER_IP))
            self.assertTrue(self.server.authenticate_client(PEER_IP))
        self.assertEqual(verify.call_count, 2)

    def test_active_probe_can_replace_only_an_unconfirmed_claim(self):
        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip", return_value=True
        ):
            passive = self.server.claim_client(PEER_IP)
            active = self.server.claim_client(PEER_IP, replace_unconfirmed=True)

        self.assertIsNotNone(passive)
        self.assertIsNotNone(active)
        assert passive is not None and active is not None
        self.assertNotEqual(passive, active)
        self.assertFalse(self.server.confirm_client(PEER_IP, passive))
        self.server.release_client(PEER_IP, passive)
        self.assertTrue(self.server.confirm_client(PEER_IP, active))
        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip", return_value=True
        ):
            self.assertIsNone(
                self.server.claim_client(PEER_IP, replace_unconfirmed=True)
            )


class RTSPGroupInterfaceTest(unittest.TestCase):
    def setUp(self):
        self.server = WFDRTSPServer(
            WFDMediaConfig(monitor=None),
            peer_address=PEER_MAC,
        )

    def test_backend_can_publish_one_exact_group_interface(self):
        self.assertTrue(self.server.set_group_interface("p2p-wlan0-3"))
        self.assertEqual(self.server.interface, "p2p-wlan0-3")
        self.assertTrue(self.server.set_group_interface("p2p-wlan0-3"))
        self.assertFalse(self.server.set_group_interface("p2p-wlan0-4"))
        self.assertEqual(self.server.interface, "p2p-wlan0-3")

    def test_invalid_or_control_interface_is_rejected(self):
        self.assertFalse(self.server.set_group_interface("p2p-dev-wlan0"))
        self.assertFalse(self.server.set_group_interface("bad/interface"))
        self.assertIsNone(self.server.interface)

    def test_iwd_group_can_be_published_but_not_replaced(self):
        self.assertFalse(self.server.set_group_interface("/net/connman/iwd/0"))
        self.assertFalse(self.server.set_group_interface("wlan0"))
        self.assertTrue(self.server.set_group_interface("wlan0-p2p-cl0"))
        self.assertFalse(self.server.set_group_interface("wlan0-p2p-cl1"))
        self.assertEqual(self.server.interface, "wlan0-p2p-cl0")

    def test_connection_waits_for_the_backend_to_publish_the_group(self):
        checked = threading.Event()

        def verify(client_ip, peer_mac, interface):
            self.assertEqual(client_ip, PEER_IP)
            self.assertEqual(peer_mac, PEER_MAC)
            self.assertEqual(interface, "p2p-wlan0-3")
            checked.set()
            return True

        with mock.patch(
            "wfd.rtsp.rtsp_server._is_expected_peer_ip",
            side_effect=verify,
        ):
            worker = threading.Thread(
                target=self.server.authenticate_client,
                args=(PEER_IP,),
            )
            worker.start()
            self.assertFalse(checked.wait(timeout=0.05))
            self.assertTrue(self.server.set_group_interface("p2p-wlan0-3"))
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(checked.is_set())


class RTSPPreThreadAdmissionTest(unittest.TestCase):
    def test_unverified_socket_is_rejected_before_handler_thread(self):
        parent = mock.Mock()
        parent.authenticate_client.return_value = False
        server = _ThreadingTCPServer.__new__(_ThreadingTCPServer)
        server.parent_server = parent

        self.assertFalse(server.verify_request(None, ("192.168.1.20", 49152)))
        parent.authenticate_client.assert_called_once_with("192.168.1.20")

    def test_verified_socket_can_reach_handler_thread(self):
        parent = mock.Mock()
        parent.authenticate_client.return_value = True
        server = _ThreadingTCPServer.__new__(_ThreadingTCPServer)
        server.parent_server = parent

        self.assertTrue(server.verify_request(None, (PEER_IP, 49152)))

    def test_real_server_does_not_construct_a_handler_for_an_unverified_socket(self):
        handled = threading.Event()

        class RecordingHandler(socketserver.BaseRequestHandler):
            def handle(self):
                handled.set()

        parent = mock.Mock()
        parent.authenticate_client.return_value = False
        with _ThreadingTCPServer(("127.0.0.1", 0), RecordingHandler) as server:
            server.parent_server = parent
            server.timeout = 1
            worker = threading.Thread(target=server.handle_request)
            worker.start()
            with socket.create_connection(server.server_address, timeout=1):
                pass
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(handled.is_set())
        parent.authenticate_client.assert_called_once_with("127.0.0.1")


class RTSPHandlerAdmissionTest(unittest.TestCase):
    @staticmethod
    def _handler(parent):
        handler = _WFDRTSPHandler.__new__(_WFDRTSPHandler)
        handler.client_address = (PEER_IP, 49152)
        handler.server = SimpleNamespace(parent_server=parent)
        return handler

    def test_rejected_client_never_reaches_negotiation(self):
        parent = mock.Mock()
        parent.claim_client.return_value = None
        handler = self._handler(parent)
        with mock.patch.object(handler, "_handle_verified_client") as negotiate:
            handler.handle()

        parent.claim_client.assert_called_once_with(PEER_IP)
        negotiate.assert_not_called()
        parent.release_client.assert_not_called()

    def test_client_ownership_is_released_after_handler_failure(self):
        parent = mock.Mock()
        parent.claim_client.return_value = 7
        handler = self._handler(parent)
        with (
            mock.patch.object(
                handler,
                "_handle_verified_client",
                side_effect=RuntimeError("test failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "test failure"),
        ):
            handler.handle()

        parent.release_client.assert_called_once_with(PEER_IP, 7)

    def test_superseded_handler_cannot_dispatch_protocol_progress(self):
        parent = mock.Mock()
        parent.confirm_client.return_value = False
        handler = self._handler(parent)
        handler._parent_server = parent
        handler._client_ip = PEER_IP
        handler._client_claim = 3
        handler.pending = {}
        message = SimpleNamespace(is_response=False, method="PLAY")

        with mock.patch.object(handler, "_dispatch_message") as dispatch:
            self.assertFalse(handler._process_claimed_message(message, "peer"))

        parent.confirm_client.assert_called_once_with(PEER_IP, 3)
        dispatch.assert_not_called()

    def test_current_handler_confirms_before_dispatching_protocol_progress(self):
        parent = mock.Mock()
        parent.confirm_client.return_value = True
        handler = self._handler(parent)
        handler._parent_server = parent
        handler._client_ip = PEER_IP
        handler._client_claim = 3
        handler.pending = {"1": "M1_OPTIONS"}
        message = SimpleNamespace(
            is_response=True,
            cseq="1",
            status="200 OK",
        )

        with mock.patch.object(handler, "_dispatch_message") as dispatch:
            self.assertTrue(handler._process_claimed_message(message, "peer"))

        parent.confirm_client.assert_called_once_with(PEER_IP, 3)
        dispatch.assert_called_once_with(message)

    def test_unknown_method_does_not_confirm_claim(self):
        parent = mock.Mock()
        handler = self._handler(parent)
        handler._parent_server = parent
        handler._client_ip = PEER_IP
        handler._client_claim = 3
        handler.pending = {}
        message = SimpleNamespace(is_response=False, method="GARBAGE")

        with mock.patch.object(handler, "_dispatch_message") as dispatch:
            self.assertTrue(handler._process_claimed_message(message, "peer"))

        parent.confirm_client.assert_not_called()
        dispatch.assert_called_once_with(message)


if __name__ == "__main__":
    unittest.main()
