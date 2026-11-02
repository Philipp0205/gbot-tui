"""What each bot is doing: needs you, a new reply, running, or idle.

The list and `gbot-tui --overview` read this. A scan stores it under
~/.cache/gbot-tui and does not move the cursor the user has already seen.
"""

import os
import threading

from .cache import cache_dir, read_json, read_thread_cache, write_private
from .gbot import GbotError, read_thread
from .transcript import approval_of, entry_text, is_user, last_id, pending_approval

# How many trailing entries the overview reads. Same window as a chat poll,
# so a pending card the chat would see is visible here too.
OVERVIEW_LIMIT = 80
# The list stays open. Scan again this long after the previous pass finishes.
OVERVIEW_PAUSE = 8
OVERVIEW_STATES = ("blocked", "unread", "working", "idle")
ATTENTION_RANK = {"blocked": 0, "unread": 1, "working": 2, "idle": 4}
STATUS_STYLE = {"blocked": "yellow", "unread": "green", "working": "bold", "idle": "dim"}
# In-progress flags a transcript entry may carry. Absent on a finished line.
PROGRESS_STATES = {"running", "streaming", "in_progress", "generating", "working"}

STATE_LOCK = threading.Lock()
ACTIVITY = {}
SEEN = {}
ACTIVITY_LOADED = {"done": False}
SEEN_LOADED = {"done": False}


def activity_path():
    return os.path.join(cache_dir(), "activity.json")


def seen_path():
    return os.path.join(cache_dir(), "seen.json")


def activity_keys(bot):
    keys = []
    bot_id = bot.get("id") if isinstance(bot, dict) else ""
    name = bot.get("name") if isinstance(bot, dict) else ""
    if isinstance(bot_id, str) and bot_id:
        keys.append("id:" + bot_id)
    if isinstance(name, str) and name:
        keys.append("name:" + name)
    return keys


def _clean_snap(snap):
    if not isinstance(snap, dict) or snap.get("state") not in OVERVIEW_STATES:
        return None
    unread = snap.get("unread")
    return {
        "state": snap["state"],
        "unread": unread if isinstance(unread, int) and unread > 0 else 0,
        "cursor": snap.get("cursor") if isinstance(snap.get("cursor"), str) else "",
        "preview": snap.get("preview") if isinstance(snap.get("preview"), str) else "",
    }


def load_activity():
    with STATE_LOCK:
        if ACTIVITY_LOADED["done"]:
            return
        ACTIVITY_LOADED["done"] = True
        data = read_json(activity_path())
        if not isinstance(data, dict):
            return
        for key, snap in data.items():
            clean = _clean_snap(snap)
            if isinstance(key, str) and clean:
                ACTIVITY[key] = clean


def load_seen():
    with STATE_LOCK:
        if SEEN_LOADED["done"]:
            return
        SEEN_LOADED["done"] = True
        data = read_json(seen_path())
        if not isinstance(data, dict):
            return
        for key, cursor in data.items():
            if isinstance(key, str) and isinstance(cursor, str) and cursor:
                SEEN[key] = cursor


def store_activity(bot, snap):
    clean = _clean_snap(snap)
    keys = activity_keys(bot)
    if not clean or not keys:
        return
    load_activity()
    with STATE_LOCK:
        for key in keys:
            ACTIVITY[key] = clean
        write_private(activity_path(), dict(ACTIVITY))


def lookup_activity(bot):
    load_activity()
    with STATE_LOCK:
        for key in activity_keys(bot):
            snap = ACTIVITY.get(key)
            if snap:
                return dict(snap)
    return None


def remember_seen(name, bot_id, cursor, only_if_absent=False):
    """The latest entry this chat had on screen. A later reply is then `new`.

    `only_if_absent` is the first scan of a bot nobody has opened. It must not
    move a cursor the chat, or an earlier scan, already stored.
    """
    if not isinstance(cursor, str) or not cursor:
        return
    load_seen()
    with STATE_LOCK:
        keys = []
        if bot_id:
            keys.append("id:" + str(bot_id))
        if name:
            keys.append("name:" + name)
        if only_if_absent and any(SEEN.get(key) for key in keys):
            return
        for key in keys:
            SEEN[key] = cursor
        write_private(seen_path(), dict(SEEN))


def lookup_seen(bot):
    load_seen()
    with STATE_LOCK:
        for key in activity_keys(bot):
            cursor = SEEN.get(key)
            if cursor:
                return cursor
    return ""


def seen_mark(bot):
    """The cursor to diff against. A scan never moves this.

    The seen file wins. The first look writes it once, and the chat advances
    it when the thread is on screen. A cached thread counts when that file has
    no row, because the thread was on screen. The activity cursor does not:
    it advances every pass, which cleared `new` on the pass after it appeared.
    """
    seen = lookup_seen(bot)
    if seen:
        return seen
    return last_id(read_thread_cache(bot.get("name") or ""))


def baseline_seen(bot, cursor):
    """Record the first cursor a scan saw. Later scans leave it where it is."""
    if not isinstance(cursor, str) or not cursor:
        return
    if lookup_seen(bot):
        return
    if last_id(read_thread_cache(bot.get("name") or "")):
        return
    remember_seen(bot.get("name") or "", bot.get("id") or "", cursor, only_if_absent=True)


def one_line(text, limit=160):
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    if limit <= 1:
        return "…"
    return flat[: limit - 1] + "…"


def awaiting_reply(entries):
    """True when the last line with text is the user's. The bot has not answered."""
    speaker = ""
    for entry in entries:
        if entry.get("local"):
            continue
        text = entry_text(entry).strip()
        if not text:
            continue
        speaker = "user" if is_user(entry) else "other"
    return speaker == "user"


def entry_in_progress(entries):
    if not entries:
        return False
    last = entries[-1]
    if last.get("local"):
        return False
    if last.get("streaming") is True or last.get("inProgress") is True:
        return True
    status = last.get("status")
    if isinstance(status, str) and status.lower() in PROGRESS_STATES:
        return True
    return False


def notable(entry):
    """A line worth calling new: a bot message or a card, not the user's own text."""
    if not isinstance(entry, dict) or entry.get("local") or is_user(entry):
        return False
    if entry_text(entry).strip():
        return True
    return approval_of(entry) is not None


def unseen_notable(entries, seen):
    if not seen:
        return 0
    index = next((i for i, entry in enumerate(entries) if entry.get("id") == seen), None)
    # The seen id is older than this tail, so every line here arrived after it.
    rest = entries if index is None else entries[index + 1 :]
    return sum(1 for entry in rest if notable(entry))


def activity_preview(entries, state):
    if state == "blocked":
        for entry in reversed(entries):
            card = approval_of(entry)
            if not card:
                continue
            if card["details"]:
                return one_line(card["details"][0])
            return "Needs approval"
    for entry in reversed(entries):
        text = entry_text(entry).strip()
        if text:
            return one_line(text)
    return ""


def judge_activity(entries, cursor, seen):
    """What one thread tail says. `seen` is the last cursor the user had on screen.

    An empty `seen` does not mark the whole history as new. `blocked` wins over
    a new reply, and a new reply wins over `running`.
    """
    entries = [entry for entry in entries if isinstance(entry, dict) and not entry.get("local")]
    cursor = cursor or last_id(entries)
    if pending_approval(entries):
        phase = "blocked"
    elif awaiting_reply(entries) or entry_in_progress(entries):
        phase = "working"
    else:
        phase = "idle"
    unread = unseen_notable(entries, seen)
    if phase == "blocked":
        state = "blocked"
    elif unread:
        state = "unread"
    else:
        state = phase
    return {
        "state": state,
        "unread": unread,
        "cursor": cursor,
        "preview": activity_preview(entries, state),
    }


def status_label(state, unread=0):
    if state == "blocked":
        return "needs you"
    if state == "unread":
        count = unread if isinstance(unread, int) else 0
        if count > 1:
            return f"{count} new"
        return "new"
    if state == "working":
        return "running"
    if state == "idle":
        return "idle"
    return ""


def with_activity(bots):
    views = []
    for bot in bots:
        view = dict(bot)
        snap = lookup_activity(bot)
        if snap:
            view["activity"] = snap
        views.append(view)
    return views


def order_bots(bots):
    """Needs-you, then new, then running. A search does not use this order."""
    ranked = []
    for index, bot in enumerate(bots):
        state = ((bot.get("activity") or {}).get("state")) or ""
        rank = ATTENTION_RANK.get(state, 3)
        ranked.append((rank, index, bot))
    ranked.sort()
    return [bot for _rank, _index, bot in ranked]


def attention_counts(bots):
    counts = {state: 0 for state in OVERVIEW_STATES}
    for bot in bots:
        state = ((bot.get("activity") or {}).get("state")) or ""
        if state in counts:
            counts[state] += 1
    return counts


def _count_phrase(count, one, many):
    if count == 1:
        return one
    if count:
        return f"{count} {many}"
    return ""


def overview_summary(bots, checked=None, total=None):
    """One line: `1 needs you · 2 new · 1 running`, plus a scan hint while reading."""
    counts = attention_counts(bots)
    parts = [
        phrase
        for phrase in (
            _count_phrase(counts["blocked"], "1 needs you", "need you"),
            _count_phrase(counts["unread"], "1 new", "new"),
            _count_phrase(counts["working"], "1 running", "running"),
        )
        if phrase
    ]
    scanning = bool(total) and checked is not None and checked < total
    if not parts:
        if scanning:
            if not checked:
                return "checking threads"
            return f"checking {checked} of {total}"
        return _count_phrase(counts["idle"], "1 idle", "idle")
    if scanning:
        if not checked:
            parts.append("checking threads")
        else:
            parts.append(f"checking {checked} of {total}")
    return " · ".join(parts)


def format_overview(bots):
    ordered = order_bots(bots)
    summary = overview_summary(ordered)
    name_width = min(max((len(bot["name"]) for bot in ordered), default=0), 24)
    lines = [summary, ""] if summary else []
    for bot in ordered:
        snap = bot.get("activity") or {}
        label = status_label(snap.get("state") or "", snap.get("unread") or 0)
        preview = snap.get("preview") or bot.get("title") or bot.get("tagline") or ""
        preview = " ".join(str(preview).split())
        line = f"{label:<9}  {bot['name']:<{name_width}}"
        if preview:
            line += "  " + preview
        lines.append(line.rstrip())
    return "\n".join(lines)


def page_entries(page):
    entries = page.get("entries") if isinstance(page, dict) else []
    if not isinstance(entries, list):
        entries = []
    cursor = page.get("cursor") if isinstance(page, dict) else ""
    if not isinstance(cursor, str):
        cursor = ""
    return entries, cursor


def record_page(bot, page):
    """Judge one thread. The first look pins the seen cursor; a scan does not move it."""
    entries, cursor = page_entries(page)
    mark = seen_mark(bot)
    snap = judge_activity(entries, cursor, mark)
    if not mark:
        baseline_seen(bot, snap.get("cursor") or cursor)
    store_activity(bot, snap)
    return snap


def build_overview(bots, read):
    """Read each bot once. The first look baselines; a later look marks new replies.

    Returns (views, error). `error` is set only when every read failed.
    """
    views = []
    failed = None
    saw = False
    for bot in bots:
        try:
            page = read(bot)
        except GbotError as error:
            failed = str(error)
            views.append(dict(bot))
            continue
        saw = True
        snap = record_page(bot, page)
        view = dict(bot)
        view["activity"] = snap
        views.append(view)
    return order_bots(views), (None if saw or not bots else failed)


def read_overview(bot):
    name = bot.get("name") or ""
    if not name:
        raise GbotError("No bot.")
    return read_thread(name, limit=OVERVIEW_LIMIT, timeout=20)


class Survey:
    """Walk the roster off the UI thread and publish each bot as its read returns."""

    def __init__(self, bots, read=None):
        self._read = read or read_overview
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._bots = list(bots)
        self._checked = 0
        self._total = len(self._bots)
        self._failures = 0
        self._running = True
        self._restart = False
        self.generation = 0
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def replace(self, bots):
        with self._lock:
            self._bots = list(bots)
            self._restart = True
        self._wake.set()

    def nudge(self):
        """Drop the pause and scan again. Ctrl-R on the list."""
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()

    def status(self):
        with self._lock:
            return self._running, self._checked, self._total, self.generation

    def problem(self):
        """The gbot error when every bot in the last pass failed, else ""."""
        with self._lock:
            if self._total and self._failures == self._total and not self._running:
                return self.error
            return ""

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                bots = list(self._bots)
                self._total = len(bots)
                self._checked = 0
                self._failures = 0
                self._running = True
                self._restart = False
                self.generation += 1
            for bot in bots:
                if self._stop.is_set():
                    return
                with self._lock:
                    if self._restart:
                        break
                try:
                    page = self._read(bot)
                except GbotError as error:
                    with self._lock:
                        self.error = str(error)
                        self._failures += 1
                        self._checked += 1
                        self.generation += 1
                    continue
                record_page(bot, page)
                with self._lock:
                    self._checked += 1
                    self.generation += 1
            else:
                with self._lock:
                    self._running = False
                    self.generation += 1
                self._wake.wait(OVERVIEW_PAUSE)
                self._wake.clear()
                continue
            # The roster was replaced mid-pass. Start over without the pause.
