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
    finally:
        run.close()
    return ok


FLOWS = {"main": main_flow, "error": error_flow, "loading": loading_flow, "compose": compose_flow}


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
