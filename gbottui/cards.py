"""Secret, 1Password and form requests: read-only cards in the transcript.

The terminal never takes or sends a secret, a password, an OTP or a form
value. These cards only say what the bot asked for and whether it is settled.
The handles that grant access (`appLink`, `accessRequestId`, `entryId`,
`requestId`) and any submitted value are never read here, so they cannot
reach the screen, the search, or a copy.
"""

import time

from . import transcript

SECRET = "secret-request"
CREDENTIAL = "credential-request"
FORM = "user-form"
REQUEST_KEY = {SECRET: "secretRequest", CREDENTIAL: "credentialRequest", FORM: "formRequest"}

WAIT_SECRET = "waiting: enter it in the Grok Bot app"
WAIT_CREDENTIAL = "waiting: approve in 1Password or the Grok Bot app"
WAIT_FORM = "waiting: fill it in the Grok Bot app"

# Row kinds, the same three the approval cards use (styles.CARD_STYLE).
PENDING, OK, NO = "card", "card-ok", "card-no"
RESOLVED_OK = {"approved", "submitted", "provided"}


def now_ms():
    """Wall clock in ms. Tests replace this to move a credential past its expiry."""
    return time.time() * 1000


def _clean(value, limit=400):
    """One line of plain text. Control characters (an escape sequence) become spaces."""
    if not isinstance(value, str):
        return ""
    flat = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value)
    flat = " ".join(flat.split())
    if len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


def _paragraph(value, limit=600):
    """Text that may span lines. Newlines stay, other control characters go."""
    if not isinstance(value, str):
        return ""
    lines = []
    for raw in value.splitlines():
        line = _clean(raw, limit)
        if line:
            lines.append(line)
    return "\n".join(lines)


def kind(entry):
    """secret-request, credential-request, user-form, or None."""
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    name = message.get("type")
    return name if name in REQUEST_KEY else None


def _request(entry):
    request = entry["message"].get(REQUEST_KEY[kind(entry)])
    return request if isinstance(request, dict) else {}


def _resolution(value):
    """A settled outcome as words (`approved`), or "" while still pending."""
    word = _clean(value, 40).lower().replace("-", " ").replace("_", " ")
    return "" if word in {"", "pending"} else word


def _expired(request):
    at = request.get("expiresAtMs")
    return isinstance(at, (int, float)) and not isinstance(at, bool) and at < now_ms()


def _secret(entry, request):
    label = _clean(request.get("label"))
    title = f"secret: {label}" if label else "secret"
    lines = ["\U0001F511 " + title]
    description = _paragraph(request.get("description"))
    if description:
        lines.append(description)
    target = request.get("target")
    env = _clean(target.get("name"), 120) if isinstance(target, dict) else ""
    if env:
        lines.append(f"env: {env}")
    if entry.get("secretProvided") is True:
        return title, lines, "provided", OK, False
    return title, lines, WAIT_SECRET, PENDING, True


def _credential(entry, request):
    goal = _clean(request.get("goal"))
    title = f"1Password: {goal}" if goal else "1Password: credential request"
    lines = [title]
    entries = request.get("entries")
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        website = _clean(item.get("website"), 200)
        reason = _clean(item.get("reason"))
        line = f"{website}: {reason}" if website and reason else website or reason
        if line:
            lines.append(line)
    outcome = _resolution(entry.get("credentialResolution"))
    if outcome:
        return title, lines, outcome, OK if outcome in RESOLVED_OK else NO, False
    if _expired(request):
        return title, lines, "expired", NO, False
    return title, lines, WAIT_CREDENTIAL, PENDING, True


def _form(entry, request):
    heading = _clean(request.get("title"))
    title = f"form: {heading}" if heading else "form"
    lines = [title]
    instruction = _paragraph(request.get("instruction"))
    if instruction:
        lines.append(instruction)
    fields = request.get("fields")
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        label = _clean(field.get("label"), 200) or "field"
        shape = _clean(field.get("type"), 40)
        lines.append(f"{label} ({shape})" if shape else label)
    outcome = _resolution(entry.get("formResolution"))
    if outcome:
        return title, lines, outcome, OK if outcome in RESOLVED_OK else NO, False
    return title, lines, WAIT_FORM, PENDING, True


_BUILD = {SECRET: _secret, CREDENTIAL: _credential, FORM: _form}


SKIPPED = "skipped"


def view(entry, answered_later=False):
    """What to draw for one card entry, or None for any other entry.

    A dict with `title` (no emoji, for the bot list), `body` (the card text
    above the state line), `state` (the state line), `tone` (the row kind of
    that line) and `pending` (True while it needs the user).

    `answered_later` is True when a user message comes after the card in the
    thread. A card nobody settled is then `skipped` and no longer pending. A
    settled or expired card keeps its own state. `views` works that out per
    entry, so this is the one place the rule lives.
    """
    which = kind(entry)
    if which is None:
        return None
    title, lines, state, tone, pending = _BUILD[which](entry, _request(entry))
    if pending and answered_later:
        state, tone, pending = SKIPPED, NO, False
    return {"title": title, "body": "\n".join(lines), "state": state, "tone": tone, "pending": pending}


def views(entries):
    """`view` for every entry of a thread, in order (None for a non-card).

    A user message (`transcript.is_user`, which includes the local echo of a
    message just sent, as `widgets.latest_widget` counts it) after a card
    means the user moved on without answering it.
    """
    out = [None] * len(entries)
    later = False
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if not isinstance(entry, dict):
            continue
        out[index] = view(entry, later)
        if transcript.is_user(entry):
            later = True
    return out


def text(entry):
    """The plain text of the whole card, for copy and search."""
    shown = view(entry)
    if shown is None:
        return ""
    return shown["body"] + "\n" + shown["state"]


def any_pending(entries):
    return any(shown and shown["pending"] for shown in views(entries))


def latest_pending(entries):
    """The view of the newest card that still needs the user, or None."""
    for shown in reversed(views(entries)):
        if shown and shown["pending"]:
            return shown
    return None
