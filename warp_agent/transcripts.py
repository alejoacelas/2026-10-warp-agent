"""Read the latest assistant reply from Claude Code and Codex transcripts."""

from __future__ import annotations

import json
import re
from pathlib import Path

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")


def claude_transcript_path(directory: str, session_id: str) -> Path:
    project = re.sub(r"[^A-Za-z0-9]", "-", directory)
    return Path.home() / ".claude" / "projects" / project / f"{session_id}.jsonl"


def _lines(path: Path):
    try:
        with open(path) as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def claude_last_reply(path: Path) -> str | None:
    """Text of the last assistant message; blocks of one message share message.id."""
    last_id, texts = None, []
    for entry in _lines(path):
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        blocks = [b.get("text", "") for b in message.get("content") or []
                  if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        if not blocks:
            continue
        if message.get("id") != last_id:
            last_id, texts = message.get("id"), []
        texts.extend(blocks)
    return "\n".join(texts) if texts else None


def codex_last_reply(path: Path) -> str | None:
    reply = None
    for entry in _lines(path):
        payload = entry.get("payload") or {}
        if entry.get("type") == "response_item" and payload.get("type") == "message" \
                and payload.get("role") == "assistant":
            texts = [c.get("text", "") for c in payload.get("content") or []
                     if c.get("type") in ("output_text", "text")]
            if any(texts):
                reply = "\n".join(t for t in texts if t)
        elif entry.get("type") == "event_msg" and payload.get("type") == "task_complete" \
                and payload.get("last_agent_message"):
            reply = payload["last_agent_message"]
    return reply


def transcript_path(session) -> Path | None:
    status, meta = session.status, session.meta
    if status.get("transcript_path"):
        return Path(status["transcript_path"])
    if meta.get("agent") == "claude" and meta.get("claude_session_id"):
        return claude_transcript_path(meta["dir"], meta["claude_session_id"])
    return None


def last_reply(session) -> str | None:
    path = transcript_path(session)
    if path is None:
        return None
    if session.meta.get("agent") == "codex":
        return codex_last_reply(path)
    return claude_last_reply(path)


def log_tail(path: Path, max_bytes: int = 6000) -> str:
    try:
        data = path.read_bytes()[-max_bytes * 4:]
    except OSError:
        return ""
    text = ANSI.sub(b"", data).replace(b"\r", b"").decode("utf-8", "replace")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line.strip())[-max_bytes:]
