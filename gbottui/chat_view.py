"""Draws one chat: header, transcript, composer box, and footer."""

import curses

from .layout import (
    PLACEHOLDER,
    bubble_column,
    clip,
    composer_cursor_column,
    composer_lines,
    draft_lines,
    footer_rows,
    header_gap,
    layout,
    visible_rows,
)
from .paint import paint_spans, put
from .styles import ATTR, CARD_STYLE, bot_text_attr
from .transcript import latest_decision


def paint_composer(stdscr, y, width, lines, placeholder=False):
    span = max(width, 0)
    if span < 2:
        return
    inner = span - 2
    attr = ATTR["dim"] if placeholder else ATTR["default"]
    for index, line in enumerate(lines):
        put(stdscr, y + index, 0, "│", ATTR["default"])
        prefix = " →  " if index == 0 else "    "
        put(stdscr, y + index, 1, (prefix + line)[:inner], attr)
        put(stdscr, y + index, span - 1, "│", ATTR["default"])


def draw(stdscr, chat):
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    lines = draft_lines(chat.typed, width)
    header, body_h, composer_h, footer_h = layout(height, len(lines) or 1)
    footer = []
    if footer_h:
        decide = bool(latest_decision(chat.messages, chat.answered)) and not chat.deciding
        footer = footer_rows(chat.phase, chat.error, decide=decide, width=width)
        # Error rows come out of the transcript, which keeps at least one row.
        footer = footer[-max(min(len(footer), body_h), 1) :]
        body_h -= len(footer) - 1
    color = chat.avatar_color
    if header:
        put(stdscr, 0, 1, "●", bot_text_attr(color))
        put(stdscr, 0, 3, clip(chat.title, max(width - 4, 0)), ATTR["bold"])
        if header > 1 and chat.label:
            put(stdscr, 1, 3, clip(chat.label, max(width - 4, 0)), ATTR["dim"])
    gap = header_gap(header, chat.label, body_h)
    room = max(body_h - gap, 0)
    rows = visible_rows(chat, width)
    if chat.follow:
        chat.scroll = max(len(rows) - room, 0)
    start = min(chat.scroll, max(len(rows) - room, 0))
    for row, (kind, text) in enumerate(rows[start : start + room]):
        index = start + row
        y = header + gap + row
        previous = rows[index - 1][0] if index else ""
        if kind == "user":
            put(stdscr, y, bubble_column(width, text), " " + text + " ", ATTR["user"])
        elif kind == "text":
            if previous != "text":
                put(stdscr, y, 1, "●", bot_text_attr(color))
            put(stdscr, y, 3, text, bot_text_attr(color))
        elif kind in CARD_STYLE:
            put(stdscr, y, 1, clip(text, max(width - 2, 0)), ATTR[CARD_STYLE[kind]])
    composer_at = header + body_h
    cursor = None
    if composer_h >= 3:
        top, _mid, bot = composer_lines(width, chat.typed)
        put(stdscr, composer_at, 0, top, ATTR["default"])
        visible = composer_h - 2
        shown = lines[-visible:] if lines else [PLACEHOLDER]
        paint_composer(stdscr, composer_at + 1, width, shown, placeholder=not lines)
        put(stdscr, composer_at + composer_h - 1, 0, bot, ATTR["default"])
        last = "" if not lines else shown[-1]
        column = composer_cursor_column(last, placeholder=not lines)
        cursor = (composer_at + len(shown), min(column, max(width - 2, 0)))
    elif composer_h == 1:
        shown = chat.typed or PLACEHOLDER
        put(
            stdscr,
            composer_at,
            0,
            clip("→  " + shown, max(width - 1, 0)),
            ATTR["default"] if chat.typed else ATTR["dim"],
        )
        column = 2 if not chat.typed else 3 + len(chat.typed)
        cursor = (composer_at, min(column, max(width - 2, 0)))
    for offset, spans in enumerate(footer):
        paint_spans(stdscr, height - len(footer) + offset, 1, spans)
    # The footer write leaves the cursor on "scroll". Put it back in the box.
    if cursor:
        try:
            stdscr.move(*cursor)
        except curses.error:
            pass
    stdscr.refresh()
