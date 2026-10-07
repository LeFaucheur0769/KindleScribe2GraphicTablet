"""Optional Qt6 (PySide6) window showing the canvas and letting you tune the pen.

Modern dark theme, native HiDPI, smooth pan/zoom. The preview only
re-renders when the canvas version, zoom level, or viewport size changed,
so idle CPU stays near zero.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PIL import Image

try:
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtGui import (
        QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
    )
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QColorDialog, QFileDialog, QFrame,
        QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QGridLayout,
        QHBoxLayout, QLabel, QMainWindow, QPushButton, QRadioButton,
        QSlider, QVBoxLayout, QWidget,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "kindle_draw.gui requires PySide6. Install with:\n"
        "    pip install PySide6\n"
        f"(import error: {exc})"
    ) from exc

from .canvas import PALETTE

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Palette                                                                    #
# --------------------------------------------------------------------------- #

BG_WINDOW      = "#1a1a1c"
BG_VIEWPORT    = "#141416"
BG_SIDEBAR     = "#232326"
BG_INPUT       = "#2f2f34"
BG_INPUT_HOVER = "#3a3a42"
BG_INPUT_PRESS = "#45454f"
FG_PRIMARY     = "#e8e8ec"
FG_SECONDARY   = "#9a9aa6"
FG_MUTED       = "#6a6a76"
ACCENT         = "#5b8cff"
ACCENT_HOVER   = "#7ba3ff"
SEPARATOR      = "#2e2e34"


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

QFrame#sidebar {{
    background-color: {BG_SIDEBAR};
    border-left: 1px solid {SEPARATOR};
}}

QLabel#titleLabel {{
    color: {FG_PRIMARY};
    font-size: 15pt;
    font-weight: 700;
}}

QLabel#subtitle {{
    color: {FG_MUTED};
    font-size: 8pt;
}}

QLabel#sectionHeader {{
    color: {FG_MUTED};
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 1px;
}}

QFrame#sectionRule {{
    background-color: {SEPARATOR};
    min-height: 1px;
    max-height: 1px;
}}

QLabel#valueLabel {{
    color: {FG_SECONDARY};
    font-family: "JetBrains Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 8pt;
}}

QLabel#statusLabel {{
    color: {FG_MUTED};
    font-family: "JetBrains Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 8pt;
}}

QPushButton {{
    background-color: {BG_INPUT};
    color: {FG_PRIMARY};
    border: none;
    border-radius: 6px;
    padding: 7px 12px;
    text-align: left;
}}

QPushButton:hover {{
    background-color: {BG_INPUT_HOVER};
}}

QPushButton:pressed {{
    background-color: {BG_INPUT_PRESS};
}}

QPushButton#nav {{
    background-color: {BG_INPUT};
    padding: 4px 10px;
    font-size: 12pt;
    text-align: center;
    min-width: 32px;
}}

QPushButton#nav:hover {{
    background-color: {BG_INPUT_HOVER};
}}

QSlider::groove:horizontal {{
    background: {BG_INPUT};
    height: 4px;
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {ACCENT};
    height: 4px;
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {ACCENT};
    width: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}

QSlider::handle:horizontal:hover {{
    background: {ACCENT_HOVER};
}}

QCheckBox, QRadioButton {{
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

QRadioButton::indicator {{
    width: 13px;
    height: 13px;
    border-radius: 7px;
    background: {BG_INPUT};
    border: 1px solid {SEPARATOR};
}}

QRadioButton::indicator:hover {{
    background: {BG_INPUT_HOVER};
}}

QRadioButton::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
}}

QGraphicsView {{
    background-color: {BG_VIEWPORT};
    border: none;
}}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: {BG_VIEWPORT};
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
    height: 0;
    width: 0;
    border: none;
    background: none;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}
"""


# --------------------------------------------------------------------------- #
# Helpers                                                                    #
# --------------------------------------------------------------------------- #

def _pil_to_pixmap(img: Image.Image) -> QPixmap:
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    data = img.tobytes("raw", "RGB")
    qimage = QImage(data, w, h, w * 3, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimage)


# --------------------------------------------------------------------------- #
# Swatch                                                                     #
# --------------------------------------------------------------------------- #

class Swatch(QWidget):
    clicked = Signal(tuple)

    def __init__(self, rgb, size: int = 26):
        super().__init__()
        self.rgb = tuple(rgb)
        self._size = size
        self._selected = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("#%02x%02x%02x" % self.rgb)

    def set_selected(self, yes: bool) -> None:
        if yes == self._selected:
            return
        self._selected = yes
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.rgb)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._selected:
            p.setPen(QPen(QColor(ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)
            inner = self.rect().adjusted(3, 3, -3, -3)
        else:
            inner = self.rect().adjusted(2, 2, -2, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*self.rgb))
        p.drawRoundedRect(inner, 4, 4)


# --------------------------------------------------------------------------- #
# Canvas view                                                                #
# --------------------------------------------------------------------------- #

class CanvasView(QGraphicsView):
    """Zoomable, pannable preview of the Pillow canvas."""

    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixitem = QGraphicsPixmapItem()
        self._scene.addItem(self._pixitem)

        self.setRenderHints(QPainter.Antialiasing |
                            QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.NoFrame)

        self._scale_mode = "fit"
        self._explicit_scale = 1.0
        self._effective_scale = 1.0
        self._last_key = None

    # -- API ------------------------------------------------------------ #

    def set_scale_mode(self, mode: str) -> None:
        self._scale_mode = mode
        if mode != "fit":
            try:
                self._explicit_scale = float(mode)
            except ValueError:
                self._explicit_scale = 1.0
        self._last_key = None
        self.refresh()

    def zoom_by(self, factor: float) -> None:
        new_scale = max(0.05, min(8.0, self._effective_scale * factor))
        self.set_scale_mode(f"{new_scale:.4f}")

    def refresh(self) -> None:
        canvas = self.pages.current()

        vw = max(1, self.viewport().width())
        vh = max(1, self.viewport().height())

        if self._scale_mode == "fit":
            scale = min(vw / canvas.width, vh / canvas.height) * 0.94
        else:
            scale = self._explicit_scale
        self._effective_scale = scale

        key = (canvas.version, round(scale, 6), id(canvas))
        if key == self._last_key:
            return
        self._last_key = key

        target_w = max(1, int(canvas.width * scale))
        target_h = max(1, int(canvas.height * scale))
        img = canvas.snapshot().resize((target_w, target_h), Image.LANCZOS)
        pm = _pil_to_pixmap(img)
        self._pixitem.setPixmap(pm)
        self._scene.setSceneRect(0, 0, pm.width(), pm.height())
        self.setSceneRect(self._scene.sceneRect())

    def invalidate(self) -> None:
        self._last_key = None

    # -- events --------------------------------------------------------- #

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._scale_mode == "fit":
            self._last_key = None

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.15 if delta > 0 else (1.0 / 1.15)
        self.zoom_by(factor)
        event.accept()


# --------------------------------------------------------------------------- #
# Main window                                                                #
# --------------------------------------------------------------------------- #

class DrawingWindow:
    SIDEBAR_WIDTH = 260

    def __init__(self, pages, streamer, injector=None) -> None:
        self.pages = pages
        self.streamer = streamer
        self.injector = injector
        self._closed = False

        # Own the QApplication. If one already exists (e.g. we're inside
        # an existing Qt app), reuse it.
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication(sys.argv)
        self.app.setApplicationName("Kindle Draw")
        self.app.setStyleSheet(_QSS)

        self.win = QMainWindow()
        self.win.setWindowTitle("Kindle Draw")
        self.win.setMinimumSize(720, 520)

        screen = self.app.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(1400, max(900, avail.width() - 80))
            h = min(900, max(600, avail.height() - 80))
            self.win.resize(w, h)

        # --- central layout ------------------------------------------- #
        central = QWidget()
        self.win.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas_view = CanvasView(pages)
        root.addWidget(self.canvas_view, 1)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(self.SIDEBAR_WIDTH)
        root.addWidget(sidebar)

        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 16, 16, 12)
        side.setSpacing(0)

        # --- title ---------------------------------------------------- #
        title = QLabel("Kindle Draw")
        title.setObjectName("titleLabel")
        side.addWidget(title)

        self._subtitle = QLabel("")
        self._subtitle.setObjectName("subtitle")
        side.addWidget(self._subtitle)
        side.addSpacing(14)

        # --- Pen section ---------------------------------------------- #
        self._section(side, "Pen")

        swatch_grid = QGridLayout()
        swatch_grid.setContentsMargins(0, 0, 0, 0)
        swatch_grid.setSpacing(4)
        self._swatches = []
        for i, rgb in enumerate(PALETTE):
            s = Swatch(rgb)
            s.clicked.connect(self._set_color)
            swatch_grid.addWidget(s, i // 5, i % 5)
            self._swatches.append(s)
        side.addLayout(swatch_grid)
        side.addSpacing(8)

        colour_row = QWidget()
        cr = QHBoxLayout(colour_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(8)
        custom = QPushButton("Custom colour…")
        custom.clicked.connect(self._pick_color)
        cr.addWidget(custom, 1)
        self._color_label = QLabel("")
        self._color_label.setObjectName("valueLabel")
        cr.addWidget(self._color_label)
        side.addWidget(colour_row)
        side.addSpacing(12)


        pen_head = QWidget()
        ph = QHBoxLayout(pen_head)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self._small("Size"))
        ph.addStretch(1)
        self._pen_size_label = QLabel("")
        self._pen_size_label.setObjectName("valueLabel")
        ph.addWidget(self._pen_size_label)
        side.addWidget(pen_head)

        self._pen_size_slider = QSlider(Qt.Horizontal)
        self._pen_size_slider.setRange(5, 600)   # 0.5 .. 60.0 px
        self._pen_size_slider.setValue(80)
        self._pen_size_slider.valueChanged.connect(self._on_pen_size)
        side.addWidget(self._pen_size_slider)
        side.addSpacing(14)

        # --- Eraser section ------------------------------------------- #
        self._section(side, "Eraser")

        eraser_head = QWidget()
        eh = QHBoxLayout(eraser_head)
        eh.setContentsMargins(0, 0, 0, 0)
        eh.addWidget(self._small("Size"))
        eh.addStretch(1)
        self._eraser_size_label = QLabel("")
        self._eraser_size_label.setObjectName("valueLabel")
        eh.addWidget(self._eraser_size_label)
        side.addWidget(eraser_head)

        self._eraser_size_slider = QSlider(Qt.Horizontal)
        self._eraser_size_slider.setRange(20, 2000)  # 2 .. 200 px
        self._eraser_size_slider.setValue(480)
        self._eraser_size_slider.valueChanged.connect(self._on_eraser_size)
        side.addWidget(self._eraser_size_slider)
        side.addSpacing(14)

        # --- Behaviour ------------------------------------------------ #
        self._section(side, "Behaviour")

        self._pressure_cb = QCheckBox("Pressure sensitive")
        self._pressure_cb.setChecked(True)
        self._pressure_cb.toggled.connect(self._on_pressure_toggle)
        side.addWidget(self._pressure_cb)

        self._sticky_cb: Optional[QCheckBox] = None
        if injector is not None and hasattr(injector, "toggle_sticky_click"):
            self._sticky_cb = QCheckBox("Sticky click (barrel button)")
            self._sticky_cb.setChecked(
                bool(getattr(injector, "sticky_click", False)))
            self._sticky_cb.toggled.connect(self._on_sticky_toggle)
            side.addWidget(self._sticky_cb)

        side.addSpacing(14)

        # --- View ----------------------------------------------------- #
        self._section(side, "View")

        self._view_radios = []
        for text, val in (("Fit to window", "fit"),
                          ("100 %", "1.0"),
                          ("50 %", "0.5"),
                          ("25 %", "0.25")):
            rb = QRadioButton(text)
            rb.setProperty("scale_mode", val)
            rb.toggled.connect(lambda on, v=val: self._on_scale_mode(on, v))
            if val == "fit":
                rb.setChecked(True)
            side.addWidget(rb)
            self._view_radios.append(rb)

        side.addSpacing(14)

        # --- Actions -------------------------------------------------- #
        self._section(side, "Actions")
        for text, cb in (("Save PNG…", self._save_png),
                         ("Save PDF…", self._save_pdf),
                         ("Clear canvas", self._clear)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            side.addWidget(b)
            side.addSpacing(4)

        side.addSpacing(10)

        # --- Pages ---------------------------------------------------- #
        self._section(side, "Pages")

        nav_row = QWidget()
        nav = QHBoxLayout(nav_row)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(6)

        prev_btn = QPushButton("←")
        prev_btn.setObjectName("nav")
        prev_btn.clicked.connect(self._prev_page)
        nav.addWidget(prev_btn)

        new_btn = QPushButton("+")
        new_btn.setObjectName("nav")
        new_btn.clicked.connect(self._new_page)
        nav.addWidget(new_btn)

        next_btn = QPushButton("→")
        next_btn.setObjectName("nav")
        next_btn.clicked.connect(self._next_page)
        nav.addWidget(next_btn)
        nav.addStretch(1)
        side.addWidget(nav_row)
        side.addSpacing(6)

        self._page_label = QLabel("page 1 / 1")
        self._page_label.setObjectName("valueLabel")
        side.addWidget(self._page_label)

        side.addStretch(1)

        self._status = QLabel("")
        self._status.setObjectName("statusLabel")
        self._status.setWordWrap(True)
        side.addWidget(self._status)

        # --- shortcuts ------------------------------------------------- #
        self._install_shortcuts()

        self._sync_from_canvas()
        self.canvas_view.refresh()
        self._update_status()

        # Actually map the window. Without this, the QMainWindow exists
        # but never becomes visible.
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    # ------------------------------------------------------------------ #
    # Public API (called by app.py)                                      #
    # ------------------------------------------------------------------ #

    def tick(self) -> None:
        if self._closed:
            return
        try:
            self.canvas_view.refresh()
            self._update_status()
            self.app.processEvents()
        except RuntimeError:
            # Window was destroyed from underneath us.
            self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.win.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Layout helpers                                                     #
    # ------------------------------------------------------------------ #

    def _small(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {FG_SECONDARY}; font-size: 10pt;")
        return lbl

    def _section(self, layout: QVBoxLayout, text: str) -> None:
        lbl = QLabel(text.upper())
        lbl.setObjectName("sectionHeader")
        layout.addWidget(lbl)
        rule = QFrame()
        rule.setObjectName("sectionRule")
        layout.addSpacing(4)
        layout.addWidget(rule)
        layout.addSpacing(8)

    def _install_shortcuts(self) -> None:
        def sc(seq, cb):
            s = QShortcut(QKeySequence(seq), self.win)
            s.activated.connect(cb)
            return s

        sc("Ctrl+S", self._save_png)
        sc("Ctrl+P", self._save_pdf)
        sc("Ctrl+N", self._new_page)
        sc("PageUp", self._prev_page)
        sc("PageDown", self._next_page)
        sc("+", lambda: self.canvas_view.zoom_by(1.25))
        sc("=", lambda: self.canvas_view.zoom_by(1.25))
        sc("-", lambda: self.canvas_view.zoom_by(0.8))
        sc("0", lambda: self._select_view_radio("fit"))
        sc("1", lambda: self._select_view_radio("1.0"))

    def _select_view_radio(self, mode: str) -> None:
        for rb in self._view_radios:
            if rb.property("scale_mode") == mode:
                rb.setChecked(True)
                return

    # ------------------------------------------------------------------ #
    # Canvas / pen actions                                               #
    # ------------------------------------------------------------------ #

    def _current_canvas(self):
        return self.pages.current()

    def _set_color(self, rgb) -> None:
        self._current_canvas().set_pen_color(tuple(rgb))
        self._update_swatch_selection(tuple(rgb))
        self._color_label.setText("#%02x%02x%02x" % tuple(rgb))

    def _pick_color(self) -> None:
        cur = self._current_canvas().pen_color
        col = QColorDialog.getColor(QColor(*cur), self.win, "Pen colour")
        if col.isValid():
            self._set_color((col.red(), col.green(), col.blue()))

    def _update_swatch_selection(self, rgb) -> None:
        for s in self._swatches:
            s.set_selected(s.rgb == tuple(rgb))

    def _on_pen_size(self, value: int) -> None:
        size = value / 10.0
        self._current_canvas().set_pen_size(size)
        self._pen_size_label.setText(f"{size:.1f} px")

    def _on_eraser_size(self, value: int) -> None:
        size = value / 10.0
        self._current_canvas().set_eraser_size(size)
        self._eraser_size_label.setText(f"{size:.1f} px")

    def _on_pressure_toggle(self, on: bool) -> None:
        self._current_canvas().set_pressure_sensitive(bool(on))

    def _on_sticky_toggle(self, on: bool) -> None:
        if self.injector is None:
            return
        try:
            if hasattr(self.injector, "set_sticky_click"):
                self.injector.set_sticky_click(bool(on))
            elif hasattr(self.injector, "toggle_sticky_click"):
                want = bool(on)
                for _ in range(4):
                    if bool(getattr(self.injector, "sticky_click",
                                    False)) == want:
                        break
                    self.injector.toggle_sticky_click()
        except Exception:
            log.debug("sticky toggle failed", exc_info=True)

    def _on_scale_mode(self, on: bool, mode: str) -> None:
        if on:
            self.canvas_view.set_scale_mode(mode)

    def _sync_from_canvas(self) -> None:
        c = self._current_canvas()

        for w in (self._pen_size_slider, self._eraser_size_slider,
                  self._pressure_cb):
            w.blockSignals(True)
        self._pen_size_slider.setValue(int(round(c.pen_size * 10)))
        self._eraser_size_slider.setValue(int(round(c.eraser_size * 10)))
        self._pressure_cb.setChecked(bool(c.pressure_sensitive))
        for w in (self._pen_size_slider, self._eraser_size_slider,
                  self._pressure_cb):
            w.blockSignals(False)

        self._pen_size_label.setText(f"{c.pen_size:.1f} px")
        self._eraser_size_label.setText(f"{c.eraser_size:.1f} px")
        self._color_label.setText("#%02x%02x%02x" % tuple(c.pen_color))
        self._update_swatch_selection(tuple(c.pen_color))

        if self._sticky_cb is not None and self.injector is not None:
            self._sticky_cb.blockSignals(True)
            self._sticky_cb.setChecked(
                bool(getattr(self.injector, "sticky_click", False)))
            self._sticky_cb.blockSignals(False)

    # ------------------------------------------------------------------ #
    # File / page actions                                                #
    # ------------------------------------------------------------------ #

    def _save_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self.win, "Save PNG", "", "PNG image (*.png)")
        if path:
            self.pages.current().save_png(path)

    def _save_pdf(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self.win, "Save PDF", "", "PDF document (*.pdf)")
        if path:
            self.pages.current().save_pdf(path)

    def _clear(self) -> None:
        self.pages.current().clear()
        self.canvas_view.invalidate()
        if self.streamer is not None:
            self.streamer.wake()

    def _new_page(self) -> None:
        self.pages.new_page()
        self._sync_from_canvas()
        self.canvas_view.invalidate()
        if self.streamer is not None:
            self.streamer.reset()

    def _next_page(self) -> None:
        self.pages.next_page()
        self._sync_from_canvas()
        self.canvas_view.invalidate()
        if self.streamer is not None:
            self.streamer.reset()

    def _prev_page(self) -> None:
        self.pages.prev_page()
        self._sync_from_canvas()
        self.canvas_view.invalidate()
        if self.streamer is not None:
            self.streamer.reset()

    # ------------------------------------------------------------------ #
    # Status footer                                                      #
    # ------------------------------------------------------------------ #

    def _update_status(self) -> None:
        canvas = self.pages.current()
        scale = self.canvas_view._effective_scale
        tw = max(1, int(canvas.width * scale))
        th = max(1, int(canvas.height * scale))
        self._page_label.setText(
            f"page {self.pages.idx + 1} / {len(self.pages.pages)}")
        self._subtitle.setText(f"{canvas.width} × {canvas.height} px")
        self._status.setText(f"{scale * 100:.1f} %  ·  {tw}×{th} px")