"""IO watchdog freeze detection and encode-stall debounce."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.media.wlroots import (  # noqa: E402
    impairment_debounce_seconds,
    io_watch_is_frozen,
)


class IoWatchFrozenTest(unittest.TestCase):
    def test_both_requires_rchar_and_wchar_flat(self):
        last = (100, 200)
        self.assertTrue(io_watch_is_frozen(last, (100, 200), "both"))
        self.assertFalse(io_watch_is_frozen(last, (101, 200), "both"))
        self.assertFalse(io_watch_is_frozen(last, (100, 201), "both"))

    def test_wchar_ignores_flat_rchar(self):
        last = (100, 200)
        # DMA wf-recorder: rchar stays flat while wchar is annex-B into the muxer.
        self.assertFalse(io_watch_is_frozen(last, (100, 201), "wchar"))
        self.assertTrue(io_watch_is_frozen(last, (999, 200), "wchar"))
        self.assertTrue(io_watch_is_frozen(last, (100, 200), "wchar"))


class ImpairmentDebounceTest(unittest.TestCase):
    def test_encode_stall_is_faster_than_dts(self):
        self.assertEqual(impairment_debounce_seconds("encode-stall"), 4.0)
        self.assertEqual(impairment_debounce_seconds("buffer-pool"), 8.0)
        self.assertEqual(impairment_debounce_seconds("bufs-size-storm"), 8.0)
        self.assertEqual(impairment_debounce_seconds("dts"), 20.0)


if __name__ == "__main__":
    unittest.main()
