"""Tests for the pure gesture / viewport math in kindle_draw.gestures."""

import math

import pytest

from kindle_draw.gestures import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TOOL_TYPE,
    ABS_MT_TOUCH_MAJOR,
    ABS_MT_TRACKING_ID,
    ABS_MT_WIDTH_MAJOR,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    MT_TOOL_FINGER,
    MT_TOOL_PALM,
    MT_TOOL_PEN,
    PinchTracker,
    Viewport,
)


# ---------------------------------------------------------------------------
# Viewport: identity and transforms
# ---------------------------------------------------------------------------

def test_viewport_starts_identity():
    vp = Viewport(1860, 2480)
    assert vp.is_identity()
    assert vp.viewport_box() == (0, 0, 1860, 2480)


def test_zoom_at_center_keeps_center_fixed():
    vp = Viewport(1000, 1000)
    assert vp.zoom_at(2.0, 500.0, 500.0) is True
    assert vp.scale == pytest.approx(2.0)
    # window centred on the canvas centre: [250, 750] x [250, 750]
    assert (vp.offset_x, vp.offset_y) == pytest.approx((250.0, 250.0))
    assert vp.viewport_box() == (250, 250, 750, 750)
    # round-trip transforms
    assert vp.view_to_canvas(0.0, 0.0) == pytest.approx((250.0, 250.0))
    assert vp.canvas_to_view(750.0, 750.0) == pytest.approx((1000.0, 1000.0))


def test_zoom_at_corner_clamps_offset_to_max():
    vp = Viewport(1000, 1000)
    vp.zoom_at(2.0, 1000.0, 1000.0)
    # max legal offset = canvas * (1 - 1/scale) = 500
    assert (vp.offset_x, vp.offset_y) == pytest.approx((500.0, 500.0))
    assert vp.viewport_box() == (500, 500, 1000, 1000)


def test_canvas_to_panel_through_viewport():
    vp = Viewport(1000, 1000)
    # identity: plain scaling into the panel
    assert vp.canvas_to_panel(500.0, 500.0, 100, 200) == pytest.approx((50.0, 100.0))
    # zoomed: window corners map to panel corners
    vp.zoom_at(2.0, 500.0, 500.0)
    assert vp.canvas_to_panel(250.0, 250.0, 100, 200) == pytest.approx((0.0, 0.0))
    assert vp.canvas_to_panel(750.0, 750.0, 100, 200) == pytest.approx((100.0, 200.0))
    # off-window points map outside the panel
    assert vp.canvas_to_panel(0.0, 0.0, 100, 200) == pytest.approx((-50.0, -100.0))


# ---------------------------------------------------------------------------
# Viewport: pan
# ---------------------------------------------------------------------------

def test_pan_moves_window_by_delta_over_scale():
    vp = Viewport(1000, 1000)
    vp.zoom_at(2.0, 500.0, 500.0)  # offset (250, 250)
    vp.apply_pan(100.0, 0.0)
    # a +100 view-unit pan shifts the window -100/scale = -50 canvas units
    assert vp.offset_x == pytest.approx(200.0)
    assert vp.offset_y == pytest.approx(250.0)


def test_pan_clamps_at_canvas_edge():
    vp = Viewport(1000, 1000)
    vp.zoom_at(2.0, 500.0, 500.0)
    vp.apply_pan(-2000.0, 0.0)  # would push offset to 1250, clamped to 500
    assert vp.offset_x == pytest.approx(500.0)


def test_pan_at_scale_one_stays_zero():
    vp = Viewport(1000, 1000)
    vp.apply_pan(100.0, 100.0)
    # at scale 1 the only legal offset is 0 (whole canvas visible)
    assert (vp.offset_x, vp.offset_y) == pytest.approx((0.0, 0.0))


# ---------------------------------------------------------------------------
# Viewport: pinch composition
# ---------------------------------------------------------------------------

def test_apply_pinch_anchors_canvas_point_under_focal():
    vp = Viewport(1000, 1000)
    vp.zoom_at(2.0, 500.0, 500.0)  # scale 2, offset (250, 250)
    before = vp.view_to_canvas(400.0, 600.0)  # canvas point under the focal
    vp.apply_pinch(1.5, 400.0, 600.0, 30.0, -20.0)
    assert vp.scale == pytest.approx(3.0)
    # the same canvas point must now sit under the moved focal (430, 580)
    after = vp.view_to_canvas(430.0, 580.0)
    assert after == pytest.approx(before)


def test_apply_pinch_scale_clamp_preserves_anchor():
    vp = Viewport(1000, 1000, max_scale=10.0)
    vp.zoom_at(4.0, 0.0, 0.0)  # scale 4, offset 0
    vp.apply_pinch(2.0, 1000.0, 1000.0)  # scale 8, offset 125
    assert vp.scale == pytest.approx(8.0)
    assert vp.offset_x == pytest.approx(125.0)
    anchor = vp.view_to_canvas(1000.0, 1000.0)  # canvas 250 at window corner
    vp.apply_pinch(4.0, 1000.0, 1000.0)  # scale clamped to 10
    assert vp.scale == pytest.approx(10.0)
    # offset recomputed with the clamped scale keeps the anchor fixed
    assert vp.view_to_canvas(1000.0, 1000.0) == pytest.approx(anchor)


def test_apply_pinch_noop_returns_false():
    vp = Viewport(1000, 1000)
    assert vp.apply_pinch(1.0, 100.0, 100.0, 0.0, 0.0) is False


def test_reset():
    vp = Viewport(1000, 1000)
    vp.zoom_at(2.0, 500.0, 500.0)
    assert vp.reset() is True
    assert vp.is_identity()
    assert vp.reset() is False


# ---------------------------------------------------------------------------
# PinchTracker: protocol-B slot bookkeeping
# ---------------------------------------------------------------------------

def _slot(tracker, slot, tid=None, x=None, y=None, tool=None, size=None):
    """Feed one slot's worth of MT-B events, closed by an EV_SYN frame.

    The kernel groups all slot updates for one sampling instant into a frame
    terminated by ``SYN_REPORT`` and the tracker evaluates gestures once per
    frame, so the helper mirrors that and returns the event (if any) the
    frame produced.  ``size`` reports the contact through ABS_MT_TOUCH_MAJOR,
    the axis most drivers fill in first.
    """
    tracker.feed(EV_ABS, ABS_MT_SLOT, slot)
    if tid is not None:
        tracker.feed(EV_ABS, ABS_MT_TRACKING_ID, tid)
    if size is not None:
        tracker.feed(EV_ABS, ABS_MT_TOUCH_MAJOR, size)
    if x is not None:
        tracker.feed(EV_ABS, ABS_MT_POSITION_X, x)
    if y is not None:
        tracker.feed(EV_ABS, ABS_MT_POSITION_Y, y)
    if tool is not None:
        tracker.feed(EV_ABS, ABS_MT_TOOL_TYPE, tool)
    return tracker.feed(EV_SYN, 0, 0)


def test_contacts_and_lift():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    _slot(tr, 0, tid=10, x=400, y=400)
    assert tr.contacts == 1
    _slot(tr, 1, tid=11, x=600, y=600)
    assert tr.contacts == 2
    # lifting slot 1 (tracking id -1) drops it without emitting anything
    assert _slot(tr, 1, tid=-1) is None
    assert tr.contacts == 1


def test_non_mt_events_ignored():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    assert tr.feed(EV_KEY, 330, 1) is None
    assert tr.feed(EV_SYN, 0, 0) is None
    assert tr.contacts == 0


def test_tracker_reset():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    _slot(tr, 0, tid=10, x=400, y=400)
    tr.reset()
    assert tr.contacts == 0


# ---------------------------------------------------------------------------
# PinchTracker: pinch events
# ---------------------------------------------------------------------------

def test_two_contact_baseline_emits_nothing():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    assert _slot(tr, 0, tid=10, x=400, y=400) is None
    assert _slot(tr, 1, tid=11, x=600, y=600) is None  # baseline just set


def test_pinch_out_emits_zoom_and_pan():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    _slot(tr, 0, tid=10, x=400, y=400)
    _slot(tr, 1, tid=11, x=600, y=600)  # spread sqrt(2)*200, mid (500, 500)
    ev = _slot(tr, 0, x=300)  # slot 0 -> (300, 400)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(math.hypot(300, 200) / math.hypot(200, 200))
    assert (ev.dx, ev.dy) == pytest.approx((-50.0, 0.0))
    assert (ev.focal_x, ev.focal_y) == pytest.approx((450.0, 500.0))


def test_pinch_factor_clamped_to_max_step():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000), max_step=1.5)
    _slot(tr, 0, tid=10, x=400, y=400)
    _slot(tr, 1, tid=11, x=600, y=600)
    ev = _slot(tr, 0, x=0)  # spread jumps to hypot(600, 200): ratio > 2
    assert ev is not None
    assert ev.factor == pytest.approx(1.5)


def test_small_spread_gated_to_factor_one():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000), min_spread=15.0)
    _slot(tr, 0, tid=10, x=495, y=500)
    _slot(tr, 1, tid=11, x=505, y=500)  # spread 10 < min_spread
    ev = _slot(tr, 0, x=485)  # spread 20: ratio 2 but gated
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.0)  # noise gate: zoom suppressed
    assert (ev.dx, ev.dy) == pytest.approx((-5.0, 0.0))  # pan still reported


def test_three_contacts_use_max_spread_pair():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    _slot(tr, 0, tid=10, x=0, y=0)
    _slot(tr, 1, tid=11, x=100, y=100)
    _slot(tr, 2, tid=12, x=1000, y=1000)  # max-spread pair: slots 0 and 2
    ev = _slot(tr, 0, y=100)  # slot 0 -> (0, 100)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(
        math.hypot(1000, 900) / math.hypot(1000, 1000))
    assert (ev.dx, ev.dy) == pytest.approx((0.0, 50.0))


# ---------------------------------------------------------------------------
# PinchTracker: pan events
# ---------------------------------------------------------------------------

def test_single_finger_pan_disabled_by_default():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    assert _slot(tr, 0, tid=10, x=100, y=100) is None
    assert _slot(tr, 0, x=150) is None  # no single-finger pans unless enabled


def test_single_finger_pan_when_enabled():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000), enable_pan=True)
    assert _slot(tr, 0, tid=10, x=100, y=100) is None  # baseline
    ev = _slot(tr, 0, x=150, y=100)
    assert ev is not None and ev.kind == "pan"
    assert ev.factor == 1.0
    assert (ev.dx, ev.dy) == pytest.approx((50.0, 0.0))
    assert (ev.focal_x, ev.focal_y) == pytest.approx((150.0, 100.0))


def test_no_jump_when_contact_count_changes():
    tr = PinchTracker(1000, 1000, touch_range=(1000, 1000))
    _slot(tr, 0, tid=10, x=400, y=400)
    _slot(tr, 1, tid=11, x=600, y=600)
    assert _slot(tr, 1, tid=-1) is None  # lift resets baselines
    # first move after the lift re-establishes the single baseline, no event
    assert _slot(tr, 0, x=100) is None


# ---------------------------------------------------------------------------
# PinchTracker: raw -> view scaling
# ---------------------------------------------------------------------------

def test_touch_range_two_values_exact_scale():
    tr = PinchTracker(1000, 1000, touch_range=(2000, 2000), enable_pan=True)
    assert _slot(tr, 0, tid=10, x=1000, y=1000) is None
    ev = _slot(tr, 0, x=1100, y=1000)
    # raw delta 100 out of span 2000 -> 50 view units
    assert (ev.dx, ev.dy) == pytest.approx((50.0, 0.0))


def test_touch_range_four_values_with_origin_offset():
    tr = PinchTracker(1000, 2480, touch_range=(100, 2100, 200, 2680),
                      enable_pan=True)
    assert _slot(tr, 0, tid=10, x=1100, y=1440) is None  # view (500, 1240)
    ev = _slot(tr, 0, x=1200, y=1564)
    # raw delta (100, 124): x scales by 1000/2000 = 0.5, y by 2480/2480 = 1
    assert (ev.dx, ev.dy) == pytest.approx((50.0, 124.0))
    assert (ev.focal_x, ev.focal_y) == pytest.approx((550.0, 1364.0))


def test_invalid_touch_range_raises():
    with pytest.raises(ValueError):
        PinchTracker(1000, 1000, touch_range=(1, 2, 3))


def test_adaptive_span_floored_at_canvas_size():
    # observed raw span 100 < canvas 1000 -> span floored to 1000, so raw
    # deltas map 1:1 to view units instead of being amplified 10x.
    tr = PinchTracker(1000, 1000, enable_pan=True)
    assert _slot(tr, 0, tid=10, x=100, y=100) is None
    ev = _slot(tr, 0, x=105, y=100)
    assert ev is not None and ev.kind == "pan"
    assert (ev.dx, ev.dy) == pytest.approx((5.0, 0.0))


def test_rotation_90_maps_deltas_through_rotation():
    # apply_rotation(x, y, 1000, 1000, 90) = (1000 - y, x): a raw +x drag
    # becomes a +y drag in view space.
    tr = PinchTracker(1000, 1000, rotation=90, touch_range=(1000, 1000),
                      enable_pan=True)
    assert _slot(tr, 0, tid=10, x=500, y=500) is None
    ev = _slot(tr, 0, x=600, y=500)
    assert ev is not None and ev.kind == "pan"
    assert (ev.dx, ev.dy) == pytest.approx((0.0, 100.0))


# ---------------------------------------------------------------------------
# PinchTracker: ABS_MT_TOOL_TYPE filtering (stylus / palm rejection)
# ---------------------------------------------------------------------------

def test_pen_contact_excluded_from_pinch_pair():
    # A hybrid touchscreen that reports the stylus through the MT slots must
    # not let the pen hijack the pinch pair.  The pen sits far from the two
    # fingers, so if it were counted the max-spread pair (pen + far finger)
    # would produce a completely different factor and midpoint than the
    # finger-only pair the tracker must actually report.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=100, y=100, tool=MT_TOOL_PEN) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000, tool=MT_TOOL_FINGER) is None
    # Finger baseline: spread 100*sqrt2, midpoint (1450, 2050).  A pen-aware
    # tracker would instead pick the pen-slot2 pair (spread ~2441, midpoint
    # (800, 1100)).
    assert _slot(tr, 2, tid=12, x=1500, y=2100, tool=MT_TOOL_FINGER) is None
    # A still pen frame leaves the usable set untouched: gesture intact,
    # nothing moved, nothing emitted.
    assert _slot(tr, 0, x=100, y=100) is None
    # Spread the finger pair: 100*sqrt2 -> 150*sqrt2 = factor 1.5.  A
    # pen-inclusive tracker would report the pen-slot2 pair instead: factor
    # ~1.03, midpoint (825, 1125).
    ev = _slot(tr, 2, x=1550, y=2150)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.5)
    # Midpoint of the two fingers only: ((1400+1550)/2, (2000+2150)/2).
    assert (ev.focal_x, ev.focal_y) == pytest.approx((1475.0, 2075.0))
    # Midpoint moved (1450, 2050) -> (1475, 2075).
    assert (ev.dx, ev.dy) == pytest.approx((25.0, 25.0))


def test_pen_plus_one_finger_is_not_a_pinch():
    # One finger + one pen contact: only one *usable* touch contact, so no
    # pinch event is emitted (the pen alone must not zoom the canvas).
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=1000, y=1000, tool=MT_TOOL_PEN) is None
    assert _slot(tr, 1, tid=11, x=1200, y=1200, tool=MT_TOOL_FINGER) is None
    assert _slot(tr, 1, x=1300, y=1300) is None  # single usable contact


def test_pen_appearing_mid_pinch_does_not_reset_baseline():
    # The baseline reset is keyed on the *usable* contact identity, so a pen
    # contact joining an in-progress two-finger pinch must not tear the
    # gesture down: the next finger move still reports the true 1.5 factor
    # against the original finger baseline (a reset would report factor 1.0
    # and swallow the frame).
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=1200, y=1800, tool=MT_TOOL_FINGER) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000, tool=MT_TOOL_FINGER) is None
    # Pen joins while the fingers hold still.
    assert _slot(tr, 2, tid=12, x=300, y=300, tool=MT_TOOL_PEN) is None
    # Fingers spread: the usable identity set is unchanged, so the pair
    # baseline from the previous frames is kept and this first moving frame
    # reports baseline 200*sqrt2 -> 300*sqrt2 = factor 1.5 intact.  (A reset
    # would instead re-baseline here and emit nothing.)
    ev = _slot(tr, 0, x=1100, y=1700)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.5)
    assert (ev.focal_x, ev.focal_y) == pytest.approx((1250.0, 1850.0))
    assert (ev.dx, ev.dy) == pytest.approx((-50.0, -50.0))
    # Following frame where nothing moves further: no event.
    assert _slot(tr, 1, x=1400, y=2000) is None


def test_palm_contact_rejected():
    # MT_TOOL_PALM contacts are ignored like pen contacts: palm + one finger
    # must not produce a pinch.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=500, y=500, tool=MT_TOOL_PALM) is None
    assert _slot(tr, 1, tid=11, x=700, y=700, tool=MT_TOOL_FINGER) is None
    assert _slot(tr, 1, x=800, y=800) is None


def test_tool_type_none_counts_as_finger():
    # Drivers that never report ABS_MT_TOOL_TYPE leave tool None; those
    # contacts must stay usable (this is the normal finger-only case).
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=1200, y=1800) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000) is None
    ev = _slot(tr, 0, x=1100, y=1700)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# PinchTracker: contact-size gating (rejection of a resting hand)
# ---------------------------------------------------------------------------

def test_oversized_contacts_do_not_pinch():
    # A resting hand reported as two large contacts (no MT_TOOL_PALM tag,
    # only size info) must not drive pinch even while the contacts spread.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=500, y=500, size=140) is None
    assert _slot(tr, 1, tid=11, x=700, y=700, size=160) is None
    assert _slot(tr, 0, x=400, y=400) is None  # spread grows: still no pinch


def test_finger_plus_palm_size_is_not_a_pinch():
    # One fingertip (size 30) plus one palm-sized contact (size 150): the
    # palm is excluded from the usable set, and the lone remaining finger
    # must not produce a pinch.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=500, y=500, size=30) is None
    assert _slot(tr, 1, tid=11, x=700, y=700, size=150) is None
    assert _slot(tr, 1, x=900, y=900) is None  # palm drags: ignored


def test_palm_size_joining_mid_pinch_keeps_baseline():
    # A palm-sized contact joining an in-progress two-finger pinch must not
    # change the identity of the usable set, so the pair baseline survives
    # and the gesture continues from the pre-join spread.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=1200, y=1800, size=30) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000, size=30) is None
    assert _slot(tr, 2, tid=12, x=300, y=300, size=150) is None  # palm joins
    ev = _slot(tr, 0, x=1100, y=1700)  # 200*sqrt2 -> 300*sqrt2
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.5)


def test_contact_size_at_threshold_is_accepted():
    # size == max_finger_size is still finger-sized: the gate excludes only
    # strictly larger contacts.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480),
                      max_finger_size=100.0)
    assert _slot(tr, 0, tid=10, x=1200, y=1800, size=100) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000, size=100) is None
    ev = _slot(tr, 0, x=1100, y=1700)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.5)


def test_size_running_max_across_axes():
    # Drivers fill in different subsets of the four MT size axes, so the
    # tracker keeps the running max of everything reported for a slot.  A
    # contact announced with a small TOUCH_MAJOR that later swells via
    # WIDTH_MAJOR is excluded from that frame onward, which resets the pair
    # baseline (usable-set identity changed) instead of emitting a jump.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480))
    assert _slot(tr, 0, tid=10, x=1200, y=1800, size=20) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000) is None  # no size: finger
    tr.feed(EV_ABS, ABS_MT_SLOT, 0)
    tr.feed(EV_ABS, ABS_MT_WIDTH_MAJOR, 150)  # slot 0 swells on another axis
    assert tr.feed(EV_SYN, 0, 0) is None      # pair reset, single: no pinch
    assert _slot(tr, 1, x=1500, y=2100) is None  # fresh single baseline: none


def test_max_finger_size_zero_disables_gate():
    # max_finger_size <= 0 turns the size gate off entirely: contacts of any
    # reported size stay usable.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480),
                      max_finger_size=0.0)
    assert _slot(tr, 0, tid=10, x=1200, y=1800, size=1000) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000, size=1000) is None
    ev = _slot(tr, 0, x=1100, y=1700)
    assert ev is not None and ev.kind == "pinch"
    assert ev.factor == pytest.approx(1.5)


def test_oversized_contact_does_not_pan():
    # Single-finger pan honours the size gate too: a resting palm dragging
    # across the panel must not pan the canvas.
    tr = PinchTracker(1860, 2480, touch_range=(1860, 2480), enable_pan=True)
    assert _slot(tr, 0, tid=10, x=500, y=500, size=150) is None
    assert _slot(tr, 0, x=700, y=700) is None


# ---------------------------------------------------------------------------
# Integration: pinch anchors the canvas point under the fingers, not the
# screen centre (the regression the wrong ABS_MT_* constants caused).
# ---------------------------------------------------------------------------

def test_pinch_anchors_at_fingers_not_screen_centre():
    # Two fingers pinch-open around (1250, 1850) on a 1860x2480 Scribe panel,
    # well off-centre.  The focal point must be the finger midpoint, the drag
    # the midpoint movement, and the resulting viewport must keep the canvas
    # point under the fingers pinned to the same panel position.
    tr = PinchTracker(1860, 2480)  # adaptive bounds: raw max <= canvas dims
    assert _slot(tr, 0, tid=10, x=1200, y=1800) is None
    assert _slot(tr, 1, tid=11, x=1400, y=2000) is None
    ev = _slot(tr, 0, x=1100, y=1700)  # spread 200*sqrt2 -> 300*sqrt2
    assert ev is not None and ev.kind == "pinch"
    # Focal = current finger midpoint (1250, 1850), NOT the panel centre
    # (930, 1240) the buggy build anchored to.
    assert (ev.focal_x, ev.focal_y) == pytest.approx((1250.0, 1850.0))
    assert ev.factor == pytest.approx(1.5)
    # Midpoint moved (1300, 1900) -> (1250, 1850).
    assert (ev.dx, ev.dy) == pytest.approx((-50.0, -50.0))

    vp = Viewport(1860, 2480)
    assert vp.apply_pinch(ev.factor, ev.focal_x, ev.focal_y, ev.dx, ev.dy) is True
    assert vp.scale == pytest.approx(1.5)
    # offset' = 0 + f/s - (f + d)/s' = 1250 - 1200/1.5 = 450 (x),
    #                       1850 - 1800/1.5 = 650 (y); both inside the clamps
    # (620 and 826.67), so no clamping distorts the anchor.
    assert vp.offset_x == pytest.approx(450.0)
    assert vp.offset_y == pytest.approx(650.0)
    # The canvas point that was under the fingers' midpoint maps back to the
    # panel position the fingers are at now: WYSIWYG anchoring.
    panel = vp.canvas_to_panel(1250.0, 1850.0, 1860, 2480)
    assert panel == pytest.approx((1200.0, 1800.0))
