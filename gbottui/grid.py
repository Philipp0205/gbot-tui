"""Wrapping and laying out the transcript and the composer."""

from . import cards, markdown, transcript, widgets


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
    return [pad + raw[start:end] for start, end, pad in _wrap_spans(raw, width)]


def _skip_spaces(raw, at):
    while at < len(raw) and raw[at] == " ":
        at += 1
    return at


def _wrap_spans(raw, width):
    """(start, end, pad) per wrapped line: the line is pad + raw[start:end]."""
    if raw == "":
        return [(0, 0, "")]
    pad = "  " if raw.startswith("- ") else ""
    if pad and width <= len(pad):
        pad = ""
    at = 0
    first = True
    spans = []
    while at < len(raw):
        rest = raw[at:]
        lead = "" if first else pad
        limit = width if first or not pad else width - len(pad)
        if len(rest) <= limit:
            spans.append((at, len(raw), lead))
            break
        window = rest[: limit + 1]
        cut = window.rfind(" ")
        head = rest[:cut] if cut > 0 else ""
        # The bullet's own space is not a break, or the first line is just "-".
        if cut > 0 and head.strip() and not (first and pad and cut <= len(pad)):
            spans.append((at, at + cut, lead))
            at = _skip_spaces(raw, at + cut + 1)
            first = False
            continue
        spans.append((at, at + limit, lead))
        at = _skip_spaces(raw, at + limit)
        first = False
    return spans or [(0, 0, "")]


def bubble_column(width, text):
    """Right edge of a user bubble, inset one column, as in the desktop chat."""
    return max(width - len(markdown.line_plain(text)) - 3, 0)


def composer_text_width(width):
    # " →  " plus the two borders, and one cell so the cursor is not on the border.
    return max(width - 7, 1)


def composer_cursor_column(line, placeholder):
    """Empty draft: sit on the space between the arrow and the placeholder."""
    if placeholder:
        return 4
    return 5 + len(line or "")


def draft_rows(typed, width):
    """Wrapped draft rows as (text, start, end, pad); start/end index `typed`."""
    if not typed:
        return []
    room = composer_text_width(width)
    rows = []
    base = 0
    for raw in typed.split("\n"):
        for start, end, pad in _wrap_spans(raw, room):
            rows.append((pad + raw[start:end], base + start, base + end, pad))
        base += len(raw) + 1
    return rows


def draft_lines(typed, width):
    return [row[0] for row in draft_rows(typed, width)]


def draft_cursor(typed, pos, width):
    """(row, column) of the cursor inside the wrapped draft, column in text cells."""
    rows = draft_rows(typed, width)
    if not rows:
        return 0, 0
    pos = len(typed) if pos is None else max(0, min(pos, len(typed)))
    at = 0
    for index, (_text, start, _end, _pad) in enumerate(rows):
        if start <= pos:
            at = index
    _text, start, end, pad = rows[at]
    column = len(pad) + min(pos - start, max(end - start, 0) + 1)
    return at, min(column, composer_text_width(width))


def draft_at(typed, width, row, column):
    """The draft index at a wrapped row and a column in text cells, or None past the last row."""
    rows = draft_rows(typed, width)
    if not 0 <= row < len(rows):
        return None
    _text, start, end, pad = rows[row]
    return start + max(0, min(column - len(pad), end - start))


def draft_marks(typed, width, found):
    """[(row, from, to)] columns of the selection `found` (start, end) in each wrapped row."""
    if not found:
        return []
    marks = []
    for index, (_text, start, end, pad) in enumerate(draft_rows(typed, width)):
        low, high = max(found[0], start), min(found[1], end)
        if low < high:
            marks.append((index, len(pad) + low - start, len(pad) + high - start))
    return marks


def draft_vertical(typed, pos, width, delta):
    """The index one wrapped row up (-1) or down (1), or None at the edge."""
    rows = draft_rows(typed, width)
    if not rows:
        return None
    row, column = draft_cursor(typed, pos, width)
    target = row + delta
    if target < 0 or target >= len(rows):
        return None
    _text, start, end, pad = rows[target]
    return start + max(0, min(column - len(pad), end - start))


def composer_window(count, cursor_row, visible):
    """First draft row shown: the tail, unless the cursor is above it."""
    first = max(count - visible, 0)
    return min(first, max(cursor_row, 0))


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
    picks = getattr(chat, "widget_picks", None) or {}
    card = getattr(chat, "widget_card", None) or {}
    key = (
        id(chat.messages),
        len(chat.messages),
        width,
        transcript.entry_text(last) if last else "",
        chat.rows_rev,
        tuple((name, tuple(values)) for name, values in picks.items()),
        (card.get("first"), card.get("at"), tuple((card.get("answers") or {}).items())),
    )
    if getattr(chat, "_rows_key", None) != key:
        chat._rows = transcript_rows(chat.messages, width, picks, card)
        chat._rows_key = key
    return chat._rows


def search_hits(rows, query):
    """Indexes of the rows whose text holds `query`, case-folded, top to bottom."""
    needle = (query or "").casefold()
    if not needle:
        return []
    return [index for index, (_kind, text) in enumerate(rows) if needle in markdown.line_plain(text).casefold()]


def match_spans(text, query):
    """(start, end) of each case-folded occurrence of `query` in one row's text."""
    plain = markdown.line_plain(text).casefold()
    needle = (query or "").casefold()
    out = []
    at = plain.find(needle) if needle else -1
    while at >= 0:
        out.append((at, at + len(needle)))
        at = plain.find(needle, at + len(needle))
    return out


SEARCH_HINT = " · enter/ctrl-n older · ctrl-p newer · esc close"


def search_spans(query, hits, at, width=0):
    """The one-line search prompt that replaces the footer."""
    if not query:
        status = ""
    elif not hits:
        status = "  no match"
    else:
        status = f"  {(at if at is not None else len(hits) - 1) + 1}/{len(hits)}"
    spans = [("find: ", "bold"), (query, "default"), (status, "dim")]
    room = max(width - 2, 0) if width else 0
    if not room or len("find: " + query + status + SEARCH_HINT) <= room:
        spans.append((SEARCH_HINT, "dim"))
    return spans


def card_lines(entry, width):
    card = transcript.message_card(entry)
    if card is None:
        return []
    heading = transcript.card_heading(card.get("status"))
    if not heading:
        return []
    lines = [heading]
    room = max(width - 2, 1)
    for key in transcript.DETAIL_KEYS:
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            for line in markdown.render_markdown(value.strip(), room):
                lines.append(markdown.line_plain(line))
    return lines


def request_rows(asked, width):
    """Rows of a secret, 1Password or form card: (kind, text), the state line last.

    The body is neutral and only the state line takes the tone (green, yellow,
    gray), so a settled card does not turn the whole thing green. No Markdown
    here: an env name like GH_TOKEN_WORKFLOW is not emphasis.
    """
    room = max(width - 2, 1)
    rows = []
    for part in asked["body"].split("\n"):
        rows.extend(("card-info", line) for line in wrap(part, room))
    rows.extend((asked["tone"], line) for line in wrap(asked["state"], room))
    return rows


PAGE_PREFIX = "\U0001F4C4 page: "


class PageRow(str):
    """The text of a `card-page` row. Equal to the plain string; `.url` is what a click opens."""

    __slots__ = ("url",)

    def __new__(cls, text, url):
        row = super().__new__(cls, text)
        row.url = url
        return row


def transcript_rows(entries, width, picks=None, card=None):
    rows = []
    user_w = max(width - 8, 1)
    text_w = max(width - 4, 1)
    picks = picks or {}
    found, at, answers = widgets.card_view(entries, card)
    # An open card of several questions is one page, drawn where its newest widget is.
    paged = len(found) > 1
    paged_ids = {id(page["entry"]) for page in found} if paged else set()
    page_entry = found[-1]["entry"] if paged else None
    # A single open question is numbered, and only that one.
    current = found[0]["entry"] if found and not paged else None
    hidden = transcript.hidden_status_ids(entries)
    asked_views = cards.views(entries)
    for entry, asked in zip(entries, asked_views):
        if isinstance(entry, dict) and transcript._row_key(entry) in hidden:
            continue
        if asked:
            if rows:
                rows.append(("gap", ""))
            rows.extend(request_rows(asked, width))
            continue
        lines = card_lines(entry, width)
        pictures = [] if transcript.is_user(entry) else transcript.image_lines(entry)
        linked = transcript.pages(entry)
        if id(entry) in paged_ids:
            page = found[at]
            text = (
                widgets.page_text(page, at, len(found), widgets.page_picks(page, picks, answers), answers.get(page["key"], ""))
                if entry is page_entry
                else ""
            )
        else:
            numbered = current is not None and entry is current
            name = found[0]["key"] if numbered else entry.get("id") if isinstance(entry, dict) else None
            # A bot's image lines are drawn dim below; a user's stay in the bubble.
            text = transcript.entry_text(entry, picks.get(name) or (), numbered, media=not pictures).strip()
        if not text and not lines and not pictures and not linked:
            continue
        if rows:
            rows.append(("gap", ""))
        if text:
            user = transcript.is_user(entry)
            wrapped = markdown.render_markdown(text, user_w if user else text_w)
            if user:
                span = max(len(markdown.line_plain(line)) for line in wrapped)
                for line in wrapped:
                    rows.append(("user", markdown.pad_line(line, span)))
            else:
                for line in wrapped:
                    rows.append(("text", line))
        for picture in pictures:
            for line in wrap(picture, text_w):
                rows.append(("text", [(line, "dim")]))
        for title, url in linked:
            if text or pictures:
                rows.append(("gap", ""))
            rows.append(("card-page", PageRow(clip(PAGE_PREFIX + title, max(width - 2, 1)), url)))
        if lines:
            if text or pictures or linked:
                rows.append(("gap", ""))
            kind = transcript.card_row_kind(entry)
            for line in lines:
                rows.append((kind, line))
    return rows


def composer_lines(width, typed):
    span = max(width, 0)
    if span <= 0:
        return "", "", ""
    if span == 1:
        return "┌", "│", "└"
    rule = "─" * (span - 2)
    shown = typed if typed else "Add a follow-up"
    if typed:
        shown = draft_lines(typed, width)[0]
    body = f" →  {shown}"[: span - 2].ljust(span - 2)
    return "┌" + rule + "┐", "│" + body + "│", "└" + rule + "┘"


DECIDE_HINT = "  y approve · n decline · ctrl-y/n types"
SHORT_HINT = "  y/n"
ERROR_ROWS = 3


def footer_spans(phase, error, decide=False, width=0, choice="", notice=""):
    """One footer line. With a width, the y/n hint shrinks before the error does.

    An error is red. A notice (copied, sent elsewhere) is green and only shows
    when there is no error.
    """
    room = max(width - 2, 0) if width else 0
    if phase == "blocked":
        # The error used to replace this line, so a failed respond hid y/n
        # while the card was pending again.
        if error:
            lead = (error, "red")
        elif notice:
            lead = (notice, "green")
        else:
            lead = ("Pending decision", "yellow")
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
    if notice:
        return [(clip(notice, room) if room else notice, "green")]
    if phase == "working":
        return [("Generating", "bold"), ("  —  pgup scroll", "dim")]
    rest = "esc bots · ctrl-c quit · pgup scroll · @ tag a bot · ? keys"
    if choice:
        # One "enter". A multi-select hint already says "enter sends".
        enter = "" if "enter" in choice.lower() else "enter send · "
        hint = f"{choice} · space then digit types · {enter}{rest}"
    else:
        hint = f"enter send · {rest}"
    return [(hint, "dim")]


def footer_rows(phase, error, decide=False, width=0, choice="", notice=""):
    """Footer lines, bottom last. An error too long for one line gets rows of its own.

    A 401 or a gateway message has to stay readable in a 58-column window, so
    it is wrapped above the footer rather than clipped beside the y/n hint.
    """
    room = max(width - 2, 1)
    hint = len(SHORT_HINT) if phase == "blocked" and decide else 0
    if not error or len(error) + hint <= room:
        return [footer_spans(phase, error, decide, width, choice, notice)]
    lines = wrap(error, room)
    if len(lines) > ERROR_ROWS:
        lines = lines[: ERROR_ROWS - 1] + [clip(" ".join(lines[ERROR_ROWS - 1 :]), room)]
    rows = [[(line, "red")] for line in lines]
    rows.append(footer_spans(phase, "", decide, width, choice))
    return rows
