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
    tmp_png: str = "/tmp/_kindle_draw.png"


@dataclass
class PanelConfig:
    width: int = 1860
    height: int = 2480
    aspect: str = "fit"          # fit | crop | stretch
    invert: bool = False


@dataclass
class StylusConfig:
    dev: Optional[str] = None
    esz: Optional[int] = None
    cal_path: str = "~/.kindle_draw_cal.json"
    pressure_min: Optional[int] = None
    pressure_max: Optional[int] = None
    rotate: int = 0   # 0, 90, 180, 270 — clockwise, "hold the Scribe with
                      # its native top edge toward the given side"


@dataclass
class PenConfig:
    """Pen and eraser defaults. All overridable on the CLI and live from the GUI."""
    color: Tuple[int, int, int] = (0, 0, 0)
    size: float = 8.0                 # max stroke width in pixels
    eraser_size: float = 48.0
    pressure_sensitive: bool = True


@dataclass
class StreamConfig:
    active_wf: str = "DU"
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
    width: int = 0     # 0 = resolved at startup by build_config
    height: int = 0


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
    pen: PenConfig = field(default_factory=PenConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    canvas_width: int = 1860
    canvas_height: int = 2480

    calibrate: bool = False
    no_input: bool = False
    freeze_ui: bool = False
    no_screensaver: bool = True
    verbose: int = 0