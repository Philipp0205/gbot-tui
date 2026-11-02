"""Herdr program icons: each bot's avatar, tinted from the Grok Bot AppImage."""

import io
import json
import os
import struct
import subprocess
import threading

from . import herdr
from .cache import cache_dir
from .colors import avatar_color, bot_avatar, hex_rgb
from .gbot import grok_appimage

AVATAR_LOCK = threading.Lock()


def bot_color(bot):
    """A bot row's avatar color name. The list says avatarColor, a thread page avatar_color."""
    return bot.get("avatarColor") or bot.get("avatar_color") or ""


def bot_shape(bot):
    return bot.get("avatarShape") or bot.get("avatar_shape") or ""


def program_ids(bot_id):
    """The OSC key, then the full gbot-<id> when programs.json already has both."""
    safe = herdr.safe_id(bot_id)
    full = f"gbot-{safe}" if safe else ""
    return list(dict.fromkeys(key for key in (herdr.program_key(bot_id), full) if key))


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
    rgb = hex_rgb(color)
    painted = Image.new("RGBA", frame.size)
    src = frame.load()
    dst = painted.load()
    for y in range(frame.height):
        for x in range(frame.width):
            dst[x, y] = (*rgb, src[x, y][3])
    out = io.BytesIO()
    painted.save(out, format="PNG")
    return out.getvalue()


def avatar_src():
    cache = os.path.join(cache_dir(), "avatar-src")
    if os.path.exists(os.path.join(cache, "bot-heart.png")):
        return cache
    if extract_avatar_sources(cache):
        return cache
    return None


def extract_avatar_sources(cache):
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
        with open(os.path.join(mount, "resources", "app.asar"), "rb") as handle:
            data = handle.read()
        header_size = struct.unpack_from("<I", data, 4)[0]
        header = json.loads(data[16 : 16 + header_size].split(b"\0", 1)[0])
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
                    with open(os.path.join(cache, name), "wb") as out:
                        out.write(blob)

        walk(header)
    except (OSError, ValueError, KeyError):
        return False
    finally:
        for unmount in (["fusermount3", "-u", mount], ["fusermount", "-u", mount]):
            done = subprocess.run(unmount, capture_output=True, text=True)
            if done.returncode == 0:
                break
    return os.path.exists(os.path.join(cache, "bot-heart.png"))


def read_programs(path):
    """programs.json as a dict with a `programs` table, or None when it cannot be read."""
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                table = json.loads(handle.read())
        else:
            table = {}
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(table, dict):
        return None
    if not isinstance(table.get("programs"), dict):
        table["programs"] = {}
    return table


def write_if_changed(dest, data):
    try:
        with open(dest, "rb") as handle:
            if handle.read() == data:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as handle:
        handle.write(data)


def install_avatars(bots, root=None, src=None):
    """Write one Herdr program icon per bot. Herdr matches it from the 7501 app name."""
    root = root or herdr_root()
    src = src or avatar_src()
    if not src:
        return {}
    path = os.path.join(root, "programs.json")
    table = read_programs(path)
    if table is None:
        return {}
    programs = table["programs"]
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
        try:
            png = compose_avatar(mask, face, avatar_color(color_name))
        except (OSError, ValueError, ImportError):
            continue
        write_if_changed(os.path.join(root, rel), png)
        # Herdr matches this list against the OSC app. The app is the hyphenated
        # name (`Cursor-Cloud-Session`), so that string has to be here.
        human = herdr.notice_name(bot.get("name") or "")

        def aliases(slot):
            return list(dict.fromkeys(item for item in (human, slot) if item))

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
    herdr.bind_app(bot.get("name"), bot.get("id"))


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
        key = herdr.program_key(bot.get("id") or "")
        # A later chat, or the list after Esc, owns APP now. Leave it alone.
        if key in made and herdr.APP in {herdr.DEFAULT_APP, key, herdr.notice_name(name)}:
            herdr.bind_app(name, bot.get("id"))

    return install_avatars_later([bot], done)
