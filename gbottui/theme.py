"""Light or dark: GBOT_TUI_THEME, then an OSC 11 probe of the terminal, then COLORFGBG."""

import os
import re
import select
import termios
import time

from . import herdr
from .colors import hex_rgb, is_light, nearest_gray

THEME_ENV = "GBOT_TUI_THEME"
# The background the terminal reported to an OSC 11 query, as "#RRGGBB".
PROBED = {"bg": None}
DA1_REPLY = re.compile(rb"\033\[\?[0-9;]*c")
# Herdr paints #282C34 for about two seconds after a tab opens, then reports
# the real background. A fresh light tab otherwise looks dark.
HERDR_DEFAULT_BG = "#282C34"
THEME_LIMIT = 5
THEME_GAP = 0.5
THEME = {"watch": False, "tries": 0, "due": 0.0, "follow": False}


def parse_osc11(data):
    """The color in an OSC 11 reply ("rgb:RRRR/GGGG/BBBB" or "#RRGGBB"), or None."""
    if isinstance(data, bytes):
        data = data.decode("latin-1")
    start = data.find("\033]11;")
    if start < 0:
        return None
    body = data[start + 5 :]
    end = min((i for i in (body.find("\007"), body.find("\033\\")) if i >= 0), default=-1)
    if end < 0:
        return None
    value = body[:end].strip()
    if value.startswith("#") and len(value) == 7:
        channels = [value[1:3], value[3:5], value[5:7]]
    elif value.startswith(("rgb:", "rgba:")):
        channels = value.split(":", 1)[1].split("/")[:3]
    else:
        return None
    if len(channels) != 3:
        return None
    out = []
    for channel in channels:
        if not channel or len(channel) > 4 or any(ch not in "0123456789abcdefABCDEF" for ch in channel):
            return None
        out.append(round(int(channel, 16) * 255 / (16 ** len(channel) - 1)))
    return "#{:02X}{:02X}{:02X}".format(*out)


def fold_override(value):
    folded = (value or "").strip().lower()
    return folded if folded in {"dark", "light"} else ""


def theme_override():
    return fold_override(os.environ.get(THEME_ENV))


def theme_choice(override, probed, colorfgbg):
    """("dark" | "light", where it came from). First of: GBOT_TUI_THEME, OSC 11, COLORFGBG."""
    folded = fold_override(override)
    if folded:
        return folded, THEME_ENV
    if probed:
        return ("light" if is_light(probed) else "dark"), "osc11"
    # COLORFGBG is "fg;bg". 7 and 15 are the light backgrounds.
    bg = (colorfgbg or "").split(";")[-1]
    if bg.isdigit():
        return ("light" if int(bg) in {7, 15} else "dark"), "COLORFGBG"
    return "dark", "default"


def dark_terminal():
    choice, _source = theme_choice(os.environ.get(THEME_ENV), PROBED["bg"], os.environ.get("COLORFGBG"))
    return choice == "dark"


def probe_background(fd=None, timeout=0.2):
    """Ask the terminal for its background with OSC 11.

    A device-attributes query (DA1) goes right after it. Terminals answer in
    order and nearly all answer DA1, so its reply means OSC 11 is not coming
    and the wait ends early. Runs before curses, so the replies are not read
    as keys.
    """
    owned = fd is None
    if owned:
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError:
            return None
    try:
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return None
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, raw)
            os.write(fd, b"\033]11;?\033\\\033[c")
            data = b""
            deadline = time.monotonic() + timeout
            while not DA1_REPLY.search(data):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _w, _x = select.select([fd], [], [], left)
                if not ready:
                    break
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                data += chunk
            return parse_osc11(data)
        except OSError:
            return None
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except termios.error:
                pass
    finally:
        if owned:
            os.close(fd)


def placeholder_background(color):
    return (color or "").upper() == HERDR_DEFAULT_BG


def fold_sample(current, sample, attempt, limit=THEME_LIMIT, follow=False):
    """One OSC 11 sample. (color, settled).

    A Herdr default-dark sample is not the answer while attempts remain.
    `follow` keeps a real color already seen, through a later default-dark
    sample, so a resize does not flash the theme back to black.
    """
    got = (sample or "").upper() or None
    held = (current or "").upper() or None
    if got and got != HERDR_DEFAULT_BG:
        return got, True
    if got == HERDR_DEFAULT_BG:
        if follow and held and held != HERDR_DEFAULT_BG:
            return held, attempt >= limit
        if attempt >= limit:
            return HERDR_DEFAULT_BG, True
        return held or HERDR_DEFAULT_BG, False
    return held, attempt >= limit


def detect_theme():
    """Probe once before curses. A #282C34 sample is checked again after the first paint.

    Herdr Desktop answers with its default dark for about two seconds. The same
    color is rechecked outside Herdr too: five probes, half a second apart.
    """
    if theme_override():
        THEME["watch"] = False
        return
    PROBED["bg"] = probe_background()
    if placeholder_background(PROBED["bg"]):
        THEME.update(watch=True, tries=1, due=time.monotonic() + THEME_GAP, follow=False)
    else:
        THEME.update(watch=False, tries=0, due=0.0, follow=True)


def note_appearance():
    """The window changed. Look again, unless GBOT_TUI_THEME already decided."""
    if theme_override() or not herdr.herdr_session():
        return
    THEME.update(watch=True, due=0, follow=not placeholder_background(PROBED["bg"]), tries=0)


def theme_due_ms():
    """How long the list may block before the next background check, or None."""
    if theme_override() or not THEME["watch"]:
        return None
    return max(int((THEME["due"] - time.monotonic()) * 1000), 30)


def maybe_retheme(apply=None):
    """Take the next background sample when it is due. True when the theme changed."""
    if theme_override() or not THEME["watch"] or time.monotonic() < THEME["due"]:
        return False
    THEME["tries"] += 1
    sample = probe_background(timeout=0.15)
    color, settled = fold_sample(PROBED["bg"], sample, THEME["tries"], follow=THEME["follow"])
    before = (PROBED["bg"] or "").upper()
    changed = bool(color) and color != before
    if color:
        PROBED["bg"] = color
    THEME["watch"] = not settled
    if settled:
        THEME["follow"] = not placeholder_background(PROBED["bg"])
    else:
        THEME["due"] = time.monotonic() + THEME_GAP
    if changed and apply:
        apply()
    return changed


def background_hex():
    if PROBED["bg"]:
        return PROBED["bg"]
    return "#000000" if dark_terminal() else "#FFFFFF"


def user_bubble():
    """The desktop app's user bubble: ansi 235 on a dark terminal, 254 on a light one.

    With a probed background the bubble is a step off that exact color, so a
    gray theme still shows it.
    """
    dark = dark_terminal()
    probed = PROBED["bg"]
    if not probed or (is_light(probed) == dark):
        return 235 if dark else 254
    gray = round(sum(hex_rgb(probed)) / 3)
    return nearest_gray(gray + 0x22 if dark else gray - 0x1C)
