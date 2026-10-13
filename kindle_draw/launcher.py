"""GUI launcher for kindle_draw.

Opens a config window, spawns the drawing tool as a subprocess, and
streams its output into a log panel. The CLI stays the single source of
truth; the launcher just builds the argv and shows what's happening.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .gui import (
    ACCENT, ACCENT_HOVER, BG_INPUT, BG_INPUT_HOVER, BG_SIDEBAR, BG_WINDOW,
    FG_MUTED, FG_PRIMARY, FG_SECONDARY, SEPARATOR,
)


# --------------------------------------------------------------------------- #
# Stylesheet                                                                 #
# --------------------------------------------------------------------------- #

_QSS = f"""
QWidget {{
    background-color: {BG_WINDOW};
    color: {FG_PRIMARY};
    font-family: "Inter", "SF Pro Text", "Segoe UI", "Cantarell",
                 "Ubuntu", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 10pt;
}}

QMainWindow {{
    background-color: {BG_WINDOW};
}}

QLabel#titleLabel {{
    color: {FG_PRIMARY};
    font-size: 16pt;
    font-weight: 700;
}}

QLabel#subtitle {{
    color: {FG_MUTED};
    font-size: 9pt;
}}

QGroupBox {{
    background-color: {BG_SIDEBAR};
    border: 1px solid {SEPARATOR};
    border-radius: 8px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
    color: {FG_SECONDARY};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    background-color: {BG_WINDOW};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {BG_INPUT};
    color: {FG_PRIMARY};
    border: 1px solid {SEPARATOR};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}

QLineEdit:read-only {{
    color: {FG_MUTED};
}}

QComboBox::drop-down {{
    border: none;
    width: 18px;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {FG_SECONDARY};
    margin-right: 6px;
}}

QComboBox QAbstractItemView {{
    background-color: {BG_INPUT};
    color: {FG_PRIMARY};
    border: 1px solid {SEPARATOR};
    selection-background-color: {ACCENT};
    outline: none;
}}

QPushButton {{
    background-color: {BG_INPUT};
    color: {FG_PRIMARY};
    border: none;
    border-radius: 6px;
    padding: 7px 14px;
}}

QPushButton:hover {{
    background-color: {BG_INPUT_HOVER};
}}

QPushButton:disabled {{
    color: {FG_MUTED};
    background-color: {BG_SIDEBAR};
}}

QPushButton#primary {{
    background-color: {ACCENT};
    color: #ffffff;
    font-weight: 600;
    padding: 9px 18px;
}}

QPushButton#primary:hover {{
    background-color: {ACCENT_HOVER};
}}

QPushButton#primary:disabled {{
    background-color: #3a4a7a;
    color: #8888aa;
}}

QPushButton#stop {{
    background-color: #b03030;
    color: #ffffff;
    font-weight: 600;
    padding: 9px 18px;
}}

QPushButton#stop:hover {{
    background-color: #cf4040;
}}

QCheckBox {{
    color: {FG_PRIMARY};
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    background: {BG_INPUT};
    border: 1px solid {SEPARATOR};
}}

QCheckBox::indicator:hover {{
    background: {BG_INPUT_HOVER};
}}

QCheckBox::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
}}

QPlainTextEdit {{
    background-color: #101012;
    color: #c8c8d0;
    border: 1px solid {SEPARATOR};
    border-radius: 8px;
    font-family: "JetBrains Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 9pt;
    padding: 6px;
}}

QSplitter::handle {{
    background-color: {SEPARATOR};
    height: 2px;
}}

/* The config scroll area must blend into the window background; Qt would
   otherwise paint its viewport with the palette's base colour. */
QScrollArea {{
    background: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: {BG_WINDOW};
    width: 10px;
    height: 10px;
    border: none;
    margin: 0;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {BG_INPUT};
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}}

QScrollBar::handle:hover {{
    background: {BG_INPUT_HOVER};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; width: 0; border: none; background: none;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}
"""


# --------------------------------------------------------------------------- #
# Config file path                                                           #
# --------------------------------------------------------------------------- #

CONFIG_PATH = os.path.expanduser("~/.kindle_draw_launcher.json")


# --------------------------------------------------------------------------- #
# Launcher                                                                   #
# --------------------------------------------------------------------------- #

class Launcher(QMainWindow):
    process_ended = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Kindle Draw — Launcher")
        # The config area is scrollable, so the window can shrink well below
        # the natural size of its widgets.  Clamp the initial size to the
        # available screen geometry so the window (and the log pane) fits on
        # small or low-resolution displays.
        self.setMinimumSize(620, 480)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1040, int(screen.width() * 0.92)),
                    min(800, int(screen.height() * 0.92)))

        self._proc: Optional[subprocess.Popen] = None
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(60)
        self._log_timer.timeout.connect(self._drain_log)
        self._log_timer.start()

        self.process_ended.connect(self._on_process_ended)

        self._build_ui()
        self._load_config_into_ui(self._read_config_from_disk())

        QShortcut(QKeySequence("Ctrl+Return"), self,
                  activated=self._on_start)
        QShortcut(QKeySequence("Ctrl+S"), self,
                  activated=self._on_save_config)

    # ------------------------------------------------------------------ #
    # Layout                                                             #
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        # --- header ---------------------------------------------------- #
        head = QWidget()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(2)
        t = QLabel("Kindle Draw")
        t.setObjectName("titleLabel")
        hl.addWidget(t)
        s = QLabel("Configure a session, then start it. The log below "
                   "shows the tool's output in real time.")
        s.setObjectName("subtitle")
        hl.addWidget(s)
        outer.addWidget(head)

        # --- splitter: config above, log below ------------------------- #
        splitter = QSplitter(Qt.Vertical)
        outer.addWidget(splitter, 1)

        # The config boxes live inside a scroll area: on short screens the
        # settings shrink to a scrollable pane instead of forcing the window
        # taller than the desktop and squeezing the log out of view.
        config_area = QWidget()
        config_scroll = QScrollArea()
        config_scroll.setWidget(config_area)
        config_scroll.setWidgetResizable(True)
        config_scroll.setFrameShape(QFrame.NoFrame)
        splitter.addWidget(config_scroll)

        log_area = QWidget()
        splitter.addWidget(log_area)

        # Give both panes a fair share when the window is resized, and keep
        # the log from being collapsed to nothing by the drag handle.
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([420, 280])

        # --- config grid ---------------------------------------------- #
        grid = QGridLayout(config_area)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)

        grid.addWidget(self._build_kindle_box(),   0, 0)
        grid.addWidget(self._build_pen_box(),      0, 1)
        grid.addWidget(self._build_mode_box(),     1, 0)
        grid.addWidget(self._build_view_box(),     1, 1)
        grid.addWidget(self._build_output_box(),   2, 0)
        grid.addWidget(self._build_advanced_box(), 2, 1)
        # Pinch zoom spans both columns: it only applies to canvas mode and
        # groups naturally under the mode/output boxes.
        grid.addWidget(self._build_touch_box(),    3, 0, 1, 2)

        # --- log ------------------------------------------------------- #
        log_layout = QVBoxLayout(log_area)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)

        log_head = QHBoxLayout()
        log_head.addWidget(QLabel("Log"))
        log_head.addStretch(1)
        self._clear_log_btn = QPushButton("Clear log")
        self._clear_log_btn.clicked.connect(self._on_clear_log)
        log_head.addWidget(self._clear_log_btn)
        log_layout.addLayout(log_head)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(4000)
        # Hard floor so the log always keeps a readable slice, even when
        # the window is dragged to its minimum size.
        self._log.setMinimumHeight(140)
        log_layout.addWidget(self._log, 1)

        # --- action bar ------------------------------------------------- #
        actions = QHBoxLayout()
        actions.setSpacing(8)

        load_btn = QPushButton("Load config…")
        load_btn.clicked.connect(self._on_load_config)
        actions.addWidget(load_btn)

        save_btn = QPushButton("Save config…")
        save_btn.clicked.connect(self._on_save_config)
        actions.addWidget(save_btn)

        actions.addStretch(1)

        self._status_label = QLabel("idle")
        self._status_label.setStyleSheet(f"color: {FG_MUTED};")
        actions.addWidget(self._status_label)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("stop")
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        actions.addWidget(self._stop_btn)

        self._start_btn = QPushButton("Start drawing")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._on_start)
        actions.addWidget(self._start_btn)

        outer.addLayout(actions)

    # ------------------------------------------------------------------ #
    # Group boxes                                                        #
    # ------------------------------------------------------------------ #

    def _build_kindle_box(self) -> QGroupBox:
        box = QGroupBox("Kindle")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        self._host = QLineEdit()
        self._host.setPlaceholderText("192.168.1.14")
        form.addRow("Host", self._host)

        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(2022)
        form.addRow("Port", self._port)

        self._user = QLineEdit("root")
        form.addRow("User", self._user)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setPlaceholderText(
            "optional — SSH keys recommended")
        form.addRow("Password", self._password)
        self._key = self._path_row(
            form, "SSH key", mode="open",
            filt="All files (*)")
        self._key.setPlaceholderText("optional — e.g. ~/.ssh/kindle_scribe")

        self._remember_password = QCheckBox("Remember password (insecure)")
        form.addRow("", self._remember_password)


        self._fbink = QLineEdit()
        self._fbink.setPlaceholderText("fbink")
        form.addRow("fbink path", self._fbink)

        return box

    def _build_pen_box(self) -> QGroupBox:
        box = QGroupBox("Pen")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        colour_row = QWidget()
        ch = QHBoxLayout(colour_row)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(6)
        self._pen_color = QLineEdit("#000000")
        self._pen_color.setMaxLength(9)
        ch.addWidget(self._pen_color, 1)
        presets = QComboBox()
        presets.addItems([
            "black", "dark grey", "mid grey", "light grey",
            "white", "red", "green", "blue", "orange",
            "purple", "yellow",
        ])
        presets.currentTextChanged.connect(self._on_colour_preset)
        ch.addWidget(presets)
        form.addRow("Colour", colour_row)

        self._pen_size = QDoubleSpinBox()
        self._pen_size.setRange(0.5, 120.0)
        self._pen_size.setSingleStep(0.5)
        self._pen_size.setValue(8.0)
        self._pen_size.setSuffix(" px")
        form.addRow("Size", self._pen_size)

        self._eraser_size = QDoubleSpinBox()
        self._eraser_size.setRange(2.0, 300.0)
        self._eraser_size.setSingleStep(2.0)
        self._eraser_size.setValue(48.0)
        self._eraser_size.setSuffix(" px")
        form.addRow("Eraser size", self._eraser_size)

        self._pressure = QCheckBox("Pressure sensitive")
        self._pressure.setChecked(True)
        form.addRow("", self._pressure)

        self._pen_zoom = QCheckBox("Scale pen size with zoom (log)")
        self._pen_zoom.setChecked(True)
        self._pen_zoom.setToolTip(
            "Keep the pen visually useful while zoomed in: its on-screen\n"
            "width grows with 1 + ln(zoom) instead of shrinking with the\n"
            "view. Uncheck for a constant on-screen pen size\n"
            "(--fixed-pen-size).")
        form.addRow("", self._pen_zoom)

        return box

    def _build_mode_box(self) -> QGroupBox:
        box = QGroupBox("Mode")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        self._inject = QComboBox()
        self._inject.addItems(["canvas", "uinput", "none"])
        self._inject.currentTextChanged.connect(self._on_inject_changed)
        form.addRow("Inject", self._inject)

        self._inject_size = QLineEdit("auto")
        self._inject_size.setPlaceholderText("auto, or WxH e.g. 1920x1080")
        form.addRow("Target size", self._inject_size)

        self._rotate = QComboBox()
        self._rotate.addItems(["auto", "0", "90", "180", "270"])
        form.addRow("Rotation", self._rotate)

        self._sticky = QCheckBox("Sticky click (barrel button toggles)")
        form.addRow("", self._sticky)

        self._no_letterbox = QCheckBox("Stretch pen to fill screen")
        self._no_letterbox.setToolTip(
            "When on, the Scribe's corners map to the screen's corners "
            "(shape distorted). When off, aspect ratio is preserved and "
            "the pen only reaches the central rectangle of the screen.")
        form.addRow("", self._no_letterbox)

        return box

    def _build_view_box(self) -> QGroupBox:
        box = QGroupBox("View")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        self._open_gui = QCheckBox("Open drawing window on the host")
        self._open_gui.setChecked(True)
        form.addRow("", self._open_gui)

        self._cursor = QComboBox()
        self._cursor.addItems(["off", "hover", "always"])
        form.addRow("Kindle cursor", self._cursor)

        self._freeze_ui = QCheckBox("Freeze Kindle UI while running")
        form.addRow("", self._freeze_ui)

        self._active_wf = QComboBox()
        self._active_wf.addItems(
            ["DU", "A2", "GL16", "GC16", "GC16_FAST", "DU4"])
        form.addRow("Active waveform", self._active_wf)

        return box

    def _build_output_box(self) -> QGroupBox:
        box = QGroupBox("Output")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        self._save_png = self._path_row(
            form, "Save PNG", mode="save", filt="PNG (*.png)")
        self._save_pdf = self._path_row(
            form, "Save PDF", mode="save", filt="PDF (*.pdf)")
        self._pages_dir = self._path_row(
            form, "Pages dir", mode="dir")

        self._autosave = QDoubleSpinBox()
        self._autosave.setRange(0.0, 3600.0)
        self._autosave.setSingleStep(5.0)
        self._autosave.setSuffix(" s")
        form.addRow("Autosave", self._autosave)

        return box

    def _build_advanced_box(self) -> QGroupBox:
        box = QGroupBox("Advanced")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        self._clean_wf = QComboBox()
        self._clean_wf.addItems(["GL16", "GC16", "DU"])
        form.addRow("Clean waveform", self._clean_wf)

        self._flash_wf = QComboBox()
        self._flash_wf.addItems(["GC16", "GL16"])
        form.addRow("Flash waveform", self._flash_wf)

        self._fps = QDoubleSpinBox()
        self._fps.setRange(1.0, 60.0)
        self._fps.setValue(15.0)
        form.addRow("Max FPS", self._fps)

        self._threshold = QSpinBox()
        self._threshold.setRange(0, 255)
        self._threshold.setValue(8)
        form.addRow("Diff threshold", self._threshold)

        self._verbose = QCheckBox("Verbose logging (-v)")
        form.addRow("", self._verbose)

        return box

    def _build_touch_box(self) -> QGroupBox:
        """Pinch-zoom / pan controls, mirroring the CLI's Touch group.

        Maps to ``--pinch``, ``--touch-pan``, ``--max-zoom``,
        ``--min-spread``, ``--max-finger-size``, ``--touch-dev`` and
        ``--touch-range``.  The box is only enabled in
        canvas mode because the gesture manipulates the host-side canvas
        viewport (the CLI warns and ignores ``--pinch`` in other modes).
        """
        box = QGroupBox("Pinch zoom (canvas mode)")
        box.setToolTip(
            "Read the Kindle's touchscreen over a second SSH stream and\n"
            "scale/pan the drawing canvas with two fingers.\n"
            "Reset the view to 1:1 with:  kill -USR2 <pid>")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setContentsMargins(12, 8, 12, 8)
        form.setSpacing(6)

        self._pinch = QCheckBox("Enable pinch zoom (2-finger scale)")
        self._pinch.setToolTip(
            "Two fingers on the Kindle screen scale the canvas viewport; a\n"
            "two-finger drag pans it. The stylus draws what is visible\n"
            "under its tip (brush widths stay in canvas units, so strokes\n"
            "look thicker on screen while zoomed in).")
        self._pinch.toggled.connect(self._on_pinch_toggled)
        form.addRow("", self._pinch)

        self._touch_pan = QCheckBox("Single-finger pan too")
        self._touch_pan.setToolTip(
            "Also pan with one finger. Off by default so stylus-induced\n"
            "single touches do not move the view.")
        form.addRow("", self._touch_pan)

        self._canvas_res = QComboBox()
        self._canvas_res.addItems(["1x", "2x", "3x", "4x"])
        self._canvas_res.setCurrentIndex(3)  # 4x: canvas = 4x the panel
        self._canvas_res.setToolTip(
            "Canvas resolution vs. the screen (--canvas-resolution).\n"
            "4x means a 7440x9920 canvas: zooming to 4x shows it 1:1, so\n"
            "strokes stay sharp at max zoom. ~221 MB RAM at 4x.\n"
            "The zoom limit follows this factor automatically.")
        self._canvas_res.currentIndexChanged.connect(self._on_resolution_changed)
        form.addRow("Canvas resolution", self._canvas_res)

        self._max_zoom = QDoubleSpinBox()
        self._max_zoom.setRange(1.5, 20.0)
        self._max_zoom.setSingleStep(0.5)
        self._max_zoom.setValue(4.0)  # auto-synced to the resolution factor
        self._max_zoom.setSuffix("x")
        self._max_zoom.setToolTip(
            "Maximum zoom factor (--max-zoom). Kept in sync with the\n"
            "canvas resolution above; change it afterwards to override.")
        form.addRow("Max zoom", self._max_zoom)

        self._min_spread = QDoubleSpinBox()
        self._min_spread.setRange(0.0, 1000.0)
        self._min_spread.setSingleStep(10.0)
        self._min_spread.setValue(60.0)
        self._min_spread.setToolTip(
            "Minimum distance between the two fingers (--min-spread),\n"
            "in raw touch-device units, before a two-finger gesture is\n"
            "recognised at all.  Raise it if close-together fingers\n"
            "trigger pinch/pan by accident; lower it for a tighter pinch.")
        form.addRow("Min finger distance", self._min_spread)

        self._max_finger_size = QDoubleSpinBox()
        self._max_finger_size.setRange(0.0, 1000.0)
        self._max_finger_size.setSingleStep(10.0)
        self._max_finger_size.setValue(100.0)
        self._max_finger_size.setToolTip(
            "Ignore touch contacts bigger than this (--max-finger-size),\n"
            "in raw touch-device units, so a hand resting on the screen\n"
            "cannot zoom or pan.  Lower it if palm rests still zoom; raise\n"
            "it if real fingers get ignored; 0 disables the check.  Only\n"
            "applies to drivers that report contact sizes (check evtest).")
        form.addRow("Max finger size", self._max_finger_size)

        self._touch_dev = QLineEdit()
        self._touch_dev.setPlaceholderText("auto — e.g. /dev/input/event2")
        self._touch_dev.setToolTip(
            "Touchscreen event device. Leave empty to auto-detect the\n"
            "device that reports ABS_MT_* multitouch events.")
        form.addRow("Touch device", self._touch_dev)

        self._touch_range = QLineEdit()
        self._touch_range.setPlaceholderText("auto, or XMIN XMAX YMIN YMAX")
        self._touch_range.setToolTip(
            "Raw touchscreen coordinate range used to map finger deltas to\n"
            "canvas units. Leave empty to infer it from observed events;\n"
            "set the four numbers if autodetection feels off.")
        form.addRow("Touch range", self._touch_range)

        # Sub-options only matter while pinch is enabled.
        self._on_pinch_toggled(False)
        return box

    def _path_row(self, form: QFormLayout, label: str,
                  mode: str = "save", filt: str = "") -> QLineEdit:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        line = QLineEdit()
        h.addWidget(line, 1)
        btn = QPushButton("…")
        btn.setFixedWidth(32)

        def pick():
            if mode == "dir":
                path = QFileDialog.getExistingDirectory(self, label)
            elif mode == "open":
                path, _ = QFileDialog.getOpenFileName(
                    self, label, line.text() or "", filt)
            else:
                path, _ = QFileDialog.getSaveFileName(
                    self, label, line.text() or "", filt)
            if path:
                line.setText(path)

        btn.clicked.connect(pick)
        h.addWidget(btn)
        form.addRow(label, row)
        return line

    # ------------------------------------------------------------------ #
    # Preset colours                                                     #
    # ------------------------------------------------------------------ #

    def _on_colour_preset(self, name: str) -> None:
        mapping = {
            "black":      "#000000",
            "dark grey":  "#404040",
            "mid grey":   "#808080",
            "light grey": "#c0c0c0",
            "white":      "#ffffff",
            "red":        "#a01e1e",
            "green":      "#1e7a1e",
            "blue":       "#1e3ca0",
            "orange":     "#a05a00",
            "purple":     "#5a1e78",
            "yellow":     "#a08c00",
        }
        if name in mapping:
            self._pen_color.setText(mapping[name])

    def _on_inject_changed(self, mode: str) -> None:
        system = mode in ("uinput",)
        self._inject_size.setEnabled(system or mode == "none")
        self._rotate.setEnabled(system)
        self._sticky.setEnabled(system)

        # Pinch zoom only manipulates the canvas-mode viewport; the CLI
        # warns and ignores --pinch in the other modes, so the whole box
        # follows the inject selector (the checked state is preserved in
        # case the user switches back to canvas).
        self._pinch.setEnabled(mode == "canvas")
        self._on_pinch_toggled(mode == "canvas" and self._pinch.isChecked())

    def _on_pinch_toggled(self, on: bool) -> None:
        """Gate the pinch sub-options behind the main --pinch checkbox."""
        for widget in (self._touch_pan, self._max_zoom, self._min_spread,
                       self._max_finger_size, self._touch_dev,
                       self._touch_range):
            widget.setEnabled(on)

    def _on_resolution_changed(self, index: int) -> None:
        """Mirror the CLI's resolution-linked zoom default into the spinbox.

        The CLI defaults the zoom ceiling to the canvas resolution factor
        (1:1 sharp pixels at max zoom). The launcher reproduces that by
        pushing the new resolution into the max-zoom spinbox; the user can
        still change the spinbox afterwards to override it. The resolution
        combo itself is never gated by the pinch checkbox: it sizes the
        canvas in every mode, so higher resolution also means sharper
        exported pages even without zooming.
        """
        self._max_zoom.setValue(float(index + 1))

    # ------------------------------------------------------------------ #
    # Config I/O                                                         #
    # ------------------------------------------------------------------ #

    def _collect_config(self) -> dict:
        cfg = {
            "host": self._host.text().strip(),
            "port": self._port.value(),
            "key": self._key.text().strip(),
            "user": self._user.text().strip(),
            "fbink": self._fbink.text().strip(),
            "inject": self._inject.currentText(),
            "inject_size": self._inject_size.text().strip() or "auto",
            "rotate": self._rotate.currentText(),
            "sticky_click": self._sticky.isChecked(),
            "pen_color": self._pen_color.text().strip(),
            "pen_size": self._pen_size.value(),
            "eraser_size": self._eraser_size.value(),
            "pressure_sensitive": self._pressure.isChecked(),
            "fixed_pen_size": not self._pen_zoom.isChecked(),
            "open_gui": self._open_gui.isChecked(),
            "cursor": self._cursor.currentText(),
            "freeze_ui": self._freeze_ui.isChecked(),
            "active_wf": self._active_wf.currentText(),
            "save_png": self._save_png.text().strip(),
            "save_pdf": self._save_pdf.text().strip(),
            "pages_dir": self._pages_dir.text().strip(),
            "autosave": self._autosave.value(),
            "clean_wf": self._clean_wf.currentText(),
            "flash_wf": self._flash_wf.currentText(),
            "fps": self._fps.value(),
            "threshold": self._threshold.value(),
            "no_letterbox": self._no_letterbox.isChecked(),
            "verbose": self._verbose.isChecked(),
            "pinch": self._pinch.isChecked(),
            "touch_pan": self._touch_pan.isChecked(),
            "canvas_resolution": self._canvas_res.currentIndex() + 1,
            "max_zoom": self._max_zoom.value(),
            "min_spread": self._min_spread.value(),
            "max_finger_size": self._max_finger_size.value(),
            "touch_dev": self._touch_dev.text().strip(),
            "touch_range": self._touch_range.text().strip(),
        }
        if self._remember_password.isChecked() and self._password.text():
            cfg["password"] = self._password.text()
        return cfg

    def _load_config_into_ui(self, cfg: dict) -> None:
        if not cfg:
            return

        def s(key, widget, default=""):
            widget.setText(str(cfg.get(key, default) or ""))

        def b(key, widget, default=False):
            widget.setChecked(bool(cfg.get(key, default)))

        self._host.setText(cfg.get("host", ""))
        self._port.setValue(int(cfg.get("port", 2022)))
        self._user.setText(cfg.get("user", "root"))
        self._key.setText(cfg.get("key", ""))
        self._fbink.setText(cfg.get("fbink", ""))

        if "password" in cfg:
            self._password.setText(cfg["password"])
            self._remember_password.setChecked(True)

        idx = self._inject.findText(cfg.get("inject", "canvas"))
        if idx >= 0:
            self._inject.setCurrentIndex(idx)
        self._inject_size.setText(cfg.get("inject_size", "auto"))

        idx = self._rotate.findText(str(cfg.get("rotate", "auto")))
        if idx >= 0:
            self._rotate.setCurrentIndex(idx)

        b("sticky_click", self._sticky)
        self._pen_color.setText(cfg.get("pen_color", "#000000"))
        self._pen_size.setValue(float(cfg.get("pen_size", 8.0)))
        self._eraser_size.setValue(float(cfg.get("eraser_size", 48.0)))
        b("pressure_sensitive", self._pressure, True)
        # fixed_pen_size=True means the log pen-scaling checkbox is off.
        self._pen_zoom.setChecked(not bool(cfg.get("fixed_pen_size", False)))

        b("open_gui", self._open_gui, True)
        b("no_letterbox", self._no_letterbox)

        idx = self._cursor.findText(cfg.get("cursor", "off"))
        if idx >= 0:
            self._cursor.setCurrentIndex(idx)

        b("freeze_ui", self._freeze_ui)

        idx = self._active_wf.findText(cfg.get("active_wf", "DU"))
        if idx >= 0:
            self._active_wf.setCurrentIndex(idx)

        self._save_png.setText(cfg.get("save_png", ""))
        self._save_pdf.setText(cfg.get("save_pdf", ""))
        self._pages_dir.setText(cfg.get("pages_dir", ""))
        self._autosave.setValue(float(cfg.get("autosave", 0.0)))

        idx = self._clean_wf.findText(cfg.get("clean_wf", "GL16"))
        if idx >= 0:
            self._clean_wf.setCurrentIndex(idx)

        idx = self._flash_wf.findText(cfg.get("flash_wf", "GC16"))
        if idx >= 0:
            self._flash_wf.setCurrentIndex(idx)

        self._fps.setValue(float(cfg.get("fps", 15.0)))
        self._threshold.setValue(int(cfg.get("threshold", 8)))
        b("verbose", self._verbose)

        # Touch controls (absent from configs saved before pinch zoom
        # existed) fall back to the CLI defaults.
        #
        # The resolution combo is restored first: its change handler syncs
        # the max-zoom spinbox to the resolution factor, and a saved
        # max_zoom override is then applied on top of that.
        try:
            resolution = int(cfg.get("canvas_resolution", 4))
        except (TypeError, ValueError):
            resolution = 4
        self._canvas_res.setCurrentIndex(min(max(resolution, 1), 4) - 1)
        b("pinch", self._pinch)
        b("touch_pan", self._touch_pan)
        self._max_zoom.setValue(float(cfg.get("max_zoom", 4.0)))
        self._min_spread.setValue(float(cfg.get("min_spread", 60.0)))
        self._max_finger_size.setValue(float(cfg.get("max_finger_size", 100.0)))
        self._touch_dev.setText(str(cfg.get("touch_dev", "") or ""))
        self._touch_range.setText(str(cfg.get("touch_range", "") or ""))

        self._on_inject_changed(self._inject.currentText())

    def _read_config_from_disk(self) -> dict:
        try:
            with open(CONFIG_PATH, "r") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _on_load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load config", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r") as fh:
                self._load_config_into_ui(json.load(fh))
            self._append_log(f"[launcher] loaded {path}\n")
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", str(exc))

    def _on_save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save config", CONFIG_PATH, "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w") as fh:
                json.dump(self._collect_config(), fh, indent=2)
            self._append_log(f"[launcher] saved {path}\n")
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    # ------------------------------------------------------------------ #
    # Build argv                                                         #
    # ------------------------------------------------------------------ #

    def _build_argv(self) -> Optional[List[str]]:
        cfg = self._collect_config()

        if not cfg["host"]:
            QMessageBox.warning(self, "Missing host",
                                "Please enter the Kindle's hostname or IP.")
            return None

        args = [
            sys.executable, "-m", "kindle_draw",
            "--kindle", cfg["host"],
            "--port", str(cfg["port"]),
            "--user", cfg["user"],
        ]

        if cfg.get("key"):
            args += ["--key", os.path.expanduser(cfg["key"])]
        
        if cfg["fbink"]:
            args += ["--fbink", cfg["fbink"]]
        if cfg["inject"]:
            args += ["--inject", cfg["inject"]]
        if cfg["inject"] != "canvas":
            args += ["--inject-size", cfg["inject_size"]]
            args += ["--rotate", cfg["rotate"]]
            if cfg["sticky_click"]:
                args += ["--sticky-click"]
            if cfg.get("no_letterbox"):
                args += ["--no-letterbox"]
        if cfg["pen_color"]:
            args += ["--pen-color", cfg["pen_color"]]
        args += ["--pen-size", str(cfg["pen_size"])]
        args += ["--eraser-size", str(cfg["eraser_size"])]
        if not cfg["pressure_sensitive"]:
            args += ["--no-pressure"]
        if cfg.get("fixed_pen_size"):
            args += ["--fixed-pen-size"]
        # Canvas resolution sizes the drawing surface in every mode (and
        # sets the zoom ceiling), so it is always passed explicitly.
        args += ["--canvas-resolution", str(cfg.get("canvas_resolution", 4))]
        if cfg["cursor"] != "off":
            args += ["--cursor", cfg["cursor"]]
        if cfg["freeze_ui"]:
            args += ["--freeze-ui"]
        if cfg["active_wf"]:
            args += ["--active-wf", cfg["active_wf"]]
        if cfg["clean_wf"]:
            args += ["--clean-wf", cfg["clean_wf"]]
        if cfg["flash_wf"]:
            args += ["--flash-wf", cfg["flash_wf"]]
        args += ["--fps", str(cfg["fps"])]
        args += ["--threshold", str(cfg["threshold"])]
        if cfg["autosave"] > 0:
            args += ["--autosave", str(cfg["autosave"])]
        if cfg["save_png"]:
            args += ["--save-png", cfg["save_png"]]
        if cfg["save_pdf"]:
            args += ["--save-pdf", cfg["save_pdf"]]
        if cfg["pages_dir"]:
            args += ["--pages-dir", cfg["pages_dir"]]
        if cfg["open_gui"] and cfg["inject"] == "canvas":
            args += ["--gui"]

        # Pinch zoom is a canvas-mode feature (the CLI warns and ignores
        # --pinch in the other injection modes), so only emit it there.
        if cfg["pinch"] and cfg["inject"] == "canvas":
            args += ["--pinch"]
            if cfg["touch_pan"]:
                args += ["--touch-pan"]
            args += ["--max-zoom", str(cfg["max_zoom"])]
            args += ["--min-spread", str(cfg["min_spread"])]
            args += ["--max-finger-size", str(cfg["max_finger_size"])]
            if cfg["touch_dev"]:
                args += ["--touch-dev", cfg["touch_dev"]]
            # The range field is free text: empty or "auto" means let the
            # tool autodetect; otherwise it must be 1 int (max for both
            # axes) or 4 ints (XMIN XMAX YMIN YMAX), matching the CLI's
            # --touch-range argument.
            raw_range = str(cfg["touch_range"]).strip()
            if raw_range and raw_range.lower() != "auto":
                parts = raw_range.replace(",", " ").split()
                try:
                    nums = [int(p) for p in parts]
                except ValueError:
                    nums = []
                if len(nums) not in (1, 4):
                    QMessageBox.warning(
                        self, "Invalid touch range",
                        "Touch range must be empty (auto-detect), one "
                        "number (axis maximum), or four numbers: "
                        "XMIN XMAX YMIN YMAX.")
                    return None
                args += ["--touch-range"] + [str(n) for n in nums]

        if cfg["verbose"]:
            args += ["-v"]

        return args

    # ------------------------------------------------------------------ #
    # Start / stop                                                       #
    # ------------------------------------------------------------------ #

    def _on_start(self) -> None:
        if self._proc is not None:
            return

        # Refuse to start without some form of authentication.
        if not self._key.text().strip() and not self._password.text():
            answer = QMessageBox.question(
                self, "No SSH credentials",
                "Neither an SSH key nor a password is set. The tool will "
                "not be able to connect to the Kindle non-interactively "
                "when launched from the GUI.\n\n"
                "Start anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        argv = self._build_argv()
        if argv is None:
            return

        # Make sure the package is importable from the subprocess.
        try:
            import kindle_draw as _pkg
            pkg_parent = os.path.dirname(
                os.path.dirname(os.path.abspath(_pkg.__file__)))
        except Exception:
            pkg_parent = os.getcwd()

        env = os.environ.copy()
        env["PYTHONPATH"] = (pkg_parent + os.pathsep
                             + env.get("PYTHONPATH", ""))
        env["PYTHONUNBUFFERED"] = "1"

        pw = self._password.text()
        if pw:
            env["KINDLE_SSH_PASSWORD"] = pw

        self._append_log("\n" + "$ "
                         + " ".join(shlex.quote(a) for a in argv) + "\n")

        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=pkg_parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except Exception as exc:
            self._append_log(f"[launcher] failed to start: {exc}\n")
            self._proc = None
            return

        threading.Thread(target=self._read_output, daemon=True).start()
        self._set_running(True)

    def _read_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._log_queue.put(line)
        except Exception:
            pass
        code = proc.wait()
        self._log_queue.put(f"\n[launcher] process exited ({code})\n")
        self.process_ended.emit(code)

    def _on_stop(self) -> None:
        if self._proc is None:
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGINT)
        except ProcessLookupError:
            pass
        except Exception as exc:
            self._append_log(f"[launcher] stop failed: {exc}\n")

    def _on_process_ended(self, code: int) -> None:
        self._proc = None
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._status_label.setText("running" if running else "idle")
        self._status_label.setStyleSheet(
            f"color: {ACCENT if running else FG_MUTED};")

    # ------------------------------------------------------------------ #
    # Log drain                                                          #
    # ------------------------------------------------------------------ #

    def _drain_log(self) -> None:
        wrote = False
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
            wrote = True
        if wrote:
            self._log.verticalScrollBar().setValue(
                self._log.verticalScrollBar().maximum())

    def _append_log(self, text: str) -> None:
        cursor = self._log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._log.setTextCursor(cursor)
        self._log.insertPlainText(text)

    def _on_clear_log(self) -> None:
        self._log.clear()

    # ------------------------------------------------------------------ #
    # Close handling                                                     #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:
        if self._proc is not None:
            answer = QMessageBox.question(
                self, "Quit?",
                "A drawing session is running. Stop it and quit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._on_stop()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except Exception:
                    pass

        # Persist current settings for next launch (except password
        # unless the user explicitly asked to remember it).
        try:
            with open(CONFIG_PATH, "w") as fh:
                json.dump(self._collect_config(), fh, indent=2)
        except Exception:
            pass

        event.accept()


# --------------------------------------------------------------------------- #
# Entry point                                                                #
# --------------------------------------------------------------------------- #

def run_launcher() -> int:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName("Kindle Draw")
    app.setStyleSheet(_QSS)
    win = Launcher()
    win.show()
    return app.exec()