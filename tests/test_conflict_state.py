#!/usr/bin/env python3
"""The DANGLING CONFLICT — the tree state no agent can recover from by reflex.

It hit this repo's shared checkout FIVE times on 2026-07-24 and `helm work list`
reported the tree as ordinary every single time. The shape: conflict stages in
the index with NO operation in progress, which is what a conflicted `git stash
apply/pop` leaves behind. Why that is uniquely bad:

  * `git merge --abort` REFUSES, correctly — git sees no merge in progress, so
    the reflex an agent reaches for does not apply.
  * so the natural next move is `git reset --hard`, which is exactly the move
    that would destroy a hand-resolution if someone had started one.
  * and nothing in helm named the state, so the only way to find it was to trip
    over a failed merge or a SyntaxError from committed conflict markers.

The porcelain parser ALREADY distinguished an unmerged record (`u`) and every
caller collapsed it into plain "dirty". This suite pins the distinction, and it
builds the real state with a real stash rather than a fixture, because a fixture
of a state is not the state.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm.work import _lanes                                    # noqa: E402


class ConflictStateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-conflict-")
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self._write("a.txt", "base\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.repo] + list(args),
                              capture_output=True, text=True)

    def _write(self, name, body):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(body)

    def _stash_conflict(self):
        """Build the REAL dangling state: stash an edit, commit a conflicting
        one, then apply the stash. git leaves conflict stages and NO MERGE_HEAD."""
        self._write("a.txt", "stashed change\n")
        self._git("stash", "push", "-q", "-m", "the edit")
        self._write("a.txt", "committed change\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "conflicting")
        self._git("stash", "apply")          # conflicts, by construction

    # --- the state that was invisible ---------------------------------------

    def test_a_dangling_stash_apply_conflict_is_NAMED(self):
        self._stash_conflict()
        st = _lanes._room_status(self.repo)
        self.assertTrue(st["dirty"])
        self.assertGreater(st["conflicts"], 0, "unmerged paths must be counted, "
                                               "not folded into plain dirty")
        self.assertEqual(st["operation"], "",
                         "a stash apply leaves NO operation in progress — that "
                         "is precisely what makes it unrecoverable by reflex")
        self.assertTrue(st["dangling_conflict"])

    def test_git_really_cannot_abort_it(self):
        """Proves the premise rather than asserting it: the reflex an agent
        reaches for genuinely does not work here, which is WHY naming it matters."""
        self._stash_conflict()
        rc = self._git("merge", "--abort")
        self.assertNotEqual(rc.returncode, 0,
                            "if git CAN abort this, the whole premise is wrong")

    # --- and the states it must NOT be confused with -------------------------

    def test_a_real_merge_conflict_is_conflicted_but_NOT_dangling(self):
        """The distinction is the entire point: a mid-merge conflict is
        recoverable by reflex (abort or continue), so it must not be reported as
        the dangling kind or the remedy would be wrong."""
        self._git("checkout", "-q", "-b", "other")
        self._write("a.txt", "other side\n")
        self._git("commit", "-qam", "other")
        self._git("checkout", "-q", "main")
        self._write("a.txt", "main side\n")
        self._git("commit", "-qam", "mainside")
        self._git("merge", "other")          # conflicts
        st = _lanes._room_status(self.repo)
        self.assertGreater(st["conflicts"], 0)
        self.assertEqual(st["operation"], "merge")
        self.assertFalse(st["dangling_conflict"],
                         "a mid-merge conflict CAN be aborted — calling it "
                         "dangling would send the agent to the wrong remedy")

    def test_ordinary_dirt_is_not_a_conflict(self):
        self._write("a.txt", "just editing\n")
        st = _lanes._room_status(self.repo)
        self.assertTrue(st["dirty"])
        self.assertEqual(st["conflicts"], 0)
        self.assertFalse(st["dangling_conflict"])

    def test_a_clean_room_reports_the_new_keys_too(self):
        """Every early return must carry them, or a caller reading the fields
        gets a KeyError on exactly the paths that matter most."""
        st = _lanes._room_status(self.repo)
        self.assertFalse(st["dirty"])
        for key in ("conflicts", "operation", "dangling_conflict"):
            self.assertIn(key, st)
        self.assertEqual(st["conflicts"], 0)
        self.assertFalse(st["dangling_conflict"])

    # --- could-not-look is never "no operation" ------------------------------

    def test_an_unreadable_gitdir_is_UNKNOWN_not_no_operation(self):
        """None is a third answer on purpose. If we could not read the git dir we
        must not report "no operation in progress", because that is what makes a
        conflict look dangling — and a false dangling verdict recommends
        `reset --hard`, the one destructive move. Same class as the marker probe
        at the filesystem layer and the event ledger's (rows, unavailable)."""
        self.assertIsNone(_lanes._operation_in_progress(
            os.path.join(self.tmp, "not-a-repo-at-all")))

    def test_unknown_operation_never_reads_as_dangling(self):
        self._stash_conflict()
        real = _lanes._operation_in_progress
        try:
            _lanes._operation_in_progress = lambda _p: None    # could not look
            st = _lanes._room_status(self.repo)
        finally:
            _lanes._operation_in_progress = real
        self.assertGreater(st["conflicts"], 0)
        self.assertIsNone(st["operation"])
        self.assertFalse(st["dangling_conflict"],
                         "UNKNOWN must never be promoted to the dangling "
                         "verdict — we did not look")



class BoardSurfacesTheConflictTest(unittest.TestCase):
    """The pin that would have caught my own two-authorities miss.

    The unit tests above pass `_room_status` directly, so they ALL passed while
    `helm work list` still printed plain "dirty" for a dangling conflict — because
    the board read a SECOND, cruder oracle (`_gc._dirty`, its own
    `git status --porcelain` answering only a boolean). Only dogfooding the real
    state on the real command found it. So this asserts the BOARD row, not the
    helper: the same one-question-one-authority law a review raised twice today,
    pinned at the surface an operator actually reads.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-board-")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self._git(self.root, "init", "-q", "-b", "main", ".")
        self._git(self.root, "config", "user.email", "t@t")
        self._git(self.root, "config", "user.name", "t")
        with open(os.path.join(self.root, "a.txt"), "w") as f:
            f.write("base\n")
        self._git(self.root, "add", "-A")
        self._git(self.root, "commit", "-qm", "base")
        # a lane room where helm expects one: <root>-wt/<lane>
        self.lane = os.path.join(self.tmp, "proj-wt", "demo")
        self._git(self.root, "worktree", "add", "-q", "-b", "lane/demo",
                  self.lane)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, where, *args):
        return subprocess.run(["git", "-C", where] + list(args),
                              capture_output=True, text=True)

    def _row(self):
        from helm.work import _gc
        rows = _gc.list_rows(self.root)
        return next(r for r in rows if r["lane"] == "demo")

    def test_the_board_row_names_a_dangling_conflict(self):
        with open(os.path.join(self.lane, "a.txt"), "w") as f:
            f.write("stashed\n")
        self._git(self.lane, "stash", "push", "-q", "-m", "demo edit")
        with open(os.path.join(self.lane, "a.txt"), "w") as f:
            f.write("committed\n")
        self._git(self.lane, "commit", "-qam", "conflicting")
        self._git(self.lane, "stash", "apply")
        row = self._row()
        self.assertTrue(row["dirty"])
        self.assertGreater(row["conflicts"], 0,
                           "the BOARD must carry the conflict count, not just "
                           "a dirty boolean from a second status oracle")
        self.assertTrue(row["dangling_conflict"])

    def test_a_clean_board_row_carries_the_keys(self):
        row = self._row()
        self.assertFalse(row["dirty"])
        self.assertEqual(row["conflicts"], 0)
        self.assertFalse(row["dangling_conflict"])


class RemedyScopeTest(unittest.TestCase):
    """The printed remedy must be scoped to the CONFLICTED PATHS.

    The note used to end with `git -C <path> reset --hard HEAD`, guarded by a
    check that every conflicted file still carried its <<<<<<< markers. That
    guard is real but it is scoped WRONG: it inspects the conflicted paths, then
    prescribes a command that discards every OTHER uncommitted change in the room
    as well — changes the check never looked at. Caught live on 2026-07-24 in the
    `roster-read-cache` room, whose dangling conflict was unrelated to a modified
    test file sitting beside it; following our own note would have destroyed that
    file. A remedy an operator cannot safely follow is worse than no remedy,
    which is the same law that keeps `lr stalls` from naming a fix the dispatch
    ledger would refuse.
    """

    def test_the_remedy_is_per_path_restore_not_a_tree_wide_reset(self):
        from helm.work._cli import _DANGLING_NOTE
        note = _DANGLING_NOTE % (2, "/tmp/room")
        self.assertIn("restore --staged --worktree --", note)
        self.assertIn("ONLY THE CONFLICTED PATHS", note)

    def test_the_note_never_prescribes_reset_hard(self):
        """The mutation kill: `reset --hard` may appear ONLY as a prohibition.
        Every line mentioning it must also carry the refusal, so restoring the
        old remedy — a bare `reset --hard HEAD` as the instruction — fails."""
        from helm.work._cli import _DANGLING_NOTE
        note = _DANGLING_NOTE % (2, "/tmp/room")
        for line in note.splitlines():
            if "reset --hard" in line:
                self.assertTrue(
                    "Do NOT" in line or "not" in line.lower(),
                    "reset --hard appears without a prohibition: %r" % line)

    def test_the_note_still_explains_WHY_the_scope_matters(self):
        """A bare 'do not' gets deleted by the next person tidying up; the reason
        is what makes a rule survive."""
        from helm.work._cli import _DANGLING_NOTE
        note = _DANGLING_NOTE % (2, "/tmp/room")
        self.assertIn("every OTHER uncommitted", note)
        self.assertIn("<<<<<<<", note)      # the marker precondition survives
        self.assertIn("stash@{N}", note)    # and the position-vs-id warning


if __name__ == "__main__":
    unittest.main()
