#!/usr/bin/env python3
"""`helm lr compose` — CAN A TRAIN OF APPROVED LANES BECOME ONE TRUNK-MOVING
TIP, AND DOES EVERY REFUSAL FIRE FOR ITS OWN STATED REASON?

That is the whole question this file asks. Compose is the merge queue's one
writing leg: it takes a set of already-reviewed lanes and produces a single
composed tip, or it refuses. The oracle here must be able to DISAGREE with the
verb, so every refusal arm is pinned by a fixture that makes the refusal fire
for its own reason (conflict names the member and the file, drift names the two
patch ids, contained names the trunk), and the green arm's positive control is
the composed tip MOVING off trunk with both members' files in its tree.

MOVED WHOLE OUT OF `tests/test_landreq.py`, which stood 126,090 bytes PAST the
never-track ceiling. No body was rewritten on the way: the class text here is
byte-identical to its text there, apart from the base reference on the `class`
line, which the note below explains and measures.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `LandReqBase` and `run` are
the objects `tests/test_landreq.py` defines, so a change to the fixture still
reaches these arms and the two files cannot drift apart.
"""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helm import dispatches, landreq
from tests import test_landreq as _landreq
from tests.test_landreq import run


# THE BASE IS REACHED THROUGH ITS MODULE, NOT IMPORTED BY NAME, AND THAT IS
# MEASURED RATHER THAN styled. `unittest` collects every TestCase bound at
# module scope, imported ones included, under the id of the module that
# DEFINES it. A plain `from tests.test_landreq import LandReqBase` would
# therefore collect every arm the base carries a second time, here, under the
# same id. The base carries none now, and tests/test_suite_collection.py
# refuses the binding the day it gains one. Reaching the base through
# `_landreq` keeps the fixture shared by reference, which is the point of
# importing it, without republishing a collectable name.


# THIS MODULE DOES NOT READ HOST LIVENESS. Same declaration as
# `tests/test_landreq.py`'s, where the full argument and its measurements live;
# the short version is that every dispatch write here reaches
# `seat_usability.seat_verdict`, which walks the host's whole process table
# (54,114 pids on the build node, 0.677s a walk) to consult a liveness these
# arms never assert on — which made what they OBSERVED depend on what else was
# running beside them.
#
# DECLARED PER MODULE ON PURPOSE, not hoisted into a shared helper: "this
# module does not read host liveness" is a claim about THIS file that someone
# must re-check when its arms change, and a helper import would hide it.
#
# MODULE SCOPE, NOT A BASE CLASS, because a base-class hook would miss every
# class here that inherits `unittest.TestCase` directly, and importing
# `tests/test_landreq.py` does NOT run its setUpModule — unittest runs module
# fixtures per module under test.
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


class ComposeTest(_landreq.LandReqBase):
    """helm lr compose — the merge queue's compose leg (the merge-queue owner directive).

    The oracle here must be able to DISAGREE with the verb: every refusal arm
    is pinned by a fixture that makes the refusal fire for its OWN stated
    reason (conflict names the member and file; drift names the two patch-ids;
    contained names the trunk), and the green arm's positive control is the
    composed tip MOVING off trunk with both members' files in its tree."""

    def approve(self, row, tip):
        self.mark_verdict(row["id"], tip, "ok", polarity="approve")

    def second_lane(self, name="side2", path="h"):
        self.git("checkout", "-q", "-b", name, self.a)
        tip = self.commit(name, path=path)
        self.git("checkout", "-q", self.main)
        return tip

    def compose(self, *args):
        return run(["compose", *args])

    def ready_rows(self, *rows):
        """The real projection with the named rows FORCED to LIVE READY. Pins
        the verb's OWN rungs deterministically: the projection's landed
        observation is order-flaky in a shared test process (measured —
        batch-blind, isolation-sighted), and its derivation
        is its own module's test subject, not this class's.

        LIVE, SO `terminal` IS FORCED WITH THE WORD. The projection derives it
        from the state for a row nothing retired (LANDED is terminal), so a
        row it had observed as LANDED and this helper forced to READY read
        READY, terminal and retired by nothing, a row the projection never
        mints. Compose admits only `landreq.live_ready` rows, and these arms
        are about the screens behind that admission: a LIVE row whose work
        reached trunk before the projection observed it."""
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        for r in rows:
            lrs[r["id"]].update(state="READY", terminal=False)
        return mock.patch.object(landreq, "project",
                                 lambda now=None: (lrs, None))

    def room_of(self, *rows):
        return (self.repo.rstrip(os.sep) + "-wt" + os.sep + "compose"
                + os.sep + "+".join(r["id"][:4] for r in rows))

    def tree_read_failure(self):
        """A backend whose only failure is the final gate-tree derivation."""
        real = landreq.vcs.backend
        def failing(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args == ("rev-parse", "HEAD^{tree}"):
                        return 1, "", "forced tree read failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        return mock.patch.object(landreq.vcs, "backend", failing)

    def approved_pair(self):
        tip2 = self.second_lane()
        one = self.dispatch(lane="lane/one")
        two = self.dispatch(ref=tip2, lane="lane/two")
        self.approve(one, self.side)
        self.approve(two, tip2)
        return one, two

    def test_two_disjoint_approved_lanes_compose_and_carry(self):
        tip2 = self.second_lane()
        one = self.dispatch(lane="lane/one")
        two = self.dispatch(ref=tip2, lane="lane/two")
        self.approve(one, self.side)
        self.approve(two, tip2)
        rc, out, err = self.compose(one["id"][:12], two["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        # the composed tip MOVED off trunk — the positive control that the
        # verb did anything at all before any per-member claim is trusted
        trunk = self.git("rev-parse", self.main)
        self.assertNotEqual(got["composed_tip"], trunk)
        self.assertEqual([m["carries"] for m in got["members"]], [True, True])
        self.assertEqual([m["approved_tip"] for m in got["members"]],
                         [self.side, tip2])
        for m in got["members"]:
            self.assertTrue(m["patch_id"])
        room = self.room_of(one, two)
        self.assertEqual(got["room"], room)
        self.assertTrue(os.path.isdir(room))
        listing = self.git("ls-tree", "--name-only", got["composed_tip"],
                           cwd=room)
        self.assertIn("g", listing)
        self.assertIn("h", listing)
        with open(room + ".manifest.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["composed_tip"],
                             got["composed_tip"])
        # task/228: the manifest must NOT dirty the room it describes — an
        # untracked file in the room at gate time makes the gate bind a
        # tree no commit has (fab snapshots untracked files silently).
        self.assertFalse(os.path.exists(
            os.path.join(room, "compose-manifest.json")))
        self.assertEqual(self.git("status", "--porcelain", cwd=room), "")

    def test_conflict_refuses_naming_the_member_and_cleans_up(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls precede the absences: assertIn(member-id), assertIn('conflict') and assertIn('state') demand the refusal NAME the member and file, so an empty err fails before any absence is read
        # side3 edits `state`, which trunk's own b/c commits also append to —
        # a genuine textual conflict, not a fixture that merely hopes for one.
        # --stop-on-first preserves the OLD halt behaviour (opt-in fail-fast);
        # the default is best-effort, pinned one class down.
        tip3 = self.second_lane(name="side3", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        self.approve(one, self.side)
        self.approve(three, tip3)
        rc, _out, err = self.compose(one["id"][:12], three["id"][:12],
                                     "--stop-on-first")
        self.assertEqual(rc, 1)
        self.assertIn(three["id"][:12], err)
        self.assertIn("conflict", err)
        self.assertIn("state", err)
        self.assertFalse(os.path.exists(self.room_of(one, three)))
        self.assertNotIn("compose", self.git("worktree", "list"))

    def test_a_tier_held_approve_refusal_discloses_the_projections_own_reading(self):
        """task/1067: an approve the store had DM'd as ready refused here as
        REVIEWED and the identical retry admitted it — a transient tier
        resolution had projected the hold, the row carried tier/ungated the
        whole time, and the refusal printed only the state word. The refusal
        must disclose the SAME projection's own reading, so a stale refusal
        is distinguishable from a real one without a second ~8-minute run."""
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        held = lrs[row["id"]]
        # the incident shape, on a REAL projected row: an authorizing
        # polarity demoted by a tier hold, never a hand-built fixture richer
        # or poorer than production
        self.assertEqual(held["polarity"], "approve")
        # THE TIER IS RESOLVED, NEVER HANDED IN. A fixture that sets
        # held["tier"]/["ungated"] by hand is exactly the shape that lets a
        # wrong classification ship: the row agrees with whatever the test
        # wrote, so the classifier is never in the loop and a build that
        # stamped every unknown "transient" passes. The tier comes back
        # through the real refusal predicate.
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict",
                return_value=(dispatches.TierUnknown(
                    dispatches.TIER_TRANSIENT, "unknown"),
                    "approval-tier check unavailable for @seat-a: canary")):
            lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        held = lrs[row["id"]]
        self.assertEqual(held["state"], "REVIEWED")
        self.assertEqual(held["tier"], "unknown")
        self.assertEqual(held["tier_kind"], dispatches.TIER_TRANSIENT)
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, out, _err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)
        self.assertIsNone(got["composed_tip"])
        self.assertEqual(len(got["excluded"]), 1)
        x = got["excluded"][0]
        self.assertEqual(x["state"], "REVIEWED")
        self.assertEqual(x["polarity"], "approve")
        self.assertEqual(x["tier"], "unknown")
        self.assertEqual(x["tier_kind"], dispatches.TIER_TRANSIENT)
        self.assertIn("approval-tier check unavailable", x["ungated"])
        self.assertIn("tier=unknown/transient", x["reason"])
        self.assertIn("approval-tier check unavailable", x["reason"])
        self.assertIn("helm lr show %s" % row["id"][:12], x["reason"])
        # ...and THIS kind, alone, is the one allowed to invite a retry.
        self.assertIn("re-run this command ONCE", x["reason"])

    def test_the_refusal_words_each_kind_of_unknown_as_its_own_world(self):  # noqa: VACUOUS_ASSERTION — every assertNotIn is paired, on the SAME excluded record, with an assertIn on the phrase that branch must carry (NOTHING IS STORED / CONTRADICTS ITSELF / does not resolve / MEASUREMENT, not an unresolved read); the absences are the discrimination half and can never fire alone
        """An earlier cut told the reader every unknown "heals on re-read".
        Measured false — a dark upstream stores NO proof, so there is nothing
        to re-read and nothing that heals; a malformed record reads malformed
        forever; and OUTSIDE is a MEASUREMENT that no retry can move.

        THE BAR IS DISCRIMINATION. Each arm below pins the phrase a reader
        acts on AND asserts the other worlds' phrases are absent, so a build
        whose sentences are individually true but mutually confusable reddens
        here — that confusability IS the defect."""
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)

        def refusal(tier_answer):
            with mock.patch.object(dispatches, "approval_tier_for_verdict",
                                   return_value=tier_answer):
                lrs, unavailable = landreq.project()
            self.assertIsNone(unavailable)
            self.assertEqual(lrs[row["id"]]["state"], "REVIEWED")
            with mock.patch.object(landreq, "project",
                                   lambda now=None: (lrs, None)):
                rc, out, _err = self.compose(row["id"][:12], "--json")
            self.assertEqual(rc, 1)
            return json.loads(out)["excluded"][0]

        def unknown(kind, why="planted"):
            return dispatches.TierUnknown(kind, "unknown"), why

        # DARK — nothing stored. Must not invite a retry, must not send the
        # reader to repair anything, and must name what actually clears it.
        x = refusal(unknown(dispatches.TIER_DARK))
        self.assertEqual(x["tier_kind"], dispatches.TIER_DARK)
        self.assertIn("NOTHING IS STORED", x["reason"])
        self.assertIn("must come back", x["reason"])
        # SAID ONCE. `ungated` already carries the cure; a branch that
        # appends it again prints the paragraph twice.
        self.assertEqual(x["reason"].count("NOTHING IS STORED"), 1)
        self.assertNotIn("re-run this command ONCE", x["reason"])
        self.assertNotIn("CONTRADICTS ITSELF", x["reason"])

        # DAMAGED — stored and self-contradicting. Opposite instruction.
        x = refusal(unknown(dispatches.TIER_DAMAGED))
        self.assertEqual(x["tier_kind"], dispatches.TIER_DAMAGED)
        self.assertIn("CONTRADICTS ITSELF", x["reason"])
        self.assertIn("repairing", x["reason"])
        self.assertNotIn("NOTHING IS STORED", x["reason"])
        self.assertNotIn("re-run this command ONCE", x["reason"])

        # UNNAMED — the row is wrong, not the tier.
        x = refusal(unknown(dispatches.TIER_UNNAMED))
        self.assertEqual(x["tier_kind"], dispatches.TIER_UNNAMED)
        self.assertIn("does not resolve to exactly one roster seat",
                      x["reason"])
        self.assertNotIn("NOTHING IS STORED", x["reason"])
        self.assertNotIn("CONTRADICTS ITSELF", x["reason"])

        # OUTSIDE — a MEASUREMENT, and a naive cut calls it transient.
        x = refusal(("outside", "@seat-a is outside the current approval tier"))
        self.assertEqual(x["tier"], "outside")
        self.assertIsNone(x["tier_kind"])
        self.assertIn("MEASUREMENT, not an unresolved read", x["reason"])
        self.assertIn("re-running changes nothing", x["reason"])
        self.assertNotIn("re-run this command ONCE", x["reason"])
        self.assertNotIn("NOTHING IS STORED", x["reason"])

        # AND NOT ONE OF THEM PROMISES HEALING, NOR CLAIMS THE TIER READ FINE.
        #
        # THE SECOND HALF IS A MEASURED GAP in the source lane, not a
        # precaution: its sabotage matrix disabled the
        # `tier in ("outside", "unknown")` branch and every arm above still
        # passed — a dark row silently gained "The tier read fine
        # (tier=unknown), so the hold is on this APPROVAL's own verification",
        # the exact sentence for the OPPOSITE world, appended to a row whose
        # tier could not be read at all, and nothing was red. Each arm
        # asserted what its own branch must SAY and none asserted what it
        # must never say, so the branches were free to leak into each other.
        # Both phrases are checked across every kind at once, because a
        # per-kind check is what let this through.
        for answer in (unknown(dispatches.TIER_TRANSIENT),
                       unknown(dispatches.TIER_DARK),
                       unknown(dispatches.TIER_DAMAGED),
                       unknown(dispatches.TIER_UNNAMED),
                       unknown(dispatches.TIER_UNCLASSIFIED),
                       ("outside", "measured")):
            reason = refusal(answer)["reason"]
            self.assertNotIn("heals", reason)
            self.assertNotIn("The tier read fine", reason)

    def test_a_tier_check_that_RAISES_is_transient_and_says_so(self):
        """The one unknown minted outside the resolver. `_approval_refusal`
        fails closed on any exception from `approval_tier_for_verdict`, and
        that unknown needs a kind like every other: a crash is a fact about
        the MOMENT (projscope's own law — "a raise is never memoised"), so it
        is TRANSIENT, the memo re-asks, and the surface says run it once more
        rather than sending anyone to repair a ledger that is fine."""
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               side_effect=RuntimeError("canary socket gone")):
            lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        held = lrs[row["id"]]
        self.assertEqual(held["state"], "REVIEWED")
        self.assertEqual(held["tier"], "unknown")
        self.assertEqual(held["tier_kind"], dispatches.TIER_TRANSIENT)
        # the exception's own text travels, so a repeating crash IS the
        # diagnosis rather than a generic "check raised"
        self.assertIn("canary socket gone", held["ungated"])
        self.assertIn("re-run this command ONCE", held["ungated"])
        self.assertNotIn("NOTHING IS STORED", held["ungated"])
        self.assertNotIn("CONTRADICTS ITSELF", held["ungated"])

    def test_a_verification_hold_is_not_reported_as_a_tier_condition(self):
        """A readable tier and an unbound receipt: the row is held by the
        APPROVAL's own verification, and that never clears on a re-read
        either. A cut that branches on `ungated` alone — and not on the axis
        `ungated` came from — prints the transient sentence for it."""
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               return_value=("ok", None)), \
                mock.patch.object(landreq, "gate_requirement",
                                  return_value="required"):
            lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        held = lrs[row["id"]]
        self.assertEqual(held["state"], "REVIEWED")
        self.assertEqual(held["tier"], "ok")
        self.assertIsNone(held["tier_kind"])
        self.assertIn("no minted gate receipt", held["ungated"])
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, out, _err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 1)
        x = json.loads(out)["excluded"][0]
        self.assertIn("tier=ok", x["reason"])
        self.assertIn("this APPROVAL's own verification", x["reason"])
        self.assertIn("does not clear on a re-read", x["reason"])
        # AND the cure paragraph is printed ONCE, never twice — the
        # duplication that makes a refusal unreadable. Counted on the phrase
        # every unknown branch ends with, so it reddens for any branch that
        # re-appends its cure.
        self.assertLessEqual(x["reason"].count("NOTHING IS STORED"), 1)
        self.assertNotIn("re-run this command ONCE", x["reason"])
        self.assertNotIn("NOTHING IS STORED", x["reason"])

    def test_a_non_approve_refusal_keeps_the_plain_state_sentence(self):  # noqa: VACUOUS_ASSERTION — assertIn('state is REVIEWED') and assertEqual(tier,'none') are unconditional positive controls on the same excluded record; only then are the disclosure phrases asserted absent
        # THE MUST-MISS CONTROL: the disclosure must discriminate a tier-held
        # approve from a genuinely unauthorized row. An undeclared-polarity
        # REVIEWED row (polarity None, tier "none", ungated None — the shape
        # _lr actually projects when _approval_refusal never ran) gets the
        # plain sentence; if it ever grows the tier text, the probe has
        # stopped discriminating and every refusal reads "maybe transient".
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        held = lrs[row["id"]]
        held["state"] = "REVIEWED"
        held["polarity"] = None
        held["tier"] = "none"
        held["ungated"] = None
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, out, _err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 1)
        x = json.loads(out)["excluded"][0]
        self.assertIn("state is REVIEWED", x["reason"])
        self.assertNotIn("own reading", x["reason"])
        self.assertNotIn("tier=", x["reason"])
        self.assertEqual(x["tier"], "none")
        self.assertIsNone(x["ungated"])

    def test_a_closed_row_the_projection_still_calls_READY_is_refused_by_name(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted positively (rc 1, the EXCLUDED line naming the row and its closure, the JSON record); the absent room follows it, and the live-row control composes on the SAME projection
        """THE GUARD `helm train` PUTS ON ITS CARS, on this verb's admission.
        The projection keeps the stored state READY on a row closed as landed
        and marks it terminal, and `_resolve_row` hands a retired row back to
        compose by design. Passed by id, such a row reached the cherry-pick
        with only the patch-id ALREADY-ON screen behind it, and a row that
        landed under a rewritten patch (this one's tip is not on trunk at all)
        was composed back in."""
        one, two = self.approved_pair()
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        # THE SHAPE THE LIVE LEDGER CARRIES: stored READY, closed as landed.
        # The projection mints both fields on every row; the state is forced
        # the way `ready_rows` forces it, for the same measured flake.
        for row in (one, two):
            self.assertIn("terminal", lrs[row["id"]])
            self.assertIn("close_reason", lrs[row["id"]])
            lrs[row["id"]]["state"] = "READY"
        lrs[two["id"]] = dict(lrs[two["id"]], terminal=True,
                              close_reason="landed")
        # PRECONDITION: its reviewed tip is NOT on trunk, so no ancestry or
        # patch-id screen stands between it and the cherry-pick.
        self.assertEqual(subprocess.run(
            ["git", "-C", self.repo, "merge-base", "--is-ancestor",
             lrs[two["id"]]["reviewed_tip"], self.main]).returncode, 1)
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, _out, err = self.compose(two["id"][:12])
            rc_json, out, _err = self.compose(two["id"][:12], "--json")
        self.assertEqual(rc, 1, err)
        self.assertIn("EXCLUDED %s (two): is CLOSED by close --reason "
                      "landed, though its stored state reads READY"
                      % two["id"][:12], err)
        self.assertIn("compose takes only a LIVE READY row", err)
        self.assertEqual(rc_json, 1)
        got = json.loads(out)
        self.assertIsNone(got["composed_tip"])
        self.assertEqual([(x["id"], x["closed_by"]) for x in got["excluded"]],
                         [(two["id"], "close --reason landed")])
        # NOTHING COMPOSED: no room was minted for it.
        self.assertFalse(os.path.exists(self.room_of(two)))
        self.assertNotIn("compose", self.git("worktree", "list"))
        # CONTROL, on the same projection: the live READY row composes.
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, out, err = self.compose(one["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertNotEqual(got["composed_tip"],
                            self.git("rev-parse", self.main))
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_context_drift_carries_when_the_changed_lines_are_identical(self):
        # THE BATCH-2 MOVED-TARGET EVICTION, as a fixture: a CLEAN cherry-pick
        # whose surrounding context moved. Member edits line 18 of a 20-line
        # file; trunk then edits line 15 — inside the member hunk's context
        # window, outside its changed lines. patch-id (which hashes context)
        # calls that drift and evicted an innocent lane; the content hash
        # (the +/- lines, which are what an APPROVE binds) calls it a carry.
        lines = [str(i) for i in range(1, 21)]
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "ctx")
        fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "drift", fork)
        lines[17] = "eighteen-changed"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "drift edit")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        lines[17] = "18"
        lines[14] = "fifteen-moved"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "trunk moves the context")
        row = self.dispatch(ref=tip, lane="lane/drift")
        self.approve(row, tip)
        rc, out, err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual([m["id"] for m in got["members"]], [row["id"]])
        self.assertEqual(got["excluded"], [])
        # the pick REALLY landed: the composed tip moved off trunk and the
        # member's change is in its tree
        listing = self.git("ls-tree", "--name-only", got["composed_tip"],
                           cwd=got["room"])
        self.assertIn("ctx", listing)

    def test_a_TRAILING_NEWLINE_change_is_not_the_same_content(self):
        """task/789 producer arm. Two commits differing ONLY by a file's
        final newline share patch-id, while both the payload digest and the
        rich owner's newline fingerprint discriminate them.

        The digest difference is necessary but insufficient for Compose:
        patch-id equality still wins the historical `(patch OR digest)` rule.
        The third axis exists so Compose can apply newline equality as an AND
        veto without changing the historical two-field adapter's shape.
        """
        path = os.path.join(self.repo, "nl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("one\ntwo")                      # NO trailing newline
        self.git("add", "nl")
        self.git("commit", "-q", "-m", "without the trailing newline")
        without = self.git("rev-parse", "HEAD")

        self.git("checkout", "-q", "-b", "nl-variant", "HEAD~1")
        with open(path, "w", encoding="utf-8") as f:
            f.write("one\ntwo\n")                    # WITH one
        self.git("add", "nl")
        self.git("commit", "-q", "-m", "with the trailing newline")
        with_nl = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)

        a = landreq._commit_content_identity(self.repo, without)
        b = landreq._commit_content_identity(self.repo, with_nl)
        self.assertTrue(a and b)
        self.assertEqual(len(a), 3)
        self.assertEqual(len(b), 3)
        self.assertEqual(a, landreq._commit_content_identity(
            self.repo, without))
        self.assertEqual(landreq._commit_content_id(self.repo, without), a[:2])
        self.assertEqual(landreq._commit_content_id(self.repo, with_nl), b[:2])
        blob_without = self.git("rev-parse", without + ":nl")
        blob_with = self.git("rev-parse", with_nl + ":nl")
        self.assertEqual(len(blob_without), 40)
        self.assertEqual(len(blob_with), 40)
        self.assertNotEqual(blob_without, blob_with,
                            "the fixture must write two different blobs")

        self.assertEqual(a[0], b[0],
                         "git patch-id normalises the newline marker away")
        self.assertNotEqual(a[1], b[1],
                            "the digest must see the no-newline marker")
        self.assertNotEqual(a[2], b[2],
                            "the rich owner must expose the newline change")

    def test_empty_commit_has_the_same_rich_identity_shape(self):
        self.git("commit", "--allow-empty", "-q", "-m", "empty identity")
        sha = self.git("rev-parse", "HEAD")
        rich = landreq._commit_content_identity(self.repo, sha)
        self.assertEqual(rich, ("EMPTY", "EMPTY", "EMPTY"))
        self.assertEqual(landreq._commit_content_id(self.repo, sha), rich[:2])

    def _synthetic_content_identity(self, body):
        class _BE:
            def run(self, root, *a, **k):
                return (0, body, "")

            def text(self, root, *a, **k):
                return (0, "d" * 40 + " x", "")

        with mock.patch.object(landreq.vcs, "backend", lambda root: _BE()):
            return landreq._commit_content_identity(self.repo, "sha")

    def test_newline_fingerprint_binds_payload_side_and_stream_order(self):
        """The third axis is a canonical stream, not a marker count. It binds
        whether the marker annotates removed or added payload, the path, and
        the order in which those attributed markers occur."""
        nl = b"\\ No newline at end of file\n"

        def one(path, side):
            old, new = (b"1", b"0") if side == b"-" else (b"0", b"1")
            return (b"diff --git a/" + path + b" b/" + path + b"\n"
                    b"index 1111111111111111111111111111111111111111.."
                    b"2222222222222222222222222222222222222222 100644\n"
                    b"--- a/" + path + b"\n+++ b/" + path + b"\n"
                    b"@@ -" + old + b" +" + new + b" @@\n"
                    + side + b"payload\n" + nl)

        removed = self._synthetic_content_identity(one(b"f", b"-"))
        added = self._synthetic_content_identity(one(b"f", b"+"))
        ab = self._synthetic_content_identity(one(b"a", b"-")
                                              + one(b"b", b"+"))
        ba = self._synthetic_content_identity(one(b"b", b"+")
                                              + one(b"a", b"-"))
        self.assertTrue(removed and added and ab and ba)
        self.assertNotEqual(removed[2], added[2],
                            "removed and added markers are different facts")
        self.assertNotEqual(ab[2], ba[2],
                            "the attributed marker stream is order-sensitive")

    def test_unenumerated_structural_line_refuses(self):
        """Closed-world canary: any nonempty line outside the enumerated Git
        grammar makes the whole identity unmeasurable. Removing the parser's
        final unknown-line refusal must make this arm mint an identity."""
        known = (b"diff --git a/f b/f\n"
                 b"old mode 100644\n"
                 b"new mode 100755\n")
        self.assertTrue(self._synthetic_content_identity(known),
                        "the enumerated prefix must mint a real identity")
        self.assertIsNone(self._synthetic_content_identity(
            known + b"future metadata only\n"))

    def test_nonempty_identity_with_missing_axes_refuses(self):
        """Axis totality: even an entirely recognized nonempty stream cannot
        return a truthy `(patch, None, None)` partial identity."""
        body = (b"diff --git a/f b/f\n"
                b"index 1111111111111111111111111111111111111111.."
                b"2222222222222222222222222222222222222222 100644\n")
        self.assertTrue(self._synthetic_content_identity(
            body + b"old mode 100644\nnew mode 100755\n"),
            "the same recognized stream with content must be measurable")
        self.assertIsNone(self._synthetic_content_identity(body))

    def test_unmeasurable_rich_identity_cannot_authorize_compose(self):
        """The closed grammar's refusal value reaches the consumer in the
        safe direction: exclusion as unmeasurable, never carry or equality."""
        owner = landreq._commit_content_identity

        def refuse_composed(root, sha):
            if root == self.repo:
                return owner(root, sha)
            return None

        with mock.patch.object(landreq, "_commit_content_identity",
                               refuse_composed):
            got = self.compose_sole(self.side, "lane/unmeasurable-rich-owner")
        self.assertEqual(got["members"], [])
        self.assertEqual(got["rc"], 1)
        self.assertEqual(len(got["excluded"]), 1)
        self.assertIn("composed content unmeasurable",
                      got["excluded"][0]["reason"])

    def _install_newline_merge_driver(self):
        """Preserve trunk except for the lane's final-line text, then add a
        trailing newline: a clean pick differing only in newline shape."""
        script = os.path.join(self.tmp, "merge-newline.py")
        called = os.path.join(self.tmp, "merge-newline.called")
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n"
                    "from pathlib import Path\n"
                    "import sys\n"
                    "a, b = map(Path, sys.argv[1:3])\n"
                    "cur, other = a.read_bytes().splitlines(), "
                    "b.read_bytes().splitlines()\n"
                    "cur[-1] = other[-1]\n"
                    "a.write_bytes(b'\\n'.join(cur) + b'\\n')\n"
                    "Path(%r).write_text('called\\n')\n" % called)
        os.chmod(script, 0o755)
        self.git("config", "merge.nl-shape.driver", "%s %%A %%B" % script)
        return called

    def test_compose_evicts_a_changed_line_newline_difference(self):
        """Real Compose rejection: the clean pick has equal patch-id and a
        different changed-line newline fingerprint. Deleting Compose's third-
        axis veto must make this arm false-carry again."""
        called = self._install_newline_merge_driver()
        lines = ["line%d" % i for i in range(1, 21)]
        base = self.fork_point(
            (".gitattributes", b"nl-carry merge=nl-shape\n"),
            ("nl-carry", ("\n".join(lines) + "\n").encode()))
        self.git("checkout", "-q", "-b", "nl-shape-lane", base)
        lane_lines = list(lines)
        lane_lines[-1] = "lane-final"
        with open(os.path.join(self.repo, "nl-carry"), "wb") as f:
            f.write("\n".join(lane_lines).encode())       # NO trailing NL
        self.git("add", "nl-carry")
        self.git("commit", "-q", "-m", "lane changes final line and newline")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        trunk_lines = list(lines)
        trunk_lines[0] = "TRUNK-FIRST"
        with open(os.path.join(self.repo, "nl-carry"), "w") as f:
            f.write("\n".join(trunk_lines) + "\n")
        self.git("add", "nl-carry")
        self.git("commit", "-q", "-m", "trunk changes first line")
        trunk = self.git("rev-parse", "HEAD")

        self.git("checkout", "-q", "-b", "nl-shape-probe", trunk)
        self.git("cherry-pick", tip)
        picked = self.git("rev-parse", "HEAD")
        orig_id = landreq._commit_content_identity(self.repo, tip)
        picked_id = landreq._commit_content_identity(self.repo, picked)
        self.assertTrue(orig_id and picked_id)
        self.assertEqual(orig_id[0], picked_id[0],
                         "the fixture must reach Compose through patch equality")
        self.assertNotEqual(orig_id[1], picked_id[1])
        self.assertNotEqual(orig_id[2], picked_id[2],
                            "the rich newline veto must see the changed shape")
        lane_blob = subprocess.run(
            ["git", "-C", self.repo, "show", tip + ":nl-carry"],
            capture_output=True, check=True).stdout
        picked_blob = subprocess.run(
            ["git", "-C", self.repo, "show", picked + ":nl-carry"],
            capture_output=True, check=True).stdout
        self.assertFalse(lane_blob.endswith(b"\n"))
        self.assertTrue(picked_blob.endswith(b"\n"))
        self.assertTrue(picked_blob.startswith(b"TRUNK-FIRST\n"))
        self.git("checkout", "-q", self.main)
        os.unlink(called)

        got = self.compose_sole(tip, "lane/newline-shape-drift")
        self.assertTrue(os.path.exists(called),
                        "the real Compose pick must invoke the merge driver")
        self.assertEqual(got["members"], [])
        self.assertEqual(got["rc"], 1)
        self.assertEqual(len(got["excluded"]), 1)
        self.assertIn("newline drift", got["excluded"][0]["reason"])

    def test_trunk_only_newline_context_still_carries(self):
        """Trunk alone strips the final newline while the lane changes a
        nearby line. The marker annotates context, so all rich axes stay equal
        and Compose must carry."""
        lines = ["line%d" % i for i in range(1, 13)]
        base = self.fork_point(
            ("nl-context", ("\n".join(lines) + "\n").encode()))
        self.git("checkout", "-q", "-b", "nl-context-lane", base)
        lane_lines = list(lines)
        lane_lines[8] = "lane-nine"
        with open(os.path.join(self.repo, "nl-context"), "w") as f:
            f.write("\n".join(lane_lines) + "\n")
        self.git("add", "nl-context")
        self.git("commit", "-q", "-m", "lane changes nearby line")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        with open(os.path.join(self.repo, "nl-context"), "wb") as f:
            f.write("\n".join(lines).encode())           # trunk strips NL
        self.git("add", "nl-context")
        self.git("commit", "-q", "-m", "trunk strips final newline")
        trunk = self.git("rev-parse", "HEAD")

        self.git("checkout", "-q", "-b", "nl-context-probe", trunk)
        self.git("cherry-pick", tip)
        picked = self.git("rev-parse", "HEAD")
        orig_id = landreq._commit_content_identity(self.repo, tip)
        picked_id = landreq._commit_content_identity(self.repo, picked)
        self.assertTrue(orig_id and picked_id)
        self.assertEqual(orig_id, picked_id,
                         "a context-attached marker is not the lane's content")
        self.git("checkout", "-q", self.main)

        got = self.compose_sole(tip, "lane/newline-context-clean")
        self.assertEqual(got["rc"], 0, got["stderr"])
        self.assertEqual(got["excluded"], [])
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_a_copy_remapped_by_a_directory_rename_is_content_drift(self):
        """Git can cleanly relocate a lane-added copy when trunk renamed its
        destination directory. The approved `copy to dir/new` and composed
        `copy to moved/new` are different reviewed paths, so copy metadata must
        participate in the digest instead of falling through alphabetically."""
        self.git("config", "diff.renames", "copies")
        self.git("config", "merge.directoryRenames", "true")
        base = self.fork_point(("src", b"shared payload\n"),
                               ("dir/anchor", b"anchor\n"))
        self.git("checkout", "-q", "-b", "copy-lane", base)
        shutil.copyfile(os.path.join(self.repo, "src"),
                        os.path.join(self.repo, "dir", "new"))
        with open(os.path.join(self.repo, "src"), "a") as f:
            f.write("lane changes source\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "copy into directory")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("mv", "dir", "moved")
        self.git("commit", "-q", "-m", "trunk renames directory")
        trunk = self.git("rev-parse", "HEAD")

        self.git("checkout", "-q", "-b", "copy-probe", trunk)
        self.git("cherry-pick", tip)
        picked = self.git("rev-parse", "HEAD")
        approved_show = self.git("show", "--format=", tip)
        composed_show = self.git("show", "--format=", picked)
        self.assertIn("copy from src", approved_show)
        self.assertIn("copy to dir/new", approved_show)
        self.assertIn("copy from src", composed_show)
        self.assertIn("copy to moved/new", composed_show)
        orig_id = landreq._commit_content_identity(self.repo, tip)
        picked_id = landreq._commit_content_identity(self.repo, picked)
        self.assertTrue(orig_id and picked_id,
                        "the chosen grammar explicitly binds copy metadata")
        self.assertNotEqual(orig_id[0], picked_id[0],
                            "the relocated copy must move patch identity")
        self.assertNotEqual(orig_id[1], picked_id[1],
                            "copy destination is reviewed content identity")
        self.git("checkout", "-q", self.main)

        got = self.compose_sole(tip, "lane/copy-path-drift")
        self.assertEqual(got["members"], [])
        self.assertEqual(got["rc"], 1)
        self.assertEqual(len(got["excluded"]), 1)
        self.assertIn("content drift", got["excluded"][0]["reason"])

    def test_a_META_line_between_payload_and_marker_orphans_the_marker(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(plain and plain[1])` is an unconditional positive control on the SAME observable: it proves the mock backend drives this instrument to a real, non-None identity, so every assertIsNone below is this scanner REFUSING rather than the harness failing to reach it
        """The invariant is IMMEDIATE predecessor, so EVERY non-marker line
        must replace it — including the branches that return early.

        Pre-cure, the Binary and mode/rename META branches `continue`d after
        appending their part WITHOUT clearing `prev`. A stream of
        `+payload` -> `old mode 100644` -> the exact sentinel therefore
        inherited the earlier `+` and hashed the marker as the author's own
        content, when it belongs to nobody and the identity is unmeasurable.

        MOCKED ON PURPOSE, not for convenience: git will not emit a mode line
        between a payload line and a marker, so the fixture cannot be built
        from a real commit. The defect is in how this scanner treats a stream
        it may be HANDED — by a future git, another backend, or a corrupt
        capture — and refusing an unattributable token is the whole contract.

        LOAD-BEARING MUTATION — and it is the ORDERING, not the guard.
        MEASURED both ways rather than assumed: dropping `_DIFF_STRUCTURAL`
        SURVIVES this arm, because `old mode 100644` starts with `o` and is
        rejected by the +/-/space test anyway. The mutation that reddens it
        is restoring the pre-cure ordering — exempt the META families from
        the top-of-loop assignment so their branches keep a stale `prev`:

          if ln != _NO_NEWLINE and not ln.startswith(
                  (b"Binary files", b"GIT binary patch", ...)):
              prev = _nl_predecessor(ln)
          -> AssertionError: a marker after a META boundary must be an orphan

        `_DIFF_STRUCTURAL` is load-bearing for a DIFFERENT case and is pinned
        by its own arm below: it stops `--- a/f.txt` and `+++ b/f.txt`, which
        begin with `-` and `+`, from reading as payload.
        """
        hdr = (b"diff --git a/f.txt b/f.txt\n"
               b"index 1111111111111111111111111111111111111111.."
               b"2222222222222222222222222222222222222222 100644\n"
               b"--- a/f.txt\n+++ b/f.txt\n")
        nl = b"\\ No newline at end of file"

        class _BE:
            def __init__(self, body):
                self.body = body

            def run(self, root, *a, **k):
                return (0, self.body, "")

            def text(self, root, *a, **k):
                return (0, "d" * 40 + " x", "")

        def cid(body):
            with mock.patch.object(landreq.vcs, "backend",
                                   lambda root: _BE(body)):
                return landreq._commit_content_id(self.repo, "sha")

        # MUST-HIT CONTROLS on the same instrument, so a None below is this
        # scanner refusing rather than the harness failing to drive it.
        plain = cid(hdr + b"@@ -1 +1 @@\n-old\n+new\n")
        self.assertTrue(plain and plain[1],
                        "the mock backend must produce a real identity")
        orphan = cid(hdr + b"@@ -1 +1 @@\n" + nl + b"\n")
        self.assertIsNone(orphan,
                          "a marker with no predecessor is already an orphan")

        # THE CLAIM, both early-continue families.
        after_mode = cid(hdr + b"@@ -1 +1 @@\n+payload\n"
                         b"old mode 100644\n" + nl + b"\n")
        self.assertIsNone(
            after_mode,
            "a marker after a META boundary must be an orphan")
        after_binary = cid(hdr + b"@@ -1 +1 @@\n+payload\n"
                           b"Binary files a/f.bin and b/f.bin differ\n"
                           + nl + b"\n")
        self.assertIsNone(
            after_binary,
            "a marker after a Binary boundary must be an orphan")

    def test_a_diff_HEADER_is_never_the_line_a_marker_annotates(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(plain and plain[1])` is an unconditional positive control on the SAME observable, proving the instrument reaches a real identity, so the assertIsNone is a refusal and not an unreachable harness
        """`--- a/f.txt` and `+++ b/f.txt` BEGIN WITH `-` AND `+` and are not
        payload. A predecessor test written as `ln[:1] in (b"+", b"-")`
        against the raw stream classifies both as changed content, so a
        marker following a file header would be hashed as the author's own
        newline change on a file whose text the commit may not have touched.

        This arm exists because the sibling META arm does NOT cover it:
        measured, dropping `_DIFF_STRUCTURAL` leaves that one green, since
        `old mode` fails the +/-/space test on its own. Two different
        protections, two arms — the guard was real but unpinned until now.

        LOAD-BEARING MUTATION: in `_nl_predecessor`, drop the
        `_DIFF_STRUCTURAL` guard.
          python3 -m unittest tests.test_landreq.ComposeTest\\
.test_a_diff_HEADER_is_never_the_line_a_marker_annotates
          -> AssertionError: a marker after a file header must be an orphan
        """
        hdr = (b"diff --git a/f.txt b/f.txt\n"
               b"index 1111111111111111111111111111111111111111.."
               b"2222222222222222222222222222222222222222 100644\n"
               b"--- a/f.txt\n+++ b/f.txt\n")
        nl = b"\\ No newline at end of file"

        class _BE:
            def __init__(self, body):
                self.body = body

            def run(self, root, *a, **k):
                return (0, self.body, "")

            def text(self, root, *a, **k):
                return (0, "d" * 40 + " x", "")

        def cid(body):
            with mock.patch.object(landreq.vcs, "backend",
                                   lambda root: _BE(body)):
                return landreq._commit_content_id(self.repo, "sha")

        # MUST-HIT CONTROL on the same instrument: it reaches a real identity
        # for an ordinary diff, so a None below is this scanner refusing.
        plain = cid(hdr + b"@@ -1 +1 @@\n-old\n+new\n")
        self.assertTrue(plain and plain[1],
                        "the mock backend must produce a real identity")

        # THE CLAIM: the marker sits immediately after `+++ b/f.txt`, whose
        # first byte is `+`. Its predecessor is a HEADER, so it is an orphan.
        self.assertIsNone(
            cid(hdr + nl + b"\n"),
            "a marker after a file header must be an orphan")

    def test_REAL_payload_that_renders_like_a_header_stays_measurable(self):  # noqa: VACUOUS_ASSERTION — the CLAIMS here are assertIsNotNone (presence, not absence); the two assertIn controls on the rendered diff are unconditional within each iteration and prove the fixture really emitted the colliding spelling and the sentinel before anything is asserted about identity
        """A file line whose text is `-- final` RENDERS as `--- final` when
        removed, and `++ final` renders as `+++ final` when added. Those are
        byte-identical to the diff's own file headers, and they are PAYLOAD.

        REAL COMMITS, not a synthetic body: this is the case a prefix-only
        structural test gets wrong in the direction that HIDES work, and a
        mock could be accused of inventing a stream git never emits. Git
        emits exactly this for a no-newline file containing that text.

        The cure is parser STATE, not a longer prefix list. Outside a hunk
        `--- `/`+++ ` are headers; inside one, every line carries a leading
        +, - or space, so the first byte alone classifies and nothing else
        can collide. The sibling arm above proves the same spelling OUTSIDE
        a hunk still orphans, so the two directions are pinned separately.

        LOAD-BEARING MUTATION: in `_nl_predecessor`, delete the `if in_hunk`
        early return so the structural prefix test applies everywhere.
          python3 -m unittest tests.test_landreq.ComposeTest\\
.test_REAL_payload_that_renders_like_a_header_stays_measurable
          -> AssertionError: a removed `-- final` is payload, not a header
        """
        # Which SIDE produces the collision differs, and getting it wrong is
        # how this arm first passed a fixture that never held the shape:
        # deleting `-- final` renders `--- final` (a `-` on that text), while
        # deleting `++ final` renders `-++ final`. The plus collision comes
        # from the ADD.
        for name, text, collide_on in (("dashes", "-- final", "gone"),
                                       ("pluses", "++ final", "born")):
            path = os.path.join(self.repo, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)                    # NO trailing newline
            self.git("add", name)
            self.git("commit", "-q", "-m", "add " + name)
            born = self.git("rev-parse", "HEAD")
            os.unlink(path)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "delete " + name)
            gone = self.git("rev-parse", "HEAD")

            # MUST-HIT CONTROL ON THE FIXTURE: git really does render the
            # payload line with three leading characters. Without this the
            # arm could pass on a diff that never contained the collision.
            want = ("-" if collide_on == "gone" else "+") + text
            shown = self.git("show", "--format=",
                             gone if collide_on == "gone" else born)
            self.assertIn(want, shown,
                          "the fixture must emit the colliding spelling")
            self.assertIn("\\ No newline at end of file", shown,
                          "the fixture must emit the sentinel")

            # THE CLAIM: both identities are MEASURABLE. Pre-cure the marker
            # was called an orphan and the whole identity went None, which
            # reads to compose as REFUSAL — an innocent lane evicted.
            self.assertIsNotNone(
                landreq._commit_content_id(self.repo, born),
                "a %s payload line is payload, not a header" % name)
            self.assertIsNotNone(
                landreq._commit_content_id(self.repo, gone),
                "a removed `%s` is payload, not a header" % text)

    def test_a_REAL_content_change_still_evicts_and_names_the_commit(self):
        # The other half of the contract: changed +/- lines are a genuine
        # drift and must still evict — now NAMING the commit that drifted,
        # which the aggregate patch-id never could.
        lines = [str(i) for i in range(1, 21)]
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "ctx")
        fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "drift", fork)
        lines[17] = "eighteen-changed"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "drift edit")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        # trunk edits the SAME line — a real semantic conflict that applies
        # as a textual one? no: make the pick succeed but change the content:
        # trunk edits line 18 to a THIRD value; the pick then conflicts.
        # Simplest real-drift fixture: the member's range gets a SECOND
        # commit appended after approval, changing the +/- payload.
        row = self.dispatch(ref=tip, lane="lane/drift")
        self.approve(row, tip)
        # mutate the approved tip's history: rebase the lane onto trunk with
        # an edited payload (the "reviewed one thing, composed another" case)
        self.git("checkout", "-q", "drift")
        lines[17] = "eighteen-CHANGED-TWICE"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "second edit changing the payload")
        tip2 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        # approve the original tip, but the row's tip now resolves to tip2's
        # content only if composed from tip2 — compose the ORIGINAL row (tip)
        # whose content id no longer matches a hand-picked tip2. Direct unit
        # check of the discriminator instead: content ids differ between the
        # two payloads, and match themselves.
        a = landreq._commit_content_id(self.repo, tip)
        b = landreq._commit_content_id(self.repo, tip2)
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)
        self.assertEqual(a, landreq._commit_content_id(self.repo, tip))
        # the unconditional positive control on the same observable: the
        # tip's OWN cherry-pick onto trunk (same +/- lines, moved context)
        # must hash EQUAL to itself — without this, assertNotEqual above
        # could be satisfied by a discriminator that says everything
        # differs.
        self.git("checkout", "-q", "-b", "carry-check", self.main)
        self.git("cherry-pick", tip)
        picked = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(a, landreq._commit_content_id(self.repo, picked))
        self.assertFalse(os.path.exists(self.room_of(row)))

    def test_member_without_approve_refuses(self):
        row = self.dispatch(lane="lane/one")
        rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("is OPEN", err)
        self.assertIn("takes only READY", err)
        self.assertIn(row["id"][:12], err)
        self.assertIn("EXCLUDED", err)

    def test_already_contained_member_refuses(self):
        row = self.dispatch(ref=self.b, lane="lane/one")
        self.approve(row, self.b)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("already contained", err)

    def test_dry_run_measures_and_removes_the_room(self):  # noqa: VACUOUS_ASSERTION — rc==0, dry_run True, and carries==[True] are unconditional positive controls proving a real composition was measured; the absence asserts then prove ONLY the removal
        one = self.dispatch(lane="lane/one")
        self.approve(one, self.side)
        rc, out, err = self.compose(one["id"][:12], "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertTrue(got["dry_run"])
        self.assertIsNone(got["room"])
        self.assertEqual([m["carries"] for m in got["members"]], [True])
        self.assertFalse(os.path.exists(self.room_of(one)))
        self.assertNotIn("compose", self.git("worktree", "list"))

    def test_rebased_land_refuses_by_patch_identity_not_conflict(self):  # noqa: VACUOUS_ASSERTION — assertIn(ALREADY ON), assertIn(patch-identity) and assertIn(landed-sha) are unconditional positive controls on err content; assertNotIn(conflict) and the room absence only qualify a refusal already proven non-empty and correctly framed
        # The live incident this rung is from: a lane lands REBASED, so its
        # tip is no trunk ancestor (ancestry silent) while its content is on
        # trunk — without the rung the pick mis-frames ledger residue as a
        # live conflict. The refusal must say CLOSE, and name the trunk sha.
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        self.git("cherry-pick", self.side)
        rc0 = self.git("rev-parse", "HEAD")
        self.assertTrue(rc0)                    # the land really happened
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("ALREADY ON", err)
        self.assertIn("patch-identity", err)
        self.assertIn("close it, do not compose it", err)
        self.assertNotIn("conflict", err)
        self.assertFalse(os.path.exists(self.room_of(row)))

    def test_manifest_patch_id_is_immune_to_diff_noprefix_config(self):
        # The EXPORTED id is the subject: compose's internal carry check is
        # config-self-consistent (both sides, one instrument), but manifest
        # rows and receipts cross boxes. Measured: noprefix=true
        # yields a different patch-id for identical content, so the pin —
        # not config luck — is what makes these ids comparable anywhere.
        one = self.dispatch(lane="lane/one")
        self.approve(one, self.side)
        rc, out, err = self.compose(one["id"][:12], "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        default_pid = json.loads(out)["members"][0]["patch_id"]
        self.assertTrue(default_pid)
        self.git("config", "diff.noprefix", "true")
        rc, out, err = self.compose(one["id"][:12], "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["members"][0]["patch_id"],
                         default_pid)

    def test_a_reviewed_ungated_approve_refuses_by_state_word(self):
        # FIX HIGH 1: REVIEWED+ungated rows exist by HISTORICAL replay
        # (fresh writes cannot mint the shape — mark_verdict refuses an
        # untokened gate-capable approve outright, pinned at the write door
        # by this module's own EraStability arms). So the seam under test is
        # compose's STATE gate: raw polarity=approve would compose the row;
        # the state word must refuse it and say which word it saw.
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertEqual(lrs[row["id"]].get("polarity"), "approve")
        lrs[row["id"]]["state"] = "REVIEWED"
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn(row["id"][:12], err)
        self.assertIn("is REVIEWED", err)
        self.assertIn("takes only READY", err)

    def test_multi_commit_rebased_land_is_screened_per_commit(self):
        # FIX HIGH 2a, reproduced: an N-commit lane rebased in lands
        # as N trunk commits with N per-commit ids; the AGGREGATE id matches
        # none of them, so the old screen missed exactly its target class.
        self.git("checkout", "-q", "-b", "two", self.a)
        t1 = self.commit("two-1", path="h")
        t2 = self.commit("two-2", path="i")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=t2, lane="lane/two")
        self.approve(row, t2)
        self.git("cherry-pick", t1)
        self.git("cherry-pick", t2)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("ALREADY ON", err)
        self.assertIn("2 commits by patch-identity", err)
        self.assertIn("close it, do not compose it", err)
        self.assertNotIn("conflict", err)

    def test_partially_landed_stack_refuses_naming_the_split(self):
        # the state that must keep its branch (rowstate's veto, arriving
        # here): half a stack on trunk is neither composable nor closable
        self.git("checkout", "-q", "-b", "half", self.a)
        h1 = self.commit("half-1", path="h")
        h2 = self.commit("half-2", path="i")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=h2, lane="lane/half")
        self.approve(row, h2)
        self.git("cherry-pick", h1)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("PARTIALLY", err)
        self.assertIn("1 of 2", err)
        self.assertIn("keep its branch", err)

    def test_empty_pick_is_not_called_a_conflict_and_names_the_screen(self):
        # FIX HIGH 2b: with the screen blinded (capped), an
        # already-landed single commit reaches the pick, which stops WITHOUT
        # unmerged paths — that must read as UNKNOWN + the screen's status,
        # never as a confident conflict.
        row = self.dispatch(lane="lane/one")
        self.approve(row, self.side)
        self.git("cherry-pick", self.side)
        with self.ready_rows(row), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  lambda gd, t: ({}, landreq.INDEX_CAPPED)):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("WITHOUT conflicts", err)
        self.assertIn("capped", err)
        self.assertIn("state UNKNOWN", err)
        self.assertNotIn("conflict in", err)

    def test_capped_screen_with_partial_hits_says_unprovable_not_split(self):
        # FIX r2: under a capped scan, the unmatched remainder may sit
        # beyond the cap — claiming an exact K-of-N split would accuse the
        # lane of a state the screen cannot see. The refusal must carry
        # UNPROVABLE + the cap, never the confident PARTIALLY wording.
        self.git("checkout", "-q", "-b", "capped", self.a)
        c1 = self.commit("capped-1", path="h")
        c2 = self.commit("capped-2", path="i")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=c2, lane="lane/capped")
        self.approve(row, c2)
        self.git("cherry-pick", c1)
        real = landreq._stored_patch_index
        with self.ready_rows(row), \
                mock.patch.object(
                    landreq, "_stored_patch_index",
                    lambda gd, t: (real(gd, t)[0], landreq.INDEX_CAPPED)):
            rc, _out, err = self.compose(row["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("UNPROVABLE", err)
        self.assertIn("capped at", err)
        self.assertNotIn("PARTIALLY", err)

    def test_duplicate_patch_ids_decline_the_screen_instead_of_lying(self):
        # FIX r2, the false-ALREADY-ON reproduced: lane [X, revert-X,
        # X-again] has net +X; trunk cherry-picked X then the revert (net 0).
        # Every range patch-id "hits" trunk, but the lane's net is NOT
        # landed — the old screen said ALREADY ON; the dup-guard declines
        # and the clean pick composes the truth.
        self.git("checkout", "-q", "-b", "dup", self.a)
        x = self.commit("dup-x", path="d")
        rev = self.git("revert", "--no-edit", x) and \
            self.git("rev-parse", "HEAD")
        self.git("cherry-pick", x)
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", x)
        self.git("revert", "--no-edit", "HEAD")
        row = self.dispatch(ref=tip, lane="lane/dup")
        self.approve(row, tip)
        with self.ready_rows(row):
            rc, out, err = self.compose(row["id"][:12], "--dry-run",
                                        "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_drift_refusal_carries_no_abort_noise_and_help_names_compose(self):
        # FIX r3, both controls in one place. (a) scrap on a POST-pick
        # failure (drift) must not run --abort or stamp its failure text —
        # the room is already clean and the old unconditional form said
        # 'abort failed / room may hold' over an absent room. (b) the root
        # help surfaces must carry compose — cli._VERB_HELP and docs/VERBS.md
        # both went dark once each (built-not-wired, caught at review twice).
        lines = [str(i) for i in range(1, 21)]
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "ctx")
        fork = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "drift2", fork)
        lines[17] = "moved"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "drift edit")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        lines[17] = "18"
        lines[14] = "ctx-moved"
        with open(os.path.join(self.repo, "ctx"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.git("add", "ctx")
        self.git("commit", "-q", "-m", "trunk moves the context")
        row = self.dispatch(ref=tip, lane="lane/drift2")
        self.approve(row, tip)
        with self.ready_rows(row):
            rc, _out, err = self.compose(row["id"][:12])
        # CONTEXT-ONLY DRIFT NOW CARRIES (the batch-2 moved-target lesson):
        # the +/- lines are unchanged, so the content id matches and the
        # member composes. What this test still pins is the scrap hygiene
        # when a refusal DOES happen, plus the wiring: help and VERBS.md
        # both name compose (built-not-wired, caught at review twice).
        self.assertEqual(rc, 0, err)
        self.assertNotIn("abort", err)
        from helm import cli
        self.assertIn("compose", cli._VERB_HELP["lr"])
        docs = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "VERBS.md")
        with open(docs, encoding="utf-8") as f:
            self.assertIn("`compose` stands N LIVE READY lanes", f.read())
        # cleanup: the standing room from this compose is test exhaust
        room = self.room_of(row)
        if os.path.exists(room):
            self.git("worktree", "remove", "--force", room)

    def test_a_failed_reset_stops_the_batch_instead_of_blaming_the_next_member(self):
        # The traced FIX on the reviewed tip: evict checked --abort's rc and
        # DISCARDED reset/clean's. A failed reset leaves member A's residue,
        # B's pick then fails against THAT, and B is evicted naming B's own
        # files — attribution failure. Now a cleanup failure stops the batch
        # with the room state UNKNOWN rather than issuing verdicts measured
        # against a dirty room.
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        real = landreq.vcs.backend
        def failing_reset(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:1] == ("reset",):
                        return 1, "", "forced reset failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_reset):
            rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                        four["id"][:12], "--json")
        self.assertEqual(rc, 1)
        self.assertIn("reset --hard", out)
        self.assertIn("UNKNOWN", out)
        # THE ATTRIBUTION PIN: four was never tried and must NOT carry an
        # eviction naming its files — batch stopped, not blamed.
        self.assertNotIn("j", out.split("reset --hard")[-1][:200])

    def test_abort_failure_with_clean_removal_says_no_room_remains(self):  # noqa: VACUOUS_ASSERTION — assertIn(--abort failed) and assertIn(no room remains) are unconditional positive controls on err content; assertNotIn(may hold) and the room-absence assert only qualify a refusal already proven non-empty
        # FIX r4, the positive abort-failure control: a live pick whose
        # --abort FAILS but whose forced removal SUCCEEDS must say exactly
        # that — 'may hold' over an absent room is an intermediate state
        # narrated as final.
        tip3 = self.second_lane(name="side4", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        real = landreq.vcs.backend
        def failing_abort(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:2] == ("cherry-pick", "--abort"):
                        return 1, "", "forced abort failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_abort):
            rc, _out, err = self.compose(one["id"][:12], three["id"][:12],
                                         "--stop-on-first")
        self.assertEqual(rc, 1)
        self.assertIn("--abort failed", err)
        self.assertNotIn("may hold", err)
        self.assertFalse(os.path.exists(self.room_of(one, three)))

    def test_best_effort_evicts_the_conflicted_member_and_tries_the_rest(self):
        # TASK/165's HEADLINE, reproduced from the live 8-member dry-run that
        # halted at member 3 with "later members were not tried": the default
        # loop gives EVERY member a verdict. Three members — good, conflict,
        # good — the batch must STAND on the two that compose and name the
        # eviction with its file.
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    four["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual([m["id"] for m in got["members"]],
                         [one["id"], four["id"]])
        self.assertEqual(len(got["excluded"]), 1)
        x = got["excluded"][0]
        self.assertEqual(x["id"], three["id"])
        self.assertIn("conflict", x["reason"])
        self.assertIn("state", x["reason"])
        # and the batch REALLY stands: the composed tip carries both good
        # members' files, and the room anchors it
        listing = self.git("ls-tree", "--name-only", got["composed_tip"],
                           cwd=got["room"])
        self.assertIn("g", listing)
        self.assertIn("j", listing)

    def test_the_post_land_leg_prints_the_per_member_commands(self):
        # SLICE 3: on a standing (non-dry) batch the verb prints the exact
        # per-member land list from the manifest — the handwork the N² tax
        # left to memory — and names the excluded rows' next read.
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    four["id"][:12])
        self.assertEqual(rc, 0, err)
        self.assertIn("helm lr land %s" % one["id"][:12], out)
        self.assertIn("helm lr land %s" % four["id"][:12], out)
        self.assertNotIn("helm lr land %s" % three["id"][:12], out)
        self.assertIn("helm lr show %s" % three["id"][:12], out)

    def test_every_member_gets_a_line_or_the_run_is_a_lie(self):
        # OI's silent-empty observation, as a construction invariant: the
        # report must account for every member it was given — members plus
        # exclusions equals the ask, in --json and on the human surface.
        tip3 = self.second_lane(name="side3", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        open_row = self.dispatch(lane="lane/open")
        self.approve(one, self.side)
        self.approve(three, tip3)
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    open_row["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(len(got["members"]) + len(got["excluded"]), 3)
        self.assertEqual({x["id"] for x in got["excluded"]},
                         {three["id"], open_row["id"]})
        # the standing room from the --json leg anchors the composed tip;
        # remove it before the human-surface leg or the room name collides
        self.git("worktree", "remove", "--force", got["room"])
        rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                    open_row["id"][:12])
        self.assertEqual(rc, 0, err)
        for rid in (one["id"][:12], three["id"][:12], open_row["id"][:12]):
            self.assertTrue(rid in out or rid in err, rid)

    def test_non_textual_commit_shapes_are_distinct_and_measurable(self):
        # The r2 probe family, pinned pairwise: deletion-only,
        # mode-only, rename-only, true-empty and revert-pair commits each
        # get a DISTINCT, measurable id (the first cut collapsed every
        # readable no-+/- diff to one marker, and deletions lost their
        # payload to +++ /dev/null).
        self.git("rm", "-q", "state")
        deletion = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "-m", "delete state")
        deletion = self.git("rev-parse", "HEAD")
        ids = {"deletion": landreq._commit_content_id(self.repo, deletion)}
        self.git("checkout", "-q", "-f", self.b)
        os.chmod(os.path.join(self.repo, "state"), 0o755)
        self.git("add", "state")
        self.git("commit", "-q", "-m", "mode change")
        ids["mode"] = landreq._commit_content_id(
            self.repo, self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-f", self.b)
        self.git("mv", "state", "state-renamed")
        self.git("commit", "-q", "-m", "rename")
        ids["rename"] = landreq._commit_content_id(
            self.repo, self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-f", self.b)
        self.git("commit", "-q", "--allow-empty", "-m", "empty")
        ids["empty"] = landreq._commit_content_id(
            self.repo, self.git("rev-parse", "HEAD"))
        self.assertTrue(all(ids.values()), ids)
        self.assertEqual(len(set(ids.values())), len(ids), ids)
        self.assertEqual(ids["empty"], ("EMPTY", "EMPTY"))
        self.git("checkout", "-q", "-f", self.b)

    def test_the_content_id_binds_PATH_not_just_payload(self):
        # The r1 probe, pinned: identical text added to a.txt and to
        # b.txt must NOT collide — the path axis is part of what an approve
        # binds, and the first cut's payload-only hash dropped it.
        self.git("checkout", "-q", "-b", "path-a", self.a)
        a = self.commit("identical text", path="a.txt")
        self.git("checkout", "-q", self.main)
        self.git("checkout", "-q", "-b", "path-b", self.a)
        b = self.commit("identical text", path="b.txt")
        self.git("checkout", "-q", self.main)
        ia = landreq._commit_content_id(self.repo, a)
        ib = landreq._commit_content_id(self.repo, b)
        self.assertTrue(ia and ib)
        self.assertNotEqual(ia, ib)
        # and the control on the same observable: a's own cherry-pick onto
        # trunk (same path, same payload, moved context) MUST match.
        self.git("cherry-pick", "-q", a) if False else None
        self.git("checkout", "-q", "-b", "carry-a", self.main)
        self.git("cherry-pick", a)
        picked = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(ia, landreq._commit_content_id(self.repo, picked))

    def test_the_content_id_attributes_each_files_payload_to_its_own_path(self):
        # The r4 probe, pinned: `path` is PER-FILE state and
        # must reset on `diff --git`. Held across files, two commits that
        # edit a.txt identically and delete DIFFERENT same-content files
        # hashed byte-identical — pid differed, digest agreed, and one
        # instrument's collision is all a launder needs (measured live
        # against lane bigfile-split-web-ui-html before the cure). The edit
        # and the deletion must ride ONE commit: staged as two commits, the
        # content-id'd tip is a single-file deletion with no second path to
        # leak into, and the arm passes under the mutation (the first two
        # drafts of this arm made exactly that mistake).
        self.git("checkout", "-q", "-b", "multi-del", self.a)
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("A0\n")
        with open(os.path.join(self.repo, "z1"), "w") as f:
            f.write("same\n")
        with open(os.path.join(self.repo, "z2"), "w") as f:
            f.write("same\n")
        self.git("add", "a.txt", "z1", "z2")
        self.git("commit", "-q", "-m", "a.txt plus two same-content files")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "del-z1", base)
        os.remove(os.path.join(self.repo, "z1"))
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("identical edit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "edit a.txt and del z1 in ONE commit")
        c1 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "del-z2", base)
        os.remove(os.path.join(self.repo, "z2"))
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("identical edit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "edit a.txt and del z2 in ONE commit")
        c2 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        i1 = landreq._commit_content_id(self.repo, c1)
        i2 = landreq._commit_content_id(self.repo, c2)
        self.assertTrue(i1 and i2)
        self.assertNotEqual(i1[1], i2[1],
                            "the digest attributed z1's and z2's deletions "
                            "to the same path — per-file state leaked")
        # control on the same observable: two commits with IDENTICAL
        # deletions must agree on the digest, or the finding arm passes by
        # the digest having stopped binding anything at all.
        self.git("checkout", "-q", "-b", "del-z1-again", base)
        os.remove(os.path.join(self.repo, "z1"))
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("identical edit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "edit a.txt and del z1, restaged")
        c3 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(i1[1],
                         landreq._commit_content_id(self.repo, c3)[1],
                         "control: identical content must digest identically")

    def test_a_multi_commit_range_composes_with_per_commit_identity(self):
        # The end-to-end carry through the real verb: a two-commit lane
        # composes, and the manifest's recorded per-range identity can be
        # checked commit-by-commit — the granularity the aggregate never
        # had. (Measured: the compose-path eviction of an impostor tip is
        # impossible by construction, because compose reads review_sha —
        # the verdict's own tip; a row pointing anywhere else is a ledger
        # forgery, a different threat with a different door.)
        self.git("checkout", "-q", "-b", "two-commit", self.a)
        c1 = self.commit("first", path="g")
        c2 = self.commit("second", path="g2")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        one = self.dispatch(ref=tip, lane="lane/two")
        self.approve(one, tip)
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        lrs[one["id"]]["state"] = "READY"
        with mock.patch.object(landreq, "project",
                               lambda now=None: (lrs, None)):
            rc, out, err = self.compose(one["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(len(got["members"]), 1)
        ids = [landreq._commit_content_id(self.repo, s) for s in (c1, c2)]
        self.assertTrue(all(ids))
        self.assertNotEqual(ids[0], ids[1])

    # ------------------------------------------------------------------
    # The r5 false-carry classes (row 5f394d10): each fixture drives a
    # CLEAN cherry-pick (rc 0) whose composed commit differs from the
    # approved one in exactly the axis the digest was blind to, and each
    # arm asserts compose's DECISION and its STATED reason — never the
    # absence of an error. The clean pick reaching the drift clause is
    # itself measured: a fixture that trips an earlier clause (conflict,
    # count, unmeasurable) answers a DIFFERENT reason and the assertIn
    # fails. Pre-cure, every one of these picks answered CARRIES with
    # patch-ids DIFFERING (measured pairwise) — the digest was the sole
    # deciding vote, and it hashed each class under the `?` path.

    def fork_point(self, *writes, config=None):
        """A fresh fixture base on trunk: write files, one commit. Keeps
        these fixtures off `state`, which trunk's own b/c commits edit."""
        for path, payload in writes:
            full = os.path.join(self.repo, path)
            os.makedirs(os.path.dirname(full) or self.repo, exist_ok=True)
            with open(full, "wb") as f:
                f.write(payload)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture base")
        return self.git("rev-parse", "HEAD")

    def compose_sole(self, tip, lane):
        """Dispatch/approve the fixture tip and compose it alone, returning
        the parsed --json account — the one observable every arm (drift and
        control alike) reads."""
        row = self.dispatch(ref=tip, lane=lane)
        self.approve(row, tip)
        rc, out, err = self.compose(row["id"][:12], "--json")
        got = json.loads(out)
        got["rc"], got["stderr"] = rc, err
        return got

    def test_a_mode_only_chmod_remapped_across_a_rename_is_drift(self):
        # Class 1: lane chmods `perm`; trunk renames perm -> perm-moved.
        # merge-ort applies the mode bit to the RENAMED path — a clean pick
        # whose composed commit chmods a file the reviewer never saw. A
        # mode-only diff has no ---/+++ lines, so the pre-cure digest
        # hashed both mode changes under `?` and the OR carried it.
        base = self.fork_point(("perm", b"payload\n"))
        self.git("checkout", "-q", "-b", "chmod-lane", base)
        os.chmod(os.path.join(self.repo, "perm"), 0o755)
        self.git("add", "perm")
        self.git("commit", "-q", "-m", "chmod perm")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("mv", "perm", "perm-moved")
        self.git("commit", "-q", "-m", "trunk renames perm")
        got = self.compose_sole(tip, "lane/chmod-drift")
        self.assertEqual(got["members"], [])
        self.assertEqual(got["rc"], 1)
        self.assertEqual(len(got["excluded"]), 1)
        reason = got["excluded"][0]["reason"]
        self.assertIn("content drift", reason)
        self.assertIn(tip[:12], reason)

    def test_a_mode_only_chmod_on_an_untouched_path_still_carries(self):
        # Positive control on the same observable: the identical mode-only
        # commit with trunk NOT renaming underneath must still compose.
        base = self.fork_point(("perm2", b"payload\n"))
        self.git("checkout", "-q", "-b", "chmod-clean", base)
        os.chmod(os.path.join(self.repo, "perm2"), 0o755)
        self.git("add", "perm2")
        self.git("commit", "-q", "-m", "chmod perm2")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        got = self.compose_sole(tip, "lane/chmod-clean")
        self.assertEqual(got["rc"], 0, got["stderr"])
        self.assertEqual(got["excluded"], [])
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_a_quoted_path_add_remapped_by_a_directory_rename_is_drift(self):
        # Class 2's compose-reachable NEIGHBOR, pinned as a regression
        # guard: an ADD's `+++ "b/qd/.."` bound the raw quoted token even
        # pre-cure (the else-branch keeps the whole token), so this shape
        # was already evicted at trunk — measured, not assumed. The cure
        # moves path binding to DECODED canonical bytes; this arm proves
        # the discrimination survives that change. With
        # merge.directoryRenames the pick cleanly RELOCATES the approved
        # add into the renamed directory — content at a path the reviewer
        # never saw.
        self.git("config", "merge.directoryRenames", "true")
        base = self.fork_point((os.path.join("qd", "seed.txt"), b"x\n"))
        self.git("checkout", "-q", "-b", "quoted-lane", base)
        with open(os.path.join(self.repo, "qd", "pä-new.txt"),
                  "wb") as f:
            f.write(b"newpayload\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add quoted path")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("mv", "qd", "qe")
        self.git("commit", "-q", "-m", "trunk renames the directory")
        got = self.compose_sole(tip, "lane/quoted-drift")
        self.assertEqual(got["members"], [])
        self.assertEqual(got["rc"], 1)
        self.assertEqual(len(got["excluded"]), 1)
        reason = got["excluded"][0]["reason"]
        self.assertIn("content drift", reason)
        self.assertIn(tip[:12], reason)

    def test_a_quoted_path_add_and_deletion_still_carry_when_clean(self):
        # Positive control, same observable: a lane that adds one quoted
        # path and deletes another, picked onto an untouched trunk, must
        # compose with both commits carried.
        base = self.fork_point(("pä-old.txt", b"same content\n"))
        self.git("checkout", "-q", "-b", "quoted-clean", base)
        with open(os.path.join(self.repo, "pä-add.txt"), "wb") as f:
            f.write(b"newpayload\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add quoted")
        self.git("rm", "-q", "pä-old.txt")
        self.git("commit", "-q", "-m", "delete quoted")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        got = self.compose_sole(tip, "lane/quoted-clean")
        self.assertEqual(got["rc"], 0, got["stderr"])
        self.assertEqual(got["excluded"], [])
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_quoted_deletion_paths_do_not_collide_in_the_digest(self):
        # Class 2, the deletion flavor named in review. It cannot reach the
        # drift clause through a clean pick — deleting a file trunk renamed
        # is a rename/delete CONFLICT (measured: rc 1) — so the collision
        # is pinned at the exact seam compose evaluates: two deletions of
        # DIFFERENT quoted paths with identical bytes must not share a
        # digest. Pre-cure both hashed under `?` and were byte-identical.
        base = self.fork_point(("pä1.txt", b"same content\n"),
                               ("pä2.txt", b"same content\n"))
        self.git("checkout", "-q", "-b", "qdel-1", base)
        self.git("rm", "-q", "pä1.txt")
        self.git("commit", "-q", "-m", "del quoted 1")
        c1 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "qdel-2", base)
        self.git("rm", "-q", "pä2.txt")
        self.git("commit", "-q", "-m", "del quoted 2")
        c2 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        i1 = landreq._commit_content_id(self.repo, c1)
        i2 = landreq._commit_content_id(self.repo, c2)
        self.assertTrue(i1 and i2)
        self.assertNotEqual(i1[1], i2[1],
                            "two deletions of different quoted paths share "
                            "a digest — the path axis is unbound")
        # control on the same observable: the SAME quoted deletion restaged
        # must digest identically, or the finding above is satisfied by a
        # digest that says everything differs.
        self.git("checkout", "-q", "-b", "qdel-1-again", base)
        self.git("rm", "-q", "pä1.txt")
        self.git("commit", "-q", "-m", "del quoted 1 again")
        c3 = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertEqual(i1[1], landreq._commit_content_id(self.repo, c3)[1])

    def test_the_digest_ignores_the_repos_quotepath_config(self):
        # The rename META lines hash the RENDERED path text, and these ids
        # cross repos in manifests — so the rendering is pinned
        # (core.quotepath=true), not inherited from whatever config the
        # measuring box happens to have. A rename of a quoted path is the
        # shape whose META text changes under quotepath=false.
        base = self.fork_point(("pö-src.txt", b"payload\n"))
        self.git("checkout", "-q", "-b", "quotecfg", base)
        self.git("mv", "pö-src.txt", "pö-dst.txt")
        self.git("commit", "-q", "-m", "rename quoted")
        c = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        pinned = landreq._commit_content_id(self.repo, c)
        self.assertTrue(pinned)
        self.git("config", "core.quotepath", "false")
        try:
            self.assertEqual(pinned,
                             landreq._commit_content_id(self.repo, c))
        finally:
            self.git("config", "--unset", "core.quotepath")

    # A 12-line `state`, final line `old` with NO trailing newline, so a
    # change to ONLY the final line sits in a hunk with real context lines
    # above it — the shape a genuine moved-context control needs.
    _CTX_HEAD = "\n".join("line%d" % i for i in range(1, 12)) + "\n"

    def _ctx_base(self):
        """A base whose `state` is the 12-line context file ending `old`
        (no trailing newline). Committed once per test, off self.a."""
        if getattr(self, "_ctxbase", None):
            return self._ctxbase
        self.git("checkout", "-q", "-b", "ctx-base", self.a)
        with open(os.path.join(self.repo, "state"), "w") as f:
            f.write(self._CTX_HEAD + "old")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "context base")
        self._ctxbase = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return self._ctxbase

    def _moved_ctx_base(self, tag):
        """A DESCENDANT base whose NEARBY hunk context genuinely moved: line
        10 becomes `TEN-MOVED` while the final `old` line is untouched, so a
        restaged final-line change rides shifted context (the
        validated control). Asserted to differ from the plain base."""
        base = self._ctx_base()
        self.git("checkout", "-q", "-b", tag, base)
        lines = (self._CTX_HEAD + "old").split("\n")
        lines[9] = "TEN-MOVED"
        with open(os.path.join(self.repo, "state"), "w") as f:
            f.write("\n".join(lines))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "move nearby context")
        moved = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertNotEqual(moved, base, "the control base must have moved")
        return moved

    def _header_shaped_payload_commit(self, branch, payload, base=None):
        """A commit changing ONLY the final line of `state` to `payload`
        (whose text renders like a file header: `++ alpha` diffs as
        `+++ alpha`, byte-identical to `+++ b/<path>`). The collision class
        measured in review. On a 12-line file so the hunk carries genuine
        context lines a moved-base control can shift."""
        self.git("checkout", "-q", "-b", branch, base or self._ctx_base())
        # Change ONLY the final line, preserving whatever context the base
        # carries (a moved base has TEN-MOVED at line 10, which must survive
        # so the restaged change rides the shifted context).
        with open(os.path.join(self.repo, "state")) as f:
            cur = f.read().split("\n")
        cur[-1] = payload                       # final line, no trailing NL
        with open(os.path.join(self.repo, "state"), "w") as f:
            f.write("\n".join(cur))
        self.git("add", "state")
        self.git("commit", "-q", "-m", "payload " + payload)
        sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return sha

    def test_header_shaped_payloads_do_not_share_a_digest(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(ia and ib)` proves the instrument reaches a real identity on the same observable, and the moved-base restage `assertEqual(ia[1], a2)` is the unconditional positive control: the digest provably CAN match, so the assertNotEqual is a discrimination, not an inert probe
        # The second stop, the live false-carry axis. Inside a hunk a
        # payload line `+++ alpha` / `--- alpha` renders byte-identical to a
        # file header; the pre-cure scan read `+++ `/`--- ` UNCONDITIONALLY,
        # so the payload never reached `parts` and two commits adding the
        # same no-newline path with payloads `++ alpha` vs `++ bravo` shared
        # ONE digest (measured: patch-ids distinct, digests equal). Under
        # Compose's patch-OR-digest that is a false carry.
        a = self._header_shaped_payload_commit("hsp-a", "++ alpha")
        b = self._header_shaped_payload_commit("hsp-b", "++ bravo")
        ia = landreq._commit_content_id(self.repo, a)
        ib = landreq._commit_content_id(self.repo, b)
        self.assertTrue(ia and ib, "instrument must reach a real identity")
        self.assertNotEqual(ia[1], ib[1],
                            "payloads `++ alpha` vs `++ bravo` share a digest "
                            "— the in-hunk header swallow survived")
        # control on the same observable: the SAME final-line change restaged
        # on a base whose NEARBY HUNK CONTEXT genuinely moved (line 10 ->
        # TEN-MOVED) must digest identically — the digest strips context and
        # patch-id does not, so this is the context-shift survival the digest
        # exists to provide. The base is asserted to differ; a same-base
        # restage would prove determinism, not context independence.
        moved = self._moved_ctx_base("hsp-moved")
        a2 = self._header_shaped_payload_commit("hsp-a2", "++ alpha",
                                                base=moved)
        i2 = landreq._commit_content_id(self.repo, a2)
        self.assertTrue(i2, "moved-context restage must be measurable")
        self.assertNotEqual(
            ia[0], i2[0],
            "the moved context must change patch-id before digest equality "
            "can prove context erasure")
        self.assertEqual(ia[1], i2[1])

    def test_header_shaped_REMOVALS_do_not_share_a_digest(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(ia and ib)` proves a real identity on the same observable, and the moved-base restage `assertEqual(ia[1], a2)` is the unconditional positive control: the digest provably CAN match, so the assertNotEqual discriminates
        # The mirror: removing a line whose text is `-- alpha` renders
        # `--- alpha` inside the hunk, the `--- ` header's exact shape. Each
        # removal needs its OWN seed holding that payload line (no trailing
        # newline), so the removed line is the payload and nothing else.
        def removal(branch, payload, base=None):
            self.git("checkout", "-q", "-b", branch, base or self._ctx_base())
            # Seed the payload as the final line, PRESERVING the base's
            # current context — writing `_CTX_HEAD + payload` would revert
            # the moved base's TEN-MOVED line 10, so the removal's parent
            # would carry un-moved context and the patch-ids would falsely
            # agree (measured: patch_equal=True). Read the base's
            # state and replace only the final line.
            with open(os.path.join(self.repo, "state")) as f:
                cur = f.read().split("\n")
            cur[-1] = payload                          # seed: payload, no NL
            with open(os.path.join(self.repo, "state"), "w") as f:
                f.write("\n".join(cur))
            self.git("add", "state")
            self.git("commit", "-q", "-m", "seed " + payload)
            cur[-1] = "old"                            # remove the payload line
            with open(os.path.join(self.repo, "state"), "w") as f:
                f.write("\n".join(cur))
            self.git("add", "state")
            self.git("commit", "-q", "-m", "removal " + payload)
            sha = self.git("rev-parse", "HEAD")     # the removal commit
            self.git("checkout", "-q", self.main)
            return sha

        a = removal("hsr-a", "-- alpha")
        b = removal("hsr-b", "-- bravo")
        ia = landreq._commit_content_id(self.repo, a)
        ib = landreq._commit_content_id(self.repo, b)
        self.assertTrue(ia and ib)
        self.assertNotEqual(ia[1], ib[1],
                            "removals `-- alpha` vs `-- bravo` share a digest "
                            "— the in-hunk header swallow survived")
        # control on a genuinely moved NEARBY-CONTEXT base (asserted), not a
        # same-base restage: the descendant base moved line 10, so
        # the same removal restaged on it must digest identically.
        moved = self._moved_ctx_base("hsr-moved")
        a2 = removal("hsr-a2", "-- alpha", base=moved)
        i2 = landreq._commit_content_id(self.repo, a2)
        self.assertTrue(i2, "moved-context restage must be measurable")
        self.assertNotEqual(
            ia[0], i2[0],
            "the moved context must change patch-id before digest equality "
            "can prove context erasure")
        self.assertEqual(ia[1], i2[1])

    def test_an_unsupported_hunk_header_makes_the_identity_unmeasurable(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(plain and plain[1])` is the unconditional positive control on the SAME observable: it proves the mock backend drives this instrument to a real, non-None identity, so each assertIsNone below is the scanner REFUSING an unsupported hunk, not the harness failing to reach it
        # The strict-grammar ruling: `startswith(b"@@")` also matches
        # the combined-diff `@@@` header and any malformed `@@` line, and
        # this parser has no scan rules for either. Guessing the state from
        # one would mint an identity for a diff it did not actually parse.
        # MOCKED because real git only emits these under combined diff,
        # which `show` never produces here — the defect is in how the scanner
        # treats a stream it may be HANDED.
        hdr = (b"diff --git a/f.txt b/f.txt\n"
               b"index 1111111111111111111111111111111111111111.."
               b"2222222222222222222222222222222222222222 100644\n"
               b"--- a/f.txt\n+++ b/f.txt\n")

        class _BE:
            def __init__(self, body):
                self.body = body

            def run(self, root, *a, **k):
                return (0, self.body, "")

            def text(self, root, *a, **k):
                return (0, "d" * 40 + " x", "")

        def cid(body):
            with mock.patch.object(landreq.vcs, "backend",
                                   lambda root: _BE(body)):
                return landreq._commit_content_id(self.repo, "sha")

        # positive control on the same instrument: a REGULAR hunk parses.
        plain = cid(hdr + b"@@ -1 +1 @@\n-old\n+new\n")
        self.assertTrue(plain and plain[1],
                        "the mock backend must produce a real identity")
        # THE CLAIM, both unsupported shapes refused.
        combined = cid(hdr + b"@@@ -1,2 -1,2 +1,3 @@@\n-old\n+new\n")
        self.assertIsNone(combined,
                          "a combined-diff @@@ header is not a regular hunk")
        malformed = cid(hdr + b"@@ not-a-range @@\n-old\n+new\n")
        self.assertIsNone(malformed,
                          "a malformed @@ header must be unmeasurable")
        # The prefix-match probe: `.match` anchors the start only, so
        # a closing `@@` followed by junk would pass a prefix grammar. The
        # full-match `\Z` anchor refuses it.
        suffix_junk = cid(hdr + b"@@ -1 +1 @@oops\n-old\n+new\n")
        self.assertIsNone(suffix_junk,
                          "a closing @@ with trailing junk is not a clean "
                          "hunk header (prefix-only .match would accept it)")
        # and the legitimate section-context suffix MUST still parse, or the
        # full-match anchor over-refuses real diffs.
        with_ctx = cid(hdr + b"@@ -1 +1 @@ int main(void)\n-old\n+new\n")
        self.assertTrue(with_ctx and with_ctx[1],
                        "a @@ header with section context must still parse")

    def test_a_payload_line_with_no_hunk_is_unmeasurable(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(plain and plain[1])` is the unconditional positive control on the SAME observable: the mock backend drives the instrument to a real identity, so each assertIsNone is the scanner REFUSING an out-of-hunk payload, not the harness failing to reach it
        # Row 1160 blocker #2. After recognized outside-hunk
        # headers/META with NO `@@` hunk opening, a raw +/- line must return
        # unmeasurable — the generic payload append used to mint a real
        # digest for `+payload-without-any-hunk` (measured: 1ba50682...),
        # pretending a diff was parsed when none was.
        hdr = (b"diff --git a/f.txt b/f.txt\n"
               b"index 1111111111111111111111111111111111111111.."
               b"2222222222222222222222222222222222222222 100644\n"
               b"--- a/f.txt\n+++ b/f.txt\n")

        class _BE:
            def __init__(self, body):
                self.body = body

            def run(self, root, *a, **k):
                return (0, self.body, "")

            def text(self, root, *a, **k):
                return (0, "d" * 40 + " x", "")

        def cid(body):
            with mock.patch.object(landreq.vcs, "backend",
                                   lambda root: _BE(body)):
                return landreq._commit_content_id(self.repo, "sha")

        plain = cid(hdr + b"@@ -1 +1 @@\n-old\n+new\n")
        self.assertTrue(plain and plain[1],
                        "the mock backend must produce a real identity")
        add = cid(hdr + b"+payload-without-any-hunk\n")
        self.assertIsNone(add, "an added line with no hunk is not a diff")
        rem = cid(hdr + b"-stray-without-any-hunk\n")
        self.assertIsNone(rem, "a removed line with no hunk is not a diff")
        # The space extension: a bare CONTEXT line outside a hunk is
        # context of nothing — pre-cure it slipped through as
        # (patch_id, None) rather than an explicit refusal.
        ctx = cid(hdr + b" context-without-any-hunk\n")
        self.assertIsNone(ctx,
                          "a context line with no hunk is context of nothing")

    def test_divergent_binary_content_composed_is_drift(self):
        # Class 3: the file is binary to diff (`-diff`) and union-merged, so
        # the pick is CLEAN while the composed blob holds bytes the reviewer
        # never saw. `git show` emits only "Binary files .. differ" — the
        # pre-cure digest bound path and binary-ness, never content, so any
        # bytes at that path carried.
        base = self.fork_point((".gitattributes",
                                b"blob.bin -diff merge=union\n"),
                               ("blob.bin", b"base\n"))
        self.git("checkout", "-q", "-b", "bin-lane", base)
        with open(os.path.join(self.repo, "blob.bin"), "ab") as f:
            f.write(b"lane-line\n")
        self.git("add", "blob.bin")
        self.git("commit", "-q", "-m", "lane edits blob")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        with open(os.path.join(self.repo, "blob.bin"), "ab") as f:
            f.write(b"trunk-line\n")
        self.git("add", "blob.bin")
        self.git("commit", "-q", "-m", "trunk edits blob")
        got = self.compose_sole(tip, "lane/binary-drift")
        self.assertEqual(got["members"], [])
        self.assertEqual(got["rc"], 1)
        self.assertEqual(len(got["excluded"]), 1)
        reason = got["excluded"][0]["reason"]
        self.assertIn("content drift", reason)
        self.assertIn(tip[:12], reason)

    def test_an_unchanged_binary_edit_still_carries(self):
        # Positive control, same observable: the identical binary edit with
        # trunk NOT touching the blob picks clean and must carry — this is
        # also the arm that would catch an abbreviation-unstable binary
        # binding (the full-index oids are what make it deterministic).
        base = self.fork_point((".gitattributes", b"blob2.bin -diff\n"),
                               ("blob2.bin", b"base\n"))
        self.git("checkout", "-q", "-b", "bin-clean", base)
        with open(os.path.join(self.repo, "blob2.bin"), "ab") as f:
            f.write(b"lane-line\n")
        self.git("add", "blob2.bin")
        self.git("commit", "-q", "-m", "lane edits blob2")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        got = self.compose_sole(tip, "lane/binary-clean")
        self.assertEqual(got["rc"], 0, got["stderr"])
        self.assertEqual(got["excluded"], [])
        self.assertEqual([m["carries"] for m in got["members"]], [True])

    def test_the_manifest_binds_the_composed_tree(self):  # noqa: VACUOUS_ASSERTION — assertRegex(derived, 40-hex) IS the unconditional positive control: derived comes from rev-parse (check=True raises on failure) and must be a real tree object name before the equality reads composed_tree, so None == None can never satisfy this arm
        # The binding-gate leg (codex: "exact head tree .. no binding
        # gate"): a gate receipt binds a TREE, and until compose RECORDS
        # the composed head's tree there is nothing for that receipt to
        # bind to this composition. The manifest and the --json account
        # must both name the tree the room's HEAD actually holds.
        tip2 = self.second_lane()
        one = self.dispatch(lane="lane/one")
        two = self.dispatch(ref=tip2, lane="lane/two")
        self.approve(one, self.side)
        self.approve(two, tip2)
        rc, out, err = self.compose(one["id"][:12], two["id"][:12], "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        derived = self.git("rev-parse", got["composed_tip"] + "^{tree}",
                           cwd=got["room"])
        # unconditional positive control: the derived anchor is a real tree
        # object name, so the equality below can never be None == None.
        self.assertRegex(derived, r"^[0-9a-f]{40}$")
        self.assertEqual(got["composed_tree"], derived)
        with open(got["room"] + ".manifest.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["composed_tree"], derived)

    def test_an_unreadable_composed_tree_refuses_in_json_and_keeps_its_anchor(self):  # noqa: VACUOUS_ASSERTION — the room's independently-read 40-hex HEAD is the positive control; the refusal must preserve that exact anchor while declining its unreadable tree
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        with self.tree_read_failure():
            rc, out, err = self.compose(one["id"][:12], two["id"][:12],
                                        "--json")
        self.assertEqual(rc, 1, err)
        got = json.loads(out)
        anchored = self.git("rev-parse", "HEAD", cwd=room)
        self.assertRegex(anchored, r"^[0-9a-f]{40}$")
        self.assertEqual(got["composed_tip"], anchored)
        self.assertIsNone(got["composed_tree"])
        self.assertEqual(got["room"], room)
        self.assertEqual(len(got["members"]), 2)
        self.assertIn("no gate can bind", got["refused"])
        self.assertIn("forced tree read failure", got["refused"])
        self.assertFalse(os.path.exists(room + ".manifest.json"))

    def test_an_unreadable_composed_tree_refuses_in_text_and_keeps_its_anchor(self):  # noqa: VACUOUS_ASSERTION — the real room and independently-read HEAD positively prove a composition exists before the text refusal is trusted
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        with self.tree_read_failure():
            rc, out, err = self.compose(one["id"][:12], two["id"][:12])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertRegex(self.git("rev-parse", "HEAD", cwd=room),
                         r"^[0-9a-f]{40}$")
        self.assertIn("no gate can bind", err)
        self.assertIn("forced tree read failure", err)
        self.assertIn("room is KEPT", err)
        self.assertIn(room, err)
        self.assertFalse(os.path.exists(room + ".manifest.json"))

    def test_an_unreadable_composed_tree_refuses_and_cleans_a_dry_run(self):  # noqa: VACUOUS_ASSERTION — two carried members positively prove the dry composition ran before the room's required absence is asserted
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        with self.tree_read_failure():
            rc, out, err = self.compose(one["id"][:12], two["id"][:12],
                                        "--dry-run", "--json")
        self.assertEqual(rc, 1, err)
        got = json.loads(out)
        self.assertEqual(len(got["members"]), 2)
        self.assertTrue(all(m["carries"] for m in got["members"]))
        self.assertIsNone(got["composed_tip"])
        self.assertIsNone(got["composed_tree"])
        self.assertIsNone(got["room"])
        self.assertTrue(got["dry_run"])
        self.assertIn("forced tree read failure", got["refused"])
        self.assertIn("dry-run room was removed", got["refused"])
        self.assertFalse(os.path.exists(room))

    def test_a_refusal_keeps_the_whole_git_diagnostic_not_its_first_line(self):
        """splitlines()[0] threw away the cause lines a multi-line git error
        carries — the operator got the banner and lost the reason."""
        one, two = self.approved_pair()
        real = landreq.vcs.backend
        def failing(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args == ("rev-parse", "HEAD^{tree}"):
                        return 1, "", "fatal: banner line\nhint: the cause line"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing):
            rc, _out, err = self.compose(one["id"][:12], two["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("fatal: banner line", err)
        self.assertIn("hint: the cause line", err)

    def test_a_kept_refusal_drops_a_stale_sibling_manifest(self):
        """The sidecar exists exactly when a composition STANDS. A manifest
        a prior composition left beside this room's path described a batch
        that no longer exists, and every later reader took it for this
        one."""
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        os.makedirs(os.path.dirname(room), exist_ok=True)
        with open(room + ".manifest.json", "w", encoding="utf-8") as f:
            f.write('{"composed_tip": "STALE-PRIOR-BATCH"}')
        with self.tree_read_failure():
            rc, _out, err = self.compose(one["id"][:12], two["id"][:12])
        self.assertEqual(rc, 1)
        # positive control on the same refusal: the room itself was kept
        self.assertIn("room is KEPT", err)
        self.assertFalse(os.path.exists(room + ".manifest.json"))

    def test_a_blocked_sidecar_removal_is_said_not_swallowed(self):
        """Removal-failed and nothing-to-remove must not share one silence:
        a directory at the sidecar path survives the drop, and the refusal
        must SAY the stale state stands rather than read as handled."""
        # A directory at the sidecar path makes unlink raise a non-ENOENT
        # error on the refusal. The must-miss lives in the sibling arm:
        # test_a_kept_refusal_drops_a_stale_sibling_manifest exercises the
        # same refusal with a removable file and its message stays quiet
        # about sidecars — absence is the goal state, not a fault.
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        os.makedirs(os.path.dirname(room), exist_ok=True)
        os.makedirs(room + ".manifest.json")
        with self.tree_read_failure():
            rc, _out, err = self.compose(one["id"][:12], two["id"][:12])
        self.assertEqual(rc, 1)
        self.assertIn("stale sidecar manifest SURVIVES", err)
        self.assertTrue(os.path.isdir(room + ".manifest.json"))

    def test_a_dry_runs_blocked_sidecar_removal_answers_in_json(self):
        """The SECOND door's own regression arm — the duplicated callsite
        most likely to drift. A dry run that composes cleanly but cannot
        remove a directory at the sidecar path refuses with a parseable
        account: rc 1, no room, and the SURVIVES sentence naming the stale
        state that outlived it."""
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        os.makedirs(os.path.dirname(room), exist_ok=True)
        os.makedirs(room + ".manifest.json")
        rc, out, err = self.compose(one["id"][:12], two["id"][:12],
                                    "--dry-run", "--json")
        self.assertEqual(rc, 1, err)
        got = json.loads(out)
        self.assertIn("SURVIVES", got["refused"])
        self.assertIsNone(got["room"])
        self.assertTrue(os.path.isdir(room + ".manifest.json"))

    def test_a_kept_anchor_claim_is_measured_and_the_text_carries_accounts(self):
        """Two cures on one refusal: the anchors-tip sentence re-reads the
        room's HEAD instead of assuming it, and the text mode carries the
        same member accounting the JSON mode always had."""
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        with self.tree_read_failure():
            rc, _out, err = self.compose(one["id"][:12], two["id"][:12])
        self.assertEqual(rc, 1)
        anchored = self.git("rev-parse", "HEAD", cwd=room)
        self.assertIn("it anchors tip %s" % anchored[:12], err)
        self.assertEqual(err.count("CARRIES"), 2)
        self.assertIn(one["id"][:12], err)
        self.assertIn(two["id"][:12], err)

    def test_a_manifest_write_failure_answers_in_json_too(self):
        """The two prose-only failure doors after composition — cleanup and
        manifest write — join the refuse-batch contract: rc 1 with a
        parseable account in --json, never empty stdout."""
        one, two = self.approved_pair()
        room = self.room_of(one, two)
        os.makedirs(room + ".manifest.json")   # open() for write now fails
        rc, out, err = self.compose(one["id"][:12], two["id"][:12], "--json")
        self.assertEqual(rc, 1, err)
        got = json.loads(out)   # must parse — the old shape printed prose
        self.assertTrue(got["refused"])
        self.assertIn("manifest could not be written", got["refused"])
        self.assertEqual(got["room"], room)
        self.assertEqual(len(got["members"]), 2)

    def test_a_dry_cleanup_failure_answers_in_json_too(self):
        """The cleanup door's half of the same contract, with the removal
        forced to fail while everything else runs real."""
        one, two = self.approved_pair()
        real = landreq.vcs.backend
        def failing(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:3] == ("worktree", "remove", "--force"):
                        return 1, "", "forced remove failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing):
            rc, out, err = self.compose(one["id"][:12], two["id"][:12],
                                        "--dry-run", "--json")
        self.assertEqual(rc, 1, err)
        got = json.loads(out)
        self.assertIn("could not remove its room", got["refused"])
        self.assertIn("forced remove failure", got["refused"])
        self.assertEqual(len(got["members"]), 2)

    def test_an_all_excluded_batch_refuses_and_lists_every_reason(self):
        open_row = self.dispatch(lane="lane/open")
        open2 = self.dispatch(lane="lane/open2")
        rc, _out, err = self.compose(open_row["id"][:12], open2["id"][:12])
        self.assertEqual(rc, 1)
        self.assertEqual(err.count("EXCLUDED"), 2)
        self.assertIn(open_row["id"][:12], err)
        self.assertIn(open2["id"][:12], err)

    def test_an_all_excluded_batch_still_answers_in_json(self):
        # The measured FIX on the reviewed tip: the all-excluded path returned
        # BEFORE the --json branch, so a scripted caller got empty stdout
        # and a JSONDecodeError — the silent-empty disease cured for the
        # human and preserved for the machine. rc stays 1; the account is
        # parseable in both modes.
        open_row = self.dispatch(lane="lane/open")
        open2 = self.dispatch(lane="lane/open2")
        rc, out, err = self.compose(open_row["id"][:12], open2["id"][:12],
                                    "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)   # must parse — the old shape raised here
        self.assertIsNone(got["composed_tip"])
        self.assertEqual(got["members"], [])
        self.assertEqual(len(got["excluded"]), 2)
        self.assertTrue(got["refused"])

    def test_a_preflight_all_excluded_batch_also_answers_in_json(self):
        # r2: the cure covered admission and the no-members guard but
        # left pre-flight and post-screen printing prose — same empty-stdout
        # disease, two doors down. An already-contained member exercises
        # pre-flight; the account must parse.
        row = self.dispatch(ref=self.b, lane="lane/one")
        self.approve(row, self.b)
        with self.ready_rows(row):
            rc, out, err = self.compose(row["id"][:12], "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)
        self.assertIsNone(got["composed_tip"])
        self.assertEqual(got["members"], [])
        self.assertEqual(len(got["excluded"]), 1)
        self.assertIn("already contained", got["excluded"][0]["reason"])

    def test_a_cleanup_halt_answers_in_json(self):
        # r2's newly-load-bearing one: scrap() carries the cleanup-failure
        # halt, and before this leg a --json caller got empty stdout on the
        # single most parse-worthy failure (room integrity).
        tip3 = self.second_lane(name="side3", path="state")
        tip4 = self.second_lane(name="side4", path="j")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        four = self.dispatch(ref=tip4, lane="lane/four")
        self.approve(one, self.side)
        self.approve(three, tip3)
        self.approve(four, tip4)
        real = landreq.vcs.backend
        def failing_reset(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:1] == ("reset",):
                        return 1, "", "forced reset failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_reset):
            rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                        four["id"][:12], "--json")
        self.assertEqual(rc, 1)
        got = json.loads(out)   # must parse even on the halt path
        self.assertIsNone(got["composed_tip"])
        self.assertIn("reset --hard", got["refused"])
        # r3: the machine field derives from the same rc the message
        # branches on — here the forced removal SUCCEEDS (only reset
        # failed), so the room is gone and the field says None.
        self.assertIsNone(got["room"])

    def test_a_resisted_removal_reports_the_room_to_the_machine(self):
        # r3's one-liner: scrap's removal-failure branch tells the human
        # the room "may hold a conflicted half-pick" and WHERE — while the
        # json leg hardcoded room None. Same rc, one read, no drift.
        tip3 = self.second_lane(name="side3", path="state")
        one = self.dispatch(lane="lane/one")
        three = self.dispatch(ref=tip3, lane="lane/three")
        self.approve(one, self.side)
        self.approve(three, tip3)
        real = landreq.vcs.backend
        def failing_removal(root):
            be = real(root)
            class Wrap:
                def __getattr__(self, name):
                    return getattr(be, name)
                def text(self, cwd, *args, **kw):
                    if args[:2] == ("worktree", "remove"):
                        return 1, "", "forced removal failure (test)"
                    return be.text(cwd, *args, **kw)
                def run(self, cwd, *args, **kw):
                    return be.run(cwd, *args, **kw)
            return Wrap()
        with mock.patch.object(landreq.vcs, "backend", failing_removal):
            rc, out, err = self.compose(one["id"][:12], three["id"][:12],
                                        "--json", "--stop-on-first")
        self.assertEqual(rc, 1)
        got = json.loads(out)
        self.assertIsNotNone(got["room"])
        self.assertIn("compose", got["room"])
        self.assertIn("may hold", got["refused"])
        # cleanup: the room really does stand (the mock refused its removal)
        self.git("worktree", "remove", "--force", got["room"])
