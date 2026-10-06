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
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:                    # pragma: no cover
    LANCZOS = Image.LANCZOS
try:
    BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    BILINEAR = Image.BILINEAR

Box = Tuple[int, int, int, int]

# Default palette exposed to the GUI. Colours are chosen to look good on
# e-ink (they end up quantised to 16 greys, so saturated hues survive as
# dark/mid/light bands rather than as hue).
PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (0, 0, 0),         # black
    (80, 80, 80),      # dark grey
    (150, 150, 150),   # mid grey
    (220, 50, 47),     # red
    (60, 170, 60),     # green
    (50, 80, 220),     # blue
    (245, 130, 32),    # orange
    (150, 60, 200),    # purple
    (240, 120, 180),   # pink
    (255, 200, 0),     # yellow
)


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
        self.color = tuple(color)
        self.base = base
        self.gain = gain
        self.min_w = min_w
        self.max_w = max_w

    def width_at(self, pressure: float) -> float:
        w = self.base + self.gain * max(0.0, min(1.0, pressure))
        return max(self.min_w, min(self.max_w, w))


def make_brush(color, size: float, pressure_sensitive: bool = True) -> Brush:
    """Build a brush whose MAX width is ``size`` px.

    With pressure sensitivity: width ramps from size*0.2 to size.
    Without: constant width == size.
    """
    size = max(0.5, float(size))
    if pressure_sensitive:
        return Brush(
            color=color,
            base=size * 0.2,
            gain=size * 0.8,
            min_w=max(0.5, size * 0.15),
            max_w=size,
        )
    return Brush(color=color, base=size, gain=0.0, min_w=size, max_w=size)


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

        # user-tunable brush state
        self._pen_color: Tuple[int, int, int] = (0, 0, 0)
        self._pen_size: float = 8.0
        self._eraser_size: float = 48.0
        self._pressure_sensitive: bool = True

        # current brush (pen or eraser depending on last input event)
        self._is_eraser: bool = False
        self.brush: Brush = self._pen_brush()

        self._dirty: Optional[Box] = None
        self.version: int = 0

    # -- brush construction --------------------------------------------- #

    def _pen_brush(self) -> Brush:
        return make_brush(self._pen_color, self._pen_size,
                          self._pressure_sensitive)

    def _eraser_brush(self) -> Brush:
        # Eraser paints with the background colour. That works for the common
        # white-background case and degrades gracefully on tinted backgrounds.
        return make_brush(self.bg, self._eraser_size,
                          self._pressure_sensitive)

    # -- public brush API ------------------------------------------------ #

    def set_brush_for(self, eraser: bool) -> None:
        """Select the pen or eraser brush. Called by the input reader."""
        self._is_eraser = bool(eraser)
        self.brush = self._eraser_brush() if eraser else self._pen_brush()

    def set_pen_color(self, color: Tuple[int, int, int]) -> None:
        self._pen_color = tuple(color)
        if not self._is_eraser:
            self.brush = self._pen_brush()

    def set_pen_size(self, size: float) -> None:
        self._pen_size = max(0.5, float(size))
        if not self._is_eraser:
            self.brush = self._pen_brush()

    def set_eraser_size(self, size: float) -> None:
        self._eraser_size = max(0.5, float(size))
        if self._is_eraser:
            self.brush = self._eraser_brush()

    def set_pressure_sensitive(self, on: bool) -> None:
        self._pressure_sensitive = bool(on)
        self.brush = (self._eraser_brush() if self._is_eraser
                      else self._pen_brush())

    @property
    def pen_color(self) -> Tuple[int, int, int]:
        return self._pen_color

    @property
    def pen_size(self) -> float:
        return self._pen_size

    @property
    def eraser_size(self) -> float:
        return self._eraser_size

    @property
    def pressure_sensitive(self) -> bool:
        return self._pressure_sensitive

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
        """Direct brush override (advanced use; prefer set_* methods)."""
        self.brush = brush

    def draw_segment(self, x0: float, y0: float, x1: float, y1: float,
                     pressure: float) -> None:
        b = self.brush
        w = b.width_at(pressure)
        r = w / 2.0
        with self.lock:
            self._draw.line([(x0, y0), (x1, y1)], fill=b.color,
                            width=max(1, int(round(w))))
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
            if (pw, ph) == self.image.size and mode == "fit":
                return self.image.convert("L")     # was: .copy()
            img = self.image
            iw, ih = img.size
            if mode == "stretch":
                return img.resize((pw, ph), BILINEAR).convert("L")
            if mode == "crop":
                s = max(pw / iw, ph / ih)
                nw, nh = int(round(iw * s)), int(round(ih * s))
                r = img.resize((nw, nh), BILINEAR)
                l = (nw - pw) // 2
                t = (nh - ph) // 2
                return r.crop((l, t, l + pw, t + ph)).convert("L")
            s = min(pw / iw, ph / ih)
            nw, nh = int(round(iw * s)), int(round(ih * s))
            r = img.resize((nw, nh), BILINEAR)
            out = Image.new("L", (pw, ph), 255)    # was "RGB", (255,255,255)
            out.paste(r, ((pw - nw) // 2, (ph - nh) // 2))
            return out
