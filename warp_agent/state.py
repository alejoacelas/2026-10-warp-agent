"""Session and group state kept under ~/.local/state/warp-agent.

Each session directory holds files written by different processes, so no two
writers share a file:

- meta.json    the launcher: agent, directory, group, titles, session IDs
- pane.json    the run script inside Warp: focus URL and pane UUID
- proc.json    the PTY wrapper: process IDs and exit code
- status.json  the agent hooks (and the wrapper on exit): state and sequence number
- events.jsonl every hook payload, for debugging and transcript lookup
- sends.json   the status sequence number at the latest `send`, used by `wait`
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def home() -> Path:
    root = Path(os.environ.get("WARP_AGENT_HOME") or Path.home() / ".local/state/warp-agent")
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_dir() -> Path:
    path = home() / "sessions"
    path.mkdir(exist_ok=True)
    return path


def session_dir(session_id: str) -> Path:
    return sessions_dir() / session_id


def new_session_id(label: str) -> str:
    words = re.findall(r"[a-z0-9]+", label.lower())[:4]
    slug = "-".join(words)[:32].strip("-") or "agent"
    return f"{slug}-{secrets.token_hex(2)}"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def update_status(session_id: str, /, **fields) -> dict:
    """Merge fields into status.json; a changed `state` bumps `seq`."""
    directory = session_dir(session_id)
    with locked(directory / "status.lock"):
        status = read_json(directory / "status.json", {"seq": 0})
        if "state" in fields and fields["state"] != status.get("state"):
            status["seq"] = status.get("seq", 0) + 1
        elif fields.get("bump"):
            status["seq"] = status.get("seq", 0) + 1
        fields.pop("bump", None)
        status.update({k: v for k, v in fields.items() if v is not None})
        status["updated_at"] = time.time()
        write_json(directory / "status.json", status)
        return status


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Session:
    def __init__(self, session_id: str):
        self.id = session_id
        self.dir = session_dir(session_id)
        if not (self.dir / "meta.json").exists():
            raise KeyError(session_id)

    @classmethod
    def all(cls) -> list["Session"]:
        found = []
        for path in sorted(sessions_dir().iterdir(), key=lambda p: p.stat().st_mtime):
            if (path / "meta.json").exists():
                found.append(cls(path.name))
        return found

    @property
    def meta(self) -> dict:
        return read_json(self.dir / "meta.json", {})

    @property
    def pane(self) -> dict:
        return read_json(self.dir / "pane.json", {})

    @property
    def proc(self) -> dict:
        return read_json(self.dir / "proc.json", {})

    @property
    def status(self) -> dict:
        return read_json(self.dir / "status.json", {"seq": 0})

    def alive(self) -> bool:
        proc = self.proc
        return "exit_code" not in proc and pid_alive(proc.get("wrapper_pid"))


def resolve(prefix: str) -> Session:
    """Find a session by full ID or unique prefix."""
    try:
        return Session(prefix)
    except KeyError:
        pass
    matches = [s for s in Session.all() if s.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"warp-agent: no session matches {prefix!r}")
    raise SystemExit(f"warp-agent: {prefix!r} matches {', '.join(s.id for s in matches)}")


def groups_path() -> Path:
    return home() / "groups.json"


def load_groups() -> dict:
    return read_json(groups_path(), {})


def add_group_member(name: str, session_id: str) -> None:
    with locked(home() / "groups.lock"):
        groups = load_groups()
        group = groups.setdefault(name, {"members": [], "created_at": time.time()})
        if session_id not in group["members"]:
            group["members"].append(session_id)
        write_json(groups_path(), groups)
