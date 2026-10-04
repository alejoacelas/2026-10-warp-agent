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

When a viewer disappears without the server having asked it to (it was not
replaced, and no `detach` message came through the inbox), the server waits a few
seconds for a new viewer, then stops the agent and reports whether the Warp
process the viewer ran in (sent when it connected) was still running: still
running means the pane was closed (Cmd+W); gone means Warp quit, and the session
can be resumed when Warp restores its pane. With `keep_running`, an agent whose
Warp quit (or whose viewer ran outside Warp) is kept instead, for
`warp-agent restore`. This does not depend on how the viewer died: Warp may hang
it up, end it, or close its terminal.

An agent with no viewer that is not mid-turn is stopped once it has stayed that
way for `idle_limit` seconds, so agents left behind after quitting Warp do not
run until the next restart.

Viewer-to-server frames: one type byte, a 4-byte big-endian length, the payload.
"D" carries input bytes; "W" carries the window size as two 16-bit integers
(rows, columns); "A" carries the Warp app's process ID as a 32-bit integer.
Server-to-viewer traffic is the agent's raw output.
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
# How long after a hang-up to wait before deciding Warp did not quit.
CLOSE_GRACE = float(os.environ.get("WARP_AGENT_CLOSE_GRACE", "5"))
WARP_APP = "/Warp.app/Contents/MacOS/stable"

# DEC private modes worth restoring in a new viewer: cursor keys, cursor visibility,
# alternate screens, mouse reporting, focus reporting, bracketed paste.
TRACKED_MODES = {1, 25, 47, 1047, 1049, 1000, 1002, 1003, 1005, 1006, 1015, 1004, 2004}
DECSET = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
KITTY_KEYBOARD = re.compile(rb"\x1b\[([<>=])([0-9;]*)u")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def warp_app_pid() -> int | None:
    """The Warp app process this terminal runs in, found among its ancestors."""
    override = os.environ.get("WARP_AGENT_APP_PID")  # tests stand in a fake app
    if override:
        return int(override)
    import subprocess
    pid = os.getppid()
    for _ in range(16):
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        ppid, command = out.split(None, 1)
        if command.endswith(WARP_APP):
            return pid
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


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
          on_start=None, on_exit=None, on_output=None, on_viewer=None, on_pane_closed=None,
          winsize: tuple[int, int] | None = None, idle_limit: float = 0, is_busy=None,
          on_idle_stop=None, keep_running: bool = False) -> int:
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
    close_check: tuple[float, int] | None = None  # (deadline, Warp app pid) after a viewer vanished
    viewer_app_pid: int | None = None
    unattended_since: float | None = None  # when the agent last had a viewer or was busy
    idle_check_every = min(60.0, idle_limit / 4) if idle_limit else None

    def drop_viewer(reason: str):
        """Disconnect the viewer; one that vanished on its own may mean its pane closed."""
        nonlocal viewer, viewer_buffer, close_check, viewer_app_pid
        if viewer is not None:
            print(f"{time.ctime()}: viewer disconnected ({reason})", file=sys.stderr, flush=True)
            if reason == "vanished":
                close_check = (time.monotonic() + CLOSE_GRACE, viewer_app_pid or 0)
            viewer_app_pid = None
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
            elif kind == b"A" and len(payload) == 4:
                nonlocal viewer_app_pid
                viewer_app_pid = struct.unpack(">I", payload)[0]

    try:
        while True:
            watched = [master, inbox_fd]
            if listener is not None:
                watched.append(listener)
            if viewer is not None:
                watched.append(viewer)
            deadlines = [t for t, _ in pending] + ([close_check[0]] if close_check else [])
            timeout = max(0.0, min(deadlines) - time.monotonic()) if deadlines else None
            if idle_check_every:
                timeout = idle_check_every if timeout is None else min(timeout, idle_check_every)
            try:
                ready, _, _ = select.select(watched, [], [], timeout)
            except InterruptedError:
                continue
            now = time.monotonic()
            for item in [item for item in pending if item[0] <= now]:
                pending.remove(item)
                os.write(master, item[1])
            if idle_limit:
                if viewer is not None or (is_busy and is_busy()):
                    unattended_since = None
                elif unattended_since is None:
                    unattended_since = now
                elif now - unattended_since >= idle_limit:
                    print(f"{time.ctime()}: idle with no viewer for {idle_limit:g}s; stopping",
                          file=sys.stderr, flush=True)
                    unattended_since = None
                    if on_idle_stop:
                        on_idle_stop()
                    os.kill(pid, signal.SIGTERM)
            if close_check and close_check[0] <= now:
                app_pid = close_check[1]
                close_check = None
                warp_running = bool(app_pid) and _pid_alive(app_pid)
                print(f"{time.ctime()}: viewer gone {CLOSE_GRACE:g}s, new viewer={'yes' if viewer else 'no'}, "
                      f"Warp pid {app_pid or 'unknown'} running={warp_running}", file=sys.stderr, flush=True)
                if viewer is None and (warp_running or not keep_running):
                    if on_pane_closed:
                        on_pane_closed(warp_running)
                    os.kill(pid, signal.SIGTERM)

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
                        drop_viewer("vanished")
                if on_output:
                    for offset, key in enumerate(on_output(data) or []):
                        pending.append((time.monotonic() + 0.3 + 0.2 * offset, KEYS.get(key, key.encode())))

            if listener is not None and listener in ready:
                incoming, _ = listener.accept()
                drop_viewer("replaced")  # one viewer at a time; the newest wins
                close_check = None  # a viewer came back, so nothing was closed for good
                viewer = incoming
                try:
                    viewer.sendall(b"\x1b[2J\x1b[H" + modes.replay())
                except OSError:
                    drop_viewer("vanished")
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
                    drop_viewer("vanished")
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
                    if message.get("detach"):
                        drop_viewer("detached")
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
    return serve(argv, inbox, log_path, None, on_start=on_start, on_exit=on_exit,
                 on_output=on_output, winsize=winsize)


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
    app_pid = warp_app_pid()
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
    if app_pid:
        conn.sendall(_frame(b"A", struct.pack(">I", app_pid)))
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
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # the terminal went away (Warp closed the pane or quit)
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
