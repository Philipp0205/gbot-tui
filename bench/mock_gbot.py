#!/usr/bin/env python3
"""A stateful stand-in for `gbot`, for the latency bench. Not used by the tests.

MOCK_STATE  folder for the thread file and the call log
MOCK_DELAY  seconds each call takes, like node startup plus the gateway (default 0.4)
MOCK_REPLY  seconds after a send before the bot's reply is visible (default 1.5)
"""

import json
import os
import sys
import time

STATE = os.environ.get("MOCK_STATE", "/tmp/mock-gbot")
DELAY = float(os.environ.get("MOCK_DELAY", "0.4"))
REPLY = float(os.environ.get("MOCK_REPLY", "1.5"))
BOTS = [
    {"id": "a1", "name": "Researcher", "title": "digs into papers", "avatarColor": "blue", "avatarShape": "heart"},
    {"id": "a2", "name": "Travel Agent", "title": "books the trip", "avatarColor": "red", "avatarShape": "heart"},
    {"id": "a3", "name": "Chef", "title": "what to cook", "avatarColor": "orange", "avatarShape": "heart"},
]


def thread_path(target):
    return os.path.join(STATE, "thread-" + "".join(ch for ch in target if ch.isalnum()) + ".json")


def load(target):
    try:
        with open(thread_path(target), encoding="utf-8") as handle:
            return json.load(handle)
    except OSError:
        entries = [{"id": f"m{i}", "role": "user" if i % 2 else "assistant", "text": f"earlier line {i}", "at": 0} for i in range(1, 31)]
        entries[-1]["text"] = "earlier reply"
        return entries


def save(target, entries):
    with open(thread_path(target), "w", encoding="utf-8") as handle:
        json.dump(entries, handle)


def bot_for(target):
    return next((bot for bot in BOTS if bot["name"] == target), {"id": "x", "name": target})


def main(argv):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "calls.log"), "a", encoding="utf-8") as log:
        log.write(f"{time.time():.3f} {' '.join(argv)}\n")
    time.sleep(DELAY)
    args = [arg for arg in argv if arg not in ("--gateway", "--json")]
    fail = os.environ.get("MOCK_FAIL")
    if fail and args[:1] != ["bots"]:
        print(json.dumps({"error": {"message": fail}}))
        return 1
    if args[:2] == ["bots", "list"]:
        print(json.dumps(BOTS))
        return 0
    if args[:1] == ["thread"]:
        after = args[args.index("--after") + 1] if "--after" in args else None
        target = args[-1]
        now = time.time()
        entries = [entry for entry in load(target) if entry.get("at", 0) <= now]
        if after:
            ids = [entry["id"] for entry in entries]
            entries = entries[ids.index(after) + 1 :] if after in ids else entries
        bot = bot_for(target)
        payload = {
            "target": {"id": bot["id"], "name": bot["name"], "avatarColor": bot.get("avatarColor", "")},
            "transcript": {"entries": [{k: v for k, v in e.items() if k != "at"} for e in entries][-80:]},
        }
        print(json.dumps(payload))
        return 0
    if args[:1] == ["send"]:
        target, text = args[args.index("--") + 1], args[args.index("--") + 2]
        entries = load(target)
        count = len(entries)
        now = time.time()
        entries.append({"id": f"m{count + 1}", "role": "user", "text": text, "at": now})
        entries.append({"id": f"m{count + 2}", "role": "assistant", "text": "reply to " + text, "at": now + REPLY})
        save(target, entries)
        print(json.dumps({"ok": True}))
        return 0
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
