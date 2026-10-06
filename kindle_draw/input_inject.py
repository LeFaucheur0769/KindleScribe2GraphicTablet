"""Inject stylus events into the host system (or directly into the canvas).

Multiple backends are provided so that the tool works on X11, Wayland, or
purely in-memory (``canvas`` mode).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Base                                                                       #
# --------------------------------------------------------------------------- #

class InputInjector:
    def down(self, x: float, y: float, p: float) -> None: ...
    def move(self, x: float, y: float, p: float) -> None: ...
    def up(self, x: float, y: float, p: float) -> None: ...
    def close(self) -> None: ...


class NullInjector(InputInjector):
    """No-op backend (useful for "just log strokes" mode)."""


# --------------------------------------------------------------------------- #
# Canvas backend                                                             #
# --------------------------------------------------------------------------- #

class StrokeToCanvasInjector(InputInjector):
    """Draw straight onto the Pillow canvas."""

    def __init__(self, canvas_provider: Callable) -> None:
        self.canvas_provider = (
            canvas_provider if callable(canvas_provider) else (lambda: canvas_provider)
        )
        self._last = None

    def down(self, x: float, y: float, p: float) -> None:
        c = self.canvas_provider()
        c.draw_dot(x, y, p)
        self._last = (x, y)

    def move(self, x: float, y: float, p: float) -> None:
        c = self.canvas_provider()
        if self._last is not None:
            c.draw_segment(self._last[0], self._last[1], x, y, p)
        self._last = (x, y)

    def up(self, x: float, y: float, p: float) -> None:
        c = self.canvas_provider()
        if self._last is not None:
            c.draw_segment(self._last[0], self._last[1], x, y, p)
        self._last = None


# --------------------------------------------------------------------------- #
# uinput backend (Linux, works on X11 and Wayland)                           #
# --------------------------------------------------------------------------- #

class UinputInjector(InputInjector):
    def __init__(
        self,
        width: int,
        height: int,
        pressure_max: int = 2047,
        name: str = "Kindle Scribe Pen",
    ) -> None:
        from evdev import UInput, AbsInfo, ecodes as e  # local import

        cap = {
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, width, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, height, 0, 0, 0)),
                (e.ABS_PRESSURE, AbsInfo(0, 0, pressure_max, 0, 0, 0)),
            ],
            e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOUCH, e.BTN_STYLUS, e.BTN_STYLUS2],
        }
        self.ui = UInput(cap, name=name, version=1)
        self.w = width
        self.h = height
        self.pmax = max(1, pressure_max)
        self._down = False

    def _write_abs(self, x: float, y: float, p: float) -> None:
        from evdev import ecodes as e
        xi = int(max(0, min(self.w - 1, x)))
        yi = int(max(0, min(self.h - 1, y)))
        pi = int(max(0, min(self.pmax, p * self.pmax)))
        self.ui.write(e.EV_ABS, e.ABS_X, xi)
        self.ui.write(e.EV_ABS, e.ABS_Y, yi)
        self.ui.write(e.EV_ABS, e.ABS_PRESSURE, pi)

    def down(self, x: float, y: float, p: float) -> None:
        from evdev import ecodes as e
        if not self._down:
            self.ui.write(e.EV_KEY, e.BTN_TOOL_PEN, 1)
            self._down = True
        self.ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
        self._write_abs(x, y, p)
        self.ui.syn()

    def move(self, x: float, y: float, p: float) -> None:
        self._write_abs(x, y, p)
        self.ui.syn()

    def up(self, x: float, y: float, p: float) -> None:
        from evdev import ecodes as e
        self._write_abs(x, y, p)
        self.ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
        self.ui.syn()
        self.ui.write(e.EV_KEY, e.BTN_TOOL_PEN, 0)
        self._down = False
        self.ui.syn()

    def close(self) -> None:
        try:
            self.ui.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# xdotool backend (X11 only)                                                 #
# --------------------------------------------------------------------------- #

class XdotoolInjector(InputInjector):
    def __init__(self) -> None:
        if shutil.which("xdotool") is None:
            raise RuntimeError("xdotool not found on PATH")

    def down(self, x: float, y: float, p: float) -> None:
        subprocess.run(
            ["xdotool", "mousemove", str(int(x)), str(int(y)),
             "mousedown", "1"],
            check=False,
        )

    def move(self, x: float, y: float, p: float) -> None:
        subprocess.run(
            ["xdotool", "mousemove", str(int(x)), str(int(y))],
            check=False,
        )

    def up(self, x: float, y: float, p: float) -> None:
        subprocess.run(["xdotool", "mouseup", "1"], check=False)


# --------------------------------------------------------------------------- #
# ydotool backend (Wayland)                                                  #
# --------------------------------------------------------------------------- #

class YdotoolInjector(InputInjector):
    def __init__(self) -> None:
        if shutil.which("ydotool") is None:
            raise RuntimeError("ydotool not found on PATH")

    def _move(self, x: float, y: float) -> None:
        subprocess.run(
            ["ydotool", "mousemove", "--absolute",
             "-x", str(int(x)), "-y", str(int(y))],
            check=False,
        )

    def down(self, x: float, y: float, p: float) -> None:
        self._move(x, y)
        # 0x40 == BTN_LEFT press
        subprocess.run(["ydotool", "click", "0x40"], check=False)

    def move(self, x: float, y: float, p: float) -> None:
        self._move(x, y)

    def up(self, x: float, y: float, p: float) -> None:
        # 0x80 == BTN_LEFT release
        subprocess.run(["ydotool", "click", "0x80"], check=False)


# --------------------------------------------------------------------------- #
# Factory                                                                    #
# --------------------------------------------------------------------------- #

def build_injector(mode: str, canvas_provider: Callable, width: int, height: int
                   ) -> InputInjector:
    mode = (mode or "canvas").lower()
    if mode == "canvas":
        return StrokeToCanvasInjector(canvas_provider)
    if mode == "none":
        return NullInjector()
    if mode == "uinput":
        return UinputInjector(width, height)
    if mode == "xdotool":
        return XdotoolInjector()
    if mode == "ydotool":
        return YdotoolInjector()
    raise ValueError(f"unknown injector mode: {mode!r}")