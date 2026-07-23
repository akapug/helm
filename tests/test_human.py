#!/usr/bin/env python3
"""helm --human — the TUI's model/render functions, headless (the curses
screen-scrape harness pattern: no TTY, no curses window; render() returns the
exact strings the shell paints). The shell itself gets an import + layout
smoke plus the no-TTY refusal."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, human  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
            "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM_SOURCE",
            "HELM_CHAT_LOG", "MELD_CHAT_LOG")


class HumanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-human-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class ModelTest(HumanBase):
    def test_feed_appends_and_rotation_resets(self):
        m = human.model_new()
        human.model_feed(m, [{"ts": "", "from": "a", "text": "1"}], 1)
        human.model_feed(m, [{"ts": "", "from": "a", "text": "2"}], 2)
        self.assertEqual([r["text"] for r in m["rows"]], ["1", "2"])
        # the room rotated under us: smaller total resets the row set
        human.model_feed(m, [{"ts": "", "from": "a", "text": "9"}], 1)
        self.assertEqual([r["text"] for r in m["rows"]], ["9"])
        self.assertEqual(m["total"], 1)

    def test_operator_name_env_then_david(self):
        self.assertEqual(human.operator_name(), "david")
        os.environ["HELM_CHAT_NAME"] = "skipper"
        self.assertEqual(human.operator_name(), "skipper")


class RenderTest(HumanBase):
    def test_chat_pane_tags_wraps_and_aggregates(self):
        chat.post("a really long unsigned message that will wrap", who="a1")
        chat.react(1, ":tada:", who="a2")
        chat.react(1, ":tada:", who="a3")
        m = human.model_new()
        human.model_feed(m, *chat.read())
        lines = human.chat_pane(m, 24, 10)
        self.assertTrue(any("[unsigned]" in l for l in lines))
        self.assertTrue(any("🎉×2" in l for l in lines))
        self.assertTrue(all(len(l) <= 24 * 2 for l in lines))
        # bottom-anchored: a 2-row pane keeps only the tail
        self.assertEqual(human.chat_pane(m, 24, 2), lines[-2:])

    def test_chat_pane_empty_state(self):
        m = human.model_new()
        self.assertIn("no messages yet", human.chat_pane(m, 60, 5)[0])

    def test_status_line_signed_and_unsigned(self):
        m = human.model_new()
        m["quota"] = "quota claude x 63% left"
        m["status"] = {"mode": "signed", "url": "http://x", "head": 12}
        s = human.status_line(m, 80)
        self.assertIn("SIGNED #12", s)
        self.assertIn("63% left", s)
        m["status"] = {"mode": "unsigned", "url": None, "head": None}
        self.assertIn("UNSIGNED -", human.status_line(m, 80))
        m["status"] = {"mode": "degraded", "head": 12, "profile": "seat-a",
                       "reason": "send failed", "last_age_s": 7}
        loud = human.status_line(m, 120)
        self.assertIn("DEGRADED #12", loud)
        self.assertIn("seat-a: send failed", loud)
        self.assertIn("last 7s ago", loud)
        m["status"] = {"mode": "degraded", "head": None,
                       "profile": "seat\x1b[31m\x00\x85‮",
                       "reason": "node\x1b[2J\x01\x85‮ down",
                       "last_age_s": 1}
        hostile = human.status_line(m, 160)
        for ch in ("\x1b", "\x00", "\x01", "\x85", "‮"):
            self.assertNotIn(ch, hostile)

    def test_input_line_keeps_the_cursor_end_visible(self):
        m = human.model_new()
        m["input"] = "abcdefghij"
        self.assertEqual(human.input_line(m, 8), "defghij")  # the tail wins
        m["input"] = "hi"
        self.assertEqual(human.input_line(m, 20), "> hi")

    def test_render_frame_shape(self):
        chat.post("hello", who="a1")
        m = human.model_new()
        human.model_feed(m, *chat.read())
        frame = human.render(m, 10, 40)
        self.assertEqual(sorted(frame), ["chat", "input", "status"])
        self.assertLessEqual(len(frame["chat"]), 8)  # h-2 rows for the pane

    def test_quota_headline_cached_only(self):
        with mock.patch("helm.brief._seats", return_value=[]):
            self.assertIn("helm creds", human.quota_headline())
        seats = [{"provider": "anthropic", "account": "a@b", "headroom_pct": 41.0},
                 {"provider": "anthropic", "account": "c@d", "headroom_pct": 80.0}]
        with mock.patch("helm.brief._seats", return_value=seats):
            self.assertEqual(human.quota_headline(), "quota anthropic c@d 80% left")


class SubmitTest(HumanBase):
    def test_submit_posts_as_operator_and_marks_unread(self):
        m = human.model_new()
        m["input"] = "  hello fleet :fire:  "
        human._submit(m)
        self.assertEqual(m["input"], "")
        rows, total = chat.read()
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["from"], "david")
        self.assertEqual(rows[0]["text"], "hello fleet 🔥")
        # the OWNER posted: the marker drops so agents get the reflex nudge
        self.assertTrue(os.path.exists(chat.marker_path("main")))

    def test_submit_react_passthrough_and_errors(self):
        chat.post("target", who="a1")
        m = human.model_new()
        m["input"] = "/react 1 :tada:"
        human._submit(m)
        self.assertEqual(m["notice"], "")
        self.assertEqual(chat.read()[0][-1]["react"], "🎉")
        m["input"] = "/react nope"
        human._submit(m)
        self.assertIn("usage", m["notice"])
        m["input"] = "/react 99 :tada:"
        human._submit(m)
        self.assertIn("out of range", m["notice"])

    def test_empty_submit_is_a_noop(self):
        m = human.model_new()
        m["input"] = "   "
        human._submit(m)
        self.assertEqual(chat.read(), ([], 0))


class ShellSmokeTest(HumanBase):
    def test_paint_degrades_twice_never_raises(self):
        # degrade law: addnstr raising UnicodeEncodeError on BOTH passes
        # (an unmapped emoji on a legacy locale) skips the line, never crashes
        class BoomScreen:
            def getmaxyx(self):
                return (5, 20)

            def erase(self):
                pass

            def refresh(self):
                pass

            def addnstr(self, *a):
                raise UnicodeEncodeError("ascii", "x", 0, 1, "boom")

        class FakeCurses:
            error = RuntimeError
            A_REVERSE = 1
            A_NORMAL = 0

        human._paint(BoomScreen(), human.model_new(), FakeCurses)  # no raise

    def test_no_tty_refuses_cleanly(self):
        out, err = io.StringIO(), io.StringIO()  # StringIO.isatty() is False
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(human.cmd_human([]), 1)
        self.assertIn("terminal", err.getvalue())

    def test_layout_functions_run_at_odd_sizes(self):
        # the smoke the PRD asks for: import + layout at hostile geometry
        chat.post("smoke :rocket:", who="a1")
        m = human.model_new()
        human.model_feed(m, *chat.read())
        for h, w in ((3, 10), (24, 80), (5, 2)):
            frame = human.render(m, h, w)
            self.assertTrue(frame["status"] is not None)
            self.assertTrue(isinstance(frame["chat"], list))


if __name__ == "__main__":
    unittest.main()
