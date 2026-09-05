import asyncio
import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import capture.portal_capture as portal_capture  # noqa: E402
import wfd.media.portal as media_portal  # noqa: E402
from wfd.config import WFDMediaConfig, WFDNotReady  # noqa: E402


class SelectSourcesTypesTest(unittest.TestCase):
    """1=Monitor | 2=Window | 4=Virtual. The WFD pipeline letterboxes, so it
    asks for all three; the DLNA/Cast one does not and must keep getting 5.
    """

    def _types(self, allow_window):
        seen = {}

        async def fake_call(bus, member, signature, body, timeout):
            if member == "CreateSession":
                return {"session_handle": "/x"}
            if member == "SelectSources":
                seen["types"] = body[1]["types"].value
            return {}

        async def drive():
            with mock.patch.object(portal_capture, "_portal_call_with_response", fake_call), \
                 contextlib.suppress(Exception):
                # Start returns nothing usable here; only SelectSources matters.
                await portal_capture._start_portal_capture_async(
                    mock.MagicMock(), timeout=1.0, allow_window=allow_window
                )

        asyncio.run(drive())
        return seen.get("types")

    def test_windows_are_offered_when_the_caller_can_letterbox(self):
        self.assertEqual(self._types(True), 7)

    def test_default_caller_keeps_getting_monitors_only(self):
        self.assertEqual(self._types(False), 5)


class StreamSelectionTest(unittest.TestCase):
    def test_monitor_still_wins_when_both_are_offered(self):
        streams = {"streams": [(1, {"source_type": 2, "size": (3000, 3000)}),
                               (2, {"source_type": 1, "size": (1920, 1080)})]}
        self.assertEqual(portal_capture._extract_stream_node(streams)[0], 2)

    def test_a_lone_window_stream_is_kept(self):
        streams = {"streams": [(7, {"source_type": 2, "size": (900, 500)})]}
        node, source_type, _, _, _ = portal_capture._extract_stream_node(streams)
        self.assertEqual((node, source_type), (7, 2))


class OpenPortalSessionTest(unittest.TestCase):
    class _Holder(media_portal.PortalMixin):
        def __init__(self, config):
            self.config = config
            self.portal_session = None

    def _open(self, source_type):
        config = WFDMediaConfig(monitor=None)
        session = mock.MagicMock(source_type=source_type, size=(800, 600))
        with mock.patch.object(media_portal, "start_portal_capture", return_value=session), \
             mock.patch.object(media_portal, "close_portal_capture"), \
             mock.patch("builtins.print") as printed:
            return self._Holder(config)._open_portal_session(None), printed

    def test_monitor_window_and_virtual_are_accepted(self):
        for source_type in (1, 2, 4):
            self.assertIsNotNone(self._open(source_type)[0])

    def test_camera_source_is_rejected(self):
        with self.assertRaises(WFDNotReady):
            self._open(8)

    def test_every_source_gets_the_revoke_callback(self):
        for source_type in (1, 2, 4):
            session, _ = self._open(source_type)
            self.assertIs(session.on_closed, media_portal._end_session_on_portal_revoke)



class PortalRevokeTest(unittest.TestCase):
    """The portal emits Session.Closed when the shared window is closed or
    sharing is stopped; without acting on it the pipeline keeps pushing the
    last captured frame to the TV (#62).
    """

    def _session_with_handler(self):
        handlers = []
        bus = mock.MagicMock(unique_name=":1.42")
        bus.add_message_handler.side_effect = handlers.append

        async def fake_call(bus_, member, signature, body, timeout):
            if member == "CreateSession":
                return {"session_handle": "/s"}
            if member == "Start":
                return {"streams": [(9, {"source_type": 2, "size": (800, 600)})]}
            return {}

        reply = mock.MagicMock(message_type=None, body=[0], unix_fds=[7])

        async def drive():
            with mock.patch.object(portal_capture, "_portal_call_with_response", fake_call), \
                 mock.patch.object(bus, "call", mock.AsyncMock(return_value=reply)):
                return await portal_capture._start_portal_capture_async(bus, timeout=1.0,
                                                                        allow_window=True)

        return asyncio.run(drive()), handlers[-1]

    @staticmethod
    def _closed_message(path="/s", interface=portal_capture.SESSION_IFACE, member="Closed"):
        return mock.MagicMock(message_type=portal_capture.MessageType.SIGNAL,
                              path=path, interface=interface, member=member)

    def test_closed_signal_runs_the_callback(self):
        session, handler = self._session_with_handler()
        fired = []
        session.on_closed = lambda: fired.append(True)
        handler(self._closed_message())
        self.assertEqual(fired, [True])

    def test_unrelated_signals_are_ignored(self):
        session, handler = self._session_with_handler()
        session.on_closed = lambda: self.fail("should not fire")
        handler(self._closed_message(path="/other"))
        handler(self._closed_message(interface="org.example.Other"))
        handler(self._closed_message(member="Response"))

    def test_our_own_close_does_not_count_as_a_revoke(self):
        session, handler = self._session_with_handler()
        session.on_closed = lambda: self.fail("should not fire")
        session.runtime = None
        session.bus = None
        with mock.patch.object(portal_capture, "asyncio") as fake_asyncio, \
             mock.patch.object(portal_capture.os, "close"):
            fake_asyncio.run.return_value = None
            portal_capture.close_portal_capture(session)
        handler(self._closed_message())


if __name__ == "__main__":
    unittest.main()
