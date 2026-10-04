"""Run an agent in a pseudo-terminal owned by a background server, viewed from Warp.

The server owns the agent's terminal, so the agent keeps running when Warp quits
or its pane closes. A Warp pane runs a viewer that connects to the server's Unix
socket and relays bytes both ways; a new viewer can attach at any time (after a
Warp restart, or from any terminal). Bytes pass through unchanged, so Warp still
sees the agent's escape sequences (titles, notifications, status events).

The server also:
- types messages written to the session's `inbox` pipe into the agent. Messages
  are JSON lines: {"text": "...", "enter": true} or {"keys": ["down", "enter"]}.
  Text is sent as a bracketed paste when the agent enabled bracketed paste mode,
  and Enter follows after a short pause so it submits rather than joins the paste;
- appends everything the agent prints to `output.log`;
- tracks the terminal modes the agent switched on (alternate screen, bracketed
  paste, mouse and focus reporting, keyboard protocol) and replays them to each
  new viewer, then nudges the window size so the agent repaints its screen.

Viewer-to-server frames: one type byte, a 4-byte big-endian length, the payload.
"D" carries input bytes; "W" carries the window size as two 16-bit integers
(rows, columns). Server-to-viewer traffic is the agent's raw output.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from pathlib import Path

KEYS = {
    "enter": b"\r", "esc": b"\x1b", "escape": b"\x1b", "tab": b"\t",
    "backspace": b"\x7f", "ctrl-c": b"\x03", "ctrl-d": b"\x04",
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "shift-tab": b"\x1b[Z", "space": b" ",
}
ENTER_DELAY = 0.4
LOG_LIMIT = 8 * 1024 * 1024
LOG_KEEP = 2 * 1024 * 1024
FIRST_VIEWER_TIMEOUT = 15.0

# DEC private modes worth restoring in a new viewer: cursor keys, cursor visibility,
# alternate screens, mouse reporting, focus reporting, bracketed paste.
TRACKED_MODES = {1, 25, 47, 1047, 1049, 1000, 1002, 1003, 1005, 1006, 1015, 1004, 2004}
DECSET = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
KITTY_KEYBOARD = re.compile(rb"\x1b\[([<>=])([0-9;]*)u")


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _get_winsize(fd: int) -> tuple[int, int] | None:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        return (rows, cols) if rows and cols else None
    except OSError:
        return None


class Log:
    def __init__(self, path: Path):
        self.path = path
        self.handle = open(path, "ab")

    def write(self, data: bytes) -> None:
        self.handle.write(data)
        self.handle.flush()
        if self.handle.tell() > LOG_LIMIT:
            self.handle.close()
            tail = self.path.read_bytes()[-LOG_KEEP:]
            self.path.write_bytes(tail)
            self.handle = open(self.path, "ab")


class TerminalModes:
    """The terminal modes an agent has switched on, rebuilt from its output."""

    def __init__(self):
        self.modes: dict[int, bool] = {}
        self.keyboard: list[str] = []  # kitty keyboard protocol flag stack
        self.tail = b""

    def feed(self, data: bytes) -> None:
        # Keep a short tail so a sequence split across two reads is still seen once.
        text = self.tail + data
        start = len(self.tail)
        for match in DECSET.finditer(text):
            if match.end() <= start:
                continue
            for mode in match.group(1).split(b";"):
                if mode.isdigit() and int(mode) in TRACKED_MODES:
                    self.modes[int(mode)] = match.group(2) == b"h"
        for match in KITTY_KEYBOARD.finditer(text):
            if match.end() <= start:
                continue
            kind, args = match.group(1), match.group(2).decode()
            if kind == b">":
                self.keyboard.append(args or "0")
            elif kind == b"<":
                for _ in range(int(args or "1")):
                    if self.keyboard:
                        self.keyboard.pop()
            elif kind == b"=" and self.keyboard:
                self.keyboard[-1] = args.split(";")[0] or "0"
        self.tail = text[-32:]

    @property
    def bracketed_paste(self) -> bool:
        return self.modes.get(2004, False)

    def replay(self) -> bytes:
        """Escape sequences that put a fresh terminal into the agent's current modes."""
        out = b""
        for mode in (1049, 1047, 47):  # enter the alternate screen first
            if self.modes.get(mode):
                out += b"\x1b[?%dh" % mode
                break
        for mode, on in sorted(self.modes.items()):
            if mode not in (1049, 1047, 47):
                out += b"\x1b[?%d%s" % (mode, b"h" if on else b"l")
        for flags in self.keyboard:
            out += f"\x1b[>{flags}u".encode()
        return out


def _frame(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def serve(argv: list[str], inbox: Path, log_path: Path, socket_path: Path | None = None,
          on_start=None, on_exit=None, on_output=None, on_viewer=None,
          winsize: tuple[int, int] | None = None) -> int:
    """Run argv under a PTY until it exits; return its exit code.

    With `socket_path`, wait for the first viewer before starting the agent (so
    its startup queries to the terminal get answers), then accept viewers for
    the rest of its life. `on_output(data)` sees each chunk and may return key
    names to type in reply; `on_viewer(connected)` is told when viewers come
    and go.
    """
    if inbox.exists():
        inbox.unlink()
    os.mkfifo(inbox, 0o600)
    # Opening read-write keeps the pipe from reporting end-of-file between senders.
    inbox_fd = os.open(inbox, os.O_RDWR | os.O_NONBLOCK)

    listener = viewer = None
    viewer_buffer = b""
    if socket_path is not None:
        if socket_path.exists():
            socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(4)
        listener.settimeout(FIRST_VIEWER_TIMEOUT)
        try:
            viewer, _ = listener.accept()
        except socket.timeout:
            viewer = None
        listener.settimeout(None)

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            os.write(2, f"warp-agent: cannot run {argv[0]}: {exc}\n".encode())
            os._exit(127)

    _set_winsize(master, *(winsize or (40, 120)))
    # Pass termination on to the agent so `stop` ends it cleanly.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: os.kill(pid, signum))
    if on_start:
        on_start(pid)
    if viewer is not None and on_viewer:
        on_viewer(True)

    log = Log(log_path)
    modes = TerminalModes()
    pending: list[tuple[float, bytes]] = []
    inbox_buffer = b""

    def drop_viewer():
        nonlocal viewer, viewer_buffer
        if viewer is not None:
            viewer.close()
            viewer, viewer_buffer = None, b""
            if on_viewer:
                on_viewer(False)

    def handle_frames():
        nonlocal viewer_buffer
        while len(viewer_buffer) >= 5:
            kind, length = viewer_buffer[:1], struct.unpack(">I", viewer_buffer[1:5])[0]
            if len(viewer_buffer) < 5 + length:
                return
            payload, viewer_buffer = viewer_buffer[5:5 + length], viewer_buffer[5 + length:]
            if kind == b"D":
                os.write(master, payload)
            elif kind == b"W" and len(payload) == 4:
                rows, cols = struct.unpack(">HH", payload)
                if rows and cols:
                    _set_winsize(master, rows, cols)

    try:
        while True:
            watched = [master, inbox_fd]
            if listener is not None:
                watched.append(listener)
            if viewer is not None:
                watched.append(viewer)
            timeout = max(0.0, min(t for t, _ in pending) - time.monotonic()) if pending else None
            try:
                ready, _, _ = select.select(watched, [], [], timeout)
            except InterruptedError:
                continue
            now = time.monotonic()
            for item in [item for item in pending if item[0] <= now]:
                pending.remove(item)
                os.write(master, item[1])

            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                modes.feed(data)
                log.write(data)
                if viewer is not None:
                    try:
                        viewer.sendall(data)
                    except OSError:
                        drop_viewer()
                if on_output:
                    for offset, key in enumerate(on_output(data) or []):
                        pending.append((time.monotonic() + 0.3 + 0.2 * offset, KEYS.get(key, key.encode())))

            if listener is not None and listener in ready:
                incoming, _ = listener.accept()
                drop_viewer()  # one viewer at a time; the newest wins
                viewer = incoming
                try:
                    viewer.sendall(b"\x1b[2J\x1b[H" + modes.replay())
                except OSError:
                    drop_viewer()
                if viewer is not None:
                    # Nudge the size so the agent repaints for the new viewer; the
                    # viewer's own size frame follows and settles the real size.
                    size = _get_winsize(master) or (40, 120)
                    _set_winsize(master, max(size[0] - 1, 1), size[1])
                    if on_viewer:
                        on_viewer(True)

            if viewer is not None and viewer in ready:
                try:
                    chunk = viewer.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    drop_viewer()
                else:
                    viewer_buffer += chunk
                    handle_frames()

            if inbox_fd in ready:
                try:
                    inbox_buffer += os.read(inbox_fd, 65536)
                except BlockingIOError:
                    pass
                while b"\n" in inbox_buffer:
                    line, inbox_buffer = inbox_buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    delay = 0.0
                    text = message.get("text")
                    if text:
                        payload = text.encode()
                        if modes.bracketed_paste:
                            payload = b"\x1b[200~" + payload + b"\x1b[201~"
                        os.write(master, payload)
                        delay = ENTER_DELAY
                    keys = list(message.get("keys") or [])
                    if message.get("enter"):
                        keys.append("enter")
                    for key in keys:
                        sequence = KEYS.get(key, key.encode())
                        if delay:
                            pending.append((time.monotonic() + delay, sequence))
                            delay += 0.15
                        else:
                            os.write(master, sequence)
    finally:
        os.close(inbox_fd)
        for path in (inbox, socket_path):
            try:
                if path is not None:
                    path.unlink()
            except OSError:
                pass
        if listener is not None:
            listener.close()

    _, wait_status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(wait_status)
    if on_exit:
        on_exit(code)
    if viewer is not None:
        viewer.close()  # the viewer sees end-of-file and exits after the exit code is saved
    return code


def run(argv, inbox, log_path, on_start=None, on_exit=None, on_output=None, winsize=None) -> int:
    """Run argv under a PTY with no viewer (used by tests and scripted runs)."""
    return serve(argv, inbox, log_path, None, on_start, on_exit, on_output, None, winsize)


def view(socket_path: Path, connect_timeout: float = 15.0) -> int:
    """Attach this terminal to a session server until the agent exits or we are closed.

    Returns 0 when the agent's session ended, 1 if no server could be reached.
    """
    deadline = time.monotonic() + connect_timeout
    while True:
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(str(socket_path))
            break
        except OSError:
            conn.close()
            if time.monotonic() > deadline:
                print(f"warp-agent: no session server at {socket_path}", file=sys.stderr)
                return 1
            time.sleep(0.05)

    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
    interactive = os.isatty(stdin_fd)
    saved = termios.tcgetattr(stdin_fd) if interactive else None

    def send_size(*_):
        size = _get_winsize(stdin_fd)
        if size:
            try:
                conn.sendall(_frame(b"W", struct.pack(">HH", *size)))
            except OSError:
                pass

    stop = False

    def closed(*_):
        nonlocal stop
        stop = True

    if interactive:
        tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, send_size)
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, closed)
    send_size()
    ended = False
    try:
        while not stop:
            watched = [conn] + ([stdin_fd] if interactive else [])
            try:
                ready, _, _ = select.select(watched, [], [], 0.5)
            except InterruptedError:
                continue
            if conn in ready:
                data = conn.recv(65536)
                if not data:
                    ended = True
                    break
                os.write(stdout_fd, data)
            if interactive and stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                conn.sendall(_frame(b"D", data))
    except OSError:
        pass
    finally:
        if saved is not None:
            # Leave the pane usable: exit the alternate screen and keyboard modes. The
            # terminal may already be gone (Warp quit), and nobody may be reading it, so
            # restore settings immediately rather than waiting for output to drain.
            try:
                os.write(stdout_fd, b"\x1b[?1049l\x1b[?2004l\x1b[?1004l\x1b[?1000l\x1b[?1006l\x1b[<99u\x1b[?25h")
                termios.tcsetattr(stdin_fd, termios.TCSANOW, saved)
            except (OSError, termios.error):
                pass
        conn.close()
    return 0 if ended else 2


def send(inbox: Path, message: dict) -> None:
    """Write one message to a session's inbox; fail if no server is listening."""
    try:
        fd = os.open(inbox, os.O_WRONLY | os.O_NONBLOCK)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("the agent is not running (no session server listening)") from exc
    try:
        os.write(fd, (json.dumps(message) + "\n").encode())
    finally:
        os.close(fd)
