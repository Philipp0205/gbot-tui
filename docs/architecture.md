# How gbot-tui works

`gbot-tui` is one Python program. `gbot_tui.py` starts it and the code lives in the `gbottui` package beside that file. It chats with one Grok bot in a curses terminal. It does not talk to Grok itself, and it does not attach to the Grok Bot desktop app. Every list, thread read, and send is a subprocess of the third-party `gbot` CLI (`grok-bot-cli`), which then calls the same HTTPS gateway the desktop app uses.

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

## Where the code lives

Each module in `gbottui/` does one job. Modules import each other as `from . import herdr` and read `herdr.APP`, `tabs.TAB`, `roster.ROSTER`, or `activity.ACTIVITY` through the module, because those values are rebound at run time and swapped in tests. Run it from this directory, or symlink `gbot_tui.py`: the link resolves to the package next to it. Copying `gbot_tui.py` alone does not run.

| Module | Job |
|---|---|
| `app.py` | arguments, the curses session that holds the list and the chat, exit and signals |
| `picker.py` | the bot list: rows, search, wheel and keys |
| `chat_screen.py` | the chat screen: keys, the event loop, background jobs |
| `chat_state.py` | one chat's state: transcript, draft, phase, pending decisions |
| `chat_view.py` | drawing one chat |
| `activity.py` | per-bot state (needs you, unread, working, idle), seen cursors, ordering |
| `backend.py` | Cursor backend calls: login token, per-bot unread state, mark read |
| `gbot_cli.py` | every call to the `gbot` CLI: reads, sends, approvals, widgets |
| `transcript.py` | reading thread entries: text, cards, approvals, hiding thinking and progress lines |
| `widgets.py` | the bot's answer widgets: options, steps, digit keys |
| `markdown.py` | Markdown rendering into styled rows |
| `grid.py` | wrapping, rows and the screen layout |
| `paint.py`, `styles.py`, `colors.py`, `theme.py` | painting, style names, colors and contrast, light or dark |
| `roster.py`, `cache.py` | the bot roster, `@bot` routing, files under `~/.cache/gbot-tui` |
| `herdr.py`, `tabs.py`, `icons.py` | Herdr reports, tab names, avatar icons |
| `terminal.py` | terminal modes, paging and the mouse wheel |

## Starting a session

`main` accepts a short argument list. It refuses to start a chat unless stdin and stdout are a terminal.

| Invocation | What happens |
|---|---|
| `gbot-tui` | a curses picker, filled from the cached list while `gbot bots list` runs |
| `gbot-tui <name>` | exact name, case-insensitive name, or a unique prefix |
| `gbot-tui --list` | print names and exit |
| `gbot-tui --overview` | print each bot's status and latest line, then exit |
| `gbot-tui --complete` | names for the zsh completion in `gbot-tui.zsh` |

A typed name is resolved from `~/.cache/gbot-tui/roster.json` first, so the chat opens without waiting on `gbot`, and the list is refreshed on a thread behind it. A name the cache does not have waits for `gbot bots list`. Completion reads `~/.cache/gbot-tui/bots` and treats it as fresh for 10 minutes (`NAMES_TTL`).

`gbot bots list` drops groups. The remaining bots go into an in-memory `ROSTER` and both cache files. A refresh swaps `ROSTER` in place so the UI never sees it empty. Herdr icons are composed on a thread after a list or a first thread read; the AppImage mount behind them can take seconds. When the icon is ready the loop re-reports the pane with the new `app` key.

The picker and the chat share one curses session, so Enter and Esc do not drop back to the shell between screens. Leaving a chat for the list releases the Herdr agent on a thread and reports the list as `gbot-tui`, because Herdr keeps the last app name until the next report. Quitting releases the agent before exit.

## Speed and end-to-end runs

`bench/latency.py` times the TUI in a pseudo-terminal against `bench/mock_gbot.py` (needs `pyte`). Its terminal answers the background probe; `--bg '#FFFFFF'`, `--bg none` (DA1 only), and `--bg mute` change that answer. Each `gbot` call costs a process start plus the gateway, so the work is in not waiting on one:

- The bot list paints from `roster.json`, and a chat paints its last 80 entries from `~/.cache/gbot-tui/threads/<sha1>.json` (mode 0600) while the first `gbot thread` runs. That read replaces the cached rows. Cached cards cannot be answered: y/n are live only after the first read. The overview pass uses that same thread read, one bot at a time, and shares the single `gbot` lock with send and the open chat so a scan cannot pass a send. Status from the last pass is cached in `activity.json` (mode 0600) and shown until the next read returns. A thread saved from the last time that chat was open is judged the same way, so the list can show running, unread, or idle before the pass reaches that bot.
- Keys are read without waiting. Every key curses already has is applied before one redraw, so a paste is one frame. No key calls `gbot`. An Esc that curses passes through is decoded by `paint.decode_keys`: a terminal not in keypad mode, or a multiplexer, sends arrows as CSI (`ESC [ D`) where terminfo says SS3 (`ESC O D`). `ESC [ A/B/C/D/H/F`, `ESC [1;<mod>X` (mapped to terminfo's `kLFT5` and the like), `ESC O X`, and `ESC [n~` become those keys; an unknown sequence or Alt-letter is dropped. Only an Esc with no byte behind it inside the escape delay (25ms) goes back to the list. The picker uses the same decoder.
- The loop sleeps in `select` on the terminal and a pipe the worker writes to, so a finished read draws at once. The sleep is capped at 0.1s, which is how late a resize shows.
- `bench/e2e_tui.py` drives the whole app in a pseudo-terminal (pyte) against the same mock: picker, filter, open, a streamed reply, long Markdown, PgUp/PgDn, Ctrl-J newlines, an approval, a widget, resize with SIGWINCH, Esc, switching bots, Ctrl-D, `MOCK_FAIL`, and a hanging first read (`MOCK_HANG`), and composer editing with the cursor (16, 17), input history (18), CSI and SS3 arrows in the chat and the list (19). Each run uses a temp HOME and MOCK_STATE, a PATH with only the mock `gbot`, no Cursor or gateway variables, and `GBOT_TUI_THEME=dark`. Each scenario's screen is written to `$GBOT_E2E_ARTIFACTS/<scenario>.txt` (default `/workspace/oc/gbot-tui-artifacts/tui-polish`); the exit code counts failures. `docs/parity.md` maps the desktop chat's features to this TUI.
- A chat opened with no cached thread shows `Loading conversation…` until the first read lands or fails.
- Reads are 0.3s apart while waiting for a reply, 1s while new entries arrive, 2s once they stop (so two quiet polls still take about four seconds), 1s while a card is pending, and 2s backing off to 8s in an idle chat. A key press puts an idle chat back on 2s.

`gbot` runs with the app's live login when `CURSOR_ACCESS_TOKEN` in the shell has expired: the expired token is dropped from its environment before it starts.

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

A widget answer is the send row: the option value is the prompt text. With `GBOT_TUI_WIDGET_HOOK=1`, the TUI first tries `POST {gatewayUrl}/api/respondGrokBotWidget` with `{agentId, entryId, value}` and `Content-Type: application/json`, using the agent id from the thread target. That call is not used unless the flag is set, and a failed or non-JSON response still sends the value.

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
- A turn that stays `working` with no new text for 120 seconds falls back to `idle`, in the header and the footer. That covers a last line that is still the user's, and a message whose `isStreaming` flag never clears. New text starts the 120 seconds over and shows `working` again. The overview uses the same limit: it remembers when that text first looked in progress, and the next scan calls it `idle` once 120 seconds have passed with nothing new.

The two-poll window is about four seconds, because once a reply stops growing, reads are 2 seconds apart. A bot that pauses longer than that looks finished while it is still working. The 120 second limit is the backstop for a turn the two quiet polls no longer end.

## The screen

`curses.wrapper` owns the terminal. Keys stay local:

- printable characters edit the draft at the cursor (`gbottui/draft.py`). The draft wraps inside the box, and the box grows until one transcript row remains; the cursor is drawn where it is, also on a wrapped row, and the box shows the rows around it. Left/Right move a character, Home/End and Ctrl-A/Ctrl-E go to the start and end of the typed line, Ctrl-Left/Ctrl-Right and Alt-Left/Alt-Right jump a word, Up/Down move between rows of a multi-line draft. Backspace and Delete remove the character before or under the cursor, Ctrl-W the word before it, Ctrl-U to the line start, Ctrl-K to the line end (at an edge they join the lines)
- Up on an empty draft, or on its first row, recalls the messages you sent in this chat, newest first; Down goes forward and past the newest gives back the draft you had. The history is read from the transcript (`Chat.sent_history`), in memory only, so editing a recalled line does not change it
- Enter sends. While a card can be answered and the composer is empty, `y` approves and `n` declines through `gbot approvals respond`. A card without both ids does not take those keys. Shift-Y, Shift-N, and ctrl-y / ctrl-n type the letter, so a message can start with y or n. The card switches to Approved or Declined immediately, in green or gray rather than the pending orange, that request id cannot be answered again, and the next read is the whole thread so the gateway status replaces the local one. A failed respond puts the card back to pending and leaves the error beside the y/n hint. A widget message is the bot's choices: the prompt and a numbered option. With the composer empty, that number answers the newest open one, and only when no user message follows it. A widget you already replied to does not take digits. Options 1–9 use those digits. A letter never answers: a message can start with `a`. With more than 9 options the keys are two digits inside about a second (`10` for the tenth, `02` for the second). A lone digit selects that option once the moment passes; `0` alone does nothing. Type a space first, then a digit, to put the digit in the message; the space is not part of what gets sent. A widget you already replied to is drawn without numbers, the same as an older or answered card. The footer names enter once. A multi-select toggles, and Enter sends the chosen lines joined by newlines. A card with `steps` is several questions: the current one is numbered, earlier answers stay marked, and Enter or a number moves to the next. The sent text is one answer per step with a blank line between them. A step with no options and no `allowCustom` says `answer in the app`. When the open widget has `allowCustom` (or the current step does), text in the composer is that answer. The chosen value is marked on the card immediately and sent as a normal message with `gbot send`, because a widget answer arrives as the user's reply. A failed send puts the options back. `GBOT_TUI_WIDGET_HOOK=1` tries `respondGrokBotWidget` first, on the gateway session a one-entry `gbot thread` just opened. That post sets `Content-Type: application/json`. `agentId` is the thread's agent id (`target.agentId`, else `target.id` when that id is not `target.sessionId`). A session id is not sent in that field. A 2xx body that is not JSON is not success. If the hook fails, the same value is sent as the message
- Ctrl-Enter (a bare line feed) adds a new line to the draft. `@bot task` sends the task to that bot and stays in this chat. A bot name with spaces is written with hyphens: `@Cursor-Cloud-Session`. An unknown name shows the error and sends nothing
- Escape returns to the bot list. The list is the overview: a background pass reads each thread and labels it `needs you` (a pending approval, or an open widget after your last message, with the prompt as the preview), `unread` (a reply since you last had that chat on screen), `working` (the last line is yours, or the latest entry is still in progress), or `idle`. Rows sort by the server's last-activity time, newest first (see below). `needs you` is yellow. `working` is cyan, including the `working` mark in the chat header, so the two stay apart. `gbot-tui --overview` still prints `running` for that state, so a script that reads the old word keeps working. The on-screen list says `working`. A `working` row whose text has not changed for 120 seconds goes back to `idle` on the next scan. The line under the title counts them (`1 needs you · 2 unread · 1 working`). Each row adds that word and a one-line preview of the pending action or the latest message. What you have already seen is the cursor in `~/.cache/gbot-tui/seen.json`. The first scan writes that cursor once and later scans leave it there, so a reply that arrives afterward stays `unread` until the chat is opened. Opening the chat advances the cursor. A cached thread counts when that file has no row. A bot you have never opened is not marked `unread` on the first look. The same pass repeats 8 seconds after it finishes. Ctrl-R scans again. Name and status are padded so the previews share a column; a narrow window clips the name before it drops the grid. The list filters as you type. A name or a title matches when the letters appear in order, and a typed substring wins over a scattered one. A preview matches only when those letters sit together. A search keeps match order. Until you press Up or Down the highlight stays on the top row. The chat's short key wait is turned off here, or the list crashes with no input
- The list order and the `unread` count come from the server, as in the desktop app. Each scan asks `GrokBotService/ListGrokBotAgentStatuses` for every bot's unread count and last activity time, sorts by that time, newest first, and shows that count. Leaving a chat sends `SetGrokBotAgentClientState` with `mark_read`, so the desktop app clears it too. When the call fails, the list falls back to its own reads
- Page Up / Page Down scroll a screen. Alt-Page Up / Alt-Page Down scroll half of that, the same split Herdr uses in a normal pane. Scrolling up turns off follow-tail. Scrolling back to the bottom turns it on
- The mouse wheel scrolls 3 lines in a chat and moves the highlight in the bot list. Reporting the wheel turns off plain click-drag selection in the terminal; hold Shift to select text
- Ctrl-C or Ctrl-D exits. Herdr starts the program with SIGINT ignored, so startup puts the default interrupt handler back and also accepts Ctrl-C as a key. Both take the same exit as Ctrl-D: the agent report is cleared, the tab gets its old name, and the window title is popped

The layout follows the desktop chat. The header is the bot's name, a color mark for its avatar, `working` in cyan while a reply is in progress, and its tagline when the bot list has one. Opening a thread whose last line is yours, or whose latest message is still streaming (`isStreaming`), starts in that same state, and a streaming message does not count as finished. A blank line sits between that header and the first message. Opening by name loads that list first, so the tagline is there too. Your lines are a gray bubble on the right, a step off the terminal background (ANSI 235 on black, 254 on white), with its text color set outright so it reads even if the theme guess is wrong. The bot's lines sit on the left, next to that same mark, in its avatar color, lightened on a dark terminal until it reaches 7:1 contrast and darkened on a light one until it reaches 4.5:1, measured on the 256-color cell actually drawn. Black is `#000000`, so on a dark terminal it is lifted with the other colors until the dot reads. The list mark and the chat mark use that same color. On a light terminal the warning, error, green, and gray styles switch to darker indices. Text wraps on words. A wrapped "- " line keeps a hanging indent. A pending approval shows the action's details in the warning color and, when gbot has the card ids, `y` / `n` in the footer. A widget shows its prompt and numbered options in the bot's color, and the footer names the keys while one is still open, including `space then digit types` for the digit escape. A stale widget, one with a user message after it, is not numbered. Inserting your reply clears the physical screen before the next paint, so a shifted frame does not stay behind the new lines. A `steps` card shows every question, with the current one numbered. A one-paragraph progress line ("Checking the agent now.", "Ich schau mir EXE-3318 kurz an.") is left out once any later bot line is on screen. A line with a link or more than one paragraph stays, and so does the progress line while it is still the latest bot line. A finished card is green or gray. Under that is a full-width box composer and a one-line footer. The empty composer leaves a space between the arrow and the cursor. The footer says `enter send`, `Generating`, `Pending decision`, or the last `gbot` error. A failed approval shows the error and the y/n hint together. After a reply, two quiet polls mark the turn done and the footer returns to the idle hint.

Light or dark is decided once, before curses starts, from the first of:

1. `GBOT_TUI_THEME=light|dark`. Any other value is ignored, and a set value skips the probe.
2. An OSC 11 query (`ESC ] 11 ; ? ST`) on `/dev/tty`, followed by a DA1 query. Terminals answer in order, so the DA1 reply ends the wait as soon as it arrives. The wait is at most 0.2s, which only a terminal that answers neither query pays. Either `rgb:` (1 to 4 hex digits per channel) or `#RRGGBB` is accepted. The background is light when black text has more contrast on it than white text.
3. `COLORFGBG`, whose background 7 or 15 is light. Herdr does not set it.
4. Dark.

A background of exactly `#282C34` is Herdr's default dark, which a new tab reports for about two seconds before the real color. The first paint uses that sample wherever it shows up, and the TUI asks again every half second, up to five times (a 2.5 s budget). A real color stops the retries. Any other first sample is final. `GBOT_TUI_THEME` still wins and skips the probe. Inside Herdr, a resize asks once more; a stale default-dark sample does not throw away a real color already seen. Herdr Desktop 0.1.11 does not set `HERDR_ENV` or `HERDR_PANE_ID`. A session is Herdr when either of those is set, or when `HC_TERMINAL_ID`, `HC_SCOPE`, or `HC_TERMINAL_SOCKET` is set.

On exit the TUI emits an OSC clear and, inside Herdr, `herdr pane release-agent`.

## Herdr

The chat works without Herdr. When the phase changes, `publish` reports it two ways:

- **OSC 7501**, written to `/dev/tty`: `state`, `app`, a base64 title, and `kind=permission` when blocked. The window title (OSC 0) is set to the same label. Herdr's agent list reads that sequence.
- **`herdr pane report-agent`**, when this is a Herdr session and `HERDR_BIN_PATH` / `HERDR_PANE_ID` are set. `HERDR_ENV` is not required on its own. Desktop panes usually have neither path nor pane id; OSC 7501 is what they read. The state, bot name, session id, and a resume command (`gbot-tui <name>`) go with the CLI report. A millisecond timestamp is the sequence number so a late report does not overwrite a newer one.

If this process is the only live terminal in its Herdr tab, the tab is renamed to the bot. The check walks `/proc` for ancestor pids, then asks `hc ls` / `hc get` which terminal owns this process. Before the first rename, `herdr-app ls` gives the tab's own name. A tab Herdr shows by number counts as unnamed. Esc back to the list and quitting both rename the tab back to that name, and an empty name gives Herdr's number back. One worker thread (`TabTitle`) runs those renames in order, so a restore never lands before the rename it undoes. Quitting, Ctrl-C, and SIGTERM/SIGHUP all wait up to 10s for that restore, clear the OSC report, and pop the window title (`CSI 22;0t` at start, `CSI 23;0t` once on exit). The bot list sets the title to `gbot`. A tab Herdr still shows by its id (`tb12`) displays the program name, so the clear matters as much as the rename.

Avatars are optional and only for Herdr's program icons. After `bots list`, a background thread mounts the Grok Bot AppImage with `squashfuse`, pulls `bot-*.png` masks and faces out of `app.asar`, and writes `~/.config/herdr-app/icons/gbot/` plus `programs.json`. The face file is 29 frames of 18px. Frame 0 is the resting pose. Its RGB is black and is not drawn; the icon uses the bot's color and that frame's alpha, so the body is filled and the eyes stay holes. A bot that never stored a shape or color still gets that filled icon: both are derived from its agent id the way the desktop app does (one of blob, pebble, squircle, tablet, wedge, hex, cloud, teardrop, and any palette color except black). That matches the app when `gbot bots list` returns the agent id as `id`. Herdr's program icon is one static path, so the strip is not animated. A png already on disk is replaced when the bytes differ. The OSC `app` field is the bot's name with whitespace turned into hyphens, clipped to 32 bytes. Herdr drops an app that contains a space, a non-breaking space, or an en space, and the popup then says `An agent needs you` with no icon. `Cursor Cloud Session` is reported as `Cursor-Cloud-Session`, which is what the popup reads. A single word such as `Cloud` or `Chef` is unchanged. The icon key stays `program_key` (at most 32 bytes). `programs.json` matches both that hyphenated name and the key, and may also keep the longer `gbot-<id>` key. Both point at the same icon. The `name` in that file stays the bot's name, spaces included. `herdr pane report-agent` passes the same hyphenated name as `--agent`. Tab rename does not look at `HERDR_ENV`; it finds the terminal with `hc`, which Desktop provides. A missing AppImage, `squashfuse`, or Pillow is ignored. The chat still runs.
