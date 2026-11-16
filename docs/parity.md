# Parity with the Grok Bot desktop chat

What the desktop app's chat does, and where gbot-tui stands. The backend is fixed: the `gbot` calls in `gbottui/gbot_cli.py` (`bots list`, `thread [--after]`, `send`, `approvals respond`, the opt-in widget hook) and the three Cursor calls in `gbottui/backend.py` (`ListGrokBotAgentStatuses`, `SetGrokBotAgentClientState` mark-read, and the read-only `GetGrokBotUserRuntimeSettings` for pinned bots and sidebar sections; nothing is ever written back to it). A Gap is something those calls already allow. Not possible needs a call the repo does not make.

`bench/e2e_tui.py` runs the scenarios named in the last column against `bench/mock_gbot.py`.

| Feature | State | Where / why | e2e |
|---|---|---|---|
| Bot list with status (needs you / unread / working / idle) | Done | `picker.py` `pick`, `activity.py` `Survey`, `with_activity` | 01 |
| Unread counts and order from the server | Done | `backend.refresh_server_state`, `activity.order_bots` | |
| Bot categories (sidebar sections, pinned) | Done (read-only) | `backend.refresh_sections` reads `GetGrokBotUserRuntimeSettings` (empty body; pinned agents are field 6, sidebar sections field 7) in the background beside `refresh_server_state` and caches it in `~/.cache/gbot-tui/sections.json` (`backend.load_sections_cache` at startup). With an empty search `picker.group_bots` draws dim, unselectable headers: `pinned` first, each section by name in the server's order, then `other`; empty sections have no header, a bot in several sections shows in the first. A search is flat, with the section name dimmed at the end of the row when it fits (`picker.section_tags`). No sections known (no token, offline, a failed call) is the flat list. Sections cannot be created, renamed or edited: there is no Update call | 49 |
| Mark read on leave | Done | `backend.mark_read_later`, `Chat.sync_activity` (seen cursor and saved state under the roster id, so the row is `idle` on return) | 38 |
| Mark read from the list (Ctrl-X) | Done | `picker.py` `pick`, `activity.mark_read` (same seen cursor, server mark-read and unread 0 as opening the chat; `needs you` and `working` stay) | 41 |
| Filter the list | Done | `picker.fuzzy_bots`, `preview_score` | 02 |
| Open a bot, switch bots | Done | `app.screens` (Enter / Esc share one curses session) | 03, 11, 12 |
| Open by name from the shell | Done | `app.main`, `roster` prefix match | 14, 15 |
| `@bot task` route from inside a chat | Done | `roster.parse_route`, `chat_key` "route" | |
| `@` mention autocomplete | Done | typing `@` at the start of the draft or after whitespace opens up to 6 suggestions above the composer, from `roster.ROSTER` minus this chat's bot; fuzzy subsequence match, prefix > word start > tighter subsequence > name (`mentions.py`); Up/Down, Tab/Enter insert `@Name `, Esc closes (`chat_screen.mention_key`, `sync_mention`); drawn by `chat_view.paint_mentions`; a bracketed paste does not open it; footer and `?` help say `@ tag a bot` | 37 |
| Switch conversations within a bot | Not possible | `gbot thread` reads one transcript per target; no list-of-threads call is used | |
| New conversation | Not possible | no new-thread / reset call in `gbot_cli.py` | |
| Streaming replies | Done | polls at 0.3s; an entry with the same id replaces its row (`Chat._absorb`); `isStreaming` keeps `working` | 04 |
| Working / generating indicator | Done | header `· working`, footer `Generating` (`paint.title_spans`, `grid.footer_spans`); a re-sent unchanged cursor entry is a quiet poll (`Chat._absorb`); the indicator stays on while the gateway marks the turn live (`activity.entry_in_progress`) up to a 30 minute cap (`WORKING_LIVE_TIMEOUT`), and while any entry, hidden thinking / progress / tool ones included, keeps changing the thread (`activity.content_token`); a last line that is only yours with nothing arriving drops to idle after 120 s; chat header, Herdr state and list use the same rule (`activity.quiet_limit`) | 04, 20, 21 |
| Scrollback (PgUp/PgDn, Alt-Pg half, wheel) | Done | `chat_key`, `terminal.page_motion`, `wheel_direction` | 06 |
| Long output | Done | word wrap with hanging indent, follow-tail (`grid.wrap`, `chat_view.draw`) | 05 |
| Older history than the last 80 entries | Done | scrolling up at the top asks `gbot thread --limit <n+80>` (`Chat.take_older`, `ingest_older`, `chat_screen.older_job`), up to 800; the view keeps its rows; there is no read before the oldest id, so it is a larger tail, not a page | 30 |
| Multiline composer (Ctrl-Enter / Ctrl-J newline) | Done | `chat_key` `"\n"`; box grows (`grid.layout`) | 07 |
| Cursor movement within the draft (Left/Right/Up/Down) | Done | `Chat.pos`, `draft.py`, `grid.draft_cursor` / `draft_vertical`, drawn by `chat_view.draw` | 16, 17 |
| Home/End, Ctrl-A/E, word jumps (Alt/Ctrl-Left/Right), Ctrl-W, Ctrl-U, Ctrl-K, Delete | Done | `chat_screen.edit_motion`, `draft.py` | 16 |
| Input history (Up on empty draft recalls last sent) | Done | `Chat.sent_history` / `recall` over the user rows in `chat.messages`, in memory | 18 |
| Arrow keys as CSI (`ESC [ D`) or SS3 (`ESC O D`) | Done | `paint.decode_keys` in `read_keys` and the picker; only a lone Esc goes back | 19 |
| Paste | Done | `paint.read_keys` drains all keys before one redraw | 22 |
| Bracketed paste (keep newlines in a paste) | Done | the chat sets `?2004h`; `paint.decode_keys` turns Enter inside `ESC[200~`…`ESC[201~` into a draft newline | 22 |
| Approvals (y / n) | Done | `transcript.approval_of`, `Chat.begin_decision`, `gbot_cli.respond_approval` | 08 |
| Widgets / choices (single, multi, steps, custom) | Done | `widgets.py`, `chat_screen.choice_key`; answer is a `send` | 09 |
| Several questions at once: paged card, back/forward, one submit (one widget with `steps`, or widgets in a row) | Done | `widgets.pages`, `chat_screen.page_key`, `Chat.widget_card`; Left/Right or `[` `]`; one `send` with every answer | 39, 40 |
| Secret, 1Password and form request cards | Done (read-only) | `cards.py`: `secret-request` (label, description, `target.name`; `provided` or `waiting: enter it in the Grok Bot app`), `credential-request` (goal, one `website: reason` line per entry; `approved` / `failed` / `expired` once `expiresAtMs` is past / `waiting: approve in 1Password or the Grok Bot app`), `user-form` (title, instruction, field labels and types; `submitted` or `waiting: fill it in the Grok Bot app`). The terminal never takes or sends a secret, password, OTP or form value, and never shows `appLink`, `accessRequestId`, `entryId` or `requestId`. A pending one is `needs you` in the chat header, the list and Herdr (`transcript.needs_user`), and ends when the app settles it or when you send a message after it (`skipped`, gray, no longer `needs you` and no full read per poll; `cards.views`); there is no y/n for it. `transcript.entry_text` has the same plain text for copy and search | 42-46 |
| Attachments display | Done | `transcript.attachment_text` for `user-attachment` entries and `send-message` entries with `message.type` `attachment`: `📎 name`, then alt and `WxH` in brackets when present; an `https` url is appended and clickable, a `file://` url is never shown (the name comes from `file_name`, else the url's last segment) | 47 |
| Images inline | Placeholder only | a plain curses grid cannot draw pixels. Each entry of `message.images` is one dim line after the text, `🖼 alt (WxH)` (file name from the url when alt is missing; `transcript.image_lines`). The `file://` url is never opened, fetched or printed (it is a path on the box). The same line is in previews, copy and search (`transcript.entry_text`) | 47 |
| Canvas pages | Done (card) | a markdown link to `https://cursor.com/canvas/<store>/<id>` keeps its inline link, and one `📄 page: <title>` card per distinct url follows the message (`transcript.pages`, `grid.transcript_rows` row kind `card-page`, `styles.CARD_STYLE`); the card is registered in `terminal.LINK_HITS` by `chat_view.draw`, so a click opens it like any link | 47 |
| File upload / send attachments | Not possible | `gbot send` takes text only | |
| Markdown: headings, lists, emphasis, code blocks | Done | `markdown.py`; fences drop the ``` and indent the code, the language name sits on its own line | 05 |
| Links (click to open) | Done | `terminal.LINK_HITS`, `open_link_at` | |
| Select and copy text with the mouse | Done | F2 in the list and the chat toggles mouse capture (`terminal.toggle_mouse`, one shared `terminal.MOUSE` flag; off is `curses.mousemask(0)`, so the terminal handles drag-select natively); `GBOT_TUI_MOUSE=0` starts off; while off the hint line starts with `select mode · f2 mouse`; Shift-drag selects while it is on in most terminals; both key helps list `f2` and `shift+drag` | 48 |
| Key help | Done | `?` on an empty draft or F1 shows `chat_view.HELP` over the transcript, any key closes it; footer says `? keys`. In the bot list F1, or `?` on an empty search, shows `picker.LIST_HELP` in place of the rows; any key closes it | 23, 28 |
| Search in transcript | Done | Ctrl-F opens a `find:` prompt in the footer (`chat_screen.search_key`); matches over `grid.visible_rows` are reverse video (`grid.search_hits`, `chat_view.paint_matches`); Enter / Ctrl-N older, Ctrl-P newer, wrapping; the view scrolls to the match; Esc closes only the search | 24 |
| Copy a message | Done (last reply) | Ctrl-O writes OSC 52 of the newest bot message (`chat_screen.copy_last_reply`, `terminal.osc52`); footer `Copied the last reply (N chars).` as a green notice (`Chat.notice`, `grid.footer_spans`), cleared after 4s or on the next key; listed in `?` help; Shift-drag still selects | 25, 26 |
| Resize | Done | `KEY_RESIZE`, layout per frame, `clearok` on size change (`chat_view.draw`) | 10 |
| Empty / loading state | Done | picker `loading bots…` / `no match`; chat `Loading conversation…` until the first read when nothing is cached | 15 |
| Error state | Done | red footer, long errors get up to 3 rows (`grid.footer_rows`); composer stays usable | 14 |
| Reconnect | Done | a failed read retries with backoff 2, 4, 8, 15, 30s (`Chat.read_failed`, `chat_state.RETRY_DELAYS`) | 27 |
| Explicit reconnecting / offline label | Done | footer `offline · retrying in Ns` counts down (`Chat.offline_label`, `footer_error`); `reconnected` notice on the next good read (`Chat.read_back`) | 14, 27 |
| Theme (light / dark) | Done | `theme.py` OSC 11 probe, `GBOT_TUI_THEME` | |
| Notifications | Partial | Herdr OSC 7501 state and tab name (`herdr.py`); bell + OSC 9 for needs you (below); no notice for a plain reply in another bot. The Herdr reports are checked end to end against a fake `herdr` in the `herdr` e2e flow: idle on open, working on send, blocked/permission on an approval, idle after the reply, `release-agent` on quit, and OSC 7501 with no CLI call when only `HC_TERMINAL_ID` is set (Desktop) | 31-36 |
| Bell / OSC 9 notify on needs-you while in the list | Done | `picker.pick` writes `terminal.notify` (BEL, `ESC ] 9 ; <name> needs you BEL`) for each name `activity.newly_blocked` returns; once per transition, the first look is the baseline | 29 |
| Settings screen | Not possible | no settings call; theme is env only | |
| Bot avatar and color | Done | `styles.dot_attr`, `icons.py` for Herdr | `colors.bot_color` resolves every list and chat color; no `avatarColor` falls back to `default_color(id)` as in the app and Herdr. The chat composer box and `→` use the same color. |
| Thinking / progress lines hidden | Done | `transcript.is_thinking`, `hidden_status_ids` | |
| Voice input / output | Not possible | no audio path in a terminal or in `gbot` | |
| Edit / delete / regenerate a message | Not possible | no such `gbot` command | |
| Stop generation | Not possible | no cancel call in `gbot_cli.py` | |

## Recommended for session 2, in order

1. Composer cursor: Left/Right, Home/End, Ctrl-A/E, word jumps, Ctrl-W/U, Delete. The biggest everyday gap.
2. Input history on Up/Down with an empty draft (or cursor on the first line).
3. Bracketed paste so a pasted multi-line block stays in the draft instead of sending at the first line.
4. Search in transcript (`/` or Ctrl-F) with next/previous.
5. Load older history when scrolled to the top (larger `--limit`).
6. Key help overlay on `?` with an empty draft.
7. Copy the last bot message (OSC 52).
8. Footer "reconnecting in Ns" after a failed read; bell / OSC 9 when a bot starts needing you.
