"""The `gbot` CLI (grok-bot-cli). Every gateway call is one of its processes."""

import json
import os
import subprocess
import threading

from .transcript import parse_thread


class GbotError(Exception):
    pass


def error_message(payload):
    err = payload["error"]
    return err.get("message") if isinstance(err, dict) else str(err)


def parse_gbot(stdout):
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise GbotError(stdout.strip() or "gbot returned no JSON") from error
    if isinstance(payload, dict) and "error" in payload and "transcript" not in payload and "target" not in payload:
        raise GbotError(error_message(payload) or "gbot failed")
    return payload


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
            message = error_message(payload)
            if message:
                return message
        return text
    return "gbot failed"


# One gbot process at a time, so a list scan cannot pass a send or a chat read.
GBOT_LOCK = threading.Lock()


def gbot(*args, timeout=60):
    env = os.environ.copy()
    # gbot 0.12.4 tells Cursor it is Grok Bot 0.20.0, which the server rejects.
    env.setdefault("SAND_CLIENT_VERSION", sand_client_version())
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


def grok_appimage():
    link = os.path.expanduser("~/.local/share/grok-bot/appimage")
    if os.path.exists(link):
        return link
    folder = os.path.expanduser("~/Applications")
    if not os.path.isdir(folder):
        return None
    names = sorted(name for name in os.listdir(folder) if name.startswith("Grok_Bot_") and name.endswith(".AppImage"))
    return os.path.join(folder, names[-1]) if names else None


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
