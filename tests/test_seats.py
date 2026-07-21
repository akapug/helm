#!/usr/bin/env python3
"""helm seats — the delivery lane (meld-half port). Hermetic: HELM_CHAT_DIR +
HELM_HOME are tmp dirs, HELM_CHAT_NODE_URL set-but-empty kills the signed
transport, HELM_CHAT_OWNER_NAMES pinned so the unix login never leaks into
addressing assertions. The hook legs are fed synthetic hook JSON; no harness,
no node, no network."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_DELIVER")


class SeatsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seats-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, verb, args=(), stdin=None, room="main"):
        out, err = io.StringIO(), io.StringIO()
        fake = None
        if stdin is not None:
            fake = types.SimpleNamespace(buffer=io.BytesIO(stdin))
        ctx = mock.patch.object(sys, "stdin", fake) if fake else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), room)
        return rc, out.getvalue(), err.getvalue()


class AddressingTest(SeatsBase):
    def test_mention_broadcast_owner_and_noise(self):
        row = lambda frm, text: {"ts": "t", "from": frm, "text": text}
        self.assertTrue(seats.deliverable(row("x", "hey @alice look"), "alice"))
        self.assertTrue(seats.deliverable(row("x", "@ALL standup"), "alice"))
        self.assertTrue(seats.deliverable(row("david", "no mention at all"), "alice"))
        # noise law: agent chatter without a mention does NOT deliver
        self.assertFalse(seats.deliverable(row("bob", "chatting about @alicein"), "alice"))
        self.assertFalse(seats.deliverable(row("alice", "@alice self"), "alice"))
        self.assertFalse(seats.deliverable({"ts": "t", "from": "david",
                                            "react": "🔥", "tts": "t", "tfrom": "x"},
                                           "alice"))
        # boundary: @alice-2 must not hit seat alice
        self.assertFalse(seats.deliverable(row("x", "ping @alice-2"), "alice"))

    def test_owner_names_env_override(self):
        os.environ["HELM_CHAT_OWNER_NAMES"] = "boss, Chief"
        self.assertEqual(seats.owner_names(), {"boss", "chief"})
        self.assertTrue(seats.deliverable(
            {"ts": "t", "from": "Boss", "text": "go"}, "alice"))


class JoinTest(SeatsBase):
    def test_join_writes_roster_and_baselines_cursor(self):
        chat.post("history 1", who="old")
        chat.post("history 2 @alice", who="old")
        seat, line = seats.join(session="sess-1234", cwd="/tmp/projx", seat="alice")
        self.assertEqual(seat, "alice")
        self.assertIn("seat 'alice'", line)
        row = seats.roster()["alice"]
        self.assertEqual(row["session"], "sess-1234")
        self.assertEqual(row["project"], "projx")
        self.assertTrue(row["joined"])
        # baseline: the pre-join backlog (even an @alice row) never floods
        self.assertIsNone(seats.deliver(seat="alice"))

    def test_join_hook_json_leg(self):
        payload = json.dumps({"session_id": "s-77", "cwd": "/tmp/p"}).encode()
        rc, out, _ = self.cmd("join", ["--hook-json", "--seat", "zed"], stdin=payload)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("seat 'zed'", d["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(seats.seat_for_session("s-77"), "zed")

    def test_join_fail_open_on_garbage_stdin(self):
        rc, out, err = self.cmd("join", ["--hook-json"], stdin=b"not json{{")
        self.assertEqual(rc, 0)
        # garbage session is just absent — the join still lands, never raises
        self.assertEqual(err, "")


class DeliverTest(SeatsBase):
    def seat_up(self, seat="alice"):
        seats.join(session="s-" + seat, cwd="/tmp/p", seat=seat)

    def test_one_per_boundary_with_waiting_count(self):
        self.seat_up()
        chat.post("@alice first", who="bob")
        chat.post("@alice second", who="bob")
        line = seats.deliver(seat="alice")
        self.assertIn("bob: @alice first", line)
        self.assertIn("(+1 waiting", line)
        line2 = seats.deliver(seat="alice")
        self.assertIn("@alice second", line2)
        self.assertNotIn("waiting", line2)
        self.assertIsNone(seats.deliver(seat="alice"))

    def test_owner_post_delivers_without_mention(self):
        self.seat_up()
        chat.post("course correction", who="david")
        line = seats.deliver(seat="alice")
        self.assertIn("david: course correction", line)

    def test_chatter_advances_cursor_silently(self):
        self.seat_up()
        chat.post("agent noise", who="bob")
        chat.post("more noise", who="carol")
        self.assertIsNone(seats.deliver(seat="alice"))
        # cursor caught up: the next boundary is the one-stat O(1) path
        n, size = seats._cursor("main", "alice")
        self.assertEqual(n, chat.read("main")[1])
        self.assertEqual(size, os.path.getsize(chat.room_path("main")))

    def test_scrub_and_clip(self):
        self.seat_up()
        hostile = "@alice \x1b[31mred\x1b[0m line1\nline2 sep " + "x" * 400
        chat.post(hostile, who="bob")
        line = seats.deliver(seat="alice")
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\n", line)
        self.assertNotIn(" ", line)
        # the clipped payload (after the label prefix) stays inside budget
        payload = line.split("bob: ", 1)[1]
        self.assertLessEqual(len(payload.encode("utf-8")),
                             seats.MAX_BYTES + len("…".encode("utf-8")) + 32)
        self.assertIn("…", line)

    def test_kill_switch(self):
        self.seat_up()
        chat.post("@alice ping", who="bob")
        os.environ["HELM_CHAT_DELIVER"] = "0"
        self.assertIsNone(seats.deliver(seat="alice"))

    def test_marker_untouched_by_delivery(self):
        self.seat_up()
        chat.post("steer", who="david")
        chat.mark_owner_unread("main")
        self.assertIsNotNone(seats.deliver(seat="alice"))
        self.assertTrue(os.path.exists(chat.marker_path("main")))

    def test_rotation_resets_cursor_and_still_delivers(self):
        self.seat_up()
        seats._write_cursor("main", "alice", 999, -1)   # cursor past a rotated room
        chat.post("@alice after rotation", who="bob")
        line = seats.deliver(seat="alice")
        self.assertIn("after rotation", line)

    def test_unknown_session_self_heals_roster(self):
        chat.post("noise", who="bob")   # room exists, no cursor for this seat
        self.assertIsNone(seats.deliver(session="brand-new-session"))
        self.assertIn("agent-brand-ne", seats.roster())
        chat.post("@agent-brand-ne go", who="bob")
        self.assertIn("go", seats.deliver(session="brand-new-session"))

    def test_deliver_hook_json_shape_and_fail_open(self):
        self.seat_up()
        chat.post("@alice hi", who="bob")
        sid = seats.roster()["alice"]["session"]
        payload = json.dumps({"session_id": sid}).encode()
        rc, out, _ = self.cmd("deliver", ["--hook-json"], stdin=payload)
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("bob: @alice hi", d["hookSpecificOutput"]["additionalContext"])
        # fail-open: a raising core never surfaces into the boundary
        with mock.patch.object(seats, "deliver", side_effect=RuntimeError("boom")):
            rc, out, err = self.cmd("deliver", ["--hook-json"], stdin=b"{}")
        self.assertEqual((rc, out, err), (0, "", ""))


class WaitTest(SeatsBase):
    def test_wait_returns_pending_and_advances_cursor(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice now", who="bob")
        line = seats.wait(seat="alice", timeout=1, poll=0.01)
        self.assertIn("@alice now", line)
        self.assertIsNone(seats.deliver(seat="alice"))  # no re-nudge

    def test_wait_timeout_rc1(self):
        seats.join(seat="alice", cwd="/tmp/p")
        rc, out, _ = self.cmd("wait", ["--seat", "alice", "--timeout", "0.05"])
        self.assertEqual(rc, 1)

    def test_wait_any_sees_only_rows_after_arming(self):
        import threading
        chat.post("pre-existing", who="bob")
        line = seats.wait(any_row=True, timeout=0.2, poll=0.01)
        self.assertIsNone(line)   # nothing NEW after arming — the old row never fires
        t = threading.Timer(0.05, chat.post, args=("newest",),
                            kwargs={"who": "carol"})
        t.start()
        try:
            line = seats.wait(any_row=True, timeout=2, poll=0.01)
        finally:
            t.join()
        self.assertIn("newest", line)


class ClaimsTest(SeatsBase):
    def test_exclusion_extend_release(self):
        ok, _ = seats.claim("worktree-main", "alice", ttl=60)
        self.assertTrue(ok)
        ok, msg = seats.claim("worktree-main", "bob", ttl=60)
        self.assertFalse(ok)
        self.assertIn("held by alice", msg)
        ok, _ = seats.claim("worktree-main", "alice", ttl=120)   # holder extends
        self.assertTrue(ok)
        ok, msg = seats.release("worktree-main", "bob")
        self.assertFalse(ok)
        self.assertIn("only the holder", msg)
        ok, _ = seats.release("worktree-main", "alice")
        self.assertTrue(ok)
        self.assertEqual(seats.claims_list(), [])

    def test_ttl_expiry_frees_the_lease(self):
        seats.claim("port-8900", "alice", ttl=0)     # expires immediately
        ok, _ = seats.claim("port-8900", "bob", ttl=60)
        self.assertTrue(ok)
        rows = seats.claims_list()
        self.assertEqual([r["holder"] for r in rows], ["bob"])

    def test_release_unclaimed(self):
        ok, msg = seats.release("ghost", "alice")
        self.assertFalse(ok)
        self.assertIn("not claimed", msg)


class CouncilTest(SeatsBase):
    def test_embargo_then_reveal_once(self):
        seats.verdict("design-x", "alice", "approve — the seam is right")
        seats.verdict("design-x", "bob", "reject — hidden coupling")
        seats.verdict("design-x", "alice", "approve with nit")   # replace mine
        rows = [m["text"] for m in chat.read("main")[0] if m.get("text")]
        self.assertTrue(all("sealed a verdict" in t for t in rows))
        self.assertFalse(any("hidden coupling" in t for t in rows))  # embargoed
        n = seats.reveal("design-x")
        self.assertEqual(n, 2)
        rows = [m["text"] for m in chat.read("main")[0]]
        self.assertTrue(any("hidden coupling" in t for t in rows))
        self.assertTrue(any("approve with nit" in t for t in rows))
        self.assertFalse(os.path.exists(seats.council_path("design-x")))
        self.assertEqual(seats.reveal("design-x"), 0)   # the embargo lifts once


class RosterReportTest(SeatsBase):
    def test_report_presence_pending_preview_claims(self):
        seats.join(seat="alice", cwd="/tmp/projx", session="s-a")
        chat.post("@alice one", who="bob")
        chat.post("@alice two", who="bob")
        seats.claim("db-migrate", "alice", ttl=60)
        rep = seats.roster_report("main")
        s = rep["seats"][0]
        self.assertEqual(s["seat"], "alice")
        self.assertEqual(s["presence"], "fresh")
        self.assertEqual(s["pending"], 2)
        self.assertIn("two", s["preview"])
        self.assertEqual(rep["claims"][0]["resource"], "db-migrate")

    def test_presence_tiers(self):
        seats.touch_roster("old-seat")
        r = seats.roster()
        r["old-seat"]["last_seen"] = time.time() - 10_000
        from helm import pk
        pk.write_json(seats.roster_path(), r)
        rep = seats.roster_report("main")
        self.assertEqual(rep["seats"][0]["presence"], "absent")

    def test_cli_seats_table(self):
        seats.join(seat="alice", cwd="/tmp/p")
        rc, out, _ = self.cmd("seats")
        self.assertEqual(rc, 0)
        self.assertIn("alice", out)
        self.assertIn("fresh", out)


class WebRosterTest(SeatsBase):
    def test_endpoint_shape_and_fail_open(self):
        seats.join(seat="alice", cwd="/tmp/p")
        obj, code = web._api_chat_roster({})
        self.assertEqual(code, 200)
        self.assertEqual(obj["seats"][0]["seat"], "alice")
        with mock.patch.object(seats, "roster_report", side_effect=RuntimeError):
            obj, code = web._api_chat_roster({})
        self.assertEqual(code, 200)
        self.assertEqual(obj, {"seats": [], "claims": [], "unavailable": True})


class ChatDispatchTest(SeatsBase):
    def test_chat_verbs_reach_seats(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chat.cmd_chat(["claim", "res-1", "--seat", "alice"])
        self.assertEqual(rc, 0)
        self.assertEqual(seats.claims_list()[0]["holder"], "alice")


if __name__ == "__main__":
    unittest.main()
