"""Tests against data recorded from real Claude Code, Codex and Warp runs.

Fixtures (trimmed, tool output removed) come from the 2026-10-04 live runs:
hook event logs, transcripts, and screenshots of Warp's vertical tabs sidebar.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from warp_agent import hooks, state, transcripts, warp

FIXTURES = Path(__file__).parent / "fixtures"


class HookReplayTest(unittest.TestCase):
    """Replay recorded hook payloads and check the status a supervisor would see."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ["WARP_AGENT_HOME"] = self.home.name
        os.environ["WARP_AGENT_NOTIFY"] = "0"
        os.environ["WARP_AGENT_ID"] = "replay"
        state.write_json(state.session_dir("replay") / "meta.json", {"id": "replay"})

    def tearDown(self):
        for key in ("WARP_AGENT_HOME", "WARP_AGENT_NOTIFY", "WARP_AGENT_ID"):
            os.environ.pop(key, None)
        self.home.cleanup()

    def replay(self, name):
        seen = []
        for line in (FIXTURES / name).read_text().splitlines():
            event = json.loads(line)
            hooks.handle(event.pop("agent"), json.dumps(event))
            status = state.read_json(state.session_dir("replay") / "status.json", {})
            seen.append((event["hook_event_name"], status.get("state"), status.get("detail")))
        return seen, state.read_json(state.session_dir("replay") / "status.json")

    def test_claude_permission_prompt_is_reported_then_finishes(self):
        seen, final = self.replay("events-claude-permission.jsonl")
        waiting = [s for s in seen if s[1] == "waiting"]
        self.assertTrue(waiting, seen)
        self.assertTrue(waiting[0][2].startswith("permission: Bash: date > notes.txt"))
        # Claude cancels a scheduled wakeup after Stop; that must not look like new work.
        self.assertEqual(seen[-2][0], "PreToolUse")
        self.assertEqual(final["state"], "done")

    def test_codex_permission_prompt_and_session_identity(self):
        seen, final = self.replay("events-codex-permission.jsonl")
        self.assertIn("waiting", [s[1] for s in seen])
        self.assertEqual(final["state"], "done")
        self.assertRegex(final["agent_session_id"], r"^[0-9a-f-]{36}$")
        self.assertIn(final["agent_session_id"], final["transcript_path"])

    def test_codex_autonomous_task_never_waits(self):
        seen, final = self.replay("events-codex-task.jsonl")
        self.assertNotIn("waiting", [s[1] for s in seen])
        self.assertEqual([s[1] for s in seen][-1], "done")
        self.assertIn("wc.py", final["last_prompt"])


class TranscriptTest(unittest.TestCase):
    def test_claude_last_reply_is_the_follow_up_answer(self):
        # The second turn asked for "the commit hash only"; the agent answered 0942579.
        self.assertEqual(transcripts.claude_last_reply(FIXTURES / "claude-transcript.jsonl"), "0942579")

    def test_codex_last_reply_includes_the_printed_output(self):
        reply = transcripts.codex_last_reply(FIXTURES / "codex-rollout.jsonl")
        self.assertIn("Committed as `ded1c4e`", reply)
        self.assertIn("the 4\ndog 3\na 2", reply)


class SidebarTest(unittest.TestCase):
    """Read tab groups from real Warp screenshots with macOS text recognition."""

    def groups(self, name, scale):
        found = warp.sidebar_groups(warp.recognize_text(FIXTURES / name), scale)
        return {group: [m for m in members if not m.endswith("main")] for group, members in found.items()}

    def test_two_groups_at_reduced_size(self):
        groups = self.groups("sidebar-pair-1400px.png", 1400 / 1280)
        self.assertEqual(groups["wa-test"], ["stats-fix-ac68", "wc-top-9eb8"])
        self.assertEqual(groups["wa-pair"], ["wa-pair-tab"])
        self.assertNotIn("new session", sum(groups.values(), []))

    def test_three_groups_on_retina(self):
        groups = self.groups("sidebar-three-groups-2x.png", 2.0)
        self.assertEqual(groups["wa-test"], ["stats-fix-ac68", "wc-top-9eb8", "stats-fork-3b19", "wc-fork-740d"])
        self.assertEqual(groups["wa-perm"], ["perm-claude-46ee", "perm-codex-d661", "trust-auto-d2a2"])
        self.assertEqual(groups["wa-pair"], ["wa-pair-tab"])


if __name__ == "__main__":
    unittest.main()
