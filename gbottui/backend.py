"""Calls to the Cursor backend: login token, unread state, mark read."""

import os
import subprocess
import threading
import time

from . import activity, gbot_cli


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
# Per-agent read state the desktop app keeps on the server: id -> {"unread", "latest"}.
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


def parse_agent_statuses(data):
    found = {}
    for field, status in _proto_fields(data):
        if field != 1 or not isinstance(status, bytes):
            continue
        parts = _proto_fields(status)
        ids = [v for f, v in parts if f == 1 and isinstance(v, bytes)]
        client = next((v for f, v in parts if f == 3 and isinstance(v, bytes)), b"")
        if not ids:
            continue
        state = dict(_proto_fields(client))
        unread = state.get(3)
        latest = state.get(4)
        found[ids[0].decode("utf-8", "replace")] = {
            "unread": unread if isinstance(unread, int) else 0,
            "latest": latest if isinstance(latest, int) else 0,
        }
    return found


def refresh_server_state(bots):
    """Best effort. The list falls back to its own reads when this fails."""
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
