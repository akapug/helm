#!/usr/bin/env python3
"""`expired` — the door for a review that authorizes nothing, on work that
never landed.

Hermetic exactly as tests/test_lr_close.py, whose CloseBase this inherits:
HELM_HOME/HELM_CHAT_DIR are tmp dirs and every git repo is minted in setUp.
Two disciplines from that module carry here and are load-bearing:
  * a refusal arm asserts the EFFECT — no close event appended — never the
    absence of a complaint;
  * a scan-shaped arm seeds a MUST-HIT before any empty result is trusted,
    because this file's central regression is a scan that returned a
    PLAUSIBLE WRONG NUMBER rather than nothing.
"""
import json
import os
import subprocess
import unittest
from unittest import mock

from helm import dispatches, eventledger, landreq, rowstate
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_lr_close as _close


class LaneTipsTest(_close.CloseBase):
    """`_lane_tips_with_work` — which lanes hold work, asked of real git."""

    def lane(self, name, at=None, commit=False):
        self.git("branch", "-f", "lane/" + name, at or self.main)
        if commit:
            self.git("checkout", "-q", "lane/" + name)
            self.commit("work-" + name, path="w-" + name)
            self.git("checkout", "-q", self.main)
        return "lane/" + name

    def test_a_lane_with_commits_is_reported_and_an_empty_one_is_not(self):  # noqa: VACUOUS_ASSERTION — the assertIn on `refs` IS the unconditional positive control for the assertNotIn on that same list, one line below it
        """THE MUST-HIT IS THE WORKING LANE, and it is why the empty one's
        absence means anything: a walk that reported nothing at all would
        satisfy the second assertion on its own."""
        self.lane("busy", commit=True)
        self.lane("fresh")                      # cut at trunk, writes nothing
        tips, err = landreq._lane_tips_with_work(self.gitdir(), self.main)
        self.assertIsNone(err)
        refs = sorted(r for v in tips.values() for r in v)
        self.assertIn("lane/busy", refs,
                      "the walk found no working lane, so the absence below "
                      "proves nothing")
        self.assertNotIn("lane/fresh", refs,
                         "a lane cut at trunk with no commits of its own was "
                         "reported as holding work — every fresh claim would "
                         "shield a row")

    def test_the_walk_reads_stdout_and_not_the_process_object(self):  # noqa: VACUOUS_ASSERTION — the claim is an EQUALITY to a two-element list, not an absence; the broken read this guards against produces exactly one entry, so the assertion discriminates in both directions
        """THE REGRESSION THIS FILE EXISTS FOR. `_git` returns a
        CompletedProcess; reading it as a string stringifies the whole repr
        onto ONE line, so the parse yields a single bogus sha, `rev-list`
        answers non-zero on the garbage, and the walk reports exactly one lane
        with work. It does not raise and it does not return empty — it returns
        a plausible number, which is why it survived a review and a dogfood.

        Two real lanes with work make the count discriminating: the broken
        read can only ever produce one entry."""
        self.lane("alpha", commit=True)
        self.lane("beta", commit=True)
        tips, err = landreq._lane_tips_with_work(self.gitdir(), self.main)
        self.assertIsNone(err)
        refs = sorted(r for v in tips.values() for r in v)
        self.assertEqual(refs, ["lane/alpha", "lane/beta"],
                         "the walk did not read .stdout — a repr-as-string "
                         "parse yields exactly one bogus entry")

    def test_an_unreadable_ref_walk_is_UNKNOWN_and_never_an_empty_mapping(self):
        """"no lane holds this tip" and "I could not read the lanes" must not
        share a value on the guard side of a close."""
        self.lane("busy", commit=True)
        with mock.patch.object(landreq, "_git", return_value=None):
            tips, err = landreq._lane_tips_with_work(self.gitdir(), self.main)
        self.assertIsNone(tips, "an unreadable walk answered with a mapping")
        self.assertIn("could not be read", err)


class ExpiredBase(_close.CloseBase):
    """The projected-row fixture every suite below shares.

    ONE DEFINITION, reached by inheritance. A first cut left `lr` on the
    predicate suite and borrowed it from the others through a bound-method
    descriptor, which works and is exactly the cleverness that breaks the next
    time somebody renames a class."""

    def lr(self, **kw):
        """A projected-row shape with the fields the predicate indexes.

        Every key here is one `_lr` sets on every row it emits; the values are
        varied per arm. Derived from the producer rather than imagined: the
        predicate reads `state`, `polarity`, `tier_kind`, `land_state` and
        `reviewed_tip`, and nothing else."""
        base = {"id": "a" * 32, "state": "REVIEWED", "polarity": "concur",
                "tier_kind": None, "land_state": "ABSENT",
                # THE LADDER READS THIS AND THE FIRST FIXTURE OMITTED IT, so
                # every ladder arm refused with "is an open row" before
                # reaching the rung under test — a fixture smaller than its
                # consumer, which is the defect this docstring warns about one
                # paragraph up. A REVIEWED row always carries a verdict ref;
                # a row without one has had no review to expire.
                "verdict_ref": "receipt none — advisory review recorded",
                "reviewed_tip": self.side}
        base.update(kw)
        return base


class ExpiredVerdictTest(ExpiredBase):
    """The predicate: three dispositions, and each carries its own line."""

    def test_concur_and_pre_tier_admit_and_an_authorizing_approve_does_not(self):
        """The positive controls sit beside the refusal ON PURPOSE: a
        predicate that admitted nothing would pass the third assertion alone."""
        state, why = landreq.expired_verdict(self.lr(), {}, None)
        self.assertEqual(state, landreq.EXPIRE_ADMIT, why)
        self.assertIn("advisory", why)
        state, why = landreq.expired_verdict(
            self.lr(polarity="approve", tier_kind=dispatches.TIER_PRE_TIER),
            {}, None)
        self.assertEqual(state, landreq.EXPIRE_ADMIT, why)
        state, why = landreq.expired_verdict(
            self.lr(polarity="approve", tier_kind="stamped"), {}, None)
        self.assertEqual(state, landreq.EXPIRE_REFUSE)
        self.assertIn("can still authorize a land", why)

    def test_a_landed_tip_is_a_landed_close_not_an_expiry(self):
        seen = []
        for land in ("LANDED", "MERGED_LOCAL"):
            state, why = landreq.expired_verdict(
                self.lr(land_state=land), {}, None)
            self.assertEqual(state, landreq.EXPIRE_REFUSE, land)
            self.assertIn("REACHED the trunk", why)
            seen.append(land)
        # THE LOOP RAN. Every assertion above is inside it, so an empty
        # iterable would make this arm pass having tested nothing.
        self.assertEqual(seen, ["LANDED", "MERGED_LOCAL"])

    def test_an_unmeasured_landing_is_its_own_answer_never_a_refusal(self):
        """UNMEASURED is a third state because a caller batching over the
        board must count "could not tell" apart from "measured and no"."""
        seen = []
        for land in ("UNKNOWN", "NOT_CLAIMED", None):
            state, why = landreq.expired_verdict(
                self.lr(land_state=land), {}, None)
            self.assertEqual(state, landreq.EXPIRE_UNMEASURED, land)
            self.assertIn("MEASURED absence", why)
            seen.append(land)
        self.assertEqual(seen, ["UNKNOWN", "NOT_CLAIMED", None],
                         "the loop did not run over every producer")

    def test_a_live_lanes_tip_refuses_and_names_the_lane(self):
        state, why = landreq.expired_verdict(
            self.lr(), {self.side: ["lane/busy"]}, None)
        self.assertEqual(state, landreq.EXPIRE_REFUSE)
        self.assertIn("lane/busy", why)

    def test_unreadable_lanes_are_UNMEASURED_rather_than_admitted(self):
        """The fail-closed direction: a tip that MIGHT be a live lane's must
        never close because the walk broke."""
        state, why = landreq.expired_verdict(self.lr(), None, "refs unreadable")
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED)
        self.assertIn("refs unreadable", why)

    def test_every_disposition_carries_a_line_that_names_the_row(self):
        """The owner's shape: a predicate over evidence yields a disposition
        AND AN EVIDENCE LINE. A disposition with an empty line could not be
        recorded by the door that consumes it."""
        lines = []
        for kw in ({}, {"land_state": "LANDED"}, {"land_state": "UNKNOWN"}):
            _state, why = landreq.expired_verdict(self.lr(**kw), {}, None)
            self.assertTrue(why and why.strip(), kw)
            self.assertIn("a" * 32, why, kw)
            lines.append(why)
        self.assertEqual(len(lines), 3, "the loop did not run")
        self.assertEqual(len(set(lines)), 3,
                         "three dispositions produced the same line, so the "
                         "line does not carry the disposition")


class ExpiredUnmeasuredCauseTest(ExpiredBase):
    """An UNMEASURED refusal states what is KNOWN about why the projection
    could not measure the row, and no more. A bare refusal reads as a defect
    in the ROW, which sends the reader to cut the derivation; a refusal that
    names a cause the row does not record sends them to cut a different wrong
    layer.

    EVERY ARM HERE DRIVES THE PRODUCER. `observe_why` is written by
    `_git_observe` and by `_UNOBSERVED`, each of whose values covers SEVERAL
    worlds, so an arm that hands `expired_verdict` a hand-picked label can
    only confirm the world its author had in mind. Each arm below builds one
    of the OTHER worlds for its value out of real git and the real observation
    door, and carries the control that proves the fixture is in that world —
    the trunk refs gone with the tip object present, the derive budget unspent
    while a `cherry` fails, the observation declined only by the unresolved
    project. The refusal may not assert the cause that world rules out.
    """

    def test_no_trunk_with_the_tip_object_present_is_not_called_a_pruned_tip(self):
        """`no-trunk-or-tip` is ONE value over TWO facts, and this is the one
        the first clause could not see: the reviewed tip object is right here
        and the repository holds no recognized trunk ref. The clause may not
        say the tip is missing, because in this world it is not."""
        self.git("branch", "-m", self.main, "trunkless")
        gitdir, cache = self.gitdir(), {}
        self.assertEqual(
            landreq._trunk_refs(gitdir, cache), (None, None),
            "the fixture still names a recognized trunk ref, so the producer "
            "below would answer for a reason this arm is not about")
        self.assertTrue(
            landreq._commit_exists_cached(gitdir, self.side, {}),
            "THE MUST-HIT: the reviewed tip object is absent from this "
            "fixture, so the assertions below would hold of a genuinely "
            "pruned tip and discriminate nothing")
        facts = landreq._git_observe(gitdir, self.side, cache)
        self.assertEqual(facts["observe_why"], landreq.OBSERVE_NO_TRUNK,
                         "the producer did not reach the value under test")
        state, why = landreq.expired_verdict(
            self.lr(land_state="UNKNOWN", observe_why=facts["observe_why"]),
            {}, None)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)
        self.assertNotIn(
            "is not an object in this repository", why,
            "the refusal asserts the tip object is gone about a tip this arm "
            "just proved present")
        self.assertIn("either", why)
        self.assertIn("cat-file -t", why)
        self.assertIn(self.side[:12], why)

    def test_a_cherry_failure_is_underived_and_the_budget_is_not_named(self):
        """`underived` follows ANY landing leg that came back unknown, and a
        git call that failed is one of them. The clause may not name the
        derive budget, which this arm proves was not spent."""
        gitdir, cache = self.gitdir(), {}
        real_git = landreq._git

        def cherry_fails(gd, *args, **kw):
            if args and args[0] == "cherry":
                return subprocess.CompletedProcess(["git", *args], 1, "", "")
            return real_git(gd, *args, **kw)

        self.assertFalse(
            landreq._derive_expired(),
            "THE CONTROL: the derive budget is ALREADY expired, so an "
            "`underived` answer below would be the budget's doing and this "
            "arm would be asserting the opposite of what it measures")
        with mock.patch.object(landreq, "_git", cherry_fails), \
                mock.patch.object(landreq, "_landed_index",
                                  return_value=(None, False)):
            facts = landreq._git_observe(gitdir, self.side, cache)
            self.assertFalse(landreq._derive_expired(),
                             "the budget expired DURING the observation")
        self.assertEqual(facts["observe_why"], landreq.OBSERVE_UNDERIVED,
                         "the producer did not reach the value under test")
        state, why = landreq.expired_verdict(
            self.lr(land_state="UNKNOWN", observe_why=facts["observe_why"]),
            {}, None)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)
        self.assertNotIn(
            "derive budget ran out", why,
            "the refusal names the budget as the cause of a derivation that "
            "this arm proves failed on a git error with the budget unspent")
        self.assertIn("did not COMPLETE", why)
        self.assertNotIn("stranded", why)

    def test_an_unresolved_project_with_a_live_verdict_is_told_git_was_not_asked(self):
        """`not-asked` is emitted for a row `_will_observe_git` declines, and
        an unresolved project is declined WITH a nonretired CONCUR verdict on
        it. The clause may not relay "no verdict yet" as its own finding."""
        row = {"tip": self.side, "status": "verdict", "polarity": "concur",
               "project_unresolved": True}
        owned = {k: v for k, v in row.items() if k != "project_unresolved"}
        self.assertTrue(
            landreq._will_observe_git(owned),
            "THE MUST-HIT: this fixture is declined for some reason OTHER "
            "than the flag, so the arm proves nothing about the flag")
        self.assertFalse(landreq._will_observe_git(row),
                         "the unresolved project was observed after all")
        self.assertEqual(landreq._UNOBSERVED["observe_why"],
                         landreq.OBSERVE_NOT_ASKED,
                         "a declined row no longer carries this value")
        state, why = landreq.expired_verdict(
            self.lr(land_state="UNKNOWN", project_unresolved=True,
                    observe_why=landreq._UNOBSERVED["observe_why"]), {}, None)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)
        self.assertNotIn(
            "this row: not asked", why,
            "the refusal relays the mapping's default wording as its OWN "
            "finding about a row that carries a live CONCUR verdict")
        self.assertIn("never asked git", why)
        self.assertIn("NOT a reading of this row's verdict", why)

    def test_no_trunk_over_a_present_tip_does_not_call_trunk_the_missing_thing(self):
        """THE THIRD WORLD of `no-trunk-or-tip`, which the two-world clause
        ruled out: a recognized trunk ref, a reviewed tip object that IS in
        the store, and a commit PEEL that did not succeed. The peel projection
        answers False on any failed git call, so this world is written to the
        same value — and a later `cat-file -t` printing `commit` then reads as
        a missing trunk under a clause that says a present object leaves trunk
        as the thing missing."""
        gitdir, cache = self.gitdir(), {}
        real_git = landreq._git

        def peel_spawn_fails(gd, *args, **kw):
            if args and args[0] == "cat-file":
                return None
            return real_git(gd, *args, **kw)

        local_ref, up_ref = landreq._trunk_refs(gitdir, {})
        self.assertTrue(
            local_ref or up_ref,
            "THE MUST-HIT: this fixture names no recognized trunk ref, so a "
            "`no-trunk-or-tip` answer below would be about trunk after all "
            "and this arm would discriminate nothing")
        self.assertTrue(
            landreq._commit_exists_cached(gitdir, self.side, {}),
            "THE SECOND MUST-HIT: the reviewed tip does not peel to a commit "
            "in this fixture unpatched, so the clause's claim about a PRESENT "
            "object would not be the claim under test")
        with mock.patch.object(landreq, "_git", peel_spawn_fails):
            facts = landreq._git_observe(gitdir, self.side, cache)
        self.assertEqual(facts["observe_why"], landreq.OBSERVE_NO_TRUNK,
                         "the producer did not reach the value under test")
        state, why = landreq.expired_verdict(
            self.lr(land_state="UNKNOWN", observe_why=facts["observe_why"]),
            {}, None)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)
        self.assertNotIn(
            "leaves trunk as the thing missing", why,
            "the refusal reads a present tip object as a missing trunk, and "
            "this arm holds a named trunk with a peel call that failed")
        self.assertNotIn(
            "settles it", why,
            "the refusal credits `cat-file -t` with deciding the whole "
            "disjunction, and it separates an absent object from a present "
            "one and nothing more")
        self.assertIn("commit peel that did not succeed", why)
        self.assertIn("any failed git call", why)

    def test_underived_promises_no_outcome_for_a_reattempted_leg(self):
        """`underived` says a leg answered unknown and nothing about what the
        NEXT projection will read. A non-ancestor tip with an unavailable
        index and a transient `cherry` spawn failure reaches this value, and a
        projection taken after that failure clears can read reached or
        absent — so the clause may not promise the leg fails again."""
        gitdir, cache = self.gitdir(), {}
        real_git = landreq._git

        def cherry_spawn_fails(gd, *args, **kw):
            if args and args[0] == "cherry":
                return None
            return real_git(gd, *args, **kw)

        with mock.patch.object(landreq, "_git", cherry_spawn_fails), \
                mock.patch.object(landreq, "_landed_index",
                                  return_value=(None, False)):
            facts = landreq._git_observe(gitdir, self.side, cache)
        self.assertEqual(facts["observe_why"], landreq.OBSERVE_UNDERIVED,
                         "the producer did not reach the value under test")
        clean = landreq._git_observe(gitdir, self.side, {})
        self.assertIsNone(
            clean["observe_why"],
            "THE MUST-HIT: the same row is UNDERIVED with the spawn failure "
            "REMOVED, so the arm would be asserting about a permanent "
            "condition and the clause's promise would be true")
        state, why = landreq.expired_verdict(
            self.lr(land_state="UNKNOWN", observe_why=facts["observe_why"]),
            {}, None)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)
        self.assertNotIn(
            "fails again", why,
            "the refusal promises the re-attempted leg fails, and this arm "
            "holds a transient spawn failure whose removal measures the row")
        self.assertIn("may fail the same way", why)
        self.assertIn("can read reached or absent", why)
        self.assertIn("nothing here promises either", why)

    def test_the_cause_clause_never_turns_unmeasured_into_admit(self):
        """THE MUST-HIT: the same fixture with a MEASURED absence admits, so
        the three refusals above are the clause's doing and not a fixture
        that could never pass."""
        state, _why = landreq.expired_verdict(self.lr(), {}, None)
        self.assertEqual(state, landreq.EXPIRE_ADMIT)
        for why in (landreq.OBSERVE_NO_TRUNK, landreq.OBSERVE_UNDERIVED,
                    landreq.OBSERVE_NOT_ASKED, None):
            state, text = landreq.expired_verdict(
                self.lr(land_state="UNKNOWN", observe_why=why), {}, None)
            self.assertEqual(state, landreq.EXPIRE_UNMEASURED, why)
            self.assertIn("MEASURED absence", text)


class ExpiredCensusTest(ExpiredBase):
    """The census and the door answer from ONE predicate."""

    def rows(self):
        return [self.lr(id="b" * 32),
                self.lr(id="c" * 32, land_state="UNKNOWN"),
                self.lr(id="d" * 32, polarity="approve", tier_kind="stamped")]

    def test_the_three_buckets_are_three_and_a_closed_row_is_skipped(self):
        rows = self.rows() + [dict(self.lr(id="e" * 32),
                                   close_reason="landed")]
        out = landreq.expired_census(rows, self.gitdir(), self.main)
        self.assertEqual([e["id"] for e in out["admit"]], ["b" * 32])
        self.assertEqual([e["id"] for e in out["unmeasured"]], ["c" * 32])
        self.assertEqual([e["id"] for e in out["refuse"]], ["d" * 32])

    def test_the_census_calls_the_predicate_rather_than_restating_it(self):
        """A second copy of the rule would drift, and the drift would be
        invisible because both surfaces would still look authoritative. Proven
        by substitution: with the predicate replaced, the census reports what
        the STAND-IN says, so it cannot be reaching a private copy."""
        with mock.patch.object(landreq, "expired_verdict",
                               return_value=(landreq.EXPIRE_REFUSE, "stood in")):
            out = landreq.expired_census(self.rows(), self.gitdir(), self.main)
        self.assertEqual(out["admit"], [])
        self.assertEqual(len(out["refuse"]), 3)
        self.assertTrue(all(e["why"] == "stood in" for e in out["refuse"]))


class ExpiredLadderTest(ExpiredBase):
    """The write door."""

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def lr(self, **kw):
        """THE PROJECTION OF A ROW THAT ACTUALLY EXISTS IN THE LEDGER.

        `ExpiredBase.lr` builds a projected-row SHAPE with a literal id, which
        is right for the predicate suites: they read fields and never touch
        the store. This suite drives the write DOOR, and since task/2857 a
        rehearsal runs the writer's own validator under the ledger lock — so
        the literal id is refused as "no such dispatch" before the rung under
        test is reached. THAT REFUSAL IS THE CURE WORKING: the old dry run
        answered WOULD CLOSE for a row that does not exist, which is the
        instrument lying in the direction an operator acts on.

        The sibling class below already says why a dry-run-only suite goes
        wrong ("a dry run cannot reach the persistence table"). The same gap
        is what let this fixture stay smaller than its consumer. Only the id
        changes: every varied field an arm passes still comes from the shared
        producer, and the row is verdicted on the fixture's divergent tip, so
        the land rung still has a genuine absence to measure.
        """
        row = self.dispatch(ref=self.side, lane="lane/expiring", kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="concur")
        return dict(super().lr(**kw), id=row["id"])

    def test_a_repo_that_cannot_read_its_own_trunk_closes_nothing(self):
        """THE MASS-TERMINATION GUARD, inherited from `stranded` and mandatory
        here because this door closes in BATCHES: a momentarily unreadable
        repository could otherwise expire the whole board in one command."""
        lr = self.lr()
        # THE POSITIVE CONTROL FIRST, on the same row and the same call: with
        # the repository readable this row DRY-RUNS CLEAN, so the refusal
        # below is the guard firing rather than the row being unclosable for
        # some unrelated reason.
        ok, why = landreq._close_ladder_expired(
            lr, None, self.repo, self.main, True)
        self.assertIsNotNone(ok, why)
        self.assertEqual(ok["proof_mode"],
                         "nonauthorizing-verdict-unlanded-tip")
        with mock.patch.object(landreq, "_object_exists", return_value=False):
            out, err = landreq._close_ladder_expired(
                lr, None, self.repo, self.main, True)
        self.assertIsNone(out)
        self.assertIn("cannot prove its own trunk object", err)
        # NO LEDGER ASSERTION HERE, DELIBERATELY. Both calls are DRY RUNS, so
        # "no close event was appended" is true by construction and would be
        # an assertion that cannot fail — the module's own discipline is that
        # a refusal arm asserts the EFFECT, and this arm's effect is the
        # refusal itself, discriminated by the clean dry run above it. The
        # ledger-history discipline belongs to the arms that actually write.


class ExpiredWritesTest(ExpiredBase):
    """THE ARMS THAT DRIVE A REAL CLOSE, and the reason this class exists.

    Every other arm in this file is a DRY RUN, and a dry run cannot reach the
    persistence table: `dispatches._CLOSE_STATE_FIELDS` decides which keys
    `_record_close_proven` copies onto the event, and a reason missing from it
    persists NOTHING while every dry-run assertion stays green. `chain-proof`
    records that exact hole one entry above `expired`'s, calling itself the
    seventh registration point found by the first arms to drive a non-dry-run
    close. This class is those arms for this reason.
    """

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def real_row(self, polarity="concur", lane="lane/expiring"):
        """A REAL verdicted row through the real writer — `self.side` is the
        fixture's divergent tip, which is on no trunk, so the land rung has a
        genuine absence to measure rather than a stubbed one."""
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity=polarity)
        return row

    def test_a_real_close_persists_the_proof_the_fold_admits(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone on err is a precondition, not the claim; the unconditional positive controls are the assertGreater on the ledger count and the three persisted-field assertions read back off the event, each of which a missing _CLOSE_STATE_FIELDS entry makes fail
        row = self.real_row()
        before = self.history()
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), before,
                           "the close appended no event")
        event = self.close_event(row["id"])
        # EACH FIELD IS ONE `_CLOSE_STATE_FIELDS` ADMITS. A reason absent from
        # that table appends an event carrying none of them, and only an arm
        # that reads the PERSISTED event can tell.
        self.assertEqual(event.get("close_proof_mode"),
                         "nonauthorizing-verdict-unlanded-tip")
        self.assertTrue(event.get("closing_repo_id"), event)
        self.assertTrue(event.get("control_sha"), event)

    def test_the_persisted_evidence_is_the_predicates_own_line(self):  # noqa: VACUOUS_ASSERTION — the claim is three assertIn checks on the PERSISTED evidence string, all unconditional and all positive; the assertIsNone on err only guards that the close happened at all
        """Derived, never a caller's sentence — so a batch records a reason
        per row rather than one prose line stamped across rows that differ."""
        row = self.real_row()
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        evidence = self.close_event(row["id"]).get("close_evidence") or ""
        self.assertIn(row["id"], evidence)
        self.assertIn("advisory", evidence)
        self.assertIn(self.side[:12], evidence)

    def test_a_closed_row_is_terminal_and_maps_to_CANCELLED(self):
        """WHAT THIS DOOR PROMISES, which is narrower than it looks.

        A closed `expired` row renders REVIEWED, not CANCELLED, and that is
        correct. For a CLOSED row carrying a verdict the projection keeps the
        VERDICT INTENT unless the reason sits in
        `_TERMINAL_OVERRIDES_VERDICT`, which is one entry long and whose
        register says other reasons join BY ARGUMENT, NOT BY ACCIDENT.
        `withdrawn` — this door's sibling, the other absence door — is
        deliberately absent from it and behaves identically, so `expired`
        matching it is consistency rather than an omission.

        The contract this arm holds is the one the door owes: the row is
        CLOSED and TERMINAL, and the reason maps to CANCELLED in the canonical
        table, which is what a reader of `rowstate` gets. That the projection
        and that table say different words for a verdicted row is a
        PRE-EXISTING property shared with withdrawn, not something this lane
        introduced. Changing it would be a contract change for two doors
        wearing a bug fix, which is the failure the register's own comment
        warns against."""
        row = self.real_row()
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["close_reason"], "expired")
        self.assertEqual(rowstate._CLOSE_TERMINAL["expired"],
                         rowstate.CANCELLED)
        # THE SIBLING IS THE CONTROL: withdrawn maps the same way and is
        # likewise outside the override register, so this pair is the
        # tree's existing shape and not a hole this door opened.
        self.assertEqual(rowstate._CLOSE_TERMINAL["withdrawn"],
                         rowstate.CANCELLED)
        self.assertNotIn("withdrawn", landreq._TERMINAL_OVERRIDES_VERDICT)
        self.assertNotIn("expired", landreq._TERMINAL_OVERRIDES_VERDICT)

    def test_an_authorizing_approve_is_refused_by_the_real_writer_too(self):
        """THE MUST-MISS AT THE WRITE DOOR. The dry-run arms prove the
        predicate refuses; this proves the refusal survives to the ledger, so
        no event is appended for a row whose verdict can still carry a land."""
        # THE CONTROL IS ON THE SAME OBSERVABLE: a CONCUR row appends, so the
        # unchanged count below is this door refusing rather than a ledger
        # that never grows in this fixture.
        good = self.real_row(lane="lane/advisory")
        grew_from = self.history()
        out, err = landreq.close(good["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), grew_from,
                           "the ledger does not grow here at all, so the "
                           "refusal below proves nothing")
        row = self.real_row(polarity="approve", lane="lane/authorizing")
        before = self.history()
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(out)
        self.assertTrue(err)
        self.assertEqual(self.history(), before,
                         "a refused close still appended an event")


class ExpiredAbsenceReboundTest(ExpiredBase):
    """CURE 1 — the absence is re-measured AT THE WRITE, never read off the row.

    `lr.land_state` is a CACHED PROJECTION. The census reads the warm body on
    purpose, so the preview matches the board an operator is looking at; that
    body can predate a merge or a fetch with no ledger write of its own. Every
    fixture row below carries `land_state="ABSENT"` — the stale cache, exactly
    as production hands it to the ladder — and the arms differ only in what
    TRUNK actually contains.
    """

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def real_row(self, polarity="concur", lane="lane/expiring"):
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity=polarity)
        return row

    def stale(self, row):
        """The WARM READING: this row, believed absent from trunk."""
        return self.lr(id=row["id"], land_state="ABSENT")

    def close_one(self, row):
        return landreq._close_expired_one(
            self.stale(row), None, self.repo, self.main, False)

    def test_a_cached_ABSENT_cannot_close_a_tip_that_has_since_landed(self):  # noqa: VACUOUS_ASSERTION — the assertGreater on the ledger count after the truly-absent close is the unconditional positive control on the SAME observable the unchanged-count assertion reads
        """THE POSITIVE CONTROL RUNS FIRST, ON THE SAME CALL. A truly absent
        tip closes through this exact path, so the refusal afterwards is the
        re-measurement firing and not the fixture being unclosable."""
        absent = self.real_row(lane="lane/still-absent")
        before = self.history()
        state, out, err = self.close_one(absent)
        self.assertIsNone(err, err)
        self.assertEqual(state, landreq.EXPIRE_ADMIT)
        self.assertIsNotNone(out)
        self.assertGreater(self.history(), before,
                           "the control close appended no event, so the "
                           "refusal below proves nothing")
        # THE LANE GUARD CANNOT COVER FOR THIS AND MAKES IT WORSE: a lane whose
        # work has LANDED has no commits of its own any more, so it stops
        # protecting the tip at exactly the moment the row must not close.
        landed = self.real_row(lane="lane/landed-since")
        self.git("update-ref", "refs/heads/" + self.main, self.side)
        held = self.history()
        state, out, err = self.close_one(landed)
        self.assertIsNone(out)
        self.assertEqual(state, landreq.EXPIRE_REFUSE)
        self.assertIn("IS on trunk", err)
        self.assertEqual(self.history(), held,
                         "a landed review was closed as expired")

    def test_an_unreadable_landing_measurement_stays_OPEN(self):  # noqa: VACUOUS_ASSERTION — the control close on `lane/measurable-landing` immediately above is unconditional and reads the SAME ledger counter, so an empty ledger reddens there before the unchanged-count claim is believed; the rung cannot match them because the two readings are held in different locals
        """UNKNOWN IS NOT ABSENT. A door that closes work may not treat a
        landing question it could not answer as an answer of no."""
        # THE CONTROL FIRST, UNCONDITIONALLY: a sibling row closes through
        # this same call, so the unchanged count below is the UNKNOWN refusing
        # rather than a fixture whose ledger never grows.
        control = self.real_row(lane="lane/measurable-landing")
        grew_from = self.history()
        state, out, err = self.close_one(control)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), grew_from)
        row = self.real_row(lane="lane/unreadable-landing")
        before = self.history()
        with mock.patch.object(landreq, "landed_ever", return_value=None):
            state, out, err = self.close_one(row)
        self.assertIsNone(out)
        self.assertEqual(state, landreq.EXPIRE_UNMEASURED)
        self.assertIn("could not be proven absent", err)
        self.assertEqual(self.history(), before,
                         "an UNMEASURED landing still appended a close")

    def test_the_measurement_is_taken_against_the_PINNED_object(self):
        """NOT AGAINST A REF NAME. The ladder proves one trunk object readable
        and every later question is asked of THAT sha, so a ref that moves
        between the proof and the measurement cannot change the answer."""
        row = self.real_row(lane="lane/pinned-measure")
        pin = self.git("rev-parse", self.main)
        seen = []

        def spy(gitdir, tip, ref):
            seen.append((tip, ref))
            return False

        with mock.patch.object(landreq, "landed_ever", side_effect=spy):
            state, out, err = landreq._close_expired_one(
                self.stale(row), None, self.repo, self.main, True)
        self.assertIsNone(err, err)
        self.assertEqual(state, landreq.EXPIRE_ADMIT)
        self.assertEqual(seen, [(self.side, pin)])


class ExpiredWriterAdmissionTest(ExpiredBase):
    """CURE 2 — the writer and the replay both judge an expired close.

    The reason was registered in the polarity and persistence maps with NO
    validation branch, so it inherited the chain's default: ACCEPT. Every arm
    here mutates WHAT THE REAL WRITER WROTE rather than a hand-built event,
    because a shape a test author invented is the one shape no forger has to
    use.
    """

    def real_row(self, polarity="concur", lane="lane/admission"):
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity=polarity)
        return row

    def closed_event(self, polarity="concur", lane="lane/admission"):
        """(row, the written close event, the state it was written AGAINST).

        THE SNAPSHOT IS TAKEN BEFORE THE CLOSE, deliberately: replay judges an
        event against the state at its own position, and the post-close
        projection has already advanced past it — feeding that back refuses on
        the sequence rung and every later assertion rides on a precondition
        rather than on the branch it names."""
        row = self.real_row(polarity=polarity, lane=lane)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        before = (current[row["id"]], current, verdicts)
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        return row, self.close_event(row["id"]), before

    def replay(self, event, before):
        state, current, verdicts = before
        return dispatches._close_event_error(
            event, state, current=current, verdicts=verdicts)

    def test_the_writer_stamps_the_hold_kind_and_replay_accepts_it(self):
        """THE POSITIVE CONTROL for every refusal below: the untouched event
        the real writer wrote replays clean, and carries the authorization
        half of the proof the reason shipped without."""
        row, event, before = self.closed_event()
        self.assertEqual(event.get("close_hold_kind"), "advisory")
        self.assertEqual(event.get("close_proof_mode"),
                         "nonauthorizing-verdict-unlanded-tip")
        self.assertIsNone(self.replay(event, before))

    def test_replay_refuses_every_omitted_or_malformed_proof_field(self):  # noqa: VACUOUS_ASSERTION — the unconditional `assertIsNone(self.replay(event, before))` above the loop is the positive control on the same observable, and every forged case is a MUTATION of that same event; the per-case assertIsNotNone then proves the walk ran
        row, event, before = self.closed_event(lane="lane/malformed")
        # THE UNTOUCHED EVENT REPLAYS CLEAN, asserted here and not only in the
        # sibling arm: every refusal below is a MUTATION of this event, so a
        # forgery loop that never ran would otherwise pass on an empty walk.
        self.assertIsNone(self.replay(event, before))
        for expected, forged in (
                ("closing repo id must be an absolute path",
                 dict(event, closing_repo_id=None)),
                ("closing repo id must be an absolute path",
                 dict(event, closing_repo_id="not/absolute")),
                ("control sha must be the full trunk object",
                 dict(event, control_sha=None)),
                ("control sha must be the full trunk object",
                 dict(event, control_sha=self.side[:12])),
                ("may only ever record",
                 dict(event, close_proof_mode="object-pruned")),
                ("may only ever record",
                 dict(event, close_proof_mode=None))):
            err = self.replay(forged, before)
            self.assertIsNotNone(err, expected)
            self.assertIn(expected, err)

    def test_replay_refuses_a_close_whose_verdict_AUTHORIZES(self):  # noqa: VACUOUS_ASSERTION — same shape: the clean replay above the loop is the unconditional positive control, and each case mutates only close_hold_kind on that event
        """THE TIER IS HALF THIS PROOF. Without the recorded hold kind a
        stamped authorizing approve and a pre-tier one are the same bytes
        here, so the one population this door must never touch was
        indistinguishable from the one it exists for."""
        row, event, before = self.closed_event(lane="lane/tier-replay")
        self.assertIsNone(self.replay(event, before))
        # THE STANDING ROW HERE IS A CONCUR, so every case below is the
        # capture DISAGREEING with a record that is itself admissible — the
        # binding rung, not the membership one. The rung that refuses an
        # AUTHORIZING standing row is exercised in
        # ExpiredCaptureBindsTheRecordTest, where the row is a stamped approve.
        for kind in ("authorization-held", "unbillable", None, "advisory "):
            err = self.replay(dict(event, close_hold_kind=kind), before)
            self.assertIsNotNone(err, kind)
            self.assertIn("without binding the record", err)

    def test_the_writer_derives_the_hold_kind_and_ignores_the_ladders(self):
        """THE HOLD KIND IS THE LOCK'S, NEVER THE CALLER'S. The ladder read a
        projection built outside the lock; the writer re-derives from the row
        the lock resolved, so a caller cannot stamp an answer of its own."""
        row = self.real_row(polarity="approve", lane="lane/writer-derives")
        before = len(dispatches.history(row["id"]))
        out, err = dispatches._record_close_proven(
            row["id"], "expired", self.side, evidence="hand-typed",
            closing_repo_id=self.gitdir(),
            control_sha=self.git("rev-parse", self.main),
            proof_mode="nonauthorizing-verdict-unlanded-tip")
        self.assertIsNone(out)
        self.assertTrue(err)
        self.assertIn("AUTHORIZES NOTHING", err)
        self.assertIn("authorization-held", err)
        self.assertEqual(len(dispatches.history(row["id"])), before,
                         "the writer appended a close it refused")


class ExpiredRetryTest(ExpiredBase):
    """CURE 3 — an identical retry returns the standing row, once.

    The ladder's terminal branch runs BEFORE the derivation, so on the default
    path `evidence` is None while the standing row holds the derived line.
    Compared literally, every honest retry read as a DIFFERENT closure.
    """

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def closes(self, rid):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("event") == "close" and e.get("id") == rid]

    def real_row(self, lane="lane/retry"):
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="concur")
        return row

    def test_a_default_retry_returns_the_standing_row_with_ONE_close(self):  # noqa: VACUOUS_ASSERTION — the exactly-one count is asserted UNCONDITIONALLY after the first close, before the retry, on the same accessor the post-retry claim reads; the proof comparison is an equality to a captured dict, not an absence
        row = self.real_row()
        first, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        proof = dict(self.close_event(row["id"]))
        self.assertEqual(len(self.closes(row["id"])), 1,
                         "the first close did not append exactly one event, "
                         "so the retry assertion below measures nothing")
        again, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertIsNotNone(again)
        self.assertEqual(len(self.closes(row["id"])), 1,
                         "the retry appended a second close event")
        self.assertEqual(self.close_event(row["id"]), proof,
                         "the retry rewrote the recorded proof")

    def test_an_identical_EXPLICIT_evidence_retry_reconciles(self):
        row = self.real_row(lane="lane/retry-explicit")
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        stored = self.close_event(row["id"]).get("close_evidence")
        self.assertTrue(stored)
        again, err = landreq.close(row["id"], "expired", repo=self.repo,
                                   evidence=stored)
        self.assertIsNone(err, err)
        self.assertEqual(len(self.closes(row["id"])), 1)
        # THE MUST-MISS: a DIFFERENT explicit line is a different closure and
        # still refuses, so the reconciliation above is not blanket acceptance.
        out, err = landreq.close(row["id"], "expired", repo=self.repo,
                                 evidence="some other sentence entirely")
        self.assertIsNone(out)
        self.assertTrue(err)
        self.assertEqual(len(self.closes(row["id"])), 1)

    def test_the_LOCKED_writer_reconciles_a_racing_retry(self):  # noqa: VACUOUS_ASSERTION — the assertGreater on the ledger counter after the real close is unconditional and reads the same counter; the rung cannot match it because the before/after readings live in different locals
        """The ladder's branch never runs for a caller that raced to the lock
        with a row it read as OPEN — `_close_idempotent` is the only thing
        standing between that caller and the retired-once refusal."""
        row = self.real_row(lane="lane/retry-racing")
        grew_from = self.history()
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), grew_from,
                           "the ledger does not grow here at all, so the "
                           "unchanged count below proves nothing")
        event = self.close_event(row["id"])
        before = self.history()
        again, err = dispatches._record_close_proven(
            row["id"], "expired", self.side,
            evidence=event.get("close_evidence"),
            closing_repo_id=event.get("closing_repo_id"),
            control_sha=event.get("control_sha"),
            proof_mode="nonauthorizing-verdict-unlanded-tip")
        self.assertIsNone(err, err)
        self.assertIsNotNone(again)
        self.assertEqual(self.history(), before,
                         "the racing retry appended a second event")
        # AND THE MOVING PIN IS NOT IDENTITY: a retry taken after trunk moved
        # is the same closure, and the recorded sha stays the anchor.
        self.git("update-ref", "refs/heads/" + self.main, self.side)
        again, err = dispatches._record_close_proven(
            row["id"], "expired", self.side,
            evidence=event.get("close_evidence"),
            closing_repo_id=event.get("closing_repo_id"),
            control_sha=self.side,
            proof_mode="nonauthorizing-verdict-unlanded-tip")
        self.assertIsNone(err, err)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.close_event(row["id"]).get("control_sha"),
                         event.get("control_sha"))


class ExpiredApplyDispositionTest(ExpiredBase):
    """CURE 4 — a NEW unmeasured at apply is counted, not called a refusal.

    The census admitted the row, so a read failure during the batch is a
    disposition the predicate already has a word for. Appending it to
    `refused` reports a MEASURED no for a row nothing measured, while the
    unmeasured count sits at its earlier value looking complete.
    """

    def setUp(self):
        super().setUp()
        # THE VERB TAKES NO --repo AND NO --trunk, by design: the census reads
        # the cwd's repository and the trunk every sibling close derives. So
        # the fixture has to BE that world rather than be handed to the verb —
        # an origin, an `origin/main` for the default to resolve (the fixture's
        # own trunk branch is whatever `init` named it), and a cwd inside it.
        self.add_origin()
        self.git("update-ref", "refs/remotes/origin/main",
                 self.git("rev-parse", self.main))
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def closes(self, rid):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("event") == "close" and e.get("id") == rid]

    def run_cli(self, *args):
        import io as _io
        import contextlib
        buf, errbuf = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(errbuf):
            rc = landreq._cmd_expired(list(args))
        return rc, buf.getvalue(), errbuf.getvalue()

    def test_a_read_failure_at_apply_is_UNMEASURED_and_writes_nothing(self):
        row = self.dispatch(ref=self.side, lane="lane/apply-disposition",
                            kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="concur")
        # THE CENSUS MUST ADMIT IT FIRST or the arm below proves nothing about
        # a TRANSITION — this is the must-hit for the fixture itself.
        rc, out, errout = self.run_cli("--json")
        self.assertTrue(out, "the census printed nothing (rc %s): %s"
                        % (rc, errout))
        census = json.loads(out)
        self.assertIn(row["id"], [e["id"] for e in census["admit"]],
                      "the census did not admit the row under test: %s" % out)
        real = landreq._lane_tips_with_work
        calls = []

        def flaky(gitdir, trunk):
            calls.append(1)
            if len(calls) == 1:            # the census walk still succeeds
                return real(gitdir, trunk)
            return None, "refs unreadable during apply"

        with mock.patch.object(landreq, "_lane_tips_with_work",
                               side_effect=flaky):
            rc, out, errout = self.run_cli("--apply", "--json")
        self.assertTrue(out, "the apply printed nothing (rc %s): %s"
                        % (rc, errout))
        applied = json.loads(out)
        self.assertEqual([e["id"] for e in applied["unmeasured"]
                          if e["id"] == row["id"]], [row["id"]],
                         "the row is not in the unmeasured bucket: %s" % out)
        self.assertNotIn(row["id"], [e["id"] for e in applied["refused"]],
                         "an unmeasured row was reported as a measured "
                         "refusal: %s" % out)
        self.assertEqual(applied["closed"], [])
        self.assertEqual(self.closes(row["id"]), [],
                         "an unmeasured row was closed")
        self.assertEqual(rc, 1, "an unmeasured apply reported a clean sweep")


class RecordedHoldKindTest(ExpiredBase):
    """The one classifier both close doors bind to, asked directly.

    THIS FUNCTION IS ASKED HERE AND NOT ONLY THROUGH THE DOORS THAT CALL IT,
    because its whole failure surface fits in one line and none of it is
    reachable from a CONCUR fixture. `approval_tier_for_verdict` returns the
    PAIR (state, why); handing that pair to `tier_unknown_kind`, which compares
    `str(tier_state)` to "unknown", answers None for everything and so makes
    every APPROVE read authorization-held — including a genuine historical
    pre-tier one, which is the population this door exists for. A suite whose
    real-writer positives are all CONCUR stays green through that.
    """

    def test_the_four_answers_come_from_immutable_fields_only(self):
        # UNCONDITIONAL, OUTSIDE THE WALK: the two poles this door turns on,
        # so a table that silently emptied cannot pass on an unexecuted loop.
        self.assertEqual(landreq.recorded_hold_kind(
            {"verdict_ref": "r", "polarity": "concur"}), "advisory")
        self.assertEqual(landreq.recorded_hold_kind(
            {"verdict_ref": "r", "polarity": "approve",
             "verdict_tier_evidence": {}, "verdict_tier_anchor": "a"}),
            "authorization-held")
        cases = (({}, "unbillable"),
                 ({"verdict_ref": "r", "polarity": "concur"}, "advisory"),
                 ({"verdict_ref": "r", "polarity": "approve"}, "pre-tier"),
                 ({"verdict_ref": "r", "polarity": "approve",
                   "verdict_tier_evidence": {}, "verdict_tier_anchor": "a"},
                  "authorization-held"),
                 ({"verdict_ref": "r", "polarity": "fix"}, "unbillable"))
        for row, want in cases:
            self.assertEqual(landreq.recorded_hold_kind(row), want, row)

    def test_a_STAMPED_approve_is_authorizing_whatever_its_evidence_says(self):
        """PRE-TIER IS THE ABSENCE OF THE FIELDS, never a judgement about
        them. An approve whose stamped evidence is malformed reads DAMAGED to
        the tier resolver and authorization-held here, which is the same
        refusal an `ok` tier gets — so this door needs only the bit that
        cannot move, and never loads policy to find it."""
        damaged = {"verdict_ref": "r", "polarity": "approve",
                   "verdict_tier_evidence": "not-a-dict",
                   "verdict_tier_anchor": "bogus"}
        self.assertEqual(landreq.recorded_hold_kind(damaged),
                         "authorization-held")
        self.assertNotIn(landreq.recorded_hold_kind(damaged),
                         landreq.NONAUTHORIZING_HOLDS)
        # THE POSITIVE CONTROL ON THE SAME ROW SHAPE: drop only the stamp and
        # the very same verdict becomes the pre-tier this door exists for.
        pre = {k: v for k, v in damaged.items()
               if k not in dispatches.VERDICT_TIER_FIELDS}
        self.assertEqual(landreq.recorded_hold_kind(pre), "pre-tier")
        self.assertIn(landreq.recorded_hold_kind(pre),
                      landreq.NONAUTHORIZING_HOLDS)


class ExpiredPreTierApproveTest(ExpiredBase):
    """A REAL historical pre-tier APPROVE, closed and replayed.

    THE APPROVE LEG NEEDS A POSITIVE OF ITS OWN. Every other real-writer arm in
    this file drives a CONCUR, so without this class the approve leg is reached
    only by NEGATIVES — and a negative passes whether the branch refuses for
    the right reason or for the wrong one. A row
    written by an older writer carries a verdict with polarity and NO
    record-time tier fields, which is what pre-tier IS.
    """

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def historical_verdict(self, lane, v=3):
        """A verdict written by an older writer: polarity and a ref, no
        record-time author or tier proof. `v` is the EVENT version, which the
        reducer persists as `verdict_version`."""
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": v, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": "2026-08-01T00:00:00Z",
            "reviewed_tip": self.side, "verdict_ref": "old review",
            "polarity": "approve", "gate": "", "gate_caps": []}))
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        return row, current[row["id"]]

    def test_a_v4_verdict_with_NO_tier_proof_is_damaged_not_pre_tier(self):  # noqa: VACUOUS_ASSERTION — the v3 sibling closes first and its assertGreater on the ledger counter is unconditional and reads the same counter the unchanged-count claim reads; the rung cannot match them because the two readings live in different locals
        """THE VERSION CLAUSE, and why presence alone is the wrong question.

        The reducer RETAINS this row: it accepts a v4 verdict event, persists
        verdict_version=4, and permits the author and tier fields to be wholly
        absent. So a standing state exists that has no tier fields and is NOT
        historical — its own writer was obliged to stamp a proof and did not,
        which the canonical resolver calls DAMAGED. Classifying it by absence
        alone admits a missing-proof v4 APPROVE through a door built for
        readable historical ones.
        """
        v3_row, v3_state = self.historical_verdict("lane/v3-historical", v=3)
        v4_row, v4_state = self.historical_verdict("lane/v4-missing-proof", v=4)
        # THE FIXTURES DIFFER IN EXACTLY ONE FIELD, so the two answers below
        # cannot come from anything else.
        self.assertEqual(v3_state.get("verdict_version"), 3)
        self.assertEqual(v4_state.get("verdict_version"), 4)
        # AND NEITHER CARRIES TIER OR AUTHOR PROOF, which is what makes the
        # pair discriminate: the only input to this classification they differ
        # on is the version, so the two answers below cannot come from a
        # stamped field on one of them.
        for state in (v3_state, v4_state):
            for key in (dispatches.VERDICT_TIER_FIELDS
                        + dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS):
                self.assertNotIn(key, state)
        # THE CANONICAL RESOLVER IS THE AUTHORITY THIS FUNCTION MUST AGREE
        # WITH, so both readings are taken from it rather than asserted.
        self.assertEqual(dispatches.tier_unknown_kind(
            dispatches.approval_tier_for_verdict(v3_state)[0]),
            dispatches.TIER_PRE_TIER)
        self.assertEqual(dispatches.tier_unknown_kind(
            dispatches.approval_tier_for_verdict(v4_state)[0]),
            dispatches.TIER_DAMAGED)
        self.assertEqual(landreq.recorded_hold_kind(v3_state), "pre-tier")
        self.assertEqual(landreq.recorded_hold_kind(v4_state),
                         "authorization-held")
        # AND THE DOOR REFUSES IT. The v3 sibling closing first is the
        # unconditional positive control on the same ledger counter.
        before = self.history()
        out, err = landreq.close(v3_row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), before)
        held = self.history()
        out, err = landreq.close(v4_row["id"], "expired", repo=self.repo)
        self.assertIsNone(out)
        self.assertIn("AUTHORIZES NOTHING", err or "")
        self.assertEqual(self.history(), held,
                         "a missing-proof v4 approve was closed as expired")

    def test_replay_refuses_a_forged_close_over_a_missing_proof_v4(self):  # noqa: VACUOUS_ASSERTION — the same honest event replayed against its OWN row is asserted None unconditionally on the same validator, immediately above the forgery; the rung cannot match the pair because the two calls pass different state locals
        """The writer's refusal must not be the only one: a hand-written event
        naming an allowed word over that same row has to fail replay too."""
        v3_row, v3_state = self.historical_verdict("lane/v3-replay-control")
        out, err = landreq.close(v3_row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        honest = self.close_event(v3_row["id"])
        v4_row, _v4 = self.historical_verdict("lane/v4-replay", v=4)
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        # THE SAME EVENT AGAINST ITS OWN ROW REPLAYS CLEAN, unconditionally and
        # on the same validator: the refusal below is the v4 row deciding, not
        # an event this arm forged into something unreplayable.
        self.assertIsNone(dispatches._close_event_error(
            honest, v3_state, current=current, verdicts=verdicts))
        forged = dict(honest, id=v4_row["id"], close_hold_kind="pre-tier")
        err = dispatches._close_event_error(
            forged, current[v4_row["id"]], current=current, verdicts=verdicts)
        self.assertIsNotNone(err)
        self.assertIn("AUTHORIZES NOTHING", err)

    def pre_tier_row(self, lane="lane/pre-tier"):
        """The tree's own recipe for a pre-tier verdict (the v3 shape an older
        writer emitted), with the fixture's MUST-HIT asserted here rather than
        assumed: the tier resolver itself must call this PRE-TIER, or every
        arm below is about some other row."""
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": "2026-08-01T00:00:00Z",
            "reviewed_tip": self.side, "verdict_ref": "old review",
            "polarity": "approve", "gate": "", "gate_caps": []}))
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        state = current[row["id"]]
        tier, why = dispatches.approval_tier_for_verdict(state)
        self.assertEqual(dispatches.tier_unknown_kind(tier),
                         dispatches.TIER_PRE_TIER, why)
        self.assertEqual(landreq.recorded_hold_kind(state), "pre-tier")
        return row

    def test_a_pre_tier_APPROVE_closes_and_replays(self):  # noqa: VACUOUS_ASSERTION — the clean replay is paired with an assertIsNotNone on the SAME validator over a one-field mutation, and the assertGreater on the ledger count is the unconditional positive for the close itself; the rung cannot match the pair because the two readings are held in different locals
        """THE DEFECT THIS ARM WOULD HAVE CAUGHT: with the tier pair passed
        where a state string was expected, no approve was ever pre-tier and
        the locked writer refused this row."""
        row = self.pre_tier_row()
        before = self.history()
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        standing = current[row["id"]]
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), before)
        event = self.close_event(row["id"])
        self.assertEqual(event.get("close_hold_kind"), "pre-tier")
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=current, verdicts=verdicts))
        # THE SAME CALL REFUSES A MUTATION, so the clean replay above is this
        # validator judging the event rather than a call that answers None for
        # everything handed to it.
        self.assertIsNotNone(dispatches._close_event_error(
            dict(event, close_hold_kind="advisory"), standing,
            current=current, verdicts=verdicts))

    def test_a_STAMPED_authorizing_approve_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — the pre-tier sibling closes first and its assertGreater on the ledger counter is unconditional and reads the same counter; the rung cannot match it because the before/after readings live in different locals
        """THE MUST-MISS BESIDE IT. Widening the approve leg to admit pre-tier
        must not admit the population this door exists to protect."""
        # THE CONTROL FIRST AND UNCONDITIONALLY: the pre-tier sibling closes
        # and grows the ledger, so the unchanged count below is this refusal
        # rather than a fixture in which nothing ever writes.
        allowed = self.pre_tier_row(lane="lane/pre-tier-control")
        grew_from = self.history()
        out, err = landreq.close(allowed["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), grew_from)
        row = self.dispatch(ref=self.side, lane="lane/stamped-approve",
                            kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="approve")
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(landreq.recorded_hold_kind(current[row["id"]]),
                         "authorization-held")
        before = self.history()
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(out)
        self.assertTrue(err)
        self.assertEqual(self.history(), before)


class ExpiredCaptureBindsTheRecordTest(ExpiredBase):
    """AN ALLOWED WORD IS NOT A PROOF.

    Membership in NONAUTHORIZING_HOLDS is a claim about the VOCABULARY, and on
    its own it admits an otherwise valid expired event over a STAMPED
    AUTHORIZING APPROVE carrying `close_hold_kind="advisory"` — replay clean,
    writer refusing, two doors and one row. An arm that forges a FORBIDDEN word
    onto a CONCUR row cannot discriminate the two rungs, because the membership
    check answers first and the binding is never reached. So every case here
    forges an ALLOWED word, and the standing row decides.
    """

    def authorizing_state(self, lane="lane/forged-capture"):
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="approve")
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        state = current[row["id"]]
        self.assertEqual(landreq.recorded_hold_kind(state),
                         "authorization-held")
        return row, state, current, verdicts

    def concur_close(self, lane="lane/honest-capture"):
        row = self.dispatch(ref=self.side, lane=lane, kind="review")
        self.mark_verdict(row["id"], self.side, "reviewed", polarity="concur")
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        standing = current[row["id"]]
        out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        return (self.close_event(row["id"]), standing, current, verdicts)

    def test_an_allowed_word_over_an_authorizing_row_is_refused(self):  # noqa: VACUOUS_ASSERTION — the clean replay of the untouched advisory event is asserted UNCONDITIONALLY before the loop and on the same validator; the per-word assertIsNotNone then proves the walk ran
        # THE HONEST EVENT FIRST AND UNCONDITIONALLY: a real advisory close
        # replays clean against its own standing row, so the refusals below
        # are the BINDING firing rather than the arm forging an unreplayable
        # event.
        event, standing, current, verdicts = self.concur_close()
        self.assertEqual(event.get("close_hold_kind"), "advisory")
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=current, verdicts=verdicts))
        _row, authorizing, cur2, ver2 = self.authorizing_state()
        for word in landreq.NONAUTHORIZING_HOLDS:
            err = dispatches._close_event_error(
                dict(event, id=authorizing["id"], close_hold_kind=word),
                authorizing, current=cur2, verdicts=ver2)
            self.assertIsNotNone(err, word)
            self.assertIn("AUTHORIZES NOTHING", err)

    def test_a_capture_disagreeing_with_its_own_row_is_refused(self):
        """Both rows are nonauthorizing here, so the refusal cannot come from
        the membership rung — only from the capture naming the wrong one."""
        event, standing, current, verdicts = self.concur_close(
            lane="lane/mismatched-capture")
        self.assertIsNone(dispatches._close_event_error(
            event, standing, current=current, verdicts=verdicts))
        err = dispatches._close_event_error(
            dict(event, close_hold_kind="pre-tier"), standing,
            current=current, verdicts=verdicts)
        self.assertIsNotNone(err)
        self.assertIn("without binding the record", err)


if __name__ == "__main__":
    unittest.main()
