"""Render Markdown into styled terminal rows."""

import re

from . import grid


# A reply is worth parsing when it carries markup. Plain text keeps wrap().
_MD_HINT = re.compile(
    r"(?:"
    r"\*\*|__|~~|`"
    r"|^\s{0,3}#{1,6}[ \t]+\S"
    r"|^\s*[-*+][ \t]+\S"
    r"|^\s*\d{1,4}[.)][ \t]+\S"
    r"|^\s{0,3}>"
    r"|^\s*\|.+\|.+"
    r"|^\s*(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
    r"|\[[^\]\n]+\]\([^)\n]+\)"
    r"|!\[[^\]]*\]\([^)\n]+\)"
    r"|\*[^*\s][^*\n]*\S\*"
    r"|(?<![A-Za-z0-9])_[^_\s][^_\n]*\S_(?![A-Za-z0-9])"
    r"|https?://"
    r")",
    re.M,
)
_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*([^`~]*)$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)\s*#*[ \t]*$")
_RULE_RE = re.compile(r"^(?:\*[ \t]*){3,}$|^(?:-[ \t]*){3,}$|^(?:_[ \t]*){3,}$")
_BULLET_RE = re.compile(r"^( *)([-*+])[ \t]+(.*)$")
_NUMBER_RE = re.compile(r"^( *)(\d{1,4})([.)])[ \t]+(.*)$")
_CHECK_RE = re.compile(r"^\[([ xX])\][ \t]+(.*)$")
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?(.*)$")
_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
_AUTOLINK_RE = re.compile(r"<((?:https?://)[^>\s]+)>")
_URL_RE = re.compile(r"https?://[^\s<>]+")
_EM_STAR_RE = re.compile(r"\*(?!\s|\*)(.+?\S)\*(?!\*)")
_EM_UNDER_RE = re.compile(r"_(?!\s)([^_\n]*?\S)_(?![A-Za-z0-9])")


def line_plain(line):
    """The characters a row occupies. A styled row is a list of (text, style)."""
    if isinstance(line, str):
        return line
    return "".join(text for text, _style in line)


def pad_line(line, span):
    plain = line_plain(line)
    if len(plain) >= span:
        return line
    gap = " " * (span - len(plain))
    if isinstance(line, str):
        return line + gap
    return list(line) + [(gap, "")]


def _merge_spans(spans):
    merged = []
    for text, style in spans:
        if not text:
            continue
        if merged and merged[-1][1] == style:
            merged[-1] = (merged[-1][0] + text, style)
        else:
            merged.append((text, style))
    return merged


def _as_line(spans):
    """A row with no style stays a string, so a plain reply draws as before."""
    merged = _merge_spans(spans)
    if not merged:
        return ""
    if all(style == "" for _text, style in merged):
        return "".join(text for text, _style in merged)
    return merged


def _slice_spans(spans, start, end):
    out = []
    pos = 0
    for text, style in spans:
        nxt = pos + len(text)
        if nxt <= start:
            pos = nxt
            continue
        if pos >= end:
            break
        piece = text[max(0, start - pos) : min(len(text), end - pos)]
        if piece:
            out.append((piece, style))
        pos = nxt
    return out


def _join_style(outer, inner):
    if not outer:
        return inner
    if not inner:
        return outer
    if "code" in outer.split("+") or "code" in inner.split("+"):
        return "code"
    names = []
    for part in f"{outer}+{inner}".split("+"):
        if part and part not in names:
            names.append(part)
    return "+".join(names)


def _with_style(spans, style):
    return [(text, _join_style(style, inner)) for text, inner in spans]


def _fit_spans(spans, room):
    """Clip or pad one table cell to room columns."""
    spans = _merge_spans(spans)
    plain = "".join(text for text, _style in spans)
    if len(plain) <= room:
        gap = room - len(plain)
        if gap and spans:
            return spans + [(" " * gap, spans[-1][1])]
        if gap:
            return [(" " * gap, "")]
        return spans
    if room <= 0:
        return []
    if room == 1:
        return [("…", spans[0][1] if spans else "")]
    body = _slice_spans(spans, 0, room - 1)
    body.append(("…", body[-1][1] if body else ""))
    return body


def _inline_break(text, index):
    char = text[index]
    if char in "`*[<\\~":
        return True
    if char == "_" and (index == 0 or not text[index - 1].isalnum()):
        return True
    if char == "!" and text.startswith("![", index):
        return True
    if text.startswith(("http://", "https://"), index):
        return True
    return False


def _parse_inlines(text):
    """Inline markdown. Unclosed markers stay visible, so a stream can grow."""
    spans = []
    index = 0
    limit = len(text)
    while index < limit:
        if text[index] == "\\" and index + 1 < limit and text[index + 1] in "\\`*_{}[]()#+-.!|>~":
            spans.append((text[index + 1], ""))
            index += 2
            continue
        if text[index] == "`":
            found = _CODE_RE.match(text, index)
            if found:
                spans.append((found.group(1), "code"))
                index = found.end()
                continue
        if text.startswith("![", index):
            found = _IMAGE_RE.match(text, index)
            if found:
                alt = found.group(1).strip()
                spans.append((alt, "") if alt else (found.group(2).strip(), "dim"))
                index = found.end()
                continue
        if text[index] == "[":
            found = _LINK_RE.match(text, index)
            if found:
                label = found.group(1)
                url = found.group(2).strip()
                spans.extend(_with_style(_parse_inlines(label), "link"))
                if url and url != label:
                    spans.append((f" ({url})", "dim"))
                index = found.end()
                continue
        if text[index] == "<":
            found = _AUTOLINK_RE.match(text, index)
            if found:
                spans.append((found.group(1), "link"))
                index = found.end()
                continue
        if text.startswith(("http://", "https://"), index):
            found = _URL_RE.match(text, index)
            if found:
                url = found.group(0).rstrip(".,;:!?)]")
                if url:
                    spans.append((url, "link"))
                    index += len(url)
                    continue
        if text.startswith("~~", index):
            end = text.find("~~", index + 2)
            if end > index + 2:
                spans.extend(_with_style(_parse_inlines(text[index + 2 : end]), "dim"))
                index = end + 2
                continue
        if text.startswith("***", index):
            end = text.find("***", index + 3)
            if end > index + 3:
                spans.extend(_with_style(_parse_inlines(text[index + 3 : end]), "bold+em"))
                index = end + 3
                continue
        if text.startswith(("**", "__"), index):
            marker = text[index : index + 2]
            end = text.find(marker, index + 2)
            if end > index + 2:
                spans.extend(_with_style(_parse_inlines(text[index + 2 : end]), "bold"))
                index = end + 2
                continue
        if text[index] == "*" and not text.startswith("**", index):
            found = _EM_STAR_RE.match(text, index)
            if found:
                spans.extend(_with_style(_parse_inlines(found.group(1)), "em"))
                index = found.end()
                continue
        if text[index] == "_" and (index == 0 or not text[index - 1].isalnum()):
            found = _EM_UNDER_RE.match(text, index)
            if found:
                spans.extend(_with_style(_parse_inlines(found.group(1)), "em"))
                index = found.end()
                continue
        nxt = index + 1
        while nxt < limit and not _inline_break(text, nxt):
            nxt += 1
        spans.append((text[index:nxt], ""))
        index = nxt
    return _merge_spans(spans)


def _wrap_styled(spans, width, hang):
    """Wrap like wrap(), keeping each run's style. hang is the continuation indent."""
    spans = _merge_spans(spans)
    raw = "".join(text for text, _style in spans)
    if raw == "":
        return [""]
    width = max(width, 1)
    if hang < 0 or hang >= width:
        hang = 0
    pos = 0
    first = True
    lines = []
    while pos < len(raw):
        limit = width if first or not hang else width - hang
        rest = raw[pos:]
        if len(rest) <= limit:
            piece = _slice_spans(spans, pos, len(raw))
            if not first and hang:
                piece = [(" " * hang, "")] + piece
            lines.append(_as_line(piece))
            break
        window = rest[: limit + 1]
        cut = window.rfind(" ")
        # The marker's own space is not a break, or the first line is just "-".
        if cut > 0 and rest[:cut].strip() and not (first and hang and cut <= hang):
            piece = _slice_spans(spans, pos, pos + cut)
            if not first and hang:
                piece = [(" " * hang, "")] + piece
            lines.append(_as_line(piece))
            pos += cut + 1
        else:
            piece = _slice_spans(spans, pos, pos + limit)
            if not first and hang:
                piece = [(" " * hang, "")] + piece
            lines.append(_as_line(piece))
            pos += limit
        while pos < len(raw) and raw[pos] == " ":
            pos += 1
        first = False
    return lines or [""]


def _list_match(line):
    return _NUMBER_RE.match(line) or _BULLET_RE.match(line)


def _split_item(line):
    numbered = _NUMBER_RE.match(line)
    if numbered:
        indent, number, punct, rest = numbered.groups()
        marker = f"{number}{punct} "
    else:
        bullet = _BULLET_RE.match(line)
        indent, _mark, rest = bullet.groups()
        marker = "- "
    box = _CHECK_RE.match(rest)
    if box:
        marker += "[x] " if box.group(1).lower() == "x" else "[ ] "
        rest = box.group(2)
    prefix = f"{indent}{marker}"
    return prefix, rest


def _render_list(lines, width):
    out = []
    index = 0
    while index < len(lines):
        if not _list_match(lines[index]):
            out.extend(_wrap_styled(_parse_inlines(lines[index].strip()), width, 0))
            index += 1
            continue
        prefix, rest = _split_item(lines[index])
        index += 1
        extra = []
        while index < len(lines) and not _list_match(lines[index]):
            extra.append(lines[index].strip())
            index += 1
        more = " ".join(part for part in extra if part)
        if more:
            rest = f"{rest} {more}".strip()
        out.extend(_wrap_styled([(prefix, "")] + _parse_inlines(rest), width, len(prefix)))
    return out


def _take_list(lines, index):
    start = len(_list_match(lines[index]).group(1))
    block = []
    while index < len(lines):
        line = lines[index]
        if line.strip() == "":
            nxt = lines[index + 1] if index + 1 < len(lines) else ""
            found = _list_match(nxt)
            if found and len(found.group(1)) >= start:
                index += 1
                continue
            break
        found = _list_match(line)
        if found and len(found.group(1)) >= start:
            block.append(line)
            index += 1
            continue
        if block and (line.startswith(" ") or line.startswith("\t")):
            block.append(line)
            index += 1
            continue
        break
    return block, index


def _is_sep(line):
    return bool(_SEP_RE.match(line.strip()))


def _pipe_row(line):
    stripped = line.strip()
    return (
        stripped.startswith("|")
        and stripped.endswith("|")
        and stripped.count("|") >= 3
        and not _is_sep(stripped)
    )


def _cells(line):
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _starts_table(lines, index):
    if not _pipe_row(lines[index]):
        return False
    if index + 1 >= len(lines):
        return False
    return _is_sep(lines[index + 1]) or _pipe_row(lines[index + 1])


def _take_table(lines, index):
    rows = []
    header = False
    while index < len(lines):
        if _is_sep(lines[index]):
            if rows:
                header = True
                index += 1
                continue
            break
        if not _pipe_row(lines[index]):
            break
        rows.append(_cells(lines[index]))
        index += 1
    return rows, header, index


def _render_table(rows, header, width):
    if not rows:
        return []
    cols = max(len(row) for row in rows)
    parsed = []
    for row in rows:
        cells = [_parse_inlines(cell) for cell in row]
        cells.extend([] for _ in range(cols - len(cells)))
        parsed.append(cells)
    widths = [1] * cols
    for row in parsed:
        for col, spans in enumerate(row):
            widths[col] = max(widths[col], len("".join(text for text, _style in spans)) or 1)

    def total(sizes):
        return sum(sizes) + 2 * (cols - 1)

    while total(widths) > width and max(widths) > 1:
        widest = max(range(cols), key=lambda col: widths[col])
        widths[widest] -= 1
    out = []
    for index, row in enumerate(parsed):
        parts = []
        for col, spans in enumerate(row):
            shown = _fit_spans(spans, widths[col])
            if header and index == 0:
                shown = _with_style(shown, "bold")
            if parts:
                parts.append(("  ", ""))
            parts.extend(shown)
        out.append(_as_line(parts))
    return out


def _open_fence(line):
    found = _FENCE_OPEN.match(line)
    if not found:
        return None
    return found.group(2), found.group(3).strip()


def _take_fence(lines, index, opener):
    marker, lang = opener
    need = len(marker)
    char = re.escape(marker[0])
    close_re = re.compile(rf"^ {{0,3}}({char}{{3,}})[ \t]*$")
    body = []
    index += 1
    while index < len(lines):
        close = close_re.match(lines[index])
        if close and len(close.group(1)) >= need:
            return body, lang, index + 1
        body.append(lines[index])
        index += 1
    return body, lang, index


def _code_language(lang, width):
    token = lang.split()[0].strip("{}") if lang.split() else ""
    if not token or len(token) > 20 or width < 1:
        return ""
    if not all(char.isalnum() or char in "+#.-" for char in token):
        return ""
    return token[:width]


def _code_lines(body, width):
    if not body:
        return []
    indent = "  " if width > 4 else ""
    room = max(width - len(indent), 1)
    out = []
    for raw in body:
        if raw == "":
            out.append("")
            continue
        start = 0
        while start < len(raw):
            out.append(_as_line([(indent + raw[start : start + room], "code")]))
            start += room
    return out


def _take_quote(lines, index):
    block = []
    while index < len(lines):
        found = _QUOTE_RE.match(lines[index])
        if not found:
            break
        block.append(found.group(1))
        index += 1
    return block, index


def _render_quote(block, width):
    inner_width = max(width - 2, 1)
    out = []
    for line in _render_blocks(block, inner_width):
        if line == "":
            out.append(_as_line([("│", "dim")]))
            continue
        if isinstance(line, str):
            out.append(_as_line([("│ ", "dim"), (line, "")]))
        else:
            out.append(_as_line([("│ ", "dim"), *line]))
    return out


def _render_blocks(lines, width):
    out = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "":
            out.append("")
            index += 1
            continue
        fence = _open_fence(line)
        if fence:
            body, lang, index = _take_fence(lines, index, fence)
            label = _code_language(lang, width)
            if body and label:
                out.append(_as_line([(label, "dim")]))
            out.extend(_code_lines(body, width))
            continue
        if _RULE_RE.match(line.strip()):
            out.append(_as_line([("─" * min(width, 24), "dim")]))
            index += 1
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            out.extend(_wrap_styled(_with_style(_parse_inlines(heading.group(2)), "bold"), width, 0))
            index += 1
            continue
        if _QUOTE_RE.match(line):
            block, index = _take_quote(lines, index)
            out.extend(_render_quote(block, width))
            continue
        if _list_match(line):
            block, index = _take_list(lines, index)
            out.extend(_render_list(block, width))
            continue
        if _starts_table(lines, index):
            rows, header, index = _take_table(lines, index)
            out.extend(_render_table(rows, header, width))
            continue
        out.extend(_wrap_styled(_parse_inlines(line), width, 0))
        index += 1
    return out


def _clip_row(line, width):
    if len(line_plain(line)) <= width:
        return line
    spans = [(line, "")] if isinstance(line, str) else line
    return _as_line(_fit_spans(spans, width))


def render_markdown(text, width):
    """Screen lines for one Grok reply.

    Markers such as **bold**, `code`, headings, lists, quotes, links, and
    tables become styles. A line with no style stays a string. Text with no
    markup is wrap(), unchanged. A table that cannot shrink to the width is
    clipped, so a row never draws past the message column.
    """
    width = max(int(width) if isinstance(width, int) else 1, 1)
    if not isinstance(text, str) or text == "":
        return [""]
    if not _MD_HINT.search(text):
        return grid.wrap(text, width)
    lines = _render_blocks(text.splitlines(), width) or [""]
    return [_clip_row(line, width) for line in lines]


def markdown_text(text):
    """The words of a reply, for a one-line preview. Markers are left out."""
    if not isinstance(text, str) or text == "":
        return ""
    parts = []
    for line in render_markdown(text, 400):
        parts.append(line_plain(line).replace("│", " ").replace("─", " "))
    return " ".join(parts)
