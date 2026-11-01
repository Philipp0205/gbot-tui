#!/usr/bin/env python3
"""Chat with one Grok Bot. Inside Herdr, the pane shows up in the agents list."""

import base64
import curses
import json
import locale
import os
import subprocess
import sys
import time

SOURCE = "gbot-tui"
APP = "gbot-tui"
# shortcut: two empty polls after a reply count as finished, so a bot that
# pauses longer than that looks done while it is still working.
QUIET_POLLS = 2

locale.setlocale(locale.LC_ALL, "")


class GbotError(Exception):
    pass


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


def pending_approval(entries):
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "send-message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        card = message.get("approval") if kind == "auto-review-approval" else None
        if kind == "local-tool-permission":
            card = message.get("ask")
        if isinstance(card, dict) and card.get("status") == "pending":
            return True
    return False


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
    return {
        "entries": [entry for entry in entries if isinstance(entry, dict)],
        "cursor": cursor,
        "gap_reset": bool(payload.get("gapReset")) if isinstance(payload, dict) else False,
        "name": target.get("name") or "",
        "id": target.get("id") or "",
        "avatar_shape": target.get("avatarShape") or "",
        "avatar_color": target.get("avatarColor") or "",
    }


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
        label,
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
        label,
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


def gbot(*args, timeout=60):
    env = os.environ.copy()
    # gbot 0.12.4 tells Cursor it is Grok Bot 0.20.0, which the server rejects.
    env.setdefault("SAND_CLIENT_VERSION", sand_client_version())
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


def avatar_color(name):
    return AVATAR_COLORS.get((name or "").lower(), "#777777")


def program_key(bot_id):
    safe = "".join(ch for ch in str(bot_id) if ch.isalnum() or ch == "-")
    return f"gbot-{safe}" if safe else ""


def list_bots():
    payload = gbot("bots", "list", timeout=30)
    rows = payload if isinstance(payload, list) else []
    bots = []
    for row in rows:
        if isinstance(row, dict) and row.get("name") and row.get("kind", "bot") != "group":
            bots.append(row)
    ROSTER.clear()
    ROSTER.update({bot["name"]: bot for bot in bots})
    write_names([bot["name"] for bot in bots])
    try:
        install_avatars(bots)
    except OSError:
        pass
    return bots


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


def compose_avatar(mask_path, face_path, color):
    """Tint the shape mask and lay the black face on it. Both files come from the Grok Bot app."""
    from PIL import Image

    mask = Image.open(mask_path).convert("RGBA")
    face = Image.open(face_path).convert("RGBA")
    face = face.crop((0, 0, min(18, face.width), min(18, face.height)))
    size = 32
    resample = getattr(getattr(Image, "Resampling", Image), "BOX")
    mask = mask.resize((size, size), resample)
    rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
    painted = Image.new("RGBA", (size, size))
    src = mask.load()
    dst = painted.load()
    for y in range(size):
        for x in range(size):
            dst[x, y] = (*rgb, src[x, y][3])
    painted.alpha_composite(face.resize((size, size), resample))
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
        key = program_key(bot.get("id") or "")
        shape = "".join(ch for ch in str(bot.get("avatarShape") or bot.get("avatar_shape") or "") if ch.isalnum())
        color_name = "".join(ch for ch in str(bot.get("avatarColor") or bot.get("avatar_color") or "") if ch.isalnum())
        if not key or not shape:
            continue
        mask = os.path.join(src, f"bot-{shape}.png")
        face = os.path.join(src, f"bot-{shape}-face.png")
        if not (os.path.exists(mask) and os.path.exists(face)):
            continue
        rel = f"icons/gbot/{key}-{shape}-{color_name}.png"
        dest = os.path.join(root, rel)
        if not os.path.exists(dest):
            try:
                png = compose_avatar(mask, face, avatar_color(color_name))
            except (OSError, ValueError):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(png)
        entry = {
            "name": bot.get("name") or key,
            "icon": rel,
            "tint": False,
            "tile": "#111111",
            "color": avatar_color(color_name),
            "match": [key],
        }
        made[key] = entry
        if programs.get(key) != entry:
            programs[key] = entry
            changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(table, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    return made


def show_avatar(bot, root=None, src=None):
    global APP
    made = install_avatars([bot], root, src)
    key = program_key(bot.get("id") or "")
    if key in made:
        APP = key


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
    if argv == ["--complete"]:
        return "complete", ""
    return "open", " ".join(argv).strip()


def read_thread(target, after=None):
    args = ["thread", "--limit", "80"]
    if after:
        args += ["--after", after]
    args.append(target)
    return parse_thread(gbot(*args, timeout=30))


def send_message(target, text):
    gbot("send", "--", target, text, timeout=90)


def ancestor_pids():
    pids = set()
    pid = os.getpid()
    while pid and pid not in pids and len(pids) < 12:
        pids.add(pid)
        try:
            text = open(f"/proc/{pid}/status", encoding="utf-8").read()
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
    tab = (info.get("labels") or {}).get("herdr.tab")
    if not tab or not name:
        return
    subprocess.run(
        ["herdr-app", "do", "tab.rename", "--id", tab, "--name", name],
        capture_output=True,
        text=True,
        timeout=3,
    )


def emit(state, title, kind=None):
    try:
        tty = os.open("/dev/tty", os.O_WRONLY)
    except OSError:
        tty = None
    data = osc_report(state, title, kind).encode()
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


def tell_herdr(phase, label, session_id, resume):
    herdr = os.environ.get("HERDR_BIN_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not herdr or not pane:
        return
    seq = time.time_ns() // 1_000_000
    subprocess.run(
        report_command(herdr, pane, phase, label, session_id, seq, resume),
        capture_output=True,
        text=True,
        timeout=2,
    )


def silence_herdr(label):
    herdr = os.environ.get("HERDR_BIN_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not herdr or not pane:
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
    width = max(width, 1)
    lines = []
    for raw in text.splitlines() or [""]:
        while len(raw) > width:
            lines.append(raw[:width])
            raw = raw[width:]
        lines.append(raw)
    return lines or [""]


class Chat:
    def __init__(self, target):
        self.target = target
        self.title = target
        self.session_id = ""
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

    def note(self, phase):
        if phase != self.phase:
            self.phase = phase
            publish(phase, self.title, self.session_id, self.resume)

    def apply(self, page, replace):
        if page["name"]:
            self.title = page["name"]
            self.resume = resume_arg(page["name"]) or resume_arg(page["id"]) or self.resume
        if page["id"]:
            self.session_id = page["id"]
        if replace or page["gap_reset"]:
            self.messages = page["entries"]
            self.cursor = page["cursor"]
            return []
        fresh = page["entries"]
        if fresh:
            self.messages.extend(fresh)
        if page["cursor"]:
            self.cursor = page["cursor"]
        return fresh

    def load(self):
        page = read_thread(self.target)
        self.apply(page, replace=True)
        show_avatar(page)
        phase = "blocked" if pending_approval(self.messages) else "idle"
        self.phase = phase
        publish(phase, self.title, self.session_id, self.resume)
        info, alone = own_terminal()
        if info and alone:
            try:
                rename_tab(info, self.title)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def poll(self):
        if not self.cursor:
            page = read_thread(self.target)
            self.apply(page, replace=True)
            fresh = []
        else:
            page = read_thread(self.target, self.cursor)
            if page["gap_reset"]:
                self.apply(page, replace=True)
                fresh = []
                if self.phase == "working" and any(
                    not is_user(entry) and entry_text(entry).strip() for entry in self.messages
                ):
                    self.saw_other = True
            else:
                fresh = self.apply(page, replace=False)
        phase, self.quiet, self.saw_other = watch_step(
            self.phase,
            fresh,
            self.quiet,
            self.saw_other,
            pending_approval(self.messages),
        )
        self.note(phase)

    def send(self):
        text = self.typed.strip()
        if not text:
            return
        self.typed = ""
        self.error = ""
        self.saw_other = False
        self.quiet = 0
        self.note("working")
        send_message(self.target, text)
        self.poll()


def dark_terminal():
    # COLORFGBG is "fg;bg". 7 and 15 are the light backgrounds.
    raw = os.environ.get("COLORFGBG", "")
    bg = raw.split(";")[-1] if raw else ""
    if bg.isdigit():
        return int(bg) not in {7, 15}
    return True


def user_bubble():
    # Cursor's fallback user-message colors: ansi 235 on a dark terminal, 254 on a light one.
    return 235 if dark_terminal() else 254


def layout(height):
    if height >= 10:
        header = 2
    elif height >= 5:
        header = 1
    else:
        header = 0
    if height >= 7:
        composer, footer = 3, 1
    elif height >= 3:
        composer, footer = 1, 0
    else:
        composer, footer = 0, 0
    body = max(height - header - composer - footer, 0)
    return header, body, composer, footer


def transcript_rows(entries, width):
    rows = []
    user_w = max(width - 8, 1)
    text_w = max(width - 2, 1)
    for entry in entries:
        text = entry_text(entry).strip()
        if not text:
            continue
        user = is_user(entry)
        wrapped = wrap(text, user_w if user else text_w)
        if rows:
            rows.append(("gap", ""))
        kind = "user" if user else "text"
        for line in wrapped:
            rows.append((kind, line))
    return rows


def composer_lines(width, typed):
    span = max(width - 1, 0)
    if span <= 0:
        return "", "", ""
    if span == 1:
        return "┌", "│", "└"
    rule = "─" * (span - 2)
    shown = typed if typed else "Add a follow-up"
    body = f" → {shown}"[: span - 2].ljust(span - 2)
    return "┌" + rule + "┐", "│" + body + "│", "└" + rule + "┘"


def footer_spans(phase, error):
    if error:
        return [(error, "red")]
    if phase == "blocked":
        return [("Pending decision", "yellow")]
    if phase == "working":
        return [("Generating", "bold"), ("  —  pgup scroll", "dim")]
    return [("enter send · ctrl-c quit · pgup scroll", "dim")]


# -1 keeps the terminal background. A filled curses color is the block this CLI does not use.
ATTR = {
    "default": 0,
    "dim": curses.A_DIM,
    "bold": curses.A_BOLD,
    "user": curses.A_BOLD,
    "yellow": curses.A_BOLD,
    "red": curses.A_BOLD,
}


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
            curses.init_pair(2, -1, bubble)
            ATTR["user"] = curses.color_pair(2)
        else:
            ATTR["user"] = ATTR["bold"]
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        ATTR["yellow"] = curses.color_pair(3)
        ATTR["red"] = curses.color_pair(4)
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


def paint_spans(stdscr, y, x, spans):
    col = x
    for text, style in spans:
        put(stdscr, y, col, text, ATTR.get(style, 0))
        col += len(text)


def paint_composer(stdscr, y, width, typed):
    span = max(width - 1, 0)
    if span < 2:
        return
    inner = span - 2
    put(stdscr, y, 0, "│", ATTR["default"])
    arrow = " → "
    put(stdscr, y, 1, arrow[:inner], ATTR["default"] if typed else ATTR["dim"])
    rest = max(inner - len(arrow), 0)
    if typed:
        put(stdscr, y, 1 + min(len(arrow), inner), typed[:rest], ATTR["default"])
    else:
        put(stdscr, y, 1 + min(len(arrow), inner), "Add a follow-up"[:rest], ATTR["dim"])
    put(stdscr, y, span - 1, "│", ATTR["default"])


def draw(stdscr, chat):
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    header, body_h, composer_h, footer_h = layout(height)
    if header:
        put(stdscr, 0, 1, "│", ATTR["default"])
        put(stdscr, 0, 3, clip(chat.title, max(width - 4, 0)), ATTR["default"])
    rows = transcript_rows(chat.messages, width)
    if chat.follow:
        chat.scroll = max(len(rows) - body_h, 0)
    start = min(chat.scroll, max(len(rows) - body_h, 0))
    bubble_w = max(width - 2, 0)
    for row in range(body_h):
        index = start + row
        if index >= len(rows):
            continue
        kind, text = rows[index]
        y = header + row
        if kind == "user":
            put(stdscr, y, 1, (" " + text).ljust(bubble_w)[:bubble_w], ATTR["user"])
        elif kind == "text":
            put(stdscr, y, 1, text, ATTR["default"])
    composer_at = header + body_h
    if composer_h >= 3:
        top, _mid, bot = composer_lines(width, chat.typed)
        put(stdscr, composer_at, 0, top, ATTR["default"])
        paint_composer(stdscr, composer_at + 1, width, chat.typed)
        put(stdscr, composer_at + 2, 0, bot, ATTR["default"])
        try:
            stdscr.move(composer_at + 1, min(4 + len(chat.typed), max(width - 3, 0)))
        except curses.error:
            pass
    elif composer_h == 1:
        shown = chat.typed or "Add a follow-up"
        put(
            stdscr,
            composer_at,
            0,
            clip("→ " + shown, max(width - 1, 0)),
            ATTR["default"] if chat.typed else ATTR["dim"],
        )
    if footer_h:
        paint_spans(stdscr, height - 1, 1, footer_spans(chat.phase, chat.error))
    stdscr.refresh()


def place_cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def chat_loop(stdscr, chat):
    apply_theme(stdscr)
    place_cursor(True)
    stdscr.keypad(True)
    stdscr.timeout(2000)
    chat.load()
    while True:
        draw(stdscr, chat)
        try:
            key = stdscr.get_wch()
        except curses.error:
            try:
                chat.poll()
            except GbotError as error:
                chat.error = str(error)
            continue
        except KeyboardInterrupt:
            return
        if key in (curses.KEY_RESIZE,):
            continue
        if key in ("\x03", "\x04"):
            return
        if key == curses.KEY_PPAGE:
            chat.follow = False
            chat.scroll = max(chat.scroll - max(curses.LINES - 4, 1), 0)
            continue
        if key == curses.KEY_NPAGE:
            chat.scroll += max(curses.LINES - 4, 1)
            height, width = stdscr.getmaxyx()
            _header, body_h, _composer, _footer = layout(height)
            rows = transcript_rows(chat.messages, width)
            if chat.scroll >= max(len(rows) - body_h, 0):
                chat.follow = True
            continue
        if key in ("\n", "\r", curses.KEY_ENTER):
            try:
                chat.send()
            except GbotError as error:
                chat.error = str(error)
                chat.note("idle")
            chat.follow = True
            continue
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            chat.typed = chat.typed[:-1]
            continue
        if isinstance(key, str) and key.isprintable():
            chat.typed += key


def pick(stdscr, bots):
    apply_theme(stdscr)
    place_cursor(False)
    stdscr.keypad(True)
    selected = 0
    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        put(stdscr, 0, 1, "│", ATTR["default"])
        put(stdscr, 0, 3, "Grok bots", ATTR["default"])
        footer = 1 if height >= 4 else 0
        body_h = max(height - 2 - footer, 0)
        top = 0
        if body_h and selected >= body_h:
            top = selected - body_h + 1
        for index, bot in enumerate(bots[top : top + body_h]):
            actual = top + index
            title = bot.get("title") or ""
            name = bot["name"] + (f"  {title}" if title else "")
            if actual == selected:
                put(stdscr, 2 + index, 1, clip("→ " + name, max(width - 2, 0)), ATTR["bold"])
            else:
                put(stdscr, 2 + index, 1, clip("  " + name, max(width - 2, 0)), ATTR["dim"])
        if footer:
            paint_spans(stdscr, height - 1, 1, [("enter open · ctrl-c quit", "dim")])
        stdscr.refresh()
        key = stdscr.get_wch()
        if key in ("\x03", "\x04", "q"):
            return None
        if key in (curses.KEY_UP, "k") and selected > 0:
            selected -= 1
        elif key in (curses.KEY_DOWN, "j") and selected < len(bots) - 1:
            selected += 1
        elif key in ("\n", "\r", curses.KEY_ENTER):
            return bots[selected]["name"]


def open_chat(target):
    bot = ROSTER.get(target)
    if bot:
        show_avatar(bot)
    chat = Chat(target)
    publish("idle", target, "", chat.resume)
    try:
        curses.wrapper(lambda stdscr: chat_loop(stdscr, chat))
    finally:
        emit("clear", chat.title)
        try:
            silence_herdr(chat.title)
        except (OSError, subprocess.TimeoutExpired):
            pass


USAGE = """\
gbot-tui [name]
gbot-tui --list

Open a chat with one Grok bot. A name opens that bot; a unique prefix works too.
With no name, pick from the list. --list prints every bot.
Run it inside a Herdr tab and that bot shows up in the agents list.
"""


def print_bots():
    try:
        bots = list_bots()
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    if not bots:
        print("No bots.", file=sys.stderr)
        return 1
    for bot in bots:
        print(bot["name"])
    return 0


def complete_names():
    names = read_names(NAMES_TTL)
    if not names:
        try:
            names = [bot["name"] for bot in list_bots()]
        except GbotError:
            names = read_names()
    for name in names:
        print(name)
    return 0 if names else 1


def open_named(query):
    chosen = query
    cached = read_names()
    if cached:
        try:
            chosen = match_bot(query, cached)
        except GbotError as error:
            if str(error).startswith("Several"):
                print(error, file=sys.stderr)
                return 1
            chosen = query
    try:
        open_chat(chosen)
        return 0
    except GbotError as error:
        if "No bot or group named" not in str(error):
            print(error, file=sys.stderr)
            return 1
    try:
        chosen = match_bot(query, [bot["name"] for bot in list_bots()])
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        open_chat(chosen)
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def main(argv):
    action, target = parse_args(argv)
    if action == "help":
        print(USAGE, end="")
        return 0
    if action == "list":
        return print_bots()
    if action == "complete":
        return complete_names()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("gbot-tui needs a terminal.", file=sys.stderr)
        return 1
    if not target:
        try:
            bots = list_bots()
        except GbotError as error:
            print(error, file=sys.stderr)
            return 1
        if not bots:
            print("No bots.", file=sys.stderr)
            return 1
        try:
            target = curses.wrapper(lambda stdscr: pick(stdscr, bots))
        except KeyboardInterrupt:
            return 130
        if not target:
            return 0
        try:
            open_chat(target)
        except GbotError as error:
            print(error, file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
        return 0
    try:
        return open_named(target)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
