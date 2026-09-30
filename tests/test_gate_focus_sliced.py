"""The sliced focused runner: `gate run --focus` over helm/gateslice.py.

A focused selection is routinely half the suite, and one serial process spent
nearly all of a focused round executing it. These arms pin what running that
selection as slices may and may not change:

  * a selection of FOCUS_SLICE_MIN_MODULES or more runs through the slice
    runner's `--modules` scope on more than one worker, loads only the
    selected modules (never the whole discovery), and records each module's
    seconds where the next round's schedule reads them;
  * a small selection, a Fab job granted too few cores, or a host that grants
    no slots runs serial, and SAYS which and why;
  * the receipt is the same focused kind (v6) and binds exactly as before: a
    cure-round FIX, never an APPROVE or a land;
  * a failing module fails the run and is named, and a report-mode leak makes
    the focused receipt non-OK before it can bind;
  * a two-module barrier proves useful test work overlaps in distinct worker
    processes, rather than merely proving that workers were configured;
  * the tree is read before and after the run.

The child is replaced at `gate._queued_process` by one that runs the real
command directly, so the planner, the runner, its workers, the protocol
parse, the mint and every binding door are real; only the cgroup guard
around the child is not (`tests/_gate_supervisor` says why a `fab test`
node cannot create one).
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helm import dispatches, foldcheck, gate, gateslice, landgate, vcs
from tests import test_gate_focus as _focus
from tests.test_gate_cap import CapBase

CONSUMER = ("import unittest\n"
            "from pkg import alpha\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertEqual(alpha.X, 1)\n")
# A consumer whose test leaves the environment changed: state a later module
# in ONE serial process would see, and a module on another worker would not.
LEAKING = ("import os, unittest\n"
           "from pkg import alpha\n"
           "class T(unittest.TestCase):\n"
           "    def test_a(self):\n"
           "        os.environ['FOCUS_SLICE_LEFT_BEHIND'] = '1'\n"
           "        self.assertEqual(alpha.X, 1)\n")
LEAKING_SYNC = ("import os, time, unittest\n"
                "from pkg import alpha\n"
                "ME, OTHER = %r, %r\n"
                "class T(unittest.TestCase):\n"
                "    def test_a(self):\n"
                "        os.environ['FOCUS_SLICE_LEFT_BEHIND'] = '1'\n"
                "        if os.environ.get('HELM_GATESLICE_WORKER'):\n"
                "            open(ME, 'w').close()\n"
                "            deadline = time.monotonic() + 30\n"
                "            while not os.path.exists(OTHER):\n"
                "                if time.monotonic() >= deadline:\n"
                "                    self.fail('consumer never reached its "
                "worker')\n"
                "                time.sleep(0.01)\n"
                "        self.assertEqual(alpha.X, 1)\n")
LEAK_CONSUMER_SYNC = ("import os, time, unittest\n"
                      "from pkg import alpha\n"
                      "ME, OTHER = %r, %r\n"
                      "class T(unittest.TestCase):\n"
                      "    def test_a(self):\n"
                      "        if os.environ.get('HELM_GATESLICE_WORKER'):\n"
                      "            open(ME, 'w').close()\n"
                      "            deadline = time.monotonic() + 30\n"
                      "            while not os.path.exists(OTHER):\n"
                      "                if time.monotonic() >= deadline:\n"
                      "                    self.fail('producer never reached its "
                      "worker')\n"
                      "                time.sleep(0.01)\n"
                      "        self.assertNotIn('FOCUS_SLICE_LEFT_BEHIND', "
                      "os.environ)\n"
                      "        self.assertEqual(alpha.X, 1)\n")
BARRIER = ("import os, time, unittest\n"
           "from pkg import alpha\n"
           "ME, OTHER = %r, %r\n"
           "class T(unittest.TestCase):\n"
           "    def test_a(self):\n"
           "        tmp = ME + '.%%d' %% os.getpid()\n"
           "        with open(tmp, 'w', encoding='ascii') as fh:\n"
           "            fh.write(str(os.getpid()))\n"
           "        os.replace(tmp, ME)\n"
           "        deadline = time.monotonic() + 30\n"
           "        while not os.path.exists(OTHER):\n"
           "            if time.monotonic() >= deadline:\n"
           "                self.fail('the other module never ran concurrently')\n"
           "            time.sleep(0.01)\n"
           "        with open(OTHER, encoding='ascii') as fh:\n"
           "            self.assertNotEqual(int(fh.read()), os.getpid())\n"
           "        self.assertEqual(alpha.X, 1)\n")
FAILING = ("import unittest\n"
           "from pkg import alpha\n"
           "class T(unittest.TestCase):\n"
           "    def test_a(self):\n"
           "        self.assertEqual(alpha.X, 99)\n")
# A module no change reaches. Importing it leaves a mark, so an arm can tell
# a run that loaded only its selection from one that walked the discovery.
STRANGER = ("import unittest\n"
            "open(%r, 'a').close()\n"
            "class T(unittest.TestCase):\n"
            "    def test_s(self):\n"
            "        self.assertTrue(True)\n")


def _direct_child(case):
    """A `_queued_process` that runs the real command in the repo, with the
    env the gate composed, and keeps what it was asked."""
    case.spawned = []
    case.before_child = None

    def child(repo, cmd, position, timeout, identity=None, env=None):
        case.spawned.append({"cmd": list(cmd), "env": dict(env or {}),
                             "identity": identity})
        if case.before_child:
            case.before_child()
        proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True,
                              text=True, timeout=600)
        return proc.stdout, proc.stderr, proc.returncode, None
    patch = mock.patch.object(gate, "_queued_process", side_effect=child)
    patch.start()
    case.addCleanup(patch.stop)


def _outside_fab(case, cores=32):
    """This process is not a Fab job, and the host has `cores` online CPUs:
    the arms decide the runner from the selection alone unless they say
    otherwise. (A `fab test` node exports FAB_ID and FAB_JOBS into the very
    process these arms run in.)"""
    patch = mock.patch.dict(os.environ)
    patch.start()
    case.addCleanup(patch.stop)
    for key in (gate.FAB_JOB_ENV, gate.FAB_CORES_ENV):
        os.environ.pop(key, None)
    cpus = mock.patch.object(gate, "_online_cpu_count", return_value=cores)
    cpus.start()
    case.addCleanup(cpus.stop)


class SlicedFocusBase(_focus.FocusBase):
    """FocusBase's lane (it changed pkg/alpha.py) over a base that also holds
    six more consumers of pkg.alpha and ten strangers: the selection is eight
    modules of nineteen, over the slicing floor and under half the universe."""

    CONSUMERS = 6
    STRANGERS = 10

    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.tmp, "a-stranger-was-imported")
        self._git("checkout", "-q", "main")
        for n in range(self.CONSUMERS):
            self._write("tests/test_alpha_%d.py" % n, CONSUMER)
        for n in range(self.STRANGERS):
            self._write("tests/test_stranger_%d.py" % n,
                        STRANGER % self.marker)
        self._git("add", "-A")
        self._git("commit", "-qm", "more consumers and strangers")
        self._git("checkout", "-q", "lane/focus")
        self._git("rebase", "-q", "main")
        self.head = self._git("rev-parse", "HEAD")
        _outside_fab(self)
        _direct_child(self)

    def focused(self):
        """-> (row, err, stderr text) of one real `gate.run(focus=True)`."""
        err_text = io.StringIO()
        with contextlib.redirect_stderr(err_text):
            row, err = gate.run(repo=self.repo, focus=True)
        return row, err, err_text.getvalue()

    def minted(self):
        row, err, text = self.focused()
        self.assertIsNone(err, "%s\n%s" % (err, text))
        return row, text

    def lane_commit(self, rel, body, message):
        self._write(rel, body)
        self._git("add", "-A")
        self._git("commit", "-qm", message)
        self.head = self._git("rev-parse", "HEAD")


class SlicedRunArms(SlicedFocusBase):
    def test_a_large_selection_runs_as_slices_and_records_module_seconds(self):
        row, text = self.minted()
        focus = row["focus"]
        selected = focus["selected"]
        self.assertEqual(len(selected), 8, selected)
        self.assertEqual(focus["universe"], 19)
        # THE RUNNER: this helm's slice runner over exactly the selection.
        cmd = self.spawned[0]["cmd"]
        self.assertEqual(cmd[1:3], [os.path.realpath(gateslice.__file__),
                                    gateslice.MODULES_FLAG])
        self.assertEqual(cmd[3:], selected)
        self.assertEqual(row["argv"], cmd)
        env = self.spawned[0]["env"]
        self.assertEqual((env["HELM_GATESLICE_LEAKS"],
                          env["HELM_GATE_SUITE_CAP"]), ("report", "1"))
        self.assertGreater(int(env["HELM_GATESLICE_WORKERS"]), 1)
        self.assertIn("executes as SLICES", text)
        # THE SAME KIND, THE SAME RAN SET: v6, the protocol read.
        self.assertEqual((row["v"], row["suite"], row["status"], row["ran"]),
                         (gate.FOCUSED_VERSION, False, "OK", 8), row)
        self.assertEqual(focus["executed"], selected)
        self.assertEqual(focus["executed_ids"], 8)
        # MORE THAN ONE WORKER, AND EVERY MODULE TIMED.
        block = focus["slice"]
        self.assertGreater(block["workers"], 1, block)
        self.assertEqual((block["leak_mode"], block["leaks"], block["planned"]),
                         ("report", 0, 8))
        self.assertEqual(len(block["seconds"]), len(selected))
        self.assertTrue(all(isinstance(s, float) and s >= 0
                            for s in block["seconds"]), block["seconds"])
        # ONLY THE SELECTION WAS LOADED: no stranger was ever imported, and
        # the mark is one an import DOES leave (the control below).
        self.assertFalse(os.path.exists(self.marker),
                         "the focused slices walked the whole discovery")
        subprocess.run([sys.executable, "-c", "import tests.test_stranger_0"],
                       cwd=self.repo, check=True, timeout=60)
        self.assertTrue(os.path.exists(self.marker))
        # THE ID BINDS THE EVIDENCE, and the fix bar still verifies it.
        self.assertEqual(gate.by_id(row["id"])[0]["focus"]["slice"], block)
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            gate._show_focus(row)
        self.assertIn("    slices    %d workers, " % block["workers"],
                      shown.getvalue())
        self.assertIn("    slowest   tests.", shown.getvalue())
        state, rid, why = gate.bind("gate:%s" % row["id"], self.head,
                                    repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)
        # THE NEXT ROUND READS THESE SECONDS: they are what the focused
        # schedule is handed, and the next sliced round schedules by them.
        recorded = gate.recorded_module_seconds()
        self.assertEqual({m: recorded[m] for m in selected},
                         dict(zip(selected, block["seconds"])))
        work = tempfile.mkdtemp(prefix="helm-test-focus-timings-")
        self.addCleanup(__import__("shutil").rmtree, work, True)
        with open(gate._slice_timings(work, focused=True)) as fh:
            self.assertLessEqual(set(selected), set(json.load(fh)))
        self.assertNotIn("HELM_GATESLICE_TIMINGS", env)
        self.lane_commit("pkg/alpha.py", "X = 1\nZ = 3\n", "again")
        again, _text = self.minted()
        self.assertIn("HELM_GATESLICE_TIMINGS", self.spawned[1]["env"])
        self.assertEqual(again["focus"]["slice"]["schedule"],
                         "recorded-longest-first")

    def test_a_failing_module_in_one_worker_fails_the_run_and_is_named(self):
        self.lane_commit("tests/test_alpha_3.py", FAILING, "one module fails")
        row, err, text = self.focused()
        self.assertIsNone(err, "%s\n%s" % (err, text))
        self.assertEqual(self.spawned[0]["cmd"][2], gateslice.MODULES_FLAG)
        self.assertEqual((row["status"], row["rc"]), ("FAILED", 1), row)
        self.assertEqual([f["test"] for f in row["failures"]],
                         ["tests.test_alpha_3.T.test_a"])
        # One worker's module failed; the others' still ran and reported.
        self.assertGreater(row["focus"]["slice"]["workers"], 1)
        self.assertEqual(row["focus"]["executed"], row["focus"]["selected"])
        self.assertEqual(row["ran"], 8)
        state, _rid, why = gate.bind("gate:%s" % row["id"], self.head,
                                     repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("FAILED", why)

    def test_a_leaking_module_is_named_and_the_report_mode_round_cannot_bind(self):
        """Report mode preserves unittest's own protocol while the separate
        gateshard leak cure is composed, but its finding is never advisory."""
        self.lane_commit("tests/test_alpha_2.py", LEAKING, "one module leaks")
        row, err, text = self.focused()
        self.assertIsNone(err, "%s\n%s" % (err, text))
        self.assertEqual(self.spawned[0]["cmd"][2], gateslice.MODULES_FLAG)
        self.assertEqual((row["status"], row["rc"], row["ran"]),
                         ("UNKNOWN", 0, 8), row)
        block = row["focus"]["slice"]
        self.assertEqual((block["leak_mode"], block["leaks"]), ("report", 1))
        self.assertIn("1 selected module left process state", text)
        self.assertIn("tests.test_alpha_2", text)
        self.assertIn("fresh workers can hide the serial state", row["detail"])
        state, _rid, why = gate.bind("gate:%s" % row["id"], self.head,
                                     repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("reported 1 leaking module", why)
        # A row minted by the reviewed tip before this cure can say OK/0 while
        # carrying the same report census. The canonical stored-row reader
        # refuses that old shape too, rather than protecting only new mints.
        crafted = self.reresolve(row, status="OK", detail="")
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(crafted) + "\n")
        state, _rid, why = gate.bind("gate:%s" % crafted["id"], self.head,
                                     repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("reported 1 leaking module", why)

    def test_serial_red_from_a_leak_and_its_consumer_cannot_be_sliced_green(self):
        producer = os.path.join(self.tmp, "producer.ready")
        consumer = os.path.join(self.tmp, "consumer.ready")
        self._write("tests/test_alpha_2.py",
                    LEAKING_SYNC % (producer, consumer))
        self._write("tests/test_alpha_3.py",
                    LEAK_CONSUMER_SYNC % (consumer, producer))
        self._git("add", "-A")
        self._git("commit", "-qm", "producer leaks into downstream consumer")
        self.head = self._git("rev-parse", "HEAD")
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        # THE SERIAL CHILD'S CONTRACT, not this process's environment: run
        # inside a slice worker, this process carries HELM_GATESLICE_WORKER,
        # and a serial run handed it would take the fixtures' two-worker
        # branch and fail the producer too. The marker is planted here so
        # the arm reads the same under either runner.
        with mock.patch.dict(os.environ, {"HELM_GATESLICE_WORKER": "1"}):
            env = gateslice.scrubbed_env(os.environ)
        env.pop("FOCUS_SLICE_LEFT_BEHIND", None)
        serial = subprocess.run(
            [gate.interpreter()["executable"], "-m", "unittest", "-v"]
            + plan["selected"], cwd=self.repo, env=env, capture_output=True,
            text=True, timeout=120)
        parsed = gate.parse_result(serial.stderr)
        self.assertEqual((serial.returncode, parsed["status"]), (1, "FAILED"),
                         serial.stderr)
        self.assertEqual([f["test"] for f in parsed["failures"]],
                         ["tests.test_alpha_3.T.test_a"])

        row, err, text = self.focused()
        self.assertIsNone(err, "%s\n%s" % (err, text))
        self.assertEqual(self.spawned[0]["cmd"][2], gateslice.MODULES_FLAG)
        # The consumer runs clean on its fresh worker and unittest exits zero;
        # the producer's report census is the fail-closed signal that prevents
        # that cross-worker split from minting a green focused receipt.
        self.assertEqual((row["status"], row["rc"], row["failures"]),
                         ("UNKNOWN", 0, []), row)
        block = row["focus"]["slice"]
        self.assertEqual((block["leak_mode"], block["leaks"]), ("report", 1))
        self.assertIn("tests.test_alpha_2", text)
        state, _rid, why = gate.bind("gate:%s" % row["id"], self.head,
                                     repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("reported 1 leaking module", why)

    def test_two_modules_execute_concurrently_in_distinct_worker_processes(self):  # noqa: VACUOUS_ASSERTION — both barrier tests must complete before mint returns OK, and each publishes a PID the parent reads and compares
        first = os.path.join(self.tmp, "barrier-first.pid")
        second = os.path.join(self.tmp, "barrier-second.pid")
        self._write("tests/test_alpha.py", BARRIER % (first, second))
        self._write("tests/test_alpha_0.py", BARRIER % (second, first))
        self._git("add", "-A")
        self._git("commit", "-qm", "two modules meet at a worker barrier")
        self.head = self._git("rev-parse", "HEAD")
        row, text = self.minted()
        self.assertEqual(row["status"], "OK", (row, text))
        self.assertGreater(row["focus"]["slice"]["workers"], 1)
        with open(first, encoding="ascii") as fh:
            first_pid = int(fh.read())
        with open(second, encoding="ascii") as fh:
            second_pid = int(fh.read())
        self.assertNotEqual(first_pid, second_pid)

    def test_the_tree_is_read_before_and_after_the_sliced_run(self):
        def touch():
            with open(os.path.join(self.repo, ".gitignore"), "a") as fh:
                fh.write("# the run moved the tree\n")
        self.before_child = touch
        row, err, text = self.focused()
        self.assertIsNone(err, "%s\n%s" % (err, text))
        self.assertEqual(self.spawned[0]["cmd"][2], gateslice.MODULES_FLAG)
        self.assertEqual(row["status"], "OK", row)
        self.assertEqual((row["dirty"], row["dirty_after"]), (False, True))
        self.assertEqual(row["tree"], self._git("rev-parse", "HEAD^{tree}"))
        state, _rid, why = gate.bind("gate:%s" % row["id"], self.head,
                                     repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED")
        self.assertIn("DIRTY", why)


class SerialFallbackArms(SlicedFocusBase):
    def serial_argv(self, row):
        return [gate.interpreter()["executable"], "-m", "unittest",
                "-v"] + row["focus"]["selected"]

    def test_a_host_that_grants_no_slots_runs_serial_and_says_why(self):
        refused = gate.CapacityRefusal(
            "this host's whole-suite cap is 2 (carries agent panes) and 2 are "
            "already running — REFUSED")
        with mock.patch.object(gate, "_admit_suite",
                               return_value=({"cap": 2, "reason": "x"},
                                             refused)) as admit:
            row, text = self.minted()
        self.assertEqual(admit.call_args.kwargs["slots"], 4)
        self.assertEqual(self.spawned[0]["cmd"], self.serial_argv(row))
        self.assertNotIn("slice", row["focus"])
        self.assertEqual((row["status"], row["ran"]), ("OK", 8))
        self.assertIn("executes SERIAL", text)
        self.assertIn("granted no slots", text)
        self.assertIn("whole-suite cap is 2", text)

    def test_a_fab_job_granted_too_few_cores_runs_serial_and_says_why(self):
        os.environ.update({gate.FAB_JOB_ENV: "lane-x-1", gate.FAB_CORES_ENV: "3"})
        row, text = self.minted()
        self.assertEqual(self.spawned[0]["cmd"], self.serial_argv(row))
        self.assertIn("executes SERIAL", text)
        self.assertIn("Fab job granted 3 cores", text)
        # The grant a sliced dispatch asks for is honoured, never exceeded.
        os.environ[gate.FAB_CORES_ENV] = "4"
        self.lane_commit("pkg/alpha.py", "X = 1\nZ = 4\n", "again")
        row, text = self.minted()
        self.assertEqual(self.spawned[1]["cmd"][2], gateslice.MODULES_FLAG)
        workers = row["focus"]["slice"]["workers"]
        self.assertGreater(workers, 1)
        self.assertLessEqual(workers, 4)
        self.assertEqual(self.spawned[1]["env"]["HELM_GATESLICE_WORKERS"], "4")

    def test_the_plan_answer_names_the_runner_and_sizes_its_grant(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = gate._cmd_run(["--repo", self.repo, "--focus", "--plan",
                                "--json"])
        self.assertEqual(rc, 0)
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["mode"], gate.SLICED)
        self.assertEqual(answer["slice"], {
            "runner": gate.SLICE_RUNNER, "workers": 8, "slots": 4,
            "modules": 8})


class SmallSelectionArms(_focus.FocusBase):
    def setUp(self):
        super().setUp()
        _outside_fab(self)
        _direct_child(self)

    def test_a_small_selection_runs_serial_and_says_why(self):
        err_text = io.StringIO()
        with contextlib.redirect_stderr(err_text):
            row, err = gate.run(repo=self.repo, focus=True)
        text = err_text.getvalue()
        self.assertIsNone(err, "%s\n%s" % (err, text))
        self.assertEqual(self.spawned[0]["cmd"],
                         [gate.interpreter()["executable"], "-m", "unittest",
                          "-v", "tests.test_alpha", "tests.test_beta"])
        self.assertNotIn("slice", row["focus"])
        self.assertIn("executes SERIAL", text)
        self.assertIn("2 selected modules: under %d"
                      % gate.FOCUS_SLICE_MIN_MODULES, text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            gate._cmd_run(["--repo", self.repo, "--focus", "--plan", "--json"])
        answer = json.loads(out.getvalue())
        self.assertEqual((answer["mode"], answer["slice"]), (gate.SERIAL, None))
        self.assertIn("under %d" % gate.FOCUS_SLICE_MIN_MODULES,
                      answer["slice_reason"])


class SlicedBindingArms(SlicedFocusBase):
    """The focused kind's binding rules, unchanged by the runner: a sliced
    focused receipt binds a cure-round FIX and nothing that spends land
    authority."""

    def _dispatch(self, tip):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True,
                                  new_work=True)
        self.assertIsNone(err, err)
        return row

    def test_a_sliced_focused_receipt_binds_a_fix_and_never_an_approve_or_a_land(self):
        row, _text = self.minted()
        self.assertEqual(self.spawned[0]["cmd"][2], gateslice.MODULES_FLAG)
        self.assertEqual((row["v"], row["suite"]),
                         (gate.FOCUSED_VERSION, False))
        self.assertIn("slice", row["focus"])
        token = "gate:%s" % row["id"]
        self.assertEqual(gate.bind(token, self.head, repo_id=self.repo,
                                   need=gate.NEED_FOCUSED)[0], "VERIFIED")
        for need in (gate.NEED_SUITE, gate.NEED_LAND):
            state, _rid, why = gate.bind(token, self.head, repo_id=self.repo,
                                         need=need)
            self.assertEqual(state, "REFUSED", need)
            self.assertIn("FOCUSED", why)
        # The review chokepoint: an APPROVE never records, a FIX binds.
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("FOCUSED", err)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row) + " 1 blocker", "fix")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.gate_state(got), "VERIFIED " + row["id"])
        # Both doors that read a receipt as land authority directly.
        state, why = landgate.gate_binds_tree(row["id"], None, repo=self.repo,
                                              tip=self.head)
        self.assertEqual(state, landgate.REFUSE, why)
        self.assertIn("FOCUSED", why)
        rung = foldcheck._tree_matches_gate(
            vcs.backend(self.repo), self.repo, self.head, token)
        self.assertEqual(rung.verdict, foldcheck.REFUSE)
        # Re-flagged as a whole suite, the row is refused by the row
        # predicate whatever evidence its focus block carries.
        crafted = self.reresolve(row, suite=True)
        self.assertIn("is the focused kind (v6)", gate.row_refusal(crafted))


class SlicedFocusCensusArms(CapBase):
    """A sliced focused run holds no FIFO position, so its own intent carries
    its grant: every other admission prices it at the slots it holds."""

    TOPOLOGY = {"cores": 32, "reason": "32 host-online CPUs"}
    TOKEN = "f" * 32

    def test_the_focused_runner_is_a_suite_occupant_priced_at_its_grant(self):  # noqa: VACUOUS_ASSERTION — each absence sits beside a positive on its own subject: the full argv is a suite root, the grant is 4 slots with its intent row, and occupancy reads 4 before the release empties the ledger
        runner = ["python3", "/lane/helm/gateslice.py", gateslice.MODULES_FLAG,
                  "tests.test_a", "tests.test_b"]
        self.assertEqual(gate._suite_root_kind(runner), "suite")
        self.assertIsNone(gate._suite_root_kind(runner[:3]))
        self.assertFalse(gate._suite_shaped(runner))
        self.paneless()
        self.launcher(501)
        err = self.admit(topology=self.TOPOLOGY, slots=4, occupant={
            "id": self.TOKEN, "pid": 501, "starttime": 7, "holder": "focus"})
        self.assertIsNone(err, err)
        self.assertEqual(self.capacity["slots"], 4)
        self.assertEqual([(r["position"], r.get("slots"))
                          for r in self.admission_rows()], [(self.TOKEN, 4)])
        # Pending, then running: its root in the guard's cgroup is the same
        # occupant, never a second one.
        self.assertEqual(gate.suite_occupancy(
            proc_dir=self.proc, admissions_path=self.admissions), 4)
        self.plant(502, runner, ppid=501,
                   cgroup="helm-gate-%s" % self.TOKEN)
        self.assertEqual(gate.suite_occupancy(
            proc_dir=self.proc, admissions_path=self.admissions), 4)
        self.assertIsNone(gate._admission_release(
            self.TOKEN, admissions_path=self.admissions))
        self.assertEqual(self.admission_rows(), [])


class ModulesFlagArms(unittest.TestCase):
    def test_the_modules_scope_takes_distinct_dotted_names_only(self):  # noqa: VACUOUS_ASSERTION — every refusal asserts exit 2 and the usage line; a valid list starts the runner, which only its own process may do, and SlicedRunArms drives a valid list through the gate
        for argv in ([gateslice.MODULES_FLAG],
                     [gateslice.MODULES_FLAG, "tests.a", "tests.a"],
                     [gateslice.MODULES_FLAG, "-v"],
                     [gateslice.MODULES_FLAG, "tests/test_a.py"]):
            with self.subTest(argv=argv), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(gateslice.main(argv), 2)
            self.assertIn("usage", err.getvalue())


def setUpModule():
    """No dispatch row this module writes walks the host's process table."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
