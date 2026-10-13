"""Tests for zoom-linked rendering resolution and logarithmic pen scaling.

Covers the TODO features end to end:

* ``--canvas-resolution N`` sizes the canvas buffer N times the panel, so
  the deepest zoom maps canvas pixels 1:1 onto panel pixels (sharp ink).
* The zoom ceiling defaults to that same factor N (``Viewport`` clamps
  scale into [1, N]); an explicit ``--max-zoom`` overrides it and warns
  when it exceeds N.
* Brush widths (configured in panel pixels) grow logarithmically with the
  zoom while drawing: on-screen width == size * (1 + ln scale).  With
  ``--fixed-pen-size`` the on-screen width stays constant instead.
"""

import math

import pytest

from kindle_draw.app import build_config, parse_args
from kindle_draw.canvas import Canvas, pen_zoom_factor
from kindle_draw.gestures import Viewport
from kindle_draw.pages import PageManager


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _ink_width(c: Canvas, y: float) -> int:
    """Count dark pixels along the canvas row ``y``.

    Assumes the only ink on that row is the dot being measured (each test
    draws one dot on a fresh canvas), so the run length equals the brush
    diameter in *canvas* pixels.
    """
    px = c.image.load()
    w, h = c.image.size
    yy = int(round(y))
    return sum(1 for x in range(w) if px[x, yy][0] < 128)


def _center_dot(c: Canvas) -> None:
    """Draw one full-pressure dot at the canvas centre (default pen: black,
    size 8 panel px, pressure-sensitive so pressure 1.0 == full size)."""
    c.draw_dot(c.width / 2.0, c.height / 2.0, 1.0)


# --------------------------------------------------------------------------- #
# pen_zoom_factor: pure formula                                              #
# --------------------------------------------------------------------------- #

def test_pen_zoom_factor_log_mode():
    # At scale 1 the factor is just the resolution: the stroke must be N
    # times wider in canvas units to look identical on the panel.
    assert pen_zoom_factor(1.0, 4) == pytest.approx(4.0)
    assert pen_zoom_factor(1.0, 1) == pytest.approx(1.0)
    # Log growth term: on-screen width = size * (1 + ln scale).  The
    # on-screen ratio is factor * scale / resolution (panel px per canvas
    # px is scale / resolution).
    for s in (1.0, 1.5, 2.0, 4.0):
        expected = (1.0 + math.log(s)) * 4.0 / s
        assert pen_zoom_factor(s, 4) == pytest.approx(expected)
        on_screen = pen_zoom_factor(s, 4) * s / 4.0
        assert on_screen == pytest.approx(1.0 + math.log(s))


def test_pen_zoom_factor_fixed_mode():
    # Fixed mode cancels the log term: on-screen width stays constant.
    assert pen_zoom_factor(1.0, 4, log_scaling=False) == pytest.approx(4.0)
    assert pen_zoom_factor(4.0, 4, log_scaling=False) == pytest.approx(1.0)
    for s in (1.0, 2.0, 4.0):
        on_screen = pen_zoom_factor(s, 4, log_scaling=False) * s / 4.0
        assert on_screen == pytest.approx(1.0)


def test_pen_zoom_factor_growth_clamped():
    # 1 + ln(scale) goes negative below scale ~0.37; the growth term is
    # clamped at 0.5 so strokes never vanish (or invert).
    assert pen_zoom_factor(0.25, 1) == pytest.approx(0.5 / 0.25)
    assert pen_zoom_factor(0.1, 1) == pytest.approx(0.5 / 0.1)
    # Fixed mode has no growth term to clamp, only the resolution/scale ratio.
    assert pen_zoom_factor(0.5, 1, log_scaling=False) == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# Canvas: resolution sizes the buffer, pen factor follows the viewport       #
# --------------------------------------------------------------------------- #

def test_canvas_resolution_sizes_buffer():
    # ``Canvas`` does *not* scale its own buffer: it allocates exactly the
    # (width, height) it is given.  The multiplication happens upstream in
    # ``build_config`` (canvas = panel * N) and the app then constructs the
    # canvas at those already-scaled dimensions (see the wiring in app.py).
    # This test walks that chain so the buffer really is 4x the panel.
    cfg = build_config(parse_args(["--canvas-resolution", "4"]))
    c = Canvas(cfg.canvas_width, cfg.canvas_height,
               resolution=cfg.canvas_resolution)
    assert c.image.size == (7440, 9920)          # 1860x2480 panel * 4
    assert c.viewport.canvas_w == pytest.approx(7440)
    assert c.viewport.canvas_h == pytest.approx(9920)
    assert c.resolution == 4                     # metadata for the pen factor
    # Out-of-range resolutions are coerced to at least 1.
    assert Canvas(100, 100, resolution=0).resolution == 1
    assert Canvas(100, 100).resolution == 1      # default keeps old behaviour


def test_pen_width_at_zoom_log_mode():
    # 1860x2480 here is the *canvas* buffer: think of it as a 465x620 panel
    # scaled 4x (a quarter-size Scribe).  That keeps the test light while
    # staying faithful to the app wiring, where build_config hands Canvas the
    # already-scaled dims (7440x9920 for the real panel at 4x).
    c = Canvas(1860, 2480, resolution=4)  # default pen size 8 panel px
    # Scale 1: the whole 1860x2480 canvas is squeezed into the 465x620 panel,
    # so 1 canvas px shows as 1/4 panel px.  The dot is 8 * 4 = 32 canvas px
    # wide, which appears as 8 panel px on screen.
    _center_dot(c)
    assert _ink_width(c, c.height / 2.0) == pytest.approx(32, abs=2)

    # Zoom to the ceiling (4x): the viewport window is panel-sized, so 1
    # canvas px == 1 panel px.  A *new* stroke drawn now gets the log factor:
    # 8 * (1 + ln 4) ~= 19.09 canvas px, which is also ~19.09 panel px on
    # screen (sharper and slightly thicker than at scale 1, but nowhere near
    # the 32 px a linear scale would give).
    assert c.zoom_at(4.0, 930, 1240) is True
    assert c.viewport.scale == 4.0
    c.clear()
    _center_dot(c)
    expected = 8.0 * (1.0 + math.log(4.0))
    assert _ink_width(c, c.height / 2.0) == pytest.approx(expected, abs=2)


def test_pen_width_fixed_mode_constant_on_screen():
    c = Canvas(1860, 2480, resolution=4)
    assert c.log_pen_scaling is True          # on by default
    assert c.set_log_pen_scaling(False) is True   # reports the change
    assert c.set_log_pen_scaling(False) is False  # idempotent
    assert c.log_pen_scaling is False

    _center_dot(c)
    assert _ink_width(c, c.height / 2.0) == pytest.approx(32, abs=2)

    assert c.zoom_at(4.0, 930, 1240) is True
    # Fixed mode: factor = resolution / scale = 1 -> 8 canvas px, and at 1:1
    # zoom that is 8 panel px on screen: identical apparent size as scale 1.
    assert c.pen_factor() == pytest.approx(1.0)
    c.clear()
    _center_dot(c)
    assert _ink_width(c, c.height / 2.0) == pytest.approx(8, abs=2)


def test_pen_factor_tracks_viewport_scale():
    c = Canvas(1860, 2480, resolution=4)
    assert c.pen_factor() == pytest.approx(4.0)      # scale 1
    c.zoom_at(2.0, 930, 1240)
    assert c.pen_factor() == pytest.approx(
        (1.0 + math.log(2.0)) * 4.0 / 2.0)


# --------------------------------------------------------------------------- #
# build_config: resolution -> canvas size and zoom ceiling                   #
# --------------------------------------------------------------------------- #

def test_build_config_defaults_resolution_and_max_zoom():
    cfg = build_config(parse_args([]))
    assert cfg.canvas_resolution == 4
    assert cfg.canvas_width == 1860 * 4
    assert cfg.canvas_height == 2480 * 4
    # Zoom ceiling auto-follows the resolution factor.
    assert cfg.touch.max_scale == pytest.approx(4.0)
    assert cfg.pen.fixed_pen_size is False


def test_build_config_resolution_sets_zoom_ceiling():
    cfg = build_config(parse_args(["--canvas-resolution", "2"]))
    assert cfg.canvas_resolution == 2
    assert cfg.canvas_width == 3720
    assert cfg.canvas_height == 4960
    assert cfg.touch.max_scale == pytest.approx(2.0)


def test_build_config_max_zoom_override_and_warning(caplog):
    cfg = build_config(parse_args(["--max-zoom", "6"]))
    assert cfg.touch.max_scale == pytest.approx(6.0)
    assert "exceeds the canvas resolution factor" in caplog.text

    # An override *below* the ceiling is fine and silent.
    cfg = build_config(parse_args(["--max-zoom", "2"]))
    assert cfg.touch.max_scale == pytest.approx(2.0)


def test_build_config_max_zoom_must_exceed_one():
    with pytest.raises(ValueError, match="must be > 1"):
        build_config(parse_args(["--max-zoom", "1"]))
    with pytest.raises(ValueError, match="must be > 1"):
        build_config(parse_args(["--max-zoom", "0.5"]))


def test_build_config_resolution_one_warns_about_zoom(caplog):
    cfg = build_config(parse_args(["--canvas-resolution", "1"]))
    assert cfg.canvas_width == 1860
    assert cfg.touch.max_scale == pytest.approx(1.0)
    assert "canvas is panel-sized" in caplog.text
    # ...and an explicit --max-zoom still overrides it cleanly.
    cfg = build_config(parse_args(["--canvas-resolution", "1",
                                   "--max-zoom", "2"]))
    assert cfg.touch.max_scale == pytest.approx(2.0)


def test_build_config_explicit_canvas_dims_derive_resolution():
    # Explicit dims win over --canvas-resolution; the factor is derived from
    # them so pen scaling and the zoom ceiling stay consistent.
    cfg = build_config(parse_args(["--canvas-width", "3720",
                                   "--canvas-height", "4960"]))
    assert cfg.canvas_width == 3720
    assert cfg.canvas_height == 4960
    assert cfg.canvas_resolution == 2
    assert cfg.touch.max_scale == pytest.approx(2.0)


def test_build_config_fixed_pen_size_flag():
    cfg = build_config(parse_args(["--fixed-pen-size"]))
    assert cfg.pen.fixed_pen_size is True


# --------------------------------------------------------------------------- #
# Default zoom ceilings on the wiring classes                                #
# --------------------------------------------------------------------------- #

def test_default_max_scale_is_four():
    assert Canvas(1860, 2480).viewport.max_scale == pytest.approx(4.0)
    assert Viewport(1860, 2480).max_scale == pytest.approx(4.0)
    pm = PageManager(1860, 2480, resolution=4)
    assert pm.max_scale == pytest.approx(4.0)
    assert pm.resolution == 4
    # Every page the manager hands out shares the ceiling and resolution
    # (current() returns the Canvas itself).
    page = pm.current()
    assert page.viewport.max_scale == pytest.approx(4.0)
    assert page.resolution == 4
    # PageManager forwards its width/height straight to Canvas (no internal
    # scaling): the app gives it the already-scaled dims, so 1860x2480 here
    # is the canvas buffer of a 465x620 panel scaled 4x.  On a real Scribe
    # build_config would hand the manager 7440x9920 for resolution 4.
    assert page.image.size == (1860, 2480)
