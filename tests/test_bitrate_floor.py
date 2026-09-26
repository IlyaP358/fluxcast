import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.config import WFDMediaConfig  # noqa: E402
from wfd.encoding import _effective_kbits, _quality_floor_kbits  # noqa: E402


def _config(**kwargs):
    return WFDMediaConfig(monitor=None, **kwargs)


class QualityFloorTest(unittest.TestCase):
    """The floor exists so someone who never passed --bitrate still gets a
    legible desktop. It was also overriding people who did pass one, so there
    was no way to ask for less than the floor - on a link that could not carry
    it, that is the difference between a poor picture and no picture (#80).
    """

    # 1080p30's floor, so the numbers below are not magic.
    FLOOR_1080P30 = _quality_floor_kbits(1920, 1080, 30)

    def test_the_floor_still_raises_an_unspecified_bitrate(self):
        # The default path must be byte-identical to before the change.
        config = _config(bitrate="4M", fps=30)
        self.assertEqual(
            _effective_kbits(config, 4000, 1920, 1080), self.FLOOR_1080P30
        )
        self.assertGreater(self.FLOOR_1080P30, 4000)

    def test_an_explicit_bitrate_below_the_floor_is_honoured(self):
        config = _config(bitrate="2M", fps=30, bitrate_explicit=True)
        self.assertEqual(_effective_kbits(config, 2000, 1920, 1080), 2000)

    def test_an_explicit_bitrate_above_the_floor_is_left_alone(self):
        config = _config(bitrate="20M", fps=30, bitrate_explicit=True)
        self.assertEqual(_effective_kbits(config, 20000, 1920, 1080), 20000)

    def test_explicitness_is_the_only_thing_that_changes_the_answer(self):
        for width, height, fps in ((640, 480, 30), (1280, 720, 60), (1920, 1080, 30)):
            requested = 1000
            implicit = _effective_kbits(
                _config(fps=fps), requested, width, height)
            explicit = _effective_kbits(
                _config(fps=fps, bitrate_explicit=True), requested, width, height)
            self.assertEqual(implicit, _quality_floor_kbits(width, height, fps))
            self.assertEqual(explicit, requested)


class BitrateSentinelTest(unittest.TestCase):
    """--bitrate defaults to None purely so an explicit value can be told apart
    from the default. None must not escape parse_args: the DLNA and Cast path
    passes args.bitrate straight into start_capture, which calls .upper() on
    it, so a leaked None would crash every non-WFD user.
    """

    def _parse(self, argv):
        import main
        with mock.patch.object(sys, "argv", ["fluxcast", *argv]):
            return main.parse_args()

    def test_default_is_substituted_back_and_marked_implicit(self):
        args = self._parse([])
        self.assertEqual(args.bitrate, "4M")
        self.assertIs(args.bitrate_explicit, False)

    def test_an_explicit_value_is_kept_and_marked(self):
        args = self._parse(["--bitrate", "2M"])
        self.assertEqual(args.bitrate, "2M")
        self.assertIs(args.bitrate_explicit, True)

    def test_bitrate_is_never_none_whatever_was_passed(self):
        # The crash guard, stated as its own assertion.
        for argv in ([], ["--bitrate", "2M"], ["--protocol", "dlna"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(self._parse(argv).bitrate)


if __name__ == "__main__":
    unittest.main()
