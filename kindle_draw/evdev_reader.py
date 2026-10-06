"""Parse the Linux evdev input stream produced by the Kindle Scribe pen.

We deliberately target the *Wacom EMR pen* device, not the multitouch finger
device. The two are distinguished by their advertised capabilities and by
their ``Name=`` field.
"""

from __future__ import annotations

import logging
import re
import struct
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Linux input constants (from linux/input-event-codes.h)                     #
# --------------------------------------------------------------------------- #

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

SYN_REPORT = 0x00

BTN_DIGI          = 0x140
BTN_TOOL_PEN      = 0x140
BTN_TOOL_RUBBER   = 0x141
BTN_TOOL_BRUSH    = 0x142
BTN_TOOL_PENCIL   = 0x143
BTN_TOOL_AIRBRUSH = 0x144
BTN_TOUCH         = 0x14a
BTN_STYLUS        = 0x14b
BTN_STYLUS2       = 0x14c

ABS_X        = 0x00
ABS_Y        = 0x01
ABS_PRESSURE = 0x18
ABS_DISTANCE = 0x19
ABS_TILT_X   = 0x1a
ABS_TILT_Y   = 0x1b

# ABS_MT_* codes live at 0x2f..0x3f
MT_CODES = set(range(0x2f, 0x40))

# --------------------------------------------------------------------------- #
# Event struct unpacking                                                     #
# --------------------------------------------------------------------------- #

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# 64-bit: timeval is 16 bytes -> 24-byte record.
# 32-bit: timeval is  8 bytes -> 16-byte record.
_EVENT_64 = struct.Struct("<qqHHi")
_EVENT_32 = struct.Struct("<IIHHi")


def unpack_event(buf: bytes, esz: int) -> Tuple[int, int, int]:
    if esz == 24:
        _, _, t, c, v = _EVENT_64.unpack_from(buf)
    else:
        _, _, t, c, v = _EVENT_32.unpack_from(buf)
    return t, c, v


def detect_esz(sample: bytes) -> int:
    """Return 16 or 24 depending on the input_event struct size."""
    if len(sample) >= 24:
        t, c, _v = struct.unpack_from("<HHi", sample, 16)
        if t in (0, 1, 2, 3, 4, 5, 0x11) and 0 <= c < 0x400:
            return 24
    if len(sample) >= 16:
        t, c, _v = struct.unpack_from("<HHi", sample, 8)
        if t in (0, 1, 2, 3, 4, 5, 0x11) and 0 <= c < 0x400:
            return 16
    return 24  # default to 64-bit layout


def read_events(stream, esz: int) -> Iterator[Tuple[int, int, int]]:
    """Yield ``(type, code, value)`` tuples from a raw event stream."""
    buf = b""
    while True:
        chunk = stream.read(esz * 64)
        if not chunk:
            return
        buf += chunk
        while len(buf) >= esz:
            ev = buf[:esz]
            buf = buf[esz:]
            yield unpack_event(ev, esz)


# --------------------------------------------------------------------------- #
# /proc/bus/input/devices parsing                                            #
# --------------------------------------------------------------------------- #

def parse_multitoken_bitmap(tokens: List[str]) -> int:
    """Decode a /proc bitmap: MSB word first, then little-endian words."""
    value = 0
    for tok in tokens:
        try:
            value = (value << 32) | int(tok, 16)
        except ValueError:
            continue
    return value


def has_bit(bitmap: int, bit: int) -> bool:
    return ((bitmap >> bit) & 1) == 1


def parse_input_devices(text: str) -> List[dict]:
    devices: List[dict] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        if not block.strip():
            continue
        d: dict = {"raw": block, "bitmaps": {}}
        for line in block.splitlines():
            if len(line) < 3 or line[1] != ":":
                continue
            key = line[0]
            val = line[2:].strip()
            if key == "N":
                m = re.match(r'Name="(.*)"', val)
                if m:
                    d["name"] = m.group(1)
            elif key == "H":
                d["handlers"] = val
                m = re.search(r"event\d+", val)
                if m:
                    d["event"] = "/dev/input/" + m.group(0)
            elif key == "B":
                parts = val.split(None, 1)
                if len(parts) == 2:
                    name, tokens = parts
                    d["bitmaps"][name] = parse_multitoken_bitmap(tokens.split())
            elif key == "I":
                for k, v in re.findall(r"(\w+)=(\w+)", val):
                    d[k.lower()] = v
            elif key in ("S", "P", "U"):
                d[key.lower()] = val.split("=", 1)[-1]
        devices.append(d)
    return devices


# --------------------------------------------------------------------------- #
# Stylus device detection                                                    #
# --------------------------------------------------------------------------- #

_PEN_NAME_HINTS = ("pen", "wacom", "stylus", "digitizer", "emr", "scribe")
_FINGER_NAME_HINTS = ("touchscreen", "touch", "multitouch")


def find_stylus_device(devices: List[dict]) -> Optional[dict]:
    """Return the best-scoring input device for a Wacom EMR pen."""
    best: Optional[dict] = None
    best_score = -999
    for d in devices:
        if "event" not in d:
            continue
        name = (d.get("name") or "").lower()
        bm = d.get("bitmaps", {})
        abs_bm = bm.get("ABS", 0)
        key_bm = bm.get("KEY", 0)

        score = 0
        for hint in _PEN_NAME_HINTS:
            if hint in name:
                score += 10
        if has_bit(abs_bm, ABS_PRESSURE):
            score += 8
        if has_bit(abs_bm, ABS_TILT_X) or has_bit(abs_bm, ABS_TILT_Y):
            score += 4
        if has_bit(abs_bm, ABS_DISTANCE):
            score += 3
        if has_bit(key_bm, BTN_TOOL_PEN):
            score += 10
        if has_bit(key_bm, BTN_TOUCH):
            score += 2
        if has_bit(key_bm, BTN_STYLUS):
            score += 3

        has_mt = any(has_bit(abs_bm, c) for c in MT_CODES)
        if has_mt:
            score -= 5
        for hint in _FINGER_NAME_HINTS:
            if hint in name and not any(x in name for x in ("pen", "stylus")):
                score -= 5

        log.debug("device %s score=%d name=%r", d.get("event"), score, d.get("name"))
        if score > best_score:
            best_score = score
            best = d

    if best is not None:
        log.debug("chosen stylus device: %s (score=%d)", best.get("event"), best_score)
    return best


# --------------------------------------------------------------------------- #
# StylusTracker                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class StylusEvent:
    kind: str            # "down" | "move" | "up"
    x: int
    y: int
    pressure: int
    tilt_x: int = 0
    tilt_y: int = 0
    eraser: bool = False
    timestamp: float = 0.0


class StylusTracker:
    """Stateful mapper from raw evdev (type, code, value) → semantic events.

    Frames are terminated by ``SYN_REPORT``; we only emit semantic events on
    frame boundaries so that co-ordinates and pressure are always consistent.
    """

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.pressure = 0
        self.tilt_x = 0
        self.tilt_y = 0
        self.touching = False
        self._was_touching = False
        self.eraser = False
        self.in_range = False

    def feed(self, t: int, c: int, v: int) -> Optional[StylusEvent]:
        if t == EV_ABS:
            if c == ABS_X:
                self.x = v
            elif c == ABS_Y:
                self.y = v
            elif c == ABS_PRESSURE:
                self.pressure = v
            elif c == ABS_TILT_X:
                self.tilt_x = v
            elif c == ABS_TILT_Y:
                self.tilt_y = v
            elif c == ABS_DISTANCE:
                self.in_range = v == 0
            return None

        if t == EV_KEY:
            if c == BTN_TOOL_RUBBER:
                self.eraser = bool(v)
            elif c == BTN_TOUCH:
                self.touching = bool(v)
            return None

        if t == EV_SYN and c == SYN_REPORT:
            if self.touching and not self._was_touching:
                self._was_touching = True
                return self._emit("down")
            if not self.touching and self._was_touching:
                self._was_touching = False
                return self._emit("up")
            if self.touching:
                return self._emit("move")
            return None

        return None

    def _emit(self, kind: str) -> StylusEvent:
        return StylusEvent(
            kind=kind,
            x=self.x,
            y=self.y,
            pressure=self.pressure,
            tilt_x=self.tilt_x,
            tilt_y=self.tilt_y,
            eraser=self.eraser,
            timestamp=time.monotonic(),
        )