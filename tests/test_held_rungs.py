#!/usr/bin/env python3
"""A held rung closes as ruled, and only as ruled.

A RE-TIP LEAVES ONE RUNG BEHIND. The successor is sent with --supersedes, it
is approved and it lands, and the rung it replaced still HOLDS with no
verdict. No landed door takes a row without a verdict, so a seat has to
notice the rung and mint a late verdict by hand.

THE RULE THESE ARMS PIN. A verdict is where findings live, and a close
reason is not a verdict. So a held rung closes on its chain's landed APPROVE
(through `discharged`, which records the successor as its authority) only
when its hold says there is nothing to record: a SOURCE-CLEAN hold. An
ordinary hold, an owner-gated hold, and a row whose advisory read names a
finding all STAY, and the held listing says a verdict is owed, on a second
line, with the verbs that pay it. The sweep that closes the rung on the land
names only a rung whose tip is INSIDE the landed head, so a rebased rung is
left to its own doors.
"""
import contextlib
import io
import unittest
from unittest import mock

from helm import dispatches, landreq
from tests import test_lr_close as _close


# THIS MODULE DOES NOT READ HOST LIVENESS. Same declaration as
# test_lr_close's, made here because a module fixture runs only for the
# module that declares it: every dispatch write would otherwise walk the
# host's process table to consult a liveness no arm here asserts on.
_LIVE_SEATS_PATCH = None


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats", lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()


def _cli(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = dispatches.cmd_dispatch(args)
    return rc, out.getvalue(), err.getvalue()


class HeldRungBase(_close.CloseBase):
    """One chain per arm, built through the real doors: a rung at the side
    tip, a successor at a NEW commit on the same branch (the lane moved),
    an APPROVE on the successor, and the branch merged to trunk."""

    AUTHOR = "claude-opus-5-5"

    def rung(self, ref, lane="lane/held-rung", supersedes=None):
        row, why = dispatches.add(
            "seat-a", lane, ref=ref, repo=self.repo, kind="review",
            notify=False, new_work=supersedes is None, supersedes=supersedes,
            force=True, _reason=True)
        self.assertIsNone(why, why)
        return row

    def moved(self, text="moved"):
        """A new commit on the side branch, descending from the rung's tip."""
        self.git("checkout", "-q", "side")
        tip = self.commit(text, path="g")
        self.git("checkout", "-q", self.main)
        return tip

    def hold(self, rid, reason, **kw):
        row, why = dispatches.mark_hold(rid, reason, **kw)
        self.assertIsNone(why, why)
        self.assertEqual(row["status"], "held")
        return row

    def successor(self, held, lane="lane/held-rung"):
        """(successor, its tip): the approved next rung at a moved tip."""
        tip = self.moved()
        succ = self.rung(tip, lane=lane, supersedes=held["id"])
        out, err = self.mark_verdict(succ["id"], tip, "clean",
                                     polarity="approve")
        self.assertIsNone(err, err)
        self.assertEqual(out["polarity"], "approve")
        return succ, tip

    def staged(self, **hold):
        """(held rung, approved successor): the rung is held with `hold`,
        and the successor's tip is merged, so the rung's tip is inside it."""
        held = self.rung(self.side)
        self.hold(held["id"], hold.pop("reason", "awaiting the land gate"),
                  **hold)
        succ, _tip = self.successor(held)
        self.git("merge", "--no-edit", "-q", "side")
        return held, succ

    def land(self, succ, **kw):
        out, err = landreq.close(succ["id"], "landed", evidence="folded",
                                 live=True, **kw)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        return out

    def state(self, rid):
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        return snap[rid]

    def refusal(self, out, rid):
        """The sweep's refusal for `rid`, which must be there."""
        why = [r["why"] for r in out.get("sibling_refusals") or ()
               if r["id"] == rid]
        self.assertEqual(len(why), 1, "the sweep never reached %s: %r"
                         % (rid[:12], out.get("sibling_refusals")))
        return why[0]


class ASourceCleanRungClosesTest(HeldRungBase):
    """The ruled positive: zero findings, a source-clean hold, a landed
    successor whose head contains the rung's tip."""

    def test_the_land_closes_the_SOURCE_CLEAN_rung_it_left_behind(self):
        held, succ = self.staged(source_clean_tip=self.side)
        pre = self.state(held["id"])
        self.assertEqual((pre["status"], pre.get("close_reason")),
                         ("held", None))
        out = self.land(succ)
        closed = self.state(held["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["close_reason"], "discharged")
        # THE AUTHORITY IS THE SUCCESSOR'S, and the row says which one.
        self.assertEqual(closed["discharging_id"], succ["id"])
        self.assertEqual(closed["discharge_tier"],
                         dispatches.DISCHARGE_TIER_CHAIN)
        self.assertIn(held["id"], [p["id"] for p in out["closed_siblings"]])
        # AND THE EVENT IT WROTE REPLAYS. Replay hands the validator the row
        # BEFORE the event, against the ledger in which the successor is
        # already closed landed.
        event = self.close_event(held["id"])
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        at_event = dict(current, **{held["id"]: pre})
        self.assertIsNone(dispatches._close_event_error(
            event, pre, current=at_event, verdicts=verdicts))
        # POSITIVE CONTROL ON THE SAME CALL: the same event on the same row
        # with its hold made ordinary is refused, by name.
        ordinary = {k: v for k, v in pre.items() if k != "source_clean_tip"}
        self.assertIn("not source-clean", str(dispatches._close_event_error(
            event, ordinary, current=dict(at_event, **{held["id"]: ordinary}),
            verdicts=verdicts)))

    def test_one_verb_closes_it_when_the_land_did_not_sweep(self):
        held, succ = self.staged(source_clean_tip=self.side)
        self.land(succ, fan_out=False)
        self.assertEqual(self.state(held["id"])["status"], "held",
                         "the suppressed sweep must leave the rung held")
        out, err = landreq.close(held["id"], "discharged",
                                 evidence="rode in on the landed successor")
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "discharged")
        self.assertEqual(self.state(held["id"])["discharging_id"],
                         succ["id"])


class AHoldThatNamesFindingsStaysTest(HeldRungBase):
    """The ruled negatives. Each stays HELD, and the refusal names what is
    owed instead, so a reader is never left to guess the next verb."""

    def test_an_ORDINARY_hold_stays_and_is_owed_a_verdict(self):
        held, succ = self.staged(
            reason="Measured FIX delivered; author is curing")
        out = self.land(succ)
        why = self.refusal(out, held["id"])
        self.assertIn("not source-clean", why)
        self.assertIn("helm dispatch verdict %s" % held["id"][:12], why)
        self.assertEqual(self.state(held["id"])["status"], "held")
        # THE HAND DOOR SAYS THE SAME, because a sweep refusal and a hand
        # refusal that disagree would teach the reader two answers.
        row, err = landreq.close(held["id"], "discharged",
                                 evidence="rode in on the landed successor")
        self.assertIsNone(row)
        self.assertIn("refuses HELD %s" % held["id"][:12], err)
        self.assertIn("What is owed", err)

    def test_an_OWNER_GATED_hold_stays_and_names_the_owner(self):
        held, succ = self.staged(reason="awaiting the owner's ruling",
                                 owner_gated=True)
        out = self.land(succ)
        why = self.refusal(out, held["id"])
        self.assertIn("OWNER-GATED", why)
        self.assertIn("the OWNER owes the next move", why)
        self.assertEqual(self.state(held["id"])["status"], "held")

    def test_an_advisory_read_naming_a_finding_keeps_a_SOURCE_CLEAN_rung(self):
        held = self.rung(self.side)
        read, err = dispatches.mark_verdict(
            held["id"], self.side, "read: one path regresses", polarity="fix",
            basis="measured", bind_author=True, reviewer_model="fable",
            reviewer_run="wf-1", author_model=self.AUTHOR,
            worse_than_main_paths=["g"])
        self.assertIsNone(err, err)
        self.assertEqual(read["status"], "open")
        self.hold(held["id"], "awaiting the land gate",
                  source_clean_tip=self.side)
        reads = self.state(held["id"])["advisory_reads"]
        self.assertEqual([r["polarity"] for r in reads], ["fix"])
        succ, _tip = self.successor(held)
        self.git("merge", "--no-edit", "-q", "side")
        out = self.land(succ)
        self.assertIn("advisory read", self.refusal(out, held["id"]))
        self.assertEqual(self.state(held["id"])["status"], "held")


class OnlyARungInsideTheLandedHeadIsSweptTest(HeldRungBase):
    """Containment is the sweep's selector. An OPEN rung inside the head
    closes; a rebased rung outside it is not named at all."""

    def test_the_contained_rung_closes_and_the_rebased_one_is_not_named(self):
        # A REBASED RUNG: its tip is on a branch the landed head never
        # contains, and it is the chain's root.
        self.git("checkout", "-q", "-b", "rebased", self.a)
        elsewhere = self.commit("rebased", path="h")
        self.git("checkout", "-q", self.main)
        rebased = self.rung(elsewhere)
        contained = self.rung(self.side, supersedes=rebased["id"])
        succ, _tip = self.successor(contained)
        self.git("merge", "--no-edit", "-q", "side")
        out = self.land(succ)
        self.assertEqual(self.state(contained["id"]).get("close_reason"),
                         "discharged",
                         "an OPEN rung inside the landed head must close")
        named = [p["id"] for p in out.get("closed_siblings") or ()] \
            + [r["id"] for r in out.get("sibling_refusals") or ()]
        self.assertIn(contained["id"], named)
        self.assertNotIn(rebased["id"], named)
        self.assertEqual(self.state(rebased["id"])["status"], "open")


class TheHeldListingReadsWhatIsOwedTest(HeldRungBase):
    """The two-line read: DISCHARGED IN FACT, and a verdict is owed."""

    def line_after(self, lines, rid):
        at = next(i for i, line in enumerate(lines) if rid in line)
        return lines[at], (lines[at + 1] if at + 1 < len(lines) else "")

    def test_each_hold_reads_its_own_answer_once_its_chain_ended(self):
        chains = {}
        for name, hold in (
                ("findings", {"reason": "Measured FIX delivered"}),
                ("owner", {"reason": "awaiting the owner",
                           "owner_gated": True}),
                ("clean", {"reason": "awaiting the land gate",
                           "source_clean_tip": self.side})):
            lane = "lane/held-%s" % name
            held = self.rung(self.side, lane=lane)
            self.hold(held["id"], hold.pop("reason"), **hold)
            self.successor(held, lane=lane)
            chains[name] = held["id"]
        rc, out, err = _cli(["list", "--held", "--all-projects"])
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        row, owed = self.line_after(lines, chains["findings"])
        self.assertIn("DISCHARGED IN FACT", row)
        self.assertIn("a VERDICT is owed", row)
        self.assertNotIn("you owe nothing", row)
        self.assertTrue(owed.startswith(
            "      owed on %s: " % chains["findings"][:12]), owed)
        self.assertIn("helm dispatch release %s" % chains["findings"][:12],
                      owed)
        self.assertIn("--source-clean", owed)
        row, owed = self.line_after(lines, chains["owner"])
        self.assertIn("the OWNER's decision is owed", row)
        self.assertIn("OWNER-GATED", owed)
        row, after = self.line_after(lines, chains["clean"])
        self.assertIn("SOURCE-CLEAN, zero findings, no verdict owed", row)
        self.assertIn("helm lr close %s --reason discharged"
                      % chains["clean"][:12], row)
        self.assertFalse(after.startswith("      owed on"), after)


if __name__ == "__main__":
    unittest.main()
