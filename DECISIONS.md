# warp-agent decisions

## Core decisions

### Build on stock Warp
- [Use Warp's links and tab configs before keystrokes](#links-before-keystrokes).
- [Place agents in groups by focusing a group member first](#group-placement).
- [Stay off a Warp fork](#no-fork).

### Supervise through the agents, not the terminal
- [Hooks and transcripts are the source of status and replies](#hooks-and-transcripts).
- [Hooks are passed per launch, never installed globally](#per-launch-hooks).
- [A PTY wrapper carries follow-up messages and answers trust prompts](#pty-wrapper).

### Keep both apps working
- [`supervise-workers` keeps an Orca track and adds a Warp track](#two-tracks).

### Test against reality
- [Real agents, realistic tasks, no tautological tests](#real-tests).
- [Check placement with screenshots, not Warp's database](#screenshot-verification).

## Details

### Links before keystrokes
`warp://tab_config/...` and `warp://session/...` need no permissions and cannot type
into the wrong window. Keystrokes are used only where Warp has no link: creating a
group and splitting an existing pane. Typed text is limited to characters that do
not depend on the keyboard layout.

### Group placement
Warp puts a new tab in the active tab's group by default. Focusing any live pane in
the target group, then opening a tab config, lands the agent there.

### No fork
Warp is AGPL and changes fast. Its control CLI deliberately excludes running
commands and reading output, so a fork carrying those would never merge upstream.

### Hooks and transcripts
Claude Code and Codex hooks report status independently of the terminal app.
Transcripts give the exact last reply. Orca relies on the same signal.

### Per-launch hooks
Claude gets hooks through `--settings`; Codex through `-c` overrides with
`--dangerously-bypass-hook-trust`. This leaves the user's dotfiles-managed settings
(which already carry Orca's hooks) untouched and limits the hooks to warp-agent
sessions.

### PTY wrapper
Warp offers no way to type into another tab. Each agent runs inside a wrapper that
relays both the keyboard and a per-session pipe, so supervisors can send follow-ups
and people can still type. The folder-trust prompt appears before any hook runs,
so the wrapper also watches output for it: it accepts in the default (approvals
bypassed) mode and reports `waiting` with `--ask-permissions`.

### Two tracks
The user runs agents in both Orca and Warp; the skill chooses by environment.

### Real tests
Asked for on 2026-10-04: Claude Code and Codex on default models, realistic tasks,
and assertions only on outcomes produced by Warp, agents or the OS. Fixtures come
from real runs with tool output removed, since it can contain private files.

### Screenshot verification
Warp writes `warp.sqlite` only when session restore is on, and the user keeps
`restore_session = false`; Warp's accessibility tree is a single text area. Placement
is therefore checked by reading the vertical tabs sidebar from a screenshot with
macOS text recognition. Changing the user's restore setting to enable database
checks was rejected because it changes Warp's behavior at every restart.

## Decision log

- 2026-10-04: Project started after comparing Orca, Warp's source and Peter Hartree's
  Warp skills.
- 2026-10-04: Built and verified live: per-launch hooks, trust-prompt handling,
  screenshot verification.
