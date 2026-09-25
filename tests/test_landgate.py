#!/usr/bin/env python3
"""landgate — is THIS seat qualified to land THIS lane, computed not judged.

Pins the five clauses of the sealed decision (2026-08-01 meld) and, above all,
the POLARITY: a land is irreversible on a shared trunk, so anything the module
cannot PROVE is a refusal. UNKNOWN never qualifies. That is deliberately the
opposite of helm/board.py, whose guard fails OPEN because a board write is
recoverable and the guard is for an honest mistake.

Hermetic: a tmp git repo and hand-built claims/board/gate rows. No real estate.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-landgate-home-", var="HELM_HOME")

from helm import gate as gatemod, landgate  # noqa: E402


def _proven(row, repo, where=None):
    return True, "fixture door"


class ProvenIsNotTheQuestion:
    """For arms about clause (ii)'s CONTENT (kind, status, tree derivation):
    their hand-built rows are no receipts any door placed, so the clause's
    last question, provenance (task/3066), is answered for them here. The
    arms ABOUT provenance are `ProvenanceClauseTest`, which answer nothing."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(gatemod, "land_provenance", _proven)
        patch.start()
        self.addCleanup(patch.stop)


class LandGateBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.base = self.commit("a.py", "one\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", "touch " + name)
        return self.git("rev-parse", "HEAD")

    def claims(self, holder="helm-claude", ttl=3600):
        import time
        return {landgate.LANDLOCK: {"holder": holder,
                                    "exp_mono": time.monotonic() + ttl}}


class LandlockTest(LandGateBase):
    def test_the_holder_qualifies_and_a_different_holder_does_not(self):
        st, d = landgate.holds_landlock("helm-claude", self.claims())
        self.assertEqual(st, landgate.OK, d)
        st, d = landgate.holds_landlock("helm-claude",
                                        self.claims(holder="opus-integrator"))
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("opus-integrator", d)

    def test_an_UNHELD_lock_refuses_rather_than_waving_through(self):
        st, d = landgate.holds_landlock("helm-claude", {})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("claim it", d)

    def test_an_EXPIRED_claim_is_not_held(self):
        st, d = landgate.holds_landlock("helm-claude",
                                        self.claims(ttl=-1))
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("refuse", st)          # literal pin: a constant-vs-
        self.assertIn("not held", d)         # constant equality is satisfiable
                                             # by two empty strings

    def test_an_UNDECLARED_seat_is_UNKNOWN_and_UNKNOWN_never_qualifies(self):
        st, d = landgate.holds_landlock("", self.claims())
        self.assertEqual(st, landgate.UNKNOWN)
        self.assertIn("declares no seat", d)


class GateBindsTreeTest(ProvenIsNotTheQuestion, LandGateBase):
    # suite=True DELIBERATELY: these fixtures stand in for WHOLE-SUITE
    # receipts, because that is the only kind this clause may pass. They
    # were minted before the flag was load-bearing and passed BECAUSE the
    # clause never read it — the custom/focused must-refuse poles below pin
    # the closed hole from the other side.
    REC = {"id": "abc123", "status": "OK", "tree": "deadbeefcafe",
           "suite": True}

    def test_a_receipt_for_the_LANDED_tree_qualifies(self):
        st, d = landgate.gate_binds_tree("abc123", "deadbeefcafe",
                                         {"abc123": self.REC})
        self.assertEqual(st, landgate.OK, d)
        self.assertIn("ok", st)
        self.assertIn("post-rebase tree", d)

    def test_a_receipt_for_a_DIFFERENT_tree_refuses(self):
        """The whole point of clause (ii): the reviewed tip and the landed tree
        differ after a rebase, and cross-file coupling lives in that gap."""
        st, d = landgate.gate_binds_tree("abc123", "0000111122223333",
                                         {"abc123": self.REC})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("DIFFERENT tree", d)

    def test_a_FAILED_receipt_refuses_even_on_the_right_tree(self):
        rec = dict(self.REC, status="FAILED")
        st, d = landgate.gate_binds_tree("abc123", "deadbeefcafe",
                                         {"abc123": rec})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("FAILED", d)

    def test_NO_receipt_refuses(self):
        st, d = landgate.gate_binds_tree("", "deadbeefcafe", {})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("no gate receipt", d)

    def test_a_CUSTOM_receipt_refuses_even_on_the_right_tree(self):
        """The explicit must-refuse pole for the hole this clause closed: a
        receipt that does not claim the whole suite proves only the tests it
        selected, and this clause authorizes an IRREVERSIBLE merge. The tree
        MATCHES on purpose — the refusal must be about the kind, not a tree
        mismatch riding to the rescue."""
        rec = dict(self.REC)
        del rec["suite"]
        st, d = landgate.gate_binds_tree("abc123", "deadbeefcafe",
                                         {"abc123": rec})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("custom receipt", d)
        self.assertIn("WHOLE-SUITE", d)

    def test_a_FOCUSED_receipt_refuses_and_is_named_as_such(self):
        rec = dict(self.REC, suite=False,
                   focus={"policy": "changed+importers-v2"})
        st, d = landgate.gate_binds_tree("abc123", "deadbeefcafe",
                                         {"abc123": rec})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("FOCUSED", d)
        self.assertIn("WHOLE-SUITE", d)


class GateBindsTreeDerivedTest(ProvenIsNotTheQuestion, LandGateBase):
    """task/285: the landed tree is DERIVED from the tip, never trusted from
    the caller — and compared at FULL length against a receipt tree that must
    itself be a real object."""

    def _rec_for(self, tip):
        # suite=True: whole-suite stand-ins, same reason as GateBindsTreeTest
        # — the arms here are about tree DERIVATION, and only the whole-suite
        # kind ever reaches the tree comparison.
        tree = self.git("rev-parse", "%s^{tree}" % tip)
        return tree, {"id": "g1", "status": "OK", "tree": tree,
                      "suite": True}

    def test_the_tree_is_derived_from_the_tip_and_matches_full_length(self):
        tip = self.commit("b.py", "two\n")
        tree, rec = self._rec_for(tip)
        st, d = landgate.gate_binds_tree("g1", None, {"g1": rec},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.OK, d)
        self.assertIn(tree[:12], d)

    def test_a_CALLER_SUPPLIED_tree_that_disagrees_refuses_naming_both(self):
        """The 285 defect in one arm: feeding the gate its own answer must
        stop passing. A wrong --tree against the right receipt is a lie the
        clause can now see, because it derives the truth itself."""
        tip = self.commit("b.py", "two\n")
        tree, rec = self._rec_for(tip)
        other = self.commit("c.py", "three\n")
        wrong = self.git("rev-parse", "%s^{tree}" % other)
        st, d = landgate.gate_binds_tree("g1", wrong, {"g1": rec},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn(wrong[:12], d)
        self.assertIn(tree[:12], d)

    def test_a_PREFIX_of_the_real_tree_no_longer_matches(self):
        """The both-directions prefix compare made even an honest short sha
        vacuous: receipt-tree[:12] 'matched' anything sharing the prefix.
        Now the short form RESOLVES to its full tree and PASSES — honest
        abbreviation is fine; truncation-borrowed equality is gone because
        the comparison runs full-length on both sides."""
        tip = self.commit("b.py", "two\n")
        tree, rec = self._rec_for(tip)
        short = dict(rec, tree=tree[:12])
        st, d = landgate.gate_binds_tree("g1", None, {"g1": short},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.OK, d)
        # the discriminating control on the same observable: a DIFFERENT real
        # tree's prefix resolves just as honestly — and must REFUSE, so the
        # OK above is the resolution being right, not the clause always
        # saying yes.
        other = self.commit("c.py", "three\n")
        other_tree = self.git("rev-parse", "%s^{tree}" % other)
        st, d = landgate.gate_binds_tree(
            "g1", None, {"g1": dict(rec, tree=other_tree[:12])},
            repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.REFUSE, d)

    def test_a_receipt_naming_a_NONEXISTENT_tree_is_UNKNOWN_not_ok(self):
        tip = self.commit("b.py", "two\n")
        ghost = "f" * 40
        rec = {"id": "g1", "status": "OK", "tree": ghost, "suite": True}
        st, d = landgate.gate_binds_tree("g1", None, {"g1": rec},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.UNKNOWN, d)
        self.assertIn("does not resolve", d)

    def test_an_ABBREVIATED_caller_tree_resolves_and_agrees(self):
        """The witness on the reviewed tip: caller --tree d97fa1ff1fc0 (the same
        tree, abbreviated) REFUSED with both displayed sides identical —
        the raw compare judged an honest short form a mismatch. Now the
        cross-check value is normalized before it is judged."""
        tip = self.commit("b.py", "two\n")
        tree, rec = self._rec_for(tip)
        st, d = landgate.gate_binds_tree("g1", tree[:12], {"g1": rec},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.OK, d)

    def test_an_UNRESOLVABLE_caller_tree_is_UNKNOWN_not_a_mismatch(self):
        tip = self.commit("b.py", "two\n")
        tree, rec = self._rec_for(tip)
        st, d = landgate.gate_binds_tree("g1", "f" * 40, {"g1": rec},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.UNKNOWN, d)
        self.assertIn("unmeasurable cross-check", d)

    def test_an_underivable_tip_tree_is_UNKNOWN(self):
        _, rec = self._rec_for(self.base)
        st, d = landgate.gate_binds_tree("g1", None, {"g1": rec},
                                         repo=self.repo, tip="0" * 40)
        self.assertEqual(st, landgate.UNKNOWN, d)
        self.assertIn("unmeasured", d)


class ProvenanceClauseTest(LandGateBase):
    """task/3066: clause (ii) admits only a receipt an authenticated door
    placed in the repository being landed, and asks it LAST, so every content
    refusal keeps its own words. The seam is the answer an arm injects beside
    an injected row; the default is the real reader."""

    def _rec_for(self, tip):
        tree = self.git("rev-parse", "%s^{tree}" % tip)
        return {"id": "g1", "status": "OK", "tree": tree, "suite": True}

    def test_an_unproven_receipt_refuses_on_the_right_tree_and_names_the_cure(self):
        tip = self.commit("b.py", "two\n")
        rec = self._rec_for(tip)
        st, d = landgate.gate_binds_tree(
            "g1", None, {"g1": rec}, repo=self.repo, tip=tip,
            provenance=lambda row, repo: (False, "generic import door only"))
        self.assertEqual(st, landgate.REFUSE, d)
        self.assertIn("generic import door only", d)
        self.assertIn("helm gate window launch", d)
        # CONTROL on the same observable: the same row, proven.
        st, d = landgate.gate_binds_tree("g1", None, {"g1": rec},
                                         repo=self.repo, tip=tip,
                                         provenance=_proven)
        self.assertEqual(st, landgate.OK, d)
        self.assertIn("fixture door", d)

    def test_an_unreadable_provenance_is_UNKNOWN_and_UNKNOWN_never_qualifies(self):
        tip = self.commit("b.py", "two\n")
        st, d = landgate.gate_binds_tree(
            "g1", None, {"g1": self._rec_for(tip)}, repo=self.repo, tip=tip,
            provenance=lambda row, repo: (None, "ledger unreadable"))
        self.assertEqual(st, landgate.UNKNOWN, d)
        self.assertIn("provenance UNKNOWN", d)

    def test_the_real_reader_refuses_a_row_no_door_placed(self):  # noqa: VACUOUS_ASSERTION — REFUSE and the refusal text are asserted positively; the seam arms above pass the same row when proven
        """With the flip ACTIVE (a fresh home, activated before anything
        reached its ledger): the injected row is no receipt any door placed."""
        from tests._tmphome import own_env
        from helm import gateimport
        own_env(self, "HELM_HOME", os.path.join(self.repo, ".helm-home"))
        record, err = gateimport.activate(ts="2000-01-01T00:00:00Z")
        self.assertIsNone(err, err)
        tip = self.commit("b.py", "two\n")
        st, d = landgate.gate_binds_tree("g1", None,
                                         {"g1": self._rec_for(tip)},
                                         repo=self.repo, tip=tip)
        self.assertEqual(st, landgate.REFUSE, d)
        self.assertIn("cannot authorize a land", d)

    def test_a_content_refusal_keeps_its_own_words(self):
        tip = self.commit("b.py", "two\n")
        other = self.commit("c.py", "three\n")
        st, d = landgate.gate_binds_tree(
            "g1", None, {"g1": self._rec_for(other)}, repo=self.repo, tip=tip,
            provenance=lambda row, repo: (False, "never asked"))
        self.assertEqual(st, landgate.REFUSE, d)
        self.assertIn("DIFFERENT tree", d)
        self.assertNotIn("never asked", d)


class DisjointLandedTest(LandGateBase):
    """task/284: a competitor whose content is ALREADY on trunk is not a
    competitor. Ancestry alone calls every rebased-then-landed row a
    permanent phantom blocker for its files (#72's exact shape)."""

    def _lane_touching(self, name, path):
        self.git("checkout", "-q", "-b", name, self.base)
        tip = self.commit(path, "lane text\n")
        self.git("checkout", "-q", "master")
        return tip

    def test_a_LANDED_competitor_drops_out_of_the_queue(self):
        """Fixture: the competitor's tip is literally an ancestor of the
        landing base (landed), and it OVERLAPS my file. An ancestry-only
        clause would keep it and refuse; the ladder drops it."""
        other = self._lane_touching("side", "shared.py")
        mine_tip = self.commit("shared.py", "mine\n")
        landed = lambda tip: True   # the ladder says: on trunk
        st, d = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)], landed=landed)
        self.assertEqual(st, landgate.OK, d)
        self.assertIn("0 approved-unlanded", d)

    def test_an_UNREADABLE_landedness_keeps_the_competitor_and_says_so(self):
        # Uncertainty is debt ONLY where overlap exists. A
        # DISJOINT competitor whose landedness cannot be read contributes
        # nothing — landedness cannot change disjointness — so this is OK.
        other = self._lane_touching("side", "z.py")
        mine_tip = self.commit("y.py", "mine\n")
        landed = lambda tip: None   # the ladder could not tell
        st, d = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)], landed=landed)
        self.assertEqual(st, landgate.OK, d)
        # the discriminating control on the same observable: the SAME
        # competitor with landed=False must REFUSE if it overlaps — pinned
        # by the overlap arms beside this one; here the fixture is disjoint
        # by construction, so False also passes. What must NOT happen is
        # UNKNOWN on a disjoint row, which the OK above pins.
        st2, d2 = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)],
            landed=lambda tip: False)
        self.assertEqual(st2, landgate.OK, d2)

    def test_an_unknown_competitor_that_OVERLAPS_is_UNKNOWN(self):
        # The other half: an uncertain-landed competitor that
        # OVERLAPS is UNKNOWN, never REFUSE (it may have landed, making the
        # overlap free) and never OK (it may be live, making the overlap
        # real). Same-row controls: landed=True on this fixture gives OK,
        # landed=False gives REFUSE — so None is UNKNOWN.
        other = self._lane_touching("side", "shared.py")
        mine_tip = self.commit("shared.py", "mine\n")
        st, d = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)],
            landed=lambda tip: None)
        self.assertEqual(st, landgate.UNKNOWN, d)
        self.assertIn("UNREADABLE", d)
        st, d = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)],
            landed=lambda tip: False)
        self.assertEqual(st, landgate.REFUSE, d)
        self.assertIn("overlaps lane side", d)
        st, d = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)],
            landed=lambda tip: True)
        self.assertEqual(st, landgate.OK, d)

    def test_a_LIVE_competitor_still_refuses_on_overlap(self):
        """Control: the ladder saying NOT-landed must leave the ordinary
        overlap refusal exactly as it was."""
        other = self._lane_touching("side", "shared.py")
        mine_tip = self.commit("shared.py", "mine\n")
        landed = lambda tip: False
        st, d = landgate.disjoint_from_queue(
            self.repo, self.base, mine_tip, [("side", other)], landed=landed)
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("overlaps lane side", d)
        self.assertIn("shared.py", d)


class DisjointTest(LandGateBase):
    def test_disjoint_lanes_qualify_and_an_overlap_refuses(self):
        # each lane branches FROM BASE — stacking them would make base...theirs
        # contain mine.py too, and the "overlap" would be my fixture's, not the
        # code's. (It was, on the first run.)
        mine = self.commit("mine.py", "x\n")
        self.git("checkout", "-q", "-b", "other", self.base)
        theirs = self.commit("theirs.py", "y\n")
        st, d = landgate.disjoint_from_queue(self.repo, self.base, mine,
                                             [("other", theirs)])
        self.assertEqual(st, landgate.OK, d)
        # now a lane that touches the SAME file
        self.git("checkout", "-q", "-b", "clash", self.base)
        clash = self.commit("mine.py", "z\n")
        st, d = landgate.disjoint_from_queue(self.repo, self.base, mine,
                                             [("clash", clash)])
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("mine.py", d)
        self.assertIn("clash", d)

    def test_an_EMPTY_queue_is_disjoint_by_construction(self):
        mine = self.commit("mine.py", "x\n")
        st, d = landgate.disjoint_from_queue(self.repo, self.base, mine, [])
        self.assertEqual(st, landgate.OK)
        self.assertIn("ok", st)
        self.assertIn("disjoint from 0", d)

    def test_an_UNSUPPLIED_queue_is_UNKNOWN_not_a_vacuous_pass(self):
        """NOT-SUPPLIED AND GATHERED-EMPTY ARE DIFFERENT ANSWERS. `[]` means "I
        looked and there are no other approved-unlanded lanes"; `None` means
        nobody looked, which is what a caller that FORGOT to gather passes.

        Before this, None was not even representable — it raised TypeError —
        so the only way to call the clause without a queue was to pass [] and
        receive OK. Since `qualify` returns qualified=True when every clause is
        OK, forgetting to gather silently satisfied the clause that exists to
        catch two lanes editing one file: a fail-open in the exact direction
        this module's docstring rules out."""
        mine = self.commit("mine.py", "x\n")
        # CONTROL FIRST, unconditional, same call shape: a GATHERED empty queue
        # is a real OK, so the UNKNOWN below is about absence-of-a-caller and
        # not a clause that can no longer pass.
        st, d = landgate.disjoint_from_queue(self.repo, self.base, mine, [])
        self.assertEqual(st, landgate.OK)
        st, d = landgate.disjoint_from_queue(self.repo, self.base, mine, None)
        self.assertEqual(st, landgate.UNKNOWN)
        self.assertIn("not supplied", d)

    def test_an_UNDIFFABLE_tip_is_UNKNOWN_not_disjoint(self):
        """A probe that cannot run is not evidence of absence — the shape that
        would otherwise wave through exactly the lane nobody could inspect."""
        mine = self.commit("mine.py", "x\n")
        st, d = landgate.disjoint_from_queue(self.repo, self.base, mine,
                                             [("ghost", "0" * 40)])
        self.assertEqual(st, landgate.UNKNOWN)
        self.assertIn("unprovable", d)


class FreezeTest(LandGateBase):
    def test_open_admits_and_a_freeze_refuses(self):
        self.assertEqual(landgate.freeze_admits({"freeze": "open"})[0],
                         landgate.OK)
        self.assertEqual(landgate.freeze_admits({})[0], landgate.OK)
        st, d = landgate.freeze_admits({"freeze": "land window closed"})
        self.assertEqual(st, landgate.REFUSE)
        self.assertIn("FROZEN", d)

    def test_an_unreadable_board_is_UNKNOWN(self):
        st, d = landgate.freeze_admits("not a dict")
        self.assertEqual(st, landgate.UNKNOWN)
        self.assertIn("unknown", st)
        self.assertIn("unprovable", d)


class QualifyTest(ProvenIsNotTheQuestion, LandGateBase):
    def ok_args(self, **over):
        mine = self.commit("mine.py", "x\n")
        tree = self.git("rev-parse", "HEAD^{tree}")
        args = dict(seat="helm-claude", repo=self.repo, base=self.base,
                    tip=mine, tree=tree, receipt="abc123", queue=[],
                    board={"freeze": "open"}, claims=self.claims(),
                    verdict_ok=(landgate.OK, "approved by codex at the tip"),
                    gates={"abc123": {"id": "abc123", "status": "OK",
                                      "tree": tree, "suite": True}})
        args.update(over)
        return args

    def qualify(self, **over):
        # the gate row is INJECTED rather than mocked: a side_effect that calls
        # the function it patches recurses forever, and a seam that must be
        # mocked to be tested is a seam with a missing parameter.
        return landgate.qualify(**self.ok_args(**over))

    def test_all_five_OK_qualifies(self):
        ok, rows = self.qualify()
        self.assertTrue(ok, landgate.render(rows))
        self.assertEqual(len(rows), 5, "all five clauses must be reported")

    def test_forgetting_the_queue_DISQUALIFIES_rather_than_passing(self):
        """The hazard end to end, at the level a future self-land ACTUATOR
        would hit it: a caller that omits the approved-unlanded set must not
        come back qualified. UNKNOWN never qualifies — a land is irreversible
        on a shared trunk."""
        # ok_args mints a commit, so build ONE arg set and vary only the queue
        # — calling the helper twice would try an empty second commit.
        args = self.ok_args()
        ok, rows = landgate.qualify(**args)            # control: all five OK
        self.assertTrue(ok, landgate.render(rows))
        args["queue"] = None
        ok, rows = landgate.qualify(**args)
        self.assertFalse(ok, landgate.render(rows))
        bad = [r for r in rows if r[1] != landgate.OK]
        self.assertEqual([r[0] for r in bad], ["iv-disjoint-from-queue"])
        self.assertEqual([r[1] for r in bad], [landgate.UNKNOWN])

    def test_ANY_refusal_disqualifies_and_the_row_says_which(self):
        ok, rows = self.qualify(board={"freeze": "closed"})
        self.assertFalse(ok)
        bad = [r for r in rows if r[1] != landgate.OK]
        self.assertEqual([r[0] for r in bad], ["v-freeze-admits"])

    def test_a_non_suite_receipt_disqualifies_on_the_gate_clause(self):
        """The focused kind through the WHOLE predicate: a receipt for the
        right tree that is not a whole-suite run must cost exactly the gate
        clause — the other four stay OK, so the refusal is the kind check
        and not collateral noise."""
        args = self.ok_args()
        args["gates"]["abc123"] = dict(args["gates"]["abc123"], suite=False,
                                       focus={"policy": "changed+"
                                                        "importers-v2"})
        ok, rows = landgate.qualify(**args)
        self.assertFalse(ok, landgate.render(rows))
        bad = [r for r in rows if r[1] != landgate.OK]
        self.assertEqual([r[0] for r in bad], ["ii-gate-binds-landed-tree"])
        self.assertIn("FOCUSED", bad[0][2])

    def test_UNKNOWN_NEVER_QUALIFIES(self):
        """The polarity, and it is the opposite of board.py's on purpose: a
        board write is recoverable so its guard fails OPEN; a land is
        irreversible on a shared trunk, so what cannot be proven is refused."""
        ok, rows = self.qualify(seat="")          # -> landlock UNKNOWN
        self.assertFalse(ok)
        states = {r[0]: r[1] for r in rows}
        self.assertEqual(states["i-landlock"], landgate.UNKNOWN)
        self.assertIn("unknown", states["i-landlock"])
        self.assertEqual(len(rows), 5)

    def test_a_MISSING_verdict_state_is_UNKNOWN_not_assumed_approved(self):
        ok, rows = self.qualify(verdict_ok=None)
        self.assertFalse(ok)
        states = {r[0]: r[1] for r in rows}
        self.assertEqual(states["iii-cross-family-approve"], landgate.UNKNOWN)
        self.assertIn("unknown", states["iii-cross-family-approve"])
        self.assertEqual(states["i-landlock"], landgate.OK)   # only iii is the
                                                              # missing one

    def test_render_names_the_clause_and_the_reason(self):
        _ok, rows = self.qualify(board={"freeze": "closed"})
        text = landgate.render(rows)
        self.assertIn("v-freeze-admits", text)
        self.assertIn("FROZEN", text)
        self.assertIn("i-landlock", text, "every clause is reported, not just "
                                          "the one that failed")



class CmdLandgateTest(LandGateBase):
    """The VERB, which had ZERO tests until kimi said so.

    I wired it to satisfy the built-not-wired guard and then never exercised
    it — the guard's letter without its point. A verb nobody tests is barely
    better than a module nobody calls, and the bug kimi found could only exist
    because nothing ran it: QUALIFIED was structurally unreachable and the
    NOTE explained a branch that never ran."""

    def invoke(self, args):   # NOT `run` — that shadows TestCase.run,
                              # which unittest calls with result=
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landgate.cmd_landgate(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_it_NEVER_claims_qualified_and_says_why(self):
        tip = self.commit("x.py", "x\n")
        rc, out, _e = self.invoke(["--lane", "L", "--tip", tip, "--tree", "deadbeef",
                                "--gate", "abc123", "--repo", self.repo])
        self.assertNotIn("QUALIFIED", out.replace("NOT qualified", ""))
        self.assertIn("does NOT answer", out)
        self.assertIn("WHY NOT", out)
        self.assertIn("clause (iii) is UNKNOWN", out)

    def test_rc_reports_the_REPORT_not_the_qualification(self):
        """rc 1 for a structural UNKNOWN would read to a caller as a refusal
        about its own work. The verb succeeded at reporting; that is what 0
        means here."""
        tip = self.commit("x.py", "x\n")
        rc, out, _e = self.invoke(["--lane", "L", "--tip", tip, "--tree", "deadbeef",
                               "--gate", "abc123", "--repo", self.repo])
        # rc 0 alone would pass just as well if the verb printed NOTHING, so
        # the positive control is on the same observable: it reported.
        self.assertIn("i-landlock", out)
        self.assertEqual(rc, 0)

    def test_every_clause_is_reported_not_just_the_failing_one(self):
        tip = self.commit("x.py", "x\n")
        _rc, out, _e = self.invoke(["--lane", "L", "--tip", tip, "--tree", "deadbeef",
                                 "--gate", "abc123", "--repo", self.repo])
        clauses = ("i-landlock", "ii-gate-binds-landed-tree",
                   "iii-cross-family-approve", "iv-disjoint-from-queue",
                   "v-freeze-admits")
        # An empty tuple would make the loop below assert nothing at all, and
        # the point of this test is that ALL FIVE render — so pin the count
        # unconditionally before looping over it.
        self.assertEqual(len(clauses), 5)
        for clause in clauses:
            self.assertIn(clause, out)

    def test_missing_args_refuse_with_usage(self):
        rc, _o, err = self.invoke(["--lane", "L"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)


class TheVerbAsksTheResolverThatAnswersTest(LandGateBase):
    """Clause (i) was UNPROVABLE for an entire CLASS of seat, and the class was
    not exotic — it was every seat that never exported $HELM_CHAT_NAME, which
    includes every claude-direct seat, including the one that found this.

    `cmd_landgate` asked `seats.own_name()`: the process's DECLARED name and
    nothing else, None when nothing declared one. Clause (i) then answered
    "caller declares no seat, so the lock cannot be matched" -> UNKNOWN, and
    UNKNOWN never qualifies. So the verb whose whole job is telling a seat
    whether it may land told that class of seat nothing it could act on, and
    said it in the voice of a missing capability rather than a missing export.

    Measured live before the fix (HELM_CHAT_NAME unset, a real seat holding a
    real roster row): own_name() -> None, acting_seat() -> 'helm-claude-2',
    holds_landlock('claude', None) -> refuse. So the bare-family path was
    never the breaker; the CALL SITE was. `acting_seat` is not a laxer
    resolver — it asks the declared name FIRST (keeping the cross-seat
    contamination its own docstring records closed) and only then falls back.

    These tests pin the call site, because the call site is the entire bug."""

    def invoke(self, args):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landgate.cmd_landgate(list(args))
        return rc, out.getvalue(), err.getvalue()

    def _patched(self, own, acting):
        """Swap BOTH resolvers so the report's seat name says which one the
        verb consulted. Distinguishable values on purpose: a test that let
        them return the same string could not fail."""
        from helm import seats
        return mock.patch.object(seats, "own_name", lambda: own), \
               mock.patch.object(seats, "acting_seat",
                                 lambda session=None, cwd=None: acting)

    def _report(self, own, acting):
        from helm import pk, seats
        tip = self.commit("x.py", "x\n")
        claims_path = seats.claims_path()
        read_json = pk.read_json

        # The resolver is the subject; ambient claim-ledger state must not turn
        # its clause back into UNKNOWN under a different whole-suite order.
        def read(path, default=None):
            return {} if path == claims_path else read_json(path, default)

        p_own, p_act = self._patched(own, acting)
        with p_own, p_act, mock.patch.object(pk, "read_json", side_effect=read):
            _rc, out, _e = self.invoke(["--lane", "L", "--tip", tip,
                                        "--tree", "deadbeef", "--gate", "abc123",
                                        "--repo", self.repo])
        return out

    def test_a_seat_that_declared_no_name_still_gets_a_clause_i_answer(self):
        """THE BUG. own_name is None — the ordinary state — and the verb must
        still resolve an identity and give clause (i) a real verdict."""
        out = self._report(own=None, acting="seat-from-the-resolver")
        self.assertIn("as seat-from-the-resolver", out)
        line = [ln for ln in out.splitlines() if "i-landlock" in ln]
        self.assertEqual(len(line), 1, out)
        self.assertNotIn("UNKNOWN", line[0])
        self.assertNotIn("declares no seat", out)

    def test_the_control_a_TRULY_seatless_caller_is_still_UNKNOWN(self):
        """The unconditional positive control, and the half that must NOT
        change. When NEITHER resolver can name a seat, the honest answer is
        still UNKNOWN — the fix widened who can be named, and must not have
        invented a name for a caller that genuinely has none."""
        out = self._report(own=None, acting=None)
        self.assertIn("(no seat)", out)
        line = [ln for ln in out.splitlines() if "i-landlock" in ln]
        self.assertEqual(len(line), 1, out)
        self.assertIn("UNKNOWN", line[0])
        self.assertIn("declares no seat", out)

    def test_a_declared_name_is_NOT_overridden_by_the_fallbacks(self):
        """acting_seat asks own_name FIRST, so a seat that DID declare a name
        keeps it. Pinned here because the danger of widening a resolver is
        that a roster row starts outranking the process's own claim — the
        exact inversion acting_seat's docstring says it exists to prevent."""
        from helm import seats, seats_common, seats_identity
        # PATCHED IN BOTH READERS, DIRECTLY — which is a CHOICE now, not a
        # necessity. The split moved own_name into seats_common and
        # acting_seat into seats_identity, each holding its own `home`
        # binding; since the facade gained its setattr fanout, patching
        # seats.home DOES reach both sibling cells again (measured: both
        # take the mock and both restore). This test stays off the facade
        # on purpose: its verdict is about resolver ORDER, and patching the
        # reader modules directly keeps that verdict true even if the
        # fanout machinery (pinned in test_seats_split_contract) regresses.
        with mock.patch.object(seats_common, "home") as h, \
                mock.patch.object(seats_identity, "home", h):
            h.chat_name.return_value = "i-said-who-i-am"
            self.assertEqual(seats.own_name(), "i-said-who-i-am")
            self.assertEqual(seats.acting_seat(cwd=self.repo), "i-said-who-i-am")


if __name__ == "__main__":
    unittest.main()
