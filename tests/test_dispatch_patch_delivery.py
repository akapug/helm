#!/usr/bin/env python3
"""A reviewer's committed cure is not delivered until the next round's ref
carries it (task/3288).

THE MEASURED CASE. Row 1fb5e123c3d6 was a FIX whose reviewer committed the
cure and named it as `patch_tip`. The author rebased its ORIGINAL two commits
onto trunk, added one arm, and re-dispatched with `--supersedes` at a tip that
lacked the cure (`merge-base --is-ancestor <patch> <ref>` fails). helm took it,
and took a second superseding send at the same tip one row further down the
chain: each re-read would have spent a reader re-finding the defects the cure
already fixed.

THE DOOR. `send` and `add` with `--supersedes` read the chain above the new
row for every FIX that names a patch not declined by name, newest first, and
refuse a ref that does not carry one (by ancestry, patch identity or the
cure's own changed lines), naming the patch and the three ways out: build on
it (rebase onto it, or cherry-pick it), `--decline-patch
PATCH[,PATCH...]=REASON` naming every patch it declines, recorded on the new
row, or a retraction of that FIX. A FIX with no patch, and `--new-work`, are untouched.

ITS OWN MODULE BECAUSE `tests/test_dispatches.py` IS AT ITS BLOB CEILING, and
the module is imported, never its names (see tests/test_dispatch_brief_ref.py).
"""
import json
import os
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import dispatches
from tests import test_dispatches as td

WHY = "the cure moved to its own lane, task/9999"


def decline(*patches):
    """The `--decline-patch` value that declines `patches` BY NAME."""
    return ",".join(p[:12] for p in patches) + "=" + WHY


class PatchDeliveryBase(td.DispatchBase):
    """One lane reviewed at `tip`, the reviewer's cure on top of it, trunk
    moved on, and the lane's ORIGINAL commits rebased onto the moved trunk
    WITHOUT the cure: the measured shape, as real commits.

    No branch is named `lane/*`, so the review-lane door has nothing to say
    and every refusal below is this door's."""

    def setUp(self):
        super().setUp()
        self.git("checkout", "-q", "-b", "work", self.c)
        self.one = self.commit_file("work-one", "one")
        self.tip = self.commit_file("work-two", "two")
        self.git("checkout", "-q", "-b", "cure", self.tip)
        self.patch = self.commit_file("cure", "the reviewer's cure")
        self.git("checkout", "-q", self.main)
        self.moved = self.commit_file("trunk", "trunk moved on")
        self.git("checkout", "-q", "-b", "rebased", self.moved)
        self.git("cherry-pick", self.one, self.tip)
        self.rebased = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.assertNotEqual(
            subprocess_rc(self, "merge-base", "--is-ancestor", self.patch,
                          self.rebased), 0,
            "fixture premise: the rebased lane must lack the cure")

    def reviewed(self, patch=True):
        """The first round: a review at `tip` answered FIX, naming the cure
        when `patch` is true and stating why not otherwise."""
        row = self.add(recipient="seat-b", ref=self.tip, lane="work")
        out, why = dispatches.mark_verdict(
            row["id"], self.tip, "findings", "fix",
            patch_tip=self.patch if patch else None,
            no_patch_because=None if patch else "a design finding")
        self.assertIsNone(why, why)
        self.assertEqual(out.get("patch_tip"), self.patch if patch else None)
        return out

    def send(self, ref, parent, **kw):
        return dispatches.send("seat-b", "work", "re-read the lane", ref,
                               repo=self.repo, supersedes=parent, **kw)

    def ids(self):
        return set(dispatches.snapshot()[0])


def subprocess_rc(case, *args):
    import subprocess
    return subprocess.run(["git", "-C", case.repo, *args],
                          capture_output=True).returncode


class ARefWithoutThePatchIsRefusedTest(PatchDeliveryBase):

    def test_the_measured_case_is_refused_naming_the_patch_and_the_ways_out(self):  # noqa: VACUOUS_ASSERTION — the unchanged ledger is paired with the refusal's exact words on the same call and with the admitted control in the next class
        fix = self.reviewed()
        before = self.ids()
        row, why, sent = self.send(self.rebased, fix["id"])
        self.assertIsNone(row, "a ref without the reviewer's cure was admitted")
        self.assertFalse(sent)
        self.assertEqual(self.ids(), before, "a refused send wrote a row")
        self.assertIn(self.patch[:12], why)
        self.assertIn(fix["id"][:12], why)
        self.assertIn("rebase", why)
        self.assertIn("cherry-pick", why)
        self.assertIn("--decline-patch", why)
        self.assertIn("helm dispatch retract %s" % fix["id"][:12], why)

    def test_the_chain_is_read_not_only_the_row_superseded(self):
        """The second measured send superseded the FIRST re-dispatch, which
        carried no verdict at all; only a read of the chain above it finds
        the cure it still lacks."""
        fix = self.reviewed()
        with mock.patch.object(dispatches, "_undelivered_patch",
                               return_value=(None, None)):
            first, why, _sent = self.send(self.rebased, fix["id"])
        self.assertIsNotNone(first, why)
        second, why, _sent = self.send(self.rebased, first["id"])
        self.assertIsNone(second)
        self.assertIn(self.patch[:12], why)
        self.assertIn(fix["id"][:12], why)

    def test_the_add_door_refuses_the_same_ref(self):
        fix = self.reviewed()
        row, why = dispatches.add("seat-b", "work", ref=self.rebased,
                                  repo=self.repo, supersedes=fix["id"],
                                  notify=False, _reason=True)
        self.assertIsNone(row)
        self.assertIn(self.patch[:12], why)
        self.assertIn("--decline-patch", why)


class ARefThatCarriesThePatchIsAdmittedTest(PatchDeliveryBase):

    def test_the_patch_tip_itself_is_admitted(self):
        fix = self.reviewed()
        row, why, _sent = self.send(self.patch, fix["id"])
        self.assertIsNotNone(row, why)
        self.assertNotIn("declined_patch_tips", self.row(row))

    def test_a_ref_rebased_onto_the_patch_is_admitted(self):
        fix = self.reviewed()
        self.git("checkout", "-q", "-b", "onto", self.patch)
        self.git("rebase", "-q", self.moved)
        self.git("checkout", "-q", self.main)
        onto = self.git("rev-parse", "onto")
        # A REBASE MOVES THE CURE'S SHA TOO, so ancestry says no here and
        # the admission is patch identity's; the next arm keeps the sha.
        self.assertNotEqual(subprocess_rc(self, "merge-base", "--is-ancestor",
                                          self.patch, onto), 0)
        row, why, _sent = self.send(onto, fix["id"])
        self.assertIsNotNone(row, why)

    def test_a_ref_built_on_top_of_the_patch_is_admitted(self):
        fix = self.reviewed()
        self.git("checkout", "-q", "-b", "on-top", self.patch)
        top = self.commit_file("work-three", "an arm on top of the cure")
        self.git("checkout", "-q", self.main)
        row, why, _sent = self.send(top, fix["id"])
        self.assertIsNotNone(row, why)

    def test_the_cure_cherry_picked_onto_the_rebased_lane_is_admitted(self):
        fix = self.reviewed()
        self.git("checkout", "-q", "-b", "picked", self.rebased)
        self.git("cherry-pick", self.patch)
        picked = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        row, why, _sent = self.send(picked, fix["id"])
        self.assertIsNotNone(row, why)

    def test_a_FIX_without_a_patch_tip_is_unaffected(self):
        fix = self.reviewed(patch=False)
        row, why, _sent = self.send(self.rebased, fix["id"])
        self.assertIsNotNone(row, why)

    def test_a_retracted_FIX_no_longer_names_a_patch(self):
        """The existing verdict that withdraws a FIX: its author retracts it,
        and the projection reads RETRACTED, not FIX."""
        fix = self.reviewed()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}):
            _out, why = dispatches.retract(
                fix["id"], "the cure was wrong", "fix", "measured",
                notify=False)
        self.assertIsNone(why, why)
        row, why, _sent = self.send(self.rebased, fix["id"])
        self.assertIsNotNone(row, why)

    def row(self, row):
        return dispatches.snapshot()[0][row["id"]]


class DeclineThePatchTest(PatchDeliveryBase):

    def test_a_decline_is_recorded_on_the_new_row_and_answers_the_chain(self):
        fix = self.reviewed()
        row, why, _sent = self.send(self.rebased, fix["id"],
                                    decline_patch=decline(self.patch))
        self.assertIsNotNone(row, why)
        stored = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(stored["declined_patch_tips"], [self.patch])
        self.assertNotIn("declined_patch_tip", stored)
        self.assertEqual(stored["decline_patch_because"], WHY)
        # THE DECLINE ANSWERED THE CURE ABOVE IT, so the next round down the
        # chain is not asked again.
        again, why, _sent = self.send(self.rebased, row["id"])
        self.assertIsNotNone(again, why)
        self.assertNotIn("declined_patch_tips",
                         dispatches.snapshot()[0][again["id"]])

    def test_a_decline_with_no_patch_to_decline_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop ends on an unconditional EXACT count of the cases swept
        plain = self.reviewed(patch=False)
        cured = self.reviewed()
        cases = (("new work", None, self.rebased),
                 ("a FIX with no patch", plain["id"], self.rebased),
                 ("a ref that already carries the patch", cured["id"],
                  self.patch))
        seen = 0
        for name, parent, ref in cases:
            with self.subTest(case=name):
                before = self.ids()
                row, why, _sent = self.send(
                    ref, parent, decline_patch=decline(self.patch),
                    new_work=parent is None)
                self.assertIsNone(row, name)
                self.assertIn("--decline-patch", why)
                self.assertIn("no patch", why)
                self.assertEqual(self.ids(), before)
            seen += 1
        self.assertEqual(seen, 3, "the case sweep did not run")


class EveryFixRoundBindsItsOwnPatchTest(PatchDeliveryBase):
    """A decline answers the ONE patch it names, never the chain: every FIX
    round's own patch still binds a later round, whatever was declined."""

    def second_cure(self, parent, at):
        """Round 2's reviewer answers FIX with its OWN committed cure, off the
        tip round 2 was sent at."""
        self.git("checkout", "-q", "-b", "cure-two", at)
        patch = self.commit_file("cure-two", "the second reviewer's cure")
        self.git("checkout", "-q", self.main)
        out, why = dispatches.mark_verdict(parent["id"], at, "findings",
                                           "fix", patch_tip=patch)
        self.assertIsNone(why, why)
        self.assertEqual(out.get("patch_tip"), patch)
        return out, patch

    def test_a_declined_round_FIXed_with_its_own_patch_still_binds_it(self):
        """THE REVIEWER'S REPRODUCTION: round 2 declined round 1's cure, its
        reviewer answered FIX with a cure of its own, and round 3 at a ref
        without that cure was admitted, because the walk stopped at round
        2's decline before it asked round 2's own FIX."""
        fix = self.reviewed()
        two, why, _sent = self.send(self.rebased, fix["id"],
                                    decline_patch=decline(self.patch))
        self.assertIsNotNone(two, why)
        _fix_two, patch_two = self.second_cure(two, self.rebased)
        before = self.ids()
        three, why, _sent = self.send(self.rebased, two["id"])
        self.assertIsNone(three, "round 2's own patch was hidden by the "
                                 "decline it recorded of round 1's")
        self.assertEqual(self.ids(), before)
        self.assertIn(patch_two[:12], why)
        self.assertIn(two["id"][:12], why)
        # THE DECLINE STILL ANSWERS THE PATCH IT NAMED: a ref carrying round
        # 2's cure and not round 1's is admitted.
        three, why, _sent = self.send(patch_two, two["id"])
        self.assertIsNotNone(three, why)

    def test_an_older_patch_never_delivered_nor_declined_still_binds(self):
        """Round 2 was admitted without round 1's cure by a path that never
        asked (a legacy row, or an UNKNOWN warning); round 3 carries round 2's
        cure only, and round 1's is still owed."""
        fix = self.reviewed()
        with mock.patch.object(dispatches, "_undelivered_patch",
                               return_value=(None, None)):
            two, why, _sent = self.send(self.rebased, fix["id"])
        self.assertIsNotNone(two, why)
        _fix_two, patch_two = self.second_cure(two, self.rebased)
        three, why, _sent = self.send(patch_two, two["id"])
        self.assertIsNone(three, "round 1's cure was never asked for again")
        self.assertIn(self.patch[:12], why)
        self.assertIn(fix["id"][:12], why)


class ADeclineNamesEveryPatchItAnswersTest(PatchDeliveryBase):
    """A decline answers exactly the patches it names, and a ref lacking any
    OTHER undeclined patch is still refused, naming every one it lacks.

    THE REVIEWER'S REPRODUCTIONS (re-read of task/3288): one decline admitted
    a ref lacking two undeclined patches, recorded only the newer, and the
    older surfaced only a round later."""

    def cure(self, parent, at, name):
        """`parent`'s reviewer answers FIX with a committed cure off `at`."""
        self.git("checkout", "-q", "-b", name, at)
        patch = self.commit_file(name, "the cure of " + name)
        self.git("checkout", "-q", self.main)
        _out, why = dispatches.mark_verdict(parent["id"], at, "findings",
                                            "fix", patch_tip=patch)
        self.assertIsNone(why, why)
        return patch

    def stored(self, row):
        return dispatches.snapshot()[0][row["id"]]

    def assert_refused_naming(self, why, missing, absent=()):
        for fix_id, patch in missing:
            self.assertIn(patch[:12], why)
            self.assertIn(fix_id[:12], why)
        for patch in absent:
            self.assertNotIn(patch[:12], why)
        self.assertIn("--decline-patch %s=REASON" % ",".join(
            p[:12] for _f, p in missing), why)

    def test_three_rounds_a_ref_lacking_two_patches_needs_both_named(self):
        fix = self.reviewed()
        p1 = self.patch
        two, why, _sent = self.send(p1, fix["id"])
        self.assertIsNotNone(two, why)
        p2 = self.cure(two, p1, "cure-p2")
        before = self.ids()
        # Round 3 at a ref lacking P1 and P2: refused, naming BOTH.
        three, why, _sent = self.send(self.rebased, two["id"])
        self.assertIsNone(three)
        self.assert_refused_naming(why, ((two["id"], p2), (fix["id"], p1)))
        # A decline naming P2 alone leaves P1 owed: refused, naming P1.
        three, why, _sent = self.send(self.rebased, two["id"],
                                      decline_patch=decline(p2))
        self.assertIsNone(three, "one decline admitted a ref lacking two "
                                 "undeclined patches")
        self.assertIn(p1[:12], why)
        self.assertIn(fix["id"][:12], why)
        self.assertIn("--decline-patch %s,%s=REASON" % (p2[:12], p1[:12]),
                      why)
        self.assertEqual(self.ids(), before)
        # Naming both admits, and records EXACTLY those two.
        three, why, _sent = self.send(self.rebased, two["id"],
                                      decline_patch=decline(p1, p2))
        self.assertIsNotNone(three, why)
        self.assertEqual(self.stored(three)["declined_patch_tips"], [p2, p1])
        self.assertEqual(self.stored(three)["decline_patch_because"], WHY)
        # Nothing is owed a round late.
        four, why, _sent = self.send(self.rebased, three["id"])
        self.assertIsNotNone(four, why)

    def test_four_rounds_a_decline_of_the_newest_leaves_the_older_owed(self):
        fix = self.reviewed()
        p1 = self.patch
        two, why, _sent = self.send(self.rebased, fix["id"],
                                    decline_patch=decline(p1))
        self.assertIsNotNone(two, why)
        p2 = self.cure(two, self.rebased, "cure-p2")
        three, why, _sent = self.send(p2, two["id"])
        self.assertIsNotNone(three, why)
        p3 = self.cure(three, p2, "cure-p3")
        before = self.ids()
        four, why, _sent = self.send(self.rebased, three["id"])
        self.assertIsNone(four)
        self.assert_refused_naming(why, ((three["id"], p3), (two["id"], p2)),
                                   absent=(p1,))
        four, why, _sent = self.send(self.rebased, three["id"],
                                     decline_patch=decline(p3))
        self.assertIsNone(four, "a decline of P3 admitted a ref lacking P2")
        self.assertIn(p2[:12], why)
        self.assertEqual(self.ids(), before)
        four, why, _sent = self.send(self.rebased, three["id"],
                                     decline_patch=decline(p3, p2))
        self.assertIsNotNone(four, why)
        self.assertEqual(self.stored(four)["declined_patch_tips"], [p3, p2])

    def test_a_decline_naming_a_patch_the_ref_carries_is_refused(self):
        """Declining a delivered patch records a decline that never
        happened, so a decline names only patches the ref lacks."""
        fix = self.reviewed()
        two, why, _sent = self.send(self.patch, fix["id"])
        self.assertIsNotNone(two, why)
        p2 = self.cure(two, self.patch, "cure-p2")
        before = self.ids()
        row, why, _sent = self.send(p2, two["id"],
                                    decline_patch=decline(self.patch))
        self.assertIsNone(row)
        self.assertIn("--decline-patch", why)
        self.assertIn(self.patch[:12], why)
        self.assertEqual(self.ids(), before)

    def test_a_decline_naming_a_sha_that_is_no_patch_is_refused(self):
        fix = self.reviewed()
        before = self.ids()
        row, why, _sent = self.send(self.rebased, fix["id"],
                                    decline_patch=decline(self.patch,
                                                          self.moved))
        self.assertIsNone(row)
        self.assertIn(self.moved[:12], why)
        self.assertEqual(self.ids(), before)

    def test_a_legacy_single_decline_still_answers_its_patch(self):
        """A row recorded before a decline could name several patches
        carries `declined_patch_tip`, one sha, and is read as declining
        exactly that one."""
        fix = self.reviewed()
        two, why, _sent = self.send(self.rebased, fix["id"],
                                    decline_patch=decline(self.patch))
        self.assertIsNotNone(two, why)
        path = dispatches.ledger_path()
        events = [json.loads(line) for line in open(path, encoding="utf-8")
                  if line.strip()]
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                if event.get("declined_patch_tips"):
                    event["declined_patch_tip"] = \
                        event.pop("declined_patch_tips")[0]
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        stored = self.stored(two)
        self.assertEqual(stored.get("declined_patch_tip"), self.patch,
                         "fixture premise: the legacy field folds")
        self.assertNotIn("declined_patch_tips", stored)
        three, why, _sent = self.send(self.rebased, two["id"])
        self.assertIsNotNone(three, why)


class AContentCheckThatRaisesKeepsTheMeasuredNoTest(PatchDeliveryBase):

    def test_a_raising_content_check_refuses_rather_than_warns(self):
        """Ancestry and patch identity both MEASURED no; a bug in the
        content check is not evidence the cure is there, so it must not turn
        that NO into an UNKNOWN that warns and admits."""
        fix = self.reviewed()
        with mock.patch.object(dispatches, "_cure_content_carried",
                               side_effect=RuntimeError("a bug")):
            row, why, _sent = self.send(self.rebased, fix["id"])
        self.assertIsNone(row, "a raising content check admitted the ref")
        self.assertIn(self.patch[:12], why)


class TheHelpStatesTheRuleTest(unittest.TestCase):

    def setUp(self):
        _tmphome.pin_live_seats(self)

    def test_the_dispatch_help_states_the_chain_rule_not_the_old_one(self):
        from helm import cli_help
        text = cli_help._VERB_HELP["dispatch"]
        self.assertNotIn("the newest such FIX", text)
        self.assertIn("every FIX naming a patch not declined by name", text)
        self.assertIn("carried by ancestry, patch identity or the cure's "
                      "changed lines", text)
        self.assertIn("--decline-patch PATCH[,PATCH...]=REASON", text)


class TheCureIsJudgedByItsContentTest(PatchDeliveryBase):
    """A ref carries a cure when the cure's own changed lines are there,
    whatever trunk did to the lines around them.

    Patch identity hashes CONTEXT lines, so a clean cherry-pick onto a trunk
    that edited a neighbouring line gets a new patch id, and a squash never
    had the cure's own. Only a replay of `reviewed..patch` onto the ref can
    say the cure changes nothing there."""

    LINES = ["line %d\n" % n for n in range(1, 21)]

    def write(self, lines, text):
        with open(os.path.join(self.repo, "lines"), "w",
                  encoding="utf-8") as fh:
            fh.write("".join(lines))
        self.git("add", "lines")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def edited(self, *numbers):
        lines = list(self.LINES)
        for n in numbers:
            lines[n - 1] = "line %d, edited\n" % n
        return lines

    def setUp(self):
        super().setUp()
        self.git("checkout", "-q", "-b", "ctx-base", self.c)
        base = self.write(self.LINES, "twenty lines")
        self.lane = self.commit_file("ctx-lane", "the lane's own commit")
        self.git("checkout", "-q", "-b", "ctx-cure", self.lane)
        self.cure_one = self.write(self.edited(10), "cure line 10")
        self.cure_two = self.write(self.edited(10, 5), "cure line 5")
        self.git("checkout", "-q", "-b", "ctx-trunk", base)
        self.trunk = self.write(self.edited(12), "trunk edits line 12")
        self.git("cherry-pick", self.lane)
        self.moved = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)

    def fix(self, patch):
        row = self.add(recipient="seat-b", ref=self.lane, lane="ctx")
        out, why = dispatches.mark_verdict(row["id"], self.lane, "findings",
                                           "fix", patch_tip=patch)
        self.assertIsNone(why, why)
        return out

    def on_moved(self, name, lines=None, picks=()):
        self.git("checkout", "-q", "-b", name, self.moved)
        for pick in picks:
            self.git("cherry-pick", pick)
        if lines is not None:
            self.write(lines, "the cure, squashed into the lane")
        out = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return out

    def state(self, patch, ref):
        from helm import vcs
        return vcs.backend(self.repo).landed_state(self.repo, patch, ref,
                                                   limit=self.lane)

    def test_a_clean_cherry_pick_over_a_changed_context_is_admitted(self):
        """THE REVIEWER'S REPRODUCTION: the cure edits line 10, trunk edits
        line 12, and `git cherry-pick <cure>` applies cleanly."""
        from helm import vcs
        fix = self.fix(self.cure_one)
        picked = self.on_moved("ctx-picked", picks=(self.cure_one,))
        self.assertNotEqual(self.state(self.cure_one, picked),
                            vcs.PATCH_EQUIVALENT,
                            "fixture premise: the pick's patch id differs")
        row, why, _sent = self.send(picked, fix["id"])
        self.assertIsNotNone(row, why)
        self.assertNotIn("declined_patch_tips",
                         dispatches.snapshot()[0][row["id"]])

    def test_a_two_commit_cure_squashed_into_the_lane_is_admitted(self):
        from helm import vcs
        fix = self.fix(self.cure_two)
        squashed = self.on_moved("ctx-squash", self.edited(5, 10, 12))
        self.assertNotEqual(self.state(self.cure_two, squashed),
                            vcs.PATCH_EQUIVALENT)
        row, why, _sent = self.send(squashed, fix["id"])
        self.assertIsNotNone(row, why)

    def test_a_ref_with_part_of_the_cure_or_none_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop ends on an unconditional EXACT count of the cases swept, and the admitted squash of the whole cure is the arm above
        fix = self.fix(self.cure_two)
        cases = (("none of the cure", self.moved),
                 ("line 10 only, picked",
                  self.on_moved("ctx-part", picks=(self.cure_one,))),
                 ("line 5 only, squashed",
                  self.on_moved("ctx-half", self.edited(5, 12))))
        seen = 0
        for name, ref in cases:
            with self.subTest(case=name):
                before = self.ids()
                row, why, _sent = self.send(ref, fix["id"])
                self.assertIsNone(row, name)
                self.assertIn(self.cure_two[:12], why)
                self.assertEqual(self.ids(), before)
            seen += 1
        self.assertEqual(seen, 3, "the case sweep did not run")

    def test_the_content_check_leaves_the_index_and_worktree_alone(self):
        fix = self.fix(self.cure_one)
        picked = self.on_moved("ctx-clean", picks=(self.cure_one,))
        head = self.git("rev-parse", "HEAD")
        status = self.git("status", "--porcelain")
        row, why, _sent = self.send(picked, fix["id"])
        self.assertIsNotNone(row, why)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), status)

    def test_a_cure_equal_to_the_reviewed_tip_is_carried_by_any_ref(self):
        """An EMPTY cure changes nothing, so every ref carries it."""
        fix = self.fix(self.lane)
        row, why, _sent = self.send(self.moved, fix["id"])
        self.assertIsNotNone(row, why)


class UnreadableAncestryWarnsTest(PatchDeliveryBase):

    def test_a_patch_this_repository_cannot_read_warns_UNKNOWN(self):
        """The door's precedent for an ancestry it cannot read (`_ref_sanity`,
        RefSanityTest): a warning naming what could not be read, never a
        silent pass and never a refusal."""
        fix = self.reviewed()
        ghost = "e" * 40
        path = dispatches.ledger_path()
        events = [json.loads(line) for line in open(path, encoding="utf-8")
                  if line.strip()]
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                if event.get("id") == fix["id"] and event.get("patch_tip"):
                    event["patch_tip"] = ghost
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.assertEqual(dispatches.snapshot()[0][fix["id"]]["patch_tip"],
                         ghost, "fixture premise: the forged patch folds")
        row, why, _sent = self.send(self.rebased, fix["id"])
        self.assertIsNotNone(row, why)
        warnings = " ".join(row.get(dispatches._WRITE_WARNINGS, ()))
        self.assertIn("UNKNOWN", warnings)
        self.assertIn(ghost[:12], warnings)


class TheFlagReachesTheCliTest(PatchDeliveryBase):

    def test_send_takes_decline_patch_and_records_it(self):
        fix = self.reviewed()
        rc, out, err = td.run(dispatches.cmd_dispatch, [
            "send", "seat-b", "work", "re-read the lane",
            "--ref", self.rebased, "--repo", self.repo, "--kind", "build",
            "--supersedes", fix["id"], "--decline-patch",
            decline(self.patch)])
        self.assertEqual(rc, 0, out + err)
        kids = [r for r in dispatches.snapshot()[0].values()
                if r.get("supersedes") == fix["id"]]
        self.assertEqual(len(kids), 1, kids)
        self.assertEqual(kids[0]["decline_patch_because"], WHY)
        self.assertEqual(kids[0]["declined_patch_tips"], [self.patch])

    def test_a_decline_that_names_no_patch_is_refused_at_exit_1(self):
        """A bare reason names no patch, so it declines none: the refusal
        names the missing patch and the exact form that declines it."""
        fix = self.reviewed()
        before = self.ids()
        rc, out, err = td.run(dispatches.cmd_dispatch, [
            "send", "seat-b", "work", "re-read the lane",
            "--ref", self.rebased, "--repo", self.repo, "--kind", "build",
            "--supersedes", fix["id"], "--decline-patch", WHY])
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.ids(), before)
        self.assertIn("--decline-patch %s=REASON" % self.patch[:12], err)

    def test_send_without_it_refuses_at_exit_1(self):
        fix = self.reviewed()
        rc, out, err = td.run(dispatches.cmd_dispatch, [
            "send", "seat-b", "work", "re-read the lane",
            "--ref", self.rebased, "--repo", self.repo, "--kind", "build",
            "--supersedes", fix["id"]])
        self.assertEqual(rc, 1, out + err)
        self.assertIn(self.patch[:12], err)
        self.assertIn("--decline-patch", err)


if __name__ == "__main__":
    unittest.main()
