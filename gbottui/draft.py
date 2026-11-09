"""Editing the composer draft: a text and a cursor index into it.

Every function takes (text, pos) and returns (text, pos). Line start and end
are the typed lines (split on newline), as in readline. Up and Down move
between the wrapped rows on screen and live in `grid`.
"""


def clamp(text, pos):
    if pos is None:
        return len(text)
    return max(0, min(int(pos), len(text)))


def insert(text, pos, value):
    pos = clamp(text, pos)
    return text[:pos] + value + text[pos:], pos + len(value)


def backspace(text, pos):
    pos = clamp(text, pos)
    if pos == 0:
        return text, 0
    return text[: pos - 1] + text[pos:], pos - 1


def delete(text, pos):
    pos = clamp(text, pos)
    return text[:pos] + text[pos + 1 :], pos


def left(text, pos):
    return text, max(clamp(text, pos) - 1, 0)


def right(text, pos):
    return text, min(clamp(text, pos) + 1, len(text))


def line_start(text, pos):
    pos = clamp(text, pos)
    return text, text.rfind("\n", 0, pos) + 1


def line_end(text, pos):
    pos = clamp(text, pos)
    end = text.find("\n", pos)
    return text, len(text) if end < 0 else end


def word_left(text, pos):
    pos = clamp(text, pos)
    while pos > 0 and not text[pos - 1].isalnum():
        pos -= 1
    while pos > 0 and text[pos - 1].isalnum():
        pos -= 1
    return text, pos


def word_right(text, pos):
    pos = clamp(text, pos)
    while pos < len(text) and not text[pos].isalnum():
        pos += 1
    while pos < len(text) and text[pos].isalnum():
        pos += 1
    return text, pos


def kill_word(text, pos):
    """Ctrl-W: the word before the cursor and the spaces after it."""
    pos = clamp(text, pos)
    start = pos
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return text[:start] + text[pos:], start


def kill_line_start(text, pos):
    pos = clamp(text, pos)
    _text, start = line_start(text, pos)
    if start == pos and pos > 0:
        start = pos - 1  # at a line start, join with the line above
    return text[:start] + text[pos:], start


def kill_line_end(text, pos):
    pos = clamp(text, pos)
    _text, end = line_end(text, pos)
    if end == pos and pos < len(text):
        end = pos + 1  # at a line end, join with the line below
    return text[:pos] + text[end:], pos
