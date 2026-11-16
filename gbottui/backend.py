"""Calls to the Cursor backend: login token, unread state, mark read, sidebar sections."""

import os
import subprocess
import threading
import time

from . import activity, cache, gbot_cli


def token_expired(token, now=None):
    """True when a Cursor JWT's exp is in the past. Unreadable tokens count as live."""
    try:
        import base64
        import json as _json

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return float(exp) <= (time.time() if now is None else now)
    except (IndexError, ValueError, KeyError, TypeError):
        return False


BACKEND = "https://api2.cursor.sh/aiserver.v1.GrokBotService/"
TOKEN_JS = (
    'const c=require("crypto"),f=require("fs"),o=require("os");'
    'const k=c.pbkdf2Sync("peanuts","saltysalt",1,16,"sha1");'
    'const d=b=>{const e=Buffer.from(b,"base64");'
    'const x=c.createDecipheriv("aes-128-cbc",k,Buffer.alloc(16,32));'
    'return Buffer.concat([x.update(e.subarray(3)),x.final()]).toString().trim()};'
    'const s=JSON.parse(f.readFileSync(o.homedir()+"/.config/Grok Bot/sand-secrets.json","utf8"));'
    'const a=JSON.parse(s["cursor-accounts"]);const n=a.accounts[a.active];'
    'process.stdout.write(d(n["cursor-access-token"]))'
)
# Per-agent state the desktop app keeps on the server: id -> {"unread", "latest", "running"}.
# "running" is True, False, or None (unknown).
SERVER = {}


def backend_token():
    token = os.environ.get("CURSOR_ACCESS_TOKEN", "")
    if token and not token_expired(token):
        return token
    try:
        proc = subprocess.run(["node", "-e", TOKEN_JS], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _varint(value):
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _proto_fields(data):
    """(field, value) pairs of one protobuf message. Varints are ints, the rest bytes."""
    fields = []
    i = 0
    while i < len(data):
        key = shift = 0
        while True:
            byte = data[i]
            i += 1
            key |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                break
        kind = key & 7
        if kind == 1 or kind == 5:
            # Fixed-width fields (doubles, floats) are not read here. Skip them.
            i += 8 if kind == 1 else 4
            if i > len(data):
                raise IndexError("truncated fixed-width field")
            continue
        if kind == 0 or kind == 2:
            value = shift = 0
            while True:
                byte = data[i]
                i += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if byte < 0x80:
                    break
            if kind == 2:
                value, i = data[i : i + value], i + value
        else:
            raise ValueError("unsupported wire type")
        fields.append((key >> 3, value))
    return fields


def backend_call(method, body, timeout=15):
    import urllib.request
    import uuid

    token = backend_token()
    if not token:
        raise gbot_cli.GbotError("no Cursor token")
    request = urllib.request.Request(
        BACKEND + method,
        data=body,
        headers={
            "authorization": "Bearer " + token,
            "content-type": "application/proto",
            "x-ghost-mode": "true",
            "x-request-id": str(uuid.uuid4()),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except OSError as error:
        raise gbot_cli.GbotError(str(error)) from error


def _running_of(status_flag, live, now_ms):
    """True / False / None (unknown) from a status message and its live state."""
    if status_flag:
        return True
    state = dict(_proto_fields(live))
    updated = state.get(7)
    stale_after = state.get(8)
    if (
        isinstance(updated, int)
        and isinstance(stale_after, int)
        and updated
        and stale_after > 0
        and now_ms > updated + stale_after
    ):
        return None
    return any(bool(state.get(number)) for number in (2, 3, 4, 13))


def parse_agent_statuses(data, now_ms=None):
    """id -> {"unread", "latest", "running"}. `running` is True, False, or None when unknown."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    found = {}
    for field, status in _proto_fields(data):
        if field != 1 or not isinstance(status, bytes):
            continue
        parts = _proto_fields(status)
        ids = [v for f, v in parts if f == 1 and isinstance(v, bytes)]
        live = next((v for f, v in parts if f == 2 and isinstance(v, bytes)), b"")
        client = next((v for f, v in parts if f == 3 and isinstance(v, bytes)), b"")
        flag = next((v for f, v in parts if f == 4 and isinstance(v, int)), 0)
        if not ids:
            continue
        state = dict(_proto_fields(client))
        unread = state.get(3)
        latest = state.get(4)
        found[ids[0].decode("utf-8", "replace")] = {
            "unread": unread if isinstance(unread, int) else 0,
            "latest": latest if isinstance(latest, int) else 0,
            "running": _running_of(bool(flag), live, now_ms),
        }
    return found


def _text(value):
    """A protobuf bytes value as a clean string, or None when it is not plain text."""
    if not isinstance(value, bytes):
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text or not text.isprintable():
        return None
    return text


def _strings(data, field=None):
    """Every plain string in one message. `field` limits it to that field number."""
    out = []
    for number, value in _proto_fields(data):
        text = _text(value)
        if text is not None and (field is None or number == field):
            out.append(text)
    return out


def _pinned_ids(data):
    """Agent ids in the pinned message. Field 1 is the likely one, so any string is the fallback."""
    try:
        return _strings(data, 1) or _strings(data)
    except (ValueError, IndexError):
        return []


def parse_runtime_settings(data):
    """{"pinned": [agent id], "sections": [{"id", "name", "agent_ids"}]} from the response.

    Only fields 6 (pinned agents) and 7 (sidebar sections) of the settings are read.
    """
    pinned = []
    sections = []
    for field, settings in _proto_fields(data):
        if field != 1 or not isinstance(settings, bytes):
            continue
        for number, value in _proto_fields(settings):
            if not isinstance(value, bytes):
                continue
            if number == 6:
                pinned.extend(_pinned_ids(value))
            elif number == 7:
                for inner, raw in _proto_fields(value):
                    if inner != 1 or not isinstance(raw, bytes):
                        continue
                    section = {"id": "", "name": "", "agent_ids": []}
                    for part, item in _proto_fields(raw):
                        text = _text(item)
                        if text is None:
                            continue
                        if part == 1 and not section["id"]:
                            section["id"] = text
                        elif part == 2 and not section["name"]:
                            section["name"] = text
                        elif part == 3:
                            section["agent_ids"].append(text)
                    sections.append(section)
    return {"pinned": pinned, "sections": sections}


def clean_sections(data):
    """The saved or parsed layout in its one shape. Anything else is no sections."""
    empty = {"pinned": [], "sections": []}
    if not isinstance(data, dict):
        return empty

    def ids(value):
        return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []

    sections = []
    for raw in data.get("sections") if isinstance(data.get("sections"), list) else []:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") if isinstance(raw.get("name"), str) else ""
        ident = raw.get("id") if isinstance(raw.get("id"), str) else ""
        sections.append({"id": ident, "name": name, "agent_ids": ids(raw.get("agent_ids"))})
    return {"pinned": ids(data.get("pinned")), "sections": sections}


# The sidebar layout the desktop app keeps on the server: pinned ids and named sections.
SECTIONS = {"pinned": [], "sections": []}


def current_sections():
    with activity.STATE_LOCK:
        return SECTIONS


def set_sections(layout):
    global SECTIONS
    with activity.STATE_LOCK:
        SECTIONS = clean_sections(layout)


def load_sections_cache():
    """Show the last known sections at once. Never replaces a layout already fetched."""
    layout = clean_sections(cache.read_json(cache.sections_path()))
    with activity.STATE_LOCK:
        fetched = bool(SECTIONS["pinned"] or SECTIONS["sections"])
    if not fetched:
        set_sections(layout)


def refresh_sections():
    """Read-only. Best effort: with no token, offline or a bad answer the old layout stays."""
    try:
        layout = parse_runtime_settings(backend_call("GetGrokBotUserRuntimeSettings", b""))
    except (gbot_cli.GbotError, ValueError, IndexError):
        return
    set_sections(layout)
    cache.write_private(cache.sections_path(), current_sections())


def refresh_server_state(bots):
    """Best effort. The list falls back to its own reads when this fails."""
    threading.Thread(target=refresh_sections, daemon=True).start()
    ids = [bot["id"] for bot in bots if isinstance(bot.get("id"), str) and bot["id"]]
    if not ids:
        return
    body = b"".join(b"\x0a" + _varint(len(raw)) + raw for raw in (i.encode() for i in ids))
    try:
        found = parse_agent_statuses(backend_call("ListGrokBotAgentStatuses", body))
    except (gbot_cli.GbotError, ValueError, IndexError):
        return
    with activity.STATE_LOCK:
        SERVER.update(found)


def running_flag(agent_id):
    """The server's running flag for one agent: True, False, or None when unknown."""
    with activity.STATE_LOCK:
        known = SERVER.get(agent_id or "")
        return known.get("running") if known else None


def refresh_running(agent_id):
    """Read-only. Ask for one agent's status and store it. Best effort, like refresh_server_state.

    A failed call makes the flag unknown (None), so an old True or False never sticks.
    """
    if not isinstance(agent_id, str) or not agent_id:
        return
    body = b"\x0a" + _varint(len(agent_id.encode())) + agent_id.encode()
    try:
        found = parse_agent_statuses(backend_call("ListGrokBotAgentStatuses", body))
    except (gbot_cli.GbotError, ValueError, IndexError):
        found = {}
    with activity.STATE_LOCK:
        if not found and agent_id in SERVER:
            SERVER[agent_id]["running"] = None
        for ident, state in found.items():
            # The chat is open, so a bot not known yet starts read.
            known = SERVER.setdefault(ident, {"unread": 0, "latest": state["latest"]})
            known["running"] = state["running"]


def clear_running(agent_id):
    """Make one agent's running flag unknown. A send makes the last answer stale."""
    with activity.STATE_LOCK:
        known = SERVER.get(agent_id or "")
        if known:
            known["running"] = None


def mark_read_later(agent_id):
    """Tell the server this chat was read, as the desktop app does when it opens one."""
    with activity.STATE_LOCK:
        known = SERVER.get(agent_id)
        if not known or not known["unread"]:
            return
        known["unread"] = 0
    body = b"\x0a" + _varint(len(agent_id.encode())) + agent_id.encode() + b"\x10\x01"

    def run():
        try:
            backend_call("SetGrokBotAgentClientState", body)
        except gbot_cli.GbotError:
            pass

    threading.Thread(target=run, daemon=True).start()
