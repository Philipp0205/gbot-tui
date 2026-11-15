# gbot-tui

Philipp's personal terminal UI for talking to Grok Bot. Python script + tests.

## Stack
- `gbot_tui.py` — entry point; the code is in `gbottui/` (see `docs/architecture.md`)
- `test_gbot_tui.py` — `unittest` tests
- `gbot-tui.zsh` — shell helper
- `docs/`, `bench/`

## Commands
- `python -m unittest test_gbot_tui.py`

## Hard rules
- Personal tool only. Never touch Aney or Advantest repos.
- Keep changes small. Do not commit or push unless asked.
- Never print or paste secrets or tokens.
- Run the unittest suite above before claiming done.

## Tools

- View files with the read tool (use offset/limit for long files like `test_gbot_tui.py`), search with the grep tool, find files with the glob tool. Do not use `cat`, `sed -n`, `head`, `tail`, `grep`, `awk`, `wc -l` or `ls -R` in bash to read or search files, also not for several files at once. Trimming a command's own output (`python -m unittest test_gbot_tui.py 2>&1 | tail -20`) is fine.
- Bash is for running commands (the unittest suite, `bench/e2e_tui.py`, git status). Do not run `python3 -c` or `python3 - <<EOF` to poke at code; write a test or read the source instead. Do not probe the permission rules with dummy commands.
- Create new files with the write tool. Never `cat > file`, heredocs or `echo >` into repo files, also not behind `cd ... &&`.
