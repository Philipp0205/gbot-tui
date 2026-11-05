"""Terminal modes and input helpers."""

import curses
import os
import subprocess
import sys


def write_tty(data):
    try:
        tty = os.open("/dev/tty", os.O_WRONLY)
    except OSError:
        tty = None
    try:
        if tty is None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            os.write(tty, data)
    except OSError:
        pass
    finally:
        if tty is not None:
            os.close(tty)


def window_title(text):
    label = text.replace("\x1b", "").replace("\r", " ").replace("\n", " ")
    write_tty(f"\033]0;{label}\033\\".encode())


# xterm's title stack: push on start, pop on exit. Terminals without it ignore both.
PUSH_TITLE = b"\033[22;0t"
POP_TITLE = b"\033[23;0t"


WHEEL_LINES = 3
LAST_CLICK = None
LINK_HITS = []


def open_link_at(x, y):
    for hy, x0, x1, url in LINK_HITS:
        if hy == y and x0 <= x < x1:
            try:
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                pass
            return True
    return False


def enable_wheel():
    try:
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)
        curses.mouseinterval(0)
    except (AttributeError, curses.error):
        pass


def wheel_direction(key):
    """-1 for wheel up, 1 for wheel down, None for any other key."""
    if key != curses.KEY_MOUSE:
        return None
    try:
        _id, mx, my, _z, state = curses.getmouse()
    except curses.error:
        return None
    if state & curses.BUTTON1_PRESSED:
        global LAST_CLICK
        LAST_CLICK = (mx, my)
    if state & curses.BUTTON4_PRESSED:
        return -1
    if state & curses.BUTTON5_PRESSED:
        return 1
    return None


def page_motion(key):
    """(direction, half). Alt-Page is kPRV3/kNXT3; plain Page is KEY_PPAGE/KEY_NPAGE."""
    if key == curses.KEY_PPAGE:
        return -1, False
    if key == curses.KEY_NPAGE:
        return 1, False
    if isinstance(key, int):
        try:
            name = curses.keyname(key)
        except curses.error:
            name = b""
        if name in (b"kPRV3", "kPRV3"):
            return -1, True
        if name in (b"kNXT3", "kNXT3"):
            return 1, True
    return None


def page_step(lines, half):
    """A screen, or half of one. Same split as a Herdr pane."""
    step = max(lines - 4, 1)
    return max(step // 2, 1) if half else step
