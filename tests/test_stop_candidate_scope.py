#!/usr/bin/env python3
"""The stop-whisper's dispatch rung must offer a seat only ITS OWN rows.

`seats._dispatch_candidate` documents this rung as "work YOU handed to another
seat and have not checked on". The filter that makes that true was never
written: `stop_candidate` iterated every open row, so every stopping seat was
offered the same globally-oldest row regardless of whose obligation it was.

MEASURED 2026-07-29/30, five wasted turns across four seats — three peer seats
each spent a turn establishing a row was not theirs, and this seat was offered kimi's row plus two whose sender it could
not prove.

THE EXCEPTION IS THE MAJORITY CASE, WHICH IS WHY IT IS NOT DEFENSIVE CODE.
Measured over the live open set by a peer seat:

    open rows 9 -> sender "claude" x6 (in_roster=False), kimi x2, ds4pro x1

`claude` is the bare-family floor every seat without HELM_CHAT_NAME resolved to
before 3e0fe8e, and no seat is named that. A naive `sender == me` would have
silently stranded SIX OF NINE open rows — two thirds of the backlog — which is
strictly worse than the noise it fixes, because a stranded obligation is
invisible and a noisy one is merely expensive.

Both non-ownership escapes are pinned below, because they fail in opposite
directions and each looks like the other from the outside.
"""

import time
import unittest
from unittest import mock

from helm import dispatches


def _row(rid, sender, ts="2026-07-30T00:00:00Z"):
    return {"id": rid, "sender": sender, "recipient": "someone", "lane": "l",
            "status": "open", "delivery": "observed", "ts": ts,
            "deadline_s": 2700, "kind": "build"}


def LIVE():
    """A fresh presence stamp AT CALL TIME. As module constants these were
    stamped at import — collection time in a whole-suite run — and judged
    against the wall-clock presence bar ~13 minutes later, so the suite's
    own duration turned live fixtures absent (a time bomb the lane that
    added five tests upstream of this file set off)."""
    return {"last_seen": time.time()}


def DEAD():
    return {"last_seen": time.time() - 86400}   # a tmp-claude-N style corpse


class ScopeTest(unittest.TestCase):

    def _pick(self, rows, seat, roster=None, roster_failed=False):
        snap = {r["id"]: r for r in rows}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(snap, None)), \
             mock.patch.object(dispatches, "_open", return_value=True), \
             mock.patch.object(dispatches, "_is_overdue", return_value=True):
            from helm import seats
            with mock.patch.object(seats, "roster_checked",
                                   return_value=(roster or {}, roster_failed)):
                r, kind, unavailable = dispatches.stop_candidate(seat=seat)
        return None if r is None else r["id"]

    def test_my_own_row_surfaces(self):
        self.assertEqual(
            self._pick([_row("mine", "helm-claude")], "helm-claude",
                       roster={"helm-claude": LIVE(), "kimi": LIVE()}),
            "mine")

    def test_a_LIVE_other_seats_row_is_skipped(self):
        """THE DEFECT: kimi's retire-cave-tab row offered to this seat."""
        self.assertIsNone(
            self._pick([_row("kimis", "kimi")], "helm-claude",
                       roster={"helm-claude": LIVE(), "kimi": LIVE()}))

    def test_the_collapsed_floor_sender_STILL_surfaces(self):
        """Six of nine live rows. `claude` names no seat, so scoping it away
        hides it from everyone — the strand this exception exists to prevent."""
        self.assertEqual(
            self._pick([_row("legacy", "claude")], "helm-claude",
                       roster={"helm-claude": LIVE(), "kimi": LIVE()}),
            "legacy")

    def test_a_ROSTERED_BUT_ABSENT_sender_still_surfaces(self):
        """a peer seat's find: membership is not liveness. The
        roster holds 99 dead tmp-claude-N rows; a sender naming one would be
        skipped for EVERY seat and stranded by a different door."""
        self.assertEqual(
            self._pick([_row("orphan", "tmp-claude-7")], "helm-claude",
                       roster={"helm-claude": LIVE(), "tmp-claude-7": DEAD()}),
            "orphan")

    def test_an_empty_sender_surfaces(self):
        self.assertEqual(
            self._pick([_row("nosender", "")], "helm-claude",
                       roster={"helm-claude": LIVE()}),
            "nosender")

    def test_an_UNREADABLE_roster_surfaces_everything(self):
        """Cannot look -> keep the net. An unreadable roster must never read as
        'these rows belong to other people'."""
        self.assertEqual(
            self._pick([_row("kimis", "kimi")], "helm-claude",
                       roster={}, roster_failed=True),
            "kimis")

    def test_seat_None_keeps_the_old_fleet_wide_behaviour(self):
        """Back-compat for any caller with no identity to offer — it must not
        start returning None and silently disarm the rung."""
        self.assertEqual(
            self._pick([_row("kimis", "kimi")], None,
                       roster={"kimi": LIVE()}),
            "kimis")

    def test_mine_is_chosen_over_a_skippable_older_row(self):
        """Ordering survives the filter: the oldest MINE wins, not the oldest
        overall — otherwise scoping would silently reorder the queue."""
        rows = [_row("kimis", "kimi", ts="2026-07-01T00:00:00Z"),
                _row("mine", "helm-claude", ts="2026-07-30T00:00:00Z")]
        self.assertEqual(
            self._pick(rows, "helm-claude",
                       roster={"helm-claude": LIVE(), "kimi": LIVE()}),
            "mine")


class PredicateTest(unittest.TestCase):
    """The predicate alone, so a failure names the rule rather than the rung."""

    def test_both_controls(self):
        roster = {"me": LIVE(), "other": LIVE(), "corpse": DEAD()}
        cases = [
            ("me", True, "my own row"),
            ("other", False, "a live peer's row"),
            ("ghost", True, "a sender naming no seat"),
            ("corpse", True, "a rostered but absent sender"),
            ("", True, "no sender at all"),
        ]
        for sender, want, why in cases:
            with self.subTest(sender=sender, why=why):
                self.assertEqual(
                    dispatches._mine_or_unprovable(
                        {"sender": sender}, "me", roster, False),
                    want, why)



class AccessorNotFieldTest(unittest.TestCase):
    """THE BUG MY OTHER TESTS COULD NOT CATCH, pinned so it cannot return.

    `last_seen` is a FUNCTION over the roster row, not the row's field of the
    same name. The raw field carries a join-era timestamp — it read 47 HOURS
    for a seat that was mid-build — so a predicate reading row["last_seen"]
    graded EVERY sender absent, kept every row, and the whole fix silently did
    nothing while nine unit tests stayed green.

    They stayed green because they built rows as {"last_seen": <now>}, where
    field and accessor agree. A fixture that cannot distinguish two readings is
    not a test of which one the code uses. This one makes them DISAGREE.
    """

    def test_the_ACCESSOR_wins_over_the_raw_field(self):
        from helm import seats
        # raw field: ancient. accessor: fresh. Only reading the accessor gives
        # "this peer is live, skip their row".
        roster = {"peer": {"last_seen": time.time() - 200_000, "sessions": []}}
        with mock.patch.object(seats, "last_seen", return_value=time.time()):
            keep = dispatches._mine_or_unprovable(
                {"sender": "peer"}, "me", roster, False)
        self.assertFalse(keep, "a LIVE peer's row must scope away; reading the "
                               "raw field instead of the accessor grades it "
                               "absent and keeps it, which is the silent no-op")

    def test_and_a_genuinely_absent_peer_still_keeps_its_row(self):
        """The other direction, so the fix cannot become skip-everything."""
        from helm import seats
        roster = {"peer": {"last_seen": time.time(), "sessions": []}}
        with mock.patch.object(seats, "last_seen",
                               return_value=time.time() - 200_000):
            self.assertTrue(dispatches._mine_or_unprovable(
                {"sender": "peer"}, "me", roster, False))

class FixtureOutlivesALongSuiteTest(unittest.TestCase):
    """THE BOMB ITSELF, pinned — the cure landed without an arm that fails if
    it is undone.

    `LIVE`/`DEAD` were module CONSTANTS stamped at import. `unittest discover`
    imports every module up front and then runs for as long as the whole suite
    takes, and `presence_of` calls a peer absent past QUIET_S (900s), so the
    fixture decayed into a corpse partway through the run and three tests in
    this file failed — accusing whichever lane happened to be gating.

    MEASURED 2026-08-07 across the binding ledger, 1444 suite receipts: six
    carried those failures, all on one afternoon, all at elapsed 1344-1562s,
    while receipts at 1144-1215s were clean — no overlap in either direction.
    The suite crossed its own fixture's expiry that afternoon.

    Every OTHER test here would go green again the moment the suite got
    faster, which is exactly how this hid: nothing asserted the property that
    broke, so the file could not tell a repaired fixture from a lucky clock.
    This arm can, because it moves the clock itself instead of waiting."""

    def test_LIVE_stays_fresh_after_a_suite_longer_than_QUIET_S(self):
        from helm import seats, seats_common
        stamped_at_import = LIVE()["last_seen"]    # what a CONSTANT would hold
        later = time.time() + 3 * seats_common.QUIET_S
        with mock.patch.object(time, "time", return_value=later):
            # POSITIVE CONTROL FIRST: the stamp taken before the wait MUST
            # decay, or this test cannot detect the thing it guards. Without
            # it, a LIVE() returning a hardcoded fresh value would pass.
            self.assertEqual(seats.presence_of(stamped_at_import), "absent",
                             "control: a stamp taken before a long suite must "
                             "read absent, or this arm proves nothing")
            self.assertNotEqual(
                seats.presence_of(LIVE()["last_seen"]), "absent",
                "LIVE() must be built at CALL time — a peer this suite calls "
                "live cannot be a corpse to the code under test")
            # and the corpse stays a corpse on the same clock, so the cure
            # cannot become make-everything-fresh
            self.assertEqual(seats.presence_of(DEAD()["last_seen"]), "absent")


if __name__ == "__main__":
    unittest.main()
