"""Escape sequences written straight to the terminal, outside curses."""

import os
import sys

# xterm's title stack: push on start, pop on exit. Terminals without it ignore both.
PUSH_TITLE = b"\033[22;0t"
POP_TITLE = b"\033[23;0t"


def printable(text):
    """Text safe inside an escape sequence: no ESC and no line breaks."""
    return text.replace("\x1b", "").replace("\r", " ").replace("\n", " ")


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
    write_tty(f"\033]0;{printable(text)}\033\\".encode())
