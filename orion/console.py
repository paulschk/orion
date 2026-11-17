"""Interactive shells into tenant instances.

Bridges a WebSocket to a real PTY running `incus exec`, so the browser gets a
genuine terminal rather than a "type a command, read the output" box: job
control, Ctrl-C, live resize and full-screen programs (top, vim, htop) all work.

Verified against a live container: prompt and echo, a real /dev/pts device
inside the container, SIGINT delivered to the foreground process on Ctrl-C, and
window-size changes propagating after a resize.
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from pathlib import Path

from .config import settings


class ConsoleError(RuntimeError):
    pass


class ConsoleSession:
    """One interactive shell inside one instance."""

    def __init__(self, incus_name: str, *, cols: int = 80, rows: int = 24,
                 shell: str = "bash -l"):
        self.incus_name = incus_name
        self.cols = cols
        self.rows = rows
        self.shell = shell
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._closed = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        master, slave = pty.openpty()
        self._resize_fd(slave, self.cols, self.rows)

        argv = [
            settings.incus_bin, "--project", settings.incus_project,
            "exec", self.incus_name, "--force-interactive", "--",
            "bash", "-lc", self.shell,
        ]
        env = dict(os.environ)
        env["INCUS_DIR"] = str(Path(settings.incus_socket).parent)
        env["TERM"] = "xterm-256color"
        env.setdefault("HOME", "/root")

        def child_setup() -> None:
            # Make the PTY the child's controlling terminal. Without this the
            # kernel never sends SIGWINCH when we resize the master, so the incus
            # client has nothing to forward and the remote shell keeps its
            # original dimensions forever.
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self._proc = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=child_setup, env=env, close_fds=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise ConsoleError(f"could not start the console: {exc}") from exc

        os.close(slave)
        self._master = master

    @property
    def fd(self) -> int:
        if self._master is None:
            raise ConsoleError("console not started")
        return self._master

    @property
    def alive(self) -> bool:
        return not self._closed and self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ plumbing

    def read(self) -> bytes:
        """Read pending output. Only call when the fd is readable.

        Returns b"" at end of stream (the shell exited, or the PTY went away).
        """
        if self._master is None or self._closed:
            return b""
        try:
            return os.read(self._master, 65536)
        except OSError:
            # EIO is the normal signal that the child closed the PTY.
            return b""

    def write(self, data: bytes) -> None:
        if self._master is None or self._closed or not data:
            return
        try:
            os.write(self._master, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self._master is None:
            return
        try:
            self._resize_fd(self._master, cols, rows)
        except OSError:
            pass

    @staticmethod
    def _resize_fd(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGHUP)
                proc.terminate()
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
