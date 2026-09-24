"""The carriage witness's REFUSAL SENTENCE, which is not the same object as
its answer.

Split out of `test_dispatches` rather than added to it: that module sits
within a few kilobytes of the never-track size ceiling, and the guard refuses
any commit that pushes it over.

IT SUBCLASSES THE BASE FIXTURE, NOT THE SIBLING TEST CLASS. Inheriting from
`TheCarriageWitnessIsDerivedOnEveryReadTest` would import its whole arm set
and re-run every one of them here against a real git repository for nothing.

THE ANSWER AND THE SENTENCE FAIL SEPARATELY. Every arm asserts the answer is
still None alongside whatever it claims about the wording, because a cure to
a refusal's prose that quietly widened what the door admits would satisfy a
prose assertion and lose the gate.
"""
import os
import subprocess

from helm import dispatches
from tests.test_dispatches import DispatchBase


class TheRefusalNamesWhichQuestionWentUnansweredTest(DispatchBase):
    """`rowworld._reached_trunk` collapses six distinct conditions into
    `(None, None)` -- an unresolvable tip, an ancestor, a failed `git cherry`,
    an empty listing, unparseable output, and a merge tip absent from its own
    listing -- and the caller cannot tell them apart from that return. The
    sentence it composes must therefore not name an instrument that never
    ran."""

    def setUp(self):
        super().setUp()
        self.gitdir = os.path.join(self.repo, ".git")
        self.trunk = "refs/heads/" + self.main

    def _witness(self, tip):
        return dispatches._carriage_reached_witness(
            self.gitdir, tip, self.trunk, None, "no replay witness")

    def _merge_of_two_landed_lanes(self):
        """A tip that reaches the FINAL RETURN while NOT being trunk history.

        Getting here is narrow, and the two shapes that look like they should
        work do not:

        * an ORPHAN tip -- with no merge-base `git cherry` does not go quiet,
          it lists the entire right side as `+`, so the tip leaves through the
          `unmatched` arm instead;
        * a lane that MERGED TRUNK -- `_reached_trunk`'s own docstring records
          that this loses the upstream-only twin the lane's work would have
          matched against, so its commits read `+` and it leaves the same way.

        What works is a merge of TWO LANES, neither of which merged trunk.
        Each lane's commit keeps its landed twin upstream-only and reads `-`,
        so nothing is unmatched; the merge commit itself is SKIPPED by `git
        cherry`, so the tip is never `seen`; and the result is (None, None)
        with the tip plainly not an ancestor of trunk.
        """
        self.git("checkout", "-q", "-b", "lane-one", self.main)
        one = self.commit_file("one", "first lane")
        self.git("checkout", "-q", "-b", "lane-two", self.main)
        two = self.commit_file("two", "second lane")
        # TRUNK MOVES FIRST so each cherry-pick mints a DIFFERENT sha carrying
        # the same patch; onto an unmoved trunk the pick reproduces the lane
        # commit's own sha and there is nothing upstream-only to match.
        self.git("checkout", "-q", self.main)
        self.commit_file("drift", "trunk moved under both lanes")
        self.git("cherry-pick", one)
        self.git("cherry-pick", two)
        self.git("checkout", "-q", "lane-one")
        self.git("merge", "-q", "--no-ff", "-m", "merge lane-two", "lane-two")
        merge_tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return merge_tip

    def test_an_ancestor_tip_is_told_ancestry_NOT_patch_identity(self):
        """The refusal must not blame an instrument that never ran.

        `rowworld._reached_trunk` probes ANCESTRY FIRST and short-circuits on
        it -- its own comment says an ancestor leaves `git cherry` an empty
        range and the probe answers "for a fortieth of the price" -- so for a
        tip already reachable from trunk, cherry IS NEVER SPAWNED. The
        refusal nonetheless read "did not reach <trunk> by patch identity
        either", naming a measurement that did not happen, which the next
        reader takes as a finding that the work never landed.

        Not a corner: a row's `tip` is the sha it was FILED at, an anchor
        rather than a work result, so a never-verdicted row still bound to
        its filing sha reaches this sentence as soon as that sha becomes
        trunk history -- the ordinary fate of every such row.
        """
        # PRECONDITION, enforced by the helper: `git` runs with check=True,
        # so a non-ancestor raises here rather than quietly weakening the arm.
        self.git("merge-base", "--is-ancestor", self.c, self.main)

        answer, detail = self._witness(self.c)
        # THE GATE IS UNCHANGED, and this assertion is the point of the arm:
        # an empty range still affirms every possible trunk, so the witness
        # still refuses. Only the sentence learned which question went
        # unanswered.
        self.assertIsNone(answer)
        self.assertIn("ALREADY history of", detail)
        self.assertNotIn("did not reach", detail)

    def test_a_non_ancestor_reaching_the_same_line_keeps_the_old_words(self):
        """CONTROL on the SAME final return, which is what makes the arm above
        a discrimination rather than a sentence that always fires.

        Here patch identity genuinely found no match and the original wording
        is correct, so it must survive the cure.
        """
        merge_tip = self._merge_of_two_landed_lanes()
        # The two properties the route depends on, asserted rather than
        # assumed: it IS a merge, and it is NOT trunk history.
        self.assertEqual(len(self.git("rev-list", "--parents", "-1",
                                      merge_tip).split()) - 1, 2)
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("merge-base", "--is-ancestor", merge_tip, self.main)

        answer, detail = self._witness(merge_tip)
        self.assertIsNone(answer)
        self.assertIn("did not reach", detail)
        self.assertNotIn("ALREADY history of", detail)

    def test_the_ancestry_probe_is_FALSE_when_it_cannot_read(self):
        """A failed probe falls back to the weaker sentence, never forward.

        `_reached_by_ancestry` answers the WORDING, so its failure mode has to
        be the conservative one: an unreadable `merge-base` must not be
        spelled "ALREADY history of", which would assert an ancestry nobody
        measured.
        """
        missing = os.path.join(self.tmp, "not-a-repo")
        self.assertFalse(dispatches._reached_by_ancestry(
            missing, self.c, self.trunk))
        # CONTROL on the same helper in the same method: it really does
        # affirm when the read succeeds, so the False above is a measured
        # refusal rather than a function that never returns True.
        self.assertTrue(dispatches._reached_by_ancestry(
            self.gitdir, self.c, self.trunk))


if __name__ == "__main__":
    import unittest
    unittest.main()
