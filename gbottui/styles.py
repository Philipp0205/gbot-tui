"""Curses attributes for the named text styles and each bot's color."""

import curses

from .colors import AVATAR_COLORS, bubble_fg, readable_hex, xterm_color
from .theme import background_hex, dark_terminal, user_bubble

# -1 keeps the terminal background. A filled curses color is the block this CLI does not use.
ATTR = {
    "default": 0,
    "dim": curses.A_DIM,
    "bold": curses.A_BOLD,
    "user": curses.A_BOLD,
    "yellow": curses.A_BOLD,
    "red": curses.A_BOLD,
    # Calmer than the pending warning. Theme replaces these with real colors.
    "green": curses.A_DIM,
    "gray": curses.A_DIM,
}
CARD_STYLE = {"card": "yellow", "card-ok": "green", "card-no": "gray"}
# The avatar color as drawn in icons, and the same color made readable as text.
AVATAR_ATTR = {}
TEXT_ATTR = {}


def theme_palette(dark):
    """256-color indices for the named styles. Yellow on white is unreadable."""
    if dark:
        return {"green": 108, "gray": 245}
    return {"yellow": 130, "red": 124, "green": 28, "gray": 240}


def apply_theme(stdscr):
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, -1, -1)
        base = curses.color_pair(1)
        stdscr.bkgd(" ", base)
        ATTR.update(default=base, dim=base | curses.A_DIM, bold=base | curses.A_BOLD)
        AVATAR_ATTR.clear()
        TEXT_ATTR.clear()
        wide = curses.COLORS >= 256
        if wide:
            bubble = user_bubble()
            curses.init_pair(2, bubble_fg(bubble), bubble)
            ATTR["user"] = curses.color_pair(2)
            dark = dark_terminal()
            background = background_hex()
            for index, (name, hex_color) in enumerate(AVATAR_COLORS.items()):
                curses.init_pair(10 + index, xterm_color(hex_color), -1)
                AVATAR_ATTR[name] = curses.color_pair(10 + index)
                curses.init_pair(30 + index, xterm_color(readable_hex(hex_color, dark, background)), -1)
                TEXT_ATTR[name] = curses.color_pair(30 + index)
        else:
            ATTR["user"] = ATTR["bold"]
        palette = theme_palette(dark_terminal()) if wide else {}
        for pair, name, fallback in (
            (3, "yellow", curses.COLOR_YELLOW),
            (4, "red", curses.COLOR_RED),
            (5, "green", curses.COLOR_GREEN),
            (6, "gray", curses.COLOR_WHITE),
        ):
            curses.init_pair(pair, palette.get(name, fallback), -1)
            ATTR[name] = curses.color_pair(pair)
        if not wide:
            # Eight colors have no mid gray.
            ATTR["gray"] |= curses.A_DIM
    except curses.error:
        return


def avatar_attr(name):
    return AVATAR_ATTR.get((name or "").lower(), ATTR["default"])


def bot_text_attr(name):
    """The bot's color, lightened or darkened until it reads on this background.

    Black is #000000. On a dark terminal that raw color is the background, so
    the list dot and the chat mark use the same lifted gray as the other colors.
    """
    folded = (name or "").lower()
    if folded in TEXT_ATTR:
        return TEXT_ATTR[folded]
    return avatar_attr(name)


def dot_attr(name):
    """The bot mark in the list and in the chat. One lightened color for both."""
    return bot_text_attr(name)


def span_attr(style):
    """Named text styles stay named. A bot color uses a `bot:` prefix.

    "red" and "yellow" are both avatar colors and the warning styles. The
    footer asks for the warning style by those names. The bot list asks for
    the avatar color as `bot:red`, which is the lightened bot color.
    """
    if isinstance(style, str) and style.startswith("bot:"):
        return bot_text_attr(style[4:])
    if style in ATTR:
        return ATTR[style]
    return avatar_attr(style)
