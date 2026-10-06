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
- Optional fallbacks: `xdotool` (X11), `ydotool` (Wayland)
- Optional GUI: `tkinter` (usually bundled with CPython)

**Kindle**

- A **jailbroken Kindle Scribe** with SSH access
- `fbink` installed (any recent build; this project is tested against
  FBInk 1.25). If it lives somewhere non-standard, pass `--fbink`.

**Install**

```bash
pip install -r requirements.txt

# For --inject uinput
sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null <<'EOF'
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
EOF
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
sudo usermod -aG input "$USER"
# log out / back in so the group applies