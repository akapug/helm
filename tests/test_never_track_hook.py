#!/usr/bin/env python3
"""The never-track pre-commit guard, END TO END — the enforcement-timing leg.

THE GAP THIS PROVES CLOSED: a leak entered history with a green
suite because the fixture was untracked when pytest ran, and `git add` +
`git commit` followed with no scan in between. tests/test_never_track.py pins
WHY a suite-side fix cannot help (ls-files already reads the index; the
proposed staged-set union was measured to be a no-op). This file proves the
fix that can: `helm work install-guard --apply` composes a pre-commit hook
that runs helm/nevertrack.py against the STAGED set inside `git commit`, so
the add->commit window is guarded by machinery, not memory.

Every test drives the REAL mechanism — the `helm work` CLI dispatcher, the
real installed hook file, real `git commit` subprocesses — in a scratch repo.
Hermetic: git global/system config nulled, needles planted via
$HELM_PRIVATE_NEEDLES (synthetic values only — a guard's tests must not
re-import what the guard exists to keep out), HELM_HOME sandboxed.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import doctor, work  # noqa: E402
from helm.work import _guard  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_LOG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "HELM_PRIVATE_NEEDLES", "HELM_NEVER_TRACK_SKIP",
            "HELM_WORK_CLAIM", "HELM_WORK_INTEGRATOR", "HELM_LANDLOCK")

NEEDLE = "zz-synthetic-omega"      # synthetic on purpose; see module docstring


class HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ntk-hook-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_LANDLOCK"] = "0"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.needles = os.path.join(self.tmp, "needles.txt")
        with open(self.needles, "w") as f:
            f.write("# synthetic test needles\n%s\n" % NEEDLE)
        os.environ["HELM_PRIVATE_NEEDLES"] = self.needles
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(self.sh(self.root, *cmd).returncode, 0)
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("seed\n")
        self.sh(self.root, "git", "add", "-A")
        r = self.sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, cwd, *args, env=None):
        merged = dict(os.environ, **(env or {}))
        return subprocess.run(list(args), cwd=cwd, capture_output=True,
                              text=True, timeout=60, env=merged)

    def cli(self, *args):
        """The real mechanism: the `helm work` dispatcher, not install_guard
        called directly — an install path nobody can reach is not wiring."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def install(self):
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        return out

    def head(self, cwd=None):
        return self.sh(cwd or self.root, "git", "rev-parse",
                       "HEAD").stdout.strip()

    def stage(self, rel, content, cwd=None):
        self.stage_bytes(rel, content.encode(), cwd)

    def stage_bytes(self, rel, blob, cwd=None):
        """Bytes, so a test can stage something git will call BINARY — which
        is where a diff-reading guard goes blind and has to fall back."""
        cwd = cwd or self.root
        full = os.path.join(cwd, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(blob)
        r = self.sh(cwd, "git", "add", "--", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def commit(self, msg="c", cwd=None, env=None):
        return self.sh(cwd or self.root, "git", "commit", "-q", "-m", msg,
                       env=env)


class InstallWiringTest(HookBase):
    def test_cli_apply_installs_an_executable_managed_pre_commit_hook(self):
        hook = work.hook_path(self.root, "pre-commit")
        rc, out, _err = self.cli("install-guard")
        self.assertEqual(rc, 0)
        self.assertIn("pre-commit", out)
        self.assertFalse(os.path.exists(hook), "dry-run prints, never writes")  # noqa: VACUOUS_ASSERTION — dry-run absence is the invariant
        out = self.install()
        self.assertIn("never-track", out)
        self.assertTrue(os.access(hook, os.X_OK))
        with open(hook) as f:
            head = f.read().splitlines()[:8]
        self.assertTrue(any(ln.startswith(work.MANAGED_HOOK_MARKER)
                            for ln in head),
                        "unmarked hook would never be owned/updated again")
        # hooks name stable installed snapshots, never the author source lane
        with open(hook) as f:
            body = f.read()
        assets = _guard._scanner_assets(self.root)
        never = next(p for p in assets if p.endswith("nevertrack.py"))
        vacuous = next(p for p in assets if p.endswith("vacuous_assertion.py"))
        self.assertIn(never, body)
        self.assertIn(vacuous, body)
        hardcode = next(p for p in assets if p.endswith("hardcode.py"))
        self.assertEqual(body.count('python3 "$hardcode_rung" --staged'), 1,
                         "duplicate hook invocations repeat every warning")
        self.assertIn(hardcode, body)
        self.assertNotIn(_guard._scanner_path(), body,
                         "a source lane path is editable and disposable")
        for installed, source in assets.items():
            with open(installed, "rb") as got, open(source, "rb") as want:
                self.assertEqual(got.read(), want.read())  # noqa: VACUOUS_ASSERTION — asset census above proves this loop runs

    def test_installed_snapshot_is_immune_to_later_source_edits(self):
        source = os.path.join(self.tmp, "source-vacuous.py")
        with open(source, "w") as f:
            f.write("ORIGINAL = True\n")
        with mock.patch.object(_guard, "_vacuous_assertion_rung_path",
                               return_value=source):
            self.install()
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("vacuous_assertion.py"))
        with open(installed) as f:
            before = f.read()
        self.assertIn("ORIGINAL", before, "installed snapshot was the source")
        with open(source, "w") as f:
            f.write("MUTATED = True\n")
        with open(installed) as f:
            self.assertEqual(f.read(), before,
                             "author source remained the live shared guard")

    def test_doctor_names_a_stale_installed_scanner_snapshot(self):
        self.install()
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("vacuous_assertion.py"))
        with open(installed, "a") as f:
            f.write("\n# stale\n")
        rows = doctor.check_work_guard(self.root)
        self.assertEqual(len(rows), 1)
        self.assertIn("scanner:vacuous_assertion.py", rows[0][1])
        self.assertIn("install-guard --apply", rows[0][1])

    def test_missing_scanner_fails_open_but_loud(self):
        """A deleted installed snapshot must not brick every commit — but the
        skip has to announce itself, or the estate believes it is guarded."""
        self.install()
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("nevertrack.py"))
        os.unlink(installed)
        self.stage("evals/report.md", "would be a violation if scanned\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: scanner missing", r.stderr)
        self.assertIn("install-guard --apply", r.stderr)

    def test_missing_vacuity_advisory_fails_open_but_loud(self):
        self.install()
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("vacuous_assertion.py"))
        os.unlink(installed)
        self.stage("docs/clean.md", "clean\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm vacuous-assertion-rung] WARNING", r.stderr)
        self.assertIn("install-guard --apply", r.stderr)


class RefusalTest(HookBase):
    def setUp(self):
        super().setUp()
        self.install()

    def test_staged_never_track_path_cannot_become_a_commit(self):
        before = self.head()
        self.stage("evals/dogfood-report.md", "internal build notes\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, "the commit MUST be refused")
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("evals/", r.stderr)
        self.assertIn("git restore --staged", r.stderr)
        self.assertEqual(self.head(), before, "history did not move")
        status = self.sh(self.root, "git", "status", "--porcelain").stdout
        self.assertIn("A  evals/dogfood-report.md", status,
                      "refusal leaves the stage intact for the fix-up")

    def test_staged_needle_content_is_refused_without_printing_the_needle(self):
        before = self.head()
        self.stage("docs/note.md", "context %s context\n" % NEEDLE)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("staged CONTENT carries private needle #1", r.stderr)
        self.assertNotIn(NEEDLE, r.stderr,
                         "the guard must stay quiet about the thing it guards")
        self.assertEqual(self.head(), before)

    def test_needle_in_the_filename_is_refused(self):
        self.stage("docs/%s-notes.md" % NEEDLE, "clean content\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PATH carries private needle #1", r.stderr)

    def test_the_scan_reads_the_index_not_the_worktree(self):
        """The commit takes the INDEX; the worktree is a rumor. Both
        directions: dirty worktree over a clean stage commits, and a clean
        worktree over a dirty stage is still refused."""
        self.stage("docs/a.md", "clean\n")
        with open(os.path.join(self.root, "docs", "a.md"), "w") as f:
            f.write("worktree now dirty: %s\n" % NEEDLE)
        r = self.commit()
        self.assertEqual(r.returncode, 0,
                         "unstaged dirt is the NEXT commit's problem: %s"
                         % r.stderr)
        self.stage("docs/b.md", "staged dirt: %s\n" % NEEDLE)
        with open(os.path.join(self.root, "docs", "b.md"), "w") as f:
            f.write("worktree scrubbed clean\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0,
                            "scrubbing the worktree does not scrub the stage")

    def test_lane_worktree_commits_are_guarded_too(self):
        """Worktrees share the common hooks dir — the guard covers the room
        where seats ACTUALLY commit, which is where the incident happened."""
        room = os.path.join(self.tmp, "proj-wt", "lane")
        r = self.sh(self.root, "git", "worktree", "add", "-q", "-b",
                    "lane/x", room, "main", env={"HELM_WORK_CLAIM": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("evals/leak.md", "internal\n", cwd=room)
        r = self.commit(cwd=room)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.stage("docs/fine.md", "clean\n", cwd=room)
        self.sh(room, "git", "restore", "--staged", "--", "evals/leak.md")
        r = self.commit(cwd=room)
        self.assertEqual(r.returncode, 0, r.stderr)


class PassageTest(HookBase):
    def setUp(self):
        super().setUp()
        self.install()

    def test_a_clean_commit_passes_with_needles_configured(self):
        before = self.head()
        self.stage("docs/clean.md", "nothing private here\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.head(), before)
        self.assertNotIn("REFUSED", r.stderr)

    def test_a_vacuous_test_warns_but_the_real_commit_succeeds(self):
        before = self.head()
        self.stage("tests/test_probe.py",
                   "def test_empty():\n    assert rows == []\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm vacuous-assertion-rung]", r.stderr)
        self.assertIn("test_empty", r.stderr)
        self.assertNotEqual(self.head(), before)

    def test_vacuity_warns_before_a_never_track_refusal(self):
        self.stage("tests/test_probe.py",
                   "def test_empty():\n    assert rows == []\n")
        self.stage("evals/private.md", "internal\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm vacuous-assertion-rung]", r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertLess(r.stderr.index("[helm vacuous-assertion-rung]"),
                        r.stderr.index("[helm never-track] REFUSED"))

    def test_no_needles_configured_is_a_visible_noop_not_a_silent_pass(self):
        self.stage("docs/clean.md", "content\n")
        r = self.commit(env={"HELM_PRIVATE_NEEDLES":
                             os.path.join(self.tmp, "absent-needles.txt")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NO-OP", r.stderr,
                      "an estate with no needle file must be able to SEE "
                      "that the content scan cannot fire")

    def test_skip_env_is_a_one_commit_owner_override(self):
        self.stage("evals/owner-sanctioned.md", "owner said land it\n")
        r = self.commit(env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        # and the very next commit is guarded again
        self.stage("evals/second.md", "no standing exemption\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)


class PreExistingLeakTest(HookBase):
    """THE COMMIT'S DIFF IS WHAT BLOCKS, NEVER THE FILE.

    The scan read each staged file WHOLE, so a needle sitting in a file's
    HISTORY refused every later commit that touched it — for content the
    commit did not write and could not remove. Measured: three occurrences of
    one needle in a 3,000-line tracked file's history refused every commit
    that touched the file, and the seat that hit it did the only thing left
    and used the documented one-commit bypass. That is the damage: blocking a NEW
    commit cannot un-commit a leak already in history, so the guard bought
    nothing and taught everyone the skip flag.

    So the FIRST test here is the one that matters — the guard must still
    refuse a commit that ADDS a needle. Everything after it only earns its
    keep once that one is green; a diff-aware guard that stopped blocking
    would pass every "does it let honest work through" test perfectly.
    """

    def setUp(self):
        super().setUp()
        self.install()

    def plant(self, rel, content):
        """A needle into HEAD the way the real one got there: the documented
        one-commit bypass, which is exactly what the blocked seat reached
        for. Returns the sha it landed as."""
        self.stage(rel, content)
        r = self.commit("plant", env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.head()

    def test_adding_a_needle_line_to_a_clean_file_is_STILL_refused(self):
        """THE MUST-HIT. Diff-awareness exists to stop punishing the innocent
        commit; if it also stopped catching the guilty one it would have
        disarmed the guard instead of aiming it."""
        self.stage("docs/clean.md", "line one\nline two\n")
        r = self.commit("seed the clean file")
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.head()
        self.stage("docs/clean.md", "line one\nline two\nline three %s\n" % NEEDLE)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, "an ADDED needle MUST be refused")
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertIn("docs/clean.md", r.stderr)
        self.assertEqual(self.head(), before, "history did not move")

    def test_a_brand_new_file_carrying_a_needle_is_refused(self):
        """Every line of a new file is an added line — the diff and the blob
        agree here, and that agreement is what makes the new-file case the
        easy one to accidentally break while rewriting for the hard one."""
        before = self.head()
        self.stage("docs/fresh.md", "intro\n%s\noutro\n" % NEEDLE)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertEqual(self.head(), before)

    def test_a_pre_existing_needle_does_not_block_an_unrelated_edit(self):
        """The measured incident, reproduced: the needle is in HEAD, this
        commit touches a different part of the file, and the commit LANDS."""
        self.plant("docs/legacy.md", "history line %s\nkeep me\n" % NEEDLE)
        before = self.head()
        self.stage("docs/legacy.md",
                   "history line %s\nkeep me\nnew clean line\n" % NEEDLE)
        r = self.commit("edit elsewhere in the file")
        self.assertEqual(r.returncode, 0,
                         "a commit cannot be blamed for what HEAD already "
                         "carries: %s" % r.stderr)
        self.assertNotEqual(self.head(), before, "history DID move")
        self.assertIn("docs/legacy.md", r.stderr,
                      "the pre-existing leak must still be named — a leak "
                      "that stops blocking and stops being mentioned is worse "
                      "than one that blocks")
        self.assertIn("ALREADY IN HEAD", r.stderr)
        self.assertIn("scrub commit", r.stderr)
        self.assertNotIn(NEEDLE, r.stderr,
                         "the guard must stay quiet about the thing it guards")

    def test_the_pre_existing_note_is_a_note_and_never_a_refusal(self):
        """Pinned apart from the returncode: the word REFUSED in that output
        is what trains a reader to reach for HELM_NEVER_TRACK_SKIP, whatever
        the exit status says."""
        self.plant("docs/legacy.md", "history %s\n" % NEEDLE)
        self.stage("docs/legacy.md", "history %s\nappended\n" % NEEDLE)
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        self.assertNotIn("ADDED BY THIS COMMIT", r.stderr)

    def test_a_binary_change_carrying_a_needle_is_refused(self):
        """git calls it binary and offers no line structure, so the added-side
        is unknowable — the scanner falls back to the WHOLE blob and
        over-blocks. An unreadable diff must never become a skipped scan."""
        before = self.head()
        self.stage_bytes("docs/blob.bin",
                         b"\x00\x01\x02" + NEEDLE.encode() + b"\x00\xff")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0,
                            "binary is where a diff-based guard goes blind")
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("docs/blob.bin", r.stderr)
        self.assertEqual(self.head(), before)

    def test_a_needle_in_a_NEW_path_is_refused(self):
        before = self.head()
        self.stage("docs/%s-notes.md" % NEEDLE, "clean content\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PATH carries private needle #1", r.stderr)
        self.assertNotIn("ALREADY IN HEAD", r.stderr,
                         "a path this commit CREATES is added by it")
        self.assertEqual(self.head(), before)

    def test_a_needle_path_already_in_HEAD_is_noted_not_refused(self):
        """Renaming a tracked file is its own commit. Refusing every edit
        until someone does it is the same trap as the content case."""
        rel = "docs/%s-notes.md" % NEEDLE
        self.plant(rel, "clean content\n")
        before = self.head()
        self.stage(rel, "clean content\nstill clean\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.head(), before)
        self.assertNotIn("REFUSED", r.stderr)
        self.assertIn("PATH carries private needle #1", r.stderr)
        self.assertIn("ALREADY IN HEAD", r.stderr)

    def test_a_needle_path_in_HEAD_does_not_excuse_ADDED_content(self):
        """The hole the first draft of the diff-aware scanner had: a path hit
        `continue`d past the content scan for that same needle, so a file
        already living at a needle-carrying path — the one file nobody
        re-reads — could have the needle added to its BODY unblocked."""
        rel = "docs/%s-notes.md" % NEEDLE
        self.plant(rel, "clean content\n")
        before = self.head()
        self.stage(rel, "clean content\nnow the body says %s too\n" % NEEDLE)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0,
                            "the path was already dirty; the CONTENT is new")
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertEqual(self.head(), before)


class ComposedUserHookTest(HookBase):
    def test_existing_pre_commit_hook_is_preserved_and_still_runs(self):
        hook = work.hook_path(self.root, "pre-commit")
        log = os.path.join(self.tmp, "user-hook.log")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nprintf ran >> %s\nexit 0\n" % log)
        os.chmod(hook, 0o755)
        with open(hook) as f:
            original = f.read()
        self.install()
        with open(hook + ".helm-user") as f:
            preserved = f.read()
        self.assertEqual(preserved, original, "user hook preserved byte-for-byte")
        self.stage("docs/ok.md", "clean\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            executed = f.read()
        self.assertEqual(executed, "ran", "user hook still executes")
        # a failing user hook refuses the commit before the scan runs
        with open(hook + ".helm-user", "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(hook + ".helm-user", 0o755)
        self.stage("docs/ok2.md", "clean\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
