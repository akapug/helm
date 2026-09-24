#!/usr/bin/env python3
"""The never-track pre-commit guard, END TO END — the enforcement-timing leg.

THE GAP THIS PROVES CLOSED (2026-07-29): a leak entered history with a green
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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import doctor, nevertrack, selfrepo, work  # noqa: E402
from helm.work import _guard  # noqa: E402

ENV_KEYS = ("HOME", "HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_LOG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "HELM_PRIVATE_NEEDLES", "HELM_NEVER_TRACK_SKIP",
            "HELM_WORK_CLAIM", "HELM_WORK_INTEGRATOR", "HELM_LANDLOCK")

NEEDLE = "zz-synthetic-omega"      # synthetic on purpose; see module docstring


class HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ntk-hook-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["HOME"])
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
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
                    ("git", "config", "user.name", "t"),
                    # THIS FIXTURE SIMULATES HELM-THE-SHARED-CHECKOUT, so it
                    # DECLARES the rail: an undeclared PROJECT repo now
                    # defaults to the leak legs (task/2441), and a fixture
                    # must state which law it is under rather than inherit
                    # a product-wide default.
                    ("git", "config", "--local",
                     "helm.guard.profile", "rail"),):
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
        return self.cli_in(self.root, *args)

    def cli_in(self, root, *args):
        """The same dispatcher against another repo in the sandbox — a fixture
        that stands in for helm's own checkout is a different repo, and it must
        be driven through the shipped verb like every other one."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", root])
        return rc, out.getvalue(), err.getvalue()

    def _helm_source_repo(self, name="helm-clone"):
        """A real temp git repo that IS a helm source checkout — the running
        package's own __init__.py and cli.py bytes at its root, plus the real
        entry script. Never a skeleton whose contents nothing produced."""
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(_guard.__file__)))
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "helm"))
        os.makedirs(os.path.join(root, "bin"))
        for mod in ("__init__.py", "cli.py"):
            shutil.copy2(os.path.join(pkg, mod),
                         os.path.join(root, "helm", mod))
        shutil.copy2(os.path.join(os.path.dirname(pkg), "bin", "helm"),
                     os.path.join(root, "bin", "helm"))
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(self.sh(root, *cmd).returncode, 0)
        # THE SEED CARRIES THE REPO'S NAME: two independently built repos with
        # identical content, message and author in the same second land on the
        # SAME commit sha, which is a silence condition for the tree warning.
        with open(os.path.join(root, "SEED"), "w") as f:
            f.write(name + "\n")
        self.sh(root, "git", "add", "-A")
        r = self.sh(root, "git", "commit", "-q", "-m", "helm")
        self.assertEqual(r.returncode, 0, r.stderr)
        return root

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
        # THE SOURCE PATH IS NAMED, AND ONLY EVER READ. The rule this arm
        # holds is that the hook must not RUN from the author's checkout: a
        # source lane is editable and disposable, so enforcement that reads it
        # would change when a lane edits it and fail open when the lane is
        # retired. The freshness compare names the source to answer "are these
        # still the rules that tree ships?", which touches neither property —
        # so the arm pins the executing position instead of the string.
        for ln in body.splitlines():
            if "$scanner_src" not in ln and _guard._scanner_path() not in ln:
                continue
            self.assertNotIn("python3", ln,
                             "the hook EXECUTES its author source: " + ln)
            self.assertTrue(ln.startswith("scanner_src=")
                            or ln.lstrip().startswith("cmp -s ")
                            or ln.lstrip().startswith('if [ -f "$scanner_src"')
                            or ln.lstrip().startswith('echo '),
                            "the source path is used for something other than "
                            "the read-only freshness compare: " + ln)
        self.assertIn('python3 "$scanner" --staged || exit $?', body,
                      "the scan does not run from the installed snapshot")
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


class OrphanedMockHookTest(HookBase):
    """THE INSTALLED HOOK, END TO END, and WARN-ONLY is half the contract.

    A census that only runs when someone types its name is the built-not-wired
    shape it was written to find. These arms drive the real composed
    pre-commit hook: one stages a double the modeled call graph cannot reach
    and requires the WARNING; the other stages an arm that proves its double
    fired and requires SILENCE. Both require the commit to SUCCEED, because an
    advisory rung that blocks is a refusing rung nobody agreed to.
    """

    ORPHAN = ('from unittest import mock\n'
              'from helm import landreq\n'
              'def test_probe():\n'
              '    with mock.patch.object(landreq, "_stored_patch_index") as i:\n'
              '        landreq._landed_index("/g", "t")\n'
              '    i.assert_not_called()\n')

    # The SAME otherwise-reportable target: reachability must not hide removal
    # of the firing-proof helper. Only the assertion differs between the arms.
    LIVE = ORPHAN.replace('i.assert_not_called()',
                          'i.assert_called_once_with("/g", "t")')

    def setUp(self):
        super().setUp()
        # Graph DATA, not an imported package. The installed scanners must read
        # the real control premise under the repository in which Git runs.
        source = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.makedirs(os.path.join(self.root, "helm"))
        shutil.copyfile(os.path.join(source, "helm", "landreq.py"),
                        os.path.join(self.root, "helm", "landreq.py"))

    def assert_candidate(self, result, rel):
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stderr.splitlines()
                 if line.startswith("[helm orphaned-mock] " + rel + ":")]
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("test_probe — patches landreq._stored_patch_index, "
                      "NOT REACHED BY THE MODELED GRAPH", lines[0])
        self.assert_no_scan_failure(result)

    def assert_no_scan_failure(self, result):
        self.assertNotIn("CONTROL FAILED", result.stderr)
        self.assertNotIn("scan UNKNOWN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("scanner missing", result.stderr)

    def test_an_unreachable_double_WARNS_and_the_commit_still_lands(self):
        self.install()
        before = self.head()
        self.stage("tests/test_planted_orphan.py", self.ORPHAN)
        r = self.commit("plant an orphan")
        self.assertEqual(r.returncode, 0,
                         "the advisory rung BLOCKED a commit: %s" % r.stderr)
        self.assertNotEqual(self.head(), before, "nothing was committed")
        self.assert_candidate(r, "tests/test_planted_orphan.py")

    def test_a_double_the_arm_proves_FIRED_stays_quiet_on_the_same_hook(self):
        """One installed hook, one file/target, changing only the assertion."""
        self.install()
        rel = "tests/test_planted_live.py"
        self.stage(rel, self.ORPHAN)
        self.assert_candidate(self.commit("plant the absence arm"), rel)
        before = self.head()
        self.stage(rel, self.LIVE)
        r = self.commit("require this same double to fire")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.head(), before)
        self.assert_no_scan_failure(r)
        self.assertNotIn("[helm orphaned-mock] " + rel + ":", r.stderr)

    def test_the_shared_helper_is_a_sibling_SNAPSHOT_not_the_source_lane(self):
        source = _guard._vacuous_assertion_rung_path()
        disposable = os.path.join(self.tmp, "vacuous_assertion.py")
        shutil.copyfile(source, disposable)
        with mock.patch.object(_guard, "_vacuous_assertion_rung_path",
                               return_value=disposable):
            self.install()
        # Deleting the author copy must not affect either installed scanner.
        os.remove(disposable)
        rel = "tests/test_sibling_snapshot.py"
        self.stage(rel, self.ORPHAN)
        self.assert_candidate(self.commit("prove the snapshot scans"), rel)
        self.stage(rel, self.LIVE)
        r = self.commit("prove the sibling helper exempts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_no_scan_failure(r)
        self.assertNotIn("[helm orphaned-mock] " + rel + ":", r.stderr)

    def test_a_missing_or_incompatible_REQUIRED_sibling_is_UNKNOWN_not_quiet(self):
        for mode in ("missing", "incompatible"):
            with self.subTest(mode=mode):
                self.install()
                rel = "tests/test_required_%s.py" % mode
                self.stage(rel, self.ORPHAN)
                self.assert_candidate(self.commit("prove intact siblings"), rel)
                sibling = next(p for p in _guard._scanner_assets(self.root)
                               if p.endswith("vacuous_assertion.py"))
                if mode == "missing":
                    os.remove(sibling)
                else:
                    with open(sibling, "w") as f:
                        f.write("# An old snapshot without the shared helper.\n")
                before = self.head()
                self.stage(rel, self.ORPHAN + "\n# new staged revision\n")
                r = self.commit("commit with the required helper unavailable")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotEqual(self.head(), before)
                self.assertIn("[helm orphaned-mock] scan UNKNOWN: required "
                              "vacuous_assertion sibling unavailable", r.stderr)
                self.assertIn("advisory SKIPPED", r.stderr)
                self.assertIn("install-guard --apply", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertNotIn("[helm orphaned-mock] " + rel + ":", r.stderr)

    def test_a_MISSING_scanner_snapshot_says_so_and_still_lets_the_commit_land(self):
        """The third state: the rung cannot run. It must SAY it did not run
        rather than pass silently, and it must still not block."""
        self.install()
        assets = _guard._scanner_assets(self.root)
        target = next(p for p in assets if p.endswith("orphaned_mock.py"))
        os.remove(target)
        self.stage("tests/test_after_removal.py", self.ORPHAN)
        r = self.commit("commit with the scanner gone")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm orphaned-mock] WARNING: scanner missing",
                      r.stderr)
        self.assertIn("install-guard --apply", r.stderr)


class RefusalTest(HookBase):
    def setUp(self):
        super().setUp()
        self.install()

    def test_staged_never_track_path_cannot_become_a_commit(self):
        before = self.head()
        self.stage("evals/dogfood-2026-07-29.md", "internal build notes\n")
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, "the commit MUST be refused")
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("evals/", r.stderr)
        self.assertIn("git restore --staged", r.stderr)
        self.assertEqual(self.head(), before, "history did not move")
        status = self.sh(self.root, "git", "status", "--porcelain").stdout
        self.assertIn("A  evals/dogfood-2026-07-29.md", status,
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
    """THE COMMIT'S DIFF IS WHAT BLOCKS, NEVER THE FILE (2026-07-30).

    The scan read each staged file WHOLE, so a needle sitting in a file's
    HISTORY refused every later commit that touched it — for content the
    commit did not write and could not remove. Measured: three occurrences of
    one needle in helm/web_ui.html's hint copy refused every commit to a
    3,000-line file, and the seat that hit it did the only thing left and
    used the documented one-commit bypass. That is the damage: blocking a NEW
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


class MergeAddsOnlyWhatNoParentHadTest(HookBase):
    """ADDED BY THIS COMMIT MEANS ABSENT FROM EVERY PARENT.

    THE MEASUREMENT. On a real lane, merging the trunk into a lane branch was
    refused: 14 files named as carrying private needles ADDED BY THIS COMMIT
    plus 30 e-mail addresses across 17 files, and `git rev-parse :<path>`
    equalled `git rev-parse <trunk>:<path>` for all 25 of them — every named
    byte was the trunk's, already in history, and exactly four files in that
    merge differed from both parents at all. The scan measured added-ness with
    `git diff --cached`, whose base is HEAD: during a merge that is the FIRST
    parent only. The lane then used HELM_NEVER_TRACK_SKIP=1 over a needle that
    was a TRUE positive for the history it already sat in — the bypass habit
    this guard family exists not to teach (bug class
    `guard-refuses-what-it-cannot-fix`).

    Every arm here drives the real installed hook through a real `git merge`
    and a real `git commit`; the two-parent index is git's, not a fixture's
    idea of one.
    """

    def setUp(self):
        super().setUp()
        self.install()

    def trunk(self):
        return self.sh(self.root, "git", "rev-parse", "--abbrev-ref",
                       "HEAD").stdout.strip()

    #: THE FIXTURE DECLARES THE RAIL, so its reference-transaction guard
    #: refuses a branch created in a shared checkout and names this override.
    #: A merge into a lane is integrator work; this is the door it uses.
    INTEGRATOR = {"HELM_WORK_INTEGRATOR": "1"}

    def plant_on_trunk(self, rel, content):
        """Land `content` on the trunk the way the real needle got there — the
        documented one-commit bypass — and come back to a lane branched BEFORE
        it, so the lane's HEAD has never seen the path."""
        trunk = self.trunk()
        r = self.sh(self.root, "git", "checkout", "-qb", "lane",
                    env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage("lane/own-work.md", "the lane's own work\n")
        r = self.commit("lane work", env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sh(self.root, "git", "checkout", "-q", trunk,
                    env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage(rel, content)
        r = self.commit("trunk work", env=dict(self.INTEGRATOR,
                                               HELM_NEVER_TRACK_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sh(self.root, "git", "checkout", "-q", "lane",
                    env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        return trunk

    def merge_head_count(self):
        """How many oids MERGE_HEAD holds — the claim that one line per merged
        head is what MERGE_HEAD records, asked of git rather than believed."""
        gitdir = self.sh(self.root, "git", "rev-parse",
                         "--absolute-git-dir").stdout.strip()
        with open(os.path.join(gitdir, "MERGE_HEAD")) as f:
            return len(f.read().split())

    def test_a_merge_of_a_trunk_that_carries_a_needle_LANDS(self):  # noqa: VACUOUS_ASSERTION — the same r.stderr is asserted to CONTAIN the pre-existing-parent note, unconditionally and before the absence assertion, so a hook that printed nothing fails first
        """THE MEASURED FAILURE, reproduced end to end: the merge's only change
        is a blob byte-identical to the second parent's, and the commit must
        land — refusing it cannot un-commit the trunk's leak and leaves the
        lane nothing but the skip flag."""
        trunk = self.plant_on_trunk("docs/legacy.md",
                                    "contact %s here\n" % NEEDLE)
        before = self.head()
        r = self.sh(self.root, "git", "merge", "--no-commit", "--no-ff", trunk,
                    env=self.INTEGRATOR)
        self.assertEqual(self.merge_head_count(), 1,
                         "no merge is in progress, so this arm is about an "
                         "ordinary commit: %s" % (r.stdout + r.stderr))
        staged = self.sh(self.root, "git", "diff", "--cached", "--name-only"
                         ).stdout.split()
        self.assertIn("docs/legacy.md", staged,
                      "the needle-carrying path is not even in the staged set, "
                      "so this arm proves nothing about the guard")
        r = self.commit("merge the trunk", env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0,
                         "a merge was refused for a blob its other parent "
                         "already carried: %s" % r.stderr)
        self.assertNotEqual(self.head(), before, "the merge did not land")
        # THE POSITIVE CONTROL ON THIS OBSERVABLE FIRST: the scan really did
        # speak about this file, so the absence below is about the verdict and
        # not about a hook that printed nothing.
        self.assertIn("ALREADY IN A PARENT OF THIS MERGE", r.stderr,
                      "the pre-existing leak is not named at all — a leak that "
                      "stops blocking and stops being mentioned is worse than "
                      "one that blocks: %s" % r.stderr)
        self.assertNotIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertNotIn(NEEDLE, r.stderr,
                         "the guard must stay quiet about what it guards")

    def test_a_merge_whose_RESOLUTION_adds_a_needle_is_refused_by_path(self):  # noqa: VACUOUS_ASSERTION — the same r.stderr is asserted to CONTAIN REFUSED, ADDED BY THIS COMMIT and docs/shared.md before it is asserted not to name lane/own-work.md
        """THE MUST-HIT on an otherwise-valid merge: same two parents, same
        clean-side file, and the only difference is that the resolution writes
        a needle. It must refuse, and name ONLY the file it is about."""
        trunk = self.plant_on_trunk("docs/shared.md", "the trunk's version\n")
        before = self.head()
        self.sh(self.root, "git", "merge", "--no-commit", "--no-ff", trunk,
                env=self.INTEGRATOR)
        self.assertEqual(self.merge_head_count(), 1)
        self.stage("docs/shared.md", "resolved with %s in it\n" % NEEDLE)
        r = self.commit("merge with a dirty resolution",
                        env=self.INTEGRATOR)
        self.assertNotEqual(r.returncode, 0,
                            "a needle THIS merge wrote was admitted: %s"
                            % r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertIn("docs/shared.md", r.stderr)
        self.assertNotIn("lane/own-work.md", r.stderr,
                         "the refusal names a file it has no finding about")
        self.assertEqual(self.head(), before, "history did not move")

    def test_a_single_parent_commit_adding_a_needle_is_STILL_refused(self):  # noqa: VACUOUS_ASSERTION — the same r.stderr is asserted to CONTAIN ADDED BY THIS COMMIT before the two absence assertions about the merge wording
        """THE CONTROL: the base did not move for the ordinary commit. Every
        arm above would also pass on a guard that simply stopped scanning."""
        self.stage("docs/clean.md", "line one\n")
        self.assertEqual(self.commit("seed").returncode, 0)
        before = self.head()      # AFTER the seed: the claim is about the next
        self.stage("docs/clean.md", "line one\nline two %s\n" % NEEDLE)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, "the single-parent scan stopped "
                                            "refusing an ADDED needle")
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertNotIn("merge in progress", r.stderr,
                         "a single-parent commit is reported as a merge")
        self.assertNotIn("PARENT OF THIS MERGE", r.stderr,
                         "the merge wording reached a commit with one parent, "
                         "so the single-parent output did not stay put")
        self.assertEqual(self.head(), before, "history did not move")

    def test_an_OCTOPUS_merge_reads_every_head_in_MERGE_HEAD(self):  # noqa: VACUOUS_ASSERTION — every reading here is a positive: MERGE_HEAD holds two oids, the commit exits 0, HEAD moved, and the note names three parents
        """MERGE_HEAD RECORDS ONE OID PER LINE, which is why an octopus needs
        no MERGE_MSG parsing. Asked of git: two branches merged at once, each
        carrying its own needle-bearing file, and the arm first asserts that
        MERGE_HEAD really does hold two oids — otherwise it would pass for the
        wrong reason on a guard that read only the first line."""
        trunk = self.trunk()
        for side in ("one", "two"):
            r = self.sh(self.root, "git", "checkout", "-q", "-b", side, trunk,
                        env=self.INTEGRATOR)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.stage("docs/%s.md" % side, "contact %s\n" % NEEDLE)
            r = self.commit("side " + side,
                            env=dict(self.INTEGRATOR,
                                     HELM_NEVER_TRACK_SKIP="1"))
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sh(self.root, "git", "checkout", "-q", trunk,
                    env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.head()
        r = self.sh(self.root, "git", "merge", "--no-commit", "one", "two",
                    env=self.INTEGRATOR)
        self.assertEqual(self.merge_head_count(), 2,
                         "MERGE_HEAD does not hold two oids, so this arm says "
                         "nothing about an octopus: %s" % (r.stdout + r.stderr))
        r = self.commit("octopus", env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0,
                         "an octopus merge was refused for blobs its second "
                         "and third parents already carried: %s" % r.stderr)
        self.assertNotEqual(self.head(), before)
        self.assertIn("all 3 parents", r.stderr,
                      "the note does not say how many parents were measured, "
                      "so a first-line-only read would look identical: %s"
                      % r.stderr)


class MergeInALinkedWorktreeSeesEveryParentTest(HookBase):
    """A LINKED WORKTREE WAS ACCUSED OF BLINDING THE PARENT SET, AND DOES NOT.

    THE REPORT: a project repo merged its trunk into a lane on a
    linked worktree (`git worktree add` under <repo>/.claude/worktrees/<name>,
    gitdir under <repo>/.git/worktrees/<name>), resolved one conflict by hand,
    and the pre-commit never-track guard refused six files as ADDED BY THIS
    COMMIT although `git cat-file -e <second-parent>:<path>` succeeded for
    every one — the exact symptom the merge-aware cure had closed. It was filed
    as `commit_parents` failing to surface the second parent under a linked
    worktree, where `rev-parse --git-path MERGE_HEAD` is answered against
    another base or MERGE_HEAD lives only in the linked gitdir.

    WHAT IS MEASURED INSTEAD. `--git-path MERGE_HEAD` answers with an ABSOLUTE
    path in a linked worktree (git sets GIT_DIR to the linked gitdir for hooks,
    and answers absolute even when the .git file holds a relative pointer), so
    the join is right and the parent set is complete. The refusing repo was
    running a FROZEN SNAPSHOT of this scanner installed before the cure landed
    — it holds no parent logic at all. That gap is cured at the hook (see
    AStaleScannerSnapshotSaysSoAtTheCommitTest); this class pins the shape the
    report accused, so the refutation is a standing arm and not a memory.

    Every arm drives the real installed hook through a real `git merge`, a real
    hand resolution and a real `git commit` INSIDE the linked worktree, which
    is how git supplies the hook environment (cwd at the worktree root, GIT_DIR
    and GIT_INDEX_FILE pointing into the linked gitdir, no GIT_WORK_TREE). The
    main-checkout twin of this leg is MergeAddsOnlyWhatNoParentHadTest above —
    the control that says a green arm here is about the worktree and not about
    a guard that stopped scanning.
    """

    #: A merge into a lane is integrator work; the fixture declares the rail.
    INTEGRATOR = {"HELM_WORK_INTEGRATOR": "1"}

    def setUp(self):
        super().setUp()
        self.install()
        r = self.sh(self.root, "git", "branch", "lane", env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The trunk carries a needle, the way the real one got there: the
        # documented one-commit bypass, on a path the lane has never seen.
        self.stage("docs/legacy.md", "contact %s here\n" % NEEDLE)
        self.stage("docs/shared.md", "the trunk's version\n")
        r = self.commit("trunk work", env=dict(self.INTEGRATOR,
                                               HELM_NEVER_TRACK_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wt = os.path.join(self.root, ".claude", "worktrees", "lane")
        r = self.sh(self.root, "git", "worktree", "add", "-q", self.wt,
                    "lane", env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.wt, ".git")),
                        "the room is not a LINKED worktree, so every arm here "
                        "would be a second main-checkout arm")
        self.stage("docs/shared.md", "the lane's version\n", cwd=self.wt)
        self.stage("lane/own-work.md", "the lane's own work\n", cwd=self.wt)
        r = self.commit("lane work", cwd=self.wt, env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)

    def linked_gitdir(self):
        return self.sh(self.wt, "git", "rev-parse",
                       "--absolute-git-dir").stdout.strip()

    def merge_main(self):
        """Merge the trunk into the lane INSIDE the linked worktree and stop on
        the conflict, which is the reported shape. Returns the second parent."""
        r = self.sh(self.wt, "git", "merge", "--no-ff", "main",
                    env=self.INTEGRATOR)
        self.assertNotEqual(r.returncode, 0,
                            "the merge did not conflict, so no hand "
                            "resolution is being tested: %s" % r.stdout)
        gitdir = self.linked_gitdir()
        self.assertNotEqual(os.path.realpath(gitdir),
                            os.path.realpath(os.path.join(self.root, ".git")),
                            "the merge state is in the SHARED gitdir")
        with open(os.path.join(gitdir, "MERGE_HEAD")) as f:
            oids = f.read().split()
        self.assertEqual(len(oids), 1, "MERGE_HEAD does not hold the one "
                                       "second parent this arm is about")
        return oids[0]

    def assert_carried_by(self, parent, rel):
        """The report's own instrument: the second parent HAS this path, so a
        refusal naming it as this commit's is false."""
        r = self.sh(self.wt, "git", "cat-file", "-e", parent + ":" + rel)
        self.assertEqual(r.returncode, 0,
                         "%s is NOT in the second parent, so refusing it would "
                         "be correct and this arm proves nothing" % rel)

    def pre_cure_snapshot(self):
        """Put the PRE-CURE resolution back into the snapshot the hook runs:
        parents are HEAD and nothing else, which is what `git diff --cached`
        baselines against during a merge. An arm that stays green with this in
        place is reading a constant instead of the producer."""
        snap = next(p for p in _guard._scanner_assets(self.root)
                    if p.endswith("nevertrack.py"))
        with open(snap) as f:
            src = f.read()
        marker = 'if __name__ == "__main__":'
        self.assertEqual(src.count(marker), 1,
                         "the snapshot's entry block is not where this "
                         "override has to sit to win")
        override = ("\n\ndef commit_parents(root):\n"
                    "    rc, out, _e = _git(root, 'rev-parse', '--verify',\n"
                    "                       '--quiet', 'HEAD')\n"
                    "    return [out.strip()] if rc == 0 and out.strip() "
                    "else []\n\n\n")
        with open(snap, "w") as f:
            f.write(src.replace(marker, override + marker, 1))
        return snap

    def test_a_hand_resolved_merge_in_a_linked_worktree_is_NOT_refused(self):  # noqa: VACUOUS_ASSERTION — the absences are read on an r.stderr already asserted to CONTAIN 'all 2 parents' and the in-a-parent note, after an exit 0 and a moved HEAD: a hook that printed nothing fails first
        """THE POSITIVE, end to end through the shipped rung: the trunk's
        needle-carrying file is in the staged set of a merge made on a linked
        worktree, and the commit lands because a parent already carries it."""
        parent = self.merge_main()
        self.stage("docs/shared.md", "resolved by hand\n", cwd=self.wt)
        staged = self.sh(self.wt, "git", "diff", "--cached",
                         "--name-only").stdout.split()
        self.assertIn("docs/legacy.md", staged,
                      "the needle-carrying path is not in the staged set, so "
                      "this arm says nothing about the guard")
        self.assert_carried_by(parent, "docs/legacy.md")
        before = self.head(cwd=self.wt)
        r = self.commit("merge main into lane", cwd=self.wt,
                        env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0,
                         "a merge on a LINKED worktree was refused for a blob "
                         "its second parent already carries: %s" % r.stderr)
        self.assertNotEqual(self.head(cwd=self.wt), before,
                            "the merge did not land")
        self.assertIn("all 2 parents", r.stderr,
                      "the scan did not measure against two parents, so the "
                      "second parent was never read here: %s" % r.stderr)
        self.assertIn("ALREADY IN A PARENT OF THIS MERGE", r.stderr,
                      "the pre-existing leak is not named at all: %s"
                      % r.stderr)
        self.assertNotIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertNotIn(NEEDLE, r.stderr,
                         "the guard must stay quiet about what it guards")

    def test_the_PRE_CURE_parent_set_refuses_that_same_merge(self):  # noqa: VACUOUS_ASSERTION — every reading is a positive except the unmoved HEAD, and that one follows an asserted non-zero exit plus the named file and the ADDED wording
        """THE MUST-HIT. Same worktree, same merge, same resolution — only the
        producer differs, and the arm above goes red without it. It also shows
        what the reporting repo saw: an unexplainable refusal, which the hook
        now prefaces by saying the scanner it ran is not its source's."""
        parent = self.merge_main()
        self.stage("docs/shared.md", "resolved by hand\n", cwd=self.wt)
        self.assert_carried_by(parent, "docs/legacy.md")
        self.pre_cure_snapshot()
        before = self.head(cwd=self.wt)
        r = self.commit("merge main into lane", cwd=self.wt,
                        env=self.INTEGRATOR)
        self.assertNotEqual(r.returncode, 0,
                            "the pre-cure parent set admitted the merge, so "
                            "the arm above is not reading commit_parents")
        self.assertIn("docs/legacy.md", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertEqual(self.head(cwd=self.wt), before, "history moved")
        self.assertIn("the installed scanner is NOT the one its source tree "
                      "now ships", r.stderr,
                      "the refusal that started this task is still silent "
                      "about the scanner that produced it: %s" % r.stderr)

    def test_a_path_NEITHER_parent_carries_is_still_refused_there(self):  # noqa: VACUOUS_ASSERTION — REFUSED, the new path and the ADDED wording are asserted present on the same stderr before the one absence, and the absent-from-both-parents precondition is asserted on git itself
        """THE NEGATIVE, on an otherwise-valid input: same worktree, same
        merge, same two parents, and the only difference is that the resolution
        writes a needle into a path no parent has. It can fail only at the gate
        under test."""
        parent = self.merge_main()
        self.stage("docs/shared.md", "resolved by hand\n", cwd=self.wt)
        self.stage("docs/resolution.md", "notes for %s\n" % NEEDLE,
                   cwd=self.wt)
        for ref in ("HEAD", parent):
            r = self.sh(self.wt, "git", "cat-file", "-e",
                        ref + ":docs/resolution.md")
            self.assertNotEqual(r.returncode, 0,
                                "a parent already carries the path this arm "
                                "calls new")
        before = self.head(cwd=self.wt)
        r = self.commit("merge main into lane", cwd=self.wt,
                        env=self.INTEGRATOR)
        self.assertNotEqual(r.returncode, 0,
                            "a needle THIS merge wrote was admitted on a "
                            "linked worktree: %s" % r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("docs/resolution.md", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertNotIn("docs/legacy.md — its staged CONTENT carries private "
                         "needle #1 of %s, ADDED" % self.needles, r.stderr,
                         "the second parent's own leak was blamed on this "
                         "merge as well")
        self.assertEqual(self.head(cwd=self.wt), before, "history moved")


class AStaleScannerSnapshotSaysSoAtTheCommitTest(HookBase):
    """THE SCANNER IS A FROZEN COPY, AND NOTHING TOLD THE COMMITTER.

    A scanner cure that LANDS does not reach a repo whose guard was installed
    earlier: the hook keeps running the snapshot under .helm-scanners, and the
    only advice its refusal offers is the bypass flag. The failure mode: a repo
    refuses a hand-resolved merge, six files named ADDED BY THIS COMMIT and all
    six present in the second parent, long after the merge-aware parent set
    made that impossible — its snapshot predates the cure. The estate surface
    already answers it (`helm doctor` reads `stale_guard_hooks` and reports
    such a repo as drift); the committer is not in the doctor's room, so the
    refusal reads as a fresh defect in a producer that measures correct. The
    hook asks the same byte compare where the commit happens.
    """

    def setUp(self):
        super().setUp()
        self.install()

    def snapshot(self):
        return next(p for p in _guard._scanner_assets(self.root)
                    if p.endswith("nevertrack.py"))

    WARNING = "the installed scanner is NOT the one its source tree now ships"
    #: printed by every run of the scan itself — the positive control that the
    #: hook reached the scanner at all, so an absence assertion below is about
    #: the freshness line and not about a hook that printed nothing.
    RAN = "private needles loaded"

    def test_a_snapshot_that_is_not_its_source_warns_and_still_commits(self):  # noqa: VACUOUS_ASSERTION — no absence is read: the exit is 0, HEAD moved, and the same stderr is asserted to carry the census note, the warning, both pathnames and the refresh
        """THE POSITIVE: an older copy stands in the snapshot's place, and the
        next ordinary commit says so — naming both files and the refresh — and
        is NOT refused, because a guard must not block over its housekeeping."""
        with open(self.snapshot(), "a") as f:
            f.write("\n# an older copy of the rules\n")
        self.stage("docs/clean.md", "line one\n")
        before = self.head()
        r = self.commit("ordinary")
        self.assertEqual(r.returncode, 0,
                         "the freshness warning refused a clean commit: %s"
                         % r.stderr)
        self.assertNotEqual(self.head(), before, "the commit did not land")
        self.assertIn(self.RAN, r.stderr)
        self.assertIn(self.WARNING, r.stderr)
        self.assertIn(self.snapshot(), r.stderr,
                      "the warning does not name the file that is running")
        self.assertIn(_guard._scanner_path(), r.stderr,
                      "the warning does not name the source it is behind")
        self.assertIn("helm work install-guard --apply", r.stderr,
                      "the reader is told they are stale and not how to fix it")

    def test_a_fresh_install_says_nothing_about_staleness(self):  # noqa: VACUOUS_ASSERTION — the absence IS the reviewed shape, and its unconditional positive controls come first: the snapshot bytes equal the source bytes, and the same stderr carries the census note the scan prints on every run
        """THE NEGATIVE, on an otherwise-valid input: byte-identical snapshot,
        same commit, same hook — it can differ only at the compare under test.
        A warning on every commit is how a guard's output gets ignored."""
        with open(self.snapshot(), "rb") as got, \
                open(_guard._scanner_path(), "rb") as want:
            self.assertEqual(got.read(), want.read(),
                             "the install did not snapshot its source, so a "
                             "silent hook here would prove nothing")
        self.stage("docs/clean.md", "line one\n")
        r = self.commit("ordinary")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.RAN, r.stderr)
        # SCOPED TO THIS RUNG'S OWN WARNINGS: sibling rungs on the same hook
        # warn about other things (the trailer rung does, every commit here),
        # and a bare "WARNING" absence would be a claim about them too.
        self.assertNotIn(self.WARNING, r.stderr)
        self.assertNotIn("[helm never-track] WARNING", r.stderr)

    def test_a_source_that_is_GONE_is_silent_rather_than_a_nag(self):  # noqa: VACUOUS_ASSERTION — the absence IS the reviewed boundary; the same stderr is first asserted to carry the census note, so a hook that never reached the scanner fails first
        """The documented boundary: the installing checkout can legitimately be
        retired, and a per-commit nag nobody in this repo can act on teaches the
        reader to skip the guard's output. `helm doctor` owns that case."""
        source = os.path.join(self.tmp, "retired-lane-nevertrack.py")
        shutil.copy2(_guard._scanner_path(), source)
        with mock.patch.object(_guard, "_scanner_path", return_value=source):
            self.install()
        os.remove(source)
        self.stage("docs/clean.md", "line one\n")
        r = self.commit("ordinary")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.RAN, r.stderr,
                      "the hook did not reach the scanner, so its silence "
                      "about staleness says nothing")
        self.assertNotIn(self.WARNING, r.stderr)
        self.assertNotIn("[helm never-track] WARNING", r.stderr)

    def test_the_hook_and_the_doctor_answer_the_same_question(self):
        """One predicate, two surfaces: the file the commit-time compare names
        is the file `stale_guard_hooks` reports, so a repo cannot be clean on
        one surface and stale on the other."""
        with open(self.snapshot(), "a") as f:
            f.write("\n# an older copy of the rules\n")
        self.stage("docs/clean.md", "line one\n")
        r = self.commit("ordinary")
        self.assertIn(self.WARNING, r.stderr)
        rows = [(st, n) for st, n, _w in _guard.stale_guard_hooks(self.root)]
        self.assertIn(("STALE", "scanner:nevertrack.py"), rows,
                      "the estate predicate does not see what the hook saw: "
                      "%s" % rows)


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


class LeakProfileTest(HookBase):
    """`--profile leak`: the two legs EVERY repo owes, without the rail.

    A project repo that is not run on the shared-checkout rail still has
    history that is forever and a remote that is a distribution. Installing
    the full rail there is wrong in both directions — its venue and citation
    rungs refuse ordinary work on that repo's own branch, so the operator
    learns the bypass, and a guard people bypass protects nothing. The leak
    profile arms the never-track staged-set scan (with its bulk-data leg) and
    the host-path push scan, records the choice in git config, and every
    reader — install drift, doctor — judges the repo by that declaration.
    """

    WXR = (b'<rss xmlns:wp="http://wordpress.org/export/1.2/"><channel>'
           b'<wp:wxr_version>1.2</wp:wxr_version></channel></rss>\n')

    def hooks_dir(self):
        return os.path.dirname(_guard.hook_path(self.root, "pre-commit"))

    def test_leak_profile_installs_two_hooks_and_records_the_declaration(self):
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        present = sorted(n for n in os.listdir(self.hooks_dir())
                         if not n.endswith(".sample") and not n.startswith("."))
        self.assertEqual(present, ["pre-commit", "pre-merge-commit", "pre-push"],
                         present)
        r = self.sh(self.root, "git", "config", "--get", _guard.PROFILE_KEY)
        self.assertEqual(r.stdout.strip(), "leak")
        self.assertEqual(_guard.guard_profile(self.root), "leak")

    def test_the_declaration_survives_a_reinstall_with_no_flag(self):
        self.cli("install-guard", "--apply", "--profile", "leak")
        rc, _out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        present = sorted(n for n in os.listdir(self.hooks_dir())
                         if not n.endswith(".sample") and not n.startswith("."))
        self.assertEqual(present, ["pre-commit", "pre-merge-commit", "pre-push"],
                         present)
        self.assertEqual(_guard.stale_guard_hooks(self.root), [])
        with open(_guard.hook_path(self.root, "pre-push"), "a") as f:
            f.write("# tampered\n")                 # positive control
        self.assertEqual([n for _s, n, _w in _guard.stale_guard_hooks(self.root)],
                         ["pre-push"])

    def test_a_commit_on_the_base_branch_lands_under_the_leak_profile(self):
        """The rail's lane-discipline rung would refuse this; the leak
        profile must not, or the operator learns the bypass."""
        self.cli("install-guard", "--apply", "--profile", "leak")
        before = self.head()
        self.stage("src/app.py", "print('hello')\n")
        r = self.commit("ordinary work on main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.head(), before, "nothing was committed")

    def test_a_staged_export_is_refused_by_the_installed_hook(self):
        self.cli("install-guard", "--apply", "--profile", "leak")
        self.stage_bytes("uploads/site.xml", self.WXR)
        before = self.head()
        r = self.commit("customer export")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("WordPress export", r.stderr)
        self.assertIn("untracking is NOT a scrub", r.stderr)
        self.assertEqual(self.head(), before, "the export became history")
        self.sh(self.root, "git", "restore", "--staged", "--", "uploads/site.xml")
        self.stage("uploads/site.txt", "not an export\n")
        r = self.commit("the same hook lets source through")   # positive control
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.head(), before)

    def test_a_staged_needle_is_still_refused_under_the_leak_profile(self):
        self.cli("install-guard", "--apply", "--profile", "leak")
        self.stage("notes.md", "ping %s\n" % NEEDLE)
        r = self.commit("needle")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(NEEDLE, r.stderr, "the guard printed what it guards")

    def test_doctor_reads_the_leak_profile_as_current_not_partial(self):
        self.cli("install-guard", "--apply", "--profile", "leak")
        rows = doctor.check_work_guard(self.root)
        self.assertTrue(rows and rows[0][0] == doctor.OK, rows)
        self.assertIn("pre-commit", rows[0][1])
        self.assertNotIn("reference-transaction", rows[0][1])

    def test_rail_to_leak_retires_the_rail_only_hooks_and_their_snapshots(self):
        """A switch is a transition, not an overlay: the hooks the old profile
        planned and the new one does not are retired in the same
        transaction, or they would keep enforcing rules outside every
        census."""
        rc, _out, err = self.cli("install-guard", "--apply")          # rail
        self.assertEqual(rc, 0, err)
        for name in ("reference-transaction", "post-checkout", "commit-msg"):
            self.assertTrue(os.path.exists(_guard.hook_path(self.root, name)), name)
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        for name in ("reference-transaction", "post-checkout", "commit-msg"):
            self.assertFalse(os.path.exists(_guard.hook_path(self.root, name)), name)
        self.assertIn("profile rail → leak", out)   # a declared rail, not a fresh repo
        snaps = os.listdir(os.path.join(self.hooks_dir(), ".helm-scanners"))
        self.assertEqual(sorted(snaps), ["conflict_marker.py", "hostpath_guard.py",
                                        "nevertrack.py"], snaps)
        self.assertEqual(_guard.stale_guard_hooks(self.root), [])

    def test_rail_to_leak_restores_a_user_hook_the_rail_had_wrapped(self):
        """The rail composes a user's own hook in as <name>.helm-user and runs
        it from its wrapper. Retiring the wrapper must give the name back to
        the user's hook, or their enforcement goes silently dark."""
        user = _guard.hook_path(self.root, "commit-msg")
        os.makedirs(os.path.dirname(user), exist_ok=True)
        with open(user, "w") as f:
            f.write("#!/bin/sh\ngrep -q FORBIDDEN \"$1\" && { echo user-hook-refused >&2; exit 1; }\nexit 0\n")
        os.chmod(user, 0o755)
        with open(user, "rb") as f:
            original = f.read()
        rc, _out, err = self.cli("install-guard", "--apply")          # rail
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(user + ".helm-user"))
        self.stage("a.txt", "a\n")
        r = self.commit("FORBIDDEN word")
        self.assertNotEqual(r.returncode, 0, "the rail did not compose the user hook")
        self.assertIn("user-hook-refused", r.stderr)
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertIn("restored the user's own hook", out)
        self.assertFalse(os.path.exists(user + ".helm-user"))
        with open(user, "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertTrue(os.access(user, os.X_OK))
        r = self.commit("FORBIDDEN word again")
        self.assertNotEqual(r.returncode, 0, "the user's hook went dark")
        self.assertIn("user-hook-refused", r.stderr)
        r = self.commit("an ordinary message")               # positive control
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rail_to_leak_archives_a_helm_owned_companion_instead_of_deleting_it(self):
        """A superseded helm hook sits at <name>.helm-user (the legacy
        population the retained plan preserves as .helm-superseded). Retiring
        its wrapper must archive it the same way, never delete it."""
        target = _guard.hook_path(self.root, "commit-msg")
        legacy = "#!/bin/sh\n# helm work managed hook: commit-msg v0b (legacy)\nexit 0\n"
        rc, out, err = self.cli("install-guard", "--apply")          # rail
        self.assertEqual(rc, 0, err)
        archive = target + _guard.RETIRED_HOOK_SUFFIX
        self.assertFalse(os.path.exists(archive))
        # plant a helm-owned companion the way an older installer left one
        with open(target + ".helm-user", "w") as f:
            f.write(legacy)
        os.chmod(target + ".helm-user", 0o755)
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".helm-user"))
        self.assertTrue(os.path.exists(archive), out)
        with open(archive) as f:
            self.assertIn("v0b", f.read())
        self.assertFalse(os.access(archive, os.X_OK))
        self.assertIn("archived superseded helm hook", out)

    def test_the_archive_is_written_before_the_companion_is_emptied(self):
        """Order is the invariant: an interrupt between the two writes must
        find the bytes in the archive already. Pinned by recording the
        sequence of node writes the transaction performs."""
        target = _guard.hook_path(self.root, "commit-msg")
        rc, _out, err = self.cli("install-guard", "--apply")          # rail
        self.assertEqual(rc, 0, err)
        with open(target + ".helm-user", "w") as f:
            f.write("#!/bin/sh\n# helm work managed hook: commit-msg v0b (legacy)\nexit 0\n")
        os.chmod(target + ".helm-user", 0o755)
        seq = []
        real = _guard._put_snapshot
        def spy(path, snap):
            seq.append(path)
            return real(path, snap)
        with mock.patch.object(_guard, "_put_snapshot", spy):
            rc, _out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        archive = target + _guard.RETIRED_HOOK_SUFFIX
        self.assertIn(archive, seq)
        self.assertIn(target + ".helm-user", seq)
        self.assertLess(seq.index(archive), seq.index(target + ".helm-user"),
                        "the companion was emptied before its archive existed: %r" % seq)

    def test_a_foreign_hook_at_a_retired_name_is_left_alone(self):
        self.cli("install-guard", "--apply")                          # rail
        foreign = _guard.hook_path(self.root, "commit-msg")
        with open(foreign, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(foreign, 0o755)
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(foreign))
        self.assertIn("not a helm hook", out)

    def test_a_failed_switch_keeps_the_previous_declaration(self):
        """The profile is written LAST, inside the transaction: a leak→rail
        install that cannot read a rail scanner rolls back and the repo
        still declares leak — the hooks it actually has."""
        self.cli("install-guard", "--apply", "--profile", "leak")
        real = _guard._docref_rung_path
        _guard._docref_rung_path = lambda: os.path.join(self.tmp, "no-such-scanner.py")
        try:
            rc, _out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        finally:
            _guard._docref_rung_path = real
        self.assertEqual(rc, 1)
        self.assertIn("unreadable", err + _out)
        self.assertEqual(_guard.guard_profile(self.root), "leak")
        present = sorted(n for n in os.listdir(self.hooks_dir())
                         if not n.endswith(".sample") and not n.startswith("."))
        self.assertEqual(present, ["pre-commit", "pre-merge-commit", "pre-push"],
                         present)

    def test_a_non_executable_hook_is_drift_even_with_identical_bytes(self):
        self.cli("install-guard", "--apply", "--profile", "leak")
        target = _guard.hook_path(self.root, "pre-commit")
        os.chmod(target, 0o644)
        drift = _guard.stale_guard_hooks(self.root)
        self.assertEqual([(st, n) for st, n, _w in drift], [("STALE", "pre-commit")], drift)
        self.assertIn("not executable", drift[0][2])
        rows = doctor.check_work_guard(self.root)
        self.assertEqual(rows[0][0], doctor.WARN, rows)

    def test_the_leak_profile_installs_from_a_feature_branch(self):
        """The base-branch precondition is the rail's; the leak hooks are
        branch-independent and an ordinary repo must not switch to install."""
        self.sh(self.root, "git", "checkout", "-q", "-b", "feature/x")
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.guard_profile(self.root), "leak")
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        self.assertEqual(rc, 1, "the rail still refuses off its base branch")
        self.assertIn("expected main", err)

    def test_the_leak_success_text_names_only_what_it_installed(self):
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertIn("never-track", out)
        self.assertIn("host paths", out)
        # THE TRANSITION THIS FIXTURE IS IN: HookBase declares the rail, so the
        # note reports a declared rail → leak switch. The UNDECLARED wording is
        # its own subject one class down, where it now names the leak default a
        # project repo actually gets (task/2441).
        self.assertIn("profile rail → leak", out)
        # The merge door runs the conflict-marker rung under the leak
        # profile too, so its name belongs in this text.
        self.assertIn("pre-merge-commit runs the conflict-marker", out)
        for absent in ("ORIGINATES", "branch creation",
                       "HELM_LANE_DISCIPLINE_SKIP", "HELM_WORLD_PROSE_SKIP",
                       "HELM_SEATNAME_SKIP"):
            self.assertNotIn(absent, out, absent)

    def test_an_unknown_declared_profile_refuses_rather_than_falling_back(self):
        self.sh(self.root, "git", "config", "--local", _guard.PROFILE_KEY, "lek")
        rc, _out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("not a guard profile", err)

    def test_the_cli_refuses_a_profile_it_does_not_know(self):
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "all")
        self.assertEqual(rc, 2)
        self.assertIn("--profile must be one of", err)


class UndeclaredProjectRepoGetsTheUserDefaultTest(HookBase):
    """TASK/2441 — a repo that declared nothing gets the LEAK profile; only
    helm's own source checkout defaults to the rail.

    A team USING helm must never need to know how helm is made. `rail` is
    helm-the-repo's shared-checkout law — the reference-transaction ref guard,
    the lane-discipline venue rung, the attribution trailer, the citation and
    public-prose rungs — process an adopter's project never agreed to. `leak`
    is the pair of legs EVERY repo owes, because history is forever and a
    remote is a distribution.

    MEASURED from a project repo outside this checkout: `helm work
    install-guard --apply --profile leak` printed "profile undeclared (rail by
    default)", and `guard_profile` on a fresh project repo returned "rail", so
    a flagless install planned all five rail hooks into it.
    """

    def setUp(self):
        super().setUp()
        # THE SUBJECT IS THE ABSENCE OF A DECLARATION. HookBase declares the
        # rail because its other subclasses fixture helm-the-shared-checkout;
        # this class is about the repo that declared nothing at all.
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root),
                          "fixture: nothing may be declared here")

    def test_an_undeclared_project_repo_resolves_to_the_leak_profile(self):
        # THE IDENTITY DEFAULT IS THE LAST STEP, so this arm must state that
        # the two before it declined: nothing is declared (setUp asserts it)
        # and nothing is installed. Without that, task/2504's middle step —
        # what the repo is RUNNING — could be what answers here, and this arm
        # would pass while saying nothing about the default.
        self.assertIsNone(_guard.installed_profile(self.root),
                          "fixture: this repo runs no helm hook, so the "
                          "IDENTITY default is the step under test")
        self.assertEqual(_guard.guard_profile(self.root), "leak")

    def test_the_generated_leak_hooks_name_leak_in_their_own_remedy(self):  # noqa: VACUOUS_ASSERTION — the bare-form and wrong-profile clauses are absence-shaped by nature (a remedy line that CANNOT narrow this repo), and the unconditional positive on the same observable is the assertGreater below: the leak hooks really do carry `--profile leak` remedy lines, counted from the same bodies the absences are read from
        """TASK/2504, the leak pole of the hook-remedy arm in
        `LegacyUndeclaredRailInstallIsWhatItLeavesTest`: the same generator,
        the same lines, the other profile."""
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        found = 0
        for name, _t in _guard.GUARD_PROFILES["leak"]:
            with open(work.hook_path(self.root, name)) as f:
                body = f.read()
            found += body.count("install-guard --apply --profile leak")
            self.assertNotIn("install-guard --apply\"", body, name)
            self.assertNotIn("--profile rail", body, name)
            self.assertNotIn("%(profile)s", body, name)
        self.assertGreater(found, 0, "the leak hooks carry no remedy line at "
                                     "all — this arm would measure nothing")

    def test_a_flagless_install_arms_the_leak_legs_and_declares_leak(self):
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "leak")
        self.assertIn("profile leak", out)
        self.assertTrue(os.access(work.hook_path(self.root, "pre-commit"),
                                  os.X_OK))
        self.assertTrue(os.access(work.hook_path(self.root, "pre-push"),
                                  os.X_OK))
        for rail_only in ("reference-transaction", "post-checkout",
                          "commit-msg"):
            self.assertFalse(
                os.path.exists(work.hook_path(self.root, rail_only)),
                "%s is the rail's, and this repo is not the rail" % rail_only)

    def test_the_install_note_names_the_default_that_actually_applied(self):
        """The reported line. "rail by default" was true of the code and wrong
        about this repo; the note now names the default this repo GOT."""
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("undeclared (leak by default)", out)
        self.assertNotIn("rail by default", out)

    def test_a_project_repo_plans_only_the_leak_hooks(self):
        plan = [p["name"] for p in _guard._guard_plan(self.root)[1]]
        # POSITIVE CONTROL FIRST: the planner produced the two legs it owes, so
        # the equality below is about WHICH hooks, never about an empty plan.
        self.assertIn("pre-commit", plan)
        self.assertIn("pre-push", plan)
        self.assertEqual(plan, [n for n, _t in _guard.GUARD_PROFILES["leak"]])

    def test_helms_OWN_source_checkout_still_defaults_to_the_rail(self):
        """MUST-HIT CONTROL. The leak answer above must come from the repo's
        identity, not from a default that stopped saying rail to anyone: an
        undeclared HELM checkout still resolves to the rail and still plans all
        five of its hooks."""
        clone = self._helm_source_repo()
        self.assertIsNone(_guard.declared_profile(clone))
        self.assertIsNone(_guard.installed_profile(clone),
                          "fixture: a FRESH checkout runs no helm hook, so "
                          "the IDENTITY default is the step under test")
        self.assertEqual(_guard.guard_profile(clone), "rail")
        self.assertEqual([p["name"] for p in _guard._guard_plan(clone)[1]],
                         [n for n, _t in _guard.GUARD_HOOKS])

    def test_an_explicit_rail_flag_still_installs_the_rail_here(self):
        """The default is a DEFAULT, not a ceiling: a project that grows onto
        the shared-checkout rail asks for it and gets it."""
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "rail")
        self.assertTrue(os.access(work.hook_path(self.root, "commit-msg"),
                                  os.X_OK))


class LegacyUndeclaredRailInstallIsWhatItLeavesTest(HookBase):
    """A REPO ALREADY UNDER THE RAIL, DECLARING NOTHING, IS NOT A FRESH REPO.

    The install transaction retires the slots the outgoing profile planned and
    the incoming one does not. Reading that outgoing profile through the
    identity DEFAULT made a project repo's installed history unreadable: put
    under the rail before it ever recorded a profile — the state every repo the
    rail reached before the declaration existed is in — it answered "leak", the
    default for its identity, so a flagless install found nothing departing. The
    reference-transaction, post-checkout and commit-msg hooks and the rail's
    scanner snapshots kept executing, and `leak` was recorded over them: the
    declaration described hooks that were not the ones running, and doctor,
    drift and the next install all read the declaration.

    The outgoing profile is now what this repo IS under — what it RECORDED, and
    when it recorded nothing, the hook set on disk (`installed_profile`).
    """

    def setUp(self):
        super().setUp()
        # THE LEGACY STATE, BUILT BY THE REAL INSTALLER: arm the rail through
        # the shipped CLI, then remove the declaration. Owned rail hooks on
        # disk, nothing recorded — no hand-written hook skeleton, because the
        # question is whether the installer recognises its OWN output.
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        self.assertEqual(rc, 0, err)
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root),
                          "fixture: nothing may be declared here")
        # DERIVED FROM THE PROFILE TABLES, never transcribed, and never from
        # the constant the cure introduced: the hooks the rail plans and the
        # leak profile does not are what must depart.
        leak_names = {name for name, _t in _guard.GUARD_PROFILES["leak"]}
        self.rail_only = [work.hook_path(self.root, name)
                          for name, _t in _guard.GUARD_PROFILES["rail"]
                          if name not in leak_names]
        self.assertEqual(len(self.rail_only), 3,
                         "fixture: three slots are the rail's alone")
        for path in self.rail_only:
            self.assertTrue(os.access(path, os.X_OK),
                            "fixture: the rail really is armed at %s" % path)
        self.rail_snapshots = [
            path for path in _guard._scanner_assets(self.root, "rail")
            if path not in _guard._scanner_assets(self.root, "leak")]
        for path in self.rail_snapshots:
            self.assertTrue(os.path.isfile(path),
                            "fixture: the rail's shelf really is there")

    def test_the_generated_rail_hooks_name_the_rail_in_their_own_remedy(self):  # noqa: VACUOUS_ASSERTION — same shape as its leak pole: the unconditional positive is the assertGreater on the rendered `--profile rail` remedy count, read from the same hook bodies the absence clauses are read from
        """TASK/2504. THE HOOK'S OWN ADVICE MUST NOT BE ABLE TO NARROW THE
        REPO IT RUNS IN. Every SKIPPED/stale line inside a generated hook told
        the reader to run `helm work install-guard --apply`, and a hook is the
        oldest tree in the repo by construction — it was written by whatever
        version installed it. The generator knows the profile at generation
        time, so it renders it.

        CONTROL: the same assertion at the leak pole is
        `UndeclaredProjectRepoGetsTheUserDefaultTest`'s, and it demands
        `--profile leak` in the leak hooks — so this is the profile being
        rendered, not the word "rail" appearing in a rail repo's hooks."""
        bodies = {}
        for name, _t in _guard.GUARD_PROFILES["rail"]:
            with open(work.hook_path(self.root, name)) as f:
                bodies[name] = f.read()
        remedies = sum(b.count("install-guard --apply --profile rail")
                       for b in bodies.values())
        self.assertGreater(remedies, 0,
                           "the installed rail hooks carry no remedy line at "
                           "all — this arm would be measuring nothing")
        for name, body in bodies.items():
            self.assertNotIn("install-guard --apply\"", body,
                             "%s still carries the bare remedy a stale hook "
                             "would print" % name)
            self.assertNotIn("--profile leak", body, name)
            self.assertNotIn("%(profile)s", body,
                             "%s leaked the template key instead of the "
                             "profile" % name)

    def test_the_synopsis_and_the_help_state_what_a_flagless_install_does(self):
        """The doc and the `--help` tail are pinned HERE, beside the behaviour:
        a synopsis and the verb it describes go stale separately, and the arms
        above are what make this more than a string compare. `docs/VERBS.md`
        said an undeclared repo "gets the profile its own identity carries",
        full stop — the sentence that made a client project's paste look correct."""
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(_guard.__file__)))
        with open(os.path.join(os.path.dirname(pkg), "docs", "VERBS.md")) as f:
            text = f.read()
        self.assertIn("THE TARGET RESOLVES IN THREE STEPS", text)
        self.assertIn("what its installed hooks are RUNNING", text)
        self.assertIn("A FLAGLESS INSTALL NEVER", text)
        from helm.work import _cli
        self.assertIn("NO --profile RESOLVES IN THIS ORDER", _cli.USAGE)

    def test_the_disk_says_rail_when_nothing_is_recorded(self):
        """The predicate itself: an undeclared repo carrying the rail's own
        hooks reports rail, and the identity default it is JUDGED under —
        leak, for this project repo — is a different question."""
        self.assertEqual(_guard.installed_profile(self.root), "rail")
        self.assertEqual(_guard.default_profile(self.root), "leak")

    def test_a_flagless_install_KEEPS_the_profile_this_repo_is_running(self):  # noqa: VACUOUS_ASSERTION — the loop is a byte/mode equality over a fixture-derived path list, not an absence; the unconditional positives are the recorded profile (rail) and the install note naming `undeclared (rail installed) → rail`, both equalities on the shipped verb's own output
        """THE CLIENT-PROJECT SHAPE, task/2504, and the arm the whole class turns on.

        MEASURED on a shared checkout that had never recorded a profile and
        had been put under the rail: `helm work claim`
        warned GUARD RAIL NOT ARMED and printed `helm work install-guard
        --apply` as the remedy; that exact paste retired three rail-only hook
        slots and eleven rail scanner snapshots and recorded leak. The refresh
        SAW the rail — its own note said `undeclared (rail installed)` — and
        narrowed the repo anyway, because the target resolved through the
        IDENTITY default, which answers a question about a FRESH repo.

        CONTROL, and it is a mutation control rather than a sibling call: the
        pre-cure `guard_profile` is literally `checked_declaration(root) or
        default_profile(root)`, so this exact fixture returned `leak` and every
        assertion below inverted — the recorded profile, the rail's whole
        artifact set (three hook slots plus every rail-only snapshot), and the
        note's arrow. Measured on the fab by restoring that one-line body
        under this arm: RED, `leak` recorded and the artifacts gone."""
        before = {}
        for path in self.rail_only + self.rail_snapshots:
            with open(path, "rb") as f:
                before[path] = (os.stat(path).st_mode, f.read())
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "rail",
                         "an undeclared repo RUNNING the rail is recorded "
                         "under the rail it is running, never narrowed to the "
                         "default a FRESH repo of its identity would get; "
                         "note was: %s" % out)
        # BYTE-IDENTICAL, not merely present: a hook rewritten to the leak
        # profile's body at the same path would pass an existence check.
        for path, (mode, body) in before.items():
            self.assertTrue(os.path.lexists(path),
                            "%s is a rung this repo was running; note was: %s"
                            % (path, out))
            self.assertEqual(os.stat(path).st_mode, mode, path)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), body, path)
        self.assertIn("undeclared (rail installed) → rail", out)

    def test_an_EXPLICIT_leak_retires_every_departing_rail_slot(self):
        """THE OPERATOR'S DECISION STAYS OPEN, and it is the path the refusal
        advertises: naming `--profile leak` on a repo running the rail narrows
        it on purpose and keeps every retirement note. The arm above and this
        one differ by two argv tokens."""
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "leak")
        for path in self.rail_only:
            self.assertFalse(os.path.lexists(path),
                             "%s is the rail's and this repo left the rail; "
                             "note was: %s" % (path, out))
        # POSITIVE CONTROLS, UNCONDITIONAL: the two legs the leak profile DOES
        # run are armed, so the absences above are about which hooks and never
        # about an emptied hooks directory or a refused install.
        self.assertTrue(os.access(work.hook_path(self.root, "pre-commit"),
                                  os.X_OK))
        self.assertTrue(os.access(work.hook_path(self.root, "pre-push"),
                                  os.X_OK))
        self.assertIn("leak guard", out)

    def test_the_departing_snapshots_go_and_the_note_says_so(self):
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        for path in self.rail_snapshots:
            self.assertFalse(os.path.lexists(path),
                             "%s is a rung the leak hooks never run; note was: "
                             "%s" % (path, out))
        self.assertIn("retired", out)
        self.assertIn("scanner snapshot", out)
        self.assertIn("lane_discipline.py", out)
        # POSITIVE CONTROLS, UNCONDITIONAL: the shelf the leak hooks DO run is
        # still there, so the absences above are not a deleted directory.
        shelf = os.path.join(
            os.path.dirname(work.hook_path(self.root, "pre-commit")),
            ".helm-scanners")
        self.assertTrue(os.path.isfile(os.path.join(shelf, "nevertrack.py")),
                        "the staged-set scanner is what the leak pre-commit runs")
        self.assertTrue(os.path.isfile(os.path.join(shelf, "hostpath_guard.py")),
                        "the host-path scanner is what the leak pre-push runs")

    def test_the_note_names_the_transition_off_the_installed_profile(self):
        """The reader is told which law this repo was under, not which one an
        undeclared repo of its identity would have been judged under."""
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertIn("undeclared (rail installed) → leak", out)
        self.assertNotIn("by default", out)

    def test_a_flagless_NARROWING_refuses_before_touching_a_hook(self):
        """THE OTHER ROUTE TO THE SAME ACCIDENT. The resolver keeps an
        undeclared repo under what it is running, so the only way a flagless
        refresh can still retire live rungs is a DECLARATION that disagrees
        with the disk — leak recorded while the rail is armed. Nobody asked for
        a narrowing there either, and this verb is what the staleness notice
        tells an operator to run, so it REFUSES and names the command that
        does it on purpose.

        AND IT REFUSES BEFORE ANY WRITE: the whole hook directory is compared
        path-by-path, bytes and mode, against the snapshot taken first.

        CONTROL: `test_the_departing_snapshots_go_and_the_note_says_so` is this
        fixture plus the two argv tokens the refusal names, and it retires
        every one of them — so this is the flag being absent, not a door that
        refuses every install on a rail repo."""
        self.sh(self.root, "git", "config", "--local", _guard.PROFILE_KEY,
                "leak")
        hookdir = os.path.dirname(work.hook_path(self.root, "pre-commit"))

        def fingerprint():
            seen = {}
            for dirpath, _dirs, files in os.walk(hookdir):
                for name in files:
                    path = os.path.join(dirpath, name)
                    with open(path, "rb") as f:
                        seen[path] = (os.lstat(path).st_mode, f.read())
            return seen

        before = fingerprint()
        self.assertTrue(before, "fixture: this repo really has hooks to lose")
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 1, "a flagless refresh may not narrow: %s"
                                % (out + err))
        # A REFUSAL SPEAKS ON STDERR — the dispatcher routes a non-zero install
        # there, and a refusal an operator's pipe drops is not a refusal.
        self.assertIn("REFUSED", err)
        self.assertIn("RUNNING the rail profile", err)
        self.assertIn("no --profile was given", err)
        # THE EXACT COMMANDS, both poles: the one that narrows on purpose and
        # the one that keeps this repo where it is.
        self.assertIn("install-guard --apply --profile leak", err)
        self.assertIn("install-guard --apply --profile rail", err)
        # AND EVERY ARTIFACT IT NAMED IS STILL THERE. Derived from the profile
        # tables by setUp, never transcribed.
        for path in self.rail_only + self.rail_snapshots:
            self.assertIn(os.path.basename(path), err,
                          "the refusal names what would have gone")
        self.assertEqual(fingerprint(), before,
                         "nothing in the hook directory may change before a "
                         "refusal — bytes and mode, every path")


class AGitdirIsTheSameRepositoryAsItsCheckoutTest(HookBase):
    """FINDING 1 — A REPOSITORY IDENTITY IS NOT ALWAYS SPELLED AS A ROOT.

    helm's dispatch store names a repository by its GITDIR: `_repo_info`
    records the absolute `--git-common-dir` as `repo_id`, `_close_repo` selects
    that value, and a `--live` close hands it straight to `stale_guard_hooks`.
    The identity default classified whatever it was given by looking for
    `<given>/helm/__init__.py`, so the gitdir spelling of helm's OWN checkout
    answered "not helm's source" and the expected hook set was rendered for the
    leak profile. A repo running a CURRENT rail then compared STALE at its
    composed `pre-commit`, and the close refused a `--live` row over drift that
    does not exist.

    The cure is one normalization — `selfrepo.checkout_root` — asked before any
    classification, and it walks UP the path it was given and never sideways,
    so two clones stay two repositories.
    """

    def setUp(self):
        super().setUp()
        self.src = self._helm_source_repo()
        self.assertIsNone(_guard.declared_profile(self.src),
                          "fixture: nothing is declared — the identity default "
                          "is the subject")
        rc, _out, err = self.cli_in(self.src, "install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.src), "rail",
                         "fixture: an undeclared helm checkout arms the rail")
        # AND THE DECLARATION IS REMOVED AGAIN: the defect is about a repo whose
        # profile has to be DERIVED from its identity, which is the state every
        # repo the rail reached before the declaration existed is in.
        self.sh(self.src, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.src))
        self.gitdir = os.path.join(self.src, ".git")
        self.assertTrue(os.path.isdir(self.gitdir), "fixture: a real gitdir")

    def test_the_drift_reader_sees_no_drift_in_either_spelling(self):
        """THE PRODUCER THE LAND CLOSE CALLS. RED before the cure: the gitdir
        spelling planned the leak hooks, so the installed rail `pre-commit`
        compared STALE and `--live` was refused."""
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME PRODUCER: the
        # gitdir spelling renders the rail's whole hook set, so the reader below
        # had real installed hooks to compare and an empty finding list means
        # "no drift" rather than "nothing was looked at".
        self.assertEqual([p["name"] for p in _guard._guard_plan(self.gitdir)[1]],
                         [n for n, _t in _guard.GUARD_HOOKS])
        self.assertIn("commit-msg",
                      [p["name"] for p in _guard._guard_plan(self.gitdir)[1]])
        # AND THE CHECKOUT-ROOT SPELLING IS SILENT, so the question below is
        # only about how the identity was spelled.
        self.assertEqual(_guard.stale_guard_hooks(self.src), [])
        self.assertEqual(
            [(state, name) for state, name, _why
             in _guard.stale_guard_hooks(self.gitdir)], [])

    def _linked_worktree(self, name):
        """A real linked worktree of the fixture checkout, and its own gitdir.

        THE RAIL THIS FIXTURE ARMED REFUSES BRANCH CREATION in the shared
        checkout, which is the hook working: adding a room is the integrator's
        own act, and the shipped override is how that is spelled."""
        room = os.path.join(self.tmp, name)
        r = self.sh(self.src, "git", "worktree", "add", "-q", "--detach", room,
                    env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        wt_gitdir = os.path.join(self.gitdir, "worktrees", name)
        self.assertTrue(os.path.isdir(wt_gitdir),
                        "fixture: the linked worktree's own gitdir")
        return room, wt_gitdir

    def test_every_supported_spelling_resolves_to_the_same_profile(self):
        """A checkout root, a linked worktree root, and either one's gitdir."""
        # UNCONDITIONAL FIRST, so nothing here depends on a loop running: the
        # checkout root and its gitdir are the two spellings the defect was
        # about, and they are asserted outside any control flow.
        self.assertEqual(_guard.guard_profile(self.src), "rail")
        self.assertEqual(_guard.default_profile(self.src), "rail")
        self.assertEqual(_guard.guard_profile(self.gitdir), "rail")
        self.assertEqual(_guard.default_profile(self.gitdir), "rail")
        room, wt_gitdir = self._linked_worktree("helm-lane")
        for spelling in (room, wt_gitdir):
            self.assertEqual(_guard.guard_profile(spelling), "rail", spelling)
            self.assertEqual(_guard.default_profile(spelling), "rail", spelling)

    def test_separate_clones_are_not_folded_into_one_repository(self):
        """MUST-HIT CONTROL. Normalization must not reach sideways: a second
        clone's gitdir names the SECOND clone, never the first — the maker
        mistake the tree warning exists for stays two trees. And a PROJECT
        repo's gitdir still answers leak, so the normalization did not start
        calling every identity helm's own source."""
        other = self._helm_source_repo(name="helm-clone-two")
        self.assertEqual(selfrepo.checkout_root(os.path.join(other, ".git")),
                         other)
        self.assertNotEqual(selfrepo.checkout_root(os.path.join(other, ".git")),
                            self.src)
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root))
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, UNCONDITIONAL: the helm
        # checkout's gitdir answers rail one line up, so leak here is this
        # repo's identity and not a reader that stopped resolving.
        self.assertEqual(_guard.guard_profile(self.gitdir), "rail")
        self.assertEqual(_guard.guard_profile(os.path.join(self.root, ".git")),
                         "leak")


class ADeclarationShortCircuitsTheDiskTest(HookBase):
    """FINDING 2 — THE DECLARATION IS AUTHORITATIVE, SO IT MUST SHORT-CIRCUIT.

    The install read the disk unconditionally and only then preferred the
    declaration to it. A declared-leak repo with healthy leak hooks and an
    unrelated UNREADABLE foreign `commit-msg` therefore had its rail-only
    inference open a slot its profile does not own: `_path_snapshot` raised
    PermissionError before the transaction's rollback handler existed, so the
    verb died over a repo it had no reason to look at.

    And when nothing IS declared the disk is the only witness — so a failed
    observation is CLASSIFIED and reported as unknown, never guessed into a
    profile, and nothing is retired on a guess.
    """

    def _foreign_commit_msg(self, mode):
        """A hook that is not helm's, at a name only the rail plans, in the
        mode the arm is about. Returns its path."""
        path = work.hook_path(self.root, "commit-msg")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n# someone else's hook\nexit 0\n")
        # THE REAL SNAPSHOT DECIDES, before the mode closes the file: this
        # hook must be nobody's but the user's, or the arm is about the wrong
        # thing entirely.
        self.assertFalse(_guard._owned_hook(_guard._path_snapshot(path)),
                         "fixture: this hook is nobody's but the user's")
        os.chmod(path, mode)
        return path

    def test_a_declared_reinstall_never_opens_an_unrelated_slot(self):
        """RED before the cure: PermissionError out of the shipped verb."""
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "leak")
        foreign = self._foreign_commit_msg(0o000)
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, "a declared repo's reinstall must not depend "
                               "on a slot its profile does not own: %s" % err)
        # POSITIVE CONTROLS, UNCONDITIONAL: the install really ran and the
        # foreign hook was left exactly as it was.
        self.assertIn("leak guard", out)
        self.assertTrue(os.access(work.hook_path(self.root, "pre-commit"),
                                  os.X_OK))
        self.assertEqual(os.stat(foreign).st_mode & 0o777, 0o000)

    def test_an_unreadable_disk_REFUSES_and_records_nothing(self):  # noqa: VACUOUS_ASSERTION — a refusal's whole claim is that nothing was recorded and nothing was written, which no same-call positive can carry; the unconditional positive is test_a_READABLE_foreign_slot_is_no_obstacle, the same fixture one octal literal away, which installs and declares
        """ROUND-THREE FINDING 1. When nothing is declared the disk is the only
        witness, and an install that cannot read it does not know what it is
        moving this repo OFF. Reporting that in a note and recording the target
        anyway CERTIFIED a profile nobody observed — an unreadable input is not
        a pass, and the leak profile does not get to speak for the rail. RED
        before this cure: rc 0, `leak` recorded, and a note admitting the
        observation had failed."""
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root))
        self._foreign_commit_msg(0o000)
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 1, "an unknown outgoing profile is not a pass: %s"
                                % (out + err))
        # A REFUSAL SPEAKS ON STDERR — the dispatcher routes a non-zero install
        # there, and a refusal an operator's pipe drops is not a refusal.
        self.assertIn("REFUSED", err)
        self.assertIn("UNKNOWN", err)
        self.assertIn("PermissionError", err)
        # NOTHING WAS CERTIFIED AND NOTHING WAS WRITTEN.
        self.assertIsNone(_guard.declared_profile(self.root))
        self.assertFalse(os.path.lexists(work.hook_path(self.root,
                                                        "pre-commit")))
        self.assertNotIn("retired", out + err)

    def test_the_refusal_advises_the_recovery_that_actually_works(self):
        """ROUND-FIVE FINDING 4. The refusal was right and its advice was not:
        it said "Declare it (--profile rail|leak) or make that path readable",
        and `--profile` NAMES THE INCOMING PROFILE. The observation that failed
        is of the profile this repo is moving OFF, which is read whenever the
        CONFIG declares nothing — whatever the flag says — so an operator who
        followed the advice hit the same unreadable path and the same refusal,
        with no reason to think they had been told something untrue.

        This arm is the advice AND the two behaviours behind it: the flag does
        not recover it, and the named path does."""
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root))
        foreign = self._foreign_commit_msg(0o000)
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 1, out + err)
        # THE ADVICE. RED before the cure: the refusal offered --profile as the
        # recovery and said nothing about the retry.
        self.assertIn(foreign, err, "the refusal names the exact slot to fix")
        self.assertIn("MAKE THIS PATH READABLE", err)
        self.assertIn("every retry refuses until it is", err)
        # AND THE ADVICE IS TRUE. The flag the old text offered changes nothing:
        # same rc, same refusal, still nothing declared.
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 1, "--profile names the INCOMING profile; it "
                                "cannot settle the outgoing observation: "
                                "%s" % (out + err))
        self.assertIn("REFUSED", err)
        self.assertIsNone(_guard.declared_profile(self.root))
        self.assertFalse(os.path.lexists(work.hook_path(self.root,
                                                        "pre-commit")))
        # MUST-HIT CONTROL, one chmod from every assertion above: the path the
        # refusal named IS the thing in the way, so making it readable completes
        # the install the advice promised. Blast radius: this arm's own fixture —
        # one mode change on one hook file in a scratch repo.
        os.chmod(foreign, 0o755)
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "leak")
        self.assertTrue(os.access(work.hook_path(self.root, "pre-commit"),
                                  os.X_OK))

    def test_the_synopsis_documents_the_refusal_and_not_a_note(self):
        """ROUND-FIVE FINDING 5. `docs/VERBS.md` still described this as a NOTE
        on a successful install — "the note says the observation failed and the
        install retires nothing" — after the branch made it exit 1 before any
        write. A reader planning an unattended install would have planned for a
        warning line and got a failure.

        The doc is pinned HERE, beside the behaviour, because a synopsis and the
        verb it describes go stale separately: the arms above are what make this
        one more than a string compare."""
        # The same derivation `_helm_source_repo` uses: the package this test is
        # running, then the repo above it.
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(_guard.__file__)))
        doc = os.path.join(os.path.dirname(pkg), "docs", "VERBS.md")
        with open(doc) as f:
            text = f.read()
        self.assertNotIn("the note says the observation failed", text,
                         "the note-only account describes behaviour this verb "
                         "no longer has")
        self.assertIn("the install REFUSES", text)
        # The clauses are matched without crossing a line break: this file is
        # hand-wrapped prose, so a phrase spanning two lines is a false negative
        # about the sentence rather than a finding about the doc.
        self.assertIn("nothing recorded and nothing retired", text)
        self.assertIn("settle the outgoing history", text)

    def test_a_READABLE_foreign_slot_is_no_obstacle(self):
        """MUST-HIT CONTROL, one chmod from the arm above: the same undeclared
        repo with the same foreign hook readable installs and names its identity
        default, so the refusal above is the MODE and not a door that refuses
        every undeclared repo with a foreign hook. Blast radius: this arm and
        the one above share the fixture and differ in one octal literal."""
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self._foreign_commit_msg(0o755)
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("undeclared (leak by default)", out)
        self.assertNotIn("unreadable", out)


class APartlyDismantledRailIsStillARailTest(HookBase):
    """FINDING 3 — EVERY ARTIFACT ONLY A RAIL INSTALL WRITES IS EVIDENCE.

    Keying the installed profile on the three rail-only hook SLOTS alone read a
    partially dismantled rail — those slots gone, the rail's composed
    `pre-commit` still running it and the rail's whole scanner shelf still on
    disk — as leak. An explicit `--profile leak` then computed no departures at
    all: the shelf survived, `drift` and `doctor` kept judging rungs no
    installed hook runs, and the note reported leak as the profile already
    installed.
    """

    def setUp(self):
        super().setUp()
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        self.assertEqual(rc, 0, err)
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root))
        leak_names = {name for name, _t in _guard.GUARD_PROFILES["leak"]}
        self.rail_only = [work.hook_path(self.root, name)
                          for name, _t in _guard.GUARD_PROFILES["rail"]
                          if name not in leak_names]
        self.assertEqual(len(self.rail_only), 3,
                         "fixture: three slots are the rail's alone")
        for path in self.rail_only:
            self.assertTrue(os.access(path, os.X_OK),
                            "fixture: the real installer armed %s" % path)
            os.unlink(path)
        # THE PARTIAL STATE: no rail-only SLOT is left, and the rail's composed
        # pre-commit and its whole shelf are still running.
        for path in self.rail_only:
            self.assertFalse(os.path.lexists(path))
        self.precommit = work.hook_path(self.root, "pre-commit")
        self.assertTrue(os.access(self.precommit, os.X_OK),
                        "fixture: the rail's composed pre-commit still runs")
        self.rail_snapshots = [
            path for path in _guard._scanner_assets(self.root, "rail")
            if path not in _guard._scanner_assets(self.root, "leak")]
        for path in self.rail_snapshots:
            self.assertTrue(os.path.isfile(path),
                            "fixture: the rail's shelf really is there")

    def test_the_disk_still_says_rail(self):
        """RED before the cure: leak, because only the shared slots were owned."""
        self.assertEqual(_guard.installed_profile(self.root), "rail")
        found = _guard.rail_only_artifacts(self.root)
        self.assertIn(self.precommit, found,
                      "the rail's own pre-commit body names its rungs")
        for path in self.rail_snapshots:
            self.assertIn(path, found)

    def test_an_explicit_leak_retires_the_shelf_and_says_what_departed(self):
        """RED before the cure: previous and target were both leak, so nothing
        departed, the shelf stayed and the note named leak as installed."""
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        for path in self.rail_snapshots:
            self.assertFalse(os.path.lexists(path),
                             "%s is a rung the leak hooks never run; note was: "
                             "%s" % (path, out))
        self.assertIn("undeclared (rail installed) → leak", out)
        self.assertIn("scanner snapshot", out)
        self.assertIn("lane_discipline.py", out)
        # POSITIVE CONTROLS, UNCONDITIONAL: the shelf the leak hooks DO run is
        # there and the legs are armed, so the absences are a retirement and
        # not an emptied directory.
        shelf = os.path.dirname(self.rail_snapshots[0])
        self.assertTrue(os.path.isfile(os.path.join(shelf, "nevertrack.py")))
        self.assertTrue(os.path.isfile(os.path.join(shelf,
                                                    "hostpath_guard.py")))
        self.assertTrue(os.access(self.precommit, os.X_OK))
        self.assertEqual(_guard.declared_profile(self.root), "leak")

    def test_a_RECORDED_leak_over_a_RUNNING_rail_needs_the_flag(self):  # noqa: VACUOUS_ASSERTION — a refusal's claim is that nothing was written, which no same-call positive can carry; the unconditional positives are the refusal text on stderr and the SECOND half of this arm, where the flag the refusal named installs (rc 0) and `installed_profile` really does end at leak
        """TASK/2504, ONE GIT CONFIG LINE FROM THE ARM ABOVE, and the same
        disease entered from the other side. A DECLARATION is not a fact about
        the disk: this repo records leak while the rail's composed pre-commit
        and its whole shelf are still executing. Treating the record as the
        only witness made a flagless install here a no-op that left every one
        of those rungs running outside the census — and the only way to reach
        that state is a narrowing nobody typed.

        So the flagless refresh REFUSES and names the flag, and the flag then
        retires what the declaration had been hiding. Both halves are here
        because either alone is a trap: a refusal whose advice does nothing is
        worse than no advice."""
        self.sh(self.root, "git", "config", "--local", _guard.PROFILE_KEY,
                "leak")
        self.assertEqual(_guard.installed_profile(self.root), "rail",
                         "fixture: the disk disagrees with the declaration")
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("REFUSED", err)
        self.assertIn("RUNNING the rail profile", err)
        self.assertIn("install-guard --apply --profile leak", err)
        for path in self.rail_snapshots:
            self.assertTrue(os.path.lexists(path),
                            "a refusal writes nothing; note was: %s" % err)
        # AND THE ADVICE IS TRUE — the same call with the flag the refusal
        # named retires the shelf the declaration was hiding.
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertIn("leak guard", out)
        for path in self.rail_snapshots:
            self.assertFalse(os.path.lexists(path),
                             "%s is a rung the leak hooks never run; note was: "
                             "%s" % (path, out))
        self.assertEqual(_guard.installed_profile(self.root), "leak")


def _hook_tree(root):
    """Every file under the repo's hook dir, snapshots included, as
    relpath -> (kind, bytes, mode) through the transaction's own reader —
    so "unchanged" is a byte-and-mode claim about the whole tree."""
    d = os.path.dirname(work.hook_path(root, "pre-commit"))
    tree = {}
    for base, _dirs, files in os.walk(d):
        for name in files:
            full = os.path.join(base, name)
            tree[os.path.relpath(full, d)] = _guard._path_snapshot(full)
    return tree


REMEDY = re.compile(r"helm work install-guard --apply --profile ([a-z|]+)")
DISAGREES = "but is RUNNING the"


class APrintedRemedyNeverNarrowsTest(HookBase):
    """TASK/2504 ROUND 3 (ledger row 844f54ded002) — THE TOOL'S OWN ADVICE
    SUPPLIED THE FLAG THE REFUSAL ADMITS.

    A repo that DECLARES leak while the rail's hooks are on disk is the
    conflicted state a flagless install now refuses to narrow. But every
    printed remedy — the claim notice, `helm work gc`, `helm doctor` —
    resolved its profile through the declaration first and printed
    `--profile leak`: the explicit flag the refusal deliberately lets through.
    An operator who pasted it retired the rail's three slots and twelve
    scanner snapshots by following the notice, the exact class this task
    exists to end.

    THE RULE: a printed remedy names what the flagless verb would install,
    except where that verb would refuse — there it names the profile the repo
    is RUNNING, says the declaration disagrees, and names the narrowing
    command as the on-purpose door. Following it keeps every rung armed and
    re-records the declaration to match.

    CONTROL, by mutation on the fab (the resolver's narrowing clause made to
    return the declaration, this module run, the mutant restored): the doctor
    and gc arms below rendered `--profile leak` and went red at that line,
    while the SKIPPED-lines arm and the healthy poles stayed green. That an
    explicit `--profile leak` on this state retires the rail's slots and
    shelf is measured by `test_a_RECORDED_leak_over_a_RUNNING_rail_needs_the_flag`
    below, green in the same run — the two together are the pre-cure trap.
    """

    def setUp(self):
        super().setUp()
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        self.assertEqual(rc, 0, err)
        self.rail_snapshots = [
            path for path in _guard._scanner_assets(self.root, "rail")
            if path not in _guard._scanner_assets(self.root, "leak")]
        self.assertEqual(len(self.rail_snapshots), 12,
                         "fixture: twelve snapshots are the rail's alone")
        for path in self.rail_snapshots:
            self.assertTrue(os.path.isfile(path),
                            "fixture: the rail's shelf really is there")

    def declare(self, profile):
        if profile is None:
            self.sh(self.root, "git", "config", "--local", "--unset",
                    _guard.PROFILE_KEY)
        else:
            self.sh(self.root, "git", "config", "--local",
                    _guard.PROFILE_KEY, profile)
        self.assertEqual(_guard.declared_profile(self.root), profile)

    def conflict(self):
        """The trap: leak recorded over a running rail."""
        self.declare("leak")
        self.assertEqual(_guard.installed_profile(self.root), "rail",
                         "fixture: the disk disagrees with the declaration")

    def doctor_row(self):
        rows = doctor.check_work_guard(self.root)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def assert_remedy(self, text, profile, note):
        """The FIRST remedy in `text` names `profile`; the note either follows
        it or is absent; and the leak spelling, when present at all, is only
        ever the named narrowing door."""
        found = REMEDY.findall(text)
        self.assertTrue(found, "no remedy printed: %s" % text)
        self.assertEqual(found[0], profile, text)
        if note:
            self.assertIn(DISAGREES, text)
            self.assertIn("`helm work install-guard --apply --profile leak` "
                          "narrows on purpose", text)
        else:
            self.assertNotIn(DISAGREES, text)
        for tail in re.findall(r"--profile leak(.{0,20})", text):
            self.assertTrue(tail.startswith("` narrows on purpose")
                            or profile == "leak",
                            "a bare narrowing remedy: %s" % text)

    def follow(self, text):
        """RUN the printed command through the shipped verb, as an operator
        pastes it."""
        m = re.search(r"`(helm work install-guard[^`]*)`", text)
        self.assertIsNotNone(m, text)
        argv = shlex.split(m.group(1))
        self.assertEqual(argv[:2], ["helm", "work"], m.group(1))
        rc, out, err = self.cli(*argv[2:])
        self.assertEqual(rc, 0, out + err)
        return out

    def test_doctor_names_the_running_rail_and_following_it_keeps_the_rail(self):  # noqa: VACUOUS_ASSERTION — the two absence-shaped clauses are the bare-leak scan (a loop over matches) and the byte-and-mode 'unchanged' tree; the unconditional positives on the same observables come first and after: the row is WARN, its first remedy is rail, the disagreement text is present, the follow exits 0, all ten rail-only snapshots are asserted present afterwards, the declaration reads rail and the doctor then reads OK
        self.conflict()
        before = _hook_tree(self.root)
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.WARN, text)
        self.assert_remedy(text, "rail", note=True)
        self.assertIn("declares %s=leak" % _guard.PROFILE_KEY, text)
        self.assertIn("re-records the declaration as rail", text)
        out = self.follow(text)
        self.assertEqual(_hook_tree(self.root), before,
                         "the remedy changed the hook tree; note was: %s"
                         % out)
        for path in self.rail_snapshots:
            self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(_guard.declared_profile(self.root), "rail")
        # AND THE REPO IS WHOLE AGAIN ON THE SAME SURFACE: the declaration
        # now describes the hooks that run, so the doctor reads it current.
        level, text = self.doctor_row()
        self.assertEqual(level, doctor.OK, text)

    def test_gc_prints_the_same_remedy_and_note(self):
        self.conflict()
        rc, out, err = self.cli("gc")
        self.assertEqual(rc, 0, out + err)
        stale = [line for line in out.splitlines() if "GUARD-" in line]
        self.assertTrue(stale, "gc printed no drift on a conflicted repo: %s"
                        % (out + err))
        for line in stale:
            self.assert_remedy(line, "rail", note=True)

    def test_rail_hooks_generated_under_a_leak_declaration_name_the_rail(self):
        """The SKIPPED lines inside a generated hook render the profile at
        generation time. A rail hook minted while the repo DECLARES leak —
        exactly what following the remedy does — must still send its reader
        to `--profile rail`: the plan's own profile, never the declaration."""
        self.conflict()
        rc, out, err = self.cli("install-guard", "--profile", "rail")
        self.assertEqual(rc, 0, err)
        rendered = [m for m in REMEDY.findall(out)]
        self.assertGreaterEqual(len(rendered), 10,
                                "the dry run rendered no SKIPPED lines: %s"
                                % out)
        self.assertEqual(set(rendered), {"rail"}, out)
        rc, out, err = self.cli("install-guard", "--apply", "--profile",
                                "rail")
        self.assertEqual(rc, 0, out + err)
        on_disk = []
        for name, _t in _guard.GUARD_PROFILES["rail"]:
            with open(work.hook_path(self.root, name)) as f:
                on_disk += REMEDY.findall(f.read())
        self.assertGreaterEqual(len(on_disk), 10, on_disk)
        self.assertEqual(set(on_disk), {"rail"}, on_disk)

    def test_the_healthy_poles_are_unchanged(self):  # noqa: VACUOUS_ASSERTION — the loop IS the table of four literal poles, not a guard: every iteration asserts WARN, the remedy's profile, an empty note, a follow that exits 0, the installed profile and a doctor row that reads OK
        """Declared rail + rail running, declared leak + leak running, nothing
        declared + rail running: the remedy names that one profile with no
        note. And the WIDENING disagreement — declared rail over running leak
        legs — follows the declaration, as the flagless verb does, because a
        widening needs nobody's permission."""
        nevertrack = next(p for p in _guard._scanner_assets(self.root, "leak")
                          if p.endswith("nevertrack.py"))

        def stale():
            with open(nevertrack, "a") as f:
                f.write("\n# an older copy of the rules\n")

        def declared_rail_running_rail():
            self.declare("rail")
            stale()

        def declared_leak_running_leak():
            rc, _o, err = self.cli("install-guard", "--apply", "--profile",
                                   "leak")
            self.assertEqual(rc, 0, err)
            self.assertEqual(_guard.installed_profile(self.root), "leak")
            stale()

        def undeclared_running_rail():
            self.declare(None)
            stale()

        def declared_rail_running_leak():
            rc, _o, err = self.cli("install-guard", "--apply", "--profile",
                                   "leak")
            self.assertEqual(rc, 0, err)
            self.declare("rail")
            self.assertEqual(_guard.installed_profile(self.root), "leak")

        poles = [(declared_rail_running_rail, "rail"),
                 (declared_leak_running_leak, "leak"),
                 (undeclared_running_rail, "rail"),
                 (declared_rail_running_leak, "rail")]
        for arrange, expected in poles:
            with self.subTest(pole=arrange.__name__):
                # EVERY POLE STARTS FROM THE SAME ARMED RAIL: the pole before
                # it may have left the leak legs installed.
                self.declare("rail")
                rc, _o, err = self.cli("install-guard", "--apply",
                                       "--profile", "rail")
                self.assertEqual(rc, 0, err)
                self.assertEqual(_guard.installed_profile(self.root), "rail")
                arrange()
                level, text = self.doctor_row()
                self.assertEqual(level, doctor.WARN, text)
                self.assert_remedy(text, expected, note=False)
                self.assertEqual(_guard.guard_remedy_note(self.root), "")
                self.follow(text)
                self.assertEqual(_guard.installed_profile(self.root),
                                 expected)
                level, text = self.doctor_row()
                self.assertEqual(level, doctor.OK, text)


class AnUnknownOutgoingProfileNeverCertifiesLeakTest(HookBase):
    """ROUND-THREE FINDING 1 — AN UNREADABLE INPUT IS NEVER A PASS.

    A repo already under the rail and declaring nothing, with ONE unreadable
    foreign hook at a name only the rail plans, was recorded as `leak` by a
    flagless install: the rail-only inference raised from the middle of its
    walk, the caller classified that as "no outgoing profile", nothing was
    departing, and the reference-transaction, post-checkout and commit-msg hooks
    kept executing outside every census under a declaration that named the two
    leak legs.

    TWO ROOTS, and the second is why curing the first alone is not enough:

      * A rail this reader has already SEEN cannot be unseen by a path it could
        not open. The unreadable path is deferred and raised only when the walk
        ends with no rail artifact found — the one case where reading it could
        have changed the answer.
      * And then the transaction has to RETIRE that slot, so it has to be able
        to restore it. A path the rollback record cannot hold refuses before the
        first byte is written, rather than raising out of the verb once the walk
        has already decided what departs.
    """

    def setUp(self):
        super().setUp()
        # THE LEGACY STATE, BUILT BY THE REAL INSTALLER: the rail armed through
        # the shipped CLI, then the declaration removed.
        rc, _out, err = self.cli("install-guard", "--apply", "--profile", "rail")
        self.assertEqual(rc, 0, err)
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.root),
                          "fixture: nothing may be declared here")
        leak_names = {name for name, _t in _guard.GUARD_PROFILES["leak"]}
        self.rail_only = [work.hook_path(self.root, name)
                          for name, _t in _guard.GUARD_PROFILES["rail"]
                          if name not in leak_names]
        self.assertEqual(len(self.rail_only), 3,
                         "fixture: three slots are the rail's alone")
        for path in self.rail_only:
            self.assertTrue(os.access(path, os.X_OK),
                            "fixture: the rail really is armed at %s" % path)
        self.rail_snapshots = [
            path for path in _guard._scanner_assets(self.root, "rail")
            if path not in _guard._scanner_assets(self.root, "leak")]
        for path in self.rail_snapshots:
            self.assertTrue(os.path.isfile(path),
                            "fixture: the rail's shelf really is there")

    def _unreadable_foreign_commit_msg(self):
        """A FOREIGN hook, at a name only the rail plans, mode 000 — the exact
        state the reviewer measured. It replaces the rail's own hook at that
        slot, so the rail's OTHER evidence is what the reader has left."""
        path = work.hook_path(self.root, "commit-msg")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n# someone else's hook\nexit 0\n")
        self.assertFalse(_guard._owned_hook(_guard._path_snapshot(path)),
                         "fixture: this hook is nobody's but the user's")
        os.chmod(path, 0o000)
        return path

    def test_readable_rail_evidence_is_not_discarded_by_an_unreadable_sibling(self):
        """THE READER'S OWN LAYER. RED before the cure: `installed_profile`
        raised, because the walk hit the unreadable slot and threw away the
        reference-transaction and post-checkout hooks it had already seen."""
        self._unreadable_foreign_commit_msg()
        # POSITIVE CONTROL FIRST, UNCONDITIONAL: the rail's other evidence is
        # readable and owned, so "rail" below is that evidence and not a reader
        # that answers rail whenever it cannot read something.
        for path in self.rail_only[:2]:
            self.assertTrue(_guard._owned_hook(_guard._path_snapshot(path)),
                            "%s is readable, owned and the rail's alone" % path)
        self.assertEqual(_guard.installed_profile(self.root), "rail")
        self.assertIn(self.rail_only[0], _guard.rail_only_artifacts(self.root))

    def test_an_unreadable_slot_with_NO_rail_evidence_still_raises(self):
        """MUST-HIT CONTROL for the deferral. If a deferred error were simply
        dropped, a repo with nothing readable to go on would answer `leak` — the
        certification this whole finding is about. The same unreadable slot, with
        every rail artifact removed, must still be UNKNOWN.

        Blast radius: this arm removes the fixture's rail artifacts, so it
        shares the setUp with its sibling and differs only in what is on disk."""
        self._unreadable_foreign_commit_msg()
        for path in self.rail_only[:2] + self.rail_snapshots:
            os.unlink(path)
        os.unlink(work.hook_path(self.root, "pre-commit"))
        with self.assertRaises(OSError):
            _guard.installed_profile(self.root)

    def test_a_flagless_install_REFUSES_and_leaves_the_rail_armed(self):  # noqa: VACUOUS_ASSERTION — "nothing was recorded" and "nothing was retired" are absence claims a refusal cannot pair with in one call; the unconditional positives here are the still-executable rail hook and the intact shelf, and the same-fixture control one chmod away is test_the_same_repo_with_the_slot_READABLE_completes
        """THE SHIPPED PRODUCER. RED before the cure: rc 0, `leak` recorded, and
        all three rail slots plus the whole rail shelf still executing under
        it."""
        foreign = self._unreadable_foreign_commit_msg()
        rc, out, err = self.cli("install-guard", "--apply")
        said = out + err
        self.assertEqual(rc, 1, "an install that cannot restore what it must "
                                "retire is not a transaction: %s" % said)
        # A REFUSAL SPEAKS ON STDERR, and it names the profile it OBSERVED —
        # the whole point being that it observed one rather than guessing.
        self.assertIn("REFUSED", err)
        self.assertIn("rail", err)
        # POSITIVE CONTROL, UNCONDITIONAL AND OUTSIDE EVERY LOOP: the rail's own
        # reference-transaction hook is still executable, so the hook directory
        # was not emptied and the per-slot claims below are about what this
        # refusal left alone.
        self.assertTrue(os.access(self.rail_only[0], os.X_OK))
        # NOTHING WAS CERTIFIED: the rail is still the law, and still armed.
        self.assertIsNone(_guard.declared_profile(self.root))
        for path in self.rail_only[:2]:
            self.assertTrue(os.access(path, os.X_OK),
                            "%s is the rail's and this repo never left it; "
                            "note was: %s" % (path, said))
        for path in self.rail_snapshots:
            self.assertTrue(os.path.isfile(path),
                            "%s is the rail's shelf; note was: %s"
                            % (path, said))
        self.assertEqual(os.stat(foreign).st_mode & 0o777, 0o000,
                         "the user's own hook was not touched either")
        self.assertNotIn("retired", said)

    def test_an_explicit_profile_with_an_UNREADABLE_history_still_REFUSES(self):  # noqa: VACUOUS_ASSERTION — "nothing was recorded" is an absence a refusal cannot pair with in one call; the unconditional positive is the PermissionError the observation really raises on this state, and the same-fixture control is test_the_same_repo_with_the_slot_READABLE_completes one chmod away
        """THE OTHER HALF OF THE ROUND-SIX CURE. An explicit `--profile` says
        which profile this repo is moving TO. It says NOTHING about the profile
        it is moving OFF, which is read from the disk whenever the config
        declares nothing — so an outgoing profile that cannot be observed still
        refuses before any write, flag or no flag. Curing the identity
        derivation for an explicit target must not be read as trusting that flag
        for the history too.

        The state is the one its sibling
        `test_an_unreadable_slot_with_NO_rail_evidence_still_raises` measures at
        the reader: the unreadable slot with every other rail artifact gone, so
        the observation has nothing readable to answer from.

        Blast radius: this arm removes the fixture's rail artifacts, like that
        sibling, and differs from it only in driving the shipped verb with an
        explicit profile."""
        foreign = self._unreadable_foreign_commit_msg()
        for path in self.rail_only[:2] + self.rail_snapshots:
            os.unlink(path)
        os.unlink(work.hook_path(self.root, "pre-commit"))
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE READER THE VERB CALLS:
        # the observation really does raise on this state, so the refusal below
        # is that unreadable history and not some other door.
        with self.assertRaises(OSError):
            _guard.installed_profile(self.root)
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        said = out + err
        self.assertEqual(rc, 1, said)
        self.assertIn("REFUSED", err)
        self.assertIn("UNKNOWN", err, "the profile this repo is RUNNING is what "
                                      "an explicit target says nothing about")
        self.assertIsNone(_guard.declared_profile(self.root))
        self.assertEqual(os.stat(foreign).st_mode & 0o777, 0o000,
                         "the user's own hook was not touched either")

    def test_the_same_repo_with_the_slot_READABLE_completes(self):
        """MUST-HIT CONTROL, one chmod from the arm above. The same repo, the
        same foreign hook, readable: the install observes the rail, retires
        every departing slot and records leak. So the refusal above is the MODE
        of one path and not a door that refuses every legacy rail.

        Blast radius: one octal literal separates this arm from its sibling,
        which drives the same explicit `--profile leak` — the flagless spelling
        would now refuse to NARROW this repo (task/2504), which is a different
        door and would stop measuring the MODE this pair is about."""
        foreign = self._unreadable_foreign_commit_msg()
        os.chmod(foreign, 0o755)
        rc, out, err = self.cli("install-guard", "--apply", "--profile", "leak")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.root), "leak")
        self.assertIn("undeclared (rail installed) → leak", out)
        for path in self.rail_snapshots:
            self.assertFalse(os.path.lexists(path),
                             "%s is a rung the leak hooks never run; note was: "
                             "%s" % (path, out))
        # AND THE USER'S OWN HOOK IS LEFT IN PLACE at a slot the rail planned
        # and leak does not — it was never helm's to retire.
        self.assertTrue(os.path.lexists(foreign))
        self.assertIn("not a helm hook", out)
        # POSITIVE CONTROLS, UNCONDITIONAL: the leak legs really are armed.
        self.assertTrue(os.access(work.hook_path(self.root, "pre-commit"),
                                  os.X_OK))
        self.assertTrue(os.access(work.hook_path(self.root, "pre-push"),
                                  os.X_OK))


class AGitdirSpellingGitItselfResolvesTest(HookBase):
    """ROUND-THREE FINDING 2 — THE IDENTITY IS RESOLVED BY GIT, NOT BY ITS SHAPE.

    A gitdir's shape does not determine its tree, and two supported identities
    do not have the shape a path-shape matcher looks for:

      * A SEPARATE GITDIR (`git clone --separate-git-dir`, which is the shape
        `tests/test_dispatches.py` builds for the production dispatch writer)
        was returned unchanged, so the package root was looked for INSIDE the
        metadata directory. helm's own source answered "project repo" through
        that spelling, `_guard_plan` rendered the leak hook set against an armed
        rail, and a `--live` land close refused over drift that does not exist.
      * A LINKED WORKTREE'S PRIVATE GITDIR named the MAIN checkout, which is a
        different tree with different content.

    Both are proven here against the guard's own readers, which are what a land
    close calls.
    """

    def setUp(self):
        super().setUp()
        self.src = self._helm_source_repo()
        rc, _out, err = self.cli_in(self.src, "install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_guard.declared_profile(self.src), "rail",
                         "fixture: an undeclared helm checkout arms the rail")
        self.sh(self.src, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(self.src),
                          "fixture: the identity default is the subject")

    def test_a_separate_gitdir_of_helms_source_still_plans_the_rail(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped assertions are FIXTURE preconditions: the `git clone` exit status, whose subprocess result carries no positive observable of its own, and the metadata directory holding no package, which is the state the defect misread; every arm after them is an unconditional equality on the shipped planner, the checkout spelling asserted first
        """THE PRODUCER THE LAND CLOSE CALLS, through the identity spelling the
        dispatch store keeps. RED before the cure: the metadata directory holds
        no `helm/__init__.py`, so this planned the leak hooks and the installed
        rail `pre-commit` compared STALE."""
        tree = os.path.join(self.tmp, "sep-source-checkout")
        gitdir = os.path.join(self.tmp, "sep-source-common.git")
        r = self.sh(self.tmp, "git", "clone", "-q", "--separate-git-dir",
                    gitdir, self.src, tree)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(tree, ".git")),
                        "fixture: `.git` is a FILE pointing outside the tree")
        self.assertFalse(
            os.path.exists(os.path.join(gitdir, "helm", "__init__.py")),
            "fixture: the metadata directory carries no package — reading one "
            "there is the defect")
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME PRODUCER: the
        # CHECKOUT spelling of the same repository plans the rail's whole hook
        # set, so a leak plan below would be about the spelling alone.
        self.assertEqual([p["name"] for p in _guard._guard_plan(tree)[1]],
                         [n for n, _t in _guard.GUARD_HOOKS])
        # AND GIT IS GIVEN ITS OWN RECORD OF THE BINDING. A clone leaves
        # `core.worktree` UNSET (MEASURED, git 2.53.0), which makes this
        # identity's checkout unreachable — and round five settled what happens
        # then: UNKNOWN, never a verdict read off the tracked history (see
        # `test_an_UNREACHABLE_gitdir_refuses_rather_than_calling_a_live_rail_stale`).
        # This arm is about the RESOLUTION, so the fixture records the binding
        # git itself uses; with it recorded, `rev-parse --show-toplevel` asked
        # from the gitdir prints the worktree.
        self.assertEqual(selfrepo.repository_state(gitdir), selfrepo.UNKNOWN)
        self.sh(gitdir, "git", "config", "core.worktree", tree)
        self.assertEqual(selfrepo.checkout_root(gitdir), tree)
        # THE ARM.
        self.assertEqual(_guard.default_profile(gitdir), "rail")
        self.assertEqual(_guard.guard_profile(gitdir), "rail")
        self.assertEqual([p["name"] for p in _guard._guard_plan(gitdir)[1]],
                         [n for n, _t in _guard.GUARD_HOOKS])
        self.assertIn("commit-msg",
                      [p["name"] for p in _guard._guard_plan(gitdir)[1]])

    def test_a_separate_gitdir_of_a_PROJECT_repo_still_gets_the_leak_legs(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped assertion is the fixture's `git clone` exit status, and a subprocess result carries no positive observable of its own; the two arms are unconditional equalities on the shipped planner, with helm's own source answering rail on the same reader first
        """MUST-HIT CONTROL. Resolving the separate-gitdir spelling must not have
        started calling every identity helm's own source: an adopter's repo
        cloned exactly the same way keeps the two legs every repo owes.

        Blast radius: this arm clones `self.root` (the project fixture) where
        its sibling clones `self.src` (the helm fixture); one argument apart.
        Both record `core.worktree`, git's own binding of a gitdir to its tree,
        because an identity that records none is UNKNOWN and answers neither
        profile — which is a different arm."""
        tree = os.path.join(self.tmp, "sep-adopter-checkout")
        gitdir = os.path.join(self.tmp, "sep-adopter-common.git")
        r = self.sh(self.tmp, "git", "clone", "-q", "--separate-git-dir",
                    gitdir, self.root, tree)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.sh(gitdir, "git", "config", "core.worktree", tree)
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME READER: helm's own
        # source answers rail, so leak below is this repository's content.
        self.assertEqual(_guard.default_profile(self.src), "rail")
        self.assertEqual(_guard.default_profile(gitdir), "leak")
        self.assertEqual([p["name"] for p in _guard._guard_plan(gitdir)[1]],
                         [n for n, _t in _guard.GUARD_PROFILES["leak"]])

    def test_a_linked_worktrees_private_gitdir_answers_for_the_LANE(self):  # noqa: VACUOUS_ASSERTION — the absence-shaped assertions are FIXTURE preconditions: the `git worktree add` exit status, whose subprocess result carries no positive observable of its own, and the main checkout no longer holding the package, which is what makes WHICH tree answered observable; the arms are unconditional equalities with the main-gitdir control asserted first
        """RED before the cure: the private gitdir resolved to the MAIN
        checkout. This fixture makes the two trees disagree in the one file the
        predicate reads — a populated lane beside a main checkout whose package
        has been moved away — so which tree answered is observable."""
        room = os.path.join(self.tmp, "helm-lane")
        r = self.sh(self.src, "git", "worktree", "add", "-q", "--detach", room,
                    env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        wt_gitdir = os.path.join(self.src, ".git", "worktrees",
                                 os.path.basename(room))
        self.assertTrue(os.path.isdir(wt_gitdir),
                        "fixture: git worktree add made a private gitdir")
        shutil.move(os.path.join(self.src, "helm"),
                    os.path.join(self.tmp, "moved-package"))
        self.assertTrue(
            os.path.isfile(os.path.join(room, "helm", "__init__.py")),
            "fixture: the LANE holds the package root")
        self.assertFalse(os.path.exists(os.path.join(self.src, "helm")),
                         "fixture: the main checkout no longer does")
        # POSITIVE CONTROL FIRST, UNCONDITIONAL: the main checkout's own gitdir
        # answers about the main checkout, whose package is gone — so the answer
        # below is which tree the identity denotes.
        self.assertEqual(_guard.default_profile(os.path.join(self.src, ".git")),
                         "leak")
        # THE ARM.
        self.assertEqual(selfrepo.checkout_root(wt_gitdir), room)
        self.assertEqual(_guard.default_profile(wt_gitdir), "rail")
        self.assertEqual(_guard.default_profile(room), "rail")

    def test_an_UNREACHABLE_gitdir_refuses_rather_than_calling_a_live_rail_stale(self):  # noqa: VACUOUS_ASSERTION — the zero-STALE claim IS this finding and no same-call positive can carry it; the unconditional positives on the same readers are the non-empty ('UNKNOWN', 'guard-plan') row, the rc-1 refusal naming --profile, and the declared-profile control that makes the SAME gitdir spelling compare all five hooks and then report STALE on one mode change
        """ROUND-FIVE FINDING 1, AT THE READER A LAND CLOSE CALLS. Answering an
        identity that cannot be resolved with what the repository TRACKS
        (`cat-file -e HEAD:helm/__init__.py`) answers a different question, and
        this fixture is one where the two answers differ: the rail is ARMED and
        CURRENT here, nothing is declared, and HEAD carries no package root
        while the working tree does. The tracked reading therefore says `leak`,
        `_guard_plan` renders the leak hook set against the live rail, and
        `stale_guard_hooks` reports a STALE `pre-commit` — the exact drift a
        `--live` close refuses on (`landreq._close_repo` filters STALE from this
        very list).

        MEASURED (git 2.53.0): `git init --separate-git-dir` MOVES the metadata
        out of the tree, hooks and all, and leaves `core.worktree` UNSET, so
        nothing in the gitdir names its checkout. There is no tree to classify,
        so the identity answers UNKNOWN and the guard refuses to derive a law
        from it — while NOTHING IS ARMED. Once a profile is installed the
        identity stops being the last witness (task/2504): the disk carries the
        answer, `guard_profile` reads it there, and the live rail is compared
        instead of being reported as a can't-tell. Both poles are driven here,
        in that order, because the fixture passes through the fresh one.

        RED before the cure, MEASURED against the reviewed tip's own body:
        `other` / `leak` / `[('STALE', 'pre-commit')]`.

        The producers are called directly because the `helm work` verb layer
        cannot address this identity at all — `automap._git_root` requires a
        common dir named `.git` — while `landreq` hands a store's `repo_id`
        straight to `stale_guard_hooks`, which is this path."""
        # A REPO OF THIS ARM'S OWN, because the ORDER is load-bearing: the
        # metadata is moved and HEAD is changed while NO hook is armed, and the
        # rail is installed last. Moving a gitdir after an install relocates
        # every path the installed hooks name, which is real drift and would
        # have made this fixture stale for an honest reason.
        root = self._helm_source_repo(name="unreachable-src")
        gitdir = os.path.join(self.tmp, "moved-common.git")
        r = self.sh(root, "git", "init", "-q", "--separate-git-dir", gitdir, ".")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(root, ".git")),
                        "fixture: `.git` is now a FILE naming the gitdir")
        self.assertNotEqual(
            self.sh(gitdir, "git", "config", "--get",
                    "core.worktree").returncode, 0,
            "fixture: git records NO working tree for this gitdir")
        # HEAD DISAGREES WITH THE WORKING TREE about the one file every one of
        # these readers looks at — the state the tracked reading got wrong.
        self.sh(root, "git", "rm", "-q", "--cached", "helm/__init__.py")
        r = self.sh(root, "git", "commit", "-qm", "package root out of the index")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(
            self.sh(root, "git", "cat-file", "-e",
                    "HEAD:helm/__init__.py").returncode, 0,
            "fixture: the commit no longer carries the package root")
        self.assertTrue(os.path.isfile(os.path.join(root, "helm",
                                                    "__init__.py")),
                        "fixture: the WORKING TREE still does")
        # THE FRESH POLE, WHILE NOTHING IS ARMED YET — the one state where the
        # identity is still the only witness, and the state task/2504's resolver
        # leaves entirely to it. Nothing is declared and nothing is installed,
        # so no profile follows from this repo at all and the install REFUSES
        # before any write rather than recording a law it had to invent.
        self.assertIsNone(_guard.declared_profile(gitdir))
        self.assertIsNone(_guard.installed_profile(gitdir),
                          "fixture: no hook is armed yet, so the identity is "
                          "the only witness left")
        self.assertRaises(_guard.UnknownRepositoryIdentity,
                          _guard.guard_profile, gitdir)
        rc, lines = _guard.install_guard(gitdir, apply=True)
        self.assertEqual(rc, 1, "".join(lines))
        self.assertIn("REFUSED", lines[0])
        self.assertIn("names no checkout", lines[0])
        self.assertIn("--profile", lines[0], "the refusal names the door that "
                                             "DOES settle this")
        self.assertIsNone(_guard.declared_profile(gitdir),
                          "nothing may be recorded by a refusal")
        # `install_guard` returns (rc, lines) — the same two-tuple the refusal
        # arm above unpacks; there is no third stream to take.
        rc, lines = _guard.install_guard(root, apply=True)
        self.assertEqual(rc, 0, "".join(lines))
        self.assertEqual(_guard.declared_profile(root), "rail",
                         "fixture: the identity default armed the rail")
        self.sh(root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(root),
                          "fixture: the identity default is the subject")
        # POSITIVE CONTROLS FIRST, UNCONDITIONAL, ON THE SAME PRODUCERS: through
        # the CHECKOUT this repo is helm's source, the rail it has armed is
        # current, and there is no drift. So the arm below is about an identity
        # that cannot reach a tree — not about a repo that is really stale.
        self.assertEqual(_guard.default_profile(root), "rail")
        self.assertEqual(_guard.stale_guard_hooks(root), [])
        # THE ARM.
        self.assertEqual(selfrepo.repository_state(gitdir), selfrepo.UNKNOWN)
        self.assertRaises(_guard.UnknownRepositoryIdentity,
                          _guard.default_profile, gitdir)
        # AND THE DISK IS ASKED BEFORE THE IDENTITY (task/2504), so an identity
        # nobody can resolve stops mattering the moment a profile is RUNNING:
        # this reader answers `rail` off the hook set rather than refusing, and
        # the live rail is COMPARED. It previously refused here and the drift
        # reader reported UNKNOWN — better than STALE, which is what the
        # identity default produced before that, and still a can't-tell about
        # hooks that were readable and current all along. The property this arm
        # defends is unchanged and now holds by measurement rather than by
        # abstention: a live rail never reads STALE.
        self.assertEqual(_guard.guard_profile(gitdir), "rail")
        self.assertEqual(_guard.installed_profile(gitdir), "rail")
        drift = _guard.stale_guard_hooks(gitdir)
        self.assertEqual(drift, [],
                         "the rail this fixture armed is current, and the "
                         "gitdir spelling reads the same hooks the checkout "
                         "spelling does")
        # AND THE FLAGLESS INSTALL KEEPS IT THERE, inventing nothing: the
        # identity was never consulted because the repo had already answered.
        rc, lines = _guard.install_guard(gitdir, apply=True)
        self.assertEqual(rc, 0, "".join(lines))
        self.assertEqual(_guard.declared_profile(root), "rail")
        self.assertIn("undeclared (rail installed) → rail", "".join(lines))
        self.sh(root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        # MUST-HIT CONTROL ON THE SAME READER AND THE SAME IDENTITY: zero STALE
        # must mean "the classification refused", not "this reader cannot see
        # this repo at all". Record the declaration — the one thing that makes
        # the identity question unnecessary — and the SAME gitdir spelling
        # compares every hook and finds them current; then break one hook's
        # executable bit, which this reader treats as identity, and the same
        # call reports it. Blast radius: one config key and one chmod at the end
        # of this arm's own scratch repo.
        self.sh(root, "git", "config", "--local", _guard.PROFILE_KEY, "rail")
        self.assertEqual(_guard.declared_profile(gitdir), "rail")
        self.assertEqual(_guard.stale_guard_hooks(gitdir), [])
        self.assertEqual(len(_guard._guard_plan(gitdir)[1]),
                         len(_guard.GUARD_HOOKS))
        os.chmod(work.hook_path(root, "pre-commit"), 0o644)
        self.assertIn(("STALE", "pre-commit"),
                      [(s, n) for s, n, _w in _guard.stale_guard_hooks(gitdir)])

    def test_an_EXPLICIT_profile_installs_where_the_identity_names_no_checkout(self):  # noqa: VACUOUS_ASSERTION — the fixture preconditions are absence-shaped (git records no core.worktree, nothing is declared yet) and a subprocess exit status carries no positive observable of its own; every arm after them is an unconditional equality on the shipped installer — rc 0, the recorded declaration, both leak hooks owned and executable — and the same readers answer positively on the same identity (installed_profile leak, checked_declaration leak after the write)
        """ROUND-SIX FINDING. The refusal the arm above asserts NAMES `--profile`
        as the door that settles an unresolvable identity, and `docs/VERBS.md`
        promises the same thing — so passing that flag has to get through.

        RED before the cure, on this fixture: `install_guard(gitdir,
        apply=True, profile="leak")` returned rc 1 with the UNKNOWN-identity
        refusal. The explicit profile passed the entry check, the plan and the
        scope, and then the re-read of the declaration UNDER THE LOCK asked the
        COMBINED reader, which derives a default for an undeclared repo — and a
        default is a question about the TARGET this install had already been
        told. So the advertised recovery refused exactly like the flagless call,
        and nothing the operator could type recovered the state.

        The history this repo is LEAVING is still observed and still has to be
        readable: the sibling arm
        `test_an_explicit_profile_with_an_UNREADABLE_history_still_REFUSES`
        holds that leg, and this one is the readable case.

        `install_guard` is called directly for the reason the arm above states:
        no verb layer can address this identity at all."""
        root = os.path.join(self.tmp, "unknown-identity-project")
        os.makedirs(os.path.join(root, "src"))
        with open(os.path.join(root, "src", "a.py"), "w") as f:
            f.write("x = 1\n")
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(self.sh(root, *cmd).returncode, 0)
        self.sh(root, "git", "add", "-A")
        r = self.commit("seed", cwd=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        gitdir = os.path.join(self.tmp, "unknown-identity-common.git")
        r = self.sh(root, "git", "init", "-q", "--separate-git-dir", gitdir, ".")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(root, ".git")),
                        "fixture: `.git` is now a FILE naming the gitdir")
        self.assertNotEqual(
            self.sh(gitdir, "git", "config", "--get",
                    "core.worktree").returncode, 0,
            "fixture: git records NO working tree for this gitdir")
        # A READABLE HISTORY, ARMED BY THE SHIPPED INSTALLER through the
        # spelling that CAN be resolved, then undeclared — so the install under
        # test has a real outgoing profile to observe and retire nothing from.
        rc, lines = _guard.install_guard(root, apply=True, profile="leak")
        self.assertEqual(rc, 0, "".join(lines))
        self.sh(root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(gitdir),
                          "fixture: nothing is declared — the identity default "
                          "is what the install would otherwise consult")
        # POSITIVE CONTROLS FIRST, UNCONDITIONAL, ON THE SAME READERS: the
        # outgoing history IS readable through this very spelling, and the
        # identity IS unresolvable through it. So the rc below is about the
        # explicit flag and not about a repo that happens to be ordinary.
        self.assertEqual(_guard.installed_profile(gitdir), "leak")
        self.assertEqual(selfrepo.repository_state(gitdir), selfrepo.UNKNOWN)
        # MUST-HIT CONTROL: the DERIVATION the pre-cure line performed under the
        # lock still raises on this identity, so the arm below would have been
        # red without the cure — the cure is that an explicit target never asks
        # this question. It is asked of `default_profile` because that IS the
        # derivation; `guard_profile` reaches it only for a repo with nothing
        # declared and nothing installed, and this fixture is RUNNING leak
        # (task/2504), so the combined reader answers off the disk here.
        # Blast radius: two read-only calls on this arm's own scratch identity.
        self.assertRaises(_guard.UnknownRepositoryIdentity,
                          _guard.default_profile, gitdir)
        self.assertEqual(_guard.guard_profile(gitdir), "leak")
        self.assertIsNone(_guard.checked_declaration(gitdir),
                          "the validation half answers without deriving")
        # THE ARM: the advertised recovery COMPLETES, and it is a real install —
        # both leak legs executable and helm's own, and the declaration recorded.
        rc, lines = _guard.install_guard(gitdir, apply=True, profile="leak")
        self.assertEqual(rc, 0, "".join(lines))
        self.assertEqual(_guard.declared_profile(gitdir), "leak")
        # POSITIVE CONTROL ON THE ABSENCE-SHAPED OBSERVABLE ABOVE: the same
        # reader that answered None before the install answers `leak` after it,
        # so the None was "nothing declared" and not a reader that answers None
        # about this identity whatever is recorded.
        self.assertEqual(_guard.checked_declaration(gitdir), "leak")
        for name, _t in _guard.GUARD_PROFILES["leak"]:
            path = work.hook_path(gitdir, name)
            self.assertTrue(os.access(path, os.X_OK), path)
            self.assertTrue(_guard._owned_hook(_guard._path_snapshot(path)),
                            "%s is helm's own hook" % path)
        # AND THE FLAGLESS CALL ON THE SAME REPO NOW COMPLETES, on the profile
        # this repo is RUNNING — the identity is unresolvable and no longer has
        # to be resolved, because the disk already answered (task/2504). It
        # refused here while the identity was the only witness consulted; the
        # fresh pole of that refusal — nothing declared AND nothing installed —
        # is driven by the arm above, which is where it belongs.
        self.sh(gitdir, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        self.assertIsNone(_guard.declared_profile(gitdir))
        rc, lines = _guard.install_guard(gitdir, apply=True)
        self.assertEqual(rc, 0, "".join(lines))
        self.assertEqual(_guard.declared_profile(gitdir), "leak")
        self.assertIn("undeclared (leak installed) → leak", "".join(lines))

    def test_a_lane_under_a_nonUTF8_ancestor_has_its_hooks_COMPARED(self):  # noqa: VACUOUS_ASSERTION — an empty drift list is the claim (compared, not UNKNOWN); its unconditional positives on the same identity are the SOURCE state and the rail default, and the chmod control makes the same call report STALE
        """ROUND-FIVE FINDING 2. A linked worktree's private gitdir holds the
        path of that worktree's `.git` FILE, and a path is BYTES: one ancestor
        directory carrying a non-UTF8 byte is enough, and the lane basename
        beside it is ordinary. Read in TEXT mode that pointer raised
        UnicodeDecodeError — which is NOT an OSError, so the catch around it
        missed it — out of a classifier that must never raise, and the guard's
        drift reader reported UNKNOWN for a repo whose hooks it could have read
        byte for byte.

        MEASURED (git 2.53.0): `git worktree add` under an ancestor holding byte
        0xff writes that byte into `<private gitdir>/gitdir`, and a text-mode
        read of it raises. RED before the cure, measured against the reviewed
        tip's own body: `[('UNKNOWN', 'guard-plan')]` with
        `UnicodeDecodeError` as the reason. The rail this class's setUp armed on
        `self.src`, undeclared, is the fixture."""
        ancestor = os.fsdecode(os.fsencode(self.tmp) + b"/anc-\xff")
        os.makedirs(ancestor)
        room = os.path.join(ancestor, "helm-lane")
        r = self.sh(self.src, "git", "worktree", "add", "-q", "--detach", room,
                    env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        wt_gitdir = os.path.join(self.src, ".git", "worktrees", "helm-lane")
        with open(os.path.join(wt_gitdir, "gitdir"), "rb") as f:
            pointer = f.read()
        self.assertIn(b"\xff", pointer,
                      "fixture: git really wrote the non-UTF8 byte into the "
                      "pointer this reader opens")
        self.assertRaises(UnicodeDecodeError, pointer.decode, "utf-8")
        # POSITIVE CONTROL FIRST, UNCONDITIONAL, ON THE SAME PRODUCERS: the main
        # checkout's own spelling resolves and compares clean, so the arm below
        # is about the pointer's bytes and not about a fixture with real drift.
        self.assertEqual(_guard.default_profile(self.src), "rail")
        self.assertEqual(_guard.stale_guard_hooks(self.src), [])
        # THE ARM: the identity resolves to the LANE and its hooks are COMPARED.
        self.assertEqual(selfrepo.checkout_root(wt_gitdir), room)
        self.assertEqual(selfrepo.repository_state(wt_gitdir), selfrepo.SOURCE)
        self.assertEqual(_guard.default_profile(wt_gitdir), "rail")
        self.assertEqual(_guard.stale_guard_hooks(wt_gitdir), [])
        # MUST-HIT CONTROL ON THE SAME CALL THROUGH THE SAME 0xff IDENTITY: an
        # empty finding list must mean "compared and identical", not "returned
        # before looking". Drop the installed pre-commit's executable bit — git
        # will not run it, which this reader treats as part of the hook's
        # identity — and the same call reports it. Blast radius: one chmod on one
        # hook in this class's scratch checkout, at the end of this arm.
        os.chmod(work.hook_path(self.src, "pre-commit"), 0o644)
        self.assertIn(("STALE", "pre-commit"),
                      [(s, n) for s, n, _w
                       in _guard.stale_guard_hooks(wt_gitdir)])


class TheParentQueryCrossesTheGitPathBoundaryAsBYTESTest(HookBase):
    """A FILESYSTEM PATHNAME IS NOT REQUIRED TO BE UTF-8, AND commit_parents
    ASKS GIT FOR ONE.

    `rev-parse --git-path MERGE_HEAD` answers with a path, and for a LINKED
    WORKTREE that path is absolute and lives under the COMMON git directory —
    so one ancestor directory of the common gitdir carrying a raw 0xff byte is
    enough, while the lane's own root, its staged filenames and its staged
    bytes stay pure ASCII. Read through the helper's default text mode that
    answer raised UnicodeDecodeError INSIDE commit_parents, before the
    existence check, on an ordinary single-parent commit with no MERGE_HEAD at
    all. UnicodeDecodeError is a ValueError, so every consumer that guards
    itself against RuntimeError/TimeoutExpired/OSError — helm/conflict_marker's
    `--staged` rung is the supported one — died with a traceback on a clean
    ASCII staged file that the pre-merge scanner read byte for byte, and
    helm/splitbudget swallowed the same failure as UNMEASURED and returned
    success, dropping its inherited-debt decision.

    MEASURED (git 2.53.0) on this fixture: the query returns
    `/.../anc-\xff/main/.git/worktrees/lane/MERGE_HEAD`. The cure is at the
    boundary — that one query runs in binary, the pathname is joined to an
    os.fsencode-d root, MERGE_HEAD is read in binary, and only the oids are
    decoded — so no decoding failure can leave the helper for any consumer to
    catch or miss.
    """

    #: The rail fixture's reference-transaction guard names this door for
    #: integrator work; branch creation in a shared checkout goes through it.
    INTEGRATOR = {"HELM_WORK_INTEGRATOR": "1"}

    def lane_under(self, ancestor_suffix, tag):
        """A repo whose COMMON git directory sits under `ancestor_suffix`, with
        an ASCII-named linked worktree on branch `lane`. The two calls below
        differ in that suffix and in NOTHING else — same worktree basename,
        same staged paths, same staged bytes."""
        anc = os.fsdecode(os.fsencode(self.tmp) + b"/anc-" + ancestor_suffix)
        os.makedirs(anc)
        main = os.path.join(anc, "main")
        os.makedirs(main)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t"),
                    ("git", "config", "--local", "helm.guard.profile",
                     "rail")):
            self.assertEqual(self.sh(main, *cmd).returncode, 0)
        self.stage("README", "seed\n", cwd=main)
        self.assertEqual(self.commit("seed", cwd=main).returncode, 0)
        # THE LANE IS NAMED FROM THE ASCII `tag`, NEVER from the ancestor
        # bytes: os.fsdecode of a raw 0xff yields a surrogate, which would put
        # non-ASCII in the WORKTREE path and destroy the one-variable design.
        lane = os.path.join(self.tmp, "lane-" + tag)
        r = self.sh(main, "git", "worktree", "add", "-q", "-b", "lane", lane,
                    env=self.INTEGRATOR)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(lane.isascii(),
                        "fixture: the WORKTREE must stay ASCII so the only "
                        "non-ASCII thing in play is the common gitdir")
        return main, lane

    def git_path_bytes(self, lane):
        """The shipped producer's own answer to the query under test, in the
        currency the cure gives it."""
        rc, out, _err = nevertrack._git(lane, "rev-parse", "--git-path",
                                        "MERGE_HEAD", binary=True)
        self.assertEqual(rc, 0)
        return out

    def merge_the_trunk(self, main, lane):
        """A real two-parent index, left staged-but-uncommitted — the state a
        pre-commit rung is actually asked about."""
        self.stage("lane/own.txt", "the lane's own work\n", cwd=lane)
        self.assertEqual(self.commit("lane work", cwd=lane).returncode, 0)
        self.stage("docs/trunk.txt", "the trunk's own work\n", cwd=main)
        self.assertEqual(self.commit("trunk work", cwd=main).returncode, 0)
        r = self.sh(lane, "git", "merge", "--no-commit", "--no-ff", "main",
                    env=self.INTEGRATOR)
        # THE FIXTURE READS MERGE_HEAD THROUGH THE BYTES PATH TOO. Asking
        # `--absolute-git-dir` through a text-mode helper decodes the very
        # pathname this arm made undecodable, so the fixture would die of the
        # defect before the producer under test was ever called.
        with open(self.git_path_bytes(lane).rstrip(b"\n"), "rb") as f:
            heads = [h.decode("ascii") for h in f.read().split()]
        self.assertEqual(len(heads), 1,
                         "the fixture left no merge in progress, so this arm "
                         "is about an ordinary commit: %s"
                         % (r.stdout + r.stderr))
        return heads[0]

    def test_the_no_MERGE_HEAD_state_answers_the_same_under_either_ancestor(self):  # noqa: VACUOUS_ASSERTION — the only absence-shaped reading is the ASCII twin's pathname holding no 0xff, and its unconditional positive on the same producer is asserted first: the 0xff byte IS in the raw lane's answer; every arm after it is an unconditional equality on commit_parents itself
        """THE ORDINARY COMMIT — no merge anywhere — is where this defect bit,
        because the pathname is resolved BEFORE anything asks whether the file
        exists. The ASCII ancestor is the control: same call, same fixture, one
        byte of difference in a directory name nobody in the lane can see."""
        _ascii_main, ascii_lane = self.lane_under(b"ascii", "ascii")
        _raw_main, raw_lane = self.lane_under(b"\xff", "raw")
        # FIXTURE PRECONDITION, ASKED OF GIT RATHER THAN BELIEVED: the query
        # really does answer with the 0xff byte here and really does not there.
        raw = self.git_path_bytes(raw_lane)
        self.assertIn(b"\xff", raw,
                      "git did not put the non-UTF8 byte in the answer, so "
                      "this arm is not about the defect: %r" % raw)
        self.assertNotIn(b"\xff", self.git_path_bytes(ascii_lane))
        # MUST-HIT CONTROL, DRIVING THE SHIPPED PRODUCER WITH THE PRE-CURE
        # CURRENCY: the same argv through _git's DEFAULT text mode — which is
        # exactly what commit_parents did before the cure — raises on this
        # fixture, so the two readings below would have been a traceback and
        # not a wrong answer. Blast radius: one read-only `git rev-parse` in
        # this arm's own scratch repo; it changes no state.
        self.assertRaises(UnicodeDecodeError, nevertrack._git, raw_lane,
                          "rev-parse", "--git-path", "MERGE_HEAD")
        # THE ARMS: both answer HEAD alone, and they answer the SAME thing.
        ascii_parents = nevertrack.commit_parents(ascii_lane)
        raw_parents = nevertrack.commit_parents(raw_lane)
        self.assertEqual(ascii_parents, [self.head(cwd=ascii_lane)])
        self.assertEqual(raw_parents, [self.head(cwd=raw_lane)])
        self.assertEqual(len(raw_parents), 1)

    def test_a_real_two_parent_merge_is_read_through_the_same_boundary(self):
        """THE MUST-HIT: bytes-in/bytes-out must still OPEN the file it names.
        An arm that only proved "no traceback" would be satisfied by a helper
        that had stopped reading MERGE_HEAD at all, which is the over-blocking
        behaviour this lane exists to remove."""
        main, lane = self.lane_under(b"\xff", "raw")
        other = self.merge_the_trunk(main, lane)
        self.assertIn(b"\xff", self.git_path_bytes(lane))
        parents = nevertrack.commit_parents(lane)
        self.assertEqual(parents, [self.head(cwd=lane), other],
                         "the second parent was not read out of a MERGE_HEAD "
                         "whose pathname carries a non-UTF8 byte")
        # POSITIVE CONTROL ON THE SAME PRODUCER, SAME FIXTURE SHAPE, ASCII
        # ancestor: two parents there too, so the reading above is about the
        # merge and not about a helper that appends an oid whatever it reads.
        a_main, a_lane = self.lane_under(b"ascii", "ascii")
        a_other = self.merge_the_trunk(a_main, a_lane)
        self.assertEqual(nevertrack.commit_parents(a_lane),
                         [self.head(cwd=a_lane), a_other])


class MergeCommitMeetsTheLeakGuardTest(HookBase):
    """A MERGE COMMIT NEVER REACHES pre-commit, so the leak legs owe a
    pre-merge-commit hook.

    Git builds the commit of a merge it completes itself (`git merge --no-ff`)
    through pre-merge-commit. A repo armed at pre-commit and pre-push only let
    a --no-ff merge land a branch's blobs with the never-track scanner never
    invoked. Every arm installs through the shipped `helm work install-guard`
    verb and drives a real `git merge` in a scratch repo; no hook is written
    by hand.
    """

    PROFILE = "leak"
    BLOB = b"\x00helm-merge-arm\x00" * (2 * 1024 * 1024 // 16 + 1)   # > 2 MiB
    #: The rail's reference-transaction guard refuses a branch created in a
    #: shared checkout and names this override; the leak profile has no such
    #: hook, so passing it everywhere keeps one set of helpers for both.
    ENV = {"HELM_WORK_INTEGRATOR": "1"}

    def setUp(self):
        super().setUp()
        rc, _out, err = self.cli("install-guard", "--apply", "--profile",
                                 self.PROFILE)
        self.assertEqual(rc, 0, err)

    def test_install_guard_writes_an_executable_pre_merge_commit_hook(self):
        hook = _guard.hook_path(self.root, "pre-merge-commit")
        self.assertTrue(os.access(hook, os.X_OK),
                        "install-guard wrote no pre-merge-commit")
        self.assertTrue(_guard._owned_hook(_guard._path_snapshot(hook)))

    def git(self, *args, env=None):
        return self.sh(self.root, "git", *args,
                       env=dict(self.ENV, **(env or {})))

    def branch_with(self, name, files, env=None):
        """Commit `files` (rel -> bytes) on a new branch cut from main, with
        the commit door's skips when `env` names them, and return to main."""
        r = self.git("checkout", "-q", "-b", name, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel, blob in files.items():
            self.stage_bytes(rel, blob)
        r = self.git("commit", "-q", "-m", name, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = self.head()
        r = self.git("checkout", "-q", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        return tip

    def blob_branch(self):
        tip = self.branch_with("bulk", {"data/blob.bin": self.BLOB},
                               env={"HELM_NEVER_TRACK_SKIP": "1"})
        size = self.git("cat-file", "-s", "bulk:data/blob.bin").stdout.strip()
        self.assertGreaterEqual(int(size or 0), 2 * 1024 * 1024,
                                "fixture: the branch does not carry a 2 MiB "
                                "blob, so no arm here is about one")
        return tip

    def assert_refused_blob_merge(self, before):
        r = self.git("merge", "--no-ff", "-m", "merge bulk", "bulk")
        self.assertNotEqual(r.returncode, 0,
                            "a --no-ff merge landed a 2 MiB blob: %s"
                            % (r.stdout + r.stderr))
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("data/blob.bin", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertIn("[helm merge]", r.stderr)
        self.assertEqual(self.head(), before, "HEAD moved on a refused merge")
        self.assertNotEqual(
            self.git("cat-file", "-e", "HEAD:data/blob.bin").returncode, 0,
            "the blob is in HEAD's tree")
        return r

    def test_a_no_ff_merge_of_a_branch_carrying_a_2_MiB_blob_is_refused(self): # noqa: VACUOUS_ASSERTION — assert_refused_blob_merge asserts a non-zero exit, REFUSED, the blob's path and ADDED BY THIS COMMIT on the same stderr before the unmoved HEAD and the blob's absence from HEAD's tree
        self.blob_branch()
        self.assert_refused_blob_merge(self.head())

    def test_CONTROL_the_same_branch_as_a_fast_forward_is_admitted(self): # noqa: VACUOUS_ASSERTION — the absent [helm merge] line follows an asserted exit 0 and HEAD equal to the branch tip
        """What this control proves, exactly: a fast-forward creates NO commit,
        so no commit hook runs and git moves main to the branch tip. The blob
        entered history on the branch commit, through the commit door's skip
        above; the merge added nothing, and admitting it is not a verdict
        about the blob."""
        tip = self.blob_branch()
        r = self.git("merge", "--ff-only", "bulk")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(), tip, "the fast-forward did not move main")
        self.assertNotIn("[helm merge]", r.stderr)

    def test_CONTROL_a_no_ff_merge_of_only_small_files_is_admitted(self): # noqa: VACUOUS_ASSERTION — every reading is a positive: exit 0 and a HEAD whose parents are exactly the old main and the branch tip
        tip = self.branch_with("small", {"docs/a.md": b"a\n",
                                         "src/b.py": b"print('b')\n"})
        before = self.head()
        r = self.git("merge", "--no-ff", "-m", "merge small", "small")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        parents = self.git("rev-list", "--parents", "-n", "1",
                           "HEAD").stdout.split()
        self.assertEqual(parents[1:], [before, tip],
                         "no merge commit was made, so nothing here met the "
                         "pre-merge-commit hook")

    def test_a_conflict_marker_brought_in_by_a_merge_is_refused(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD follows an asserted non-zero exit, REFUSED and the path on the same stderr
        self.branch_with("markers", {"docs/notes.md":
                                     b"intro\n<<<<<<< ours\nbody\n"},
                         env={"HELM_CONFLICT_MARKER_SKIP": "1"})
        before = self.head()
        r = self.git("merge", "--no-ff", "-m", "merge markers", "markers")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)
        self.assertIn("docs/notes.md", r.stderr)
        self.assertEqual(self.head(), before)

    def test_a_blob_the_target_branch_already_tracks_does_not_refuse_a_later_merge(self):  # noqa: VACUOUS_ASSERTION — the absence is read on a merge asserted to exit 0 and make a two-parent commit, after the same blob is asserted REFUSED at the commit door
        self.stage_bytes("data/blob.bin", self.BLOB)
        r = self.commit("bulk on main", env=self.ENV)
        self.assertNotEqual(r.returncode, 0,
                            "must-hit: the scanner does not flag this blob "
                            "at all, so its silence below says nothing")
        self.assertIn("data/blob.bin", r.stderr)
        r = self.commit("bulk on main",
                        env=dict(self.ENV, HELM_NEVER_TRACK_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = self.branch_with("later", {"docs/later.md": b"later\n"})
        before = self.head()
        r = self.git("merge", "--no-ff", "-m", "merge later", "later")
        self.assertEqual(r.returncode, 0,
                         "a blob main already tracks refused an unrelated "
                         "merge: %s" % r.stderr)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD"
                                  ).stdout.split()[1:], [before, tip])
        self.assertNotIn("REFUSED", r.stderr)

    def test_git_commit_on_the_refused_merge_result_is_refused_too(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD follows an asserted non-zero exit, REFUSED, the path and ADDED BY THIS COMMIT; the control then asserts exit 0 and a three-oid parents line
        """Git leaves a refused merge in progress and suggests `git commit`,
        which reaches pre-commit with MERGE_HEAD present. Nothing is recorded
        between the doors: pre-commit judges the blob against HEAD on its own
        and gives the merge door's answer."""
        self.blob_branch()
        before = self.head()
        self.assert_refused_blob_merge(before)
        self.assert_git_commit_refuses_the_blob(before)
        # CONTROL: a resolution that drops the blob commits as an ordinary
        # two-parent merge.
        r = self.git("rm", "-q", "--cached", "data/blob.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.git("commit", "--no-edit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1",
                                      "HEAD").stdout.split()), 3)

    STAMP = "helm-refused-merge"

    def assert_no_stamp(self):
        """No file passes a verdict from the merge door to the commit door."""
        record = self.git("rev-parse", "--git-path", self.STAMP).stdout.strip()
        self.assertTrue(record, "fixture: git answered no git-dir path")
        gitdir = self.git("rev-parse", "--absolute-git-dir").stdout.strip()
        self.assertTrue(os.path.isdir(gitdir), "fixture: no git dir to read")
        self.assertFalse(os.path.exists(os.path.join(self.root, record)),
                         "a %s record was written" % self.STAMP)
        self.assertNotIn(self.STAMP, os.listdir(gitdir))

    def assert_git_commit_refuses_the_blob(self, before):
        """`git commit --no-edit` on an in-progress merge that carries the
        branch's blob: the refusal is the pre-commit door's own HEAD-only bulk
        judgement."""
        self.assertEqual(self.git("rev-parse", "-q", "--verify",
                                  "MERGE_HEAD").returncode, 0,
                         "fixture: no merge is in progress, so this arm is "
                         "about an ordinary commit")
        self.assert_no_stamp()
        r = self.git("commit", "--no-edit")
        self.assertNotEqual(r.returncode, 0,
                            "a merge concluded with git commit landed a "
                            "2 MiB blob only the merged branch carries: %s"
                            % r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("data/blob.bin", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertEqual(self.head(), before, "HEAD moved on a refused commit")
        self.assertNotEqual(
            self.git("cat-file", "-e", "HEAD:data/blob.bin").returncode, 0,
            "the blob is in HEAD's tree")

    def test_a_no_commit_merge_of_a_2_MiB_blob_is_refused_at_git_commit(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD follows an asserted non-zero exit, REFUSED, the path and ADDED BY THIS COMMIT; the control asserts exit 0 and a three-oid parents line
        """`git merge --no-commit` never runs pre-merge-commit; the merge is
        concluded with `git commit`, which reaches pre-commit with MERGE_HEAD
        present. CONTROL: the same --no-commit shape over a branch of small
        files lands as a two-parent merge."""
        self.blob_branch()
        before = self.head()
        r = self.git("merge", "--no-ff", "--no-commit", "bulk")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("[helm merge]", r.stderr,
                         "fixture: the merge door ran, so this arm is not "
                         "about the commit door")
        self.assert_git_commit_refuses_the_blob(before)
        self.assertEqual(self.git("merge", "--abort").returncode, 0)
        self.branch_with("small", {"docs/a.md": b"a\n"})
        r = self.git("merge", "--no-ff", "--no-commit", "small")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.git("commit", "--no-edit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1",
                                      "HEAD").stdout.split()), 3)

    def test_a_conflicted_merge_that_brings_a_2_MiB_blob_is_refused_at_git_commit(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD follows an asserted non-zero exit, REFUSED, the path and ADDED BY THIS COMMIT
        """A conflict stops the merge before any commit door; the operator
        resolves by hand and runs `git commit`."""
        self.stage_bytes("src/m.txt", b"base\n")
        self.assertEqual(self.commit("m", env=self.ENV).returncode, 0)
        self.branch_with("bulk", {"data/blob.bin": self.BLOB,
                                  "src/m.txt": b"theirs\n"},
                         env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.stage_bytes("src/m.txt", b"ours\n")
        self.assertEqual(self.commit("ours", env=self.ENV).returncode, 0)
        before = self.head()
        r = self.git("merge", "--no-ff", "bulk")
        self.assertNotEqual(r.returncode, 0,
                            "fixture: the merge did not conflict: %s"
                            % r.stdout)
        self.assertIn("CONFLICT", r.stdout)
        self.stage_bytes("src/m.txt", b"resolved\n")
        self.assert_git_commit_refuses_the_blob(before)

    # ---- THE BULK LEG AGREES AT BOTH DOORS: two poles, one rule ----------

    MIB = 1024 * 1024

    def source_blob(self, size):
        """A landreq.py-shaped source file of `size` bytes: text, one line."""
        head = b"# landreq\n#"
        return head + b"x" * (size - len(head) - 1) + b"\n"

    def parents_of(self, ref="HEAD"):
        return self.git("rev-list", "--parents", "-n", "1",
                        ref).stdout.split()[1:]

    def at_both_doors(self, branch, before):
        """Fold `branch` into `before` through the merge door (`git merge
        --no-ff`) and, from the same `before`, through the commit door
        (`git merge --no-ff --no-commit` then `git commit`), each on its own
        target branch. -> (merge-door run, merge-door head, commit-door run,
        commit-door head). A refused attempt is aborted before the next."""
        tip = self.git("rev-parse", branch).stdout.strip()
        runs = []
        for door in ("merge-door", "commit-door"):
            r = self.git("checkout", "-q", "-B", door, before)
            self.assertEqual(r.returncode, 0, r.stderr)
            if door == "merge-door":
                r = self.git("merge", "--no-ff", "-m", "fold " + branch,
                             branch)
            else:
                m = self.git("merge", "--no-ff", "--no-commit", branch)
                self.assertEqual(m.returncode, 0, m.stdout + m.stderr)
                self.assertNotIn("[helm merge]", m.stderr,
                                 "fixture: --no-commit met the merge door")
                self.assertEqual(self.git("rev-parse", "-q", "--verify",
                                          "MERGE_HEAD").stdout.strip(), tip)
                r = self.git("commit", "--no-edit")
            head = self.head()
            if r.returncode != 0:
                self.assertEqual(self.git("merge", "--abort").returncode, 0)
            runs += [r, head]
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        return runs

    def test_PASSING_POLE_a_fold_into_a_head_already_over_the_line_needs_no_waiver(self): # noqa: VACUOUS_ASSERTION — every absent REFUSED follows an asserted exit 0 and a merge commit whose parents are exactly the target and the folded tip; the must-hit refuses the same blob at the commit door first
        """HEAD already carries helm/landreq.py above the size line. A fold
        that brings the SAME blob, and a fold that brings a SMALLER one still
        above the line, are refused by nothing at either door and need no
        helm-bulk=ok."""
        rel = "helm/landreq.py"
        big, smaller = self.source_blob(2 * self.MIB), self.source_blob(
            3 * self.MIB // 2)
        seed = self.head()
        self.stage_bytes(rel, big)
        r = self.commit("landreq on main", env=self.ENV)
        self.assertNotEqual(r.returncode, 0,
                            "must-hit: the blob is not over the size line, "
                            "so no pass below is about one")
        self.assertIn(rel, r.stderr)
        r = self.commit("landreq on main",
                        env=dict(self.ENV, HELM_NEVER_TRACK_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.head()
        # THE SAME BLOB, arriving through a history of its own: a branch cut
        # before main took the file commits identical bytes.
        r = self.git("checkout", "-q", "-b", "same", seed)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage_bytes(rel, big)
        self.stage_bytes("docs/same.md", b"same\n")
        r = self.git("commit", "-q", "-m", "same",
                     env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        same = self.head()
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        # A SMALLER BLOB, still over the line, committed on top of main's.
        smaller_tip = self.branch_with("smaller", {rel: smaller},
                                       env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertGreater(len(smaller), nevertrack.BULK_CEILING)
        for branch, tip in (("same", same), ("smaller", smaller_tip)):
            merge_r, merge_head, commit_r, commit_head = self.at_both_doors(
                branch, before)
            for door, r, head in (("merge", merge_r, merge_head),
                                  ("commit", commit_r, commit_head)):
                self.assertEqual(r.returncode, 0,
                                 "the %s door refused a %s fold into a HEAD "
                                 "already over the line: %s"
                                 % (door, branch, r.stderr))
                self.assertEqual(self.parents_of(head), [before, tip])
                self.assertNotIn("REFUSED", r.stderr)
                self.assertIn(rel, self.git("ls-tree", "-r", "--name-only",
                                            head).stdout.split())

    def test_REFUSING_POLE_a_fold_that_takes_a_path_over_the_line_is_refused_at_both_doors(self): # noqa: VACUOUS_ASSERTION — the unmoved heads follow asserted non-zero exits naming the path and ADDED BY THIS COMMIT; the declared fold then asserts exit 0 and exact parents at both doors
        """HEAD holds helm/landreq.py below the line; the incoming parent
        brings it above. Both doors judge against HEAD and refuse; the same
        fold with helm-bulk=ok declared in the staged .gitattributes lands
        at both."""
        rel = "helm/landreq.py"
        self.stage_bytes(rel, self.source_blob(4096))
        r = self.commit("landreq small", env=self.ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.head()
        grow = self.branch_with("grow", {rel: self.source_blob(2 * self.MIB)},
                                env={"HELM_NEVER_TRACK_SKIP": "1"})
        merge_r, merge_head, commit_r, commit_head = self.at_both_doors(
            "grow", before)
        for door, r, head in (("merge", merge_r, merge_head),
                              ("commit", commit_r, commit_head)):
            self.assertNotEqual(r.returncode, 0,
                                "the %s door landed a fold that takes %s over "
                                "the line: %s" % (door, rel, r.stderr))
            self.assertIn("[helm never-track] REFUSED", r.stderr)
            self.assertIn(rel, r.stderr)
            self.assertIn("ADDED BY THIS COMMIT", r.stderr)
            self.assertEqual(head, before, "the %s door moved HEAD" % door)
        self.assertIn("helm-bulk=ok", merge_r.stderr,
                      "the merge door does not name the declared route")
        # THE HOOK'S OWN BULK SENTENCE, apart from the scanner's: the scanner
        # prints helm-bulk=ok on every refusal, so only this names the
        # agreement between the two doors.
        self.assertIn("judged against HEAD at BOTH doors", merge_r.stderr,
                      "the merge door does not say bulk agrees at both doors")
        # THE DECLARED ROUTE.
        r = self.git("checkout", "-q", "-b", "declared", grow)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage_bytes(".gitattributes", b"%s helm-bulk=ok\n" % rel.encode())
        r = self.git("commit", "-q", "-m", "declare landreq")
        self.assertEqual(r.returncode, 0, r.stderr)
        declared = self.head()
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        merge_r, merge_head, commit_r, commit_head = self.at_both_doors(
            "declared", before)
        for door, r, head in (("merge", merge_r, merge_head),
                              ("commit", commit_r, commit_head)):
            self.assertEqual(r.returncode, 0,
                             "the %s door refused a declared fold: %s"
                             % (door, r.stderr))
            self.assertEqual(self.parents_of(head), [before, declared])
            self.assertIn("helm-bulk=ok", r.stderr)

    # ---- NEEDLES, MARKERS AND ADDRESSES: the stated asymmetry ------------

    def needle_branch(self, name):
        return self.branch_with(name, {"data/contact.txt":
                                       ("contact %s\n" % NEEDLE).encode()},
                                env={"HELM_NEVER_TRACK_SKIP": "1"})

    def test_a_needle_merge_is_refused_at_the_merge_door_and_the_refusal_names_the_commit_door(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD and the needle's absence from stderr follow an asserted non-zero exit naming the path and the route; the commit door then asserts a refusal for a needle no parent carries and exit 0 with exact parents for the one the branch carries
        """The merge door judges needles against HEAD alone; the commit door
        credits every parent. The refusal SAYS so, and the route it names
        admits the content the merged branch already carries while still
        refusing a needle no parent carries."""
        tip = self.needle_branch("needle")
        before = self.head()
        r = self.git("merge", "--no-ff", "-m", "merge needle", "needle")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertIn("data/contact.txt", r.stderr)
        self.assertNotIn(NEEDLE, r.stderr)
        self.assertIn("judged HERE against HEAD alone", r.stderr)
        self.assertIn("`git merge --no-commit` then `git commit`", r.stderr)
        self.assertIn("credits every parent", r.stderr)
        self.assertEqual(self.head(), before)
        self.assert_no_stamp()
        self.assertEqual(self.git("merge", "--abort").returncode, 0)
        r = self.git("merge", "--no-ff", "--no-commit", "needle")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # CONTROL: a needle NO parent carries is still refused at this door.
        self.stage_bytes("data/resolution.txt",
                         ("resolved %s\n" % NEEDLE).encode())
        r = self.git("commit", "--no-edit")
        self.assertNotEqual(r.returncode, 0,
                            "the commit door credited a needle no parent "
                            "carries: %s" % r.stderr)
        self.assertIn("data/resolution.txt", r.stderr)
        self.assertIn("ADDED BY THIS COMMIT", r.stderr)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.git("rm", "-q", "--cached",
                                  "data/resolution.txt").returncode, 0)
        r = self.git("commit", "--no-edit")
        self.assertEqual(r.returncode, 0,
                         "the route the refusal names did not admit what "
                         "the merged branch carries: %s" % r.stderr)
        self.assertEqual(self.parents_of(), [before, tip])
        self.assertEqual(self.git("cat-file", "-e",
                                  "HEAD:data/contact.txt").returncode, 0)

    def test_an_aborted_refusal_leaves_nothing_behind_for_a_later_fold_from_the_same_head(self): # noqa: VACUOUS_ASSERTION — the stamp's absence is read after an asserted refusal, and again after the later fold is asserted to exit 0 with exact parents
        """A merge refused and aborted writes no record, so a later
        legitimate fold from the SAME HEAD bringing the same content through
        a different commit is judged on its own: credited at the commit
        door."""
        self.needle_branch("first")
        before = self.head()
        r = self.git("merge", "--no-ff", "-m", "merge first", "first")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[helm merge]", r.stderr)
        self.assert_no_stamp()
        self.assertEqual(self.git("merge", "--abort").returncode, 0)
        self.assertEqual(self.head(), before)
        r = self.git("checkout", "-q", "-b", "later", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stage_bytes("data/contact.txt", ("contact %s\n" % NEEDLE).encode())
        r = self.git("commit", "-q", "-m", "the same content, a later commit",
                     env={"HELM_NEVER_TRACK_SKIP": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        later = self.head()
        self.assertNotEqual(later, self.git("rev-parse", "first").stdout.strip())
        self.assertEqual(self.git("checkout", "-q", "main").returncode, 0)
        r = self.git("merge", "--no-ff", "--no-commit", "later")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), before, "fixture: not the same HEAD")
        r = self.git("commit", "--no-edit")
        self.assertEqual(r.returncode, 0,
                         "an aborted refusal contaminated a later fold: %s"
                         % r.stderr)
        self.assertEqual(self.parents_of(), [before, later])
        self.assert_no_stamp()

    # ---- THE COMMIT DOOR'S MARKER RUNG ----------------------------------

    MARKED = b"intro\n<<<<<<< ours\nbody\n"

    def test_an_unchanged_marker_is_refused_at_pre_commit_and_the_corrected_file_commits(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD follows an asserted non-zero exit, REFUSED and the path; the corrected commit asserts exit 0 and the committed bytes
        before = self.head()
        self.stage_bytes("docs/notes.md", self.MARKED)
        r = self.commit("marked", env=self.ENV)
        self.assertNotEqual(r.returncode, 0,
                            "a conflict-marker line committed under the %s "
                            "profile: %s" % (self.PROFILE, r.stderr))
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr)
        self.assertIn("docs/notes.md", r.stderr)
        self.assertEqual(self.head(), before)
        self.stage_bytes("docs/notes.md", b"intro\nbody\n")
        r = self.commit("corrected", env=self.ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git("show", "HEAD:docs/notes.md").stdout,
                         "intro\nbody\n")

    def test_the_marker_rungs_skip_and_absence_never_disarm_never_track(self): # noqa: VACUOUS_ASSERTION — each absent marker REFUSED follows an asserted non-zero exit carrying never-track's REFUSED for the needle staged beside it
        self.stage_bytes("docs/notes.md", self.MARKED + NEEDLE.encode() + b"\n")
        r = self.commit("skipped", env=dict(self.ENV,
                                            HELM_CONFLICT_MARKER_SKIP="1"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)
        self.assertNotIn("[helm conflict-marker] REFUSED", r.stderr)
        snap = next(p for p in _guard._scanner_assets(self.root)
                    if p.endswith("/conflict_marker.py"))
        os.remove(snap)
        r = self.commit("absent", env=self.ENV)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm conflict-marker] WARNING: scanner missing", r.stderr)
        self.assertIn("[helm never-track] REFUSED", r.stderr)

    def test_a_foreign_pre_merge_commit_hook_is_preserved_and_still_runs(self): # noqa: VACUOUS_ASSERTION — the unmoved HEAD follows an asserted non-zero exit, after the same hook is asserted to have run and the companion to hold the original bytes
        """The same contract as a foreign pre-commit: preserved byte-for-byte
        as `.helm-user`, run first, and its refusal refuses the merge."""
        shutil.rmtree(os.path.dirname(_guard.hook_path(self.root,
                                                       "pre-merge-commit")))
        self.sh(self.root, "git", "config", "--local", "--unset",
                _guard.PROFILE_KEY)
        hook = _guard.hook_path(self.root, "pre-merge-commit")
        log = os.path.join(self.tmp, "user-merge-hook.log")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nprintf ran >> %s\nexit 0\n" % log)
        os.chmod(hook, 0o755)
        with open(hook, "rb") as f:
            original = f.read()
        rc, out, err = self.cli("install-guard", "--apply", "--profile",
                                self.PROFILE)
        self.assertEqual(rc, 0, err)
        self.assertIn("preserved existing %s" % hook, out)
        with open(hook + ".helm-user", "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertTrue(_guard._owned_hook(_guard._path_snapshot(hook)))
        self.branch_with("small", {"docs/a.md": b"a\n"})
        r = self.git("merge", "--no-ff", "-m", "merge small", "small")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            self.assertEqual(f.read(), "ran", "the user's hook did not run")
        with open(hook + ".helm-user", "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        self.branch_with("small2", {"docs/b.md": b"b\n"})
        before = self.head()
        r = self.git("merge", "--no-ff", "-m", "merge small2", "small2")
        self.assertNotEqual(r.returncode, 0, "a refusing user hook was skipped")
        self.assertEqual(self.head(), before)


class RailMergeCommitMeetsTheLeakGuardTest(MergeCommitMeetsTheLeakGuardTest):
    """The rail profile installs the same pre-merge-commit hook, and every arm
    above holds under it."""

    PROFILE = "rail"

    def test_the_rail_plans_pre_merge_commit(self):
        self.assertIn("pre-merge-commit",
                      [n for n, _t in _guard.GUARD_PROFILES["rail"]])
        with open(_guard.hook_path(self.root, "pre-merge-commit")) as f:
            body = f.read()
        self.assertIn("pre-merge-commit v2", body)


class TheDocsNameEveryRungTheMergeDoorSkipsTest(unittest.TestCase):
    """The merge door runs fewer refusing rungs than the rail's pre-commit.
    The set is DERIVED from the hook templates, so a rung added to the rail's
    pre-commit reddens this arm until the docs and the merge hook's comment
    name it."""

    REFUSING = re.compile(
        r'(?:exec python3 "\$(\w+)"|python3 "\$(\w+)"[^\n]*'
        r'\|\| (?:exit \$\?|helm_merge_refused))')
    LABEL = re.compile(r'\[helm ([\w-]+)\] WARNING: \w+ missing at \$(\w+)')

    def rungs(self, template):
        labels = {var: label for label, var in self.LABEL.findall(template)}
        found = set()
        for exec_var, var in self.REFUSING.findall(template):
            var = exec_var or var
            self.assertIn(var, labels, "no WARNING label for $%s" % var)
            found.add(labels[var])
        return found

    def test_the_rail_commit_door_rungs_the_merge_door_skips_are_named(self):
        merge = self.rungs(_guard.MERGE_COMMIT_HOOK)
        self.assertEqual(merge, {"conflict-marker", "never-track"})
        skipped = self.rungs(_guard.NEVER_TRACK_HOOK) - merge
        # MUST-HIT: the rail's pre-commit does run rungs the merge door skips.
        self.assertIn("seat-name", skipped)
        self.assertEqual(self.rungs(_guard.LEAK_PRECOMMIT_HOOK), merge,
                         "the leak doors no longer run the same rungs")
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(_guard.__file__)))
        with open(os.path.join(os.path.dirname(pkg), "docs", "VERBS.md")) as f:
            doc = " ".join(f.read().split())
        start = doc.index("THE MERGE DOOR RUNS ONLY THOSE TWO RUNGS")
        passage = doc[start:start + 600]
        comment = " ".join(_guard.MERGE_COMMIT_HOOK.split())
        start = comment.index("THIS DOOR RUNS ONLY THOSE TWO RUNGS")
        comment = comment[start:start + 600]
        for rung in sorted(skipped):
            self.assertIn("`%s`" % rung, passage,
                          "docs/VERBS.md does not name the %s rung the merge "
                          "door skips" % rung)
            self.assertIn(rung, comment,
                          "the merge hook's comment does not name the %s rung"
                          % rung)
