"""Host-side drawing surface backed by a Pillow ``Image``.

The canvas owns the "truth" of the drawing and is the source the streamer
pushes to the Kindle. A monotonically increasing ``version`` counter lets the
streamer skip work when nothing has changed.
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

from PIL import Image, ImageDraw

from .gestures import GestureEvent, Viewport

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
    """A fixed-size drawing surface with a zoomable/pannable viewport.

    The canvas image itself is always full resolution (``width x height``);
    the :class:`~kindle_draw.gestures.Viewport` decides which *region* of it
    ``render_preview`` maps onto the Kindle panel.  Stylus coordinates are
    converted from view space to canvas space with :meth:`view_to_canvas`
    before drawing, so you always draw exactly under your pen tip even while
    zoomed in.  Brush sizes stay in canvas units (a 8 px pen draws 8 canvas
    pixels regardless of the zoom level, which means it looks *thicker* on
    screen when zoomed in -- like magnifying paper).
    """

    def __init__(self, width: int, height: int, bg=(255, 255, 255),
                 min_scale: float = 1.0, max_scale: float = 8.0) -> None:
        self.width = width
        self.height = height
        self.bg = bg
        self.image = Image.new("RGB", (width, height), bg)
        self.lock = threading.RLock()
        self._draw = ImageDraw.Draw(self.image)

        # Zoom/pan window onto the canvas (see gestures.py for the math).
        self.viewport = Viewport(width, height, min_scale=min_scale,
                                 max_scale=max_scale)

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

    # -- viewport / gestures -------------------------------------------- #
    #
    # View coordinates are canvas-normalised: the visible window always maps
    # onto [0, width] x [0, height] regardless of the zoom level (see the
    # module docstring in gestures.py).  Gesture mutators bump ``version`` so
    # the streamer re-renders the preview; they deliberately do *not* touch
    # ``_dirty`` because canvas-mode updates are version-triggered and diffed
    # from the rendered preview, not from the dirty box.

    def apply_gesture(self, ev: GestureEvent) -> bool:
        """Apply a recognised pinch/pan gesture (view units).

        Returns True when the viewport actually changed (and ``version`` was
        bumped).  A pinch event carries its focal point and pan delta in one
        composition, so simultaneous zoom+drag stays anchored under the
        fingers.
        """
        with self.lock:
            if self.viewport.apply_pinch(ev.factor, ev.focal_x, ev.focal_y,
                                         ev.dx, ev.dy):
                self.version += 1
                return True
            return False

    def zoom_at(self, factor: float, cx: float, cy: float) -> bool:
        """Zoom around a *view-space* point. Bumps version when changed."""
        with self.lock:
            if self.viewport.apply_pinch(factor, cx, cy):
                self.version += 1
                return True
            return False

    def pan_by(self, dx: float, dy: float) -> bool:
        """Pan the window by a *view-space* delta. Bumps version if changed."""
        with self.lock:
            if self.viewport.apply_pan(dx, dy):
                self.version += 1
                return True
            return False

    def reset_viewport(self) -> bool:
        """Return to scale 1 / offset 0 (whole canvas visible)."""
        with self.lock:
            if self.viewport.reset():
                self.version += 1
                return True
            return False

    @property
    def viewport_identity(self) -> bool:
        """True when the whole canvas is visible at scale 1 (no zoom/pan)."""
        with self.lock:
            return self.viewport.is_identity()

    def view_to_canvas(self, vx: float, vy: float) -> Tuple[float, float]:
        """Map a view-space point (pen tip on screen) to canvas pixels."""
        with self.lock:
            return self.viewport.view_to_canvas(vx, vy)

    def canvas_to_view(self, cx: float, cy: float) -> Tuple[float, float]:
        """Map canvas pixels to view space (may fall outside the window)."""
        with self.lock:
            return self.viewport.canvas_to_view(cx, cy)

    def canvas_to_panel(self, cx: float, cy: float,
                        pw: int, ph: int) -> Tuple[float, float]:
        """Map canvas pixels to panel pixels through the current viewport.

        Points outside the visible window map outside the panel; callers
        should clamp to the panel bounds when drawing cursors.
        """
        with self.lock:
            return self.viewport.canvas_to_panel(cx, cy, pw, ph)

    def viewport_box(self) -> Box:
        """The visible window in integer canvas pixels (x0, y0, x1, y1)."""
        with self.lock:
            return self.viewport.viewport_box()

    # -- I/O ------------------------------------------------------------ #

    def save_png(self, path: str) -> None:
        with self.lock:
            self.image.save(path, "PNG")

    def save_pdf(self, path: str, dpi: int = 300) -> None:
        with self.lock:
            self.image.convert("RGB").save(path, "PDF", resolution=dpi)

    # -- preview -------------------------------------------------------- #

    def render_preview(self, pw: int, ph: int, mode: str = "fit") -> Image.Image:
        """Render the visible viewport window as a panel-sized grayscale image.

        With an identity viewport this is the whole canvas scaled to the panel
        (the classic behaviour).  With a zoomed/panned viewport only the
        window returned by :meth:`viewport_box` is rendered, stretched to fill
        the panel exactly -- matching ``Viewport.canvas_to_panel`` mapping, so
        a pen tip at view ``(vx, vy)`` lands at panel pixel
        ``(vx * pw / width, vy * ph / height)``.  The ``mode`` aspect
        handling then applies to the cropped window (relevant only when the
        canvas and panel aspect ratios differ).
        """
        with self.lock:
            img = self.image
            if not self.viewport.is_identity():
                img = img.crop(self.viewport.viewport_box())
            if (pw, ph) == img.size and mode == "fit":
                return img.convert("L")             # was: .copy()
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
