"""Run an agent inside a pseudo-terminal that also listens on a named pipe.

Bytes pass through unchanged in both directions, so Warp still sees the
agent's own escape sequences (titles, notifications, status events). The
wrapper adds two things: messages written to the session's `inbox` pipe are
typed into the agent, and everything the agent prints is appended to
`output.log`.

Inbox messages are JSON lines: {"text": "...", "enter": true} or
{"keys": ["down", "enter"]}. Text is sent as a bracketed paste when the agent
has enabled bracketed paste mode, and Enter follows after a short pause so
the agent treats it as submitting rather than as part of the paste.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import signal
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


def _copy_winsize(src_fd: int, dst_fd: int) -> None:
    try:
        size = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


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


def run(argv: list[str], inbox: Path, log_path: Path, on_start=None, on_exit=None,
        on_output=None, winsize: tuple[int, int] | None = None) -> int:
    """Run argv under a PTY until it exits; return its exit code.

    `on_output(data)` sees each chunk the agent prints and may return a list of
    key names to type in reply (used to answer known startup prompts).
    """
    if inbox.exists():
        inbox.unlink()
    os.mkfifo(inbox, 0o600)
    # Opening read-write keeps the pipe from reporting end-of-file between senders.
    inbox_fd = os.open(inbox, os.O_RDWR | os.O_NONBLOCK)

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            os.write(2, f"warp-agent: cannot run {argv[0]}: {exc}\n".encode())
            os._exit(127)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    interactive = os.isatty(stdin_fd)
    saved_tty = termios.tcgetattr(stdin_fd) if interactive else None
    if winsize:
        _set_winsize(master, *winsize)
    elif interactive:
        _copy_winsize(stdin_fd, master)
    if interactive:
        tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, lambda *_: _copy_winsize(stdin_fd, master))
    # Forward termination to the agent so `stop` and closing the pane end it cleanly.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: os.kill(pid, signum))

    if on_start:
        on_start(pid)

    log = Log(log_path)
    bracketed = False
    pending: list[tuple[float, bytes]] = []
    inbox_buffer = b""
    watched = [master, inbox_fd] + ([stdin_fd] if interactive else [])

    try:
        while True:
            timeout = max(0.0, min(t for t, _ in pending) - time.monotonic()) if pending else None
            try:
                ready, _, _ = select.select(watched, [], [], timeout)
            except InterruptedError:
                continue
            now = time.monotonic()
            due = [item for item in pending if item[0] <= now]
            for item in due:
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
                if b"\x1b[?2004h" in data:
                    bracketed = True
                if b"\x1b[?2004l" in data:
                    bracketed = False
                os.write(stdout_fd, data)
                log.write(data)
                if on_output:
                    for offset, key in enumerate(on_output(data) or []):
                        pending.append((time.monotonic() + 0.3 + 0.2 * offset, KEYS.get(key, key.encode())))

            if interactive and stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if data:
                    os.write(master, data)

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
                        if bracketed:
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
        if saved_tty is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_tty)
        os.close(inbox_fd)
        try:
            inbox.unlink()
        except OSError:
            pass

    _, wait_status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(wait_status)
    if on_exit:
        on_exit(code)
    return code


def send(inbox: Path, message: dict) -> None:
    """Write one message to a wrapper's inbox; fail if no wrapper is listening."""
    try:
        fd = os.open(inbox, os.O_WRONLY | os.O_NONBLOCK)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("the agent is not running (no wrapper listening)") from exc
    try:
        os.write(fd, (json.dumps(message) + "\n").encode())
    finally:
        os.close(fd)
