#!/usr/bin/env python3
"""helm work — worktree lifecycle on the claims lane. Hermetic: HELM_HOME +
HELM_CHAT_DIR are tmp dirs, git global/system config nulled, every room is a
scratch repo minted in setUp — the real repo and its worktrees are never
touched (every `helm work` call pins --repo at the scratch root)."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seats, work  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CELL_BIN", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_WORK_INTEGRATOR")


def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=30)


class WorkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-work-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "david"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(self.root, *cmd).returncode, 0)
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("seed\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def work(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def room(self, lane, dirty=None):
        """Mint a lease-less room directly (git only, never the desk)."""
        path = work.lane_path(self.root, lane)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                work.lane_branch(lane), path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        if dirty:
            with open(os.path.join(path, dirty), "w") as f:
                f.write("precious uncommitted bytes\n")
        return path


class ClaimTest(WorkBase):
    def test_claim_mints_room_lock_and_lease(self):
        rc, out, err = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path, branch, lease, ttl = out.strip().split("\t")
        self.assertEqual(path, work.lane_path(self.root, "demo"))
        self.assertEqual(branch, "lane/demo")
        self.assertEqual(int(ttl), work.DEFAULT_TTL)
        self.assertTrue(os.path.isdir(path))
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertTrue(row["locked"])                    # do-not-disturb tag
        self.assertEqual(row["reason"], "lease:" + lease[:8])
        self.assertIn("worktree:proj:demo",
                      [c["resource"] for c in seats.claims_list()])
        # idempotent re-entry: the holder re-claims with the lease, same room
        rc2, out2, err2 = self.work("claim", "demo", "--seat", "s1",
                                    "--lease", lease)
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(out2.strip().split("\t")[:3], [path, branch, lease])
        # a second seat without the capability is refused, holder named
        rc3, _out3, err3 = self.work("claim", "demo", "--seat", "s2")
        self.assertEqual(rc3, 1)
        self.assertIn("held by s1", err3)

    def test_claim_bad_lane_name(self):
        rc, _out, err = self.work("claim", "no/slashes")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)


class ReleaseTest(WorkBase):
    def test_release_clean_removes_room_and_merged_branch(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(seats.claims_list(), [])         # key surrendered
        self.assertFalse(work._has_branch(self.root, "lane/demo"))  # merged: -d

    def test_release_dirty_refuses_then_park_commits(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _b, lease, _t = out.strip().split("\t")
        junk = os.path.join(path, "junk.txt")
        with open(junk, "w") as f:
            f.write("do not lose me\n")
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 1)                     # inspect before the key
        self.assertIn("--park", err)                # the two exits are named
        self.assertTrue(os.path.exists(junk))       # nothing touched
        self.assertEqual(len(seats.claims_list()), 1)   # key still in pocket
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease, "--park")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        show = _sh(self.root, "git", "show", "lane/demo:junk.txt")
        self.assertEqual(show.stdout, "do not lose me\n")   # lost-and-found
        self.assertIn("awaits integration", out)  # unmerged branch stays, told

    def test_release_occupied_room_keeps_room_and_lease(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], cwd=path)
        try:
            rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                      "--lease", lease)
            self.assertEqual(rc, 1)
            self.assertIn("OCCUPIED", err)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(len(seats.claims_list()), 1)
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_release_wrong_lease_removes_nothing(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path = out.split("\t")[0]
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", "beefbeefbeefbeef")
        self.assertEqual(rc, 1)                 # key surrender precedes removal
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), 1)


class GcTest(WorkBase):
    def setUp(self):
        super().setUp()
        rc, out, _e = self.work("claim", "held", "--seat", "s1")
        self.assertEqual(rc, 0)
        self.held = out.split("\t")[0]                    # live lease
        self.locked = self.room("noturn")                 # out-of-band lock
        _sh(self.root, "git", "worktree", "lock", self.locked,
            "--reason", "owner says keep")
        self.clean = self.room("cleanmg")                 # clean + merged,
        _sh(self.root, "git", "worktree", "lock", self.clean,
            "--reason", "lease:deadbeef")                 # stale key tag
        self.dirty = self.room("messy", dirty="junk.txt")  # dirty, lease-less

    def test_dry_run_verdicts_touch_nothing(self):
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        for path in (self.held, self.locked, self.clean, self.dirty):
            self.assertTrue(os.path.isdir(path))
        self.assertRegex(out, r"KEEP\s+held\s+lease live")
        self.assertRegex(out, r"KEEP\s+noturn\s+locked out-of-band")
        self.assertRegex(out, r"REMOVE\s+cleanmg")
        self.assertRegex(out, r"RESCUE\s+messy")
        self.assertIn("dry-run", out)
        self.assertIn("never discarded", out)

    def test_apply_sweeps_and_rescues_never_discards(self):
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.held))      # guest in the room
        self.assertTrue(os.path.isdir(self.locked))    # do-not-disturb
        self.assertFalse(os.path.exists(self.clean))   # clean+merged swept
        self.assertFalse(work._has_branch(self.root, "lane/cleanmg"))
        self.assertFalse(os.path.exists(self.dirty))   # room swept, BYTES SAVED
        show = _sh(self.root, "git", "show", "lane/messy:junk.txt")
        self.assertEqual(show.stdout, "precious uncommitted bytes\n")
        self.assertTrue(work._has_branch(self.root, "lane/messy"))
        self.assertIn("rescued", out)

    def test_apply_never_removes_an_occupied_room(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        proc = subprocess.Popen(["sleep", "30"], cwd=self.clean)
        try:
            rc, out, err = self.work("gc", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertIn("OCCUPIED", out)
            self.assertTrue(os.path.isdir(self.clean))
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_enact_rechecks_occupancy_after_scan(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        proc = subprocess.Popen(["sleep", "30"], cwd=self.clean)
        try:
            lines = work.gc_enact(self.root, row)
            self.assertTrue(any("OCCUPIED" in line for line in lines))
            self.assertTrue(os.path.isdir(self.clean))
        finally:
            proc.terminate()
            proc.wait()

    def test_gc_orphans_feeds_the_helm_gc_report_row(self):
        got = work.gc_orphans(self.root)
        self.assertEqual(sorted(got), sorted([self.clean, self.dirty]))


class GcUnmergedTest(WorkBase):
    def test_unmerged_clean_room_removed_branch_stays(self):
        path = self.room("aheadln")
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(work._has_branch(self.root, "lane/aheadln"))
        self.assertIn("stays for the integrator", out)


class ListTest(WorkBase):
    def test_list_joins_registry_with_claims(self):
        rc, _out, err = self.work("claim", "webui", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray", dirty="j.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        row_w = next(ln for ln in out.splitlines() if "webui" in ln)
        self.assertRegex(row_w, r"webui\s+s1 \d+s\s+clean")
        row_s = next(ln for ln in out.splitlines() if "stray" in ln)
        self.assertRegex(row_s, r"stray\s+-\s+dirty")

    def test_list_empty(self):
        rc, out, _err = self.work("list")
        self.assertEqual(rc, 0)
        self.assertIn("no lane rooms", out)


class UnguardedRoomTest(WorkBase):
    """Worktrees outside `<root>-wt/` — above all the agent-spawned rooms under
    .claude/worktrees/wf_* — are real writable checkouts that no lease covers.
    `list` showed only lane rooms, so occupancy lived solely in chat prose and
    three agents came to share one review room."""

    def foreign(self, name, dirty=None):
        path = os.path.join(self.root, ".claude", "worktrees", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "worktree-" + name, path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        if dirty:
            with open(os.path.join(path, dirty), "w") as f:
                f.write("a reviewer's uncommitted security fixes\n")
        return path

    def test_foreign_worktree_is_reported_as_unguarded(self):
        self.foreign("wf_dead00-1")
        rows = work.unguarded_rows(self.root)
        self.assertEqual([os.path.basename(r["path"]) for r in rows],
                         ["wf_dead00-1"])
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("UNGUARDED", out)
        self.assertIn("wf_dead00-1", out)

    def test_lane_rooms_and_the_checkout_itself_are_not_unguarded(self):
        rc, _out, err = self.work("claim", "mine", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray")
        # Leased room, lease-less lane room, and the shared checkout all sit
        # INSIDE the claims system — only foreign paths may be flagged.
        self.assertEqual(work.unguarded_rows(self.root), [])

    def test_recent_write_reads_as_live_without_any_cwd_occupant(self):
        # The subagent-writer shape: bytes land in the room, nothing is ever
        # cwd'd there. The cwd census alone reports it empty.
        path = self.foreign("wf_busy00-2", dirty="cred.py")
        self.assertEqual(work._occupants(path), [])
        row = next(r for r in work.unguarded_rows(self.root)
                   if r["path"] == path)
        self.assertTrue(row["dirty"])
        self.assertIsNotNone(row["wrote_ago"])
        self.assertLess(row["wrote_ago"], 900)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("assume live", out)

    def test_clean_room_reports_no_work_in_flight(self):
        path = self.foreign("wf_idle00-3")
        self.assertIsNone(work._wrote_ago(path))
        row = next(r for r in work.unguarded_rows(self.root)
                   if r["path"] == path)
        self.assertIsNone(row["wrote_ago"])
        rc, out, _err = self.work("list")
        self.assertIn("no live occupant", out)

    def test_stale_write_is_in_flight_but_not_claimed_live(self):
        path = self.foreign("wf_stale0-4", dirty="half.py")
        old = time.time() - 7200
        os.utime(os.path.join(path, "half.py"), (old, old))
        row = next(r for r in work.unguarded_rows(self.root)
                   if r["path"] == path)
        self.assertGreaterEqual(row["wrote_ago"], 7000)
        rc, out, _err = self.work("list")
        self.assertIn("work in flight", out)
        self.assertNotIn("assume live", out)


class GuardTest(WorkBase):
    def test_install_guard_dry_then_apply_then_heal(self):
        hook = work.hook_path(self.root)
        rc, out, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        self.assertIn("HELM_WORK_INTEGRATOR", out)
        self.assertIn("DRY", out)
        self.assertFalse(os.path.exists(hook))          # print, never run
        rc, out, _err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.access(hook, os.X_OK))
        # the motivating failure: `checkout -b` in the shared checkout heals —
        # pointer back to main, the created branch SURVIVES, message is loud
        r = _sh(self.root, "git", "checkout", "-b", "oops")
        self.assertEqual(r.returncode, 0, r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "main")
        self.assertTrue(work._has_branch(self.root, "oops"))
        self.assertIn("integrator", r.stderr)
        # worktree add must NOT trip the guard (lane rooms are unguarded)
        wt = self.room("quiet")
        self.assertEqual(
            _sh(wt, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "lane/quiet")
        # the escape hatch: the integrator's own env skips the heal
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "checkout", "-q", "-b", "intg"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "intg")

    def test_install_guard_refuses_foreign_hook(self):
        hook = work.hook_path(self.root)
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("not overwriting", err)
        with open(hook) as f:
            self.assertIn("echo mine", f.read())


class WiringTest(unittest.TestCase):
    def test_cli_and_gc_wiring(self):
        from helm import cli, gc
        self.assertIn("work", cli.VERBS)
        self.assertIn("work", cli._VERB_HELP)
        rows = [p for p in gc.POLICIES if p["stream"] == "work-worktrees"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cls"], "state")   # report-only, never reaped
        self.assertEqual(rows[0]["act"], "report")


if __name__ == "__main__":
    unittest.main()
