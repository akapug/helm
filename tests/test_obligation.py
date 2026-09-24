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
        open(os.path.join(path, "f.txt"), "w").write("x")
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
