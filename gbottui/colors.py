"""Terminal colors and contrast."""



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


def is_light(hex_color):
    return contrast(hex_color, "#000000") > contrast(hex_color, "#FFFFFF")


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


def bubble_fg(bubble):
    """Bubble text set outright, so a wrong theme guess cannot give dark on dark."""
    return 234 if is_light(xterm_hex(bubble)) else 255


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
