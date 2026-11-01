# How gbot-tui works

`gbot-tui` is one Python program, `gbot_tui.py`. It chats with one Grok bot in a curses terminal. It does not talk to Grok itself, and it does not attach to the Grok Bot desktop app. Every list, thread read, and send is a subprocess of the third-party `gbot` CLI (`grok-bot-cli`), which then calls the same HTTPS gateway the desktop app uses.

```
terminal
  curses loop (poll every 2s, draw, read keys)
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
| `gbot-tui` | `gbot bots list`, then a curses picker |
| `gbot-tui <name>` | exact name, case-insensitive name, or a unique prefix |
| `gbot-tui --list` | print names and exit |
| `gbot-tui --complete` | names for the zsh completion in `gbot-tui.zsh` |

A typed name is resolved from `~/.cache/gbot-tui/bots` first. That file is rewritten whenever the bot list is fetched. Completion treats it as fresh for 10 minutes (`NAMES_TTL`). If the cached name is wrong, the TUI lists bots again and retries.

`gbot bots list` drops groups. The remaining bots go into an in-memory `ROSTER` and into the cache file.

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

The first load replaces the message list. Later polls pass `--after <cursor>` and append only new entries. `gapReset` means the cursor is stale, so the whole thread is replaced.

Sending clears the draft, marks the phase `working`, calls `gbot send`, then polls once so the new user line shows up immediately.

`watch_step` moves the phase:

- A pending approval card (`auto-review-approval` or `local-tool-permission` with `status: pending`) forces `blocked`.
- After a send, the phase is `working`.
- Each poll with no new entries increments a quiet counter. Two quiet polls after a non-user message with text count as `done`.
- `done` is reported to Herdr as `idle`. The footer goes back to the idle hint.

The two-poll window is about four seconds, because the curses loop waits up to 2 seconds for a key and a timeout is a poll. A bot that pauses longer than that looks finished while it is still working.

## The screen

`curses.wrapper` owns the terminal. Keys stay local:

- printable characters and backspace edit the draft. The draft wraps inside the box, and the box grows until one transcript row remains
- Enter sends
- Escape returns to the bot list. The list filters as you type; letters match a name or title in order, and a typed substring wins over a scattered one. Up and down move the highlight
- Page Up / Page Down scroll. Scrolling up turns off follow-tail. Scrolling back to the bottom turns it on
- Ctrl-C or Ctrl-D exits

The layout is a header (bot name), a transcript, a box composer, and a one-line footer. User lines are padded into a gray bubble (ANSI 235 on a dark terminal, 254 on a light one, from `COLORFGBG`). Other messages are plain wrapped text. The footer says `enter send`, `Generating`, `Pending decision`, or the last `gbot` error.

On exit the TUI emits an OSC clear and, inside Herdr, `herdr pane release-agent`.

## Herdr

The chat works without Herdr. When the phase changes, `publish` reports it two ways:

- **OSC 7501**, written to `/dev/tty`: `state`, `app`, a base64 title, and `kind=permission` when blocked. The window title (OSC 0) is set to the same label. Herdr's agent list reads that sequence.
- **`herdr pane report-agent`**, only when `HERDR_ENV=1` and `HERDR_BIN_PATH` / `HERDR_PANE_ID` are set. The state, bot name, session id, and a resume command (`gbot-tui <name>`) go with it. A millisecond timestamp is the sequence number so a late report does not overwrite a newer one.

If this process is the only live terminal in its Herdr tab, the tab is renamed to the bot. The check walks `/proc` for ancestor pids, then asks `hc ls` / `hc get` which terminal owns this process.

Avatars are optional and only for Herdr's program icons. On `bots list`, the TUI mounts the Grok Bot AppImage with `squashfuse`, pulls `bot-*.png` masks and faces out of `app.asar`, and writes `~/.config/herdr-app/icons/gbot/` plus a `programs.json` entry. The face file is a strip of 18px frames whose alpha is the body (eyes are holes, RGB is black). The icon is the resting frame tinted with the bot's color. A png already on disk is replaced when the bytes differ. The OSC `app` field becomes `gbot-<bot id>`, which is the key Herdr matches. A missing AppImage, `squashfuse`, or Pillow is ignored. The chat still runs.
