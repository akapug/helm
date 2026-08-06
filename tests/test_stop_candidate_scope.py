#!/usr/bin/env python3
"""The stop-whisper's dispatch rung must offer a seat only ITS OWN rows.

`seats._dispatch_candidate` documents this rung as "work YOU handed to another
seat and have not checked on". The filter that makes that true was never
written: `stop_candidate` iterated every open row, so every stopping seat was
offered the same globally-oldest row regardless of whose obligation it was.

MEASURED live: five wasted turns across four seats — three peer seats each
spent a turn establishing a row was not theirs, and a fourth was offered a
peer's row plus two whose sender it could not prove.

THE EXCEPTION IS THE MAJORITY CASE, WHICH IS WHY IT IS NOT DEFENSIVE CODE.
Measured over the live open set:

    open rows 9 -> sender "claude" x6 (in_roster=False), kimi x2, other x1

`claude` is the bare-family floor every seat without HELM_CHAT_NAME resolved to
before seats carried explicit names, and no seat is named that. A naive
`sender == me` would have
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


LIVE = {"last_seen": time.time()}
DEAD = {"last_seen": time.time() - 86400}       # a tmp-claude-N style corpse


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
                       roster={"helm-claude": LIVE, "kimi": LIVE}),
            "mine")

    def test_a_LIVE_other_seats_row_is_skipped(self):
        """THE DEFECT: a live peer's open row offered to this seat."""
        self.assertIsNone(
            self._pick([_row("kimis", "kimi")], "helm-claude",
                       roster={"helm-claude": LIVE, "kimi": LIVE}))

    def test_the_collapsed_floor_sender_STILL_surfaces(self):
        """Six of nine live rows. `claude` names no seat, so scoping it away
        hides it from everyone — the strand this exception exists to prevent."""
        self.assertEqual(
            self._pick([_row("legacy", "claude")], "helm-claude",
                       roster={"helm-claude": LIVE, "kimi": LIVE}),
            "legacy")

    def test_a_ROSTERED_BUT_ABSENT_sender_still_surfaces(self):
        """Membership is not liveness. The
        roster held 99 dead tmp-claude-N rows; a sender naming one would be
        skipped for EVERY seat and stranded by a different door."""
        self.assertEqual(
            self._pick([_row("orphan", "tmp-claude-7")], "helm-claude",
                       roster={"helm-claude": LIVE, "tmp-claude-7": DEAD}),
            "orphan")

    def test_an_empty_sender_surfaces(self):
        self.assertEqual(
            self._pick([_row("nosender", "")], "helm-claude",
                       roster={"helm-claude": LIVE}),
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
                       roster={"kimi": LIVE}),
            "kimis")

    def test_mine_is_chosen_over_a_skippable_older_row(self):
        """Ordering survives the filter: the oldest MINE wins, not the oldest
        overall — otherwise scoping would silently reorder the queue."""
        rows = [_row("kimis", "kimi", ts="2026-07-01T00:00:00Z"),
                _row("mine", "helm-claude", ts="2026-07-30T00:00:00Z")]
        self.assertEqual(
            self._pick(rows, "helm-claude",
                       roster={"helm-claude": LIVE, "kimi": LIVE}),
            "mine")


class PredicateTest(unittest.TestCase):
    """The predicate alone, so a failure names the rule rather than the rung."""

    def test_both_controls(self):
        roster = {"me": LIVE, "other": LIVE, "corpse": DEAD}
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
    """THE BUG THE OTHER TESTS COULD NOT CATCH, pinned so it cannot return.

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

if __name__ == "__main__":
    unittest.main()
