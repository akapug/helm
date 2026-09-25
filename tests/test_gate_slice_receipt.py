"""The sliced receipt kind (v10): what mints it, what reads it, what refuses it.

The child is replaced at `gate._queued_process`, the seam every minting arm in
tests/test_gate.py uses, so the run is helm's real mint path end to end: the
FIFO, admission, the tree bracket, the evidence the runner would write, the
receipt id, the ledger and the binding door. Every refusal arm recomputes the
edited row's id first, so what it measures is the reader's judgment of the
CONTENT, not the integrity filter noticing an edit.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helm import (fabgate, foldcheck, gate, gateimport, gateslice, gatewindow,
                  landgate, vcs)
from helm.work import _gc
from tests._gate_supervisor import require_supervisor
from tests.test_gate import GateBase
from tests.test_gate_cap import CapBase

HELM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE = "import unittest\nclass Case(unittest.TestCase):\n    def test_%s(self): pass\n"
# What the real runner prints: its marker line first, then the protocol.
OK_TEXT = gateslice.DIAGNOSTIC_MARKER + "\nRan 2 tests in 0.1s\n\nOK\n"


class SliceFixture(GateBase):
    """A helm-shaped repository that ships the slice runner and two tests."""

    def setUp(self):
        super().setUp()
        for rel in gate.SLICE_RUNNER_FILES:
            shutil.copy(os.path.join(HELM, *rel.split("/")),
                        os.path.join(self.repo, *rel.split("/")))
        os.makedirs(os.path.join(self.repo, "tests"))
        # A real run compiles what it imports; the tree the receipt names
        # must stay clean after it, as helm's own .gitignore keeps it.
        for rel, body in ((".gitignore", "__pycache__/\n"),
                          ("tests/__init__.py", ""),
                          ("tests/test_a.py", CASE % "a"),
                          ("tests/test_b.py", CASE % "b")):
            with open(os.path.join(self.repo, rel), "w") as fh:
                fh.write(body)
        self._git("add", "-A")
        self._git("commit", "-qm", "suite")
        self.head = self._git("rev-parse", "HEAD")
        self.labels = gateslice.discovery_modules(
            gateslice.working_tree_files(self.repo))

    def evidence(self, **over):
        ev = {"v": 1, "kind": "gateslice", "workers": 2,
              "units": len(self.labels), "planned": 2,
              "modules_digest": gateslice.canonical_digest(self.labels),
              "inventory_digest": "a" * 64, "assignment_digest": "b" * 64,
              "schedule": "ascending-claim", "leak_mode": "fail", "leaks": 0,
              "swept": "empty-after-exit",
              "seconds": {label: 0.1 for label in self.labels},
              "outcome": {"ran": 2, "skipped": 0, "failures": 0, "errors": 0,
                          "expected_failures": 0, "unexpected_successes": 0,
                          "ok": True}}
        ev.update(over)
        return ev

    def mint(self, evidence, text=OK_TEXT, rc=0):
        self.spawned = None

        def child(repo, cmd, position, timeout, env=None, **_kw):
            self.spawned = (list(cmd), dict(env or {}))
            if evidence is not None:
                with open(env["HELM_GATESLICE_EVIDENCE"], "w") as fh:
                    json.dump(evidence, fh)
            return "", text, rc, None
        with mock.patch.object(gate, "_queued_process", side_effect=child):
            return gate.run(repo=self.repo, sliced=True)

    def minted(self):
        row, err = self.mint(self.evidence())
        self.assertIsNone(err, err)
        self.assertEqual(row["v"], gate.SLICE_VERSION, row)
        return row

    def edited(self, row, change):
        """A copy with `change` applied and its id recomputed, so the only
        thing wrong with it is the content the reader must judge."""
        out = copy.deepcopy(row)
        change(out)
        out["id"] = gate._receipt_id(out)
        return out


class MintTest(SliceFixture):
    def test_a_sliced_run_mints_v10_and_binds_at_its_exact_tip(self):
        row = self.minted()
        cmd, env = self.spawned
        self.assertEqual(cmd, [gate.interpreter()["executable"],
                               os.path.join(self.repo, "helm", "gateslice.py")])
        self.assertEqual((env["HELM_GATESLICE_LEAKS"], env["HELM_GATE_SUITE_CAP"]),
                         ("fail", "1"))
        workers = int(env["HELM_GATESLICE_WORKERS"])
        self.assertTrue(workers >= 2 and workers % gate._CORES_PER_SUITE == 0,
                        workers)
        runner = row["slice_authority"]["runner"]
        self.assertEqual([f["path"] for f in runner["files"]],
                         list(gate.SLICE_RUNNER_FILES))
        for item in runner["files"]:
            self.assertEqual(item["blob"],
                             self._git("rev-parse", "HEAD:" + item["path"]))
        self.assertEqual((row["failure_total"], row["failure_chunks"]), (0, []))
        self.assertEqual(gate.by_id(row["id"])[0]["id"], row["id"])
        self.assertIsNone(gate.row_refusal(row, consuming_repo=self.repo))
        state, rid, why = gate.bind("gate:%s" % row["id"], self.head)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)

    def test_evidence_that_disagrees_with_the_protocol_mints_nothing(self):
        row, err = self.mint(self.evidence(outcome=dict(
            self.evidence()["outcome"], ran=3)))
        self.assertIsNone(row)
        self.assertIn("diagnostic runner's marker", err)
        self.assertEqual(gate.receipts()[0], [])

    def test_a_run_that_left_no_evidence_mints_nothing(self):
        row, err = self.mint(None, text=gateslice.DIAGNOSTIC_MARKER + "\n"
                             "gateslice: workers discovered 2 different test "
                             "inventories; result refused\n", rc=2)
        self.assertIsNone(row)
        self.assertIn("diagnostic runner's marker", err)
        self.assertEqual(gate.receipts()[0], [])

    def test_a_tree_without_the_runner_refuses_before_spending_anything(self):
        os.unlink(os.path.join(self.repo, "helm", "gateshard.py"))
        self._git("commit", "-qam", "drop a runner file")
        with mock.patch.object(gate, "_acquire_gate") as fifo:
            row, err = gate.run(repo=self.repo, sliced=True)
        self.assertIsNone(row)
        self.assertIn("does not ship the slice runner", err)
        self.assertIn("helm/gateshard.py", err)
        fifo.assert_not_called()

    def test_sliced_does_not_combine_with_focus_or_a_command(self):
        for kwargs in ({"focus": True}, {"argv": ["true"]}):
            row, err = gate.run(repo=self.repo, sliced=True, **kwargs)
            self.assertIsNone(row)
            self.assertIn("--sliced runs helm's whole suite", err)


class RealRunnerTest(SliceFixture):
    """The gate and the runner agree on their contract only if the real child
    runs: every other arm here fakes it at `_queued_process`, so a renamed
    key on either side would pass them all and close the door silently."""

    def test_the_real_runner_through_the_sliced_door_mints_a_v10_that_binds(self):
        require_supervisor()
        row, err = gate.run(repo=self.repo, sliced=True, timeout=300)
        self.assertIsNone(err, err)
        self.assertEqual((row["v"], row["status"], row["ran"]),
                         (gate.SLICE_VERSION, "OK", 2), row)
        auth = row["slice_authority"]
        self.assertEqual((auth["leak_mode"], auth["leaks"], auth["planned"]),
                         ("fail", 0, 2))
        self.assertIsNone(gate.row_refusal(row, consuming_repo=self.repo))
        state, rid, why = gate.bind("gate:%s" % row["id"], self.head)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)


class SlicedNeverLandsTest(SliceFixture):
    """The review's divergence tree, pinned (the sliced kind's final QC).

    test_a sets tests._shared.VALUE to 1 and test_b expects 0. Serial runs
    both in one process, so test_b sees the 1 and FAILS. The slice runner
    puts them on different workers, so test_b never sees the 1. Before the
    data audit the sliced receipt came back OK with zero leaks, and bind said
    VERIFIED: THE KNOWN LIMIT. The data audit (task/3039) names the write to
    tests._shared.VALUE, and fail mode makes the sliced run FAILED too. Every
    land door still refuses the sliced kind by name, green or red, until the
    flip is decided.
    """

    TREE = {
        "tests/_shared.py": "VALUE = 0\n",
        "tests/test_a.py": (
            "import unittest\nfrom tests import _shared\n"
            "class A(unittest.TestCase):\n"
            "    def test_sets_the_shared_value(self):\n"
            "        _shared.VALUE = 1\n"),
        "tests/test_b.py": (
            "import unittest\nfrom tests import _shared\n"
            "class B(unittest.TestCase):\n"
            "    def test_expects_the_initial_value(self):\n"
            "        self.assertEqual(_shared.VALUE, 0)\n"),
    }

    def setUp(self):
        super().setUp()
        for rel, body in self.TREE.items():
            with open(os.path.join(self.repo, rel), "w") as fh:
                fh.write(body)
        self._git("add", "-A")
        self._git("commit", "-qm", "the divergence tree")
        self.head = self._git("rev-parse", "HEAD")

    def sliced_apart(self):
        """The real runner through the sliced door, with a recorded-time plan
        that puts test_a and test_b on different workers, as the review's
        weighted two-worker run did."""
        timings = os.path.join(self.tmp, "timings.json")
        with open(timings, "w") as fh:
            json.dump({"tests": 0.0, "tests.test_a": 1.0,
                       "tests.test_b": 1.0}, fh)
        with mock.patch.object(gate, "_slice_timings", return_value=timings):
            return gate.run(repo=self.repo, sliced=True, timeout=300)

    def test_serial_fails_it_and_the_audited_sliced_run_fails_it_too(self):
        require_supervisor()
        serial, err = gate.run(repo=self.repo, timeout=300)
        self.assertIsNone(err, err)
        self.assertEqual((serial["status"], serial["ran"]), ("FAILED", 2),
                         serial)
        row, err = self.sliced_apart()
        self.assertIsNone(err, err)
        # THE LIMIT, CLOSED: the same tree is red when sliced, and the one
        # failure is the audit naming the write test_b would have read.
        self.assertEqual((row["v"], row["status"], row["ran"]),
                         (gate.SLICE_VERSION, "FAILED", 2), row)
        self.assertEqual((row["slice_authority"]["leak_mode"],
                          row["slice_authority"]["leaks"],
                          row["slice_authority"]["v"]),
                         ("fail", 1, gate.SLICE_DATA_AUDIT_EVIDENCE))
        self.assertEqual([item["test"] for item in row["failures"]],
                         ["tests.test_a." + gateslice.LEAK_TEST])
        self.assertIn("tests._shared.VALUE (rebound)",
                      json.dumps(row["failures"]))
        state, _rid, why = gate.bind("gate:" + row["id"], self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_no_land_door_takes_a_green_sliced_receipt(self):
        row = self.minted()
        rid, handle = row["id"], "gate:" + row["id"]
        # Lane level binds it.
        state, _rid, why = gate.bind(handle, self.head)
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(foldcheck.gate_authority(
            self.repo, self.head, handle).verdict, foldcheck.PASS)
        # Every door that authorizes a land refuses it, by kind and by name.
        needle = "a land needs a serial whole-suite receipt"
        state, _rid, why = gate.bind(handle, self.head, need=gate.NEED_LAND)
        self.assertEqual(state, "REFUSED")
        self.assertIn(needle, why)
        state, why = landgate.gate_binds_tree(rid, None, repo=self.repo,
                                              tip=self.head)
        self.assertEqual(state, landgate.REFUSE)
        self.assertIn(needle, why)
        rung = foldcheck._tree_matches_gate(vcs.backend(self.repo), self.repo,
                                            self.head, handle, land=True)
        self.assertEqual(rung.verdict, foldcheck.REFUSE)
        self.assertIn(needle, rung.discriminator)
        self.assertIn("(task/3039)", rung.discriminator)

    def test_the_train_asks_its_node_for_the_serial_suite_only(self):  # noqa: VACUOUS_ASSERTION — the scope is asserted EQUAL to the non-empty canonical serial scope and argv first; the absence (names no runner) is the second claim on the same argv
        """The landing window measures and submits helm's canonical serial
        scope, so the train cannot choose the sliced mode for its receipt."""
        argv = gatewindow.measure_argv(self.repo, "0" * 40)
        scope = json.loads(argv[argv.index("--scope-json") + 1])
        self.assertEqual(scope, fabgate.whole_scope())
        self.assertEqual(scope["argv"], list(gate.SUITE))
        self.assertFalse(gate._names_diagnostic_runner(scope["argv"]))


class DiagnosticOutputNeverMintsTest(SliceFixture):
    """However the runner is reached, its output mints no suite receipt; only
    the sliced door, which binds the runner's evidence, may carry it."""

    def output(self, argv):
        env = dict(os.environ, HELM_GATESLICE_WORKERS="1",
                   HELM_GATE_SUITE_CAP="1", HELM_GATESLICE_LEAKS="report")
        for key in ("HELM_GATESLICE_EVIDENCE", "HELM_GATESLICE_KEEP",
                    "HELM_GATESLICE_TIMINGS"):
            env.pop(key, None)
        proc = subprocess.run(argv, cwd=self.repo, env=env, text=True,
                              capture_output=True, timeout=300)
        return proc.returncode, proc.stderr

    def mint_declared(self, argv, rc, out):
        head, tree, dirty, err = gate.tree_state(self.repo)
        self.assertIsNone(err, err)
        command = {"source": "registry", "argv": list(argv),
                   "protocol": "unittest", "project": "p"}
        return gate._mint_result(self.repo, head, tree, dirty,
                                 gate.interpreter(), list(argv), True, None,
                                 rc, time.time(), out, command=command)

    def test_every_wrapper_of_the_runner_is_refused_at_mint(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts the runner output parsed OK and the mint refusal text positively; the serial-suite arm beside it is the control that a marker-free suite still mints
        runner = os.path.join(self.repo, "helm", "gateslice.py")
        for name, argv in (
                ("joined -m", [sys.executable, "-mhelm.gateslice"]),
                ("shell", ["sh", "-c", "exec %s %s" % (sys.executable, runner)]),
                ("runpy", [sys.executable, "-c",
                           "import runpy; runpy.run_path(%r, run_name="
                           "'__main__')" % runner])):
            with self.subTest(wrapper=name):
                rc, out = self.output(argv)
                self.assertEqual(gate.parse_result(out)["status"], "OK", out)
                row, minted, err = self.mint_declared(argv, rc, out)
                self.assertFalse(minted, name)
                self.assertIn("diagnostic runner's marker", err)
        self.assertEqual(gate.receipts()[0], [])

    def test_the_serial_suite_still_mints(self):
        argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                "-t", "."]
        rc, out = self.output(argv)
        row, minted, err = self.mint_declared(argv, rc, out)
        self.assertIsNone(err, err)
        self.assertTrue(minted)
        self.assertEqual((row["status"], row["ran"]), ("OK", 2))

    def test_a_serial_red_quoting_the_marker_is_told_to_fix_the_test(self):
        """The documented limitation: a serial run whose failure text quotes
        a runner's marker at column 0 is refused, and the remedy is the test
        that dumped raw runner output, not the sliced door."""
        ident = gate.interpreter()
        head, tree, dirty, err = gate.tree_state(self.repo)
        self.assertIsNone(err, err)
        out = ("F.\n" + "=" * 70 + "\nFAIL: test_a (tests.test_a.Case.test_a)"
               "\n" + "-" * 70 + "\nAssertionError: the runner said:\n"
               + gateslice.DIAGNOSTIC_MARKER + "\n\n" + "-" * 70
               + "\nRan 2 tests in 0.1s\n\nFAILED (failures=1)\n")
        self.assertEqual(gate.parse_result(out)["status"], "FAILED")
        row, minted, err = gate._mint_result(
            self.repo, head, tree, dirty, ident,
            [ident["executable"]] + list(gate.SUITE), True, None, 1,
            time.time(), out)
        self.assertFalse(minted)
        self.assertIn("diagnostic runner's marker", err)
        self.assertIn("capture or strip", err)
        self.assertNotIn("--sliced", err)
        # The wrapped-runner case keeps the sliced door as its remedy.
        runner = os.path.join(self.repo, "helm", "gateslice.py")
        argv = ["sh", "-c", "exec %s %s" % (sys.executable, runner)]
        rc, out = self.output(argv)
        row, minted, err = self.mint_declared(argv, rc, out)
        self.assertFalse(minted)
        self.assertIn("--sliced", err)

    def test_every_spelling_of_a_runner_reads_as_one_and_serial_does_not(self):
        runner = "/srv/x/helm/gateslice.py"
        for argv in ([sys.executable, "-mhelm.gateslice"],
                     [sys.executable, "-m", "helm.gateshard"],
                     ["sh", "-c", "exec python3 %s" % runner],
                     [sys.executable, "-c", "import runpy; runpy.run_path("
                      "%r, run_name='__main__')" % runner]):
            self.assertTrue(gate._names_diagnostic_runner(argv), argv)
        for argv in (
                [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                 "-t", "."],
                # A NAME, NOT A SUBSTRING: an interpreter under a lane
                # directory named for a runner, and the runner's own tests.
                ["/srv/lanes/gateslice-stall-fix/.venv/bin/python3", "-m",
                 "unittest", "discover", "-s", "tests", "-t", "."],
                ["/srv/lanes/gateshard/bin/python3", "-m", "unittest",
                 "discover", "-s", "tests", "-t", "."],
                [sys.executable, "-m", "unittest", "tests.test_gateslice"],
                ["sh", "-c", "python3 -m unittest tests/test_gateshard.py"]):
            self.assertFalse(gate._names_diagnostic_runner(argv), argv)
        self.assertTrue(gate.diagnostic_output(
            "noise\n" + gateslice.DIAGNOSTIC_MARKER + "\nRan 1 test\n"))
        self.assertTrue(gate.diagnostic_output(
            "HELM-DIAGNOSTIC-RUNNER gateshard\n"))
        self.assertFalse(gate.diagnostic_output(
            "quoted: %s in a sentence\n" % gateslice.DIAGNOSTIC_MARKER))


class OutsideTimingsTest(SliceFixture):
    """A Fab-run slice starts in a fresh home with no sliced receipt, so its
    schedule's seconds come from outside (task/3039): validated, and ignored
    loudly when they do not validate."""

    def write(self, name, value):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(value if isinstance(value, str) else json.dumps(value))
        return path

    def chosen(self, outside=None):
        work = tempfile.mkdtemp(dir=self.tmp)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            path = gate._slice_timings(work, outside)
        if path is None:
            return None, err.getvalue()
        with open(path) as fh:
            return json.load(fh), err.getvalue()

    def test_malformed_timings_are_named_and_never_trusted(self):
        good = {"tests": 0.0, "tests.test_a": 1.5, "tests.test_b": 2}
        seconds, why = gate.read_slice_timings(self.write("good.json", good))
        self.assertEqual((seconds, why), (good, None))
        for name, value, needle in (
                ("list", [1, 2], "not a non-empty JSON object"),
                ("empty", {}, "not a non-empty JSON object"),
                ("label", {"tests/test_a.py": 1}, "not a module label"),
                ("negative", {"tests.test_a": -1}, "not in [0, 86400]"),
                ("huge", {"tests.test_a": 1e9}, "not in [0, 86400]"),
                ("bool", {"tests.test_a": True}, "not in [0, 86400]"),
                ("nan", '{"tests.test_a": NaN}', "not in [0, 86400]"),
                ("text", "not json", "unreadable")):
            seconds, why = gate.read_slice_timings(self.write(name, value))
            self.assertIsNone(seconds, name)
            self.assertIn(needle, why, name)
        big = self.write("big.json", " " * (gate._TIMINGS_MAX_BYTES + 1))
        self.assertIn("larger than", gate.read_slice_timings(big)[1])

    def test_the_first_source_that_validates_orders_the_schedule(self):
        ledger = self.minted()["slice_authority"]["seconds"]
        outside = {"tests.test_a": 9.0}
        home_file = {"tests.test_b": 7.0}
        # An explicit file wins.
        self.assertEqual(self.chosen(self.write("o.json", outside)),
                         (outside, ""))
        # With none, the home's file; with a malformed explicit one, the
        # home's file too, and the malformed one is NAMED.
        with open(gate.slice_timings_path(), "w") as fh:
            json.dump(home_file, fh)
        self.assertEqual(self.chosen(), (home_file, ""))
        bad = self.write("bad.json", {"tests.test_a": -3})
        seconds, said = self.chosen(bad)
        self.assertEqual(seconds, home_file)
        self.assertIn("IGNORING slice timings %s" % bad, said)
        # A malformed home file is named too, and the ledger answers.
        with open(gate.slice_timings_path(), "w") as fh:
            fh.write("[]")
        seconds, said = self.chosen()
        self.assertEqual(seconds, ledger)
        self.assertIn("IGNORING slice timings %s" % gate.slice_timings_path(),
                      said)

    def test_run_hands_the_runner_the_validated_outside_seconds(self):  # noqa: VACUOUS_ASSERTION — the runner's timings file is asserted EQUAL to the non-empty outside seconds; err None is the mint that let the child run
        outside = {"tests.test_a": 4.0, "tests.test_b": 3.0}
        seen = {}

        def child(repo, cmd, position, timeout, env=None, **_kw):
            with open(env["HELM_GATESLICE_TIMINGS"]) as fh:
                seen["timings"] = json.load(fh)
            with open(env["HELM_GATESLICE_EVIDENCE"], "w") as fh:
                json.dump(self.evidence(), fh)
            return "", OK_TEXT, 0, None
        with mock.patch.object(gate, "_queued_process", side_effect=child):
            row, err = gate.run(repo=self.repo, sliced=True,
                                timings=self.write("o.json", outside))
        self.assertIsNone(err, err)
        self.assertEqual(seen["timings"], outside)

    def test_the_read_verb_prints_the_newest_sliced_seconds(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gate.cmd_gate(["slice-timings"])
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertIn("no sliced receipt", err.getvalue())
        row = self.minted()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = gate.cmd_gate(["slice-timings"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         row["slice_authority"]["seconds"])

    def test_timings_need_a_local_sliced_run(self):  # noqa: VACUOUS_ASSERTION — the loop is a fixed two-case tuple and each case asserts rc 2 and the refusal text positively
        for argv in (["--timings", "x.json"],
                     ["--sliced", "--box", "snoozy", "--timings", "x.json"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = gate._cmd_run(argv + ["--repo", self.repo])
            self.assertEqual(rc, 2, argv)
            self.assertIn("--timings orders a local sliced run", err.getvalue())

    def test_the_plan_names_the_most_workers_the_runner_starts(self):  # noqa: VACUOUS_ASSERTION — rc 0 and workers asserted EQUAL to slots x cores are positive claims on the answer
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = gate._cmd_run(["--sliced", "--plan", "--json",
                                "--repo", self.repo])
        self.assertEqual(rc, 0)
        piece = json.loads(out.getvalue())["slice"]
        self.assertEqual(piece["workers"],
                         piece["slots"] * gate._CORES_PER_SUITE)


class FabSliceImportTest(SliceFixture):
    """A v10 receipt a Fab job minted imports through the Fab completion
    door when the job named the slice scope and the sliced runner, and only
    then (task/3039)."""

    def authority(self, row, scope, gate_args, digest):
        identity = {
            "format": gateimport.FAB_KEY_FORMAT,
            "repository": {"common_dir": gateimport._repo_identity(self.repo)},
            "tree": row["tree"], "scope": scope,
            "interpreter": row["interpreter"],
            "runner": {"format": "fab-gate-runner-v1",
                       "argv": list(gateimport.FAB_RUNNER_ARGV) + gate_args,
                       "wrapper_version": "c" * 64,
                       "cgroup": gateimport.FAB_RUNNER_CGROUPS[0]}}
        key = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"format": "helm-fab-completion-v2", "key": key,
                "generation": "run-%s-1-%s" % ("a" * 32, "b" * 16),
                "job_id": "gate-" + key, "host": row["host"]["node"],
                "sha": self.head, "identity": identity,
                "budgets": {"queue_s": 60.0, "execution_s": 600.0},
                "source_artifact": "/remote/gate-receipts.jsonl",
                "artifact_sha256": digest, "exit": 0}

    def test_the_sliced_job_imports_its_v10_and_nothing_else_does(self):  # noqa: VACUOUS_ASSERTION — each refusal is asserted EQUAL to its named error, and the final import of the same row through the right authority is the unconditional positive control
        row = self.minted()
        artifact = os.path.join(self.tmp, "fab-artifact.jsonl")
        with open(artifact, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        with open(artifact, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        os.unlink(gate.receipts_path())
        gateimport._STORED_IDS_CACHE[0] = None
        refused = (
            (self.authority(row, fabgate.slice_scope(), [], digest),
             "Fab completion runner identity is malformed"),
            (self.authority(row, fabgate.whole_scope(), ["--sliced"], digest),
             "Fab completion runner identity is malformed"),
            (self.authority(row, fabgate.whole_scope(), [], digest),
             "Fab scope differs from the receipt version's canonical suite"))
        for authority, needle in refused:
            imported, _verdict, err = gateimport.import_fab_receipt(
                artifact, self.repo, authority, want_id=row["id"])
            self.assertIsNone(imported, needle)
            self.assertEqual(err, needle)
        imported, verdict, err = gateimport.import_fab_receipt(
            artifact, self.repo,
            self.authority(row, fabgate.slice_scope(), ["--sliced"], digest),
            want_id=row["id"])
        self.assertIsNone(err, err)
        self.assertEqual((imported["id"], verdict), (row["id"], "imported"))


class RunnerFilesPinnedTest(unittest.TestCase):
    """SLICE_RUNNER_FILES is the runner record's claim about which files run.
    It is derived here from the source, so a new by-path load on the slice
    path cannot leave the record short: every file gateslice loads, plus
    every file loaded by a gateshard function the slice path can reach."""

    @staticmethod
    def parse(name):
        with open(os.path.join(HELM, "helm", name), encoding="utf-8") as fh:
            return ast.parse(fh.read())

    @staticmethod
    def functions(tree):
        return {node.name: node for node in tree.body
                if isinstance(node, ast.FunctionDef)}

    @staticmethod
    def loads(node, standalone):
        """The .py files one function loads by path, or [] if it loads none."""
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "spec_from_file_location"]
        if not calls:
            return []
        found = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str) and n.value.endswith(".py")]
        found += [standalone[n.slice.value] for n in ast.walk(node)
                  if isinstance(n, ast.Subscript)
                  and getattr(n.value, "id", None) == "STANDALONE_DEPENDENCIES"]
        return found

    def test_the_runner_record_names_every_file_the_slice_path_loads(self):  # noqa: VACUOUS_ASSERTION — the walk is controlled (it must see the slice path call _fresh_env) and the comparison is to the non-empty SLICE_RUNNER_FILES; mutation-verified by making gateslice reach _recorder
        from helm import gateshard
        slicer, shard = self.parse("gateslice.py"), self.parse("gateshard.py")
        standalone = gateshard.STANDALONE_DEPENDENCIES
        files = {"gateslice.py"}
        for node in self.functions(slicer).values():
            files.update(self.loads(node, standalone))
        used = {n.attr for n in ast.walk(slicer) if isinstance(n, ast.Attribute)
                and (getattr(n.value, "id", None) == "shard"
                     or getattr(getattr(n.value, "func", None), "id", None)
                     == "_shard")}
        defs = self.functions(shard)
        # CONTROL: the walk sees the slice path's real calls into gateshard.
        self.assertIn("_fresh_env", used)
        seen, todo = set(), [name for name in used if name in defs]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            files.update(self.loads(defs[name], standalone))
            todo.extend(n.id for n in ast.walk(defs[name])
                        if isinstance(n, ast.Name) and n.id in defs)
        self.assertEqual(sorted("helm/" + name for name in files),
                         list(gate.SLICE_RUNNER_FILES))


class ReaderTest(SliceFixture):
    def assertRefused(self, row, needle):
        refusal = gate.row_refusal(row, consuming_repo=self.repo)
        self.assertIsNotNone(refusal, "the reader admitted %r" % needle)
        self.assertIn(needle, refusal)

    def test_the_minted_row_is_the_control(self):
        row = self.minted()
        self.assertIsNone(gate.slice_refusal(row))
        self.assertIsNone(gate.row_refusal(row, consuming_repo=self.repo))
        self.assertTrue(gate.stored_whole_suite(row))

    def test_both_evidence_versions_read_and_an_unknown_one_refuses(self):  # noqa: VACUOUS_ASSERTION — the absent refusal for v1 and v2 is controlled by v3 on the same row, asserted REFUSED by name
        """v2 is the runner's census with module data in it; v1 rows in a
        ledger stay readable, and a version nobody wrote is refused."""
        self.assertEqual(gateslice.EVIDENCE_VERSION,
                         gate.SLICE_DATA_AUDIT_EVIDENCE)
        for version in gate.SLICE_EVIDENCE_VERSIONS:
            row, err = self.mint(self.evidence(v=version))
            self.assertIsNone(err, err)
            self.assertIsNone(gate.slice_refusal(row), version)
        row = self.minted()
        self.assertRefused(self.edited(row, lambda r: r["slice_authority"]
                                       .update(v=3)),
                           "slice evidence version or kind is unknown")

    def test_a_report_mode_run_or_a_leaking_module_stands_for_nothing(self):
        row = self.minted()
        self.assertRefused(self.edited(row, lambda r: r["slice_authority"]
                                       .update(leak_mode="report")),
                           "leak audit in 'report' mode")
        self.assertRefused(self.edited(row, lambda r: r["slice_authority"]
                                       .update(leaks=1)),
                           "1 leaking module(s)")

    def test_a_runner_blob_its_tree_does_not_hold_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRefused asserts the refusal is present (assertIsNotNone) and names the defect (assertIn); test_the_minted_row_is_the_control is the same row unedited
        row = self.minted()

        def swap(r):
            r["slice_authority"]["runner"]["files"][2]["blob"] = "0" * 40
        self.assertRefused(self.edited(row, swap),
                           "runner file helm/gateslice.py is not the blob")

    def test_a_module_list_its_tree_does_not_walk_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRefused asserts the refusal is present (assertIsNotNone) and names the defect (assertIn); test_the_minted_row_is_the_control is the same row unedited
        row = self.minted()
        other = self.labels[:-1] + ["tests.test_z"]

        def claim(r):
            r["slice_authority"]["modules_digest"] = \
                gateslice.canonical_digest(other)
            r["slice_authority"]["seconds"] = {m: 0.1 for m in other}
        self.assertRefused(self.edited(row, claim),
                           "are not the modules the run recorded")

    def test_an_unreadable_moment_is_refused_but_never_remembered(self):
        row = self.minted()
        with mock.patch.object(gate.vcs, "backend",
                               side_effect=OSError("transiently unreadable")):
            refusal = gate.slice_tree_refusal(row, self.repo)
        self.assertIn("cannot be re-derived: transiently unreadable", refusal)
        self.assertIsNone(gate.slice_tree_refusal(row, self.repo))

    def test_counts_argv_or_a_declaration_the_evidence_does_not_support(self):
        row = self.minted()
        for change, needle in (
                (lambda r: r["slice_authority"]["outcome"].update(ok=False),
                 "verdict disagrees"),
                (lambda r: r.update(ran=5), "counts disagree"),
                (lambda r: r["slice_authority"]["seconds"].popitem(),
                 "seconds do not cover"),
                (lambda r: r.update(argv=r["argv"][:1] + ["helm/gateslice.py"]),
                 "argv is not this checkout's slice runner"),
                (lambda r: r.update(suite_command={
                    "source": "registry", "argv": ["x"], "protocol": "exit",
                    "project": "p"}), "declared project command")):
            with self.subTest(needle=needle):
                self.assertRefused(self.edited(row, change), needle)

    def test_an_edit_without_a_new_id_leaves_the_ledger(self):
        row = self.minted()
        with open(gate.receipts_path()) as fh:
            lines = fh.read().splitlines()
        tampered = json.loads(lines[-1])
        tampered["slice_authority"]["leaks"] = 0
        tampered["slice_authority"]["planned"] += 1
        with open(gate.receipts_path(), "a") as fh:
            fh.write(json.dumps(tampered) + "\n")
        self.assertNotEqual(gate._receipt_id(tampered), tampered["id"])
        ids = [r["id"] for r in gate.receipts()[0]]
        self.assertEqual(ids.count(row["id"]), 1)

    def test_the_kind_carries_the_serial_ladder_and_not_the_sharded_rung(self):
        keys = gate.receipt_version_keys(gate.SLICE_VERSION)
        self.assertIn("slice_authority", keys)
        self.assertIn("failure_total", keys)
        self.assertIn("host", keys)
        self.assertNotIn("sharded_authority", keys)
        self.assertFalse(gate._version_has(gate.SLICE_VERSION,
                                           "sharded_authority"))
        self.assertTrue(gate._version_has(9, "sharded_authority"))
        self.assertIn(gate.SLICE_VERSION, gateimport.KNOWN_VERSIONS)
        self.assertNotIn(gate.WITHDRAWN_SHARD_VERSION, gate.RECEIPT_VERSIONS)

    def test_the_import_door_reads_the_kind_and_re_derives_its_tree(self):
        row = self.minted()
        self.assertIsNone(gateimport._schema_err(row))
        self.assertIsNone(gateimport.placement_err(row, self.repo))
        broken = self.edited(row, lambda r: r["slice_authority"].update(
            schedule="whatever"))
        self.assertIn("schedule", gateimport._schema_err(broken))

    def test_the_stored_suite_question_asks_the_kind_not_the_argv(self):
        row = self.minted()
        self.assertTrue(gate.stored_whole_suite(row))
        self.assertIsNone(_gc.receipt_inadmissible(
            row, consuming_repo=self.repo))
        plain = dict(row, v=4)
        plain.pop("slice_authority")
        self.assertFalse(gate.stored_whole_suite(plain))


class SliceAdmissionTest(CapBase):
    """A sliced run holds several slots of the host's cap and every other
    admission prices it at that weight."""

    TOPOLOGY = {"cores": 32, "reason": "32 host-online CPUs"}

    def position(self, pid, name):
        self.launcher(pid)
        return {"id": name, "pid": pid, "starttime": 7, "holder": "seat-a"}

    def grant(self, pid, name, slots):
        err = self.admit(position=self.position(pid, name),
                         topology=self.TOPOLOGY, slots=slots)
        return err, (self.capacity or {}).get("slots")

    def test_two_sliced_runs_fill_a_32_core_host_and_the_third_waits(self):
        self.paneless()
        self.assertEqual(self.grant(201, "pos-a", gate.SLICE_SLOTS),
                         (None, gate.SLICE_SLOTS))
        self.assertEqual(self.grant(202, "pos-b", gate.SLICE_SLOTS),
                         (None, gate.SLICE_SLOTS))
        err, _slots = self.grant(203, "pos-c", 1)
        self.assertIn("whole-suite cap is 16", str(err))
        weights = {row["position"]: row.get("slots", 1)
                   for row in self.admission_rows()}
        self.assertEqual(weights, {"pos-a": 8, "pos-b": 8})

    def test_a_sliced_run_takes_what_is_free_and_a_serial_run_one_slot(self):
        self.paneless()
        for pid in range(301, 313):
            self.plant(pid, ("python3", "-m", "unittest", "discover", "-s",
                             "tests", "-t", "."), cwd="/lane/%d" % pid)
        # A serial run holds one slot and its intent row names no weight.
        self.assertEqual(self.grant(401, "pos-serial", 1), (None, None))
        # 12 running + 1 admitted leaves 3 of 16: the sliced run takes those.
        self.assertEqual(self.grant(402, "pos-slice", gate.SLICE_SLOTS),
                         (None, 3))
        weights = {row["position"]: row.get("slots", 1)
                   for row in self.admission_rows()}
        self.assertEqual(weights, {"pos-serial": 1, "pos-slice": 3})
        err, _slots = self.grant(403, "pos-late", 1)
        self.assertIn("whole-suite cap is 16", str(err))

    def test_the_occupancy_a_router_reads_is_the_weight_admission_prices(self):
        """A router counting suite roots saw 12 here and routed a thirteenth
        into a node admission had already filled (task/3039)."""
        self.paneless()
        for pid in range(301, 313):
            self.plant(pid, ("python3", "-m", "unittest", "discover", "-s",
                             "tests", "-t", "."), cwd="/lane/%d" % pid)
        self.assertEqual(self.grant(401, "pos-serial", 1), (None, None))
        self.assertEqual(self.grant(402, "pos-slice", gate.SLICE_SLOTS),
                         (None, 3))
        census = gate.suite_census(self.proc)
        self.assertEqual(sum(1 for r in census if r.get("kind") == "suite"),
                         12)
        self.assertEqual(gate.suite_occupancy(
            proc_dir=self.proc, admissions_path=self.admissions), 16)
        with mock.patch.object(gate, "suite_census", return_value=None):
            self.assertIsNone(gate.suite_occupancy(
                proc_dir=self.proc, admissions_path=self.admissions))


if __name__ == "__main__":
    unittest.main()
