import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import mice  # noqa: E402
from wfd.config import WFDMediaConfig, WFDNotReady  # noqa: E402
from wfd.rtsp.rtsp_server import WFDRTSPServer  # noqa: E402


class MiceMessageTests(unittest.TestCase):
    def test_source_ready_layout(self):
        msg = mice.source_ready_message("FW", 7236)
        self.assertEqual(int.from_bytes(msg[0:2], "big"), len(msg))
        self.assertEqual(msg[2:4], b"\x01\x01")
        # Friendly name: UTF-16LE, no terminator.
        self.assertEqual(msg[4:11], b"\x00\x00\x04F\x00W\x00")
        # RTSP port before the source ID; LG webOS ignores the other order.
        self.assertEqual(msg[11:16], b"\x02\x00\x02\x1c\x44")
        self.assertEqual(msg[16:19], b"\x03\x00\x10")
        self.assertEqual(len(msg), 19 + 16)

    def test_stop_projection_has_no_port(self):
        msg = mice.stop_projection_message("FW")
        self.assertEqual(int.from_bytes(msg[0:2], "big"), len(msg))
        self.assertEqual(msg[2:4], b"\x01\x02")
        self.assertEqual(len(msg), 4 + 7 + 19)


class MiceRTSPAuthTests(unittest.TestCase):
    def test_only_the_mice_host_is_accepted(self):
        server = WFDRTSPServer(
            media_config=WFDMediaConfig(monitor=None),
            peer_address="",
            mice_host="192.168.1.50",
        )
        self.assertTrue(server.authenticate_client("192.168.1.50"))
        self.assertFalse(server.authenticate_client("192.168.1.51"))


class MiceDiscoveryTests(unittest.TestCase):
    def test_cast_friendly_name_wins_over_the_instance_name(self):
        name = mice._mdns_name(
            "_googlecast._tcp.local.",
            "TV-0123abcd._googlecast._tcp.local.",
            {b"fn": b"Example TV"},
        )
        self.assertEqual(name, "Example TV")

    def test_instance_name_drops_the_service_type(self):
        name = mice._mdns_name("_airplay._tcp.local.", "Example TV._airplay._tcp.local.", {})
        self.assertEqual(name, "Example TV")

    def test_probe_marks_only_hosts_listening_on_the_mice_port(self):
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        refusing = socket.socket()  # bound but not listening: connection refused
        refusing.bind(("127.0.0.1", 0))
        self.addCleanup(listening.close)
        self.addCleanup(refusing.close)
        sinks = [mice.MiceSink("127.0.0.1", "tv"), mice.MiceSink("127.0.0.1", "speaker")]

        ports = iter([listening.getsockname()[1], refusing.getsockname()[1]])
        real_accepts = mice._accepts_mice

        def accepts(address, timeout=1.0):
            with mock.patch.object(mice, "MICE_PORT", next(ports)):
                return real_accepts(address, timeout)

        with mock.patch.object(mice, "_accepts_mice", accepts), \
                mock.patch.object(mice, "ThreadPoolExecutor", _InlinePool):
            ready = mice.probe_candidates(sinks)
        self.assertEqual([s.name for s in ready], ["tv"])
        self.assertFalse(sinks[1].ready)

    def test_select_takes_the_only_sink_without_asking(self):
        sink = mice.MiceSink("192.168.1.50", "Example TV", ready=True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertIs(mice.select_sink([sink], None), sink)

    def test_select_by_index_or_name(self):
        sinks = [mice.MiceSink("192.168.1.50", "Example TV"),
                 mice.MiceSink("192.168.1.60", "Office PC")]
        self.assertIs(mice.select_sink(sinks, "1"), sinks[1])
        self.assertIs(mice.select_sink(sinks, "example"), sinks[0])
        self.assertIs(mice.select_sink(sinks, "192.168.1.60"), sinks[1])
        with self.assertRaises(WFDNotReady):
            mice.select_sink(sinks, "projector")
        with self.assertRaises(WFDNotReady):
            mice.select_sink(sinks, "5")

    def test_discovery_without_zeroconf_says_what_to_do(self):
        with mock.patch.object(mice, "browse_candidates", return_value=None):
            with self.assertRaisesRegex(WFDNotReady, "zeroconf"):
                mice.discover_sink()

    def test_discovery_with_no_devices_fails_at_once(self):
        with mock.patch.object(mice, "browse_candidates", return_value=[]), \
                mock.patch.object(mice, "probe_candidates") as probe:
            with self.assertRaises(WFDNotReady):
                mice.discover_sink()
        probe.assert_not_called()

    def test_discovery_waits_for_the_sink_to_start_listening(self):
        # A sink may only accept MICE while its screen-share app is open,
        # which can be after FluxCast started.
        tv = mice.MiceSink("192.168.1.50", "Example TV")
        answers = iter([False, False, True])
        with mock.patch.object(mice, "browse_candidates", return_value=[tv]), \
                mock.patch.object(mice, "_accepts_mice", lambda _a: next(answers)), \
                mock.patch.object(mice, "ThreadPoolExecutor", _InlinePool):
            self.assertIs(mice.discover_sink(wait=5, poll_interval=0), tv)

    def test_discovery_gives_up_at_the_deadline_naming_what_it_saw(self):
        tv = mice.MiceSink("192.168.1.50", "Example TV")
        with mock.patch.object(mice, "browse_candidates", return_value=[tv]), \
                mock.patch.object(mice, "_accepts_mice", lambda _a: False), \
                mock.patch.object(mice, "ThreadPoolExecutor", _InlinePool):
            with self.assertRaisesRegex(WFDNotReady, "192.168.1.50"):
                mice.discover_sink(wait=0, poll_interval=0)


class MiceArgumentTests(unittest.TestCase):
    def _parse(self, argv):
        import main
        with mock.patch.object(sys, "argv", ["fluxcast", "--protocol", "wfd", *argv]):
            return main.parse_args()

    def test_bare_flag_means_discover(self):
        self.assertEqual(self._parse(["--wfd-mice"]).wfd_mice, mice.MICE_AUTO)

    def test_host_is_kept(self):
        self.assertEqual(self._parse(["--wfd-mice", "192.168.1.50"]).wfd_mice, "192.168.1.50")

    def test_off_by_default(self):
        self.assertIsNone(self._parse([]).wfd_mice)


class _InlinePool:
    """ThreadPoolExecutor stand-in so mocked probes run in order."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items):
        return [fn(item) for item in items]


if __name__ == "__main__":
    unittest.main()
