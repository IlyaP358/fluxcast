import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
import server  # noqa: E402


class StreamAccessTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.hls_dir = os.path.join(self._tmpdir.name, "session-test")
        os.makedirs(self.hls_dir, mode=0o700)
        playlist = os.path.join(self.hls_dir, "stream.m3u8")
        with open(playlist, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nstream0.ts\n")
        with open(os.path.join(self.hls_dir, "stream0.ts"), "wb") as fh:
            fh.write(b"\x00" * 64)

        self._hls_dir_patch = mock.patch.object(server, "HLS_DIR", self.hls_dir)
        self._hls_dir_patch.start()
        self.addCleanup(self._hls_dir_patch.stop)

        self.session_id = "session-testtoken"
        self.allowed_client = "127.0.0.1"
        self.stream_server = server.StreamServer(
            host="127.0.0.1",
            port=0,
            handler_class=server.HLSRequestHandler,
            session_id=self.session_id,
            allowed_client=self.allowed_client,
        )
        self.stream_server.start()
        self.addCleanup(self.stream_server.stop)
        self.port = self.stream_server._server.server_address[1]

    def _status(self, path: str, host: str = "127.0.0.1") -> int:
        conn = HTTPConnection(host, self.port, timeout=2)
        try:
            conn.request("HEAD", path)
            return conn.getresponse().status
        finally:
            conn.close()

    def test_binds_only_to_requested_host(self):
        host, _port = self.stream_server._server.server_address
        self.assertEqual(host, "127.0.0.1")

    def test_rejects_bare_root_without_session_prefix(self):
        self.assertEqual(self._status("/"), 404)

    def test_rejects_unprefixed_live_ts(self):
        self.assertEqual(self._status("/live.ts"), 404)

    def test_rejects_wrong_session_prefix(self):
        self.assertEqual(self._status("/session-other/stream.m3u8"), 404)

    def test_allows_correct_session_and_client(self):
        self.assertEqual(self._status(f"/{self.session_id}/stream.m3u8"), 200)

    def test_rejects_disallowed_client(self):
        self.stream_server.allow_client("192.0.2.10")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/{self.session_id}/stream.m3u8",
                timeout=2,
            )
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_rejects_before_allowed_client_is_set(self):
        locked = server.StreamServer(
            host="127.0.0.1",
            port=0,
            handler_class=server.HLSRequestHandler,
            session_id=self.session_id,
            allowed_client=None,
        )
        locked.start()
        self.addCleanup(locked.stop)
        port = locked._server.server_address[1]
        conn = HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("HEAD", f"/{self.session_id}/stream.m3u8")
            self.assertEqual(conn.getresponse().status, 403)
        finally:
            conn.close()


class SessionSetupTest(unittest.TestCase):
    def test_prepare_hls_dir_uses_session_subdir_mode_0700(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server, "HLS_BASE", tmp):
                path = server.prepare_hls_dir("session-abc")
            self.assertEqual(path, os.path.join(tmp, "session-abc"))
            self.assertTrue(os.path.isdir(path))
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o700)
            base_mode = stat.S_IMODE(os.stat(tmp).st_mode)
            self.assertEqual(base_mode, 0o700)
            self.assertEqual(server.HLS_DIR, path)

    def test_new_session_id_uses_secrets(self):
        with mock.patch("secrets.token_urlsafe", return_value="tok123") as token:
            # Import after patch path — exercise helper once it exists
            session_id = server.new_session_id()
        token.assert_called_once()
        self.assertEqual(session_id, "session-tok123")


class MainHlsDirBindingTest(unittest.TestCase):
    def test_wait_for_hls_segments_follows_prepare_hls_dir(self):
        """main must read server.HLS_DIR dynamically, not import it by value."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server, "HLS_BASE", tmp):
                session_dir = server.prepare_hls_dir("session-binding")
            playlist = os.path.join(session_dir, "stream.m3u8")
            with open(playlist, "w", encoding="utf-8") as fh:
                fh.write(
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n"
                    "#EXTINF:1.0,\nstream0.ts\n"
                    "#EXTINF:1.0,\nstream1.ts\n"
                )
            for name in ("stream0.ts", "stream1.ts"):
                with open(os.path.join(session_dir, name), "wb") as fh:
                    fh.write(b"\x00" * 64)

            # Would time out if main still pointed at the pre-session HLS_DIR.
            self.assertTrue(
                main._wait_for_hls_segments(required_segments=2, timeout=1.0)
            )


class DeviceIpTest(unittest.TestCase):
    def test_cast_device_ip(self):
        device = mock.Mock()
        device.cast_info.host = "192.168.1.50"
        self.assertEqual(server.device_client_ip(device, protocol="cast"), "192.168.1.50")

    def test_dlna_device_ip_from_location(self):
        device = mock.Mock()
        device.location = "http://192.168.1.60:9197/DeviceDescription.xml"
        self.assertEqual(server.device_client_ip(device, protocol="dlna"), "192.168.1.60")


if __name__ == "__main__":
    unittest.main()
