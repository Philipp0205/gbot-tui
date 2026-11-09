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
        curses.set_escdelay(ESC_WAIT_MS)
    except (AttributeError, curses.error):
        return


# How long a lone Esc waits for the rest of a sequence, in ms. Same as set_escdelay.
ESC_WAIT_MS = 25
# CSI and SS3 finals curses may not decode: a terminal not in keypad mode, or a
# multiplexer, sends ESC [ D where terminfo says ESC O D.
ARROW_FINALS = {
    "A": (curses.KEY_UP, "kUP"),
    "B": (curses.KEY_DOWN, "kDN"),
    "C": (curses.KEY_RIGHT, "kRIT"),
    "D": (curses.KEY_LEFT, "kLFT"),
    "H": (curses.KEY_HOME, "kHOM"),
    "F": (curses.KEY_END, "kEND"),
}
SS3_FUNCTION = {"P": 1, "Q": 2, "R": 3, "S": 4}
TILDE_KEYS = {
    1: (curses.KEY_HOME, "kHOM"),
    2: (curses.KEY_IC, "kIC"),
    3: (curses.KEY_DC, "kDC"),
    4: (curses.KEY_END, "kEND"),
    5: (curses.KEY_PPAGE, "kPRV"),
    6: (curses.KEY_NPAGE, "kNXT"),
    7: (curses.KEY_HOME, "kHOM"),
    8: (curses.KEY_END, "kEND"),
}
TILDE_FUNCTION = {11: 1, 12: 2, 13: 3, 14: 4, 15: 5, 17: 6, 18: 7, 19: 8, 20: 9, 21: 10, 23: 11, 24: 12}
_NAMED = None
# Between ESC [200~ and ESC [201~. A paste can span two reads.
PASTING = False
BRACKETED_PASTE_ON = b"\033[?2004h"
BRACKETED_PASTE_OFF = b"\033[?2004l"


def named_key(name, fallback):
    """The curses code terminfo gives a modified key (`kLFT5`), else the plain key."""
    global _NAMED
    if _NAMED is None:
        found = {}
        for code in range(curses.KEY_MIN, 1024):
            try:
                label = curses.keyname(code)
            except (curses.error, ValueError):
                continue
            if isinstance(label, str):
                label = label.encode()
            found.setdefault(label, code)
        _NAMED = found
    return _NAMED.get(name.encode(), fallback)


def _csi_key(params, final, ss3=False):
    """One decoded key, or None to drop an unknown sequence."""
    fields = [field for field in params.split(";")]
    try:
        numbers = [int(field) if field else 1 for field in fields] if params else []
    except ValueError:
        return None
    mod = numbers[1] if len(numbers) > 1 else (numbers[0] if ss3 and numbers else 1)
    if final in ARROW_FINALS:
        plain, name = ARROW_FINALS[final]
        return named_key(f"{name}{mod}", plain) if mod > 1 else plain
    if final in SS3_FUNCTION and (ss3 or numbers[:1] == [1]):
        return curses.KEY_F0 + SS3_FUNCTION[final]
    if final == "~" and numbers:
        if numbers[0] in TILDE_FUNCTION:
            return curses.KEY_F0 + TILDE_FUNCTION[numbers[0]]
        if numbers[0] in TILDE_KEYS:
            plain, name = TILDE_KEYS[numbers[0]]
            return named_key(f"{name}{mod}", plain) if mod > 1 else plain
    return None


def is_escape(key):
    return key in ("\x1b", 27)


def decode_keys(keys, more=lambda: None):
    """Fold ESC-prefixed CSI / SS3 sequences curses passed through into keys.

    `more()` returns the next key within the escape delay, or None. Only an
    Esc with nothing after it stays "\x1b". An unknown sequence, or Esc and a
    plain character (Alt-letter), is dropped instead of reading as Esc.
    """
    global PASTING
    queue = list(keys)
    out = []

    def pull():
        return queue.pop(0) if queue else more()

    while queue:
        key = queue.pop(0)
        if not is_escape(key):
            # Inside a bracketed paste Enter is a newline in the draft, not send.
            out.append("\n" if PASTING and key in ("\r", curses.KEY_ENTER) else key)
            continue
        lead = pull()
        if lead is None:
            out.append("\x1b")
            continue
        if is_escape(lead) or not isinstance(lead, str):
            # Esc then a key curses decoded: the Esc was lone.
            out.append("\x1b")
            queue.insert(0, lead)
            continue
        if lead not in ("[", "O"):
            continue
        params = ""
        final = None
        while True:
            part = pull()
            if isinstance(part, str) and (part.isdigit() or part == ";"):
                params += part
                continue
            if isinstance(part, str) and "\x40" <= part <= "\x7e":
                final = part
            elif part is not None:
                queue.insert(0, part)
            break
        if final is None:
            continue
        if lead == "[" and final == "~" and params in ("200", "201"):
            PASTING = params == "200"
            continue
        decoded = _csi_key(params, final, ss3=lead == "O")
        if decoded is not None:
            out.append(decoded)
    return out


def follow_up(stdscr, restore):
    """A `more` for decode_keys: one key within ESC_WAIT_MS, then the old timeout."""

    def more():
        stdscr.timeout(ESC_WAIT_MS)
        try:
            return stdscr.get_wch()
        except curses.error:
            return None
        finally:
            stdscr.timeout(restore)

    return more


def read_keys(stdscr, restore=0):
    """Every key curses has now. A paste is one redraw, not one per character."""
    keys = []
    while len(keys) < 4096:
        try:
            keys.append(stdscr.get_wch())
        except curses.error:
            break
    if any(is_escape(key) for key in keys):
        keys = decode_keys(keys, follow_up(stdscr, restore))
    return keys
