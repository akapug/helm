#!/usr/bin/env python3
"""THE USES-CLOSURE: every path that can START or ATTACH a live agent session
passes a fatal re-mint door, or is classified with a measured reason.

WHY A CLOSURE AND NOT AN ASSERTION. The middle shape — fatal on launch, loud
and nonfatal on mint — is only safe if the fatal doors are the ONLY way a
session begins. Neither the author nor the reviewer can settle that by
inspection: the answer is a property of the whole tree and it changes whenever
someone adds an exec. So this enumerates the tree's exec/attach sites and fails
on any it does not recognize, which makes a NEW session-starting path a test
failure rather than a silent hole in the ruling.

CLOSED OVER WHAT CAN EXEC OR ATTACH, not over what imports a module: importing
seat_launch_assets proves nothing, and a module that never imports it can still
`subprocess` a launch.sh. The scan is therefore for exec/attach PRIMITIVES.

WHAT THE CLOSURE FOUND, and it changed the design: `launch.sh` is itself an
executable artifact, so a human running it — and helm/evalrun.py, which shells
it with -p — start a live session without passing seat launch/spawn/resume.
Rather than argue that hand-running a script is out of scope, the generated
asset now carries the rule (`_guard_preflight`), which is also what makes the
nonfatal mint's promise ("launch will refuse until it is resolved") literally
true instead of advisory.
"""
import ast
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402,F401
# `seat` IS IMPORTED EXPLICITLY because the co-occurrence guard in
# tests/test_seat_facade_injection.py requires an accepted facade import
# beside any impl import. It asserts nothing about import order.
from helm import seat, seat_launch_assets, hooks  # noqa: E402,F401

HELM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "helm")

# The primitives by which a process can become, or attach to, a live session.
_EXEC_NAMES = {"execv", "execve", "execvp", "execvpe", "execl", "execle",
               "execlp", "spawnl", "spawnv", "posix_spawn", "exec_attached"}

# EVERY site is classified with the reason it is safe, and the reason is a
# MEASURED property of that call site, not a category anyone found convenient.
#   fatal-door        : re-mints through _write_launch_assets (or launch.py's
#                       install check) and refuses a shortened contract
#   owner-primitive   : the exec owner itself; it runs whatever a door handed
#                       it, so it is the mechanism the doors use, not a door
#   not-a-seat-session: starts something that is not a claude seat session
#                       against a helm-minted seat config dir
_CLASSIFIED = {
    ("launch.py", "exec_attached"): "fatal-door",
    ("seat_launch_owner.py", "execvp"): "owner-primitive",
    ("seat_launch_owner.py", "execvpe"): "owner-primitive",
    ("seat_launch_owner.py", "exec_attached"): "owner-primitive",
    # `helm pi start` execs the pi harness binary, not claude, and writes no
    # seat claude config dir — the suite guard is a Claude Code hook.
    ("pi.py", "exec_attached"): "not-a-seat-session",
    # WAS "not-a-seat-session", AND THAT WAS FALSE. This path PRESERVES
    # CLAUDE_CONFIG_DIR, so a cv-recorded resume can start under a seat config
    # with no fatal door between it and exec. It is now routed through the same
    # runtime helper the generated asset calls, and
    # `test_the_cv_resume_is_really_guarded` DERIVES that rather than trusting
    # this label — a classification nobody re-measures is decoration.
    ("session.py", "exec_attached"): "runtime-guarded",
}


def _exec_sites():
    """[(module, primitive, lineno)] across helm/ — AST, not grep, so a name in
    a docstring or a comment cannot pad the closure."""
    found = []
    for root, _dirs, files in os.walk(HELM):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except (OSError, SyntaxError):
                continue
            rel = os.path.relpath(path, HELM)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fnode = node.func
                name = (fnode.attr if isinstance(fnode, ast.Attribute)
                        else getattr(fnode, "id", None))
                if name in _EXEC_NAMES:
                    found.append((rel, name, node.lineno))
    return found


class SessionStartClosureTest(unittest.TestCase):
    def test_every_exec_or_attach_site_is_classified(self):
        sites = _exec_sites()
        # MUST-HIT: the scanner actually finds the sites we know exist. A
        # closure over an empty set is vacuously "complete" and proves nothing.
        self.assertTrue(any(m == "launch.py" for m, _n, _l in sites), sites)
        self.assertTrue(any(m == "seat_launch_owner.py" for m, _n, _l in sites),
                        sites)
        self.assertGreaterEqual(len(sites), 5, sites)
        unknown = sorted({(m, n) for m, n, _l in sites
                          if (m, n) not in _CLASSIFIED})
        self.assertEqual(
            unknown, [],
            "UNCLASSIFIED session-start path(s): %s\n"
            "A new way to exec or attach is a new door. If it can start a "
            "claude session against a helm-minted seat config, it belongs in "
            "the FATAL set (refuse a shortened contract before it runs); if it "
            "cannot, classify it here with the measured reason." % (unknown,))

    def test_the_fatal_doors_refuse_before_anything_runs(self):
        """The three seat doors default to fatal; only the mint opts out."""
        import inspect
        sig = inspect.signature(seat_launch_assets._write_launch_assets)
        self.assertIs(sig.parameters["fatal_shortened"].default, True,
                      "the shared door must be FATAL by default, so a new "
                      "caller is safe unless it deliberately opts out")
        src = inspect.getsource(seat_launch_assets)
        self.assertIn("fatal_shortened", src)

    def test_only_the_mint_opts_out_of_fatal(self):
        """seat_provision (add) opts out; seat.py (launch/spawn/resume) does
        not. Measured over the source, so moving a call site between the two
        files cannot silently change which doors are fatal."""
        import inspect
        from helm import seat, seat_provision
        prov = inspect.getsource(seat_provision)
        self.assertEqual(prov.count("fatal_shortened=False"), 3, prov.count)
        self.assertNotIn("fatal_shortened=False", inspect.getsource(seat))

    def test_the_generated_asset_carries_the_rule(self):
        """launch.sh is a DIRECT GENERATED-ASSET CONSUMER's input — a human
        running it, or evalrun shelling it with -p, starts a session without
        passing any seat door. The artifact therefore refuses on its own."""
        pre = seat_launch_assets._guard_preflight("/seat/claude")
        self.assertIn("exit 1", pre)
        self.assertIn("REFUSED", pre)
        # NOT generated per-spec any more, and that is the point: the asset
        # asks the runtime about the CURRENT spec list, so an external spec
        # added after this file was written is covered without regeneration.
        self.assertNotIn("suite-guard", pre)

    def test_direct_generated_asset_consumers_are_covered_by_construction(self):
        """THE SECOND CLASS, and the one an exec-primitive scan cannot see:
        a module that `subprocess`es the seat's own launch.sh. helm/evalrun.py
        does exactly that (`arm_command` drives the seat's launch.sh with -p).

        These are covered BY CONSTRUCTION rather than by classification — the
        refusal lives inside the artifact they run — so what this pins is that
        the class is still enumerated and still ends up at that artifact."""
        consumers = []
        for root, _d, files in os.walk(HELM):
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    src = f.read()
                # runs the asset: names it AND hands it to a subprocess
                if "launch.sh" in src and ("subprocess" in src or "Popen" in src):
                    consumers.append(os.path.relpath(
                        os.path.join(root, fn), HELM))
        # MUST-HIT: the known direct consumer is found, so an empty result
        # below would be a broken scan rather than an absence of consumers.
        self.assertIn("evalrun.py", consumers, consumers)
        # and the artifact every one of them runs refuses on its own
        self.assertIn("REFUSED", seat_launch_assets._launch_owner(
            "claude", cdir="/seat/claude"))

    def test_the_cv_resume_is_really_guarded(self):
        """DERIVE the `runtime-guarded` label, never trust it.

        Two properties, both measured: the seat-config detector recognises a
        real seat config dir (and rejects one outside the seat root), and
        _cv_launch actually REFUSES when the canonical preflight refuses. The
        second is what makes the label true; the first is what makes it apply.
        """
        from unittest import mock
        from helm import home as hmod, session
        root = os.path.join(hmod.global_dir(), "seats")
        seat_cfg = os.path.join(root, "codex", "claude")
        # applies to a seat config dir...
        self.assertEqual(session.seat_config_dir(
            {"CLAUDE_CONFIG_DIR": seat_cfg}), seat_cfg)
        # ...and NOT to an unrelated one (must-hit: the detector discriminates)
        self.assertIsNone(session.seat_config_dir(
            {"CLAUDE_CONFIG_DIR": os.path.join(os.sep, "tmp", "elsewhere")}))
        self.assertIsNone(session.seat_config_dir({}))

        spec = (["claude", "--resume"], {"CLAUDE_CONFIG_DIR": seat_cfg},
                None, None)
        with mock.patch.object(session, "_cv_resume_spec", return_value=spec), \
                mock.patch.object(session.seat_launch_owner,
                                  "exec_attached") as ex:
            with mock.patch("helm.hooks.preflight",
                            return_value=(1, "REFUSED — synthetic")):
                rc, msg = session._cv_launch("sid")
            self.assertNotEqual(rc, 0)
            self.assertIn("REFUSED", msg)
            ex.assert_not_called()          # THE EFFECT: no session started
            # MUST-HIT: it is the PREFLIGHT deciding, not a path that always
            # refuses — a passing preflight lets the very same resume through.
            with mock.patch("helm.hooks.preflight", return_value=(0, "")):
                session._cv_launch("sid")
            ex.assert_called_once()

    def test_the_generated_asset_calls_the_one_implementation(self):
        """BLOCKER 1's shape cure: the asset must CALL the canonical resolver,
        not reimplement it. A shell that re-derives the rule is a second
        implementation, and it diverged four measured ways."""
        pre = seat_launch_assets._guard_preflight("/seat/claude")
        self.assertIn("hooks preflight", pre)
        self.assertIn("--config-dir", pre)
        # and it must NOT re-derive resolution in shell
        for reimpl in ("command -v", "[ -x", "-x \"$"):
            self.assertNotIn(reimpl, pre,
                             "the asset re-implements resolution in shell")

    def test_the_preflight_precedes_the_exec_line(self):
        """Ordering is the property: a check after `exec` never runs."""
        line = seat_launch_assets._launch_owner("claude", cdir="/seat/claude")
        self.assertIn("REFUSED", line)
        self.assertLess(line.index("REFUSED"), line.index("exec "),
                        "the guard preflight must come BEFORE exec")


class PreflightExecutesTest(unittest.TestCase):
    """THE ARMS THAT WOULD HAVE CAUGHT BLOCKER 1.

    The previous closure only inspected the generated STRING and its ordering.
    A string arm cannot see a resolver disagree — every one of the four
    divergences below produced text that looked correct and behaved wrong. So
    these RUN the canonical helper under hostile environments, and one runs the
    generated script itself.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-preflight-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A REAL SEAT CONFIG DIR under a tmp HELM_HOME. configs' gated write
        # recognises a seat home by its LOCATION, so this is accepted both in
        # this process and in the `helm hooks preflight` SUBPROCESS the
        # generated script runs — which a HOME_ROOTS patch could never reach,
        # and it is also the only shape launch.sh ever points at.
        self._env_prior = {k: os.environ.get(k)
                           for k in ("HELM_HOME", "HELM_SUITE_GUARD")}
        self.addCleanup(self._restore)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.cdir = os.path.join(os.environ["HELM_HOME"], "_global", "seats",
                                 "codex", "claude")
        os.makedirs(self.cdir)
        self.good = os.path.join(self.tmp, "fab-suite-pretooluse")
        with open(self.good, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.good, 0o755)

    def _restore(self):
        for k, v in self._env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _pre(self):
        return hooks.preflight(self.cdir)

    def test_a_resolvable_guard_passes_and_writes_the_hook(self):
        """MUST-HIT for every refusal below: the helper CAN say yes, and when
        it does the guard is really in the config the session will load."""
        os.environ["HELM_SUITE_GUARD"] = self.good
        rc, msg = self._pre()
        self.assertEqual(rc, 0, msg)
        with open(os.path.join(self.cdir, "settings.json")) as f:
            self.assertIn("fab-suite-pretooluse",
                          " ".join(hooks._all_hook_cmds(json.load(f))))

    def test_a_relative_pin_refuses(self):
        """Divergence 1: external_status says relative-pin, the old shell rc0."""
        os.environ["HELM_SUITE_GUARD"] = "fab-suite-pretooluse"
        rc, msg = self._pre()
        self.assertNotEqual(rc, 0)
        self.assertIn("REFUSED", msg)

    def test_an_executable_directory_refuses(self):
        """Divergence 2: a DIRECTORY is `-x`, so the old shell started a
        session; external_status calls it pin-dead."""
        d = os.path.join(self.tmp, "a-dir")
        os.makedirs(d)
        os.environ["HELM_SUITE_GUARD"] = d
        rc, msg = self._pre()
        self.assertNotEqual(rc, 0)
        self.assertIn("REFUSED", msg)

    def test_a_relative_PATH_entry_refuses(self):
        """Divergence 3: `command -v` honours a relative PATH entry; the
        canonical resolver requires an absolute answer."""
        os.environ.pop("HELM_SUITE_GUARD", None)
        rel = os.path.join(self.tmp, "relbin")
        os.makedirs(rel)
        shutil.copy(self.good, os.path.join(rel, "fab-suite-pretooluse"))
        prior = os.environ.get("PATH")
        self.addCleanup(os.environ.__setitem__, "PATH", prior or "")
        os.environ["PATH"] = os.path.relpath(rel, os.getcwd())
        rc, msg = self._pre()
        self.assertNotEqual(rc, 0)
        self.assertIn("REFUSED", msg)

    def test_shortened_mint_then_install_refreshes_and_verifies(self):
        """Divergence 4, the one NO static check could catch: after a shortened
        mint the guard is absent from the config; installing a valid guard
        makes RESOLUTION read ok while the CONFIG is still missing the hook.
        The old shell said rc0 and a session started unguarded."""
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "gone")
        hooks.install_home(self.cdir, specs=hooks.SEAT_SPECS)   # shortened mint
        with open(os.path.join(self.cdir, "settings.json")) as f:
            self.assertNotIn("fab-suite-pretooluse",
                             " ".join(hooks._all_hook_cmds(json.load(f))))
        rc, _m = self._pre()
        self.assertNotEqual(rc, 0, "unresolved guard must refuse")
        # now the guard exists: resolution reads ok...
        os.environ["HELM_SUITE_GUARD"] = self.good
        spec = next(s for s in hooks.SPECS if s.get("external"))
        self.assertEqual(hooks.external_status(spec)[1], "ok")
        # ...and the helper REFRESHES so the config really carries it
        rc, msg = self._pre()
        self.assertEqual(rc, 0, msg)
        with open(os.path.join(self.cdir, "settings.json")) as f:
            self.assertIn("fab-suite-pretooluse",
                          " ".join(hooks._all_hook_cmds(json.load(f))))

    def test_a_config_missing_the_hook_refuses_even_when_resolution_is_ok(self):
        """VERIFY is a separate rung from RESOLVE: strip the hook from the file
        with the guard resolvable, and refuse anyway."""
        os.environ["HELM_SUITE_GUARD"] = self.good
        self.assertEqual(self._pre()[0], 0)
        rc, msg = hooks.preflight(self.cdir, apply=False)
        self.assertEqual(rc, 0, msg)            # must-hit: verify alone passes
        with open(os.path.join(self.cdir, "settings.json")) as f:
            got = json.load(f)
        got["hooks"]["PreToolUse"] = []
        with open(os.path.join(self.cdir, "settings.json"), "w") as f:
            json.dump(got, f)
        rc, msg = hooks.preflight(self.cdir, apply=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("REFUSED", msg)

    def test_the_generated_script_actually_refuses_when_run(self):
        """END TO END, the arm class that was missing: mint a script and RUN
        it. A string arm proved the text; this proves the behaviour."""
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "gone")
        script = os.path.join(self.tmp, "launch.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n"
                    + seat_launch_assets._guard_preflight(self.cdir)
                    + "echo SESSION_STARTED\n")
        os.chmod(script, 0o755)
        p = subprocess.run(["sh", script], capture_output=True, text=True,
                           timeout=60, env=dict(os.environ))
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("SESSION_STARTED", p.stdout)
        # MUST-HIT: the same script with the guard resolvable DOES proceed,
        # so the refusal above is the guard and not a broken script.
        os.environ["HELM_SUITE_GUARD"] = self.good
        p = subprocess.run(["sh", script], capture_output=True, text=True,
                           timeout=60, env=dict(os.environ))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SESSION_STARTED", p.stdout)

    # ---- the minting home is DATA, not an environment requirement --------

    def _script(self, tail="echo SESSION_STARTED\n", helm_bin=None):
        """A real generated script for THIS seat, written to disk."""
        from unittest import mock
        path = os.path.join(self.tmp, "launch.sh")
        ctx = (mock.patch.object(hooks, "helm_bin", return_value=helm_bin)
               if helm_bin else contextlib.nullcontext())
        with ctx:
            body = seat_launch_assets._guard_preflight(self.cdir)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + tail)
        os.chmod(path, 0o755)
        return path

    def _run(self, script, env):
        return subprocess.run(["sh", script], capture_output=True, text=True,
                              timeout=60, env=env)

    def test_the_script_launches_under_any_parent_HELM_HOME(self):  # noqa: VACUOUS_ASSERTION — the positive control is the refusal check AFTER the loop, unconditional and on the same observable; mutation-proven (unpinning HELM_HOME fails parent_env=absent and mismatched while matching still passes)
        """THE DURABLE LAUNCH REGRESSION. A seat minted under a custom
        HELM_HOME produced a script that started a session only when the
        invoking shell happened to export the same value: rc0 when it matched,
        rc1 from an ordinary shell where it was unset or pointed elsewhere.

        Which home a seat belongs to is known at mint time and never changes,
        so the script STATES it. All three parent environments must behave
        identically — that is the property, not merely that one of them works.
        """
        os.environ["HELM_SUITE_GUARD"] = self.good
        script = self._script()
        base = dict(os.environ)
        elsewhere = os.path.join(self.tmp, "some-other-home")
        os.makedirs(elsewhere, exist_ok=True)
        cases = {"matching": dict(base, HELM_HOME=os.environ["HELM_HOME"]),
                 "mismatched": dict(base, HELM_HOME=elsewhere)}
        absent = dict(base)
        absent.pop("HELM_HOME", None)
        cases["absent"] = absent
        for name, env in cases.items():
            with self.subTest(parent_env=name):
                p = self._run(script, env)
                self.assertEqual(p.returncode, 0,
                                 "%s: %s" % (name, p.stderr))
                self.assertIn("SESSION_STARTED", p.stdout, name)
        # UNCONDITIONAL CONTROL, outside the loop: the same script under the
        # same three parents still REFUSES when the guard really is gone, so
        # the passes above are the pin working and not a preflight that always
        # says yes.
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "gone")
        gone = self._script()
        # rebuild the env AFTER the change: `absent` was snapshotted while the
        # guard was still good, so reusing it would have run the control
        # against a resolvable guard and passed for the wrong reason
        gone_env = dict(os.environ)
        gone_env.pop("HELM_HOME", None)
        p = self._run(gone, gone_env)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("SESSION_STARTED", p.stdout)

    def test_the_generated_call_pins_the_minting_home(self):
        """The shape, beside the behaviour: HELM_HOME rides the call as data,
        ahead of the absolute helm invocation."""
        body = seat_launch_assets._guard_preflight(self.cdir)
        # the CALL line, not the comment block above it — a comment mentioning
        # `hooks preflight` would otherwise satisfy the ordering check
        call = next(ln for ln in body.splitlines()
                    if ln.lstrip().startswith("if !"))
        self.assertIn("HELM_HOME=", call)
        self.assertLess(call.index("HELM_HOME="), call.index("hooks preflight"))
        self.assertIn(os.environ["HELM_HOME"], call)

    # ---- a spec added AFTER generation is still enforced -----------------

    def _future_spec_helm(self):
        """A `helm` whose RUNTIME carries an external spec that did not exist
        when the script was generated. The script is never regenerated — this
        is what 'the asset asks the runtime' has to mean."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        w = os.path.join(self.tmp, "helm-future")
        with open(w, "w") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "sys.path.insert(0, %r)\n"
                "from helm import hooks\n"
                "if os.environ.get('HELM_TEST_FUTURE_SPEC'):\n"
                "    hooks.SPECS = hooks.SPECS + ({'name': 'future-guard',\n"
                "        'event': 'PreToolUse', 'args': '', 'timeout': 3,\n"
                "        'own': ('helm-future-guard',), 'matcher': 'Bash',\n"
                "        'gate': True,\n"
                "        'external': 'helm-future-guard-not-on-this-host'},)\n"
                "    hooks.SEAT_SPECS = hooks.SPECS\n"
                "a = sys.argv[1:]\n"
                "if a and a[0] == 'hooks':\n"
                "    a = a[1:]\n"
                "sys.exit(hooks.cmd_hooks(a))\n" % repo)
        os.chmod(w, 0o755)
        return w

    def test_an_already_generated_script_enforces_a_POST_GENERATION_spec(self):
        """The claim the old arm only gestured at with an assertNotIn.

        The script is generated ONCE and never touched again. A new external
        spec then appears in the runtime it calls — exactly what happens when
        someone adds a guard to hooks.SPECS and the fleet's launch.sh files are
        months old. The unchanged script must refuse."""
        os.environ["HELM_SUITE_GUARD"] = self.good
        script = self._script(helm_bin=self._future_spec_helm())
        env = dict(os.environ)
        env.pop("HELM_TEST_FUTURE_SPEC", None)
        # MUST-HIT: before the new spec exists this very script starts a
        # session, so the refusal below is the future spec and nothing else.
        p = self._run(script, env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SESSION_STARTED", p.stdout)
        # the SAME script, unregenerated, against a runtime that now carries an
        # unresolvable external spec
        p = self._run(script, dict(env, HELM_TEST_FUTURE_SPEC="1"))
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("SESSION_STARTED", p.stdout)
        self.assertIn("future-guard", p.stderr)


if __name__ == "__main__":
    unittest.main()
