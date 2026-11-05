"""Reporting to Herdr: agent state, tab names, icons."""

import base64
import os
import subprocess
import threading
import time

from . import terminal


SOURCE = "gbot-tui"
APP = "gbot-tui"


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


def emit(state, title, kind=None):
    terminal.write_tty(osc_report(state, title, kind).encode())


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
