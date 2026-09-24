"""roguescan — the local rogue-compute watchdog (task/1039).

Every arm below is RED without the change it pins and states its
counterfactual explicitly, because a test that passes both ways proves
nothing. The whole module is new, so every arm was red before the module
existed; the counterfactuals name the SPECIFIC regression each one refuses.
"""
import contextlib
import io
import json
import os
import signal
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import roguescan, seats_integrator  # noqa: E402

# the integrator asked for when none is configured, as the module names it
DEFAULT = seats_integrator.INTEGRATOR_SEAT_DEFAULT


def P(pid, ppid, exe, argv, start=1000, cpu=200.0, age=300.0, comm=""):
    return {"pid": pid, "ppid": ppid, "exe": exe, "argv": argv,
            "comm": comm, "starttime": start, "cpu_s": cpu, "age": age}


def S(procs, environs=None):
    return roguescan.Snapshot(procs, environs=environs if environs is not None
                              else {})


class ClassifyTest(unittest.TestCase):
    """Identity-first classification: the EXE decides what a process IS."""

    def test_pytest_sweep_classifies(self):
        # Counterfactual: a classifier that missed `-m pytest` leaves the
        # 9149-test sweep class (measured 2026-08-03, 3 suites/90min) unseen.
        self.assertEqual(
            roguescan.classify("/usr/bin/python3.14",
                               ["python3", "-m", "pytest", "tests/"]),
            "python -m pytest")

    def test_unittest_module_classifies(self):
        self.assertEqual(
            roguescan.classify("/usr/bin/python3",
                               ["python3", "-m", "unittest", "tests.test_x"]),
            "python -m unittest")

    def test_inline_code_never_classifies(self):
        # Counterfactual: flagging -c would bill every heredoc helper script.
        # MUST-HIT control first: the classifier is not just returning None.
        self.assertTrue(roguescan.classify(
            "/usr/bin/python3", ["python3", "-m", "pytest"]))
        self.assertIsNone(roguescan.classify(
            "/usr/bin/python3", ["python3", "-c", "print(1)"]))

    def test_direct_test_file_classifies(self):
        # task/286: `python3 tests/test_x.py` collects exactly like -m form.
        self.assertTrue(roguescan.classify(
            "/usr/bin/python3", ["python3", "tests/test_web.py"]))

    def test_helm_cli_is_not_classified(self):
        # THE false-positive control: helm web/chat run all day on this box
        # as python3 processes. Counterfactual: a looser python predicate
        # kills the fleet's own CLI.
        self.assertTrue(roguescan.classify(          # MUST-HIT control
            "/usr/bin/python3", ["python3", "tests/test_web.py"]))
        self.assertIsNone(roguescan.classify(
            "/usr/bin/python3",
            ["python3", "/home/owner/.local/bin/helm", "chat", "post", "hi"]))

    def test_fab_client_is_structurally_unflaggable(self):
        # THE CRUX ARM (exemption direction 1): a local fab client's argv
        # contains a forbidden suite spelling VERBATIM. Counterfactual: a
        # substring classifier over the command line flags — and after the
        # grace, KILLS — the very wrapper that offloads the work.
        # MUST-HIT control: the SAME spelling, actually EXECUTING as
        # python, classifies — identity is the only difference.
        self.assertEqual(roguescan.classify(
            "/usr/bin/python3", ["python3", "-m", "unittest", "tests"]),
            "python -m unittest")
        self.assertIsNone(roguescan.classify(
            "/usr/bin/bash",
            ["bash", "/home/owner/.local/bin/fab", "test", "--repo", ".",
             "--", "python3", "-m", "unittest", "tests"]))

    def test_ssh_carrying_remote_command_not_classified(self):
        self.assertTrue(roguescan.classify(          # MUST-HIT control
            "/usr/bin/python3", ["python3", "-m", "pytest", "tests/"]))
        self.assertIsNone(roguescan.classify(
            "/usr/bin/ssh",
            ["ssh", "snoozy", "cd ~/fab && python3 -m pytest tests/"]))

    def test_node_vitest_and_pnpm_install_classify(self):
        # The classes that came through the JS/TS hole on 2026-08-11
        # (load-49: vitest-pool-workers x1659 tests, 3 pnpm installs).
        self.assertEqual(roguescan.classify(
            "/usr/bin/node",
            ["node", "/gt/node_modules/.bin/vitest", "run"]), "node vitest")
        self.assertEqual(roguescan.classify(
            "/usr/bin/node",
            ["node", "/usr/local/bin/pnpm", "install", "--frozen-lockfile"]),
            "pnpm install")

    def test_network_commands_classify_safe_while_vitest_stays_killable(self):
        cases = (
            ["node", "/usr/local/bin/pnpm", "exec", "wrangler", "d1",
             "execute", "remote-db", "--remote"],
            ["node", "/usr/local/bin/pnpm", "exec", "wrangler", "deploy"],
            ["node", "/usr/local/bin/pnpm", "exec", "wrangler",
             "deployments", "list"],
            ["node", "/usr/local/bin/pnpm", "exec",
             "opennextjs-cloudflare", "build"],
        )
        for argv in cases:
            self.assertEqual(
                roguescan.classify("/usr/bin/node", argv),
                "network-bound " + argv[3],
                argv,
            )
        self.assertEqual(
            roguescan.classify(
                "/usr/bin/node",
                ["node", "/usr/local/bin/pnpm", "exec", "vitest", "run"],
            ),
            "pnpm exec",
        )
        self.assertEqual(
            roguescan.classify(
                "/usr/bin/node",
                ["node", "/gt/node_modules/.bin/vitest", "run",
                 "--reporter", "wrangler"],
            ),
            "node vitest",
        )

    def test_next_build_yes_next_dev_no(self):
        # Counterfactual: flagging `next dev` kills interactive owner usage;
        # only the build shape has bitten (OpenNext builds, task/1038).
        self.assertEqual(roguescan.classify(
            "/usr/bin/node", ["node", "/gt/node_modules/.bin/next", "build"]),
            "node next build")
        self.assertIsNone(roguescan.classify(
            "/usr/bin/node", ["node", "/gt/node_modules/.bin/next", "dev"]))

    def test_cargo_and_go_verbs(self):
        self.assertEqual(roguescan.classify(
            "/home/owner/.cargo/bin/cargo", ["cargo", "build", "--release"]),
            "cargo build")
        self.assertIsNone(roguescan.classify(
            "/home/owner/.cargo/bin/cargo", ["cargo", "metadata"]))
        self.assertEqual(roguescan.classify(
            "/usr/local/go/bin/go", ["go", "test", "./..."]), "go test")
        self.assertIsNone(roguescan.classify(
            "/usr/local/go/bin/go", ["go", "version"]))


class ScanTest(unittest.TestCase):
    """Floors, exemption (both directions), attribution, topmost-wins."""

    def test_rogue_suite_flagged_and_fab_helper_exempt_same_pass(self):
        # BOTH DIRECTIONS in one snapshot. Counterfactual for direction 2:
        # without the ancestor walk, fab's own local python helper is
        # flagged; without classification at all, the rogue goes unseen.
        procs = [
            P(1, 0, "/usr/lib/systemd/systemd", ["init"]),
            P(100, 1, "/usr/bin/bash", ["bash"]),
            P(200, 100, "/usr/bin/python3.14",
              ["python3", "-m", "pytest", "tests/"]),
            P(150, 1, "/usr/bin/bash",
              ["bash", "/home/owner/.rigger/infra-bin/fab-gate", "--repo", "."]),
            P(201, 150, "/usr/bin/python3.14",
              ["python3", "-m", "pytest", "tests/"]),
        ]
        res = roguescan.scan(S(procs), min_age=30, min_cpu=20)
        self.assertEqual([f["pid"] for f in res["flagged"]], [200])
        self.assertEqual([f["pid"] for f in res["exempt"]], [201])
        self.assertIn("under-fab", res["exempt"][0]["exempt"])

    def test_human_escape_env_exempts(self):
        # The PATH shims honor FAB_ALLOW_LOCAL_SUITE=1 as the human-only
        # escape; the watchdog must not kill what the shim blessed.
        procs = [P(100, 1, "/usr/bin/bash", ["bash"]),
                 P(200, 100, "/usr/bin/python3",
                   ["python3", "-m", "pytest", "tests/"])]
        res = roguescan.scan(
            S(procs, environs={100: {"FAB_ALLOW_LOCAL_SUITE": "1"}}),
            min_age=30, min_cpu=20)
        self.assertEqual(res["flagged"], [])
        self.assertIn("human-escape", res["exempt"][0]["exempt"])

    def test_attribution_walks_the_parent_chain(self):
        # Subagent shells often lack HELM_CHAT_NAME; the seat lives two
        # levels up. Counterfactual: leaf-only attribution says UNKNOWN and
        # the alert names nobody.
        procs = [P(398, 1, "/usr/bin/claude", ["claude"]),
                 P(399, 398, "/usr/bin/bash", ["bash"]),
                 P(400, 399, "/usr/bin/python3",
                   ["python3", "-m", "unittest", "tests.test_x"])]
        res = roguescan.scan(
            S(procs, environs={398: {"HELM_CHAT_NAME": "seat-a"}}),
            min_age=30, min_cpu=20)
        self.assertEqual(res["flagged"][0]["seat"], "seat-a")

    def test_unattributable_reads_unknown_never_guessed(self):
        procs = [P(400, 1, "/usr/bin/python3",
                   ["python3", "-m", "pytest", "tests/"])]
        res = roguescan.scan(S(procs), min_age=30, min_cpu=20)
        self.assertEqual(res["flagged"][0]["seat"], "UNKNOWN")

    def test_age_floor_spares_allowed_brief_runs(self):
        # A single-test-method local run (allowed by the shim) finishes in
        # seconds. Counterfactual: no floor -> every legitimate targeted run
        # is alerted on within its first pass.
        rogue = ["python3", "-m", "unittest", "pkg.mod.Case.test_x"]
        # MUST-HIT control: the identical process ABOVE the floor is flagged.
        old = roguescan.scan(
            S([P(200, 1, "/usr/bin/python3", rogue, age=300.0)]),
            min_age=30, min_cpu=20)
        self.assertEqual(len(old["flagged"]), 1)
        res = roguescan.scan(
            S([P(200, 1, "/usr/bin/python3", rogue, age=5.0)]),
            min_age=30, min_cpu=20)
        self.assertEqual(res["flagged"], [])
        self.assertEqual(res["exempt"], [])

    def test_subtree_cpu_counts_pool_workers(self):
        # vitest-pool-workers burns cpu in CHILDREN (one workerd per test
        # file) while the flagged parent idles. Counterfactual: parent-only
        # cpu reads 0.3% and the load-49 shape passes under the floor.
        procs = [P(500, 1, "/usr/bin/node",
                   ["node", "/gt/node_modules/.bin/vitest", "run"], cpu=1.0),
                 P(501, 500, "/usr/bin/workerd", ["workerd"], cpu=200.0),
                 P(502, 500, "/usr/bin/workerd", ["workerd"], cpu=200.0)]
        res = roguescan.scan(S(procs), min_age=30, min_cpu=20)
        self.assertEqual([f["pid"] for f in res["flagged"]], [500])

    def test_topmost_classified_process_carries_the_finding(self):
        # `pnpm run test` -> `node vitest` are ONE rogue action; killing the
        # child alone lets the manager respawn it.
        procs = [P(500, 1, "/usr/bin/node",
                   ["node", "/usr/local/bin/pnpm", "run", "test"]),
                 P(501, 500, "/usr/bin/node",
                   ["node", "/gt/node_modules/.bin/vitest", "run"])]
        res = roguescan.scan(S(procs), min_age=30, min_cpu=20)
        self.assertEqual([f["pid"] for f in res["flagged"]], [500])

    def test_network_bound_child_makes_its_manager_unkillable(self):
        procs = [P(500, 1, "/usr/bin/node",
                   ["node", "/usr/local/bin/pnpm", "run", "deploy"]),
                 P(501, 500, "/usr/bin/node",
                   ["node", "/gt/node_modules/.bin/wrangler", "deploy"])]
        res = roguescan.scan(S(procs), min_age=30, min_cpu=20)
        self.assertEqual(res["flagged"], [])
        self.assertEqual([f["pid"] for f in res["exempt"]], [500])
        self.assertEqual(res["exempt"][0]["exempt"], "network-bound")


class SafetyRegressionTest(unittest.TestCase):
    """Network work is never killable; compute must stay hot to arm."""

    def setUp(self):
        self.kills = []
        self.starts = {800: 8000, 900: 9000}
        state = os.path.join(roguescan._state_dir(), roguescan._STATE)
        if os.path.exists(state):
            os.unlink(state)
        for pid, start in self.starts.items():
            ev = os.path.join(
                roguescan._state_dir(), roguescan._EVID_DIR,
                "rogue-%d-%d.json" % (pid, start),
            )
            if os.path.exists(ev):
                os.unlink(ev)

    def _check(self, snap, now):
        return roguescan.check(
            snap, now=now, kill=True, grace=60, quiet=False,
            min_age=30, min_cpu=20,
            kill_fn=lambda pid, sig: self.kills.append((pid, sig)),
            start_of=lambda pid: self.starts.get(pid),
        )

    def test_wrangler_is_network_bound_alert_only_while_vitest_must_hit(self):
        wrangler = P(
            800, 1, "/usr/bin/node",
            ["node", "/usr/local/bin/pnpm", "exec", "wrangler", "d1",
             "execute", "remote-db", "--remote"],
            start=8000,
        )
        vitest = P(
            900, 1, "/usr/bin/node",
            ["node", "/usr/local/bin/pnpm", "exec", "vitest", "run"],
            start=9000,
        )
        scanned = roguescan.scan(S([wrangler, vitest]), min_age=30,
                                  min_cpu=20)
        self.assertEqual([f["pid"] for f in scanned["flagged"]], [900])
        self.assertEqual([f["pid"] for f in scanned["exempt"]], [800])
        self.assertEqual(scanned["exempt"][0]["exempt"], "network-bound")

        cool_wrangler = dict(wrangler, cpu_s=1.0)
        with mock.patch.object(roguescan, "_post") as post:
            res = self._check(S([wrangler]), now=10000.0)
            cool = self._check(S([cool_wrangler]), now=10030.0)
            again = self._check(S([wrangler]), now=10090.0)
        self.assertEqual(res["flagged"], [])  # noqa: VACUOUS_ASSERTION — scanned above proves the same list contains vitest
        self.assertEqual(cool["flagged"], [])
        self.assertEqual(again["flagged"], [])
        self.assertEqual(res["exempt"][0]["action"], "network-bound")
        self.assertEqual(self.kills, [])
        self.assertEqual(post.call_count, 1)
        line = post.call_args.args[0]
        self.assertIn("NETWORK-BOUND", line)
        self.assertIn("never kill", line)
        self.assertIn("wrangler d1 execute", line)
        with mock.patch.object(roguescan, "_post"):
            self._check(S([vitest]), now=10180.0)
            self._check(S([vitest]), now=10270.0)
            self._check(S([vitest]), now=10360.0)
        self.assertTrue(self.kills)
        self.assertIn((900, signal.SIGTERM), self.kills)

    def test_cpu_must_be_high_across_two_samples_before_kill_arms(self):  # noqa: VACUOUS_ASSERTION — SIGTERM below proves the recorder
        vitest = P(
            900, 1, "/usr/bin/node",
            ["node", "/usr/local/bin/pnpm", "exec", "vitest", "run"],
            start=9000,
        )
        snap = S([vitest])
        cool = S([P(
            900, 1, "/usr/bin/node",
            ["node", "/usr/local/bin/pnpm", "exec", "vitest", "run"],
            start=9000, cpu=1.0,
        )])
        with mock.patch.object(roguescan, "_post") as post:
            first = self._check(snap, now=10000.0)
            self.assertEqual(first["flagged"][0]["action"], "cpu-sampling")
            self.assertFalse(post.called)  # noqa: VACUOUS_ASSERTION — later call_count and SIGTERM are positive controls
            self.assertEqual(self._check(cool, now=10030.0)["flagged"], [])
            second = self._check(snap, now=10090.0)
            self.assertEqual(second["flagged"][0]["action"], "cpu-sampling")
            third = self._check(snap, now=10180.0)
            self.assertEqual(third["flagged"][0]["action"], "alerted")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(self.kills, [])
            fourth = self._check(snap, now=10270.0)
        self.assertEqual(fourth["flagged"][0]["action"], "killed")
        self.assertTrue(self.kills)
        self.assertIn((900, signal.SIGTERM), self.kills)

    def test_terminal_kill_rewrites_the_evidence_action_and_timestamp(self):
        vitest = P(
            900, 1, "/usr/bin/node",
            ["node", "/usr/local/bin/pnpm", "exec", "vitest", "run"],
            start=9000,
        )
        snap = S([vitest])
        with mock.patch.object(roguescan, "_post"):
            self._check(snap, now=10000.0)
            self._check(snap, now=10090.0)
            self._check(snap, now=10180.0)
        ev = os.path.join(
            roguescan._state_dir(), roguescan._EVID_DIR,
            "rogue-900-9000.json",
        )
        with open(ev) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["action"], "killed")
        self.assertEqual(rec["captured_at"], 10090.0)
        self.assertEqual(rec["action_at"], 10180.0)


class CheckTest(unittest.TestCase):
    """The grace->kill state machine, identity-bound at the moment of action."""

    def setUp(self):
        self.kills = []
        self.starts = {700: 5000}
        for p in (os.path.join(roguescan._state_dir(), roguescan._STATE),
                  os.path.join(roguescan._state_dir(), roguescan._EVID_DIR,
                               "rogue-700-5000.json")):
            if os.path.exists(p):
                os.unlink(p)

    def _snap(self):
        procs = [P(700, 1, "/usr/bin/python3",
                   ["python3", "-m", "pytest", "tests/"], start=5000),
                 P(701, 700, "/usr/bin/python3", ["python3", "worker"],
                   start=5001)]
        return S(procs)

    def _pass(self, now, **kw):
        kw.setdefault("kill", True)
        kw.setdefault("grace", 60)
        kw.setdefault("quiet", True)
        kw.setdefault("min_age", 30)
        kw.setdefault("min_cpu", 20)
        kw.setdefault("kill_fn", lambda pid, sig: self.kills.append((pid, sig)))
        kw.setdefault("start_of", lambda pid: self.starts.get(pid))
        return roguescan.check(self._snap(), now=now, **kw)

    def test_two_hot_samples_alert_and_capture_evidence_before_any_kill(self):
        first = self._pass(now=10000.0)
        self.assertEqual(first["flagged"][0]["action"], "cpu-sampling")
        res = self._pass(now=10030.0)
        self.assertEqual(res["flagged"][0]["action"], "alerted")
        self.assertEqual(self.kills, [])  # noqa: VACUOUS_ASSERTION — no-kill-yet IS the property; sibling arms receive kills on this same recorder
        ev = os.path.join(roguescan._state_dir(), roguescan._EVID_DIR,
                          "rogue-700-5000.json")
        self.assertTrue(os.path.exists(ev), "evidence file missing")
        with open(ev) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["seat"], "UNKNOWN")
        self.assertIn("pytest", " ".join(rec["argv"]))
        self.assertTrue(isinstance(rec["chain"], list))

    def test_grace_window_holds_then_kills_the_subtree(self):
        # Counterfactual: no grace -> a process the owner just blessed dies
        # before anyone can act on the alert; no subtree -> workerd pool
        # workers orphan and keep burning.
        self._pass(now=10000.0)
        res = self._pass(now=10030.0)
        self.assertEqual(res["flagged"][0]["action"], "alerted")
        res = self._pass(now=10060.0)
        self.assertEqual(res["flagged"][0]["action"], "grace-wait")
        self.assertEqual(self.kills, [])  # noqa: VACUOUS_ASSERTION — the hold-during-grace IS the property; the SIGTERM assertions below are the positive control on the same recorder
        res = self._pass(now=10090.0)
        self.assertEqual(res["flagged"][0]["action"], "killed")
        self.assertIn((700, signal.SIGTERM), self.kills)
        self.assertIn((701, signal.SIGTERM), self.kills)

    def test_survivor_gets_sigkill_next_pass(self):
        self._pass(now=10000.0)
        self._pass(now=10030.0)
        self._pass(now=10090.0)
        self.kills.clear()
        res = self._pass(now=10180.0)
        self.assertEqual(res["flagged"][0]["action"], "sigkilled")
        self.assertIn((700, signal.SIGKILL), self.kills)

    def test_recycled_pid_is_never_killed_on_a_stale_finding(self):
        # Store: measuring-later-only-shrinks-the-window — bind to IDENTITY
        # at the moment of action. Counterfactual: pid-only binding SIGTERMs
        # whatever innocent process inherited pid 700.
        self._pass(now=10000.0)
        self._pass(now=10030.0)
        self.starts[700] = 9999   # the live re-read disagrees: new process
        res = self._pass(now=10090.0)
        self.assertEqual(res["flagged"][0]["action"], "identity-changed")
        self.assertEqual(self.kills, [])  # noqa: VACUOUS_ASSERTION — not-killing the recycled pid IS the property; test_grace_window proves the same recorder fills when identity holds

    def test_alert_only_mode_never_kills(self):
        # HELM_ROGUE_KILL=0 must mean what it says.
        self._pass(now=10000.0, kill=False)
        self._pass(now=10030.0, kill=False)
        res = self._pass(now=10090.0, kill=False)
        self.assertEqual(res["flagged"][0]["action"], "alert-only")
        self.assertEqual(self.kills, [])  # noqa: VACUOUS_ASSERTION — alert-only not killing IS the property; test_grace_window fills this same recorder when kill=True

    def test_dry_run_is_genuinely_read_only(self):
        # Counterfactual: a dry pass that latches spends the alert budget and
        # suppresses the next REAL episode (the silent-drop dry-run lesson).
        res = self._pass(now=10000.0, dry=True)
        self.assertEqual(res["flagged"][0]["action"], "cpu-sampling")
        self.assertEqual(self.kills, [])  # noqa: VACUOUS_ASSERTION — dry writing/killing nothing IS the property; the non-dry siblings prove these same observables fill
        self.assertFalse(os.path.exists(
            os.path.join(roguescan._state_dir(), roguescan._STATE)))
        self.assertFalse(os.path.exists(
            os.path.join(roguescan._state_dir(), roguescan._EVID_DIR,
                         "rogue-700-5000.json")))


class AlertAddressesTheResolvedIntegratorTest(unittest.TestCase):
    """The rogue-compute alert names the integrator the roster resolves,
    never a seat name written into this module: a spelled name keeps being
    addressed after no seat carries it, and the alert then reaches nobody
    while it reads as sent."""

    KEYS = ("HELM_INTEGRATOR_SEAT", "MELD_INTEGRATOR_SEAT")
    F = {"seat": "seat-b", "label": "python3 -m pytest", "pid": 700,
         "age_s": 300, "cpu_pct": 200, "argv": ["python3", "-m", "pytest"]}

    def setUp(self):
        for k in self.KEYS:
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)

    def _alert(self, *roster):
        with mock.patch("helm.seats_roster.roster_checked",
                        return_value=({n: {"seat": n} for n in roster}, False)):
            return roguescan._alert(self.F, 60, True, "/tmp/evidence.json")

    def test_a_CONFIGURED_integrator_is_the_one_addressed(self):
        os.environ["HELM_INTEGRATOR_SEAT"] = "seat-lead"
        txt = self._alert("seat-lead", DEFAULT, "seat-b")
        self.assertTrue(txt.startswith("@seat-lead @seat-b ROGUE-COMPUTE"), txt)
        self.assertNotIn("@" + DEFAULT, txt)
        self.assertNotIn("UNROSTERED", txt)

    def test_the_DEFAULT_applies_when_none_is_configured(self):
        txt = self._alert(DEFAULT, "seat-b")
        self.assertTrue(txt.startswith("@%s @seat-b ROGUE-COMPUTE" % DEFAULT),
                        txt)

    def test_NOTHING_resolves_the_alert_still_goes_and_says_so(self):
        txt = self._alert("seat-b")
        # POSITIVE CONTROL: the whole alert is there, addressed to the seat
        self.assertTrue(txt.startswith("@seat-b ROGUE-COMPUTE on "), txt)
        self.assertIn("KILLING in 60s", txt)
        self.assertIn("[UNROSTERED: no integrator could be addressed", txt)
        self.assertNotIn("-integrator ", txt)


class WiringTest(unittest.TestCase):
    """A watchdog that exists but is not armed is the built-not-wired class."""

    def test_silent_drop_cadence_carries_the_rogue_pass(self):
        # THE ARMING ARM. The installed 90s timer runs `helm seat
        # silent-drop`; this proves that verb now carries the rogue pass.
        # Counterfactual: without the seat.py wiring this records nothing
        # and the watchdog is dead code on a shelf.
        from helm import seat
        calls = []
        orig = roguescan.cadence_pass
        roguescan.cadence_pass = lambda dry=False: calls.append(dry)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                seat.cmd_seat(["silent-drop", "--dry-run", "--quiet"])
        finally:
            roguescan.cadence_pass = orig
        self.assertEqual(calls, [True])

    def test_cli_verb_registered_with_usage(self):
        # Counterfactual: without the cli.py registration `helm rogue` is an
        # unknown verb (exit 2) and no operator can ever invoke it.
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["rogue", "--help"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("rogue [--dry-run]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
