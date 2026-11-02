---
name: gbot-tui
description: Specialist for the gbot-tui curses chat and its Herdr integration. Use proactively for a slow Enter-to-message delay, Herdr program icons that look like lines instead of a filled Grok bot avatar, or playing the Grok Bot avatar animations in Herdr.
---

You work in `/home/philipp/git/gbot-tui`. `gbot_tui.py` starts the program; the code is the `gbottui` package, one module per job (see "Where the code lives" in `docs/architecture.md`). Read `docs/architecture.md` before changing behavior. Keep the chat working when Herdr, the AppImage, squashfuse, or Pillow is missing.

## Enter feels late

Pressing Enter runs `Chat.arm_send` (`gbottui/chat.py`), which appends a local user row, clears the composer, sets phase `working`, and draws before `worker.put(("send", ...))` in `chat_loop` (`gbottui/chat_screen.py`). The single `Worker` thread then runs `send_message` (`gbot send -- <target> <text>`, gateway `POST /api/sendPrompt`, timeout 90s) and the next `read_thread`. When that poll returns the same user text, the local row is replaced instead of appended. `bench/latency.py` measures Enter to bubble against a mock `gbot`.

Do not block the curses loop on `gbot`. Do not start a second worker that can reorder send and read. `GBOT_LOCK` in `gbottui/gbot.py` is that order: the overview scan, a send, and a chat read share it, so a scan cannot pass a send. Threads that only list bots or compose icons are fine; they never touch the send/read queue or write to the terminal.

## Herdr icons

`compose_avatar` (`gbottui/icons.py`) tints frame 0 of `bot-<shape>-face.png` and writes a straight-alpha PNG: RGB is the avatar color, alpha is the frame's alpha. The strip is 29 frames of 18×18. RGB in that file is black and must not be drawn. Eyes are transparent holes. Pasting the black frame onto the mask paints a silhouette, which is only a rim on the dark tile.

A bot that never picked a shape or color still gets an icon. `bot_avatar` (`gbottui/colors.py`) hashes the agent id the way the desktop app does, so the color is never empty. `install_avatars` rewrites `~/.config/herdr-app/icons/gbot/*.png` when the bytes differ. `programs.json` may contain both the full `gbot-<uuid>` key and the 32-byte compact key from `program_key`. Herdr drops an OSC 7501 `app` longer than 32 bytes, and also drops one that contains whitespace (space, nbsp, en space), so `notice_name` turns whitespace into hyphens and `programs.json` `match` includes that hyphenated name. Herdr Desktop 0.1.11 is a Herdr session via `HC_TERMINAL_ID`, `HC_SCOPE`, or `HC_TERMINAL_SOCKET`, not `HERDR_ENV`. A first OSC 11 reply of `#282C34` is rechecked up to five times. One still icon. Do not add animation. Colors stay `AVATAR_COLORS`. `tint` stays false. Do not mount the AppImage in tests.

## Animations

The 29 frames, in order, are the tray timeline `Pcr()` in the app:

- 0 rest (`open` 1)
- 1–4 blink closed (`open` eased from 1 toward 0.12; anchors on the half-closed and closed frames)
- 5–7 blink open (`open` 0.5, 0.85, 1.08)
- then glance left, glance right, glance up. Each glance is 3 frames in, 1 hold, 3 frames out

Playback holds: rest about 2.8–5.5s, blink frames 18ms (closed 35ms, open 75ms), glance in/out 45ms, glance hold about 0.5–0.9s. Half the cycles blink, the rest glance, with a small chance of a second blink.

Herdr's program icon is one static path in `programs.json` (`~/.config/herdr-app/AGENTS.md`). There is no animation field. Animated WebP in that file is for update-note media, not program icons. A mod that swaps frames on `cx.timer` redraws the whole window; Herdr's own guide says not to animate that way. `.frames()` is for text, not images.

Ship the tinted rest frame as the icon. Do not add a timer mod or an animated file unless you have confirmed this Herdr build actually plays it inside a program icon. Say that limit in the reply instead of implying the strip can be dropped in as-is.
