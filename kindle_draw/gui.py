"""Optional Tkinter window showing the canvas and letting you tune the pen."""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import colorchooser, filedialog, ttk
from typing import Optional

from PIL import Image, ImageTk

from .canvas import PALETTE

log = logging.getLogger(__name__)

try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    LANCZOS = Image.LANCZOS


class DrawingWindow:
    SIDEBAR_WIDTH = 200
    MARGIN = 40

    def __init__(self, pages, streamer) -> None:
        self.pages = pages
        self.streamer = streamer

        self.root = tk.Tk()
        self.root.title("Kindle Draw")
        self.root.minsize(500, 400)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        avail_w = max(400, sw - self.SIDEBAR_WIDTH - self.MARGIN)
        avail_h = max(300, sh - self.MARGIN)
        self.root.geometry(f"{avail_w + self.SIDEBAR_WIDTH}x{avail_h}")

        # --- canvas viewport -------------------------------------------- #
        self._canvas_widget = tk.Canvas(self.root, bg="#202020",
                                        highlightthickness=0)
        self._canvas_widget.pack(side="left", fill="both", expand=True)

        # --- sidebar ---------------------------------------------------- #
        side = tk.Frame(self.root, width=self.SIDEBAR_WIDTH)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        # Pen colour
        ttk.Label(side, text="Pen colour").pack(anchor="w", padx=8, pady=(8, 2))
        self._swatch_frame = tk.Frame(side)
        self._swatch_frame.pack(anchor="w", padx=8)
        self._swatch_buttons = []
        for i, rgb in enumerate(PALETTE):
            b = tk.Frame(self._swatch_frame, width=22, height=22,
                         bg="#%02x%02x%02x" % rgb, cursor="hand2",
                         highlightthickness=2, highlightbackground="#333")
            b.grid(row=i // 5, column=i % 5, padx=2, pady=2)
            b.pack_propagate(False)
            b.bind("<Button-1>", lambda e, c=rgb: self._set_color(c))
            self._swatch_buttons.append((b, rgb))

        ttk.Button(side, text="Custom colour…",
                   command=self._pick_color).pack(fill="x", padx=8, pady=(4, 0))
        self._color_label = ttk.Label(side, text="", foreground="#555")
        self._color_label.pack(anchor="w", padx=8)

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)

        # Pen size
        ttk.Label(side, text="Pen size").pack(anchor="w", padx=8)
        self._pen_size_var = tk.DoubleVar(value=8.0)
        self._pen_size_scale = ttk.Scale(side, from_=1.0, to=60.0,
                                         variable=self._pen_size_var,
                                         command=self._on_pen_size)
        self._pen_size_scale.pack(fill="x", padx=8)
        self._pen_size_label = ttk.Label(side, text="3.0 px",
                                         foreground="#555")
        self._pen_size_label.pack(anchor="w", padx=8)

        # Eraser size
        ttk.Label(side, text="Eraser size").pack(anchor="w", padx=8,
                                                 pady=(8, 0))
        self._eraser_size_var = tk.DoubleVar(value=48.0)
        self._eraser_size_scale = ttk.Scale(side, from_=2.0, to=120.0,
                                            variable=self._eraser_size_var,
                                            command=self._on_eraser_size)
        self._eraser_size_scale.pack(fill="x", padx=8)
        self._eraser_size_label = ttk.Label(side, text="24.0 px",
                                            foreground="#555")
        self._eraser_size_label.pack(anchor="w", padx=8)

        # Pressure toggle
        self._pressure_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(side, text="Pressure sensitive",
                        variable=self._pressure_var,
                        command=self._on_pressure_toggle).pack(
            anchor="w", padx=8, pady=(8, 0))

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)

        # Zoom
        ttk.Label(side, text="Zoom:").pack(anchor="w", padx=8)
        self._scale_var = tk.StringVar(value="fit")
        for text, val in (("Fit to window", "fit"),
                          ("100 %", "1.0"),
                          ("50 %", "0.5"),
                          ("25 %", "0.25")):
            ttk.Radiobutton(side, text=text, value=val,
                            variable=self._scale_var,
                            command=self._request_render).pack(anchor="w",
                                                               padx=12)

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)

        # Actions
        ttk.Button(side, text="Save PNG…",
                   command=self._save_png).pack(fill="x", padx=6, pady=2)
        ttk.Button(side, text="Save PDF…",
                   command=self._save_pdf).pack(fill="x", padx=6, pady=2)
        ttk.Button(side, text="Clear",
                   command=self._clear).pack(fill="x", padx=6, pady=2)
        ttk.Button(side, text="New page",
                   command=self._new_page).pack(fill="x", padx=6, pady=2)
        ttk.Button(side, text="Next page →",
                   command=self._next_page).pack(fill="x", padx=6, pady=2)
        ttk.Button(side, text="← Prev page",
                   command=self._prev_page).pack(fill="x", padx=6, pady=2)
        self._page_label = ttk.Label(side, text="page 1 / 1")
        self._page_label.pack(padx=6, pady=4)

        self._status = ttk.Label(side, text="", foreground="#666",
                                 wraplength=self.SIDEBAR_WIDTH - 16,
                                 justify="left")
        self._status.pack(side="bottom", fill="x", padx=8, pady=6)

        # --- state ------------------------------------------------------ #
        self._tk_img: Optional[ImageTk.PhotoImage] = None
        self._effective_scale = 1.0
        self._closed = False
        self._render_pending = False

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._canvas_widget.bind("<Configure>", lambda e: self._request_render())

        # Keyboard shortcuts for zoom
        self.root.bind("<plus>",  lambda e: self._zoom_by(1.25))
        self.root.bind("<equal>", lambda e: self._zoom_by(1.25))
        self.root.bind("<minus>", lambda e: self._zoom_by(0.8))
        self.root.bind("<0>",     lambda e: self._set_scale_mode("fit"))
        self.root.bind("<1>",     lambda e: self._set_scale_mode("1.0"))

        # Tab through pages
        self.root.bind("<Prior>", lambda e: self._prev_page())
        self.root.bind("<Next>",  lambda e: self._next_page())

        # Reflect whatever the current canvas already has
        self._sync_from_canvas()

    # -- external API --------------------------------------------------- #

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

    # -- pen actions ---------------------------------------------------- #

    def _current_canvas(self):
        return self.pages.current()

    def _set_color(self, rgb) -> None:
        self._current_canvas().set_pen_color(rgb)
        self._color_label.config(text="#%02x%02x%02x" % rgb)

    def _pick_color(self) -> None:
        initial = "#%02x%02x%02x" % self._current_canvas().pen_color
        rgb, _ = colorchooser.askcolor(color=initial, title="Pen colour")
        if rgb:
            self._set_color(tuple(int(c) for c in rgb))

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

    def _sync_from_canvas(self) -> None:
        """Read the current canvas's pen settings into the widgets."""
        c = self._current_canvas()
        self._pen_size_var.set(c.pen_size)
        self._eraser_size_var.set(c.eraser_size)
        self._pressure_var.set(c.pressure_sensitive)
        self._pen_size_label.config(text=f"{c.pen_size:.1f} px")
        self._eraser_size_label.config(text=f"{c.eraser_size:.1f} px")
        self._color_label.config(text="#%02x%02x%02x" % c.pen_color)

    # -- zoom ----------------------------------------------------------- #

    def _set_scale_mode(self, mode: str) -> None:
        self._scale_var.set(mode)
        self._request_render()

    def _zoom_by(self, factor: float) -> None:
        self._scale_var.set(f"{self._effective_scale * factor:.4f}")
        self._request_render()

    def _request_render(self) -> None:
        self._render_pending = True

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

    def _render(self) -> None:
        canvas = self.pages.current()
        src_w, src_h = canvas.width, canvas.height
        scale = self._compute_scale(src_w, src_h)
        self._effective_scale = scale

        new_w = max(1, int(round(src_w * scale)))
        new_h = max(1, int(round(src_h * scale)))

        img = canvas.snapshot().resize((new_w, new_h), LANCZOS)
        self._tk_img = ImageTk.PhotoImage(img)

        cw = max(1, self._canvas_widget.winfo_width())
        ch = max(1, self._canvas_widget.winfo_height())
        x = max(0, (cw - new_w) // 2)
        y = max(0, (ch - new_h) // 2)

        self._canvas_widget.delete("all")
        self._canvas_widget.create_image(x, y, anchor="nw", image=self._tk_img)
        self._canvas_widget.configure(
            scrollregion=(0, 0, max(cw, new_w), max(ch, new_h)),
        )

        self._page_label.config(
            text=f"page {self.pages.idx + 1} / {len(self.pages.pages)}"
        )
        self._status.config(
            text=(f"canvas {src_w}×{src_h} px\n"
                  f"scale {scale * 100:.1f} %  →  {new_w}×{new_h} px")
        )

    # -- actions -------------------------------------------------------- #

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
        self.streamer.wake()

    def _new_page(self) -> None:
        self.pages.new_page()
        self._sync_from_canvas()
        self.streamer.reset()

    def _next_page(self) -> None:
        self.pages.next_page()
        self._sync_from_canvas()
        self.streamer.reset()

    def _prev_page(self) -> None:
        self.pages.prev_page()
        self._sync_from_canvas()
        self.streamer.reset()

    def _on_close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass