#!/usr/bin/env python3
"""End-to-end run of gbot-tui in a pseudo-terminal against bench/mock_gbot.py.

    python3 bench/e2e_tui.py [--only name,name] [--keep]

Needs `pyte`. Every run gets a fresh HOME and MOCK_STATE under a temp dir, a PATH
whose only `gbot` is the mock, no Cursor or gateway variables, and
GBOT_TUI_THEME=dark. Nothing reaches a real bot or the network.

Each scenario writes the screen to $GBOT_E2E_ARTIFACTS/<scenario>.txt
(default /workspace/oc/gbot-tui-artifacts/tui-polish) and prints PASS or FAIL.
The exit code is the number of failures.
"""

import argparse
import base64
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
import time

import pyte

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "gbot_tui.py")
ARTIFACTS = os.environ.get("GBOT_E2E_ARTIFACTS") or "/workspace/oc/gbot-tui-artifacts/tui-polish"

PGUP, PGDN, ESC, CTRL_D, CTRL_J = "\x1b[5~", "\x1b[6~", "\x1b", "\x04", "\n"
# curses turns on keypad (application) mode, so arrows and Home/End are SS3, as in terminfo.
LEFT, RIGHT, UP, DOWN, HOME, END, DEL = "\x1bOD", "\x1bOC", "\x1bOA", "\x1bOB", "\x1bOH", "\x1bOF", "\x1b[3~"
CTRL_LEFT, ALT_RIGHT = "\x1b[1;5D", "\x1b[1;3C"
# The same arrows from a terminal not in keypad mode, or a multiplexer: CSI, not SS3.
CSI_LEFT, CSI_RIGHT, CSI_UP, CSI_DOWN, CSI_HOME, CSI_END = "\x1b[D", "\x1b[C", "\x1b[A", "\x1b[B", "\x1b[H", "\x1b[F"
# Variables that could point a child at a real gateway or a Herdr pane.
SCRUB = (
    "CURSOR_ACCESS_TOKEN",
    "GROK_BOT_GATEWAY_URL",
    "GROK_BOT_GATEWAY_TOKEN",
    "GBOT_TUI_WIDGET_HOOK",
    "HERDR_ENV",
    "HERDR_PANE_ID",
    "HERDR_BIN_PATH",
    "HC_TERMINAL_ID",
    "HC_SCOPE",
    "HC_TERMINAL_SOCKET",
    "MOCK_FAIL",
    "MOCK_HANG",
    "MOCK_FAIL_FILE",
    "MOCK_HISTORY",
)


class Term:
    def __init__(self, argv, env, rows=24, cols=80):
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(argv[0], argv, env)
        self._winsize(rows, cols)
        self.alive = True
        self.status = None
        self.raw = bytearray()
        threading.Thread(target=self._read, daemon=True).start()

    def _winsize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _read(self):
        while self.alive:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            if b"\033[c" in data:
                os.write(self.fd, b"\033[?62;22c")
            with self.lock:
                self.raw.extend(data)
                self.stream.feed(data)

    def resize(self, rows, cols):
        with self.lock:
            self.screen.resize(rows, cols)
        self._winsize(rows, cols)
        os.kill(self.pid, signal.SIGWINCH)

    def lines(self):
        with self.lock:
            return list(self.screen.display)

    def cursor(self):
        with self.lock:
            return self.screen.cursor.y, self.screen.cursor.x

    def text(self):
        return "\n".join(line.rstrip() for line in self.lines())

    def wait(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate(self.lines()):
                return True
            if self.exited():
                break
            time.sleep(0.02)
        raise AssertionError("screen never matched")

    def settle(self, seconds=0.3):
        time.sleep(seconds)

    def send(self, data):
        os.write(self.fd, data.encode())

    def exited(self, timeout=0.0):
        end = time.monotonic() + timeout
        while True:
            if self.status is None:
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                except ChildProcessError:
                    pid, status = self.pid, 0
                if pid:
                    self.status = status
            if self.status is not None or time.monotonic() >= end:
                return self.status is not None
            time.sleep(0.05)

    def close(self):
        self.alive = False
        if not self.exited():
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except OSError:
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def has(*texts):
    return lambda lines: all(any(text in line for line in lines) for text in texts)


def lacks(*texts):
    return lambda lines: not any(text in line for line in lines for text in texts)


def composer_rows(lines):
    """Text rows inside the composer box."""
    out, inside = [], False
    for line in lines:
        if line.startswith("┌"):
            inside, out = True, []
            continue
        if line.startswith("└"):
            return out
        if inside and line.startswith("│"):
            out.append(line[1:].rstrip().rstrip("│").strip())
    return out


HERDR_PANE = "pane-e2e-1"
HERDR_DESKTOP_ID = "term-e2e-1"


def fake_herdr(bindir, log):
    """A `herdr` that appends its argv as one JSON line to `log` and exits 0."""
    path = os.path.join(bindir, "herdr")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"with open({log!r}, 'a', encoding='utf-8') as out:\n"
            "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
    os.chmod(path, 0o755)
    return path


def environment(home, herdr=None, **mock):
    """`herdr` is None (scrubbed, the default), "cli" (HERDR_* set) or "desktop" (only HC_TERMINAL_ID)."""
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir, exist_ok=True)
    shim = os.path.join(bindir, "gbot")
    with open(shim, "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'mock_gbot.py')} \"$@\"\n")
    os.chmod(shim, 0o755)
    env = {key: value for key, value in os.environ.items() if key not in SCRUB}
    if herdr:
        # On PATH in both cases, so a call the app should not make still shows in the log.
        binary = fake_herdr(bindir, os.path.join(home, "herdr.log"))
        if herdr == "cli":
            env.update(HERDR_ENV="1", HERDR_PANE_ID=HERDR_PANE, HERDR_BIN_PATH=binary)
        else:
            env.update(HC_TERMINAL_ID=HERDR_DESKTOP_ID)
    env.update(
        HOME=home,
        XDG_CONFIG_HOME=os.path.join(home, ".config"),
        XDG_CACHE_HOME=os.path.join(home, ".cache"),
        # Only the mock and the base system: no node, hc, or herdr from ~/.local or /usr/local.
        PATH=bindir + os.pathsep + "/usr/bin:/bin",
        TERM="xterm-256color",
        GBOT_TUI_THEME="dark",
        MOCK_STATE=os.path.join(home, "mock"),
        MOCK_DELAY="0.05",
        MOCK_REPLY="0.4",
        MOCK_STREAM="1.5",
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
    )
    env.update({key: str(value) for key, value in mock.items()})
    return env


class Run:
    """One app process in its own HOME. Scenarios share it and run in order."""

    def __init__(self, *argv, rows=24, cols=80, herdr=None, seen=None, sections=None, **mock):
        self.home = tempfile.mkdtemp(prefix="gbot-e2e-")
        if seen or sections:
            folder = os.path.join(self.home, ".cache", "gbot-tui")
            os.makedirs(folder)
        if seen:
            # The cursors the app saved on an earlier run: a bot whose newest reply is after it is unread.
            with open(os.path.join(folder, "seen.json"), "w", encoding="utf-8") as handle:
                json.dump(seen, handle)
        if sections:
            # The sections the app saved on an earlier run. The e2e has no backend token, so this is all it gets.
            with open(os.path.join(folder, "sections.json"), "w", encoding="utf-8") as handle:
                json.dump(sections, handle)
        self.env = environment(self.home, herdr=herdr, **mock)
        self.term = Term([sys.executable, APP, *argv], self.env, rows, cols)

    def close(self):
        self.term.close()
        shutil.rmtree(self.home, ignore_errors=True)


RESULTS = []


def dump(name, term):
    os.makedirs(ARTIFACTS, exist_ok=True)
    with open(os.path.join(ARTIFACTS, name + ".txt"), "w", encoding="utf-8") as handle:
        handle.write(term.text() + "\n")


def scenario(name, term, check):
    """Run `check(term)`, dump the screen, record the result."""
    error = ""
    try:
        check(term)
    except AssertionError as failure:
        error = str(failure) or "assertion failed"
    except Exception as failure:  # a crash in the harness is a failure too
        error = f"{type(failure).__name__}: {failure}"
    dump(name, term)
    RESULTS.append((name, error))
    print(f"{'PASS' if not error else 'FAIL'}  {name}" + (f"  -- {error}" if error else ""), flush=True)
    return not error


def check_box(lines, cols):
    """The composer box spans the full width with both corners."""
    tops = [line for line in lines if line.startswith("┌")]
    assert tops, "no composer box"
    assert tops[-1].rstrip().endswith("┐") and len(tops[-1].rstrip()) == cols, f"box top is {len(tops[-1].rstrip())} wide, want {cols}"


def main_flow():
    run = Run()
    term = run.term
    ok = True
    try:
        def picker(t):
            t.wait(has("Grok bots", "Researcher", "Travel Agent", "Chef"))
            t.wait(has("type to search"))

        ok &= scenario("01_picker", term, picker)

        def list_help(t):
            t.send("?")
            t.wait(has("Keys  (any key closes)", "scan every bot again"))
            t.wait(lacks("digs into papers"))
            t.send("x")
            t.wait(lacks("Keys  (any key closes)"))
            t.wait(has("digs into papers", "? keys"))
            assert not any("/x" in line for line in t.lines()), "the closing key searched"
            t.send("\x1bOP")  # F1
            t.wait(has("Keys  (any key closes)"))
            dump("28_list_help_open", t)
            t.send(ESC)
            t.wait(has("Researcher", "Travel Agent", "Chef"))
            t.wait(lacks("Keys  (any key closes)"))
            t.send("c?")  # a ? inside a search is a letter, not help
            t.wait(has("/c?"))
            assert not any("Keys  (any key closes)" in line for line in t.lines()), "? in a search opened help"
            t.send("\x7f\x7f")
            t.wait(lacks("/c"))

        ok &= scenario("28_list_help", term, list_help)

        def filter_(t):
            t.send("trav")
            t.wait(has("trav"))
            t.wait(lacks("Researcher", "Chef"))
            assert any("Travel Agent" in line for line in t.lines()), "Travel Agent filtered out"

        ok &= scenario("02_filter", term, filter_)

        def open_bot(t):
            t.send("\x7f" * 4 + "resea")
            t.wait(lacks("Chef"))
            t.send("\r")
            t.wait(has("Researcher", "earlier reply", "Add a follow-up"))
            check_box(t.lines(), 80)

        ok &= scenario("03_open_bot", term, open_bot)

        def mention(t):
            t.settle(0.5)
            t.send("@tra")
            # The list sits above the composer box and names the bot.
            t.wait(lambda lines: any("● Travel Agent" in line for line in lines))
            assert not any("● Researcher" in line for line in t.lines()[2:]), "this chat's own bot was suggested"
            dump("37_mention_list_open", t)
            t.send("\t")
            t.wait(lambda lines: composer_rows(lines) == ["→  @Travel Agent"])
            t.wait(lacks("● Travel Agent"))
            t.send("\x15")  # ctrl-u clears the draft for the next scenario
            t.wait(has("→  Add a follow-up"))

        ok &= scenario("37_mention_autocomplete", term, mention)

        def send_stream(t):
            t.settle(0.5)
            t.send("hello there my friend")
            t.wait(lambda lines: "hello there my friend" in " ".join(composer_rows(lines)))
            t.send("\r")
            t.wait(has("working"))
            # The reply grows over several polls: it must show part way, then whole.
            seen = set()

            def grown(lines):
                for line in lines:
                    if "● reply" in line:
                        seen.add(line.strip())
                return any("reply to hello there my friend" in line for line in lines)

            t.wait(grown, timeout=10)
            assert len(seen) >= 2, f"reply never streamed, only saw {sorted(seen)}"
            t.wait(lacks("working"), timeout=12)

        ok &= scenario("04_send_streamed_reply", term, send_stream)

        def long_output(t):
            t.send("long answer please")
            t.send("\r")
            t.wait(has("END OF LONG"), timeout=15)
            text = t.text()
            assert "```" not in text, "code fence drawn as raw ```"
            for line in t.lines():
                assert len(line.rstrip()) <= 80, "line wider than the screen"

        ok &= scenario("05_long_output", term, long_output)

        def scroll(t):
            t.wait(lacks("Generating"), timeout=12)
            t.send(PGUP)
            t.wait(lacks("END OF LONG"))
            t.send(PGUP)
            t.wait(has("Trip plan"))
            t.send(PGDN + PGDN + PGDN)
            t.wait(has("END OF LONG"))

        ok &= scenario("06_scroll_pgup_pgdn", term, scroll)

        def multiline(t):
            t.send("first line")
            t.send(CTRL_J)
            t.send("second line")
            t.wait(lambda lines: composer_rows(lines)[-2:] == ["→  first line", "second line"])
            t.send("\r")
            t.wait(has("reply to first line"), timeout=10)
            assert composer_rows(t.lines()) == ["→  Add a follow-up"], composer_rows(t.lines())

        ok &= scenario("07_multiline_input", term, multiline)

        def approval(t):
            t.wait(lacks("Generating", "working"), timeout=12)
            t.send("approve the cleanup")
            t.send("\r")
            t.wait(has("Needs approval", "rm -rf build"), timeout=10)
            t.wait(has("y approve"))
            t.send("y")
            t.wait(has("Approved"), timeout=10)
            t.wait(lacks("y approve"))

        ok &= scenario("08_approval", term, approval)

        def widget(t):
            t.wait(lacks("Generating"), timeout=12)
            t.send("widget time")
            t.send("\r")
            t.wait(has("Pick a city", "Paris"), timeout=10)
            t.wait(has("space then digit"), timeout=5)
            t.send("2")
            t.wait(has("reply to Paris"), timeout=10)

        ok &= scenario("09_widget_choice", term, widget)

        def resize(t):
            t.resize(30, 100)
            t.wait(lambda lines: any(line.startswith("┌") and line.rstrip().endswith("┐") and len(line.rstrip()) == 100 for line in lines))
            check_box(t.lines(), 100)
            t.resize(14, 44)
            t.wait(lambda lines: any(line.startswith("┌") and len(line.rstrip()) == 44 for line in lines))
            t.settle(0.3)
            dump("10_resize_narrow", t)
            check_box(t.lines(), 44)
            for line in t.lines():
                assert len(line.rstrip()) <= 44
            t.resize(24, 80)
            t.wait(lambda lines: any(line.startswith("┌") and len(line.rstrip()) == 80 for line in lines))
            t.settle(0.3)
            check_box(t.lines(), 80)
            assert not t.exited(), "app exited on resize"

        ok &= scenario("10_resize", term, resize)

        def back(t):
            t.send(ESC)
            t.wait(has("Grok bots", "Chef", "type to search"))

        ok &= scenario("11_esc_back_to_list", term, back)

        def switch(t):
            t.send("chef")
            t.wait(lacks("Researcher"))
            t.send("\r")
            t.wait(has("Chef", "what to cook", "earlier reply"))
            assert not any("reply to hello" in line for line in t.lines()), "old chat still on screen"

        ok &= scenario("12_switch_bot", term, switch)

        def quit_(t):
            t.send(CTRL_D)
            assert t.exited(5), "still running 5s after ctrl-d"
            assert os.WIFEXITED(t.status) and os.WEXITSTATUS(t.status) == 0, f"exit status {t.status}"

        ok &= scenario("13_quit_ctrl_d", term, quit_)
    finally:
        run.close()
    return ok


def error_flow():
    run = Run("Chef", MOCK_FAIL="gateway is down")
    try:
        def error(t):
            t.wait(has("gateway is down"), timeout=10)
            check_box(t.lines(), 80)
            t.send("still here")
            t.wait(lambda lines: "still here" in " ".join(composer_rows(lines)))

        return scenario("14_error_state", run.term, error)
    finally:
        run.close()


def offline_flow():
    flag = os.path.join(tempfile.mkdtemp(prefix="gbot-e2e-flag-"), "down")
    with open(flag, "w", encoding="utf-8") as handle:
        handle.write("gateway is down")
    run = Run("Chef", MOCK_FAIL_FILE=flag)
    try:
        def offline(t):
            t.wait(has("offline · retrying in", "gateway is down"), timeout=10)
            # Second failure backs off to 4s; the label counts down to it.
            t.wait(has("offline · retrying in 4s"), timeout=10)
            t.wait(has("offline · retrying in 3s"), timeout=3)
            dump("27_offline_countdown", t)
            check_box(t.lines(), 80)
            os.unlink(flag)
            t.wait(has("reconnected"), timeout=15)
            t.wait(has("earlier reply"))
            assert not any("offline" in line or "gateway is down" in line for line in t.lines()), "offline label stayed"
            dump("27_offline_reconnected", t)
            t.wait(lacks("reconnected"), timeout=8)
            t.wait(has("? keys"))

        return scenario("27_offline_reconnect", run.term, offline)
    finally:
        run.close()
        shutil.rmtree(os.path.dirname(flag), ignore_errors=True)


def bell_flow():
    run = Run("Chef", MOCK_REPLY="2.5", MOCK_STREAM="0")
    try:
        def bell(t):
            notice = b"\x07\x1b]9;Chef needs you\x07"
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.send("approve the cleanup")
            t.wait(lambda lines: "approve the cleanup" in " ".join(composer_rows(lines)))
            t.send("\r")
            t.wait(has("working"))
            t.send(ESC)  # back to the list before the card lands
            t.wait(has("Grok bots", "Researcher"))
            assert notice not in bytes(t.raw), "rang before the card"
            end = time.monotonic() + 20
            while notice not in bytes(t.raw) and time.monotonic() < end:
                t.send("\x12")  # ctrl-r: scan again
                t.settle(1.0)
            assert notice in bytes(t.raw), "no BEL + OSC 9 for needs you"
            t.wait(has("needs you"))
            dump("29_needs_you_bell_list", t)
            for _ in range(3):
                t.send("\x12")
                t.settle(1.0)
            count = bytes(t.raw).count(notice)
            assert count == 1, f"rang {count} times for one transition"

        return scenario("29_needs_you_bell", run.term, bell)
    finally:
        run.close()


def row_with(lines, name):
    return next((line for line in lines if name in line and "Grok bots" not in line), "")


def row_state(lines, name):
    """The state word on a list row: the text between the dot and the name."""
    row = row_with(lines, name)
    return row.split("●", 1)[-1].split(name)[0].strip() if "●" in row else ""


def unread_flow():
    """Open an unread bot and come back: its row and the count line change at once."""
    # The mock thread ends m29 (user), m30 (bot's "earlier reply"). Chef was last seen at m29.
    # The thread names a viewer session, not the list's bot id (MOCK_THREAD_ID).
    run = Run(seen={"id:a3": "m29", "name:Chef": "m29"}, MOCK_REPLY="0.4", MOCK_THREAD_ID="session")
    try:
        def back_to_idle(t):
            t.wait(lambda lines: row_state(lines, "Chef") == "unread", timeout=10)
            t.wait(has("1 unread"))
            dump("38_unread_list_before", t)
            t.send(DOWN * 2)
            t.send("\r")
            t.wait(has("Chef", "earlier reply", "Add a follow-up"))
            t.wait(lacks("Generating", "working"), timeout=10)
            t.send(ESC)
            t.wait(has("Grok bots", "Researcher"))
            # The scan pass takes longer than this: it is the saved state that must be right.
            t.wait(lambda lines: row_state(lines, "Chef") == "idle", timeout=1.5)
            assert "unread" not in row_with(t.lines(), "Chef"), row_with(t.lines(), "Chef")
            assert not any("1 unread" in line for line in t.lines()), "the count line still says unread"

        return scenario("38_unread_clears_on_return", run.term, back_to_idle)
    finally:
        run.close()


def ends_with(text):
    return lambda lines: any(line.rstrip().endswith(text) for line in lines)


def history_flow():
    run = Run("Chef", MOCK_HISTORY="120")
    try:
        def older(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.wait(has("? keys"))
            for _ in range(30):
                t.send(PGUP)
                t.settle(0.15)
                if any("older messages" in line for line in t.lines()):
                    break
            t.wait(has("Loaded 40 older messages."), timeout=10)
            # The rows that were on screen stay put; the new ones sit above.
            assert ends_with("earlier line 41")(t.lines()) or ends_with("earlier line 42")(t.lines()), "view jumped"
            for _ in range(30):
                t.send(PGUP)
                t.settle(0.1)
                if ends_with("earlier line 1")(t.lines()):
                    break
            t.wait(ends_with("earlier line 1"))
            dump("30_older_history_top", t)
            t.send(PGDN * 40)
            t.wait(has("earlier reply"))

        return scenario("30_older_history", run.term, older)
    finally:
        run.close()


def loading_flow():
    run = Run("Researcher", MOCK_HANG="4")
    try:
        def loading(t):
            t.wait(has("Researcher", "Add a follow-up"), timeout=10)
            t.settle(0.5)
            assert not any("earlier reply" in line for line in t.lines()), "thread before the read"
            t.wait(has("Loading"), timeout=3)
            t.wait(has("earlier reply"), timeout=10)
            t.wait(lacks("Loading"))

        return scenario("15_loading_state", run.term, loading)
    finally:
        run.close()


def box_text(lines):
    return "\n".join(composer_rows(lines))


def box_row(t):
    """Screen row of the first text line inside the composer."""
    for index, line in enumerate(t.lines()):
        if line.startswith("┌"):
            return index + 1
    raise AssertionError("no composer box")


def compose_flow():
    run = Run("Chef")
    term = run.term
    ok = True
    try:
        def editing(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.send("helo world")
            t.wait(has("→  helo world"))
            t.send(LEFT * 7 + "l")
            t.wait(has("→  hello world"))
            row = box_row(t)
            assert t.cursor() == (row, 9), f"cursor at {t.cursor()}, want {(row, 9)}"
            t.send("\x01>")  # ctrl-a, then type at the start
            t.send("\x05" + "\x17" + "there")  # ctrl-e, ctrl-w, type
            t.wait(has("→  >hello there"))
            t.send(CTRL_LEFT + CTRL_LEFT + DEL)
            t.wait(has("→  >ello there"))
            t.send(HOME + DEL)
            t.wait(has("→  ello there"))
            t.send(ALT_RIGHT + "\x0b")  # word right, ctrl-k
            t.wait(has("→  ello"))
            t.wait(lacks("there"))
            t.send(HOME + "H" + END + "\x7f" + "o!")
            t.wait(has("→  Hello!"))
            assert t.cursor() == (box_row(t), 11), t.cursor()

        ok &= scenario("16_composer_cursor", term, editing)

        def wrapped(t):
            t.send("\x15")  # ctrl-u clears the line
            t.wait(has("→  Add a follow-up"))
            t.send("first line" + CTRL_J + "second line that is long enough to wrap around the right edge of the composer box")
            t.wait(lambda lines: len(composer_rows(lines)) == 3)
            t.send(UP + UP)
            t.wait(lambda lines: t.cursor()[0] == box_row(t))
            t.send(END + " here")
            t.wait(has("→  first line here"))
            t.send(DOWN + DOWN)
            t.wait(lambda lines: t.cursor()[0] == box_row(t) + 2)
            t.send("\r")
            t.wait(has("reply to first line here"), timeout=10)

        ok &= scenario("17_composer_wrapped", term, wrapped)

        def history(t):
            t.wait(lacks("Generating", "working"), timeout=12)
            t.send("short one")
            t.send("\r")
            t.wait(has("reply to short one"), timeout=10)
            t.wait(has("→  Add a follow-up"))
            t.send(UP)
            t.wait(has("→  short one"))
            t.send(UP)
            t.wait(has("→  first line here"))
            t.send(" edited")
            t.wait(has("edited"))
            t.send(DOWN)
            t.wait(has("→  short one"))
            t.send(UP)
            t.wait(lambda lines: box_text(lines).startswith("→  first line here\n"))
            t.send(DOWN + DOWN)
            t.wait(has("→  Add a follow-up"))

        ok &= scenario("18_input_history", term, history)

        def csi_arrows(t):
            t.send("\x15abc")
            t.wait(has("→  abc"))
            # Each key on its own, so a split read cannot hide a stray Esc.
            for key in (CSI_LEFT, "X", LEFT, "Y", CSI_HOME, "<", CSI_END, ">", CSI_LEFT, CSI_RIGHT, "!"):
                t.send(key)
                t.settle(0.08)
            t.wait(has("→  <abYXc>!"))
            t.send(CSI_HOME + "\x1b[3~" + "\x1b[1;5C" + "_")
            t.wait(has("→  abYXc_>!"))
            assert any("Chef" in line for line in t.lines()[:2]), "left the chat"
            t.send(CSI_END + "\x15")
            t.wait(has("→  Add a follow-up"))
            t.send(ESC)
            t.wait(has("Grok bots", "type to search"))
            # The list: CSI and SS3 Down both move the highlight.
            def marked(lines):
                return [i for i, line in enumerate(lines) if line.startswith(" → ")]

            t.wait(lambda lines: marked(lines) == [2])
            t.send(CSI_DOWN)
            t.wait(lambda lines: marked(lines) == [3])
            t.send(DOWN)
            t.wait(lambda lines: marked(lines) == [4])
            t.send(CSI_UP)
            t.wait(lambda lines: marked(lines) == [3])

        ok &= scenario("19_csi_arrows", term, csi_arrows)
    finally:
        run.close()
    return ok


# (name, word left, word right) as terminals send them. The terminfo of screen and linux has none of the
# modified arrows, so those only work when the app decodes the sequence itself.
WORD_KEYS = (
    ("ctrl", "\x1b[1;5D", "\x1b[1;5C"),
    ("alt", "\x1b[1;3D", "\x1b[1;3C"),
    ("ctrl_alt", "\x1b[1;7D", "\x1b[1;7C"),
    ("meta", "\x1b[1;9D", "\x1b[1;9C"),
    ("csi_5", "\x1b[5D", "\x1b[5C"),
    ("ss3_5", "\x1bO5D", "\x1bO5C"),
    ("rxvt_ctrl", "\x1bOd", "\x1bOc"),
    ("readline_alt", "\x1bb", "\x1bf"),  # alt-b / alt-f, what macOS option-arrow sends
)


def words_flow():
    """Word jumps in the draft with TERM values whose terminfo has no kLFT5 or kRIT5."""
    ok = True
    for term_name in ("xterm-256color", "screen", "linux"):
        run = Run("Chef", TERM=term_name)
        try:
            run.term.wait(has("Chef", "earlier reply"), timeout=10)
            run.term.settle(0.5)
            for name, left, right in WORD_KEYS:

                def jump(t, name=name, left=left, right=right):
                    t.send("\x05\x15alpha beta gamma")
                    t.wait(has("→  alpha beta gamma"))
                    # Each key on its own, so a split read cannot hide a stray Esc.
                    for key in (left, left, "X"):
                        t.send(key)
                        t.settle(0.08)
                    try:
                        t.wait(has("→  alpha Xbeta gamma"), timeout=3)
                    except AssertionError:
                        raise AssertionError(f"{name} left: draft is {composer_rows(t.lines())}, want alpha Xbeta gamma")
                    for key in (right, "Y"):
                        t.send(key)
                        t.settle(0.08)
                    try:
                        t.wait(has("→  alpha XbetaY gamma"), timeout=3)
                    except AssertionError:
                        raise AssertionError(f"{name} right: draft is {composer_rows(t.lines())}, want alpha XbetaY gamma")
                    assert any("Chef" in line for line in t.lines()[:2]), "left the chat"

                ok &= scenario(f"50_words_{term_name}_{name}", run.term, jump)
        finally:
            run.close()
    return ok


SHIFT_LEFT, CTRL_SHIFT_LEFT, ALT_A, CTRL_O, CTRL_X = "\x1b[1;2D", "\x1b[1;6D", "\x1ba", "\x0f", "\x18"


def draft_reads(t, want, what):
    try:
        t.wait(lambda lines: composer_rows(lines) == [want], timeout=3)
    except AssertionError:
        raise AssertionError(f"{what}: draft is {composer_rows(t.lines())}, want {want}")


def selected_is(t, text):
    """The reverse-video cells of some row read `text`: that is the selection."""
    t.wait(lambda lines: text in reversed_text(t), timeout=3)


def press_release(t, start, end):
    """A left press at draft index `start` and a release at `end` (SGR mouse reports, 1-based)."""
    top = next(i for i, line in enumerate(t.lines()) if line.startswith("┌")) + 2
    for index, final in ((start, "M"), (end, "m")):
        t.send(f"\x1b[<0;{6 + index};{top}{final}")
        t.settle(0.1)


def select_flow():
    """Selecting in the draft, with TERM values whose terminfo has no kLFT2, kLFT6 or alt-a."""
    ok = True
    for term_name in ("xterm-256color", "screen"):
        run = Run("Chef", TERM=term_name)
        try:
            run.term.wait(has("Chef", "earlier reply"), timeout=10)
            run.term.settle(0.5)

            def chars(t):
                t.send("\x05\x15alpha beta gamma")
                t.wait(has("→  alpha beta gamma"))
                # Each key on its own, so a split read cannot hide a stray Esc.
                for _ in range(5):
                    t.send(SHIFT_LEFT)
                    t.settle(0.08)
                selected_is(t, "gamma")
                t.send("X")
                draft_reads(t, "→  alpha beta X", "shift-left x5, X")
                assert any("Chef" in line for line in t.lines()[:2]), "left the chat"

            ok &= scenario(f"51_select_chars_{term_name}", run.term, chars)

            def words(t):
                t.send("\x05\x15alpha beta gamma")
                t.wait(has("→  alpha beta gamma"))
                for _ in range(2):
                    t.send(CTRL_SHIFT_LEFT)
                    t.settle(0.08)
                selected_is(t, "beta gamma")
                t.send("\x7f")
                draft_reads(t, "→  alpha", "ctrl+shift-left x2, backspace")
                t.send("Z")
                draft_reads(t, "→  alpha Z", "backspace took more than the selection")

            ok &= scenario(f"51_select_words_{term_name}", run.term, words)

            def everything(t):
                t.send("\x05\x15alpha beta gamma")
                t.wait(has("→  alpha beta gamma"))
                t.send(ALT_A)
                selected_is(t, "alpha beta gamma")
                t.send("Q")
                draft_reads(t, "→  Q", "alt-a, Q")
                assert any("Chef" in line for line in t.lines()[:2]), "alt-a left the chat"

            ok &= scenario(f"51_select_all_{term_name}", run.term, everything)

            def copy_cut_esc(t):
                t.send("\x05\x15alpha beta")
                t.wait(has("→  alpha beta"))
                for _ in range(4):
                    t.send(SHIFT_LEFT)
                    t.settle(0.08)
                selected_is(t, "beta")
                t.send(CTRL_O)
                t.wait(has("Copied the selection (4 chars)."))
                t.send(ESC)
                t.wait(lambda lines: "beta" not in reversed_text(t), timeout=3)
                draft_reads(t, "→  alpha beta", "esc changed the draft")
                assert any("Chef" in line for line in t.lines()[:2]), "the first esc left the chat"
                # The cursor stays where the selection ended, so go back to the end first.
                t.send("\x05")
                for _ in range(4):
                    t.send(SHIFT_LEFT)
                    t.settle(0.08)
                t.send(CTRL_X)
                draft_reads(t, "→  alpha", "ctrl-x")

            ok &= scenario(f"51_select_copy_cut_esc_{term_name}", run.term, copy_cut_esc)

            if term_name == "xterm-256color":

                def mouse(t):
                    t.send("\x05\x15alpha beta gamma")
                    t.wait(has("→  alpha beta gamma"))
                    press_release(t, 6, 10)
                    selected_is(t, "beta")
                    t.send("X")
                    draft_reads(t, "→  alpha X gamma", "mouse press and release over beta")
                    press_release(t, 0, 0)
                    t.send("<")
                    draft_reads(t, "→  <alpha X gamma", "a press places the cursor")

                ok &= scenario(f"51_select_mouse_{term_name}", run.term, mouse)
        finally:
            run.close()
    return ok


def idle_flow():
    """A gateway that returns the --after entry again: header and footer still go idle."""
    run = Run("Chef", MOCK_INCLUSIVE=1)
    term = run.term
    ok = True

    def settles(final):
        def check(t):
            t.wait(has("Chef", "Add a follow-up"), timeout=10)
            t.wait(lacks("working", "Generating"), timeout=12)
            t.send(final[0])
            t.send("\r")
            t.wait(has("working"))
            t.wait(has(final[1]), timeout=20)
            start = time.monotonic()
            t.wait(lacks("working", "Generating"), timeout=6)
            assert "enter send" in t.lines()[-1], "footer is not the idle hint"
            print(f"      idle {time.monotonic() - start:.1f}s after {final[1]!r}", flush=True)

        return check

    try:
        ok &= scenario("20_idle_after_long_reply", term, settles(("long please", "END OF LONG")))
        ok &= scenario("21_idle_after_widget", term, settles(("widget", "Pick a city")))

        def paste(t):
            t.send(" ")  # text first, so the widget does not take digits
            t.send("\x1b[200~paste one\rpaste two\rpaste three\x1b[201~")
            t.wait(lambda lines: composer_rows(lines)[-3:] == ["paste one", "paste two", "paste three"] or composer_rows(lines) == ["→   paste one", "paste two", "paste three"])
            t.settle(1.0)
            assert not any("reply to" in line and "paste" in line for line in t.lines()), "the paste was sent"
            assert "Generating" not in t.lines()[-1], "the paste started a send"
            t.send("\r")
            t.wait(has("reply to paste one"), timeout=10)

        ok &= scenario("22_bracketed_paste", term, paste)

        def key_help(t):
            t.wait(lacks("Generating", "working"), timeout=12)
            t.send("?")
            t.wait(has("Keys  (any key closes)", "back to the bot list"))
            t.send(ESC)
            t.wait(lacks("Keys  (any key closes)"))
            t.wait(has("Chef", "? keys"))
            t.send("\x1bOP")  # F1
            t.wait(has("Keys  (any key closes)"))
            dump("23_key_help_open", t)
            t.send("x")
            t.wait(lacks("Keys  (any key closes)"))
            t.wait(has("→  Add a follow-up"))

        ok &= scenario("23_key_help", term, key_help)
    finally:
        run.close()
    return ok


def reversed_text(t):
    """Text drawn in reverse video, row by row: the search highlight."""
    with t.lock:
        rows = []
        for y in range(t.screen.lines):
            line = t.screen.buffer[y]
            rows.append("".join(line[x].data for x in range(t.screen.columns) if line[x].reverse))
        return rows


def footer_cell(t, text):
    """(row, fg color) of the first row holding `text`, from the pyte cells."""
    with t.lock:
        for y in range(t.screen.lines):
            line = t.screen.buffer[y]
            row = "".join(line[x].data for x in range(t.screen.columns))
            at = row.find(text)
            if at >= 0:
                return y, line[at].fg
    return None, None


def polish_flow():
    run = Run("Chef")
    term = run.term
    ok = True
    try:
        def search(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.wait(lacks("earlier line 5 "))
            t.send("\x06")
            t.wait(has("find: ", "esc close"))
            t.send("earlier line 5")
            t.wait(has("find: earlier line 5", "1/1"))
            t.wait(has("earlier line 5"))
            assert any("earlier line 5" in row for row in reversed_text(t)), "match not highlighted"
            dump("24_search_open", t)
            t.send("\x1b")
            t.wait(lacks("find: "))
            t.wait(has("Chef", "? keys"))
            t.send("\x06line 2")
            t.wait(has("find: line 2"))
            first = [line for line in t.lines() if "/" in line and "find:" in line][0]
            t.send("\x10")  # ctrl-p wraps from the newest to the oldest
            t.wait(lambda lines: any("find:" in line and line != first for line in lines))
            t.send("\r")
            t.wait(lambda lines: any(line == first for line in lines))
            assert not any("reply to" in line for line in t.lines()), "enter sent the search"
            t.send("\x1b")
            t.wait(lacks("find: "))
            assert any("Chef" in line for line in t.lines()[:2]), "left the chat"

        ok &= scenario("24_search", term, search)

        def copy(t):
            t.send("\x0f")
            t.wait(has("Copied the last reply"))
            assert any("Chef" in line for line in t.lines()[:2]), "left the chat"

        ok &= scenario("25_copy_osc52", term, copy)

        def notice(t):
            # The copy line is a notice, not an error: not red, and it goes away.
            t.wait(has("Copied the last reply"))
            row, col = footer_cell(t, "Copied")
            assert row is not None, "no notice row"
            assert col != "red", f"notice drawn in {col}"
            dump("26_copy_notice_shown", t)
            t.wait(lacks("Copied the last reply"), timeout=8)
            t.wait(has("? keys"))
            t.send("\x0f")
            t.wait(has("Copied the last reply"))
            t.send("x")
            t.wait(lacks("Copied the last reply"), timeout=3)
            t.send("\x7f")

        ok &= scenario("26_copy_notice", term, notice)
    finally:
        run.close()
    return ok


OSC_7501 = re.compile(rb"\x1b\]7501;([^\x1b]*)\x1b\\")


def osc_reports(raw):
    """Every OSC 7501 report in the pty bytes, as dicts: state, app, title (decoded), kind."""
    out = []
    for body in OSC_7501.findall(bytes(raw)):
        fields = dict(part.split("=", 1) for part in body.decode().split(":") if "=" in part)
        if "title" in fields:
            fields["title"] = base64.b64decode(fields["title"]).decode()
        out.append(fields)
    return out


def herdr_calls(home):
    """argv (without the program name) of every call the fake herdr logged."""
    try:
        with open(os.path.join(home, "herdr.log"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def expect_osc(t, since, timeout=15, **want):
    """Wait for an OSC 7501 report after index `since` that has the wanted fields. Returns the next index."""
    end = time.monotonic() + timeout
    while True:
        reports = osc_reports(t.raw)
        for index in range(since, len(reports)):
            if all(reports[index].get(key) == value for key, value in want.items()):
                return index + 1
        if time.monotonic() >= end:
            raise AssertionError(f"no OSC 7501 {want} after #{since}; saw {reports[since:]}")
        time.sleep(0.05)


def expect_call(home, since, verb, state=None, timeout=15):
    """Wait for a fake-herdr `pane <verb>` call after index `since`; check pane, source, agent. Returns the next index."""
    end = time.monotonic() + timeout
    while True:
        calls = herdr_calls(home)
        for index in range(since, len(calls)):
            argv = calls[index]
            if argv[:3] == ["pane", verb, HERDR_PANE] and (state is None or flag(argv, "--state") == state):
                assert flag(argv, "--source") == "gbot-tui", argv
                assert flag(argv, "--agent") == "Chef", argv
                assert flag(argv, "--seq").isdigit(), argv
                return index + 1
        if time.monotonic() >= end:
            raise AssertionError(f"no herdr pane {verb} {state or ''} after #{since}; saw {calls[since:]}")
        time.sleep(0.05)


def herdr_flow():
    run = Run("Chef", herdr="cli", MOCK_REPLY="1.0", MOCK_STREAM="0")
    term, home = run.term, run.home
    ok = True
    mark = {"osc": 0, "cli": 0}
    try:
        def send(t, text):
            t.send(text)
            t.wait(lambda lines: text in " ".join(composer_rows(lines)))
            t.send("\r")

        def opened(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            mark["osc"] = expect_osc(t, 0, state="idle", app="Chef", title="Chef")
            mark["cli"] = expect_call(home, 0, "report-agent", "idle")
            calls = herdr_calls(home)
            assert calls[0][:3] == ["pane", "report-agent", HERDR_PANE], calls[0]
            assert all(argv[1] == "report-agent" for argv in calls), calls
            assert calls[0][calls[0].index("--") + 1 :] == ["gbot-tui", "Chef"], calls[0]

        ok &= scenario("31_herdr_open_idle", term, opened)

        def working(t):
            send(t, "hello there")
            mark["osc"] = expect_osc(t, mark["osc"], state="working", app="Chef", title="Chef")
            mark["cli"] = expect_call(home, mark["cli"], "report-agent", "working")
            # The reply lands: the report is idle again (done is reported to Herdr as idle).
            t.wait(has("reply to hello there"), timeout=10)
            mark["cli"] = expect_call(home, mark["cli"], "report-agent", "idle")
            last = osc_reports(t.raw)[-1]
            assert last["state"] in ("idle", "done"), f"after the reply the last OSC state is {last['state']}"

        ok &= scenario("32_herdr_send_working_then_idle", term, working)

        def blocked(t):
            t.wait(lacks("working"), timeout=12)
            send(t, "approve the cleanup")
            mark["osc"] = expect_osc(t, mark["osc"], state="blocked", kind="permission", app="Chef", title="Chef")
            mark["cli"] = expect_call(home, mark["cli"], "report-agent", "blocked")
            t.wait(has("Needs approval", "rm -rf build"), timeout=10)
            t.send("y")
            t.wait(has("Approved"), timeout=10)
            mark["osc"] = expect_osc(t, mark["osc"], state="idle", app="Chef")
            mark["cli"] = expect_call(home, mark["cli"], "report-agent", "idle")

        ok &= scenario("33_herdr_approval_blocked", term, blocked)

        def reply_done(t):
            t.wait(lacks("working", "Generating"), timeout=12)
            before = len(osc_reports(t.raw))
            send(t, "one more")
            mark["osc"] = expect_osc(t, before, state="working", app="Chef")
            t.wait(has("reply to one more"), timeout=10)
            end = time.monotonic() + 15
            while osc_reports(t.raw)[-1]["state"] not in ("idle", "done") and time.monotonic() < end:
                time.sleep(0.05)
            assert osc_reports(t.raw)[-1]["state"] in ("idle", "done"), osc_reports(t.raw)[-3:]
            mark["cli"] = expect_call(home, mark["cli"], "report-agent", "idle")

        ok &= scenario("34_herdr_reply_done_idle", term, reply_done)

        def quit_(t):
            t.send(CTRL_D)
            assert t.exited(5), "still running 5s after ctrl-d"
            assert os.WIFEXITED(t.status) and os.WEXITSTATUS(t.status) == 0, f"exit status {t.status}"
            clear = [report for report in osc_reports(t.raw) if report["state"] == "clear"]
            assert clear and clear[0]["app"] == "Chef", f"no OSC clear for Chef: {clear}"
            index = expect_call(home, mark["cli"], "release-agent", timeout=3)
            release = herdr_calls(home)[index - 1]
            assert release[0:3] == ["pane", "release-agent", HERDR_PANE], release
            assert herdr_calls(home)[-1] == release, "a report landed after the release"

        ok &= scenario("35_herdr_quit_releases", term, quit_)
    finally:
        run.close()

    # Herdr Desktop: no HERDR_BIN_PATH or pane id, only HC_TERMINAL_ID. OSC 7501 yes, CLI no.
    run = Run("Chef", herdr="desktop", MOCK_REPLY="0.4", MOCK_STREAM="0")
    try:
        def desktop(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            nxt = expect_osc(t, 0, state="idle", app="Chef", title="Chef")
            t.send("hello desktop")
            t.wait(lambda lines: "hello desktop" in " ".join(composer_rows(lines)))
            t.send("\r")
            expect_osc(t, nxt, state="working", app="Chef")
            t.wait(has("reply to hello desktop"), timeout=10)
            t.send(CTRL_D)
            assert t.exited(5), "still running 5s after ctrl-d"
            assert any(report["state"] == "clear" for report in osc_reports(t.raw)), "no OSC clear"
            t.settle(0.3)
            assert herdr_calls(run.home) == [], f"herdr was called: {herdr_calls(run.home)}"

        ok &= scenario("36_herdr_desktop_osc_only", run.term, desktop)
    finally:
        run.close()
    return ok


def sent_texts(run, bot):
    """What the mock's thread file holds as the user's messages, whole and in order."""
    name = "thread-" + "".join(ch for ch in bot if ch.isalnum()) + ".json"
    with open(os.path.join(run.home, "mock", name), encoding="utf-8") as handle:
        return [entry["text"] for entry in json.load(handle) if entry.get("role") == "user"]


def paged_flow():
    """Several questions at once: one card with `steps`, and widgets in a row."""
    # A: one widget, three steps, the second takes typed text.
    run = Run("Chef")
    try:
        def steps(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.settle(0.5)
            t.send("steps for the trip")
            t.send("\r")
            t.wait(has("question 1/3", "Pick a city", "1  Berlin"), timeout=6)
            t.wait(lacks("Name the hotel", "Pick a day"), timeout=2)
            t.wait(has("1-3 answer"), timeout=2)
            dump("39_steps_first_page", t)
            t.send("2")
            t.wait(has("question 2/3", "Name the hotel", "1  Ritz"), timeout=4)
            t.wait(lacks("Pick a city", "Pick a day"), timeout=2)
            t.send(LEFT)
            t.wait(has("question 1/3", "Pick a city", "● 2  Paris"), timeout=4)
            t.send(RIGHT)
            t.wait(has("question 2/3", "Name the hotel"), timeout=4)
            t.send("Grand")
            t.wait(lambda lines: "Grand" in " ".join(composer_rows(lines)))
            t.send("\r")
            t.wait(has("question 3/3", "Pick a day", "1  Monday"), timeout=4)
            t.send("[")
            t.wait(has("question 2/3", "●  Grand"), timeout=4)
            assert composer_rows(t.lines()) == ["→  Add a follow-up"], composer_rows(t.lines())
            t.send("]")
            t.wait(has("question 3/3"), timeout=4)
            t.send("2")
            t.wait(has("reply to Paris", "Friday"), timeout=10)
            assert sent_texts(run, "Chef")[-1] == "Paris\n\nGrand\n\nFriday", sent_texts(run, "Chef")[-1]
            t.wait(lacks("question"), timeout=2)
            assert not any(line.lstrip().startswith(("1  ", "2  ")) for line in t.lines()), "an answered card is numbered"

        ok = scenario("39_steps_paged_card", run.term, steps)
    finally:
        run.close()

    # B: three separate widget messages after the last user message.
    run = Run("Chef")
    try:
        def several(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.settle(0.5)
            t.send("multi for the trip")
            t.send("\r")
            t.wait(has("question 1/3", "Pick a city", "1  Berlin"), timeout=6)
            t.wait(lacks("Name the hotel", "Pick a day"), timeout=2)
            dump("40_widgets_first_page", t)
            t.send("1")
            t.wait(has("question 2/3", "Name the hotel"), timeout=4)
            t.send(RIGHT)
            t.wait(has("question 3/3", "Pick a day", "1  Monday"), timeout=4)
            t.send(LEFT)
            t.send("Grand")
            t.wait(lambda lines: "Grand" in " ".join(composer_rows(lines)))
            t.send("\r")
            t.wait(has("question 3/3", "Pick a day"), timeout=4)
            t.send("[[")
            t.wait(has("question 1/3", "● 1  Berlin"), timeout=4)
            t.send("]]")
            t.wait(has("question 3/3"), timeout=4)
            t.send("1")
            t.wait(has("reply to Pick a city: Berlin"), timeout=10)
            want = "Pick a city: Berlin\n\nName the hotel: Grand\n\nPick a day: Monday"
            assert sent_texts(run, "Chef")[-1] == want, sent_texts(run, "Chef")[-1]
            t.wait(lacks("question"), timeout=2)

        ok &= scenario("40_widgets_paged_card", run.term, several)
    finally:
        run.close()
    return ok


def markread_flow():
    """Ctrl-X on an unread row clears its label in place, without opening the chat."""
    run = Run(seen={"id:a3": "m29", "name:Chef": "m29"}, MOCK_REPLY="0.4", MOCK_THREAD_ID="session")
    try:
        def read_in_place(t):
            t.wait(lambda lines: row_state(lines, "Chef") == "unread", timeout=10)
            t.wait(has("1 unread"))
            t.send(DOWN * 2)
            t.settle(0.3)
            dump("41_markread_before", t)
            t.send("\x18")
            t.wait(lambda lines: row_state(lines, "Chef") == "idle", timeout=1.5)
            assert "unread" not in row_with(t.lines(), "Chef"), row_with(t.lines(), "Chef")
            assert not any("unread" in line for line in t.lines()), "an unread label is still shown"
            # Still the list, and the highlight is still on Chef: Enter opens it.
            assert has("Grok bots", "Researcher")(t.lines()), "left the list"
            dump("41_markread_after", t)
            t.send("\r")
            t.wait(has("Chef", "earlier reply", "Add a follow-up"))

        return scenario("41_markread_in_the_list", run.term, read_in_place)
    finally:
        run.close()


def sent_bytes_clean(t):
    """None of the fake grant handles the mock puts in these cards reached the terminal."""
    raw = bytes(t.raw)
    for handle in (b"REQHANDLE", b"ACCESSHANDLE", b"ENTRYHANDLE", b"APPLINKHANDLE", b"onepassword://"):
        assert handle not in raw, f"{handle.decode()} was printed"


def state_row(*words):
    """A screen with a whole row that is exactly each word: a card's state line."""
    return lambda lines: all(any(line.strip() == word for line in lines) for word in words)


def cards_flow():
    """Secret, 1Password and form cards: pending text, needs you, resolved state lines."""
    run = Run("Chef", rows=40, cols=100)
    try:
        def ask(t, text):
            t.wait(lambda lines: composer_rows(lines) == ["→  Add a follow-up"], timeout=10)
            t.send(text)
            t.wait(lambda lines: text in " ".join(composer_rows(lines)))
            t.send("\r")

        def secret(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.settle(0.5)
            ask(t, "secret please")
            t.wait(has("\U0001F511 secret: GitHub token", "Needed to push the workflow", "env: GH_TOKEN_WORKFLOW"), timeout=10)
            t.wait(has("waiting: enter it in the Grok Bot app"), timeout=3)
            # A pending card blocks the chat the way an open approval does. There is no y/n for it.
            t.wait(has("Pending decision"), timeout=3)
            assert lacks("y approve")(t.lines()), "a secret card offered y/n"
            sent_bytes_clean(t)
            dump("42_card_secret_pending", t)
            t.send(ESC)
            t.wait(has("Grok bots", "Researcher"))
            t.wait(lambda lines: row_state(lines, "Chef") == "needs you", timeout=3)
            t.wait(has("1 needs you"), timeout=3)
            assert "GitHub token" in row_with(t.lines(), "Chef"), row_with(t.lines(), "Chef")
            dump("42_card_secret_list", t)
            t.send("\r")
            t.wait(has("Chef", "waiting: enter it in the Grok Bot app", "Add a follow-up"), timeout=10)

        ok = scenario("42_card_secret_pending", run.term, secret)

        def reopen(t):
            """Leave the chat and open it again: the first read is the whole thread."""
            t.wait(lacks("Generating", "working"), timeout=12)
            t.send(ESC)
            t.wait(has("Grok bots", "Researcher"))
            t.send("\r")
            t.wait(has("Chef", "Add a follow-up"), timeout=10)

        def resolved_secret(t):
            ask(t, "resolve the secret")
            # A message after the card skips it at once: no `waiting`, no `needs you`, gray state line.
            t.wait(state_row("skipped"), timeout=10)
            t.wait(lacks("waiting: enter it in the Grok Bot app", "Pending decision"), timeout=5)
            # The app settled it behind the chat's back. A skipped card is not polled, so the next open shows it.
            reopen(t)
            t.wait(state_row("provided"), timeout=10)
            t.wait(lacks("waiting: enter it in the Grok Bot app", "skipped", "Pending decision"), timeout=5)
            dump("43_card_secret_provided", t)

        ok &= scenario("43_card_secret_provided", run.term, resolved_secret)

        def others(t):
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "cred buy it")
            t.wait(has("1Password: Buy the domain", "ionos.com: sign in to buy loanso.me"), timeout=10)
            t.wait(has("waiting: approve in 1Password or the Grok Bot app"), timeout=3)
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "form code")
            t.wait(has("form: Hetzner 2FA code", "Type the code from your phone", "OTP code (otp)"), timeout=10)
            t.wait(has("waiting: fill it in the Grok Bot app", "Pending decision"), timeout=3)
            # The credential card now has a message of yours after it: skipped. Only the form still waits.
            t.wait(state_row("skipped"), timeout=3)
            assert lacks("waiting: approve in 1Password or the Grok Bot app")(t.lines()), "a skipped card still waits"
            sent_bytes_clean(t)
            dump("44_card_credential_form_pending", t)

        ok &= scenario("44_card_credential_form_pending", run.term, others)

        def resolved_both(t):
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "resolve the rest")
            t.wait(lacks("waiting:", "Pending decision"), timeout=5)
            reopen(t)
            t.wait(state_row("approved", "submitted"), timeout=10)
            t.wait(lacks("waiting:", "Pending decision"), timeout=5)
            sent_bytes_clean(t)
            dump("45_card_resolved", t)
            # The request cards never take a value: the composer is still just the composer.
            assert composer_rows(t.lines()) == ["→  Add a follow-up"], composer_rows(t.lines())

        ok &= scenario("45_card_credential_form_resolved", run.term, resolved_both)

        def ended(t):
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "credfailed x")
            t.wait(state_row("failed"), timeout=10)
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "credexpired x")
            t.wait(state_row("failed", "expired"), timeout=10)
            # Neither one is waiting for the user.
            t.wait(lacks("waiting:", "Pending decision"), timeout=5)
            dump("46_card_failed_expired", t)

        ok &= scenario("46_card_failed_and_expired", run.term, ended)
    finally:
        run.close()
    return ok


def media_flow():
    """Images, attachments and canvas pages: a placeholder line each, never the file:// path."""
    run = Run("Chef", rows=40, cols=100)
    try:
        def ask(t, text):
            t.wait(lambda lines: composer_rows(lines) == ["→  Add a follow-up"], timeout=10)
            t.send(text)
            t.wait(lambda lines: text in " ".join(composer_rows(lines)))
            t.send("\r")

        def media(t):
            t.wait(has("Chef", "earlier reply"), timeout=10)
            t.settle(0.5)
            ask(t, "image please")
            t.wait(has("here is the proof", "terminal proof (780x1688)"), timeout=10)
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "attach please")
            t.wait(has("report.pdf", "quarterly numbers", "612x792", "https://files.example.com/report.pdf"), timeout=10)
            t.wait(lacks("Generating", "working"), timeout=12)
            ask(t, "page please")
            t.wait(has("the plan is ready: Extrablatt revamp", "page: Extrablatt revamp"), timeout=10)
            t.wait(lacks("Generating", "working"), timeout=12)
            raw = bytes(t.raw)
            for path in (b"file://", b"/home/box", b"agent-data", b"4f9c0de1"):
                assert path not in raw, f"{path.decode()} was printed"
            dump("47_media", t)

        ok = scenario("47_media_image_attachment_page", run.term, media)
    finally:
        run.close()
    return ok


def mouse_flow():
    """F2 in the chat turns mouse capture off and on; the footer says select mode while it is off.

    pyte does not track mouse modes, so only the hint is checked.
    """
    run = Run("Chef")
    try:
        def toggle(t):
            t.wait(has("Chef", "earlier reply", "? keys"), timeout=10)
            t.wait(lacks("Generating", "working"), timeout=12)
            assert not any("select mode" in line for line in t.lines()), "select mode shown at start"
            t.send("\x1bOQ")  # F2
            t.wait(has("select mode · f2 mouse"), timeout=3)
            dump("48_mouse_off", t)
            t.send("\x1bOQ")
            t.wait(lacks("select mode"), timeout=3)
            t.wait(has("? keys"))

        return scenario("48_mouse_toggle", run.term, toggle)
    finally:
        run.close()


def sections_flow():
    """Sidebar sections from the cache: dim headers, no selection on them, enter opens the bot below."""
    layout = {
        "pinned": [],
        "sections": [
            {"id": "section-e2e-1", "name": "Projects", "agent_ids": ["a3"]},
            {"id": "section-e2e-2", "name": "Empty", "agent_ids": ["nobody"]},
            {"id": "section-e2e-3", "name": "Home", "agent_ids": ["a2"]},
        ],
    }
    run = Run(sections=layout)
    try:
        def rows_of(lines):
            return [line.strip() for line in lines[2:12] if line.strip()]

        def headers(t):
            t.wait(has("Grok bots", "Projects", "Home", "other"), timeout=10)
            t.wait(has("Chef", "Travel Agent", "Researcher"))
            body = rows_of(t.lines())
            order = [next(word for word in ("Projects", "Home", "other", "Chef", "Travel Agent", "Researcher", "Empty") if word in row) for row in body[:6]]
            assert order == ["Projects", "Chef", "Home", "Travel Agent", "other", "Researcher"], order
            assert not any("Empty" in row for row in body), "an empty section showed a header"
            # The highlight is on the bot under the first header, never on the header.
            assert "→" in row_with(t.lines(), "Chef"), row_with(t.lines(), "Chef")
            assert "→" not in row_with(t.lines(), "Projects")
            dump("49_sections_headers", t)

        ok = scenario("49_sections_headers", run.term, headers)

        def down_skips(t):
            t.send(DOWN)
            t.wait(lambda lines: "→" in row_with(lines, "Travel Agent"))
            assert "→" not in row_with(t.lines(), "Home")
            t.send(UP)
            t.wait(lambda lines: "→" in row_with(lines, "Chef"))

        ok &= scenario("49_sections_arrows_skip_headers", run.term, down_skips)

        def search(t):
            t.send("trav")
            t.wait(has("/trav"))
            t.wait(lacks("Chef", "other"))
            row = row_with(t.lines(), "Travel Agent")
            assert row.rstrip().endswith("Home"), row
            assert not any(line.strip() == "Home" for line in t.lines()), "a header showed during a search"
            dump("49_sections_search", t)
            t.send(ESC)
            t.wait(has("Projects", "other"))

        ok &= scenario("49_sections_search", run.term, search)

        def open_first(t):
            t.send("\r")
            t.wait(has("Chef", "earlier reply", "Add a follow-up"), timeout=10)
            t.send(ESC)
            t.wait(has("Projects", "Home", "other"), timeout=10)
            t.send(DOWN + DOWN + "\r")
            t.wait(has("Researcher", "earlier reply", "Add a follow-up"), timeout=10)

        ok &= scenario("49_sections_enter_opens_bot_below_header", run.term, open_first)
    finally:
        run.close()
    return ok


FLOWS = {"sections": sections_flow, "media": media_flow, "cards": cards_flow, "paged": paged_flow, "main": main_flow, "error": error_flow, "loading": loading_flow, "compose": compose_flow, "words": words_flow, "select": select_flow, "idle": idle_flow, "polish": polish_flow, "offline": offline_flow, "bell": bell_flow, "history": history_flow, "herdr": herdr_flow, "unread": unread_flow, "markread": markread_flow, "mouse": mouse_flow}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="comma list of flows: " + ",".join(FLOWS))
    args = parser.parse_args()
    names = [name for name in args.only.split(",") if name] or list(FLOWS)
    for name in names:
        FLOWS[name]()
    failed = [name for name, error in RESULTS if error]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed, screens in {ARTIFACTS}")
    return len(failed)


if __name__ == "__main__":
    sys.exit(min(main(), 125))
