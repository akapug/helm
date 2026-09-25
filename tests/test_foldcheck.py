"""The fold rung must BITE, and the witnesses prove it.

A guard nobody can construct a failing input for is decoration. Every rung
below carries BOTH witnesses: one state it MUST refuse and one it MUST pass,
built in a real temp git repo with a real local bare origin rather than
asserted over fixtures — the rung's whole job is reading git and a receipt
store, so a mocked git would test the mock.

THE ARM THIS FILE WAS MISSING, and the reason the lane came back FIX. The
first `tree-vs-gate` took the gate tree as CALLER TEXT and compared min-length
prefixes; two reviewers independently proved within an hour that `--gate-tree
0` returned PASS against a real tree, and so did the real tree with `deadbeef`
appended — a 48-character string naming no object at all. Every witness in the
old file passed anyway, because not one of them ever handed the rung its own
answer and asked whether it took it. `SuppliedAnswerTest` is that missing
question. Its positive control is what stops it from being the same mistake
pointing the other way: a rung that answers UNKNOWN to everything is exactly
as useless as one that answered PASS to everything, and only the control tells
those two apart.

RECEIPTS ARE MINTED, NEVER FAKED. `_mint` writes a row through the real
grammar (`gate._receipt_id`) into an ISOLATED `HELM_HOME`, then reads it back
through `gate.receipts()` before any assertion runs. That filter SILENTLY
SKIPS a row whose id disagrees with its content, so a botched fixture would
surface as the very UNKNOWN these arms exist to detect and every gate witness
would go green for the wrong reason.

TESTS SPAWN GIT DIRECTLY AND `helm/` MUST NOT. The direct-spawn audit in
tests/test_vcs.py sweeps the package only; the first foldcheck.py hand-rolled
its own `subprocess.run(("git", ...))` boundary and that audit refused the
tree. Everything under helm/ goes through helm/vcs.py; the helpers here are
fixture construction, which is a different job.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import foldcheck, foldcompose, gate, gateimport, landreq, pk, vcs

# The env this suite must not read THROUGH — an unisolated HELM_HOME points
# `gate.receipts()` at the live fleet ledger, where the answer to "is there a
# receipt for this tree" depends on what the fleet did today.
ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def _run(repo, *args):
    subprocess.run(("git", "-C", repo) + args, capture_output=True, text=True,
                   check=True)


def _repo_with_origin():
    """A worktree whose 'origin' is a real local bare repo, so origin-has-it
    is answering the same question it answers in production."""
    root = os.path.realpath(tempfile.mkdtemp(prefix="foldcheck-"))
    bare, work = os.path.join(root, "origin.git"), os.path.join(root, "work")
    subprocess.run(("git", "init", "--bare", "-b", "main", bare),
                   capture_output=True, check=True)
    subprocess.run(("git", "clone", bare, work), capture_output=True, check=True)
    _run(work, "config", "user.email", "t@example.invalid")
    _run(work, "config", "user.name", "t")
    with open(os.path.join(work, "a.txt"), "w") as fh:
        fh.write("one\n")
    _run(work, "add", "a.txt")
    _run(work, "commit", "-m", "one")
    _run(work, "push", "-q", "origin", "main")
    _run(work, "fetch", "-q", "origin")
    return work


def _head(repo, rev="HEAD"):
    p = subprocess.run(("git", "-C", repo, "rev-parse", rev),
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


def _tree(repo, rev="HEAD"):
    p = subprocess.run(("git", "-C", repo, "rev-parse", "%s^{tree}" % rev),
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


def _named(rungs, name):
    return [r for r in rungs if r.name == name][0]


class FoldCheckBase(unittest.TestCase):
    """A private git world and a private receipt store, per test.

    Both halves are load-bearing. The repo has to be real because every rung
    is a git question. The STORE has to be real AND isolated because rung 2
    now derives its answer from it: reading the live ledger would make these
    arms depend on whatever the fleet minted this hour, and writing to it
    would put fixture receipts into the fleet's own evidence.
    """

    def setUp(self):
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="helm-test-foldcheck-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "foldcheck-fixture-seat"

    def _restore_env(self):
        for key, prior in self.prior.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

    def _repo(self):
        """`_repo_with_origin` plus the cleanup it never had — the bare origin
        and the worktree share one root, so one rmtree takes both."""
        work = _repo_with_origin()
        self.addCleanup(shutil.rmtree, os.path.dirname(work),
                        ignore_errors=True)
        return work

    def _clone(self, work, name="other"):
        """A SECOND checkout of the same bare origin — the only honest way to
        move origin out from under a worktree, which is what rungs 3 and 5
        exist to notice."""
        path = os.path.join(os.path.dirname(work), name)
        subprocess.run(("git", "clone", os.path.join(os.path.dirname(work),
                                                     "origin.git"), path),
                       capture_output=True, check=True)
        _run(path, "config", "user.email", "t2@example.invalid")
        _run(path, "config", "user.name", "t2")
        return path

    def _commit(self, repo, name, body):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(body)
        _run(repo, "add", name)
        _run(repo, "commit", "-m", body.strip() or name)
        return _head(repo)

    def _receipt_row(self, head, tree, **over):
        """A receipt the REAL minting grammar would produce: v4, host block,
        id computed by the same function `gate.receipts()` recomputes."""
        row = {"v": 4, "event": "gate", "ts": pk.now_ts(),
               "repo_id": "/fixture/wt/some-lane-abc12345",
               "head": head, "tree": tree, "dirty": False,
               "head_after": head, "tree_after": tree, "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.12.3",
                               "language": "3.12.3",
                               "executable": "/usr/bin/python3"},
               "host": {"node": "fixture-node", "system": "Linux",
                        "release": "6.8.0", "id": "ab" * 8},
               "argv": ["/usr/bin/python3", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": None, "rc": 0, "wall": 12.5,
               "status": "OK", "ran": 100, "skipped": 1, "detail": "",
               "elapsed": 12.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        return row

    def _mint(self, repo, rev="HEAD", **over):
        """Append a real receipt for `rev` to THIS test's store -> gate:<id>.

        The read-back is not ceremony. `gate.receipts()` recomputes every id
        and DROPS the rows that disagree, silently — so a fixture with one
        field out of grammar would arrive at the rung as "no receipt in this
        store", which is the exact UNKNOWN several arms below are trying to
        distinguish from a rung that refuses to look. Prove the fixture is
        readable here, and every UNKNOWN downstream is about the rung.
        """
        row = self._receipt_row(_head(repo, rev), _tree(repo, rev), **over)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        # AND THE ROW HELM'S OWN RUNNER WRITES BESIDE IT (task/3066): the fold
        # is a land, and a land takes only a receipt an authenticated door
        # placed in this repository. This fixture stands in for a local mint,
        # so it records one; the arms about provenance itself live in
        # tests/test_land_provenance.py.
        self.assertIsNone(gateimport.record_mint(row, repo))
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable, "fixture store unreadable")
        self.assertEqual(skipped, 0, "the fixture receipt failed its own "
                                     "integrity recompute — fix the fixture, "
                                     "not the assertion")
        self.assertIn(row["id"], [str(r.get("id")) for r in rows],
                      "the minted receipt is not in the store it was minted "
                      "into")
        return "gate:" + row["id"]


class FoldCheckTest(FoldCheckBase):

    def test_a_clean_pushed_fold_PASSES_all_five(self):
        """MUST-PASS witness. Without this the refusals below prove nothing:
        a rung that refuses everything is as useless as one that passes
        everything, and only this arm tells them apart."""
        repo = self._repo()
        rungs = foldcheck.check(repo, _head(repo), gate_ref=self._mint(repo))
        self.assertTrue(foldcheck.ok(rungs), foldcheck.report(rungs))
        self.assertEqual([r.verdict for r in rungs], [foldcheck.PASS] * 5)

    def test_the_FIFTH_rung_catches_an_unpushed_land(self):  # noqa: VACUOUS_ASSERTION — the four-PASS assertion below is an unconditional positive control on the same rung list
        """MUST-CATCH, and this is the whole reason the module exists: a
        commit that is local-only passes the first four and is invisible to
        everyone else. Two lands were announced in this state."""
        repo = self._repo()
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("two\n")
        _run(repo, "commit", "-am", "unpushed")
        rungs = foldcheck.check(repo, _head(repo), gate_ref=self._mint(repo))
        self.assertFalse(foldcheck.ok(rungs))
        origin = _named(rungs, "origin-has-it")
        self.assertEqual(origin.verdict, foldcheck.REFUSE)
        self.assertIn("NOT on origin/main", origin.discriminator)
        # the discriminating detail: the OTHER four are green, which is
        # exactly why a four-check protocol shipped this bug.
        others = [r for r in rungs if r.name != "origin-has-it"]
        self.assertEqual([r.verdict for r in others], [foldcheck.PASS] * 4)

    def test_a_dirty_worktree_REFUSES_and_counts_the_paths(self):
        """Rung 4's first half. HEAD is the tip and the tree is still not the
        tree that was gated, because something uncommitted is sitting on top
        of it — so the count and the first path go in the verdict, not the
        word 'dirty'."""
        repo = self._repo()
        handle = self._mint(repo)
        # the SAME repo, the SAME handle, one file apart: the control runs
        # first so the REFUSE below cannot be a rung that never says yes.
        clean = _named(foldcheck.check(repo, _head(repo), gate_ref=handle),
                       "head-clean")
        self.assertEqual(clean.verdict, foldcheck.PASS)
        self.assertIn("nothing is dirty", clean.discriminator)
        with open(os.path.join(repo, "b.txt"), "w") as fh:
            fh.write("untracked\n")
        r = _named(foldcheck.check(repo, _head(repo), gate_ref=handle),
                   "head-clean")
        self.assertEqual(r.verdict, foldcheck.REFUSE)
        self.assertIn("b.txt", r.discriminator)

    def test_head_clean_REFUSES_when_HEAD_is_not_the_tip_being_folded(self):
        """The other half of rung 4, and the one a dirty-file test misses: a
        spotless worktree sitting on a DIFFERENT commit than the one being
        announced. Nothing is uncommitted, and the fold is still about a tree
        that is not checked out here."""
        repo = self._repo()
        first = _head(repo)
        self._commit(repo, "c.txt", "second\n")
        stale = _named(foldcheck.check(repo, first, gate_ref=self._mint(repo)),
                       "head-clean")
        self.assertEqual(stale.verdict, foldcheck.REFUSE)
        self.assertIn(_head(repo)[:12], stale.discriminator)   # where we ARE
        self.assertIn(first[:12], stale.discriminator)         # what was asked
        # positive control on the same observable: naming the tip we are
        # actually on PASSES in the same clean repo.
        live = _named(foldcheck.check(repo, _head(repo),
                                      gate_ref=self._mint(repo)), "head-clean")
        self.assertEqual(live.verdict, foldcheck.PASS)
        self.assertIn("HEAD is the tip", live.discriminator)

    def test_ff_able_REFUSES_when_trunk_moved_and_PASSES_after_the_rebase(self):
        """Rung 3's missing MUST-CATCH. A second checkout advances origin/main
        while this lane commits on the old base, so folding would carry in a
        merge nobody reviewed. The discriminator's own advice — rebase and
        re-gate — is then the positive control: same repo, same rung, PASS.

        BOTH ARMS FETCH, and that is not incidental. Rung 3 compares against a
        remote-tracking ref, so under `fetch=False` it can no longer return a
        verdict at all (see `OneFetchedMomentTest`) — this arm asks it to
        REFUSE and to PASS, which only a refreshed snapshot entitles it to do.
        """
        repo = self._repo()
        other = self._clone(repo)
        self._commit(other, "trunk.txt", "trunk moved\n")
        _run(other, "push", "-q", "origin", "main")
        self._commit(repo, "mine.txt", "my lane\n")
        r = _named(foldcheck.check(repo, _head(repo), gate_ref=self._mint(repo)),
                   "ff-able")
        self.assertEqual(r.verdict, foldcheck.REFUSE)
        self.assertIn("NOT an ancestor", r.discriminator)
        self.assertIn("origin/main", r.discriminator)
        _run(repo, "fetch", "-q", "origin")
        _run(repo, "rebase", "origin/main")
        after = _named(foldcheck.check(repo, _head(repo),
                                       gate_ref=self._mint(repo)), "ff-able")
        self.assertEqual(after.verdict, foldcheck.PASS)
        self.assertIn("is an ancestor of the tip", after.discriminator)

    def test_a_lane_that_LANDED_BY_REBASE_is_not_told_to_rebase_and_re_gate(self):
        """The fold REBASES, so a landed lane fails ancestry while every line
        of it is on trunk. Both sha rungs then refused with sentences that were
        FALSE about that lane — "the push has not been accepted" and "trunk
        moved; rebase and re-gate" — and the author, holding an approve, is
        told to redo work that is already in.

        THE REFUSAL ITSELF IS CORRECT AND THIS ARM KEEPS IT. Landed work must
        not be folded a second time, so the verdict stays REFUSE; what must
        change is the reason. Asserting the verdict alone could never catch
        this defect, because the verdict was right the whole time.

        THE SECOND HALF IS THE MUST-CATCH: an ordinary unlanded lane in the
        same repo must still receive the ORIGINAL advice, verbatim. That arm is
        what makes this a fix rather than an inversion — a content predicate
        chosen carelessly answers False for a commit that is literally an
        ancestor of trunk, which would leave every plain lane refusing.
        """
        repo = self._repo()
        other = self._clone(repo)
        # A REBASING FOLD, simulated the way the integrator performs one: the
        # same CHANGE reaches origin under a DIFFERENT sha. Identical content
        # on the identical base is what patch identity is built to recognise.
        self._commit(other, "cure.txt", "the cure\n")
        _run(other, "push", "-q", "origin", "main")
        self._commit(repo, "cure.txt", "the cure\n")
        landed_tip = _head(repo)

        rungs = foldcheck.check(repo, landed_tip, gate_ref=self._mint(repo))
        ff = _named(rungs, "ff-able")
        origin = _named(rungs, "origin-has-it")
        self.assertEqual(ff.verdict, foldcheck.REFUSE)
        self.assertEqual(origin.verdict, foldcheck.REFUSE)
        self.assertIn("ALREADY LANDED", ff.discriminator)
        self.assertIn("HAS LANDED", origin.discriminator)
        # the two FALSE sentences must be gone from this lane's report
        self.assertNotIn("trunk moved; rebase and re-gate", ff.discriminator)
        self.assertNotIn("the push has not been accepted", origin.discriminator)

        # MUST-CATCH: a genuinely unlanded lane keeps the original advice.
        plain = self._repo()
        other2 = self._clone(plain)
        self._commit(other2, "trunk.txt", "trunk moved\n")
        _run(other2, "push", "-q", "origin", "main")
        self._commit(plain, "unrelated.txt", "never pushed\n")
        prungs = foldcheck.check(plain, _head(plain), gate_ref=self._mint(plain))
        pff = _named(prungs, "ff-able")
        porigin = _named(prungs, "origin-has-it")
        self.assertEqual(pff.verdict, foldcheck.REFUSE)
        self.assertEqual(porigin.verdict, foldcheck.REFUSE)
        self.assertIn("trunk moved; rebase and re-gate", pff.discriminator)
        self.assertIn("the push has not been accepted", porigin.discriminator)
        self.assertNotIn("ALREADY LANDED", pff.discriminator)
        self.assertNotIn("HAS LANDED", porigin.discriminator)

    def test_an_unmeasurable_content_answer_never_reads_as_did_not_land(self):  # noqa: VACUOUS_ASSERTION — every absence assertion is paired with an unconditional assertIn on the identical observable one line above it; see the note in the body
        """`landed_ever` is a TRI-STATE and None is not False. A caller that
        collapses them turns a cannot-look into a did-not-land, which is the
        fail-open shape this codebase keeps meeting. Measured live the day this
        landed: two different unlanded lane tips returned False and None
        respectively, so the distinction is load-bearing, not theoretical.

        Here the predicate is forced to return None, and the rung must still
        REFUSE while SAYING it could not measure — never asserting the push was
        rejected, which is a claim it has not earned.
        """
        repo = self._repo()
        other = self._clone(repo)
        self._commit(other, "cure.txt", "the cure\n")
        _run(other, "push", "-q", "origin", "main")
        self._commit(repo, "cure.txt", "the cure\n")
        real = foldcheck._landed_by_content
        foldcheck._landed_by_content = lambda *a, **k: None
        try:
            rungs = foldcheck.check(repo, _head(repo), gate_ref=self._mint(repo))
        finally:
            foldcheck._landed_by_content = real
        # BOTH RUNGS, and the first one is why this arm was rewritten. It
        # originally checked origin-has-it alone, so when ff-able collapsed
        # None into "trunk moved; rebase and re-gate" — asserting a lane never
        # landed on the strength of a measurement that did not happen — no arm
        # in this file could have reddened. Found in review, not
        # here. A tri-state has to be pinned at EVERY consumer; pinning it at
        # one and trusting the shared helper is how the second consumer drifts.
        #
        # WRITTEN OUT RATHER THAN LOOPED, deliberately. The loop this replaced
        # walked a literal 2-tuple and so could not actually skip, but it put
        # every positive control one refactor away from running zero times, and
        # the pre-commit vacuous-assertion rung said so the moment the arm grew
        # it. Unrolling is cheaper than arguing with a guard that is right.
        # WHY THIS ARM CARRIES A VACUOUS_ASSERTION ESCAPE on its def line: every
        # absence assertion below is paired with an UNCONDITIONAL positive
        # control on the identical observable, one line above it
        # (assertIn("could NOT be measured", ff.discriminator) guards the
        # ff.discriminator absences; the same holds for r.discriminator), and
        # the arm additionally proves its own double was in effect by calling
        # the real helper on the same repo. The rung still flags it because its
        # uncovered test is `not roots or roots.isdisjoint(positive)` and these
        # observables are ATTRIBUTE expressions off a name bound from a value
        # produced inside a try block, so they record no root to match against.
        # MEASURED, and stated no wider than the measurement: against the real
        # unmeasurable lane tip, the PRE-FIX ff-able printed "trunk moved;
        # rebase and re-gate" while origin-has-it already said "could NOT be
        # measured" — so the ff-able assertIn below is the clause that would
        # have reddened, and it is the only one this fix newly earns.
        ff = _named(rungs, "ff-able")
        self.assertEqual(ff.verdict, foldcheck.REFUSE)
        self.assertIn("could NOT be measured", ff.discriminator)
        self.assertNotIn("trunk moved; rebase and re-gate", ff.discriminator)
        self.assertNotIn("ALREADY LANDED", ff.discriminator)
        r = _named(rungs, "origin-has-it")
        self.assertEqual(r.verdict, foldcheck.REFUSE)
        self.assertIn("could NOT be measured", r.discriminator)
        self.assertNotIn("the push has not been accepted", r.discriminator)
        self.assertNotIn("HAS LANDED", r.discriminator)
        # and the double really was in effect — the unpatched call on the SAME
        # repo says LANDED, so the None above is the stub's doing, not the
        # repo's state.
        self.assertIs(True, real(foldcheck.vcs.backend(repo), repo,
                                 _head(repo), "origin/main"))

    def test_the_content_question_is_asked_ONCE_for_the_whole_report(self):
        """Rungs 3 and 5 both need to know whether this change already reached
        trunk, and each of them asked INDEPENDENTLY — two runs of a mutable
        query at two different moments. A fold landing between the two calls
        makes one rung say ALREADY LANDED while the other says trunk moved,
        inside a single report that presents itself as one measurement.

        THIS FILE ALREADY LEARNED THIS ONCE, for the fetch: `check`'s docstring
        records that rungs 3 and 5 answered from two different moments while
        the fetch sat inside rung 5, and that binding both to one snapshot is
        what makes the report a single measurement. I reintroduced the same
        defect one query over, inside the function whose docstring describes
        it. Found in review.

        COUNTING IS THE ONLY INSTRUMENT THAT SEES IT. Both call sites returned
        correct answers; nothing about either rung's TEXT was wrong. The defect
        is the number of times the question was asked, so an arm that reads
        discriminators cannot reach it — which is why the previous two rounds
        of arms on this exact pair stayed green over it.

        THE SECOND HALF PINS THE OPPOSITE NUMBER, and it exists because the
        first cure for the race was EAGER: computed once in check() before any
        rung ran. That is one moment, correctly, and it also made --no-fetch
        reach the network — an unreadable tip sends landreq._landing_proof into
        _vanished_proof, which runs `git ls-remote origin`. So this arm asserts
        BOTH bounds: exactly one call when a rung needs the answer, and exactly
        ZERO when the report can be decided without it. A single-count arm
        would have passed the eager version unchanged.
        """
        calls = []
        real = foldcheck._landed_by_content

        def counting(*a, **k):
            calls.append(a)
            return real(*a, **k)

        # A LANE THAT REACHES THE NOT_ANCESTOR BRANCH, because that is the only
        # path that asks. My first version of this arm ran against a fresh repo
        # whose trunk WAS its head, so both ancestry rungs PASSED and the
        # supplier was never called — the arm would have asserted 1 against a
        # correct 0 the moment evaluation became lazy.
        repo = self._repo()
        other = self._clone(repo)
        self._commit(other, "cure.txt", "the cure\n")
        _run(other, "push", "-q", "origin", "main")
        self._commit(repo, "cure.txt", "the cure\n")

        foldcheck._landed_by_content = counting
        try:
            rungs = foldcheck.check(repo, _head(repo), gate_ref=self._mint(repo))
        finally:
            foldcheck._landed_by_content = real
        # ONE call for the whole report. A count of 0 would equally prove the
        # double never installed, so this assertion carries its own in-effect
        # proof: only a real interception can make it 1.
        self.assertEqual(len(calls), 1,
                         "the content question ran %d times for one report; "
                         "two runs of a mutable query are two moments, and the "
                         "rungs can disagree between them" % len(calls))
        # and the report is still whole — five rungs, not a truncated run that
        # happened to ask once because it stopped early.
        self.assertEqual(len(rungs), 5)

        # THE OTHER HALF, and it is about NETWORK rather than count. An eager
        # version of this cure called the supplier before any rung ran, and for
        # an UNREADABLE tip landreq._landing_proof falls through to
        # _vanished_proof, which runs `git ls-remote origin`. So `--no-fetch` —
        # the flag whose entire purpose is running without a network — would
        # contact the remote and block on it to compute an answer both rungs
        # then discard. A review found it; this arm is the one it asked for.
        calls.clear()
        foldcheck._landed_by_content = counting
        try:
            offline = foldcheck.check(repo, "0" * 40, gate_ref=None, fetch=False)
        finally:
            foldcheck._landed_by_content = real
        self.assertEqual(len(calls), 0,
                         "an unreadable tip under --no-fetch asked the content "
                         "question %d time(s); that path can reach ls-remote, "
                         "so it is a network call to compute an answer no rung "
                         "uses" % len(calls))
        self.assertEqual(len(offline), 5)

    def test_an_ABSENT_gate_is_UNKNOWN_and_UNKNOWN_is_not_consent(self):
        """The rung that would have let every one of tonight's errors through:
        a missing input must not silently read as satisfied."""
        repo = self._repo()
        rungs = foldcheck.check(repo, _head(repo), gate_ref=None)
        r = _named(rungs, "tree-vs-gate")
        self.assertEqual(r.verdict, foldcheck.UNKNOWN)
        self.assertFalse(foldcheck.ok(rungs))
        self.assertIn("NOT PROVEN", "\n".join(foldcheck.report(rungs)))
        # control on the same observable: citing a real receipt in the same
        # repo makes the same rung PASS and `ok()` True.
        cited = foldcheck.check(repo, _head(repo), gate_ref=self._mint(repo))
        self.assertEqual(_named(cited, "tree-vs-gate").verdict, foldcheck.PASS)
        self.assertTrue(foldcheck.ok(cited), foldcheck.report(cited))

    def test_an_unreadable_tip_is_REFUSE_but_an_unaskable_git_is_UNKNOWN(self):
        """The two failure modes a boolean collapses together. A sha that is
        genuinely absent is a NO; a repo we cannot interrogate is a shrug."""
        repo = self._repo()
        handle = self._mint(repo)
        absent = _named(foldcheck.check(repo, "0" * 40, gate_ref=handle,
                                        fetch=False), "tip-exists")
        self.assertEqual(absent.verdict, foldcheck.REFUSE)
        self.assertIn("not a readable commit here", absent.discriminator)
        unaskable = _named(foldcheck.check(os.path.join(self.tmp, "no-repo"),
                                           "0" * 40, gate_ref=handle,
                                           fetch=False), "tip-exists")
        self.assertEqual(unaskable.verdict, foldcheck.UNKNOWN)
        self.assertIn("a shrug, not a no", unaskable.discriminator)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the same
        # rung PASSES for a real commit in a real repo, so the two verdicts
        # above are the inputs and not a rung that never says yes.
        live = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                      fetch=False), "tip-exists")
        self.assertEqual(live.verdict, foldcheck.PASS)
        self.assertIn("is a commit", live.discriminator)

    def test_ok_is_false_when_anything_is_UNKNOWN(self):
        """The vacuity guard on ok() itself: UNKNOWN must never be consent."""
        rungs = [foldcheck.Rung("a", foldcheck.PASS, ""),
                 foldcheck.Rung("b", foldcheck.UNKNOWN, "")]
        self.assertFalse(foldcheck.ok(rungs))
        # control on the same observable: all-PASS DOES return True, so the
        # False above is the UNKNOWN and not a function that never says yes.
        self.assertTrue(foldcheck.ok([foldcheck.Rung("a", foldcheck.PASS, "")]))


class SuppliedAnswerTest(FoldCheckBase):
    """THE REPRO. Rung 2's failure mode was that it failed OPEN — feeding it a
    wrong value did not produce a confusing refusal, it produced AGREEMENT.
    Every arm here hands the rung an answer; none may be accepted."""

    def test_a_caller_supplied_tree_can_NEVER_pass_the_gate_rung(self):
        """The exact three inputs two reviewers used, plus the control.

        `0` is the minimal one: under min-length prefix comparison a one-byte
        string matched every tree that started with `0`. The real tree is the
        subtle one — it is the RIGHT answer, and accepting it still proves
        only that the caller can type, never that a suite ran. The real tree
        with `deadbeef` welded on is the one that shows the comparison was
        never about an object at all: 48 characters naming nothing, PASS.
        """
        repo = self._repo()
        real = _tree(repo)

        def gate_rung(supplied):
            return _named(foldcheck.check(repo, _head(repo), gate_ref=supplied,
                                          fetch=False), "tree-vs-gate")

        # named, not looped: each input is a DIFFERENT argument about why a
        # supplied answer is not evidence, and a loop variable would report
        # all three failures as one line.
        minimal = gate_rung("0")                  # matched every tree in "0…"
        exact = gate_rung(real)                   # the RIGHT answer, still not evidence
        padded = gate_rung(real + "deadbeef")     # 48 chars naming no object
        self.assertEqual(
            [minimal.verdict, exact.verdict, padded.verdict],
            [foldcheck.UNKNOWN] * 3,
            "a caller-supplied tree was accepted as evidence: %r"
            % [minimal.discriminator, exact.discriminator, padded.discriminator])
        # and each discriminator has to SAY why, or the next caller retries
        # with a longer string instead of citing a receipt.
        self.assertIn("gate:<16-hex>", minimal.discriminator)
        self.assertIn("gate:<16-hex>", exact.discriminator)
        self.assertIn("gate:<16-hex>", padded.discriminator)
        # UNCONDITIONAL POSITIVE CONTROL, and without it the three arms above
        # are satisfied by a rung that answers UNKNOWN to everything — which
        # is the same defect wearing the opposite sign. Same repo, same tree,
        # cited through a REAL receipt: PASS.
        cited = _named(foldcheck.check(repo, _head(repo),
                                       gate_ref=self._mint(repo), fetch=False),
                       "tree-vs-gate")
        self.assertEqual(cited.verdict, foldcheck.PASS)
        self.assertIn(real[:12], cited.discriminator)

    def test_a_real_receipt_whose_tree_is_the_tips_tree_PASSES(self):
        """The MUST-PASS half of rung 2, standing on its own so the refusals
        in this class are never the whole story. A receipt minted over this
        tip's tree is the only thing that makes this rung say yes."""
        repo = self._repo()
        handle = self._mint(repo)
        r = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                   fetch=False), "tree-vs-gate")
        self.assertEqual(r.verdict, foldcheck.PASS, r.discriminator)
        self.assertIn(_tree(repo)[:12], r.discriminator)
        self.assertIn(handle.split(":")[1], r.discriminator)
        # the bare 16-hex spelling is the same handle — accepted, same verdict.
        bare = _named(foldcheck.check(repo, _head(repo),
                                      gate_ref=handle.split(":")[1],
                                      fetch=False), "tree-vs-gate")
        self.assertEqual(bare.verdict, foldcheck.PASS, bare.discriminator)
        self.assertIn(_tree(repo)[:12], bare.discriminator)

    def test_a_receipt_for_a_DIFFERENT_tree_REFUSES_and_names_both(self):
        """A gate that ran, and ran on something else. This is the honest
        near-miss the rung exists for — a receipt from before the last commit
        — and the verdict has to name BOTH trees or the reader cannot tell a
        one-byte difference from a gate that was never run."""
        repo = self._repo()
        self._commit(repo, "c.txt", "second\n")
        stale = self._mint(repo, "HEAD~1")        # the PREVIOUS tree, really minted
        r = _named(foldcheck.check(repo, _head(repo), gate_ref=stale,
                                   fetch=False), "tree-vs-gate")
        self.assertEqual(r.verdict, foldcheck.REFUSE, r.discriminator)
        # BOTH trees, spelled out. These two also prove the fixture: the same
        # discriminator cannot name two different 12-hex prefixes unless the
        # commits really do carry different trees.
        self.assertIn(_tree(repo)[:12], r.discriminator)             # ours
        self.assertIn(_tree(repo, "HEAD~1")[:12], r.discriminator)   # theirs
        self.assertIn(stale.split(":")[1], r.discriminator)          # which one
        # positive control on the same observable: a receipt minted over THIS
        # tree, in the same store, PASSES — so the REFUSE is the tree and not
        # the rung disliking receipts.
        current = _named(foldcheck.check(repo, _head(repo),
                                         gate_ref=self._mint(repo),
                                         fetch=False), "tree-vs-gate")
        self.assertEqual(current.verdict, foldcheck.PASS)
        self.assertIn("passed on", current.discriminator)

    def test_a_receipt_that_did_not_PASS_cannot_authorize_a_fold(self):
        """Right tree, wrong verdict. The receipt is genuine and its id
        recomputes; it just records a suite that FAILED, and a failing gate
        authorizes nothing. This is REFUSE, not UNKNOWN — the store answered."""
        repo = self._repo()
        failed = self._mint(repo, status="FAILED", rc=1, ran=100, skipped=0)
        r = _named(foldcheck.check(repo, _head(repo), gate_ref=failed,
                                   fetch=False), "tree-vs-gate")
        self.assertEqual(r.verdict, foldcheck.REFUSE, r.discriminator)
        self.assertIn("FAILED", r.discriminator)
        self.assertIn(failed.split(":")[1], r.discriminator)
        # positive control: same repo, same tree, same store — an OK receipt
        # PASSES, so the REFUSE above is the STATUS field and nothing else.
        okr = _named(foldcheck.check(repo, _head(repo),
                                     gate_ref=self._mint(repo), fetch=False),
                     "tree-vs-gate")
        self.assertEqual(okr.verdict, foldcheck.PASS)
        self.assertIn("passed on", okr.discriminator)

    def test_a_gate_handle_with_no_receipt_here_is_UNKNOWN_not_REFUSE(self):
        """An unfindable receipt is not a wrong one.

        A well-formed handle whose receipt was minted on another box and never
        imported has told this rung NOTHING. Calling that REFUSE manufactures
        a failure against a lane that may be perfectly gated, and the operator
        learns the wrong lesson — the remedy is `helm gate import`, not a
        rebase. UNKNOWN still blocks the fold; it just says which door.
        """
        repo = self._repo()
        absent = "gate:" + "0" * 16
        r = _named(foldcheck.check(repo, _head(repo), gate_ref=absent,
                                   fetch=False), "tree-vs-gate")
        self.assertEqual(r.verdict, foldcheck.UNKNOWN, r.discriminator)
        self.assertIn("0" * 16, r.discriminator)
        self.assertIn("gate import", r.discriminator)
        # positive control on the same observable: a handle that IS in this
        # store resolves and PASSES, so the UNKNOWN is the missing row.
        present = _named(foldcheck.check(repo, _head(repo),
                                         gate_ref=self._mint(repo),
                                         fetch=False), "tree-vs-gate")
        self.assertEqual(present.verdict, foldcheck.PASS)
        self.assertIn("passed on", present.discriminator)


class FetchIsNotOptionalTest(FoldCheckBase):
    """Rung 5's flag, and what it costs. `--no-fetch` exists so the suite can
    run without a network; it must never be the cheaper way to get a PASS."""

    def test_no_fetch_makes_the_fifth_rung_UNKNOWN_never_a_cheaper_PASS(self):
        """Same repo, same tip, one flag apart — and the flag may only ever
        move the verdict DOWN. The positive control is the identical call with
        fetch=True: it PASSES, so the UNKNOWN is the flag and not the repo."""
        repo = self._repo()
        handle = self._mint(repo)
        skipped = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                         fetch=False), "origin-has-it")
        self.assertEqual(skipped.verdict, foldcheck.UNKNOWN)
        self.assertIn("--no-fetch", skipped.discriminator)
        self.assertIn("LOCAL ref", skipped.discriminator)
        fetched = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                         fetch=True), "origin-has-it")
        self.assertEqual(fetched.verdict, foldcheck.PASS, fetched.discriminator)
        self.assertIn("origin/main", fetched.discriminator)

    def test_a_REWOUND_origin_is_caught_by_the_fetch_and_missed_without_it(self):
        """The measured incident behind the flag's tri-state.

        A second checkout force-rewinds origin/main behind this lane. Nothing
        in this worktree changes: the stale remote-tracking ref still names
        the pushed tip, so a rung answering from local refs reports the land
        as delivered. With the fetch the ref catches up and the same call
        REFUSES. That is the same repository state producing two opposite
        answers, which is why `fetch=False` may not return PASS at all.
        """
        repo = self._repo()
        landed = self._commit(repo, "d.txt", "delivered\n")
        _run(repo, "push", "-q", "origin", "main")
        _run(repo, "fetch", "-q", "origin")
        handle = self._mint(repo)
        # PRE-CONTROL: right now origin really does have it, both ways of
        # asking agree, and the fetch arm PASSES.
        before = _named(foldcheck.check(repo, landed, gate_ref=handle),
                        "origin-has-it")
        self.assertEqual(before.verdict, foldcheck.PASS, before.discriminator)
        self.assertIn("is on origin/main", before.discriminator)
        other = self._clone(repo)
        _run(other, "reset", "--hard", "HEAD~1")
        _run(other, "push", "-q", "--force", "origin", "main")
        # the local remote-tracking ref is now a LIE, and unchanged. If this
        # stops holding the arm below measures nothing: a ref that moved on
        # its own is not the staleness this rung exists to refuse.
        self.assertIn(landed, _head(repo, "origin/main"),
                      "the local remote-tracking ref moved without a fetch")
        blind = _named(foldcheck.check(repo, landed, gate_ref=handle,
                                       fetch=False), "origin-has-it")
        self.assertNotEqual(blind.verdict, foldcheck.PASS,
                            "--no-fetch read a rewound origin as delivered")
        self.assertEqual(blind.verdict, foldcheck.UNKNOWN)
        self.assertIn("LOCAL ref", blind.discriminator)
        seeing = _named(foldcheck.check(repo, landed, gate_ref=handle),
                        "origin-has-it")
        self.assertEqual(seeing.verdict, foldcheck.REFUSE, seeing.discriminator)
        self.assertIn("do not announce this land", seeing.discriminator)


class OneFetchedMomentTest(FoldCheckBase):
    """ONE fetch, ONE moment, BOTH ancestry rungs.

    Rungs 3 and 5 ask opposite questions about the same `<remote>/<branch>`
    ref, and the fetch used to live INSIDE rung 5 — which runs AFTER rung 3.
    So the two rungs answered from two different moments, and a review
    built the state where that gap prints a fully green report. Every arm here
    exists because a rung that answers from evidence it does not have is the
    one failure this module was written to refuse.
    """

    def test_a_remote_advanced_to_a_DESCENDANT_is_not_all_five_PASS(self):
        """MUST-CATCH, and the failure it produced was a GREEN report — the
        worst shape a guard can fail in.

        A's tip is pushed and genuinely is on origin, so rung 5 passes
        honestly. B then pushes a DESCENDANT of that tip. Nothing in A changes:
        its remote-tracking ref still names A's own tip, where trunk is
        trivially an ancestor, so rung 3 reading that unrefreshed ref said
        PASS; rung 5 then fetched, saw the tip contained in the advanced
        origin, and said PASS too. All five, about a tip that current trunk
        cannot fast-forward to. The direct post-fetch merge-base at the bottom
        is what proves the scenario really was un-ff-able, so this arm is
        measuring the repository rather than a rung that dislikes descendants.
        """
        repo = self._repo()
        tip = self._commit(repo, "mine.txt", "my lane\n")
        _run(repo, "push", "-q", "origin", "main")
        _run(repo, "fetch", "-q", "origin")
        handle = self._mint(repo)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: origin has NOT
        # advanced, and this identical call still reports all five PASSED. The
        # cure must not have simply made rung 3 pessimistic.
        clean = foldcheck.check(repo, tip, gate_ref=handle)
        self.assertTrue(foldcheck.ok(clean), foldcheck.report(clean))
        self.assertIn("all five PASSED", "\n".join(foldcheck.report(clean)))
        other = self._clone(repo)
        self._commit(other, "theirs.txt", "a descendant of A's tip\n")
        _run(other, "push", "-q", "origin", "main")
        # A never refetches on its own. If this stops holding, the arm below
        # measures nothing — a ref that moved by itself is not the staleness
        # rung 3 was answering from. Asserted the way the rewound-origin arm
        # asserts it: the tip is still WHAT THE REF SAYS, a present value, not
        # an absence.
        self.assertIn(tip, _head(repo, "origin/main"),
                      "the local remote-tracking ref moved without a fetch")
        rungs = foldcheck.check(repo, tip, gate_ref=handle)
        self.assertFalse(
            foldcheck.ok(rungs),
            "all five PASSED on a tip that current trunk cannot fast-forward "
            "to — rung 3 answered from a moment before the fetch:\n%s"
            % "\n".join(foldcheck.report(rungs)))
        ff = _named(rungs, "ff-able")
        self.assertEqual(ff.verdict, foldcheck.REFUSE, ff.discriminator)
        self.assertIn("NOT an ancestor", ff.discriminator)
        self.assertIn("origin/main", ff.discriminator)
        # THE CONTROL THAT PROVES THE WITNESS MEASURES THE RIGHT THING: ask git
        # itself, after a fetch, in A. rc 1 is a GENUINE non-ancestor; 128
        # would be a question git could not answer, and then the REFUSE above
        # would only be agreement with a broken instrument.
        _run(repo, "fetch", "-q", "origin")
        direct = subprocess.run(
            ("git", "-C", repo, "merge-base", "--is-ancestor", "origin/main",
             tip), capture_output=True, text=True)
        self.assertEqual(direct.returncode, 1,
                         "current origin/main IS an ancestor of the tip — the "
                         "fixture never built the state under test (%s)"
                         % direct.stderr.strip())

    def test_no_fetch_makes_the_THIRD_rung_UNKNOWN_for_the_FIFTHS_reason(self):
        """The deliberate consequence, stated where a reader will look for it.

        Rung 3 compares against a remote-tracking ref, which is the same local
        snapshot rung 5 refuses to spend under `--no-fetch`, and its dangerous
        direction is the same: a snapshot from before trunk advanced makes an
        outdated trunk look like an ancestor. So the flag moves this verdict
        DOWN too. The cost is real — an offline run no longer prints "trunk
        moved; rebase and re-gate" — and is the right cost, because it never
        had the standing to print it. The positive control is the identical
        call with fetch=True: it PASSES, so the UNKNOWN is the flag."""
        repo = self._repo()
        handle = self._mint(repo)
        skipped = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                         fetch=False), "ff-able")
        self.assertEqual(skipped.verdict, foldcheck.UNKNOWN,
                         skipped.discriminator)
        self.assertIn("--no-fetch", skipped.discriminator)
        self.assertIn("LOCAL ref", skipped.discriminator)
        self.assertIn("did not measure", skipped.discriminator)
        fetched = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                         fetch=True), "ff-able")
        self.assertEqual(fetched.verdict, foldcheck.PASS, fetched.discriminator)
        self.assertIn("is an ancestor of the tip", fetched.discriminator)

    def test_the_stale_hint_is_echoed_only_in_the_direction_that_refuses(self):
        """UNKNOWN both ways; the discriminator is asymmetric ON PURPOSE.

        A stale ref that ALREADY reads NOT an ancestor can only send a caller
        to fetch and look, so saying so costs nothing and saves a round trip.
        The opposite reading — "the stale ref says trunk is an ancestor" — is
        the sentence a tired operator spends as consent, and it is the exact
        sentence the descendant scenario above produces, so it is never
        echoed."""
        repo = self._repo()
        handle = self._mint(repo)
        agrees = _named(foldcheck.check(repo, _head(repo), gate_ref=handle,
                                        fetch=False), "ff-able")
        self.assertEqual(agrees.verdict, foldcheck.UNKNOWN,
                         agrees.discriminator)
        # the positive half of THIS arm: the rung did speak, and said the
        # staleness sentence — so the silence below is the hint being withheld
        # and not an empty discriminator that nothing could be found in.
        self.assertIn("did not measure", agrees.discriminator)
        self.assertNotIn("ALREADY reads", agrees.discriminator)
        # the positive control on that same observable: when the stale reading
        # is the REFUSING one, the hint really is printed — so the silence
        # above is the direction and not a hint that never fires.
        other = self._clone(repo)
        self._commit(other, "trunk.txt", "trunk moved\n")
        _run(other, "push", "-q", "origin", "main")
        _run(repo, "fetch", "-q", "origin")
        self._commit(repo, "mine.txt", "my lane\n")
        moved = _named(foldcheck.check(repo, _head(repo),
                                       gate_ref=self._mint(repo), fetch=False),
                       "ff-able")
        self.assertEqual(moved.verdict, foldcheck.UNKNOWN, moved.discriminator)
        self.assertIn("ALREADY reads NOT an ancestor", moved.discriminator)

    def test_a_FAILED_fetch_makes_BOTH_ancestry_rungs_UNKNOWN(self):
        """An unreachable remote has told you nothing — and now it has told
        two rungs nothing.

        Never REFUSE (that manufactures a failure against a lane that may be
        perfectly rebased) and never PASS. The control runs FIRST, with the
        remote intact: both rungs PASS in this repo, so the UNKNOWNs below are
        the broken remote and not two rungs that never say yes. Both verdicts
        also have to carry git's own complaint rather than an exit code — one
        unreadable reason now decides two rungs."""
        repo = self._repo()
        handle = self._mint(repo)
        reachable = foldcheck.check(repo, _head(repo), gate_ref=handle)
        self.assertEqual(_named(reachable, "ff-able").verdict, foldcheck.PASS,
                         _named(reachable, "ff-able").discriminator)
        self.assertEqual(_named(reachable, "origin-has-it").verdict,
                         foldcheck.PASS,
                         _named(reachable, "origin-has-it").discriminator)
        _run(repo, "remote", "set-url", "origin", "../gone.git")
        rungs = foldcheck.check(repo, _head(repo), gate_ref=handle)
        ff, origin = _named(rungs, "ff-able"), _named(rungs, "origin-has-it")
        self.assertEqual([ff.verdict, origin.verdict], [foldcheck.UNKNOWN] * 2,
                         "%r / %r" % (ff.discriminator, origin.discriminator))
        self.assertIn("gone.git", ff.discriminator)
        self.assertIn("gone.git", origin.discriminator)
        # ONE PRINTED ROW PER RUNG, plus the verdict line. git answers an
        # unreachable remote in TWO lines ("fatal: … does not appear to be a
        # git repository" / "fatal: Could not read from remote repository"),
        # and carrying the second one into a discriminator does not make the
        # report longer — it splits one rung's verdict across two rows that
        # read like two rungs.
        self.assertEqual(
            len("\n".join(foldcheck.report(rungs)).splitlines()), 6,
            "a discriminator wrapped onto its own row:\n%s"
            % "\n".join(foldcheck.report(rungs)))
        self.assertFalse(foldcheck.ok(rungs), foldcheck.report(rungs))

    def test_a_DELETED_requested_branch_fails_fetch_and_never_reads_stale_PASS(self):  # noqa: VACUOUS_ASSERTION — `before` unconditionally proves this exact release ref fetches and all five rungs PASS before the second clone deletes it
        """task/340 — an exact branch request that no longer exists upstream.

        A second clone deletes origin/release while this worktree's
        origin/release remains at the delivered tip. The pre-cure fetch of the
        whole remote left that stale tracking ref readable and rung 5 reported
        PASS. The exact refspec must instead fail, making BOTH consumers of the
        snapshot UNKNOWN; a stale local answer is not evidence that the branch
        still exists upstream.
        """
        repo = self._repo()
        _run(repo, "checkout", "-b", "release")
        tip = self._commit(repo, "release.txt", "release tip\n")
        _run(repo, "push", "-q", "-u", "origin", "release")
        handle = self._mint(repo)
        before = foldcheck.check(repo, tip, gate_ref=handle, branch="release")
        self.assertTrue(foldcheck.ok(before), foldcheck.report(before))
        self.assertIn(
            "is on origin/release",
            _named(before, "origin-has-it").discriminator)

        other = self._clone(repo)
        _run(other, "push", "-q", "origin", "--delete", "release")
        self.assertEqual(
            _head(repo, "origin/release"), tip,
            "the local tracking ref did not stay stale after another clone "
            "deleted the upstream branch")

        rungs = foldcheck.check(repo, tip, gate_ref=handle, branch="release")
        ff, origin = _named(rungs, "ff-able"), _named(rungs, "origin-has-it")
        self.assertEqual([ff.verdict, origin.verdict], [foldcheck.UNKNOWN] * 2,
                         foldcheck.report(rungs))
        self.assertIn("could not fetch", origin.discriminator)
        self.assertIn("release", origin.discriminator)
        self.assertFalse(foldcheck.ok(rungs), foldcheck.report(rungs))


class ReviewEvidenceReadersTest(unittest.TestCase):
    """The two readers behind the (unwired) cars-reviewed rung.

    SCOPE, STATED SO THIS IS NOT READ AS COVERAGE IT DOES NOT PROVIDE: these
    arms pin the two PURE readers and nothing else. The rung's verdict logic
    reads the live dispatch ledger and still owes real-repo witnesses in the
    style of the rest of this file — one input it MUST refuse and one it MUST
    pass. Those are not written yet, and the rung is not wired into `check()`.

    BOTH ARMS EXIST BECAUSE I SHIPPED THE BUG EACH ONE CATCHES.
    """

    def test_a_row_stamp_is_PARSED_not_type_tested(self):
        """The exact defect: the ledger writes `ts` as an ISO-8601 STRING, and
        a probe of mine filtered it with `isinstance(value, (int, float))`.
        That excluded all 12,057 live events and returned a confident zero,
        which I then recorded as a measured negative on a task row.

        The positive control is unconditional and comes FIRST, so the zeros
        below are a working reader refusing junk rather than a reader that
        returns 0 for everything.
        """
        self.assertEqual(foldcheck._row_stamp({"ts": "2026-08-28T14:47:39Z"}),
                         1787928459,
                         "the ISO string form the ledger actually writes did "
                         "not parse, so this reader cannot see any real row")
        # ...and it MOVES with its input, so the number above is not a constant
        # returned regardless of what it was handed.
        self.assertGreater(
            foldcheck._row_stamp({"ts": "2026-08-29T14:47:39Z"}),
            foldcheck._row_stamp({"ts": "2026-08-28T14:47:39Z"}))

        self.assertEqual(foldcheck._row_stamp({"ts": 1787928459}), 0,
                         "a NUMERIC ts must read as unusable rather than be "
                         "silently trusted — the ledger does not write one")
        self.assertEqual(foldcheck._row_stamp({"ts": "nonsense"}), 0)
        self.assertEqual(foldcheck._row_stamp({}), 0)
        self.assertEqual(foldcheck._row_stamp(None), 0)

    def test_a_pending_row_does_not_credit_the_work_it_has_not_reviewed(self):  # noqa: VACUOUS_ASSERTION - the rung reads assertEqual(x, foldcheck.PASS) as an absence, so it sees no positive control here; the real vacuity it points at (an empty _NO_REVIEW_YET satisfying the loop) is closed by assertTrue on the set plus a counted-iterations assertion, and the mock's own called-check proves the double was in effect
        """The exact probe (row 48eb1ddffc4248ef), as a standing arm.

        THE DEFECT WAS AN ORDER, not a predicate: `_NO_REVIEW_YET` sat BELOW
        the ref handling, so a CANCELLED or still-OPEN pairless row was already
        in `ranges` by the time it was skipped. Its tip's patch identity
        counted as review evidence, and an UNREVIEWED folded commit could PASS
        on the strength of a review nobody had performed.

        ONE FIELD MOVES. The same repo, the same row, the same ref — only
        `status` changes — so a PASS here can only come from the status, which
        is the thing under test. A pair of unrelated fixtures could differ for
        any reason and would prove nothing about the ordering.
        """
        from unittest import mock
        from helm import dispatches, rowworld
        repo = _repo_with_origin()
        self.addCleanup(shutil.rmtree, os.path.dirname(repo), True)
        with open(os.path.join(repo, "b.txt"), "w") as fh:
            fh.write("two\n")
        _run(repo, "add", "b.txt")
        _run(repo, "commit", "-m", "two")
        tip = _head(repo)
        gitdir, err = rowworld.repo_identity(repo)
        self.assertIsNone(err, err)

        def row(status):
            return {"id": "a" * 32, "repo_id": gitdir, "ref": tip,
                    "kind": "review", "status": status,
                    "ts": "2026-09-09T19:00:00Z", "lane": "l", "seq": 1}

        def verdict(status):
            """Run the rung against ONE synthetic row, and PROVE the double
            was in effect. `_cars_reviewed` does `from . import dispatches`
            inside the function, which reads the PACKAGE ATTRIBUTE — patching
            it works, but a future refactor to a module-level import or a
            different accessor would silently bypass this and every assertion
            below would then be measuring the LIVE ledger while still passing.
            So the fake counts its own calls and the caller checks."""
            fake = mock.Mock(
                return_value=({row(status)["id"]: row(status)}, None))
            with mock.patch.object(dispatches, "snapshot", fake):
                got = foldcheck._cars_reviewed(
                    vcs.backend(repo), repo, tip, "origin/main")
            self.assertTrue(
                fake.called,
                "the rung never read the patched ledger, so this arm is "
                "measuring the live one")
            return got

        # POSITIVE CONTROL FIRST, unconditional: a row that HAS reviewed does
        # credit its tip, so a REFUSE below is the status being read and not
        # the harness failing to reach the credit at all.
        reviewed = verdict("verdict")
        self.assertEqual(reviewed.verdict, foldcheck.PASS, reviewed.discriminator)

        # ...and each pending status withholds that same credit. THE LOOP IS
        # COUNTED because an EMPTY _NO_REVIEW_YET would satisfy every
        # assertion inside it by never running — the vacuous-assertion rung
        # caught exactly that here, for the second time tonight on my own
        # code, and it was right both times.
        self.assertTrue(foldcheck._NO_REVIEW_YET,
                        "no pending statuses to withhold credit from")
        checked = 0
        for status in sorted(foldcheck._NO_REVIEW_YET):
            got = verdict(status)
            checked += 1
            self.assertNotEqual(
                got.verdict, foldcheck.PASS,
                "status %r credited a commit it has not reviewed: %s"
                % (status, got.discriminator))
        self.assertEqual(checked, len(foldcheck._NO_REVIEW_YET))

    def test_an_OPEN_row_with_a_verified_retip_is_not_credited_either(self):  # noqa: VACUOUS_ASSERTION - the absence-shaped half is assertNotEqual(verdict, PASS); its unconditional positive control is the SAME fixture with status "verdict" asserted == PASS two lines earlier, and the must-hit below proves the row takes the PAIRED path rather than the pairless one
        """The SECOND probe on this rung (row 5bf9eef37584496b).

        ROUND ONE moved `_NO_REVIEW_YET` above the ref handling. I then left
        it INSIDE THE PAIRLESS ARM, which fixed the instance and not the
        invariant: an OPEN row carrying a VERIFIED RETIP derives a real
        (base, tip) from `_work_pair`, so it never reached the guard at all
        and its unreviewed patch was credited. A Fab probe returned a
        false PASS — "all 1 folded commit inside a dispatched review range" —
        and my own focused arms were green straight through it, because every
        one of them built a PAIRLESS row.

        `retip` is OPEN-only by construction, so this state is
        production-reachable rather than contrived.

        THE MUST-HIT below is the whole reason this arm is not a duplicate of
        its sibling: it asserts `_work_pair` actually returns a pair for this
        row. Without it, a fixture that quietly fell back to the pairless path
        would pass this test while testing nothing new.
        """
        from unittest import mock
        from helm import dispatches, rowworld
        repo = _repo_with_origin()
        self.addCleanup(shutil.rmtree, os.path.dirname(repo), True)
        base_sha = _head(repo)
        with open(os.path.join(repo, "b.txt"), "w") as fh:
            fh.write("two\n")
        _run(repo, "add", "b.txt")
        _run(repo, "commit", "-m", "two")
        tip = _head(repo)
        gitdir, err = rowworld.repo_identity(repo)
        self.assertIsNone(err, err)
        build_id, review_id = "c" * 32, "d" * 32

        def rows_for(status):
            return {
                build_id: {"id": build_id, "repo_id": gitdir, "kind": "build",
                           "chain_root": build_id, "tip": base_sha,
                           "status": "verdict", "ts": "2026-09-09T18:00:00Z",
                           "lane": "l", "seq": 1},
                review_id: {"id": review_id, "repo_id": gitdir,
                            "kind": "review", "status": status,
                            "chain_root": build_id, "tip": tip, "ref": tip,
                            "retips": [{"identity": "verified", "tip": tip}],
                            "ts": "2026-09-09T19:00:00Z", "lane": "l",
                            "seq": 2},
            }

        # MUST-HIT: this fixture must take the PAIRED path. If _work_pair
        # returns (None, None) the row falls into the pairless arm and this
        # arm silently becomes a copy of the one above it.
        rows = rows_for("open")
        pair = rowworld._work_pair(rows[review_id], rows,
                                   rowworld._carriers(rows))
        self.assertEqual(pair, (base_sha, tip),
                         "fixture did not take the PAIRED path (_work_pair "
                         "gave %r), so this arm would re-test the pairless "
                         "case" % (pair,))

        def verdict(status):
            fake = mock.Mock(return_value=(rows_for(status), None))
            with mock.patch.object(dispatches, "snapshot", fake):
                got = foldcheck._cars_reviewed(
                    vcs.backend(repo), repo, tip, "origin/main")
            self.assertTrue(fake.called, "the rung never read the patched "
                                         "ledger, so this arm is measuring "
                                         "the live one")
            return got

        # POSITIVE CONTROL, unconditional and on the SAME fixture: a settled
        # review row on the paired path DOES credit its work.
        settled = verdict("verdict")
        self.assertEqual(settled.verdict, foldcheck.PASS, settled.discriminator)

        # ...and every pending status withholds it, paired or not.
        self.assertTrue(foldcheck._NO_REVIEW_YET,
                        "no pending statuses to withhold credit from")
        checked = 0
        for status in sorted(foldcheck._NO_REVIEW_YET):
            got = verdict(status)
            checked += 1
            self.assertNotEqual(
                got.verdict, foldcheck.PASS,
                "a PAIRED %r row credited work it has not reviewed: %s"
                % (status, got.discriminator))
        self.assertEqual(checked, len(foldcheck._NO_REVIEW_YET))

    def test_a_sibling_range_cancels_a_commit_the_index_names_explicitly(self):
        """THE GIT SEMANTIC THAT MADE THIS RUNG SILENTLY WRONG, on a real repo.

        `git log A..B C..D` is NOT a union of ranges. It is (every positive
        tip) MINUS (every negative base), ONE global negative set — so a base
        recorded by any one spec removes that commit from the WHOLE index,
        including from a sibling spec that names it explicitly. A review chain
        produces exactly that shape, because round two's base IS round one's
        tip, so every lane's EARLIEST commit was excluded by its own later
        rounds and the rung refused composed trains of properly reviewed cars.

        The three specs below are the minimal discriminator, and the FIRST is
        the unconditional positive control: the same commit, in the same repo,
        indexed alone.
        """
        from helm import rowworld
        repo = _repo_with_origin()
        # THE ROOT IS ONE LEVEL UP, NOT TWO. `_repo_with_origin` returns
        # <root>/work, so dirname(repo) IS the mkdtemp root that holds both
        # origin.git and work. My first cut went up twice and resolved to
        # /tmp itself, and rmtree(ignore_errors=True) fired three times before
        # the missing harness files told me what I had written.
        self.addCleanup(shutil.rmtree, os.path.dirname(repo), True)
        with open(os.path.join(repo, "b.txt"), "w") as fh:
            fh.write("two\n")
        _run(repo, "add", "b.txt")
        _run(repo, "commit", "-m", "two")
        first = _head(repo)
        with open(os.path.join(repo, "c.txt"), "w") as fh:
            fh.write("three\n")
        _run(repo, "add", "c.txt")
        _run(repo, "commit", "-m", "three")
        second = _head(repo)
        gitdir = os.path.join(repo, ".git")

        alone, err = rowworld._patch_ids(gitdir, ["%s^!" % first])
        self.assertIsNone(err, err)
        self.assertIn(first, alone,
                      "the positive control failed: this commit does not index "
                      "even on its own, so nothing below measures cancellation")
        target = alone[first]

        # THE DEFECT: the very same spec, beside a range that merely STARTS at
        # that commit, and the commit disappears.
        both, err = rowworld._patch_ids(
            gitdir, ["%s^!" % first, "%s..%s" % (first, second)])
        self.assertIsNone(err, err)
        self.assertNotIn(target, set(both.values()),
                         "if this now passes, git stopped treating range bases "
                         "as one global negative set and the tips-only index "
                         "below is no longer load-bearing")

        # THE CURE the rung uses: positive tips, ONE negative (the trunk), so
        # no base is ever a negative and nothing can cancel anything.
        tips, err = rowworld._patch_ids(gitdir, [second, "^origin/main"])
        self.assertIsNone(err, err)
        self.assertIn(target, set(tips.values()))
        self.assertIn(second, tips)

    def test_a_row_that_reviewed_nothing_is_not_an_evidence_gap(self):
        """CANCELLED means withdrawn and OPEN/HELD mean not answered yet, so
        none of them can account for a commit. Counting them as "a row that
        might cover this" inflated the blind set by 37% — 462 of 1263 live
        rows — with rows that reviewed no code by construction.

        Pinned by IDENTITY, the exact status strings, because a shape rule here
        would silently admit whatever status word gets added next.
        """
        # POSITIVE MEMBERSHIP FIRST, unconditional and on the SAME object the
        # absence assertions below read, so an empty or renamed set cannot
        # satisfy them by vacuity.
        self.assertIn("cancelled", foldcheck._NO_REVIEW_YET)
        self.assertIn("open", foldcheck._NO_REVIEW_YET)
        self.assertIn("held", foldcheck._NO_REVIEW_YET)
        self.assertEqual(foldcheck._NO_REVIEW_YET,
                         frozenset(("cancelled", "open", "held")))
        # The states that DO carry review evidence must stay OUT of the set, or
        # the horizon would ignore real gaps and the rung would refuse work a
        # real row covered.
        for live in ("verdict", "closed"):
            self.assertNotIn(live, foldcheck._NO_REVIEW_YET,
                             "%r carries review evidence and must count "
                             "toward the blind horizon" % live)


class FoldCheckCliTest(FoldCheckBase):
    """The tail is a closed set, and it was not.

    Reading options by `opts.index(name) + 1` raised IndexError on a trailing
    `--gate-tree` and silently IGNORED `--bogus` — so the verb answered a
    question the caller did not ask and the caller read the answer as though
    it had. `guard_tail` is the closed-set reader; these are its witnesses.
    """

    def _lr(self, *argv):
        """-> (rc, stdout, stderr). The verb is measured through cmd_lr, not
        through foldcheck.check, because the defect was in the ARGUMENTS."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def _fold_with_proof(self, rows):
        repo = self._repo()
        with mock.patch.object(foldcompose, "project_state",
                               return_value=("registered", "fixture")), \
                mock.patch.object(foldcompose, "composition_proof",
                                  return_value=rows):
            return self._lr("foldcheck", _head(repo), "--repo", repo,
                            "--gate", self._mint(repo))

    def test_pre_activation_fold_keeps_the_exact_five_rung_contract(self):
        rc, out, err = self._fold_with_proof(None)
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("all five PASSED", out)
        self.assertNotIn("composition proof", out)

    def test_active_fold_invokes_and_requires_the_separate_proof_stage(self):
        rc, out, err = self._fold_with_proof([
            ("manifest", False, "active tip carries no readable manifest")])
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("all five PASSED", out)
        self.assertIn("composition proof:", out)
        self.assertIn("STOP", out)
        rc, out, err = self._fold_with_proof([
            ("replay:row-a", True, "replay and APPROVE both pass")])
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("ok", out)

    def test_active_fold_refuses_UNKNOWN_distinctly(self):
        rc, out, err = self._fold_with_proof([
            ("activation", None, "activation record is unreadable")])
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("????", out)
        self.assertNotIn("STOP  activation", out)

    def test_a_trailing_gate_flag_exits_2_instead_of_raising_IndexError(self):
        """The crash arm. `--gate` with nothing after it used to index past
        the end of the list; a traceback is not a refusal, and a caller
        gating on rc got neither."""
        repo = self._repo()
        rc, _out, err = self._lr("foldcheck", _head(repo), "--repo", repo,
                                 "--gate")
        self.assertEqual(rc, 2)
        self.assertIn("--gate wants a value", err)
        # positive control on the same observable lives in
        # test_a_well_formed_invocation_reaches_the_rungs_and_exits_0: the
        # same verb with a value returns 0 and prints all five rungs. Named
        # here so the pairing is deliberate rather than incidental.

    def test_remote_value_is_forwarded_to_the_real_fetch_and_rungs(self):
        """The parser must not merely accept --remote; its VALUE reaches the
        fetch and both ancestry rungs. Renaming origin makes the default name
        invalid, so rc0 is impossible if the value is dropped."""
        repo = self._repo()
        _run(repo, "remote", "rename", "origin", "upstream")
        rc, out, err = self._lr(
            "foldcheck", _head(repo), "--repo", repo,
            "--gate", self._mint(repo), "--remote", "upstream")
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("upstream/main", out)
        self.assertIn("all five PASSED", out)

    def test_branch_value_is_forwarded_to_the_real_fetch_and_rungs(self):
        """A release-only commit is not on origin/main. This invocation can
        pass only if --branch release reaches both the exact refspec fetch and
        the ancestry comparisons."""
        repo = self._repo()
        _run(repo, "checkout", "-b", "release")
        tip = self._commit(repo, "release.txt", "release only\n")
        _run(repo, "push", "-q", "-u", "origin", "release")
        rc, out, err = self._lr(
            "foldcheck", tip, "--repo", repo,
            "--gate", self._mint(repo), "--branch", "release")
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("origin/release", out)
        self.assertIn("all five PASSED", out)

    def test_valued_remote_and_branch_flags_refuse_cleanly_when_missing(self):  # noqa: VACUOUS_ASSERTION — the unconditional ok_rc call immediately below proves both valued flags with real values reach all five PASS before the missing-value loop
        """task/340 — both valued additions get the same clean refusal as
        --gate: rc2, no traceback, and no rung work before the refusal."""
        repo = self._repo()
        ok_rc, ok_out, ok_err = self._lr(
            "foldcheck", _head(repo), "--repo", repo,
            "--gate", self._mint(repo), "--remote", "origin",
            "--branch", "main")
        self.assertEqual(ok_rc, 0, (ok_out, ok_err))
        self.assertIn("all five PASSED", ok_out)
        for flag in ("--remote", "--branch"):
            with self.subTest(flag=flag):
                rc, out, err = self._lr(
                    "foldcheck", _head(repo), "--repo", repo, flag)
                self.assertEqual(rc, 2)
                self.assertIn("%s wants a value" % flag, err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("tip-exists", out)

    def test_an_unknown_flag_exits_2_rather_than_being_silently_ignored(self):
        """The quieter arm and the worse one. `--bogus` used to fall through
        and the verb printed a clean report — an answer to a question nobody
        asked, indistinguishable from the one they did."""
        repo = self._repo()
        rc, out, err = self._lr("foldcheck", _head(repo), "--repo", repo,
                                "--bogus")
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg '--bogus'", err)
        # and it refused BEFORE doing the work: no rung report was printed.
        self.assertNotIn("tip-exists", out)

    def test_a_missing_tip_prints_the_usage_and_exits_2(self):
        repo = self._repo()
        rc, _out, err = self._lr("foldcheck", "--repo", repo)
        self.assertEqual(rc, 2)
        self.assertIn("helm lr foldcheck <tip>", err)

    def test_a_well_formed_invocation_reaches_the_rungs_and_exits_0(self):
        """THE POSITIVE CONTROL for this whole class. Every refusal above is
        worthless if the verb refuses everything, and this arm also proves the
        plumbing: `--gate` and `--repo` have to reach `foldcheck.check` for a
        five-PASS report to be possible at all."""
        repo = self._repo()
        rc, out, _err = self._lr("foldcheck", _head(repo), "--repo", repo,
                                 "--gate", self._mint(repo))
        self.assertEqual(rc, 0, out)
        self.assertIn("all five PASSED", out)
        for name in ("tip-exists", "tree-vs-gate", "ff-able", "head-clean",
                     "origin-has-it"):
            self.assertIn(name, out)

    def test_a_passing_fold_names_the_leases_the_trunk_now_carries(self):
        """A LAND RELEASES NO LEASE, so a board reading leases draws the lanes
        a land carried as building ("about half the listed lanes already
        landed", the owner). After the five PASS, the fold names
        each held lane whose work is on the trunk with the exact release line,
        and leaves out the lane it did not carry. Advisory: rc is unchanged."""
        from helm import seats, work
        repo = self._repo()
        seat = os.environ["HELM_CHAT_NAME"]
        for lane in ("carried", "still-open"):
            rc, line = work.claim(repo, lane, seat)
            self.assertEqual(rc, 0, line)
            self._commit(work.lane_path(repo, lane), lane + ".txt",
                         lane + " work\n")
        _run(repo, "merge", "--no-ff", "-q", "-m", "train: merge lane carried",
             "lane/carried")
        _run(repo, "push", "-q", "origin", "main")
        rc, out, err = self._lr("foldcheck", _head(repo), "--repo", repo,
                                "--gate", self._mint(repo))
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("all five PASSED", out)
        self.assertIn("LEASES ON LANDED WORK", out)
        tail = out.split("LEASES ON LANDED WORK", 1)[1]
        self.assertIn("carried (", tail)
        self.assertIn("LANDED by ancestry", tail)
        self.assertIn("helm work release carried --lease ", tail)
        self.assertNotIn("still-open", tail,
                         "a lane the land did not carry was offered for release")
        # IT RELEASED NOTHING
        self.assertEqual(sorted(c["resource"].rsplit(":", 1)[1]
                                for c in seats.claims_list()),
                         ["carried", "still-open"])

    def test_an_uncited_gate_exits_1_because_not_measured_is_not_consent(self):
        """The rc contract a fold script gates on: UNKNOWN and REFUSE share an
        exit code, because both are reasons not to announce a land."""
        repo = self._repo()
        rc, out, _err = self._lr("foldcheck", _head(repo), "--repo", repo,
                                 "--no-fetch")
        self.assertEqual(rc, 1)
        self.assertIn("NOT PROVEN", out)
        # positive control on the same observable: the cited, fetched call in
        # the same repo exits 0 (see the arm above) — asserted here too so
        # this rc contract is not read off a verb that only ever fails.
        ok_rc, ok_out, _ = self._lr("foldcheck", _head(repo), "--repo", repo,
                                    "--gate", self._mint(repo))
        self.assertEqual(ok_rc, 0, ok_out)


if __name__ == "__main__":
    unittest.main()
