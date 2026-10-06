"""Map raw stylus coordinates to canvas coordinates.

Three-corner (TL / TR / BL) calibration, mirroring the reference tool. The
result is a JSON file that also records the stylus device, the event struct
size and the useful pressure range.
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

    # Bold border so you can see "something" is on the panel even from across
    # the room, and so we can tell if the draw is landing at the right size.
    border = 20
    for i in range(border):
        d.rectangle([i, i, w - 1 - i, h - 1 - i], outline=(0, 0, 0))

    # Chunky crosshair.
    r_outer = 90
    r_inner = 60
    d.ellipse([tx - r_outer, ty - r_outer, tx + r_outer, ty + r_outer],
              outline=(0, 0, 0), width=10)
    d.ellipse([tx - r_inner, ty - r_inner, tx + r_inner, ty + r_inner],
              outline=(0, 0, 0), width=4)
    d.line([tx - r_outer, ty, tx + r_outer, ty], fill=(0, 0, 0), width=8)
    d.line([tx, ty - r_outer, tx, ty + r_outer], fill=(0, 0, 0), width=8)
    d.ellipse([tx - 12, ty - 12, tx + 12, ty + 12], fill=(0, 0, 0))

    # Label far from the crosshair so it doesn't cover it.
    lx = tx + r_outer + 30
    ly = ty - 20
    if lx + 200 > w:
        lx = tx - r_outer - 230
    d.text((lx, ly), label, fill=(0, 0, 0))
    d.rectangle([lx - 10, ly - 10, lx + 180, ly + 20],
                outline=(0, 0, 0), width=2)

    # Big instructions.
    d.text((w // 2 - 300, 60),
           "Tap the centre of the black dot with the stylus.",
           fill=(0, 0, 0))
    return img


def _wait_for_tap(kindle, dev: str, esz: int, timeout: float = 120.0
                  ) -> Tuple[int, int, int]:
    """Block until the stylus produces a ``down`` event, then return its raw
    coordinates and the pressure sample at that instant."""
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
# Calibration driver                                                         #
# --------------------------------------------------------------------------- #

def calibrate(kindle, panel_w: int, panel_h: int, dev: str, esz: int
              ) -> list:
    """Prompt the user to tap three targets and return the sample list."""
    targets = [
        (120, 120, "TL"),
        (panel_w - 120, 120, "TR"),
        (120, panel_h - 120, "BL"),
    ]
    samples = []
    for tx, ty, name in targets:
        img = _make_target_image(panel_w, panel_h, tx, ty, name)
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
    """Solve an axis-aligned affine transform from raw stylus coordinates to
    panel pixel coordinates.

    ``samples`` must contain TL, TR and BL (in that order).
    """
    if len(samples) < 3:
        raise ValueError("need at least 3 calibration samples")
    tl, tr, bl = samples[0], samples[1], samples[2]

    dx_raw = tr["raw"][0] - tl["raw"][0]
    dy_raw = bl["raw"][1] - tl["raw"][1]
    if dx_raw == 0 or dy_raw == 0:
        raise ValueError("degenerate calibration (identical raw coordinates)")

    ax = (tr["canvas"][0] - tl["canvas"][0]) / dx_raw
    bx = tl["canvas"][0] - ax * tl["raw"][0]
    ay = (bl["canvas"][1] - tl["canvas"][1]) / dy_raw
    by = tl["canvas"][1] - ay * tl["raw"][1]
    return {"ax": ax, "bx": bx, "ay": ay, "by": by}


def save_calibration(path: str, data: dict) -> None:
    p = os.path.expanduser(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


def load_calibration(path: str) -> dict:
    p = os.path.expanduser(path)
    with open(p, "r") as f:
        return json.load(f)


def validate_calibration(cal: dict) -> None:
    t = cal.get("transform")
    if not t:
        raise ValueError("calibration missing 'transform'")
    if not all(k in t for k in ("ax", "bx", "ay", "by")):
        raise ValueError("calibration transform is incomplete")
    if t["ax"] == 0 or t["ay"] == 0:
        raise ValueError("calibration transform is degenerate")


# --------------------------------------------------------------------------- #
# Runtime mapping                                                            #
# --------------------------------------------------------------------------- #

def raw_to_canvas(
    cal: dict,
    rx: int,
    ry: int,
    pressure: int,
    canvas_w: int,
    canvas_h: int,
) -> Tuple[float, float, float]:
    """Convert a raw stylus sample to canvas coordinates and a [0..1] pressure."""
    t = cal["transform"]
    x_panel = t["ax"] * rx + t["bx"]
    y_panel = t["ay"] * ry + t["by"]

    panel = cal.get("panel", {"width": canvas_w, "height": canvas_h})
    pw = panel["width"] or canvas_w
    ph = panel["height"] or canvas_h

    x = x_panel * (canvas_w / pw)
    y = y_panel * (canvas_h / ph)

    pmin = cal["pressure"]["min"]
    pmax = cal["pressure"]["max"]
    if pmax <= pmin:
        p = 1.0
    else:
        p = (pressure - pmin) / (pmax - pmin)
        p = max(0.0, min(1.0, p))
    return x, y, p