#!/usr/bin/env python3
"""helm chat — RAM-room groupchat tests. Hermetic: HELM_CHAT_DIR + HELM_HOME
are tmp dirs (read through home.env at call time); the real /dev/shm/helm-chat
and ~/.helm are never touched. --follow itself is interactive and untested;
the polling read primitive it loops on (chat.read) is pinned here."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, home, reflex  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG")


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chat-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        # SET-BUT-EMPTY disables the signed transport — v1 behavior, hermetic
        # even when a real room node is live on this machine
        os.environ["HELM_CHAT_NODE_URL"] = ""

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, args, stdin_text=""):
        out, err = io.StringIO(), io.StringIO()
        stdin_prior = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = chat.cmd_chat(list(args))
        finally:
            sys.stdin = stdin_prior
        return rc, out.getvalue(), err.getvalue()


class RoomTest(ChatBase):
    def test_post_read_roundtrip_and_since(self):
        chat.post("first", who="a1")
        chat.post("second", who="a2")
        msgs, total = chat.read()
        self.assertEqual(total, 2)
        self.assertEqual([m["text"] for m in msgs], ["first", "second"])
        for m in msgs:
            self.assertEqual(sorted(m), ["from", "text", "ts"])
        tail, total = chat.read(since=1)
        self.assertEqual(total, 2)
        self.assertEqual([m["text"] for m in tail], ["second"])
        self.assertEqual(chat.read(since=2), ([], 2))  # caught up

    def test_since_past_the_end_resets(self):
        # a rotation shrank the room under a poller — it re-syncs, never starves
        chat.post("only", who="a1")
        msgs, total = chat.read(since=99)
        self.assertEqual((len(msgs), total), (1, 1))

    def test_env_dir_and_room_layout(self):
        chat.post("hi", who="a1")
        self.assertEqual(chat.chat_dir(), os.environ["HELM_CHAT_DIR"])
        path = chat.room_path("main")
        self.assertTrue(path.startswith(os.environ["HELM_CHAT_DIR"]))
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "hi")  # one JSON obj/line
        mode = stat.S_IMODE(os.stat(chat.chat_dir()).st_mode)
        self.assertEqual(mode, 0o700)  # the room dir is the operator's

    def test_from_env_name_wins(self):
        os.environ["HELM_CHAT_NAME"] = "pilot"
        self.assertEqual(chat.post("x")["from"], "pilot")

    def test_unparseable_lines_skipped(self):
        chat.post("good", who="a1")
        with open(chat.room_path("main"), "a") as f:
            f.write("not json\n[1,2]\n")
        msgs, total = chat.read()
        self.assertEqual(total, 1)
        self.assertEqual(msgs[0]["text"], "good")

    def test_rotation_keeps_the_newest_half(self):
        with mock.patch.object(chat, "SIZE_CAP", 400):
            for i in range(20):
                chat.post("msg-%02d" % i, who="a1")
        msgs, total = chat.read()
        self.assertLess(total, 20)                       # oldest half rotated out
        self.assertEqual(msgs[-1]["text"], "msg-19")     # newest kept
        self.assertNotIn("msg-00", [m["text"] for m in msgs])
        self.assertLessEqual(os.path.getsize(chat.room_path("main")), 400)


class MarkerTest(ChatBase):
    def test_mark_and_consume_semantics(self):
        chat.post("agents?", who="david")
        chat.mark_owner_unread()
        mp = chat.marker_path("main")
        with open(mp) as f:
            self.assertEqual(f.read(), "1")  # the count at owner-post time
        self.assertFalse(chat.consume(total=0))   # not consumed past the post
        self.assertTrue(os.path.exists(mp))
        self.assertTrue(chat.consume(total=1))    # seen it -> cleared
        self.assertFalse(os.path.exists(mp))
        self.assertFalse(chat.consume(total=9))   # no marker -> nothing to clear

    def test_cli_read_clears_the_marker(self):
        chat.post("fleet, look alive", who="david")
        chat.mark_owner_unread()
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("david: fleet, look alive", out)
        self.assertFalse(os.path.exists(chat.marker_path("main")))

    def test_reflex_fires_while_marker_exists_and_not_after(self):
        home.scaffold_global()  # seeds the pack; marker path resolves to tmp
        self.assertEqual(reflex.fire("any turn text"), [])
        chat.post("ship it", who="david")
        chat.mark_owner_unread()
        fired = [e["id"] for e in reflex.fire("any turn text")]
        self.assertEqual(fired, ["owner-chat-unread"])
        self.run_cmd(["read"])  # the read consumes past the owner's post
        self.assertEqual(reflex.fire("any turn text"), [])


class CmdTest(ChatBase):
    def test_post_and_read_cycle(self):
        rc, out, _ = self.run_cmd(["post", "hello", "crew"])
        self.assertEqual(rc, 0)
        self.assertIn("hello crew", out)
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("hello crew", out)

    def test_read_since_flag(self):
        chat.post("one", who="a1")
        chat.post("two", who="a1")
        rc, out, _ = self.run_cmd(["read", "--since", "1"])
        self.assertEqual(rc, 0)
        self.assertNotIn("one", out)
        self.assertIn("two", out)

    def test_rooms_listing(self):
        chat.post("hi", who="a1")
        chat.post("ops talk", room="ops", who="a2")
        chat.mark_owner_unread("ops")
        rc, out, _ = self.run_cmd(["rooms"])
        self.assertEqual(rc, 0)
        self.assertIn("main  1 msg", out)
        self.assertIn("ops  1 msg [owner-unread]", out)

    def test_room_flag_scopes_post_and_read(self):
        self.assertEqual(self.run_cmd(["post", "sidebar", "--room", "ops"])[0], 0)
        self.assertEqual(chat.read("main"), ([], 0))
        rc, out, _ = self.run_cmd(["read", "--room", "ops"])
        self.assertIn("sidebar", out)

    def test_empty_read_and_bad_args(self):
        rc, out, _ = self.run_cmd(["read"])
        self.assertEqual(rc, 0)
        self.assertIn("no messages", out)
        self.assertEqual(self.run_cmd(["post"])[0], 2)            # nothing to say
        self.assertEqual(self.run_cmd(["read", "--since", "x"])[0], 2)
        self.assertEqual(self.run_cmd(["post", "x", "--room"])[0], 2)
        self.assertEqual(self.run_cmd(["bogus"])[0], 2)

    def test_rooms_empty(self):
        rc, out, _ = self.run_cmd(["rooms"])
        self.assertEqual(rc, 0)
        self.assertIn("no rooms yet", out)


class RowIntegrityTest(ChatBase):
    def test_unicode_line_separator_never_tears_the_row(self):
        """U+2028/U+2029 inside a message (a voice paste can carry them) must
        not split the JSON row for readers — read() splits on exactly \\n,
        never str.splitlines() (found by the delivery lane 2026-07-20)."""
        chat.post("voice paste second visual line third", who="bob")
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertIn(" ", rows[0]["text"])
        chat.post("padding", who="bob")
        p = chat.room_path("main")
        chat._rotate(p, cap=1)   # force rotation through the same split law
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["text"], "padding")


if __name__ == "__main__":
    unittest.main()
