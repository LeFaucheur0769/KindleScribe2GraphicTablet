"""Tests for the pinch-zoom viewport integrated into Canvas.

These complement tests/test_gestures.py (which tests the pure Viewport /
PinchTracker math) by checking the Canvas-side behaviour: version bumps,
viewport-aware preview rendering, coordinate mapping for the stylus and
cursor, and the gesture entry point used by the touch reader thread.
"""

from PIL import Image

from kindle_draw.canvas import Canvas
from kindle_draw.gestures import GestureEvent


def _dot(c: Canvas, x: float, y: float) -> None:
    """Draw a dot at full pressure (== the currently configured brush size)."""
    c.draw_dot(x, y, 1.0)


def test_default_viewport_is_identity():
    c = Canvas(1860, 2480)
    assert c.viewport_identity
    assert c.viewport.scale == 1.0
    assert c.viewport.offset_x == 0.0
    assert c.viewport.offset_y == 0.0


def test_zoom_at_bumps_version_without_touching_dirty_box():
    c = Canvas(1860, 2480)
    assert c.version == 0
    assert c.zoom_at(2.0, 930, 1240) is True
    assert c.version == 1
    assert c.viewport.scale == 2.0
    # zoom keeps the focal canvas point centred: window = [465,1395] x [620,1860]
    assert c.viewport.offset_x == 465.0
    assert c.viewport.offset_y == 620.0
    # the dirty box must stay empty: the streamer re-renders from version alone
    assert c.take_dirty() is None
    # a no-op zoom must not bump the version (no spurious full-panel redraw)
    assert c.zoom_at(1.0, 930, 1240) is False
    assert c.version == 1


def test_zoom_respects_scale_limits():
    c = Canvas(1860, 2480, max_scale=8.0)
    c.zoom_at(100.0, 930, 1240)
    assert c.viewport.scale == 8.0
    c.zoom_at(0.01, 930, 1240)
    assert c.viewport.scale == 1.0
    assert c.viewport_identity


def test_pan_by_moves_the_window_and_bumps_version():
    c = Canvas(1860, 2480)
    c.zoom_at(2.0, 930, 1240)  # offset (465, 620)
    v = c.version
    assert c.pan_by(100, 0) is True
    assert c.version == v + 1
    # fingers move right -> window moves left in canvas space (content follows)
    assert c.viewport.offset_x == 415.0
    # a fully clamped pan (identity viewport cannot pan) reports no change
    c.reset_viewport()  # resets the viewport, bumping the version once more
    v2 = c.version
    assert c.pan_by(100, 0) is False
    assert c.version == v2


def test_pan_clamps_at_the_canvas_edges():
    c = Canvas(1860, 2480)
    c.zoom_at(2.0, 930, 1240)  # offset (465, 620), max offset_x = 930
    c.pan_by(-10_000, 0)  # fingers left -> window right, clamped
    assert c.viewport.offset_x == 930.0
    assert c.viewport.offset_y == 620.0
    # further panning while clamped changes nothing
    assert c.pan_by(-100, 0) is False
    c.pan_by(10_000, 0)  # fingers right -> window left, clamped at 0
    assert c.viewport.offset_x == 0.0


def test_apply_gesture_pinch_composes_zoom_and_pan():
    c = Canvas(1860, 2480)
    ev = GestureEvent(kind="pinch", factor=2.0, dx=50.0, dy=0.0,
                      focal_x=930.0, focal_y=1240.0)
    assert c.apply_gesture(ev) is True
    assert c.viewport.scale == 2.0
    # offset' = 465 + 930/2 - (930+50)/2 = 465 - 25 = 440
    assert c.viewport.offset_x == 440.0
    assert c.viewport.offset_y == 620.0
    assert c.version == 1

    ev = GestureEvent(kind="pan", factor=1.0, dx=0.0, dy=100.0,
                      focal_x=0.0, focal_y=0.0)
    assert c.apply_gesture(ev) is True
    assert c.viewport.offset_y == 570.0  # 620 - 100/2

    assert c.apply_gesture(GestureEvent(kind="unknown")) is False


def test_reset_viewport():
    c = Canvas(1860, 2480)
    c.zoom_at(3.0, 100, 100)
    assert not c.viewport_identity
    assert c.reset_viewport() is True
    assert c.viewport_identity
    assert c.reset_viewport() is False  # already identity: no change, no bump


def test_view_canvas_round_trip_under_zoom():
    c = Canvas(1860, 2480)
    c.zoom_at(2.5, 900, 1200)  # offset (540, 720)
    assert c.viewport.offset_x == 540.0
    assert c.viewport.offset_y == 720.0
    # the four corners of the view map to the viewport window corners
    assert c.view_to_canvas(0, 0) == (540.0, 720.0)
    assert c.view_to_canvas(1860, 2480) == (1284.0, 1712.0)
    # and back again
    assert c.canvas_to_view(540, 720) == (0.0, 0.0)
    assert c.canvas_to_view(1284, 1712) == (1860.0, 2480.0)
    # a stylus point inside the window round-trips through both helpers
    cx, cy = c.view_to_canvas(930, 1240)
    assert c.canvas_to_view(cx, cy) == (930.0, 1240.0)


def test_canvas_to_panel_maps_through_the_viewport():
    c = Canvas(1860, 2480)
    # identity: canvas coords map 1:1 onto the panel
    assert c.canvas_to_panel(100, 200, 1860, 2480) == (100, 200)
    c.zoom_at(2.0, 930, 1240)  # window [465,1395] x [620,1860]
    # window corners map to panel corners (pure stretch, same aspect)
    assert c.canvas_to_panel(465, 620, 1860, 2480) == (0, 0)
    assert c.canvas_to_panel(1395, 1860, 1860, 2480) == (1860, 2480)
    # the focal point stays centred on the panel
    assert c.canvas_to_panel(930, 1240, 1860, 2480) == (930, 1240)


def test_viewport_box_guard_extreme_zoom_tiny_canvas():
    # 1px window at max zoom must still produce a >=1x1 crop for Pillow
    c = Canvas(4, 4, max_scale=8.0)
    c.zoom_at(8.0, 4, 4)  # offset clamped to (3.5, 3.5)
    assert c.viewport.scale == 8.0
    assert c.viewport.offset_x == 3.5
    box = c.viewport_box()
    assert box[2] - box[0] >= 1 and box[3] - box[1] >= 1
    prev = c.render_preview(4, 4)
    assert prev.size == (4, 4)


def test_render_preview_identity_fast_path_unchanged():
    c = Canvas(1860, 2480)
    c.set_pen_size(20)
    c.set_pressure_sensitive(False)
    _dot(c, 930, 1240)
    prev = c.render_preview(1860, 2480)
    assert prev.mode == "L"
    assert prev.getpixel((930, 1240)) < 128
    assert prev.getpixel((100, 100)) == 255


def test_render_preview_zoomed_crops_the_window_and_fills_the_panel():
    # canvas aspect == panel aspect: the zoomed window is stretched 1:1
    c = Canvas(1860, 2480)
    c.set_pen_size(10)
    c.set_pressure_sensitive(False)
    _dot(c, 500, 700)    # inside the future window
    _dot(c, 1700, 2300)  # outside the future window
    c.zoom_at(2.0, 930, 1240)  # window [465,1395] x [620,1860]

    prev = c.render_preview(1860, 2480)
    assert prev.size == (1860, 2480)
    # the dot at canvas (500,700) lands at panel (70,160):
    # view = (500-465)*2 = 70, (700-620)*2 = 160
    assert prev.getpixel((70, 160)) < 128
    # the dot outside the window is gone from the preview
    assert prev.getpixel((1240, 1660)) == 255  # where (1700,2300) used to be
    # just inside the window's bottom-right corner: plain white canvas
    assert prev.getpixel((1859, 2479)) == 255  # canvas (1394.5, 1859.25)


def test_pen_stroke_under_zoom_appears_scaled_on_the_panel():
    # brush widths stay in canvas units: a 2x zoom doubles their panel size
    c = Canvas(1860, 2480)
    c.set_pen_size(10)
    c.set_pressure_sensitive(False)  # constant width 10 -> radius 5 canvas px
    _dot(c, 500, 500)

    # identity: dot spans canvas [495,505] -> panel pixels 495..505 dark
    prev = c.render_preview(1860, 2480)
    assert prev.getpixel((500, 500)) < 128
    assert prev.getpixel((500, 505)) < 128
    assert prev.getpixel((500, 507)) == 255

    c.zoom_at(2.0, 500, 500)  # window [250,750] x [250,750]
    prev = c.render_preview(1860, 2480)
    # canvas radius 5 -> 10 view px -> 10 panel px (same aspect, 1:1 stretch)
    assert prev.getpixel((500, 500)) < 128
    assert prev.getpixel((500, 509)) < 128   # canvas 500.4: inside r=5
    assert prev.getpixel((500, 513)) == 255  # canvas 502.2: outside r=5


def test_render_preview_zoomed_dot_position_tracks_pan():
    c = Canvas(1860, 2480)
    c.set_pen_size(10)
    c.set_pressure_sensitive(False)
    c.zoom_at(2.0, 930, 1240)  # offset (465, 620)
    c.pan_by(-100, 0)          # window shifts right: offset (515, 620)
    _dot(c, 520, 700)

    prev = c.render_preview(1860, 2480)
    # view = (520-515)*2 = 10, (700-620)*2 = 160
    assert prev.getpixel((10, 160)) < 128
    assert prev.getpixel((12, 162)) < 128


def test_clear_and_replace_image_keep_the_viewport():
    c = Canvas(1860, 2480)
    c.zoom_at(2.0, 930, 1240)
    v = c.version

    c.clear()
    assert c.version == v + 1
    assert c.viewport.scale == 2.0
    assert c.viewport.offset_x == 465.0

    c.replace_image(Image.new("RGB", (1860, 2480), "white"))
    assert c.version == v + 2
    assert c.viewport.scale == 2.0
    assert c.viewport.offset_y == 620.0
