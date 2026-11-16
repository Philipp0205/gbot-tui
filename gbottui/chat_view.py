"""Drawing one chat."""

import curses

from . import colors, grid, markdown, paint, roster, styles, terminal, transcript, widgets

LOADING = "Loading conversation…"
HELP = [
    ("enter", "send"),
    ("ctrl-j / ctrl-enter", "new line"),
    ("left right home end", "move in the draft"),
    ("ctrl/alt-left right", "jump a word"),
    ("ctrl-a ctrl-e", "line start / end"),
    ("ctrl-w ctrl-u ctrl-k", "delete word / to start / to end"),
    ("up down", "draft rows, then sent history"),
    ("pgup pgdn", "scroll a screen (alt: half)"),
    ("ctrl-f", "search (enter/ctrl-n older, ctrl-p newer, esc)"),
    ("ctrl-o", "copy the last reply (OSC 52)"),
    ("y n", "answer a pending approval"),
    ("1-9", "pick a widget option"),
    ("left right [ ]", "page back and forth through several questions"),
    ("@bot text", "send to another bot"),
    ("@", "tag a bot: up/down, tab or enter inserts, esc closes"),
    ("esc", "back to the bot list"),
    ("ctrl-c ctrl-d", "quit"),
    ("? f1", "this help (?? types a ?)"),
    ("f2", "mouse on/off: off lets a plain drag select text"),
    ("shift+drag", "select text while the mouse is on (most terminals)"),
]


def paint_help(stdscr, top, room, width):
    """The key list over the transcript. Any key closes it."""
    lines = ["Keys  (any key closes)"] + [f"{keys:<22}{what}" for keys, what in HELP]
    for index, line in enumerate(lines[:room]):
        attr = styles.ATTR["bold"] if index == 0 else styles.ATTR["default"]
        paint.put(stdscr, top + index, 3, grid.clip(line, max(width - 4, 0)), attr)


def paint_matches(stdscr, y, x, text, query, current):
    """Reverse video over each match in one painted row."""
    plain = markdown.line_plain(text)
    attr = curses.A_REVERSE | (curses.A_BOLD if current else 0)
    for start, end in grid.match_spans(plain, query):
        paint.put(stdscr, y, x + start, plain[start:end], attr)


def search_target(rows, chat):
    """Row index of the current match, or None."""
    hits = grid.search_hits(rows, chat.search)
    if not hits:
        return None, hits
    at = len(hits) - 1 if chat.search_at is None else min(chat.search_at, len(hits) - 1)
    return hits[at], hits


def paint_composer(stdscr, y, width, lines, placeholder=False, first=0, border=None):
    span = max(width, 0)
    if span < 2:
        return
    inner = span - 2
    border = styles.ATTR["default"] if border is None else border
    attr = styles.ATTR["dim"] if placeholder else styles.ATTR["default"]
    for index, line in enumerate(lines):
        paint.put(stdscr, y + index, 0, "│", border)
        prefix = " →  " if index + first == 0 else "    "
        paint.put(stdscr, y + index, 1, (prefix + line)[:inner], attr)
        if index + first == 0 and inner >= 2:
            paint.put(stdscr, y + index, 2, "→", border)
        paint.put(stdscr, y + index, span - 1, "│", border)


def paint_mentions(stdscr, chat, bottom, top, width):
    """The @-mention list in the rows just above the composer box (`bottom` is its top row)."""
    if not getattr(chat, "mention_open", False) or getattr(chat, "help", False):
        return
    matches = chat.mention_matches()
    shown = matches[: max(min(len(matches), bottom - top), 0)]
    if not shown:
        return
    span = min(max(len(name) for name, _at in shown) + 4, max(width - 2, 0))
    if span < 5:
        return
    selected = min(getattr(chat, "mention_sel", 0), len(shown) - 1)
    roster_now = roster.ROSTER
    for index, (name, positions) in enumerate(shown):
        y = bottom - len(shown) + index
        base = curses.A_REVERSE if index == selected else 0
        paint.put(stdscr, y, 1, " " * span, styles.ATTR["default"] | base)
        dot = styles.dot_attr(colors.bot_color(roster_now.get(name) or {}))
        paint.put(stdscr, y, 2, "●", dot | base)
        room = span - 4
        shown_name = name[:room]
        at = 0
        while at < len(shown_name):
            bold = at in positions
            end = at
            while end < len(shown_name) and (end in positions) == bold:
                end += 1
            attr = styles.ATTR["default"] | base | (curses.A_BOLD if bold else 0)
            paint.put(stdscr, y, 4 + at, shown_name[at:end], attr)
            at = end


def draw(stdscr, chat):
    height, width = stdscr.getmaxyx()
    # erase() only diffs against the last frame. Inserting the local reply
    # shifts every line; Herdr can leave a ghost of the old ones unless the
    # next refresh clears the physical screen first.
    rows_now = grid.visible_rows(chat, width)
    lines = grid.draft_lines(chat.typed, width)
    # A taller box shifts the transcript the same way.
    stamp = (len(rows_now), width, height, len(lines))
    if stamp != getattr(chat, "_drawn_stamp", None):
        try:
            stdscr.clearok(True)
        except (AttributeError, curses.error):
            pass
        chat._drawn_stamp = stamp
    try:
        stdscr.erase()
    except (AttributeError, curses.error):
        pass
    terminal.LINK_HITS.clear()
    header, body_h, composer_h, footer_h = grid.layout(height, len(lines) or 1)
    footer = []
    if footer_h:
        decide = bool(transcript.latest_decision(chat.messages, chat.answered)) and not chat.deciding
        hint = ""
        if getattr(chat, "loaded", False) and not getattr(chat, "widget_busy", False) and not chat.typed:
            hint = widgets.page_hint(chat.page())
        error = chat.footer_error() if hasattr(chat, "footer_error") else chat.error
        footer = grid.footer_rows(
            chat.phase, error, decide=decide, width=width, choice=hint, notice=getattr(chat, "notice", "")
        )
        if getattr(chat, "search", None) is not None:
            target, hits = search_target(grid.visible_rows(chat, width), chat)
            at = hits.index(target) if target is not None else None
            footer = [grid.search_spans(chat.search, hits, at, width)]
        elif footer:
            footer[-1] = terminal.with_select_hint(footer[-1])
        # Error rows come out of the transcript, which keeps at least one row.
        footer = footer[-max(min(len(footer), body_h), 1) :]
        body_h -= len(footer) - 1
    color = getattr(chat, "avatar_color", "")
    label = getattr(chat, "label", "")
    if header:
        paint.put(stdscr, 0, 1, "●", styles.dot_attr(color))
        paint.paint_spans(stdscr, 0, 3, paint.title_spans(chat.title, chat.phase, width))
        if header > 1 and label:
            paint.put(stdscr, 1, 3, grid.clip(label, max(width - 4, 0)), styles.ATTR["dim"])
    gap = grid.header_gap(header, label, body_h)
    room = max(body_h - gap, 0)
    rows = grid.visible_rows(chat, width)
    searching = getattr(chat, "search", None) is not None
    target, _hits = search_target(rows, chat) if searching else (None, [])
    if searching and getattr(chat, "search_jump", False) and target is not None:
        # Put the match a third of the way down, so the lines under it show.
        chat.follow = False
        chat.scroll = min(max(target - room // 3, 0), max(len(rows) - room, 0))
        chat.search_jump = False
    if chat.follow:
        chat.scroll = max(len(rows) - room, 0)
    start = min(chat.scroll, max(len(rows) - room, 0))
    if not rows and room and not getattr(chat, "loaded", True) and not chat.error:
        # First open with no cached thread: say the read is out, not an empty chat.
        paint.put(stdscr, header + gap, 3, grid.clip(LOADING, max(width - 4, 0)), styles.ATTR["dim"])
    if getattr(chat, "help", False):
        paint_help(stdscr, header + gap, room, width)
        rows = []
    for row in range(room):
        index = start + row
        if index >= len(rows):
            continue
        kind, text = rows[index]
        y = header + gap + row
        previous = rows[index - 1][0] if index else ""
        x = None
        if kind == "user":
            paint.paint_user_line(stdscr, y, grid.bubble_column(width, text), text)
            x = grid.bubble_column(width, text) + 1
        elif kind == "text":
            if previous != "text":
                paint.put(stdscr, y, 1, "●", styles.dot_attr(color))
            paint.paint_bot_line(stdscr, y, 3, text, styles.bot_text_attr(color))
            x = 3
        elif kind in styles.CARD_STYLE:
            url = getattr(text, "url", "")
            text = grid.clip(text, max(width - 2, 0))
            attr = styles.ATTR[styles.CARD_STYLE[kind]]
            if url:
                # A page card opens its canvas like a link in the text does.
                attr |= curses.A_UNDERLINE
                terminal.LINK_HITS.append((y, 1, 1 + paint.cell_width(text), url))
            paint.put(stdscr, y, 1, text, attr)
            x = 1
        if searching and chat.search and x is not None:
            paint_matches(stdscr, y, x, text, chat.search, index == target)
    composer_at = header + body_h
    paint_mentions(stdscr, chat, composer_at, header + gap, width)
    cursor = None
    if composer_h >= 3:
        top, _mid, bot = grid.composer_lines(width, chat.typed)
        border = styles.dot_attr(color)
        paint.put(stdscr, composer_at, 0, top, border)
        visible = composer_h - 2
        row, col = grid.draft_cursor(chat.typed, getattr(chat, "pos", None), width)
        first = grid.composer_window(len(lines), row, visible)
        shown = lines[first : first + visible] if lines else ["Add a follow-up"]
        paint_composer(stdscr, composer_at + 1, width, shown, placeholder=not lines, first=first, border=border)
        paint.put(stdscr, composer_at + composer_h - 1, 0, bot, border)
        column = grid.composer_cursor_column("", placeholder=True) if not lines else 5 + col
        cursor = (composer_at + 1 + (row - first if lines else 0), min(column, max(width - 2, 0)))
    elif composer_h == 1:
        shown = chat.typed or "Add a follow-up"
        paint.put(
            stdscr,
            composer_at,
            0,
            grid.clip("→  " + shown, max(width - 1, 0)),
            styles.ATTR["default"] if chat.typed else styles.ATTR["dim"],
        )
        paint.put(stdscr, composer_at, 0, "→", styles.dot_attr(color))
        pos = len(chat.typed) if getattr(chat, "pos", None) is None else min(chat.pos, len(chat.typed))
        column = 2 if not chat.typed else 3 + pos
        cursor = (composer_at, min(column, max(width - 2, 0)))
    for offset, spans in enumerate(footer):
        paint.paint_spans(stdscr, height - len(footer) + offset, 1, spans)
    if footer and getattr(chat, "search", None) is not None:
        # The prompt has the cursor while the search is open.
        cursor = (height - 1, min(1 + len("find: ") + len(chat.search), max(width - 2, 0)))
    # The footer write leaves the cursor on "scroll". Put it back in the box.
    if cursor:
        try:
            stdscr.move(*cursor)
        except curses.error:
            pass
    stdscr.refresh()
