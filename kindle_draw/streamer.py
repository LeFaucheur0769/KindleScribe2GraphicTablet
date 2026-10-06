"""Push the host canvas to the Kindle panel via FBInk partial updates.

The streamer runs on its own thread. It watches a canvas provider for version
changes, computes the bounding box of the changed pixels, crops that region
out of a Kindle-sized preview, encodes it as PNG and hands it to FBInk with an
adaptive waveform (DU while strokes are live, GL16/GC16 once they settle).
"""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Callable, Optional, Tuple

from PIL import Image, ImageChops

from .config import StreamConfig

log = logging.getLogger(__name__)

Box = Tuple[int, int, int, int]


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
    ) -> None:
        super().__init__(daemon=True, name="Streamer")
        self.kindle = kindle
        self.canvas_provider = canvas_provider
        self.cfg = cfg
        self.stop_event = stop_event

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
        """Force a full refresh on the next tick (e.g. page change)."""
        self._reset.set()
        self._wake.set()

    # -- main loop ------------------------------------------------------ #

    def run(self) -> None:
        log.info("streamer started (fps=%.1f)", self.cfg.fps)
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
                log.warning("streamer tick failed (%d): %s", self._error_count, exc)
                time.sleep(min(2.0, 0.2 * self._error_count))
                continue
            dt = time.monotonic() - t0
            if dt < min_dt:
                time.sleep(min_dt - dt)
        log.info("streamer stopped")

    # -- tick ----------------------------------------------------------- #

    def _tick(self) -> None:
        if self._panel_w is None or self._panel_h is None:
            return

        canvas = self.canvas_provider()
        if canvas is None:
            return

        if self._reset.is_set():
            self._reset.clear()
            self._last = None
            self._dirty_union = None
            self._clean_pending = False
            self._last_version = -1

        v = getattr(canvas, "version", 0)
        if self._last is not None and v == self._last_version:
            # Nothing changed — maybe run a clean pass.
            if (self._clean_pending
                    and (time.time() - self._last_activity) >= self.cfg.settle):
                self._send_clean(self._last)
                self._clean_pending = False
            return
        self._last_version = v

        preview = canvas.render_preview(self._panel_w, self._panel_h, self._aspect)

        if self._last is None or self._last.size != preview.size:
            self._send_full(preview)
            self._last = preview
            self._dirty_union = (0, 0, self._panel_w, self._panel_h)
            self._clean_pending = True
            self._last_activity = time.time()
            return

        diff = ImageChops.difference(preview, self._last)
        bbox = _diff_bbox(diff, self.cfg.threshold)
        if bbox is None:
            self._last = preview
            return

        bbox = snap_box(bbox, self.cfg.align, self._panel_w, self._panel_h)
        bbox = clamp_box(bbox, self._panel_w, self._panel_h)
        if bbox is None:
            self._last = preview
            return

        self._send_region(preview, bbox, wf=self.cfg.active_wf)
        self._last = preview
        self._dirty_union = union(self._dirty_union, bbox)
        self._clean_pending = True
        self._last_activity = time.time()

    # -- FBInk plumbing ------------------------------------------------- #

    def _send_full(self, preview: Image.Image) -> None:
        buf = io.BytesIO()
        preview.save(buf, "PNG", compress_level=1)
        log.debug("streamer: full refresh %dx%d", preview.size[0], preview.size[1])
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

    def _send_clean(self, preview: Image.Image) -> None:
        box = self._dirty_union or (0, 0, self._panel_w, self._panel_h)
        box = clamp_box(box, self._panel_w, self._panel_h)
        if box is None:
            return
        self._send_region(preview, box, wf=self.cfg.clean_wf)
        log.debug("streamer: clean pass %s", box)