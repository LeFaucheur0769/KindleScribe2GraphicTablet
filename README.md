# KindleScribe2GraphicTablet
100% vibecoded don't get angry the point was just to have it working


# Kindle Scribe as a Graphics Tablet

Turn a **jailbroken Kindle Scribe** into a drawing tablet for a host machine
(Linux first; macOS/Windows with extra plumbing).

- The host owns the canvas (Pillow).
- The canvas is streamed to the Scribe over SSH + FBInk using adaptive
  waveforms (DU while drawing, GL16/GC16 to settle).
- Stylus input on the Scribe is read via evdev, calibrated, and either drawn
  straight onto the canvas or injected into the host OS via `uinput`
  (works on X11 *and* Wayland), `xdotool` or `ydotool`.

## Requirements

- A **jailbroken Kindle Scribe** with SSH access and `fbink` installed.
- Host: Python ≥ 3.9, `Pillow`.
  - `--inject uinput` additionally needs `python-evdev` (Linux).
  - `--inject xdotool` needs `xdotool` (X11).
  - `--inject ydotool` needs `ydotool` (Wayland).
- Optional: `--gui` needs Tkinter (usually bundled with CPython).

```bash
pip install -r requirements.txt