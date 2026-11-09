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
import fcntl
import os
import pty
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


def environment(home, **mock):
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir, exist_ok=True)
    shim = os.path.join(bindir, "gbot")
    with open(shim, "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'mock_gbot.py')} \"$@\"\n")
    os.chmod(shim, 0o755)
    env = {key: value for key, value in os.environ.items() if key not in SCRUB}
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

    def __init__(self, *argv, rows=24, cols=80, **mock):
        self.home = tempfile.mkdtemp(prefix="gbot-e2e-")
        self.env = environment(self.home, **mock)
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


FLOWS = {"main": main_flow, "error": error_flow, "loading": loading_flow, "compose": compose_flow, "idle": idle_flow, "polish": polish_flow, "offline": offline_flow, "bell": bell_flow}


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
