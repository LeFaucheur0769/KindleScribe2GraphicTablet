"""Host-side drawing surface backed by a Pillow ``Image``.

The canvas owns the "truth" of the drawing and is the source the streamer
pushes to the Kindle. A monotonically increasing ``version`` counter lets the
streamer skip work when nothing has changed.
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

from PIL import Image, ImageDraw

try:
    LANCZOS = Image.Resampling.LANCZOS  # Pillow ≥ 9.1
except AttributeError:                    # pragma: no cover
    LANCZOS = Image.LANCZOS

Box = Tuple[int, int, int, int]


# --------------------------------------------------------------------------- #
# Brushes                                                                    #
# --------------------------------------------------------------------------- #

class Brush:
    """Pressure-modulated round brush."""

    def __init__(
        self,
        color: Tuple[int, int, int] = (0, 0, 0),
        base: float = 1.5,
        gain: float = 10.0,
        min_w: float = 0.5,
        max_w: float = 32.0,
    ) -> None:
        self.color = color
        self.base = base
        self.gain = gain
        self.min_w = min_w
        self.max_w = max_w

    def width_at(self, pressure: float) -> float:
        w = self.base + self.gain * max(0.0, min(1.0, pressure))
        return max(self.min_w, min(self.max_w, w))


PEN = Brush(color=(0, 0, 0), base=1.5, gain=10.0, min_w=0.6, max_w=28.0)
HIGHLIGHTER = Brush(color=(255, 224, 64), base=12.0, gain=10.0, min_w=10.0, max_w=48.0)
ERASER = Brush(color=(255, 255, 255), base=12.0, gain=24.0, min_w=10.0, max_w=64.0)


# --------------------------------------------------------------------------- #
# Canvas                                                                     #
# --------------------------------------------------------------------------- #

class Canvas:
    def __init__(self, width: int, height: int, bg=(255, 255, 255)) -> None:
        self.width = width
        self.height = height
        self.bg = bg
        self.image = Image.new("RGB", (width, height), bg)
        self.lock = threading.RLock()
        self._draw = ImageDraw.Draw(self.image)
        self.brush: Brush = PEN
        self._dirty: Optional[Box] = None
        self.version: int = 0

    # -- dirty tracking -------------------------------------------------- #

    def _mark(self, bbox: Box) -> None:
        self.version += 1
        if self._dirty is None:
            self._dirty = bbox
            return
        d = self._dirty
        self._dirty = (
            min(d[0], bbox[0]),
            min(d[1], bbox[1]),
            max(d[2], bbox[2]),
            max(d[3], bbox[3]),
        )

    def take_dirty(self) -> Optional[Box]:
        with self.lock:
            d = self._dirty
            self._dirty = None
            return d

    # -- drawing -------------------------------------------------------- #

    def set_brush(self, brush: Brush) -> None:
        self.brush = brush

    def draw_segment(self, x0: float, y0: float, x1: float, y1: float,
                     pressure: float) -> None:
        b = self.brush
        w = b.width_at(pressure)
        r = w / 2.0
        with self.lock:
            self._draw.line([(x0, y0), (x1, y1)], fill=b.color,
                            width=max(1, int(round(w))))
            # round caps
            self._draw.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=b.color)
            self._draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=b.color)
            x_lo = int(min(x0, x1) - r - 2)
            y_lo = int(min(y0, y1) - r - 2)
            x_hi = int(max(x0, x1) + r + 2)
            y_hi = int(max(y0, y1) + r + 2)
            self._mark((x_lo, y_lo, x_hi, y_hi))

    def draw_dot(self, x: float, y: float, pressure: float) -> None:
        b = self.brush
        w = b.width_at(pressure)
        r = w / 2.0
        with self.lock:
            self._draw.ellipse([x - r, y - r, x + r, y + r], fill=b.color)
            self._mark((int(x - r - 2), int(y - r - 2),
                        int(x + r + 2), int(y + r + 2)))

    def clear(self) -> None:
        with self.lock:
            self._draw.rectangle([0, 0, self.width, self.height], fill=self.bg)
            self._mark((0, 0, self.width, self.height))

    # -- state ---------------------------------------------------------- #

    def snapshot(self) -> Image.Image:
        with self.lock:
            return self.image.copy()

    def replace_image(self, img: Image.Image) -> None:
        with self.lock:
            self.image = img.convert("RGB").copy()
            self._draw = ImageDraw.Draw(self.image)
            self._mark((0, 0, self.width, self.height))

    # -- I/O ------------------------------------------------------------ #

    def save_png(self, path: str) -> None:
        with self.lock:
            self.image.save(path, "PNG")

    def save_pdf(self, path: str, dpi: int = 300) -> None:
        with self.lock:
            self.image.convert("RGB").save(path, "PDF", resolution=dpi)

    # -- preview -------------------------------------------------------- #

    def render_preview(self, pw: int, ph: int, mode: str = "fit") -> Image.Image:
        with self.lock:
            img = self.image
            iw, ih = img.size
            if mode == "stretch":
                return img.resize((pw, ph), LANCZOS)
            if mode == "crop":
                s = max(pw / iw, ph / ih)
                nw, nh = int(round(iw * s)), int(round(ih * s))
                r = img.resize((nw, nh), LANCZOS)
                l = (nw - pw) // 2
                t = (nh - ph) // 2
                return r.crop((l, t, l + pw, t + ph))
            # "fit"
            s = min(pw / iw, ph / ih)
            nw, nh = int(round(iw * s)), int(round(ih * s))
            r = img.resize((nw, nh), LANCZOS)
            out = Image.new("RGB", (pw, ph), (255, 255, 255))
            out.paste(r, ((pw - nw) // 2, (ph - nh) // 2))
            return out