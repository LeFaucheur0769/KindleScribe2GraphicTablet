"""SSH transport to the Kindle.

Establishes a persistent ControlMaster once (interactively if needed), then
multiplexes all subsequent commands over that single TCP connection.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import List, Optional

from .config import KindleConfig
from .evdev_reader import parse_input_devices

log = logging.getLogger(__name__)


class KindleLink:
    def __init__(self, cfg: KindleConfig):
        self.cfg = cfg
        if cfg.control_path:
            self.control_path = os.path.expanduser(cfg.control_path)
        else:
            safe_host = cfg.host.replace("/", "_")
            self.control_path = os.path.join(
                tempfile.gettempdir(),
                f"kindle-draw-{cfg.user}-{safe_host}-{cfg.port}.sock",
            )

        self._target = f"{cfg.user}@{cfg.host}"
        self._closed = False
        self._master_up = False

    # ------------------------------------------------------------------ #
    # Master connection                                                   #
    # ------------------------------------------------------------------ #

    def _master_alive(self) -> bool:
        try:
            r = subprocess.run(
                ["ssh", "-O", "check",
                 "-o", f"ControlPath={self.control_path}",
                 self._target],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def ensure_master(self) -> None:
        """Bring up the SSH ControlMaster, prompting for a password if needed."""
        if self._master_up and self._master_alive():
            return
        if self._master_alive():
            self._master_up = True
            return

        argv: List[str] = [
            "ssh", "-f", "-N", "-M",
            "-S", self.control_path,
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ControlPersist=600",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(self.cfg.port),
        ]
        if self.cfg.key:
            argv += ["-i", os.path.expanduser(self.cfg.key)]
        argv.append(self._target)

        env = os.environ.copy()
        password = self.cfg.password or os.environ.get("KINDLE_SSH_PASSWORD")
        if password and shutil.which("sshpass"):
            argv = ["sshpass", "-e"] + argv
            env["SSHPASS"] = password
            log.info("establishing SSH ControlMaster (sshpass) to %s", self._target)
        else:
            log.info("establishing SSH ControlMaster to %s "
                     "(you may be prompted for a password)", self._target)

        # No capture_* here — ssh must inherit the terminal for the prompt.
        r = subprocess.run(argv, env=env)
        if r.returncode != 0:
            raise RuntimeError(
                f"SSH ControlMaster to {self._target}:{self.cfg.port} failed "
                f"(exit {r.returncode}). Check host/port/user/key."
            )
        self._master_up = True

    # ------------------------------------------------------------------ #
    # Command plumbing                                                    #
    # ------------------------------------------------------------------ #

    def _argv(self, remote_cmd: Optional[str] = None) -> List[str]:
        argv = [
            "ssh", "-T",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlMaster=no",
            "-o", "ControlPersist=600",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(self.cfg.port),
        ]
        if self.cfg.key:
            argv += ["-i", os.path.expanduser(self.cfg.key)]
        argv.append(self._target)
        if remote_cmd is not None:
            argv.append(remote_cmd)
        return argv

    def run(self, remote_cmd: str, data: Optional[bytes] = None,
            timeout: Optional[float] = None, check: bool = False
            ) -> subprocess.CompletedProcess:
        self.ensure_master()
        argv = self._argv(remote_cmd)
        log.debug("ssh run: %s", remote_cmd)
        r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"remote command failed ({r.returncode}): {remote_cmd}\n{err}"
            )
        return r

    def popen(self, remote_cmd: str) -> subprocess.Popen:
        self.ensure_master()
        argv = self._argv(remote_cmd)
        log.debug("ssh popen: %s", remote_cmd)
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            subprocess.run(
                ["ssh", "-O", "exit",
                 "-o", f"ControlPath={self.control_path}",
                 self._target],
                capture_output=True, timeout=5,
            )
        except Exception:
            log.debug("close: ssh -O exit failed (ignored)", exc_info=True)
        self._master_up = False

    # ------------------------------------------------------------------ #
    # Probes                                                              #
    # ------------------------------------------------------------------ #

    def probe(self) -> dict:
        self.ensure_master()
        r = self.run("echo ok", timeout=15)
        if r.returncode != 0 or r.stdout.strip() != b"ok":
            err = r.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"SSH to {self._target} failed: {err}")

        info: dict = {}

        r = self.run(f"command -v {shlex.quote(self.cfg.fbink)}")
        fbink_path = r.stdout.decode().strip() or None
        if not fbink_path:
            raise RuntimeError(
                f"fbink not found on remote (looked for {self.cfg.fbink!r}); "
                "pass --fbink /path/to/fbink"
            )
        info["fbink"] = fbink_path
        info["uname"] = self.run("uname -a").stdout.decode().strip()
        info["devices"] = self.get_input_devices()
        return info

    def get_input_devices(self) -> list:
        r = self.run("cat /proc/bus/input/devices")
        if r.returncode != 0:
            return []
        return parse_input_devices(r.stdout.decode("utf-8", "replace"))

    # ------------------------------------------------------------------ #
    # FBInk                                                               #
    # ------------------------------------------------------------------ #

    def draw_png(
        self,
        png_bytes: bytes,
        x: int = 0,
        y: int = 0,
        wf: Optional[str] = None,
        flash: bool = False,
        no_clear: bool = True,   # kept for API compat; not used
    ) -> None:
        # NOTE: waveform flag is UPPERCASE -W (--waveform MODE).
        # Lowercase -w is --wait; using it makes FBInk print the waveform
        # name as a STRING and skip the image.
        parts: List[str] = [self.cfg.fbink, "-q"]
        if flash:
            parts.append("-f")
        if wf:
            parts += ["-W", wf]
        parts += ["-g", f"file={self.cfg.tmp_png},x={x},y={y}"]

        cmd = (
            f"cat > {shlex.quote(self.cfg.tmp_png)} && "
            + " ".join(shlex.quote(a) for a in parts)
        )
        log.debug("fbink: %s", " ".join(parts))
        r = self.run(cmd, data=png_bytes, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(
                f"fbink failed: {r.stderr.decode('utf-8', 'replace')}"
            )


    def clear_screen(self) -> None:
        self.run(f"{self.cfg.fbink} -q -c -f")

    # ------------------------------------------------------------------ #
    # Kindle housekeeping                                                 #
    # ------------------------------------------------------------------ #

    def enable_no_screensaver(self) -> None:
        self.run("lipc-set-prop com.lab126.powerd preventScreenSaver 1")

    def disable_no_screensaver(self) -> None:
        self.run("lipc-set-prop com.lab126.powerd preventScreenSaver 0")

    def freeze_ui(self) -> None:
        self.run(
            "for p in awesome lab126_gui framework; do "
            "killall -STOP $p 2>/dev/null; done"
        )

    def unfreeze_ui(self) -> None:
        self.run(
            "for p in awesome lab126_gui framework; do "
            "killall -CONT $p 2>/dev/null; done"
        )