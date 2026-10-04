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
GRACE = 1.0  # shortened close-versus-quit wait for tests

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
                         root / "inbox", root / "output.log", root / "sock", winsize=(30, 100),
                         idle_limit=float(sys.argv[3]), is_busy=lambda: (root / "busy").exists(),
                         keep_running=sys.argv[4] == "1",
                         on_pane_closed=lambda running: (root / "closed").write_text(str(running)))
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
    idle_limit = 0  # seconds; subclasses turn the idle cleanup on
    keep_running = False  # the default: a vanished viewer stops the agent

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "child.py").write_text(CHILD)
        self.server = subprocess.Popen([sys.executable, "-c", SERVER, str(REPO), str(self.root),
                                        str(self.idle_limit), "1" if self.keep_running else "0"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       env={**os.environ, "WARP_AGENT_CLOSE_GRACE": str(GRACE)})

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

    def hang_up_viewer(self, app_pid, sig=signal.SIGHUP):
        """Run a viewer on a real terminal inside `app_pid`, then end it as Warp would."""
        env = {**os.environ, "WARP_AGENT_APP_PID": str(app_pid)}
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-c", VIEWER, str(REPO), str(self.root)], env)
        output = b""
        deadline = time.monotonic() + 10
        while b"size" not in output:
            self.assertLess(time.monotonic(), deadline, output)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                output += os.read(fd, 65536)
        os.kill(pid, sig)
        deadline = time.monotonic() + 5
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            self.assertLess(time.monotonic(), deadline, "viewer did not exit after SIGHUP")
            time.sleep(0.05)
        os.close(fd)
        if sig == signal.SIGHUP:
            self.assertEqual(os.waitstatus_to_exitcode(status), 2)  # closed, not "agent ended"

    def fake_warp(self):
        app = subprocess.Popen(["sleep", "60"])
        self.addCleanup(app.kill)
        return app

    def test_closing_the_pane_while_warp_runs_ends_the_agent(self):
        app = self.fake_warp()
        self.hang_up_viewer(app.pid)
        self.assertEqual(self.server.wait(timeout=GRACE + 5), 0)
        # SIGTERM from the server ends the child: exit status -15.
        self.assertEqual((self.root / "exit").read_text(), "-15")
        self.assertEqual((self.root / "closed").read_text(), "True")

    def test_quitting_warp_ends_the_agent_as_resumable(self):
        app = self.fake_warp()
        self.hang_up_viewer(app.pid)
        app.kill()
        app.wait()
        self.assertEqual(self.server.wait(timeout=GRACE + 5), 0)
        self.assertEqual((self.root / "exit").read_text(), "-15")
        self.assertEqual((self.root / "closed").read_text(), "False")

    def test_a_viewer_killed_outright_while_warp_runs_ends_the_agent(self):
        # Warp does not always hang a viewer up; whatever ends it, Warp still running means
        # the pane is gone for good.
        app = self.fake_warp()
        self.hang_up_viewer(app.pid, signal.SIGKILL)
        self.assertEqual(self.server.wait(timeout=GRACE + 5), 0)
        self.assertEqual((self.root / "exit").read_text(), "-15")

    def test_detach_keeps_the_agent_even_while_warp_runs(self):
        viewer = Client(self.root / "sock")
        viewer.send(b"A", struct.pack(">I", self.fake_warp().pid))
        viewer.read_until(b"size")
        ptywrap.send(self.root / "inbox", {"detach": True})
        self.assertTrue(viewer.closed())
        time.sleep(GRACE + 0.5)
        self.assertIsNone(self.server.poll())
        again = Client(self.root / "sock")
        again.send(b"D", b"q")
        self.assertTrue(again.closed())

    def _test_quitting_warp_leaves_the_agent_running(self):
        app = self.fake_warp()
        self.hang_up_viewer(app.pid)
        app.kill()
        app.wait()
        time.sleep(GRACE + 0.5)
        self.assertIsNone(self.server.poll())
        viewer = Client(self.root / "sock")
        viewer.send(b"D", b"x")
        viewer.read_until(b"got:x")
        viewer.send(b"D", b"q")
        self.assertTrue(viewer.closed())

    def _test_reattaching_within_the_grace_period_keeps_the_agent(self):
        app = self.fake_warp()
        self.hang_up_viewer(app.pid)
        viewer = Client(self.root / "sock")
        time.sleep(GRACE + 0.5)
        self.assertIsNone(self.server.poll())
        viewer.send(b"D", b"q")
        self.assertTrue(viewer.closed())


class BackgroundModeTest(ServerTest):
    """With keep_running (`--background`), quitting Warp leaves the agent running."""

    keep_running = True
    test_quitting_warp_leaves_the_agent_running = ServerTest._test_quitting_warp_leaves_the_agent_running
    test_reattaching_within_the_grace_period_keeps_the_agent = (
        ServerTest._test_reattaching_within_the_grace_period_keeps_the_agent)
    test_quitting_warp_ends_the_agent_as_resumable = None
    test_agent_starts_only_after_the_first_viewer = None
    test_input_inbox_and_exit_reach_the_right_places = None
    test_detach_keeps_the_agent_even_while_warp_runs = None


class IdleCleanupTest(ServerTest):
    """Agents left with no viewer and no turn running are stopped after the idle limit."""

    idle_limit = 1.0

    def detach_first_viewer(self):
        viewer = Client(self.root / "sock")
        viewer.read_until(b"size")
        ptywrap.send(self.root / "inbox", {"detach": True})
        self.assertTrue(viewer.closed())

    def test_an_idle_agent_with_no_viewer_is_stopped(self):
        self.detach_first_viewer()
        self.assertEqual(self.server.wait(timeout=self.idle_limit + 5), 0)
        self.assertEqual((self.root / "exit").read_text(), "-15")

    def test_a_busy_agent_is_kept_until_its_turn_ends(self):
        (self.root / "busy").touch()
        self.detach_first_viewer()
        time.sleep(self.idle_limit * 3)
        self.assertIsNone(self.server.poll())
        (self.root / "busy").unlink()
        self.assertEqual(self.server.wait(timeout=self.idle_limit + 5), 0)

    def test_an_idle_agent_shown_in_a_viewer_is_kept(self):
        viewer = Client(self.root / "sock")
        viewer.read_until(b"size")
        time.sleep(self.idle_limit * 3)
        self.assertIsNone(self.server.poll())
        viewer.send(b"D", b"q")
        self.assertTrue(viewer.closed())

    # The inherited ServerTest cases run with idle cleanup off.
    test_agent_starts_only_after_the_first_viewer = None
    test_input_inbox_and_exit_reach_the_right_places = None
    test_closing_the_pane_while_warp_runs_ends_the_agent = None
    test_a_viewer_killed_outright_while_warp_runs_ends_the_agent = None
    test_detach_keeps_the_agent_even_while_warp_runs = None
    test_quitting_warp_ends_the_agent_as_resumable = None


if __name__ == "__main__":
    unittest.main()
