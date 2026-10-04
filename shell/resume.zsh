# warp-agent: when Warp restores a pane that showed an agent session before Warp
# quit, resume that conversation in it. Sourced from ~/.zshrc. Costs one file
# check per shell; Python runs only in a pane warp-agent has used.
if [[ -n ${WARP_TERMINAL_SESSION_UUID-} && -e $HOME/.local/state/warp-agent/panes/$WARP_TERMINAL_SESSION_UUID ]]; then
  # Run just before the first prompt, after Warp has set the shell up.
  _warp_agent_resume() {
    precmd_functions=(${precmd_functions:#_warp_agent_resume})
    local id
    id=$(warp-agent _resumable) && exec warp-agent resume "$id"
  }
  precmd_functions+=(_warp_agent_resume)
fi
