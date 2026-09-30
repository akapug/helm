#!/usr/bin/env python3
"""The cured-but-unreviewed derivation — the debt nothing ever wrote down.

A FIX verdict tells the author to cure and NOTHING tells them that curing
creates a new obligation: a review dispatch on the new tip. No surface showed a
lane in that state, so authors cured and then re-gated, because re-gating is
the action the tooling makes obvious. This is the reader that finds them.

EVERY ARM INJECTS ITS ROWS. `unanswered_fixes` takes the snapshot as an
argument, so these run against a graph built for the case rather than against
the live ledger — a test whose fixture is the estate passes or fails for
reasons that have nothing to do with the code.

THE ARM THAT EARNS ITS KEEP IS test_a_cancelled_child_whose_own_chain_
CONTINUES. The first draft of this reader asked whether a FIX row had a live
DIRECT child, which bills every parent whose child was cancelled — and a
cancelled child is exactly what `rebind` leaves behind, since it cancels the
old row and opens the replacement BENEATH it. Measured on the live ledger
before it shipped: 112 parents in that shape against 16 genuine dead ends. The
burn-down would have been 87% work somebody had already done.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import obligation  # noqa: E402


def row(rid, lane="lane-x", polarity="fix", status="verdict", supersedes=None,
        sender="author-seat", recipient="reviewer-seat", ts="2026-08-10T00:00:00Z",
        chain_root=None):
    """One dispatch row.

    chain_root DEFAULTS TO THE ROOT OF THE CHAIN, NOT TO THE ROW'S OWN ID, and
    the first version of this helper got that wrong in a way that hid a real
    behaviour. helm stamps a successor with its PARENT's chain root, so a row
    whose root is its own id is a NEW-WORK chain. Giving every fixture child its
    own root made every successor FOREIGN — and a foreign chain cannot take the
    parent's obligation, so `carrier` correctly refused to discharge it while my
    arms read that as the reader being broken. The old hand-rolled walk ignored
    chain identity entirely and would have let a stranger's row answer a debt,
    which is precisely the defect a reviewer named."""
    return {"id": rid, "lane": lane, "polarity": polarity, "status": status,
            "supersedes": supersedes, "sender": sender, "recipient": recipient,
            "ts": ts, "reviewed_tip": "tip-" + rid,
            "chain_root": chain_root or rid, "_root": chain_root}


def snap(*rows):
    """The rows as a snapshot, WITH CHAIN ROOTS RESOLVED TO THE TRUE ROOT.

    A row's chain_root is the root of its whole chain, not its parent's id, and
    a fixture that stamps the parent gets it right for children and WRONG for
    grandchildren — in a -> b -> c, c's root is a. Resolving here rather than in
    `row` means every multi-level fixture is automatically honest and no test
    has to remember to pass it.

    This matters because `dispatches._same_chain` refuses a successor whose root
    differs, so a mis-stamped grandchild reads as a FOREIGN chain and cannot
    carry — which presents as the reader being broken when it is the fixture.
    That has now cost two rounds, both mine."""
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        root, hops = r["id"], 0
        while by_id.get(root, {}).get("supersedes") and hops < 100:
            root = by_id[root]["supersedes"]
            hops += 1
        r["chain_root"] = r.get("_root") or root
    return by_id


class UnansweredFixTests(unittest.TestCase):

    def test_a_FIX_nothing_supersedes_is_owed_by_its_AUTHOR(self):
        items, forks, un = obligation.unanswered_fixes(snap(row("a")), None)
        self.assertIsNone(un)
        self.assertEqual([i["row"] for i in items], ["a"])
        self.assertEqual(items[0]["owed_by"], "author")
        self.assertEqual(items[0]["owed_seat"], "author-seat",
                         "the debt is the AUTHOR's — the reviewer already "
                         "answered, which is what the FIX verdict WAS")
        self.assertEqual(forks, [])

    def test_a_row_the_LAND_LADDER_RETIRED_is_not_owed_though_its_status_says_verdict(self):
        """A FIX row that lr later DISCHARGED, CLOSED, WITHDREW or ABANDONED is
        not debt — and `status` will never say so.

        `status` names how a row FIRST closed, and the projection's transitions
        are guarded on `status == "open"`, so a retirement arriving AFTER a FIX
        verdict cannot move it. Correctly: the verdict really is how it closed.
        The retirement lands in its own fields, and this reader was not looking
        at them.

        MEASURED on the live ledger: `helm owed` reported 130 unanswered FIX
        rows, 106 of which carried a retirement AFTER their verdict. Specimen
        9641bb987009 took a FIX at 15:31Z and a discharge TWELVE HOURS LATER,
        and had rendered as debt every day since.

        EACH FIELD IS ASSERTED SEPARATELY because they are five different
        ladders (`landreq._retired_by`'s own set), and a cure that happened to
        read only `discharged` would pass a single-field arm while leaving the
        392 `close_reason` rows — the largest population — still phantom."""
        # UNCONDITIONAL POSITIVE CONTROL, FIRST and on the same observable:
        # the identical row with NO retirement IS owed, so every refusal below
        # is a fact about the retirement and not about a dead reader.
        items, _f, un = obligation.unanswered_fixes(snap(row("a")), None)
        self.assertIsNone(un)
        self.assertEqual([i["row"] for i in items], ["a"],
                         "the control row is not owed, so this arm can prove "
                         "nothing about the retirements below")

        for field, value in (("discharged", True),
                             ("withdrawn", True),
                             ("closed_by_landing", True),
                             ("abandoned", True),
                             ("close_reason", "landed")):
            r = row("a")
            r[field] = value
            got, _f2, _u2 = obligation.unanswered_fixes(snap(r), None)
            self.assertEqual(
                [i["row"] for i in got], [],
                "a row retired by %s=%r is still reported as owed FIX debt; "
                "that is the 106-of-130 phantom board" % (field, value))

    def test_a_FOREIGN_CHAIN_child_cannot_discharge_the_debt(self):
        """A successor that roots its OWN chain is different work, and a
        stranger's row may not answer this row's obligation.

        The hand-rolled walk this replaced ignored chain identity entirely — it
        followed any `supersedes` edge — so a --new-work row that happened to
        name this parent silently discharged a real FIX. `dispatches.carrier`
        refuses it through _same_chain, which is one of the four failure classes
        a reviewer enumerated when he told me to stop writing my own walk."""
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a"),
                 row("z", supersedes="a", status="open", chain_root="z")), None)
        self.assertEqual([i["row"] for i in items], ["a"],
                         "a foreign chain discharged a debt it never took")
        self.assertEqual(_f, [], "@codex-3: this arm bound the forks and threw "
                                 "them away, so a reader that ALSO fabricated a "
                                 "fork here would have passed it")

    def test_a_FIX_with_a_live_successor_is_NOT_owed(self):
        """The pole for the arm above. Without it, a reader that reports every
        FIX row unconditionally passes the first test."""
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a"), row("b", supersedes="a", status="open",
                               polarity=None)), None)
        self.assertEqual([i["row"] for i in items], [],
                         "an answered FIX was billed to its author again")

    def test_a_cancelled_child_whose_own_chain_CONTINUES_is_NOT_owed(self):
        """THE 112-ROW CASE, and the reason `answered` is a subtree question.

        `rebind` cancels the old row and opens the replacement BENEATH it, so
        a healthy rebind leaves a parent whose only child is cancelled. Asking
        for a live DIRECT child bills that parent — 112 of them on the live
        ledger against 16 real dead ends."""
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a", status="cancelled"),
                 row("c", supersedes="b", status="open", polarity=None)), None)
        self.assertEqual([i["row"] for i in items], [],
                         "a chain that continues below a CANCELLED row was "
                         "billed as unanswered — this is the rebind shape")

    def test_a_cancelled_DEAD_END_child_leaves_the_debt_standing(self):
        """The other side of the same coin, and the reason the cure is not
        simply 'ignore cancelled rows'. A successor that was cancelled and
        never replaced carries nothing: the FIX is still owed."""
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a"), row("b", supersedes="a", status="cancelled")), None)
        self.assertEqual([i["row"] for i in items], ["a"],
                         "a cancelled dead end discharged a debt nobody took")

    def test_a_FORK_is_ONE_defective_chain_and_not_two_debts(self):
        items, forks, _u = obligation.unanswered_fixes(
            snap(row("a"), row("b", supersedes="a"), row("c", supersedes="a")),
            None)
        self.assertEqual(len(forks), 1, "two live branches did not read as a fork")
        self.assertEqual(forks[0]["row"], "a")
        self.assertEqual(forks[0]["branches"], ["b", "c"])
        self.assertEqual([i["row"] for i in items], [],
                         "both fork branches were ALSO billed as debts — one "
                         "broken chain became two people fixing one lane")

    def test_a_branch_alive_only_BELOW_still_counts_as_a_branch(self):
        """The correction that moved the live-fork count from 49 to 55: a
        branch whose head is cancelled but whose chain continues is a live
        branch. Testing the head alone hides a real double-track behind a
        rebind."""
        _i, forks, _u = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a"),                       # live head
                 row("c", supersedes="a", status="cancelled"),   # cancelled...
                 row("d", supersedes="c", status="open", polarity=None)),
            None)
        self.assertEqual(len(forks), 1,
                         "a branch that is alive only below its head was read "
                         "as dead, hiding the fork")
        self.assertEqual(forks[0]["branches"], ["b", "c"])

    def test_a_non_FIX_verdict_is_not_a_debt(self):
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a", polarity="approve")), None)
        self.assertEqual(items, [], "an APPROVE was billed as a cure to write")

    def test_an_ABSENT_polarity_is_UNKNOWN_and_never_silently_dropped(self):
        """THE 26-ROW HOLE, and it is the same fail-open one layer up.

        The first draft filtered with `polarity != "fix"`, which reads a
        MISSING field as a decided non-fix and drops the row. Measured on the
        live ledger: 26 verdicts that never recorded a polarity, invisible to a
        burn-down whose entire job is completeness — and the dispatch layer
        already knows they exist, reporting its 28 historical ones in their own
        bucket rather than guessing.

        THE OTHER DIRECTION IS ALSO WRONG, which is why this is a third state
        and not a widened filter: nobody can say a cure is owed on a verdict
        that never declared one, so counting them as debts invents work exactly
        as dropping them hides it. The arm pins BOTH — present, and NOT in the
        fix bucket."""
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a", polarity=None)), None)
        self.assertEqual([i["row"] for i in items], ["a"],
                         "a verdict with no declared polarity vanished — the "
                         "absent field was read as a decided non-fix")
        self.assertEqual(items[0]["kind"], obligation.UNDECLARED_VERDICT,
                         "an undeclared verdict was counted as an unanswered "
                         "FIX, which invents a debt nobody can act on")
        self.assertIn("no polarity", items[0]["what"].lower())

    def test_an_undeclared_verdict_that_WAS_answered_is_not_reported(self):
        """The pole for the arm above: the undeclared bucket is subject to the
        same answered/forked exclusions as the fix bucket, so a chain that
        moved on does not linger in it forever."""
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a", polarity=None),
                 row("b", supersedes="a", status="open", polarity=None)), None)
        self.assertEqual(items, [],
                         "an undeclared verdict with a live successor was "
                         "still reported — the bucket skipped the exclusions")

    def test_an_UNREADABLE_ledger_is_UNKNOWN_and_never_a_clean_burn_down(self):
        """The fail-open this whole family of readers exists to stop, and here
        it is at its most dangerous: [] does not mean 'nothing is owed', it
        means 'I could not look' — and on a burn-down that reads as done."""
        items, forks, un = obligation.unanswered_fixes(None, "ledger unreadable")
        self.assertEqual(un, "ledger unreadable")
        self.assertEqual((items, forks), ([], []))

    def test_the_sentence_NAMES_the_row_to_supersede(self):
        """The remedy rides with the diagnosis. An author told only that
        something is unanswered still has to work out that the answer is a
        re-dispatch, which is the exact step nothing in the loop mentions."""
        items, _f, _u = obligation.unanswered_fixes(snap(row("abcdef123456")), None)
        what = items[0]["what"]
        self.assertIn("--supersedes abcdef123456", what)
        self.assertIn("re-dispatch", what)

    def test_ONE_CHAIN_collapses_but_UNRELATED_work_never_does(self):
        """THIS ARM ASSERTED THE DEFECT AND A REVIEWER OVERTURNED IT.

        It built two rows sharing lane="dup", called them one lane, and pinned
        the collapse as correct. But helm's own reference says a lane is FREE
        TEXT and may be reused by unrelated `--new-work`; CHAIN ROOT is work
        identity. So keying on the label merged two independent FIX verdicts
        into one line and one count, and a real debt vanished from the default
        screen — with my test holding the door open.

        Both halves are pinned now, because either alone is satisfiable by a
        wrong reader: a dedupe that never collapses passes the second, and one
        that always collapses passes the first."""
        # SAME LABEL, DIFFERENT WORK -> both survive
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a", lane="dup", ts="2026-08-01T00:00:00Z"),
                 row("b", lane="dup", ts="2026-08-02T00:00:00Z")), None)
        for i in items:                      # unrelated chains root themselves
            i["chain_root"] = i["row"]
        kept = obligation.unanswered_lanes(items)
        self.assertEqual([i["row"] for i in kept], ["a", "b"],
                         "two unrelated chains sharing a free-text label were "
                         "merged; one debt disappeared from the burn-down")
        # SAME CHAIN, TWO ROWS -> one line
        items2, _f, _u = obligation.unanswered_fixes(
            snap(row("c", lane="same", ts="2026-08-01T00:00:00Z"),
                 row("d", lane="same", ts="2026-08-02T00:00:00Z")), None)
        for i in items2:
            i["chain_root"] = "R"            # one work identity
        self.assertEqual([i["row"] for i in obligation.unanswered_lanes(items2)],
                         ["c"],
                         "one chain reported twice — the count triple-counts "
                         "its worst-maintained chains again")

    def test_a_FIX_DEEP_under_a_forked_branch_is_not_ALSO_billed(self):
        """A reviewer's hole, and it made one defective chain into a fork AND a
        debt. A forks to B and C; B is cancelled and continues to D; D is a FIX
        with no child. Excluding only the branch HEADS left D neither answered
        nor excluded, so the surface reported the fork and then billed a row
        inside it — contradicting its own claim that branches are excluded."""
        items, forks, _u = obligation.unanswered_fixes(
            snap(row("A"),
                 row("B", supersedes="A", status="cancelled"),
                 row("C", supersedes="A"),
                 row("D", supersedes="B")), None)
        self.assertEqual(len(forks), 1, "the fork itself stopped being reported")
        self.assertEqual([i["row"] for i in items], [],
                         "a FIX inside a forked subtree was billed as an "
                         "ordinary debt as well as reported as a fork")


class ParkedLaneSignalTests(unittest.TestCase):
    """A PARKED tip and a CURED tip look identical on the burn-down.

    A lane's dispatch row says a FIX is unanswered; it cannot say whether the
    room's HEAD is the author's cure or an interrupted edit the lease actuator
    rescued mid-flight. A seat hit exactly this: two of his lanes read the same
    on the screen, one was three days of finished work and one was a WIP commit
    by helm-work@local. Dispatching the second would have spent a reviewer's
    turn discovering what the author already knew.
    """

    def setUp(self):
        import shutil
        import subprocess
        self.tmp = tempfile.mkdtemp(prefix="helm-parked-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        self.wt = self.root + "-wt"
        os.makedirs(self.wt)
        self.git = shutil.which("git")
        if not self.git:
            raise unittest.SkipTest("git not available")
        self.subprocess = subprocess

    def _lane(self, name, author, subject):
        """A room whose HEAD carries exactly the author and subject given."""
        path = os.path.join(self.wt, name)
        os.makedirs(path)
        run = lambda *a: self.subprocess.run([self.git, "-C", path] + list(a),
                                             capture_output=True, text=True)
        run("init", "-q")
        run("config", "user.email", author)
        run("config", "user.name", "fixture")
        with open(os.path.join(path, "f.txt"), "w") as fh:
            fh.write("x")
        run("add", "-A")
        run("commit", "-q", "-m", subject)
        return path

    def test_an_ACTUATOR_wip_tip_fires_BOTH_signals_and_names_them(self):
        self._lane("parked", "helm-work@local", "wip: rescued mid-edit")
        signals, unreadable = obligation.parked_signals("parked", self.root)
        self.assertIsNone(unreadable)
        self.assertEqual(sorted(signals), ["actuator-authored", "wip-subject"],
                         "a parked tip did not name the evidence — the author "
                         "cannot tell a park from a rescue without it")

    def test_an_ORDINARY_authored_tip_fires_NOTHING(self):
        """The pole. A detector that flags everything tells nobody anything,
        and this one shares a line with a real debt."""
        self._lane("cured", "someone@example.com", "fix(x): a real cure")
        signals, unreadable = obligation.parked_signals("cured", self.root)
        self.assertEqual((signals, unreadable), ((), None))

    def test_EITHER_signal_alone_is_enough(self):
        """The OR, and why it is not an AND: over-flagging costs the author a
        glance, under-flagging costs a reviewer a whole turn. The two
        predicates are independent and a tip can carry one without the other."""
        self._lane("actor-only", "helm-work@local", "fix(x): looks finished")
        self._lane("wip-only", "someone@example.com", "wip: half an edit")
        self.assertEqual(obligation.parked_signals("actor-only", self.root)[0],
                         ("actuator-authored",))
        self.assertEqual(obligation.parked_signals("wip-only", self.root)[0],
                         ("wip-subject",))

    def test_a_lane_with_NO_ROOM_says_nothing_rather_than_doubting(self):
        """THE NOISE FIX, found by running it rather than by reasoning.

        PARKED is a property of a LIVE room. A lane whose room was retired has
        no such state, so there is nothing to doubt. The first draft returned
        "unreadable" here and, on the live burn-down, 72 of 80 listed lanes
        have no room — the caveat fired on ninety percent of the screen and
        buried the ONE lane that was genuinely parked. A doubt printed on every
        row is not honesty; it hides the signal beside it."""
        signals, unreadable = obligation.parked_signals("never-existed",
                                                        self.root)
        self.assertEqual(signals, ())
        self.assertIsNone(unreadable,
                          "a retired room reported a doubt — on the live "
                          "estate that is most rows, and it buries the "
                          "one that matters")

    def test_a_room_that_is_NOT_A_REPO_is_UNREADABLE_not_clear(self):
        """The state that DOES deserve a doubt, and the reason the previous arm
        is not simply fail-open: a room exists and git will not answer for it.
        That is a lane nobody can classify, and saying so is the third
        answer."""
        os.makedirs(os.path.join(self.wt, "not-a-repo"))
        signals, unreadable = obligation.parked_signals("not-a-repo", self.root)
        self.assertEqual(signals, ())
        self.assertIsNotNone(unreadable,
                             "a room git cannot read was reported as ordinary "
                             "authored work")


class OwedSurfaceTests(unittest.TestCase):
    """THE SCREEN'S OWN CLAIMS, which had no arms at all until a reviewer said
    so — and both owner-facing contradictions lived exactly there.

    The derivation was tested from the first commit; the SENTENCES it prints
    were not, so the headline could assert CURED while the module's own
    docstring said the opposite, and an all-clear could print immediately above
    its own refutation. A surface is where a reader forms the belief, so it is
    the surface that owes the arm.
    """

    def _run(self, items, forks=(), unavailable=None, args=()):
        import contextlib
        import io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(obligation, "unanswered_fixes",
                               return_value=(list(items), list(forks),
                                             unavailable)), \
                mock.patch.object(obligation, "_repo_root", return_value=None), \
                contextlib.redirect_stdout(out):
            rc = obligation.cmd_owed(list(args))
        return rc, out.getvalue()

    def _fix(self, row="a", lane="lane-x", seat="author-seat"):
        return {"kind": obligation.UNANSWERED_FIX, "row": row, "lane": lane,
                "owed_seat": seat, "owed_since": "2026-08-10T00:00:00Z",
                "chain_root": row, "what": "…"}

    def _undeclared(self, row="u", lane="lane-u"):
        return {"kind": obligation.UNDECLARED_VERDICT, "row": row, "lane": lane,
                "owed_seat": "author-seat", "owed_since": "2026-08-10T00:00:00Z",
                "chain_root": row, "what": "…"}

    def test_each_row_is_placed_against_ITS_OWN_repo_and_resolved_ONCE(self):
        """THE INTEGRATION SEAM, which a review called unpinned — and it was:
        every existing arm tested the helper in isolation, so the wiring could
        have handed every row the same root and stayed green.

        Two claims, both at the seam rather than in the helper: each row's
        signals are read from the root derived from THAT row's repo_id (a
        cross-wire here re-creates the cross-repo bug the whole blocker is
        about, one layer up), and a repo is resolved ONCE however many rows
        name it — resolution now spawns git, so a per-ROW call is a fleet-wide
        cost regression that no unit test of the helper can see.
        """
        from unittest import mock
        seen, resolved = [], []

        def fake_root(repo_id):
            resolved.append(repo_id)
            return "/root-for/" + str(repo_id)

        def fake_signals(lane, root=None):
            seen.append((lane, root))
            return (), None

        rows = [self._fix(row="a", lane="lane-a"),
                self._fix(row="b", lane="lane-b"),
                self._fix(row="c", lane="lane-c")]
        rows[0]["repo_id"] = "/repo/one/.git"
        rows[1]["repo_id"] = "/repo/two/.git"
        rows[2]["repo_id"] = "/repo/one/.git"     # same repo as row a

        with mock.patch.object(obligation, "_root_for_repo", fake_root), \
                mock.patch.object(obligation, "parked_signals", fake_signals):
            self._run(rows)

        self.assertEqual(
            dict(seen),
            {"lane-a": "/root-for//repo/one/.git",
             "lane-b": "/root-for//repo/two/.git",
             "lane-c": "/root-for//repo/one/.git"},
            "a row was placed against a repository it does not name")
        self.assertEqual(
            sorted(resolved), ["/repo/one/.git", "/repo/two/.git"],
            "resolution is not cached per repo — %d calls for 2 repos"
            % len(resolved))

    def test_the_headline_does_NOT_assert_CURED(self):
        """The derivation CANNOT know it. A no-successor FIX is either uncured
        or cured-and-not-dispatched, and only tip evidence separates them — so
        a headline saying "cured-but-unreviewed" states as fact the one thing
        the reader is being asked to determine."""
        _rc, out = self._run([self._fix()])
        self.assertIn("UNANSWERED FIX", out)
        self.assertNotIn("cured-but-unreviewed", out,
                         "the screen asserted CURED, which this module's own "
                         "docstring says it cannot know")

    def test_BOTH_author_paths_are_printed_not_just_the_cured_one(self):
        """An author who has NOT cured was told to dispatch "the cured tip",
        which spends a reviewer's turn on an unchanged artifact."""
        _rc, out = self._run([self._fix()])
        self.assertIn("not cured yet", out)
        self.assertIn("already cured", out)

    def test_the_ALL_CLEAR_is_WITHHELD_while_undeclared_verdicts_stand(self):
        """The false all-clear printed directly above its own refutation: "every
        cure has been re-dispatched", and then a list of verdicts whose polarity
        nobody recorded and which MAY be FIX."""
        _rc, out = self._run([self._undeclared()])
        self.assertIn("no unanswered DECLARED-FIX", out,
                      "the clearance did not say DECLARED, so it claimed more "
                      "than the derivation knows")
        self.assertNotIn("every cure", out,
                         "an all-clear printed while undeclared verdicts were "
                         "outstanding — the screen refutes itself")
        self.assertIn("NO DECLARED POLARITY", out)

    def test_a_TRUE_all_clear_still_reads_as_one(self):
        """The pole. Withholding the clearance in every case would make the
        screen useless — a seat that genuinely owes nothing must be told so."""
        _rc, out = self._run([])
        self.assertIn("no unanswered DECLARED-FIX", out)
        self.assertIn("every cure", out,
                      "a genuinely clear board withheld its clearance")

    def test_an_UNREADABLE_ledger_exits_1_and_says_UNKNOWN(self):
        rc, out = self._run([], unavailable="ledger unreadable")
        self.assertEqual(rc, 1, "an unreadable ledger exited 0 — a burn-down "
                                "that cannot be read must not look empty")
        self.assertIn("UNKNOWN", out)


class DeliveryReaderReadsALISTTest(unittest.TestCase):
    """Review blocker 1 of 6, and the trigger is the interesting part.

    `delivery_of` read the event ledger as a MAP from obligation id to events
    and called `.get()` on it. `eventledger.checked_events` returns a LIST —
    its docstring said "dict", which is what I trusted when I wrote the caller,
    and its code says `out = []` then `out.append(row)` on every path.

    `(events or {}).get(...)` is TOTAL on an empty ledger, because `[]` is
    falsy and the fallback dict takes over. It raises AttributeError on the
    FIRST REAL EVENT, because a non-empty list is truthy and lists have no
    `.get`. So the defect could only appear once the feature began to work,
    which is why every arm on this lane was green: they all ran against a
    ledger with nothing in it.
    """

    def setUp(self):
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="helm-delivery-list-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for k in ("HELM_HOME", "HELM_CHAT_DIR"):
            self._set(k, self.tmp)
        self._set("HELM_CHAT_NAME", "zz-delivery-test")
        self.path = obligation.delivery_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _set(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, old)
                        if old is not None else os.environ.pop(key, None))

    def _write(self, *rows):
        import json
        with open(self.path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_the_reader_survives_its_FIRST_real_event(self):
        """THE ARM THAT GOES RED ON THE REVIEWED TIP. One valid delivery event
        is all it takes; the reviewed code raises AttributeError here."""
        self._write({"v": 1, "event": "delivery-attempt", "id": "ob-1",
                     "ok": True})
        state, detail = obligation.delivery_of("ob-1")
        self.assertEqual(detail.get("attempts"), 1,
                         "the reader did not see the one event in the ledger")
        self.assertNotEqual(state, obligation.NEVER_DELIVERED)

    def test_an_EMPTY_ledger_is_the_state_that_hid_it(self):
        """Kept as the control, and named for what it is: this case passed on
        the broken code too, so its greenness proves the reader runs — never
        that it reads."""
        self._write()
        state, detail = obligation.delivery_of("ob-1")
        # CONTROL BEFORE THE ABSENCE: prove the reader returned a real
        # answer at all. Every assertion below is satisfied by a reader that
        # fell over and returned nothing shaped like a verdict.
        self.assertIn("attempts", detail)
        self.assertEqual(state, obligation.NEVER_DELIVERED)
        self.assertEqual(detail.get("attempts"), 0)

    def test_another_obligations_event_is_not_billed_to_this_one(self):
        """The discriminator a map-shaped read got for free and a list-shaped
        read must do explicitly. Without the id filter every obligation would
        inherit every other obligation's delivery history."""
        self._write({"v": 1, "event": "delivery-attempt", "id": "ob-OTHER",
                     "ok": True})
        state, detail = obligation.delivery_of("ob-1")
        self.assertIn("attempts", detail)          # same control, same reason
        self.assertEqual(detail.get("attempts"), 0,
                         "an event for ob-OTHER was billed to ob-1")
        self.assertEqual(state, obligation.NEVER_DELIVERED)


class AForeignEdgeCannotManufactureAForkTest(unittest.TestCase):
    """Review blocker 6 of 6, and it is worse than the inflated count it looks like.

    `kids` is built from RAW `supersedes` edges. That field is an edge any
    writer can put on any row, so a --new-work row naming this parent counted
    as one of its branches. Two branches means FORKED, and a forked parent is
    EXCLUDED FROM BILLING — so one stranger's edge both invents a fork and
    HIDES A REAL OBLIGATION. The row disappears from the burn-down entirely.

    The cure asks `_same_chain`, which is the same predicate `carrier` already
    refuses foreign successors with — so the fork question and the debt question
    are finally answered by one authority instead of two.

    LIVE IMPACT TODAY IS ZERO, measured by toggling that one predicate against a
    live snapshot: 56 forks with it, 56 without. This is a correctness fix
    against a future misreport, not a change to any current number, and the
    class deserves an arm precisely because nothing on the ledger exercises it.
    """

    def test_a_legitimate_child_alone_forks_nothing(self):
        # `q` IS THE POSITIVE CONTROL: an unrelated unanswered FIX, so the
        # reader must produce a NON-EMPTY answer. Without it both channels are
        # empty and a reader that returned nothing at all would pass.
        items, forks, _ = obligation.unanswered_fixes(
            snap(row("a"), row("b", supersedes="a", status="open",
                               chain_root="a"),
                 row("q", lane="lane-q")), None)
        self.assertEqual([i["row"] for i in items], ["q"],
                         "the reader produced nothing, so the emptiness below "
                         "is about the reader rather than about the fork")
        self.assertEqual(forks, [])

    def test_a_foreign_child_alone_forks_nothing_and_takes_nothing(self):
        items, forks, _ = obligation.unanswered_fixes(
            snap(row("a"), row("z", supersedes="a", status="open",
                               chain_root="z")), None)
        self.assertEqual(forks, [])
        self.assertEqual([i["row"] for i in items], ["a"],
                         "a stranger's row answered a debt it never took")

    def test_a_foreign_child_BESIDE_a_legitimate_one_is_not_a_fork(self):
        """THE ARM THAT GOES RED ON THE PRE-CURE READER. Three rows, and the
        failure is silent in both directions: forks reports 1 that does not
        exist, and row `a` vanishes from the burn-down because forked parents
        are excluded."""
        items, forks, _ = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a", status="open", chain_root="a"),
                 row("z", supersedes="a", status="open", chain_root="z"),
                 row("q", lane="lane-q")), None)
        # POSITIVE CONTROL FIRST — see the sibling arm above.
        self.assertEqual([i["row"] for i in items], ["q"],
                         "the legitimate child b took a's debt and q remains "
                         "owed; `a` must not be hidden by a phantom fork, and "
                         "an empty answer here would prove nothing")
        self.assertEqual(forks, [],
                         "a foreign edge manufactured a fork: %r" % (forks,))

    def test_a_REAL_fork_is_still_reported(self):
        """THE POLE, and without it every arm above is satisfied by a reader
        that simply stopped detecting forks. Two children on the SAME chain is
        a genuine fork and must survive the filter."""
        items, forks, _ = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a", status="open", chain_root="a"),
                 row("c", supersedes="a", status="open", chain_root="a")), None)
        self.assertEqual(len(forks), 1,
                         "the cure disabled fork detection instead of "
                         "narrowing it: %r" % (forks,))
        self.assertEqual(sorted(forks[0]["branches"]), ["b", "c"])

    def test_an_UNREADABLE_parent_forks_nothing(self):
        """The first escape from the half-cure. Admitting every child
        when the parent row is absent LOOKS like being permissive with missing
        data and is the opposite: two rows naming one NONEXISTENT parent then
        read as a fork of work that does not exist. There is no chain identity
        to compare against, so there is no branch set."""
        items, forks, _ = obligation.unanswered_fixes(
            snap(row("m", supersedes="ghost", status="open", chain_root="ghost"),
                 row("n", supersedes="ghost", status="open", chain_root="ghost"),
                 row("q", lane="lane-q")), None)
        self.assertEqual([i["row"] for i in items], ["q"],
                         "positive control: the reader produced a real answer")
        self.assertEqual(forks, [],
                         "two children of a parent that does not exist were "
                         "reported as a fork: %r" % (forks,))

    def test_a_foreign_row_BENEATH_a_real_fork_keeps_its_own_debt(self):
        """The second escape, and the subtler one. Filtering the
        IMMEDIATE children while walking their descendants RAW leaves the defect
        one level down: a foreign row beneath either branch entered
        forked_branches and was then excluded from billing, so a stranger's edge
        still hid an unrelated FIX debt — it just needed one more hop.

        The fork itself must SURVIVE this fix, which is what the first assertion
        pins: narrowing the descendant walk must not stop reporting the fork.
        """
        items, forks, _ = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a", status="open", chain_root="a"),
                 row("c", supersedes="a", status="open", chain_root="a"),
                 row("q", supersedes="b", chain_root="q")), None)
        self.assertEqual(len(forks), 1,
                         "the REAL fork a{b,c} stopped being reported: %r"
                         % (forks,))
        self.assertIn("q", [i["row"] for i in items],
                      "a foreign FIX one hop below a fork branch was swallowed "
                      "by the descendant walk and lost its own debt")


class ARowIsPlacedInItsOwnRepositoryTest(unittest.TestCase):
    """Review blocker 5: one cwd-derived root over a GLOBAL ledger.

    `parked_signals` reached for this process's checkout whenever the caller
    passed nothing, and `helm owed` passed ONE root for every row. The ledger is
    global — measured on the live estate, 6 of 119 items name a repository other
    than helm (a sibling project, CLIProxyAPI, a proxy fork, a tmp dogfood repo) — so
    those rows were classified against HELM's worktrees and answered CONFIDENTLY
    AND WRONGLY, which is the direction that mints false PARKED/resume hints
    rather than honest unknowns.

    My own first measurement of this was wrong and is worth recording: I counted
    rows with NO repo_id (6 fleet-wide) and concluded the objection did not
    materialise. That is the ABSENT set. The population the blocker is about is
    rows scoped to ANOTHER repo, which is a different set entirely.
    """

    def test_a_row_naming_no_repository_is_UNREADABLE_not_guessed(self):
        signals, unreadable = obligation.parked_signals("some-lane", None)
        self.assertEqual(signals, ())
        self.assertIn("names no repository", unreadable or "",
                      "a row we cannot place was placed anyway: %r" % unreadable)

    def test_a_row_WITH_a_repository_still_resolves(self):  # noqa: VACUOUS_ASSERTION — unreadable-is-None IS the product law for a placeable row; signals is asserted a real tuple on the same result
        """THE POLE, on the boundary rather than in the middle: refusing the
        absent case must not refuse the present one. A `parked_signals` that
        returned unreadable for everything satisfies the arm above."""
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="oblig-repo-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        signals, unreadable = obligation.parked_signals("no-such-lane", tmp)
        # POSITIVE CONTROL ON THE SAME RESULT: prove the call returned a real
        # signals channel before asserting the other channel is empty.
        self.assertIsInstance(signals, tuple)
        self.assertIsNone(unreadable,
                          "a readable root was reported unplaceable: %r"
                          % unreadable)

    def _a_real_repo(self, sep_gitdir=False):
        """A throwaway checkout. SYNTHETIC PATHS ONLY — the first draft of these
        arms asserted against the operator's real gitdir for a sibling project and the
        never-track guard refused the commit, correctly."""
        import shutil
        import subprocess
        tmp = tempfile.mkdtemp(prefix="oblig-gitdir-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        work = os.path.join(tmp, "work")
        cmd = ["git", "init", "-q"]
        if sep_gitdir:
            cmd += ["--separate-git-dir", os.path.join(tmp, "elsewhere.git")]
        rc = subprocess.run(cmd + [work], capture_output=True).returncode
        if rc != 0:
            self.skipTest("git init unavailable in this environment")
        return work, os.path.join(tmp, "elsewhere.git") if sep_gitdir else \
            os.path.join(work, ".git")

    def test_the_root_comes_from_the_repo_id_not_the_cwd(self):  # noqa: VACUOUS_ASSERTION — the FIRST assertion is the unconditional positive control on this exact observable: a real gitdir resolves to its real checkout, so the Nones below cannot be an always-None helper
        # A REAL repository, because the helper now DERIVES THEN VERIFIES
        # instead of inverting a string. This arm's first draft asserted
        # "/w/some-repo/.git" -> "/w/some-repo" against a path that does not
        # exist, which pinned precisely the string-inversion a review refuted:
        # it passed for a repo shape that was never proven to be a checkout.
        work, gitdir = self._a_real_repo()
        self.assertEqual(obligation._root_for_repo(gitdir), work)
        self.assertIsNone(obligation._root_for_repo(""),
                          "an absent repo_id must yield None, never a fallback")
        self.assertIsNone(obligation._root_for_repo(None))
        self.assertIsNone(
            obligation._root_for_repo("/w/no-such-repo/.git"),
            "a gitdir naming no checkout must be UNKNOWN, not a guessed path")

    def test_a_RELATIVE_repo_id_is_refused_even_when_it_WOULD_resolve(self):
        """A relative id is resolved against the PROCESS CWD, so it
        names a different repository depending on where the caller stands.

        THE ARM IS BUILT SO THE OLD CODE WOULD HAVE PASSED IT. A relative id
        pointing at nothing returns None either way and proves nothing, so this
        chdirs INTO the parent of a real repository and hands over the relative
        form of a checkout that genuinely exists there. Before the guard, every
        Git proof succeeded and the function returned that root — the exact
        cwd-dependent cross-repo placement the lane exists to kill, re-entering
        through the identity rather than through the resolution.
        """
        work, gitdir = self._a_real_repo()
        parent, name = os.path.dirname(work), os.path.basename(work)
        relative = os.path.join(name, ".git")
        here = os.getcwd()
        try:
            os.chdir(parent)
            # CONTROL: the relative path really does resolve to a real repo
            # from here, so a refusal below cannot be absence in disguise.
            self.assertTrue(os.path.isdir(relative),
                            "fixture no longer reproduces the shape: %r does "
                            "not exist from %r" % (relative, parent))
            self.assertIsNone(
                obligation._root_for_repo(relative),
                "a relative repo_id resolved against the process cwd; the "
                "same row now means a different repository per caller")
            # POLE: the ABSOLUTE form of the very same repo still resolves.
            self.assertEqual(obligation._root_for_repo(gitdir), work,
                             "refusing relative ids cost us absolute ones")
        finally:
            os.chdir(here)

    def test_persisted_repo_paths_have_strict_string_and_NUL_symmetry(self):  # noqa: VACUOUS_ASSERTION — the final valid pair resolves positively, proving the four refusals discriminate
        work, gitdir = self._a_real_repo()
        for repo_id, repo_root in (("/w/bad\0name/.git", work),
                                   (b"/w/bytes/.git", work),
                                   (gitdir, "/w/bad\0root"),
                                   (gitdir, b"/w/bytes-root")):
            with self.subTest(repo_id=repo_id, repo_root=repo_root):
                self.assertIsNone(obligation._root_for_repo(repo_id, repo_root))
        self.assertEqual(obligation._root_for_repo(gitdir, work), work)

    def test_an_existing_wrong_checkout_falls_back_to_the_plain_checkout(self):
        work, gitdir = self._a_real_repo()
        wrong, _wrong_gitdir = self._a_real_repo()
        self.assertTrue(os.path.isdir(wrong), "fixture must exercise Git proof")
        self.assertEqual(obligation._root_for_repo(gitdir, wrong), work)

    def test_ambient_GIT_DIR_cannot_make_a_wrong_checkout_verify(self):  # noqa: VACUOUS_ASSERTION — the returned real checkout is the positive identity observable under hostile ambient selection
        work, gitdir = self._a_real_repo()
        wrong, wrong_gitdir = self._a_real_repo()
        from unittest import mock
        with mock.patch.dict(os.environ, {"GIT_DIR": wrong_gitdir}):
            self.assertEqual(obligation._root_for_repo(gitdir, wrong), work)

    def test_a_below_toplevel_carrier_normalizes_to_the_verified_checkout(self):  # noqa: VACUOUS_ASSERTION — the exact top-level return is the positive normalization observable
        work, gitdir = self._a_real_repo()
        below = os.path.join(work, "nested")
        os.makedirs(below)
        self.assertEqual(obligation._root_for_repo(gitdir, below), work)

    def test_a_SEPARATE_GIT_DIR_repo_is_UNKNOWN_rather_than_MISPLACED(self):  # noqa: VACUOUS_ASSERTION — the control is the closing assertEqual: same helper, a DIFFERENT input, because no single argument can yield both None and a root. The rung wants one expression and an input-discriminating refusal cannot have one
        """The finding as an executable case, not a paraphrase.

        A repo_id is a Git COMMON-DIR. Under `--separate-git-dir` (and for a
        submodule) that common-dir does NOT live at `<root>/.git`, so dirname()
        names a directory that is not this repository's checkout — and in a
        nested layout it can name a REAL but WRONG tree, which is how a row
        gets placed against someone else's worktree and reported confidently.

        Refusing is the product law: an unplaceable row owes UNKNOWN.
        """
        work, gitdir = self._a_real_repo(sep_gitdir=True)
        self.assertNotEqual(os.path.dirname(gitdir), work,
                            "this fixture no longer reproduces the shape it "
                            "exists to reproduce — separate-git-dir did not "
                            "separate, so the arm below proves nothing")
        self.assertIsNone(
            obligation._root_for_repo(gitdir),
            "a separate-git-dir repo_id resolved to a path that is not its "
            "checkout; that is the misplacement, printed as fact")
        # THE POLE, on the boundary: a helper that returned None for every
        # input satisfies the assertion above. The MEASURED case must still pay
        # its old price, so a plain checkout built by the same fixture resolves.
        plain_work, plain_gitdir = self._a_real_repo()
        self.assertEqual(obligation._root_for_repo(plain_gitdir), plain_work,
                         "refusing the separable case cost us the ordinary one")


class DeliveryIsIdempotentPerObligationTest(unittest.TestCase):
    """Review blockers 2+3, built to the ruling rather than to my reading.

    "idempotence belongs to obligation_id, before wiring. Under the delivery
    lock, read the LIST once and return the existing successful state without
    another DM."

    WHY THIS IS NOT COSMETIC: a seat already told about an obligation is told
    AGAIN by every sweep that runs, and a second DM about one obligation is
    indistinguishable, at the reader's end, from a second obligation. The
    burn-down's own delivery leg becomes the noise it exists to prevent.
    """

    def _bed(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="deliv-arm-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp, os.path.join(tmp, "deliveries.jsonl")

    def _ob(self, oid="OB-1", seat="some-seat"):
        return {"obligation_id": oid, "owed_seat": seat, "what": "a thing"}

    def test_a_second_deliver_of_ONE_obligation_sends_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the FIRST assertion is the unconditional positive control on this exact observable: sent == [("some-seat", "a thing")], a non-empty literal proving a real DM went out before anything below claims one did not
        from unittest import mock
        _tmp, sidecar = self._bed()
        sent = []

        def dm(seat, body, who=None):
            sent.append((seat, body))
            return ({"id": "row%d" % len(sent)}, None)

        with mock.patch.object(obligation, "delivery_path", lambda: sidecar), \
                mock.patch("helm.seats_delivery.dm", dm):
            first, _d1 = obligation.deliver(self._ob())
            # POSITIVE CONTROL, and it is the first assertion for a reason: if
            # the first delivery does not send, every "did not send" below is
            # satisfied by a function that never sends at all. Asserted as the
            # CONTENT that was sent, not merely a count — a length is one
            # inequality away from being satisfied by the wrong message.
            self.assertEqual(sent, [("some-seat", "a thing")],
                             "the first delivery did not send")
            self.assertEqual(first, obligation.DELIVERED_UNACKED)

            second, detail = obligation.deliver(self._ob())
            self.assertEqual(len(sent), 1,
                             "a second deliver re-sent the DM; the seat is "
                             "told twice about one obligation")
            self.assertEqual(second, obligation.DELIVERED_UNACKED,
                             "the existing successful state was not returned")
            self.assertIs(detail.get("resent"), False)

    def test_a_DIFFERENT_obligation_still_sends(self):
        """THE POLE. Suppressing the repeat must not suppress the estate — an
        idempotence keyed on nothing is a delivery leg that delivers once."""
        from unittest import mock
        _tmp, sidecar = self._bed()
        sent = []

        def dm(seat, body, who=None):
            sent.append((seat, body))
            return ({"id": "row%d" % len(sent)}, None)

        with mock.patch.object(obligation, "delivery_path", lambda: sidecar), \
                mock.patch("helm.seats_delivery.dm", dm):
            obligation.deliver(self._ob(oid="OB-1"))
            obligation.deliver(self._ob(oid="OB-1"))          # suppressed
            obligation.deliver(self._ob(oid="OB-2", seat="other-seat"))
        self.assertEqual(len(sent), 2,
                         "idempotence is keyed on something other than the "
                         "obligation id — a distinct obligation was swallowed")
        self.assertEqual([s for s, _b in sent], ["some-seat", "other-seat"])

    def test_an_UNREADABLE_sidecar_REFUSES_to_send(self):  # noqa: VACUOUS_ASSERTION — the control runs the SAME call against a readable sidecar first and asserts both its returned state and the send, then clears; the refusal is measured against a call proven able to send
        """Cannot-look is not not-yet-delivered. Sending blind on an unreadable
        sidecar re-notifies the whole estate from one bad read — the storm
        delivery_of's own docstring exists to prevent, arriving through the
        writer instead of the reader."""
        from unittest import mock
        tmp, _sidecar = self._bed()
        blocked = os.path.join(tmp, "blocked")
        os.makedirs(blocked)
        sent = []

        def dm(seat, body, who=None):
            sent.append(seat)
            return ({"id": "r"}, None)

        # POSITIVE CONTROL FIRST, on the same observable and the same mock:
        # against a READABLE sidecar this exact call sends. Without it, an
        # assertion that nothing was sent is satisfied by a deliver() that
        # never sends, and the interesting half of this arm is invisible.
        readable = os.path.join(tmp, "readable.jsonl")
        with mock.patch.object(obligation, "delivery_path", lambda: readable), \
                mock.patch("helm.seats_delivery.dm", dm):
            ctrl_state, ctrl_detail = obligation.deliver(self._ob())
        # The control asserts the CALL'S RESULT and not merely the spy: a spy
        # firing proves the mock was wired, never that deliver() reported a
        # delivery. Both are asserted, because either alone is half a control.
        self.assertEqual(ctrl_state, obligation.DELIVERED_UNACKED,
                         "the control call did not report a delivery")
        self.assertIs(ctrl_detail.get("posted"), None)
        self.assertEqual(sent, ["some-seat"],
                         "the control did not send, so the refusal below "
                         "proves nothing about the unreadable case")
        sent.clear()

        os.chmod(blocked, 0o000)
        self.addCleanup(os.chmod, blocked, 0o755)
        with mock.patch.object(obligation, "delivery_path",
                               lambda: os.path.join(blocked, "d.jsonl")), \
                mock.patch("helm.seats_delivery.dm", dm):
            state, detail = obligation.deliver(self._ob())
        if state != obligation.DELIVERY_UNKNOWN:
            self.skipTest("this filesystem let the blocked path be read "
                          "(running as root?); the arm cannot discriminate")
        self.assertEqual(sent, [],
                         "an unreadable sidecar still sent a DM — one bad "
                         "read re-notifies every seat on the estate")
        self.assertIs(detail.get("posted"), False)

    def test_a_MISSING_id_sends_nothing_writes_nothing_and_poisons_nothing(self):
        """The boundary blocker, and the third clause is the whole point.

        An obligation with no id used to send its DM and then append an event
        carrying `id: ""` — append_unlocked validates SIZE, not shape. The next
        strict read rejects that line ("corrupt ledger line 1 is not an object
        with a non-empty id"), which makes the SHARED sidecar UNKNOWN; and
        because deliver() now refuses to send on an unreadable sidecar, one
        malformed caller silently suppresses delivery for the whole estate.

        So the damage was never to the row with the bad id. It was to every row
        AFTER it, through a store they all share — which is why the third
        assertion here (a later VALID obligation still delivers) is the one
        that actually measures the defect.
        """
        from unittest import mock
        _tmp, sidecar = self._bed()
        sent = []

        def dm(seat, body, who=None):
            sent.append(seat)
            return ({"id": "r"}, None)

        no_id = {"owed_seat": "some-seat", "what": "a thing"}
        with mock.patch.object(obligation, "delivery_path", lambda: sidecar), \
                mock.patch("helm.seats_delivery.dm", dm):
            state, detail = obligation.deliver(no_id)
            self.assertEqual(state, obligation.UNDELIVERABLE_UNRESOLVED)
            self.assertEqual(sent, [], "a DM went out for an obligation that "
                                       "could not be recorded against an id")
            self.assertFalse(os.path.exists(sidecar),
                             "an unrecordable delivery still wrote to the "
                             "shared sidecar")
            self.assertIn("no id", detail.get("unresolved_reason") or "")

            # THE CLAUSE THAT MEASURES THE BLAST RADIUS, and it doubles as the
            # unconditional positive control: a VALID obligation delivered
            # afterwards must still work. If the refusal above had written its
            # row, this call would read an unparseable sidecar and refuse.
            later, _d = obligation.deliver(self._ob(oid="OB-OK", seat="s2"))
            self.assertEqual(later, obligation.DELIVERED_UNACKED,
                             "one malformed obligation poisoned the shared "
                             "sidecar for every obligation after it")
            self.assertEqual(sent, ["s2"])

    def test_a_NON_STRING_id_is_refused_the_same_way(self):
        """Coercion invents an identity for a value that never had one — the
        same reason the repo_id guard refuses non-strings rather than str()-ing
        them. `str(5)` is a perfectly good ledger key for a row that has no
        key at all."""
        from unittest import mock
        _tmp, sidecar = self._bed()
        sent = []

        def dm(seat, body, who=None):
            sent.append(seat)
            return ({"id": "r"}, None)

        with mock.patch.object(obligation, "delivery_path", lambda: sidecar), \
                mock.patch("helm.seats_delivery.dm", dm):
            for bad in (5, None, b"OB-1", ["OB-1"], "   "):
                state, _d = obligation.deliver(
                    {"obligation_id": bad, "owed_seat": "s", "what": "x"})
                self.assertEqual(
                    state, obligation.UNDELIVERABLE_UNRESOLVED,
                    "a %r id was accepted as an identity" % (bad,))
            self.assertEqual(sent, [], "a non-string id still sent a DM")
            # POLE on the same observable: a well-formed id still delivers, so
            # the refusals above discriminate rather than blanket-refusing.
            good, _d = obligation.deliver(self._ob(oid="OB-GOOD"))
        self.assertEqual(good, obligation.DELIVERED_UNACKED)
        self.assertEqual(sent, ["some-seat"])


class AConcurredContinuationAnswersTheFixTest(unittest.TestCase):
    """A FIX IS ANSWERED ONCE ITS CURE HAS BEEN READ AND ENDORSED, and a
    standing CONCUR on a row that continues the chain is exactly that.

    MEASURED LIVE (2026-09-25T15:28Z). Chain 9203cacc97f9 ran FIX, then a FIX
    whose reviewer committed the cure (patch tip 2e1263de966), then a CONCUR on
    that exact patch tip. The owed-bot billed the middle FIX as UNANSWERED at
    0h44m and `helm owed` listed the lane as waiting. Seats then re-dispatched
    reviews of a patch its author had already adopted, only to silence the
    bot, and each one joined the single local reader's backlog.

    WHICH CONTINUATIONS ANSWER is one table, so a later change to the rule has
    to edit a row: a CONCUR, an APPROVE and a source-clean hold (held or
    landed) answer the FIX. An OPEN re-dispatch answers the AUTHOR's debt: the
    reviewer owes the next move and the open frontier shows it. A FIX answers
    its parent and bills the lane again on its OWN row, once. A continuation
    that was RETRACTED, WITHDRAWN, STRANDED, EXPIRED or CANCELLED answered
    nothing, so the parent bills. So does a CONCUR that read the FIX's own
    uncured tip, or that records no tip: neither is a read of a cure.

    The fixture gives every row its own `reviewed_tip` ("tip-<id>"), so a
    concur in this table reads a commit other than the FIX's unless its row
    says otherwise."""

    # (the continuation `b` of FIX `a`, its fields, the rows the pass bills)
    SHAPES = (
        ("a standing CONCUR", {"polarity": "concur"}, []),
        ("an APPROVE", {"polarity": "approve"}, []),
        ("a source-clean HOLD", {"status": "held", "polarity": None,
                                 "source_clean_tip": "tip-b"}, []),
        ("a source-clean hold that LANDED",
         {"status": "closed", "polarity": None,
          "close_reason": "source-clean-landed"}, []),
        ("an OPEN re-dispatch with no verdict yet",
         {"status": "open", "polarity": None}, []),
        ("a FIX, the next round", {"polarity": "fix"}, ["b"]),
        ("a RETRACTED concur", {"polarity": "retracted",
                                "verdict_retracted": True,
                                "retracted_polarity": "concur"}, ["a"]),
        ("a concur WITHDRAWN", {"polarity": "concur",
                                "close_reason": "withdrawn"}, ["a"]),
        ("a concur closed STRANDED", {"polarity": "concur",
                                      "close_reason": "stranded"}, ["a"]),
        ("a re-dispatch CANCELLED before any verdict",
         {"status": "cancelled", "polarity": None}, ["a"]),
        ("a concur on a FOREIGN chain", {"polarity": "concur",
                                         "_root": "z"}, ["a"]),
        ("a concur closed EXPIRED", {"polarity": "concur",
                                     "close_reason": "expired"}, ["a"]),
        ("a concur on the FIX's own uncured tip",
         {"polarity": "concur", "reviewed_tip": "tip-a"}, ["a"]),
        ("a concur that records no tip", {"polarity": "concur",
                                          "reviewed_tip": None}, ["a"]),
    )

    def _specimen(self, concur=True):
        rows = [row("r9203"), row("r7f9f", supersedes="r9203")]
        if concur:
            rows.append(row("rc803", supersedes="r7f9f", polarity="concur"))
        return snap(*rows)

    def test_the_specimen_chain_bills_nothing(self):
        # CONTROL FIRST, same call: without the CONCUR the middle FIX IS owed,
        # so the empty answer below is about the CONCUR and not a dead reader.
        items, forks, un = obligation.unanswered_fixes(
            self._specimen(concur=False), None)
        self.assertIsNone(un)
        self.assertEqual([i["row"] for i in items], ["r7f9f"])
        items, forks, un = obligation.unanswered_fixes(self._specimen(), None)
        self.assertIsNone(un)
        self.assertEqual(forks, [])
        self.assertEqual([i["row"] for i in items], [],
                         "a FIX whose cure a continuation CONCURRED on was "
                         "billed as unanswered: the 7f9fdca3c22a DM")

    def test_each_continuation_answers_or_bills_as_its_table_row_says(self):  # noqa: VACUOUS_ASSERTION — the table carries its own positive rows: the FIX, retracted, withdrawn, stranded, cancelled and foreign continuations each bill a NON-empty list through the same assertion, in the same loop
        for label, fields, billed in self.SHAPES:
            with self.subTest(continuation=label):
                b = row("b", supersedes="a")
                b.update(fields)
                items, forks, un = obligation.unanswered_fixes(
                    snap(row("a"), b), None)
                self.assertIsNone(un)
                self.assertEqual(forks, [])
                self.assertEqual(
                    [i["row"] for i in items if i["kind"] ==
                     obligation.UNANSWERED_FIX], billed,
                    "a FIX continued by %s billed the wrong rows" % label)

    def test_a_concur_beyond_a_rebinds_cancelled_head_answers_too(self):
        """`rebind` cancels the old row and opens its replacement BENEATH it,
        so a CONCUR one level below a cancelled head is the ordinary shape of
        a re-dispatched review, not an edge case."""
        dead = row("b", supersedes="a", status="cancelled", polarity=None)
        alone, _f, _u = obligation.unanswered_fixes(snap(row("a"), dead), None)
        self.assertEqual([i["row"] for i in alone], ["a"],
                         "the control is not owed, so the empty answer below "
                         "proves nothing about the CONCUR")
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a"), dead,
                 row("c", supersedes="b", polarity="concur")), None)
        self.assertEqual([i["row"] for i in items], [],
                         "a CONCUR behind a rebind's cancelled head did not "
                         "answer the FIX above it")

    def test_an_UNDECLARED_verdict_a_continuation_concurred_on_is_answered(self):
        """The undeclared bucket asks the same answered question: its remedy
        is to re-dispatch with an explicit polarity, and a continuation that
        recorded a CONCUR did exactly that."""
        alone, _f, _u = obligation.unanswered_fixes(
            snap(row("a", polarity=None)), None)
        self.assertEqual([i["kind"] for i in alone],
                         [obligation.UNDECLARED_VERDICT])
        items, _f, _u = obligation.unanswered_fixes(
            snap(row("a", polarity=None),
                 row("b", supersedes="a", polarity="concur")), None)
        self.assertEqual(items, [],
                         "an undeclared verdict whose continuation concurred "
                         "was still reported")

    def test_an_UNDECLARED_verdict_is_answered_by_a_concur_on_its_own_tip(self):
        """THE TIP RULE IS A FIX's RULE. A FIX names a defect, so a concur on
        the FIX's own tip endorsed the uncured work. An UNDECLARED verdict
        names none: its remedy is a re-dispatch with an explicit polarity on
        the SAME work, so a concur on that very tip is the remedy done."""
        def chain(polarity):
            b = row("b", supersedes="a", polarity="concur")
            b["reviewed_tip"] = "tip-a"
            items, _f, _u = obligation.unanswered_fixes(
                snap(row("a", polarity=polarity), b), None)
            return [i["row"] for i in items]
        # CONTROL: the same concur under a FIX still bills it.
        self.assertEqual(chain("fix"), ["a"])
        self.assertEqual(chain(None), [],
                         "an undeclared verdict re-read on its own tip with a "
                         "CONCUR was still reported, so the owed-bot keeps "
                         "DMing after the remedy is done")

    def test_a_concur_branch_behind_a_cancelled_head_is_a_LIVE_branch(self):
        """A FORK ASKS THE SAME QUESTION: a branch is alive when it, or
        something below it, answered. Two CONCUR branches directly under one
        FIX are a fork; the same two, one of them re-dispatched through a
        rebind, must read the same way, or the fork hides and the parent is
        answered by whichever branch the walk reached first."""
        direct = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a", polarity="concur"),
                 row("c", supersedes="a", polarity="concur")), None)
        self.assertEqual([f["branches"] for f in direct[1]], [["b", "c"]],
                         "the control fork is not reported, so the arm below "
                         "proves nothing about the rebind shape")
        rebound = obligation.unanswered_fixes(
            snap(row("a"),
                 row("b", supersedes="a", polarity="concur"),
                 row("c", supersedes="a", status="cancelled", polarity=None),
                 row("d", supersedes="c", polarity="concur")), None)
        self.assertEqual([f["branches"] for f in rebound[1]], [["b", "c"]],
                         "a CONCUR branch behind a cancelled head was read as "
                         "dead, so the fork was hidden")
        self.assertEqual([i["row"] for i in rebound[0]], [])

    def test_helm_owed_lists_no_lane_for_the_specimen(self):
        """THE SURFACE, through the ledger read `helm owed` really makes."""
        import contextlib
        import io
        import json
        from unittest import mock
        from helm import dispatches

        def owed(ledger):
            out = io.StringIO()
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=(ledger, None)), \
                    mock.patch.object(obligation, "_root_for_repo",
                                      return_value=None), \
                    contextlib.redirect_stdout(out):
                rc = obligation.cmd_owed(["--json"])
            self.assertEqual(rc, 0)
            return [r["row"] for r in json.loads(out.getvalue())["owed"]]

        self.assertEqual(owed(self._specimen(concur=False)), ["r7f9f"],
                         "the control lane is not listed, so the empty list "
                         "below proves nothing")
        self.assertEqual(owed(self._specimen()), [],
                         "`helm owed` still lists a lane whose cure was "
                         "concurred on")


def landed_patch_repo(test, trunk=True):
    """(gitdir, reviewed, patch, stray) in a REAL repository whose trunk
    carries a reviewer's cure.

    `reviewed` is the tip a FIX read, `patch` the cure its reviewer committed
    on top of it, and `refs/remotes/origin/main` points at `patch`: the lane
    LANDED WITH THE CURE, so both tips are on trunk. `stray` is a second cure
    off `reviewed` that never landed. `trunk=False` leaves the trunk ref unset,
    which is the unreadable trunk the reader must fail closed on.

    REAL GIT, NOT A FAKE, because the default reader is the thing owed-bot
    runs every hour and an arm that only ever injects a double proves nothing
    about it. SYNTHETIC PATHS ONLY, and the ambient GIT_* selection is
    stripped so a caller standing in a hook cannot point these commands at
    another repository."""
    import shutil
    import subprocess
    git = shutil.which("git")
    if not git:
        test.skipTest("git not available")
    tmp = tempfile.mkdtemp(prefix="oblig-landed-")
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def run(*args):
        p = subprocess.run(
            [git, "-C", tmp, "-c", "user.name=fixture",
             "-c", "user.email=fixture@example.com",
             "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
            + list(args), capture_output=True, text=True, env=env)
        if p.returncode:
            test.fail("fixture git %s failed: %s" % (args[0], p.stderr))
        return p.stdout.strip()

    run("init", "-q")
    run("commit", "-q", "--allow-empty", "-m", "the reviewed tip")
    reviewed = run("rev-parse", "HEAD")
    run("commit", "-q", "--allow-empty", "-m", "the reviewer's cure")
    patch = run("rev-parse", "HEAD")
    run("checkout", "-q", "--detach", reviewed)
    run("commit", "-q", "--allow-empty", "-m", "a cure that never landed")
    stray = run("rev-parse", "HEAD")
    if trunk:
        run("update-ref", "refs/remotes/origin/main", patch)
    return os.path.join(tmp, ".git"), reviewed, patch, stray


def cured_fix(rid, gitdir, reviewed, patch, **fields):
    """A FIX row whose reviewer named `patch` as the cure of `reviewed`."""
    r = row(rid, **fields)
    r.update(repo_id=gitdir, reviewed_tip=reviewed, patch_tip=patch)
    return r


class AFixAnsweredByItsLandedPatchTest(unittest.TestCase):
    """A FIX WHOSE OWN CURE IS ON TRUNK IS ANSWERED (task/3357).

    MEASURED 2026-09-26T21:45Z: owed-bot DMed one seat hourly about seven FIX
    rows. Each had been carried by a source-clean hold that was cancelled as
    moot once its lane landed, so nothing on the ledger continued the FIX any
    more and it read as unanswered debt again. For four of the seven the
    REVIEWER'S patch tip was already an ancestor of origin/main: the cure the
    FIX asked for had landed. No close door takes such a row — `lr close
    --reason carried` refuses an ancestor tip, `resolved` wants a later
    verdict, and `close-landed` never takes a FIX — so the nag had no end.

    THE LEDGER CANNOT SEE A LAND, so this is the one question the pass asks
    git, through a reader the arms can inject. ONLY A MEASURED YES ANSWERS:
    a reader that says no, cannot say, or raises keeps the FIX listed.

    THE TABLE (task/3357's surface by state), one arm or subTest per cell:
      FIX, own patch on trunk, nothing carrying it   -> not listed
      FIX, own patch NOT on trunk                    -> listed
      FIX, no patch, REVIEWED tip on trunk           -> listed (contrary debt)
      FIX, patch named, reader errs / unknown repo   -> listed (fail closed)
      SUPERSEDE with a patch on trunk                -> unchanged: not listed,
                                                        and git is never asked
      FIX already carried, retired or forked         -> unchanged, never asked
      UNDECLARED polarity with a patch               -> unchanged, never asked
    and two the brief did not name, found in the code:
      a "patch" that IS the reviewed tip             -> listed, never asked
      a patch beside a DESIGN finding                -> listed, never asked
    """

    def owed(self, *rows, reader=None):
        """The FIX rows billed, and every question the reader was asked."""
        asked = []

        def fake(repo_id, sha):
            asked.append((repo_id, sha))
            return reader(repo_id, sha)

        kw = {} if reader is None else {"on_trunk": fake}
        items, _forks, un = obligation.unanswered_fixes(snap(*rows), None,
                                                        **kw)
        self.assertIsNone(un)
        return [(i["row"], i["kind"]) for i in items], asked

    # -- the default reader, against a real repository ----------------------

    def test_a_FIX_whose_own_cure_LANDED_is_answered(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST through the same default reader on the same repository: the identical FIX naming a cure that never landed must bill before the landed cure is asked
        gitdir, reviewed, patch, stray = landed_patch_repo(self)
        # CONTROL FIRST, same reader, same repository: a cure that never
        # landed is still owed, so the empty list below is about the land.
        billed, _ = self.owed(cured_fix("a", gitdir, reviewed, stray))
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)],
                         "the control FIX is not owed, so the answer below "
                         "proves nothing about the landed cure")
        billed, _ = self.owed(cured_fix("a", gitdir, reviewed, patch))
        self.assertEqual(billed, [],
                         "a FIX whose reviewer's cure is an ancestor of "
                         "origin/main is still billed: the hourly owed-bot "
                         "DM of task/3357")

    def test_a_FIX_with_no_patch_whose_REVIEWED_tip_is_on_trunk_stays_owed(self):
        """CONTRARY DEBT. The tip the FIX found defective is on trunk and no
        cure is named: the land went ahead over the FIX, which is the case a
        reader most needs to see, not an answer."""
        gitdir, reviewed, _patch, _stray = landed_patch_repo(self)
        r = cured_fix("a", gitdir, reviewed, None)
        del r["patch_tip"]
        billed, _ = self.owed(r)
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)])

    def test_a_patch_that_IS_the_reviewed_tip_answers_nothing(self):
        """The verdict door proves a patch DESCENDS from the reviewed tip,
        and a commit descends from itself. Such a patch names no cure, and
        reading it as one would retire exactly the contrary debt above."""
        gitdir, reviewed, _patch, _stray = landed_patch_repo(self)
        billed, _ = self.owed(cured_fix("a", gitdir, reviewed, reviewed))
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)])

    def test_an_UNREADABLE_trunk_keeps_the_FIX_listed(self):
        gitdir, reviewed, patch, _stray = landed_patch_repo(self, trunk=False)
        billed, _ = self.owed(cured_fix("a", gitdir, reviewed, patch))
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)],
                         "a repository with no origin/main to read answered "
                         "the FIX: UNKNOWN was spent as a yes")

    def test_an_UNKNOWN_or_RELATIVE_repository_keeps_the_FIX_listed(self):  # noqa: VACUOUS_ASSERTION — every row of the table asserts a NON-empty billed list through the same assertion; the relative form is proven to resolve by the unconditional isdir assertion before the loop
        """A relative repo_id resolves against the PROCESS CWD, so it names a
        different repository wherever the caller stands; the arm chdirs into
        the parent of a real landed repository so the relative form WOULD
        resolve to it."""
        gitdir, reviewed, patch, _stray = landed_patch_repo(self)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(os.path.dirname(os.path.dirname(gitdir)))
        relative = os.path.relpath(gitdir)
        self.assertTrue(os.path.isdir(relative),
                        "the relative repo_id would not resolve, so its row "
                        "below proves nothing about the cwd hazard")
        for label, repo in (("absent", None), ("empty", ""),
                            ("relative", relative),
                            ("no such path", "/nonexistent/oblig/.git"),
                            ("not a string", ["/x/.git"])):
            with self.subTest(repo=label):
                r = cured_fix("a", gitdir, reviewed, patch)
                r["repo_id"] = repo
                billed, _ = self.owed(r)
                self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)],
                                 "a FIX in a repository nobody can place "
                                 "(%s) was answered" % label)

    # -- the injected reader: what it is asked, and when -------------------

    def test_only_a_measured_YES_answers_and_every_other_reading_keeps_it(self):  # noqa: VACUOUS_ASSERTION — the table carries its own positive rows: four of five readings bill a NON-empty list, and every row asserts the reader was asked, through the same assertions in the same loop
        patch = "c" * 40
        r = row("a")
        r.update(repo_id="/r/.git", patch_tip=patch)

        def boom(_repo, _sha):
            raise OSError("git could not be spawned")

        for label, reader, billed in (
                ("yes", lambda _r, _s: True, []),
                ("no", lambda _r, _s: False, [("a", obligation.UNANSWERED_FIX)]),
                ("cannot say", lambda _r, _s: None,
                 [("a", obligation.UNANSWERED_FIX)]),
                ("truthy but not True", lambda _r, _s: "ancestor",
                 [("a", obligation.UNANSWERED_FIX)]),
                ("raises", boom, [("a", obligation.UNANSWERED_FIX)])):
            with self.subTest(reader=label):
                got, asked = self.owed(dict(r), reader=reader)
                self.assertEqual(asked, [("/r/.git", patch)],
                                 "the reader was not asked the row's OWN "
                                 "repository and patch")
                self.assertEqual(got, billed)

    def test_SUPERSEDE_with_a_landed_patch_is_unchanged_and_never_asks(self):
        """A SUPERSEDE was never billed here: its remedy is a replacement,
        not a cure this pass could see land. It stays exactly as it was, and
        it costs no git call. The control is the same row as a FIX, through
        the same reader, which IS asked."""
        yes = lambda _r, _s: True
        r = row("a")
        r.update(repo_id="/r/.git", patch_tip="c" * 40)
        _got, asked = self.owed(dict(r), reader=yes)
        self.assertEqual(len(asked), 1, "the control FIX never reached the "
                                        "reader, so the zero below is empty")
        got, asked = self.owed(dict(r, polarity="supersede"), reader=yes)
        self.assertEqual((got, asked), ([], []))

    def test_an_UNDECLARED_verdict_with_a_landed_patch_is_unchanged(self):
        """An undeclared verdict demanded nothing, so a landed commit cannot
        say what it meant; its remedy is still an explicit polarity or an
        advisory close, and it stays in its own bucket."""
        r = row("a", polarity=None)
        r.update(repo_id="/r/.git", patch_tip="c" * 40)
        got, asked = self.owed(r, reader=lambda _r, _s: True)
        self.assertEqual(got, [("a", obligation.UNDECLARED_VERDICT)])
        self.assertEqual(asked, [])

    def test_a_FIX_already_carried_retired_or_forked_never_asks(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST on the same observables: the same cured FIX alone is billed and its reader asked once, before the settled shapes are asserted to bill and ask nothing
        """The ledger's own answers come first and git is the last question,
        so a row the ledger already settled costs nothing. Each shape is
        pinned unchanged against a reader that would say NO, so a pass that
        consulted it would bill the row."""
        no = lambda _r, _s: False

        def cured(rid="a", **fields):
            r = row(rid, **fields)
            r.update(repo_id="/r/.git", patch_tip="c" * 40)
            return r

        got, asked = self.owed(cured(), reader=no)
        self.assertEqual((got, len(asked)),
                         ([("a", obligation.UNANSWERED_FIX)], 1),
                         "the control FIX was not billed and asked, so the "
                         "empty answers below prove nothing")
        retired = cured()
        retired["discharged"] = True
        for label, rows in (
                ("carried by a live successor",
                 (cured(), row("b", supersedes="a", status="open",
                               polarity=None))),
                ("retired by the ladder", (retired,)),
                ("a branch of a fork",
                 (row("p"), cured("b", supersedes="p"),
                  cured("c", supersedes="p")))):
            with self.subTest(shape=label):
                got, asked = self.owed(*rows, reader=no)
                self.assertEqual(got, [])
                self.assertEqual(asked, [], "git was asked about a row the "
                                            "ledger had already settled")

    def test_a_patch_beside_a_DESIGN_finding_stays_owed_and_never_asks(self):
        """A landed patch answers the MECHANICAL findings it cured. A design
        finding recorded beside it is not answered by any commit — it goes to
        a meld — so the FIX stays owed, and `review_door`'s all-mechanical
        rule (a patch, no design finding, no reason for having no cure) is the
        line this draws too. A patch beside a no-patch reason contradicts
        itself (the verdict door takes one or the other) and fails closed."""
        r = row("a")
        r.update(repo_id="/r/.git", patch_tip="c" * 40,
                 design_findings=["the API shape is wrong"])
        got, asked = self.owed(r, reader=lambda _r, _s: True)
        self.assertEqual(got, [("a", obligation.UNANSWERED_FIX)])
        self.assertEqual(asked, [])
        r = row("a")
        r.update(repo_id="/r/.git", patch_tip="c" * 40,
                 no_patch_because="a design disagreement")
        got, asked = self.owed(r, reader=lambda _r, _s: True)
        self.assertEqual(got, [("a", obligation.UNANSWERED_FIX)])
        self.assertEqual(asked, [])

    def test_a_patch_that_IS_the_reviewed_tip_never_asks(self):
        r = row("a")
        r.update(repo_id="/r/.git", patch_tip="c" * 40, reviewed_tip="c" * 40)
        got, asked = self.owed(r, reader=lambda _r, _s: True)
        self.assertEqual(got, [("a", obligation.UNANSWERED_FIX)])
        self.assertEqual(asked, [])

    def test_ONE_question_per_repository_and_patch_per_pass(self):
        """owed-bot runs this over the whole ledger every hour, so the cost is
        bounded by DISTINCT (repository, patch) pairs, never by rows. Three
        chains naming one landed patch cost one question; a second patch, or
        the same patch in another repository, costs one more each. The memo
        lives for ONE pass: a second pass asks again, because trunk moves."""
        def cured(rid, repo, patch):
            r = row(rid)
            r.update(repo_id=repo, patch_tip=patch)
            return r
        rows = (cured("a", "/r/.git", "c" * 40), cured("b", "/r/.git", "c" * 40),
                cured("d", "/r/.git", "c" * 40), cured("e", "/r/.git", "d" * 40),
                cured("f", "/s/.git", "c" * 40))
        got, asked = self.owed(*rows, reader=lambda _r, _s: False)
        self.assertEqual(len(got), 5, "the control rows are not all billed")
        self.assertEqual(sorted(asked), [("/r/.git", "c" * 40),
                                         ("/r/.git", "d" * 40),
                                         ("/s/.git", "c" * 40)])
        _got, again = self.owed(*rows, reader=lambda _r, _s: False)
        self.assertEqual(len(again), 3, "the memo outlived its pass")

    # -- the surface -------------------------------------------------------

    def test_helm_owed_does_not_list_a_lane_whose_cure_landed(self):
        """THE SURFACE, through the ledger read `helm owed` makes and the
        reader it really uses."""
        import contextlib
        import io
        import json
        from unittest import mock
        from helm import dispatches
        gitdir, reviewed, patch, stray = landed_patch_repo(self)

        def owed(ledger):
            out = io.StringIO()
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=(ledger, None)), \
                    mock.patch.object(obligation, "_root_for_repo",
                                      return_value=None), \
                    contextlib.redirect_stdout(out):
                rc = obligation.cmd_owed(["--json"])
            self.assertEqual(rc, 0)
            return [r["row"] for r in json.loads(out.getvalue())["owed"]]

        self.assertEqual(owed(snap(cured_fix("a", gitdir, reviewed, stray))),
                         ["a"], "the control lane is not listed, so the empty "
                                "list below proves nothing")
        self.assertEqual(owed(snap(cured_fix("a", gitdir, reviewed, patch))),
                         [], "`helm owed` still lists a lane whose reviewer's "
                             "cure is on trunk")


def rebased_cure_repo(test, landed=2):
    """(gitdir, reviewed, patch, first) in a REAL repository whose trunk
    carries a reviewer's two-commit cure REBASED.

    `reviewed` is the lane tip a FIX read, and `first` then `patch` are the
    cure its reviewer committed on top of it. Trunk moved on, took the lane's
    own commit REWORKED (other bytes, so no patch twin), then cherry-picked
    the cure commits under new object ids. That is the shape of both live
    specimens (task/3357): no cure commit is an ancestor of trunk, every cure
    commit has a patch-identical twin there, and the lane's own commit has
    none. `landed=1` cherry-picks `first` alone, which is a cure PARTLY
    landed.

    REAL FILE CONTENT, NEVER --allow-empty. An empty commit is
    patch-identical to every other empty commit, so the instrument refuses
    it (`vcs.landed_state`), and a fixture built of them tests that refusal
    and nothing else."""
    import shutil
    import subprocess
    git = shutil.which("git")
    if not git:
        test.skipTest("git not available")
    tmp = tempfile.mkdtemp(prefix="oblig-rebased-")
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def run(*args):
        p = subprocess.run(
            [git, "-C", tmp, "-c", "user.name=fixture",
             "-c", "user.email=fixture@example.com",
             "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
            + list(args), capture_output=True, text=True, env=env)
        if p.returncode:
            test.fail("fixture git %s failed: %s" % (args[0], p.stderr))
        return p.stdout.strip()

    def commit(name, text, message):
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(text)
        run("add", "-A")
        run("commit", "-q", "-m", message)
        return run("rev-parse", "HEAD")

    run("init", "-q")
    base = commit("base.txt", "base\n", "base")
    reviewed = commit("lane.txt", "the lane's work\n", "the reviewed tip")
    first = commit("cure-one.txt", "the first cure\n", "the first cure commit")
    patch = commit("cure-two.txt", "the second cure\n",
                   "the second cure commit")
    run("checkout", "-q", "--detach", base)
    commit("trunk.txt", "trunk moved on\n", "trunk moved on")
    commit("lane.txt", "the lane's work, reworked when it landed\n",
           "the lane, landed reworked")
    for sha in (first, patch)[:landed]:
        run("cherry-pick", sha)
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    return os.path.join(tmp, ".git"), reviewed, patch, first


class AFixAnsweredByItsRebasedPatchTest(unittest.TestCase):
    """A FIX WHOSE CURE LANDED REBASED IS ANSWERED TOO (task/3357 remainder).

    MEASURED 22:53Z with `helm owed --json`: FIX rows b18ca15245af (lane
    remote-session-relay-3087) and a7c03bddf819 (or-free-is-one-model-class)
    still billed their author. Neither patch tip is an ancestor of trunk, and
    every commit each reviewer added (reviewed_tip..patch_tip) has a
    patch-identical twin on trunk: the cures landed rebased. LAND 374's
    ancestry reader answers that honestly with a no, so the debt never ends.

    PATCH IDENTITY IS THE FALLBACK, AND ONLY A MEASURED NO FROM ANCESTRY
    REACHES IT. An ancestry that cannot say is not a no, and an ancestor
    patch needs no second question.

    THE TABLE (task/3357's surface by state), one arm or subTest per cell:
      rebased patch, every cure commit has a twin  -> not listed
      partly landed (one cure commit has none)      -> listed
      ancestry None                                 -> listed, identity never asked
      identity raises                               -> listed
      an ancestor patch                             -> not listed by ancestry,
                                                       identity never asked
    and three the brief did not name, found in the code:
      an empty reviewed..patch range                -> listed
      a reviewed tip that is not a full sha         -> listed, identity never asked
      the same patch under two reviewed tips        -> two questions, because
                                                       the range is the question
    """

    def owed(self, *rows, ancestry=None, identity=None):
        """The rows billed, and every question each reader was asked."""
        asked, identified = [], []

        def on_trunk(repo_id, sha):
            asked.append((repo_id, sha))
            return ancestry(repo_id, sha)

        def by_identity(repo_id, reviewed, patch):
            identified.append((repo_id, reviewed, patch))
            return identity(repo_id, reviewed, patch)

        kw = {}
        if ancestry is not None:
            kw["on_trunk"] = on_trunk
        if identity is not None:
            kw["by_identity"] = by_identity
        items, _forks, un = obligation.unanswered_fixes(snap(*rows), None, **kw)
        self.assertIsNone(un)
        return [(i["row"], i["kind"]) for i in items], asked, identified

    # -- the default readers, against a real repository --------------------

    def test_a_FIX_whose_cure_LANDED_REBASED_is_answered(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST through the same default readers on the same repository shape: a cure only PARTLY landed must bill before the fully landed one is asked
        gitdir, reviewed, patch, _first = rebased_cure_repo(self, landed=1)
        billed, _, _ = self.owed(cured_fix("a", gitdir, reviewed, patch))
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)],
                         "the control FIX (a cure only partly on trunk) is not "
                         "owed, so the answer below proves nothing")
        gitdir, reviewed, patch, _first = rebased_cure_repo(self)
        billed, _, _ = self.owed(cured_fix("a", gitdir, reviewed, patch))
        self.assertEqual(billed, [],
                         "a FIX whose every cure commit is on origin/main "
                         "under a new sha is still billed: b18ca15245af and "
                         "a7c03bddf819 of task/3357")

    def test_a_cure_only_PARTLY_landed_stays_owed(self):
        """One cure commit has no twin on trunk, so the cure did not land."""
        gitdir, reviewed, patch, _first = rebased_cure_repo(self, landed=1)
        billed, _, _ = self.owed(cured_fix("a", gitdir, reviewed, patch))
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)])

    def test_the_cure_range_alone_is_judged_not_the_whole_lane(self):  # noqa: VACUOUS_ASSERTION — the unconditional git cherry read proves the lane's own commit has NO twin on trunk, so the whole-lane question would say no; the FIX must still be answered
        """The lane's own commit landed REWORKED, so the whole lane is not on
        trunk by any measure. The FIX asked for the CURE, and the cure is.
        Asking the whole lane is what would keep both specimens billed: 3 of
        6 and 3 of 14 of their lane commits read '+' against trunk."""
        import subprocess
        gitdir, reviewed, patch, _first = rebased_cure_repo(self)
        cherry = subprocess.run(
            ["git", "--git-dir", gitdir, "cherry", "refs/remotes/origin/main",
             patch], capture_output=True, text=True,
            env={k: v for k, v in os.environ.items()
                 if not k.startswith("GIT_")})
        self.assertIn("+ " + reviewed, cherry.stdout,
                      "the fixture's lane commit has a twin on trunk, so this "
                      "arm cannot tell the cure range from the whole lane")
        billed, _, _ = self.owed(cured_fix("a", gitdir, reviewed, patch))
        self.assertEqual(billed, [])

    def test_an_EMPTY_cure_range_keeps_the_FIX_listed(self):  # noqa: VACUOUS_ASSERTION — the unconditional control answers the same repository through the same readers: the one-commit range reviewed..first IS answered, so the listing below is about the empty range
        """A "patch" behind the reviewed tip names no cure commit at all, so
        nothing can be measured and the FIX stays owed. The verdict door
        refuses such a patch; a hand-edited or old row can still carry it."""
        gitdir, reviewed, patch, first = rebased_cure_repo(self)
        billed, _, _ = self.owed(cured_fix("a", gitdir, reviewed, first))
        self.assertEqual(billed, [], "the control (a real one-commit cure "
                                     "range) is not answered")
        billed, _, _ = self.owed(cured_fix("a", gitdir, patch, first))
        self.assertEqual(billed, [("a", obligation.UNANSWERED_FIX)],
                         "a patch BEHIND its reviewed tip answered the FIX")

    def test_helm_owed_does_not_list_a_lane_whose_cure_landed_rebased(self):
        """THE SURFACE, through the ledger read `helm owed` makes and the
        readers it really uses."""
        import contextlib
        import io
        import json
        from unittest import mock
        from helm import dispatches
        gitdir, reviewed, patch, _first = rebased_cure_repo(self, landed=1)
        partly = cured_fix("a", gitdir, reviewed, patch)
        gitdir, reviewed, patch, _first = rebased_cure_repo(self)
        landed = cured_fix("a", gitdir, reviewed, patch)

        def owed(ledger):
            out = io.StringIO()
            with mock.patch.object(dispatches, "snapshot",
                                   return_value=(ledger, None)), \
                    contextlib.redirect_stdout(out):
                rc = obligation.cmd_owed(["--json"])
            self.assertEqual(rc, 0)
            return [r["row"] for r in json.loads(out.getvalue())["owed"]]

        self.assertEqual(owed(snap(partly)), ["a"],
                         "the control lane is not listed, so the empty list "
                         "below proves nothing")
        self.assertEqual(owed(snap(landed)), [],
                         "`helm owed` still lists a lane whose reviewer's "
                         "cure is on trunk rebased")

    # -- the injected readers: what each is asked, and when ------------------

    def cured(self, rid="a", reviewed="b" * 40, patch="c" * 40, repo="/r/.git"):
        r = row(rid)
        r.update(repo_id=repo, reviewed_tip=reviewed, patch_tip=patch)
        return r

    def test_only_a_measured_NO_from_ancestry_asks_patch_identity(self):  # noqa: VACUOUS_ASSERTION — the table carries its own positive rows: the ancestry-yes and identity-yes readings bill nothing while four others bill the row, and every row asserts both readers' questions exactly
        """ANCESTRY SAYS NO, OR IT SAYS NOTHING. Only the first is a measured
        absence that a rebased land explains; the second is a blind reader,
        and asking a second instrument to overrule a blind first one would
        answer a FIX nobody measured."""
        fix = [("a", obligation.UNANSWERED_FIX)]
        asked = [("/r/.git", "c" * 40)]
        question = [("/r/.git", "b" * 40, "c" * 40)]

        def boom(*_args):
            raise OSError("git could not be spawned")

        yes = lambda *_a: True
        for label, ancestry, billed, identified in (
                ("an ancestor patch", yes, [], []),
                ("ancestry cannot say", lambda *_a: None, fix, []),
                ("ancestry truthy but not True", lambda *_a: "ancestor",
                 fix, []),
                ("ancestry raises", boom, fix, []),
                ("ancestry says no", lambda *_a: False, [], question)):
            with self.subTest(ancestry=label):
                got, a, i = self.owed(self.cured(), ancestry=ancestry,
                                      identity=yes)
                self.assertEqual(a, asked)
                self.assertEqual(i, identified,
                                 "patch identity was asked when ancestry had "
                                 "not measured a no" if not identified else
                                 "patch identity was not asked after a no")
                self.assertEqual(got, billed)

    def test_only_a_measured_YES_from_patch_identity_answers(self):  # noqa: VACUOUS_ASSERTION — the table carries its own positive row: the identity-yes reading bills nothing while every other reading bills the row, through the same assertions in the same loop
        fix = [("a", obligation.UNANSWERED_FIX)]

        def boom(*_args):
            raise OSError("git cherry could not be spawned")

        for label, identity, billed in (
                ("yes", lambda *_a: True, []),
                ("no", lambda *_a: False, fix),
                ("cannot say", lambda *_a: None, fix),
                ("truthy but not True", lambda *_a: "patch-equivalent", fix),
                ("raises", boom, fix)):
            with self.subTest(identity=label):
                got, _a, i = self.owed(self.cured(),
                                       ancestry=lambda *_a: False,
                                       identity=identity)
                self.assertEqual(i, [("/r/.git", "b" * 40, "c" * 40)])
                self.assertEqual(got, billed)

    def test_a_reviewed_tip_that_is_not_a_full_sha_never_asks_identity(self):  # noqa: VACUOUS_ASSERTION — the unconditional control before the loop runs the same readers on the same row with a full reviewed sha and must reach the identity reader and answer, so each empty identity list in the loop is about the malformed tip
        """The cure is the range reviewed..patch, so a reviewed tip that names
        no commit leaves nothing to measure. The FIX stays owed."""
        got, _a, i = self.owed(self.cured(), ancestry=lambda *_a: False,
                               identity=lambda *_a: True)
        self.assertEqual((got, len(i)), ([], 1),
                         "the control (a full reviewed sha) did not reach "
                         "the identity reader, so the silence below is empty")
        for label, reviewed in (("short", "b" * 12), ("absent", None),
                                ("not hex", "tip-a")):
            with self.subTest(reviewed=label):
                r = self.cured(reviewed=reviewed)
                if reviewed is None:
                    del r["reviewed_tip"]
                got, a, i = self.owed(r, ancestry=lambda *_a: False,
                                      identity=lambda *_a: True)
                self.assertEqual(len(a), 1, "ancestry was not asked, so the "
                                            "silence below is empty")
                self.assertEqual((got, i),
                                 ([("a", obligation.UNANSWERED_FIX)], []))

    def test_ONE_identity_question_per_repository_range_per_pass(self):
        """owed-bot runs this every hour, so patch identity is bounded by
        DISTINCT questions, never by rows: three chains naming one cure cost
        one. The question is the RANGE reviewed..patch, so the same patch
        under another reviewed tip, or in another repository, costs one more.
        The memo lives for ONE pass: trunk moves."""
        rows = (self.cured("a"), self.cured("b"), self.cured("d"),
                self.cured("e", reviewed="e" * 40),
                self.cured("f", repo="/s/.git"))
        no = lambda *_a: False
        got, _a, i = self.owed(*rows, ancestry=no, identity=no)
        self.assertEqual(len(got), 5, "the control rows are not all billed")
        self.assertEqual(sorted(i), [("/r/.git", "b" * 40, "c" * 40),
                                     ("/r/.git", "e" * 40, "c" * 40),
                                     ("/s/.git", "b" * 40, "c" * 40)])
        _got, _a, again = self.owed(*rows, ancestry=no, identity=no)
        self.assertEqual(len(again), 3, "the memo outlived its pass")

    def test_the_ledger_answers_first_and_no_git_is_asked(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST on the same observables: the same cured FIX alone is billed and asks both readers once, before the settled shapes are asserted to ask neither
        """A carried, retired or undeclared row, a SUPERSEDE and a design
        finding never reach either reader, exactly as LAND 374 ruled for
        ancestry: git is the last question."""
        no = lambda *_a: False
        got, a, i = self.owed(self.cured(), ancestry=no, identity=no)
        self.assertEqual((got, len(a), len(i)),
                         ([("a", obligation.UNANSWERED_FIX)], 1, 1),
                         "the control FIX did not reach both readers")
        retired = self.cured()
        retired["discharged"] = True
        design = self.cured()
        design["design_findings"] = ["the API shape is wrong"]
        for label, rows, billed in (
                ("carried", (self.cured(), row("b", supersedes="a",
                                               status="open", polarity=None)),
                 []),
                ("retired", (retired,), []),
                ("supersede", (dict(self.cured(), polarity="supersede"),), []),
                ("design finding", (design,),
                 [("a", obligation.UNANSWERED_FIX)])):
            with self.subTest(shape=label):
                got, a, i = self.owed(*rows, ancestry=no, identity=no)
                self.assertEqual(got, billed)
                self.assertEqual((a, i), ([], []))
