"""Multi-page support.

A :class:`PageManager` is itself the "canvas provider" handed to the streamer
and injector: it always returns the currently-selected :class:`Canvas`.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, List

from .canvas import Canvas

log = logging.getLogger(__name__)


class PageManager:
    def __init__(
        self,
        width: int,
        height: int,
        bg=(255, 255, 255),
        max_scale: float = 4.0,
        resolution: int = 1,
    ) -> None:
        """Create a manager whose pages all share the canvas size and pinch
        zoom limits.

        ``max_scale`` and ``resolution`` are forwarded to every
        :class:`Canvas` so each page gets its own viewport with the same zoom
        ceiling and the same panel-relative resolution (used for pen-width
        scaling; see :func:`kindle_draw.canvas.pen_zoom_factor`).  A fresh
        page always starts at the identity viewport: scale 1, offset 0.
        """
        self.width = width
        self.height = height
        self.bg = bg
        self.max_scale = max_scale
        self.resolution = max(1, int(resolution))
        self.pages: List[Canvas] = [self._make_canvas()]
        self.idx = 0
        self._listeners: List[Callable[[], None]] = []

    def _make_canvas(self) -> Canvas:
        return Canvas(self.width, self.height, self.bg,
                      max_scale=self.max_scale, resolution=self.resolution)

    # -- listener plumbing --------------------------------------------- #

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                log.exception("page-change listener failed")

    # -- provider API --------------------------------------------------- #

    def current(self) -> Canvas:
        return self.pages[self.idx]

    # -- navigation ----------------------------------------------------- #

    def new_page(self) -> Canvas:
        self.pages.append(self._make_canvas())
        self.idx = len(self.pages) - 1
        self._notify()
        return self.current()

    def next_page(self) -> Canvas:
        if self.idx + 1 < len(self.pages):
            self.idx += 1
            self._notify()
        else:
            self.new_page()
        return self.current()

    def prev_page(self) -> Canvas:
        if self.idx > 0:
            self.idx -= 1
            self._notify()
        return self.current()

    def goto_page(self, n: int) -> Canvas:
        if 0 <= n < len(self.pages):
            self.idx = n
            self._notify()
        return self.current()

    # -- export --------------------------------------------------------- #

    def save_all_png(self, directory: str) -> List[str]:
        os.makedirs(directory, exist_ok=True)
        paths = []
        for i, c in enumerate(self.pages):
            p = os.path.join(directory, f"page_{i + 1:03d}.png")
            c.save_png(p)
            paths.append(p)
        return paths

    def save_all_pdf(self, path: str, dpi: int = 300) -> None:
        imgs = [c.snapshot().convert("RGB") for c in self.pages]
        if not imgs:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        imgs[0].save(path, "PDF", save_all=True, append_images=imgs[1:],
                     resolution=dpi)