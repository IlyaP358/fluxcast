import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# pychromecast is an optional runtime dependency; stub it so the module imports.
for _name, _attrs in {
    "pychromecast": {},
    "pychromecast.const": {"CAST_TYPE_CHROMECAST": "cast"},
    "pychromecast.discovery": {"discover_chromecasts": None},
    "pychromecast.models": {"CastInfo": None, "HostServiceInfo": None},
}.items():
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__dict__.update(_attrs)
        sys.modules[_name] = _mod

from cast import cast as cast_mod  # noqa: E402


class _Device:
    def __init__(self, app_id, switches=True):
        self.status = types.SimpleNamespace(app_id=app_id)
        self.switches = switches
        self.started = []
        self.cast_info = types.SimpleNamespace(friendly_name="TV")
        self.media_controller = mock.Mock()

    def wait(self):
        pass

    def register_connection_listener(self, _):
        pass

    def start_app(self, app_id):
        self.started.append(app_id)
        if self.switches:
            self.status = types.SimpleNamespace(app_id=app_id)


class EnsureMediaReceiverTests(unittest.TestCase):
    def test_launches_receiver_before_load_when_other_app_foreground(self):
        dev = _Device("AndroidNativeApp")
        order = []
        orig = dev.start_app
        dev.start_app = lambda a: (order.append("start"), orig(a))
        dev.media_controller.play_media.side_effect = lambda *a: order.append("load")
        cast_mod.start_cast(dev, "http://x/s.m3u8")
        self.assertEqual(order, ["start", "load"])

    def test_skips_launch_when_receiver_already_running(self):
        dev = _Device(cast_mod.APP_MEDIA_RECEIVER)
        cast_mod.start_cast(dev, "http://x/s.m3u8")
        self.assertEqual(dev.started, [])
        dev.media_controller.play_media.assert_called_once()

    def test_warns_but_continues_on_timeout(self):
        dev = _Device("AndroidNativeApp", switches=False)
        with mock.patch.object(cast_mod.time, "sleep"), \
             mock.patch.object(cast_mod.time, "monotonic", side_effect=[0, 1, 99]):
            cast_mod._ensure_media_receiver(dev)
        self.assertEqual(dev.started, [cast_mod.APP_MEDIA_RECEIVER])


if __name__ == "__main__":
    unittest.main()
