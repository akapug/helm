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
from helm.work import _gc  # noqa: E402
from tests import _roomclock  # noqa: E402


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


# The lean delivery subset seats were reconciled to before task/3089, spelled
# literally: the module no longer carries it, and a fixture seat wearing it is
# the shape every live seat had when the owner ruled a seat a full agent.
OLD_LEAN_ARGS = ("chat deliver --hook-json", "chat join --hook-json",
                 "chat stop-guard --hook-json")


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

        # seats: one carrying only the OLD lean delivery subset (the shape
        # every live seat had before task/3089), one bare.
        lean = tuple(h for h in envtidy.CANONICAL_HOOKS if h[2] in OLD_LEAN_ARGS)
        self.assertEqual(len(lean), 3)          # the fixture really is the old subset
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
        # a seat is a full agent: the old lean subset is NOT complete — the
        # recorder legs, inject and both handoff producers read as missing
        for want in ("PostToolUse record --hook-json",
                     "PostToolUseFailure record --hook-json",
                     "UserPromptSubmit inject --hook-json",
                     "PreCompact handoff check --hook-json"):
            self.assertIn(want, by["seat:codex"]["missing"])
        self.assertEqual(len(by["seat:codex"]["missing"]),
                         len(envtidy.CANONICAL_HOOKS) - len(OLD_LEAN_ARGS))
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

    def _mcp_canon(self, mapping, mode=0o600, name="mcp-canon.json"):
        """A canonical MCP file. 0600 by default: a file anyone but its
        owner can reach is REFUSED (task/3089), so every arm that wants its
        configs read must make the file private — as the operator must."""
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            json.dump(mapping, f)
        os.chmod(p, mode)
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

    def test_mcp_mid_write_failure_restores_original(self):  # noqa: VACUOUS_ASSERTION — the FAIL verdict and the byte-equal pre-image read are unconditional positives on the same state file
        state = os.path.join(self.whole, ".claude.json")
        _write(state, {"mcpServers": {"pre-existing": {"command": "keep-me"}}})
        with open(state, "rb") as f:
            original = f.read()
        os.environ["HELM_MCPS_CANONICAL"] = self._mcp_canon(
            {"foo-mcp": {"command": "foo"}})

        def mutate_then_fail(path, _text, **_kw):
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
# task/3089 — a seat is a full agent: servers, hooks, global instructions
# ---------------------------------------------------------------------------

SECRET = "FIXTURE-BEARER-SENTINEL-3089"  # gitleaks:allow (a test fixture, not a credential)


class SeatFullAgentTest(unittest.TestCase):
    """Every seat config dir is planned like a home. The fixture estate is the
    shape the owner measured: a codex-family seat wearing the old lean hook
    subset whose launch runs the CLAUDE harness (seat:codex — planned), a
    codex instance with no launch script (unsure — planned), a bare seat with
    no servers (seat:kimi), and one seat whose launch stamps a NON-claude
    harness (seat:seat-b — the only exclusion). Every arm plants the violating
    state and asserts the guard FIRES on it, beside a positive control on the
    same observable so no arm can pass by observing nothing. Borrows
    EstateBase's fixture WITHOUT inheriting its arms (they run once, in
    EstateBase)."""

    _mcp_canon = EstateBase._mcp_canon

    def setUp(self):
        EstateBase.setUp(self)
        quiet = mock.patch.object(envtidy, "_worktree_census",
                                  return_value={"note": "fixture"})
        quiet.start()
        self.addCleanup(quiet.stop)
        j = os.path.join
        self.kimi = j(self.seats, "kimi", "claude")
        self.codex = j(self.seats, "codex", "claude")
        # the measured codex launch shape: the claude harness on a codex model
        self._launch(self.codex, "claude")
        # a codex INSTANCE with no launch script yet: UNSURE, so planned
        self.codex_inst = j(self.seats, "codex", "instances", "seat-a", "claude")
        os.makedirs(self.codex_inst)
        # the one seat whose launch runs a harness that is not Claude Code
        self.other = j(self.seats, "seat-b", "claude")
        os.makedirs(self.other)
        self._launch(self.other, "pi")
        self.dirs = skillsync.config_dirs(
            claude_root=self.croot, default_claude=self.default,
            seats_root=self.seats)
        configs.HOME_ROOTS = [cdir for _label, cdir in self.dirs]
        self.canon = {"alpha-mcp": {"command": "alpha"},
                      "beta-mcp": {"type": "http", "url": "https://x.invalid/mcp",
                                   "headers": {"Authorization": "Bearer " + SECRET}}}

    def _env(self, **kw):
        """Point the MCP / instructions sources at fixtures for one arm; every
        other source stays at the suite's planted `off`."""
        return mock.patch.dict(os.environ, kw)

    @staticmethod
    def _launch(cdir, *harnesses):
        """A seat launch script beside `cdir` shaped like the minted one: the
        harness stamp rides an `env` word list, beside a token export."""
        stamps = " ".join("HELM_AGENT_HARNESS=%s" % h for h in harnesses)
        with open(os.path.join(os.path.dirname(cdir), "launch.sh"), "w") as f:
            f.write("#!/bin/sh\nexport ANTHROPIC_AUTH_TOKEN=\"$(cat token)\"\n"
                    "exec env HELM_CHAT_NAME=seat-c %s "
                    "CLAUDE_CONFIG_DIR=%s claude --model m \"$@\"\n"
                    % (stamps, cdir))

    def _state(self, cdir):
        with open(os.path.join(cdir, ".claude.json")) as f:
            return json.load(f)

    # ---- MCP: a seat is planned, not skipped --------------------------------

    def test_a_seat_with_zero_servers_is_planned_not_skipped(self):
        canon = self._mcp_canon(self.canon)
        with self._env(HELM_MCPS_CANONICAL=canon):
            plan = envtidy.plan_mcp_home("seat:kimi", self.kimi)
            # THE VIOLATION: zero servers. The old planner answered `skip`.
            self.assertEqual(plan["verdict"], "change")
            self.assertEqual(sorted(plan["adds"]), ["alpha-mcp", "beta-mcp"])
            # mutation control: only a non-claude harness stamp makes a seat
            # skip — plant one on this seat and it is excluded, then remove it
            self._launch(self.kimi, "pi")
            self.assertEqual(envtidy.plan_mcp_home(
                "seat:kimi", self.kimi)["verdict"], "excluded")
            os.remove(os.path.join(os.path.dirname(self.kimi), "launch.sh"))
            verdict, detail = envtidy.apply_mcp_home(plan, self.backup)
            self.assertEqual(verdict, "applied", detail)
            self.assertEqual(sorted(self._state(self.kimi)["mcpServers"]),
                             ["alpha-mcp", "beta-mcp"])
            # the written state holds a credential, so it is born private
            mode = os.stat(os.path.join(self.kimi, ".claude.json")).st_mode
            self.assertEqual(mode & 0o777, 0o600)
            self.assertEqual(envtidy.plan_mcp_home("seat:kimi", self.kimi)["verdict"],
                             "ok")                                 # now canonical

    def _window(self, cdir, *windows):
        """A claude-harness launch beside `cdir` stamping each window, in the
        names launch_line writes (test_the_window_is_read_from_a_REAL_launch
        pins that the two agree)."""
        stamps = " ".join("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d "
                          "CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % (w, w)
                          for w in windows)
        with open(os.path.join(os.path.dirname(cdir), "launch.sh"), "w") as f:
            f.write("#!/bin/sh\nexec env HELM_AGENT_HARNESS=claude %s "
                    "CLAUDE_CONFIG_DIR=%s claude \"$@\"\n" % (stamps, cdir))

    def test_a_SMALL_WINDOW_seat_takes_the_web_search_floor_only(self):
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "alpha-mcp": {"command": "alpha"},
                                 "polyana": {"command": "pa"}})
        with self._env(HELM_MCPS_CANONICAL=canon):
            # the measured local seat: a 115k window, zero servers
            self._window(self.kimi, 115072)
            plan = envtidy.plan_mcp_home("seat:kimi", self.kimi)
            self.assertEqual(plan["adds"], ["exa"])        # web search, always
            self.assertEqual(sorted(n for n, _ in plan["withheld"]),
                             ["alpha-mcp", "polyana"])
            self.assertIn("115072", dict(plan["withheld"])["polyana"])
            # control: a 1M window carries the whole set
            self._window(self.kimi, 1000000)
            self.assertEqual(sorted(envtidy.plan_mcp_home(
                "seat:kimi", self.kimi)["adds"]), ["alpha-mcp", "exa", "polyana"])
            # disagreeing stamps are UNSURE, and an unsure seat is planned whole
            self._window(self.kimi, 115072, 1000000)
            self.assertEqual(len(envtidy.plan_mcp_home(
                "seat:kimi", self.kimi)["adds"]), 3)
            # a server already present above the floor is surfaced, not removed
            self._window(self.kimi, 115072)
            _write(os.path.join(self.kimi, ".claude.json"),
                   {"mcpServers": {"exa": {"command": "exa"},
                                   "polyana": {"command": "pa"}}})
            os.chmod(os.path.join(self.kimi, ".claude.json"), 0o600)
            plan = envtidy.plan_mcp_home("seat:kimi", self.kimi)
            self.assertEqual(plan["verdict"], "change")
            self.assertEqual([n for n, _ in plan["refused"]], ["polyana"])
            self.assertIn("REMOVE polyana by hand", envtidy._mcp_bits(plan))
            self.assertEqual(plan["adds"], [])
            # a home is never budgeted: no launch stamps a window for it
            self.assertEqual(envtidy.mcp_withheld("home:x", self.kimi, canon), {})

    def test_a_LOCAL_GPU_seat_takes_the_floor_whatever_its_window(self):
        """A local seat's schemas cost PREFILL on the owner's own GPU, every
        request, main and subagents alike. MEASURED: qwenlocal's 229,376
        window cleared the share by about 2k, so it took the whole set (about
        20k tokens of schemas on a 1.4k tok/s prefill), and the local seats
        made 3 MCP calls in 1,512."""
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "alpha-mcp": {"command": "alpha"},
                                 "polyana": {"command": "pa"}})
        local = os.path.join(self.seats, "qwenlocal", "claude")
        os.makedirs(local)
        with self._env(HELM_MCPS_CANONICAL=canon):
            # CONTROL: a vendor seat on the same window carries the whole set
            self._window(self.kimi, 229376)
            self.assertEqual(sorted(envtidy.plan_mcp_home(
                "seat:kimi", self.kimi)["adds"]), ["alpha-mcp", "exa", "polyana"])
            self._window(local, 229376)
            plan = envtidy.plan_mcp_home("seat:qwenlocal", local)
            self.assertEqual(plan["adds"], ["exa"])        # web search, always
            self.assertEqual(sorted(n for n, _ in plan["withheld"]),
                             ["alpha-mcp", "polyana"])
            self.assertIn("prefill", dict(plan["withheld"])["polyana"])

    def test_a_LITE_seat_takes_the_floor_wherever_it_is_served(self):  # noqa: VACUOUS_ASSERTION — the plan's adds equality ['exa'] and its withheld equality are unconditional positives on the same plan; assertFalse(own_box) is the arm's precondition, and the control's three-server equality follows
        """task/3253: the lite profile's `mcp_floor` holds on its own, not
        only through the own-box arm above. A planted family served from a
        vendor URL (so not the operator's box) on a window that carries the
        whole set takes the floor while it declares the lite profile, with
        the profile's reason; the same family without the profile takes the
        whole set (the control on the same observable). The live FAMILIES is
        patched for the arm and restored."""
        from helm import seat, seat_catalog     # noqa: F401 — seat seeds the impl
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "alpha-mcp": {"command": "alpha"},
                                 "polyana": {"command": "pa"}})
        remote = {"mode": "proxy-key", "pool_default": "vendor",
                  "pool_providers": {"vendor": {
                      "base_url": "http://192.0.2.99/v1", "rung": "paid"}}}
        cdir = os.path.join(self.seats, "fam-lite", "claude")
        os.makedirs(cdir)
        self._window(cdir, 1000000)
        with self._env(HELM_MCPS_CANONICAL=canon):
            with mock.patch.dict(seat_catalog.FAMILIES,
                                 {"fam-lite": dict(remote, profile="lite")}):
                self.assertFalse(seat_catalog.own_box(
                    seat_catalog.FAMILIES["fam-lite"]))
                plan = envtidy.plan_mcp_home("seat:fam-lite", cdir)
                self.assertEqual(plan["adds"], ["exa"])
                self.assertEqual(sorted(n for n, _ in plan["withheld"]),
                                 ["alpha-mcp", "polyana"])
                self.assertIn("lite profile", dict(plan["withheld"])["polyana"])
            with mock.patch.dict(seat_catalog.FAMILIES, {"fam-lite": remote}):
                self.assertEqual(sorted(envtidy.plan_mcp_home(
                    "seat:fam-lite", cdir)["adds"]),
                    ["alpha-mcp", "exa", "polyana"])

    def test_the_local_floor_follows_the_family_not_the_dir_and_only_surfaces(self):
        """The own-box arm reads the FAMILY a label parses to: a project
        instance of a local family is floored like its family, a seat whose
        family the catalog does not hold is left to its window, and a server
        the local seat already carries is surfaced for removal, never
        removed."""
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "alpha-mcp": {"command": "alpha"},
                                 "polyana": {"command": "pa"}})
        inst = os.path.join(self.seats, "qwenlocal", "instances",
                            "helm-qwenlocal", "claude")
        stray = os.path.join(self.seats, "nosuchfamily", "claude")
        local = os.path.join(self.seats, "qwenlocal", "claude")
        for d in (inst, stray, local):
            os.makedirs(d)
            self._window(d, 229376)
        with self._env(HELM_MCPS_CANONICAL=canon):
            self.assertEqual(envtidy.plan_mcp_home(
                "seat:qwenlocal/helm-qwenlocal", inst)["adds"], ["exa"])
            # CONTROL: no catalog row, so only the window decides
            self.assertEqual(sorted(envtidy.plan_mcp_home(
                "seat:nosuchfamily", stray)["adds"]),
                ["alpha-mcp", "exa", "polyana"])
            _write(os.path.join(local, ".claude.json"),
                   {"mcpServers": {"exa": {"command": "exa"},
                                   "polyana": {"command": "pa"}}})
            os.chmod(os.path.join(local, ".claude.json"), 0o600)
            plan = envtidy.plan_mcp_home("seat:qwenlocal", local)
            self.assertEqual(plan["verdict"], "change")
            self.assertEqual([n for n, _ in plan["refused"]], ["polyana"])
            bits = envtidy._mcp_bits(plan)
            self.assertIn("REMOVE polyana by hand", bits)
            self.assertIn("prefill", bits)
            self.assertEqual(envtidy.apply_mcp_home(plan, self.backup)[0], "ok")
            self.assertIn("polyana", self._state(local)["mcpServers"])

    def test_the_window_is_read_from_a_REAL_launch_line(self):
        # The stamp comes from the generator, never a hand-typed fixture: a
        # bare MAX_CONTEXT_TOKENS fixture once passed while every real seat
        # read UNSURE and took the whole set.
        from helm import seat  # noqa: F401  (the facade seeds the impl modules)
        from helm.seat_launch_assets import launch_line
        launch = os.path.join(os.path.dirname(self.kimi), "launch.sh")
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec %s\n"
                    % launch_line("codex", model="gpt-5.3-codex-spark"))
        self.assertEqual(envtidy.seat_window(self.kimi), 76000)
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "polyana": {"command": "pa"}})
        with self._env(HELM_MCPS_CANONICAL=canon):
            self.assertEqual(envtidy.plan_mcp_home("seat:kimi", self.kimi)["adds"],
                             ["exa"])
        # control: a bare name inside another word is not the stamp
        with open(launch, "w") as f:
            f.write("#!/bin/sh\nexec env X_MAX_CONTEXT_TOKENS=76000 claude\n")
        self.assertIsNone(envtidy.seat_window(self.kimi))

    def test_a_settings_env_pin_outranks_the_launch_stamp(self):
        """The window and output cap a seat RUNS with, not only the ones its
        launch stamps (task/3184). Claude Code applies the `env` block of the
        seat's own settings.json over the launch environment (MEASURED on a
        live local seat whose window was pinned there), so a pin in that file
        is the effective value.

        Each arm plants the case and reads the same knob, so no arm passes by
        reading nothing: the launch stamp alone; a settings pin over it; a pin
        that is not a token count (UNSURE, never a silent fall back to the
        launch stamp that the pin was meant to replace); a settings file with
        no such key (the launch stamp again); and a settings file that does
        not parse (UNSURE, because nobody can say which value Claude Code
        took)."""
        name = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
        settings = os.path.join(self.kimi, "settings.json")
        self._window(self.kimi, 229376)
        self.assertEqual(envtidy.seat_stamp(self.kimi, name),
                         (229376, "launch.sh"))
        _write(settings, {"env": {name: "196608"}})
        self.assertEqual(envtidy.seat_stamp(self.kimi, name),
                         (196608, "settings.json"))
        self.assertEqual(envtidy.seat_window(self.kimi), 196608)
        _write(settings, {"env": {name: "lots"}})
        self.assertIsNone(envtidy.seat_stamp(self.kimi, name)[0])
        self.assertIsNone(envtidy.seat_window(self.kimi))
        _write(settings, {"env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384"}})
        self.assertEqual(envtidy.seat_stamp(self.kimi, name),
                         (229376, "launch.sh"))
        self.assertEqual(
            envtidy.seat_stamp(self.kimi, "CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
            (16384, "settings.json"))
        with open(settings, "w") as f:
            f.write("{not json")
        self.assertIsNone(envtidy.seat_stamp(self.kimi, name)[0])

    def test_a_backup_shelf_is_private_and_the_copy_keeps_its_mode(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        root = os.path.join(self.backup, "fresh-root")
        os.makedirs(root, mode=0o755)
        src = os.path.join(self.kimi, ".claude.json")
        _write(src, {"mcpServers": {"beta-mcp": {"headers": {
            "Authorization": "Bearer " + SECRET}}}})
        os.chmod(src, 0o644)                                 # a loose pre-image
        dst = envtidy._backup(root, "seat:kimi", src)
        for d in (root, os.path.dirname(dst)):
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)
        self.assertEqual(os.stat(dst).st_mode & 0o777, 0o644)  # rollback exact
        with open(dst) as f:
            self.assertIn(SECRET, f.read())                  # the bytes, whole

    def test_a_server_the_family_API_REFUSES_is_never_written(self):
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "polyana": {"command": "pa"}})
        gem = os.path.join(self.seats, "gemini", "claude")
        os.makedirs(gem)
        self._window(gem, 1000000)
        refusal = {"gemini": {"polyana": "a fixture refusal: HTTP 400"}}
        with self._env(HELM_MCPS_CANONICAL=canon), \
                mock.patch.dict(envtidy.MCP_FAMILY_REFUSES, refusal):
            plan = envtidy.plan_mcp_home("seat:gemini", gem)
            self.assertEqual(plan["adds"], ["exa"])
            self.assertIn("400", dict(plan["withheld"])["polyana"])
            # control: the same window on another family takes polyana
            self._window(self.kimi, 1000000)
            self.assertIn("polyana", envtidy.plan_mcp_home(
                "seat:kimi", self.kimi)["adds"])
            # present already -> surfaced for removal, and the sync adds nothing
            _write(os.path.join(gem, ".claude.json"),
                   {"mcpServers": {"exa": {"command": "exa"},
                                   "polyana": {"command": "pa"}}})
            os.chmod(os.path.join(gem, ".claude.json"), 0o600)
            plan = envtidy.plan_mcp_home("seat:gemini", gem)
            self.assertEqual([n for n, _ in plan["refused"]], ["polyana"])
            self.assertEqual(envtidy.apply_mcp_home(plan, self.backup)[0], "ok")
            self.assertIn("polyana", self._state(gem)["mcpServers"])

    def test_gemini_takes_polyana_now_the_proxy_fills_array_items(self):
        # The refusal row is gone: a stale row told the operator to REMOVE a
        # server gemini had been answering on since the proxy fix.
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "polyana": {"command": "pa"}})
        gem = os.path.join(self.seats, "gemini", "claude")
        os.makedirs(gem)
        self._window(gem, 1000000)
        with self._env(HELM_MCPS_CANONICAL=canon):
            plan = envtidy.plan_mcp_home("seat:gemini", gem)
            self.assertEqual(sorted(plan["adds"]), ["exa", "polyana"])
            self.assertEqual(plan["refused"], [])

    def test_a_loose_state_file_holding_servers_is_planned_private(self):
        state = os.path.join(self.kimi, ".claude.json")
        _write(state, {"mcpServers": {"alpha-mcp": {"command": "alpha"}}})
        os.chmod(state, 0o664)
        canon = self._mcp_canon({"alpha-mcp": {"command": "alpha"}})
        with self._env(HELM_MCPS_CANONICAL=canon):
            plan = envtidy.plan_mcp_home("seat:kimi", self.kimi)
            self.assertEqual((plan["verdict"], plan["adds"]), ("change", []))
            self.assertEqual(plan["tighten"], "-rw-rw-r--")
            with open(state, "rb") as f:
                before = f.read()
            self.assertEqual(envtidy.apply_mcp_home(plan, self.backup)[0], "applied")
            self.assertEqual(os.stat(state).st_mode & 0o777, 0o600)
            with open(state, "rb") as f:
                self.assertEqual(f.read(), before)                  # bytes untouched
            self.assertEqual(envtidy.plan_mcp_home("seat:kimi", self.kimi)["verdict"],
                             "ok")
        # control: a loose file that holds NO servers is not ours to judge
        other = os.path.join(self.croot, "bare-com", ".claude.json")
        _write(other, {"numStartups": 1})
        os.chmod(other, 0o664)
        with self._env(HELM_MCPS_CANONICAL=self._mcp_canon({}, name="empty.json")):
            self.assertIsNone(envtidy.plan_mcp_home("bare-com", os.path.dirname(other))["tighten"])

    def test_the_census_never_asks_back_what_the_sync_withholds(self):
        """THE CENSUS AND THE SYNC ANSWER ONE QUESTION. A server the sync
        withholds from a seat (over its window, or on the operator's own GPU)
        is not a gap: listing it as missing asks the operator to add back
        what the sync said to remove by hand."""
        canon = self._mcp_canon({"exa": {"command": "exa"},
                                 "polyana": {"command": "pa"}})
        local = os.path.join(self.seats, "qwenlocal", "claude")
        os.makedirs(local)
        for d in (local, self.kimi):
            self._window(d, 229376)
            _write(os.path.join(d, ".claude.json"),
                   {"mcpServers": {"exa": {"command": "exa"}}})
            os.chmod(os.path.join(d, ".claude.json"), 0o600)
        dirs = skillsync.config_dirs(
            claude_root=self.croot, default_claude=self.default,
            seats_root=self.seats)
        with self._env(HELM_MCPS_CANONICAL=canon):
            m = envtidy.census(dirs=dirs)["mcp_variance"]
        # CONTROL: the vendor seat on the same window is missing polyana
        self.assertEqual(m["missing_by_home"].get("seat:kimi"), ["polyana"])
        self.assertNotIn("seat:qwenlocal", m["missing_by_home"])
        self.assertEqual(m["withheld_by_home"].get("seat:qwenlocal"),
                         ["polyana"])

    def test_a_codex_seat_on_the_claude_harness_is_planned(self):
        """THE RETIRED PREMISE: a codex-family seat was excluded by its family
        name. Its launch runs the claude harness, so it is Claude Code and is
        planned — the family instance with no launch script too (unsure)."""
        self.assertEqual(envtidy.seat_harness(self.codex), "claude")
        self.assertIsNone(envtidy.seat_harness(self.codex_inst))   # unsure
        canon = self._mcp_canon(self.canon)
        with self._env(HELM_MCPS_CANONICAL=canon):
            for label, cdir in (("seat:codex", self.codex),
                                ("seat:codex/seat-a", self.codex_inst)):
                plan = envtidy.plan_mcp_home(label, cdir)
                self.assertEqual(plan["verdict"], "change", label)
                self.assertEqual(sorted(plan["adds"]), ["alpha-mcp", "beta-mcp"])
            c = envtidy.census(dirs=self.dirs)
        self.assertIn("seat:codex", c["mcp_variance"]["missing_by_home"])
        self.assertNotIn("seat:codex", c["mcp_variance"]["excluded_by_home"])

    def test_a_non_claude_harness_seat_is_excluded_and_counted(self):  # noqa: VACUOUS_ASSERTION — the exact excluded list, the harness named in its reason and the unsure-seat changes are unconditional positives on the same sync result
        canon = self._mcp_canon(self.canon)
        with self._env(HELM_MCPS_CANONICAL=canon):
            r = envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup)
            self.assertEqual([p["label"] for p in r["excluded"]], ["seat:seat-b"])
            self.assertIn("pi harness", r["excluded"][0]["detail"])   # the reason
            changed = {p["label"] for p in r["changed"]}
            self.assertTrue({"seat:codex", "seat:codex/seat-a", "seat:kimi"} <= changed)
            self.assertNotIn("seat:seat-b", changed)
            # every dir lands in exactly ONE bucket — none vanishes, none twice
            self.assertEqual(len(r["changed"]) + len(r["failed"]) + r["steady"]
                             + len(r["excluded"]), len(self.dirs))
            c = envtidy.census(dirs=self.dirs)
            self.assertIn("seat:seat-b", c["mcp_variance"]["excluded_by_home"])
            self.assertNotIn("seat:seat-b", c["mcp_variance"]["missing_by_home"])
            # UNSURE IS PLANNED. Each unsure shape of the same seat plans it:
            # disagreeing stamps, a stamp-free script, an unreadable script.
            launch = os.path.join(os.path.dirname(self.other), "launch.sh")
            for shape in ("mixed", "no-stamp", "unreadable", "claude"):
                with self.subTest(shape=shape):
                    os.remove(launch) if os.path.isfile(launch) else os.rmdir(launch)
                    if shape == "mixed":
                        self._launch(self.other, "pi", "claude")
                    elif shape == "no-stamp":
                        self._launch(self.other)
                    elif shape == "unreadable":
                        os.makedirs(launch)          # a dir where the script belongs
                    else:
                        self._launch(self.other, "claude")
                    self.assertIsNone(envtidy.mcp_exclusion("seat:seat-b", self.other))
                    self.assertEqual(envtidy.plan_mcp_home(
                        "seat:seat-b", self.other)["verdict"], "change")
            # a HOME is never excluded, whatever sits beside it
            self._launch(self.whole, "pi")
            self.assertIsNone(envtidy.mcp_exclusion("whole-com", self.whole))

    def test_the_summary_never_reports_a_skipped_dir_as_canonical(self):  # noqa: VACUOUS_ASSERTION — the exact count line and the per-seat line are unconditional positives on the same output the absence check reads
        """THE MEASURED FALSE GREEN: `0 homes with gaps, 29 already canonical`
        while four seats held zero servers. The planted estate is exactly that
        — excluded and zero-server seats only — and the count must say so."""
        seats_only = [(l, c) for l, c in self.dirs if l.startswith("seat:")]
        canon = self._mcp_canon(self.canon)
        out, err = io.StringIO(), io.StringIO()
        with self._env(HELM_MCPS_CANONICAL=canon), \
                mock.patch.object(envtidy.skillsync, "config_dirs",
                                  return_value=seats_only), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(envtidy.cmd_mcp(["sync"]), 0)
        text = out.getvalue()
        self.assertIn("3 dir(s) with gaps, 0 already canonical, 1 excluded, "
                      "0 failed", text)
        self.assertIn("seat:seat-b", text)
        self.assertIn("EXCLUDED", text)
        self.assertIn("seat:kimi", text)
        self.assertIn("seat had 0/2 canonical server(s); missing alpha-mcp, "
                      "beta-mcp", text)
        self.assertNotIn(SECRET, text + err.getvalue())    # configs never printed
        # the instructions leg has the same law: no source is not canonical
        r = envtidy.instructions_sync(dirs=seats_only)
        self.assertEqual(r["steady"], 0)
        self.assertEqual(len(r["unsourced"]), len(seats_only))

    # ---- the canonical file's permission rule --------------------------------

    def test_a_world_readable_canonical_file_is_refused(self):  # noqa: VACUOUS_ASSERTION — the 0600 read after the loop is the unconditional positive control on the same resolver
        body = dict(self.canon)
        for mode in (0o644, 0o640, 0o604, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                priv = self._mcp_canon(body, mode=mode, name="priv.json")
                with self._env(HELM_MCPS_PRIVATE=priv):
                    with self.assertRaises(envtidy.MCPSourceRefused) as cm:
                        envtidy.canonical_mcps()
                msg = str(cm.exception)
                self.assertIn(priv, msg)                        # named
                self.assertIn("chmod 600", msg)                 # and the repair
                self.assertNotIn(SECRET, msg)                   # never the content
        # the explicit env file obeys the same rule
        loose = self._mcp_canon(body, mode=0o644, name="env.json")
        with self._env(HELM_MCPS_CANONICAL=loose):
            with self.assertRaises(envtidy.MCPSourceRefused):
                envtidy.canonical_mcps()
        # positive control: the SAME content at 0600 is read
        tight = self._mcp_canon(body, mode=0o600, name="priv.json")
        with self._env(HELM_MCPS_PRIVATE=tight):
            self.assertEqual(sorted(envtidy.canonical_mcps()), ["alpha-mcp", "beta-mcp"])

    def test_a_refused_file_writes_nothing_anywhere(self):  # noqa: VACUOUS_ASSERTION — rc 2, the named refusal on stderr and tidy's recorded error are unconditional positives before any absence is asserted
        priv = self._mcp_canon(self.canon, mode=0o644, name="priv.json")
        out, err = io.StringIO(), io.StringIO()
        with self._env(HELM_MCPS_PRIVATE=priv), \
                mock.patch.object(envtidy.skillsync, "config_dirs",
                                  return_value=self.dirs), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = envtidy.cmd_mcp(["sync", "--apply"])
            with self.assertRaises(envtidy.MCPSourceRefused):
                envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
            with mock.patch.object(envtidy, "worktree_gc",
                                   return_value={"error": "fixture"}):
                t = envtidy.tidy(dirs=self.dirs, backup_root=self.backup,
                                 root=self.tmp, apply=False)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())
        self.assertIn(priv, err.getvalue())
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        for _label, cdir in self.dirs:
            self.assertFalse(os.path.exists(os.path.join(cdir, ".claude.json")), cdir)
        self.assertFalse(os.path.exists(self.backup))
        # tidy reports the leg as refused instead of aborting or reading clean
        self.assertIn("REFUSED", t["mcp"]["error"])
        self.assertIn("REFUSED", t["census"]["mcp_variance"]["canonical_error"])
        self.assertIn("hooks", t)                        # the other legs still ran

    def test_the_private_file_sits_between_the_env_file_and_the_host_block(self):
        priv = self._mcp_canon({"from-private": None}, name="priv.json")
        env = self._mcp_canon({"from-env": None}, name="env.json")
        host = {"canonical_mcps": {"from-host": None}}
        with mock.patch.object(envtidy.registry, "authored_host", return_value=host):
            with self._env(HELM_MCPS_PRIVATE=priv, HELM_MCPS_CANONICAL=env):
                self.assertEqual(list(envtidy.canonical_mcps()), ["from-env"])
            with self._env(HELM_MCPS_PRIVATE=priv, HELM_MCPS_CANONICAL=""):
                self.assertEqual(list(envtidy.canonical_mcps()), ["from-private"])
            gone = os.path.join(self.tmp, "absent.json")
            with self._env(HELM_MCPS_PRIVATE=gone, HELM_MCPS_CANONICAL=""):
                self.assertEqual(list(envtidy.canonical_mcps()), ["from-host"])
            with self._env(HELM_MCPS_PRIVATE="off", HELM_MCPS_CANONICAL=""):
                self.assertEqual(list(envtidy.canonical_mcps()), ["from-host"])
                self.assertIsNone(envtidy.private_mcps_path())
        with self._env(HELM_MCPS_PRIVATE=""):
            self.assertEqual(envtidy.private_mcps_path(), envtidy.MCPS_PRIVATE)
        self.assertTrue(envtidy.MCPS_PRIVATE.endswith(
            os.path.join(".config", "helm", "mcps-canonical.json")))

    # ---- hooks: the full set, not the lean subset -----------------------------

    def test_seats_are_reconciled_to_the_full_hook_set(self):
        pre = envtidy.plan_hooks_home("seat:codex", self.codex)
        self.assertEqual(pre["verdict"], "change")      # the old lean subset is short
        self.assertEqual(pre["actions"].get("inject --hook-json"), "add")
        r = envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertEqual([p["label"] for p in r["failed"]], ["broken-com"])
        from helm import record
        for cdir in (self.codex, self.kimi, self.codex_inst):
            with open(os.path.join(cdir, "settings.json")) as f:
                got = json.load(f)
            for tup in envtidy.CANONICAL_HOOKS:
                self.assertTrue(hooks_mod._lane_live(got, envtidy._spec(tup)),
                                (cdir, tup))
            for ev in record.HOOK_EVENTS:            # both recorder legs, by record's own check
                self.assertTrue(record._leg_live(got, ev), (cdir, ev))
        r2 = envtidy.hooks_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
        self.assertEqual([p["label"] for p in r2["changed"]], [])

    # ---- the global instructions ------------------------------------------------

    def _global_rules(self):
        src = os.path.join(self.tmp, "owner-home", "CLAUDE.md")
        os.makedirs(os.path.dirname(src))
        with open(src, "w") as f:
            f.write("# the host's global rules\n")
        return src

    def test_instructions_link_every_seat_and_keep_a_real_file(self):
        src = self._global_rules()
        own = os.path.join(self.kimi, "CLAUDE.md")
        with open(own, "wb") as f:
            f.write(b"# this seat's own rules\r\nkeep\r\n")
        st = os.stat(own)
        # THE VIOLATION: a real file already holds the link's name on one seat
        squat = os.path.join(self.codex_inst, "rules", "global-instructions.md")
        os.makedirs(os.path.dirname(squat))
        with open(squat, "w") as f:
            f.write("operator's own\n")
        with self._env(HELM_INSTRUCTIONS_CANONICAL=src):
            dry = envtidy.instructions_sync(dirs=self.dirs)
            self.assertFalse(os.path.lexists(os.path.join(
                self.kimi, "rules", "global-instructions.md")))   # dry wrote nothing
            self.assertIn("would-link", {p["action"] for p in dry["changed"]})
            r = envtidy.instructions_sync(dirs=self.dirs, apply=True)
        linked = {p["label"] for p in r["changed"]}
        self.assertEqual(linked, {"seat:codex", "seat:kimi", "seat:seat-b"})
        for cdir in (self.codex, self.kimi, self.other):
            link = os.path.join(cdir, "rules", "global-instructions.md")
            self.assertEqual(os.readlink(link), os.path.realpath(src))
        # the real file is KEPT, byte for byte, and surfaced — never canonical
        with open(squat) as f:
            self.assertEqual(f.read(), "operator's own\n")
        self.assertEqual([(p["label"], p["action"]) for p in r["surfaced"]],
                         [("seat:codex/seat-a", "real")])
        # the seat's OWN CLAUDE.md is never touched: same bytes, same inode
        with open(own, "rb") as f:
            self.assertEqual(f.read(), b"# this seat's own rules\r\nkeep\r\n")
        self.assertEqual((os.stat(own).st_ino, os.stat(own).st_mtime_ns),
                         (st.st_ino, st.st_mtime_ns))
        # homes are counted as not planned, never as canonical
        self.assertEqual(len(r["not_planned"]),
                         len([l for l, _c in self.dirs if not l.startswith("seat:")]))
        self.assertEqual(r["steady"], 0)
        with self._env(HELM_INSTRUCTIONS_CANONICAL=src):
            again = envtidy.instructions_sync(dirs=self.dirs, apply=True)
        self.assertEqual((again["steady"], again["changed"]), (3, []))

    def test_tidy_reports_each_seats_servers_hooks_and_instructions(self):
        src = self._global_rules()
        canon = self._mcp_canon(self.canon)
        out = io.StringIO()
        with self._env(HELM_MCPS_CANONICAL=canon, HELM_INSTRUCTIONS_CANONICAL=src):
            r = envtidy.census(dirs=self.dirs)
            envtidy._print_census(r, out=out)
        text = out.getvalue()
        self.assertIn("seat parity", text)
        lines = {ln.split()[0]: ln for ln in text.splitlines()
                 if ln.startswith("  seat:") and "instructions" in ln}
        self.assertEqual(sorted(lines), ["seat:codex", "seat:codex/seat-a",
                                         "seat:kimi", "seat:seat-b"])
        self.assertIn("servers EXCLUDED", lines["seat:seat-b"])
        self.assertIn("servers 0/2 missing alpha-mcp,beta-mcp", lines["seat:codex"])
        self.assertIn("servers 0/2 missing alpha-mcp,beta-mcp", lines["seat:kimi"])
        self.assertIn("hooks 5 missing", lines["seat:codex"])
        self.assertIn("instructions would-link", lines["seat:kimi"])
        self.assertIn("own CLAUDE.md: none", lines["seat:kimi"])
        self.assertNotIn(SECRET, text)

    def test_the_report_flags_a_readable_state_file_holding_servers(self):  # noqa: VACUOUS_ASSERTION — the flagged-label map and the LOOSE lines are unconditional positives on the same census the absence checks read
        """A `.claude.json` holding MCP servers that group or world can read
        is a credential they can read: the census flags it on EVERY dir, an
        excluded seat included (with the manual repair, since the sync does
        not plan it). A private one, and a loose one with no servers, are
        not flagged."""
        servers = {"mcpServers": {"beta-mcp": self.canon["beta-mcp"]}}
        for cdir, mode in ((self.kimi, 0o664), (self.other, 0o644),
                           (self.whole, 0o660), (self.codex, 0o600)):
            _write(os.path.join(cdir, ".claude.json"), servers)
            os.chmod(os.path.join(cdir, ".claude.json"), mode)
        bare = os.path.join(self.croot, "bare-com", ".claude.json")
        _write(bare, {"numStartups": 1})
        os.chmod(bare, 0o666)
        out = io.StringIO()
        with self._env(HELM_MCPS_CANONICAL=self._mcp_canon(self.canon)):
            r = envtidy.census(dirs=self.dirs)
            envtidy._print_census(r, out=out)
        self.assertEqual(r["mcp_variance"]["loose_state_by_home"],
                         {"seat:kimi": "-rw-rw-r--", "seat:seat-b": "-rw-r--r--",
                          "whole-com": "-rw-rw----"})
        text = out.getvalue()
        self.assertIn("CREDENTIAL EXPOSURE", text)
        self.assertIn("LOOSE seat:kimi", text)
        self.assertIn("LOOSE whole-com", text)
        flagged = [ln for ln in text.splitlines() if "LOOSE seat:seat-b" in ln]
        self.assertEqual(len(flagged), 1)
        self.assertIn("excluded from mcp sync: chmod 600", flagged[0])
        self.assertIn("LOOSE -rw-rw-r--", [ln for ln in text.splitlines()
                                            if ln.startswith("  seat:kimi")
                                            and "servers" in ln][0])
        self.assertNotIn("LOOSE seat:codex", text)          # private: not flagged
        self.assertNotIn("bare-com", "".join(
            ln for ln in text.splitlines() if "LOOSE" in ln))  # no servers: not flagged
        self.assertNotIn(SECRET, text)
        # and the sync repairs exactly the planned ones, bytes untouched
        with self._env(HELM_MCPS_CANONICAL=self._mcp_canon(self.canon)):
            envtidy.mcp_sync(dirs=self.dirs, backup_root=self.backup, apply=True)
            after = envtidy.census(dirs=self.dirs)
        self.assertEqual(after["mcp_variance"]["loose_state_by_home"],
                         {"seat:seat-b": "-rw-r--r--"})       # excluded: by hand


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
                          for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                                    "HELM_CACHE_DIR")}
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        # the sweep's keep-verdict memo lives in the cache root (task/4061)
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
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
        # registered worktree, clean, at main tip, and ABANDONED: nobody has
        # moved it for longer than the grace a sweep gives a room before its
        # first commit, so it is removable (a fresh one is kept, task/3428)
        self.wt_rm = os.path.join(self.tmp, "wts", "wf-clean")
        _sh(self.root, "git", "worktree", "add", "-q", "-b", "worktree-wfclean",
            self.wt_rm, "main")
        _roomclock.age_room(self.wt_rm)
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
        _roomclock.age_room(path)        # abandoned, so work.gc removes it
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

    def test_enact_rereads_what_the_room_holds_after_scan(self):
        """task/3125. The scan judged the room by the branch it held then. A
        room that detaches and commits before enact holds a commit only its
        HEAD reaches, and removing it orphans that commit while the branch
        the scan judged still reads merged. `gc_enact` re-reads the room's
        branch before removal; this remover did not."""
        self._seed()
        row = next(w for w in envtidy._worktree_rows(self.root, "main")
                   if w["path"] == self.wt_rm)
        self.assertEqual(row["verdict"], "remove")
        _sh(self.wt_rm, "git", "checkout", "-q", "--detach")
        self._commit("after-scan.txt", "committed after the scan",
                     where=self.wt_rm)
        doomed = _sh(self.wt_rm, "git", "rev-parse", "HEAD").stdout.strip()
        lines = envtidy._enact_worktree(self.root, row, True)
        self.assertTrue(os.path.isdir(self.wt_rm),
                        "removed a room that detached under the scan: %s"
                        % lines)
        self.assertEqual(
            _sh(self.wt_rm, "git", "rev-parse", "HEAD").stdout.strip(), doomed)
        self.assertTrue(any("moved under the scan" in line for line in lines),
                        lines)

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


class _StrayRooms:
    """A stray room minted off main, its row in the estate sweep, and the
    branch set, for the arms that walk a stray room's life."""

    def _add(self, name):
        path = os.path.join(self.tmp, "wts", name)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "worktree-" + name, path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def _row(self, path):
        return next(r for r in envtidy._worktree_rows(self.root, "main")
                    if r["path"] == path)

    def _branches(self):
        r = _sh(self.root, "git", "for-each-ref", "--format=%(refname:short)",
                "refs/heads/")
        self.assertEqual(r.returncode, 0, r.stderr)
        return set(r.stdout.split())


class WorktreeGcUnstartedTest(_StrayRooms, WorktreeBase):
    """THE ESTATE SWEEP ASKS THE SAME QUESTION AS `helm work gc` (task/3428).

    A stray room minted at the trunk a moment ago is clean and its branch is
    an ancestor of the trunk, which this sweep read as "clean + merged" and
    removed, branch and all. Nothing of that branch was ever on the trunk; it
    is EMPTY, not landed, and its builder is about to commit in it."""

    def test_a_fresh_stray_room_at_the_trunk_is_KEPT(self):
        path = self._add("wf-fresh")
        row = self._row(path)
        self.assertEqual(row["verdict"], "keep", row)
        self.assertFalse(row["remove"])
        self.assertIn("UNSTARTED", row["why"])
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("worktree-wf-fresh", self._branches())

    def test_control_a_landed_stray_room_is_still_removed(self):  # noqa: VACUOUS_ASSERTION — the absence is the removal under test; the row reads remove and the branch is asserted present before apply
        path = self._add("wf-landed")
        self._commit("landed.txt", "landed work", where=path)
        r = _sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "land",
                "worktree-wf-landed")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self._row(path)
        self.assertEqual(row["verdict"], "remove", row)
        self.assertIn("worktree-wf-landed", self._branches())
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("worktree-wf-landed", self._branches())

    def test_an_OLD_unstarted_stray_room_is_still_tidied(self):  # noqa: VACUOUS_ASSERTION — the absence is the tidy under test; the row reads remove naming the abandoned-claim rule first
        self.assertGreater(_roomclock.AGED_S, _gc._UNSTARTED_GRACE_S)
        path = self._add("wf-old")
        _roomclock.age_room(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "remove", row)
        self.assertIn("an abandoned claim", row["why"])
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("worktree-wf-old", self._branches())

    def test_enact_RE_READS_the_sweep_state_before_removal(self):
        """The scan judged the room abandoned; its HEAD moved before the
        enact, on the same branch. The estate sweep had no landedness re-read
        at the removal at all, so the scan's verdict alone removed rooms."""
        path = self._add("wf-revived")
        _roomclock.age_room(path)
        row = self._row(path)
        self.assertEqual(row["verdict"], "remove", row)
        r = _sh(path, "git", "reset", "-q", "--hard", "HEAD")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = envtidy._enact_worktree(self.root, row, True)
        self.assertTrue(os.path.isdir(path), lines)
        self.assertIn("worktree-wf-revived", self._branches())
        self.assertTrue(any(line.startswith("SKIPPED") and "UNSTARTED" in line
                            for line in lines), lines)

    def test_an_ORPHAN_branch_reset_back_after_committing_is_KEPT(self):  # noqa: VACUOUS_ASSERTION — the never-started branch's absence is the control; the reset branch is asserted PRESENT in the same branch set and its dropped sha read back from its reflog
        """No room stands on this branch, so no age protects it, and a branch
        that never committed is still deleted (the control below: it holds no
        commit). But this one committed and was reset back to the trunk: its
        tip reads LANDED by ancestry while the commit it wrote is on neither
        the branch nor the trunk, only in the branch's reflog — which
        deleting the branch deletes."""
        self._branch_at_main("lane/never-started")
        r = _sh(self.root, "git", "checkout", "-q", "-b", "lane/undone")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._commit("undone.txt", "written, then reset away")
        dropped = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        for cmd in (("git", "reset", "-q", "--hard", "main"),
                    ("git", "checkout", "-q", "main")):
            r = _sh(self.root, *cmd)
            self.assertEqual(r.returncode, 0, r.stderr)
        verdicts = {o["branch"]: o["verdict"]
                    for o in envtidy._orphan_branches(self.root, "main")}
        self.assertEqual(verdicts["lane/never-started"], "delete")
        self.assertEqual(verdicts["lane/undone"], "keep")
        envtidy.worktree_gc(root=self.root, apply=True)
        branches = self._branches()
        self.assertNotIn("lane/never-started", branches)
        self.assertIn("lane/undone", branches)
        log = _sh(self.root, "git", "reflog", "show", "--format=%H",
                  "lane/undone")
        self.assertEqual(log.returncode, 0, log.stderr)
        self.assertIn(dropped, log.stdout.split())


class WorktreeGcDroppedWorkTest(_StrayRooms, WorktreeBase):
    """THE ESTATE SWEEP KEEPS EVERY COMMIT ONLY A REFLOG HOLDS UNTIL IT LANDS
    (task/3436): the orphan pass (no room, so the branch's reflog is the only
    record) and the stray-room pass (the room's HEAD reflog too), read by the
    same `work._sweep_state` as `helm work gc`, and the enact re-proves the
    room unmoved at the removal itself."""

    def _git(self, cwd, *args):
        r = _sh(cwd, *(("git",) + args))
        self.assertEqual(r.returncode, 0, "git %s: %s" % (args, r.stderr))
        return r.stdout.strip()

    def _head(self, cwd):
        return self._git(cwd, "rev-parse", "HEAD")

    def _reflog(self, cwd, ref):
        return self._git(cwd, "reflog", "show", "--format=%H", ref).split()

    def _orphan(self, branch, *steps):
        """A branch off main in the main checkout, each step a (file, reset)
        pair: commit `file`, then reset back to main if `reset`. Returns the
        shas committed, and leaves the checkout on main."""
        self._git(self.root, "checkout", "-q", "-b", branch)
        shas = []
        for name, reset in steps:
            self._commit(name, "work on " + name)
            shas.append(self._head(self.root))
            if reset:
                self._git(self.root, "reset", "-q", "--hard", "main")
        self._git(self.root, "checkout", "-q", "main")
        return shas

    def _detour(self, path, branch, name):
        self._git(path, "checkout", "-q", "--detach")
        with open(os.path.join(path, name), "w") as f:
            f.write("detour\n")
        self._git(path, "add", name)
        self._git(path, "commit", "-q", "-m", "detour " + name)
        sha = self._head(path)
        self._git(path, "checkout", "-q", branch)
        return sha

    def _landed_room(self, name):
        path = self._add(name)
        self._commit("landed.txt", "landed work", where=path)
        self._git(self.root, "merge", "-q", "--no-ff", "-m", "land",
                  "worktree-" + name)
        return path

    def test_F1_an_ORPHAN_whose_carried_commit_landed_keeps_a_dropped_one(self):
        dropped, _kept = self._orphan("lane/o1", ("draft.txt", True),
                                      ("final.txt", False))
        self._git(self.root, "merge", "-q", "--no-ff", "-m", "land", "lane/o1")
        verdict = {o["branch"]: o for o in
                   envtidy._orphan_branches(self.root, "main")}["lane/o1"]
        self.assertEqual(verdict["verdict"], "keep", verdict)
        self.assertIn(dropped[:12], verdict["why"])
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertIn("lane/o1", self._branches())
        self.assertIn(dropped, self._reflog(self.root, "refs/heads/lane/o1"))

    def test_F3_an_ORPHAN_whose_creation_line_expired_keeps_a_dropped_one(self):
        """No room, so no grace; the reflog has lost its creation line and
        still names the commit the branch reset away."""
        (dropped,) = self._orphan("lane/o3", ("dropped.txt", True))
        said = self._git(self.root, "reflog", "show", "--format=%gs",
                         "refs/heads/lane/o3").splitlines()
        at = [i for i, s in enumerate(said) if s.startswith("branch: Created")]
        self.assertEqual(len(at), 1, said)
        self._git(self.root, "reflog", "delete",
                  "refs/heads/lane/o3@{%d}" % at[0])
        verdict = {o["branch"]: o for o in
                   envtidy._orphan_branches(self.root, "main")}["lane/o3"]
        self.assertEqual(verdict["verdict"], "keep", verdict)
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertIn("lane/o3", self._branches())
        self.assertIn(dropped, self._reflog(self.root, "refs/heads/lane/o3"))

    def test_F5_a_REUSED_stray_room_on_a_NEW_branch_is_KEPT_as_UNSTARTED(self):
        """The room's old branch committed and landed; the room then checked
        out a new branch at the trunk. Its HEAD reflog still records the old
        commit, which is not the new branch's work (task/3436 round 2)."""
        path = self._landed_room("wf-reuse")
        old = self._head(path)
        self._git(path, "checkout", "-q", "-b", "worktree-wf-reuse-next",
                  "main")
        self.assertIn(old, self._reflog(path, "HEAD"))
        row = self._row(path)
        self.assertEqual(row["branch"], "worktree-wf-reuse-next", row)
        self.assertEqual(row["verdict"], "keep", row)
        self.assertIn("UNSTARTED", row["why"])
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("worktree-wf-reuse-next", self._branches())

    def test_F2_a_stray_room_with_a_CONFLICTING_rebase_is_KEPT(self):  # noqa: VACUOUS_ASSERTION — the room's removal is the base under test; the positives are B' rewritten, B' in the room's HEAD reflog, and the room + branch standing after apply
        """The room committed B on its branch; a ref held B, so the ORIGINAL
        is not the commit at risk. The trunk then wrote B's own file, and the
        room rebased onto it: the conflict resolved into a NEW commit B'
        recorded only as `rebase (continue)`, and the room reset back to the
        trunk. B' is off the trunk and lives only in the two reflogs that
        removing the room deletes (task/3436 round 3, ruling 2). The base
        counted only `_OWN_COMMIT` actions in the reflogs, and
        `rebase (continue)` is not one, so B' never entered the reflog-only
        judgment; the room, aged past the unstarted grace, read ANCESTOR as an
        abandoned claim and was removed, branch and B' with it. The cures keep
        every reflog line's new sha, whatever its action, so B' is judged,
        found off the trunk, and the room is kept."""
        self.addCleanup(os.environ.pop, "GIT_EDITOR")
        os.environ["GIT_EDITOR"] = "true"
        path = self._add("wf-rebase")
        branch = "worktree-wf-rebase"
        self._commit("f.txt", "B change", where=path)
        B = self._head(path)
        self._git(path, "update-ref", "refs/remotes/origin/" + branch, "HEAD")
        self.assertEqual(self._git(self.root, "rev-parse",
                                   "refs/remotes/origin/" + branch), B,
                         "premise: a ref holds B, so it is not at risk")
        self._commit("f.txt", "trunk change")
        r = _sh(path, "git", "rebase", "main")
        self.assertNotEqual(r.returncode, 0,
                            "fixture: the rebase must conflict (B and the "
                            "trunk both write f.txt)")
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("B change (resolved)\n")
        self._git(path, "add", "f.txt")
        self._git(path, "rebase", "--continue")
        Bp = self._head(path)
        self.assertNotEqual(Bp, B, "the rebase rewrote the commit")
        self._git(path, "reset", "-q", "--hard", "main")
        self.assertEqual(self._head(path), self._head(self.root),
                         "premise: the room is back at the trunk")
        self.assertIn(Bp, self._reflog(path, "HEAD"),
                      "premise: B' survives only in the reflog")
        self.assertGreater(_roomclock.AGED_S, _gc._UNSTARTED_GRACE_S)
        _roomclock.age_room(path)        # abandoned: only the rule can keep it
        row = self._row(path)
        self.assertEqual(row["branch"], branch, row)
        self.assertEqual(row["verdict"], "keep", row)
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertTrue(os.path.isdir(path))
        self.assertIn(branch, self._branches())
        self.assertIn(Bp, self._reflog(path, "HEAD"))

    def test_F2_a_stray_room_with_a_DETACHED_commit_is_KEPT(self):
        path = self._landed_room("wf-detour")
        detached = self._detour(path, "worktree-wf-detour", "d.txt")
        row = self._row(path)
        self.assertEqual(row["verdict"], "keep", row)
        self.assertIn(detached[:12], row["why"])
        envtidy.worktree_gc(root=self.root, apply=True)
        self.assertTrue(os.path.isdir(path))
        self.assertIn(detached, self._reflog(path, "HEAD"))

    def test_F4_a_detour_AFTER_the_enact_verdict_is_SKIPPED(self):
        path = self._landed_room("wf-race")
        row = self._row(path)
        self.assertEqual(row["verdict"], "remove", row)
        real, late = work._sweep_state, []

        def verdict_then_detour(*args, **kw):
            got = real(*args, **kw)
            if not late:
                late.append(self._detour(path, "worktree-wf-race", "l.txt"))
            return got
        with mock.patch.object(work, "_sweep_state", verdict_then_detour):
            lines = envtidy._enact_worktree(self.root, row, True)
        self.assertEqual(len(late), 1, "the enact never asked the verdict")
        self.assertTrue(os.path.isdir(path), lines)
        self.assertIn(late[0], self._reflog(path, "HEAD"))
        self.assertTrue(any(line.startswith("SKIPPED") and "moved" in line
                            for line in lines), lines)

    def test_F2_the_RESCUE_path_re_proves_the_room_before_removing_it(self):
        """A landed, dirty stray room is rescue+remove, and the enact did not
        re-ask anything about the room after its rescue commit. A detour made
        after the scan leaves a commit only the room's HEAD reflog holds."""
        path = self._landed_room("wf-rescue")
        junk = os.path.join(path, "junk.txt")
        with open(junk, "w") as f:
            f.write("uncommitted\n")
        # ABANDONED, so the rescue itself does not defer (`_RESCUE_ACTIVE_S`)
        # and the arm reaches the removal it is about.
        cutoff = time.time() - (_gc._RESCUE_ACTIVE_S + 60)
        os.utime(junk, (cutoff, cutoff))
        row = self._row(path)
        self.assertEqual(row["verdict"], "rescue+remove", row)
        detached = self._detour(path, "worktree-wf-rescue", "d.txt")
        with mock.patch.object(work, "_wip_commit",
                               wraps=work._wip_commit) as rescue:
            lines = envtidy._enact_worktree(self.root, row, True)
        self.assertTrue(os.path.isdir(path), lines)
        self.assertIn(detached, self._reflog(path, "HEAD"))
        self.assertTrue(any(line.startswith("SKIPPED") and detached[:12] in line
                            for line in lines), lines)
        rescue.assert_called_once()


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
