#!/usr/bin/env python3
"""helm vcs — the version-control seam. Hermetic: HELM_HOME is a tmp dir, git
global/system config is nulled, every repo is a scratch repo minted in setUp;
the real repo, its worktrees and ~/.helm are never touched.

Two things are proved here. (1) Each GitVcs op does what the private `_git`
helper it consolidated did — including the two shapes that DISAGREE on
'exit 0 with empty output' (`probe` answers '', `capture` answers None), which
is why both survive as named ops. (2) Backend selection resolves to git for
every input — unset, git, jj, garbage, a colocated `.jj/` — and never raises.
"""
import ast
import os
import shutil
import signal
import subprocess
import sys
import collections
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import capsule, gitfacts, handoff, projscope, ship, vcs, work  # noqa: E402

T1 = 1700000000    # the first commit's author/committer date
T2 = 1750000000    # the second's

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_VCS", "MELD_VCS",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")


def _sh(cwd, *args, **kw):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=30, **kw)


class VcsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-vcs-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.root = os.path.join(self.tmp, "proj")
        self.git = vcs.GitVcs()
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(self.root, *cmd).returncode, 0)
        self.shas = []
        for name, ts in (("one", T1), ("two", T2)):
            self.shas.append(self._commit(name, ts))

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, name, ts=None, where=None):
        where = where or self.root
        with open(os.path.join(where, name + ".txt"), "w") as f:
            f.write(name)
        _sh(where, "git", "add", "-A")
        env = dict(os.environ)
        if ts:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "%d +0000" % ts
        r = _sh(where, "git", "commit", "-q", "-m", name, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return _sh(where, "git", "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# the call primitives — one per consolidated helper, shapes preserved exactly
# ---------------------------------------------------------------------------

class TrunkAuthorityTest(VcsBase):
    """`observe_trunk_authority` — the freshness seam a persisted trunk
    binding rests on. Every fixture here is a LOCAL bare repository standing in
    for a remote, so the arms exercise the real fetch/ls-remote path without a
    network."""

    def _remote(self, name="origin"):
        """A bare repo wired as `name`, with this repo's main pushed to it."""
        bare = os.path.join(self.tmp, name + ".git")
        self.assertEqual(_sh(self.tmp, "git", "init", "-q", "--bare",
                             "-b", "main", bare).returncode, 0)
        self.assertEqual(_sh(self.root, "git", "remote", "add", name,
                             bare).returncode, 0)
        self.assertEqual(_sh(self.root, "git", "push", "-q", name,
                             "main").returncode, 0)
        return bare

    def test_a_LOCAL_authority_resolves_without_a_network(self):
        """Resolving the explicit refs/heads ref IS the receipt: nothing is
        over a network, so nothing can be stale. `.` means THIS repository and
        is a declaration, not a remote name to dial."""
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        for remote in (None, "."):
            sha, receipt, failure = vcs.observe_trunk_authority(
                self.root, "refs/heads/main", remote)
            self.assertIsNone(failure, failure)
            self.assertEqual(sha, head)
            self.assertEqual(receipt, vcs.TRUNK_LOCAL)

    def test_a_REMOTE_authority_observes_the_declared_SOURCE_ref(self):
        """The declared ref is the SOURCE on the remote, never a destination
        under refs/remotes. Fetch refspecs map one to the other however the
        operator configured them, so inferring across that mapping is the
        discovery problem this design exists to remove."""
        self._remote()
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        sha, receipt, failure = vcs.observe_trunk_authority(
            self.root, "refs/heads/main", "origin")
        self.assertIsNone(failure, failure)
        self.assertEqual(sha, head)
        self.assertEqual(receipt, vcs.TRUNK_FETCHED)

    def test_a_CUSTOM_REFSPEC_does_not_change_the_answer(self):
        """The observation asks the remote about the declared source ref
        directly, so a fetch refspec pointing destinations somewhere unusual
        cannot move it."""
        self._remote()
        self.assertEqual(_sh(self.root, "git", "config",
                             "remote.origin.fetch",
                             "+refs/heads/*:refs/remotes/somewhere-else/*"
                             ).returncode, 0)
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        sha, receipt, failure = vcs.observe_trunk_authority(
            self.root, "refs/heads/main", "origin")
        self.assertIsNone(failure, failure)
        self.assertEqual(sha, head)
        self.assertEqual(receipt, vcs.TRUNK_FETCHED)

    def test_a_REMOTE_THAT_MOVED_is_observed_at_its_NEW_sha(self):
        """The point of fetching rather than reading refs/remotes: a remote
        that advanced after the last fetch is observed where it IS, not where
        a local snapshot remembers it."""
        bare = self._remote()
        stale = _sh(self.root, "git", "rev-parse",
                    "refs/remotes/origin/main").stdout.strip()
        other = os.path.join(self.tmp, "other")
        self.assertEqual(_sh(self.tmp, "git", "clone", "-q", bare,
                             other).returncode, 0)
        for cmd in (("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            _sh(other, *cmd)
        with open(os.path.join(other, "moved.txt"), "w",
                  encoding="utf-8") as f:
            f.write("the remote moved\n")
        _sh(other, "git", "add", "moved.txt")
        _sh(other, "git", "commit", "-q", "-m", "remote moves")
        self.assertEqual(_sh(other, "git", "push", "-q", "origin",
                             "main").returncode, 0)
        moved = _sh(other, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(moved, stale,
                            "fixture: the remote must actually have moved")
        sha, receipt, failure = vcs.observe_trunk_authority(
            self.root, "refs/heads/main", "origin")
        self.assertIsNone(failure, failure)
        self.assertEqual(sha, moved,
                         "the observation must be the remote's CURRENT sha, "
                         "not the local snapshot %s" % stale[:12])
        self.assertEqual(receipt, vcs.TRUNK_FETCHED)

    def test_an_UNREACHABLE_remote_is_UNKNOWN_and_says_why(self):  # noqa: VACUOUS_ASSERTION — a failure arm; the reachable case is asserted positively in the sibling arms on the same seam
        """Offline, unreachable, unauthenticated — every one is UNKNOWN with
        git's own words, never a guess and never a fallback to a local ref."""
        sha, receipt, failure = vcs.observe_trunk_authority(
            self.root, "refs/heads/main", "no-such-remote", timeout=10)
        self.assertIsNone(sha)
        self.assertIsNone(receipt)
        self.assertIn("no-such-remote", failure)
        self.assertNotIn("[b", failure,
                         "git's words, not a Python repr of them")

    def test_a_SOURCE_that_is_not_a_canonical_ref_is_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — a refusal arm whose positive control is the canonical-ref success in the sibling local arm
        """A declaration this cannot interpret is UNKNOWN rather than an
        invitation to guess at what the operator meant."""
        # REVISION SYNTAX AND REMOTE-TRACKING REFS ARE THE DANGEROUS TWO, and
        # neither is caught by a startswith("refs/"): `refs/heads/main~1`
        # RESOLVES, to main's parent, and `refs/remotes/origin/main` is the
        # stale snapshot this seam exists to reject wearing a local receipt.
        for bad in ("main", "origin/main", "refs/heads/main~1",
                    "refs/remotes/origin/main", "refs/tags/v1",
                    "refs/heads/a..b", "refs/heads/x@{1}"):
            sha, receipt, failure = vcs.observe_trunk_authority(
                self.root, bad, None)
            self.assertIsNone(sha, "accepted %r" % bad)
            self.assertIsNone(receipt)
            self.assertIn("canonical refs/heads", failure, "for %r" % bad)
        sha, receipt, failure = vcs.observe_trunk_authority(self.root, "", None)
        self.assertIsNone(sha)
        self.assertIn("no trunk source is declared", failure,
                      "an ABSENT declaration is its own answer, not a "
                      "malformed one")

    def test_an_ADVERTISEMENT_the_fetch_did_not_deliver_is_TORN(self):  # noqa: VACUOUS_ASSERTION — a failure arm; the delivered case is the positive control in the REMOTE arm above
        """THE RACE, and it is the reason ls-remote alone is not enough. The
        remote can move between the fetch and the advertisement, and an
        advertised sha we do not hold locally means the observation is torn
        rather than current. Simulated by advertising a sha this repository
        has never seen."""
        self._remote()
        ghost = "0" * 40
        real = vcs.GitVcs.text

        def spy(self_, root, *args, **kw):
            if args and args[0] == "ls-remote":
                return 0, "%s\trefs/heads/main\n" % ghost, ""
            return real(self_, root, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", spy):
            sha, receipt, failure = vcs.observe_trunk_authority(
                self.root, "refs/heads/main", "origin")
        self.assertIsNone(sha)
        self.assertIsNone(receipt)
        self.assertIn("torn, not current", failure)


class PrimitiveTest(VcsBase):
    def test_run_is_byte_preserving_and_never_raises(self):
        rc, out, err = self.git.run(self.root, "rev-parse", "HEAD")
        self.assertEqual((rc, err), (0, b""))
        self.assertEqual(out.strip(), self.shas[1].encode())
        # spawn trouble is rc -1 with the reason fsencoded, never an exception
        # (work/_lanes.py's contract: every caller fails toward its SAFE verdict)
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("no git here")):
            rc, out, err = self.git.run(self.root, "status")
        self.assertEqual((rc, out), (-1, b""))
        self.assertIn(b"no git here", err)

    def test_run_env_overlays_never_replaces_the_ambient_environment(self):
        # the ref-guard's sanctioned-creator bit rides this overlay; git still
        # needs HOME/PATH/GIT_CONFIG_* from the ambient env
        seen = {}

        def spy(argv, **kw):
            seen.update(kw.get("env") or {})
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(vcs.subprocess, "run", side_effect=spy):
            self.git.run(self.root, "status", env={"HELM_WORK_CLAIM": "1"})
        self.assertEqual(seen.get("HELM_WORK_CLAIM"), "1")
        self.assertEqual(seen.get("GIT_CONFIG_GLOBAL"), "/dev/null")

    def test_the_uncached_declaration_is_the_seams_and_never_the_childs(self):
        """`gitfacts.UNCACHED` names nothing git reads, so it must not be
        exported: a child inherits it, and so does whatever that child spawns.

        The ordinary overlay key beside it is the positive control — this
        environment IS being built and IS carrying what a caller put in it,
        so the absence below is this removal and not a spy that saw no env."""
        seen = {}

        def spy(argv, **kw):
            seen.update(kw.get("env") or {})
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(vcs.subprocess, "run", side_effect=spy):
            self.git.run(self.root, "status",
                         env={gitfacts.UNCACHED: "1", "HELM_WORK_CLAIM": "1"})
        self.assertEqual(seen.get("HELM_WORK_CLAIM"), "1",
                         "control: the overlay reached the child")
        self.assertNotIn(gitfacts.UNCACHED, seen)

    def test_text_decodes_and_strips_both_streams(self):
        rc, out, err = self.git.text(self.root, "rev-parse", "--abbrev-ref",
                                     "HEAD")
        self.assertEqual((rc, out, err), (0, "main", ""))
        rc, out, err = self.git.text(self.root, "rev-parse", "--verify",
                                     "refs/heads/nope")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertTrue(err)

    def test_probe_keeps_empty_string_as_an_answer(self):
        # handoff's shape: a CLEAN status is '' — folding it to None would make
        # a clean repo indistinguishable from a missing git
        self.assertEqual(self.git.probe(self.root, "status", "--porcelain"), "")
        self.assertEqual(self.git.probe(self.root, "rev-parse", "HEAD"),
                         self.shas[1])
        self.assertIsNone(self.git.probe(self.tmp, "rev-parse",
                                         "--show-toplevel"))
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertIsNone(self.git.probe(self.root, "status"))

    def test_probe_outcome_separates_a_completed_run_from_one_that_never_ran(self):  # noqa: VACUOUS_ASSERTION — the (True, 0, head) and (True, 0, '') equalities are unconditional positive controls on probe_outcome's own return, and assertNotEqual(completed, never_ran) is falsified by any collapse (mutation-proven)
        # THE ONE GUARANTEE. probe() answers None for outcomes that are not
        # the same outcome: the process COMPLETED and exited nonzero, versus
        # nothing ran at all. Only the second carries an unconditional rule —
        # never cache it, at any TTL, because a cached unreadable is
        # indistinguishable from one that was actually measured.
        self.assertEqual(
            self.git.probe_outcome(self.root, "rev-parse", "HEAD"),
            (True, 0, self.shas[1]))
        self.assertEqual(
            self.git.probe_outcome(self.root, "status", "--porcelain"),
            (True, 0, ""))       # '' stays an ANSWER here exactly as in probe

        completed = self.git.probe_outcome(self.tmp, "rev-parse",
                                           "--show-toplevel")
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("boom")):
            never_ran = self.git.probe_outcome(self.root, "status")
        with mock.patch.object(
                vcs.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("git", 1)):
            timed_out = self.git.probe_outcome(self.root, "status")

        self.assertEqual(completed[0], True)             # the process ran
        self.assertNotEqual(completed[1], 0)             # and exited nonzero
        self.assertIsNone(completed[2])
        self.assertEqual(never_ran, (False, None, None))
        self.assertEqual(timed_out, (False, None, None))
        self.assertNotEqual(completed, never_ran)

        # and the collapse this op exists to undo: through probe() all three
        # are one indistinguishable None
        self.assertIsNone(self.git.probe(self.tmp, "rev-parse",
                                         "--show-toplevel"))
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertIsNone(self.git.probe(self.root, "status"))

    def test_probe_outcome_claims_nothing_about_the_question(self):  # noqa: VACUOUS_ASSERTION — the argv-echo assertion is an unconditional positive control on probe_outcome's own return with an EXACT non-empty payload ('--not-a-real-flag'), and assertEqual(missing_cwd, not_a_repo) is an equality between two live returns, not an absence: both are falsified by any change to what probe_outcome reports
        # `ran True` means the process COMPLETED, not that git answered
        # what was asked. Both halves below are things a caller would
        # otherwise assume, and each one is false.
        gone = os.path.join(self.tmp, "no-such-directory")
        self.assertFalse(os.path.exists(gone))

        # (a) a non-repo and a MISSING directory are byte-identical, so
        # this op cannot diagnose an unmounted home
        not_a_repo = self.git.probe_outcome(self.tmp, "rev-parse",
                                            "--show-toplevel")
        missing_cwd = self.git.probe_outcome(gone, "rev-parse",
                                             "--show-toplevel")
        self.assertEqual(not_a_repo[0], True)
        self.assertNotEqual(not_a_repo[1], 0)
        self.assertIsNone(not_a_repo[2])
        self.assertEqual(missing_cwd, not_a_repo)

        # (b) rc 0 does NOT validate argv: git echoes an unknown flag back and
        # exits ZERO, so a malformed question yields a successful-looking
        # outcome whose payload is the caller's own flag
        ran, rc, out = self.git.probe_outcome(self.root, "rev-parse",
                                              "--not-a-real-flag")
        self.assertTrue(ran)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "--not-a-real-flag")

    def test_a_completed_negative_is_about_NOW_and_not_immutable(self):
        # A completed negative is a fact about NOW. A directory becomes a
        # repository the moment somebody runs `git init` in it, so anything
        # caching this owes invalidation on the directory/.git lifecycle.
        room = os.path.join(self.tmp, "becomes-a-repo")
        os.makedirs(room)
        before = self.git.probe_outcome(room, "rev-parse", "--show-toplevel")
        self.assertEqual(before[0], True)
        self.assertNotEqual(before[1], 0)
        self.assertIsNone(before[2])
        subprocess.run(["git", "-C", room, "init", "-q"], check=True,
                       capture_output=True)
        after = self.git.probe_outcome(room, "rev-parse", "--show-toplevel")
        self.assertEqual(after[0:2], (True, 0))
        self.assertIsNotNone(after[2])         # it is a repository NOW
        self.assertNotEqual(before, after)     # same path, changed outcome

    def test_capture_ignores_exit_status_and_folds_empty_to_none(self):
        # capsule's shape: it takes whatever git printed, rc unread
        self.assertEqual(self.git.capture(self.root, "rev-parse", "HEAD"),
                         self.shas[1])
        self.assertIsNone(self.git.capture(self.root, "status", "--porcelain"))
        with mock.patch.object(vcs.GitVcs, "_capture", return_value=(128, "x")):
            self.assertEqual(self.git.capture(self.root, "anything"), "x")
            self.assertIsNone(self.git.probe(self.root, "anything"))
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertIsNone(self.git.capture(self.root, "status"))

    def test_proc_returns_the_process_and_fails_LOUD(self):
        p = self.git.proc(self.root, "rev-parse", "HEAD")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, self.shas[1]))
        p = self.git.proc(self.root, "remote", "get-url", "origin")
        self.assertNotEqual(p.returncode, 0)   # ship reads .returncode itself
        self.assertEqual(p.stdout.strip(), "")
        # ship is an operator verb: a missing git raises rather than degrading
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=FileNotFoundError("git")):
            with self.assertRaises(FileNotFoundError):
                self.git.proc(self.root, "status")


# ---------------------------------------------------------------------------
# the semantic ops — what a second backend must reimplement
# ---------------------------------------------------------------------------

class WorktreeOpTest(VcsBase):
    def test_worktree_lifecycle_add_lock_unlock_remove(self):
        wt = os.path.join(self.tmp, "wt-a")
        rows, error = self.git.worktrees(self.root)
        self.assertIsNone(error)
        self.assertEqual([r["branch"] for r in rows], ["refs/heads/main"])

        rc, _out, err = self.git.add_worktree(self.root, wt, "lane/a",
                                             base="main")
        self.assertEqual(rc, 0, err)
        rows, error = self.git.worktrees(self.root)
        row = next(r for r in rows if r["path"] == wt)
        self.assertEqual((row["branch"], row["locked"]),
                         ("refs/heads/lane/a", False))

        self.assertEqual(self.git.lock_worktree(self.root, wt,
                                               "lease:abcd1234")[0], 0)
        row = next(r for r in self.git.worktrees(self.root)[0]
                   if r["path"] == wt)
        self.assertEqual((row["locked"], row["reason"]),
                         (True, "lease:abcd1234"))
        # git's own refusal is the do-not-disturb: even a raw remove bounces
        self.assertNotEqual(self.git.remove_worktree(self.root, wt)[0], 0)

        self.assertEqual(self.git.unlock_worktree(self.root, wt)[0], 0)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        self.assertEqual([r["path"] for r in self.git.worktrees(self.root)[0]],
                         [self.root])

    def test_remove_targets_one_missing_record_and_respects_lock(self):
        gone = os.path.join(self.tmp, "wt-missing")
        locked = os.path.join(self.tmp, "wt-locked-missing")
        self.assertEqual(self.git.add_worktree(self.root, gone, "lane/missing",
                                              base="main")[0], 0)
        self.assertEqual(self.git.add_worktree(self.root, locked,
                                              "lane/locked-missing",
                                              base="main")[0], 0)
        self.assertEqual(self.git.lock_worktree(self.root, locked,
                                               "on removable media")[0], 0)
        shutil.rmtree(gone)
        shutil.rmtree(locked)

        rc, _out, err = self.git.remove_worktree_record(self.root, gone)
        self.assertEqual(rc, 0, err)
        rows, error = self.git.worktrees(self.root)
        self.assertIsNone(error)
        self.assertNotIn(gone, {r["path"] for r in rows})
        self.assertIn(locked, {r["path"] for r in rows})
        self.assertTrue(self.git.has_branch(self.root, "lane/missing"),
                        "record cleanup must not delete the branch")

        self.assertNotEqual(
            self.git.remove_worktree_record(self.root, locked)[0], 0)
        self.assertIn(locked, {r["path"] for r in self.git.worktrees(self.root)[0]})

    def test_record_removal_resolves_relative_gitdir_pointer(self):  # noqa: VACUOUS_ASSERTION — the discovered admin record, successful exact removal, and real relative pointer prove the final registry absence is observed, not vacuous
        wt = os.path.join(self.tmp, "wt-relative")
        self.assertEqual(self.git.add_worktree(self.root, wt, "lane/relative",
                                              base="main")[0], 0)
        records = os.path.join(self.root, ".git", "worktrees")
        admin = None
        for name in os.listdir(records):
            candidate = os.path.join(records, name)
            try:
                with open(os.path.join(candidate, "gitdir")) as f:
                    pointer = f.read().strip()
            except OSError:
                continue
            if os.path.abspath(pointer) == os.path.join(wt, ".git"):
                admin = candidate
                break
        self.assertIsNotNone(admin)
        pointer = os.path.relpath(os.path.join(wt, ".git"), admin)
        with open(os.path.join(admin, "gitdir"), "w") as f:
            f.write(pointer + "\n")
        shutil.rmtree(wt)
        rc, _out, err = self.git.remove_worktree_record(self.root, wt)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(wt, {r["path"] for r in self.git.worktrees(self.root)[0]})

    def test_record_scan_does_not_block_on_nonregular_gitdir(self):
        """A FIFO `gitdir` must not block the record scan.

        THE BOUND DETECTS A HANG, IT IS NOT A SPEED CLAIM. Opening a FIFO for
        reading waits for a writer, and nobody here ever opens one, so a
        blocking scan never returns. The old 2 s timeout covered the WHOLE
        child: interpreter start, `from helm import vcs`, and the `git
        rev-parse` inside `common_dir`. That fixed cost alone went past 2 s
        once, on a box at load 30, with nothing blocked.

        So the child pays those costs FIRST, including the memoised
        `common_dir`, and only then arms SIGALRM around the scan. The scan
        then starts no process: it is a few syscalls against a
        `scan_alarm`-second alarm. A blocked FIFO open is interruptible, so
        the alarm kills the child by SIGALRM and the arm fails at once
        instead of hanging the suite. The disposition and mask are reset in
        the child because both are inherited across exec.

        POSITIVE CONTROL, run through fab: with O_NONBLOCK dropped from the
        scan's open, this arm fails by SIGALRM."""
        scan_alarm, child_bound = 30, 300
        records = os.path.join(self.root, ".git", "worktrees")
        hostile = os.path.join(records, "hostile")
        os.makedirs(hostile)
        os.mkfifo(os.path.join(hostile, "gitdir"))
        target = os.path.join(self.tmp, "not-registered")
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "from helm import vcs; import signal, sys; "
            "git = vcs.backend(sys.argv[1]); "
            "git.common_dir(sys.argv[1]) or sys.exit('no common dir'); "
            "signal.signal(signal.SIGALRM, signal.SIG_DFL); "
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGALRM]); "
            "signal.alarm(%d); "
            "rc = git.remove_worktree_record(sys.argv[1], sys.argv[2])[0]; "
            "signal.alarm(0); print(rc)" % scan_alarm
        )
        try:
            probe = subprocess.run(
                [sys.executable, "-c", code, self.root, target],
                cwd=project, capture_output=True, text=True,
                timeout=child_bound)
        except subprocess.TimeoutExpired:
            self.fail("the probe child did not exit within %d s, and its "
                      "%d s scan alarm did not end it" % (child_bound,
                                                          scan_alarm))
        self.assertNotEqual(
            probe.returncode, -signal.SIGALRM,
            "record scan blocked while opening a non-regular gitdir")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "1")

    def test_partial_quarantine_cleanup_never_restores_corrupt_record(self):  # noqa: VACUOUS_ASSERTION — injected partial deletion and the retained-quarantine error prove mutation occurred before asserting corrupt live metadata stayed absent
        wt = os.path.join(self.tmp, "wt-partial")
        self.assertEqual(self.git.add_worktree(self.root, wt, "lane/partial",
                                              base="main")[0], 0)

        def partial(path):
            os.remove(os.path.join(path, "gitdir"))
            raise OSError("simulated partial rmtree")

        with mock.patch.object(vcs.shutil, "rmtree", side_effect=partial):
            rc, _out, err = self.git.remove_worktree_record(self.root, wt)
        self.assertEqual(rc, -1)
        self.assertIn("quarantine retained", err)
        records = os.path.join(self.root, ".git", "worktrees")
        live = [name for name in os.listdir(records)
                if os.path.isdir(os.path.join(records, name))]
        self.assertEqual(live, [],
                         "a partially deleted record was restored as live")
        self.assertNotIn(wt, {r["path"] for r in self.git.worktrees(self.root)[0]})

    def test_record_removal_never_deletes_a_revived_checkout(self):
        wt = os.path.join(self.tmp, "wt-revived")
        self.assertEqual(self.git.add_worktree(self.root, wt, "lane/revived",
                                              base="main")[0], 0)
        with open(os.path.join(wt, "unique.txt"), "w") as f:
            f.write("survives metadata cleanup\n")
        rc, _out, err = self.git.remove_worktree_record(self.root, wt)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(wt),
                        "record-only removal deleted the revived checkout")
        with open(os.path.join(wt, "unique.txt")) as f:
            self.assertEqual(f.read(), "survives metadata cleanup\n")
        self.assertTrue(self.git.has_branch(self.root, "lane/revived"))

    def test_add_worktree_without_a_base_reopens_an_existing_branch(self):
        wt = os.path.join(self.tmp, "wt-b")
        self.assertEqual(self.git.add_worktree(self.root, wt, "lane/b",
                                              base="main")[0], 0)
        self._commit("parked", where=wt)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        self.assertTrue(self.git.has_branch(self.root, "lane/b"))
        # the parked branch re-opens IN PLACE — base None, never `-b`
        rc, _out, err = self.git.add_worktree(self.root, wt, "lane/b")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(os.path.join(wt, "parked.txt")))

    def test_add_worktree_refuses_a_second_checkout_of_one_branch(self):
        wt = os.path.join(self.tmp, "wt-c")
        self.assertEqual(self.git.add_worktree(self.root, wt, "lane/c",
                                              base="main")[0], 0)
        rc, _out, err = self.git.add_worktree(
            self.root, os.path.join(self.tmp, "wt-c2"), "lane/c")
        self.assertNotEqual(rc, 0)
        self.assertTrue(err)                    # the reason reaches the caller

    def test_parse_worktree_records_is_nul_and_byte_safe(self):
        weird = os.fsencode(self.root) + b"/wt/agent-deadbeef\\\n\xff"
        raw = (b"worktree " + weird + b"\0HEAD abc\0branch refs/heads/x\0"
               b"locked because\0\0")
        rows, error = vcs.parse_worktree_records(0, raw, b"")
        self.assertIsNone(error)
        self.assertEqual(os.fsencode(rows[0]["path"]), weird)
        self.assertEqual((rows[0]["branch"], rows[0]["locked"],
                          rows[0]["reason"]), ("refs/heads/x", True, "because"))

    def test_parse_worktree_records_failure_is_not_empty_success(self):
        # an empty-looking registry authorizes deletions; failure must SAY so
        self.assertEqual(vcs.parse_worktree_records(1, b"", b"boom"),
                         ([], "boom"))
        self.assertEqual(vcs.parse_worktree_records(1, b"", b""),
                         ([], "git worktree list failed"))
        rows, error = vcs.parse_worktree_records(0, b"worktree /tmp/no-nul", b"")
        self.assertEqual(rows, [])
        self.assertIn("truncated", error)
        rows, error = vcs.parse_worktree_records(0, b"branch refs/heads/x\0", b"")
        self.assertEqual(rows, [])
        self.assertIn("invalid", error)
        self.assertEqual(vcs.parse_worktree_records(0, b"", b""), ([], None))


class RefAndStatusOpTest(VcsBase):
    def test_dirty_is_true_for_uncommitted_untracked_and_unreadable(self):
        self.assertFalse(self.git.dirty(self.root))
        with open(os.path.join(self.root, "untracked"), "w") as f:
            f.write("x")
        self.assertTrue(self.git.dirty(self.root))
        os.remove(os.path.join(self.root, "untracked"))
        with open(os.path.join(self.root, "one.txt"), "w") as f:
            f.write("modified")
        self.assertTrue(self.git.dirty(self.root))
        # unreadable reads as DIRTY — callers fail toward rescue, not discard
        self.assertTrue(self.git.dirty(os.path.join(self.tmp, "not-a-repo")))

    def test_head_sha_resolves_head_a_ref_and_an_era(self):
        self.assertEqual(self.git.head_sha(self.root), self.shas[1])
        self.assertEqual(self.git.head_sha(self.root, ref="HEAD~1"),
                         self.shas[0])
        # capsule's era read: the commit the repo was on at a past instant
        between = datetime.fromtimestamp(
            (T1 + T2) // 2).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(self.git.head_sha(self.root, ref="HEAD",
                                          before=between), self.shas[0])
        before_all = datetime.fromtimestamp(
            T1 - 3600).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIsNone(self.git.head_sha(self.root, before=before_all))
        self.assertIsNone(self.git.head_sha(self.tmp))

    def test_base_branch_prefers_main_then_the_checkout_head(self):
        self.assertEqual(self.git.base_branch(self.root), "main")
        other = os.path.join(self.tmp, "trunky")
        os.makedirs(other)
        _sh(other, "git", "init", "-q", "-b", "trunk")
        _sh(other, "git", "config", "user.email", "t@t")
        _sh(other, "git", "config", "user.name", "t")
        self._commit("seed", where=other)
        self.assertEqual(self.git.base_branch(other), "trunk")
        # no repo at all still answers a usable name, never an exception
        self.assertEqual(self.git.base_branch(os.path.join(self.tmp, "void")),
                         "main")

    def _master_repo_with_a_lane_room(self):
        """A repo trunked on `master` (no `main`) with one linked lane
        worktree checked out on its own branch -- a client project's shape."""
        repo = os.path.join(self.tmp, "mastered")
        os.makedirs(repo)
        for cmd in (("git", "init", "-q", "-b", "master"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(repo, *cmd).returncode, 0)
        self._commit("seed", where=repo)
        room = os.path.join(self.tmp, "mastered-wt", "a-lane")
        made = _sh(repo, "git", "worktree", "add", "-q", "-b", "lane/a-lane",
                   room)
        self.assertEqual(made.returncode, 0, made.stderr)
        self._commit("lane-work", where=room)
        # THE PRECONDITION IS THE BUG'S INPUT: the room's own HEAD is the lane
        # branch, which is what a bare `symbolic-ref HEAD` answers there.
        self.assertEqual(_sh(room, "git", "symbolic-ref", "--short",
                             "HEAD").stdout.strip(), "lane/a-lane")
        return repo, room

    def test_a_lane_room_names_the_shared_checkouts_trunk_not_its_own_branch(self):
        """From inside a linked worktree on a repo with no `main`, the trunk is
        the SHARED checkout's HEAD. Reading the room's own HEAD made the lane
        its own trunk: every tip already landed, its own commits foreign."""
        repo, room = self._master_repo_with_a_lane_room()
        self.assertEqual(self.git.base_branch(room), "master")
        self.assertEqual(self.git.trunk_ref(room), "master")
        # THE MIRROR THE COMMIT HOOK BAKES must give the same answer, or the
        # two drift -- lane_discipline says it resolves "exactly as" the seam.
        from helm import lane_discipline
        self.assertEqual(lane_discipline.base_branch(room), "master")
        # CONTROL: the shared checkout itself answers the same name, so the
        # arm is about WHERE the question is asked from.
        self.assertEqual(self.git.base_branch(repo), "master")
        self.assertEqual(lane_discipline.base_branch(repo), "master")
        # AND the answer is not pinned to the literal: a shared checkout that
        # is itself on another branch is still what the room reports.
        _sh(repo, "git", "checkout", "-q", "-b", "trunk-two")
        self.assertEqual(self.git.base_branch(room), "trunk-two")
        self.assertEqual(lane_discipline.base_branch(room), "trunk-two")

    def test_a_path_reused_for_another_repository_names_that_repositorys_trunk(self):
        """A room path deleted and recreated as a DIFFERENT repository inside
        one process must answer the new repository's trunk. The common-dir memo
        is keyed by path and keeps the old binding, so a trunk read through it
        would name repository A's branch while every ancestry read runs
        against repository B's objects. The memo is WARMED on purpose first."""
        repo, room = self._master_repo_with_a_lane_room()
        self.assertEqual(self.git.base_branch(room), "master")
        self.assertIsNotNone(self.git.common_dir(room),
                             "fixture: the memo was not warmed for the path")
        gone = _sh(repo, "git", "worktree", "remove", "--force", room)
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertFalse(os.path.exists(room))
        os.makedirs(room)
        for cmd in (("git", "init", "-q", "-b", "develop"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(room, *cmd).returncode, 0)
        self._commit("b-seed", where=room)
        _sh(room, "git", "branch", "master")
        from helm import lane_discipline
        self.assertEqual(self.git.base_branch(room), "develop")
        self.assertEqual(self.git.trunk_ref(room), "develop")
        self.assertEqual(lane_discipline.base_branch(room), "develop")
        # CONTROL: the original repository, whose binding never changed, still
        # answers its own trunk from its own shared checkout.
        self.assertEqual(self.git.base_branch(repo), "master")

    def test_a_tag_shadowing_the_trunk_keeps_the_branch_qualified(self):
        """A tag named like the trunk branch must not become the trunk. git's own
        shortening answers `heads/master` when a tag `master` also exists, and
        every ancestry read built on the name then reads the BRANCH; a bare
        `master` resolves to the tag first. The tag sits on an unlanded lane
        commit, so a tag-resolving trunk would call that commit landed.
        CONTROL: without the tag the same room answers plain `master`."""
        repo, room = self._master_repo_with_a_lane_room()
        lane_tip = _sh(room, "git", "rev-parse", "HEAD").stdout.strip()
        from helm import lane_discipline
        self.assertEqual(self.git.base_branch(room), "master")
        _sh(repo, "git", "tag", "master", lane_tip)
        self.assertEqual(self.git.base_branch(room), "heads/master")
        self.assertEqual(lane_discipline.base_branch(room), "heads/master")
        # THE HOOK'S TWO READS AGREE in the shared checkout, so the venue rung
        # sees its base branch rather than "some other branch".
        self.assertEqual(lane_discipline.base_branch(repo),
                         lane_discipline.head_branch(repo))
        trunk = self.git.trunk_ref(room)
        self.assertEqual(_sh(room, "git", "rev-parse", trunk).stdout.strip(),
                         _sh(room, "git", "rev-parse", "refs/heads/master").stdout.strip())
        self.assertEqual(self.git.ancestry(room, lane_tip, trunk), vcs.NOT_ANCESTOR)

    def test_a_bare_repository_names_its_symbolic_head(self):
        """A bare repository has no working tree and a real symbolic HEAD, and a
        lane room linked to it asks the same question. Both answer the branch
        HEAD names, never `main`. CONTROL: a non-bare clone of the same history
        answers the same branch."""
        src = os.path.join(self.tmp, "bare-src")
        os.makedirs(src)
        for cmd in (("git", "init", "-q", "-b", "master"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(src, *cmd).returncode, 0)
        self._commit("seed", where=src)
        bare = os.path.join(self.tmp, "mirror.git")
        made = _sh(self.tmp, "git", "clone", "-q", "--bare", src, bare)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(_sh(bare, "git", "rev-parse", "--is-bare-repository")
                         .stdout.strip(), "true")
        self.assertEqual(self.git.base_branch(bare), "master")
        room = os.path.join(self.tmp, "bare-wt", "lane-x")
        added = _sh(bare, "git", "worktree", "add", "-q", "-b", "lane/x", room)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.git.base_branch(room), "master")
        from helm import lane_discipline
        self.assertEqual(lane_discipline.base_branch(room), "master")
        self.assertEqual(self.git.base_branch(src), "master")

    def test_trunk_ref_prefers_the_remote_and_falls_back_when_there_is_none(self):
        """No remote at all -> the local name, because a LOCAL-audience repo
        with no remote genuinely has no other trunk and refusing there would
        break the one case where local main IS the trunk."""
        self.assertEqual(self.git.trunk_ref(self.root), "main")
        _sh(self.root, "git", "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(self.git.trunk_ref(self.root), "origin/main")

    def test_a_merge_that_was_never_pushed_does_not_read_as_landed(self):
        """THE DESTRUCTIVE DIRECTION, and the reason this seam exists.

        `_merged` gates `git branch -d` and room removal. Measured 2026-07-30:
        a seat merged an approved lane into LOCAL main, verified with
        `--is-ancestor <tip> main` -> TRUE, released its landlock and let the
        branch auto-delete as PROVEN. origin/main was 4 commits behind and
        still red. Had gc --apply run in that window it would have deleted the
        branch for a fix the fleet did not have.

        So: local main AHEAD of origin/main, a lane contained in local main and
        NOT in origin/main, must answer NOT_ANCESTOR against the trunk ref."""
        _sh(self.root, "git", "update-ref", "refs/remotes/origin/main", "HEAD")
        _sh(self.root, "git", "checkout", "-q", "-b", "lane/unpushed")
        self._commit("work the fleet has never seen")
        lane_tip = self.git.head_sha(self.root)
        _sh(self.root, "git", "checkout", "-q", "main")
        _sh(self.root, "git", "merge", "--no-ff", "-q", "-m", "land",
            "lane/unpushed")

        # the WRONG question — the one that shipped — still says yes
        self.assertEqual(self.git.ancestry(self.root, lane_tip, "main"),
                         vcs.ANCESTOR)
        # the RIGHT question refuses, so no delete is authorized
        self.assertEqual(
            self.git.ancestry(self.root, lane_tip, self.git.trunk_ref(self.root)),
            vcs.NOT_ANCESTOR)

        # and once it is actually pushed, the trunk ref agrees it landed
        _sh(self.root, "git", "update-ref", "refs/remotes/origin/main", "main")
        self.assertEqual(
            self.git.ancestry(self.root, lane_tip, self.git.trunk_ref(self.root)),
            vcs.ANCESTOR)

    def test_has_branch_and_is_ancestor(self):
        self.assertTrue(self.git.has_branch(self.root, "main"))
        self.assertFalse(self.git.has_branch(self.root, "lane/nope"))
        self.assertTrue(self.git.is_ancestor(self.root, "HEAD~1", "HEAD"))
        self.assertFalse(self.git.is_ancestor(self.root, "HEAD", "HEAD~1"))
        wt = os.path.join(self.tmp, "wt-anc")
        self.git.add_worktree(self.root, wt, "lane/anc", base="main")
        self._commit("ahead", where=wt)
        self.assertFalse(self.git.is_ancestor(self.root, "lane/anc", "main"))
        self.assertTrue(self.git.is_ancestor(self.root, "main", "lane/anc"))

    def test_ancestry_is_tri_state_and_unknown_is_never_a_verdict(self):
        """The phantom-unlanded-lanes fold, pinned at the seam: rc 0 / 1 / 128
        are THREE answers. Folding 128 into the negative is how a land-audit
        read tips whose objects were GONE as "not merged" — a check that
        cannot see a case must return UNKNOWN, never a verdict."""
        # both real directions first: proof and the CLEAN negative
        self.assertEqual(self.git.ancestry(self.root, "HEAD~1", "HEAD"),
                         vcs.ANCESTOR)
        self.assertEqual(self.git.ancestry(self.root, "HEAD", "HEAD~1"),
                         vcs.NOT_ANCESTOR)
        # an UNREADABLE object (the 40-zero sha names nothing) is UNKNOWN —
        # never the negative, whichever side of the question it sits on
        zero = "0" * 40
        self.assertEqual(self.git.ancestry(self.root, zero, "HEAD"),
                         vcs.UNKNOWN)
        self.assertEqual(self.git.ancestry(self.root, "HEAD", zero),
                         vcs.UNKNOWN)
        # no repo at all: cannot see != not merged
        self.assertEqual(self.git.ancestry(os.path.join(self.tmp, "void"),
                                           "HEAD", "HEAD"), vcs.UNKNOWN)
        # spawn trouble (text's rc -1 contract) is equally UNKNOWN
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("no git here")):
            self.assertEqual(self.git.ancestry(self.root, "HEAD~1", "HEAD"),
                             vcs.UNKNOWN)
        # the boolean projection: True is PROOF; UNKNOWN projects to 'no
        # proof', exactly like the clean negative — which is why a caller
        # with a load-bearing negative branch must use ancestry itself
        self.assertTrue(self.git.is_ancestor(self.root, "HEAD~1", "HEAD"))
        self.assertFalse(self.git.is_ancestor(self.root, zero, "HEAD"))

    def test_delete_branch_refuses_unmerged_and_deletes_merged(self):  # noqa: VACUOUS_ASSERTION — the refusal is the contract; rc!=0 + a truthy err + has_branch True are the positive controls
        wt = os.path.join(self.tmp, "wt-del")
        self.git.add_worktree(self.root, wt, "lane/del", base="main")
        self._commit("work", where=wt)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        rc, _out, err = self.git.delete_branch(self.root, "lane/del")
        self.assertNotEqual(rc, 0)              # DEFAULT is -d, never -D
        self.assertTrue(err)
        self.assertTrue(self.git.has_branch(self.root, "lane/del"))
        _sh(self.root, "git", "merge", "-q", "--no-edit", "lane/del")
        self.assertEqual(self.git.delete_branch(self.root, "lane/del")[0], 0)
        self.assertFalse(self.git.has_branch(self.root, "lane/del"))

    def test_force_is_opt_in_and_the_default_stays_the_safe_delete(self):  # noqa: VACUOUS_ASSERTION — has_branch True after the default call is the positive control for the forced absence
        """`force` exists for ONE proof and is never the default.

        `-d` refuses on REACHABILITY, which is the same wrong question
        `landed_state` corrects — a rebased-and-landed branch fails `-d`
        forever. So `force` is available, and this pins that a caller must ASK:
        the unmerged branch survives the default call and only a deliberate
        force removes it."""
        wt = os.path.join(self.tmp, "wt-force")
        self.git.add_worktree(self.root, wt, "lane/force", base="main")
        self._commit("unlanded", where=wt)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        self.assertNotEqual(
            self.git.delete_branch(self.root, "lane/force")[0], 0)
        self.assertTrue(self.git.has_branch(self.root, "lane/force"))
        self.assertEqual(
            self.git.delete_branch(self.root, "lane/force", force=True)[0], 0)
        self.assertFalse(self.git.has_branch(self.root, "lane/force"))


# ---------------------------------------------------------------------------
# landed_state — the reap predicate, taught CONTENT identity
# ---------------------------------------------------------------------------

class LandedStateTest(VcsBase):
    """THE ROOT CAUSE, pinned: the cleanup check asked SHA IDENTITY and got a
    truthful "no" for every rebased land, so every well-behaved agent kept its
    branch forever.

    Owner's live measurement, 2026-08-03: lane/orca-seam-s1-doorway tip
    5905f65 -> `merge-base --is-ancestor` against origin/main is FALSE, while
    the landed ba5e740 IS on trunk and `git patch-id --stable` prints
    1f7bacc86ebe5849 for BOTH. This class rebuilds that shape in a throwaway
    repo and requires BOTH arms — the rebased land classified retirable, and
    the genuinely unlanded lane classified keep. One arm proves nothing.
    """

    def _lane(self, name, files):
        """A lane branch off main carrying one commit per file. Returns its
        branch name; the worktree is removed so the branch stands alone (the
        real shape: branches outlive their rooms)."""
        wt = os.path.join(self.tmp, "wt-" + name)
        branch = "lane/" + name
        self.git.add_worktree(self.root, wt, branch, base="main")
        for f in files:
            self._commit(f, where=wt)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        return branch

    def _patch_id(self, committish):
        show = _sh(self.root, "git", "show", committish)
        pid = subprocess.run(["git", "patch-id", "--stable"],
                             cwd=self.root, input=show.stdout,
                             capture_output=True, text=True, timeout=30)
        return pid.stdout.split()[0]

    def test_a_rebased_land_is_RETIRABLE_and_an_unlanded_lane_is_KEPT(self):  # noqa: VACUOUS_ASSERTION — equal patch-ids and the exact PATCH_EQUIVALENT/NOT_ANCESTOR verdicts are the positive controls
        """BOTH ARMS. This is the whole predicate.

        `git cherry-pick` reproduces exactly what our integrator does: it
        replays the lane's change onto current trunk, so the landed commit is
        patch-identical and object-DIFFERENT. Ancestry says no, truthfully.
        Only content identity can see the branch is redundant."""
        landed = self._lane("rebased", ["feature"])
        lane_tip = _sh(self.root, "git", "rev-parse", landed).stdout.strip()
        kept = self._lane("unlanded", ["never-landed"])
        # main moves on FIRST, so the cherry-pick cannot be a fast-forward and
        # the replayed commit is forced to take a new sha
        self._commit("trunk-moved")
        self.assertEqual(
            _sh(self.root, "git", "cherry-pick", lane_tip).returncode, 0)
        trunk_tip = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()

        # the owner's exact measurement: same content, different object
        self.assertNotEqual(lane_tip, trunk_tip)
        self.assertEqual(self._patch_id(lane_tip), self._patch_id(trunk_tip))

        # ARM 1 — ancestry is TRUTHFULLY negative, and content identity sees it
        self.assertEqual(self.git.ancestry(self.root, landed, "main"),
                         vcs.NOT_ANCESTOR)
        self.assertEqual(self.git.landed_state(self.root, landed, "main"),
                         vcs.PATCH_EQUIVALENT)
        # ARM 2 — a lane whose content never landed is still a clean NEGATIVE.
        # Without this arm the predicate could be `return PATCH_EQUIVALENT`.
        self.assertEqual(self.git.landed_state(self.root, kept, "main"),
                         vcs.NOT_ANCESTOR)

    def test_patch_sequence_owns_its_diff_and_commit_grammar(self):  # noqa: VACUOUS_ASSERTION — the unconditional one-patch control proves the accessor is live before hostile repository config is required to preserve that exact identity
        base, tip = self.shas
        control = self.git.patch_sequence(self.root, base, tip)
        self.assertEqual(len(control), 1,
                         "positive control: the fixture must emit one patch")
        self.assertEqual(_sh(self.root, "git", "config", "diff.noprefix",
                             "true").returncode, 0)
        for key, value in (
                ("format.pretty", "format:commit " + base),
                ("diff.renames", "true"),
                ("diff.algorithm", "histogram"),
                ("diff.context", "20"),
                ("diff.interHunkContext", "20"),
                ("diff.srcPrefix", "hostile-old/"),
                ("diff.dstPrefix", "hostile-new/")):
            self.assertEqual(_sh(self.root, "git", "config", key,
                                 value).returncode, 0)
        self.assertEqual(self.git.patch_sequence(self.root, base, tip), control,
                         "repository config cannot redefine proof identity")

    def test_patch_train_containment_requires_one_complete_ordered_run(self):  # noqa: VACUOUS_ASSERTION — the unconditional CONTAINED tuple proves the production observable is live before the table exercises its refusing states
        with mock.patch.object(
                self.git, "patch_sequence",
                side_effect=(("a", "b"), ("x", "a", "b", "y"))):
            control = self.git.patch_sequence_containment(
                self.root, self.shas[0], self.shas[1])
        self.assertEqual(control, (vcs.PATCH_SEQUENCE_CONTAINED, 1, 2, 4),
                         "positive control: one complete run must authorize")
        cases = (
            (("a", "b"), ("b", "a"), vcs.PATCH_SEQUENCE_ABSENT, None),
            (("a",), ("a", "x", "a"),
             vcs.PATCH_SEQUENCE_AMBIGUOUS, None),
            ((), ("x",), vcs.PATCH_SEQUENCE_EMPTY, None),
            (None, ("x",), vcs.PATCH_SEQUENCE_UNKNOWN, None),
        )
        for needle, train, wanted, start in cases:
            with self.subTest(wanted=wanted), mock.patch.object(
                    self.git, "patch_sequence", side_effect=(needle, train)):
                state = self.git.patch_sequence_containment(
                    self.root, self.shas[0], self.shas[1])
            self.assertEqual(state[0], wanted)
            self.assertEqual(state[1], start)

    def test_a_WHITESPACE_ONLY_difference_is_NOT_patch_identity(self):  # noqa: VACUOUS_ASSERTION — POSITIVE CONTROL 3 measures the same observable (landed_state) on a byte-identical lane and requires PATCH_EQUIVALENT, so a predicate stuck on UNKNOWN fails here; mutation-verified 2026-08-04 ('patch-equivalent' != 'unknown')
        """cherry's '-' is necessary and NOT sufficient to authorise a delete.

        `git cherry` is patch-id's DEFAULT algorithm, which normalises
        whitespace — right for its real job (spotting a commit across a
        rebase), wrong as a deletion bar. Trunk carrying `value` and a lane
        carrying `value  ` are different blobs, and cherry answers '-':
        ALREADY UPSTREAM. That is the one cherry blind spot that fails toward
        LANDED; the two this module already documents (merge commits, merged-in
        trunk) both fail toward UNKNOWN, which keeps.

        The positive control IS the hazard: this asserts cherry really does say
        '-' and the blobs really do differ, so a green here can never mean the
        scenario failed to build. The contract is that landed_state refuses it
        anyway — UNKNOWN, which keeps the branch.

        Its recall twin is test_a_rebased_land_is_RETIRABLE above: the same
        byte-exact check must NOT break a genuinely rebased land, or this
        predicate would delete the feature it is guarding."""
        wt = os.path.join(self.tmp, "wt-wsp")
        branch = "lane/wsp"
        self.git.add_worktree(self.root, wt, branch, base="main")
        with open(os.path.join(wt, "payload.txt"), "w") as fh:
            fh.write("value\n")
        _sh(wt, "git", "add", "-A")
        self.assertEqual(
            _sh(wt, "git", "commit", "-q", "-m", "lane adds payload").returncode, 0)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        # trunk gains the SAME logical change, spelled with trailing spaces
        with open(os.path.join(self.root, "payload.txt"), "w") as fh:
            fh.write("value  \n")
        _sh(self.root, "git", "add", "-A")
        self.assertEqual(
            _sh(self.root, "git", "commit", "-q", "-m",
                "trunk adds payload, spaced").returncode, 0)

        # POSITIVE CONTROL 1 — the hazard exists: cherry calls it upstream.
        cherry = _sh(self.root, "git", "cherry", "main", branch).stdout.strip()
        self.assertTrue(cherry.startswith("-"),
                        "the whitespace hazard did not reproduce: %r" % cherry)
        # POSITIVE CONTROL 2 — and the content genuinely differs.
        self.assertNotEqual(
            _sh(self.root, "git", "rev-parse",
                "main:payload.txt").stdout.strip(),
            _sh(self.root, "git", "rev-parse",
                branch + ":payload.txt").stdout.strip())
        # THE CONTRACT — a byte-inexact match is no evidence, so it KEEPS.
        self.assertEqual(self.git.landed_state(self.root, branch, "main"),
                         vcs.UNKNOWN)
        # POSITIVE CONTROL 3, on the OBSERVABLE ITSELF and not on the fixture:
        # the two above prove the SCENARIO built; neither proves landed_state
        # can still return anything but UNKNOWN here. A predicate broken to
        # `return UNKNOWN` would pass this test without them. So a lane that
        # IS byte-identically on trunk is measured in the same repo, in the
        # same call, and must read PATCH_EQUIVALENT.
        exact = self._lane("wspexact", ["exactfeature"])
        exact_tip = _sh(self.root, "git", "rev-parse", exact).stdout.strip()
        self._commit("trunk-moves-again")
        self.assertEqual(
            _sh(self.root, "git", "cherry-pick", exact_tip).returncode, 0)
        self.assertEqual(self.git.landed_state(self.root, exact, "main"),
                         vcs.PATCH_EQUIVALENT)

    def test_ancestry_stays_the_decisive_fast_path(self):
        """An ancestor is answered by ancestry alone — no cherry spawn. The
        measured cost model (1.2ms ancestry vs up to 44ms cherry) only holds if
        the fast path really is short-circuited."""
        merged = self._lane("ff", ["ff-work"])
        _sh(self.root, "git", "merge", "-q", "--ff-only", merged)
        spawns = []
        real = vcs.GitVcs.text

        def spy(self_, cwd, *args, **kw):
            spawns.append(args)
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", spy):
            self.assertEqual(self.git.landed_state(self.root, merged, "main"),
                             vcs.ANCESTOR)
        self.assertTrue(spawns)                          # it really ran
        self.assertFalse([a for a in spawns if a[0] == "cherry"])

    def test_a_PARTLY_landed_stack_keeps_its_branch(self):  # noqa: VACUOUS_ASSERTION — cherry output asserted to contain BOTH signs is the positive control
        """Landing SOME of a stack is the state that most needs its branch.
        A single '+' is decisive against retirement — measured on the live box
        2026-08-03: 8 of 107 lanes were exactly this shape."""
        mixed = self._lane("mixed", ["first", "second"])
        first = _sh(self.root, "git", "rev-parse", mixed + "~1").stdout.strip()
        self._commit("trunk-moved")
        self.assertEqual(
            _sh(self.root, "git", "cherry-pick", first).returncode, 0)
        cherry = _sh(self.root, "git", "cherry", "main", mixed).stdout
        self.assertIn("-", cherry)                # the landed half IS seen
        self.assertIn("+", cherry)                # and the other half is not
        self.assertEqual(self.git.landed_state(self.root, mixed, "main"),
                         vcs.NOT_ANCESTOR)

    def test_a_merge_commit_cherry_CANNOT_SEE_reads_UNKNOWN_never_landed(self):  # noqa: VACUOUS_ASSERTION — exact counts (ahead==3, cherry==2) are the positive control for the undercount
        """THE SAFETY BELT, and it is not theoretical.

        `git cherry` is `rev-list --no-merges` underneath, so a branch whose
        only unlanded commit is a MERGE produces EMPTY output — which a naive
        "no '+' lines means landed" reader calls retirable. Measured on the
        live box 2026-08-03, this exact shape existed FOUR times
        (lane/aspublic-gate, lane/lr-merge-prep, lane/never-track-needle-breadth,
        lane/seat-commits-carry-the-seat). Shipping without the count
        cross-check would have deleted branches holding real work.
        """
        # A lane that merged a SIBLING lane in — the chain shape our protocol
        # produces routinely — and then had both its real commits land rebased.
        wa = os.path.join(self.tmp, "wt-sib")
        self.git.add_worktree(self.root, wa, "lane/sib", base="main")
        a_tip = self._commit("aaa", where=wa)
        wb = os.path.join(self.tmp, "wt-merger")
        self.git.add_worktree(self.root, wb, "lane/merger", base="main")
        b_tip = self._commit("bbb", where=wb)
        self.assertEqual(
            _sh(wb, "git", "merge", "-q", "--no-ff", "--no-edit",
                "lane/sib").returncode, 0)
        self._commit("trunk-moved")
        for sha in (a_tip, b_tip):
            self.assertEqual(
                _sh(self.root, "git", "cherry-pick", sha).returncode, 0)
        for wt in (wa, wb):
            self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)

        # THE TRAP, spelled out: every line cherry prints says LANDED...
        ahead = _sh(self.root, "git", "rev-list", "--count",
                    "main..lane/merger").stdout.strip()
        cherry = [ln for ln in _sh(self.root, "git", "cherry", "main",
                                   "lane/merger").stdout.split("\n") if ln.strip()]
        self.assertNotIn("+", "".join(cherry))    # ...nothing looks unlanded...
        self.assertEqual(ahead, "3")              # ...yet 3 commits are ahead...
        self.assertEqual(len(cherry), 2)          # ...and cherry saw only 2.
        # The invisible one is the MERGE, and a merge is not nothing: its
        # conflict resolution can be authored bytes that exist in no other
        # commit. `git cherry` structurally cannot speak for it.

        self.assertEqual(self.git.ancestry(self.root, "lane/merger", "main"),
                         vcs.NOT_ANCESTOR)
        self.assertEqual(
            self.git.landed_state(self.root, "lane/merger", "main"),
            vcs.UNKNOWN)                          # NOT patch-equivalent

    def test_a_lane_that_merged_TRUNK_in_can_never_prove_patch_identity(self):  # noqa: VACUOUS_ASSERTION — the matching patch-ids and the asserted '+ <sha>' line are the positive controls
        """A SECOND blindness, found by building the fixture (2026-08-03).

        `git cherry <upstream> <head>` compares against `<merge-base>..<upstream>`.
        Once a lane merges trunk IN, the merge-base becomes trunk's tip, that
        upstream set is EMPTY, and every lane commit reads '+' — even one whose
        `git patch-id --stable` matches a commit on trunk exactly. The
        primitive goes blind in precisely the state that looks most landed.

        This costs RECALL, never safety — such a lane is never retirable, by
        the count cross-check or by the '+' or by both. Pinned so nobody later
        reads such a lane's '+' as proof the work is absent: it is proof of
        nothing at all, and a future "just trust the '+'" simplification would
        be reasoning from a blind instrument."""
        wt = os.path.join(self.tmp, "wt-trunkmerge")
        self.git.add_worktree(self.root, wt, "lane/trunkmerge", base="main")
        tip = self._commit("side", where=wt)
        self._commit("trunk-moved")
        self.assertEqual(_sh(self.root, "git", "cherry-pick", tip).returncode, 0)
        landed = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        # the content IS on trunk — same patch-id, different object
        self.assertEqual(self._patch_id(tip), self._patch_id(landed))
        self.assertEqual(
            _sh(wt, "git", "merge", "-q", "--no-ff", "--no-edit",
                "main").returncode, 0)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        # ...and cherry now marks that very commit '+', its patch-id match
        # notwithstanding
        self.assertIn("+ " + tip,
                      _sh(self.root, "git", "cherry", "main",
                          "lane/trunkmerge").stdout)
        state = self.git.landed_state(self.root, "lane/trunkmerge", "main")
        self.assertNotEqual(state, vcs.PATCH_EQUIVALENT)
        self.assertNotEqual(state, vcs.ANCESTOR)   # never retirable, either way

    def test_every_unreadable_ambiguous_and_capped_case_is_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the readable control asserts PATCH_EQUIVALENT first; UNKNOWN is the contract under test
        """This gates a DELETION, so UNKNOWN must never be spendable. Each leg
        is a separate way the read can fail and every one keeps the branch."""
        lane = self._lane("edge", ["a"])
        self._commit("trunk-moved")
        self.assertEqual(
            _sh(self.root, "git", "cherry-pick",
                _sh(self.root, "git", "rev-parse", lane).stdout.strip()
                ).returncode, 0)
        # the control: with everything readable this IS retirable
        self.assertEqual(self.git.landed_state(self.root, lane, "main"),
                         vcs.PATCH_EQUIVALENT)

        # (a) a range over the cap — a latency bound that fails toward KEEP
        self.assertEqual(self.git.landed_state(self.root, lane, "main", cap=0),
                         vcs.UNKNOWN)
        # (b) an object that names nothing
        self.assertEqual(self.git.landed_state(self.root, "0" * 40, "main"),
                         vcs.UNKNOWN)
        # (c) spawn trouble anywhere in the ladder
        with mock.patch.object(vcs.subprocess, "run",
                               side_effect=OSError("no git here")):
            self.assertEqual(self.git.landed_state(self.root, lane, "main"),
                             vcs.UNKNOWN)
        # (d) an UNRECOGNISED cherry format is never a verdict. Real git can
        # change; a parser that shrugs and returns the positive would delete
        # on output it did not understand.
        real = vcs.GitVcs.text

        def garble(self_, cwd, *args, **kw):
            if args and args[0] == "cherry":
                return 0, "? deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", ""
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", garble):
            self.assertEqual(self.git.landed_state(self.root, lane, "main"),
                             vcs.UNKNOWN)
        # (e) a count/cherry disagreement in EITHER direction
        def extra(self_, cwd, *args, **kw):
            if args and args[0] == "cherry":
                rc, out, err = real(self_, cwd, *args, **kw)
                return rc, out + "\n- " + "b" * 40, err
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", extra):
            self.assertEqual(self.git.landed_state(self.root, lane, "main"),
                             vcs.UNKNOWN)
        # and the branch survived every one of them
        self.assertTrue(self.git.has_branch(self.root, lane))

    def test_an_EMPTY_commit_supplies_NO_patch_identity_evidence(self):  # noqa: VACUOUS_ASSERTION — cherry asserted to start with '-' is the positive control that the trap fires
        """AN EMPTY DIFF HAS AN EMPTY PATCH-ID, so every empty commit is
        "patch-identical" to every other one.

        Measured 2026-08-03: a lane carrying one `commit --allow-empty` marker
        reads '-' from `git cherry` against a trunk whose only empty commit is
        TOTALLY UNRELATED. Nothing about the two corresponds; they merely both
        changed no files. No bytes are at risk in that case, but the VERDICT is
        false and a false landed verdict is the input to a deletion — so the
        range is refused. This is the one real instance of "patch identity is a
        weaker grade of fact than ancestry" that survived measurement."""
        wt = os.path.join(self.tmp, "wt-empty")
        self.git.add_worktree(self.root, wt, "lane/empty", base="main")
        self.assertEqual(
            _sh(wt, "git", "commit", "-q", "--allow-empty",
                "-m", "lane marker").returncode, 0)
        self.assertEqual(self.git.remove_worktree(self.root, wt)[0], 0)
        self.assertEqual(
            _sh(self.root, "git", "commit", "-q", "--allow-empty",
                "-m", "unrelated trunk marker").returncode, 0)
        self.assertTrue(_sh(self.root, "git", "cherry", "main",
                            "lane/empty").stdout.startswith("-"))
        self.assertEqual(
            self.git.landed_state(self.root, "lane/empty", "main"),
            vcs.UNKNOWN)

    def test_a_CHERRY_PICK_ELSEWHERE_never_reads_as_landed(self):  # noqa: VACUOUS_ASSERTION — equal patch-ids plus the asserted '+ <sha>' line are the positive controls
        """THE COUNTEREXAMPLE THAT WAS PUT TO THIS LANE, measured and REFUTED.

        The concern: "cherry-pick a commit onto a throwaway branch; its
        patch-id now matches WHILE THE LANE ITSELF NEVER LANDED, so a reaper
        deletes on correlation." That is a true and fatal objection to a
        HAND-ROLLED reader that searches `git patch-id` across all refs. It is
        not true of `git cherry`, and the difference is exactly why the
        primitive is used instead of the plumbing: `git cherry <upstream>
        <head>` compares only against commits in UPSTREAM. A throwaway branch
        is not upstream, so it contributes nothing to the comparison.

        Pinned as a REGRESSION GUARD, because the tempting "optimisation" here
        is to look the patch-id up across all refs, and that rewrite would
        reintroduce the deletion-on-correlation the objection describes."""
        lane = self._lane("nevr", ["lanework"])
        lane_tip = _sh(self.root, "git", "rev-parse", lane).stdout.strip()
        self._commit("trunk-moved")
        # the cherry-pick lands on a THROWAWAY branch — never on main
        _sh(self.root, "git", "branch", "throwaway", "main")
        wt = os.path.join(self.tmp, "wt-throw")
        self.git.add_worktree(self.root, wt, "throwaway")
        self.assertEqual(
            _sh(wt, "git", "cherry-pick", lane_tip).returncode, 0)
        thrown = _sh(wt, "git", "rev-parse", "HEAD").stdout.strip()

        # the premise of the objection HOLDS: the patch-ids really do match
        self.assertEqual(self._patch_id(lane_tip), self._patch_id(thrown))
        # ...and the verdict is still KEEP, because main never received it
        self.assertIn("+ " + lane_tip,
                      _sh(self.root, "git", "cherry", "main", lane).stdout)
        self.assertEqual(self.git.landed_state(self.root, lane, "main"),
                         vcs.NOT_ANCESTOR)

    def test_the_seam_and_landreqs_reader_agree_on_the_shared_question(self):  # noqa: VACUOUS_ASSERTION — three exact expected verdicts per reader are the positive controls
        """TWO READERS OF ONE FACT DRIFT APART, so they are pinned together.

        `landreq._landing_proof` reached patch identity first, for the land /
        stall projection. It is NOT reusable by the reap path — it targets a
        `--git-dir` (the declared vcs.py exception) and it answers PER COMMIT,
        while retiring a branch is a question about the WHOLE range and a
        per-commit answer on the tip alone would call a partly-landed stack
        landed. So the seam owns the branch question and landreq keeps the
        commit question, and this test is the containment: on the case both can
        answer — ONE commit — they must never disagree."""
        from helm import landreq
        gitdir = os.path.join(self.root, ".git")
        cases = []
        merged = self._lane("agree-ff", ["ff"])
        _sh(self.root, "git", "merge", "-q", "--ff-only", merged)
        cases.append((merged, "ancestor"))
        rebased = self._lane("agree-rebased", ["replayed"])
        tip = _sh(self.root, "git", "rev-parse", rebased).stdout.strip()
        self._commit("trunk-moved")
        self.assertEqual(_sh(self.root, "git", "cherry-pick", tip).returncode, 0)
        cases.append((rebased, "patch-equivalent"))
        absent = self._lane("agree-absent", ["never"])
        cases.append((absent, "absent"))
        seam_of = {"ancestor": vcs.ANCESTOR,
                   "patch-equivalent": vcs.PATCH_EQUIVALENT,
                   "absent": vcs.NOT_ANCESTOR}
        for branch, expected in cases:
            proof = landreq._landing_proof(gitdir, branch, "main")
            self.assertEqual(proof, expected, branch)
            self.assertEqual(self.git.landed_state(self.root, branch, "main"),
                             seam_of[expected], branch)

    def test_wip_commit_stages_everything_under_the_janitor_identity(self):
        wt = os.path.join(self.tmp, "wt-wip")
        self.git.add_worktree(self.root, wt, "lane/wip", base="main")
        with open(os.path.join(wt, "rescued.txt"), "w") as f:
            f.write("never discarded")
        self.assertTrue(self.git.dirty(wt))
        rc, _out, err = self.git.wip_commit(wt, "wip: parked lane/wip")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.git.dirty(wt))
        log = _sh(wt, "git", "log", "-1", "--name-only",
                  "--format=%an%x09%ae%x09%s")
        self.assertIn("helm-work\thelm-work@local\twip: parked lane/wip",
                      log.stdout)
        self.assertIn("rescued.txt", log.stdout)

    def test_wip_commit_janitor_identity_outranks_inherited_per_seat_env_vars(self):
        wt = os.path.join(self.tmp, "wt-wip-seat")
        self.git.add_worktree(self.root, wt, "lane/wip-seat", base="main")
        with open(os.path.join(wt, "rescued.txt"), "w") as f:
            f.write("never discarded")
        with mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "seat-alice",
            "GIT_COMMITTER_NAME": "seat-alice",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_EMAIL": "alice@example.com",
        }):
            rc, _out, err = self.git.wip_commit(wt, "wip: parked lane/wip-seat")
        self.assertEqual(rc, 0, err)
        log = _sh(wt, "git", "log", "-1", "--name-only",
                  "--format=%an%x09%cn%x09%ae%x09%s")
        self.assertIn("helm-work\thelm-work\thelm-work@local\twip: parked lane/wip-seat",
                      log.stdout)

    def test_ahead_behind_counts_and_none_on_a_failed_read(self):
        wt = os.path.join(self.tmp, "wt-ab")
        self.git.add_worktree(self.root, wt, "lane/ab", base="main")
        self._commit("ahead1", where=wt)
        self.assertEqual(self.git.ahead_behind(self.root, "main", "lane/ab"),
                         ("0", "1"))
        self.assertEqual(self.git.ahead_behind(self.root, "lane/ab", "main"),
                         ("1", "0"))
        self.assertIsNone(self.git.ahead_behind(self.root, "main", "no/such"))


# ---------------------------------------------------------------------------
# backend selection — git today, jj never a crash
# ---------------------------------------------------------------------------

class SelectionTest(VcsBase):
    def _jj(self, *parts):
        """A jj-governed checkout: `.jj` beside a real `.git` (colocated)."""
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.join(path, ".jj"))
        os.makedirs(os.path.join(path, ".git"))
        return path

    def test_unset_selects_the_explicit_default_git(self):
        self.assertEqual(vcs.DEFAULT, vcs.GIT)
        self.assertEqual(vcs.select(target=self.root), (vcs.GIT, None))

    def test_helm_vcs_git_selects_git_quietly(self):
        for value in ("git", "GIT", "  git  "):
            os.environ["HELM_VCS"] = value
            self.assertEqual(vcs.select(target=self.root), (vcs.GIT, None),
                             value)

    def test_helm_vcs_jj_falls_back_to_git_with_one_note(self):
        os.environ["HELM_VCS"] = "jj"
        name, note = vcs.select(target=self.root)
        self.assertEqual(name, vcs.GIT)
        self.assertIn("jj", note)
        self.assertIn("not built yet", note)
        self.assertEqual(len(note.splitlines()), 1)

    def test_garbage_falls_back_to_git_with_one_note(self):
        os.environ["HELM_VCS"] = "banana"
        name, note = vcs.select(target=self.root)
        self.assertEqual(name, vcs.GIT)
        self.assertIn("unknown", note)
        self.assertIn("banana", note)
        self.assertEqual(len(note.splitlines()), 1)

    def test_only_helm_vcs_is_read_never_a_legacy_meld_spelling(self):
        # ENVIRONMENT.md's row says "legacy fallback: —"; the code must agree.
        # There has never been a MELD_VCS, so honoring one would be a lie.
        os.environ["MELD_VCS"] = "jj"
        self.assertEqual(vcs.select(target=self.root), (vcs.GIT, None))
        self.assertEqual(vcs.ENV_VAR, "HELM_VCS")

    def test_colocated_jj_marker_asks_for_jj_and_env_still_wins(self):
        jj = self._jj("colocated")
        self.assertEqual(vcs.detect(jj), vcs.JJ)   # jj drives, .git is the surface
        name, note = vcs.select(target=jj)
        self.assertEqual(name, vcs.GIT)
        self.assertIn("jj", note)
        os.environ["HELM_VCS"] = "git"          # the operator overrides detection
        self.assertEqual(vcs.select(target=jj), (vcs.GIT, None))

    # ── THE ROUTING BUG THIS CLASS EXISTS FOR ────────────────────────────────
    # Selection must follow the OPERATED repo, not the cwd a verb was typed in.
    # A cwd-derived answer picks git for a jj repo (and jj for a git one) the
    # moment the two differ — which, for a fleet driving many repos, is normal.

    def test_selection_follows_the_target_not_the_invoking_cwd(self):
        jj = self._jj("elsewhere")
        prior = os.getcwd()
        try:
            os.chdir(self.root)                  # cwd = a plain git repo
            self.assertEqual(vcs.detect(jj), vcs.JJ)
            self.assertIsNotNone(vcs.select(target=jj)[1])   # jj target: noted
            self.assertIsNone(vcs.select(target=self.root)[1])
            os.chdir(jj)                         # cwd = the jj repo
            self.assertEqual(vcs.detect(self.root), vcs.GIT)
            self.assertIsNone(vcs.select(target=self.root)[1])  # git target: quiet
        finally:
            os.chdir(prior)

    @staticmethod
    def _bare_backend_calls(source, label):
        """['<label>:<line>'] for every ARGUMENTLESS `backend(...)` CALL.

        AST, NEVER A LINE SCAN. A regex over source lines cannot tell a call
        from the same characters inside a comment, a docstring or a string
        literal — so prose that NAMES the forbidden form refuses the commit,
        and the refusal is indistinguishable from the real defect: same arm,
        same message, same path. The author then re-reads working code hunting
        a call that is not there. Comments and strings are invisible to the
        parser, which is the whole reason to ask the parser.

        It also reads the calls the line scan could not: `backend(` and its
        closing paren on different lines is one node here and two lines there.
        """
        try:
            tree = ast.parse(source, filename=label)
        except SyntaxError as exc:
            # UNPARSEABLE IS NOT CLEAN. A file this audit cannot read is a file
            # whose call sites it has not checked, and returning [] for it
            # would report the unexamined world as compliant.
            raise AssertionError(
                "%s could not be parsed, so its call sites are UNCHECKED, "
                "which this audit must never report as clean: %s"
                % (label, exc))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.args or node.keywords:
                continue
            func = node.func
            named = (func.attr if isinstance(func, ast.Attribute)
                     else func.id if isinstance(func, ast.Name) else None)
            if named == "backend":
                out.append("%s:%d" % (label, node.lineno))
        return out

    def test_every_migrated_call_site_passes_its_target(self):  # noqa: VACUOUS_ASSERTION — the positive control IS unconditional and IS on this observable: `audit([("planted", ...)])` builds the same finding list by the same walk over the same tree and must come back non-empty. The rung credits a control only from the SAME call's other channels, and these are two calls to one local helper, which it cannot see across. Six further controls above it fix the detector's answer in both directions on known inputs
        # source-driven: a bare `vcs.backend()` anywhere in helm/ is the bug —
        # it silently re-introduces cwd-derived selection.
        #
        # THE CONTROLS COME FIRST, and they are what this arm's silence means.
        # An audit that found nothing is indistinguishable from one that cannot
        # find anything, so the detector is made to answer both ways on inputs
        # whose answers are known before it is pointed at the tree.
        self.assertEqual(
            ["probe:1"], self._bare_backend_calls("vcs.backend()\n", "probe"),
            "control: the detector must find a real argumentless call")
        self.assertEqual(
            ["probe:2"],
            self._bare_backend_calls("x = (\n  backend()\n)\n", "probe"),
            "control: a bare Name call, and one the line scan would misplace")
        self.assertEqual(
            [], self._bare_backend_calls("vcs.backend(root)\n", "probe"),
            "control: a call that PASSES ITS TARGET is the compliant form")
        self.assertEqual(
            [], self._bare_backend_calls("vcs.backend(root=r)\n", "probe"),
            "control: and so is the keyword spelling")
        self.assertEqual(
            [],
            self._bare_backend_calls(
                '# never write vcs.backend() here\n'
                '"""Nor vcs.backend() in a docstring."""\n'
                's = "vcs.backend()"\n', "probe"),
            "THE DEFECT THIS ARM EXISTS TO NOT HAVE: prose naming the "
            "forbidden form is not the forbidden form")
        self.assertEqual(
            ["probe:2"],
            self._bare_backend_calls(
                '# vcs.backend() named in a comment\n'
                'vcs.backend()\n', "probe"),
            "control: and the prose above a real call does not hide it")

        sources = list(_helm_sources())
        self.assertTrue(sources, "control: the tree walk found source files")

        def audit(extra=()):
            """The finding list over the real tree, plus any planted source."""
            found = []
            for name in sources:
                with open(os.path.join(_PKG, name), encoding="utf-8") as f:
                    found += self._bare_backend_calls(f.read(), name)
            for label, src in extra:
                found += self._bare_backend_calls(src, label)
            return found

        # THE CONTROL ON THIS EXACT OBSERVABLE. The assertion below is an
        # ABSENCE, and an absence proves nothing unless the same list, built by
        # the same walk over the same tree, is shown to fill when a defect is
        # present. Planting one source is the only difference between the two
        # calls, so a detector that had stopped detecting anything fails here.
        self.assertEqual(["planted:1"], audit([("planted", "vcs.backend()\n")]),
                         "the audit must report a planted defect, and ONLY it")

        bare = audit()
        self.assertEqual(bare, [], "cwd-derived backend() calls: %r" % bare)

    def test_detection_walks_up_and_the_nearest_marker_wins(self):
        nested = os.path.join(self._jj("outer"), "sub", "deep")
        os.makedirs(nested)
        # a path deep inside a jj workspace still resolves to jj
        self.assertEqual(vcs.detect(nested), vcs.JJ)
        # ...but an inner git checkout is NOT hijacked by the jj above it
        inner = os.path.join(self.tmp, "outer", "inner-git")
        os.makedirs(os.path.join(inner, ".git"))
        self.assertEqual(vcs.detect(inner), vcs.GIT)
        self.assertEqual(vcs.detect(os.path.join(inner, "a", "b")), vcs.GIT)
        # the real repo minted in setUp, and a path with no repo above it
        self.assertEqual(vcs.detect(self.root), vcs.GIT)
        self.assertIsNone(vcs.detect("/"))

    def test_detection_survives_a_vanished_cwd_and_an_io_error(self):
        with mock.patch.object(vcs.os, "getcwd", side_effect=OSError("gone")):
            self.assertIsNone(vcs.detect())
            self.assertEqual(vcs.select(), (vcs.GIT, None))
        with mock.patch.object(vcs.os, "stat", side_effect=OSError("EIO")):
            self.assertIsNone(vcs.detect(self.root))     # UNKNOWN, not ABSENT
        with mock.patch.object(vcs.os.path, "realpath",
                               side_effect=OSError("ELOOP")):
            self.assertIsNone(vcs.detect(self.root))

    # ── the marker-identity matrix: helm must agree with `git -C` ─────────────

    def _portal(self, name):
        """A git repo containing a symlink that points INTO a separate jj repo.

        This shape is what actually separates a lexical answer from git's: a link
        whose target is in ANOTHER repo. (A link pointing at its own repo proves
        nothing — `os.stat` follows links itself, so even a lexical walk finds a
        marker through it. An earlier version of this test made exactly that
        mistake and passed against the bug it claimed to catch.)
        -> (portal path, the jj repo root)
        """
        outer = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(outer, ".git"))          # a GIT repo...
        target = self._jj(name + "-target")               # ...and a JJ repo
        os.makedirs(os.path.join(target, "sub"))
        portal = os.path.join(outer, "portal")
        os.symlink(os.path.join(target, "sub"), portal)
        return portal, target

    def test_a_symlink_out_of_one_repo_into_another_follows_git(self):
        portal, target = self._portal("crossing")
        # `git -C <portal>` chdirs into the TARGET and discovers the jj repo; a
        # lexical walk would instead climb the LINK'S PARENT and answer git
        self.assertEqual(vcs.detect(portal), vcs.JJ)
        self.assertEqual(vcs.resolved_target(portal),
                         os.path.realpath(os.path.join(target, "sub")))

    def test_a_nonexistent_tail_through_a_symlink_still_finds_the_repo(self):
        # a lane path that does not exist yet is the NORMAL case: work.claim asks
        # about <repo>-wt/<lane> before `worktree add` creates it
        portal, target = self._portal("pending")
        self.assertEqual(vcs.detect(os.path.join(portal, "not", "yet", "here")),
                         vcs.JJ)
        self.assertEqual(vcs.resolved_target(os.path.join(portal, "nope")),
                         os.path.join(os.path.realpath(target), "sub", "nope"))

    def test_an_unreadable_marker_never_lets_an_outer_marker_hijack(self):
        """THE HIJACK: a real git checkout whose `.git` helm cannot stat, with an
        accessible `.jj` above it. Treating unreadable as absent hands a git repo
        to the jj backend — the vacuous-pass class, applied to the filesystem."""
        outer = self._jj("hijack")
        inner = os.path.join(outer, "locked", "checkout")
        os.makedirs(os.path.join(inner, ".git"))
        locked = os.path.join(outer, "locked")
        os.chmod(locked, 0o000)
        try:
            state = vcs.marker_state(os.path.join(inner, ".git"))
            self.assertEqual(state, vcs.UNKNOWN)     # not ABSENT
            self.assertIsNone(vcs.detect(inner))     # NOT vcs.JJ
            self.assertEqual(vcs.select(target=inner), (vcs.GIT, None))
        finally:
            os.chmod(locked, 0o755)
        self.assertEqual(vcs.detect(inner), vcs.GIT)  # readable again: honest

    def test_a_readable_marker_beside_an_unreadable_one_still_decides(self):
        # same-level fact, not an outward adoption: `.git` is PRESENT here, so
        # stopping the walk does not mean refusing to answer
        root = os.path.join(self.tmp, "mixed")
        os.makedirs(os.path.join(root, ".git"))
        with mock.patch.object(vcs, "marker_state", side_effect=lambda p: (
                vcs.UNKNOWN if p.endswith(".jj") else vcs.PRESENT)):
            self.assertEqual(vcs.detect(root), vcs.GIT)

    def test_the_hop_bound_is_parent_edges_exactly(self):
        deep = os.path.join(self.tmp, "chain")
        os.makedirs(deep)
        # a marker exactly _MAX_WALK parent EDGES up is FOUND
        leaf = os.path.join(deep, *(["d"] * vcs._MAX_WALK))
        os.makedirs(leaf)
        os.makedirs(os.path.join(deep, ".git"))
        self.assertEqual(vcs.detect(leaf), vcs.GIT)
        # one edge further out is NOT (nothing in the tmp ancestry carries one)
        beyond = os.path.join(leaf, "d")
        os.makedirs(beyond)
        self.assertIsNone(vcs.detect(beyond))

    def test_backend_returns_a_gitvcs_for_every_input_and_notes_once(self):
        noted = set(vcs._NOTED)
        try:
            for value in (None, "git", "jj", "banana", ""):
                vcs._NOTED.clear()
                if value is None:
                    os.environ.pop("HELM_VCS", None)
                else:
                    os.environ["HELM_VCS"] = value
                err = _Capture()
                with mock.patch.object(vcs.sys, "stderr", err):
                    first = vcs.backend(self.root)
                    again = vcs.backend(self.root)
                self.assertIsInstance(first, vcs.GitVcs)
                self.assertIs(first, again)     # one instance per process
                self.assertLessEqual(len(err.text.splitlines()), 1)
        finally:
            vcs._NOTED.clear()
            vcs._NOTED.update(noted)

    def test_the_interface_is_a_checklist_a_backend_cannot_half_implement(self):
        import inspect
        ops = [n for n in vars(vcs.Vcs) if not n.startswith("__")
               and callable(getattr(vcs.Vcs, n))]
        self.assertTrue(ops)
        for name in ops:
            sig = inspect.signature(getattr(vcs.Vcs, name))
            need = [p for p in list(sig.parameters.values())[1:]
                    if p.default is p.empty and p.kind is p.POSITIONAL_OR_KEYWORD]
            # a forgotten op must SAY so, never silently answer wrong
            with self.assertRaises(NotImplementedError, msg=name):
                getattr(vcs.Vcs(), name)(*([self.root] * len(need)))
            self.assertTrue(getattr(vcs.GitVcs, name).__doc__ or
                            getattr(vcs.Vcs, name).__doc__, name)
            self.assertTrue(hasattr(vcs.GitVcs, name), name)


class BackendIsTheAuthorityTest(VcsBase):
    """Not just one parser — one AUTHORITY. A registry read must REACH the
    selected backend, argv and format both, or a jj backend would be ignored by
    a call site that hardcoded git's."""

    def test_worktree_registry_read_reaches_the_selected_backend(self):
        calls = []

        class Fake(vcs.GitVcs):
            name = "fake"

            def worktrees(self, root, read=None):
                calls.append((root, read))
                return [{"path": "/fake", "branch": "refs/heads/f",
                         "locked": False, "reason": ""}], None

        with mock.patch.dict(vcs._INSTANCES, {vcs.GIT: Fake()}):
            rows, error = work._worktree_records(self.root)
            self.assertEqual([w["path"] for w in work.worktrees(self.root)],
                             ["/fake"])
        self.assertIsNone(error)
        self.assertEqual(rows[0]["path"], "/fake")
        self.assertEqual([c[0] for c in calls], [self.root, self.root])
        # the raw spawn is handed in (the module's one git boundary), but the
        # argv and the parse belong to the backend
        self.assertIs(calls[0][1], work._lanes._git_bytes)

    def test_a_backend_owning_its_own_argv_and_format_is_honored(self):
        class Fake(vcs.GitVcs):
            def worktrees(self, root, read=None):
                rc, out, err = (read or self.run)(root, "log", "-1",
                                                 "--format=%H")
                return ([{"path": out.decode().strip(), "branch": None,
                          "locked": False, "reason": ""}], None) if rc == 0 \
                    else ([], err.decode())

        with mock.patch.dict(vcs._INSTANCES, {vcs.GIT: Fake()}):
            rows, error = work._worktree_records(self.root)
        self.assertIsNone(error)
        self.assertEqual(rows[0]["path"], self.shas[1])   # a NON-git format won

    def test_the_git_backends_own_read_path_still_works_unwrapped(self):
        rows, error = self.git.worktrees(self.root)        # read=None
        self.assertIsNone(error)
        self.assertEqual([r["branch"] for r in rows], ["refs/heads/main"])


# ---------------------------------------------------------------------------
# the honesty tripwire — the seam's real reach, pinned
# ---------------------------------------------------------------------------

_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "helm")


def _helm_sources():
    """helm/*.py plus one level of package submodules — a module decomposed
    into a PACKAGE must never blind these sweeps."""
    out = [f for f in sorted(os.listdir(_PKG)) if f.endswith(".py")]
    for d in sorted(os.listdir(_PKG)):
        sub = os.path.join(_PKG, d)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, "__init__.py")):
            out += [os.path.join(d, f) for f in sorted(os.listdir(sub))
                    if f.endswith(".py")]
    return out


# The spawn functions a module could reach for. `getoutput`/`getstatusoutput`
# take a shell STRING rather than an argv list; both forms resolve below.
_SPAWNERS = ("run", "Popen", "call", "check_call", "check_output",
             "getoutput", "getstatusoutput")


def _class_binding_nodes(tree):
    """Assignments whose bare target lives in a class namespace, not a scope.

    Treating ``class X: run = subprocess.run`` as the ordinary alias ``run``
    both consumes the evidence and invents a module-level binding that does not
    exist. The nearest lexical scope decides, so assignments under a class-body
    ``if`` are class attributes while assignments inside its methods are locals.
    """
    import ast
    parent = {id(child): node for node in ast.walk(tree)
              for child in ast.iter_child_nodes(node)}
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        scope = parent.get(id(node))
        while scope is not None and not isinstance(
                scope, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef,
                        ast.Lambda)):
            scope = parent.get(id(scope))
        if isinstance(scope, ast.ClassDef):
            out.add(id(node))
    return out


def _spawn_bindings(tree):
    """Return the explicit grammar's known and unresolved spawn bindings.

    The audit does NOT claim to understand arbitrary Python data flow. It decides
    imports, simple/annotated aliases, and recursively composed
    ``functools.partial`` calls. Binding discovery feeds a separate reference
    ledger: direct spawner attributes, imported/derived aliases and subprocess
    ``getattr`` lookups must be consumed by that grammar or remain unresolved.
    """
    import ast
    mods, direct, consts, partials, unresolved = set(), {}, {}, set(), set()
    class_bindings = _class_binding_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    mods.add(alias.asname or alias.name)
                elif alias.name == "functools":
                    partials.add((alias.asname or alias.name) + ".partial")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SPAWNERS:
                    direct[alias.asname or alias.name] = _binding()
        elif isinstance(node, ast.ImportFrom) and node.module == "functools":
            for alias in node.names:
                if alias.name == "partial":
                    partials.add(alias.asname or alias.name)
        else:
            if id(node) in class_bindings:
                continue
            for target, value in _bindings(node):
                if isinstance(value, ast.Constant) \
                        and isinstance(value.value, str):
                    consts.setdefault(target, set()).add(value.value)

    for _pass in range(8):                 # aliases + nested partials -> fixpoint
        changed = False
        for node in ast.walk(tree):
            if id(node) in class_bindings:
                continue
            for target, value in _bindings(node):
                if _names_partial(value, partials) and target not in partials:
                    partials.add(target)
                    changed = True
        for node in ast.walk(tree):
            if id(node) in class_bindings:
                continue
            pairs = _bindings(node)
            for target, value in pairs:
                b = _spawner_binding(value, mods, direct, partials)
                if b is not None and direct.get(target) != b:
                    direct[target] = b
                    unresolved.discard(target)
                    changed = True
                elif b is None and _references_spawner_binding(
                        value, mods, direct, consts, partials, unresolved):
                    if target not in unresolved:
                        unresolved.add(target)
                        changed = True
            # Unsupported destructuring is outside the decided grammar, but its
            # target names must still remain visible when they are called later.
            if not pairs:
                value = _binding_value(node)
                if value is not None and _references_spawner_binding(
                        value, mods, direct, consts, partials, unresolved):
                    for target in _binding_target_names(node):
                        if target not in unresolved:
                            unresolved.add(target)
                            changed = True
        if not changed:
            break
    return mods, direct, consts, partials, unresolved


def _binding(args=None, kw=None):
    """A spawn callable plus the complete call state already bound by partial.

    ``subprocess.run`` forwards positional arguments to ``Popen``; argv is only
    position zero, executable is position two, and shell is position eight.
    Keeping just argv therefore loses real execution semantics. The binding owns
    the full positional vector and keyword map until the final call is composed.
    """
    return {"args": tuple(args or ()), "kw": dict(kw or {})}


def _spawner_binding(value, mods, direct, partials):
    """The decided binding an expression evaluates to, else None.

    Recursive partial composition follows Python: earlier positionals precede
    later ones, while later keyword arguments override earlier keywords.
    """
    import ast
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) \
            and value.value.id in mods and value.attr in _SPAWNERS:
        return _binding()
    if isinstance(value, ast.Name) and value.id in direct:
        b = direct[value.id]
        return _binding(b["args"], b["kw"])
    if isinstance(value, ast.Call) and _names_partial(value.func, partials):
        if not value.args:
            return None
        inner = _spawner_binding(value.args[0], mods, direct, partials)
        if inner is None:
            return None
        args = inner["args"] + tuple(value.args[1:])
        kw = dict(inner["kw"])
        for k in value.keywords:
            if k.arg is None:               # **kwargs is outside the grammar
                return None
            kw[k.arg] = k.value
        return _binding(args, kw)
    return None


def _bindings(node):
    """Bindings inside the decided grammar: simple Assign and AnnAssign."""
    import ast
    if isinstance(node, ast.Assign):
        return [(t.id, node.value) for t in node.targets
                if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
            and node.value is not None:
        return [(node.target.id, node.value)]
    return []


def _binding_value(node):
    """The RHS of any binding form, including unsupported destructuring."""
    import ast
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        return node.value
    return None


def _target_names(target):
    """Every name a target binds; tuple/list destructuring stays unresolved."""
    import ast
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for item in target.elts:
            out += _target_names(item)
        return out
    return []


def _binding_target_names(node):
    import ast
    if isinstance(node, ast.Assign):
        out = []
        for target in node.targets:
            out += _target_names(target)
        return out
    if isinstance(node, ast.AnnAssign):
        return _target_names(node.target)
    if isinstance(node, ast.NamedExpr):
        return _target_names(node.target)
    return []


def _getattr_spawner_ref(node, mods, consts):
    """A subprocess ``getattr`` that names, or may name, a spawner.

    A statically-known non-spawner is clear. A dynamic attribute is unresolved:
    assuming it is harmless would recreate the unknown-as-absent bug.
    """
    import ast
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) \
            or node.func.id != "getattr" or len(node.args) < 2 \
            or not isinstance(node.args[0], ast.Name) \
            or node.args[0].id not in mods:
        return False
    name = _const_str(node.args[1], consts)
    return name is None or name in _SPAWNERS


def _spawner_reference_nodes(node, mods, direct, consts, unresolved):
    """Direct capability references in ``node``, independent of call decoding.

    This is the accounting surface: subprocess spawner attributes, known or
    unresolved aliases in load position, and potentially-spawning ``getattr``
    lookups. Container/class/callback transport does not need to be understood;
    its source reference remains visible until a decided binding or call consumes
    it.
    """
    import ast
    out = []
    for item in ast.walk(node):
        if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name) \
                and item.value.id in mods and item.attr in _SPAWNERS:
            out.append(item)
        elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) \
                and item.id in set(direct) | set(unresolved):
            out.append(item)
        elif _getattr_spawner_ref(item, mods, consts):
            out.append(item)
    return out


def _binding_reference_nodes(node, mods, direct, partials):
    """References consumed specifically as a decided callable binding."""
    import ast
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id in mods and node.attr in _SPAWNERS:
        return [node]
    if isinstance(node, ast.Name) and node.id in direct:
        return [node]
    if isinstance(node, ast.Call) and _names_partial(node.func, partials) \
            and node.args:
        return _binding_reference_nodes(node.args[0], mods, direct, partials)
    return []


def _references_spawner_binding(node, mods, direct, consts, partials,
                                  unresolved):
    """Whether an expression may carry a spawner callable outside grammar.

    A CALL to ``subprocess.run`` returns a CompletedProcess, not another spawner;
    treating that result as an unresolved callable polluted ordinary local names
    throughout a module. Containers, conditionals, ``getattr`` and ``partial``
    constructors can carry one and therefore propagate an unresolved target.
    """
    import ast
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id in mods and node.attr in _SPAWNERS
    if isinstance(node, ast.Name):
        return node.id in set(direct) | set(unresolved)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_references_spawner_binding(
            item, mods, direct, consts, partials, unresolved)
                   for item in node.elts)
    if isinstance(node, ast.Dict):
        return any(_references_spawner_binding(
            item, mods, direct, consts, partials, unresolved)
                   for item in node.keys + node.values if item is not None)
    if isinstance(node, ast.IfExp):
        return any(_references_spawner_binding(
            item, mods, direct, consts, partials, unresolved)
                   for item in (node.body, node.orelse))
    if _getattr_spawner_ref(node, mods, consts):
        return True
    if isinstance(node, ast.Call) and _names_partial(node.func, partials) \
            and node.args:
        return _references_spawner_binding(
            node.args[0], mods, direct, consts, partials, unresolved)
    return False


def _references_spawner(node, mods, direct, consts, unresolved):
    """Whether an expression directly references a spawn capability."""
    return bool(node is not None and _spawner_reference_nodes(
        node, mods, direct, consts, unresolved))


def _names_partial(func, partials):
    """`partial` / `P` (from-import, possibly aliased) or `functools.partial`
    (module import, possibly aliased) — resolved through the same alias set as
    everything else rather than matched on the literal spelling."""
    import ast
    if isinstance(func, ast.Name):
        return func.id in partials
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id + "." + func.attr) in partials
    return False


def _const_str(node, consts):
    """A string expression's value when it is statically knowable, else None:
    literals, single-valued names, `+` concatenation, and f-strings whose every
    piece resolves. Anything else is honestly unknown."""
    import ast
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        values = consts.get(node.id)
        return next(iter(values)) if values and len(values) == 1 else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_str(node.left, consts), _const_str(node.right, consts)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        out = ""
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                out += piece.value
            elif isinstance(piece, ast.FormattedValue) and not piece.format_spec:
                value = _const_str(piece.value, consts)
                if value is None:
                    return None
                out += value
            else:
                return None
        return out
    return None


def _argv_program(argv, consts):
    """The PROGRAM an argv expression runs, or None when it is not static.
    Handles a list/tuple literal, a concatenation (the program is in the LEFT
    operand — `[GIT, "-C", cwd] + list(args)`, exactly how the seam spells it),
    and a shell string (first token)."""
    import ast
    if isinstance(argv, (ast.List, ast.Tuple)):
        return _const_str(argv.elts[0], consts) if argv.elts else None
    if isinstance(argv, ast.BinOp) and isinstance(argv.op, ast.Add):
        return _argv_program(argv.left, consts)
    text = _const_str(argv, consts)
    return text.split()[0] if text and text.split() else None


def _is_git(program):
    """`git`, `/usr/bin/git`, `git.exe` — an absolute path must not smuggle it
    past, and a merely git-ISH name (`gitk`, `my-git-wrapper`) is not it."""
    return os.path.basename(program).lower() in ("git", "git.exe")


_WEB_SPLIT_SPAWN_MODULES = frozenset({
    "web.py", "web_cache.py", "web_chat.py", "web_chat_rooms.py",
    "web_common.py", "web_configs.py", "web_core.py", "web_land.py",
    "web_land_model.py", "web_ledger.py", "web_multiplayer.py",
    "web_quota.py", "web_roster.py", "web_server.py", "web_sessions.py",
    "web_sse.py",
})


def _spawn_owner(module, tree, lineno):
    """Stable semantic key for spawn sites moved by the web split."""
    if module not in _WEB_SPLIT_SPAWN_MODULES:
        return module
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
                and node.lineno <= lineno <= node.end_lineno):
            return "web:" + node.name
    return module


def scan_spawns(source, module="<source>"):
    """Return decided Git, decided static, and unresolved spawn call sites.

    The decided grammar covers subprocess imports, simple aliases, literal/string
    argv, and recursively composed ``functools.partial`` bindings. A separate
    accounting pass tracks direct subprocess spawner attributes, known/derived
    aliases and subprocess ``getattr`` lookups; any reference not consumed by a
    decided binding or call is unresolved. This is total only over that named
    reference surface, not arbitrary Python introspection or data flow.
    """
    import ast
    tree = ast.parse(source)
    mods, direct, consts, partials, unknown_names = _spawn_bindings(tree)
    git, static, unresolved, consumed = [], [], [], set()

    # A resolved alias/partial binding consumes only its callable chain. Spawner
    # references transported in a bound argument remain unconsumed and therefore
    # unresolved below.
    class_bindings = _class_binding_nodes(tree)
    for node in ast.walk(tree):
        if id(node) in class_bindings:
            continue
        for _target, value in _bindings(node):
            if _spawner_binding(value, mods, direct, partials) is not None:
                consumed.update(id(ref) for ref in _binding_reference_nodes(
                    value, mods, direct, partials))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        site = (_spawn_owner(module, tree, node.lineno), node.lineno)
        bound = _spawner_binding(node.func, mods, direct, partials)
        if bound is None:
            if _references_spawner(
                    node.func, mods, direct, consts, unknown_names):
                unresolved.append(site)
            continue
        consumed.update(id(ref) for ref in _binding_reference_nodes(
            node.func, mods, direct, partials))

        # functools.partial prepends bound positionals; call-site keywords replace
        # bound keywords. Preserve the COMPLETE vector because subprocess.run
        # forwards it to Popen (args=0, executable=2, shell=8).
        args = bound["args"] + tuple(node.args)
        kw = dict(bound["kw"])
        unknown_kw = False
        for item in node.keywords:
            if item.arg is None:
                unknown_kw = True
            else:
                kw[item.arg] = item.value
        if unknown_kw:
            unresolved.append(site)
            continue

        kw_argv = kw.get("args")
        if args and kw_argv is not None:       # runtime duplicate-value error
            unresolved.append(site)
            continue
        argv = args[0] if args else kw_argv

        positional_exe = args[2] if len(args) > 2 else None
        keyword_exe = kw.get("executable")
        if positional_exe is not None and keyword_exe is not None:
            unresolved.append(site)
            continue
        exe = keyword_exe if keyword_exe is not None else positional_exe

        positional_shell = args[8] if len(args) > 8 else None
        keyword_shell = kw.get("shell")
        if positional_shell is not None and keyword_shell is not None:
            unresolved.append(site)
            continue
        shell_node = keyword_shell if keyword_shell is not None \
            else positional_shell
        if shell_node is None:
            shell = False
        elif isinstance(shell_node, ast.Constant) \
                and type(shell_node.value) is bool:
            shell = shell_node.value
        else:
            unresolved.append(site)          # unknown is never assumed False
            continue

        program = None if argv is None else _argv_program(argv, consts)
        if exe is not None and not shell:
            program = _const_str(exe, consts)
        if program is None:
            unresolved.append(site)
            continue
        static.append(site)
        if _is_git(program):
            git.append(site)

    # REFERENCE ACCOUNTING, after call classification: every direct capability
    # reference not consumed as a decided alias/partial/final call is residue. This
    # catches transport through dicts, class attributes, callbacks and computed
    # getattr without pretending to evaluate those data-flow shapes.
    for ref in _spawner_reference_nodes(
            tree, mods, direct, consts, unknown_names):
        if id(ref) not in consumed:
            owner = _spawn_owner(module, tree, ref.lineno)
            unresolved.append((owner, ref.lineno))
    return sorted(set(git)), sorted(set(static)), sorted(set(unresolved))


def _sweep_helm():
    """The three buckets over the whole package."""
    git, static, unresolved = [], [], []
    for fn in _helm_sources():
        with open(os.path.join(_PKG, fn), encoding="utf-8") as f:
            g, s, u = scan_spawns(f.read(), fn)
        git += g
        static += s
        unresolved += u
    return sorted(git), sorted(static), sorted(unresolved)


# The allowlist IS the law (the display-launder tripwire's pattern): every module
# still spawning git directly appears here WITH A TRUE REASON. A wrong reason is
# worse than no allowlist, because the reason IS the audit trail — the first
# version of this dict claimed automap could not be migrated without a selection
# cycle, which was simply false (vcs.detect reads the filesystem and calls
# nothing in automap); automap is migrated now.
# `helm/vcs.py` is the only entry that is not debt: it is the seam.
_DIRECT_SPAWN_DEBT = {
    "vcs.py": "THE SEAM — the sanctioned spawn site, not debt",
    "docref_guard.py": "THE NEVERTRACK CLASS, same as inflight_gate.py: a pre-commit rung snapshotted into the hooks dir and script-run from there, where helm is NOT importable — it cannot reach the seam by construction. ONE spawn helper, and it must stay stdlib-only for exactly that reason",
    "world_prose_guard.py": "THE NEVERTRACK CLASS: a pre-commit rung "
                            "snapshotted into the hooks dir and script-run "
                            "without an importable helm package. One git "
                            "helper owns staged-tree and committed-population "
                            "reads; the module stays stdlib-only.",
    "retired_name_rung.py": "THE NEVERTRACK CLASS: a pre-commit rung "
                            "snapshotted into the hooks dir and script-run "
                            "without an importable helm package. One git "
                            "helper owns the root, staged-diff and index "
                            "reads; the module stays stdlib-only.",
    "inflight_gate.py": "THE NEVERTRACK CLASS: a pre-commit rung snapshotted "
                        "into the hooks dir and script-run from there, where "
                        "helm is NOT importable — so it cannot reach the seam "
                        "by construction. Proven twice by @codex on 2026-08-03: "
                        "importing helm made it inert everywhere it was "
                        "installed, and baking the installer's source path made "
                        "the guard die with the tree it came from",
    "seatname_guard.py": "THE NEVERTRACK CLASS, same as inflight_gate.py and "
                         "docref_guard.py: a pre-commit rung snapshotted into "
                         "the hooks dir and script-run from there, where helm "
                         "is NOT importable — it cannot reach the seam by "
                         "construction. TWO reads — the committing worktree "
                         "and ONE captured index snapshot, taken before and "
                         "after the scan so a concurrent `git add` becomes a "
                         "refusal — and it stays stdlib-only for that reason",
    "orphaned_mock.py": "THE NEVERTRACK CLASS: a pre-commit rung snapshotted "
                        "into the hooks dir and script-run from there, where "
                        "helm is NOT importable — it cannot reach the seam by "
                        "construction. ONE spawn helper covering three reads: "
                        "the repository root, the staged name list, and each "
                        "staged blob, and the module stays stdlib-only for "
                        "exactly that reason",
    "cli.py": "front-door source-tree equivalence probe before verb dispatch; "
              "ordinary migration debt with a measured two-call floor",
    "landreq.py": "declared slice exception: one `git --git-dir` observation "
                  "helper owns ancestry, patch identity, and trunk reads; "
                  "ordinary migration debt in the land-receipt slice",
    # MOVED, NOT ADDED, and the arithmetic is the check: all FOUR probes
    # relocated to seats_identity.py in the seats.py split, and seats.py
    # now spawns git ZERO times. The debt follows the code — leaving the
    # old key would vouch for a spawn that is not there any more, and
    # adding a new one beside it would double-count a single debt.
    "seats_identity.py": "4 repo-identity probes (is-inside-work-tree / "
                         "bare / toplevel / common-dir) behind the seat "
                         "name and project resolvers, on the hot claim "
                         "path; ordinary migration debt, inherited "
                         "verbatim from seats.py by the split",
    "dispatches.py": "6 write-boundary repo/ref-resolution spawns (toplevel, "
                     "common-dir, disambiguate, show-ref --verify, rev-parse "
                     "commit, for-each-ref raw-SHA unique-local-tip binding) "
                     "plus 3 movement-evidence probes in the moved-lane verdict "
                     "guard (rev-parse branch head, merge-base --is-ancestor, "
                     "rev-list -g reflog); head/ancestry have seam equivalents, "
                     "the reflog and exact-tip branch reads do not yet — ordinary "
                     "migration debt",
    "record.py": "per-turn dirty probe and the tree it was taken in, both fail-open by contract; the seam exposes no --show-toplevel, and common_dir cannot answer it (a linked worktree shares its main repo's common dir, so it matches everywhere); debt",
    "hardcode.py": "the staged-diff scanner runs as a PLAIN SCRIPT under the "
                   "composed pre-commit hook with no importable helm package "
                   "(the nevertrack/hostpath_guard class — canonical path "
                   "baked at install, a lane cannot neuter the rung for its "
                   "own commit), so its three git spawns (diff --cached x2, "
                   "cat-file) cannot route through the seam",
    "lineage.py": "dirty probe on the lineage edge; ordinary migration debt",
    "rearm.py": "toplevel + `show -s --format` build stamp; ordinary debt",
    "seat_launch_assets.py": "toplevel probe during spawn homing; ordinary migration debt",
    "web:_roster_git_one": "`log -1` row for the project panel; semantic owner survives the web.py physical split; ordinary migration debt",
    "wiring.py": "2 read-only observation calls (diff --name-only --diff-filter=A, ls-files --others) answering ONE question: which modules did THIS tree add. Runs inside the Stop hook, so it is fail-open by contract — a None answer means 'could not look' and leaves the gate open; ordinary migration debt",
    "hostpath_guard.py": "6 read-only observations (config --get-regexp "
                         "of remote.* and url.*, ls-remote --heads --tags of "
                         "the push URL, remote get-url, rev-list --objects, "
                         "cat-file --batch-check, cat-file --batch) behind "
                         "ONE _git helper. Same standing "
                         "exception class as nevertrack.py: the pre-push guard "
                         "executes this file as a PLAIN SCRIPT "
                         "(python3 hostpath_guard.py --pre-push) with zero helm "
                         "imports, by design twice over — the hook must run in "
                         "trees where no helm package is importable, and baking "
                         "the canonical scanner path means a lane's checked-out "
                         "copy cannot be edited to neuter the guard for that "
                         "lane's own push. A seam import would re-open exactly "
                         "that door.",
    "nevertrack.py": "5 read-only index+HEAD observations (diff --cached "
                     "--name-only for the staged set, diff --cached -U0 for the "
                     "bytes this commit ADDS, cat-file blob :0: for the staged "
                     "blob, cat-file -e HEAD: for whether the path is new, "
                     "rev-parse --show-toplevel) behind ONE _git helper. "
                     "NOT migration debt and never routable through the seam: the "
                     "pre-commit guard executes this file as a PLAIN SCRIPT "
                     "(python3 nevertrack.py --staged) with zero helm imports, by "
                     "design twice over — the hook must run in trees where no helm "
                     "package is importable, and baking the canonical scanner path "
                     "means a lane's checked-out copy (or its vcs.py) cannot be "
                     "edited to neuter the guard for that lane's own commit. A "
                     "seam import would re-open exactly that door.",
    "vacuous_assertion.py": "3 read-only staged-index observations (diff --cached "
                            "--name-only, cat-file blob :0:, diff --cached -U0) "
                            "behind ONE _git helper. Standing canonical-script "
                            "exception: pre-commit must parse the staged blob in "
                            "trees where helm is not importable, and a lane may "
                            "not edit the detector that judges its own commit.",
    "silent_cap.py":
        "A PRE-COMMIT RUNG, the nevertrack class: the installer snapshots "
        "its bytes beside the shared hook, where no helm package exists to "
        "import, so it cannot reach the vcs seam, and a lane must not be able "
        "to edit the rung that judges its own staged set. Four read-only "
        "observations behind ONE _git helper — diff --cached --name-only for "
        "the staged paths, cat-file blob :0: for the staged bytes, diff "
        "--cached -U0 for the added line ranges, and ls-tree/show for the "
        "--all population pass.",
    "splitbudget.py":
        "A PRE-COMMIT RUNG, SAME LAW AS ITS SIBLINGS ABOVE: the "
        "installer snapshots its bytes beside the hook, where no "
        "helm package exists to import, so it cannot reach the vcs "
        "seam. Two reads only — `diff --cached --name-only` for the "
        "staged paths and `show <ref>:<path>` for the line counts — "
        "and it fails OPEN, printing UNMEASURED and returning 0 "
        "when either refuses.",
    "conflict_marker.py": "7 read-only index+HEAD observations (diff --cached "
                          "--name-status -z -M for the rename-aware staged "
                          "set, ls-files --stage for stage-0 OIDs, check-attr "
                          "TWICE — --cached AND working-tree — for per-path "
                          "conflict-marker-size from BOTH attribute sources, "
                          "cat-file blob <oid> for the staged bytes, diff "
                          "--cached -U0 --no-textconv ':(literal)' for the "
                          "added line ranges, rev-parse --show-toplevel) "
                          "behind ONE _git helper. Standing canonical-script "
                          "exception, the nevertrack class: the pre-commit "
                          "guard executes this file as a PLAIN SCRIPT "
                          "(python3 conflict_marker.py --staged) with zero "
                          "helm imports, and a lane may not edit the detector "
                          "that judges its own staged conflict markers.",
    "lane_discipline.py": "6 read-only venue observations (rev-parse "
                          "--git-dir/--git-common-dir for shared-checkout vs "
                          "lane worktree, rev-parse --verify refs/heads/main "
                          "and symbolic-ref --short HEAD for the base branch, "
                          "symbolic-ref --quiet HEAD for the checked-out "
                          "branch, rev-parse --verify HEAD for the root-commit "
                          "exemption, worktree list --porcelain for the estate "
                          "size, rev-parse --show-toplevel) behind ONE _git "
                          "helper. Standing canonical-script exception, the "
                          "nevertrack class: the pre-commit guard executes this "
                          "file as a PLAIN SCRIPT (python3 lane_discipline.py "
                          "--staged) with zero helm imports, and a lane may not "
                          "edit the rung that judges whether its own commit "
                          "belongs in the shared checkout at all.",
    "cell.py": "1 read-only observation (log -1 --all -- <crate>) against a FOREIGN repo — the dregg fork, never helm's own tree — answering ONE question: is the DEPLOYED signer older than the source that builds it. Deliberately not the vcs seam's business: the seam governs the repo helm operates ON, and this reads a dependency's history the way a version probe reads a banner. Fail-open by contract — an unreadable repo returns state 'unknown', which is explicitly NOT 'current', so a failed look can never be mistaken for a clean bill; ordinary migration debt",
    "clearspan.py": "9 read-only git observations (rev-parse --show-toplevel, rev-parse HEAD, show HEAD:path ×2 with bare-filename fallback, log --before, show REV:path ×2 filing-era, merge-base --is-ancestor, cherry) serving ONE purpose: re-measure dispatch row claims against live trunk. Ordinary migration debt",
}

# Modules whose subprocess argv is assembled at runtime. Pinned at MODULE
# granularity, which is the honest strength of a static pass: a module NEW to
# this set trips, but a dynamic argv added inside an already-listed module cannot
# be decoded and this test does not pretend otherwise (see the class docstring).
_DYNAMIC_ARGV_MODULES = {
    "autocompact.py", "cell.py", "chatnode.py", "doctor.py", "fleet.py",
    "handoff.py", "harness.py", "modelrouter.py", "providers.py", "seat.py",
    "seat_health.py", "seat_lifecycle.py", "seat_lifecycle_runtime.py",
    "owedpush.py",
    "seat_proxy.py", "session.py",
    # keepalive.py — CONFIRMED not git, read off every subprocess in the
    # module. `ensure_timer` runs the runtime-discovered systemctl twice
    # (`--user daemon-reload`, `--user enable --now helm-keepalive.timer`) —
    # the identical shape tasksmirror.py and owedpush.py are listed for — and
    # `hand_crontab` runs the runtime-discovered `crontab -l` READ-ONLY, to
    # report a hand-installed cadence it must never edit. Nothing else in the
    # module spawns at all: its own work is filesystem reads, one OAuth
    # refresh over urllib, and an atomic credential write, so there is no
    # version control for a dynamic argv to reach.
    "keepalive.py",
    # suspend.py — CONFIRMED not git, read off every subprocess in the
    # module. `_kick_pass` detaches `python -m helm proxywatch --force` (the
    # module's own proof pass, never a repository operation) and `_post`
    # shells `helm chat post` (the room, not a repo). The module reads two
    # clocks and a state file and bounces proxies through seat_proxy's own
    # primitives, so there is no version control for a dynamic argv to reach.
    "suspend.py",
    # gatewindow.py — CONFIRMED not git, read off every subprocess in the
    # module. All three are the FAB_BINARY constant, which is fab's
    # dispatch CLI and not version control: `fab gate --repo <room>` to
    # dispatch one whole suite, `fab status <host>` to read a node's in-flight
    # runs, and `fab kill <host> <id>` for --supersede. The module's OWN
    # repository questions — the room's head, the trunk head, the project
    # identity and every containment answer — all go through the helm/vcs.py
    # seam, which is why nothing here belongs in _DIRECT_SPAWN_DEBT.
    "gatewindow.py",
    "tasksmirror.py",
    "transcripts.py",
    # upstream_watch.py — CONFIRMED not git, read off every subprocess in the
    # module. `run_claude` runs the runtime-discovered `claude` program with
    # `-p` (the headless model runs of the watcher's propose and refute
    # stages), and `install_timer` runs the runtime-discovered systemctl twice
    # (`--user daemon-reload`, `--user enable --now helm-upstream-watch.timer`),
    # the shape tasksmirror.py is listed for. Its one repository question, the
    # stable checkout the model runs read, goes through work.find_root.
    "upstream_watch.py",
    # owedpush.py — CONFIRMED not git, and confirmed by reading every
    # subprocess in the module rather than by its name. `_ensure_timer` is its
    # ONLY one: two runtime-discovered systemctl argvs, `--user daemon-reload`
    # and `--user enable --now helm-owed-push.timer`, which is the identical
    # shape tasksmirror.py is listed for directly below. The module's own work
    # touches no repository at all — it reads the obligation ledger and posts
    # chat DMs — so there is no version control for a dynamic argv to reach.
    # tasksmirror.py — CONFIRMED not git. `ensure_timer` invokes the
    # runtime-discovered systemctl to install and enable the mirror's user
    # timer, the same seam autocompact and beacons already use. No version
    # control is involved; the module reads harness JSON and appends to the
    # ledger, and its only subprocess is the cadence installer.
    # seat_health.py — CONFIRMED not git. Doctor invokes the runtime-discovered
    # CLIProxyAPI binary only for its version banner.
    # seat_lifecycle.py — CONFIRMED not git. Rebind timer installation invokes
    # runtime-discovered systemctl; rebind commands target the selected harness
    # adapter. seat_lifecycle_runtime.py owns headless launch spawning through a
    # runtime-assembled argv. The pinned dynamic resume/spawn argv remain in
    # seat.py.
    # seat_proxy.py — CONFIRMED not git. Smoke invokes claude with dynamically
    # selected model/tool arguments; proxy up invokes the runtime-discovered
    # CLIProxyAPI binary. Neither command is version control.
    # gatechild.py — CONFIRMED not git. It supervises the already-decided
    # whole-suite argv so launcher death can kill the complete process group;
    # the command comes from gate.py and is executed verbatim.
    "gatechild.py",
    # relevance.py — CONFIRMED not git, by reading its one spawn: `_spawn`
    # detaches `<the running interpreter> -m helm relevance score-turn`, the
    # re-rank's per-turn worker. The head is sys.executable, which is why the
    # static pass cannot decode it; the worker talks HTTP to a scorer and
    # writes helm state, and touches no repository.
    "relevance.py",
    # relevanced.py — CONFIRMED not git, by reading its one spawn:
    # `start_llama` runs the operator-configured llama.cpp embedding server
    # binary with model and port arguments. The binary path comes from argv,
    # so the static pass cannot decode it; nothing it runs is version control.
    "relevanced.py",
    # proxywatch.py — CONFIRMED not git. Two systemctl spawns, both in
    # ensure_timer: `daemon-reload` and `enable --now helm-proxywatch.timer`.
    # The argv is assembled from a which()-resolved binary plus literals, which
    # is why the static pass cannot decode it. Named here rather than routed
    # through the vcs seam because the seam is for GIT, and a systemctl call is
    # not a version-control operation wearing a different hat.
    "proxywatch.py",
    # stalebot.py — CONFIRMED not git. ONE spawn site: `ensure_timer` invokes
    # the runtime-discovered systemctl (daemon-reload, enable --now) to
    # install the stale-bot's daily user timer — the identical seam
    # tasksmirror and proxywatch already declare. Every git question the
    # module asks (landing proof, carrier patch-id scan) routes through
    # helm/vcs.py's backend, including the patch-id pipe via stdin=.
    "stalebot.py",
    # codexhomes.py — CONFIRMED not git, by reading _orca_cli_codex_state.
    # ONE spawn: `<orca> account list`, sync-orca's CLI fallback for a daemon
    # whose accounts.list RPC fails (2026-08-04). The argv head is dynamic —
    # the HELM_ORCA_CLI pin when set, else the OrcaAdapter's PATH-discovered
    # `orca` — plus two literals; a read-only roster query, no repo touched.
    "codexhomes.py",
    # storage_matrix.py — CONFIRMED not git by reading its one remaining spawn
    # boundary: `_remote_probe` runs `ssh -T` with fixed safety options, a
    # validated inventory host and the same bounded Python probe used locally.
    # The command never names or reaches a repository; its dynamic host is why
    # this static Git-spawn audit cannot decide it from literals alone. (The
    # PATH-discovered inventory read that used to live here as `_fab_inventory`
    # moved to boxes.py with the #224 provider chain — see that entry.)
    "storage_matrix.py",
    # boxes.py — CONFIRMED not git by reading its one spawn:
    # `ExternalInventoryProvider.boxes` runs the which()-resolved optional
    # cross-box inventory CLI with two literal args (`nodes --json`), the read
    # storage_matrix._fab_inventory used to own before the #224 provider-chain
    # extraction. Absent binary = no spawn at all; the argv head is dynamic
    # (env-pinned or PATH-discovered), which is why the static pass cannot
    # decode it. Nothing reachable from that call is git.
    "boxes.py",
    # gateroute.py — CONFIRMED not git by reading its one spawn:
    # `SshTransport.run` executes `ssh -T` with storage_matrix's fixed safety
    # options against a HOST_RE-validated inventory host (#225 job routing);
    # the dynamic host is why the static pass cannot decode it. The GIT work
    # in that module (bundle create, tree reads) already rides the vcs seam
    # and gate.tree_state; the ssh child runs a marker-structured sh script
    # on ANOTHER machine, which no local spawn audit could classify anyway.
    "gateroute.py",
    # gc.py — CONFIRMED not git, and the SAME TWO SPAWNS as proxywatch.py above:
    # `systemctl --user daemon-reload` and `systemctl --user enable --now
    # helm-gc.timer`, both in the ship-its-own-cadence path, argv assembled from
    # a which()-resolved binary plus literals. This audit did its job: the lane
    # that taught gc to install its own timer added the spawn and did not
    # discharge the allowlist, and the FULL SUITE caught it at the land gate
    # after the review had already approved (2026-07-30). Confirming "not git"
    # is the whole obligation this entry represents — it is not a formality,
    # and the day it is added without someone reading the spawn, the audit
    # stops meaning anything.
    "gc.py",
    # beacons.py — CONFIRMED not git, by reading beacons.py:1477 and not by
    # pattern-matching the three entries above. It is the FOURTH module with
    # the identical pair — `systemctl --user daemon-reload` and `enable --now
    # helm-beacons.timer`, in ensure_timer, argv from a which()-resolved binary
    # plus literals — and the static pass cannot decode it only because `cmd`
    # is a LOOP VARIABLE over two literal lists, both of which begin with the
    # resolved systemctl. Nothing reachable from that call is git.
    # The gc.py note above predicted this lane exactly: the row that taught
    # beacons to install its own timer added the spawn and did not discharge
    # the allowlist, and the whole suite caught it at the gate — after the
    # local subset had passed green. Two for two now. If a FIFTH timer-
    # installer lands, the repetition has earned a helper (an `ensure_timer`
    # the modules share) rather than a fifth paragraph saying the same thing.
    "beacons.py",
    # gate.py — CONFIRMED not git, and confirmed by READING the spawns rather
    # than by the module's name sounding harmless. There are exactly two
    # subprocess calls: `gate.run`, whose argv is either
    # `[sys.executable] + SUITE` — helm spawning the test suite as a child of
    # ITSELF, which is the whole mechanism by which the recorded interpreter
    # cannot differ from the running one — or the operator's explicit `-- argv`;
    # and `gate._reference_run`, which is `[sys.executable, "-m", "unittest"]`
    # plus the failing test ids, the stale-base check re-running those tests in
    # a detached trunk/merge-base checkout under the same interpreter. Never
    # git: gate.py's git reads (HEAD, HEAD^{tree}, dirty, trunk, merge-base,
    # diff, ls-files, worktree add/remove) go through vcs.backend(), and this
    # audit is why. Its first draft grew a private `_git` helper, the seventh
    # spelling of one, and failed here.
    "gate.py",
    # gateshard.py — CONFIRMED not git. Its two dynamic argvs launch the current
    # interpreter with this tool-owned absolute script as argv[1], once for the
    # fresh `--plan` discovery process and once per `--worker` shard. Both are
    # Python/unittest sharding infrastructure; neither invokes version control.
    "gateshard.py",
    # resumeturn.py — CONFIRMED not git, and named here rather than hidden
    # behind another module's helper because spawning is this module's central
    # mechanism: the post-compaction resume MUST detach (a SessionStart hook
    # that waits out the composer settle inline blocks the session start it is
    # trying to resume). The one spawn is `<hooks.helm_bin()> seat resume-turn
    # --deliver …` — helm re-entering itself, argv0 computed from this
    # checkout's own bin/helm and never from a caller-supplied string.
    "resumeturn.py",
    # evalrun.py — CONFIRMED not git. The one spawn IS the module's whole
    # point: run_arm execs the seat's own launch.sh with -p (§C pilot
    # FINDING 1 — an arm assembled from a reconstructed env measures the
    # reconstruction). argv0 is the minted launch.sh path under the seats
    # root, resolved through seat._instance_dir, never a caller string.
    "evalrun.py",
    # findingspass.py — CONFIRMED not git. Two spawns, both the module's
    # whole point: `/bin/sh -c 'exec "$@" &' <python> -m helm.findingspass
    # <row id>` (helm re-entering itself, detached through a reaped shell),
    # and `<python> <local-review.py> <tip> --judge ...`, the local review
    # script run in place from HELM_LOCAL_REVIEW_SCRIPT or its default path.
    # The script spawns git itself; helm spawns only the interpreter.
    "findingspass.py",
}

# PINNED so the seam's reach cannot quietly regress. OUTSIDE may only go DOWN
# for migration debt — and when it moves, this number, helm/vcs.py's docstring
# and the count in docs/ARCHITECTURE.md move together. Two UP moves are on
# record: nevertrack.py (+1, 2026-07-29), a STANDING exception whose pre-commit
# guard runs importless as a plain script; and dispatches.py (+1, 2026-08-03),
# the future-write raw-SHA unique-local-tip binding probe. See each module's
# _DIRECT_SPAWN_DEBT reason.
#
# dispatches.py (+1, 2026-08-12), the continuation-ancestor rung: the --new-work
# guard asks whether a live row's tip is an ancestor of the ref being minted, so
# a cure round dispatched as new work is refused before it can root a chain no
# close door can ever reach. It is the SAME `merge-base --is-ancestor` spelling
# already standing in this module, and the module is already debt — this adds a
# call of an accepted shape rather than a new anti-pattern.
_SEAM_SPAWNS = 4                  # vcs.py: run, _capture, proc, git_version
_GIT_SPAWNS_OUTSIDE = 51


class SpawnDetectorTest(unittest.TestCase):
    """The detector's OWN coverage, proved against a battery of spellings.

    This class exists because the first version of this guard was praised as
    "docs that cannot drift" while detecting only 2 of 10 ordinary ways to spell
    a git subprocess. A guard's claim has to be testable, or it is just a more
    convincing kind of overclaim.
    """

    SPELLINGS = {
        "plain": 'import subprocess\nsubprocess.run(["git", "status"])\n',
        "module alias": 'import subprocess as sp\nsp.run(["git", "status"])\n',
        "from-import": 'from subprocess import run\nrun(["git", "status"])\n',
        "from-import alias": 'from subprocess import Popen as P\n'
                             'P(["git", "status"])\n',
        "other spawner": 'import subprocess\n'
                         'subprocess.check_output(["git", "log"])\n',
        "assigned runner": 'import subprocess\nr = subprocess.run\n'
                           'r(["git", "status"])\n',
        "chained alias": 'from subprocess import run\na = run\nb = a\n'
                         'b(["git", "status"])\n',
        "args keyword": 'import subprocess\n'
                        'subprocess.run(args=["git", "status"])\n',
        "concatenated": 'import subprocess\nsubprocess.run(["gi" + "t", "st"])\n',
        "f-string": 'import subprocess\nG = "git"\n'
                    'subprocess.run([f"{G}", "status"])\n',
        "module constant": 'import subprocess\nGIT = "git"\n'
                           'subprocess.run([GIT, "-C", "/x"])\n',
        "function-local constant": 'import subprocess\ndef f():\n'
                                   '    GIT = "git"\n'
                                   '    subprocess.run([GIT, "status"])\n',
        "argv concatenation": 'import subprocess\nGIT = "git"\n'
                              'def f(a):\n'
                              '    subprocess.run([GIT, "-C", "/x"] + list(a))\n',
        "tuple argv": 'import subprocess\nsubprocess.run(("git", "status"))\n',
        "shell string": 'import subprocess\n'
                        'subprocess.getoutput("git status")\n',
        "absolute path": 'import subprocess\n'
                         'subprocess.run(["/usr/bin/git", "status"])\n',
        # the three a cross-family re-gate caught still passing
        # — each is a statically KNOWABLE git execution
        "annotated runner": 'import subprocess\nr: object = subprocess.run\n'
                            'r(["git", "status"])\n',
        "functools.partial": 'import subprocess\nimport functools\n'
                             'r = functools.partial(subprocess.run)\n'
                             'r(["git", "status"])\n',
        "partial from-import": 'from functools import partial\n'
                               'from subprocess import run\n'
                               'r = partial(run)\nr(["git", "status"])\n',
        "aliased partial": 'import subprocess\nimport functools as ft\n'
                           'r = ft.partial(subprocess.run, check=True)\n'
                           'r(["git", "status"])\n',
        "immediate partial call": 'import subprocess\nimport functools\n'
                                  'functools.partial(subprocess.run)'
                                  '(["git", "status"])\n',
        "executable override": 'import subprocess\n'
                               'subprocess.run(["not-git"], '
                               'executable="/usr/bin/git")\n',
        "annotated partial": 'import subprocess\nimport functools\n'
                             'r: object = functools.partial(subprocess.run)\n'
                             'r(["git", "status"])\n',
        # A partial is a BINDING, not merely a callable —
        # recognising it while dropping its bound args was still wrong. The first
        # of these was SILENTLY missed (not even unresolved), because `partial`
        # itself was aliased by assignment.
        "assignment-aliased partial": 'import subprocess\nimport functools\n'
                                      'P = functools.partial\n'
                                      'r = P(subprocess.run)\n'
                                      'r(["git", "status"])\n',
        "bound git executable": 'import subprocess\nfrom functools import partial\n'
                                'r = partial(subprocess.run, '
                                'executable="/usr/bin/git")\n'
                                'r(["not-git"])\n',
        "pre-bound argv": 'import subprocess\nfrom functools import partial\n'
                          'r = partial(subprocess.run, ["git", "status"])\n'
                          'r()\n',
        "nested partial": 'import subprocess\nimport functools\n'
                          'a = functools.partial(subprocess.run)\n'
                          'b = functools.partial(a, ["git", "status"])\n'
                          'b()\n',
        "double-aliased partial": 'import subprocess\nimport functools as ft\n'
                                  'P = ft.partial\nQ = P\n'
                                  'r = Q(subprocess.run)\nr(["git"])\n',
    }

    # Outside the decided grammar, but still syntactically names a spawner. The
    # inversion's promise is REPORTING, not guessing how arbitrary Python data
    # flow evaluates.
    UNRESOLVED_SPELLINGS = {
        "tuple destructure": 'import subprocess\n'
                             '(runner,) = (subprocess.run,)\n'
                             'runner(["git", "status"])\n',
        "walrus binding": 'import subprocess\n'
                          '(runner := subprocess.run)(["git", "status"])\n',
        "getattr binding": 'import subprocess\n'
                           'runner = getattr(subprocess, "run")\n'
                           'runner(["git", "status"])\n',
        # Post-land falsification: each source reference used to disappear because
        # the pass inspected callable bindings/call sites, not references.
        "dict transport": 'import subprocess\n'
                          'spawners = {"run": subprocess.run}\n'
                          'spawners["run"](["git", "status"])\n',
        "class attribute": 'import subprocess\n'
                           'class Runner:\n'
                           '    run = subprocess.run\n'
                           'Runner.run(["git", "status"])\n',
        "nested class attribute": 'import subprocess\n'
                                  'class Runner:\n'
                                  '    if enabled:\n'
                                  '        run = subprocess.run\n',
        "computed getattr": 'import subprocess as sp\n'
                            'runner = getattr(sp, "ru" + "n")\n'
                            'runner(["git", "status"])\n',
        # Novel probes, not examples supplied with the finding: the same ledger
        # must catch capability transport through arbitrary expression contexts.
        "callback argument": 'import subprocess\n'
                             'def consume(callback): pass\n'
                             'consume(subprocess.run)\n',
        "default argument": 'import subprocess\n'
                            'def later(runner=subprocess.run): pass\n',
        "conditional transport": 'import subprocess\n'
                                 'runner = subprocess.run if enabled else fallback\n',
        "dynamic getattr": 'import subprocess as sp\n'
                           'def choose(name):\n'
                           '    return getattr(sp, name)\n',
    }

    def test_every_ordinary_spelling_of_a_git_spawn_is_detected(self):
        missed = [name for name, src in self.SPELLINGS.items()
                  if not scan_spawns(src)[0]]
        self.assertEqual(missed, [], "the decided grammar's claim is only as "
                         "good as its coverage — missed spellings: %r" % missed)

    def test_every_spawner_reference_outside_the_grammar_is_reported(self):
        for name, src in self.UNRESOLVED_SPELLINGS.items():
            git, static, unresolved = scan_spawns(src)
            self.assertEqual(git, [], name)
            self.assertEqual(static, [], name)
            self.assertTrue(unresolved, "%s was silently skipped" % name)

    def test_decided_references_are_consumed_not_double_reported(self):
        cases = {
            "direct": 'import subprocess\nsubprocess.run(["git"])\n',
            "alias": 'import subprocess\nr = subprocess.run\n'
                     'r(["git"])\n',
            "partial": 'import subprocess\nfrom functools import partial\n'
                       'r = partial(subprocess.run, ["git"])\nr()\n',
            "known non-spawner getattr": 'import subprocess\n'
                                         'pipe = getattr(subprocess, "PIPE")\n',
        }
        for name, src in cases.items():
            _git, _static, unresolved = scan_spawns(src)
            self.assertEqual(unresolved, [], name)

    def test_non_git_spawns_are_not_flagged(self):
        for src in ('import subprocess\nsubprocess.run(["cv", "show", "x"])\n',
                    'import subprocess\nsubprocess.run(["gitk"])\n',
                    'import subprocess\nsubprocess.run(["my-git-wrapper"])\n',
                    'import subprocess\nB = "claude"\nsubprocess.run([B, "-p"])\n'):
            git, static, unresolved = scan_spawns(src)
            self.assertEqual(git, [], src)
            self.assertEqual(unresolved, [], src)
            self.assertTrue(static, src)

    def test_a_runtime_argv_is_reported_unresolved_not_silently_passed(self):
        for src in ('import subprocess\ndef f(cmd):\n    subprocess.run(cmd)\n',
                    'import subprocess\ndef f(p):\n'
                    '    subprocess.run([p, "status"])\n',
                    'import subprocess\ndef f(a):\n'
                    '    subprocess.run(["git"] if a else a)\n'):
            git, _static, unresolved = scan_spawns(src)
            self.assertEqual(git, [], src)
            self.assertTrue(unresolved, src)   # unknown is SAID, never assumed

    def test_shell_true_means_executable_names_the_SHELL_not_the_program(self):
        """The nuance a review's probe did not reach, and getting it backwards
        would cost a real detection: under shell=True the `executable` kwarg
        picks the SHELL, while the command still lives in the argv string. If
        the override were trusted here, `run("git status", shell=True,
        executable="/bin/bash")` would be filed as a bash call and the git
        execution would vanish from the sweep."""
        git, _static, unresolved = scan_spawns(
            'import subprocess\nsubprocess.run("git status", shell=True, '
            'executable="/bin/bash")\n')
        self.assertTrue(git, "shell=True must not let executable= hide the "
                             "program named in argv")
        self.assertEqual(unresolved, [])

    def test_an_undecodable_executable_override_is_unresolved_not_cleared(self):
        """A program we could not read is not a program we cleared — the same
        vacuous-pass law the marker probe enforces at the filesystem layer."""
        for src in ('import subprocess\ndef f(exe):\n'
                    '    subprocess.run(["not-git"], executable=exe)\n',
                    'import subprocess\ndef f(d):\n'
                    '    subprocess.run(["x"], executable=d["git"])\n'):
            git, static, unresolved = scan_spawns(src)
            self.assertEqual(git, [], src)
            self.assertEqual(static, [], src)
            self.assertTrue(unresolved, src)

    def test_a_partials_BOUND_executable_is_honoured_in_both_directions(self):
        """The round-2 cell: the bound kwargs travel with the callable.
        Classifying on the final call alone made a bound `executable=` invisible,
        which was wrong BOTH ways — a bound git executable read as non-git, and a
        bound hg executable with an argv of git read as git."""
        git, _s, unresolved = scan_spawns(
            'import subprocess\nfrom functools import partial\n'
            'r = partial(subprocess.run, executable="/usr/bin/hg")\n'
            'r(["git", "status"])\n')
        self.assertEqual(git, [], "a bound executable pointing AWAY from git "
                                  "must not report a git site")
        self.assertEqual(unresolved, [])

    def test_bound_positional_executable_uses_popen_semantics(self):
        cases = (
            ('["not-git"], -1, "/usr/bin/git"', True),
            ('["git"], -1, "/usr/bin/hg"', False),
        )
        for bound, expected_git in cases:
            src = ('import subprocess\nfrom functools import partial\n'
                   'r = partial(subprocess.run, %s)\nr()\n' % bound)
            git, static, unresolved = scan_spawns(src)
            self.assertEqual(bool(git), expected_git, src)
            self.assertTrue(static, src)
            self.assertEqual(unresolved, [], src)

    def test_nonliteral_bound_shell_is_unresolved_not_false(self):
        src = ('import subprocess\nfrom functools import partial\n'
               'def f(s):\n'
               '    r = partial(subprocess.run, shell=s, '
               'executable="/bin/bash")\n'
               '    r("git status")\n')
        git, static, unresolved = scan_spawns(src)
        self.assertEqual(git, [])
        self.assertEqual(static, [])
        self.assertTrue(unresolved)

    def test_a_call_site_keyword_overrides_the_partials_bound_one(self):
        """functools semantics, not a guess: keywords supplied at the call win."""
        git, _s, _u = scan_spawns(
            'import subprocess\nfrom functools import partial\n'
            'r = partial(subprocess.run, executable="/usr/bin/hg")\n'
            'r(["not-git"], executable="/usr/bin/git")\n')
        self.assertTrue(git, "the call site's executable must override the "
                             "partial's bound one")

    def test_an_undecodable_BOUND_value_is_unresolved_not_cleared(self):
        """The vacuous-pass law reaches bound arguments too."""
        for src in ('import subprocess\nfrom functools import partial\n'
                    'def f(exe):\n'
                    '    r = partial(subprocess.run, executable=exe)\n'
                    '    r(["not-git"])\n',
                    'import subprocess\nfrom functools import partial\n'
                    'def f(cmd):\n'
                    '    r = partial(subprocess.run, cmd)\n'
                    '    r()\n'):
            git, static, unresolved = scan_spawns(src)
            self.assertEqual(git, [], src)
            self.assertEqual(static, [], src)
            self.assertTrue(unresolved, src)

    def test_an_override_AWAY_from_git_is_honestly_not_a_git_site(self):
        """Symmetry check, so the override handling is not just biased toward
        finding git: argv says git but `executable` says otherwise, and what
        actually runs is what `executable` names."""
        git, static, unresolved = scan_spawns(
            'import subprocess\n'
            'subprocess.run(["git", "status"], executable="/usr/bin/hg")\n')
        self.assertEqual(git, [])
        self.assertEqual(unresolved, [])
        self.assertTrue(static)

    def test_a_name_bound_to_two_literals_is_ambiguous_not_guessed(self):
        src = ('import subprocess\nG = "git"\nG = "hg"\n'
               'subprocess.run([G, "status"])\n')
        git, _static, unresolved = scan_spawns(src)
        self.assertEqual(git, [])
        self.assertTrue(unresolved)

    def test_the_seams_own_spawns_are_detected_by_this_sweep(self):
        # if the sweep cannot see the seam's own three, it cannot see a copycat
        git, _static, _unresolved = _sweep_helm()
        self.assertEqual(len([s for s in git if s[0] == "vcs.py"]), _SEAM_SPAWNS)


class DirectSpawnAuditTest(unittest.TestCase):
    """The seam's real reach, pinned.

    WHAT THIS GUARANTEES: every spawn call decided by the explicit grammar is
    classified, and every unconsumed reference on the named accounting surface
    (subprocess spawner attributes, known aliases, subprocess ``getattr``) is
    unresolved. A new Git site fails until routed or reason-allowlisted; a new
    unresolved module fails until investigated and named. WHAT IT DOES NOT:
    discover capabilities obtained through arbitrary introspection, re-export or
    Python data flow. The bound is explicit rather than advertised as total.
    """

    def test_web_spawn_ownership_survives_physical_motion(self):  # noqa: VACUOUS_ASSERTION — both physical web modules assert the exact semantic git site, then a non-web module is the unconditional contrasting control
        source = ('import subprocess\n'
                  'def _roster_git_one():\n'
                  '    subprocess.run(["git", "log"])\n')
        for module in ("web.py", "web_roster.py"):
            git, _static, unresolved = scan_spawns(source, module)
            self.assertEqual(git, [("web:_roster_git_one", 3)])
            self.assertEqual(unresolved, [])
        git, _static, _unresolved = scan_spawns(source, "other.py")
        self.assertEqual(git, [("other.py", 3)])

    def test_patch_proof_uses_fresh_scrubbed_seam_reads_and_bound_identity(self):
        from unittest import mock
        from helm import dispatches, rowworld
        import subprocess

        checkout, bound = "/synthetic/checkout", "/synthetic/owner/.git"
        reviewed, patch = "a" * 40, "b" * 40
        commands = [
            ["git", "-C", checkout, "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            ["git", "-C", bound, "rev-parse", "--is-shallow-repository"],
            ["git", "-C", bound, "rev-parse", "--verify", "--end-of-options",
             patch + "^{commit}"],
            ["git", "-C", bound, "merge-base", "--is-ancestor", reviewed, patch]]
        healthy = [(0, bound), (0, "false"), (0, patch), (0, "")]
        cases = [(healthy, None),
                 ([(1, "")], "identity could not be measured"),
                 ([(0, "/synthetic/foreign/.git")], "cross-repository proof"),
                 (healthy[:1] + [(0, "true")], "history is shallow"),
                 (healthy[:2] + [(0, reviewed)], "does not resolve"),
                 (healthy[:3] + [(1, "")], "does not descend"),
                 (healthy[:3] + [(-1, "")], "could not be measured")]
        be = vcs.GitVcs()
        ambient = {key: "/synthetic/hostile" for key in dispatches._GIT_SELECTION_ENV}
        ambient.update(PATH=os.defpath, HOME="/synthetic/home")
        for answers, refusal in cases:
            with self.subTest(refusal=refusal), \
                    mock.patch.dict(os.environ, ambient, clear=True), \
                    mock.patch.object(dispatches.os.path, "isdir", return_value=True), \
                    mock.patch("helm.vcs.backend", return_value=be), \
                    mock.patch.object(be, "common_dir") as cached, \
                    mock.patch.object(subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], rc, out.encode(), b"")
                        for rc, out in answers]) as spawn:
                why = dispatches._patch_tip_ancestry(
                    {"repo_root": checkout, "repo_id": bound}, reviewed, patch)
                if refusal is None:
                    self.assertIsNone(why)
                else:
                    self.assertIn(refusal, why)
                cached.assert_not_called()
                self.assertEqual([call.args[0] for call in spawn.call_args_list],
                                 commands[:len(answers)])
                for call in spawn.call_args_list:
                    self.assertEqual(call.kwargs["timeout"], 10)
                    child = call.kwargs["env"]
                    self.assertTrue(set(dispatches._GIT_SELECTION_ENV).isdisjoint(child))
                    self.assertEqual(child["HOME"], ambient["HOME"])
                    for key, value in rowworld._history_view_env().items():
                        self.assertEqual(child[key], value)

    def test_every_direct_git_spawn_is_declared_with_a_reason(self):
        git, _static, _unresolved = _sweep_helm()
        modules = {fn for fn, _ln in git}
        undeclared = sorted(modules - set(_DIRECT_SPAWN_DEBT))
        self.assertEqual(undeclared, [], "a NEW direct git spawn appeared "
                         "outside the seam — route it through helm/vcs.py, or "
                         "add it to _DIRECT_SPAWN_DEBT with a reason: %r"
                         % undeclared)
        stale = sorted(set(_DIRECT_SPAWN_DEBT) - modules)
        self.assertEqual(stale, [], "these modules no longer spawn git "
                         "directly — drop them from _DIRECT_SPAWN_DEBT and "
                         "narrow the docs: %r" % stale)
        for module, reason in _DIRECT_SPAWN_DEBT.items():
            self.assertGreater(len(reason), 20, module)

    def test_the_counts_are_pinned_and_match_the_docs(self):
        git, _static, unresolved = _sweep_helm()
        outside = [s for s in git if s[0] != "vcs.py"]
        self.assertEqual(len(git) - len(outside), _SEAM_SPAWNS)
        self.assertEqual(len(outside), _GIT_SPAWNS_OUTSIDE,
                         "direct git spawns outside the seam moved (%d -> %d). "
                         "If DOWN, re-pin here AND update the count in "
                         "docs/ARCHITECTURE.md + helm/vcs.py's docstring."
                         % (_GIT_SPAWNS_OUTSIDE, len(outside)))
        new_dynamic = sorted({fn for fn, _ln in unresolved}
                             - _DYNAMIC_ARGV_MODULES - {"vcs.py"})
        self.assertEqual(new_dynamic, [], "a module started assembling a "
                         "subprocess argv at runtime — confirm it is not git, "
                         "then add it to _DYNAMIC_ARGV_MODULES: %r"
                         % new_dynamic)
        # the number the docs state must BE the measured number (the doc wraps
        # and bolds, so compare across whitespace/emphasis)
        import re
        arch = os.path.join(os.path.dirname(_PKG), "docs", "ARCHITECTURE.md")
        with open(arch, encoding="utf-8") as f:
            text = re.sub(r"[\s*]+", " ", f.read())
        self.assertIn("%d direct git spawns" % len(outside), text)

    def test_the_seam_never_claims_to_be_total(self):
        # the truth-failure class: a doc advertising completeness we do not have
        # is the same bug as code that lies.
        docs = os.path.join(os.path.dirname(_PKG), "docs")
        for name in ("ARCHITECTURE.md", "ENVIRONMENT.md"):
            with open(os.path.join(docs, name), encoding="utf-8") as f:
                text = f.read()
            for overclaim in ("every helm git call goes through",
                              "ONE place every VCS call",
                              "every VCS call goes through",
                              "cannot drift"):
                self.assertNotIn(overclaim, text, "%s: %r" % (name, overclaim))
        with open(os.path.join(_PKG, "vcs.py"), encoding="utf-8") as f:
            head = f.read()[:4000]
        self.assertIn("NOT yet every git call", head)


class _Capture:
    """A minimal stderr stand-in (the suite never writes to the real one)."""

    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# behavior preservation — the migrated helpers still answer as they did
# ---------------------------------------------------------------------------

class MigratedCallSiteTest(VcsBase):
    def test_handoff_probe_keeps_clean_status_as_empty_string(self):
        self.assertEqual(handoff._git(self.root, "status", "--porcelain"), "")
        self.assertEqual(handoff._git(self.root, "rev-parse", "--abbrev-ref",
                                      "HEAD"), "main")
        self.assertEqual(handoff._git(self.root, "rev-parse",
                                      "--show-toplevel"), self.root)
        self.assertIsNone(handoff._git(self.tmp, "rev-parse",
                                       "--show-toplevel"))
        with open(os.path.join(self.root, "dirt"), "w") as f:
            f.write("x")
        self.assertIn("dirt", handoff._git(self.root, "status", "--porcelain"))

    def test_capsule_era_read_goes_through_the_seam(self):
        between = datetime.fromtimestamp(
            (T1 + T2) // 2).strftime("%Y-%m-%d %H:%M:%S")
        row = {"i": "a" * 36, "h": "claude", "t": "era", "cwd": self.root,
               "mt": (T1 + T2) // 2, "b": "main"}
        with mock.patch.object(capsule.transcripts, "_resolve_sid",
                               return_value=(row, None)):
            out = _stdout(lambda: capsule.cmd_capsule([row["i"][:8]]))
        self.assertIn(self.shas[0][:12], out)
        self.assertEqual(vcs.backend().head_sha(self.root, ref="main",
                                                before=between), self.shas[0])

    def test_ship_git_still_returns_a_completedprocess(self):
        p = ship._git(self.root, "rev-parse", "--short", "HEAD")
        self.assertIsInstance(p, subprocess.CompletedProcess)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), self.shas[1][:len(p.stdout.strip())])
        self.assertEqual(ship._branch(self.root), "main")

    def test_work_git_shapes_are_unchanged(self):
        self.assertEqual(work._git_bytes(self.root, "status", "--porcelain"),
                         (0, b"", b""))
        self.assertEqual(work._git(self.root, "rev-parse", "--abbrev-ref",
                                   "HEAD"), (0, "main", ""))
        rows, error = work._worktree_records(self.root)
        self.assertIsNone(error)
        self.assertEqual([r["branch"] for r in rows], ["refs/heads/main"])
        # the suite's injection point still reaches the porcelain parser
        with mock.patch.object(work._lanes, "_git_bytes",
                               return_value=(1, b"", b"boom")):
            self.assertEqual(work._worktree_records(self.root), ([], "boom"))
        self.assertEqual(work._base(self.root), "main")
        self.assertFalse(work._dirty(self.root))
        self.assertTrue(work._has_branch(self.root, "main"))


def _stdout(fn):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()

class VerbatimMapTest(VcsBase):
    """The whole-history patch-id map, and the three ways it was wrong.

    It exists because `_range_is_verbatim_identical` walked `base..trunk` PER
    ROW — 364 calls over 248 distinct ranges in one live projection, all ending
    at the same trunk sha, 89 of 162 profiled seconds. The ranges are nested
    subsets of ONE history, so one walk answers all of them.

    Every arm here was a PROBE first. A probe convinces its author and vanishes
    with them; these are the same measurements written down so the next reader
    inherits them.
    """

    def _map(self):
        return self.git._verbatim_map(self.root, "main")

    def test_a_repo_local_pretty_format_cannot_rewrite_the_grammar(self):  # noqa: VACUOUS_ASSERTION — the equality could pass on two empty maps, so assertTrue(clean, "no map to compare") is the unconditional positive control on the SAME observable, taken BEFORE the hostile config is written
        """`patch-id` recovers the commit from the header `log` prints, so the
        grammar this map parses is chosen by log's PRETTY FORMAT — and
        `format.pretty` is repository-local config. Unpinned, every patch in
        the walk is attributed to whatever that format names, so an OLD
        pre-base patch is imported as the trunk tip and `landed_state` answers
        PATCH_EQUIVALENT for work that never landed. That verdict authorises a
        branch DELETE, which is the one direction this may not be wrong in."""
        clean = self._map()
        self.assertTrue(clean, "no map to compare")          # positive control
        head = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        self.assertEqual(_sh(self.root, "git", "config", "--local",
                             "format.pretty",
                             "format:commit %s" % head).returncode, 0)
        self.assertEqual(self._map(), clean,
                         "repository config rewrote the map's grammar")

    def test_out_of_scope_callers_never_build_the_whole_history_map(self):
        """`landed_state` is GENERIC: work GC, gate and the dispatch sweep call
        it outside any projection, one branch at a time. The map only pays for
        itself when REUSED, so for those callers it is a straight regression —
        measured at 44.6MB/8.54s of full history against 29KB/0.78s for the
        bounded `base..ref` proof they used to do."""
        built = []
        real = self.git._verbatim_map_uncached
        self.git._verbatim_map_uncached = (
            lambda root, ref, timeout=60: built.append(ref) or real(root, ref, timeout))
        tip = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        self.git._range_is_verbatim_identical(self.root, "main", tip)
        self.assertEqual(built, [], "an out-of-scope caller built the map")
        # UNCONDITIONAL POSITIVE CONTROL: inside a scope it IS built, so the
        # empty list above measures the gate and not a dead code path.
        from helm import projscope
        with projscope.scope():
            self.git._range_is_verbatim_identical(self.root, "main", tip)
        self.assertTrue(built, "the map is never built, even in a scope")
        self.git._verbatim_map_uncached = real

    def test_the_parsed_map_is_memoised_and_so_are_its_failures(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(x/y) is the failure-memo observable and its unconditional positive control is the SUCCESS block above it, where the same call path returns a real map and assertIs(a, b) proves a non-None value is memoised through it; len(calls) pins the walk count in both directions
        """Only raw subprocess bytes were cached one layer down, which is not
        enough in either outcome. On SUCCESS the 2,424-entry map was reparsed
        per row. On FAILURE it was strictly worse than no shortcut: `run`
        evicts rc=-1, so a 30s whole-history TIMEOUT was retried for EVERY row.

        THIS DOCSTRING USED TO CALL FOUR SUCH ROWS A LIVELOCK against a 120s
        hard TTL. Trunk raised `_LR_HARD_TTL_S` to 600 and the rebase took that
        value, so the deadline argument is DEAD — 20 retried walks now fit
        where 4 did not. What this arm still pins is the property that does not
        depend on any budget: a retried whole-history walk is a straight
        regression against the bounded per-range walk it replaced, so a failure
        must be paid ONCE per scope."""
        from helm import projscope
        calls = []
        real = self.git._verbatim_map_uncached
        self.git._verbatim_map_uncached = (
            lambda root, ref, timeout=60: calls.append(ref) or {"sha": "pid"})
        with projscope.scope():
            a = self.git._verbatim_map(self.root, "main")
            b = self.git._verbatim_map(self.root, "main")
        self.assertEqual(len(calls), 1, "the parse was repeated per ask")
        self.assertIs(a, b)

        calls[:] = []
        self.git._verbatim_map_uncached = (
            lambda root, ref, timeout=60: calls.append(ref) or None)
        with projscope.scope():
            x = self.git._verbatim_map(self.root, "main")
            y = self.git._verbatim_map(self.root, "main")
        self.assertEqual(len(calls), 1,
                         "a FAILED walk was retried, which is worse than "
                         "having no shortcut at all")
        self.assertIsNone(x)
        self.assertIsNone(y)

        # CONTROL: the memo discriminates rather than serving one answer to
        # every question — the failure a too-loosely-keyed cache introduces.
        calls[:] = []
        self.git._verbatim_map_uncached = (
            lambda root, ref, timeout=60: calls.append(ref) or {"k": ref})
        with projscope.scope():
            p = self.git._verbatim_map(self.root, "main")
            q = self.git._verbatim_map(self.root, "HEAD~1")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(p, q)
        self.git._verbatim_map_uncached = real

class CommonDirMemoTest(VcsBase):
    """The memo is only safe if it DISCRIMINATES and if it never caches an
    unreadable. Both are asserted against inputs the arm must REJECT — a
    collapsed key, and a path that is not a repository — because an arm that
    cannot name what it would refuse is measuring its author's intent.

    WHY THE OBVIOUS MUTATION IS VACUOUS HERE, recorded so nobody repeats it:
    running the whole of tests/test_lr_close with every key collapsed to one
    constant leaves 288 tests GREEN. That module resolves exactly ONE distinct
    root, so collapsing is behaviourally identical to the real key and the
    mutation CANNOT fail. Discrimination needs TWO repos in one process, which
    is what this arm builds."""

    def _second_repo(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.assertEqual(_sh(other, "git", "init", "-q", "-b", "main").returncode, 0)
        return other

    def test_two_repos_get_two_answers_and_two_cache_entries(self):
        vcs._COMMON_DIR.clear()
        other = self._second_repo()
        a = self.git.common_dir(self.root)
        b = self.git.common_dir(other)
        self.assertTrue(a and b)
        self.assertNotEqual(a, b, "a memo that returns one repo's git dir for "
                                  "another is worse than no memo")
        self.assertEqual(len(vcs._COMMON_DIR), 2)
        self.assertEqual(self.git.common_dir(self.root), a, "re-ask must be stable")
        self.assertEqual(self.git.common_dir(other), b)

    def test_a_collapsed_key_is_caught(self):  # noqa: VACUOUS_ASSERTION — the mutant COLLAPSING to one answer IS the contract; the unmutated discrimination control precedes it on the same two repos
        """THE MUST-MISS. With the key collapsed the memo answers the FIRST
        repo for every path; if this arm ever passes, the discrimination arm
        above has stopped testing anything."""
        vcs._COMMON_DIR.clear()
        other = self._second_repo()
        # POSITIVE CONTROL, same observable, same two repos: unmutated, the
        # two paths give DIFFERENT answers. Only then does the mutant giving
        # one answer mean anything.
        self.assertNotEqual(self.git.common_dir(self.root),
                            self.git.common_dir(other))
        vcs._COMMON_DIR.clear()
        real = type(self.git).common_dir

        def collapsed(inner, path, timeout=10):
            hit = vcs._COMMON_DIR.get(("K",), vcs._MISS)
            if hit is not vcs._MISS:
                return hit
            got = inner.probe(path, "rev-parse", "--path-format=absolute",
                              "--git-common-dir", timeout=timeout)
            if got is not None:
                vcs._COMMON_DIR[("K",)] = got
            return got

        type(self.git).common_dir = collapsed
        try:
            a = self.git.common_dir(self.root)
            b = self.git.common_dir(other)
            self.assertEqual(a, b, "the mutant must collapse both to one "
                                   "answer, or this arm proves nothing")
        finally:
            type(self.git).common_dir = real
        vcs._COMMON_DIR.clear()

    def test_an_unreadable_probe_is_never_cached(self):  # noqa: VACUOUS_ASSERTION — a non-repo HAVING no common dir is the product law; the cache-count control above it proves the cache is live, and by the rung's own provenance rule a second common_dir() call cannot share the first's observable
        """None folds nonzero-exit, missing-git and timeout together, so it
        means UNREADABLE. Caching it would make a transient failure permanent
        and indistinguishable from a measured answer."""
        vcs._COMMON_DIR.clear()
        # POSITIVE CONTROL FIRST: a readable path DOES enter the cache, so a
        # later count of 1 means "the unreadable stayed out", not "the cache
        # never worked". Without this the assertion below passes on a memo
        # that caches nothing at all.
        self.assertIsNotNone(self.git.common_dir(self.root))
        self.assertEqual(len(vcs._COMMON_DIR), 1)
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertIsNone(self.git.common_dir(plain))
        self.assertEqual(len(vcs._COMMON_DIR), 1,
                         "an unreadable answer must not enter the cache")

    def test_eviction_is_production_eviction_not_the_arms_own(self):
        """Drives common_dir ITSELF across cap+1 paths with a stubbed probe.

        THE ARM THIS REPLACES WAS VACUOUS, a review catch: it inserted
        into the dict and popped from it BY HAND, so it would have passed with
        production's eviction DELETED. The only way to test eviction is to make
        production do it and then observe a RE-PROBE — the evicted key must go
        back to git, the surviving key must not."""
        vcs._COMMON_DIR.clear()
        real_cap = vcs._COMMON_DIR_CAP
        self.addCleanup(setattr, vcs, "_COMMON_DIR_CAP", real_cap)
        self.addCleanup(vcs._COMMON_DIR.clear)
        vcs._COMMON_DIR_CAP = 3

        seen = []
        real_probe = type(self.git).probe

        def stub(inner, cwd, *args, **kw):
            seen.append(cwd)
            return "/fake%s/.git" % cwd
        type(self.git).probe = stub
        self.addCleanup(setattr, type(self.git), "probe", real_probe)

        paths = ["/p/%d" % i for i in range(4)]
        for path in paths:
            self.git.common_dir(path)
        self.assertEqual(len(seen), 4, "each new path must reach git once")
        self.assertEqual(len(vcs._COMMON_DIR), 3, "the cap must bind")

        # the NEWEST is a hit: no new probe
        before = len(seen)
        self.assertEqual(self.git.common_dir(paths[3]), "/fake/p/3/.git")
        self.assertEqual(len(seen), before, "a cached key must not re-probe")

        # the OLDEST was evicted by PRODUCTION: it must go back to git
        self.assertEqual(self.git.common_dir(paths[0]), "/fake/p/0/.git")
        self.assertEqual(len(seen), before + 1,
                         "the evicted key must re-probe — if this does not "
                         "grow, production eviction never ran")

    def test_a_hit_survives_a_concurrent_eviction_of_its_own_key(self):
        """THE EXACT INTERLEAVING review drove: thread A takes a hit on key K,
        thread B evicts K, A's move_to_end(K) raises KeyError. helm web is a
        ThreadingHTTPServer so this is a production path.

        A FIRST ATTEMPT AT THIS ARM WAS VACUOUS and the why is recorded: four
        threads hammering common_dir with a stubbed probe PASSED with the lock
        REMOVED, because a stubbed probe has no yield point and the GIL never
        interleaves inside the critical section. A stress loop cannot find this
        race; the window has to be FORCED. This arm forces it by blocking
        inside the dict's own get(), so the eviction lands exactly between A's
        get and A's move_to_end.

        VERIFIED TO DISCRIMINATE: with _COMMON_DIR_LOCK replaced by a no-op,
        this arm raises KeyError from move_to_end."""
        real_cap = vcs._COMMON_DIR_CAP
        real_dir = vcs._COMMON_DIR
        self.addCleanup(setattr, vcs, "_COMMON_DIR_CAP", real_cap)
        self.addCleanup(setattr, vcs, "_COMMON_DIR", real_dir)
        vcs._COMMON_DIR_CAP = 1

        opened = threading.Event()
        evicted = threading.Event()

        class Hooked(collections.OrderedDict):
            armed = True

            def get(self, key, default=None):
                got = super().get(key, default)
                if self.armed and got is not default:
                    self.armed = False        # only the first hit is hooked
                    opened.set()              # let the evictor run
                    # THE WAIT IS THE MECHANISM, NOT A SLEEP. Without the
                    # lock B completes its eviction inside this window and the
                    # move_to_end below raises. WITH the lock B blocks, this
                    # wait expires, and that expiry IS the lock working — so
                    # the arm costs one second exactly when it passes.
                    evicted.wait(timeout=1.0)
                return got

        hooked = Hooked()
        vcs._COMMON_DIR = hooked
        real_probe = type(self.git).probe
        type(self.git).probe = lambda inner, cwd, *a, **kw: "/fake%s/.git" % cwd
        self.addCleanup(setattr, type(self.git), "probe", real_probe)

        self.git.common_dir("/p/A")           # seed the key A will hit
        hooked.armed = True

        def evictor():
            opened.wait(timeout=10)
            self.git.common_dir("/p/B")       # cap is 1 -> evicts A's key
            evicted.set()

        tb = threading.Thread(target=evictor)
        tb.start()
        self.addCleanup(tb.join, 20)
        # THE HIT RUNS ON THIS THREAD so its result is a direct binding rather
        # than a spy list: an unraised KeyError errors the test, and the value
        # itself is the positive control on the same observable.
        hit = self.git.common_dir("/p/A")
        evicted.set()
        tb.join(timeout=20)
        self.assertEqual(hit, "/fake/p/A/.git",
                         "the hit must come back CORRECT, not merely quietly — "
                         "a reader that never ran also raises nothing")


class AmbientGitBudgetTest(unittest.TestCase):
    def setUp(self):
        self.git = vcs.GitVcs()

    def test_an_expired_nested_scope_refuses_even_a_warm_memo_hit(self):  # noqa: VACUOUS_ASSERTION — the first successful call and one spawn are positive controls; the nested call must refuse before memo lookup
        answer = subprocess.CompletedProcess([], 0, b"tip\n", b"")
        with mock.patch.object(vcs.subprocess, "run", return_value=answer) as spawn, \
                projscope.scope():
            self.assertEqual(self.git.run("/repo", "rev-parse", "HEAD")[0], 0)
            with projscope.scope(deadline=0):
                with self.assertRaises(projscope.Expired):
                    self.git.run("/repo", "rev-parse", "HEAD")
        self.assertEqual(spawn.call_count, 1,
                         "the refusal must happen before the warm memo lookup")

    def test_the_child_timeout_is_clamped_to_ambient_remaining(self):  # noqa: VACUOUS_ASSERTION — one captured positive timeout proves the subprocess ran; its value pins the ambient clamp
        seen = []

        def answer(*_args, **kw):
            seen.append(kw["timeout"])
            return subprocess.CompletedProcess([], 0, b"ok", b"")

        with mock.patch.object(vcs.subprocess, "run", side_effect=answer), \
                projscope.scope(deadline=time.monotonic() + 5):
            self.assertEqual(self.git.run("/repo", "status", timeout=30)[0], 0)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)
        self.assertLessEqual(seen[0], 5)

    def test_global_config_options_do_not_hide_an_observation(self):
        self.assertTrue(vcs._observation_argv(
            ("-c", "diff.noprefix=false", "log", "-p")))
        self.assertFalse(vcs._observation_argv(
            ("-c", "user.name=helm", "commit", "-m", "x")))

    def test_a_shorter_caller_timeout_keeps_the_existing_unknown_result(self):  # noqa: VACUOUS_ASSERTION — TimeoutExpired is the positive control; rc -1 proves the shorter caller timeout keeps legacy UNKNOWN semantics
        with mock.patch.object(
                vcs.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("git", 0.01)), \
                projscope.scope(deadline=time.monotonic() + 5):
            self.assertEqual(self.git.run(
                "/repo", "status", timeout=0.01)[0], -1)

    def test_an_ambient_limited_timeout_raises_expired(self):  # noqa: VACUOUS_ASSERTION — TimeoutExpired is the positive control; Expired proves an ambient-clamped child timeout propagates incomplete coverage
        with mock.patch.object(
                vcs.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("git", 5)), \
                projscope.scope(deadline=time.monotonic() + 5):
            with self.assertRaises(projscope.Expired):
                self.git.run("/repo", "status", timeout=30)

    def test_ls_remote_is_observed_but_never_projection_memoised(self):  # noqa: VACUOUS_ASSERTION — two distinct nonempty answers and spawn count two prove each remote observation executed
        answers = (
            subprocess.CompletedProcess([], 0, b"first\n", b""),
            subprocess.CompletedProcess([], 0, b"second\n", b""),
        )
        with mock.patch.object(vcs.subprocess, "run", side_effect=answers) as spawn, \
                projscope.scope():
            first = self.git.run("/repo", "ls-remote", "origin")
            second = self.git.run("/repo", "ls-remote", "origin")
        self.assertEqual(spawn.call_count, 2,
                         "a remote answer can change outside this process")
        self.assertNotEqual(first, second)
        self.assertTrue(vcs._observation_argv(("ls-remote", "origin")))

    def test_ls_remote_inherits_the_ambient_timeout(self):  # noqa: VACUOUS_ASSERTION — captured timeout proves the remote observation spawned before Expired propagated
        seen = []

        def timeout(*_args, **kwargs):
            seen.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired("git", kwargs["timeout"])

        with mock.patch.object(vcs.subprocess, "run", side_effect=timeout), \
                projscope.scope(deadline=time.monotonic() + 1):
            with self.assertRaises(projscope.Expired):
                self.git.run("/repo", "ls-remote", "origin", "refs/heads/main",
                             timeout=20)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)
        self.assertLessEqual(seen[0], 1)

        with mock.patch.object(
                vcs.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("git", 0.01)), \
                projscope.scope(deadline=time.monotonic() + 1):
            self.assertEqual(self.git.run(
                "/repo", "ls-remote", "origin", "refs/heads/main",
                timeout=0.01)[0], -1)


if __name__ == "__main__":
    unittest.main()
