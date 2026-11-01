import base64
import os
import unittest

import gbot_tui


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
        self.assertIn("→", mid)
        self.assertIn("Add a follow-up", mid)
        _top, typed, _bot = gbot_tui.composer_lines(32, "hello")
        self.assertIn("hello", typed)
        self.assertNotIn("Add a follow-up", typed)

    def test_user_text_is_a_bubble_and_the_reply_is_plain(self):
        rows = gbot_tui.transcript_rows(
            [
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "yo"},
            ],
            40,
        )
        self.assertEqual([kind for kind, _text in rows], ["user", "gap", "text"])

    def test_footer_states(self):
        self.assertEqual(gbot_tui.footer_spans("blocked", "")[0], ("Pending decision", "yellow"))
        working = "".join(text for text, _style in gbot_tui.footer_spans("working", ""))
        self.assertIn("Generating", working)
        self.assertEqual(gbot_tui.footer_spans("idle", "no session")[0][1], "red")


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
