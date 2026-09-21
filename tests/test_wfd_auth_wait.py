import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig
from wfd.proc import _run
from wfd.rtsp.rtsp_server import WFDRTSPServer


PEER_MAC = "00:51:ed:35:0f:ae"
PEER_IP = "10.42.0.182"
GROUP = "p2p-wlan0-3"


class ReceiverNeighbourWaitTest(unittest.TestCase):
    def setUp(self):
        self.server = WFDRTSPServer(
            WFDMediaConfig(monitor=None), peer_address=PEER_MAC, interface=GROUP,
        )
        self.now = 0.0
        for target, replacement in (
            ("time.monotonic", lambda: self.now),
            ("time.sleep", self.advance),
            ("wfd.p2p.addressing.shutil.which", lambda command: "/usr/bin/ip"),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def result(self, rows):
        return subprocess.CompletedProcess([], 0, rows, "")

    def test_delayed_exact_and_randomized_neighbours_are_accepted(self):
        for mac in (PEER_MAC, "7a:11:22:33:44:55"):
            with self.subTest(mac=mac):
                self.now = 0
                results = [self.result(""), self.result(f"{PEER_IP} INCOMPLETE"),
                           self.result(f"{PEER_IP} lladdr {mac} REACHABLE")]
                with mock.patch("wfd.p2p.addressing._run", side_effect=results) as query:
                    self.assertTrue(self.server.authenticate_client(PEER_IP))
                self.assertEqual(query.call_count, 3)
                for index, call in enumerate(query.call_args_list):
                    self.assertEqual(call.args[0], ["ip", "neigh", "show", "dev", GROUP])
                    self.assertAlmostEqual(call.kwargs["timeout"], 2.0 - index * 0.1)

    def test_missing_wrong_interface_and_ambiguous_neighbours_expire(self):
        cases = (
            "", f"{PEER_IP} INCOMPLETE", f"{PEER_IP} lladdr {PEER_MAC} FAILED",
            f"{PEER_IP} dev wlan0 lladdr {PEER_MAC} REACHABLE",
            f"{PEER_IP} dev p2p-wlan0-4 lladdr {PEER_MAC} REACHABLE",
            f"{PEER_IP} lladdr 7a:11:22:33:44:55 REACHABLE\n"
            "10.42.0.183 lladdr 7a:11:22:33:44:56 REACHABLE",
        )
        for rows in cases:
            with self.subTest(rows=rows):
                self.now = 0
                with mock.patch("wfd.p2p.addressing._run", return_value=self.result(rows)) as query:
                    self.assertFalse(self.server.authenticate_client(PEER_IP))
                self.assertAlmostEqual(self.now, 2.0)
                self.assertGreater(query.call_count, 1)
                self.assertLessEqual(query.call_count, 21)
                self.assertFalse(self.server.has_connected_client)
                self.assertIsNone(self.server._connected_client)

    def test_subprocess_timeout_uses_remaining_budget(self):
        def query(command, timeout):
            self.advance(timeout)
            raise subprocess.TimeoutExpired(command, timeout)

        with mock.patch("wfd.p2p.addressing._run", side_effect=query) as run:
            self.assertFalse(self.server.authenticate_client(PEER_IP))
        self.assertEqual(run.call_count, 1)
        self.assertAlmostEqual(self.now, 2.0)

    def test_query_failure_cannot_turn_into_acceptance(self):
        with mock.patch("wfd.p2p.addressing._run", return_value=subprocess.CompletedProcess([], 1, "", "failed")):
            self.assertFalse(self.server.authenticate_client(PEER_IP))
        self.assertAlmostEqual(self.now, 2.0)

    def test_late_success_is_not_accepted_after_deadline(self):
        def query(command, timeout):
            self.advance(timeout + 0.1)
            return self.result(f"{PEER_IP} lladdr {PEER_MAC} REACHABLE")

        with mock.patch("wfd.p2p.addressing._run", side_effect=query):
            self.assertFalse(self.server.authenticate_client(PEER_IP))

    def test_missing_group_does_not_query_neighbours(self):
        self.server = WFDRTSPServer(WFDMediaConfig(monitor=None), peer_address=PEER_MAC)
        with (
            mock.patch.object(self.server._group_interface_ready, "wait", return_value=False) as wait,
            mock.patch("wfd.p2p.addressing._run") as query,
        ):
            self.assertFalse(self.server.authenticate_client(PEER_IP))
        wait.assert_called_once_with(timeout=2.0)
        query.assert_not_called()

    def test_lock_contention_is_part_of_neighbour_deadline(self):
        def acquire(*, timeout):
            self.advance(timeout)
            return False

        self.server._auth_lock = mock.Mock()
        self.server._auth_lock.acquire.side_effect = acquire
        with mock.patch("wfd.p2p.addressing._run") as query:
            self.assertFalse(self.server.authenticate_client(PEER_IP))
        self.assertAlmostEqual(self.now, 2.0)
        query.assert_not_called()
        self.server._auth_lock.release.assert_not_called()

    def test_lock_wait_reduces_subprocess_budget(self):
        def acquire(*, timeout):
            self.advance(1.5)
            return True

        self.server._auth_lock = mock.Mock()
        self.server._auth_lock.acquire.side_effect = acquire
        with mock.patch("wfd.p2p.addressing._run", return_value=self.result(f"{PEER_IP} lladdr {PEER_MAC} REACHABLE")) as query:
            self.assertTrue(self.server.authenticate_client(PEER_IP))
        self.assertAlmostEqual(query.call_args.kwargs["timeout"], 0.5)
        self.server._auth_lock.release.assert_called_once_with()

    def test_immediately_verified_peer_does_not_wait(self):
        with mock.patch("wfd.p2p.addressing._run", return_value=self.result(f"{PEER_IP} lladdr {PEER_MAC} REACHABLE")) as query:
            self.assertTrue(self.server.authenticate_client(PEER_IP))
        query.assert_called_once()
        self.assertEqual(self.now, 0)

    def test_retry_sleeps_do_not_hold_the_lookup_lock(self):
        def sleep(seconds):
            self.assertFalse(self.server._auth_lock.locked())
            self.advance(seconds)

        with (
            mock.patch("time.sleep", side_effect=sleep),
            mock.patch("wfd.p2p.addressing._run", return_value=self.result("")),
        ):
            self.assertFalse(self.server.authenticate_client(PEER_IP))
        self.assertAlmostEqual(self.now, 2.0)

    def test_expired_lock_acquisition_skips_query_and_releases_lock(self):
        def acquire(*, timeout):
            self.advance(timeout + 0.1)
            return True

        self.server._auth_lock = mock.Mock()
        self.server._auth_lock.acquire.side_effect = acquire
        with mock.patch("wfd.p2p.addressing._run") as query:
            self.assertFalse(self.server.authenticate_client(PEER_IP))
        query.assert_not_called()
        self.server._auth_lock.release.assert_called_once_with()

    def test_unexpected_lookup_exception_releases_lock(self):
        with mock.patch("wfd.rtsp.rtsp_server._is_expected_peer_ip", side_effect=RuntimeError("lookup failed")):
            with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                self.server.authenticate_client(PEER_IP)
        self.assertFalse(self.server._auth_lock.locked())


class ReceiverNeighbourTimingTest(unittest.TestCase):
    def test_unverified_retries_do_not_starve_verified_receiver(self):
        server = WFDRTSPServer(
            WFDMediaConfig(monitor=None), peer_address=PEER_MAC, interface=GROUP,
        )
        first_query = threading.Event()
        verified_waiting = threading.Event()
        verified_done = threading.Event()
        unverified_done = threading.Event()
        results = {}
        errors = []
        query_threads = set()
        lock = server._auth_lock

        class ObservedLock:
            def acquire(self, *, timeout):
                if threading.current_thread() is verified:
                    verified_waiting.set()
                return lock.acquire(timeout=timeout)

            def release(self):
                lock.release()

        server._auth_lock = ObservedLock()

        def query(command, timeout):
            current = threading.current_thread()
            if query_threads:
                errors.append("neighbour queries overlapped")
            query_threads.add(current)
            try:
                if not first_query.is_set():
                    first_query.set()
                    if not verified_waiting.wait(1):
                        errors.append("verified receiver did not reach lookup lock")
                time.sleep(min(0.05, timeout))
                if timeout < 0.05:
                    raise subprocess.TimeoutExpired(command, timeout)
                return subprocess.CompletedProcess(command, 0, f"{PEER_IP} lladdr {PEER_MAC} REACHABLE", "")
            finally:
                query_threads.remove(current)

        def authenticate(label, address, done):
            try:
                results[label] = server.authenticate_client(address)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        unverified = threading.Thread(
            target=authenticate, args=("unverified", "192.168.1.50", unverified_done), daemon=True,
        )
        verified = threading.Thread(
            target=authenticate, args=("verified", PEER_IP, verified_done), daemon=True,
        )
        with (
            mock.patch("wfd.p2p.addressing.shutil.which", return_value="/usr/bin/ip"),
            mock.patch("wfd.p2p.addressing._run", side_effect=query),
        ):
            unverified.start()
            try:
                self.assertTrue(first_query.wait(1))
                verified.start()
                self.assertTrue(verified_done.wait(1), "valid receiver blocked behind retries")
                self.assertTrue(results.get("verified"))
                self.assertFalse(unverified_done.is_set())
            finally:
                unverified.join(3)
                if verified.ident is not None:
                    verified.join(3)
        self.assertFalse(unverified.is_alive())
        self.assertFalse(verified.is_alive())
        self.assertFalse(results.get("unverified", True))
        self.assertEqual(errors, [])

    def test_real_subprocess_cannot_extend_neighbour_deadline(self):
        server = WFDRTSPServer(
            WFDMediaConfig(monitor=None), peer_address=PEER_MAC, interface=GROUP,
        )

        def slow_query(command, timeout):
            return _run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=timeout)

        with (
            mock.patch("wfd.p2p.addressing.shutil.which", return_value="/usr/bin/ip"),
            mock.patch("wfd.p2p.addressing._run", side_effect=slow_query) as query,
        ):
            started = time.monotonic()
            self.assertFalse(server.authenticate_client(PEER_IP))
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 3.5)
        query.assert_called_once()
        self.assertTrue(server._auth_lock.acquire(blocking=False))
        server._auth_lock.release()

    def test_tcp_admission_waits_for_delayed_neighbour(self):
        server = WFDRTSPServer(
            WFDMediaConfig(monitor=None), peer_address=PEER_MAC,
            interface=GROUP, host="127.0.0.1", port=0,
        )
        queries = []

        def neighbour_query(command, timeout):
            queries.append((command, timeout))
            rows = "" if len(queries) < 3 else f"127.0.0.1 lladdr {PEER_MAC} REACHABLE"
            return subprocess.CompletedProcess(command, 0, rows, "")

        with (
            mock.patch("wfd.p2p.addressing.shutil.which", return_value="/usr/bin/ip"),
            mock.patch("wfd.p2p.addressing._run", side_effect=neighbour_query),
        ):
            server.start()
            try:
                with socket.create_connection(server._server.server_address, timeout=3) as client:
                    self.assertTrue(client.recv(4096).startswith(b"OPTIONS * RTSP/1.0"))
                    self.assertEqual(server._connected_client, "127.0.0.1")
                deadline = time.monotonic() + 2
                while server._connected_client is not None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNone(server._connected_client)
            finally:
                server.stop()
                server._thread.join(2)
        self.assertFalse(server._thread.is_alive())
        self.assertGreaterEqual(len(queries), 4)
        for command, timeout in queries:
            self.assertEqual(command, ["ip", "neigh", "show", "dev", GROUP])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2.0)


if __name__ == "__main__":
    unittest.main()
