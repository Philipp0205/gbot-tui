"""The chat screen: keys, the event loop, and jobs."""

import curses
import os
import queue
import select
import sys
import threading
import time

from . import activity, app, cache, chat_state, chat_view, draft, gbot_cli, grid, herdr, paint, roster, styles, terminal, theme, transcript, widgets


# The loop sleeps in select, so a finished read wakes it at once. This cap is
# how late a resize can be noticed.
WAKE_CAP = 0.1


def composer_key(chat, key):
    """What y or n does in the composer.

    Lowercase y and n answer a card only when it has both ids and the box is
    empty. Shift-Y and Shift-N are text, and ctrl-y / ctrl-n insert the
    lowercase letter, so a message can still start with y or n. A card that
    cannot be answered does not swallow the key.
    """
    if key == "\x19":
        return ("type", "y")
    if key == "\x0e":
        return ("type", "n")
    if key not in ("y", "Y", "n", "N"):
        return None
    if (
        not chat.typed
        and not chat.deciding
        and chat.phase == "blocked"
        and key in ("y", "n")
        and transcript.latest_decision(chat.messages, chat.answered)
    ):
        return ("decide", "accept" if key == "y" else "decline")
    if chat.deciding and not chat.typed and key in ("y", "n"):
        return ("ignore", "")
    return ("type", key)


def picked_lines(entry, picks, step_at=0):
    _prompt, options, _multi, _custom = widgets.current_question(entry, step_at)
    chosen = set(picks or ())
    return "\n".join(value for _label, value in options if value in chosen)


def clear_choice_digit(chat):
    chat.digit_buf = ""
    chat.digit_at = None


def _digit_fresh(chat):
    if not getattr(chat, "digit_buf", "") or chat.digit_at is None:
        return False
    return activity.clock() - chat.digit_at <= widgets.DIGIT_WINDOW


def _activate_option(chat, number):
    """1-based option. A tuple is the send job. "toggle", "step", or "ignore"."""
    entry = widgets.latest_widget(chat.messages)
    if entry is None or not chat.loaded or not (chat.agent_id or chat.session_id):
        return "ignore"
    _prompt, options, multi, _custom = widgets.current_question(entry, chat.step_at(entry))
    if number < 1 or number > len(options):
        return "ignore"
    index = number - 1
    if multi:
        chat.toggle_choice(index)
        return "toggle"
    result = chat.commit_choice(options[index][1])
    if result == "step":
        chat.error = ""
        return "step"
    if not result:
        return "ignore"
    chat.error = ""
    return ("choice", result)


def flush_choice_digit(chat):
    """Once the window passes, a lone 1–9 selects. `0` is dropped.

    Returns the same kinds of value as `choice_key`, or None if still waiting.
    """
    if not getattr(chat, "digit_buf", "") or chat.digit_at is None:
        return None
    if activity.clock() - chat.digit_at <= widgets.DIGIT_WINDOW:
        return None
    buf = chat.digit_buf
    clear_choice_digit(chat)
    if len(buf) != 1 or buf == "0":
        return None
    entry = widgets.latest_widget(chat.messages)
    if entry is None:
        return None
    _prompt, options, _multi, _custom = widgets.current_question(entry, chat.step_at(entry))
    if len(options) <= widgets.OPTION_DIGITS:
        return None
    return _activate_option(chat, int(buf))


def choice_pause(chat):
    """Seconds left on a two-digit entry, or None."""
    if not _digit_fresh(chat):
        return None
    return max(widgets.DIGIT_WINDOW - (activity.clock() - chat.digit_at), 0)


def choice_key(chat, key):
    """What a digit does while a widget is open.

    A tuple is the send job. "toggle", "step", "wait", and "ignore" consume
    the key. None means the key is ordinary text. A letter is always text, so
    a message can start with one. With more than 9 options the choice is two
    digits inside DIGIT_WINDOW (`10`, or `02`). Text already in the composer
    wins, so a space and then a digit types the digit.
    """
    if not isinstance(key, str) or len(key) != 1:
        return None
    if chat.typed or key.isalpha():
        clear_choice_digit(chat)
        return None
    if chat.widget_busy:
        if key.isdigit():
            return "ignore"
        return None
    entry = widgets.latest_widget(chat.messages)
    if entry is None:
        clear_choice_digit(chat)
        return None
    if not chat.loaded or not (chat.agent_id or chat.session_id):
        if key.isdigit():
            return "ignore"
        return None
    _prompt, options, _multi, _custom = widgets.current_question(entry, chat.step_at(entry))
    if not options or not key.isdigit():
        return None
    if len(options) <= widgets.OPTION_DIGITS:
        clear_choice_digit(chat)
        picked = widgets.key_to_index(key, len(options))
        if picked is None:
            return None
        if picked == "ignore":
            return "ignore"
        return _activate_option(chat, picked + 1)
    if _digit_fresh(chat) and chat.digit_buf:
        number = int(chat.digit_buf + key)
        clear_choice_digit(chat)
        if number < 1 or number > len(options):
            return "ignore"
        return _activate_option(chat, number)
    chat.digit_buf = key
    chat.digit_at = activity.clock()
    return "wait"


# Ctrl keys that edit the draft, as in readline.
EDIT_KEYS = {
    "\x01": draft.line_start,
    "\x05": draft.line_end,
    "\x17": draft.kill_word,
    "\x15": draft.kill_line_start,
    "\x0b": draft.kill_line_end,
}
# Curses keys, and the terminfo names of the modified arrows.
CURSOR_KEYS = {
    curses.KEY_LEFT: draft.left,
    curses.KEY_RIGHT: draft.right,
    curses.KEY_HOME: draft.line_start,
    curses.KEY_END: draft.line_end,
    curses.KEY_DC: draft.delete,
}
WORD_KEYS = {b"kLFT5": draft.word_left, b"kLFT3": draft.word_left, b"kRIT5": draft.word_right, b"kRIT3": draft.word_right}


def edit_motion(key):
    """The `draft` function a cursor or edit key runs, or None."""
    if isinstance(key, str):
        return EDIT_KEYS.get(key)
    if not isinstance(key, int):
        return None
    if key in CURSOR_KEYS:
        return CURSOR_KEYS[key]
    try:
        name = curses.keyname(key)
    except (curses.error, ValueError):
        return None
    if isinstance(name, str):
        name = name.encode()
    return WORD_KEYS.get(name)


def screen_width(stdscr):
    try:
        return stdscr.getmaxyx()[1]
    except AttributeError:
        return 80


def vertical_key(chat, key, stdscr):
    """Up or Down: the row above or below inside the draft, else the input history."""
    delta = -1 if key == curses.KEY_UP else 1
    pos = grid.draft_vertical(chat.typed, chat.pos, screen_width(stdscr), delta)
    if pos is None:
        # Past the first or last row: earlier or later sent messages.
        return chat.recall(delta)
    chat.pos = pos
    return True


SEARCH_KEY = "\x06"  # ctrl-f
SEARCH_OLDER = ("\r", "\x0e", curses.KEY_ENTER)  # enter, ctrl-n
SEARCH_NEWER = ("\x10",)  # ctrl-p


def search_key(chat, key, stdscr):
    """A key while the search prompt is open. Esc closes the search, not the chat.

    Typing jumps to the newest match. Enter or ctrl-n step to the next older
    one and ctrl-p back toward newer, both wrapping. Page keys and the wheel
    still scroll.
    """
    if key in ("\x1b", 27):
        chat.search = None
        chat.search_at = None
        chat.search_jump = False
        return None
    if terminal.page_motion(key) is not None or terminal.wheel_direction(key) is not None:
        return False
    hits = grid.search_hits(grid.visible_rows(chat, screen_width(stdscr)), chat.search)
    if key in SEARCH_OLDER or key in SEARCH_NEWER:
        if hits:
            at = len(hits) - 1 if chat.search_at is None else chat.search_at
            at += -1 if key in SEARCH_OLDER else 1
            chat.search_at = at % len(hits)
            chat.search_jump = True
        return None
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        chat.search = chat.search[:-1]
    elif isinstance(key, str) and key.isprintable():
        chat.search += key
    else:
        return None
    chat.search_at = None
    chat.search_jump = bool(chat.search)
    return None


COPY_KEY = "\x0f"  # ctrl-o: ctrl-y and ctrl-n already type y and n


def copy_last_reply(chat):
    """OSC 52 the newest bot message to the terminal clipboard. Footer says so."""
    for entry in reversed(chat.messages):
        if isinstance(entry, dict) and not transcript.is_user(entry):
            text = transcript.entry_text(entry).strip()
            if text:
                terminal.write_tty(terminal.osc52(text))
                chat.show_notice(f"Copied the last reply ({len(text)} chars).")
                return True
    chat.show_notice("No reply to copy.")
    return False


def chat_key(chat, key, stdscr):
    """Apply one key. Returns "quit", "back", a job tuple, or None."""
    if key == curses.KEY_RESIZE:
        return None
    if key in ("\x03", "\x04"):
        return "quit"
    # The next key clears a notice; ctrl-o sets a new one below.
    chat.expire_notice(any_key=True)
    if getattr(chat, "help", False):
        # Any key closes the overlay. Esc closes it and stays in the chat;
        # a second `?` types one, so a message can still start with it.
        chat.help = False
        if key == "?":
            chat.type_text("?")
        return None
    if chat.search is not None and search_key(chat, key, stdscr) is None:
        return None
    if key == COPY_KEY:
        copy_last_reply(chat)
        return None
    if key == SEARCH_KEY:
        chat.search = ""
        chat.search_at = None
        return None
    if key in ("\x1b", 27):
        return "back"
    if key == curses.KEY_F1 or (key == "?" and not chat.typed):
        chat.help = True
        return None
    chat.nudge()
    motion = terminal.page_motion(key)
    if motion is None:
        terminal.LAST_CLICK = None
        wheel = terminal.wheel_direction(key)
        if wheel is not None:
            motion = (wheel, None)
        elif terminal.LAST_CLICK:
            terminal.open_link_at(*terminal.LAST_CLICK)
            return None
    if motion is not None:
        direction, half = motion
        step = terminal.WHEEL_LINES if half is None else terminal.page_step(curses.LINES, half)
        if direction < 0:
            chat.follow = False
            chat.scroll = max(chat.scroll - step, 0)
            return None
        chat.scroll += step
        height, width = stdscr.getmaxyx()
        page_header, body_h, _composer, _footer = grid.layout(height, len(grid.draft_lines(chat.typed, width)) or 1)
        room = max(body_h - grid.header_gap(page_header, chat.label, body_h), 0)
        rows = grid.visible_rows(chat, width)
        if chat.scroll >= max(len(rows) - room, 0):
            chat.follow = True
        return None
    if key == "\n":
        # nonl() makes Enter "\r", so a bare LF is ctrl+enter / ctrl+j.
        chat.type_text("\n")
        return None
    change = edit_motion(key)
    if change is not None:
        chat.edit(change)
        return None
    if key in (curses.KEY_UP, curses.KEY_DOWN):
        vertical_key(chat, key, stdscr)
        return None
    if key in ("\r", curses.KEY_ENTER):
        entry = widgets.latest_widget(chat.messages) if not chat.widget_busy else None
        if entry is not None and chat.typed.strip():
            _prompt, _options, _multi, custom = widgets.current_question(entry, chat.step_at(entry))
            if custom:
                text = chat.typed.strip()
                chat.typed = ""
                chat.error = ""
                result = chat.commit_choice(text)
                if result == "step":
                    return None
                if result:
                    chat.follow = True
                    return ("choice", result)
        if entry is not None and not chat.typed:
            _prompt, _options, multi, _custom = widgets.current_question(entry, chat.step_at(entry))
            if multi:
                value = picked_lines(entry, chat.widget_picks.get(entry.get("id")), chat.step_at(entry))
                result = chat.commit_choice(value) if value else None
                if result == "step":
                    chat.error = ""
                    chat.follow = True
                    return None
                if result:
                    chat.error = ""
                    chat.follow = True
                    return ("choice", result)
        try:
            route = roster.parse_route(chat.typed.strip(), [bot["name"] for bot in cache.read_roster()])
        except gbot_cli.GbotError as error:
            chat.error = str(error)
            return None
        if route and route[0] != chat.target:
            chat.typed = ""
            chat.error = ""
            return ("route", route)
        if route:
            chat.typed, chat.pos = route[1], None
        text = chat.arm_send()
        if not text:
            return None
        chat.follow = True
        return ("send", (chat.target, text))
    handled = composer_key(chat, key)
    if handled is not None:
        action, value = handled
        if action == "ignore":
            return None
        if action == "decide":
            args = chat.begin_decision(value)
            if not args:
                chat.type_text("y" if value == "accept" else "n")
                return None
            chat.error = ""
            return ("decide", args)
        chat.type_text(value)
        return None
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        if getattr(chat, "digit_buf", "") and not chat.typed:
            clear_choice_digit(chat)
            return None
        chat.edit(draft.backspace)
        return None
    picked = choice_key(chat, key)
    if picked in {"ignore", "toggle", "step", "wait"}:
        return None
    if isinstance(picked, tuple):
        return picked
    if isinstance(key, str) and key.isprintable():
        chat.type_text(key)
    return None


def chat_loop(stdscr, chat):
    styles.apply_theme(stdscr)
    paint.fast_escape()
    paint.place_cursor(True)
    stdscr.keypad(True)
    curses.nonl()

    terminal.enable_wheel()
    # A pasted block arrives between ESC [200~ and ESC [201~, so its newlines stay in the draft.
    paint.PASTING = False
    terminal.write_tty(paint.BRACKETED_PASTE_ON)
    # gbot's network calls used to run here, so typing froze until each one returned.
    # Keys are read without waiting; the loop sleeps in select on the tty and a pipe.
    stdscr.timeout(0)
    jobs = queue.Queue()
    done = queue.Queue()
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)

    def finish(item):
        done.put(item)
        try:
            os.write(wake_w, b"x")
        except OSError:
            pass

    def worker():
        # One worker, so a send never overtakes the read before it.
        while True:
            job = jobs.get()
            if job is None:
                # The worker owns the write end, so a late result never lands
                # on a descriptor number the next chat has reused.
                os.close(wake_w)
                return
            kind, args = job
            try:
                if kind == "read":
                    finish(("page", gbot_cli.read_thread(*args)))
                elif kind == "decide":
                    gbot_cli.respond_approval(*args)
                    finish(("decided", None))
                elif kind == "choice":
                    gbot_cli.respond_widget(*args)
                    finish(("chosen", None))
                elif kind == "route":
                    gbot_cli.send_message(*args)
                    finish(("routed", args[0]))
                else:
                    gbot_cli.send_message(*args)
                    finish(("sent", None))
            except gbot_cli.GbotError as error:
                finish(("error", (kind, str(error))))

    threading.Thread(target=worker, daemon=True).start()
    jobs.put(("read", (chat.target,)))
    waiting = 1
    next_poll = 0
    dirty = True
    shown_offline = ""
    try:
        tty = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        tty = None
    try:
        while True:
            try:
                while os.read(wake_r, 4096):
                    pass
            except OSError:
                pass
            while True:
                try:
                    kind, payload = done.get_nowait()
                except queue.Empty:
                    break
                waiting -= 1
                dirty = True
                if kind == "error":
                    source, message = payload
                    chat.error = message
                    if source == "decide":
                        chat.fail_decision()
                    if source == "choice":
                        chat.fail_choice()
                    if source == "send" and chat.phase == "working":
                        chat.note("idle")
                    if source == "read":
                        next_poll = time.monotonic() + chat.read_failed(message)
                elif kind == "routed":
                    chat.show_notice(f"Sent to {payload}.")
                elif kind in {"sent", "decided", "chosen"}:
                    if kind == "decided":
                        chat.finish_decision()
                    elif kind == "chosen":
                        chat.finish_choice()
                    next_poll = 0
                elif not chat.loaded:
                    chat.ingest(payload, first=True)
                    cache.write_thread_cache(chat.target, chat.messages)
                    next_poll = time.monotonic() + chat.poll_delay()
                else:
                    chat.ingest(payload, first=False)
                    next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
            if theme.maybe_retheme(lambda: styles.apply_theme(stdscr)):
                dirty = True
            if chat.app_seen != herdr.APP:
                chat.app_seen = herdr.APP
                herdr.publish(chat.phase, chat.title, chat.session_id, chat.resume)
            if not chat.label and chat.target in roster.ROSTER:
                # A list refresh behind a named open can bring the tagline late.
                bot = roster.ROSTER.get(chat.target) or {}
                label = (bot.get("title") or bot.get("tagline") or "").strip()
                if label:
                    chat.label = label
                    chat.avatar_color = chat.avatar_color or bot.get("avatarColor") or ""
                    dirty = True
            if chat.expire_working():
                dirty = True
            if chat.expire_notice():
                dirty = True
            offline = chat.offline_label()
            if offline != shown_offline:
                # The countdown ticks once a second.
                shown_offline = offline
                dirty = True
            flushed = flush_choice_digit(chat)
            if isinstance(flushed, tuple):
                chat_view.draw(stdscr, chat)
                dirty = False
                jobs.put(flushed)
                waiting += 1
                next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
            elif flushed is not None:
                dirty = True
            now = time.monotonic()
            if waiting == 0 and now >= next_poll and (chat.loaded or chat.error or chat.read_failures):
                jobs.put(("read", chat.take_read()))
                waiting += 1
                next_poll = now + chat.poll_delay()
            if dirty:
                chat_view.draw(stdscr, chat)
                dirty = False
            keys = paint.read_keys(stdscr)
            if not keys:
                wait = WAKE_CAP
                if waiting == 0 and (chat.loaded or chat.error or chat.read_failures):
                    wait = min(WAKE_CAP, max(next_poll - time.monotonic(), 0))
                pause = choice_pause(chat)
                if pause is not None:
                    wait = min(wait, pause)
                watch = [wake_r] + ([tty] if tty is not None else [])
                try:
                    select.select(watch, [], [], wait)
                except (OSError, ValueError):
                    time.sleep(min(wait, 0.02))
                keys = paint.read_keys(stdscr)
            for key in keys:
                dirty = True
                if key == curses.KEY_RESIZE:
                    theme.note_appearance()
                    if theme.maybe_retheme(lambda: styles.apply_theme(stdscr)):
                        dirty = True
                result = chat_key(chat, key, stdscr)
                if result == "quit":
                    return None
                if result == "back":
                    return "back"
                if isinstance(result, tuple):
                    if result[0] in {"send", "choice"}:
                        # The bubble, or the chosen option, is on screen before gbot starts.
                        chat_view.draw(stdscr, chat)
                        dirty = False
                    jobs.put(result)
                    waiting += 1
                    if result[0] in {"send", "choice"}:
                        next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
    except KeyboardInterrupt:
        return None
    finally:
        terminal.write_tty(paint.BRACKETED_PASTE_OFF)
        paint.PASTING = False
        jobs.put(None)
        cache.write_thread_cache(chat.target, chat.messages)
        os.close(wake_r)


def open_chat(target):
    chat = app.start_chat(target)
    back = None
    try:
        back = curses.wrapper(lambda stdscr: chat_loop(stdscr, chat))
        return back
    finally:
        app.leave_chat(chat, True)
