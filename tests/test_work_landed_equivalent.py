#!/usr/bin/env python3
"""`helm work gc` retires a LANDED-EQUIVALENT room (task/1026).

A lane whose change is on the trunk under another sha is invisible to
ancestry and to whole-branch patch identity (`git cherry`): both say no to a
lane that merged the trunk back in, or whose work landed as its `-rN`
rebuild, so without a per-commit read the sweep keeps such a room forever.
The row's example: lane cursor-bridge-context-count-3374 landed as train336
under lane cursor-bridge-context-count-3374-r2.

These arms pin both halves. A room retires on printable evidence per commit:
its own patch-id on the trunk, a train merge naming this lane or its `-rN`
successor that carried the commit, or a merge commit that adds nothing of its
own. Anything else keeps the room for triage and says which commit and why:
an uncommitted change, a commit whose patch is not on the trunk, a commit
rewritten after the train merged its copy, a merge with a change of its own,
and a predecessor's train, which never speaks for its rebuild.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import vcs, work  # noqa: E402
from helm.work import _gc as _work_gc  # noqa: E402
from tests.test_work import WorkBase, _sh  # noqa: E402

# A FIXED CLOCK FOR EVERY AUTHORED COMMIT. The fleet commits under one shared
# identity, so an author time is what tells two commits apart; each arm names
# its own seconds so no two unrelated commits share one by accident.
T = 1790000000


class LandedEquivalentBase(WorkBase):

    def commit(self, cwd, name, text, authored, committed=None, msg=None):
        """Write `name`, commit it at the given author and committer times,
        and return the new sha."""
        with open(os.path.join(cwd, name), "w") as f:
            f.write(text)
        self.assertEqual(_sh(cwd, "git", "add", "-A").returncode, 0)
        env = dict(os.environ,
                   GIT_AUTHOR_DATE="@%d +0000" % authored,
                   GIT_COMMITTER_DATE="@%d +0000" % (committed or authored))
        r = self.git(cwd, env, "commit", "-q", "-m", msg or "work " + name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.sha(cwd, "HEAD")

    def git(self, cwd, env, *args):
        import subprocess
        return subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=30, env=env)

    def sha(self, cwd, rev):
        return _sh(cwd, "git", "rev-parse", rev).stdout.strip()

    def move_trunk(self, name, authored):
        """Advance the trunk first, so no copy of a lane commit on it can be
        the lane commit itself."""
        return self.commit(self.root, name, "trunk moved %s\n" % name,
                           authored)

    def pick(self, sha):
        """Land a rebased copy of `sha` on the trunk (same author and author
        time, new sha), the integrator's own move."""
        r = _sh(self.root, "git", "cherry-pick", sha)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.sha(self.root, "HEAD")

    def train(self, branch, subject, committed=None):
        """Merge `branch` onto the trunk with a train's car subject."""
        env = dict(os.environ)
        if committed:
            env.update(GIT_AUTHOR_DATE="@%d +0000" % committed,
                       GIT_COMMITTER_DATE="@%d +0000" % committed)
        r = self.git(self.root, env, "merge", "--no-ff", "-q", "-m", subject,
                     branch)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.sha(self.root, "HEAD")

    def row(self, path):
        return next(r for r in work.gc_scan(self.root) if r["path"] == path)

    def assertNotRetirableByTheOldProofs(self, branch):
        """The premise: ancestry and whole-branch patch identity say no."""
        self.assertNotIn(_work_gc._merge_state(self.root, branch),
                         (vcs.ANCESTOR, vcs.PATCH_EQUIVALENT))


class RetiresOnEvidenceTest(LandedEquivalentBase):

    def test_a_lane_that_merged_the_trunk_back_in_retires_by_patch_id(self):  # noqa: VACUOUS_ASSERTION — the remove verdict, the LANDED_EQUIVALENT proof and the named sha pair are the positive controls
        """The lane's commit landed as a cherry-pick, then the lane merged the
        trunk in. `git cherry` skips the merge, so the range cannot be
        counted, and whole-branch patch identity reads UNKNOWN. Per commit,
        the lane's commit is on the trunk by its own patch-id and the merge
        adds nothing of its own."""
        path = self.room("mergedin")
        mine = self.commit(path, "a.txt", "lane work\n", T + 1)
        self.move_trunk("moved-1.txt", T + 2)
        copy = self.pick(mine)
        self.assertEqual(_sh(path, "git", "merge", "-q", "--no-edit",
                             "main").returncode, 0)
        self.assertNotRetirableByTheOldProofs("lane/mergedin")

        row = self.row(path)
        self.assertEqual(row["verdict"], "remove", row["why"])
        self.assertEqual(row["proof"], _work_gc.LANDED_EQUIVALENT)
        self.assertIn("LANDED-EQUIVALENT", row["why"])
        self.assertIn("patch-id", row["why"])
        self.assertIn("%s as %s" % (mine[:12], copy[:12]), row["why"])
        self.assertIn("1 merge adding nothing of its own", row["why"])

    def test_a_lane_whose_rebuild_landed_by_train_retires_naming_the_train(self):  # noqa: VACUOUS_ASSERTION — the remove verdict, the LANDED_EQUIVALENT proof and the named train merge are the positive controls
        """The row's own example: lane X landed as a train merge of lane
        X-r2, whose copy of the commit was reworked, so no patch-id matches.
        The train record naming the successor carried it."""
        path = self.room("bridge")
        mine = self.commit(path, "bridge.txt", "first cut\n", T + 10,
                           msg="bridge: first cut")
        rebuild = self.room("bridge-r2")
        theirs = self.commit(rebuild, "bridge.txt", "reworked cut\n", T + 10,
                             msg="bridge: reworked cut, over the budget")
        merge = self.train("lane/bridge-r2",
                           "train9: merge lane bridge-r2 (task/1, P1, rebuilt "
                           "by a seat)")
        self.assertNotRetirableByTheOldProofs("lane/bridge")

        row = self.row(path)
        self.assertEqual(row["verdict"], "remove", row["why"])
        self.assertEqual(row["proof"], _work_gc.LANDED_EQUIVALENT)
        self.assertIn("train9 %s" % merge[:12], row["why"])
        self.assertIn("lane bridge-r2", row["why"])
        self.assertIn("%s as %s" % (mine[:12], theirs[:12]), row["why"])

    def test_a_train_naming_the_lane_itself_at_a_retip_carries_it(self):
        """A retip made in another room lands under the lane's own name
        (`<train>: merge lane <lane> at its retip <sha>`), and the lane's room
        still holds the pre-retip commit."""
        path = self.room("retip")
        mine = self.commit(path, "retip.txt", "before the retip\n", T + 20)
        other = self.room("retip-scratch")
        theirs = self.commit(other, "retip.txt", "after the retip\n", T + 20)
        self.train("lane/retip-scratch",
                   "train12: merge lane retip at its retip %s (task/2)"
                   % theirs[:10])
        row = self.row(path)
        self.assertEqual(row["verdict"], "remove", row["why"])
        self.assertIn("lane retip", row["why"])
        self.assertIn(mine[:12], row["why"])

    def test_the_dry_run_proposes_it_and_changes_nothing(self):
        path = self.room("drybridge")
        self.commit(path, "dry.txt", "first\n", T + 30)
        rebuild = self.room("drybridge-r3")
        self.commit(rebuild, "dry.txt", "second\n", T + 30)
        self.train("lane/drybridge-r3", "train13: merge lane drybridge-r3")
        rc, out, err = self.work("gc")
        self.assertEqual(rc, 0, err)
        line = next(ln for ln in out.splitlines() if " drybridge " in ln)
        self.assertIn("REMOVE", line)
        self.assertIn("LANDED-EQUIVALENT", line)
        self.assertIn("train13", line)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/drybridge"))

    def test_enact_retires_the_room_and_preserves_the_tip(self):  # noqa: VACUOUS_ASSERTION — the preserved ref reading back as the tip and the restore line are the positive controls
        """The weaker grade of proof pays the same way patch identity does:
        the tip is written to refs/helm-retired/ before the branch goes."""
        path = self.room("enacted")
        self.commit(path, "enacted.txt", "first\n", T + 35)
        tip = self.sha(path, "HEAD")
        rebuild = self.room("enacted-r2")
        self.commit(rebuild, "enacted.txt", "second\n", T + 35)
        self.train("lane/enacted-r2", "train14: merge lane enacted-r2")
        row = self.row(path)
        self.assertEqual(row["verdict"], "remove", row["why"])
        blob = "\n".join(work.gc_enact(self.root, row))
        self.assertFalse(os.path.exists(path), blob)
        self.assertFalse(work._has_branch(self.root, "lane/enacted"))
        self.assertIn("LANDED-EQUIVALENT", blob)
        keep = _work_gc.RETIRED_NS + "lane/enacted"
        self.assertEqual(self.sha(self.root, keep), tip)
        self.assertIn("git branch lane/enacted " + tip, blob)


class KeepsWhatItCannotProveTest(LandedEquivalentBase):

    def test_a_commit_whose_patch_is_not_on_the_trunk_keeps_and_is_named(self):
        path = self.room("partial")
        landed = self.commit(path, "p1.txt", "landed\n", T + 40)
        stranded = self.commit(path, "p2.txt", "never landed\n", T + 41)
        self.move_trunk("moved-40.txt", T + 42)
        self.pick(landed)
        row = self.row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("UNPROVEN", row["why"])
        self.assertIn("1 of 2 commits", row["why"])
        self.assertIn("%s: no trunk commit carries its patch"
                      % stranded[:12], row["why"])
        self.assertNotIn(landed[:12] + ":", row["why"])

    def test_a_rebased_copy_with_a_different_patch_keeps_and_names_the_copy(self):  # noqa: VACUOUS_ASSERTION — the triage verdict and the named copy are the positive controls
        path = self.room("reworked")
        mine = self.commit(path, "r.txt", "lane version\n", T + 45)
        self.move_trunk("moved-45.txt", T + 46)
        env = dict(os.environ, GIT_AUTHOR_DATE="@%d +0000" % (T + 45))
        with open(os.path.join(self.root, "r.txt"), "w") as f:
            f.write("trunk version\n")
        _sh(self.root, "git", "add", "-A")
        r = self.git(self.root, env, "commit", "-q", "-m", "work r.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = self.sha(self.root, "HEAD")
        row = self.row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("%s: its copy on the trunk %s" % (mine[:12], copy[:12]),
                      row["why"])
        self.assertIn("different patch", row["why"])

    def test_a_different_binary_change_to_the_same_path_is_not_its_copy(self):  # noqa: VACUOUS_ASSERTION — the triage verdict and the named copy are the positive controls
        """Two binary changes to one path print the same `Binary files
        differ` line without their bytes; the patch-id read carries the bytes
        (`--binary`), so the copy with other bytes is not a match."""
        path = self.room("blob")
        with open(os.path.join(path, "f.bin"), "wb") as f:
            f.write(b"\x00\x01lane bytes")
        _sh(path, "git", "add", "-A")
        env = dict(os.environ, GIT_AUTHOR_DATE="@%d +0000" % (T + 47),
                   GIT_COMMITTER_DATE="@%d +0000" % (T + 47))
        self.assertEqual(self.git(path, env, "commit", "-q", "-m", "blob"
                                  ).returncode, 0)
        mine = self.sha(path, "HEAD")
        self.move_trunk("moved-47.txt", T + 48)
        with open(os.path.join(self.root, "f.bin"), "wb") as f:
            f.write(b"\x00\x01trunk bytes")
        _sh(self.root, "git", "add", "-A")
        self.assertEqual(self.git(self.root, env, "commit", "-q", "-m", "blob"
                                  ).returncode, 0)
        copy = self.sha(self.root, "HEAD")
        row = self.row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("%s: its copy on the trunk %s" % (mine[:12], copy[:12]),
                      row["why"])

    def test_a_predecessors_train_never_speaks_for_its_rebuild(self):
        """Lane cable landed as train10. Lane cable-r2 is the rebuild made
        after it; its reworked commit is not what train10 carried."""
        old = self.room("cable")
        self.commit(old, "cable.txt", "first\n", T + 50)
        self.train("lane/cable", "train10: merge lane cable")
        path = self.room("cable-r2")
        self.commit(path, "cable.txt", "rebuilt\n", T + 50)
        row = self.row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("UNPROVEN", row["why"])

    def test_a_commit_rewritten_after_the_train_merged_its_copy_keeps(self):
        path = self.room("late")
        mine = self.commit(path, "late.txt", "amended later\n", T + 60,
                           committed=T + 90)
        rebuild = self.room("late-r2")
        self.commit(rebuild, "late.txt", "as merged\n", T + 60,
                    committed=T + 61)
        self.train("lane/late-r2", "train11: merge lane late-r2",
                   committed=T + 70)
        row = self.row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("%s: rewritten after train11" % mine[:12], row["why"])

    def test_a_merge_that_carries_a_change_of_its_own_keeps(self):  # noqa: VACUOUS_ASSERTION — the triage verdict and the named merge are the positive controls
        path = self.room("evil")
        mine = self.commit(path, "e.txt", "lane work\n", T + 80)
        self.move_trunk("moved-80.txt", T + 81)
        self.pick(mine)
        self.assertEqual(_sh(path, "git", "merge", "-q", "--no-ff",
                             "--no-commit", "main").returncode, 0)
        with open(os.path.join(path, "extra.txt"), "w") as f:
            f.write("a change only the merge carries\n")
        _sh(path, "git", "add", "-A")
        self.assertEqual(_sh(path, "git", "commit", "-q", "--no-edit"
                             ).returncode, 0)
        merge = self.sha(path, "HEAD")
        row = self.row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("%s: a merge that carries a change of its own"
                      % merge[:12], row["why"])

    def test_an_uncommitted_change_keeps_the_room_whatever_its_branch_proves(self):
        path = self.room("dirtyeq")
        self.commit(path, "d.txt", "first\n", T + 100)
        rebuild = self.room("dirtyeq-r2")
        self.commit(rebuild, "d.txt", "second\n", T + 100)
        self.train("lane/dirtyeq-r2", "train15: merge lane dirtyeq-r2")
        with open(os.path.join(path, "wip.txt"), "w") as f:
            f.write("precious uncommitted bytes\n")
        self.abandon(path)
        row = self.row(path)
        self.assertEqual(row["verdict"], "rescue", row["why"])


if __name__ == "__main__":
    unittest.main()
