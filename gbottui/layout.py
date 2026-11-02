"""Where text goes on the chat screen. Plain strings and row tuples, no curses."""

from .transcript import card_details, card_heading, card_row_kind, entry_text, is_user, message_card

PLACEHOLDER = "Add a follow-up"
DECIDE_HINT = "  y approve · n decline · ctrl-y/n types"
SHORT_HINT = "  y/n"
ERROR_ROWS = 3


def clip(text, width):
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def wrap(text, width):
    """Wrap on spaces. A word longer than the width is the only hard break.

    The space at the break is not drawn on the next line. A line that starts
    with "- " keeps a hanging indent, so the continuation lines up under the
    text after the bullet rather than under the "-".
    """
    width = max(width, 1)
    lines = []
    for raw in text.splitlines() or [""]:
        lines.extend(_wrap_line(raw, width))
    return lines or [""]


def _wrap_line(raw, width):
    if raw == "":
        return [""]
    pad = "  " if raw.startswith("- ") else ""
    if pad and width <= len(pad):
        pad = ""
    rest = raw
    first = True
    lines = []
    while rest:
        limit = width if first or not pad else width - len(pad)
        if len(rest) <= limit:
            lines.append(rest if first else pad + rest)
            break
        window = rest[: limit + 1]
        cut = window.rfind(" ")
        head = rest[:cut] if cut > 0 else ""
        # The bullet's own space is not a break, or the first line is just "-".
        if cut > 0 and head.strip() and not (first and pad and cut <= len(pad)):
            lines.append(head if first else pad + head)
            rest = rest[cut + 1 :].lstrip(" ")
            first = False
            continue
        lines.append(rest[:limit] if first else pad + rest[:limit])
        rest = rest[limit:].lstrip(" ")
        first = False
    return lines or [""]


def bubble_column(width, text):
    """Right edge of a user bubble, inset one column, as in the desktop chat."""
    shown = " " + text + " "
    return max(width - len(shown) - 1, 0)


def composer_text_width(width):
    # " →  " plus the two borders, and one cell so the cursor is not on the border.
    return max(width - 7, 1)


def composer_cursor_column(line, placeholder):
    """Empty draft: sit on the space between the arrow and the placeholder."""
    if placeholder:
        return 4
    return 5 + len(line or "")


def draft_lines(typed, width):
    if not typed:
        return []
    return wrap(typed, composer_text_width(width))


def layout(height, text_lines=1):
    if height >= 10:
        header = 2
    elif height >= 5:
        header = 1
    else:
        header = 0
    if height >= 7:
        footer = 1
        text_lines = max(int(text_lines), 1)
        # Two box rules and one transcript row stay on screen.
        text_lines = min(text_lines, max(height - header - footer - 3, 1))
        composer = text_lines + 2
    elif height >= 3:
        composer, footer = 1, 0
    else:
        composer, footer = 0, 0
    body = max(height - header - composer - footer, 0)
    return header, body, composer, footer


def header_gap(header, label, body_h):
    """Blank row between the header text and the first message.

    A two-line header with no tagline already leaves that second row empty.
    A one-row body keeps the message instead of spending its only line on the gap.
    """
    if header <= 0 or body_h <= 1:
        return 0
    if header > 1 and not label:
        return 0
    return 1


def visible_rows(chat, width):
    # Wrapping the whole thread on every keystroke is what made typing feel late.
    last = chat.messages[-1] if chat.messages else None
    key = (
        id(chat.messages),
        len(chat.messages),
        width,
        entry_text(last) if last else "",
        chat.rows_rev,
    )
    if getattr(chat, "_rows_key", None) != key:
        chat._rows = transcript_rows(chat.messages, width)
        chat._rows_key = key
    return chat._rows


def card_lines(entry, width):
    card = message_card(entry)
    if card is None:
        return []
    heading = card_heading(card.get("status"))
    if not heading:
        return []
    lines = [heading]
    room = max(width - 2, 1)
    for value in card_details(card):
        lines.extend(wrap(value, room))
    return lines


def transcript_rows(entries, width):
    rows = []
    user_w = max(width - 8, 1)
    text_w = max(width - 4, 1)
    for entry in entries:
        lines = card_lines(entry, width)
        text = entry_text(entry).strip()
        if not text and not lines:
            continue
        if rows:
            rows.append(("gap", ""))
        if text:
            if is_user(entry):
                wrapped = wrap(text, user_w)
                span = max(len(line) for line in wrapped)
                rows.extend(("user", line.ljust(span)) for line in wrapped)
            else:
                rows.extend(("text", line) for line in wrap(text, text_w))
        if lines:
            if text:
                rows.append(("gap", ""))
            kind = card_row_kind(entry)
            rows.extend((kind, line) for line in lines)
    return rows


def composer_lines(width, typed):
    span = max(width, 0)
    if span <= 0:
        return "", "", ""
    if span == 1:
        return "┌", "│", "└"
    rule = "─" * (span - 2)
    shown = draft_lines(typed, width)[0] if typed else PLACEHOLDER
    body = f" →  {shown}"[: span - 2].ljust(span - 2)
    return "┌" + rule + "┐", "│" + body + "│", "└" + rule + "┘"


def footer_spans(phase, error, decide=False, width=0):
    """One footer line. With a width, the y/n hint shrinks before the error does."""
    room = max(width - 2, 0) if width else 0
    if phase == "blocked":
        # The error used to replace this line, so a failed respond hid y/n
        # while the card was pending again.
        lead = (error, "red") if error else ("Pending decision", "yellow")
        if not decide:
            return [(clip(lead[0], room) if room else lead[0], lead[1])]
        hint = DECIDE_HINT
        if room and len(lead[0]) + len(hint) > room:
            hint = SHORT_HINT
        if room and len(lead[0]) + len(hint) > room:
            lead = (clip(lead[0], max(room - len(hint), 1)), lead[1])
        return [lead, (hint, "dim")]
    if error:
        return [(clip(error, room) if room else error, "red")]
    if phase == "working":
        return [("Generating", "bold"), ("  —  pgup scroll", "dim")]
    return [("enter send · esc bots · ctrl-c quit · pgup scroll", "dim")]


def footer_rows(phase, error, decide=False, width=0):
    """Footer lines, bottom last. An error too long for one line gets rows of its own.

    A 401 or a gateway message has to stay readable in a 58-column window, so
    it is wrapped above the footer rather than clipped beside the y/n hint.
    """
    room = max(width - 2, 1)
    hint = len(SHORT_HINT) if phase == "blocked" and decide else 0
    if not error or len(error) + hint <= room:
        return [footer_spans(phase, error, decide, width)]
    lines = wrap(error, room)
    if len(lines) > ERROR_ROWS:
        lines = lines[: ERROR_ROWS - 1] + [clip(" ".join(lines[ERROR_ROWS - 1 :]), room)]
    rows = [[(line, "red")] for line in lines]
    rows.append(footer_spans(phase, "", decide, width))
    return rows
