#!/usr/bin/env python3
"""helm tidy — hermetic tests against a FAKE estate (tmp config dirs) and a
scratch git repo. Mirrors test_skillsync's estate shape and test_work's repo
harness. Proves the laws the task pins: dry-run purity, idempotence,
backup-integrity, rollback-after-mutation, superset-refusal, fail-closed,
locked/occupied-worktree immunity, rescue-dirty-first, never-remove-unmerged."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-envtidy-home-"))

from helm import envtidy, hooks as hooks_mod, skillsync, work  # noqa: E402


def _cmd(tup):
    return hooks_mod.spec_command(envtidy._spec(tup))


def _settings_with(tuples, strays=()):
    """A settings dict carrying the exact canonical commands for `tuples`
    (so a sync sees them as already-current) plus any foreign stray hooks."""
    hooks = {}
    for tup in tuples:
        event, matcher, _args, _t = tup
        entry = {"hooks": [{"type": "command", "command": _cmd(tup)}]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
    for event, command in strays:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": command}]})
    return {"hooks": hooks}


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


# ---------------------------------------------------------------------------
# config-dir estate: hooks sync + mcp sync + census
# ---------------------------------------------------------------------------

class EstateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-envtidy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        j = os.path.join
        self.backup = j(self.tmp, "premerge-backup")
        self.croot = j(self.tmp, "claude-homes")
        self.default = j(self.tmp, "dot-claude")
        self.seats = j(self.tmp, "seats")

        # whole: a credhome with the FULL canonical hook set already wired.
        self.whole = j(self.croot, "whole-com")
        _write(j(self.whole, "settings.json"), _settings_with(envtidy.CANONICAL_HOOKS))

        # partial: a credhome MISSING several canonical hooks + a foreign stray.
        self.partial = j(self.croot, "partial-com")
        _write(j(self.partial, "settings.json"), _settings_with(
            envtidy.CANONICAL_HOOKS[:2],
            strays=[("SessionStart", "/usr/local/bin/repo-hygiene --quick")]))

        # bare: a credhome with NO settings.json at all.
        os.makedirs(j(self.croot, "bare-com"))

        # broken: unreadable settings.json (fail-closed target).
        self.broken = j(self.croot, "broken-com")
        os.makedirs(self.broken)
        with open(j(self.broken, "settings.json"), "w") as f:
            f.write("{ this is not json ]")

        # default ~/.claude: full set + a foreign integration hook.
        _write(j(self.default, "settings.json"), _settings_with(
            envtidy.CANONICAL_HOOKS,
            strays=[("UserPromptSubmit", "bash /home/x/herdr.sh ambient")]))

        # seats: one with the LEAN set already, one bare.
        lean = tuple(h for h in envtidy.CANONICAL_HOOKS
                     if h[2] in envtidy._LEAN_ARGS)
        _write(j(self.seats, "codex", "claude", "settings.json"),
               _settings_with(lean))
        os.makedirs(j(self.seats, "kimi", "claude"))

        self.dirs = skillsync.config_dirs(
            claude_root=self.croot, default_claude=self.default,
            seats_root=self.seats)

    def _bytes(self, cdir):
        with open(os.path.join(cdir, "settings.json"), "rb") as f:
            return f.read()

    # ---- census -----------------------------------------------------------

    def test_census_reads_the_whole_estate(self):
        r = envtidy.census(dirs=self.dirs)
        by = {h["label"]: h for h in r["homes"]}
        self.assertEqual(by["whole-com"]["missing"], [])
        self.assertEqual(by["whole-com"]["kind"], "home")
        self.assertTrue(by["partial-com"]["missing"])       # several missing
        self.assertEqual(len(by["partial-com"]["strays"]), 1)
        self.assertIn("error", by["broken-com"])            # fail-closed, reported
        self.assertEqual(by["seat:codex"]["kind"], "seat")
        self.assertEqual(by["seat:codex"]["missing"], [])   # lean set complete
        # the partial home's gap is surfaced in the variance map
        self.assertTrue(r["hooks_variance"]["missing_by_hook"])
        self.assertIn("partial-com", r["hooks_variance"]["strays_by_home"])

    # ---- hooks sync -------------------------------------------------------

    def test_hooks_dry_run_touches_nothing(self):
        before = {c: self._bytes(c) for _l, c in self.dirs
                  if os.path.isfile(os.path.join(c, "settings.json"))}
        r = envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=False)
        for c, b in before.items():
            self.assertEqual(self._bytes(c), b)             # not one byte moved
        self.assertFalse(os.path.exists(self.backup))       # dry: no backup dir
        self.assertTrue(any(p["label"] == "partial-com" for p in r["changed"]))

    def test_hooks_apply_adds_missing_and_preserves_strays(self):
        r = envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertEqual([p["label"] for p in r["failed"]], ["broken-com"])
        with open(os.path.join(self.partial, "settings.json")) as f:
            got = json.load(f)
        cmds = [c for _e, _m, c in envtidy._all_hooks(got)]
        for tup in envtidy.CANONICAL_HOOKS:                 # every canonical present
            self.assertIn(_cmd(tup), cmds)
        # the foreign stray survived byte-for-byte
        self.assertTrue(any("repo-hygiene" in c for c in cmds))
        # the broken home was NEVER written (fail-closed)
        with open(os.path.join(self.broken, "settings.json")) as f:
            self.assertEqual(f.read(), "{ this is not json ]")
        # backup-integrity: the partial home's pre-image is on the shelf
        shelf = os.path.join(self.backup, "partial-com")
        self.assertTrue(os.path.isdir(shelf) and os.listdir(shelf))

    def test_hooks_mid_write_failure_restores_original_or_absence(self):
        """A writer can fail after changing bytes. Both kinds of pre-image —
        an existing file and no file — must be restored exactly."""
        partial_path = os.path.join(self.partial, "settings.json")
        with open(partial_path, "rb") as f:
            original = f.read()

        def mutate_then_fail(path, _text):
            with open(path, "w") as f:
                f.write('{"failed-after-mutation": true}\n')
            raise OSError("injected after mutation")

        with mock.patch.object(envtidy.pk, "atomic_write",
                               side_effect=mutate_then_fail):
            verdict, _detail = envtidy.apply_hooks_home(
                envtidy.plan_hooks_home("partial-com", self.partial), self.backup)
        self.assertEqual(verdict, "FAIL")
        with open(partial_path, "rb") as f:
            self.assertEqual(f.read(), original)

        bare_path = os.path.join(self.croot, "bare-com", "settings.json")
        self.assertFalse(os.path.exists(bare_path))
        with mock.patch.object(envtidy.pk, "atomic_write",
                               side_effect=mutate_then_fail):
            verdict, _detail = envtidy.apply_hooks_home(
                envtidy.plan_hooks_home("bare-com", os.path.dirname(bare_path)),
                self.backup)
        self.assertEqual(verdict, "FAIL")
        self.assertFalse(os.path.exists(bare_path))

    def test_hooks_idempotent(self):
        envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        r = envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        # only the broken home remains (unfixable); every other home is steady
        self.assertEqual([p["label"] for p in r["changed"]], [])
        self.assertEqual([p["label"] for p in r["failed"]], ["broken-com"])

    def test_hooks_env_override_replaces_canonical(self):
        ov = os.path.join(self.tmp, "canon.json")
        with open(ov, "w") as f:
            json.dump([{"event": "Stop", "args": "chat stop-guard --hook-json"}], f)
        old = os.environ.get("HELM_HOOKS_CANONICAL")
        os.environ["HELM_HOOKS_CANONICAL"] = ov
        try:
            self.assertEqual(envtidy.canonical_hooks(),
                             (("Stop", None, "chat stop-guard --hook-json", 5),))
        finally:
            if old is None:
                del os.environ["HELM_HOOKS_CANONICAL"]
            else:
                os.environ["HELM_HOOKS_CANONICAL"] = old

    # ---- mcp sync ---------------------------------------------------------

    def _mcp_canon(self, mapping):
        p = os.path.join(self.tmp, "mcp-canon.json")
        with open(p, "w") as f:
            json.dump(mapping, f)
        return p

    def test_mcp_add_with_config_preserves_existing(self):
        # a home already carrying one raw server, missing the canonical one
        _write(os.path.join(self.whole, ".claude.json"),
               {"mcpServers": {"pre-existing": {"command": "keep-me"}}})
        os.environ["HELM_MCPS_CANONICAL"] = self._mcp_canon(
            {"foo-mcp": {"command": "foo"}})
        try:
            r = envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
            self.assertEqual(r["failed"], [])
            with open(os.path.join(self.whole, ".claude.json")) as f:
                got = json.load(f)
            self.assertIn("foo-mcp", got["mcpServers"])       # added
            self.assertIn("pre-existing", got["mcpServers"])  # superset preserved
            # idempotent second apply: nothing left to add
            r2 = envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
            self.assertEqual(r2["changed"], [])
        finally:
            del os.environ["HELM_MCPS_CANONICAL"]

    def test_mcp_mid_write_failure_restores_original(self):
        state = os.path.join(self.whole, ".claude.json")
        _write(state, {"mcpServers": {"pre-existing": {"command": "keep-me"}}})
        with open(state, "rb") as f:
            original = f.read()
        os.environ["HELM_MCPS_CANONICAL"] = self._mcp_canon(
            {"foo-mcp": {"command": "foo"}})

        def mutate_then_fail(path, _text):
            with open(path, "w") as f:
                f.write('{"mcpServers": {}}\n')
            raise OSError("injected after mutation")

        try:
            plan = envtidy.plan_mcp_home("whole-com", self.whole)
            with mock.patch.object(envtidy.pk, "atomic_write",
                                   side_effect=mutate_then_fail):
                verdict, _detail = envtidy.apply_mcp_home(plan, self.backup)
            self.assertEqual(verdict, "FAIL")
            with open(state, "rb") as f:
                self.assertEqual(f.read(), original)
        finally:
            del os.environ["HELM_MCPS_CANONICAL"]

    def test_mcp_dry_run_touches_nothing(self):
        _write(os.path.join(self.whole, ".claude.json"), {"mcpServers": {}})
        with open(os.path.join(self.whole, ".claude.json"), "rb") as f:
            pre = f.read()
        os.environ["HELM_MCPS_CANONICAL"] = self._mcp_canon(
            {"foo-mcp": {"command": "foo"}})
        try:
            envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup, apply=False)
            with open(os.path.join(self.whole, ".claude.json"), "rb") as f:
                self.assertEqual(f.read(), pre)
            self.assertFalse(os.path.exists(self.backup))
        finally:
            del os.environ["HELM_MCPS_CANONICAL"]

    def test_mcp_default_is_report_only(self):
        # the default canonical carries no configs -> surface, never mutate
        r = envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertEqual(r["failed"], [])
        for p in r["changed"]:
            self.assertEqual(p["adds"], [])                  # nothing auto-added
        self.assertFalse(os.path.exists(self.backup))        # no config -> no write


# ---------------------------------------------------------------------------
# git repo: worktree gc
# ---------------------------------------------------------------------------

def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=30)


class WorktreeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-envtidy-wt-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_prior = {k: os.environ.get(k)
                          for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.addCleanup(self._restore_env)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(self.root, *cmd).returncode, 0)
        self._commit("README", "seed")

    def _restore_env(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _commit(self, name, body, where=None):
        where = where or self.root
        with open(os.path.join(where, name), "w") as f:
            f.write(body + "\n")
        _sh(where, "git", "add", "-A")
        r = _sh(where, "git", "commit", "-q", "-m", "add " + name)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _branch_at_main(self, name):
        self.assertEqual(_sh(self.root, "git", "branch", name).returncode, 0)


class WorktreeGcTest(WorktreeBase):
    def _seed(self):
        # merged orphan stub: branch whose commit is merged, no worktree
        _sh(self.root, "git", "checkout", "-q", "-b", "worktree-merged")
        self._commit("m.txt", "merged work")
        _sh(self.root, "git", "checkout", "-q", "main")
        _sh(self.root, "git", "merge", "-q", "worktree-merged")
        # unmerged orphan stub: branch with an unlanded commit, no worktree
        _sh(self.root, "git", "checkout", "-q", "-b", "worktree-unmerged")
        self._commit("u.txt", "unlanded work")
        _sh(self.root, "git", "checkout", "-q", "main")
        # registered worktree, clean, at main tip (merged -> removable)
        self.wt_rm = os.path.join(self.tmp, "wts", "wf-clean")
        _sh(self.root, "git", "worktree", "add", "-q", "-b", "worktree-wfclean",
            self.wt_rm, "main")
        # registered worktree, AHEAD of main (unmerged -> blocked/keep)
        self.wt_keep = os.path.join(self.tmp, "wts", "wf-ahead")
        _sh(self.root, "git", "worktree", "add", "-q", "-b", "worktree-wfahead",
            self.wt_keep, "main")
        self._commit("ahead.txt", "ahead work", where=self.wt_keep)
        # registered worktree, LOCKED (active review) -> immune even if merged
        self.wt_lock = os.path.join(self.tmp, "wts", "wf-lock")
        _sh(self.root, "git", "worktree", "add", "-q", "-b", "worktree-wflock",
            self.wt_lock, "main")
        _sh(self.root, "git", "worktree", "lock", self.wt_lock, "--reason",
            "lease:review")
        # registered worktree, DIRTY + unmerged -> rescue-commit, then KEEP
        self.wt_dirty = os.path.join(self.tmp, "wts", "wf-dirty")
        _sh(self.root, "git", "worktree", "add", "-q", "-b", "worktree-wfdirty",
            self.wt_dirty, "main")
        self._commit("d1.txt", "committed", where=self.wt_dirty)
        with open(os.path.join(self.wt_dirty, "uncommitted.txt"), "w") as f:
            f.write("rescue me\n")

    def _branches(self):
        r = _sh(self.root, "git", "for-each-ref", "--format=%(refname:short)",
                "refs/heads/")
        return set(r.stdout.split())

    def _worktree_paths(self):
        return {w["path"] for w in envtidy._worktree_rows(self.root, "main")}

    def test_dry_run_touches_nothing(self):
        self._seed()
        branches, wts = self._branches(), self._worktree_paths()
        r = envtidy.worktree_gc(root=self.root, apply=False)
        self.assertEqual(self._branches(), branches)         # no branch deleted
        self.assertEqual(self._worktree_paths(), wts)        # no worktree removed
        # the dry plan classifies correctly
        verds = {o["branch"]: o["verdict"] for o in r["orphans"]}
        self.assertEqual(verds["worktree-merged"], "delete")
        self.assertEqual(verds["worktree-unmerged"], "keep")

    def test_apply_deletes_merged_orphans_keeps_unmerged(self):
        self._seed()
        envtidy.worktree_gc(root=self.root, apply=True)
        b = self._branches()
        self.assertNotIn("worktree-merged", b)               # merged stub deleted
        self.assertIn("worktree-unmerged", b)                # unmerged stub kept

    def test_apply_removes_merged_worktree_keeps_ahead(self):
        self._seed()
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertFalse(os.path.exists(self.wt_rm))         # merged+clean removed
        self.assertTrue(os.path.exists(self.wt_keep))        # ahead -> blocked, kept

    def test_locked_worktree_is_immune(self):
        self._seed()
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertTrue(os.path.exists(self.wt_lock))        # never touched
        self.assertIn("worktree-wflock", self._branches())

    def test_occupied_worktree_is_immune(self):
        """A live pane/process cwd'd in a worktree is a hard keep even when the
        tree is clean + merged. Deleting it strands that process at `(deleted)`."""
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        self._seed()
        proc = subprocess.Popen(["sleep", "30"], cwd=self.wt_rm)
        try:
            r = envtidy.worktree_gc(root=self.root, apply=True)
            row = next(w for w in r["worktree_rows"] if w["path"] == self.wt_rm)
            self.assertEqual(row["verdict"], "keep")
            self.assertTrue(row["occupied"])
            self.assertTrue(os.path.isdir(self.wt_rm))
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_enact_rechecks_occupancy_after_scan(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        self._seed()
        row = next(w for w in envtidy._worktree_rows(self.root, "main")
                   if w["path"] == self.wt_rm)
        self.assertEqual(row["verdict"], "remove")
        proc = subprocess.Popen(["sleep", "30"], cwd=self.wt_rm)
        try:
            lines = envtidy._enact_worktree(self.root, row, True)
            self.assertTrue(any("OCCUPIED" in line for line in lines))
            self.assertTrue(os.path.isdir(self.wt_rm))
        finally:
            proc.terminate()
            proc.wait()

    def test_enact_rechecks_occupancy_after_rescue(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        self._seed()
        with open(os.path.join(self.wt_rm, "late.txt"), "w") as f:
            f.write("rescue first\n")
        row = next(w for w in envtidy._worktree_rows(self.root, "main")
                   if w["path"] == self.wt_rm)
        self.assertEqual(row["verdict"], "rescue+remove")
        procs = []
        original = work._wip_commit

        def rescue_then_enter(path, msg):
            result = original(path, msg)
            procs.append(subprocess.Popen(["sleep", "30"], cwd=path))
            return result

        try:
            with mock.patch.object(work, "_wip_commit",
                                   side_effect=rescue_then_enter):
                lines = envtidy._enact_worktree(self.root, row, True)
            self.assertTrue(any("OCCUPIED" in line for line in lines))
            self.assertTrue(os.path.isdir(self.wt_rm))
            self.assertEqual(_sh(self.wt_rm, "git", "status", "--porcelain").stdout,
                             "")
        finally:
            for proc in procs:
                proc.terminate()
                proc.wait()

    def test_enact_rechecks_lock_after_scan(self):
        self._seed()
        row = next(w for w in envtidy._worktree_rows(self.root, "main")
                   if w["path"] == self.wt_rm)
        self.assertEqual(row["verdict"], "remove")
        _sh(self.root, "git", "worktree", "lock", self.wt_rm,
            "--reason", "review started after scan")
        lines = envtidy._enact_worktree(self.root, row, True)
        self.assertTrue(any("LOCKED" in line for line in lines))
        self.assertTrue(os.path.isdir(self.wt_rm))

    def test_rescue_dirty_first_then_keep(self):
        self._seed()
        envtidy.worktree_gc(root=self.root, apply=True)
        # the worktree survives (unmerged -> kept), and the rescue committed
        # the previously-uncommitted file onto its branch (never discarded)
        self.assertTrue(os.path.exists(self.wt_dirty))
        r = _sh(self.wt_dirty, "git", "status", "--porcelain")
        self.assertEqual(r.stdout.strip(), "")               # clean after rescue
        log = _sh(self.wt_dirty, "git", "log", "-1", "--name-only", "--format=")
        self.assertIn("uncommitted.txt", log.stdout)

    def test_cmd_repo_flag_targets_named_repo(self):
        # --repo PATH (the flag the no-repo error advertises) must actually
        # steer the verb at that repo from an unrelated cwd — a real defect if
        # the message promises a flag the dispatcher drops.
        self._seed()
        cwd = os.getcwd()
        os.chdir(self.tmp)            # a dir that is NOT the git repo
        try:
            # without --repo, cwd is not a repo -> error return (1)
            self.assertEqual(envtidy.cmd_worktree(["gc"]), 1)
            # with --repo, the named repo is found and swept (dry-run) -> 0
            self.assertEqual(
                envtidy.cmd_worktree(["gc", "--repo", self.root]), 0)
        finally:
            os.chdir(cwd)
        # the merged orphan is still present (dry-run touched nothing)
        self.assertIn("worktree-merged", self._branches())

    def test_tidy_umbrella_runs_all_dry(self):
        self._seed()
        r = envtidy.tidy(root=self.root, apply=False)
        self.assertIn("census", r)
        self.assertIn("hooks", r)
        self.assertIn("mcp", r)
        self.assertFalse(r["apply"])
        # dry umbrella removed nothing
        self.assertIn("worktree-merged", self._branches())


class CliSafetyTest(unittest.TestCase):
    def test_every_mutating_verb_defaults_to_dry_run(self):
        """The public dispatchers, not only the Python helpers, must require
        the explicit --apply capability before they pass apply=True."""
        hooks = {"changed": [], "failed": [], "steady": 0,
                 "backup_root": "/backup"}
        with mock.patch.object(envtidy, "hooks_sync", return_value=hooks) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(envtidy.cmd_hooks_sync([]), 0)
        run.assert_called_once_with(apply=False)

        mcp = {"changed": [], "failed": [], "steady": 0,
               "backup_root": "/backup"}
        with mock.patch.object(envtidy, "mcp_sync", return_value=mcp) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(envtidy.cmd_mcp(["sync"]), 0)
        run.assert_called_once_with(apply=False)

        worktrees = {"root": "/repo", "base": "main", "apply": False,
                     "lane_rows": [], "lane_lines": {}, "worktree_rows": [],
                     "worktree_lines": {}, "orphans": [], "orphan_lines": {}}
        with mock.patch.object(envtidy, "worktree_gc", return_value=worktrees) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(envtidy.cmd_worktree(["gc"]), 0)
        run.assert_called_once_with(root=None, apply=False)

        report = {"apply": False, "backup_root": "/backup",
                  "census": {"homes": [], "hooks_variance": {
                      "missing_by_hook": {}, "strays_by_home": {}},
                      "mcp_variance": {"universal_effective": [],
                                       "canonical_names": [],
                                       "missing_by_home": {}},
                      "worktrees": {"note": "not inside a git repo"}},
                  "hooks": hooks, "mcp": mcp, "worktree": worktrees}
        with mock.patch.object(envtidy, "tidy", return_value=report) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(envtidy.cmd_tidy([]), 0)
        run.assert_called_once_with(root=None, apply=False)


if __name__ == "__main__":
    unittest.main()
