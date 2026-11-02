import base64
import os
import signal
import tempfile
import termios
import time
import unittest
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

    def test_thinking_is_not_message_text(self):
        self.assertEqual(gbot_tui.entry_text({"kind": "thinking", "text": "plan the reply"}), "")
        self.assertEqual(
            gbot_tui.entry_text({"kind": "tool-call", "name": "thinking", "content": "still planning"}),
            "",
        )
        self.assertEqual(
            gbot_tui.entry_text({
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "private"},
                    {"type": "text", "text": "the answer"},
                ],
            }),
            "the answer",
        )
        self.assertEqual(
            gbot_tui.entry_text({
                "kind": "send-message",
                "message": {"type": "text", "content": "<thinking>\nhidden\n</thinking>\n\nvisible"},
            }),
            "visible",
        )
        self.assertEqual(
            gbot_tui.entry_text({"role": "assistant", "text": "<think>streaming the plan"}),
            "",
        )
        self.assertEqual(gbot_tui.entry_text({"role": "user", "text": "<think>keep this</think> hi"}), "<think>keep this</think> hi")
        self.assertEqual(gbot_tui.entry_text({"role": "assistant", "text": "yo"}), "yo")

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

    def test_thinking_does_not_finish_the_turn(self):
        phase, quiet, saw = gbot_tui.watch_step(
            "working",
            [{"kind": "thinking", "text": "hmm"}],
            0,
            False,
            False,
        )
        self.assertEqual((phase, quiet, saw), ("working", 0, False))

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

    def test_thinking_stays_off_the_transcript(self):
        rows = gbot_tui.transcript_rows(
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
        marked = gbot_tui.transcript_rows(
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
        shown = " ".join(gbot_tui.line_plain(line) for kind, line in marked if kind != "gap")
        self.assertIn("hello", shown)
        self.assertNotIn("**", shown)
        self.assertNotIn("private plan", shown)
        self.assertNotIn("still hidden", shown)

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


class MarkdownTests(unittest.TestCase):
    def _plain(self, text, width=40):
        rows = gbot_tui.transcript_rows([{"role": "assistant", "text": text}], width)
        return [gbot_tui.line_plain(line) for kind, line in rows if kind == "text"]

    def _styles(self, text, width=40):
        rows = gbot_tui.transcript_rows([{"role": "assistant", "text": text}], width)
        found = []
        for _kind, line in rows:
            if isinstance(line, list):
                found.extend(style for _text, style in line)
        return found

    def test_plain_text_stays_a_string(self):
        rows = gbot_tui.transcript_rows([{"role": "assistant", "text": "earlier"}], 40)
        self.assertIn(("text", "earlier"), rows)
        self.assertEqual(gbot_tui.render_markdown("2 * 3 = 6", 40), ["2 * 3 = 6"])
        self.assertEqual(gbot_tui.render_markdown("snake_case stays", 40), ["snake_case stays"])
        self.assertEqual(gbot_tui.render_markdown("**hello", 40), ["**hello"])

    def test_a_plain_bullet_still_hangs(self):
        self.assertEqual(
            [gbot_tui.line_plain(line) for line in gbot_tui.render_markdown("- hello world", 10)],
            gbot_tui.wrap("- hello world", 10),
        )
        self.assertEqual(
            [gbot_tui.line_plain(line) for line in gbot_tui.render_markdown("**abcdefghijklmnopqrstuvwxyz**", 10)],
            gbot_tui.wrap("abcdefghijklmnopqrstuvwxyz", 10),
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
        lines = gbot_tui.render_markdown("**hello world**", 8)
        self.assertEqual([gbot_tui.line_plain(line) for line in lines], ["hello", "world"])
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
        squeezed = gbot_tui.render_markdown(
            "| a | b | c |\n| --- | --- | --- |\n| 1 | 2 | 3 |",
            4,
        )
        self.assertTrue(squeezed)
        self.assertTrue(all(len(gbot_tui.line_plain(line)) <= 4 for line in squeezed))

    def test_a_user_bubble_renders_markdown_and_stays_rectangular(self):
        rows = gbot_tui.transcript_rows([{"role": "user", "text": "say **hi** now"}], 40)
        lines = [line for kind, line in rows if kind == "user"]
        self.assertEqual(len(lines), 1)
        self.assertIn("hi", gbot_tui.line_plain(lines[0]))
        self.assertNotIn("*", gbot_tui.line_plain(lines[0]))
        wide = gbot_tui.transcript_rows(
            [{"role": "user", "text": "**hello**\nworld"}],
            40,
        )
        bubbles = [gbot_tui.line_plain(line) for kind, line in wide if kind == "user"]
        self.assertGreater(len(bubbles), 1)
        self.assertEqual(len(set(map(len, bubbles))), 1)

    def test_the_preview_drops_markers(self):
        entries = [{"role": "assistant", "text": "**lunch** is ready"}]
        self.assertEqual(gbot_tui.activity_preview(entries, "idle"), "lunch is ready")

    def test_styles_keep_the_bot_color_except_code(self):
        base = 7
        bold = gbot_tui.transcript_attr("bold", base)
        self.assertEqual(bold & gbot_tui.curses.A_BOLD, gbot_tui.curses.A_BOLD)
        self.assertEqual(bold & ~gbot_tui.curses.A_BOLD, base)
        self.assertEqual(gbot_tui.transcript_attr("code", base), gbot_tui.ATTR["gray"])
        self.assertEqual(gbot_tui.transcript_attr("dim", base), gbot_tui.ATTR["gray"])
        link = gbot_tui.transcript_attr("link", base)
        self.assertEqual(link & gbot_tui.curses.A_UNDERLINE, gbot_tui.curses.A_UNDERLINE)
        bubble = gbot_tui.user_attr("code", base)
        self.assertEqual(bubble & base, base)
        self.assertEqual(bubble & gbot_tui.curses.A_DIM, gbot_tui.curses.A_DIM)

    def test_a_markdown_reply_draws_without_the_markers(self):
        chat = gbot_tui.Chat("Researcher")
        chat.messages = [{"role": "assistant", "text": "Say **hello** and `go`."}]
        screen = FakeScreen(24, 58)
        gbot_tui.draw(screen, chat)
        shown = " ".join(" ".join(row) for row in screen.rows.values())
        self.assertIn("hello", shown)
        self.assertIn("go", shown)
        self.assertNotIn("**", shown)
        self.assertNotIn("`", shown)
        self.assertIn("●", shown)


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
        chat = gbot_tui.Chat("Cloud")
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
    paths = [
        gbot_tui.activity_path(),
        gbot_tui.seen_path(),
        gbot_tui.thread_cache_path("Chef"),
        gbot_tui.thread_cache_path("Researcher"),
        gbot_tui.thread_cache_path("Night"),
    ]
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


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
        self.assertEqual(gbot_tui.status_label("unread", 2), "2 unread")
        self.assertEqual(gbot_tui.status_label("unread", 1), "unread")

    def test_thinking_is_not_a_new_reply(self):
        entries = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "role": "assistant", "text": "done"},
            {"id": "m3", "kind": "thinking", "text": "more private"},
        ]
        snap = gbot_tui.judge_activity(entries, "m3", "m1")
        self.assertEqual(snap["state"], "unread")
        self.assertEqual(snap["unread"], 1)
        self.assertEqual(snap["preview"], "done")
        waiting = [
            {"id": "m1", "role": "user", "text": "go"},
            {"id": "m2", "kind": "thinking", "text": "private plan"},
        ]
        snap = gbot_tui.judge_activity(waiting, "m2", "m1")
        self.assertEqual(snap["state"], "working")
        self.assertEqual(snap["unread"], 0)
        self.assertEqual(snap["preview"], "go")
        marked = [{"role": "assistant", "text": "<thinking>nope</thinking>\n\n**lunch** is ready"}]
        self.assertEqual(gbot_tui.activity_preview(marked, "idle"), "lunch is ready")

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
            "1 needs you · 1 unread · 1 running",
        )
        self.assertEqual(gbot_tui.overview_summary([], checked=0, total=4), "checking threads")
        self.assertEqual(
            gbot_tui.overview_summary(bots[:2], checked=1, total=4),
            "1 running · checking 1 of 4",
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
        self.assertEqual(spans[2], ("needs you  ", "yellow"))
        self.assertEqual(spans[3], ("Outfitly", "bold"))
        self.assertEqual(spans[4], (" · ", "dim"))
        self.assertEqual(spans[5], ("npm publish", "dim"))
        self.assertLess(
            "".join(text for text, _style in spans).index("needs you"),
            "".join(text for text, _style in spans).index("Outfitly"),
        )
        narrow = "".join(text for text, _style in gbot_tui.picker_spans(bot, False, 16))
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
        columns = gbot_tui.list_columns(bots, 80)
        self.assertIsNotNone(columns)
        words = ["needs you", "3 unread", "running", "idle", "-"]
        starts = []
        for bot, word in zip(bots, words):
            spans = gbot_tui.picker_spans(bot, False, 80, columns)
            row = "".join(text for text, _style in spans)
            self.assertIn(word, spans[2][0])
            self.assertLess(row.index(word), row.index(bot["name"]))
            self.assertEqual(spans[-1][1], "dim")
            starts.append(sum(len(text) for text, _style in spans[:-1]))
        self.assertEqual(len(set(starts)), 1)
        styles = [
            gbot_tui.picker_spans(bot, False, 80, columns)[2][1]
            for bot in bots
        ]
        self.assertEqual(styles, ["yellow", "green", "bold", "dim", "dim"])

    def test_a_saved_thread_shows_its_state_before_the_scan(self):
        gbot_tui.write_thread_cache("Chef", [{"id": "m1", "role": "user", "text": "cook"}])
        gbot_tui.write_thread_cache(
            "Researcher",
            [
                {"id": "m1", "role": "user", "text": "go"},
                {"id": "m2", "role": "assistant", "text": "the paper is out"},
            ],
        )
        gbot_tui.remember_seen("Researcher", "a1", "m1")
        gbot_tui.store_activity(
            {"name": "Night", "id": "a9"},
            {"state": "unread", "unread": 1, "cursor": "m9", "preview": "done"},
        )
        gbot_tui.write_thread_cache("Night", [{"id": "m1", "role": "assistant", "text": "old"}])
        views = gbot_tui.with_activity(
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
        running = "".join(text for text, _style in gbot_tui.picker_spans(by_name["Chef"], False, 70))
        self.assertIn("running", running)
        self.assertLess(running.index("running"), running.index("Chef"))
        unread = "".join(text for text, _style in gbot_tui.picker_spans(by_name["Researcher"], False, 70))
        self.assertIn("unread", unread)
        self.assertLess(unread.index("unread"), unread.index("Researcher"))

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
        self.assertIn("1 unread", text)
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


if __name__ == "__main__":
    unittest.main()
