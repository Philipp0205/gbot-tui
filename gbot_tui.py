#!/usr/bin/env python3
"""Chat with one Grok Bot. Inside Herdr, the pane shows up in the agents list.

The bot list is an overview: who needs you, who is working, and who has a new reply.
"""

import atexit
import base64
import curses
import hashlib
import json
import locale
import os
import queue
import re
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time

SOURCE = "gbot-tui"
APP = "gbot-tui"
# shortcut: two empty polls after a reply count as finished, so a bot that
# pauses longer than that looks done while it is still working.
QUIET_POLLS = 2
# A turn that never grows (the last line stays yours, or isStreaming stays
# set) would otherwise say `working` forever. This is how long with no new
# text before the chat and the overview fall back to idle.
WORKING_TIMEOUT = 120
# The gateway widget call is optional. The answer is a normal message unless
# this is 1/true/yes/on, and a failed hook still falls back to that message.
WIDGET_HOOK_ENV = "GBOT_TUI_WIDGET_HOOK"
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


def entry_text(entry, picks=(), numbered=True, step_at=0, step_answers=()):
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
    shown = widget_text(entry, picks, numbered, step_at, step_answers)
    if shown:
        return shown
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(entry.get("preview"), str):
        return entry["preview"]
    return ""


def widget_message(entry):
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("type") != "widget":
        return None
    widget = message.get("widget")
    return widget if isinstance(widget, dict) else None


def _option_pairs(raw):
    """(label, value) in the order the bot sent them. The value is what gets sent."""
    if not isinstance(raw, list):
        return []
    options = []
    for opt in raw:
        if not isinstance(opt, dict):
            continue
        label = opt.get("label") if isinstance(opt.get("label"), str) else ""
        value = opt.get("value") if isinstance(opt.get("value"), str) else ""
        label = label.strip() or value.strip()
        value = value.strip() or label
        if label and value:
            options.append((label, value))
    return options


def widget_options(entry):
    """(label, value) on a single-question widget."""
    widget = widget_message(entry)
    if not widget:
        return []
    return _option_pairs(widget.get("options"))


def step_questions(entry):
    """Questions on one card. Each has prompt, options, multi, and custom.

    A widget without `steps` is one question, handled by widget_options.
    """
    widget = widget_message(entry)
    raw = widget.get("steps") if widget else None
    if not isinstance(raw, list):
        return []
    steps = []
    for step in raw:
        if not isinstance(step, dict):
            continue
        prompt = step.get("prompt") if isinstance(step.get("prompt"), str) else ""
        prompt = prompt.strip()
        options = _option_pairs(step.get("options"))
        multi = step.get("multiSelect") is True
        custom = step.get("allowCustom") is True
        if prompt or options or custom:
            steps.append({"prompt": prompt, "options": options, "multi": multi, "custom": custom})
    return steps


def widget_answered(entry):
    value = entry.get("respondedValue") if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value.strip() else ""


def widget_skipped(entry):
    return isinstance(entry, dict) and (
        entry.get("widgetSkipped") is True or entry.get("widgetDismissed") is True
    )


def _question_answerable(step, widget):
    if step["options"] or step["custom"]:
        return True
    return bool(widget and widget.get("allowCustom") is True)


def widget_open(entry):
    if widget_answered(entry) or widget_skipped(entry) or not widget_message(entry):
        return False
    steps = step_questions(entry)
    if steps:
        widget = widget_message(entry)
        return any(_question_answerable(step, widget) for step in steps)
    return bool(widget_options(entry))


def latest_widget(entries):
    """The newest choice the user can still answer.

    A widget behind a later user message is stale. Digits belong to the
    composer then, because the user already replied.
    """
    last_user = -1
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and is_user(entry):
            last_user = index
    found = None
    for index, entry in enumerate(entries):
        if index > last_user and widget_open(entry):
            found = entry
    return found


# Up to 9 options, one digit selects. Past that, two digits (`10`, or `02`)
# within DIGIT_WINDOW. A letter is never a choice: a message may start with one.
OPTION_DIGITS = 9
OPTION_PAIR = 99
DIGIT_WINDOW = 0.8


def option_key(index, wide=False):
    """1-based position as a key. Empty when this option has no key."""
    if index < 1:
        return ""
    if wide:
        if index > OPTION_PAIR:
            return ""
        return f"{index:02d}"
    if index <= OPTION_DIGITS:
        return str(index)
    return ""


def key_to_index(key, count):
    """0-based option for one digit, "ignore", or None when the key is text.

    Only used when there are at most 9 options. `0` does not fall into the
    composer. A letter is never an option.
    """
    if not isinstance(key, str) or len(key) != 1 or count <= 0 or count > OPTION_DIGITS:
        return None
    if not key.isdigit() or key == "0":
        return "ignore" if key.isdigit() else None
    index = int(key) - 1
    if index < count:
        return index
    return "ignore"


def _key_span(keys):
    """`1`, `1-2`, or `1-9,a-c`."""
    digits = [key for key in keys if key.isdigit()]
    letters = [key for key in keys if key.isalpha()]
    parts = []
    if digits:
        parts.append(digits[0] if len(digits) == 1 else f"{digits[0]}-{digits[-1]}")
    if letters:
        parts.append(letters[0] if len(letters) == 1 else f"{letters[0]}-{letters[-1]}")
    return ",".join(parts)


def current_question(entry, step_at=0):
    """Prompt, options, multi-select, and allowCustom for the question keys answer."""
    widget = widget_message(entry)
    if not widget:
        return "", [], False, False
    steps = step_questions(entry)
    if steps:
        at = min(max(int(step_at or 0), 0), len(steps) - 1)
        step = steps[at]
        custom = step["custom"] or widget.get("allowCustom") is True
        return step["prompt"], step["options"], step["multi"], custom
    prompt = widget.get("prompt") if isinstance(widget.get("prompt"), str) else ""
    return prompt.strip(), widget_options(entry), widget.get("multiSelect") is True, widget.get("allowCustom") is True


def _option_lines(options, open_, multi, chosen, picked):
    lines = []
    matched = False
    uncovered = False
    wide = len(options) > OPTION_DIGITS
    for index, (label, value) in enumerate(options, 1):
        marked = value in chosen or value in picked
        matched = matched or value in chosen
        key = option_key(index, wide) if open_ else ""
        if open_ and not key:
            uncovered = True
        if open_ and key and multi:
            mark = "●" if marked else " "
            lines.append(f"{mark} {key}  {label}")
        elif open_ and key:
            lines.append(f"{key}  {label}")
        elif marked:
            lines.append(f"●  {label}")
        else:
            lines.append(f"   {label}")
    return lines, matched, uncovered


def _steps_text(entry, steps, picks, numbered, step_at, step_answers):
    widget = widget_message(entry)
    answered = widget_answered(entry)
    # A finished card stores one answer per step, with a blank line between them.
    if answered and not step_answers:
        step_answers = answered.split("\n\n")
    answers = list(step_answers or [])
    open_ = widget_open(entry) and numbered
    at = int(step_at or 0) if open_ else len(steps)
    lines = []
    head = widget.get("prompt") if isinstance(widget.get("prompt"), str) else ""
    head = head.strip()
    if head and head not in {step["prompt"] for step in steps}:
        lines.append(head)
    if widget_skipped(entry) and not answered:
        if lines:
            lines[-1] += "  · skipped"
        elif steps and steps[0]["prompt"]:
            steps = [dict(steps[0], prompt=steps[0]["prompt"] + "  · skipped"), *steps[1:]]
        else:
            lines.append("skipped")
    for index, step in enumerate(steps):
        if step["prompt"] and not (index == 0 and lines and lines[-1].startswith(step["prompt"])):
            lines.append(step["prompt"])
        if index < len(answers) and str(answers[index]).strip():
            chosen = {part for part in str(answers[index]).split("\n") if part}
            option_lines, matched, _extra = _option_lines(step["options"], False, False, chosen, ())
            lines.extend(option_lines)
            if not matched:
                lines.append("●  " + str(answers[index]).strip())
            continue
        current = open_ and index == at
        option_lines, _matched, extra = _option_lines(
            step["options"], current, step["multi"], set(), set(picks or ()) if current else ()
        )
        lines.extend(option_lines)
        if current and extra:
            lines.append("answer the rest in the app")
        elif current and not step["options"] and not _question_answerable(step, widget):
            lines.append("answer in the app")
    if not answered and not widget_skipped(entry) and not any(_question_answerable(step, widget) for step in steps):
        lines.append("answer in the app")
    return "\n".join(line for line in lines if line is not None)


def widget_text(entry, picks=(), numbered=True, step_at=0, step_answers=()):
    """The prompt and its options. The open choice is numbered. A picked one is marked."""
    widget = widget_message(entry)
    if widget is None:
        return ""
    steps = step_questions(entry)
    if steps:
        return _steps_text(entry, steps, picks, numbered, step_at, step_answers)
    prompt = widget.get("prompt")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    if widget_skipped(entry) and not widget_answered(entry) and prompt:
        prompt += "  · skipped"
    options = widget_options(entry)
    answered = widget_answered(entry)
    chosen = {part for part in answered.split("\n") if part} if answered else set()
    picked = set(picks or ())
    multi = widget.get("multiSelect") is True
    # Only the newest open widget takes number keys, so older ones are not numbered.
    open_ = widget_open(entry) and numbered
    lines = [prompt] if prompt else []
    option_lines, matched, extra = _option_lines(options, open_, multi, chosen, picked)
    lines.extend(option_lines)
    if open_ and extra:
        lines.append("answer the rest in the app")
    if answered and not matched:
        lines.append(f"●  {answered}")
    return "\n".join(lines)


def choice_hint(entry, step_at=0):
    prompt, options, multi, custom = current_question(entry, step_at) if entry else ("", [], False, False)
    if not options:
        if entry and widget_open(entry) and custom:
            return "type an answer"
        return ""
    wide = len(options) > OPTION_DIGITS
    keys = [option_key(index, wide) for index in range(1, len(options) + 1)]
    keys = [key for key in keys if key]
    if not keys:
        return "answer in the app"
    span = _key_span(keys)
    if wide:
        span = f"{span} two digits"
    if multi:
        return f"{span} choose · enter sends"
    return f"{span} answer"


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
    session = target.get("sessionId") if isinstance(target.get("sessionId"), str) else ""
    return {
        "entries": [entry for entry in entries if isinstance(entry, dict)],
        "cursor": cursor,
        "gap_reset": bool(payload.get("gapReset")) if isinstance(payload, dict) else False,
        "name": target.get("name") or "",
        "id": target.get("id") or "",
        "agent_id": gateway_agent_id(target),
        "viewer_session": session.strip(),
        "avatar_shape": target.get("avatarShape") or "",
        "avatar_color": target.get("avatarColor") or "",
    }


def gateway_agent_id(target):
    """The id `respondGrokBotWidget` calls `agentId`.

    `gbot thread` puts the gateway agent id on `target.id` (`agent.id`, else
    `agentId`). `target.agentId` wins when it is also present. `target.sessionId`
    is the viewer's session and is a different field. When `id` and `sessionId`
    are the same string and `agentId` is absent, there is no agent id to send.
    """
    if not isinstance(target, dict):
        return ""
    session = target.get("sessionId") if isinstance(target.get("sessionId"), str) else ""
    session = session.strip()
    explicit = target.get("agentId") if isinstance(target.get("agentId"), str) else ""
    explicit = explicit.strip()
    if explicit:
        if session and explicit == session:
            return ""
        return explicit
    ident = target.get("id") if isinstance(target.get("id"), str) else ""
    ident = ident.strip()
    if not ident or (session and ident == session):
        return ""
    return ident


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
        notice_name(label) or label,
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
        notice_name(label) or label,
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


# One gbot process at a time, so a list scan cannot pass a send or a chat read.
GBOT_LOCK = threading.Lock()


def gbot(*args, timeout=60, env_extra=None):
    env = os.environ.copy()
    # gbot 0.12.4 tells Cursor it is Grok Bot 0.20.0, which the server rejects.
    env.setdefault("SAND_CLIENT_VERSION", sand_client_version())
    if env_extra:
        env.update(env_extra)
    with GBOT_LOCK:
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


# Shapes the app will draw. A bot that never picked one gets one of the eight
# defaults below; the rest (heart, pumpkin, …) are explicit choices.
AVATAR_SHAPES = {
    "blob", "pebble", "bean", "egg", "squircle", "tablet", "capsule", "cylinder",
    "hex", "gem", "crystal", "wedge", "shield", "dome", "arch", "cloud", "teardrop",
    "leaf", "square", "sparkle", "clover", "flower", "heart", "house", "star", "pumpkin",
}

# Grok Bot's own fallback (index.eager-common An/Dn/Tn). The hash indexes these
# lists, so the order is part of the result. Black is a chosen color only.
DEFAULT_SHAPES = ["blob", "pebble", "squircle", "tablet", "wedge", "hex", "cloud", "teardrop"]
DEFAULT_COLORS = [name for name in AVATAR_COLORS if name != "black"]


def _imul(a, b):
    """Low 32 bits of a product, matching JavaScript Math.imul."""
    return (a * b) & 0xFFFFFFFF


def _fnv(text):
    """FNV-1a over UTF-16 code units, the way JS charCodeAt walks a string."""
    h = 2166136261
    for ch in str(text):
        code = ord(ch)
        if code < 0x10000:
            units = (code,)
        else:
            code -= 0x10000
            units = (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
        for unit in units:
            h = _imul(h ^ unit, 16777619)
    return h


def default_shape(agent_id):
    t = _fnv(agent_id)
    t = _imul(t ^ (t >> 16), 73244475)
    t = _imul(t ^ (t >> 13), 3266489909)
    return DEFAULT_SHAPES[((t ^ (t >> 16)) & 0xFFFFFFFF) % len(DEFAULT_SHAPES)]


def default_color(agent_id):
    # Math.imul(1, 0x9E3779B9) is applied twice and cancels. Both steps stay
    # so the seed matches the app's mulberry32 call.
    seed = (_fnv(agent_id) ^ _imul(1, 2654435769)) & 0xFFFFFFFF
    t = (seed ^ _imul(1, 2654435769)) & 0xFFFFFFFF
    t = (t + 1831565813) & 0xFFFFFFFF
    e = _imul(t ^ (t >> 15), 1 | t)
    e = ((e + _imul(e ^ (e >> 7), 61 | e)) & 0xFFFFFFFF) ^ e
    r = ((e ^ (e >> 14)) & 0xFFFFFFFF) / 4294967296
    return DEFAULT_COLORS[int(r * len(DEFAULT_COLORS))]


def bot_avatar(bot):
    """(shape, color) the way Grok Bot draws it, defaults included.

    The hash input is `id`. `gbot bots list` sets that from the gateway
    agent id (`agent.id`, else `agentId`). `agentId` is only a fallback
    for a row that did not go through that list.
    """
    agent_id = str(bot.get("id") or bot.get("agentId") or "")
    shape = str(bot.get("avatarShape") or bot.get("avatar_shape") or "")
    color = str(bot.get("avatarColor") or bot.get("avatar_color") or "").lower()
    if shape not in AVATAR_SHAPES:
        shape = default_shape(agent_id)
    if color not in AVATAR_COLORS:
        color = default_color(agent_id)
    return shape, color


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


APP_LIMIT = 32


def display_name(name):
    """The bot's name as a person reads it, with control characters removed."""
    return " ".join(str(name or "").replace("\x1b", " ").replace("\r", " ").replace("\n", " ").split())


def notice_name(name):
    """That name as an OSC 7501 app. Herdr shows this string in "Cloud needs you".

    Herdr drops an app that contains whitespace (a space, a non-breaking space,
    or an en space), and then the popup says "An agent needs you" and the icon
    does not attach. Those become hyphens: "Cursor-Cloud-Session needs you".
    The app field is at most 32 bytes, and a colon would split the report.
    """
    text = "-".join(display_name(str(name or "").replace(":", " ").replace("=", " ")).split())
    raw = text.encode()
    if len(raw) <= APP_LIMIT:
        return text
    return raw[:APP_LIMIT].decode("utf-8", "ignore").rstrip().rstrip("-")


def bind_app(name, bot_id=""):
    """Point Herdr's program report at the bot's name. The icon key is the fallback."""
    global APP
    label = notice_name(name) or program_key(bot_id) or ""
    if label:
        APP = label


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


# How many trailing entries the overview reads. Same window as a chat poll,
# so a pending card the chat would see is visible here too.
OVERVIEW_LIMIT = 80
# The list stays open. Scan again this long after the previous pass finishes.
OVERVIEW_PAUSE = 8
OVERVIEW_STATES = ("blocked", "unread", "working", "idle")
ATTENTION_RANK = {"blocked": 0, "unread": 1, "working": 2, "idle": 4}
# `working` stays off the yellow used for `needs you`, in the list and the header.
STATUS_STYLE = {"blocked": "yellow", "unread": "green", "working": "cyan", "idle": "dim"}
# In-progress flags a transcript entry may carry. Absent on a finished line.
PROGRESS_STATES = {"running", "streaming", "in_progress", "generating", "working"}

STATE_LOCK = threading.Lock()
ACTIVITY = {}
SEEN = {}
ACTIVITY_LOADED = {"done": False}
SEEN_LOADED = {"done": False}


def activity_path():
    return os.path.join(cache_dir(), "activity.json")


def seen_path():
    return os.path.join(cache_dir(), "seen.json")


def activity_keys(bot):
    keys = []
    bot_id = bot.get("id") if isinstance(bot, dict) else ""
    name = bot.get("name") if isinstance(bot, dict) else ""
    if isinstance(bot_id, str) and bot_id:
        keys.append("id:" + bot_id)
    if isinstance(name, str) and name:
        keys.append("name:" + name)
    return keys


def _clean_snap(snap):
    if not isinstance(snap, dict) or snap.get("state") not in OVERVIEW_STATES:
        return None
    unread = snap.get("unread")
    return {
        "state": snap["state"],
        "unread": unread if isinstance(unread, int) and unread > 0 else 0,
        "cursor": snap.get("cursor") if isinstance(snap.get("cursor"), str) else "",
        "preview": snap.get("preview") if isinstance(snap.get("preview"), str) else "",
        "token": snap.get("token") if isinstance(snap.get("token"), str) else "",
        "since": snap.get("since") if isinstance(snap.get("since"), (int, float)) else 0,
    }


def load_activity():
    with STATE_LOCK:
        if ACTIVITY_LOADED["done"]:
            return
        ACTIVITY_LOADED["done"] = True
        data = read_json(activity_path())
        if not isinstance(data, dict):
            return
        for key, snap in data.items():
            clean = _clean_snap(snap)
            if isinstance(key, str) and clean:
                ACTIVITY[key] = clean


def load_seen():
    with STATE_LOCK:
        if SEEN_LOADED["done"]:
            return
        SEEN_LOADED["done"] = True
        data = read_json(seen_path())
        if not isinstance(data, dict):
            return
        for key, cursor in data.items():
            if isinstance(key, str) and isinstance(cursor, str) and cursor:
                SEEN[key] = cursor


def _save_locked(path, data):
    write_private(path, data)


def store_activity(bot, snap):
    clean = _clean_snap(snap)
    keys = activity_keys(bot)
    if not clean or not keys:
        return
    load_activity()
    with STATE_LOCK:
        for key in keys:
            ACTIVITY[key] = clean
        _save_locked(activity_path(), dict(ACTIVITY))


def lookup_activity(bot):
    load_activity()
    with STATE_LOCK:
        for key in activity_keys(bot):
            snap = ACTIVITY.get(key)
            if snap:
                return dict(snap)
    return None


def remember_seen(name, bot_id, cursor, only_if_absent=False):
    """The latest entry this chat had on screen. A later reply is then `new`.

    `only_if_absent` is the first scan of a bot nobody has opened. It must not
    move a cursor the chat, or an earlier scan, already stored.
    """
    if not isinstance(cursor, str) or not cursor:
        return
    load_seen()
    with STATE_LOCK:
        keys = []
        if bot_id:
            keys.append("id:" + str(bot_id))
        if name:
            keys.append("name:" + name)
        if only_if_absent and any(SEEN.get(key) for key in keys):
            return
        for key in keys:
            SEEN[key] = cursor
        _save_locked(seen_path(), dict(SEEN))


def lookup_seen(bot):
    load_seen()
    with STATE_LOCK:
        for key in activity_keys(bot):
            cursor = SEEN.get(key)
            if cursor:
                return cursor
    return ""


def seen_mark(bot):
    """The cursor to diff against. A scan never moves this.

    The seen file wins. The first look writes it once, and the chat advances
    it when the thread is on screen. A cached thread counts when that file has
    no row, because the thread was on screen. The activity cursor does not:
    it advances every pass, which cleared `new` on the pass after it appeared.
    """
    seen = lookup_seen(bot)
    if seen:
        return seen
    return last_id(read_thread_cache(bot.get("name") or ""))


def baseline_seen(bot, cursor):
    """Record the first cursor a scan saw. Later scans leave it where it is."""
    if not isinstance(cursor, str) or not cursor:
        return
    if lookup_seen(bot):
        return
    if last_id(read_thread_cache(bot.get("name") or "")):
        return
    remember_seen(bot.get("name") or "", bot.get("id") or "", cursor, only_if_absent=True)


def one_line(text, limit=160):
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    if limit <= 1:
        return "…"
    return flat[: limit - 1] + "…"


def clock():
    """Monotonic time. Tests replace this to move a quiet turn forward."""
    return time.monotonic()


def content_token(entries):
    """Identity of the visible text. A longer reply is new content; a quiet flag is not."""
    parts = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = "u" if is_user(entry) else "b"
        flag = ""
        if entry.get("streaming") is True or entry.get("inProgress") is True or entry.get("isStreaming") is True:
            flag = "*"
        parts.append(kind + flag + entry_text(entry))
    return "\x1e".join(parts)


def awaiting_reply(entries):
    """True when the last line with text is the user's. The bot has not answered."""
    speaker = ""
    for entry in entries:
        if entry.get("local"):
            continue
        text = entry_text(entry).strip()
        if not text:
            continue
        speaker = "user" if is_user(entry) else "other"
    return speaker == "user"


def entry_in_progress(entries):
    """The latest message is still being written. A trailing event does not hide that.

    The gateway flag on a message is `isStreaming`.
    """
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("local"):
            continue
        if entry.get("streaming") is True or entry.get("inProgress") is True or entry.get("isStreaming") is True:
            return True
        status = entry.get("status")
        if isinstance(status, str) and status.lower() in PROGRESS_STATES:
            return True
        if entry_text(entry).strip() or entry.get("role") or entry.get("kind") in {"message", "send-message"}:
            return False
    return False


def notable(entry):
    """A line worth calling new: a bot message or a card, not the user's own text."""
    if not isinstance(entry, dict) or entry.get("local") or is_user(entry):
        return False
    if entry_text(entry).strip():
        return True
    return approval_of(entry) is not None


def unseen_notable(entries, seen):
    if not seen:
        return 0
    index = next((i for i, entry in enumerate(entries) if entry.get("id") == seen), None)
    # The seen id is older than this tail, so every line here arrived after it.
    rest = entries if index is None else entries[index + 1 :]
    return sum(1 for entry in rest if notable(entry))


def activity_preview(entries, state):
    if state == "blocked":
        for entry in reversed(entries):
            card = approval_of(entry)
            if not card:
                continue
            if card["details"]:
                return one_line(card["details"][0])
            return "Needs approval"
        widget = latest_widget(entries)
        if widget:
            prompt, _options, _multi, _custom = current_question(widget)
            if prompt:
                return one_line(prompt)
            return "Needs a choice"
    for entry in reversed(entries):
        text = entry_text(entry).strip()
        if text:
            return one_line(text)
    return ""


def _working_since(previous, token, now):
    """When this same text first looked in progress. A new clock starts over."""
    if not isinstance(previous, dict) or previous.get("token") != token:
        return now
    old = previous.get("since")
    if not isinstance(old, (int, float)):
        return now
    # A since from a previous process can sit past this clock. Start over then.
    if old > now:
        return now
    return old


def judge_activity(entries, cursor, seen, previous=None):
    """What one thread tail says. `seen` is the last cursor the user had on screen.

    An empty `seen` does not mark the whole history as new. `blocked` wins over
    a new reply, and a new reply wins over `running`. An open widget after the
    user's last message is `needs you`. `working` that gains no text for
    WORKING_TIMEOUT falls back to idle. `previous` is the last snap, so the
    overview can time that quiet stretch with `clock`.
    """
    entries = [entry for entry in entries if isinstance(entry, dict) and not entry.get("local")]
    cursor = cursor or last_id(entries)
    now = clock()
    token = content_token(entries)
    running = awaiting_reply(entries) or entry_in_progress(entries)
    since = _working_since(previous, token, now) if running else 0
    if pending_approval(entries) or latest_widget(entries):
        phase = "blocked"
    elif running and now - since < WORKING_TIMEOUT:
        phase = "working"
    else:
        phase = "idle"
    unread = unseen_notable(entries, seen)
    if phase == "blocked":
        state = "blocked"
    elif unread:
        state = "unread"
    else:
        state = phase
    return {
        "state": state,
        "unread": unread,
        "cursor": cursor,
        "preview": activity_preview(entries, state),
        "token": token,
        "since": since,
    }


def status_label(state, unread=0, printed=False):
    """The list says `working`. Printed `--overview` keeps the old word `running`."""
    if state == "blocked":
        return "needs you"
    if state == "unread":
        count = unread if isinstance(unread, int) else 0
        if count > 1:
            return f"{count} new"
        return "new"
    if state == "working":
        return "running" if printed else "working"
    if state == "idle":
        return "idle"
    return ""


def with_activity(bots):
    views = []
    for bot in bots:
        view = dict(bot)
        snap = lookup_activity(bot)
        if snap:
            view["activity"] = snap
        views.append(view)
    return views


def order_bots(bots):
    """Needs-you, then new, then running. A search does not use this order."""
    ranked = []
    for index, bot in enumerate(bots):
        state = ((bot.get("activity") or {}).get("state")) or ""
        rank = ATTENTION_RANK.get(state, 3)
        ranked.append((rank, index, bot))
    ranked.sort()
    return [bot for _rank, _index, bot in ranked]


def attention_counts(bots):
    counts = {state: 0 for state in OVERVIEW_STATES}
    for bot in bots:
        state = ((bot.get("activity") or {}).get("state")) or ""
        if state in counts:
            counts[state] += 1
    return counts


def _count_phrase(count, one, many):
    if count == 1:
        return one
    if count:
        return f"{count} {many}"
    return ""


def overview_summary(bots, checked=None, total=None, printed=False):
    """One line: `1 needs you · 2 new · 1 working`, plus a scan hint while reading.

    `printed` is `--overview` on stdout. That line still says `running`.
    """
    counts = attention_counts(bots)
    working = "running" if printed else "working"
    parts = [
        phrase
        for phrase in (
            _count_phrase(counts["blocked"], "1 needs you", "need you"),
            _count_phrase(counts["unread"], "1 new", "new"),
            _count_phrase(counts["working"], f"1 {working}", working),
        )
        if phrase
    ]
    scanning = bool(total) and checked is not None and checked < total
    if not parts:
        if scanning:
            if not checked:
                return "checking threads"
            return f"checking {checked} of {total}"
        return _count_phrase(counts["idle"], "1 idle", "idle")
    if scanning:
        if not checked:
            parts.append("checking threads")
        else:
            parts.append(f"checking {checked} of {total}")
    return " · ".join(parts)


def format_overview(bots):
    """Stdout for `--overview`. `running` stays the printed word for `working`."""
    ordered = order_bots(bots)
    summary = overview_summary(ordered, printed=True)
    name_width = min(max((len(bot["name"]) for bot in ordered), default=0), 24)
    lines = [summary, ""] if summary else []
    for bot in ordered:
        activity = bot.get("activity") or {}
        label = status_label(activity.get("state") or "", activity.get("unread") or 0, printed=True)
        preview = activity.get("preview") or bot.get("title") or bot.get("tagline") or ""
        preview = " ".join(str(preview).split())
        line = f"{label:<9}  {bot['name']:<{name_width}}"
        if preview:
            line += "  " + preview
        lines.append(line.rstrip())
    return "\n".join(lines)


def page_entries(page):
    entries = page.get("entries") if isinstance(page, dict) else []
    if not isinstance(entries, list):
        entries = []
    cursor = page.get("cursor") if isinstance(page, dict) else ""
    if not isinstance(cursor, str):
        cursor = ""
    return entries, cursor


def record_page(bot, page):
    """Judge one thread. The first look pins the seen cursor; a scan does not move it."""
    entries, cursor = page_entries(page)
    mark = seen_mark(bot)
    snap = judge_activity(entries, cursor, mark, previous=lookup_activity(bot))
    if not mark:
        baseline_seen(bot, snap.get("cursor") or cursor)
    store_activity(bot, snap)
    return snap


def build_overview(bots, read):
    """Read each bot once. The first look baselines; a later look marks new replies.

    Returns (views, error). `error` is set only when every read failed.
    """
    views = []
    failed = None
    saw = False
    for bot in bots:
        try:
            page = read(bot)
        except GbotError as error:
            failed = str(error)
            views.append(dict(bot))
            continue
        saw = True
        snap = record_page(bot, page)
        view = dict(bot)
        view["activity"] = snap
        views.append(view)
    return order_bots(views), (None if saw or not bots else failed)


def read_overview(bot):
    name = bot.get("name") or ""
    if not name:
        raise GbotError("No bot.")
    return read_thread(name, limit=OVERVIEW_LIMIT, timeout=20)


class Survey:
    """Walk the roster off the UI thread and publish each bot as its read returns."""

    def __init__(self, bots, read=None):
        self._read = read or read_overview
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._bots = list(bots)
        self._checked = 0
        self._total = len(self._bots)
        self._failures = 0
        self._running = True
        self._restart = False
        self.generation = 0
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def replace(self, bots):
        with self._lock:
            self._bots = list(bots)
            self._restart = True
        self._wake.set()

    def nudge(self):
        """Drop the pause and scan again. Ctrl-R on the list."""
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()

    def status(self):
        with self._lock:
            return self._running, self._checked, self._total, self.generation

    def problem(self):
        """The gbot error when every bot in the last pass failed, else ""."""
        with self._lock:
            if self._total and self._failures == self._total and not self._running:
                return self.error
            return ""

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                bots = list(self._bots)
                self._total = len(bots)
                self._checked = 0
                self._failures = 0
                self._running = True
                self._restart = False
                self.generation += 1
            for bot in bots:
                if self._stop.is_set():
                    return
                with self._lock:
                    if self._restart:
                        break
                try:
                    page = self._read(bot)
                except GbotError as error:
                    with self._lock:
                        self.error = str(error)
                        self._failures += 1
                        self._checked += 1
                        self.generation += 1
                    continue
                record_page(bot, page)
                with self._lock:
                    self._checked += 1
                    self.generation += 1
            else:
                with self._lock:
                    self._running = False
                    self.generation += 1
                self._wake.wait(OVERVIEW_PAUSE)
                self._wake.clear()
                continue
            # The roster was replaced mid-pass. Start over without the pause.


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
    """Keep the app name on the bot once its icon is on disk. The loop re-reports."""
    name = bot.get("name") or ""

    def done(made):
        key = program_key(bot.get("id") or "")
        # A later chat, or the list after Esc, owns APP now. Leave it alone.
        if key in made and APP in {"gbot-tui", key, notice_name(name)}:
            bind_app(name, bot.get("id"))

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
        shape, color_name = bot_avatar(bot)
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
        # Herdr matches this list against the OSC app. The app is the hyphenated
        # name (`Cursor-Cloud-Session`), so that string has to be here.
        human = notice_name(bot.get("name") or "")

        def aliases(slot):
            found = []
            for item in (human, slot):
                if item and item not in found:
                    found.append(item)
            return found

        entry = {
            "name": bot.get("name") or human or key,
            "icon": rel,
            "tint": False,
            "tile": "#111111",
            "color": avatar_color(color_name),
            "match": aliases(key),
        }
        made[key] = entry
        for slot in keys:
            slot_entry = dict(entry, match=aliases(slot))
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
    install_avatars([bot], root, src)
    # The icon stays under the program key. The popup reads the app string
    # ("Cursor-Cloud-Session needs you"), so the app is the hyphenated name.
    bind_app(bot.get("name"), bot.get("id"))


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
    if argv in (["--overview"], ["-o"]):
        return "overview", ""
    if argv == ["--complete"]:
        return "complete", ""
    return "open", " ".join(argv).strip()


def read_thread(target, after=None, limit=80, timeout=30):
    args = ["thread", "--limit", str(limit)]
    if after:
        args += ["--after", after]
    args.append(target)
    return parse_thread(gbot(*args, timeout=timeout))


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


# Optional path. gbot 0.12.4 has no widget command, so this hook posts the
# choice on the gateway session a one-entry thread read opens. It is off
# unless GBOT_TUI_WIDGET_HOOK is set. The answer is still a normal message.
WIDGET_HOOK = r"""
const fs = require("fs");
const orig = globalThis.fetch;
const body = process.env.GBOT_WIDGET_BODY;
const resultPath = process.env.GBOT_WIDGET_RESULT;
function copyHeaders(incoming) {
  const headers = {};
  if (incoming && typeof incoming.forEach === "function") {
    incoming.forEach((value, key) => { headers[key] = value; });
  } else if (incoming && typeof incoming === "object") {
    Object.assign(headers, incoming);
  }
  delete headers["Content-Type"];
  delete headers["content-type"];
  headers["content-type"] = "application/json";
  return headers;
}
if (typeof orig === "function" && body && resultPath) {
  let once = false;
  globalThis.fetch = async function (url, opts) {
    const href = typeof url === "string" ? url : (url && url.url) || "";
    if (!once && href.includes("/api/")) {
      once = true;
      const base = href.slice(0, href.indexOf("/api/"));
      let payload;
      try {
        const res = await orig.call(globalThis, base + "/api/respondGrokBotWidget", {
          method: "POST",
          redirect: "error",
          headers: copyHeaders(opts && opts.headers),
          body,
        });
        const text = (await res.text()).slice(0, 500);
        let confirmed = false;
        if (res.ok) {
          const trimmed = text.trim();
          if (!trimmed) {
            confirmed = true;
          } else {
            try {
              JSON.parse(trimmed);
              confirmed = true;
            } catch (err) {
              confirmed = false;
            }
          }
        }
        payload = { ok: !!(res.ok && confirmed), status: res.status, text, confirmed };
      } catch (err) {
        payload = { ok: false, status: 0, text: String((err && err.message) || err), confirmed: false };
      }
      fs.writeFileSync(resultPath, JSON.stringify(payload));
    }
    return orig.call(this, url, opts);
  };
}
"""


def widget_hook_enabled():
    return os.environ.get(WIDGET_HOOK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def widget_error(payload):
    """Empty when the gateway accepted the choice.

    A 2xx body that is not JSON is not acceptance. An empty body still counts,
    matching a gateway that answers 204 with nothing to parse.
    """
    if not isinstance(payload, dict) or not payload.get("ok"):
        text = payload.get("text") if isinstance(payload, dict) else ""
        return (text or "choice was rejected").strip() or "choice was rejected"
    if payload.get("confirmed") is False:
        return "choice was not confirmed"
    text = payload.get("text") or ""
    if not str(text).strip():
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "choice was not confirmed"
    if not isinstance(data, dict):
        return ""
    if data.get("accepted") is False or data.get("refusal"):
        return str(data.get("refusal") or data.get("message") or "choice was rejected")
    return ""


def post_widget(target, agent_id, entry_id, value):
    """POST respondGrokBotWidget through gbot's gateway session.

    Raises GbotError when the call did not confirm. `agent_id` has to be the
    gateway agent id. A session id must not be passed in that slot.
    """
    if not agent_id or not entry_id:
        raise GbotError("This choice has no agent id.")
    body = json.dumps(
        {"agentId": agent_id, "entryId": entry_id, "value": value},
        ensure_ascii=False,
    )
    hook = tempfile.NamedTemporaryFile(prefix="gbot-widget-", suffix=".cjs", delete=False)
    result = tempfile.NamedTemporaryFile(prefix="gbot-widget-", suffix=".json", delete=False)
    hook.write(WIDGET_HOOK.encode())
    hook.close()
    result.close()
    node_opts = (os.environ.get("NODE_OPTIONS") or "").strip()
    require = f"--require {hook.name}"
    extra = {
        "GBOT_WIDGET_BODY": body,
        "GBOT_WIDGET_RESULT": result.name,
        "NODE_OPTIONS": f"{node_opts} {require}".strip(),
    }
    thread_error = None
    try:
        try:
            gbot("thread", "--limit", "1", target, timeout=45, env_extra=extra)
        except GbotError as error:
            thread_error = error
        try:
            with open(result.name, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            raw = ""
        if not raw:
            raise thread_error or GbotError("could not reach the bot to send that choice")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GbotError("choice was rejected") from error
        message = widget_error(payload)
        if message:
            raise GbotError(message)
    finally:
        for path in (hook.name, result.name):
            try:
                os.unlink(path)
            except OSError:
                pass


def respond_widget(target, agent_id, entry_id, value):
    """Deliver one widget answer as the user's reply.

    The value goes out with `gbot send`. `GBOT_TUI_WIDGET_HOOK=1` tries the
    gateway hook first. A failure there, including a 2xx body that is not
    JSON, falls back to the same send. `agent_id` empty means the only id on
    hand was a session id, so the hook is skipped.
    """
    if widget_hook_enabled() and agent_id and entry_id:
        try:
            post_widget(target, agent_id, entry_id, value)
            return
        except GbotError as hook_error:
            try:
                send_message(target, value)
            except GbotError as send_error:
                raise GbotError(f"{hook_error}; {send_error}") from send_error
            return
    send_message(target, value)


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
    herdr, pane = herdr_cli()
    if not herdr or not pane:
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
    herdr, pane = herdr_cli()
    if not herdr or not pane:
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
        self.agent_id = ""
        self.viewer_session = ""
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
        self.widget_picks = {}
        self.widget_sent = {}
        self.widget_steps = {}
        self.widget_busy = False
        self.widget_entry = ""
        self.widget_phase = "idle"
        self._held_steps = None
        self.choice_wait = False
        self.content_token = ""
        self.content_at = None
        self.digit_buf = ""
        self.digit_at = None
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
            bind_app(self.title)
        if page.get("agent_id"):
            self.agent_id = page["agent_id"]
        elif page.get("id") and not page.get("viewer_session"):
            # target.id is the agent id when the thread names no other session.
            self.agent_id = page["id"]
        elif "agent_id" in page and page.get("viewer_session"):
            self.agent_id = page.get("agent_id") or ""
        if page.get("viewer_session"):
            self.viewer_session = page["viewer_session"]
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
        self._stamp_choices()
        self.note_content()
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

    def _stamp_choices(self):
        """A poll that has not caught up must not wipe a choice just sent."""
        for entry in self.messages:
            value = self.widget_sent.get(entry.get("id"))
            if value and not widget_answered(entry):
                entry["respondedValue"] = value

    def touch_rows(self):
        self.rows_rev += 1
        self._rows_key = None

    def sync_activity(self):
        """The open chat is what the list shows on the way back.

        A reply on screen is not new. `working` stays running while it arrives.
        """
        entries = [entry for entry in self.messages if isinstance(entry, dict)]
        cursor = self.cursor or last_id([entry for entry in entries if not entry.get("local")])
        remember_seen(self.target, self.session_id, cursor)
        shown = [entry for entry in entries if not entry.get("local")]
        # An open widget is needs-you on the list even while this chat is idle.
        if self.phase == "blocked" or (self.phase != "working" and latest_widget(shown)):
            state = "blocked"
        elif self.phase == "working":
            state = "working"
        else:
            state = "idle"
        store_activity(
            {"id": self.session_id, "name": self.target},
            {
                "state": state,
                "unread": 0,
                "cursor": cursor,
                "preview": activity_preview(shown, state),
                "token": content_token(shown),
                "since": 0 if self.content_at is None else self.content_at,
            },
        )

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
        self.sync_activity()
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
        self.sync_activity()

    def choice_agent_id(self):
        """Agent id for the optional hook. A viewer session is never that id."""
        if self.agent_id:
            return self.agent_id
        if self.viewer_session:
            return ""
        return self.session_id

    def step_at(self, entry):
        if not isinstance(entry, dict):
            return 0
        return (self.widget_steps.get(entry.get("id")) or {}).get("at", 0)

    def note_content(self):
        """Restart the quiet-turn clock when the transcript text changes."""
        token = content_token(self.messages)
        now = clock()
        if token != self.content_token or self.content_at is None:
            self.content_token = token
            self.content_at = now

    def quiet_for(self):
        """True when the visible text has been unchanged for WORKING_TIMEOUT."""
        now = clock()
        if self.content_at is None:
            self.content_at = now
            return False
        return now - self.content_at >= WORKING_TIMEOUT

    def content_expired(self):
        return self.phase == "working" and self.quiet_for()

    def settle_working(self, phase):
        """Keep a live reply in `working`, and give up when it stops changing."""
        if phase == "done" and entry_in_progress(self.messages):
            phase = "working"
        # Any turn with no new text, including one whose last line is the user's
        # or whose isStreaming flag never clears.
        if phase == "working" and self.quiet_for():
            return "idle"
        if phase in {"idle", "done"} and entry_in_progress(self.messages) and not self.quiet_for():
            return "working"
        return phase

    def expire_working(self):
        """Drop a turn that has had no new text for WORKING_TIMEOUT."""
        if not self.content_expired():
            return False
        self.choice_wait = False
        self.note("idle")
        self.sync_activity()
        return True

    def begin_choice(self, value):
        """Mark the newest open widget answered and return the gateway arguments.

        The mark shows immediately. The value is what `gbot send` will carry.
        `agent_id` is empty when the thread only had a session id.
        """
        if self.widget_busy or not self.loaded or not (self.agent_id or self.session_id):
            return None
        entry = latest_widget(self.messages)
        if entry is None:
            return None
        entry_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
        if not entry_id or not value:
            return None
        self.widget_sent[entry_id] = value
        entry["respondedValue"] = value
        self.widget_picks.pop(entry_id, None)
        self.messages.append({"role": "user", "text": value, "local": True, "widget_answer": entry_id})
        self.widget_entry = entry_id
        self.widget_busy = True
        self.widget_phase = self.phase
        self.choice_wait = True
        self.saw_other = False
        self.quiet = 0
        self.touch_rows()
        self.note_content()
        self.note("working")
        self.sync_activity()
        return (self.target, self.choice_agent_id(), entry_id, value)

    def commit_choice(self, value):
        """Record one answer. A finished card returns the gateway arguments.

        A `steps` card advances one question at a time and sends once, with a
        blank line between answers. "step" means another question is waiting.
        """
        if not value:
            return None
        entry = latest_widget(self.messages)
        if entry is None:
            return None
        steps = step_questions(entry)
        if not steps:
            return self.begin_choice(value)
        entry_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
        if not entry_id:
            return None
        state = self.widget_steps.get(entry_id) or {"at": 0, "answers": []}
        answers = list(state.get("answers") or [])
        at = int(state.get("at") or 0)
        if at >= len(steps):
            return None
        answers.append(value)
        at += 1
        self.widget_picks.pop(entry_id, None)
        if at < len(steps):
            self.widget_steps[entry_id] = {"at": at, "answers": answers}
            self.touch_rows()
            return "step"
        self._held_steps = (entry_id, {"at": len(steps) - 1, "answers": answers[:-1]})
        self.widget_steps.pop(entry_id, None)
        return self.begin_choice("\n\n".join(answers))

    def toggle_choice(self, index):
        entry = latest_widget(self.messages)
        if entry is None:
            return False
        _prompt, options, multi, _custom = current_question(entry, self.step_at(entry))
        if not multi:
            return False
        if index < 0 or index >= len(options):
            return False
        entry_id = entry.get("id")
        value = options[index][1]
        picks = list(self.widget_picks.get(entry_id) or [])
        if value in picks:
            picks.remove(value)
        else:
            picks.append(value)
        if picks:
            self.widget_picks[entry_id] = picks
        else:
            self.widget_picks.pop(entry_id, None)
        self.touch_rows()
        return True

    def finish_choice(self):
        """The widget call returned. The next read is the whole thread."""
        self.widget_busy = False
        self.widget_entry = ""
        self.full_reload = True

    def fail_choice(self):
        """The widget call failed. Put the options back so they can be picked again."""
        entry_id = self.widget_entry
        value = self.widget_sent.pop(entry_id, None)
        self.widget_busy = False
        self.widget_entry = ""
        self.choice_wait = False
        self.messages = [
            entry
            for entry in self.messages
            if not (entry.get("local") and entry.get("widget_answer") == entry_id)
        ]
        for entry in self.messages:
            if entry.get("id") == entry_id and entry.get("respondedValue") == value:
                entry.pop("respondedValue", None)
        if self._held_steps and self._held_steps[0] == entry_id:
            self.widget_steps[entry_id] = self._held_steps[1]
        self._held_steps = None
        self.touch_rows()
        self.note_content()
        phase = self.widget_phase if self.widget_phase in {"working", "blocked"} else "idle"
        self.note(phase)
        self.sync_activity()

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
            # An older answer is not this turn's reply. Counting it would
            # clear `working` after two quiet polls while the bot is still going.
            if (
                self.phase == "working"
                and not self.choice_wait
                and not awaiting_reply(self.messages)
                and not entry_in_progress(self.messages)
                and any(not is_user(entry) and entry_text(entry).strip() for entry in self.messages)
            ):
                self.saw_other = True
            self.choice_wait = False
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
            elif self.phase == "working" or awaiting_reply(self.messages) or entry_in_progress(self.messages):
                phase = "working"
            else:
                phase = "idle"
            phase = self.settle_working(phase)
            self.phase = phase
            publish(phase, self.title, self.session_id, self.resume)
            TAB.show(self.title)
            self.sync_activity()
            return
        phase, self.quiet, self.saw_other = watch_step(
            self.phase,
            fresh,
            self.quiet,
            self.saw_other,
            pending_approval(self.messages),
        )
        phase = self.settle_working(phase)
        self.note(phase)
        self.sync_activity()

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
        self.note_content()
        self.note("working")
        self.sync_activity()
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


# Herdr paints #282C34 for about two seconds after a tab opens, then reports
# the real background. A fresh light tab otherwise looks dark.
HERDR_DEFAULT_BG = "#282C34"
THEME_LIMIT = 5
THEME_GAP = 0.5
THEME = {"watch": False, "tries": 0, "due": 0.0, "follow": False}


# Herdr Desktop 0.1.11 panes set none of HERDR_ENV / HERDR_PANE_ID. They do set these.
DESKTOP_MARKS = ("HC_TERMINAL_ID", "HC_SCOPE", "HC_TERMINAL_SOCKET")


def herdr_session():
    """True in a Herdr pane, including Desktop, which only publishes HC_* variables."""
    if os.environ.get("HERDR_ENV") == "1" or os.environ.get("HERDR_PANE_ID"):
        return True
    return any(os.environ.get(name) for name in DESKTOP_MARKS)


def herdr_cli():
    """The `herdr` binary and pane id, or ("", "") when the CLI cannot be called.

    OSC 7501 does not need either value, so Desktop still gets icons and popups.
    The CLI report runs for any detected session that also has HERDR_BIN_PATH
    and HERDR_PANE_ID, including when HERDR_ENV itself is unset.
    """
    if not herdr_session():
        return "", ""
    return os.environ.get("HERDR_BIN_PATH") or "", os.environ.get("HERDR_PANE_ID") or ""


def theme_override():
    folded = (os.environ.get(THEME_ENV) or "").strip().lower()
    return folded if folded in {"dark", "light"} else ""


def placeholder_background(color):
    return (color or "").upper() == HERDR_DEFAULT_BG


def fold_sample(current, sample, attempt, limit=THEME_LIMIT, follow=False):
    """One OSC 11 sample. (color, settled).

    A Herdr default-dark sample is not the answer while attempts remain.
    `follow` keeps a real color already seen, through a later default-dark
    sample, so a resize does not flash the theme back to black.
    """
    got = (sample or "").upper() or None
    held = (current or "").upper() or None
    if got and got != HERDR_DEFAULT_BG:
        return got, True
    if got == HERDR_DEFAULT_BG:
        if follow and held and held != HERDR_DEFAULT_BG:
            return held, attempt >= limit
        if attempt >= limit:
            return HERDR_DEFAULT_BG, True
        return held or HERDR_DEFAULT_BG, False
    if attempt >= limit:
        return held, True
    return held, False


def detect_theme():
    """Probe once before curses. A #282C34 sample is checked again after the first paint.

    Herdr Desktop answers with its default dark for about two seconds. The same
    color is rechecked outside Herdr too: five probes, half a second apart.
    """
    if theme_override():
        THEME["watch"] = False
        return
    PROBED["bg"] = probe_background()
    if placeholder_background(PROBED["bg"]):
        THEME.update(watch=True, tries=1, due=time.monotonic() + THEME_GAP, follow=False)
    else:
        THEME.update(watch=False, tries=0, due=0.0, follow=not placeholder_background(PROBED["bg"]))


def note_appearance():
    """The window changed. Look again, unless GBOT_TUI_THEME already decided."""
    if theme_override() or not herdr_session():
        return
    THEME["watch"] = True
    THEME["due"] = 0
    THEME["follow"] = not placeholder_background(PROBED["bg"])
    THEME["tries"] = 0


def theme_due_ms():
    """How long the list may block before the next background check, or None."""
    if theme_override() or not THEME["watch"]:
        return None
    return max(int((THEME["due"] - time.monotonic()) * 1000), 30)


def maybe_retheme(apply=None):
    """Take the next background sample when it is due. True when the theme changed."""
    if theme_override() or not THEME["watch"] or time.monotonic() < THEME["due"]:
        return False
    THEME["tries"] += 1
    sample = probe_background(timeout=0.15)
    color, settled = fold_sample(PROBED["bg"], sample, THEME["tries"], follow=THEME["follow"])
    before = (PROBED["bg"] or "").upper()
    changed = bool(color) and color != before
    if color:
        PROBED["bg"] = color
    THEME["watch"] = not settled
    if settled:
        THEME["follow"] = not placeholder_background(PROBED["bg"])
    else:
        THEME["due"] = time.monotonic() + THEME_GAP
    if changed and apply:
        apply()
    return changed


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
    """256-color indices for the named styles. Yellow on white is unreadable.

    Cyan is the `working` mark, so it stays off the yellow used for needs-you.
    """
    if dark:
        return {"green": 108, "gray": 245, "cyan": 44}
    return {"yellow": 130, "red": 124, "green": 28, "gray": 240, "cyan": 23}


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
    """The bot's color, lightened or darkened until it reads on this background.

    Black is #000000. On a dark terminal that raw color is the background, so
    the list dot and the chat mark use the same lifted gray as the other colors.
    """
    folded = (name or "").lower()
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
    picks = getattr(chat, "widget_picks", None) or {}
    progress = getattr(chat, "widget_steps", None) or {}
    key = (
        id(chat.messages),
        len(chat.messages),
        width,
        entry_text(last) if last else "",
        chat.rows_rev,
        tuple((name, tuple(values)) for name, values in picks.items()),
        tuple(
            (name, state.get("at"), tuple(state.get("answers") or ()))
            for name, state in progress.items()
            if isinstance(state, dict)
        ),
    )
    if getattr(chat, "_rows_key", None) != key:
        chat._rows = transcript_rows(chat.messages, width, picks, progress)
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


def transcript_rows(entries, width, picks=None, progress=None):
    rows = []
    user_w = max(width - 8, 1)
    text_w = max(width - 4, 1)
    picks = picks or {}
    progress = progress or {}
    current = latest_widget(entries)
    current_id = current.get("id") if current else None
    for entry in entries:
        lines = card_lines(entry, width)
        chosen = picks.get(entry.get("id")) if isinstance(entry, dict) else None
        # No open widget means nothing is numbered, including a stale one
        # that still has options because a later user message closed it.
        numbered = bool(current_id) and isinstance(entry, dict) and entry.get("id") == current_id
        state = progress.get(entry.get("id")) if isinstance(entry, dict) else None
        step_at = (state or {}).get("at", 0) if isinstance(state, dict) else 0
        step_answers = (state or {}).get("answers") or () if isinstance(state, dict) else ()
        text = entry_text(entry, chosen or (), numbered, step_at, step_answers).strip()
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


def title_spans(title, phase, width):
    """The header name, and a cyan `working` while a reply is in progress."""
    room = max(width - 4, 0)
    mark = " · working" if phase == "working" else ""
    if mark and room > len(mark):
        return [(clip(title, room - len(mark)), "bold"), (mark, STATUS_STYLE["working"])]
    return [(clip(title, room), "bold")]


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


def footer_spans(phase, error, decide=False, width=0, choice=""):
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
    rest = "esc bots · ctrl-c quit · pgup scroll"
    if choice:
        # One "enter". A multi-select hint already says "enter sends".
        enter = "" if "enter" in choice.lower() else "enter send · "
        hint = f"{choice} · space then digit types · {enter}{rest}"
    else:
        hint = f"enter send · {rest}"
    return [(hint, "dim")]


def footer_rows(phase, error, decide=False, width=0, choice=""):
    """Footer lines, bottom last. An error too long for one line gets rows of its own.

    A 401 or a gateway message has to stay readable in a 58-column window, so
    it is wrapped above the footer rather than clipped beside the y/n hint.
    """
    room = max(width - 2, 1)
    hint = len(SHORT_HINT) if phase == "blocked" and decide else 0
    if not error or len(error) + hint <= room:
        return [footer_spans(phase, error, decide, width, choice)]
    lines = wrap(error, room)
    if len(lines) > ERROR_ROWS:
        lines = lines[: ERROR_ROWS - 1] + [clip(" ".join(lines[ERROR_ROWS - 1 :]), room)]
    rows = [[(line, "red")] for line in lines]
    rows.append(footer_spans(phase, "", decide, width, choice))
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
    "cyan": curses.A_BOLD,
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
        curses.init_pair(7, palette.get("cyan", curses.COLOR_CYAN), -1)
        ATTR["yellow"] = curses.color_pair(3)
        ATTR["red"] = curses.color_pair(4)
        ATTR["cyan"] = curses.color_pair(7)
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
    # erase() only diffs against the last frame. Inserting the local reply
    # shifts every line; Herdr can leave a ghost of the old ones unless the
    # next refresh clears the physical screen first.
    rows_now = visible_rows(chat, width)
    stamp = (len(rows_now), width, height)
    if stamp != getattr(chat, "_drawn_stamp", None):
        try:
            stdscr.clearok(True)
        except (AttributeError, curses.error):
            pass
        chat._drawn_stamp = stamp
    try:
        stdscr.erase()
    except (AttributeError, curses.error):
        pass
    lines = draft_lines(chat.typed, width)
    header, body_h, composer_h, footer_h = layout(height, len(lines) or 1)
    footer = []
    if footer_h:
        decide = bool(latest_decision(chat.messages, chat.answered)) and not chat.deciding
        hint = ""
        if getattr(chat, "loaded", False) and not getattr(chat, "widget_busy", False) and not chat.typed:
            entry = latest_widget(chat.messages)
            hint = choice_hint(entry, chat.step_at(entry) if entry else 0)
        footer = footer_rows(chat.phase, chat.error, decide=decide, width=width, choice=hint)
        # Error rows come out of the transcript, which keeps at least one row.
        footer = footer[-max(min(len(footer), body_h), 1) :]
        body_h -= len(footer) - 1
    color = getattr(chat, "avatar_color", "")
    label = getattr(chat, "label", "")
    if header:
        put(stdscr, 0, 1, "●", dot_attr(color))
        paint_spans(stdscr, 0, 3, title_spans(chat.title, chat.phase, width))
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


def page_motion(key):
    """(direction, half). Alt-Page is kPRV3/kNXT3; plain Page is KEY_PPAGE/KEY_NPAGE."""
    if key == curses.KEY_PPAGE:
        return -1, False
    if key == curses.KEY_NPAGE:
        return 1, False
    if isinstance(key, int):
        try:
            name = curses.keyname(key)
        except curses.error:
            name = b""
        if name in (b"kPRV3", "kPRV3"):
            return -1, True
        if name in (b"kNXT3", "kNXT3"):
            return 1, True
    return None


def page_step(lines, half):
    """A screen, or half of one. Same split as a Herdr pane."""
    step = max(lines - 4, 1)
    return max(step // 2, 1) if half else step


def picked_lines(entry, picks, step_at=0):
    _prompt, options, _multi, _custom = current_question(entry, step_at)
    chosen = set(picks or ())
    return "\n".join(value for _label, value in options if value in chosen)


def clear_choice_digit(chat):
    chat.digit_buf = ""
    chat.digit_at = None


def _digit_fresh(chat):
    if not getattr(chat, "digit_buf", "") or chat.digit_at is None:
        return False
    return clock() - chat.digit_at <= DIGIT_WINDOW


def _activate_option(chat, number):
    """1-based option. A tuple is the send job. "toggle", "step", or "ignore"."""
    entry = latest_widget(chat.messages)
    if entry is None or not chat.loaded or not (chat.agent_id or chat.session_id):
        return "ignore"
    _prompt, options, multi, _custom = current_question(entry, chat.step_at(entry))
    if number < 1 or number > len(options):
        return "ignore"
    index = number - 1
    if multi:
        chat.toggle_choice(index)
        return "toggle"
    result = chat.commit_choice(options[index][1])
    if result == "step":
        chat.error = ""
        return "step"
    if not result:
        return "ignore"
    chat.error = ""
    return ("choice", result)


def flush_choice_digit(chat):
    """Once the window passes, a lone 1–9 selects. `0` is dropped.

    Returns the same kinds of value as `choice_key`, or None if still waiting.
    """
    if not getattr(chat, "digit_buf", "") or chat.digit_at is None:
        return None
    if clock() - chat.digit_at <= DIGIT_WINDOW:
        return None
    buf = chat.digit_buf
    clear_choice_digit(chat)
    if len(buf) != 1 or buf == "0":
        return None
    entry = latest_widget(chat.messages)
    if entry is None:
        return None
    _prompt, options, _multi, _custom = current_question(entry, chat.step_at(entry))
    if len(options) <= OPTION_DIGITS:
        return None
    return _activate_option(chat, int(buf))


def choice_pause(chat):
    """Seconds left on a two-digit entry, or None."""
    if not _digit_fresh(chat):
        return None
    return max(DIGIT_WINDOW - (clock() - chat.digit_at), 0)


def choice_key(chat, key):
    """What a digit does while a widget is open.

    A tuple is the send job. "toggle", "step", "wait", and "ignore" consume
    the key. None means the key is ordinary text. A letter is always text, so
    a message can start with one. With more than 9 options the choice is two
    digits inside DIGIT_WINDOW (`10`, or `02`). Text already in the composer
    wins, so a space and then a digit types the digit.
    """
    if not isinstance(key, str) or len(key) != 1:
        return None
    if chat.typed or key.isalpha():
        clear_choice_digit(chat)
        return None
    if chat.widget_busy:
        if key.isdigit():
            return "ignore"
        return None
    entry = latest_widget(chat.messages)
    if entry is None:
        clear_choice_digit(chat)
        return None
    if not chat.loaded or not (chat.agent_id or chat.session_id):
        if key.isdigit():
            return "ignore"
        return None
    _prompt, options, _multi, _custom = current_question(entry, chat.step_at(entry))
    if not options or not key.isdigit():
        return None
    if len(options) <= OPTION_DIGITS:
        clear_choice_digit(chat)
        picked = key_to_index(key, len(options))
        if picked is None:
            return None
        if picked == "ignore":
            return "ignore"
        return _activate_option(chat, picked + 1)
    if _digit_fresh(chat) and chat.digit_buf:
        number = int(chat.digit_buf + key)
        clear_choice_digit(chat)
        if number < 1 or number > len(options):
            return "ignore"
        return _activate_option(chat, number)
    chat.digit_buf = key
    chat.digit_at = clock()
    return "wait"


def chat_key(chat, key, stdscr):
    """Apply one key. Returns "quit", "back", a job tuple, or None."""
    if key == curses.KEY_RESIZE:
        return None
    if key in ("\x03", "\x04"):
        return "quit"
    if key in ("\x1b", 27):
        return "back"
    chat.nudge()
    motion = page_motion(key)
    if motion is not None:
        direction, half = motion
        step = page_step(curses.LINES, half)
        if direction < 0:
            chat.follow = False
            chat.scroll = max(chat.scroll - step, 0)
            return None
        chat.scroll += step
        height, width = stdscr.getmaxyx()
        page_header, body_h, _composer, _footer = layout(height, len(draft_lines(chat.typed, width)) or 1)
        room = max(body_h - header_gap(page_header, chat.label, body_h), 0)
        rows = visible_rows(chat, width)
        if chat.scroll >= max(len(rows) - room, 0):
            chat.follow = True
        return None
    if key in ("\n", "\r", curses.KEY_ENTER):
        entry = latest_widget(chat.messages) if not chat.widget_busy else None
        if entry is not None and chat.typed.strip():
            _prompt, _options, _multi, custom = current_question(entry, chat.step_at(entry))
            if custom:
                text = chat.typed.strip()
                chat.typed = ""
                chat.error = ""
                result = chat.commit_choice(text)
                if result == "step":
                    return None
                if result:
                    chat.follow = True
                    return ("choice", result)
        if entry is not None and not chat.typed:
            _prompt, _options, multi, _custom = current_question(entry, chat.step_at(entry))
            if multi:
                value = picked_lines(entry, chat.widget_picks.get(entry.get("id")), chat.step_at(entry))
                result = chat.commit_choice(value) if value else None
                if result == "step":
                    chat.error = ""
                    chat.follow = True
                    return None
                if result:
                    chat.error = ""
                    chat.follow = True
                    return ("choice", result)
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
        if getattr(chat, "digit_buf", "") and not chat.typed:
            clear_choice_digit(chat)
            return None
        chat.typed = chat.typed[:-1]
        return None
    picked = choice_key(chat, key)
    if picked in {"ignore", "toggle", "step", "wait"}:
        return None
    if isinstance(picked, tuple):
        return picked
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
                elif kind == "choice":
                    respond_widget(*args)
                    finish(("chosen", None))
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
                    if source == "choice":
                        chat.fail_choice()
                    if source == "send" and chat.phase == "working":
                        chat.note("idle")
                    if source == "read":
                        chat.read_failed()
                        next_poll = time.monotonic() + IDLE_DELAYS[0]
                elif kind in {"sent", "decided", "chosen"}:
                    if kind == "decided":
                        chat.finish_decision()
                    elif kind == "chosen":
                        chat.finish_choice()
                    next_poll = 0
                elif not chat.loaded:
                    chat.ingest(payload, first=True)
                    write_thread_cache(chat.target, chat.messages)
                    next_poll = time.monotonic() + chat.poll_delay()
                else:
                    chat.ingest(payload, first=False)
                    next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
            if maybe_retheme(lambda: apply_theme(stdscr)):
                dirty = True
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
            if chat.expire_working():
                dirty = True
            flushed = flush_choice_digit(chat)
            if isinstance(flushed, tuple):
                draw(stdscr, chat)
                dirty = False
                jobs.put(flushed)
                waiting += 1
                next_poll = min(next_poll, time.monotonic() + chat.poll_delay())
            elif flushed is not None:
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
                pause = choice_pause(chat)
                if pause is not None:
                    wait = min(wait, pause)
                watch = [wake_r] + ([tty] if tty is not None else [])
                try:
                    select.select(watch, [], [], wait)
                except (OSError, ValueError):
                    time.sleep(min(wait, 0.02))
                keys = read_keys(stdscr)
            for key in keys:
                dirty = True
                if key == curses.KEY_RESIZE:
                    note_appearance()
                    if maybe_retheme(lambda: apply_theme(stdscr)):
                        dirty = True
                result = chat_key(chat, key, stdscr)
                if result == "quit":
                    return None
                if result == "back":
                    return "back"
                if isinstance(result, tuple):
                    if result[0] in {"send", "choice"}:
                        # The bubble, or the chosen option, is on screen before gbot starts.
                        draw(stdscr, chat)
                        dirty = False
                    jobs.put(result)
                    waiting += 1
                    if result[0] in {"send", "choice"}:
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


STATE_COLUMN = 10


def list_columns(bots, width):
    """Shared name and status widths so every preview starts at the same column.

    A narrow window gives the name less room before it gives up the grid.
    """
    room = max(width - 7, 1)
    overhead = 1 + STATE_COLUMN
    if room <= overhead + 4:
        return None
    names = []
    for bot in bots:
        activity = bot.get("activity") if isinstance(bot.get("activity"), dict) else None
        if activity and activity.get("state") in OVERVIEW_STATES and bot.get("name"):
            names.append(bot["name"])
    if not names:
        return None
    rest = room - overhead
    # 58 columns still keeps a short preview. Below that, keep a few characters.
    preview_floor = 12 if width >= 58 else 4
    name_w = min(max(len(name) for name in names), max(rest - preview_floor, 1))
    if name_w > rest:
        name_w = rest
    if name_w < 1:
        return None
    return {"name": name_w, "state": STATE_COLUMN}


def _pad(text, width):
    if len(text) >= width:
        return text
    return text + (" " * (width - len(text)))


def _status_spans(lead, lead_style, mark, name, name_style, activity, title, room, columns=None):
    label = status_label(activity.get("state"), activity.get("unread") or 0)
    preview = " ".join((activity.get("preview") or title or "").split())
    state_w = columns["state"] if columns else STATE_COLUMN
    field = _pad(label, state_w)[: max(state_w, len(label))]
    if columns:
        name_w = columns["name"]
        if name_w + 1 + state_w > room:
            name_w = max(room - 1 - state_w, 1)
        shown = _pad(clip(name, name_w), name_w)
        preview_room = room - name_w - 1 - len(field)
        spans = [
            (lead, lead_style),
            mark,
            (shown, name_style),
            (" ", "dim"),
            (field, STATUS_STYLE.get(activity.get("state"), "dim")),
        ]
        if preview and preview_room > 0:
            spans.append((clip(preview, preview_room), "dim"))
        return spans
    overhead = 1 + len(field)
    if room <= overhead:
        return [(lead, lead_style), mark, (clip(name, room), name_style)]
    rest = room - overhead
    name_room = len(name)
    if name_room > rest:
        name_room = rest
    elif preview and name_room > (rest * 2) // 3 and rest >= 12:
        name_room = max((rest * 2) // 3, 1)
    preview_room = rest - name_room
    spans = [
        (lead, lead_style),
        mark,
        (clip(name, name_room), name_style),
        (" ", "dim"),
        (field, STATUS_STYLE.get(activity.get("state"), "dim")),
    ]
    if preview and preview_room > 0:
        spans.append((clip(preview, preview_room), "dim"))
    return spans


def picker_spans(bot, selected, width, columns=None):
    """One sidebar row: selection mark, avatar, name, status, and a preview.

    A row with no activity yet keeps the tagline, the same as before a scan.
    """
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
    activity = bot.get("activity") if isinstance(bot.get("activity"), dict) else None
    if activity and activity.get("state") in OVERVIEW_STATES:
        return _status_spans(lead, lead_style, mark, name, name_style, activity, title, room, columns)
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


def list_banner(query, summary, width):
    """The line under the title: the search, the counts, or both."""
    room = max(width - 2, 0)
    if not room:
        return []
    if query and summary:
        left = clip("/" + query, max(room // 2, 1))
        rest = max(room - len(left) - 2, 0)
        spans = [(left, "dim")]
        if rest:
            spans.append(("  ", "dim"))
            spans.append((clip(summary, rest), "dim"))
        return spans
    if query:
        return [(clip("/" + query, room), "dim")]
    if summary:
        return [(clip(summary, room), "dim")]
    return []


def preview_score(query, text):
    """A preview matches only as a contiguous substring, not scattered letters.

    A long reply is full of letters. Fuzzy order through it pulled in most of
    the list for a short query. A word start ranks above a match mid-word, and
    both stay under a real name match.
    """
    query = query.casefold()
    if not query:
        return 0
    hay = str(text).casefold()
    found = hay.find(query)
    if found < 0:
        return None
    word = found == 0 or not hay[found - 1].isalnum()
    return 800 - min(found, 700) + (40 if word else 0)


def fuzzy_bots(query, bots):
    if not query:
        return list(bots)
    ranked = []
    for index, bot in enumerate(bots):
        scores = [fuzzy_score(query, bot.get("name") or "")]
        title = bot.get("title") or ""
        if title:
            scores.append(fuzzy_score(query, title))
        preview = ((bot.get("activity") or {}).get("preview")) or ""
        if preview:
            scores.append(preview_score(query, preview))
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


LIST_HINT = "type to search · enter open · ctrl-r refresh · ctrl-c quit"


def pick(stdscr, bots, refresh=None):
    """The bot list, with who needs you, who is working, and who has a new reply.

    `refresh` fetches a newer roster on a thread. A survey reads each thread
    beside that. Until you move, the highlight stays on the top row, which is
    the bot that needs attention. A search keeps match order.
    """
    # The chat loop reads keys without waiting. Leaving that on makes get_wch
    # raise "no input" the moment this list draws.
    stdscr.timeout(-1)
    apply_theme(stdscr)
    fast_escape()
    place_cursor(False)
    stdscr.keypad(True)
    bots = list(bots)
    loader = Loader(refresh) if refresh else None
    survey = Survey(bots)
    note = ""
    selected = 0
    query = ""
    # False until Up or Down. The top row then stays the one that needs attention.
    moved = False
    chosen = ""
    try:
        while True:
            if loader:
                ready, fresh, error = loader.take()
                if ready:
                    loader = None
                    if error and not bots:
                        raise GbotError(error)
                    if error:
                        note = error
                    elif fresh is not None:
                        if not fresh and not bots:
                            raise GbotError("No bots.")
                        bots = list(fresh)
                        survey.replace(bots)
            views = with_activity(bots)
            shown = fuzzy_bots(query, views)
            if not query:
                shown = order_bots(shown)
            if moved and chosen:
                names = [bot["name"] for bot in shown]
                if chosen in names:
                    selected = names.index(chosen)
                else:
                    moved = False
                    chosen = ""
            if not moved:
                selected = 0
            if not shown:
                selected = 0
            elif selected >= len(shown):
                selected = len(shown) - 1
            running, checked, total, _generation = survey.status()
            if running:
                summary = overview_summary(shown, checked, total)
            else:
                summary = overview_summary(shown)
            problem = survey.problem()
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            put(stdscr, 0, 1, "Grok bots", ATTR["bold"])
            banner = list_banner(query, summary, width)
            if banner:
                paint_spans(stdscr, 1, 1, banner)
            footer = 1 if height >= 4 else 0
            body_h = max(height - 2 - footer, 0)
            top = 0
            if body_h and selected >= body_h:
                top = selected - body_h + 1
            if not shown and body_h:
                empty = "  loading bots…" if loader and not bots else "  no match"
                put(stdscr, 2, 1, clip(empty, max(width - 2, 0)), ATTR["dim"])
            columns = list_columns(shown, width)
            for index, bot in enumerate(shown[top : top + body_h]):
                actual = top + index
                paint_spans(stdscr, 2 + index, 1, picker_spans(bot, actual == selected, width, columns))
            if footer:
                if note:
                    spans = [(clip(note, max(width - 2, 0)), "red")]
                elif problem:
                    spans = [(clip(problem, max(width - 2, 0)), "red")]
                else:
                    spans = [(LIST_HINT, "dim")]
                paint_spans(stdscr, height - 1, 1, spans)
            stdscr.refresh()
            due = theme_due_ms()
            wait = 30 if loader else (150 if running else 400)
            if due is not None:
                wait = min(wait, due)
            stdscr.timeout(wait)
            try:
                key = stdscr.get_wch()
            except curses.error:
                if maybe_retheme(lambda: apply_theme(stdscr)):
                    pass
                continue
            if key == curses.KEY_RESIZE:
                note_appearance()
                maybe_retheme(lambda: apply_theme(stdscr))
                continue
            if key in ("\x03", "\x04"):
                return None
            if key == "\x12":
                survey.nudge()
                continue
            if key in ("\x1b", 27):
                query = ""
                selected = 0
                moved = False
                chosen = ""
                continue
            if key == curses.KEY_UP and selected > 0:
                selected -= 1
                moved = True
                chosen = shown[selected]["name"] if shown else ""
                continue
            if key == curses.KEY_DOWN and selected < len(shown) - 1:
                selected += 1
                moved = True
                chosen = shown[selected]["name"] if shown else ""
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                if shown:
                    return shown[selected]["name"]
                continue
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query = query[:-1]
                selected = 0
                moved = False
                chosen = ""
                continue
            if isinstance(key, str) and key.isprintable():
                query += key
                selected = 0
                moved = False
                chosen = ""
    finally:
        survey.close()


def start_chat(target):
    """A chat with the last thread already on screen. The real read is the loop's first job."""
    bot = ROSTER.get(target)
    if bot:
        bind_app(bot.get("name") or target, bot.get("id"))
        show_avatar_later(bot)
    else:
        bind_app(target)
    chat = Chat(target)
    chat.show_cached(read_thread_cache(target))
    publish("idle", target, "", chat.resume)
    return chat


def leave_chat(chat, final):
    # Clear the bot under its own app name, then report the list as gbot-tui.
    # Herdr keeps the last app until a new report arrives.
    emit("clear", chat.title)
    global APP
    APP = "gbot-tui"
    TAB.restore()
    if not final:
        emit("idle", LIST_TITLE)
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
    accept_ctrl_c()
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
gbot-tui --overview

Open a chat with one Grok bot. A name opens that bot; a unique prefix works too.
With no name, pick from the list. The list shows which bots need you, which
are working, and which have a new reply. --list prints every name.
--overview prints that status and the latest line, then exits.
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


def print_overview():
    try:
        bots = list_bots(avatars="skip")
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    if not bots:
        print("No bots.", file=sys.stderr)
        return 1
    views, error = build_overview(
        bots,
        lambda bot: read_thread(bot["name"], limit=OVERVIEW_LIMIT, timeout=20),
    )
    if error:
        print(error, file=sys.stderr)
        return 1
    print(format_overview(views))
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


TITLE = {"pushed": False, "popped": False}
EXITED = {"done": False}


def push_session_title():
    if TITLE["pushed"]:
        return
    write_tty(PUSH_TITLE)
    TITLE["pushed"] = True
    TITLE["popped"] = False


def pop_session_title():
    if not TITLE["pushed"] or TITLE["popped"]:
        return
    TITLE["popped"] = True
    write_tty(POP_TITLE)


def shutdown():
    """Clear the agent report, give the tab its name back, and pop the window title.

    Quit, Ctrl-C, and a signal all come through here. A second call does nothing,
    so the title stack is popped once.
    """
    if EXITED["done"]:
        return
    EXITED["done"] = True
    global APP
    try:
        emit("clear", "")
    except (OSError, ValueError):
        pass
    APP = "gbot-tui"
    try:
        TAB.restore()
        # A rename still talking to herdr-app has to finish before this restore.
        TAB.flush(10)
    except (OSError, RuntimeError):
        pass
    pop_session_title()


def _stop(signum, _frame):
    shutdown()
    raise SystemExit(128 + signum)


def enable_ctrl_c():
    """Herdr launches programs with SIGINT ignored, so Ctrl-C does not quit."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (OSError, ValueError):
            pass


def accept_ctrl_c(fd=None):
    """Also deliver Ctrl-C as a key, the same quit as Ctrl-D."""
    enable_ctrl_c()
    try:
        fd = sys.stdin.fileno() if fd is None else fd
        attr = termios.tcgetattr(fd)
        attr[3] &= ~termios.ISIG
        termios.tcsetattr(fd, termios.TCSANOW, attr)
    except (termios.error, OSError, ValueError, AttributeError):
        return False
    return True


def session(target):
    push_session_title()
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
        shutdown()


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
    if action == "overview":
        return print_overview()
    if action == "complete":
        return complete_names()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("gbot-tui needs a terminal.", file=sys.stderr)
        return 1
    enable_ctrl_c()
    atexit.register(shutdown)
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
