"""Persist strokes as PNG / PDF / JSON."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


class StrokeLogger:
    def __init__(
        self,
        canvas_provider: Callable,
        png_path: Optional[str] = None,
        pdf_path: Optional[str] = None,
        json_path: Optional[str] = None,
        autosave_interval: float = 0.0,
    ) -> None:
        self.canvas_provider = canvas_provider
        self.png_path = png_path
        self.pdf_path = pdf_path
        self.json_path = json_path
        self.autosave_interval = autosave_interval

        self._strokes: List[dict] = []
        self._current: Optional[dict] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- recording ------------------------------------------------------ #

    def begin(self, x: float, y: float, p: float, eraser: bool = False) -> None:
        with self._lock:
            self._current = {
                "points": [[float(x), float(y), float(p), time.time()]],
                "eraser": bool(eraser),
            }

    def point(self, x: float, y: float, p: float) -> None:
        with self._lock:
            if self._current is not None:
                self._current["points"].append([float(x), float(y), float(p), time.time()])

    def end(self, x: float, y: float, p: float) -> None:
        with self._lock:
            if self._current is None:
                return
            self._current["points"].append([float(x), float(y), float(p), time.time()])
            self._strokes.append(self._current)
            self._current = None

    # -- persistence ---------------------------------------------------- #

    def save_png(self, path: Optional[str] = None) -> None:
        path = path or self.png_path
        if not path:
            return
        c = self.canvas_provider()
        if c is None:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        c.save_png(path)
        log.info("saved PNG: %s", path)

    def save_pdf(self, path: Optional[str] = None) -> None:
        path = path or self.pdf_path
        if not path:
            return
        c = self.canvas_provider()
        if c is None:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        c.save_pdf(path)
        log.info("saved PDF: %s", path)

    def save_json(self, path: Optional[str] = None) -> None:
        path = path or self.json_path
        if not path:
            return
        with self._lock:
            data = {"strokes": list(self._strokes)}
            if self._current is not None:
                data["strokes"].append(self._current)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        log.info("saved strokes JSON: %s", path)

    def save_all(self) -> None:
        self.save_png()
        self.save_pdf()
        self.save_json()

    # -- autosave ------------------------------------------------------- #

    def start(self) -> None:
        if self.autosave_interval <= 0:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="Autosave")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.autosave_interval):
            try:
                self.save_all()
            except Exception:
                log.exception("autosave failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)