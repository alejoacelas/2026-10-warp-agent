# warp-agent

Explores replicating Orca's agent workflow inside the Warp terminal: opening Claude
Code and Codex sessions inside chosen Warp tab groups and split panes, and
supervising them from a script or another agent.

The deliverables are the `warp-agent` CLI (Python 3.13, standard library only,
linked into `~/.local/bin`) and the Warp track of the `supervise-workers` skill in
`~/best/dotfiles/skills/supervise-workers/`, which keeps its Orca track. The skill
picks the track from the environment: `TERM_PROGRAM=WarpTerminal` means Warp,
`ORCA_TERMINAL_HANDLE` means Orca.

## Layout

- `warp_agent/cli.py`: commands (`new`, `panes`, `wait`, `read`, `send`, `focus`,
  `stop`, `ls`, `groups`, `shot`) and the run script each pane executes.
- `warp_agent/warp.py`: `warp://` links, tab config files, guarded keystrokes,
  screenshots and sidebar reading.
- `warp_agent/ptywrap.py`: the PTY wrapper every agent runs inside; it relays the
  keyboard and the session's `inbox` pipe, and logs output.
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
- Creating a group: command palette (Cmd+P), "Create tab group from active", which
  opens the new group's name for editing (`view.rs:7635`). Splitting: Cmd+D.
- Keystrokes need Accessibility for Warp and are guarded: modifier keys released,
  Warp frontmost with the expected window title. Type only `[A-Za-z0-9 /._;:=-]`:
  on this keyboard layout `~` arrived as `a`.
- A pane closes when its shell exits, so panes run `<run script>; exit`.
- Warp's `warp.sqlite` is not written because `restore_session = false`, and Warp's
  accessibility tree exposes only one text area. Check placement by screenshot and
  macOS text recognition (`warp-agent shot`). Recognition can return look-alike
  characters, such as a Cyrillic `е`; `warp.normalize` maps them.
- Hooks are passed per launch, never installed globally: Claude via
  `--settings <file>`, Codex via `-c hooks.<Event>=...` plus
  `--dangerously-bypass-hook-trust`. Codex fires `SessionStart` only with the first turn.
- Both agents show a folder-trust prompt in new directories before any hook runs.
  The wrapper's `TrustPromptWatcher` accepts it in the default mode and reports
  `waiting` with `--ask-permissions`. It strips escape sequences from the joined
  output tail, because sequences can span two reads.
- Codex 0.160 accepts only `on-request` and `never` approval policies; ask mode uses
  `-a on-request -s read-only`.

## Testing

- `python3 -m unittest discover -s tests` runs the fast tests: recorded hook events,
  transcripts and Warp screenshots from real runs, and the PTY wrapper driving a real
  child process.
- `WARP_AGENT_LIVE=1 python3 -m unittest tests.test_live -v` runs real Claude Code and
  Codex sessions in the live Warp app (about 2 minutes, default models). It opens a
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
