import base64
import os
import tempfile
import unittest
from pathlib import Path

import gbot_tui

_HOME = {}


def setUpModule():
    # The roster and thread caches live under ~/.cache. Keep a real one out of the tests.
    _HOME["old"] = os.environ.get("HOME")
    _HOME["dir"] = tempfile.TemporaryDirectory()
    os.environ["HOME"] = _HOME["dir"].name


def tearDownModule():
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
                self.assertEqual(gbot_tui.APP, "gbot-abc")
                report = gbot_tui.osc_report("idle", "Outfitly")
                self.assertIn("app=gbot-abc", report)
            finally:
                gbot_tui.APP = previous
            programs = __import__("json").loads((root / "programs.json").read_text())
            self.assertEqual(programs["programs"]["gbot-abc"]["icon"], made["gbot-abc"]["icon"])
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
                self.assertEqual(gbot_tui.APP, compact)
                report = gbot_tui.osc_report("working", "Researcher")
                self.assertIn(f"app={compact}", report)
                self.assertLessEqual(len(compact), 32)
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
        self.assertEqual(gbot_tui.readable_hex(original, dark=False), original)

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


if __name__ == "__main__":
    unittest.main()
