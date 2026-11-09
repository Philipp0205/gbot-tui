#!/usr/bin/env python3
"""A stateful stand-in for `gbot`, for the latency bench. Not used by the tests.

MOCK_STATE  folder for the thread file and the call log
MOCK_DELAY  seconds each call takes, like node startup plus the gateway (default 0.4)
MOCK_REPLY  seconds after a send before the bot's reply is visible (default 1.5)
MOCK_FAIL   every call but `bots list` prints this as a gbot error
MOCK_FAIL_FILE  while this file exists, calls but `bots list` fail with its text
MOCK_HISTORY  entries in a new thread (default 30); `thread --limit N` returns the newest N
MOCK_HANG   seconds each `thread` call hangs, for the loading state (default 0)
MOCK_STREAM seconds a reply keeps growing after it first shows (default 0: whole at once)
MOCK_INCLUSIVE  1: `thread --after <id>` always returns that entry too, unchanged

The text sent picks the reply, for bench/e2e_tui.py:
  "long ..."     a long Markdown reply with headings, a list and a fenced code block
  "approve ..."  a pending approval card
  "widget ..."   a widget with three options
  anything else  "reply to <text>"
While a reply grows it is an `isStreaming` entry, and a `thread --after <its id>`
still returns it, so the poll sees the new text under the same id. It keeps
returning it for GRACE seconds after it stops, so the final text lands.
"""

import json
import os
import sys
import time

STATE = os.environ.get("MOCK_STATE", "/tmp/mock-gbot")
DELAY = float(os.environ.get("MOCK_DELAY", "0.4"))
REPLY = float(os.environ.get("MOCK_REPLY", "1.5"))
HANG = float(os.environ.get("MOCK_HANG", "0") or 0)
STREAM = float(os.environ.get("MOCK_STREAM", "0") or 0)
GRACE = 3.0
INCLUSIVE = os.environ.get("MOCK_INCLUSIVE") == "1"
LONG = """# Trip plan

Here is the plan for the week, with the parts that still need a decision.

## Day by day

- Monday: fly out early and check in by noon, then walk the old town before dinner.
- Tuesday: museum in the morning, market after lunch.
- Wednesday: free day.

1. Book the train
2. Pack light

## Script

```python
def total(prices):
    return sum(prices)
```

That is the **whole** plan. See https://example.com/plan for the map.
""" + "\n".join(f"Extra line {i} of the long answer." for i in range(1, 21)) + "\nEND OF LONG"


def reply_entries(ident, text, at):
    """The bot's entries for one sent text."""
    if text.startswith("approve"):
        return [{
            "id": ident, "kind": "send-message", "at": at,
            "message": {"type": "auto-review-approval", "approval": {
                "status": "pending", "requestId": "req-1", "command": "rm -rf build", "reason": "clean the build"}},
        }]
    if text.startswith("widget"):
        return [{
            "id": ident, "kind": "send-message", "at": at,
            "message": {"type": "widget", "widget": {"prompt": "Pick a city", "options": [
                {"label": "Berlin", "value": "Berlin"}, {"label": "Paris", "value": "Paris"}, {"label": "Rome", "value": "Rome"}]}},
        }]
    body = LONG if text.startswith("long") else "reply to " + text
    return [{"id": ident, "role": "assistant", "text": body, "at": at, "stream": STREAM}]


def shown(entry, now):
    """An entry as the gateway would return it at `now`: a streaming one is cut short."""
    out = {k: v for k, v in entry.items() if k not in ("at", "stream")}
    span = entry.get("stream") or 0
    if span and now < entry["at"] + span:
        words = entry["text"].split(" ")
        keep = max(1, int(len(words) * (now - entry["at"]) / span))
        out["text"] = " ".join(words[:keep])
        out["isStreaming"] = True
    return out
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
        count = int(os.environ.get("MOCK_HISTORY", "30") or 30)
        entries = [{"id": f"m{i}", "role": "user" if i % 2 else "assistant", "text": f"earlier line {i}", "at": 0} for i in range(1, count + 1)]
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
    fail_file = os.environ.get("MOCK_FAIL_FILE")
    if not fail and fail_file:
        try:
            with open(fail_file, encoding="utf-8") as handle:
                fail = handle.read().strip() or "offline"
        except OSError:
            pass
    if fail and args[:1] != ["bots"]:
        print(json.dumps({"error": {"message": fail}}))
        return 1
    if args[:2] == ["bots", "list"]:
        print(json.dumps(BOTS))
        return 0
    if args[:1] == ["thread"]:
        if HANG:
            time.sleep(HANG)
        after = args[args.index("--after") + 1] if "--after" in args else None
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 80
        target = args[-1]
        now = time.time()
        entries = [entry for entry in load(target) if entry.get("at", 0) <= now]
        if after:
            ids = [entry["id"] for entry in entries]
            if after in ids:
                at = ids.index(after)
                # A reply that grew in the last few seconds comes back once more
                # with its final text, so the poll after the last word sees it.
                growing = entries[at].get("stream") and now < entries[at]["at"] + entries[at]["stream"] + GRACE
                entries = entries[at:] if growing or INCLUSIVE else entries[at + 1 :]
        bot = bot_for(target)
        payload = {
            "target": {"id": bot["id"], "name": bot["name"], "avatarColor": bot.get("avatarColor", "")},
            "transcript": {"entries": [shown(e, now) for e in entries][-limit:]},
        }
        print(json.dumps(payload))
        return 0
    if args[:1] == ["send"]:
        target, text = args[args.index("--") + 1], args[args.index("--") + 2]
        entries = load(target)
        count = len(entries)
        now = time.time()
        entries.append({"id": f"m{count + 1}", "role": "user", "text": text, "at": now})
        entries.extend(reply_entries(f"m{count + 2}", text, now + REPLY))
        save(target, entries)
        print(json.dumps({"ok": True}))
        return 0
    if args[:2] == ["approvals", "respond"]:
        target = args[args.index("--target") + 1]
        entry_id = args[args.index("--entry-id") + 1]
        decision = args[args.index("--decision") + 1]
        entries = load(target)
        for entry in entries:
            card = (entry.get("message") or {}).get("approval") if isinstance(entry.get("message"), dict) else None
            if entry.get("id") == entry_id and card:
                card["status"] = "approved" if decision == "accept" else "declined"
        save(target, entries)
        print(json.dumps({"ok": True}))
        return 0
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
