"""Names the Herdr tab after the bot while this pane is the only terminal in it."""

import json
import os
import queue
import subprocess
import threading


def ancestor_pids():
    pids = set()
    pid = os.getpid()
    while pid and pid not in pids and len(pids) < 12:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as status:
                text = status.read()
        except OSError:
            break
        parent = 0
        for line in text.splitlines():
            if line.startswith("PPid:"):
                parent = int(line.split()[1])
        pid = parent
    return pids


def hc_json(*args):
    got = subprocess.run(["hc", *args, "--json"], capture_output=True, text=True, timeout=3)
    return json.loads(got.stdout or "{}")


def live_terminals():
    try:
        data = hc_json("ls")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    infos = []
    for row in data.get("terminals") or []:
        address = row.get("address")
        if not address:
            continue
        try:
            infos.append(hc_json("get", address))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    return infos


def tab_of(info):
    return (info.get("labels") or {}).get("herdr.tab")


def alone_in_tab(info, infos):
    tab = tab_of(info)
    if not tab:
        return False
    mates = [other for other in infos if other.get("state") == "live" and tab_of(other) == tab]
    return len(mates) == 1


def own_terminal(infos=None):
    """This process's Herdr terminal, and whether it is the only one in its tab."""
    if infos is None:
        infos = live_terminals()
    pids = ancestor_pids()
    hits = []
    for info in infos:
        foreground = info.get("foreground") or {}
        if info.get("pid") in pids or foreground.get("pid") in pids:
            hits.append(info)
    if len(hits) != 1:
        return None, False
    info = hits[0]
    return info, alone_in_tab(info, infos)


def rename_tab(info, name):
    """An empty name gives the tab back its number."""
    tab = tab_of(info)
    if not tab or name is None:
        return
    subprocess.run(
        ["herdr-app", "do", "tab.rename", "--id", tab, "--name", name],
        capture_output=True,
        text=True,
        timeout=3,
    )


def herdr_spaces():
    got = subprocess.run(["herdr-app", "ls"], capture_output=True, text=True, timeout=3)
    try:
        return (json.loads(got.stdout or "{}") or {}).get("spaces") or []
    except (json.JSONDecodeError, AttributeError):
        return []


def tab_name(tab, spaces):
    """The tab's own name from `herdr-app ls`, or "" when Herdr shows its number."""
    for space in spaces:
        for position, entry in enumerate(space.get("tabs") or [], start=1):
            if entry.get("id") == tab:
                name = entry.get("name") or ""
                return "" if name == str(position) else name
    return ""


class TabTitle:
    """Names the Herdr tab after the bot and gives it its old name back.

    One worker runs the jobs in order, so a restore never lands before the
    rename it undoes, and the curses loop never waits on herdr-app.
    Finding the tab uses `hc`, not HERDR_ENV, so a Desktop pane is included.
    """

    def __init__(self, find=None, spaces=None, rename=None):
        self.find = find or own_terminal
        self.spaces = spaces or herdr_spaces
        self.rename = rename or rename_tab
        self.jobs = queue.Queue()
        self.lock = threading.Lock()
        self.thread = None
        self.info = None
        self.original = None
        self.shown = None

    def show(self, title):
        self._put("show", title)

    def restore(self):
        self._put("restore", None)

    def flush(self, timeout=3.0):
        with self.lock:
            if self.thread is None:
                return True
        done = threading.Event()
        self._put("flush", done)
        return done.wait(timeout)

    def _put(self, kind, value):
        with self.lock:
            self.jobs.put((kind, value))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def _run(self):
        while True:
            kind, value = self.jobs.get()
            try:
                self._do(kind, value)
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
                pass

    def _do(self, kind, value):
        if kind == "flush":
            value.set()
        elif kind == "show":
            if self.info is None:
                info, alone = self.find()
                if not info or not alone:
                    return
                self.original = tab_name(tab_of(info), self.spaces())
                self.info = info
            if value != self.shown:
                self.rename(self.info, value)
                self.shown = value
        elif kind == "restore":
            if self.info is not None and self.shown is not None:
                self.rename(self.info, self.original)
                self.shown = None


# Swapped in tests, so read it as `tabs.TAB`.
TAB = TabTitle()
