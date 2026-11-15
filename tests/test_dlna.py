import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dlna import dlna  # noqa: E402


def _device(sink=None, name="Test TV", model="Test Model"):
    device = mock.Mock()
    device.friendly_name = name
    device.model_name = model
    if sink is None:
        device.ConnectionManager.GetProtocolInfo.side_effect = RuntimeError("no CM")
    else:
        device.ConnectionManager.GetProtocolInfo.return_value = {"Sink": sink}
    device.AVTransport.GetTransportInfo.return_value = {
        "CurrentTransportState": "PLAYING"
    }
    return device


class SinkProtocolInfoTest(unittest.TestCase):
    def test_hls_device_keeps_hls_protocol_info(self):
        device = _device("http-get:*:application/x-mpegurl:*,http-get:*:video/mpeg:*")
        self.assertEqual(
            dlna._protocol_info("http://host/session/stream.m3u8", device),
            dlna.HLS_PROTOCOL_INFO,
        )

    def test_device_without_hls_gets_generic_video_mime(self):
        # Real EShare-5322 sink list: HLS is not advertised even though the
        # renderer can fetch m3u8.  DLNA-compliant renderers reject a
        # protocolInfo they never listed, so advertise video/mpeg instead.
        device = _device(
            "http-get:*:image/png:*,http-get:*:video/mpeg:*,"
            "http-get:*:video/mp4:*"
        )
        self.assertEqual(
            dlna._protocol_info("http://host/session/stream.m3u8", device),
            dlna.GENERIC_VIDEO_PROTOCOL_INFO,
        )

    def test_samsung_keeps_hls_metadata_even_when_not_advertised(self):
        device = _device(
            "http-get:*:video/mpeg:*",
            name="Samsung TV",
            model="UE55TU7092U",
        )
        self.assertEqual(
            dlna._protocol_info("http://host/session/stream.m3u8", device),
            dlna.HLS_PROTOCOL_INFO,
        )

    def test_samsung_keeps_profile_for_ts(self):
        device = _device(
            "http-get:*:video/mpeg:*",
            name="[TV] Samsung",
            model="UE55TU7092U",
        )
        self.assertEqual(
            dlna._protocol_info("http://host/session/progressive.ts", device),
            dlna.TS_PROTOCOL_INFO,
        )

    def test_device_query_failure_keeps_hls_protocol_info(self):
        device = _device()
        self.assertEqual(
            dlna._protocol_info("http://host/session/stream.m3u8", device),
            dlna.HLS_PROTOCOL_INFO,
        )

    def test_ts_device_with_matching_profile_keeps_full_protocol_info(self):
        device = _device("http-get:*:video/mpeg:DLNA.ORG_PN=AVC_TS_MP_HD_AAC;*")
        self.assertEqual(
            dlna._protocol_info("http://host/session/progressive.ts", device),
            dlna.TS_PROTOCOL_INFO,
        )

    def test_ts_device_without_profile_gets_generic_video_mime(self):
        device = _device("http-get:*:video/mpeg:*")
        self.assertEqual(
            dlna._protocol_info("http://host/session/progressive.ts", device),
            dlna.GENERIC_VIDEO_PROTOCOL_INFO,
        )

    def test_metadata_escapes_url(self):
        metadata = dlna._build_didl_metadata(
            "http://host/session/stream.ts?x=1&y=2",
            protocol_info=dlna.GENERIC_VIDEO_PROTOCOL_INFO,
        )
        self.assertIn("x=1&amp;y=2", metadata)
        self.assertIn(
            'protocolInfo="http-get:*:video/mpeg:*"',
            metadata,
        )


class StartCastTest(unittest.TestCase):
    def test_success_when_renderer_reports_playing(self):
        device = _device("http-get:*:video/mpeg:*")
        with mock.patch.object(dlna.time, "sleep"):
            self.assertTrue(
                dlna.start_cast(device, "http://host/session/stream.m3u8")
            )

        device.AVTransport.SetAVTransportURI.assert_called_once()
        device.AVTransport.Play.assert_called_once()
        metadata = device.AVTransport.SetAVTransportURI.call_args.kwargs[
            "CurrentURIMetaData"
        ]
        self.assertIn('protocolInfo="http-get:*:video/mpeg:*"', metadata)

    def test_success_when_renderer_starts_fetching_stream(self):
        device = _device("http-get:*:video/mpeg:*")
        device.AVTransport.GetTransportInfo.return_value = {
            "CurrentTransportState": "STOPPED"
        }
        with mock.patch.object(dlna.time, "sleep"):
            self.assertTrue(
                dlna.start_cast(
                    device,
                    "http://host/session/stream.m3u8",
                    is_active=lambda: True,
                )
            )

    def test_retries_with_generic_metadata_after_silent_renderer(self):
        device = _device("http-get:*:application/x-mpegurl:*")
        with mock.patch.object(dlna.time, "sleep"), mock.patch.object(
            dlna, "_wait_for_playback", side_effect=[False, False, True]
        ):
            self.assertTrue(
                dlna.start_cast(device, "http://host/session/stream.m3u8")
            )

        calls = device.AVTransport.SetAVTransportURI.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn(
            "application/x-mpegurl",
            calls[0].kwargs["CurrentURIMetaData"],
        )
        self.assertIn(
            'protocolInfo="http-get:*:video/mpeg:*"',
            calls[1].kwargs["CurrentURIMetaData"],
        )

    def test_returns_false_after_all_attempts_and_reports_error(self):
        device = _device("http-get:*:application/x-mpegurl:*")
        with mock.patch.object(dlna.time, "sleep"), \
                mock.patch.object(dlna, "_wait_for_playback", return_value=False):
            self.assertFalse(
                dlna.start_cast(device, "http://host/session/stream.m3u8")
            )

        # primary, generic/empty, empty metadata
        self.assertEqual(device.AVTransport.SetAVTransportURI.call_count, 3)
        # one Play plus one nudge per attempt
        self.assertEqual(device.AVTransport.Play.call_count, 6)

    def test_playback_wait_uses_activity_probe(self):
        device = _device()
        device.AVTransport.GetTransportInfo.return_value = {
            "CurrentTransportState": "STOPPED"
        }
        with mock.patch.object(dlna.time, "sleep"):
            self.assertTrue(
                dlna._wait_for_playback(
                    device, is_active=lambda: True, timeout=0.01
                )
            )


class TransportStateTest(unittest.TestCase):
    def test_unreachable_device_returns_none(self):
        device = mock.Mock()
        device.AVTransport.GetTransportInfo.side_effect = RuntimeError("offline")
        self.assertIsNone(dlna.transport_state(device))


class WatchPlaybackTest(unittest.TestCase):
    @mock.patch.object(dlna.time, "sleep")
    @mock.patch.object(dlna, "transport_state", side_effect=["STOPPED"] * 6)
    def test_exits_after_renderer_stays_stopped(self, state, _sleep):
        dlna.watch_playback(object(), idle_limit=6)
        self.assertEqual(state.call_count, 6)

    @mock.patch.object(dlna.time, "sleep")
    @mock.patch.object(
        dlna,
        "transport_state",
        side_effect=["PLAYING", "STOPPED", "STOPPED", "PLAYING"]
        + ["STOPPED"] * 6,
    )
    def test_playing_resets_the_miss_counter(self, state, _sleep):
        dlna.watch_playback(object(), idle_limit=6)
        self.assertEqual(state.call_count, 10)

    @mock.patch.object(dlna.time, "sleep")
    @mock.patch.object(
        dlna,
        "transport_state",
        side_effect=["PAUSED_PLAYBACK"] + ["STOPPED"] * 6,
    )
    def test_paused_renderer_keeps_the_session_alive(self, state, _sleep):
        dlna.watch_playback(object(), idle_limit=6)
        self.assertEqual(state.call_count, 7)


if __name__ == "__main__":
    unittest.main()
