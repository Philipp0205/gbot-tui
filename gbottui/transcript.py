"""What a `gbot thread` page means: message text, roles, approval cards, and phases."""

# shortcut: two empty polls after a reply count as finished, so a bot that
# pauses longer than that looks done while it is still working.
QUIET_POLLS = 2


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


def has_reply(entries):
    """True when one of these entries is the bot saying something."""
    return any(not is_user(entry) and entry_text(entry).strip() for entry in entries)


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


def card_details(card):
    return [card[key].strip() for key in DETAIL_KEYS if isinstance(card.get(key), str) and card[key].strip()]


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
    entry_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
    return {"entry_id": entry_id, "request_id": request_of(entry), "details": card_details(card)}


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
    if new_entries:
        quiet = 0
        saw_other = saw_other or has_reply(new_entries)
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
    if not isinstance(payload, dict):
        payload = {}
    transcript = payload.get("transcript")
    if not isinstance(transcript, dict):
        transcript = payload
    entries = transcript.get("entries")
    if not isinstance(entries, list):
        entries = []
    cursor = payload.get("cursor")
    if not isinstance(cursor, str) or not cursor:
        cursor = last_id(entries)
    target = payload.get("target")
    if not isinstance(target, dict):
        target = {}
    return {
        "entries": [entry for entry in entries if isinstance(entry, dict)],
        "cursor": cursor,
        "gap_reset": bool(payload.get("gapReset")),
        "name": target.get("name") or "",
        "id": target.get("id") or "",
        "avatar_shape": target.get("avatarShape") or "",
        "avatar_color": target.get("avatarColor") or "",
    }
