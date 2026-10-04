# warp-agent

Explores replicating Orca's agent workflow inside the Warp terminal: opening Claude
Code and Codex sessions inside chosen Warp tab groups and split panes, and
supervising them from a script or another agent.

The deliverables are the `warp-agent` CLI (Python 3.13, standard library only,
linked into `~/.local/bin`) and the Warp track of the `supervise-workers` skill in
`~/best/dotfiles/skills/supervise-workers/`, which keeps its Orca track. The skill
picks the track from the environment: `TERM_PROGRAM=WarpTerminal` means Warp,
`ORCA_TERMINAL_HANDLE` means Orca.

## Moving this repository

Absolute paths point into this checkout: the `~/.local/bin/warp-agent` link, and each
running session's `run` script, `claude-settings.json` and background server in
`~/.local/state/warp-agent/sessions/`. Stop running sessions first (`warp-agent ls`,
`warp-agent stop`), move the repository, then re-create the link:
`ln -sf <new path>/bin/warp-agent ~/.local/bin/warp-agent`. Update the `supervise-workers`
skill's link to this repository too.

## Layout

- `warp_agent/cli.py`: commands (`new`, `panes`, `wait`, `read`, `send`, `focus`,
  `stop`, `ls`, `groups`, `shot`, `view`, `detach`, `restore`, `prune`) and the run
  script each pane executes.
- `warp_agent/warp.py`: `warp://` links, tab config files, guarded keystrokes,
  screenshots and sidebar reading.
- `warp_agent/ptywrap.py`: the background session server that owns each agent's
  terminal (inbox pipe, output log, terminal-mode replay, close-versus-quit rule) and
  the viewer a Warp pane runs to show it. Its docstring describes the protocol.
- `warp_agent/hooks.py`: per-launch hook setup and the event-to-status mapping.
- `warp_agent/transcripts.py`: last-reply readers for Claude and Codex transcripts.
- State lives in `~/.local/state/warp-agent/` (`sessions/<id>/`, `groups.json`);
  `state.py` documents which process writes each file.

## How Warp is driven (verified live on Warp 0.2026.09.30)

Source references are to `reference/warp`.

- `open warp://tab_config/<stem>` opens `~/.warp/tab_configs/<stem>.toml` as a new
  tab in the front window (`app/src/uri/mod.rs:822`). Generated configs must have no
  `[params]`, which would show a modal. They are deleted once the pane starts.
- A new tab joins the active tab's group (`app/src/workspace/view.rs:13000`). To open
  in group G, `open` the focus URL of a live pane in G, then open the tab config.
- Each pane's shell has `WARP_FOCUS_URL`; the front window's title is its active
  tab's title, so `focus` confirms success by comparing titles.
- Creating a group: the ctrl-alt-cmd-g chord bound in `~/.warp/keybindings.yaml`
  (linked from `~/best/dotfiles/warp/`), which Warp loads only at startup; until
  then the command palette (Cmd+P, "Create tab group from active"). Either opens
  the new group's name for editing (`view.rs:7635`). Splitting: Cmd+D, then 1.0 s
  before typing; 0.3 s lost the typed command in live tests.
- Keystrokes need Accessibility for Warp and are guarded: modifier keys released,
  Warp frontmost with the expected window title. Type only `[A-Za-z0-9 /._;:=-]`:
  on this keyboard layout `~` arrived as `a`.
- A pane closes when its shell exits, so panes `exec` the session's viewer.
- Closing a pane (Cmd+W, then "Yes, close") keeps it for 60 s so Cmd+Shift+T can
  reopen it (`general.undo_close.grace_period`), then ends its process, not always
  with SIGHUP. The server stops the agent 5 s after its viewer vanishes if Warp is
  still running; if Warp quit, the agent keeps running for `restore`.
- Warp's accessibility tree exposes only one text area. Check placement by
  screenshot and macOS text recognition (`warp-agent shot`), which opens a closed
  sidebar with Cmd+Shift+B. Recognition can return look-alike characters (a Cyrillic
  `е`, `0` read as `o`); `warp.normalize` and `warp.match_name` handle them.
- Hooks are passed per launch, never installed globally: Claude via
  `--settings <file>`, Codex via `-c hooks.<Event>=...` plus
  `--dangerously-bypass-hook-trust`. Codex fires `SessionStart` only with the first turn.
- Both agents show a folder-trust prompt in new directories before any hook runs.
  The wrapper's `TrustPromptWatcher` accepts it in the default mode and reports
  `waiting` with `--ask-permissions`. It strips escape sequences from the joined
  output tail, because sequences can span two reads.
- Codex 0.160 accepts only `on-request` and `never` approval policies; ask mode uses
  `-a on-request -s read-only`.

## Checking survival across a Warp restart

This cannot run from inside Warp, because quitting Warp ends the session running the
check. With one or more agents running, quit Warp (Cmd+Q), reopen it, then run:

1. `warp-agent ls`: the agents are still listed as running, marked "(no pane)".
2. `warp-agent restore`: each session reports "restored pane" (Warp kept the pane's
   ID, so the session went back into its restored pane) or "new tab".
3. `warp-agent send <id> "..."` and `warp-agent wait <id>`: the agent still answers.

## Testing

- `python3 -m unittest discover -s tests` runs the fast tests: recorded hook events,
  transcripts and Warp screenshots from real runs, and the session server and viewer
  driving real child processes, sockets and terminals.
- `WARP_AGENT_LIVE=1 python3 -m unittest tests.test_live -v` runs real Claude Code and
  Codex sessions in the live Warp app (about 3 minutes, default models). It opens a
  new window and two groups; avoid typing in Warp while it runs.
- Test with real agents on their default models, on realistic tasks. Write no
  tautological tests: every assertion checks an outcome produced by an agent, Warp
  or the OS. New fixtures come from real runs, trimmed and with `tool_response`
  removed, because tool output can contain private files.

## Reference material

`reference/` is ignored by Git. Re-clone if missing:

- `reference/warp`: `git clone --depth 1 https://github.com/warpdotdev/warp`
- `reference/orca`: `git clone --depth 1 https://github.com/stablyai/orca`
- `reference/hartree`: Peter Hartree's HartreeWorks repos (`skill--pane`, `skill--tab`,
  `skill--window`, `claude-scripts`, `cr`).
