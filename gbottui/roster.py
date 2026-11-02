"""The account's bots: fetched from gbot, cached on disk, matched by name."""

import threading

from . import cache, gbot, icons
from .gbot import GbotError

# Swapped in place by a refresh, so the UI never sees it empty.
ROSTER = {}


def bot_label(bot):
    """The tagline shown beside a bot's name."""
    return (bot.get("title") or bot.get("tagline") or "").strip()


def seed_roster(bots):
    """Fill an empty ROSTER from the cached list, so a chat opens before gbot answers."""
    if bots and not ROSTER:
        ROSTER.update({bot["name"]: bot for bot in bots})


def list_bots(avatars="later"):
    """Fetch the bots. `avatars` is "later" (a background thread), "now", or "skip"."""
    payload = gbot.gbot("bots", "list", timeout=30)
    rows = payload if isinstance(payload, list) else []
    bots = [row for row in rows if isinstance(row, dict) and row.get("name") and row.get("kind", "bot") != "group"]
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


def refresh_roster_later():
    def run():
        try:
            list_bots()
        except GbotError:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def match_bot(query, names):
    query = query.strip()
    exact = [name for name in names if name == query]
    if exact:
        return exact[0]
    folded = query.casefold()
    for found in (
        [name for name in names if name.casefold() == folded],
        [name for name in names if name.casefold().startswith(folded)],
    ):
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise GbotError("Several bots match:\n" + "\n".join(found))
    raise GbotError(f'No bot named "{query}".')


def resolve_for_open(query):
    """Match a name from the cached list and refresh it behind the chat.

    With no cache, or a name the cache does not have, the list is loaded
    first, so a direct name still has its tagline.
    """
    cached = cache.read_roster()
    if cached:
        try:
            chosen = match_bot(query, [bot["name"] for bot in cached])
        except GbotError:
            chosen = None
        if chosen:
            seed_roster(cached)
            refresh_roster_later()
            return chosen
    try:
        names = [bot["name"] for bot in list_bots()]
    except GbotError:
        names = cache.read_names()
        if not names:
            raise
    return match_bot(query, names)
