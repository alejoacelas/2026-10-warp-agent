"""End-to-end checks with real Claude Code and Codex sessions in the running Warp app.

Opt in with WARP_AGENT_LIVE=1. Takes about 10 minutes and uses the agents'
default models. It opens a new Warp window, creates two tab groups, and needs
Accessibility access for Warp (group creation and pane splitting use keystrokes).
Avoid typing in Warp while it runs: keystroke steps abort if focus moves.

Every assertion checks something an agent, Warp or the OS produced: commits and
program output in scratch repos, replies, sidebar groups read from screenshots,
processes that exited.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from warp_agent import state

BIN = str(Path(__file__).resolve().parent.parent / "bin" / "warp-agent")
LIVE = os.environ.get("WARP_AGENT_LIVE") == "1"


def wa(*args, check=True, timeout=900):
    result = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode not in (0, 2):
        raise AssertionError(f"warp-agent {' '.join(args)} failed ({result.returncode}):\n"
                             f"{result.stdout}\n{result.stderr}")
    return result


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(root: Path, name: str, files: dict) -> Path:
    repo = root / name
    repo.mkdir()
    for path, text in files.items():
        (repo / path).write_text(textwrap.dedent(text).lstrip())
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def unit_tests_pass(repo) -> bool:
    return subprocess.run(["python3", "-m", "unittest", "-q"], cwd=repo, capture_output=True).returncode == 0


STATS = {
    "stats.py": """
        def mean(values):
            return sum(values) / len(values)


        def median(values):
            ordered = sorted(values)
            return ordered[len(ordered) // 2]
    """,
    "test_stats.py": """
        import unittest
        from stats import mean, median


        class StatsTest(unittest.TestCase):
            def test_mean(self):
                self.assertEqual(mean([1, 2, 3, 4]), 2.5)

            def test_median_odd(self):
                self.assertEqual(median([3, 1, 2]), 2)

            def test_median_even(self):
                self.assertEqual(median([4, 1, 3, 2]), 2.5)


        if __name__ == "__main__":
            unittest.main()
    """,
}

WORDCOUNT = {
    "wc.py": """
        import sys
        from collections import Counter


        def main(argv):
            counts = Counter(word.lower() for word in open(argv[1]).read().split())
            for word, n in sorted(counts.items()):
                print(word, n)


        if __name__ == "__main__":
            main(sys.argv)
    """,
    "sample.txt": "The cat saw the dog. The dog ran!\nA cat, a dog, the end.\n",
}


@unittest.skipUnless(LIVE, "set WARP_AGENT_LIVE=1 to run live Warp tests")
class LiveWarpTest(unittest.TestCase):
    sessions: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="warp-agent-live-", dir=Path.home() / ".cache"))
        tag = secrets.token_hex(2)
        cls.group_a, cls.group_b = f"live-a-{tag}", f"live-b-{tag}"
        cls.stats = make_repo(cls.root, "stats", STATS)
        cls.wc = make_repo(cls.root, "wordcount", WORDCOUNT)
        cls.perm = make_repo(cls.root, "perm", {"app.py": "print('hello')\n"})

    @classmethod
    def tearDownClass(cls):
        for session_id in cls.sessions.values():
            wa("stop", session_id, check=False)
        shutil.rmtree(cls.root, ignore_errors=True)

    def launch(self, key, *args):
        session_id = wa("new", "--no-notify", *args).stdout.strip().splitlines()[-1]
        self.sessions[key] = session_id
        return session_id

    def wait(self, session_id, expect="DONE", timeout=600):
        result = wa("wait", session_id, "--timeout", str(timeout), timeout=timeout + 30)
        self.assertTrue(result.stdout.startswith(expect), result.stdout)
        return result.stdout

    def sidebar(self, session_id):
        groups = json.loads(wa("shot", session_id).stdout)["groups"]
        return {g: [m for m in members if not m.endswith("main")] for g, members in groups.items()}

    def test_1_claude_in_new_group_fixes_bug(self):
        sid = self.launch("fix", "--window", "--group", self.group_a, "--dir", str(self.stats),
                          "--name", "live-fix",
                          "The median test fails. Fix stats.py so all tests pass, run them, and commit. "
                          "Reply with one sentence.")
        self.wait(sid)
        self.assertEqual(len(git(self.stats, "log", "--oneline").splitlines()), 2)
        self.assertTrue(unit_tests_pass(self.stats))
        self.assertIn(sid, self.sidebar(sid)[self.group_a])

    def test_2_follow_up_reaches_the_same_session(self):
        sid = self.sessions["fix"]
        wa("send", sid, "Now add mode(values) returning the most common value (smallest on ties) with a test, "
                        "run the tests, commit, and reply with the short commit hash only.")
        reply = self.wait(sid)
        head = git(self.stats, "rev-parse", "--short", "HEAD")
        self.assertIn(head[:7], reply)
        self.assertIn("def mode", (self.stats / "stats.py").read_text())
        self.assertTrue(unit_tests_pass(self.stats))

    def test_3_codex_joins_the_existing_group(self):
        sid = self.launch("wc", "--agent", "codex", "--group", self.group_a, "--dir", str(self.wc),
                          "--name", "live-wc",
                          "Make wc.py strip punctuation so 'dog.' and 'dog' are one word, and add --top N to print "
                          "only the N most frequent words (ties alphabetical). Commit.")
        self.wait(sid)
        out = subprocess.run(["python3", "wc.py", "sample.txt", "--top", "2"], cwd=self.wc,
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["the", "4", "dog", "3"])
        members = self.sidebar(sid)[self.group_a]
        self.assertIn(self.sessions["fix"], members)
        self.assertIn(sid, members)

    def test_4_two_agents_as_panes_in_a_second_group(self):
        manifest = self.root / "panes.json"
        manifest.write_text(json.dumps([
            {"agent": "claude", "dir": str(self.stats), "name": "live-pane-claude",
             "prompt": "Without editing anything, reply with the number of functions defined in stats.py."},
            {"agent": "codex", "dir": str(self.wc), "name": "live-pane-codex",
             "prompt": "Without editing anything, reply with the number of lines in sample.txt."},
        ]))
        title = f"pair-{self.group_b}"
        out = wa("panes", str(manifest), "--no-notify", "--group", self.group_b, "--title", title).stdout.split()
        self.sessions.update(pane_claude=out[0], pane_codex=out[1])
        functions = len(re.findall(r"^def ", (self.stats / "stats.py").read_text(), re.M))
        self.assertIn(str(functions), self.wait(out[0]))
        self.assertIn("2", self.wait(out[1]))
        groups = self.sidebar(out[0])
        self.assertEqual(groups[self.group_b], [title])
        self.assertIn(self.group_a, groups)

    def test_5_split_an_existing_pane(self):
        target = self.sessions["wc"]
        sid = self.launch("readme", "--split", target, "--dir", str(self.wc), "--name", "live-readme",
                          "Write README.md documenting wc.py's options with one real example each, then commit.")
        self.wait(sid)
        self.assertIn("README.md", git(self.wc, "show", "--stat", "--format=", "HEAD"))
        self.assertEqual(wa("focus", sid).returncode, 0)
        front = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of '
                                'front window of process "stable"'], capture_output=True, text=True).stdout.strip()
        self.assertEqual(front, state.Session(target).meta["tab_title"])

    def test_6_permission_prompts_are_reported_and_answerable(self):
        sid = self.launch("perm", "--ask-permissions", "--group", self.group_b, "--dir", str(self.perm),
                          "--name", "live-perm",
                          "Write the output of the date command to notes.txt and commit it.")
        self.assertIn("folder trust prompt", self.wait(sid, "WAITING", 90))
        wa("send", sid, "--key", "down", "--key", "enter")
        self.assertIn("permission", self.wait(sid, "WAITING", 300))
        while True:
            wa("send", sid, "--key", "1")
            result = wa("wait", sid, "--timeout", "300", timeout=330).stdout
            if result.startswith("DONE"):
                break
            self.assertTrue(result.startswith("WAITING"), result)
        self.assertIn("notes.txt", git(self.perm, "show", "--stat", "--format=", "HEAD"))

    def test_7_fork_remembers_the_original_conversation(self):
        sid = self.launch("fork", "--fork", self.sessions["fix"], "--group", self.group_a, "--dir", str(self.stats),
                          "--name", "live-fork",
                          "Without running commands or reading files: which function did you fix first, and "
                          "what was wrong with it? One sentence.")
        reply = self.wait(sid).lower()
        self.assertIn("median", reply)
        events = (state.session_dir(sid) / "events.jsonl").read_text().splitlines()
        self.assertFalse([e for e in events if json.loads(e).get("tool_name")])

    def test_8_stop_ends_the_process_and_closes_the_tab(self):
        sid = self.sessions["fork"]
        child = state.Session(sid).proc["child_pid"]
        wa("stop", sid)
        self.assertFalse(state.pid_alive(child))
        self.assertNotIn(sid, self.sidebar(self.sessions["fix"])[self.group_a])


if __name__ == "__main__":
    unittest.main()
