"""Drive the PTY wrapper with a real child process on a real pseudo-terminal."""

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from warp_agent import ptywrap

REPO = Path(__file__).resolve().parent.parent

# A stand-in for an agent TUI: switches the terminal to raw mode, optionally
# enables bracketed paste, records every byte it receives with a timestamp, and
# exits with code 7 once it receives Enter.
CHILD = textwrap.dedent("""
    import json, os, sys, termios, time, tty
    record, bracketed = sys.argv[1], sys.argv[2] == "1"
    tty.setraw(0)
    cols, rows = os.get_terminal_size(0)
    sys.stdout.write(f"ready {rows}x{cols}\\r\\n")
    if bracketed:
        sys.stdout.write("\\x1b[?2004h")
    sys.stdout.flush()
    chunks = []
    while True:
        data = os.read(0, 1024)
        chunks.append([time.monotonic(), data.decode("latin-1")])
        json.dump(chunks, open(record, "w"))
        if b"\\r" in data:
            sys.stdout.write("bye\\r\\n"); sys.stdout.flush()
            sys.exit(7)
""")

WRAPPER = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from warp_agent import ptywrap
    root = Path(sys.argv[2])
    code = ptywrap.run([sys.executable, str(root / "child.py"), str(root / "record.json"), sys.argv[3]],
                       root / "inbox", root / "output.log", winsize=(33, 101))
    (root / "exit").write_text(str(code))
""")


class PtyWrapperTest(unittest.TestCase):
    def run_session(self, bracketed: bool, message: dict):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "child.py").write_text(CHILD)
            wrapper = subprocess.Popen([sys.executable, "-c", WRAPPER, str(REPO), tmp, "1" if bracketed else "0"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while b"ready" not in (root / "output.log").read_bytes() if (root / "output.log").exists() else True:
                self.assertLess(time.monotonic(), deadline, "child never started")
                time.sleep(0.05)
            time.sleep(0.2)  # let the child enable bracketed paste before sending
            ptywrap.send(root / "inbox", message)
            self.assertEqual(wrapper.wait(timeout=10), 0)
            return (json.loads((root / "record.json").read_text()),
                    (root / "output.log").read_bytes(),
                    int((root / "exit").read_text()),
                    root / "inbox")

    def test_text_is_pasted_then_submitted_separately(self):
        chunks, log, code, inbox = self.run_session(True, {"text": "fix the bug\nthen commit", "enter": True})
        received = "".join(c[1] for c in chunks)
        self.assertEqual(received, "\x1b[200~fix the bug\nthen commit\x1b[201~\r")
        paste_time = next(t for t, data in chunks if "\x1b[200~" in data)
        enter_time = next(t for t, data in chunks if "\r" in data)
        self.assertGreaterEqual(enter_time - paste_time, 0.3)
        self.assertIn(b"ready 33x101", log)
        self.assertIn(b"bye", log)
        self.assertEqual(code, 7)
        self.assertFalse(inbox.exists())

    def test_plain_text_without_bracketed_paste_and_named_keys(self):
        chunks, _, code, _ = self.run_session(False, {"text": "1", "keys": ["down", "enter"]})
        self.assertEqual("".join(c[1] for c in chunks), "1\x1b[B\r")
        self.assertEqual(code, 7)

    def test_send_fails_when_no_wrapper_listens(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                ptywrap.send(Path(tmp) / "inbox", {"text": "hello"})


if __name__ == "__main__":
    unittest.main()
