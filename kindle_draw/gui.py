"""Optional Tkinter window showing the canvas on the host.

Renders the same Pillow image the Kindle sees. Scales to fit the window,
preserves aspect ratio, and re-fits on resize.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Optional

from PIL import Image, ImageTk

log = logging.getLogger(__name__)

try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:                      # Pillow < 9.1
    LANCZOS = Image.LANCZOS


class DrawingWindow:
    SIDEBAR_WIDTH = 160
    MARGIN = 40           # leave room for window decorations / taskbar

    def __init__(self, pages, streamer) -> None:
        self.pages = pages
        self.streamer = streamer

        self.root = tk.Tk()
        self.root.title("Kindle Draw")
        self.root.minsize(400, 300)

        # --- figure out a sensible starting size ------------------------ #
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        avail_w = max(400, sw - self.SIDEBAR_WIDTH - self.MARGIN)
        avail_h = max(300, sh - self.MARGIN)
        self.root.geometry(f"{avail_w + self.SIDEBAR_WIDTH}x{avail_h}")

        # --- layout ----------------------------------------------------- #
        self._canvas_widget = tk.Canvas(
            self.root, bg="#202020", highlightthickness=0,
        )
        self._canvas_widget.pack(side="left", fill="both", expand=True)

        side = tk.Frame(self.root, width=self.SIDEBAR_WIDTH)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        ttk.Label(side, text="Scale:").pack(anchor="w", padx=6, pady=(8, 0))
        self._scale_var = tk.StringVar(value="fit")
        for text, val in (("Fit to window", "fit"),
                          ("100 %", "1.0"),
                          ("50 %", "0.5"),
                          ("25 %", "0.25")):
            ttk.Radiobutton(side, text=text, value=val,
                            variable=self._scale_var,
                            command=self._on_scale_change).pack(
                anchor="w", padx=6)

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)

        ttk.Button(side, text="Save PNG…",
                   command=self._save_png).pack(fill="x", padx=4, pady=2)
        ttk.Button(side, text="Save PDF…",
                   command=self._save_pdf).pack(fill="x", padx=4, pady=2)
        ttk.Button(side, text="Clear",
                   command=self._clear).pack(fill="x", padx=4, pady=2)

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)

        ttk.Button(side, text="New page",
                   command=self._new_page).pack(fill="x", padx=4, pady=2)
        ttk.Button(side, text="Next page →",
                   command=self._next_page).pack(fill="x", padx=4, pady=2)
        ttk.Button(side, text="← Prev page",
                   command=self._prev_page).pack(fill="x", padx=4, pady=2)
        self._page_label = ttk.Label(side, text="page 1 / 1")
        self._page_label.pack(padx=4, pady=4)

        self._status = ttk.Label(side, text="", foreground="#666",
                                 wraplength=self.SIDEBAR_WIDTH - 12,
                                 justify="left")
        self._status.pack(side="bottom", fill="x", padx=6, pady=6)

        # --- state ------------------------------------------------------ #
        self._tk_img: Optional[ImageTk.PhotoImage] = None
        self._last_img_size = (0, 0)
        self._effective_scale = 1.0
        self._closed = False

        # --- bindings --------------------------------------------------- #
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._canvas_widget.bind("<Configure>", lambda e: self._request_render())
        self.root.bind("<Control-s>", lambda e: self._save_png())
        self.root.bind("<Control-S>", lambda e: self._save_png())
        self.root.bind("<Control-p>", lambda e: self._save_pdf())
        self.root.bind("<Control-n>", lambda e: self._new_page())
        self.root.bind("<Prior>",   lambda e: self._prev_page())   # Page Up
        self.root.bind("<Next>",    lambda e: self._next_page())   # Page Down
        self.root.bind("<plus>",  lambda e: self._zoom_by(1.25))
        self.root.bind("<equal>", lambda e: self._zoom_by(1.25))
        self.root.bind("<minus>", lambda e: self._zoom_by(0.8))
        self.root.bind("<0>",     lambda e: self._set_scale_mode("fit"))
        self.root.bind("<1>",     lambda e: self._set_scale_mode("1.0"))

        self._render_pending = False

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

    # -- event handlers ------------------------------------------------- #

    def _on_scale_change(self) -> None:
        self._request_render()

    def _set_scale_mode(self, mode: str) -> None:
        self._scale_var.set(mode)
        self._request_render()

    def _zoom_by(self, factor: float) -> None:
        # Turn the current effective scale into an explicit value.
        self._scale_var.set(f"{self._effective_scale * factor:.4f}")
        self._request_render()

    def _request_render(self) -> None:
        self._render_pending = True

    # -- render --------------------------------------------------------- #

    def _compute_scale(self, src_w: int, src_h: int) -> float:
        mode = self._scale_var.get()
        if mode == "fit":
            cw = max(1, self._canvas_widget.winfo_width())
            ch = max(1, self._canvas_widget.winfo_height())
            if cw <= 1 or ch <= 1:                # before layout settles
                cw = max(1, self.root.winfo_width() - self.SIDEBAR_WIDTH)
                ch = max(1, self.root.winfo_height())
            s = min(cw / src_w, ch / src_h)
            return max(0.01, s)
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
        # Center the image inside the viewport (or align to top-left when
        # it's larger than the viewport, so scrollbars are meaningful).
        x = max(0, (cw - new_w) // 2)
        y = max(0, (ch - new_h) // 2)

        self._canvas_widget.delete("all")
        self._canvas_widget.create_image(x, y, anchor="nw", image=self._tk_img)

        # Expand the scroll region when the image exceeds the viewport.
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
        self.streamer.reset()

    def _next_page(self) -> None:
        self.pages.next_page()
        self.streamer.reset()

    def _prev_page(self) -> None:
        self.pages.prev_page()
        self.streamer.reset()

    def _on_close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass