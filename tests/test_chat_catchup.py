#!/usr/bin/env python3
"""helm chat catchup — the verb for "seen, park it", born from a real flood.

Mute silences the ambient wake; the addressed backlog stays pending because a
direct ask is an obligation. Until this verb there was no deliberate act
between "answer every row" and "wait out the stop-guard" — measured
2026-07-28 when the integrator muted a flooded room and its stop-guard kept
re-arming on the churning backlog.

Three laws pinned here, each from a live incident and one an affected-party
verdict (meld e:1785274962):
  - SELF-ONLY: parking another seat's backlog hides THEIR obligations.
  - addressed rows park only behind --including-mentions, and a room holding
    any is refused in default mode — cursors are single offsets, so parking
    "around" an addressed row is positionally impossible.
  - parking an addressed row posts a DURABLE TRACE into the room naming who
    parked how many asks from whom. Accountability is never removed.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-catchup-", var="HELM_HOME")

from helm import chat, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_SCRATCH_GC", "HELM_CACHE_DIR",
            "MELD_CHAT_DIR")


class CatchupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-catchup-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "me"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        # a tracked seat whose cursor sits BEFORE the backlog
        seats.write_roster("me", session="s1", cwd=self.tmp)
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "me", *st, base=st[2])

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pending(self):
        return seats._pending_rows("main", "me", backfill=True)

    def run_verb(self, *args, room="main"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd("catchup", list(args), room)
        return rc, out.getvalue() + err.getvalue()


class AmbientParkTest(CatchupBase):
    """The reflexive case: home-room noise, no direct asks."""

    def setUp(self):
        super().setUp()
        seats.write_roster("me", session="s1", cwd=self.tmp,
                           home_room="main")
        chat.post("fleet status chatter one", room="main", who="alice")
        chat.post("fleet status chatter two", room="main", who="bob")

    def test_dry_run_lists_and_writes_nothing(self):
        before = seats._cursor("main", "me")
        out = seats.catchup("me")
        self.assertEqual(out["parked"], 2)
        self.assertFalse(out["applied"])
        self.assertEqual(seats._cursor("main", "me"), before)
        self.assertEqual(len(self.pending()), 2, "dry run must not consume")

    def test_apply_parks_and_the_stop_guard_agrees(self):
        out = seats.catchup("me", apply=True)
        self.assertEqual(out["parked"], 2)
        self.assertEqual(len(self.pending()), 0)
        rows, _ = chat.read("main")
        self.assertEqual(seats._cursor("main", "me").get("rid"),
                         rows[-1].get("id"))

    def test_parked_rows_remain_READABLE(self):
        """Parking moves delivery state, never content — availability is
        the leg that must survive (attention-budget law)."""
        seats.catchup("me", apply=True)
        texts = [str(r.get("text")) for r in chat.read("main")[0]]
        self.assertIn("fleet status chatter one", texts)
        self.assertIn("fleet status chatter two", texts)

    def test_session_scoped_cursors_move_too(self):
        st0 = seats._baseline_state("main", at_start=True)
        seats._write_cursor("main", "me", *st0, base=st0[2],
                            session="sess12345678")
        seats.catchup("me", apply=True)
        cur = seats._cursor("main", "me", session="sess12345678")
        rows, _ = chat.read("main")
        self.assertEqual(cur.get("rid"), rows[-1].get("id"),
                         "delivery prefers the session cursor — leaving it "
                         "behind re-floods the seat it was parked for")

    def test_the_stopfp_latch_is_cleared_in_the_same_act(self):
        latch = seats._stop_fp_path("main", "me")
        with open(latch, "w") as f:
            f.write("stale-fingerprint")
        seats.catchup("me", apply=True)
        self.assertFalse(os.path.exists(latch),
                         "stale block state must not survive the decision "
                         "it gated")


class AddressedRowTest(CatchupBase):
    """The affected-party half: direct asks are obligations."""

    def setUp(self):
        super().setUp()
        seats.write_roster("me", session="s1", cwd=self.tmp,
                           home_room="main")
        chat.post("ambient chatter", room="main", who="alice")
        chat.post("@me please review the thing", room="main", who="bob")

    def test_default_mode_REFUSES_the_room_and_parks_nothing(self):
        """Cursors are single offsets: parking 'around' the addressed row is
        positionally impossible, so the honest default is all-or-nothing."""
        before = seats._cursor("main", "me")
        out = seats.catchup("me", apply=True)
        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["held"], 2)
        self.assertEqual(seats._cursor("main", "me"), before)
        self.assertEqual(len(self.pending()), 2)

    def test_the_flag_parks_and_posts_the_TRACE(self):
        out = seats.catchup("me", apply=True, include_addressed=True)
        self.assertEqual(out["parked"], 2)
        self.assertEqual(len(self.pending()), 0)
        rows, _ = chat.read("main")
        trace = [r for r in rows if "parked" in str(r.get("text", ""))
                 and str(r.get("from")) == "me"]
        self.assertEqual(len(trace), 1, "parking an ask must leave a trace")
        t = trace[0]["text"]
        self.assertIn("1 addressed row", t)
        self.assertIn("bob", t)             # the sender can see who was parked
        self.assertIn("remain above", t)    # and that access was not removed

    def test_the_trace_is_posted_BEFORE_the_cursors_move(self):
        """A crash between the two must leave over-accounting, never a
        silent park — the same either-alone-is-a-trap law as restore."""
        real_write = seats._write_cursor
        def boom(*a, **k):
            raise RuntimeError("crash between trace and cursor")
        try:
            seats._write_cursor = boom
            with self.assertRaises(RuntimeError):
                seats.catchup("me", apply=True, include_addressed=True)
        finally:
            seats._write_cursor = real_write
        rows, _ = chat.read("main")
        self.assertTrue(any("parked" in str(r.get("text", ""))
                            for r in rows),
                        "the trace must already be durable")
        self.assertGreater(len(self.pending()), 0,
                           "and the rows must still be pending")

    def test_dry_run_marks_which_rows_are_addressed(self):
        out = seats.catchup("me")
        marks = {m["text"][:8]: m["addressed"]
                 for m in out["rooms"]["main"]["rows"]}
        self.assertFalse(marks["ambient "])
        self.assertTrue(marks["@me plea"])

    def test_addressed_predicate_agrees_with_deliverable(self):
        """The anti-drift pin: _addressed mirrors deliverable's positive
        address branches. If they diverge, catchup parks something delivery
        would have treated as a direct ask, or vice versa."""
        sc = seats.seat_scope("me")
        for r in self.pending():
            if seats._addressed(r, "me"):
                self.assertTrue(seats.deliverable(r, "me", "main", sc),
                                "an addressed row must be deliverable")


class SelfOnlyTest(CatchupBase):
    def test_another_seats_backlog_is_refused(self):
        seats.write_roster("other", session="s2", cwd=self.tmp)
        rc, out = self.run_verb("--seat", "other", "--apply")
        self.assertEqual(rc, 2)
        self.assertIn("cannot catch up for", out)

    def test_junk_tail_refuses_before_anything(self):
        """The APPLY_READER_EXEMPT probe for the catchup branch."""
        from unittest import mock
        for argv in (["--bogus", "--apply"], ["--bogus"],
                     ["--apply", "extra"]):
            with mock.patch.object(seats, "catchup") as p:
                rc, out = self.run_verb(*argv)
            self.assertEqual(rc, 2, (argv, out))
            self.assertFalse(p.called, argv)

    def test_nothing_pending_is_an_honest_no_op(self):
        rc, out = self.run_verb()
        self.assertEqual(rc, 0)
        self.assertIn("nothing to park", out)


class CliContractTest(CatchupBase):
    def setUp(self):
        super().setUp()
        seats.write_roster("me", session="s1", cwd=self.tmp,
                           home_room="main")
        chat.post("noise", room="main", who="alice")
        chat.post("@me an ask", room="main", who="bob")

    def test_junk_tail_refuses_before_anything(self):
        from unittest import mock
        with mock.patch.object(seats, "catchup") as p:
            rc, out = self.run_verb("--frobnicate")
        self.assertEqual(rc, 2)
        self.assertFalse(p.called)

    def test_held_room_output_says_what_to_DO(self):
        rc, out = self.run_verb("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("HELD", out)
        self.assertIn("--including-mentions", out)

    def test_the_flag_flows_through_the_cli(self):
        rc, out = self.run_verb("--including-mentions", "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("parked 2 rows", out)
        self.assertEqual(len(self.pending()), 0)


if __name__ == "__main__":
    unittest.main()
