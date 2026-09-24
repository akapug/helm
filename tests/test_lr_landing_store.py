#!/usr/bin/env python3
"""THE LANDING STORE — WHEN A READER ASKS WHETHER A LANE LANDED, IS IT SERVED
THE REAL PROOF, AND CAN IT STILL TELL "I COULD NOT TAKE THE HASH" APART FROM
"THERE IS NO HASH"?

One question in two halves, which is why these three classes travel together.
`ProofAvailabilityIsItsOwnAnswerTest` is the vocabulary half: `_patch_id`
answers None for five different facts, three of them "git could not answer" and
two of them "git answered and there is no id", so every reader that treats None
as absence asserts a negative about a question that was never put.
`LandingStoreServesTheRealProofTest` is the end-to-end half, driven through the
real `_landing_proof` rather than through a copy of its rules — a
reimplementation agrees with the original right up until one of them changes.
`LandingStoreBackfillTest` is the structural case both halves need: a landing
further back than STORED_PID_SCAN_CAP, which no amount of budget reaches.

MOVED WHOLE OUT OF `tests/test_landreq.py`, which stood 126,090 bytes PAST the
never-track ceiling. The class text here is byte-identical to its text there.

THE FIXTURES ARE REUSED BY REFERENCE, NEVER COPIED. `_Ok`, `_Fail`, `_Missed`,
`_hash_faces`, `_tmp_rescue_store`, `_EMPTY`, `git_verb` and `run` are the
objects `tests/test_landreq.py` defines, so one fixture serves both files and
they cannot come to disagree about what a faked git answer looks like.
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import landreq, pk
from tests.test_landreq import (_EMPTY, _Fail, _Missed, _Ok, _hash_faces,
                                _tmp_rescue_store, git_verb, run)


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


class LandingStoreBackfillTest(unittest.TestCase):
    """The store must learn a landing OUTSIDE the capped scan window.

    THE ACCEPTANCE THE BRIEF DID NOT CARRY. "The console reads the true
    dozens" proves the budget half only, and only when the rows in play
    happen to be recent. The structural half is a landing further back than
    STORED_PID_SCAN_CAP, which no amount of budget reaches.

    MEASURED on the live estate while building this: 0 of 154 stored
    patch-ids hit the capped index, while 6 of 8 sampled receipts were plain
    ANCESTORS of trunk — one of them at depth 107, INSIDE the window, whose
    stored id still missed. So the window was never the only reason, and a
    backfill keyed on the index stores NOTHING. Ancestry is unbounded and is
    the rung that works; this arm is what makes that non-negotiable.
    """

    def test_a_landing_beyond_the_scan_cap_is_still_stored(self):
        seen = {}

        def fake_ancestry(_gitdir, commit, _ref):
            seen[commit] = True
            return landreq.ANCESTOR

        def fake_index(*_a, **_k):
            raise AssertionError(
                "the backfill consulted the CAPPED INDEX for a tip ancestry "
                "already proved — that is the shape that stored nothing")

        # THE FIXTURE MUST SATISFY THE READER OR IT PROVES THE WRONG THING.
        # The first cut gave the receipt patch-id "b" and let the carrier hash
        # to "d", so the writer's admission rule refused it and the arm read
        # (0, 1, False) while asserting (1, 1, False) — it was measuring the
        # admission gate, not the scan window. The receipt's id and what its
        # carrier hashes to are ONE fact here; they are spelled that way now.
        pid, tip = "b" * 40, "c" * 40
        receipts = [{"patch_id": pid, "reviewed_tip": tip}]
        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry", fake_ancestry), \
                mock.patch.object(landreq, "_stored_patch_index", fake_index), \
                _hash_faces(lambda *_a, **_k: pid), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            stored, examined, incomplete = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main", receipts=receipts)
            written = pk.read_json(store, {})
        self.assertEqual((stored, examined, incomplete), (1, 1, False))
        self.assertTrue(seen.get(tip), "ancestry was never asked")
        self.assertEqual(list(written.values()), [tip],
                         "an ancestor stores its OWN sha; it is on trunk")

    def test_the_index_walk_is_bounded_by_the_same_budget(self):
        """THE OVERRUN HAPPENS INSIDE ONE CALL, so the deadline has to be
        inside it. A budget consulted only between receipts bounded nothing:
        the uncapped walk is 100000 commits at ~7.9ms each."""
        clock = [0.0]

        hashed = []

        def creeping_hash(_g, sha, _trunk=None):
            clock[0] += 5.0          # each commit blows the whole budget
            hashed.append(sha)
            return landreq.HASHED, "p" + sha[:39]

        # THE DOUBLE MUST BE THE ONE THE WALK ACTUALLY CALLS. This arm was
        # written against `_patch_id` and kept passing when the walk moved to
        # the availability-aware face: the fake never fired, the clock never
        # advanced, and a budget arm measured a walk with no budget pressure
        # at all. `hashed` below is the assertion that the double is live.
        with mock.patch.object(landreq, "_monotonic", lambda: clock[0]), \
                mock.patch.object(landreq, "_measured_patch_id",
                                  creeping_hash), \
                mock.patch.object(landreq, "_git",
                                  return_value=_Ok("\n".join(
                                      "%040x" % n for n in range(50)))):
            index, why = landreq._stored_patch_index(
                "/nonexistent/.git", "origin/main", cap=100000, deadline=3.0)
        self.assertTrue(hashed,
                        "the hash double never ran, so nothing consumed the "
                        "budget and this arm measured an unpressured walk")
        self.assertEqual(why, landreq.INDEX_BUDGET,
                         "a walk cut short by its budget reported %r — a "
                         "partial index cannot prove absence, and this is "
                         "what tells the caller so" % (why,))
        self.assertLess(len(index), 50,
                        "the deadline never stopped the walk: every one of "
                        "the 50 commits was hashed inside a 3s budget that "
                        "the first commit alone exhausted")


class ProofAvailabilityIsItsOwnAnswerTest(unittest.TestCase):
    """A HASH THAT COULD NOT BE TAKEN IS NOT A HASH THAT DOES NOT EXIST.

    `_patch_id` answers None for five different facts, three of them "git
    could not answer" and two of them "git answered, and there is no id".
    Every reader that treats None as absence is asserting a negative about a
    question that was never put, invisibly. `_measured_patch_id` splits the
    state from the id; these arms are the split's contract, and the two index
    controls below are the case the split exists for.
    """

    GITDIR = "/nonexistent/.git"

    @staticmethod
    def _git_script(table):
        """Answer each git subcommand from `table`. Unlisted verbs return
        None, which is `_git`'s own "could not run" and never a silent OK."""
        def fake(_gitdir, *argv, **kw):
            for key, value in table.items():
                if key in argv:
                    return value
            return None
        return fake

    def _proof_with(self, result):
        """`_landing_proof` driven to the local-resolution branch, with the
        vanished pass replaced by a SPY. Returns (answer, times_consulted).

        The tip is deliberately not a sha: that is what sends the call to
        `rev-parse` instead of the NOT_ANCESTOR fast path, so the branch under
        test is the one that actually runs."""
        seen = []

        def spy(_gitdir, _tip):
            seen.append(1)
            return "absent"

        with mock.patch.object(landreq, "_derive_expired", lambda: False), \
                mock.patch.object(landreq, "_ancestry",
                                  lambda *a, **k: landreq.NOT_ANCESTOR), \
                mock.patch.object(landreq, "_git",
                                  self._git_script({"rev-parse": result})), \
                mock.patch.object(landreq, "_vanished_proof", spy):
            answer = landreq._landing_proof(self.GITDIR, "lane/not-a-sha",
                                            "refs/heads/main")
        return answer, len(seen)

    def test_a_RESOLVED_name_reaches_patch_comparison_carrying_ITS_sha(self):  # noqa: VACUOUS_ASSERTION — the answer is asserted EQUAL to "patch-equivalent" and the spy list EQUAL to [resolved] before the empty-list check; both are unconditional positives on the same call, so a reader that answered nothing reddens above the absence
        """THE MUST-HIT THE OTHER TWO ARMS ARE MEANINGLESS WITHOUT, and its
        absence is exactly how a dead success branch survived 843 green tests.

        Both refusal arms below assert that something did NOT happen, and a
        reader that stopped resolving at all would satisfy both. Only this arm
        says what the branch is FOR: an rc 0 read with a non-empty sha BINDS
        THAT SHA and continues to patch comparison. Every other arm on this
        path enters through the full-sha fast path above, which never reaches
        the resolution branch -- so a suite can be entirely green about a
        function whose success case returns unknown, and mutating the constant
        or the classifier cannot see it either, because both live downstream
        of a branch no arm entered."""
        resolved, own_id = "d" * 40, "e" * 40
        asked, vanished = [], []

        def spy_tip(_gitdir, sha):
            asked.append(sha)
            return own_id

        def spy_vanished(_gitdir, _tip):
            vanished.append(1)
            return "absent"

        with mock.patch.object(landreq, "_derive_expired", lambda: False), \
                mock.patch.object(landreq, "_ancestry",
                                  lambda *a, **k: landreq.NOT_ANCESTOR), \
                mock.patch.object(
                    landreq, "_git",
                    self._git_script({"rev-parse": _Ok(resolved + "\n")})), \
                mock.patch.object(landreq, "_landed_index",
                                  lambda *a, **k: ({own_id: "f" * 40}, None)), \
                mock.patch.object(landreq, "_tip_patch_id", spy_tip), \
                mock.patch.object(landreq, "_vanished_proof", spy_vanished):
            answer = landreq._landing_proof(self.GITDIR, "lane/not-a-sha",
                                            "refs/heads/main")
        self.assertEqual(answer, "patch-equivalent")
        # ...and it carried the RESOLVED sha, not the name it was asked about.
        self.assertEqual(asked, [resolved])
        self.assertEqual(vanished, [], "a successful read was refuted")

    def test_a_read_that_FAILED_never_reaches_the_vanished_pass(self):
        """rc 128 is git reporting on ITSELF, not on the object. Letting it
        travel to the refutation pass lets a remote or reflog miss finish the
        sentence and publish ABSENT about a question nobody put -- the same
        negative-on-an-unmeasured-question one branch over from the malformed
        success this reader already refuses."""
        answer, consulted = self._proof_with(_Fail())
        self.assertEqual(answer, "unknown")
        self.assertEqual(consulted, 0, "a failed read was refuted, not refused")

    def _absence_with(self, result):
        """`_vanished_absence` — the subsumed door's reader — driven by the
        same scripted `rev-parse` face, with the same SPY on the vanished
        pass. Returns (answer, times_consulted)."""
        seen = []

        def spy(_gitdir, _tip):
            seen.append(1)
            return "absent"

        with mock.patch.object(landreq, "_git",
                               self._git_script({"rev-parse": result})), \
                mock.patch.object(landreq, "_vanished_proof", spy):
            answer = landreq._vanished_absence(self.GITDIR, "c" * 40)
        return answer, len(seen)

    def test_a_read_that_LOOKED_and_missed_is_unknown_to_the_ladder(self):
        """rc 1 is git reporting that THIS CLONE holds no such object, which is
        not a reading of trunk. Handing it to the vanished pass publishes that
        pass's ABSENT, and ABSENT is what `withdrawn` retires a row on — so a
        pruned object would record "not on trunk" for work that may have
        landed. The ladder answers unknown and never asks."""
        answer, consulted = self._proof_with(_Missed())
        self.assertEqual(answer, "unknown")
        self.assertEqual(consulted, 0, "the ladder refuted a vanished object "
                         "instead of reporting it unreadable")

    def test_a_read_that_LOOKED_and_missed_still_reaches_its_own_door(self):
        """THE UNCONDITIONAL POSITIVE CONTROL for both arms above, on the same
        observable. Without it, "the vanished pass was not consulted" would
        also be true of a tree that had deleted the pass outright, and the
        subsumed door — the one door whose clauses bound a vanished object —
        would have lost its only way in. rc 1 reaches the pass through
        `_vanished_absence`; rc 128 still does not."""
        answer, consulted = self._absence_with(_Missed())
        self.assertEqual(answer, "absent")
        self.assertEqual(consulted, 1)
        answer, consulted = self._absence_with(_Fail())
        self.assertEqual(answer, "unknown")
        self.assertEqual(consulted, 0, "a failed read was refuted, not refused")

    def test_the_two_nonzero_faces_are_not_the_same_fixture(self):
        """The pair above only means something if the two results DIFFER in
        the bit the cure reads. Stated as an arm so a later tidy that gives
        both classes one returncode reddens here rather than silently making
        the pair a duplicate of itself."""
        self.assertEqual(_Missed().returncode, landreq._GIT_LOOKED_AND_MISSED)
        self.assertNotEqual(_Fail().returncode, _Missed().returncode)

    def test_the_writer_admits_an_EMPTY_range_as_a_measured_NO(self):
        """git LOOKED: the target holds no diff, so it carries no patch-id at
        all and cannot be carrying the non-empty one the entry was cached
        under. False is earned here, and the rewrite it licenses is too."""
        with mock.patch.object(landreq, "_git",
                               self._git_script({"diff": _Ok("")})):
            answer = landreq._readable_correlation(
                self.GITDIR, "b" * 40, "a" * 40)
        self.assertIs(answer, False)

    def test_the_writer_leaves_an_UNMEASURED_hash_undecided(self):
        """...and the opposite fact keeps the opposite answer. None preserves
        the entry; collapsing these two is how a store that could not read a
        hash starts discarding live work."""
        with mock.patch.object(landreq, "_git", self._git_script({})):
            answer = landreq._readable_correlation(
                self.GITDIR, "b" * 40, "a" * 40)
        self.assertIsNone(answer)

    def test_the_writer_still_compares_when_git_produced_an_id(self):
        """The positive control for both arms above: HASHED reaches the
        comparison, and reaches it in both directions."""
        table = {"diff": _Ok("diff --git a/x b/x\n"),
                 "patch-id": _Ok("b" * 40 + " " + "c" * 40)}
        with mock.patch.object(landreq, "_git", self._git_script(table)):
            same = landreq._readable_correlation(
                self.GITDIR, "b" * 40, "a" * 40)
            other = landreq._readable_correlation(
                self.GITDIR, "d" * 40, "a" * 40)
        self.assertIs(same, True)
        self.assertIs(other, False)

    def test_an_unrecognised_state_preserves_rather_than_demotes(self):
        """THE FALLTHROUGH IS THE PRESERVING ONE, and it is asserted rather
        than trusted to the three states staying three. A state added later
        that this reader has never seen must not reach the comparison with a
        None id and demote a live entry on a question it could not read."""
        with mock.patch.object(landreq, "_measured_patch_id",
                               lambda *a, **k: ("a-state-from-the-future",
                                                None)):
            answer = landreq._readable_correlation(
                self.GITDIR, "b" * 40, "a" * 40)
        self.assertIsNone(answer)
        # THE UNCONDITIONAL POSITIVE CONTROL, on the same call with the same
        # operands: a None here would also be produced by a reader that had
        # stopped answering at all, and only a state it DOES recognise can
        # tell those two apart.
        with mock.patch.object(landreq, "_measured_patch_id",
                               lambda *a, **k: (landreq.HASHED, "b" * 40)):
            known = landreq._readable_correlation(
                self.GITDIR, "b" * 40, "a" * 40)
        self.assertIs(known, True)

    def test_a_diff_that_could_not_run_is_unmeasured_not_empty(self):
        with mock.patch.object(landreq, "_git", self._git_script({})):
            state, pid = landreq._measured_patch_id(self.GITDIR, "a" * 40)
        self.assertEqual((state, pid), (landreq.UNMEASURED, None))

    def test_a_diff_that_ran_and_was_empty_is_a_measurement(self):
        """AND IT MUST NOT BE A HOLE. git ran, git looked, there is nothing
        to hash -- an ordinary empty commit. Folding it in with the failures
        would make normal history look like a broken repository."""
        with mock.patch.object(landreq, "_git",
                               self._git_script({"diff": _Ok("")})):
            state, pid = landreq._measured_patch_id(self.GITDIR, "a" * 40)
        self.assertEqual((state, pid), (landreq.EMPTY_RANGE, None))

    def test_a_hashed_range_carries_its_id_and_only_then(self):
        with mock.patch.object(landreq, "_git", self._git_script(
                {"diff": _Ok("diff --git a/x b/x\n"),
                 "patch-id": _Ok("b" * 40 + " " + "c" * 40)})):
            state, pid = landreq._measured_patch_id(self.GITDIR, "a" * 40)
        self.assertEqual((state, pid), (landreq.HASHED, "b" * 40))

    def test_the_id_is_none_unless_the_state_is_hashed(self):
        """THE PROPERTY THAT MAKES THE PAIR SAFE, stated as an arm rather
        than trusted to single return points staying single."""
        cases = [
            ({}, landreq.UNMEASURED),
            ({"diff": _Ok("")}, landreq.EMPTY_RANGE),
            ({"diff": _Fail("")}, landreq.UNMEASURED),
            ({"diff": _Ok("d\n"), "patch-id": _Fail("")}, landreq.UNMEASURED),
            ({"diff": _Ok("d\n"), "patch-id": _Ok("")}, landreq.EMPTY_RANGE),
        ]
        for table, expected in cases:
            with mock.patch.object(landreq, "_git", self._git_script(table)):
                state, pid = landreq._measured_patch_id(self.GITDIR, "a" * 40)
            self.assertEqual(state, expected, "table %r" % (sorted(table),))
            self.assertIsNone(
                pid, "a non-HASHED state carried an id (%r, %r)"
                % (state, pid))

    def test_a_merge_base_that_succeeded_with_no_output_is_unmeasured(self):
        """rc0 WITH EMPTY OUTPUT IS MALFORMED, NOT A NO-BASE.

        Success means a base was found and printed. Success with nothing
        printed means the answer did not survive to us, and the old code sent
        it down the same fallback as a measured no-base -- so a range
        silently became the tip's own diff and the id computed after git
        broke was indistinguishable from one computed after git answered.
        """
        with mock.patch.object(landreq, "_git", self._git_script(
                {"merge-base": _Ok(""),
                 "diff": _Ok("d\n"),
                 "patch-id": _Ok("b" * 40 + " x")})):
            state, pid = landreq._measured_patch_id(
                self.GITDIR, "a" * 40, "origin/main")
        self.assertEqual(
            (state, pid), (landreq.UNMEASURED, None),
            "a malformed merge-base produced an id, so the range it was "
            "computed over was chosen by a broken read")

    def test_a_merge_base_exit_one_is_a_measured_no_base(self):
        """MUST-HIT FOR THE ARM ABOVE. rc 1 is this command's documented way
        of saying the two commits share no ancestor, and falling back to the
        tip's own diff on it is correct. Without this the arm above passes
        against a face that refuses every merge-base outcome."""
        class _NoBase(object):
            returncode = 1
            stdout = ""

        with mock.patch.object(landreq, "_git", self._git_script(
                {"merge-base": _NoBase(),
                 "diff": _Ok("d\n"),
                 "patch-id": _Ok("b" * 40 + " x")})):
            state, pid = landreq._measured_patch_id(
                self.GITDIR, "a" * 40, "origin/main")
        self.assertEqual((state, pid), (landreq.HASHED, "b" * 40))

    def test_patch_id_is_still_the_fail_open_projection(self):
        """EVERY EXISTING CALLER KEEPS ITS CONTRACT. `_patch_id` returns the
        id or None; the split is available to the callers whose answer
        changes with availability, and imposed on none of them."""
        with mock.patch.object(landreq, "_git", self._git_script(
                {"diff": _Ok("d\n"), "patch-id": _Ok("b" * 40 + " x")})):
            self.assertEqual(landreq._patch_id(self.GITDIR, "a" * 40),
                             "b" * 40)
        with mock.patch.object(landreq, "_git", self._git_script({})):
            self.assertIsNone(landreq._patch_id(self.GITDIR, "a" * 40))

    # ---- the parentless commit: root, empty root, shallow boundary ----

    # ---- the parentless commit, under the sealed reader contract ----
    #
    # THE FIXTURES HERE ARE REAL REPOSITORIES ON PURPOSE. The contract's whole
    # content is that git parses the object and we do not, so a mock of git's
    # output would assert my model of git rather than git. The three ancestry
    # rewriters are BUILT, not described.

    def _repo(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        repo = os.path.join(root, "r")
        os.makedirs(repo)

        def g(*argv):
            r = subprocess.run(("git", "-C", repo) + argv,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0,
                             "fixture setup failed: git %s -> %s"
                             % (" ".join(argv), r.stderr.strip()))
            return r.stdout.strip()

        g("init", "-q")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "T")
        return repo, g

    def test_a_contentful_root_is_hashed_not_called_empty(self):
        """NO PARENT IS NOT NO CONTENT. A root adds its whole tree against the
        empty tree and has a real id; reporting EMPTY_RANGE drops it while
        certifying the window complete, which manufactures an absence."""
        repo, g = self._repo()
        with io.open(os.path.join(repo, "x"), "w") as fh:
            fh.write("root content\n")
        g("add", "x"); g("commit", "-qm", "root")
        state, pid = landreq._measured_patch_id(os.path.join(repo, ".git"),
                                                g("rev-parse", "HEAD"))
        self.assertEqual(state, landreq.HASHED)
        self.assertTrue(pid, "a hashed root carried no id")

    def test_a_genuinely_empty_root_is_a_measurement(self):
        """A root with nothing in it really has nothing to hash, and that is
        an answer rather than a hole."""
        repo, g = self._repo()
        g("commit", "-q", "--allow-empty", "-m", "empty root")
        state, pid = landreq._measured_patch_id(os.path.join(repo, ".git"),
                                                g("rev-parse", "HEAD"))
        self.assertEqual((state, pid), (landreq.EMPTY_RANGE, None))

    def test_a_replace_grafted_child_is_not_a_root(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual against the root answer is the absence half; the HASHED assertion on the un-grafted measurement is its unconditional positive control
        """REWRITER ONE: a replace ref. `git replace --graft` makes a child
        parentless to every ordinary traversal while the repository is not
        shallow, so neither the plain parent query nor the shallow check can
        refuse it -- only asking with replace refs DISABLED."""
        repo, g = self._repo()
        with io.open(os.path.join(repo, "f"), "w") as fh:
            fh.write("1\n")
        g("add", "f"); g("commit", "-qm", "one")
        with io.open(os.path.join(repo, "f"), "w") as fh:
            fh.write("2\n")
        g("add", "f"); g("commit", "-qm", "two")
        child = g("rev-parse", "HEAD")
        g("replace", "--graft", child)
        gitdir = os.path.join(repo, ".git")

        # MUST-HIT ON THE FIXTURE: the graft really is in effect, or this arm
        # measures an ordinary two-commit repo and proves nothing.
        plain = subprocess.run(("git", "-C", repo, "log", "--format=%P",
                                "-1", child), capture_output=True, text=True)
        self.assertEqual(plain.stdout.strip(), "",
                         "the replace graft did not take, so this child still "
                         "shows its parent and the arm is vacuous")

        # THE PROPERTY IS INERTNESS: a replace ref cannot move the answer.
        # The instrument reads the object view, so the rewriter is invisible
        # to it, and the id is IDENTICAL to the one this same repository
        # yields with no replace ref installed. That is stronger than a
        # refusal, which would only say the question could not be answered.
        with_graft = landreq._measured_patch_id(gitdir, child)
        # THE NONROOT CONTROL, at its EXACT value and WHILE the rewriter is
        # still installed: the root helper must REFUSE this child, because it
        # is not a root and only the rewriter makes it look like one. An
        # inequality would pass on any answer that merely differs.
        self.assertEqual(landreq._root_patch_id(gitdir, child),
                         (landreq.UNMEASURED, None),
                         "the root helper accepted a REPLACE-grafted child")
        g("replace", "-d", child)
        without = landreq._measured_patch_id(gitdir, child)
        self.assertEqual(with_graft, without,
                         "a REPLACE ref moved the patch-id instrument")
        # MUST-HIT on the comparison itself: an unrewritten two-commit child
        # really does hash, so the equality above is between two real
        # measurements and not between two refusals.
        self.assertEqual(without[0], landreq.HASHED)

    def test_the_index_enumerates_and_hashes_through_ONE_view(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn is the pre-cure CONTROL on the ambient walk, not the subject; the subject is asserted positively, that the ancestor's id IS in the index and the flag IS None
        """MIXED VIEWS CERTIFY A FALSE ABSENCE, and neither half looks wrong.

        The index ENUMERATES the commits whose ids it will hold and HASHES
        each one. If the enumeration reads the ambient view while the hashes
        read the object view, a graft that hides an ancestor from the walk
        leaves the index reporting a COMPLETE window it was never shown — and
        a later miss on that ancestor's id is published as ABSENT, a negative
        asserted about a commit nobody looked at.

        The history is R (adds x) then T (adds y), with a LEGACY GRAFT on T so
        T reads as parentless. Read through one view the population is whole,
        so R's id is in the index and a lookup for it is a genuine POSITIVE —
        not an UNKNOWN. The pre-cure control is the ambient enumeration, which
        does not contain R at all.

        THE ARM DRIVES `_landed_index`, THE DOOR, AND NEVER NAMES THE BUILDER
        BEHIND IT. Which function builds the cover is the door's business and
        has already been replaced once underneath this arm; the property --
        one view for the population and for the hashes -- belongs to the door
        and survives that replacement. What does NOT survive is the ARGUMENT,
        which is why it is spelled out at the call below.
        """
        repo, g = self._repo()
        with io.open(os.path.join(repo, "x"), "w") as fh:
            fh.write("x\n")
        g("add", "x"); g("commit", "-qm", "R adds x")
        ancestor = g("rev-parse", "HEAD")
        with io.open(os.path.join(repo, "y"), "w") as fh:
            fh.write("y\n")
        g("add", "y"); g("commit", "-qm", "T adds y")
        child = g("rev-parse", "HEAD")
        gitdir = os.path.join(repo, ".git")
        state, ancestor_id = landreq._measured_patch_id(gitdir, ancestor)
        self.assertEqual(state, landreq.HASHED)

        info = os.path.join(gitdir, "info")
        os.makedirs(info, exist_ok=True)
        with io.open(os.path.join(info, "grafts"), "w") as fh:
            fh.write(child + "\n")

        # THE CONTROL FIRST, and it is what makes the assertion below a
        # measurement: under the AMBIENT view the ancestor is not enumerated
        # at all, which is precisely the population the old walk trusted.
        branch = g("rev-parse", "--abbrev-ref", "HEAD")
        ambient = subprocess.run(
            ("git", "--git-dir", gitdir, "rev-list", branch),
            capture_output=True, text=True)
        self.assertNotIn(ancestor, ambient.stdout.split(),
                         "the graft did not hide the ancestor, so this arm "
                         "measures an ordinary two-commit repo")

        # THE UPSTREAM IS A RESOLVED SHA, and that is a CONTRACT and not a
        # convenience. The builder behind this door refuses anything `_sha`
        # does not accept and answers (None, True) -- no index, no error -- so
        # a REF NAME here makes the arm read "the window was incomplete" when
        # nothing was ever built. Production normalises the upstream to a sha
        # before this call for the same reason; the arm does too.
        index, flag = landreq._landed_index(gitdir, child)
        self.assertIsNotNone(index, "no index was built at all, so the "
                                    "membership assertion below would be "
                                    "measuring nothing")
        self.assertIn(ancestor_id, index,
                      "the index certified a window it never enumerated")
        # FALSY, NOT A PARTICULAR FALSE. Completeness is the PROPERTY; which
        # falsy value carries it belongs to the builder and has already
        # changed once underneath this arm.
        self.assertFalse(flag, "a complete window reported incompleteness")

    def test_a_legacy_graft_file_child_is_not_a_root(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual against the root answer is the absence half; the HASHED assertion on the un-grafted measurement is its unconditional positive control
        """REWRITER TWO, AND IT IS THE ONE --no-replace-objects DOES NOT
        COVER. The deprecated info/grafts file rewrites ancestry through a
        different mechanism than replace refs, so disabling replacement alone
        still reads this child as parentless; only pointing the graft file at
        an empty source recovers its parent."""
        repo, g = self._repo()
        with io.open(os.path.join(repo, "f"), "w") as fh:
            fh.write("1\n")
        g("add", "f"); g("commit", "-qm", "one")
        with io.open(os.path.join(repo, "f"), "w") as fh:
            fh.write("2\n")
        g("add", "f"); g("commit", "-qm", "two")
        child = g("rev-parse", "HEAD")
        info = os.path.join(repo, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with io.open(os.path.join(info, "grafts"), "w") as fh:
            fh.write(child + "\n")
        gitdir = os.path.join(repo, ".git")

        # MUST-HIT, AND IT IS TWO CLAIMS. The graft is in effect, AND
        # --no-replace-objects does NOT undo it -- which is the entire reason
        # this arm exists separately from the replace one.
        plain = subprocess.run(("git", "-C", repo, "log", "--format=%P",
                                "-1", child), capture_output=True, text=True)
        self.assertEqual(plain.stdout.strip(), "",
                         "the graft file did not take; the arm is vacuous")
        norepl = subprocess.run(
            ("git", "-C", repo, "--no-replace-objects", "log", "--format=%P",
             "-1", child), capture_output=True, text=True)
        self.assertEqual(
            norepl.stdout.strip(), "",
            "--no-replace-objects DID undo the graft file, which would make "
            "the separate graft isolation unnecessary; the contract's shape "
            "rests on it not doing so")

        # INERTNESS, as in the replace arm, and the claim is sharper here:
        # --no-replace-objects provably does NOT undo this rewriter (asserted
        # above), so an identical answer can only come from the empty graft
        # file the object view supplies.
        with_graft = landreq._measured_patch_id(gitdir, child)
        self.assertEqual(landreq._root_patch_id(gitdir, child),
                         (landreq.UNMEASURED, None),
                         "the root helper accepted a GRAFT-FILE child")
        os.remove(os.path.join(info, "grafts"))
        without = landreq._measured_patch_id(gitdir, child)
        self.assertEqual(with_graft, without,
                         "the LEGACY GRAFT FILE moved the patch-id instrument")
        self.assertEqual(without[0], landreq.HASHED)

    def test_git_really_does_hide_the_parent_from_traversal_when_shallow(self):
        """REWRITER THREE, and the one with no bypass at all: the parent
        objects are genuinely absent, so the refusal is coarse by necessity.

        This arm also carries the claim every other one leans on -- that a
        shallow boundary is parentless to traversal while its object names a
        parent -- because if that were false the whole discriminator would be
        solving a problem that does not exist.
        """
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        src = os.path.join(root, "shallow-src")
        os.makedirs(src)

        def must(*argv):
            """Every setup step is CHECKED. A fixture that half-built and
            then skipped would excuse its own failure and report nothing."""
            r = subprocess.run(("git", "-C", src) + argv,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0,
                             "fixture setup failed: git %s -> %s"
                             % (" ".join(argv), r.stderr.strip()))
            return r.stdout.strip()

        must("init", "-q")
        must("config", "user.email", "t@example.com")
        must("config", "user.name", "T")
        with io.open(os.path.join(src, "f"), "w") as fh:
            fh.write("one\n")
        must("add", "f"); must("commit", "-qm", "one")
        with io.open(os.path.join(src, "f"), "w") as fh:
            fh.write("two\n")
        must("add", "f"); must("commit", "-qm", "two")

        dst = os.path.join(root, "shallow-dst")
        rc = subprocess.run(("git", "clone", "-q", "--depth", "1",
                             "file://" + src, dst), capture_output=True,
                            text=True)
        self.assertEqual(
            rc.returncode, 0,
            "the shallow clone did not build, so this arm would have SKIPPED "
            "and silently excused the fixture the contract rests on: %s"
            % rc.stderr.strip())
        head = subprocess.run(("git", "-C", dst, "rev-parse", "HEAD"),
                              capture_output=True, text=True).stdout.strip()
        parents = subprocess.run(
            ("git", "-C", dst, "log", "--format=%P", "-1", head),
            capture_output=True, text=True).stdout.strip()
        obj = subprocess.run(("git", "-C", dst, "cat-file", "commit", head),
                             capture_output=True, text=True).stdout
        self.assertEqual(
            parents, "",
            "the boundary was NOT parentless to traversal, so the confusion "
            "this discriminator guards against does not exist as described")
        self.assertIn(
            "\nparent ", "\n" + obj.split("\n\n", 1)[0],
            "the boundary object did NOT name a parent, so there is nothing "
            "for the traversal view to be hiding")

        state, pid = landreq._measured_patch_id(dst + "/.git", head)
        self.assertEqual((state, pid), (landreq.UNMEASURED, None))

    def test_the_parent_record_must_be_framed_and_self_identifying(self):
        """EMPTY OUTPUT IS NEVER THE EMPTY-PARENT RECORD.

        `--format=%P` alone answers "" both for a root and for a call that
        produced nothing -- the same absence-means-negative substitution this
        module exists to remove, one level down. The record is framed as
        `%H|%P`, so the parent field is explicitly PRESENT and empty, and the
        identity travels with it so a reply about a different commit is
        refused rather than read.

        These four are envelope logic and are the only mocked cases left in
        this family; everything about git's PARSE is measured against real
        repositories above.
        """
        pinned = "a" * 40
        rejects = {
            "no output at all": "",
            "no separator": pinned + "\n",
            "a different commit": "b" * 40 + "|\n",
            "two records": pinned + "|\n" + "b" * 40 + "|\n",
            # THE ONE THAT LOOKS EXACTLY LIKE A ROOT. A child's
            # `<pin>|<parent>` truncated right after the pipe is
            # byte-identical to a root's complete record minus its
            # terminator, so the terminator is the only thing that separates
            # "no parents" from "we stopped reading before the parents".
            "truncated after the separator": pinned + "|",
            "truncated mid-parent": pinned + "|" + "c" * 12,
        }
        for name, record in rejects.items():
            with self.subTest(record=name):
                with mock.patch.object(landreq, "_git", self._git_script(
                        {"cat-file": _Ok("commit\n"),
                         "--is-shallow-repository": _Ok("false\n"),
                         "log": _Ok(record)})):
                    self.assertIsNone(
                        landreq._is_parentless(self.GITDIR, pinned),
                        "a record that does not positively state THIS "
                        "commit's empty parent list (%s) was read as "
                        "parentlessness" % name)

        # MUST-HIT: the well-formed record for this identity DOES answer, and
        # a parented one answers False rather than None -- so the refusals
        # above are about the envelope, not a reader that never decides.
        with mock.patch.object(landreq, "_git", self._git_script(
                {"cat-file": _Ok("commit\n"),
                 "--is-shallow-repository": _Ok("false\n"),
                 "log": _Ok(pinned + "|\n")})):
            self.assertIs(landreq._is_parentless(self.GITDIR, pinned), True)
        with mock.patch.object(landreq, "_git", self._git_script(
                {"cat-file": _Ok("commit\n"),
                 "--is-shallow-repository": _Ok("false\n"),
                 "log": _Ok(pinned + "|" + "c" * 40 + "\n")})):
            self.assertIs(landreq._is_parentless(self.GITDIR, pinned), False)

    def test_a_shallow_repository_refuses_parentlessness_outright(self):
        """The coarse policy, stated as an arm: `is-shallow` anything but a
        well-formed `false` is UNKNOWN, including a reply we could not read.
        This deliberately sacrifices root availability in shallow clones
        rather than mint an identity from a tree whose parents are absent."""
        pinned = "a" * 40
        for name, reply in (("true", _Ok("true\n")),
                            ("unreadable", _Fail("")),
                            ("empty", _Ok(""))):
            with self.subTest(is_shallow=name):
                with mock.patch.object(landreq, "_git", self._git_script(
                        {"cat-file": _Ok("commit\n"),
                         "--is-shallow-repository": reply,
                         "log": _Ok(pinned + "|\n")})):
                    self.assertIsNone(
                        landreq._is_parentless(self.GITDIR, pinned),
                        "a repository whose shallow state answered %r was "
                        "allowed to declare a root" % name)

    def test_an_object_that_is_not_a_commit_is_unmeasured(self):
        """The type question is git's parse, not a shape I recognise."""
        pinned = "a" * 40
        for name, reply in (("a tree", _Ok("tree\n")),
                            ("unreadable", _Fail("")),
                            ("empty", _Ok(""))):
            with self.subTest(cat_file_t=name):
                with mock.patch.object(landreq, "_git", self._git_script(
                        {"cat-file": reply,
                         "--is-shallow-repository": _Ok("false\n"),
                         "log": _Ok(pinned + "|\n")})):
                    self.assertIsNone(
                        landreq._is_parentless(self.GITDIR, pinned),
                        "an object typed %r was treated as a commit" % name)

    def test_a_reintroduced_root_change_is_found_in_a_real_index(self):
        """THE ORIGINAL GEOMETRY, END TO END, THROUGH REAL GIT.

        The defect this whole slice exists for is not visible in a
        measurement arm: it needs the root's id to travel into a NONEMPTY
        index and be looked up. Root R adds a file; T adds an unrelated one;
        a later commit deletes R's file and re-adds it, so the re-add's
        patch-id EQUALS R's addition. Trunk holds R and T.

        With the root hashed, the lookup HITS and presence is provable. With
        the root dropped -- the behaviour before this slice -- the index is
        nonempty, holds T only, reports itself COMPLETE, and the same lookup
        is a miss that the ladder renders ABSENT for content that is on
        trunk. The arm asserts the hit AND that the index carries both ids,
        so a version that indexed nothing could not satisfy it either.
        """
        root_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root_dir, True)
        repo = os.path.join(root_dir, "reintro")
        os.makedirs(repo)

        def g(*argv):
            r = subprocess.run(("git", "-C", repo) + argv,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0,
                             "fixture setup failed: git %s -> %s"
                             % (" ".join(argv), r.stderr.strip()))
            return r.stdout.strip()

        def write(name, text):
            with io.open(os.path.join(repo, name), "w") as fh:
                fh.write(text)

        g("init", "-q")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "T")
        write("x", "the root content\n")
        g("add", "x"); g("commit", "-qm", "root")
        root_sha = g("rev-parse", "HEAD")
        write("y", "unrelated\n")
        g("add", "y"); g("commit", "-qm", "second")
        trunk = g("rev-parse", "HEAD")
        # delete and re-add: the re-add reproduces the root's own addition
        g("rm", "-q", "x"); g("commit", "-qm", "drop x")
        write("x", "the root content\n")
        g("add", "x"); g("commit", "-qm", "put x back")
        reintro = g("rev-parse", "HEAD")

        gitdir = os.path.join(repo, ".git")
        root_state, root_pid = landreq._measured_patch_id(gitdir, root_sha)
        self.assertEqual(root_state, landreq.HASHED,
                         "the root was not measured, so the geometry below "
                         "cannot be exercised at all")
        again_state, again_pid = landreq._measured_patch_id(gitdir, reintro)
        self.assertEqual(again_state, landreq.HASHED)
        self.assertEqual(
            again_pid, root_pid,
            "the re-added file did not reproduce the root's patch-id, so "
            "this fixture does not build the collision it is about")

        index, why = landreq._stored_patch_index(gitdir, trunk)
        self.assertIsNone(why, "the two-commit window reported incomplete")
        self.assertEqual(
            len(index), 2,
            "the trunk window should carry BOTH the root and the second "
            "commit; a version that indexed neither would pass a hit-only "
            "assertion vacuously (%r)" % (sorted(index.values()),))
        self.assertEqual(
            index.get(again_pid), root_sha,
            "the re-added content's id missed a COMPLETE trunk index that "
            "does carry it -- the ladder above renders that as ABSENT for "
            "content sitting on trunk")

    # ---- the two index controls (C1 and C2) ----

    @staticmethod
    def _index_over(wanted_sha, wanted_pid, other_sha, other_pid, unhashable):
        """Build the real index over two trunk commits, one of which git
        refuses to hash. Returns (index, why)."""
        def measured(_gitdir, sha, _trunk=None):
            if sha == unhashable:
                return landreq.UNMEASURED, None
            return landreq.HASHED, (wanted_pid if sha == wanted_sha
                                    else other_pid)

        with mock.patch.object(landreq, "_git",
                               lambda *a, **k: _Ok(
                                   wanted_sha + "\n" + other_sha + "\n")), \
                mock.patch.object(landreq, "_measured_patch_id", measured):
            return landreq._stored_patch_index("/nonexistent/.git", "trunk")

    def test_c1_a_failed_hash_elsewhere_still_proves_presence(self):
        """A HIT IS A HIT UNDER EVERY CAUSE.

        The wanted id WAS successfully measured against a commit on trunk.
        Nothing the walk failed to reach can take that back, so an incomplete
        window still proves PRESENCE -- and a cure that refused to answer
        from an incomplete index would break exactly this.
        """
        wanted, other = "a" * 40, "b" * 40
        wpid, opid = "1" * 40, "2" * 40
        index, why = self._index_over(wanted, wpid, other, opid,
                                      unhashable=other)
        self.assertEqual(index.get(wpid), wanted)
        self.assertEqual(
            why, landreq.INDEX_UNMEASURED,
            "the failed hash on the unrelated commit left no trace, so the "
            "window reports itself complete when it is not")

        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda *_a, **_k: wpid), \
                mock.patch.object(landreq, "_git", return_value=_Ok("")):
            sha, reason = landreq._stored_patch_on_trunk(
                "/nonexistent/.git", wpid, "trunk", index, why)
        self.assertEqual(
            sha, wanted,
            "an incomplete window refused a POSITIVE it had measured "
            "(reason %r)" % (reason,))

    def test_c2_a_failed_hash_of_the_wanted_commit_is_not_an_absence(self):
        """THE CASE THAT USED TO SLIP, AND IT LOOKS EXACTLY LIKE SUCCESS.

        The index is NONEMPTY -- another commit hashed fine -- and the wanted
        id is missing from it. A complete window that has never heard of the
        id looks identical. `if pid:` dropped the unhashable commit silently,
        the walk reached the end, and the miss was reported as ABSENT: a
        negative asserted about the one commit the index never read.
        """
        wanted, other = "a" * 40, "b" * 40
        wpid, opid = "1" * 40, "2" * 40
        index, why = self._index_over(wanted, wpid, other, opid,
                                      unhashable=wanted)
        self.assertEqual(
            index, {opid: other},
            "the fixture must leave the index NONEMPTY, or this arm cannot "
            "tell an incomplete miss from an unreadable index")
        self.assertEqual(why, landreq.INDEX_UNMEASURED)

        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_git", return_value=_Ok("")):
            sha, reason = landreq._stored_patch_on_trunk(
                "/nonexistent/.git", wpid, "trunk", index, why)
        self.assertIsNone(sha)
        self.assertIsNotNone(
            reason,
            "a miss against a window with a hole in it returned the SILENT "
            "no-answer, which the ladder above renders as ABSENT")
        self.assertIn("UNKNOWN", reason)
        self.assertIn("could not be hashed", reason)

        # MUST-HIT: the SAME miss against a COMPLETE window is the silent
        # no-answer, so the reason above is the hole and not a reader that
        # refuses every miss.
        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_git", return_value=_Ok("")):
            sha2, reason2 = landreq._stored_patch_on_trunk(
                "/nonexistent/.git", wpid, "trunk", {opid: other}, None)
        self.assertEqual((sha2, reason2), (None, None))

    def test_the_miss_reason_names_the_cause_it_actually_had(self):
        """THREE CAUSES, THREE SENTENCES. The text said "the scan was CAPPED"
        for every reason a window could fall short, and two of the three were
        never a cap."""
        seen = {}
        for why in (landreq.INDEX_CAPPED, landreq.INDEX_BUDGET,
                    landreq.INDEX_UNMEASURED):
            with _tmp_rescue_store(), \
                    mock.patch.object(landreq, "_git",
                                      return_value=_Ok("")):
                _sha, reason = landreq._stored_patch_on_trunk(
                    "/nonexistent/.git", "1" * 40, "trunk", {"2" * 40: "b"},
                    why)
            seen[why] = reason
            self.assertIn("UNKNOWN", reason or "")
        self.assertEqual(
            len(set(seen.values())), 3,
            "two causes rendered the same sentence, so a reader cannot tell "
            "a bounded window from an unread commit: %r" % (seen,))
        self.assertIn("CAPPED", seen[landreq.INDEX_CAPPED])
        self.assertIn("deadline", seen[landreq.INDEX_BUDGET])
        self.assertIn("hashed", seen[landreq.INDEX_UNMEASURED])


class LandingStoreServesTheRealProofTest(unittest.TestCase):
    """END-TO-END THROUGH `_landing_proof`, NOT THROUGH A COPY OF ITS RULES.

    A review of this lane: the acceptance must invoke the real
    predicate. Every earlier check here re-implemented the reader's two legs,
    and a reimplementation agrees with the original right up until one of them
    changes — which is the failure this row is made of.
    """

    def test_a_stored_landing_is_what_the_real_proof_consults(self):
        gitdir = "/nonexistent/.git"
        pid, sha = "e" * 40, "f" * 40
        seen = {}

        def fake_patch_id(_g, target):
            seen["hashed"] = target
            return pid                       # the target CARRIES the key

        def fake_git(_g, *args, **_k):
            if args[:1] == ("merge-base",):
                seen["ancestry"] = args
                return _Ok("")               # rc 0: still on trunk
            return _Ok(sha)

        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"%s\t%s" % (gitdir, pid): sha}), \
                _hash_faces(fake_patch_id), \
                mock.patch.object(landreq, "_git", fake_git):
            got, why = landreq._stored_patch_on_trunk(gitdir, pid, "origin/main")

        self.assertEqual(got, sha, why)
        self.assertEqual(seen.get("hashed"), sha,
                         "the reader never re-hashed the target — the second "
                         "leg of its contract did not run")
        self.assertIn("merge-base", str(seen.get("ancestry")),
                      "the reader never re-checked ancestry")

        # THE DISCRIMINATING HALF, on the SAME reader and the same entry:
        # flip ONLY what the target hashes to, and the real proof must refuse
        # it. Without this the arm above passes for a reader that returns its
        # cached value unconditionally, which is the bug the second leg exists
        # to prevent.
        def wrong_patch_id(_g, _target):
            return "9" * 40                  # target does NOT carry the key

        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"%s\t%s" % (gitdir, pid): sha}), \
                _hash_faces(wrong_patch_id), \
                mock.patch.object(landreq, "_git", fake_git):
            got2, why2 = landreq._stored_patch_on_trunk(gitdir, pid,
                                                        "origin/main")
        self.assertIsNone(got2, "a poisoned entry was served as a landing")
        self.assertTrue(why2, "a refusal must say why")

    def test_the_writer_refuses_what_that_reader_would_reject(self):
        """THE ADMISSION RULE IS THE READER'S, driven through the REAL WRITER.

        The earlier version of this arm called `_readable_correlation` and
        stopped there — it proved the predicate, never that any writer
        consults it, which is precisely the gap that let the land feed write
        entries the reader refuses. `_remember_landing` is the writer, so
        `_remember_landing` is what runs here.
        """
        gitdir = "/nonexistent/.git"
        pid, tip, head = "e" * 40, "f" * 40, "a" * 40

        def only_tip_carries(_g, sha):
            return pid if sha == tip else "9" * 40

        # The tip carries the key and is on trunk: STORED, and stored as the
        # TIP — never as `pinned`, which carries a different id.
        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                mock.patch.object(landreq, "_tip_patch_id", return_value=pid), \
                _hash_faces(only_tip_carries):
            landreq._remember_landing(gitdir, tip, head)
            self.assertEqual(list(pk.read_json(store, {}).values()), [tip])

        # Nothing carries the key: the writer stores NOTHING rather than
        # filing the new trunk head under it. An absent entry costs a
        # re-derivation; a wrong one costs a permanent refusal.
        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                mock.patch.object(landreq, "_tip_patch_id", return_value=pid), \
                _hash_faces(lambda *_a, **_k: "9" * 40):
            landreq._remember_landing(gitdir, tip, head)
            self.assertEqual(pk.read_json(store, {}), {},
                             "the writer filed a key against a sha that does "
                             "not carry it — the poisoning this closes")

    def test_a_readable_entry_is_never_demoted_by_a_later_land(self):
        """THE POISONING WAS AN OVERWRITE, not a bad first write.

        Measured on fdca109: a good pid_A -> carrier_A was replaced
        by pid_A -> a newer trunk head carrying a DIFFERENT id, the reader then
        refused, and the backfill skipped the key as present. Permanent.
        """
        gitdir = "/nonexistent/.git"
        pid, good, newer = "e" * 40, "f" * 40, "a" * 40
        key = "%s\t%s" % (gitdir, pid)

        trunk = "origin/main"
        other_pid, other_sha = "d" * 40, "1" * 40
        # EACH CARRIER HASHES TO ITS OWN KEY. Getting this wrong is what made
        # the beyond-cap arm assert (1,1,False) and yield (0,1,False): a key
        # whose carrier hashes to something else is REFUSED, correctly, and
        # the arm then measures the admission gate instead of its subject.
        carries = {good: pid, other_sha: other_pid}

        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda _g, sha: carries.get(sha, "9" * 40)):
            fresh = "%s\t%s" % (gitdir, other_pid)
            pk.write_json(store, {key: good})
            landreq._merge_rescue_entries(gitdir, {pid: newer}, trunk)
            # THE SECOND CALL IS THE POSITIVE CONTROL, and both are read by
            # ONE assertion over the WHOLE store. "The good entry is still
            # there" is satisfied by a writer that does nothing at all — the
            # fail-open wrapper makes a swallowed exception look exactly like
            # a refusal to demote — so a fresh readable key must land in the
            # same fixture, through the same door, for this to mean anything.
            landreq._merge_rescue_entries(gitdir, {other_pid: other_sha}, trunk)
            self.assertEqual(pk.read_json(store, {}),
                             {key: good, fresh: other_sha},
                             "either a usable correlation was demoted, or "
                             "the writer wrote nothing at all")

    def test_the_backfill_repairs_a_key_the_reader_would_refuse(self):
        """AND THE OTHER HALF: a present key is not an answered key.

        The old filter skipped every key already in the store, so a poisoned
        entry was never revisited and the backfill reported nothing to do.
        """
        gitdir = "/nonexistent/.git"
        pid, tip, bad = "b" * 40, "c" * 40, "9" * 40
        key = "%s\t%s" % (gitdir, pid)

        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda _g, sha: pid if sha == tip else "7" * 40), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            pk.write_json(store, {key: bad})
            self.assertEqual(pk.read_json(store, {}), {key: bad},
                             "MUST-HIT: the fixture never reached the store, "
                             "so a 'repair' below would be measuring nothing")
            stored, examined, _ = landreq.backfill_landing_store(
                gitdir, "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            self.assertEqual(pk.read_json(store, {}), {key: tip},
                             "the poisoned entry was not replaced by the sha "
                             "that answers for its key")
        self.assertEqual((stored, examined), (1, 1),
                         "the backfill skipped an existing key it could have "
                         "repaired")

    def test_an_unreadable_repo_does_not_answer_for_an_entry(self):
        """ANCESTRY IS TRI-STATE AND `!= ANCESTOR` FLATTENS IT.

        `_ancestry` answers UNDETERMINED for exit 128 — a bad or missing
        object, a corrupt repository — and for a timeout or an OSError. Folding
        that into "not an ancestor" asserts a negative on an unknown, and
        False here DEMOTES a held entry and licenses a rewrite: a repository
        nobody could read would be authorising a write.

        TWO UNCONDITIONAL POSITIVE CONTROLS ON THE SAME CALL, because an
        UNKNOWN-only assertion is satisfied by a helper that answers UNKNOWN
        to everything — which would be a strictly worse function passing this
        arm.
        """
        with mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.UNDETERMINED), \
                _hash_faces(lambda *_a, **_k: "b" * 40):
            unknown = landreq._answers_for(
                "/nonexistent/.git", "b" * 40, "c" * 40, "origin/main")
        self.assertIsNone(
            unknown,
            "a repository that could not be read answered %r, and anything "
            "but None here licenses a rewrite" % (unknown,))
        # CONTROL 1: a MEASURED non-ancestor must still refuse.
        with mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR), \
                _hash_faces(lambda *_a, **_k: "b" * 40):
            measured_no = landreq._answers_for(
                "/nonexistent/.git", "b" * 40, "c" * 40, "origin/main")
        self.assertIs(measured_no, False,
                      "MUST-HIT: a measured non-ancestor must still be a "
                      "refusal, not an unknown (%r)" % (measured_no,))
        # CONTROL 2: a MEASURED ancestor carrying the id must still accept.
        with mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.ANCESTOR), \
                _hash_faces(lambda *_a, **_k: "b" * 40):
            measured_yes = landreq._answers_for(
                "/nonexistent/.git", "b" * 40, "c" * 40, "origin/main")
        self.assertIs(measured_yes, True,
                      "MUST-HIT: a measured ancestor carrying the patch-id "
                      "must still be accepted (%r)" % (measured_yes,))

    def test_only_a_measured_refusal_evicts_a_held_entry(self):
        """THE ONLY CALL SITE THAT DESTROYS SOMETHING.

        A held entry is left alone when the reader still accepts it (True)
        AND when the question could not be asked (None); only a MEASURED
        refusal (False) may replace it. `if _answers_for(...)` cannot express
        that — it treats None exactly like False and rewrites on a question
        nobody answered.
        """
        held, fresh, pid = "a" * 40, "d" * 40, "b" * 40
        key = "%s\t%s" % ("/nonexistent/.git", pid)

        def run(answer_for_held):
            def fake(_g, _pid, sha, _trunk):
                return True if sha == fresh else answer_for_held
            with _tmp_rescue_store() as path, \
                    mock.patch.object(landreq, "_answers_for", fake):
                pk.write_json(path, {key: held})
                landreq._merge_rescue_entries(
                    "/nonexistent/.git", {pid: fresh}, "origin/main")
                return (pk.read_json(path, {}) or {}).get(key)

        self.assertEqual(
            run(None), held,
            "an UNANSWERED question evicted a held entry — that is a write "
            "authorised by work that never ran")
        self.assertEqual(
            run(True), held,
            "MUST-HIT: an entry the reader still accepts must be left alone")
        # MUST-HIT ON THE OTHER SIDE: without this, a function that never
        # writes anything passes both assertions above.
        self.assertEqual(
            run(False), fresh,
            "MUST-HIT: a MEASURED refusal must be replaced, or this arm "
            "would pass against a writer that never writes")

    def test_an_unreadable_hash_does_not_replace_a_held_entry(self):
        """THROUGH THE REAL HELPERS, STUBBING ONLY THE PRODUCER.

        The other write-door arms mock `_answers_for`, which is the DECISION
        seam -- they prove the writer obeys a verdict and say nothing about
        how the verdict is reached. This one stubs `_patch_id` and `_ancestry`
        (the producers) and lets `_answers_for` and `_readable_correlation`
        run for real, so it exercises the path where an unreadable hash could
        still become a refusal.

        `_patch_id` is fail-open and answers None for a spawn that failed AND
        for a range with no diff. Mapping that to False makes the content leg
        say "this sha does not answer for that pid", which DEMOTES the held
        entry and licenses a rewrite -- a live entry replaced on work that
        never ran.
        """
        held, fresh, pid = "a" * 40, "d" * 40, "b" * 40
        key = "%s\t%s" % ("/nonexistent/.git", pid)

        def run(hash_of_held):
            def hashes(gitdir, sha, trunk_ref=None):
                return pid if sha == fresh else hash_of_held
            with _tmp_rescue_store() as path, \
                    mock.patch.object(landreq, "_ancestry",
                                      return_value=landreq.ANCESTOR), \
                    _hash_faces(hashes):
                pk.write_json(path, {key: held})
                landreq._merge_rescue_entries(
                    "/nonexistent/.git", {pid: fresh}, "origin/main")
                return (pk.read_json(path, {}) or {}).get(key)

        self.assertEqual(
            run(None), held,
            "the held entry's hash could not be READ and it was replaced "
            "anyway -- an unreadable answer became a measured refusal")
        self.assertEqual(
            run(pid), held,
            "MUST-HIT: an entry the reader still accepts must be left alone")
        # MUST-HIT IN THE OTHER DIRECTION, or a writer that never writes
        # passes both assertions above: a MEASURED mismatch really does evict.
        self.assertEqual(
            run("q" * 40), fresh,
            "MUST-HIT: a held entry whose hash MEASURABLY differs was not "
            "replaced, so this arm cannot tell preservation from paralysis")

    def test_an_ancestor_tip_that_is_not_the_carrier_falls_back(self):
        """ANCESTRY IS THE FIRST LEG AND NOT THE ONLY ONE.

        A rebase or cherry-pick lands different bytes under the same review,
        so a reviewed tip can be ON trunk while hashing to a DIFFERENT id from
        the one the receipt stored. Selecting it on ancestry alone hands the
        write door an entry its admission check refuses, and the receipt is
        dropped with no attempt at the carrier the index may hold -- a proof
        lost to the rung that was supposed to find it fastest.
        """
        pid, tip, carrier = "b" * 40, "c" * 40, "e" * 40

        def hashes(gitdir, sha, trunk_ref=None):
            return {tip: "q" * 40, carrier: pid}.get(sha)

        with _tmp_rescue_store() as path, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(hashes), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({pid: carrier}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            result = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            held = (pk.read_json(path, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertIsNotNone(result, "the ledger was readable")
        self.assertEqual(
            held, carrier,
            "the on-trunk tip does NOT carry this receipt's id, and the "
            "index held a carrier that does -- the backfill stored %r"
            % (held,))

        # MUST-HIT ON THE FIRST LEG: when the tip DOES carry the id it is
        # selected directly and the index is never walked, or this arm would
        # pass against a backfill that ignored ancestry entirely.
        def tip_carries(gitdir, sha, trunk_ref=None):
            return pid

        with _tmp_rescue_store() as path2, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(tip_carries), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  side_effect=AssertionError(
                                      "the index was walked for a tip that "
                                      "already carries the receipt's id")), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            held2 = (pk.read_json(path2, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertEqual(held2, tip,
                         "MUST-HIT: a tip that DOES carry the id was not "
                         "stored, so the fallback above proves nothing about "
                         "ancestry-first (%r)" % (held2,))

    def test_an_unread_hash_on_an_ancestor_tip_decides_nothing(self):
        """UNKNOWN ON THE SECOND LEG IS NOT A REASON TO ASK THE INDEX.

        If the tip's hash could not be read, a MISS in the index would stand
        as absence for a receipt nobody actually asked about, and storing the
        tip would write on a question git did not answer. Neither is allowed;
        the receipt stays exactly as unprovable as it already was.
        """
        pid, tip, carrier = "b" * 40, "c" * 40, "e" * 40

        # THE CARRIER MUST BE ADMISSIBLE OR THIS ARM CANNOT SEE THE BRANCH.
        # If the tip's hash is unreadable AND the carrier's is too, the write
        # door refuses the entry on its own and nothing is stored either way
        # -- the arm passes against a reader that fell straight through to
        # the index. Only the TIP is unreadable here; the carrier hashes to
        # the receipt's id and would be written the moment the fallback ran.
        def only_the_tip_is_unreadable(gitdir, sha, trunk_ref=None):
            return None if sha == tip else pid

        with _tmp_rescue_store() as path, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(only_the_tip_is_unreadable), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({pid: carrier}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            held = (pk.read_json(path, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertIsNone(
            held,
            "an UNREAD hash on the tip fell through to the index and stored "
            "%r, which is a carrier chosen for a receipt nobody could ask "
            "about" % (held,))
        # MUST-HIT: the very same carrier IS stored when the tip's hash reads
        # and simply does not match, which is the case the fallback exists
        # for. Without this, the assertion above passes against a backfill
        # that never falls back at all.
        def the_tip_hashes_to_something_else(gitdir, sha, trunk_ref=None):
            return "q" * 40 if sha == tip else pid

        with _tmp_rescue_store() as path2, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(the_tip_hashes_to_something_else), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({pid: carrier}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            reachable = (pk.read_json(path2, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertEqual(
            reachable, carrier,
            "MUST-HIT: the fallback never stores this carrier at all, so the "
            "None above says nothing about the UNKNOWN path (%r)"
            % (reachable,))

    # THE FOUR ARMS BELOW ARE ABOUT THE RETURNED STATUS, NOT THE STORE, and
    # they exist because every arm above this line asserts on what was
    # WRITTEN. A backfill that decides nothing writes nothing, which is
    # exactly what a backfill with nothing to do also does -- so the whole
    # UNKNOWN family was verifiable only through a channel that cannot
    # distinguish "I could not answer" from "there was nothing to answer".
    # The third element is the channel that can, and `incomplete=False` is
    # the sentence "there is no work left", which a caller acts on by never
    # asking again.
    #
    # THE READABLE ARM IS NOT SYMMETRY. Without a case that MUST report
    # complete, `incomplete = True` hardwired at the top of the function
    # passes all three UNKNOWN arms.

    def test_an_unread_tip_hash_leaves_the_run_incomplete(self):
        """AN ELIGIBLE RECEIPT NOBODY COULD JUDGE IS NOT A FINISHED HISTORY.

        One receipt, a readable pin, a tip that IS an ancestor, and a hash
        git could not read. Everything about this row is available except
        the one question that would settle it, so the run has work left --
        and it used to report (0, 1, False): looked at one, finished.
        """
        pid, tip = "b" * 40, "c" * 40

        def only_the_tip_is_unreadable(gitdir, sha, trunk_ref=None):
            return None if sha == tip else pid

        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(only_the_tip_is_unreadable), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            stored, examined, incomplete = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
        self.assertEqual((stored, examined), (0, 1))
        self.assertTrue(
            incomplete,
            "an unreadable tip hash reported a FINISHED history after "
            "deciding nothing, so the caller never comes back to this "
            "receipt (stored=%r examined=%r)" % (stored, examined))

    def test_an_unreadable_held_entry_leaves_the_run_incomplete(self):
        """AND THIS ONE REPORTED THAT IT HAD NOT EVEN LOOKED.

        The held-entry re-check skips on TRUE (settled) and on NONE (never
        asked), and the skip lands BEFORE `examined` is incremented -- so a
        run whose only receipt could not be re-checked returned (0, 0,
        False): stored nothing, examined nothing, nothing left to do. Three
        numbers, all of them reading like an empty estate.

        `examined` is deliberately NOT changed here. It counts work, the
        flag carries doubt, and neither substitutes for the other.
        """
        pid, held = "b" * 40, "f" * 40
        with _tmp_rescue_store() as path, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda *_a, **_k: None), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            key = "%s\t%s" % ("/nonexistent/.git", pid)
            pk.write_json(path, {key: held})
            stored, examined, incomplete = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": "c" * 40}])
            still = (pk.read_json(path, {}) or {}).get(key)
        self.assertEqual(
            still, held,
            "UNKNOWN must PRESERVE the held entry; this door is unchanged "
            "and the arm fails loudly if the status work touched it (%r)"
            % (still,))
        self.assertTrue(
            incomplete,
            "an unreadable held re-check reported a finished history "
            "(stored=%r examined=%r)" % (stored, examined))

    def test_an_EMPTY_ranged_held_entry_is_REPLACED_not_preserved(self):  # noqa: VACUOUS_ASSERTION — the stored value is asserted EQUAL to the live tip and stored EQUAL to 1 before the incomplete check; the assertFalse is about the FLAG on a run already proven to have written
        """THE WRITER'S OWN TRAVERSAL, not the reader's classification.

        The arms beside `_readable_correlation` prove it ANSWERS False on an
        empty range. They stop there, and the question that matters is what
        the WRITER does with that answer: an entry whose target git LOOKED at
        and found no diff carries no patch-id at all, so it cannot be carrying
        the non-empty one it was cached under -- a MEASURED negative, which
        licenses the rewrite. Its neighbour above proves the opposite half on
        the same door: UNMEASURED preserves and leaves the run incomplete.

        THIS IS ALSO THE FIRST CONTROL THAT USES `_EMPTY`. The migration gave
        the fixtures a spelling for "git answered and the range was empty" and
        nothing exercised it, so the third state was reachable in the helper
        and unreached by any arm.
        """
        pid, held, live = "b" * 40, "f" * 40, "c" * 40
        with _tmp_rescue_store() as path, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda _g, sha: _EMPTY if sha == held else pid), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            key = "%s\t%s" % ("/nonexistent/.git", pid)
            pk.write_json(path, {key: held})
            stored, examined, incomplete = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": live}])
            now = (pk.read_json(path, {}) or {}).get(key)
        self.assertEqual(
            now, live,
            "a MEASURED empty range did not license the rewrite: the dead "
            "entry %r survived and the store stays unreadable forever (%r)"
            % (held, now))
        self.assertEqual(stored, 1)
        self.assertFalse(
            incomplete,
            "every question this run asked was ANSWERED -- reporting work "
            "left makes a decided run look undecided (stored=%r examined=%r)"
            % (stored, examined))

    def test_scripted_measurement_reaches_the_backfill_writer(self):
        """Keep measurement, correlation, answers and the store writer real.

        Only git responses and ancestry are scripted. An empty patch-id
        result must replace the held entry and finish; a failed or unavailable
        result must preserve it and leave work pending. `_hash_faces` cannot
        test this path because it replaces the measurement we need to reach.
        """
        pid, held, live, pinned = (c * 40 for c in "bfcd")
        trunk = "origin/main"
        held_diff = "diff --git a/held b/held\n"
        live_diff = "diff --git a/live b/live\n"
        pin_args = landreq._REF_ARGV + (trunk,)
        diff_args = ("--no-replace-objects", "-c", "diff.noprefix=false",
                     "diff")
        held_args = diff_args + (held + "^.." + held,)
        live_args = diff_args + (live + "^.." + live,)
        pid_args = ("patch-id", "--stable")
        view = {"GIT_GRAFT_FILE": os.devnull}
        pin_call = (pin_args, None, {})
        held_calls = [(held_args, None, view), (pid_args, held_diff, {})]
        live_calls = [(live_args, None, view), (pid_args, live_diff, {})]

        for label, result in (("empty", _Ok("")), ("failed", _Fail()),
                              ("unavailable", None)):
            with self.subTest(measurement=label), _tmp_rescue_store() as path:
                gitdir = os.path.join(os.path.dirname(path), "repo", ".git")
                key = "%s\t%s" % (gitdir, pid)
                pk.write_json(path, {key: held})
                self.assertEqual(pk.read_json(path, {}), {key: held})
                seen = []
                responses = {
                    (pin_args, None): _Ok(pinned + "\n"),
                    (held_args, None): _Ok(held_diff),
                    (live_args, None): _Ok(live_diff),
                    (pid_args, held_diff): result,
                    (pid_args, live_diff): _Ok(pid + " " + live + "\n"),
                }

                def scripted(gd, *args, input_text=None, env=None):
                    seen.append((tuple(args), input_text, dict(env or {})))
                    self.assertEqual(gd, gitdir)
                    question = (tuple(args), input_text)
                    if question not in responses:
                        raise AssertionError("unexpected git question: %r"
                                             % (question,))
                    return responses[question]

                with mock.patch.object(landreq, "_ancestry",
                                       return_value=landreq.ANCESTOR), \
                        mock.patch.object(landreq, "_git", scripted), \
                        mock.patch.object(
                            landreq, "_stored_patch_index",
                            side_effect=AssertionError(
                                "the live candidate needs no index fallback")):
                    outcome = landreq.backfill_landing_store(
                        gitdir, trunk,
                        receipts=[{"patch_id": pid, "reviewed_tip": live}])
                    stored = pk.read_json(path, {})

                if label == "empty":
                    self.assertEqual(stored, {key: live})
                    self.assertEqual(outcome, (1, 1, False))
                    # Held recheck, candidate selection, then the writer's
                    # OWN candidate admission and held-entry demotion checks.
                    expected = ([pin_call] + held_calls + live_calls
                                + live_calls + held_calls)
                else:
                    self.assertEqual(stored, {key: held})
                    self.assertEqual(outcome, (0, 0, True))
                    expected = [pin_call] + held_calls
                # Must-hit on every low-level response, including diff view
                # and patch-id input. Assert outside the writer's conservative
                # exception handling so a swallowed script error cannot look correct.
                self.assertEqual(seen, expected)

    def test_an_unreadable_final_validation_leaves_the_run_incomplete(self):
        """THE LAST DOOR ASKS THE SAME QUESTION AND USED TO SWALLOW IT.

        A candidate that survives the loop is re-judged at the write door by
        the reader's own contract, and `is not True` is the right ADMISSION
        rule there -- an unanswered question is not a yes. It is not a
        status rule: a candidate git refused and a candidate git could not
        judge both leave the plan empty, and only the second means the run
        still owes an answer.

        Reached through the index rather than the tip, so the loop's own
        UNKNOWN branches are not the thing under test.
        """
        pid, tip, carrier = "b" * 40, "c" * 40, "e" * 40

        def the_carrier_is_unreadable(gitdir, sha, trunk_ref=None):
            return None if sha == carrier else "q" * 40

        with _tmp_rescue_store() as path, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(the_carrier_is_unreadable), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({pid: carrier}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            stored, examined, incomplete = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            wrote = (pk.read_json(path, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertIsNone(
            wrote,
            "an unjudgeable candidate was ADMITTED; that door is unchanged "
            "and must still refuse it (%r)" % (wrote,))
        self.assertTrue(
            incomplete,
            "the write door refused a candidate it could not judge and "
            "reported a finished history (stored=%r examined=%r)"
            % (stored, examined))

    def test_every_ancestry_state_at_every_ancestry_door(self):
        """THE CLASS, CLOSED BY ENUMERATION RATHER THAN BY FINDING THE NEXT
        SITE.

        Three doors in this function ask ancestry -- the held re-check, the
        tip rung, and the merge admission -- and each one can get three
        answers: ANCESTOR, NOT_ANCESTOR, UNDETERMINED. Nine cells. Every
        arm written for the incomplete contract before this one pinned
        ancestry to ANCESTOR, so an entire column was untested by
        construction and the UNDETERMINED fall-through at the tip rung
        survived a full review with three UNKNOWN arms passing beside it.

        The rule is one sentence and it holds in every cell: a MEASURED
        answer, ancestor or not, decides something and leaves the run
        complete; an UNANSWERABLE probe decides nothing and the run must
        say so. A fall-through added at any of the three doors later reddens
        here without anyone having to notice the door exists.
        """
        pid = "b" * 40
        tip, held, carrier = "c" * 40, "f" * 40, "e" * 40
        key = "%s\t%s" % ("/nonexistent/.git", pid)

        def run(door, state):
            # ANCESTRY IS ROUTED PER SHA so each door is isolated: the other
            # two are pinned to a MEASURED answer, and only the door under
            # test sees `state`. Without that, one UNDETERMINED anywhere
            # marks the run and the matrix proves nothing about WHICH door.
            subject = {"tip": tip, "held": held, "merge": carrier}[door]

            def ancestry(_g, sha, _trunk):
                if sha == subject:
                    return state
                return landreq.NOT_ANCESTOR if sha == tip \
                    else landreq.ANCESTOR

            index = {pid: carrier} if door == "merge" else {}
            with _tmp_rescue_store() as path, \
                    mock.patch.object(landreq, "_ancestry", ancestry), \
                    _hash_faces(lambda _g, sha, *a: pid), \
                    mock.patch.object(landreq, "_stored_patch_index",
                                      return_value=(index, False)), \
                    mock.patch.object(landreq, "_git",
                                      return_value=_Ok("d" * 40)):
                if door == "held":
                    pk.write_json(path, {key: held})
                return landreq.backfill_landing_store(
                    "/nonexistent/.git", "origin/main",
                    receipts=[{"patch_id": pid, "reviewed_tip": tip}])

        # THE NINE CELLS ABOVE CANNOT REACH THE FOURTH DOOR -- which is a fact
        # about THOSE FIXTURES, not about the code. Production reaches it
        # readily: a MEASURED held refusal, then a tip whose ancestry fails,
        # then a validly indexed candidate arriving at the merge while the
        # held entry is still there. The nine miss it only because of how they
        # are built: in the held cells the index is empty so no candidate
        # reaches the merge at all, and in the merge cells there is no held
        # entry to re-validate.
        #
        # `_merge_rescue_entries` re-validates an existing held entry before
        # overwriting it -- the one call site that DESTROYS something -- so
        # deleting that site's status line would slip past a matrix that
        # stopped at three doors. The fourth is therefore driven directly,
        # with the candidate's own admission pinned to a MEASURED yes so no
        # earlier UNKNOWN can mask the bit under test.
        def run_final_held(state):
            candidate, held_sha = "1" * 40, "2" * 40

            def ancestry(_g, sha, _trunk):
                # The CANDIDATE's admission is measured-positive in every
                # cell; only the HELD re-check varies. Without that pinning a
                # cell could report incomplete for the wrong reason and the
                # matrix would still look right.
                return landreq.ANCESTOR if sha == candidate else state

            with _tmp_rescue_store() as path, \
                    mock.patch.object(landreq, "_ancestry", ancestry), \
                    _hash_faces(lambda _g, sha, *a: pid), \
                    mock.patch.object(landreq, "_git",
                                      return_value=_Ok("d" * 40)):
                pk.write_json(path, {key: held_sha})
                written, incomplete = landreq._merge_rescue_entries(
                    "/nonexistent/.git", {pid: candidate}, "origin/main")
                after = (pk.read_json(path, {}) or {}).get(key)
            return written, incomplete, after, held_sha

        for door in ("held", "tip", "merge"):
            for state, expect_incomplete in (
                    (landreq.ANCESTOR, False),
                    (landreq.NOT_ANCESTOR, False),
                    (landreq.UNDETERMINED, True)):
                with self.subTest(door=door, ancestry=state):
                    stored, examined, incomplete = run(door, state)
                    self.assertIs(
                        bool(incomplete), expect_incomplete,
                        "the %s door under ancestry %r reported "
                        "incomplete=%r (stored=%r examined=%r); a MEASURED "
                        "answer decides and an UNANSWERABLE one does not"
                        % (door, state, incomplete, stored, examined))

        for state, expect_incomplete in (
                (landreq.ANCESTOR, False),
                (landreq.NOT_ANCESTOR, False),
                (landreq.UNDETERMINED, True)):
            with self.subTest(door="final-held", ancestry=state):
                written, incomplete, after, held_sha = run_final_held(state)
                self.assertIs(
                    bool(incomplete), expect_incomplete,
                    "the final-held re-validation under ancestry %r reported "
                    "incomplete=%r (written=%r); this is the door the other "
                    "nine cells structurally cannot reach"
                    % (state, incomplete, written))
                # AND THE EVICTION RULE IS ASSERTED IN THE SAME CELLS, so a
                # change that fixes the status by loosening what may be
                # overwritten reddens here instead of passing. Only a
                # MEASURED no evicts: NOT_ANCESTOR replaces the held entry,
                # ANCESTOR and UNDETERMINED both leave it exactly as it was.
                if state == landreq.NOT_ANCESTOR:
                    self.assertEqual(written, 1)
                    self.assertNotEqual(after, held_sha)
                else:
                    self.assertEqual(written, 0)
                    self.assertEqual(
                        after, held_sha,
                        "a held entry was overwritten on ancestry %r; only a "
                        "MEASURED no may evict" % (state,))

    def test_a_fully_readable_run_reports_complete(self):
        """THE CONTROL WITHOUT WHICH THE THREE ABOVE PROVE NOTHING.

        Two shapes, because the failure mode they guard against is a flag
        pinned True and the store cannot see it. FIRST, a receipt that is
        stored: everything readable, the tip carries the id, one write, and
        the run is finished. SECOND, and this is the one that matters, a run
        that legitimately writes NOTHING -- a readable tip that measurably
        does not carry the id and an index that has no other carrier. No
        write, nothing undecided, and it must report COMPLETE. That is the
        pair the whole family turns on: absence of a write is not doubt.
        """
        pid, tip = "b" * 40, "c" * 40
        with _tmp_rescue_store() as path, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda *_a, **_k: pid), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            stored, examined, incomplete = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            wrote = (pk.read_json(path, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertEqual((stored, examined, wrote), (1, 1, tip))
        self.assertFalse(
            incomplete,
            "a run in which every question was answered reported work left, "
            "so the three UNKNOWN arms above pass against a flag that is "
            "always True")

        with _tmp_rescue_store() as path2, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda *_a, **_k: "q" * 40), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            stored2, examined2, incomplete2 = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": pid, "reviewed_tip": tip}])
            wrote2 = (pk.read_json(path2, {}) or {}).get(
                "%s\t%s" % ("/nonexistent/.git", pid))
        self.assertEqual((stored2, examined2, wrote2), (0, 1, None))
        self.assertFalse(
            incomplete2,
            "a MEASURED miss with nothing to store reported work left; a "
            "run that wrote nothing because there was nothing to write is "
            "finished, and conflating that with doubt makes the caller "
            "re-walk history forever")

    def test_a_failed_rev_list_is_not_an_empty_index(self):
        """A WALK THAT COULD NOT RUN IS NOT A WALK THAT FOUND NOTHING.

        `_stored_patch_index` answers (None, False) when rev-list itself fails
        -- an unreadable or absent trunk, a broken object store -- because
        there is no partial index to describe. Coercing that to `{}` gives
        every lookup a clean MISS: nothing is stored, and the run reports
        incomplete=False, which tells the caller the history is FINISHED after
        proving nothing at all.

        ANCESTRY MUST FAIL FIRST OR THIS ARM CANNOT REACH THE INDEX, so the
        fixture makes it NOT_ANCESTOR -- the one state that falls through to
        the index while still being a real answer.
        """
        def failing_git(_gitdir, *args, **kw):
            if git_verb(args) == "rev-parse":
                return _Ok("d" * 40)           # the trunk pin resolves
            if git_verb(args) == "rev-list":
                return _Fail()                 # ... and the walk does not
            return _Ok("")

        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.NOT_ANCESTOR), \
                mock.patch.object(landreq, "_git", failing_git):
            result = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": "b" * 40, "reviewed_tip": "c" * 40}])
        self.assertIsNotNone(result, "the ledger was readable")
        stored, examined, incomplete = result
        self.assertTrue(
            incomplete,
            "a failed rev-list was reported as a FINISHED history: the walk "
            "never ran, so nothing about this receipt was decided (%r)"
            % (result,))
        self.assertEqual(stored, 0, "nothing was provable, so nothing is "
                                    "stored (%r)" % (result,))
        self.assertEqual(examined, 1,
                         "MUST-HIT: the receipt was never examined, so the "
                         "index path this arm is about never ran (%r)"
                         % (result,))

    def test_a_trunk_pin_that_could_not_be_taken_is_not_finished(self):
        """EVERY RECEIPT IS JUDGED AGAINST THE PIN, SO WITHOUT IT NOTHING WAS.

        `incomplete=False` is the sentence "there is no work left", which a
        caller acts on by never asking again. A run that could not resolve
        trunk decided nothing about any receipt and must say so.
        """
        def no_pin(_gitdir, *args, **kw):
            if git_verb(args) == "rev-parse":
                return _Fail()
            return _Ok("")

        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_git", no_pin):
            result = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": "b" * 40, "reviewed_tip": "c" * 40}])
        self.assertIsNotNone(result, "the ledger was readable")
        stored, _examined, incomplete = result
        self.assertTrue(
            incomplete,
            "a trunk pin that could not be taken was reported as a finished "
            "history (%r)" % (result,))
        self.assertEqual(stored, 0)
        # MUST-HIT: with a pin that DOES resolve, the same receipt reaches a
        # decision, so the exhaustion above is the missing pin's doing.
        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.NOT_ANCESTOR), \
                mock.patch.object(landreq, "_stored_patch_index",
                                  return_value=({}, False)), \
                mock.patch.object(landreq, "_git", return_value=_Ok("d" * 40)):
            ok = landreq.backfill_landing_store(
                "/nonexistent/.git", "origin/main",
                receipts=[{"patch_id": "b" * 40, "reviewed_tip": "c" * 40}])
        self.assertFalse(
            ok[2],
            "MUST-HIT: a run with a resolvable pin and a complete index still "
            "reported itself unbounded, so this arm cannot tell a missing pin "
            "from anything else (%r)" % (ok,))

    def test_an_unreadable_ledger_is_not_an_empty_one(self):
        """A MISSING SOURCE AND NO WORK MUST NOT SHARE A RETURN VALUE.

        READ THROUGH REAL FILES, not a mocked `_landing_receipts`. A review
        qualified the first version of this arm exactly there: stubbing the
        reader asserts the CALLER's handling and says nothing about the
        reader, and the reader is where the collapse lived — a wholly
        malformed ledger skipped every line and returned [], which the
        missing-file fix never reached.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "land-receipts.jsonl")
        with mock.patch.object(landreq, "receipts_path", lambda: path):
            self.assertIsNone(landreq._landing_receipts(),
                              "an ABSENT ledger read as a readable one")

            open(path, "w").close()
            self.assertEqual(landreq._landing_receipts(), [],
                             "an EMPTY ledger is genuinely no work")

            with open(path, "w") as fh:
                fh.write("{not json\n[also not\n")
            self.assertIsNone(landreq._landing_receipts(),
                              "a ledger with content and ZERO usable rows "
                              "returned the same value an empty one gives")

            # A TORN TAIL HAS NO TERMINATOR, and that is the whole fixture.
            # `{"patch_id": "aa"}\n{torn\n` looks like this case and is not
            # it: those are TWO COMPLETE LINES, the second malformed, so any
            # reader that skips a complete corrupt row passes against them
            # while failing the property this arm is named for.
            with open(path, "w") as fh:
                fh.write('{"patch_id": "aa"}\n{torn')
            self.assertEqual(landreq._landing_receipts(),
                             [{"patch_id": "aa"}],
                             "an UNTERMINATED tail is ordinary -- the writer "
                             "was interrupted and those bytes are not a row "
                             "yet -- and its rows must survive")

            # AND THE CASE A TERMINATED FIXTURE ACTUALLY BUILDS: a COMPLETE
            # malformed line between healthy ones. It was fully written, so
            # its malformation is CORRUPTION rather than an unfinished
            # append, and skipping it hands the caller a list that silently
            # omits a receipt while looking whole -- which a backfill turns
            # into "0 stored, nothing left to do" for a receipt that will now
            # never be proved.
            with open(path, "w") as fh:
                fh.write('{"patch_id": "aa"}\n{torn\n{"patch_id": "bb"}\n')
            self.assertIsNone(
                landreq._landing_receipts(),
                "a COMPLETE malformed row between healthy ones was skipped, "
                "so the caller got a shorter list and no way to know")

            # A COMPLETE LINE THAT PARSES AND IS NOT A ROW. `[1, 2]` is
            # valid JSON, so it passes the decode and the parse and fails
            # only the shape check -- a different branch from the malformed
            # line above, and one a `continue` there would swallow just as
            # silently.
            with open(path, "w") as fh:
                fh.write('{"patch_id": "aa"}\n[1, 2]\n{"patch_id": "bb"}\n')
            self.assertIsNone(
                landreq._landing_receipts(),
                "a complete line that parsed to a NON-ROW was skipped, so "
                "the caller got a shorter list and no way to know")

            # NOT VALID UTF-8, which reaches this reader as a decode failure
            # rather than a JSON one. Read as text it raises where only OSError
            # is caught and escapes the function entirely.
            with open(path, "wb") as fh:
                fh.write(b'{"patch_id": "aa"}\n\xff\xfe not utf-8\n')
            self.assertIsNone(
                landreq._landing_receipts(),
                "an undecodable complete row did not report the ledger "
                "unreadable")

            # MUST-HIT IN THE OTHER DIRECTION: a wholly healthy ledger still
            # returns its rows, or every assertion above is satisfied by a
            # reader that answers None to everything.
            with open(path, "w") as fh:
                fh.write('{"patch_id": "aa"}\n{"patch_id": "bb"}\n')
            self.assertEqual(
                landreq._landing_receipts(),
                [{"patch_id": "aa"}, {"patch_id": "bb"}],
                "MUST-HIT: a clean ledger did not read, so the Nones above "
                "say nothing about corruption")

    def test_a_same_pid_key_that_left_trunk_is_repaired(self):
        """THE SECOND LEG. An entry can carry the right patch-id and still be
        off trunk — a reset or a force-move does exactly that — and the reader
        refuses it on ancestry. A never-demote test that asks only the content
        leg calls that entry healthy and protects it forever.

        ANCESTRY IS NOT MOCKED TRUE HERE. A review qualified the earlier
        controls on precisely that: they stubbed ancestry to ANCESTOR, so the
        leg the defect lives in was never exercised.
        """
        gitdir, trunk = "/nonexistent/.git", "origin/main"
        pid, gone, live = "e" * 40, "f" * 40, "a" * 40
        key = "%s\t%s" % (gitdir, pid)

        def on_trunk(_g, sha, _ref):
            # BOTH shas carry the pid; only one is still reachable.
            return landreq.ANCESTOR if sha == live else landreq.NOT_ANCESTOR

        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry", on_trunk), \
                _hash_faces(lambda *_a, **_k: pid):
            pk.write_json(store, {key: gone})
            written, _ = landreq._merge_rescue_entries(
                gitdir, {pid: live}, trunk)
            self.assertEqual(written, 1)
            self.assertEqual(pk.read_json(store, {}), {key: live},
                             "a key holding a sha that carries the right id "
                             "but LEFT TRUNK was preserved as healthy")

    def test_the_merge_spends_no_git_after_the_deadline(self):
        """THE BUDGET COVERS ADMISSION. Measured on the first cut:
        three entries at ten seconds of admission each returned (3, 3, False)
        against a one-second budget, because the deadline stopped the caller's
        loop and then the merge ran unbounded."""
        gitdir, trunk = "/nonexistent/.git", "origin/main"
        clock, checked = [0.0], []

        def creeping(_g, sha, _ref):
            checked.append(sha)
            clock[0] += 10.0
            return landreq.ANCESTOR

        with _tmp_rescue_store(), \
                mock.patch.object(landreq, "_monotonic", lambda: clock[0]), \
                mock.patch.object(landreq, "_ancestry", creeping), \
                _hash_faces(lambda _g, sha: "p" + sha[1:]):
            written, incomplete = landreq._merge_rescue_entries(
                gitdir,
                {"p" + c * 39: c * 40 for c in "abc"}, trunk, deadline=1.0)
        self.assertTrue(incomplete,
                        "the merge overran its budget and reported it had "
                        "not — the caller then reads a finished history")
        self.assertEqual(len(checked), 1,
                         "admission kept spending git past the deadline")
        # The one entry admitted BEFORE the budget expired is correctly
        # written; the property under test is that the other two never cost
        # a git call, not that nothing lands.
        self.assertEqual(written, 1)

    def test_the_hot_cache_fill_never_waits_on_another_writer(self):
        """THE READ PATH MUST NOT QUEUE BEHIND A WRITER. One of the three
        writers is `_stored_patch_on_trunk`'s cache-fill, which runs on every
        render; a blocking lock there puts a reader behind someone else's git.
        A cold cache is slow, never wrong — so losing the race is fine.

        ONE FIXTURE, ONE STORE, ONE ASSERTION. The contended call and its
        positive control read the SAME observable, because "it wrote nothing"
        is satisfied by a merge that never writes at all.
        """
        import fcntl
        gitdir, trunk = "/nonexistent/.git", "origin/main"
        pid, sha = "e" * 40, "f" * 40
        with _tmp_rescue_store() as store, \
                mock.patch.object(landreq, "_ancestry",
                                  return_value=landreq.ANCESTOR), \
                _hash_faces(lambda *_a, **_k: pid):
            held = os.open(landreq._rescue_lock_path(),
                           os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(held, fcntl.LOCK_EX)   # another writer owns it
            try:
                blocked, _ = landreq._merge_rescue_entries(
                    gitdir, {pid: sha}, trunk)
                during = pk.read_json(store, {})
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)
            free, _ = landreq._merge_rescue_entries(gitdir, {pid: sha}, trunk)
            self.assertEqual(
                (blocked, during, free, pk.read_json(store, {})),
                (0, {}, 1, {"%s\t%s" % (gitdir, pid): sha}),
                "contended: it must write nothing and NOT wait; uncontended: "
                "the same call must write, or the first half proves nothing")
