"""Style names and curses attributes."""

import curses

from . import colors, theme


# Sits between the name and the latest line, the same mark the tagline uses.
PREVIEW_MARK = " · "


def theme_palette(dark):
    """256-color indices for the named styles. Yellow on white is unreadable.

    Cyan is the `working` mark, so it stays off the yellow used for needs-you.
    """
    if dark:
        return {"green": 108, "gray": 245, "cyan": 44}
    return {"yellow": 130, "red": 124, "green": 28, "gray": 240, "cyan": 23}


AVATAR_ATTR = {}


def avatar_attr(name):
    return AVATAR_ATTR.get((name or "").lower(), ATTR["default"])


TEXT_ATTR = {}


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
    "cyan": curses.A_BOLD,
}

CARD_STYLE = {"card": "yellow", "card-ok": "green", "card-no": "gray"}


def apply_theme(stdscr):
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, -1, -1)
        base = curses.color_pair(1)
        stdscr.bkgd(" ", base)
        ATTR["default"] = base
        ATTR["dim"] = base | curses.A_DIM
        ATTR["bold"] = base | curses.A_BOLD
        if curses.COLORS >= 256:
            bubble = theme.user_bubble()
            curses.init_pair(2, colors.bubble_fg(bubble), bubble)
            ATTR["user"] = curses.color_pair(2)
            AVATAR_ATTR.clear()
            TEXT_ATTR.clear()
            dark = theme.dark_terminal()
            background = theme.background_hex()
            for index, (name, hex_color) in enumerate(colors.AVATAR_COLORS.items()):
                curses.init_pair(10 + index, colors.xterm_color(hex_color), -1)
                AVATAR_ATTR[name] = curses.color_pair(10 + index)
                curses.init_pair(30 + index, colors.xterm_color(colors.readable_hex(hex_color, dark, background)), -1)
                TEXT_ATTR[name] = curses.color_pair(30 + index)
        else:
            ATTR["user"] = ATTR["bold"]
            AVATAR_ATTR.clear()
            TEXT_ATTR.clear()
        palette = theme_palette(theme.dark_terminal()) if curses.COLORS >= 256 else {}
        curses.init_pair(3, palette.get("yellow", curses.COLOR_YELLOW), -1)
        curses.init_pair(4, palette.get("red", curses.COLOR_RED), -1)
        curses.init_pair(7, palette.get("cyan", curses.COLOR_CYAN), -1)
        ATTR["yellow"] = curses.color_pair(3)
        ATTR["red"] = curses.color_pair(4)
        ATTR["cyan"] = curses.color_pair(7)
        if curses.COLORS >= 256:
            curses.init_pair(5, palette["green"], -1)
            curses.init_pair(6, palette["gray"], -1)
            ATTR["green"] = curses.color_pair(5)
            ATTR["gray"] = curses.color_pair(6)
        else:
            curses.init_pair(5, curses.COLOR_GREEN, -1)
            curses.init_pair(6, curses.COLOR_WHITE, -1)
            ATTR["green"] = curses.color_pair(5)
            ATTR["gray"] = curses.color_pair(6) | curses.A_DIM
    except curses.error:
        return


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
