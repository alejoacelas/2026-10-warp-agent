# warp-agent

Open Claude Code and Codex sessions inside chosen Warp tab groups and split panes,
then supervise them from a script or another agent: wait for a session to finish,
read its last reply, send it a follow-up, answer its prompts, focus it, or stop it.

It reproduces the parts of [Orca](https://github.com/stablyai/orca) that matter for
running parallel agents, using only what stock Warp exposes: `warp://` links, tab
config files, guarded keystrokes, and per-launch Claude Code and Codex hooks. The
launch pattern builds on Peter Hartree's
[pane skill](https://github.com/HartreeWorks/skill--pane).

## Usage

```sh
id=$(warp-agent new --group myproject --dir ~/code/app "Fix the failing test and commit")
warp-agent wait "$id"              # DONE / WAITING / EXITED / TIMEOUT, then the last reply
warp-agent send "$id" "Now add a regression test"
warp-agent wait "$id"
warp-agent send "$id" --key 1      # answer a menu, such as a permission prompt
warp-agent read "$id" --log        # recent terminal output
warp-agent focus "$id"
warp-agent stop "$id"              # ends the agent and closes its tab
warp-agent ls
```

Agents run in the background and survive quitting Warp. After reopening Warp,
`warp-agent restore` shows each running session again, in the pane Warp restored or
in a new tab in its group. Closing a session's pane (Cmd+W) ends its agent once
Warp's 60-second undo window passes. `warp-agent detach <id>` leaves an agent
running without a pane; `warp-agent view <id>` shows it in any terminal.
An agent with no pane and no turn running is stopped after 2 hours
(`--idle-hours H` per launch or `WARP_AGENT_IDLE_HOURS`; `0` never). Restarting or
logging out of the Mac ends all agents; their conversations resume with
`claude --resume <id>` or `codex resume <id>`. `warp-agent prune` lists the folders
of sessions that ended over a week ago, and deletes them with `--yes`.

- `--agent codex` runs Codex instead of Claude Code.
- `--window` opens a new Warp window; `--split <id>` opens beside another session.
- `warp-agent panes tasks.json --group G` opens 2–4 agents as panes of one tab.
- `--fork [<id>]` copies the current Claude conversation or another session.
- `--ask-permissions` keeps the agent's approval prompts (the default bypasses them).
- `warp-agent shot <id>` screenshots the session's window and reads its tab groups.

Requirements: macOS, Warp with vertical tabs, Accessibility access for Warp (for
creating groups and splitting panes), `terminal-notifier` for notifications.

The [`supervise-workers`](https://github.com/alejoacelas/dotfiles/tree/main/skills/supervise-workers)
skill uses `warp-agent` for its Warp track.
