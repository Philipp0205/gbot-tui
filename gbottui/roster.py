"""The bot roster and name matching."""

import threading

from . import cache, gbot_cli, icons


ROSTER = {}


def list_bots(avatars="later"):
    """Fetch the bots. `avatars` is "later" (a background thread), "now", or "skip"."""
    payload = gbot_cli.gbot("bots", "list", timeout=30)
    rows = payload if isinstance(payload, list) else []
    bots = []
    for row in rows:
        if isinstance(row, dict) and row.get("name") and row.get("kind", "bot") != "group":
            bots.append(row)
    # A refresh runs beside the UI, so ROSTER is never empty in between.
    fresh = {bot["name"]: bot for bot in bots}
    ROSTER.update(fresh)
    for name in [name for name in ROSTER if name not in fresh]:
        ROSTER.pop(name, None)
    cache.write_names([bot["name"] for bot in bots])
    cache.write_roster(bots)
    if avatars == "later":
        icons.install_avatars_later(bots)
    elif avatars == "now":
        try:
            icons.install_avatars(bots)
        except (OSError, ImportError):
            pass
    return bots


def match_bot(query, names):
    query = query.strip()
    exact = [name for name in names if name == query]
    if exact:
        return exact[0]
    folded = query.casefold()
    same = [name for name in names if name.casefold() == folded]
    if len(same) == 1:
        return same[0]
    if len(same) > 1:
        raise gbot_cli.GbotError("Several bots match:\n" + "\n".join(same))
    prefix = [name for name in names if name.casefold().startswith(folded)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise gbot_cli.GbotError("Several bots match:\n" + "\n".join(prefix))
    raise gbot_cli.GbotError(f'No bot named "{query}".')


def parse_route(text, names):
    """`@bot task` -> (bot name, task). None when the text is not routed.

    A bot name with spaces is written with hyphens: `@Cursor-Cloud-Session`.
    """
    if not text.startswith("@"):
        return None
    head, _, task = text[1:].partition(" ")
    task = task.strip()
    if not head or not task:
        return None
    by_slug = {name.replace(" ", "-"): name for name in names}
    return by_slug[match_bot(head, list(by_slug))], task


def refresh_roster_later():
    def run():
        try:
            list_bots()
        except gbot_cli.GbotError:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def resolve_for_open(query):
    """Match a name from the cached list and refresh it behind the chat.

    With no cache, or a name the cache does not have, the list is loaded
    first, so a direct name still has its tagline.
    """
    cached = cache.read_roster()
    if cached:
        try:
            chosen = match_bot(query, [bot["name"] for bot in cached])
        except gbot_cli.GbotError:
            chosen = None
        if chosen:
            if not ROSTER:
                ROSTER.update({bot["name"]: bot for bot in cached})
            refresh_roster_later()
            return chosen
    try:
        names = [bot["name"] for bot in list_bots()]
    except gbot_cli.GbotError:
        names = cache.read_names()
        if not names:
            raise
    return match_bot(query, names)
