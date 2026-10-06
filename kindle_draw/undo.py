"""Undo / redo via canvas snapshots.

Memory-bounded: only the last ``max_size`` snapshots are kept.
"""

from __future__ import annotations

import threading
from typing import List

from .canvas import Canvas


class UndoStack:
    def __init__(self, max_size: int = 30) -> None:
        self.max_size = max_size
        self._undo: List = []
        self._redo: List = []
        self._lock = threading.Lock()

    def snapshot(self, canvas: Canvas) -> None:
        with self._lock:
            self._undo.append(canvas.snapshot())
            if len(self._undo) > self.max_size:
                self._undo.pop(0)
            self._redo.clear()

    def undo(self, canvas: Canvas) -> bool:
        with self._lock:
            if not self._undo:
                return False
            self._redo.append(canvas.snapshot())
            img = self._undo.pop()
            canvas.replace_image(img)
            return True

    def redo(self, canvas: Canvas) -> bool:
        with self._lock:
            if not self._redo:
                return False
            self._undo.append(canvas.snapshot())
            img = self._redo.pop()
            canvas.replace_image(img)
            return True