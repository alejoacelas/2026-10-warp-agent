# warp-agent decisions

## Core decisions

### Build on stock Warp
- [Use Warp's links and tab configs before keystrokes](#links-before-keystrokes).
- [Place agents in groups by focusing a group member first](#group-placement).
- [Stay off a Warp fork](#no-fork).

### Supervise through the agents, not the terminal
- [Hooks and transcripts are the source of status and replies](#hooks-and-transcripts).
- [A PTY wrapper carries follow-up messages](#pty-wrapper).

### Keep both apps working
- [`supervise-workers` keeps an Orca track and adds a Warp track](#two-tracks).

### Test against reality
- [Real agents, realistic tasks, no tautological tests](#real-tests).

## Details

### Links before keystrokes
`warp://tab_config/...` and `warp://session/...` need no permissions and cannot type
into the wrong window. Keystrokes are used only where Warp has no link: creating a
group in the current window and splitting an existing pane.

### Group placement
Warp puts a new tab in the active tab's group by default. Focusing any pane in the
target group and then opening a tab config lands the agent there.

### No fork
Warp is AGPL and changes fast. Its control CLI deliberately excludes running
commands and reading output, so a fork carrying those would never merge upstream.

### Hooks and transcripts
Claude Code and Codex hooks report status independently of the terminal app.
Transcripts give the exact last reply. This is how Orca gets its most reliable
signal too.

### PTY wrapper
Warp offers no way to type into another tab. Each agent runs inside a small wrapper
that relays both the keyboard and a per-session pipe, so supervisors can send
follow-ups and people can still type into the session.

### Two tracks
The user runs agents in both Orca and Warp; the skill chooses by environment.

### Real tests
Asked for on 2026-10-04: use Claude Code and Codex on default models, realistic
tasks, and assertions only on outcomes produced by Warp, agents or the OS.

## Decision log

- 2026-10-04: Project started after comparing Orca, Warp's source and Peter Hartree's
  Warp skills.
