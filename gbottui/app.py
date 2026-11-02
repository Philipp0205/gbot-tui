"""Command line and session: the list and the chat in one curses session, and a clean exit."""

import atexit
import curses
import functools
import signal
import subprocess
import sys
import termios
import threading

from . import activity, cache, gbot, herdr, roster, tabs, terminal, theme
from .chat import Chat
from .chat_screen import chat_loop
from .gbot import GbotError
from .icons import show_avatar_later
from .picker import pick

# The window title while the bot list is up.
LIST_TITLE = "gbot"
USAGE = """\
gbot-tui [name]
gbot-tui --list
gbot-tui --overview

Open a chat with one Grok bot. A name opens that bot; a unique prefix works too.
With no name, pick from the list. The list shows which bots need you, which
are running, and which have a new reply. --list prints every name.
--overview prints that status and the latest line, then exits.
Run it inside a Herdr tab and that bot shows up in the agents list.
"""
TITLE = {"pushed": False, "popped": False}
EXITED = {"done": False}


def start_chat(target):
    """A chat with the last thread already on screen. The real read is the loop's first job."""
    bot = roster.ROSTER.get(target)
    if bot:
        herdr.bind_app(bot.get("name") or target, bot.get("id"))
        show_avatar_later(bot)
    else:
        herdr.bind_app(target)
    chat = Chat(target)
    chat.show_cached(cache.read_thread_cache(target))
    herdr.publish("idle", target, "", chat.resume)
    return chat


def leave_chat(chat, final):
    # Clear the bot under its own app name, then report the list as gbot-tui.
    # Herdr keeps the last app until a new report arrives.
    herdr.emit("clear", chat.title)
    herdr.unbind_app()
    tabs.TAB.restore()
    if not final:
        herdr.emit("idle", LIST_TITLE)
        terminal.window_title(LIST_TITLE)

    def release():
        try:
            herdr.silence_herdr(chat.title)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if final:
        release()
    else:
        # Back to the list should not wait on the herdr CLI.
        threading.Thread(target=release, daemon=True).start()


def screens(stdscr, target):
    """The list and the chat share one curses session, so Esc and Enter do not flash the shell."""
    accept_ctrl_c()
    first = True
    while True:
        if not target:
            cached = list(roster.ROSTER.values()) or cache.read_roster()
            roster.seed_roster(cached)
            fetch = roster.list_bots
            refresh = fetch if first or not cached else functools.partial(fetch, avatars="skip")
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


def print_bots():
    try:
        bots = roster.list_bots(avatars="now")
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
        bots = roster.list_bots(avatars="skip")
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    if not bots:
        print("No bots.", file=sys.stderr)
        return 1
    views, error = activity.build_overview(
        bots,
        lambda bot: gbot.read_thread(bot["name"], limit=activity.OVERVIEW_LIMIT, timeout=20),
    )
    if error:
        print(error, file=sys.stderr)
        return 1
    print(activity.format_overview(views))
    return 0


def complete_names():
    names = cache.read_names(cache.NAMES_TTL)
    if not names:
        try:
            names = [bot["name"] for bot in roster.list_bots(avatars="skip")]
        except GbotError:
            names = cache.read_names()
    for name in names:
        print(name)
    return 0 if names else 1


def push_session_title():
    if TITLE["pushed"]:
        return
    terminal.write_tty(terminal.PUSH_TITLE)
    TITLE["pushed"] = True
    TITLE["popped"] = False


def pop_session_title():
    if not TITLE["pushed"] or TITLE["popped"]:
        return
    TITLE["popped"] = True
    terminal.write_tty(terminal.POP_TITLE)


def shutdown():
    """Clear the agent report, give the tab its name back, and pop the window title.

    Quit, Ctrl-C, and a signal all come through here. A second call does nothing,
    so the title stack is popped once.
    """
    if EXITED["done"]:
        return
    EXITED["done"] = True
    try:
        herdr.emit("clear", "")
    except (OSError, ValueError):
        pass
    herdr.unbind_app()
    try:
        tabs.TAB.restore()
        # A rename still talking to herdr-app has to finish before this restore.
        tabs.TAB.flush(10)
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
        terminal.window_title(LIST_TITLE)
    try:
        return curses.wrapper(lambda stdscr: screens(stdscr, target))
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        shutdown()


def open_named(query):
    try:
        chosen = roster.resolve_for_open(query)
    except GbotError as error:
        print(error, file=sys.stderr)
        return 1
    return session(chosen)


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
    theme.detect_theme()
    try:
        return open_named(target) if target else session("")
    except KeyboardInterrupt:
        return 130
