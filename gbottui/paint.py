"""Curses drawing shared by the bot list and the chat."""

import curses

from .styles import span_attr


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
        put(stdscr, y, col, text, span_attr(style))
        col += len(text)


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
