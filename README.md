# Kindle Scribe as a Graphics Tablet

**100% vibecoded.** No apology, no hidden agenda — the goal is to make a
jailbroken Kindle Scribe usable as a graphics tablet, and it does. Bug
reports and PRs welcome; don't expect beautiful architecture.

Turn a jailbroken Kindle Scribe into a drawing tablet for a host machine
(Linux first; macOS/Windows with extra plumbing).

Two independent modes:

- **Canvas mode** (`--inject canvas`, default) — the host owns a Pillow
  canvas, streams it to the Scribe over SSH + FBInk using adaptive
  waveforms, and draws strokes into the canvas from the Scribe's pen.
  Save PNG / PDF / JSON.
- **System tablet mode** (`--inject uinput`) — the Scribe becomes a
  real kernel-level Wacom-class tablet for the host. Cursor follows the
  pen, taps click, pressure flows into Krita/GIMP/Inkscape. Works on
  X11 **and** Wayland. Nothing is streamed to the Scribe in this mode.

Mixing them (draw on the Kindle while Krita is the target) is not
supported yet — one mode at a time.

---

## Requirements

**Host**

- Python ≥ 3.9
- `Pillow`
- `python-evdev` (only for `--inject uinput`; Linux)
- `PySide6` (only for the GUI launcher)
- Optional fallbacks: `xdotool` (X11), `ydotool` (Wayland)
- Optional extra window: `tkinter` (`--gui`, usually bundled with CPython)
- `sshpass` (only if you insist on password auth; SSH keys are preferred)

**Kindle**

- A **jailbroken Kindle Scribe** with SSH access
- `fbink` installed (any recent build; this project is tested against
  FBInk 1.25). If it lives somewhere non-standard, pass `--fbink`.

---

## Install

The project is a normal Python package (`pyproject.toml`):

```bash
# Core install (canvas mode)
pip install .

# With extras
pip install ".[uinput]"   # python-evdev, for --inject uinput
pip install ".[dev]"      # pytest, for running the test suite
pip install -e ".[uinput,dev]"   # editable, everything

# This also installs a `kindle-draw` console script.
```

`requirements.txt` is kept as a convenience mirror; `pyproject.toml` is
the canonical dependency list.

### uinput setup (one-time, for `--inject uinput`)

```bash
sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null <<'EOF'
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
EOF
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
sudo usermod -aG input "$USER"
# log out / back in so the group applies
```

---

## Running

### GUI launcher

```bash
kindle-draw            # or: python -m kindle_draw
```

Opens the Qt launcher: fill in the Kindle's IP/credentials, pick a mode,
launch. No arguments required.

### CLI

```bash
# Canvas mode (default): draw on a host canvas streamed to the Scribe
kindle-draw --kindle 192.168.1.14

# System tablet mode: pen controls the desktop
kindle-draw --kindle 192.168.1.14 --inject uinput

# See what input devices the Kindle exposes
kindle-draw --kindle 192.168.1.14 --list-devices

# Interactive 4-point stylus calibration (portrait, stored as JSON)
kindle-draw --kindle 192.168.1.14 --calibrate
```

Connection defaults live in `kindle_draw/config.py` (host, port `2022`,
user `root`, fbink path) and every one of them can be overridden with a
flag. Password auth works via `--password` or `$KINDLE_SSH_PASSWORD`
(requires `sshpass`), but an SSH key (`--key`) is strongly preferred.

### Frequently used flags

| Flag | What it does |
|---|---|
| `--inject canvas\|uinput\|xdotool\|ydotool\|none` | Where pen input goes |
| `--inject-size WxH\|auto` | Target resolution for system injectors (`auto` = primary monitor) |
| `--rotate 0\|90\|180\|270\|auto` | Panel rotation for system injectors (`auto` matches a landscape host screen) |
| `--sticky-click` | Start with the barrel button in click-hold mode |
| `--calibrate` / `--cal-path PATH` | Run / relocate the 4-point calibration |
| `--pressure-min` / `--pressure-max` | Override pressure range autodetection |
| `--pen-color`, `--pen-size`, `--eraser-size`, `--no-pressure` | Pen behaviour in canvas mode |
| `--save-png`, `--save-pdf`, `--save-json`, `--autosave SEC` | Canvas output (canvas mode) |
| `--active-wf`, `--clean-wf`, `--flash-wf`, `--fps`, `--threshold` | FBInk streaming tuning (canvas mode) |
| `--cursor off\|hover\|always` | Hover reticle on the Kindle (canvas mode) |
| `--gui` | Also open a host-side Tkinter window mirroring the canvas |
| `--freeze-ui` | SIGSTOP the Kindle window manager while running |
| `-v`, `-vv` | More logging |

`kindle-draw --help` lists everything.

---

## Calibration

Calibration is always done in **portrait** (1860×2480) and stored as
JSON at `~/.kindle_draw_cal.json`. Rotation for system injectors is
resolved at runtime, so one calibration works in both orientations.
Re-run `--calibrate` if strokes land offset, or hand-edit the JSON.

---

## Tests

The pure-logic parts (calibration math, evdev/uinput parsing, stylus
state machine, streamer geometry/diff helpers) have a pytest suite:

```bash
pytest          # works straight from a checkout, no install needed
```

The SSH, FBInk, and GUI paths are not covered — those need real hardware.

---

## Known limits

- One mode at a time; no Kindle-drawing-into-Krita hybrid yet.
- Canvas mode streams over SSH — latency is dominated by FBInk refresh
  timing, not the network.
- evdev bitmap parsing assumes the Kindle's 32-bit kernel word layout
  (true for the Scribe; a 64-bit host kernel reading Kindle devices
  remotely is not a supported setup).
- macOS/Windows: canvas mode can work with extra plumbing, uinput mode
  is Linux-only by definition.

## License

Apache-2.0 — see [LICENSE](LICENSE).
