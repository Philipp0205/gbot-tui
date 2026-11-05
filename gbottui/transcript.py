"""Reading thread entries: text, cards, approvals."""

import os
import re

from . import gbot_cli, widgets


# shortcut: two empty polls after a reply count as finished, so a bot that
# pauses longer than that looks done while it is still working.
QUIET_POLLS = 2

# A progress line the bot sends while it works ("Checking the agent now.",
# "Ich schau mir EXE-3318 kurz an."). The desktop shows that as Thinking.
# A link or a second paragraph is the reply itself, so it stays.
_STATUS_BEAT = re.compile(
    r"^(?:"
    r"hi\b.{0,80}?\bich\s+(?:check\w*|schau\w*|such\w*)\b"
    r"|(?:(?:"
    r"gut|okay|got it|right|sounds good|good idea|sure|will do|makes sense|probably(?:\s*,\s*yes)?|on it"
    r")[,.!]?\s+)?"
    r"(?:"
    r"(?:still\s+)?(?:checking|looking(?:\s+up)?|reading|searching|refreshing|comparing)\b"
    r"|i(?:'m|\s+am)\s+(?:still\s+)?(?:\w+ing|going)\b"
    r"|i(?:'ll|\s+will)\s+(?:send|tell|let|get back|look|check|start|be back|come back|grab|hand|pull|read|pass|delete|make|do|re-run|draft|mute)\b"
    r"|i(?:'ve|\s+have)\s+asked\b"
    r"|i haven't found\b"
    r"|on it\b"
    r"|(?:gute idee,\s+)?ich\s+(?:schau\w*|such\w*|durchsuch\w*|verschieb\w*|lass\w*|poste\w*|schick\w*|check\w*|gleich\w*|meld\w*|bin\s+gerade|geb\w*|führ\w*)\b"
    r"|mach ich[.!,]?\s+ich\s+(?:schau\w*|such\w*|durchsuch\w*|geb\w*|führ\w*)\b"
    r"|ich hab(?:e)?\s+@"
    r"|@\S+\s+schreibt\s+jetzt\b"
    r"|läuft\b"
    r"))",
    re.IGNORECASE,
)
_STATUS_BEAT_LATER = re.compile(
    r"\b(?:wird\s+gerade|geht\s+gerade|schicke ich,\s+sobald|kommen direkt nach|kommt direkt nach)\b",
    re.IGNORECASE,
)


def is_status_beat(text):
    raw = str(text).strip()
    if not raw or len(raw) > 400 or "\n" in raw or "http" in raw.lower():
        return False
    return _STATUS_BEAT.match(raw) is not None or _STATUS_BEAT_LATER.search(raw) is not None


def _row_key(entry):
    ident = entry.get("id") if isinstance(entry, dict) else None
    if isinstance(ident, str) and ident:
        return ident
    return id(entry)


def hidden_status_ids(entries):
    """Progress lines to leave out once any later bot line is visible."""
    shown = [
        entry
        for entry in entries
        if isinstance(entry, dict) and not is_user(entry) and entry_text(entry).strip()
    ]
    return {
        _row_key(entry)
        for entry in shown[:-1]
        if is_status_beat(entry_text(entry))
    }


# The desktop chat does not show the model's thinking. These names mark a
# whole entry, a tool, or one part of a message.
THINKING_NAMES = frozenset({
    "thinking",
    "thought",
    "reasoning",
    "redacted-thinking",
    "agent-thought",
    "agent-thought-chunk",
})
_THINKING_BLOCK = re.compile(
    r"<(?:think|thinking|reasoning)\b[^>]*>.*?</(?:think|thinking|reasoning)>",
    re.IGNORECASE | re.DOTALL,
)
_THINKING_OPEN = re.compile(
    r"<(?:think|thinking|reasoning)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_THINKING_CLOSE = re.compile(r"</(?:think|thinking|reasoning)>", re.IGNORECASE)


def _thinking_name(value):
    if not isinstance(value, str):
        return False
    return value.strip().lower().replace("_", "-") in THINKING_NAMES


def is_thinking_part(part):
    if not isinstance(part, dict):
        return False
    for key in ("type", "kind", "role", "name"):
        if _thinking_name(part.get(key)):
            return True
    return False


def is_thinking(entry):
    """True when this row is the model's thinking, not a chat message."""
    if not isinstance(entry, dict) or entry.get("local") or is_user(entry):
        return False
    message = entry.get("message")
    if isinstance(message, dict) and _thinking_name(message.get("type")):
        return True
    if entry.get("kind") == "send-message":
        return False
    for key in ("kind", "role", "type", "name", "tool", "toolName", "tool_name"):
        if _thinking_name(entry.get(key)):
            return True
    for key in ("toolCall", "tool_call", "function"):
        nested = entry.get(key)
        if isinstance(nested, dict) and (
            _thinking_name(nested.get("name")) or _thinking_name(nested.get("type"))
        ):
            return True
    return False


def hide_thinking_markup(text):
    """Drop <think>, <thinking>, and <reasoning> blocks from a reply."""
    if not isinstance(text, str) or "<" not in text:
        return text
    cleaned = _THINKING_BLOCK.sub("", text)
    cleaned = _THINKING_OPEN.sub("", cleaned)
    cleaned = _THINKING_CLOSE.sub("", cleaned)
    if cleaned == text:
        return text
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _part_text(part):
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and not is_thinking_part(part):
        return part.get("text") or part.get("content") or ""
    return ""


def entry_text(entry, picks=(), numbered=True, step_at=0, step_answers=()):
    if not isinstance(entry, dict) or is_thinking(entry):
        return ""
    text = _entry_body(entry, picks, numbered, step_at, step_answers)
    if is_user(entry):
        return text
    return hide_thinking_markup(text)


def attachment_text(entry):
    name = entry.get("file_name") or os.path.basename(entry.get("file_path") or "") or "attachment"
    details = [str(entry[key]) for key in ("secondaryText",) if entry.get(key)]
    if entry.get("width") and entry.get("height"):
        details.append(f"{entry['width']}x{entry['height']}")
    return f"\U0001F4CE {name}" + (f" ({', '.join(details)})" if details else "")


def _entry_body(entry, picks=(), numbered=True, step_at=0, step_answers=()):
    if entry.get("kind") == "user-attachment":
        return attachment_text(entry)
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
        return "\n".join(piece for piece in (_part_text(part) for part in content) if piece)
    if isinstance(content, dict) and not is_thinking_part(content) and isinstance(content.get("text"), str):
        return content["text"]
    shown = widgets.widget_text(entry, picks, numbered, step_at, step_answers)
    if shown:
        return shown
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
    return role in {"user", "human", "user-attachment"} or role.endswith(":user")


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
    session = target.get("sessionId") if isinstance(target.get("sessionId"), str) else ""
    return {
        "entries": [entry for entry in entries if isinstance(entry, dict)],
        "cursor": cursor,
        "gap_reset": bool(payload.get("gapReset")) if isinstance(payload, dict) else False,
        "name": target.get("name") or "",
        "id": target.get("id") or "",
        "agent_id": gbot_cli.gateway_agent_id(target),
        "viewer_session": session.strip(),
        "avatar_shape": target.get("avatarShape") or "",
        "avatar_color": target.get("avatarColor") or "",
    }
