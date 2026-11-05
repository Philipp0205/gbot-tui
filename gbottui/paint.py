"""Painting spans and lines."""

import curses

from . import activity, grid, markdown, styles, terminal


def title_spans(title, phase, width):
    """The header name, and a cyan `working` while a reply is in progress."""
    room = max(width - 4, 0)
    mark = " · working" if phase == "working" else ""
    if mark and room > len(mark):
        return [(grid.clip(title, room - len(mark)), "bold"), (mark, activity.STATUS_STYLE["working"])]
    return [(grid.clip(title, room), "bold")]


def put(stdscr, y, x, text, attr=0):
    if not text:
        return
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    room = width - x - (1 if y == height - 1 else 0)
    if room <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, room, attr)
    except curses.error:
        pass


def paint_spans(stdscr, y, x, spans):
    col = x
    for text, style in spans:
        put(stdscr, y, col, text, styles.span_attr(style))
        col += len(text)


def transcript_attr(style, base):
    """Bold and italic stay in the bot color. Code, rules, and urls use gray.

    Gray is the readable secondary color. Dimming the bot color itself drops
    it below the contrast the rest of the line was lifted to.
    """
    names = [part for part in (style or "").split("+") if part]
    if "code" in names or "dim" in names:
        attr = styles.ATTR["gray"]
    else:
        attr = base
    if "bold" in names:
        attr |= curses.A_BOLD
    if "em" in names:
        attr |= getattr(curses, "A_ITALIC", 0) or curses.A_UNDERLINE
    if "link" in names:
        attr |= curses.A_UNDERLINE
    return attr


def user_attr(style, base):
    """Styles inside a bubble keep the bubble color, or the fill breaks."""
    attr = base
    names = [part for part in (style or "").split("+") if part]
    if "bold" in names:
        attr |= curses.A_BOLD
    if "em" in names:
        attr |= getattr(curses, "A_ITALIC", 0) or curses.A_UNDERLINE
    if "link" in names or "code" in names:
        attr |= curses.A_UNDERLINE
    if "code" in names or "dim" in names:
        attr |= curses.A_DIM
    return attr


def paint_bot_line(stdscr, y, x, line, base):
    if isinstance(line, str):
        put(stdscr, y, x, line, base)
        return
    col = x
    for text, style in line:
        put(stdscr, y, col, text, transcript_attr(style, base))
        if "link" in style or style == "dim":
            for found in markdown._URL_RE.finditer(text):
                url = found.group(0).rstrip(".,;:!?)]")
                terminal.LINK_HITS.append((y, col + found.start(), col + found.start() + len(url), url))
        col += len(text)


def paint_user_line(stdscr, y, x, line):
    base = styles.ATTR["user"]
    if isinstance(line, str):
        put(stdscr, y, x, " " + line + " ", base)
        return
    put(stdscr, y, x, " ", base)
    col = x + 1
    for text, style in line:
        put(stdscr, y, col, text, user_attr(style, base))
        col += len(text)
    put(stdscr, y, col, " ", base)


def place_cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def fast_escape():
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        return


def read_keys(stdscr):
    """Every key curses has now. A paste is one redraw, not one per character."""
    keys = []
    while len(keys) < 4096:
        try:
            keys.append(stdscr.get_wch())
        except curses.error:
            break
    return keys
