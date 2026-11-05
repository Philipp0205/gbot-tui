"""Files under ~/.cache/gbot-tui and the private JSON writer."""

import hashlib
import json
import os
import time


def cache_dir():
    return os.path.expanduser("~/.cache/gbot-tui")


ROSTER_KEYS = ("id", "name", "title", "tagline", "avatarColor", "avatarShape", "kind")


def roster_path():
    return os.path.join(cache_dir(), "roster.json")


def write_private(path, data):
    """Write a cache file only this user can read. Transcripts can be private."""
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        return


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_roster(bots):
    rows = [{key: bot[key] for key in ROSTER_KEYS if key in bot} for bot in bots]
    write_private(roster_path(), rows)


def read_roster():
    """The last bot list, so the picker and a named open paint before gbot answers."""
    rows = read_json(roster_path())
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]]


THREAD_KEEP = 80


def thread_cache_path(target):
    digest = hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir(), "threads", digest + ".json")


def write_thread_cache(target, messages):
    kept = [entry for entry in messages if isinstance(entry, dict) and not entry.get("local")]
    write_private(thread_cache_path(target), {"target": target, "entries": kept[-THREAD_KEEP:]})


def read_thread_cache(target):
    data = read_json(thread_cache_path(target))
    if not isinstance(data, dict) or data.get("target") != target:
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _save_locked(path, data):
    write_private(path, data)


# shortcut: tab completion reuses this file for 10 minutes, refresh sooner if bots are renamed often.
NAMES_TTL = 600


def names_path():
    return os.path.join(os.path.expanduser("~/.cache/gbot-tui"), "bots")


def read_names(max_age=None):
    path = names_path()
    try:
        if max_age is not None and time.time() - os.path.getmtime(path) > max_age:
            return []
        with open(path, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line]
    except OSError:
        return []


def write_names(names):
    path = names_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(names))
            if names:
                handle.write("\n")
    except OSError:
        return
