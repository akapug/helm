#!/usr/bin/env python3
"""The one join from a piece of work to its task (task/3643, slice 2) —
helm/taskkey.py.

helm never recorded which task a lane or a dispatch chain was for, so six
readers guessed, each its own way, and one of them read a lane's trailing
number as its task (`canary-seeded-red-0926` joined task/926). These arms pin
the cure: the key is STORED (the lane branch's own record, written by `helm
work claim --task`; the chain's first row, written by the first `dispatch
--new-work`), and ONE join reads it: a stored key, else a literal `task-N`,
else UNKNOWN. Every write runs in a scratch HELM_HOME and scratch repository.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, taskkey, tasks, trainblame, work  # noqa: E402
from tests.test_dispatch_chain import ChainBase, run  # noqa: E402
from tests.test_work import WorkBase  # noqa: E402

ROOT = "c3" * 16
KID = "d4" * 16


def setUpModule():
    """This module writes dispatch rows, so it plants the live-seat stand-in
    (tests._tmphome.pin_live_seats), as its row-writing siblings do."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


def _git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    got = subprocess.run(("git", "-C", repo) + args, env=env,
                         capture_output=True, text=True)
    if got.returncode != 0:
        raise AssertionError("git %s: %s" % (args, got.stderr))
    return got.stdout.strip()


class JoinRuleTest(unittest.TestCase):
    """The rule, with the evidence handed in: stored > literal > UNKNOWN."""

    def test_the_stored_key_wins_over_a_literal(self):
        first = {"id": ROOT, "chain_root": ROOT, "lane": "task-99-x",
                 "task": "task/12"}
        key = taskkey.join(row=first, current={ROOT: first}, lanes=False)
        self.assertEqual((key.task, key.via, key.why),
                         ("task/12", taskkey.STORED, None))
        self.assertEqual(key.cited, ("task/99",))
        lane = taskkey.join(lane="work-key-3643", lanes={
            "work-key-3643": frozenset({"task/3643"})})
        self.assertEqual((lane.task, lane.via), ("task/3643", taskkey.STORED))

    def test_a_literal_task_n_name_joins(self):
        self.assertEqual(taskkey.join(lane="task-3585-one-board",
                                      lanes=False).task, "task/3585")
        for lane, task in (("task-3585-one-board", "task/3585"),
                           ("lane/task-77-x", "task/77"),
                           ("refs/heads/lane/fix-task/12", "task/12")):
            with self.subTest(lane=lane):
                key = taskkey.join(lane=lane, lanes=False)
                self.assertEqual((key.task, key.via), (task, taskkey.LITERAL))

    def test_a_suffix_only_name_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the control below joins a literal through the same call
        """The number at the end of a lane is a naming habit, not a record:
        0926 may be a date."""
        self.assertEqual(taskkey.join(lane="task-926-canary",
                                      lanes={}).task, "task/926")
        for lane in ("canary-seeded-red-0926", "work-key-3643",
                     "toolless-seat-3533p2", "guard-3301-r3"):
            with self.subTest(lane=lane):
                key = taskkey.join(lane=lane, lanes={})
                self.assertIsNone(key.task)
                self.assertIsNone(key.via)
                self.assertIn("no task", key.why)

    def test_a_contradictory_mapping_is_unknown_with_the_reason(self):
        first = {"id": ROOT, "chain_root": ROOT, "lane": "work-key",
                 "task": "task/1"}
        for lanes, recorded in (({"work-key": frozenset({"task/2"})}, ()),
                                ({}, ("task/2",)),
                                ({"work-key": frozenset({"task/1",
                                                         "task/2"})}, ())):
            with self.subTest(lanes=lanes, recorded=recorded):
                key = taskkey.join(row=first, current={ROOT: first},
                                   lanes=lanes, recorded=recorded)
                self.assertIsNone(key.task)
                self.assertIn("contradictory", key.why)
                self.assertIn("task/1", key.why)
                self.assertIn("task/2", key.why)
        # agreeing records are one key, never a contradiction
        agree = taskkey.join(row=first, current={ROOT: first},
                             lanes={"work-key": frozenset({"task/1"})},
                             recorded=("task/1",))
        self.assertEqual(agree.task, "task/1")

    def test_two_literals_are_ambiguous(self):
        key = taskkey.join(lane="task-1-and-task/2", lanes=False)
        self.assertIsNone(key.task)
        self.assertEqual(key.cited, ("task/1", "task/2"))
        self.assertIn("ambiguous", key.why)

    def test_a_literal_is_verified_against_a_given_ledger(self):
        known = {"task/5": {"id": "task/5", "status": "open"}}
        self.assertEqual(taskkey.join(lane="task-5-x", lanes=False,
                                      known=known).task, "task/5")
        missing = taskkey.join(lane="task-6-x", lanes=False, known=known)
        self.assertIsNone(missing.task)
        self.assertIn("not in the task ledger", missing.why)

    def test_the_first_row_decides(self):
        """A later round with a renamed lane stays with its chain."""
        plain = {"id": ROOT, "chain_root": ROOT, "lane": "plain"}
        kid = {"id": KID, "chain_root": ROOT, "lane": "now-task-9"}
        self.assertIsNone(taskkey.join(row=kid, current={ROOT: plain,
                                                         KID: kid},
                                       lanes=False).task)
        stored = dict(plain, task="task/40")
        self.assertEqual(taskkey.join(row=kid, current={ROOT: stored,
                                                        KID: kid},
                                      lanes=False).task, "task/40")

    def test_an_unreadable_chain_is_unknown(self):
        kid = {"id": KID, "chain_root": ROOT, "lane": "task-9"}
        key = taskkey.join(row=kid, current={KID: kid}, lanes=False)
        self.assertIsNone(key.task)
        self.assertIn("UNKNOWN", key.why)

    def test_landed_is_filled_only_when_asked(self):
        lands = {"task/12": ({"land": 5},)}
        self.assertIsNone(taskkey.join(lane="task-12", lanes=False).landed)
        self.assertEqual(taskkey.join(lane="task-12", lanes=False,
                                      lands=lands).landed, ({"land": 5},))
        self.assertEqual(taskkey.join(lane="task-13", lanes=False,
                                      lands=lands).landed, ())

    def test_landed_state_names_an_open_task_with_landed_work(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the unconditional positive control on the same call
        land = ({"land": 5},)
        self.assertEqual(taskkey.landed_state({"status": "open"}, land),
                         taskkey.LANDED_UNREAD)
        self.assertIsNone(taskkey.landed_state({"status": "open"}, ()))
        self.assertIsNone(taskkey.landed_state({"status": "closed"}, land))


class LaneRecordTest(unittest.TestCase):
    """The lane's record lives on its branch's own git config key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-taskkey-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "root")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_recorded_lane_joins_through_the_repository(self):
        _git(self.repo, "branch", "lane/work-key-3643")
        self.assertEqual(taskkey.record_lane(self.repo, "work-key-3643",
                                             "task/3643"), (True, None))
        self.assertEqual(_git(self.repo, "config", "--get",
                              "branch.lane/work-key-3643.helmTask"),
                         "task/3643")
        self.assertEqual(taskkey.lane_records(self.repo),
                         ({"work-key-3643": frozenset({"task/3643"})}, None))
        for lane in ("work-key-3643", "lane/work-key-3643"):
            key = taskkey.join(lane=lane, repo=self.repo)
            self.assertEqual((key.task, key.via),
                             ("task/3643", taskkey.STORED))
        # the same answer through the repository's git dir
        gitdir = os.path.join(self.repo, ".git")
        self.assertEqual(taskkey.join(lane="work-key-3643",
                                      repo=gitdir).task, "task/3643")

    def test_recording_a_different_task_is_refused(self):
        _git(self.repo, "branch", "lane/lane-a")
        taskkey.record_lane(self.repo, "lane-a", "task/7")
        self.assertEqual(taskkey.record_lane(self.repo, "lane-a", "task/7"),
                         (False, None))
        written, why = taskkey.record_lane(self.repo, "lane-a", "task/8")
        self.assertFalse(written)
        self.assertIn("task/7", why)
        self.assertIn("task/8", why)
        self.assertEqual(taskkey.lane_records(self.repo)[0]["lane-a"],
                         frozenset({"task/7"}))

    def test_the_record_lives_and_dies_with_the_lane_branch(self):
        _git(self.repo, "branch", "lane/short")
        taskkey.record_lane(self.repo, "short", "task/7")
        self.assertIn("short", taskkey.lane_records(self.repo)[0])
        _git(self.repo, "branch", "-D", "lane/short")
        self.assertNotIn("short", taskkey.lane_records(self.repo)[0])

    def test_a_lane_recording_two_tasks_joins_nothing(self):
        _git(self.repo, "branch", "lane/x")
        _git(self.repo, "config", "--add", "branch.lane/x.helmTask", "task/1")
        _git(self.repo, "config", "--add", "branch.lane/x.helmTask", "task/2")
        key = taskkey.join(lane="x", repo=self.repo)
        self.assertIsNone(key.task)
        self.assertIn("contradictory", key.why)

    def test_a_repository_this_host_cannot_see_holds_no_record(self):
        self.assertEqual(taskkey.lane_records(
            os.path.join(self.tmp, "no-such-repo")), ({}, None))

    # FINDING 1: a record outlives its branch when the ref is deleted by
    # `update-ref -d` (the gc seam), and a reused lane name inherited it.
    def test_the_gc_seam_drops_the_record_with_the_lane_ref(self):
        from helm import vcs
        _git(self.repo, "branch", "lane/reused")
        self.assertEqual(taskkey.record_lane(self.repo, "reused", "task/7"),
                         (True, None))
        sha = _git(self.repo, "rev-parse", "lane/reused")
        rc, _out, err = vcs.backend(self.repo).delete_branch(
            self.repo, "lane/reused", force=True, expect=sha)
        self.assertEqual(rc, 0, err)
        got = subprocess.run(("git", "-C", self.repo, "config", "--get",
                              "branch.lane/reused.helmTask"),
                             capture_output=True, text=True)
        self.assertEqual((got.returncode, got.stdout), (1, ""))
        _git(self.repo, "branch", "lane/reused")
        self.assertIsNone(taskkey.join(lane="reused", repo=self.repo).task)
        self.assertEqual(taskkey.record_lane(self.repo, "reused", "task/8"),
                         (True, None))

    def test_a_config_section_the_delete_could_not_remove_is_reported(self):
        """R4: the ref is gone but a locked config kept its section; the
        delete says so, and the record is still never read."""
        from helm import vcs
        _git(self.repo, "branch", "lane/stuck")
        self.assertEqual(taskkey.record_lane(self.repo, "stuck", "task/7"),
                         (True, None))
        sha = _git(self.repo, "rev-parse", "lane/stuck")
        lock = os.path.join(self.repo, ".git", "config.lock")
        open(lock, "w").close()
        try:
            rc, _out, err = vcs.backend(self.repo).delete_branch(
                self.repo, "lane/stuck", force=True, expect=sha)
        finally:
            os.remove(lock)
        self.assertEqual(rc, 0, err)
        self.assertIn("branch.lane/stuck", err)
        self.assertIn("NOT removed", err)
        self.assertEqual(_git(self.repo, "config", "--get",
                              "branch.lane/stuck.helmTask"), "task/7")
        self.assertNotIn("stuck", taskkey.lane_records(self.repo)[0])

    def test_a_branch_recut_before_the_cleanup_keeps_its_record(self):
        """R4a: a claim re-cuts lane/race and records task/8 after the old
        ref's compare-and-delete and before its config cleanup; the cleanup
        must not erase the new branch's record."""
        from helm import vcs
        be = vcs.backend(self.repo)
        _git(self.repo, "branch", "lane/race")
        self.assertEqual(taskkey.record_lane(self.repo, "race", "task/7"),
                         (True, None))
        sha = _git(self.repo, "rev-parse", "lane/race")
        real = be.text

        def text(cwd, *args, **kw):
            got = real(cwd, *args, **kw)
            if args[:2] == ("update-ref", "-d") and got[0] == 0:
                _git(self.repo, "branch", "lane/race")
                _git(self.repo, "config", "branch.lane/race.helmTask",
                     "task/8")
            return got
        with mock.patch.object(be, "text", text):
            rc, _out, err = be.delete_branch(self.repo, "lane/race",
                                             force=True, expect=sha)
        self.assertEqual(rc, 0, err)
        self.assertEqual(_git(self.repo, "config", "--get",
                              "branch.lane/race.helmTask"), "task/8")
        self.assertEqual(taskkey.lane_records(self.repo)[0]["race"],
                         frozenset({"task/8"}))

    def test_a_corrupt_lane_ref_reads_unreadable_never_absent(self):  # noqa: VACUOUS_ASSERTION — the record is asserted still present with its value
        """R4b, real git: a loose ref file of junk under refs/heads/lane/
        is a lookup ERROR (`show-ref --exists` rc 1), not an absent branch
        (rc 2), so its record is never erased."""
        _git(self.repo, "branch", "lane/broken")
        self.assertEqual(taskkey.record_lane(self.repo, "broken", "task/7"),
                         (True, None))
        with open(os.path.join(self.repo, ".git", "refs", "heads", "lane",
                               "broken"), "w") as fh:
            fh.write("junk\n")
        common = os.path.join(self.repo, ".git")
        state, why = taskkey.lane_branch_state(common, "broken")
        self.assertIsNone(state)
        self.assertIn("could not be read", why)
        forgot, why = taskkey.forget_lane(self.repo, "broken")
        self.assertFalse(forgot)
        self.assertIn("could not be read", why)
        self.assertEqual(_git(self.repo, "config", "--get",
                              "branch.lane/broken.helmTask"), "task/7")
        self.assertEqual(taskkey.lane_branch_state(common, "never-cut"),
                         (taskkey.ABSENT, None))

    def test_a_record_whose_branch_is_gone_is_ignored(self):  # noqa: VACUOUS_ASSERTION — the live branch's record beside it is asserted read
        _git(self.repo, "branch", "lane/live")
        _git(self.repo, "config", "branch.lane/live.helmTask", "task/4")
        _git(self.repo, "config", "branch.lane/orphan.helmTask", "task/5")
        records, why = taskkey.lane_records(self.repo)
        self.assertIsNone(why, why)
        self.assertEqual(records, {"live": frozenset({"task/4"})})
        self.assertIsNone(taskkey.join(lane="orphan", repo=self.repo).task)
        written, why = taskkey.record_lane(self.repo, "no-branch", "task/6")
        self.assertFalse(written)
        self.assertIn("lane/no-branch", why)


class ClaimTaskTest(WorkBase):
    """`helm work claim <lane> --task task/N` records the key, refusing an
    unknown or closed task by name before any room or lease exists."""

    def file(self, title="the thing"):
        row, err = tasks.add(title, "s1", force_new=True)
        self.assertIsNone(err, err)
        return row["id"]

    def test_claim_task_records_the_lane_key(self):  # noqa: VACUOUS_ASSERTION — the record and the join are asserted equal to the filed task id
        tid = self.file()
        rc, out, err = self.work("claim", "demo", "--seat", "s1",
                                 "--task", tid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(taskkey.lane_records(self.root)[0]["demo"],
                         frozenset({tid}))
        self.assertEqual(taskkey.join(lane="demo", repo=self.root).task, tid)
        self.assertIn(tid, out + err)

    def test_claim_task_refuses_an_unknown_task_by_name(self):  # noqa: VACUOUS_ASSERTION — the refusal text is the positive control; the absent room and record are the point
        rc, _out, err = self.work("claim", "demo", "--seat", "s1",
                                  "--task", "task/999999")
        self.assertNotEqual(rc, 0)
        self.assertIn("task/999999", err)
        self.assertIn("not in the task ledger", err)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "proj-wt",
                                                    "demo")))
        self.assertEqual(taskkey.lane_records(self.root)[0], {})

    def test_claim_task_refuses_a_closed_task_by_name(self):  # noqa: VACUOUS_ASSERTION — the refusal text is the positive control; the absent room is the point
        tid = self.file()
        tasks.close(tid, "done by hand")
        rc, _out, err = self.work("claim", "demo", "--seat", "s1",
                                  "--task", tid)
        self.assertNotEqual(rc, 0)
        self.assertIn(tid, err)
        self.assertIn("closed", err)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "proj-wt",
                                                    "demo")))

    def test_a_fresh_lane_branch_starts_without_a_stale_record(self):  # noqa: VACUOUS_ASSERTION — the new record is asserted equal to the filed id
        """R4: a record a deleted branch left behind (a config that could
        not be cleaned) never reaches the lane name's next claim."""
        tid = self.file()
        rc = subprocess.run(("git", "-C", self.root, "config",
                             "branch.lane/demo.helmTask", "task/999998"),
                            capture_output=True).returncode
        self.assertEqual(rc, 0)
        rc, _out, err = self.work("claim", "demo", "--seat", "s1",
                                  "--task", tid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(taskkey.lane_records(self.root)[0]["demo"],
                         frozenset({tid}))

    def test_an_unreadable_lane_ref_erases_nothing_and_refuses_the_claim(self):  # noqa: VACUOUS_ASSERTION — the record is asserted still present with its value
        """R4b: a ref read that fails with anything but "absent" is
        UNREADABLE: no record is erased and the claim refuses with the
        reason, before any room is cut."""
        from helm.work import _lanes
        tid = self.file()
        self.assertEqual(subprocess.run(
            ("git", "-C", self.root, "config", "branch.lane/demo.helmTask",
             "task/999998"), capture_output=True).returncode, 0)
        real = _lanes._git

        def git(where, *args, **kw):
            if args[:1] == ("show-ref",) and \
                    args[-1] == "refs/heads/lane/demo":
                return 128, "", "fatal: the ref store could not be read"
            return real(where, *args, **kw)
        with mock.patch.object(_lanes, "_git", git):
            rc, _out, err = self.work("claim", "demo", "--seat", "s1",
                                      "--task", tid)
        self.assertNotEqual(rc, 0)
        self.assertIn("could not be read", err)
        self.assertEqual(subprocess.run(
            ("git", "-C", self.root, "config", "--get",
             "branch.lane/demo.helmTask"), capture_output=True,
            text=True).stdout.strip(), "task/999998")
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "proj-wt",
                                                    "demo")))

    def test_a_claim_in_the_late_window_keeps_its_record(self):
        """Round 5: a fresh claim (lease, re-cut, record task/8) lands after
        the retiring delete read lane/task-7-fix ABSENT and before it removed
        the section. The delete runs under the claims lock and only while the
        lane holds no live lease, so the claim waits for it: task/8
        survives, and the join never falls back to the literal task/7."""
        from helm import seats_claims, vcs
        from helm.work import _gc, _lanes
        for tid in ("7", "8"):
            self.assertIsNone(tasks.add("task " + tid, "s1", tid=tid,
                                        force_new=True)[1])
        lane, branch = "task-7-fix", "lane/task-7-fix"

        def git(*args):
            return subprocess.run(("git", "-C", self.root) + args,
                                  capture_output=True, text=True)
        self.assertEqual(git("branch", branch).returncode, 0)
        self.assertEqual(taskkey.record_lane(self.root, lane, "task/7"),
                         (True, None))
        res = _lanes.resource(self.root, lane)
        be = vcs.backend(self.root)
        real = be.text
        done, worker = [], []

        def fresh_claim():
            ok, msg, _lease = seats_claims.claim(res, "s2")
            if ok:                  # a claim re-cuts and records under its lease
                git("branch", branch)
                git("config", "branch.%s.helmTask" % branch, "task/8")
            done.append((ok, msg))

        def text(cwd, *args, **kw):
            got = real(cwd, *args, **kw)
            if not worker and args[:1] == ("show-ref",) \
                    and args[-1] == "refs/heads/" + branch and got[0] != 0:
                worker.append(threading.Thread(target=fresh_claim))
                worker[0].start()
                worker[0].join(timeout=1.0)     # the late window
            return got
        with mock.patch.object(be, "text", text):
            lines = _gc._delete_lane_branch(self.root, branch,
                                            state=vcs.PATCH_EQUIVALENT,
                                            why="patch-identical")
        self.assertTrue(worker, lines)
        worker[0].join(timeout=30)
        self.assertEqual([ok for ok, _m in done], [True], done)
        self.assertEqual(git("config", "--get",
                             "branch.%s.helmTask" % branch).stdout.strip(),
                         "task/8")
        self.assertEqual(taskkey.join(lane=lane, repo=self.root).task,
                         "task/8")

    def test_a_lane_under_a_live_lease_is_never_retired(self):  # noqa: VACUOUS_ASSERTION — the lease-free retirement beside it is asserted
        """The guarded delete keeps a branch whose lane holds a live lease,
        and retires it once none does."""
        from helm import seats_claims, vcs
        from helm.work import _gc, _lanes
        branch = "lane/held"
        subprocess.run(("git", "-C", self.root, "branch", branch),
                       check=True)
        res = _lanes.resource(self.root, "held")
        ok, _msg, lease = seats_claims.claim(res, "s2")
        self.assertTrue(ok)
        kept = _gc._delete_lane_branch(self.root, branch,
                                       state=vcs.PATCH_EQUIVALENT, why="p")
        self.assertIn("KEPT", " ".join(kept))
        self.assertEqual(subprocess.run(
            ("git", "-C", self.root, "show-ref", "--verify", "--quiet",
             "refs/heads/" + branch)).returncode, 0)
        self.assertTrue(seats_claims.release(res, "s2", lease=lease)[0])
        gone = _gc._delete_lane_branch(self.root, branch,
                                       state=vcs.PATCH_EQUIVALENT, why="p")
        self.assertIn("retired", " ".join(gone))

    def test_a_lookup_error_rc_1_erases_nothing_and_refuses_the_claim(self):  # noqa: VACUOUS_ASSERTION — the record is asserted still present with its value
        """R4b: `show-ref --exists` answers rc 1 for a lookup error (a
        corrupt ref, a refs dir it cannot read) and rc 2 for absent; rc 1
        erases nothing and the claim refuses with the reason."""
        from helm.work import _lanes
        tid = self.file()
        self.assertEqual(subprocess.run(
            ("git", "-C", self.root, "config", "branch.lane/demo.helmTask",
             "task/999998"), capture_output=True).returncode, 0)
        real = _lanes._git

        def git(where, *args, **kw):
            if args[:1] == ("show-ref",) and \
                    args[-1] == "refs/heads/lane/demo":
                return 1, "", "error: failed to look up reference"
            return real(where, *args, **kw)
        with mock.patch.object(_lanes, "_git", git):
            rc, _out, err = self.work("claim", "demo", "--seat", "s1",
                                      "--task", tid)
        self.assertNotEqual(rc, 0)
        self.assertIn("could not be read", err)
        self.assertEqual(subprocess.run(
            ("git", "-C", self.root, "config", "--get",
             "branch.lane/demo.helmTask"), capture_output=True,
            text=True).stdout.strip(), "task/999998")

    def test_claim_task_refuses_a_lane_that_records_another_task(self):  # noqa: VACUOUS_ASSERTION — the first claim's record is asserted present and unchanged
        one, two = self.file("one"), self.file("two")
        rc, out, err = self.work("claim", "demo", "--seat", "s1",
                                 "--task", one)
        self.assertEqual(rc, 0, err)
        lease = out.strip().split("\t")[2]
        rc, _out, err = self.work("claim", "demo", "--seat", "s1",
                                  "--lease", lease, "--task", two)
        self.assertNotEqual(rc, 0)
        self.assertIn(one, err)
        self.assertIn(two, err)
        self.assertEqual(taskkey.lane_records(self.root)[0]["demo"],
                         frozenset({one}))


class _RaceKit:
    """A second seat's claim, or a second actor, started inside a room's
    retirement, and the checks on what each holds afterwards (task/3643
    rounds 6 and 7). Mixed into WorkBase test classes."""

    def setUp(self):
        super().setUp()
        from helm import seats_common
        # a claim that waits out the section must not give up first
        wait = mock.patch.object(seats_common, "CLAIM_LOCK_WAIT_S", 60)
        wait.start()
        self.addCleanup(wait.stop)
        self.me = threading.get_ident()

    def race(self, fn, done, window=2.0):
        """Run `fn` on another thread, its answer appended to `done`, and
        give it `window` seconds before the caller goes on."""
        worker = threading.Thread(target=lambda: done.append(fn()))
        worker.start()
        worker.join(timeout=window)
        return worker

    def racing_claim(self, lane, done, window=2.0):
        """A second seat's `helm work claim`, started in the window and given
        `window` seconds to finish before the caller goes on."""
        return self.race(lambda: work.claim(self.root, lane, "s2"), done,
                         window)

    def task_claim(self, lane, tid):
        """(rc, line): `helm work claim <lane> --task <tid>` in the CLI's
        order: the lane's record judged first, then lease and room, then the
        record written."""
        from helm.work import _cli
        _tid, why = _cli._claim_task(self.root, lane, tid)
        if why:
            return 1, why
        rc, line = work.claim(self.root, lane, "s2")
        if rc == 0:
            _written, why = taskkey.record_lane(self.root, lane, tid)
            if why:
                return 1, "claimed but not recorded: " + why
        return rc, line

    def assert_real_room(self, done, worker):
        worker.join(timeout=60)
        self.assertEqual(len(done), 1, done)
        rc, line = done[0]
        self.assertEqual(rc, 0, line)
        self.assert_room_of(line)

    def assert_room_of(self, line):
        """The room a claim's line names is registered, on disk and locked
        with that claim's lease."""
        path, _branch, lease, _ttl = line.split("\t")
        rows = {w["path"]: w for w in work.worktrees(self.root)}
        self.assertIn(path, rows,
                      "the claim holds a lease on a room that was removed")
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(rows[path]["reason"], "lease:" + lease[:8])

    def assert_claim_outcome(self, lane, got, tid=None):
        """A claim that succeeded holds a live grant, a registered room on
        disk locked with its lease and, given `tid`, joins tid; one that was
        refused holds no grant and recorded nothing."""
        from helm import seats
        from helm.work import _lanes
        rc, line = got
        res = _lanes.resource(self.root, lane)
        grants = [c["resource"] for c in seats.claims_list()]
        if rc != 0:
            self.assertNotIn(res, grants, line)
            if tid:
                self.assertNotIn(tid, taskkey.lane_records(
                    self.root)[0].get(lane, frozenset()), line)
            return
        self.assertIn(res, grants, line)
        self.assert_room_of(line)
        if tid:
            self.assertEqual(taskkey.join(lane=lane, repo=self.root).task,
                             tid)

    def destructive_spies(self, acts, pane_error=None, shell_error=None):
        """gc's two effects before a removal, the pane close and the shell
        stop, as spies that record each call (and fail when told to)."""
        from helm.work import _gc

        def pane(path):
            acts.append("pane close " + path)
            return (["term_1"], None) if pane_error is None \
                else ([], pane_error)

        def shell(path):
            acts.append("shell stop " + path)
            return (["4242"], None) if shell_error is None \
                else ([], shell_error)
        return (mock.patch.object(_gc, "_retire_bound_panes", pane),
                mock.patch.object(_gc, "_retire_disposable_occupants", shell))

    def landed_row(self, lane):
        path = self.room(lane, aged=True)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        return path, row

    def registered(self, path):
        return path in {w["path"] for w in work.worktrees(self.root)}

    def has_branch(self, branch):
        return subprocess.run(
            ("git", "-C", self.root, "show-ref", "--verify", "--quiet",
             "refs/heads/" + branch)).returncode == 0

    def garble_ledger(self):
        from helm import seats
        os.makedirs(os.path.dirname(seats.claims_path()), exist_ok=True)
        with open(seats.claims_path(), "w") as f:
            f.write("{not json")

    def ledger_bytes(self):
        from helm import seats
        with open(seats.claims_path()) as f:
            return f.read()

    def holding(self):
        """A context: another thread holds THE claims lock until it exits."""
        from helm import seats_common
        kit = self

        class Hold:
            def __enter__(self):
                self.held, self.done = threading.Event(), threading.Event()

                def hold():
                    with seats_common._claim_flocked(create_dir=True):
                        self.held.set()
                        self.done.wait(60)
                self.worker = threading.Thread(target=hold)
                self.worker.start()
                kit.assertTrue(self.held.wait(30))
                return self

            def __exit__(self, *exc):
                self.done.set()
                self.worker.join(30)
                return False
        return Hold()

    def second_process_take(self):
        """'refused' or 'took': one take of the claims lock by another
        process, with no wait."""
        code = ("from helm import seats_common\n"
                "with seats_common._claim_flocked(wait=0) as lock:\n"
                "    print('refused' if lock.f is None else 'took')\n")
        got = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=120, cwd=os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
        return got.stdout.strip() or got.stderr.strip()

    def claims_takes(self):
        """(patch, takes): a seam on the one place a claims-lock take opens
        the lock file; `takes` gets one entry per open."""
        from helm import seats_common
        real, takes = seats_common._flocked, []

        def flocked(path, *a, **kw):
            if path == seats_common.claims_path() + ".lock":
                takes.append(path)
            return real(path, *a, **kw)
        return mock.patch.object(seats_common, "_flocked", flocked), takes


class RoomWindowTest(_RaceKit, WorkBase):
    """Round 6: a claim that takes its lease while a retiring room is still
    registered either finds that room intact or waits for the retirement to
    finish and cuts its own. gc and release never remove, or unlock, a room a
    live claim points at: the last check, the room's removal and the branch's
    retirement run as one section under the claims lock, the lock every lease
    is taken under."""

    def test_a_forked_child_of_the_holder_does_not_pass_the_lock(self):  # noqa: VACUOUS_ASSERTION — the CONTROL fork below asserts the same child take succeeds once the lock is free
        """Round 7: a child forked while its parent holds the claims lock
        opens and takes the lock itself, so it waits (here: a one-take
        refusal) while the parent holds it; it never acts on the parent's
        take."""
        from helm import seats_common
        with seats_common._claim_flocked(create_dir=True) as outer:
            self.assertIsNotNone(outer.f)
            pid = os.fork()
            if pid == 0:                # the child: exit 0 only on a refusal
                code = 2
                try:
                    with seats_common._claim_flocked(wait=0) as lock:
                        code = 0 if lock.f is None else 1
                finally:
                    os._exit(code)
            _pid, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0,
                         "a forked child passed the claims lock its parent "
                         "holds without taking it")
        pid = os.fork()                 # CONTROL: with the lock free it takes
        if pid == 0:
            code = 2
            try:
                with seats_common._claim_flocked(wait=0) as lock:
                    code = 0 if lock.f is not None else 1
            finally:
                os._exit(code)
        _pid, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    def test_control_gc_closes_panes_and_stops_shells_inside_its_section(self):
        """CONTROL for the two arms below: with the lock free and the ledger
        readable, the same spies record the pane close and the shell stop,
        and the room is removed."""
        path, row = self.landed_row("spied")
        acts = []
        pane, shell = self.destructive_spies(acts)
        with pane, shell:
            lines = work.gc_enact(self.root, row)
        self.assertEqual(acts, ["pane close " + path, "shell stop " + path],
                         lines)
        self.assertIn("removed " + path, lines)

    def test_gc_does_nothing_destructive_when_its_section_cannot_take_the_lock(self):  # noqa: VACUOUS_ASSERTION — the control above records both acts through the same spies, and the room is asserted on disk with a SKIPPED line
        """Round 7: another holder keeps the claims lock past gc's wait. gc
        SKIPS the room, and no pane was closed and no shell stopped first."""
        from helm import seats_common
        path, row = self.landed_row("timeout")
        acts, held, done = [], threading.Event(), threading.Event()

        def hold():
            with seats_common._claim_flocked(create_dir=True):
                held.set()
                done.wait(60)
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(held.wait(30))
        pane, shell = self.destructive_spies(acts)
        try:
            with pane, shell, mock.patch.object(
                    seats_common, "CLAIM_LOCK_WAIT_S", 0.2):
                lines = work.gc_enact(self.root, row)
        finally:
            done.set()
            holder.join(30)
        self.assertEqual(acts, [], lines)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(any(ln.startswith("SKIPPED %s" % path)
                            for ln in lines), lines)

    def test_gc_does_nothing_destructive_on_an_unreadable_claims_ledger(self):  # noqa: VACUOUS_ASSERTION — the control above records both acts through the same spies, and the room is asserted on disk with a SKIPPED line naming the unreadable ledger
        """Round 7: the claims ledger cannot be read when gc's section
        starts. gc SKIPS the room, and no pane was closed and no shell
        stopped first."""
        from helm import seats
        path, row = self.landed_row("garbled")
        os.makedirs(os.path.dirname(seats.claims_path()), exist_ok=True)
        with open(seats.claims_path(), "w") as f:
            f.write("{not json")
        acts = []
        pane, shell = self.destructive_spies(acts)
        with pane, shell:
            lines = work.gc_enact(self.root, row)
        self.assertEqual(acts, [], lines)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(any(ln.startswith("SKIPPED %s" % path)
                            and "unreadable" in ln for ln in lines), lines)

    def test_a_claim_in_gc_s_window_points_at_a_real_room(self):
        """gc's last check passed; a claim leases the lane and finds the room
        still registered before gc removes it."""
        from helm.work import _gc
        path = self.room("racer", aged=True)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        real, done, worker = _gc._removal_blocker, [], []

        def blocker(*a, **kw):
            got = real(*a, **kw)
            if not worker and got is None and not kw.get("panes_ok"):
                worker.append(self.racing_claim("racer", done))
            return got
        with mock.patch.object(_gc, "_removal_blocker", blocker):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(worker, lines)
        self.assert_real_room(done, worker[0])

    def test_control_a_claim_before_gc_s_section_keeps_the_room(self):  # noqa: VACUOUS_ASSERTION — the claim's room is asserted registered and locked with its lease, and gc's SKIPPED line is asserted to name the holder
        """A claim that leases before gc's section finds the room intact,
        and gc keeps it, naming the holder."""
        from helm.work import _gc
        path = self.room("early", aged=True)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        real, done, worker = _gc._removal_blocker, [], []

        def blocker(*a, **kw):
            got = real(*a, **kw)
            if not worker and got is None and kw.get("panes_ok"):
                worker.append(self.racing_claim("early", done, window=60))
            return got
        with mock.patch.object(_gc, "_removal_blocker", blocker):
            lines = work.gc_enact(self.root, row)
        self.assert_real_room(done, worker[0])
        self.assertEqual(done[0][1].split("\t")[0], path)
        self.assertTrue(any(ln.startswith("SKIPPED %s" % path) and "s2" in ln
                            for ln in lines), lines)

    def test_a_claim_in_release_s_window_points_at_a_real_room(self):
        """release gave the lease back; a claim leases the lane and finds the
        landed room still registered before release removes it."""
        from helm import seats
        from helm.work import _claims
        rc, line = work.claim(self.root, "relracer", "s1")
        self.assertEqual(rc, 0, line)
        lease = line.split("\t")[2]
        real, done, worker = seats.release, [], []

        def release(*a, **kw):
            got = real(*a, **kw)
            if not worker and got[0] and threading.get_ident() == self.me:
                worker.append(self.racing_claim("relracer", done))
            return got
        with mock.patch.object(seats, "release", release):
            rc, lines = _claims.release_lane(self.root, "relracer", "s1",
                                             lease=lease)
        self.assertEqual(rc, 0, lines)
        self.assertTrue(worker, lines)
        self.assert_real_room(done, worker[0])

    def test_a_claim_in_a_stale_release_s_window_keeps_its_room_locked(self):
        """A stale release gave a dead holder's lease back; a claim leases the
        lane and locks the room before the release unlocks it."""
        from helm import seats
        from helm.work import _claims, _lanes
        rc, line = work.claim(self.root, "stale", "s1")
        self.assertEqual(rc, 0, line)
        lease, res = line.split("\t")[2], _lanes.resource(self.root, "stale")
        done, worker = [], []

        def release_stale(resource, seat, session=None, held=None):
            # the liveness proof is not under test: s1 stands in as dead
            got = seats.release(resource, "s1", lease=lease,
                                **({"held": held} if held else {}))
            if got[0] and not worker:
                worker.append(self.racing_claim("stale", done))
            return got
        with mock.patch.object(seats, "release_stale", release_stale):
            rc, lines = _claims.release_stale_lane(self.root, "stale", "s3")
        self.assertEqual(rc, 0, lines)
        self.assertTrue(worker, lines)
        self.assert_real_room(done, worker[0])
        self.assertIn(res, [c["resource"] for c in seats.claims_list()])


class RetireMatrixTest(_RaceKit, WorkBase):
    """Round 7: the falsifier matrix for a room's retirement (task/3643).
    Each test's letter is its row: (A) a claim at each gc step, (B) a claim
    at each release step, (C) two retirements of one room, (D) a section
    that cannot start, (E) the lock itself under fork, nesting, another
    process, an exception and a death, (F) a failure injected at each step,
    (G) a lane name reused."""

    GC_STEPS = ("lock", "final blocker", "pane and shell", "room remove",
                "ref delete")
    RELEASE_STEPS = ("lease release", "unlock", "room remove", "retirement")

    def file_task(self, title):
        row, err = tasks.add(title, "s1", force_new=True)
        self.assertIsNone(err, err)
        return row["id"]

    def gc_with(self, step, lane, fn, done, old_task=None):
        """(path, lines): gc_enact on a landed room, with `fn` started on
        another thread at `step` and its answer appended to `done`."""
        from helm import vcs
        from helm.work import _gc
        path, row = self.landed_row(lane)
        if old_task:
            self.assertEqual(taskkey.record_lane(self.root, lane, old_task),
                             (True, None))
        be, fired = vcs.backend(self.root), []
        real = {"unleased": _gc._unleased, "blocker": _gc._removal_blocker,
                "panes": _gc._retire_bound_panes,
                "remove": be.remove_worktree, "delete": be.delete_branch}

        def fire(at, window=1.0):
            if at == step and not fired:
                fired.append(self.race(fn, done, window))

        def unleased(*a, **kw):
            fire("lock", window=60)
            return real["unleased"](*a, **kw)

        def blocker(*a, **kw):
            got = real["blocker"](*a, **kw)
            if got is None and not kw.get("panes_ok"):
                fire("final blocker")
            return got

        def panes(where):
            fire("pane and shell")
            return real["panes"](where)

        def remove(*a, **kw):
            fire("room remove")
            return real["remove"](*a, **kw)

        def delete(*a, **kw):
            fire("ref delete")
            return real["delete"](*a, **kw)
        with mock.patch.object(_gc, "_unleased", unleased), \
                mock.patch.object(_gc, "_removal_blocker", blocker), \
                mock.patch.object(_gc, "_retire_bound_panes", panes), \
                mock.patch.object(be, "remove_worktree", remove), \
                mock.patch.object(be, "delete_branch", delete):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(fired, (step, lines))
        fired[0].join(timeout=60)
        self.assertEqual(len(done), 1, (step, lines))
        return path, lines

    def release_with(self, step, lane, fn, done, stale=False):
        """(rc, lines): s1 claims `lane` and gives it back (normal, or stale
        with s1 standing in as dead), with `fn` started on another thread at
        `step` and its answer appended to `done`."""
        from helm import seats, vcs
        from helm.work import _claims
        rc, line = work.claim(self.root, lane, "s1")
        self.assertEqual(rc, 0, line)
        lease = line.split("\t")[2]
        be, fired = vcs.backend(self.root), []
        real = {"release": seats.release, "unlock": be.unlock_worktree,
                "remove": be.remove_worktree, "delete": be.delete_branch}

        def fire(at):
            if at == step and not fired and threading.get_ident() == self.me:
                fired.append(self.race(fn, done, 1.0))

        def release(*a, **kw):
            got = real["release"](*a, **kw)
            if got[0]:
                fire("lease release")
            return got

        def release_stale(resource, seat, session=None, held=None):
            got = real["release"](resource, "s1", lease=lease,
                                  **({"held": held} if held else {}))
            if got[0]:
                fire("lease release")
            return got

        def unlock(*a, **kw):
            fire("unlock")
            return real["unlock"](*a, **kw)

        def remove(*a, **kw):
            fire("room remove")
            return real["remove"](*a, **kw)

        def delete(*a, **kw):
            fire("retirement")
            return real["delete"](*a, **kw)
        with mock.patch.object(seats, "release", release), \
                mock.patch.object(seats, "release_stale", release_stale), \
                mock.patch.object(be, "unlock_worktree", unlock), \
                mock.patch.object(be, "remove_worktree", remove), \
                mock.patch.object(be, "delete_branch", delete):
            rc, lines = (_claims.release_stale_lane(self.root, lane, "s3")
                         if stale else _claims.release_lane(
                             self.root, lane, "s1", lease=lease))
        self.assertEqual(rc, 0, lines)
        self.assertTrue(fired, (step, lines))
        fired[0].join(timeout=60)
        return rc, lines

    def assert_said_is_done(self, lines, path, branch):
        """No false success: a room said removed is gone and a branch said
        retired is gone; one not said so is still there."""
        text = "\n".join(lines)
        said = ("removed " + path) in text or ("room %s removed" % path) in text
        self.assertEqual(said, not self.registered(path), text)
        said = any(w + branch in text
                   for w in ("deleted branch ", "retired branch "))
        self.assertEqual(said, not self.has_branch(branch), text)

    # (A) ---------------------------------------------------------------
    def test_A_a_claim_at_each_gc_step_holds_a_real_room_or_nothing(self):  # noqa: VACUOUS_ASSERTION — a successful claim is asserted to hold its grant and a registered room locked with its lease
        for step in self.GC_STEPS:
            with self.subTest(step=step):
                lane, done = "a-" + step.replace(" ", "-"), []
                self.gc_with(step, lane,
                             lambda lane=lane: work.claim(self.root, lane,
                                                          "s2"), done)
                self.assert_claim_outcome(lane, done[0])

    def test_A_a_task_claim_at_each_gc_step_joins_its_own_task(self):  # noqa: VACUOUS_ASSERTION — each claim is asserted to succeed, hold a locked room and join its task
        new = self.file_task("new")
        for step in self.GC_STEPS:
            with self.subTest(step=step):
                lane, done = "n-" + step.replace(" ", "-"), []
                self.gc_with(step, lane,
                             lambda lane=lane: self.task_claim(lane, new),
                             done)
                self.assertEqual(done[0][0], 0, done)
                self.assert_claim_outcome(lane, done[0], tid=new)

    def test_A_a_task_claim_over_another_task_s_lane_holds_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal's empty grant is the point; the old task is asserted gone from the join
        old, new = self.file_task("old"), self.file_task("new")
        for step in self.GC_STEPS:
            with self.subTest(step=step):
                lane, done = "o-" + step.replace(" ", "-"), []
                self.gc_with(step, lane,
                             lambda lane=lane: self.task_claim(lane, new),
                             done, old_task=old)
                self.assert_claim_outcome(lane, done[0], tid=new)
                self.assertNotEqual(
                    taskkey.join(lane=lane, repo=self.root).task, old)

    # (B) ---------------------------------------------------------------
    def test_B_a_claim_at_each_release_step_keeps_a_real_room(self):  # noqa: VACUOUS_ASSERTION — each claim is asserted to succeed and hold a registered room locked with its lease
        for step in self.RELEASE_STEPS:
            with self.subTest(step=step):
                lane, done = "b-" + step.replace(" ", "-"), []
                self.release_with(step, lane,
                                  lambda lane=lane: work.claim(
                                      self.root, lane, "s2"), done)
                self.assert_claim_outcome(lane, done[0])
                self.assertEqual(done[0][0], 0, done)

    def test_B_a_claim_at_each_stale_release_step_keeps_a_real_room(self):  # noqa: VACUOUS_ASSERTION — each claim is asserted to succeed and hold a registered room locked with its lease
        for step in ("lease release", "unlock"):
            with self.subTest(step=step):
                lane, done = "s-" + step.replace(" ", "-"), []
                self.release_with(step, lane,
                                  lambda lane=lane: work.claim(
                                      self.root, lane, "s2"), done,
                                  stale=True)
                self.assert_claim_outcome(lane, done[0])
                self.assertEqual(done[0][0], 0, done)

    # (C) ---------------------------------------------------------------
    def test_C_gc_against_gc_retires_the_room_once(self):
        path, row = self.landed_row("twice")
        done = []
        from helm.work import _gc
        real, fired = _gc._removal_blocker, []

        def blocker(*a, **kw):
            got = real(*a, **kw)
            if got is None and not kw.get("panes_ok") and not fired:
                fired.append(self.race(
                    lambda: work.gc_enact(self.root, row), done, 1.0))
            return got
        with mock.patch.object(_gc, "_removal_blocker", blocker):
            first = work.gc_enact(self.root, row)
        fired[0].join(timeout=60)
        both = list(first) + list(done[0])
        self.assertEqual(both.count("removed " + path), 1, both)
        self.assertEqual(sum(ln.startswith(("deleted branch lane/twice",
                                            "retired branch lane/twice"))
                             for ln in both), 1, both)
        self.assert_said_is_done(both, path, "lane/twice")

    def test_C_gc_against_release_retires_the_room_once(self):
        path = work.lane_path(self.root, "both")
        row = {"lane": "both", "path": path, "branch": "lane/both",
               "verdict": "remove"}
        done = []
        rc, lines = self.release_with(
            "lease release", "both",
            lambda: work.gc_enact(self.root, row), done)
        both = list(lines) + list(done[0])
        said = [ln for ln in both if ("removed " + path) in ln
                or ("room %s removed" % path) in ln]
        self.assertEqual(len(said), 1, both)
        self.assert_said_is_done(both, path, "lane/both")
        self.assertFalse(self.registered(path))

    def test_C_a_stale_gc_row_never_retires_the_next_generation(self):
        path, row = self.landed_row("gen")
        first = work.gc_enact(self.root, row)
        self.assertIn("removed " + path, first)
        got = work.claim(self.root, "gen", "s2")
        again = work.gc_enact(self.root, row)
        self.assertTrue(any(ln.startswith("SKIPPED %s" % path)
                            for ln in again), again)
        self.assert_claim_outcome("gen", got)
        self.assertTrue(self.has_branch("lane/gen"))

    # (D) ---------------------------------------------------------------
    def claimed(self, lane):
        rc, line = work.claim(self.root, lane, "s1")
        self.assertEqual(rc, 0, line)
        return line

    def assert_untouched(self, lane, line, lines):
        """The claim `line` still holds: its grant, its locked room and its
        branch."""
        from helm import seats
        from helm.work import _lanes
        self.assertIn(_lanes.resource(self.root, lane),
                      [c["resource"] for c in seats.claims_list()], lines)
        self.assert_room_of(line)
        self.assertTrue(self.has_branch("lane/" + lane), lines)

    def test_D_release_does_nothing_when_its_section_cannot_take_the_lock(self):  # noqa: VACUOUS_ASSERTION — the grant, the locked room and the branch are asserted present
        from helm import seats_common
        from helm.work import _claims
        line = self.claimed("dlock")
        # (E) THE WAIT IS BOUNDED: the holder lets go only after this call
        # returns, so returning at all is the waiter giving up on its own.
        with self.holding(), mock.patch.object(
                seats_common, "CLAIM_LOCK_WAIT_S", 0.2):
            rc, lines = _claims.release_lane(self.root, "dlock", "s1",
                                             lease=line.split("\t")[2])
        self.assertEqual(rc, 1, lines)
        self.assertIn("unavailable", "\n".join(lines))
        self.assert_untouched("dlock", line, lines)

    def test_D_release_does_nothing_on_an_unreadable_claims_ledger(self):  # noqa: VACUOUS_ASSERTION — the ledger bytes, the locked room and the branch are asserted present
        from helm.work import _claims
        line = self.claimed("dgarble")
        self.garble_ledger()
        rc, lines = _claims.release_lane(self.root, "dgarble", "s1",
                                         lease=line.split("\t")[2])
        self.assertEqual(rc, 1, lines)
        self.assertIn("unreadable", "\n".join(lines))
        self.assertEqual(self.ledger_bytes(), "{not json")
        self.assert_room_of(line)
        self.assertTrue(self.has_branch("lane/dgarble"), lines)

    def test_D_a_stale_release_does_nothing_when_its_section_cannot_take_the_lock(self):
        from helm import seats_common
        from helm.work import _claims
        line = self.claimed("slock")
        with self.holding(), mock.patch.object(
                seats_common, "CLAIM_LOCK_WAIT_S", 0.2):
            rc, lines = _claims.release_stale_lane(self.root, "slock", "s3")
        self.assertEqual(rc, 1, lines)
        self.assertIn("unavailable", "\n".join(lines))
        self.assert_untouched("slock", line, lines)

    def test_D_a_stale_release_does_nothing_on_an_unreadable_claims_ledger(self):  # noqa: VACUOUS_ASSERTION — the ledger bytes and the locked room are asserted present
        from helm.work import _claims
        line = self.claimed("sgarble")
        self.garble_ledger()
        rc, lines = _claims.release_stale_lane(self.root, "sgarble", "s3")
        self.assertEqual(rc, 1, lines)
        self.assertIn("unreadable", "\n".join(lines))
        self.assertEqual(self.ledger_bytes(), "{not json")
        self.assert_room_of(line)

    # (E) ---------------------------------------------------------------
    def test_E_a_section_takes_the_lock_once_and_holds_it_to_its_last_step(self):  # noqa: VACUOUS_ASSERTION — exactly one take and a refused probe are asserted
        """One claims-lock take per section: the lease release and the branch
        delete inside it take none, and another process is still refused at
        the section's last step."""
        from helm import vcs
        from helm.work import _claims
        be = vcs.backend(self.root)
        real = be.delete_branch
        for actor in ("release", "gc"):
            with self.subTest(actor=actor):
                lane, probes = "e-" + actor, []

                def delete(*a, probes=probes, **kw):
                    probes.append(self.second_process_take())
                    return real(*a, **kw)
                if actor == "release":
                    lease = self.claimed(lane).split("\t")[2]
                else:
                    _path, row = self.landed_row(lane)
                patch, takes = self.claims_takes()
                with patch, mock.patch.object(be, "delete_branch", delete):
                    lines = (_claims.release_lane(self.root, lane, "s1",
                                                  lease=lease)[1]
                             if actor == "release"
                             else work.gc_enact(self.root, row))
                self.assertEqual(len(takes), 1, (takes, lines))
                self.assertEqual(probes, ["refused"], lines)
                self.assertFalse(self.has_branch("lane/" + lane), lines)

    def test_E_an_exception_mid_section_frees_the_lock_and_the_room_retires_later(self):  # noqa: VACUOUS_ASSERTION — the free lock, the kept room and its later removal are asserted for each actor
        from helm import vcs
        from helm.work import _claims
        from tests import _roomclock
        be = vcs.backend(self.root)

        def boom(*a, **kw):
            raise RuntimeError("killed mid-section")
        for actor in ("gc", "release"):
            with self.subTest(actor=actor):
                lane = "x-" + actor
                if actor == "gc":
                    path, row = self.landed_row(lane)
                else:
                    line = self.claimed(lane)
                    path = line.split("\t")[0]
                with mock.patch.object(be, "remove_worktree", boom), \
                        self.assertRaises(RuntimeError):
                    if actor == "gc":
                        work.gc_enact(self.root, row)
                    else:
                        _claims.release_lane(self.root, lane, "s1",
                                             lease=line.split("\t")[2])
                self.assertEqual(self.second_process_take(), "took")
                self.assertTrue(self.registered(path))
                self.assertTrue(self.has_branch("lane/" + lane))
                _roomclock.age_room(path)
                row = next(r for r in work.gc_scan(self.root)
                           if r["path"] == path)
                self.assertEqual(row["verdict"], "remove", row)
                lines = work.gc_enact(self.root, row)
                self.assertIn("removed " + path, lines)
                self.assert_said_is_done(lines, path, "lane/" + lane)

    def test_E_a_holder_that_dies_mid_section_frees_the_lock(self):
        code = ("import time\n"
                "from helm import seats_common\n"
                "with seats_common._claim_flocked(create_dir=True) as lock:\n"
                "    print('held' if lock.f else 'refused', flush=True)\n"
                "    time.sleep(600)\n")
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            self.assertEqual(self.second_process_take(), "refused")
        finally:
            child.kill()
            child.wait(30)
            child.stdout.close()
        self.assertEqual(self.second_process_take(), "took")

    # (F) ---------------------------------------------------------------
    def test_F_a_failed_step_in_gc_never_reads_as_success(self):  # noqa: VACUOUS_ASSERTION — every case asserts what gc said against the room and branch it left
        from helm import vcs
        be = vcs.backend(self.root)
        cases = (("pane close", {"pane_error": "pane close failed"}, None),
                 ("shell stop", {"shell_error": "shell survived"}, None),
                 ("room unlock", None, "unlock_worktree"),
                 ("room remove", None, "remove_worktree"),
                 ("ref compare-delete", None, "delete_branch"))
        for name, spy, method in cases:
            with self.subTest(step=name):
                lane = "f-" + name.replace(" ", "-")
                path, row = self.landed_row(lane)
                if method == "unlock_worktree":
                    # a stale lease tag, which only the unlock takes off
                    subprocess.run(("git", "-C", self.root, "worktree",
                                    "lock", "--reason", "lease:deadbeef",
                                    path), check=True, capture_output=True)
                    row = next(r for r in work.gc_scan(self.root)
                               if r["path"] == path)
                    self.assertEqual(row["verdict"], "remove", row)
                acts = []
                pane, shell = (self.destructive_spies(acts, **spy) if spy
                               else (contextlib.nullcontext(),
                                     contextlib.nullcontext()))
                inject = (mock.patch.object(
                    be, method, lambda *a, **kw: (1, "", "injected failure"))
                    if method else contextlib.nullcontext())
                with pane, shell, inject:
                    lines = work.gc_enact(self.root, row)
                self.assert_said_is_done(lines, path, "lane/" + lane)
                if method == "delete_branch":
                    self.assertFalse(self.registered(path), lines)
                    self.assertTrue(any(ln.startswith("KEPT branch")
                                        for ln in lines), lines)
                else:
                    self.assertTrue(self.registered(path), lines)
                    self.assertTrue(any(ln.startswith("SKIPPED %s" % path)
                                        for ln in lines), lines)

    def test_F_a_failed_step_in_release_never_reads_as_success(self):  # noqa: VACUOUS_ASSERTION — every case asserts rc, the failure text and what release said against the room and branch
        from helm import vcs
        from helm.work import _claims
        be = vcs.backend(self.root)
        for name, method, want in (("room remove", "remove_worktree", 1),
                                   ("ref compare-delete", "delete_branch",
                                    0)):
            with self.subTest(step=name):
                lane = "r-" + name.replace(" ", "-")
                line = self.claimed(lane)
                path = line.split("\t")[0]
                with mock.patch.object(
                        be, method,
                        lambda *a, **kw: (1, "", "injected failure")):
                    rc, lines = _claims.release_lane(
                        self.root, lane, "s1", lease=line.split("\t")[2])
                self.assertEqual(rc, want, lines)
                self.assert_said_is_done(lines, path, "lane/" + lane)
                self.assertTrue(self.has_branch("lane/" + lane), lines)
                self.assertIn("injected failure", "\n".join(lines))

    def test_F_a_failed_ledger_write_in_release_changes_nothing(self):
        from helm import pk, seats
        from helm.work import _claims
        line = self.claimed("wfail")
        real = pk.write_json

        def write(path, *a, **kw):
            if path == seats.claims_path():
                raise OSError("injected: the ledger write failed")
            return real(path, *a, **kw)
        with mock.patch.object(pk, "write_json", write):
            rc, lines = _claims.release_lane(self.root, "wfail", "s1",
                                             lease=line.split("\t")[2])
        self.assertEqual(rc, 1, lines)
        self.assertIn("ledger write failed", "\n".join(lines))
        self.assert_untouched("wfail", line, lines)

    # (D, round 8) ------------------------------------------------------
    MALFORMED = (
        ("not JSON", "{not json"),
        ("a malformed row", json.dumps(
            {"_fence": 1, "worktree:proj:other": {"holder": "s9"}})),
    )

    def test_D_every_claims_writer_refuses_a_malformed_ledger_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — every writer's refusal is asserted to name the unreadable ledger, and the bytes are asserted equal to what was planted
        """Round 8: no door that writes the claims ledger reads a malformed
        one as empty and writes that back. Each refuses UNKNOWN, naming the
        unreadable ledger, and the file stays byte-identical."""
        from helm import seats, seats_cli
        from helm.work import _claims
        park = self.claimed("wpark")
        with open(os.path.join(park.split("\t")[0], "wip.txt"), "w") as f:
            f.write("parked bytes\n")
        rel, stale = self.claimed("wrel"), self.claimed("wstale")
        _path, row = self.landed_row("wgc")

        def said(fn):
            try:
                return repr(fn())
            except OSError as exc:
                return str(exc)

        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = seats_cli.cmd(*args)
            return rc, out.getvalue() + err.getvalue()
        writers = (
            ("helm work claim", lambda: work.claim(self.root, "wnew", "s2")),
            ("release --park", lambda: _claims.release_lane(
                self.root, "wpark", "s1", lease=park.split("\t")[2],
                park=True)),
            ("release", lambda: _claims.release_lane(
                self.root, "wrel", "s1", lease=rel.split("\t")[2])),
            ("stale release", lambda: _claims.release_stale_lane(
                self.root, "wstale", "s3")),
            ("gc", lambda: work.gc_enact(self.root, row)),
            ("seats.claim", lambda: seats.claim("res:x", "s1")),
            ("seats.refresh_claim", lambda: seats.refresh_claim(
                "res:x", "s1", lease="0" * 16)),
            ("seats.release", lambda: seats.release(
                "res:x", "s1", lease="0" * 16)),
            ("helm chat release", lambda: cli(
                "release", ["res:x", "--seat", "s1", "--lease", "0" * 16])),
            ("rebind_claim_sessions", lambda: seats.rebind_claim_sessions(
                "s1", "sess-a", "sess-b")),
            ("rollback_claim_sessions",
             lambda: seats.rollback_claim_sessions(
                 "s1", "sess-a", "sess-b", ["res:x"])),
        )
        for shape, planted in self.MALFORMED:
            for name, fn in writers:
                with self.subTest(ledger=shape, writer=name):
                    with open(seats.claims_path(), "w") as f:
                        f.write(planted)
                    answer = said(fn)
                    self.assertEqual(self.ledger_bytes(), planted, answer)
                    self.assertIn("unreadable", answer)
            with self.subTest(ledger=shape, writer="claims_list tidy"):
                with open(seats.claims_path(), "w") as f:
                    f.write(planted)
                seats.claims_list(gc=True)
                self.assertEqual(self.ledger_bytes(), planted)
        # the parked room was never committed into
        self.assertIn("wip.txt", _git(park.split("\t")[0], "status",
                                      "--porcelain"))

    def test_D_a_park_release_on_an_unreadable_ledger_commits_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal's text, the unchanged HEAD and the still-dirty file are each asserted
        """Round 8: `release --park` refreshes the grant before its WIP
        commit. On a ledger it cannot read it refuses UNKNOWN before either:
        no commit, the dirty bytes still in the tree, the ledger unchanged."""
        from helm.work import _claims
        line = self.claimed("pk")
        path, _b, lease, _t = line.split("\t")
        head = _git(path, "rev-parse", "HEAD")
        with open(os.path.join(path, "wip.txt"), "w") as f:
            f.write("parked bytes\n")
        self.garble_ledger()
        rc, lines = _claims.release_lane(self.root, "pk", "s1", lease=lease,
                                         park=True)
        self.assertEqual(rc, 1, lines)
        text = "\n".join(lines)
        self.assertIn("unreadable", text)
        self.assertIn("UNKNOWN", text)
        self.assertEqual(self.ledger_bytes(), "{not json")
        self.assertEqual(_git(path, "rev-parse", "HEAD"), head)
        self.assertIn("wip.txt", _git(path, "status", "--porcelain"))

    # (F, round 8) ------------------------------------------------------
    def fail_unlock(self):
        from helm import vcs
        return mock.patch.object(
            vcs.backend(self.root), "unlock_worktree",
            lambda *a, **kw: (1, "", "injected unlock failure"))

    def test_F_a_failed_unlock_in_a_stale_release_is_reported(self):  # noqa: VACUOUS_ASSERTION — the failure text and the room's unchanged lease tag are asserted
        """Round 8: the stale release gave the claim back and the unlock
        failed. It says so, exits 1 and changes nothing else; it never
        reports the room unlocked."""
        from helm import seats
        from helm.work import _claims
        line = self.claimed("sfail")
        path, _b, lease, _t = line.split("\t")

        def release_stale(resource, seat, session=None, held=None):
            return seats.release(resource, "s1", lease=lease,
                                 **({"held": held} if held else {}))
        with mock.patch.object(seats, "release_stale", release_stale), \
                self.fail_unlock():
            rc, lines = _claims.release_stale_lane(self.root, "sfail", "s3")
        text = "\n".join(lines)
        self.assertEqual(rc, 1, text)
        self.assertIn("injected unlock failure", text)
        self.assertNotIn("unlocked (claim released)", text)
        rows = {w["path"]: w for w in work.worktrees(self.root)}
        self.assertEqual(rows[path]["reason"], "lease:" + lease[:8])

    def test_F_a_failed_unlock_in_release_keeps_room_and_branch(self):  # noqa: VACUOUS_ASSERTION — the failure text, the kept room and the kept branch are asserted
        """Round 8: release's unlock failed on a landed room. It says so,
        exits 1 and removes nothing; it never retires a room or branch past
        a failed step."""
        from helm.work import _claims
        line = self.claimed("rfail")
        path, _b, lease, _t = line.split("\t")
        with self.fail_unlock():
            rc, lines = _claims.release_lane(self.root, "rfail", "s1",
                                             lease=lease)
        text = "\n".join(lines)
        self.assertEqual(rc, 1, text)
        self.assertIn("injected unlock failure", text)
        self.assertTrue(self.registered(path), text)
        self.assertTrue(self.has_branch("lane/rfail"), text)
        self.assert_said_is_done(lines, path, "lane/rfail")

    # (B, round 8) ------------------------------------------------------
    def test_B_release_keeps_a_clean_landed_room_a_pane_is_bound_to(self):  # noqa: VACUOUS_ASSERTION — the kept room, the reason naming the pane and the released lease are asserted
        """Round 8: a clean landed room with no /proc occupant, but a
        metaharness pane bound to it. gc keeps such a room (its blocker
        refuses on a bound pane); release keeps it by the same check and
        says why."""
        from helm import seats
        from helm.work import _claims, _gc, _lanes
        line = self.claimed("paned")
        path, _b, lease, _t = line.split("\t")
        with mock.patch.object(_gc, "_panes_bound_to",
                               return_value=(["term_bound"], None)):
            rc, lines = _claims.release_lane(self.root, "paned", "s1",
                                             lease=lease)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.registered(path), text)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("term_bound", text)
        self.assertIn("room %s kept" % path, text)
        self.assertNotIn(_lanes.resource(self.root, "paned"),
                         [c["resource"] for c in seats.claims_list()])
        self.assert_said_is_done(lines, path, "lane/paned")

    # (B/F, round 9) ----------------------------------------------------
    def disposable_shell(self, path, alive, stops):
        """Seams that plant one disposable Orca shell (pid 424242) in `path`
        while `alive` holds it, and a stop that records itself in `stops`
        and ends it."""
        from helm.work import _claims, _gc

        def occupants(where):
            return sorted(alive) if where == path else []

        def stop(where):
            stops.append(where)
            gone = sorted(alive)
            alive.clear()
            return gone, None
        disposable = (lambda pid: pid in {"424242"})
        return (mock.patch.object(_claims, "_occupants", occupants),
                mock.patch.object(_gc, "_occupants", occupants),
                mock.patch.object(_claims, "_disposable_worktree_occupant",
                                  disposable),
                mock.patch.object(_gc, "_disposable_worktree_occupant",
                                  disposable),
                mock.patch.object(_claims, "_retire_disposable_occupants",
                                  stop))

    def test_B_release_keeps_a_pane_bound_room_s_disposable_shell_alive(self):  # noqa: VACUOUS_ASSERTION — the control below stops the same shell through the same seam and removes the room
        """Round 9: a clean landed room holds a bound pane AND a disposable
        Orca shell. Release keeps the room for the pane, by gc's order (the
        blocker before the shell stop), so the shell is never stopped."""
        from helm.work import _claims, _gc
        line = self.claimed("paneshell")
        path, _b, lease, _t = line.split("\t")
        alive, stops = {"424242"}, []
        seams = self.disposable_shell(path, alive, stops)
        with contextlib.ExitStack() as stack:
            for seam in seams:
                stack.enter_context(seam)
            stack.enter_context(mock.patch.object(
                _gc, "_panes_bound_to", return_value=(["term_bound"], None)))
            rc, lines = _claims.release_lane(self.root, "paneshell", "s1",
                                             lease=lease)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertEqual(stops, [], text)
        self.assertEqual(alive, {"424242"}, text)
        self.assertTrue(self.registered(path), text)
        self.assertIn("term_bound", text)

    def test_control_release_stops_a_disposable_shell_in_a_room_it_removes(self):  # noqa: VACUOUS_ASSERTION — the stop is asserted recorded and the removal line asserted
        """CONTROL for the arm above: the same shell with no pane bound is
        stopped, and then the room is removed."""
        from helm.work import _claims
        line = self.claimed("shellonly")
        path, _b, lease, _t = line.split("\t")
        alive, stops = {"424242"}, []
        with contextlib.ExitStack() as stack:
            for seam in self.disposable_shell(path, alive, stops):
                stack.enter_context(seam)
            rc, lines = _claims.release_lane(self.root, "shellonly", "s1",
                                             lease=lease)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertEqual(stops, [path], text)
        self.assertIn("room %s removed" % path, text)
        self.assertFalse(self.registered(path), text)

    def omitting(self, path):
        """A registry read that answers, but no longer lists `path`."""
        from helm.work import _claims, _lanes
        real = _lanes._worktree_records

        def records(root):
            rows, error = real(root)
            return [w for w in rows if w["path"] != path], error
        return mock.patch.object(_claims, "_worktree_records", records)

    def test_F_an_unlock_whose_room_the_registry_omits_is_a_failure(self):  # noqa: VACUOUS_ASSERTION — each actor's exit, failure text and untouched room are asserted
        """Round 9: the unlock failed and the registry read after it no
        longer lists the room. Neither release nor stale release reads that
        as unlocked: each reports the failure, exits 1 and changes nothing
        else."""
        from helm import seats
        from helm.work import _claims
        for actor in ("stale release", "release"):
            with self.subTest(actor=actor):
                lane = "omit-" + actor.split()[0]
                line = self.claimed(lane)
                path, _b, lease, _t = line.split("\t")

                def release_stale(resource, seat, session=None, held=None,
                                  lease=lease):
                    return seats.release(resource, "s1", lease=lease,
                                         **({"held": held} if held else {}))
                with mock.patch.object(seats, "release_stale",
                                       release_stale), \
                        self.fail_unlock(), self.omitting(path):
                    rc, lines = (
                        _claims.release_stale_lane(self.root, lane, "s3")
                        if actor == "stale release"
                        else _claims.release_lane(self.root, lane, "s1",
                                                  lease=lease))
                text = "\n".join(lines)
                self.assertEqual(rc, 1, text)
                self.assertIn("injected unlock failure", text)
                self.assertNotIn("unlocked (claim released)", text)
                rows = {w["path"]: w for w in work.worktrees(self.root)}
                self.assertEqual(rows[path]["reason"], "lease:" + lease[:8])
                self.assertTrue(self.has_branch("lane/" + lane), text)

    # (G) ---------------------------------------------------------------
    def test_G_a_reused_lane_name_at_the_same_base_sha_joins_only_its_new_task(self):  # noqa: VACUOUS_ASSERTION — the new record and the join are asserted equal to the new task
        old, new = self.file_task("old"), self.file_task("new")
        path, row = self.landed_row("reuse")
        tip = _git(self.root, "rev-parse", "lane/reuse")
        self.assertEqual(taskkey.record_lane(self.root, "reuse", old),
                         (True, None))
        self.assertIn("removed " + path, work.gc_enact(self.root, row))
        self.assertFalse(self.has_branch("lane/reuse"))
        got = self.task_claim("reuse", new)
        self.assertEqual(got[0], 0, got)
        self.assertEqual(_git(self.root, "rev-parse", "lane/reuse"), tip)
        self.assertEqual(taskkey.lane_records(self.root)[0]["reuse"],
                         frozenset({new}))
        self.assertEqual(taskkey.join(lane="reuse", repo=self.root).task, new)

    def test_G_a_parked_lane_re_claimed_keeps_its_own_task(self):  # noqa: VACUOUS_ASSERTION — the second claim is asserted to hold its room and join the lane's own task
        from helm.work import _claims
        mine, other = self.file_task("mine"), self.file_task("other")
        line = self.claimed("parked")
        path, _b, lease, _t = line.split("\t")
        self.assertEqual(taskkey.record_lane(self.root, "parked", mine),
                         (True, None))
        with open(os.path.join(path, "work.txt"), "w") as f:
            f.write("unlanded\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "unlanded work")
        rc, lines = _claims.release_lane(self.root, "parked", "s1",
                                         lease=lease)
        self.assertEqual(rc, 0, lines)
        self.assertTrue(self.has_branch("lane/parked"), lines)
        refused = self.task_claim("parked", other)
        self.assertNotEqual(refused[0], 0, refused)
        self.assert_claim_outcome("parked", refused, tid=other)
        got = work.claim(self.root, "parked", "s2")
        self.assert_claim_outcome("parked", got, tid=mine)


class DispatchTaskTest(ChainBase):
    """The first `dispatch --new-work` of a chain records its task on the
    row: --task, else the lane's record, else one literal."""

    def file(self, title="the thing"):
        row, err = tasks.add(title, "seat-b", force_new=True)
        self.assertIsNone(err, err)
        return row["id"]

    def test_new_work_with_task_records_it_on_the_first_row(self):
        tid = self.file()
        row = self.root(task=tid)
        self.assertEqual(row["task"], tid)
        self.assertEqual(dispatches.rows()[row["id"]]["task"], tid)
        self.assertEqual(trainblame.lane_task(row["id"]), (tid, None, None))

    def test_task_refuses_an_unknown_task_by_name(self):
        row, why = dispatches.add("seat-b", "lane-a", repo=self.repo,
                                  kind="review", notify=False, new_work=True,
                                  _reason=True, ref=self.a,
                                  task="task/999999")
        self.assertIsNone(row)
        self.assertIn("task/999999", why)
        self.assertEqual(dispatches.rows(), {})

    def test_task_on_a_continuation_is_refused(self):
        tid = self.file()
        parent = self.root(task=tid)
        kid, why = self.child(parent["id"], task=tid)
        self.assertIsNone(kid)
        self.assertIn("--new-work", why)

    def test_a_literal_in_the_lane_is_recorded_and_a_suffix_is_not(self):  # noqa: VACUOUS_ASSERTION — the literal lane's row carries the task, the positive control on the same field
        tid = self.file()
        num = tid.split("/")[1]
        named = self.root(lane="task-%s-x" % num)
        self.assertEqual(named.get("task"), tid)
        suffix = self.root(lane="fix-%s" % num)
        self.assertNotIn("task", suffix)
        self.assertEqual(trainblame.lane_task(suffix["id"]),
                         (None, None, None))

    def test_the_brief_names_one_task(self):  # noqa: VACUOUS_ASSERTION — the one-task brief's row carries the task, the positive control on the same field
        tid, other = self.file("one"), self.file("two")
        one, err = dispatches._base(
            "seat-b", "lane-b", self.a, None, 600, self.repo, kind="review",
            new_work=True, message_body="build %s now" % tid)
        self.assertIsNone(err, err)
        self.assertEqual(one.get("task"), tid)
        two, err = dispatches._base(
            "seat-b", "lane-b", self.a, None, 600, self.repo, kind="review",
            new_work=True, message_body="build %s after %s" % (tid, other))
        self.assertIsNone(err, err)
        self.assertNotIn("task", two)

    def test_the_lane_record_is_inherited_and_a_contrary_task_refused(self):
        tid, other = self.file("one"), self.file("two")
        self.git("branch", "lane/lane-a")
        self.assertEqual(taskkey.record_lane(self.repo, "lane-a", tid),
                         (True, None))
        row = self.root()
        self.assertEqual(row.get("task"), tid)
        bad, why = dispatches.add("seat-b", "lane-a", repo=self.repo,
                                  kind="review", notify=False, new_work=True,
                                  _reason=True, ref=self.b, task=other)
        self.assertIsNone(bad)
        self.assertIn(tid, why)
        self.assertIn(other, why)

    def test_a_lane_record_whose_task_closed_is_refused_by_name(self):  # noqa: VACUOUS_ASSERTION — the refusal names the task and no row exists
        tid = self.file()
        self.git("branch", "lane/lane-a")
        self.assertEqual(taskkey.record_lane(self.repo, "lane-a", tid),
                         (True, None))
        tasks.close(tid, "closed between the claim and the first dispatch")
        row, why = dispatches.add("seat-b", "lane-a", repo=self.repo,
                                  kind="review", notify=False, new_work=True,
                                  _reason=True, ref=self.a)
        self.assertIsNone(row)
        self.assertIn(tid, why)
        self.assertIn("closed", why)
        self.assertEqual(dispatches.rows(), {})

    def test_the_cli_takes_task(self):  # noqa: VACUOUS_ASSERTION — the stored row's task is asserted equal to the filed id
        tid = self.file()
        rc, _out, err = run(dispatches.cmd_dispatch,
                            ["add", "seat-b", "lane-a", "--ref", self.a,
                             "--kind", "review", "--repo", self.repo,
                             "--new-work", "--task", tid])
        self.assertEqual(rc, 0, err)
        (row,) = dispatches.rows().values()
        self.assertEqual(row["task"], tid)


class LandsTest(ChainBase):
    """Landed is reported through the join, and nothing closes the task."""

    def test_a_done_train_reports_its_cars_tasks_as_landed(self):
        from helm import autoland
        tid = tasks.add("the thing", "seat-b", force_new=True)[0]["id"]
        row = self.root(task=tid)
        done = os.path.join(autoland.state_dir(self.repo),
                            autoland.DONE_SUBDIR)
        os.makedirs(done)
        for name, state, land in (("train7", autoland.DONE, 90),
                                  ("train8", autoland.ABANDONED, None)):
            with open(os.path.join(done, name + ".json"), "w") as fh:
                json.dump({"train": name, "name": name, "state": state,
                           "land": land, "head": "0" * 40, "pushed_ts": 5.0,
                           "cars": [{"id": row["id"], "lane": "lane-a",
                                     "tip": self.a, "task": None}]}, fh)
        lands, why = taskkey.lands_by_task(self.repo)
        self.assertIsNone(why, why)
        self.assertEqual([(r["land"], r["train"], r["via"])
                          for r in lands[tid]],
                         [(90, "train7", taskkey.STORED)])
        key = taskkey.join(row=row, lands=lands)
        self.assertEqual(key.landed, lands[tid])
        self.assertEqual(taskkey.landed_state(tasks.get(tid), key.landed),
                         taskkey.LANDED_UNREAD)
        self.assertEqual(tasks.get(tid)["status"], "open")

    def test_a_force_abandoned_train_on_trunk_landed_and_an_unpushed_one_did_not(self):
        from helm import autoland
        on, off = (tasks.add(t, "seat-b", force_new=True)[0]["id"]
                   for t in ("pushed then abandoned", "never pushed"))
        rows = [self.root(lane="lane-%d" % i, task=tid)
                for i, tid in enumerate((on, off))]
        done = os.path.join(autoland.state_dir(self.repo),
                            autoland.DONE_SUBDIR)
        os.makedirs(done)
        for name, row, abandoned in (
                ("train9", rows[0], {"forced": True, "pushed": "yes",
                                     "owed": ["x"]}),
                ("train10", rows[1], {"by": "i", "reason": "r"})):
            with open(os.path.join(done, name + ".json"), "w") as fh:
                json.dump({"train": name, "name": name,
                           "state": autoland.ABANDONED, "land": None,
                           "head": "1" * 40, "pushed_ts": 6.0,
                           "abandoned": abandoned,
                           "cars": [{"id": row["id"], "lane": row["lane"],
                                     "tip": self.a, "task": None}]}, fh)
        lands, why = taskkey.lands_by_task(self.repo)
        self.assertIsNone(why, why)
        self.assertEqual([r["train"] for r in lands.get(on, ())], ["train9"])
        self.assertNotIn(off, lands)


    def test_a_car_recorded_unknown_at_its_merge_is_never_proved(self):
        """R2: the merge line said task UNKNOWN, so the land proves no task;
        its first row is never re-read to turn that into a task land."""
        from helm import autoland
        tid = tasks.add("the thing", "seat-b", force_new=True)[0]["id"]
        row = self.root(task=tid)
        done = os.path.join(autoland.state_dir(self.repo),
                            autoland.DONE_SUBDIR)
        os.makedirs(done)
        for name, unknown in (("train11", "contradictory: two records"),
                              ("train12", None)):
            with open(os.path.join(done, name + ".json"), "w") as fh:
                json.dump({"train": name, "name": name,
                           "state": autoland.DONE, "land": 91,
                           "head": "2" * 40, "pushed_ts": 7.0,
                           "cars": [{"id": row["id"], "lane": "lane-a",
                                     "tip": self.a, "task": None,
                                     "task_unknown": unknown}]}, fh)
        lands, why = taskkey.lands_by_task(self.repo)
        self.assertIsNone(why, why)
        self.assertEqual([r["train"] for r in lands.get(tid, ())],
                         ["train12"])


class TrainBlameUnknownTest(ChainBase):
    """FINDING 4: an UNKNOWN join is said with its reason, never read as
    "no task"."""

    def test_a_contradictory_key_is_unknown_with_its_reason(self):
        one, two = (tasks.add(t, "seat-b", force_new=True)[0]["id"]
                    for t in ("one", "two"))
        row = self.root(lane="lane-a", task=one)
        self.git("branch", "lane/lane-a")
        self.git("config", "branch.lane/lane-a.helmTask", two)
        task, unknown, refusal = trainblame.lane_task(row["id"])
        self.assertIsNone(task)
        self.assertIsNone(refusal)
        self.assertIn("contradictory", unknown)
        plain = self.root(lane="plain-lane")
        self.assertEqual(trainblame.lane_task(plain["id"]),
                         (None, None, None))
        from helm import autoland
        detail = autoland._merge_detail({
            "id": row["id"], "lane": "lane-a", "task": None,
            "task_unknown": unknown, "doors": [], "author": "a",
            "reader": "b", "basis": "approved"})
        self.assertTrue(detail.startswith("task UNKNOWN"), detail)
        self.assertNotIn("(", detail.split(";")[0])


class RowTasksTest(ChainBase):
    """FINDING 5: the land board's task comes from the one join over the
    chain row and the lane record, read once, never the label alone."""

    def test_rows_join_once_with_their_lane_records(self):
        seven = tasks.add("seven", "seat-b", force_new=True)[0]["id"]
        row = self.root(lane="task-12-x", task=seven)
        keys = taskkey.row_tasks({row["id"]: row}, dispatches.rows())
        self.assertEqual(keys[row["id"]].task, seven)


if __name__ == "__main__":
    unittest.main()
