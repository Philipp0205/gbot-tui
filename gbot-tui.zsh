#compdef gbot-tui

_gbot-tui() {
  local -a names
  names=("${(@f)$(command gbot-tui --complete 2>/dev/null)}")
  (( ${#names} )) || return
  _describe -t bots bot names
}

if (( $+functions[compdef] )); then
  compdef _gbot-tui gbot-tui
fi
