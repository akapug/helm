"""The whole-ledger fold asks each derived question ONCE.

`projscope.memo` exists and is correct; before this lane nothing opened a
scope around the fold, so every memo() call took its documented
outside-a-scope path and recomputed. Measured on the live ledger: 4,276
memo() calls during one cold fold, ZERO of them inside a scope, and 46.9% of
the fold's git spawns were byte-identical repeats of an argv already run in
the same pass.

WHY THE CURE IS A SCOPE AND NOT A NEW MEMO. `projscope.memo` already had
the exact contract wanted here -- "compute() once per key per scope",
thread-local, and `scope.__exit__` clears the cache at depth zero -- so the
mechanism was correct and merely unreached. Adding a second memo beside it
would have been a new thing to keep honest.

WHAT IT PINS, AND WHY THAT IS CORRECTNESS RATHER THAN SPEED.
`dispatches._carriage_trunk_sha` already argues that trunk must be resolved
ONCE and the object handed down: "two resolutions of one ref name are two
questions, so a trunk that moved between them lets one family answer about
trunk A while the other answers about trunk B and the caller reports a
single verdict." That was implemented per ROW, and the fold resolved the
trunk ref 508 times across a pass costing two minutes on a live board -- so
a land arriving mid-fold could have rows proven against two different
trunks inside one snapshot. Now one snapshot is one measurement:
consistently stale rather than inconsistently fresh, and the writer
re-derives under its own lock so staleness never binds a write.

MEASURED, cold by construction on a copied ledger (3,961 -> 2,124 spawns,
33.0s -> 23.5s, 7.80 -> 4.18 spawns per carried row; byte-identical repeats
inside one fold 46.9% -> 0.9%; `ls-tree` of one tree 323 calls -> 1).

A NESTED SCOPE SHARES ITS CALLER'S CACHE BY DESIGN and this lane leaves that
alone: `cmd_dispatch` opens a scope for the read verbs and three folds run
inside it, which is one projection and one measurement. What must never
happen is a scope OUTLIVING its projection, and the arms below stage exactly
that accident -- a scope that is entered and never closed -- and go red.

THESE ARMS PIN THE SCOPE, NOT THE SPEED. A spawn count is a property of the
ledger it ran against; that the fold RUNS INSIDE A SCOPE is a property of the
code, and it is what makes every memo in the tree effective here.
"""
import os
import subprocess
from unittest import mock

from helm import dispatches, projscope
from tests.test_dispatches import DispatchBase


class TheFoldRunsInsideAMemoScopeTest(DispatchBase):

    def test_the_fold_opens_a_scope_so_the_trees_memos_are_effective(self):
        """`projscope.memo` is documented "outside a scope, always compute".

        So a fold that opens no scope makes every memo in every module it
        reaches inert -- the cache is allocated and never consulted. This
        asserts the scope is OPEN while the fold runs, which is the single
        fact the redundancy depended on.
        """
        seen = []
        real = dispatches._fold_into

        def spy(*a, **k):
            seen.append((projscope.active(), projscope._state().depth))
            return real(*a, **k)

        row = self.add(recipient="seat-a")
        with mock.patch.object(dispatches, "_fold_into", side_effect=spy):
            out, unavailable = dispatches.snapshot()
        # POSITIVE CONTROL on the fold's own output, so this is not an arm
        # about spy instrumentation: the fold really produced the row.
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], out)
        self.assertTrue(seen, "the fold never ran, so this arm proved nothing")
        for active, depth in seen:
            self.assertTrue(active,
                            "the fold body ran OUTSIDE a projscope scope, so "
                            "every memo() it reaches recomputes")
            self.assertGreater(depth, 0)

    def test_the_scope_does_not_leak_past_the_fold(self):
        """A memo that outlives its fold is the persisted answer the module
        forbids, arrived at by accident. CONTROL for the arm above: the scope
        the fold opens must be closed when it returns, so a later caller in
        the same process is not answered from it."""
        self.add(recipient="seat-a")
        self.assertFalse(projscope.active())
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: active() does
        # report True when a scope really is open, so the False assertions
        # below are measured answers rather than a predicate stuck off.
        with projscope.scope():
            self.assertTrue(projscope.active())
        dispatches.snapshot()
        self.assertFalse(projscope.active(),
                         "the fold left a scope open; the next fold in this "
                         "process would reuse its answers")


    def test_a_LATER_fold_never_reuses_an_EARLIER_folds_memo(self):
        """The accident this lane must not introduce.

        A memo that outlives its fold is the persisted answer the module
        forbids, reached by accident rather than by decision. This asks the
        question directly: the same memo key, computed once per fold, must
        produce a DIFFERENT answer in the second fold -- because the first
        fold's cache is gone -- and the CONTROL below proves the memo really
        is working inside a single fold, so a difference cannot be explained
        by the memo never caching at all.
        """
        self.add(recipient="seat-a")
        calls, per_fold = [], []
        real = dispatches._fold_into

        def spy(*a, **k):
            # Same key every time. Inside one fold the memo must answer with
            # the first computation; across folds it must recompute.
            per_fold.append(projscope.memo(
                ("test-probe",), lambda: len(calls)))
            calls.append(1)
            per_fold.append(projscope.memo(
                ("test-probe",), lambda: len(calls)))
            return real(*a, **k)

        with mock.patch.object(dispatches, "_fold_into", side_effect=spy):
            dispatches.snapshot()
            first = list(per_fold)
            per_fold.clear()
            dispatches.snapshot()
            second = list(per_fold)

        self.assertTrue(first and second, "a fold did not run")
        # CONTROL, inside one fold: the two asks of one key agree, so the
        # memo is live. Without this the arm below passes on a dead memo.
        self.assertEqual(first[0], first[1],
                         "the memo did not cache inside a single fold, so "
                         "this arm proves nothing about isolation")
        # THE PROPERTY: the second fold recomputed rather than inheriting.
        self.assertNotEqual(first[0], second[0],
                            "the second fold reused the first fold's cache; "
                            "a pinned trunk would outlive its fold")

    def test_the_trunk_a_scope_pins_does_not_outlive_the_scope(self):
        """Semantic form of the arm above, on the real subject.

        Inside ONE scope a moved trunk is deliberately NOT seen -- that is
        the pinning this lane adds, and one snapshot being one measurement is
        the whole point. Across scopes it MUST be seen, or the pin has become
        a stale cache.
        """
        gitdir = os.path.join(self.repo, ".git")
        trunk = "refs/heads/" + self.main

        def resolve():
            return dispatches._carriage_trunk_sha(gitdir, trunk)

        before = resolve()
        # POSITIVE CONTROL: the resolver returns a real sha here, so an
        # equality below cannot be two Nones agreeing.
        self.assertTrue(before)
        self.assertEqual(before, self.git("rev-parse", self.main))
        with projscope.scope():
            pinned = resolve()
            self.commit_file("moves-trunk", "trunk advances mid-scope")
            moved = self.git("rev-parse", "HEAD")
            # PINNED: the same scope keeps its answer, on purpose.
            self.assertEqual(resolve(), pinned)
            # UNCONDITIONAL POSITIVE CONTROL inside the scope, on the same
            # observable: the resolver still answers with a real sha here, so
            # the equality above is two measurements agreeing rather than two
            # failures agreeing.
            self.assertTrue(resolve())
        self.assertNotEqual(moved, before, "the fixture did not move trunk, "
                                           "so neither assertion means anything")
        # ACROSS SCOPES: the new trunk is visible, so the pin died with it.
        self.assertEqual(resolve(), moved)

    def test_the_writer_is_never_reached_INSIDE_a_fold_scope(self):
        """Requirement (C): a write must never be answered from a fold's pin.

        `A listing that is one pass stale is a listing; a write that is one
        pass stale is a lie` -- cmd_dispatch's own words. The property that
        keeps it true here is STRUCTURAL: the writer re-derives under its own
        lock before the append, and the checkpoint advance folds after it, so
        `_record_close_proven` is never invoked while a scope of this lane's
        making is open.

        THE PROPERTY IS AN ABSENCE AND IS ASSERTED AS ONE. Looping over
        observations of the writer during a fold cannot express it: the
        writer is never reached during a fold, so such a loop has an empty
        body and reports nothing while passing. The control below proves the
        instrument that reports this absence can also report a presence.
        """
        self.add(recipient="seat-a")
        seen = []
        real = dispatches._record_close_proven

        def spy(*a, **k):
            seen.append(projscope.active())
            return real(*a, **k)

        with mock.patch.object(dispatches, "_record_close_proven",
                               side_effect=spy):
            dispatches.snapshot()
            # THE PROPERTY: a fold reaches no writer at all.
            self.assertEqual(seen, [],
                             "a close re-derivation ran inside the fold and "
                             "could be answered from its pinned trunk")
            # UNCONDITIONAL POSITIVE CONTROL on the SAME observable: the spy
            # does record a call when one happens, so the empty list above is
            # a measured absence and not a double that never fires.
            # The call's OUTCOME is asserted, not discarded: it raises for
            # missing arguments, which is all this control needs -- that the
            # double is reached and records.
            with self.assertRaises(TypeError):
                dispatches._record_close_proven()
        self.assertEqual(len(seen), 1,
                         "the spy never fired even when called directly, so "
                         "the emptiness above proved nothing")
        self.assertFalse(seen[0], "no scope may be open outside a fold")


if __name__ == "__main__":
    import unittest
    unittest.main()
