"""The session server and viewer, with real processes, sockets and pseudo-terminals.

The child stands in for an agent TUI: it records when it started, switches on the
alternate screen, bracketed paste and the kitty keyboard protocol, reports every
window size it sees, echoes input, and exits on "q".
"""

import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from warp_agent import ptywrap

REPO = Path(__file__).resolve().parent.parent

CHILD = textwrap.dedent("""
    import os, signal, sys, time, tty
    open(sys.argv[1], "w").write(str(time.time()))
    tty.setraw(0)
    def size(*_):
        cols, rows = os.get_terminal_size(0)
        os.write(1, f"size {rows}x{cols}\\r\\n".encode())
    signal.signal(signal.SIGWINCH, size)
    os.write(1, b"\\x1b[?1049h\\x1b[?2004h\\x1b[>1u")
    size()
    while True:
        try:
            data = os.read(0, 1024)
        except InterruptedError:
            continue
        os.write(1, b"got:" + data.replace(b"\\x1b", b"ESC") + b"\\r\\n")
        if b"q" in data:
            sys.exit(5)
""")

SERVER = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from warp_agent import ptywrap
    root = Path(sys.argv[2])
    code = ptywrap.serve([sys.executable, str(root / "child.py"), str(root / "started")],
                         root / "inbox", root / "output.log", root / "sock", winsize=(30, 100))
    (root / "exit").write_text(str(code))
""")

VIEWER = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from warp_agent import ptywrap
    sys.exit(ptywrap.view(Path(sys.argv[2]) / "sock"))
""")


def frame(kind, payload):
    return kind + struct.pack(">I", len(payload)) + payload


class Client:
    """A minimal viewer speaking the server's frame protocol over the socket."""

    def __init__(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(str(path))
                break
            except OSError:
                self.sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.sock.settimeout(0.2)
        self.received = b""

    def send(self, kind, payload):
        self.sock.sendall(frame(kind, payload))

    def read_until(self, needle, timeout=10):
        deadline = time.monotonic() + timeout
        while needle not in self.received:
            if time.monotonic() > deadline:
                raise AssertionError(f"{needle!r} not received; got {self.received[-400:]!r}")
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise AssertionError(f"connection closed before {needle!r}")
            self.received += chunk
        return self.received

    def closed(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self.sock.recv(65536):
                    return True
            except socket.timeout:
                continue
        return False


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "child.py").write_text(CHILD)
        self.server = subprocess.Popen([sys.executable, "-c", SERVER, str(REPO), str(self.root)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)

    def tearDown(self):
        if self.server.poll() is None:
            self.server.kill()
        self.server.wait()
        subprocess.run(["pkill", "-f", str(self.root / "child.py")])
        self.tmp.cleanup()

    def test_agent_starts_only_after_the_first_viewer(self):
        time.sleep(1.0)
        self.assertFalse((self.root / "started").exists())
        connected_at = time.time()
        viewer = Client(self.root / "sock")
        viewer.send(b"W", struct.pack(">HH", 33, 101))
        viewer.read_until(b"size 33x101")
        self.assertGreaterEqual(float((self.root / "started").read_text()), connected_at - 0.05)

    def test_input_inbox_and_exit_reach_the_right_places(self):
        viewer = Client(self.root / "sock")
        viewer.read_until(b"size 30x100")
        viewer.send(b"D", b"abc")
        viewer.read_until(b"got:abc")
        # With the viewer gone, the server keeps the agent and still takes inbox messages.
        viewer.sock.close()
        time.sleep(0.3)
        ptywrap.send(self.root / "inbox", {"text": "hello", "enter": True})
        deadline = time.monotonic() + 5
        while b"ESC[200~hello" not in (self.root / "output.log").read_bytes():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertIsNone(self.server.poll())
        # A new viewer gets a cleared screen, the agent's modes, then a repaint.
        second = Client(self.root / "sock")
        first_bytes = second.read_until(b"\x1b[>1u")
        self.assertTrue(first_bytes.startswith(b"\x1b[2J\x1b[H\x1b[?1049h"), first_bytes[:40])
        self.assertIn(b"\x1b[?2004h", first_bytes)
        second.read_until(b"size 29x100")
        second.send(b"D", b"q")
        self.assertTrue(second.closed())
        self.assertEqual(self.server.wait(timeout=5), 0)
        self.assertEqual((self.root / "exit").read_text(), "5")

    def test_hanging_up_the_viewer_leaves_the_agent_running(self):
        # The viewer runs on a real terminal; SIGHUP is what Warp sends when it quits.
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-c", VIEWER, str(REPO), str(self.root)])
        output = b""
        deadline = time.monotonic() + 10
        while b"size" not in output:
            self.assertLess(time.monotonic(), deadline, output)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                output += os.read(fd, 65536)
        os.kill(pid, signal.SIGHUP)
        deadline = time.monotonic() + 5
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            self.assertLess(time.monotonic(), deadline, "viewer did not exit after SIGHUP")
            time.sleep(0.05)
        os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 2)  # closed, not "agent ended"
        time.sleep(0.3)
        self.assertIsNone(self.server.poll())
        viewer = Client(self.root / "sock")
        viewer.send(b"D", b"x")
        viewer.read_until(b"got:x")
        viewer.send(b"D", b"q")
        self.assertTrue(viewer.closed())


if __name__ == "__main__":
    unittest.main()
