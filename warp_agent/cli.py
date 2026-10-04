"""warp-agent: open and supervise Claude Code and Codex sessions in Warp."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
import time
import uuid
from pathlib import Path

from . import hooks, ptywrap, state, transcripts, warp

BIN = Path(__file__).resolve().parent.parent / "bin" / "warp-agent"
ATTACH_TIMEOUT = 20.0
RETURNABLE = {"done", "waiting", "failed", "ended"}


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
    elif isinstance(data, str):
        print(data)
    else:
        for key, value in data.items():
            print(f"{key}: {value}")


# Launching --------------------------------------------------------------------

def _prompt_text(args) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text()
    if args.prompt == "-":
        return sys.stdin.read()
    return args.prompt or ""


def _fork_source(args) -> dict | None:
    if args.fork is None:
        return None
    if args.fork == "current":
        if args.agent == "claude":
            sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
            if not sid:
                raise SystemExit("warp-agent: --fork needs CLAUDE_CODE_SESSION_ID (run from Claude Code) or a session ID")
            return {"claude_session_id": sid}
        return {"codex_last": True}
    source = state.resolve(args.fork)
    meta, status = source.meta, source.status
    if meta["agent"] != args.agent:
        raise SystemExit(f"warp-agent: cannot fork a {meta['agent']} session with {args.agent}")
    if args.agent == "claude":
        return {"claude_session_id": meta["claude_session_id"], "session": source.id}
    if not status.get("agent_session_id"):
        raise SystemExit("warp-agent: that Codex session has not reported its ID yet")
    return {"codex_session_id": status["agent_session_id"], "session": source.id}


def create_session(args, prompt: str, group: str | None, tab_title: str | None = None) -> state.Session:
    directory = os.path.abspath(os.path.expanduser(args.dir or os.getcwd()))
    if not os.path.isdir(directory):
        raise SystemExit(f"warp-agent: directory does not exist: {directory}")
    session_id = state.new_session_id(args.name or prompt or args.agent)
    path = state.session_dir(session_id)
    path.mkdir(parents=True)
    meta = {
        "id": session_id,
        "agent": args.agent,
        "dir": directory,
        "group": group,
        "title": args.name or session_id,
        "tab_title": tab_title or session_id,
        "permissions": "ask" if args.ask_permissions else "bypass",
        "model": args.model,
        "agent_args": shlex.split(args.agent_args or ""),
        "fork": _fork_source(args),
        "notify": not args.no_notify,
        "created_at": time.time(),
    }
    if args.agent == "claude":
        meta["claude_session_id"] = str(uuid.uuid4())
    state.write_json(path / "meta.json", meta)
    (path / "prompt.txt").write_text(prompt)
    state.update_status(session_id, state="starting")
    run = path / "run"
    run.write_text(
        "#!/bin/zsh\n"
        f"export WARP_AGENT_ID={session_id}\n"
        f"export WARP_AGENT_HOME={shlex.quote(str(state.home()))}\n"
        f"cd {shlex.quote(directory)} || exit 1\n"
        f"{shlex.quote(sys.executable)} {shlex.quote(str(BIN))} _attach\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(BIN))} _wrap\n"
    )
    run.chmod(0o700)
    return state.Session(session_id)


def agent_argv(session: state.Session) -> list[str]:
    meta = session.meta
    prompt = (session.dir / "prompt.txt").read_text()
    fork = meta.get("fork") or {}
    bypass = meta["permissions"] == "bypass"
    if meta["agent"] == "claude":
        argv = ["claude"]
        if fork.get("claude_session_id"):
            argv += ["--resume", fork["claude_session_id"], "--fork-session"]
        argv += ["--session-id", meta["claude_session_id"], "--name", meta["title"],
                 "--settings", str(hooks.claude_settings(session.dir / "claude-settings.json"))]
        argv += ["--dangerously-skip-permissions"] if bypass else ["--permission-mode", "default"]
        if meta.get("model"):
            argv += ["--model", meta["model"]]
    else:
        argv = ["codex"]
        if fork.get("codex_session_id"):
            argv += ["fork", fork["codex_session_id"]]
        elif fork.get("codex_last"):
            argv += ["fork", "--last"]
        argv += hooks.codex_overrides() + ["--dangerously-bypass-hook-trust"]
        argv += ["--dangerously-bypass-approvals-and-sandbox"] if bypass else ["-a", "on-request", "-s", "read-only"]
        if meta.get("model"):
            argv += ["-m", meta["model"]]
    argv += meta.get("agent_args") or []
    if prompt.strip():
        argv += ["--", prompt] if meta["agent"] == "claude" else [prompt]
    return argv


def wait_attached(session: state.Session, timeout: float = ATTACH_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = session.pane
        if pane.get("focus_url"):
            return pane
        time.sleep(0.1)
    raise SystemExit(f"warp-agent: session {session.id} did not start in Warp within {timeout:.0f}s")


def run_command(session: state.Session, keep_pane: bool) -> str:
    command = str(session.dir / "run")
    return command if keep_pane else f"{command}; exit"


def find_anchor(group: str) -> state.Session | None:
    """Focus a live member of the group and return it, or None if none can be focused."""
    for member_id in reversed(state.load_groups().get(group, {}).get("members", [])):
        try:
            member = state.Session(member_id)
        except KeyError:
            continue
        if not member.alive() or not member.pane.get("focus_url"):
            continue
        if warp.focus(member.pane["focus_url"], member.meta["tab_title"]):
            return member
    return None


def place_tab(stem: str, panes: list[dict], title: str, group: str | None,
              new_window: bool, first: state.Session, split: str = "horizontal") -> None:
    """Open a tab config so the new tab lands in `group` (creating it if needed)."""
    warp.write_tab_config(stem, title, panes, split)
    try:
        anchor = find_anchor(group) if group and not new_window else None
        warp.open_tab_config(stem, new_window=new_window)
        pane = wait_attached(first)
        if group and anchor is None:
            if not warp.focus(pane["focus_url"], title):
                raise SystemExit(f"warp-agent: could not focus the new tab to create group {group!r}")
            warp.create_group_from_active_tab(group, title)
    finally:
        (warp.TAB_CONFIG_DIR / f"{stem}.toml").unlink(missing_ok=True)


def cmd_new(args) -> int:
    prompt = _prompt_text(args)
    if args.split:
        target = state.resolve(args.split)
        session = create_session(args, prompt, target.meta.get("group"), target.meta["tab_title"])
        if not target.alive() or not warp.focus(target.pane["focus_url"], target.meta["tab_title"]):
            raise SystemExit(f"warp-agent: could not focus {target.id} to split it")
        warp.split_and_run(run_command(session, args.keep_pane), target.meta["tab_title"], args.direction)
        wait_attached(session)
    else:
        session = create_session(args, prompt, args.group)
        place_tab(warp.TAB_CONFIG_PREFIX + session.id,
                  [{"directory": session.meta["dir"], "command": run_command(session, args.keep_pane)}],
                  session.meta["tab_title"], args.group, args.window, session)
    if session.meta.get("group"):
        state.add_group_member(session.meta["group"], session.id)
    _print({"id": session.id, "agent": session.meta["agent"], "dir": session.meta["dir"],
            "group": session.meta.get("group"), "focus_url": session.pane.get("focus_url")}
           if args.json else session.id, args.json)
    return 0


def cmd_panes(args) -> int:
    """Open several agents as split panes of one new tab."""
    tasks = json.loads(Path(args.manifest).read_text() if args.manifest != "-" else sys.stdin.read())
    if not isinstance(tasks, list) or not 2 <= len(tasks) <= 4:
        raise SystemExit("warp-agent: the manifest must be a JSON list of 2 to 4 tasks")
    title = args.title or state.new_session_id(args.group or "panes")
    sessions = []
    for task in tasks:
        task_args = argparse.Namespace(**{**vars(args), "agent": task.get("agent", args.agent),
                                          "dir": task.get("dir", args.dir), "name": task.get("name"),
                                          "model": task.get("model", args.model), "fork": None,
                                          "agent_args": task.get("agent_args", args.agent_args)})
        sessions.append(create_session(task_args, task["prompt"], args.group, title))
    panes = [{"directory": s.meta["dir"], "command": run_command(s, args.keep_pane)} for s in sessions]
    place_tab(warp.TAB_CONFIG_PREFIX + title, panes, title, args.group, args.window, sessions[0],
              "vertical" if args.stacked else "horizontal")
    for session in sessions[1:]:
        wait_attached(session)
    if args.group:
        for session in sessions:
            state.add_group_member(args.group, session.id)
    for session in sessions:
        print(session.id)
    return 0


# Running inside Warp ------------------------------------------------------------

def cmd_attach(args) -> int:
    session_id = os.environ["WARP_AGENT_ID"]
    state.write_json(state.session_dir(session_id) / "pane.json", {
        "focus_url": os.environ.get("WARP_FOCUS_URL"),
        "warp_session_uuid": os.environ.get("WARP_TERMINAL_SESSION_UUID"),
        "shell_pid": os.getppid(),
        "attached_at": time.time(),
    })
    return 0


class TrustPromptWatcher:
    """Handle the folder-trust prompt Claude Code and Codex show in a new directory.

    The prompt appears before any hook runs, so nothing else would report it.
    In the default mode (approvals bypassed) it is accepted for the directory the
    user chose; with --ask-permissions it is reported as waiting for a person.
    Terminal output draws spaces with cursor moves, so matching ignores whitespace.
    """

    # (text that identifies the prompt, keys that accept it given the screen so far)
    PROMPTS = {
        "claude": ("Yes,Itrustthisfolder",
                   lambda screen: ["down", "enter"]
                   if screen.rfind("❯No,exit") > screen.rfind("❯Yes,Itrustthisfolder") else ["enter"]),
        "codex": ("1.Trustandcontinue",
                  lambda screen: ["enter"] if screen.rfind("›1.Trustandcontinue") >= 0 else ["1"]),
    }

    def __init__(self, session: state.Session):
        self.session = session
        meta = session.meta
        self.marker, self.accept = self.PROMPTS[meta["agent"]]
        self.auto = meta.get("permissions") == "bypass"
        self.raw = b""
        self.buffer = ""
        self.done = False

    def __call__(self, data: bytes):
        if self.done:
            return None
        # Strip escape sequences from the joined tail, not per chunk: a sequence split
        # across two reads would otherwise leave fragments (like "1C") inside the text.
        self.raw = (self.raw + data)[-16000:]
        text = transcripts.ANSI.sub(b"", self.raw).decode("utf-8", "replace")
        self.buffer = "".join(text.split())
        if self.marker not in self.buffer:
            return None
        self.done = True
        if not self.auto:
            state.update_status(self.session.id, state="waiting", detail="folder trust prompt")
            return None
        return self.accept(self.buffer)


def cmd_wrap(args) -> int:
    session = state.Session(os.environ["WARP_AGENT_ID"])

    def on_start(child_pid):
        state.write_json(session.dir / "proc.json", {"wrapper_pid": os.getpid(),
                                                     "child_pid": child_pid,
                                                     "started_at": time.time()})

    def on_exit(code):
        proc = session.proc
        proc.update(exit_code=code, ended_at=time.time())
        state.write_json(session.dir / "proc.json", proc)
        state.update_status(session.id, state="exited", detail=f"exit code {code}")

    return ptywrap.run(agent_argv(session), session.dir / "inbox", session.dir / "output.log",
                       on_start=on_start, on_exit=on_exit, on_output=TrustPromptWatcher(session))


def cmd_hook(args) -> int:
    hooks.handle(args.agent, sys.stdin.read())
    return 0


# Supervising ----------------------------------------------------------------------

def cmd_wait(args) -> int:
    session = state.resolve(args.id)
    baseline = state.read_json(session.dir / "sends.json", {}).get("seq", 0)
    deadline = time.monotonic() + args.timeout
    while True:
        status = session.status
        result = None
        if status.get("seq", 0) > baseline and status.get("state") in RETURNABLE:
            result = status["state"]
        elif not session.alive() and session.proc:
            result = "exited"
        elif not session.proc and time.monotonic() > deadline:
            result = "timeout"
        elif time.monotonic() > deadline:
            result = "timeout"
        if result:
            break
        time.sleep(0.5)
    reply = transcripts.last_reply(session) if result in ("done", "waiting", "exited", "ended") else None
    output = {"id": session.id, "result": result, "state": status.get("state"),
              "detail": status.get("detail"), "last_message": reply}
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(result.upper() + (f": {status['detail']}" if status.get("detail") and result == "waiting" else ""))
        if reply:
            print(reply)
    return {"done": 0, "waiting": 2, "timeout": 124}.get(result, 3)


def cmd_read(args) -> int:
    session = state.resolve(args.id)
    if args.log:
        print(transcripts.log_tail(session.dir / "output.log", args.bytes))
        return 0
    reply = transcripts.last_reply(session)
    if reply is None:
        print("warp-agent: no reply yet", file=sys.stderr)
        return 1
    print(reply)
    return 0


def cmd_send(args) -> int:
    session = state.resolve(args.id)
    if not session.alive():
        raise SystemExit(f"warp-agent: {session.id} is not running")
    message = {"text": args.text, "enter": not args.no_enter and bool(args.text)}
    if args.key:
        message["keys"] = args.key
    state.write_json(session.dir / "sends.json", {"seq": session.status.get("seq", 0), "at": time.time()})
    try:
        ptywrap.send(session.dir / "inbox", message)
    except RuntimeError as exc:
        raise SystemExit(f"warp-agent: {exc}")
    return 0


def cmd_focus(args) -> int:
    session = state.resolve(args.id)
    if not session.pane.get("focus_url"):
        raise SystemExit(f"warp-agent: {session.id} has no Warp pane")
    ok = warp.focus(session.pane["focus_url"], session.meta["tab_title"])
    if not ok:
        print("warp-agent: Warp did not bring that tab to the front (was it closed?)", file=sys.stderr)
        return 1
    return 0


def cmd_stop(args) -> int:
    session = state.resolve(args.id)
    child = session.proc.get("child_pid")
    if not session.alive() or not state.pid_alive(child):
        print(f"{session.id} is not running")
        return 0
    os.kill(child, signal.SIGTERM)
    deadline = time.monotonic() + args.grace
    while time.monotonic() < deadline and session.alive():
        time.sleep(0.2)
    if session.alive() and state.pid_alive(child):
        os.kill(child, signal.SIGKILL)
        time.sleep(0.5)
    print(f"stopped {session.id}")
    return 0


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def cmd_ls(args) -> int:
    rows = []
    now = time.time()
    for session in state.Session.all():
        meta, status = session.meta, session.status
        alive = session.alive()
        if not args.all and not alive and now - meta.get("created_at", 0) > 3600:
            continue
        rows.append({
            "id": session.id, "agent": meta.get("agent"), "group": meta.get("group") or "",
            "state": status.get("state") if alive or status.get("state") == "exited" else "gone",
            "detail": status.get("detail") or "",
            "updated": _age(now - status.get("updated_at", now)), "dir": meta.get("dir"),
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    home = str(Path.home())
    for row in rows:
        print(f"{row['id']:<28} {row['agent']:<6} {row['group'][:14]:<14} {row['state']:<8} "
              f"{row['updated']:>4}  {row['dir'].replace(home, '~')}  {row['detail'][:60]}")
    return 0


def cmd_groups(args) -> int:
    groups = state.load_groups()
    for name, group in groups.items():
        live = [m for m in group["members"] if (state.session_dir(m) / "meta.json").exists()
                and state.Session(m).alive()]
        print(f"{name}: {len(live)} live of {len(group['members'])} sessions")
    return 0


def cmd_shot(args) -> int:
    """Screenshot the Warp window showing a session and report the sidebar's groups."""
    session = state.resolve(args.id)
    out = Path(args.out or session.dir / "screenshot.png")
    if not args.window and session.alive():
        # A window's title is its active tab's title, so bring the session's tab forward.
        warp.focus(session.pane["focus_url"], session.meta["tab_title"])
    scale = warp.screenshot(args.window or session.meta["tab_title"], out)
    groups = warp.sidebar_groups(warp.recognize_text(out), scale)
    print(json.dumps({"screenshot": str(out), "groups": groups}, indent=2))
    return 0


# Entry point ------------------------------------------------------------------------

def _launch_options(parser):
    parser.add_argument("--agent", choices=["claude", "codex"], default="claude")
    parser.add_argument("--dir", help="working directory (default: current)")
    parser.add_argument("--group", help="Warp tab group to open in; created if missing")
    parser.add_argument("--window", action="store_true", help="open in a new Warp window")
    parser.add_argument("--model", help="model override (default: the agent's default)")
    parser.add_argument("--agent-args", metavar="ARGS",
                        help='extra flags for the agent, e.g. "--profile cli -c model_reasoning_effort=high"')
    parser.add_argument("--ask-permissions", action="store_true",
                        help="let the agent ask before acting (default: bypass approvals)")
    parser.add_argument("--keep-pane", action="store_true",
                        help="keep the pane open after the agent exits")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warp-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="open an agent in a new Warp tab or pane")
    new.add_argument("prompt", nargs="?", help="initial prompt, or - to read stdin")
    new.add_argument("--prompt-file")
    new.add_argument("--name", help="session name (default: from the prompt)")
    new.add_argument("--split", metavar="ID", help="split this session's pane instead of opening a tab")
    new.add_argument("--direction", choices=["right", "down"], default="right")
    new.add_argument("--fork", nargs="?", const="current", metavar="ID",
                     help="copy a conversation: the current Claude session, or a warp-agent session")
    _launch_options(new)
    new.set_defaults(func=cmd_new)

    panes = sub.add_parser("panes", help="open 2-4 agents as split panes of one new tab")
    panes.add_argument("manifest", help='JSON list of {"prompt", "agent"?, "dir"?, "name"?}, or -')
    panes.add_argument("--title")
    panes.add_argument("--stacked", action="store_true", help="stack panes vertically")
    _launch_options(panes)
    panes.set_defaults(func=cmd_panes)

    wait = sub.add_parser("wait", help="wait until a session finishes, needs input, or exits")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, default=1200)
    wait.add_argument("--json", action="store_true")
    wait.set_defaults(func=cmd_wait)

    read = sub.add_parser("read", help="print the latest reply")
    read.add_argument("id")
    read.add_argument("--log", action="store_true", help="print recent terminal output instead")
    read.add_argument("--bytes", type=int, default=4000)
    read.set_defaults(func=cmd_read)

    send = sub.add_parser("send", help="type a message or keys into a running session")
    send.add_argument("id")
    send.add_argument("text", nargs="?", default="")
    send.add_argument("--no-enter", action="store_true")
    send.add_argument("--key", action="append", help="special key: enter, esc, up, down, tab, ctrl-c, ...")
    send.set_defaults(func=cmd_send)

    focus = sub.add_parser("focus", help="bring a session's pane to the front")
    focus.add_argument("id")
    focus.set_defaults(func=cmd_focus)

    stop = sub.add_parser("stop", help="end a session's agent process")
    stop.add_argument("id")
    stop.add_argument("--grace", type=float, default=5.0)
    stop.set_defaults(func=cmd_stop)

    ls = sub.add_parser("ls", help="list sessions")
    ls.add_argument("--all", action="store_true", help="include sessions that ended over an hour ago")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    groups = sub.add_parser("groups", help="list known tab groups")
    groups.set_defaults(func=cmd_groups)

    shot = sub.add_parser("shot", help="screenshot a session's window and read its sidebar groups")
    shot.add_argument("id")
    shot.add_argument("--out")
    shot.add_argument("--window", help="window title to capture instead")
    shot.set_defaults(func=cmd_shot)

    hook = sub.add_parser("hook")
    hook.add_argument("agent", choices=["claude", "codex"])
    hook.set_defaults(func=cmd_hook)
    sub.add_parser("_attach").set_defaults(func=cmd_attach)
    sub.add_parser("_wrap").set_defaults(func=cmd_wrap)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except warp.WarpError as exc:
        print(f"warp-agent: {exc}", file=sys.stderr)
        return 1
