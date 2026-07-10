"""
UIBC (User Input Back Channel), WFD *source* side. Issue #37.

This module is fully self-contained and opt-in: nothing here runs unless
`UIBCServer` is explicitly started. It never touches the streaming path, so it
cannot regress any working device.

Pipeline:
    sink --TCP--> UIBCServer --parse--> CoordinateMapper --> UinputPointer --> /dev/uinput

Wire format (Wi-Fi Display spec, cross-checked against miraclecast
`src/uibc/miracle-uibcctl.c`):

    Common packet header (4 bytes)
        byte 0 : version(3b) | T timestamp flag(1b) | reserved(4b)   -> 0x00
        byte 1 : reserved(4b, high nibble) | input category(4b, low nibble)  -> 0x00 = GENERIC
        byte 2-3 : total packet length, big-endian (header + body)
      (if T=1: 2 extra timestamp bytes follow the header, i skip them)

    Generic input body
        byte 4   : input type id (see GENERIC_* below)
        byte 5-6 : generic body length, big-endian
        byte 7.. : type-specific body

    Touch / mouse body
        byte 7 : number of pointers
        per pointer (5 bytes): id(1) | X(2 BE) | Y(2 BE)
        X/Y are pixels in the *negotiated* video resolution, origin top-left.

    Key body
        byte 7     : reserved
        byte 8-9   : key code 1 (BE)
        byte 10-11 : key code 2 (BE)
"""

import ctypes
import fcntl
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

UIBC_CATEGORY_GENERIC = 0x0

GENERIC_TOUCH_DOWN = 0
GENERIC_TOUCH_UP = 1
GENERIC_TOUCH_MOVE = 2
GENERIC_KEY_DOWN = 3
GENERIC_KEY_UP = 4
GENERIC_ZOOM = 5
GENERIC_VSCROLL = 6
GENERIC_HSCROLL = 7
GENERIC_ROTATE = 8

_HEADER_LEN = 4
_GENERIC_BODY_HEADER_LEN = 3  # input-type(1) + length(2)


def build_uibc_capability(port: int) -> str:
    """Value for the `wfd_uibc_capability` RTSP parameter (M3/M4).

    Advertises the GENERIC category with mouse, single-touch and keyboard, plus
    the TCP port the sink should connect to for input. The sink only sends input
    types present here, so keyboard must be advertised to receive key events
    (they are parsed and logged; injection is a follow-up).
    """
    return (
        "input_category_list=GENERIC;"
        "generic_cap_list=Keyboard, Mouse, SingleTouch;"
        "hidc_cap_list=none;"
        f"port={port}"
    )


# ── parsed events ───────────────────────────────────────────────────────────

@dataclass
class PointerEvent:
    kind: int          # GENERIC_TOUCH_DOWN / _UP / _MOVE
    x: int             # sink-space pixel coordinate (negotiated resolution)
    y: int


@dataclass
class KeyEvent:
    kind: int          # GENERIC_KEY_DOWN / _UP
    code1: int
    code2: int
    raw: bytes = b""   # full body bytes, for inspecting modifier encoding


def parse_packets(buf: bytes) -> tuple[list, int]:
    """Parse as many complete UIBC packets as "buf" contains.

    Returns (events, consumed_bytes). Leftover bytes (a partial packet) stay in
    the caller's buffer for the next read. Malformed packets are skipped by
    resyncing one byte at a time rather than crashing.
    """
    events: list = []
    offset = 0
    n = len(buf)

    while n - offset >= _HEADER_LEN:
        b0 = buf[offset]
        category = buf[offset + 1] & 0x0F  # Input Category = low nibble (spec WIFI ALLIANCE Fig.20, bits 12-15)
        total_len = (buf[offset + 2] << 8) | buf[offset + 3]

        # Sanity: length must at least cover the header, and not be ABSURD.
        if total_len < _HEADER_LEN or total_len > 4096:
            offset += 1  # resync
            continue
        if n - offset < total_len:
            break  # wait for the rest of this packet

        packet = buf[offset:offset + total_len]
        offset += total_len

        if category != UIBC_CATEGORY_GENERIC:
            continue  # HIDC not supported yet

        has_ts = bool((b0 >> 4) & 0x1)
        body_start = _HEADER_LEN + (2 if has_ts else 0)
        _parse_generic_body(packet, body_start, events)

    return events, offset


def _parse_generic_body(packet: bytes, start: int, events: list) -> None:
    if len(packet) < start + _GENERIC_BODY_HEADER_LEN:
        return
    type_id = packet[start]
    body = packet[start + _GENERIC_BODY_HEADER_LEN:]

    if type_id in (GENERIC_TOUCH_DOWN, GENERIC_TOUCH_UP, GENERIC_TOUCH_MOVE):
        if not body:
            return
        count = body[0]
        pos = 1
        for _ in range(count):
            if pos + 5 > len(body):
                break
            # pointer id at body[pos]
            x = (body[pos + 1] << 8) | body[pos + 2]
            y = (body[pos + 3] << 8) | body[pos + 4]
            events.append(PointerEvent(type_id, x, y))
            pos += 5
    elif type_id in (GENERIC_KEY_DOWN, GENERIC_KEY_UP):
        if len(body) >= 5:
            code1 = (body[1] << 8) | body[2]
            code2 = (body[3] << 8) | body[4]
            events.append(KeyEvent(type_id, code1, code2, bytes(body[:16])))
    # zoom/scroll/rotate ignored for now =/


# ── coordinate mapping ──────────────────────────────────────────────────────

class CoordinateMapper:
    """Maps sink-space pixels (negotiated resolution) to source screen pixels.

    The sink reports coordinates in the resolution negotiated (e.g. 1920x1080).
    The captured monitor may be a different size/offset, so scale then translate.
    """

    def __init__(self, sink_w: int, sink_h: int,
                 mon_w: int, mon_h: int, mon_x: int = 0, mon_y: int = 0):
        self._sink_w = max(1, sink_w)
        self._sink_h = max(1, sink_h)
        self._mon_w = mon_w
        self._mon_h = mon_h
        self._mon_x = mon_x
        self._mon_y = mon_y

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        sx = self._mon_x + round(x * self._mon_w / self._sink_w)
        sy = self._mon_y + round(y * self._mon_h / self._sink_h)
        sx = min(max(sx, self._mon_x), self._mon_x + self._mon_w - 1)
        sy = min(max(sy, self._mon_y), self._mon_y + self._mon_h - 1)
        return sx, sy


# ── uinput injection (raw ctypes, no external dependency) ────────────────────

# Event types / codes (linux/input-event-codes.h)
_EV_SYN, _EV_KEY, _EV_ABS = 0x00, 0x01, 0x03
_SYN_REPORT = 0x00
_ABS_X, _ABS_Y = 0x00, 0x01
_BTN_LEFT, _BTN_TOUCH = 0x110, 0x14A
_ABS_CNT = 0x40
# Multitouch (protocol B) axes + direct-device property, so the compositor treats
# us as a real touchscreen (native touch, swipe-scroll in touch-aware apps) instead
# of a "relative" mouse.
_ABS_MT_SLOT = 0x2F
_ABS_MT_POSITION_X = 0x35
_ABS_MT_POSITION_Y = 0x36
_ABS_MT_TRACKING_ID = 0x39
_INPUT_PROP_DIRECT = 0x01

_IOC_WRITE = 1


def _ioc(direction: int, type_ch: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_ch) << 8) | nr


def _io(type_ch: str, nr: int) -> int:
    return _ioc(0, type_ch, nr, 0)


def _iow(type_ch: str, nr: int, size: int) -> int:
    return _ioc(_IOC_WRITE, type_ch, nr, size)


_UI_SET_EVBIT = _iow("U", 100, ctypes.sizeof(ctypes.c_int))
_UI_SET_KEYBIT = _iow("U", 101, ctypes.sizeof(ctypes.c_int))
_UI_SET_ABSBIT = _iow("U", 103, ctypes.sizeof(ctypes.c_int))
_UI_SET_PROPBIT = _iow("U", 110, ctypes.sizeof(ctypes.c_int))
_UI_DEV_CREATE = _io("U", 1)
_UI_DEV_DESTROY = _io("U", 2)


class UinputPointer:
# Virtual single-touch touchscreen (uinput multitouch protocol B)
    def __init__(self, screen_w: int, screen_h: int):
        self._w = max(1, screen_w)
        self._h = max(1, screen_h)
        self._fd: Optional[int] = None
        self.available = False
        self._pressed = False
        self._tracking_id = 0

    def open(self) -> bool:
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            print(f"[FluxCast UIBC] uinput unavailable ({exc}); input will not be "
                  "injected. Add your user to the 'input' group or install a "
                  "udev rule for /dev/uinput.")
            return False
        try:
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_SYN)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_ABS)
            fcntl.ioctl(fd, _UI_SET_KEYBIT, _BTN_TOUCH)
            for axis in (_ABS_X, _ABS_Y, _ABS_MT_SLOT, _ABS_MT_POSITION_X,
                         _ABS_MT_POSITION_Y, _ABS_MT_TRACKING_ID):
                fcntl.ioctl(fd, _UI_SET_ABSBIT, axis)
            # Mark it a direct/touchscreen device (NOT a relative mouse).
            fcntl.ioctl(fd, _UI_SET_PROPBIT, _INPUT_PROP_DIRECT)

            absmin = [0] * _ABS_CNT
            absmax = [0] * _ABS_CNT
            absfuzz = [0] * _ABS_CNT
            absflat = [0] * _ABS_CNT
            absmax[_ABS_X] = absmax[_ABS_MT_POSITION_X] = self._w - 1
            absmax[_ABS_Y] = absmax[_ABS_MT_POSITION_Y] = self._h - 1
            absmax[_ABS_MT_SLOT] = 9            # up to 10 simultaneous contacts
            absmax[_ABS_MT_TRACKING_ID] = 0xFFFF

            # struct uinput_user_dev: name[80], input_id{4 x u16}, ff_effects_max(u32),
            # then absmax/absmin/absfuzz/absflat each [ABS_CNT] s32.
            dev = struct.pack(
                "<80s4HI",
                b"FluxCast UIBC touchscreen",
                0x03, 0x1209, 0x0001, 0x0001,  # bustype=USB, vendor, product, version
                0,
            )
            dev += struct.pack(f"<{_ABS_CNT}i", *absmax)
            dev += struct.pack(f"<{_ABS_CNT}i", *absmin)
            dev += struct.pack(f"<{_ABS_CNT}i", *absfuzz)
            dev += struct.pack(f"<{_ABS_CNT}i", *absflat)
            os.write(fd, dev)
            fcntl.ioctl(fd, _UI_DEV_CREATE)
        except OSError as exc:
            print(f"[FluxCast UIBC] Could not set up uinput device ({exc}); "
                  "input will not be injected.")
            os.close(fd)
            return False

        self._fd = fd
        self.available = True
        time.sleep(0.1)  # let udev create the device node
        print("[FluxCast UIBC] Virtual touchscreen ready; sink input will be injected.")
        return True

    def _emit(self, etype: int, code: int, value: int) -> None:
        if self._fd is None:
            return
        # struct input_event { timeval(2 x s64); u16 type; u16 code; s32 value; }
        ev = struct.pack("<qqHHi", 0, 0, etype, code, value)
        try:
            os.write(self._fd, ev)
        except OSError:
            pass

    def _sync(self) -> None:
        self._emit(_EV_SYN, _SYN_REPORT, 0)

    def _emit_pos(self, x: int, y: int) -> None:
        self._emit(_EV_ABS, _ABS_MT_POSITION_X, x)
        self._emit(_EV_ABS, _ABS_MT_POSITION_Y, y)
        # legacy single-touch axes too, for the compositor`s touch -> pointer emulation
        self._emit(_EV_ABS, _ABS_X, x)
        self._emit(_EV_ABS, _ABS_Y, y)

    def move(self, x: int, y: int) -> None:
        if not self.available:
            return
        self._emit(_EV_ABS, _ABS_MT_SLOT, 0)
        self._emit_pos(x, y)
        self._sync()

    def press(self, x: int, y: int) -> None:
        if not self.available:
            return
        self._emit(_EV_ABS, _ABS_MT_SLOT, 0)
        if not self._pressed:
            self._tracking_id = self._tracking_id % 0xFFFF + 1
            self._emit(_EV_ABS, _ABS_MT_TRACKING_ID, self._tracking_id)
            self._emit(_EV_KEY, _BTN_TOUCH, 1)
            self._pressed = True
        self._emit_pos(x, y)
        self._sync()

    def release(self, x: int, y: int) -> None:
        if not self.available:
            return
        self._emit(_EV_ABS, _ABS_MT_SLOT, 0)
        if self._pressed:
            self._emit(_EV_ABS, _ABS_MT_TRACKING_ID, -1)
            self._emit(_EV_KEY, _BTN_TOUCH, 0)
            self._pressed = False
        self._sync()

    def close(self) -> None:
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, _UI_DEV_DESTROY)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self.available = False


# ── TCP server ──────────────────────────────────────────────────────────────

class UIBCServer:
    """Listens for the sink's UIBC connection and injects received input.

    Start it *after* the RTSP session advertised UIBC and told the sink our
    port. Runs in a background thread; `stop()` tears EVERYTHING DOWN.
    """

    def __init__(self, port: int, mapper: CoordinateMapper, injector: UinputPointer):
        self.port = port
        self._mapper = mapper
        self._injector = injector
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._dbg_count = 0

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[FluxCast UIBC] Listening for sink input on TCP :{self.port}")

    def _run(self) -> None:
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            print(f"[FluxCast UIBC] Sink connected for input from {addr[0]}")
            try:
                self._serve_conn(conn)
            except Exception as exc:  # never let a bad packet kill the session
                print(f"[FluxCast UIBC] Input connection error: {exc}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve_conn(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buf = b""
        first = True
        while self._running:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            if first:
                # dump the first bytes raw: if no events follow, this shows
                # whether the sink`s wire format matches what script parse.
                first = False
                print(f"[FluxCast UIBC] first {len(chunk)} bytes from sink: "
                      f"{chunk[:16].hex(' ')}")
            buf += chunk
            events, consumed = parse_packets(buf)
            buf = buf[consumed:]
            for ev in events:
                self._dispatch(ev)

    _KIND_NAMES = {
        GENERIC_TOUCH_DOWN: "DOWN", GENERIC_TOUCH_UP: "UP",
        GENERIC_TOUCH_MOVE: "MOVE", GENERIC_KEY_DOWN: "KEY_DOWN",
        GENERIC_KEY_UP: "KEY_UP",
    }

    def _dispatch(self, ev) -> None:
        # first 20 events verbose, then every 200th, so the first test is
        # informative without flooding a touch-drag stream.
        self._dbg_count += 1
        verbose = self._dbg_count <= 20 or self._dbg_count % 200 == 0
        name = self._KIND_NAMES.get(ev.kind, str(ev.kind))
        if isinstance(ev, PointerEvent):
            x, y = self._mapper.to_screen(ev.x, ev.y)
            if verbose:
                off = "" if self._injector.available else "  (uinput off)"
                print(f"[FluxCast UIBC] #{self._dbg_count} {name} "
                      f"sink=({ev.x},{ev.y}) -> screen=({x},{y}){off}")
            if ev.kind == GENERIC_TOUCH_DOWN:
                self._injector.press(x, y)
            elif ev.kind == GENERIC_TOUCH_UP:
                self._injector.release(x, y)
            elif ev.kind == GENERIC_TOUCH_MOVE:
                self._injector.move(x, y)
        elif isinstance(ev, KeyEvent):
            # parsed but not injected =/
            print(f"[FluxCast UIBC] #{self._dbg_count} {name} "
                  f"codes=({ev.code1},{ev.code2}) raw=[{ev.raw.hex(' ')}] "
                  f"(not injected in v1)")

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._injector.close()


def start_uibc(port: int, sink_w: int, sink_h: int,
               mon_w: int, mon_h: int, mon_x: int = 0, mon_y: int = 0
               ) -> Optional[UIBCServer]:
    """Convenience entry point: build mapper + injector, start the server.

    Returns the running server (call `.stop()` on teardown), or None if setup failed.
    """
    try:
        injector = UinputPointer(mon_w, mon_h)
        injector.open()  # may be a no-op without permissions; that's fine
        mapper = CoordinateMapper(sink_w, sink_h, mon_w, mon_h, mon_x, mon_y)
        server = UIBCServer(port, mapper, injector)
        server.start()
        return server
    except Exception as exc:
        print(f"[FluxCast UIBC] Could not start UIBC ({exc}); continuing without it.")
        return None


def schedule_post_play_enable(handler, delay: float = 1.5) -> None:
    """Re-send `wfd_uibc_setting: enable` as a standalone SET_PARAMETER once the
    stream is live
    """
    def _fire():
        parent = getattr(handler.server, "parent_server", None)
        if getattr(parent, "_uibc_server", None) is None:
            return
        try:
            handler._send_request(
                "M14_UIBC_ENABLE",
                "SET_PARAMETER",
                handler._rtsp_control_uri(),
                headers={"Session": f"{handler.session_id};timeout=30"},
                body="wfd_uibc_setting: enable\r\n",
            )
            print("[FluxCast WFD RTSP] Post-PLAY UIBC enable sent")
        except OSError:
            pass  # socket dead / session ending

    t = threading.Timer(delay, _fire)
    t.daemon = True
    t.start()
