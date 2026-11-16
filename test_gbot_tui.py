import base64
import curses
import json
import os
import signal
import tempfile
import termios
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from gbottui import (
    activity,
    app as app_mod,
    backend,
    cache,
    cards as cards_mod,
    chat_screen,
    chat_state,
    chat_view,
    colors,
    draft,
    gbot_cli,
    grid,
    herdr,
    icons,
    markdown,
    mentions,
    paint,
    picker,
    roster,
    styles as styles_mod,
    tabs,
    terminal,
    theme,
    transcript,
    widgets,
)

_HOME = {}


def setUpModule():
    # The roster and thread caches live under ~/.cache. Keep a real one out of the tests.
    _HOME["old"] = os.environ.get("HOME")
    _HOME["dir"] = tempfile.TemporaryDirectory()
    os.environ["HOME"] = _HOME["dir"].name
    # publish() writes OSC to the terminal. Keep that out of the test log.
    _HOME["write_tty"] = terminal.write_tty
    terminal.write_tty = lambda data: None


def tearDownModule():
    terminal.write_tty = _HOME["write_tty"]
    if _HOME["old"] is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _HOME["old"]
    _HOME["dir"].cleanup()


class ParseTests(unittest.TestCase):
    def test_entry_text_and_user(self):
        self.assertEqual(transcript.entry_text({"text": "hi"}), "hi")
        self.assertEqual(transcript.entry_text({"message": {"content": "there"}}), "there")
        self.assertTrue(transcript.is_user({"role": "user", "text": "hi"}))
        self.assertFalse(transcript.is_user({"role": "assistant", "text": "yo"}))

    def test_thinking_is_not_message_text(self):
        self.assertEqual(transcript.entry_text({"kind": "thinking", "text": "plan the reply"}), "")
        self.assertEqual(
            transcript.entry_text({"kind": "tool-call", "name": "thinking", "content": "still planning"}),
            "",
        )
        self.assertEqual(
            transcript.entry_text({
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "private"},
                    {"type": "text", "text": "the answer"},
                ],
            }),
            "the answer",
        )
        self.assertEqual(
            transcript.entry_text({
                "kind": "send-message",
                "message": {"type": "text", "content": "<thinking>\nhidden\n</thinking>\n\nvisible"},
            }),
            "visible",
        )
        self.assertEqual(
            transcript.entry_text({"role": "assistant", "text": "<think>streaming the plan"}),
            "",
        )
        self.assertEqual(transcript.entry_text({"role": "user", "text": "<think>keep this</think> hi"}), "<think>keep this</think> hi")
        self.assertEqual(transcript.entry_text({"role": "assistant", "text": "yo"}), "yo")

    def test_thread_delta(self):
        page = transcript.parse_thread(
            {
                "target": {"id": "a1", "name": "Researcher"},
                "transcript": {"entries": [{"id": "m2", "role": "assistant", "text": "done"}]},
                "cursor": "m2",
                "gapReset": False,
            }
        )
        self.assertEqual(page["name"], "Researcher")
        self.assertEqual(page["cursor"], "m2")
        self.assertEqual(page["entries"][0]["text"], "done")
        self.assertEqual(page["avatar_shape"], "")

    def test_thread_keeps_the_avatar(self):
        page = transcript.parse_thread(
            {"target": {"id": "bot-1", "name": "Outfitly", "avatarShape": "heart", "avatarColor": "brown"}}
        )
        self.assertEqual(page["avatar_shape"], "heart")
        self.assertEqual(page["avatar_color"], "brown")
        self.assertEqual(herdr.program_key(page["id"]), "gbot-bot-1")
        # A full bot id used to make app= longer than 32 bytes, so Herdr dropped the report.
        key = herdr.program_key("ea566f99-aded-40c2-99fc-60e5477d2a61")
        self.assertLessEqual(len(key), 32)
        self.assertNotEqual(key, herdr.program_key("aa566f99-aded-40c2-99fc-60e5477d2a61"))
        self.assertEqual(colors.avatar_color("brown"), "#936439")
        self.assertEqual(
            gbot_cli.client_version_from_name("Grok_Bot_0.78.1_abc.AppImage"),
            "0.78.1",
        )

    def test_gbot_error(self):
        with self.assertRaises(gbot_cli.GbotError):
            gbot_cli.parse_gbot('{"error":{"message":"no session"}}')

    def test_json_flag_follows_the_subcommand(self):
        self.assertEqual(
            gbot_cli.gbot_argv(("bots", "list")),
            ["gbot", "bots", "list", "--gateway", "--json"],
        )
        self.assertEqual(
            gbot_cli.gbot_argv(("send", "--", "Researcher", "hi")),
            ["gbot", "send", "--gateway", "--json", "--", "Researcher", "hi"],
        )
        self.assertEqual(
            gbot_cli.gbot_argv(
                (
                    "approvals",
                    "respond",
                    "--target",
                    "Outfitly",
                    "--entry-id",
                    "card",
                    "--request-id",
                    "ask",
                    "--decision",
                    "decline",
                )
            ),
            [
                "gbot",
                "approvals",
                "respond",
                "--target",
                "Outfitly",
                "--entry-id",
                "card",
                "--request-id",
                "ask",
                "--decision",
                "decline",
                "--gateway",
                "--json",
            ],
        )

    def test_failure_uses_stderr_when_stdout_is_empty(self):
        self.assertEqual(
            gbot_cli.gbot_failure("", "Unknown option: --json.\n"),
            "Unknown option: --json.",
        )
        self.assertEqual(
            gbot_cli.gbot_failure("", '{"error":{"message":"no session"}}'),
            "no session",
        )


class AttachmentTests(unittest.TestCase):
    def test_attachment_shows_name_and_size(self):
        entry = {"kind": "user-attachment", "id": "a", "file_name": "p.jpg", "file_path": "/x/y.jpg",
                 "secondaryText": "649kB", "width": 10, "height": 20}
        self.assertEqual(transcript.entry_text(entry), "\U0001F4CE p.jpg (649kB, 10x20)")
        self.assertTrue(transcript.is_user(entry))


class WatchTests(unittest.TestCase):
    def test_reply_then_quiet_is_done(self):
        reply = [{"role": "assistant", "text": "shipped"}]
        phase, quiet, saw = transcript.watch_step("working", reply, 0, False, False)
        self.assertEqual(phase, "working")
        self.assertTrue(saw)
        phase, quiet, saw = transcript.watch_step(phase, [], quiet, saw, False)
        self.assertEqual(phase, "working")
        phase, quiet, saw = transcript.watch_step(phase, [], quiet, saw, False)
        self.assertEqual((phase, quiet, saw), ("done", 0, False))

    def test_thinking_does_not_finish_the_turn(self):
        phase, quiet, saw = transcript.watch_step(
            "working",
            [{"kind": "thinking", "text": "hmm"}],
            0,
            False,
            False,
        )
        self.assertEqual((phase, quiet, saw), ("working", 0, False))

    def test_a_cursor_entry_returned_again_does_not_keep_working(self):
        def page(*entries):
            return {"entries": list(entries), "cursor": entries[-1]["id"] if entries else "", "gap_reset": False, "name": "", "id": ""}

        chat = chat_state.Chat("Researcher")
        chat.ingest(page({"id": "m1", "role": "assistant", "text": "old"}), first=True)
        _typed(chat, "hi")
        self.assertEqual(chat_screen.chat_key(chat, "\r", None)[0], "send")
        self.assertEqual(chat.phase, "working")
        reply = {"id": "m3", "role": "assistant", "text": "the answer"}
        chat.ingest(page({"id": "m2", "role": "user", "text": "hi"}, reply))
        # The gateway keeps returning the `--after` entry, unchanged.
        chat.ingest(page(dict(reply)))
        self.assertEqual(chat.phase, "working")
        chat.ingest(page(dict(reply)))
        self.assertEqual(chat.phase, "done")
        # A changed copy of the same id is still new text.
        chat.ingest(page({"id": "m3", "role": "assistant", "text": "the answer, longer"}))
        self.assertEqual(chat.quiet, 0)

    def test_pending_approval_blocks(self):
        entries = [
            {
                "kind": "send-message",
                "message": {"type": "auto-review-approval", "approval": {"status": "pending"}},
            }
        ]
        self.assertTrue(transcript.pending_approval(entries))
        phase, _, _ = transcript.watch_step("working", [], 2, True, True)
        self.assertEqual(phase, "blocked")


class NameTests(unittest.TestCase):
    def test_parse_list_and_name(self):
        self.assertEqual(app_mod.parse_args(["--list"]), ("list", ""))
        self.assertEqual(app_mod.parse_args(["-l"]), ("list", ""))
        self.assertEqual(app_mod.parse_args(["--overview"]), ("overview", ""))
        self.assertEqual(app_mod.parse_args(["-o"]), ("overview", ""))
        self.assertEqual(app_mod.parse_args(["--complete"]), ("complete", ""))
        self.assertEqual(app_mod.parse_args(["Homescreen", "Manager"]), ("open", "Homescreen Manager"))
        self.assertEqual(app_mod.parse_args([]), ("open", ""))

    def test_match_exact_prefix_and_ambiguous(self):
        names = ["Outfitly", "Homescreen Manager", "Nutritionist", "Night Watch"]
        self.assertEqual(roster.match_bot("outfitly", names), "Outfitly")
        self.assertEqual(roster.match_bot("Home", names), "Homescreen Manager")
        with self.assertRaises(gbot_cli.GbotError) as error:
            roster.match_bot("n", names)
        self.assertIn("Nutritionist", str(error.exception))
        self.assertIn("Night Watch", str(error.exception))
        with self.assertRaises(gbot_cli.GbotError):
            roster.match_bot("missing", names)


class AvatarTests(unittest.TestCase):
    def test_icon_is_the_colored_shape_with_a_face(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mask = root / "bot-heart.png"
            face = root / "bot-heart-face.png"
            # The mask is a solid disc. The face is the body: frame 0 is filled,
            # the corner and the eye are holes, and frame 1 must not be drawn.
            Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(mask)
            sheet = Image.new("RGBA", (36, 18), (0, 0, 0, 0))
            for y in range(18):
                for x in range(18):
                    sheet.putpixel((x, y), (0, 0, 0, 255))
            sheet.putpixel((0, 0), (0, 0, 0, 0))
            sheet.putpixel((9, 9), (0, 0, 0, 0))
            sheet.putpixel((18, 0), (255, 255, 255, 255))
            sheet.save(face)
            stale = root / "icons" / "gbot" / "gbot-abc-heart-red.png"
            stale.parent.mkdir(parents=True)
            Image.new("RGBA", (4, 4), (0, 0, 0, 255)).save(stale)
            made = icons.install_avatars(
                [{"id": "abc", "name": "Outfitly", "avatarShape": "heart", "avatarColor": "red"}],
                root=tmp,
                src=tmp,
            )
            self.assertIn("gbot-abc", made)
            self.assertFalse(made["gbot-abc"]["tint"])
            icon = root / made["gbot-abc"]["icon"]
            image = Image.open(icon)
            self.assertEqual(image.size, (64, 64))
            body = image.getpixel((4, 4))
            self.assertEqual(body, (255, 38, 60, 255))
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((32, 32))[3], 0)
            self.assertTrue(all(pixel[:3] == (255, 38, 60) for pixel in image.get_flattened_data()))
            previous = herdr.APP
            try:
                icons.show_avatar(
                    {"id": "abc", "name": "Outfitly", "avatar_shape": "heart", "avatar_color": "red"},
                    root=tmp,
                    src=tmp,
                )
                self.assertEqual(herdr.APP, "Outfitly")
                report = herdr.osc_report("idle", "Outfitly")
                self.assertIn("app=Outfitly", report)
                self.assertNotIn("app=gbot-abc", report)
                self.assertLessEqual(len(herdr.APP.encode()), 32)
            finally:
                herdr.APP = previous
            programs = __import__("json").loads((root / "programs.json").read_text())
            self.assertEqual(programs["programs"]["gbot-abc"]["icon"], made["gbot-abc"]["icon"])
            self.assertEqual(programs["programs"]["gbot-abc"]["name"], "Outfitly")
            self.assertIn("Outfitly", programs["programs"]["gbot-abc"]["match"])
            self.assertIn("gbot-abc", programs["programs"]["gbot-abc"]["match"])
            spaced = icons.install_avatars(
                [{"id": "b3", "name": "Cursor Cloud Session", "avatarShape": "heart", "avatarColor": "red"}],
                root=tmp,
                src=tmp,
            )
            self.assertEqual(herdr.notice_name("Cursor Cloud Session"), "Cursor-Cloud-Session")
            self.assertEqual(spaced["gbot-b3"]["name"], "Cursor Cloud Session")
            self.assertIn("Cursor-Cloud-Session", spaced["gbot-b3"]["match"])
            self.assertIn("gbot-b3", spaced["gbot-b3"]["match"])
            self.assertTrue(all(" " not in item and "\u00a0" not in item for item in spaced["gbot-b3"]["match"]))
            long_id = "ea566f99-aded-40c2-99fc-60e5477d2a61"
            long_made = icons.install_avatars(
                [{"id": long_id, "name": "Researcher", "avatarShape": "heart", "avatarColor": "red"}],
                root=tmp,
                src=tmp,
            )
            compact = herdr.program_key(long_id)
            full = "gbot-" + long_id
            self.assertLessEqual(len(compact), 32)
            self.assertGreater(len(full), 32)
            self.assertEqual(long_made[compact]["icon"], long_made[full]["icon"])
            self.assertFalse(long_made[compact]["tint"])
            programs = __import__("json").loads((root / "programs.json").read_text())
            self.assertEqual(programs["programs"][compact]["icon"], programs["programs"][full]["icon"])
            self.assertFalse(programs["programs"][compact]["tint"])
            self.assertEqual(programs["programs"]["gbot-abc"]["icon"], made["gbot-abc"]["icon"])
            previous = herdr.APP
            try:
                icons.show_avatar(
                    {
                        "id": long_id,
                        "name": "Researcher",
                        "avatar_shape": "heart",
                        "avatar_color": "red",
                    },
                    root=tmp,
                    src=tmp,
                )
                self.assertEqual(herdr.APP, "Researcher")
                report = herdr.osc_report("working", "Researcher")
                self.assertIn("app=Researcher", report)
                self.assertLessEqual(len(herdr.APP.encode()), 32)
                self.assertIn("Researcher", programs["programs"][compact]["match"])
            finally:
                herdr.APP = previous

    def test_missing_pillow_skips_the_icon(self):
        import builtins

        real_import = builtins.__import__

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no pillow")
            return real_import(name, globals, locals, fromlist, level)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from PIL import Image

            Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(root / "bot-heart.png")
            Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(root / "bot-heart-face.png")
            builtins.__import__ = guarded
            try:
                made = icons.install_avatars(
                    [{"id": "abc", "name": "Outfitly", "avatarShape": "heart", "avatarColor": "red"}],
                    root=tmp,
                    src=tmp,
                )
            finally:
                builtins.__import__ = real_import
            self.assertEqual(made, {})

    def test_unset_or_invalid_look_uses_the_app_default(self):
        # These pairs are the app's id hash (FNV-1a, then the shape mix and one
        # mulberry32 draw). The same steps under node produced these strings.
        expected = {
            "abc": ("hex", "cyan"),
            "a1": ("tablet", "gray"),
            "Outfitly": ("tablet", "orange"),
            "ea566f99-aded-40c2-99fc-60e5477d2a61": ("pebble", "brown"),
            "🤖": ("hex", "yellow"),
            "agent-🤖": ("squircle", "orange"),
        }
        for agent_id, pair in expected.items():
            self.assertEqual(colors.bot_avatar({"id": agent_id}), pair)
            self.assertEqual(colors.bot_avatar({"agentId": agent_id}), pair)

        self.assertEqual(
            colors.bot_avatar({"id": "abc", "avatarShape": "heart", "avatarColor": "Red"}),
            ("heart", "red"),
        )
        self.assertEqual(
            colors.bot_avatar({"id": "abc", "avatar_shape": "heart", "avatar_color": "red"}),
            ("heart", "red"),
        )
        self.assertEqual(colors.bot_avatar({"id": "abc", "avatarColor": "red"}), ("hex", "red"))
        self.assertEqual(
            colors.bot_avatar({"id": "abc", "avatarShape": "", "avatarColor": "red"}),
            ("hex", "red"),
        )
        self.assertEqual(colors.bot_avatar({"id": "abc", "avatarShape": "heart"}), ("heart", "cyan"))
        self.assertEqual(
            colors.bot_avatar({"id": "a1", "avatarShape": "heart", "avatarColor": ""}),
            ("heart", "gray"),
        )
        self.assertEqual(colors.bot_avatar({"id": "abc"}), ("hex", "cyan"))
        self.assertEqual(
            colors.bot_avatar({"id": "ea566f99-aded-40c2-99fc-60e5477d2a61", "avatarShape": "", "avatarColor": ""}),
            ("pebble", "brown"),
        )
        self.assertEqual(
            colors.bot_avatar({"id": "abc", "avatarShape": "nope", "avatarColor": "plaid"}),
            ("hex", "cyan"),
        )
        self.assertEqual(
            colors.bot_avatar({"id": "🤖", "avatarShape": "heart!", "avatarColor": "#ff0000"}),
            ("hex", "yellow"),
        )

    def test_install_fills_an_icon_when_the_bot_never_picked_a_look(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for shape in ("hex", "heart"):
                Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(root / f"bot-{shape}.png")
                Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(root / f"bot-{shape}-face.png")

            def installed(bot):
                made = icons.install_avatars([bot], root=tmp, src=tmp)
                self.assertIn("gbot-abc", made)
                return made["gbot-abc"]["icon"]

            bare = installed({"id": "abc", "name": "Outfitly"})
            self.assertIn("gbot-abc-hex-cyan.png", bare)
            self.assertEqual(Image.open(root / bare).getpixel((4, 4)), (0x00, 0xBC, 0xA6, 255))
            self.assertIn("hex-red", installed({"id": "abc", "name": "Outfitly", "avatarColor": "red"}))
            self.assertIn("heart-cyan", installed({"id": "abc", "name": "Outfitly", "avatarShape": "heart"}))
            both = installed({"id": "abc", "name": "Outfitly", "avatarShape": "", "avatarColor": ""})
            self.assertIn("gbot-abc-hex-cyan.png", both)
            invalid = installed({"id": "abc", "name": "Outfitly", "avatarShape": "nope", "avatarColor": "plaid"})
            self.assertIn("gbot-abc-hex-cyan.png", invalid)
            self.assertFalse((root / "icons" / "gbot" / "gbot-abc-nope-plaid.png").exists())


class LookTests(unittest.TestCase):
    def test_screen_uses_the_terminal_background(self):
        old = os.environ.get("COLORFGBG")
        try:
            os.environ["COLORFGBG"] = "15;0"
            self.assertTrue(theme.dark_terminal())
            self.assertEqual(theme.user_bubble(), 235)
            os.environ["COLORFGBG"] = "0;15"
            self.assertFalse(theme.dark_terminal())
            self.assertEqual(theme.user_bubble(), 254)
        finally:
            if old is None:
                os.environ.pop("COLORFGBG", None)
            else:
                os.environ["COLORFGBG"] = old

    def test_composer_matches_the_cursor_box(self):
        top, mid, bot = grid.composer_lines(32, "")
        self.assertTrue(top.startswith("┌") and top.endswith("┐"))
        self.assertTrue(bot.startswith("└") and bot.endswith("┘"))
        self.assertIn("→  Add a follow-up", mid)
        _top, typed, _bot = grid.composer_lines(32, "hello")
        self.assertIn("hello", typed)
        self.assertNotIn("Add a follow-up", typed)
        wide, _middle, wide_bottom = grid.composer_lines(58, "")
        self.assertEqual(len(wide), 58)
        self.assertEqual(len(wide_bottom), 58)
        self.assertEqual(grid.composer_cursor_column("", True), 4)

    def test_long_draft_wraps_and_the_box_grows(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        lines = grid.draft_lines(text, 20)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), text)
        width = grid.composer_text_width(20)
        self.assertTrue(all(len(line) <= width for line in lines))
        _header, short_body, short_box, _footer = grid.layout(24, 1)
        _header, long_body, long_box, _footer = grid.layout(24, 4)
        self.assertEqual(short_box, 3)
        self.assertEqual(long_box, 6)
        self.assertEqual(short_body - long_body, 3)

    def test_fuzzy_search_orders_the_list(self):
        bots = [
            {"name": "Nutritionist", "title": ""},
            {"name": "Night Watch", "title": ""},
            {"name": "Outfitly", "title": "clothes"},
        ]
        self.assertEqual(
            [bot["name"] for bot in picker.fuzzy_bots("", bots)],
            ["Nutritionist", "Night Watch", "Outfitly"],
        )
        self.assertEqual(picker.fuzzy_bots("otf", bots)[0]["name"], "Outfitly")
        self.assertEqual(
            [bot["name"] for bot in picker.fuzzy_bots("n", bots)],
            ["Nutritionist", "Night Watch"],
        )
        self.assertEqual(picker.fuzzy_bots("clothes", bots)[0]["name"], "Outfitly")
        self.assertEqual(picker.fuzzy_bots("zzz", bots), [])
        spans = picker.picker_spans(
            {"name": "Outfitly", "title": "wardrobe", "avatarColor": "violet"},
            True,
            40,
        )
        self.assertEqual(spans[0], ("→ ", "bold"))
        self.assertEqual(spans[1], ("● ", "bot:violet"))
        self.assertEqual(spans[2], ("Outfitly", "bold"))
        self.assertEqual(spans[3], ("  ·  ", "dim"))
        self.assertEqual(spans[4], ("wardrobe", "dim"))

    def test_user_text_is_a_bubble_and_the_reply_is_plain(self):
        rows = grid.transcript_rows(
            [
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "yo"},
            ],
            40,
        )
        self.assertEqual([kind for kind, _text in rows], ["user", "gap", "text"])
        self.assertGreater(grid.bubble_column(40, "hi"), 20)

    def test_thinking_stays_off_the_transcript(self):
        rows = grid.transcript_rows(
            [
                {"role": "user", "text": "hi"},
                {"kind": "thinking", "text": "plan the reply"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "still planning"},
                        {"type": "text", "text": "the answer"},
                    ],
                },
            ],
            40,
        )
        texts = [text for kind, text in rows if kind != "gap"]
        self.assertEqual(texts, ["hi", "the answer"])
        self.assertNotIn("plan the reply", texts)
        self.assertNotIn("still planning", texts)
        marked = grid.transcript_rows(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "private plan"},
                        {"type": "text", "text": "<think>still hidden</think>\n\nSay **hello**"},
                    ],
                }
            ],
            40,
        )
        shown = " ".join(markdown.line_plain(line) for kind, line in marked if kind != "gap")
        self.assertIn("hello", shown)
        self.assertNotIn("**", shown)
        self.assertNotIn("private plan", shown)
        self.assertNotIn("still hidden", shown)

    def test_pending_card_reads_needs_approval(self):
        rows = grid.transcript_rows(
            [
                {
                    "kind": "send-message",
                    "message": {
                        "type": "auto-review-approval",
                        "approval": {"status": "pending"},
                    },
                }
            ],
            40,
        )
        self.assertEqual(rows, [("card", "Needs approval")])

    def test_approval_card_shows_what_is_pending(self):
        entry = {
            "id": "card",
            "kind": "send-message",
            "message": {
                "type": "auto-review-approval",
                "approval": {
                    "status": "pending",
                    "requestId": "ask",
                    "command": "npm publish",
                    "reason": "ship the package",
                },
            },
        }
        rows = grid.transcript_rows([entry], 40)
        texts = [text for kind, text in rows if kind == "card"]
        self.assertEqual(texts[0], "Needs approval")
        self.assertIn("npm publish", texts)
        self.assertIn("ship the package", texts)
        self.assertTrue(all(not line.startswith(" ") for line in texts))
        card = transcript.latest_decision([entry])
        self.assertEqual((card["entry_id"], card["request_id"]), ("card", "ask"))

    def test_a_decision_updates_the_card_and_will_not_send_twice(self):
        chat = chat_state.Chat("Researcher")
        chat.phase = "blocked"
        chat.messages = [
            {
                "id": "card",
                "kind": "send-message",
                "message": {
                    "type": "auto-review-approval",
                    "approval": {
                        "status": "pending",
                        "requestId": "ask",
                        "command": "npm publish",
                    },
                },
            }
        ]
        first = chat.begin_decision("accept")
        self.assertEqual(first, ("Researcher", "card", "ask", "accept"))
        texts = [text for kind, text in grid.transcript_rows(chat.messages, 40) if kind == "card-ok"]
        self.assertEqual(texts[0], "Approved")
        self.assertIn("npm publish", texts)
        self.assertIsNone(transcript.latest_decision(chat.messages, chat.answered))
        self.assertFalse(transcript.pending_approval(chat.messages))
        self.assertEqual(chat.phase, "idle")
        self.assertIsNone(chat.begin_decision("decline"))
        declined = chat_state.Chat("Researcher")
        declined.phase = "blocked"
        declined.messages = [
            {
                "id": "card",
                "kind": "send-message",
                "message": {
                    "type": "local-tool-permission",
                    "ask": {"status": "pending", "requestId": "ask"},
                },
            }
        ]
        declined.begin_decision("decline")
        rows = grid.transcript_rows(declined.messages, 40)
        self.assertEqual(rows, [("card-no", "Declined")])

    def test_a_failed_decision_can_be_retried(self):
        chat = chat_state.Chat("Researcher")
        chat.phase = "blocked"
        chat.messages = [
            {
                "id": "card",
                "kind": "send-message",
                "message": {
                    "type": "auto-review-approval",
                    "approval": {"status": "pending", "requestId": "ask"},
                },
            }
        ]
        chat.begin_decision("accept")
        chat.fail_decision()
        self.assertEqual(chat.messages[0]["message"]["approval"]["status"], "pending")
        self.assertEqual(chat.phase, "blocked")
        self.assertEqual(chat.begin_decision("decline")[-1], "decline")

    def test_a_decision_reloads_the_full_thread_once(self):
        chat = chat_state.Chat("Researcher")
        chat.cursor = "m9"
        chat.finish_decision()
        self.assertEqual(chat.take_read(), ("Researcher",))
        self.assertTrue(chat.replacing)
        self.assertEqual(chat.take_read(), ("Researcher", "m9"))
        chat.finish_decision()
        chat.take_read()
        chat.read_failed()
        self.assertEqual(chat.take_read(), ("Researcher",))

    def test_the_full_reload_replaces_a_card_the_cursor_would_skip(self):
        chat = chat_state.Chat("Researcher")
        chat.phase = "blocked"
        chat.cursor = "card"
        chat.messages = [
            {
                "id": "card",
                "kind": "send-message",
                "message": {
                    "type": "auto-review-approval",
                    "approval": {"status": "pending", "requestId": "ask", "command": "npm publish"},
                },
            }
        ]
        chat.begin_decision("accept")
        chat.finish_decision()
        chat.take_read()
        stale = transcript.parse_thread(
            {
                "transcript": {
                    "entries": [
                        {
                            "id": "card",
                            "kind": "send-message",
                            "message": {
                                "type": "auto-review-approval",
                                "approval": {
                                    "status": "pending",
                                    "requestId": "ask",
                                    "command": "npm publish",
                                },
                            },
                        }
                    ]
                },
                "cursor": "card",
            }
        )
        chat.ingest(stale)
        self.assertEqual(len(chat.messages), 1)
        texts = [text for kind, text in grid.transcript_rows(chat.messages, 40) if kind == "card-ok"]
        self.assertEqual(texts[0], "Approved")
        self.assertIsNone(chat.begin_decision("decline"))
        fresh = transcript.parse_thread(
            {
                "transcript": {
                    "entries": [
                        {
                            "id": "card",
                            "kind": "send-message",
                            "message": {
                                "type": "auto-review-approval",
                                "approval": {
                                    "status": "approved",
                                    "requestId": "ask",
                                    "command": "npm publish",
                                },
                            },
                        }
                    ]
                },
                "cursor": "card",
            }
        )
        chat.replacing = True
        chat.ingest(fresh)
        self.assertEqual(len(chat.messages), 1)
        texts = [text for kind, text in grid.transcript_rows(chat.messages, 40) if kind == "card-ok"]
        self.assertEqual(texts[0], "Approved")

    def test_an_updated_entry_replaces_the_same_id(self):
        chat = chat_state.Chat("Researcher")
        chat.messages = [{"id": "m1", "role": "assistant", "text": "old"}]
        chat.cursor = "m1"
        page = transcript.parse_thread(
            {
                "transcript": {"entries": [{"id": "m1", "role": "assistant", "text": "new"}]},
                "cursor": "m1",
            }
        )
        chat.apply(page, replace=False)
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(transcript.entry_text(chat.messages[0]), "new")
        extra = transcript.parse_thread(
            {
                "transcript": {"entries": [{"id": "m2", "role": "assistant", "text": "later"}]},
                "cursor": "m2",
            }
        )
        chat.apply(extra, replace=False)
        self.assertEqual(
            [transcript.entry_text(entry) for entry in chat.messages],
            ["new", "later"],
        )

    def test_red_and_yellow_list_dots_use_the_bot_color(self):
        sentinel = object()
        warn = object()
        saved_text = dict(styles_mod.TEXT_ATTR)
        saved_red = styles_mod.ATTR["red"]
        try:
            styles_mod.TEXT_ATTR["red"] = sentinel
            styles_mod.TEXT_ATTR["yellow"] = sentinel
            styles_mod.ATTR["red"] = warn
            self.assertIs(styles_mod.span_attr("bot:red"), sentinel)
            self.assertIs(styles_mod.span_attr("bot:yellow"), sentinel)
            self.assertIs(styles_mod.span_attr("red"), warn)
            self.assertIs(styles_mod.span_attr("bold"), styles_mod.ATTR["bold"])
            self.assertIs(styles_mod.dot_attr("red"), styles_mod.span_attr("bot:red"))
            self.assertIs(styles_mod.dot_attr("red"), sentinel)
            self.assertIsNot(styles_mod.dot_attr("red"), styles_mod.avatar_attr("red"))
        finally:
            styles_mod.TEXT_ATTR.clear()
            styles_mod.TEXT_ATTR.update(saved_text)
            styles_mod.ATTR["red"] = saved_red
        spans = picker.picker_spans({"name": "Alert", "title": "", "avatarColor": "red"}, True, 40)
        self.assertEqual(spans[1], ("● ", "bot:red"))
        yellow = picker.picker_spans(
            {"name": "Alert", "title": "", "avatarColor": "yellow"}, True, 40
        )
        self.assertEqual(yellow[1], ("● ", "bot:yellow"))

    def test_a_black_dot_uses_the_lifted_color(self):
        shown = colors.xterm_hex(colors.xterm_color(colors.readable_hex("#000000", True, "#000000")))
        self.assertNotEqual(shown.upper(), "#000000")
        self.assertGreaterEqual(colors.contrast(shown, "#000000"), 7)
        sentinel = object()
        saved = dict(styles_mod.TEXT_ATTR)
        try:
            styles_mod.TEXT_ATTR["black"] = sentinel
            self.assertIs(styles_mod.dot_attr("black"), sentinel)
            self.assertIs(styles_mod.bot_text_attr("black"), sentinel)
            self.assertIs(styles_mod.span_attr("bot:black"), sentinel)
        finally:
            styles_mod.TEXT_ATTR.clear()
            styles_mod.TEXT_ATTR.update(saved)
        spans = picker.picker_spans({"name": "Ink", "title": "", "avatarColor": "black"}, True, 40)
        self.assertEqual(spans[1], ("● ", "bot:black"))

    def test_wrap_keeps_words_together(self):
        self.assertEqual(grid.wrap("right now", 7), ["right", "now"])
        self.assertEqual(grid.wrap("your turn", 4), ["your", "turn"])
        self.assertEqual(grid.wrap("Bedrock's next", 12), ["Bedrock's", "next"])
        self.assertEqual(grid.draft_lines("before I review", 20), ["before I", "review"])
        self.assertEqual(
            grid.wrap("abcdefghijklmnopqrstuvwxyz", 10),
            ["abcdefghij", "klmnopqrst", "uvwxyz"],
        )
        self.assertTrue(all(not line.startswith(" ") for line in grid.wrap("hello world", 6)))
        self.assertEqual(grid.wrap("- hello world", 10), ["- hello", "  world"])
        self.assertEqual(grid.wrap("- short", 20), ["- short"])

    def test_red_text_is_lighter_on_black(self):
        original = colors.avatar_color("red")
        self.assertEqual(original, "#FF263C")
        lifted = colors.readable_hex(original, dark=True)
        self.assertGreater(colors.contrast_on_black(lifted), colors.contrast_on_black(original))
        self.assertGreaterEqual(colors.contrast_on_black(lifted), 7)
        darkened = colors.readable_hex(original, dark=False)
        self.assertGreaterEqual(colors.contrast(darkened, "#FFFFFF"), 4.5)

    def test_footer_states(self):
        self.assertEqual(grid.footer_spans("blocked", "")[0], ("Pending decision", "yellow"))
        working = "".join(text for text, _style in grid.footer_spans("working", ""))
        self.assertIn("Generating", working)
        self.assertEqual(grid.footer_spans("idle", "no session")[0][1], "red")
        blocked = grid.footer_spans("blocked", "", decide=True)
        self.assertEqual(blocked[0], ("Pending decision", "yellow"))
        self.assertIn("approve", blocked[1][0])
        self.assertIn("decline", blocked[1][0])
        failed = grid.footer_spans("blocked", "gateway down", decide=True)
        self.assertEqual(failed[0], ("gateway down", "red"))
        self.assertIn("approve", failed[1][0])
        self.assertIn("decline", failed[1][0])
        roomy = grid.footer_spans("blocked", "gateway down", decide=True, width=80)
        self.assertIn("approve", roomy[-1][0])

    def test_a_long_error_stays_readable_in_a_narrow_footer(self):
        short = "EnsureSandBox failed: 401 Unauthorized"
        rows = grid.footer_rows("blocked", short, decide=True, width=58)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], (short, "red"))
        self.assertEqual(rows[0][1][0].strip(), "y/n")
        self.assertLessEqual(sum(len(text) for text, _style in rows[0]), 56)
        long = "the approval gateway rejected this decision because the card changed (401)"
        rows = grid.footer_rows("blocked", long, decide=True, width=58)
        self.assertGreater(len(rows), 1)
        shown = " ".join(text for row in rows[:-1] for text, _style in row)
        self.assertEqual(shown, long)
        self.assertTrue(all(style == "red" for row in rows[:-1] for _text, style in row))
        self.assertTrue(all(sum(len(text) for text, _style in row) <= 56 for row in rows))
        self.assertIn("y", "".join(text for text, _style in rows[-1]))
        self.assertEqual(rows[-1][0], ("Pending decision", "yellow"))
        idle = grid.footer_rows("idle", long, width=58)
        self.assertEqual(" ".join(text for row in idle[:-1] for text, _style in row), long)
        self.assertEqual(idle[-1], grid.footer_spans("idle", ""))

    def test_decided_cards_leave_the_warning_color(self):
        pending = {
            "kind": "send-message",
            "message": {"type": "auto-review-approval", "approval": {"status": "pending"}},
        }
        approved = {
            "kind": "send-message",
            "message": {"type": "auto-review-approval", "approval": {"status": "approved"}},
        }
        declined = {
            "kind": "send-message",
            "message": {"type": "local-tool-permission", "ask": {"status": "declined"}},
        }
        self.assertEqual(transcript.card_row_kind(pending), "card")
        self.assertEqual(transcript.card_row_kind(approved), "card-ok")
        self.assertEqual(transcript.card_row_kind(declined), "card-no")
        self.assertEqual(styles_mod.CARD_STYLE["card"], "yellow")
        self.assertEqual(styles_mod.CARD_STYLE["card-ok"], "green")
        self.assertEqual(styles_mod.CARD_STYLE["card-no"], "gray")
        self.assertNotEqual(styles_mod.ATTR["green"], styles_mod.ATTR["yellow"])

    def test_y_types_unless_the_card_can_be_answered(self):
        chat = chat_state.Chat("Researcher")
        chat.phase = "blocked"
        chat.messages = [
            {
                "kind": "send-message",
                "message": {
                    "type": "auto-review-approval",
                    "approval": {"status": "pending"},
                },
            }
        ]
        self.assertEqual(chat_screen.composer_key(chat, "y"), ("type", "y"))
        self.assertEqual(chat_screen.composer_key(chat, "n"), ("type", "n"))
        chat.messages[0]["id"] = "card"
        chat.messages[0]["message"]["approval"]["requestId"] = "ask"
        self.assertEqual(chat_screen.composer_key(chat, "y"), ("decide", "accept"))
        self.assertEqual(chat_screen.composer_key(chat, "n"), ("decide", "decline"))
        self.assertEqual(chat_screen.composer_key(chat, "Y"), ("type", "Y"))
        self.assertEqual(chat_screen.composer_key(chat, "N"), ("type", "N"))
        self.assertEqual(chat_screen.composer_key(chat, "\x19"), ("type", "y"))
        self.assertEqual(chat_screen.composer_key(chat, "\x0e"), ("type", "n"))
        chat.typed = "a"
        self.assertEqual(chat_screen.composer_key(chat, "y"), ("type", "y"))
        chat.typed = ""
        chat.deciding = True
        self.assertEqual(chat_screen.composer_key(chat, "n"), ("ignore", ""))

    def test_a_blank_line_sits_under_the_header(self):
        class Screen:
            def __init__(self):
                self.rows = {}

            def getmaxyx(self):
                return (24, 58)

            def erase(self):
                self.rows.clear()

            def addnstr(self, y, x, text, n, attr=0):
                self.rows.setdefault(y, []).append(text[:n])

            def move(self, y, x):
                pass

            def refresh(self):
                pass

        chat = chat_state.Chat("Chef")
        chat.label = "what to cook"
        chat.messages = [{"role": "user", "text": "hi"}]
        screen = Screen()
        chat_view.draw(screen, chat)
        self.assertTrue(any("what to cook" in text for text in screen.rows.get(1, [])))
        self.assertNotIn(2, screen.rows)
        self.assertTrue(any("hi" in text for text in screen.rows.get(3, [])))
        self.assertEqual(grid.header_gap(2, "what to cook", 10), 1)
        self.assertEqual(grid.header_gap(2, "", 10), 0)
        self.assertEqual(grid.header_gap(0, "tag", 10), 0)

    def test_an_empty_chat_says_loading_until_the_first_read(self):
        class Screen:
            def __init__(self):
                self.rows = {}

            def getmaxyx(self):
                return (24, 58)

            def erase(self):
                self.rows.clear()

            def addnstr(self, y, x, text, n, attr=0):
                self.rows.setdefault(y, []).append(text[:n])

            def move(self, y, x):
                pass

            def refresh(self):
                pass

        def shown(chat):
            screen = Screen()
            chat_view.draw(screen, chat)
            return " ".join(" ".join(parts) for parts in screen.rows.values())

        chat = chat_state.Chat("Chef")
        self.assertIn(chat_view.LOADING, shown(chat))
        chat.error = "gateway is down"
        self.assertNotIn(chat_view.LOADING, shown(chat))
        chat.error = ""
        chat.loaded = True
        self.assertNotIn(chat_view.LOADING, shown(chat))

    def test_pick_restores_blocking_input(self):
        class Screen:
            def timeout(self, value):
                self.value = value
                raise StopIteration

        screen = Screen()
        with self.assertRaises(StopIteration):
            picker.pick(screen, [])
        self.assertEqual(screen.value, -1)

    def test_direct_name_loads_the_tagline(self):
        bots = [{"name": "Outfitly", "title": "what to wear", "avatarColor": "red"}]

        def fake_list():
            roster.ROSTER.clear()
            roster.ROSTER["Outfitly"] = bots[0]
            return bots

        original = roster.list_bots
        roster.list_bots = fake_list
        try:
            self.assertEqual(roster.resolve_for_open("out"), "Outfitly")
            self.assertEqual(chat_state.Chat("Outfitly").label, "what to wear")
        finally:
            roster.list_bots = original
            roster.ROSTER.clear()

    def test_a_bot_without_avatar_color_gets_the_default_color(self):
        bot = {"id": "ea566f99-aded-40c2-99fc-60e5477d2a61", "name": "Plain", "title": "", "avatarColor": ""}
        expected = colors.default_color(bot["id"])
        self.assertEqual(picker.picker_spans(bot, True, 40)[1], ("● ", "bot:" + expected))
        self.assertEqual(picker.picker_spans({"id": bot["id"], "name": "Plain"}, False, 40)[1], ("● ", "bot:" + expected))
        roster.ROSTER.clear()
        roster.ROSTER["Plain"] = bot
        try:
            self.assertEqual(chat_state.Chat("Plain").avatar_color, expected)
        finally:
            roster.ROSTER.clear()
        page = transcript.parse_thread({"target": {"id": bot["id"], "name": "Plain"}, "transcript": {"entries": []}})
        self.assertEqual(page["avatar_color"], expected)

    def test_a_bot_with_avatar_color_keeps_it(self):
        agent_id = "ea566f99-aded-40c2-99fc-60e5477d2a61"
        chosen = "black" if colors.default_color(agent_id) != "black" else "red"
        bot = {"id": agent_id, "name": "Ink", "avatarColor": chosen}
        self.assertEqual(picker.picker_spans(bot, True, 40)[1], ("● ", "bot:" + chosen))
        roster.ROSTER.clear()
        roster.ROSTER["Ink"] = bot
        try:
            self.assertEqual(chat_state.Chat("Ink").avatar_color, chosen)
        finally:
            roster.ROSTER.clear()
        page = transcript.parse_thread({"target": {"id": agent_id, "name": "Ink", "avatarColor": chosen}})
        self.assertEqual(page["avatar_color"], chosen)

    def test_the_composer_box_and_prompt_use_the_bot_color(self):
        class AttrScreen(FakeScreen):
            def __init__(self):
                super().__init__(24, 40)
                self.cells = []

            def addnstr(self, y, x, text, n, attr=0):
                self.cells.append((y, x, text[:n], attr))

        saved = dict(styles_mod.TEXT_ATTR)
        saved_avatar = dict(styles_mod.AVATAR_ATTR)
        try:
            styles_mod.TEXT_ATTR["blue"] = 777
            chat = chat_state.Chat("Researcher")
            chat.avatar_color = "blue"
            screen = AttrScreen()
            chat_view.draw(screen, chat)
        finally:
            styles_mod.TEXT_ATTR.clear()
            styles_mod.TEXT_ATTR.update(saved)
            styles_mod.AVATAR_ATTR.clear()
            styles_mod.AVATAR_ATTR.update(saved_avatar)
        colored = [cell for cell in screen.cells if cell[3] == 777]
        self.assertTrue(any(text.startswith("┌") or text.startswith("╭") for _y, _x, text, _a in colored))
        self.assertTrue(any(text == "│" for _y, _x, text, _a in colored))
        self.assertTrue(any(text == "→" for _y, _x, text, _a in colored))


class MarkdownTests(unittest.TestCase):
    def _plain(self, text, width=40):
        rows = grid.transcript_rows([{"role": "assistant", "text": text}], width)
        return [markdown.line_plain(line) for kind, line in rows if kind == "text"]

    def _styles(self, text, width=40):
        rows = grid.transcript_rows([{"role": "assistant", "text": text}], width)
        found = []
        for _kind, line in rows:
            if isinstance(line, list):
                found.extend(style for _text, style in line)
        return found

    def test_plain_text_stays_a_string(self):
        rows = grid.transcript_rows([{"role": "assistant", "text": "earlier"}], 40)
        self.assertIn(("text", "earlier"), rows)
        self.assertEqual(markdown.render_markdown("2 * 3 = 6", 40), ["2 * 3 = 6"])
        self.assertEqual(markdown.render_markdown("snake_case stays", 40), ["snake_case stays"])
        self.assertEqual(markdown.render_markdown("**hello", 40), ["**hello"])

    def test_a_plain_bullet_still_hangs(self):
        self.assertEqual(
            [markdown.line_plain(line) for line in markdown.render_markdown("- hello world", 10)],
            grid.wrap("- hello world", 10),
        )
        self.assertEqual(
            [markdown.line_plain(line) for line in markdown.render_markdown("**abcdefghijklmnopqrstuvwxyz**", 10)],
            grid.wrap("abcdefghijklmnopqrstuvwxyz", 10),
        )

    def test_emphasis_code_and_links_drop_the_markers(self):
        text = "A **bold** and *lean* word, plus `code` and [Grok](https://x.ai)."
        plain = " ".join(self._plain(text, 80))
        self.assertEqual(plain, "A bold and lean word, plus code and Grok (https://x.ai).")
        self.assertNotIn("**", plain)
        self.assertNotIn("`", plain)
        styles = self._styles(text, 80)
        self.assertIn("bold", styles)
        self.assertIn("em", styles)
        self.assertIn("code", styles)
        self.assertIn("link", styles)
        self.assertIn("dim", styles)

    def test_bold_wraps_without_losing_the_style(self):
        lines = markdown.render_markdown("**hello world**", 8)
        self.assertEqual([markdown.line_plain(line) for line in lines], ["hello", "world"])
        self.assertTrue(all(isinstance(line, list) for line in lines))
        self.assertTrue(all(style == "bold" for line in lines for _text, style in line))

    def test_headings_lists_quotes_code_and_tables(self):
        text = "\n".join(
            [
                "# Title",
                "",
                "- one",
                "- two words here",
                "",
                "1. first",
                "",
                "> quoted **bit**",
                "",
                "```python",
                "print(1)",
                "```",
                "",
                "| name | role |",
                "| --- | --- |",
                "| Ada | cook |",
                "",
                "---",
            ]
        )
        plain = self._plain(text, 40)
        blob = "\n".join(plain)
        self.assertNotIn("# Title", blob)
        self.assertNotIn("**", blob)
        self.assertNotIn("```", blob)
        self.assertNotIn("| --- |", blob)
        self.assertIn("Title", plain)
        self.assertIn("- one", plain)
        self.assertIn("1. first", plain)
        self.assertTrue(any(line.endswith("quoted bit") for line in plain))
        self.assertIn("python", plain)
        self.assertTrue(any("print(1)" in line for line in plain))
        self.assertTrue(any("Ada" in line and "cook" in line for line in plain))
        self.assertTrue(any(set(line) == {"─"} for line in plain))
        text_w = 36
        self.assertTrue(all(len(line) <= text_w for line in plain))
        styles = self._styles(text, 40)
        self.assertIn("bold", styles)
        self.assertIn("code", styles)

    def test_a_narrow_table_stays_inside_the_line(self):
        text = "| name | role |\n| --- | --- |\n| Adelaide | cook |"
        plain = self._plain(text, 20)
        self.assertTrue(plain)
        self.assertTrue(all(len(line) <= 16 for line in plain))
        self.assertTrue(any("Adel" in line or "…" in line for line in plain))
        squeezed = markdown.render_markdown(
            "| a | b | c |\n| --- | --- | --- |\n| 1 | 2 | 3 |",
            4,
        )
        self.assertTrue(squeezed)
        self.assertTrue(all(len(markdown.line_plain(line)) <= 4 for line in squeezed))

    def test_a_user_bubble_renders_markdown_and_stays_rectangular(self):
        rows = grid.transcript_rows([{"role": "user", "text": "say **hi** now"}], 40)
        lines = [line for kind, line in rows if kind == "user"]
        self.assertEqual(len(lines), 1)
        self.assertIn("hi", markdown.line_plain(lines[0]))
        self.assertNotIn("*", markdown.line_plain(lines[0]))
        wide = grid.transcript_rows(
            [{"role": "user", "text": "**hello**\nworld"}],
            40,
        )
        bubbles = [markdown.line_plain(line) for kind, line in wide if kind == "user"]
        self.assertGreater(len(bubbles), 1)
        self.assertEqual(len(set(map(len, bubbles))), 1)

    def test_the_preview_drops_markers(self):
        entries = [{"role": "assistant", "text": "**lunch** is ready"}]
        self.assertEqual(activity.activity_preview(entries, "idle"), "lunch is ready")

    def test_styles_keep_the_bot_color_except_code(self):
        base = 7
        bold = paint.transcript_attr("bold", base)
        self.assertEqual(bold & curses.A_BOLD, curses.A_BOLD)
        self.assertEqual(bold & ~curses.A_BOLD, base)
        self.assertEqual(paint.transcript_attr("code", base), styles_mod.ATTR["gray"])
        self.assertEqual(paint.transcript_attr("dim", base), styles_mod.ATTR["gray"])
        link = paint.transcript_attr("link", base)
        self.assertEqual(link & curses.A_UNDERLINE, curses.A_UNDERLINE)
        bubble = paint.user_attr("code", base)
        self.assertEqual(bubble & base, base)
        self.assertEqual(bubble & curses.A_DIM, curses.A_DIM)

    def test_a_markdown_reply_draws_without_the_markers(self):
        chat = chat_state.Chat("Researcher")
        chat.messages = [{"role": "assistant", "text": "Say **hello** and `go`."}]
        screen = FakeScreen(24, 58)
        chat_view.draw(screen, chat)
        shown = " ".join(" ".join(row) for row in screen.rows.values())
        self.assertIn("hello", shown)
        self.assertIn("go", shown)
        self.assertNotIn("**", shown)
        self.assertNotIn("`", shown)
        self.assertIn("●", shown)


class CellWidthTests(unittest.TestCase):
    def test_wide_and_combining_characters_take_their_cells(self):
        self.assertEqual(paint.cell_width("abc"), 3)
        self.assertEqual(paint.cell_width("📎"), 2)
        self.assertEqual(paint.cell_width("📄 report"), 9)
        self.assertEqual(paint.cell_width("中文"), 4)
        self.assertEqual(paint.cell_width("e\u0301"), 1)

    def test_a_span_after_an_emoji_starts_after_its_two_cells(self):
        class Recorder(FakeScreen):
            def __init__(self):
                super().__init__(4, 80)
                self.drawn = []

            def addnstr(self, y, x, text, n, attr=0):
                self.drawn.append((x, text))

        screen = Recorder()
        line = [("📎 ", "dim"), ("report.pdf (612x792) ", "dim"), ("https://x.example", "link")]
        terminal.LINK_HITS.clear()
        try:
            paint.paint_bot_line(screen, 1, 3, line, 0)
            # "📎 " is 3 cells, so the next span starts at column 6, not 5.
            self.assertIn((6, "report.pdf (612x792) "), screen.drawn)
            # The link starts after 3 + 21 cells and is 17 cells wide.
            self.assertEqual(terminal.LINK_HITS, [(1, 3 + 3 + 21, 3 + 3 + 21 + 17, "https://x.example")])
        finally:
            terminal.LINK_HITS.clear()


class SendTests(unittest.TestCase):
    def test_enter_shows_the_line_and_the_poll_replaces_it(self):
        chat = chat_state.Chat("Researcher")
        chat.typed = "  hello  "
        self.assertEqual(chat.arm_send(), "hello")
        self.assertEqual(chat.phase, "working")
        self.assertEqual(chat.typed, "")
        self.assertEqual(transcript.entry_text(chat.messages[-1]), "hello")
        self.assertTrue(chat.messages[-1].get("local"))
        page = transcript.parse_thread(
            {
                "target": {"id": "a1", "name": "Researcher"},
                "transcript": {
                    "entries": [
                        {"id": "m1", "role": "user", "text": "hello"},
                        {"id": "m2", "role": "assistant", "text": "yo"},
                    ]
                },
                "cursor": "m2",
            }
        )
        chat.apply(page, replace=False)
        self.assertEqual([transcript.entry_text(entry) for entry in chat.messages], ["hello", "yo"])
        self.assertEqual(chat.messages[0]["id"], "m1")
        self.assertFalse(chat.messages[0].get("local"))

    def test_a_reply_between_two_sends_stays_between_them(self):
        chat = chat_state.Chat("Researcher")
        chat.messages = [{"role": "assistant", "text": "earlier"}]
        chat.typed = "one"
        chat.arm_send()
        chat.typed = "two"
        chat.arm_send()
        page = transcript.parse_thread(
            {
                "transcript": {
                    "entries": [
                        {"id": "u1", "role": "user", "text": "one"},
                        {"id": "a1", "role": "assistant", "text": "ack"},
                        {"id": "u2", "role": "user", "text": "two"},
                    ]
                },
                "cursor": "u2",
            }
        )
        chat.apply(page, replace=False)
        self.assertEqual(
            [transcript.entry_text(entry) for entry in chat.messages],
            ["earlier", "one", "ack", "two"],
        )

    def test_gap_reset_keeps_an_unacked_local_line(self):
        chat = chat_state.Chat("Researcher")
        chat.typed = "hello"
        chat.arm_send()
        page = transcript.parse_thread(
            {
                "transcript": {"entries": [{"id": "old", "role": "assistant", "text": "prior"}]},
                "cursor": "old",
                "gapReset": True,
            }
        )
        chat.apply(page, replace=False)
        self.assertEqual(
            [transcript.entry_text(entry) for entry in chat.messages],
            ["prior", "hello"],
        )
        self.assertTrue(chat.messages[-1].get("local"))

    def test_footer_returns_to_idle_after_the_reply(self):
        chat = chat_state.Chat("Researcher")
        chat.cursor = "old"
        chat.phase = "working"
        chat.messages = [{"role": "user", "text": "hi", "local": True}]
        reply = transcript.parse_thread(
            {
                "target": {"id": "a1", "name": "Researcher"},
                "transcript": {
                    "entries": [
                        {"id": "m1", "role": "user", "text": "hi"},
                        {"id": "m2", "role": "assistant", "text": "shipped"},
                    ]
                },
                "cursor": "m2",
            }
        )
        chat.ingest(reply)
        self.assertEqual(chat.phase, "working")
        working = "".join(text for text, _style in grid.footer_spans(chat.phase, ""))
        self.assertIn("Generating", working)
        quiet = transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
        chat.ingest(quiet)
        chat.ingest(quiet)
        self.assertEqual(chat.phase, "done")
        done = grid.footer_spans(chat.phase, "")
        self.assertEqual(done, grid.footer_spans("idle", ""))
        self.assertNotIn("Generating", "".join(text for text, _style in done))


class FakeScreen:
    def __init__(self, height=24, width=58):
        self.size = (height, width)
        self.rows = {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows.clear()

    def addnstr(self, y, x, text, n, attr=0):
        self.rows.setdefault(y, []).append(text[:n])

    def move(self, y, x):
        pass

    def refresh(self):
        pass


class SpeedTests(unittest.TestCase):
    def test_polls_fast_while_a_reply_comes_and_back_off_when_idle(self):
        chat = chat_state.Chat("Researcher")
        chat.phase = "working"
        self.assertLessEqual(chat.poll_delay(), 0.5)
        chat.saw_other = True
        self.assertEqual(chat.poll_delay(), chat_state.STREAM_DELAY)
        chat.quiet = 1
        # Two quiet polls still span about four seconds before "done".
        self.assertGreaterEqual(chat.poll_delay() * transcript.QUIET_POLLS, 4)
        chat.phase = "blocked"
        self.assertEqual(chat.poll_delay(), chat_state.BLOCKED_DELAY)
        chat.phase = "idle"
        chat.cursor = "m1"
        chat.loaded = True
        delays = []
        quiet = transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m1"})
        for _ in range(8):
            delays.append(chat.poll_delay())
            chat.ingest(quiet)
        self.assertEqual(delays[0], chat_state.IDLE_DELAYS[0])
        self.assertEqual(delays[-1], chat_state.IDLE_DELAYS[-1])
        self.assertEqual(delays, sorted(delays))
        chat.nudge()
        self.assertEqual(chat.poll_delay(), chat_state.IDLE_DELAYS[0])
        fresh = transcript.parse_thread(
            {"transcript": {"entries": [{"id": "m2", "role": "assistant", "text": "hi"}]}, "cursor": "m2"}
        )
        chat.idle_polls = 4
        chat.ingest(fresh)
        self.assertEqual(chat.idle_polls, 0)

    def test_the_bot_list_comes_from_the_cache(self):
        bots = [
            {"id": "a1", "name": "Outfitly", "title": "what to wear", "avatarColor": "red", "secret": "x"},
            {"id": "a2", "name": "Chef", "title": "what to cook"},
        ]
        cache.write_roster(bots)
        cached = cache.read_roster()
        self.assertEqual([bot["name"] for bot in cached], ["Outfitly", "Chef"])
        self.assertNotIn("secret", cached[0])
        self.assertEqual(os.stat(cache.roster_path()).st_mode & 0o777, 0o600)
        calls = []
        original_list = roster.list_bots
        original_later = roster.refresh_roster_later
        roster.list_bots = lambda *args, **kwargs: calls.append("list") or bots
        roster.refresh_roster_later = lambda: calls.append("later")
        roster.ROSTER.clear()
        try:
            self.assertEqual(roster.resolve_for_open("out"), "Outfitly")
            self.assertEqual(calls, ["later"])
            self.assertEqual(chat_state.Chat("Outfitly").label, "what to wear")
            # A name the cache does not know still waits for the real list.
            bots.append({"id": "a3", "name": "Nomad", "title": "new"})
            self.assertEqual(roster.resolve_for_open("nom"), "Nomad")
            self.assertEqual(calls, ["later", "list"])
        finally:
            roster.list_bots = original_list
            roster.refresh_roster_later = original_later
            roster.ROSTER.clear()
            os.remove(cache.roster_path())

    def test_a_chat_opens_on_its_cached_thread(self):
        card = {
            "id": "card",
            "kind": "send-message",
            "message": {"type": "auto-review-approval", "approval": {"status": "pending", "requestId": "ask"}},
        }
        messages = [
            {"id": "m1", "role": "assistant", "text": "earlier"},
            card,
            {"role": "user", "text": "unsent", "local": True},
        ]
        cache.write_thread_cache("Researcher", messages)
        path = cache.thread_cache_path("Researcher")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        cached = cache.read_thread_cache("Researcher")
        self.assertEqual([entry.get("id") for entry in cached], ["m1", "card"])
        self.assertEqual(cache.read_thread_cache("Chef"), [])
        chat = chat_state.Chat("Researcher")
        chat.show_cached(cached)
        rows = grid.transcript_rows(chat.messages, 40)
        self.assertIn(("text", "earlier"), rows)
        # A cached card is not live, so y is text until the first read.
        self.assertFalse(chat.loaded)
        self.assertEqual(chat_screen.composer_key(chat, "y"), ("type", "y"))
        self.assertEqual(chat.take_read(), ("Researcher",))
        page = transcript.parse_thread(
            {"transcript": {"entries": [{"id": "m1", "role": "assistant", "text": "earlier"}]}, "cursor": "m1"}
        )
        chat.ingest(page, first=True)
        self.assertTrue(chat.loaded)
        self.assertEqual([entry.get("id") for entry in chat.messages], ["m1"])
        os.remove(path)

    def test_a_paste_is_one_batch_of_keys(self):
        class Keys:
            def __init__(self, keys):
                self.keys = list(keys)

            def get_wch(self):
                if not self.keys:
                    raise curses.error("no input")
                return self.keys.pop(0)

        screen = Keys("hello world")
        self.assertEqual("".join(paint.read_keys(screen)), "hello world")
        self.assertEqual(paint.read_keys(screen), [])
        chat = chat_state.Chat("Researcher")
        for key in "hi":
            self.assertIsNone(chat_screen.chat_key(chat, key, None))
        self.assertEqual(chat.typed, "hi")
        self.assertEqual(chat_screen.chat_key(chat, "\r", None), ("send", ("Researcher", "hi")))
        self.assertTrue(chat.messages[-1].get("local"))
        self.assertEqual(chat_screen.chat_key(chat, "\x1b", None), "back")

    def test_alt_page_scrolls_half_of_a_page(self):
        self.assertEqual(terminal.page_step(40, False), 36)
        self.assertEqual(terminal.page_step(40, True), 18)
        self.assertEqual(terminal.page_step(5, True), 1)
        self.assertEqual(terminal.page_motion(curses.KEY_PPAGE), (-1, False))
        self.assertEqual(terminal.page_motion(curses.KEY_NPAGE), (1, False))
        original = curses.keyname

        def keyname(key):
            return {7: b"kPRV3", 8: b"kNXT3"}.get(key, b"KEY_OTHER")

        curses.keyname = keyname
        saved_lines = getattr(curses, "LINES", None)
        curses.LINES = 40
        try:
            self.assertEqual(terminal.page_motion(7), (-1, True))
            self.assertEqual(terminal.page_motion(8), (1, True))
            self.assertIsNone(terminal.page_motion(9))
            chat = chat_state.Chat("Researcher")
            chat.scroll = 100
            chat.follow = True
            self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_PPAGE, None))
            self.assertEqual(chat.scroll, 64)
            self.assertFalse(chat.follow)
            self.assertIsNone(chat_screen.chat_key(chat, 7, None))
            self.assertEqual(chat.scroll, 46)
            screen = FakeScreen(24, 80)
            chat.follow = False
            chat.scroll = 0
            self.assertIsNone(chat_screen.chat_key(chat, 8, screen))
            self.assertEqual(chat.scroll, 18)
            self.assertTrue(chat.follow)
        finally:
            curses.keyname = original
            if saved_lines is None:
                try:
                    del curses.LINES
                except AttributeError:
                    pass
            else:
                curses.LINES = saved_lines
        self.assertIs(getattr(curses, "LINES", None), saved_lines)

    def test_a_long_error_gets_its_own_rows_on_screen(self):
        chat = chat_state.Chat("Researcher")
        chat.messages = [{"role": "assistant", "text": "hello"}]
        chat.error = "EnsureSandBox failed: 401 Unauthorized. Sign in to Grok Bot again and retry."
        screen = FakeScreen(24, 58)
        chat_view.draw(screen, chat)
        lines = [" ".join(screen.rows.get(y, [])).strip() for y in (21, 22, 23)]
        self.assertTrue(lines[0].startswith("EnsureSandBox"))
        self.assertEqual(" ".join(lines[:2]), chat.error)
        self.assertIn("enter send", lines[2])
        self.assertTrue(any("hello" in text for row in screen.rows.values() for text in row))


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            key: os.environ.get(key)
            for key in (
                "COLORFGBG",
                theme.THEME_ENV,
                "HERDR_ENV",
                "HERDR_PANE_ID",
                "HERDR_BIN_PATH",
                *herdr.DESKTOP_MARKS,
            )
        }
        for key in self.saved:
            os.environ.pop(key, None)
        self.probed = theme.PROBED["bg"]
        self.theme = dict(theme.THEME)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        theme.PROBED["bg"] = self.probed
        theme.THEME.clear()
        theme.THEME.update(self.theme)

    def test_osc11_replies(self):
        parse = theme.parse_osc11
        self.assertEqual(parse(b"\033]11;rgb:ffff/ffff/ffff\033\\"), "#FFFFFF")
        self.assertEqual(parse("\033]11;rgb:1e1e/1e1e/2e2e\007"), "#1E1E2E")
        self.assertEqual(parse("\033]11;rgb:fa/f9/f6\007\033[?62;22c"), "#FAF9F6")
        self.assertEqual(parse("\033]11;rgb:f/0/8\007"), "#FF0088")
        self.assertEqual(parse("\033]11;rgba:0000/0000/0000/ffff\033\\"), "#000000")
        self.assertEqual(parse("\033]11;#fdf6e3\033\\"), "#FDF6E3")
        self.assertIsNone(parse(b"\033[?62;22c"))
        self.assertIsNone(parse("\033]11;rgb:zz/00/00\007"))
        self.assertIsNone(parse("\033]11;rgb:ffff/ffff"))
        self.assertIsNone(parse("\033]11;rgb:ffff/ffff/ffff"))
        self.assertIsNone(parse(""))

    def test_theme_comes_from_env_then_osc11_then_colorfgbg(self):
        choice = theme.theme_choice
        self.assertEqual(choice("light", "#000000", "15;0"), ("light", "GBOT_TUI_THEME"))
        self.assertEqual(choice(" Dark ", "#FFFFFF", "0;15"), ("dark", "GBOT_TUI_THEME"))
        self.assertEqual(choice("solarized", "#FFFFFF", "15;0"), ("light", "osc11"))
        self.assertEqual(choice(None, "#FAFAFA", "15;0"), ("light", "osc11"))
        self.assertEqual(choice(None, "#282C34", "0;15"), ("dark", "osc11"))
        self.assertEqual(choice(None, None, "0;15"), ("light", "COLORFGBG"))
        self.assertEqual(choice(None, None, "0;default;7"), ("light", "COLORFGBG"))
        self.assertEqual(choice(None, None, "15;0"), ("dark", "COLORFGBG"))
        self.assertEqual(choice(None, None, None), ("dark", "default"))
        self.assertEqual(choice("", None, "garbage"), ("dark", "default"))

    def test_herdr_light_theme_without_colorfgbg(self):
        theme.PROBED["bg"] = "#FFFFFF"
        self.assertFalse(theme.dark_terminal())
        bubble = theme.user_bubble()
        self.assertTrue(colors.is_light(colors.xterm_hex(bubble)))
        self.assertNotEqual(colors.xterm_hex(bubble), "#FFFFFF")
        self.assertGreaterEqual(
            colors.contrast(colors.xterm_hex(colors.bubble_fg(bubble)), colors.xterm_hex(bubble)), 7
        )
        os.environ[theme.THEME_ENV] = "dark"
        self.assertTrue(theme.dark_terminal())
        self.assertEqual(theme.user_bubble(), 235)

    def test_bubble_on_a_probed_dark_background(self):
        theme.PROBED["bg"] = "#1E1E2E"
        bubble = theme.user_bubble()
        self.assertFalse(colors.is_light(colors.xterm_hex(bubble)))
        self.assertGreater(
            colors._hex_luminance(colors.xterm_hex(bubble)), colors._hex_luminance("#1E1E2E")
        )
        self.assertEqual(colors.bubble_fg(bubble), 255)
        self.assertEqual(colors.bubble_fg(235), 255)
        self.assertEqual(colors.bubble_fg(254), 234)

    def test_every_bot_color_reads_on_white_and_on_black(self):
        for name, hex_color in colors.AVATAR_COLORS.items():
            for dark, background, target in ((True, "#000000", 7), (False, "#FFFFFF", 4.5)):
                shown = colors.xterm_hex(colors.xterm_color(colors.readable_hex(hex_color, dark, background)))
                self.assertGreaterEqual(colors.contrast(shown, background), target, (name, dark))

    def test_named_styles_read_on_a_light_background(self):
        for name, index in styles_mod.theme_palette(False).items():
            self.assertGreaterEqual(colors.contrast(colors.xterm_hex(index), "#FFFFFF"), 4.5, name)
        for name, index in styles_mod.theme_palette(True).items():
            self.assertGreaterEqual(colors.contrast(colors.xterm_hex(index), "#000000"), 4.5, name)

    def answer(self, reply):
        import threading
        import time

        master, slave = os.openpty()
        asked = []

        def terminal():
            data = b""
            end = time.monotonic() + 2
            while b"\033[c" not in data and time.monotonic() < end:
                try:
                    data += os.read(master, 1024)
                except OSError:
                    break
            asked.append(data)
            if reply:
                os.write(master, reply)

        thread = threading.Thread(target=terminal, daemon=True)
        thread.start()
        start = time.monotonic()
        try:
            got = theme.probe_background(slave, timeout=0.3)
        finally:
            elapsed = time.monotonic() - start
            thread.join(1)
            os.close(slave)
            os.close(master)
        return got, elapsed, asked

    def test_probe_reads_the_osc11_answer(self):
        got, elapsed, asked = self.answer(b"\033]11;rgb:ffff/ffff/ffff\033\\\033[?62;22c")
        self.assertEqual(got, "#FFFFFF")
        self.assertIn(b"\033]11;?", asked[0])
        self.assertLess(elapsed, 0.3)

    def test_probe_stops_at_the_da1_answer(self):
        got, elapsed, _asked = self.answer(b"\033[?1;2c")
        self.assertIsNone(got)
        self.assertLess(elapsed, 0.3)

    def test_probe_gives_up_on_a_silent_terminal(self):
        got, elapsed, _asked = self.answer(b"")
        self.assertIsNone(got)
        self.assertLess(elapsed, 0.6)

    def test_herdr_default_dark_is_replaced_when_the_real_background_arrives(self):
        fold = theme.fold_sample
        color, settled = fold(None, "#282C34", 1)
        self.assertEqual((color, settled), ("#282C34", False))
        color, settled = fold(color, "#282C34", 4)
        self.assertFalse(settled)
        color, settled = fold(color, "#FFFFFF", 5)
        self.assertEqual((color, settled), ("#FFFFFF", True))
        # Five default-dark samples, and it keeps that dark.
        color, settled = "#282C34", False
        for attempt in range(1, 6):
            color, settled = fold(color, "#282C34", attempt)
        self.assertEqual((color, settled), ("#282C34", True))
        # A resize keeps the real light color through one stale default-dark reply.
        held, settled = fold("#FFFFFF", "#282C34", 1, follow=True)
        self.assertEqual((held, settled), ("#FFFFFF", False))
        held, settled = fold("#FFFFFF", "#111111", 1, follow=True)
        self.assertEqual((held, settled), ("#111111", True))
        # No answer does not invent the default dark.
        self.assertEqual(fold("#FFFFFF", None, 1, follow=True), ("#FFFFFF", False))

    def test_herdr_reprobes_after_the_first_paint(self):
        os.environ["HERDR_ENV"] = "1"
        calls = []

        def fake(fd=None, timeout=0.2):
            calls.append(timeout)
            return "#282C34" if len(calls) == 1 else "#FFFFFF"

        original = theme.probe_background
        theme.probe_background = fake
        try:
            started = time.monotonic()
            theme.detect_theme()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(calls, [0.2])
            self.assertTrue(theme.dark_terminal())
            self.assertTrue(theme.THEME["watch"])
            theme.THEME["due"] = 0
            applied = []
            self.assertTrue(theme.maybe_retheme(lambda: applied.append("theme")))
            self.assertEqual(applied, ["theme"])
            self.assertEqual(theme.PROBED["bg"], "#FFFFFF")
            self.assertFalse(theme.dark_terminal())
            self.assertFalse(theme.THEME["watch"])
        finally:
            theme.probe_background = original

    def test_placeholder_dark_is_rechecked_without_herdr_env(self):
        self.assertFalse(herdr.herdr_session())
        calls = []

        def fake(fd=None, timeout=0.2):
            calls.append(timeout)
            return "#282C34" if len(calls) == 1 else "#FFFFFF"

        original = theme.probe_background
        theme.probe_background = fake
        try:
            theme.detect_theme()
            self.assertEqual(calls, [0.2])
            self.assertTrue(theme.THEME["watch"])
            self.assertEqual(theme.THEME["tries"], 1)
            self.assertEqual((theme.THEME_LIMIT, theme.THEME_GAP), (5, 0.5))
            theme.THEME["due"] = 0
            self.assertTrue(theme.maybe_retheme(lambda: None))
            self.assertEqual(theme.PROBED["bg"], "#FFFFFF")
            self.assertFalse(theme.dark_terminal())
            self.assertFalse(theme.THEME["watch"])
        finally:
            theme.probe_background = original

    def test_a_real_dark_reply_is_final(self):
        calls = []

        def fake(fd=None, timeout=0.2):
            calls.append(timeout)
            return "#1E1E2E"

        original = theme.probe_background
        theme.probe_background = fake
        try:
            theme.detect_theme()
            self.assertEqual(calls, [0.2])
            self.assertFalse(theme.THEME["watch"])
            self.assertFalse(theme.maybe_retheme(lambda: self.fail("rethemed")))
            self.assertEqual(len(calls), 1)
            self.assertTrue(theme.dark_terminal())
        finally:
            theme.probe_background = original

    def test_desktop_hc_vars_count_as_herdr(self):
        self.assertFalse(herdr.herdr_session())
        self.assertEqual(herdr.herdr_cli(), ("", ""))
        for key in herdr.DESKTOP_MARKS:
            os.environ[key] = "desktop"
            self.assertTrue(herdr.herdr_session(), key)
            # No pane id, so the CLI stays quiet. OSC 7501 still reports.
            self.assertEqual(herdr.herdr_cli(), ("", ""))
            theme.PROBED["bg"] = "#FFFFFF"
            theme.THEME["watch"] = False
            theme.note_appearance()
            self.assertTrue(theme.THEME["watch"], key)
            os.environ.pop(key, None)
            theme.THEME["watch"] = False
        self.assertFalse(herdr.herdr_session())
        theme.note_appearance()
        self.assertFalse(theme.THEME["watch"])
        os.environ["HERDR_BIN_PATH"] = "/usr/bin/herdr"
        os.environ["HERDR_PANE_ID"] = "w1:p1"
        self.assertTrue(herdr.herdr_session())
        self.assertEqual(herdr.herdr_cli(), ("/usr/bin/herdr", "w1:p1"))

    def test_theme_override_wins_over_herdr_default_dark(self):
        os.environ["HERDR_ENV"] = "1"
        os.environ[theme.THEME_ENV] = "light"
        theme.PROBED["bg"] = "#282C34"
        theme.THEME.update(watch=True, tries=1, due=0, follow=False)
        original = theme.probe_background
        theme.probe_background = lambda *a, **k: self.fail("probed")
        try:
            theme.detect_theme()
            self.assertFalse(theme.THEME["watch"])
            self.assertFalse(theme.maybe_retheme(lambda: self.fail("rethemed")))
            theme.note_appearance()
            self.assertFalse(theme.dark_terminal())
        finally:
            theme.probe_background = original

    def test_env_override_skips_the_probe(self):
        os.environ[theme.THEME_ENV] = "light"
        theme.PROBED["bg"] = None
        original = theme.probe_background
        theme.probe_background = lambda *a, **k: self.fail("probed")
        try:
            theme.detect_theme()
        finally:
            theme.probe_background = original
        self.assertFalse(theme.dark_terminal())


class NoticeTests(unittest.TestCase):
    def test_notifications_use_the_bots_name(self):
        self.assertEqual(herdr.notice_name("Cloud"), "Cloud")
        self.assertEqual(herdr.notice_name("Chef"), "Chef")
        self.assertEqual(herdr.display_name("  Cloud\nbot "), "Cloud bot")
        self.assertEqual(herdr.notice_name("Cursor Cloud Session"), "Cursor-Cloud-Session")
        self.assertEqual(herdr.notice_name("Travel Agent"), "Travel-Agent")
        self.assertEqual(herdr.notice_name("Cursor\u00a0Cloud\u00a0Session"), "Cursor-Cloud-Session")
        self.assertEqual(herdr.notice_name("Travel\u2002Agent"), "Travel-Agent")
        self.assertEqual(herdr.notice_name("Cursor_Cloud_Session"), "Cursor_Cloud_Session")
        self.assertEqual(herdr.notice_name("Cursor.Cloud"), "Cursor.Cloud")
        self.assertNotIn(" ", herdr.notice_name("Cursor Cloud Session"))
        self.assertNotIn(":", herdr.notice_name("a:b"))
        self.assertEqual(herdr.notice_name("a:b"), "a-b")
        clipped = herdr.notice_name("C" * 40)
        self.assertLessEqual(len(clipped.encode()), 32)
        self.assertTrue(clipped.startswith("C"))
        wide = herdr.notice_name("Cursor Cloud Session Extra Words " * 3)
        self.assertLessEqual(len(wide.encode()), 32)
        self.assertNotIn(" ", wide)
        self.assertNotIn("\u00a0", wide)
        previous = herdr.APP
        try:
            herdr.bind_app("Cloud", "b3-cloud")
            self.assertEqual(herdr.APP, "Cloud")
            report = herdr.osc_report("blocked", "Cloud", "permission")
            self.assertIn("app=Cloud", report)
            self.assertNotIn("gbot-b3-cloud", report)
            self.assertLessEqual(len(herdr.APP.encode()), 32)
        finally:
            herdr.APP = previous
        command = herdr.report_command("herdr", "p1", "blocked", "Cloud", "s", 1, "Cloud")
        self.assertEqual(command[command.index("--agent") + 1], "Cloud")
        release = herdr.release_command("herdr", "p1", "Cloud", 2)
        self.assertEqual(release[release.index("--agent") + 1], "Cloud")
        spaced = herdr.report_command(
            "herdr", "p1", "blocked", "Cursor Cloud Session", "s", 1, "Cursor Cloud Session"
        )
        self.assertEqual(spaced[spaced.index("--agent") + 1], "Cursor-Cloud-Session")
        self.assertEqual(spaced[-2:], ["gbot-tui", "Cursor Cloud Session"])
        previous = herdr.APP
        try:
            herdr.bind_app("Cursor Cloud Session", "b3")
            self.assertEqual(herdr.APP, "Cursor-Cloud-Session")
            report = herdr.osc_report("blocked", "Cursor Cloud Session", "permission")
            app = report.split("app=", 1)[1].split(":", 1)[0]
            self.assertEqual(app, "Cursor-Cloud-Session")
        finally:
            herdr.APP = previous

    def test_a_bot_named_like_the_tui_gets_its_own_app(self):
        from PIL import Image

        self.assertEqual(herdr.notice_name("gbot tui"), "gbot-tui")
        self.assertEqual(herdr.app_name("gbot tui"), "gbot-tui-bot")
        self.assertNotEqual(herdr.app_name("gbot tui"), herdr.SOURCE)
        self.assertLessEqual(len(herdr.app_name("gbot tui").encode()), 32)
        self.assertNotIn(" ", herdr.app_name("gbot tui"))
        self.assertEqual(herdr.app_name("Outfitly"), "Outfitly")
        self.assertEqual(herdr.app_name("Cursor Cloud Session"), "Cursor-Cloud-Session")
        previous = herdr.APP
        try:
            herdr.bind_app("gbot tui", "b9")
            self.assertNotEqual(herdr.APP, "gbot-tui")
            app = herdr.APP
            report = herdr.osc_report("idle", "gbot tui")
            self.assertEqual(report.split("app=", 1)[1].split(":", 1)[0], app)
            herdr.bind_app("Outfitly", "b4")
            self.assertEqual(herdr.APP, "Outfitly")
        finally:
            herdr.APP = previous
        with tempfile.TemporaryDirectory() as tmp:
            Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(Path(tmp) / "bot-heart.png")
            Image.new("RGBA", (18, 18), (0, 0, 0, 255)).save(Path(tmp) / "bot-heart-face.png")
            made = icons.install_avatars(
                [
                    {"id": "b9", "name": "gbot tui", "avatarShape": "heart", "avatarColor": "red"},
                    {"id": "b4", "name": "Outfitly", "avatarShape": "heart", "avatarColor": "red"},
                ],
                root=tmp,
                src=tmp,
            )
            self.assertIn(app, made["gbot-b9"]["match"])
            self.assertNotIn("gbot-tui", made["gbot-b9"]["match"])
            self.assertEqual(made["gbot-b9"]["name"], "gbot tui")
            self.assertEqual(made["gbot-b4"]["match"], ["Outfitly", "gbot-b4"])

    def test_a_late_icon_does_not_replace_another_bots_name(self):
        previous_app = herdr.APP
        previous_install = icons.install_avatars_later

        def immediate(bots, done=None):
            if done:
                done({"gbot-b4-outfitly": {}})
            return None

        icons.install_avatars_later = immediate
        herdr.APP = "Chef"
        try:
            icons.show_avatar_later({"id": "b4-outfitly", "name": "Outfitly"})
            self.assertEqual(herdr.APP, "Chef")
            herdr.APP = "gbot-tui"
            icons.show_avatar_later({"id": "b4-outfitly", "name": "Outfitly"})
            self.assertEqual(herdr.APP, "Outfitly")
        finally:
            herdr.APP = previous_app
            icons.install_avatars_later = previous_install


class QuitTests(unittest.TestCase):
    def tearDown(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        app_mod.EXITED["done"] = False
        app_mod.TITLE.update(pushed=False, popped=False)

    def test_ctrl_c_quits_like_ctrl_d_and_sigint_is_restored(self):
        chat = chat_state.Chat("Researcher")
        self.assertEqual(chat_screen.chat_key(chat, "\x03", None), "quit")
        self.assertEqual(chat_screen.chat_key(chat, "\x04", None), "quit")
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        app_mod.enable_ctrl_c()
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertIs(signal.getsignal(signal.SIGTERM), app_mod._stop)
        master, slave = os.openpty()
        try:
            attr = termios.tcgetattr(slave)
            attr[3] |= termios.ISIG
            termios.tcsetattr(slave, termios.TCSANOW, attr)
            self.assertTrue(app_mod.accept_ctrl_c(slave))
            self.assertFalse(termios.tcgetattr(slave)[3] & termios.ISIG)
        finally:
            os.close(slave)
            os.close(master)

    def test_shutdown_restores_the_tab_and_pops_the_title_once(self):
        renames = []
        pops = []
        clears = []
        title = tabs.TabTitle(
            find=lambda: ({"labels": {"herdr.tab": "t2"}}, True),
            spaces=lambda: TabTests.SPACES,
            rename=lambda _info, name: renames.append(name),
        )
        saved = tabs.TAB, terminal.write_tty, herdr.emit, herdr.APP
        tabs.TAB = title
        terminal.write_tty = lambda data: pops.append(data)
        herdr.emit = lambda state, name, kind=None: clears.append(state)
        app_mod.EXITED["done"] = False
        app_mod.TITLE.update(pushed=True, popped=False)
        try:
            title.show("Chef")
            self.assertTrue(title.flush(2))
            app_mod.shutdown()
            app_mod.shutdown()
            self.assertEqual(herdr.APP, "gbot-tui")
        finally:
            tabs.TAB, terminal.write_tty, herdr.emit, herdr.APP = saved
            app_mod.EXITED["done"] = False
            app_mod.TITLE.update(pushed=False, popped=False)
        self.assertEqual(renames, ["Chef", "Work"])
        self.assertEqual(clears, ["clear"])
        self.assertEqual(pops, [terminal.POP_TITLE])


class TabTests(unittest.TestCase):
    SPACES = [
        {"id": "s1", "tabs": [{"id": "t1", "name": "1"}, {"id": "t2", "name": "Work"}]},
        {"id": "s2", "tabs": [{"id": "t3", "name": "Outfitly"}]},
    ]

    def tab(self, tab_id, alone=True):
        renames = []
        info = {"labels": {"herdr.tab": tab_id}}
        title = tabs.TabTitle(
            find=lambda: (info, alone),
            spaces=lambda: self.SPACES,
            rename=lambda _info, name: renames.append(name),
        )
        return title, renames

    def test_tab_name_from_ls(self):
        self.assertEqual(tabs.tab_name("t1", self.SPACES), "")
        self.assertEqual(tabs.tab_name("t2", self.SPACES), "Work")
        self.assertEqual(tabs.tab_name("t3", self.SPACES), "Outfitly")
        self.assertEqual(tabs.tab_name("gone", self.SPACES), "")

    def test_back_to_the_list_gives_the_tab_its_number_again(self):
        title, renames = self.tab("t1")
        title.show("Outfitly")
        title.restore()
        title.show("Researcher")
        title.restore()
        title.restore()
        self.assertTrue(title.flush(2))
        self.assertEqual(renames, ["Outfitly", "", "Researcher", ""])

    def test_a_named_tab_keeps_its_name(self):
        title, renames = self.tab("t2")
        title.show("Outfitly")
        title.restore()
        self.assertTrue(title.flush(2))
        self.assertEqual(renames, ["Outfitly", "Work"])

    def test_a_shared_tab_is_left_alone(self):
        title, renames = self.tab("t2", alone=False)
        title.show("Outfitly")
        title.restore()
        self.assertTrue(title.flush(2))
        self.assertEqual(renames, [])

    def test_restore_without_a_rename_does_nothing(self):
        title, renames = self.tab("t2")
        self.assertTrue(title.flush(0.1))
        title.restore()
        self.assertTrue(title.flush(2))
        self.assertEqual(renames, [])

    def test_leaving_a_chat_restores_the_tab_and_the_list_title(self):
        calls = []
        saved = tabs.TAB, herdr.emit, terminal.window_title, herdr.silence_herdr, herdr.APP
        title, renames = self.tab("t2")
        tabs.TAB = title
        herdr.APP = "Outfitly"
        herdr.emit = lambda state, name, kind=None: calls.append(("emit", state, herdr.APP, name))
        terminal.window_title = lambda text: calls.append(("title", text))
        herdr.silence_herdr = lambda label: None
        try:
            chat = chat_state.Chat("Outfitly")
            title.show("Outfitly")
            app_mod.leave_chat(chat, final=False)
            self.assertTrue(title.flush(2))
        finally:
            tabs.TAB, herdr.emit, terminal.window_title, herdr.silence_herdr, herdr.APP = saved
        self.assertEqual(renames, ["Outfitly", "Work"])
        self.assertEqual(calls[0], ("emit", "clear", "Outfitly", "Outfitly"))
        self.assertIn(("emit", "idle", "gbot-tui", "gbot"), calls)
        self.assertIn(("title", "gbot"), calls)


class HerdrTests(unittest.TestCase):
    def test_osc_carries_state_and_title(self):
        report = herdr.osc_report("working", "Researcher")
        self.assertIn("state=working", report)
        self.assertIn("app=gbot-tui", report)
        encoded = base64.b64encode(b"Researcher").decode()
        self.assertIn(f"title={encoded}", report)
        self.assertIn("\033]0;Researcher\033\\", report)

    def test_resume_rejects_quotes(self):
        self.assertIsNone(herdr.resume_arg("it's"))
        self.assertEqual(herdr.resume_arg("Researcher"), "Researcher")

    def test_rename_only_when_the_tab_has_one_terminal(self):
        mine = {"state": "live", "labels": {"herdr.tab": "tb1"}}
        other = {"state": "live", "labels": {"herdr.tab": "tb1"}}
        self.assertTrue(tabs.alone_in_tab(mine, [mine]))
        self.assertFalse(tabs.alone_in_tab(mine, [mine, other]))

    def test_report_command(self):
        command = herdr.report_command(
            "herdr", "p1", "done", "Researcher", "a1", 5, "Researcher"
        )
        self.assertEqual(command[command.index("--state") + 1], "idle")
        self.assertEqual(command[-2:], ["gbot-tui", "Researcher"])


def _reset_overview():
    activity.ACTIVITY.clear()
    activity.SEEN.clear()
    activity.ACTIVITY_LOADED["done"] = False
    activity.SEEN_LOADED["done"] = False
    paths = [
        activity.activity_path(),
        activity.seen_path(),
        cache.thread_cache_path("Chef"),
        cache.thread_cache_path("Researcher"),
        cache.thread_cache_path("Night"),
    ]
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _widget(prompt, options, **extra):
    entry = {
        "kind": "send-message",
        "id": extra.pop("id", "w1"),
        "message": {
            "type": "widget",
            "widget": {"prompt": prompt, "options": options},
        },
    }
    entry.update(extra)
    return entry


class WidgetTests(unittest.TestCase):
    def _chat(self, entry):
        chat = chat_state.Chat("Researcher")
        chat.loaded = True
        chat.session_id = "agent"
        chat.messages = [entry]
        return chat

    def test_an_open_widget_shows_the_prompt_and_numbered_options(self):
        entry = _widget(
            "How should I get into your Vivino?",
            [
                {"label": "Sign in and request the export", "value": "Sign in and export", "style": "primary"},
                {"label": "Just sign in and browse", "value": "Just browse"},
            ],
        )
        text = transcript.entry_text(entry)
        self.assertIn("How should I get into your Vivino?", text)
        self.assertIn("1  Sign in and request the export", text)
        self.assertIn("2  Just sign in and browse", text)
        rows = [line for kind, line in grid.transcript_rows([entry], 80) if kind == "text"]
        self.assertIn("1  Sign in and request the export", rows)
        older = _widget("Older?", [{"label": "Old", "value": "old"}], id="old")
        reply = {"id": "u", "role": "user", "text": "later"}
        both = [line for _kind, line in grid.transcript_rows([older, reply, entry], 80)]
        self.assertIn("   Old", both)
        self.assertNotIn("1  Old", both)
        self.assertIn("1  Sign in and request the export", both)

    def test_an_answered_widget_marks_the_chosen_option(self):
        entry = _widget(
            "How should I get into your Vivino?",
            [
                {"label": "Sign in and request the export", "value": "Sign in and export"},
                {"label": "Just sign in and browse", "value": "Just browse"},
            ],
            respondedValue="Just browse",
        )
        text = transcript.entry_text(entry)
        self.assertIn("●  Just sign in and browse", text)
        self.assertNotIn("\n1  ", text)
        self.assertNotIn("\n2  ", text)

    def test_a_skipped_widget_says_so(self):
        entry = _widget("Install it?", [{"label": "pstack", "value": "install pstack"}], widgetSkipped=True)
        self.assertIn("Install it?  · skipped", transcript.entry_text(entry))
        self.assertNotIn("\n1  ", transcript.entry_text(entry))

    def test_a_number_sends_that_option(self):
        entry = _widget(
            "Send these fixes?",
            [
                {"label": "Badge bug and polish", "value": "Send the badge bug"},
                {"label": "Leave it", "value": "Leave it for now"},
            ],
        )
        chat = self._chat(entry)
        job = chat_screen.chat_key(chat, "1", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "Send the badge bug")))
        self.assertIn("●  Badge bug and polish", transcript.entry_text(chat.messages[0]))
        self.assertEqual(chat.phase, "working")
        chat.fail_choice()
        self.assertIn("1  Badge bug and polish", transcript.entry_text(chat.messages[0]))
        self.assertEqual(chat.phase, "idle")
        self.assertIsNone(chat_screen.chat_key(chat, "0", None))
        self.assertEqual(chat.typed, "")

    def test_a_number_is_text_when_nothing_is_open(self):
        chat = chat_state.Chat("Researcher")
        chat.loaded = True
        chat.session_id = "agent"
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "1")

    def test_a_number_waits_until_the_thread_is_live(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "Yes, send them"}])
        chat = self._chat(entry)
        chat.loaded = False
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "")
        self.assertIsNone(transcript.latest_decision(chat.messages))

    def test_multi_select_toggles_and_enter_sends_each_line(self):
        entry = _widget(
            "Was soll raus?",
            [
                {"label": "Emil", "value": "Send Emil"},
                {"label": "Mario", "value": "Send Mario"},
            ],
        )
        entry["message"]["widget"]["multiSelect"] = True
        chat = self._chat(entry)
        self.assertIsNone(chat_screen.chat_key(chat, "2", None))
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        shown = transcript.entry_text(entry, chat.widget_picks["w1"])
        self.assertIn("● 1  Emil", shown)
        self.assertIn("● 2  Mario", shown)
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job[0], "choice")
        self.assertEqual(job[1][-1], "Send Emil\nSend Mario")

    def test_widget_error_reads_a_refusal(self):
        self.assertEqual(gbot_cli.widget_error({"ok": True, "text": ""}), "")
        self.assertEqual(gbot_cli.widget_error({"ok": True, "text": '{"accepted": true}'}), "")
        self.assertEqual(
            gbot_cli.widget_error({"ok": True, "text": '{"accepted": false, "refusal": "gone"}'}),
            "gone",
        )
        self.assertEqual(gbot_cli.widget_error({"ok": False, "text": "nope"}), "nope")

    def test_the_footer_names_the_keys(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        hint = widgets.choice_hint(entry)
        self.assertEqual(hint, "1-2 answer")
        footer = grid.footer_spans("idle", "", choice=hint)[0][0]
        self.assertIn("1-2 answer", footer)
        self.assertIn("space then digit types", footer)
        self.assertEqual(footer.count("enter"), 1)

    def test_a_widget_behind_your_reply_does_not_take_digits(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        chat = self._chat(entry)
        chat.messages.append({"id": "u", "role": "user", "text": "later"})
        self.assertIsNone(widgets.latest_widget(chat.messages))
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "1")
        self.assertEqual(chat.phase, "idle")
        shown = [line for kind, line in grid.transcript_rows(chat.messages, 80) if kind == "text"]
        self.assertIn("   Yes", shown)
        self.assertIn("   No", shown)
        self.assertNotIn("1  Yes", shown)
        self.assertNotIn("2  No", shown)

    def test_a_space_then_a_digit_types_it(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        chat = self._chat(entry)
        self.assertIsNone(chat_screen.chat_key(chat, " ", None))
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, " 1")
        self.assertEqual(chat.phase, "idle")
        self.assertFalse(widgets.widget_answered(entry))

    def test_options_past_nine_take_two_digits_and_letters_type(self):
        options = [{"label": f"L{i}", "value": f"v{i}"} for i in range(1, 12)]
        entry = _widget("Which?", options)
        text = transcript.entry_text(entry)
        self.assertIn("09  L9", text)
        self.assertIn("10  L10", text)
        self.assertIn("11  L11", text)
        self.assertNotIn("a  L10", text)
        self.assertEqual(widgets.choice_hint(entry), "01-11 two digits answer")
        chat = self._chat(entry)
        self.assertIsNone(chat_screen.chat_key(chat, "a", None))
        self.assertEqual(chat.typed, "a")
        self.assertEqual(chat.phase, "idle")
        self.assertFalse(widgets.widget_answered(entry))
        chat.typed = ""
        self.assertIsNone(chat_screen.chat_key(chat, "0", None))
        self.assertEqual(chat.typed, "")
        self.assertIsNone(chat_screen.chat_key(chat, "\x7f", None))
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "")
        job = chat_screen.chat_key(chat, "0", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "v10")))
        self.assertEqual(chat.messages[0].get("respondedValue"), "v10")
        chat.fail_choice()
        self.assertIn("10  L10", transcript.entry_text(chat.messages[0]))
        # A letter cancels a half-typed number and is typed, not answered.
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertIsNone(chat_screen.chat_key(chat, "b", None))
        self.assertEqual(chat.typed, "b")
        self.assertFalse(widgets.widget_answered(chat.messages[0]))
        chat.typed = ""
        # After the window, a lone digit selects that option.
        times = {"now": 20.0}
        original = activity.clock
        activity.clock = lambda: times["now"]
        try:
            self.assertIsNone(chat_screen.chat_key(chat, "2", None))
            self.assertIsNone(chat_screen.flush_choice_digit(chat))
            times["now"] = 20.0 + widgets.DIGIT_WINDOW + 0.01
            flushed = chat_screen.flush_choice_digit(chat)
            self.assertEqual(flushed, ("choice", ("Researcher", "agent", "w1", "v2")))
        finally:
            activity.clock = original

    def test_steps_are_answered_one_at_a_time(self):
        entry = {
            "kind": "send-message",
            "id": "w1",
            "message": {
                "type": "widget",
                "widget": {
                    "steps": [
                        {
                            "prompt": "City?",
                            "options": [
                                {"label": "Berlin", "value": "Berlin"},
                                {"label": "Paris", "value": "Paris"},
                            ],
                        },
                        {
                            "prompt": "Days?",
                            "multiSelect": True,
                            "options": [
                                {"label": "One", "value": "1"},
                                {"label": "Two", "value": "2"},
                            ],
                        },
                    ]
                },
            },
        }
        text = transcript.entry_text(entry)
        self.assertIn("City?", text)
        self.assertIn("1  Berlin", text)
        self.assertIn("Days?", text)
        self.assertNotIn("1  One", text)
        chat = self._chat(entry)
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.phase, "idle")
        shown = self._shown(chat)
        self.assertIn("question 2/2", shown[0])
        self.assertIn("Days?", shown)
        self.assertNotIn("City?", shown)
        self.assertIn("  1  One", shown)
        self.assertIsNone(chat_screen.chat_key(chat, "2", None))
        self.assertIn("● 2  Two", self._shown(chat))
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job[0], "choice")
        self.assertEqual(job[1][-1], "Berlin\n\n2")
        self.assertEqual(chat.messages[0].get("respondedValue"), "Berlin\n\n2")
        chat.fail_choice()
        self.assertFalse(widgets.widget_answered(chat.messages[0]))
        self.assertEqual(chat.phase, "idle")
        # The answers survive a failed send, on the last question.
        restored = self._shown(chat)
        self.assertIn("question 2/2", restored[0])
        self.assertIn("● 2  Two", restored)
        self.assertIsNone(chat_screen.chat_key(chat, "[", None))
        self.assertIn("● 1  Berlin", self._shown(chat))

    def _shown(self, chat, width=80):
        return [line for kind, line in grid.transcript_rows(chat.messages, width, chat.widget_picks, chat.widget_card) if kind == "text"]

    def _steps_entry(self, **extra):
        steps = [
            {"prompt": "City?", "options": [{"label": "Berlin", "value": "Berlin"}, {"label": "Paris", "value": "Paris"}]},
            {"prompt": "Hotel?", "allowCustom": True, "options": [{"label": "Ritz", "value": "Ritz"}]},
            {"prompt": "Days?", "multiSelect": True, "options": [{"label": "One", "value": "1"}, {"label": "Two", "value": "2"}]},
        ]
        entry = {"kind": "send-message", "id": "w1", "message": {"type": "widget", "widget": {"steps": steps}}}
        entry.update(extra)
        return entry

    def _row(self):
        asks = [
            ("City?", [("Berlin", "Berlin"), ("Paris", "Paris")], False),
            ("Hotel?", [("Ritz", "Ritz")], True),
            ("Day?", [("Mon", "Monday"), ("Fri", "Friday")], False),
        ]
        out = []
        for n, (prompt, options, custom) in enumerate(asks, 1):
            entry = _widget(prompt, [{"label": label, "value": value} for label, value in options], id=f"r{n}")
            if custom:
                entry["message"]["widget"]["allowCustom"] = True
            out.append(entry)
        return out

    def test_a_paged_card_shows_one_question_and_pages_back_and_forth(self):
        chat = self._chat(self._steps_entry())
        first = self._shown(chat)
        self.assertIn("question 1/3", first[0])
        self.assertEqual(first[1:], ["City?", "  1  Berlin", "  2  Paris"])
        self.assertEqual(widgets.page_hint(chat.page()), "1-2 answer")
        self.assertIsNone(chat_screen.chat_key(chat, "2", None))
        self.assertIn("question 2/3", self._shown(chat)[0])
        self.assertNotIn("City?", self._shown(chat))
        # Right and `]` page forward without an answer, Left and `[` back, and answers stay.
        self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_RIGHT, None))
        self.assertIn("question 3/3", self._shown(chat)[0])
        self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_RIGHT, None))
        self.assertIn("question 3/3", self._shown(chat)[0])
        self.assertIsNone(chat_screen.chat_key(chat, "[", None))
        self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_LEFT, None))
        back = self._shown(chat)
        self.assertIn("question 1/3", back[0])
        self.assertIn("● 2  Paris", back)
        self.assertEqual(chat.typed, "")
        self.assertIsNone(chat_screen.chat_key(chat, "]", None))
        self.assertIn("question 2/3", self._shown(chat)[0])
        self.assertEqual(chat.phase, "idle")

    def test_a_paged_card_sends_once_with_every_answer(self):
        chat = self._chat(self._steps_entry())
        chat_screen.chat_key(chat, "1", None)
        chat.typed = "Grand"
        self.assertIsNone(chat_screen.chat_key(chat, "\r", None))
        self.assertEqual(chat.typed, "")
        self.assertIn("question 3/3", self._shown(chat)[0])
        # Toggling a multi-select, paging away and back keeps the toggle.
        chat_screen.chat_key(chat, "2", None)
        chat_screen.chat_key(chat, "[", None)
        chat_screen.chat_key(chat, "]", None)
        self.assertIn("● 2  Two", self._shown(chat))
        self.assertIn("  1  One", self._shown(chat))
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "Berlin\n\nGrand\n\n2")))
        self.assertEqual(chat.messages[0]["respondedValue"], "Berlin\n\nGrand\n\n2")
        shown = self._shown(chat)
        self.assertFalse(any(line.startswith("question") for line in shown))
        self.assertFalse(any(line.lstrip().startswith(("1  ", "2  ")) for line in shown), shown)

    def test_a_paged_card_waits_until_every_question_has_an_answer(self):
        chat = self._chat(self._steps_entry())
        chat_screen.chat_key(chat, curses.KEY_RIGHT, None)
        chat_screen.chat_key(chat, curses.KEY_RIGHT, None)
        chat_screen.chat_key(chat, "1", None)
        chat_screen.chat_key(chat, "2", None)
        self.assertIsNone(chat_screen.chat_key(chat, "\r", None))
        # The last question is answered, so it goes to the first one still open.
        self.assertIn("question 1/3", self._shown(chat)[0])
        self.assertEqual(chat.phase, "idle")
        job = chat_screen.chat_key(chat, "2", None)
        self.assertIn("question 2/3", self._shown(chat)[0])
        self.assertIsNone(job)
        chat.typed = "Grand"
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job[1][-1], "Paris\n\nGrand\n\n1\n2")

    def test_widgets_in_a_row_are_one_paged_card(self):
        first, second, third = self._row()
        user = {"id": "u0", "role": "user", "text": "plan"}
        chat = self._chat(first)
        chat.messages = [user, first, second, third]
        self.assertEqual([page["prompt"] for page in widgets.pages(chat.messages)], ["City?", "Hotel?", "Day?"])
        shown = self._shown(chat)
        self.assertIn("question 1/3", shown[0])
        self.assertIn("City?", shown)
        self.assertNotIn("Hotel?", shown)
        self.assertNotIn("Day?", shown)
        chat_screen.chat_key(chat, "1", None)
        self.assertIn("Hotel?", self._shown(chat))
        chat.typed = "Grand"
        chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(chat.typed, "")
        self.assertIn("question 3/3", self._shown(chat)[0])
        job = chat_screen.chat_key(chat, "2", None)
        want = "City?: Berlin\n\nHotel?: Grand\n\nDay?: Friday"
        self.assertEqual(job, ("choice", ("Researcher", "agent", "r3", want)))
        self.assertEqual([entry.get("respondedValue") for entry in chat.messages[1:4]], ["Berlin", "Grand", "Friday"])
        self.assertEqual(widgets.pages(chat.messages), [])
        shown = self._shown(chat)
        self.assertFalse(any(line.startswith("question") for line in shown))
        self.assertFalse(any(line.lstrip().startswith(("1  ", "2  ")) for line in shown), shown)
        # A failed send gives every card back, answers kept, on the last question.
        chat.fail_choice()
        self.assertEqual(len(widgets.pages(chat.messages)), 3)
        self.assertFalse(any(widgets.widget_answered(entry) for entry in chat.messages[1:4]))
        self.assertIn("question 3/3", self._shown(chat)[0])
        chat_screen.chat_key(chat, "[", None)
        chat_screen.chat_key(chat, "[", None)
        self.assertIn("● 1  Berlin", self._shown(chat))

    def test_widgets_before_your_reply_are_not_paged(self):
        first, second, third = self._row()
        user = {"id": "u0", "role": "user", "text": "plan"}
        reply = {"id": "u1", "role": "user", "text": "later"}
        chat = self._chat(first)
        chat.messages = [first, second, user, third]
        self.assertEqual([page["prompt"] for page in widgets.pages(chat.messages)], ["Day?"])
        self.assertFalse(any(line.startswith("question") for line in self._shown(chat)))
        chat.messages = [first, second, third, reply]
        self.assertEqual(widgets.pages(chat.messages), [])
        self.assertFalse(any(line.startswith("question") for line in self._shown(chat)))

    def test_page_keys_are_text_or_cursor_keys_when_there_is_nothing_to_page(self):
        single = _widget("Send?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        chat = self._chat(single)
        self.assertIsNone(chat_screen.chat_key(chat, "[", None))
        self.assertEqual(chat.typed, "[")
        self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_LEFT, None))
        self.assertEqual(chat.pos, 0)
        self.assertNotIn("question", "\n".join(self._shown(chat)))
        # With a paged card open, text in the composer keeps the arrows and brackets.
        paged = self._chat(self._steps_entry())
        paged.typed = "ab"
        chat_screen.chat_key(paged, curses.KEY_LEFT, None)
        self.assertEqual(paged.pos, 1)
        chat_screen.chat_key(paged, "[", None)
        self.assertEqual(paged.typed, "a[b")
        self.assertIn("question 1/3", self._shown(paged)[0])

    def test_a_paged_card_does_not_page_while_the_thread_is_loading(self):
        chat = self._chat(self._steps_entry())
        chat.loaded = False
        chat_screen.chat_key(chat, curses.KEY_RIGHT, None)
        self.assertIn("question 1/3", self._shown(chat)[0])

    def test_a_step_without_a_way_to_answer_says_so(self):
        entry = {
            "kind": "send-message",
            "id": "w1",
            "message": {"type": "widget", "widget": {"steps": [{"prompt": "Use the app"}]}},
        }
        self.assertIn("answer in the app", transcript.entry_text(entry))
        self.assertFalse(widgets.widget_open(entry))
        chat = self._chat(entry)
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "1")

    def test_custom_text_answers_the_open_widget(self):
        entry = _widget("Name?", [{"label": "Ann", "value": "Ann"}])
        entry["message"]["widget"]["allowCustom"] = True
        chat = self._chat(entry)
        chat.typed = "Bea"
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "Bea")))
        self.assertEqual(chat.messages[0].get("respondedValue"), "Bea")
        self.assertEqual(chat.typed, "")
        plain = _widget("Name?", [{"label": "Ann", "value": "Ann"}], id="w2")
        other = self._chat(plain)
        other.typed = "Bea"
        sent = chat_screen.chat_key(other, "\r", None)
        self.assertEqual(sent, ("send", ("Researcher", "Bea")))
        self.assertFalse(widgets.widget_answered(plain))

    def test_inserting_the_reply_clears_the_physical_screen(self):
        class Screen(FakeScreen):
            def __init__(self):
                super().__init__(24, 80)
                self.full = 0

            def clearok(self, flag):
                if flag:
                    self.full += 1

        entry = _widget(
            "Send these?",
            [{"label": "Emil", "value": "Emil"}, {"label": "Mario", "value": "Mario"}],
        )
        entry["message"]["widget"]["multiSelect"] = True
        chat = self._chat(entry)
        screen = Screen()
        chat_view.draw(screen, chat)
        self.assertEqual(screen.full, 1)
        self.assertIsNone(chat_screen.chat_key(chat, "1", None))
        chat_view.draw(screen, chat)
        self.assertEqual(screen.full, 1)
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job[0], "choice")
        chat_view.draw(screen, chat)
        self.assertGreater(screen.full, 1)
        self.assertTrue(any("Emil" in text for row in screen.rows.values() for text in row))

    def test_the_footer_says_enter_once(self):
        entry = _widget(
            "Send these?",
            [{"label": "Emil", "value": "Emil"}, {"label": "Mario", "value": "Mario"}],
        )
        entry["message"]["widget"]["multiSelect"] = True
        footer = grid.footer_spans("idle", "", choice=widgets.choice_hint(entry))[0][0]
        self.assertIn("enter sends", footer)
        self.assertNotIn("enter send ·", footer)
        self.assertEqual(footer.count("enter"), 1)
        steps = {
            "kind": "send-message",
            "id": "w9",
            "message": {
                "type": "widget",
                "widget": {
                    "steps": [
                        {
                            "prompt": "Days?",
                            "multiSelect": True,
                            "options": [{"label": "One", "value": "1"}, {"label": "Two", "value": "2"}],
                        }
                    ]
                },
            },
        }
        step_footer = grid.footer_spans("idle", "", choice=widgets.choice_hint(steps))[0][0]
        self.assertIn("enter sends", step_footer)
        self.assertEqual(step_footer.count("enter"), 1)

    def test_widget_error_rejects_a_non_json_body(self):
        self.assertEqual(gbot_cli.widget_error({"ok": True, "text": "OK", "confirmed": False}), "choice was not confirmed")
        self.assertEqual(gbot_cli.widget_error({"ok": True, "text": "OK"}), "choice was not confirmed")

    def test_a_session_id_is_not_the_agent_id(self):
        self.assertEqual(gbot_cli.gateway_agent_id({"id": "agent-real"}), "agent-real")
        self.assertEqual(
            gbot_cli.gateway_agent_id({"id": "agent-real", "sessionId": "sess-9"}),
            "agent-real",
        )
        self.assertEqual(
            gbot_cli.gateway_agent_id({"id": "sess-9", "agentId": "agent-real", "sessionId": "sess-9"}),
            "agent-real",
        )
        self.assertEqual(gbot_cli.gateway_agent_id({"id": "sess-9", "sessionId": "sess-9"}), "")
        self.assertEqual(gbot_cli.gateway_agent_id({"agentId": "sess-9", "sessionId": "sess-9"}), "")
        page = transcript.parse_thread(
            {
                "target": {"id": "sess-9", "sessionId": "sess-9", "name": "Researcher"},
                "transcript": {"entries": [_widget("Go?", [{"label": "Yes", "value": "yes"}])]},
                "cursor": "w1",
            }
        )
        chat = chat_state.Chat("Researcher")
        chat.ingest(page, first=True)
        self.assertEqual(chat.agent_id, "")
        self.assertEqual(chat.viewer_session, "sess-9")
        job = chat_screen.chat_key(chat, "1", None)
        self.assertEqual(job[1][1], "")
        distinct = transcript.parse_thread(
            {
                "target": {"id": "agent-real", "sessionId": "sess-9", "name": "Researcher"},
                "transcript": {"entries": [_widget("Go?", [{"label": "Yes", "value": "yes"}])]},
                "cursor": "w1",
            }
        )
        live = chat_state.Chat("Researcher")
        live.ingest(distinct, first=True)
        picked = chat_screen.chat_key(live, "1", None)
        self.assertEqual(picked[1][1], "agent-real")


class _Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.hits.append(
            {
                "path": self.path,
                "type": self.headers.get("Content-Type"),
                "auth": self.headers.get("Authorization"),
                "body": body.decode("utf-8"),
            }
        )
        mode = self.server.mode
        if self.path.endswith("/respondGrokBotWidget"):
            if mode == "json":
                raw, code, ctype = b'{"accepted":true}', 200, "application/json"
            elif mode == "plain":
                raw, code, ctype = b"OK", 200, "text/plain"
            elif mode == "refuse":
                raw, code, ctype = b'{"accepted":false,"refusal":"gone"}', 200, "application/json"
            else:
                raw, code, ctype = b"no", 500, "text/plain"
        else:
            raw, code, ctype = b"{}", 200, "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


class WidgetHookTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            key: os.environ.get(key)
            for key in ("PATH", "GBOT_TUI_WIDGET_HOOK", "MOCK_GATEWAY", "MOCK_LOG", "NODE_OPTIONS")
        }
        self.tmp = tempfile.TemporaryDirectory()
        server = HTTPServer(("127.0.0.1", 0), _Gateway)
        server.hits = []
        server.mode = "json"
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        port = server.server_address[1]
        log = os.path.join(self.tmp.name, "calls.log")
        script = os.path.join(self.tmp.name, "gbot")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                """#!/usr/bin/env node
const fs = require("fs");
const args = process.argv.slice(2);
function record(line) {
  fs.appendFileSync(process.env.MOCK_LOG, line + "\\n");
}
const dash = args.indexOf("--");
if (args[0] === "send") {
  const rest = dash >= 0 ? args.slice(dash + 1) : args.slice(1);
  record("send " + rest.join("\\t"));
  process.stdout.write(JSON.stringify({ ok: true }) + "\\n");
  process.exit(0);
}
if (args[0] === "thread") {
  fetch(process.env.MOCK_GATEWAY + "/api/getAgentTranscriptTail", {
    method: "POST",
    headers: { authorization: "Bearer secret" },
    body: "{}",
  }).then(() => {
    record("thread");
    process.stdout.write(JSON.stringify({
      target: { id: "from-thread", name: args[args.length - 1] },
      transcript: { entries: [] },
      cursor: "",
    }) + "\\n");
  }).catch((err) => {
    process.stdout.write(JSON.stringify({ error: { message: String(err && err.message || err) } }) + "\\n");
    process.exit(1);
  });
} else {
  process.stdout.write(JSON.stringify({ ok: true }) + "\\n");
}
"""
            )
        os.chmod(script, 0o755)
        os.environ["PATH"] = self.tmp.name + os.pathsep + (os.environ.get("PATH") or "")
        os.environ["MOCK_GATEWAY"] = f"http://127.0.0.1:{port}"
        os.environ["MOCK_LOG"] = log
        os.environ["GBOT_TUI_WIDGET_HOOK"] = "1"
        self.log = log

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _log(self):
        try:
            with open(self.log, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    def _widget_hits(self):
        return [hit for hit in self.server.hits if hit["path"].endswith("/respondGrokBotWidget")]

    def test_the_hook_posts_json_and_a_non_json_success_falls_back_to_send(self):
        gbot_cli.respond_widget("Researcher", "agent-real", "e1", "Yes please")
        hits = self._widget_hits()
        self.assertEqual(len(hits), 1)
        self.assertIn("application/json", (hits[0]["type"] or "").lower())
        self.assertEqual(hits[0]["auth"], "Bearer secret")
        body = json.loads(hits[0]["body"])
        self.assertEqual(body["agentId"], "agent-real")
        self.assertEqual(body["entryId"], "e1")
        self.assertEqual(body["value"], "Yes please")
        self.assertNotIn("send ", self._log())

        self.server.hits.clear()
        open(self.log, "w", encoding="utf-8").close()
        self.server.mode = "plain"
        gbot_cli.respond_widget("Researcher", "agent-real", "e1", "Yes please")
        self.assertTrue(self._widget_hits())
        self.assertIn("send ", self._log())
        self.assertIn("Yes please", self._log())

        open(self.log, "w", encoding="utf-8").close()
        self.server.mode = "refuse"
        gbot_cli.respond_widget("Researcher", "agent-real", "e1", "Yes please")
        self.assertIn("Yes please", self._log())

        open(self.log, "w", encoding="utf-8").close()
        self.server.hits.clear()
        gbot_cli.respond_widget("Researcher", "", "e1", "typed")
        self.assertEqual(self._widget_hits(), [])
        self.assertIn("typed", self._log())
        self.assertNotIn("thread", self._log())

    def test_without_the_hook_the_value_is_only_a_message(self):
        os.environ.pop("GBOT_TUI_WIDGET_HOOK", None)
        gbot_cli.respond_widget("Researcher", "agent-real", "e1", "Just send")
        self.assertEqual(self._widget_hits(), [])
        self.assertIn("Just send", self._log())
        self.assertNotIn("thread", self._log())


class OverviewTests(unittest.TestCase):
    def setUp(self):
        _reset_overview()

    def tearDown(self):
        _reset_overview()

    def test_pending_card_needs_you_and_shows_the_action(self):
        entries = [
            {"id": "m1", "role": "assistant", "text": "ready"},
            {
                "id": "c1",
                "kind": "send-message",
                "message": {
                    "type": "auto-review-approval",
                    "approval": {"status": "pending", "command": "npm publish", "requestId": "ask"},
                },
            },
        ]
        snap = activity.judge_activity(entries, "c1", "m1")
        self.assertEqual(snap["state"], "blocked")
        self.assertEqual(snap["preview"], "npm publish")
        self.assertEqual(activity.status_label(snap["state"], snap["unread"]), "needs you")

    def test_a_user_line_with_no_reply_is_running(self):
        entries = [
            {"id": "m1", "role": "assistant", "text": "hi"},
            {"id": "m2", "role": "user", "text": "go"},
        ]
        snap = activity.judge_activity(entries, "m2", "m1")
        self.assertEqual(snap["state"], "working")
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(snap["preview"], "go")

    def test_a_reply_you_have_not_opened_is_new(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "done"},
            {"id": "m3", "role": "assistant", "text": "and more"},
        ]
        snap = activity.judge_activity(entries, "m3", "m1")
        self.assertEqual(snap["state"], "unread")
        self.assertEqual(snap["unread"], 2)
        self.assertEqual(snap["preview"], "and more")
        self.assertEqual(activity.status_label("unread", 2), "2 unread")
        self.assertEqual(activity.status_label("unread", 1), "unread")

    def test_thinking_is_not_a_new_reply(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "done"},
            {"id": "m3", "kind": "thinking", "text": "more private"},
        ]
        snap = activity.judge_activity(entries, "m3", "m1")
        self.assertEqual(snap["state"], "unread")
        self.assertEqual(snap["unread"], 1)
        self.assertEqual(snap["preview"], "done")
        waiting = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "kind": "thinking", "text": "private plan"},
        ]
        snap = activity.judge_activity(waiting, "m2", "m1")
        self.assertEqual(snap["state"], "working")
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(snap["preview"], "go")
        marked = [{"role": "assistant", "text": "<thinking>nope</thinking>\n\n**lunch** is ready"}]
        self.assertEqual(activity.activity_preview(marked, "idle"), "lunch is ready")

    def test_a_seen_reply_is_idle_and_a_first_look_is_not_all_new(self):
        entries = [{"id": "m2", "role": "assistant", "text": "done"}]
        self.assertEqual(activity.judge_activity(entries, "m2", "m2")["state"], "idle")
        first = activity.judge_activity(entries, "m2", "")
        self.assertEqual(first["state"], "idle")
        self.assertEqual(first["unread"], 0)

    def test_a_streaming_reply_you_have_open_stays_running(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "streaming": True},
        ]
        self.assertEqual(activity.judge_activity(entries, "m2", "m2")["state"], "working")
        # The same partial, not yet on screen, is a new message.
        self.assertEqual(activity.judge_activity(entries, "m2", "m1")["state"], "unread")

    def test_is_streaming_on_the_latest_entry_is_running(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "isStreaming": True},
        ]
        snap = activity.judge_activity(entries, "m2", "m2")
        self.assertEqual(snap["state"], "working")
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(activity.status_label(snap["state"], snap["unread"]), "working")
        # A partial you have not opened is still unread, not a second running row.
        self.assertEqual(activity.judge_activity(entries, "m2", "m1")["state"], "unread")
        finished = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "isStreaming": False},
        ]
        idle = activity.judge_activity(finished, "m2", "m2")
        self.assertEqual(idle["state"], "idle")
        text = activity.format_overview(
            [
                {"name": "Kit", "activity": snap},
                {"name": "Quiet", "activity": idle},
            ]
        )
        kit = next(line for line in text.splitlines() if "Kit" in line)
        quiet = next(line for line in text.splitlines() if "Quiet" in line)
        self.assertTrue(kit.startswith("running"))
        self.assertTrue(quiet.startswith("idle"))
        self.assertIn("1 running", text.splitlines()[0])

    def test_a_seen_id_older_than_this_tail_counts_the_tail(self):
        entries = [{"id": "m9", "role": "assistant", "text": "later"}]
        snap = activity.judge_activity(entries, "m9", "m1")
        self.assertEqual(snap["state"], "unread")

    def test_attention_sorts_above_idle_and_the_summary_counts_it(self):
        bots = [
            {"name": "Idle", "activity": {"state": "idle", "unread": 0, "latest": 10}},
            {"name": "Runner", "activity": {"state": "working", "unread": 0, "latest": 30}},
            {"name": "Fresh", "activity": {"state": "unread", "unread": 1, "latest": 20}},
            {"name": "Ask", "activity": {"state": "blocked", "unread": 0, "latest": 50}},
            {"name": "Wait"},
        ]
        self.assertEqual(
            [bot["name"] for bot in activity.order_bots(bots)],
            ["Ask", "Runner", "Fresh", "Idle", "Wait"],
        )
        self.assertEqual(
            activity.overview_summary(bots[:4]),
            "1 needs you · 1 unread · 1 working",
        )
        self.assertEqual(activity.overview_summary([], checked=0, total=4), "checking threads")
        self.assertEqual(
            activity.overview_summary(bots[:2], checked=1, total=4),
            "1 working · checking 1 of 4",
        )
        self.assertEqual(activity.overview_summary(bots[:1]), "1 idle")

    def test_the_row_shows_the_status_and_the_preview(self):
        bot = {
            "name": "Outfitly",
            "title": "wardrobe",
            "avatarColor": "violet",
            "activity": {"state": "blocked", "unread": 1, "preview": "npm publish", "cursor": "c"},
        }
        spans = picker.picker_spans(bot, True, 60)
        self.assertEqual(spans[0], ("→ ", "bold"))
        self.assertEqual(spans[1], ("● ", "bot:violet"))
        self.assertEqual(spans[2], ("needs you  ", "yellow"))
        self.assertEqual(spans[3], ("Outfitly", "bold"))
        self.assertEqual(spans[4], (" · ", "dim"))
        self.assertEqual(spans[5], ("npm publish", "dim"))
        self.assertLess(
            "".join(text for text, _style in spans).index("needs you"),
            "".join(text for text, _style in spans).index("Outfitly"),
        )
        narrow = "".join(text for text, _style in picker.picker_spans(bot, False, 16))
        self.assertIn("Outfitly", narrow)

    def test_the_selection_shows_every_state_in_one_column(self):
        bots = [
            {
                "name": "Ask",
                "title": "",
                "avatarColor": "violet",
                "activity": {"state": "blocked", "unread": 0, "preview": "npm publish", "cursor": "c"},
            },
            {
                "name": "Fresh",
                "title": "",
                "avatarColor": "blue",
                "activity": {"state": "unread", "unread": 3, "preview": "and more", "cursor": "c"},
            },
            {
                "name": "Runner",
                "title": "",
                "avatarColor": "orange",
                "activity": {"state": "working", "unread": 0, "preview": "go", "cursor": "c"},
            },
            {
                "name": "Quiet",
                "title": "",
                "avatarColor": "gray",
                "activity": {"state": "idle", "unread": 0, "preview": "night", "cursor": "c"},
            },
            {"name": "Scout", "title": "looks around", "avatarColor": "green"},
        ]
        columns = picker.list_columns(bots, 80)
        self.assertIsNotNone(columns)
        words = ["needs you", "3 unread", "working", "idle", "-"]
        starts = []
        for bot, word in zip(bots, words):
            spans = picker.picker_spans(bot, False, 80, columns)
            row = "".join(text for text, _style in spans)
            self.assertIn(word, spans[2][0])
            self.assertLess(row.index(word), row.index(bot["name"]))
            self.assertEqual(spans[-1][1], "dim")
            starts.append(sum(len(text) for text, _style in spans[:-1]))
        self.assertEqual(len(set(starts)), 1)
        styles = [
            picker.picker_spans(bot, False, 80, columns)[2][1]
            for bot in bots
        ]
        self.assertEqual(styles, ["yellow", "green", "cyan", "dim", "dim"])

    def test_a_saved_thread_shows_its_state_before_the_scan(self):
        cache.write_thread_cache("Chef", [{"id": "m1", "role": "user", "text": "cook"}])
        cache.write_thread_cache(
            "Researcher",
            [
                {"id": "m1", "role": "user", "text": "go"},
                {"id": "m2", "role": "assistant", "text": "the paper is out"},
            ],
        )
        activity.remember_seen("Researcher", "a1", "m1")
        activity.store_activity(
            {"name": "Night", "id": "a9"},
            {"state": "unread", "unread": 1, "cursor": "m9", "preview": "done"},
        )
        cache.write_thread_cache("Night", [{"id": "m1", "role": "assistant", "text": "old"}])
        views = activity.with_activity(
            [
                {"name": "Chef", "id": "a2", "title": "what to cook"},
                {"name": "Researcher", "id": "a1"},
                {"name": "Night", "id": "a9"},
                {"name": "Scout", "id": "a3", "title": "looks"},
            ]
        )
        by_name = {bot["name"]: bot for bot in views}
        self.assertEqual(by_name["Chef"]["activity"]["state"], "working")
        self.assertEqual(by_name["Researcher"]["activity"]["state"], "unread")
        self.assertEqual(by_name["Researcher"]["activity"]["preview"], "the paper is out")
        # A live state already on disk wins over the older saved thread.
        self.assertEqual(by_name["Night"]["activity"]["state"], "unread")
        self.assertEqual(by_name["Night"]["activity"]["preview"], "done")
        self.assertNotIn("activity", by_name["Scout"])
        running = "".join(text for text, _style in picker.picker_spans(by_name["Chef"], False, 70))
        self.assertIn("working", running)
        self.assertLess(running.index("working"), running.index("Chef"))
        unread = "".join(text for text, _style in picker.picker_spans(by_name["Researcher"], False, 70))
        self.assertIn("unread", unread)
        self.assertLess(unread.index("unread"), unread.index("Researcher"))

    def test_search_matches_the_latest_line(self):
        bots = [
            {"name": "Chef", "title": "", "activity": {"preview": "pasta is ready", "state": "unread"}},
            {"name": "Night", "title": ""},
        ]
        self.assertEqual(picker.fuzzy_bots("pasta", bots)[0]["name"], "Chef")

    def _as_another_instance(self, path, rows, bump=2):
        """Write the file directly, as a second gbot-tui process would, with a newer mtime."""
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        stat = os.stat(path)
        stamp = stat.st_mtime_ns + bump * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))

    def test_lookup_seen_picks_up_a_cursor_another_instance_wrote(self):
        bot = {"id": "a1", "name": "Chef"}
        activity.remember_seen("Chef", "a1", "m1")
        self.assertEqual(activity.lookup_seen(bot), "m1")
        with open(activity.seen_path(), encoding="utf-8") as handle:
            rows = json.load(handle)
        rows.update({"id:a1": "m7", "name:Chef": "m7"})
        self._as_another_instance(activity.seen_path(), rows)
        self.assertEqual(activity.lookup_seen(bot), "m7")

    def test_lookup_activity_picks_up_a_snapshot_another_instance_wrote(self):
        bot = {"id": "a1", "name": "Chef"}
        activity.store_activity(bot, {"state": "unread", "unread": 2, "cursor": "m5", "preview": "hi"})
        self.assertEqual(activity.lookup_activity(bot)["state"], "unread")
        cleared = {"state": "idle", "unread": 0, "cursor": "m5", "preview": "hi"}
        self._as_another_instance(activity.activity_path(), {"id:a1": cleared, "name:Chef": cleared})
        self.assertEqual(activity.lookup_activity(bot)["state"], "idle")
        self.assertEqual(activity.lookup_activity(bot)["unread"], 0)

    def test_an_unchanged_file_is_not_read_again(self):
        activity.remember_seen("Chef", "a1", "m1")
        calls = []
        real = cache.read_json
        cache.read_json = lambda path: calls.append(path) or real(path)
        try:
            for _ in range(5):
                activity.lookup_seen({"id": "a1", "name": "Chef"})
        finally:
            cache.read_json = real
        self.assertEqual(calls, [])

    def test_remember_seen_keeps_a_row_another_instance_wrote(self):
        activity.remember_seen("Chef", "a1", "m1")
        rows = {"id:a1": "m1", "name:Chef": "m1", "id:a2": "n4", "name:Night": "n4"}
        self._as_another_instance(activity.seen_path(), rows)
        activity.remember_seen("Chef", "a1", "m2")
        with open(activity.seen_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["id:a1"], "m2")
        self.assertEqual(saved["id:a2"], "n4")
        self.assertEqual(saved["name:Night"], "n4")
        self.assertEqual(activity.lookup_seen({"id": "a2", "name": "Night"}), "n4")

    def test_store_activity_keeps_a_row_another_instance_wrote(self):
        chef = {"id": "a1", "name": "Chef"}
        activity.store_activity(chef, {"state": "idle", "unread": 0, "cursor": "m1", "preview": ""})
        night = {"state": "unread", "unread": 1, "cursor": "n2", "preview": "new"}
        self._as_another_instance(
            activity.activity_path(),
            {
                "id:a1": {"state": "idle", "unread": 0, "cursor": "m1", "preview": ""},
                "id:a2": night,
            },
        )
        activity.store_activity(chef, {"state": "working", "unread": 0, "cursor": "m2", "preview": ""})
        with open(activity.activity_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["id:a1"]["state"], "working")
        self.assertEqual(saved["id:a2"]["state"], "unread")

    def test_only_if_absent_is_judged_against_the_reloaded_file(self):
        bot = {"id": "a1", "name": "Chef"}
        activity.remember_seen("Other", "a9", "x1")
        self._as_another_instance(activity.seen_path(), {"id:a1": "m9", "name:Chef": "m9", "id:a9": "x1"})
        activity.remember_seen("Chef", "a1", "m1", only_if_absent=True)
        self.assertEqual(activity.lookup_seen(bot), "m9")
        with open(activity.seen_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["id:a1"], "m9")
        snap = {"state": "idle", "unread": 0, "cursor": "m1", "preview": ""}
        activity.store_activity({"id": "a5", "name": "Zed"}, snap)
        other = {"state": "blocked", "unread": 0, "cursor": "z3", "preview": "ask"}
        self._as_another_instance(activity.activity_path(), {"id:a5": other, "name:Zed": other})
        activity.store_activity({"id": "a5", "name": "Zed"}, snap, only_if_absent=True)
        self.assertEqual(activity.lookup_activity({"id": "a5", "name": "Zed"})["state"], "blocked")

    def test_write_private_replaces_atomically_and_stays_private(self):
        path = activity.seen_path()
        cache.write_private(path, {"a": "1"})
        first = os.stat(path).st_ino
        cache.write_private(path, {"a": "2"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotEqual(os.stat(path).st_ino, first)
        self.assertEqual(cache.read_json(path), {"a": "2"})
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], [])

    def test_seen_is_private_and_the_cached_thread_counts_as_read(self):
        activity.remember_seen("Outfitly", "a1", "m3")
        self.assertEqual(os.stat(activity.seen_path()).st_mode & 0o777, 0o600)
        activity.SEEN.clear()
        activity.SEEN_LOADED["done"] = False
        self.assertEqual(activity.lookup_seen({"id": "a1", "name": "Outfitly"}), "m3")
        cache.write_thread_cache("Chef", [{"id": "m4", "role": "assistant", "text": "old"}])
        self.assertEqual(activity.seen_mark({"name": "Chef"}), "m4")
        activity.store_activity(
            {"name": "Nomad", "id": "a9"},
            {"state": "idle", "unread": 0, "cursor": "m1", "preview": ""},
        )
        # A scan stores this cursor in the activity file. It is not a seen mark:
        # the next pass would otherwise treat it as already read.
        self.assertEqual(activity.seen_mark({"name": "Nomad", "id": "a9"}), "")
        activity.remember_seen("Nomad", "a9", "m9")
        self.assertEqual(activity.seen_mark({"name": "Nomad", "id": "a9"}), "m9")
        self.assertEqual(os.stat(activity.activity_path()).st_mode & 0o777, 0o600)

    def test_a_later_pass_marks_a_reply_you_have_not_opened(self):
        bots = [{"id": "a1", "name": "Chef", "title": "what to cook"}]
        first = {"entries": [{"id": "m1", "role": "assistant", "text": "hello"}], "cursor": "m1"}
        views, error = activity.build_overview(bots, lambda bot: first)
        self.assertIsNone(error)
        self.assertEqual(views[0]["activity"]["state"], "idle")
        second = {
            "entries": [
                {"id": "m1", "role": "assistant", "text": "hello"},
                {"id": "m2", "role": "assistant", "text": "lunch is ready"},
            ],
            "cursor": "m2",
        }
        views, error = activity.build_overview(bots, lambda bot: second)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(views[0]["activity"]["preview"], "lunch is ready")
        text = activity.format_overview(views)
        self.assertIn("1 unread", text)
        self.assertIn("Chef", text)
        self.assertIn("lunch is ready", text)
        self.assertEqual(activity.lookup_seen(bots[0]), "m1")

    def test_a_never_opened_bot_stays_new_across_scans(self):
        bots = [{"id": "a1", "name": "Researcher"}]

        def page(*rows):
            entries = [{"id": mid, "role": "assistant", "text": text} for mid, text in rows]
            return {"entries": entries, "cursor": rows[-1][0]}

        first = page(("m1", "hello"))
        views, error = activity.build_overview(bots, lambda bot: first)
        self.assertIsNone(error)
        self.assertEqual(views[0]["activity"]["state"], "idle")
        self.assertEqual(activity.lookup_seen(bots[0]), "m1")

        second = page(("m1", "hello"), ("m2", "done"))
        views, error = activity.build_overview(bots, lambda bot: second)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(views[0]["activity"]["preview"], "done")
        self.assertEqual(activity.lookup_seen(bots[0]), "m1")

        third = page(("m1", "hello"), ("m2", "done"), ("m3", "still here"))
        views, error = activity.build_overview(bots, lambda bot: third)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(views[0]["activity"]["unread"], 2)
        self.assertEqual(views[0]["activity"]["preview"], "still here")
        self.assertEqual(activity.lookup_seen(bots[0]), "m1")

        # A fresh process reads the pinned cursor. Another scan does not move it.
        activity.SEEN.clear()
        activity.SEEN_LOADED["done"] = False
        activity.ACTIVITY.clear()
        activity.ACTIVITY_LOADED["done"] = False
        views, error = activity.build_overview(bots, lambda bot: third)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(activity.lookup_seen(bots[0]), "m1")

        activity.remember_seen("Researcher", "a1", "m3")
        activity.remember_seen("Researcher", "a1", "m1", only_if_absent=True)
        self.assertEqual(activity.lookup_seen(bots[0]), "m3")
        views, error = activity.build_overview(bots, lambda bot: third)
        self.assertEqual(views[0]["activity"]["state"], "idle")
        self.assertEqual(activity.lookup_seen(bots[0]), "m3")

    def test_opening_a_chat_clears_unread_when_the_thread_names_another_id(self):
        # The list knows the bot as a1. The thread names its own id (a session).
        bots = [{"id": "a1", "name": "Researcher"}]
        first = {"entries": [{"id": "m1", "role": "assistant", "text": "hello"}], "cursor": "m1"}
        activity.build_overview(bots, lambda bot: first)
        second = {
            "entries": [
                {"id": "m1", "role": "assistant", "text": "hello"},
                {"id": "m2", "role": "assistant", "text": "the paper is out"},
            ],
            "cursor": "m2",
        }
        views, _error = activity.build_overview(bots, lambda bot: second)
        self.assertEqual(views[0]["activity"]["state"], "unread")

        page = transcript.parse_thread(
            {
                "target": {"id": "sess-1", "sessionId": "sess-1", "name": "Researcher"},
                "transcript": {"entries": second["entries"]},
                "cursor": "m2",
            }
        )
        roster.ROSTER["Researcher"] = bots[0]
        try:
            chat = chat_state.Chat("Researcher")
            chat.ingest(page, first=True)
        finally:
            roster.ROSTER.pop("Researcher", None)
        # Back on the list: the saved state is what is drawn until the next scan.
        row = activity.with_activity(bots)[0]["activity"]
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["unread"], 0)
        self.assertEqual(activity.lookup_seen(bots[0]), "m2")
        self.assertEqual(activity.overview_summary(activity.with_activity(bots)), "1 idle")
        # The next scan reads the same cursor and agrees.
        views, _error = activity.build_overview(bots, lambda bot: second)
        self.assertEqual(views[0]["activity"]["state"], "idle")

    def test_mark_read_moves_the_cursor_clears_unread_and_keeps_the_state(self):
        calls = []
        real = backend.mark_read_later
        backend.mark_read_later = calls.append
        try:
            bot = {"id": "a1", "name": "Researcher"}
            # No cursor or activity yet: nothing to do.
            self.assertFalse(activity.mark_read(bot))
            self.assertEqual(calls, [])
            self.assertEqual(activity.lookup_seen(bot), "")
            self.assertIsNone(activity.lookup_activity(bot))

            activity.remember_seen("Researcher", "a1", "m1")
            activity.store_activity(
                bot, {"state": "unread", "unread": 3, "cursor": "m4", "preview": "out", "latest": 7}
            )
            view = activity.with_activity([bot])[0]
            self.assertEqual(view["activity"]["unread"], 3)
            self.assertTrue(activity.mark_read(view))
            self.assertEqual(calls, ["a1"])
            self.assertEqual(activity.lookup_seen(bot), "m4")
            snap = activity.lookup_activity(bot)
            self.assertEqual(snap["unread"], 0)
            self.assertEqual(snap["state"], "idle")
            self.assertEqual((snap["cursor"], snap["preview"], snap["latest"]), ("m4", "out", 7))
            self.assertEqual(activity.status_label(snap["state"], snap["unread"]), "idle")

            # needs you and working stay as they are; only the count clears.
            for state in ("blocked", "working"):
                activity.store_activity(bot, {"state": state, "unread": 2, "cursor": "m5"})
                self.assertTrue(activity.mark_read(bot))
                snap = activity.lookup_activity(bot)
                self.assertEqual((snap["state"], snap["unread"]), (state, 0))
                self.assertEqual(activity.lookup_seen(bot), "m5")
        finally:
            backend.mark_read_later = real

    def test_list_previews_share_a_column(self):
        def bot(name, preview):
            return {
                "name": name,
                "title": "",
                "activity": {"state": "idle", "unread": 0, "preview": preview, "cursor": "c"},
            }

        bots = [bot("A", "one"), bot("Researcher", "two replies waiting")]
        for width in (80, 58):
            columns = picker.list_columns(bots, width)
            self.assertIsNotNone(columns, width)
            starts = []
            for item in bots:
                spans = picker.picker_spans(item, False, width, columns)
                self.assertEqual(spans[-1][1], "dim")
                self.assertTrue(spans[-1][0].startswith(item["activity"]["preview"][:4]))
                starts.append(sum(len(text) for text, _style in spans[:-1]))
            self.assertEqual(starts[0], starts[1], width)
        self.assertIsNone(picker.list_columns(bots, 20))
        shown = "".join(text for text, _style in picker.picker_spans(bots[1], False, 18))
        self.assertIn("Researcher", shown)
        clipped = "".join(text for text, _style in picker.picker_spans(bots[1], False, 16))
        self.assertIn("Research…", clipped)

    def test_preview_search_needs_the_letters_together(self):
        bots = [
            {
                "name": "Night",
                "title": "",
                "activity": {"preview": "our task follows the plan today", "state": "idle"},
            },
            {
                "name": "Chef",
                "title": "",
                "activity": {"preview": "the outfit is ready", "state": "idle"},
            },
            {
                "name": "Outfitly",
                "title": "clothes",
                "activity": {"preview": "hang the coat", "state": "idle"},
            },
        ]
        names = [bot["name"] for bot in picker.fuzzy_bots("outf", bots)]
        self.assertEqual(names[0], "Outfitly")
        self.assertIn("Chef", names)
        self.assertNotIn("Night", names)
        self.assertIsNone(picker.preview_score("outf", "our task follows the plan today"))
        self.assertGreater(
            picker.preview_score("outf", "outfit"),
            picker.preview_score("outf", "theoutfit"),
        )
        self.assertGreater(
            picker.fuzzy_score("outf", "Outfitly"),
            picker.preview_score("outf", "the outfit is ready"),
        )

    def test_opening_the_thread_clears_new(self):
        page = transcript.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": [{"id": "m2", "role": "assistant", "text": "served"}]},
                "cursor": "m2",
            }
        )
        chat = chat_state.Chat("Chef")
        chat.ingest(page, first=True)
        self.assertEqual(activity.lookup_seen({"name": "Chef", "id": "a2"}), "m2")
        snap = activity.lookup_activity({"name": "Chef", "id": "a2"})
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(snap["state"], "idle")
        chat.typed = "more"
        chat.arm_send()
        self.assertEqual(activity.lookup_activity({"name": "Chef"})["state"], "working")

    def test_opening_an_unanswered_thread_shows_working(self):
        page = transcript.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {
                    "entries": [
                        {"id": "m1", "role": "assistant", "text": "hi"},
                        {"id": "m2", "role": "user", "text": "go", "isStreaming": False},
                    ]
                },
                "cursor": "m2",
            }
        )
        chat = chat_state.Chat("Chef")
        chat.ingest(page, first=True)
        self.assertEqual(chat.phase, "working")
        quiet = transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
        chat.ingest(quiet)
        chat.ingest(quiet)
        self.assertEqual(chat.phase, "working")
        self.assertEqual(activity.lookup_activity({"name": "Chef", "id": "a2"})["state"], "working")
        screen = FakeScreen(24, 80)
        chat_view.draw(screen, chat)
        header = "".join(screen.rows.get(0, []))
        self.assertIn("working", header)
        spans = picker.picker_spans(
            {"name": "Chef", "activity": {"state": "working", "unread": 0, "preview": "go"}},
            False,
            60,
        )
        self.assertIn(("working    ", "cyan"), spans)
        self.assertEqual(activity.STATUS_STYLE["blocked"], "yellow")
        self.assertNotEqual(activity.STATUS_STYLE["working"], activity.STATUS_STYLE["blocked"])

    def test_a_streaming_reply_stays_working_through_quiet_polls(self):
        page = transcript.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {
                    "entries": [
                        {"id": "m1", "role": "user", "text": "go"},
                        {"id": "m2", "role": "assistant", "text": "partial", "isStreaming": True},
                    ]
                },
                "cursor": "m2",
            }
        )
        chat = chat_state.Chat("Chef")
        chat.phase = "working"
        chat.cursor = "old"
        chat.ingest(page)
        quiet = transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
        chat.ingest(quiet)
        chat.ingest(quiet)
        self.assertEqual(chat.phase, "working")

    def test_the_scan_publishes_each_bot_and_keeps_a_total_failure(self):
        bots = [{"name": "A", "id": "1"}, {"name": "B", "id": "2"}]

        def read(bot):
            return {"entries": [{"id": "m", "role": "user", "text": "go"}], "cursor": "m"}

        survey = activity.Survey(bots, read)
        try:
            deadline = time.monotonic() + 2
            running, checked, total = True, 0, 2
            while time.monotonic() < deadline:
                running, checked, total, _generation = survey.status()
                if checked == total and not running:
                    break
                time.sleep(0.01)
            self.assertEqual((checked, total, running), (2, 2, False))
            self.assertEqual(activity.lookup_activity(bots[0])["state"], "working")
            self.assertEqual(survey.problem(), "")
        finally:
            survey.close()

        def fail(bot):
            raise gbot_cli.GbotError("gbot is not on PATH")

        _reset_overview()
        survey = activity.Survey([{"name": "A", "id": "1"}], fail)
        try:
            deadline = time.monotonic() + 2
            problem = ""
            while time.monotonic() < deadline:
                running, checked, total, _generation = survey.status()
                problem = survey.problem()
                if problem and not running:
                    break
                time.sleep(0.01)
            self.assertEqual(problem, "gbot is not on PATH")
        finally:
            survey.close()

    def test_a_failed_read_does_not_hide_the_bots_that_worked(self):
        bots = [{"name": "A", "id": "1"}, {"name": "B", "id": "2"}]

        def read(bot):
            if bot["name"] == "B":
                raise gbot_cli.GbotError("timeout")
            return {"entries": [{"id": "m", "role": "assistant", "text": "ok"}], "cursor": "m"}

        views, error = activity.build_overview(bots, read)
        self.assertIsNone(error)
        by_name = {bot["name"]: bot for bot in views}
        self.assertEqual(by_name["A"]["activity"]["state"], "idle")
        self.assertNotIn("activity", by_name["B"])

    def test_an_open_widget_needs_you(self):
        widget = _widget("Ship it?", [{"label": "Yes", "value": "yes"}], id="w")
        entries = [
            {"id": "u", "role": "user", "text": "hi"},
            widget,
        ]
        snap = activity.judge_activity(entries, "w", "u")
        self.assertEqual(snap["state"], "blocked")
        self.assertEqual(snap["preview"], "Ship it?")
        self.assertEqual(activity.status_label(snap["state"]), "needs you")
        text = activity.format_overview(
            [{"name": "Chef", "activity": snap}]
        )
        self.assertIn("needs you", text)
        self.assertIn("Ship it?", text)
        running = activity.format_overview(
            [{"name": "Chef", "activity": {"state": "working", "unread": 0, "preview": "go"}}]
        )
        self.assertIn("1 running", running)
        self.assertIn("running", running.splitlines()[-1])
        self.assertNotIn("working", running)
        self.assertEqual(activity.status_label("working"), "working")
        self.assertEqual(activity.overview_summary(
            [{"name": "Chef", "activity": {"state": "working", "unread": 0}}]
        ), "1 working")
        stale = [widget, {"id": "u2", "role": "user", "text": "yes"}]
        later = activity.judge_activity(stale, "u2", "w")
        self.assertNotEqual(later["state"], "blocked")
        self.assertEqual(later["state"], "working")

    def test_working_gives_up_when_nothing_new_arrives(self):
        class Clock:
            def __init__(self, at):
                self.at = at
                self.old = None

            def __enter__(self):
                self.old = activity.clock
                activity.clock = lambda: self.at
                return self

            def __exit__(self, *args):
                activity.clock = self.old

        entries = [
            {"id": "m1", "role": "assistant", "text": "hi"},
            {"id": "m2", "role": "user", "text": "go"},
        ]
        with Clock(50) as clock:
            snap = activity.judge_activity(entries, "m2", "m2")
            self.assertEqual(snap["state"], "working")
            clock.at = 50 + activity.WORKING_TIMEOUT - 1
            snap = activity.judge_activity(entries, "m2", "m2", previous=snap)
            self.assertEqual(snap["state"], "working")
            clock.at = 50 + activity.WORKING_TIMEOUT
            snap = activity.judge_activity(entries, "m2", "m2", previous=snap)
            self.assertEqual(snap["state"], "idle")
            idle = snap
            moved = [
                {"id": "m1", "role": "assistant", "text": "hi"},
                {"id": "m2", "role": "user", "text": "go"},
                {"id": "m3", "role": "assistant", "text": "partial", "isStreaming": True},
            ]
            snap = activity.judge_activity(moved, "m3", "m3", previous=snap)
            self.assertEqual(snap["state"], "working")
            spans = picker.picker_spans(
                {"name": "Chef", "activity": idle},
                False,
                60,
            )
            self.assertNotIn("working", "".join(text for text, _style in spans))

        streaming = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "isStreaming": True},
        ]
        bots = [{"id": "a2", "name": "Chef"}]

        def read(bot):
            return {"entries": streaming, "cursor": "m2"}

        with Clock(10) as clock:
            views, error = activity.build_overview(bots, read)
            self.assertIsNone(error)
            self.assertEqual(views[0]["activity"]["state"], "working")
            clock.at = 10 + activity.WORKING_TIMEOUT
            views, error = activity.build_overview(bots, read)
            self.assertEqual(views[0]["activity"]["state"], "working")
            clock.at = 10 + activity.WORKING_LIVE_TIMEOUT
            views, error = activity.build_overview(bots, read)
            self.assertEqual(views[0]["activity"]["state"], "idle")

        page = transcript.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": entries},
                "cursor": "m2",
            }
        )
        with Clock(1000) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(page, first=True)
            self.assertEqual(chat.phase, "working")
            header = "".join(text for text, _style in paint.title_spans(chat.title, chat.phase, 40))
            self.assertIn("working", header)
            self.assertEqual(paint.title_spans(chat.title, "working", 40)[1][1], "cyan")
            quiet = transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
            clock.at = 1000 + activity.WORKING_TIMEOUT - 1
            chat.ingest(quiet)
            self.assertEqual(chat.phase, "working")
            self.assertIn("Generating", "".join(text for text, _style in grid.footer_spans(chat.phase, "")))
            clock.at = 1000 + activity.WORKING_TIMEOUT
            chat.ingest(quiet)
            self.assertEqual(chat.phase, "idle")
            self.assertNotIn("Generating", "".join(text for text, _style in grid.footer_spans(chat.phase, "")))
            self.assertNotIn("working", "".join(text for text, _style in paint.title_spans(chat.title, chat.phase, 40)))
            self.assertEqual(activity.lookup_activity({"name": "Chef", "id": "a2"})["state"], "idle")

        stream = transcript.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": streaming},
                "cursor": "m2",
            }
        )
        with Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(stream, first=True)
            self.assertEqual(chat.phase, "working")
            clock.at = activity.WORKING_LIVE_TIMEOUT
            self.assertTrue(chat.expire_working())
            self.assertEqual(chat.phase, "idle")
            grown = transcript.parse_thread(
                {
                    "target": {"id": "a2", "name": "Chef"},
                    "transcript": {
                        "entries": [
                            {"id": "m1", "role": "user", "text": "go"},
                            {"id": "m2", "role": "assistant", "text": "partial more", "isStreaming": True},
                        ]
                    },
                    "cursor": "m2",
                }
            )
            chat.ingest(grown)
            self.assertEqual(chat.phase, "working")


class WorkingLivenessTests(unittest.TestCase):
    """The working indicator follows the gateway, not only the visible text."""

    class Clock:
        def __init__(self, at):
            self.at = at

        def __enter__(self):
            self.old = activity.clock
            activity.clock = lambda: self.at
            return self

        def __exit__(self, *args):
            activity.clock = self.old

    @staticmethod
    def page(entries):
        return transcript.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": entries},
                "cursor": entries[-1]["id"],
            }
        )

    def test_streaming_entry_quiet_for_five_minutes_stays_working(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "isStreaming": True},
        ]
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(self.page(entries), first=True)
            self.assertEqual(chat.phase, "working")
            clock.at = 300
            self.assertFalse(chat.expire_working())
            chat.ingest(transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"}))
            self.assertEqual(chat.phase, "working")
            snap = activity.judge_activity(entries, "m2", "m2", previous=activity.lookup_activity({"name": "Chef", "id": "a2"}))
            self.assertEqual(snap["state"], "working")
            self.assertEqual(activity.lookup_activity({"name": "Chef", "id": "a2"})["state"], "working")

    def test_hidden_thinking_entries_keep_working_past_timeout(self):
        base = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "t1", "role": "assistant", "kind": "thinking", "text": "hmm"},
        ]
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(self.page(base), first=True)
            self.assertEqual(chat.phase, "working")
            snap = activity.judge_activity(base, "t1", "t1")
            self.assertEqual(snap["state"], "working")
            entries = list(base)
            for step in range(1, 5):
                clock.at = step * 60
                entries.append({"id": f"t{step + 1}", "role": "assistant", "kind": "thinking", "text": "more" * step})
                chat.ingest(
                    transcript.parse_thread(
                        {"transcript": {"entries": [entries[-1]]}, "cursor": entries[-1]["id"]}
                    )
                )
                self.assertEqual(chat.phase, "working", step)
                snap = activity.judge_activity(entries, entries[-1]["id"], "m1", previous=snap)
                self.assertEqual(snap["state"], "working", step)
            self.assertGreater(clock.at, activity.WORKING_TIMEOUT)
            # A hidden entry that grows in place counts as well.
            clock.at += 100
            entries[-1] = dict(entries[-1], text="more" * 40)
            chat.ingest(
                transcript.parse_thread(
                    {"transcript": {"entries": [entries[-1]]}, "cursor": entries[-1]["id"]}
                )
            )
            self.assertEqual(chat.phase, "working")
            # Once nothing arrives for the plain timeout, it ends.
            clock.at += activity.WORKING_TIMEOUT
            chat.ingest(transcript.parse_thread({"transcript": {"entries": []}, "cursor": entries[-1]["id"]}))
            self.assertEqual(chat.phase, "idle")

    def test_user_last_line_with_nothing_arriving_goes_idle(self):
        entries = [
            {"id": "m1", "role": "assistant", "text": "hi"},
            {"id": "m2", "role": "user", "text": "go"},
        ]
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(self.page(entries), first=True)
            self.assertEqual(chat.phase, "working")
            clock.at = activity.WORKING_TIMEOUT - 1
            self.assertFalse(chat.expire_working())
            clock.at = activity.WORKING_TIMEOUT
            self.assertTrue(chat.expire_working())
            self.assertEqual(chat.phase, "idle")
            snap = activity.judge_activity(entries, "m2", "m2")
            clock.at = 2 * activity.WORKING_TIMEOUT
            snap = activity.judge_activity(entries, "m2", "m2", previous=snap)
            self.assertEqual(snap["state"], "idle")

    def test_stuck_streaming_flag_goes_idle_after_safety_cap(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "inProgress": True},
        ]
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(self.page(entries), first=True)
            snap = activity.judge_activity(entries, "m2", "m2")
            clock.at = activity.WORKING_LIVE_TIMEOUT - 1
            self.assertFalse(chat.expire_working())
            self.assertEqual(activity.judge_activity(entries, "m2", "m2", previous=snap)["state"], "working")
            clock.at = activity.WORKING_LIVE_TIMEOUT
            self.assertTrue(chat.expire_working())
            self.assertEqual(chat.phase, "idle")
            self.assertEqual(activity.judge_activity(entries, "m2", "m2", previous=snap)["state"], "idle")
            # The next poll must not flip it back to working.
            chat.ingest(transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"}))
            self.assertEqual(chat.phase, "idle")


class RunningFlagChatTests(unittest.TestCase):
    """The server's running flag decides `working` when it is known."""

    Clock = WorkingLivenessTests.Clock

    def setUp(self):
        backend.SERVER.clear()
        self.addCleanup(backend.SERVER.clear)

    @staticmethod
    def page(entries):
        return WorkingLivenessTests.page(entries)

    def set_flag(self, flag):
        backend.SERVER["a2"] = {"unread": 0, "latest": 1, "running": flag}

    def sent(self):
        return [
            {"id": "m1", "role": "assistant", "text": "hi"},
            {"id": "m2", "role": "user", "text": "go"},
        ]

    def quiet(self):
        return transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})

    def test_true_has_no_quiet_timeout(self):
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            self.set_flag(True)
            chat.ingest(self.page(self.sent()), first=True)
            self.assertEqual(chat.phase, "working")
            clock.at = activity.WORKING_LIVE_TIMEOUT * 3
            self.assertFalse(chat.expire_working())
            chat.ingest(self.quiet())
            self.assertEqual(chat.phase, "working")
            self.assertEqual(chat.settle_working("done"), "working")
            self.assertEqual(chat.settle_working("idle"), "working")
            self.assertEqual(chat.settle_working("blocked"), "blocked")

    def test_true_does_not_override_a_card_that_needs_you(self):
        ask = [
            {"id": "m1", "role": "user", "text": "go"},
            {
                "id": "m2",
                "kind": "send-message",
                "message": {"type": "auto-review-approval", "approval": {"status": "pending", "requestId": "r1"}},
            },
        ]
        with self.Clock(0):
            chat = chat_state.Chat("Chef")
            self.set_flag(True)
            chat.ingest(self.page(ask), first=True)
            self.assertEqual(chat.phase, "blocked")
            self.assertEqual(chat.settle_working("blocked"), "blocked")

    def test_false_ends_working_right_away(self):
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            chat.ingest(self.page(self.sent()), first=True)
            self.assertEqual(chat.phase, "working")
            self.set_flag(False)
            clock.at = 1
            self.assertTrue(chat.expire_working())
            self.assertEqual(chat.phase, "idle")
            # A poll with no news does not bring it back.
            chat.ingest(self.quiet())
            self.assertEqual(chat.phase, "idle")
            self.assertEqual(activity.lookup_activity({"name": "Chef", "id": "a2"})["state"], "idle")

    def test_false_settle_maps_working_to_idle_and_keeps_done(self):
        with self.Clock(0):
            chat = chat_state.Chat("Chef")
            chat.ingest(self.page(self.sent()), first=True)
            self.set_flag(False)
            self.assertEqual(chat.settle_working("working"), "idle")
            self.assertEqual(chat.settle_working("done"), "done")
            streaming = [
                {"id": "m1", "role": "user", "text": "go"},
                {"id": "m2", "role": "assistant", "text": "partial", "isStreaming": True},
            ]
            chat.messages = list(streaming)
            self.assertEqual(chat.settle_working("idle"), "idle")

    def test_none_keeps_the_old_timeouts(self):
        with self.Clock(0) as clock:
            chat = chat_state.Chat("Chef")
            self.set_flag(None)
            chat.ingest(self.page(self.sent()), first=True)
            self.assertEqual(chat.phase, "working")
            clock.at = activity.WORKING_TIMEOUT - 1
            self.assertFalse(chat.expire_working())
            clock.at = activity.WORKING_TIMEOUT
            self.assertTrue(chat.expire_working())
            self.assertEqual(chat.phase, "idle")

    def test_poll_running_is_background_and_rate_limited(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def refresh(ident):
            calls.append(ident)
            started.set()
            release.wait(5)

        with self.Clock(100) as clock, mock.patch.object(backend, "refresh_running", refresh):
            chat = chat_state.Chat("Chef")
            chat.session_id = "a2"
            self.assertTrue(chat.poll_running())
            self.assertTrue(started.wait(5))
            # One is out, so no second call, even after the interval.
            clock.at = 200
            self.assertFalse(chat.poll_running())
            release.set()
            for _ in range(200):
                if not chat.running_busy:
                    break
                time.sleep(0.01)
            self.assertFalse(chat.running_busy)
            # The interval counts from the start of the last call (100), so this one is due.
            self.assertTrue(chat.poll_running())
            for _ in range(200):
                if not chat.running_busy:
                    break
                time.sleep(0.01)
            clock.at = 200 + chat_state.RUNNING_EVERY - 0.5
            self.assertFalse(chat.poll_running())
            clock.at = 200 + chat_state.RUNNING_EVERY
            self.assertTrue(chat.poll_running())
            for _ in range(200):
                if not chat.running_busy:
                    break
                time.sleep(0.01)
        self.assertEqual(calls, ["a2", "a2", "a2"])


class ServerStateTests(unittest.TestCase):
    def test_statuses_give_unread_and_latest(self):
        client = b"\x18\x02" + b"\x20" + backend._varint(1793864041547)
        status = b"\x0a\x03abc" + b"\x1a" + backend._varint(len(client)) + client
        data = b"\x0a" + backend._varint(len(status)) + status
        self.assertEqual(
            backend.parse_agent_statuses(data),
            {"abc": {"unread": 2, "latest": 1793864041547, "running": False, "doing": ""}},
        )

    @staticmethod
    def live_with_activity(text, tool="SetStatus", **flags):
        message = _pb(1, b"tool") + _pb(2, tool.encode()) + (_pb(3, text) if text is not None else b"")
        return ServerStateTests.live(**flags) + _pb(5, message)

    def doing(self, data, now_ms=1_000_000):
        return backend.parse_agent_statuses(data, now_ms)["abc"]["doing"]

    def test_doing_is_the_live_activity_line_while_running(self):
        live = self.live_with_activity(b"is fixing the working indicator", is_running=True)
        self.assertEqual(self.doing(self.status("abc", live=live)), "is fixing the working indicator")
        # The status flag alone also means running.
        self.assertEqual(self.doing(self.status("abc", live=self.live_with_activity(b"on it"), flag=True)), "on it")

    def test_doing_is_one_clean_line_of_at_most_120_chars(self):
        live = self.live_with_activity(b"  is\n fixing\t\tit  " + b"x" * 200, is_running=True)
        text = self.doing(self.status("abc", live=live))
        self.assertTrue(text.startswith("is fixing it xxx"))
        self.assertEqual(len(text), 120)
        self.assertEqual(text, text.strip())

    def test_doing_is_empty_when_not_running_stale_or_missing(self):
        text = b"is fixing it"
        self.assertEqual(self.doing(self.status("abc", live=self.live_with_activity(text, is_running=False))), "")
        self.assertEqual(self.doing(self.status("abc", live=self.live_with_activity(text), flag=False)), "")
        stale = self.live_with_activity(text, is_running=True, updated=1000, stale_after=500)
        self.assertEqual(self.doing(self.status("abc", live=stale), now_ms=1501), "")
        self.assertEqual(self.doing(self.status("abc", live=self.live_with_activity(text, is_running=True), flag=None)), "is fixing it")
        self.assertEqual(self.doing(self.status("abc", live=self.live_with_activity(None, is_running=True))), "")
        self.assertEqual(self.doing(self.status("abc", live=self.live(is_running=True))), "")

    def test_doing_never_shows_the_raw_tool_name_or_unprintable_text(self):
        only_tool = self.live_with_activity(None, tool="SetStatus", is_running=True)
        self.assertEqual(self.doing(self.status("abc", live=only_tool)), "")
        for bad in (b"\xff\xfe", b"bell\x07", b"   "):
            with self.subTest(bad):
                live = self.live_with_activity(bad, is_running=True)
                self.assertEqual(self.doing(self.status("abc", live=live)), "")

    @staticmethod
    def status(agent, live=b"", flag=None, client=b""):
        body = _pb(1, agent.encode())
        if live is not None and live != b"":
            body += _pb(2, live)
        if client:
            body += _pb(3, client)
        if flag is not None:
            body += backend._varint(4 << 3) + bytes([1 if flag else 0])
        return _pb(1, body)

    @staticmethod
    def live(**fields):
        numbers = {"is_running": 2, "composing": 3, "retrying": 4, "subagents": 13}
        data = b""
        for name, value in fields.items():
            if name == "updated":
                data += backend._varint(7 << 3) + backend._varint(value)
            elif name == "stale_after":
                data += backend._varint(8 << 3) + backend._varint(value)
            else:
                data += backend._varint(numbers[name] << 3) + bytes([1 if value else 0])
        return data

    def running(self, data, now_ms=1_000_000):
        return backend.parse_agent_statuses(data, now_ms)["abc"]["running"]

    def test_running_from_status_field_four(self):
        self.assertIs(self.running(self.status("abc", flag=True)), True)

    def test_running_from_live_flags(self):
        for name in ("is_running", "composing", "retrying", "subagents"):
            with self.subTest(name):
                self.assertIs(self.running(self.status("abc", live=self.live(**{name: True}))), True)

    def test_running_false_when_nothing_is_set(self):
        self.assertIs(self.running(self.status("abc", flag=False)), False)
        self.assertIs(self.running(self.status("abc", live=self.live(is_running=False, composing=False))), False)
        self.assertIs(self.running(self.status("abc")), False)

    def test_running_unknown_when_live_state_is_stale(self):
        stale = self.live(is_running=True, updated=1000, stale_after=500)
        self.assertIsNone(self.running(self.status("abc", live=stale), now_ms=1501))
        # Inside the window it still counts, and field 4 wins over a stale live state.
        self.assertIs(self.running(self.status("abc", live=stale), now_ms=1500), True)
        self.assertIs(self.running(self.status("abc", live=stale, flag=True), now_ms=9999), True)
        # No stale_after means the state never goes stale.
        self.assertIs(self.running(self.status("abc", live=self.live(is_running=True, updated=1000)), now_ms=9999), True)

    def test_running_flag_reads_server(self):
        backend.SERVER.clear()
        backend.SERVER.update(
            {
                "t": {"unread": 0, "latest": 1, "running": True},
                "f": {"unread": 0, "latest": 1, "running": False},
                "n": {"unread": 0, "latest": 1, "running": None},
                "o": {"unread": 0, "latest": 1},
            }
        )
        try:
            self.assertEqual(
                [backend.running_flag(i) for i in ("t", "f", "n", "o", "missing")],
                [True, False, None, None, None],
            )
        finally:
            backend.SERVER.clear()

    def test_refresh_running_updates_one_agent_and_swallows_errors(self):
        backend.SERVER.clear()
        backend.SERVER["abc"] = {"unread": 0, "latest": 7, "running": None}
        calls = []

        def answer(method, body):
            calls.append((method, body))
            return self.status("abc", flag=True, client=b"\x18\x05")

        try:
            with mock.patch.object(backend, "backend_call", answer):
                backend.refresh_running("abc")
            self.assertEqual(calls, [("ListGrokBotAgentStatuses", b"\x0a\x03abc")])
            self.assertIs(backend.running_flag("abc"), True)
            # Unread stays what the list holds, the flag alone moves.
            self.assertEqual(backend.SERVER["abc"]["unread"], 0)

            def broken(method, body):
                raise gbot_cli.GbotError("no Cursor token")

            with mock.patch.object(backend, "backend_call", broken):
                backend.refresh_running("abc")
            self.assertIsNone(backend.running_flag("abc"))
            with mock.patch.object(backend, "backend_call", lambda m, b: b"\x0a\x05"):
                backend.refresh_running("abc")
        finally:
            backend.SERVER.clear()

    def with_flag(self, flag, snap):
        bot = {"id": "w1", "name": "Wflag"}
        backend.SERVER.clear()
        backend.SERVER["w1"] = {"unread": snap.get("server_unread", 0), "latest": 5, "running": flag}
        self.addCleanup(backend.SERVER.clear)
        activity.store_activity(bot, {"state": snap["state"], "cursor": "c", "preview": "", "unread": 0})
        return activity.with_activity([bot])[0]["activity"]["state"]

    def test_with_activity_follows_server_running_flag(self):
        self.assertEqual(self.with_flag(True, {"state": "idle"}), "working")
        self.assertEqual(self.with_flag(True, {"state": "idle", "server_unread": 2}), "working")
        self.assertEqual(self.with_flag(True, {"state": "blocked"}), "blocked")
        self.assertEqual(self.with_flag(False, {"state": "working"}), "idle")
        self.assertEqual(self.with_flag(False, {"state": "working", "server_unread": 2}), "unread")
        self.assertEqual(self.with_flag(False, {"state": "blocked"}), "blocked")
        self.assertEqual(self.with_flag(None, {"state": "working"}), "working")
        self.assertEqual(self.with_flag(None, {"state": "idle"}), "idle")

    def test_with_activity_sets_doing_only_while_working(self):
        def snap(running, state, doing="is fixing it", unread=0):
            bot = {"id": "w2", "name": "Wdoing"}
            backend.SERVER.clear()
            backend.SERVER["w2"] = {"unread": unread, "latest": 5, "running": running, "doing": doing}
            self.addCleanup(backend.SERVER.clear)
            activity.store_activity(bot, {"state": state, "cursor": "c", "preview": "", "unread": 0})
            return activity.with_activity([bot])[0]["activity"]

        self.assertEqual(snap(True, "idle")["doing"], "is fixing it")
        self.assertNotIn("doing", snap(True, "idle", doing=""))
        self.assertNotIn("doing", snap(False, "working"))
        self.assertNotIn("doing", snap(True, "blocked"))
        self.assertNotIn("doing", snap(None, "idle"))

    def test_refresh_running_stores_doing_and_doing_text_reads_it(self):
        backend.SERVER.clear()
        backend.SERVER["abc"] = {"unread": 0, "latest": 7, "running": None}
        live = self.live_with_activity(b"is fixing it", is_running=True)
        try:
            self.assertEqual(backend.doing_text("abc"), "")
            with mock.patch.object(backend, "backend_call", lambda m, b: self.status("abc", live=live)):
                backend.refresh_running("abc")
            self.assertEqual(backend.doing_text("abc"), "is fixing it")
            self.assertEqual(backend.doing_text("missing"), "")
            self.assertEqual(backend.doing_text(None), "")
            backend.clear_running("abc")
            self.assertEqual(backend.doing_text("abc"), "")
            backend.SERVER["abc"]["doing"] = "old"

            def broken(method, body):
                raise gbot_cli.GbotError("offline")

            with mock.patch.object(backend, "backend_call", broken):
                backend.refresh_running("abc")
            self.assertEqual(backend.doing_text("abc"), "")
        finally:
            backend.SERVER.clear()

    def test_the_working_row_shows_doing_instead_of_the_preview(self):
        bot = {
            "name": "Chef",
            "title": "cooks",
            "avatarColor": "violet",
            "activity": {"state": "working", "unread": 0, "preview": "old reply", "cursor": "c", "doing": "is fixing the working indicator"},
        }
        for columns in (None, {"name": 6, "state": 11}):
            with self.subTest(columns=columns):
                spans = picker.picker_spans(bot, False, 80, columns)
                row = "".join(text for text, _style in spans)
                self.assertRegex(row, r"working\s+Chef\s+· is fixing")
                self.assertTrue(row.rstrip().endswith("· is fixing the working indicator"))
                self.assertNotIn("old reply", row)
                self.assertEqual(spans[-1], ("is fixing the working indicator", "cyan"))
        idle = dict(bot, activity=dict(bot["activity"], state="idle"))
        row = "".join(text for text, _style in picker.picker_spans(idle, False, 80))
        self.assertIn("old reply", row)
        self.assertNotIn("fixing", row)
        plain = dict(bot, activity={"state": "working", "unread": 0, "preview": "old reply", "cursor": "c"})
        self.assertEqual(picker.picker_spans(plain, False, 80)[-1], ("old reply", "dim"))

    def test_title_spans_show_doing_while_working(self):
        doing = "is fixing the working indicator"
        wide = paint.title_spans("Chef", "working", 80, doing)
        self.assertEqual(wide, [("Chef", "bold"), (" · working · " + doing, "cyan")])
        # Clipped so the name keeps 8 cells.
        mid = paint.title_spans("Outfitly Bot", "working", 40, doing)
        text = "".join(t for t, _s in mid)
        self.assertLessEqual(len(text), 36)
        self.assertEqual(len(mid[0][0]), 8)
        self.assertTrue(mid[0][0].startswith("Outfit"))
        self.assertIn("…", mid[1][0])
        # No room for doing: today's mark, then today's name only.
        self.assertEqual(paint.title_spans("Outfitly Bot", "working", 26, doing), paint.title_spans("Outfitly Bot", "working", 26))
        self.assertEqual(paint.title_spans("Chef", "working", 20, doing), paint.title_spans("Chef", "working", 20))
        self.assertEqual(paint.title_spans("Chef", "working", 8, doing), [("Chef", "bold")])
        # Only while working.
        for phase in ("idle", "blocked"):
            self.assertEqual(paint.title_spans("Chef", phase, 80, doing), paint.title_spans("Chef", phase, 80))
        self.assertEqual(paint.title_spans("Chef", "working", 80, ""), paint.title_spans("Chef", "working", 80))

    def test_server_state_sorts_and_counts(self):
        backend.SERVER.clear()
        backend.SERVER.update({"a": {"unread": 0, "latest": 5}, "b": {"unread": 3, "latest": 9}})
        try:
            views = activity.with_activity([{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
        finally:
            backend.SERVER.clear()
        ordered = activity.order_bots(views)
        self.assertEqual([bot["name"] for bot in ordered], ["B", "A"])
        self.assertEqual(ordered[0]["activity"]["unread"], 3)


def _pb(field, payload):
    """One length-delimited protobuf field."""
    return backend._varint(field << 3 | 2) + backend._varint(len(payload)) + payload


def _section_blob(ident, name, agent_ids):
    return _pb(1, ident.encode()) + _pb(2, name.encode()) + b"".join(_pb(3, a.encode()) for a in agent_ids)


class SectionParseTests(unittest.TestCase):
    def blob(self, pinned_field=1):
        sections = _pb(1, _section_blob("section-mwb7wxq3-1", "Projects", ["u-1", "u-2"])) + _pb(
            1, _section_blob("section-x-2", "Home", ["u-3"])
        )
        settings = (
            b"\x08\x05"  # field 1, a varint: ignored
            + b"\x15\x00\x00\x80\x3f"  # field 2, a fixed32: skipped
            + _pb(3, b"ignored")
            + _pb(6, _pb(pinned_field, b"u-9") + _pb(pinned_field, b"u-8"))
            + _pb(7, sections)
        )
        return _pb(1, settings)

    def test_parses_pinned_and_sections_in_order(self):
        layout = backend.parse_runtime_settings(self.blob())
        self.assertEqual(layout["pinned"], ["u-9", "u-8"])
        self.assertEqual(
            layout["sections"],
            [
                {"id": "section-mwb7wxq3-1", "name": "Projects", "agent_ids": ["u-1", "u-2"]},
                {"id": "section-x-2", "name": "Home", "agent_ids": ["u-3"]},
            ],
        )

    def test_pinned_ids_are_read_from_another_field_number_too(self):
        self.assertEqual(backend.parse_runtime_settings(self.blob(pinned_field=2))["pinned"], ["u-9", "u-8"])

    def test_empty_and_unrelated_answers_have_no_sections(self):
        empty = {"pinned": [], "sections": []}
        self.assertEqual(backend.parse_runtime_settings(b""), empty)
        self.assertEqual(backend.parse_runtime_settings(_pb(2, b"x")), empty)
        self.assertEqual(backend.parse_runtime_settings(_pb(1, b"")), empty)

    def test_a_cut_off_answer_raises_what_the_refresh_catches(self):
        with self.assertRaises((ValueError, IndexError)):
            backend.parse_runtime_settings(b"\x0a\x80")

    def test_the_call_is_read_only_with_an_empty_body(self):
        calls = []
        real = backend.backend_call
        backend.backend_call = lambda method, body, timeout=15: calls.append((method, body)) or self.blob()
        backend.set_sections({})
        try:
            backend.refresh_sections()
            self.assertEqual(calls, [("GetGrokBotUserRuntimeSettings", b"")])
            self.assertEqual(backend.current_sections()["pinned"], ["u-9", "u-8"])
            self.assertEqual(cache.read_json(cache.sections_path())["sections"][0]["name"], "Projects")
        finally:
            backend.backend_call = real
            backend.set_sections({})

    def test_a_failed_call_keeps_the_layout_and_raises_nothing(self):
        real = backend.backend_call

        def fail(method, body, timeout=15):
            raise gbot_cli.GbotError("no Cursor token")

        backend.backend_call = fail
        backend.set_sections({"pinned": ["a"], "sections": []})
        try:
            backend.refresh_sections()
            self.assertEqual(backend.current_sections()["pinned"], ["a"])
        finally:
            backend.backend_call = real
            backend.set_sections({})

    def test_the_cache_fills_the_layout_at_startup_but_never_replaces_a_fresh_one(self):
        cache.write_private(cache.sections_path(), {"pinned": ["p"], "sections": [{"id": "s", "name": "Work", "agent_ids": ["a", 3]}]})
        backend.set_sections({})
        try:
            backend.load_sections_cache()
            self.assertEqual(backend.current_sections()["sections"], [{"id": "s", "name": "Work", "agent_ids": ["a"]}])
            backend.set_sections({"pinned": ["fresh"], "sections": []})
            backend.load_sections_cache()
            self.assertEqual(backend.current_sections()["pinned"], ["fresh"])
        finally:
            backend.set_sections({})
            os.remove(cache.sections_path())

    def test_a_broken_cache_means_no_sections(self):
        for junk in ([], "x", {"sections": "x", "pinned": 3}, {"sections": [1, None]}):
            self.assertEqual(backend.clean_sections(junk)["pinned"], [])
            self.assertEqual([s for s in backend.clean_sections(junk)["sections"] if s["agent_ids"]], [])


BOTS_FOR_SECTIONS = [
    {"id": "a", "name": "Alpha"},
    {"id": "b", "name": "Bravo"},
    {"id": "c", "name": "Charlie"},
    {"id": "d", "name": "Delta"},
]


class SectionGroupTests(unittest.TestCase):
    layout = {
        "pinned": ["d"],
        "sections": [
            {"id": "s1", "name": "Projects", "agent_ids": ["c", "a", "d"]},
            {"id": "s2", "name": "Empty", "agent_ids": ["zzz"]},
            {"id": "s3", "name": "Again", "agent_ids": ["a"]},
        ],
    }

    def names(self, bots, rows):
        return [bots[v]["name"] if k == "bot" else "#" + v for k, v in rows]

    def test_pinned_then_sections_then_other_in_each_groups_own_order(self):
        ordered, rows = picker.group_bots(BOTS_FOR_SECTIONS, self.layout)
        self.assertEqual(self.names(ordered, rows), ["#pinned", "Delta", "#Projects", "Alpha", "Charlie", "#other", "Bravo"])
        self.assertEqual([bot["name"] for bot in ordered], ["Delta", "Alpha", "Charlie", "Bravo"])

    def test_empty_sections_have_no_header_and_a_bot_shows_once(self):
        _ordered, rows = picker.group_bots(BOTS_FOR_SECTIONS, self.layout)
        headers = [value for kind, value in rows if kind == "header"]
        self.assertEqual(headers, ["pinned", "Projects", "other"])
        self.assertEqual(len([1 for kind, _ in rows if kind == "bot"]), 4)

    def test_no_sections_known_is_the_flat_list(self):
        for layout in ({}, {"pinned": [], "sections": []}, {"pinned": ["nobody"], "sections": []}):
            ordered, rows = picker.group_bots(BOTS_FOR_SECTIONS, layout)
            self.assertEqual(ordered, BOTS_FOR_SECTIONS)
            self.assertEqual([kind for kind, _ in rows], ["bot"] * 4)

    def test_section_tags_use_the_first_section(self):
        tags = picker.section_tags(self.layout)
        self.assertEqual(tags, {"c": "Projects", "a": "Projects", "d": "Projects", "zzz": "Empty"})

    def test_a_tag_is_dim_at_the_end_when_it_fits_and_dropped_when_it_does_not(self):
        bot = BOTS_FOR_SECTIONS[0]
        wide = picker.picker_spans(bot, False, 80, None, "Projects")
        self.assertEqual(wide[-1], ("  Projects", "dim"))
        narrow = picker.picker_spans(bot, False, 30, None, "Projects")
        self.assertNotIn("Projects", "".join(text for text, _style in narrow))
        self.assertEqual(picker.picker_spans(bot, False, 80), picker.picker_spans(bot, False, 80, None, ""))


class _ListScreen(FakeScreen):
    """Feeds keys to the list and keeps a copy of every frame."""

    def __init__(self, keys, height=24, width=80):
        super().__init__(height, width)
        self.keys = list(keys)
        self.frames = []

    def timeout(self, _ms):
        pass

    def keypad(self, _on):
        pass

    def bkgd(self, *_args):
        pass

    def get_wch(self):
        self.frames.append({y: " ".join(parts) for y, parts in self.rows.items()})
        if not self.keys:
            raise KeyboardInterrupt
        return self.keys.pop(0)


class SectionListTests(unittest.TestCase):
    layout = {
        "pinned": ["d"],
        "sections": [{"id": "s1", "name": "Projects", "agent_ids": ["c", "a"]}],
    }

    def run_list(self, keys, height=24, width=80):
        class Quiet:
            def __init__(self, _bots):
                pass

            def status(self):
                return False, 0, 0, 0

            def problem(self):
                return ""

            def close(self):
                pass

        marked = []
        originals = (activity.Survey, picker.paint.fast_escape, picker.paint.place_cursor, picker.terminal.enable_wheel, activity.mark_read)
        activity.Survey = Quiet
        picker.paint.fast_escape = picker.paint.place_cursor = picker.terminal.enable_wheel = lambda *a: None
        activity.mark_read = lambda bot: marked.append(bot["name"])
        backend.set_sections(self.layout)
        screen = _ListScreen(keys, height, width)
        try:
            result = picker.pick(screen, [dict(bot) for bot in BOTS_FOR_SECTIONS])
        finally:
            (activity.Survey, picker.paint.fast_escape, picker.paint.place_cursor, picker.terminal.enable_wheel, activity.mark_read) = originals
            backend.set_sections({})
        return result, screen, marked

    def test_headers_are_drawn_dim_in_the_servers_order(self):
        _result, screen, _marked = self.run_list(["\r"])
        frame = screen.frames[0]
        self.assertEqual(frame[2].strip(), "pinned")
        self.assertIn("Delta", frame[3])
        self.assertEqual(frame[4].strip(), "Projects")
        self.assertIn("Alpha", frame[5])
        self.assertIn("Charlie", frame[6])
        self.assertEqual(frame[7].strip(), "other")
        self.assertIn("Bravo", frame[8])
        # The highlight starts on the first bot, not on the header above it.
        self.assertIn("→", frame[3])

    def test_enter_on_the_first_row_below_a_header_opens_that_bot(self):
        self.assertEqual(self.run_list(["\r"])[0], "Delta")

    def test_up_and_down_skip_headers(self):
        self.assertEqual(self.run_list([curses.KEY_DOWN, "\r"])[0], "Alpha")
        self.assertEqual(self.run_list([curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_DOWN, "\r"])[0], "Bravo")
        self.assertEqual(self.run_list([curses.KEY_DOWN] * 6 + [curses.KEY_UP, curses.KEY_UP, "\r"])[0], "Alpha")
        self.assertEqual(self.run_list([curses.KEY_UP, "\r"])[0], "Delta")

    def test_ctrl_x_marks_the_highlighted_bot_not_a_header(self):
        result, _screen, marked = self.run_list([curses.KEY_DOWN, curses.KEY_DOWN, "\x18", "\r"])
        self.assertEqual(marked, ["Charlie"])
        self.assertEqual(result, "Charlie")

    def test_the_scroll_window_counts_header_rows(self):
        # Height 7 leaves 4 body rows for 7 drawn rows (3 headers, 4 bots).
        result, screen, _marked = self.run_list([curses.KEY_DOWN] * 3 + ["\r"], height=7)
        self.assertEqual(result, "Bravo")
        last = screen.frames[-1]
        self.assertIn("→", last[5])
        self.assertIn("Bravo", last[5])
        self.assertEqual(last[4].strip(), "other")

    def test_a_search_is_flat_and_tags_each_row_with_its_section(self):
        result, screen, _marked = self.run_list(list("a") + ["\r"])
        frame = screen.frames[1]
        rows = [frame[y] for y in sorted(frame) if y >= 2 and "·" not in frame[y] and "●" in frame[y]]
        self.assertTrue(rows)
        self.assertFalse(any(frame.get(y, "").strip() in ("pinned", "other", "Projects") for y in range(2, 10)))
        alpha = next(row for row in rows if "Alpha" in row)
        self.assertTrue(alpha.rstrip().endswith("Projects"), alpha)
        self.assertEqual(result, "Alpha")

    def test_no_sections_known_is_todays_flat_list(self):
        self.layout, saved = {}, self.layout
        try:
            result, screen, _marked = self.run_list([curses.KEY_DOWN, "\r"])
        finally:
            self.layout = saved
        frame = screen.frames[0]
        self.assertEqual(result, "Bravo")
        for y in range(2, 8):
            self.assertNotIn(frame.get(y, "").strip(), ("pinned", "other", "Projects"))
        self.assertIn("Alpha", frame[2])


class RouteTests(unittest.TestCase):
    names = ["Chef", "Cursor Cloud Session"]

    def test_routes_by_prefix_and_slug(self):
        self.assertEqual(roster.parse_route("@che make soup", self.names), ("Chef", "make soup"))
        self.assertEqual(
            roster.parse_route("@Cursor-Cloud-Session do it", self.names),
            ("Cursor Cloud Session", "do it"),
        )

    def test_full_name_with_spaces_routes(self):
        self.assertEqual(
            roster.parse_route("@Travel Agent plan a trip", ["Travel Agent"]),
            ("Travel Agent", "plan a trip"),
        )
        self.assertEqual(
            roster.parse_route("@cursor cloud session do it", self.names),
            ("Cursor Cloud Session", "do it"),
        )

    def test_longest_full_name_wins(self):
        names = ["Travel", "Travel Agent", "Travel Agent Pro"]
        self.assertEqual(
            roster.parse_route("@Travel Agent Pro plan", names),
            ("Travel Agent Pro", "plan"),
        )
        self.assertEqual(roster.parse_route("@Travel Agent plan", names), ("Travel Agent", "plan"))

    def test_hyphen_slug_still_routes(self):
        self.assertEqual(
            roster.parse_route("@Travel-Agent plan", ["Travel Agent"]),
            ("Travel Agent", "plan"),
        )

    def test_plain_text_is_not_routed(self):
        self.assertIsNone(roster.parse_route("hello @chef", self.names))
        self.assertIsNone(roster.parse_route("@chef", self.names))

    def test_unknown_bot_raises(self):
        with self.assertRaises(gbot_cli.GbotError):
            roster.parse_route("@nobody hi", self.names)


class StatusBeatTests(unittest.TestCase):
    def test_a_progress_line_drops_once_the_reply_arrives(self):
        beat = {
            "kind": "send-message",
            "id": "t1s1",
            "message": {"type": "text", "content": "Checking the agent now."},
        }
        answer = {
            "kind": "send-message",
            "id": "t1s2",
            "message": {"type": "text", "content": "Just one cloud agent is working on Hangry."},
        }
        rows = [text for kind, text in grid.transcript_rows([beat, answer], 80) if kind == "text"]
        self.assertEqual(rows, ["Just one cloud agent is working on Hangry."])
        # Still the latest line, so it stays until the reply shows up.
        alone = [text for kind, text in grid.transcript_rows([beat], 80) if kind == "text"]
        self.assertEqual(alone, ["Checking the agent now."])
        # A scheduled fact is the reply. A ticket number inside a progress line is not.
        plan = {
            "kind": "send-message",
            "id": "t2s1",
            "message": {"type": "text", "content": "Mach ich. Ich lege Moustafa von 14:00 bis 14:30."},
        }
        kept = [text for kind, text in grid.transcript_rows([plan, answer], 80) if kind == "text"]
        self.assertIn("Mach ich. Ich lege Moustafa von 14:00 bis 14:30.", kept)
        looking = {
            "kind": "send-message",
            "id": "t3s1",
            "message": {
                "type": "text",
                "content": "Ich schau mir EXE-3318 kurz an und schlage dir dann einen Kommentar vor.",
            },
        }
        posted = {
            "kind": "send-message",
            "id": "t3s2",
            "message": {"type": "text", "content": "Dein Kommentar steht jetzt auf EXE-3318."},
        }
        rows = [text for kind, text in grid.transcript_rows([looking, posted], 80) if kind == "text"]
        self.assertEqual(rows, ["Dein Kommentar steht jetzt auf EXE-3318."])
        promise = {
            "kind": "send-message",
            "id": "t4s1",
            "message": {
                "type": "text",
                "content": 'Ich schicke die Antwort jetzt ohne Grußformel im Thread "Re: GH Copilot".',
            },
        }
        gone = [text for kind, text in grid.transcript_rows([promise, posted], 80) if kind == "text"]
        self.assertEqual(gone, ["Dein Kommentar steht jetzt auf EXE-3318."])
        decision = {
            "kind": "send-message",
            "id": "t5s1",
            "message": {"type": "text", "content": "Okay, I'll leave those 7 alone."},
        }
        stayed = [text for kind, text in grid.transcript_rows([decision, posted], 80) if kind == "text"]
        self.assertIn("Okay, I'll leave those 7 alone.", stayed)
        # A question in between does not keep the progress line once the result arrives.
        sending = {
            "kind": "send-message",
            "id": "t6s1",
            "message": {"type": "text", "content": "Ich schicke die Nachricht jetzt an Robin."},
        }
        asked = {"kind": "message", "id": "t6u", "role": "user", "content": "was war die antwort an mario?"}
        sent = {
            "kind": "send-message",
            "id": "t6s2",
            "message": {"type": "text", "content": "Die Nachricht an Robin ist raus."},
        }
        rows = grid.transcript_rows([sending, asked, sent], 80)
        self.assertEqual([text for kind, text in rows if kind == "text"], ["Die Nachricht an Robin ist raus."])
        self.assertEqual(
            [text for kind, text in rows if kind == "user"],
            ["was war die antwort an mario?"],
        )

class TokenTests(unittest.TestCase):
    @staticmethod
    def jwt(exp):
        import json
        body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"

    def test_expired_token_is_dropped(self):
        self.assertTrue(backend.token_expired(self.jwt(100), now=200))
        self.assertFalse(backend.token_expired(self.jwt(300), now=200))

    def test_garbage_counts_as_live(self):
        self.assertFalse(backend.token_expired(""))
        self.assertFalse(backend.token_expired("not.a.jwt"))


class CursorScreen(FakeScreen):
    def move(self, y, x):
        self.cursor = (y, x)


def _typed(chat, keys, screen=None):
    for key in keys:
        chat_screen.chat_key(chat, key, screen)


class ComposerEditTests(unittest.TestCase):
    def test_left_right_insert_and_backspace_at_the_cursor(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "helo")
        _typed(chat, [curses.KEY_LEFT, "l"])
        self.assertEqual((chat.typed, chat.pos), ("hello", 4))
        _typed(chat, [curses.KEY_LEFT, curses.KEY_LEFT, "\x7f"])
        self.assertEqual((chat.typed, chat.pos), ("hllo", 1))
        _typed(chat, [curses.KEY_DC])
        self.assertEqual((chat.typed, chat.pos), ("hlo", 1))
        _typed(chat, [curses.KEY_RIGHT] * 5 + ["!"])
        self.assertEqual(chat.typed, "hlo!")

    def test_line_start_end_and_kills(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "one two\nthree four")
        _typed(chat, ["\x01"])
        self.assertEqual(chat.pos, len("one two\n"))
        _typed(chat, [curses.KEY_END])
        self.assertEqual(chat.pos, len(chat.typed))
        _typed(chat, ["\x17"])
        self.assertEqual(chat.typed, "one two\nthree ")
        _typed(chat, [curses.KEY_HOME, "\x0b"])
        self.assertEqual(chat.typed, "one two\n")
        _typed(chat, ["\x7f", "\x05", "\x15"])
        self.assertEqual((chat.typed, chat.pos), ("", 0))

    def test_word_jumps(self):
        self.assertEqual(draft.word_left("say hello there", 15), ("say hello there", 10))
        self.assertEqual(draft.word_left("say hello there", 10), ("say hello there", 4))
        self.assertEqual(draft.word_right("say hello there", 0), ("say hello there", 3))
        self.assertEqual(draft.word_right("say hello there", 3), ("say hello there", 9))
        original = curses.keyname
        curses.keyname = lambda key: {601: b"kLFT5", 602: b"kRIT3"}.get(key, b"KEY_OTHER")
        try:
            chat = chat_state.Chat("Researcher")
            _typed(chat, "say hello")
            _typed(chat, [601])
            self.assertEqual(chat.pos, 4)
            _typed(chat, [601, 602])
            self.assertEqual(chat.pos, 3)
        finally:
            curses.keyname = original

    def test_kill_line_start_and_end_join_lines_at_an_edge(self):
        self.assertEqual(draft.kill_line_start("ab\ncd", 3), ("abcd", 2))
        self.assertEqual(draft.kill_line_end("ab\ncd", 2), ("abcd", 2))
        self.assertEqual(draft.kill_word("a  b  ", 6), ("a  ", 3))

    def test_cursor_is_drawn_inside_a_wrapped_draft(self):
        text = "abcdefghij klmnopqrst uvwxyz"
        rows = grid.draft_rows(text, 20)
        self.assertEqual([row[0] for row in rows], grid.draft_lines(text, 20))
        self.assertEqual(grid.draft_cursor(text, len(text), 20)[0], len(rows) - 1)
        self.assertEqual(grid.draft_cursor(text, 2, 20), (0, 2))
        chat = chat_state.Chat("Researcher")
        chat.typed, chat.pos = text, 2
        screen = CursorScreen(24, 20)
        chat_view.draw(screen, chat)
        y, x = screen.cursor
        _header, body, _box, _footer = grid.layout(24, len(rows))
        self.assertEqual((y, x), (grid.layout(24, len(rows))[0] + body + 1, 7))
        chat.pos = None
        chat_view.draw(screen, chat)
        self.assertEqual(screen.cursor[0], y + len(rows) - 1)

    def test_draft_row_keeps_the_hanging_pad(self):
        rows = grid.draft_rows("- one two three four five", 16)
        self.assertEqual(rows[1][3], "  ")
        self.assertTrue(rows[1][0].startswith("  "))

    def test_up_down_move_between_draft_lines(self):
        chat = chat_state.Chat("Researcher")
        screen = FakeScreen(24, 80)
        _typed(chat, "first line" + "\n" + "second", screen)
        _typed(chat, [curses.KEY_UP], screen)
        self.assertEqual(chat.pos, 6)
        _typed(chat, ["X"], screen)
        self.assertEqual(chat.typed, "first Xline\nsecond")
        _typed(chat, [curses.KEY_DOWN, curses.KEY_END, "!"], screen)
        self.assertEqual(chat.typed, "first Xline\nsecond!")

    def test_old_keys_still_work_with_a_cursor(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "hi")
        _typed(chat, [curses.KEY_LEFT, "\n"])
        self.assertEqual(chat.typed, "h\ni")
        self.assertEqual(chat_screen.chat_key(chat, "\r", None), ("send", ("Researcher", "h\ni")))
        self.assertEqual((chat.typed, chat.pos), ("", None))


class HistoryTests(unittest.TestCase):
    def test_up_recalls_sent_messages_and_down_goes_back(self):
        chat = chat_state.Chat("Researcher")
        chat.messages = [
            {"role": "user", "text": "first"},
            {"role": "assistant", "text": "ok"},
            {"role": "user", "text": "second"},
            {"role": "user", "text": "second"},
        ]
        screen = FakeScreen(24, 80)
        _typed(chat, "draft", screen)
        _typed(chat, [curses.KEY_UP], screen)
        self.assertEqual(chat.typed, "second")
        _typed(chat, [curses.KEY_UP], screen)
        self.assertEqual(chat.typed, "first")
        _typed(chat, [curses.KEY_UP], screen)
        self.assertEqual(chat.typed, "first")
        _typed(chat, ["!"], screen)
        self.assertEqual(chat.sent_history(), ["first", "second"])
        _typed(chat, [curses.KEY_DOWN], screen)
        self.assertEqual(chat.typed, "second")
        _typed(chat, [curses.KEY_DOWN], screen)
        self.assertEqual((chat.typed, chat.pos), ("draft", 5))
        self.assertFalse(chat.recall(1))

    def test_a_sent_message_joins_the_history(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "hello")
        self.assertEqual(chat_screen.chat_key(chat, "\r", None)[0], "send")
        _typed(chat, [curses.KEY_UP], FakeScreen(24, 80))
        self.assertEqual(chat.typed, "hello")
        self.assertEqual(chat_screen.chat_key(chat, "\r", None)[0], "send")
        self.assertIsNone(chat.hist_at)

    def test_up_inside_a_multi_line_draft_moves_first(self):
        chat = chat_state.Chat("Researcher")
        chat.messages = [{"role": "user", "text": "old"}]
        screen = FakeScreen(24, 80)
        _typed(chat, "a\nb", screen)
        _typed(chat, [curses.KEY_UP], screen)
        self.assertEqual(chat.typed, "a\nb")
        _typed(chat, [curses.KEY_UP], screen)
        self.assertEqual(chat.typed, "old")


class _KeyFeed:
    """get_wch over a list; `timeout` is recorded, not slept."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.timeouts = []

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        return self.keys.pop(0)

    def timeout(self, ms):
        self.timeouts.append(ms)


class EscapeSequenceTests(unittest.TestCase):
    def test_csi_and_ss3_arrows_are_arrows(self):
        for seq, key in (
            ("\x1b[D", curses.KEY_LEFT),
            ("\x1b[C", curses.KEY_RIGHT),
            ("\x1b[A", curses.KEY_UP),
            ("\x1b[B", curses.KEY_DOWN),
            ("\x1b[H", curses.KEY_HOME),
            ("\x1b[F", curses.KEY_END),
            ("\x1bOD", curses.KEY_LEFT),
            ("\x1bOH", curses.KEY_HOME),
            ("\x1b[3~", curses.KEY_DC),
            ("\x1b[5~", curses.KEY_PPAGE),
            ("\x1b[6~", curses.KEY_NPAGE),
            ("\x1b[1~", curses.KEY_HOME),
            ("\x1b[4~", curses.KEY_END),
        ):
            self.assertEqual(paint.decode_keys(list(seq)), [key], seq)

    def test_modified_arrows_use_the_terminfo_names(self):
        original = curses.keyname
        names = {601: b"kLFT5", 602: b"kRIT3"}
        curses.keyname = lambda key: names.get(key, b"KEY_OTHER")
        paint._NAMED = None
        try:
            keys = paint.decode_keys(list("\x1b[1;5D\x1b[1;3C\x1b[1;7Z"))
            self.assertEqual(keys, [601, 602])
            self.assertIs(chat_screen.edit_motion(keys[0]), draft.word_left)
            self.assertIs(chat_screen.edit_motion(keys[1]), draft.word_right)
            # A modifier terminfo has no name for is the plain arrow.
            self.assertEqual(paint.decode_keys(list("\x1b[1;2D")), [curses.KEY_LEFT])
        finally:
            curses.keyname = original
            paint._NAMED = None

    def test_a_lone_escape_stays_escape(self):
        self.assertEqual(paint.decode_keys(["\x1b"]), ["\x1b"])
        self.assertEqual(paint.decode_keys([27]), ["\x1b"])
        self.assertEqual(paint.decode_keys(["\x1b", "\x1b"]), ["\x1b", "\x1b"])
        self.assertEqual(paint.decode_keys(["\x1b", curses.KEY_LEFT]), ["\x1b", curses.KEY_LEFT])

    def test_unknown_or_alt_sequences_are_dropped_not_escape(self):
        self.assertEqual(paint.decode_keys(list("\x1b[99z")), [])
        self.assertEqual(paint.decode_keys(list("\x1bx")), [])

    def test_bracketed_paste_keeps_newlines_in_the_draft(self):
        paint.PASTING = False
        try:
            keys = paint.decode_keys(list("\x1b[200~one\rtwo"))
            self.assertTrue(paint.PASTING)
            keys += paint.decode_keys(list("\rthree\x1b[201~\r"))
            self.assertFalse(paint.PASTING)
        finally:
            paint.PASTING = False
        self.assertEqual(keys, list("one\ntwo\nthree\r"))
        chat = chat_state.Chat("Researcher")
        results = [chat_screen.chat_key(chat, key, None) for key in keys[:-1]]
        self.assertEqual(results, [None] * len(results))
        self.assertEqual(chat.typed, "one\ntwo\nthree")
        self.assertEqual(chat_screen.chat_key(chat, keys[-1], None)[0], "send")

    def test_keys_around_a_sequence_keep_their_order(self):
        self.assertEqual(paint.decode_keys(list("ab\x1b[Dc")), ["a", "b", curses.KEY_LEFT, "c"])

    def test_a_split_sequence_reads_the_rest_within_the_delay(self):
        screen = _KeyFeed(["[", "D"])
        more = paint.follow_up(screen, 0)
        self.assertEqual(paint.decode_keys(["\x1b"], more), [curses.KEY_LEFT])
        self.assertIn(paint.ESC_WAIT_MS, screen.timeouts)
        self.assertEqual(screen.timeouts[-1], 0)

    def test_read_keys_decodes_csi_and_the_chat_stays_open(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "abc")
        for key in paint.read_keys(_KeyFeed(list("\x1b[DX"))):
            self.assertIsNone(chat_screen.chat_key(chat, key, None))
        self.assertEqual(chat.typed, "abXc")
        self.assertEqual(chat_screen.chat_key(chat, paint.read_keys(_KeyFeed(["\x1b"]))[0], None), "back")


class KeyHelpTests(unittest.TestCase):
    def test_question_mark_or_f1_opens_help_and_any_key_closes_it(self):
        chat = chat_state.Chat("Researcher")
        self.assertIsNone(chat_screen.chat_key(chat, "?", None))
        self.assertTrue(chat.help)
        # Esc closes the overlay and stays in the chat.
        self.assertIsNone(chat_screen.chat_key(chat, "\x1b", None))
        self.assertFalse(chat.help)
        self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_F1, None))
        self.assertTrue(chat.help)
        self.assertIsNone(chat_screen.chat_key(chat, "x", None))
        self.assertEqual((chat.help, chat.typed), (False, ""))

    def test_a_question_mark_in_a_draft_is_text(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "why?")
        self.assertEqual((chat.help, chat.typed), (False, "why?"))
        chat = chat_state.Chat("Researcher")
        _typed(chat, "??")
        self.assertEqual((chat.help, chat.typed), (False, "?"))

    def test_the_idle_footer_names_the_help_key(self):
        self.assertIn("? keys", grid.footer_spans("idle", "")[0][0])


def _search_chat(count=30):
    chat = chat_state.Chat("Researcher")
    chat.loaded = True
    chat.messages = [{"id": f"m{i}", "role": "user" if i % 2 else "assistant", "text": f"line {i}" + (" apple" if i in (3, 20) else "")} for i in range(1, count + 1)]
    chat.touch_rows()
    return chat


class SearchTests(unittest.TestCase):
    def test_hits_are_case_folded_rows(self):
        rows = [("text", "An Apple"), ("gap", ""), ("user", [("apple", "bold"), (" pie", "")]), ("text", "pear")]
        self.assertEqual(grid.search_hits(rows, "APPLE"), [0, 2])
        self.assertEqual(grid.search_hits(rows, ""), [])
        self.assertEqual(grid.match_spans("apple Apple", "apple"), [(0, 5), (6, 11)])

    def test_ctrl_f_opens_a_prompt_and_esc_closes_only_the_search(self):
        chat = _search_chat()
        self.assertIsNone(chat_screen.chat_key(chat, "\x06", None))
        self.assertEqual(chat.search, "")
        _typed(chat, "apple")
        self.assertEqual((chat.search, chat.typed), ("apple", ""))
        self.assertIsNone(chat_screen.chat_key(chat, "\x1b", None))
        self.assertIsNone(chat.search)
        self.assertEqual(chat_screen.chat_key(chat, "\x1b", None), "back")

    def test_enter_and_ctrl_n_go_older_ctrl_p_newer_and_wrap(self):
        chat = _search_chat()
        _typed(chat, "\x06apple")
        rows = grid.visible_rows(chat, 80)
        hits = grid.search_hits(rows, "apple")
        self.assertEqual(len(hits), 2)
        self.assertEqual(chat_view.search_target(rows, chat)[0], hits[1])
        chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(chat_view.search_target(rows, chat)[0], hits[0])
        chat_screen.chat_key(chat, "\x0e", None)
        self.assertEqual(chat_view.search_target(rows, chat)[0], hits[1])
        chat_screen.chat_key(chat, "\x10", None)
        self.assertEqual(chat_view.search_target(rows, chat)[0], hits[0])
        # Enter in the search never sends.
        self.assertEqual(chat.typed, "")

    def test_draw_scrolls_to_the_match_and_shows_the_prompt(self):
        chat = _search_chat(60)
        screen = FakeScreen(12, 58)
        chat_view.draw(screen, chat)
        self.assertNotIn("apple", " ".join(" ".join(parts) for parts in screen.rows.values()))
        _typed(chat, "\x06apple")
        chat_screen.chat_key(chat, "\r", None)
        chat_view.draw(screen, chat)
        shown = " ".join(" ".join(parts) for parts in screen.rows.values())
        self.assertIn("line 3 apple", shown)
        self.assertIn("find: ", shown)
        self.assertIn("1/2", shown)
        self.assertFalse(chat.follow)

    def test_no_match_says_so(self):
        self.assertIn("  no match", [text for text, _style in grid.search_spans("zzz", [], None)])


class CopyTests(unittest.TestCase):
    def test_ctrl_o_copies_the_last_bot_reply_with_osc52(self):
        sent = []
        original = terminal.write_tty
        terminal.write_tty = sent.append
        try:
            chat = _search_chat(4)
            chat.messages.append({"id": "u", "role": "user", "text": "mine"})
            self.assertIsNone(chat_screen.chat_key(chat, "\x0f", None))
        finally:
            terminal.write_tty = original
        self.assertEqual(sent, [b"\x1b]52;c;" + base64.b64encode(b"line 4") + b"\x07"])
        self.assertIn("Copied", chat.notice)
        self.assertEqual(chat.error, "")
        self.assertEqual(chat.typed, "")
        self.assertTrue(any("ctrl-o" in keys for keys, _what in chat_view.HELP))


class NoticeFooterTests(unittest.TestCase):
    def test_a_notice_is_green_and_an_error_wins_over_it(self):
        self.assertEqual(grid.footer_spans("idle", "", notice="Copied")[0], ("Copied", "green"))
        self.assertEqual(grid.footer_spans("idle", "down", notice="Copied")[0], ("down", "red"))
        self.assertEqual(grid.footer_spans("blocked", "", decide=True, notice="Copied")[0], ("Copied", "green"))
        self.assertEqual(grid.footer_rows("idle", "", width=58, notice="Copied")[-1][0], ("Copied", "green"))

    def test_a_notice_clears_after_a_few_seconds_or_on_the_next_key(self):
        now = [100.0]
        original = activity.clock
        activity.clock = lambda: now[0]
        try:
            chat = _search_chat(4)
            chat.show_notice("Copied")
            self.assertFalse(chat.expire_notice())
            now[0] += chat_state.NOTICE_SECONDS
            self.assertTrue(chat.expire_notice())
            self.assertEqual(chat.notice, "")
            chat.show_notice("Copied")
            chat_screen.chat_key(chat, "x", None)
            self.assertEqual((chat.notice, chat.typed), ("", "x"))
        finally:
            activity.clock = original

    def test_draw_shows_the_notice_not_the_hint(self):
        chat = _search_chat(4)
        chat.show_notice("Copied the last reply (6 chars).")
        screen = FakeScreen(12, 58)
        chat_view.draw(screen, chat)
        self.assertIn("Copied the last reply", " ".join(screen.rows[11]))



class ListHelpTests(unittest.TestCase):
    def test_f1_or_question_mark_on_an_empty_search_opens_it(self):
        self.assertTrue(picker.opens_help(curses.KEY_F1, ""))
        self.assertTrue(picker.opens_help(curses.KEY_F1, "che"))
        self.assertTrue(picker.opens_help("?", ""))
        self.assertFalse(picker.opens_help("?", "che"))
        self.assertFalse(picker.opens_help("x", ""))
        self.assertIn("? keys", picker.LIST_HINT)

    def test_the_list_shows_help_and_any_key_closes_it(self):
        drawn = []

        class Screen(FakeScreen):
            def __init__(self, keys):
                super().__init__(24, 80)
                self.keys = list(keys)

            def timeout(self, _ms):
                pass

            def keypad(self, _on):
                pass

            def bkgd(self, *_args):
                pass

            def get_wch(self):
                drawn.append(" ".join(" ".join(parts) for parts in self.rows.values()))
                if not self.keys:
                    raise KeyboardInterrupt
                return self.keys.pop(0)

        bots = [{"name": "Chef", "title": "what to cook"}]
        screen = Screen(["?", "x", "\r"])
        originals = (activity.Survey, picker.paint.fast_escape, picker.paint.place_cursor, picker.terminal.enable_wheel)

        class Quiet:
            def __init__(self, _bots):
                pass

            def status(self):
                return False, 0, 0, 0

            def problem(self):
                return ""

            def close(self):
                pass

        activity.Survey = Quiet
        picker.paint.fast_escape = picker.paint.place_cursor = picker.terminal.enable_wheel = lambda *a: None
        try:
            self.assertEqual(picker.pick(screen, bots), "Chef")
        finally:
            activity.Survey, picker.paint.fast_escape, picker.paint.place_cursor, picker.terminal.enable_wheel = originals
        self.assertNotIn("Keys", drawn[0])
        self.assertIn("Keys  (any key closes)", drawn[1])
        self.assertIn("open the bot", drawn[1])
        self.assertNotIn("what to cook", drawn[1])
        # "x" closed it and did not start a search.
        self.assertIn("what to cook", drawn[2])
        self.assertNotIn("/x", drawn[2])


class MouseToggleTests(unittest.TestCase):
    def setUp(self):
        self.saved = terminal.MOUSE
        self.masks = []
        self.mousemask = curses.mousemask
        curses.mousemask = lambda mask: self.masks.append(mask)

    def tearDown(self):
        curses.mousemask = self.mousemask
        terminal.MOUSE = self.saved

    def test_toggle_flips_the_shared_flag_and_the_mask(self):
        terminal.set_mouse(True)
        self.assertTrue(terminal.MOUSE)
        self.assertNotEqual(self.masks[-1], 0)
        self.assertFalse(terminal.toggle_mouse())
        self.assertEqual(self.masks[-1], 0)
        self.assertTrue(terminal.toggle_mouse())
        self.assertNotEqual(self.masks[-1], 0)

    def test_enable_wheel_respects_the_flag(self):
        terminal.set_mouse(False)
        terminal.enable_wheel()
        self.assertEqual(self.masks[-1], 0)
        terminal.set_mouse(True)
        terminal.enable_wheel()
        self.assertNotEqual(self.masks[-1], 0)

    def test_env_sets_the_start_state(self):
        self.assertTrue(terminal.mouse_default({}))
        self.assertTrue(terminal.mouse_default({"GBOT_TUI_MOUSE": "1"}))
        self.assertTrue(terminal.mouse_default({"GBOT_TUI_MOUSE": ""}))
        self.assertFalse(terminal.mouse_default({"GBOT_TUI_MOUSE": "0"}))
        self.assertFalse(terminal.mouse_default({"GBOT_TUI_MOUSE": " 0 "}))

    def test_the_select_hint_leads_the_line_only_while_off(self):
        spans = [("enter send", "dim")]
        terminal.set_mouse(True)
        self.assertEqual(terminal.with_select_hint(spans), spans)
        terminal.set_mouse(False)
        shown = terminal.with_select_hint(spans)
        self.assertTrue(shown[0][0].startswith("select mode · f2 mouse"))
        self.assertEqual(shown[1:], spans)

    def test_f2_in_the_chat_toggles_and_the_footer_says_so(self):
        terminal.set_mouse(True)
        chat = _search_chat(4)
        screen = FakeScreen(12, 80)
        chat_view.draw(screen, chat)
        self.assertNotIn("select mode", " ".join(screen.rows[11]))
        self.assertIsNone(chat_screen.chat_key(chat, curses.KEY_F2, None))
        self.assertFalse(terminal.MOUSE)
        self.assertEqual(chat.typed, "")
        chat_view.draw(screen, chat)
        self.assertTrue(" ".join(screen.rows[11]).lstrip().startswith("select mode · f2 mouse"))
        chat_screen.chat_key(chat, curses.KEY_F2, None)
        self.assertTrue(terminal.MOUSE)
        chat_view.draw(screen, chat)
        self.assertNotIn("select mode", " ".join(screen.rows[11]))

    def test_the_list_toggles_with_f2_and_shows_the_hint(self):
        drawn = []

        class Screen(FakeScreen):
            def __init__(self, keys):
                super().__init__(24, 80)
                self.keys = list(keys)

            def timeout(self, _ms):
                pass

            def keypad(self, _on):
                pass

            def bkgd(self, *_args):
                pass

            def get_wch(self):
                drawn.append(" ".join(self.rows.get(23, [])))
                return self.keys.pop(0)

        class Quiet:
            def __init__(self, _bots):
                pass

            def status(self):
                return False, 0, 0, 0

            def problem(self):
                return ""

            def close(self):
                pass

        originals = (activity.Survey, picker.paint.fast_escape, picker.paint.place_cursor)
        activity.Survey = Quiet
        picker.paint.fast_escape = picker.paint.place_cursor = lambda *a: None
        terminal.set_mouse(True)
        try:
            screen = Screen([curses.KEY_F2, curses.KEY_F2, "\r"])
            self.assertEqual(picker.pick(screen, [{"name": "Chef"}]), "Chef")
        finally:
            activity.Survey, picker.paint.fast_escape, picker.paint.place_cursor = originals
        self.assertNotIn("select mode", drawn[0])
        self.assertIn("select mode · f2 mouse", drawn[1])
        self.assertNotIn("select mode", drawn[2])
        self.assertTrue(terminal.MOUSE)

    def test_both_helps_list_f2_and_shift_drag(self):
        for table in (chat_view.HELP, picker.LIST_HELP):
            keys = dict(table)
            self.assertEqual(keys["f2"], "mouse on/off: off lets a plain drag select text")
            self.assertEqual(keys["shift+drag"], "select text while the mouse is on (most terminals)")


def _views(**states):
    return [{"name": name, "activity": {"state": state}} for name, state in states.items()]


class NeedsYouBellTests(unittest.TestCase):
    def test_osc9_is_bell_then_esc_9_text_bel(self):
        self.assertEqual(terminal.notify("Chef needs you"), b"\x07\x1b]9;Chef needs you\x07")
        # A control byte in a bot name cannot end the sequence early.
        self.assertEqual(terminal.notify("a\x07b\x1bc"), b"\x07\x1b]9;a b c\x07")

    def test_only_a_new_transition_rings_and_only_once(self):
        fresh, states = activity.newly_blocked({}, _views(Chef="blocked", Researcher="idle"))
        self.assertEqual(fresh, [])  # the first look is the baseline
        fresh, states = activity.newly_blocked(states, _views(Chef="blocked", Researcher="blocked"))
        self.assertEqual(fresh, ["Researcher"])
        fresh, states = activity.newly_blocked(states, _views(Chef="blocked", Researcher="blocked"))
        self.assertEqual(fresh, [])
        fresh, states = activity.newly_blocked(states, _views(Chef="blocked", Researcher="idle"))
        fresh, states = activity.newly_blocked(states, _views(Chef="blocked", Researcher="blocked"))
        self.assertEqual(fresh, ["Researcher"])
        # A bot new to the list is a baseline too.
        fresh, states = activity.newly_blocked(states, _views(Chef="blocked", Researcher="blocked", New="blocked"))
        self.assertEqual(fresh, [])


def _thread(first, last):
    entries = [{"id": f"m{i}", "role": "user" if i % 2 else "assistant", "text": f"line {i}"} for i in range(first, last + 1)]
    return transcript.parse_thread({"transcript": {"entries": entries}})


class OlderHistoryTests(unittest.TestCase):
    def test_a_full_first_read_offers_older_entries_with_a_larger_limit(self):
        chat = chat_state.Chat("Chef")
        chat.ingest(_thread(21, 100), first=True)
        self.assertTrue(chat.more_history)
        args = chat.take_older()
        self.assertEqual(args, ("Chef", None, 160))
        self.assertIsNone(chat.take_older())  # one at a time
        self.assertEqual(chat.ingest_older(_thread(1, 100), 160), 20)
        self.assertEqual(chat.messages[0]["id"], "m1")
        self.assertFalse(chat.more_history)  # 100 < 160: that was the start
        self.assertIsNone(chat.take_older())
        # A later full reload keeps the older rows.
        chat.full_reload = True
        self.assertEqual(chat.take_read(), ("Chef", None, 160))

    def test_a_short_thread_or_a_busy_chat_does_not_ask(self):
        chat = chat_state.Chat("Chef")
        chat.ingest(_thread(1, 30), first=True)
        self.assertIsNone(chat.take_older())
        chat = chat_state.Chat("Chef")
        chat.ingest(_thread(21, 100), first=True)
        chat.phase = "working"
        self.assertIsNone(chat.take_older())

    def test_page_up_at_the_top_queues_the_older_read(self):
        chat = chat_state.Chat("Chef")
        chat.ingest(_thread(21, 100), first=True)
        screen = FakeScreen(12, 58)
        chat_view.draw(screen, chat)
        job = None
        saved = getattr(curses, "LINES", None)
        curses.LINES = 12
        try:
            for _ in range(60):
                job = chat_screen.chat_key(chat, curses.KEY_PPAGE, screen) or job
                if job:
                    break
        finally:
            if saved is None:
                del curses.LINES
            else:
                curses.LINES = saved
        self.assertEqual(job, ("older", ("Chef", None, 160)))
        self.assertEqual(chat.scroll, 0)
        self.assertIn("older", chat.notice)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.now = [50.0]
        self.original = activity.clock
        activity.clock = lambda: self.now[0]

    def tearDown(self):
        activity.clock = self.original

    def test_failed_reads_back_off_and_the_footer_counts_down(self):
        chat = chat_state.Chat("Chef")
        delays = [chat.read_failed("gateway is down") for _ in range(7)]
        self.assertEqual(delays[:5], list(chat_state.RETRY_DELAYS))
        self.assertEqual(delays[-1], chat_state.RETRY_DELAYS[-1])
        chat = chat_state.Chat("Chef")
        chat.error = "gateway is down"
        chat.read_failed("gateway is down")
        self.assertEqual(chat.offline_label(), "offline · retrying in 2s")
        self.now[0] += 1.2
        self.assertEqual(chat.offline_label(), "offline · retrying in 1s")
        self.now[0] += 1.0
        self.assertEqual(chat.offline_label(), "offline · retrying…")
        self.assertEqual(chat.footer_error(), "offline · retrying… · gateway is down")
        self.assertEqual(grid.footer_spans("idle", chat.footer_error())[0][1], "red")

    def test_the_next_good_read_says_reconnected_once(self):
        chat = chat_state.Chat("Chef")
        page = transcript.parse_thread({"transcript": {"entries": [{"id": "m1", "role": "assistant", "text": "hi"}]}})
        chat.ingest(page, first=True)
        self.assertEqual(chat.notice, "")
        chat.error = "gateway is down"
        chat.read_failed("gateway is down")
        chat.ingest(transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m1"}))
        self.assertEqual((chat.error, chat.offline_label(), chat.notice), ("", "", "reconnected"))
        self.now[0] += chat_state.NOTICE_SECONDS
        chat.expire_notice()
        chat.ingest(transcript.parse_thread({"transcript": {"entries": []}, "cursor": "m1"}))
        self.assertEqual(chat.notice, "")

    def test_a_send_error_survives_a_good_read(self):
        chat = chat_state.Chat("Chef")
        chat.read_failed("gateway is down")
        chat.error = "send failed"
        chat.ingest(transcript.parse_thread({"transcript": {"entries": []}}), first=True)
        self.assertEqual(chat.error, "send failed")


class MentionTests(unittest.TestCase):
    NAMES = ["Researcher", "Travel Agent", "Chef", "Smart Trade", "Trader", "Cursor Cloud Session"]

    def setUp(self):
        self.saved = dict(roster.ROSTER)
        roster.ROSTER.clear()
        roster.ROSTER.update({name: {"name": name, "avatarColor": "red"} for name in self.NAMES})

    def tearDown(self):
        roster.ROSTER.clear()
        roster.ROSTER.update(self.saved)

    def test_ranking_prefix_then_word_start_then_subsequence(self):
        names = ["Xtra Bot", "Smart Trade", "Trader", "T r a"]
        ranked = [name for name, _at in mentions.rank("tra", names)]
        self.assertEqual(ranked[0], "Trader")
        self.assertEqual(ranked[1], "Smart Trade")
        self.assertLess(ranked.index("Smart Trade"), ranked.index("T r a"))
        # A tighter subsequence beats a looser one, then name order.
        self.assertEqual([n for n, _ in mentions.rank("ce", ["Cursor Cloud Session", "Chef", "Cache"])], ["Cache", "Chef", "Cursor Cloud Session"])
        self.assertEqual([n for n, _ in mentions.rank("zz", names)], [])

    def test_match_is_case_insensitive_and_reports_positions(self):
        self.assertEqual(mentions.match("TA", "Travel Agent")[1], [0, 2])
        self.assertEqual(mentions.match("tra", "Travel Agent")[1], [0, 1, 2])
        self.assertEqual(mentions.match("agent", "Travel Agent")[1], [7, 8, 9, 10, 11])
        self.assertEqual(mentions.match("tg", "Travel Agent")[1], [0, 8])
        self.assertEqual(len(mentions.rank("", self.NAMES + self.NAMES)), mentions.MAX_ROWS)

    def test_query_at_the_cursor(self):
        self.assertEqual(mentions.query_at("@tra", 4), (0, "tra"))
        self.assertEqual(mentions.query_at("hi @tra", 7), (3, "tra"))
        self.assertEqual(mentions.query_at("hi @", None), (3, ""))
        self.assertEqual(mentions.query_at("hi @tra there", 7), (3, "tra"))
        self.assertIsNone(mentions.query_at("a@b", 3))
        self.assertIsNone(mentions.query_at("mail me a@b", 11))
        self.assertIsNone(mentions.query_at("@tra ", 5))
        self.assertIsNone(mentions.query_at("hello", 5))

    def test_insert_replaces_the_query(self):
        self.assertEqual(mentions.insert("@tra", 4, "Travel Agent"), ("@Travel Agent ", 14))
        self.assertEqual(mentions.insert("hi @tra there", 7, "Travel Agent"), ("hi @Travel Agent  there", 17))
        self.assertEqual(mentions.insert("hi @", None, "Chef"), ("hi @Chef ", 9))

    def test_typing_opens_filters_and_excludes_this_bot(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@")
        self.assertTrue(chat.mention_open)
        names = [name for name, _ in chat.mention_matches()]
        self.assertNotIn("Researcher", names)
        self.assertEqual(len(names), 5)
        _typed(chat, "tra")
        self.assertEqual([name for name, _ in chat.mention_matches()][0], "Trader")
        self.assertTrue(chat.mention_open)

    def test_at_after_text_without_a_space_does_not_open(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "a@")
        self.assertFalse(chat.mention_open)
        chat = chat_state.Chat("Researcher")
        _typed(chat, "hi @")
        self.assertTrue(chat.mention_open)

    def test_bracketed_paste_does_not_open(self):
        chat = chat_state.Chat("Researcher")
        paint.PASTING = True
        try:
            keys = paint.decode_keys(list("@tra"))
        finally:
            paint.PASTING = False
        _typed(chat, keys)
        self.assertEqual(chat.typed, "@tra")
        self.assertFalse(chat.mention_open)

    def test_arrows_move_the_selection_in_both_forms(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@")
        self.assertEqual(chat.mention_sel, 0)
        for key in (curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_UP):
            _typed(chat, [key])
        self.assertEqual(chat.mention_sel, 1)
        # Down, Down, Up, Up in CSI and SS3 form: 1 -> 2 -> 3 -> 2 -> 1.
        for key in paint.decode_keys(list("\x1b[B\x1bOB\x1b[A\x1bOA")):
            _typed(chat, [key])
        self.assertEqual(chat.mention_sel, 1)
        self.assertEqual(chat.typed, "@")
        self.assertTrue(chat.mention_open)
        _typed(chat, [curses.KEY_UP, curses.KEY_UP])
        self.assertEqual(chat.mention_sel, len(chat.mention_matches()) - 1)

    def test_tab_inserts_the_selected_bot(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "hi @tra")
        self.assertEqual(chat_screen.chat_key(chat, "\t", None), None)
        self.assertEqual(chat.typed, "hi @Trader ")
        self.assertEqual(chat.pos, len(chat.typed))
        self.assertFalse(chat.mention_open)

    def test_enter_with_the_list_open_inserts_and_does_not_send(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@trav")
        result = chat_screen.chat_key(chat, "\r", None)
        self.assertIsNone(result)
        self.assertEqual(chat.typed, "@Travel Agent ")
        self.assertFalse(any(entry.get("local") for entry in chat.messages))
        self.assertFalse(chat.mention_open)

    def test_esc_closes_the_list_and_stays_in_the_chat(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@tra")
        self.assertIsNone(chat_screen.chat_key(chat, "\x1b", None))
        self.assertFalse(chat.mention_open)
        self.assertEqual(chat.typed, "@tra")
        self.assertEqual(chat_screen.chat_key(chat, "\x1b", None), "back")

    def test_backspace_space_and_no_match_close_the_list(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@t")
        _typed(chat, ["\x7f"])
        self.assertTrue(chat.mention_open)
        _typed(chat, ["\x7f"])
        self.assertFalse(chat.mention_open)
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@tra ")
        self.assertFalse(chat.mention_open)
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@zzz")
        self.assertFalse(chat.mention_open)

    def test_the_sent_text_is_the_plain_draft(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "see @che")
        _typed(chat, ["\t"])
        _typed(chat, "for that")
        self.assertEqual(chat.typed, "see @Chef for that")
        job = chat_screen.chat_key(chat, "\r", None)
        self.assertEqual(job, ("send", ("Researcher", "see @Chef for that")))

    def test_list_closed_keys_are_unchanged(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "hello")
        self.assertEqual(chat_screen.chat_key(chat, "\r", None), ("send", ("Researcher", "hello")))
        _typed(chat, "a\tb")
        self.assertEqual(chat.typed, "ab")

    def test_the_list_is_drawn_above_the_composer(self):
        chat = chat_state.Chat("Researcher")
        _typed(chat, "@tra")
        screen = FakeScreen(24, 80)
        chat_view.draw(screen, chat)
        found = {y: "".join(parts) for y, parts in screen.rows.items()}
        box = next(y for y, text in found.items() if text.startswith("┌"))
        self.assertTrue(any("Trader" in text for y, text in found.items() if y < box))
        self.assertTrue(any("Travel Agent" in text for y, text in found.items() if y < box))
        chat.mention_open = False
        screen = FakeScreen(24, 80)
        chat_view.draw(screen, chat)
        self.assertFalse(any("Trader" in "".join(parts) for parts in screen.rows.values()))

    def test_help_and_footer_mention_tagging(self):
        self.assertTrue(any(keys == "@" for keys, _what in chat_view.HELP))
        self.assertIn("@ tag a bot", grid.footer_spans("idle", "")[0][0])


class RequestCardTests(unittest.TestCase):
    """Secret, 1Password and form cards: read-only text, a state line, and `needs you`."""

    HANDLES = ("REQHANDLE", "ACCESSHANDLE", "ENTRYHANDLE", "onepassword://", "APPLINKHANDLE")

    def secret(self, **extra):
        entry = {
            "kind": "send-message",
            "id": "s1",
            "message": {
                "type": "secret-request",
                "secretRequest": {
                    "label": "GitHub token (repo + workflow)",
                    "description": "Needed to push the workflow",
                    "target": {"kind": "box-env", "name": "GH_TOKEN_WORKFLOW", "platform": "x", "field": "GH_TOKEN_WORKFLOW"},
                },
            },
            "timestampMs": 1794769353435,
            "requestId": "REQHANDLE",
            "seq": 18,
        }
        entry.update(extra)
        return entry

    def credential(self, expires=None, **extra):
        entry = {
            "kind": "send-message",
            "id": "c1",
            "message": {
                "type": "credential-request",
                "credentialRequest": {
                    "kind": "brokered-login",
                    "accessRequestId": "ACCESSHANDLE",
                    "goal": "buy loanso.me",
                    "entries": [
                        {"entryId": "ENTRYHANDLE", "website": "ionos.com", "reason": "sign in to ionos"},
                        {"entryId": "ENTRYHANDLE2", "website": "paypal.com", "reason": "pay"},
                    ],
                    "appLink": "onepassword://grant-brokered-access?token=APPLINKHANDLE",
                    "requestedAtMs": 1794664413981,
                    "expiresAtMs": expires if expires is not None else 1794665013981,
                },
            },
            "requestId": "REQHANDLE",
        }
        entry.update(extra)
        return entry

    def form(self, **extra):
        entry = {
            "kind": "send-message",
            "id": "f1",
            "message": {
                "type": "user-form",
                "formRequest": {
                    "secureValues": True,
                    "title": "Hetzner 2FA code",
                    "instruction": "",
                    "reason": "auth",
                    "domains": ["hetzner.com"],
                    "fields": [
                        {"id": "a", "label": "OTP code", "type": "otp", "required": True, "secret": True},
                        {"id": "b", "label": "Name", "type": "text"},
                    ],
                },
            },
            "requestId": "REQHANDLE",
            "formRequestId": "REQHANDLE",
            "formFieldOutcomes": [{"id": "a", "filled": True, "secureValueStorage": "VALUEHANDLE"}],
        }
        entry.update(extra)
        return entry

    def rows(self, entry, width=60):
        return grid.transcript_rows([entry], width)

    def clock(self, ms):
        old = cards_mod.now_ms
        cards_mod.now_ms = lambda: ms
        self.addCleanup(setattr, cards_mod, "now_ms", old)

    def test_a_pending_secret(self):
        self.assertEqual(
            self.rows(self.secret()),
            [
                ("card-info", "\U0001F511 secret: GitHub token (repo + workflow)"),
                ("card-info", "Needed to push the workflow"),
                ("card-info", "env: GH_TOKEN_WORKFLOW"),
                ("card", "waiting: enter it in the Grok Bot app"),
            ],
        )

    def test_a_provided_secret_is_green(self):
        rows = self.rows(self.secret(secretProvided=True))
        self.assertEqual(rows[-1], ("card-ok", "provided"))
        self.assertEqual(styles_mod.CARD_STYLE[rows[-1][0]], "green")
        self.assertEqual(styles_mod.CARD_STYLE["card"], "yellow")
        self.assertEqual({kind for kind, _text in rows[:-1]}, {"card-info"})

    def test_a_pending_credential_with_one_line_per_entry(self):
        self.clock(1794664413981)
        self.assertEqual(
            self.rows(self.credential()),
            [
                ("card-info", "1Password: buy loanso.me"),
                ("card-info", "ionos.com: sign in to ionos"),
                ("card-info", "paypal.com: pay"),
                ("card", "waiting: approve in 1Password or the Grok Bot app"),
            ],
        )

    def test_a_credential_states(self):
        self.clock(1794664413981)
        self.assertEqual(self.rows(self.credential(credentialResolution="approved"))[-1], ("card-ok", "approved"))
        self.assertEqual(self.rows(self.credential(credentialResolution="failed"))[-1], ("card-no", "failed"))
        self.assertEqual(self.rows(self.credential(credentialResolution="pending"))[-1][0], "card")

    def test_a_pending_credential_past_its_expiry_is_expired(self):
        self.clock(1794665013982)
        rows = self.rows(self.credential())
        self.assertEqual(rows[-1], ("card-no", "expired"))
        self.assertFalse(cards_mod.any_pending([self.credential()]))
        # A resolution wins over the clock.
        self.assertEqual(self.rows(self.credential(credentialResolution="approved"))[-1], ("card-ok", "approved"))
        self.clock(1794665013980)
        self.assertEqual(self.rows(self.credential())[-1][0], "card")

    def test_a_pending_form_lists_fields_without_values(self):
        rows = self.rows(self.form())
        self.assertEqual(
            rows,
            [
                ("card-info", "form: Hetzner 2FA code"),
                ("card-info", "OTP code (otp)"),
                ("card-info", "Name (text)"),
                ("card", "waiting: fill it in the Grok Bot app"),
            ],
        )

    def test_a_form_instruction_and_a_submitted_form(self):
        entry = self.form(formResolution="submitted")
        entry["message"]["formRequest"]["instruction"] = "Type the code from your phone"
        rows = self.rows(entry)
        self.assertEqual(rows[1], ("card-info", "Type the code from your phone"))
        self.assertEqual(rows[-1], ("card-ok", "submitted"))

    def test_no_handle_or_value_is_ever_shown_or_copied(self):
        self.clock(1794664413981)
        for entry in (
            self.secret(secretProvided=True),
            self.secret(),
            self.credential(),
            self.credential(credentialResolution="approved"),
            self.form(),
            self.form(formResolution="submitted"),
        ):
            text = transcript.entry_text(entry)
            shown = "\n".join(line for _kind, line in self.rows(entry))
            for handle in self.HANDLES + ("VALUEHANDLE",):
                self.assertNotIn(handle, text)
                self.assertNotIn(handle, shown)
            self.assertEqual(text.strip(), shown.strip())

    def test_entry_text_is_the_plain_card(self):
        self.assertEqual(
            transcript.entry_text(self.secret()),
            "\U0001F511 secret: GitHub token (repo + workflow)\nNeeded to push the workflow\n"
            "env: GH_TOKEN_WORKFLOW\nwaiting: enter it in the Grok Bot app",
        )
        self.assertTrue(transcript.entry_text(self.form(formResolution="submitted")).endswith("\nsubmitted"))
        self.assertIn("ionos.com: sign in to ionos", transcript.entry_text(self.credential(credentialResolution="failed")))

    def test_a_card_is_found_by_search_and_is_a_bot_line(self):
        rows = self.rows(self.secret())
        self.assertEqual(grid.search_hits(rows, "gh_token_workflow"), [2])
        self.assertFalse(transcript.is_user(self.secret()))

    def test_a_missing_or_odd_field_does_not_crash(self):
        for entry in (
            {"kind": "send-message", "message": {"type": "secret-request"}},
            {"kind": "send-message", "message": {"type": "credential-request", "credentialRequest": {"entries": "x"}}},
            {"kind": "send-message", "message": {"type": "user-form", "formRequest": {"fields": [1, {"label": 3}]}}},
            {"kind": "send-message", "message": {"type": "secret-request", "secretRequest": []}},
        ):
            self.assertTrue(self.rows(entry))
            self.assertTrue(transcript.entry_text(entry))

    def test_control_characters_in_a_label_are_flattened(self):
        entry = self.secret()
        entry["message"]["secretRequest"]["label"] = "tok\x1b]0;x\x07en"
        text = transcript.entry_text(entry)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_a_narrow_card_wraps_and_keeps_the_state_line_whole(self):
        rows = self.rows(self.secret(), width=24)
        self.assertTrue(all(len(line) <= 22 for _kind, line in rows))
        self.assertEqual(
            " ".join(line for kind, line in rows if kind == "card"),
            "waiting: enter it in the Grok Bot app",
        )

    def test_a_pending_card_needs_you_and_a_settled_one_does_not(self):
        self.clock(1794664413981)
        for pending, settled in (
            (self.secret(), self.secret(secretProvided=True)),
            (self.credential(), self.credential(credentialResolution="approved")),
            (self.credential(), self.credential(credentialResolution="failed")),
            (self.form(), self.form(formResolution="submitted")),
        ):
            self.assertTrue(transcript.needs_user([pending]))
            self.assertFalse(transcript.needs_user([settled]))
            snap = activity.judge_activity([pending], "x", "")
            self.assertEqual(snap["state"], "blocked")
            self.assertEqual(activity.status_label(snap["state"]), "needs you")
            self.assertEqual(activity.judge_activity([settled], "x", "")["state"], "idle")

    def test_an_expired_credential_does_not_need_you(self):
        self.clock(1794665013982)
        self.assertFalse(transcript.needs_user([self.credential()]))
        self.assertEqual(activity.judge_activity([self.credential()], "c1", "")["state"], "idle")

    def test_a_pending_card_followed_by_a_user_message_is_skipped(self):
        self.clock(1794664413981)
        user = {"id": "u", "kind": "message", "role": "user", "text": "ok done"}
        for pending in (self.secret(), self.credential(), self.form()):
            entries = [pending, {"id": "b", "role": "assistant", "text": "hm"}, user]
            self.assertFalse(transcript.needs_user(entries))
            self.assertFalse(cards_mod.any_pending(entries))
            self.assertIsNone(cards_mod.latest_pending(entries))
            snap = activity.judge_activity(entries, "u", "")
            self.assertNotEqual(snap["state"], "blocked")
            rows = grid.transcript_rows(entries, 60)
            self.assertIn(("card-no", "skipped"), rows)
            self.assertNotIn("card", [kind for kind, _text in rows])
        self.assertEqual(styles_mod.CARD_STYLE["card-no"], "gray")

    def test_the_local_echo_of_a_sent_message_skips_the_card_too(self):
        entries = [self.secret(), {"role": "user", "text": "later", "local": True}]
        self.assertFalse(transcript.needs_user(entries))
        self.assertIn(("card-no", "skipped"), grid.transcript_rows(entries, 60))

    def test_a_pending_card_with_only_bot_entries_after_it_is_still_pending(self):
        entries = [
            self.secret(),
            {"id": "b", "kind": "message", "role": "assistant", "text": "still waiting"},
            {"id": "t", "kind": "send-message", "message": {"type": "thinking"}},
        ]
        self.assertTrue(transcript.needs_user(entries))
        self.assertEqual(activity.judge_activity(entries, "t", "")["state"], "blocked")
        self.assertIn(("card", "waiting: enter it in the Grok Bot app"), grid.transcript_rows(entries, 60))
        # A user message before the card does not skip it.
        self.assertTrue(transcript.needs_user([{"id": "u", "role": "user", "text": "hi"}, self.secret()]))

    def test_only_the_cards_before_the_user_message_are_skipped(self):
        entries = [self.secret(), {"id": "u", "role": "user", "text": "x"}, self.form()]
        self.assertEqual([shown and shown["pending"] for shown in cards_mod.views(entries)], [False, None, True])
        self.assertEqual(cards_mod.latest_pending(entries)["title"], "form: Hetzner 2FA code")

    def test_a_resolved_or_expired_card_keeps_its_state_after_a_user_message(self):
        user = {"id": "u", "role": "user", "text": "x"}
        self.clock(1794664413981)
        self.assertEqual(cards_mod.view(self.secret(secretProvided=True), True)["state"], "provided")
        self.assertEqual(cards_mod.view(self.form(formResolution="submitted"), True)["tone"], "card-ok")
        self.assertEqual(cards_mod.view(self.credential(credentialResolution="failed"), True)["state"], "failed")
        self.assertEqual(cards_mod.view(self.credential(credentialResolution="approved"), True)["tone"], "card-ok")
        self.clock(1794665013982)
        self.assertEqual(grid.transcript_rows([self.credential(), user], 60)[3], ("card-no", "expired"))

    def test_a_skipped_card_does_not_force_a_full_read(self):
        chat = chat_state.Chat("bot")
        chat.loaded = True
        chat.cursor = "c"
        chat.messages = [self.secret()]
        self.assertEqual(chat.take_read(), ("bot",))
        chat.messages = [self.secret(), {"id": "u", "role": "user", "text": "x"}]
        self.assertEqual(chat.take_read(), ("bot", "c"))

    def test_the_list_preview_names_the_request(self):
        self.clock(1794664413981)
        self.assertEqual(
            activity.judge_activity([self.secret()], "s1", "")["preview"], "secret: GitHub token (repo + workflow)"
        )
        self.assertEqual(activity.judge_activity([self.credential()], "c1", "")["preview"], "1Password: buy loanso.me")
        self.assertEqual(activity.judge_activity([self.form()], "f1", "")["preview"], "form: Hetzner 2FA code")

    def test_a_new_pending_card_is_unread_worthy(self):
        self.assertTrue(activity.notable(self.secret()))
        self.assertTrue(activity.notable(self.form(formResolution="submitted")))

    def test_the_chat_blocks_and_unblocks_with_the_card(self):
        chat = chat_state.Chat("Researcher")
        page = transcript.parse_thread({"transcript": {"entries": [self.form()]}, "cursor": "f1"})
        chat.ingest(page, first=True)
        self.assertEqual(chat.phase, "blocked")
        self.assertEqual(activity.lookup_activity({"name": "Researcher"})["state"], "blocked")
        header = "".join(text for text, _style in paint.title_spans(chat.title, chat.phase, 60))
        self.assertIn("needs you", header)
        self.assertEqual(paint.title_spans(chat.title, "blocked", 60)[1][1], "yellow")
        self.assertEqual(grid.footer_spans(chat.phase, "")[0], ("Pending decision", "yellow"))
        # No y/n: nothing here can be answered from the terminal.
        self.assertIsNone(transcript.latest_decision(chat.messages))
        # While pending, the next read is the whole tail, so a settlement made in the app shows up.
        self.assertEqual(chat.take_read(), ("Researcher",))
        settled = self.form(formResolution="submitted")
        chat.ingest(transcript.parse_thread({"transcript": {"entries": [settled]}, "cursor": "f1"}))
        self.assertNotEqual(chat.phase, "blocked")
        self.assertEqual(chat.take_read(), ("Researcher", "f1"))
        self.assertEqual(activity.lookup_activity({"name": "Researcher"})["state"], "idle")

    def test_herdr_reports_blocked_for_a_pending_card(self):
        seen = []
        old = herdr.publish
        herdr.publish = lambda phase, *args: seen.append(phase)
        self.addCleanup(setattr, herdr, "publish", old)
        chat = chat_state.Chat("Researcher")
        chat.ingest(transcript.parse_thread({"transcript": {"entries": [self.secret()]}, "cursor": "s1"}), first=True)
        self.assertEqual(seen[-1], "blocked")
        self.assertEqual(herdr.herdr_state("blocked"), "blocked")
        chat.ingest(transcript.parse_thread({"transcript": {"entries": [self.secret(secretProvided=True)]}, "cursor": "s1"}))
        self.assertNotEqual(seen[-1], "blocked")

    def test_keys_are_not_swallowed_by_a_request_card(self):
        chat = chat_state.Chat("Researcher")
        chat.ingest(transcript.parse_thread({"transcript": {"entries": [self.secret()]}, "cursor": "s1"}), first=True)
        self.assertEqual(chat_screen.composer_key(chat, "y"), ("type", "y"))

    def test_the_card_draws_in_the_chat(self):
        chat = chat_state.Chat("Researcher")
        chat.ingest(transcript.parse_thread({"transcript": {"entries": [self.secret()]}, "cursor": "s1"}), first=True)
        screen = FakeScreen(24, 80)
        chat_view.draw(screen, chat)
        text = "\n".join("".join(parts) for _y, parts in sorted(screen.rows.items()))
        self.assertIn("secret: GitHub token", text)
        self.assertIn("env: GH_TOKEN_WORKFLOW", text)
        self.assertIn("waiting: enter it in the Grok Bot app", text)
        self.assertIn("needs you", text)


class MediaTests(unittest.TestCase):
    IMAGE = {
        "url": "file:///home/box/agent-data/agents/a1/assets/4f9c0de1",
        "alt": "terminal proof",
        "width": 780,
        "height": 1688,
    }

    def image_entry(self, **image):
        shown = dict(self.IMAGE, **image)
        shown = {key: value for key, value in shown.items() if value is not None}
        return {
            "id": "i1",
            "kind": "send-message",
            "message": {"type": "text", "content": "here is the proof", "images": [shown]},
        }

    def page_entry(self, content):
        return {"id": "p1", "kind": "send-message", "message": {"type": "text", "content": content}}

    def rows(self, entry, width=60):
        return grid.transcript_rows([entry], width)

    def plain(self, entry, width=60):
        return [(kind, markdown.line_plain(text)) for kind, text in self.rows(entry, width)]

    def test_image_is_one_dim_line_after_the_text(self):
        rows = self.rows(self.image_entry())
        self.assertEqual([kind for kind, _text in rows], ["text", "text"])
        self.assertEqual(rows[0][1], "here is the proof")
        self.assertEqual(rows[1][1], [("\U0001F5BC terminal proof (780x1688)", "dim")])

    def test_image_without_alt_uses_the_file_name_and_never_the_path(self):
        for alt in (None, "", "   "):
            lines = transcript.image_lines(self.image_entry(alt=alt))
            self.assertEqual(lines, ["\U0001F5BC 4f9c0de1 (780x1688)"])
        text = "\n".join(text for _kind, text in self.plain(self.image_entry(alt=None)))
        self.assertNotIn("file://", text)
        self.assertNotIn("/home/box", text)
        self.assertNotIn("agent-data", text)

    def test_image_without_size_or_url_still_shows_a_line(self):
        entry = self.image_entry(width=None, height=None)
        self.assertEqual(transcript.image_lines(entry), ["\U0001F5BC terminal proof"])
        entry = self.image_entry(url=None, alt=None, width=None, height=None)
        self.assertEqual(transcript.image_lines(entry), ["\U0001F5BC image"])

    def test_an_image_only_message_is_not_empty_anywhere(self):
        entry = self.image_entry()
        entry["message"]["content"] = ""
        placeholder = "\U0001F5BC terminal proof (780x1688)"
        self.assertEqual(transcript.entry_text(entry), placeholder)
        self.assertEqual(activity.activity_preview([entry], "idle"), placeholder)
        self.assertIn(placeholder, self.plain(entry)[0][1])
        self.assertEqual(transcript.watch_step("working", [entry], 0, False, False)[2], True)
        chat = chat_state.Chat("Researcher")
        chat.messages = [entry]
        written = []
        old = terminal.write_tty
        terminal.write_tty = written.append
        self.addCleanup(setattr, terminal, "write_tty", old)
        self.assertTrue(chat_screen.copy_last_reply(chat))
        self.assertIn(base64.b64encode(placeholder.encode("utf-8")), written[0])

    def test_activity_preview_keeps_text_and_placeholder(self):
        self.assertEqual(
            activity.activity_preview([self.image_entry()], "idle"),
            "here is the proof \U0001F5BC terminal proof (780x1688)",
        )

    def test_a_user_image_stays_in_the_bubble(self):
        entry = self.image_entry()
        entry["kind"] = "user"
        kinds = {kind for kind, _text in self.rows(entry)}
        self.assertEqual(kinds, {"user"})

    def test_attachment_with_a_file_url_shows_name_alt_and_size_only(self):
        entry = {
            "id": "a1",
            "kind": "send-message",
            "message": {
                "type": "attachment",
                "url": "file:///workspace/report.pdf",
                "file_name": "report.pdf",
                "alt": "quarterly numbers",
                "width": 612,
                "height": 792,
            },
        }
        self.assertEqual(transcript.entry_text(entry), "\U0001F4CE report.pdf (quarterly numbers, 612x792)")
        self.assertNotIn("file://", "".join(text for _kind, text in self.plain(entry)))

    def test_attachment_without_extras_or_name(self):
        message = {"type": "attachment", "url": "file:///workspace/report.pdf"}
        entry = {"id": "a1", "kind": "send-message", "message": message}
        self.assertEqual(transcript.entry_text(entry), "\U0001F4CE report.pdf")
        self.assertEqual(transcript.attachment_text({}), "\U0001F4CE attachment")

    def test_attachment_with_an_https_url_keeps_a_clickable_link(self):
        url = "https://files.example.com/report.pdf"
        entry = {
            "id": "a1",
            "kind": "send-message",
            "message": {"type": "attachment", "url": url, "file_name": "report.pdf"},
        }
        self.assertEqual(transcript.entry_text(entry), f"\U0001F4CE report.pdf {url}")
        (_kind, line), = self.rows(entry, 80)
        link = [text for text, style in line if style == "link"]
        self.assertEqual(link, [url])
        screen = FakeScreen(24, 80)
        chat = chat_state.Chat("Researcher")
        chat.messages = [entry]
        chat_view.draw(screen, chat)
        self.assertTrue(any(hit[3] == url for hit in terminal.LINK_HITS))

    def test_the_old_user_attachment_is_unchanged(self):
        entry = {"kind": "user-attachment", "id": "a", "file_name": "p.jpg", "url": "file:///x/p.jpg",
                 "secondaryText": "649kB", "width": 10, "height": 20}
        self.assertEqual(transcript.entry_text(entry), "\U0001F4CE p.jpg (649kB, 10x20)")

    def test_a_page_link_gets_one_card_after_the_body(self):
        entry = self.page_entry("the plan is ready: [Extrablatt revamp](https://cursor.com/canvas/store-x/abc)")
        rows = self.rows(entry, 80)
        self.assertEqual([kind for kind, _text in rows], ["text", "gap", "card-page"])
        body = markdown.line_plain(rows[0][1])
        self.assertIn("the plan is ready: Extrablatt revamp", body)
        self.assertIn("https://cursor.com/canvas/store-x/abc", body)
        self.assertEqual(rows[2][1], "\U0001F4C4 page: Extrablatt revamp")
        self.assertEqual(rows[2][1].url, "https://cursor.com/canvas/store-x/abc")
        self.assertIn("card-page", styles_mod.CARD_STYLE)

    def test_one_card_per_distinct_page_url(self):
        first = "[One](https://cursor.com/canvas/s/a)"
        second = "[Two](https://cursor.com/canvas/s/b)"
        again = "[One again](https://cursor.com/canvas/s/a)"
        entry = self.page_entry(f"{first} and {second} and {again}")
        self.assertEqual(
            transcript.pages(entry),
            [("One", "https://cursor.com/canvas/s/a"), ("Two", "https://cursor.com/canvas/s/b")],
        )
        cards = [text for kind, text in self.rows(entry, 80) if kind == "card-page"]
        self.assertEqual(cards, ["\U0001F4C4 page: One", "\U0001F4C4 page: Two"])

    def test_only_canvas_links_are_pages(self):
        for content in (
            "[Docs](https://example.com/canvas/s/a)",
            "[Canvas](https://cursor.com/docs/canvas)",
            "[Insecure](http://cursor.com/canvas/s/a)",
            "[Short](https://cursor.com/canvas/s)",
            "plain https://cursor.com/canvas/s/a url",
        ):
            self.assertEqual(transcript.pages(self.page_entry(content)), [], content)
        self.assertEqual(transcript.pages({"role": "user", "text": "[A](https://cursor.com/canvas/s/a)"}), [])

    def test_the_page_card_opens_through_the_link_machinery(self):
        entry = self.page_entry("see [Plan](https://cursor.com/canvas/store-x/abc)")
        chat = chat_state.Chat("Researcher")
        chat.messages = [entry]
        screen = FakeScreen(24, 80)
        chat_view.draw(screen, chat)
        urls = [hit[3] for hit in terminal.LINK_HITS]
        self.assertEqual(urls.count("https://cursor.com/canvas/store-x/abc"), 2)
        card = next(hit for hit in terminal.LINK_HITS if hit[2] - hit[1] <= len("\U0001F4C4 page: Plan") + 1)
        opened = []
        old = terminal.subprocess.Popen
        terminal.subprocess.Popen = lambda argv, **_kw: opened.append(argv)
        self.addCleanup(setattr, terminal.subprocess, "Popen", old)
        self.assertTrue(terminal.open_link_at(card[1], card[0]))
        self.assertEqual(opened, [["xdg-open", "https://cursor.com/canvas/store-x/abc"]])
        text = "\n".join("".join(parts) for _y, parts in sorted(screen.rows.items()))
        self.assertIn("\U0001F4C4 page: Plan", text)


if __name__ == "__main__":
    unittest.main()
