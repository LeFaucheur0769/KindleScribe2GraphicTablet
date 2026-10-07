"""Optional Tkinter window showing the canvas and letting you tune the pen.

Modern dark theme, flat buttons, section headers. The window re-renders
the canvas only when something actually changed (canvas version, zoom,
or widget size), so it stays light on CPU while the streamer is running.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import colorchooser, filedialog, ttk, font as tkfont
from typing import Callable, Optional

from PIL import Image, ImageTk

from .canvas import PALETTE

log = logging.getLogger(__name__)

try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    LANCZOS = Image.LANCZOS


# --------------------------------------------------------------------------- #
# Palette                                                                    #
# --------------------------------------------------------------------------- #

BG_MAIN       = "#1a1a1c"
BG_SIDEBAR    = "#232326"
BG_CARD       = "#2a2a2e"
BG_INPUT      = "#2f2f34"
BG_HOVER      = "#3a3a42"
BG_ACTIVE     = "#45454f"
FG_PRIMARY    = "#e8e8ec"
FG_SECONDARY  = "#9a9aa6"
FG_MUTED      = "#6a6a76"
ACCENT        = "#5b8cff"
ACCENT_HOVER  = "#7ba3ff"
ACCENT_SOFT   = "#3a4a7a"
SEPARATOR     = "#2e2e34"


# --------------------------------------------------------------------------- #
# Fonts                                                                      #
# --------------------------------------------------------------------------- #

def _pick_font(candidates, fallback="TkDefaultFont"):
    try:
        families = set(tkfont.families())
    except Exception:
        return fallback
    for name in candidates:
        if name in families:
            return name
    return fallback


_UI_FAMILY = _pick_font([
    "Inter", "SF Pro Text", "Segoe UI Variable", "Segoe UI",
    "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans", "Helvetica",
])
_MONO_FAMILY = _pick_font([
    "JetBrains Mono", "SF Mono", "Cascadia Mono", "Consolas", "Menlo",
    "DejaVu Sans Mono", "Courier New", "Courier",
])


def _f(size: int, weight: str = "normal"):
    return (_UI_FAMILY, size, weight)


def _fm(size: int, weight: str = "normal"):
    return (_MONO_FAMILY, size, weight)


# --------------------------------------------------------------------------- #
# ttk style configuration                                                    #
# --------------------------------------------------------------------------- #

def _configure_ttk(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(
        "Modern.Horizontal.TScale",
        background=BG_SIDEBAR,
        troughcolor=BG_INPUT,
        bordercolor=BG_SIDEBAR,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        sliderrelief="flat",
        gripcount=0,
        troughrelief="flat",
    )
    style.map(
        "Modern.Horizontal.TScale",
        background=[("active", BG_SIDEBAR)],
        lightcolor=[("active", ACCENT_HOVER)],
        darkcolor=[("active", ACCENT_HOVER)],
    )

    style.configure(
        "Modern.TRadiobutton",
        background=BG_SIDEBAR,
        foreground=FG_PRIMARY,
        focuscolor=BG_SIDEBAR,
        font=_f(10),
        indicatorcolor=BG_INPUT,
        indicatorrelief="flat",
        bordercolor=BG_SIDEBAR,
    )
    style.map(
        "Modern.TRadiobutton",
        background=[("active", BG_SIDEBAR)],
        foreground=[("selected", ACCENT), ("active", ACCENT_HOVER)],
        indicatorcolor=[("selected", ACCENT), ("pressed", ACCENT_HOVER)],
    )

    style.configure(
        "Modern.TCheckbutton",
        background=BG_SIDEBAR,
        foreground=FG_PRIMARY,
        focuscolor=BG_SIDEBAR,
        font=_f(10),
        indicatorcolor=BG_INPUT,
        indicatorrelief="flat",
    )
    style.map(
        "Modern.TCheckbutton",
        background=[("active", BG_SIDEBAR)],
        foreground=[("selected", ACCENT), ("active", ACCENT_HOVER)],
        indicatorcolor=[("selected", ACCENT), ("pressed", ACCENT_HOVER)],
    )

    style.configure("Modern.TSeparator", background=SEPARATOR)


# --------------------------------------------------------------------------- #
# Small widgets                                                              #
# --------------------------------------------------------------------------- #

class FlatButton(tk.Frame):
    """A flat button with a hover state."""

    def __init__(self, parent, text: str, command: Callable[[], None],
                 *, accent: bool = False, font=None, **kw):
        bg = ACCENT if accent else BG_INPUT
        bg_hover = ACCENT_HOVER if accent else BG_HOVER
        fg = "#ffffff" if accent else FG_PRIMARY

        super().__init__(parent, bg=bg, cursor="hand2",
                         highlightthickness=0, bd=0, **kw)
        self._bg = bg
        self._bg_hover = bg_hover
        self._bg_active = BG_ACTIVE
        self._command = command

        self._label = tk.Label(self, text=text, bg=bg, fg=fg,
                               font=font or _f(10),
                               padx=12, pady=7, anchor="w")
        self._label.pack(fill="x")

        for w in (self, self._label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_press)
            w.bind("<ButtonRelease-1>", self._on_release)

    def _set_bg(self, colour):
        self.configure(bg=colour)
        self._label.configure(bg=colour)

    def _on_enter(self, _e):
        self._set_bg(self._bg_hover)

    def _on_leave(self, _e):
        self._set_bg(self._bg)

    def _on_press(self, _e):
        self._set_bg(self._bg_active)

    def _on_release(self, e):
        # Only fire if the release happened inside the widget.
        x, y = e.x_root, e.y_root
        wx, wy = self.winfo_rootx(), self.winfo_rooty()
        ww, wh = self.winfo_width(), self.winfo_height()
        inside = wx <= x < wx + ww and wy <= y < wy + wh
        self._set_bg(self._bg_hover if inside else self._bg)
        if inside and self._command:
            self._command()


class SectionHeader(tk.Frame):
    """Small uppercase label with a thin rule below it."""

    def __init__(self, parent, text: str, **kw):
        super().__init__(parent, bg=BG_SIDEBAR, **kw)
        tk.Label(self, text=text.upper(), bg=BG_SIDEBAR,
                 fg=FG_MUTED, font=_f(9, "bold"),
                 anchor="w").pack(fill="x")
        tk.Frame(self, bg=SEPARATOR, height=1).pack(fill="x", pady=(4, 0))


class Swatch(tk.Frame):
    """A colour swatch. Click selects; highlight shows current selection."""

    def __init__(self, parent, rgb, command, size: int = 26):
        self.rgb = tuple(rgb)
        self._command = command
        self._size = size
        self._selected = False

        super().__init__(parent, bg=BG_SIDEBAR, bd=0,
                         highlightthickness=2,
                         highlightbackground=BG_SIDEBAR,
                         highlightcolor=BG_SIDEBAR)
        hexcolor = "#%02x%02x%02x" % self.rgb
        self._inner = tk.Frame(self, bg=hexcolor, cursor="hand2",
                               width=size, height=size, bd=0,
                               highlightthickness=0)
        self._inner.pack(padx=2, pady=2)
        self._inner.pack_propagate(False)

        for w in (self._inner,):
            w.bind("<Button-1>", lambda e: self._command(self.rgb))

    def set_selected(self, yes: bool) -> None:
        if yes == self._selected:
            return
        self._selected = yes
        colour = ACCENT if yes else BG_SIDEBAR
        self.configure(highlightbackground=colour, highlightcolor=colour)


# --------------------------------------------------------------------------- #
# Main window                                                                #
# --------------------------------------------------------------------------- #

class DrawingWindow:
    SIDEBAR_WIDTH = 240
    MARGIN = 60

    def __init__(self, pages, streamer, injector=None) -> None:
        self.pages = pages
        self.streamer = streamer
        self.injector = injector

        self.root = tk.Tk()
        self.root.title("Kindle Draw")
        self.root.minsize(640, 480)
        self.root.configure(bg=BG_MAIN)

        _configure_ttk(self.root)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        avail_w = max(500, sw - self.SIDEBAR_WIDTH - self.MARGIN)
        avail_h = max(400, sh - self.MARGIN)
        self.root.geometry(f"{avail_w + self.SIDEBAR_WIDTH}x{avail_h}")

        # --- canvas viewport ------------------------------------------- #
        viewport = tk.Frame(self.root, bg=BG_MAIN, bd=0)
        viewport.pack(side="left", fill="both", expand=True)

        self._canvas_widget = tk.Canvas(
            viewport, bg=BG_MAIN, highlightthickness=0, bd=0,
        )
        self._canvas_widget.pack(fill="both", expand=True)

        # --- sidebar ---------------------------------------------------- #
        side = tk.Frame(self.root, width=self.SIDEBAR_WIDTH, bg=BG_SIDEBAR)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        # Inner padded container
        inner = tk.Frame(side, bg=BG_SIDEBAR)
        inner.pack(fill="both", expand=True, padx=14, pady=14)

        # Title block
        tk.Label(inner, text="Kindle Draw", bg=BG_SIDEBAR, fg=FG_PRIMARY,
                 font=_f(15, "bold"), anchor="w").pack(fill="x")
        self._title_sub = tk.Label(inner, text="", bg=BG_SIDEBAR,
                                   fg=FG_MUTED, font=_fm(9), anchor="w")
        self._title_sub.pack(fill="x", pady=(2, 12))

        # --- Pen section ------------------------------------------------ #
        SectionHeader(inner, "Pen").pack(fill="x", pady=(0, 8))

        # Colour swatches grid
        swatch_grid = tk.Frame(inner, bg=BG_SIDEBAR)
        swatch_grid.pack(fill="x", pady=(0, 6))
        self._swatches = []
        cols = 5
        for i, rgb in enumerate(PALETTE):
            s = Swatch(swatch_grid, rgb, self._set_color)
            s.grid(row=i // cols, column=i % cols, padx=(0, 4), pady=(0, 4))
            self._swatches.append(s)

        # Custom colour + current hex
        colour_row = tk.Frame(inner, bg=BG_SIDEBAR)
        colour_row.pack(fill="x", pady=(4, 8))
        FlatButton(colour_row, "Custom colour…", self._pick_color,
                   font=_f(9)).pack(side="left", fill="x", expand=True)
        self._color_label = tk.Label(colour_row, text="",
                                     bg=BG_SIDEBAR, fg=FG_SECONDARY,
                                     font=_fm(9))
        self._color_label.pack(side="right", padx=(8, 0))

        # Pen size
        pen_size_head = tk.Frame(inner, bg=BG_SIDEBAR)
        pen_size_head.pack(fill="x", pady=(6, 0))
        tk.Label(pen_size_head, text="Size", bg=BG_SIDEBAR,
                 fg=FG_SECONDARY, font=_f(10)).pack(side="left")
        self._pen_size_label = tk.Label(pen_size_head, text="",
                                        bg=BG_SIDEBAR, fg=FG_SECONDARY,
                                        font=_fm(9))
        self._pen_size_label.pack(side="right")

        self._pen_size_var = tk.DoubleVar(value=8.0)
        ttk.Scale(inner, from_=1.0, to=60.0,
                  variable=self._pen_size_var,
                  command=self._on_pen_size,
                  style="Modern.Horizontal.TScale").pack(fill="x", pady=(4, 0))

        # --- Eraser section --------------------------------------------- #
        SectionHeader(inner, "Eraser").pack(fill="x", pady=(16, 8))

        eraser_head = tk.Frame(inner, bg=BG_SIDEBAR)
        eraser_head.pack(fill="x")
        tk.Label(eraser_head, text="Size", bg=BG_SIDEBAR,
                 fg=FG_SECONDARY, font=_f(10)).pack(side="left")
        self._eraser_size_label = tk.Label(eraser_head, text="",
                                           bg=BG_SIDEBAR, fg=FG_SECONDARY,
                                           font=_fm(9))
        self._eraser_size_label.pack(side="right")

        self._eraser_size_var = tk.DoubleVar(value=48.0)
        ttk.Scale(inner, from_=2.0, to=200.0,
                  variable=self._eraser_size_var,
                  command=self._on_eraser_size,
                  style="Modern.Horizontal.TScale").pack(fill="x", pady=(4, 0))

        # --- Behaviour section ------------------------------------------ #
        SectionHeader(inner, "Behaviour").pack(fill="x", pady=(16, 8))

        self._pressure_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="Pressure sensitive",
                        variable=self._pressure_var,
                        command=self._on_pressure_toggle,
                        style="Modern.TCheckbutton").pack(
            anchor="w", pady=(0, 4))

        self._sticky_var: Optional[tk.BooleanVar] = None
        if injector is not None and hasattr(injector, "toggle_sticky_click"):
            self._sticky_var = tk.BooleanVar(
                value=bool(getattr(injector, "sticky_click", False)))
            ttk.Checkbutton(inner, text="Sticky click (barrel button)",
                            variable=self._sticky_var,
                            command=self._on_sticky_toggle,
                            style="Modern.TCheckbutton").pack(anchor="w")

        # --- View section ----------------------------------------------- #
        SectionHeader(inner, "View").pack(fill="x", pady=(16, 8))

        self._scale_var = tk.StringVar(value="fit")
        for text, val in (("Fit to window", "fit"),
                          ("100 %", "1.0"),
                          ("50 %", "0.5"),
                          ("25 %", "0.25")):
            ttk.Radiobutton(inner, text=text, value=val,
                            variable=self._scale_var,
                            command=self._request_render,
                            style="Modern.TRadiobutton").pack(
                anchor="w", pady=1)

        # --- Actions section -------------------------------------------- #
        SectionHeader(inner, "Actions").pack(fill="x", pady=(16, 8))

        FlatButton(inner, "Save PNG…", self._save_png).pack(
            fill="x", pady=(0, 4))
        FlatButton(inner, "Save PDF…", self._save_pdf).pack(
            fill="x", pady=(0, 4))
        FlatButton(inner, "Clear canvas", self._clear).pack(
            fill="x", pady=(0, 4))

        # --- Pages section ---------------------------------------------- #
        SectionHeader(inner, "Pages").pack(fill="x", pady=(16, 8))

        nav = tk.Frame(inner, bg=BG_SIDEBAR)
        nav.pack(fill="x")

        def _nav_btn(text, cmd):
            b = tk.Label(nav, text=text, bg=BG_INPUT, fg=FG_PRIMARY,
                         font=_f(12), padx=10, pady=5, cursor="hand2")
            b.bind("<Enter>", lambda e: b.configure(bg=BG_HOVER))
            b.bind("<Leave>", lambda e: b.configure(bg=BG_INPUT))
            b.bind("<Button-1>", lambda e: cmd())
            return b

        _nav_btn("←", self._prev_page).pack(side="left")
        _nav_btn("+", self._new_page).pack(side="left", padx=4)
        _nav_btn("→", self._next_page).pack(side="left")

        self._page_label = tk.Label(inner, text="page 1 / 1",
                                    bg=BG_SIDEBAR, fg=FG_MUTED,
                                    font=_fm(9))
        self._page_label.pack(pady=(6, 0))

        # --- status footer ---------------------------------------------- #
        self._status = tk.Label(side, text="", bg=BG_SIDEBAR,
                                fg=FG_MUTED, font=_fm(8),
                                anchor="w", justify="left")
        self._status.pack(side="bottom", fill="x", padx=14, pady=10)

        # --- state ------------------------------------------------------ #
        self._tk_img: Optional[ImageTk.PhotoImage] = None
        self._effective_scale = 1.0
        self._closed = False
        self._render_pending = False
        self._last_render_key = None

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._canvas_widget.bind(
            "<Configure>", lambda e: self._request_render())

        # Zoom shortcuts
        self.root.bind("<plus>",  lambda e: self._zoom_by(1.25))
        self.root.bind("<equal>", lambda e: self._zoom_by(1.25))
        self.root.bind("<minus>", lambda e: self._zoom_by(0.8))
        self.root.bind("<0>",     lambda e: self._set_scale_mode("fit"))
        self.root.bind("<1>",     lambda e: self._set_scale_mode("1.0"))

        # Save shortcuts
        self.root.bind("<Control-s>", lambda e: self._save_png())
        self.root.bind("<Control-p>", lambda e: self._save_pdf())

        # Page shortcuts
        self.root.bind("<Prior>", lambda e: self._prev_page())
        self.root.bind("<Next>",  lambda e: self._next_page())

        self._sync_from_canvas()

    # ------------------------------------------------------------------ #
    # External API                                                       #
    # ------------------------------------------------------------------ #

    def tick(self) -> None:
        if self._closed:
            return
        try:
            self._render()
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            self._closed = True

    def close(self) -> None:
        self._on_close()

    # ------------------------------------------------------------------ #
    # Pen actions                                                        #
    # ------------------------------------------------------------------ #

    def _current_canvas(self):
        return self.pages.current()

    def _set_color(self, rgb) -> None:
        self._current_canvas().set_pen_color(tuple(rgb))
        self._update_swatch_selection(tuple(rgb))
        self._color_label.config(text="#%02x%02x%02x" % tuple(rgb))

    def _pick_color(self) -> None:
        initial = "#%02x%02x%02x" % self._current_canvas().pen_color
        rgb, _ = colorchooser.askcolor(color=initial, title="Pen colour")
        if rgb:
            self._set_color(tuple(int(c) for c in rgb))

    def _update_swatch_selection(self, current_rgb) -> None:
        for s in self._swatches:
            s.set_selected(s.rgb == tuple(current_rgb))

    def _on_pen_size(self, _value) -> None:
        v = float(self._pen_size_var.get())
        self._current_canvas().set_pen_size(v)
        self._pen_size_label.config(text=f"{v:.1f} px")

    def _on_eraser_size(self, _value) -> None:
        v = float(self._eraser_size_var.get())
        self._current_canvas().set_eraser_size(v)
        self._eraser_size_label.config(text=f"{v:.1f} px")

    def _on_pressure_toggle(self) -> None:
        self._current_canvas().set_pressure_sensitive(
            self._pressure_var.get())

    def _on_sticky_toggle(self) -> None:
        if self.injector is None or self._sticky_var is None:
            return
        want = bool(self._sticky_var.get())
        try:
            if hasattr(self.injector, "set_sticky_click"):
                self.injector.set_sticky_click(want)
            elif hasattr(self.injector, "toggle_sticky_click"):
                # Fallback if only toggle exists.
                for _ in range(4):
                    if bool(getattr(self.injector, "sticky_click", False)) == want:
                        break
                    self.injector.toggle_sticky_click()
        except Exception:
            log.debug("sticky toggle failed", exc_info=True)

    def _sync_from_canvas(self) -> None:
        c = self._current_canvas()
        self._pen_size_var.set(c.pen_size)
        self._eraser_size_var.set(c.eraser_size)
        self._pressure_var.set(c.pressure_sensitive)
        self._pen_size_label.config(text=f"{c.pen_size:.1f} px")
        self._eraser_size_label.config(text=f"{c.eraser_size:.1f} px")
        self._color_label.config(text="#%02x%02x%02x" % c.pen_color)
        self._update_swatch_selection(c.pen_color)

        if self._sticky_var is not None and self.injector is not None:
            self._sticky_var.set(
                bool(getattr(self.injector, "sticky_click", False)))

    # ------------------------------------------------------------------ #
    # Zoom                                                                #
    # ------------------------------------------------------------------ #

    def _set_scale_mode(self, mode: str) -> None:
        self._scale_var.set(mode)
        self._request_render()

    def _zoom_by(self, factor: float) -> None:
        self._scale_var.set(f"{self._effective_scale * factor:.4f}")
        self._request_render()

    def _request_render(self) -> None:
        self._render_pending = True
        self._last_render_key = None   # force a redraw

    def _compute_scale(self, src_w: int, src_h: int) -> float:
        mode = self._scale_var.get()
        if mode == "fit":
            cw = max(1, self._canvas_widget.winfo_width())
            ch = max(1, self._canvas_widget.winfo_height())
            if cw <= 1 or ch <= 1:
                cw = max(1, self.root.winfo_width() - self.SIDEBAR_WIDTH)
                ch = max(1, self.root.winfo_height())
            return max(0.01, min(cw / src_w, ch / src_h))
        try:
            return max(0.01, float(mode))
        except ValueError:
            return 1.0

    # ------------------------------------------------------------------ #
    # Render                                                              #
    # ------------------------------------------------------------------ #

    def _render(self) -> None:
        canvas = self.pages.current()
        src_w, src_h = canvas.width, canvas.height
        scale = self._compute_scale(src_w, src_h)
        self._effective_scale = scale

        cw = max(1, self._canvas_widget.winfo_width())
        ch = max(1, self._canvas_widget.winfo_height())

        key = (canvas.version, round(scale, 6), cw, ch, id(canvas))
        if key == self._last_render_key and self._tk_img is not None:
            return
        self._last_render_key = key

        new_w = max(1, int(round(src_w * scale)))
        new_h = max(1, int(round(src_h * scale)))

        img = canvas.snapshot().resize((new_w, new_h), LANCZOS)
        self._tk_img = ImageTk.PhotoImage(img)

        x = max(0, (cw - new_w) // 2)
        y = max(0, (ch - new_h) // 2)

        self._canvas_widget.delete("all")

        # Subtle outline so the page reads as an object on the dark backdrop.
        pad = 1
        self._canvas_widget.create_rectangle(
            x - pad, y - pad, x + new_w + pad, y + new_h + pad,
            fill="", outline="#3a3a44", width=1,
        )
        self._canvas_widget.create_image(x, y, anchor="nw",
                                         image=self._tk_img)
        self._canvas_widget.configure(
            scrollregion=(0, 0, max(cw, new_w), max(ch, new_h)),
        )

        self._page_label.config(
            text=f"page {self.pages.idx + 1} / {len(self.pages.pages)}"
        )
        self._title_sub.config(text=f"{src_w} × {src_h} px")
        self._status.config(
            text=f"{scale * 100:.1f} %  ·  {new_w}×{new_h} px"
        )

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def _save_png(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG", "*.png")])
        if path:
            self.pages.current().save_png(path)

    def _save_pdf(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if path:
            self.pages.current().save_pdf(path)

    def _clear(self) -> None:
        self.pages.current().clear()
        self._request_render()
        self.streamer.wake()

    def _new_page(self) -> None:
        self.pages.new_page()
        self._sync_from_canvas()
        self._request_render()
        self.streamer.reset()

    def _next_page(self) -> None:
        self.pages.next_page()
        self._sync_from_canvas()
        self._request_render()
        self.streamer.reset()

    def _prev_page(self) -> None:
        self.pages.prev_page()
        self._sync_from_canvas()
        self._request_render()
        self.streamer.reset()

    def _on_close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass