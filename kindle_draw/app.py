"""CLI entry point and orchestration for the Kindle Draw tool."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import threading
import time
from typing import Optional, Tuple

from .calibration import (
    build_transform,
    calibrate,
    load_calibration,
    raw_to_canvas,
    raw_to_target,
    save_calibration,
    transform_is_legacy,
    validate_calibration,
)
from .canvas import Canvas, Brush  # noqa: F401
from .config import (
    AppConfig,
    InjectConfig,
    KindleConfig,
    OutputConfig,
    PanelConfig,
    PenConfig,
    StreamConfig,
    StylusConfig,
)
from .evdev_reader import (
    StylusTracker,
    detect_esz,
    find_stylus_device,
    unpack_event,
)
from .input_inject import build_injector
from .kindle_link import KindleLink
from .pages import PageManager
from .streamer import Streamer
from .stroke_logger import StrokeLogger

log = logging.getLogger("kindle_draw")


# --------------------------------------------------------------------------- #
# CLI                                                                        #
# --------------------------------------------------------------------------- #

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kindle_draw",
        description="Use a jailbroken Kindle Scribe as a drawing tablet.",
    )

    # Kindle
    p.add_argument("--kindle", default=None,
                   help="SSH host of the Kindle (default from config.py)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--key", default=None, help="SSH private key path")
    p.add_argument("--fbink", default=None,
                   help="fbink binary path on the Kindle")
    p.add_argument("--control-path", default=None,
                   help="SSH ControlPath socket")
    p.add_argument("--password", default=None,
                   help="SSH password (requires sshpass; prefer SSH keys). "
                        "Also read from $KINDLE_SSH_PASSWORD.")
    p.add_argument("--list-devices", action="store_true",
                   help="Print the Kindle input device list and exit")

    # Panel
    p.add_argument("--width", type=int, default=1860,
                   help="Kindle panel width in pixels (native portrait)")
    p.add_argument("--height", type=int, default=2480,
                   help="Kindle panel height in pixels (native portrait)")
    p.add_argument("--aspect", choices=["fit", "crop", "stretch"], default="fit")

    # Canvas
    p.add_argument("--canvas-width", type=int, default=None)
    p.add_argument("--canvas-height", type=int, default=None)

    # Stylus
    p.add_argument("--stylus-dev", default=None,
                   help="Override autodetected /dev/input/eventN")
    p.add_argument("--esz", type=int, default=None,
                   help="Override event struct size (16 or 24)")
    p.add_argument("--calibrate", action="store_true",
                   help="Run interactive stylus calibration and exit")
    p.add_argument("--cal-path", default="~/.kindle_draw_cal.json")
    p.add_argument("--no-input", action="store_true",
                   help="Don't read stylus events (display only)")
    p.add_argument("--pressure-min", type=int, default=None)
    p.add_argument("--pressure-max", type=int, default=None)

    # Pen
    g = p.add_argument_group("Pen")
    g.add_argument("--pen-color", default=None,
                   help="Pen colour: '#000000', '#f00', 'rgb(0,0,0)', or a "
                        "name (black, white, red, green, blue, orange, "
                        "yellow, purple, pink, gray)")
    g.add_argument("--pen-size", type=float, default=None,
                   help="Max pen stroke width in pixels")
    g.add_argument("--eraser-size", type=float, default=None,
                   help="Eraser width in pixels")
    g.add_argument("--no-pressure", dest="pressure_sensitive",
                   action="store_false", default=None,
                   help="Disable pressure sensitivity")

    # Injection
    p.add_argument("--inject",
                   choices=["uinput", "xdotool", "ydotool", "canvas", "none"],
                   default="canvas",
                   help="Where to send stylus input (default: draw on canvas)")
    p.add_argument("--inject-size", default="auto",
                   help="Target size for uinput/xdotool/ydotool: 'WxH' or "
                        "'auto' to use the primary monitor's resolution "
                        "(default). Ignored for canvas mode.")
    p.add_argument("--no-letterbox", action="store_true",
                   help="For uinput/etc., stretch the pen across the whole "
                        "target instead of preserving the Scribe's aspect ratio")
    p.add_argument("--rotate", default="auto",
                   help="Panel rotation for system injectors only "
                        "(uinput/xdotool/ydotool): 0/90/180/270, or 'auto' "
                        "(default). 'auto' picks 90 when the host screen is "
                        "landscape (so the Scribe matches), 0 when portrait. "
                        "Ignored in canvas/GUI mode (canvas is fixed portrait).")
    p.add_argument("--sticky-click", action="store_true",
                   help="Start with the barrel button in sticky mode: press "
                        "once to hold the click down, press again to release. "
                        "Toggle at runtime with SIGUSR1.")

    # Output
    p.add_argument("--save-png", default=None)
    p.add_argument("--save-pdf", default=None)
    p.add_argument("--save-json", default=None)
    p.add_argument("--autosave", type=float, default=0.0,
                   help="Autosave interval in seconds (0 = disabled)")
    p.add_argument("--pages-dir", default=None,
                   help="Save all pages as PNG + pages.pdf in this directory")

    # Stream
    p.add_argument("--active-wf", default="DU")
    p.add_argument("--clean-wf", default="GL16")
    p.add_argument("--flash-wf", default="GC16")
    p.add_argument("--settle", type=float, default=1.5)
    p.add_argument("--fps", type=float, default=15.0)
    p.add_argument("--threshold", type=int, default=8)
    p.add_argument("--align", type=int, default=8)

    # Misc
    p.add_argument("--freeze-ui", action="store_true",
                   help="SIGSTOP the Kindle window manager while running")
    p.add_argument("--no-screensaver", dest="no_screensaver",
                   action="store_true", default=True)
    p.add_argument("--allow-screensaver", dest="no_screensaver",
                   action="store_false")
    p.add_argument("--gui", action="store_true",
                   help="Also open a host-side Tkinter drawing window")
    p.add_argument("--cursor", choices=["off", "hover", "always"],
                   default="off",
                   help="Hover cursor on the Kindle: 'hover' (default) shows "
                        "a small reticle while the pen is in range but not "
                        "touching; 'always' keeps it visible during strokes "
                        "too; 'off' disables it. Only meaningful in canvas "
                        "mode.")
    p.add_argument("-v", "--verbose", action="count", default=0)

    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Logging                                                                    #
# --------------------------------------------------------------------------- #

def setup_logging(verbose: int) -> None:
    level = logging.INFO if verbose < 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("PIL.Image").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Screen size detection                                                      #
# --------------------------------------------------------------------------- #

def detect_screen_size() -> Optional[Tuple[int, int]]:
    """Best-effort primary-monitor resolution in logical pixels."""
    import json
    import shutil

    if shutil.which("wlr-randr"):
        try:
            out = subprocess.run(["wlr-randr", "--json"],
                                 capture_output=True, text=True,
                                 timeout=2).stdout
            for m in json.loads(out):
                if m.get("enabled"):
                    mode = m.get("current_mode") or {}
                    scale = m.get("scale") or 1
                    w = mode.get("width")
                    h = mode.get("height")
                    if w and h:
                        return int(w / scale), int(h / scale)
        except Exception:
            pass

    if shutil.which("kscreen-doctor"):
        try:
            out = subprocess.run(["kscreen-doctor", "-o"],
                                 capture_output=True, text=True,
                                 timeout=2).stdout
            m = re.search(r"Geometry:\s*\d+,\d+\s+(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass

    for tool in ("gnome-randr", "gnome-monitor-config"):
        if shutil.which(tool):
            try:
                argv = [tool] if tool == "gnome-randr" else [tool, "list"]
                out = subprocess.run(argv, capture_output=True, text=True,
                                     timeout=2).stdout
                m = re.search(r"(\d{3,5})x(\d{3,5})", out)
                if m:
                    return int(m.group(1)), int(m.group(2))
            except Exception:
                pass

    if shutil.which("xrandr"):
        try:
            out = subprocess.run(["xrandr", "--query"],
                                 capture_output=True, text=True,
                                 timeout=2).stdout
            for line in out.splitlines():
                if "*" in line:
                    m = re.search(r"(\d+)x(\d+)", line)
                    if m:
                        return int(m.group(1)), int(m.group(2))
        except Exception:
            pass

    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        if w and h:
            return int(w), int(h)
    except Exception:
        pass

    return None


# --------------------------------------------------------------------------- #
# Stylus reader thread                                                       #
# --------------------------------------------------------------------------- #

class StylusReader(threading.Thread):
    """Read the Kindle stylus stream and dispatch semantic events."""

    def __init__(
        self,
        kindle: KindleLink,
        dev: str,
        esz: int,
        cal: dict,
        pages: PageManager,
        injector,
        logger: StrokeLogger,
        streamer: Streamer,
        stop_event: threading.Event,
        target_size: Optional[Tuple[int, int]] = None,
        rotation: int = 0,
        letterbox: bool = True,
        stream_enabled: bool = True,
        cursor_mode: str = "hover",
    ) -> None:
        super().__init__(daemon=True, name="StylusReader")
        self.kindle = kindle
        self.dev = dev
        self.esz = esz
        self.cal = cal
        self.pages = pages
        self.injector = injector
        self.logger = logger
        self.streamer = streamer
        self.stop_event = stop_event
        self.target_size = target_size
        self.rotation = rotation
        self.letterbox = letterbox
        self.stream_enabled = stream_enabled
        self.cursor_mode = cursor_mode
        self._proc: Optional[subprocess.Popen] = None

    def run(self) -> None:
        log.info("stylus reader started on %s (esz=%d)", self.dev, self.esz)
        backoff = 0.5
        while not self.stop_event.is_set():
            try:
                self._loop()
                backoff = 0.5
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                log.warning("stylus stream error: %s", exc)
                time.sleep(backoff)
                backoff = min(5.0, backoff * 2)
        log.info("stylus reader stopped")

    def _loop(self) -> None:
        proc = self.kindle.popen(f"cat {self.dev}")
        self._proc = proc
        try:
            tracker = StylusTracker()
            buf = b""
            while not self.stop_event.is_set():
                chunk = proc.stdout.read(self.esz * 64)
                if not chunk:
                    if self.stop_event.is_set():
                        return
                    raise RuntimeError("stylus stream ended")
                buf += chunk
                while len(buf) >= self.esz:
                    ev = buf[:self.esz]
                    buf = buf[self.esz:]
                    t, c, v = unpack_event(ev, self.esz)
                    out = tracker.feed(t, c, v)
                    if out is not None:
                        self._dispatch(out)
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            self._proc = None

    def _dispatch(self, ev) -> None:
        if self.target_size is not None:
            # System injector: map onto the host screen with rotation.
            tw, th = self.target_size
            if self.letterbox:
                x, y, p = raw_to_target(self.cal, ev.x, ev.y, ev.pressure,
                                        tw, th, rotation=self.rotation)
            else:
                x, y, p = raw_to_target(self.cal, ev.x, ev.y, ev.pressure,
                                        tw, th, rotation=0)
                # Stretch mode ignores rotation's aspect effect but keeps
                # the axis swap so "up" on the Scribe still moves the
                # cursor up. Approximate by re-running without letterbox
                # via a small helper on top.
                x, y, p = self._stretch(ev.x, ev.y, ev.pressure, tw, th)
            x = max(0.0, min(tw - 1.0, x))
            y = max(0.0, min(th - 1.0, y))
        else:
            # Canvas mode: portrait only, no rotation.
            canvas = self.pages.current()
            cw, ch = canvas.width, canvas.height
            x, y, p = raw_to_canvas(self.cal, ev.x, ev.y, ev.pressure, cw, ch)
            x = max(0.0, min(cw - 1.0, x))
            y = max(0.0, min(ch - 1.0, y))
            canvas.set_brush_for(eraser=ev.eraser)

        if hasattr(self.injector, "set_eraser"):
            try:
                self.injector.set_eraser(ev.eraser)
            except Exception:
                log.debug("set_eraser failed", exc_info=True)

        # Hover cursor (canvas mode only; no-op for system injectors).
        if self.stream_enabled and getattr(self.streamer, "cursor_enabled", False):
            if ev.kind == "hover":
                self.streamer.set_cursor(x, y)
            elif ev.kind == "move" and self.cursor_mode == "always":
                self.streamer.set_cursor(x, y)
            elif ev.kind == "down":
                if self.cursor_mode != "always":
                    self.streamer.set_cursor(None)
            elif ev.kind == "up":
                if ev.in_range:
                    self.streamer.set_cursor(x, y)
                else:
                    self.streamer.set_cursor(None)
            elif ev.kind == "hover_end":
                self.streamer.set_cursor(None)


        if ev.kind == "down":
            self.logger.begin(x, y, p, eraser=ev.eraser)
            self.injector.down(x, y, p)
        elif ev.kind == "move":
            self.logger.point(x, y, p)
            self.injector.move(x, y, p)
        elif ev.kind == "hover":
            if hasattr(self.injector, "move_hover"):
                self.injector.move_hover(x, y, p)
        elif ev.kind == "up":
            self.logger.end(x, y, p)
            self.injector.up(x, y, p)

        if ev.button is not None and hasattr(self.injector, "set_stylus_button"):
            try:
                self.injector.set_stylus_button(ev.button)
            except Exception:
                log.debug("set_stylus_button failed", exc_info=True)
        if ev.button2 is not None and hasattr(self.injector, "set_stylus_button2"):
            try:
                self.injector.set_stylus_button2(ev.button2)
            except Exception:
                log.debug("set_stylus_button2 failed", exc_info=True)

        if self.stream_enabled:
            self.streamer.wake()

    def _stretch(self, rx, ry, pressure, tw, th):
        """--no-letterbox: rotate + stretch to fill, preserving axis swap."""
        from .calibration import apply_rotation, _transform_xy, _pressure
        px, py = _transform_xy(self.cal, rx, ry)
        panel = self.cal.get("panel", {"width": tw, "height": th})
        pw = panel["width"] or tw
        ph = panel["height"] or th
        ux, uy = apply_rotation(px, py, pw, ph, self.rotation)
        if self.rotation in (90, 270):
            rot_w, rot_h = ph, pw
        else:
            rot_w, rot_h = pw, ph
        x = (ux / rot_w) * tw
        y = (uy / rot_h) * th
        pmin = self.cal["pressure"]["min"]
        pmax = self.cal["pressure"]["max"]
        return x, y, _pressure(pmin, pmax, pressure)

    def stop(self) -> None:
        self.stop_event.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.join(timeout=3)


# --------------------------------------------------------------------------- #
# esz probe                                                                  #
# --------------------------------------------------------------------------- #

def probe_esz(kindle: KindleLink, dev: str, timeout: float = 12.0) -> int:
    log.info("probing event struct size — tap the stylus on the screen now")
    proc = kindle.popen(f"cat {dev}")
    try:
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = proc.stdout.read(24)
            if not chunk:
                break
            buf += chunk
            if len(buf) >= 48:
                esz = detect_esz(buf)
                log.info("detected event struct size: %d bytes", esz)
                return esz
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    log.warning("no stylus events detected during probe; assuming 24-byte struct")
    return 24


# --------------------------------------------------------------------------- #
# Colour parsing                                                             #
# --------------------------------------------------------------------------- #

_NAMED_COLORS = {
    "black":  (0, 0, 0),
    "white":  (255, 255, 255),
    "red":    (220, 50, 47),
    "green":  (60, 170, 60),
    "blue":   (50, 80, 220),
    "orange": (245, 130, 32),
    "yellow": (255, 200, 0),
    "purple": (150, 60, 200),
    "pink":   (240, 120, 180),
    "gray":   (100, 100, 100),
    "grey":   (100, 100, 100),
}


def _parse_color(s):
    if s is None:
        return None
    s = s.strip().lower()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(f"bad hex colour: {s!r}")
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            raise ValueError(f"bad hex colour: {s!r}")
    if s in _NAMED_COLORS:
        return _NAMED_COLORS[s]
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s)
    if m:
        vals = tuple(int(g) for g in m.groups())
        if any(v < 0 or v > 255 for v in vals):
            raise ValueError(f"rgb component out of range: {s!r}")
        return vals
    raise ValueError(
        f"cannot parse colour {s!r}: use '#rrggbb', '#rgb', 'rgb(r,g,b)', "
        f"or one of {', '.join(sorted(_NAMED_COLORS))}"
    )


# --------------------------------------------------------------------------- #
# Rotation resolution (system injectors only)                                #
# --------------------------------------------------------------------------- #

def _resolve_inject_rotation(raw: str, is_system_inject: bool) -> int:
    """Decide the panel rotation for system-injector mode.

    Canvas mode never rotates (the canvas is a fixed portrait buffer).
    System-injector mode auto-picks based on the host screen's shape.
    """
    if not is_system_inject:
        if raw != "auto":
            try:
                requested = int(raw)
            except (TypeError, ValueError):
                requested = 0
            if requested != 0:
                log.warning("--rotate %s ignored in canvas mode "
                            "(canvas is fixed portrait)", raw)
        return 0

    if raw == "auto":
        size = detect_screen_size()
        if size is None:
            log.warning("could not detect screen size; defaulting to "
                        "rotation=0. Pass --rotate 90 for landscape.")
            return 0
        w, h = size
        if w > h:
            log.info("host is landscape (%dx%d) -> rotation=90 "
                     "(use --rotate 270 if it feels mirrored)", w, h)
            return 90
        log.info("host is portrait (%dx%d) -> rotation=0", w, h)
        return 0

    try:
        rotation = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"--rotate must be auto/0/90/180/270, got {raw!r}")
    if rotation not in (0, 90, 180, 270):
        raise ValueError(f"--rotate must be 0/90/180/270, got {rotation}")
    return rotation


# --------------------------------------------------------------------------- #
# Config                                                                     #
# --------------------------------------------------------------------------- #

def build_config(args: argparse.Namespace) -> AppConfig:
    canvas_w = args.canvas_width or args.width
    canvas_h = args.canvas_height or args.height
    kd = KindleConfig()

    # --- pen --------------------------------------------------------- #
    pen = PenConfig()
    color = _parse_color(args.pen_color)
    if color is not None:
        pen.color = color
    if args.pen_size is not None:
        if args.pen_size <= 0:
            raise ValueError(f"--pen-size must be > 0, got {args.pen_size}")
        pen.size = float(args.pen_size)
    if args.eraser_size is not None:
        if args.eraser_size <= 0:
            raise ValueError(
                f"--eraser-size must be > 0, got {args.eraser_size}"
            )
        pen.eraser_size = float(args.eraser_size)
    if getattr(args, "pressure_sensitive", None) is not None:
        pen.pressure_sensitive = bool(args.pressure_sensitive)

    # --- inject target size ----------------------------------------- #
    is_system_inject = args.inject in ("uinput", "xdotool", "ydotool")
    if is_system_inject:
        inject_w, inject_h = 1920, 1080
        if args.inject_size in (None, "auto"):
            detected = detect_screen_size()
            if detected:
                inject_w, inject_h = detected
                log.info("detected primary monitor: %dx%d",
                         inject_w, inject_h)
            else:
                log.warning("could not detect screen size; falling back "
                            "to 1920x1080. Pass --inject-size WxH to override.")
        else:
            try:
                inject_w, inject_h = (
                    int(x) for x in args.inject_size.lower().split("x")
                )
            except Exception:
                raise ValueError(
                    f"--inject-size must be WxH or 'auto', got "
                    f"{args.inject_size!r}"
                )
    else:
        inject_w, inject_h = canvas_w, canvas_h

    # --- rotation (system injectors only) --------------------------- #
    rotation = _resolve_inject_rotation(args.rotate, is_system_inject)

    return AppConfig(
        kindle=KindleConfig(
            host=args.kindle or kd.host,
            port=args.port if args.port is not None else kd.port,
            user=args.user or kd.user,
            key=args.key or kd.key,
            fbink=args.fbink or kd.fbink,
            control_path=args.control_path,
            password=(args.password
                      or os.environ.get("KINDLE_SSH_PASSWORD")
                      or kd.password),
        ),
        panel=PanelConfig(
            width=args.width,
            height=args.height,
            aspect=args.aspect,
        ),
        stylus=StylusConfig(
            dev=args.stylus_dev,
            esz=args.esz,
            cal_path=args.cal_path,
            pressure_min=args.pressure_min,
            pressure_max=args.pressure_max,
        ),
        pen=pen,
        stream=StreamConfig(
            active_wf=args.active_wf,
            clean_wf=args.clean_wf,
            flash_wf=args.flash_wf,
            settle=args.settle,
            fps=args.fps,
            threshold=args.threshold,
            align=args.align,
        ),
        inject=InjectConfig(
            mode=args.inject,
            width=inject_w,
            height=inject_h,
            rotation=rotation,
        ),
        output=OutputConfig(
            save_png=args.save_png,
            save_pdf=args.save_pdf,
            save_json=args.save_json,
            autosave=args.autosave,
            pages_dir=args.pages_dir,
        ),
        canvas_width=canvas_w,
        canvas_height=canvas_h,
        calibrate=args.calibrate,
        no_input=args.no_input,
        freeze_ui=args.freeze_ui,
        no_screensaver=args.no_screensaver,
        verbose=args.verbose,
    )


# --------------------------------------------------------------------------- #
# Main                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    cfg = build_config(args)

    kindle = KindleLink(cfg.kindle)
    log.info("connecting to %s@%s:%d",
             cfg.kindle.user, cfg.kindle.host, cfg.kindle.port)
    try:
        info = kindle.probe()
    except Exception as exc:
        log.error("connection failed: %s", exc)
        log.error("(re-run with -v for the full ssh command)")
        kindle.close()
        return 2
    log.info("remote: %s", info["uname"])
    log.info("fbink: %s", info["fbink"])

    if args.list_devices:
        print(f"\n{len(info['devices'])} input devices on {cfg.kindle.host}:\n")
        for d in info["devices"]:
            print(f"  {d.get('event', '?'):<22}  "
                  f"name={d.get('name', '?')!r}")
            bm = d.get("bitmaps", {})
            if "ABS" in bm:
                print(f"      ABS=0x{bm['ABS']:x}  KEY=0x{bm.get('KEY', 0):x}")
        kindle.close()
        return 0

    # --- choose stylus device ---------------------------------------- #
    dev = cfg.stylus.dev
    if dev is None:
        stylus_info = find_stylus_device(info["devices"])
        if stylus_info is None:
            log.warning("no stylus device found")
        else:
            dev = stylus_info["event"]
            log.info("stylus device: %s (%s)", dev, stylus_info.get("name"))

    esz = cfg.stylus.esz
    if dev and esz is None:
        esz = probe_esz(kindle, dev)

    # --- calibration -------------------------------------------------- #
    if cfg.calibrate:
        if not dev:
            log.error("no stylus device — cannot calibrate")
            kindle.close()
            return 2

        try:
            kindle.enable_no_screensaver()
        except Exception:
            log.debug("enable_no_screensaver failed", exc_info=True)
        if cfg.freeze_ui:
            try:
                kindle.freeze_ui()
            except Exception:
                log.debug("freeze_ui failed", exc_info=True)

        log.info("calibrating using %s (portrait, native)", dev)
        try:
            samples = calibrate(
                kindle, cfg.panel.width, cfg.panel.height, dev, esz,
            )
        except Exception as exc:
            log.error("calibration failed: %s", exc)
            samples = None
        finally:
            if cfg.freeze_ui:
                try:
                    kindle.unfreeze_ui()
                except Exception:
                    pass
            try:
                kindle.disable_no_screensaver()
            except Exception:
                pass

        if not samples:
            kindle.close()
            return 3

        pmins = [s["pressure"] for s in samples]
        pmin = (cfg.stylus.pressure_min
                if cfg.stylus.pressure_min is not None else min(pmins))
        pmax = (cfg.stylus.pressure_max
                if cfg.stylus.pressure_max is not None else max(pmins))
        if pmax <= pmin:
            pmax = pmin + 4095

        transform = build_transform(samples)
        cal = {
            "dev": dev,
            "esz": esz,
            "panel":  {"width": cfg.panel.width,  "height": cfg.panel.height},
            "canvas": {"width": cfg.canvas_width, "height": cfg.canvas_height},
            "points": samples,
            "transform": transform,
            "pressure": {"min": pmin, "max": pmax},
        }
        save_calibration(cfg.stylus.cal_path, cal)
        log.info("wrote %s (panel %dx%d, portrait)",
                 os.path.expanduser(cfg.stylus.cal_path),
                 cfg.panel.width, cfg.panel.height)
        kindle.clear_screen()
        kindle.close()
        return 0

    # --- load calibration --------------------------------------------- #
    cal = None
    need_input = dev and not cfg.no_input and cfg.inject.mode != "none"
    if need_input:
        try:
            cal = load_calibration(cfg.stylus.cal_path)
            validate_calibration(cal)
            if transform_is_legacy(cal):
                log.warning(
                    "calibration uses the old diagonal transform; "
                    "re-run with --calibrate for best accuracy."
                )
            if cfg.stylus.pressure_min is not None:
                cal["pressure"]["min"] = cfg.stylus.pressure_min
            if cfg.stylus.pressure_max is not None:
                cal["pressure"]["max"] = cfg.stylus.pressure_max
            log.info("loaded calibration from %s", cfg.stylus.cal_path)
        except FileNotFoundError:
            log.warning("no calibration at %s — running in canvas mode only",
                        cfg.stylus.cal_path)
            if cfg.inject.mode != "canvas":
                log.error("inject mode %r requires calibration", cfg.inject.mode)
                kindle.close()
                return 2
            cal = None
        except Exception as exc:
            log.error("invalid calibration: %s", exc)
            kindle.close()
            return 2

    # --- components --------------------------------------------------- #
    pages = PageManager(cfg.canvas_width, cfg.canvas_height)
    stop_event = threading.Event()

    canvas = pages.current()
    canvas.set_pen_color(cfg.pen.color)
    canvas.set_pen_size(cfg.pen.size)
    canvas.set_eraser_size(cfg.pen.eraser_size)
    canvas.set_pressure_sensitive(cfg.pen.pressure_sensitive)
    log.info("pen: color=%s size=%.1f eraser=%.1f pressure=%s",
             cfg.pen.color, cfg.pen.size, cfg.pen.eraser_size,
             cfg.pen.pressure_sensitive)

    def _apply_pen_to_current():
        c = pages.current()
        c.set_pen_color(cfg.pen.color)
        c.set_pen_size(cfg.pen.size)
        c.set_eraser_size(cfg.pen.eraser_size)
        c.set_pressure_sensitive(cfg.pen.pressure_sensitive)

    pages.add_listener(_apply_pen_to_current)

    # --- injector ----------------------------------------------------- #
    is_system_inject = cfg.inject.mode in ("uinput", "xdotool", "ydotool")
    try:
        if cfg.inject.mode == "uinput":
            from .input_inject import UinputInjector
            injector = UinputInjector(
                cfg.inject.width, cfg.inject.height,
                sticky_click=args.sticky_click,
            )
        else:
            injector = build_injector(
                cfg.inject.mode,
                pages.current,
                cfg.inject.width if is_system_inject else cfg.canvas_width,
                cfg.inject.height if is_system_inject else cfg.canvas_height,
            )
    except Exception as exc:
        log.error("failed to build injector %r: %s", cfg.inject.mode, exc)
        kindle.close()
        return 2

    stream_enabled = not is_system_inject

    cursor_enabled = (args.cursor != "off") and stream_enabled
    streamer = Streamer(kindle, pages.current, cfg.stream, stop_event,
                        cursor_enabled=cursor_enabled)
    streamer.set_panel(cfg.panel.width, cfg.panel.height, cfg.panel.aspect)

    stroke_logger = StrokeLogger(
        pages.current,
        png_path=cfg.output.save_png,
        pdf_path=cfg.output.save_pdf,
        json_path=cfg.output.save_json,
        autosave_interval=cfg.output.autosave,
    )

    reader: Optional[StylusReader] = None
    if need_input and cal is not None:
        reader = StylusReader(
            kindle=kindle,
            dev=dev,
            esz=esz or 24,
            cal=cal,
            pages=pages,
            injector=injector,
            logger=stroke_logger,
            streamer=streamer,
            stop_event=stop_event,
            target_size=(cfg.inject.width, cfg.inject.height)
                        if is_system_inject else None,
            rotation=cfg.inject.rotation if is_system_inject else 0,
            letterbox=not args.no_letterbox,
            stream_enabled=stream_enabled,
            cursor_mode=args.cursor,
        )

    # --- Kindle housekeeping ------------------------------------------ #
    if cfg.no_screensaver:
        try:
            kindle.enable_no_screensaver()
        except Exception:
            log.debug("enable_no_screensaver failed", exc_info=True)
    if cfg.freeze_ui:
        try:
            kindle.freeze_ui()
        except Exception:
            log.debug("freeze_ui failed", exc_info=True)

    if stream_enabled:
        try:
            kindle.clear_screen()
        except Exception as exc:
            log.warning("clear_screen failed: %s", exc)

    def on_page_change() -> None:
        if stream_enabled:
            streamer.reset()

    pages.add_listener(on_page_change)

    # --- optional GUI -------------------------------------------------- #
    gui = None
    if args.gui and stream_enabled:
        try:
            from .gui import DrawingWindow
            gui = DrawingWindow(pages, streamer, injector=injector)
        except Exception as exc:
            log.warning("GUI unavailable: %s", exc)
            gui = None
    elif args.gui and not stream_enabled:
        log.warning("--gui is only meaningful with --inject canvas; ignoring")

    # --- signals ------------------------------------------------------ #
    
    def _toggle_sticky(*_sig) -> None:
        if not hasattr(injector, "toggle_sticky_click"):
            log.info("sticky click not supported by injector %r",
                     cfg.inject.mode)
            return
        new_state = injector.toggle_sticky_click()
        log.info("sticky click: %s", "ON" if new_state else "OFF")
    
    def _stop(*_sig) -> None:
        log.info("shutdown requested")
        stop_event.set()
        if stream_enabled:
            streamer.wake()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGUSR1, _toggle_sticky)


    

    # --- start threads ------------------------------------------------ #
    if stream_enabled:
        stroke_logger.start()
        streamer.start()
    if reader is not None:
        reader.start()

    log.info("running. inject=%s stream=%s target=%s rotation=%d° sticky=%s",
             cfg.inject.mode,
             "on" if stream_enabled else "off",
             f"{cfg.inject.width}x{cfg.inject.height}"
             if is_system_inject else "canvas",
             cfg.inject.rotation if is_system_inject else 0,
             "on" if getattr(injector, "sticky_click", False) else "off")

    # --- main loop ---------------------------------------------------- #
    try:
        while not stop_event.is_set():
            if gui is not None:
                gui.tick()
            time.sleep(0.05)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        if stream_enabled:
            streamer.wake()

        if reader is not None:
            reader.stop()
        if stream_enabled:
            streamer.join(timeout=4)
            stroke_logger.stop()
        try:
            injector.close()
        except Exception:
            pass

        try:
            if cfg.output.pages_dir:
                paths = pages.save_all_png(cfg.output.pages_dir)
                log.info("saved %d page PNGs to %s",
                         len(paths), cfg.output.pages_dir)
                pages.save_all_pdf(
                    os.path.join(cfg.output.pages_dir, "pages.pdf"))
            else:
                stroke_logger.save_all()
        except Exception:
            log.exception("final save failed")

        try:
            if cfg.freeze_ui:
                kindle.unfreeze_ui()
        except Exception:
            log.debug("unfreeze_ui failed", exc_info=True)
        try:
            if cfg.no_screensaver:
                kindle.disable_no_screensaver()
        except Exception:
            log.debug("disable_no_screensaver failed", exc_info=True)
        if stream_enabled:
            try:
                kindle.clear_screen()
            except Exception:
                log.debug("clear_screen failed", exc_info=True)
        kindle.close()
        if gui is not None:
            gui.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())