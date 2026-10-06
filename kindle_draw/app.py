"""CLI entry point and orchestration for the Kindle Draw tool."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from .calibration import (
    build_transform,
    calibrate,
    load_calibration,
    raw_to_canvas,
    save_calibration,
    validate_calibration,
)
from .canvas import ERASER, HIGHLIGHTER, PEN
from .config import (
    AppConfig,
    InjectConfig,
    KindleConfig,
    OutputConfig,
    PanelConfig,
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
                   help="SSH ControlPath socket (default: /tmp/kindle-draw-*.sock)")
    p.add_argument("--password", default=None,
                   help="SSH password (requires sshpass; prefer SSH keys). "
                        "Also read from $KINDLE_SSH_PASSWORD.")
    p.add_argument("--list-devices", action="store_true",
                   help="Print the Kindle input device list and exit")

    # Panel
    p.add_argument("--width", type=int, default=1860,
                   help="Kindle panel width in pixels (portrait)")
    p.add_argument("--height", type=int, default=2480,
                   help="Kindle panel height in pixels (portrait)")
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

    # Injection
    p.add_argument("--inject",
                   choices=["uinput", "xdotool", "ydotool", "canvas", "none"],
                   default="canvas",
                   help="Where to send stylus input (default: draw on canvas)")

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
    p.add_argument("-v", "--verbose", action="count", default=0)

    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Logging                                                                    #
# --------------------------------------------------------------------------- #

def setup_logging(verbose: int) -> None:
    # 0 -> INFO (so --calibrate shows its prompts)
    # >=1 -> DEBUG
    level = logging.INFO if verbose < 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


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
        self._proc: Optional[subprocess.Popen] = None

    def run(self) -> None:
        log.info("stylus reader started on %s (esz=%d)", self.dev, self.esz)
        backoff = 0.5
        while not self.stop_event.is_set():
            try:
                self._loop()
                backoff = 0.5
            except Exception as exc:
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
        canvas = self.pages.current()
        cw, ch = canvas.width, canvas.height
        x, y, p = raw_to_canvas(self.cal, ev.x, ev.y, ev.pressure, cw, ch)
        x = max(0.0, min(cw - 1.0, x))
        y = max(0.0, min(ch - 1.0, y))

        # Brush selection is a no-op for non-canvas injectors, harmless.
        canvas.set_brush(ERASER if ev.eraser else PEN)

        if ev.kind == "down":
            self.logger.begin(x, y, p, eraser=ev.eraser)
            self.injector.down(x, y, p)
        elif ev.kind == "move":
            self.logger.point(x, y, p)
            self.injector.move(x, y, p)
        elif ev.kind == "up":
            self.logger.end(x, y, p)
            self.injector.up(x, y, p)

        self.streamer.wake()

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
    """Ask the user to tap the screen once so we can detect the struct size."""
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
# Main                                                                       #
# --------------------------------------------------------------------------- #

def build_config(args: argparse.Namespace) -> AppConfig:
    canvas_w = args.canvas_width or args.width
    canvas_h = args.canvas_height or args.height
    kd = KindleConfig()   # class defaults (your edited host/port/user)
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
        stream=StreamConfig(
            active_wf=args.active_wf,
            clean_wf=args.clean_wf,
            flash_wf=args.flash_wf,
            settle=args.settle,
            fps=args.fps,
            threshold=args.threshold,
            align=args.align,
        ),
        inject=InjectConfig(mode=args.inject),
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
    stylus_info = None
    if dev is None:
        stylus_info = find_stylus_device(info["devices"])
        if stylus_info is None:
            log.warning("no stylus device found")
        else:
            dev = stylus_info["event"]
            log.info("stylus device: %s (%s)", dev, stylus_info.get("name"))
    else:
        stylus_info = {"event": dev, "name": "manual"}

    esz = cfg.stylus.esz
    if dev and esz is None:
        esz = probe_esz(kindle, dev)

    # --- calibration -------------------------------------------------- #
    if cfg.calibrate:
        if not dev:
            log.error("no stylus device — cannot calibrate")
            kindle.close()
            return 2

        # Keep the panel awake and quiet while we ask the user to tap.
        try:
            kindle.enable_no_screensaver()
        except Exception:
            log.debug("enable_no_screensaver failed", exc_info=True)
        if cfg.freeze_ui:
            try:
                kindle.freeze_ui()
            except Exception:
                log.debug("freeze_ui failed", exc_info=True)

        log.info("calibrating using %s", dev)
        try:
            samples = calibrate(
                kindle, cfg.panel.width, cfg.panel.height, dev, esz
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
        log.info("wrote %s", os.path.expanduser(cfg.stylus.cal_path))
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

    try:
        injector = build_injector(cfg.inject.mode, pages.current,
                                  cfg.canvas_width, cfg.canvas_height)
    except Exception as exc:
        log.error("failed to build injector %r: %s", cfg.inject.mode, exc)
        kindle.close()
        return 2

    streamer = Streamer(kindle, pages.current, cfg.stream, stop_event)
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

    try:
        kindle.clear_screen()
    except Exception as exc:
        log.warning("clear_screen failed: %s", exc)

    # Page changes must trigger a full refresh.
    def on_page_change() -> None:
        streamer.reset()

    pages.add_listener(on_page_change)

    # --- optional GUI -------------------------------------------------- #
    gui = None
    if args.gui:
        try:
            from .gui import DrawingWindow
            gui = DrawingWindow(pages, streamer)
        except Exception as exc:
            log.warning("GUI unavailable: %s", exc)
            gui = None

    # --- signals ------------------------------------------------------ #
    def _stop(*_sig) -> None:
        log.info("shutdown requested")
        stop_event.set()
        streamer.wake()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # --- start threads ------------------------------------------------ #
    stroke_logger.start()
    streamer.start()
    if reader is not None:
        reader.start()

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
        streamer.wake()

        if reader is not None:
            reader.stop()
        streamer.join(timeout=4)
        stroke_logger.stop()
        try:
            injector.close()
        except Exception:
            pass

        # Final save.
        try:
            if cfg.output.pages_dir:
                paths = pages.save_all_png(cfg.output.pages_dir)
                log.info("saved %d page PNGs to %s", len(paths), cfg.output.pages_dir)
                pages.save_all_pdf(os.path.join(cfg.output.pages_dir, "pages.pdf"))
            else:
                stroke_logger.save_all()
        except Exception:
            log.exception("final save failed")

        # Restore Kindle state.
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