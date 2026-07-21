"""True View: the real Claude Code TUI, streamed to the browser.

Each terminal owns a PTY running `claude --resume <claude_session_id>` and
fans raw bytes out to xterm.js clients over a WebSocket — the browser renders
the actual terminal UI (ttyd-style), so it mirrors Claude Code exactly with
no reimplemented chat to drift. The PTY outlives page loads (tmux-like):
re-attaching replays capped scrollback, and the TUI redraws on resize.

Queue protocol (per attached client): bytes = terminal output to forward
verbatim; dict = control message ({"type": "exit", ...}) after which the
stream is over.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import termios
import threading

SCROLLBACK_MAX = 512 * 1024
READ_CHUNK = 65536
KILL_GRACE_SECS = 3.0
DEFAULT_COLS, DEFAULT_ROWS = 100, 30


class TerminalError(RuntimeError):
    pass


class TerminalSession:
    def __init__(self, sid: str, argv: list[str], cwd: str):
        self.sid = sid
        self.argv = argv
        self.cwd = cwd
        self.loop = asyncio.get_running_loop()
        self.pid = -1
        self.fd = -1
        self.alive = False
        self.exit_reason = ""
        self.scrollback = bytearray()
        self.clients: set[asyncio.Queue] = set()
        self._fd_lock = threading.Lock()

    def spawn(self) -> None:
        # Env is fully prepared before fork: the child must only make
        # async-signal-safe calls (chdir, exec) between fork and exec.
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        argv = self.argv
        cwd = self.cwd
        pid, fd = pty.fork()
        if pid == 0:  # child — never returns
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except OSError:
                pass
            os._exit(127)
        self.pid, self.fd = pid, fd
        self.alive = True
        self._set_winsize(DEFAULT_COLS, DEFAULT_ROWS)
        threading.Thread(target=self._read_loop, daemon=True,
                         name=f"term-{self.sid}").start()

    # ---------- output pump (reader thread → event loop) ----------

    def _read_loop(self) -> None:
        while True:
            try:
                data = os.read(self.fd, READ_CHUNK)
            except OSError:  # EIO — child exited and output drained
                break
            if not data:
                break
            self.loop.call_soon_threadsafe(self._on_data, data)
        self.loop.call_soon_threadsafe(self._on_exit)

    def _on_data(self, data: bytes) -> None:
        self.scrollback.extend(data)
        if len(self.scrollback) > SCROLLBACK_MAX:
            del self.scrollback[:len(self.scrollback) - SCROLLBACK_MAX]
        for q in self.clients:
            q.put_nowait(data)

    def _on_exit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        with self._fd_lock:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1
        with contextlib.suppress(OSError):
            os.waitpid(self.pid, os.WNOHANG)
        msg = {"type": "exit", "reason": self.exit_reason or "Terminal ended"}
        for q in self.clients:
            q.put_nowait(msg)

    # ---------- client side ----------

    def attach(self, q: asyncio.Queue) -> bytes:
        """Register a client queue; returns scrollback to replay first."""
        self.clients.add(q)
        return bytes(self.scrollback)

    def detach(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def write(self, data: bytes) -> None:
        if self.alive:
            with contextlib.suppress(OSError):
                os.write(self.fd, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.alive and 0 < cols <= 1000 and 0 < rows <= 1000:
            self._set_winsize(cols, rows)

    def _set_winsize(self, cols: int, rows: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))

    def close(self, reason: str = "") -> None:
        """SIGHUP the TUI (clean exit, like closing a terminal window);
        escalate to SIGKILL if it lingers. The reader thread observes the
        death and finishes cleanup in _on_exit."""
        if not self.alive:
            return
        self.exit_reason = reason
        pid = self.pid
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGHUP)

        def _force():
            if self.alive and self.pid == pid:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.loop.call_later(KILL_GRACE_SECS, _force)


class TerminalManager:
    def __init__(self) -> None:
        self.terms: dict[str, TerminalSession] = {}

    def get(self, sid: str) -> TerminalSession | None:
        term = self.terms.get(sid)
        if term is not None and not term.alive:
            self.terms.pop(sid, None)
            return None
        return term

    def open(self, sid: str, argv: list[str], cwd: str) -> TerminalSession:
        term = self.get(sid)
        if term is not None:
            return term
        term = TerminalSession(sid, argv, cwd)
        term.spawn()
        self.terms[sid] = term
        return term

    def close(self, sid: str, reason: str = "") -> None:
        term = self.terms.pop(sid, None)
        if term is not None:
            term.close(reason)

    def close_all(self) -> None:
        for sid in list(self.terms):
            self.close(sid, "Server shutting down")


def claude_argv(resume_id: str, model: str, mode: str) -> list[str]:
    """Command line for a TUI that continues the given claude session with
    the webapp session's model/mode settings."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resume_id or ""):
        raise TerminalError("invalid resume session id")
    exe = shutil.which("claude")
    if not exe:
        raise TerminalError("claude CLI not found on PATH")
    argv = [exe, "--resume", resume_id]
    if model and model != "default":
        argv += ["--model", model]
    if mode and mode != "default":
        argv += ["--permission-mode", mode]
    return argv
