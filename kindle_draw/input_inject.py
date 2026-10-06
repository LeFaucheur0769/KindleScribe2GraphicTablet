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
    """Kernel-level virtual Wacom-class tablet via /dev/uinput.

    Recognised by libinput on X11 and Wayland alike as a pen tablet:
    the cursor follows the pen absolutely, taps click, pressure flows
    through ABS_PRESSURE for Krita/GIMP/Inkscape.
    """

    VENDOR = 0x1234
    PRODUCT = 0x5678
    VERSION = 1

    def __init__(
        self,
        width: int,
        height: int,
        pressure_max: int = 4095,
        name: str = "Kindle Scribe Pen",
    ) -> None:
        from evdev import UInput, AbsInfo, ecodes as e

        # 12 units/mm ≈ 230 ppi, matching the Scribe panel. libinput refuses
        # to classify a device as a tablet if ABS_X or ABS_Y has no
        # resolution, so this value must be nonzero.
        RES_PER_MM = 12

        cap = {
            e.EV_ABS: [
                (e.ABS_X,        AbsInfo(0, 0, width,  0, 0, RES_PER_MM)),
                (e.ABS_Y,        AbsInfo(0, 0, height, 0, 0, RES_PER_MM)),
                (e.ABS_PRESSURE, AbsInfo(0, 0, pressure_max, 0, 0, 0)),
                (e.ABS_TILT_X,   AbsInfo(0, -9000, 9000, 0, 0, 0)),
                (e.ABS_TILT_Y,   AbsInfo(0, -9000, 9000, 0, 0, 0)),
                (e.ABS_DISTANCE, AbsInfo(0, 0, 255, 0, 0, 0)),
            ],
            e.EV_KEY: [
                e.BTN_TOOL_PEN,
                e.BTN_TOOL_RUBBER,
                e.BTN_TOUCH,
                e.BTN_STYLUS,
                e.BTN_STYLUS2,
            ],
        }

        self.ui = UInput(
            cap,
            name=name,
            vendor=self.VENDOR,
            product=self.PRODUCT,
            version=self.VERSION,
            bustype=e.BUS_USB,
            # This is the magic bit — says "tablet, not mouse".
            input_props=[e.INPUT_PROP_DIRECT],
        )

        self.w = width
        self.h = height
        self.pmax = max(1, pressure_max)
        self._in_range = False
        self._eraser = False

    # -- internal -------------------------------------------------------- #

    def _abs(self, code, v) -> None:
        from evdev import ecodes as e
        self.ui.write(e.EV_ABS, code, int(v))

    def _key(self, code, v) -> None:
        from evdev import ecodes as e
        self.ui.write(e.EV_KEY, code, int(v))

    def _state(self, x, y, p, tilt_x=0.0, tilt_y=0.0) -> None:
        from evdev import ecodes as e
        self._abs(e.ABS_X, max(0, min(self.w - 1, int(x))))
        self._abs(e.ABS_Y, max(0, min(self.h - 1, int(y))))
        self._abs(e.ABS_PRESSURE, max(0, min(self.pmax, int(p * self.pmax))))
        self._abs(e.ABS_TILT_X, max(-9000, min(9000, int(tilt_x))))
        self._abs(e.ABS_TILT_Y, max(-9000, min(9000, int(tilt_y))))
        self._abs(e.ABS_DISTANCE, 0)

    # -- InputInjector API ---------------------------------------------- #

    def down(self, x, y, p) -> None:
        from evdev import ecodes as e
        if not self._in_range:
            # Enter range with the correct tool.
            self._key(e.BTN_TOOL_RUBBER if self._eraser
                      else e.BTN_TOOL_PEN, 1)
            self._in_range = True
        self._key(e.BTN_TOUCH, 1)
        self._state(x, y, p)
        self.ui.syn()

    def move(self, x, y, p) -> None:
        self._state(x, y, p)
        self.ui.syn()

    def up(self, x, y, p) -> None:
        from evdev import ecodes as e
        self._state(x, y, p)
        self._key(e.BTN_TOUCH, 0)
        self.ui.syn()
        # Keep BTN_TOOL_PEN set — the pen stays "in range" between strokes,
        # so the app treats it as hovering, not as a lifted mouse.

    def set_eraser(self, on: bool) -> None:
        """Called when the pen's eraser button flips state."""
        from evdev import ecodes as e
        on = bool(on)
        if on == self._eraser:
            return
        if self._in_range:
            self._key(e.BTN_TOOL_PEN if on else e.BTN_TOOL_RUBBER, 0)
            self._key(e.BTN_TOOL_RUBBER if on else e.BTN_TOOL_PEN, 1)
        self._eraser = on
        self.ui.syn()

    def close(self) -> None:
        try:
            from evdev import ecodes as e
            if self._in_range:
                self._key(e.BTN_TOOL_PEN if not self._eraser
                          else e.BTN_TOOL_RUBBER, 0)
                self.ui.syn()
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