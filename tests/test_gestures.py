"""Tests for the pure gesture / viewport math in kindle_draw.gestures."""

import math

import pytest

from kindle_draw.gestures import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    EV_ABS,
    EV_KEY,
    EV_SYN,
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

def _slot(tracker, slot, tid=None, x=None, y=None):
    """Feed one slot's worth of MT-B events, closed by an EV_SYN frame.

    The kernel groups all slot updates for one sampling instant into a frame
    terminated by ``SYN_REPORT`` and the tracker evaluates gestures once per
    frame, so the helper mirrors that and returns the event (if any) the
    frame produced.
    """
    tracker.feed(EV_ABS, ABS_MT_SLOT, slot)
    if tid is not None:
        tracker.feed(EV_ABS, ABS_MT_TRACKING_ID, tid)
    if x is not None:
        tracker.feed(EV_ABS, ABS_MT_POSITION_X, x)
    if y is not None:
        tracker.feed(EV_ABS, ABS_MT_POSITION_Y, y)
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
