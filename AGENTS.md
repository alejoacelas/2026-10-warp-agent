# warp-agent

Explores replicating Orca's agent workflow inside the Warp terminal. The two goals:
opening Claude Code and Codex sessions inside chosen Warp tab groups (and split
panes), and supervising those sessions from a script or another agent.

The deliverable is a `warp-agent` CLI (Python 3.13, standard library only) plus a
Warp track in the `supervise-workers` skill (`~/best/dotfiles/skills/supervise-workers/`),
which keeps its existing Orca track. The skill picks the track from the supervisor's
environment: `TERM_PROGRAM=WarpTerminal` means Warp, `ORCA_*` variables mean Orca.

## Features to build

1. Group registry: each tab group's name maps to the focus URL of one live pane in it.
2. Group launch: `warp-agent new --group G --dir D "prompt"` focuses a pane in G, then opens the agent in a new tab there.
3. New group: `warp-agent group new G` creates a Warp tab group and records it.
4. Panes: launch several agents as split panes in one new tab, or split an existing agent's pane.
5. Prompt handoff: prompts go in a file and the agent starts with `"$(cat file)"`; nothing is pasted.
6. Fork mode: `--fork` copies the current conversation (`claude --resume $CLAUDE_CODE_SESSION_ID --fork-session`, `codex fork --last`).
7. Fixed session IDs: Claude starts with `--session-id <uuid>` and `--name <slug>`, so the transcript path and tab title are known up front.
8. Status files: Claude and Codex hooks write each session's state (working, waiting for permission, done, failed) to a small file.
9. `warp-agent wait <id>`: blocks until the agent finishes, needs an answer, exits, or times out, then prints its last message.
10. `warp-agent read <id>`: prints the agent's latest reply from its transcript.
11. `warp-agent send <id> "text"`: types a follow-up into a running agent through a PTY wrapper every agent runs inside.
12. `warp-agent focus <id>`: brings the agent's pane to the front.
13. `warp-agent stop <id>`: ends the agent's process.
14. `warp-agent ls`: lists sessions with group, directory, status and last activity.
15. Notifications: `terminal-notifier` (installed) alerts on done or permission; clicking opens the agent's `warp://session/...` URL.

## How Warp can be driven (verified in Warp's source, installed version 0.2026.09.30)

Line references are to `reference/warp` (shallow clone of warpdotdev/warp, AGPL).

- `open warp://tab_config/<stem>` opens `~/.warp/tab_configs/<stem>.toml` as a new
  tab in the front window, with directory, commands, title, color and splits
  (`app/src/uri/mod.rs:822`). A config with `[params]` shows a modal, so generated
  configs must have none. Configs are re-read on every open.
- New tabs, including tab-config tabs, join the active tab's group when the "new
  tab placement" setting is "after current tab", the default
  (`app/src/workspace/view.rs:13000`, `:13068`). So: focus a pane in the group,
  then open the tab config.
- Every pane's shell has `WARP_FOCUS_URL=warp://session/<hex>` and
  `WARP_TERMINAL_SESSION_UUID`; `open "$WARP_FOCUS_URL"` focuses that pane.
- `warp://launch/<name>` opens a launch config (`~/.warp/launch_configurations/*.yaml`,
  which supports named `tab_groups`) but always in a new window.
- Creating a group in the current window has no URL. The actions
  `workspace:new_tab_group` and `workspace:new_tab_group_from_active_or_selected_tabs`
  have no default shortcut; bind one and send it by keystroke. New groups get a
  default name.
- Splitting an existing pane has no URL; use Cmd+D by keystroke after focusing it.
- Keystrokes go through `osascript` and System Events, which needs Accessibility for
  Warp (granted 2026-10-04; applies after a Warp restart). Copy the safety guards in
  `reference/hartree/skill--pane/scripts/spawn_agent_panes.py`: wait for modifier keys
  to be released and check Warp is frontmost before every keystroke; abort otherwise.
- Warp shows agent status from OSC 777 `warp://cli-agent` events in the PTY stream,
  whatever the command name (`app/src/terminal/view.rs:13803`), so a byte-transparent
  PTY wrapper should keep the badges. The `warp@claude-code-warp` plugin emits these
  for Claude; it is installed but disabled in `~/.claude/settings.json`.
- `warpctrl` (local control CLI) exists in the source but is off in stable builds and
  cannot run commands or read output. There is no external API for typing into or
  reading a tab.
- Warp saves windows, tabs, groups and panes to
  `~/Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite`
  (`tabs.tab_group_id`, `tab_groups.name`, `terminal_panes.uuid` and `cwd`). Read it
  with `sqlite3 -readonly` to check where a session landed. How soon Warp writes
  changes is unmeasured; measure it before relying on it.

## Reference material

`reference/` is ignored by Git. Re-clone if missing:

- `reference/warp`: `git clone --depth 1 https://github.com/warpdotdev/warp`
- `reference/orca`: `git clone --depth 1 https://github.com/stablyai/orca`. Orca's
  agent-state detection (hooks, title glyphs, `tui-idle` tiers) is in
  `src/shared/agent-hook-listener/`, `src/shared/agent-title-status.ts` and
  `src/main/runtime/tui-idle-evidence.ts`.
- `reference/hartree`: Peter Hartree's HartreeWorks repos (`skill--pane`, `skill--tab`,
  `skill--window`, `claude-scripts` for `notify.py`, `cr`, `skill--chief-of-staff`).

## Testing

- Test with real agents: Claude Code and Codex on their default models (pass no
  `--model`). Aim for realistic tasks over minimal ones: agents that edit files in a
  scratch Git repo, run commands, need permission, receive follow-ups, and fork.
- Write no tautological tests. Every assertion checks an outcome produced by Warp, an
  agent or the operating system: a file the agent wrote, the reply it gave, the group
  in `warp.sqlite`, a process that exited, a screenshot.
- Unit tests are fine for logic with real inputs, such as the PTY wrapper driving a
  real child process or parsing real transcripts copied from a run.
- Tests open tabs and windows in the live Warp. Use a dedicated window
  (`warp://tab_config/<stem>?new_window=true`) and clean up after.
- Screenshots work: `screencapture -x` for the screen, or `screencapture -l <window id>`
  for one Warp window, even when covered. Read them to check the sidebar and badges.
