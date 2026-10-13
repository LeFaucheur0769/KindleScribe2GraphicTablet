"""Dataclasses that describe the runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass
class KindleConfig:
    host: str = "192.168.1.14"
    port: int = 2022
    user: str = "root"
    key: Optional[str] = None
    password: Optional[str] = None
    fbink: str = "/mnt/us/libkh/bin/fbink"
    control_path: Optional[str] = None
    tmp_png: Optional[str] = None   # None -> unique per-instance remote path


@dataclass
class PanelConfig:
    width: int = 1860
    height: int = 2480
    aspect: str = "fit"
    invert: bool = False


@dataclass
class StylusConfig:
    dev: Optional[str] = None
    esz: Optional[int] = None
    cal_path: str = "~/.kindle_draw_cal.json"
    pressure_min: Optional[int] = None
    pressure_max: Optional[int] = None
    # NOTE: no `rotate` here. Calibration is always portrait; rotation is
    # resolved at runtime and lives on InjectConfig.


@dataclass
class TouchConfig:
    """Multi-touch (finger) gesture settings for pinch-zoom/pan in canvas mode.

    The touchscreen device is read through a second SSH ``cat`` stream,
    independent from the stylus device.  Gestures only make sense in canvas
    mode (the host-side Pillow canvas owns the viewport); in system-inject
    modes the flag is ignored with a warning.
    """

    enabled: bool = False             # --pinch
    dev: Optional[str] = None         # --touch-dev (None -> autodetect)
    pan: bool = False                 # --touch-pan: single-finger pan (opt-in)
    touch_range: Optional[Tuple[int, ...]] = None  # --touch-range: raw min,max pairs
    # Zoom ceiling.  None means "derive it from the canvas resolution" so the
    # user can zoom until canvas pixels map 1:1 onto panel pixels (the sharpest
    # possible view).  build_config always resolves this to a concrete float.
    max_scale: Optional[float] = None  # --max-zoom (explicit value must be > 1)
    # Minimum two-finger distance, in raw touch-device units, before a
    # two-finger frame counts as a gesture at all (--min-spread).  Raise it
    # if close-together fingers trigger pinch/pan by accident.
    min_spread: float = 60.0
    # Largest contact size, in raw touch-device units, that may drive a gesture
    # (--max-finger-size).  Bigger contacts (a resting hand/palm) are ignored,
    # so they cannot zoom or pan.  <= 0 disables the gate; drivers without
    # contact-size axes are unaffected.
    max_finger_size: float = 100.0


@dataclass
class PenConfig:
    color: Tuple[int, int, int] = (0, 0, 0)
    size: float = 8.0                 # nominal width in panel pixels at 1:1 zoom
    eraser_size: float = 48.0
    pressure_sensitive: bool = True
    # --fixed-pen-size: keep the on-screen pen width constant at every zoom
    # level instead of growing it logarithmically (see canvas.pen_zoom_factor).
    fixed_pen_size: bool = False


@dataclass
class StreamConfig:
    active_wf: str = "GC16"
    clean_wf: str = "GL16"
    flash_wf: str = "GC16"
    settle: float = 1.5
    fps: float = 15.0
    threshold: int = 8
    align: int = 8
    full_refresh_every: int = 0


@dataclass
class InjectConfig:
    mode: str = "canvas"
    width: int = 0          # 0 = resolved at startup by build_config
    height: int = 0
    rotation: int = 0       # 0/90/180/270; only meaningful for system injectors


@dataclass
class OutputConfig:
    save_png: Optional[str] = None
    save_pdf: Optional[str] = None
    save_json: Optional[str] = None
    autosave: float = 0.0
    pages_dir: Optional[str] = None


@dataclass
class AppConfig:
    kindle: KindleConfig = field(default_factory=KindleConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)
    stylus: StylusConfig = field(default_factory=StylusConfig)
    touch: TouchConfig = field(default_factory=TouchConfig)
    pen: PenConfig = field(default_factory=PenConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    canvas_width: int = 1860
    canvas_height: int = 2480
    # Drawing-buffer resolution as a multiple of the panel size (1..4).  The
    # zoom ceiling defaults to this factor: at that zoom one canvas pixel maps
    # to exactly one panel pixel, so the extra pixels buy sharpness, not blur.
    canvas_resolution: int = 4

    calibrate: bool = False
    no_input: bool = False
    freeze_ui: bool = False
    no_screensaver: bool = True
    verbose: int = 0