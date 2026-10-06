"""Dataclasses that describe the runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KindleConfig:
    # Adjust these to your setup, or override with CLI flags.
    host: str = "192.168.1.14"
    port: int = 2022
    user: str = "root"
    key: Optional[str] = None
    # Optional SSH password. Only used if `sshpass` is on PATH; otherwise
    # the ControlMaster is established interactively (you get a prompt).
    # Prefer SSH keys. Env var KINDLE_SSH_PASSWORD is also honoured.
    password: Optional[str] = None
    fbink: str = "/mnt/us/libkh/bin/fbink"
    control_path: Optional[str] = None
    tmp_png: str = "/tmp/_kindle_draw.png"


@dataclass
class PanelConfig:
    width: int = 1872
    height: int = 4960
    aspect: str = "fit"          # fit | crop | stretch
    invert: bool = False


@dataclass
class StylusConfig:
    dev: Optional[str] = None
    esz: Optional[int] = None
    cal_path: str = "~/.kindle_draw_cal.json"
    pressure_min: Optional[int] = None
    pressure_max: Optional[int] = None


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