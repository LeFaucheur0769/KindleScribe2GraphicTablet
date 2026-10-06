
"""Map raw stylus coordinates to canvas coordinates.

Three-point calibration. The transform is a general 2D affine, which
handles any rotation / axis swap between the digitizer's native frame
and the user's perceived frame — so calibration works whether the
Scribe is held in portrait or landscape.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from typing import Optional, Tuple

from PIL import Image, ImageDraw

from .evdev_reader import StylusTracker, unpack_event

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Target rendering                                                           #
# --------------------------------------------------------------------------- #

def _make_target_image(w: int, h: int, tx: int, ty: int, label: str) -> Image.Image:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)

    border = 20
    for i in range(border):
        d.rectangle([i, i, w - 1 - i, h - 1 - i], outline=(0, 0, 0))

    r_outer = 90
    r_inner = 60
    d.ellipse([tx - r_outer, ty - r_outer, tx + r_outer, ty + r_outer],
              outline=(0, 0, 0), width=10)
    d.ellipse([tx - r_inner, ty - r_inner, tx + r_inner, ty + r_inner],
              outline=(0, 0, 0), width=4)
    d.line([tx - r_outer, ty, tx + r_outer, ty], fill=(0, 0, 0), width=8)
    d.line([tx, ty - r_outer, tx, ty + r_outer], fill=(0, 0, 0), width=8)
    d.ellipse([tx - 12, ty - 12, tx + 12, ty + 12], fill=(0, 0, 0))

    lx = tx + r_outer + 30
    ly = ty - 20
    if lx + 200 > w:
        lx = tx - r_outer - 230
    d.text((lx, ly), label, fill=(0, 0, 0))
    d.rectangle([lx - 10, ly - 10, lx + 180, ly + 20],
                outline=(0, 0, 0), width=2)

    d.text((w // 2 - 300, 60),
           "Tap the centre of the black dot with the stylus.",
           fill=(0, 0, 0))
    return img


def _wait_for_tap(kindle, dev: str, esz: int, timeout: float = 120.0
                  ) -> Tuple[int, int, int]:
    proc = kindle.popen(f"cat {dev}")
    try:
        buf = b""
        tracker = StylusTracker()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = proc.stdout.read(esz * 32)
            if not chunk:
                raise RuntimeError("stylus stream ended during calibration")
            buf += chunk
            while len(buf) >= esz:
                ev = buf[:esz]
                buf = buf[esz:]
                t, c, v = unpack_event(ev, esz)
                out = tracker.feed(t, c, v)
                if out is not None and out.kind == "down":
                    return out.x, out.y, out.pressure
        raise TimeoutError("no stylus tap received")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Rotation helpers                                                           #
# --------------------------------------------------------------------------- #

def user_to_native(tx, ty, panel_w, panel_h, rotation):
    """Map a position in the user's perceived frame to native panel coords."""
    if rotation == 0:
        return tx, ty
    if rotation == 90:
        return panel_w - ty, tx
    if rotation == 180:
        return panel_w - tx, panel_h - ty
    if rotation == 270:
        return ty, panel_h - tx
    raise ValueError(f"bad rotation: {rotation}")


def native_to_user(nx, ny, panel_w, panel_h, rotation):
    """Inverse of user_to_native."""
    if rotation == 0:
        return nx, ny
    if rotation == 90:
        return ny, panel_w - nx
    if rotation == 180:
        return panel_w - nx, panel_h - ny
    if rotation == 270:
        return panel_h - ny, nx
    raise ValueError(f"bad rotation: {rotation}")


# --------------------------------------------------------------------------- #
# Calibration driver                                                         #
# --------------------------------------------------------------------------- #

def calibrate(kindle, panel_w: int, panel_h: int, dev: str, esz: int,
              rotation: int = 0) -> list:
    """Prompt the user to tap three targets and return the sample list.

    The user perceives a frame of size ``(user_w, user_h)`` — the native
    panel dimensions, possibly swapped for 90°/270° rotation. We draw
    each crosshair at its native position so FBInk places it correctly
    on the panel, and record the sample's canvas coordinate in the
    *user's* frame. A general affine transform then solves the mapping.
    """
    if rotation in (90, 270):
        user_w, user_h = panel_h, panel_w
    else:
        user_w, user_h = panel_w, panel_h

    targets = [
        (120, 120, "TL"),
        (user_w - 120, 120, "TR"),
        (120, user_h - 120, "BL"),
    ]
    samples = []
    for tx, ty, name in targets:
        nx, ny = user_to_native(tx, ty, panel_w, panel_h, rotation)
        img = _make_target_image(panel_w, panel_h, nx, ny, name)
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        kindle.draw_png(buf.getvalue(), x=0, y=0, wf="GC16", flash=True,
                        no_clear=False)
        log.info("calibration: tap the %s target", name)
        rx, ry, pr = _wait_for_tap(kindle, dev, esz)
        log.info("  %s -> raw=(%d, %d) pressure=%d", name, rx, ry, pr)
        samples.append({"canvas": [tx, ty], "raw": [rx, ry], "pressure": pr})
    return samples


# --------------------------------------------------------------------------- #
# Transform construction and persistence                                     #
# --------------------------------------------------------------------------- #

def build_transform(samples: list) -> dict:
    """Solve a general 2D affine from raw stylus coords to canvas coords.

    The transform is:
        canvas_x = a * raw_x + b * raw_y + c
        canvas_y = d * raw_x + e * raw_y + f

    A general affine (rather than a diagonal one) is required whenever
    the panel is rotated: in landscape, canvas_x is driven by raw_y and
    canvas_y by raw_x, which the diagonal form cannot express.
    """
    if len(samples) < 3:
        raise ValueError("need at least 3 calibration samples")

    (r0x, r0y) = samples[0]["raw"]
    (r1x, r1y) = samples[1]["raw"]
    (r2x, r2y) = samples[2]["raw"]
    (c0x, c0y) = samples[0]["canvas"]
    (c1x, c1y) = samples[1]["canvas"]
    (c2x, c2y) = samples[2]["canvas"]

    det = (r0x * (r1y - r2y)
           + r1x * (r2y - r0y)
           + r2x * (r0y - r1y))
    if abs(det) < 1e-6:
        raise ValueError(
            "degenerate calibration (raw points collinear or coincident); "
            "tap the three crosshairs precisely and try again"
        )

    def det3(a11, a12, a13,
             a21, a22, a23,
             a31, a32, a33):
        return (a11 * (a22 * a33 - a23 * a32)
                - a12 * (a21 * a33 - a23 * a31)
                + a13 * (a21 * a32 - a22 * a31))

    a = det3(c0x, r0y, 1,
             c1x, r1y, 1,
             c2x, r2y, 1) / det
    b = det3(r0x, c0x, 1,
             r1x, c1x, 1,
             r2x, c2x, 1) / det
    c = det3(r0x, r0y, c0x,
             r1x, r1y, c1x,
             r2x, r2y, c2x) / det
    d = det3(c0y, r0y, 1,
             c1y, r1y, 1,
             c2y, r2y, 1) / det
    e = det3(r0x, c0y, 1,
             r1x, c1y, 1,
             r2x, c2y, 1) / det
    f = det3(r0x, r0y, c0y,
             r1x, r1y, c1y,
             r2x, r2y, c2y) / det

    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f}


def save_calibration(path: str, data: dict) -> None:
    p = os.path.expanduser(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as fh:
        json.dump(data, fh, indent=2)


def load_calibration(path: str) -> dict:
    p = os.path.expanduser(path)
    with open(p, "r") as fh:
        return json.load(fh)


def validate_calibration(cal: dict) -> None:
    t = cal.get("transform")
    if not t:
        raise ValueError("calibration missing 'transform'")
    if "a" in t:
        if not all(k in t for k in ("a", "b", "c", "d", "e", "f")):
            raise ValueError("affine transform is incomplete")
    else:
        # Legacy diagonal format.
        if not all(k in t for k in ("ax", "bx", "ay", "by")):
            raise ValueError("diagonal transform is incomplete")
        if t["ax"] == 0 or t["ay"] == 0:
            raise ValueError("calibration transform is degenerate")


def transform_is_legacy(cal: dict) -> bool:
    return "a" not in cal.get("transform", {})


def _transform_xy(cal: dict, rx: int, ry: int) -> Tuple[float, float]:
    """Apply the transform, supporting old diagonal and new affine formats."""
    t = cal["transform"]
    if "a" in t:
        return (t["a"] * rx + t["b"] * ry + t["c"],
                t["d"] * rx + t["e"] * ry + t["f"])
    return (t["ax"] * rx + t["bx"],
            t["ay"] * ry + t["by"])


# --------------------------------------------------------------------------- #
# Runtime mapping                                                            #
# --------------------------------------------------------------------------- #

def _pressure(pmin: int, pmax: int, pressure: int) -> float:
    if pmax <= pmin:
        return 1.0
    p = (pressure - pmin) / (pmax - pmin)
    return max(0.0, min(1.0, p))


def raw_to_canvas(
    cal: dict,
    rx: int,
    ry: int,
    pressure: int,
    canvas_w: int,
    canvas_h: int,
) -> Tuple[float, float, float]:
    """Convert a raw stylus sample to canvas coords and a [0..1] pressure."""
    xu, yu = _transform_xy(cal, rx, ry)

    panel = cal.get("panel", {"width": canvas_w, "height": canvas_h})
    pw = panel["width"] or canvas_w
    ph = panel["height"] or canvas_h

    x = xu * (canvas_w / pw)
    y = yu * (canvas_h / ph)

    pmin = cal["pressure"]["min"]
    pmax = cal["pressure"]["max"]
    return x, y, _pressure(pmin, pmax, pressure)


def raw_to_target(
    cal: dict,
    rx: int,
    ry: int,
    pressure: int,
    target_w: int,
    target_h: int,
) -> Tuple[float, float, float]:
    """Map raw stylus coords onto a target of arbitrary size, preserving the
    panel's aspect ratio (letterboxing the drawing area inside the target).
    """
    xu, yu = _transform_xy(cal, rx, ry)

    panel = cal.get("panel", {"width": target_w, "height": target_h})
    pw = panel["width"] or target_w
    ph = panel["height"] or target_h

    nx = max(0.0, min(1.0, xu / pw))
    ny = max(0.0, min(1.0, yu / ph))

    aspect_panel = pw / ph
    aspect_target = target_w / target_h
    if aspect_panel < aspect_target:
        draw_h = target_h
        draw_w = max(1, int(draw_h * aspect_panel))
    else:
        draw_w = target_w
        draw_h = max(1, int(draw_w / aspect_panel))
    off_x = (target_w - draw_w) // 2
    off_y = (target_h - draw_h) // 2

    x = off_x + nx * draw_w
    y = off_y + ny * draw_h

    pmin = cal["pressure"]["min"]
    pmax = cal["pressure"]["max"]
    return x, y, _pressure(pmin, pmax, pressure)