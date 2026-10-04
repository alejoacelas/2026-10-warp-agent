# warp-agent

A command-line tool for opening Claude Code and Codex sessions inside chosen Warp tab
groups and split panes, and for supervising them: wait for a session to finish, read
its last reply, send it a follow-up, focus it, or stop it.

It reproduces the parts of [Orca](https://github.com/stablyai/orca) that matter for
running parallel agents, using only what stock Warp exposes: `warp://` links, tab
config files, guarded keystrokes, and Claude Code and Codex hooks. It borrows its
launch pattern from Peter Hartree's
[pane skill](https://github.com/HartreeWorks/skill--pane).

Status: design done, implementation not started. See `AGENTS.md` for the feature
list and how Warp is driven.
