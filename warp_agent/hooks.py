"""Turn Claude Code and Codex hook events into session status.

Hooks are installed per launch, not globally: Claude gets them through
`--settings <file>`, Codex through `-c hooks.<Event>=...` with
`--dangerously-bypass-hook-trust`. Both agents use the same event names.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import state

CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                 "PostToolUseFailure", "PermissionRequest", "Notification", "Stop",
                 "StopFailure", "SessionEnd"]
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                "PermissionRequest", "Stop"]
WAITING_TOOLS = {"AskUserQuestion", "request_user_input"}
EVENT_LOG_FIELD_LIMIT = 2000


def hook_command(agent: str) -> str:
    bin_path = Path(__file__).resolve().parent.parent / "bin" / "warp-agent"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(bin_path))} hook {agent}"


def claude_settings(path: Path) -> Path:
    command = hook_command("claude")
    hooks = {event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
             for event in CLAUDE_EVENTS}
    state.write_json(path, {"hooks": hooks})
    return path


def codex_overrides() -> list[str]:
    command = json.dumps(hook_command("codex"))
    args = []
    for event in CODEX_EVENTS:
        args += ["-c", f'hooks.{event}=[{{hooks=[{{type="command",command={command},timeout=10}}]}}]']
    return args


def _clip(value):
    if isinstance(value, str) and len(value) > EVENT_LOG_FIELD_LIMIT:
        return value[:EVENT_LOG_FIELD_LIMIT] + "…"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return value


def _summarize_tool(payload: dict) -> str:
    tool = payload.get("tool_name") or "tool"
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, dict):
        detail = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("description")
        if not detail and tool_input.get("questions"):
            detail = (tool_input["questions"][0] or {}).get("question")
        if detail:
            return f"{tool}: {str(detail)[:200]}"
    return tool


def transition(event: str, payload: dict) -> dict | None:
    """Map one hook event to status fields, or None to leave status unchanged."""
    if event == "SessionStart":
        return {"session_id": payload.get("session_id"),
                "transcript_path": payload.get("transcript_path"),
                "state": "idle", "detail": payload.get("source")}
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        return {"state": "working", "detail": None, "last_prompt": prompt[:500], "bump": True}
    if event == "PreToolUse":
        if payload.get("tool_name") in WAITING_TOOLS:
            return {"state": "waiting", "detail": _summarize_tool(payload)}
        return {"state": "working", "detail": _summarize_tool(payload)}
    if event in ("PostToolUse", "PostToolUseFailure"):
        return {"state": "working", "detail": None}
    if event == "PermissionRequest":
        return {"state": "waiting", "detail": "permission: " + _summarize_tool(payload)}
    if event == "Notification":
        kind = payload.get("notification_type")
        if kind in ("permission_prompt", "elicitation_dialog"):
            return {"state": "waiting", "detail": payload.get("message")}
        return None
    if event == "Stop":
        return {"state": "done", "detail": None,
                "last_message": payload.get("last_assistant_message")}
    if event == "StopFailure":
        return {"state": "failed", "detail": payload.get("error") or payload.get("message")}
    if event == "SessionEnd":
        return {"state": "ended", "detail": payload.get("reason")}
    return None


def handle(agent: str, stdin_text: str) -> None:
    session_id = os.environ.get("WARP_AGENT_ID")
    if not session_id:
        return
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        return
    event = payload.get("hook_event_name") or ""
    directory = state.session_dir(session_id)
    if not directory.exists():
        return
    with open(directory / "events.jsonl", "a") as log:
        log.write(json.dumps({"t": time.time(), "agent": agent, **_clip(payload)}) + "\n")
    fields = transition(event, payload)
    if fields is None:
        return
    before = state.read_json(directory / "status.json", {}).get("state")
    status = state.update_status(session_id, last_event=event, **fields)
    if status.get("state") in ("done", "waiting") and status["state"] != before:
        notify(session_id, status)


def notify(session_id: str, status: dict) -> None:
    if os.environ.get("WARP_AGENT_NOTIFY", "1") == "0":
        return
    session = state.Session(session_id)
    meta, pane = session.meta, session.pane
    if not meta.get("notify", True):
        return
    from . import transcripts
    message = status.get("last_message") or transcripts.last_reply(session) or ""
    if status["state"] == "waiting":
        message = status.get("detail") or "Needs your input"
    args = ["terminal-notifier",
            "-title", f"{meta.get('agent', 'agent')} · {session_id}",
            "-subtitle", ("You: " + (status.get("last_prompt") or ""))[:120],
            "-message", message[:300] or "Done",
            "-group", f"warp-agent-{session_id}"]
    if pane.get("focus_url"):
        args += ["-open", pane["focus_url"]]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass
