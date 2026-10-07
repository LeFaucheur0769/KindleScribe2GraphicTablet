"""Push host content to the Kindle panel via FBInk.

Two modes:

- *Canvas mode* (default): diffs a host-side Pillow canvas against the last
  frame sent, and pushes the changed region.
- *Cursor-only mode* (``cursor_only=True``): renders a plain white panel
  and tracks a small reticle at the pen's position. Used when the Scribe
  is acting as a system tablet (``--inject uinput``) so you get a visual
  aim point on the Kindle itself.

An optional hover cursor can be composited onto either mode.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Callable, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw

from .config import StreamConfig

log = logging.getLogger(__name__)

Box = Tuple[int, int, int, int]

CURSOR_RADIUS = 14
CURSOR_OUTLINE = 2
CURSOR_HALO = 2


# --------------------------------------------------------------------------- #
# Geometry helpers                                                           #
# --------------------------------------------------------------------------- #

def union(a: Optional[Box], b: Optional[Box]) -> Optional[Box]:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def snap_box(box: Box, align: int, w: int, h: int) -> Box:
    x0, y0, x1, y1 = box
    if align > 1:
        x0 = (x0 // align) * align
        y0 = (y0 // align) * align
        x1 = ((x1 + align - 1) // align) * align
        y1 = ((y1 + align - 1) // align) * align
    return (x0, y0, x1, y1)


def clamp_box(box: Box, w: int, h: int) -> Optional[Box]:
    x0, y0, x1, y1 = box
    x0 = max(0, min(w, x0))
    y0 = max(0, min(h, y0))
    x1 = max(0, min(w, x1))
    y1 = max(0, min(h, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _diff_bbox(diff: Image.Image, threshold: int) -> Optional[Box]:
    if threshold > 0:
        gray = diff.convert("L")
        mask = gray.point(lambda v: 255 if v >= threshold else 0)
        return mask.getbbox()
    return diff.getbbox()


def _cursor_bbox(x: float, y: float) -> Box:
    r = CURSOR_RADIUS + CURSOR_OUTLINE + CURSOR_HALO + 4
    return (int(x - r), int(y - r), int(x + r), int(y + r))


def _draw_cursor(img: Image.Image, x: float, y: float,
                 color: Tuple[int, int, int] = (0, 0, 0)) -> None:
    """Draw a small aiming reticle with a white halo so it reads over ink.

    Picks scalar vs tuple colour depending on image mode: the streamed
    preview is 8-bit grayscale (``L``), not RGB.
    """
    if img.mode == "L":
        fg = 0
        bg = 255
    else:
        fg = color
        bg = (255, 255, 255)

    d = ImageDraw.Draw(img)
    r = CURSOR_RADIUS
    halo = CURSOR_HALO
    d.ellipse([x - r - halo, y - r - halo, x + r + halo, y + r + halo],
              fill=bg)
    d.ellipse([x - r, y - r, x + r, y + r],
              outline=fg, width=CURSOR_OUTLINE)
    d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=fg)


# --------------------------------------------------------------------------- #
# Streamer                                                                   #
# --------------------------------------------------------------------------- #

class Streamer(threading.Thread):
    def __init__(
        self,
        kindle,
        canvas_provider: Callable,
        cfg: StreamConfig,
        stop_event: threading.Event,
        cursor_enabled: bool = False,
        cursor_only: bool = False,
    ) -> None:
        super().__init__(daemon=True, name="Streamer")
        self.kindle = kindle
        self.canvas_provider = canvas_provider
        self.cfg = cfg
        self.stop_event = stop_event
        self.cursor_enabled = cursor_enabled
        self.cursor_only = cursor_only

        self._wake = threading.Event()
        self._reset = threading.Event()

        self._panel_w: Optional[int] = None
        self._panel_h: Optional[int] = None
        self._aspect = "fit"

        self._last: Optional[Image.Image] = None
        self._last_version: int = -1
        self._dirty_union: Optional[Box] = None
        self._clean_pending = False
        self._last_activity = time.time()

        # Cursor in canvas coords (canvas mode) or in panel coords
        # (cursor-only mode). Kept separate so we don't mix frames.
        self._cursor: Optional[Tuple[float, float]] = None
        self._cursor_panel: Optional[Tuple[float, float]] = None
        self._cursor_drawn: Optional[Tuple[float, float]] = None
        self._cursor_lock = threading.Lock()

        self._blank: Optional[Image.Image] = None

        self._frame_count = 0
        self._error_count = 0

    # -- public API ----------------------------------------------------- #

    def set_panel(self, w: int, h: int, aspect: str) -> None:
        self._panel_w = w
        self._panel_h = h
        self._aspect = aspect
        self.reset()

    def wake(self) -> None:
        self._wake.set()

    def reset(self) -> None:
        self._reset.set()
        self._wake.set()

    def set_cursor(self, x: Optional[float], y: Optional[float] = None) -> None:
        """Set cursor in *canvas* coordinates. Pass None to hide."""
        with self._cursor_lock:
            if x is None:
                self._cursor = None
            else:
                self._cursor = (float(x), float(y))
        self._wake.set()

    def set_cursor_panel(self, x: Optional[float], y: Optional[float] = None) -> None:
        """Set cursor in *panel* coordinates. Pass None to hide.

        Used by cursor-only mode where there is no intermediate canvas.
        """
        with self._cursor_lock:
            if x is None:
                self._cursor_panel = None
            else:
                self._cursor_panel = (float(x), float(y))
        self._wake.set()

    # -- main loop ------------------------------------------------------ #

    def run(self) -> None:
        log.info("streamer started (fps=%.1f, cursor=%s, cursor_only=%s)",
                 self.cfg.fps,
                 "on" if self.cursor_enabled else "off",
                 self.cursor_only)
        min_dt = 1.0 / max(1.0, self.cfg.fps)
        while not self.stop_event.is_set():
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            if self.stop_event.is_set():
                break
            t0 = time.monotonic()
            try:
                self._tick()
                self._frame_count += 1
                self._error_count = 0
            except Exception as exc:
                self._error_count += 1
                log.warning("streamer tick failed (%d): %s",
                            self._error_count, exc)
                time.sleep(min(2.0, 0.2 * self._error_count))
                continue
            dt = time.monotonic() - t0
            if dt < min_dt:
                time.sleep(min_dt - dt)
        log.info("streamer stopped")

    # -- helpers -------------------------------------------------------- #

    def _snapshot_cursor_canvas(self) -> Optional[Tuple[float, float]]:
        with self._cursor_lock:
            return self._cursor

    def _snapshot_cursor_panel(self) -> Optional[Tuple[float, float]]:
        with self._cursor_lock:
            return self._cursor_panel

    def _cursor_canvas_to_panel(self, pos) -> Optional[Tuple[float, float]]:
        if pos is None:
            return None
        canvas = self.canvas_provider()
        if canvas is None or self._panel_w is None:
            return None
        cx, cy = pos
        px = cx * self._panel_w / max(1, canvas.width)
        py = cy * self._panel_h / max(1, canvas.height)
        return px, py

    # -- dispatch ------------------------------------------------------- #

    def _tick(self) -> None:
        if self._panel_w is None or self._panel_h is None:
            return
        if self.cursor_only:
            self._tick_cursor_only()
        else:
            self._tick_canvas()

    # -- cursor-only mode ---------------------------------------------- #

    def _tick_cursor_only(self) -> None:
        if self._blank is None or self._blank.size != (self._panel_w,
                                                        self._panel_h):
            self._blank = Image.new("L", (self._panel_w, self._panel_h), 255)
            self._reset.set()

        if self._reset.is_set():
            self._reset.clear()
            self._last = None
            self._cursor_drawn = None
            self._dirty_union = None
            self._clean_pending = False

        cur = self._snapshot_cursor_panel() if self.cursor_enabled else None
        cursor_changed = cur != self._cursor_drawn

        if self._last is None:
            img = self._blank.copy()
            if cur is not None:
                _draw_cursor(img, *cur)
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=1)
            self.kindle.draw_png(buf.getvalue(), x=0, y=0,
                                 wf=self.cfg.flash_wf, flash=True,
                                 no_clear=False)
            self._last = self._blank
            self._cursor_drawn = cur
            self._dirty_union = (0, 0, self._panel_w, self._panel_h)
            self._clean_pending = True
            self._last_activity = time.time()
            return

        if not cursor_changed:
            if (self._clean_pending
                    and (time.time() - self._last_activity) >= self.cfg.settle):
                box = clamp_box(self._dirty_union or
                                (0, 0, self._panel_w, self._panel_h),
                                self._panel_w, self._panel_h)
                if box is not None:
                    img = self._blank.copy()
                    if self._cursor_drawn is not None:
                        _draw_cursor(img, *self._cursor_drawn)
                    crop = img.crop(box)
                    buf = io.BytesIO()
                    crop.save(buf, "PNG", compress_level=1)
                    self.kindle.draw_png(buf.getvalue(), x=box[0], y=box[1],
                                         wf=self.cfg.clean_wf, no_clear=True)
                self._clean_pending = False
            return

        dirty = None
        if self._cursor_drawn is not None:
            dirty = union(dirty, _cursor_bbox(*self._cursor_drawn))
        if cur is not None:
            dirty = union(dirty, _cursor_bbox(*cur))

        if dirty is None:
            self._cursor_drawn = cur
            return

        dirty = clamp_box(dirty, self._panel_w, self._panel_h)
        if dirty is None:
            self._cursor_drawn = cur
            return

        img = self._blank.copy()
        if cur is not None:
            _draw_cursor(img, *cur)
        crop = img.crop(dirty)
        buf = io.BytesIO()
        crop.save(buf, "PNG", compress_level=1)
        self.kindle.draw_png(buf.getvalue(), x=dirty[0], y=dirty[1],
                             wf=self.cfg.active_wf, no_clear=True)
        self._last = self._blank
        self._cursor_drawn = cur
        self._dirty_union = union(self._dirty_union, dirty)
        self._clean_pending = True
        self._last_activity = time.time()

    # -- canvas mode ---------------------------------------------------- #

    def _tick_canvas(self) -> None:
        canvas = self.canvas_provider()
        if canvas is None:
            return

        if self._reset.is_set():
            self._reset.clear()
            self._last = None
            self._dirty_union = None
            self._clean_pending = False
            self._last_version = -1
            self._cursor_drawn = None

        v = getattr(canvas, "version", 0)

        cursor_canvas = self._snapshot_cursor_canvas() if self.cursor_enabled else None
        cursor_preview = self._cursor_canvas_to_panel(cursor_canvas)
        cursor_changed = cursor_preview != self._cursor_drawn

        if (self._last is not None
                and v == self._last_version
                and not cursor_changed):
            if (self._clean_pending
                    and (time.time() - self._last_activity) >= self.cfg.settle):
                self._send_clean_canvas(self._last, self._cursor_drawn)
                self._clean_pending = False
            return
        self._last_version = v

        preview = canvas.render_preview(self._panel_w, self._panel_h,
                                        self._aspect)

        if self._last is None or self._last.size != preview.size:
            self._send_full_canvas(preview, cursor_preview)
            self._last = preview
            self._dirty_union = None
            self._clean_pending = False
            self._last_activity = time.time()
            self._cursor_drawn = cursor_preview
            return

        dirty = None

        if v != self._last_version or True:
            diff = ImageChops.difference(preview, self._last)
            bbox = _diff_bbox(diff, self.cfg.threshold)
            if bbox is not None:
                bbox = snap_box(bbox, self.cfg.align,
                                self._panel_w, self._panel_h)
                dirty = union(dirty, bbox)

        if cursor_changed:
            if self._cursor_drawn is not None:
                dirty = union(dirty, _cursor_bbox(*self._cursor_drawn))
            if cursor_preview is not None:
                dirty = union(dirty, _cursor_bbox(*cursor_preview))

        if dirty is None:
            self._last = preview
            self._cursor_drawn = cursor_preview
            return

        dirty = clamp_box(dirty, self._panel_w, self._panel_h)
        if dirty is None:
            self._last = preview
            self._cursor_drawn = cursor_preview
            return

        composited = preview.copy()
        if cursor_preview is not None:
            _draw_cursor(composited, *cursor_preview)

        self._send_region(composited, dirty, wf=self.cfg.active_wf)
        self._last = preview
        self._cursor_drawn = cursor_preview
        self._dirty_union = union(self._dirty_union, dirty)
        self._clean_pending = True
        self._last_activity = time.time()

    def _send_full_canvas(self, preview: Image.Image, cursor) -> None:
        img = preview
        if cursor is not None:
            img = preview.copy()
            _draw_cursor(img, *cursor)
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        log.debug("streamer: full refresh %dx%d", img.size[0], img.size[1])
        self.kindle.draw_png(
            buf.getvalue(), x=0, y=0,
            wf=self.cfg.flash_wf, flash=True, no_clear=False,
        )

    def _send_region(self, preview: Image.Image, bbox: Box, wf: str) -> None:
        crop = preview.crop(bbox)
        buf = io.BytesIO()
        crop.save(buf, "PNG", compress_level=1)
        x0, y0, _x1, _y1 = bbox
        log.debug("streamer: partial refresh %s wf=%s", bbox, wf)
        self.kindle.draw_png(
            buf.getvalue(), x=x0, y=y0, wf=wf, no_clear=True,
        )

    def _send_clean_canvas(self, preview: Image.Image, cursor) -> None:
        box = self._dirty_union or (0, 0, self._panel_w, self._panel_h)
        box = clamp_box(box, self._panel_w, self._panel_h)
        if box is None:
            return
        img = preview
        if cursor is not None:
            img = preview.copy()
            _draw_cursor(img, *cursor)
        self._send_region(img, box, wf=self.cfg.clean_wf)
        log.debug("streamer: clean pass %s", box)