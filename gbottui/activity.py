"""Per-bot state: needs you, unread, working, idle, and the seen cursors."""

import os
import threading
import time

from . import backend, cache, gbot_cli, markdown, picker, styles, transcript, widgets


# A turn that never grows (the last line stays yours, or isStreaming stays
# set) would otherwise say `working` forever. This is how long with no new
# text before the chat and the overview fall back to idle.
WORKING_TIMEOUT = 120


# How many trailing entries the overview reads. Same window as a chat poll,
# so a pending card the chat would see is visible here too.
OVERVIEW_LIMIT = 80
# The list stays open. Scan again this long after the previous pass finishes.
OVERVIEW_PAUSE = 8
OVERVIEW_STATES = ("blocked", "unread", "working", "idle")
ATTENTION_RANK = {"blocked": 0, "unread": 1, "working": 2, "idle": 4}
# `working` stays off the yellow used for `needs you`, in the list and the header.
STATUS_STYLE = {"blocked": "yellow", "unread": "green", "working": "cyan", "idle": "dim"}
# In-progress flags a transcript entry may carry. Absent on a finished line.
PROGRESS_STATES = {"running", "streaming", "in_progress", "generating", "working"}

STATE_LOCK = threading.Lock()
ACTIVITY = {}
SEEN = {}
ACTIVITY_LOADED = {"done": False}
SEEN_LOADED = {"done": False}


def activity_path():
    return os.path.join(cache.cache_dir(), "activity.json")


def seen_path():
    return os.path.join(cache.cache_dir(), "seen.json")


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
    latest = snap.get("latest")
    return {
        "latest": latest if isinstance(latest, (int, float)) and latest > 0 else 0,
        "state": snap["state"],
        "unread": unread if isinstance(unread, int) and unread > 0 else 0,
        "cursor": snap.get("cursor") if isinstance(snap.get("cursor"), str) else "",
        "preview": snap.get("preview") if isinstance(snap.get("preview"), str) else "",
        "token": snap.get("token") if isinstance(snap.get("token"), str) else "",
        "since": snap.get("since") if isinstance(snap.get("since"), (int, float)) else 0,
    }


def load_activity():
    with STATE_LOCK:
        if ACTIVITY_LOADED["done"]:
            return
        ACTIVITY_LOADED["done"] = True
        data = cache.read_json(activity_path())
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
        data = cache.read_json(seen_path())
        if not isinstance(data, dict):
            return
        for key, cursor in data.items():
            if isinstance(key, str) and isinstance(cursor, str) and cursor:
                SEEN[key] = cursor


def store_activity(bot, snap, only_if_absent=False):
    clean = _clean_snap(snap)
    keys = activity_keys(bot)
    if not clean or not keys:
        return
    load_activity()
    with STATE_LOCK:
        # A live read can land while the list is still judging a saved thread.
        if only_if_absent and any(ACTIVITY.get(key) for key in keys):
            return
        for key in keys:
            ACTIVITY[key] = clean
        cache._save_locked(activity_path(), dict(ACTIVITY))


def lookup_activity(bot):
    load_activity()
    with STATE_LOCK:
        for key in activity_keys(bot):
            snap = ACTIVITY.get(key)
            if snap:
                return dict(snap)
    return None


def remember_seen(name, bot_id, cursor, only_if_absent=False):
    """The latest entry this chat had on screen. A later reply is then unread.

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
        cache._save_locked(seen_path(), dict(SEEN))


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
    it advances every pass, which cleared unread on the pass after it appeared.
    """
    seen = lookup_seen(bot)
    if seen:
        return seen
    return transcript.last_id(cache.read_thread_cache(bot.get("name") or ""))


def baseline_seen(bot, cursor):
    """Record the first cursor a scan saw. Later scans leave it where it is."""
    if not isinstance(cursor, str) or not cursor:
        return
    if lookup_seen(bot):
        return
    if transcript.last_id(cache.read_thread_cache(bot.get("name") or "")):
        return
    remember_seen(bot.get("name") or "", bot.get("id") or "", cursor, only_if_absent=True)


def one_line(text, limit=160):
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    if limit <= 1:
        return "…"
    return flat[: limit - 1] + "…"


def clock():
    """Monotonic time. Tests replace this to move a quiet turn forward."""
    return time.monotonic()


def content_token(entries):
    """Identity of the visible text. A longer reply is new content; a quiet flag is not."""
    parts = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = "u" if transcript.is_user(entry) else "b"
        flag = ""
        if entry.get("streaming") is True or entry.get("inProgress") is True or entry.get("isStreaming") is True:
            flag = "*"
        parts.append(kind + flag + transcript.entry_text(entry))
    return "\x1e".join(parts)


def awaiting_reply(entries):
    """True when the last line with text is the user's. The bot has not answered."""
    speaker = ""
    for entry in entries:
        if entry.get("local"):
            continue
        text = transcript.entry_text(entry).strip()
        if not text:
            continue
        speaker = "user" if transcript.is_user(entry) else "other"
    return speaker == "user"


def entry_in_progress(entries):
    """The latest message is still being written. A trailing event does not hide that.

    The gateway flag on a message is `isStreaming`.
    """
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("local"):
            continue
        if entry.get("streaming") is True or entry.get("inProgress") is True or entry.get("isStreaming") is True:
            return True
        status = entry.get("status")
        if isinstance(status, str) and status.lower() in PROGRESS_STATES:
            return True
        if transcript.entry_text(entry).strip() or entry.get("role") or entry.get("kind") in {"message", "send-message"}:
            return False
    return False


def notable(entry):
    """A line worth calling new: a bot message or a card, not the user's own text."""
    if not isinstance(entry, dict) or entry.get("local") or transcript.is_user(entry):
        return False
    if transcript.entry_text(entry).strip():
        return True
    return transcript.approval_of(entry) is not None


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
            card = transcript.approval_of(entry)
            if not card:
                continue
            if card["details"]:
                return one_line(card["details"][0])
            return "Needs approval"
        widget = widgets.latest_widget(entries)
        if widget:
            prompt, _options, _multi, _custom = widgets.current_question(widget)
            if prompt:
                return one_line(prompt)
            return "Needs a choice"
    for entry in reversed(entries):
        text = transcript.entry_text(entry).strip()
        if text:
            return one_line(markdown.markdown_text(text))
    return ""


def _working_since(previous, token, now):
    """When this same text first looked in progress. A new clock starts over."""
    if not isinstance(previous, dict) or previous.get("token") != token:
        return now
    old = previous.get("since")
    if not isinstance(old, (int, float)):
        return now
    # A since from a previous process can sit past this clock. Start over then.
    if old > now:
        return now
    return old


def judge_activity(entries, cursor, seen, previous=None):
    """What one thread tail says. `seen` is the last cursor the user had on screen.

    An empty `seen` does not mark the whole history as new. `blocked` wins over
    a new reply, and a new reply wins over `running`. An open widget after the
    user's last message is `needs you`. `working` that gains no text for
    WORKING_TIMEOUT falls back to idle. `previous` is the last snap, so the
    overview can time that quiet stretch with `clock`.
    """
    entries = [entry for entry in entries if isinstance(entry, dict) and not entry.get("local")]
    cursor = cursor or transcript.last_id(entries)
    now = clock()
    token = content_token(entries)
    running = awaiting_reply(entries) or entry_in_progress(entries)
    since = _working_since(previous, token, now) if running else 0
    if transcript.pending_approval(entries) or widgets.latest_widget(entries):
        phase = "blocked"
    elif running and now - since < WORKING_TIMEOUT:
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
        "latest": max(
            (e["timestampMs"] for e in entries if isinstance(e.get("timestampMs"), (int, float))),
            default=0,
        ),
        "token": token,
        "since": since,
    }


def status_label(state, unread=0, printed=False):
    """The list says `working`. Printed `--overview` keeps the old word `running`."""
    if state == "blocked":
        return "needs you"
    if state == "unread":
        count = unread if isinstance(unread, int) else 0
        if count > 1:
            return f"{count} unread"
        return "unread"
    if state == "working":
        return "running" if printed else "working"
    if state == "idle":
        return "idle"
    return ""


def cached_thread_activity(bot):
    """State from the thread already on disk, before the next `gbot thread` returns."""
    name = bot.get("name") or ""
    entries = cache.read_thread_cache(name) if name else []
    if not entries:
        return None
    return judge_activity(entries, transcript.last_id(entries), seen_mark(bot))


def with_activity(bots):
    """Attach a state to each bot. A saved thread counts until the live read lands."""
    views = []
    for bot in bots:
        view = dict(bot)
        snap = lookup_activity(bot)
        if not snap:
            snap = cached_thread_activity(bot)
            if snap:
                store_activity(bot, snap, only_if_absent=True)
                # The scan may have stored a newer state while this was judged.
                snap = lookup_activity(bot) or snap
        with STATE_LOCK:
            server = backend.SERVER.get(bot.get("id") or "")
        if server:
            snap = dict(snap or {"state": "idle", "cursor": "", "preview": ""})
            snap["unread"] = server["unread"]
            snap["latest"] = server["latest"] or snap.get("latest") or 0
            if snap.get("state") != "blocked":
                if server["unread"]:
                    snap["state"] = "unread"
                elif snap.get("state") == "unread":
                    snap["state"] = "idle"
        if snap:
            view["activity"] = snap
        views.append(view)
    return views


def newly_blocked(previous, views):
    """Names that turned `needs you` since `previous` ({name: state}), and the new map.

    A bot first seen in this map is the baseline, so opening the list does not
    ring for every bot that already needs you. Each transition counts once:
    the bot has to leave `needs you` before it can ring again.
    """
    states = {}
    fresh = []
    for bot in views:
        name = bot.get("name") or ""
        state = ((bot.get("activity") or {}).get("state")) or ""
        if not name:
            continue
        states[name] = state
        before = previous.get(name)
        if state == "blocked" and before is not None and before != "blocked":
            fresh.append(name)
    for name, state in previous.items():
        states.setdefault(name, state)
    return fresh, states


def order_bots(bots):
    """Most recent message first. A search does not use this order."""
    ranked = []
    for index, bot in enumerate(bots):
        latest = (bot.get("activity") or {}).get("latest") or 0
        ranked.append((-latest, index, bot))
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


def overview_summary(bots, checked=None, total=None, printed=False):
    """One line: `1 needs you · 2 new · 1 working`, plus a scan hint while reading.

    `printed` is `--overview` on stdout. That line still says `running`.
    """
    counts = attention_counts(bots)
    working = "running" if printed else "working"
    parts = [
        phrase
        for phrase in (
            _count_phrase(counts["blocked"], "1 needs you", "need you"),
            _count_phrase(counts["unread"], "1 unread", "unread"),
            _count_phrase(counts["working"], f"1 {working}", working),
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
    """Stdout for `--overview`. `running` stays the printed word for `working`."""
    ordered = order_bots(bots)
    summary = overview_summary(ordered, printed=True)
    name_width = min(max((len(bot["name"]) for bot in ordered), default=0), 24)
    lines = [summary, ""] if summary else []
    for bot in ordered:
        activity = bot.get("activity") or {}
        label = status_label(activity.get("state") or "", activity.get("unread") or 0, printed=True)
        preview = activity.get("preview") or bot.get("title") or bot.get("tagline") or ""
        preview = " ".join(str(preview).split())
        line = f"{label:<{picker.STATE_COLUMN}}{bot['name']:<{name_width}}"
        if preview:
            line += styles.PREVIEW_MARK + preview
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
    snap = judge_activity(entries, cursor, mark, previous=lookup_activity(bot))
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
        except gbot_cli.GbotError as error:
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
        raise gbot_cli.GbotError("No bot.")
    return gbot_cli.read_thread(name, limit=OVERVIEW_LIMIT, timeout=20)


class Survey:
    """Walk the roster off the UI thread and publish each bot as its read returns."""

    def __init__(self, bots, read=None):
        self._read = read or read_overview
        self._refresh = backend.refresh_server_state if read is None else (lambda _bots: None)
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
            self._refresh(bots)
            with self._lock:
                self.generation += 1
            for bot in bots:
                if self._stop.is_set():
                    return
                with self._lock:
                    if self._restart:
                        break
                try:
                    page = self._read(bot)
                except gbot_cli.GbotError as error:
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
