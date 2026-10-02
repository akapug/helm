#!/usr/bin/env python3
"""helm work gc PARKS an idle lane's checkout and keeps its branch (task/4061).

TRIAGE had no exit: a clean lane whose work had not landed kept its room and
its branch forever, so 230 rooms (13 GB) stood under helm-wt while nobody
resolved them. The branch is the work; the checkout is a cache of it. These
arms pin the one rule: an idle, clean, unleased, unlocked, unoccupied room
whose removal loses nothing the branch lacks gives up its CHECKOUT, keeps its
BRANCH at the same tip, and `helm work claim` brings the room back. Every
other room keeps both. Hermetic: scratch repositories only (tests/test_work's
WorkBase)."""
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _roomclock  # noqa: E402
from tests.test_work import WorkBase, _RoomStory, _reap, _sh  # noqa: E402

from helm import work  # noqa: E402
from helm.work import _gc as _work_gc  # noqa: E402


def _age(path, by=_roomclock.AGED_S):
    """Age the room by every clock the park rule reads: its two reflogs
    (`_roomclock.age_room`) and the admin-dir files the acts that move a
    room write (`_work_gc._ROOM_CLOCKS`)."""
    _roomclock.age_room(path, by)
    _age_admin(path, by)


def _age_admin(path, by=_roomclock.AGED_S):
    admin = _sh(path, "git", "rev-parse", "--absolute-git-dir").stdout.strip()
    then = time.time() - by
    for name in _work_gc._ROOM_CLOCKS:
        if os.path.exists(os.path.join(admin, name)):
            os.utime(os.path.join(admin, name), (then, then))
    return admin


class GcParksIdleUnlandedRoomsTest(_RoomStory, WorkBase):

    def _idle_lane(self, lane, name="f.txt"):
        """A room holding one unlanded commit, idle past the park grace."""
        path = self.room(lane)
        tip = self._commit_in(path, name)
        _age(path)
        return path, tip

    def _tip(self, lane):
        return self._git(self.root, "rev-parse", work.lane_branch(lane))

    def test_the_fixture_age_outruns_the_park_grace(self):  # noqa: VACUOUS_ASSERTION — the two orderings of constants are the observable
        self.assertGreater(_roomclock.AGED_S, _work_gc._PARK_IDLE_S)
        self.assertGreater(_work_gc._PARK_IDLE_S, _work_gc._UNSTARTED_GRACE_S)

    def test_idle_clean_unlanded_room_parks_and_its_branch_stays(self):  # noqa: VACUOUS_ASSERTION — the absent checkout is paired with the branch tip, its reflog and the re-claimed room's file
        path, tip = self._idle_lane("idle")
        row = self._row(path)
        self.assertEqual(row["verdict"], "park", row["why"])
        self.assertIn("PARK the checkout", row["why"])
        self.assertIn("helm work claim idle", row["why"])
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self._tip("idle"), tip)       # the work, untouched
        self.assertIn(tip, self._reflog(self.root, "refs/heads/lane/idle"))
        self.assertRegex(out, r"parked %s — checkout retired, branch lane/idle "
                              r"kept at %s" % (path, tip[:12]))
        self.assertRegex(out, r"parked=1 kept=\d+ triage=1")
        # ONE CLAIM BRINGS IT BACK, on the branch, with the commit in it
        rc, out, err = self.work("claim", "idle", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.split("\t")[0], path)
        self.assertEqual(self._git(path, "rev-parse", "HEAD"), tip)
        self.assertTrue(os.path.isfile(os.path.join(path, "f.txt")))

    def test_young_room_is_triage_and_keeps_its_checkout(self):
        path = self.room("young")
        self._commit_in(path, "f.txt")
        row = self._row(path)
        self.assertEqual(row["verdict"], "triage")
        self.assertIn("not parked: last moved", row["why"])
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))

    def test_a_fresh_lease_lock_is_activity_even_when_the_reflog_is_old(self):
        """The claim door re-locks the room; its lock file is the second
        clock, so an old reflog under a fresh claim is not an idle room."""
        path, _tip = self._idle_lane("relocked")
        _sh(self.root, "git", "worktree", "lock", path,
            "--reason", "lease:deadbeef")            # stale key, fresh file
        row = self._row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("inside the 3d park grace", row["why"])

    def test_a_fresh_admin_file_is_activity_even_when_the_reflog_is_old(self):
        """A commit writes COMMIT_EDITMSG beside the reflog line; with the
        reflog back-dated and that file fresh the room is not idle."""
        path = self.room("editmsg")
        self._commit_in(path, "f.txt")
        _roomclock.age_room(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("inside the 3d park grace", row["why"])

    def _expired_room(self, lane, committed_ago):
        """A room whose reflogs `git gc` expired to nothing, its admin
        files old, its one commit's committer time `committed_ago` back."""
        path = self.room(lane)
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("old work\n")
        when = "@%d +0000" % (time.time() - committed_ago)
        _sh(path, "git", "add", "f.txt")
        r = subprocess.run(["git", "commit", "-q", "-m", "old work"], cwd=path,
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, GIT_AUTHOR_DATE=when,
                                    GIT_COMMITTER_DATE=when))
        self.assertEqual(r.returncode, 0, r.stderr)
        _roomclock.age_room(path)
        _age_admin(path)
        for log in _roomclock.reflogs(path):          # room HEAD + branch
            open(log, "w").close()
        self.assertEqual(_sh(path, "git", "reflog", "show", "HEAD")
                         .stdout.strip(), "", "fixture: the reflog is empty")
        return path

    def test_an_expired_HEAD_reflog_is_dated_by_the_admin_files_and_tip(self):
        path = self._expired_room("expired", _roomclock.AGED_S)
        row = self._row(path)
        self.assertEqual(row["verdict"], "park", row["why"])
        young = self._expired_room("expiredyoung", 3600)
        row = self._row(young)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("inside the 3d park grace", row["why"])

    def test_an_unreadable_HEAD_reflog_keeps_the_checkout(self):
        path = self._expired_room("blindhead", _roomclock.AGED_S)
        log = os.path.join(_age_admin(path), "logs", "HEAD")
        os.remove(log)
        os.mkdir(log)
        row = self._row(path)
        self.assertNotEqual(row["verdict"], "park", row["why"])
        self.assertTrue(os.path.isdir(path))

    def test_dirty_room_rescues_and_never_parks(self):
        path, _tip = self._idle_lane("dirtyln")
        with open(os.path.join(path, "loose.txt"), "w") as f:
            f.write("precious\n")
        self.abandon(path)
        self.assertEqual(self._row(path)["verdict"], "rescue")

    def test_leased_room_keeps(self):
        rc, out, err = self.work("claim", "leased", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path = out.split("\t")[0]
        self._commit_in(path, "f.txt")
        _age(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("lease live", row["why"])

    def test_locked_out_of_band_room_keeps(self):
        path, _tip = self._idle_lane("lockedln")
        _sh(self.root, "git", "worktree", "lock", path,
            "--reason", "owner says keep")
        self.assertEqual(self._row(path)["verdict"], "keep")

    def test_occupied_room_keeps(self):  # noqa: VACUOUS_ASSERTION — the verdict assertions run unconditionally inside try; finally only reaps the occupant
        path, _tip = self._idle_lane("busy")
        proc = subprocess.Popen(
            ["sleep", "30"], cwd=path,
            preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL))
        try:
            row = self._row(path)
            self.assertEqual(row["verdict"], "keep")
            self.assertIn("OCCUPIED", row["why"])
        finally:
            proc.kill()
            _reap(proc)

    def test_ignored_bytes_the_branch_lacks_keep_the_checkout(self):
        path, _tip = self._idle_lane("secrets")
        with open(os.path.join(self.root, ".git", "info", "exclude"), "a") as f:
            f.write(".env\n__pycache__/\n")
        with open(os.path.join(path, ".env"), "w") as f:
            f.write("TOKEN=only-here\n")
        row = self._row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn("ignored path the branch does not (.env)", row["why"])
        # A CACHE IS NOT A KEEPSAKE: with only __pycache__ ignored, it parks
        os.remove(os.path.join(path, ".env"))
        os.makedirs(os.path.join(path, "pkg", "__pycache__"))
        with open(os.path.join(path, "pkg", "__pycache__", "m.pyc"), "wb") as f:
            f.write(b"\0")
        self.assertEqual(self._row(path)["verdict"], "park")

    def test_a_commit_only_the_rooms_head_reflog_holds_keeps_the_checkout(self):
        path = self.room("detour")
        self._commit_in(path, "f.txt")
        lost = self._detour(path, "lane/detour", "g.txt")
        _age(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn(lost[:12], row["why"])

    def test_work_only_the_branch_reflog_holds_keeps_the_checkout(self):  # noqa: VACUOUS_ASSERTION — the kept room is paired with the dropped sha named in the row and the SKIPPED enact line
        """The lane committed twice, then reset one away: the tip is
        unlanded AND a commit lives only in the reflogs. The branch's reflog
        would survive a park, but the room is where a builder sees the
        dropped work, so it stays."""
        path, _tip = self._idle_lane("resetln")
        dropped = self._commit_in(path, "g.txt")
        r = _sh(path, "git", "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(r.returncode, 0, r.stderr)
        _age(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "triage", row["why"])
        self.assertIn(dropped[:12], row["why"])
        self.assertEqual(work.gc_enact(self.root, dict(row, verdict="park"))
                         [0][:7], "SKIPPED")
        self.assertTrue(os.path.isdir(path))

    def test_an_unknown_landedness_never_parks(self):
        kept, parked = _work_gc._park_state(self.root, self.root, "main",
                                            state=_work_gc.vcs.UNKNOWN)
        self.assertIsNone(parked)
        self.assertIn("not a clean NOT landed", kept)

    def test_enact_re_asks_and_keeps_a_room_that_changed_after_the_scan(self):
        path, _tip = self._idle_lane("moving")
        row = self._row(path)
        self.assertEqual(row["verdict"], "park")
        with open(os.path.join(path, "new.txt"), "w") as f:
            f.write("written after the scan\n")
        out = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("became DIRTY after scan", out[0])
        os.remove(os.path.join(path, "new.txt"))
        self._commit_in(path, "h.txt")                # the room moved: young
        out = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("inside the 3d park grace", out[0])

    def test_harness_room_parks_with_a_git_reopen_line(self):  # noqa: VACUOUS_ASSERTION — the absent checkout is paired with the kept branch and the reopen line in the row
        rel = os.path.join(".claude", "worktrees", "agent-x")
        path = os.path.join(self.root, rel)
        r = subprocess.run(["git", "worktree", "add", "-q", "-b",
                            "worktree-agent-x", path, "main"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self._commit_in(path, "f.txt")
        _age(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "park", row["why"])
        self.assertIn("worktree add %s worktree-agent-x" % path, row["why"])
        self.assertTrue(row.get("harness_minted"), row)
        out = work.gc_enact(self.root, row)
        self.assertFalse(os.path.exists(path), out)
        self.assertIn("worktree add %s worktree-agent-x" % path, out[-1])
        self.assertTrue(work._has_branch(self.root, "worktree-agent-x"))

    def test_a_room_on_another_branch_parks_with_a_git_reopen_line(self):  # noqa: VACUOUS_ASSERTION — the absent checkout is paired with the kept branch and the reopen line in both the row and the enact line
        """A lane room moved onto a branch that is not lane_branch(lane):
        `helm work claim <lane>` would not re-open THAT branch, so the row and
        the enact line both name `git worktree add <path> <branch>`."""
        path = self.room("renamed")
        r = subprocess.run(["git", "-C", path, "checkout", "-q", "-b",
                            "lane/renamed-r2"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = self._commit_in(path, "f.txt")
        _age(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "park", row["why"])
        reopen = "worktree add %s lane/renamed-r2" % path
        self.assertIn(reopen, row["why"])
        self.assertNotIn("helm work claim renamed", row["why"])
        out = work.gc_enact(self.root, row)
        self.assertFalse(os.path.exists(path), out)
        self.assertIn(reopen, out[-1])
        self.assertIn(tip[:12], out[-1])
        self.assertTrue(work._has_branch(self.root, "lane/renamed-r2"))


class GcLandedMemoTest(_RoomStory, WorkBase):
    """The sweep remembers KEEP verdicts across runs, and only those
    (task/4061): a lane whose tip and trunk did not change costs no landedness
    read; a trunk that only gained unrelated commits carries the verdict; any
    commit with the lane's author line, a moved tip, or an old entry asks git
    again; a retiring verdict is never remembered."""

    def _asked(self, lane):
        """How many landedness reads one scan spent on `lane`."""
        with mock.patch.object(_work_gc, "_merge_state",
                               wraps=_work_gc._merge_state) as asked:
            row = next(r for r in work.gc_scan(self.root)
                       if r["lane"] == lane)
        return row, sum(1 for c in asked.call_args_list
                        if c.args[1] == work.lane_branch(lane))

    def _trunk_commit(self, name, when):
        with open(os.path.join(self.root, name), "w") as f:
            f.write("trunk %s\n" % name)
        self._git(self.root, "add", name)
        r = subprocess.run(["git", "commit", "-q", "-m", "trunk " + name],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30, env=dict(os.environ,
                                                GIT_AUTHOR_DATE=when))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_unchanged_unlanded_lane_is_not_asked_again(self):
        path = self.room("memo")
        self._commit_in(path, "f.txt")
        # ONE CLOCK FOR BOTH SCANS. The `why` line prints the tip's age
        # against the wall clock, so two scans that straddle a second boundary
        # print "age 0s" and "age 1s" for the same verdict -- a loaded gate
        # host did exactly that. The equality below is about the remembered
        # verdict, so the clock it is read against is held still.
        frozen = mock.patch.object(_work_gc.time, "time",
                                   return_value=time.time())
        frozen.start()
        self.addCleanup(frozen.stop)
        row, asked = self._asked("memo")
        self.assertEqual(row["verdict"], "triage")
        self.assertGreater(asked, 0)
        again, asked = self._asked("memo")
        self.assertEqual(asked, 0)
        self.assertEqual(again["why"], row["why"])

    def test_a_trunk_that_gained_only_unrelated_work_carries_the_verdict(self):  # noqa: VACUOUS_ASSERTION — the verdict and the zero read count are asserted together as one tuple
        path = self.room("carry")
        self._commit_in(path, "f.txt")
        self._asked("carry")
        self._trunk_commit("other.txt", "2001-01-01T00:00:00Z")
        row, asked = self._asked("carry")
        self.assertEqual((row["verdict"], asked), ("triage", 0))

    def test_the_lane_landing_on_the_trunk_is_asked_and_retires(self):
        path = self.room("lands")
        tip = self._commit_in(path, "f.txt")
        self._asked("lands")
        self._git(self.root, "cherry-pick", tip)     # same author line
        row, asked = self._asked("lands")
        self.assertGreater(asked, 0)
        self.assertEqual(row["verdict"], "remove", row["why"])

    def test_a_moved_tip_or_an_old_entry_is_asked_again(self):
        path = self.room("moved")
        self._commit_in(path, "f.txt")
        self._asked("moved")
        self._commit_in(path, "g.txt")
        self.assertGreater(self._asked("moved")[1], 0)
        later = time.time() + _work_gc._LANDED_CARRY_S + 60
        with mock.patch.object(_work_gc.time, "time", return_value=later):
            self.assertGreater(self._asked("moved")[1], 0)

    def test_a_retiring_verdict_is_never_remembered(self):  # noqa: VACUOUS_ASSERTION — the empty memo is paired with the remove verdict of the same scan
        path = self.room("landed")
        self._commit_in(path, "f.txt")
        self._land("landed")
        memo = _work_gc.LandedMemo(self.root)
        row = next(r for r in work.gc_scan(self.root, memo=memo)
                   if r["lane"] == "landed")
        self.assertEqual(row["verdict"], "remove")
        self.assertEqual(memo.entries, {})

    def _remembered(self, lane):
        """A memo holding `lane`'s NOT-landed keep, and that entry's key."""
        path = self.room(lane)
        self._commit_in(path, "f.txt")
        memo = _work_gc.LandedMemo(self.root)
        _work_gc._sweep_state(self.root, path, work.lane_branch(lane),
                              memo=memo)
        key = next(k for k in memo.entries if k.endswith(" " + lane))
        self.assertIsNotNone(memo.get(work.lane_branch(lane), path))
        return memo, key, path

    def test_a_remembered_retiring_state_is_asked_again(self):  # noqa: VACUOUS_ASSERTION — _remembered asserts the same get is a hit before the entry changes
        """The memo can only keep: an entry whose state is not NOT_ANCESTOR
        is a miss, never a hit that could read as landed."""
        memo, key, path = self._remembered("tamper")
        memo.entries[key]["state"] = _work_gc.vcs.ANCESTOR
        misses = memo.misses
        self.assertIsNone(memo.get(work.lane_branch("tamper"), path))
        self.assertEqual(memo.misses, misses + 1)

    def test_a_malformed_memo_file_is_asked_again_never_an_error(self):  # noqa: VACUOUS_ASSERTION — _remembered asserts the same get is a hit before the file changes
        """A memo file is a cache: an entry or a file of the wrong shape is
        dropped and the lane asked again, never a scan that raises."""
        memo, key, path = self._remembered("shape")
        good = memo.entries[key]
        for entries in ({key: {k: v for k, v in good.items() if k != "at"}},
                        [good]):
            _work_gc.pk.write_json(memo.path, {"v": _work_gc._LANDED_MEMO_V,
                                               "entries": entries})
            again = _work_gc.LandedMemo(self.root)
            self.assertEqual(again.entries, {})
            self.assertIsNone(again.get(work.lane_branch("shape"), path))
        for bad in ("not-a-dict", dict(good, authors="x"),
                    dict(good, state=_work_gc.vcs.ANCESTOR)):
            again.entries = {key: bad}
            self.assertIsNone(again.get(work.lane_branch("shape"), path))

    def test_an_entry_dated_after_now_is_asked_again(self):  # noqa: VACUOUS_ASSERTION — _remembered asserts the same get is a hit before the date changes
        """The carry window bounds a keep from both sides: an entry stamped
        later than now (a clock that went back, a hand-edited file) or with
        no finite time is a miss, never a keep that outlives the window."""
        memo, key, path = self._remembered("future")
        for at in (memo.now + 10 * _work_gc._LANDED_CARRY_S, float("nan"),
                   float("inf")):
            memo.entries[key]["at"] = at
            self.assertIsNone(memo.get(work.lane_branch("future"), path), at)


if __name__ == "__main__":
    unittest.main()
