"""Drawing one chat."""

import curses

from . import grid, paint, styles, transcript, widgets


def paint_composer(stdscr, y, width, lines, placeholder=False):
    span = max(width, 0)
    if span < 2:
        return
    inner = span - 2
    attr = styles.ATTR["dim"] if placeholder else styles.ATTR["default"]
    for index, line in enumerate(lines):
        paint.put(stdscr, y + index, 0, "│", styles.ATTR["default"])
        prefix = " →  " if index == 0 else "    "
        paint.put(stdscr, y + index, 1, (prefix + line)[:inner], attr)
        paint.put(stdscr, y + index, span - 1, "│", styles.ATTR["default"])


def draw(stdscr, chat):
    height, width = stdscr.getmaxyx()
    # erase() only diffs against the last frame. Inserting the local reply
    # shifts every line; Herdr can leave a ghost of the old ones unless the
    # next refresh clears the physical screen first.
    rows_now = grid.visible_rows(chat, width)
    stamp = (len(rows_now), width, height)
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
    lines = grid.draft_lines(chat.typed, width)
    header, body_h, composer_h, footer_h = grid.layout(height, len(lines) or 1)
    footer = []
    if footer_h:
        decide = bool(transcript.latest_decision(chat.messages, chat.answered)) and not chat.deciding
        hint = ""
        if getattr(chat, "loaded", False) and not getattr(chat, "widget_busy", False) and not chat.typed:
            entry = widgets.latest_widget(chat.messages)
            hint = widgets.choice_hint(entry, chat.step_at(entry) if entry else 0)
        footer = grid.footer_rows(chat.phase, chat.error, decide=decide, width=width, choice=hint)
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
    if chat.follow:
        chat.scroll = max(len(rows) - room, 0)
    start = min(chat.scroll, max(len(rows) - room, 0))
    for row in range(room):
        index = start + row
        if index >= len(rows):
            continue
        kind, text = rows[index]
        y = header + gap + row
        previous = rows[index - 1][0] if index else ""
        if kind == "user":
            paint.paint_user_line(stdscr, y, grid.bubble_column(width, text), text)
        elif kind == "text":
            if previous != "text":
                paint.put(stdscr, y, 1, "●", styles.dot_attr(color))
            paint.paint_bot_line(stdscr, y, 3, text, styles.bot_text_attr(color))
        elif kind in styles.CARD_STYLE:
            paint.put(stdscr, y, 1, grid.clip(text, max(width - 2, 0)), styles.ATTR[styles.CARD_STYLE[kind]])
    composer_at = header + body_h
    cursor = None
    if composer_h >= 3:
        top, _mid, bot = grid.composer_lines(width, chat.typed)
        paint.put(stdscr, composer_at, 0, top, styles.ATTR["default"])
        visible = composer_h - 2
        shown = lines[-visible:] if lines else ["Add a follow-up"]
        paint_composer(stdscr, composer_at + 1, width, shown, placeholder=not lines)
        paint.put(stdscr, composer_at + composer_h - 1, 0, bot, styles.ATTR["default"])
        last = "" if not lines else shown[-1]
        column = grid.composer_cursor_column(last, placeholder=not lines)
        cursor = (composer_at + len(shown), min(column, max(width - 2, 0)))
    elif composer_h == 1:
        shown = chat.typed or "Add a follow-up"
        paint.put(
            stdscr,
            composer_at,
            0,
            grid.clip("→  " + shown, max(width - 1, 0)),
            styles.ATTR["default"] if chat.typed else styles.ATTR["dim"],
        )
        column = 2 if not chat.typed else 3 + len(chat.typed)
        cursor = (composer_at, min(column, max(width - 2, 0)))
    for offset, spans in enumerate(footer):
        paint.paint_spans(stdscr, height - len(footer) + offset, 1, spans)
    # The footer write leaves the cursor on "scroll". Put it back in the box.
    if cursor:
        try:
            stdscr.move(*cursor)
        except curses.error:
            pass
    stdscr.refresh()
