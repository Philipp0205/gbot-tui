#!/usr/bin/env python3
"""Chat with one Grok Bot. Inside Herdr, the pane shows up in the agents list."""

import base64
import curses
import hashlib
import json
import locale
import os
import queue
import re
import select
import subprocess
import sys
import termios
import threading
import time

SOURCE = "gbot-tui"
APP = "gbot-tui"
# shortcut: two empty polls after a reply count as finished, so a bot that
# pauses longer than that looks done while it is still working.
QUIET_POLLS = 2
# Poll cadence in seconds. Each read is one gbot process, so idle backs off.
WAITING_DELAY = 0.3
STREAM_DELAY = 1.0
SETTLE_DELAY = 2.0
BLOCKED_DELAY = 1.0
IDLE_DELAYS = (2.0, 2.0, 3.0, 5.0, 8.0)
# The loop sleeps in select, so a finished read wakes it at once. This cap is
# how late a resize can be noticed.
WAKE_CAP = 0.1

locale.setlocale(locale.LC_ALL, "")


class GbotError(Exception):
    pass


def entry_text(entry):
    if not isinstance(entry, dict):
        return ""
    for key in ("text", "prompt"):
        if isinstance(entry.get(key), str) and entry[key]:
            return entry[key]
    message = entry.get("message")
    if isinstance(message, str) and message:
        return message
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(entry.get("preview"), str):
        return entry["preview"]
    return ""


def entry_role(entry):
    if not isinstance(entry, dict):
        return "msg"
    for key in ("role", "kind", "sender", "type"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return "msg"


def is_user(entry):
    role = entry_role(entry).lower()
    return role in {"user", "human"} or role.endswith(":user")


# Fields gbot copies off a pending auto-review or local-tool card.
DETAIL_KEYS = (
    "summary",
    "description",
    "action",
    "command",
    "reason",
    "target",
    "workingDirectory",
    "surface",
    "machineId",
)


def message_card(entry):
    """The approval or permission object on one entry, whatever its status."""
    if not isinstance(entry, dict) or entry.get("kind") != "send-message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    card = message.get("approval") if kind == "auto-review-approval" else None
    if kind == "local-tool-permission":
        card = message.get("ask")
    if not isinstance(card, dict):
        return None
    return card


def approval_of(entry):
    """The pending card on one transcript entry, or None."""
    card = message_card(entry)
    if card is None or card.get("status") != "pending":
        return None
    details = []
    for key in DETAIL_KEYS:
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            details.append(value.strip())
    entry_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
    request_id = card.get("requestId") if isinstance(card.get("requestId"), str) else ""
    return {"entry_id": entry_id, "request_id": request_id, "details": details}


def request_of(entry):
    card = message_card(entry)
    if card is None:
        return ""
    request_id = card.get("requestId")
    return request_id if isinstance(request_id, str) else ""


def pending_approval(entries):
    return any(approval_of(entry) for entry in entries)


def latest_decision(entries, answered=()):
    """Newest card gbot can answer. Accept and decline need both ids.

    `answered` is the request ids already sent, so a second key cannot
    respond again while a poll still shows the old pending card.
    """
    found = None
    for entry in entries:
        card = approval_of(entry)
        if not card or not card["entry_id"] or not card["request_id"]:
            continue
        if card["request_id"] in answered:
            continue
        found = card
    return found


# Local status written the moment y/n is accepted. The next full read
# replaces it with whatever status the gateway stored.
DECISION_STATUS = {"accept": "approved", "decline": "declined"}

CARD_HEADING = {
    "pending": "Needs approval",
    "accept": "Approved",
    "accepted": "Approved",
    "approve": "Approved",
    "approved": "Approved",
    "allow": "Approved",
    "allow-once": "Approved",
    "allowed": "Approved",
    "decline": "Declined",
    "declined": "Declined",
    "deny": "Declined",
    "denied": "Declined",
    "rejected": "Declined",
}


def card_heading(status):
    folded = status.lower() if isinstance(status, str) else ""
    if folded in CARD_HEADING:
        return CARD_HEADING[folded]
    if not folded:
        return ""
    return folded.replace("-", " ").capitalize()


def card_row_kind(entry):
    """Pending stays a warning. A finished card uses a calmer row kind."""
    heading = card_heading((message_card(entry) or {}).get("status"))
    if heading == "Approved":
        return "card-ok"
    if heading == "Needs approval":
        return "card"
    if heading:
        return "card-no"
    return "card"


def set_card_status(entry, status):
    card = message_card(entry)
    if card is None:
        return False
    card["status"] = status
    return True


def watch_step(phase, new_entries, quiet, saw_other, pending):
    """Return phase, quiet, saw_other after one poll."""
    if pending:
        return "blocked", 0, False
    other = any(not is_user(entry) and entry_text(entry).strip() for entry in new_entries)
    if new_entries:
        quiet = 0
        saw_other = saw_other or other
    else:
        quiet += 1
    if phase == "blocked":
        return "idle", 0, False
    if saw_other and quiet >= QUIET_POLLS and phase in {"working", "idle"}:
        return "done", 0, False
    return phase, quiet, saw_other


def parse_gbot(stdout):
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise GbotError(stdout.strip() or "gbot returned no JSON") from error
    if isinstance(payload, dict) and "error" in payload and "transcript" not in payload and "target" not in payload:
        err = payload["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise GbotError(message or "gbot failed")
    return payload


def last_id(entries):
    for entry in reversed(entries):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            return entry["id"]
    return ""


def parse_thread(payload):
    transcript = payload.get("transcript") if isinstance(payload, dict) else None
    if not isinstance(transcript, dict):
        transcript = payload if isinstance(payload, dict) else {}
    entries = transcript.get("entries")
    if not isinstance(entries, list):
        entries = []
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    if not isinstance(cursor, str) or not cursor:
        cursor = last_id(entries)
    target = payload.get("target") if isinstance(payload, dict) else {}
    if not isinstance(target, dict):
        target = {}
    return {
        "entries": [entry for entry in entries if isinstance(entry, dict)],
        "cursor": cursor,
        "gap_reset": bool(payload.get("gapReset")) if isinstance(payload, dict) else False,
        "name": target.get("name") or "",
        "id": target.get("id") or "",
        "avatar_shape": target.get("avatarShape") or "",
        "avatar_color": target.get("avatarColor") or "",
    }


def osc_report(state, title, kind=None):
    encoded = base64.b64encode(title.encode()).decode()
    if len(encoded) > 256:
        encoded = base64.b64encode(title[:60].encode()).decode()
    parts = [f"state={state}", f"app={APP}", f"title={encoded}"]
    if state == "blocked" and kind:
        parts.append(f"kind={kind}")
    body = ":".join(parts)
    label = title.replace("\x1b", "").replace("\r", " ").replace("\n", " ")
    return f"\033]7501;{body}\033\\\033]0;{label}\033\\"


def resume_arg(text):
    if not text or len(text) > 200:
        return None
    if any(ord(ch) < 32 or ch in "'\\" for ch in text):
        return None
    return text


def herdr_state(phase):
    if phase == "done":
        return "idle"
    if phase in {"idle", "working", "blocked"}:
        return phase
    return "idle"


def report_command(herdr, pane, phase, label, session_id, seq, resume):
    command = [
        herdr,
        "pane",
        "report-agent",
        pane,
        "--source",
        SOURCE,
        "--agent",
        label,
        "--state",
        herdr_state(phase),
        "--seq",
        str(seq),
    ]
    if session_id:
        command += ["--agent-session-id", session_id]
    if resume:
        command += ["--", "gbot-tui", resume]
    return command


def release_command(herdr, pane, label, seq):
    return [
        herdr,
        "pane",
        "release-agent",
        pane,
        "--source",
        SOURCE,
        "--agent",
        label,
        "--seq",
        str(seq),
    ]


def gbot_argv(args):
    # gbot rejects --json before the subcommand. Keep it before a "--" message.
    if "--" in args:
        split = args.index("--")
        return ["gbot", *args[:split], "--gateway", "--json", *args[split:]]
    return ["gbot", *args, "--gateway", "--json"]


def gbot_failure(stdout, stderr):
    for raw in (stdout, stderr):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            if message:
                return message
        return text
    return "gbot failed"


def gbot(*args, timeout=60):
    env = os.environ.copy()
    # gbot 0.12.4 tells Cursor it is Grok Bot 0.20.0, which the server rejects.
    env.setdefault("SAND_CLIENT_VERSION", sand_client_version())
    try:
        proc = subprocess.run(
            gbot_argv(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise GbotError("gbot timed out") from error
    except FileNotFoundError as error:
        raise GbotError("gbot is not on PATH") from error
    if proc.returncode != 0:
        raise GbotError(gbot_failure(proc.stdout, proc.stderr))
    return parse_gbot(proc.stdout)


ROSTER = {}

# The Grok Bot app's avatar colors. A shape mask is tinted with one of these.
AVATAR_COLORS = {
    "black": "#000000",
    "brown": "#936439",
    "red": "#FF263C",
    "orange": "#FF6700",
    "yellow": "#FF9800",
    "green": "#00C972",
    "cyan": "#00BCA6",
    "blue": "#1084FE",
    "violet": "#9159FE",
    "magenta": "#FF309B",
    "gray": "#777777",
}


def avatar_color(name):
    return AVATAR_COLORS.get((name or "").lower(), "#777777")


def program_key(bot_id):
    # Herdr throws away an OSC 7501 report whose app is longer than 32 bytes.
    safe = "".join(ch for ch in str(bot_id) if ch.isalnum() or ch == "-")
    key = f"gbot-{safe}" if safe else ""
    if len(key) <= 32:
        return key
    compact = "".join(ch for ch in safe if ch != "-")
    return ("gbot-" + compact)[:32]


def program_ids(bot_id):
    """The OSC key, then the full gbot-<id> when programs.json already has both."""
    primary = program_key(bot_id)
    safe = "".join(ch for ch in str(bot_id) if ch.isalnum() or ch == "-")
    full = f"gbot-{safe}" if safe else ""
    keys = []
    for key in (primary, full):
        if key and key not in keys:
            keys.append(key)
    return keys


def list_bots(avatars="later"):
    """Fetch the bots. `avatars` is "later" (a background thread), "now", or "skip"."""
    payload = gbot("bots", "list", timeout=30)
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
    write_names([bot["name"] for bot in bots])
    write_roster(bots)
    if avatars == "later":
        install_avatars_later(bots)
    elif avatars == "now":
        try:
            install_avatars(bots)
        except (OSError, ImportError):
            pass
    return bots


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


AVATAR_LOCK = threading.Lock()


def install_avatars_later(bots, done=None):
    """Compose Herdr icons off the UI thread. The AppImage mount can take seconds."""
    bots = [dict(bot) for bot in bots]

    def run():
        made = {}
        try:
            with AVATAR_LOCK:
                made = install_avatars(bots)
        except (OSError, ImportError, ValueError):
            made = {}
        if done:
            done(made)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def show_avatar_later(bot):
    """Set APP once the icon exists. The UI loop re-reports the pane when APP changes."""

    def done(made):
        global APP
        key = program_key(bot.get("id") or "")
        if key in made:
            APP = key

    return install_avatars_later([bot], done)


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


def herdr_root():
    return os.path.join(os.path.expanduser("~"), ".config", "herdr-app")


def compose_avatar(_mask_path, face_path, color):
    """Tint the resting face frame. The strip's black RGB is not drawn.

    Frame 0 of bot-<shape>-face.png is 18×18. Its alpha is the filled body
    and the eyes are holes. Pasting that black frame onto the mask only
    leaves a rim on Herdr's dark tile. Herdr's program icon is one still
    path, so this is the rest pose and not the other 28 frames.
    """
    from PIL import Image

    face = Image.open(face_path).convert("RGBA")
    frame = face.crop((0, 0, min(18, face.width), min(18, face.height)))
    size = 64
    resample = getattr(getattr(Image, "Resampling", Image), "NEAREST")
    frame = frame.resize((size, size), resample)
    rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
    painted = Image.new("RGBA", frame.size)
    src = frame.load()
    dst = painted.load()
    for y in range(frame.height):
        for x in range(frame.width):
            dst[x, y] = (*rgb, src[x, y][3])
    import io

    out = io.BytesIO()
    painted.save(out, format="PNG")
    return out.getvalue()


def client_version_from_name(name):
    parts = os.path.basename(name).split("_")
    if len(parts) >= 3 and parts[2][:1].isdigit():
        return parts[2]
    return ""


def sand_client_version():
    app = grok_appimage()
    if app:
        found = client_version_from_name(os.path.realpath(app))
        if found:
            return found
    return "0.78.1"


def grok_appimage():
    link = os.path.expanduser("~/.local/share/grok-bot/appimage")
    if os.path.exists(link):
        return link
    folder = os.path.expanduser("~/Applications")
    if not os.path.isdir(folder):
        return None
    names = sorted(name for name in os.listdir(folder) if name.startswith("Grok_Bot_") and name.endswith(".AppImage"))
    return os.path.join(folder, names[-1]) if names else None


def avatar_src():
    cache = os.path.join(os.path.expanduser("~/.cache/gbot-tui"), "avatar-src")
    if os.path.exists(os.path.join(cache, "bot-heart.png")):
        return cache
    if extract_avatar_sources(cache):
        return cache
    return None


def extract_avatar_sources(cache):
    import json as _json
    import struct

    app = grok_appimage()
    if not app:
        return False
    try:
        offset = subprocess.check_output([app, "--appimage-offset"], text=True, timeout=10).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    mount = os.path.join(cache, "mnt")
    os.makedirs(mount, exist_ok=True)
    mounted = subprocess.run(
        ["squashfuse", "-o", f"offset={offset}", app, mount],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if mounted.returncode != 0:
        return False
    try:
        asar = os.path.join(mount, "resources", "app.asar")
        data = open(asar, "rb").read()
        header_size = struct.unpack_from("<I", data, 4)[0]
        header = _json.loads(data[16 : 16 + header_size].split(b"\0", 1)[0])
        aligned = (16 + header_size + 3) & ~3
        os.makedirs(cache, exist_ok=True)

        def walk(node):
            for name, meta in node.get("files", {}).items():
                if "files" in meta:
                    walk(meta)
                    continue
                if not (name.startswith("bot-") and name.endswith(".png") and "@2x" not in name):
                    continue
                start = aligned + int(meta["offset"]) - 8
                blob = data[start : start + meta["size"]]
                if blob.startswith(b"\x89PNG"):
                    with open(os.path.join(cache, name), "wb") as handle:
                        handle.write(blob)

        walk(header)
    except (OSError, ValueError, KeyError):
        return False
    finally:
        for unmount in (["fusermount3", "-u", mount], ["fusermount", "-u", mount]):
            done = subprocess.run(unmount, capture_output=True, text=True)
            if done.returncode == 0:
                break
    return os.path.exists(os.path.join(cache, "bot-heart.png"))


def install_avatars(bots, root=None, src=None):
    """Write one Herdr program icon per bot. Herdr matches it from the 7501 app name."""
    root = root or herdr_root()
    src = src or avatar_src()
    if not src:
        return {}
    path = os.path.join(root, "programs.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                table = json.loads(handle.read())
        else:
            table = {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(table, dict):
        return {}
    programs = table.get("programs")
    if not isinstance(programs, dict):
        programs = {}
        table["programs"] = programs
    made = {}
    changed = False
    for bot in bots:
        keys = program_ids(bot.get("id") or "")
        key = keys[0] if keys else ""
        shape = "".join(ch for ch in str(bot.get("avatarShape") or bot.get("avatar_shape") or "") if ch.isalnum())
        color_name = "".join(ch for ch in str(bot.get("avatarColor") or bot.get("avatar_color") or "") if ch.isalnum())
        if not key or not shape:
            continue
        mask = os.path.join(src, f"bot-{shape}.png")
        face = os.path.join(src, f"bot-{shape}-face.png")
        if not (os.path.exists(mask) and os.path.exists(face)):
            continue
        rel = f"icons/gbot/{key}-{shape}-{color_name}.png"
        dest = os.path.join(root, rel)
        try:
            png = compose_avatar(mask, face, avatar_color(color_name))
        except (OSError, ValueError, ImportError):
            continue
        try:
            with open(dest, "rb") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current != png:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(png)
        entry = {
            "name": bot.get("name") or key,
            "icon": rel,
            "tint": False,
            "tile": "#111111",
            "color": avatar_color(color_name),
            "match": [key],
        }
        made[key] = entry
        for slot in keys:
            slot_entry = dict(entry, match=[slot])
            made[slot] = slot_entry
            if programs.get(slot) != slot_entry:
                programs[slot] = slot_entry
                changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(table, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    return made


def show_avatar(bot, root=None, src=None):
    global APP
    made = install_avatars([bot], root, src)
    key = program_key(bot.get("id") or "")
    if key in made:
        APP = key


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
        raise GbotError("Several bots match:\n" + "\n".join(same))
    prefix = [name for name in names if name.casefold().startswith(folded)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise GbotError("Several bots match:\n" + "\n".join(prefix))
    raise GbotError(f'No bot named "{query}".')


def parse_args(argv):
    if argv in (["-h"], ["--help"]):
        return "help", ""
    if argv in (["--list"], ["-l"]):
        return "list", ""
    if argv == ["--complete"]:
        return "complete", ""
    return "open", " ".join(argv).strip()


def read_thread(target, after=None):
    args = ["thread", "--limit", "80"]
    if after:
        args += ["--after", after]
    args.append(target)
    return parse_thread(gbot(*args, timeout=30))


def send_message(target, text):
    gbot("send", "--", target, text, timeout=90)


def respond_approval(target, entry_id, request_id, decision):
    # gbot approvals respond rechecks the card, then resolves it once.
    gbot(
        "approvals",
        "respond",
        "--target",
        target,
        "--entry-id",
        entry_id,
        "--request-id",
        request_id,
        "--decision",
        decision,
        timeout=30,
    )


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


def live_terminals():
    try:
        listed = subprocess.run(
            ["hc", "ls", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        data = json.loads(listed.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    infos = []
    for row in data.get("terminals") or []:
        address = row.get("address")
        if not address:
            continue
        try:
            got = subprocess.run(
                ["hc", "get", address, "--json"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            infos.append(json.loads(got.stdout or "{}"))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    return infos


def alone_in_tab(info, infos):
    tab = (info.get("labels") or {}).get("herdr.tab")
    if not tab:
        return False
    mates = [
        other
        for other in infos
        if other.get("state") == "live" and (other.get("labels") or {}).get("herdr.tab") == tab
    ]
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
    tab = (info.get("labels") or {}).get("herdr.tab")
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
                tab = (info.get("labels") or {}).get("herdr.tab")
                self.original = tab_name(tab, self.spaces())
                self.info = info
            if value != self.shown:
                self.rename(self.info, value)
                self.shown = value
        elif kind == "restore":
            if self.info is not None and self.shown is not None:
                self.rename(self.info, self.original)
                self.shown = None


TAB = TabTitle()
# The window title while the bot list is up.
LIST_TITLE = "gbot"


def write_tty(data):
    try:
        tty = os.open("/dev/tty", os.O_WRONLY)
    except OSError:
        tty = None
    try:
        if tty is None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            os.write(tty, data)
    except OSError:
        pass
    finally:
        if tty is not None:
            os.close(tty)


def emit(state, title, kind=None):
    write_tty(osc_report(state, title, kind).encode())


def window_title(text):
    label = text.replace("\x1b", "").replace("\r", " ").replace("\n", " ")
    write_tty(f"\033]0;{label}\033\\".encode())


# xterm's title stack: push on start, pop on exit. Terminals without it ignore both.
PUSH_TITLE = b"\033[22;0t"
POP_TITLE = b"\033[23;0t"


def tell_herdr(phase, label, session_id, resume):
    herdr = os.environ.get("HERDR_BIN_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not herdr or not pane:
        return
    seq = time.time_ns() // 1_000_000
    command = report_command(herdr, pane, phase, label, session_id, seq, resume)

    def run():
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    threading.Thread(target=run, daemon=True).start()


def silence_herdr(label):
    herdr = os.environ.get("HERDR_BIN_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not herdr or not pane:
        return
    seq = time.time_ns() // 1_000_000
    subprocess.run(
        release_command(herdr, pane, label, seq),
        capture_output=True,
        text=True,
        timeout=2,
    )


def publish(phase, title, session_id, resume):
    kind = "permission" if phase == "blocked" else None
    osc_state = "idle" if phase == "idle" else phase
    emit(osc_state, title, kind)
    try:
        tell_herdr(phase, title, session_id, resume)
    except (OSError, subprocess.TimeoutExpired):
        pass


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


class Chat:
    def __init__(self, target):
        self.target = target
        self.title = target
        self.session_id = ""
        self.messages = []
        self.cursor = ""
        self.phase = "idle"
        self.quiet = 0
        self.saw_other = False
        self.scroll = 0
        self.follow = True
        self.typed = ""
        self.error = ""
        self.resume = resume_arg(target)
        bot = ROSTER.get(target) or {}
        self.avatar_color = bot.get("avatarColor") or bot.get("avatar_color") or ""
        self.avatar_shape = bot.get("avatarShape") or bot.get("avatar_shape") or ""
        self.label = (bot.get("title") or bot.get("tagline") or "").strip()
        self.deciding = False
        self.answered = {}
        self.pending_request = ""
        self.full_reload = False
        self.replacing = False
        self.rows_rev = 0
        self.loaded = False
        self.idle_polls = 0
        self.app_seen = APP

    def show_cached(self, entries):
        """Paint the last thread seen while the first read is still out.

        Phase stays idle, so y/n cannot answer a card from the cache.
        """
        if entries and not self.messages:
            self.messages = list(entries)
            self.touch_rows()

    def poll_delay(self):
        """Seconds until the next thread read.

        Fast while a reply is coming, slower once it settles, so two quiet
        polls still span about four seconds. An idle chat backs off to
        IDLE_DELAYS[-1]; a key press brings it back to the first step.
        """
        if self.phase == "blocked":
            return BLOCKED_DELAY
        if self.phase == "working":
            if not self.saw_other:
                return WAITING_DELAY
            return STREAM_DELAY if self.quiet == 0 else SETTLE_DELAY
        return IDLE_DELAYS[min(self.idle_polls, len(IDLE_DELAYS) - 1)]

    def nudge(self):
        self.idle_polls = 0

    def note(self, phase):
        if phase != self.phase:
            self.phase = phase
            publish(phase, self.title, self.session_id, self.resume)

    def apply(self, page, replace):
        if page["name"]:
            self.title = page["name"]
            self.resume = resume_arg(page["name"]) or resume_arg(page["id"]) or self.resume
        if page["id"]:
            self.session_id = page["id"]
        if page.get("avatar_shape"):
            self.avatar_shape = page["avatar_shape"]
        if page.get("avatar_color"):
            self.avatar_color = page["avatar_color"]
        if replace or page["gap_reset"]:
            pending = [entry for entry in self.messages if entry.get("local")]
            self.messages = list(page["entries"])
            for entry in pending:
                if not self._covers(entry):
                    self.messages.append(entry)
            self.cursor = page["cursor"]
            fresh = []
        else:
            fresh = list(page["entries"])
            for entry in fresh:
                self._absorb(entry)
            if page["cursor"]:
                self.cursor = page["cursor"]
        self._stamp_answered()
        self.touch_rows()
        return fresh

    def _covers(self, local):
        text = entry_text(local).strip()
        return any(
            is_user(entry) and not entry.get("local") and entry_text(entry).strip() == text
            for entry in self.messages
        )

    def _take_local(self, entry):
        if not is_user(entry):
            return False
        text = entry_text(entry).strip()
        if not text:
            return False
        for index, local in enumerate(self.messages):
            if local.get("local") and entry_text(local).strip() == text:
                self.messages[index] = entry
                return True
        return False

    def _absorb(self, entry):
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(entry_id, str) and entry_id:
            for index, existing in enumerate(self.messages):
                if existing.get("id") == entry_id:
                    self.messages[index] = entry
                    return
        if self._take_local(entry):
            return
        index = next((i for i, local in enumerate(self.messages) if local.get("local")), None)
        if index is None:
            self.messages.append(entry)
        else:
            self.messages.insert(index, entry)

    def _stamp_answered(self):
        """A poll that still says pending must not undo a decision just sent."""
        for entry in self.messages:
            decision = self.answered.get(request_of(entry))
            if not decision:
                continue
            card = message_card(entry)
            if card is not None and card.get("status") == "pending":
                set_card_status(entry, DECISION_STATUS[decision])

    def touch_rows(self):
        self.rows_rev += 1
        self._rows_key = None

    def begin_decision(self, decision):
        """Mark the newest card decided and return the gbot arguments.

        None means this request was already answered, or there is nothing to answer.
        """
        card = latest_decision(self.messages, self.answered)
        if not card or decision not in DECISION_STATUS:
            return None
        request_id = card["request_id"]
        self.answered[request_id] = decision
        status = DECISION_STATUS[decision]
        for entry in self.messages:
            if entry.get("id") == card["entry_id"] or request_of(entry) == request_id:
                set_card_status(entry, status)
        self.pending_request = request_id
        self.deciding = True
        self.touch_rows()
        if self.phase == "blocked" and not pending_approval(self.messages):
            self.note("idle")
        return (self.target, card["entry_id"], request_id, decision)

    def finish_decision(self):
        """The respond call returned. The next read is the whole thread."""
        self.deciding = False
        self.pending_request = ""
        self.full_reload = True

    def fail_decision(self):
        """The respond call failed. Put the card back so it can be retried."""
        request_id = self.pending_request
        self.deciding = False
        self.pending_request = ""
        if request_id:
            self.answered.pop(request_id, None)
            for entry in self.messages:
                if request_of(entry) == request_id:
                    set_card_status(entry, "pending")
            self.touch_rows()
            if pending_approval(self.messages) and self.phase != "blocked":
                self.note("blocked")

    def take_read(self):
        """Arguments for the next `gbot thread` call.

        After a decision the following read drops `--after` once, so the
        gateway can return the card with its new status. Later polls resume
        from the cursor.
        """
        if self.full_reload:
            self.full_reload = False
            self.replacing = True
            return (self.target,)
        if self.cursor:
            return (self.target, self.cursor)
        return (self.target,)

    def read_failed(self):
        if self.replacing:
            self.replacing = False
            self.full_reload = True

    def ingest(self, page, first=False):
        replace = first or not self.cursor or page["gap_reset"] or self.replacing
        self.replacing = False
        if replace:
            self.apply(page, replace=True)
            fresh = []
            if self.phase == "working" and any(
                not is_user(entry) and entry_text(entry).strip() for entry in self.messages
            ):
                self.saw_other = True
        else:
            fresh = self.apply(page, replace=False)
        if fresh or replace:
            self.idle_polls = 0
        elif self.phase in {"idle", "done"}:
            self.idle_polls += 1
        if first:
            self.loaded = True
            show_avatar_later(page)
            if pending_approval(self.messages):
                phase = "blocked"
            elif self.phase == "working":
                phase = "working"
            else:
                phase = "idle"
            self.phase = phase
            publish(phase, self.title, self.session_id, self.resume)
            TAB.show(self.title)
            return
        phase, self.quiet, self.saw_other = watch_step(
            self.phase,
            fresh,
            self.quiet,
            self.saw_other,
            pending_approval(self.messages),
        )
        self.note(phase)

    def arm_send(self):
        text = self.typed.strip()
        if not text:
            return ""
        self.typed = ""
        self.error = ""
        self.saw_other = False
        self.quiet = 0
        self.follow = True
        self.messages.append({"role": "user", "text": text, "local": True})
        self.note("working")
        return text


THEME_ENV = "GBOT_TUI_THEME"
# The background the terminal reported to an OSC 11 query, as "#RRGGBB".
PROBED = {"bg": None}


def parse_osc11(data):
    """The color in an OSC 11 reply ("rgb:RRRR/GGGG/BBBB" or "#RRGGBB"), or None."""
    if isinstance(data, bytes):
        data = data.decode("latin-1")
    start = data.find("\033]11;")
    if start < 0:
        return None
    body = data[start + 5 :]
    end = min((i for i in (body.find("\007"), body.find("\033\\")) if i >= 0), default=-1)
    if end < 0:
        return None
    value = body[:end].strip()
    if value.startswith("#") and len(value) == 7:
        channels = [value[1:3], value[3:5], value[5:7]]
    elif value.startswith(("rgb:", "rgba:")):
        channels = value.split(":", 1)[1].split("/")[:3]
    else:
        return None
    if len(channels) != 3:
        return None
    out = []
    for channel in channels:
        if not channel or len(channel) > 4 or any(ch not in "0123456789abcdefABCDEF" for ch in channel):
            return None
        out.append(round(int(channel, 16) * 255 / (16 ** len(channel) - 1)))
    return "#{:02X}{:02X}{:02X}".format(*out)


def is_light(hex_color):
    return contrast(hex_color, "#000000") > contrast(hex_color, "#FFFFFF")


def theme_choice(override, probed, colorfgbg):
    """("dark" | "light", where it came from). First of: GBOT_TUI_THEME, OSC 11, COLORFGBG."""
    folded = (override or "").strip().lower()
    if folded in {"dark", "light"}:
        return folded, THEME_ENV
    if probed:
        return ("light" if is_light(probed) else "dark"), "osc11"
    # COLORFGBG is "fg;bg". 7 and 15 are the light backgrounds.
    bg = (colorfgbg or "").split(";")[-1]
    if bg.isdigit():
        return ("light" if int(bg) in {7, 15} else "dark"), "COLORFGBG"
    return "dark", "default"


def dark_terminal():
    choice, _source = theme_choice(os.environ.get(THEME_ENV), PROBED["bg"], os.environ.get("COLORFGBG"))
    return choice == "dark"


DA1_REPLY = re.compile(rb"\033\[\?[0-9;]*c")


def probe_background(fd=None, timeout=0.2):
    """Ask the terminal for its background with OSC 11.

    A device-attributes query (DA1) goes right after it. Terminals answer in
    order and nearly all answer DA1, so its reply means OSC 11 is not coming
    and the wait ends early. Runs before curses, so the replies are not read
    as keys.
    """
    owned = fd is None
    if owned:
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError:
            return None
    try:
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return None
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, raw)
            os.write(fd, b"\033]11;?\033\\\033[c")
            data = b""
            deadline = time.monotonic() + timeout
            while not DA1_REPLY.search(data):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _w, _x = select.select([fd], [], [], left)
                if not ready:
                    break
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                data += chunk
            return parse_osc11(data)
        except OSError:
            return None
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except termios.error:
                pass
    finally:
        if owned:
            os.close(fd)


def detect_theme():
    """Probe once per run, unless GBOT_TUI_THEME already says light or dark."""
    if (os.environ.get(THEME_ENV) or "").strip().lower() in {"dark", "light"}:
        return
    PROBED["bg"] = probe_background()


def background_hex():
    if PROBED["bg"]:
        return PROBED["bg"]
    return "#000000" if dark_terminal() else "#FFFFFF"


XTERM_LEVELS = (0, 95, 135, 175, 215, 255)


def xterm_hex(index):
    """The RGB the 256-color palette shows for one index (16 and up)."""
    if index >= 232:
        level = 8 + 10 * (index - 232)
        return f"#{level:02X}{level:02X}{level:02X}"
    index -= 16
    red, green, blue = index // 36, (index // 6) % 6, index % 6
    return f"#{XTERM_LEVELS[red]:02X}{XTERM_LEVELS[green]:02X}{XTERM_LEVELS[blue]:02X}"


def nearest_gray(level):
    level = max(8, min(238, level))
    return 232 + int(round((level - 8) / 10))


def user_bubble():
    """The desktop app's user bubble: ansi 235 on a dark terminal, 254 on a light one.

    With a probed background the bubble is a step off that exact color, so a
    gray theme still shows it.
    """
    dark = dark_terminal()
    probed = PROBED["bg"]
    if not probed or (is_light(probed) == dark):
        return 235 if dark else 254
    gray = round(sum(int(probed[i : i + 2], 16) for i in (1, 3, 5)) / 3)
    return nearest_gray(gray + 0x22 if dark else gray - 0x1C)


def bubble_fg(bubble):
    """Bubble text set outright, so a wrong theme guess cannot give dark on dark."""
    return 234 if is_light(xterm_hex(bubble)) else 255


def theme_palette(dark):
    """256-color indices for the named styles. Yellow on white is unreadable."""
    if dark:
        return {"green": 108, "gray": 245}
    return {"yellow": 130, "red": 124, "green": 28, "gray": 240}


def xterm_color(hex_color):
    red = int(hex_color[1:3], 16)
    green = int(hex_color[3:5], 16)
    blue = int(hex_color[5:7], 16)
    if max(red, green, blue) - min(red, green, blue) < 12:
        if red < 8:
            return 16
        if red > 248:
            return 231
        return 232 + int(round((red - 8) / 247 * 24))
    return 16 + 36 * int(round(red / 51.0)) + 6 * int(round(green / 51.0)) + int(round(blue / 51.0))


AVATAR_ATTR = {}


def avatar_attr(name):
    return AVATAR_ATTR.get((name or "").lower(), ATTR["default"])


def _srgb(channel):
    value = channel / 255
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _luminance(red, green, blue):
    return 0.2126 * _srgb(red) + 0.7152 * _srgb(green) + 0.0722 * _srgb(blue)


def _hex_luminance(hex_color):
    return _luminance(int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))


def contrast(first, second):
    a, b = _hex_luminance(first), _hex_luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def contrast_on_black(hex_color):
    return contrast(hex_color, "#000000")


# Bot text on a dark background aims higher, as before. 4.5 is WCAG AA body text.
DARK_TARGET = 7
LIGHT_TARGET = 4.5


def readable_hex(hex_color, dark=True, background=None):
    """Move a bot color away from the background until it reads. Icons keep the original.

    Lighten on a dark terminal, darken on a light one. The check uses the
    256-color cell the terminal will actually draw.
    """
    background = background or ("#000000" if dark else "#FFFFFF")
    target = DARK_TARGET if dark else LIGHT_TARGET
    red = int(hex_color[1:3], 16)
    green = int(hex_color[3:5], 16)
    blue = int(hex_color[5:7], 16)
    for _ in range(16):
        exact = f"#{red:02X}{green:02X}{blue:02X}"
        shown = xterm_hex(xterm_color(exact))
        if min(contrast(exact, background), contrast(shown, background)) >= target:
            break
        if dark:
            red = min(255, red + max(1, (255 - red) // 4))
            green = min(255, green + max(1, (255 - green) // 4))
            blue = min(255, blue + max(1, (255 - blue) // 4))
        else:
            red = max(0, red - max(1, red // 5))
            green = max(0, green - max(1, green // 5))
            blue = max(0, blue - max(1, blue // 5))
    return f"#{red:02X}{green:02X}{blue:02X}"


TEXT_ATTR = {}


def bot_text_attr(name):
    # Desktop 1:1 messages use the bot's color. Black would vanish on a dark terminal.
    folded = (name or "").lower()
    if folded == "black" and dark_terminal():
        return ATTR["default"]
    if folded in TEXT_ATTR:
        return TEXT_ATTR[folded]
    return avatar_attr(name)


def dot_attr(name):
    """The bot mark in the list and in the chat. One lightened color for both."""
    return bot_text_attr(name)


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
    for key in DETAIL_KEYS:
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            lines.extend(wrap(value.strip(), room))
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
            user = is_user(entry)
            wrapped = wrap(text, user_w if user else text_w)
            if user:
                span = max(len(line) for line in wrapped)
                for line in wrapped:
                    rows.append(("user", line.ljust(span)))
            else:
                for line in wrapped:
                    rows.append(("text", line))
        if lines:
            if text:
                rows.append(("gap", ""))
            kind = card_row_kind(entry)
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


# -1 keeps the terminal background. A filled curses color is the block this CLI does not use.
ATTR = {
    "default": 0,
    "dim": curses.A_DIM,
    "bold": curses.A_BOLD,
    "user": curses.A_BOLD,
    "yellow": curses.A_BOLD,
    "red": curses.A_BOLD,
    # Calmer than the pending warning. Theme replaces these with real colors.
    "green": curses.A_DIM,
    "gray": curses.A_DIM,
}

CARD_STYLE = {"card": "yellow", "card-ok": "green", "card-no": "gray"}


def apply_theme(stdscr):
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, -1, -1)
        base = curses.color_pair(1)
        stdscr.bkgd(" ", base)
        ATTR["default"] = base
        ATTR["dim"] = base | curses.A_DIM
        ATTR["bold"] = base | curses.A_BOLD
        if curses.COLORS >= 256:
            bubble = user_bubble()
            curses.init_pair(2, bubble_fg(bubble), bubble)
            ATTR["user"] = curses.color_pair(2)
            AVATAR_ATTR.clear()
            TEXT_ATTR.clear()
            dark = dark_terminal()
            background = background_hex()
            for index, (name, hex_color) in enumerate(AVATAR_COLORS.items()):
                curses.init_pair(10 + index, xterm_color(hex_color), -1)
                AVATAR_ATTR[name] = curses.color_pair(10 + index)
                curses.init_pair(30 + index, xterm_color(readable_hex(hex_color, dark, background)), -1)
                TEXT_ATTR[name] = curses.color_pair(30 + index)
        else:
            ATTR["user"] = ATTR["bold"]
            AVATAR_ATTR.clear()
            TEXT_ATTR.clear()
        palette = theme_palette(dark_terminal()) if curses.COLORS >= 256 else {}
        curses.init_pair(3, palette.get("yellow", curses.COLOR_YELLOW), -1)
        curses.init_pair(4, palette.get("red", curses.COLOR_RED), -1)
        ATTR["yellow"] = curses.color_pair(3)
        ATTR["red"] = curses.color_pair(4)
        if curses.COLORS >= 256:
            curses.init_pair(5, palette["green"], -1)
            curses.init_pair(6, palette["gray"], -1)
            ATTR["green"] = curses.color_pair(5)
            ATTR["gray"] = curses.color_pair(6)
        else:
            curses.init_pair(5, curses.COLOR_GREEN, -1)
            curses.init_pair(6, curses.COLOR_WHITE, -1)
            ATTR["green"] = curses.color_pair(5)
            ATTR["gray"] = curses.color_pair(6) | curses.A_DIM
    except curses.error:
        return


def put(stdscr, y, x, text, attr=0):
    if not text:
        return
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    room = width - x - (1 if y == height - 1 else 0)
    if room <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, room, attr)
    except curses.error:
        pass


def span_attr(style):
    """Named text styles stay named. A bot color uses a `bot:` prefix.

    "red" and "yellow" are both avatar colors and the warning styles. The
    footer asks for the warning style by those names. The bot list asks for
    the avatar color as `bot:red`, which is the lightened bot color.
    """
    if isinstance(style, str) and style.startswith("bot:"):
        return bot_text_attr(style[4:])
    if style in ATTR:
        return ATTR[style]
    return avatar_attr(style)


def paint_spans(stdscr, y, x, spans):
    col = x
    for text, style in spans:
        put(stdscr, y, col, text, span_attr(style))
        col += len(text)


def paint_composer(stdscr, y, width, lines, placeholder=False):
    span = max(width, 0)
    if span < 2:
        return
    inner = span - 2
    attr = ATTR["dim"] if placeholder else ATTR["default"]
    for index, line in enumerate(lines):
        put(stdscr, y + index, 0, "│", ATTR["default"])
        prefix = " →  " if index == 0 else "    "
        put(stdscr, y + index, 1, (prefix + line)[:inner], attr)
        put(stdscr, y + index, span - 1, "│", ATTR["default"])


def draw(stdscr, chat):
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    lines = draft_lines(chat.typed, width)
    header, body_h, composer_h, footer_h = layout(height, len(lines) or 1)
    footer = []
    if footer_h:
        decide = bool(latest_decision(chat.messages, chat.answered)) and not chat.deciding
        footer = footer_rows(chat.phase, chat.error, decide=decide, width=width)
        # Error rows come out of the transcript, which keeps at least one row.
        footer = footer[-max(min(len(footer), body_h), 1) :]
        body_h -= len(footer) - 1
    color = getattr(chat, "avatar_color", "")
    label = getattr(chat, "label", "")
    if header:
        put(stdscr, 0, 1, "●", dot_attr(color))
        put(stdscr, 0, 3, clip(chat.title, max(width - 4, 0)), ATTR["bold"])
        if header > 1 and label:
            put(stdscr, 1, 3, clip(label, max(width - 4, 0)), ATTR["dim"])
    gap = header_gap(header, label, body_h)
    room = max(body_h - gap, 0)
    rows = visible_rows(chat, width)
    if chat.follow:
        chat.scroll = max(len(rows) - room, 0)
    start = min(chat.scroll, max(len(rows) - room, 0))
    for row in range(room):
        index = start + row
        if index >= len(rows):
            continue
        kind, text = rows[index]
        y = header + gap + row
        previous = rows[index - 1][0] if index else ""
        if kind == "user":
            shown = " " + text + " "
            put(stdscr, y, bubble_column(width, text), shown, ATTR["user"])
        elif kind == "text":
            if previous != "text":
                put(stdscr, y, 1, "●", dot_attr(color))
            put(stdscr, y, 3, text, bot_text_attr(color))
        elif kind in CARD_STYLE:
            put(stdscr, y, 1, clip(text, max(width - 2, 0)), ATTR[CARD_STYLE[kind]])
    composer_at = header + body_h
    cursor = None
    if composer_h >= 3:
        top, _mid, bot = composer_lines(width, chat.typed)
        put(stdscr, composer_at, 0, top, ATTR["default"])
        visible = composer_h - 2
        shown = lines[-visible:] if lines else ["Add a follow-up"]
        paint_composer(stdscr, composer_at + 1, width, shown, placeholder=not lines)
        put(stdscr, composer_at + composer_h - 1, 0, bot, ATTR["default"])
        last = "" if not lines else shown[-1]
        column = composer_cursor_column(last, placeholder=not lines)
        cursor = (composer_at + len(shown), min(column, max(width - 2, 0)))
    elif composer_h == 1:
        shown = chat.typed or "Add a follow-up"
        put(
            stdscr,
            composer_at,
            0,
            clip("→  " + shown, max(width - 1, 0)),
            ATTR["default"] if chat.typed else ATTR["dim"],
        )
        column = 2 if not chat.typed else 3 + len(chat.typed)
        cursor = (composer_at, min(column, max(width - 2, 0)))
    for offset, spans in enumerate(footer):
        paint_spans(stdscr, height - len(footer) + offset, 1, spans)
    # The footer write leaves the cursor on "scroll". Put it back in the box.
    if cursor:
        try:
            stdscr.move(*cursor)
        except curses.error:
            pass
    stdscr.refresh()


def place_cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def composer_key(chat, key):
    """What y or n does in the composer.

    Lowercase y and n answer a card only when it has both ids and the box is
    empty. Shift-Y and Shift-N are text, and ctrl-y / ctrl-n insert the
    lowercase letter, so a message can still start with y or n. A card that
    cannot be answered does not swallow the key.
    """
    if key == "\x19":
        return ("type", "y")
    if key == "\x0e":
        return ("type", "n")
    if key not in ("y", "Y", "n", "N"):
        return None
    if (
        not chat.typed
        and not chat.deciding
        and chat.phase == "blocked"
        and key in ("y", "n")
        and latest_decision(chat.messages, chat.answered)
    ):
        return ("decide", "accept" if key == "y" else "decline")
    if chat.deciding and not chat.typed and key in ("y", "n"):
        return ("ignore", "")
    return ("type", key)


def fast_escape():
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        return


def chat_key(chat, key, stdscr):
    """Apply one key. Returns "quit", "back", a job tuple, or None."""
    if key == curses.KEY_RESIZE:
        return None
    if key in ("\x03", "\x04"):
        return "quit"
    if key in ("\x1b", 27):
        return "back"
    chat.nudge()
    if key == curses.KEY_PPAGE:
        chat.follow = False
        chat.scroll = max(chat.scroll - max(curses.LINES - 4, 1), 0)
        return None
    if key == curses.KEY_NPAGE:
        chat.scroll += max(curses.LINES - 4, 1)
        height, width = stdscr.getmaxyx()
        page_header, body_h, _composer, _footer = layout(height, len(draft_lines(chat.typed, width)) or 1)
        room = max(body_h - header_gap(page_header, chat.label, body_h), 0)
        rows = visible_rows(chat, width)
        if chat.scroll >= max(len(rows) - room, 0):
            chat.follow = True
        return None
    if key in ("\n", "\r", curses.KEY_ENTER):
        text = chat.arm_send()
        if not text:
            return None
        chat.follow = True
        return ("send", (chat.target, text))
    handled = composer_key(chat, key)
    if handled is not None:
        action, value = handled
        if action == "ignore":
            return None
        if action == "decide":
            args = chat.begin_decision(value)
            if not args:
                chat.typed += "y" if value == "accept" else "n"
                return None
            chat.error = ""
            return ("decide", args)
        chat.typed += value
        return None
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        chat.typed = chat.typed[:-1]
        return None
    if isinstance(key, str) and key.isprintable():
        chat.typed += key
    return None


def read_keys(stdscr):
    """Every key curses has now. A paste is one redraw, not one per character."""
    keys = []
    while len(keys) < 4096:
        try:
            keys.append(stdscr.get_wch())
        except curses.error:
            break
    return keys


def chat_loop(stdscr, chat):
    apply_theme(stdscr)
    fast_escape()
    place_cursor(True)
    stdscr.keypad(True)
    # gbot's network calls used to run here, so typing froze until each one returned.
    # Keys are read without waiting; the loop sleeps in select on the tty and a pipe.
    stdscr.timeout(0)
    jobs = queue.Queue()
    done = queue.Queue()
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)

    def finish(item):
        done.put(item)
        try:
            os.write(wake_w, b"x")
        except OSError:
            pass

    def worker():
        # One worker, so a send never overtakes the read before it.
        while True:
            job = jobs.get()
            if job is None:
                # The worker owns the write end, so a late result never lands
                # on a descriptor number the next chat has reused.
                os.close(wake_w)
                return
            kind, args = job
            try:
                if kind == "read":
                    finish(("page", read_thread(*args)))
                elif kind == "decide":
                    respond_approval(*args)
                    finish(("decided", None))
                else:
                    send_message(*args)
                    finish(("sent", None))
            except GbotError as error:
                finish(("error", (kind, str(error))))

    threading.Thread(target=worker, daemon=True).start()
    jobs.put(("read", (chat.target,)))
    waiting = 1
    next_poll = 0
    dirty = True
    try:
        tty = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        tty = None
    try:
        while True:
            try:
                while os.read(wake_r, 4096):
                    pass
            except OSError:
                pass
            while True:
                try:
                    kind, payload = done.get_nowait()
                except queue.Empty:
                    break
                waiting -= 1
                dirty = True
                if kind == "error":
                    source, message = payload
                    chat.error = message
                    if source == "decide":
                        chat.fail_decision()
                    if source == "send" and chat.phase == "working":
                        chat.note("idle")
                    if source == "read":
                        chat.read_failed()
                        next_poll = time.monotonic() + IDLE_DELAYS[0]
                elif kind in {"sent", "decided"}:
                    if kind == "decided":
                        chat.finish_decision()
                    next_poll = 0
                elif not chat.loaded:
                    chat.ingest(payload, first=True)
                    write_thread_cache(chat.target, chat.messages)
                    next_poll = time.monotonic() + chat.poll_delay()
                else:
                    chat.ingest(payload, first=False)
                    next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
            if chat.app_seen != APP:
                chat.app_seen = APP
                publish(chat.phase, chat.title, chat.session_id, chat.resume)
            if not chat.label and chat.target in ROSTER:
                # A list refresh behind a named open can bring the tagline late.
                bot = ROSTER.get(chat.target) or {}
                label = (bot.get("title") or bot.get("tagline") or "").strip()
                if label:
                    chat.label = label
                    chat.avatar_color = chat.avatar_color or bot.get("avatarColor") or ""
                    dirty = True
            now = time.monotonic()
            if waiting == 0 and now >= next_poll and (chat.loaded or chat.error):
                jobs.put(("read", chat.take_read()))
                waiting += 1
                next_poll = now + chat.poll_delay()
            if dirty:
                draw(stdscr, chat)
                dirty = False
            keys = read_keys(stdscr)
            if not keys:
                wait = WAKE_CAP
                if waiting == 0 and (chat.loaded or chat.error):
                    wait = min(WAKE_CAP, max(next_poll - time.monotonic(), 0))
                watch = [wake_r] + ([tty] if tty is not None else [])
                try:
                    select.select(watch, [], [], wait)
                except (OSError, ValueError):
                    time.sleep(min(wait, 0.02))
                keys = read_keys(stdscr)
            for key in keys:
                dirty = True
                result = chat_key(chat, key, stdscr)
                if result == "quit":
                    return None
                if result == "back":
                    return "back"
                if isinstance(result, tuple):
                    if result[0] == "send":
                        # The bubble is on screen before gbot starts.
                        draw(stdscr, chat)
                        dirty = False
                    jobs.put(result)
                    waiting += 1
                    if result[0] == "send":
                        next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
    except KeyboardInterrupt:
        return None
    finally:
        jobs.put(None)
        write_thread_cache(chat.target, chat.messages)
        os.close(wake_r)


def fuzzy_score(query, name):
    """Higher is a tighter match. None means the letters are not there in order."""
    query = query.casefold()
    if not query:
        return 0
    name = name.casefold()
    found = name.find(query)
    if found >= 0:
        return 1000 - found + (50 if found == 0 else 0)
    at = 0
    score = 0
    run = False
    for index, char in enumerate(name):
        if at < len(query) and char == query[at]:
            score += 3 if index == 0 or not name[index - 1].isalnum() else 1
            if run:
                score += 2
            run = True
            at += 1
        else:
            run = False
    if at != len(query):
        return None
    return score


def picker_spans(bot, selected, width):
    """One sidebar row: selection mark, avatar, name, and a dim tagline."""
    title = (bot.get("title") or bot.get("tagline") or "").strip()
    name = bot["name"]
    name_style = "bold" if selected else "default"
    lead = "→ " if selected else "  "
    lead_style = "bold" if selected else "dim"
    color = (bot.get("avatarColor") or bot.get("avatar_color") or "").lower()
    room = max(width - 7, 1)
    # The same lightened color the chat mark uses, including red and yellow,
    # which are also the names of the warning styles.
    mark_style = "bot:" + color if color in AVATAR_COLORS else (color or "default")
    mark = ("● ", mark_style)
    if not title:
        return [(lead, lead_style), mark, (clip(name, room), name_style)]
    name_room = min(len(name), max(room // 2, 1))
    title_room = max(room - min(len(name), name_room) - 5, 1)
    return [
        (lead, lead_style),
        mark,
        (clip(name, name_room), name_style),
        ("  ·  ", "dim"),
        (clip(title, title_room), "dim"),
    ]


def fuzzy_bots(query, bots):
    if not query:
        return list(bots)
    ranked = []
    for index, bot in enumerate(bots):
        scores = [fuzzy_score(query, bot.get("name") or "")]
        title = bot.get("title") or ""
        if title:
            scores.append(fuzzy_score(query, title))
        best = max((score for score in scores if score is not None), default=None)
        if best is not None:
            ranked.append((-best, index, bot))
    ranked.sort()
    return [bot for _score, _index, bot in ranked]


class Loader:
    """Run one gbot call on a thread. The UI polls `ready` instead of waiting."""

    def __init__(self, call):
        self.result = None
        self.error = None
        self.ready = False
        self.lock = threading.Lock()

        def run():
            try:
                value = call()
                with self.lock:
                    self.result = value
            except GbotError as error:
                with self.lock:
                    self.error = str(error)
            with self.lock:
                self.ready = True

        threading.Thread(target=run, daemon=True).start()

    def take(self):
        with self.lock:
            return self.ready, self.result, self.error


def pick(stdscr, bots, refresh=None):
    """The bot list. `refresh` fetches a newer list on a thread while this one is shown."""
    # The chat loop reads keys without waiting. Leaving that on makes get_wch
    # raise "no input" the moment this list draws.
    stdscr.timeout(-1)
    apply_theme(stdscr)
    fast_escape()
    place_cursor(False)
    stdscr.keypad(True)
    bots = list(bots)
    loader = Loader(refresh) if refresh else None
    if loader:
        stdscr.timeout(30)
    note = ""
    selected = 0
    query = ""
    while True:
        if loader:
            ready, fresh, error = loader.take()
            if ready:
                loader = None
                stdscr.timeout(-1)
                if error and not bots:
                    raise GbotError(error)
                if error:
                    note = error
                elif fresh is not None:
                    if not fresh and not bots:
                        raise GbotError("No bots.")
                    current = fuzzy_bots(query, bots)
                    name = current[selected]["name"] if current and selected < len(current) else ""
                    bots = list(fresh)
                    shown = fuzzy_bots(query, bots)
                    names = [bot["name"] for bot in shown]
                    selected = names.index(name) if name in names else 0
        shown = fuzzy_bots(query, bots)
        if not shown:
            selected = 0
        elif selected >= len(shown):
            selected = len(shown) - 1
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        put(stdscr, 0, 1, "Grok bots", ATTR["bold"])
        if query:
            put(stdscr, 1, 3, clip("/" + query, max(width - 4, 0)), ATTR["dim"])
        footer = 1 if height >= 4 else 0
        body_h = max(height - 2 - footer, 0)
        top = 0
        if body_h and selected >= body_h:
            top = selected - body_h + 1
        if not shown and body_h:
            empty = "  loading bots…" if loader and not bots else "  no match"
            put(stdscr, 2, 1, clip(empty, max(width - 2, 0)), ATTR["dim"])
        for index, bot in enumerate(shown[top : top + body_h]):
            actual = top + index
            paint_spans(stdscr, 2 + index, 1, picker_spans(bot, actual == selected, width))
        if footer:
            if note:
                spans = [(clip(note, max(width - 2, 0)), "red")]
            else:
                spans = [("type to search · enter open · ctrl-c quit", "dim")]
            paint_spans(stdscr, height - 1, 1, spans)
        stdscr.refresh()
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        if key in ("\x03", "\x04"):
            return None
        if key in ("\x1b", 27):
            query = ""
            selected = 0
            continue
        if key == curses.KEY_UP and selected > 0:
            selected -= 1
            continue
        if key == curses.KEY_DOWN and selected < len(shown) - 1:
            selected += 1
            continue
        if key in ("\n", "\r", curses.KEY_ENTER):
            if shown:
                return shown[selected]["name"]
            continue
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            query = query[:-1]
            selected = 0
            continue
        if isinstance(key, str) and key.isprintable():
            query += key
            selected = 0


def start_chat(target):
    """A chat with the last thread already on screen. The real read is the loop's first job."""
    bot = ROSTER.get(target)
    if bot:
        show_avatar_later(bot)
    chat = Chat(target)
    chat.show_cached(read_thread_cache(target))
    publish("idle", target, "", chat.resume)
    return chat


def leave_chat(chat, final):
    emit("clear", chat.title)
    TAB.restore()
    if not final:
        window_title(LIST_TITLE)

    def release():
        try:
            silence_herdr(chat.title)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if final:
        release()
    else:
        # Back to the list should not wait on the herdr CLI.
        threading.Thread(target=release, daemon=True).start()


def open_chat(target):
    chat = start_chat(target)
    back = None
    try:
        back = curses.wrapper(lambda stdscr: chat_loop(stdscr, chat))
        return back
    finally:
        leave_chat(chat, True)


def screens(stdscr, target):
    """The list and the chat share one curses session, so Esc and Enter do not flash the shell."""
    first = True
    while True:
        if not target:
            cached = list(ROSTER.values()) or read_roster()
            if cached and not ROSTER:
                ROSTER.update({bot["name"]: bot for bot in cached})
            refresh = list_bots if first or not cached else (lambda: list_bots(avatars="skip"))
            target = pick(stdscr, cached, refresh=refresh)
            first = False
            if not target:
                return 0
        chat = start_chat(target)
        back = None
        try:
            back = chat_loop(stdscr, chat)
        finally:
            leave_chat(chat, back != "back")
        if back != "back":
            return 0
        target = ""


USAGE = """\
gbot-tui [name]
gbot-tui --list

Open a chat with one Grok bot. A name opens that bot; a unique prefix works too.
With no name, pick from the list. --list prints every bot.
Run it inside a Herdr tab and that bot shows up in the agents list.
"""


def print_bots():
    try:
        bots = list_bots(avatars="now")
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    if not bots:
        print("No bots.", file=sys.stderr)
        return 1
    for bot in bots:
        print(bot["name"])
    return 0


def complete_names():
    names = read_names(NAMES_TTL)
    if not names:
        try:
            names = [bot["name"] for bot in list_bots(avatars="skip")]
        except GbotError:
            names = read_names()
    for name in names:
        print(name)
    return 0 if names else 1


def session(target):
    write_tty(PUSH_TITLE)
    if not target:
        window_title(LIST_TITLE)
    try:
        return curses.wrapper(lambda stdscr: screens(stdscr, target))
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        TAB.restore()
        TAB.flush()
        write_tty(POP_TITLE)


def refresh_roster_later():
    def run():
        try:
            list_bots()
        except GbotError:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def resolve_for_open(query):
    """Match a name from the cached list and refresh it behind the chat.

    With no cache, or a name the cache does not have, the list is loaded
    first, so a direct name still has its tagline.
    """
    cached = read_roster()
    if cached:
        try:
            chosen = match_bot(query, [bot["name"] for bot in cached])
        except GbotError:
            chosen = None
        if chosen:
            if not ROSTER:
                ROSTER.update({bot["name"]: bot for bot in cached})
            refresh_roster_later()
            return chosen
    try:
        names = [bot["name"] for bot in list_bots()]
    except GbotError:
        names = read_names()
        if not names:
            raise
    return match_bot(query, names)


def open_named(query):
    try:
        chosen = resolve_for_open(query)
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    return session(chosen)


def main(argv):
    action, target = parse_args(argv)
    if action == "help":
        print(USAGE, end="")
        return 0
    if action == "list":
        return print_bots()
    if action == "complete":
        return complete_names()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("gbot-tui needs a terminal.", file=sys.stderr)
        return 1
    detect_theme()
    if not target:
        try:
            return session("")
        except KeyboardInterrupt:
            return 130
    try:
        return open_named(target)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
