"""The chat's curses loop. Keys stay local; gbot runs on one worker thread."""

import curses
import functools
import os
import queue
import select
import sys
import threading
import time

from . import cache, herdr, theme
from .chat import IDLE_DELAYS
from .chat_view import draw
from .gbot import GbotError, read_thread, respond_approval, send_message
from .layout import draft_lines, header_gap, layout, visible_rows
from .paint import fast_escape, place_cursor, read_keys
from .styles import apply_theme
from .transcript import latest_decision

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
        and latest_decision(chat.messages, chat.answered)
    ):
        return ("decide", "accept" if key == "y" else "decline")
    if chat.deciding and not chat.typed and key in ("y", "n"):
        return ("ignore", "")
    return ("type", key)


def chat_key(chat, key, stdscr):
    """Apply one key. Returns "quit", "back", a job tuple, or None."""
    if key == curses.KEY_RESIZE:
        return None
    if key in ("\x03", "\x04"):
        return "quit"
    if key in ("\x1b", 27):
        return "back"
    chat.nudge()
    if key == curses.KEY_PPAGE:
        chat.follow = False
        chat.scroll = max(chat.scroll - max(curses.LINES - 4, 1), 0)
        return None
    if key == curses.KEY_NPAGE:
        chat.scroll += max(curses.LINES - 4, 1)
        height, width = stdscr.getmaxyx()
        header, body_h, _composer, _footer = layout(height, len(draft_lines(chat.typed, width)) or 1)
        room = max(body_h - header_gap(header, chat.label, body_h), 0)
        if chat.scroll >= max(len(visible_rows(chat, width)) - room, 0):
            chat.follow = True
        return None
    if key in ("\n", "\r", curses.KEY_ENTER):
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
                chat.typed += "y" if value == "accept" else "n"
                return None
            chat.error = ""
            return ("decide", args)
        chat.typed += value
        return None
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        chat.typed = chat.typed[:-1]
        return None
    if isinstance(key, str) and key.isprintable():
        chat.typed += key
    return None


class Worker:
    """Runs gbot jobs in order on one thread and wakes the loop when one is done.

    One worker, so a send never overtakes the read before it.
    """

    def __init__(self):
        self.jobs = queue.Queue()
        self.done = queue.Queue()
        self.waiting = 0
        self.wake, self._wake_w = os.pipe()
        os.set_blocking(self.wake, False)
        os.set_blocking(self._wake_w, False)
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, job):
        self.jobs.put(job)
        self.waiting += 1

    def results(self):
        """Finished jobs, oldest first, as (kind, payload)."""
        try:
            while os.read(self.wake, 4096):
                pass
        except OSError:
            pass
        found = []
        while True:
            try:
                found.append(self.done.get_nowait())
            except queue.Empty:
                break
        self.waiting -= len(found)
        return found

    def stop(self):
        self.jobs.put(None)
        os.close(self.wake)

    def _finish(self, item):
        self.done.put(item)
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                # The worker owns the write end, so a late result never lands
                # on a descriptor number the next chat has reused.
                os.close(self._wake_w)
                return
            kind, args = job
            try:
                if kind == "read":
                    self._finish(("page", read_thread(*args)))
                elif kind == "decide":
                    respond_approval(*args)
                    self._finish(("decided", None))
                else:
                    send_message(*args)
                    self._finish(("sent", None))
            except GbotError as error:
                self._finish(("error", (kind, str(error))))


def take_result(chat, kind, payload, next_poll):
    """Fold one finished job into the chat. Returns when the next read is due."""
    if kind == "error":
        source, message = payload
        chat.error = message
        if source == "decide":
            chat.fail_decision()
        if source == "send" and chat.phase == "working":
            chat.note("idle")
        if source == "read":
            chat.read_failed()
            return time.monotonic() + IDLE_DELAYS[0]
        return next_poll
    if kind in {"sent", "decided"}:
        if kind == "decided":
            chat.finish_decision()
        return 0
    if not chat.loaded:
        chat.ingest(payload, first=True)
        cache.write_thread_cache(chat.target, chat.messages)
        return time.monotonic() + chat.poll_delay()
    chat.ingest(payload, first=False)
    return min(next_poll, time.monotonic() + chat.poll_delay())


def chat_loop(stdscr, chat):
    apply_theme(stdscr)
    fast_escape()
    place_cursor(True)
    stdscr.keypad(True)
    # gbot's network calls used to run here, so typing froze until each one returned.
    # Keys are read without waiting; the loop sleeps in select on the tty and a pipe.
    stdscr.timeout(0)
    retheme = functools.partial(apply_theme, stdscr)
    worker = Worker()
    worker.put(("read", (chat.target,)))
    next_poll = 0
    dirty = True
    try:
        tty = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        tty = None
    try:
        while True:
            for kind, payload in worker.results():
                dirty = True
                next_poll = take_result(chat, kind, payload, next_poll)
            if theme.maybe_retheme(retheme):
                dirty = True
            if chat.app_seen != herdr.APP:
                chat.app_seen = herdr.APP
                chat.publish()
            if chat.take_roster_label():
                dirty = True
            ready = chat.loaded or chat.error
            now = time.monotonic()
            if worker.waiting == 0 and now >= next_poll and ready:
                worker.put(("read", chat.take_read()))
                next_poll = now + chat.poll_delay()
            if dirty:
                draw(stdscr, chat)
                dirty = False
            keys = read_keys(stdscr)
            if not keys:
                wait = WAKE_CAP
                if worker.waiting == 0 and ready:
                    wait = min(WAKE_CAP, max(next_poll - time.monotonic(), 0))
                watch = [worker.wake] + ([tty] if tty is not None else [])
                try:
                    select.select(watch, [], [], wait)
                except (OSError, ValueError):
                    time.sleep(min(wait, 0.02))
                keys = read_keys(stdscr)
            for key in keys:
                dirty = True
                if key == curses.KEY_RESIZE:
                    theme.note_appearance()
                    theme.maybe_retheme(retheme)
                result = chat_key(chat, key, stdscr)
                if result == "quit":
                    return None
                if result == "back":
                    return "back"
                if isinstance(result, tuple):
                    if result[0] == "send":
                        # The bubble is on screen before gbot starts.
                        draw(stdscr, chat)
                        dirty = False
                    worker.put(result)
                    if result[0] == "send":
                        next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
    except KeyboardInterrupt:
        return None
    finally:
        worker.stop()
        cache.write_thread_cache(chat.target, chat.messages)
