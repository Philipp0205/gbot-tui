import base64
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

import gbot_tui

_HOME = {}


def setUpModule():
    # The roster and thread caches live under ~/.cache. Keep a real one out of the tests.
    _HOME["old"] = os.environ.get("HOME")
    _HOME["dir"] = tempfile.TemporaryDirectory()
    os.environ["HOME"] = _HOME["dir"].name
    # publish() writes OSC to the terminal. Keep that out of the test log.
    _HOME["write_tty"] = gbot_tui.write_tty
    gbot_tui.write_tty = lambda data: None


def tearDownModule():
    gbot_tui.write_tty = _HOME["write_tty"]
    if _HOME["old"] is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _HOME["old"]
    _HOME["dir"].cleanup()


class ParseTests(unittest.TestCase):
    def test_entry_text_and_user(self):
        self.assertEqual(gbot_tui.entry_text({"text": "hi"}), "hi")
        self.assertEqual(gbot_tui.entry_text({"message": {"content": "there"}}), "there")
        self.assertTrue(gbot_tui.is_user({"role": "user", "text": "hi"}))
        self.assertFalse(gbot_tui.is_user({"role": "assistant", "text": "yo"}))

    def test_thread_delta(self):
        page = gbot_tui.parse_thread(
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
        page = gbot_tui.parse_thread(
            {"target": {"id": "bot-1", "name": "Outfitly", "avatarShape": "heart", "avatarColor": "brown"}}
        )
        self.assertEqual(page["avatar_shape"], "heart")
        self.assertEqual(page["avatar_color"], "brown")
        self.assertEqual(gbot_tui.program_key(page["id"]), "gbot-bot-1")
        # A full bot id used to make app= longer than 32 bytes, so Herdr dropped the report.
        key = gbot_tui.program_key("ea566f99-aded-40c2-99fc-60e5477d2a61")
        self.assertLessEqual(len(key), 32)
        self.assertNotEqual(key, gbot_tui.program_key("aa566f99-aded-40c2-99fc-60e5477d2a61"))
        self.assertEqual(gbot_tui.avatar_color("brown"), "#936439")
        self.assertEqual(
            gbot_tui.client_version_from_name("Grok_Bot_0.78.1_abc.AppImage"),
            "0.78.1",
        )

    def test_gbot_error(self):
        with self.assertRaises(gbot_tui.GbotError):
            gbot_tui.parse_gbot('{"error":{"message":"no session"}}')

    def test_json_flag_follows_the_subcommand(self):
        self.assertEqual(
            gbot_tui.gbot_argv(("bots", "list")),
            ["gbot", "bots", "list", "--gateway", "--json"],
        )
        self.assertEqual(
            gbot_tui.gbot_argv(("send", "--", "Researcher", "hi")),
            ["gbot", "send", "--gateway", "--json", "--", "Researcher", "hi"],
        )
        self.assertEqual(
            gbot_tui.gbot_argv(
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
            gbot_tui.gbot_failure("", "Unknown option: --json.\n"),
            "Unknown option: --json.",
        )
        self.assertEqual(
            gbot_tui.gbot_failure("", '{"error":{"message":"no session"}}'),
            "no session",
        )


class WatchTests(unittest.TestCase):
    def test_reply_then_quiet_is_done(self):
        reply = [{"role": "assistant", "text": "shipped"}]
        phase, quiet, saw = gbot_tui.watch_step("working", reply, 0, False, False)
        self.assertEqual(phase, "working")
        self.assertTrue(saw)
        phase, quiet, saw = gbot_tui.watch_step(phase, [], quiet, saw, False)
        self.assertEqual(phase, "working")
        phase, quiet, saw = gbot_tui.watch_step(phase, [], quiet, saw, False)
        self.assertEqual((phase, quiet, saw), ("done", 0, False))

    def test_pending_approval_blocks(self):
        entries = [
            {
                "kind": "send-message",
                "message": {"type": "auto-review-approval", "approval": {"status": "pending"}},
            }
        ]
        self.assertTrue(gbot_tui.pending_approval(entries))
        phase, _, _ = gbot_tui.watch_step("working", [], 2, True, True)
        self.assertEqual(phase, "blocked")


class NameTests(unittest.TestCase):
    def test_parse_list_and_name(self):
        self.assertEqual(gbot_tui.parse_args(["--list"]), ("list", ""))
        self.assertEqual(gbot_tui.parse_args(["-l"]), ("list", ""))
        self.assertEqual(gbot_tui.parse_args(["--overview"]), ("overview", ""))
        self.assertEqual(gbot_tui.parse_args(["-o"]), ("overview", ""))
        self.assertEqual(gbot_tui.parse_args(["--complete"]), ("complete", ""))
        self.assertEqual(gbot_tui.parse_args(["Homescreen", "Manager"]), ("open", "Homescreen Manager"))
        self.assertEqual(gbot_tui.parse_args([]), ("open", ""))

    def test_match_exact_prefix_and_ambiguous(self):
        names = ["Outfitly", "Homescreen Manager", "Nutritionist", "Night Watch"]
        self.assertEqual(gbot_tui.match_bot("outfitly", names), "Outfitly")
        self.assertEqual(gbot_tui.match_bot("Home", names), "Homescreen Manager")
        with self.assertRaises(gbot_tui.GbotError) as error:
            gbot_tui.match_bot("n", names)
        self.assertIn("Nutritionist", str(error.exception))
        self.assertIn("Night Watch", str(error.exception))
        with self.assertRaises(gbot_tui.GbotError):
            gbot_tui.match_bot("missing", names)


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
            made = gbot_tui.install_avatars(
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
            previous = gbot_tui.APP
            try:
                gbot_tui.show_avatar(
                    {"id": "abc", "name": "Outfitly", "avatar_shape": "heart", "avatar_color": "red"},
                    root=tmp,
                    src=tmp,
                )
                self.assertEqual(gbot_tui.APP, "Outfitly")
                report = gbot_tui.osc_report("idle", "Outfitly")
                self.assertIn("app=Outfitly", report)
                self.assertNotIn("app=gbot-abc", report)
                self.assertLessEqual(len(gbot_tui.APP.encode()), 32)
            finally:
                gbot_tui.APP = previous
            programs = __import__("json").loads((root / "programs.json").read_text())
            self.assertEqual(programs["programs"]["gbot-abc"]["icon"], made["gbot-abc"]["icon"])
            self.assertEqual(programs["programs"]["gbot-abc"]["name"], "Outfitly")
            self.assertIn("Outfitly", programs["programs"]["gbot-abc"]["match"])
            self.assertIn("gbot-abc", programs["programs"]["gbot-abc"]["match"])
            spaced = gbot_tui.install_avatars(
                [{"id": "b3", "name": "Cursor Cloud Session", "avatarShape": "heart", "avatarColor": "red"}],
                root=tmp,
                src=tmp,
            )
            self.assertEqual(gbot_tui.notice_name("Cursor Cloud Session"), "Cursor-Cloud-Session")
            self.assertEqual(spaced["gbot-b3"]["name"], "Cursor Cloud Session")
            self.assertIn("Cursor-Cloud-Session", spaced["gbot-b3"]["match"])
            self.assertIn("gbot-b3", spaced["gbot-b3"]["match"])
            self.assertTrue(all(" " not in item and "\u00a0" not in item for item in spaced["gbot-b3"]["match"]))
            long_id = "ea566f99-aded-40c2-99fc-60e5477d2a61"
            long_made = gbot_tui.install_avatars(
                [{"id": long_id, "name": "Researcher", "avatarShape": "heart", "avatarColor": "red"}],
                root=tmp,
                src=tmp,
            )
            compact = gbot_tui.program_key(long_id)
            full = "gbot-" + long_id
            self.assertLessEqual(len(compact), 32)
            self.assertGreater(len(full), 32)
            self.assertEqual(long_made[compact]["icon"], long_made[full]["icon"])
            self.assertFalse(long_made[compact]["tint"])
            programs = __import__("json").loads((root / "programs.json").read_text())
            self.assertEqual(programs["programs"][compact]["icon"], programs["programs"][full]["icon"])
            self.assertFalse(programs["programs"][compact]["tint"])
            self.assertEqual(programs["programs"]["gbot-abc"]["icon"], made["gbot-abc"]["icon"])
            previous = gbot_tui.APP
            try:
                gbot_tui.show_avatar(
                    {
                        "id": long_id,
                        "name": "Researcher",
                        "avatar_shape": "heart",
                        "avatar_color": "red",
                    },
                    root=tmp,
                    src=tmp,
                )
                self.assertEqual(gbot_tui.APP, "Researcher")
                report = gbot_tui.osc_report("working", "Researcher")
                self.assertIn("app=Researcher", report)
                self.assertLessEqual(len(gbot_tui.APP.encode()), 32)
                self.assertIn("Researcher", programs["programs"][compact]["match"])
            finally:
                gbot_tui.APP = previous

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
                made = gbot_tui.install_avatars(
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
            self.assertEqual(gbot_tui.bot_avatar({"id": agent_id}), pair)
            self.assertEqual(gbot_tui.bot_avatar({"agentId": agent_id}), pair)

        self.assertEqual(
            gbot_tui.bot_avatar({"id": "abc", "avatarShape": "heart", "avatarColor": "Red"}),
            ("heart", "red"),
        )
        self.assertEqual(
            gbot_tui.bot_avatar({"id": "abc", "avatar_shape": "heart", "avatar_color": "red"}),
            ("heart", "red"),
        )
        self.assertEqual(gbot_tui.bot_avatar({"id": "abc", "avatarColor": "red"}), ("hex", "red"))
        self.assertEqual(
            gbot_tui.bot_avatar({"id": "abc", "avatarShape": "", "avatarColor": "red"}),
            ("hex", "red"),
        )
        self.assertEqual(gbot_tui.bot_avatar({"id": "abc", "avatarShape": "heart"}), ("heart", "cyan"))
        self.assertEqual(
            gbot_tui.bot_avatar({"id": "a1", "avatarShape": "heart", "avatarColor": ""}),
            ("heart", "gray"),
        )
        self.assertEqual(gbot_tui.bot_avatar({"id": "abc"}), ("hex", "cyan"))
        self.assertEqual(
            gbot_tui.bot_avatar({"id": "ea566f99-aded-40c2-99fc-60e5477d2a61", "avatarShape": "", "avatarColor": ""}),
            ("pebble", "brown"),
        )
        self.assertEqual(
            gbot_tui.bot_avatar({"id": "abc", "avatarShape": "nope", "avatarColor": "plaid"}),
            ("hex", "cyan"),
        )
        self.assertEqual(
            gbot_tui.bot_avatar({"id": "🤖", "avatarShape": "heart!", "avatarColor": "#ff0000"}),
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
                made = gbot_tui.install_avatars([bot], root=tmp, src=tmp)
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
            self.assertTrue(gbot_tui.dark_terminal())
            self.assertEqual(gbot_tui.user_bubble(), 235)
            os.environ["COLORFGBG"] = "0;15"
            self.assertFalse(gbot_tui.dark_terminal())
            self.assertEqual(gbot_tui.user_bubble(), 254)
        finally:
            if old is None:
                os.environ.pop("COLORFGBG", None)
            else:
                os.environ["COLORFGBG"] = old

    def test_composer_matches_the_cursor_box(self):
        top, mid, bot = gbot_tui.composer_lines(32, "")
        self.assertTrue(top.startswith("┌") and top.endswith("┐"))
        self.assertTrue(bot.startswith("└") and bot.endswith("┘"))
        self.assertIn("→  Add a follow-up", mid)
        _top, typed, _bot = gbot_tui.composer_lines(32, "hello")
        self.assertIn("hello", typed)
        self.assertNotIn("Add a follow-up", typed)
        wide, _middle, wide_bottom = gbot_tui.composer_lines(58, "")
        self.assertEqual(len(wide), 58)
        self.assertEqual(len(wide_bottom), 58)
        self.assertEqual(gbot_tui.composer_cursor_column("", True), 4)

    def test_long_draft_wraps_and_the_box_grows(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        lines = gbot_tui.draft_lines(text, 20)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), text)
        width = gbot_tui.composer_text_width(20)
        self.assertTrue(all(len(line) <= width for line in lines))
        _header, short_body, short_box, _footer = gbot_tui.layout(24, 1)
        _header, long_body, long_box, _footer = gbot_tui.layout(24, 4)
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
            [bot["name"] for bot in gbot_tui.fuzzy_bots("", bots)],
            ["Nutritionist", "Night Watch", "Outfitly"],
        )
        self.assertEqual(gbot_tui.fuzzy_bots("otf", bots)[0]["name"], "Outfitly")
        self.assertEqual(
            [bot["name"] for bot in gbot_tui.fuzzy_bots("n", bots)],
            ["Nutritionist", "Night Watch"],
        )
        self.assertEqual(gbot_tui.fuzzy_bots("clothes", bots)[0]["name"], "Outfitly")
        self.assertEqual(gbot_tui.fuzzy_bots("zzz", bots), [])
        spans = gbot_tui.picker_spans(
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
        rows = gbot_tui.transcript_rows(
            [
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "yo"},
            ],
            40,
        )
        self.assertEqual([kind for kind, _text in rows], ["user", "gap", "text"])
        self.assertGreater(gbot_tui.bubble_column(40, "hi"), 20)

    def test_pending_card_reads_needs_approval(self):
        rows = gbot_tui.transcript_rows(
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
        rows = gbot_tui.transcript_rows([entry], 40)
        texts = [text for kind, text in rows if kind == "card"]
        self.assertEqual(texts[0], "Needs approval")
        self.assertIn("npm publish", texts)
        self.assertIn("ship the package", texts)
        self.assertTrue(all(not line.startswith(" ") for line in texts))
        card = gbot_tui.latest_decision([entry])
        self.assertEqual((card["entry_id"], card["request_id"]), ("card", "ask"))

    def test_a_decision_updates_the_card_and_will_not_send_twice(self):
        chat = gbot_tui.Chat("Researcher")
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
        texts = [text for kind, text in gbot_tui.transcript_rows(chat.messages, 40) if kind == "card-ok"]
        self.assertEqual(texts[0], "Approved")
        self.assertIn("npm publish", texts)
        self.assertIsNone(gbot_tui.latest_decision(chat.messages, chat.answered))
        self.assertFalse(gbot_tui.pending_approval(chat.messages))
        self.assertEqual(chat.phase, "idle")
        self.assertIsNone(chat.begin_decision("decline"))
        declined = gbot_tui.Chat("Researcher")
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
        rows = gbot_tui.transcript_rows(declined.messages, 40)
        self.assertEqual(rows, [("card-no", "Declined")])

    def test_a_failed_decision_can_be_retried(self):
        chat = gbot_tui.Chat("Researcher")
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
        chat = gbot_tui.Chat("Researcher")
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
        chat = gbot_tui.Chat("Researcher")
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
        stale = gbot_tui.parse_thread(
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
        texts = [text for kind, text in gbot_tui.transcript_rows(chat.messages, 40) if kind == "card-ok"]
        self.assertEqual(texts[0], "Approved")
        self.assertIsNone(chat.begin_decision("decline"))
        fresh = gbot_tui.parse_thread(
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
        texts = [text for kind, text in gbot_tui.transcript_rows(chat.messages, 40) if kind == "card-ok"]
        self.assertEqual(texts[0], "Approved")

    def test_an_updated_entry_replaces_the_same_id(self):
        chat = gbot_tui.Chat("Researcher")
        chat.messages = [{"id": "m1", "role": "assistant", "text": "old"}]
        chat.cursor = "m1"
        page = gbot_tui.parse_thread(
            {
                "transcript": {"entries": [{"id": "m1", "role": "assistant", "text": "new"}]},
                "cursor": "m1",
            }
        )
        chat.apply(page, replace=False)
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(gbot_tui.entry_text(chat.messages[0]), "new")
        extra = gbot_tui.parse_thread(
            {
                "transcript": {"entries": [{"id": "m2", "role": "assistant", "text": "later"}]},
                "cursor": "m2",
            }
        )
        chat.apply(extra, replace=False)
        self.assertEqual(
            [gbot_tui.entry_text(entry) for entry in chat.messages],
            ["new", "later"],
        )

    def test_red_and_yellow_list_dots_use_the_bot_color(self):
        sentinel = object()
        warn = object()
        saved_text = dict(gbot_tui.TEXT_ATTR)
        saved_red = gbot_tui.ATTR["red"]
        try:
            gbot_tui.TEXT_ATTR["red"] = sentinel
            gbot_tui.TEXT_ATTR["yellow"] = sentinel
            gbot_tui.ATTR["red"] = warn
            self.assertIs(gbot_tui.span_attr("bot:red"), sentinel)
            self.assertIs(gbot_tui.span_attr("bot:yellow"), sentinel)
            self.assertIs(gbot_tui.span_attr("red"), warn)
            self.assertIs(gbot_tui.span_attr("bold"), gbot_tui.ATTR["bold"])
            self.assertIs(gbot_tui.dot_attr("red"), gbot_tui.span_attr("bot:red"))
            self.assertIs(gbot_tui.dot_attr("red"), sentinel)
            self.assertIsNot(gbot_tui.dot_attr("red"), gbot_tui.avatar_attr("red"))
        finally:
            gbot_tui.TEXT_ATTR.clear()
            gbot_tui.TEXT_ATTR.update(saved_text)
            gbot_tui.ATTR["red"] = saved_red
        spans = gbot_tui.picker_spans({"name": "Alert", "title": "", "avatarColor": "red"}, True, 40)
        self.assertEqual(spans[1], ("● ", "bot:red"))
        yellow = gbot_tui.picker_spans(
            {"name": "Alert", "title": "", "avatarColor": "yellow"}, True, 40
        )
        self.assertEqual(yellow[1], ("● ", "bot:yellow"))

    def test_a_black_dot_uses_the_lifted_color(self):
        shown = gbot_tui.xterm_hex(gbot_tui.xterm_color(gbot_tui.readable_hex("#000000", True, "#000000")))
        self.assertNotEqual(shown.upper(), "#000000")
        self.assertGreaterEqual(gbot_tui.contrast(shown, "#000000"), 7)
        sentinel = object()
        saved = dict(gbot_tui.TEXT_ATTR)
        try:
            gbot_tui.TEXT_ATTR["black"] = sentinel
            self.assertIs(gbot_tui.dot_attr("black"), sentinel)
            self.assertIs(gbot_tui.bot_text_attr("black"), sentinel)
            self.assertIs(gbot_tui.span_attr("bot:black"), sentinel)
        finally:
            gbot_tui.TEXT_ATTR.clear()
            gbot_tui.TEXT_ATTR.update(saved)
        spans = gbot_tui.picker_spans({"name": "Ink", "title": "", "avatarColor": "black"}, True, 40)
        self.assertEqual(spans[1], ("● ", "bot:black"))

    def test_wrap_keeps_words_together(self):
        self.assertEqual(gbot_tui.wrap("right now", 7), ["right", "now"])
        self.assertEqual(gbot_tui.wrap("your turn", 4), ["your", "turn"])
        self.assertEqual(gbot_tui.wrap("Bedrock's next", 12), ["Bedrock's", "next"])
        self.assertEqual(gbot_tui.draft_lines("before I review", 20), ["before I", "review"])
        self.assertEqual(
            gbot_tui.wrap("abcdefghijklmnopqrstuvwxyz", 10),
            ["abcdefghij", "klmnopqrst", "uvwxyz"],
        )
        self.assertTrue(all(not line.startswith(" ") for line in gbot_tui.wrap("hello world", 6)))
        self.assertEqual(gbot_tui.wrap("- hello world", 10), ["- hello", "  world"])
        self.assertEqual(gbot_tui.wrap("- short", 20), ["- short"])

    def test_red_text_is_lighter_on_black(self):
        original = gbot_tui.avatar_color("red")
        self.assertEqual(original, "#FF263C")
        lifted = gbot_tui.readable_hex(original, dark=True)
        self.assertGreater(gbot_tui.contrast_on_black(lifted), gbot_tui.contrast_on_black(original))
        self.assertGreaterEqual(gbot_tui.contrast_on_black(lifted), 7)
        darkened = gbot_tui.readable_hex(original, dark=False)
        self.assertGreaterEqual(gbot_tui.contrast(darkened, "#FFFFFF"), 4.5)

    def test_footer_states(self):
        self.assertEqual(gbot_tui.footer_spans("blocked", "")[0], ("Pending decision", "yellow"))
        working = "".join(text for text, _style in gbot_tui.footer_spans("working", ""))
        self.assertIn("Generating", working)
        self.assertEqual(gbot_tui.footer_spans("idle", "no session")[0][1], "red")
        blocked = gbot_tui.footer_spans("blocked", "", decide=True)
        self.assertEqual(blocked[0], ("Pending decision", "yellow"))
        self.assertIn("approve", blocked[1][0])
        self.assertIn("decline", blocked[1][0])
        failed = gbot_tui.footer_spans("blocked", "gateway down", decide=True)
        self.assertEqual(failed[0], ("gateway down", "red"))
        self.assertIn("approve", failed[1][0])
        self.assertIn("decline", failed[1][0])
        roomy = gbot_tui.footer_spans("blocked", "gateway down", decide=True, width=80)
        self.assertIn("approve", roomy[-1][0])

    def test_a_long_error_stays_readable_in_a_narrow_footer(self):
        short = "EnsureSandBox failed: 401 Unauthorized"
        rows = gbot_tui.footer_rows("blocked", short, decide=True, width=58)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], (short, "red"))
        self.assertEqual(rows[0][1][0].strip(), "y/n")
        self.assertLessEqual(sum(len(text) for text, _style in rows[0]), 56)
        long = "the approval gateway rejected this decision because the card changed (401)"
        rows = gbot_tui.footer_rows("blocked", long, decide=True, width=58)
        self.assertGreater(len(rows), 1)
        shown = " ".join(text for row in rows[:-1] for text, _style in row)
        self.assertEqual(shown, long)
        self.assertTrue(all(style == "red" for row in rows[:-1] for _text, style in row))
        self.assertTrue(all(sum(len(text) for text, _style in row) <= 56 for row in rows))
        self.assertIn("y", "".join(text for text, _style in rows[-1]))
        self.assertEqual(rows[-1][0], ("Pending decision", "yellow"))
        idle = gbot_tui.footer_rows("idle", long, width=58)
        self.assertEqual(" ".join(text for row in idle[:-1] for text, _style in row), long)
        self.assertEqual(idle[-1], gbot_tui.footer_spans("idle", ""))

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
        self.assertEqual(gbot_tui.card_row_kind(pending), "card")
        self.assertEqual(gbot_tui.card_row_kind(approved), "card-ok")
        self.assertEqual(gbot_tui.card_row_kind(declined), "card-no")
        self.assertEqual(gbot_tui.CARD_STYLE["card"], "yellow")
        self.assertEqual(gbot_tui.CARD_STYLE["card-ok"], "green")
        self.assertEqual(gbot_tui.CARD_STYLE["card-no"], "gray")
        self.assertNotEqual(gbot_tui.ATTR["green"], gbot_tui.ATTR["yellow"])

    def test_y_types_unless_the_card_can_be_answered(self):
        chat = gbot_tui.Chat("Researcher")
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
        self.assertEqual(gbot_tui.composer_key(chat, "y"), ("type", "y"))
        self.assertEqual(gbot_tui.composer_key(chat, "n"), ("type", "n"))
        chat.messages[0]["id"] = "card"
        chat.messages[0]["message"]["approval"]["requestId"] = "ask"
        self.assertEqual(gbot_tui.composer_key(chat, "y"), ("decide", "accept"))
        self.assertEqual(gbot_tui.composer_key(chat, "n"), ("decide", "decline"))
        self.assertEqual(gbot_tui.composer_key(chat, "Y"), ("type", "Y"))
        self.assertEqual(gbot_tui.composer_key(chat, "N"), ("type", "N"))
        self.assertEqual(gbot_tui.composer_key(chat, "\x19"), ("type", "y"))
        self.assertEqual(gbot_tui.composer_key(chat, "\x0e"), ("type", "n"))
        chat.typed = "a"
        self.assertEqual(gbot_tui.composer_key(chat, "y"), ("type", "y"))
        chat.typed = ""
        chat.deciding = True
        self.assertEqual(gbot_tui.composer_key(chat, "n"), ("ignore", ""))

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

        chat = gbot_tui.Chat("Chef")
        chat.label = "what to cook"
        chat.messages = [{"role": "user", "text": "hi"}]
        screen = Screen()
        gbot_tui.draw(screen, chat)
        self.assertTrue(any("what to cook" in text for text in screen.rows.get(1, [])))
        self.assertNotIn(2, screen.rows)
        self.assertTrue(any("hi" in text for text in screen.rows.get(3, [])))
        self.assertEqual(gbot_tui.header_gap(2, "what to cook", 10), 1)
        self.assertEqual(gbot_tui.header_gap(2, "", 10), 0)
        self.assertEqual(gbot_tui.header_gap(0, "tag", 10), 0)

    def test_pick_restores_blocking_input(self):
        class Screen:
            def timeout(self, value):
                self.value = value
                raise StopIteration

        screen = Screen()
        with self.assertRaises(StopIteration):
            gbot_tui.pick(screen, [])
        self.assertEqual(screen.value, -1)

    def test_direct_name_loads_the_tagline(self):
        bots = [{"name": "Outfitly", "title": "what to wear", "avatarColor": "red"}]

        def fake_list():
            gbot_tui.ROSTER.clear()
            gbot_tui.ROSTER["Outfitly"] = bots[0]
            return bots

        original = gbot_tui.list_bots
        gbot_tui.list_bots = fake_list
        try:
            self.assertEqual(gbot_tui.resolve_for_open("out"), "Outfitly")
            self.assertEqual(gbot_tui.Chat("Outfitly").label, "what to wear")
        finally:
            gbot_tui.list_bots = original
            gbot_tui.ROSTER.clear()


class SendTests(unittest.TestCase):
    def test_enter_shows_the_line_and_the_poll_replaces_it(self):
        chat = gbot_tui.Chat("Researcher")
        chat.typed = "  hello  "
        self.assertEqual(chat.arm_send(), "hello")
        self.assertEqual(chat.phase, "working")
        self.assertEqual(chat.typed, "")
        self.assertEqual(gbot_tui.entry_text(chat.messages[-1]), "hello")
        self.assertTrue(chat.messages[-1].get("local"))
        page = gbot_tui.parse_thread(
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
        self.assertEqual([gbot_tui.entry_text(entry) for entry in chat.messages], ["hello", "yo"])
        self.assertEqual(chat.messages[0]["id"], "m1")
        self.assertFalse(chat.messages[0].get("local"))

    def test_a_reply_between_two_sends_stays_between_them(self):
        chat = gbot_tui.Chat("Researcher")
        chat.messages = [{"role": "assistant", "text": "earlier"}]
        chat.typed = "one"
        chat.arm_send()
        chat.typed = "two"
        chat.arm_send()
        page = gbot_tui.parse_thread(
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
            [gbot_tui.entry_text(entry) for entry in chat.messages],
            ["earlier", "one", "ack", "two"],
        )

    def test_gap_reset_keeps_an_unacked_local_line(self):
        chat = gbot_tui.Chat("Researcher")
        chat.typed = "hello"
        chat.arm_send()
        page = gbot_tui.parse_thread(
            {
                "transcript": {"entries": [{"id": "old", "role": "assistant", "text": "prior"}]},
                "cursor": "old",
                "gapReset": True,
            }
        )
        chat.apply(page, replace=False)
        self.assertEqual(
            [gbot_tui.entry_text(entry) for entry in chat.messages],
            ["prior", "hello"],
        )
        self.assertTrue(chat.messages[-1].get("local"))

    def test_footer_returns_to_idle_after_the_reply(self):
        chat = gbot_tui.Chat("Researcher")
        chat.cursor = "old"
        chat.phase = "working"
        chat.messages = [{"role": "user", "text": "hi", "local": True}]
        reply = gbot_tui.parse_thread(
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
        working = "".join(text for text, _style in gbot_tui.footer_spans(chat.phase, ""))
        self.assertIn("Generating", working)
        quiet = gbot_tui.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
        chat.ingest(quiet)
        chat.ingest(quiet)
        self.assertEqual(chat.phase, "done")
        done = gbot_tui.footer_spans(chat.phase, "")
        self.assertEqual(done, gbot_tui.footer_spans("idle", ""))
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
        chat = gbot_tui.Chat("Researcher")
        chat.phase = "working"
        self.assertLessEqual(chat.poll_delay(), 0.5)
        chat.saw_other = True
        self.assertEqual(chat.poll_delay(), gbot_tui.STREAM_DELAY)
        chat.quiet = 1
        # Two quiet polls still span about four seconds before "done".
        self.assertGreaterEqual(chat.poll_delay() * gbot_tui.QUIET_POLLS, 4)
        chat.phase = "blocked"
        self.assertEqual(chat.poll_delay(), gbot_tui.BLOCKED_DELAY)
        chat.phase = "idle"
        chat.cursor = "m1"
        chat.loaded = True
        delays = []
        quiet = gbot_tui.parse_thread({"transcript": {"entries": []}, "cursor": "m1"})
        for _ in range(8):
            delays.append(chat.poll_delay())
            chat.ingest(quiet)
        self.assertEqual(delays[0], gbot_tui.IDLE_DELAYS[0])
        self.assertEqual(delays[-1], gbot_tui.IDLE_DELAYS[-1])
        self.assertEqual(delays, sorted(delays))
        chat.nudge()
        self.assertEqual(chat.poll_delay(), gbot_tui.IDLE_DELAYS[0])
        fresh = gbot_tui.parse_thread(
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
        gbot_tui.write_roster(bots)
        cached = gbot_tui.read_roster()
        self.assertEqual([bot["name"] for bot in cached], ["Outfitly", "Chef"])
        self.assertNotIn("secret", cached[0])
        self.assertEqual(os.stat(gbot_tui.roster_path()).st_mode & 0o777, 0o600)
        calls = []
        original_list = gbot_tui.list_bots
        original_later = gbot_tui.refresh_roster_later
        gbot_tui.list_bots = lambda *args, **kwargs: calls.append("list") or bots
        gbot_tui.refresh_roster_later = lambda: calls.append("later")
        gbot_tui.ROSTER.clear()
        try:
            self.assertEqual(gbot_tui.resolve_for_open("out"), "Outfitly")
            self.assertEqual(calls, ["later"])
            self.assertEqual(gbot_tui.Chat("Outfitly").label, "what to wear")
            # A name the cache does not know still waits for the real list.
            bots.append({"id": "a3", "name": "Nomad", "title": "new"})
            self.assertEqual(gbot_tui.resolve_for_open("nom"), "Nomad")
            self.assertEqual(calls, ["later", "list"])
        finally:
            gbot_tui.list_bots = original_list
            gbot_tui.refresh_roster_later = original_later
            gbot_tui.ROSTER.clear()
            os.remove(gbot_tui.roster_path())

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
        gbot_tui.write_thread_cache("Researcher", messages)
        path = gbot_tui.thread_cache_path("Researcher")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        cached = gbot_tui.read_thread_cache("Researcher")
        self.assertEqual([entry.get("id") for entry in cached], ["m1", "card"])
        self.assertEqual(gbot_tui.read_thread_cache("Chef"), [])
        chat = gbot_tui.Chat("Researcher")
        chat.show_cached(cached)
        rows = gbot_tui.transcript_rows(chat.messages, 40)
        self.assertIn(("text", "earlier"), rows)
        # A cached card is not live, so y is text until the first read.
        self.assertFalse(chat.loaded)
        self.assertEqual(gbot_tui.composer_key(chat, "y"), ("type", "y"))
        self.assertEqual(chat.take_read(), ("Researcher",))
        page = gbot_tui.parse_thread(
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
                    raise gbot_tui.curses.error("no input")
                return self.keys.pop(0)

        screen = Keys("hello world")
        self.assertEqual("".join(gbot_tui.read_keys(screen)), "hello world")
        self.assertEqual(gbot_tui.read_keys(screen), [])
        chat = gbot_tui.Chat("Researcher")
        for key in "hi":
            self.assertIsNone(gbot_tui.chat_key(chat, key, None))
        self.assertEqual(chat.typed, "hi")
        self.assertEqual(gbot_tui.chat_key(chat, "\r", None), ("send", ("Researcher", "hi")))
        self.assertTrue(chat.messages[-1].get("local"))
        self.assertEqual(gbot_tui.chat_key(chat, "\x1b", None), "back")

    def test_alt_page_scrolls_half_of_a_page(self):
        self.assertEqual(gbot_tui.page_step(40, False), 36)
        self.assertEqual(gbot_tui.page_step(40, True), 18)
        self.assertEqual(gbot_tui.page_step(5, True), 1)
        self.assertEqual(gbot_tui.page_motion(gbot_tui.curses.KEY_PPAGE), (-1, False))
        self.assertEqual(gbot_tui.page_motion(gbot_tui.curses.KEY_NPAGE), (1, False))
        original = gbot_tui.curses.keyname

        def keyname(key):
            return {7: b"kPRV3", 8: b"kNXT3"}.get(key, b"KEY_OTHER")

        gbot_tui.curses.keyname = keyname
        saved_lines = getattr(gbot_tui.curses, "LINES", None)
        gbot_tui.curses.LINES = 40
        try:
            self.assertEqual(gbot_tui.page_motion(7), (-1, True))
            self.assertEqual(gbot_tui.page_motion(8), (1, True))
            self.assertIsNone(gbot_tui.page_motion(9))
            chat = gbot_tui.Chat("Researcher")
            chat.scroll = 100
            chat.follow = True
            self.assertIsNone(gbot_tui.chat_key(chat, gbot_tui.curses.KEY_PPAGE, None))
            self.assertEqual(chat.scroll, 64)
            self.assertFalse(chat.follow)
            self.assertIsNone(gbot_tui.chat_key(chat, 7, None))
            self.assertEqual(chat.scroll, 46)
            screen = FakeScreen(24, 80)
            chat.follow = False
            chat.scroll = 0
            self.assertIsNone(gbot_tui.chat_key(chat, 8, screen))
            self.assertEqual(chat.scroll, 18)
            self.assertTrue(chat.follow)
        finally:
            gbot_tui.curses.keyname = original
            if saved_lines is None:
                try:
                    del gbot_tui.curses.LINES
                except AttributeError:
                    pass
            else:
                gbot_tui.curses.LINES = saved_lines
        self.assertIs(getattr(gbot_tui.curses, "LINES", None), saved_lines)

    def test_a_long_error_gets_its_own_rows_on_screen(self):
        chat = gbot_tui.Chat("Researcher")
        chat.messages = [{"role": "assistant", "text": "hello"}]
        chat.error = "EnsureSandBox failed: 401 Unauthorized. Sign in to Grok Bot again and retry."
        screen = FakeScreen(24, 58)
        gbot_tui.draw(screen, chat)
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
                gbot_tui.THEME_ENV,
                "HERDR_ENV",
                "HERDR_PANE_ID",
                "HERDR_BIN_PATH",
                *gbot_tui.DESKTOP_MARKS,
            )
        }
        for key in self.saved:
            os.environ.pop(key, None)
        self.probed = gbot_tui.PROBED["bg"]
        self.theme = dict(gbot_tui.THEME)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        gbot_tui.PROBED["bg"] = self.probed
        gbot_tui.THEME.clear()
        gbot_tui.THEME.update(self.theme)

    def test_osc11_replies(self):
        parse = gbot_tui.parse_osc11
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
        choice = gbot_tui.theme_choice
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
        gbot_tui.PROBED["bg"] = "#FFFFFF"
        self.assertFalse(gbot_tui.dark_terminal())
        bubble = gbot_tui.user_bubble()
        self.assertTrue(gbot_tui.is_light(gbot_tui.xterm_hex(bubble)))
        self.assertNotEqual(gbot_tui.xterm_hex(bubble), "#FFFFFF")
        self.assertGreaterEqual(
            gbot_tui.contrast(gbot_tui.xterm_hex(gbot_tui.bubble_fg(bubble)), gbot_tui.xterm_hex(bubble)), 7
        )
        os.environ[gbot_tui.THEME_ENV] = "dark"
        self.assertTrue(gbot_tui.dark_terminal())
        self.assertEqual(gbot_tui.user_bubble(), 235)

    def test_bubble_on_a_probed_dark_background(self):
        gbot_tui.PROBED["bg"] = "#1E1E2E"
        bubble = gbot_tui.user_bubble()
        self.assertFalse(gbot_tui.is_light(gbot_tui.xterm_hex(bubble)))
        self.assertGreater(
            gbot_tui._hex_luminance(gbot_tui.xterm_hex(bubble)), gbot_tui._hex_luminance("#1E1E2E")
        )
        self.assertEqual(gbot_tui.bubble_fg(bubble), 255)
        self.assertEqual(gbot_tui.bubble_fg(235), 255)
        self.assertEqual(gbot_tui.bubble_fg(254), 234)

    def test_every_bot_color_reads_on_white_and_on_black(self):
        for name, hex_color in gbot_tui.AVATAR_COLORS.items():
            for dark, background, target in ((True, "#000000", 7), (False, "#FFFFFF", 4.5)):
                shown = gbot_tui.xterm_hex(gbot_tui.xterm_color(gbot_tui.readable_hex(hex_color, dark, background)))
                self.assertGreaterEqual(gbot_tui.contrast(shown, background), target, (name, dark))

    def test_named_styles_read_on_a_light_background(self):
        for name, index in gbot_tui.theme_palette(False).items():
            self.assertGreaterEqual(gbot_tui.contrast(gbot_tui.xterm_hex(index), "#FFFFFF"), 4.5, name)
        for name, index in gbot_tui.theme_palette(True).items():
            self.assertGreaterEqual(gbot_tui.contrast(gbot_tui.xterm_hex(index), "#000000"), 4.5, name)

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
            got = gbot_tui.probe_background(slave, timeout=0.3)
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
        fold = gbot_tui.fold_sample
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

        original = gbot_tui.probe_background
        gbot_tui.probe_background = fake
        try:
            started = time.monotonic()
            gbot_tui.detect_theme()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(calls, [0.2])
            self.assertTrue(gbot_tui.dark_terminal())
            self.assertTrue(gbot_tui.THEME["watch"])
            gbot_tui.THEME["due"] = 0
            applied = []
            self.assertTrue(gbot_tui.maybe_retheme(lambda: applied.append("theme")))
            self.assertEqual(applied, ["theme"])
            self.assertEqual(gbot_tui.PROBED["bg"], "#FFFFFF")
            self.assertFalse(gbot_tui.dark_terminal())
            self.assertFalse(gbot_tui.THEME["watch"])
        finally:
            gbot_tui.probe_background = original

    def test_placeholder_dark_is_rechecked_without_herdr_env(self):
        self.assertFalse(gbot_tui.herdr_session())
        calls = []

        def fake(fd=None, timeout=0.2):
            calls.append(timeout)
            return "#282C34" if len(calls) == 1 else "#FFFFFF"

        original = gbot_tui.probe_background
        gbot_tui.probe_background = fake
        try:
            gbot_tui.detect_theme()
            self.assertEqual(calls, [0.2])
            self.assertTrue(gbot_tui.THEME["watch"])
            self.assertEqual(gbot_tui.THEME["tries"], 1)
            self.assertEqual((gbot_tui.THEME_LIMIT, gbot_tui.THEME_GAP), (5, 0.5))
            gbot_tui.THEME["due"] = 0
            self.assertTrue(gbot_tui.maybe_retheme(lambda: None))
            self.assertEqual(gbot_tui.PROBED["bg"], "#FFFFFF")
            self.assertFalse(gbot_tui.dark_terminal())
            self.assertFalse(gbot_tui.THEME["watch"])
        finally:
            gbot_tui.probe_background = original

    def test_a_real_dark_reply_is_final(self):
        calls = []

        def fake(fd=None, timeout=0.2):
            calls.append(timeout)
            return "#1E1E2E"

        original = gbot_tui.probe_background
        gbot_tui.probe_background = fake
        try:
            gbot_tui.detect_theme()
            self.assertEqual(calls, [0.2])
            self.assertFalse(gbot_tui.THEME["watch"])
            self.assertFalse(gbot_tui.maybe_retheme(lambda: self.fail("rethemed")))
            self.assertEqual(len(calls), 1)
            self.assertTrue(gbot_tui.dark_terminal())
        finally:
            gbot_tui.probe_background = original

    def test_desktop_hc_vars_count_as_herdr(self):
        self.assertFalse(gbot_tui.herdr_session())
        self.assertEqual(gbot_tui.herdr_cli(), ("", ""))
        for key in gbot_tui.DESKTOP_MARKS:
            os.environ[key] = "desktop"
            self.assertTrue(gbot_tui.herdr_session(), key)
            # No pane id, so the CLI stays quiet. OSC 7501 still reports.
            self.assertEqual(gbot_tui.herdr_cli(), ("", ""))
            gbot_tui.PROBED["bg"] = "#FFFFFF"
            gbot_tui.THEME["watch"] = False
            gbot_tui.note_appearance()
            self.assertTrue(gbot_tui.THEME["watch"], key)
            os.environ.pop(key, None)
            gbot_tui.THEME["watch"] = False
        self.assertFalse(gbot_tui.herdr_session())
        gbot_tui.note_appearance()
        self.assertFalse(gbot_tui.THEME["watch"])
        os.environ["HERDR_BIN_PATH"] = "/usr/bin/herdr"
        os.environ["HERDR_PANE_ID"] = "w1:p1"
        self.assertTrue(gbot_tui.herdr_session())
        self.assertEqual(gbot_tui.herdr_cli(), ("/usr/bin/herdr", "w1:p1"))

    def test_theme_override_wins_over_herdr_default_dark(self):
        os.environ["HERDR_ENV"] = "1"
        os.environ[gbot_tui.THEME_ENV] = "light"
        gbot_tui.PROBED["bg"] = "#282C34"
        gbot_tui.THEME.update(watch=True, tries=1, due=0, follow=False)
        original = gbot_tui.probe_background
        gbot_tui.probe_background = lambda *a, **k: self.fail("probed")
        try:
            gbot_tui.detect_theme()
            self.assertFalse(gbot_tui.THEME["watch"])
            self.assertFalse(gbot_tui.maybe_retheme(lambda: self.fail("rethemed")))
            gbot_tui.note_appearance()
            self.assertFalse(gbot_tui.dark_terminal())
        finally:
            gbot_tui.probe_background = original

    def test_env_override_skips_the_probe(self):
        os.environ[gbot_tui.THEME_ENV] = "light"
        gbot_tui.PROBED["bg"] = None
        original = gbot_tui.probe_background
        gbot_tui.probe_background = lambda *a, **k: self.fail("probed")
        try:
            gbot_tui.detect_theme()
        finally:
            gbot_tui.probe_background = original
        self.assertFalse(gbot_tui.dark_terminal())


class NoticeTests(unittest.TestCase):
    def test_notifications_use_the_bots_name(self):
        self.assertEqual(gbot_tui.notice_name("Cloud"), "Cloud")
        self.assertEqual(gbot_tui.notice_name("Chef"), "Chef")
        self.assertEqual(gbot_tui.display_name("  Cloud\nbot "), "Cloud bot")
        self.assertEqual(gbot_tui.notice_name("Cursor Cloud Session"), "Cursor-Cloud-Session")
        self.assertEqual(gbot_tui.notice_name("Travel Agent"), "Travel-Agent")
        self.assertEqual(gbot_tui.notice_name("Cursor\u00a0Cloud\u00a0Session"), "Cursor-Cloud-Session")
        self.assertEqual(gbot_tui.notice_name("Travel\u2002Agent"), "Travel-Agent")
        self.assertEqual(gbot_tui.notice_name("Cursor_Cloud_Session"), "Cursor_Cloud_Session")
        self.assertEqual(gbot_tui.notice_name("Cursor.Cloud"), "Cursor.Cloud")
        self.assertNotIn(" ", gbot_tui.notice_name("Cursor Cloud Session"))
        self.assertNotIn(":", gbot_tui.notice_name("a:b"))
        self.assertEqual(gbot_tui.notice_name("a:b"), "a-b")
        clipped = gbot_tui.notice_name("C" * 40)
        self.assertLessEqual(len(clipped.encode()), 32)
        self.assertTrue(clipped.startswith("C"))
        wide = gbot_tui.notice_name("Cursor Cloud Session Extra Words " * 3)
        self.assertLessEqual(len(wide.encode()), 32)
        self.assertNotIn(" ", wide)
        self.assertNotIn("\u00a0", wide)
        previous = gbot_tui.APP
        try:
            gbot_tui.bind_app("Cloud", "b3-cloud")
            self.assertEqual(gbot_tui.APP, "Cloud")
            report = gbot_tui.osc_report("blocked", "Cloud", "permission")
            self.assertIn("app=Cloud", report)
            self.assertNotIn("gbot-b3-cloud", report)
            self.assertLessEqual(len(gbot_tui.APP.encode()), 32)
        finally:
            gbot_tui.APP = previous
        command = gbot_tui.report_command("herdr", "p1", "blocked", "Cloud", "s", 1, "Cloud")
        self.assertEqual(command[command.index("--agent") + 1], "Cloud")
        release = gbot_tui.release_command("herdr", "p1", "Cloud", 2)
        self.assertEqual(release[release.index("--agent") + 1], "Cloud")
        spaced = gbot_tui.report_command(
            "herdr", "p1", "blocked", "Cursor Cloud Session", "s", 1, "Cursor Cloud Session"
        )
        self.assertEqual(spaced[spaced.index("--agent") + 1], "Cursor-Cloud-Session")
        self.assertEqual(spaced[-2:], ["gbot-tui", "Cursor Cloud Session"])
        previous = gbot_tui.APP
        try:
            gbot_tui.bind_app("Cursor Cloud Session", "b3")
            self.assertEqual(gbot_tui.APP, "Cursor-Cloud-Session")
            report = gbot_tui.osc_report("blocked", "Cursor Cloud Session", "permission")
            app = report.split("app=", 1)[1].split(":", 1)[0]
            self.assertEqual(app, "Cursor-Cloud-Session")
        finally:
            gbot_tui.APP = previous

    def test_a_late_icon_does_not_replace_another_bots_name(self):
        previous_app = gbot_tui.APP
        previous_install = gbot_tui.install_avatars_later

        def immediate(bots, done=None):
            if done:
                done({"gbot-b4-outfitly": {}})
            return None

        gbot_tui.install_avatars_later = immediate
        gbot_tui.APP = "Chef"
        try:
            gbot_tui.show_avatar_later({"id": "b4-outfitly", "name": "Outfitly"})
            self.assertEqual(gbot_tui.APP, "Chef")
            gbot_tui.APP = "gbot-tui"
            gbot_tui.show_avatar_later({"id": "b4-outfitly", "name": "Outfitly"})
            self.assertEqual(gbot_tui.APP, "Outfitly")
        finally:
            gbot_tui.APP = previous_app
            gbot_tui.install_avatars_later = previous_install


class QuitTests(unittest.TestCase):
    def tearDown(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        gbot_tui.EXITED["done"] = False
        gbot_tui.TITLE.update(pushed=False, popped=False)

    def test_ctrl_c_quits_like_ctrl_d_and_sigint_is_restored(self):
        chat = gbot_tui.Chat("Researcher")
        self.assertEqual(gbot_tui.chat_key(chat, "\x03", None), "quit")
        self.assertEqual(gbot_tui.chat_key(chat, "\x04", None), "quit")
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        gbot_tui.enable_ctrl_c()
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertIs(signal.getsignal(signal.SIGTERM), gbot_tui._stop)
        master, slave = os.openpty()
        try:
            attr = termios.tcgetattr(slave)
            attr[3] |= termios.ISIG
            termios.tcsetattr(slave, termios.TCSANOW, attr)
            self.assertTrue(gbot_tui.accept_ctrl_c(slave))
            self.assertFalse(termios.tcgetattr(slave)[3] & termios.ISIG)
        finally:
            os.close(slave)
            os.close(master)

    def test_shutdown_restores_the_tab_and_pops_the_title_once(self):
        renames = []
        pops = []
        clears = []
        title = gbot_tui.TabTitle(
            find=lambda: ({"labels": {"herdr.tab": "t2"}}, True),
            spaces=lambda: TabTests.SPACES,
            rename=lambda _info, name: renames.append(name),
        )
        saved = gbot_tui.TAB, gbot_tui.write_tty, gbot_tui.emit, gbot_tui.APP
        gbot_tui.TAB = title
        gbot_tui.write_tty = lambda data: pops.append(data)
        gbot_tui.emit = lambda state, name, kind=None: clears.append(state)
        gbot_tui.EXITED["done"] = False
        gbot_tui.TITLE.update(pushed=True, popped=False)
        try:
            title.show("Chef")
            self.assertTrue(title.flush(2))
            gbot_tui.shutdown()
            gbot_tui.shutdown()
            self.assertEqual(gbot_tui.APP, "gbot-tui")
        finally:
            gbot_tui.TAB, gbot_tui.write_tty, gbot_tui.emit, gbot_tui.APP = saved
            gbot_tui.EXITED["done"] = False
            gbot_tui.TITLE.update(pushed=False, popped=False)
        self.assertEqual(renames, ["Chef", "Work"])
        self.assertEqual(clears, ["clear"])
        self.assertEqual(pops, [gbot_tui.POP_TITLE])


class TabTests(unittest.TestCase):
    SPACES = [
        {"id": "s1", "tabs": [{"id": "t1", "name": "1"}, {"id": "t2", "name": "Work"}]},
        {"id": "s2", "tabs": [{"id": "t3", "name": "Outfitly"}]},
    ]

    def tab(self, tab_id, alone=True):
        renames = []
        info = {"labels": {"herdr.tab": tab_id}}
        title = gbot_tui.TabTitle(
            find=lambda: (info, alone),
            spaces=lambda: self.SPACES,
            rename=lambda _info, name: renames.append(name),
        )
        return title, renames

    def test_tab_name_from_ls(self):
        self.assertEqual(gbot_tui.tab_name("t1", self.SPACES), "")
        self.assertEqual(gbot_tui.tab_name("t2", self.SPACES), "Work")
        self.assertEqual(gbot_tui.tab_name("t3", self.SPACES), "Outfitly")
        self.assertEqual(gbot_tui.tab_name("gone", self.SPACES), "")

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
        saved = gbot_tui.TAB, gbot_tui.emit, gbot_tui.window_title, gbot_tui.silence_herdr, gbot_tui.APP
        title, renames = self.tab("t2")
        gbot_tui.TAB = title
        gbot_tui.APP = "Outfitly"
        gbot_tui.emit = lambda state, name, kind=None: calls.append(("emit", state, gbot_tui.APP, name))
        gbot_tui.window_title = lambda text: calls.append(("title", text))
        gbot_tui.silence_herdr = lambda label: None
        try:
            chat = gbot_tui.Chat("Outfitly")
            title.show("Outfitly")
            gbot_tui.leave_chat(chat, final=False)
            self.assertTrue(title.flush(2))
        finally:
            gbot_tui.TAB, gbot_tui.emit, gbot_tui.window_title, gbot_tui.silence_herdr, gbot_tui.APP = saved
        self.assertEqual(renames, ["Outfitly", "Work"])
        self.assertEqual(calls[0], ("emit", "clear", "Outfitly", "Outfitly"))
        self.assertIn(("emit", "idle", "gbot-tui", "gbot"), calls)
        self.assertIn(("title", "gbot"), calls)


class HerdrTests(unittest.TestCase):
    def test_osc_carries_state_and_title(self):
        report = gbot_tui.osc_report("working", "Researcher")
        self.assertIn("state=working", report)
        self.assertIn("app=gbot-tui", report)
        encoded = base64.b64encode(b"Researcher").decode()
        self.assertIn(f"title={encoded}", report)
        self.assertIn("\033]0;Researcher\033\\", report)

    def test_resume_rejects_quotes(self):
        self.assertIsNone(gbot_tui.resume_arg("it's"))
        self.assertEqual(gbot_tui.resume_arg("Researcher"), "Researcher")

    def test_rename_only_when_the_tab_has_one_terminal(self):
        mine = {"state": "live", "labels": {"herdr.tab": "tb1"}}
        other = {"state": "live", "labels": {"herdr.tab": "tb1"}}
        self.assertTrue(gbot_tui.alone_in_tab(mine, [mine]))
        self.assertFalse(gbot_tui.alone_in_tab(mine, [mine, other]))

    def test_report_command(self):
        command = gbot_tui.report_command(
            "herdr", "p1", "done", "Researcher", "a1", 5, "Researcher"
        )
        self.assertEqual(command[command.index("--state") + 1], "idle")
        self.assertEqual(command[-2:], ["gbot-tui", "Researcher"])


def _reset_overview():
    gbot_tui.ACTIVITY.clear()
    gbot_tui.SEEN.clear()
    gbot_tui.ACTIVITY_LOADED["done"] = False
    gbot_tui.SEEN_LOADED["done"] = False
    paths = [gbot_tui.activity_path(), gbot_tui.seen_path(), gbot_tui.thread_cache_path("Chef")]
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
        chat = gbot_tui.Chat("Researcher")
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
        text = gbot_tui.entry_text(entry)
        self.assertIn("How should I get into your Vivino?", text)
        self.assertIn("1  Sign in and request the export", text)
        self.assertIn("2  Just sign in and browse", text)
        rows = [line for kind, line in gbot_tui.transcript_rows([entry], 80) if kind == "text"]
        self.assertIn("1  Sign in and request the export", rows)
        older = _widget("Older?", [{"label": "Old", "value": "old"}], id="old")
        both = [line for _kind, line in gbot_tui.transcript_rows([older, entry], 80)]
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
        text = gbot_tui.entry_text(entry)
        self.assertIn("●  Just sign in and browse", text)
        self.assertNotIn("\n1  ", text)
        self.assertNotIn("\n2  ", text)

    def test_a_skipped_widget_says_so(self):
        entry = _widget("Install it?", [{"label": "pstack", "value": "install pstack"}], widgetSkipped=True)
        self.assertIn("Install it?  · skipped", gbot_tui.entry_text(entry))
        self.assertNotIn("\n1  ", gbot_tui.entry_text(entry))

    def test_a_number_sends_that_option(self):
        entry = _widget(
            "Send these fixes?",
            [
                {"label": "Badge bug and polish", "value": "Send the badge bug"},
                {"label": "Leave it", "value": "Leave it for now"},
            ],
        )
        chat = self._chat(entry)
        job = gbot_tui.chat_key(chat, "1", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "Send the badge bug")))
        self.assertIn("●  Badge bug and polish", gbot_tui.entry_text(chat.messages[0]))
        self.assertEqual(chat.phase, "working")
        chat.fail_choice()
        self.assertIn("1  Badge bug and polish", gbot_tui.entry_text(chat.messages[0]))
        self.assertEqual(chat.phase, "idle")
        self.assertIsNone(gbot_tui.chat_key(chat, "0", None))
        self.assertEqual(chat.typed, "")

    def test_a_number_is_text_when_nothing_is_open(self):
        chat = gbot_tui.Chat("Researcher")
        chat.loaded = True
        chat.session_id = "agent"
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "1")

    def test_a_number_waits_until_the_thread_is_live(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "Yes, send them"}])
        chat = self._chat(entry)
        chat.loaded = False
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "")
        self.assertIsNone(gbot_tui.latest_decision(chat.messages))

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
        self.assertIsNone(gbot_tui.chat_key(chat, "2", None))
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        shown = gbot_tui.entry_text(entry, chat.widget_picks["w1"])
        self.assertIn("● 1  Emil", shown)
        self.assertIn("● 2  Mario", shown)
        job = gbot_tui.chat_key(chat, "\r", None)
        self.assertEqual(job[0], "choice")
        self.assertEqual(job[1][-1], "Send Emil\nSend Mario")

    def test_widget_error_reads_a_refusal(self):
        self.assertEqual(gbot_tui.widget_error({"ok": True, "text": ""}), "")
        self.assertEqual(gbot_tui.widget_error({"ok": True, "text": '{"accepted": true}'}), "")
        self.assertEqual(
            gbot_tui.widget_error({"ok": True, "text": '{"accepted": false, "refusal": "gone"}'}),
            "gone",
        )
        self.assertEqual(gbot_tui.widget_error({"ok": False, "text": "nope"}), "nope")

    def test_the_footer_names_the_keys(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        hint = gbot_tui.choice_hint(entry)
        self.assertEqual(hint, "1-2 answer")
        footer = gbot_tui.footer_spans("idle", "", choice=hint)[0][0]
        self.assertIn("1-2 answer", footer)
        self.assertIn("space then digit types", footer)
        self.assertEqual(footer.count("enter"), 1)

    def test_a_widget_behind_your_reply_does_not_take_digits(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        chat = self._chat(entry)
        chat.messages.append({"id": "u", "role": "user", "text": "later"})
        self.assertIsNone(gbot_tui.latest_widget(chat.messages))
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "1")
        self.assertEqual(chat.phase, "idle")
        shown = [line for kind, line in gbot_tui.transcript_rows(chat.messages, 80) if kind == "text"]
        self.assertIn("   Yes", shown)
        self.assertIn("   No", shown)
        self.assertNotIn("1  Yes", shown)
        self.assertNotIn("2  No", shown)

    def test_a_space_then_a_digit_types_it(self):
        entry = _widget("Send these fixes?", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])
        chat = self._chat(entry)
        self.assertIsNone(gbot_tui.chat_key(chat, " ", None))
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, " 1")
        self.assertEqual(chat.phase, "idle")
        self.assertFalse(gbot_tui.widget_answered(entry))

    def test_options_past_nine_take_two_digits_and_letters_type(self):
        options = [{"label": f"L{i}", "value": f"v{i}"} for i in range(1, 12)]
        entry = _widget("Which?", options)
        text = gbot_tui.entry_text(entry)
        self.assertIn("09  L9", text)
        self.assertIn("10  L10", text)
        self.assertIn("11  L11", text)
        self.assertNotIn("a  L10", text)
        self.assertEqual(gbot_tui.choice_hint(entry), "01-11 two digits answer")
        chat = self._chat(entry)
        self.assertIsNone(gbot_tui.chat_key(chat, "a", None))
        self.assertEqual(chat.typed, "a")
        self.assertEqual(chat.phase, "idle")
        self.assertFalse(gbot_tui.widget_answered(entry))
        chat.typed = ""
        self.assertIsNone(gbot_tui.chat_key(chat, "0", None))
        self.assertEqual(chat.typed, "")
        self.assertIsNone(gbot_tui.chat_key(chat, "\x7f", None))
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "")
        job = gbot_tui.chat_key(chat, "0", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "v10")))
        self.assertEqual(chat.messages[0].get("respondedValue"), "v10")
        chat.fail_choice()
        self.assertIn("10  L10", gbot_tui.entry_text(chat.messages[0]))
        # A letter cancels a half-typed number and is typed, not answered.
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertIsNone(gbot_tui.chat_key(chat, "b", None))
        self.assertEqual(chat.typed, "b")
        self.assertFalse(gbot_tui.widget_answered(chat.messages[0]))
        chat.typed = ""
        # After the window, a lone digit selects that option.
        times = {"now": 20.0}
        original = gbot_tui.clock
        gbot_tui.clock = lambda: times["now"]
        try:
            self.assertIsNone(gbot_tui.chat_key(chat, "2", None))
            self.assertIsNone(gbot_tui.flush_choice_digit(chat))
            times["now"] = 20.0 + gbot_tui.DIGIT_WINDOW + 0.01
            flushed = gbot_tui.flush_choice_digit(chat)
            self.assertEqual(flushed, ("choice", ("Researcher", "agent", "w1", "v2")))
        finally:
            gbot_tui.clock = original

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
        text = gbot_tui.entry_text(entry)
        self.assertIn("City?", text)
        self.assertIn("1  Berlin", text)
        self.assertIn("Days?", text)
        self.assertNotIn("1  One", text)
        chat = self._chat(entry)
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.phase, "idle")
        shown = [
            line
            for kind, line in gbot_tui.transcript_rows(chat.messages, 80, chat.widget_picks, chat.widget_steps)
            if kind == "text"
        ]
        self.assertIn("●  Berlin", shown)
        self.assertIn("1  One", "\n".join(shown))
        self.assertIsNone(gbot_tui.chat_key(chat, "2", None))
        job = gbot_tui.chat_key(chat, "\r", None)
        self.assertEqual(job[0], "choice")
        self.assertEqual(job[1][-1], "Berlin\n\n2")
        self.assertEqual(chat.messages[0].get("respondedValue"), "Berlin\n\n2")
        chat.fail_choice()
        self.assertFalse(gbot_tui.widget_answered(chat.messages[0]))
        self.assertEqual(chat.phase, "idle")
        restored = gbot_tui.entry_text(
            chat.messages[0],
            step_at=chat.step_at(chat.messages[0]),
            step_answers=chat.widget_steps["w1"]["answers"],
        )
        self.assertIn("●  Berlin", restored)
        self.assertIn("1  One", restored)

    def test_a_step_without_a_way_to_answer_says_so(self):
        entry = {
            "kind": "send-message",
            "id": "w1",
            "message": {"type": "widget", "widget": {"steps": [{"prompt": "Use the app"}]}},
        }
        self.assertIn("answer in the app", gbot_tui.entry_text(entry))
        self.assertFalse(gbot_tui.widget_open(entry))
        chat = self._chat(entry)
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        self.assertEqual(chat.typed, "1")

    def test_custom_text_answers_the_open_widget(self):
        entry = _widget("Name?", [{"label": "Ann", "value": "Ann"}])
        entry["message"]["widget"]["allowCustom"] = True
        chat = self._chat(entry)
        chat.typed = "Bea"
        job = gbot_tui.chat_key(chat, "\r", None)
        self.assertEqual(job, ("choice", ("Researcher", "agent", "w1", "Bea")))
        self.assertEqual(chat.messages[0].get("respondedValue"), "Bea")
        self.assertEqual(chat.typed, "")
        plain = _widget("Name?", [{"label": "Ann", "value": "Ann"}], id="w2")
        other = self._chat(plain)
        other.typed = "Bea"
        sent = gbot_tui.chat_key(other, "\r", None)
        self.assertEqual(sent, ("send", ("Researcher", "Bea")))
        self.assertFalse(gbot_tui.widget_answered(plain))

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
        gbot_tui.draw(screen, chat)
        self.assertEqual(screen.full, 1)
        self.assertIsNone(gbot_tui.chat_key(chat, "1", None))
        gbot_tui.draw(screen, chat)
        self.assertEqual(screen.full, 1)
        job = gbot_tui.chat_key(chat, "\r", None)
        self.assertEqual(job[0], "choice")
        gbot_tui.draw(screen, chat)
        self.assertGreater(screen.full, 1)
        self.assertTrue(any("Emil" in text for row in screen.rows.values() for text in row))

    def test_the_footer_says_enter_once(self):
        entry = _widget(
            "Send these?",
            [{"label": "Emil", "value": "Emil"}, {"label": "Mario", "value": "Mario"}],
        )
        entry["message"]["widget"]["multiSelect"] = True
        footer = gbot_tui.footer_spans("idle", "", choice=gbot_tui.choice_hint(entry))[0][0]
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
        step_footer = gbot_tui.footer_spans("idle", "", choice=gbot_tui.choice_hint(steps))[0][0]
        self.assertIn("enter sends", step_footer)
        self.assertEqual(step_footer.count("enter"), 1)

    def test_widget_error_rejects_a_non_json_body(self):
        self.assertEqual(gbot_tui.widget_error({"ok": True, "text": "OK", "confirmed": False}), "choice was not confirmed")
        self.assertEqual(gbot_tui.widget_error({"ok": True, "text": "OK"}), "choice was not confirmed")

    def test_a_session_id_is_not_the_agent_id(self):
        self.assertEqual(gbot_tui.gateway_agent_id({"id": "agent-real"}), "agent-real")
        self.assertEqual(
            gbot_tui.gateway_agent_id({"id": "agent-real", "sessionId": "sess-9"}),
            "agent-real",
        )
        self.assertEqual(
            gbot_tui.gateway_agent_id({"id": "sess-9", "agentId": "agent-real", "sessionId": "sess-9"}),
            "agent-real",
        )
        self.assertEqual(gbot_tui.gateway_agent_id({"id": "sess-9", "sessionId": "sess-9"}), "")
        self.assertEqual(gbot_tui.gateway_agent_id({"agentId": "sess-9", "sessionId": "sess-9"}), "")
        page = gbot_tui.parse_thread(
            {
                "target": {"id": "sess-9", "sessionId": "sess-9", "name": "Researcher"},
                "transcript": {"entries": [_widget("Go?", [{"label": "Yes", "value": "yes"}])]},
                "cursor": "w1",
            }
        )
        chat = gbot_tui.Chat("Researcher")
        chat.ingest(page, first=True)
        self.assertEqual(chat.agent_id, "")
        self.assertEqual(chat.viewer_session, "sess-9")
        job = gbot_tui.chat_key(chat, "1", None)
        self.assertEqual(job[1][1], "")
        distinct = gbot_tui.parse_thread(
            {
                "target": {"id": "agent-real", "sessionId": "sess-9", "name": "Researcher"},
                "transcript": {"entries": [_widget("Go?", [{"label": "Yes", "value": "yes"}])]},
                "cursor": "w1",
            }
        )
        live = gbot_tui.Chat("Researcher")
        live.ingest(distinct, first=True)
        picked = gbot_tui.chat_key(live, "1", None)
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
        gbot_tui.respond_widget("Researcher", "agent-real", "e1", "Yes please")
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
        gbot_tui.respond_widget("Researcher", "agent-real", "e1", "Yes please")
        self.assertTrue(self._widget_hits())
        self.assertIn("send ", self._log())
        self.assertIn("Yes please", self._log())

        open(self.log, "w", encoding="utf-8").close()
        self.server.mode = "refuse"
        gbot_tui.respond_widget("Researcher", "agent-real", "e1", "Yes please")
        self.assertIn("Yes please", self._log())

        open(self.log, "w", encoding="utf-8").close()
        self.server.hits.clear()
        gbot_tui.respond_widget("Researcher", "", "e1", "typed")
        self.assertEqual(self._widget_hits(), [])
        self.assertIn("typed", self._log())
        self.assertNotIn("thread", self._log())

    def test_without_the_hook_the_value_is_only_a_message(self):
        os.environ.pop("GBOT_TUI_WIDGET_HOOK", None)
        gbot_tui.respond_widget("Researcher", "agent-real", "e1", "Just send")
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
        snap = gbot_tui.judge_activity(entries, "c1", "m1")
        self.assertEqual(snap["state"], "blocked")
        self.assertEqual(snap["preview"], "npm publish")
        self.assertEqual(gbot_tui.status_label(snap["state"], snap["unread"]), "needs you")

    def test_a_user_line_with_no_reply_is_running(self):
        entries = [
            {"id": "m1", "role": "assistant", "text": "hi"},
            {"id": "m2", "role": "user", "text": "go"},
        ]
        snap = gbot_tui.judge_activity(entries, "m2", "m1")
        self.assertEqual(snap["state"], "working")
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(snap["preview"], "go")

    def test_a_reply_you_have_not_opened_is_new(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "done"},
            {"id": "m3", "role": "assistant", "text": "and more"},
        ]
        snap = gbot_tui.judge_activity(entries, "m3", "m1")
        self.assertEqual(snap["state"], "unread")
        self.assertEqual(snap["unread"], 2)
        self.assertEqual(snap["preview"], "and more")
        self.assertEqual(gbot_tui.status_label("unread", 2), "2 new")

    def test_a_seen_reply_is_idle_and_a_first_look_is_not_all_new(self):
        entries = [{"id": "m2", "role": "assistant", "text": "done"}]
        self.assertEqual(gbot_tui.judge_activity(entries, "m2", "m2")["state"], "idle")
        first = gbot_tui.judge_activity(entries, "m2", "")
        self.assertEqual(first["state"], "idle")
        self.assertEqual(first["unread"], 0)

    def test_a_streaming_reply_you_have_open_stays_running(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "partial", "streaming": True},
        ]
        self.assertEqual(gbot_tui.judge_activity(entries, "m2", "m2")["state"], "working")
        # The same partial, not yet on screen, is a new message.
        self.assertEqual(gbot_tui.judge_activity(entries, "m2", "m1")["state"], "unread")

    def test_a_seen_id_older_than_this_tail_counts_the_tail(self):
        entries = [{"id": "m9", "role": "assistant", "text": "later"}]
        snap = gbot_tui.judge_activity(entries, "m9", "m1")
        self.assertEqual(snap["state"], "unread")

    def test_attention_sorts_above_idle_and_the_summary_counts_it(self):
        bots = [
            {"name": "Idle", "activity": {"state": "idle", "unread": 0}},
            {"name": "Runner", "activity": {"state": "working", "unread": 0}},
            {"name": "Fresh", "activity": {"state": "unread", "unread": 1}},
            {"name": "Ask", "activity": {"state": "blocked", "unread": 0}},
            {"name": "Wait"},
        ]
        self.assertEqual(
            [bot["name"] for bot in gbot_tui.order_bots(bots)],
            ["Ask", "Fresh", "Runner", "Wait", "Idle"],
        )
        self.assertEqual(
            gbot_tui.overview_summary(bots[:4]),
            "1 needs you · 1 new · 1 working",
        )
        self.assertEqual(gbot_tui.overview_summary([], checked=0, total=4), "checking threads")
        self.assertEqual(
            gbot_tui.overview_summary(bots[:2], checked=1, total=4),
            "1 working · checking 1 of 4",
        )
        self.assertEqual(gbot_tui.overview_summary(bots[:1]), "1 idle")

    def test_the_row_shows_the_status_and_the_preview(self):
        bot = {
            "name": "Outfitly",
            "title": "wardrobe",
            "avatarColor": "violet",
            "activity": {"state": "blocked", "unread": 1, "preview": "npm publish", "cursor": "c"},
        }
        spans = gbot_tui.picker_spans(bot, True, 60)
        self.assertEqual(spans[0], ("→ ", "bold"))
        self.assertEqual(spans[1], ("● ", "bot:violet"))
        self.assertEqual(spans[2], ("Outfitly", "bold"))
        self.assertEqual(spans[4], ("needs you ", "yellow"))
        self.assertEqual(spans[5], ("npm publish", "dim"))
        narrow = "".join(text for text, _style in gbot_tui.picker_spans(bot, False, 16))
        self.assertIn("Outfitly", narrow)

    def test_search_matches_the_latest_line(self):
        bots = [
            {"name": "Chef", "title": "", "activity": {"preview": "pasta is ready", "state": "unread"}},
            {"name": "Night", "title": ""},
        ]
        self.assertEqual(gbot_tui.fuzzy_bots("pasta", bots)[0]["name"], "Chef")

    def test_seen_is_private_and_the_cached_thread_counts_as_read(self):
        gbot_tui.remember_seen("Outfitly", "a1", "m3")
        self.assertEqual(os.stat(gbot_tui.seen_path()).st_mode & 0o777, 0o600)
        gbot_tui.SEEN.clear()
        gbot_tui.SEEN_LOADED["done"] = False
        self.assertEqual(gbot_tui.lookup_seen({"id": "a1", "name": "Outfitly"}), "m3")
        gbot_tui.write_thread_cache("Chef", [{"id": "m4", "role": "assistant", "text": "old"}])
        self.assertEqual(gbot_tui.seen_mark({"name": "Chef"}), "m4")
        gbot_tui.store_activity(
            {"name": "Nomad", "id": "a9"},
            {"state": "idle", "unread": 0, "cursor": "m1", "preview": ""},
        )
        # A scan stores this cursor in the activity file. It is not a seen mark:
        # the next pass would otherwise treat it as already read.
        self.assertEqual(gbot_tui.seen_mark({"name": "Nomad", "id": "a9"}), "")
        gbot_tui.remember_seen("Nomad", "a9", "m9")
        self.assertEqual(gbot_tui.seen_mark({"name": "Nomad", "id": "a9"}), "m9")
        self.assertEqual(os.stat(gbot_tui.activity_path()).st_mode & 0o777, 0o600)

    def test_a_later_pass_marks_a_reply_you_have_not_opened(self):
        bots = [{"id": "a1", "name": "Chef", "title": "what to cook"}]
        first = {"entries": [{"id": "m1", "role": "assistant", "text": "hello"}], "cursor": "m1"}
        views, error = gbot_tui.build_overview(bots, lambda bot: first)
        self.assertIsNone(error)
        self.assertEqual(views[0]["activity"]["state"], "idle")
        second = {
            "entries": [
                {"id": "m1", "role": "assistant", "text": "hello"},
                {"id": "m2", "role": "assistant", "text": "lunch is ready"},
            ],
            "cursor": "m2",
        }
        views, error = gbot_tui.build_overview(bots, lambda bot: second)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(views[0]["activity"]["preview"], "lunch is ready")
        text = gbot_tui.format_overview(views)
        self.assertIn("1 new", text)
        self.assertIn("Chef", text)
        self.assertIn("lunch is ready", text)
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m1")

    def test_a_never_opened_bot_stays_new_across_scans(self):
        bots = [{"id": "a1", "name": "Researcher"}]

        def page(*rows):
            entries = [{"id": mid, "role": "assistant", "text": text} for mid, text in rows]
            return {"entries": entries, "cursor": rows[-1][0]}

        first = page(("m1", "hello"))
        views, error = gbot_tui.build_overview(bots, lambda bot: first)
        self.assertIsNone(error)
        self.assertEqual(views[0]["activity"]["state"], "idle")
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m1")

        second = page(("m1", "hello"), ("m2", "done"))
        views, error = gbot_tui.build_overview(bots, lambda bot: second)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(views[0]["activity"]["preview"], "done")
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m1")

        third = page(("m1", "hello"), ("m2", "done"), ("m3", "still here"))
        views, error = gbot_tui.build_overview(bots, lambda bot: third)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(views[0]["activity"]["unread"], 2)
        self.assertEqual(views[0]["activity"]["preview"], "still here")
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m1")

        # A fresh process reads the pinned cursor. Another scan does not move it.
        gbot_tui.SEEN.clear()
        gbot_tui.SEEN_LOADED["done"] = False
        gbot_tui.ACTIVITY.clear()
        gbot_tui.ACTIVITY_LOADED["done"] = False
        views, error = gbot_tui.build_overview(bots, lambda bot: third)
        self.assertEqual(views[0]["activity"]["state"], "unread")
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m1")

        gbot_tui.remember_seen("Researcher", "a1", "m3")
        gbot_tui.remember_seen("Researcher", "a1", "m1", only_if_absent=True)
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m3")
        views, error = gbot_tui.build_overview(bots, lambda bot: third)
        self.assertEqual(views[0]["activity"]["state"], "idle")
        self.assertEqual(gbot_tui.lookup_seen(bots[0]), "m3")

    def test_list_previews_share_a_column(self):
        def bot(name, preview):
            return {
                "name": name,
                "title": "",
                "activity": {"state": "idle", "unread": 0, "preview": preview, "cursor": "c"},
            }

        bots = [bot("A", "one"), bot("Researcher", "two replies waiting")]
        for width in (80, 58):
            columns = gbot_tui.list_columns(bots, width)
            self.assertIsNotNone(columns, width)
            starts = []
            for item in bots:
                spans = gbot_tui.picker_spans(item, False, width, columns)
                self.assertEqual(spans[-1][1], "dim")
                self.assertTrue(spans[-1][0].startswith(item["activity"]["preview"][:4]))
                starts.append(sum(len(text) for text, _style in spans[:-1]))
            self.assertEqual(starts[0], starts[1], width)
        self.assertIsNone(gbot_tui.list_columns(bots, 20))
        shown = "".join(text for text, _style in gbot_tui.picker_spans(bots[1], False, 18))
        self.assertIn("Researcher", shown)
        clipped = "".join(text for text, _style in gbot_tui.picker_spans(bots[1], False, 16))
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
        names = [bot["name"] for bot in gbot_tui.fuzzy_bots("outf", bots)]
        self.assertEqual(names[0], "Outfitly")
        self.assertIn("Chef", names)
        self.assertNotIn("Night", names)
        self.assertIsNone(gbot_tui.preview_score("outf", "our task follows the plan today"))
        self.assertGreater(
            gbot_tui.preview_score("outf", "outfit"),
            gbot_tui.preview_score("outf", "theoutfit"),
        )
        self.assertGreater(
            gbot_tui.fuzzy_score("outf", "Outfitly"),
            gbot_tui.preview_score("outf", "the outfit is ready"),
        )

    def test_opening_the_thread_clears_new(self):
        page = gbot_tui.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": [{"id": "m2", "role": "assistant", "text": "served"}]},
                "cursor": "m2",
            }
        )
        chat = gbot_tui.Chat("Chef")
        chat.ingest(page, first=True)
        self.assertEqual(gbot_tui.lookup_seen({"name": "Chef", "id": "a2"}), "m2")
        snap = gbot_tui.lookup_activity({"name": "Chef", "id": "a2"})
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(snap["state"], "idle")
        chat.typed = "more"
        chat.arm_send()
        self.assertEqual(gbot_tui.lookup_activity({"name": "Chef"})["state"], "working")

    def test_opening_an_unanswered_thread_shows_working(self):
        page = gbot_tui.parse_thread(
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
        chat = gbot_tui.Chat("Chef")
        chat.ingest(page, first=True)
        self.assertEqual(chat.phase, "working")
        quiet = gbot_tui.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
        chat.ingest(quiet)
        chat.ingest(quiet)
        self.assertEqual(chat.phase, "working")
        self.assertEqual(gbot_tui.lookup_activity({"name": "Chef", "id": "a2"})["state"], "working")
        screen = FakeScreen(24, 80)
        gbot_tui.draw(screen, chat)
        header = "".join(screen.rows.get(0, []))
        self.assertIn("working", header)
        spans = gbot_tui.picker_spans(
            {"name": "Chef", "activity": {"state": "working", "unread": 0, "preview": "go"}},
            False,
            60,
        )
        self.assertIn(("working   ", "cyan"), spans)
        self.assertEqual(gbot_tui.STATUS_STYLE["blocked"], "yellow")
        self.assertNotEqual(gbot_tui.STATUS_STYLE["working"], gbot_tui.STATUS_STYLE["blocked"])

    def test_a_streaming_reply_stays_working_through_quiet_polls(self):
        page = gbot_tui.parse_thread(
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
        chat = gbot_tui.Chat("Chef")
        chat.phase = "working"
        chat.cursor = "old"
        chat.ingest(page)
        quiet = gbot_tui.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
        chat.ingest(quiet)
        chat.ingest(quiet)
        self.assertEqual(chat.phase, "working")

    def test_the_scan_publishes_each_bot_and_keeps_a_total_failure(self):
        bots = [{"name": "A", "id": "1"}, {"name": "B", "id": "2"}]

        def read(bot):
            return {"entries": [{"id": "m", "role": "user", "text": "go"}], "cursor": "m"}

        survey = gbot_tui.Survey(bots, read)
        try:
            deadline = time.monotonic() + 2
            running, checked, total = True, 0, 2
            while time.monotonic() < deadline:
                running, checked, total, _generation = survey.status()
                if checked == total and not running:
                    break
                time.sleep(0.01)
            self.assertEqual((checked, total, running), (2, 2, False))
            self.assertEqual(gbot_tui.lookup_activity(bots[0])["state"], "working")
            self.assertEqual(survey.problem(), "")
        finally:
            survey.close()

        def fail(bot):
            raise gbot_tui.GbotError("gbot is not on PATH")

        _reset_overview()
        survey = gbot_tui.Survey([{"name": "A", "id": "1"}], fail)
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
                raise gbot_tui.GbotError("timeout")
            return {"entries": [{"id": "m", "role": "assistant", "text": "ok"}], "cursor": "m"}

        views, error = gbot_tui.build_overview(bots, read)
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
        snap = gbot_tui.judge_activity(entries, "w", "u")
        self.assertEqual(snap["state"], "blocked")
        self.assertEqual(snap["preview"], "Ship it?")
        self.assertEqual(gbot_tui.status_label(snap["state"]), "needs you")
        text = gbot_tui.format_overview(
            [{"name": "Chef", "activity": snap}]
        )
        self.assertIn("needs you", text)
        self.assertIn("Ship it?", text)
        running = gbot_tui.format_overview(
            [{"name": "Chef", "activity": {"state": "working", "unread": 0, "preview": "go"}}]
        )
        self.assertIn("1 running", running)
        self.assertIn("running", running.splitlines()[-1])
        self.assertNotIn("working", running)
        self.assertEqual(gbot_tui.status_label("working"), "working")
        self.assertEqual(gbot_tui.overview_summary(
            [{"name": "Chef", "activity": {"state": "working", "unread": 0}}]
        ), "1 working")
        stale = [widget, {"id": "u2", "role": "user", "text": "yes"}]
        later = gbot_tui.judge_activity(stale, "u2", "w")
        self.assertNotEqual(later["state"], "blocked")
        self.assertEqual(later["state"], "working")

    def test_working_gives_up_when_nothing_new_arrives(self):
        class Clock:
            def __init__(self, at):
                self.at = at
                self.old = None

            def __enter__(self):
                self.old = gbot_tui.clock
                gbot_tui.clock = lambda: self.at
                return self

            def __exit__(self, *args):
                gbot_tui.clock = self.old

        entries = [
            {"id": "m1", "role": "assistant", "text": "hi"},
            {"id": "m2", "role": "user", "text": "go"},
        ]
        with Clock(50) as clock:
            snap = gbot_tui.judge_activity(entries, "m2", "m2")
            self.assertEqual(snap["state"], "working")
            clock.at = 50 + gbot_tui.WORKING_TIMEOUT - 1
            snap = gbot_tui.judge_activity(entries, "m2", "m2", previous=snap)
            self.assertEqual(snap["state"], "working")
            clock.at = 50 + gbot_tui.WORKING_TIMEOUT
            snap = gbot_tui.judge_activity(entries, "m2", "m2", previous=snap)
            self.assertEqual(snap["state"], "idle")
            idle = snap
            moved = [
                {"id": "m1", "role": "assistant", "text": "hi"},
                {"id": "m2", "role": "user", "text": "go"},
                {"id": "m3", "role": "assistant", "text": "partial", "isStreaming": True},
            ]
            snap = gbot_tui.judge_activity(moved, "m3", "m3", previous=snap)
            self.assertEqual(snap["state"], "working")
            spans = gbot_tui.picker_spans(
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
            views, error = gbot_tui.build_overview(bots, read)
            self.assertIsNone(error)
            self.assertEqual(views[0]["activity"]["state"], "working")
            clock.at = 10 + gbot_tui.WORKING_TIMEOUT
            views, error = gbot_tui.build_overview(bots, read)
            self.assertEqual(views[0]["activity"]["state"], "idle")

        page = gbot_tui.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": entries},
                "cursor": "m2",
            }
        )
        with Clock(1000) as clock:
            chat = gbot_tui.Chat("Chef")
            chat.ingest(page, first=True)
            self.assertEqual(chat.phase, "working")
            header = "".join(text for text, _style in gbot_tui.title_spans(chat.title, chat.phase, 40))
            self.assertIn("working", header)
            self.assertEqual(gbot_tui.title_spans(chat.title, "working", 40)[1][1], "cyan")
            quiet = gbot_tui.parse_thread({"transcript": {"entries": []}, "cursor": "m2"})
            clock.at = 1000 + gbot_tui.WORKING_TIMEOUT - 1
            chat.ingest(quiet)
            self.assertEqual(chat.phase, "working")
            self.assertIn("Generating", "".join(text for text, _style in gbot_tui.footer_spans(chat.phase, "")))
            clock.at = 1000 + gbot_tui.WORKING_TIMEOUT
            chat.ingest(quiet)
            self.assertEqual(chat.phase, "idle")
            self.assertNotIn("Generating", "".join(text for text, _style in gbot_tui.footer_spans(chat.phase, "")))
            self.assertNotIn("working", "".join(text for text, _style in gbot_tui.title_spans(chat.title, chat.phase, 40)))
            self.assertEqual(gbot_tui.lookup_activity({"name": "Chef", "id": "a2"})["state"], "idle")

        stream = gbot_tui.parse_thread(
            {
                "target": {"id": "a2", "name": "Chef"},
                "transcript": {"entries": streaming},
                "cursor": "m2",
            }
        )
        with Clock(0) as clock:
            chat = gbot_tui.Chat("Chef")
            chat.ingest(stream, first=True)
            self.assertEqual(chat.phase, "working")
            clock.at = gbot_tui.WORKING_TIMEOUT
            self.assertTrue(chat.expire_working())
            self.assertEqual(chat.phase, "idle")
            grown = gbot_tui.parse_thread(
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


class RouteTests(unittest.TestCase):
    names = ["Chef", "Cursor Cloud Session"]

    def test_routes_by_prefix_and_slug(self):
        self.assertEqual(gbot_tui.parse_route("@che make soup", self.names), ("Chef", "make soup"))
        self.assertEqual(
            gbot_tui.parse_route("@Cursor-Cloud-Session do it", self.names),
            ("Cursor Cloud Session", "do it"),
        )

    def test_plain_text_is_not_routed(self):
        self.assertIsNone(gbot_tui.parse_route("hello @chef", self.names))
        self.assertIsNone(gbot_tui.parse_route("@chef", self.names))

    def test_unknown_bot_raises(self):
        with self.assertRaises(gbot_tui.GbotError):
            gbot_tui.parse_route("@nobody hi", self.names)


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
        rows = [text for kind, text in gbot_tui.transcript_rows([beat, answer], 80) if kind == "text"]
        self.assertEqual(rows, ["Just one cloud agent is working on Hangry."])
        # Still the latest line, so it stays until the reply shows up.
        alone = [text for kind, text in gbot_tui.transcript_rows([beat], 80) if kind == "text"]
        self.assertEqual(alone, ["Checking the agent now."])
        # A scheduled fact is the reply. A ticket number inside a progress line is not.
        plan = {
            "kind": "send-message",
            "id": "t2s1",
            "message": {"type": "text", "content": "Mach ich. Ich lege Moustafa von 14:00 bis 14:30."},
        }
        kept = [text for kind, text in gbot_tui.transcript_rows([plan, answer], 80) if kind == "text"]
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
        rows = [text for kind, text in gbot_tui.transcript_rows([looking, posted], 80) if kind == "text"]
        self.assertEqual(rows, ["Dein Kommentar steht jetzt auf EXE-3318."])
        promise = {
            "kind": "send-message",
            "id": "t4s1",
            "message": {
                "type": "text",
                "content": 'Ich schicke die Antwort jetzt ohne Grußformel im Thread "Re: GH Copilot".',
            },
        }
        gone = [text for kind, text in gbot_tui.transcript_rows([promise, posted], 80) if kind == "text"]
        self.assertEqual(gone, ["Dein Kommentar steht jetzt auf EXE-3318."])
        decision = {
            "kind": "send-message",
            "id": "t5s1",
            "message": {"type": "text", "content": "Okay, I'll leave those 7 alone."},
        }
        stayed = [text for kind, text in gbot_tui.transcript_rows([decision, posted], 80) if kind == "text"]
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
        rows = gbot_tui.transcript_rows([sending, asked, sent], 80)
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
        self.assertTrue(gbot_tui.token_expired(self.jwt(100), now=200))
        self.assertFalse(gbot_tui.token_expired(self.jwt(300), now=200))

    def test_garbage_counts_as_live(self):
        self.assertFalse(gbot_tui.token_expired(""))
        self.assertFalse(gbot_tui.token_expired("not.a.jwt"))


if __name__ == "__main__":
    unittest.main()
