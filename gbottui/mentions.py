"""@-mentions in the composer: finding the query at the cursor, fuzzy ranking, inserting.

Pure text functions, no curses and no chat state. The caller owns the draft,
the cursor and which bots are on offer.
"""

MAX_ROWS = 6


def query_at(text, pos):
    """(start, query) of the `@query` that ends at the cursor, or None.

    The `@` has to open the draft or follow whitespace, so an email such as
    `a@b` is not a mention. The query is everything after the `@` up to the
    cursor and holds no whitespace.
    """
    pos = len(text) if pos is None else max(0, min(pos, len(text)))
    start = pos
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:pos]
    if not token.startswith("@"):
        return None
    return start, token[1:]


def _tightest(query, name):
    """Indexes of `query` as a subsequence of `name` with the smallest span, or None."""
    best = None
    for first, char in enumerate(name):
        if char != query[0]:
            continue
        found = [first]
        at = first
        for want in query[1:]:
            at = name.find(want, at + 1)
            if at < 0:
                break
            found.append(at)
        else:
            if best is None or found[-1] - found[0] < best[-1] - best[0]:
                best = found
    return best


def match(query, name):
    """(rank, matched indexes) or None when `query` is not a subsequence of `name`.

    Case-insensitive. The rank sorts best first: a prefix, then the start of a
    later word, then a subsequence with the smaller span.
    """
    low, want = name.lower(), query.lower()
    if not want:
        return (0, 0), []
    if low.startswith(want):
        return (0, 0), list(range(len(want)))
    for index in range(1, len(low)):
        if not low[index - 1].isalnum() and low[index:].startswith(want):
            return (1, 0), list(range(index, index + len(want)))
    found = _tightest(want, low)
    if found is None:
        return None
    return (2, found[-1] - found[0]), found


def rank(query, names, limit=MAX_ROWS):
    """The best `limit` names as (name, matched indexes), best first, ties by name."""
    scored = []
    for name in names:
        hit = match(query, name)
        if hit is not None:
            scored.append((hit[0], name.lower(), name, hit[1]))
    scored.sort(key=lambda item: item[:3])
    return [(name, positions) for _rank, _low, name, positions in scored[:limit]]


def suggest(text, pos, names, limit=MAX_ROWS):
    """(start, query, matches) for the mention at the cursor, or None when there is none."""
    found = query_at(text, pos)
    if found is None:
        return None
    return found[0], found[1], rank(found[1], names, limit)


def insert(text, pos, name):
    """Replace the `@query` at the cursor with `@Name ` -> (text, cursor)."""
    found = query_at(text, pos)
    if found is None:
        return text, len(text) if pos is None else pos
    start = found[0]
    end = len(text) if pos is None else max(0, min(pos, len(text)))
    mention = f"@{name} "
    return text[:start] + mention + text[end:], start + len(mention)
