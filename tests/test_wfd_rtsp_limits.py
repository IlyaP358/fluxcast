import io
import os
import socket
import socketserver
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig, WFDNotReady
from wfd.modes import _max_wfd_level, _parse_sink_video_format
from wfd.rtsp.handler import _WFDRTSPHandler
from wfd.rtsp.message import (
    RTSP_MAX_BODY, RTSP_MAX_HEADERS, RTSP_MAX_HEADER_BYTES, RTSP_MAX_LINE,
    RTSP_MAX_PENDING, _RTSPReader, _parse_rtp_ports,
    _parse_transport_client_ports, _read_rtsp_message,
)
from wfd.rtsp.rtsp_server import _ThreadingTCPServer, WFDRTSPServer


START = b"OPTIONS * RTSP/1.0\r\n"


class RTSPMessageLimitsTest(unittest.TestCase):
    def parse(self, wire):
        return _read_rtsp_message(io.BytesIO(wire))

    def test_empty_stream_and_complete_message(self):
        self.assertIsNone(self.parse(b""))
        for newline in (b"\r\n", b"\n"):
            msg = self.parse(newline.join([b"OPTIONS * RTSP/1.0", b"CSeq: 1", b"", b""]))
            self.assertEqual(msg.method, "OPTIONS")
            self.assertEqual(msg.cseq, "1")

    def test_line_limit(self):
        for prefix in (b"", START):
            with self.subTest(prefix=prefix):
                line = b"X:" + b"a" * (RTSP_MAX_LINE - 4) + b"\r\n"
                self.assertIsNotNone(self.parse(prefix + line + b"\r\n"))
                with self.assertRaises(WFDNotReady):
                    self.parse(prefix + b"X" + line + b"\r\n")

    def test_header_count_includes_duplicates(self):
        self.assertIsNotNone(self.parse(START + b"X: a\r\n" * RTSP_MAX_HEADERS + b"\r\n"))
        with self.assertRaises(WFDNotReady):
            self.parse(START + b"X: a\r\n" * (RTSP_MAX_HEADERS + 1) + b"\r\n")

    def test_header_flood_stops_reading_early(self):
        class Flood:
            calls = 0

            def readline(self, limit):
                self.calls += 1
                if self.calls > RTSP_MAX_HEADERS + 2:
                    raise AssertionError("read beyond header count limit")
                return START if self.calls == 1 else b"X: a\r\n"

        stream = Flood()
        with self.assertRaises(WFDNotReady):
            _read_rtsp_message(stream)
        self.assertEqual(stream.calls, RTSP_MAX_HEADERS + 2)

    def test_total_header_bytes(self):
        headers = b"".join(b"X%d: " % i + b"a" * 8186 + b"\r\n" for i in range(3))
        remaining = RTSP_MAX_HEADER_BYTES - len(START + headers) - 2
        last = b"Last: " + b"a" * (remaining - 8) + b"\r\n"
        wire = START + headers + last + b"\r\n"
        self.assertEqual(len(wire), RTSP_MAX_HEADER_BYTES)
        self.assertIsNotNone(self.parse(wire))
        with self.assertRaises(WFDNotReady):
            self.parse(START + headers + b"X" + last + b"\r\n")

    def test_content_length_rejected_before_body_read(self):
        for value in ("999999999999999", "65537", "-1", "+1", "1.0", "1e2",
                      "", "١", "000000", "9" * 7000):
            with self.subTest(value=value[:30]):
                stream = io.BytesIO(START + f"Content-Length: {value}\r\n\r\n".encode())
                with mock.patch.object(stream, "read", side_effect=AssertionError("body read")):
                    with self.assertRaises(WFDNotReady):
                        _read_rtsp_message(stream)

    def test_body_limit_and_partial_reads(self):
        class PartialReads(io.BytesIO):
            def read(self, size):
                return super().read(min(size, 31))

        wire = START + f"Content-Length: {RTSP_MAX_BODY}\r\n\r\n".encode() + b"a" * RTSP_MAX_BODY
        self.assertEqual(len(_read_rtsp_message(PartialReads(wire)).body), RTSP_MAX_BODY)

    def test_truncated_input(self):
        for wire in (START, START + b"X: a", START + b"Content-Length: 2\r\n\r\na"):
            with self.subTest(wire=wire), self.assertRaises(WFDNotReady):
                self.parse(wire)

    def test_samsung_duplicate_length_preserves_framing(self):
        wire = START + b"Content-Length: 3\r\ncontent-length: 3\r\n\r\nabc"
        stream = io.BytesIO(wire + START + b"\r\n")
        self.assertEqual(_read_rtsp_message(stream).body, "abc")
        self.assertEqual(_read_rtsp_message(stream).method, "OPTIONS")
        self.assertIsNone(_read_rtsp_message(stream))

    def test_conflicting_duplicates_and_malformed_headers(self):
        for headers in (b"Content-Length: 0\r\nContent-Length: 1\r\n",
                        b"CSeq: 1\r\nCSeq: 2\r\n", b"Bad header\r\n", b": a\r\n"):
            with self.subTest(headers=headers), self.assertRaises(WFDNotReady):
                self.parse(START + headers + b"\r\n")

    def test_ports_are_bounded_before_conversion(self):
        for value in ("65536", "999999999999999", "000001", "+1", "١", "1x"):
            with self.subTest(value=value):
                self.assertIsNone(_parse_rtp_ports(f"RTP/AVP/UDP;unicast {value} 0 mode=play"))
                self.assertIsNone(_parse_transport_client_ports(f"RTP/AVP/UDP;client_port={value}"))
        self.assertEqual(_parse_rtp_ports("RTP/AVP/UDP;unicast 65535 0 mode=play"), (65535, 0))
        self.assertEqual(_parse_transport_client_ports("RTP/AVP/UDP; client_port=19000-19001 ;mode=play"), (19000, 19001))
        self.assertIsNone(_parse_transport_client_ports("client_port=5;client_port=invalid"))

    def test_video_numeric_fields_have_wire_widths(self):
        fields = "30 00 01 02 000001e3 0f3fffff 00000fff 00 0000 00c8 00".split()
        self.assertIsNotNone(_parse_sink_video_format(" ".join(fields)))
        for index in range(7):
            bad = fields.copy()
            bad[index] = "f" * 60000
            self.assertIsNone(_parse_sink_video_format(" ".join(bad)))
        for level in ("f" * 60000, "0x01", "-1", "١١"):
            self.assertIsNone(_max_wfd_level(level))
        self.assertEqual(_max_wfd_level("ff"), 128)


class RTSPSocketDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.reader_socket, self.writer = socket.socketpair()
        self.reader_socket.settimeout(0.5)
        self.writer.settimeout(0.5)
        self.addCleanup(self.reader_socket.close)
        self.addCleanup(self.writer.close)

    def test_pipelined_socket_messages(self):
        self.writer.sendall((START + b"\r\n") * 3)
        reader = _RTSPReader(self.reader_socket)
        for _ in range(3):
            self.assertEqual(_read_rtsp_message(reader).method, "OPTIONS")

    def test_idle_negotiation_has_deadline(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            _read_rtsp_message(_RTSPReader(self.reader_socket, idle_timeout=0.05))
        self.assertLess(time.monotonic() - start, 1)

    def test_slow_drip_does_not_renew_message_deadline(self):
        stop = threading.Event()

        def drip():
            while not stop.wait(0.01):
                try:
                    self.writer.sendall(b"a")
                except OSError:
                    return

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        try:
            with mock.patch("wfd.rtsp.message.RTSP_MESSAGE_TIMEOUT", 0.08):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    _read_rtsp_message(_RTSPReader(self.reader_socket))
                self.assertLess(time.monotonic() - start, 1)
        finally:
            stop.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_header_and_body_share_one_deadline(self):
        reader = _RTSPReader(self.reader_socket)
        self.writer.sendall(START)
        with mock.patch("wfd.rtsp.message.RTSP_MESSAGE_TIMEOUT", 0.2):
            reader.begin_message()
            self.assertEqual(reader.readline(RTSP_MAX_LINE), START)
            deadline = reader.deadline
            self.writer.sendall(b"Content-Length: 1\r\n\r\n")
            reader.readline(RTSP_MAX_LINE)
            reader.readline(RTSP_MAX_LINE)
            self.assertEqual(reader.deadline, deadline)
            with mock.patch("wfd.rtsp.message.time.monotonic", return_value=deadline + 1):
                with self.assertRaises(TimeoutError):
                    reader.read(1)

    def test_play_silence_and_many_messages_have_no_session_cutoff(self):
        reader = _RTSPReader(self.reader_socket, idle_timeout=None)
        result = []

        def read_messages():
            try:
                for _ in range(3):
                    result.append(_read_rtsp_message(reader))
            except Exception as exc:
                result.append(exc)

        with mock.patch("wfd.rtsp.message.RTSP_MESSAGE_TIMEOUT", 0.03):
            thread = threading.Thread(target=read_messages, daemon=True)
            thread.start()
            try:
                for _ in range(3):
                    time.sleep(0.06)
                    self.writer.sendall(START + b"\r\n")
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual([msg.method for msg in result], ["OPTIONS"] * 3)
            finally:
                self.writer.shutdown(socket.SHUT_RDWR)
                thread.join(1)

    def test_incomplete_message_during_play_still_expires(self):
        self.writer.sendall(START + b"Content-Length: 10\r\n\r\na")
        with mock.patch("wfd.rtsp.message.RTSP_MESSAGE_TIMEOUT", 0.05):
            with self.assertRaises(TimeoutError):
                _read_rtsp_message(_RTSPReader(self.reader_socket, idle_timeout=None))


class RTSPConnectionLimitsTest(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(predicate())

    def start_server(self, handler):
        server = _ThreadingTCPServer(("127.0.0.1", 0), handler)
        server.parent_server = SimpleNamespace(authenticate_client=lambda ip: True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(cleanup)
        return server

    def connect(self, server):
        sock = socket.create_connection(server.server_address, timeout=1)
        self.addCleanup(sock.close)
        return sock

    def test_connection_flood_is_capped_and_slots_are_reused(self):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(b"ready")
                _read_rtsp_message(_RTSPReader(self.request, idle_timeout=None))

        server = self.start_server(Handler)
        clients = [self.connect(server) for _ in range(server.max_connections)]
        for client in clients:
            self.assertEqual(client.recv(5), b"ready")
        for _ in range(20):
            with self.connect(server) as rejected:
                self.assertEqual(rejected.recv(1), b"")
            self.assertEqual(len(server._requests), server.max_connections)
        clients[0].close()
        self.wait_for(lambda: len(server._requests) == server.max_connections - 1)
        self.assertEqual(self.connect(server).recv(5), b"ready")
        server.server_close()
        self.wait_for(lambda: not server._requests)

    def test_active_probe_shares_cap_and_shutdown(self):
        server = _ThreadingTCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)
        self.addCleanup(server.server_close)
        parent = WFDRTSPServer(WFDMediaConfig(monitor=None), peer_address="aa:bb:cc:dd:ee:ff")
        parent._server = server
        peers = []
        for _ in range(server.max_connections):
            tracked, peer = socket.socketpair()
            self.addCleanup(tracked.close)
            self.addCleanup(peer.close)
            peer.settimeout(1)
            peers.append(peer)
            self.assertTrue(parent._register_rtsp_socket(tracked))
        extra, peer = socket.socketpair()
        self.addCleanup(extra.close)
        self.addCleanup(peer.close)
        self.assertFalse(parent._register_rtsp_socket(extra))
        parent._unregister_rtsp_socket(tracked)
        self.assertTrue(parent._register_rtsp_socket(extra))
        server.server_close()
        for peer in peers[:-1]:
            self.assertEqual(peer.recv(1), b"")
        self.assertFalse(parent._register_rtsp_socket(tracked))

    def test_thread_start_failure_releases_slot(self):
        server = _ThreadingTCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)
        self.addCleanup(server.server_close)
        request, peer = socket.socketpair()
        self.addCleanup(request.close)
        self.addCleanup(peer.close)
        with mock.patch("threading.Thread.start", side_effect=RuntimeError("no threads")):
            with self.assertRaises(RuntimeError):
                server.process_request(request, ("127.0.0.1", 1))
        self.assertFalse(server._requests)
        self.assertEqual(request.fileno(), -1)

    def test_nonreading_peer_cannot_hold_writer_forever(self):
        finished = threading.Event()
        timed_out = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                    self.request.sendall(b"x" * 1048576)
                except TimeoutError:
                    timed_out.set()
                finally:
                    finished.set()

        server = self.start_server(Handler)
        with mock.patch("wfd.rtsp.rtsp_server.RTSP_WRITE_TIMEOUT", 0.05):
            self.connect(server)
            self.assertTrue(finished.wait(2))
        self.assertTrue(timed_out.is_set())
        self.wait_for(lambda: not server._requests)

    def test_real_handler_releases_owner_after_bad_message_and_idle_timeout(self):
        server = self.start_server(_WFDRTSPHandler)
        parent = WFDRTSPServer(WFDMediaConfig(monitor=None), peer_address="aa:bb:cc:dd:ee:ff")
        parent.authenticate_client = lambda ip: True
        server.parent_server = parent
        server.media_config = parent.media_config
        with mock.patch("wfd.rtsp.handler.RTSP_IDLE_TIMEOUT", 0.05):
            for payload in (b"", START + b"Content-Length: 999999999999999\r\n\r\n"):
                client = self.connect(server)
                reader = client.makefile("rb")
                self.addCleanup(reader.close)
                self.assertEqual(_read_rtsp_message(reader).method, "OPTIONS")
                if payload:
                    client.sendall(payload)
                self.assertEqual(reader.read(1), b"")
                self.wait_for(lambda: not server._requests)
                self.assertIsNone(parent._connected_client)


class RTSPPeriodicStateTest(unittest.TestCase):
    def setUp(self):
        self.handler = _WFDRTSPHandler.__new__(_WFDRTSPHandler)
        self.handler._write_lock = threading.RLock()
        self.handler._timer_lock = threading.RLock()
        self.handler._keepalive_timer = None
        self.handler._probe_timer = None
        self.handler._keepalive_active = True
        self.handler.pending = {}
        self.handler.next_cseq = 1
        self.handler.media = None
        self.handler._send_bytes = mock.Mock()

    def test_unanswered_keepalives_have_bounded_pending_state(self):
        with mock.patch("builtins.print"):
            for _ in range(1000):
                self.handler._send_request("M16_KEEPALIVE", "GET_PARAMETER", "*")
        self.assertEqual(len(self.handler.pending), 1)

    def test_pending_cap_rejects_before_write(self):
        self.handler.pending = {str(i): "M1_OPTIONS" for i in range(RTSP_MAX_PENDING)}
        with self.assertRaises(WFDNotReady):
            self.handler._send_request("M1_OPTIONS", "OPTIONS", "*")
        self.handler._send_bytes.assert_not_called()

    def test_sequence_wrap_does_not_overwrite_pending_request(self):
        self.handler.next_cseq = 2147483647
        self.handler.pending = {"1": "OLD"}
        with mock.patch("builtins.print"):
            self.handler._send_request("LAST", "OPTIONS", "*")
            self.handler._send_request("NEXT", "OPTIONS", "*")
        self.assertEqual(self.handler.pending, {"1": "OLD", "2147483647": "LAST", "2": "NEXT"})

    def test_repeated_play_schedules_only_one_keepalive(self):
        with mock.patch("wfd.rtsp.handler.threading.Timer") as timer:
            for _ in range(100):
                self.handler._schedule_rtsp_keepalive()
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()

    def test_cancelled_callback_cannot_restart_timer(self):
        callback = mock.Mock(return_value=5)
        with mock.patch("wfd.rtsp.handler.threading.Timer") as timer:
            self.handler._schedule_timer("_probe_timer", 1, callback)
            run = timer.call_args.args[1]
            self.handler._stop_media()
            run()
        callback.assert_not_called()
        timer.assert_called_once()

    def test_stop_while_callback_runs_cannot_restart_timer(self):
        def callback():
            self.handler._stop_media()
            return 5

        with mock.patch("wfd.rtsp.handler.threading.Timer") as timer:
            self.handler._schedule_timer("_probe_timer", 1, callback)
            timer.call_args.args[1]()
        timer.assert_called_once()
        self.assertIsNone(self.handler._probe_timer)

    def test_healthy_timer_renews_without_accumulating(self):
        callback = mock.Mock(return_value=5)
        with mock.patch("wfd.rtsp.handler.threading.Timer", side_effect=lambda *a: mock.Mock()) as timer:
            self.handler._schedule_timer("_probe_timer", 1, callback)
            first = self.handler._probe_timer
            timer.call_args.args[1]()
        self.assertEqual(timer.call_count, 2)
        self.assertIsNot(self.handler._probe_timer, first)


if __name__ == "__main__":
    unittest.main()
