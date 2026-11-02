"""Reports the chat's phase to Herdr's agents list: OSC 7501 and `herdr pane report-agent`."""

import base64
import os
import subprocess
import threading
import time

from . import terminal

SOURCE = "gbot-tui"
DEFAULT_APP = "gbot-tui"
# The OSC 7501 app field. Rebound per chat, so read it as `herdr.APP`.
APP = DEFAULT_APP
APP_LIMIT = 32
# Herdr Desktop 0.1.11 panes set none of HERDR_ENV / HERDR_PANE_ID. They do set these.
DESKTOP_MARKS = ("HC_TERMINAL_ID", "HC_SCOPE", "HC_TERMINAL_SOCKET")


def safe_id(bot_id):
    return "".join(ch for ch in str(bot_id) if ch.isalnum() or ch == "-")


def program_key(bot_id):
    # Herdr throws away an OSC 7501 report whose app is longer than 32 bytes.
    safe = safe_id(bot_id)
    key = f"gbot-{safe}" if safe else ""
    if len(key) <= APP_LIMIT:
        return key
    compact = "".join(ch for ch in safe if ch != "-")
    return ("gbot-" + compact)[:APP_LIMIT]


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


def unbind_app():
    global APP
    APP = DEFAULT_APP


def osc_report(state, title, kind=None):
    encoded = base64.b64encode(title.encode()).decode()
    if len(encoded) > 256:
        encoded = base64.b64encode(title[:60].encode()).decode()
    parts = [f"state={state}", f"app={APP}", f"title={encoded}"]
    if state == "blocked" and kind:
        parts.append(f"kind={kind}")
    body = ":".join(parts)
    return f"\033]7501;{body}\033\\\033]0;{terminal.printable(title)}\033\\"


def emit(state, title, kind=None):
    terminal.write_tty(osc_report(state, title, kind).encode())


def resume_arg(text):
    if not text or len(text) > 200:
        return None
    if any(ord(ch) < 32 or ch in "'\\" for ch in text):
        return None
    return text


def herdr_state(phase):
    return phase if phase in {"idle", "working", "blocked"} else "idle"


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


def sequence():
    # A millisecond clock, so a late report does not overwrite a newer one.
    return time.time_ns() // 1_000_000


def tell_herdr(phase, label, session_id, resume):
    herdr, pane = herdr_cli()
    if not herdr or not pane:
        return
    command = report_command(herdr, pane, phase, label, session_id, sequence(), resume)

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
    subprocess.run(
        release_command(herdr, pane, label, sequence()),
        capture_output=True,
        text=True,
        timeout=2,
    )


def publish(phase, title, session_id, resume):
    emit(phase, title, "permission" if phase == "blocked" else None)
    try:
        tell_herdr(phase, title, session_id, resume)
    except (OSError, subprocess.TimeoutExpired):
        pass
