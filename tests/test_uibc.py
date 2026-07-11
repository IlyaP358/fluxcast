import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from drivers import uibc  # noqa: E402
from drivers.uibc import (  # noqa: E402
    GENERIC_KEY_DOWN,
    GENERIC_KEY_UP,
    GENERIC_TOUCH_DOWN,
    KeyEvent,
    PointerEvent,
    _keycode_for,
    build_uibc_capability,
    parse_packets,
)


def _key_packet(type_id: int, code1: int) -> bytes:
    # common header (4) + generic body header (type + 2-byte len) + 5-byte body
    body = bytes([0x00, (code1 >> 8) & 0xFF, code1 & 0xFF, 0x00, 0x00])
    total = 4 + 3 + len(body)
    return bytes([0x00, 0x00, (total >> 8) & 0xFF, total & 0xFF,
                  type_id, 0x00, len(body)]) + body


class KeycodeMapTest(unittest.TestCase):
    def test_letters_digits_symbols(self):
        # (received char code, expected Linux key code)
        cases = {
            ord("a"): 30, ord("z"): 44, ord("m"): 50, ord("q"): 16, ord("p"): 25,
            ord("0"): 11, ord("1"): 2, ord("9"): 10,
            ord(" "): 57, ord("/"): 53, ord(";"): 39, ord("'"): 40,
            ord("`"): 41, ord("\\"): 43, ord("["): 26, ord("]"): 27,
            ord(","): 51, ord("."): 52, ord("-"): 12, ord("="): 13,
        }
        for code, expected in cases.items():
            self.assertEqual(_keycode_for(code), expected, f"code {code}")

    def test_control_keys(self):
        self.assertEqual(_keycode_for(8), 14)   # backspace
        self.assertEqual(_keycode_for(9), 15)   # tab
        self.assertEqual(_keycode_for(10), 28)  # LF -> enter
        self.assertEqual(_keycode_for(13), 28)  # CR -> enter

    def test_uppercase_falls_back_to_lowercase(self):
        self.assertEqual(_keycode_for(ord("A")), _keycode_for(ord("a")))

    def test_unmapped_and_out_of_range(self):
        self.assertIsNone(_keycode_for(0))     # blank modifier event
        self.assertIsNone(_keycode_for(127))   # DEL, not mapped
        self.assertIsNone(_keycode_for(300))   # out of ASCII range
        self.assertIsNone(_keycode_for(-1))


class KeyPacketParseTest(unittest.TestCase):
    def test_parse_key_down(self):
        events, consumed = parse_packets(_key_packet(GENERIC_KEY_DOWN, ord("l")))
        self.assertEqual(consumed, 12)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsInstance(ev, KeyEvent)
        self.assertEqual(ev.kind, GENERIC_KEY_DOWN)
        self.assertEqual(ev.code1, 108)
        self.assertEqual(ev.code2, 0)
        self.assertEqual(ev.raw, bytes([0x00, 0x00, 0x6c, 0x00, 0x00]))

    def test_parse_key_up_and_control_code(self):
        events, _ = parse_packets(_key_packet(GENERIC_KEY_UP, 13))
        self.assertEqual(events[0].kind, GENERIC_KEY_UP)
        self.assertEqual(events[0].code1, 13)
        self.assertEqual(_keycode_for(events[0].code1), 28)  # enter

    def test_parse_multiple_and_partial(self):
        buf = _key_packet(GENERIC_KEY_DOWN, ord("h")) + _key_packet(GENERIC_KEY_UP, ord("h"))
        buf += _key_packet(GENERIC_KEY_DOWN, ord("i"))[:6]  # trailing partial packet
        events, consumed = parse_packets(buf)
        self.assertEqual(len(events), 2)
        self.assertEqual(consumed, 24)  # only the two complete packets consumed

    def test_touch_still_parses(self):
        # 1 pointer at (0x0102, 0x0304); guards against regressions in shared parser
        body = bytes([0x01, 0x00, 0x01, 0x02, 0x03, 0x04])
        total = 4 + 3 + len(body)
        pkt = bytes([0x00, 0x00, 0x00, total, GENERIC_TOUCH_DOWN, 0x00, len(body)]) + body
        events, _ = parse_packets(pkt)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], PointerEvent)
        self.assertEqual((events[0].x, events[0].y), (0x0102, 0x0304))


class CapabilityTest(unittest.TestCase):
    def test_advertises_keyboard(self):
        cap = build_uibc_capability(7239)
        self.assertIn("Keyboard", cap)
        self.assertIn("port=7239", cap)


if __name__ == "__main__":
    unittest.main()
