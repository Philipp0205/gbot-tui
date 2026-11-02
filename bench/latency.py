#!/usr/bin/env python3
"""Time gbot-tui in a pseudo-terminal against bench/mock_gbot.py.

    python3 bench/latency.py [--delay 0.4] [--reply 1.5] [--runs 3] [--bg '#FFFFFF'] [--app path/to/gbot_tui.py]

Needs `pyte` (pip install pyte). Each run uses a fresh HOME, so "cold" has no
cache and "warm" is the second launch in that HOME.
"""

import argparse
import fcntl
import os
import pty
import shutil
import statistics
import struct
import sys
import tempfile
import termios
import threading
import time

import pyte

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ROWS, COLS = 24, 58
# The background the fake terminal reports: "#RRGGBB", "none" (DA1 only) or "mute".
BG = "#000000"


class Term:
    def __init__(self, argv, env):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while self.alive:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            self._answer(data)
            with self.lock:
                self.stream.feed(data)

    def _answer(self, data):
        """Reply to the background query like a real terminal. --bg mute answers nothing."""
        if BG == "mute":
            return
        reply = b""
        if b"\033]11;?" in data and BG.startswith("#"):
            channels = "/".join(BG[i : i + 2] * 2 for i in (1, 3, 5))
            reply += f"\033]11;rgb:{channels}\033\\".encode()
        if b"\033[c" in data:
            reply += b"\033[?62;22c"
        if reply:
            os.write(self.fd, reply)

    def text(self):
        with self.lock:
            return list(self.screen.display)

    def wait(self, predicate, timeout=15):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if predicate(self.text()):
                return time.monotonic()
            time.sleep(0.001)
        raise TimeoutError("screen never matched:\n" + "\n".join(self.text()))

    def send(self, data):
        os.write(self.fd, data.encode())

    def close(self):
        self.alive = False
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        os.close(self.fd)


def has(text):
    return lambda lines: any(text in line for line in lines)


def composer(lines):
    for line in lines:
        if line.startswith("│ →"):
            return line[4:].rstrip("│ ").strip()
    return None


def environment(home, state, args):
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir, exist_ok=True)
    shim = os.path.join(bindir, "gbot")
    with open(shim, "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'mock_gbot.py')} \"$@\"\n")
    os.chmod(shim, 0o755)
    env = dict(os.environ)
    env.update(
        HOME=home,
        PATH=bindir + os.pathsep + env.get("PATH", ""),
        TERM="xterm-256color",
        COLORFGBG="15;0",
        MOCK_STATE=state,
        MOCK_DELAY=str(args.delay),
        MOCK_REPLY=str(args.reply),
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
    )
    env.pop("HERDR_ENV", None)
    return env


APP = os.path.join(ROOT, "gbot_tui.py")


def launch(env, *extra):
    return Term([sys.executable, APP, *extra], env), time.monotonic()


def run_once(args):
    home = tempfile.mkdtemp(prefix="gbot-bench-")
    state = os.path.join(home, "mock")
    env = environment(home, state, args)
    out = {}
    try:
        for label in ("cold", "warm"):
            term, start = launch(env)
            out[f"first frame ({label})"] = term.wait(has("Grok bots")) - start
            out[f"list paint ({label})"] = term.wait(has("Researcher")) - start
            mark = time.monotonic()
            term.send("\r")
            out[f"open chat ({label})"] = term.wait(has("earlier reply")) - mark
            if label == "warm":
                worst = []
                term.wait(lambda lines: composer(lines) is not None)
                # Let the first full read land, so a poll is in flight while typing.
                time.sleep(0.6)
                typed = ""
                for char in "the quick brown fox jumps":
                    typed += char
                    mark = time.monotonic()
                    term.send(char)
                    goal = typed.rstrip()
                    if char == " ":
                        # A space draws nothing visible. Time the next letter instead.
                        continue
                    term.wait(lambda lines: (composer(lines) or "").endswith(goal))
                    worst.append(time.monotonic() - mark)
                    time.sleep(0.03)
                out["keypress median"] = statistics.median(worst)
                out["keypress worst"] = max(worst)
                for _ in typed:
                    term.send("\x7f")
                term.wait(lambda lines: "Add a follow-up" in (composer(lines) or ""))
                paste = "abcdefghij" * 20
                mark = time.monotonic()
                term.send(paste)
                term.wait(lambda lines: any(line.startswith("│") and "ghij" in line and "Add" not in line for line in lines) and paste[-6:] in "".join(lines))
                out["paste 200 chars"] = time.monotonic() - mark
                for _ in paste:
                    term.send("\x7f")
                term.wait(lambda lines: "Add a follow-up" in (composer(lines) or ""))
                term.send("ping")
                term.wait(lambda lines: (composer(lines) or "").endswith("ping"))
                mark = time.monotonic()
                term.send("\r")
                term.wait(
                    lambda lines: "Add a follow-up" in (composer(lines) or "")
                    and any(line.rstrip().endswith("ping") for line in lines if not line.startswith("│"))
                )
                out["enter to bubble"] = time.monotonic() - mark
                term.wait(has("reply to ping"), timeout=20)
                out["enter to reply (mock reply at %.1fs)" % args.reply] = time.monotonic() - mark
                mark = time.monotonic()
                term.send("\x1b")
                out["esc to list"] = term.wait(has("Grok bots")) - mark
            term.close()
        for label in ("first time", "again"):
            term, start = launch(env, "Chef")
            out[f"named open ({label})"] = term.wait(has("earlier reply")) - start
            term.wait(has("what to cook"))
            time.sleep(0.6)
            term.close()
        with open(os.path.join(state, "calls.log"), encoding="utf-8") as handle:
            out["gbot calls"] = sum(1 for _ in handle)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return out


def idle_cadence(args, seconds):
    home = tempfile.mkdtemp(prefix="gbot-bench-")
    state = os.path.join(home, "mock")
    env = environment(home, state, args)
    try:
        term, _start = launch(env, "Researcher")
        term.wait(has("earlier reply"))
        begin = time.time()
        time.sleep(seconds)
        term.close()
        with open(os.path.join(state, "calls.log"), encoding="utf-8") as handle:
            stamps = [float(line.split()[0]) for line in handle if " thread " in line]
        return sum(1 for stamp in stamps if stamp >= begin)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--reply", type=float, default=1.5)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--idle", type=float, default=30.0)
    parser.add_argument("--app", default=APP, help="gbot_tui.py to time, e.g. from an older checkout")
    parser.add_argument("--bg", default="#000000", help='background to answer OSC 11 with, "none" or "mute"')
    args = parser.parse_args()
    globals()["APP"] = os.path.abspath(args.app)
    globals()["BG"] = args.bg
    runs = [run_once(args) for _ in range(args.runs)]
    print(f"mock gbot call {args.delay:.2f}s, {args.runs} runs, median ms (worst)")
    for key in runs[0]:
        values = [run[key] for run in runs]
        if key == "gbot calls":
            print(f"  {key:38} {statistics.median(values):.0f}")
        else:
            print(f"  {key:38} {statistics.median(values) * 1000:7.0f}  ({max(values) * 1000:.0f})")
    if args.idle:
        print(f"  thread reads in {args.idle:.0f}s idle            {idle_cadence(args, args.idle)}")


if __name__ == "__main__":
    main()
