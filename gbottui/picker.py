"""The bot list."""

import curses
import threading

from . import activity, colors, gbot_cli, grid, paint, styles, terminal, theme


# "needs you" and "80 unread" are 9 wide. Two spaces keep the next column off the word.
STATE_COLUMN = 11


def fuzzy_score(query, name):
    """Higher is a tighter match. None means the letters are not there in order."""
    query = query.casefold()
    if not query:
        return 0
    name = name.casefold()
    found = name.find(query)
    if found >= 0:
        return 1000 - found + (50 if found == 0 else 0)
    at = 0
    score = 0
    run = False
    for index, char in enumerate(name):
        if at < len(query) and char == query[at]:
            score += 3 if index == 0 or not name[index - 1].isalnum() else 1
            if run:
                score += 2
            run = True
            at += 1
        else:
            run = False
    if at != len(query):
        return None
    return score


def list_columns(bots, width):
    """Shared state and name widths so every preview starts at the same column.

    A narrow window gives the name less room before it gives up the grid.
    Every named bot is in the grid, so a row still waiting on a read keeps the
    same state column as the ones that already have a word.
    """
    room = max(width - 7, 1)
    overhead = STATE_COLUMN + len(styles.PREVIEW_MARK)
    if room <= overhead + 4:
        return None
    names = [bot["name"] for bot in bots if bot.get("name")]
    if not names:
        return None
    rest = room - overhead
    # 58 columns still keeps a short preview. Below that, keep a few characters.
    preview_floor = 12 if width >= 58 else 4
    name_w = min(max(len(name) for name in names), max(rest - preview_floor, 1))
    if name_w > rest:
        name_w = rest
    if name_w < 1:
        return None
    return {"name": name_w, "state": STATE_COLUMN}


def _pad(text, width):
    if len(text) >= width:
        return text
    return text + (" " * (width - len(text)))


def state_field(label, state_w):
    """The state word plus the gap before the name. A missing state is a dash."""
    text = label or "-"
    if len(text) >= state_w:
        return text + "  "
    return _pad(text, state_w)


def _status_spans(lead, lead_style, mark, name, name_style, snap, title, room, columns=None):
    """State, then name, then the latest line. The state column is what you scan."""
    label = activity.status_label(snap.get("state") or "", snap.get("unread") or 0)
    preview = " ".join((snap.get("preview") or title or "").split())
    state_w = columns["state"] if columns else STATE_COLUMN
    field = state_field(label, state_w)
    style = activity.STATUS_STYLE.get(snap.get("state"), "dim")
    sep = styles.PREVIEW_MARK if preview else ""
    if columns:
        name_w = columns["name"]
        fixed = len(field) + len(sep)
        if name_w + fixed > room:
            name_w = max(room - fixed, 1)
        shown = _pad(grid.clip(name, name_w), name_w)
        preview_room = room - len(field) - len(shown) - len(sep)
        spans = [
            (lead, lead_style),
            mark,
            (field, style),
            (shown, name_style),
        ]
        if preview and preview_room > 0:
            spans.append((sep, "dim"))
            spans.append((grid.clip(preview, preview_room), "dim"))
        return spans
    # A very narrow window keeps the name. The state word needs its column.
    if room < len(field) + 1:
        return [(lead, lead_style), mark, (grid.clip(name, room), name_style)]
    rest = room - len(field)
    name_room = len(name)
    if preview and name_room + len(sep) > rest and rest >= 12:
        name_room = max((rest * 2) // 3, 1)
    if name_room > rest:
        name_room = rest
    preview_room = rest - name_room - len(sep)
    spans = [
        (lead, lead_style),
        mark,
        (field, style),
        (grid.clip(name, name_room), name_style),
    ]
    if preview and preview_room > 0:
        spans.append((sep, "dim"))
        spans.append((grid.clip(preview, preview_room), "dim"))
    return spans


def picker_spans(bot, selected, width, columns=None):
    """One selection row: mark, avatar, state, name, and a preview.

    The state is `needs you`, `unread`, `running`, or `idle`. A row with no
    reading yet holds that column with a dash so the names still line up.
    Without a shared grid, a row that has no state yet keeps the tagline.
    """
    title = (bot.get("title") or bot.get("tagline") or "").strip()
    name = bot["name"]
    name_style = "bold" if selected else "default"
    lead = "→ " if selected else "  "
    lead_style = "bold" if selected else "dim"
    color = (bot.get("avatarColor") or bot.get("avatar_color") or "").lower()
    room = max(width - 7, 1)
    # The same lightened color the chat mark uses, including red and yellow,
    # which are also the names of the warning styles.
    mark_style = "bot:" + color if color in colors.AVATAR_COLORS else (color or "default")
    mark = ("● ", mark_style)
    snap = bot.get("activity") if isinstance(bot.get("activity"), dict) else None
    if snap and snap.get("state") in activity.OVERVIEW_STATES:
        return _status_spans(lead, lead_style, mark, name, name_style, snap, title, room, columns)
    if columns:
        return _status_spans(
            lead,
            lead_style,
            mark,
            name,
            name_style,
            {"state": "", "unread": 0, "preview": title},
            "",
            room,
            columns,
        )
    if not title:
        return [(lead, lead_style), mark, (grid.clip(name, room), name_style)]
    name_room = min(len(name), max(room // 2, 1))
    title_room = max(room - min(len(name), name_room) - 5, 1)
    return [
        (lead, lead_style),
        mark,
        (grid.clip(name, name_room), name_style),
        ("  ·  ", "dim"),
        (grid.clip(title, title_room), "dim"),
    ]


def list_banner(query, summary, width):
    """The line under the title: the search, the counts, or both."""
    room = max(width - 2, 0)
    if not room:
        return []
    if query and summary:
        left = grid.clip("/" + query, max(room // 2, 1))
        rest = max(room - len(left) - 2, 0)
        spans = [(left, "dim")]
        if rest:
            spans.append(("  ", "dim"))
            spans.append((grid.clip(summary, rest), "dim"))
        return spans
    if query:
        return [(grid.clip("/" + query, room), "dim")]
    if summary:
        return [(grid.clip(summary, room), "dim")]
    return []


def preview_score(query, text):
    """A preview matches only as a contiguous substring, not scattered letters.

    A long reply is full of letters. Fuzzy order through it pulled in most of
    the list for a short query. A word start ranks above a match mid-word, and
    both stay under a real name match.
    """
    query = query.casefold()
    if not query:
        return 0
    hay = str(text).casefold()
    found = hay.find(query)
    if found < 0:
        return None
    word = found == 0 or not hay[found - 1].isalnum()
    return 800 - min(found, 700) + (40 if word else 0)


def fuzzy_bots(query, bots):
    if not query:
        return list(bots)
    ranked = []
    for index, bot in enumerate(bots):
        scores = [fuzzy_score(query, bot.get("name") or "")]
        title = bot.get("title") or ""
        if title:
            scores.append(fuzzy_score(query, title))
        preview = ((bot.get("activity") or {}).get("preview")) or ""
        if preview:
            scores.append(preview_score(query, preview))
        best = max((score for score in scores if score is not None), default=None)
        if best is not None:
            ranked.append((-best, index, bot))
    ranked.sort()
    return [bot for _score, _index, bot in ranked]


class Loader:
    """Run one gbot call on a thread. The UI polls `ready` instead of waiting."""

    def __init__(self, call):
        self.result = None
        self.error = None
        self.ready = False
        self.lock = threading.Lock()

        def run():
            try:
                value = call()
                with self.lock:
                    self.result = value
            except gbot_cli.GbotError as error:
                with self.lock:
                    self.error = str(error)
            with self.lock:
                self.ready = True

        threading.Thread(target=run, daemon=True).start()

    def take(self):
        with self.lock:
            return self.ready, self.result, self.error


LIST_HINT = "type to search · enter open · ctrl-r refresh · ctrl-c quit · ? keys"
LIST_HELP = [
    ("type", "search names, titles and previews"),
    ("backspace", "delete a search letter"),
    ("up down / wheel", "move the highlight"),
    ("enter", "open the bot"),
    ("esc", "clear the search"),
    ("ctrl-r", "scan every bot again"),
    ("ctrl-c ctrl-d", "quit"),
    ("? f1", "this help (? types when searching)"),
]


def opens_help(key, query):
    """F1, or `?` while the search is empty. A `?` in a search is a letter."""
    return key == curses.KEY_F1 or (key == "?" and not query)


def paint_list_help(stdscr, top, room, width):
    """The list keys in place of the rows. Any key closes it."""
    lines = ["Keys  (any key closes)"] + [f"{keys:<18}{what}" for keys, what in LIST_HELP]
    for index, line in enumerate(lines[:room]):
        attr = styles.ATTR["bold"] if index == 0 else styles.ATTR["default"]
        paint.put(stdscr, top + index, 3, grid.clip(line, max(width - 4, 0)), attr)


def pick(stdscr, bots, refresh=None):
    """The bot list, with who needs you, who is working, and who has a new reply.

    `refresh` fetches a newer roster on a thread. A survey reads each thread
    beside that. Until you move, the highlight stays on the top row, which is
    the bot that needs attention. A search keeps match order.
    """
    # The chat loop reads keys without waiting. Leaving that on makes get_wch
    # raise "no input" the moment this list draws.
    stdscr.timeout(-1)
    styles.apply_theme(stdscr)
    paint.fast_escape()
    paint.place_cursor(False)
    stdscr.keypad(True)
    terminal.enable_wheel()
    bots = list(bots)
    loader = Loader(refresh) if refresh else None
    survey = activity.Survey(bots)
    note = ""
    selected = 0
    query = ""
    # False until Up or Down. The top row then stays the one that needs attention.
    moved = False
    chosen = ""
    pending = []
    showing_help = False
    try:
        while True:
            if loader:
                ready, fresh, error = loader.take()
                if ready:
                    loader = None
                    if error and not bots:
                        raise gbot_cli.GbotError(error)
                    if error:
                        note = error
                    elif fresh is not None:
                        if not fresh and not bots:
                            raise gbot_cli.GbotError("No bots.")
                        bots = list(fresh)
                        survey.replace(bots)
            views = activity.with_activity(bots)
            shown = fuzzy_bots(query, views)
            if not query:
                shown = activity.order_bots(shown)
            if moved and chosen:
                names = [bot["name"] for bot in shown]
                if chosen in names:
                    selected = names.index(chosen)
                else:
                    moved = False
                    chosen = ""
            if not moved:
                selected = 0
            if not shown:
                selected = 0
            elif selected >= len(shown):
                selected = len(shown) - 1
            running, checked, total, _generation = survey.status()
            if running:
                summary = activity.overview_summary(shown, checked, total)
            else:
                summary = activity.overview_summary(shown)
            problem = survey.problem()
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            paint.put(stdscr, 0, 1, "Grok bots", styles.ATTR["bold"])
            banner = list_banner(query, summary, width)
            if banner:
                paint.paint_spans(stdscr, 1, 1, banner)
            footer = 1 if height >= 4 else 0
            body_h = max(height - 2 - footer, 0)
            top = 0
            if body_h and selected >= body_h:
                top = selected - body_h + 1
            if showing_help:
                paint_list_help(stdscr, 2, body_h, width)
            elif not shown and body_h:
                empty = "  loading bots…" if loader and not bots else "  no match"
                paint.put(stdscr, 2, 1, grid.clip(empty, max(width - 2, 0)), styles.ATTR["dim"])
            columns = list_columns(shown, width)
            for index, bot in enumerate([] if showing_help else shown[top : top + body_h]):
                actual = top + index
                paint.paint_spans(stdscr, 2 + index, 1, picker_spans(bot, actual == selected, width, columns))
            if footer:
                if note:
                    spans = [(grid.clip(note, max(width - 2, 0)), "red")]
                elif problem:
                    spans = [(grid.clip(problem, max(width - 2, 0)), "red")]
                else:
                    spans = [(LIST_HINT, "dim")]
                paint.paint_spans(stdscr, height - 1, 1, spans)
            stdscr.refresh()
            due = theme.theme_due_ms()
            wait = 30 if loader else (150 if running else 400)
            if due is not None:
                wait = min(wait, due)
            if pending:
                key = pending.pop(0)
            else:
                stdscr.timeout(wait)
                try:
                    key = stdscr.get_wch()
                except curses.error:
                    if theme.maybe_retheme(lambda: styles.apply_theme(stdscr)):
                        pass
                    continue
                if paint.is_escape(key):
                    # ESC [ D from a terminal not in keypad mode is an arrow, not Esc.
                    pending = paint.decode_keys([key], paint.follow_up(stdscr, wait))
                    continue
            if key == curses.KEY_RESIZE:
                theme.note_appearance()
                theme.maybe_retheme(lambda: styles.apply_theme(stdscr))
                continue
            if key in ("\x03", "\x04"):
                return None
            if showing_help:
                # Any other key, Esc included, only closes the help.
                showing_help = False
                continue
            if opens_help(key, query):
                showing_help = True
                continue
            if key == "\x12":
                survey.nudge()
                continue
            if key in ("\x1b", 27):
                query = ""
                selected = 0
                moved = False
                chosen = ""
                continue
            wheel = terminal.wheel_direction(key)
            if wheel is not None and shown:
                selected = min(max(selected + wheel, 0), len(shown) - 1)
                moved = True
                chosen = shown[selected]["name"]
                continue
            if key == curses.KEY_UP and selected > 0:
                selected -= 1
                moved = True
                chosen = shown[selected]["name"] if shown else ""
                continue
            if key == curses.KEY_DOWN and selected < len(shown) - 1:
                selected += 1
                moved = True
                chosen = shown[selected]["name"] if shown else ""
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                if shown:
                    return shown[selected]["name"]
                continue
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query = query[:-1]
                selected = 0
                moved = False
                chosen = ""
                continue
            if isinstance(key, str) and key.isprintable():
                query += key
                selected = 0
                moved = False
                chosen = ""
    finally:
        survey.close()
