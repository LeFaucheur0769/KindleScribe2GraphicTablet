"""Tests for the pure geometry / diff helpers in kindle_draw.streamer."""

from PIL import Image, ImageChops

from kindle_draw.streamer import (
    CURSOR_HALO,
    CURSOR_OUTLINE,
    CURSOR_RADIUS,
    _cursor_bbox,
    _diff_bbox,
    clamp_box,
    snap_box,
    union,
)


# ---------------------------------------------------------------------------
# union
# ---------------------------------------------------------------------------

def test_union_none_tolerant():
    assert union(None, None) is None
    assert union(None, (1, 2, 3, 4)) == (1, 2, 3, 4)
    assert union((1, 2, 3, 4), None) == (1, 2, 3, 4)


def test_union_two_boxes():
    assert union((5, 5, 10, 10), (0, 7, 6, 20)) == (0, 5, 10, 20)


# ---------------------------------------------------------------------------
# snap_box
# ---------------------------------------------------------------------------

def test_snap_box_aligns_outward():
    assert snap_box((3, 5, 10, 12), 8, 100, 100) == (0, 0, 16, 16)


def test_snap_box_already_aligned_is_noop():
    assert snap_box((8, 16, 24, 32), 8, 100, 100) == (8, 16, 24, 32)


def test_snap_box_align_one_is_noop():
    assert snap_box((3, 5, 10, 12), 1, 100, 100) == (3, 5, 10, 12)


# ---------------------------------------------------------------------------
# clamp_box
# ---------------------------------------------------------------------------

def test_clamp_box_clamps_to_panel():
    assert clamp_box((-10, -5, 50, 3000), 100, 200) == (0, 0, 50, 200)


def test_clamp_box_inside_is_noop():
    assert clamp_box((1, 2, 3, 4), 100, 100) == (1, 2, 3, 4)


def test_clamp_box_degenerate_becomes_none():
    assert clamp_box((50, 50, 50, 60), 100, 100) is None
    assert clamp_box((50, 50, 60, 50), 100, 100) is None
    # Fully outside the panel collapses to a zero-size box.
    assert clamp_box((-30, -30, -10, -10), 100, 100) is None


# ---------------------------------------------------------------------------
# _diff_bbox
# ---------------------------------------------------------------------------

def _white_pair():
    """Two identical 10x10 white grayscale images."""
    return Image.new("L", (10, 10), 255), Image.new("L", (10, 10), 255)


def test_diff_bbox_identical_images_is_none():
    a, b = _white_pair()
    assert _diff_bbox(ImageChops.difference(a, b), 0) is None
    assert _diff_bbox(ImageChops.difference(a, b), 20) is None


def test_diff_bbox_threshold_filters_small_changes():
    a, b = _white_pair()
    b.putpixel((3, 4), 255 - 10)
    diff = ImageChops.difference(a, b)
    # Delta of 10 is below the threshold: treated as noise.
    assert _diff_bbox(diff, 20) is None
    # With a lower threshold the single changed pixel is found.
    assert _diff_bbox(diff, 5) == (3, 4, 4, 5)


def test_diff_bbox_no_threshold_uses_any_difference():
    a, b = _white_pair()
    b.putpixel((9, 9), 254)
    diff = ImageChops.difference(a, b)
    assert _diff_bbox(diff, 0) == (9, 9, 10, 10)
    assert _diff_bbox(diff, 20) is None


# ---------------------------------------------------------------------------
# _cursor_bbox
# ---------------------------------------------------------------------------

def test_cursor_bbox_is_symmetric_square_around_point():
    r = CURSOR_RADIUS + CURSOR_OUTLINE + CURSOR_HALO + 4
    assert _cursor_bbox(100, 200) == (100 - r, 200 - r, 100 + r, 200 + r)


def test_cursor_bbox_contains_drawn_reticle():
    # The bbox must fully contain the pixels _draw_cursor actually changes.
    # (getbbox() on the canvas itself would return the whole image because
    # white is non-zero in mode "L", so diff against a blank canvas.)
    from kindle_draw.streamer import _draw_cursor
    blank = Image.new("L", (400, 400), 255)
    img = blank.copy()
    _draw_cursor(img, 200, 200)
    drawn = ImageChops.difference(blank, img).getbbox()
    assert drawn is not None
    box = _cursor_bbox(200, 200)
    assert drawn[0] >= box[0] and drawn[1] >= box[1]
    assert drawn[2] <= box[2] and drawn[3] <= box[3]
