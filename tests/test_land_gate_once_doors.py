#!/usr/bin/env python3
"""task/3039 lane 1, `land-gate-once-doors`: ONE whole suite per landing
window, and every other run focused.

The land gate needs ONE green whole suite on the exact tree that becomes
trunk. The train's receipt is that suite. A lane-tip whole suite answers
nothing the land gate asks, because the integrator rebases the lane before it
lands. The ledger measured 142 lane whole suites in a week (68.6 h), 38 train
gates stacked under a later green one (13.7 h) and 4 runs of a tree that was
already green (1.7 h).

These arms follow the design's surface x state table, one row at a time. Each
row names its surface (`gate run`, `gate run --plan`, `helm train` and
`lr compose`, the approve refusal) and the state that decides it, and asserts
both what the door does and what it prints. THE THREE DOORS THAT HOLD THE LAND STAY UNCHANGED:
`landgate.py`, `foldcheck.py` and the APPROVE `NEED_SUITE` binding in
`dispatches.mark_verdict`. This lane edits none of them, and the arms that
already pin them (test_gate_focus FocusVerdictArms and FocusLandDoorArms: a
focused receipt never authorizes an approve or a land) run unchanged.
The stop-seam row lives beside its rung, in tests/test_stop_seam.py.
"""
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

from tests import _tmphome
from helm import (dispatches, gate, gateaudits, gatewindow, landreq,
                  landwindow, lane_discipline, saguide)
from tests import test_gate_focus as _focus
from tests import test_landreq as _landreq
from tests.test_landreq import run as _lr_run


ESCAPE_KEYS = ("HELM_GATE_LANE_SUITE", "HELM_GATE_AGAIN",
               "HELM_WORK_INTEGRATOR")


def _git(cwd, *args, env=None):
    p = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                       text=True, timeout=60, env=env)
    if p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr))
    return p.stdout.strip()


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


STUB_TEST = ("import unittest\n"
             "class T(unittest.TestCase):\n"
             "    def test_ok(self):\n"
             "        self.assertTrue(True)\n")


# ---------------------------------------------------------------- F4: focus

class NonPythonFocusArms(_focus.FocusBase):
    """`gate run --focus` on a change set that includes a file with no import
    graph (.md, .sh, a data file) SELECTS instead of refusing: every
    tree-wide audit (`gateaudits.AUDITS`, which now carries
    `test_env_hygiene` and `test_scratch`) plus every test module whose
    source names the changed path.

    The fixture tree ships the whole audit list as stub modules, because the
    path leg applies only to a tree that ships the list. A tree that does not
    (an adopter project) keeps the old refusal; `FocusPlanArms.
    test_a_non_python_change_refuses` in test_gate_focus pins that."""

    def setUp(self):
        super().setUp()
        self._git("checkout", "-q", "main")
        for name in gateaudits.AUDITS:
            self._write("tests/%s.py" % name, STUB_TEST)
        self._write("docs/GUIDE.md", "the guide\n")
        # MUST-HIT, VERBATIM PATH: this module reads the doc by its repo path.
        self._write("tests/test_reads_guide.py",
                    "import unittest\n"
                    "PATH = 'docs/GUIDE.md'\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_r(self):\n"
                    "        self.assertTrue(PATH)\n")
        # MUST-HIT, QUOTED BASENAME: how os.path.join spells the last part.
        self._write("tests/test_joins_guide.py",
                    "import os, unittest\n"
                    "P = os.path.join('docs', \"GUIDE.md\")\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_j(self):\n"
                    "        self.assertTrue(P)\n")
        # MUST-MISS: the name appears only inside a longer word.
        self._write("tests/test_mentions_guidebook.py",
                    "import unittest\n"
                    "WORD = 'MYGUIDE.mdx is another file'\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_m(self):\n"
                    "        self.assertTrue(WORD)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "audits and doc readers")
        self._git("checkout", "-q", "-b", "lane/doc")
        self._write("docs/GUIDE.md", "the guide, edited\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "edit the guide")
        self.head = self._git("rev-parse", "HEAD")

    def audits(self):
        return {"tests." + name for name in gateaudits.AUDITS}

    def test_the_audit_list_carries_env_hygiene_and_scratch(self):
        """audits+2: the two modules outside the closure that failed 9 reds."""
        self.assertIn("test_env_hygiene", gateaudits.AUDITS)
        self.assertIn("test_scratch", gateaudits.AUDITS)

    def test_a_doc_only_change_selects_audits_and_path_naming_tests(self):
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["docs/GUIDE.md"])
        selected = set(plan["selected"])
        self.assertLessEqual(self.audits(), selected)
        self.assertIn("tests.test_reads_guide", selected)
        self.assertIn("tests.test_joins_guide", selected)
        # MUST-MISS: no import edge and no name: strangers stay out.
        for stranger in ("tests.test_gamma", "tests.test_alpha",
                         "tests.test_beta", "tests.test_mentions_guidebook"):
            self.assertNotIn(stranger, selected)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY)

    def test_a_mixed_change_unions_the_import_closure_and_the_path_leg(self):  # noqa: VACUOUS_ASSERTION — err is None only beside four unconditional assertIn on the selection it returned
        self._write("pkg/alpha.py", "X = 1\nW = 4\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "and alpha")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        selected = set(plan["selected"])
        self.assertIn("tests.test_alpha", selected)
        self.assertIn("tests.test_beta", selected)
        self.assertIn("tests.test_reads_guide", selected)
        self.assertLessEqual(self.audits(), selected)
        self.assertNotIn("tests.test_gamma", selected)

    def test_a_tree_missing_one_audit_module_still_refuses(self):
        """The path leg is sound only beside the tree-wide audits. A tree that
        does not ship all of them keeps the refusal (adopters unchanged)."""
        os.remove(os.path.join(self.repo, "tests", "test_vcs.py"))
        self._git("add", "-A")
        self._git("commit", "-qm", "drop one audit")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("docs/GUIDE.md", err)
        self.assertIn("whole suite", err)

    def test_the_plan_verb_prints_the_selection_without_refusing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = gate._cmd_run(["--repo", self.repo, "--focus", "--plan",
                                "--json"])
        self.assertEqual(rc, 0, out.getvalue())
        plan = json.loads(out.getvalue())["plan"]
        self.assertIn("tests.test_reads_guide", plan["selected"])

    def _assemble(self, **edits):
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        plan["executed"] = list(plan["selected"])
        plan["executed_ids"] = 2
        plan.update(edits)
        tree = self._git("rev-parse", "HEAD^{tree}")
        row = {"v": 6, "event": "gate", "ts": "2026-09-24T00:00:00Z",
               "repo_id": self.repo, "head": self.head, "tree": tree,
               "dirty": False, "head_after": self.head, "tree_after": tree,
               "dirty_after": False, "interpreter": gate.interpreter(),
               "host": gate.host(),
               "argv": ([sys.executable, "-m", "unittest", "-v"]
                        + plan["selected"]),
               "suite": False, "label": None, "rc": 0, "wall": 0.31,
               "status": "OK", "ran": 2, "skipped": 0, "detail": "",
               "elapsed": 0.3, "failures": [], "failures_unreadable": False,
               "base_check": None, "focus": plan}
        row["id"] = gate._receipt_id(row)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def test_the_binder_re_derives_the_path_leg(self):
        """Mint and bind use ONE selection function, so a doc-lane focused
        receipt binds a cure-round verdict — and a scope that dropped a
        path-named module is refused by name."""
        row = self._assemble()
        state, _rid, why = gate.bind("gate:" + row["id"], self.head,
                                     repo_id=self.repo,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "VERIFIED", why)
        short = [m for m in row["focus"]["selected"]
                 if m != "tests.test_reads_guide"]
        cut = self._assemble(selected=short, executed=short)
        state, _rid, why = gate.bind("gate:" + cut["id"], self.head,
                                     repo_id=self.repo,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("tests.test_reads_guide", why)


# ----------------------------------------------------- the gate run doors

class DoorBase(unittest.TestCase):
    """A helm-shaped repository with a LANE room (`<root>-wt/<lane>`), a
    COMPOSE room (`<root>-wt/compose/<name>`) and its shared checkout.

    `gate.run` is replaced by a spy that refuses with a marker: the door
    under test decides BEFORE anything runs, so "the spy was called" is the
    whole of "the door admitted it" and no suite ever starts here."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="helm-test-doors-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for key in ESCAPE_KEYS:
            _tmphome.own_env(self, key, "")
            os.environ.pop(key, None)
        _tmphome.own_env(self, "HELM_HOME", os.path.join(self.tmp, "home"))
        _tmphome.own_env(self, "HELM_CHAT_DIR", os.path.join(self.tmp, "chat"))
        _tmphome.own_env(self, "HELM_CHAT_NODE_URL", "")
        _tmphome.own_env(self, "HELM_CHAT_ROOM", "main")
        _tmphome.own_env(self, "HELM_ADOPTED_DIR",
                         os.path.join(self.tmp, "adopted"))
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.email", "d@t")
        _git(self.root, "config", "user.name", "doors")
        _write(self.root, ".gitignore", "__pycache__/\n")
        _write(self.root, "tests/__init__.py", "")
        _write(self.root, "tests/test_one.py", STUB_TEST)
        # A SECOND MODULE, so a lane that edits the first has a focused scope
        # that is not the whole universe and the refusal can print it.
        _write(self.root, "tests/test_two.py", STUB_TEST)
        _tmphome.helm_tree(self, self.root)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "base")
        integrator = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        self.lane = os.path.join(self.root + "-wt", "l1")
        _git(self.root, "worktree", "add", "-q", "-b", "lane/l1", self.lane,
             "main", env=integrator)
        self.compose = os.path.join(self.root + "-wt", "compose", "c1")
        os.makedirs(os.path.dirname(self.compose))
        _git(self.root, "worktree", "add", "-q", "--detach", self.compose,
             "main", env=integrator)
        self.ran = []

        def spy(repo=None, argv=None, label=None, timeout=None, focus=False,
                sliced=False, timings=None):
            self.ran.append({"repo": repo, "label": label, "focus": focus,
                             "sliced": sliced})
            return None, "SPY-RAN"

        patch = mock.patch.object(gate, "run", spy)
        patch.start()
        self.addCleanup(patch.stop)

    def verb(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, env or {}):
            rc = gate._cmd_run(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def plant(self, where, status="OK", **spoil):
        """A whole-suite receipt on the CURRENT tree of `where`, built the
        way the focus fixture's `plant_suite` builds one, so `bind` verifies
        it through the tree arm."""
        head = _git(where, "rev-parse", "HEAD")
        tree = _git(where, "rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        from helm import gateauthority
        row = {"v": 4, "event": "gate", "ts": "2026-09-24T00:00:00Z",
               "repo_id": self.root, "head": head, "tree": tree,
               "dirty": False, "head_after": head, "tree_after": tree,
               "dirty_after": False, "interpreter": ident,
               "host": gate.host(),
               "argv": [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
               "suite": True, "label": None,
               "rc": 0 if status == "OK" else 1, "wall": 41.2,
               "status": status, "ran": 9, "skipped": 0, "detail": "",
               "elapsed": 41.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row.update(spoil)
        row["id"] = gate._receipt_id(row)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def lane_commit(self):
        _write(self.lane, "tests/test_one.py", STUB_TEST + "# lane edit\n")
        _git(self.lane, "add", "-A")
        _git(self.lane, "commit", "-qm", "lane edit")


class LaneDoorArms(DoorBase):
    """Rows: `gate run` in a lane worktree."""

    def test_a_lane_whole_suite_is_REFUSED_and_handed_the_focus_command(self):
        self.lane_commit()
        rc, _out, err = self.verb("--repo", self.lane)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ran, [], "the door let a lane whole suite run")
        self.assertIn("LANE", err)
        # A WHOLE LINE, never a substring: the `--focus --plan` line carries
        # this verb as its prefix, so a substring stays green without it.
        self.assertIn("helm gate run --repo %s --focus" % self.lane,
                      [ln.strip() for ln in err.splitlines()], err)
        self.assertIn("--lane-suite --why", err)
        self.assertIn("HELM_GATE_LANE_SUITE", err)
        # POSITIVE CONTROL on the same spy: the route the refusal printed
        # reaches the run, so the empty list above is the door, not a spy
        # that never records.
        self.verb("--repo", self.lane, "--focus")
        self.assertEqual(len(self.ran), 1)

    def test_the_refusal_leads_with_the_fab_route_and_qualifies_the_local_verb(self):
        """The refusal answers `fab gate` on the agents-only hub, where
        `helm gate run --focus` RUNS the selection locally. The fab testimony
        route comes first; the local verb is last and says which box it is
        for.

        THE LOCAL VERB IS FOUND AS A WHOLE LINE. `helm gate run --repo L
        --focus` is the prefix of the `--focus --plan` line printed above the
        fab route (that verb runs nothing), so a substring index lands on the
        plan line and reads a fab-first refusal as local-first on every
        host."""
        self.lane_commit()
        _rc, _out, err = self.verb("--repo", self.lane)
        lines = [ln.strip() for ln in err.splitlines()]
        fab = [i for i, ln in enumerate(lines)
               if ln.startswith("fab test --repo %s -- " % self.lane)]
        local = [i for i, ln in enumerate(lines)
                 if ln == "helm gate run --repo %s --focus" % self.lane]
        self.assertEqual((len(fab), len(local)), (1, 1), err)
        self.assertLess(fab[0], local[0], err)
        self.assertIn("may run suites", lines[local[0] - 1], err)
        self.assertIn("agents-only", err)
        # the fab route is named by what it is: `helm gate run` never routes
        # through `fab gate`, and the selection runs on the remote runner
        self.assertIn("remote runner", err)
        self.assertNotIn("routes through", err)

    def test_a_blank_escape_in_the_environment_is_no_escape(self):
        """`HELM_GATE_LANE_SUITE` set to whitespace declares no why, so the
        plan question refuses exactly as it does unset."""
        self.lane_commit()
        rc, out, _err = self.verb("--repo", self.lane, "--plan", "--json",
                                  env={"HELM_GATE_LANE_SUITE": " \t "})
        self.assertEqual(rc, 1, out)
        self.assertIsNone(json.loads(out)["plan"])
        # POSITIVE CONTROL on the same variable: one word is a why.
        rc, out, _err = self.verb("--repo", self.lane, "--plan", "--json",
                                  env={"HELM_GATE_LANE_SUITE": " flake "})
        self.assertEqual(rc, 0, out)
        self.assertIn("flake", json.loads(out)["note"])

    def test_the_lane_suite_escape_runs_and_carries_its_why_on_the_label(self):
        self.lane_commit()
        rc, _out, err = self.verb("--repo", self.lane, "--lane-suite",
                                  "--why", "bisecting a flake", "--label",
                                  "mine")
        self.assertEqual(len(self.ran), 1, err)
        label = self.ran[0]["label"]
        self.assertTrue(label.startswith("lane-suite: bisecting a flake"),
                        label)
        self.assertIn("mine", label)
        self.assertLessEqual(len(label), 120)

    def test_the_escape_needs_a_why(self):
        rc, _out, err = self.verb("--repo", self.lane, "--lane-suite")
        self.assertEqual(rc, 2)
        self.assertIn("--why", err)
        self.assertEqual(self.ran, [])
        rc, _out, err = self.verb("--repo", self.lane, "--why", "x")
        self.assertEqual(rc, 2)
        self.assertEqual(self.ran, [])
        # POSITIVE CONTROL: the whole pair reaches the run.
        self.verb("--repo", self.lane, "--lane-suite", "--why", "x")
        self.assertEqual(len(self.ran), 1)

    def test_a_lane_focus_run_is_not_a_whole_suite_and_passes(self):
        self.lane_commit()
        self.verb("--repo", self.lane, "--focus")
        self.assertEqual(len(self.ran), 1)
        self.assertTrue(self.ran[0]["focus"])

    def test_the_hub_plan_question_refuses_a_lane_and_names_the_route(self):
        """fab-gate asks `gate run --plan --json` on the hub before it
        dispatches; a null plan with helm's reason stops it."""
        self.lane_commit()
        rc, out, _err = self.verb("--repo", self.lane, "--plan", "--json")
        self.assertEqual(rc, 1)
        answer = json.loads(out)
        self.assertIsNone(answer["plan"])
        self.assertIn("POLICY refusal", answer["reason"])
        self.assertIn("fab test --repo %s" % self.lane, answer["reason"])
        # the testimony floor carries the lane's own touched module, and the
        # full selection is named as the local verb that computes it
        self.assertIn("tests.test_one", answer["reason"])
        self.assertIn("--focus --plan", answer["reason"])

    def test_the_plan_question_admits_the_escape_from_the_environment(self):
        """The fab wrapper asks the plan question in the caller's own
        environment, so the escape crosses as HELM_GATE_LANE_SUITE."""
        self.lane_commit()
        rc, out, _err = self.verb("--repo", self.lane, "--plan", "--json",
                                  env={"HELM_GATE_LANE_SUITE": "flake hunt"})
        self.assertEqual(rc, 0, out)
        answer = json.loads(out)
        self.assertIsNotNone(answer["plan"])
        self.assertIn("flake hunt", answer["note"])

    def test_the_shared_checkout_is_not_a_lane(self):
        self.verb("--repo", self.root)
        self.assertEqual(len(self.ran), 1)


class TreeDoorArms(DoorBase):
    """Rows: `gate run` on a tree that already holds a whole-suite receipt."""

    def test_a_GREEN_tree_is_refused_naming_the_receipt(self):
        row = self.plant(self.root)
        rc, _out, err = self.verb("--repo", self.root)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ran, [])
        self.assertIn(row["id"], err)
        self.assertIn("GREEN", err)
        # --again is for a RED tree; a green one never re-gates.
        rc, _out, err = self.verb("--repo", self.root, "--again")
        self.assertEqual(rc, 1)
        self.assertEqual(self.ran, [])
        # POSITIVE CONTROL: a focused run is not a whole suite and passes.
        self.verb("--repo", self.root, "--focus")
        self.assertEqual(len(self.ran), 1)

    def test_a_green_receipt_on_ANOTHER_tree_refuses_nothing(self):
        self.plant(self.root)
        _write(self.root, "tests/test_one.py", STUB_TEST + "# moved\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "moved on")
        self.verb("--repo", self.root)
        self.assertEqual(len(self.ran), 1)

    def test_a_RED_tree_reruns_only_with_again(self):
        row = self.plant(self.root, status="FAILED")
        rc, _out, err = self.verb("--repo", self.root)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ran, [])
        self.assertIn(row["id"], err)
        self.assertIn("--again", err)
        self.verb("--repo", self.root, "--again")
        self.assertEqual(len(self.ran), 1)
        self.assertTrue(self.ran[0]["label"].startswith("again"))

    def test_the_hub_plan_question_refuses_a_green_tree(self):
        row = self.plant(self.root)
        rc, out, _err = self.verb("--repo", self.root, "--plan", "--json")
        self.assertEqual(rc, 1)
        answer = json.loads(out)
        self.assertIsNone(answer["plan"])
        self.assertIn(row["id"], answer["reason"])

    def test_a_dirty_tree_is_not_the_receipts_tree(self):
        self.plant(self.root)
        _write(self.root, "scratch.txt", "uncommitted\n")
        self.verb("--repo", self.root)
        self.assertEqual(len(self.ran), 1)


class ComposeDoorArms(DoorBase):
    """Rows: `gate run` in a compose room consults the landing window."""

    def test_a_compose_room_launches_through_the_window_door(self):  # noqa: VACUOUS_ASSERTION — the spy list stays empty beside the recorded window calls, whose room, label and supersede are the positive control
        calls = []

        def launch(room, label=None, trunk_ref=None, supersede=False, **_kw):
            calls.append((room, label, supersede))
            return 0, {"room": room}

        with mock.patch.object(gatewindow, "launch", launch):
            rc, _out, _err = self.verb("--repo", self.compose, "--label",
                                       "train9")
            self.assertEqual(rc, 0)
            rc, _out, _err = self.verb("--repo", self.compose, "--supersede")
            self.assertEqual(rc, 0)
        self.assertEqual(self.ran, [], "a compose room ran outside the window")
        self.assertEqual(calls[0], (self.compose, "train9", False))
        self.assertTrue(calls[1][2], "--supersede did not reach the window")

    def test_a_red_compose_room_reruns_through_the_window_marked_again(self):  # noqa: VACUOUS_ASSERTION — the empty call list stands before --again; the launch recorded after it, with the room and the marked label, is the positive control on the same observable
        """The tree door decides before the room door: a compose room whose
        tree is RED refuses without `--again`, and with it the window launches
        with `again` on the label, as the door's own text promises."""
        calls = []

        def launch(room, label=None, trunk_ref=None, supersede=False, **_kw):
            calls.append((room, label, supersede))
            return 0, {"room": room}

        row = self.plant(self.compose, status="FAILED")
        with mock.patch.object(gatewindow, "launch", launch):
            rc, _out, err = self.verb("--repo", self.compose)
            self.assertEqual(rc, 1)
            self.assertIn(row["id"], err)
            self.assertEqual(calls, [], "a red compose room reached the "
                             "window without --again")
            rc, _out, _err = self.verb("--repo", self.compose, "--again",
                                       "--label", "train9")
            self.assertEqual(rc, 0)
        self.assertEqual(self.ran, [])
        self.assertEqual(calls[0][0], self.compose)
        self.assertTrue(calls[0][1].startswith("again"), calls[0][1])
        self.assertIn("train9", calls[0][1])

    def test_a_green_compose_tree_is_re_gated_unless_its_receipt_can_land(self):  # noqa: VACUOUS_ASSERTION — rc 0, the recorded window call and the note text are asserted positively before the refusal half, whose GREEN text is asserted too
        """task/3066: a compose room's suite is its train's LAND gate, so its
        green tree is already gated only by a receipt a land door takes. A
        green no authenticated door placed (this plant: no mint record, no
        Fab completion, no routed custody) goes to the window with a note;
        the same tree's green, once helm's own runner is recorded as having
        minted it, refuses the second suite as every green tree does."""
        calls = []

        def launch(room, label=None, trunk_ref=None, supersede=False, **_kw):
            calls.append((room, label, supersede))
            return 0, {"room": room}

        from helm import gateimport
        record, err = gateimport.activate(ts="2000-01-01T00:00:00Z")
        self.assertIsNone(err, err)
        row = self.plant(self.compose)
        with mock.patch.object(gatewindow, "launch", launch):
            rc, _out, err = self.verb("--repo", self.compose)
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls[0][0], self.compose)
        self.assertIn("does not bind here", err)
        self.assertIn("cannot authorize a land", err)
        self.assertIsNone(gateimport.record_mint(row, self.compose))
        with mock.patch.object(gatewindow, "launch", launch):
            rc, _out, err = self.verb("--repo", self.compose)
        self.assertEqual(rc, 1)
        self.assertIn("GREEN", err)
        self.assertEqual(len(calls), 1, "a green landable tree reached the "
                         "window a second time")

    def test_the_windows_refusal_is_the_verbs_exit(self):
        with mock.patch.object(gatewindow, "launch",
                               lambda room, **_kw: (3, None)):
            rc, _out, _err = self.verb("--repo", self.compose)
        self.assertEqual(rc, 3)
        self.assertEqual(self.ran, [])
        # POSITIVE CONTROL: the room's focused run is not the window's.
        self.verb("--repo", self.compose, "--focus")
        self.assertEqual(len(self.ran), 1)

    def test_the_hub_plan_question_sends_a_compose_room_to_the_window(self):
        rc, out, _err = self.verb("--repo", self.compose, "--plan", "--json")
        self.assertEqual(rc, 1)
        answer = json.loads(out)
        self.assertIsNone(answer["plan"])
        self.assertIn("helm gate window launch --repo %s" % self.compose,
                      answer["reason"])

    def test_the_compose_container_is_helm_trains_own(self):
        """One container: the rooms `helm train` stands are the rooms this
        door hands to the window."""
        from helm import landwindow
        self.assertEqual(gate.COMPOSE_BOX, landwindow.BOX)
        self.assertEqual(gate.gate_room(self.compose), ("compose",
                                                        self.compose))

    def test_supersede_outside_a_compose_room_is_a_usage_error(self):
        rc, _out, err = self.verb("--repo", self.root, "--supersede")
        self.assertEqual(rc, 2)
        self.assertIn("compose room", err)


class AdopterDoorArms(DoorBase):
    """Row: `gate run` in an adopter (non-helm) tree is unchanged."""

    def test_an_adopter_lane_room_is_not_refused(self):
        os.remove(os.path.join(self.root, "helm", "__init__.py"))
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "not helm")
        _git(self.lane, "merge", "-q", "--ff-only", "main")
        self.lane_commit()
        self.verb("--repo", self.lane)
        self.assertEqual(len(self.ran), 1)


# ------------------------------------------------ F2: the approve refusal

class ApproveRefusalTextArms(unittest.TestCase):
    """Row: `verdict --approve`, no receipt, source clean — the refusal names
    the one repair the sequence runs: hold source-clean, and the approve
    binds the integrator's train receipt."""

    def test_the_ungated_approve_sentence_is_the_hold_not_a_lane_suite(self):
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               return_value=("ok", None)), \
                mock.patch.object(landreq, "gate_requirement",
                                  return_value="required"):
            why, _tier = landreq._approval_refusal({"gate": ""})
        self.assertIn("--source-clean", why)
        self.assertIn("train", why)
        self.assertNotIn("run `helm gate run` on the reviewed tip", why)


# ---------------------------------- a source-clean hold is no car, on trunk

class HeldSourceCleanIsNoCarArms(_landreq.LandReqBase):
    """Row: a HELD source-clean row meets both train composers. A hold
    records no actor, and `dispatch hold --source-clean` is open to any seat
    on any open row, so a hold admits nothing: `helm train` and `lr compose`
    take LIVE READY rows only, exactly as on trunk. Source-clean cars are
    task/3053, which first stamps the hold's actor."""

    def test_a_held_source_clean_row_is_not_a_car(self):  # noqa: VACUOUS_ASSERTION — each absence sits beside a positive on the same observable: the dry run lists the approved row as car 1, and the members list equals exactly the approved row
        ready = self.dispatch(lane="lane/ready")
        _row, err = self.mark_verdict(ready["id"], self.side, "ok",
                                      polarity="approve")
        self.assertIsNone(err, err)
        self.git("checkout", "-q", "-b", "clean", self.a)
        clean_tip = self.commit("clean", path="h")
        self.git("checkout", "-q", self.main)
        held = self.dispatch(ref=clean_tip, lane="lane/clean")
        row, err = dispatches.mark_hold(held["id"], "read clean, owes the "
                                        "land gate", source_clean_tip=clean_tip)
        self.assertIsNone(err, err)
        self.assertEqual(row["source_clean_tip"], clean_tip)
        # CONTROL, on the real projection: the held row is visible, carries
        # its source-clean tip and is not READY, and the approved row IS
        # READY, so each verb below reads the word and sees both rows.
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertEqual(lrs[held["id"]]["source_clean_tip"], clean_tip)
        self.assertNotEqual(lrs[held["id"]]["state"], "READY")
        self.assertTrue(landreq.live_ready(lrs[ready["id"]]))
        # helm train: the approved row is the one car; the held row is not
        # listed at all.
        out = io.StringIO()
        rc = landwindow.compose(self.repo, trunk=self.main, out=out)
        text = out.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertIn("merge order, 1 approve-ready row:", text)
        self.assertIn("1. %s  lane ready  reviewed tip %s"
                      % (ready["id"][:12], self.side[:12]), text)
        self.assertNotIn(held["id"][:12], text)
        # lr compose: the approved row composes, the held row is refused by
        # name as a row that is not live READY.
        rc, text, err = _lr_run(["compose", ready["id"][:12],
                                 held["id"][:12], "--dry-run", "--json"])
        self.assertEqual(rc, 0, err + text)
        got = json.loads(text)
        self.assertEqual([(m["id"], m["approved_tip"]) for m in got["members"]],
                         [(ready["id"], self.side)], err + text)
        self.assertNotIn("basis", got["members"][0])
        self.assertEqual([x["id"] for x in got["excluded"]], [held["id"]])
        self.assertIn("READY", got["excluded"][0]["reason"])


# ------------------------------------------- F5: one process text, agreeing

class ProcessTextArms(unittest.TestCase):
    """Row: the canonical process text. lane_discipline, the new-agent guide
    and the subagent guidance must say one thing: a lane runs FOCUSED rounds
    and the ONE whole suite belongs to the land gate."""

    def read(self, *parts):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_lane_discipline_no_longer_gates_the_whole_suite_at_the_lane(self):
        doc = lane_discipline.__doc__
        self.assertNotIn("gate the whole suite", doc)
        self.assertIn("focused", doc)
        self.assertIn("land gate", doc)

    def test_the_new_agent_guide_holds_source_clean_for_the_train(self):
        guide = self.read("docs", "NEW_AGENT_GUIDE.md")
        self.assertNotIn("mint the\n  receipt with `helm gate run` (or `fab "
                         "gate`) in your room", guide)
        self.assertIn("--source-clean", guide)

    def test_the_subagent_guidance_no_longer_says_focus_refuses_docs(self):
        self.assertNotIn("REFUSES if your lane touches any", saguide.GUIDANCE)
        self.assertIn("LAND gate", saguide.GUIDANCE)


if __name__ == "__main__":
    unittest.main()
