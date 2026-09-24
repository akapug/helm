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
import shlex
import shutil
import subprocess
import time
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-envtidy-home-", var="HELM_HOME")

from helm import configs, envtidy, hooks as hooks_mod, skillsync, work  # noqa: E402


def _cmd(tup):
    return hooks_mod.spec_command(envtidy._spec(tup))


def _settings_with(tuples, strays=()):
    """A settings dict carrying the exact canonical commands for `tuples`
    (so a sync sees them as already-current) plus any foreign stray hooks."""
    hooks = {}
    for tup in tuples:
        event, _matcher, _args, _t = tup
        entry = hooks_mod._canonical_entry(envtidy._spec(tup))
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
        # The scratch reaper (scratch.auto_gc) DELETES dead-session scratch under
        # the REAL /tmp/claude-* harness estate, and test_scratch.py's tripwire
        # pins this setting for any module that touches the Stop hook. This
        # module reconciles hook estates including the Stop guard, so it is off
        # here — matching tests/test_seats.py SeatsBase.
        _prior = os.environ.get("HELM_SCRATCH_GC")
        os.environ["HELM_SCRATCH_GC"] = "0"
        self.addCleanup(lambda: os.environ.__setitem__("HELM_SCRATCH_GC", _prior)
                        if _prior is not None
                        else os.environ.pop("HELM_SCRATCH_GC", None))
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
        prior_roots, prior_backups = configs.HOME_ROOTS, configs.BACKUP_DIR
        configs.HOME_ROOTS = [cdir for _label, cdir in self.dirs]
        configs.BACKUP_DIR = os.path.join(self.backup, "configs-cas")
        self.addCleanup(setattr, configs, "HOME_ROOTS", prior_roots)
        self.addCleanup(setattr, configs, "BACKUP_DIR", prior_backups)

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

    def test_hooks_apply_rederives_stale_plan_and_preserves_new_foreign_hook(self):
        plan = envtidy.plan_hooks_home("partial-com", self.partial)
        with open(os.path.join(self.partial, "settings.json"), encoding="utf-8") as f:
            changed = json.load(f)
        changed.setdefault("hooks", {}).setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": "external-after-plan"}]})
        changed["externalKey"] = {"revision": 2}
        with open(os.path.join(self.partial, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(changed, f, indent=2)
        verdict, detail = envtidy.apply_hooks_home(plan, self.backup)
        self.assertEqual(verdict, "applied", detail)
        with open(os.path.join(self.partial, "settings.json"), encoding="utf-8") as f:
            got = json.load(f)
        cmds = [c for _e, _m, c in envtidy._all_hooks(got)]
        self.assertIn("external-after-plan", cmds)
        self.assertEqual(got["externalKey"], {"revision": 2})
        for tup in envtidy.CANONICAL_HOOKS:
            self.assertTrue(hooks_mod._lane_live(got, envtidy._spec(tup)), tup)

    def test_hooks_apply_adds_missing_and_preserves_strays(self):
        r = envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertEqual([p["label"] for p in r["failed"]], ["broken-com"])
        with open(os.path.join(self.partial, "settings.json")) as f:
            got = json.load(f)
        cmds = [c for _e, _m, c in envtidy._all_hooks(got)]
        for tup in envtidy.CANONICAL_HOOKS:                 # every canonical present
            self.assertTrue(hooks_mod._lane_live(got, envtidy._spec(tup)), tup)
        # the foreign stray survived byte-for-byte
        self.assertTrue(any("repo-hygiene" in c for c in cmds))
        # the broken home was NEVER written (fail-closed)
        with open(os.path.join(self.broken, "settings.json")) as f:
            self.assertEqual(f.read(), "{ this is not json ]")
        # backup-integrity: the CAS writer captured the exact displaced file.
        self.assertTrue(any(b["orig"] == os.path.realpath(
            os.path.join(self.partial, "settings.json")) for b in configs.list_backups()))

    def test_hooks_non_conflict_write_failure_preserves_original_or_absence(self):  # noqa: VACUOUS_ASSERTION — existing-file control precedes missing-file absence arm
        """The atomic config writer owns rollback; the domain layer never restores."""
        partial_path = os.path.join(self.partial, "settings.json")
        with open(partial_path, "rb") as f:
            original = f.read()
        failure = {"error": "injected stage failure", "code": "stage"}
        with mock.patch.object(configs, "write_file", return_value=failure):
            verdict, _detail = envtidy.apply_hooks_home(
                envtidy.plan_hooks_home("partial-com", self.partial), self.backup)
        self.assertEqual(verdict, "FAIL")
        with open(partial_path, "rb") as f:
            self.assertEqual(f.read(), original)

        bare_path = os.path.join(self.croot, "bare-com", "settings.json")
        self.assertFalse(os.path.exists(bare_path))
        with mock.patch.object(configs, "write_file", return_value=failure):
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

    def test_spec_inherits_the_gate_flag_so_sync_cannot_disarm_the_stop_guard(self):
        """A canonical tuple carries four fields; a real spec carries more, and
        one of them is `gate` — what makes the stop guard's exit-2 refusal reach
        the harness. `_spec` used to REBUILD from the tuple and drop it, so sync
        computed the fail-open `|| true` as canonical and `--apply` re-disarmed
        the gate in every home where it had just been fixed (measured live
        2026-07-26: 10 of 12 homes reported as drifted from a CORRECT estate).

        Asserted against hooks.SPECS rather than against `_spec`'s own output.
        The pre-existing `_cmd` helper is spec_command(_spec(tup)), so every
        other test in this file derived its expected command from the very
        builder that was broken and stayed green — a self-consistent oracle
        cannot see this class, only an independent source can."""
        canon = {(sp["event"], sp["args"]): sp for sp in hooks_mod.SPECS}
        gated = [k for k, sp in canon.items() if sp.get("gate")]
        self.assertTrue(gated, "hooks.SPECS declares no gated spec — this test "
                               "would be vacuous; a gate was removed upstream")
        for tup in envtidy.CANONICAL_HOOKS:
            key = (tup[0], tup[2])
            if key not in canon:
                continue                      # tuple-only (record, or override)
            spec = envtidy._spec(tup)
            for field, want in canon[key].items():
                if field in ("timeout", "matcher"):
                    continue                  # the tuple owns cadence/placement
                self.assertEqual(spec.get(field), want,
                                 "_spec dropped %r for %s" % (field, key))
            cmd = hooks_mod.spec_command(spec)
            if canon[key].get("gate"):
                # THE REFUSAL IS THE WRAPPER'S `gate` KIND: the rc ladder
                # lives in bin/helm-hook and only that kind propagates rc 2.
                self.assertEqual(shlex.split(cmd)[1], "gate",
                                 "gated spec lost its refusal: %s" % (key,))
                self.assertNotIn("|| true", cmd,
                                 "sync would REWRITE the gate's refusal to "
                                 "success for %s" % (key,))

    def test_helm_args_discards_the_gate_tail_not_just_or_true(self):
        """A hook's identity is the helm subcommand it runs; how its exit code is
        handled afterwards is mechanism. `_helm_args` stripped `|| true` only, so
        a GATED command (`...; rc=$?; [ "$rc" = 2 ] && exit 2; exit 0`) carried
        its whole shell tail into the identity and the census reported the
        stop-guard hook MISSING from every home where the gate was correctly
        installed — a third surface misreporting the same fix."""
        bin_ = os.path.join(self.tmp, "bin", "helm")   # never a real home path
        want = "chat stop-guard --hook-json"
        gated = 'timeout 5 %s %s; rc=$?; [ "$rc" = 2 ] && exit 2; exit 0' % (bin_, want)
        self.assertEqual(envtidy._helm_args(gated), want)
        # the fail-open form must keep parsing identically — same identity
        self.assertEqual(envtidy._helm_args("timeout 5 %s %s || true" % (bin_, want)), want)
        # and a real gated spec, built by the shipped builder, round-trips
        for tup in envtidy.CANONICAL_HOOKS:
            self.assertEqual(envtidy._helm_args(_cmd(tup)), tup[2],
                             "identity lost for %s" % (tup,))
        # non-helm commands are still not helm
        self.assertIsNone(envtidy._helm_args("/usr/local/bin/repo-hygiene --quick"))

    def test_every_canonical_tuple_agrees_with_the_spec_it_mirrors(self):
        """TWO REPRESENTATIONS OF ONE FACT, AND `sync --apply` INSTALLS FROM
        THIS ONE. `_spec` deliberately lets the tuple own cadence, so a budget
        corrected in hooks.SPECS alone is silently rewritten back on the next
        sync across every home — measured on this lane: the stop-guard budget
        was re-derived to 20 in SPECS while this tuple still said 5, and
        nothing failed.

        The override seam still lets an operator set any cadence; what this
        forbids is the SHIPPED pair disagreeing with itself.
        """
        from helm import hooks as _hooks

        def spec_for(event, args):
            return next((sp for sp in _hooks.SPECS
                         if sp["event"] == event and sp["args"] == args), None)

        # PAIRS ACTUALLY COMPARED, as (event, args, tuple_timeout,
        # spec_timeout). Building the list first means ONE assertion carries
        # both the coverage and the agreement: a run that resolved nothing
        # produces an empty list and fails against a non-empty expectation,
        # so "no disagreements" can never mean "nothing was compared".
        compared = [(e, a, t, spec_for(e, a)["timeout"])
                    for e, _m, a, t in envtidy.CANONICAL_HOOKS
                    if spec_for(e, a) is not None]
        self.assertTrue(compared,
                        "no canonical tuple resolved to a spec, so this arm "
                        "compared nothing")
        self.assertEqual(
            [row for row in compared if row[2] != row[3]], [],
            "CANONICAL_HOOKS and hooks.SPECS disagree on a budget; sync "
            "installs the former, so the latter is decorative. Compared "
            "%d pairs." % len(compared))
        self.assertIn(
            "chat stop-guard --hook-json", [row[1] for row in compared],
            "MUST-HIT: the stop-guard tuple did not resolve to a spec, so "
            "the pair this arm exists for was never compared")

    def test_sync_installs_the_re_derived_stop_guard_budget(self):
        """THE CONTROL A REVIEW ASKED FOR: prove the number that reaches a home
        through the SYNC path is the re-derived one, not the old default. The
        parity arm above compares two tables; this one follows the value out
        to the artifact an agent actually runs under."""
        from helm import hooks as _hooks
        spec = next(s for s in _hooks.SPECS if s["name"] == "stop-guard")
        tup = next(t for t in envtidy.CANONICAL_HOOKS
                   if t[2] == "chat stop-guard --hook-json")
        built = envtidy._spec(tup)
        self.assertEqual(built["timeout"], spec["timeout"])
        with mock.patch.object(_hooks, "helm_bin", return_value="/b/helm"):
            command = _hooks.spec_command(built)
            entry = _hooks._canonical_entry(built)
            # INSIDE the patch: `_wrapper_prefix` resolves the wrapper from
            # helm_bin, so computing it outside compares two different
            # checkouts and is red for a reason that is not the budget.
            prefix = _hooks._wrapper_prefix(built, "gate", "stop")
        self.assertTrue(
            command.startswith(prefix + " "),
            "sync would install a stop-guard under a different budget than "
            "the one SPECS derives: %s" % command)
        self.assertEqual(entry["hooks"][0].get("timeout"),
                         _hooks._gate_outer_timeout(built),
                         "the harness grace installed by sync is not the one "
                         "derived from the budget")

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
        self._abandon(self.wt_dirty)

    def _abandon(self, path):
        """Back-date the dirty paths so the room reads as ABANDONED.

        THE SHAPE LESSON FROM THE LANE NEXT DOOR, APPLIED HERE BECAUSE IT WAS
        NOT. `_wip_commit` now refuses an AUTONOMOUS write to a room written
        inside `_gc._RESCUE_ACTIVE_S`, and a fixture that seeds a room and
        writes to it microseconds later is the freshest room there is. I fixed
        exactly this premise in tests/test_work.py's `room()` and did not carry
        it to this file, so the gate came back red on two envtidy arms that
        assert a rescue — arms whose ROOMS are meant to be long abandoned.

        A SINGLE TEST METHOD CANNOT SEE SUITE-WIDE DAMAGE: I ran the GcTest
        class green and shipped, and the blast radius of the change was every
        caller of the shared actuator, in three files.

        DERIVED FROM THE CONSTANT, never a copy of it.
        """
        from helm.work import _gc
        cutoff = time.time() - (_gc._RESCUE_ACTIVE_S + 60)
        r = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=path, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        rels = [f[3:] for f in r.stdout.split(b"\0") if len(f) > 3]
        self.assertTrue(rels, "_abandon() found no dirty path in %s" % path)
        for rel in rels:
            target = os.path.join(os.fsencode(path), rel)
            if os.path.lexists(target) and not os.path.islink(target):
                os.utime(target, (cutoff, cutoff))

    def _branches(self):
        r = _sh(self.root, "git", "for-each-ref", "--format=%(refname:short)",
                "refs/heads/")
        return set(r.stdout.split())

    def _worktree_paths(self):
        return {w["path"] for w in envtidy._worktree_rows(self.root, "main")}

    def test_registry_failure_is_UNAVAILABLE_not_empty_success(self):  # noqa: VACUOUS_ASSERTION — the explicit registry error and nonzero status prove the mocked failure path fired before the no-post assertion
        from helm import chat, vcs
        backend = vcs.backend(self.root)
        with mock.patch.object(backend, "worktrees",
                               return_value=([], "simulated registry failure")), \
                mock.patch.object(chat, "post") as post, \
                mock.patch.object(envtidy, "_print_worktree") as render:
            rc = envtidy.cmd_worktree(["gc", "--apply", "--repo", self.root])
        self.assertEqual(rc, 1)
        report = render.call_args.args[0]
        self.assertIn("registry unavailable", report["error"])
        post.assert_not_called()

    def test_linked_repo_target_uses_main_root_and_is_never_reaped(self):  # noqa: VACUOUS_ASSERTION — real Git additions prove both records exist before apply; the post-pass registry and explicit keep row are positive controls
        from helm import work
        driver = self.root + "-wt/driver"
        peek = os.path.join(self.root + "-wt", "peeks", "deadbeef0000")
        os.makedirs(os.path.dirname(driver), exist_ok=True)
        os.makedirs(os.path.dirname(peek), exist_ok=True)
        self.assertEqual(_sh(self.root, "git", "worktree", "add", "-q", "-b",
                             "lane/driver", driver, "main").returncode, 0)
        self.assertEqual(_sh(self.root, "git", "worktree", "add", "-q",
                             "--detach", peek, "main").returncode, 0)
        shutil.rmtree(peek)
        alias = os.path.join(self.tmp, "driver-link")
        os.symlink(driver, alias)

        result = envtidy.worktree_gc(root=alias, apply=True)
        self.assertEqual(result["root"], self.root)
        registered = {w["path"] for w in work.worktrees(self.root)}
        self.assertIn(driver, registered,
                      "the linked checkout named by --repo was reaped")
        self.assertIn(peek, registered,
                      "canonical ownership was not applied to the peek record")
        row = next(r for r in result["lane_rows"] if r["path"] == driver)
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("TARGET named by --repo", row["why"])

    def test_phantom_records_have_exactly_one_cleanup_owner(self):  # noqa: VACUOUS_ASSERTION — real Git records are positively classified and reported removed before the complementary registry-absence assertions
        from helm import work
        paths = {
            "lane": self.root + "-wt/ghost-lane",
            "harness": os.path.join(self.root, ".claude", "worktrees",
                                    "agent-ghost"),
            "estate": os.path.join(self.tmp, "estate-ghost"),
            "peek": os.path.join(self.root + "-wt", "peeks", "peek-ghost"),
        }
        for path in paths.values():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            r = _sh(self.root, "git", "worktree", "add", "-q", "--detach",
                    path, "main")
            self.assertEqual(r.returncode, 0, r.stderr)
            shutil.rmtree(path)

        self.assertEqual(set(work.phantom_records(self.root)),
                         {paths["lane"], paths["harness"]})
        self.assertEqual(work.estate_phantom_records(self.root),
                         [paths["estate"]])
        result = envtidy.worktree_gc(root=self.root, apply=True)
        self.assertEqual(set(result["managed_phantom_removed"]),
                         {paths["lane"], paths["harness"]})
        self.assertEqual(result["estate_phantom_removed"], [paths["estate"]])
        registered = {w["path"] for w in work.worktrees(self.root)}
        self.assertIn(paths["peek"], registered,
                      "peek records belong only to `helm work peek --drop`")
        self.assertNotIn(paths["lane"], registered)
        self.assertNotIn(paths["harness"], registered)
        self.assertNotIn(paths["estate"], registered)
        self.assertEqual(result["lane_rows"], [],
                         "managed phantoms also entered ordinary room accounting")
        from helm import chat
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(chat, "post"):
            self.assertIsNone(envtidy._post_worktree_summary(result))
        self.assertIn("removed=3 kept=0 triage=0", out.getvalue())

    def test_harness_room_has_one_gc_owner(self):
        """`.claude/worktrees/*` is delegated to lease-aware work.gc; the
        estate sweep must not classify the same room a second time."""
        from helm import work
        path = os.path.join(self.root, ".claude", "worktrees", "agent-clean")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "agent-clean", path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(path, {row["path"] for row in
                                envtidy._worktree_rows(self.root, "main")})
        lane = next(row for row in work.gc_scan(self.root) if row["path"] == path)
        self.assertEqual(lane["verdict"], "remove")
        result = envtidy.worktree_gc(root=self.root, apply=True)
        self.assertNotIn(path, {row["path"] for row in result["worktree_rows"]})
        self.assertFalse(os.path.exists(path))

    def test_apply_cli_posts_one_owner_summary(self):
        from helm import chat
        self._seed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(chat, "post") as post:
            rc = envtidy.cmd_worktree(["gc", "--apply", "--repo", self.root])
        self.assertEqual(rc, 0)
        post.assert_called_once_with(
            "worktree gc proj: removed=2 kept=4 triage=3", who="worktree-gc")
        self.assertIn("removed=2 kept=4 triage=3", out.getvalue())

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

    def test_apply_retires_merged_lane_stub_keeps_unlanded_lane(self):
        self._branch_at_main("lane/merged-old")
        self.assertEqual(_sh(self.root, "git", "checkout", "-q", "-b",
                             "lane/unlanded-old").returncode, 0)
        self._commit("lane.txt", "still needs integration")
        self.assertEqual(_sh(self.root, "git", "checkout", "-q", "main").returncode, 0)
        envtidy.worktree_gc(root=self.root, apply=True)
        branches = self._branches()
        self.assertNotIn("lane/merged-old", branches)
        self.assertIn("lane/unlanded-old", branches)

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
        # THE ARM DIRTIES A ROOM AFTER THE SEED, so it needs the same aging:
        # this room is standing in for one nobody has touched, and the
        # actuator now refuses an autonomous write to a room written seconds
        # ago. Ageing it is what the arm always MEANT.
        self._abandon(self.wt_rm)
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
    def test_optional_hook_plans_still_render_persistent_refusals(self):
        warning = "REFUSED /fake/home [non-whole-PostToolUse-member]"
        steady = {"label": "steady", "verdict": "ok", "refusal_detail": warning}
        changed = {"label": "changed", "verdict": "change", "actions": {"deliver": "add"},
                   "refusal_detail": warning + " changed"}
        report = {"changed": [changed], "failed": [], "steady": 1,
                  "backup_root": "/backup", "plans": [steady, changed]}
        for detailed in (False, True):
            with self.subTest(detailed=detailed):
                if detailed:
                    changed["detail"] = "synthetic result; " + changed["refusal_detail"]
                with mock.patch.object(envtidy, "hooks_sync", return_value=report) as run, \
                        contextlib.redirect_stdout(io.StringIO()) as text:
                    self.assertEqual(envtidy.cmd_hooks_sync([]), 0)
                run.assert_called_once_with(apply=False)
                self.assertEqual(text.getvalue().count(warning), 2)
                self.assertEqual(text.getvalue().count(warning + " changed"), 1)
                self.assertIn("1 changed, 1 already canonical, 0 failed", text.getvalue())

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
                     "lane_rows": [], "lane_lines": {},
                     "managed_phantoms": [], "managed_phantom_removed": [],
                     "managed_phantom_error": None,
                     "managed_phantom_unknown": False,
                     "estate_phantoms": [], "estate_phantom_removed": [],
                     "estate_phantom_error": None,
                     "estate_phantom_unknown": False, "worktree_rows": [],
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

    def test_UNKNOWN_phantom_result_posts_no_known_summary(self):  # noqa: VACUOUS_ASSERTION — UNKNOWN diagnostics, rc=1, and the exact one-failure report prove both suppressor paths fired before asserting no post
        from helm import chat
        report = {"root": "/repo", "base": "main", "apply": True,
                  "lane_rows": [], "lane_lines": {},
                  "managed_phantoms": ["/repo-wt/ghost"],
                  "managed_phantom_removed": [],
                  "managed_phantom_error": "removal is UNKNOWN",
                  "managed_phantom_unknown": True,
                  "estate_phantoms": [], "estate_phantom_removed": [],
                  "estate_phantom_error": None,
                  "estate_phantom_unknown": False, "worktree_rows": [],
                  "worktree_lines": {}, "orphans": [], "orphan_lines": {}}
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(chat, "post") as post:
            error = envtidy._post_worktree_summary(report)
        self.assertIn("UNKNOWN", error)
        self.assertIn("summary not posted", err.getvalue())
        post.assert_not_called()

        hooks = {"changed": [], "failed": [], "steady": 0,
                 "backup_root": "/backup"}
        mcp = {"changed": [], "failed": [], "steady": 0,
               "backup_root": "/backup"}
        tidy_report = {
            "apply": True, "backup_root": "/backup",
            "census": {"homes": [], "hooks_variance": {
                "missing_by_hook": {}, "strays_by_home": {}},
                "mcp_variance": {"universal_effective": [],
                                 "canonical_names": [],
                                 "missing_by_home": {}},
                "worktrees": {"note": "ok"}},
            "hooks": hooks, "mcp": mcp, "worktree": report}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(envtidy, "tidy", return_value=tidy_report), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(chat, "post") as post:
            rc = envtidy.cmd_tidy(["--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("1 FAILED", out.getvalue())
        self.assertNotIn("2 FAILED", out.getvalue())
        self.assertIn("summary not posted", err.getvalue())
        post.assert_not_called()

    def test_tidy_apply_propagates_phantom_failure(self):
        hooks = {"changed": [], "failed": [], "steady": 0,
                 "backup_root": "/backup"}
        mcp = {"changed": [], "failed": [], "steady": 0,
               "backup_root": "/backup"}
        worktrees = {"root": "/repo", "base": "main", "apply": True,
                     "lane_rows": [], "lane_lines": {},
                     "managed_phantoms": ["/repo-wt/ghost"],
                     "managed_phantom_removed": [],
                     "managed_phantom_error": "simulated removal failure",
                     "managed_phantom_unknown": False,
                     "estate_phantoms": [], "estate_phantom_removed": [],
                     "estate_phantom_error": None,
                     "estate_phantom_unknown": False, "worktree_rows": [],
                     "worktree_lines": {}, "orphans": [], "orphan_lines": {}}
        report = {"apply": True, "backup_root": "/backup",
                  "census": {"homes": [], "hooks_variance": {
                      "missing_by_hook": {}, "strays_by_home": {}},
                      "mcp_variance": {"universal_effective": [],
                                       "canonical_names": [],
                                       "missing_by_home": {}},
                      "worktrees": {"note": "ok"}},
                  "hooks": hooks, "mcp": mcp, "worktree": worktrees}
        rendered = io.StringIO()
        envtidy._print_worktree(worktrees, out=rendered)
        self.assertIn("KEPT", rendered.getvalue())
        out = io.StringIO()
        with mock.patch.object(envtidy, "tidy", return_value=report), \
                mock.patch.object(envtidy, "_post_worktree_summary",
                                  return_value=None), \
                contextlib.redirect_stdout(out):
            rc = envtidy.cmd_tidy(["--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("1 FAILED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
