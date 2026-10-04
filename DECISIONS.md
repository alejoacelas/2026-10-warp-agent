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
- [Agents run under a background server and survive quitting Warp](#background-server).
- [Closing a pane ends its agent; quitting Warp does not](#close-versus-quit).
- [Agents left with no pane are stopped after 2 idle hours](#idle-cleanup).

### Keep both apps working
- [`supervise-workers` keeps an Orca track and adds a Warp track](#two-tracks).

### Test against reality
- [Real agents, realistic tasks, no tautological tests](#real-tests).
- [Check placement with screenshots](#screenshot-verification).

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
Claude Code and Codex hooks report status independently of the terminal app;
Orca relies on the same signal. The reply for a finished turn comes from the Stop
event's `last_assistant_message`, not the transcript: Claude can run its Stop hook
before writing the final message to the transcript, which made `wait` print the
previous turn's reply in a live run. Transcripts are the fallback.

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

### Background server
Asked for on 2026-10-04 so agents survive quitting Warp. The server owns the agent's
terminal; panes run viewers. It waits for the first viewer before starting the
agent, so the agent's startup queries to the terminal get answers, and replays the
terminal modes the agent switched on (alternate screen, bracketed paste, mouse,
keyboard protocol) to each new viewer. Without the replay a re-attached pane shows
a garbled screen and misreads keys.

### Close versus quit
Closing a pane is how the user ends a finished session, so it must stop the agent,
or idle agents pile up at 200–500 MB each. Quitting Warp must not. Both end the
viewer, and Warp does not reliably hang it up, so the viewer reports its Warp
process when it connects and the server checks, 5 s after any viewer loss it did
not cause, whether that Warp is still running. `detach` is the deliberate way to
leave an agent running without a pane.

### Idle cleanup
Asked for on 2026-10-04 as a strict default: background agents otherwise run until
the Mac restarts. The server stops an agent after 2 hours with no viewer and no
turn running (hook state not `working`); a long turn finishes first. Agents shown in
a pane are never stopped this way, since closing the pane already ends them.

### Two tracks
The user runs agents in both Orca and Warp; the skill chooses by environment.

### Real tests
Asked for on 2026-10-04: Claude Code and Codex on default models, realistic tasks,
and assertions only on outcomes produced by Warp, agents or the OS. Fixtures come
from real runs with tool output removed, since it can contain private files.

### Screenshot verification
Warp's accessibility tree is a single text area, so placement is checked by reading
the vertical tabs sidebar from a screenshot with macOS text recognition. Warp's
`warp.sqlite` is written only with session restore on, which the user turned on
on 2026-10-04 to get tabs back after restarts; database checks could now
complement screenshots but are not built.

## Decision log

- 2026-10-04: Project started after comparing Orca, Warp's source and Peter Hartree's
  Warp skills.
- 2026-10-04: Built and verified live: per-launch hooks, trust-prompt handling,
  screenshot verification.
- 2026-10-04: Background server so agents survive quitting Warp; closing a pane ends
  its agent after Warp's 60 s undo window plus 5 s.
