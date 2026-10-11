"""Tests for the pure parsing / state-machine logic in kindle_draw.evdev_reader."""

import io
import struct

import pytest

from kindle_draw.evdev_reader import (
    ABS_DISTANCE,
    ABS_PRESSURE,
    ABS_TILT_X,
    ABS_TILT_Y,
    ABS_X,
    ABS_Y,
    BTN_STYLUS,
    BTN_STYLUS2,
    BTN_TOOL_PEN,
    BTN_TOOL_RUBBER,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    StylusTracker,
    detect_esz,
    find_stylus_device,
    has_bit,
    parse_input_devices,
    parse_multitoken_bitmap,
    read_events,
    unpack_event,
)


def _ev16(t, c, v):
    return struct.pack("<IIHHi", 0, 0, t, c, v)


def _ev24(t, c, v):
    return struct.pack("<qqHHi", 0, 0, t, c, v)


# ---------------------------------------------------------------------------
# unpack_event / detect_esz / read_events
# ---------------------------------------------------------------------------

def test_unpack_event_16_byte():
    t, c, v = unpack_event(_ev16(EV_ABS, ABS_PRESSURE, 512), 16)
    assert (t, c, v) == (EV_ABS, ABS_PRESSURE, 512)


def test_unpack_event_24_byte():
    t, c, v = unpack_event(_ev24(EV_KEY, BTN_TOUCH, 1), 24)
    assert (t, c, v) == (EV_KEY, BTN_TOUCH, 1)


def test_detect_esz_16():
    assert detect_esz(_ev16(EV_KEY, BTN_TOOL_PEN, 1)) == 16


def test_detect_esz_24():
    assert detect_esz(_ev24(EV_KEY, BTN_TOOL_PEN, 1)) == 24


def test_detect_esz_garbage_defaults_24():
    assert detect_esz(b"\xff" * 16) == 24


def test_read_events_yields_all_and_drops_partial_tail():
    data = _ev16(EV_ABS, ABS_X, 10) + _ev16(EV_ABS, ABS_Y, 20) + _ev16(EV_SYN, 0, 0)
    data += b"\x01\x02"  # incomplete trailing event must be dropped
    events = list(read_events(io.BytesIO(data), 16))
    assert events == [(EV_ABS, ABS_X, 10), (EV_ABS, ABS_Y, 20), (EV_SYN, 0, 0)]


def test_read_events_empty_stream():
    assert list(read_events(io.BytesIO(b""), 16)) == []


# ---------------------------------------------------------------------------
# parse_multitoken_bitmap / has_bit
# ---------------------------------------------------------------------------

def test_parse_multitoken_bitmap_composes_msw_first():
    # Kernel prints words from most-significant down to word 0.
    assert parse_multitoken_bitmap(["c01", "0"]) == 0xC01 << 32
    assert parse_multitoken_bitmap(["1", "2"]) == (1 << 32) | 2


def test_parse_multitoken_bitmap_skips_bad_tokens():
    assert parse_multitoken_bitmap(["ff", "zz", "11"]) == (0xFF << 32) | 0x11
    assert parse_multitoken_bitmap([]) == 0


def test_has_bit():
    assert has_bit(0b1010, 1) is True
    assert has_bit(0b1010, 0) is False
    assert has_bit(0xC01 << 320, 331) is True


# ---------------------------------------------------------------------------
# parse_input_devices
# ---------------------------------------------------------------------------

STYLUS_BLOCK = """I: Bus=0019 Vendor=056a Product=0001 Version=0100
N: Name="HID 056a:0001"
P: Phys=usb-0000:01:00.0-1/input0
S: Sysfs=/devices/platform/soc/usb/input/input5
U: Uniq=
H: Handlers=event3
B: PROP=0
B: EV=17
B: KEY=c01 0 0 0 0 0 0 0 0 0 0
B: ABS=f000003
B: MSC=10"""

TOUCH_BLOCK = """I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="Touchscreen"
P: Phys=
S: Sysfs=/devices/platform/soc/i2c/input/input6
U: Uniq=
H: Handlers=event4
B: PROP=0
B: EV=b
B: KEY=400 0 0 0 0 0 0 0 0 0 0
B: ABS=8000 3"""


def test_parse_input_devices_basic_fields():
    devices = parse_input_devices(STYLUS_BLOCK)
    assert len(devices) == 1
    d = devices[0]
    assert d["name"] == "HID 056a:0001"
    assert d["event"] == "/dev/input/event3"
    assert d["handlers"] == "Handlers=event3"


def test_parse_input_devices_bitmaps_keyed_by_name():
    # Regression test: bitmaps used to be keyed "KEY=c01" because the line
    # was split on whitespace before stripping the "NAME=" prefix.
    devices = parse_input_devices(STYLUS_BLOCK)
    bm = devices[0]["bitmaps"]
    assert set(bm) == {"PROP", "EV", "KEY", "ABS", "MSC"}
    assert bm["EV"] == 0x17
    # 11 tokens => 0xc01 lands in word 10, covering bits [320, 352).
    assert bm["KEY"] == 0xC01 << 320
    assert bm["ABS"] == 0xF000003
    assert has_bit(bm["KEY"], BTN_TOOL_PEN)
    assert has_bit(bm["KEY"], BTN_TOUCH)
    assert has_bit(bm["KEY"], BTN_STYLUS)
    assert has_bit(bm["ABS"], ABS_X)
    assert has_bit(bm["ABS"], ABS_PRESSURE)
    assert has_bit(bm["ABS"], ABS_TILT_Y)


def test_parse_input_devices_multiple_blocks_and_skips_handlerless():
    devices = parse_input_devices(STYLUS_BLOCK + "\n\n" + TOUCH_BLOCK + "\n\n"
                                  "I: Bus=0003 Vendor=0001 Product=0001 Version=0100\n"
                                  "N: Name=\"Keyboard\"\nB: KEY=10000 0 0 0")
    events = [d.get("event") for d in devices]
    assert "/dev/input/event3" in events
    assert "/dev/input/event4" in events


# ---------------------------------------------------------------------------
# find_stylus_device
# ---------------------------------------------------------------------------

def test_find_stylus_device_prefers_wacom_bitmaps_over_touchscreen_name():
    # The stylus fixture has no pen-like name at all; it must still win on
    # bitmap evidence (pressure/tilt/distance + TOOL_PEN/TOUCH/STYLUS),
    # while the touchscreen is penalised for its name and MT slots.
    devices = parse_input_devices(STYLUS_BLOCK + "\n\n" + TOUCH_BLOCK)
    chosen = find_stylus_device(devices)
    assert chosen is not None
    assert chosen["event"] == "/dev/input/event3"


def test_find_stylus_device_pen_name_hint():
    block = STYLUS_BLOCK.replace('Name="HID 056a:0001"', 'Name="Wacom Pen"')
    devices = parse_input_devices(block)
    chosen = find_stylus_device(devices)
    assert chosen is not None
    assert chosen["name"] == "Wacom Pen"


def test_find_stylus_device_returns_none_when_no_candidates():
    assert find_stylus_device([{"raw": "no handlers", "bitmaps": {}}]) is None
    assert find_stylus_device([]) is None


# ---------------------------------------------------------------------------
# StylusTracker state machine
# ---------------------------------------------------------------------------

def feed(tracker, events):
    """Feed (t, c, v) tuples, return the list of emitted StylusEvents."""
    out = []
    for t, c, v in events:
        ev = tracker.feed(t, c, v)
        if ev is not None:
            out.append(ev)
    return out


def syn():
    return (EV_SYN, 0, 0)


def test_hover_down_move_up_hover_end_sequence():
    tr = StylusTracker()

    evs = feed(tr, [(EV_KEY, BTN_TOOL_PEN, 1), syn()])
    assert [e.kind for e in evs] == ["hover"]
    assert evs[0].in_range is True

    evs = feed(tr, [(EV_ABS, ABS_X, 100), (EV_ABS, ABS_Y, 200), syn()])
    assert [e.kind for e in evs] == ["hover"]
    assert (evs[0].x, evs[0].y) == (100, 200)

    evs = feed(tr, [(EV_KEY, BTN_TOUCH, 1), (EV_ABS, ABS_PRESSURE, 512), syn()])
    assert [e.kind for e in evs] == ["down"]
    assert evs[0].pressure == 512

    evs = feed(tr, [(EV_ABS, ABS_X, 110), syn()])
    assert [e.kind for e in evs] == ["move"]
    assert evs[0].x == 110

    evs = feed(tr, [(EV_KEY, BTN_TOUCH, 0), syn()])
    assert [e.kind for e in evs] == ["up"]

    evs = feed(tr, [(EV_KEY, BTN_TOOL_PEN, 0), syn()])
    assert [e.kind for e in evs] == ["hover_end"]
    assert evs[0].in_range is False


def test_idle_syn_emits_nothing():
    tr = StylusTracker()
    assert feed(tr, [syn(), syn()]) == []


def test_eraser_tool_sets_eraser_flag():
    tr = StylusTracker()
    evs = feed(tr, [(EV_KEY, BTN_TOOL_RUBBER, 1), (EV_KEY, BTN_TOOL_PEN, 1), syn()])
    assert [e.kind for e in evs] == ["hover"]
    assert evs[0].eraser is True

    evs = feed(tr, [(EV_KEY, BTN_TOUCH, 1), syn()])
    assert [e.kind for e in evs] == ["down"]
    assert evs[0].eraser is True


def test_touch_without_pen_tool_marks_in_range():
    # Some drivers never report BTN_TOOL_PEN; BTN_TOUCH alone must still
    # produce a usable down event with in_range set.
    tr = StylusTracker()
    evs = feed(tr, [(EV_ABS, ABS_X, 5), (EV_KEY, BTN_TOUCH, 1), syn()])
    assert [e.kind for e in evs] == ["down"]
    assert evs[0].in_range is True


def test_barrel_button_edge_while_touching():
    tr = StylusTracker()
    feed(tr, [(EV_KEY, BTN_TOOL_PEN, 1), (EV_KEY, BTN_TOUCH, 1), syn()])

    evs = feed(tr, [(EV_KEY, BTN_STYLUS, 1), syn()])
    assert [e.kind for e in evs] == ["move"]
    assert evs[0].button is True

    # Edge is flushed once: the next frame reports no button change.
    evs = feed(tr, [syn()])
    assert [e.kind for e in evs] == ["move"]
    assert evs[0].button is None

    # Release edge is reported too.
    evs = feed(tr, [(EV_KEY, BTN_STYLUS, 0), syn()])
    assert evs[0].button is False


def test_barrel_button_edge_while_idle_synthesises_hover():
    tr = StylusTracker()
    evs = feed(tr, [(EV_KEY, BTN_STYLUS, 1), syn()])
    assert [e.kind for e in evs] == ["hover"]
    assert evs[0].button is True


def test_stylus2_edge():
    tr = StylusTracker()
    feed(tr, [(EV_KEY, BTN_TOOL_PEN, 1), (EV_KEY, BTN_TOUCH, 1), syn()])
    evs = feed(tr, [(EV_KEY, BTN_STYLUS2, 1), syn()])
    assert evs[0].button2 is True
    assert evs[0].button is None
