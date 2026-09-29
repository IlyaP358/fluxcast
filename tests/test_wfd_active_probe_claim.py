import os
import socket
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.probe import _active_rtsp_probe
from wfd.config import WFDNotReady
from wfd.rtsp.message import RTSPMessage


class ActiveProbeClaimTest(unittest.TestCase):
    def test_probe_scopes_lookup_and_claims_before_negotiating(self):
        server = SimpleNamespace(
            has_connected_client=False,
            interface="wlan0",
            port=7236,
            claim_client=mock.Mock(return_value=None),
            _register_rtsp_socket=mock.Mock(return_value=True),
            _unregister_rtsp_socket=mock.Mock(),
        )
        peer = SimpleNamespace(address="aa:bb:cc:dd:ee:ff", rtsp_port=7236)
        sock = mock.Mock()

        with (
            mock.patch("wfd.probe.time.sleep"),
            mock.patch(
                "wfd.probe._wait_for_peer_ip", return_value="192.168.49.1"
            ) as lookup,
            mock.patch("wfd.probe.socket.create_connection", return_value=sock),
        ):
            _active_rtsp_probe(server, peer, SimpleNamespace())

        lookup.assert_called_once_with(
            peer.address,
            timeout=10.0,
            interface="wlan0",
            allow_interface_fallback=False,
        )
        server.claim_client.assert_called_once_with(
            "192.168.49.1", replace_unconfirmed=True
        )
        sock.close.assert_called_once_with()

    def test_confirmed_passive_client_cancels_probe_before_address_lookup(self):
        server = SimpleNamespace(has_connected_client=True)
        with (
            mock.patch("wfd.probe.time.sleep"),
            mock.patch("wfd.probe._wait_for_peer_ip") as lookup,
        ):
            _active_rtsp_probe(server, SimpleNamespace(), SimpleNamespace())
        lookup.assert_not_called()

    def test_probe_confirms_valid_response_and_releases_ownership(self):
        server = SimpleNamespace(
            has_connected_client=False,
            interface="wlan0",
            port=7236,
            claim_client=mock.Mock(return_value=9),
            confirm_client=mock.Mock(return_value=True),
            release_client=mock.Mock(),
            _register_rtsp_socket=mock.Mock(return_value=True),
            _unregister_rtsp_socket=mock.Mock(),
        )
        peer = SimpleNamespace(address="aa:bb:cc:dd:ee:ff", rtsp_port=7236)
        media_config = SimpleNamespace(source_port=19002, no_audio=False)
        sock = mock.MagicMock()
        wfile = mock.Mock()
        sock.makefile.return_value = wfile
        sock.getsockname.return_value = ("192.168.49.2", 49152)
        response = RTSPMessage(
            start="RTSP/1.0 200 OK",
            headers={"cseq": "1"},
            raw_headers=[],
        )

        with (
            mock.patch("wfd.probe.time.sleep"),
            mock.patch(
                "wfd.probe._wait_for_peer_ip", return_value="192.168.49.1"
            ),
            mock.patch("wfd.probe.socket.create_connection", return_value=sock),
            mock.patch(
                "wfd.probe._read_rtsp_message", side_effect=[response, None]
            ),
        ):
            _active_rtsp_probe(server, peer, media_config)

        server.confirm_client.assert_called_once_with("192.168.49.1", 9)
        server.release_client.assert_called_once_with("192.168.49.1", 9)
        server._unregister_rtsp_socket.assert_called_once_with(sock)
        wfile.close.assert_called_once_with()

    def test_probe_failure_paths_close_socket_and_release_slot(self):
        for failure in ("capacity", "claim", "setup", "parse", "timeout"):
            with self.subTest(failure=failure):
                server = SimpleNamespace(
                    has_connected_client=False, interface="wlan0", port=7236,
                    claim_client=mock.Mock(return_value=9),
                    release_client=mock.Mock(),
                    _register_rtsp_socket=mock.Mock(return_value=failure != "capacity"),
                    _unregister_rtsp_socket=mock.Mock(),
                )
                sock = mock.MagicMock()
                sock.getsockname.return_value = ("192.168.49.2", 49152)
                if failure == "claim":
                    server.claim_client.side_effect = RuntimeError("claim failed")
                if failure == "setup":
                    sock.getsockname.side_effect = OSError("socket closed")
                    sock.makefile.return_value.close.side_effect = OSError("flush failed")
                error = TimeoutError("read deadline") if failure == "timeout" else WFDNotReady("invalid message")
                with (
                    mock.patch("wfd.probe.time.sleep"),
                    mock.patch("wfd.probe._wait_for_peer_ip", return_value="192.168.49.1"),
                    mock.patch("wfd.probe.socket.create_connection", return_value=sock),
                    mock.patch("wfd.probe._read_rtsp_message", side_effect=error),
                ):
                    peer = SimpleNamespace(address="aa:bb:cc:dd:ee:ff", rtsp_port=7236)
                    config = SimpleNamespace(source_port=19002, no_audio=False)
                    if failure == "claim":
                        with self.assertRaises(RuntimeError):
                            _active_rtsp_probe(server, peer, config)
                    else:
                        _active_rtsp_probe(server, peer, config)
                sock.close.assert_called_once_with()
                if failure == "capacity":
                    server.claim_client.assert_not_called()
                else:
                    server._unregister_rtsp_socket.assert_called_once_with(sock)
                if failure in ("setup", "parse", "timeout"):
                    server.release_client.assert_called_once_with("192.168.49.1", 9)
                    sock.makefile.return_value.close.assert_called_once_with()

    def test_probe_rejects_malformed_message_on_real_socket(self):
        client, peer_socket = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer_socket.close)
        peer_socket.settimeout(1)
        peer_socket.sendall(b"RTSP/1.0 200 OK\r\nContent-Length: 999999999999999\r\n\r\n")
        server = SimpleNamespace(
            has_connected_client=False, interface="wlan0", port=7236,
            claim_client=mock.Mock(return_value=9), release_client=mock.Mock(),
            _register_rtsp_socket=mock.Mock(return_value=True),
            _unregister_rtsp_socket=mock.Mock(),
        )
        # A TCP socket supplies an address tuple; socketpair has no IP address.
        sock = mock.MagicMock(wraps=client)
        sock.fileno.return_value = client.fileno()
        sock.getsockname.return_value = ("192.168.49.2", 49152)
        with (
            mock.patch("wfd.probe.time.sleep"),
            mock.patch("wfd.probe._wait_for_peer_ip", return_value="192.168.49.1"),
            mock.patch("wfd.probe.socket.create_connection", return_value=sock),
        ):
            _active_rtsp_probe(
                server, SimpleNamespace(address="aa:bb:cc:dd:ee:ff", rtsp_port=7236),
                SimpleNamespace(source_port=19002, no_audio=False),
            )
        self.assertEqual(client.fileno(), -1)
        server._unregister_rtsp_socket.assert_called_once_with(sock)
        server.release_client.assert_called_once_with("192.168.49.1", 9)


if __name__ == "__main__":
    unittest.main()
