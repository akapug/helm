#!/usr/bin/env python3
"""The suite's env plants hold UNDER THE CANONICAL RUNNER.

Every other test in this repo trusts that importing `tests` has already
pointed HELM_CHAT_DIR, HELM_HOME, HELM_CONFIG_ROOTS and HELM_METAHARNESS
somewhere harmless. That trust was misplaced for as long as the plants lived
in tests/conftest.py, because `python -m unittest discover` — what CI runs and
what CONTRIBUTING.md and AGENTS.md tell a developer to run — never imports a
conftest. The plants were real, tested by hand under pytest, and inert in
every run that mattered.

So these tests may not simply read os.environ: THIS process already has the
plants applied, and asserting on them would pass whether or not the mechanism
works. Each case re-enters a CLEAN subprocess with the whole HELM_/MELD_
namespace stripped — the state a CI box actually starts in — imports `tests`
the way unittest discovery does, and asks the real consumers
(chat.chat_dir(), home.helm_home(), _common.CWD_ROOTS) where they ended up.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from helm import chat

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Imports `tests` exactly as unittest discovery does (the package before any
# test module), then reports what the REAL consumers resolved to — not what the
# environment says, which is what made the conftest version look correct.
_PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import tests
from helm import chat, home
from helm.configs import _common
print(json.dumps({
    "HELM_CONFIG_ROOTS": os.environ.get("HELM_CONFIG_ROOTS"),
    "HELM_HOME": os.environ.get("HELM_HOME"),
    "HELM_CHAT_DIR": os.environ.get("HELM_CHAT_DIR"),
    "HELM_METAHARNESS": os.environ.get("HELM_METAHARNESS"),
    "chat_dir": chat.chat_dir(),
    "helm_home": home.helm_home(),
    "cwd_roots": _common.CWD_ROOTS,
}))
"""

_NAMESPACES = ("HELM_", "MELD_")


def _probe(**overrides):
    """Import `tests` in a subprocess whose helm env is exactly `overrides`."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_NAMESPACES)}
    env.update(overrides)
    proc = subprocess.run([sys.executable, "-c", _PROBE, REPO],
                          capture_output=True, text=True, cwd=REPO,
                          env=env, timeout=120)
    if proc.returncode:
        raise AssertionError("probe failed (%d):\n%s" % (proc.returncode, proc.stderr))
    return json.loads(proc.stdout)


class CanonicalRunnerTest(unittest.TestCase):
    """THE MUST-HIT: a clean unittest-shaped import leaves nothing pointed at
    live machine state. This is the assertion that was false before the plants
    moved out of conftest.py."""

    # DISCOVERY-ONLY probe: it NEVER imports tests itself, so it answers the
    # question the manual-import probe cannot — does the RUNNER's own
    # collection plant the vars? codex, receipt c6eb85369d6a8cc3: "existing
    # probe imports tests itself". That arm proves importing the package
    # plants; it cannot prove the package is imported, and those are different
    # claims. A fixture that creates the condition it asserts is the class my
    # own stored heuristic names.
    # THE LITERAL CLI, not a TestLoader model of it. codex's r4 FIX
    # (2e5423e01536): "TestLoader model differs from canonical CLI; bare
    # discover -s tests never imports tests (RED), while -t . is GREEN."
    # That is correct and it made the earlier arm assert something FALSE about
    # the real runner: TestLoader().discover(start_dir="tests") imports the
    # package under both forms, so the arm passed for both, while the actual
    # CLI plants under -t . and NOT without it. A probe that models the runner
    # can only ever prove things about the model.
    #
    # The reporter arm below runs INSIDE the spawned CLI and writes what it
    # sees; the parent reads the file. Reporting from inside is the only way to
    # observe a plant that lives in the child's os.environ.
    _REPORT_VAR = "SUITE_ENV_REPORT"      # deliberately NOT HELM_-prefixed:
                                          # the parent strips every HELM_/MELD_
                                          # var, and a HELM_ name would be eaten

    def _discovery_probe(self, top):
        """Run the CANONICAL CLI once, with or without -t ., and return what a
        test running inside it actually saw. argv is the literal thing the gate
        invokes (`helm gate show` records it as
        `python -m unittest discover -s tests -t .`), never a loader stand-in."""
        import json as _j, subprocess as _sp, sys as _s, os as _o, tempfile as _tf
        root = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
        env = {k: v for k, v in _o.environ.items()
               if not k.startswith(("HELM_", "MELD_"))}
        fd, path = _tf.mkstemp(prefix="suite-env-report-")
        _o.close(fd)
        env[self._REPORT_VAR] = path
        argv = [_s.executable, "-m", "unittest", "discover", "-s", "tests"]
        if top:
            argv += ["-t", top]
        argv += ["-p", "test_suite_env.py", "-k", "test_REPORTER_writes_what_it_saw"]
        out = _sp.run(argv, capture_output=True, text=True, cwd=root, env=env,
                      timeout=300)
        try:
            with open(path) as f:
                body = f.read().strip()
        finally:
            _o.unlink(path)
        # THE CHILD'S rc IS EVIDENCE AND I DROPPED IT. codex's r5 FIX:
        # "_discovery_probe ignores child rc; reporter data plus rc=7 returns
        # success." The arm this replaced DID assert rc == 0; I removed it when
        # I rewrote the probe to spawn the real CLI, so a suite that FAILED
        # while the reporter still wrote its file read as a clean probe. The
        # reporter writing is evidence that discovery reached it — it is not
        # evidence that the run succeeded, and those are different claims.
        self.assertEqual(out.returncode, 0,
                         "the spawned canonical runner exited %s under %r:\n%s"
                         % (out.returncode, argv, out.stderr[-500:]))
        self.assertTrue(body, "the reporter arm never ran under %r: rc=%s\n%s"
                        % (argv, out.returncode, out.stderr[-500:]))
        return _j.loads(body)

    def test_REPORTER_writes_what_it_saw(self):  # noqa: VACUOUS_ASSERTION — a reporter, not an assertion; it is the instrument the arms below read
        """NOT AN ASSERTION — the instrument. Runs inside a spawned canonical
        CLI and records what that process actually resolved. Skips (writing
        nothing) in an ordinary suite run, so it costs the fleet nothing."""
        import json as _j, os as _o
        path = _o.environ.get(self._REPORT_VAR)
        if not path:
            self.skipTest("reporter idle outside a canonical-runner probe")
        from helm import chat, home
        import sys as _s
        with open(path, "w") as f:
            f.write(_j.dumps({
                "tests_imported": "tests" in _s.modules,
                "HELM_CHAT_DIR": _o.environ.get("HELM_CHAT_DIR"),
                "chat_dir": chat.chat_dir(),
                "helm_home": home.helm_home(),
            }))

    def test_the_CANONICAL_form_plants_AND_the_bare_form_PROVABLY_DOES_NOT(self):  # noqa: VACUOUS_ASSERTION — the -t . half above IS the control
        # and it is unconditional: assertTrue(planted[tests_imported]) plus
        # assertIn('helm-suite-env-', planted[chat_dir]) run before every
        # absence assertion here. The rung cannot credit it because the two
        # halves are separate _discovery_probe CALLS and therefore separate
        # producer identities by its own documented rule — which is exactly
        # why the pair proves anything: same instrument, two runner forms,
        # opposite results. Splitting them into two arms would let either
        # pass alone while the asymmetry silently disappeared.
        """THE CLAIM CORRECTED. The arm this replaces asserted that BOTH
        discovery forms plant, and it passed — because it drove
        TestLoader().discover(), which imports the `tests` package either way.
        The real CLI does not: without -t, the start dir BECOMES the top level,
        modules load as bare test_* and tests/__init__ never executes, so
        nothing plants. codex measured that (r4 FIX, row 2e5423e01536) and was
        right; my arm was true of the model and false of the runner.

        So this asserts the asymmetry instead of denying it, which is the only
        version that can fail when it should. -t . plants. Bare does NOT, and
        that is pinned as a FACT, not a bug to be papered: it is exactly why
        the canonical invocation must carry -t ., which the next arm enforces.

        Both directions live in one arm on purpose. Either alone is satisfiable
        by a probe that never ran: if the bare form silently started planting,
        a one-sided arm would keep passing while the reason for the -t . rule
        evaporated."""
        planted = self._discovery_probe(".")
        bare = self._discovery_probe("")

        # -t . : the package loads, the plants fire, nothing points at live state
        self.assertTrue(planted["tests_imported"],
                        "-t . : discovery never imported the tests package")
        self.assertIn("helm-suite-env-", planted["chat_dir"] or "",
                      "-t . : chat_dir=%s is not a planted tmp dir — the suite "
                      "can reach the live fleet bus" % planted["chat_dir"])
        self.assertNotIn("/dev/shm/helm-chat", planted["chat_dir"] or "")

        # bare: the package never loads, so nothing plants. Asserting the
        # ABSENCE is what makes the -t . requirement falsifiable.
        self.assertFalse(bare["tests_imported"],
                         "bare discover imported tests — if that is now true "
                         "the -t . requirement may be obsolete; re-derive it "
                         "rather than deleting this arm")
        self.assertNotIn("helm-suite-env-", bare["chat_dir"] or "",
                         "bare discover planted a tmp chat_dir, which "
                         "contradicts the mechanism this whole lane rests on")

    def test_the_CANONICAL_INVOCATION_is_wired_to_carry_dash_t(self):
        """THE WIRING HALF, and my first version of it was VACUOUS — codex's r5
        FIX: "deleting the SUITE -t/. tokens still leaves -t in gate source
        comments, so it stays green."

        I asserted `"-t" in inspect.getsource(gate)`. That substring survives in
        prose and comments, so the exact mutation this pin exists to catch —
        removing -t from the invocation — left it green. A guard that cannot
        fail on its own subject is decoration.

        So it pins the ARGV ITSELF: gate.SUITE is the tuple the runner spawns,
        and it is asserted whole. Delete a token, reorder it, or split -t from
        its "." and this fails."""
        from helm import gate as _gate
        self.assertEqual(
            _gate.SUITE, ("-m", "unittest", "discover", "-s", "tests", "-t", "."),
            "the gate's suite argv changed. Bare discovery does NOT execute "
            "tests/__init__, so the plants never fire and every arm reads live "
            "machine state — re-derive this pin against the new invocation "
            "rather than widening it.")
        # THE PAIRING is the load-bearing part, asserted separately so a future
        # reorder cannot satisfy the whole-tuple check by accident: -t must be
        # immediately followed by the top-level dir.
        i = _gate.SUITE.index("-t")
        self.assertEqual(_gate.SUITE[i + 1], ".",
                         "-t is present but not followed by the top-level dir")

    def test_import_of_tests_package_plants_every_var(self):
        got = _probe()
        tmp = tempfile.gettempdir()
        for var in ("HELM_CONFIG_ROOTS", "HELM_HOME", "HELM_CHAT_DIR"):
            self.assertTrue(got[var], "%s unset after importing tests" % var)
            self.assertTrue(got[var].startswith(tmp),
                            "%s=%s is not under %s" % (var, got[var], tmp))
            self.assertIn("helm-suite-env-", got[var])
        self.assertEqual(got["HELM_METAHARNESS"], "none")

    def test_the_real_consumers_resolve_off_the_live_machine(self):
        """The env vars are the mechanism; these three are the point."""
        got = _probe()
        self.assertEqual(got["chat_dir"], got["HELM_CHAT_DIR"])
        self.assertNotEqual(got["chat_dir"], chat.DEFAULT_DIR)   # the fleet bus
        self.assertEqual(got["helm_home"], got["HELM_HOME"])
        self.assertNotEqual(got["helm_home"],
                            os.path.join(os.path.expanduser("~"), ".helm"))
        self.assertEqual(got["cwd_roots"], [got["HELM_CONFIG_ROOTS"]])
        self.assertNotIn(os.path.join(os.path.expanduser("~"), "dev"),
                         got["cwd_roots"])


class EmptyIsUnsetTest(unittest.TestCase):
    """`os.environ.setdefault` reads an exported-but-empty var as already
    chosen; every consumer reads it as absent and falls through to the live
    default. The plant has to agree with the consumers."""

    def test_empty_chat_dir_does_not_reach_the_fleet_bus(self):
        got = _probe(HELM_CHAT_DIR="")
        self.assertTrue(got["HELM_CHAT_DIR"])
        self.assertNotEqual(got["chat_dir"], chat.DEFAULT_DIR)

    def test_empty_home_does_not_reach_the_real_home(self):
        got = _probe(HELM_HOME="")
        self.assertTrue(got["HELM_HOME"])
        self.assertNotEqual(got["helm_home"],
                            os.path.join(os.path.expanduser("~"), ".helm"))

    def test_empty_config_roots_still_gets_a_planted_tree(self):
        got = _probe(HELM_CONFIG_ROOTS="")
        self.assertTrue(got["HELM_CONFIG_ROOTS"])
        self.assertEqual(got["cwd_roots"], [got["HELM_CONFIG_ROOTS"]])

    def test_empty_metaharness_does_not_re_arm_the_live_adapter(self):
        # detect() reads `(env.get(...) or "").strip()`, so "" falls through to
        # the orca/herdr probe on the operator's own box.
        got = _probe(HELM_METAHARNESS="")
        self.assertEqual(got["HELM_METAHARNESS"], "none")

    def test_every_var_empty_at_once(self):
        got = _probe(HELM_CHAT_DIR="", HELM_HOME="", HELM_CONFIG_ROOTS="",
                     HELM_METAHARNESS="")
        self.assertNotEqual(got["chat_dir"], chat.DEFAULT_DIR)
        self.assertNotEqual(got["helm_home"],
                            os.path.join(os.path.expanduser("~"), ".helm"))
        self.assertEqual(got["HELM_METAHARNESS"], "none")


class DeliberateValueWinsTest(unittest.TestCase):
    """setdefault semantics survive the rewrite: a module, a harness or a CI
    cell that chose a value knows something the plant does not."""

    def test_explicit_values_are_left_alone(self):
        got = _probe(HELM_CHAT_DIR="/tmp/deliberate-chat",
                     HELM_HOME="/tmp/deliberate-home",
                     HELM_CONFIG_ROOTS="/tmp/deliberate-roots",
                     HELM_METAHARNESS="herdr")
        self.assertEqual(got["chat_dir"], "/tmp/deliberate-chat")
        self.assertEqual(got["helm_home"], "/tmp/deliberate-home")
        self.assertEqual(got["cwd_roots"], ["/tmp/deliberate-roots"])
        self.assertEqual(got["HELM_METAHARNESS"], "herdr")

    def test_a_colon_list_of_roots_survives_whole(self):
        got = _probe(HELM_CONFIG_ROOTS="/tmp/one:/tmp/two")
        self.assertEqual(got["cwd_roots"], ["/tmp/one", "/tmp/two"])


class LegacyNamespaceTest(unittest.TestCase):
    """Planting the HELM_ name masks the older name helm still honors, so an
    operator's deliberate MELD_ choice would be silently overridden by a
    plant whose entire contract is to defer to deliberate choices."""

    def test_meld_chat_dir_is_honored(self):
        got = _probe(MELD_CHAT_DIR="/tmp/legacy-chat")
        self.assertEqual(got["chat_dir"], "/tmp/legacy-chat")

    def test_meld_home_is_honored(self):
        got = _probe(MELD_HOME="/tmp/legacy-home")
        self.assertEqual(got["helm_home"], "/tmp/legacy-home")

    def test_an_empty_helm_name_does_not_mask_the_legacy_one(self):
        # home.env only falls through to MELD_ when HELM_ is None, so leaving
        # an empty HELM_CHAT_DIR in place would bury the override AND land on
        # the fleet bus. The plant pops the empty key instead of copying the
        # value across — env_pair's rule is that a HELM value must never carry
        # MELD provenance.
        got = _probe(MELD_CHAT_DIR="/tmp/legacy-chat", HELM_CHAT_DIR="")
        self.assertEqual(got["chat_dir"], "/tmp/legacy-chat")
        got = _probe(MELD_HOME="/tmp/legacy-home", HELM_HOME="")
        self.assertEqual(got["helm_home"], "/tmp/legacy-home")

    def test_an_explicit_helm_name_still_outranks_the_legacy_one(self):
        got = _probe(MELD_CHAT_DIR="/tmp/legacy-chat",
                     HELM_CHAT_DIR="/tmp/preferred-chat")
        self.assertEqual(got["chat_dir"], "/tmp/preferred-chat")


class ConftestDelegatesTest(unittest.TestCase):
    """Two copies of this logic is how one path starts drifting from the
    other, so the pytest entry may not restate it."""

    def test_conftest_holds_no_planting_of_its_own(self):
        with open(os.path.join(REPO, "tests", "conftest.py"), encoding="utf-8") as f:
            body = [ln for ln in f.read().split("\n")
                    if "os.environ" in ln and not ln.lstrip().startswith("#")]
        # the only os.environ mentions left are inside the docstring's prose
        self.assertEqual([ln for ln in body if "=" in ln], [])


if __name__ == "__main__":
    unittest.main()
