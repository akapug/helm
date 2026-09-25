#!/usr/bin/env python3
"""The focused gate: a receipt whose SCOPE is first-class, measured, and
hash-bound — strong enough to bind a cure-round review verdict, refused by
everything that authorizes a land.

Every fixture here is a REAL git repo with a REAL tiny test tree, because the
thing under test is a measurement pipeline (worktree diff -> import graph ->
selection -> re-derived diff at bind time) and a mocked measurement is the
caller-supplied scope this lane exists to refuse.

THE MUST-MISS DISCIPLINE, applied to these arms on purpose: a must-hit proves
a probe can SEE; only a must-miss proves it can DISCRIMINATE. Every selection
arm asserts an EXCLUSION beside its inclusion, and the binding arms include a
fabricated-garbage scope that must score ZERO coverage rather than matching
everything — an empty pattern matches every line of a ledger, and a must-hit
control beside it goes on firing throughout, so an inclusion-only arm cannot
tell a working filter from no filter at all.
"""
import ast
import contextlib
import inspect
import io
import json
import os
import textwrap
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helm import (dispatches, foldcheck, gate, gateauthority, gateimport,
                  landgate, landreq, vcs)
from tests._satellite_resolution import ledger_sources
from tests._gate_receipt import serial_process
from tests._gate_supervisor import require_supervisor

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "HELM_CHAT_NODE_URL", "HELM_VERDICT_ROOM", "HELM_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "HELM_NO_TREE_WARNING", "HELM_CROSS_TREE_GATE")


def _emit(*lines):
    """A caller-supplied child used by the custom-argv refusal control."""
    return ["-c", "import sys; print(%r, file=sys.stderr)" % "\n".join(lines)]


ALPHA_TEST = ("import unittest\n"
              "from pkg import alpha\n"
              "class T(unittest.TestCase):\n"
              "    def test_a(self):\n"
              "        self.assertEqual(alpha.X, 1)\n")
# The beta consumer imports LAZILY inside a function — helm's dominant style —
# so a graph that only reads module-level imports misses it and the selection
# arm below goes red.
BETA_TEST = ("import unittest\n"
             "def _lazy():\n"
             "    from pkg import beta\n"
             "    return beta.Y\n"
             "class T(unittest.TestCase):\n"
             "    def test_b(self):\n"
             "        self.assertEqual(_lazy(), 1)\n")
GAMMA_TEST = ("import unittest\n"
              "class T(unittest.TestCase):\n"
              "    def test_g(self):\n"
              "        self.assertTrue(True)\n")


class FocusBase(unittest.TestCase):
    """A scratch project: pkg.alpha <- pkg.beta, three test modules, and a
    lane branch that changed pkg/alpha.py. tests.test_alpha consumes alpha
    directly, tests.test_beta reaches it transitively through beta's
    module-level import, tests.test_gamma consumes nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-focus-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        # ONE ENVIRONMENT-RESTORATION OWNER, AND IT RUNS LAST — the ordering
        # GateBase takes, for the same measured reason: unittest runs every
        # addCleanup AFTER tearDown, so restoring this snapshot in tearDown put
        # it BEFORE `helm_tree`'s own cleanup, which then popped the value the
        # snapshot had just put back. Registered here, ahead of any helper that
        # touches the environment, LIFO makes it the final word for EVERY
        # incoming value — "1", "0" and absent alike. See `_tmphome.own_env`.
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_NAME"] = "focus-fixture"
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        # ADMISSION ON A FIXTURE BOX: HELM_PROC never reached it (task/1740).
        from tests._tmphome import pin_admission
        pin_admission(self, proc=os.environ["HELM_PROC"])
        # REALPATH, because the focused bind arm hands this directly to
        # bind(repo_id=...) and that door refuses a path that is not its own
        # realpath — a symlinked TMPDIR would fail the arm for a reason
        # unrelated to what it tests.
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        from tests import _tmphome
        from tests._tmphome import pin_dispatch_home
        pin_dispatch_home(self, self.repo)
        self._git("config", "user.email", "focus@test")
        self._git("config", "user.name", "focus test")
        # __pycache__ ignored in the BASE commit: the focused child imports
        # the fixture packages, and an untracked cache dir would flip
        # dirty_after on every honest mint (measured while probing this
        # lane, not imagined).
        self._write(".gitignore", "__pycache__/\n")
        self._write("pkg/__init__.py", "")
        self._write("pkg/alpha.py", "X = 1\n")
        self._write("pkg/beta.py", "from . import alpha\nY = alpha.X\n")
        self._write("tests/__init__.py", "")
        self._write("tests/test_alpha.py", ALPHA_TEST)
        self._write("tests/test_beta.py", BETA_TEST)
        self._write("tests/test_gamma.py", GAMMA_TEST)
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        _tmphome.helm_tree(self, self.repo)
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self._git("checkout", "-q", "-b", "lane/focus")
        self._write("pkg/alpha.py", "X = 1\nZ = 2\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "lane changes alpha")
        self.head = self._git("rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_env(self):
        """The fixture's whole ENV_KEYS snapshot, put back last."""
        for key, val in self.prior.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val

    def _git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.repo, text=True,
                              capture_output=True).stdout.strip()

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def mint_focused(self):
        """A REAL focused receipt, which means a real guarded child: the mint
        needs the gate guard's cgroup supervisor, so where that is absent this
        says so instead of failing 30 arms about a cure they never touched."""
        require_supervisor()
        row, err = gate.run(repo=self.repo, focus=True)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK", row.get("detail"))
        return row

    def mint_suite(self):
        """A whole-suite receipt through the canonical serial gate command."""
        with serial_process(ran=9):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        return row

    def plant_suite(self):
        """A WHOLE-SUITE receipt at HEAD, planted rather than minted.

        This explicit store fixture reaches four receipt-consumer doors from
        one stable row built the way `CounterfeitArms._assemble` builds one.
        The separate `mint_suite` controls cover the production minting leg
        through `gate.run` and the canonical serial command."""
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        row = {"v": 4, "event": "gate", "ts": "2026-08-05T00:00:00Z",
               "repo_id": self.repo, "head": self.head, "tree": tree,
               "dirty": False, "head_after": self.head, "tree_after": tree,
               "dirty_after": False, "interpreter": ident,
               "host": gate.host(),
               "argv": ([ident["executable"]]
                        + list(gateauthority.SERIAL_ARGV)),
               "suite": True, "label": None, "rc": 0, "wall": 41.2,
               "status": "OK", "ran": 9, "skipped": 0, "detail": "",
               "elapsed": 41.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row["id"] = gate._receipt_id(row)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        # The plant stands in for a LOCAL MINT, so it writes the row the local
        # runner writes beside its receipt: a land takes only a receipt an
        # authenticated door placed (task/3066, tests/test_land_provenance).
        self.assertIsNone(gateimport.record_mint(row, self.repo))
        return row

    def reresolve(self, row, **edits):
        """A row EDITED AFTER MINTING whose id is recomputed so it resolves —
        the shape of an honest mint whose measurement was wrong, which is the
        only way a bad scope can exist without failing the hash first."""
        crafted = json.loads(json.dumps(row))
        for key, value in edits.items():
            crafted[key] = value
        crafted["id"] = gate._receipt_id(crafted)
        return crafted


class FocusPlanArms(FocusBase):
    def test_selection_hits_consumers_and_misses_strangers(self):
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        # MUST-HIT: the direct consumer and the lazy transitive consumer.
        self.assertIn("tests.test_alpha", plan["selected"])
        self.assertIn("tests.test_beta", plan["selected"])
        # MUST-MISS: the stranger stays out, or "selected" means "everything"
        # and the ratio on every evidence line is decoration.
        self.assertNotIn("tests.test_gamma", plan["selected"])
        self.assertEqual(plan["universe"], 3)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY)

    def test_a_deleted_module_still_selects_its_consumers(self):
        """The breakage a deletion causes lives in the files that REMAIN, so
        the graph must resolve edges to a module no walk can find."""
        os.remove(os.path.join(self.repo, "pkg", "beta.py"))
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertIn("pkg/beta.py", plan["changed"])
        self.assertIn("tests.test_beta", plan["selected"])

    def test_a_string_dispatch_edge_is_seen(self):
        """helm/cli.py reaches every verb through `_lazy("name", ...)` ->
        importlib.import_module — a real repo-load-bearing edge no import
        statement carries. Measured before the cure: helm.landgate's closure
        was 1 test module while the CLI dispatched to it."""
        self._write("pkg/hub.py",
                    "import importlib\n"
                    "def reach():\n"
                    "    return importlib.import_module('pkg.alpha')\n")
        self._write("tests/test_hub.py",
                    "import unittest\n"
                    "from pkg import hub\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_h(self):\n"
                    "        self.assertEqual(hub.reach().X, 1)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "string-dispatch hub")
        # Re-anchor the lane so ONLY alpha differs from the merge-base.
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--ff-only", "lane/focus")
        self._git("checkout", "-q", "-b", "lane/focus2")
        self._write("pkg/alpha.py", "X = 1\nZ = 3\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha again")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertIn("tests.test_hub", plan["selected"])
        # The discrimination control rides along: gamma still stays out.
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_a_non_python_change_refuses(self):
        """A file outside the import graph has consumers no graph can name —
        fail closed toward the whole suite, never guess."""
        self._write("notes.md", "prose\n")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("notes.md", err)
        self.assertIn("whole suite", err)
        self.assertIn(gate._SUITE_ROUTE, err)

    def test_an_unconsumed_change_refuses(self):
        """No test consumes it -> a focused run would prove itself by running
        nothing. The refusal, not an empty green receipt."""
        self._git("checkout", "-q", "main")
        self._git("checkout", "-q", "-b", "lane/orphan")
        self._write("pkg/orphan.py", "Q = 9\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "orphan only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("no test module consumes", err)
        self.assertIn(gate._SUITE_ROUTE, err)

    def test_a_full_universe_closure_refuses(self):
        """A focused run that selects every test module is the whole suite by
        another name and must not become the FIFO's bypass."""
        for name in ("alpha", "beta", "gamma"):
            path = os.path.join(self.repo, "tests", "test_%s.py" % name)
            with open(path, "a") as fh:
                fh.write("# touched\n")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("whole suite by another name", err)
        self.assertIn(gate._SUITE_ROUTE, err)

    def test_nothing_changed_refuses(self):
        self._git("checkout", "-q", "main")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("nothing changed", err)
        self.assertIn("A lane room has nothing of its own to test", err)
        self.assertNotIn("run `helm gate run`", err)


class RanSetArms(FocusBase):
    """THE SCOPE IS WHAT RAN, NOT WHAT WAS ASKED FOR.

    A plan is a request; an argv repeats the request; only the child's own
    output says which modules produced tests. These arms are the ones that go
    red the moment the recorded scope is taken from either of the first two —
    the fixture selects a module that CANNOT contribute a test, so the two
    answers are different sets and a reader taking the wrong one is visible."""

    def _silent_consumer(self):
        """A consumer of the changed module holding NO TestCase: the closure
        selects it (it imports alpha), the argv names it, unittest runs it,
        and it reports nothing. Selected 3, ran 2 — measured by construction
        rather than by mocking a runner, because the thing under test is
        whether the RUNNER's word is what gets recorded."""
        self._write("tests/test_delta.py",
                    "from pkg import alpha\n\nVALUE = alpha.X\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "a consumer with no tests")
        self.head = self._git("rev-parse", "HEAD")

    def test_the_recorded_scope_is_what_the_runner_RAN_not_what_argv_asked(
            self):
        self._silent_consumer()
        row = self.mint_focused()
        focus = row["focus"]
        # MUST-HIT: the argv did ask for the silent module, so the request and
        # the answer really are different sets in this fixture.
        self.assertIn("tests.test_delta", focus["selected"])
        self.assertIn("tests.test_delta", row["argv"])
        # MUST-MISS: and the RAN set drops it, because no test reported from
        # it. Deriving `executed` from the plan or from argv makes this line
        # fail — it is the whole point of the arm.
        self.assertEqual(focus["executed"],
                         ["tests.test_alpha", "tests.test_beta"])
        self.assertNotIn("tests.test_delta", focus["executed"])
        # The count is the runner's too, and it agrees with the runner's own
        # summary — two statements out of one child, not one restated twice.
        self.assertEqual(focus["executed_ids"], row["ran"])
        self.assertEqual(row["ran"], 2)
        # `-v` is what makes any of this observable, so it is pinned here.
        self.assertIn("-v", row["argv"])

    def test_the_evidence_line_ratio_counts_modules_that_RAN(self):
        """The reviewer-facing number: 2 of 4, never 3 of 4. A line reporting
        the selection would tell a reviewer three modules covered the change
        when one of them never produced a test."""
        self._silent_consumer()
        row = self.mint_focused()
        self.assertEqual(len(row["focus"]["selected"]), 3)
        self.assertIn("focused 2/4", gate.evidence_line(row))

    def test_a_selected_module_that_never_ran_cannot_bind(self):
        """And the recording is LOAD-BEARING rather than decorative: the same
        honest mint, whose every re-derivable field matches the repository,
        refuses at the cure-round bar because the closure it selected is not
        the set the runner reported."""
        self._silent_consumer()
        row = self.mint_focused()
        state, rid, why = gate.bind(gate.evidence_line(row), self.head,
                                    repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, row["id"])
        self.assertIn("never reported a test from tests.test_delta", why)
        # The must-hit control on the same door: WITHOUT the silent consumer
        # the identical call VERIFIES, so the refusal above is the ran-set
        # check and not a door that stopped binding anything.
        os.remove(os.path.join(self.repo, "tests", "test_delta.py"))
        self._git("add", "-A")
        self._git("commit", "-qm", "drop the silent consumer")
        self.head = self._git("rev-parse", "HEAD")
        again = self.mint_focused()
        state, _rid, why = gate.bind(gate.evidence_line(again), self.head,
                                     repo_id=self.repo,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "VERIFIED", why)
        self.assertIn("runner reported 2 tests across 2 modules", why)

    def test_an_id_count_above_the_runs_own_summary_refuses(self):
        """A ran-set reader that matched MORE ids than the runner says it ran
        was matching lines that are not test ids, so the module set it built
        may name a module nothing ran. The short direction is tolerated (it
        can only cost coverage, never manufacture it); this one refuses."""
        row = self.mint_focused()
        crafted = self.reresolve(
            row, focus=dict(row["focus"], executed_ids=row["ran"] + 1))
        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)):
            state, _rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("other than test ids", why)

    def test_a_receipt_with_no_ran_set_at_all_refuses(self):
        """The fail-closed floor: a v6 whose focus block never recorded what
        ran is a scope claim with no measurement behind it."""
        row = self.mint_focused()
        focus = dict(row["focus"])
        focus.pop("executed")
        crafted = self.reresolve(row, focus=focus)
        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)):
            state, _rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no readable RAN set", why)

    def test_the_ran_set_reader_resolves_a_module_not_a_test_id(self):
        """The unit under both interpreter header shapes: 3.11+ prints the
        method inside the parens and older ones stop at the class, and a
        LONGEST-prefix reading of either records `...T.test_a` as a module —
        a module name nothing can ever match, so coverage would be empty for
        every receipt. Both shapes must land on the module."""
        new_style = "test_a (tests.test_alpha.T.test_a) ... ok"
        old_style = "test_a (tests.test_alpha.T) ... ok"
        # The unconditional positive control on the same observable, so an
        # empty table or a reader that stopped answering cannot pass this arm
        # through the loop below.
        mods, ids = gate._executed_modules(new_style)
        self.assertEqual(mods, ["tests.test_alpha"])
        self.assertEqual(ids, 1)
        for line in (new_style, old_style):
            mods, ids = gate._executed_modules(line)
            self.assertEqual(mods, ["tests.test_alpha"], line)
            self.assertEqual(ids, 1, line)
        # MUST-MISS: a line that is not a unittest header contributes nothing,
        # or every printed line would inflate the set.
        mods, ids = gate._executed_modules("Ran 2 tests in 0.1s\nOK\n"
                                           "some prose about tests.test_beta")
        self.assertEqual(mods, [])
        self.assertEqual(ids, 0)

    def test_an_id_shaped_docstring_line_is_not_a_second_test(self):
        """The verbose protocol's ONE ambiguous shape, measured as
        task/1953's refused receipt: a test WITH a docstring prints TWO
        lines — the bare id on its own line, then `<docstring> ... ok` — and
        when the docstring's own first words are id-shaped (a narrative
        docstring quoting a test id, e.g. "test_b (tests.test_beta.T.test_b)
        matched ..."), the continuation line fullmatches the header grammar
        too. A shape-only reader counts BOTH lines: ids = ran + 1 — the
        exact inflation the bind rung exists to refuse, firing on an honest
        green run. This arm runs the REAL runner on a fixture whose
        docstring is id-shaped and asserts the reader's count is the
        runner's own summary, never above it."""
        self._write("tests/test_docstring.py",
                    "import unittest\n"
                    "from pkg import alpha\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_d(self):\n"
                    "        \"\"\"test_b (tests.test_beta.T.test_b) was the "
                    "id an earlier reader matched on a prose line.\"\"\"\n"
                    "        self.assertEqual(alpha.X, 1)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "a docstring that quotes a test id")
        self.head = self._git("rev-parse", "HEAD")
        # Run the REAL runner on this fixture rather than hand-writing the
        # stream: a synthetic transcript only ever proves the reader against
        # the string its author imagined, and the 2026-08 cgroup wall on the
        # fab nodes makes gate.run's own guarded child unmintable there —
        # the subprocess below needs no gate machinery at all.
        plan, plan_err = gate.focus_plan(self.repo)
        self.assertIsNone(plan_err, plan_err)
        self.assertIn("tests.test_docstring", plan["selected"])
        child = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"] + plan["selected"],
            cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr[-500:])
        out = child.stderr + child.stdout
        parsed = gate.parse_result(out)
        self.assertEqual(parsed["status"], "OK", parsed["detail"])
        # MUST-HIT: the fixture really does produce the two-line shape, so a
        # reader that stopped matching anything cannot pass by going blind.
        self.assertIn("test_d (tests.test_docstring.T.test_d)", out)
        # THE ARM: the count is the runner's own — a reader counting the
        # continuation line records ids = ran + 1, the inflation the bind
        # rung refuses on an honest green run. (tests.test_beta IS in the
        # ran set here — the fixture's closure selects it and it runs — so
        # the phantom-module half of the defect is only observable as the
        # count, which is exactly how the receipt reader saw it.)
        modules, ids = gate._executed_modules(out)
        self.assertEqual(ids, parsed["ran"])
        self.assertIn("tests.test_docstring", modules)


class FocusMintArms(FocusBase):
    def test_a_focused_mint_records_scope_interpreter_and_v6(self):
        row = self.mint_focused()
        self.assertEqual(row["v"], 6)
        self.assertFalse(row["suite"])
        # helm composed the command, so the interpreter is NAMED — the whole
        # difference between focused and custom.
        self.assertEqual(row["interpreter"]["name"],
                         sys.implementation.name)
        focus = row["focus"]
        self.assertEqual(focus["policy"], gate.FOCUS_POLICY)
        self.assertEqual(focus["changed"], ["pkg/alpha.py"])
        self.assertEqual(focus["selected"],
                         ["tests.test_alpha", "tests.test_beta"])
        self.assertEqual(focus["universe"], 3)
        # The RAN half, measured from the child's own protocol: here it agrees
        # with the selection because every selected module really did report
        # tests. RanSetArms holds the case where they differ.
        self.assertEqual(focus["executed"],
                         ["tests.test_alpha", "tests.test_beta"])
        self.assertEqual(focus["executed_ids"], 2)
        self.assertEqual(row["ran"], 2)
        # The id RESOLVES through the integrity filter — the reader knows v6.
        got, err = gate.by_id(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(got["id"], row["id"])
        self.assertIn("focused 2/3", gate.evidence_line(row))

    def test_an_edited_scope_stops_resolving(self):
        """The hash arm: the scope is part of the content id, so a changed-
        list edited after minting is ABSENT-or-tampered, never a receipt."""
        row = self.mint_focused()
        # Count the ledger FILE, not the projection, before and after: the
        # tamper below edits in place, so the row count must not move.
        with open(gate.receipts_path()) as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(lines), 1)
        lines[0]["focus"]["changed"] = ["pkg/whatever.py"]
        with open(gate.receipts_path(), "w") as fh:
            fh.write(json.dumps(lines[0]) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got)
        self.assertIn("does NOT recompute", err)

    def test_focus_refuses_a_caller_argv(self):
        row, err = gate.run(repo=self.repo, focus=True,
                            argv=[sys.executable] + _emit("Ran 1 test in "
                                                          "0.1s", "", "OK"))
        self.assertIsNone(row)
        self.assertIn("caller-supplied scope", err)

    def test_the_import_seam_refuses_the_focused_kind_by_name(self):
        """A plain v6 never enters through `gate import`: only gateroute's
        challenge-framed custody may carry one across boxes. The refusal must
        be the focused-kind message, not the unknown-version one — updating
        helm would not change the answer — and a v4 row wearing a focus block
        stays foreign."""
        row = self.mint_focused()
        err = gateimport._schema_err(row)
        self.assertIn("FOCUSED", err)
        self.assertIn("plain artifact", err)
        self.assertNotIn("update helm", err)
        wrong_version = dict(row, v=4)
        self.assertIn("focus", gateimport._schema_err(wrong_version))
        # the whole-suite control: a v4 row without the foreign key still
        # imports, so the refusal above is the focused gate and not a seam
        # that stopped reading anything.
        suite_row = self.mint_suite()
        self.assertIsNone(gateimport._schema_err(suite_row))


class AuthorityDominanceArms(unittest.TestCase):
    """Pin every VERIFIED exit to exactly one dominating authority spend."""

    @staticmethod
    def _tuple_state(node, state):
        return isinstance(node, ast.Tuple) and node.elts \
            and isinstance(node.elts[0], ast.Constant) \
            and node.elts[0].value == state

    def test_every_bind_success_exit_has_exactly_one_authority_guard(self):
        tree = ast.parse(inspect.getsource(gate.bind))
        fn = tree.body[0]
        calls = [node for node in ast.walk(fn)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id == "_repository_authority_refusal"]
        self.assertEqual(len(calls), 4,
                         "bind has four admission exits and owes one authority "
                         "spend at each")
        exits, focused_refusals = [], []

        def focused_refusal(owner):
            """The one `return focused` whose controlling fact is NOT success."""
            if not isinstance(owner, ast.If) \
                    or not isinstance(owner.test, ast.Compare):
                return False
            test = owner.test
            left = test.left
            return isinstance(left, ast.Subscript) \
                and isinstance(left.value, ast.Name) \
                and left.value.id == "focused" \
                and isinstance(left.slice, ast.Constant) \
                and left.slice.value == 0 \
                and len(test.ops) == 1 \
                and isinstance(test.ops[0], ast.NotEq) \
                and len(test.comparators) == 1 \
                and isinstance(test.comparators[0], ast.Constant) \
                and test.comparators[0].value == "VERIFIED"

        def scan(body, owner=None):
            for index, stmt in enumerate(body):
                direct = isinstance(stmt, ast.Return) \
                    and self._tuple_state(stmt.value, "VERIFIED")
                focused = isinstance(stmt, ast.Return) \
                    and isinstance(stmt.value, ast.Name) \
                    and stmt.value.id == "focused"
                if focused and focused_refusal(owner):
                    focused_refusals.append(stmt.lineno)
                elif direct or focused:
                    exits.append(("focused" if focused else "suite", stmt.lineno))
                    guards = []
                    for before, after in zip(body[:index], body[1:index]):
                        if not isinstance(before, ast.Assign) \
                                or not isinstance(before.value, ast.Call) \
                                or not isinstance(before.value.func, ast.Name) \
                                or before.value.func.id \
                                != "_repository_authority_refusal":
                            continue
                        self.assertEqual(
                            [target.id for target in before.targets
                             if isinstance(target, ast.Name)],
                            ["authority_refusal"])
                        self.assertIsInstance(after, ast.If)
                        self.assertIsInstance(after.test, ast.Name)
                        self.assertEqual(after.test.id, "authority_refusal")
                        refusals = [node for node in after.body
                                    if isinstance(node, ast.Return)
                                    and self._tuple_state(node.value, "REFUSED")]
                        self.assertEqual(len(refusals), 1,
                                         "authority guard must fail closed")
                        guards.append(before.lineno)
                    self.assertEqual(len(guards), 1,
                                     "success must be dominated by exactly one "
                                     "authority guard in its branch")
                for _field, value in ast.iter_fields(stmt):
                    if isinstance(value, list) and value \
                            and all(isinstance(item, ast.stmt)
                                    for item in value):
                        scan(value, owner=stmt)

        scan(fn.body)
        self.assertEqual(len(focused_refusals), 1,
                         "bind owes one fail-closed return of focused evidence")
        self.assertEqual([kind for kind, _line in exits],
                         ["focused", "suite", "suite", "suite"],
                         "the census must stay pinned to one focused and three "
                         "suite admission exits")
        guarded_lines = {node.lineno for node in calls}
        self.assertEqual(len(guarded_lines), len(exits),
                         "an authority call may guard exactly one success exit")

        focused_tree = ast.parse(inspect.getsource(gate._bind_focused))
        focused_verified = [node for node in ast.walk(focused_tree)
                            if isinstance(node, ast.Return)
                            and self._tuple_state(node.value, "VERIFIED")]
        self.assertEqual(len(focused_verified), 1,
                         "the focused validator must have one success exit for "
                         "bind's focused authority guard to dominate")


class FocusBindingArms(FocusBase):
    """The acceptance arms, in the order the contract names them."""

    def test_a_focused_receipt_cannot_authorize_a_land(self):
        """Arm 1, at the binding layer: the suite question REFUSES a focused
        answer. Deleting the `suite` check in bind() turns this red — the
        receipt below passes every OTHER check (clean, bracketed, OK, named
        interpreter, named host, exact tip)."""
        row = self.mint_focused()
        state, rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, row["id"])
        self.assertIn("FOCUSED", why)
        self.assertIn("whole", why.lower())

    def test_a_focused_receipt_binds_a_cure_round_need(self):
        """Arm 2, at the binding layer: the focused question accepts it, and
        the answer names the re-measured coverage, not just the colour."""
        row = self.mint_focused()
        state, rid, why = gate.bind(gate.evidence_line(row), self.head,
                                    repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, row["id"])
        self.assertIn("FOCUSED 2/3", why)
        self.assertIn("scope covers", why)

    def test_a_whole_suite_receipt_still_does_everything(self):
        """Arm 3, the must-hit control: the pre-existing kind binds the suite
        need exactly as before AND satisfies the weaker focused need —
        strength covers weakness, never the reverse."""
        row = self.mint_suite()
        evidence = gate.evidence_line(row)
        state, rid, why = gate.bind(evidence, self.head)
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, row["id"])
        state, rid, _why = gate.bind(evidence, self.head,
                                     repo_id=self.repo,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "VERIFIED")
        self.assertEqual(rid, row["id"])

    def test_suite_exact_tip_spends_authority_at_its_exit(self):
        row, calls = self.mint_suite(), []
        answers = iter(((False, "hostile repository"), (True, None)))

        def authority(receipt, repo):
            calls.append((receipt["id"], repo))
            return next(answers)

        with mock.patch.object(gateimport, "repository_authorization",
                               side_effect=authority), \
                mock.patch.object(gate, "_tree_at",
                                  side_effect=AssertionError(
                                      "exact-tip bind fell into tree equivalence")):
            denied = gate.bind(gate.evidence_line(row), self.head,
                               repo_id=self.repo, need=gate.NEED_SUITE)
            admitted = gate.bind(gate.evidence_line(row), self.head,
                                 repo_id=self.repo, need=gate.NEED_SUITE)
        expected = [(row["id"], self.repo), (row["id"], self.repo)]
        self.assertEqual(calls, expected,
                         "both exact-tip poles must hit the same authority exit")
        self.assertEqual(denied[:2], ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", denied[2])
        self.assertEqual(admitted[:2], ("VERIFIED", row["id"]), admitted[2])
        self.assertIn(" on %s@" % self.head[:12], admitted[2])

    def test_suite_same_tree_spends_authority_at_its_exit(self):
        row, calls = self.mint_suite(), []
        self._git("commit", "--allow-empty", "-qm", "same tree reviewed tip")
        reviewed = self._git("rev-parse", "HEAD")
        self.assertNotEqual(row["head"], reviewed)
        self.assertEqual(row["tree"], self._git("rev-parse", "HEAD^{tree}"),
                         "control: distinct heads must carry the exact same tree")
        answers = iter(((False, "hostile repository"), (True, None)))

        def authority(receipt, repo):
            calls.append((receipt["id"], repo))
            return next(answers)

        with mock.patch.object(gateimport, "repository_authorization",
                               side_effect=authority), \
                mock.patch.object(gate, "_binding_ts",
                                  side_effect=AssertionError(
                                      "same-tree bind fell into descendant time")):
            denied = gate.bind(gate.evidence_line(row), reviewed,
                               repo_id=self.repo, need=gate.NEED_SUITE)
            admitted = gate.bind(gate.evidence_line(row), reviewed,
                                 repo_id=self.repo, need=gate.NEED_SUITE)
        expected = [(row["id"], self.repo), (row["id"], self.repo)]
        self.assertEqual(calls, expected,
                         "both same-tree poles must hit the same authority exit")
        self.assertEqual(denied[:2], ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", denied[2])
        self.assertEqual(admitted[:2], ("VERIFIED", row["id"]), admitted[2])
        self.assertIn(" on tree %s " % row["tree"][:12], admitted[2])

    def test_suite_descendant_spends_authority_at_its_exit(self):
        reviewed, calls = self.head, []
        self._write("pkg/alpha.py", "X = 1\nZ = 3\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "descendant suite tip")
        self.head = self._git("rev-parse", "HEAD")
        row = self.mint_suite()
        self.assertNotEqual(row["tree"],
                            self._git("rev-parse", reviewed + "^{tree}"),
                            "control: descendant must not take same-tree exit")
        answers = iter(((False, "hostile repository"), (True, None)))
        backend = vcs.backend(self.repo)

        def authority(receipt, repo):
            calls.append((receipt["id"], repo))
            return next(answers)

        with mock.patch.object(gateimport, "repository_authorization",
                               side_effect=authority), \
                mock.patch.object(backend, "ancestry",
                                  wraps=backend.ancestry) as ancestry, \
                mock.patch.object(vcs, "backend", return_value=backend):
            denied = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.repo,
                reviewed_ts="2026-08-02T00:00:01Z", need=gate.NEED_SUITE)
            admitted = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.repo,
                reviewed_ts="2026-08-02T00:00:01Z", need=gate.NEED_SUITE)
        expected = [(row["id"], self.repo), (row["id"], self.repo)]
        self.assertEqual(calls, expected,
                         "both descendant poles must hit the authority exit")
        self.assertEqual(ancestry.call_count, 1,
                         "only the authorized pole may reach descendant ancestry")
        self.assertEqual(denied[:2], ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", denied[2])
        self.assertEqual(admitted[:2], ("VERIFIED", row["id"]), admitted[2])
        self.assertIn(" containing reviewed %s" % reviewed[:12], admitted[2])

    def test_descendant_authority_precedes_postdate_classification(self):
        reviewed = self.head
        self._write("pkg/alpha.py", "X = 1\nZ = 4\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "descendant ordering tip")
        self.head = self._git("rev-parse", "HEAD")
        row = self.mint_suite()
        future = "2999-01-01T00:00:00Z"
        real_door = gate._repository_authority_refusal
        bad_repos = ("/nope/not/a/repo/.git", "relative/.git")
        with mock.patch.object(gate, "_repository_authority_refusal",
                               wraps=real_door) as door:
            for repo in bad_repos:
                with self.subTest(repo=repo):
                    state, rid, why = gate.bind(
                        gate.evidence_line(row), reviewed, repo_id=repo,
                        reviewed_ts=future, need=gate.NEED_SUITE)
                    self.assertEqual((state, rid), ("REFUSED", row["id"]))
                    self.assertIn("receipt authority is UNKNOWN", why)
        self.assertEqual(
            [(call.args[0]["id"], call.args[1])
             for call in door.call_args_list],
            [(row["id"], repo) for repo in bad_repos],
            "both unreadable repository shapes must reach the authority door")

        authority = []

        def admit(receipt, repo):
            authority.append((receipt["id"], repo))
            return True, None

        with mock.patch.object(gateimport, "repository_authorization",
                               side_effect=admit):
            state, rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.repo,
                reviewed_ts=future, need=gate.NEED_SUITE)
        self.assertEqual(authority, [(row["id"], self.repo)],
                         "valid repository must cross authority exactly once")
        self.assertEqual((state, rid), ("REFUSED", row["id"]))
        self.assertIn("postdate", why,
                      "authorized content must continue to the time door")

    def test_unauthorized_but_malformed_reaches_the_focus_shape_gate(self):
        """A capability refusal cannot shadow a receipt-content refusal.

        The focus watcher proves the production focus validator executed; the
        empty authority counter proves malformed evidence never reached the
        spend door. Changing only the malformed version to v6 is covered by the
        valid unauthorized arm below, which does reach that door.
        """
        row = self.mint_focused()
        crafted = self.reresolve(row, v=4)
        focus_states, authority = [], []
        real_focus = gate._bind_focused

        def watch_focus(*args, **kwargs):
            result = real_focus(*args, **kwargs)
            focus_states.append(result)
            return result

        def refuse_authority(*args, **kwargs):
            authority.append((args, kwargs))
            return False, "hostile repository"

        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)), \
                mock.patch.object(gate, "_bind_focused",
                                  side_effect=watch_focus), \
                mock.patch.object(gateimport, "repository_authorization",
                                  side_effect=refuse_authority):
            state, rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("REFUSED", crafted["id"]))
        self.assertEqual(len(focus_states), 1,
                         "the malformed arm must execute the focus validator")
        self.assertIn("no verifiable scope", focus_states[0][2])
        self.assertIn("no verifiable scope", why)
        self.assertEqual(authority, [],
                         "malformed evidence authorizes nothing and must not spend")

    def test_unauthorized_but_scope_invalid_reaches_the_coverage_gate(self):
        """Repository-derived scope diagnostics also precede admission."""
        row = self.mint_focused()
        crafted = self.reresolve(
            row, focus=dict(row["focus"], changed=["pkg/beta.py"]))
        focus_states, authority = [], []
        real_focus = gate._bind_focused

        def watch_focus(*args, **kwargs):
            result = real_focus(*args, **kwargs)
            focus_states.append(result)
            return result

        def refuse_authority(*args, **kwargs):
            authority.append((args, kwargs))
            return False, "hostile repository"

        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)), \
                mock.patch.object(gate, "_bind_focused",
                                  side_effect=watch_focus), \
                mock.patch.object(gateimport, "repository_authorization",
                                  side_effect=refuse_authority):
            state, rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("REFUSED", crafted["id"]))
        self.assertEqual(len(focus_states), 1,
                         "the hostile scope arm must execute the focus validator")
        self.assertIn("does not cover pkg/alpha.py", focus_states[0][2])
        self.assertIn("does not cover pkg/alpha.py", why)
        self.assertEqual(authority, [],
                         "invalid scope authorizes nothing and must not spend")

    def test_fully_valid_but_unauthorized_refuses_at_the_authority_door(self):
        row = self.mint_focused()
        focus_states, authority = [], []
        real_focus = gate._bind_focused

        def watch_focus(*args, **kwargs):
            result = real_focus(*args, **kwargs)
            focus_states.append(result)
            return result

        def refuse_authority(receipt, repo):
            authority.append((receipt["id"], repo))
            return False, "hostile repository"

        with mock.patch.object(gate, "_bind_focused",
                               side_effect=watch_focus), \
                mock.patch.object(gateimport, "repository_authorization",
                                  side_effect=refuse_authority):
            state, rid, why = gate.bind(
                gate.evidence_line(row), self.head, repo_id=self.repo,
                need=gate.NEED_FOCUSED)
        self.assertEqual(focus_states[0][0], "VERIFIED", focus_states)
        self.assertEqual(authority, [(row["id"], self.repo)],
                         "valid evidence must hit the authority door exactly once")
        self.assertEqual((state, rid), ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", why)

    def test_valid_and_authorized_crosses_both_focus_and_authority_seams(self):
        row = self.mint_focused()
        focus_states, authority = [], []
        real_focus = gate._bind_focused

        def watch_focus(*args, **kwargs):
            result = real_focus(*args, **kwargs)
            focus_states.append(result)
            return result

        def admit_authority(receipt, repo):
            authority.append((receipt["id"], repo))
            return True, None

        with mock.patch.object(gate, "_bind_focused",
                               side_effect=watch_focus), \
                mock.patch.object(gateimport, "repository_authorization",
                                  side_effect=admit_authority):
            state, rid, why = gate.bind(
                gate.evidence_line(row), self.head, repo_id=self.repo,
                need=gate.NEED_FOCUSED)
        self.assertEqual(focus_states[0][0], "VERIFIED", focus_states)
        self.assertEqual(authority, [(row["id"], self.repo)],
                         "the positive path must spend authority exactly once")
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)
        self.assertIn("scope covers", why)

    def test_the_whole_suite_kind_still_passes_every_door(self):  # noqa: VACUOUS_ASSERTION — the four unconditional positive controls come FIRST and are on the same four observables the refusals below read: bind at both needs returns VERIFIED, landgate returns OK naming the bound tree, and the fold rung returns PASS
        """Arm 3 through ALL FOUR doors at once, on a receipt this box can
        actually produce: the two binding needs, the land clause and the fold
        rung. Adding a second receipt KIND must leave the first one's answers
        exactly where they were, and a control that only the gate can run is
        a control the author never gets to see.

        NOTHING IS INJECTED INTO THE LAND CLAUSE: handing `gate_binds_tree`
        both the tree AND a `gates` map would skip the two measurements that
        clause is — deriving the landed tree from the tip through git, and
        resolving the receipt id through the real store. An arm built that
        way proves its own fixture. It now passes the repo and the tip and
        NOTHING else, so the id and the tree in the answer come back from the
        code, and this arm re-derives the expected tree independently to
        compare against."""
        row = self.plant_suite()
        evidence = gate.evidence_line(row)
        self.assertIn("whole-suite", evidence)
        state, rid, why = gate.bind(evidence, self.head)
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, row["id"])
        state, _rid, why = gate.bind(evidence, self.head, repo_id=self.repo,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "VERIFIED", why)
        # The land clause on its REAL path: no tree, no map — it derives the
        # tree from the tip and reads the receipt out of the store the plant
        # wrote to.
        expected_tree = self._git("rev-parse", "HEAD^{tree}")
        state, why = landgate.gate_binds_tree(row["id"], None,
                                              repo=self.repo, tip=self.head)
        self.assertEqual(state, landgate.OK, why)
        self.assertIn(row["id"], why)
        self.assertIn(expected_tree[:12], why)
        rung = foldcheck._tree_matches_gate(vcs.backend(self.repo), self.repo,
                                            self.head, "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.PASS, rung.discriminator)
        # MUST-MISS on the same four doors, so this arm cannot be a set of
        # doors that say yes to anything: the focused kind, minted into the
        # same store, is refused by three of them and accepted by the fourth
        # only because that is the need it was built for.
        focused = self.mint_focused()
        self.assertEqual(gate.bind(gate.evidence_line(focused),
                                   self.head)[0], "REFUSED")
        state, why = landgate.gate_binds_tree(focused["id"], None,
                                              repo=self.repo, tip=self.head)
        self.assertEqual(state, landgate.REFUSE, why)
        self.assertIn(focused["id"], why)
        self.assertIn("FOCUSED", why)
        self.assertEqual(foldcheck._tree_matches_gate(
            vcs.backend(self.repo), self.repo, self.head,
            "gate:" + focused["id"]).verdict, foldcheck.REFUSE)
        self.assertEqual(gate.bind(gate.evidence_line(focused), self.head,
                                   repo_id=self.repo,
                                   need=gate.NEED_FOCUSED)[0], "VERIFIED")

    def test_a_scope_that_does_not_cover_the_diff_is_refused(self):
        """Arm 4: bind re-derives base..tip itself and refuses the omission BY
        NAME — the challenge "you omitted X's consumers" is answered by the
        binder's own derivation, never argued from the recorded scope."""
        row = self.mint_focused()
        crafted = self.reresolve(
            row, focus=dict(row["focus"], changed=["pkg/beta.py"]))
        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)):
            state, rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, crafted["id"])
        self.assertIn("does not cover pkg/alpha.py", why)

    def test_a_garbage_scope_scores_zero_not_everything(self):
        """Arm 5, the MUST-MISS: coverage is exact set membership, so "" and
        "*" cover NOTHING. If glob/prefix semantics ever sneak into the
        coverage check, "*" matches the world and this arm is the only thing
        that notices — the must-hit above would keep passing throughout."""
        row = self.mint_focused()
        crafted = self.reresolve(
            row, focus=dict(row["focus"], changed=["", "*"]))
        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)):
            state, _rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not cover pkg/alpha.py", why)

    def test_a_focused_receipt_binds_only_its_exact_tip(self):
        """No descendant arm: the scope was measured against ONE tree."""
        row = self.mint_focused()
        self._write("pkg/alpha.py", "X = 1\nZ = 4\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "moved")
        moved = self._git("rev-parse", "HEAD")
        state, _rid, why = gate.bind(
            gate.evidence_line(row), moved, repo_id=self.repo,
            reviewed_ts="2026-08-02T00:00:01Z", need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("exact tree", why)

    def test_focused_binding_needs_the_standing_repository(self):
        """No repo -> no re-measurement -> no binding. The recorded scope
        alone is never accepted on its own word."""
        row = self.mint_focused()
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("re-measure", why)

    def test_a_v4_shaped_row_wearing_a_scope_is_refused(self):
        """Only v6 binds the scope into the id; at any other version the
        focus block is outside the hash's protection and must not bind."""
        row = self.mint_focused()
        crafted = self.reresolve(row, v=4)
        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)):
            state, _rid, why = gate.bind(
                gate.evidence_line(crafted), self.head,
                repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no verifiable scope", why)

    def test_an_unknown_need_refuses(self):
        """The axis itself fails closed: a typo'd need must not quietly
        receive the weaker answer."""
        row = self.mint_focused()
        state, rid, why = gate.bind(gate.evidence_line(row), self.head,
                                    need="anything")
        self.assertEqual(state, "REFUSED")
        self.assertIsNone(rid)
        self.assertIn("unknown binding need", why)


class OneBindArms(FocusBase):
    """THE VERB MAY NOT ASK THE LEDGER TWICE. `gate run` printed one bind's
    answer and returned another bind's answer, and `bind` is not a pure
    function of its arguments — it
    resolves a token against a ledger file and can ask the repository about
    refs. Between two calls a concurrent mint, a compaction or a landing
    rebase can change the answer, and then the line a reviewer PASTES and the
    exit code a script BRANCHES on describe different worlds.

    The arm proves that divergence is reachable rather than asserting a call
    count and hoping: it makes the state really move between the two possible
    reads, and then requires the two outputs to agree anyway."""

    def _run_verb(self, tamper):
        """Drive `helm gate run --focus` with `tamper` fired the moment the
        FIRST bind returns. -> (rc, stdout, stderr, states). The wrapper calls
        the REAL bind both times, so a second call reads the tampered ledger
        exactly as a concurrent writer would have left it."""
        real_bind = gate.bind
        states = []

        def watching_bind(*args, **kwargs):
            result = real_bind(*args, **kwargs)
            states.append(result[0])
            if len(states) == 1:
                tamper()
            return result

        # THE VERB MINTS A REAL RECEIPT on the way through, so it needs the
        # guard's supervisor exactly as `mint_focused` does — and it fails
        # LATER and less legibly without one (the evidence line never prints
        # and the arm dies indexing an empty stdout), which is precisely the
        # unrunnable-looks-like-broken confusion this skip removes.
        require_supervisor()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "bind", watching_bind), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = gate._cmd_run(["--focus", "--repo", self.repo])
        return rc, out.getvalue(), err.getvalue(), states

    def test_the_printed_state_and_the_exit_code_cannot_disagree(self):
        """The receipt is minted and VERIFIED, and the ledger is then broken
        under the verb's feet. With one bind the verb reports one world: the
        evidence line prints, no refusal is printed, and rc is 0. With two,
        the second read finds the tampered ledger and rc becomes 1 while the
        printed line still says the run was fine."""
        def tamper():
            # An edit that leaves the row in place and stops it resolving —
            # the shape a torn or concurrently-rewritten ledger has. Counted
            # in the FILE, so this cannot be an append that added a row.
            path = gate.receipts_path()
            with open(path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(rows), 1)
            rows[0]["status"] = "FAILED"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rows[0]) + "\n")

        rc, out, err, states = self._run_verb(tamper)
        # THE DIVERGENCE IS REAL AND THIS PROVES IT FIRST: after the run, a
        # direct bind of the same evidence answers differently from the bind
        # the verb made. Without this control the arm below could pass on a
        # ledger nothing ever touched.
        line = out.strip().splitlines()[-1]
        after, _rid, _why = gate.bind(line, self.head, repo_id=self.repo,
                                      need=gate.NEED_FOCUSED)
        self.assertEqual(states[0], "VERIFIED", states)
        self.assertEqual(after, "REFUSED",
                         "the tamper did not change the answer, so this arm "
                         "cannot see a second read at all")
        # ONE READING, TWO OUTPUTS: the line printed and the code returned.
        # THE DIVERGENCE IS ASSERTED BEFORE THE CALL COUNT, deliberately —
        # the defect is that the two outputs describe different worlds, and a
        # kill that reports "bound twice" names the mechanism while a kill
        # that reports the disagreement names the harm.
        self.assertIn("gate:", line)
        self.assertNotIn("REFUSED", err)
        self.assertEqual(rc, 0,
                         "the verb printed a green evidence line (%s) and "
                         "exited %d — the paste a reviewer carries and the "
                         "code a script branches on describe different "
                         "worlds" % (line[:40], rc))
        self.assertEqual(len(states), 1,
                         "the verb bound %d times; a second call reads a "
                         "ledger the first one never saw" % len(states))

    def test_a_refusing_run_still_agrees_with_itself(self):
        """The opposite pole, so the cure is not "always return 0": when the
        ONE bind refuses, the refusal is printed AND the rc is 1."""
        real_bind = gate.bind

        def suite_need_bind(evidence, tip, **kwargs):
            # The verb asks the focused question for a focused mint; force
            # the suite question instead, which a focused receipt refuses.
            kwargs.pop("need", None)
            kwargs.pop("repo_id", None)
            return real_bind(evidence, tip, **kwargs)

        require_supervisor()           # a real focused mint runs first
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "bind", suite_need_bind), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = gate._cmd_run(["--focus", "--repo", self.repo])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err.getvalue())
        self.assertIn("FOCUSED", err.getvalue())
        # The evidence line still printed: a refusing run is not a silent one.
        self.assertIn("gate:", out.getvalue())


class FocusVerdictArms(FocusBase):
    """mark_verdict is the chokepoint: the polarity names the need."""

    def _dispatch(self, tip):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True,
                                  new_work=True)
        self.assertIsNone(err, err)
        return row

    def test_an_approve_citing_a_focused_receipt_is_refused(self):
        """Arm 1 at the chokepoint: APPROVE spends land authority, so it asks
        the suite question and the focused receipt refuses INSIDE bind — the
        immutable ledger never records the approve at all."""
        row = self.mint_focused()
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("gate evidence does not bind", err)
        self.assertIn("FOCUSED", err)
        events = dispatches.history(d["id"])
        self.assertFalse(any(e.get("event") == "verdict" for e in events))

    def test_a_fix_citing_a_focused_receipt_binds_it(self):
        """Arm 2 at the chokepoint: a cure-round verdict records the focused
        receipt as its bound gate — the cure round finally HAS a receipt
        instead of laundering one or dropping the token."""
        row = self.mint_focused()
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row) + " 2 blockers",
            "fix")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])
        self.assertEqual(dispatches.gate_state(got), "VERIFIED " + row["id"])
        # The EFFECT, not the absence of a complaint: the verdict event is on
        # the ledger and carries the binding.
        events = dispatches.history(d["id"])
        verdicts = [e for e in events if e.get("event") == "verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].get("gate"), row["id"])

    def test_an_approve_citing_a_whole_suite_receipt_still_works(self):
        """The must-hit control at the chokepoint: adding the focused kind
        must leave the whole-suite APPROVE path byte-for-byte the same
        transaction it already was."""
        row = self.mint_suite()
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])
        self.assertEqual(got["polarity"], "approve")
        events = dispatches.history(d["id"])
        verdicts = [e for e in events if e.get("event") == "verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].get("gate"), row["id"])

    def test_a_fix_citing_an_uncovering_focused_receipt_is_refused(self):
        """The coverage check reaches the chokepoint too: a cure-round
        verdict may not bind a receipt whose scope omitted the diff."""
        row = self.mint_focused()
        crafted = self.reresolve(
            row, focus=dict(row["focus"], changed=["pkg/beta.py"]))
        d = self._dispatch(self.head)
        with mock.patch.object(gate, "receipts",
                               return_value=([crafted], None, 0)):
            got, err = dispatches.mark_verdict(
                d["id"], self.head, gate.evidence_line(crafted), "fix")
        self.assertIsNone(got)
        self.assertIn("does not cover pkg/alpha.py", err)


class FocusLandDoorArms(FocusBase):
    """The two doors that read a receipt as land/fold authority DIRECTLY,
    without passing through bind: both must refuse the focused kind."""

    def test_landgate_refuses_a_focused_receipt(self):
        """The REAL path, not an injected one: the clause derives the landed
        tree from the tip and resolves the id through the store, so the
        refusal is the code's answer about a receipt that exists rather than
        about a dict this arm handed it."""
        row = self.mint_focused()
        state, why = landgate.gate_binds_tree(row["id"], None,
                                              repo=self.repo, tip=self.head)
        self.assertEqual(state, landgate.REFUSE, why)
        self.assertIn(row["id"], why)
        self.assertIn("FOCUSED", why)
        self.assertIn("WHOLE-SUITE", why)

    def test_landgate_still_accepts_a_whole_suite_receipt(self):
        """Must-hit control: the same clause, fed the strong kind, still
        reaches its tree comparison and passes on a match — and the detail
        NAMES the bound tree, so a pass that never compared anything cannot
        satisfy this arm."""
        row = self.mint_suite()
        state, why = landgate.gate_binds_tree(
            row["id"], row["tree"], gates={row["id"]: row},
            repo=self.repo, tip=self.head)
        self.assertEqual(state, landgate.OK, why)
        self.assertIn(row["tree"][:12], why)
        self.assertIn(row["id"], why)

    def test_foldcheck_refuses_a_focused_receipt(self):
        row = self.mint_focused()
        backend = vcs.backend(self.repo)
        rung = foldcheck._tree_matches_gate(
            backend, self.repo, self.head, "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.REFUSE)
        self.assertIn("whole-suite", rung.discriminator)

    def test_foldcheck_still_passes_a_whole_suite_receipt(self):
        """Must-hit control, and the discriminator must NAME what was
        compared — a PASS that measured nothing cannot fake the tree."""
        row = self.mint_suite()
        backend = vcs.backend(self.repo)
        rung = foldcheck._tree_matches_gate(
            backend, self.repo, self.head, "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.PASS, rung.discriminator)
        self.assertIn(row["tree"][:12], rung.discriminator)


class CounterfeitArms(FocusBase):
    """THE WAY THE DEFECT WAS FOUND, kept as the permanent probe: build the
    counterfeit BY HAND and feed it to the door. Reading the code found
    neither of the two forgeries this class descends from; a hand-assembled
    row did, twice. Every arm here constructs a v6 with plausible fields and
    a self-consistent content id WITHOUT any run — a self-computed hash
    proves nobody edited the row, never that anything happened — and asserts
    the door refuses it on a re-derived fact, not on its self-consistency.

    THE RECORDED BOUNDARY: an assembled row whose every derivable field
    matches the standing repository is indistinguishable from an honest mint
    by re-derivation alone — that residue is ORIGIN, the import door refuses
    it a journey, and provenance of the minting store owns the rest."""

    def _assemble(self, **focus_edits):
        """A no-run v6: honest derivable fields (the strongest counterfeit),
        then the caller's lies layered on top, then a fresh self-computed id
        so the row RESOLVES — the hash gate must not be what refuses it."""
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        # THE RAN SET IS ASSEMBLED TOO, and saying so is the point: a writer
        # who controls both ends writes the runner's answer as easily as the
        # planner's, so this field narrows the honest paths and does not
        # close the origin residue. The arms below are about what the
        # RE-DERIVED facts refuse, and a counterfeit that omitted this field
        # would be refused for the wrong reason.
        plan["executed"] = list(plan["selected"])
        plan["executed_ids"] = 2
        for key, value in focus_edits.items():
            plan[key] = value
        tree = self._git("rev-parse", "HEAD^{tree}")
        row = {"v": 6, "event": "gate", "ts": "2026-08-05T00:00:00Z",
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
        return row

    def _plant(self, row):
        """The hand append this row would need — straight into the ledger
        file, past every verb — so the arm proves bind itself refuses."""
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def _bind(self, row):
        return gate.bind("gate:" + row["id"], self.head, repo_id=self.repo,
                         need=gate.NEED_FOCUSED)

    def test_an_assembled_v6_is_refused_at_the_import_door(self):  # noqa: VACUOUS_ASSERTION — the tail is the unconditional positive control on the same two files: a v4 artifact through the same door writes both, and the receipt's id is read back out of the ledger
        row = self._assemble()
        artifact = os.path.join(self.tmp, "assembled.jsonl")
        with open(artifact, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        got, verdict, err = gateimport.import_receipt(artifact, self.repo)
        self.assertIsNone(verdict)
        self.assertIn("FOCUSED", err)
        # THE EFFECT, counted in the files: no receipt appended, and the
        # refusal happened before provenance — no half-import either.
        self.assertFalse(os.path.exists(gate.receipts_path()))
        self.assertFalse(os.path.exists(gateimport.imports_path()))
        # THE UNCONDITIONAL POSITIVE CONTROL on the same observables: the
        # same door, fed a whole-suite v4 artifact, WRITES both files — so
        # the emptiness above is the refusal, not a door that never writes.
        suite_row = self.mint_suite()
        os.remove(gate.receipts_path())     # leave only the artifact copy
        with open(artifact, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(suite_row) + "\n")
        _got, verdict, err = gateimport.import_receipt(artifact, self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(verdict, "imported")
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.read())["id"], suite_row["id"])
        self.assertTrue(os.path.exists(gateimport.imports_path()))

    def test_an_assembled_base_equal_to_tip_cannot_cover_by_emptiness(self):
        """The exact forgery shape: record base == tip, the re-diffed
        changed set is empty, and an empty scope 'covers' everything. The
        binder derives the merge-base itself and takes only that."""
        row = self._assemble(base=self.head, changed=[])
        self._plant(row)
        state, rid, why = self._bind(row)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, row["id"])
        self.assertIn("standing repository derives", why)

    def test_an_assembled_selection_omitting_a_consumer_is_refused(self):
        """The omitted-consumer probe: drop the transitive consumer from
        `selected` while every other field stays honest. The binder
        re-derives the closure at the tip and names what never ran."""
        row = self._assemble(selected=["tests.test_alpha"])
        self._plant(row)
        state, _rid, why = self._bind(row)
        self.assertEqual(state, "REFUSED")
        self.assertIn("never ran: tests.test_beta", why)

    def test_an_assembled_scope_wider_than_the_diff_is_refused(self):
        """Padding is the other direction of the same lie: a recorded change
        the reviewed diff does not contain was measured against some other
        tree (or nothing), and coverage claims about it are unbacked."""
        row = self._assemble(changed=["pkg/alpha.py", "pkg/ghost.py"])
        self._plant(row)
        state, _rid, why = self._bind(row)
        self.assertEqual(state, "REFUSED")
        self.assertIn("pkg/ghost.py", why)
        self.assertIn("does not contain", why)

    def test_an_assembled_universe_count_is_refused(self):
        row = self._assemble(universe=99)
        self._plant(row)
        state, _rid, why = self._bind(row)
        self.assertEqual(state, "REFUSED")
        self.assertIn("test universe", why)

    def test_a_scope_planned_under_another_policy_cannot_bind(self):
        """A recorded plan this binder's derivation cannot reproduce is a
        plan nobody can check — including an honest one minted by an older
        helm. Refused toward a re-run, never compared across grammars."""
        row = self._assemble(policy="changed+importers-v1")
        self._plant(row)
        state, _rid, why = self._bind(row)
        self.assertEqual(state, "REFUSED")
        self.assertIn("planned under policy", why)

    def test_an_unrun_receipt_binds_and_buys_no_authority(self):
        """THE RESIDUE, ASSERTED OPEN RATHER THAN PAPERED OVER, together
        with the reason it is allowed to stay open.

        `_assemble` runs no tests — it copies `focus_plan`'s own answer and
        chooses `ran`/`rc`/`status` itself — and this row DOES bind at
        NEED_FOCUSED. That cannot be measured away: mint and bind run under
        one uid against one ledger, so any marker the mint writes an
        assembler can write. The separation this seam buys is between
        HONEST paths; it does not defeat a writer who controls both ends.
        What makes the residue harmless is the CEILING, and this arm
        measures it: the same receipt, on the same tip, refuses the APPROVE
        that is the only verdict anything spends — so the cure-round verdict
        it does bind ends exactly where an untokened FIX ends, which the
        floor arm below re-measures rather than assumes.

        IF THIS ARM EVER GOES RED AT THE APPROVE LINE, the residue has
        become a real hole and the receipt must start proving its run."""
        row = self._assemble()
        self.assertEqual(row["ran"], 2)          # nothing ran to produce it
        self._plant(row)
        state, rid, why = self._bind(row)
        self.assertEqual(state, "VERIFIED", why)   # the open residue, named
        self.assertEqual(rid, row["id"])

        # THE CEILING: the land-authorizing verdict refuses it, before the
        # ledger, at the exact tip it was assembled for.
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        d, err = dispatches.add("reviewer", "unrun-lane", self.head,
                                kind="review", repo=self.repo, notify=False,
                                _reason=True, new_work=True)
        self.assertIsNone(err, err)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("needs the whole suite", err)
        # the route that works for a lane's reviewer: hold, and let the
        # approve bind the land gate's receipt (task/3039)
        self.assertIn("--source-clean", err)
        self.assertIn("land gate", err)
        self.assertNotIn("Run `helm gate run` on the reviewed tip", err)
        self.assertFalse(any(e.get("event") == "verdict"
                             for e in dispatches.history(d["id"])))
        # and neither land door will read it as authority either
        land_state, land_why = landgate.gate_binds_tree(
            row["id"], row["tree"], gates={row["id"]: row})
        self.assertEqual(land_state, landgate.REFUSE, land_why)

        # THE FLOOR it never rises above: the cure-round verdict this row
        # CAN bind is one an empty evidence line already gets. Measured on a
        # second row rather than asserted from the CLI help.
        d2, err = dispatches.add("reviewer", "untokened-lane", self.head,
                                 kind="review", repo=self.repo, notify=False,
                                 _reason=True, new_work=True)
        self.assertIsNone(err, err)
        bare, err = dispatches.mark_verdict(d2["id"], self.head,
                                            "2 blockers", "fix")
        self.assertIsNone(err, err)
        self.assertEqual(bare["gate"], "")
        self.assertEqual(bare["polarity"], "fix")

    def test_no_door_admitting_a_non_approve_verdict_reads_its_gate(self):
        """THE TRIPWIRE THE RESIDUE RESTS ON, measured instead of promised.

        The arm above leaves an unrun receipt binding at NEED_FOCUSED, and
        the whole reason that is harmless is that nothing SPENDS a
        non-approve binding. `_bind_focused` says so in prose and asks a
        future caller to notice; prose does not go red.

        The bound rests on ONE structural edge, and this arm re-derives it
        from `landreq`'s own source rather than from the design as recalled:
        `CONFIRMATION_POLARITIES` is the set that admits a non-approve
        verdict (SUPERSEDE) into a door that CLOSES work, and the
        `resolved` rung it feeds decides on polarity, a head-anchored
        resolution statement and cross-family eyes — never on the gate. Its
        sibling `subsumed` rung DOES read the gate, and refuses anything but
        approve one rung earlier. So: the two rungs are the two poles, and
        this arm is the pair.

        IF THE `resolved` RUNG EVER READS A GATE TOKEN, a receipt that ran
        nothing starts closing land requests and the residue is a real hole
        — that is the day to make a focused receipt prove its run."""
        import ast

        def reads_gate(fn):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) \
                        and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "get" and node.args \
                        and isinstance(node.args[0], ast.Constant) \
                        and node.args[0].value == "gate":
                    return True
                if isinstance(node, ast.Subscript) \
                        and isinstance(node.slice, ast.Constant) \
                        and node.slice.value == "gate":
                    return True
            return False

        # THE LEDGER'S SURFACE, NOT ONE FILE OF IT. Opening `landreq.__file__`
        # modelled a file, and the never-track ceiling forced the ledger to
        # split: both doors below still exist, are still called and are still
        # spelled `landreq.NAME`, but `_close_ladder_resolved` is no longer
        # TEXT in that one file. `ledger_sources` follows the same
        # `_OWNER_NAMES` declaration the retired-name rung reads, so this arm
        # keeps asking its real question across the next split too.
        doors = {}
        for _path, source in ledger_sources(landreq):
            doors.update({fn.name: fn for fn in ast.walk(ast.parse(source))
                          if isinstance(fn, ast.FunctionDef)})
        # The set itself, pinned: a third polarity admitted here would widen
        # what an unrun receipt can ride on without touching either door.
        self.assertEqual(landreq.CONFIRMATION_POLARITIES,
                         ("approve", "supersede"))
        self.assertIn("_close_ladder_resolved", doors)
        self.assertIn("_close_ladder_subsumed", doors)
        # THE BOUND: the rung that admits SUPERSEDE never reads a gate.
        self.assertFalse(reads_gate(doors["_close_ladder_resolved"]))
        # THE MUST-HIT: its approve-only sibling does — so the False above
        # is a fact about that door, not about this measurement being blind.
        self.assertTrue(reads_gate(doors["_close_ladder_subsumed"]))

    def test_the_honest_mint_still_binds_beside_every_counterfeit(self):
        """The must-hit inside the counterfeit class itself: the same doors,
        fed the genuinely-minted row, still say yes — so every refusal above
        is discrimination, not a binder that stopped saying yes."""
        row = self.mint_focused()
        state, _rid, why = self._bind(row)
        self.assertEqual(state, "VERIFIED", why)


class RenamedMechanismArms(FocusBase):
    """THE RENAME FAMILY, one arm per shape in which a renamed import
    mechanism can make the plan under-select. Each case plants a consumer
    whose ONLY link to the changed module is an unbounded dynamic import
    reached through a renamed callable, and fails when the plan comes back
    without it — the fail-OPEN direction, where a focused gate goes green
    having never run the test that would have caught the change.

    THE MECHANISM ARM IS THE UNIT, THE PLAN ARM IS THE DOOR: the table below
    reads `_module_reads`' own fail-closed flag so a shape that regresses
    names itself, and `test_a_renamed_loader_rides_the_plan` proves the flag
    reaches a real selection through `focus_plan`. Both, because a flag that
    never reaches the plan is a cure nobody spends."""

    DYN = "os.environ['M']"

    # (label, body of pkg/dyn.py, must-widen) — the last column is False for
    # the CONTROLS, whose whole job is to prove this table can say no. A
    # reader that answers True to everything has closed the hole by calling
    # every file a consumer of everything, which is the whole suite wearing
    # a focus block.
    SHAPES = (
        ("plain", "import importlib, os\ndef f():\n"
                  "    return importlib.import_module(%s)\n", True),
        ("module renamed", "import importlib as il, os\ndef f():\n"
                           "    return il.import_module(%s)\n", True),
        ("module rebound", "import importlib, os\nil = importlib\ndef f():\n"
                           "    return il.import_module(%s)\n", True),
        ("callable renamed",
         "from importlib import import_module as load\nimport os\ndef f():\n"
         "    return load(%s)\n", True),
        ("callable rebound",
         "import importlib, os\n_imp = importlib.import_module\ndef f():\n"
         "    return _imp(%s)\n", True),
        ("callable renamed to the module's own spelling",
         "from importlib import import_module as il\nimport os\ndef f():\n"
         "    return il(%s)\n", True),
        ("builtin", "import os\ndef f():\n    return __import__(%s)\n", True),
        ("builtin rebound",
         "import os\nbi = __import__\ndef f():\n    return bi(%s)\n", True),
        ("attribute rebound",
         "import importlib, os\nclass C:\n    def __init__(self):\n"
         "        self._imp = importlib.import_module\n    def f(self):\n"
         "        return self._imp(%s)\n", True),
        ("keyword target",
         "from importlib import import_module\nimport os\ndef f():\n"
         "    return import_module(name=%s)\n", True),
        ("no locatable target",
         "import importlib, os\ndef f(kw):\n"
         "    return importlib.import_module(**kw)\n", True),
        ("ambiguous rebind takes the wide reading",
         "import importlib, os\nf = importlib.import_module\nf = str\n"
         "def g():\n    return f(%s)\n", True),
        # THE RENAME MAP'S OWN BACKWARDS DIRECTION. Each of these three was
        # fail-CLOSED before the map existed and fail-OPEN after its first
        # cut, because resolving a callee REPLACED the spelled name: a
        # definite mechanism whose name is rebound to something merely
        # lazy-looking stopped being definite, so `consumes_all` went False
        # on a real `import_module(os.environ[...])`. Measured, not feared.
        ("NARROWING the mechanism name rebound to a lazy callable",
         "import os\nfrom importlib import import_module\n"
         "def _lazy_loader(x):\n    return x\n"
         "import_module = _lazy_loader\n"
         "def f():\n    return import_module(%s)\n", True),
        ("NARROWING a lazy-named import aliased ONTO the mechanism name",
         "import os\nfrom mystuff import lazy_thing as import_module\n"
         "def f():\n    return import_module(%s)\n", True),
        ("NARROWING __import__ rebound to a lazy-looking name",
         "import os\ndef lazy_pick(x):\n    return x\n"
         "__import__ = lazy_pick\n"
         "def f():\n    return __import__(%s)\n", True),
        ("CONTROL a gettext alias is not an import mechanism",
         "from django.utils.translation import gettext_lazy as _\n"
         "TITLE = _('pkg')\nLABEL = _('pkg.alpha')\n", False),
        ("CONTROL a constant through a rename stays bounded",
         "from importlib import import_module as load\ndef f():\n"
         "    return load('pkg.alpha')\n", False),
        ("CONTROL a stranger callable named load",
         "import os\nfrom pkg import alpha\nload = alpha.read\ndef f():\n"
         "    return load(%s)\n", False),
        ("CONTROL a foreign .lazy() does not poison the file",
         "import os\nfrom pkg import alpha\ndef f():\n"
         "    return alpha.lazy(%s)\n", False),
    )

    def test_every_renamed_mechanism_fails_closed(self):
        """The table, read at `_module_reads`' own boundary. A row that
        wants True and gets False is a SILENT UNDER-SELECT: err is None, so
        nothing downstream ever learns the reader could not see the
        import."""
        known = {"pkg", "pkg.alpha", "pkg.beta", "tests", "tests.test_alpha"}
        wrong = []
        for label, body, must_widen in self.SHAPES:
            text = body % self.DYN if "%s" in body else body
            deps, consumes_all, err = gate._module_reads(
                "pkg.dyn", False, text, label, known, {"pkg", "tests"})
            self.assertIsNone(err, "%s: %s" % (label, err))
            if consumes_all != must_widen:
                wrong.append("%s: wanted consumes_all=%s, got %s"
                             % (label, must_widen, consumes_all))
        self.assertEqual(wrong, [])

    def test_a_rename_map_can_only_widen_a_callee_never_narrow_it(self):
        """THE STRUCTURAL ARM, which is why the three NARROWING rows above
        are evidence rather than the whole guard: for every call site in
        every shape this class knows, the answer WITH the rename map must be
        at least as wide as the answer without it. A reader that substitutes
        the resolved name for the spelled one fails here on contact, whatever
        new spelling someone invents to reach it.

        The MUST-HIT keeps it from being vacuous in the other direction: at
        least one site must be widened BY the map, or a reader that simply
        ignored renames would pass this arm while failing the table."""
        import ast
        widened = narrowed = 0
        for label, body, _want in self.SHAPES:
            text = body % self.DYN if "%s" in body else body
            tree = ast.parse(text)
            aliases = gate._import_aliases(tree, [])
            rebinds = gate._import_rebinds(tree, aliases)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for reader in (gate._importish_callee, gate._strict_importish):
                    raw, wide = reader(node.func), reader(node.func, rebinds)
                    if raw and not wide:
                        narrowed += 1
                        self.fail("%s line %d: the rename map NARROWED %s "
                                  "from %s to %s"
                                  % (label, node.lineno, reader.__name__,
                                     raw, wide))
                    widened += bool(wide and not raw)
        self.assertEqual(narrowed, 0)
        self.assertGreater(widened, 0, "no site was widened by the rename "
                                       "map — this arm proved nothing")

    def test_a_gettext_alias_does_not_harvest_its_strings_as_modules(self):
        """The over-reach a rename map has whenever it admits the LAZY
        naming family instead of the DEFINITE mechanisms: `gettext_lazy`
        imported under a one-letter alias is an ordinary library idiom, and
        reading that alias as an import makes every translated string in the
        file a candidate module name.

        Both poles: the strings must not become edges, AND the file must not
        become a consumer of everything either — a reader that closed this by
        widening would satisfy the first assertion alone."""
        import ast
        known = {"pkg", "pkg.alpha", "pkg.beta"}
        gettext = ("from django.utils.translation import gettext_lazy as _\n"
                   "TITLE = _('pkg')\nLABEL = _('pkg.alpha')\n")
        deps, consumes_all, err = gate._module_reads(
            "pkg.i18n", False, gettext, "gettext", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertEqual(deps, set())
        self.assertFalse(consumes_all)

        # AT THE BOUND'S OWN BOUNDARY, because the assertions above pass on a
        # reader that carries `_` in the map and never consults it — measured
        # by sabotage, which is the only way a redundant defense announces
        # itself. `_import_rebinds` answers about DEFINITE mechanisms, so a
        # lazy-family alias must not be in the map at all.
        tree = ast.parse(gettext)
        aliases = gate._import_aliases(tree, [])
        self.assertEqual(aliases.get("_"),
                         "django.utils.translation.gettext_lazy")
        self.assertEqual(gate._import_rebinds(tree, aliases), {})
        # the must-hit: the same door, a definite mechanism, IS carried.
        real = ast.parse("from importlib import import_module as _\n")
        self.assertEqual(
            gate._import_rebinds(real, gate._import_aliases(real, [])),
            {"_": "import_module"})
        # THE MUST-HIT beside it: the identical file shape, with the alias
        # pointing at the real mechanism, DOES harvest the same strings — so
        # the emptiness above is about gettext, not about this reader being
        # blind to aliased constant imports.
        deps, consumes_all, err = gate._module_reads(
            "pkg.real", False,
            "from importlib import import_module as _\n"
            "TITLE = _('pkg')\nLABEL = _('pkg.alpha')\n",
            "aliased import", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertIn("pkg.alpha", deps)

    def test_a_keyword_passed_constant_stays_bounded(self):
        """`_import_target`'s `name=` arm, bound at the only place its loss
        SHOWS. Removing that arm was measured to change nothing about a
        keyword-passed DYNAMIC target — no locatable target and an
        unresolvable one both take the unbounded answer — so the shape table
        cannot see it. What it changes is the CONSTANT: `import_module(
        name='pkg.beta')` is perfectly bounded, and a reader that cannot
        locate the target calls the whole module a consumer of everything
        instead. That is the safe direction, which is exactly why nothing
        else here goes red on it, and why precision needs its own arm."""
        known = {"pkg", "pkg.alpha", "pkg.beta"}
        deps, consumes_all, err = gate._module_reads(
            "pkg.kw", False,
            "from importlib import import_module\n"
            "def f():\n    return import_module(name='pkg.beta')\n",
            "keyword constant", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertIn("pkg.beta", deps)
        self.assertFalse(consumes_all)
        # the must-hit beside it: the same keyword carrying something this
        # reader cannot resolve still widens, so `False` above is precision
        # rather than a reader that stopped looking at keywords.
        deps, consumes_all, err = gate._module_reads(
            "pkg.kw", False,
            "import os\nfrom importlib import import_module\n"
            "def f():\n    return import_module(name=os.environ['M'])\n",
            "keyword dynamic", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)

    def test_a_constant_reached_through_a_rename_is_still_an_edge(self):
        """The other half of the same defect: a renamed mechanism lost its
        CONSTANT edges too, so a consumer wired only through `load('pkg.x')`
        was invisible rather than over-selected."""
        known = {"pkg", "pkg.alpha", "pkg.beta"}
        for text in (
                "from importlib import import_module as load\n"
                "def f():\n    return load('pkg.beta')\n",
                "import importlib\n_imp = importlib.import_module\n"
                "def f():\n    return _imp('pkg.beta')\n"):
            deps, consumes_all, err = gate._module_reads(
                "pkg.dyn", False, text, "renamed constant", known,
                {"pkg"})
            self.assertIsNone(err, err)
            self.assertIn("pkg.beta", deps)
            # AND it stayed BOUNDED — an edge found by widening everything
            # would satisfy the line above while proving nothing.
            self.assertFalse(consumes_all)

    def test_a_c_snippet_reads_its_own_renamed_loader(self):
        """The `-c` child's code is a module of its own and gets the same
        rename map; before it did, an inline snippet's renamed loader read
        as an ordinary call and its edge vanished."""
        local = lambda dotted: {                                # noqa: E731
            ".".join(str(dotted).split(".")[:i + 1])
            for i in range(len(str(dotted).split(".")))
            if ".".join(str(dotted).split(".")[:i + 1])
            in {"pkg", "pkg.alpha", "pkg.beta"}}
        deps, readable, bounded = gate._snippet_edges(
            "from importlib import import_module as L\nL('pkg.beta')\n",
            local)
        self.assertTrue(readable)
        self.assertTrue(bounded)
        self.assertIn("pkg.beta", deps)
        # the must-miss beside it: a snippet naming nothing local yields
        # nothing, so the assertion above is not matching everything.
        self.assertEqual(gate._snippet_edges("import json\n", local)[0], set())
        # and the SUBMODULE in the alias position, which `_module_reads` has
        # always resolved for the repo's own files: without it a snippet
        # reaching pkg.beta by `from pkg import beta` came back readable,
        # bounded, and missing the edge — a confident partial by the other
        # road. The must-miss beside it: a plain attribute stays out.
        self.assertIn("pkg.beta",
                      gate._snippet_edges("from pkg import beta\n", local)[0])
        self.assertNotIn(
            "pkg.beta",
            gate._snippet_edges("from pkg import alpha\n", local)[0])

    # The `-c` shapes, each at the stage its own defense lives on. `bounded`
    # is what the snippet reader may claim about ITS OWN edge set; the parent
    # column is what the spawning module must end up saying.
    SNIPPETS = (
        ("a rename the reader cannot resolve",
         "from importlib import import_module as load\n"
         "import os, pkg.alpha\nload(os.environ['M'])\n", False),
        ("the raw mechanism with a dynamic target",
         "import importlib, sys, pkg.alpha\n"
         "importlib.import_module(sys.argv[1])\n", False),
        ("the target arrives by keyword",
         "import os, pkg.alpha\n__import__(name=os.environ['M'])\n", False),
        ("the target cannot even be LOCATED",
         "from importlib import import_module\nimport pkg.alpha\n"
         "kw = {}\nimport_module(**kw)\n", False),
        ("a name rebound to the mechanism by assignment",
         "import os, pkg.alpha\nload = __import__\n"
         "load(os.environ['M'])\n", False),
        ("CONTROL a constant through a rename is bounded",
         "from importlib import import_module as load\n"
         "import pkg.alpha\nload('pkg.beta')\n", True),
        ("CONTROL a foreign .lazy() does not unbound a child",
         "import os\nfrom pkg import alpha\nalpha.lazy(os.environ['M'])\n",
         True),
        ("CONTROL a plain import-only snippet",
         "import pkg.alpha\nfrom pkg import beta\n", True),
    )

    def test_a_c_snippet_may_not_come_back_readable_and_partial(self):  # noqa: VACUOUS_ASSERTION — the two counts below are the unconditional positive controls on the same observable: `bounded` must have come back False 5 times and True 3 times, so an empty `wrong` cannot be an empty table or a reader that stopped answering
        """THE FORBIDDEN OUTCOME, at the snippet reader's own boundary.

        `readable=True` beside a populated deps set is this reader ASSERTING
        it resolved the child. Under-selection with a STATED bound is a limit
        a caller can act on; this one has no surface to be stated on, because
        nothing downstream can tell it from a complete read. So every shape
        whose import cannot be NAMED must come back either unreadable or
        unbounded — never readable-and-bounded with edges beside it."""
        local = self._local({"pkg", "pkg.alpha", "pkg.beta"})
        wrong, unbounded, bounded_rows = [], 0, 0
        for label, code, want_bounded in self.SNIPPETS:
            deps, readable, bounded = gate._snippet_edges(code, local)
            self.assertTrue(readable, label)
            unbounded += not bounded
            bounded_rows += bool(bounded)
            if bounded != want_bounded:
                wrong.append("%s: wanted bounded=%s, got %s"
                             % (label, want_bounded, bounded))
            if not bounded and not deps:
                wrong.append("%s: no edges beside it — this row cannot "
                             "witness a CONFIDENT PARTIAL" % label)
        self.assertEqual(wrong, [])
        # THE POSITIVE CONTROLS on the same observable, because `wrong == []`
        # is equally true of a reader that answered `bounded` at random and
        # of a table that stopped discriminating: both poles must be present
        # and this arm must have READ them, not merely found no complaint.
        self.assertEqual(unbounded, 5)
        self.assertEqual(bounded_rows, 3)
        # the other pole: unparseable is still UNREADABLE, not unbounded-
        # readable, so the two answers stay distinguishable.
        self.assertEqual(gate._snippet_edges("not python ((( \n", local),
                         (set(), False, False))

    def _local(self, known):
        return lambda dotted: {                                 # noqa: E731
            ".".join(str(dotted).split(".")[:i + 1])
            for i in range(len(str(dotted).split(".")))
            if ".".join(str(dotted).split(".")[:i + 1]) in known}

    def _spawner(self, code, tail=""):
        return ("import subprocess, sys\n"
                "def go():\n"
                "    subprocess.run([sys.executable, '-c', %r%s])\n"
                % (code, tail))

    def test_an_unnameable_import_in_a_c_program_widens_its_spawner(self):  # noqa: VACUOUS_ASSERTION — same shape: `widened`/`held` are asserted unconditionally at 5 and 3, so an empty `wrong` cannot be a spawner reader stuck at either pole
        """THE PARENT, which is where the harm lands: a spawning module whose
        `-c` child imports something this reader cannot name is a CONSUMER OF
        EVERYTHING, exactly as a module holding the same call directly would
        be. err stays None either way, so a row that fails here fails
        SILENTLY — the plan comes back confident and too small."""
        known = {"pkg", "pkg.alpha", "pkg.beta", "tests", "tests.test_alpha"}
        wrong, widened, held = [], 0, 0
        for label, code, bounded in self.SNIPPETS:
            deps, consumes_all, err = gate._module_reads(
                "pkg.spawner", False, self._spawner(code), label, known,
                {"pkg", "tests"})
            self.assertIsNone(err, "%s: %s" % (label, err))
            widened += bool(consumes_all)
            held += not consumes_all
            if consumes_all == bounded:
                wrong.append("%s: bounded=%s but consumes_all=%s"
                             % (label, bounded, consumes_all))
        self.assertEqual(wrong, [])
        # both poles present, unconditionally: a spawner reader stuck at
        # "consumer of everything" and one stuck at "consumes nothing" each
        # satisfy an empty `wrong` on half a table.
        self.assertEqual(widened, 5)
        self.assertEqual(held, 3)

    def test_a_proven_sibling_argument_cannot_swallow_the_c_program(self):
        """The placement arm. `proven` means "some element named a real
        target, so the rest is the runner's data" — true of a trailing test
        id, false of `-c`, whose argument IS the child program. With the
        widening filtered through `proven`, one resolvable trailing path was
        measured to swallow an unbounded loader whole."""
        known = {"pkg", "pkg.alpha", "pkg.beta"}
        unbounded = self.SNIPPETS[0][1]
        deps, consumes_all, err = gate._module_reads(
            "pkg.spawner", False,
            self._spawner(unbounded, ", 'pkg/alpha.py'"), "proven sibling",
            known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)
        # THE MUST-MISS beside it: the same trailing path with a BOUNDED
        # snippet stays a non-consumer, so the True above is the snippet's
        # doing and not the trailing path's.
        deps, consumes_all, err = gate._module_reads(
            "pkg.spawner", False,
            self._spawner(self.SNIPPETS[5][1], ", 'pkg/alpha.py'"),
            "bounded sibling", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertFalse(consumes_all)
        self.assertIn("pkg.beta", deps)

    def test_a_c_program_is_read_whole_and_not_as_shell_words(self):
        """The tokeniser shreds every whitespace-bearing argv element into
        shell words, and a real snippet is nothing BUT whitespace-bearing —
        read as words, a `-c` argument would reach the snippet reader only
        as its first word (`from`, here), its remaining words riding loose
        in the stream where any one that names a module marks the child
        proven off token luck. Shredding follows from containing
        whitespace, so without a whole read that is EVERY constant python
        `-c` snippet, not an unlucky subset. The pre-pass over the
        UNSHREDDED elements is the reader that sees the snippet whole; this
        arm holds it to that.

        A RENAMED CONSTANT is the discriminator: read as words, the edge
        lives inside the single token `load('pkg.beta')`, which resolves to
        nothing at all — only a whole read produces pkg.beta while leaving
        the child bounded."""
        known = {"pkg", "pkg.alpha", "pkg.beta"}
        deps, consumes_all, err = gate._module_reads(
            "pkg.spawner", False,
            self._spawner("from importlib import import_module as load\n"
                          "load('pkg.beta')\n", ", 'pkg/alpha.py'"),
            "whole read", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertIn("pkg.beta", deps)
        self.assertFalse(consumes_all)
        # and the unbounded twin, whose rename ALSO only exists whole: read
        # as words, `load(os.environ['M'])` is never even seen.
        deps, consumes_all, err = gate._module_reads(
            "pkg.spawner", False,
            self._spawner("from importlib import import_module as load\n"
                          "import os\nload(os.environ['M'])\n",
                          ", 'pkg/alpha.py'"),
            "whole read, unbounded", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)

    def test_a_shell_string_c_program_is_bounded_by_the_same_rule(self):
        """The OTHER road to the same door, found by sabotage rather than by
        reading: when the command arrives as ONE shell string, the `-c` flag
        lives inside it, so the whole-read pre-pass never sees a `-c` element
        at all and the tokeniser's reading is the only reading there is. A
        snippet that survives tokenising intact — no whitespace in it — then
        reached the snippet reader, came back readable, and marked the child
        PROVEN with its unbounded loader unread.

        This is the arm that keeps the bounded check on the in-loop branch
        alive: with the pre-pass in place, every list-form spawn passes
        whether or not that branch checks anything. And because shredding
        is what this road DOES — the defect is whitespace — the arm must
        also carry the whitespace-BEARING shapes: a payload split to words
        has no whole reading here, so its failure is the child's opacity,
        and no loose sibling word that happens to name a module may narrow
        it back."""
        known = {"pkg", "pkg.alpha", "pkg.beta"}
        shell = ("import subprocess\ndef go():\n"
                 "    subprocess.run(%r, shell=True)\n")
        deps, consumes_all, err = gate._module_reads(
            "pkg.sh", False,
            shell % "python3 -c __import__(os.environ['M'])",
            "shell -c unbounded", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)
        # the must-miss on the same road: a CONSTANT target through the same
        # shell string stays bounded AND yields its edge, so the True above
        # is the unbounded loader and not the shell form itself.
        deps, consumes_all, err = gate._module_reads(
            "pkg.sh", False,
            shell % "python3 -c __import__('pkg.beta')",
            "shell -c bounded", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertFalse(consumes_all)
        self.assertIn("pkg.beta", deps)
        # THE WHITESPACE-BEARING SHAPES, the defect's own: an unbounded
        # loader with a trailing argument that names a real module. On the
        # list-form road the pre-pass keeps `proven` siblings from
        # swallowing an unbounded `-c`; this road has no pre-pass, so the
        # in-loop branch itself must hold the same line.
        deps, consumes_all, err = gate._module_reads(
            "pkg.sh", False,
            shell % "python3 -c __import__(os.environ['M']) pkg.beta",
            "shell -c unbounded, lucky argument", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)
        # and a payload shredded MID-SNIPPET: the fragment reaching the
        # reader is `import`, which does not parse, while `pkg.beta` rides
        # loose beside it. The reader cannot know the shell's quoting, so
        # a fragmentary payload is unreadable-whole and the child stays
        # opaque — token luck must not narrow it.
        deps, consumes_all, err = gate._module_reads(
            "pkg.sh", False,
            shell % "python3 -c import pkg.beta",
            "shell -c shredded, lucky fragment", known, {"pkg"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)

    def test_a_foreign_c_flag_keeps_its_exemption(self):
        """`-c` is a FOREIGN flag on foreign programs too, and `git -c
        user.email=t@t` is the common one: a whole-snippet reader keyed on
        the flag alone would claim every such argument as a python program.
        Some parse as python by accident and some do not, and neither may
        change the answer — an unparseable one must not make its test module
        a consumer of everything either. The foreign-binary exemption
        `_spawn_reads` documents is unchanged by any of this."""
        known = {"pkg", "pkg.alpha", "tests", "tests.test_git"}
        body = ("import subprocess\ndef go():\n"
                "    subprocess.run([%r, '-c', %r, 'commit'])\n")
        for snippet in ("user.email=t@t", "make test && ./run.sh"):
            deps, consumes_all, err = gate._module_reads(
                "tests.test_git", False, body % ("git", snippet),
                "foreign -c", known, {"pkg", "tests"})
            self.assertIsNone(err, err)
            self.assertFalse(consumes_all, snippet)
        # THE POSITIVE CONTROL on the same observable and the SAME TEXT: the
        # shell line above is not python, and swapping ONLY the program makes
        # the identical string widen. Without it the assertions above pass
        # just as well on a reader that widens on nothing. (`user.email=t@t`
        # cannot carry this control — it happens to BE valid python with no
        # import in it, so it is bounded under either program.)
        deps, consumes_all, err = gate._module_reads(
            "tests.test_git", False,
            body % ("python3", "make test && ./run.sh"),
            "python -c", known, {"pkg", "tests"})
        self.assertIsNone(err, err)
        self.assertTrue(consumes_all)

    def test_a_renamed_loader_rides_the_plan(self):
        """THE DOOR, not the unit: the same rename inside a real repo, with
        the consumer landed on MAIN so the selection cannot be the
        changed-test shortcut. Selection too small = red."""
        self._git("checkout", "-q", "main")
        self._write("pkg/dyn.py",
                    "from importlib import import_module as load\n"
                    "import os\n"
                    "def read():\n"
                    "    return load(os.environ['M'])\n")
        self._write("tests/test_dyn.py",
                    "import unittest\nfrom pkg import dyn\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_d(self):\n"
                    "        self.assertTrue(dyn)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "renamed unbounded importer on main")
        self._git("checkout", "-q", "-b", "lane/alpha-rename")
        self._write("pkg/alpha.py", "X = 1\nZ = 5\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        self.assertIn("tests.test_dyn", plan["selected"])
        # the must-miss: a bounded stranger still stays out, so this plan is
        # discriminating rather than selecting the universe.
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_a_keyword_fed_dynamic_import_rides_the_plan(self):
        """The same door for the other measured shape: the target arrives as
        `name=`, which the reader used to skip entirely."""
        self._git("checkout", "-q", "main")
        self._write("pkg/kw.py",
                    "import importlib\nimport os\n"
                    "def read():\n"
                    "    return importlib.import_module("
                    "name=os.environ['M'])\n")
        self._write("tests/test_kw.py",
                    "import unittest\nfrom pkg import kw\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_k(self):\n"
                    "        self.assertTrue(kw)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "keyword-fed importer on main")
        self._git("checkout", "-q", "-b", "lane/alpha-kw")
        self._write("pkg/alpha.py", "X = 1\nZ = 6\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertIn("tests.test_kw", plan["selected"])
        self.assertNotIn("tests.test_gamma", plan["selected"])


class PlannerFailClosedArms(FocusBase):
    """The under-selection classes: each arm plants a consumer the import
    statements cannot see (or a history/namespace the diff cannot anchor)
    and asserts the planner either SELECTS it or REFUSES the plan — never
    err=null with the consumer omitted."""

    def test_a_dynamic_import_nothing_can_bound_rides_every_plan(self):
        """`import_module(n)` where n is a bare parameter with no constant
        call sites: the one known thing is that nothing about its targets
        is, so its module is a consumer of everything and its test rides
        every plan — while the stranger still stays out. The importer lands
        on MAIN and the lane changes only alpha, so the selection cannot be
        the changed-test shortcut."""
        self._git("checkout", "-q", "main")
        self._write("pkg/dyn.py",
                    "import importlib\n"
                    "def load(n):\n"
                    "    return importlib.import_module(n)\n")
        self._write("tests/test_dyn.py",
                    "import unittest\nfrom pkg import dyn\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_d(self):\n"
                    "        self.assertTrue(dyn)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "unbounded dynamic importer on main")
        self._git("checkout", "-q", "-b", "lane/alpha3")
        self._write("pkg/alpha.py", "X = 1\nZ = 4\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        self.assertIn("tests.test_dyn", plan["selected"])
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_an_unnameable_import_inside_a_c_child_rides_the_plan(self):
        """THE DOOR for the `-c` shape: the unbounded loader is not in this
        repo's own source at all, it is in a string handed to a child. The
        spawner lands on MAIN so the selection cannot be the changed-test
        shortcut, and the lane changes only alpha — so a plan that omits
        tests.test_spawn is a plan that went green without running the tests
        that could have caught the change.

        The snippet is a ONE-LINER on purpose. The argv tokeniser shreds any
        whitespace-bearing element into shell words, and a shredded snippet
        already fails closed by accident (its first word does not parse); a
        one-liner is the shape that reaches the snippet reader WHOLE, which
        is the only shape that can witness this cure rather than the
        tokeniser's pre-existing clumsiness."""
        self._git("checkout", "-q", "main")
        self._write("pkg/spawn.py",
                    "import subprocess\nimport sys\n"
                    "CODE = ('__import__(\"importlib\").import_module('\n"
                    "        '__import__(\"os\").environ[\"M\"])')\n"
                    "def run():\n"
                    "    return subprocess.run("
                    "[sys.executable, '-c', CODE])\n")
        self._write("tests/test_spawn.py",
                    "import unittest\nfrom pkg import spawn\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_s(self):\n"
                    "        self.assertTrue(spawn)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "unbounded loader inside a -c child")
        self._git("checkout", "-q", "-b", "lane/alpha-snippet")
        self._write("pkg/alpha.py", "X = 1\nZ = 7\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        self.assertIn("tests.test_spawn", plan["selected"])
        # the must-miss: a bounded stranger stays out, so this plan is
        # discriminating rather than selecting its whole universe.
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_a_bounded_c_child_does_not_drag_its_spawner_in(self):
        """The other pole of the arm above, and the reason it is evidence:
        the identical spawn whose snippet imports a CONSTANT is bounded, so
        it rides only when its real target moves. A cure that widened on
        every `-c` would pass the arm above and fail here.

        The constant names pkg.delta, planted here with NO imports of its
        own: pointing it at pkg.beta proves nothing, because beta consumes
        alpha and the spawner would ride an alpha lane for a reason that has
        nothing to do with this bound."""
        self._git("checkout", "-q", "main")
        self._write("pkg/delta.py", "D = 1\n")
        self._write("pkg/spawn2.py",
                    "import subprocess\nimport sys\n"
                    "CODE = 'load=__import__;load(\"pkg.delta\")'\n"
                    "def run():\n"
                    "    return subprocess.run("
                    "[sys.executable, '-c', CODE])\n")
        self._write("tests/test_spawn2.py",
                    "import unittest\nfrom pkg import spawn2\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_s(self):\n"
                    "        self.assertTrue(spawn2)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "bounded loader inside a -c child")
        self._git("checkout", "-q", "-b", "lane/alpha-snippet2")
        self._write("pkg/alpha.py", "X = 1\nZ = 8\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertNotIn("tests.test_spawn2", plan["selected"])
        # THE MUST-HIT beside it: move the snippet's own constant target and
        # the same spawner IS selected, so the exclusion above is the bound
        # working rather than the `-c` edge being invisible.
        self._git("checkout", "-q", "main")
        self._git("checkout", "-q", "-b", "lane/delta-snippet2")
        self._write("pkg/delta.py", "D = 1\nE = 2\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "delta only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertIn("tests.test_spawn2", plan["selected"])

    def test_a_parameter_fed_import_resolves_through_its_call_sites(self):
        """The bounded shape: every same-file call site passes a constant,
        so the domain is exactly those constants — the consumer is selected
        when its target changes and NOT dragged in when a stranger does.
        The hub lands on MAIN first, so neither lane carries it in its own
        changed set and the selection can only come from the domain."""
        self._git("checkout", "-q", "main")
        self._write("pkg/hub2.py",
                    "import importlib\n"
                    "def _load(name):\n"
                    "    return importlib.import_module('pkg.' + name)\n"
                    "def touch():\n"
                    "    return _load('alpha')\n")
        self._write("tests/test_hub2.py",
                    "import unittest\nfrom pkg import hub2\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_h(self):\n"
                    "        self.assertTrue(hub2)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "bounded dynamic importer on main")
        self._git("checkout", "-q", "-b", "lane/alpha2")
        self._write("pkg/alpha.py", "X = 1\nZ = 3\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        # alpha is inside the call sites' constant domain: hub2 consumes it.
        self.assertIn("tests.test_hub2", plan["selected"])
        # the DISCRIMINATION: a beta-only lane must not drag hub2 in,
        # because beta is outside that domain.
        self._git("checkout", "-q", "main")
        self._git("checkout", "-q", "-b", "lane/beta-only")
        self._write("pkg/beta.py", "from . import alpha\nY = alpha.X + 1\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "beta only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/beta.py"])
        self.assertIn("tests.test_beta", plan["selected"])
        self.assertNotIn("tests.test_hub2", plan["selected"])

    def test_a_shell_consumer_with_a_constant_target_is_selected(self):
        """A test that reaches the changed module only as a CHILD PROCESS —
        no import statement anywhere — must still be selected when the
        target is readable (`-m pkg.alpha`). Planted on MAIN; the lane
        changes only alpha, so selection can only come through the spawn
        edge."""
        self._git("checkout", "-q", "main")
        self._write("tests/test_shell.py",
                    "import subprocess, sys, unittest\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_s(self):\n"
                    "        p = subprocess.run(\n"
                    "            [sys.executable, '-m', 'pkg.alpha'],\n"
                    "            capture_output=True)\n"
                    "        self.assertEqual(p.returncode, 0)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "shell consumer on main")
        self._git("checkout", "-q", "-b", "lane/alpha4")
        self._write("pkg/alpha.py", "X = 1\nZ = 6\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        self.assertIn("tests.test_shell", plan["selected"])
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_an_opaque_shell_test_rides_every_plan(self):
        """A test spawning a child nobody can name might be running ANY repo
        code — so it rides, and the stranger that spawns nothing does not.
        Planted on MAIN; the lane changes only alpha."""
        self._git("checkout", "-q", "main")
        self._write("tests/test_shellvar.py",
                    "import os, subprocess, sys, unittest\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_s(self):\n"
                    "        cmd = [os.environ.get('PY', sys.executable),\n"
                    "               os.environ['TARGET']]\n"
                    "        self.assertIsNotNone(\n"
                    "            subprocess.run(cmd, capture_output=True))\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "opaque shell consumer on main")
        self._git("checkout", "-q", "-b", "lane/alpha5")
        self._write("pkg/alpha.py", "X = 1\nZ = 7\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha only")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        self.assertIn("tests.test_shellvar", plan["selected"])
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_a_committed_module_shaped_symlink_refuses(self):
        """`Pkg -> pkg` puts one file under two import names; a selection
        computed over either under-counts the other, so the namespace
        refuses as a whole — this is the alias that was measured slipping
        through with err=null. The alias lands on MAIN, outside the lane's
        own changed set, so only the namespace check can refuse it."""
        self._git("checkout", "-q", "main")
        os.symlink("pkg", os.path.join(self.repo, "Pkg"))
        self._git("add", "-A")
        self._git("commit", "-qm", "alias on main")
        self._git("checkout", "-q", "-b", "lane/alias")
        self._write("pkg/alpha.py", "X = 1\nZ = 5\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "alpha beside the alias")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("symlink", err)
        self.assertIn("Pkg", err)

    def test_an_untracked_module_shaped_symlink_refuses_too(self):
        os.symlink("pkg", os.path.join(self.repo, "Pkg"))
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("Pkg", err)

    def test_two_merge_bases_refuse(self):
        """A criss-cross history has SEVERAL merge-bases and plain
        `merge-base` silently picks one — a changed-set diffed from a
        chosen base is a chosen scope. `--all` sees them; two refuse."""
        g = self._git
        g("checkout", "-q", "main")
        g("checkout", "-q", "-b", "c1")
        self._write("f1.py", "A = 1\n")
        g("add", "-A")
        g("commit", "-qm", "c1")
        g("checkout", "-q", "main")
        g("checkout", "-q", "-b", "c2")
        self._write("f2.py", "B = 1\n")
        g("add", "-A")
        g("commit", "-qm", "c2")
        g("checkout", "-q", "-b", "p", "c1")
        g("merge", "-q", "--no-ff", "c2", "-m", "P")
        g("checkout", "-q", "-b", "q", "c2")
        g("merge", "-q", "--no-ff", "c1", "-m", "Q")
        g("branch", "-f", "main", "p")
        g("checkout", "-q", "-b", "lane/cc", "q")
        self._write("pkg/alpha.py", "X = 1\nZ = 9\n")
        g("add", "-A")
        g("commit", "-qm", "lane on the criss-cross")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(plan)
        self.assertIn("merge-bases", err)
        self.assertIn("Rebase onto a single base", err)
        self.assertIn(gate._SUITE_ROUTE, err)


class PlannerRefusalRouteArms(unittest.TestCase):
    """task/3039: a lane room of helm's own tree refuses a whole suite, so a
    planner refusal may not end at "run the whole suite". The one sentence
    every such refusal ends with names each room's move, and the gate verb's
    help says the same."""

    def test_the_route_names_the_lane_moves_and_the_land_gate(self):
        route = gate._SUITE_ROUTE
        self.assertIn("helm gate audits -- <your test modules>", route)
        self.assertIn("helm gate run --lane-suite --why TEXT", route)
        self.assertIn("land gate", route)
        self.assertIn("integrator's train", route)
        # the rooms where a whole suite does run keep their verb
        self.assertIn("Anywhere else, `helm gate run`", route)

    def test_the_gate_help_names_the_same_route(self):
        from helm import cli
        text = cli._VERB_HELP["gate"]
        self.assertIn("names the move that works in the reader's room", text)
        self.assertIn("(`helm gate audits`) or takes the `--lane-suite --why` "
                      "escape", text)
        self.assertNotIn("refuse toward the whole suite", text)


class FrozenV4Control(unittest.TestCase):
    """The version bump must not move a single pre-existing id.

    The row below was hashed by the PRE-v6 `_receipt_id` and its id frozen
    into this file as bytes; it is never recomputed through the code under
    test at fixture-build time. If the v6 branch (or the widened membership
    tuples) perturbs v4 hashing in any way, this stops matching — and every
    receipt in the live ledger stops resolving with it."""

    FROZEN = {"v": 4, "event": "gate", "ts": "2026-08-04T04:00:28Z",
              "repo_id": "/fab/wt/frozen-fixture",
              "head": "897f8169c1a87d3c6fd5fcdb7806ce477814bb4f",
              "tree": "282733df1083c639f279dbbe04de039d2dcf7f85",
              "dirty": False,
              "head_after": "897f8169c1a87d3c6fd5fcdb7806ce477814bb4f",
              "tree_after": "282733df1083c639f279dbbe04de039d2dcf7f85",
              "dirty_after": False,
              "interpreter": {"name": "cpython", "version": "3.14.6",
                              "language": "3.14.6",
                              "executable": "/opt/py/bin/python3.14"},
              "host": {"node": "snoozy-am5", "system": "Linux",
                       "release": "6.17.0-40-generic",
                       "id": "a33110f5fee74a15"},
              "argv": ["/opt/py/bin/python3.14", "-m", "unittest", "discover",
                       "-s", "tests", "-t", "."],
              "suite": True, "label": "frozen v4 control", "rc": 0,
              "wall": 197.69, "status": "OK", "ran": 7410, "skipped": 14,
              "detail": "skipped=14", "elapsed": 196.539, "failures": [],
              "failures_unreadable": False, "base_check": None}

    def test_the_frozen_v4_id_still_recomputes(self):
        self.assertEqual(gate._receipt_id(self.FROZEN), "7413b94d41026426")


class FocusIsGuardedTest(FocusBase):
    """A FOCUSED run is contained and gets _suite_env's ADDED keys.

    FocusBase already builds the isolated changed repo and temp HELM_HOME
    this class needs, so the containment assertion costs no new fixture —
    the prior-art was in place before the arm that binds it.

    Focus used to take a bare subprocess.run, which is why the cure-round
    loop executed test code with no cgroup while whole-suite runs were
    contained.
    """

    def test_a_focused_run_is_guarded_and_gets_suite_env(self):
        witness = os.path.join(self.tmp, "focus-witness")
        self._write("tests/test_focus_witness.py", textwrap.dedent("""
            import os
            import unittest
            class Witness(unittest.TestCase):
                def test_records(self):
                    with open("/proc/self/cgroup") as fh:
                        cg = fh.read().strip()
                    with open(%r, "w") as out:
                        out.write("%%s\\n%%s" %% (
                            cg, os.environ.get("HELM_GATE_SUITE_CAP",
                                               "ABSENT")))
                    self.assertTrue(True)
        """) % witness)
        self.mint_focused()
        self.assertTrue(os.path.exists(witness),
                        "the focused run never executed the witness; "
                        "arm is vacuous")
        with open(witness, encoding="utf-8") as fh:
            cgroup, cap = fh.read().split("\n", 1)
        self.assertIn("helm-gate-", cgroup,
                      "a FOCUSED run executed outside a gate cgroup: %s"
                      % cgroup)
        self.assertNotEqual("ABSENT", cap.strip(),
                            "a focused run did not receive _suite_env's "
                            "added keys")


class TheFocusFixtureRestoresTheEnvironmentItInherited(unittest.TestCase):
    """R4 (P3). ONE environment-restoration owner in FocusBase, in a fixed order.

    The cure GateBase took never reached this fixture, and the second round
    measured it: `FocusBase.setUp` popped every ENV_KEY and THEN called
    `_tmphome.helm_tree`, so `own_env` captured the override as ABSENT; tearDown
    put the incoming value back, and `own_env`'s cleanup — which unittest runs
    AFTER tearDown — popped it again. The restoration is unconditional by
    design (a guard that only undoes "what is still ours" cannot tell the value
    it set from the one the process arrived with), so the order is the only
    thing that can own the answer, and every incoming value was lost the same
    way: "1", "0" and any other string alike.

    THE PROBE IS THE SHIPPED FIXTURE, driven through the real unittest
    lifecycle. Nothing about ordering is modelled, because the ordering IS the
    defect — a hand-rolled stand-in would prove the stand-in.
    """

    KEY = "HELM_CROSS_TREE_GATE"

    def setUp(self):
        prior = os.environ.get(self.KEY)

        def _restore():
            if prior is None:
                os.environ.pop(self.KEY, None)
            else:
                os.environ[self.KEY] = prior

        self.addCleanup(_restore)

    def _after_one_real_focus_case(self, incoming):
        class Probe(FocusBase):
            def runTest(inner):
                # The fixture's own setUp is the subject: `helm_tree` sets the
                # override, so it is "1" while a test body runs whatever the
                # process came in with.
                inner.assertEqual(os.environ.get("HELM_CROSS_TREE_GATE"), "1")

        if incoming is None:
            os.environ.pop(self.KEY, None)
        else:
            os.environ[self.KEY] = incoming
        result = Probe().run()
        self.assertTrue(result.wasSuccessful(),
                        [result.errors, result.failures])
        return os.environ.get(self.KEY)

    def test_an_incoming_cross_tree_override_survives_the_focus_fixture(self):
        """The defect, measured: before the cure this returned None."""
        self.assertEqual(self._after_one_real_focus_case("1"), "1")

    def test_an_incoming_value_other_than_one_survives_the_focus_fixture(self):
        """The half a "put 1 back" cure would have missed. `0` is a real
        incoming value — the explicit off — and an unconditional pop lost it
        exactly like it lost `1`, which is why the arm above alone could have
        been satisfied by restoring a constant."""
        self.assertEqual(self._after_one_real_focus_case("0"), "0")

    def test_an_absent_cross_tree_override_stays_absent_after_focus(self):  # noqa: VACUOUS_ASSERTION — the two arms above are the unconditional positives on the same observable (incoming "1" comes back "1", incoming "0" comes back "0"); a fixture must not CONFER the override, so the absence is the product law
        """The absent control, and it is why the cure cannot be "always put a
        value back": a process that never set the override must not acquire one
        from a fixture. BLAST RADIUS: this trio — the three arms together admit
        exactly one behaviour, restoring what the process actually arrived
        with."""
        self.assertIsNone(self._after_one_real_focus_case(None))


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()
