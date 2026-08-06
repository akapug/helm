#!/usr/bin/env python3
"""The eval's arms, pinned — and the refusal that makes a green mean something.

0.3's flagship deliverable is the cc-codex vs pi-codex evidence, and §E-5 names
the condition it lives or dies on: same model, same tier, same suite, "without
this pin the numbers are cross-harness noise."

MEASURED 2026-07-29: THE ARMS WERE NOT PINNED AND NOTHING WOULD HAVE SAID SO.
`cc-codex` reaches codex through helm's CLIProxyAPI on the owner's subscription
OAuth; pi had one provider configured — openrouter. Run as specified, the eval
would have compared Claude-Code-over-subscription against pi-over-OpenRouter
and credited the entire difference to the HARNESS. The confound §E-5 forbids,
living inside the design that forbids it, producing numbers that look exactly
like an answer.

SO THIS SUITE IS MOSTLY ABOUT THE INSTRUMENT BEING ABLE TO FAIL. A probe that
returns the same answer regardless of the truth is not a measurement, however
confidently it renders. Every mismatch class is planted here and asserted to
report NOT PINNED, because the value of the eventual green is exactly the
reachability of the red.

The positive control is live-verified rather than only mocked: on this host,
after `helm pi extension --seat codex --apply`, both arms read
http://127.0.0.1:8317/v1 on gpt-5.6-sol, fingerprint a8f23bb16b118d57.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import evalpin, evalrun, pi  # noqa: E402


class ArmsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-eval-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def plant(self, port=8317, model="gpt-5.6-sol"):
        p = os.path.join(self.d, "helm-codex.ts")
        with open(p, "w", encoding="utf-8") as f:
            f.write(pi.extension_source("codex", port, model))
        return p

    def _cc(self, endpoint="http://127.0.0.1:8317/v1", model="gpt-5.6-sol"):
        return mock.patch.object(
            evalpin, "_cc_arm",
            return_value=({"arm": evalpin.CC_ARM, "seat": "codex",
                           "harness": "claude-code", "endpoint": endpoint,
                           "model": model, "alias": model}, None))

    def test_matched_arms_are_PINNED(self):
        with self._cc():
            r, err = evalpin.arms("codex", pi_path=self.plant())
        self.assertIsNone(err)
        self.assertTrue(r["pinned"], r["mismatches"])
        self.assertTrue(r["fingerprint"])

    def test_a_DIFFERENT_ENDPOINT_is_not_pinned(self):
        """The live shape of the failure: pi on its own provider (openrouter)
        while cc-codex is on the seat's proxy. A latency or reliability
        difference would be the PROVIDER's, credited to the harness."""
        with self._cc():
            r, _ = evalpin.arms("codex", pi_path=self.plant(port=9999))
        self.assertFalse(r["pinned"])
        self.assertEqual([m["field"] for m in r["mismatches"]], ["endpoint"])

    def test_a_DIFFERENT_MODEL_is_not_pinned(self):
        with self._cc():
            r, _ = evalpin.arms("codex", pi_path=self.plant(model="gpt-4o-mini"))
        self.assertFalse(r["pinned"])
        self.assertEqual([m["field"] for m in r["mismatches"]], ["model"])

    def test_BOTH_mismatches_are_reported_not_just_the_first(self):
        """An operator fixing one and re-running should not discover the second
        only then — a check that reports one problem at a time turns a single
        misconfiguration into a sequence of them."""
        with self._cc():
            r, _ = evalpin.arms("codex",
                                pi_path=self.plant(port=9999, model="other"))
        self.assertEqual(sorted(m["field"] for m in r["mismatches"]),
                         ["endpoint", "model"])

    def test_a_MISSING_pi_extension_says_what_it_would_measure_instead(self):
        """The default state of any machine. The message has to name the
        CONSEQUENCE, not just the absence, or it reads as a setup nag rather
        than as an invalidated experiment."""
        with self._cc():
            r, _ = evalpin.arms("codex", pi_path=os.path.join(self.d, "none.ts"))
        self.assertFalse(r["pinned"])
        why = r["mismatches"][0]["why"]
        self.assertIn("PROVIDER, not the harness", why)
        self.assertIn("helm pi extension", why)

    def test_an_extension_declaring_nothing_comparable_is_refused(self):
        p = os.path.join(self.d, "helm-codex.ts")
        with open(p, "w", encoding="utf-8") as f:
            f.write("export default function () {}\n")
        with self._cc():
            r, _ = evalpin.arms("codex", pi_path=p)
        self.assertFalse(r["pinned"])
        self.assertIn("no baseUrl/model id", r["mismatches"][0]["why"])

    def test_the_pi_arm_is_read_from_the_INSTALLED_FILE(self):
        """Not from what the generator WOULD produce. A generator and an
        installed artifact that have drifted are exactly the state this exists
        to catch, and asking the generator would agree with itself every
        time."""
        p = self.plant(port=7777)
        facts, err = evalpin._pi_arm("codex", path=p)
        self.assertIsNone(err)
        self.assertIn("7777", facts["endpoint"])
        self.assertEqual(facts["source"], p)


class FingerprintTest(unittest.TestCase):
    def _r(self, endpoint, model):
        return {"arms": {"cc-codex": {"endpoint": endpoint, "model": model},
                         "pi-codex": {"endpoint": endpoint, "model": model}}}

    def test_the_same_arms_fingerprint_the_same(self):
        self.assertEqual(evalpin.fingerprint(self._r("u", "m")),
                         evalpin.fingerprint(self._r("u", "m")))

    def test_DIFFERENT_arms_fingerprint_differently(self):
        """This is what stops a pre-registration being inherited by a run
        against arms it was never registered for — the whole point of stamping
        it."""
        self.assertNotEqual(evalpin.fingerprint(self._r("u", "m")),
                            evalpin.fingerprint(self._r("u2", "m")))
        self.assertNotEqual(evalpin.fingerprint(self._r("u", "m")),
                            evalpin.fingerprint(self._r("u", "m2")))


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-reg-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_it_REFUSES_unpinned_arms_and_writes_nothing(self):
        """Pre-registering a decision rule against incomparable arms records a
        decision procedure for an experiment that cannot be run — and leaves a
        file that later reads as due diligence."""
        out = os.path.join(self.d, "prereg.json")
        path, err = evalpin.register({"pinned": False, "mismatches": []}, out=out)
        self.assertIsNone(path)
        self.assertIn("NOT pinned", err)
        self.assertFalse(os.path.exists(out))

    def test_it_stamps_the_fingerprint_it_was_registered_FOR(self):
        out = os.path.join(self.d, "prereg.json")
        r = {"pinned": True, "fingerprint": "abc123",
             "arms": {"cc-codex": {"endpoint": "u", "model": "m"}}}
        path, err = evalpin.register(r, out=out)
        self.assertIsNone(err)
        body = json.loads(open(path, encoding="utf-8").read())
        self.assertEqual(body["registered_for_fingerprint"], "abc123")

    def test_the_rule_names_the_PRIMARY_metric_and_forbids_self_grading(self):
        """§C.5 and §C.1. Both are the kind of thing that quietly goes missing
        between a plan and a run, and neither can be added afterwards without
        being a different experiment."""
        rule = evalpin.DECISION_RULE
        self.assertIn("silent-drop", rule["primary_metric"])
        self.assertIn("no arm grades itself", rule["grading"])
        self.assertTrue(any("silent_drop_rate" in c for c in rule["adopt_pi_iff"]))


class CmdTest(unittest.TestCase):
    def test_arms_exits_NONZERO_when_not_pinned(self):
        """The exit code is the part a script reads. A check that always exits
        0 is decoration."""
        with mock.patch.object(evalpin, "arms",
                               return_value=({"pinned": False, "seat": "codex",
                                              "arms": {}, "mismatches":
                                              [{"field": "x", "why": "y"}]}, None)):
            self.assertEqual(evalpin.cmd_eval(["arms"]), 1)

    def test_arms_exits_zero_only_when_pinned_AND_RUNNABLE(self):
        """Contract widened 2026-07-29: pinned alone used to earn a 0. It does
        not any more, because a caller reading this exit code is asking whether
        the eval can RUN, and an arm that is configured identically but cannot
        authenticate answers that with a green it has not earned."""
        with mock.patch.object(evalpin, "arms",
                               return_value=({"pinned": True, "runnable": True,
                                              "seat": "codex", "arms": {},
                                              "mismatches": [], "run_blockers": [],
                                              "fingerprint": "f"}, None)):
            self.assertEqual(evalpin.cmd_eval(["arms"]), 0)

    def test_an_unknown_flag_is_named(self):
        self.assertNotEqual(evalpin.cmd_eval(["arms", "--jsn"]), 0)

    def test_a_bare_verb_prints_usage_and_refuses(self):
        self.assertEqual(evalpin.cmd_eval([]), 2)

    def test_help_exits_zero(self):
        self.assertEqual(evalpin.cmd_eval(["--help"]), 0)



class RunnableTest(unittest.TestCase):
    """PINNED and RUNNABLE are different claims, and conflating them was an
    over-claim in this module's own first commit.

    `arms()` compares DECLARED CONFIGURATION — both sides naming one endpoint
    and one model. Necessary, not sufficient. Measured three hours after that
    commit landed: the generated pi extension references $HELM_PI_PROXY_KEY and
    NOTHING IN HELM SETS IT, so the arm was configured identically and could
    not make a single request. `helm eval arms` said PINNED, the integrator
    told the room PINNED, and a reasonable reader hears "ready to run".
    """

    def _arm(self, harness="pi", endpoint="http://127.0.0.1:1/v1"):
        return {"harness": harness, "endpoint": endpoint}

    def test_a_dead_endpoint_is_not_reachable(self):
        ok, why = evalpin.reachable(self._arm(endpoint="http://127.0.0.1:1/v1"))
        self.assertFalse(ok)
        self.assertIn("refused the connection", why)

    def test_a_pi_arm_with_NO_KEY_is_not_runnable_even_when_the_port_answers(self):
        """The live case. The proxy answers, the config is identical, and pi
        cannot authenticate — configured is not drivable."""
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(evalpin.KEY_ENV_NAME, None)
                ok, why = evalpin.reachable(
                    self._arm(endpoint="http://127.0.0.1:%d/v1" % port))
            self.assertFalse(ok)
            self.assertIn("is UNSET", why)
            self.assertIn("configured, not runnable", why)
        finally:
            srv.close()

    def test_a_cc_arm_needs_no_pi_key(self):
        """The key is pi's problem alone — demanding it of the claude-code arm
        would invent a blocker that does not exist."""
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(evalpin.KEY_ENV_NAME, None)
                ok, _ = evalpin.reachable(
                    self._arm(harness="claude-code",
                              endpoint="http://127.0.0.1:%d/v1" % port))
            self.assertTrue(ok)
        finally:
            srv.close()

    def test_arms_ITSELF_reports_pinned_but_not_runnable(self):
        """THE INTEGRATION, added because a mutation did not bite. Collapsing
        `runnable = pinned and not blockers` into `runnable = pinned` passed
        all 22 tests: reachable() was tested alone and cmd_eval was tested
        against a MOCKED report, so nothing exercised arms() wiring the two
        together — the exact seam where the over-claim lived.

        Here the arms are configured identically (PINNED) and the endpoint is
        dead, so the honest answer is pinned-and-not-runnable with the blocker
        named."""
        import tempfile as _tf
        d = _tf.mkdtemp(prefix="helm-test-int-")
        try:
            p = os.path.join(d, "helm-codex.ts")
            with open(p, "w", encoding="utf-8") as f:
                f.write(pi.extension_source("codex", 1, "gpt-5.6-sol"))
            with mock.patch.object(
                    evalpin, "_cc_arm",
                    return_value=({"arm": evalpin.CC_ARM, "seat": "codex",
                                   "harness": "claude-code",
                                   "endpoint": "http://127.0.0.1:1/v1",
                                   "model": "gpt-5.6-sol"}, None)):
                r, err = evalpin.arms("codex", pi_path=p)
            self.assertIsNone(err)
            self.assertTrue(r["pinned"], "identical config IS pinned")
            self.assertFalse(r["runnable"], "a dead endpoint is not drivable")
            self.assertTrue(r["run_blockers"])
            self.assertTrue(all(a.get("reachable") is False
                                for a in r["arms"].values()),
                            "every arm carries its own reachability verdict")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_probe_never_needs_the_credential_it_checks_for(self):
        """A TCP connect and an env PRESENCE test. It must not read, print or
        transmit the token — a checker that needs the secret to check for the
        secret is a new place for the secret to leak."""
        import inspect
        src = inspect.getsource(evalpin.reachable)
        self.assertNotIn("os.environ.get(KEY_ENV_NAME)[", src)
        self.assertNotIn("print", src)
        self.assertIn("create_connection", src)

    def test_cmd_exits_NONZERO_when_pinned_but_NOT_runnable(self):
        """The exit code is what a script reads, and the question it asks is
        'can the eval run'. Answering with the narrower pin hands a green to a
        caller about to drive an arm that cannot authenticate."""
        with mock.patch.object(evalpin, "arms",
                               return_value=({"pinned": True, "runnable": False,
                                              "seat": "codex", "arms": {},
                                              "mismatches": [], "fingerprint": "f",
                                              "run_blockers": ["pi: no key"]}, None)):
            self.assertEqual(evalpin.cmd_eval(["arms"]), 1)

# ─── the run rig (helm/evalrun.py) — the 2026-07-29 pilot's five defects ────
#
# The pilot published ZERO numbers on purpose; its yield was five ways this
# rig would have manufactured false numbers at batch scale. Every class below
# plants one defect's counterfactual and proves the rig refuses it — and the
# matching positive control, because a rig that only says no measures nothing.


class RunDirTest(unittest.TestCase):
    """FINDING 3's first half: the -64s elapsed came from ONE path pair reused
    across a failed firing and its re-fire. Per-run directories make that
    cross-read structurally impossible — if the mint refuses reuse."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-rundir-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_two_runs_get_two_disjoint_dirs_named_by_their_ids(self):
        a, err = evalrun.run_dir("run-1", root=self.d)
        self.assertIsNone(err)
        b, err = evalrun.run_dir("run-2", root=self.d)
        self.assertIsNone(err)
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.basename(a), "run-1")
        self.assertEqual(os.path.basename(b), "run-2")
        # zero overlap: an artifact written in one is invisible in the other
        with open(os.path.join(a, "cc-arm.out"), "w") as f:
            f.write("run-1's artifact")
        self.assertEqual(os.listdir(b), [])

    def test_a_REUSED_run_id_is_refused_and_the_original_untouched(self):
        """The counterfactual of the pilot's bug: attempt 2 must not be able
        to land in attempt 1's directory at all."""
        a, _ = evalrun.run_dir("run-1", root=self.d)
        with open(os.path.join(a, "cc-arm.end"), "w") as f:
            f.write("stale stamp from attempt 1")
        again, err = evalrun.run_dir("run-1", root=self.d)
        self.assertIsNone(again)
        self.assertIn("REFUSING", err)
        self.assertIn("FINDING 3", err)
        with open(os.path.join(a, "cc-arm.end")) as f:
            self.assertEqual(f.read(), "stale stamp from attempt 1")

    def test_an_id_that_cannot_be_a_directory_name_is_refused(self):
        for bad in ("", "../escape", "a/b", ".hidden"):
            path, err = evalrun.run_dir(bad, root=self.d)
            self.assertIsNone(path, bad)
            self.assertIn("cannot name a directory", err)


class ArmConfigTest(unittest.TestCase):
    """FINDING 5: a headless arm inherited the seat's SessionStart onboarding,
    obeyed it, armed `helm chat wait --follow`, and never returned — a harness
    verdict manufactured entirely by the rig. The arm's config is a per-run
    hook-free COPY; the seat's real config is never written."""

    SETTINGS = {"permissions": {"defaultMode": "bypassPermissions"},
                "theme": "dark",
                "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                       "command": "helm chat join"}]}],
                          "UserPromptSubmit": [{"hooks": [{"type": "command",
                                                           "command": "helm inject"}]}],
                          "Stop": [{"hooks": [{"type": "command",
                                               "command": "helm chat stop-guard"}]}]}}

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-armcfg-")
        self.seat_claude = os.path.join(self.d, "seat", "claude")
        os.makedirs(os.path.join(self.seat_claude, "projects"))
        with open(os.path.join(self.seat_claude, "projects", "t.jsonl"), "w") as f:
            f.write("fleet transcript — must not ride into an arm\n")
        with open(os.path.join(self.seat_claude, "settings.json"), "w") as f:
            f.write(json.dumps(self.SETTINGS, indent=2) + "\n")
        with open(os.path.join(self.seat_claude, "stats-cache.json"), "w") as f:
            f.write("{}\n")
        self.rdir = os.path.join(self.d, "run-1")
        os.makedirs(self.rdir)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_generated_arm_config_carries_NO_hooks(self):
        """Asserted on FILE CONTENT, not on the absence of a complaint — the
        stripped settings.json is re-read and parsed."""
        cfg, err = evalrun.arm_config(self.seat_claude, self.rdir)
        self.assertIsNone(err)
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as f:
            body = json.load(f)
        self.assertNotIn("hooks", body)
        # identity/settings survive — that is what keeps the arms comparable
        self.assertEqual(body["theme"], "dark")
        self.assertEqual(body["permissions"], {"defaultMode": "bypassPermissions"})

    def test_the_seats_REAL_settings_are_byte_identical_after(self):
        src = os.path.join(self.seat_claude, "settings.json")
        with open(src, "rb") as f:
            before = f.read()
        _, err = evalrun.arm_config(self.seat_claude, self.rdir)
        self.assertIsNone(err)
        with open(src, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_state_dirs_do_not_ride_into_the_arm(self):
        cfg, _ = evalrun.arm_config(self.seat_claude, self.rdir)
        self.assertFalse(os.path.exists(os.path.join(cfg, "projects")),
                         "fleet transcripts inside a measurement arm are the "
                         "contamination the strip exists to remove")
        self.assertTrue(os.path.exists(os.path.join(cfg, "stats-cache.json")),
                        "plain top-level files DO ride")

    def test_the_strip_leaves_evidence_naming_every_removed_event(self):
        evalrun.arm_config(self.seat_claude, self.rdir)
        with open(os.path.join(self.rdir, "hooks-stripped.json"),
                  encoding="utf-8") as f:
            ev = json.load(f)
        self.assertEqual(ev["stripped"]["settings.json"],
                         ["SessionStart", "Stop", "UserPromptSubmit"])
        self.assertEqual(ev["source"], self.seat_claude)

    def test_unreadable_settings_REFUSE_rather_than_copy_unknown_hooks(self):
        """An arm whose hooks could not be proven absent is FINDING 5 with
        extra steps."""
        with open(os.path.join(self.seat_claude, "settings.json"), "w") as f:
            f.write("not json{")
        cfg, err = evalrun.arm_config(self.seat_claude, self.rdir)
        self.assertIsNone(cfg)
        self.assertIn("hook-free", err)


class ArmCommandTest(unittest.TestCase):
    """FINDING 1: the pilot's first cc firing was hand-assembled from readable
    env and died in 1s ("Not logged in") — the bearer lives INSIDE launch.sh.
    The only arm that cannot drift from what seats actually run is the seat's
    own launch.sh, shelled with -p."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-armcmd-")
        self._home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.d
        self.task = os.path.join(self.d, "TASK.md")
        with open(self.task, "w") as f:
            f.write("# the seeded task text\n")

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        shutil.rmtree(self.d, ignore_errors=True)

    def plant_launch(self, text=None):
        from helm import seat as seatmod
        d = seatmod.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "launch.sh")
        with open(p, "w") as f:
            f.write(text if text is not None else
                    '#!/bin/sh\n# hermetic fixture\n'
                    'exec env CLAUDE_CONFIG_DIR="${HELM_EVAL_CONFIG_DIR:-'
                    '/seat/claude}" true "$@"\n')
        os.chmod(p, 0o700)
        return p

    def test_the_command_is_EXACTLY_launch_sh_dash_p_prompt(self):
        launch = self.plant_launch()
        cmd, err = evalrun.arm_command("codex", self.task, "/run/cfg")
        self.assertIsNone(err)
        self.assertEqual(cmd["argv"],
                         [launch, "-p", "# the seeded task text\n"],
                         "anything more IS a hand-assembled arm")
        self.assertEqual(cmd["launch_sh"], launch)

    def test_the_ONLY_env_the_rig_adds_is_the_config_override(self):
        self.plant_launch()
        cmd, err = evalrun.arm_command("codex", self.task, "/run/cfg")
        self.assertIsNone(err)
        self.assertEqual(cmd["env"][evalrun.CONFIG_OVERRIDE_ENV], "/run/cfg")
        base = dict(os.environ)
        base[evalrun.CONFIG_OVERRIDE_ENV] = "/run/cfg"
        self.assertEqual(cmd["env"], base)
        self.assertNotIn(evalrun.CONFIG_OVERRIDE_ENV, os.environ,
                         "the override rides the CHILD env, never the rig's")

    def test_a_missing_launch_sh_refuses_and_names_the_mint(self):
        cmd, err = evalrun.arm_command("codex", self.task, "/run/cfg")
        self.assertIsNone(cmd)
        self.assertIn("helm seat launch", err)
        self.assertIn("FINDING 1", err)

    def test_a_pre_seam_launch_sh_is_REFUSED_not_silently_hooked(self):
        """A launch.sh minted before the override seam would hand the arm the
        seat's REAL config dir — fleet hooks included — and nothing downstream
        could tell. The refusal names the re-mint."""
        self.plant_launch("#!/bin/sh\nexec env CLAUDE_CONFIG_DIR=/seat/claude "
                          'true "$@"\n')
        cmd, err = evalrun.arm_command("codex", self.task, "/run/cfg")
        self.assertIsNone(cmd)
        self.assertIn("FINDING 5", err)
        self.assertIn("helm seat launch codex", err)

    def test_an_unknown_seat_is_refused_with_the_family_list(self):
        _, err = evalrun.arm_command("nonesuch", self.task, "/run/cfg")
        self.assertIn("unknown seat", err)

    def test_END_TO_END_the_arm_rides_the_stripped_config_and_devnull_stdin(self):
        """The integration: run_arm through a fixture launch.sh that reports
        what it was actually given. Proves the composition — real launch path
        (F1), stripped config dir reaching CLAUDE_CONFIG_DIR (F5), stdin from
        /dev/null (F5's 3s stall), wrapper-owned stamps landing a recorded
        positive-elapsed row (F3/F4)."""
        from helm import seat as seatmod
        self.plant_launch(
            '#!/bin/sh\n'
            'echo "TASK:$2"\n'
            'echo "CFG:${HELM_EVAL_CONFIG_DIR:-unset}"\n'
            'if read -r _line; then echo "STDIN:open"; else echo "STDIN:closed"; fi\n'
            'exit 0\n')
        seat_claude = os.path.join(seatmod.seat_dir("codex"), "claude")
        os.makedirs(seat_claude)
        with open(os.path.join(seat_claude, "settings.json"), "w") as f:
            f.write(json.dumps({"hooks": {"SessionStart": []}, "theme": "d"}))
        rdir = os.path.join(self.d, "runs", "r1")
        os.makedirs(rdir)
        row, err = evalrun.run_arm("codex", self.task, rdir)
        self.assertIsNone(err)
        self.assertEqual(row["rc"], 0)
        self.assertGreater(row["elapsed"], 0)
        with open(row["out"], encoding="utf-8") as f:
            out = f.read()
        self.assertIn("TASK:# the seeded task text", out)
        self.assertIn("CFG:%s" % os.path.join(rdir, "arm-config"), out)
        self.assertIn("STDIN:closed", out)
        # and the row really landed in THIS run's results.jsonl
        with open(os.path.join(rdir, "results.jsonl"), encoding="utf-8") as f:
            (recorded,) = [json.loads(ln) for ln in f]
        self.assertEqual(recorded["arm"], "cc-codex")
        self.assertEqual(recorded["run"], "r1")


class RecordResultTest(unittest.TestCase):
    """FINDING 3's second half: the instrument printed -64s and was honest
    only by luck of sign — minutes later the same stale-stamp defect reads as
    a plausible FAST SUCCESS. elapsed <= 0 is refused by name, never filed."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-record-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _row(self, **kw):
        row = {"run": "r1", "arm": "cc-codex", "rc": 0,
               "started": 1785349411.0, "ended": 1785349485.0}
        row.update(kw)
        return row

    def test_the_pilots_own_negative_is_refused_naming_row_and_reason(self):
        """started/ended lifted from the pilot's actual crossed stamps
        (cc-arm.start=1785349411 vs stale cc-arm.end=1785349347: -64s)."""
        row, err = evalrun.record_result(
            self.d, self._row(started=1785349411.0, ended=1785349347.0))
        self.assertIsNone(row)
        self.assertIn("REFUSED result row r1/cc-codex", err)
        self.assertIn("elapsed -64.000s <= 0", err)
        self.assertFalse(os.path.exists(os.path.join(self.d, "results.jsonl")),
                         "a refused row leaves NOTHING on disk")

    def test_zero_elapsed_is_a_refusal_too(self):
        row, err = evalrun.record_result(
            self.d, self._row(started=100.0, ended=100.0))
        self.assertIsNone(row)
        self.assertIn("<= 0", err)

    def test_a_missing_stamp_is_refused_never_defaulted(self):
        for hole in ("started", "ended"):
            row, err = evalrun.record_result(self.d, self._row(**{hole: None}))
            self.assertIsNone(row, hole)
            self.assertIn("stamp missing", err)
            self.assertIn("r1/cc-codex", err)

    def test_a_positive_elapsed_records_and_reads_back(self):
        row, err = evalrun.record_result(self.d, self._row())
        self.assertIsNone(err)
        self.assertAlmostEqual(row["elapsed"], 74.0)
        with open(os.path.join(self.d, "results.jsonl"), encoding="utf-8") as f:
            (back,) = [json.loads(ln) for ln in f]
        self.assertEqual(back["arm"], "cc-codex")
        self.assertAlmostEqual(back["elapsed"], 74.0)


class PremiseSeedTest(unittest.TestCase):
    """FINDING 2: the pilot seeded the react-digest atom whose premise had
    been fixed EIGHT DAYS earlier (REACT_TAG, 18374bd, 2026-07-21). Both arms
    refuted it, so the run measured premise-checking and burned an arm run.
    The gate runs here, against current code, BEFORE any arm exists."""

    # the pilot's exact shape: the premise holds only while the fix's marker
    # is ABSENT from the tree.
    ATOM = {"id": "react-digest", "task": "# fix the reaction digest\n",
            "premise": {"path": "chat.py", "absent": "REACT_TAG"}}

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-seed-")
        self.repo = os.path.join(self.d, "repo")
        os.makedirs(self.repo)
        self.rdir = os.path.join(self.d, "run-1")
        os.makedirs(self.rdir)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _plant(self, code):
        with open(os.path.join(self.repo, "chat.py"), "w") as f:
            f.write(code)

    def test_a_refuted_premise_is_stale_and_skipped_with_the_evidence(self):
        self._plant('CHAT_TAG = "chat:b2b:"\n'
                    'REACT_TAG = "chat:react:b2b:"  # the 18374bd fix\n')
        report, err = evalrun.seed(self.ATOM, self.repo, self.rdir)
        self.assertIsNone(err)
        self.assertTrue(report["stale"])
        self.assertFalse(report["seeded"])
        self.assertIn("chat.py:2", report["evidence"],
                      "the evidence is the refuting file:line, not a shrug")
        with open(report["record"], encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["verdict"], "stale-and-skipped")
        self.assertIn("REACT_TAG", rec["evidence"])

    def test_a_refuted_premise_NEVER_seeds_a_task(self):
        self._plant('REACT_TAG = "chat:react:b2b:"\n')
        evalrun.seed(self.ATOM, self.repo, self.rdir)
        self.assertFalse(
            os.path.exists(os.path.join(self.rdir, "react-digest-TASK.md")),
            "a stale atom that still seeds burns an arm run measuring "
            "premise-checking — the pilot's exact waste")

    def test_a_live_premise_seeds_the_task_verbatim(self):
        self._plant('CHAT_TAG = "chat:b2b:"  # reactions share it — bug LIVE\n')
        report, err = evalrun.seed(self.ATOM, self.repo, self.rdir)
        self.assertIsNone(err)
        self.assertTrue(report["seeded"])
        self.assertFalse(report["stale"])
        with open(report["task_path"], encoding="utf-8") as f:
            self.assertEqual(f.read(), "# fix the reaction digest\n")
        self.assertFalse(
            os.path.exists(os.path.join(self.rdir, "react-digest.stale.json")))

    def test_present_mode_a_gone_construction_is_stale(self):
        """The other polarity: a premise that needs code to still EXIST."""
        self._plant("nothing relevant\n")
        atom = dict(self.ATOM,
                    premise={"path": "chat.py", "present": "shared_tag"})
        report, _ = evalrun.seed(atom, self.repo, self.rdir)
        self.assertTrue(report["stale"])
        self.assertIn("gone", report["evidence"])

    def test_an_atom_with_NO_premise_cannot_seed_at_all(self):
        """Unverifiable seeded anyway IS the pilot's defect — err, not a
        default-to-live."""
        report, err = evalrun.seed({"id": "x", "task": "t"}, self.repo,
                                   self.rdir)
        self.assertIsNone(report)
        self.assertIn("FINDING 2", err)

    def test_a_premise_naming_missing_code_is_stale_not_live(self):
        report, _ = evalrun.seed(self.ATOM, self.repo, self.rdir)
        self.assertTrue(report["stale"])
        self.assertIn("does not exist", report["evidence"])


class CmdRunSeedTest(unittest.TestCase):
    """The CLI leg, through evalpin's real dispatcher — exit codes are what a
    batch script reads."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-cmdrun-")
        self.repo = os.path.join(self.d, "repo")
        os.makedirs(self.repo)
        with open(os.path.join(self.repo, "chat.py"), "w") as f:
            f.write('REACT_TAG = "chat:react:b2b:"\n')
        self.atom = os.path.join(self.d, "atom.json")
        with open(self.atom, "w") as f:
            f.write(json.dumps(PremiseSeedTest.ATOM))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _seed(self, run_id="r1", extra=()):
        return evalpin.cmd_eval(["seed", "--atom", self.atom, "--repo",
                                 self.repo, "--run-id", run_id, "--root",
                                 os.path.join(self.d, "runs")] + list(extra))

    def test_a_stale_atom_exits_1_so_a_batch_script_sees_it(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._seed()
        self.assertEqual(rc, 1)
        self.assertIn("STALE-AND-SKIPPED", out.getvalue())

    def test_a_live_atom_seeds_and_exits_0(self):
        with open(os.path.join(self.repo, "chat.py"), "w") as f:
            f.write("no fix marker here\n")
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._seed(run_id="r2")
        self.assertEqual(rc, 0)
        self.assertIn("LIVE", out.getvalue())

    def test_a_reused_run_id_exits_1_before_any_arm_work(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self._seed(run_id="r3")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._seed(run_id="r3")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err.getvalue())

    def test_an_unknown_flag_is_named(self):
        self.assertEqual(evalpin.cmd_eval(["run", "--bogus"]), 2)

    def test_missing_required_flags_refuse_with_usage_not_a_traceback(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = evalpin.cmd_eval(["seed"])
        self.assertEqual(rc, 2)
        self.assertIn("--run-id", err.getvalue())


if __name__ == "__main__":
    unittest.main()
