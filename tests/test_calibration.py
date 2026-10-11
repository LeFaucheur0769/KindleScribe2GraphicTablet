"""Tests for the pure math / persistence helpers in kindle_draw.calibration."""

import json

import pytest

from kindle_draw.calibration import (
    apply_rotation,
    build_transform,
    load_calibration,
    normalize_pressure,
    rotated_size,
    save_calibration,
    transform_is_legacy,
    transform_xy,
    validate_calibration,
)


# ---------------------------------------------------------------------------
# build_transform
# ---------------------------------------------------------------------------

def _make_affine_samples(a, b, c, d, e, f):
    raws = [(100, 100), (900, 100), (100, 900), (900, 900), (500, 500)]
    samples = []
    for rx, ry in raws:
        samples.append({
            "raw": (rx, ry),
            "canvas": (a * rx + b * ry + c, d * rx + e * ry + f),
        })
    return samples


def test_build_transform_recovers_known_affine():
    a, b, c, d, e, f = 0.5, 0.1, 10.0, -0.2, 0.7, 5.0
    cal = {"transform": build_transform(_make_affine_samples(a, b, c, d, e, f))}

    t = cal["transform"]
    assert t["a"] == pytest.approx(a)
    assert t["b"] == pytest.approx(b)
    assert t["c"] == pytest.approx(c)
    assert t["d"] == pytest.approx(d)
    assert t["e"] == pytest.approx(e)
    assert t["f"] == pytest.approx(f)

    # Round-trip: a point never used as a sample must map exactly.
    cx, cy = transform_xy(cal, 500, 500)
    assert cx == pytest.approx(a * 500 + b * 500 + c)
    assert cy == pytest.approx(d * 500 + e * 500 + f)


def test_build_transform_requires_at_least_three_samples():
    samples = [
        {"raw": (0, 0), "canvas": (0, 0)},
        {"raw": (10, 10), "canvas": (10, 10)},
    ]
    with pytest.raises(ValueError):
        build_transform(samples)


def test_build_transform_rejects_collinear_raw_points():
    samples = [
        {"raw": (0, 0), "canvas": (0, 0)},
        {"raw": (1, 1), "canvas": (1, 1)},
        {"raw": (2, 2), "canvas": (2, 2)},
    ]
    with pytest.raises(ValueError):
        build_transform(samples)


def test_build_transform_rejects_coincident_raw_points():
    samples = [
        {"raw": (5, 5), "canvas": (0, 0)},
        {"raw": (5, 5), "canvas": (10, 0)},
        {"raw": (5, 5), "canvas": (0, 10)},
    ]
    with pytest.raises(ValueError):
        build_transform(samples)


# ---------------------------------------------------------------------------
# transform_xy / legacy diagonal transforms
# ---------------------------------------------------------------------------

def test_transform_xy_affine():
    cal = {"transform": {"a": 2.0, "b": 0.5, "c": 7.0,
                         "d": -0.25, "e": 1.5, "f": -3.0}}
    cx, cy = transform_xy(cal, 100, 200)
    assert cx == pytest.approx(2.0 * 100 + 0.5 * 200 + 7.0)
    assert cy == pytest.approx(-0.25 * 100 + 1.5 * 200 - 3.0)


def test_transform_xy_legacy_diagonal():
    cal = {"transform": {"ax": 2.0, "bx": 1.0, "ay": 3.0, "by": 4.0}}
    assert transform_is_legacy(cal) is True
    assert transform_xy(cal, 10, 20) == (21.0, 64.0)


def test_transform_is_legacy_modern():
    cal = {"transform": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0}}
    assert transform_is_legacy(cal) is False


# ---------------------------------------------------------------------------
# validate_calibration
# ---------------------------------------------------------------------------

def test_validate_accepts_affine():
    validate_calibration({"transform": {"a": 1, "b": 0, "c": 0,
                                        "d": 0, "e": 1, "f": 0}})


def test_validate_rejects_incomplete_affine():
    with pytest.raises(ValueError):
        validate_calibration({"transform": {"a": 1, "b": 0, "c": 0, "d": 0}})


def test_validate_rejects_missing_transform():
    with pytest.raises(ValueError):
        validate_calibration({})


def test_validate_accepts_legacy():
    validate_calibration({"transform": {"ax": 2, "bx": 0, "ay": 3, "by": 0}})


def test_validate_rejects_degenerate_legacy():
    with pytest.raises(ValueError):
        validate_calibration({"transform": {"ax": 0, "bx": 1, "ay": 3, "by": 0}})


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    cal = {
        "panel": {"w": 1860, "h": 2480},
        "transform": {"a": 0.5, "b": 0.1, "c": 10,
                      "d": -0.2, "e": 0.7, "f": 5},
    }
    path = str(tmp_path / "nested" / "calibration.json")
    save_calibration(path, cal)
    loaded = load_calibration(path)
    assert loaded == cal
    # The file on disk is real JSON, not a repr dump.
    with open(path) as fh:
        assert json.load(fh) == cal


# ---------------------------------------------------------------------------
# normalize_pressure
# ---------------------------------------------------------------------------

def test_normalize_pressure_midpoint():
    # Signature is normalize_pressure(pmin, pmax, pressure).
    assert normalize_pressure(0, 1000, 500) == pytest.approx(0.5)


def test_normalize_pressure_clamps():
    assert normalize_pressure(0, 1000, -50) == 0.0
    assert normalize_pressure(0, 1000, 2000) == 1.0


def test_normalize_pressure_degenerate_range():
    # pmax <= pmin always yields full pressure.
    assert normalize_pressure(100, 100, 42) == 1.0
    assert normalize_pressure(100, 50, 42) == 1.0


# ---------------------------------------------------------------------------
# rotation helpers
# ---------------------------------------------------------------------------

def test_rotated_size():
    assert rotated_size(1860, 2480, 0) == (1860, 2480)
    assert rotated_size(1860, 2480, 90) == (2480, 1860)
    assert rotated_size(1860, 2480, 180) == (1860, 2480)
    assert rotated_size(1860, 2480, 270) == (2480, 1860)


def test_apply_rotation_identity():
    assert apply_rotation(123, 456, 1860, 2480, 0) == (123, 456)


def test_apply_rotation_90_maps_corners_into_rotated_frame():
    w, h = 100, 200
    rw, rh = rotated_size(w, h, 90)
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    for px, py in corners:
        x, y = apply_rotation(px, py, w, h, 90)
        assert (x, y) == (h - py, px)
        assert 0 <= x <= rw and 0 <= y <= rh


def test_apply_rotation_180():
    assert apply_rotation(10, 20, 100, 200, 180) == (90, 180)


def test_apply_rotation_270():
    assert apply_rotation(10, 20, 100, 200, 270) == (20, 90)


def test_apply_rotation_rejects_invalid():
    with pytest.raises(ValueError):
        apply_rotation(1, 2, 100, 200, 45)
