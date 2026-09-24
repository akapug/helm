#!/usr/bin/env python3
"""helm chat catchup — the verb for "seen, park it", born from a real flood.

Mute gates the idle beacon only; every hook/pending/stop backlog stays owed.
Until this verb there was no deliberate act
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
import threading
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-catchup-", var="HELM_HOME")

from helm import chat, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CHAT_DELIVER", "HELM_SCRATCH_GC",
            "HELM_CACHE_DIR", "MELD_CHAT_DIR",
            # The SESSION half of identity, which this fixture did not own.
            # It matters now that catchup resolves through acting_seat: an
            # unmanaged session id is the gate RUNNER's real one leaking into
            # every arm, and a test that sets one would leak it into every
            # later test in the file. Both directions are the same bug —
            # a fixture that does not own an input cannot isolate it.
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")
THREAD_TIMEOUT = 2


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
        # AND THE PROCESS PRESENTS THAT SESSION. The row above already binds s1
        # to "me"; without exporting it the fixture declares a name with
        # nothing behind it, and every ACT verb in this module refuses a
        # DECLARED name the roster cannot corroborate. Two arms below pop this
        # deliberately — that is their subject, and they say so.
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
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

    def test_session_only_backlog_is_discovered_and_parked(self):  # noqa: VACUOUS_ASSERTION — the same session queue is proven to contain 2 rows before its final empty assertion
        """A current seat cursor must not hide a stale session cursor.

        wait/deliver prefer the session cursor, so discovery must cover the
        same queue that apply already promises to advance.
        """
        session = "sess12345678"
        st0 = seats._baseline_state("main", at_start=True)
        seats._write_cursor("main", "me", *st0, base=st0[2],
                            session=session)
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "me", *st, base=st[2])
        self.assertEqual(len(seats._pending_rows(
            "main", "me", session=session, backfill=True)), 2)
        self.assertEqual(len(self.pending()), 0,
                         "the seat cursor is intentionally current")

        out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 2)
        self.assertEqual(seats._pending_rows(
            "main", "me", session=session, backfill=True), [])

    def test_legacy_long_session_cursor_is_read_and_advanced_exactly(self):
        base = seats.cursor_path("main", "me")
        legacy = base + ".legacy-session-suffix-longer-than-eight"
        st0 = seats._baseline_state("main", at_start=True)
        seats._write_cursor_path(legacy, st0, base=st0[2])
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "me", *st, base=st[2])

        out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 2)
        self.assertEqual(seats.pk.read_json(legacy, {}).get("off"), st[2])

    def test_rows_appended_after_the_snapshot_remain_pending(self):
        real = seats._catchup_pending
        def interleave(*args, **kwargs):
            out = real(*args, **kwargs)
            chat.post("@me arrived after classification", room="main",
                      who="carol")
            return out
        with mock.patch.object(seats, "_catchup_pending", interleave):
            out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 2)
        pending = seats._pending_rows("main", "me", backfill=True)
        self.assertEqual([m["text"] for m in pending],
                         ["@me arrived after classification"])

    def test_inflight_delivery_cannot_move_a_parked_cursor_backward(self):
        session = "sess12345678"
        st0 = seats._baseline_state("main", at_start=True)
        seats._write_cursor("main", "me", *st0, base=st0[2],
                            session=session)
        self.assertEqual(len(seats._pending_rows(
            "main", "me", session=session, backfill=True)), 2)
        emitted, resume = (threading.Event() for _ in range(2))
        result = {}

        def emit(_line):
            emitted.set()
            self.assertTrue(resume.wait(THREAD_TIMEOUT))

        def deliver():
            # This test owns cursor emit/commit ordering. The public wrapper's
            # proxywatch and identity gates have their own tests and can veto
            # before this race begins based on machine-global state.
            seats._deliver_unpaused(
                session=session, room="main", seat="me", emit=emit)

        def park():
            result.update(seats.catchup("me", apply=True))

        delivery = threading.Thread(target=deliver)
        delivery.start()
        self.assertTrue(emitted.wait(THREAD_TIMEOUT))
        parking = threading.Thread(target=park)
        parking.start()
        parking.join(0.05)
        self.assertTrue(parking.is_alive(),
                        "catchup crossed an in-flight delivery transaction")
        resume.set()
        parking.join(THREAD_TIMEOUT)
        delivery.join(THREAD_TIMEOUT)
        self.assertFalse(delivery.is_alive())
        self.assertFalse(parking.is_alive())
        self.assertEqual(result["parked"], 2)
        self.assertEqual(seats._pending_rows(
            "main", "me", session=session, backfill=True), [])

    def test_cursor_advanced_past_snapshot_is_not_rewound(self):
        session = "s1"
        real = seats._catchup_pending
        advanced = {}

        def interleave(*args, **kwargs):
            out, targets = real(*args, **kwargs)
            target = targets["main"]
            path = seats.cursor_path("main", "me", session)
            seats._write_cursor_path(path, target, base=target[2])
            chat.post("@me after the snapshot", room="main", who="carol")
            self.assertIsNotNone(seats.deliver(
                session=session, room="main", seat="me", emit=lambda _line: None))
            advanced["off"] = seats._cursor(
                "main", "me", session=session)["off"]
            return out, targets

        with mock.patch.object(seats, "_catchup_pending", interleave):
            out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 2)
        self.assertEqual(seats._cursor(
            "main", "me", session=session)["off"], advanced["off"])
        self.assertEqual(seats._pending_rows(
            "main", "me", session=session, backfill=True), [])

    def test_delayed_session_initialization_inherits_the_parked_base(self):  # noqa: VACUOUS_ASSERTION — parked=2 proves backlog traversal before the late session queue is asserted empty
        session = "late-session"
        paused, resume, started = (threading.Event() for _ in range(3))
        from helm import seats_delivery
        real_commit = seats_delivery._commit_cursor_updates
        result = {}
        self.assertEqual(len(self.pending()), 2)

        def delayed_commit(updates):
            if seats.cursor_path("main", "me", session) in updates:
                paused.set()
                self.assertTrue(resume.wait(THREAD_TIMEOUT))
            return real_commit(updates)

        def initialize():
            seats._init_cursor("main", "me", session=session)

        def park():
            started.set()
            result.update(seats.catchup("me", apply=True))

        with mock.patch("helm.seats_delivery._commit_cursor_updates",
                        side_effect=delayed_commit):
            initializing = threading.Thread(target=initialize)
            initializing.start()
            self.assertTrue(paused.wait(THREAD_TIMEOUT))
            parking = threading.Thread(target=park)
            parking.start()
            self.assertTrue(started.wait(THREAD_TIMEOUT))
            parking.join(0.05)
            self.assertTrue(parking.is_alive())
            resume.set()
            initializing.join(THREAD_TIMEOUT)
            parking.join(THREAD_TIMEOUT)
        self.assertFalse(initializing.is_alive())
        self.assertFalse(parking.is_alive())
        self.assertEqual(result["parked"], 2)
        self.assertEqual(seats._pending_rows(
            "main", "me", session=session, backfill=True), [])

    def test_delayed_rehome_session_cannot_publish_a_pre_catchup_base(self):  # noqa: VACUOUS_ASSERTION — parked/retry counts prove the race ran before the final empty session queue
        session = "rehome-session"
        paused, resume, started = (threading.Event() for _ in range(3))
        real_baseline = seats._baseline_state
        calls = [0]
        result = {}

        def delayed_baseline(*args, **kwargs):
            state = real_baseline(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 1:
                paused.set()
                self.assertTrue(resume.wait(THREAD_TIMEOUT))
            return state

        def rehome():
            seats._baseline_room_cursors("main", "me", [session])

        def park():
            started.set()
            result.update(seats.catchup("me", apply=True))

        with mock.patch.object(seats, "_baseline_state", delayed_baseline):
            baselining = threading.Thread(target=rehome)
            baselining.start()
            self.assertTrue(paused.wait(THREAD_TIMEOUT))
            chat.post("arrived during rehome", room="main", who="carol")
            parking = threading.Thread(target=park)
            parking.start()
            self.assertTrue(started.wait(THREAD_TIMEOUT))
            parking.join(0.05)
            self.assertTrue(parking.is_alive(),
                            "catchup must serialize with cursor creation")
            resume.set()
            baselining.join(THREAD_TIMEOUT)
            parking.join(THREAD_TIMEOUT)
        self.assertFalse(baselining.is_alive())
        self.assertFalse(parking.is_alive())
        self.assertEqual(result["parked"], 3)
        self.assertEqual(result["retry"], 0)
        self.assertEqual(seats._pending_rows(
            "main", "me", session=session, backfill=True), [])

    def test_rehome_gives_every_session_one_shared_admission_cutoff(self):
        sessions = ["a-session", "b-session"]
        paused, resume = threading.Event(), threading.Event()
        from helm import seats_delivery
        real_commit = seats_delivery._commit_cursor_updates

        def delayed_commit(updates):
            paused.set()
            self.assertTrue(resume.wait(THREAD_TIMEOUT))
            return real_commit(updates)

        with mock.patch("helm.seats_delivery._commit_cursor_updates",
                        side_effect=delayed_commit):
            baselining = threading.Thread(
                target=seats._baseline_room_cursors,
                args=("main", "me", sessions))
            baselining.start()
            self.assertTrue(paused.wait(THREAD_TIMEOUT))
            chat.post("arrived during rehome", room="main", who="carol")
            resume.set()
            baselining.join(THREAD_TIMEOUT)
        self.assertFalse(baselining.is_alive())
        curs = [seats._cursor("main", "me", session=s) for s in sessions]
        self.assertEqual(curs[0]["off"], curs[1]["off"])
        pending = [[m["text"] for m in seats._pending_rows(
            "main", "me", session=s, backfill=True)] for s in sessions]
        self.assertEqual(pending,
                         [["arrived during rehome"],
                          ["arrived during rehome"]])

    def test_unreadable_discovery_is_unknown_not_an_empty_backlog(self):
        session = "stale-session"
        st0 = seats._baseline_state("main", at_start=True)
        seats._write_cursor("main", "me", *st0, base=st0[2],
                            session=session)
        st = seats._baseline_state("main")
        seats._write_cursor("main", "me", *st, base=st[2])

        with mock.patch.object(seats, "_catchup_cursor_paths",
                               side_effect=OSError("census unreadable")):
            out = seats.catchup("me", apply=True)

        self.assertTrue(out["unknown"])
        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["retry"], 0)
        self.assertEqual(out["rooms"], {})
        self.assertEqual(len(seats._pending_rows(
            "main", "me", session=session, backfill=True)), 2)

    def test_unreadable_apply_census_retries_every_classified_row(self):
        session = "stale-session"
        st0 = seats._baseline_state("main", at_start=True)
        seats._write_cursor("main", "me", *st0, base=st0[2],
                            session=session)
        paths = seats._catchup_cursor_paths("me")

        with mock.patch.object(seats, "_catchup_cursor_paths",
                               return_value=paths), \
                mock.patch("helm.seats_delivery.os.listdir",
                           side_effect=OSError("census unreadable")):
            out = seats.catchup("me", room="main", apply=True)

        self.assertTrue(out["unknown"])
        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["retry"], 2)
        self.assertEqual(out["rooms"]["main"]["retry"], 2)
        self.assertEqual(len(seats._pending_rows(
            "main", "me", session=session, backfill=True)), 2)

    def test_incomplete_trailing_row_is_not_consumed_unclassified(self):
        partial = (b'{"ts":"2026-01-01T00:00:00Z","from":"carol",'
                   b'"text":"late')
        with open(chat.room_path("main"), "ab") as f:
            complete = f.tell()
            f.write(partial)

        out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 2)
        self.assertEqual(seats._cursor("main", "me")["off"], complete)
        with open(chat.room_path("main"), "ab") as f:
            f.write(b' row","id":"abcdef123456"}\n')
        self.assertIn("late row", [m["text"] for m in self.pending()])

    def test_baseline_stops_before_an_incomplete_trailing_row(self):
        complete = os.path.getsize(chat.room_path("main"))
        self.assertGreater(complete, 0)
        with open(chat.room_path("main"), "ab") as f:
            f.write(b'{"ts":"unfinished')

        self.assertEqual(seats._baseline_state("main")[2], complete)

    def test_snapshot_write_failure_is_reported_and_keeps_the_latch(self):
        latch = seats._stop_fp_path("main", "me")
        with open(latch, "w") as f:
            f.write("stale-fingerprint")

        with mock.patch("helm.seats_catchup._commit_cursor_updates",
                        return_value=False):
            out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["retry"], 2)
        self.assertEqual(out["rooms"]["main"]["retry"], 2)
        self.assertNotIn("parking", out["rooms"]["main"])
        self.assertTrue(os.path.exists(latch))
        self.assertEqual(len(self.pending()), 2)

    def test_partial_paired_commit_rolls_every_cursor_back(self):
        from helm import seats_cursor
        session = "s1"
        paths = seats._cursor_paths("main", "me", [session])
        def raw(path):
            with open(path, "rb") as f:
                return f.read()

        before = {path: raw(path) for path in paths if os.path.exists(path)}
        real = seats_cursor._durable_text
        calls = [0]

        def fail_second(path, raw):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("second paired replace failed")
            return real(path, raw)

        with mock.patch("helm.seats_cursor._durable_text",
                        side_effect=fail_second):
            out = seats.catchup("me", apply=True)
        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["retry"], 2)
        after = {path: raw(path) for path in before}
        self.assertEqual(after, before)

    def test_incomplete_rollback_is_recovered_before_catchup_retry(self):
        from helm import seats_cursor
        paths = seats._cursor_paths("main", "me", ["s1"])
        real_write, calls = seats_cursor._durable_text, []

        def fail_after_first(path, raw):
            calls.append(path)
            if len(calls) >= 2:
                raise OSError("paired write/rollback failed")
            return real_write(path, raw)

        with mock.patch("helm.seats_cursor._durable_text",
                        side_effect=fail_after_first):
            first = seats.catchup("me", apply=True)
        self.assertEqual(first["parked"], 0)
        self.assertEqual(first["retry"], 2)
        self.assertTrue(any(seats_cursor._cursor_transaction_pending(path)
                            for path in paths))
        second = seats.catchup("me", apply=True)
        self.assertEqual(second["parked"], 2)
        self.assertEqual(second["retry"], 0)
        self.assertEqual(len(self.pending()), 0)

    def test_base_only_catchup_recovers_prepared_cursor_debt(self):
        from helm import pk, seats_cursor
        for path in list(seats._cursor_paths("main", "me", ["s1"])):
            parsed = seats_cursor.parse_cursor_path(path)
            if parsed and parsed["session_key"]:
                os.remove(path)
        pair = seats_cursor._cursor_pair("main", "me")
        updates = {path: dict(pk.read_json(path, {}), off=os.path.getsize(
            chat.room_path("main"))) for path in pair}
        real_write, calls = seats_cursor._durable_text, []

        def fail_after_first(path, raw):
            calls.append(path)
            if len(calls) >= 2:
                raise OSError("base write/rollback failed")
            return real_write(path, raw)

        with seats_cursor._cursor_locks(pair), mock.patch(
                "helm.seats_cursor._durable_text", side_effect=fail_after_first):
            result = seats_cursor._commit_cursor_updates(updates)
        self.assertFalse(result)
        self.assertFalse(result.rollback_complete)
        self.assertTrue(any(seats_cursor._cursor_transaction_pending(path)
                            for path in pair))
        out = seats.catchup("me", apply=True)
        self.assertEqual(out["parked"], 2)
        self.assertEqual(out["retry"], 0)
        self.assertEqual(len(self.pending()), 0)

    def test_catchup_cursor_read_rejects_pointer_publication_race(self):
        from helm import pk, seats_catchup, seats_cursor
        path = seats.cursor_path("main", "me")
        real = pk.read_json

        def raced(target, default=None):
            row = real(target, default)
            seats_cursor._durable_json(
                seats_cursor._cursor_txn_pointer(path),
                {"state": "committed", "tx": "catchup-publication-race"})
            return row

        with mock.patch("helm.seats_catchup.pk.read_json", side_effect=raced):
            with self.assertRaisesRegex(OSError, "changed during read"):
                seats_catchup._catchup_cursor(path)

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

    def test_the_trace_does_not_invalidate_its_own_snapshot(self):
        """The trace is a WRITE TO THE ROOM BEING PARKED, and production
        writes it by atomic replace — which changes the room file's inode,
        which is exactly what `_catchup_write_cursor` guards freshness by.

        MEASURED IN PRODUCTION 2026-08-14: every `--including-mentions
        --apply` reported "N rows NOT safely parked — retry catchup", six
        retries running, on a room whose inode was otherwise stable for
        seconds. Catchup was defeating itself, and the prescribed remedy
        could never converge because each retry re-posted the trace.

        THE SIBLING TEST ABOVE PASSED THROUGHOUT. It asserts the right
        things — parked, nothing pending — but this fixture's `post` appends
        in place, so the inode never moves and the guard never trips. A
        fixture more forgiving than production is not a rehearsal of the
        arm; it is a different test wearing its name. This one reproduces
        the replace so the assertion means what it says.
        """
        real_post = chat.post

        def replacing_post(*a, **kw):
            out = real_post(*a, **kw)
            room = kw.get("room") or "main"
            p = chat.room_path(room)
            tmp = p + ".replace-probe"
            with open(p, "rb") as src, open(tmp, "wb") as dst:
                dst.write(src.read())
            os.replace(tmp, p)          # new inode, byte-identical content
            return out

        before = os.stat(chat.room_path("main")).st_ino
        with mock.patch.object(chat, "post", replacing_post):
            out = seats.catchup("me", apply=True, include_addressed=True)
        after = os.stat(chat.room_path("main")).st_ino
        self.assertNotEqual(before, after,
                            "the probe did not actually replace the room "
                            "file — this test cannot see the defect it exists "
                            "for, and would pass vacuously")
        self.assertEqual(out.get("retry", 0), 0,
                         "an inode moved by catchup's OWN trace must not "
                         "refuse the park it was written for")
        self.assertGreater(out["parked"], 0)
        self.assertEqual(len(self.pending()), 0,
                         "and the backlog must actually be gone, not merely "
                         "un-complained-about")

    def test_the_trace_is_posted_BEFORE_the_cursors_move(self):  # noqa: VACUOUS_ASSERTION — trace presence and a positive pending-row count jointly prove the crash boundary
        """A crash between the two must leave over-accounting, never a
        silent park — the same either-alone-is-a-trap law as restore."""
        with mock.patch("helm.seats_catchup._commit_cursor_updates",
                        return_value=False):
            out = seats.catchup("me", apply=True, include_addressed=True)
        self.assertGreater(out["retry"], 0)
        rows, _ = chat.read("main")
        self.assertTrue(any("parked" in str(r.get("text", ""))
                            for r in rows),
                        "the trace must already be durable")
        self.assertGreater(len(self.pending()), 0,
                           "and the rows must still be pending")

    def test_addressed_row_beyond_scan_cap_holds_the_whole_room(self):
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "me", *st, base=st[2])
        seats._write_cursor("main", "me", *st, base=st[2], session="s1")
        rows = [{"ts": "2026-01-01T00:00:%02dZ" % (i % 60),
                 "from": "alice", "text": "x" * 1024, "id": "%012x" % i}
                for i in range(seats.SCAN_CAP // 1024 + 20)]
        rows.append({"ts": "2026-01-01T00:01:00Z", "from": "bob",
                     "text": "@me beyond the first window", "id": "f" * 12})
        with open(chat.room_path("main"), "ab") as f:
            for row in rows:
                f.write(json.dumps(row).encode() + b"\n")
        before = seats._cursor("main", "me")

        out = seats.catchup("me", apply=True)

        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["held"], len(rows))
        self.assertEqual(out["rooms"]["main"]["addressed"], 1)
        self.assertEqual(seats._cursor("main", "me"), before)

    def test_same_second_idless_twins_remain_distinct(self):
        st = seats._baseline_state("main", at_start=False)
        seats._write_cursor("main", "me", *st, base=st[2])
        seats._write_cursor("main", "me", *st, base=st[2], session="s1")
        rows = [
            {"ts": "2026-01-01T00:00:00Z", "from": "bob", "text": "plain"},
            {"ts": "2026-01-01T00:00:00Z", "from": "bob", "text": "@me ask"},
        ]
        with open(chat.room_path("main"), "ab") as f:
            for row in rows:
                f.write(json.dumps(row).encode() + b"\n")

        out = seats.catchup("me")

        self.assertEqual(out["parked"], 0)
        self.assertEqual(out["held"], 2)
        self.assertEqual(out["rooms"]["main"]["addressed"], 1)
        self.assertEqual([m["text"] for m in out["rooms"]["main"]["rows"]],
                         ["plain", "@me ask"])

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
        # "cannot catch up FOR" lost its preposition when the refusal stopped
        # being a reworded delivery message (`_assert_own_seat`'s "cannot
        # receive for", `.replace`d) and became the identity layer's own
        # sentence: `--seat 'other' cannot catch up as another seat`. The LAW
        # under test — SELF-ONLY — is unchanged and still pinned by the rc.
        self.assertIn("cannot catch up", out)
        self.assertIn("never selects", out,
                      "the refusal must say --seat is an assertion, or the "
                      "reader tries a different spelling of the same flag")

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

    def test_a_NAMELESS_pane_resolves_its_seat_by_law_not_by_env(self):
        """THE SILENT, SELF-CONGRATULATING FAILURE (measured 2026-08-14 on the
        integrator's own seat). `seat or own_name()` consulted ONE identity
        source; own_name() answers None for a process that declares no
        HELM_CHAT_NAME — which is exactly what a compacted or hook-joined pane
        normally is, and what this seat was while holding 272 unparked rows.

        catchup(None) finds zero rooms, so the verb printed 'nothing pending
        for None — nothing to park' and exited 0. The SAME call with the seat
        resolved by acting_seat found 272 rows in one room. A nameless seat
        could never park a backlog and was reassured, every single time, that
        it had none. Every other seat-scoped path in seats_cli already used
        acting_seat; catchup was the lone holdout, and it is the one verb whose
        entire job is the backlog."""
        os.environ.pop("HELM_CHAT_NAME", None)          # the nameless pane
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"     # but rostered to "me"
        rc, out = self.run_verb()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("for None", out,
                         "the seat resolved to None and the verb ran anyway")
        # The POSITIVE assertion, not merely the absence of "None": this seat
        # HAS an addressed row pending, so the held-room message must appear.
        # Without it, a verb that found nothing at all would also satisfy the
        # line above — the same empty answer wearing a different string.
        self.assertIn("--including-mentions", out,
                      "the backlog was not seen at all")

    def test_a_DERIVED_identity_refuses_because_it_is_a_minted_stranger(self):
        """THE FIRST VERSION OF THIS ARM ASSERTED A REFUSAL THAT COULD NEVER
        FIRE, and the gate caught it: it guarded `if not seat`, but there is
        no None to guard. acting_seat's floor MINTS a name from session and
        cwd, so the unresolved case arrives as an ordinary non-empty string.
        The gate's actual output was 'parked 1 rows for
        helm-test-catchup-9fsave4r-claude' — a seat nobody has ever been,
        holding a cursor advanced past rows nobody read.

        That is a better bug than the one the arm was written for, and it is
        why the states are now NAMED: DECLARED, ROSTERED and DERIVED are all
        non-empty and all identical to `if seat:`. Parking is precisely the
        act that must not run under a minted identity, because it MOVES
        CURSORS — which is how a backlog is declared seen."""
        os.environ.pop("HELM_CHAT_NAME", None)
        # POSITIVE CONTROL FIRST, on the same patched observable: a RESOLVABLE
        # session must reach the scan. Otherwise `p.called is False` below is
        # satisfied by a patch that never bound, or by a verb that refuses
        # everything — proving the refusal is about identity, not about the
        # arm being broken.
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"     # rostered to "me"
        with mock.patch.object(seats, "catchup") as ok:
            self.run_verb()
        self.assertTrue(ok.called,
                        "control: a resolvable identity never reached the scan")

        os.environ["CLAUDE_CODE_SESSION_ID"] = "unrostered-session"
        with mock.patch.object(seats, "catchup") as p:
            rc, out = self.run_verb()
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSING", out)
        self.assertIn("DERIVED", out,
                      "the refusal must name WHY it refused — a bare refusal "
                      "sends the reader looking for a broken seat")
        self.assertFalse(p.called,
                         "it parked a backlog under a minted stranger name")

    def test_the_three_identity_states_are_DISTINGUISHABLE_at_all(self):
        """The enum's own reason to exist. All three answers are non-empty
        strings, so `if seat:` cannot tell them apart — which is exactly how
        a minted name reached a cursor write. If this arm ever fails by two
        states collapsing, every caller that branches on provenance is
        silently back to guessing."""
        from helm.seats_identity import (DECLARED, DERIVED, ROSTERED,
                                         resolve_identity)
        os.environ["HELM_CHAT_NAME"] = "me"
        self.assertEqual(resolve_identity("s1", self.tmp)[0], DECLARED)

        os.environ.pop("HELM_CHAT_NAME", None)
        self.assertEqual(resolve_identity("s1", self.tmp), (ROSTERED, "me"))

        state, name = resolve_identity("no-such-session", self.tmp)
        self.assertEqual(state, DERIVED)
        # The load-bearing half: DERIVED is NOT None and NOT empty. Every
        # truthiness check in the tree waves this through, which is the whole
        # hazard the state name exists to make visible.
        self.assertTrue(name, "DERIVED returned a falsey name — then the old "
                              "`if not seat` guard would have worked and this "
                              "entire enum is unnecessary")
        self.assertNotEqual(name, "me")

    def test_a_NAMED_pane_is_untouched_by_the_new_resolution(self):
        """Control: the declared name still wins and still works. A fix that
        quietly moved every named seat onto the roster path would be the
        2026-07-24 contamination inversion all over again — and that is the
        one thing seats_identity says must never be done silently."""
        os.environ["HELM_CHAT_NAME"] = "me"
        rc, out = self.run_verb("--including-mentions", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("parked 2 rows", out)

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

    def test_retry_is_loud_and_exits_nonzero(self):
        with mock.patch("helm.seats_catchup._commit_cursor_updates",
                        return_value=False):
            rc, out = self.run_verb("--including-mentions", "--apply")

        self.assertEqual(rc, 1)
        self.assertIn("RETRY", out)
        self.assertIn("2 rows NOT safely parked", out)
        self.assertIn("retry catchup", out)
        self.assertEqual(len(self.pending()), 2)

    def test_unreadable_census_is_unknown_and_exits_nonzero(self):
        with mock.patch.object(seats, "_catchup_cursor_paths",
                               side_effect=OSError("census unreadable")):
            rc, out = self.run_verb("--apply")

        self.assertEqual(rc, 1)
        self.assertIn("UNKNOWN", out)
        self.assertIn("cursor census unreadable", out)
        self.assertNotIn("nothing pending", out)
        self.assertEqual(len(self.pending()), 2)


class IdentitylessCatchupTest(CatchupBase):
    """task/1074's THIRD door, and the worst of the three because THE STOP
    GUARD ROUTES SEATS INTO IT: its own message tells a seat with undelivered
    rows to park them with `helm chat catchup --including-mentions --apply`.

    `seat = seat or own_name()` carried a None straight through, so an
    identity-less process got "nothing pending for None — nothing to park"
    and read itself as CLEAR. Measured 2026-08-12: bare catchup reported
    nothing pending while an explicit seat that owns rows reported the real
    backlog on the SAME store. AN UNRESOLVED IDENTITY IS NOT AN EMPTY BACKLOG."""

    def setUp(self):
        super().setUp()
        seats.write_roster("me", session="s1", cwd=self.tmp,
                           home_room="main")
        chat.post("noise", room="main", who="alice")
        chat.post("@me an ask", room="main", who="bob")

    def test_no_identity_REFUSES_instead_of_reporting_an_empty_backlog(self):
        # NEITHER HALF, which is what makes this arm DIFFERENT from the
        # nameless-pane arm above: that one keeps a rostered session and must
        # RESOLVE; this one has no name AND no session and must REFUSE.
        del os.environ["HELM_CHAT_NAME"]
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        rc, out = self.run_verb("--including-mentions")
        self.assertEqual(rc, 2)
        # THE OLD BEHAVIOUR, NAMED SO IT CANNOT COME BACK QUIETLY: the reader
        # must never be told there is nothing to park when the question was
        # never answered.
        self.assertNotIn("nothing to park", out)
        self.assertNotIn("nothing pending for None", out)
        # THE CURED OBSERVABLE IS THE REFUSAL, NOT ITS PROSE. Trunk's actors
        # boundary now owns this door and refuses an unresolvable identity in
        # its own words (a DERIVED name, an unrostered one, an unset env all
        # phrase differently); pinning one door's sentence is how this arm
        # went red against a stronger cure of the same defect. "REFUSING" is
        # the SHOUTED contract word the verb guarantees.
        self.assertIn("REFUSING", out)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the SAME
        # store and rows DO yield the backlog once the identity is DECLARED.
        # Not `--seat me` from the identity-less process — under the actors
        # law an assertion needs an identity to check against, so that
        # spelling is itself refused now — a declared name, then the bare
        # verb. Without this the refusal above could equally mean the fixture
        # has no pending rows at all. BOTH HALVES COME BACK, because the
        # control has to be a whole identity: a name alone is what the refusal
        # above is ABOUT, so restoring only the name would make the control
        # refuse for the very reason it is meant to rule out.
        os.environ["HELM_CHAT_NAME"] = "me"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        rc, out = self.run_verb("--including-mentions")
        self.assertEqual(rc, 0, out)
        self.assertIn("parking", out)

    def test_a_named_seat_is_unaffected_by_the_guard(self):  # noqa: VACUOUS_ASSERTION — the absent observable is `len(pending()) == 0`, and its unconditional positive control is the SAME observable asserted NON-EMPTY against a literal immediately before the apply: `len(pending()) == 2`. If pending() were inert, absent, or always empty that control fails, so the zero cannot pass vacuously. Same shape as the neighbouring test_the_flag_flows_through_the_cli, which the rung does not flag only because it is not staged.
        """POLE: the cure must not make the ordinary path refuse. An identity
        that resolves behaves exactly as before."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, taken FIRST:
        # pending() is shown NON-EMPTY before the apply, so the zero after it
        # measures parking rather than a fixture that never had rows or a
        # helper that always reports none.
        self.assertEqual(len(self.pending()), 2)
        rc, out = self.run_verb("--including-mentions", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("parked 2 rows", out)
        self.assertEqual(len(self.pending()), 0)


if __name__ == "__main__":
    unittest.main()
