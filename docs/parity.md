# Parity with the Grok Bot desktop chat

What the desktop app's chat does, and where gbot-tui stands. The backend is fixed: the `gbot` calls in `gbottui/gbot_cli.py` (`bots list`, `thread [--after]`, `send`, `approvals respond`, the opt-in widget hook) and the two Cursor calls in `gbottui/backend.py` (`ListGrokBotAgentStatuses`, `SetGrokBotAgentClientState` mark-read). A Gap is something those calls already allow. Not possible needs a call the repo does not make.

`bench/e2e_tui.py` runs the scenarios named in the last column against `bench/mock_gbot.py`.

| Feature | State | Where / why | e2e |
|---|---|---|---|
| Bot list with status (needs you / unread / working / idle) | Done | `picker.py` `pick`, `activity.py` `Survey`, `with_activity` | 01 |
| Unread counts and order from the server | Done | `backend.refresh_server_state`, `activity.order_bots` | |
| Mark read on leave | Done | `backend.mark_read_later`, `app.leave_chat` | |
| Filter the list | Done | `picker.fuzzy_bots`, `preview_score` | 02 |
| Open a bot, switch bots | Done | `app.screens` (Enter / Esc share one curses session) | 03, 11, 12 |
| Open by name from the shell | Done | `app.main`, `roster` prefix match | 14, 15 |
| `@bot task` route from inside a chat | Done | `roster.parse_route`, `chat_key` "route" | |
| Switch conversations within a bot | Not possible | `gbot thread` reads one transcript per target; no list-of-threads call is used | |
| New conversation | Not possible | no new-thread / reset call in `gbot_cli.py` | |
| Streaming replies | Done | polls at 0.3s; an entry with the same id replaces its row (`Chat._absorb`); `isStreaming` keeps `working` | 04 |
| Working / generating indicator | Done | header `· working`, footer `Generating` (`paint.title_spans`, `grid.footer_spans`); a re-sent unchanged cursor entry is a quiet poll (`Chat._absorb`) | 04, 20, 21 |
| Scrollback (PgUp/PgDn, Alt-Pg half, wheel) | Done | `chat_key`, `terminal.page_motion`, `wheel_direction` | 06 |
| Long output | Done | word wrap with hanging indent, follow-tail (`grid.wrap`, `chat_view.draw`) | 05 |
| Older history than the last 80 entries | Gap | `read_thread(limit=…)` takes a larger limit; load more on scroll to top | |
| Multiline composer (Ctrl-Enter / Ctrl-J newline) | Done | `chat_key` `"\n"`; box grows (`grid.layout`) | 07 |
| Cursor movement within the draft (Left/Right/Up/Down) | Done | `Chat.pos`, `draft.py`, `grid.draft_cursor` / `draft_vertical`, drawn by `chat_view.draw` | 16, 17 |
| Home/End, Ctrl-A/E, word jumps (Alt/Ctrl-Left/Right), Ctrl-W, Ctrl-U, Ctrl-K, Delete | Done | `chat_screen.edit_motion`, `draft.py` | 16 |
| Input history (Up on empty draft recalls last sent) | Done | `Chat.sent_history` / `recall` over the user rows in `chat.messages`, in memory | 18 |
| Arrow keys as CSI (`ESC [ D`) or SS3 (`ESC O D`) | Done | `paint.decode_keys` in `read_keys` and the picker; only a lone Esc goes back | 19 |
| Paste | Done | `paint.read_keys` drains all keys before one redraw; bracketed paste is not parsed, so a pasted newline is CR and sends | |
| Bracketed paste (keep newlines in a paste) | Gap | enable `?2004h` and treat the paste block as text | |
| Approvals (y / n) | Done | `transcript.approval_of`, `Chat.begin_decision`, `gbot_cli.respond_approval` | 08 |
| Widgets / choices (single, multi, steps, custom) | Done | `widgets.py`, `chat_screen.choice_key`; answer is a `send` | 09 |
| Attachments display | Done (text) | `transcript.attachment_text` shows name and size | |
| Images inline | Not possible in a plain curses grid | could print the file name and offer open-in-viewer only if the thread carries a URL | |
| File upload / send attachments | Not possible | `gbot send` takes text only | |
| Markdown: headings, lists, emphasis, code blocks | Done | `markdown.py`; fences drop the ``` and indent the code, the language name sits on its own line | 05 |
| Links (click to open) | Done | `terminal.LINK_HITS`, `open_link_at` | |
| Key help | Partial | one-line footer hints (`grid.footer_spans`, `picker.LIST_HINT`); no `?` overlay | |
| Search in transcript | Gap | local over `grid.visible_rows`; jump `chat.scroll` to the match | |
| Copy a message | Gap | OSC 52 of the selected message text; Shift-drag works in the terminal today | |
| Resize | Done | `KEY_RESIZE`, layout per frame, `clearok` on size change (`chat_view.draw`) | 10 |
| Empty / loading state | Done | picker `loading bots…` / `no match`; chat `Loading conversation…` until the first read when nothing is cached | 15 |
| Error state | Done | red footer, long errors get up to 3 rows (`grid.footer_rows`); composer stays usable | 14 |
| Reconnect | Done (implicit) | a failed read retries on the idle cadence (`Chat.read_failed`); no explicit "reconnecting" label | |
| Explicit reconnecting / offline label | Gap | show the retry countdown in the footer when the last read failed | |
| Theme (light / dark) | Done | `theme.py` OSC 11 probe, `GBOT_TUI_THEME` | |
| Notifications | Partial | Herdr OSC 7501 state and tab name (`herdr.py`); no terminal bell / OSC 9 for a reply in another bot | |
| Bell / OSC 9 notify on needs-you while in the list | Gap | the survey already knows the state change | |
| Settings screen | Not possible | no settings call; theme is env only | |
| Bot avatar and color | Done | `styles.dot_attr`, `icons.py` for Herdr | |
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
