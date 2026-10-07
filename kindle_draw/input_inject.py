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
    """Virtual tablet where the tip moves the cursor and the barrel button
    is the sole source of clicks.

    Two button modes:

    * Momentary (default): press = click down, release = click up.
      Hold-and-drag works naturally.
    * Sticky: press = click stays down until the next press, regardless
      of tip state. Useful for long drags without cramping your thumb.

    Toggle at runtime with SIGUSR1 (see app.py) or start in either mode
    with the ``--sticky-click`` CLI flag.
    """

    VENDOR = 0x1234
    PRODUCT = 0x5678
    VERSION = 1

    HOVER_DISTANCE = 100

    def __init__(
        self,
        width: int,
        height: int,
        pressure_max: int = 4095,
        name: str = "Kindle Scribe Pen",
        sticky_click: bool = False,
    ) -> None:
        from evdev import UInput, AbsInfo, ecodes as e

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
            ],
        }
        self.ui = UInput(
            cap, name=name, vendor=self.VENDOR, product=self.PRODUCT,
            version=self.VERSION, bustype=e.BUS_USB,
            input_props=[e.INPUT_PROP_DIRECT],
        )

        self.w = width
        self.h = height
        self.pmax = max(1, pressure_max)

        self._last_x = width / 2.0
        self._last_y = height / 2.0

        self._in_range = False
        self._touching = False
        self._tip_down = False
        self._eraser = False
        self._button = False
        self._button2 = False

        # Sticky mode: when True, the button toggles a held click rather
        # than acting as a momentary press. Doesn't affect anything else.
        self._sticky = bool(sticky_click)

    # -- public mode toggle -------------------------------------------- #

    @property
    def sticky_click(self) -> bool:
        return self._sticky

    def set_sticky_click(self, on: bool) -> bool:
        """Force the sticky-click state. Returns the new value."""
        on = bool(on)
        if on == self._sticky:
            return self._sticky
        self._sticky = on
        # Leaving sticky with a click still held: release it.
        if not on and self._touching:
            self._end_click()
        return self._sticky

    def toggle_sticky_click(self) -> bool:
        """Flip the sticky-click state. Returns the new value."""
        return self.set_sticky_click(not self._sticky)

    # -- internal ------------------------------------------------------- #

    def _abs(self, code, v) -> None:
        from evdev import ecodes as e
        self.ui.write(e.EV_ABS, code, int(v))

    def _key(self, code, v) -> None:
        from evdev import ecodes as e
        self.ui.write(e.EV_KEY, code, int(v))

    def _write_pos(self, x, y) -> None:
        from evdev import ecodes as e
        self._abs(e.ABS_X, max(0, min(self.w - 1, int(x))))
        self._abs(e.ABS_Y, max(0, min(self.h - 1, int(y))))

    def _write_hover_extras(self) -> None:
        from evdev import ecodes as e
        self._abs(e.ABS_PRESSURE, 0)
        self._abs(e.ABS_DISTANCE, self.HOVER_DISTANCE)

    def _write_click_extras(self) -> None:
        from evdev import ecodes as e
        self._abs(e.ABS_PRESSURE, self.pmax)
        self._abs(e.ABS_DISTANCE, 0)

    def _enter_range(self) -> None:
        from evdev import ecodes as e
        if not self._in_range:
            self._key(e.BTN_TOOL_RUBBER if self._eraser
                      else e.BTN_TOOL_PEN, 1)
            self._in_range = True

    def _leave_range(self) -> None:
        from evdev import ecodes as e
        if self._in_range:
            self._key(e.BTN_TOOL_PEN if not self._eraser
                      else e.BTN_TOOL_RUBBER, 0)
            self._in_range = False

    def _begin_click(self) -> None:
        from evdev import ecodes as e
        self._enter_range()
        self._write_pos(self._last_x, self._last_y)
        self._write_click_extras()
        if not self._touching:
            self._key(e.BTN_TOUCH, 1)
            self._touching = True

    def _end_click(self) -> None:
        from evdev import ecodes as e
        if self._touching:
            self._key(e.BTN_TOUCH, 0)
            self._touching = False
        self._write_pos(self._last_x, self._last_y)
        self._write_hover_extras()
        if not self._tip_down:
            self._leave_range()

    def _emit_position(self, x, y) -> None:
        self._last_x = float(x)
        self._last_y = float(y)
        self._enter_range()
        self._write_pos(x, y)
        if self._touching:
            self._write_click_extras()
        else:
            self._write_hover_extras()

    # -- InputInjector API ---------------------------------------------- #

    def down(self, x, y, p) -> None:
        self._tip_down = True
        self._emit_position(x, y)
        self.ui.syn()

    def move(self, x, y, p) -> None:
        self._emit_position(x, y)
        self.ui.syn()

    def move_hover(self, x, y, p) -> None:
        # Cursor follows the pen whenever the click is active OR whenever
        # we're in sticky mode (so the user can aim before pressing again
        # to release). Otherwise stay silent.
        if self._touching or self._button or self._button2 or self._sticky:
            self._emit_position(x, y)
            self.ui.syn()

    def up(self, x, y, p) -> None:
        self._tip_down = False
        self._last_x = float(x)
        self._last_y = float(y)
        self._enter_range()
        self._write_pos(x, y)
        if self._touching:
            self._write_click_extras()
        else:
            self._write_hover_extras()
            if not self._button and not self._button2 and not self._sticky:
                self._leave_range()
        self.ui.syn()

    def set_eraser(self, on: bool) -> None:
        from evdev import ecodes as e
        on = bool(on)
        if on == self._eraser:
            return
        if self._in_range:
            self._key(e.BTN_TOOL_PEN if on else e.BTN_TOOL_RUBBER, 0)
            self._key(e.BTN_TOOL_RUBBER if on else e.BTN_TOOL_PEN, 1)
        self._eraser = on
        self.ui.syn()

    def set_stylus_button(self, pressed: bool) -> None:
        from evdev import ecodes as e
        pressed = bool(pressed)
        if pressed == self._button:
            return
        self._button = pressed

        if self._sticky:
            # Sticky mode: only the press edge matters — it toggles the
            # click. Releases are ignored.
            if pressed:
                if self._touching:
                    self._end_click()
                else:
                    self._begin_click()
                self.ui.syn()
            return

        # Momentary mode: press = down, release = up.
        if pressed:
            self._begin_click()
        else:
            self._end_click()
        self.ui.syn()

    def set_stylus_button2(self, pressed: bool) -> None:
        from evdev import ecodes as e
        pressed = bool(pressed)
        if pressed == self._button2:
            return
        self._button2 = pressed
        if not self._sticky:
            if pressed:
                self._begin_click()
            else:
                self._end_click()
        self.ui.syn()

    def close(self) -> None:
        from evdev import ecodes as e
        try:
            if self._touching:
                self._key(e.BTN_TOUCH, 0)
            if self._in_range:
                self._key(e.BTN_TOOL_PEN if not self._eraser
                          else e.BTN_TOOL_RUBBER, 0)
            self.ui.syn()
        except Exception:
            pass
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