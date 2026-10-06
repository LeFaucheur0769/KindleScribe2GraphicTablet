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
    def __init__(self, width: int, height: int, bg=(255, 255, 255)) -> None:
        self.width = width
        self.height = height
        self.bg = bg
        self.pages: List[Canvas] = [Canvas(width, height, bg)]
        self.idx = 0
        self._listeners: List[Callable[[], None]] = []

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
        self.pages.append(Canvas(self.width, self.height, self.bg))
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