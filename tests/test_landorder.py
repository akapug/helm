#!/usr/bin/env python3
"""THE LAND ORDER: every writer of a land veto or of land authority waits for
the readiness lock (task/3265 races R2).

`helm train auto`'s last word holds the readiness lock (the dispatch ledger's,
`landwindow.readiness_lock`) from its last read of the plan, the admission and
the receipt's land authority through its push. A veto written in that window
would be one the push never saw. So each writer below takes the same lock
before it writes: its veto is either visible to the last word's reads or
ordered after the push.

Each arm holds the lock on one thread, runs the writer on another, and reads
whether the write finished while the lock was held and after it was let go.
The controls are writes that carry no land authority, run the same way: they
finish while the lock is held.
"""
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-landorder-", var="HELM_HOME")

from helm import (dispatches, gate, gatecanary, landorder,  # noqa: E402
                  landwindow, proxywatch, seats, store)

PROXY = {"family": "kimi", "agent_harness": "claude", "backend": "proxy"}


class Writers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-landorder-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CHAT_NAME": "zz-seat"})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.path.join(self.tmp, "home"))

    def waits(self, write):
        """(finished while the readiness lock was held on another thread,
        finished once it was let go)."""
        taken, release = threading.Event(), threading.Event()
        done, failed = threading.Event(), []

        def holder():
            with landwindow.readiness_lock() as held:
                self.assertTrue(held)
                taken.set()
                release.wait(60)

        def writer():
            try:
                write()
            except Exception as exc:            # noqa: BLE001 — reported
                failed.append(exc)
            done.set()

        hold = threading.Thread(target=holder, daemon=True)
        hold.start()
        self.assertTrue(taken.wait(30))
        work = threading.Thread(target=writer, daemon=True)
        work.start()
        during = done.wait(0.5)
        release.set()
        after = done.wait(30)
        hold.join(30)
        work.join(30)
        self.assertEqual(failed, [])
        return during, after

    # -- the gate canary: its DISABLE marker and its record ---------------
    def test_the_canary_disable_marker_waits(self):
        self.assertEqual(self.waits(lambda: gatecanary.write_marker(
            {"reason": "serial and sliced disagree", "divergences": []},
            {"tree": "e" * 40, "head": "a" * 40, "id": "serial-1"},
            {"id": "sliced-1"})), (False, True))
        self.assertTrue(os.path.exists(
            gate.sliced_land_marker_path()))

    def test_a_canary_record_verdict_waits(self):
        """A DIVERGED in the record restarts the canary's standing: the
        shadow's DISAGREE writes one (`gateshadow._to_canary_record`)."""
        self.assertEqual(self.waits(lambda: self.assertTrue(
            gatecanary.append_verdict(
                {"verdict": gatecanary.DIVERGED, "reason": "disagree",
                 "divergences": []},
                {"tree": "e" * 40, "id": "serial-1"}, {"id": "sliced-1"},
                gatecanary.SHADOW))), (False, True))

    def test_a_canary_finder_row_waits(self):
        """An unclean finder run is the newest one: it withdraws (i)."""
        self.assertEqual(self.waits(lambda: self.assertTrue(
            gatecanary.finder_rows({"host": "h", "each": {"clean": False},
                                    "whole": {"clean": False}},
                                   "e" * 40))), (False, True))

    # -- the approval-tier policy -------------------------------------------
    def prior(self, pid, kind=None):
        e = {"id": pid, "statement": "who may approve a land",
             "confidence": 1.0, "source": "human: the owner"}
        if kind:
            e.update(policy_kind=kind, policy_reason="the land reads it",
                     policy_members=["family:codex"])
        return store.write_prior(e, root_dir=os.path.join(self.tmp, "store"))

    def test_an_approval_tier_policy_waits(self):
        self.assertEqual(self.waits(
            lambda: self.prior("tier", "approval-tier")), (False, True))

    def test_rewriting_an_approval_tier_policy_away_waits(self):  # noqa: VACUOUS_ASSERTION — the rewrite positively lands once the lock is let go
        self.prior("tier", "approval-tier")
        self.assertEqual(self.waits(lambda: self.prior("tier")),
                         (False, True))
        with open(os.path.join(self.tmp, "store", "prior-tier.md"),
                  encoding="utf-8") as fh:
            self.assertNotIn("policy_kind", fh.read())

    def test_a_prior_that_carries_no_policy_does_not_wait(self):
        self.assertEqual(self.waits(lambda: self.prior("plain")),
                         (True, True))

    # -- the runtime testimony the admission reads ----------------------------
    def test_a_roster_write_carrying_runtime_testimony_waits(self):
        self.assertEqual(self.waits(lambda: seats.write_roster(
            "zz-seat", runtime=PROXY)), (False, True))
        self.assertEqual(seats.roster()["zz-seat"]["runtime"], PROXY)

    def test_a_roster_write_carrying_no_testimony_does_not_wait(self):
        self.assertEqual(self.waits(lambda: seats.write_roster("zz-seat")),
                         (True, True))

    # -- a join that rebinds or evicts testimony (door read B4) --------------
    ENTRY = {"runtime": dict(PROXY), "verified": True, "source": "lifecycle"}

    def roster(self, rows):
        from helm import chat, pk
        chat._ensure_dir()
        pk.write_json(seats.roster_path(), rows)

    def row_with_testimony(self, session, sessions):
        return {"runtime": dict(PROXY), "runtime_verified": True,
                "session": session, "sessions": list(sessions),
                "runtime_sessions": {s: dict(self.ENTRY) for s in sessions}}

    def test_a_join_rebinding_its_testified_row_waits(self):
        self.roster({"zz-seat": self.row_with_testimony("s-old",
                                                         ["s-old"])})
        self.assertEqual(self.waits(lambda: seats.write_roster(
            "zz-seat", session="s-new", presence_beat=False)), (False, True))
        self.assertEqual(seats.roster()["zz-seat"]["session"], "s-new")

    def test_a_join_evicting_another_rows_testimony_waits(self):
        self.roster({"other-seat": self.row_with_testimony(
            "s-o2", ["s-o1", "s-o2"])})
        self.assertEqual(self.waits(lambda: seats.write_roster(
            "zz-seat", session="s-o1", presence_beat=False)), (False, True))
        self.assertEqual(sorted(seats.roster()["other-seat"]
                                ["runtime_sessions"]), ["s-o2"])

    def test_a_presence_beat_of_a_testified_row_does_not_wait(self):
        self.roster({"zz-seat": self.row_with_testimony("s-1", ["s-1"])})
        self.assertEqual(self.waits(lambda: seats.write_roster(
            "zz-seat", session="s-1", presence_beat=False)), (True, True))
        self.assertEqual(seats.roster()["zz-seat"]["runtime_sessions"],
                         {"s-1": self.ENTRY})

    def test_a_join_touching_no_testimony_does_not_wait(self):
        self.roster({"other-seat": {"session": "s-o2",
                                    "sessions": ["s-o1", "s-o2"]}})
        self.assertEqual(self.waits(lambda: seats.write_roster(
            "zz-seat", session="s-o1", presence_beat=False)), (True, True))
        self.assertEqual(seats.roster()["other-seat"]["sessions"], ["s-o2"])

    def test_a_lifecycle_runtime_bind_waits(self):
        seats.write_roster("zz-seat")

        def bind():
            entry, why = seats.bind_lifecycle_runtime("zz-seat", "sid-life",
                                                      dict(PROXY))
            self.assertIsNone(why, why)

        self.assertEqual(self.waits(bind), (False, True))
        self.assertEqual(seats.roster()["zz-seat"]["session"], "sid-life")

    def test_a_measured_proxy_runtime_stamp_waits(self):
        seats.write_roster("zz-seat", session="sid-proxy")

        def stamp():
            with mock.patch.object(proxywatch, "_proxy_proof_runtime",
                                   lambda proof: (dict(PROXY), None)):
                entry, why = seats.stamp_proxy_runtime(
                    "sid-proxy", dict(PROXY), {"session": "sid-proxy"})
            self.assertIsNone(why, why)

        self.assertEqual(self.waits(stamp), (False, True))
        self.assertEqual(seats.roster()["zz-seat"]["runtime_sessions"]
                         ["sid-proxy"]["source"], "proxywatch")


class TheLock(unittest.TestCase):
    """`landorder.locked`: the dispatch ledger's own lock, reentrant on one
    thread, let go by closing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-landorder-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home")})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.path.join(self.tmp, "home"))

    def test_it_is_the_dispatch_ledgers_own_lock(self):  # noqa: VACUOUS_ASSERTION — each equality compares two present, non-empty names
        self.assertEqual(landorder.LEDGER, dispatches.LEDGER)
        self.assertEqual(landorder.path(),
                         os.path.abspath(dispatches.ledger_path()) + ".lock")
        self.assertEqual(landwindow.readiness_lock_path(), landorder.path())

    def test_a_thread_that_holds_it_passes_and_another_waits(self):  # noqa: VACUOUS_ASSERTION — the control thread positively takes the lock once it is let go
        other = []

        def contend():
            with landorder.locked(timeout=0) as held:
                other.append(held)

        with landorder.locked() as outer:
            with landorder.locked(timeout=0) as inner:
                racer = threading.Thread(target=contend)
                racer.start()
                racer.join(30)
            # the inner exit let nothing go
            racer = threading.Thread(target=contend)
            racer.start()
            racer.join(30)
        self.assertEqual((outer, inner), (True, True))
        self.assertEqual(other, [False, False])
        # the control: once the outer is let go, another thread takes it
        contend()
        self.assertEqual(other, [False, False, True])


if __name__ == "__main__":
    unittest.main()
