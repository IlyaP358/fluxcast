import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.probe import _active_rtsp_probe
from wfd.rtsp.message import RTSPMessage


class ActiveProbeClaimTest(unittest.TestCase):
    def test_probe_scopes_lookup_and_claims_before_negotiating(self):
        server = SimpleNamespace(
            has_connected_client=False,
            interface="wlan0",
            port=7236,
            claim_client=mock.Mock(return_value=None),
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
        )
        peer = SimpleNamespace(address="aa:bb:cc:dd:ee:ff", rtsp_port=7236)
        media_config = SimpleNamespace(source_port=19002, no_audio=False)
        sock = mock.MagicMock()
        rfile = mock.Mock()
        wfile = mock.Mock()
        sock.makefile.side_effect = [rfile, wfile]
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


if __name__ == "__main__":
    unittest.main()
