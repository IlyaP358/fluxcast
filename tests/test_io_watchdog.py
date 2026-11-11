"""IO watchdog freeze detection and encode-stall debounce."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.media.wlroots import (  # noqa: E402
    bufs_size_is_storm,
    impairment_debounce_seconds,
    io_watch_is_frozen,
    stderr_rebind_reason,
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


class BufsSizeStormTest(unittest.TestCase):
    def test_pool_growth_is_not_a_storm(self):
        # 14 is wf-recorder's capture-pause high water, not a dead encoder.
        self.assertFalse(bufs_size_is_storm([5, 6, 7, 8, 9, 10, 11]))
        self.assertFalse(bufs_size_is_storm([5, 8, 12, 14, 16]))
        self.assertFalse(bufs_size_is_storm([]))


class StderrRebindReasonTest(unittest.TestCase):
    def test_encode_wait_is_not_a_restart(self):
        self.assertIsNone(
            stderr_rebind_reason(
                "encode stall: waiting for frame >1000ms (pending=4)"
            )
        )

    def test_pause_timeout_still_rebinds(self):
        self.assertEqual(
            stderr_rebind_reason(
                "capture pause timeout: encode_idle_ms=2500 pending=14"
            ),
            "encode-stall",
        )


class ImpairmentDebounceTest(unittest.TestCase):
    def test_encode_stall_is_faster_than_dts(self):
        self.assertEqual(impairment_debounce_seconds("encode-stall"), 4.0)
        self.assertEqual(impairment_debounce_seconds("buffer-pool"), 8.0)
        self.assertEqual(impairment_debounce_seconds("bufs-size-storm"), 8.0)
        self.assertEqual(impairment_debounce_seconds("dts"), 20.0)


if __name__ == "__main__":
    unittest.main()
