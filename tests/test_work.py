#!/usr/bin/env python3
"""helm work — worktree lifecycle on the claims lane. Hermetic: HELM_HOME +
HELM_CHAT_DIR are tmp dirs, git global/system config nulled, every room is a
scratch repo minted in setUp — the real repo and its worktrees are never
touched (every `helm work` call pins --repo at the scratch root)."""
import concurrent.futures
import contextlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seats, vcs, work  # noqa: E402
from helm.work import _guard as _work_guard  # noqa: E402
from helm.work import _claims as _work_claims  # noqa: E402
from helm.work import _cli as _work_cli  # noqa: E402
from helm.work import _gc as _work_gc  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CELL_BIN", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_WORK_INTEGRATOR",
            "HELM_TEST_HOOK_LOG", "HELM_PRIVATE_NEEDLES")


def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=30)


def _reap(proc, deadline=30.0):
    """Wait for `proc` to exit, polling to a generous deadline.

    The behaviour under test is that the process IS reaped — never that it is
    reaped within 3 seconds while a whole suite runs beside it. A bare
    proc.wait(timeout=3) measures the BOX, not the reap: measured 2026-08-02,
    the orca-shell pair red-ed whole-suite at ~0.6s of real work against a 3s
    budget (5x headroom inside this fleet host's demonstrated noise band,
    which GraalPy showed can stretch a single test 31x) while passing every
    isolated, repeated, and module-scoped run. Poll fast, return the moment
    the process exits — faster than the fixed wait on a quiet box, immune on
    a loud one. Raises TimeoutExpired after `deadline` exactly as wait would.
    """
    end = time.monotonic() + deadline
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        if time.monotonic() >= end:
            raise subprocess.TimeoutExpired(proc.args, deadline)
        time.sleep(0.05)


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


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
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        # An ABSENT needle file: install-guard now composes a pre-commit
        # never-track scan, so every scratch-repo commit here runs it — the
        # operator's real needle file must never leak into these fixtures'
        # behavior (hermeticity), and an absent file is the documented no-op.
        os.environ["HELM_PRIVATE_NEEDLES"] = os.path.join(
            self.tmp, "no-needles-configured.txt")
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
        """Mint a lease-less room directly (git only, never the desk).

        Carries HELM_WORK_INTEGRATOR=1 because once the ref-guard is installed
        this raw `worktree add -b` is REFUSED by design — minting a branch in
        the shared checkout is exactly what the rail exists to stop, and the
        sanctioned paths are `helm work claim` or this override. The fixture
        is standing in for the integrator, so it declares that rather than
        quietly weakening the guard to let a test through."""
        path = work.lane_path(self.root, lane)
        r = subprocess.run(["git", "worktree", "add", "-q", "-b",
                            work.lane_branch(lane), path, "main"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
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

    def test_claim_refuses_the_reserved_seat_home_container(self):
        """`<root>-wt/seats/` holds the per-seat HOME worktrees (slice 0); a
        lane by that name would check a room out on top of the container."""
        from helm import harness
        rc, _out, err = self.work("claim", harness.SEAT_HOME_DIRNAME)
        self.assertEqual(rc, 2)
        self.assertIn("reserved", err)
        self.assertFalse(os.path.isdir(
            os.path.join(self.tmp, "proj-wt", harness.SEAT_HOME_DIRNAME)))

    def test_lane_inference_skips_a_seat_home_worktree(self):
        """A seat living in its HOME shares the -wt container but is not in a
        lane room; inferring 'seats' would aim release at a lane that does not
        exist. A real lane room still infers."""
        from helm import harness
        home = harness.ensure_home_worktree("codex", self.root)
        self.assertIsNone(work._infer_lane(self.root, home))
        self.assertEqual(work._infer_lane(self.root,
                                          work.lane_path(self.root, "demo")),
                         "demo")
        self.assertIsNone(work._infer_lane(self.root, self.root))


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

    def test_release_refuses_the_room_supplying_its_running_binary(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        with mock.patch.object(_work_claims, "_runtime_source_in",
                               return_value=True):
            rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                      "--lease", lease)
        self.assertEqual(rc, 1)
        self.assertIn("running helm binary", err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), 1)

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
        self.assertTrue(os.path.isdir(path))       # unlanded room stays too
        show = _sh(self.root, "git", "show", "lane/demo:junk.txt")
        self.assertEqual(show.stdout, "do not lose me\n")   # lost-and-found
        self.assertIn("TRIAGE demo", out)
        self.assertIn("git committer, shared across seats:", out)
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertFalse(row["locked"])            # released room is inspectable

    def test_release_occupied_room_keeps_room_and_lease(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
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

    def test_release_landed_room_stops_only_disposable_orca_shell(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            with mock.patch.object(_work_claims,
                                   "_disposable_worktree_occupant",
                                   return_value=True), \
                    mock.patch.object(_work_gc,
                                      "_disposable_worktree_occupant",
                                      return_value=True):
                rc, out, err = self.work("release", "demo", "--seat", "s1",
                                         "--lease", lease)
            self.assertEqual(rc, 0, err)
            _reap(proc)
            self.assertIsNotNone(proc.poll(),  # noqa: VACUOUS_ASSERTION — reaped-or-not is the claim; _reap above already raises if it never exits
                                 "release left the disposable shell running")
            self.assertIn("stopped disposable Orca shell", out)
            self.assertFalse(os.path.exists(path))
            self.assertEqual(seats.claims_list(), [])
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()

    def test_wrong_lease_cannot_stop_a_disposable_orca_shell(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, _lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            with mock.patch.object(_work_claims,
                                   "_disposable_worktree_occupant",
                                   return_value=True), \
                    mock.patch.object(_work_gc,
                                      "_disposable_worktree_occupant",
                                      return_value=True):
                rc, out, err = self.work("release", "demo", "--seat", "s1",
                                         "--lease", "beefbeefbeefbeef")
            self.assertEqual(rc, 1)
            self.assertIsNone(proc.poll(), "wrong capability stopped a process")
            self.assertNotIn("stopped disposable", out + err)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(len(seats.claims_list()), 1)
        finally:
            proc.terminate()
            proc.wait()

    def test_unlanded_release_keeps_a_disposable_orca_shell(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        with open(os.path.join(path, "lane.txt"), "w") as f:
            f.write("unlanded\n")
        _sh(path, "git", "add", "-A")
        self.assertEqual(_sh(path, "git", "commit", "-q", "-m", "lane").returncode,
                         0)
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            with mock.patch.object(_work_claims,
                                   "_disposable_worktree_occupant",
                                   return_value=True), \
                    mock.patch.object(_work_gc,
                                      "_disposable_worktree_occupant",
                                      return_value=True):
                rc, out, err = self.work("release", "demo", "--seat", "s1",
                                         "--lease", lease)
            self.assertEqual(rc, 0, err)
            self.assertIsNone(proc.poll(), "unlanded release stopped the shell")
            self.assertNotIn("stopped disposable", out)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(work._has_branch(self.root, "lane/demo"))
            self.assertEqual(seats.claims_list(), [])
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

    def test_wrong_lease_cannot_park_commit_before_refusal(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path = out.split("\t")[0]
        with open(os.path.join(path, "precious.txt"), "w") as f:
            f.write("must remain uncommitted\n")
        before = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", "beefbeefbeefbeef", "--park")
        self.assertEqual(rc, 1)
        self.assertIn("refresh needs", err)
        self.assertEqual(_sh(path, "git", "rev-parse", "HEAD").stdout.strip(),
                         before)
        self.assertIn("?? precious.txt", _sh(path, "git", "status", "--short").stdout)

    def test_release_unmerged_clean_lane_keeps_room_branch_and_triage(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        with open(os.path.join(path, "land-me.txt"), "w") as f:
            f.write("unlanded\n")
        _sh(path, "git", "add", "-A")
        # Commit with distinct author and committer to verify %cn format specifier
        env = dict(os.environ, GIT_AUTHOR_NAME="Alice Author", GIT_AUTHOR_EMAIL="alice@example.com",
                   GIT_COMMITTER_NAME="Bob Committer", GIT_COMMITTER_EMAIL="bob@example.com")
        proc = subprocess.run(["git", "commit", "-q", "-m", "lane work"], cwd=path, env=env)
        self.assertEqual(proc.returncode, 0)
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/demo"))
        self.assertRegex(out, r"TRIAGE demo .*tip [0-9a-f]{12}, age .*\(git committer, shared across seats: Bob Committer <bob@example.com> — not seat provenance\)")
        self.assertEqual(seats.claims_list(), [])


class ListHolderlessDeltaTest(WorkBase):
    """#289 third instance: a lease expires, the room and its COMMITTED delta
    do not — and nothing listed it, so the failure mode is a seat silently
    redoing the work (two seats, one gc-triage fix, two lane names,
    2026-08-05). The list now calls out holderless-with-delta by default."""

    def _commit_in(self, path, name):
        with open(os.path.join(path, name), "w") as f:
            f.write("lane work\n")
        self.assertEqual(_sh(path, "git", "add", "-A").returncode, 0)
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_holderless_room_with_commits_is_called_out(self):
        path = self.room("orphan-delta")
        self._commit_in(path, "work.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("HOLDERLESS WITH DELTA", out)
        section = out.split("HOLDERLESS WITH DELTA", 1)[1]
        self.assertIn("orphan-delta", section)
        self.assertIn("helm work claim orphan-delta", section)

    def test_the_section_obeys_the_empty_section_law(self):  # noqa: VACUOUS_ASSERTION — in-test flip: the SAME room gains a commit and the SAME observable speaks, three lines down
        # No delta -> silent; the SAME room gaining a commit flips the SAME
        # observable (the positive control that makes the absence a fact
        # about the filter, not about a broken list).
        path = self.room("orphan-quiet")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("HOLDERLESS WITH DELTA", out)
        self._commit_in(path, "late.txt")
        rc, out, err = self.work("list")
        self.assertIn("HOLDERLESS WITH DELTA", out)

    def test_a_held_room_with_delta_is_not_in_the_section(self):  # noqa: VACUOUS_ASSERTION — in-test flip: releasing the SAME room enters it in the section
        rc, out, err = self.work("claim", "held-delta")
        self.assertEqual(rc, 0, err)
        path = work.lane_path(self.root, "held-delta")
        self._commit_in(path, "held.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # held rows stay out; the section exists only if some OTHER
        # holderless-with-delta room does — there is none here.
        self.assertNotIn("HOLDERLESS WITH DELTA", out)
        # positive control: the same room released (parked work) enters it.
        # The release MUST succeed and the assertions MUST run — a control
        # behind `if rc2 == 0` skips silently on a failed release, which is
        # a pass whose input was missing (codex-2's review caught exactly
        # that conditional).
        lease = re.search(r"lease=([0-9a-f]+)", out)
        self.assertIsNotNone(lease, "the list did not hand back the lease "
                             "token for this seat's own row")
        rc2, out2, err2 = self.work(
            "release", "held-delta", "--lease", lease.group(1))
        self.assertEqual(rc2, 0, (out2, err2))
        rc3, out3, _ = self.work("list")
        self.assertIn("HOLDERLESS WITH DELTA", out3)
        self.assertIn("held-delta",
                      out3.split("HOLDERLESS WITH DELTA", 1)[1])


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
        self.assertTrue(os.path.isdir(self.dirty))     # rescued + kept unlanded
        show = _sh(self.root, "git", "show", "lane/messy:junk.txt")
        self.assertEqual(show.stdout, "precious uncommitted bytes\n")
        self.assertTrue(work._has_branch(self.root, "lane/messy"))
        self.assertIn("rescued", out)
        self.assertIn("room kept", out)

    def test_apply_REFUSES_when_the_trunk_ref_cannot_be_refreshed(self):
        """gc_scan decides landedness against a REMOTE-TRACKING ref, which is a
        local snapshot, and --apply deletes branches on that decision. An
        unrefreshed proof must not authorize a deletion — same law as `lr
        land`'s deletion guard: an unscannable deletion set is not a known-safe
        one. vcs.py:405 already stated the rule as prose and nothing did it."""
        # PATCH THE CONSUMER, NOT THE DEFINER. _cli does `from ._gc import
        # refresh_trunk`, so the name it calls is bound at import — patching
        # _gc.refresh_trunk leaves the caller pointing at the original and the
        # test measures an object nobody uses. It passed for that reason first.
        from helm.work import _cli as _workcli
        with mock.patch.object(_workcli, "refresh_trunk",
                               return_value=(False, "git fetch origin failed: boom")):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING to apply", err)
        self.assertIn("boom", out)
        # AND NOTHING WAS TOUCHED — the refusal is before the scan, so no
        # verdict was even computed, let alone enacted
        for path in (self.held, self.locked, self.clean, self.dirty):
            self.assertTrue(os.path.isdir(path), path)
        self.assertTrue(work._has_branch(self.root, "lane/cleanmg"))

    def test_the_DRY_RUN_never_fetches(self):
        """A read-only pass must not touch the network. The refusal above is
        the price of DESTRUCTION, not of looking."""
        from helm.work import _cli as _workcli
        with mock.patch.object(_workcli, "refresh_trunk") as fetched:
            rc, _out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        fetched.assert_not_called()

    def test_a_LOCAL_trunk_has_nothing_to_refresh_and_that_is_not_a_failure(self):
        """THE CONTROL. Without it the refusal test passes for a version that
        refuses every apply — which would be 'safe' and useless."""
        from helm.work import _gc
        ok, why = _gc.refresh_trunk(self.root)
        self.assertTrue(ok, why)
        self.assertIn("nothing to refresh", why)
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.clean))   # the sweep still happens

    def test_apply_never_removes_an_occupied_room(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=self.clean)
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
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=self.clean)
        try:
            lines = work.gc_enact(self.root, row)
            self.assertTrue(any("OCCUPIED" in line for line in lines))
            self.assertTrue(os.path.isdir(self.clean))
        finally:
            proc.terminate()
            proc.wait()

    def test_scan_keeps_the_room_supplying_the_running_gc_binary(self):
        with mock.patch.object(_work_gc, "_runtime_source_in",
                               side_effect=lambda p: p == self.clean):
            row = next(r for r in work.gc_scan(self.root)
                       if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("RUNNING this helm GC binary", row["why"])

    def test_enact_rechecks_the_running_source_room(self):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        with mock.patch.object(_work_gc, "_runtime_source_in",
                               side_effect=lambda p: p == self.clean):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertTrue(any("RUNNING this helm GC binary" in line
                            for line in lines), lines)

    def test_runtime_source_relationship_is_path_based(self):
        source_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(_work_gc.__file__))))
        self.assertTrue(_work_gc._runtime_source_in(source_root))
        self.assertFalse(_work_gc._runtime_source_in(self.root))

    def test_gc_sees_harness_minted_rooms_not_only_lane_rooms(self):
        """THE SWEEP WAS BLIND BY CONSTRUCTION. lane_rows matches only
        `<root>-wt/`, so every gc built on it could not see the subagent and
        workflow worktrees under `.claude/worktrees/` — it reported a clean tree
        while 12 of 29 rooms sat abandoned, two holding uncommitted work from
        agents that had died.

        Those are also the rows the OWNER sees: orca lists every worktree in its
        sidebar, so a dozen unreadable `wf_<id>` entries sat between him and the
        six seats he actually talks to."""
        from helm.work import _lanes
        registered = [
            {"path": os.path.join(self.root + "-wt", "a-lane"), "branch": None,
             "locked": False, "reason": ""},
            {"path": os.path.join(self.root, ".claude", "worktrees",
                                  "wf_deadbeef-000-1"), "branch": None,
             "locked": False, "reason": ""},
            {"path": os.path.join(self.root, ".claude", "worktrees",
                                  "agent-abc123"), "branch": None,
             "locked": False, "reason": ""},
            # a NESTED path under the box is not a direct child and must not match
            {"path": os.path.join(self.root, ".claude", "worktrees", "wf_x",
                                  "deeper"), "branch": None,
             "locked": False, "reason": ""},
        ]
        lanes = _lanes.lane_rows(self.root, registered=registered)
        autos = _lanes.auto_rows(self.root, registered=registered)
        self.assertEqual([r["lane"] for r in lanes], ["a-lane"])
        self.assertEqual(sorted(r["lane"] for r in autos),
                         ["agent-abc123", "wf_deadbeef-000-1"])
        # the two views are DISJOINT — a room belongs to exactly one sweep
        self.assertFalse(set(r["path"] for r in lanes) &
                         set(r["path"] for r in autos))
        # and harness-minted rows are marked, so a caller can tell them apart
        self.assertTrue(all(r.get("harness_minted") for r in autos))

    def test_gc_orphans_feeds_the_helm_gc_report_row(self):
        got = work.gc_orphans(self.root)
        self.assertEqual(sorted(got), sorted([self.clean, self.dirty]))

    def test_enact_rechecks_branch_is_still_landed(self):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        with open(os.path.join(self.clean, "late.txt"), "w") as f:
            f.write("arrived after scan\n")
        _sh(self.clean, "git", "add", "-A")
        self.assertEqual(_sh(self.clean, "git", "commit", "-q", "-m",
                             "late unlanded work").returncode, 0)
        lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertTrue(any("became unlanded" in line for line in lines), lines)

    def test_enact_unknown_ancestry_never_authorizes_delete(self):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        with mock.patch.object(_work_gc, "_merge_state",
                               return_value=vcs.UNKNOWN):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertTrue(work._has_branch(self.root, "lane/cleanmg"))
        self.assertTrue(any("UNKNOWN" in line for line in lines), lines)
        self.assertFalse(any(line.startswith("removed ") for line in lines), lines)

    def test_disposable_orca_shell_is_the_only_occupied_reap_exception(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=self.clean)
        try:
            with mock.patch.object(_work_gc, "_disposable_worktree_occupant",
                                   return_value=True):
                rc, out, err = self.work("gc", "--apply")
            self.assertEqual(rc, 0, err)
            _reap(proc)
            self.assertIsNotNone(proc.poll(),  # noqa: VACUOUS_ASSERTION — reaped-or-not is the claim; _reap above already raises if it never exits
                                 "gc left the disposable shell running")
            self.assertFalse(os.path.exists(self.clean))
            self.assertIn("stopped disposable Orca shell", out)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()

    def test_disposable_orca_shell_receives_terminal_hangup_not_term(self):
        with mock.patch.object(_work_gc, "_occupants",
                               side_effect=[["123"], []]), \
                mock.patch.object(_work_gc, "_disposable_worktree_occupant",
                                  return_value=True), \
                mock.patch.object(_work_gc.os.path, "exists", return_value=False), \
                mock.patch.object(_work_gc.os, "kill") as kill:
            stopped, error = _work_gc._retire_disposable_occupants("/room")
        self.assertEqual(stopped, ["123"])
        self.assertIsNone(error)
        kill.assert_called_once_with(123, signal.SIGHUP)

    def test_orca_shell_identity_requires_rcfile_Ss_plus_and_no_children(self):
        proc_root = os.path.join(self.tmp, "proc")
        pid = "123"
        task = os.path.join(proc_root, pid, "task", pid)
        os.makedirs(task)
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"bash\0--rcfile\0/home/test/.config/orca/shell-ready\0-i\0")
        with open(os.path.join(proc_root, pid, "stat"), "wb") as f:
            f.write(b"123 (bash) S 1 123 123 34816 123 0 0 0")
        children = os.path.join(task, "children")
        with open(children, "wb") as f:
            f.write(b"")
        self.assertTrue(work._disposable_worktree_occupant(pid, proc_root=proc_root))
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0--rcfile\0/home/u/.config/orca/shell-ready/bash/rcfile\0")
        self.assertTrue(work._disposable_worktree_occupant(pid, proc_root=proc_root),
                        "current Orca shell-ready rcfile shape")
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0--rcfile\0/home/u/.config/orca/shell-ready/bash/other\0")
        self.assertFalse(work._disposable_worktree_occupant(pid, proc_root=proc_root),
                         "nearby arbitrary rcfiles are not disposable")
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0--rcfile\0/home/u/.config/orca/shell-ready/bash/rcfile\0")
        with open(children, "wb") as f:
            f.write(b"456")
        self.assertFalse(work._disposable_worktree_occupant(pid, proc_root=proc_root))

    def test_apply_posts_one_removed_kept_triage_summary(self):
        with mock.patch.object(_work_cli, "post_gc_summary",
                               return_value=None) as post:
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        post.assert_called_once()
        line = post.call_args.args[0]
        self.assertRegex(line, r"worktree gc proj: removed=1 kept=3 triage=1")
        self.assertIn(line, out)

    def test_self_source_room_is_kept_and_summary_still_posts(self):
        with mock.patch.object(_work_gc, "_runtime_source_in",
                               side_effect=lambda p: p == self.clean), \
                mock.patch.object(_work_cli, "post_gc_summary",
                                  return_value=None) as post:
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertIn("RUNNING this helm GC binary", out)
        post.assert_called_once()
        self.assertRegex(post.call_args.args[0],
                         r"worktree gc proj: removed=0 kept=4 triage=1")


class GcPaneBoundRoomTest(WorkBase):
    """GC may not delete a room the metaharness still holds a terminal in.

    THE INCIDENT (2026-07-30). The owner reported, repeatedly and over hours,
    that his agent seats were being "killed back to a CWD". He was told more
    than once that the seats were fine, because every instrument consulted was
    a /proc scan and /proc said the processes were alive. He was right and the
    instruments were looking in the wrong place: a PANE is a metaharness object
    that OUTLIVES its shell, so `_occupants` — processes whose cwd is inside
    the room — cannot clear a room on its own. GC hung up the shell, deleted
    the worktree, and left orca holding a terminal bound to a directory that no
    longer existed. That pane renders as a bare command prompt, which from the
    outside is indistinguishable from someone having killed the agent.

    The two instruments are COMPLEMENTARY, not redundant: /proc catches a shell
    sitting in the room, the metaharness catches a pane bound to the room whose
    shell is not (measured on this host: one live bash at a `(deleted)` cwd,
    and five panes orca listed with no worktree path at all).

    Every test here plants its own answer through the `_panes_bound_to` seam.
    The suite must never ask the operator's real orca — see tests/__init__.py,
    which pins HELM_METAHARNESS=none for exactly that reason.
    """

    def setUp(self):
        super().setUp()
        self.clean = self.room("panelane")   # clean + merged -> REMOVE verdict

    def _gc_apply(self, panes, error=None):
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=(panes, error)):
            return self.work("gc", "--apply")

    def test_the_suite_never_reaches_a_live_metaharness(self):
        """The MUST-HIT for every other test in this class: if an adapter is
        ambient, the unmocked legs below are reading the owner's real fleet and
        their verdicts mean nothing. Measured before the pin: inside an Orca
        pane `orca` is on PATH and ORCA_USER_DATA_PATH is set, so detect()
        returned a live OrcaAdapter and each blocker check shelled out."""
        from helm import harness
        self.assertEqual(os.environ.get("HELM_METAHARNESS"), "none")
        self.assertIsNone(harness.detect())
        self.assertEqual(harness.worktree_panes(self.clean), ([], None))

    def test_a_bound_pane_refuses_removal_even_when_disposable_is_allowed(self):
        """THE INCIDENT, as an assertion. `disposable_ok` is the flag that lets
        GC hang up an Orca shell-ready placeholder; it must NOT also authorize
        deleting the room, because a disposable SHELL says nothing about the
        PANE wrapped around it."""
        handle = "term_97a38e41-bbb3-42e5-929d-7d42435628d8"
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([handle], None)):
            blocked = _work_gc._removal_blocker(
                self.root, self.clean, "panelane",
                stale_lease_ok=True, disposable_ok=True)
        self.assertIsNotNone(blocked, "disposable_ok relaxed the pane guard")
        self.assertIn(handle, blocked)
        rc, out, err = self._gc_apply([handle])
        self.assertEqual(rc, 0, err)
        self.assertIn(handle, out)
        self.assertIn("SKIPPED", out)
        self.assertTrue(os.path.isdir(self.clean), "deleted under a live pane")
        self.assertTrue(work._has_branch(self.root, "lane/panelane"))

    def test_an_unanswerable_metaharness_refuses_removal(self):
        """Fail CLOSED. "I could not look" and "nothing is there" are the same
        value only to code that has stopped caring which one it got."""
        with mock.patch.object(
                _work_gc, "_panes_bound_to",
                return_value=(None, "metaharness pane list unavailable: "
                                    "orca terminal list: rc 1 — daemon down")):
            blocked = _work_gc._removal_blocker(
                self.root, self.clean, "panelane",
                stale_lease_ok=True, disposable_ok=True)
        self.assertIsNotNone(blocked)
        self.assertIn("cannot prove the room is pane-free", blocked)
        self.assertIn("daemon down", blocked)
        rc, out, err = self._gc_apply(None, "orca terminal list: rc 1")
        self.assertEqual(rc, 0, err)
        self.assertIn("cannot prove the room is pane-free", out)
        self.assertTrue(os.path.isdir(self.clean))

    def test_no_metaharness_at_all_blocks_nothing(self):
        """THE INVERSE CONTROL. Without it the guard could refuse every room on
        earth and still look correct above. `([], None)` is an AFFIRMATIVE
        empty — nothing can be bound to a room by a host that does not exist —
        and it must let the sweep through."""
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([], None)):
            blocked = _work_gc._removal_blocker(
                self.root, self.clean, "panelane", stale_lease_ok=True)
        self.assertIsNone(blocked, blocked)
        rc, out, err = self._gc_apply([])
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.clean), out)
        self.assertFalse(work._has_branch(self.root, "lane/panelane"))

    def test_a_pane_in_a_subdirectory_blocks_the_whole_room(self):
        """Path-prefix matching, and the boundary that makes it a prefix rather
        than a substring: `/a/b` must not be blocked by a pane in `/a/bc`."""
        from helm import harness

        class _Stub:
            name = "stub"
            reports_pane_worktree = True

            def __init__(self, rows):
                self.rows = rows

            def list(self):
                return self.rows

        room = self.clean
        deep = os.path.join(room, "src", "nested")
        os.makedirs(deep)
        sibling = room + "c"            # /…/panelane vs /…/panelanec
        os.makedirs(sibling)
        ad = _Stub([{"handle": "h-deep", "worktree": deep},
                    {"handle": "h-sibling", "worktree": sibling}])
        self.assertEqual(harness.worktree_panes(room, adapter=ad),
                         (["h-deep"], None))
        self.assertEqual(harness.worktree_panes(sibling, adapter=ad),
                         (["h-sibling"], None))
        # and the whole way down to the blocker: the room stays
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               side_effect=lambda p: harness.worktree_panes(
                                   p, adapter=ad)):
            blocked = _work_gc._removal_blocker(
                self.root, room, "panelane",
                stale_lease_ok=True, disposable_ok=True)
        self.assertIn("h-deep", blocked or "")
        self.assertNotIn("h-sibling", blocked or "")

    def test_a_pane_reaching_the_room_through_a_symlink_still_blocks(self):
        """The comparison RESOLVES both sides. The metaharness reports whatever
        path the pane was opened with, which need not be spelled the way helm
        spells `<repo>-wt/<lane>`; an abspath-only compare would miss it, and a
        miss here is a delete under a live pane, not a cosmetic mismatch."""
        from helm import harness

        class _Stub:
            name = "stub"
            reports_pane_worktree = True

            def __init__(self, wt):
                self.wt = wt

            def list(self):
                return [{"handle": "h-link", "worktree": self.wt}]

        link = os.path.join(self.tmp, "room-by-another-name")
        os.symlink(self.clean, link)
        self.assertNotEqual(link, self.clean)
        self.assertEqual(harness.worktree_panes(self.clean,
                                                adapter=_Stub(link)),
                         (["h-link"], None))
        # and the reverse spelling: helm asks about the link, orca knows the real
        self.assertEqual(harness.worktree_panes(link,
                                                adapter=_Stub(self.clean)),
                         (["h-link"], None))

    def test_an_unbound_pane_blocks_nothing(self):
        """Five of the panes measured that morning carried no worktree path at
        all. A pane belonging to no room may not keep every room alive."""
        from helm import harness

        class _Stub:
            name = "stub"
            reports_pane_worktree = True

            @staticmethod
            def list():
                return [{"handle": "h-none", "worktree": None},
                        {"handle": "h-empty", "worktree": ""},
                        {"handle": "h-missing"}]

        self.assertEqual(harness.worktree_panes(self.clean, adapter=_Stub()),
                         ([], None))

    def test_an_adapter_that_cannot_answer_is_unanswerable_not_empty(self):
        """The herdr shape. Its `pane list` rows carry pane_id/label/status and
        no worktree, so reading them yields a bare [] that MEANS "this adapter
        never says" and would authorize the delete. An adapter must declare it
        can answer; unproven refuses."""
        from helm import harness

        class _Mute:
            name = "mute"
            # reports_pane_worktree deliberately absent — the default refuses

            @staticmethod
            def list():
                raise AssertionError("must not be asked; it cannot answer")

        panes, err = harness.worktree_panes(self.clean, adapter=_Mute())
        self.assertIsNone(panes)
        self.assertIn("does not report which worktree", err)
        self.assertFalse(harness.HerdrAdapter.reports_pane_worktree)
        self.assertTrue(harness.OrcaAdapter.reports_pane_worktree)


class GcClosesTheBoundPaneTest(WorkBase):
    """THE DEADLOCK, AND THE ORDER THAT MAKES BREAKING IT SAFE.

    A room could not be removed because a pane was bound to it; the pane stayed
    in the owner's sidebar because the room existed. Measured 2026-08-04: a
    reaper run PLANNED two removals, completed ZERO, and reported removed=0.

    The guard from GcPaneBoundRoomTest is NOT relaxed here — it is SATISFIED.
    The pane is closed, the binding is re-read from the adapter, and only then
    does the unrelaxed blocker authorize the delete. Reversed, this reproduces
    the 2026-07-30 incident exactly."""

    class _Adapter:
        """Records what it was asked to close; never touches a real pane."""
        def __init__(self, fail=None):
            self.closed, self.fail = [], fail
        def stop(self, handle):
            if self.fail:
                raise OSError(self.fail)
            self.closed.append(handle)

    def setUp(self):
        super().setUp()
        self.clean = self.room("panelane")     # clean + merged -> REMOVE

    def _run(self, pane_reads, adapter):
        """pane_reads: successive (handles, error) answers as gc re-asks."""
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               side_effect=list(pane_reads)), \
             mock.patch("helm.harness.detect", return_value=adapter):
            return self.work("gc", "--apply")

    def test_the_pane_is_closed_and_THEN_the_room_is_removed(self):
        h = "term_5d72fa07-1111-2222-3333-444455556666"
        ad = self._Adapter()
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the room is
        # THERE before the run, so "gone" afterwards measures the removal and
        # not a room that never existed.
        self.assertTrue(os.path.isdir(self.clean))
        # bound at the deferred check, bound at the close, GONE on re-verify,
        # GONE for the final unrelaxed blocker.
        rc, out, err = self._run([([h], None), ([], None), ([], None)], ad)
        self.assertEqual(rc, 0, err)
        # THE EFFECT, not the absence of a complaint:
        self.assertEqual(ad.closed, [h], "the pane must actually be closed")
        self.assertFalse(os.path.isdir(self.clean), "the room must be GONE")
        self.assertIn("closed metaharness pane(s) %s" % h, out)

    def test_a_close_that_RAISES_keeps_the_room(self):
        h = "term_aaaa"
        ad = self._Adapter(fail="pane close refused")
        rc, out, _e = self._run([([h], None)], ad)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.clean),
                        "a failed close must never remove the room")
        self.assertIn("could not close metaharness pane", out)

    def test_a_pane_STILL_BOUND_after_the_close_keeps_the_room(self):
        """`stop` returning cleanly says the command was accepted, not that the
        pane is gone. Only a fresh read of the binding proves the room free."""
        h = "term_bbbb"
        ad = self._Adapter()
        rc, out, _e = self._run([([h], None), ([h], None)], ad)
        self.assertEqual(rc, 0)
        self.assertEqual(ad.closed, [h], "it did try")
        self.assertTrue(os.path.isdir(self.clean), "still-bound must KEEP")
        self.assertIn("STILL bound after close", out)

    def test_an_UNREADABLE_re_verify_keeps_the_room(self):
        """Could-not-look is never looked-and-found-nothing."""
        ad = self._Adapter()
        rc, out, _e = self._run([(["term_cccc"], None), (None, "orca down")], ad)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertIn("cannot re-verify", out)

    def test_panes_reported_with_NO_adapter_keeps_the_room(self):
        """Two readings disagree — panes exist but nothing can close them. A
        delete is not how that disagreement gets resolved."""
        rc, out, _e = self._run([(["term_dddd"], None)], None)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertIn("no adapter is available to close them", out)

    def test_a_pane_free_room_never_calls_the_adapter(self):
        """UNCONDITIONAL CONTROL that the close is CONDITIONAL: the ordinary
        path must remove without ever asking the metaharness to close anything,
        or every assertion above would also hold for code that closes blindly."""
        ad = self._Adapter()
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: this recorder
        # DOES record. Without it, `closed == []` would also hold for an
        # adapter that silently drops every close, and the assertion would be
        # measuring the double rather than the code under test.
        ad.stop("term_control")
        self.assertEqual(ad.closed, ["term_control"])
        ad.closed.clear()
        self.assertTrue(os.path.isdir(self.clean))
        rc, _o, err = self._run([([], None), ([], None), ([], None)], ad)
        self.assertEqual(rc, 0, err)
        self.assertEqual(ad.closed, [], "nothing to close, nothing closed")
        self.assertFalse(os.path.isdir(self.clean))


class GcSummaryTellsPlannedVsDoneTest(WorkBase):
    """"removed=0 kept=40" is literally true of an estate with nothing to
    remove AND of one where every planned removal FAILED — and it reads as the
    first. Measured 2026-08-04: a run planned two removals, completed zero, and
    said removed=0; real time was spent believing the estate was clean. The
    counts were all correct and the sentence was still false."""

    def setUp(self):
        super().setUp()
        self.clean = self.room("jammedlane")

    def test_a_blocked_removal_is_NAMED_with_its_reason(self):
        h = "term_eeee"
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([h], None)), \
             mock.patch("helm.harness.detect", return_value=None):
            rc, out, _e = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        # THE EFFECT: the summary must say the intention existed and failed.
        summary = [l for l in out.splitlines() if l.startswith("worktree gc ")]
        self.assertEqual(len(summary), 1, out)
        self.assertRegex(summary[0], r"removed=0 \(1 planned, 1 blocked: [^)]+\)")
        self.assertTrue(os.path.isdir(self.clean), "and the room is kept")

    def test_a_clean_estate_summary_is_UNCHANGED(self):
        """UNCONDITIONAL CONTROL on the same observable: with nothing blocked
        the clause must be ABSENT, or the new text would be decoration that
        always appears and says nothing."""
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([], None)):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.isdir(self.clean), "it really did remove")
        # Assert on the SUMMARY LINE ALONE. A first pass asserted over the
        # whole output and a fixture room named "blockedlane" satisfied
        # assertNotIn("blocked") by accident — the filter matched a name, not
        # the fact. Narrow the observable to the sentence under test.
        summary = [l for l in out.splitlines() if l.startswith("worktree gc ")]
        self.assertEqual(len(summary), 1, out)
        self.assertIn("removed=1", summary[0])
        self.assertNotIn("planned", summary[0])
        self.assertNotIn("blocked", summary[0])


class GcBlockedReasonSurvivesAMultilineErrorTest(unittest.TestCase):
    """FOUND BY DOGFOODING THIS LANE'S OWN FIX, on its first live run.

    The summary printed `blocked: unreported` while the reason sat two
    characters away: an adapter error carried a multi-line JSON body, so the
    ") — kept" the extractor required never appeared on the same line and the
    match failed. A summary that cannot name a reason it HAS is exactly the
    defect this feature exists to fix, one layer in — so the extractor must
    never depend on the closing punctuation."""

    def test_a_multiline_adapter_error_is_still_named(self):
        from helm.work._cli import _blocked_reason
        live = ["SKIPPED /p (could not close metaharness pane term_5d72fa07: "
                "HarnessError: orca terminal close: rc 1 — {",
                '  "error": "no such terminal"',
                "}"]
        self.assertEqual(_blocked_reason(live), "pane close failed")

    def test_the_ordinary_single_line_reasons_still_classify(self):
        """UNCONDITIONAL CONTROL: the extractor distinguishes reasons rather
        than returning one constant. Without this, the assertion above would
        also pass for a function hardcoded to say 'pane close failed'."""
        from helm.work._cli import _blocked_reason
        self.assertEqual(_blocked_reason(
            ["SKIPPED /p (metaharness pane(s) t are bound to this room) — kept"]),
            "bound pane")
        self.assertEqual(_blocked_reason(
            ["SKIPPED /p (OCCUPIED by cwd pid(s) 1) — kept"]), "occupied")
        self.assertEqual(_blocked_reason(["nothing was skipped"]), "unreported")


class GcUnmergedTest(WorkBase):
    def test_unmerged_clean_room_and_branch_stay_with_triage_evidence(self):
        path = self.room("aheadln")
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/aheadln"))
        self.assertRegex(out, r"TRIAGE\s+aheadln.*tip [0-9a-f]{12}, age .*"
                         r"\(git committer, shared across seats:")

    def test_an_unreadable_tip_never_reads_merged_and_triages_distinctly(self):
        """The phantom-unlanded-lanes fold at the REAP decision. rc 128 folded
        into "not merged" hid the unreadable tip inside the clean-negative
        triage; folded the other way it would -d a branch nobody proved
        landed. UNKNOWN must (a) never read as merged and (b) be its OWN
        triage reason, not the clean 'stays for the integrator' prose."""
        path = self.room("ghostln")
        # two lane commits so the MIDDLE object can vanish: `git status` in
        # the room stays readable (HEAD's tree is intact) while the ancestry
        # walk cannot parse the missing parent -> merge-base exits 128
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        mid = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        with open(os.path.join(path, "g.txt"), "w") as f:
            f.write("y\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "tip")
        self.assertEqual(r.returncode, 0, r.stderr)
        os.remove(os.path.join(self.root, ".git", "objects",
                               mid[:2], mid[2:]))          # orphan the walk
        self.assertEqual(_work_gc._merge_state(self.root, "lane/ghostln"),
                         vcs.UNKNOWN)
        self.assertFalse(work._merged(self.root, "lane/ghostln"))
        row = next(r for r in work.gc_scan(self.root)
                   if r["path"] == path)
        self.assertEqual(row["verdict"], "triage")
        self.assertIn("UNKNOWN", row["why"])       # a DISTINCT triage reason
        self.assertIn("unreadable", row["why"])
        self.assertNotIn("stays for the integrator", row["why"])
        # UNKNOWN authorizes no destructive step: both room and branch survive.
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/ghostln"))


class GcRebasedLandTest(WorkBase):
    """THE OWNER'S QUESTION, answered at the reap: "why can't an agent clean up
    after itself when the thing it made is no longer necessary".

    Because the check asked the WRONG QUESTION and got a TRUTHFUL "no". An
    agent finishing a lane asked `merge-base --is-ancestor <tip> <trunk>` —
    SHA identity — while our protocol lands work REBASED, so the commit on
    trunk has a different sha. Ancestry correctly answered "that object is not
    on trunk" and the well-behaved agent kept its branch FOREVER. Measured on
    the live box 2026-08-03: 107 `lane/*` branches against 24 branch-holding
    worktrees, 7 of them entirely on trunk under rebased shas.

    BOTH ARMS or the predicate is unproven: landed-under-a-different-sha must
    RETIRE, and not-landed must KEEP.
    """

    def _land_rebased(self, lane):
        """Replay a lane's commit onto a MOVED main — the integrator's actual
        behavior. Returns (lane_tip, trunk_tip): different objects carrying
        identical patches."""
        branch = work.lane_branch(lane)
        lane_tip = _sh(self.root, "git", "rev-parse", branch).stdout.strip()
        with open(os.path.join(self.root, "trunk-moved.txt"), "w") as f:
            f.write("main advanced first, so the replay cannot fast-forward\n")
        _sh(self.root, "git", "add", "-A")
        self.assertEqual(
            _sh(self.root, "git", "commit", "-q", "-m", "trunk moved").returncode, 0)
        self.assertEqual(
            _sh(self.root, "git", "cherry-pick", lane_tip).returncode, 0)
        return lane_tip, _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()

    def _lane_commit(self, path, name):
        with open(os.path.join(path, name), "w") as f:
            f.write("lane work in %s\n" % name)
        _sh(path, "git", "add", "-A")
        self.assertEqual(
            _sh(path, "git", "commit", "-q", "-m", "work " + name).returncode, 0)

    def _patch_id(self, committish):
        show = _sh(self.root, "git", "show", committish)
        return subprocess.run(["git", "patch-id", "--stable"], cwd=self.root,
                              input=show.stdout, capture_output=True,
                              text=True, timeout=30).stdout.split()[0]

    def test_a_lane_landed_under_a_DIFFERENT_sha_is_retired_and_says_why(self):  # noqa: VACUOUS_ASSERTION — equal patch-ids, the exact PATCH_EQUIVALENT proof and the preserved-ref sha are the positive controls
        """ARM 1 — and the audit line, because a silent reap is
        indistinguishable from data loss."""
        path = self.room("rebased")
        self._lane_commit(path, "feature.txt")
        lane_tip, trunk_tip = self._land_rebased("rebased")

        # the owner's own measurement, reproduced: same content, different sha
        self.assertNotEqual(lane_tip, trunk_tip)
        self.assertEqual(self._patch_id(lane_tip), self._patch_id(trunk_tip))
        # ancestry is TRUTHFULLY negative — this is why nothing could reap it
        self.assertEqual(
            vcs.backend(self.root).ancestry(self.root, "lane/rebased", "main"),
            vcs.NOT_ANCESTOR)
        self.assertEqual(_work_gc._merge_state(self.root, "lane/rebased"),
                         vcs.PATCH_EQUIVALENT)

        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        self.assertEqual(row["proof"], vcs.PATCH_EQUIVALENT)
        self.assertIn("patch identity", row["why"])

        lines = work.gc_enact(self.root, row)
        blob = "\n".join(lines)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/rebased"))
        # SAY WHY, and say how to undo it: the audit names the proof and the
        # exact restore command, so the decision survives the deletion.
        self.assertIn("patch identity", blob)
        self.assertIn("rebased sha", blob)
        self.assertIn("git branch lane/rebased " + lane_tip, blob)
        # PRESERVED, NOT DELETED. Patch identity is a weaker grade of fact than
        # ancestry — it proves the DIFFS are upstream, not that these commit
        # objects survive anywhere — so the tip is demoted to a durable ref
        # instead of being made collectable. The sidebar clears; the work does
        # not move.
        keep_ref = _work_gc.RETIRED_NS + "lane/rebased"
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "--verify", "-q",
                keep_ref).stdout.strip(), lane_tip)
        self.assertIn(keep_ref, blob)
        # and the restore really works — the sha is not decorative
        self.assertEqual(
            _sh(self.root, "git", "branch", "lane/rebased",
                lane_tip).returncode, 0)
        self.assertTrue(work._has_branch(self.root, "lane/rebased"))

    def test_an_ancestry_retire_needs_no_retirement_ref(self):  # noqa: VACUOUS_ASSERTION — the exact ANCESTOR proof and 'ancestry' in the audit blob are the positive controls
        """The grades are DIFFERENT and the code shows it. After `-d` on an
        ancestry proof the tip is still on the trunk, so nothing is owed; the
        retirement ref exists only to pay for the weaker evidence."""
        path = self.room("ffland")
        self._lane_commit(path, "feature.txt")
        self.assertEqual(
            _sh(self.root, "git", "merge", "-q", "--ff-only",
                "lane/ffland").returncode, 0)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["proof"], vcs.ANCESTOR)
        blob = "\n".join(work.gc_enact(self.root, row))
        self.assertFalse(work._has_branch(self.root, "lane/ffland"))
        self.assertIn("ancestry", blob)
        self.assertEqual(
            _sh(self.root, "git", "for-each-ref", _work_gc.RETIRED_NS
                ).stdout.strip(), "")

    def test_a_failed_preservation_KEEPS_the_branch(self):  # noqa: VACUOUS_ASSERTION — has_branch True plus 'KEPT'/'retirement ref' in the lines are the positive controls
        """Save, verify the save, THEN delete. If the tip cannot be provably
        preserved, the deletion never happens — reversibility is a
        precondition, not a courtesy printed afterwards."""
        path = self.room("nosave")
        self._lane_commit(path, "feature.txt")
        self._land_rebased("nosave")
        real = vcs.GitVcs.text

        def refuse_update_ref(self_, cwd, *args, **kw):
            if args and args[0] == "update-ref":
                return 1, "", "simulated ref-store failure"
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", refuse_update_ref):
            lines = _work_gc._delete_lane_branch(self.root, "lane/nosave")
        self.assertTrue(work._has_branch(self.root, "lane/nosave"))
        self.assertIn("KEPT", "\n".join(lines))
        self.assertIn("retirement ref", "\n".join(lines))

    def test_a_read_back_lie_KEEPS_the_branch(self):  # noqa: VACUOUS_ASSERTION — has_branch True plus 'KEPT'/'did not read back' in the lines are the positive controls
        """The verify half of save-verify-delete, which the update-ref arm
        above does NOT cover: the write succeeds, but the read-back returns a
        DIFFERENT sha (a lying or torn ref store). kimi's reap review: the
        comparison is the only thing standing between a successful write and
        a forced delete, and removing it left every preservation test green —
        so this arm simulates the lie at exactly that step and asserts the
        delete never happens."""
        path = self.room("liesave")
        self._lane_commit(path, "feature.txt")
        self._land_rebased("liesave")
        real = vcs.GitVcs.text
        tip = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()

        def lying_read_back(self_, cwd, *args, **kw):
            if args[:2] == ("rev-parse", "--verify") and \
                    any("refs/helm-retired/" in a for a in args):
                return 0, "0" * 40 + "\n", ""        # a different sha
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", lying_read_back):
            lines = _work_gc._delete_lane_branch(self.root, "lane/liesave")
        self.assertTrue(work._has_branch(self.root, "lane/liesave"),
                        "a branch was -D'd on an UNVERIFIED preservation")
        joined = "\n".join(lines)
        self.assertIn("KEPT", joined)
        self.assertIn("did not read back", joined)
        self.assertIn(tip[:12], joined)

    def test_a_branch_that_ADVANCES_after_the_read_back_is_NOT_deleted(self):
        """THE THIRD ARM, and the only one that loses bytes. The two above
        simulate a ref store that fails or lies; this one lets the ref store
        work perfectly and moves the BRANCH instead.

        save-verify-delete proves things about the retirement REF and never
        bound the DELETE to the sha it proved. `git branch -D` removes whatever
        the ref points at when it runs — measured 2026-08-04 in a scratch repo,
        it reported deleting the branch at its ADVANCED sha, for a decision
        taken about the earlier one. So a seat committing into its room
        between the read-back
        and the delete had that commit deleted while the retirement ref
        preserved the tip BEFORE it: the new work was referenced by nothing and
        the audit line still said PRESERVED.

        The fix is `git update-ref -d <ref> <expected>`, git's own
        compare-and-delete, so what gets deleted is exactly what was proven
        saved or nothing is. Here the branch advances with REAL content (an
        empty commit would be no loss and reads UNKNOWN anyway, per the test
        below).

        THE ASSERTION IS THE ISSUED ARGV, NOT THE SURVIVING BRANCH, and that
        distinction cost a mutation round: this room's branch is CHECKED OUT in
        a worktree, and git refuses to delete a checked-out branch whatever the
        caller asks. So "the branch is still there" passed identically with the
        fix reverted — a true effect with an unrelated cause. What discriminates
        is that a COMPARE-AND-DELETE was issued against the preserved sha, so
        that is what this pins."""
        path = self.room("racer")
        self._lane_commit(path, "feature.txt")
        self._land_rebased("racer")
        real = vcs.GitVcs.text
        raced, argvs = {}, []

        def advance_the_branch_mid_preserve(self_, cwd, *args, **kw):
            argvs.append(tuple(args))
            out = real(self_, cwd, *args, **kw)
            # Fire once, AFTER the retirement ref is written and verified —
            # i.e. at the last instant where every existing guard is satisfied.
            if args and args[0] == "update-ref" and not raced:
                raced["before"] = _sh(path, "git", "rev-parse",
                                      "HEAD").stdout.strip()
                with open(os.path.join(path, "arrived-late.txt"), "w") as fh:
                    fh.write("work a seat committed after the read-back\n")
                _sh(path, "git", "add", "arrived-late.txt")
                _sh(path, "git", "commit", "-q", "-m", "after the read-back")
                raced["after"] = _sh(path, "git", "rev-parse",
                                     "HEAD").stdout.strip()
            return out

        with mock.patch.object(vcs.GitVcs, "text",
                               advance_the_branch_mid_preserve):
            lines = _work_gc._delete_lane_branch(self.root, "lane/racer")
        # POSITIVE CONTROL: the race must actually have been staged, or this
        # test proves nothing about a window it never opened.
        self.assertTrue(raced, "the interposition never fired — no race staged")
        self.assertNotEqual(raced["before"], raced["after"])
        preserved = raced["before"]
        # THE DISCRIMINATOR: a compare-and-delete naming the sha that was
        # preserved. An unconditional `branch -D` cannot produce this argv.
        self.assertIn(
            ("update-ref", "-d", "refs/heads/lane/racer", preserved), argvs,
            "the delete was not a compare-and-delete against the preserved "
            "sha — issued argv: %r" % (argvs,))
        self.assertNotIn(
            ("branch", "-D", "lane/racer"), argvs,
            "an unconditional -D was issued despite a preserved sha")
        self.assertTrue(work._has_branch(self.root, "lane/racer"),
                        "a branch that MOVED after the read-back was deleted")
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "lane/racer").stdout.strip(),
            raced["after"],
            "the branch survived but not at the tip the race created")
        self.assertIn("KEPT", "\n".join(lines))

    def test_an_EMPTY_commit_is_never_patch_identity_evidence(self):  # noqa: VACUOUS_ASSERTION — cherry asserted to start with '-' and the exact UNKNOWN verdict are the positive controls
        """An empty diff has an empty patch-id, so EVERY empty commit is
        "patch-identical" to every other one. Measured 2026-08-03: a lane
        carrying one `--allow-empty` marker read '-' against a trunk whose only
        empty commit was totally unrelated. No bytes are at risk there, but the
        VERDICT is false and a false landed verdict is the input to a deletion.
        Patch identity supplies no evidence at all on an empty diff, so the
        range is UNKNOWN — which keeps."""
        path = self.room("emptymark")
        self.assertEqual(
            _sh(path, "git", "commit", "-q", "--allow-empty",
                "-m", "lane marker, no files").returncode, 0)
        self.assertEqual(
            _sh(self.root, "git", "commit", "-q", "--allow-empty",
                "-m", "TOTALLY UNRELATED trunk marker").returncode, 0)
        # cherry is fooled...
        self.assertTrue(_sh(self.root, "git", "cherry", "main",
                            "lane/emptymark").stdout.startswith("-"))
        # ...and the predicate is not
        self.assertEqual(_work_gc._merge_state(self.root, "lane/emptymark"),
                         vcs.UNKNOWN)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "triage")
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/emptymark"))

    def test_a_lane_whose_content_did_NOT_land_is_KEPT_with_its_reason(self):  # noqa: VACUOUS_ASSERTION — the exact NOT_ANCESTOR verdict, the triage row and its 'NOT landed' reason are the positive controls
        """ARM 2. Without it, `return PATCH_EQUIVALENT` would pass arm 1."""
        path = self.room("unlanded")
        self._lane_commit(path, "never-landed.txt")
        with open(os.path.join(self.root, "elsewhere.txt"), "w") as f:
            f.write("unrelated trunk motion\n")
        _sh(self.root, "git", "add", "-A")
        _sh(self.root, "git", "commit", "-q", "-m", "unrelated")

        self.assertEqual(_work_gc._merge_state(self.root, "lane/unlanded"),
                         vcs.NOT_ANCESTOR)
        self.assertFalse(work._merged(self.root, "lane/unlanded"))
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "triage")
        self.assertIn("NOT landed", row["why"])
        work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/unlanded"))

    def test_a_LIVE_LEASE_outranks_content_identity(self):  # noqa: VACUOUS_ASSERTION — PATCH_EQUIVALENT plus the 'lease live' keep reason are the positive controls
        """Someone mid-work holds a deliberately dirty tree, and "dirty and
        quiet" is a NORMAL working state whose meaning is knowable only from
        the worker's side. A held lease keeps the room no matter how completely
        the content landed."""
        rc, out, _e = self.work("claim", "held", "--seat", "s1")
        self.assertEqual(rc, 0)
        path = out.split("\t")[0]
        self._lane_commit(path, "feature.txt")
        self._land_rebased("held")
        self.assertEqual(_work_gc._merge_state(self.root, "lane/held"),
                         vcs.PATCH_EQUIVALENT)      # fully landed by content
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("lease live", row["why"])
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/held"))

    def test_a_worktree_LESS_branch_is_the_surface_the_owner_sees(self):  # noqa: VACUOUS_ASSERTION — the delete/keep verdicts, the PATCH_EQUIVALENT state and the restore line are the positive controls
        """Branches OUTLIVE their rooms — 107 branches against 24 rooms on the
        live box — so the reaper that matters most to a sidebar is envtidy's
        orphan-branch pass, which asked the same ancestry-only question."""
        from helm import envtidy
        path = self.room("orphaned")
        self._lane_commit(path, "feature.txt")
        lane_tip, _trunk = self._land_rebased("orphaned")
        keep = self.room("stillopen")
        self._lane_commit(keep, "unlanded.txt")
        # retire the ROOMS only; the branches remain (the real shape)
        for p in (path, keep):
            self.assertEqual(
                vcs.backend(self.root).remove_worktree(self.root, p)[0], 0)

        rows = {r["branch"]: r for r in
                envtidy._orphan_branches(self.root, "main")}
        self.assertEqual(rows["lane/orphaned"]["verdict"], "delete")
        self.assertEqual(rows["lane/orphaned"]["state"], vcs.PATCH_EQUIVALENT)
        self.assertEqual(rows["lane/stillopen"]["verdict"], "keep")
        self.assertIn("NOT landed", rows["lane/stillopen"]["why"])

        result = envtidy.worktree_gc(root=self.root, apply=True)
        self.assertFalse(work._has_branch(self.root, "lane/orphaned"))
        self.assertTrue(work._has_branch(self.root, "lane/stillopen"))
        self.assertIn("git branch lane/orphaned " + lane_tip,
                      "\n".join(result["orphan_lines"]["lane/orphaned"]))

    def test_release_retires_a_rebased_lane_instead_of_stranding_it(self):  # noqa: VACUOUS_ASSERTION — rc 0 plus 'patch identity' and the restore line in the release output are the positive controls
        """The agent's OWN exit — where the accumulation starts. The seat did
        the right thing at release, ancestry said no, and the branch stayed.
        Now the release retires it AND names the proof that authorized it."""
        rc, out, _e = self.work("claim", "selfclean", "--seat", "s1")
        self.assertEqual(rc, 0)
        path, lease = out.split("\t")[0], out.split("\t")[2]
        self._lane_commit(path, "feature.txt")
        self._land_rebased("selfclean")
        rc, out, _err = self.work("release", "selfclean", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/selfclean"))
        self.assertIn("patch identity", out)
        self.assertIn("restore: git branch lane/selfclean", out)


class GcDetachedIsManualOnlyTest(WorkBase):
    """helm REFUSES to auto-reap a room it cannot prove is safe.

    THIS CLASS REPLACES A REACHABILITY PROVER THAT FAILED FIVE REVIEW ROUNDS,
    and the history is the justification, so it is recorded rather than lost:
      r1  HEAD only            -> missed a tip reset away into ORIG_HEAD
      r2  seven-name pseudorefs-> missed FETCH_HEAD, MERGE_AUTOSTASH, rewritten
      r3  --include-root-refs  -> blind to MULTI-VALUED FETCH_HEAD/MERGE_HEAD
      r4  read the admin dir   -> truncates large files, misses abbreviated and
                                  uppercase shas, follows symlinks, blocks on
                                  FIFOs, and CANNOT CLOSE THE SCAN/REMOVE RACE
    That last one is not patchable: there is always a window between judging a
    room safe and deleting it. Five rounds of a per-case handler is the signal
    to stop handling cases.

    A BRANCH room needs no prover — `delete_branch` is `branch -d` and GIT
    refuses an unmerged branch, so the guarantee comes from the tool that owns
    the objects. A DETACHED room has no equivalent, so helm keeps it and says
    why. Debris that is VISIBLE and named beats debris silently deleted."""

    def detached_room(self, name):
        path = self.room(name)
        _sh(path, "git", "checkout", "--detach", "-q")
        with open(os.path.join(path, "u.txt"), "w") as f:
            f.write("work with no branch to protect it\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "-m", "detached work")
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def test_a_detached_room_is_KEEP_and_manual_only(self):
        path = self.detached_room("detachd")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep")
        self.assertTrue(row["manual_only"])
        self.assertIn("DETACHED", row["why"])

    def test_the_reason_names_HEAD_so_a_human_can_inspect(self):
        """A refusal that does not say where to look just moves the problem."""
        path = self.detached_room("wherelook")
        head = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["head"], head)
        self.assertIn(head[:12], row["why"])

    def test_apply_NEVER_removes_a_detached_room(self):
        """THE LOAD-BEARING ONE. Before the re-scope this room was verdicted
        `remove`, and five detached rooms in the real repo held eight commits
        reachable from nothing."""
        path = self.detached_room("survive")
        head = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path), "a detached room was reaped: %s" % out)
        self.assertEqual(
            _sh(self.root, "git", "cat-file", "-t", head).stdout.strip(),
            "commit", "the detached commit did not survive")

    def test_a_room_MID_OPERATION_is_manual_only_even_on_a_branch(self):
        """Sequencer state is neither a commit nor a branch, so neither `-d`
        nor any walk speaks for it. codex named this beside the detached case
        and it gets the same answer."""
        path = self.room("midmerge")
        admin = _sh(path, "git", "rev-parse", "--absolute-git-dir").stdout.strip()
        with open(os.path.join(admin, "MERGE_HEAD"), "w") as f:
            f.write(_sh(path, "git", "rev-parse", "HEAD").stdout.strip() + "\n")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep")
        self.assertTrue(row["manual_only"])
        self.assertIn("mid-merge", row["why"])

    def test_an_unreadable_gitdir_keeps_the_room(self):
        path = self.detached_room("unreadable")
        real = vcs.backend(path).text

        def broken(p, *args, **kw):
            if args[:2] == ("rev-parse", "--absolute-git-dir"):
                return 1, "", "simulated failure"
            return real(p, *args, **kw)
        with mock.patch.object(vcs.backend(path), "text", side_effect=broken):
            state = _work_gc._operation_state(path)
        self.assertEqual(state, "unreadable-gitdir",
                         "an unreadable git dir must read as in-an-operation")

    def test_a_room_that_DETACHES_between_scan_and_enact_is_not_reaped(self):
        """codex, on the attached path I had claimed was proven.

        `branch -d` guarantees the BRANCH is merged. It says nothing about where
        HEAD went. A room on a branch at scan can detach and commit before
        enact; removal then destroys that commit while `-d` cheerfully succeeds,
        because the branch really is merged — it simply is not where the work
        is. So the scan's premise is re-read immediately before removal.

        This SHRINKS the window, it does not close it. Closing it needs shared
        serialization across every remover, which is a different lane."""
        path = self.room("racy")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        # the room moves AFTER the scan
        _sh(path, "git", "checkout", "--detach", "-q")
        _sh(path, "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "committed after the scan")
        doomed = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(path),
                        "removed a room that detached under the scan: %s" % lines)
        self.assertTrue(any("moved under the scan" in l for l in lines), lines)
        self.assertEqual(
            _sh(self.root, "git", "cat-file", "-t", doomed).stdout.strip(),
            "commit", "the post-scan commit was destroyed")

    def test_a_DIRTY_DETACHED_room_is_manual_only_not_rescued(self):
        """codex r6, and the most dangerous room there is: uncommitted bytes AND
        no branch to hold them.

        `_dirty` was checked BEFORE the manual-only gate, so this room verdicted
        rescue with branch=None; enact then wip-committed onto a DETACHED HEAD
        and removed the room, destroying the commit the rescue had just made.
        The refusal existed and the hazardous input routed around it."""
        path = self.detached_room("dirtydet")
        with open(os.path.join(path, "uncommitted.txt"), "w") as f:
            f.write("bytes with no branch to hold them\n")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep",
                         "a dirty DETACHED room was queued for rescue+removal")
        self.assertTrue(row["manual_only"])
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path), "removed it anyway: %s" % out)
        self.assertTrue(os.path.exists(os.path.join(path, "uncommitted.txt")),
                        "the uncommitted bytes are gone")

    def test_a_DIRTY_room_ON_A_BRANCH_still_rescues(self):
        """The reorder must not disable the lost-and-found for the case it was
        built for: dirty bytes on a real branch still get wip-committed."""
        path = self.room("dirtybr", dirty="junk.txt")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "rescue")
        self.assertFalse(row.get("manual_only"))
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(
            _sh(self.root, "git", "show", "lane/dirtybr:junk.txt").stdout,
            "precious uncommitted bytes\n", "the bytes were not rescued")
        self.assertIn("room kept", out)

    def test_a_BRANCH_room_still_reaps_because_git_itself_proves_it(self):
        """The re-scope must not stop gc doing the job it CAN prove. A guard
        that always fires protects nothing — it just accumulates debris behind
        a refusal."""
        path = self.room("branchok")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        self.assertFalse(row.get("manual_only"))
        rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))


class GcPhantomRecordTest(WorkBase):
    """A registered worktree whose DIRECTORY is gone is registry residue: git
    keeps the record indefinitely, `worktree add` refuses the path, and a
    metaharness that mirrors the registry (Orca's sidebar) renders a ghost
    room forever. Measured live 2026-08-01: two dregg records outlived their
    dirs — one a session scratchpad wiped by scratch lifecycle — and sat in
    the owner's sidebar as dead entries. gc's apply pass prunes the RECORD
    only; files and branches are never in reach."""

    def phantom(self, lane):
        """A room whose dir dies WITHOUT `git worktree remove` — the reboot/
        rm -rf/scratch-reap shape that mints the residue. Asserts the record
        IS registered after the dir dies: the positive control every later
        absence reading rests on — a `registered` probe that could never
        return True would vacuously green the prune assertions."""
        path = self.room(lane)
        shutil.rmtree(path)
        self.assertTrue(self.registered(path),
                        "fixture failed to mint a registered phantom")
        return path

    def registered(self, path):
        rows, error = vcs.backend(self.root).worktrees(self.root)
        self.assertIsNone(error)
        return any(r["path"] == path for r in rows)

    def test_an_UNREADABLE_dir_is_NEVER_a_phantom(self):
        """codex-2's FIX, and the one that could have deleted a live record:
        "unreadable live dirs prune". os.path.isdir SWALLOWS OSError and answers
        False, so a directory that EXISTS but cannot be stat'd — permission
        denied, a hanging NFS mount, a failing disk — was indistinguishable from
        one that is gone, and the enact leg pruned its registry record. Prune is
        irreversible registry loss, so this is the one direction that may never
        fail open.

        The control comes FIRST and unconditionally: a genuinely-absent dir IS
        still detected as phantom in the same pass. Without it, a predicate that
        simply stopped detecting anything would green this arm."""
        gone = self.phantom("really-gone")
        live = self.room("unreadable-but-live")
        self.assertTrue(self.registered(live))
        real = os.stat

        def blind(path, *a, **k):
            if str(path) == live:
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **k)

        with mock.patch.object(os, "stat", side_effect=blind):
            found = _work_gc.phantom_records(self.root)
        self.assertIn(gone, found,
                      "the detector stopped seeing a REAL phantom — this arm "
                      "would then pass for the wrong reason")
        self.assertNotIn(live, found,
                         "an unreadable LIVE directory was classed phantom; "
                         "--apply would have deleted its registry record")

    def test_apply_targets_ONLY_the_approved_phantom_not_unreadable_live(self):
        """The load-bearing regression: one real phantom makes the apply leg
        run while an unreadable live control shares the registry. The old global
        `worktree prune` could delete both despite detecting only the first."""
        gone = self.phantom("approved-gone")
        live = self.room("excluded-unreadable-live")
        real = os.stat

        def blind(path, *a, **k):
            if str(path) == live:
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **k)

        backend = vcs.backend(self.root)
        with mock.patch.object(os, "stat", side_effect=blind), \
                mock.patch.object(backend, "remove_worktree_record",
                                  wraps=backend.remove_worktree_record) as remove, \
                mock.patch.object(backend, "text", wraps=backend.text) as text:
            approved = _work_gc.phantom_records(self.root)
            removed, error, unknown = _work_gc.prune_phantom_records(self.root, approved)
        self.assertEqual(approved, [gone])
        self.assertEqual((removed, error, unknown), ([gone], None, False))
        remove.assert_called_once_with(self.root, gone)
        self.assertFalse(any(call.args[1:] == ("worktree", "prune")
                             for call in text.call_args_list),
                         "the targeted actuator fell back to global prune")
        self.assertTrue(self.registered(live),
                        "an excluded unreadable-live record was collateral loss")
        self.assertTrue(os.path.isdir(live))

    def test_postcheck_reports_if_an_EXCLUDED_record_disappears(self):  # noqa: VACUOUS_ASSERTION — approved/excluded controls are positively identified and the collateral-loss diagnostic proves the synthetic post-state was consumed
        """The excluded control is part of the postcondition, not merely a scan
        assertion. A future widened actuator must fail loudly even if every
        approved candidate also disappeared as requested."""
        gone = self.phantom("approved-control")
        live = self.room("excluded-control")
        approved, excluded, error = _work_gc.phantom_scan(self.root)
        self.assertIsNone(error)
        self.assertEqual(approved, [gone])
        self.assertIn(live, excluded)
        before, error = vcs.backend(self.root).worktrees(self.root)
        self.assertIsNone(error)
        after = [r for r in before if r["path"] not in (gone, live)]
        with mock.patch.object(_work_gc.vcs, "backend") as be:
            be.return_value.worktrees.side_effect = [(before, None), (after, None)]
            be.return_value.remove_worktree_record.return_value = (0, "", "")
            removed, error, unknown = _work_gc.prune_phantom_records(
                self.root, approved, excluded=excluded)
        self.assertEqual(removed, [gone])
        self.assertIsNotNone(error)
        self.assertIn("excluded record(s) disappeared", error)
        self.assertFalse(unknown)

    def test_a_PARTIAL_targeted_remove_reports_BOTH_halves(self):
        """Each approved record is independent: one success and one refusal
        report both the observed removal and the residue."""
        a = self.phantom("leaves-ok")
        b = self.phantom("stays-behind")
        rows = lambda *paths: ([{"path": p, "locked": False} for p in paths], None)
        with mock.patch.object(_work_gc.vcs, "backend") as be:
            be.return_value.worktrees.side_effect = [rows(a, b), rows(b), rows(b)]
            be.return_value.remove_worktree_record.side_effect = [
                (0, "", ""), (1, "", "simulated refusal")]
            pruned, error, unknown = _work_gc.prune_phantom_records(self.root, [a, b])
        self.assertEqual(pruned, [a], "the record that DID leave was not reported")
        self.assertIsNotNone(error, "residue reported as a clean pass")
        self.assertIn("remain registered", error)
        self.assertFalse(unknown)

    def test_a_REVIVED_path_is_reauthorized_and_kept(self):  # noqa: VACUOUS_ASSERTION — the recreated directory and deterministic refusal diagnostics prove reauthorization ran before the no-removal assertions
        """Another actor may recreate the directory after the scan. The apply
        leg re-checks the proof and never calls Git for the revived record."""
        path = self.phantom("revived-during-prune")
        os.makedirs(path)
        with mock.patch.object(_work_gc.vcs.backend(self.root),
                               "remove_worktree_record") as remove:
            pruned, error, unknown = _work_gc.prune_phantom_records(self.root, [path])
        remove.assert_not_called()
        self.assertEqual(pruned, [],
                         "a still-registered path was reported removed")
        self.assertIsNotNone(error)
        self.assertIn("remain registered", error)
        self.assertFalse(unknown)

    def test_an_UNREADABLE_post_prune_registry_reports_UNKNOWN(self):
        """A failed verifier cannot prove what git removed. phantom_records()
        deliberately maps an unreadable registry to [], which is safe for
        authorization and unsafe for postcondition evidence: [] there means
        CANNOT SEE, not every record is gone."""
        path = self.phantom("unreadable-after-prune")
        with mock.patch.object(_work_gc.vcs, "backend") as be:
            be.return_value.worktrees.side_effect = [
                ([{"path": path, "locked": False}], None),
                ([], "simulated post-prune registry failure")]
            be.return_value.remove_worktree_record.return_value = (0, "", "")
            pruned, error, unknown = _work_gc.prune_phantom_records(self.root, [path])
        self.assertEqual(pruned, [],
                         "an unreadable verifier minted removal evidence")
        self.assertIsNotNone(error)
        self.assertIn("removal is UNKNOWN", error)
        self.assertTrue(unknown)

    def test_a_CONTROL_in_a_path_cannot_forge_terminal_output(self):
        """codex-2's fourth finding began with newlines, but the boundary is
        terminal printability. A raw ESC can clear or rewrite the same line
        without any newline, so a four-character control allowlist is not the
        class-level fix."""
        from helm.work import _cli
        hostile = (
            "/tmp/x\n  phantom record no longer registered /etc/passwd",
            "/tmp/x\x1b[2Jforged",
            "/tmp/x" + chr(0x202e) + "forged",
        )
        for path in hostile:
            with self.subTest(path=repr(path)):
                shown = _cli._show(path)
                self.assertTrue(shown.isprintable(),
                                "a terminal control survived the renderer")
                self.assertNotEqual(shown, path,
                                    "the hostile path was passed through raw")
                self.assertIn("forged" if "forged" in path else "passwd", shown,
                              "escaping destroyed the path's identifying text")
        # CONTROL: an ordinary path is passed through untouched, so the escape
        # is not simply mangling every path it sees.
        self.assertEqual(_cli._show("/tmp/ordinary/path"), "/tmp/ordinary/path")

    def test_dry_run_NAMES_the_phantom_and_keeps_the_record(self):
        path = self.phantom("ghostrec")
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        self.assertIn("PHANTOM %s" % path, out)
        self.assertTrue(self.registered(path),
                        "the dry run must not touch the registry")

    def test_apply_prunes_the_record_and_NEVER_the_branch(self):  # noqa: VACUOUS_ASSERTION — the fixture asserts the record exists before apply, the observational removal line fires, and the branch remains as the opposite-direction control
        path = self.phantom("deadroom")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("phantom record no longer registered %s" % path, out)
        self.assertFalse(self.registered(path), "the record must be gone")
        # The branch is git's lossless authority over the commits and prune
        # never speaks for it — unlanded work stays recoverable by re-add.
        self.assertTrue(work._has_branch(self.root, "lane/deadroom"))

    def test_UNKNOWN_phantom_result_posts_no_known_summary(self):
        self.phantom("unknown-record")
        with mock.patch.object(_work_cli, "prune_phantom_records",
                               return_value=([], "removal is UNKNOWN", True)):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("removal is UNKNOWN", err)
        self.assertNotIn("phantom records kept", err)
        self.assertNotIn("removed=", out,
                         "an unknown verifier posted a known owner summary")

    def test_failed_phantom_remove_is_not_counted_as_removed(self):
        path = self.phantom("refused-record")
        backend = vcs.backend(self.root)
        with mock.patch.object(backend, "remove_worktree_record",
                               return_value=(1, "", "simulated refusal")):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("simulated refusal", err)
        self.assertIn("removed=0 kept=1 triage=1", out)
        self.assertTrue(self.registered(path))

    def test_OUT_OF_TREE_phantom_belongs_to_worktree_gc(self):  # noqa: VACUOUS_ASSERTION — real Git registration is asserted before and after apply, positively proving the ownership-exclusion path preserved the record
        """`helm work gc` owns lane/harness rooms only. A detached scratchpad
        in the remaining estate stays for the composing `helm worktree gc`."""
        path = os.path.join(self.tmp, "scratch-land")
        r = subprocess.run(["git", "worktree", "add", "-q", "--detach", path],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.rmtree(path)
        self.assertTrue(self.registered(path),
                        "fixture failed to mint a registered phantom")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("no lane rooms", out)
        self.assertNotIn("phantom record no longer registered %s" % path, out)
        self.assertTrue(self.registered(path),
                        "work gc crossed its lane/harness ownership boundary")

    def test_a_LOCKED_record_is_immune_even_with_the_dir_gone(self):  # noqa: VACUOUS_ASSERTION — an unlocked control phantom is asserted PRESENT in the same dry-run and apply outputs the locked one must be absent from; the absences read against a proven-live detector
        """A locked room on an absent path is an owner's deliberate state
        (an unmounted disk), not residue — git's own prune rule, kept.

        The DRY-RUN assertion is the load-bearing one: git itself skips
        locked records on apply, so only the report can lie. Dropping the
        exclusion in `phantom_records` would print 'PHANTOM ... --apply
        prunes the record' — a promise apply then breaks."""
        path = self.room("lockedgone")
        _sh(self.root, "git", "worktree", "lock", path,
            "--reason", "on the usb drive")
        shutil.rmtree(path)
        # POSITIVE CONTROL ON THE SAME OBSERVABLES: an unlocked phantom in the
        # same registry IS seen and IS pruned — proving the detector and both
        # output readings can fire — while the locked one is spared.
        control = self.phantom("unlockedctrl")
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        self.assertIn("PHANTOM %s" % control, out)
        self.assertNotIn("PHANTOM %s" % path, out,
                         "the dry run promised a prune git will refuse")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("phantom record no longer registered %s" % control, out)
        self.assertNotIn("phantom record no longer registered %s" % path, out)
        self.assertFalse(self.registered(control))
        self.assertTrue(self.registered(path),
                        "a locked record must survive the prune")

    def test_an_unreadable_registry_reads_as_NO_phantoms(self):
        """'Cannot see' must never become a prune authorization."""
        path = self.phantom("blindfold")
        self.assertEqual(_work_gc.phantom_records(self.root), [path],
                         "positive control: a readable registry names it")
        with mock.patch.object(vcs.backend(self.root), "worktrees",
                               return_value=([], "simulated registry failure")):
            self.assertEqual(_work_gc.phantom_records(self.root), [])


class TrunkSyncTest(WorkBase):
    """The shared checkout itself rides the cadence (the 2026-08-03 fork:
    a local-only commit raced a land and every seat measured trunk on the
    stale side for ~80 minutes). BEHIND converges by ff; AHEAD and a FORK
    are NAMED; a fork is posted to the room; nothing here pushes, rebases,
    or repairs a fork."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        r = _sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main",
                self.origin)
        self.assertEqual(r.returncode, 0, r.stderr)
        _sh(self.root, "git", "remote", "add", "origin", self.origin)
        r = _sh(self.root, "git", "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(self.root, "git", "fetch", "-q", "origin")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _sha(self, ref="main"):
        return _sh(self.root, "git", "rev-parse", ref).stdout.strip()

    def _land_on_origin(self, fname, content="landed elsewhere\n"):
        """A land performed by the REST OF THE FLEET: clone, commit, push."""
        clone = os.path.join(self.tmp, "clone-" + fname)
        r = _sh(self.tmp, "git", "clone", "-q", self.origin, clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in (("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            _sh(clone, *cmd)
        with open(os.path.join(clone, fname), "w") as f:
            f.write(content)
        _sh(clone, "git", "add", "-A")
        r = _sh(clone, "git", "commit", "-q", "-m", "land " + fname)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(clone, "git", "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _commit_local(self, fname):
        """The stray direct commit — the incident's generator."""
        with open(os.path.join(self.root, fname), "w") as f:
            f.write("stray direct commit\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "stray " + fname)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_in_sync_says_nothing(self):
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("refreshed origin", out,
                      "positive control: the pass demonstrably ran and got "
                      "past the fetch — silence below is then a verdict")
        self.assertNotIn("TRUNK-", out)

    # noqa: VACUOUS_ASSERTION — assertNotEqual(before, after) is the MOVED
    # claim, paired with assertEqual(after, origin/main) on the same ref and
    # a file-exists control; M1 kills this test 6 ways.
    def test_behind_apply_fast_forwards_the_shared_base(self):
        self._land_on_origin("landed.txt")
        before = self._sha()
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-SYNC fast-forwarded", out)
        after = self._sha()
        self.assertNotEqual(before, after, "positive control: main must move")
        self.assertEqual(after, self._sha("origin/main"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "landed.txt")),
                        "the ff must carry the working tree with it")

    # noqa: VACUOUS_ASSERTION — restraint IS the behavior under test; the
    # positive control on the same sha/file observables is the apply arm
    # (test_behind_apply_...), same fixture, and assertIn(TRUNK-BEHIND)
    # proves this arm saw the state it declined to act on. M3 kills it.
    def test_behind_dry_run_names_the_ff_and_moves_nothing(self):
        self._land_on_origin("landed.txt")
        _sh(self.root, "git", "fetch", "-q", "origin")  # dry run never fetches
        before = self._sha()
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-BEHIND", out)
        self.assertEqual(self._sha(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "landed.txt")))

    # noqa: VACUOUS_ASSERTION — the unchanged-sha claim rides beside two
    # unconditional positives on the same observables: the refusal line in
    # out, and the byte-exact read of the dirty edit the ff must not eat.
    def test_behind_with_a_conflicting_dirty_edit_keeps_gits_words(self):
        self._land_on_origin("README", "rewritten on the trunk\n")
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("dirty local edit\n")
        before = self._sha()
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-BEHIND kept: fast-forward refused", out)
        self.assertEqual(self._sha(), before)
        with open(os.path.join(self.root, "README")) as f:
            self.assertEqual(f.read(), "dirty local edit\n",
                             "the refused ff must not eat the dirty edit")

    # noqa: VACUOUS_ASSERTION — never-pushed is the behavior under test;
    # assertIn(TRUNK-AHEAD) + the named commit prove the pass saw the state,
    # and the origin tip is read from the BARE repo, not this checkout.
    # M4 (ahead-goes-silent) kills it.
    def test_ahead_is_named_and_never_pushed(self):
        self._commit_local("stray.txt")
        origin_before = self._sha("origin/main")
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-AHEAD", out)
        self.assertIn("1 unpushed commit", out)
        self.assertIn("stray", out)                    # names the commit
        remote_tip = _sh(self.origin, "git", "rev-parse",
                         "main").stdout.strip()
        self.assertEqual(remote_tip, origin_before,
                         "gc must never publish local work")

    # noqa: VACUOUS_ASSERTION — left-alone is the behavior under test; the
    # unconditional positives on the same pass are assertIn(TRUNK-DIVERGED)
    # and exactly-one FORKED room post. M2 (post-dropped) kills it.
    def test_a_fork_is_named_posted_and_left_alone(self):
        self._commit_local("stray.txt")
        self._land_on_origin("landed.txt")
        before = self._sha()
        with mock.patch("helm.chat.post") as posted:
            rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-DIVERGED", out)
        self.assertEqual(self._sha(), before, "a fork is never auto-repaired")
        forked = [c for c in posted.call_args_list if "FORKED" in c[0][0]]
        self.assertEqual(len(forked), 1,
                         "the fork must reach the room exactly once per pass")

    # noqa: VACUOUS_ASSERTION — not-posted is the behavior under test; the
    # posted arm (test_a_fork_is_named_posted_...) is the positive control
    # on the same chat.post observable, and assertIn(TRUNK-DIVERGED) proves
    # this arm saw the fork it declined to broadcast.
    def test_a_fork_in_the_dry_run_is_named_but_not_posted(self):
        self._commit_local("stray.txt")
        self._land_on_origin("landed.txt")
        _sh(self.root, "git", "fetch", "-q", "origin")
        with mock.patch("helm.chat.post") as posted:
            rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-DIVERGED", out)
        posted.assert_not_called()

    # noqa: VACUOUS_ASSERTION — kept is the behavior under test; the
    # unconditional positives are len(lines)==1 and the HEAD reason text
    # on the same return value the absence claim reads.
    def test_head_off_the_base_is_kept_with_the_reason(self):
        r = _sh(self.root, "git", "checkout", "-q", "--detach")
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self._sha("HEAD")
        lines, err = _work_gc.trunk_sync(self.root, enforcing=True)
        self.assertIsNone(err)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("HEAD is", lines[0])
        self.assertEqual(self._sha("HEAD"), before)

    def test_no_remote_is_silence(self):
        solo = os.path.join(self.tmp, "solo")
        os.makedirs(solo)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(solo, *cmd).returncode, 0)
        with open(os.path.join(solo, "f"), "w") as f:
            f.write("x\n")
        _sh(solo, "git", "add", "-A")
        _sh(solo, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(_work_gc.trunk_sync(solo, enforcing=True),
                         ([], None))


class ListTest(WorkBase):
    def test_list_joins_registry_with_claims(self):
        rc, _out, err = self.work("claim", "webui", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray", dirty="j.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        row_w = next(ln for ln in out.splitlines() if "webui" in ln)
        # the duration says WHICH DIRECTION it runs — see
        # test_the_lease_duration_says_which_direction_it_runs
        self.assertRegex(row_w, r"webui\s+s1 \d+s left\s+clean")
        row_s = next(ln for ln in out.splitlines() if "stray" in ln)
        self.assertRegex(row_s, r"stray\s+-\s+dirty")

    def test_the_lease_duration_says_which_direction_it_runs(self):
        """A BARE DURATION READS BOTH WAYS, and two seats proved it.

        The column rendered "<seat> 13378s", which is equally natural as
        "held for" and as "left" — and the field behind it is literally named
        `remaining`. On 2026-08-05 two seats misread it the same way inside
        an hour: one nearly raised a false alarm on their own lane, and I
        built a lease-expiry policy ask on top of it. Same misreading, two
        readers, one hour — that is a surface defect, not carelessness.

        This same file already wrote "%ds ago" for the other direction, so
        the convention existed and this column had simply missed it."""
        rc, _out, err = self.work("claim", "webui", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        row = next(ln for ln in out.splitlines() if "webui" in ln)

        self.assertIn("left", row,
                      "the duration is unlabelled and reads equally as age "
                      "or as remaining: %r" % row)
        self.assertRegex(row, r"\d+s left",
                         "the label is not attached to the number")
        # NEGATIVE CONTROL: an UNHELD row has no duration to label, and must
        # not grow a stray one — the holder column is what says unheld, and
        # that is the reading the whole finding rested on.
        self.room("stray", dirty="j.txt")
        rc, out, _err = self.work("list")
        row_s = next(ln for ln in out.splitlines() if "stray" in ln)
        self.assertNotIn("left", row_s,
                         "an unheld row grew a duration: %r" % row_s)
        self.assertRegex(row_s, r"stray\s+-\s+dirty")

    def test_list_empty(self):
        rc, out, _err = self.work("list")
        self.assertEqual(rc, 0)
        self.assertIn("no lane rooms", out)

    def test_two_held_lanes_on_one_file_are_surfaced(self):
        """THE QUESTION THE LEASE COLUMN CANNOT ANSWER.

        A lease guards a worktree+branch, so it answers 'is anyone else editing
        this ROOM' — never 'is anyone else fixing this DEFECT'. On 2026-07-30
        two seats held valid, uncontested leases on two lanes both rewriting
        tests/test_seats.py for the same red; the board printed two healthy
        rows and main stayed broken for hours. A human reading chat caught it.

        Both lanes commit a change to the SAME file, both leases live -> the
        board must say so."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            p = os.path.join(self.root + "-wt", lane)
            with open(os.path.join(p, "shared.py"), "w") as f:
                f.write("# %s\n" % lane)
            _sh(p, "git", "add", "-A")
            _sh(p, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", lane)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("OVERLAPPING LANES", out)
        self.assertIn("shared.py", out)
        self.assertIn("PROXY", out, "must not claim overlap proves same-defect")

    def test_overlap_survives_one_lane_already_being_on_the_trunk(self):
        """THE MERGE-BASE MUST BE THE PAIR'S, NEVER THE TRUNK'S — and this test
        exists because a mutation proved the other two did not bind it.

        Once a lane's work is reachable from the trunk (landed, cherry-picked,
        or merged-but-room-not-yet-reaped), merge-base(tip, trunk) IS the tip,
        so a trunk-based diff is EMPTY and the pair reports no overlap. Silent,
        confident, wrong — the same shape as every other failure this lane is
        about. Measured: the first live run of this detector scored BOTH known
        duplicate-work incidents clean for exactly this reason, because I
        diffed against the trunk instead of against the pair.

        alpha's commit is merged to the trunk; beta still holds its own. They
        share a file, both leases live -> the overlap must still surface."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            p = os.path.join(self.root + "-wt", lane)
            with open(os.path.join(p, "shared.py"), "w") as f:
                f.write("# %s\n" % lane)
            _sh(p, "git", "add", "-A")
            _sh(p, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", lane)
        # alpha lands on the trunk; its room and lease stay (the real window)
        _sh(self.root, "git", "-c", "user.name=t", "-c", "user.email=t@t",
            "merge", "--no-ff", "-q", "-m", "land alpha", "lane/alpha")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("OVERLAPPING LANES", out,
                      "a lane whose work reached the trunk still collides with "
                      "an open lane on the same file; diffing against the trunk "
                      "would silently report nothing")
        self.assertIn("shared.py", out)

    def test_overlap_survives_one_lane_fast_forwarded_to_trunk(self):  # noqa: VACUOUS_ASSERTION — FF ancestry, authored reflog, rendered overlap, and UNKNOWN fallback are all positive controls
        """REVIEW FINDING: first-parent alone is not authorship.

        An FF-landed lane tip is on trunk's first-parent line just like an
        untouched stale lane. Its own reflog still records the lane commit, so
        the empty own-diff must preserve the pair-base overlap."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            self._commit(os.path.join(self.root + "-wt", lane),
                         "shared.py", "# " + lane + "\n")
        alpha = _sh(self.root, "git", "rev-parse",
                    "lane/alpha").stdout.strip()
        _sh(self.root, "git", "merge", "--ff-only", "lane/alpha")
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head, alpha,
                         "fixture broken: alpha must fast-forward onto trunk")
        first_parent = _sh(self.root, "git", "rev-list", "--first-parent",
                           "HEAD").stdout.splitlines()
        self.assertIn(alpha, first_parent,
                      "fixture broken: FF lane tip must be first-parent")
        reflog = _sh(self.root, "git", "reflog", "show", "--format=%gs",
                     "lane/alpha").stdout
        self.assertIn("commit", reflog,
                      "fixture broken: alpha branch must record authorship")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("OVERLAPPING LANES", out)
        self.assertIn("shared.py", out)
        with mock.patch.object(_work_gc, "_branch_authored",
                               return_value=None):
            unknown = _work_gc.lane_overlaps(self.root)
        self.assertTrue(unknown,
                        "UNKNOWN branch biography must preserve the wide answer")
        self.assertIn("shared.py", unknown[0][2])

    def test_an_unheld_lane_is_not_racing_you(self):
        """NOISE CONTROL, and it is what makes the signal usable.

        Raw file overlap over every open room is O(N^2) and hub-dominated —
        measured on the live board it produced 26 pairs, most of them 'we both
        touched tests/test_seats.py', which is true of nearly every lane. A
        PARKED room's holder is gone and is not racing anyone. Same two lanes,
        same shared file, one lease released -> silent."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, out, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            lease = out.split("\t")[2].strip()
            p = os.path.join(self.root + "-wt", lane)
            with open(os.path.join(p, "shared.py"), "w") as f:
                f.write("# %s\n" % lane)
            _sh(p, "git", "add", "-A")
            _sh(p, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", lane)
            if lane == "beta":
                self.work("release", lane, "--lease", lease, "--seat", seat)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("OVERLAPPING LANES", out)

    def _commit(self, path, name, body):
        with open(os.path.join(path, name), "w") as f:
            f.write(body)
        _sh(path, "git", "add", "-A")
        _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", name + " in " + os.path.basename(path))

    def test_a_fresher_lane_does_not_own_the_trunk_commits_it_inherited(self):
        """THE FALSE-POSITIVE THE PAIR-BASE PRODUCES, and why the cure is not
        'diff against the trunk'.

        When two lanes sit on DIFFERENT trunk points, merge-base(a, b) is the
        OLDER base, so the fresher lane's diff against it contains every trunk
        commit it merely INHERITED by being rebased later. If the older lane
        happens to have authored a file the trunk also changed in that window,
        the board reports a collision on a file the fresher lane never touched.
        Measured on the live board 2026-08-03: 44 reported pairs, 26 of them
        sharing nothing but inherited landings.

        alpha authors shared.py. The TRUNK then changes shared.py. beta is cut
        from that newer trunk and touches only its own file. Their merge-base
        predates the trunk change, so beta 'inherits' shared.py and the pair
        reports it. beta never opened the file."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha's own work\n")
        # the trunk moves, touching the same file — a land by somebody else
        self._commit(self.root, "shared.py", "# a landed trunk change\n")
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "beta"),
                     "beta_only.py", "# beta's own work\n")
        # POSITIVE CONTROL, unconditional: beta really does inherit shared.py
        # through the pair-base, so a silent board below is the NARROWING
        # working and not a scan that found nothing.
        base = _sh(self.root, "git", "merge-base", "lane/alpha",
                   "lane/beta").stdout.strip()
        inherited = _sh(self.root, "git", "diff", "--name-only",
                        base + "..lane/beta").stdout
        self.assertIn("shared.py", inherited,
                      "fixture broken: beta must inherit shared.py through "
                      "the pair-base, or this tests nothing")
        self.assertIn("beta_only.py", inherited)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL ON THE SAME ROOT OBJECT, unconditional: the board
        # really did render both lanes, so the absence below is the narrowing
        # and not an empty listing.
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("shared.py", out,
                         "beta never touched shared.py — it inherited the "
                         "trunk commit that did")

    def test_a_lane_that_has_authored_nothing_is_racing_nobody(self):
        """A ROOM JUST CLAIMED SITS ON THE TRUNK TIP. Its pair-base diff is
        100% inherited, so without this it collides with everything that ever
        touched a busy file. The first cut of the narrowing treated its empty
        own-diff as 'cannot narrow' and handed back the un-narrowed set —
        which made the board WORSE than no narrowing at all (44/26 false
        became, for that shape, more pairs rather than fewer)."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        self._commit(self.root, "shared.py", "# trunk moved on\n")
        # beta is claimed and NEVER COMMITS — it sits exactly on the trunk tip
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        tip = _sh(self.root, "git", "rev-parse", "lane/beta").stdout.strip()
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(tip, head,
                         "fixture broken: beta must sit on the trunk tip")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL ON THE SAME ROOT OBJECT, unconditional.
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("OVERLAPPING LANES", out,
                         "a lane with no commits of its own cannot be racing "
                         "anyone for a file")

    def test_an_untouched_lane_stays_silent_after_trunk_moves_again(self):  # noqa: VACUOUS_ASSERTION — inherited shared.py and both rendered lanes positively control the intentional no-overlap assertion
        """REVIEW FINDING: trunk-tip equality is time-sensitive.

        beta is claimed at T1 and authors NOTHING. A later trunk T2 made beta
        differ from the current tip, so the old discriminator called its empty
        own-diff a landed lane and restored inherited shared.py. Its tip remains
        on trunk's first-parent line, which is the durable no-authorship fact."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        self._commit(self.root, "shared.py", "# trunk T1\n")
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        beta = _sh(self.root, "git", "rev-parse", "lane/beta").stdout.strip()
        self._commit(self.root, "later.py", "# trunk T2\n")
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(beta, head,
                            "fixture broken: trunk must move after beta claim")
        first_parent = _sh(self.root, "git", "rev-list", "--first-parent",
                           "HEAD").stdout.splitlines()
        self.assertIn(beta, first_parent,
                      "fixture broken: untouched beta must remain trunk-line")
        base = _sh(self.root, "git", "merge-base", "lane/alpha",
                   "lane/beta").stdout.strip()
        inherited = _sh(self.root, "git", "diff", "--name-only",
                        base + "..lane/beta").stdout
        self.assertIn("shared.py", inherited,
                      "fixture broken: pair-base must contain inherited file")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("OVERLAPPING LANES", out,
                         "an untouched stale trunk-line lane authored no file")

    def test_a_narrowing_that_cannot_compute_hands_back_the_wide_answer(self):
        """A CHECK THAT COULD NOT LOOK MUST NOT ANSWER AS IF IT HAD — and here
        the safe side is the WIDE one, because a missed collision cost a day
        and an extra pair costs a glance. With no trunk resolvable the
        narrowing is impossible, so the pair-base result must stand."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            self._commit(os.path.join(self.root + "-wt", lane),
                         "shared.py", "# " + lane + "\n")
        # POSITIVE CONTROL: with a trunk, this pair is REAL and surfaces.
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("shared.py", out)
        with mock.patch.object(_work_gc, "_trunk", return_value=""):
            overlaps = _work_gc.lane_overlaps(self.root)
        self.assertTrue(overlaps,
                        "an unresolvable trunk must fall back to the "
                        "un-narrowed pair-base answer, never to silence")
        self.assertIn("shared.py", overlaps[0][2])


class ClaimDisclosesLiveFilesTest(WorkBase):
    """THE FILE AXIS OF THE SAME SEAM. KindredLaneWarningTest catches a lane
    whose NAME leads another; this catches the case where the names share
    nothing.

    MEASURED 2026-08-04: three seats built the #183 tree-warning fix in
    parallel as which-helm-warning-scope, treewarn-fires-only-in-a-helm-
    checkout and fix-183-tree-warning-scope — no shared prefix, one shared
    file (helm/cli.py). The name check could not see it, and lane_overlaps
    could, but it runs inside `gc` and reports to whoever does housekeeping
    after both duplicates are written.

    It DISCLOSES rather than compares: the lane being claimed has no commits,
    so nothing can compare its diff. It states what is live and in what."""

    def _commit(self, path, name, body):
        with open(os.path.join(path, name), "w") as f:
            f.write(body)
        _sh(path, "git", "add", "-A")
        _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", name + " in " + os.path.basename(path))

    def test_a_held_lanes_files_are_named_at_claim_time(self):
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        # POSITIVE CONTROL, unconditional: the claim SUCCEEDED, so the line
        # below is added information and not a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("live lane alpha", err)
        self.assertIn("shared.py", err,
                      "the FILE is the whole signal — a lane name it does not "
                      "recognise tells the claimer nothing")

    def test_the_lane_being_claimed_is_not_disclosed_to_itself(self):  # noqa: VACUOUS_ASSERTION — the control is a SECOND claim in this same test proving alpha IS disclosable to another claimer, so the silence toward its own holder is the exclude arm and not an unwired stream
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        # CONTROL: alpha IS disclosable — it shows to another claimer.
        rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err2)
        self.assertIn("live lane alpha", err2)
        rc, _o, err3 = self.work("claim", "alpha", "--seat", "s1")
        self.assertNotIn("live lane alpha", err3,
                         "a lane must never be announced to its own claimer")

    def test_an_UNHELD_lane_is_not_disclosed(self):  # noqa: VACUOUS_ASSERTION — the control runs first and unconditionally: while alpha is HELD it is disclosed, so the silence under a patched-empty _live is the lease filter acting
        """Same live-lease filter lane_overlaps uses: an unheld room is nobody
        racing you, and listing it is the noise that gets a guard ignored."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertIn("live lane alpha", err2)   # CONTROL: held -> disclosed
        with mock.patch.object(_work_gc, "_live", return_value={}):
            rc, _o, err3 = self.work("claim", "gamma", "--seat", "s3")
        self.assertEqual(rc, 0, err3)
        self.assertNotIn("live lane alpha", err3,
                         "an unheld lane must not be announced")

    def test_a_lane_that_has_authored_NOTHING_is_still_disclosed(self):
        """THE COLLISION WINDOW IS BEFORE ANYONE WRITES CODE, and that is
        exactly where this disclosure used to go silent.

        held_lane_files ended with `if files:`, so a live lane with no commits
        never reached the claimer. The function's own docstring says it exists
        because lane_overlaps needs commits from BOTH lanes and the lane being
        claimed has none — 'a guard that says clear exactly when you consult
        it is worse than no guard'. It then required commits from every lane
        it disclosed, reintroducing the same silence one seam over.

        Measured 2026-08-05 on the live checkout: 13 held lanes, 12 disclosed,
        1 dropped — and the dropped one was an actively-worked lane with an
        open dispatch. That same hour two seats claimed one defect under two
        labels minutes apart, neither having authored anything, and neither
        claim warned the other."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # NOTHING IS COMMITTED TO alpha. That is the whole fixture.

        rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err2)
        self.assertIn("live lane alpha", err2,
                      "a live lane with no commits was dropped from the "
                      "disclosure — the claimer was told the coast was clear "
                      "during the exact window a duplicate is born")
        self.assertIn("nothing authored yet", err2,
                      "an authored-nothing lane was flattened into the "
                      "files wording, which reads as 'live and touching "
                      "nothing' — the opposite of the warning intended")

        # AND IT STAYS DISTINCT FROM THE FILES CASE. Without this, the arm
        # above would pass on a build that printed the no-file wording for
        # EVERY lane, which is the same conflation one direction over.
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        rc, _o, err3 = self.work("claim", "gamma", "--seat", "s3")
        self.assertEqual(rc, 0, err3)
        self.assertIn("live lane alpha is in", err3,
                      "a lane that HAS authored files lost its file list")
        self.assertNotIn("live lane alpha CLAIMED, nothing authored", err3,
                         "the no-file wording leaked onto a lane that has "
                         "authored files")

    def test_an_UNREADABLE_history_says_UNKNOWN_and_not_nothing_authored(self):
        """A FAILED READ MUST NOT BECOME A CONFIDENT DENIAL, and the first cut
        of this disclosure manufactured exactly that.

        _authored returns the _EVERYTHING sentinel when it could not look — no
        trunk resolvable, merge-base non-zero, diff non-zero. _EVERYTHING is
        an EMPTY frozenset subclass, so it is FALSEY, and `sorted(x or ())`
        turned it into [] — indistinguishable from a branch that genuinely
        authored nothing. The claim seam then printed "CLAIMED, nothing
        authored yet" about a lane that may have authored plenty, stating as
        fact the one thing the read had failed to establish. (@codex.)"""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")

        # CONTROL: readable, it reports FILES — so the UNKNOWN below is the
        # sentinel doing the work and not the lane having gone quiet.
        rc, _o, ctl = self.work("claim", "ctl", "--seat", "sc")
        self.assertIn("live lane alpha is in", ctl)

        with mock.patch.object(_work_gc, "_authored",
                               return_value=_work_gc._EVERYTHING):
            rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err2)
        self.assertIn("authorship UNKNOWN", err2,
                      "a history that could not be READ was reported as a "
                      "history that was read and found empty")
        self.assertNotIn("nothing authored yet", err2,
                         "the failed read was rendered as a confident denial")

    def test_every_live_lane_is_NAMED_however_many_there_are(self):
        """NEVER OMIT AN IDENTITY, ONLY ABBREVIATE EVIDENCE.

        THREE INDEPENDENT POPULATIONS, and the previous version of this test
        did not have them — @codex caught it. I claimed 5 UNKNOWN + 8 bare +
        5 file-bearing, but the UNKNOWN five were PATCHED OUT OF THE BARE
        EIGHT, so the real populations were 5/3/5 with UNKNOWN a strict subset
        of bare. A fixture whose classes overlap can misclassify a row and
        stay green, because the row it put in the wrong bucket is a member of
        both. The classes are disjoint now: unk*, bare*, filey* are three
        separate sets of lanes.

        The cap now sits on the FILE LIST, never on the lane. A lane's NAME is
        the whole signal a claimer needs to recognise their own subject under
        someone else's label, and it costs one line."""
        unk = ["unk%d" % i for i in range(5)]
        bare = ["bare%d" % i for i in range(8)]
        filey = ["filey%d" % i for i in range(5)]
        for lane in unk + bare + filey:
            rc, _o, err = self.work("claim", lane, "--seat", "s-" + lane)
            self.assertEqual(rc, 0, err)
        for lane in filey:
            self._commit(os.path.join(self.root + "-wt", lane),
                         "%s.py" % lane, "# %s\n" % lane)

        real = _work_gc._authored

        def unknown_only_for_unk(v, root, trunk, branch, cache):
            if branch.startswith("lane/unk"):
                return _work_gc._EVERYTHING
            return real(v, root, trunk, branch, cache)

        with mock.patch.object(_work_gc, "_authored", unknown_only_for_unk):
            rc, _o, err = self.work("claim", "newcomer", "--seat", "sn")
        self.assertEqual(rc, 0, err)

        # EXACT CLASS-SPECIFIC LINES, not a name appearing somewhere. A bare
        # membership check would pass on a build that printed every lane under
        # one wording, which is the misclassification this fixture exists to
        # catch.
        for lane in unk:
            self.assertIn("live lane %s — authorship UNKNOWN" % lane, err,
                          "an UNKNOWN lane was missing or misclassified: %s"
                          % lane)
        for lane in bare:
            self.assertIn("live lane %s CLAIMED, nothing authored yet" % lane,
                          err, "a bare lane was missing or misclassified: %s"
                               % lane)
        for lane in filey:
            self.assertIn("live lane %s is in %s.py" % (lane, lane), err,
                          "a file-bearing lane lost its identity or its file "
                          "evidence: %s" % lane)

        # AND THE POPULATIONS REALLY ARE DISJOINT — without this the three
        # loops above could all be satisfied by one over-broad wording.
        self.assertEqual(err.count("authorship UNKNOWN"), len(unk))
        self.assertEqual(err.count("nothing authored yet"), len(bare))

    def test_two_lanes_on_ONE_filename_both_survive(self):
        """FILENAME OVERLAP IS THE COLLISION CASE, and my fixture did not have
        it — @codex found this by mutating rather than reading.

        Every file-bearing lane in the other arm owns a filename nobody else
        touches (fileyN -> fileyN.py). So a production change that DEDUPED
        non-empty rows by tuple(files) parsed, imported, and left that test
        GREEN — while a real probe with two lanes both on shared.py disclosed
        only the first. The fixture could not see the bug because it never
        built two lanes that could be confused for each other.

        AND FILENAME OVERLAP IS THE WHOLE POINT OF THIS DISCLOSURE. Two lanes
        editing one file is not an edge case here; it is the collision the
        claim seam exists to announce."""
        for lane in ("alpha", "zeta"):
            rc, _o, err = self.work("claim", lane, "--seat", "s-" + lane)
            self.assertEqual(rc, 0, err)
            self._commit(os.path.join(self.root + "-wt", lane),
                         "shared.py", "# %s\n" % lane)

        rc, _o, err2 = self.work("claim", "newcomer", "--seat", "sn")
        self.assertEqual(rc, 0, err2)
        # BOTH exact lines. A count would pass on a build that printed one
        # lane twice, and a bare `assertIn("shared.py")` would pass on a build
        # that dropped either lane entirely.
        self.assertIn("live lane alpha is in shared.py", err2,
                      "alpha vanished from the disclosure")
        self.assertIn("live lane zeta is in shared.py", err2,
                      "zeta vanished — two lanes on ONE file is exactly the "
                      "collision this disclosure exists to announce, and it "
                      "is the case a dedupe-by-fileset silently eats")

    def test_a_long_file_list_is_abbreviated_IN_PLACE_with_its_count(self):
        """The evidence may be abbreviated; the abbreviation must say so
        beside the lane it belongs to, not in a trailing summary a reader
        cannot attribute."""
        rc, _o, err = self.work("claim", "wide", "--seat", "sw")
        self.assertEqual(rc, 0, err)
        for i in range(7):
            self._commit(os.path.join(self.root + "-wt", "wide"),
                         "f%d.py" % i, "# %d\n" % i)

        rc, _o, err2 = self.work("claim", "after", "--seat", "sa")
        self.assertEqual(rc, 0, err2)
        # THE EXACT LINE, not a substring of it. @codex: an aggregate
        # `assertIn("more file(s)")` passes on ANY count — including a wrong
        # one — and `assertIn("is in")` passes without a single filename. The
        # thing under test is which four survive and that the remainder is
        # counted correctly, so both have to be in the assertion.
        self.assertIn(
            "live lane wide is in f0.py, f1.py, f2.py, f3.py "
            "(+3 more file(s))", err2,
            "the abbreviated file list is wrong in its files, its order, or "
            "its remainder count")

    def test_a_raising_disclosure_never_costs_the_claim(self):
        """It runs AFTER the claim has already succeeded, so a git hiccup must
        cost the line and never the lease."""
        with mock.patch.object(_work_gc, "held_lane_files",
                               side_effect=RuntimeError("boom")):
            rc, out, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("alpha", out, "the claim itself must still report")


    def test_one_raising_disclosure_never_silences_the_other(self):
        """THE REGRESSION A REVIEWER MEASURED, now pinned. The moved-target
        disclosure was added ahead of this one INSIDE THE SAME try, so when it
        raised, the live-lane line vanished entirely and the claim still
        returned 0 — a new guard silently deleting an older one, which is
        strictly worse than the gap it was added to close.

        MUST-HIT FIRST: an unpatched claim prints the live-lane line at all,
        or every assertion below is satisfied by a surface that never speaks."""
        self.work("claim", "beta", "--seat", "s2")
        # beta needs an AUTHORED file or held_lane_files has nothing to say —
        # my first fixture skipped this and the control below caught it,
        # which is the only reason the assertions are not vacuous.
        self._commit(os.path.join(self.root + "-wt", "beta"),
                     "shared.py", "# beta\n")
        _rc, _o, base_err = self.work("claim", "alpha", "--seat", "s1")
        self.assertIn("live lane beta", base_err, "control: the line exists")

        # a THIRD lane rather than release-and-reclaim: release needs the
        # lease id, and the point is only to run the seam a second time.
        with mock.patch.object(_work_gc, "moved_lane_targets",
                               side_effect=RuntimeError("boom")):
            rc, out, err = self.work("claim", "gamma", "--seat", "s3")
        self.assertEqual(rc, 0, err)
        self.assertIn("gamma", out, "the claim itself must still report")
        self.assertIn("live lane beta", err,
                      "the sibling disclosure must survive its neighbour")


class MovedLaneTargetTest(WorkBase):
    # THE ENTRY NAMES ITS EVENT, and these arms pin that. The first cut
    # reported a bare path and answered only "was it DELETED" — which was
    # SILENT on the incident that motivated the guard, because the real web
    # split left helm/web.py in place as a 156-line facade (3805 -> 156, 36
    # added / 3685 deleted, --diff-filter=D empty). Two events now report,
    # each once and each labelled: "(deleted on trunk)" and "(trunk -N,
    # yours M)". The numbers travel because the reader, not a tuned
    # constant, decides whether the ground moved too far.

    """DID TRUNK MOVE THE FILE OUT FROM UNDER A LIVE LANE — the other axis of
    the same seam. held_lane_files answers "who else is in this file NOW";
    moved_lane_targets answers "the file you are in is no longer where the
    trunk keeps that code".

    MEASURED 2026-08-05: a lane edited helm/web.py; the web split landed and
    moved that code to helm/web_core.py, leaving web.py a facade. The rebase
    CONFLICTED, which is the loud outcome and git working correctly. The silent
    outcome is the expensive one — hunks that do not collide apply to a file
    that no longer holds the caller, the real caller keeps calling a name the
    same commit deleted, and the gate stays GREEN the whole way because it
    measured the OLD base.

    ABSENCE FROM THE TRUNK IS THE WRONG TEST ON ITS OWN: a file the lane
    CREATED is absent by design, and this lane's own work added helm/tasks.py.
    The merge-base is the discriminator and
    `test_a_file_the_lane_CREATED_is_not_a_moved_target` is the arm that
    binds it."""

    def _write(self, path, name, body):
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(body)

    def _commit(self, path, msg):
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", msg)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _lane_editing_web(self, lane="alpha", seat="s1", creates=None):
        """The trunk carries helm/web.py; `lane` branches from it and edits it,
        plus any `creates` path the lane INVENTS. Returns the room path."""
        self._write(self.root, "helm/web.py", "# the real caller lives here\n")
        self._commit(self.root, "seed web")
        rc, _o, err = self.work("claim", lane, "--seat", seat)
        self.assertEqual(rc, 0, err)
        room = os.path.join(self.root + "-wt", lane)
        self._write(room, "helm/web.py",
                    "# the real caller lives here\n# %s's edit\n" % lane)
        if creates:
            self._write(room, creates, "# a module this lane CREATED\n")
        self._commit(room, "edit web")
        return room

    def _trunk_moves_web(self):
        """The split lands: helm/web.py's code becomes helm/web_core.py."""
        r = _sh(self.root, "git", "mv", "helm/web.py", "helm/web_core.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._commit(self.root, "split web out to web_core")
        # FIXTURE CONTROLS, unconditional: the move really landed on the trunk.
        self.assertNotEqual(_sh(self.root, "git", "cat-file", "-e",
                                "main:helm/web.py").returncode, 0,
                            "fixture broken: the trunk must no longer have "
                            "helm/web.py")
        self.assertEqual(_sh(self.root, "git", "cat-file", "-e",
                             "main:helm/web_core.py").returncode, 0,
                         "fixture broken: the code must be at web_core.py")

    def _trunk_DRAINS_web(self):
        """THE SHAPE PRODUCTION ACTUALLY MINTS. The real web split did NOT
        move or delete helm/web.py — it left a 156-line facade behind and
        emptied the body into siblings. Measured on the live split
        pair: 3805 lines -> 156, +36/-3685, and
        `git diff --diff-filter=D` reports NOTHING. The first version of this
        guard asked only about deletion and was therefore silent on the exact
        incident that motivated it."""
        with open(os.path.join(self.root, "helm", "web_core.py"), "w") as f:
            f.write("\n".join("def moved_%d(): return %d" % (i, i)
                               for i in range(40)) + "\n")
        with open(os.path.join(self.root, "helm", "web.py"), "w") as f:
            f.write("from .web_core import *  # facade\n")
        self._commit(self.root, "drain web.py into web_core, keep a facade")
        # FIXTURE CONTROLS, unconditional and both directions: the file is
        # STILL THERE (so a deletion test must stay silent) and it really lost
        # its body (so there is something to detect at all).
        self.assertEqual(_sh(self.root, "git", "cat-file", "-e",
                             "main:helm/web.py").returncode, 0,
                         "fixture broken: the facade must SURVIVE")
        d = _sh(self.root, "git", "diff", "--name-only", "--diff-filter=D",
                "--no-renames", "main~1", "main")
        self.assertNotIn("helm/web.py", d.stdout,
                         "fixture broken: nothing may be DELETED here — that "
                         "is the whole point of the drained shape")

    def test_the_SURFACE_never_claims_deletion_for_a_drained_file(self):  # noqa: VACUOUS_ASSERTION — the two absences (NO LONGER HAS, deleted) are on the SAME `err` binding as two unconditional must-hits directly above them: assertIn("helm/web.py", err) and assertIn("trunk +", err), both non-negotiable and both on the beta-claim result. A run where the disclosure never printed fails at those two before reaching either absence. The analyzer cannot credit them because `err` is a tuple-unpacked call result rather than a literal comparison; the controls are real and named here so the annotation is checkable rather than a shrug.
        """THE SEAM ARM. The producer learned to tell deleted from drained and
        the claim surface collapsed them back: it printed a flat deletion claim
        directly after an entry showing trunk still had the file, which shows
        trunk DOES have it. Self-contradicting in one sentence, and a reader is
        sent hunting a deleted file that is still there.

        Neither side's own tests could see it — the producer's arms assert the
        RETURN VALUE and never render it; the surface's arm asserted only that
        SOMETHING printed. This runs both and reads the sentence."""
        body = "\n".join("def real_%d(): return %d" % (i, i)
                          for i in range(60)) + "\n"
        self._write(self.root, "helm/web.py", body)
        self._commit(self.root, "seed a substantial web.py")
        rc, _o, _e = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0)
        room = os.path.join(self.root + "-wt", "alpha")
        self._write(room, "helm/web.py", body + "def lane_edit(): return 1\n")
        self._commit(room, "lane edits web.py")
        self._trunk_DRAINS_web()

        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        # MUST-HIT: the disclosure fired at all, or every absence below is free
        self.assertIn("helm/web.py", err, "the disclosure must have printed")
        self.assertIn("trunk +", err, "the entry must carry trunk's numbers")
        # AND THE SENTENCE MUST NOT CLAIM A DELETION THAT DID NOT HAPPEN
        # BOTH absences on the SAME binding the must-hits above assert on,
        # not on a derived expression. My first version tested
        # err.lower().replace("deleted on trunk", "") — a fresh observable
        # with no positive control, which is what the vacuity rung flagged
        # and it was right. A drained file must produce NO deletion language
        # at all, so the simpler assertion is also the stronger one.
        self.assertNotIn("NO LONGER HAS", err)
        self.assertNotIn("deleted", err)
        # the file really is still there — the claim would have been false
        self.assertEqual(_sh(self.root, "git", "cat-file", "-e",
                             "main:helm/web.py").returncode, 0)

    def test_a_DRAINED_file_is_reported_though_nothing_was_deleted(self):
        """THE FOUNDING INCIDENT, which the first implementation could not see.

        A reviewer pointed the predicate at the real split pair and it
        returned [] — the guard was silent on the case it was built for,
        because the fixture used `git mv` (a shape production does not mint)
        while production drains a file and leaves the path in place."""
        # A SUBSTANTIAL FILE, because the comparison is between two MEASURED
        # quantities and a one-line file cannot express it: trunk removing 1
        # line is not more than a lane that changed 1 line, and the guard is
        # RIGHT to stay quiet there. The real case was 3685 against 1.
        body = "\n".join("def real_%d(): return %d" % (i, i)
                          for i in range(60)) + "\n"
        self._write(self.root, "helm/web.py", body)
        self._commit(self.root, "seed a substantial web.py")
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        room = os.path.join(self.root + "-wt", "alpha")
        self._write(room, "helm/web.py", body + "def lane_edit(): return 1\n")
        self._commit(room, "lane edits one line of web.py")
        self._trunk_DRAINS_web()
        got = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(len(got), 1, got)
        lane, gone = got[0]
        self.assertEqual(lane, "alpha")
        self.assertEqual(len(gone), 1, gone)
        # the path is named, the EVENT is named, and the NUMBERS travel so a
        # reader judges rather than a tuned constant deciding for them
        self.assertIn("helm/web.py", gone[0])
        # the NUMBERS travel and they are TRUNK'S, not the lane's — detection
        # must not depend on how much this lane typed (a reviewer named the
        # previous "trunk removed more than you changed" rule as a tuned
        # threshold wearing a ratio of 1.0)
        self.assertIn("trunk +", gone[0])
        # the DELETION COUNT must be visible and non-zero — asserted by
        # PARSING it, not by matching a digit. My first version pinned "-3",
        # a magnitude from the REAL incident (-3685) that this fixture never
        # produces (-60): an assertion tied to the wrong instance's numbers.
        removed = int(gone[0].split("/-")[1].split(" ")[0])
        self.assertGreater(removed, 1, gone[0])
        self.assertNotIn("yours", gone[0],
                         "the lane's own edit size is not part of the claim")
        self.assertNotIn("deleted on trunk", gone[0],
                         "nothing was deleted — mislabelling the event is how "
                         "the next reader looks for the wrong thing")

    def test_a_file_the_trunk_MOVED_is_reported_against_the_live_lane(self):
        """THE MUST-HIT. Without it every other arm here is an absence and the
        whole class passes with the function returning [] forever."""
        self._lane_editing_web()
        self._trunk_moves_web()
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])

    def test_a_file_the_lane_CREATED_is_not_a_moved_target(self):
        """THE DISCRIMINATOR. A created file is absent from the trunk BY
        DESIGN, so the naive 'not on trunk' test flags every new module — it
        would have flagged helm/tasks.py, which this lane's own work added.
        One lane, one call: the moved file is named and the new one is not."""
        self._lane_editing_web(creates="helm/tasks.py")
        self._trunk_moves_web()
        # FIXTURE CONTROLS, unconditional: BOTH paths are authored by the lane
        # and BOTH are absent from the trunk, so absence cannot be what
        # separates them and the silence below is the merge-base check acting.
        base = _sh(self.root, "git", "merge-base", "lane/alpha",
                   "main").stdout.strip()
        authored = _sh(self.root, "git", "diff", "--name-only",
                       base + "..lane/alpha").stdout
        self.assertIn("helm/web.py", authored,
                      "fixture broken: the lane must have authored web.py")
        self.assertIn("helm/tasks.py", authored,
                      "fixture broken: the lane must have authored tasks.py")
        self.assertNotEqual(_sh(self.root, "git", "cat-file", "-e",
                                "main:helm/tasks.py").returncode, 0,
                            "fixture broken: a created file is absent from "
                            "the trunk — that is the confusion under test")
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])

    def test_an_UNKNOWN_authored_set_is_silence_not_an_all_clear(self):  # noqa: VACUOUS_ASSERTION — the positive control is the SAME call on the SAME fixture, run first and unconditionally: it names alpha/helm/web.py, so the empty result under the sentinel is the UNKNOWN path and not a scan that found nothing
        """CANNOT-LOOK IS NOT CLEAR. _authored hands back _EVERYTHING when it
        could not determine what a branch wrote, and _EVERYTHING is a frozenset
        SUBCLASS — EMPTY and therefore FALSEY. A truthiness test reads UNKNOWN
        as 'authored nothing'; the code must ask by IDENTITY."""
        self._lane_editing_web()
        self._trunk_moves_web()
        # POSITIVE CONTROL, unconditional and first: this fixture DOES report.
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])
        with mock.patch.object(_work_gc, "_authored",
                               return_value=_work_gc._EVERYTHING):
            unknown = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(unknown, [],
                         "an undeterminable authored set must yield SILENCE "
                         "for that lane, never a verdict")

    def test_an_UNREADABLE_deletion_probe_is_silence_not_a_drained_label(self):
        """CANNOT-LOOK IS NOT CLEAR, on the probe that labels the EVENT.

        The first cut wrote `deleted_paths = ... if rc_g == 0 else set()`,
        which turns an unreadable probe into a determinate "nothing was
        deleted" — indistinguishable from the real empty answer, and it flows
        into the drainage branch, so a genuinely DELETED file gets reported as
        merely drained with a label the evidence cannot support. The
        function's own docstring promised silence on unreadable input and this
        line broke that promise two calls later."""
        self._lane_editing_web()
        self._trunk_moves_web()
        # MUST-HIT FIRST: with the probe WORKING this lane is reported, so the
        # silence asserted below is caused by the failure and not by an empty
        # board.
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])

        real = _work_gc.vcs.backend(self.root).text

        def flaky(root, *args, **kw):
            if "--diff-filter=D" in args:
                return (128, "", "fatal: could not read the object")
            return real(root, *args, **kw)

        with mock.patch.object(type(_work_gc.vcs.backend(self.root)), "text",
                               side_effect=flaky, autospec=False):
            got = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(got, [], "an unreadable probe must say NOTHING — "
                                  "reporting it as drained asserts an event "
                                  "the evidence cannot support")

    def test_an_UNHELD_lane_is_nobody_racing_you(self):  # noqa: VACUOUS_ASSERTION — the positive control is the SAME call on the SAME fixture, run first and unconditionally: while the lease is live it names alpha/helm/web.py, so the empty result under an empty ledger is the held-lease filter acting
        """The same live-lease filter held_lane_files and lane_overlaps use: a
        parked room's holder is gone, and listing it is the noise that teaches
        people to scroll past the line that matters."""
        self._lane_editing_web()
        self._trunk_moves_web()
        # POSITIVE CONTROL, unconditional and first: held -> reported.
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])
        with mock.patch.object(_work_gc, "_live", return_value={}):
            unheld = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(unheld, [],
                         "an unheld room is nobody racing you — it must not "
                         "be reported")

    def test_the_claim_seam_says_it_on_stderr(self):
        """THE SURFACE IS THE BAR. A guard that computes correctly and reaches
        nobody is not a guard; this is the line a claimer actually reads."""
        self._lane_editing_web()
        self._trunk_moves_web()
        rc, out, err = self.work("claim", "beta", "--seat", "s2")
        # POSITIVE CONTROL, unconditional: the claim SUCCEEDED, so the line
        # below is added information and not a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("beta", out)
        # THE DURABLE PARTS, not the sentence. Pinning exact prose made this
        # arm fail every time the wording was corrected — including when the
        # correction was the POINT (the old line claimed a deletion that had
        # not happened). What must hold: the lane is named, the path is named,
        # and the reader is told what to do.
        self.assertIn("alpha", err)
        self.assertIn("helm/web.py", err)
        self.assertIn("rebase before trusting a gate", err)
        # the sentence must not assert an event the entry did not name —
        # this used to read "trunk NO LONGER HAS it" for a file trunk still
        # has. Pinned from the other side in the SURFACE arm above.
        self.assertIn("trunk changed code this lane edits", err)
        self.assertIn("rebase before trusting a gate on it", err)


class KindredLaneWarningTest(WorkBase):
    """CLAIM WAS SILENT AT THE ONE MOMENT SOMEONE IS ABOUT TO DUPLICATE WORK.

    MEASURED 2026-08-04: I claimed `gate-import-verb` while `lane/gate-import`
    sat on disk — already built and already content-reviewed — and claim said
    nothing, because `_has_branch` asks only whether THIS exact name exists.
    Six hours earlier I had written a premise telling myself to sweep for
    exactly that, read it, and did not run it. An advisory line that is read
    and ignored under load is the canon's definition of a check that belongs
    at the keystroke instead."""

    def test_a_lane_whose_name_leads_this_one_is_announced(self):
        rc, _o, err = self.work("claim", "gate-import", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.work("claim", "gate-import-verb", "--seat", "s2")
        # POSITIVE CONTROL on the same observable, unconditional: the claim
        # SUCCEEDED, so the warning below is an added line and not a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("already exists", err)
        self.assertIn("lane/gate-import", err)
        self.assertIn("Claiming anyway is fine", err,
                      "it must read as information, not a block")

    def test_an_unrelated_lane_is_silent(self):
        """A check-in that cries wolf teaches people to scroll past the line
        that matters — the law `_warn_inherited_base` states for itself."""
        rc, _o, err = self.work("claim", "gate-import", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional and first:
        # a KINDRED name really does put the line on this stream, so the
        # silence below is the predicate declining and not an unwired stderr.
        # (An earlier version asserted stderr was merely non-empty and went
        # red — claim writes NOTHING there on a clean claim, so the only
        # honest control is making the warning itself appear.)
        _rc, _o, kin_err = self.work("claim", "gate-import-verb", "--seat", "s3")
        self.assertIn("already exists", kin_err)
        rc, _o, err = self.work("claim", "seat-where-leaks", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("already exists", err)

    def test_a_single_token_lane_cannot_trip_it(self):
        """One shared token is not evidence of anything — the predicate needs
        TWO leading tokens or every lane starting with `seat` collides."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL on the same stream, unconditional.
        _rc, _o, kin_err = self.work("claim", "alpha-two-tokens", "--seat", "s3")
        self.assertNotIn("already exists", kin_err,
                         "alpha/alpha-two share ONE token — still silent")
        rc, _o, err = self.work("claim", "alphabet", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("already exists", err)

    def test_re_claiming_the_SAME_lane_does_not_warn_about_itself(self):
        """Re-claim is idempotent and must stay quiet: the branch already
        exists, so this is not a fresh cut and nothing is being duplicated."""
        rc, out, err = self.work("claim", "gate-import", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lease = out.split("\t")[2].strip()
        rc, out2, err = self.work("claim", "gate-import", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL: the re-claim really happened and handed back the
        # same room, so the absent warning is the fresh-branch gate holding.
        self.assertIn("gate-import", out2)
        self.assertNotIn("already exists", err)


class UnguardedRoomTest(WorkBase):
    """Only Claude Agent/Workflow isolation rooms are reported, with every
    uncertain liveness input retaining an explicit UNKNOWN state."""

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

    @contextlib.contextmanager
    def workflow_evidence(self, path, status="completed", live=True):
        home = os.path.join(self.tmp, "claude-home")
        sid = "11111111-2222-4333-8444-555555555555"
        slug = self.root.replace(os.sep, "-").replace(".", "-")
        d = os.path.join(home, "projects", slug, sid, "workflows")
        os.makedirs(d, exist_ok=True)
        run = os.path.basename(path).rsplit("-", 1)[0]
        with open(os.path.join(d, run + ".json"), "w") as f:
            json.dump({"runId": run, "status": status}, f)
        sessions = {sid} if live else set()
        with mock.patch.object(work._lanes, "_claude_homes", return_value=[home]), \
                mock.patch.object(work._lanes, "_live_claude_sessions",
                                  return_value=sessions):
            yield

    @contextlib.contextmanager
    def direct_agent_evidence(self, path, terminal=False, live=True):
        home = os.path.join(self.tmp, "direct-home")
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        slug = self.root.replace(os.sep, "-").replace(".", "-")
        d = os.path.join(home, "projects", slug, sid, "subagents")
        os.makedirs(d, exist_ok=True)
        room = os.path.basename(path)
        with open(os.path.join(d, room + ".meta.json"), "w") as f:
            json.dump({"worktreePath": path}, f)
        msg = {"type": "assistant", "message": {
            "stop_reason": "end_turn" if terminal else "tool_use",
            "content": [{"type": "text", "text": "done"}] if terminal
            else [{"type": "tool_use", "name": "Bash"}]}}
        with open(os.path.join(d, room + ".jsonl"), "w") as f:
            f.write(json.dumps(msg) + "\n")
        sessions = {sid} if live else set()
        with mock.patch.object(work._lanes, "_claude_homes", return_value=[home]), \
                mock.patch.object(work._lanes, "_live_claude_sessions",
                                  return_value=sessions):
            yield

    def test_agent_worktree_is_reported_with_stable_identity(self):
        path = self.foreign("wf_dead00-1")
        with self.workflow_evidence(path):
            rows = work.unguarded_rows(self.root)
            self.assertEqual([r["id"] for r in rows], ["wf_dead00-1"])
            rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("UNGUARDED CLEAN", out)
        self.assertIn("id=wf_dead00-1", out)
        self.assertIn("never authorizes cleanup", out)

    def test_main_lanes_and_arbitrary_worktrees_are_not_unguarded(self):
        rc, _out, err = self.work("claim", "mine", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray")
        normal = os.path.join(self.tmp, "ordinary-review")
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "ordinary-review", normal, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(work.unguarded_rows(self.root), [])

    def test_recent_write_is_advisory_not_ownership_claim(self):
        path = self.foreign("wf_busy00-2", dirty="cred.py")
        self.assertEqual(work._occupants(path), [])
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, err = self.work("list")
        self.assertTrue(row["dirty"])
        self.assertLessEqual(row["wrote_ago"], work.RECENT_WRITE_SECONDS)
        self.assertEqual(rc, 0, err)
        self.assertIn("RECENT-WRITE", out)
        self.assertIn("advisory, not ownership proof", out)
        self.assertNotIn("assume live", out)

    def test_clean_but_live_workflow_is_live_with_parent_cwd_elsewhere(self):
        path = self.foreign("wf_live000-3")
        self.assertEqual(work._occupants(path), [])
        with self.workflow_evidence(path, status="running", live=True):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertFalse(row["dirty"])
        self.assertEqual(row["harness"], "live")
        self.assertIn("LIVE-HARNESS", out)
        self.assertIn("Workflow active", out)

    def test_running_metadata_without_live_parent_is_unknown(self):
        path = self.foreign("wf_crashed0-4")
        with self.workflow_evidence(path, status="running", live=False):
            rc, out, _err = self.work("list")
        self.assertIn("UNGUARDED UNKNOWN", out)
        self.assertIn("harness metadata incomplete", out)

    def test_clean_direct_agent_is_live_until_its_final_text_end_turn(self):
        path = self.foreign("agent-deadbeef1234")
        with self.direct_agent_evidence(path, terminal=False, live=True):
            self.assertEqual(work.unguarded_rows(self.root)[0]["harness"], "live")
        with self.direct_agent_evidence(path, terminal=True, live=True):
            self.assertEqual(work.unguarded_rows(self.root)[0]["harness"],
                             "inactive")

    def test_terminal_clean_room_is_clean_but_not_declared_unowned(self):
        path = self.foreign("wf_idle000-5")
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertIsNone(row["wrote_ago"])
        self.assertEqual(row["harness"], "inactive")
        self.assertIn("UNGUARDED CLEAN", out)
        self.assertIn("not proof that no writer will resume", out)

    def test_stale_dirty_write_remains_dirty_without_live_claim(self):
        path = self.foreign("wf_stale00-6", dirty="half.py")
        old = time.time() - 7200
        os.utime(os.path.join(path, "half.py"), (old, old))
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertGreaterEqual(row["wrote_ago"], 7000)
        self.assertIn("UNGUARDED DIRTY", out)
        self.assertNotIn("LIVE-HARNESS", out)

    def test_missing_harness_metadata_fails_unknown(self):
        self.foreign("wf_orphan00-7")
        with mock.patch.object(work._lanes, "_claude_homes", return_value=[]), \
                mock.patch.object(work._lanes, "_live_claude_sessions", return_value=set()):
            rc, out, _err = self.work("list")
        self.assertIn("UNGUARDED UNKNOWN", out)
        self.assertIn("no matching harness metadata", out)

    def test_symlink_escape_is_reported_unknown_without_git_status(self):
        target = os.path.join(self.tmp, "outside")
        os.makedirs(target)
        path = os.path.join(self.root, ".claude", "worktrees", "wf_escape00-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        records = [{"path": path, "branch": "refs/heads/worktree-wf_escape00-8",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work._lanes, "_room_status") as status_call:
            rows, errors = work.unguarded_inventory(self.root, registered=records)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["harness"], "unknown")
        self.assertIn("symlink", rows[0]["hard_unknown"])
        status_call.assert_not_called()

    def test_foreign_repo_replacement_is_not_inspected_as_same_repo(self):
        path = os.path.join(self.root, ".claude", "worktrees", "wf_foreign0-9")
        os.makedirs(path)
        self.assertEqual(_sh(path, "git", "init", "-q").returncode, 0)
        records = [{"path": path, "branch": "refs/heads/worktree-wf_foreign0-9",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work._lanes, "_room_status") as status_call:
            rows, _errors = work.unguarded_inventory(self.root, registered=records)
        self.assertIn("not a regular worktree link", rows[0]["hard_unknown"])
        status_call.assert_not_called()

    def test_symlinked_container_escape_is_unknown(self):
        outside = os.path.join(self.tmp, "escaped-claude")
        os.makedirs(os.path.join(outside, "worktrees", "wf_parent00-1"))
        os.symlink(outside, os.path.join(self.root, ".claude"))
        path = os.path.join(self.root, ".claude", "worktrees", "wf_parent00-1")
        records = [{"path": path, "branch": "refs/heads/worktree-wf_parent00-1",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work._lanes, "_room_status") as status_call:
            rows, _errors = work.unguarded_inventory(self.root, registered=records)
        self.assertIn("container escapes", rows[0]["hard_unknown"])
        status_call.assert_not_called()


class PorcelainSafetyTest(WorkBase):
    def test_worktree_registry_is_nul_and_byte_safe(self):
        weird = os.fsencode(self.root) + b"/.claude/worktrees/agent-deadbeef\\\n\xff"
        raw = (b"worktree " + weird + b"\0HEAD abc\0branch refs/heads/x\0"
               b"locked because\0\0")
        with mock.patch.object(work._lanes, "_git_bytes", return_value=(0, raw, b"")):
            rows, error = work._worktree_records(self.root)
        self.assertIsNone(error)
        self.assertEqual(os.fsencode(rows[0]["path"]), weird)
        self.assertEqual(rows[0]["branch"], "refs/heads/x")
        self.assertEqual(rows[0]["reason"], "because")

    def test_worktree_registry_failure_and_truncation_are_not_empty_success(self):
        with mock.patch.object(work._lanes, "_git_bytes", return_value=(1, b"", b"boom")):
            self.assertEqual(work._worktree_records(self.root), ([], "boom"))
        with mock.patch.object(work._lanes, "_git_bytes",
                               return_value=(0, b"worktree /tmp/no-nul", b"")):
            rows, error = work._worktree_records(self.root)
        self.assertEqual(rows, [])
        self.assertIn("truncated", error)

    def test_porcelain_v2_parser_consumes_two_path_records_exactly(self):
        ordinary = b"1 .M N... 100644 100644 100644 a b line\\name\n\xff"
        rename = b"2 R. N... 100644 100644 100644 a b R100 new name"
        copy = b"2 C. N... 100644 100644 100644 a b C075 copy name"
        unmerged = b"u UU N... 100644 100644 100644 100644 a b c conflict"
        raw = (ordinary + b"\0" + rename + b"\0old name\0" + copy +
               b"\0source name\0" + unmerged + b"\0? untracked dir/file\0")
        got = work._status_entries(raw)
        self.assertEqual([p for p, _sub, _kind in got], [
            b"line\\name\n\xff", b"new name", b"copy name", b"conflict",
            b"untracked dir/file"])
        # the KIND is carried now, and the unmerged record must be identifiable
        # as unmerged — the parser always knew, and every caller threw it away
        self.assertEqual([k for _p, _sub, k in got],
                         [b"1", b"2", b"2", b"u", b"?"])

    def test_human_porcelain_and_truncated_records_fail_closed(self):
        with self.assertRaises(ValueError):
            work._status_entries(b" M human path\0")
        with self.assertRaises(ValueError):
            work._status_entries(b"? no terminator")
        with self.assertRaises(ValueError):
            work._status_entries(
                b"2 R. N... 100644 100644 100644 a b R100 new\0\0")

    def test_weird_untracked_names_and_untracked_directory_are_timed(self):
        path = self.room("weirdst")
        rel = b"dir/space newline\nback\\slash-\xff"
        os.makedirs(os.path.join(path, "dir"))
        fd = os.open(os.path.join(os.fsencode(path), rel),
                     os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIsNone(row["unknown"])
        self.assertIsNotNone(row["wrote_ago"])

    def test_deletion_and_dirty_submodule_are_timestamp_unknown(self):
        path = self.room("deleted")
        os.remove(os.path.join(path, "README"))
        row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIn("deleted", row["unknown"])
        record = (b"1 .M S.M. 160000 160000 160000 a b sub\0")
        fake_stat = os.stat(path)
        with mock.patch.object(work._lanes, "_git_bytes", return_value=(0, record, b"")), \
                mock.patch.object(work.os, "lstat", return_value=fake_stat):
            row = work._room_status(path)
        self.assertIn("submodule", row["unknown"])

    def test_symlink_mtime_is_not_target_mtime_and_future_clock_is_advisory(self):
        path = self.room("symlink")
        target = os.path.join(self.tmp, "future-target")
        with open(target, "w") as f:
            f.write("outside")
        future = time.time() + 3600
        os.utime(target, (future, future))
        link = os.path.join(path, "link")
        os.symlink(target, link)
        old = time.time() - 3600
        os.utime(link, (old, old), follow_symlinks=False)
        row = work._room_status(path, now=time.time())
        self.assertGreater(row["wrote_ago"], 3000)
        os.utime(link, (future, future), follow_symlinks=False)
        row = work._room_status(path, now=time.time())
        self.assertEqual(row["wrote_ago"], 0)
        self.assertTrue(row["clock_skew"])

    def test_ignored_files_are_excluded_and_status_failure_is_unknown(self):
        path = self.room("ignored")
        with open(os.path.join(path, ".gitignore"), "w") as f:
            f.write("ignored/\n")
        _sh(path, "git", "add", ".gitignore")
        _sh(path, "git", "commit", "-q", "-m", "ignore")
        os.makedirs(os.path.join(path, "ignored"))
        with open(os.path.join(path, "ignored", "secret"), "w") as f:
            f.write("ignored")
        self.assertFalse(work._room_status(path)["dirty"])
        with mock.patch.object(work._lanes, "_git_bytes",
                               return_value=(-1, b"", b"timeout")):
            row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIn("status failed", row["unknown"])


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
        # THE MOTIVATING FAILURE, now enforced rather than healed. It used to
        # SUCCEED (rc 0) and be silently corrected afterwards — git printed
        # "Switched to a new branch", the heal spoke only on stderr, and the
        # next commit landed on main. post-checkout cannot do better: git
        # ignores its exit code. reference-transaction refuses outright.
        r = _sh(self.root, "git", "checkout", "-b", "oops")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("helm work claim oops", r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "main")
        # and the ref never came into existence — nothing to clean up later
        self.assertFalse(work._has_branch(self.root, "oops"))
        # committing on the trunk is untouched by the ref guard
        with open(os.path.join(self.root, "README"), "a") as f:
            f.write("trunk work\n")
        self.assertEqual(_sh(self.root, "git", "commit", "-qam", "t").returncode,
                         0)
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

    def test_dry_run_scopes_the_integrator_override_honestly(self):
        """The dry line called HELM_WORK_INTEGRATOR=1 'the override' for a
        multi-rung install in which it clears exactly TWO things — the
        shared-tree ref rail and the lane-discipline venue rung — while six
        pre-commit rungs each carry their own one-commit skip (stated in
        their refusals and in the --apply summary). A reader following the
        dry line exported it expecting the conflict-marker or never-track
        rung to stand down; neither reads it."""
        rc, out, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        dry = next(l for l in out.splitlines() if "DRY" in l)
        self.assertIn("HELM_WORK_INTEGRATOR=1 clears the shared-tree ref "
                      "rail and the lane-discipline venue rung", dry)
        self.assertIn("its own one-commit skip", dry)
        self.assertNotIn("is the override", dry)

    def test_claim_still_works_with_the_ref_guard_installed(self):
        """The guard must not break the verb it points at. `worktree add -b`
        runs the hook with cwd AND toplevel both equal to the shared checkout
        — indistinguishable from a forbidden `checkout -b` — so claim exports
        HELM_WORK_CLAIM=1 to announce itself. Without that, installing the
        rail would refuse every `helm work claim` and strand the fleet."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("claim", "after-guard", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path = out.split("\t")[0]
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/after-guard"))
        self.assertEqual(
            _sh(path, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "lane/after-guard")
        # the shared checkout never left the trunk
        self.assertEqual(
            _sh(self.root, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "main")

    def test_release_and_gc_branch_cleanup_still_work_with_guard(self):
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("claim", "released", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lease = out.split("\t")[2]
        rc, _out, err = self.work("release", "released", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertFalse(work._has_branch(self.root, "lane/released"))

        path = self.room("collected")
        _sh(self.root, "git", "worktree", "lock", path,
            "--reason", "lease:deadbeef")
        rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/collected"))

    def test_ref_guard_leaves_lane_rooms_alone(self):
        """Branching INSIDE a lane room is the sanctioned workflow and must
        stay free — the rail guards the integrator's tree, nothing else."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        room = self.room("free")
        r = _sh(room, "git", "checkout", "-b", "free-sub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            _sh(room, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "free-sub")

    def test_existing_hooks_are_preserved_composed_and_idempotent(self):
        log = os.path.join(self.tmp, "hook log")
        os.environ["HELM_TEST_HOOK_LOG"] = log
        ref = work.hook_path(self.root, "reference-transaction")
        post = work.hook_path(self.root)
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w") as f:
            f.write("#!/bin/sh\nprintf 'ref:%s\\n' \"$1\" >> "
                    "\"$HELM_TEST_HOOK_LOG\"\n"
                    "while IFS= read -r line; do printf 'in:%s\\n' \"$line\" "
                    ">> \"$HELM_TEST_HOOK_LOG\"; done\nexit 0\n")
        os.chmod(ref, 0o750)
        with open(post, "w") as f:
            f.write("#!/bin/sh\nprintf 'post:%s:%s:%s\\n' \"$1\" \"$2\" "
                    "\"$3\" >> \"$HELM_TEST_HOOK_LOG\"\nexit 0\n")
        os.chmod(post, 0o700)
        originals = {ref: _bytes(ref), post: _bytes(post)}

        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("preserved existing", out)
        for path, mode in ((ref, 0o750), (post, 0o700)):
            user = path + ".helm-user"
            self.assertEqual(_bytes(user), originals[path])
            self.assertEqual(stat.S_IMODE(os.stat(user).st_mode), mode)
            self.assertTrue(os.access(path, os.X_OK))

        r = _sh(self.root, "git", "checkout", "-b", "blocked")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        with open(log) as f:
            calls = f.read()
        self.assertIn("ref:prepared", calls)
        self.assertIn("refs/heads/blocked", calls)

        before = {p: (_bytes(p), os.stat(p).st_mtime_ns)
                  for p in (ref, post, ref + ".helm-user", post + ".helm-user")}
        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("already up to date", out)
        after = {p: (_bytes(p), os.stat(p).st_mtime_ns)
                 for p in before}
        self.assertEqual(after, before)

    def test_install_rolls_back_every_path_after_partial_failure(self):
        ref = work.hook_path(self.root, "reference-transaction")
        post = work.hook_path(self.root)
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        for path, text in ((ref, "ref-user"), (post, "post-user")):
            with open(path, "w") as f:
                f.write("#!/bin/sh\n# %s\nexit 0\n" % text)
            os.chmod(path, 0o755)
        originals = {p: _bytes(p) for p in (ref, post)}
        real, calls = work._put_snapshot, []

        def fail_third(path, snap):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("injected install failure")
            return real(path, snap)

        with mock.patch.object(work._guard, "_put_snapshot", side_effect=fail_third):
            rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", err)
        for path in (ref, post):
            self.assertEqual(_bytes(path), originals[path])
            self.assertFalse(os.path.lexists(path + ".helm-user"))

    def test_non_executable_user_hook_stays_inactive(self):
        log = os.path.join(self.tmp, "inactive.log")
        os.environ["HELM_TEST_HOOK_LOG"] = log
        post = work.hook_path(self.root)
        os.makedirs(os.path.dirname(post), exist_ok=True)
        with open(post, "w") as f:
            f.write("#!/bin/sh\nprintf activated > \"$HELM_TEST_HOOK_LOG\"\n")
        os.chmod(post, 0o644)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(post + ".helm-user").st_mode),
                         0o644)
        _sh(self.root, "git", "checkout", "--", "README")
        self.assertFalse(os.path.exists(log))

    def test_foreign_symlink_hook_is_preserved_and_composed(self):
        log = os.path.join(self.tmp, "symlink.log")
        os.environ["HELM_TEST_HOOK_LOG"] = log
        post = work.hook_path(self.root)
        actual = os.path.join(os.path.dirname(post), "actual-user-post")
        os.makedirs(os.path.dirname(post), exist_ok=True)
        with open(actual, "w") as f:
            f.write("#!/bin/sh\nprintf symlink-user >> "
                    "\"$HELM_TEST_HOOK_LOG\"\n")
        os.chmod(actual, 0o755)
        os.symlink(os.path.basename(actual), post)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.islink(post + ".helm-user"))
        self.assertEqual(os.readlink(post + ".helm-user"), os.path.basename(actual))
        _sh(self.root, "git", "checkout", "--", "README")
        with open(log) as f:
            self.assertEqual(f.read(), "symlink-user")

    def test_existing_branch_switch_and_symbolic_ref_are_refused(self):
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "branch", "existing"], cwd=self.root,
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        r = _sh(self.root, "git", "commit", "--allow-empty", "-m", "forward")
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        r = _sh(self.root, "git", "update-ref", "refs/heads/main", "existing")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-fast-forward", r.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", "main").stdout.strip(),
                         tip)
        for cmd in (("checkout", "existing"),
                    ("symbolic-ref", "HEAD", "refs/heads/existing")):
            r = _sh(self.root, "git", *cmd)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("HEAD must stay", r.stderr)
            self.assertEqual(
                _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
                .stdout.strip(), "main")

    def test_plumbing_cannot_delete_or_move_an_occupied_branch(self):
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        room = self.room("held")
        old = _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip()
        for cmd in (("update-ref", "-d", "refs/heads/lane/held"),
                    ("update-ref", "refs/heads/lane/held", "main")):
            r = _sh(self.root, "git", *cmd)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("OCCUPIED", r.stderr)
            self.assertEqual(
                _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip(),
                old)
        # Its own ordinary commit remains legal.
        with open(os.path.join(room, "held.txt"), "w") as f:
            f.write("ok\n")
        self.assertEqual(_sh(room, "git", "add", "held.txt").returncode, 0)
        r = _sh(room, "git", "commit", "-m", "held work")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_ref_guard_fails_closed_when_occupancy_registry_is_unreadable(self):
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "branch", "other"], cwd=self.root,
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        fakebin = os.path.join(self.tmp, "fake-bin")
        os.makedirs(fakebin)
        fakegit = os.path.join(fakebin, "git")
        realgit = shutil.which("git")
        with open(fakegit, "w") as f:
            f.write("#!/bin/sh\n"
                    "if [ \"$1 $2\" = \"worktree list\" ]; then exit 9; fi\n"
                    "exec \"%s\" \"$@\"\n" % realgit)
        os.chmod(fakegit, 0o755)
        ref_hook = work.hook_path(self.root, "reference-transaction")
        with open(ref_hook) as f:
            script = f.read()
        with open(ref_hook, "w") as f:
            f.write(script.replace("#!/bin/sh\n", "#!/bin/sh\nPATH='" + fakebin
                                   + "':$PATH\n", 1))
        os.chmod(ref_hook, 0o755)
        old = _sh(self.root, "git", "rev-parse", "other").stdout.strip()
        r = subprocess.run(["git", "update-ref", "refs/heads/other", "main"],
                           cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot verify worktree occupancy", r.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", "other").stdout.strip(),
                         old)

    def test_install_from_linked_worktree_targets_common_hooks_only(self):
        linked = self.room("caller")
        root = work.find_root(linked)
        self.assertEqual(root, self.root)
        rc, lines = work.install_guard(root, apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        common = _sh(self.root, "git", "rev-parse", "--path-format=absolute",
                     "--git-common-dir").stdout.strip()
        for name, _var in work.GUARD_HOOKS:
            self.assertEqual(os.path.dirname(work.hook_path(root, name)),
                             os.path.join(common, "hooks"))
            per_worktree = _sh(linked, "git", "rev-parse", "--path-format=absolute",
                               "--git-dir").stdout.strip()
            self.assertFalse(os.path.exists(os.path.join(per_worktree, "hooks", name)))

    def test_repo_local_hooks_path_is_honored_but_external_path_is_refused(self):
        local = os.path.join(self.root, ".git", "helm-hooks")
        self.assertEqual(_sh(self.root, "git", "config", "core.hooksPath", local)
                         .returncode, 0)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(os.path.join(local, "reference-transaction")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git", "hooks",
                                                     "reference-transaction")))

        other = os.path.join(self.tmp, "foreign hooks")
        self.assertEqual(_sh(self.root, "git", "config", "core.hooksPath", other)
                         .returncode, 0)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("outside this repo", err)
        self.assertFalse(os.path.exists(other))

    def test_shell_quoting_handles_a_base_branch_with_apostrophe(self):
        base = "odd'base"
        self.assertEqual(_sh(self.root, "git", "branch", "-m", base).returncode, 0)
        rc, lines = work.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        r = _sh(self.root, "git", "commit", "--allow-empty", "-m", "on odd base")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(self.root, "git", "checkout", "-b", "blocked")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertEqual(_sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
                         .stdout.strip(), base)

    def test_guard_handles_repo_paths_with_spaces_newlines_and_symlink_callers(self):
        moved = os.path.join(self.tmp, "repo space\nline")
        os.rename(self.root, moved)
        self.root = moved
        alias = os.path.join(self.tmp, "repo-alias")
        os.symlink(self.root, alias)
        root = work.find_root(alias)
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.root))
        rc, lines = work.install_guard(root, apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        r = _sh(alias, "git", "checkout", "-b", "blocked")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertIn("REFUSED", r.stderr)

    def test_concurrent_installers_converge_to_one_complete_rail(self):
        def install(_):
            return work.install_guard(self.root, apply=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(install, range(16)))
        self.assertTrue(all(rc == 0 for rc, _lines in results), results)
        for name, _var in work.GUARD_HOOKS:
            path = work.hook_path(self.root, name)
            self.assertTrue(os.access(path, os.X_OK))
            with open(path) as f:
                self.assertIn(work.MANAGED_HOOK_MARKER, f.read())

    def test_claim_lock_blocks_normal_raw_remove_and_prune(self):
        rc, out, err = self.work("claim", "locked", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path = out.split("\t")[0]
        r = _sh(self.root, "git", "worktree", "remove", "--force", path)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("locked", r.stderr.lower())
        self.assertEqual(_sh(self.root, "git", "worktree", "prune").returncode, 0)
        self.assertTrue(os.path.isdir(path))


class SharedCheckoutLocalCommitTest(WorkBase):
    """#144, the INVERSION: the guard refused `git branch foo` — which creates
    nothing anyone can lose — while PERMITTING a plain commit onto shared
    main, the one operation that silently forks the tree nine seats read.

    A commit is always FAST-FORWARD, so it fell through the non-ff arm and
    landlock passed silently because nobody held the claim. MEASURED THREE
    TIMES on 2026-08-03: a docs commit stopped the ff for ~80 minutes and two
    more followed within the hour; every one was found by reflog archaeology
    rather than by an instrument, and the fleet spent the afternoon reasoning
    against a base that had silently forked.

    THE DISCRIMINATOR IS PROVENANCE, NOT SHAPE: a SYNC moves main to a commit
    that is ON its configured upstream; a LOCAL COMMIT moves it to one that is
    not. Both are fast-forward, so ancestry against the configured ref is the
    only thing that tells them apart. Missing upstream state is UNKNOWN, not
    proof this is a solo checkout."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.assertEqual(_sh(self.tmp, "git", "init", "-q", "--bare", "-b",
                             "main", self.origin).returncode, 0)
        _sh(self.root, "git", "remote", "add", "origin", self.origin)
        self.assertEqual(_sh(self.root, "git", "push", "-q", "origin",
                             "main").returncode, 0)
        self.assertEqual(_sh(self.root, "git", "fetch", "-q",
                             "origin").returncode, 0)
        self.assertEqual(_sh(self.root, "git", "branch", "--set-upstream-to",
                             "origin/main", "main").returncode, 0)
        rc, _out, _err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)

    def _commit(self, name, env=None):
        with open(os.path.join(self.root, name), "w") as f:
            f.write("x\n")
        _sh(self.root, "git", "add", "-A")
        return subprocess.run(["git", "commit", "-qm", name], cwd=self.root,
                              capture_output=True, text=True, timeout=30,
                              env=env or os.environ.copy())

    def test_a_local_commit_onto_shared_main_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the behavior; unconditional positives on the same pass are the stderr text and the ref-unchanged assertion
        r = self._commit("stray.txt")
        self.assertNotEqual(r.returncode, 0, "the fork operation must refuse")
        self.assertIn("LOCAL COMMIT onto shared", r.stderr)
        self.assertIn("helm work claim", r.stderr)
        # AND THE REF NEVER MOVED — a refusal that leaves the commit is worse
        # than none, because the tree forks while the operator reads REFUSED.
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "main").stdout.strip(),
            _sh(self.root, "git", "rev-parse", "origin/main").stdout.strip())

    def test_a_missing_configured_upstream_ref_is_REFUSED(self):
        _sh(self.root, "git", "update-ref", "-d",
            "refs/remotes/origin/main")
        self.assertIn("origin", _sh(self.root, "git", "remote").stdout)
        r = self._commit("missing-upstream.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("configured upstream refs/remotes/origin/main is unavailable",
                      r.stderr)

    def test_a_remote_without_a_configured_upstream_is_REFUSED(self):
        self.assertEqual(_sh(self.root, "git", "branch", "--unset-upstream").returncode,
                         0)
        self.assertIn("origin", _sh(self.root, "git", "remote").stdout)
        r = self._commit("unconfigured-upstream.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("has remotes but no configured upstream", r.stderr)

    def test_the_configured_remote_name_is_not_assumed_to_be_origin(self):  # noqa: VACUOUS_ASSERTION — remote rename rc0 and the exact central/main refusal prove both configured-upstream resolution and the refusing effect
        self.assertEqual(_sh(self.root, "git", "remote", "rename", "origin",
                             "central").returncode, 0)
        r = self._commit("central.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refs/remotes/central/main", r.stderr)

    def test_a_SYNC_from_the_remote_is_PERMITTED(self):  # noqa: VACUOUS_ASSERTION — rc 0 rides beside an unconditional positive — landed.txt must exist in the working tree after the sync
        """THE CONTROL THAT MAKES THE REFUSAL MEAN SOMETHING. Without it a
        guard that refused every update would pass the test above and brick
        the fleet's ability to pull."""
        clone = os.path.join(self.tmp, "peer")
        self.assertEqual(_sh(self.tmp, "git", "clone", "-q", self.origin,
                             clone).returncode, 0)
        for cmd in (("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            _sh(clone, *cmd)
        with open(os.path.join(clone, "landed.txt"), "w") as f:
            f.write("from the fleet\n")
        _sh(clone, "git", "add", "-A")
        _sh(clone, "git", "commit", "-qm", "peer land")
        self.assertEqual(_sh(clone, "git", "push", "-q", "origin",
                             "main").returncode, 0)
        _sh(self.root, "git", "fetch", "-q", "origin")
        r = _sh(self.root, "git", "merge", "--ff-only", "origin/main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, "landed.txt")))

    def test_the_integrator_override_still_lands(self):
        before = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        r = self._commit("intg.txt",
                         env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # UNCONDITIONAL POSITIVE CONTROL: rc 0 alone would pass for a commit
        # that silently did nothing. The ref must actually have moved.
        after = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        self.assertNotEqual(before, after)
        self.assertIn("intg.txt", _sh(self.root, "git", "show", "--name-only",
                                      "--format=", "main").stdout)

    def test_a_repo_with_NO_REMOTE_still_commits_on_trunk(self):
        """The long-standing behaviour GuardTest documents ('committing on the
        trunk is untouched') survives for a solo checkout: no remote-tracking
        ref means no shared tree to protect and no way to prove provenance,
        and refusing there would brick a single-user helm."""
        _sh(self.root, "git", "remote", "remove", "origin")
        _sh(self.root, "git", "update-ref", "-d", "refs/remotes/origin/main")
        before = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        r = self._commit("solo.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        self.assertNotEqual(before, after, "the solo commit must really land")
        self.assertIn("solo.txt", _sh(self.root, "git", "show", "--name-only",
                                      "--format=", "main").stdout)

    def test_the_branch_create_refusal_is_UNCHANGED(self):
        """The inversion's other half stays refused — this fix removes the
        asymmetry by tightening the permissive side, never by loosening the
        strict one."""
        r = _sh(self.root, "git", "branch", "zero-risk")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("may not be created in the shared checkout", r.stderr)


class StaleGuardHookTest(WorkBase):
    """A LANDED GUARD THAT IS NOT INSTALLED IS INERT, and nothing watched.
    MEASURED 2026-08-04 on the live shared checkout: it ran reference-
    transaction v3 while trunk generated v4, so v4's peek `..`-traversal
    refusal and its landlock session-identity fix had never been armed there.
    pre-commit was stale too, which the report found and the original finding
    had not. Detection only — installing rewrites an executable in a .git and
    must never be a side effect of a read pass."""

    def test_a_freshly_installed_hook_reports_NO_drift(self):
        """THE CONTROL. Without it a detector that flagged everything would
        satisfy the drift test below and be useless."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the hook
        # must EXIST and be non-empty, or "no drift" means "nothing compared".
        target = work.hook_path(self.root, "reference-transaction")
        self.assertTrue(os.path.getsize(target) > 0)
        self.assertEqual(work._guard.stale_guard_hooks(self.root), [])

    # noqa: VACUOUS_ASSERTION — the positive control on THIS observable
    # is in this same test: after drifting one snapshot, the identical
    # stale_guard_hooks(self.root) call is asserted NON-empty and exact.
    # The clean-rail [] above is the baseline half of that pair, and the
    # rung reads them independently. Suppressed with the control named,
    # not because the warning was inconvenient.
    def test_a_STALE_SCANNER_is_drift_even_when_every_hook_matches(self):
        """THE HOLE THIS FUNCTION SHIPPED WITH. A hook is a few lines of shell
        that DELEGATES to a snapshot under .helm-scanners — never-track, the
        in-flight gate, the vacuity advisory all live there. So a scanner can
        land while its installed copy keeps enforcing the old rules with every
        HOOK byte-identical.

        Found by sweeping prior art after building: `helm doctor`'s
        check_work_guard already read both halves, and this predicate read only
        the hooks — strictly weaker than the check it was built to move to a
        louder surface. The #92 class, in the file class the lane is about."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        # UNCONDITIONAL POSITIVE CONTROL, on the same observable: the scanner
        # snapshots must EXIST and be non-empty before "no drift" can mean
        # compared-and-clean rather than nothing-to-compare. This is the exact
        # failure the predicate itself shipped with, so the test must not
        # repeat it.
        snaps = sorted(work._guard._scanner_assets(self.root))
        self.assertTrue(snaps)
        for installed in snaps:
            self.assertTrue(os.path.getsize(installed) > 0, installed)
        self.assertEqual(work._guard.stale_guard_hooks(self.root), [])

        snap = next(p for p in snaps if p.endswith("nevertrack.py"))
        with open(snap, "a") as f:
            f.write("\n# drifted\n")

        drift = work._guard.stale_guard_hooks(self.root)
        self.assertEqual([(st, n) for st, n, _w in drift],
                         [("STALE", "scanner:nevertrack.py")])
        # AND EVERY HOOK STILL MATCHES — proving the hook loop alone is blind
        # to this, which is exactly why the predicate had to widen.
        for p in work._guard._guard_plan(self.root)[1]:
            with open(p["target"]) as f:
                self.assertEqual(f.read(), p["script"])

    def test_a_MISSING_scanner_is_reported_not_silent(self):
        """A hook that delegates to a file which is not there fails OPEN in the
        worst way: git runs the hook, the scanner is absent, and enforcement
        simply does not happen."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(work._guard.stale_guard_hooks(self.root), [])
        snap = next(p for p in work._guard._scanner_assets(self.root)
                    if p.endswith("inflight_gate.py"))
        os.remove(snap)
        self.assertEqual(
            [(st, n) for st, n, _w in work._guard.stale_guard_hooks(self.root)],
            [("MISSING", "scanner:inflight_gate.py")])

    def test_an_EDITED_hook_is_reported_as_drift(self):
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        target = work.hook_path(self.root, "reference-transaction")
        with open(target, "a") as f:
            f.write("\n# an older ruleset\n")
        drift = work._guard.stale_guard_hooks(self.root)
        self.assertIn(("STALE", "reference-transaction"),
                      [(state, name) for state, name, _why in drift])

    def test_an_ABSENT_hook_is_reported_as_missing(self):
        target = work.hook_path(self.root, "reference-transaction")
        self.assertFalse(os.path.exists(target), "the hook must really be absent")
        findings = work._guard.stale_guard_hooks(self.root)
        self.assertIn(("MISSING", "reference-transaction"),
                      [(state, name) for state, name, _why in findings])
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("GUARD-MISSING reference-transaction", out)

    def test_an_UNREADABLE_hook_is_reported_as_unknown(self):
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        target = work.hook_path(self.root, "reference-transaction")
        os.chmod(target, 0)
        findings = work._guard.stale_guard_hooks(self.root)
        self.assertIn(("UNKNOWN", "reference-transaction"),
                      [(state, name) for state, name, _why in findings])

    def test_an_UNRENDERABLE_plan_is_reported_as_unknown(self):
        with mock.patch.object(_work_guard, "_guard_plan",
                               side_effect=OSError("plan unreadable")):
            self.assertEqual(
                work._guard.stale_guard_hooks(self.root),
                [("UNKNOWN", "guard-plan",
                  "cannot render the expected hooks: OSError: plan unreadable")])

    def test_gc_REPORTS_the_drift_and_never_installs(self):  # noqa: VACUOUS_ASSERTION — never-installs is the claim; the unconditional positive on the same run is assertIn GUARD-STALE
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        target = work.hook_path(self.root, "reference-transaction")
        with open(target, "a") as f:
            f.write("\n# an older ruleset\n")
        with open(target) as f:
            before = f.read()
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("GUARD-STALE reference-transaction", out)
        self.assertIn("install-guard --apply", out)
        with open(target) as f:
            after = f.read()
        self.assertEqual(after, before,
                         "a read pass must never rewrite a hook")


class LandlockGuardTest(WorkBase):
    """LANDLOCK enforced in the ref-transaction hook (three same-day land
    races 2026-07-29 — SI merged inside CD's held window 8 minutes after the
    convention was POSTED; a mutex that requires reading the room is not a
    mutex). Edge policy: no claim = PASS; unreadable claims = PASS + loud
    warning; unresolvable identity = PASS + warning; HELM_LANDLOCK=0
    disables THIS check alone. It narrows races; it is not an auth gate.

    "UNRESOLVABLE" NARROWED 2026-08-02. It used to mean "$HELM_CHAT_NAME is
    empty", which is the RESTING STATE of every claude-direct seat — so the
    warning arm was the ordinary path for a whole family and the narrower
    narrowed nothing for them. Claim rows already record the ambient harness
    session and bind it on refresh/release. The reader now compares that value
    directly, without importing identity code from the candidate worktree.
    Unresolvable now means neither a declared seat nor both sides of the session
    binding are available. That still PASSES with the warning — the fail-open is
    the point and it is unchanged. What changed is only who falls into it."""

    def setUp(self):
        super().setUp()
        rc, _o, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.claims = os.path.join(self.tmp, "claims.json")
        self._env = dict(os.environ,
                         HELM_LANDLOCK_CLAIMS=self.claims)

    def _write_claims(self, holder="cd", exp_mono=None):
        import json as _json
        row = {"holder": holder, "session": "s-x", "lease": "l",
               "fence": 1,
               "exp_mono": exp_mono if exp_mono is not None
                           else __import__("time").monotonic() + 900}
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": row, "_fence": 1}, f)

    def _commit_on_main(self, env=None):
        with open(os.path.join(self.root, "README"), "a") as f:
            f.write("land\n")
        return subprocess.run(["git", "commit", "-qam", "land"],
                              cwd=self.root, capture_output=True, text=True,
                              timeout=30, env=env or self._env)

    def _nameless_env(self, session=None):
        env = dict(self._env)
        for key in ("HELM_CHAT_NAME", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
            env.pop(key, None)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return env

    def test_held_by_anOTHER_seat_refuses_and_names_the_holder(self):
        self._write_claims(holder="cd")
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("landlock:helm is held by cd", r.stderr)
        self.assertIn("HELM_LANDLOCK=0", r.stderr)

    def test_held_by_ME_with_the_RECORDED_session_passes_silently(self):  # noqa: VACUOUS_ASSERTION — each carries a POSITIVE control on the same commit observable (a foreign seat must be REFUSED); non-vacuity proven by mutation, not shape: disabling enforcement (`if True` at the nobody-holds-it arm) fails all three
        """The real holder: name AND the session the claim writer recorded.

        SUPERSEDES test_held_by_ME_passes_silently, which set only the name
        and asserted a silent PASS — that test PINNED THE BYPASS GREEN. A
        name alone is the one credential the candidate supplies for itself,
        so any future fix would have read as a regression against it."""
        self._write_claims(holder="kimi")
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi",
                                          CLAUDE_CODE_SESSION_ID="s-x"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("landlock", r.stderr)
        # POSITIVE CONTROL on the same observable: a silent pass is also what
        # a DISABLED hook produces, so prove the hook was armed during it.
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="cd",
                                          CLAUDE_CODE_SESSION_ID="s-other"))
        self.assertIn("REFUSED: landlock:helm is held by kimi", r.stderr,
                      "hook was not armed")

    def test_an_INHERITED_name_with_a_FRESH_session_is_REFUSED(self):
        """THE P0: the candidate declares the holder's name and is not it.

        Measured twice 2026-08-02 as an ACCIDENT, no adversary — a seat ran
        session-unbound with its name intact, and another auto-bound to the
        wrong roster row at reboot. Both would have committed against a lock
        another seat held, and the hook would have reported success."""
        self._write_claims(holder="opus-integrator")
        r = self._commit_on_main(env=dict(self._env,
                                          HELM_CHAT_NAME="opus-integrator",
                                          CLAUDE_CODE_SESSION_ID="s-fresh"))
        # assert the REFUSAL, never the advice wording — this test owns the
        # gate, and test_the_refusal_gives_IDENTITY_advice owns the message.
        # Asserting the advice here made both die to a message-only mutation.
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("REFUSED: landlock:helm is held by opus-integrator",
                      r.stderr)

    def test_CLEARING_the_session_does_not_reopen_the_name_alone_door(self):
        """The obvious escape from the fix, closed by construction.

        The refusal is gated on whether THE ROW records a session, never on
        whether the CANDIDATE supplies one — so unsetting your own session
        cannot buy back the name-alone path."""
        self._write_claims(holder="opus-integrator")
        env = self._nameless_env()
        env["HELM_CHAT_NAME"] = "opus-integrator"
        r = self._commit_on_main(env=env)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("REFUSED: landlock:helm is held by opus-integrator",
                      r.stderr)

    def test_the_refusal_gives_IDENTITY_advice_not_wait_for_the_lock(self):
        """A refusal that misroutes the operator is its own defect: told to
        'coordinate the release', a seat whose own name matches would sit out
        a timer that can never fix an identity binding."""
        self._write_claims(holder="opus-integrator")
        r = self._commit_on_main(env=dict(self._env,
                                          HELM_CHAT_NAME="opus-integrator",
                                          CLAUDE_CODE_SESSION_ID="s-fresh"))
        self.assertIn("seat disown", r.stderr)
        self.assertNotIn("land after it expires", r.stderr)

    def test_a_NAMELESS_holder_with_the_recorded_session_still_passes(self):  # noqa: VACUOUS_ASSERTION — each carries a POSITIVE control on the same commit observable (a foreign seat must be REFUSED); non-vacuity proven by mutation, not shape: disabling enforcement (`if True` at the nobody-holds-it arm) fails all three
        """THE BOUNDARY the fix must not cross. A legitimate holder with no
        declared name is admitted by the session alone, exactly as before —
        this change narrows only the name-alone door."""
        self._write_claims(holder="kimi")
        r = self._commit_on_main(env=self._nameless_env(session="s-x"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL: same nameless shape, WRONG session must refuse —
        # otherwise this arm cannot tell admission from an inert hook.
        r = self._commit_on_main(env=self._nameless_env(session="s-other"))
        self.assertIn("REFUSED: landlock:helm is held by kimi", r.stderr,
                      "hook was not armed")

    def test_a_LEGACY_row_with_NO_session_still_admits_its_named_holder(self):  # noqa: VACUOUS_ASSERTION — each carries a POSITIVE control on the same commit observable (a foreign seat must be REFUSED); non-vacuity proven by mutation, not shape: disabling enforcement (`if True` at the nobody-holds-it arm) fails all three
        """A claim predating session recording has only the name, so refusing
        would brick a real holder over a field their row cannot carry.
        Measured 2026-08-02: 0 of 4 live rows are this shape."""
        import json as _json
        import time as _time
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": {"holder": "kimi", "lease": "l",
                                          "fence": 1,
                                          "exp_mono": _time.monotonic() + 900},
                        "_fence": 1}, f)
        r = self._commit_on_main(env=self._nameless_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        # POSITIVE CONTROL: on this same legacy row a FOREIGN name must still
        # refuse. Without it, "legacy rows admit their holder" is
        # indistinguishable from "legacy rows admit everyone".
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="cd"))
        self.assertIn("REFUSED: landlock:helm is held by kimi", r.stderr,
                      "legacy row admitted a foreign seat")

    def test_a_name_only_admission_is_WARNED_never_silent(self):
        """The one door a declared name can still open must be VISIBLE.

        A sessionless claim is REACHABLE, not historical: `helm chat claim`
        takes the session from the ambient harness env only, so a claim
        minted from a bare shell or a cron unit records session=None
        (measured: _env_session() -> None with no harness env). We fail open
        there so a real holder is not locked out of their own lock — but a
        silent fail-open would hide the exact shape that can still be
        spoofed, so it warns and says WHICH door it used."""
        import json as _json
        import time as _time
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": {"holder": "kimi", "lease": "l",
                                          "fence": 1,
                                          "exp_mono": _time.monotonic() + 900},
                        "_fence": 1}, f)
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("name-only", r.stderr)
        self.assertIn("claim records no session", r.stderr)
        # and it must NOT be rendered as the unreadable-claims warning, which
        # would tell the reader identity was unknown when it was merely
        # uncorroborated
        self.assertNotIn("claims unreadable/identity unknown", r.stderr)

    def test_NO_claim_at_all_passes_silently(self):
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("landlock", r.stderr)

    def test_an_EXPIRED_claim_passes(self):
        import time
        self._write_claims(holder="cd", exp_mono=time.monotonic() - 1)
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unreadable_claims_warns_and_passes(self):
        with open(self.claims, "w") as f:
            f.write("{not json")
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("landlock", r.stderr)
        self.assertIn("WARNING", r.stderr)

    def _plant_candidate_helm(self, name):
        """Candidate-controlled identity code that the installed hook must not
        import while deciding whether that candidate may update main."""
        pkg = os.path.join(self.root, "helm")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(pkg, "seats.py"), "w") as f:
            f.write("def safe_cwd():\n    return '.'\n\n"
                    "def acting_seat(session=None, cwd=None):\n"
                    "    return %r\n" % (name,))
        self.assertEqual(_sh(self.root, "git", "add", "helm").returncode, 0)

    def test_a_seat_that_declared_NO_name_is_now_BOUND_by_the_lock(self):
        """THE BUG. $HELM_CHAT_NAME empty is not an exotic edge — it is how
        every claude-direct seat runs. Its ambient session is enough to prove
        that a live claim belongs to a different session."""
        self._write_claims(holder="cd")
        r = self._commit_on_main(env=self._nameless_env("s-other"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("landlock:helm is held by cd", r.stderr)

    def test_the_ANTI_BRICK_control_the_bound_session_is_ME_so_it_passes(self):
        """The half that must NOT change: the session recorded by the writer
        proves this is my own land window without consulting candidate code."""
        self._write_claims(holder="helm-claude-2")
        r = self._commit_on_main(env=self._nameless_env("s-x"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL on the SAME observable (r.stderr): the managed hook
        # chain demonstrably RAN. Without this, "no landlock line on stderr"
        # is equally satisfied by a run where no hook fired at all — and that
        # is the difference between "the check ran and stayed silent" and
        # "the check never happened", which is the whole claim of this test.
        self.assertIn("[helm", r.stderr)
        self.assertNotIn("landlock", r.stderr)
        landed = _sh(self.root, "git", "log", "--oneline", "-1", "main").stdout
        self.assertIn("land", landed)

    def test_candidate_identity_code_cannot_speak_for_the_guard(self):
        """THE REVIEW FINDING. A reference-transaction hook runs with the
        candidate files already in the worktree. Importing helm.seats there let
        a candidate return the foreign holder's name and pass its own guard."""
        self._write_claims(holder="cd")
        self._plant_candidate_helm("cd")
        r = self._commit_on_main(env=self._nameless_env("s-other"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("landlock:helm is held by cd", r.stderr)

    def test_unresolvable_identity_warns_and_passes(self):
        self._write_claims(holder="cd")
        r = self._commit_on_main(env=self._nameless_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING", r.stderr)

    def test_a_hostile_holder_name_cannot_inject_the_refusal_line(self):
        import json as _json, time
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": {
                "holder": "cd$(touch /tmp/landlock-injected)",
                "session": "s", "lease": "l", "fence": 1,
                "exp_mono": time.monotonic() + 900}, "_fence": 1}, f)
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("held by ?", r.stderr)
        self.assertFalse(os.path.exists("/tmp/landlock-injected"))

    def test_kill_switch_disables_THIS_check_alone(self):
        self._write_claims(holder="cd")
        env = dict(self._env, HELM_CHAT_NAME="kimi", HELM_LANDLOCK="0")
        r = self._commit_on_main(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # and the REST of the guard still bites under the same switch
        r2 = subprocess.run(["git", "checkout", "-b", "still-guarded"],
                            cwd=self.root, capture_output=True, text=True,
                            timeout=30, env=env)
        self.assertEqual(r2.returncode, 128, r2.stderr)
        self.assertIn("REFUSED", r2.stderr)


class PeekTtlTest(WorkBase):
    """#157 — stale peeks are TTL-reaped by `work gc`, the owner's fifth ask
    for the same sidebar recurrence. Detection (stale_peeks) is age-only BY
    DESIGN: a peek is read-only by construction, and every live-room refusal
    (occupant, pane, dirt) stays in peek_drop, the ONLY remover."""

    def setUp(self):
        super().setUp()
        rc, out, _err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, out)
        self.peek_path = out.strip().splitlines()[0].split("\t")[0]
        self.assertTrue(os.path.isdir(self.peek_path))

    def _backdate(self, seconds):
        past = time.time() - seconds
        os.utime(self.peek_path, (past, past))

    def test_reuse_resets_the_idleness_clock(self):
        # codex-2's REWORK repro 3: a pure re-issue wrote
        # nothing, so a room reused at hour 3 still read 3h idle and the
        # NEXT tick could reap it out from under its reuser.
        from helm.work import _peek
        self._backdate(3 * 3600)
        self.assertTrue(_peek.stale_peeks(self.root),
                        "control: the room must read idle before the reuse")
        rc, out, _err = self.work("peek", "HEAD")   # converges on the room
        self.assertEqual(rc, 0, out)
        self.assertEqual(_peek.stale_peeks(self.root), [],
                         "a reused room just proved it is not abandoned")

    def test_reuse_refuses_when_its_activity_refresh_fails(self):
        from helm.work import _peek
        self._backdate(3 * 3600)
        with mock.patch.object(_peek.os, "utime",
                               side_effect=OSError("read-only mount")):
            rc, payload = _peek.peek(self.root, "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("cannot refresh peek activity", payload["error"])
        self.assertTrue(os.path.isdir(self.peek_path),
                        "a failed refresh refuses reuse but destroys nothing")

    def test_reuse_after_stale_scan_refuses_the_drop_at_mutation_boundary(self):
        """Reviewer FIX: the stale list is only a candidate set.

        Reuse can refresh the room after that scan. The shared activity lock
        plus drop-side TTL recheck must consume the fresh fact, not the stale
        candidate, or the cadence deletes a room under its reuser."""
        from helm.work import _peek
        self._backdate(3 * 3600)
        self.assertTrue(_peek.stale_peeks(self.root),
                        "control: cadence must first select the stale room")
        real_drop = _peek.peek_drop

        def reuse_then_drop(root, path, **kwargs):
            rc, payload = _peek.peek(root, "HEAD")
            self.assertEqual(rc, 0, payload)
            self.assertTrue(payload["reused"])
            return real_drop(root, path, **kwargs)

        with mock.patch.object(_peek, "peek_drop",
                               side_effect=reuse_then_drop):
            rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "freshly reused room must survive the stale candidate")
        self.assertIn("activity refreshed", out)
        self.assertIn("removed=0", out)
        self.assertIn("kept=1", out)

    # noqa: VACUOUS_ASSERTION — the assertFalse(isdir) rides between two
    # unconditional positives on the same pass: setUp proves the room
    # existed, and assertIn("removed=1") reads the drop off the summary.
    def test_summary_counts_a_dropped_peek_with_no_lane_rows(self):
        # codex-2's REWORK repro 1: the no-rows branch discarded
        # peek_pass's counts, so this exact pass reported removed=0.
        self._backdate(3 * 3600)
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(self.peek_path),
                         "positive control: the peek was really dropped")
        self.assertIn("removed=1", out)
        self.assertIn("kept=0", out)

    # noqa: VACUOUS_ASSERTION — assertNotIn("kept=-1") is the regression
    # pin; the unconditional positives on the SAME summary line are
    # assertIn("removed=2") and assertIn("kept=1"), and both room drops
    # are asserted against rooms setUp/this test proved existed.
    def test_summary_counts_peeks_beside_lanes_and_kept_never_negative(self):
        # codex-2's REWORK repro 2: peek drops were folded into the LANE
        # removed count, so len(rows)-removed printed kept=-1 on exactly
        # this shape — one kept lane, two dropped peeks.
        rc, out, _err = self.work("claim", "heldlane", "--seat", "s1")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.root, "second"), "w") as f:
            f.write("x\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "second")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, out, _err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, out)
        second = out.strip().splitlines()[0].split("\t")[0]
        self.assertNotEqual(second, self.peek_path)
        past = time.time() - 3 * 3600
        os.utime(self.peek_path, (past, past))
        os.utime(second, (past, past))
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(self.peek_path))
        self.assertFalse(os.path.isdir(second))
        self.assertIn("removed=2", out)
        self.assertIn("kept=1", out, "the held lane is the 1 kept")
        self.assertNotIn("kept=-1", out)

    def test_a_fresh_peek_is_not_stale_and_an_idle_one_is(self):
        from helm.work import _peek
        self.assertEqual(_peek.stale_peeks(self.root), [],
                         "a fresh peek must never be listed")
        self._backdate(3 * 3600)
        rows = _peek.stale_peeks(self.root)
        self.assertEqual([p for p, _ in rows], [self.peek_path])
        self.assertGreaterEqual(rows[0][1], 3 * 3600 - 5)

    def test_gc_dry_run_names_the_stale_peek_and_drops_nothing(self):
        self._backdate(3 * 3600)
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("PEEK-STALE", out)
        self.assertIn(os.path.basename(self.peek_path), out)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "dry run must never drop a room")

    def test_gc_apply_drops_the_idle_peek(self):
        self._backdate(3 * 3600)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "positive control: the room must exist before the "
                        "apply, or the drop assertion below is vacuous")
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(self.peek_path),
                         "the idle peek must be dropped by --apply")
        self.assertIn("dropped", out)

    def test_an_OCCUPIED_stale_peek_is_kept_with_the_refusal_printed(self):
        self._backdate(3 * 3600)
        with mock.patch("helm.work._peek._occupants",
                        return_value=["12345"]):
            rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "an occupied room must survive the apply")
        self.assertIn("OCCUPIED", out)

    def test_the_shipped_timer_service_runs_the_work_reap_leg(self):
        from helm import gc as streamgc
        _sp, service, _tp, timer = streamgc.timer_units()
        # EXECUTABLE lines only — the template's own comment mentions the
        # same phrase, and a prose match let mutation M3 survive (the
        # tripwire-counts-prose-as-call-sites class, caught in this lane).
        exec_lines = [l for l in service.splitlines()
                      if l.startswith("ExecStart=")]
        self.assertTrue(any("work gc --apply" in l for l in exec_lines),
                        "the cadence must EXECUTE the worktree reap leg "
                        "(#157), not merely mention it: %r" % exec_lines)
        self.assertTrue(any(l.endswith("gc --apply") or " gc --apply" in l
                            for l in exec_lines))
        self.assertIn("OnUnitActiveSec", timer)


class PeekTest(WorkBase):
    """`helm work peek` — the read-only door to an arbitrary landed sha.

    THE GAP (fleet-measured, 2026-08-01): a REVIEWER had no sanctioned path to
    a point-in-time checkout — the shared checkout's HEAD rule refuses any
    departure, `work claim` mints a lane room (lease + branch + write intent,
    the wrong shape for a look), and the integrator override is
    integrator-only. The gap reproduced itself during its own fix: the build
    agent assigned to this feature was first refused by the ref guard when its
    harness tried to mint a scratch worktree.

    Verb-level tests here run WITHOUT the guard installed (the door must work
    on its own terms); PeekGuardTest below is the guard leg."""

    def setUp(self):
        super().setUp()
        # a second commit so the peeked sha is NOT the tip: the door's whole
        # point is an ARBITRARY landed sha, not a spelling of HEAD
        with open(os.path.join(self.root, "README"), "a") as f:
            f.write("more\n")
        r = _sh(self.root, "git", "commit", "-qam", "second")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.old = _sh(self.root, "git", "rev-parse",
                       "HEAD~1").stdout.strip()

    def test_peek_yields_a_detached_room_at_exactly_that_sha(self):
        rc, out, err = self.work("peek", "HEAD~1")
        self.assertEqual(rc, 0, err)
        path, sha = out.strip().split("\t")
        self.assertEqual(sha, self.old)
        self.assertEqual(path, work.peek_path(self.root, self.old))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(_sh(path, "git", "rev-parse", "HEAD").stdout.strip(),
                         self.old)                       # exactly, not the tip
        r = _sh(path, "git", "symbolic-ref", "-q", "--short", "HEAD")
        self.assertNotEqual(r.returncode, 0, "a peek room is on a BRANCH")
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertIsNone(row["branch"] or None)         # never a branch ref
        self.assertFalse(row["locked"])                  # disposable, no lock

    def test_the_same_sha_converges_on_the_same_room(self):
        rc1, out1, _e1 = self.work("peek", self.old)
        rc2, out2, err2 = self.work("peek", "HEAD~1")    # different spelling
        self.assertEqual((rc1, rc2), (0, 0), err2)
        self.assertIn(self.old, out2)        # non-empty: the sha itself printed
        self.assertEqual(out1.strip(), out2.strip())
        rows = work.peek_rows(self.root)
        self.assertEqual([r["name"] for r in rows], [self.old[:12]])

    def test_a_garbage_committish_refuses_with_gits_own_words(self):
        # MUST-HIT first: the door provably opens here, so the refusal below
        # is the committish's fault and "nothing new minted" is measurable
        # against a room that exists rather than against a vacuum
        rc, out, _e = self.work("peek", self.old)
        self.assertEqual(rc, 0)
        self.assertIn(self.old, out)
        rc, out, err = self.work("peek", "no-such-committish")
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve 'no-such-committish'", err)
        self.assertIn("fatal", err)                      # git's words, forwarded
        self.assertEqual(out, "")
        rows = work.peek_rows(self.root)
        self.assertEqual([r["name"] for r in rows], [self.old[:12]])

    def test_an_uncreatable_peek_area_refuses_and_mints_nothing(self):
        with open(self.root + "-wt", "w") as f:
            f.write("a file squatting on the container\n")
        rc, _out, err = self.work("peek", self.old)
        self.assertEqual(rc, 1)
        self.assertIn("cannot create peek area", err)
        registry = [w["path"] for w in work.worktrees(self.root)]
        self.assertEqual(registry, [self.root])   # exactly the main checkout

    def test_a_room_that_moved_off_its_sha_is_not_reissued(self):
        rc, out, _e = self.work("peek", self.old)
        self.assertEqual(rc, 0)
        path = out.split("\t")[0]
        r = _sh(path, "git", "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "someone worked here")
        commit_rc = r.returncode
        self.assertEqual(commit_rc, 0, r.stderr)
        # the premise, proven not assumed: the room's HEAD really moved
        moved = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(moved), 40)     # a real sha, not an error's ""
        self.assertNotEqual(moved, self.old)
        rc, _out, err = self.work("peek", self.old)
        self.assertEqual(rc, 1, "a moved room was silently re-issued")
        self.assertIn("no longer a clean peek", err)
        self.assertIn("--drop", err)                     # the honest exit, named
        # and nothing touched: the room still sits at the commit someone made
        after = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(after), 40)
        self.assertEqual(after, moved)

    def test_drop_by_path_and_by_committish_both_remove(self):
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        self.assertTrue(os.path.isdir(path))     # the room to remove IS there
        rc, out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("dropped", out)
        registry = [w["path"] for w in work.worktrees(self.root)]
        self.assertEqual(registry, [self.root])  # exactly the main survives
        self.assertFalse(os.path.exists(path))
        rc, out, _e = self.work("peek", self.old)        # mint again
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(path))
        rc, out, err = self.work("peek", "--drop", "HEAD~1")
        self.assertEqual(rc, 0, err)
        registry = [w["path"] for w in work.worktrees(self.root)]
        self.assertEqual(registry, [self.root])
        self.assertFalse(os.path.exists(path))

    def test_drop_refuses_a_target_that_is_not_a_peek_room(self):
        # NO CONTAINER AT ALL: no peek was ever cut in this estate, and the
        # verdict must say THAT — "not a registered peek room" here sent the
        # caller checking one room's registration when no rooms exist
        rc, _out, err = self.work("peek", "--drop", "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("no peek container", err)
        self.assertNotIn("not a registered peek room", err)
        # container PRESENT, this room absent: the registration verdict
        rc, _out, _e = self.work("peek", self.old)   # mints the container
        self.assertEqual(rc, 0)
        rc, _out, err = self.work("peek", "--drop", "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("not a registered peek room", err)
        # a LANE room path must never leave through the peek door
        rc, out, _e = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0)
        lane_path = out.split("\t")[0]
        rc, _out, err = self.work("peek", "--drop", lane_path)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.isdir(lane_path), "peek --drop removed a lane room")
        # an unresolvable non-path refuses on the resolve arm
        rc, _out, err = self.work("peek", "--drop", "garbage-target")
        self.assertEqual(rc, 1)
        self.assertIn("resolvable commit", err)

    def test_drop_refuses_an_occupied_room(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        self.addCleanup(proc.wait)         # cleanup, so the assertions below
        self.addCleanup(proc.terminate)    # are UNCONDITIONAL, not try-guarded
        rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("OCCUPIED", err)
        self.assertIn(str(proc.pid), err)  # the refusal names the occupant
        self.assertTrue(os.path.isdir(path))

    def test_drop_refuses_a_dirty_room_with_gits_own_refusal(self):
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        junk = os.path.join(path, "scribbles.txt")
        with open(junk, "w") as f:
            f.write("a peek that grew work\n")
        rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("peek drop refused", err)
        self.assertTrue(os.path.exists(junk), "--drop discarded bytes")

    def test_a_pane_bound_peek_room_refuses_drop(self):
        """Same law as gc's remover: a metaharness pane outlives its shell,
        and deleting under one leaves the operator a prompt at a dead cwd."""
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        from helm.work import _peek as _work_peek
        with mock.patch.object(_work_peek, "_panes_bound_to",
                               return_value=(["term_deadbeef"], None)):
            rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("term_deadbeef", err)
        with mock.patch.object(_work_peek, "_panes_bound_to",
                               return_value=(None, "daemon down")):
            rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("cannot prove", err)
        self.assertTrue(os.path.isdir(path))

    def test_a_peek_room_is_never_a_lane_room(self):
        rc, out, _e = self.work("peek", self.old)
        self.assertEqual(rc, 0)
        path = out.split("\t")[0]
        # MUST-HIT first: the classifier does see the room, as its own kind
        self.assertEqual(work.managed_room_kind(self.root, path), "peek")
        registered = work.worktrees(self.root)
        self.assertNotIn(path, [r["path"] for r in
                                work.lane_rows(self.root, registered=registered)])
        self.assertNotIn(path, [r["path"] for r in work.gc_scan(self.root)])
        self.assertNotIn(path, [r["path"] for r in
                                work.list_rows(self.root, registered=registered)])
        # release can never aim at it: inference answers None inside the room
        self.assertIsNone(work._infer_lane(self.root, path))
        # and the container name cannot be claimed on top of
        rc, _out, err = self.work("claim", work.PEEK_DIRNAME)
        self.assertEqual(rc, 2)
        self.assertIn("reserved", err)
        # the board shows it in its OWN section, not as GUARDED
        rc, out, _e = self.work("list")
        self.assertEqual(rc, 0)
        self.assertIn("PEEK %s" % os.path.basename(path), out)
        self.assertNotRegex(out, r"GUARDED.*%s" % os.path.basename(path))

    def test_peek_json_carries_path_sha_and_reuse(self):
        rc, out, err = self.work("peek", self.old, "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["sha"], self.old)
        self.assertEqual(got["path"], work.peek_path(self.root, self.old))
        self.assertFalse(got["reused"])
        rc, out, _e = self.work("peek", self.old, "--json")
        self.assertTrue(json.loads(out)["reused"])

    def test_drop_and_a_committish_together_refuse_as_ambiguous(self):
        rc, _out, err = self.work("peek", "--drop", "HEAD", "HEAD~1")
        self.assertEqual(rc, 2)
        self.assertIn("not both", err)


class PeekGuardTest(WorkBase):
    """The ref guard admits a peek birth STRUCTURALLY — no env override.

    MEASURED 2026-08-01 (the design's load-bearing fact): `git worktree add
    --detach` births the new worktree's HEAD in MAIN-TREE context, and its
    stdin line (`0000.. <sha> HEAD`, git-dir == common) is byte-identical to
    `git checkout --detach` detaching the shared checkout's own HEAD. The
    discriminator is the HALF-BORN ADMIN DIR: at `prepared` time
    `$common/worktrees/<name>/gitdir` exists and its HEAD does not. Each test
    below fails exactly one clause of that allowance."""

    def setUp(self):
        super().setUp()
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("HELM_WORK_INTEGRATOR", os.environ)   # the point
        self.sha = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()

    def test_the_guard_admits_a_peek_birth_without_any_env(self):
        self.assertEqual(len(self.sha), 40)             # the target is real
        rc, out, err = self.work("peek", self.sha)
        self.assertEqual(rc, 0, err)
        self.assertIn(self.sha, out)                    # the door printed it
        path = out.split("\t")[0]
        head = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(head), 40)                 # a real sha came back
        self.assertEqual(head, self.sha)
        # the shared checkout never moved
        shared = _sh(self.root, "git", "symbolic-ref", "--short",
                     "HEAD").stdout.strip()
        self.assertEqual(shared, "main")
        # and the raw git spelling of the same door is equally sanctioned —
        # the allowance is the AREA, not the helm binary
        raw = os.path.join(work.peek_area(self.root), "raw-spelling")
        r = _sh(self.root, "git", "worktree", "add", "--detach", raw, self.sha)
        raw_rc = r.returncode
        self.assertEqual(raw_rc, 0, r.stderr)
        self.assertTrue(os.path.isdir(raw))

    def test_a_detached_add_OUTSIDE_the_peek_area_is_still_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        r = _sh(self.root, "git", "worktree", "add", "--detach", outside,
                self.sha)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)      # positive: the guard SPOKE
        # refusal leaves no room; the same operation INTO the area opening
        # (test_the_guard_admits_a_peek_birth_without_any_env) is the paired
        # MUST-HIT proving this red is the area's fault, not a broken add
        self.assertFalse(os.path.exists(outside))  # noqa: VACUOUS_ASSERTION — refusal leaves nothing; paired open-door control is test_the_guard_admits_a_peek_birth_without_any_env

    def test_a_branch_add_INTO_the_peek_area_is_still_refused(self):
        """Detached-only, never a branch ref — the area alone opens nothing."""
        # the branch oracle can say yes (else the absence below proves nothing)
        self.assertTrue(work._has_branch(self.root, "main"))
        inside = os.path.join(work.peek_area(self.root), "branchy")
        os.makedirs(work.peek_area(self.root), exist_ok=True)
        r = _sh(self.root, "git", "worktree", "add", "-b", "lane/branchy",
                inside, "main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)      # positive: the guard SPOKE
        self.assertFalse(work._has_branch(self.root, "lane/branchy"))  # noqa: VACUOUS_ASSERTION — the ref never existing IS the contract; the oracle's yes-arm is pinned above on main

    def test_detaching_the_shared_checkout_itself_is_still_refused(self):
        """THE MUST-NOT-OPEN CONTROL. This operation's stdin is byte-identical
        to the sanctioned birth; only the absent half-born admin dir tells
        them apart. If this ever passes, the discriminator broke and the
        integrator's tree is detachable by anyone."""
        r = _sh(self.root, "git", "checkout", "--detach")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("helm work peek", r.stderr)     # the door is signposted
        self.assertEqual(_sh(self.root, "git", "symbolic-ref", "--short",
                             "HEAD").stdout.strip(), "main")

    def test_one_stray_half_born_admin_refuses_ALL_births(self):
        """Fail toward refusal: with a concurrent non-peek birth in flight the
        transaction cannot be attributed, so the peek is refused too — a
        spurious refusal retries; a spurious pass detaches the shared tree."""
        stray = os.path.join(self.root, ".git", "worktrees", "stray")
        os.makedirs(stray)
        with open(os.path.join(stray, "gitdir"), "w") as f:
            f.write(os.path.join(self.tmp, "elsewhere", ".git") + "\n")
        rc, _out, err = self.work("peek", self.sha)
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)
        # THE CONTROL: with the stray gone the same peek passes — proving the
        # refusal above came from the stray, not from a door that never opens
        shutil.rmtree(stray)
        rc, _out, err = self.work("peek", self.sha)
        self.assertEqual(rc, 0, err)

    def test_a_dotdot_gitdir_component_is_refused_by_name(self):
        """kimi's traversal (2026-08-02): a half-born admin whose gitdir STARTS
        WITH the sanctioned prefix but carries `../../` passes the AS-SPELLED
        prefix compare yet RESOLVES OUTSIDE the peek area. Git normally
        normalizes the path it writes, so the guard must not lean on that — a
        crafted/aliased admin can carry the traversal literally. The guard now
        refuses the '..' BY NAME before the prefix compare can be walked out."""
        peeks = work.peek_area(self.root)
        os.makedirs(peeks, exist_ok=True)
        # a stray half-born admin (gitdir present, HEAD absent) spelled THROUGH
        # peeks/ but resolving to a sibling of the repo
        stray = os.path.join(self.root, ".git", "worktrees", "traversal")
        os.makedirs(stray)
        evil = peeks + "/../../escape/.git"
        with open(os.path.join(stray, "gitdir"), "w") as f:
            f.write(evil + "\n")
        self.assertTrue(evil.startswith(peeks + "/"))      # the prefix DID match
        self.assertNotEqual(os.path.realpath(os.path.dirname(evil)),
                            os.path.realpath(peeks))        # yet it escapes
        target = os.path.join(peeks, self.sha[:12])
        r = _sh(self.root, "git", "worktree", "add", "--detach", target,
                self.sha)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)      # positive: the guard SPOKE
        self.assertIn("'..'", r.stderr)         # and it NAMES the traversal
        self.assertFalse(os.path.exists(target))
        # THE CONTROL: with the stray gone the same birth opens — proving the
        # red above came from the '..' gitdir, not a door that never opens
        shutil.rmtree(stray)
        r2 = _sh(self.root, "git", "worktree", "add", "--detach", target,
                 self.sha)
        reopen_rc = r2.returncode
        self.assertEqual(reopen_rc, 0, r2.stderr)
        self.assertTrue(os.path.isdir(target))         # the effect: it opened

    def test_a_canonical_peek_gitdir_still_opens_past_the_dotdot_guard(self):
        """The '..' refusal must not over-match a canonical `<sha12>` room name
        — the exact string that now flows past the new `..` case globs. The
        legitimate door stays open."""
        self.assertEqual(len(self.sha), 40)                # the target is real
        target = os.path.join(work.peek_area(self.root), self.sha[:12])
        r = _sh(self.root, "git", "worktree", "add", "--detach", target,
                self.sha)
        add_rc = r.returncode
        self.assertEqual(add_rc, 0, r.stderr)
        self.assertTrue(os.path.isdir(target))
        head = _sh(target, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(head), 40)                    # a real sha, not ""
        self.assertEqual(head, self.sha)                   # exactly the sha


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


class ClaimWarnsTheRailIsNotArmedTest(WorkBase):
    """THE NET UNDER THE LANDED-CLOSE RUNG. That rung only helps rows landing
    AFTER it; #92 had already landed and closed COMPLETED with its guard never
    installed. Nothing routinely LOOKS at drift — stale_guard_hooks had exactly
    one production caller, `gc`, which prints it above a 45-room listing, so it
    surfaces only if someone runs housekeeping and reads past the rooms.

    CLAIM IS THE MOMENT, not a convenient one: the drifted rules guard worktree
    BIRTH and COMMITS, and claim is where a worktree is born. It cannot live in
    the pre-commit hook, because the stale thing IS that hook."""

    # PATCH _cli, NOT _guard: _cli binds this name at import time
    # (`from ._guard import stale_guard_hooks`), so patching the defining
    # module leaves the bound reference untouched. Measured while writing
    # these: with the _guard target the mock did nothing and the STALE test
    # PASSED ANYWAY, because a fixture repo has no hooks and the real function
    # returned MISSING — a green test measuring none of its own setup.
    def test_claim_says_the_rail_is_not_armed_without_blocking(self):
        with mock.patch("helm.work._cli.stale_guard_hooks",
                        return_value=[("STALE", "pre-commit", "why")]):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        # POSITIVE CONTROL, unconditional: the claim SUCCEEDED. This is a
        # warning on a working claim, never a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARD RAIL NOT ARMED", err)
        self.assertIn("install-guard --apply", err)
        self.assertIn("SHARED by every worktree", err,
                      "one hook dir serves every room — a claimer who reads "
                      "this as 'my lane only' will not go fix it")

    def test_an_armed_rail_says_nothing(self):
        with mock.patch("helm.work._cli.stale_guard_hooks", return_value=[]):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL on the same stream: the claim's own room line is
        # present, so a silent GUARD RAIL means "not warned" and not "stderr
        # was never written to".
        self.assertIn("alpha", _o + err)
        self.assertNotIn("GUARD RAIL", err)

    def test_a_raising_detector_never_costs_the_claim(self):
        """Fail-open and LAST: the claim already succeeded before this runs,
        and a read hiccup must never take back a room that was granted."""
        with mock.patch("helm.work._cli.stale_guard_hooks",
                        side_effect=OSError("hook dir vanished")):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # STRUCTURAL: the room is really on disk. rc 0 from a verb that bailed
        # before creating anything would satisfy the line above.
        self.assertTrue(os.path.isdir(os.path.join(self.root + "-wt", "alpha")))


class ClaimInheritsUnpushedTrunkTest(WorkBase):
    """A NEW ROOM INHERITS WHATEVER LOCAL TRUNK CARRIES, and said nothing.

    `claim` branches from `_base` — the LOCAL integration branch — which is the
    right thing to branch from and says nothing about whether the fleet has
    those commits. When local trunk sits ahead of the remote, every room
    claimed afterward carries the extra commits and the claim output is
    indistinguishable from a clean one.

    MEASURED 2026-08-03: an unreviewed commit sat on the shared checkout's main
    for an hour; @codex-3 claimed a lane and inherited it for free, catching it
    only because an incident was already running. Rooms claimed before were
    clean, rooms claimed after were not, and nothing at the desk said which.
    """

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote],
                       capture_output=True)
        _sh(self.root, "git", "remote", "add", "origin", self.remote)
        r = _sh(self.root, "git", "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        _sh(self.root, "git", "fetch", "-q", "origin")

    def _commit_locally(self, name):
        with open(os.path.join(self.root, name), "w") as f:
            f.write("unpushed\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "unpushed " + name)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_room_claimed_off_an_AHEAD_trunk_says_so(self):
        # CONTROL FIRST, and it is the clause that matters most: with local
        # trunk LEVEL with the remote the claim must be SILENT. A warner that
        # always fires would satisfy the positive case below and be useless.
        rc, out, err = self.work("claim", "level", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("AHEAD", err,
                         "a level trunk must claim silently: %r" % err)
        self.assertIn("lane/level", out)          # positive: the claim worked
        # now local trunk carries a commit the remote does not have
        self._commit_locally("rogue.txt")
        rc, out, err = self.work("claim", "inherits", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/inherits", out)       # positive: still a real claim
        self.assertIn("AHEAD", err)
        self.assertIn("1 commit", err)
        # ...and the room really does carry it, which is the fact being warned
        # about — assert the WORLD, not just the message.
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        contained = _sh(self.root, "git", "merge-base", "--is-ancestor",
                        head, "lane/inherits").returncode
        self.assertEqual(contained, 0,
                         "the warning must describe a real inheritance")

    def test_it_WARNS_and_never_refuses(self):
        """The failure was SILENCE, so the cure is noise — not a refusal.

        Local-ahead is a legitimate transient: a seat that just pushed and has
        not fetched reads exactly this way. Refusing would block a land
        mid-flight, which is worse than the disease it prevents."""
        self._commit_locally("rogue.txt")
        rc, out, err = self.work("claim", "still-works", "--seat", "s1")
        self.assertEqual(rc, 0, "claim must SUCCEED while warning: %s" % err)
        path, branch, lease, _ttl = out.strip().split("\t")
        self.assertEqual(branch, "lane/still-works")
        self.assertTrue(os.path.isdir(path))      # the room really exists
        self.assertTrue(lease)

    def test_a_repo_with_no_remote_is_silent(self):
        """trunk_ref falls back to the LOCAL name when no remote exists, so the
        comparison is base-against-itself and there is nothing to say. Without
        this the guard would cry wolf on every local-only repo — and a check
        that fires when it cannot possibly know teaches people to ignore it."""
        _sh(self.root, "git", "remote", "remove", "origin")
        self._commit_locally("rogue.txt")
        rc, out, err = self.work("claim", "no-remote", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/no-remote", out)      # positive: the claim worked
        self.assertNotIn("AHEAD", err)

    def test_reopening_a_parked_lane_inherits_nothing_so_says_nothing(self):  # noqa: VACUOUS_ASSERTION — mutation-proven non-vacuous: removing the `fresh` gate REDDENS this test; the rev-parse --verify fixture asserts and assertIn('lane/parked', out) are unconditional positives
        """A parked branch re-opens ON ITSELF — `add_worktree` gets base=None —
        so it cannot pick up anything new no matter where local trunk sits. A
        warning here would be false, and a false warning at check-in is how a
        real one stops being read."""
        rc, out, err = self.work("claim", "parked", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        room, _branch, lease, _ttl = out.strip().split("\t")
        # THE LANE MUST CARRY WORK or release RETIRES the branch as merged and
        # the next claim cuts a genuinely fresh one — which SHOULD warn. My
        # first draft released an empty lane and then asserted silence on what
        # was really a brand-new branch; the guard was right and the test was
        # asserting a premise that was never true.
        with open(os.path.join(room, "lane-work.txt"), "w") as f:
            f.write("real work\n")
        _sh(room, "git", "add", "-A")
        r = _sh(room, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.work("release", "parked", "--lease", lease,
                                  "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(_sh(self.root, "git", "rev-parse", "--verify",
                            "lane/parked").returncode == 0,
                        "fixture: an unlanded lane branch must SURVIVE release")
        self._commit_locally("rogue.txt")         # trunk moves while parked
        # AND THE ROOM MUST BE GONE while the BRANCH survives, or `claim`
        # short-circuits on the still-registered worktree and never reaches the
        # base decision at all. My first draft stopped here and proved nothing:
        # removing the `fresh` gate entirely left it GREEN, because the path it
        # claimed to cover was never executed.
        r = _sh(self.root, "git", "worktree", "remove", "--force",
                work.lane_path(self.root, "parked"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", "--verify",
                             "lane/parked").returncode, 0,
                         "fixture: the BRANCH must outlive the room")
        rc, out, err = self.work("claim", "parked", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/parked", out)         # positive: re-claim worked
        self.assertNotIn("AHEAD", err,
                         "a re-opened parked lane inherits nothing: %r" % err)
