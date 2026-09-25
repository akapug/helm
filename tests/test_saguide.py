#!/usr/bin/env python3
"""helm saguide tests — the SubagentStart initial-physics seam.

WHAT THESE ARMS ARE FOR. Every trap on this path produces a hook that RUNS,
exits 0, and delivers NOTHING, so an arm asserting "no exception" or "exit 0"
is guaranteed to pass against a totally broken seam. Each arm below therefore
asserts an OBSERVABLE — the exact bytes of the envelope, the exact event name,
the presence of a spec in SPECS — and every positive arm is paired with a
MUST-MISS: an input the code has to REJECT. An arm whose failure mode I cannot
name is measuring my intent, not the seam.

HERMETIC: no settings file is written, no hook is installed, no subagent is
spawned. The behavioural half (a real agent quoting physics back) is owned by
the verifier and cannot be faked here — see the module docstring in
helm/saguide.py for why a structural pass proves only that the hook ran.
"""
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
from tests._tmphome import home as _tmp_home  # noqa: E402
# HERMETIC HOME: payload() now reads the reflex store (nested-spawn steers),
# so an ambient home would put a live estate's reflexes into these arms.
_tmp_home(prefix="helm-test-saguide-", var="HELM_HOME")

from helm import saguide  # noqa: E402

# A SYNTHETIC HELM CWD, NEVER A DERIVED ONE. This used to be
# dirname(dirname(saguide.__file__)), which passed by hand in a lane worktree
# and FAILED ON THE FAB: the node checkout lives under /scratch/fab-hot/wt/...,
# project_for_cwd correctly resolves no registered helm scope there, payload()
# returned None, and four positive arms went red on a product that was working.
# The arm was measuring THE TEST HOST'S REGISTRY, not the seam.
#
# So the positives PATCH the scope reader and the must-misses keep exercising
# the real branch. Never weaken product scoping to make a copied checkout look
# registered — the scoping is the feature.
_HELM_CWD = "/synthetic/helm/checkout"


def _scoped(project_of=None):
    """Patch project_for_cwd so _HELM_CWD reads as helm and nothing else does.

    Patched on the LEDGER MODULE because saguide imports the function inside
    payload() at call time (`from .inject._ledger import project_for_cwd`),
    so the name is resolved through the module attribute on every call.
    """
    fn = project_of or (lambda cwd: "helm" if cwd == _HELM_CWD else None)
    return mock.patch("helm.inject._ledger.project_for_cwd", fn)


class SubagentStartEnvelope(unittest.TestCase):
    def test_event_name_is_minted_not_inherited(self):
        """THE ONE THAT CATCHES THE SILENT DROP. The harness discards a
        hookSpecificOutput whose hookEventName is not the firing event, without
        error and at exit 0, so borrowing inject's UserPromptSubmit envelope
        would install green and deliver nothing."""
        with _scoped():
            env, _ = saguide.payload(json.dumps({"cwd": _HELM_CWD}))
        self.assertIsNotNone(env)
        hso = env["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SubagentStart")
        # MUST-MISS: the value the naive implementation would emit.
        self.assertNotEqual(hso["hookEventName"], "UserPromptSubmit")

    def test_additional_context_carries_the_brief_verbatim(self):
        """Not 'contains something' — the SAME string `brief()` returns, so
        no caller can trim the block on its way to a subagent.

        THE PAYLOAD IS THE BRIEF, NOT THE CANONICAL BLOCK, and the split is
        the point: GUIDANCE was 2,940 characters of unconditional context spent
        before the subagent has read its own task, six of its nine bullets
        rationale rather than a rule that changes an act. The brief carries the
        behaviour-changing rules and the route to the rest. The next arm
        proves that route actually returns the long form."""
        # POSITIVE CONTROL FIRST: two empty strings compare equal, so the
        # equality below proves nothing until the block is known substantive.
        self.assertGreater(len(saguide.brief()), 200)
        self.assertIn("helm physics", saguide.brief())
        with _scoped():
            env, _ = saguide.payload(json.dumps({"cwd": _HELM_CWD}))
        self.assertIsNotNone(env, "control: a helm cwd must deliver")
        self.assertEqual(env["hookSpecificOutput"]["additionalContext"],
                         saguide.brief())
        self.assertEqual(saguide.brief(), saguide.BRIEF)
        self.assertEqual(saguide.guidance(), saguide.GUIDANCE)

    def test_the_brief_names_the_verb_that_returns_the_long_form(self):
        """A pointer is only a pointer if it resolves. The brief withholds the
        measurements; this pins that the command it names hands them over, and
        hands over MORE than the brief itself — a `--show` that printed the
        brief back would satisfy a naive "the verb runs" check."""
        self.assertIn("helm saguide --show", saguide.brief())
        self.assertGreater(len(saguide.guidance()), 2 * len(saguide.brief()))
        for fact in ("--focus --plan", "fab test --repo .", "tests.test_gate"):
            self.assertIn(fact, saguide.guidance())

    def test_guidance_ships_no_command_that_is_not_reachable_today(self):
        """THE CONTENT IS THE DELIVERABLE, and this arm exists because the first
        draft failed it. I copied the store's recommendation of
        `helm gate run --focus` — correct on paper, and MEASURED 2026-08-25 to
        be intercepted by the python3 shim into a full whole-suite gate, with a
        remote focused mint separately measured as unable to bind. Naming it
        would push an unreachable command into the first context of every
        subagent, which is the exact defect this seam was built after.

        This is a MUST-MISS by construction: it fails the moment someone
        re-adds the verb without landing the routing that makes it true."""
        g = saguide.guidance()
        # THE VERB'S ABSENCE IS NO LONGER PINNED, and the premise that pinned
        # it is dead. This arm used to assert `assertNotIn("gate run --focus")`
        # because the focused route was MEASURED unreachable. It is now
        # measured reachable: `--focus --plan` computes a selection locally in
        # seconds, reproduced across four lanes. Keeping the old assertion
        # would have forced the block to withhold the one command that answers
        # "how expensive is my cure round" before spending it — which is the
        # exact defect this seam exists to prevent, dressed as caution.
        #
        # What IS still pinned is that the block names the PLAN form (cheap,
        # local, no run) and never a bare focused gate as the cure route.
        self.assertIn("--focus --plan", g)
        # AND what it does for a lane that touches a non-Python file, because
        # the block ships the command: since task/3039 it selects the
        # tree-wide audits plus the modules naming the file, and the block
        # must say THAT, never that it refuses. Promising one answer when the
        # other is true is the same defect as naming an unreachable verb.
        self.assertIn("non-Python", g)
        self.assertNotIn("REFUSES if your lane touches any", g)
        # THE DISCRIMINATION IS THE CUSTOM ARGV, NOT THE VERB. This arm used
        # to assert `assertNotIn("fab gate --repo", g)` on the reasoning that
        # the custom-argv path mints UNKNOWN, so the verb "must be named only
        # as a prohibition". That CONFLATED two different commands.
        # `fab gate --repo DIR` with no `--` argv is the CANONICAL positive
        # route: helm chooses the interpreter, the receipt binds, and the
        # local guard PRINTS THIS EXACT ROUTE when it refuses a local run
        # (helm/gate.py names it; tests/test_gate_cap.py pins it in the
        # refusal text). Withholding it left the block naming only
        # prohibitions, which recreates the guesswork this seam exists to
        # prevent — measured: the block's own author, holding it in context,
        # invented `fab test -- python3 -m unittest` because no positive
        # command was stated, and spent two runs measuring the absence of a
        # cgroup scope.
        self.assertIn("fab gate --repo", g)
        # MUST-MISS: the FORBIDDEN shape is the verb PLUS a custom argv, and
        # it must never appear as an instruction.
        # (The bare `fab gate -- python3 -m unittest` string DOES appear in
        # the block — inside the PROHIBITION sentence, which is the point. So
        # the must-miss is the CANONICAL route with an argv welded on, the one
        # shape that would read as an instruction to hand-roll.)
        # The forbidden shape is the argv SEPARATOR (`-- <command>`), not any
        # flag that happens to start with two dashes. This pattern used to be
        # "fab gate --repo . --" and it matched `--focus`, which is a flag and
        # is the CURE-ROUND route the standing law requires — a must-miss that
        # rejects the correct command is worse than none.
        self.assertNotIn("fab gate --repo . -- ", g)
        self.assertIn("do not hand-roll", g.lower())
        # THE POSITIVE CURE-ROUND ROUTE IS NAMED, with the consumer rule and
        # the one thing it cannot read. A codex writer read the earlier text
        # ("fab test -- is not an exemption") as a prohibition on the focused
        # run every cure round in the fleet uses, and shipped a cure with its
        # tests NOT RUN; the guard admits the run, so the prose was the defect.
        self.assertIn("fab test --repo . -- python3 -m unittest <modules>", g)
        self.assertIn("git grep -ln", g)
        self.assertIn("tests.test_gate", g)
        self.assertNotIn("is\n  not an exemption", g)
        self.assertNotIn("not an exemption", g)

    def test_guidance_keeps_the_claims_that_are_true_regardless(self):
        """The tree/land/measure invariants hold whatever the routing does, so
        removing the unreachable verb must not have gutted the block."""
        g = saguide.guidance().lower()
        self.assertIn("binds a tree", g)
        self.assertIn("not land authority", g)
        # THE LOCAL-RUN SENTENCE STATES THE GUARD'S MEASURED BEHAVIOUR, not a
        # rationale that can go stale under it. A ruling on 2026-08-25 refined
        # WHY local runs are refused (discovery is unbounded; a named module is
        # not) — but the guard still refuses `python3 -m unittest` in EVERY
        # shape, verified by running the permitted form under nice and reading
        # the refusal. So the reachable instruction is "route it through the
        # fab" and the arm pins the OBSERVABLE, never the reason.
        self.assertIn("refused", g)
        self.assertIn("route it through the fab", g)
        # MUST-MISS: the absolute justification that the ruling superseded.
        self.assertNotIn("never run a suite on this box", g)

    def test_malformed_payload_fails_open_and_emits_nothing(self):  # noqa: VACUOUS_ASSERTION — the emptiness IS the contract (fail open), and the same-observable control is the good-payload assertion on the first line of the body: if payload() emitted nothing for everything, that control fails first
        """A garbled payload must inject nothing at rc 0. An SA with no physics
        is bad; an SA that cannot start is worse."""
        # POSITIVE CONTROL: a WELL-FORMED payload must emit. Without this the
        # whole loop passes against a payload() that returns None for
        # everything, which is a seam that delivers nothing to anyone.
        with _scoped():
            good, _ = saguide.payload(json.dumps({"cwd": _HELM_CWD}))
        self.assertIsNotNone(good, "control: a good payload must emit")
        for bad in ("", "not json", "[]", "null", '"a string"', "{"):
            env, project = saguide.payload(bad)
            self.assertIsNone(env, "payload(%r) must emit nothing" % bad)
            self.assertIsNone(project)

    def test_agent_labels_are_tolerated_and_never_read_as_identity(self):  # noqa: VACUOUS_ASSERTION — the leak checks run against a blob the two assertIn controls above them prove is non-empty and carries the physics, so an empty envelope cannot satisfy this test
        """helm identity is inherited from the env seam. Guessing a seat name
        out of a harness-supplied agent label is how a foreign row gets written
        under a real seat's name."""
        with _scoped():
            env, _ = saguide.payload(json.dumps({
                "cwd": _HELM_CWD, "agent_id": "abc123",
                "agent_type": "general-purpose", "unknown_future_key": 1}))
        self.assertIsNotNone(env)  # tolerated, not refused
        blob = json.dumps(env)
        # POSITIVE CONTROL: the blob must actually carry the physics, or the
        # absence assertions below hold trivially over an empty envelope.
        self.assertIn("SubagentStart", blob)
        self.assertIn("helm physics", blob)
        for leak in ("abc123", "general-purpose", "unknown_future_key"):
            self.assertNotIn(leak, blob)

    def test_no_cwd_is_global_scope_never_a_guess(self):
        # POSITIVE CONTROL FIRST: a cwd inside helm must actually resolve to
        # the helm project and DELIVER, or every must-miss below passes against
        # a seam that emits for nobody.
        with _scoped():
            env, project = saguide.payload(json.dumps({"cwd": _HELM_CWD}))
            self.assertIsNotNone(env, "control: a helm cwd must deliver")
            self.assertEqual(project, "helm")
            # MUST-MISS 1 — A FOREIGN PROJECT GETS NOTHING. Handing another
            # project's agent helm's testing rules is worse than handing it
            # none: it cannot cross-check what it is told.
            env2, proj2 = saguide.payload(json.dumps({"cwd": "/elsewhere"}))
            self.assertIsNone(env2, "a foreign cwd must NOT receive helm physics")
            # MUST-MISS 2 — UNKNOWN SCOPE GETS NOTHING EITHER. The earlier
            # version emitted here, so a helm cwd, a foreign cwd and NO cwd all
            # received the identical block: scope computed and discarded.
            env3, proj3 = saguide.payload(json.dumps({}))
            self.assertIsNone(env3, "no cwd must NOT receive helm physics")
            self.assertIsNone(proj3)


class NestedSpawnAtStartTest(unittest.TestCase):
    """task/2971: the nested-spawn reflex rides THIS seam.

    MEASURED in the installed Claude Code 2.1.280 binary: the SubagentStart
    hookSpecificOutput schema is {hookEventName, additionalContext}, and the
    subagent runner pushes every additionalContext into the subagent's opening
    messages as a hook_additional_context attachment, unless the agent is
    isolated-context, before the subagent's first step. The PreToolUse
    envelope the first cut used reaches the model on its NEXT step, after
    the Agent call has already run. So this is the seam that can steer the
    spawn before it happens; refusing the call is task/1775's.

    A live nested-spawn reflex follows the brief for a helm cwd, and comes
    alone for another project or for no cwd, honouring the project the reflex
    records (reflex.spawn_steers). Each arm gets its own home."""

    STEER = "a fixture steer: a bounded subagent does its own work"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-saguide-nested-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        prior = os.environ.get("HELM_HOME")

        def restore():
            if prior is None:
                os.environ.pop("HELM_HOME", None)
            else:
                os.environ["HELM_HOME"] = prior
        self.addCleanup(restore)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")

    def plant(self, **extra):
        from helm import reflex
        reflex.write(dict({"id": "subagent-fanout", "steer": self.STEER,
                           "signal": "nested-spawn"}, **extra))

    @staticmethod
    def project_of(cwd):
        return {_HELM_CWD: "helm", "/work/fixture": "fixture-project",
                "/work/other": "other-project"}.get(cwd)

    def ctx(self, payload):
        with _scoped(self.project_of):
            env, _ = saguide.payload(json.dumps(payload))
        return None if env is None else env["hookSpecificOutput"]["additionalContext"]

    def test_a_helm_subagent_gets_the_brief_then_the_reflex(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty expected string, which an empty or absent result fails
        self.plant()
        self.assertEqual(self.ctx({"cwd": _HELM_CWD}),
                         saguide.brief() + "\n" + "REFLEX: " + self.STEER)

    def test_another_projects_subagent_gets_the_reflex_and_no_helm_physics(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty expected string, which an empty or absent result fails
        self.plant()
        self.assertEqual(self.ctx({"cwd": "/work/other"}), "REFLEX: " + self.STEER)

    def test_no_cwd_gets_the_fleet_reflex_only(self):  # noqa: VACUOUS_ASSERTION — asserted EQUAL to a non-empty expected string, which an empty or absent result fails
        self.plant()
        self.assertEqual(self.ctx({}), "REFLEX: " + self.STEER)

    def test_a_project_scoped_reflex_reaches_its_own_project_only(self):  # noqa: VACUOUS_ASSERTION — the owning project's envelope is asserted EQUAL to the non-empty steer before the absences, on the same payload() path
        self.plant(project="fixture-project")
        self.assertEqual(self.ctx({"cwd": "/work/fixture"}),
                         "REFLEX: " + self.STEER)
        self.assertIsNone(self.ctx({"cwd": "/work/other"}))
        self.assertIsNone(self.ctx({}))
        self.assertEqual(self.ctx({"cwd": _HELM_CWD}), saguide.brief())

    def test_no_nested_spawn_reflex_leaves_every_envelope_as_it_was(self):  # noqa: VACUOUS_ASSERTION — the helm cwd is asserted EQUAL to the non-empty brief on the same payload() call path the absences use
        self.assertEqual(self.ctx({"cwd": _HELM_CWD}), saguide.brief())
        self.assertIsNone(self.ctx({"cwd": "/work/other"}))
        self.assertIsNone(self.ctx({}))

    def test_the_hook_entry_prints_the_reflex(self):
        self.plant()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(
            {"cwd": "/work/other", "agent_id": "afixture01"}).encode("utf-8")))
        buf = io.StringIO()
        with _scoped(self.project_of), mock.patch("sys.stdin", stdin), \
                mock.patch("sys.stdout", buf):
            rc = saguide.cmd_saguide(["--hook-json"])
        self.assertEqual(rc, 0)
        hso = json.loads(buf.getvalue())["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SubagentStart")
        self.assertEqual(hso["additionalContext"], "REFLEX: " + self.STEER)


class BuildCapableChildrenOnlyTest(unittest.TestCase):
    """task/2980 lane 7: the brief and the nested-spawn reflex reach a child
    that can build, found by the payload's agent_type. A read-only child is
    told to search or plan and holds no Agent tool, so the testing, beacon
    and nested-spawn rules change none of its acts. Measured over 779
    subagents, 45% were read-only; each of them paid the block anyway."""

    # The nested-spawn fixture's own home and helpers, borrowed rather than
    # inherited so its arms do not run twice.
    STEER = NestedSpawnAtStartTest.STEER
    setUp = NestedSpawnAtStartTest.setUp
    plant = NestedSpawnAtStartTest.plant
    project_of = staticmethod(NestedSpawnAtStartTest.project_of)
    ctx = NestedSpawnAtStartTest.ctx

    def test_a_read_only_child_hears_nothing_and_a_builder_still_does(self):  # noqa: VACUOUS_ASSERTION — every builder type is asserted EQUAL to the non-empty brief plus reflex on the same payload() path before the absences
        self.plant()
        # MUST-HIT on the same payload path: a builder hears the brief and
        # the reflex, so the absences below are the gate and not a seam that
        # emits for nobody.
        whole = saguide.brief() + "\n" + "REFLEX: " + self.STEER
        for kind in ("general-purpose", "a-custom-agent", "a-plugin:custom-agent",
                     None):
            with self.subTest(agent_type=kind):
                self.assertEqual(self.ctx({"cwd": _HELM_CWD, "agent_type": kind}),
                                 whole)
        for kind in sorted(saguide.READ_ONLY_AGENT_TYPES):
            with self.subTest(agent_type=kind):
                self.assertIsNone(self.ctx({"cwd": _HELM_CWD,
                                            "agent_type": kind}))
                self.assertIsNone(self.ctx({"cwd": "/work/other",
                                            "agent_type": kind}))

    def test_the_hook_entry_prints_nothing_for_a_read_only_child(self):  # noqa: VACUOUS_ASSERTION — the builder's printed envelope must carry the physics through the same entry and buffers, a non-empty positive
        self.plant()
        printed = {}
        for kind in ("Explore", "general-purpose"):
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(
                {"cwd": _HELM_CWD, "agent_id": "afixture02",
                 "agent_type": kind}).encode("utf-8")))
            buf = io.StringIO()
            with _scoped(self.project_of), mock.patch("sys.stdin", stdin), \
                    mock.patch("sys.stdout", buf):
                self.assertEqual(saguide.cmd_saguide(["--hook-json"]), 0)
            printed[kind] = buf.getvalue()
        self.assertIn("helm physics", printed["general-purpose"])
        self.assertEqual(printed["Explore"], "")

    def test_another_project_hears_its_own_reflex_and_nothing_of_helms(self):  # noqa: VACUOUS_ASSERTION — the other project's envelope is asserted EQUAL to its non-empty steer, and helm's carries its own rule
        # task/2987 (a): the hook runs in every project now, so the fence is
        # the payload's. A helm-scoped reflex and helm's brief stay home.
        from helm import reflex
        self.plant(project="other-project")
        reflex.write({"id": "helm-only-spawn", "steer": "helm's own spawn rule",
                      "signal": "nested-spawn", "project": "helm"})
        other = self.ctx({"cwd": "/work/other", "agent_type": "general-purpose"})
        self.assertEqual(other, "REFLEX: " + self.STEER)
        self.assertIn("helm's own spawn rule",
                      self.ctx({"cwd": _HELM_CWD}))


class WiredIntoTheContract(unittest.TestCase):
    def test_specs_carries_a_subagentstart_entry(self):
        """The structural half. It proves the hook is CONTRACTED — never that a
        subagent received anything."""
        from helm import hooks
        subs = [s for s in hooks.SPECS if s["event"] == "SubagentStart"]
        self.assertEqual(len(subs), 1, "exactly one SubagentStart spec")
        self.assertEqual(subs[0]["args"], "saguide --hook-json")
        # MUST-MISS: it must NOT be wired to inject, whose envelope names the
        # wrong event and would be dropped in silence.
        self.assertNotIn("inject", subs[0]["args"])

    def test_subagentstop_spec_still_present(self):
        """MUST-HIT CONTROL for the arm above: SubagentStop already existed, so
        if this census cannot see it, a zero for SubagentStart would have been a
        fact about my filter rather than about SPECS."""
        from helm import hooks
        self.assertGreater(len(hooks.SPECS), 5)  # the table is populated at all
        stop = [s for s in hooks.SPECS if s["event"] == "SubagentStop"]
        self.assertEqual(len(stop), 1)
        self.assertIn("delegation-stop", stop[0]["args"])

    def test_cli_exposes_the_verb(self):
        """The dispatch table is `VERBS`. My first draft of this arm asserted
        `cli.COMMANDS`, which does not exist — an invented API name that a
        hand-run caught. Positive control on the same observable first, so a
        renamed-or-empty table cannot make the membership check vacuous."""
        from helm import cli
        self.assertGreater(len(cli.VERBS), 20)
        self.assertIn("inject", cli.VERBS)   # control: a verb known to exist
        self.assertIn("saguide", cli.VERBS)
        self.assertTrue(callable(cli.VERBS["saguide"]))

        # THE SYNOPSIS HALF, WHICH HAD NO ARM AND WOULD HAVE FAILED SILENTLY.
        # This lane adds `saguide` to TWO tables in helm/cli.py — the dispatch
        # map above and the help text below — and it is the one file that
        # conflicts on every rebase, because trunk edits the same two dicts for
        # other verbs. A union resolve that keeps the dispatch entry and drops
        # the synopsis leaves a verb that RUNS and cannot be discovered:
        # `helm saguide --help` still works, but the verb is invisible to
        # anyone reading the CLI's own listing, which is where an agent looks
        # first. The dispatch half reddens above; without this the synopsis
        # half is a silent loss, and a silent loss in a conflict resolution is
        # exactly what nobody re-reads.
        self.assertIn("inject", cli._VERB_HELP)   # control: same table, known verb
        self.assertIn("saguide", cli._VERB_HELP)
        self.assertIn("SubagentStart", cli._VERB_HELP["saguide"])

    def test_show_prints_the_canonical_block_the_brief_points_at(self):
        """Acceptance point 1: ONE canonical value, two consumers — the human
        reading `--show` and the subagent that follows the brief's pointer to
        it. The brief is what the HOOK ships; this is what the pointer buys."""
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = saguide.cmd_saguide(["--show"])
        self.assertEqual(rc, 0)
        printed = buf.getvalue().strip()
        # POSITIVE CONTROL: --show must actually print something substantive
        # before an equality against guidance() can mean anything.
        self.assertGreater(len(printed), 200)
        self.assertIn("helm physics", printed)
        self.assertEqual(printed, saguide.guidance().strip())

    def test_docs_point_at_the_canonical_block_and_never_copy_it(self):
        """ACCEPTANCE POINT 1, THE HALF I CLAIMED AND HAD NOT BUILT. The first
        commit said GUIDANCE was consumed by the new-agent guide while the guide
        was untouched — a claim proved only by an arm comparing `--show` to the
        constant, which is not the same statement.

        The guide must POINT at the command, never PASTE the text: a copy in a
        doc is a second version that stops tracking the first, which is exactly
        how canon ends up naming a command that no longer works."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(saguide.__file__)))
        with open(os.path.join(root, "docs", "NEW_AGENT_GUIDE.md"),
                  encoding="utf-8") as fh:
            guide = fh.read()
        self.assertIn("helm saguide --show", guide)
        # MUST-MISS: a pasted copy of the block would drift. Any distinctive
        # sentence from GUIDANCE appearing verbatim in the guide is the defect.
        distinctive = [ln.strip() for ln in saguide.guidance().splitlines()
                       if len(ln.strip()) > 40]
        self.assertTrue(distinctive)  # control: the block has quotable lines
        for line in distinctive:
            self.assertNotIn(line, guide)

    def test_hooks_doc_enumerates_the_subagentstart_contract(self):
        """No stale docs: HOOKS.md enumerated every wired event except this one."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(saguide.__file__)))
        with open(os.path.join(root, "docs", "HOOKS.md"),
                  encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("`SubagentStart`", doc)          # the event
        self.assertIn("helm saguide --hook-json", doc)  # its command
        # The status line must state INSTALLATION, never delivery — the
        # headline is what gets read, and delivery is not observable
        # from a settings file.
        self.assertIn("INSTALLED for", doc)
        self.assertIn("delivery unobserved", doc)
        self.assertNotIn("SA initial context: N of M seats", doc)
        # control: the table it joins is really the one that lists the others
        self.assertIn("`SubagentStop`", doc)

    def test_unknown_flag_refuses_rather_than_pretending(self):
        """MUST-MISS on the CLI: `saguide --bogus` must not quietly succeed."""
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = saguide.cmd_saguide(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err.getvalue())


class HookJsonReadIsItselfAPath(unittest.TestCase):
    """cmd_saguide's stdin read, which payload()'s own arms cannot reach.

    payload() has always been fail-open and an arm above proves it. That arm
    could not see this defect BY CONSTRUCTION: cmd_saguide DECODED stdin before
    calling payload(), so a payload whose BYTES are not valid UTF-8 raised
    UnicodeDecodeError inside the read and payload()'s except never ran.
    Measured on this tree before the cure — a NUL/0xff-prefixed payload exited
    1 with a traceback onto the spawning agent's stderr, while every other
    malformed shape exited 0. The module docstring promised "FAIL OPEN on every
    path" and the very first path in the function was the exception.

    An arm that calls payload() directly can never catch a defect in its
    CALLER, so this one drives the CLI entry point instead.
    """

    @staticmethod
    def _stdin(raw_bytes):
        """A stdin stub FAITHFUL TO THE REAL ONE, which is load-bearing here.

        A stub with only `.buffer` would make this arm go red against the old
        code for the WRONG REASON — AttributeError on the `.read()` the old
        line called, not the UnicodeDecodeError that was the actual defect. So
        `read()` decodes STRICTLY, exactly as the TextIOWrapper wrapping a real
        stdin does, and the arm discriminates on the true failure.
        """
        class _Stdin:
            def __init__(self, raw):
                self.buffer = io.BytesIO(raw)
                self._raw = raw

            def isatty(self):
                return False

            def read(self):
                return self._raw.decode("utf-8")

        return _Stdin(raw_bytes)

    def _run(self, raw_bytes):
        out = io.StringIO()
        with mock.patch("sys.stdin", self._stdin(raw_bytes)), \
                mock.patch("sys.stdout", out), _scoped():
            rc = saguide.cmd_saguide(["--hook-json"])
        return rc, out.getvalue()

    def test_undecodable_stdin_declines_rather_than_killing_the_spawn(self):  # noqa: VACUOUS_ASSERTION — the emptiness IS the contract (decline, not emit), and the unconditional same-observable control is the good-payload assertion at the top of the body: if this path emitted nothing for everything, that control fails first
        """An SA with no physics is bad; an SA that cannot start is worse."""
        good = json.dumps({"cwd": _HELM_CWD}).encode()

        # POSITIVE CONTROL, ON THE SAME PATH AND UNCONDITIONAL. Without it
        # every assertion below passes against a cmd_saguide that emits
        # nothing for every input, which is a seam delivering to no one.
        rc, emitted = self._run(good)
        self.assertEqual(rc, 0)
        self.assertIn("SubagentStart", emitted,
                      "control: a well-formed payload must still emit")

        # MUST-MISS: bytes no UTF-8 decoder accepts. The hook must DECLINE —
        # rc 0 and silence — never raise.
        rc, emitted = self._run(b"\x00\xff\xfe" + good)
        self.assertEqual(rc, 0,
                         "an undecodable payload must not fail the hook")
        self.assertEqual(emitted, "",
                         "an undecodable payload must emit nothing")

    def test_valid_json_carrying_one_bad_byte_is_declined_not_repaired(self):  # noqa: VACUOUS_ASSERTION — the emptiness IS the contract (decline, never silently repair), and the unconditional same-observable control is the assertIn on `emitted` three lines above: a seam that emitted nothing for everything fails that control first. The rung cannot see it because `emitted` is rebound between the control and the assertion.
        """The prefix case is the EASY one and my first arm only tested it.

        `\x00\xff` before the JSON makes the parse fail, so payload() declines
        for a reason that has nothing to do with decoding. A probe measured
        the case that survives: ONE 0xff inside an otherwise irrelevant string,
        where the JSON still parses. Under errors="replace" that returned rc 0
        and a full 1452-byte envelope — the arm and the implementation shared a
        blind spot BY CONSTRUCTION.

        It matters because `cwd` DECIDES which project's physics this block
        states. A silently repaired path can resolve to the wrong scope, and a
        foreign cwd is supposed to get nothing rather than someone else's
        rules.
        """
        good = json.dumps({"cwd": _HELM_CWD}).encode()
        rc, emitted = self._run(good)
        self.assertEqual(rc, 0)
        self.assertIn("SubagentStart", emitted, "control: good payload emits")

        # MUST-MISS: the JSON is still well-formed; only one byte is invalid.
        payload = json.dumps({"cwd": _HELM_CWD, "note": "PLACEHOLDER"}).encode()
        rc, emitted = self._run(payload.replace(b"PLACEHOLDER", b"a\xffb"))
        self.assertEqual(rc, 0, "a bad byte must not fail the hook")
        self.assertEqual(emitted, "",
                         "valid JSON carrying an undecodable byte must be "
                         "DECLINED, never silently repaired")

    def test_closed_reader_does_not_fail_the_hook_in_a_real_child(self):
        """A REAL CHILD, because the defect lives in interpreter SHUTDOWN.

        Wrapping print() is not enough and an in-process arm cannot see why:
        the write lands in a buffer, returns successfully, and the BROKEN PIPE
        surfaces when the interpreter flushes on exit — outside every try in
        the function. A probe measured it: a valid payload with the read end
        closed exited 120 with BrokenPipeError on stderr, while the in-process
        arm above reported a clean rc 0. Only a child process has a shutdown.

        Scope is patched INSIDE the child rather than derived from its cwd, for
        the reason in this module's header: a checkout resolves to no
        registered helm scope on the fab, and an arm that derives scope from
        its host measures the host.
        """
        code = (
            "import sys\n"
            "from unittest import mock\n"
            "from helm import saguide\n"
            "with mock.patch('helm.inject._ledger.project_for_cwd',\n"
            "                lambda cwd: 'helm'):\n"
            "    rc = saguide.cmd_saguide(['--hook-json'])\n"
            "sys.exit(rc)\n")
        payload = json.dumps({"cwd": _HELM_CWD}).encode()

        # THE CHILD GETS THE REPO ON ITS PATH EXPLICITLY, NEVER FROM cwd.
        # This arm used to pass cwd=os.getcwd() and rely on the runner standing
        # at the repo root — which is true by hand and FALSE ON THE FAB, where
        # unittest runs from tests/. Measured: the child died with
        # ModuleNotFoundError: No module named 'helm' and exited 1, so the
        # control failed on a product that was working. Exactly the class this
        # module's header already records for the four scope arms: an arm that
        # inherits something from its host is measuring the host.
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")

        # CONTROL: the same child with the reader OPEN must emit the envelope.
        # Without it this arm passes against a child that emits nothing at all.
        open_child = subprocess.run(
            [sys.executable, "-c", code], input=payload,
            capture_output=True, cwd=repo, env=env)
        self.assertEqual(open_child.returncode, 0,
                         "control child failed: %s"
                         % open_child.stderr.decode("utf-8", "replace")[-400:])
        self.assertIn(b"SubagentStart", open_child.stdout,
                      "control: an open reader must receive the envelope")

        # MUST-MISS: reader closed. rc must stay 0 and stderr must stay clean.
        closed = subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=repo, env=env)
        closed.stdout.close()
        closed.stdin.write(payload)
        closed.stdin.close()
        rc = closed.wait()
        err = closed.stderr.read().decode("utf-8", "replace")
        closed.stderr.close()
        self.assertEqual(rc, 0,
                         "a closed reader must not fail the hook (was 120): %s"
                         % err[-400:])
        self.assertNotIn("BrokenPipeError", err)
        # AND NO OTHER TRACEBACK EITHER. Asserting only the ABSENCE of
        # BrokenPipeError passes when the child died before ever reaching the
        # write — which is precisely what the cwd bug did on the fab. A
        # must-miss that a broken child satisfies is not a must-miss.
        self.assertNotIn("Traceback", err,
                         "the child failed for an unrelated reason: %s"
                         % err[-400:])


class SidechainBeaconRuleTest(unittest.TestCase):
    """task/2542: every subagent hears, at spawn, that the seat's beacon is not
    its to touch.

    A subagent inherits the seat's HELM_CHAT_NAME, session id and environ, so
    helm's identity layer reads it as the seat, and a Monitor it arms routes
    wake lines to the subagent, not to the seat (measured on codex-5, whose
    subagent took the beacon with --replace; once that subagent ended, the
    seat's wake lines went nowhere). SubagentStart is the one event that fires only for subagents and
    so needs no discriminator; the join and resume-turn hooks carry the same
    rule for a sidechain SessionStart."""

    def test_the_initial_physics_carries_the_sidechain_beacon_rule(self):
        g = " ".join(saguide.guidance().split())
        self.assertIn("helm physics", g)           # control: the real block
        self.assertIn("never arm, replace or stop a helm chat wait beacon; "
                      "report to your parent", g)
        from helm import actors
        self.assertIn(actors.SIDECHAIN_RULE, g,
                      "the SubagentStart block and the sidechain hooks must "
                      "state ONE rule, not two spellings of it")


class ReviewAndSiblingPhysicsTest(unittest.TestCase):
    """The owner's ruling: "it should be an integral part of helm".

    Two store rules, curing-a-defect-owes-a-sweep-for-siblings-asking-the-
    same-question and xfam-reviewer-fixes-its-own-findings, reach an agent
    only as JIT whisper text. A review agent briefed "read-only, edit nothing"
    meets neither, so each mechanical finding costs a full builder round and
    each sibling of a cured defect costs another.

    The arm reads the envelope the HOOK ENTRY prints, never the constant: a
    rule that sits in GUIDANCE but not in what the hook emits is a rule no
    subagent receives, and an arm on the constant stays green over it."""

    # Each rule's act, in the words that change it. Whitespace is folded
    # first, so rewrapping the block never reddens this arm.
    RULES = {
        "sibling sweep": ("every other place asking the same question",
                          "callers", "named in your report"),
        "reviewer patches": ("MECHANICAL", "own worktree",
                             "exact reviewed tip", "unpushed",
                             "returns that tip with its verdict"),
        # The boundary is part of the rule: a reviewer told to patch
        # everything would patch a DESIGN finding, which belongs in a meld.
        "reviewer boundary": ("DESIGN", "read-only"),
    }

    def delivered(self):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(
            {"cwd": _HELM_CWD, "agent_type": "general-purpose"}).encode()))
        buf = io.StringIO()
        with _scoped(), mock.patch("sys.stdin", stdin), \
                mock.patch("sys.stdout", buf):
            rc = saguide.cmd_saguide(["--hook-json"])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())["hookSpecificOutput"]

    def test_every_subagent_is_handed_both_rules_at_spawn(self):
        hso = self.delivered()
        # POSITIVE CONTROL on the same envelope: the event the harness keeps,
        # and the block itself, so an absence below is a missing rule and
        # never a missing envelope.
        self.assertEqual(hso["hookEventName"], "SubagentStart")
        ctx = " ".join(hso["additionalContext"].split())
        self.assertIn("helm physics", ctx)
        for rule, words in self.RULES.items():
            for word in words:
                with self.subTest(rule=rule, word=word):
                    self.assertIn(word, ctx,
                                  "%s: the delivered block lacks %r"
                                  % (rule, word))

    def test_the_long_form_names_where_each_rule_is_argued(self):
        """The brief says "More: helm saguide --show". For these two
        rules that pointer resolves only if the long form names the store
        entries that carry the measurements."""
        g = " ".join(saguide.guidance().split())
        self.assertIn("helm physics", g)
        for entry in ("curing-a-defect-owes-a-sweep-for-siblings-asking-the-"
                      "same-question", "xfam-reviewer-fixes-its-own-findings",
                      "reviewer-implements-own-findings"):
            self.assertIn(entry, g)


if __name__ == "__main__":
    unittest.main()
