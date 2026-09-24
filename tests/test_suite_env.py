#!/usr/bin/env python3
"""The suite's env plants hold UNDER THE CANONICAL RUNNER.

Every other test in this repo trusts that importing `tests` has already
pointed HELM_CHAT_DIR, HELM_HOME, HELM_CONFIG_ROOTS and HELM_METAHARNESS
somewhere harmless. That trust was misplaced for as long as the plants lived
in tests/conftest.py, because unittest discovery — what CI and gateshard run and
what CONTRIBUTING.md and AGENTS.md tell a developer to run — never imports a
conftest. The plants were real, tested by hand under pytest, and inert in
every run that mattered.

So these tests may not simply read os.environ: THIS process already has the
plants applied, and asserting on them would pass whether or not the mechanism
works. Each case re-enters a CLEAN subprocess with the whole HELM_/MELD_
namespace (and the synthetic predecessor's) stripped — the state a CI box actually starts in — imports `tests`
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

_NAMESPACES = ("HELM_", "MELD_", "OLDTOOL_")


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


def _isolated_child_env(root):
    """A credential-free synthetic estate for a canonical child unittest."""
    names = ("user-home", "helm-home", "adopted", "tmp", "config", "chat",
             "orca-user-data", "xdg")
    paths = {name: os.path.join(root, name) for name in names}
    for path in paths.values():
        os.makedirs(path)
    env = {
        "HOME": paths["user-home"],
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": paths["tmp"],
        "XDG_CONFIG_HOME": paths["xdg"],
        "HELM_HOME": paths["helm-home"],
        "HELM_ADOPTED_DIR": paths["adopted"],
        "HELM_CONFIG_ROOTS": paths["config"],
        "HELM_CHAT_DIR": paths["chat"],
        "HELM_METAHARNESS": "none",
        "ORCA_USER_DATA_PATH": paths["orca-user-data"],
    }
    return env, paths


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

    def test_the_CANONICAL_INVOCATION_is_literal_serial_discovery(self):
        """Landing authority is the same-process unittest CLI, not gateshard."""
        from helm import gate as _gate
        self.assertEqual(_gate.SUITE, (
            "-m", "unittest", "discover", "-s", "tests", "-t", ".",
        ))

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


class HomeFollowingStoreIsolationTest(unittest.TestCase):
    """Writable stores follow the suite home, never a parent's explicit path."""

    _REPORT_VAR = "SUITE_HOME_STORE_REPORT"
    _FAMILY_VAR = "SUITE_HOME_STORE_FAMILY"
    _LOCAL_OVERRIDE_VAR = "SUITE_HOME_STORE_LOCAL_OVERRIDE"
    _KEYS = {
        "turnstamp": ("HELM_TURNSTAMP_DIR", "MELD_TURNSTAMP_DIR"),
        "multiplayer": ("HELM_MULTIPLAYER_DIR", "MELD_MULTIPLAYER_DIR"),
    }

    def test_REPORTER_home_following_store_write(self):  # noqa: VACUOUS_ASSERTION — the parent asserts the real write/read, destination and both parent sentinels
        report = os.environ.get(self._REPORT_VAR)
        family = os.environ.get(self._FAMILY_VAR)
        if not report or family not in self._KEYS:
            self.skipTest("reporter idle outside a home-following-store probe")
        modern, legacy = self._KEYS[family]
        inherited = {key: os.environ.get(key) for key in (modern, legacy)}
        local = os.environ.get(self._LOCAL_OVERRIDE_VAR)
        if local:
            os.environ[legacy] = local

        if family == "turnstamp":
            from helm import turnstamp
            written, error = turnstamp.record(
                "seat-a", "sess-abc", now=123.0,
                roster_rows={"seat-a": {"session": "sess-abc"}})
            row, read_error = turnstamp.last_allowed(
                "seat-a", session="sess-abc")
            observed = {
                "store_dir": turnstamp.stamp_dir(),
                "path": written,
                "error": error,
                "read_error": read_error,
                "row": row,
            }
        else:
            from helm import multiplayer
            relay, _presence = multiplayer.adapters()
            ack = relay.publish(
                "Synthetic Cave", "Plan", "synthetic-actor", "opaque-update")
            state = relay.updates("Synthetic Cave", "Plan")
            observed = {
                "store_dir": multiplayer.multiplayer_dir(),
                "path": relay.path("Synthetic Cave", "Plan"),
                "ack": ack,
                "state": state,
            }
        observed.update({
            "inherited_after_bootstrap": inherited,
            "aliases_after_override": {
                key: os.environ.get(key) for key in (modern, legacy)},
        })
        with open(report, "w", encoding="utf-8") as fh:
            json.dump(observed, fh, sort_keys=True)

    def _run_variant(self, family, explicit):
        modern, legacy = self._KEYS[family]
        with tempfile.TemporaryDirectory(prefix="suite-%s-" % family) as root:
            env, paths = _isolated_child_env(root)
            parent = os.path.join(root, "parent-%s" % family)
            os.makedirs(parent)
            sentinel = os.path.join(parent, "parent-sentinel")
            sentinel_bytes = b"parent-sentinel\n"
            with open(sentinel, "wb") as fh:
                fh.write(sentinel_bytes)
            sentinel_stat = os.stat(sentinel)
            report = os.path.join(root, "report.json")
            local = os.path.join(root, "test-local-%s" % family)
            env.update({
                modern: parent,
                legacy: parent,
                self._REPORT_VAR: report,
                self._FAMILY_VAR: family,
            })
            if explicit:
                env[self._LOCAL_OVERRIDE_VAR] = local
            argv = [
                sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-t", ".", "-p", "test_suite_env.py",
                "-k", "test_REPORTER_home_following_store_write",
            ]
            out = subprocess.run(
                argv, capture_output=True, text=True, cwd=REPO, env=env,
                timeout=300)
            self.assertEqual(
                out.returncode, 0,
                "the spawned canonical runner exited %s under %r:\n%s"
                % (out.returncode, argv, out.stderr[-500:]))
            with open(report, encoding="utf-8") as fh:
                observed = json.load(fh)

            self.assertEqual(observed["inherited_after_bootstrap"],
                             {modern: None, legacy: None})
            expected = local if explicit else os.path.join(
                paths["helm-home"],
                "turnstamps" if family == "turnstamp" else "helm-multiplayer")
            self.assertEqual(observed["store_dir"], expected)
            if explicit:
                self.assertEqual(observed["aliases_after_override"],
                                 {modern: None, legacy: local})
            else:
                self.assertEqual(observed["aliases_after_override"],
                                 {modern: None, legacy: None})

            if family == "turnstamp":
                self.assertIsNone(observed["error"])
                self.assertIsNone(observed["read_error"])
                self.assertEqual(observed["row"]["seat"], "seat-a")
                self.assertEqual(observed["row"]["session"], "sess-abc")
                self.assertEqual(observed["row"]["allowed_at"], 123.0)
                self.assertEqual(os.path.dirname(observed["path"]), expected)
            else:
                self.assertEqual(observed["ack"]["actor"], "synthetic-actor")
                self.assertEqual(observed["ack"]["bytes"], len("opaque-update"))
                self.assertEqual(
                    [row["update"] for row in observed["state"]["updates"]],
                    ["opaque-update"])
                self.assertEqual(
                    os.path.dirname(os.path.dirname(observed["path"])), expected)
            self.assertTrue(os.path.isfile(observed["path"]))

            self.assertEqual(os.listdir(parent), ["parent-sentinel"],
                             "the child wrote into its parent's inherited store")
            with open(sentinel, "rb") as fh:
                self.assertEqual(fh.read(), sentinel_bytes)
            after_stat = os.stat(sentinel)
            self.assertEqual((after_stat.st_dev, after_stat.st_ino),
                             (sentinel_stat.st_dev, sentinel_stat.st_ino))

    def test_inherited_parent_stores_are_private_and_a_legacy_override_still_works(self):  # noqa: VACUOUS_ASSERTION — every child performs a real write/read before the parent sentinel inode and bytes are compared
        for family in self._KEYS:
            for explicit in (False, True):
                with self.subTest(family=family,
                                  post_bootstrap_legacy_override=explicit):
                    self._run_variant(family, explicit)


class HookAlarmIsolationTest(unittest.TestCase):
    """A child test run never shares its parent's timeout suppression window."""

    _REPORT_VAR = "SUITE_HOOK_ALARM_REPORT"
    _LOCAL_OVERRIDE_VAR = "SUITE_HOOK_ALARM_LOCAL_OVERRIDE"

    def test_REPORTER_hook_alarm_timeout(self):  # noqa: VACUOUS_ASSERTION — the parent asserts both timed outcomes, stderr lines, and marker destinations
        path = os.environ.get(self._REPORT_VAR)
        if not path:
            self.skipTest("reporter idle outside a hook-alarm probe")
        inherited = os.environ.get("HELM_HOOK_ALARM_DIR")
        local = os.environ.get(self._LOCAL_OVERRIDE_VAR)
        if local:
            os.environ["HELM_HOOK_ALARM_DIR"] = local

        import contextlib as _contextlib
        import io as _io
        import time as _time
        from unittest import mock as _mock
        from helm import hookalarm, hookrun

        spec = {"name": "synthetic-timed", "event": "Synthetic",
                "args": "synthetic-timed", "timeout": .02,
                "matcher": None}
        outcomes, returns = [], []
        err = _io.StringIO()

        def timed(_argv):
            _time.sleep(10)
            return 0

        # Keep both real SIGALRM timeouts in one limiter window even when the
        # test starts immediately before a ten-minute UTC boundary.
        with _contextlib.redirect_stderr(err), \
                _mock.patch("helm.cli.main", side_effect=timed), \
                _mock.patch("helm.hookalarm._bucket",
                            return_value=hookalarm._bucket(_time.time())):
            for _ in range(2):
                outcome = {}
                returns.append(hookrun.run_one(spec, "{}", outcome=outcome))
                outcomes.append(outcome)

        alarm_dir = os.environ.get("HELM_HOOK_ALARM_DIR")
        markers = []
        if alarm_dir and os.path.isdir(alarm_dir):
            markers = sorted((name, os.path.getsize(os.path.join(alarm_dir, name)))
                             for name in os.listdir(alarm_dir))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "inherited_after_bootstrap": inherited,
                "alarm_dir_after_override": alarm_dir,
                "gate_cap": os.environ.get("HELM_GATE_SUITE_CAP"),
                "returns": returns,
                "outcomes": outcomes,
                "stderr": err.getvalue(),
                "markers": markers,
            }, fh, sort_keys=True)

    def _run_variant(self, gate_cap=None, explicit=False):
        with tempfile.TemporaryDirectory(prefix="suite-hook-alarm-") as root:
            home = os.path.join(root, "helm-home")
            adopted = os.path.join(root, "adopted")
            user_home = os.path.join(root, "user-home")
            tmp = os.path.join(root, "tmp")
            parent = os.path.join(root, "parent-alarm")
            for directory in (home, adopted, user_home, tmp, parent):
                os.makedirs(directory)
            sentinel = os.path.join(parent, "parent-sentinel")
            with open(sentinel, "wb") as fh:
                fh.write(b"parent-sentinel\n")
            sentinel_stat = os.stat(sentinel)
            report = os.path.join(root, "report.json")
            local = os.path.join(root, "test-local-alarm")
            env = {
                "HOME": user_home,
                "PATH": os.environ.get("PATH", ""),
                "TMPDIR": tmp,
                "HELM_HOME": home,
                "HELM_ADOPTED_DIR": adopted,
                "HELM_HOOK_ALARM_DIR": parent,
                self._REPORT_VAR: report,
            }
            if gate_cap is not None:
                env["HELM_GATE_SUITE_CAP"] = gate_cap
            if explicit:
                env[self._LOCAL_OVERRIDE_VAR] = local
            argv = [
                sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-t", ".", "-p", "test_suite_env.py",
                "-k", "test_REPORTER_hook_alarm_timeout",
            ]
            out = subprocess.run(
                argv, capture_output=True, text=True, cwd=REPO, env=env,
                timeout=300)
            self.assertEqual(
                out.returncode, 0,
                "the spawned canonical runner exited %s under %r:\n%s"
                % (out.returncode, argv, out.stderr[-500:]))
            with open(report, encoding="utf-8") as fh:
                observed = json.load(fh)

            self.assertIsNone(observed["inherited_after_bootstrap"])
            self.assertEqual(observed["gate_cap"], gate_cap)
            self.assertEqual(observed["returns"], [0, 0])
            self.assertEqual([row.get("status") for row in observed["outcomes"]],
                             ["unchecked", "unchecked"])
            self.assertEqual([row.get("why") for row in observed["outcomes"]],
                             ["handler timed out after 0.02s"] * 2)
            line = ("[helm synthetic-timed] TIMED OUT at 0.02s — "
                    "this event is UNCHECKED\n")
            if explicit:
                self.assertEqual(observed["alarm_dir_after_override"], local)
                self.assertEqual(observed["stderr"], line)
                self.assertEqual(len(observed["markers"]), 1)
                marker, size = observed["markers"][0]
                self.assertTrue(marker.startswith("handler-synthetic-timed."),
                                observed["markers"])
                self.assertEqual(size, 1,
                                 "the second timed handler did not append its "
                                 "suppressed marker byte")
            else:
                self.assertIsNone(observed["alarm_dir_after_override"])
                self.assertEqual(observed["stderr"], line * 2)
                self.assertEqual(observed["markers"], [])

            self.assertEqual(os.listdir(parent), ["parent-sentinel"],
                             "the child wrote a suppression marker into its "
                             "parent's inherited alarm directory")
            with open(sentinel, "rb") as fh:
                self.assertEqual(fh.read(), b"parent-sentinel\n")
            after_stat = os.stat(sentinel)
            self.assertEqual((after_stat.st_dev, after_stat.st_ino),
                             (sentinel_stat.st_dev, sentinel_stat.st_ino))

    def test_inherited_alarm_dir_is_absent_and_gate_cap_cannot_disable_the_bypass(self):  # noqa: VACUOUS_ASSERTION — each child proves two real SIGALRM outcomes before comparing diagnostic multiplicity and marker writes
        for label, gate_cap, explicit in (
                ("no-gate-cap", None, False),
                ("gate-cap-present", "1", False),
                ("post-bootstrap-private-override", None, True),
                ("override-with-gate-cap", "1", True)):
            with self.subTest(case=label):
                self._run_variant(gate_cap=gate_cap, explicit=explicit)


class ProxyJournalIsolationTest(unittest.TestCase):
    """A child test run owns its journal even when its parent chose one."""

    _REPORT_VAR = "SUITE_PROXYJOURNAL_REPORT"
    _LOCAL_OVERRIDE_VAR = "SUITE_PROXYJOURNAL_LOCAL_OVERRIDE"

    def test_REPORTER_proxyjournal_append(self):  # noqa: VACUOUS_ASSERTION — this is the child instrument; its parent asserts the real write, parsed row, and both destination files
        path = os.environ.get(self._REPORT_VAR)
        if not path:
            self.skipTest("reporter idle outside a proxy-journal probe")
        inherited = os.environ.get("HELM_PROXYJOURNAL_LOG")
        local = os.environ.get(self._LOCAL_OVERRIDE_VAR)
        if local:
            os.environ["HELM_PROXYJOURNAL_LOG"] = local
        from helm import proxyjournal
        before = {
            "state": "PROXY-COOLDOWN",
            "falsification_observed_at": "2026-09-23T15:00:00Z",
            "falsification_proxy_identity": "synthetic-proxy",
            "falsification_identity_state": "VERIFIED",
            "falsification_age_s": 226,
            "falsification_due": True,
        }
        written = proxyjournal.record_episode_end(
            "synthetic-family", before, "OK")
        rows, err = proxyjournal.read()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "inherited_after_bootstrap": inherited,
                "log_path": proxyjournal.log_path(),
                "written": written,
                "rows": rows,
                "error": err,
            }, fh, sort_keys=True)

    def _run_variant(self, explicit):
        with tempfile.TemporaryDirectory(prefix="suite-proxyjournal-") as root:
            home = os.path.join(root, "helm-home")
            adopted = os.path.join(root, "adopted")
            user_home = os.path.join(root, "user-home")
            tmp = os.path.join(root, "tmp")
            for path in (home, adopted, user_home, tmp):
                os.makedirs(path)
            parent = os.path.join(root, "parent-proxywatch.log")
            sentinel = b"parent-sentinel\n"
            with open(parent, "wb") as fh:
                fh.write(sentinel)
            parent_stat = os.stat(parent)
            report = os.path.join(root, "report.json")
            local = os.path.join(home, "test-local", "proxywatch.log")
            expected = local if explicit else os.path.join(
                home, "helm", "pause-ops", "proxywatch.log")
            env = {
                "HOME": user_home,
                "PATH": os.environ.get("PATH", ""),
                "TMPDIR": tmp,
                "HELM_HOME": home,
                "HELM_ADOPTED_DIR": adopted,
                "HELM_PROXYJOURNAL_LOG": parent,
                self._REPORT_VAR: report,
            }
            if explicit:
                env[self._LOCAL_OVERRIDE_VAR] = local
            argv = [
                sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-t", ".", "-p", "test_suite_env.py",
                "-k", "test_REPORTER_proxyjournal_append",
            ]
            out = subprocess.run(
                argv, capture_output=True, text=True, cwd=REPO, env=env,
                timeout=300)
            self.assertEqual(
                out.returncode, 0,
                "the spawned canonical runner exited %s under %r:\n%s"
                % (out.returncode, argv, out.stderr[-500:]))
            with open(report, encoding="utf-8") as fh:
                observed = json.load(fh)
            with open(parent, "rb") as fh:
                self.assertEqual(fh.read(), sentinel)
            after_stat = os.stat(parent)
            self.assertEqual((after_stat.st_dev, after_stat.st_ino),
                             (parent_stat.st_dev, parent_stat.st_ino))
            self.assertEqual(observed["inherited_after_bootstrap"], None)
            self.assertEqual(observed["log_path"], expected)
            self.assertTrue(observed["written"])
            self.assertIsNone(observed["error"])
            self.assertEqual(len(observed["rows"]), 1)
            row = observed["rows"][0]
            self.assertEqual(row["event"], "PROXY-FALSIFY-END")
            self.assertEqual(row["family"], "synthetic-family")
            self.assertEqual(row["was"], "PROXY-COOLDOWN")
            self.assertEqual(row["now"], "OK")
            self.assertEqual(row["proxy_identity"], "synthetic-proxy")
            self.assertEqual(row["age_s"], "226")
            self.assertTrue(os.path.isfile(expected))

    def test_inherited_parent_journal_is_private_and_a_post_bootstrap_override_still_works(self):  # noqa: VACUOUS_ASSERTION — both variants assert a real valid append before proving the parent sentinel stayed byte-for-byte and inode-for-inode unchanged
        for explicit in (False, True):
            with self.subTest(explicit_test_local_override=explicit):
                self._run_variant(explicit)


class SeatNameAuthorityIsolationTest(unittest.TestCase):
    """A bootstrapped child projects real joins only into its own authority."""

    _REPORT_VAR = "SUITE_SEAT_AUTHORITY_REPORT"
    _LOCAL_OVERRIDE_VAR = "SUITE_SEAT_AUTHORITY_LOCAL_OVERRIDE"

    def _run_variant(self, inherited=None, explicit=False):
        with tempfile.TemporaryDirectory(prefix="suite-seat-authority-") as root:
            home = os.path.join(root, "helm-home")
            adopted = os.path.join(root, "adopted")
            user_home = os.path.join(root, "user-home")
            tmp = os.path.join(root, "tmp")
            xdg = os.path.join(root, "xdg")
            for path in (home, adopted, user_home, tmp, xdg):
                os.makedirs(path)
            parent = os.path.join(root, "parent-seat-names.txt")
            sentinel = b"parent-sentinel\n"
            with open(parent, "wb") as fh:
                fh.write(sentinel)
            parent_stat = os.stat(parent)
            report = os.path.join(root, "report.json")
            local = os.path.join(home, "test-local", "seat-names.txt")
            env = {
                "HOME": user_home,
                "PATH": os.environ.get("PATH", ""),
                "TMPDIR": tmp,
                "XDG_CONFIG_HOME": xdg,
                "HELM_HOME": home,
                "HELM_ADOPTED_DIR": adopted,
                self._REPORT_VAR: report,
            }
            if inherited:
                env[inherited] = parent
            if explicit:
                env[self._LOCAL_OVERRIDE_VAR] = local
            argv = [
                sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-t", ".", "-p", "test_seats.py", "-k",
                "test_join_writes_roster_and_baselines_cursor_at_join",
            ]
            out = subprocess.run(
                argv, capture_output=True, text=True, cwd=REPO, env=env,
                timeout=300)
            self.assertEqual(
                out.returncode, 0,
                "the spawned canonical runner exited %s under %r:\n%s"
                % (out.returncode, argv, out.stderr[-500:]))
            with open(report, encoding="utf-8") as fh:
                observed = json.load(fh)
            with open(parent, "rb") as fh:
                self.assertEqual(fh.read(), sentinel)
            after_stat = os.stat(parent)
            self.assertEqual((after_stat.st_dev, after_stat.st_ino),
                             (parent_stat.st_dev, parent_stat.st_ino))
            self.assertIsNone(observed["authority_error"])
            self.assertIsNone(observed["authority_invalid"])
            self.assertFalse(observed["authority_existed_before_join"],
                             "the suite plant must preserve ABSENT semantics")
            self.assertNotEqual(observed["authority"], parent)
            self.assertEqual(observed["authority_names"], ["alice"])
            self.assertEqual(observed["seat"], "alice")
            self.assertEqual(observed["roster_row"]["session"], "sess-1234")
            self.assertEqual(observed["roster_row"]["project"], "projx")
            self.assertIn("early word", observed["delivered"])
            if explicit:
                self.assertEqual(observed["authority"], local)
                self.assertEqual(observed["helm_seat_names"], local)
                self.assertTrue(os.path.isfile(local))
            else:
                self.assertEqual(observed["helm_seat_names"],
                                 observed["authority"])
            self.assertIsNone(observed["meld_seat_names"])

    def test_absent_modern_and_legacy_parent_targets_all_project_into_the_suite_authority(self):  # noqa: VACUOUS_ASSERTION — every variant performs a real join, roster read, delivery, and authority parse before proving the parent bytes and inode unchanged
        for inherited in (None, "HELM_SEAT_NAMES", "MELD_SEAT_NAMES"):
            with self.subTest(inherited=inherited):
                self._run_variant(inherited=inherited)

    def test_an_explicit_test_local_override_after_bootstrap_still_wins(self):  # noqa: VACUOUS_ASSERTION — the helper asserts the local authority gained alice through the real join while the inherited parent file stayed unchanged
        self._run_variant(inherited="HELM_SEAT_NAMES", explicit=True)


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
    operator's deliberate legacy choice would be silently overridden by a
    plant whose entire contract is to defer to deliberate choices. Note the
    companion differs per var: CHAT_DIR and HOME are MELD_, CONFIG_ROOTS is
    the predecessor's spelling, and only where the helm home's local names
    declare a predecessor (helm/configs/_common.py)."""

    def test_meld_chat_dir_is_honored(self):
        got = _probe(MELD_CHAT_DIR="/tmp/legacy-chat")
        self.assertEqual(got["chat_dir"], "/tmp/legacy-chat")

    def test_meld_home_is_honored(self):
        got = _probe(MELD_HOME="/tmp/legacy-home")
        self.assertEqual(got["helm_home"], "/tmp/legacy-home")

    def test_a_declared_predecessors_config_roots_is_honored(self):
        with tempfile.TemporaryDirectory() as home_dir:
            os.makedirs(os.path.join(home_dir, "_global"))
            with open(os.path.join(home_dir, "_global", "local-names.json"),
                      "w") as f:
                json.dump({"predecessor": "oldtool"}, f)
            got = _probe(HELM_HOME=home_dir,
                         OLDTOOL_CONFIG_ROOTS="/tmp/legacy-roots")
        self.assertEqual(got["cwd_roots"], ["/tmp/legacy-roots"])

    def test_an_undeclared_predecessor_spelling_is_nobodys_choice(self):
        """The planted home declares no predecessor, so production would not
        read this spelling, and the plant must not defer to it either: that
        deference would leave the roots on their live default."""
        got = _probe(OLDTOOL_CONFIG_ROOTS="/tmp/legacy-roots")
        self.assertNotEqual(got["cwd_roots"], ["/tmp/legacy-roots"])
        self.assertEqual(got["cwd_roots"], [got["HELM_CONFIG_ROOTS"]])

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
