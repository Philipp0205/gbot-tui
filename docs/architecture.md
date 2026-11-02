# How gbot-tui works

`gbot-tui` is one Python program, `gbot_tui.py`. It chats with one Grok bot in a curses terminal. It does not talk to Grok itself, and it does not attach to the Grok Bot desktop app. Every list, thread read, and send is a subprocess of the third-party `gbot` CLI (`grok-bot-cli`), which then calls the same HTTPS gateway the desktop app uses.

```
terminal
  curses loop (sleep in select, read every key, draw once, schedule polls)
       |  one worker thread runs gbot, in order
       |
       v
     Chat  -- phase: idle | working | blocked | done
       |
       v
   gbot --gateway --json
       bots list | thread | send
       |
       v
   Cursor gateway on the Grok Bot sandbox ("the box")

side channel, only inside Herdr:
  OSC 7501 + herdr pane report-agent
  tab rename via hc / herdr-app
  avatar icons in ~/.config/herdr-app
```

## Starting a session

`main` accepts a short argument list. It refuses to start a chat unless stdin and stdout are a terminal.

| Invocation | What happens |
|---|---|
| `gbot-tui` | a curses picker, filled from the cached list while `gbot bots list` runs |
| `gbot-tui <name>` | exact name, case-insensitive name, or a unique prefix |
| `gbot-tui --list` | print names and exit |
| `gbot-tui --complete` | names for the zsh completion in `gbot-tui.zsh` |

A typed name is resolved from `~/.cache/gbot-tui/roster.json` first, so the chat opens without waiting on `gbot`, and the list is refreshed on a thread behind it. A name the cache does not have waits for `gbot bots list`. Completion reads `~/.cache/gbot-tui/bots` and treats it as fresh for 10 minutes (`NAMES_TTL`).

`gbot bots list` drops groups. The remaining bots go into an in-memory `ROSTER` and both cache files. A refresh swaps `ROSTER` in place so the UI never sees it empty. Herdr icons are composed on a thread after a list or a first thread read; the AppImage mount behind them can take seconds. When the icon is ready the loop re-reports the pane with the new `app` key.

The picker and the chat share one curses session, so Enter and Esc do not drop back to the shell between screens. Leaving a chat for the list releases the Herdr agent on a thread; quitting releases it before exit.

## Speed

`bench/latency.py` times the TUI in a pseudo-terminal against `bench/mock_gbot.py` (needs `pyte`). Its terminal answers the background probe; `--bg '#FFFFFF'`, `--bg none` (DA1 only), and `--bg mute` change that answer. Each `gbot` call costs a process start plus the gateway, so the work is in not waiting on one:

- The bot list paints from `roster.json`, and a chat paints its last 80 entries from `~/.cache/gbot-tui/threads/<sha1>.json` (mode 0600) while the first `gbot thread` runs. That read replaces the cached rows. Cached cards cannot be answered: y/n are live only after the first read.
- Keys are read without waiting. Every key curses already has is applied before one redraw, so a paste is one frame. No key calls `gbot`.
- The loop sleeps in `select` on the terminal and a pipe the worker writes to, so a finished read draws at once. The sleep is capped at 0.1s, which is how late a resize shows.
- Reads are 0.3s apart while waiting for a reply, 1s while new entries arrive, 2s once they stop (so two quiet polls still take about four seconds), 1s while a card is pending, and 2s backing off to 8s in an idle chat. A key press puts an idle chat back on 2s.

## How a message reaches Grok Bot

`gbot` is [grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli), an MIT CLI by Zack Jackson. It is not part of the Grok Bot app. The bots run on Cursor's sandbox for the account. The official app and `gbot` are both HTTP clients of that sandbox's gateway.

The TUI always invokes `gbot` with `--gateway --json`. `--json` has to sit after the subcommand and before a `--` message, because `gbot` rejects `--json` in front of the subcommand. `--gateway` skips `gbot`'s on-disk agents store and forces the live gateway.

`gbot` then obtains a gateway URL and a bearer token, in this order:

1. `GROK_BOT_GATEWAY_URL` and `GROK_BOT_GATEWAY_TOKEN`, if both are set.
2. The desktop app's saved login, `~/.config/Grok Bot/gateway-descriptor.json`. `gbot` decrypts it with the app's keyring secret (`secret-tool` on Linux) and reads the `baseUrl` and `token` the app stored. The app process does not have to be running. If this file is missing, this path is skipped.
3. `CURSOR_ACCESS_TOKEN`. `gbot` POSTs it to `https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox` and Cursor returns `gatewayUrl` and `gatewayToken`. The usual start for this TUI is this path: the shell exports the token from `~/.config/cursor/auth.json` before launch.

The bootstrap request identifies the caller as the sand client (`x-cursor-client-type: sand`). `gbot` 0.12.4 sends `x-cursor-client-version: 0.20.0` unless `SAND_CLIENT_VERSION` is set, and Cursor rejects 0.20.0 with `EnsureSandBox failed: 401`. The TUI sets `SAND_CLIENT_VERSION` from the installed Grok Bot AppImage filename (the version segment of `Grok_Bot_<version>_…`), falling back to `0.78.1`. It looks for the AppImage at `~/.local/share/grok-bot/appimage`, then the newest `~/Applications/Grok_Bot_*.AppImage`. That read is the file on disk.

After the token is in hand, `gbot` only sends it to `https` hosts on `*.cursor.sh`, `*.cursor.com`, and `*.cursorvm.com`. The three calls this TUI uses become:

| TUI call | `gbot` command | Gateway method |
|---|---|---|
| list bots | `gbot bots list` | `POST {gatewayUrl}/api/listAgents` |
| read the thread | `gbot thread --limit 80 [--after <cursor>] <target>` | `POST {gatewayUrl}/api/getAgentTranscriptTail` |
| send | `gbot send -- <target> <text>` | `POST {gatewayUrl}/api/sendPrompt` with `agentId` and the text |

Authorization on those posts is `Bearer <gatewayToken>`. Local sockets in `gbot` are for Codex and Claude Code on this machine. Grok Bot chat does not use them.

`parse_gbot` turns stdout into JSON and raises `GbotError` on an error object. `parse_thread` keeps `entries`, a cursor (the last entry id when the payload has none), `gapReset`, and the bot's name, id, and avatar fields.

## Chat state

`Chat` holds the target, title, session id, messages, cursor, typed draft, scroll position, and a phase.

The first load replaces the message list and keeps a local line the thread does not have yet. Later polls pass `--after <cursor>` and merge the new entries. An incoming entry whose id is already on screen replaces that row. A local user row is replaced when the text matches. `gapReset` means the cursor is stale, so the whole thread is replaced, and an unacked local line stays at the end.

Sending clears the draft, appends the user's line to the transcript, and marks the phase `working`. One background worker then calls `gbot send`. The curses loop does not wait on it. The next poll replaces that local row when the thread returns the same user text, and keeps the local row when the poll does not have it yet.

`watch_step` moves the phase:

- A pending approval card (`auto-review-approval` or `local-tool-permission` with `status: pending`) forces `blocked`.
- After a send, the phase is `working`.
- Each poll with no new entries increments a quiet counter. Two quiet polls after a non-user message with text count as `done`.
- `done` is reported to Herdr as `idle`. The footer goes back to the idle hint.

The two-poll window is about four seconds, because once a reply stops growing, reads are 2 seconds apart. A bot that pauses longer than that looks finished while it is still working.

## The screen

`curses.wrapper` owns the terminal. Keys stay local:

- printable characters and backspace edit the draft. The draft wraps inside the box, and the box grows until one transcript row remains
- Enter sends. While a card can be answered and the composer is empty, `y` approves and `n` declines through `gbot approvals respond`. A card without both ids does not take those keys. Shift-Y, Shift-N, and ctrl-y / ctrl-n type the letter, so a message can start with y or n. The card switches to Approved or Declined immediately, in green or gray rather than the pending orange, that request id cannot be answered again, and the next read is the whole thread so the gateway status replaces the local one. A failed respond puts the card back to pending and leaves the error beside the y/n hint
- Escape returns to the bot list. The list filters as you type; letters match a name or title in order, and a typed substring wins over a scattered one. Up and down move the highlight. The chat's short key wait is turned off here, or the list crashes with no input
- Page Up / Page Down scroll. Scrolling up turns off follow-tail. Scrolling back to the bottom turns it on
- Ctrl-C or Ctrl-D exits. Herdr starts the program with SIGINT ignored, so startup puts the default interrupt handler back and also accepts Ctrl-C as a key. Both take the same exit as Ctrl-D: the agent report is cleared, the tab gets its old name, and the window title is popped

The layout follows the desktop chat. The header is the bot's name, a color mark for its avatar, and its tagline when the bot list has one. A blank line sits between that header and the first message. Opening by name loads that list first, so the tagline is there too. Your lines are a gray bubble on the right, a step off the terminal background (ANSI 235 on black, 254 on white), with its text color set outright so it reads even if the theme guess is wrong. The bot's lines sit on the left, next to that same mark, in its avatar color, lightened on a dark terminal until it reaches 7:1 contrast and darkened on a light one until it reaches 4.5:1, measured on the 256-color cell actually drawn. The list mark and the chat mark use that same color. On a light terminal the warning, error, green, and gray styles switch to darker indices. Text wraps on words. A wrapped "- " line keeps a hanging indent. A pending approval shows the action's details in the warning color and, when gbot has the card ids, `y` / `n` in the footer. A finished card is green or gray. Under that is a full-width box composer and a one-line footer. The empty composer leaves a space between the arrow and the cursor. The footer says `enter send`, `Generating`, `Pending decision`, or the last `gbot` error. A failed approval shows the error and the y/n hint together. After a reply, two quiet polls mark the turn done and the footer returns to the idle hint.

Light or dark is decided once, before curses starts, from the first of:

1. `GBOT_TUI_THEME=light|dark`. Any other value is ignored, and a set value skips the probe.
2. An OSC 11 query (`ESC ] 11 ; ? ST`) on `/dev/tty`, followed by a DA1 query. Terminals answer in order, so the DA1 reply ends the wait as soon as it arrives. The wait is at most 0.2s, which only a terminal that answers neither query pays. Either `rgb:` (1 to 4 hex digits per channel) or `#RRGGBB` is accepted. The background is light when black text has more contrast on it than white text.
3. `COLORFGBG`, whose background 7 or 15 is light. Herdr does not set it.
4. Dark.

Inside Herdr, a background of exactly `#282C34` is the app's default dark, which a new tab reports for about two seconds before the real color. The first paint uses that sample, and the TUI asks again every half second, up to five times. A real color stops the retries. `GBOT_TUI_THEME` still wins and skips the probe. A resize asks once more; a stale default-dark sample does not throw away a real color already seen.

On exit the TUI emits an OSC clear and, inside Herdr, `herdr pane release-agent`.

## Herdr

The chat works without Herdr. When the phase changes, `publish` reports it two ways:

- **OSC 7501**, written to `/dev/tty`: `state`, `app`, a base64 title, and `kind=permission` when blocked. The window title (OSC 0) is set to the same label. Herdr's agent list reads that sequence.
- **`herdr pane report-agent`**, only when `HERDR_ENV=1` and `HERDR_BIN_PATH` / `HERDR_PANE_ID` are set. The state, bot name, session id, and a resume command (`gbot-tui <name>`) go with it. A millisecond timestamp is the sequence number so a late report does not overwrite a newer one.

If this process is the only live terminal in its Herdr tab, the tab is renamed to the bot. The check walks `/proc` for ancestor pids, then asks `hc ls` / `hc get` which terminal owns this process. Before the first rename, `herdr-app ls` gives the tab's own name. A tab Herdr shows by number counts as unnamed. Esc back to the list and quitting both rename the tab back to that name, and an empty name gives Herdr's number back. One worker thread (`TabTitle`) runs those renames in order, so a restore never lands before the rename it undoes. Quitting, Ctrl-C, and SIGTERM/SIGHUP all wait up to 10s for that restore, clear the OSC report, and pop the window title (`CSI 22;0t` at start, `CSI 23;0t` once on exit). The bot list sets the title to `gbot`. A tab Herdr still shows by its id (`tb12`) displays the program name, so the clear matters as much as the rename.

Avatars are optional and only for Herdr's program icons. After `bots list`, a background thread mounts the Grok Bot AppImage with `squashfuse`, pulls `bot-*.png` masks and faces out of `app.asar`, and writes `~/.config/herdr-app/icons/gbot/` plus `programs.json`. The face file is 29 frames of 18px. Frame 0 is the resting pose. Its RGB is black and is not drawn; the icon uses the bot's color and that frame's alpha, so the body is filled and the eyes stay holes. Herdr's program icon is one static path, so the strip is not animated. A png already on disk is replaced when the bytes differ. The OSC `app` field is the bot's own name, clipped to 32 bytes, which is what Herdr puts in a popup (`Cloud needs you`). The icon key stays `program_key` (at most 32 bytes). `programs.json` matches both that name and the key, and may also keep the longer `gbot-<id>` key. Both point at the same icon. The `name` in that file is the bot's name too. `herdr pane report-agent` passes the same name as `--agent`. A missing AppImage, `squashfuse`, or Pillow is ignored. The chat still runs.
