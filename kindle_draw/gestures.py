"""Multi-touch gesture recognition and viewport math for pinch-to-zoom.

This module implements two cooperating pieces used by canvas mode to support
pinch-to-zoom and panning with fingers on the Kindle Scribe touchscreen:

``Viewport``
    A zoomable / pannable window onto a fixed-size canvas.  It stores a
    ``scale`` and the top-left ``offset`` of the visible window *in canvas
    space* and provides the coordinate transforms between the two spaces.

``PinchTracker``
    A pure state machine that consumes raw evdev multi-touch (protocol B)
    events ``(type, code, value)`` from the touchscreen and emits
    ``GestureEvent`` objects (pinch / pan) expressed in canvas-space view
    units.  It has no I/O, no threads and no dependency on the rest of the
    application, so it is fully unit-testable.

Coordinate spaces
-----------------

*canvas space*
    Fixed pixel coordinates of the host-side Pillow canvas, i.e.
    ``0 <= cx < canvas_w``, ``0 <= cy < canvas_h``.  Drawing happens here.

*view space*
    What the user currently sees.  The stylus pipeline maps the tablet panel
    onto the *visible window*, so view coordinates are normalised to the same
    numeric range as canvas coordinates: ``0 <= vx <= canvas_w``,
    ``0 <= vy <= canvas_h``.  At scale 1 with zero offset the two spaces are
    identical (identity viewport).

The viewport window in canvas space is::

    [offset_x, offset_x + canvas_w / scale] x
    [offset_y, offset_y + canvas_h / scale]

and the transforms are::

    view_to_canvas(v) = offset + v / scale
    canvas_to_view(c) = (c - offset) * scale

Pinch composition
-----------------

A pinch is a zoom by factor ``k = s' / s`` centred on the finger midpoint
(focal point) combined with a translation of that midpoint by ``d``.  The
requirement is that the canvas point currently under the finger midpoint
follows the fingers::

    c = offset + f / s                    (canvas point under focal, before)
    c = offset' + (f + d) / s'            (same point, after)

so::

    offset' = offset + f / s - (f + d) / s'

Setting ``k = 1`` reduces the formula to a pure pan, and ``d = 0`` to a pure
zoom-at-focal.  ``Viewport.apply_pinch`` implements exactly this.

Touch-device scaling
--------------------

The touchscreen reports raw device units whose absolute range is not exposed
by ``/proc/bus/input/devices``.  To convert raw deltas and midpoints into
view units the tracker scales by ``canvas_dim / span`` where ``span`` is::

* the ``touch_range`` override supplied by the caller (exact, recommended), or
* an adaptive estimate from the min/max raw coordinates observed so far,
  floored at ``canvas_dim`` so that a tiny observed finger spread does not
  amplify noise into huge pans.

With the adaptive estimate the scaling converges towards the true device
ratio as the user moves fingers across the screen; pass ``--touch-range`` for
an exact mapping.

Caveats
-------

* The Kindle UI keeps receiving the same touch events (the kernel broadcasts
  input events to every open file descriptor), so the front-end may also
  react to the gestures unless the UI is in a state that ignores them.
* Rotation is handled best-effort: canvas mode runs portrait (rotation 0),
  which is the primary path.  The ``rotation`` parameter mirrors
  :func:`kindle_draw.calibration.apply_rotation` semantics for callers that
  rotate the canvas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .calibration import apply_rotation

# evdev event types / codes we care about (linux/input.h).
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3

ABS_MT_SLOT = 47
ABS_MT_TRACKING_ID = 53
ABS_MT_POSITION_X = 54
ABS_MT_POSITION_Y = 55

Box = Tuple[int, int, int, int]


# --------------------------------------------------------------------------- #
# Viewport                                                                     #
# --------------------------------------------------------------------------- #


class Viewport:
    """A zoomable, pannable window onto a fixed-size canvas.

    All public mutators clamp ``scale`` into ``[min_scale, max_scale]`` and
    clamp the offsets so the visible window never leaves the canvas::

        0 <= offset_x <= canvas_w * (1 - 1 / scale)

    (at ``scale == 1`` the only legal offset is 0, i.e. the whole canvas is
    visible).  Coordinates are floats; callers round when indexing pixels.
    """

    def __init__(self, canvas_w: int, canvas_h: int,
                 min_scale: float = 1.0, max_scale: float = 8.0) -> None:
        self.canvas_w = float(canvas_w)
        self.canvas_h = float(canvas_h)
        self.min_scale = float(min_scale)
        self.max_scale = max(float(max_scale), self.min_scale)
        self.scale: float = 1.0
        self.offset_x: float = 0.0
        self.offset_y: float = 0.0

    # -- transforms --------------------------------------------------------- #

    def view_to_canvas(self, vx: float, vy: float) -> Tuple[float, float]:
        """Map a point in view space (what the user sees) to canvas space."""
        return (self.offset_x + vx / self.scale,
                self.offset_y + vy / self.scale)

    def canvas_to_view(self, cx: float, cy: float) -> Tuple[float, float]:
        """Map a point in canvas space to view space."""
        return ((cx - self.offset_x) * self.scale,
                (cy - self.offset_y) * self.scale)

    def canvas_to_panel(self, cx: float, cy: float,
                        pw: int, ph: int) -> Tuple[float, float]:
        """Map a canvas point to panel/preview pixels through the viewport.

        Canvas points outside the visible window map outside ``[0, pw] x
        [0, ph]``; callers clip.
        """
        vx, vy = self.canvas_to_view(cx, cy)
        return (vx * pw / max(1.0, self.canvas_w),
                vy * ph / max(1.0, self.canvas_h))

    def viewport_box(self) -> Box:
        """Integer bounding box of the visible window in canvas pixels.

        Always at least 1x1 canvas pixels (PIL crops need a non-empty
        region), even for extreme zoom factors on very small canvases.
        """
        x1 = min(self.canvas_w, self.offset_x + self.canvas_w / self.scale)
        y1 = min(self.canvas_h, self.offset_y + self.canvas_h / self.scale)
        x0, y0 = int(round(self.offset_x)), int(round(self.offset_y))
        x1, y1 = int(round(x1)), int(round(y1))
        if x1 <= x0:
            x1 = min(self.canvas_w, x0 + 1)
            x0 = max(0, x1 - 1)
        if y1 <= y0:
            y1 = min(self.canvas_h, y0 + 1)
            y0 = max(0, y1 - 1)
        return (x0, y0, x1, y1)

    def is_identity(self) -> bool:
        """True when the viewport shows the whole canvas 1:1."""
        return (self.scale == 1.0
                and self.offset_x == 0.0 and self.offset_y == 0.0)

    # -- mutators ------------------------------------------------------------- #

    def apply_pinch(self, factor: float, focal_vx: float, focal_vy: float,
                    dx: float = 0.0, dy: float = 0.0) -> bool:
        """Apply a pinch (zoom at focal + translation), return True if changed.

        ``factor`` is the multiplicative zoom step, ``(focal_vx, focal_vy)``
        is the focal point in view units and ``(dx, dy)`` the translation of
        the focal point in view units (the pan component of the gesture).
        Implements ``offset' = offset + f / s - (f + d) / s'``.
        """
        factor = max(1e-6, float(factor))
        new_scale = min(max(self.scale * factor, self.min_scale),
                        self.max_scale)
        if new_scale == self.scale and dx == 0.0 and dy == 0.0:
            return False
        before = (self.scale, self.offset_x, self.offset_y)
        self.offset_x += focal_vx / self.scale \
            - (focal_vx + dx) / new_scale
        self.offset_y += focal_vy / self.scale \
            - (focal_vy + dy) / new_scale
        self.scale = new_scale
        self._clamp_offsets()
        # A gesture that clamps away completely (e.g. panning while already
        # pinned to a canvas edge) must not report a change: callers use the
        # return value to decide whether to bump a version counter and push a
        # full-panel redraw.
        return (self.scale, self.offset_x, self.offset_y) != before

    def apply_pan(self, dx: float, dy: float) -> bool:
        """Translate the visible window by a view-space delta."""
        return self.apply_pinch(1.0, 0.0, 0.0, dx, dy)

    def zoom_at(self, factor: float, focal_cx: float, focal_cy: float) -> bool:
        """Zoom keeping the *canvas* point ``(focal_cx, focal_cy)`` fixed.

        Convenience wrapper: converts the canvas-space focal point into view
        space and calls :meth:`apply_pinch` with zero translation, which
        algebraically gives ``offset' = f - (f - offset) * s / s'``.
        """
        vx, vy = self.canvas_to_view(focal_cx, focal_cy)
        return self.apply_pinch(factor, vx, vy, 0.0, 0.0)

    def reset(self) -> bool:
        """Return to the identity viewport; True if anything changed."""
        if self.is_identity():
            return False
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        return True

    # -- internals ------------------------------------------------------------ #

    def _clamp_offsets(self) -> None:
        """Pin the visible window inside the canvas bounds."""
        max_x = max(0.0, self.canvas_w * (1.0 - 1.0 / self.scale))
        max_y = max(0.0, self.canvas_h * (1.0 - 1.0 / self.scale))
        self.offset_x = min(max(self.offset_x, 0.0), max_x)
        self.offset_y = min(max(self.offset_y, 0.0), max_y)


# --------------------------------------------------------------------------- #
# Gesture events                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class GestureEvent:
    """One recognised multi-touch gesture, in canvas-space view units.

    ``kind`` is ``"pinch"`` (zoom + pan combined) or ``"pan"`` (translation
    only, ``factor == 1.0``).  ``factor`` is the multiplicative zoom step for
    this event, ``(dx, dy)`` the focal-point translation and ``(focal_x,
    focal_y)`` the focal point itself (finger midpoint) — all in view units
    as described in the module docstring.
    """

    kind: str
    factor: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    focal_x: float = 0.0
    focal_y: float = 0.0


# --------------------------------------------------------------------------- #
# Pinch tracker                                                                #
# --------------------------------------------------------------------------- #


class PinchTracker:
    """State machine turning raw MT-B events into :class:`GestureEvent`s.

    Feed it ``(type, code, value)`` tuples exactly as produced by
    :func:`kindle_draw.evdev_reader.parse_events` (or the raw ``evtest``
    stream parsed into ints).  It tracks protocol-B slots, keeps a baseline
    (midpoint + spread) per contact-count so that each position update yields
    the *delta* since the previous update, and resets the baseline whenever
    the contact count changes so lifting/adding fingers never causes a jump.

    Parameters
    ----------
    canvas_w, canvas_h:
        Canvas size in pixels; view units match this space.
    rotation:
        Best-effort rotation (0/90/180/270, clockwise) mirroring
        :func:`kindle_draw.calibration.apply_rotation`.  Canvas mode uses 0.
    touch_range:
        ``(max_x, max_y)`` or ``(min_x, max_x, min_y, max_y)`` raw device
        range override for exact raw->view scaling.  ``None`` enables the
        adaptive observed-bounds heuristic.
    enable_pan:
        When True a *single* finger drag emits ``"pan"`` events.  Default
        False: the Scribe stylus can generate stray single-contact MT events,
        so panning defaults to two-finger drags folded into the pinch event.
    min_spread:
        Minimum raw finger spread (device units) before a pinch zoom is
        computed; below it the factor stays 1.0 (noise gate).
    max_step:
        Clamp each per-event zoom factor into ``[1/max_step, max_step]`` so a
        noisy single event cannot teleport the scale.
    """

    def __init__(self, canvas_w: int, canvas_h: int, *,
                 rotation: int = 0,
                 touch_range: Optional[Sequence[int]] = None,
                 enable_pan: bool = False,
                 min_spread: float = 15.0,
                 max_step: float = 1.5) -> None:
        self.canvas_w = float(canvas_w)
        self.canvas_h = float(canvas_h)
        self.rotation = int(rotation) % 360
        self.enable_pan = bool(enable_pan)
        self.min_spread = float(min_spread)
        self.max_step = max(1.0, float(max_step))

        # Active contacts: slot -> {"id": float, "x": float|None, "y":
        # float|None} in raw device units.  Protocol B: ABS_MT_SLOT selects
        # the slot the following ABS_MT_* events belong to.  Coordinates are
        # None until both axes have been observed for that contact.
        self._slots: Dict[int, Dict[str, Optional[float]]] = {}
        self._cur_slot = 0

        # Adaptive raw-coordinate bounds (None until observed).
        self._min_x: Optional[float] = None
        self._max_x: Optional[float] = None
        self._min_y: Optional[float] = None
        self._max_y: Optional[float] = None

        if touch_range is not None:
            rng = list(touch_range)
            if len(rng) == 2:
                self._fixed_x: Optional[Tuple[float, float]] = (0.0, float(rng[0]))
                self._fixed_y: Optional[Tuple[float, float]] = (0.0, float(rng[1]))
            elif len(rng) == 4:
                self._fixed_x = (float(rng[0]), float(rng[1]))
                self._fixed_y = (float(rng[2]), float(rng[3]))
            else:
                raise ValueError("touch_range must have 2 or 4 values")
        else:
            self._fixed_x = None
            self._fixed_y = None

        # Baselines in raw units (converted with the *current* span when an
        # event is emitted, so a mid-gesture span update stays consistent).
        self._pair: Optional[Tuple[float, float, float]] = None   # mid_x, mid_y, spread
        self._single: Optional[Tuple[float, float]] = None        # x, y

    # -- public --------------------------------------------------------------- #

    @property
    def contacts(self) -> int:
        """Number of currently tracked contacts."""
        return len(self._slots)

    def reset(self) -> None:
        """Forget all contacts and baselines (e.g. after a stream hiccup)."""
        self._slots.clear()
        self._cur_slot = 0
        self._pair = None
        self._single = None

    def feed(self, t: int, c: int, v: int) -> Optional[GestureEvent]:
        """Consume one ``(type, code, value)`` event.

        Gesture steps are evaluated once per ``EV_SYN`` frame: the kernel
        groups all slot updates for one sampling instant into a frame closed
        by ``SYN_REPORT``, so evaluating on every individual position event
        would double-count half-updated frames (contact A moved, contact B
        not yet) and let a trailing no-op event overwrite the real one.

        Returns a :class:`GestureEvent` when the frame that just closed
        produced a gesture step, else ``None``.  Non-MT events are ignored.
        """
        if t == EV_SYN:
            return self._evaluate()
        if t != EV_ABS:
            return None
        if c == ABS_MT_SLOT:
            self._cur_slot = int(v)
            return None

        if c == ABS_MT_TRACKING_ID:
            if v < 0:
                self._slots.pop(self._cur_slot, None)
            else:
                # Coordinates start unknown: MT-B announces a contact with
                # ABS_MT_TRACKING_ID before its position events, and the
                # tracker must not evaluate a half-initialised contact
                # (otherwise the missing axis reads as 0 and produces a
                # bogus baseline / spurious pan).
                slot = self._slots.setdefault(self._cur_slot,
                                              {"x": None, "y": None})
                slot["id"] = float(v)
            self._pair = None
            self._single = None
            return None

        if c == ABS_MT_POSITION_X:
            slot = self._slots.setdefault(self._cur_slot,
                                          {"id": -2.0, "x": None, "y": None})
            slot["x"] = float(v)
            self._observe(v, x=True)
            return None
        if c == ABS_MT_POSITION_Y:
            slot = self._slots.setdefault(self._cur_slot,
                                          {"id": -2.0, "x": None, "y": None})
            slot["y"] = float(v)
            self._observe(v, x=False)
            return None
        return None

    # -- evaluation ------------------------------------------------------------- #

    def _evaluate(self) -> Optional[GestureEvent]:
        """Compare current contact geometry against the stored baseline."""
        # Only contacts with *both* axes observed participate: a contact just
        # announced by ABS_MT_TRACKING_ID has no coordinates yet, and one
        # that has only seen POSITION_X would otherwise evaluate against a
        # phantom y=0 baseline.
        contacts = [s for s in self._slots.values()
                    if s["x"] is not None and s["y"] is not None]
        n = len(contacts)
        if n >= 2:
            self._single = None
            return self._evaluate_pair(contacts)
        if n == 1:
            self._pair = None
            x, y = contacts[0]["x"], contacts[0]["y"]
            prev = self._single
            self._single = (x, y)
            if prev is None or not self.enable_pan:
                return None
            # Convert both endpoints with the *current* span and subtract:
            # consistent even when the adaptive span grows mid-gesture, and
            # rotation-safe because apply_rotation is affine.
            px, py = self._raw_to_view(prev[0], prev[1])
            fx, fy = self._raw_to_view(x, y)
            dx, dy = fx - px, fy - py
            if dx == 0.0 and dy == 0.0:
                return None  # frame moved nothing: no-op, like the pair path
            return GestureEvent("pan", 1.0, dx, dy, fx, fy)
        self._pair = None
        self._single = None
        return None

    def _evaluate_pair(self,
                       contacts: List[Dict[str, Optional[float]]]) -> Optional[GestureEvent]:
        # With more than two contacts use the pair with the largest spread:
        # it gives the most stable midpoint and zoom ratio.
        best = None
        for i in range(n := len(contacts)):
            for j in range(i + 1, n):
                a, b = contacts[i], contacts[j]
                spread = ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5
                if best is None or spread > best[0]:
                    best = (spread, (a["x"] + b["x"]) / 2.0,
                            (a["y"] + b["y"]) / 2.0)
        spread, mid_x, mid_y = best

        prev = self._pair
        self._pair = (mid_x, mid_y, spread)
        if prev is None:
            return None  # baseline just established: no jump on contact start

        # Zoom factor from the spread ratio, gated against noise and clamped.
        factor = 1.0
        if prev[2] >= self.min_spread and spread >= self.min_spread:
            factor = spread / prev[2]
        factor = min(max(factor, 1.0 / self.max_step), self.max_step)

        # Convert both midpoints with the *current* span and subtract: the
        # delta stays consistent when the adaptive span grows mid-gesture,
        # and rotation is handled because apply_rotation is affine (the
        # constant part cancels in the difference).
        px, py = self._raw_to_view(prev[0], prev[1])
        fx, fy = self._raw_to_view(mid_x, mid_y)
        dx, dy = fx - px, fy - py
        if factor == 1.0 and dx == 0.0 and dy == 0.0:
            return None
        return GestureEvent("pinch", factor, dx, dy, fx, fy)

    # -- raw -> view conversion -------------------------------------------------- #

    def _observe(self, value: int, *, x: bool) -> None:
        """Grow the adaptive raw bounds with one observed coordinate."""
        v = float(value)
        if x:
            self._min_x = v if self._min_x is None else min(self._min_x, v)
            self._max_x = v if self._max_x is None else max(self._max_x, v)
        else:
            self._min_y = v if self._min_y is None else min(self._min_y, v)
            self._max_y = v if self._max_y is None else max(self._max_y, v)

    def _axis_span(self, lo: Optional[float], hi: Optional[float],
                   fixed: Optional[Tuple[float, float]],
                   canvas_dim: float) -> Tuple[float, float]:
        """Return ``(offset, span)`` for one axis in raw units."""
        if fixed is not None:
            off, top = fixed
            return off, max(top - off, 1.0)
        if lo is None or hi is None:
            return 0.0, canvas_dim
        observed = hi - lo
        # Floor at canvas_dim so a small observed spread does not amplify
        # noise; the estimate converges as the user covers more of the screen.
        return lo, max(observed, canvas_dim)

    def _raw_to_view(self, x: float, y: float) -> Tuple[float, float]:
        """Map raw device coordinates to canvas-space view units."""
        off_x, span_x = self._axis_span(self._min_x, self._max_x,
                                        self._fixed_x, self.canvas_w)
        off_y, span_y = self._axis_span(self._min_y, self._max_y,
                                        self._fixed_y, self.canvas_h)
        cx = (x - off_x) * self.canvas_w / span_x
        cy = (y - off_y) * self.canvas_h / span_y
        if self.rotation:
            cx, cy = apply_rotation(cx, cy, int(self.canvas_w),
                                    int(self.canvas_h), self.rotation)
        return cx, cy
