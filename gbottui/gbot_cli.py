"""Every call to the gbot CLI."""

import json
import os
import subprocess
import tempfile
import threading

from . import backend, transcript


# The gateway widget call is optional. The answer is a normal message unless
# this is 1/true/yes/on, and a failed hook still falls back to that message.
WIDGET_HOOK_ENV = "GBOT_TUI_WIDGET_HOOK"


class GbotError(Exception):
    pass


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
    # A stale shell token beats the app's live login inside gbot. Drop it so
    # gbot reads the token the desktop app keeps refreshed.
    if backend.token_expired(env.get("CURSOR_ACCESS_TOKEN", "")):
        env.pop("CURSOR_ACCESS_TOKEN")
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


def read_thread(target, after=None, limit=80, timeout=30):
    args = ["thread", "--limit", str(limit)]
    if after:
        args += ["--after", after]
    args.append(target)
    return transcript.parse_thread(gbot(*args, timeout=timeout))


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
