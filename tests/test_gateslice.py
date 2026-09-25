"""gateslice: parallel slices of ONE serial discovery, judged against serial.

Every arm builds a small repository of planted test modules and runs both the
literal serial command and the real slice workers against it. The claims are
comparative, so each arm asserts the SERIAL outcome first: an arm whose serial
half did not behave as planted would compare two runs of nothing.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from helm import gate, gateslice


_BOOTSTRAP = """
    import atexit, os, shutil, tempfile
    _ROOT = tempfile.mkdtemp(prefix="gateslice-fixture-root-")
    atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
    for _name, _leaf in (("HELM_CONFIG_ROOTS", "configs"),
                         ("HELM_HOME", "home"), ("HELM_CHAT_DIR", "chat")):
        _path = os.path.join(_ROOT, _leaf)
        os.makedirs(_path)
        os.environ[_name] = _path
    os.environ["HELM_METAHARNESS"] = "none"
"""


class _SliceHarness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        os.makedirs(os.path.join(self.repo, "tests"))
        self.write("__init__", _BOOTSTRAP)
        patch = mock.patch.dict(os.environ, {"HELM_GATESLICE_LEAKS": "report"})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, source):
        with open(os.path.join(self.repo, "tests", name + ".py"), "w",
                  encoding="utf-8") as fh:
            fh.write(textwrap.dedent(source))
        return "tests." + name

    def serial(self):
        proc = subprocess.run([sys.executable] + list(gate.SUITE),
                              cwd=self.repo, capture_output=True, text=True,
                              timeout=60)
        return gate.parse_result(proc.stderr), proc.stderr

    def sliced(self, workers=2, leaks="report", seconds=None):
        """The real runner, as the gate launches it: its own process, which
        arms the subreaper and sweeps only its own descendants.
        -> (parsed, text, rc, {worker: [module labels in run order]} | None)
        and leaves the run's evidence (or None) on self.evidence."""
        work = tempfile.mkdtemp(prefix="gateslice-arm-")
        self.addCleanup(__import__("shutil").rmtree, work, True)
        keep = os.path.join(work, "summary.json")
        evidence = os.path.join(work, "evidence.json")
        env = dict(os.environ, HELM_GATESLICE_LEAKS=leaks,
                   HELM_GATESLICE_WORKERS=str(workers), HELM_GATE_SUITE_CAP="1",
                   HELM_GATESLICE_KEEP=keep, HELM_GATESLICE_EVIDENCE=evidence)
        env.pop("HELM_GATESLICE_TIMINGS", None)
        if seconds is not None:
            timings = os.path.join(work, "timings.json")
            with open(timings, "w", encoding="utf-8") as fh:
                json.dump(seconds, fh)
            env["HELM_GATESLICE_TIMINGS"] = timings
        proc = subprocess.run(
            [sys.executable, os.path.abspath(gateslice.__file__)],
            cwd=self.repo, env=env, capture_output=True, text=True,
            timeout=300)
        summary = self._read(keep)
        self.evidence = self._read(evidence)
        self.first_line = proc.stderr.split("\n", 1)[0]
        assignment = None if summary is None else {
            worker: [row["label"] for row in meta["claimed"]]
            for worker, meta in summary["workers"].items()}
        # THE MARKER STAYS OUT OF EVERY ASSERTION MESSAGE. An arm that fails
        # prints this text, and a marker line in the OUTER suite's output
        # would make the gate refuse to mint that suite's (red) receipt.
        text = "\n".join(line for line in proc.stderr.split("\n")
                         if line != gateslice.DIAGNOSTIC_MARKER)
        return gate.parse_result(text), text, proc.returncode, assignment

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except OSError:
            return None


class HostCensusTest(unittest.TestCase):
    def test_a_running_parent_holds_a_suite_slot_and_never_authority(self):
        parent = [sys.executable, os.path.abspath(gateslice.__file__)]
        self.assertEqual(gate._suite_root_kind(parent), "suite")
        self.assertFalse(gate._suite_shaped(parent))
        # Workers are the parent's children, never roots of their own.
        self.assertIsNone(gate._suite_root_kind(
            parent + ["--worker", "0", "/tmp/work"]))
        self.assertFalse(gate._diagnostic_shard_shaped(
            [sys.executable, "helm/gateslice.py"]))
        self.assertFalse(gate._diagnostic_shard_shaped(
            [sys.executable, "/elsewhere/gateslice.py"]))


class DiscoveryStateTest(_SliceHarness):
    """The withdrawal's objection, pinned in both directions: state an
    import creates is visible to every module, so slices must see it too."""

    def test_an_import_time_leak_fails_serial_AND_every_slice_run(self):
        self.write("test_alpha", """
            import builtins, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    self.assertFalse(getattr(builtins, "_late_tripwire", False))
        """)
        self.write("test_z_late", """
            import builtins, unittest
            builtins._late_tripwire = True
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        serial, _text = self.serial()
        self.assertEqual(serial["status"], "FAILED")
        for workers in (1, 2, 3):
            parsed, text, rc, _a = self.sliced(workers)
            self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
            self.assertEqual([row["test"] for row in parsed["failures"]],
                             ["tests.test_alpha.Probe.test_probe"])

    def test_an_import_time_dependency_passes_serial_AND_every_slice_run(self):
        self.write("test_alpha", """
            import builtins, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    self.assertTrue(getattr(builtins, "_late_ready", False))
        """)
        self.write("test_z_late", """
            import builtins, unittest
            builtins._late_ready = True
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        serial, _text = self.serial()
        self.assertEqual((serial["status"], serial["ran"]), ("OK", 2))
        for workers in (1, 2, 3):
            parsed, text, rc, _a = self.sliced(workers)
            self.assertEqual((rc, parsed["status"], parsed["ran"]),
                             (0, "OK", 2), text)
        self.assertIn("gateslice: 3 workers, 3 units, 2 planned tests", text)


class InventoryTest(_SliceHarness):
    def _plant_mixed_suite(self):
        self.write("test_a", """
            import unittest
            class Plain(unittest.TestCase):
                def test_one(self): pass
                def test_two(self):
                    for i in range(3):
                        with self.subTest(i=i): pass
                @unittest.expectedFailure
                def test_expected(self): self.fail("expected")
        """)
        self.write("test_b", """
            import unittest
            @unittest.skip("whole class")
            class Skipped(unittest.TestCase):
                def test_x(self): pass
                def test_y(self): pass
            class Setup(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("skipped by setup")
                def test_z(self): pass
        """)
        self.write("test_c", """
            import unittest
            from tests.test_a import Plain
            class Own(unittest.TestCase):
                def test_own(self): self.skipTest("inline")
        """)
        self.write("test_d", """
            import unittest
            class Own(unittest.TestCase):
                def test_d(self): pass
        """)

    def test_counts_and_verdict_equal_serial_and_every_unit_runs_once(self):
        self._plant_mixed_suite()
        serial, text = self.serial()
        self.assertEqual(serial["status"], "OK", text)
        for workers in (1, 2, 4):
            parsed, out, rc, assignment = self.sliced(workers)
            self.assertEqual(rc, 0, out)
            for key in ("status", "ran", "skipped", "detail"):
                self.assertEqual(parsed[key], serial[key], (key, out))
            units = sorted(label for labels in assignment.values()
                           for label in labels)
            # The package itself is a discovery unit with no tests.
            self.assertEqual(units, ["tests", "tests.test_a", "tests.test_b",
                                     "tests.test_c", "tests.test_d"])
            self.assertIn("gateslice: %d workers, 5 units, " % workers, out)

    def test_the_reference_name_is_never_bound_to_a_partial_write(self):
        """A second worker reads the reference by NAME, so the name may only
        ever reach complete content. Observed from inside the write: while
        the inventory bytes are being written, the name is unbound. A second
        publisher then agrees or disagrees with the complete first one."""
        work = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, work, True)
        reference = os.path.join(work, "inventory.json")
        seen = []
        real_dump = json.dump

        def watching(obj, fh, *args, **kwargs):
            seen.append(os.path.exists(reference))
            return real_dump(obj, fh, *args, **kwargs)

        rows = [["tests.test_a", ["tests.test_a.T.test_x"]]]
        with mock.patch.object(gateslice.json, "dump", watching):
            self.assertTrue(gateslice.publish_reference(work, 0, "d1", rows))
            self.assertTrue(gateslice.publish_reference(work, 1, "d1", rows))
            self.assertFalse(gateslice.publish_reference(work, 2, "d2", rows))
        # Positive control: the watcher saw all three writes, so the absence
        # it reports for the first one was looked for, not assumed.
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0], False)
        with open(reference, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"digest": "d1", "rows": rows})
        self.assertEqual(sorted(os.listdir(work)), ["inventory.json"])

    def test_workers_that_discover_different_inventories_are_refused(self):
        self.write("test_a", """
            import unittest
            class Own(unittest.TestCase):
                def test_a(self): pass
        """)
        marker = os.path.join(self.repo, "first-importer")
        self.write("test_b", """
            import os, unittest
            class Own(unittest.TestCase):
                def test_b(self): pass
            try:
                os.close(os.open(%r, os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                pass
            else:
                Own.test_only_for_the_first_importer = lambda self: None
        """ % marker)
        parsed, text, rc, assignment = self.sliced(2)
        self.assertEqual(rc, 2, text)
        self.assertEqual(parsed["status"], "UNKNOWN", text)
        self.assertIn("different test inventories", text)
        self.assertIsNone(assignment)

    def test_a_dead_worker_is_unknown_and_named_never_a_verdict(self):
        self.write("test_a", """
            import unittest
            class Own(unittest.TestCase):
                def test_a(self): pass
        """)
        self.write("test_b", """
            import os, unittest
            class Own(unittest.TestCase):
                def test_b(self): os._exit(9)
        """)
        parsed, text, rc, _a = self.sliced(2)
        self.assertEqual(rc, 1, text)
        self.assertEqual(parsed["status"], "UNKNOWN", text)
        self.assertIn("UNKNOWN: no validated result from worker", text)
        self.assertIsNone(self.evidence, "an UNKNOWN run left evidence")

    def test_a_dead_workers_detached_child_does_not_outlive_the_run(self):
        """A worker sweeps its own descendants in a `finally`, and os._exit
        skips it. What that worker had contained as subreaper then belongs
        to the runner, which must sweep it rather than leave it running."""
        record = os.path.join(self.repo, "orphan")
        self.write("test_orphan", """
            import os, subprocess, sys, unittest
            class Own(unittest.TestCase):
                def test_orphans_then_dies(self):
                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(120)"],
                        start_new_session=True)
                    with open("/proc/%%d/stat" %% child.pid) as fh:
                        start = fh.read().rsplit(")", 1)[1].split()[19]
                    with open(%r, "w") as fh:
                        fh.write("%%d %%s" %% (child.pid, start))
                    os._exit(9)
        """ % record)
        self.write("test_plain", """
            import unittest
            class Own(unittest.TestCase):
                def test_plain(self): pass
        """)
        parsed, text, _rc, _a = self.sliced(2)
        with open(record, encoding="utf-8") as fh:
            pid, start = fh.read().split()
        pid = int(pid)
        self.addCleanup(self._kill_if_alive, pid, start)
        self.assertEqual(parsed["status"], "UNKNOWN", text)
        self.assertFalse(self._alive(pid, start),
                         "the dead worker's detached child outlived the run")

    @staticmethod
    def _alive(pid, start):
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            return fields[19] == start and fields[0] not in ("Z", "X")
        except OSError:
            return False

    def _kill_if_alive(self, pid, start):
        if self._alive(pid, start):
            os.kill(pid, 9)


class LoaderUntouchedTest(_SliceHarness):
    def test_a_module_reading_the_loader_at_import_sees_what_serial_shows(self):
        """Serial discovery never touches the default loader, so the slices
        may not either: a module that inspects it while being imported must
        read the same thing under both, or its verdict can differ while every
        test id still agrees."""
        self.write("test_loader_probe", """
            import unittest
            SEEN = sorted(vars(unittest.defaultTestLoader))
            class Probe(unittest.TestCase):
                def test_loader_untouched(self):
                    self.assertNotIn("loadTestsFromModule", SEEN)
        """)
        serial, text = self.serial()
        self.assertEqual((serial["status"], serial["ran"]), ("OK", 1), text)
        parsed, text, rc, _a = self.sliced(2)
        self.assertEqual((rc, parsed["status"], parsed["ran"]),
                         (0, "OK", 1), text)


class DiscoveryWalkTest(_SliceHarness):
    """The parent names the modules without importing anything; real
    discovery in every worker must produce units that line up with them."""

    def _plant_tree(self):
        for rel, body in (
                ("tests/helper.py", "X = 1\n"),
                ("tests/test-bad.py", "raise SystemExit('never imported')\n"),
                ("tests/nopkg/test_d.py", "raise SystemExit('never imported')\n"),
                ("tests/sub/__init__.py", ""),
                ("tests/sub/test_c.py", _CASE % "c"),
                ("tests/test_a.py", _CASE % "a"),
                ("tests/test_b.py", _CASE % "b")):
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)

    def test_the_walk_names_exactly_the_modules_discovery_loads(self):
        self._plant_tree()
        names = gateslice.discovery_modules(
            gateslice.working_tree_files(self.repo))
        self.assertEqual(names, ["tests", "tests.sub", "tests.sub.test_c",
                                 "tests.test_a", "tests.test_b"])
        serial, text = self.serial()
        self.assertEqual((serial["status"], serial["ran"]), ("OK", 3), text)
        parsed, text, rc, assignment = self.sliced(2)
        self.assertEqual((rc, parsed["status"], parsed["ran"]),
                         (0, "OK", 3), text)
        self.assertEqual(sorted(label for labels in assignment.values()
                                for label in labels), sorted(names))

    def test_the_same_walk_answers_over_a_listing_with_no_filesystem(self):
        listing = ["tests/__init__.py", "tests/sub/__init__.py",
                   "tests/sub/test_c.py", "tests/test_b.py", "tests/test_a.py",
                   "tests/nopkg/test_d.py", "tests/helper.py",
                   "tests/test-bad.py"]
        self.assertEqual(gateslice.discovery_modules(listing),
                         ["tests", "tests.sub", "tests.sub.test_c",
                          "tests.test_a", "tests.test_b"])
        self.assertEqual(gateslice.discovery_modules(["tests/test_a.py"]), [])


_CASE = """import unittest
class Case(unittest.TestCase):
    def test_%s(self): pass
"""


class ScheduleTest(_SliceHarness):
    def test_longest_first_places_every_unit_once_in_ascending_order(self):
        labels = ["m%d" % i for i in range(10)]
        seconds = {label: 1.0 for label in labels}
        seconds.update({"m0": 9.0, "m3": 8.0, "m9": 7.0})
        plan = gateslice.schedule(labels, seconds, 3)
        placed = sorted(i for indices in plan.values() for i in indices)
        self.assertEqual(placed, list(range(10)))
        for indices in plan.values():
            self.assertEqual(indices, sorted(indices))
        # The three heaviest units start on three different workers, and the
        # longest-first rule leaves no worker more than one unit heavier.
        owners = {i: w for w, indices in plan.items() for i in indices}
        self.assertEqual(len({owners[0], owners[3], owners[9]}), 3)
        loads = [sum(seconds[labels[i]] for i in indices)
                 for indices in plan.values()]
        self.assertLessEqual(max(loads) - min(loads), 9.0)

    def test_an_unrecorded_unit_is_weighed_at_the_median(self):
        plan = gateslice.schedule(["a", "b", "c"], {"a": 1.0, "b": 5.0}, 2)
        owners = {i: w for w, indices in plan.items() for i in indices}
        # c is estimated at 5.0, so it cannot share b's worker.
        self.assertNotEqual(owners[1], owners[2])

    def test_a_scheduled_run_follows_its_plan_and_says_so(self):
        for name in ("a", "b", "c", "d"):
            self.write("test_" + name, _CASE % name)
        seconds = {"tests.test_a": 5.0, "tests.test_b": 4.0}
        parsed, text, rc, assignment = self.sliced(2, seconds=seconds)
        self.assertEqual((rc, parsed["status"], parsed["ran"]),
                         (0, "OK", 4), text)
        labels = ["tests", "tests.test_a", "tests.test_b", "tests.test_c",
                  "tests.test_d"]
        plan = gateslice.schedule(labels, seconds, 2)
        self.assertEqual(
            {w: [labels[i] for i in indices] for w, indices in plan.items()},
            assignment)
        self.assertEqual(self.evidence["schedule"], "recorded-longest-first")


class EvidenceTest(_SliceHarness):
    def test_a_readable_run_leaves_evidence_that_matches_its_protocol(self):
        for name in ("a", "b", "c"):
            self.write("test_" + name, _CASE % name)
        parsed, text, rc, assignment = self.sliced(2, "fail")
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        ev = self.evidence
        labels = gateslice.discovery_modules(
            gateslice.working_tree_files(self.repo))
        self.assertEqual(ev["v"], gateslice.EVIDENCE_VERSION)
        self.assertEqual((ev["workers"], ev["units"], ev["planned"]),
                         (2, len(labels), 3))
        self.assertEqual(ev["modules_digest"],
                         gateslice.canonical_digest(labels))
        self.assertEqual((ev["leak_mode"], ev["leaks"], ev["swept"]),
                         ("fail", 0, "empty-after-exit"))
        self.assertEqual(sorted(ev["seconds"]), sorted(labels))
        self.assertEqual(ev["outcome"]["ran"], parsed["ran"])
        self.assertTrue(ev["outcome"]["ok"])
        self.assertEqual(ev["schedule"], "ascending-claim")
        self.assertRegex(ev["inventory_digest"], r"\A[0-9a-f]{64}\Z")
        self.assertRegex(ev["assignment_digest"], r"\A[0-9a-f]{64}\Z")

    def test_a_workers_env_carries_only_the_contract_keys_a_worker_reads(self):
        """A test that starts a nested runner with dict(os.environ) must not
        hand it the OUTER run's evidence, summary or timings paths."""
        self.write("test_env", """
            import json, os, unittest
            class Env(unittest.TestCase):
                def test_records(self):
                    with open("worker-env.json", "w") as fh:
                        json.dump(sorted(k for k in os.environ
                                         if k.startswith("HELM_GATESLICE_")), fh)
        """)
        parsed, text, rc, _a = self.sliced(1, "fail", seconds={"tests": 0.1})
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertIsNotNone(self.evidence, text)
        with open(os.path.join(self.repo, "worker-env.json")) as fh:
            seen = json.load(fh)
        self.assertEqual(seen, ["HELM_GATESLICE_LEAKS", "HELM_GATESLICE_WORKER",
                                "HELM_GATESLICE_WORKERS"])

    def test_a_class_skipped_in_setup_leaves_ran_below_planned_on_a_green_run(self):
        """Pins why v10 does NOT refuse `ok and ran != planned`: a class
        whose setUpClass skips records one skip and never starts its tests,
        so a green run legitimately runs fewer tests than discovery planned
        (measured on the build host: planned 22009, ran 21998, 11 tests in 5
        classes skipped by setup)."""
        self.write("test_skip", """
            import unittest
            class Skipped(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise unittest.SkipTest("not here")
                def test_one(self): pass
                def test_two(self): pass
            class Runs(unittest.TestCase):
                def test_three(self): pass
        """)
        parsed, text, rc, _a = self.sliced(1, "fail")
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        outcome = self.evidence["outcome"]
        self.assertTrue(outcome["ok"])
        self.assertEqual((self.evidence["planned"], outcome["ran"],
                          outcome["skipped"]), (3, 1, 1))




class MarkerTest(_SliceHarness):
    def test_the_runners_first_line_says_it_is_an_observation(self):
        self.write("test_a", _CASE % "a")
        parsed, text, rc, _a = self.sliced(1)
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertEqual(self.first_line, gateslice.DIAGNOSTIC_MARKER)


class DeadlineTest(_SliceHarness):
    """gateshard's deadline is per MODULE; a slice worker runs many modules,
    so the run's wall must scale with the modules each worker owes. A 1-worker
    run of units that together outlast one module's deadline must finish."""

    def test_a_one_worker_run_outlasting_one_module_deadline_still_finishes(self):
        for name in ("test_slow_a", "test_slow_b"):
            self.write(name, """
                import time, unittest
                class Own(unittest.TestCase):
                    def test_sleeps(self): time.sleep(1.8)
            """)
        parsed, text = self.serial()
        self.assertEqual(parsed["status"], "OK", text)
        with mock.patch.dict(os.environ, {"HELM_GATE_WORKER_DEADLINE": "3"}):
            parsed, text, rc, _a = self.sliced(1)
        self.assertEqual(parsed["status"], "OK", text)
        self.assertEqual(parsed.get("ran"), 2, text)

    def test_modules_slow_to_import_but_within_the_deadline_finish(self):
        """Discovery imports every module before the first test, so a
        deadline that only started counting once discovery ended read two
        1.8 s imports under a 3 s deadline as a stall."""
        for name in ("test_slow_a", "test_slow_b"):
            self.write(name, """
                import time, unittest
                time.sleep(1.8)
                class Own(unittest.TestCase):
                    def test_fast(self): pass
            """)
        parsed, text = self.serial()
        self.assertEqual(parsed["status"], "OK", text)
        with mock.patch.dict(os.environ, {"HELM_GATE_WORKER_DEADLINE": "3"}):
            parsed, text, rc, _a = self.sliced(1)
        self.assertEqual((rc, parsed["status"], parsed.get("ran")),
                         (0, "OK", 2), text)

    def _hung(self, where):
        self.write("test_fast", _CASE % "fast")
        self.write("test_hang", """
            import time, unittest
            if %r == "import":
                time.sleep(120)
            class Own(unittest.TestCase):
                def test_hangs(self):
                    time.sleep(120)
        """ % where)
        with mock.patch.dict(os.environ, {"HELM_GATE_WORKER_DEADLINE": "2"}):
            parsed, text, rc, _a = self.sliced(1)
        self.assertEqual(parsed["status"], "UNKNOWN", text)
        self.assertIn("made no progress for 2s at %s tests.test_hang"
                      % where, text)

    def test_a_module_that_hangs_at_import_is_killed_and_named(self):
        self._hung("import")

    def test_a_module_that_hangs_while_running_is_killed_and_named(self):
        self._hung("run")


class DeclarationRefusesDiagnosticRunnersTest(unittest.TestCase):
    """A declared command is spawned AND bound as the project's suite, so a
    declaration naming a diagnostic runner would let an observation mint a
    landing receipt. It is refused at the one site spawn and bind share."""

    def test_every_spelling_of_a_diagnostic_runner_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop is a fixed non-empty tuple and each pass asserts the refusal text positively; test_a_real_suite_declaration_is_still_accepted is the control
        for argv in (["python3", "helm/gateslice.py"],
                     ["python3", "/srv/x/helm/gateslice.py"],
                     ["python3", "./helm/../helm/gateslice.py"],
                     ["python3", "-m", "helm.gateslice"],
                     ["python3", "helm/gateshard.py"],
                     ["python3", "-m", "helm.gateshard"],
                     # the joined module flag and the wrappers that carry
                     # the runner in a string.
                     ["python3", "-mhelm.gateslice"],
                     ["sh", "-c", "exec python3 helm/gateslice.py"],
                     ["python3", "-c", "import runpy; runpy.run_path("
                      "'helm/gateslice.py', run_name='__main__')"]):
            plan, err = gate._declared_command(
                "p", {"command": argv, "protocol": "unittest"})
            self.assertIsNone(plan, argv)
            self.assertIn("diagnostic runner", err, argv)

    def test_a_real_suite_declaration_is_still_accepted(self):
        for argv, protocol in ((["python3", "-m", "unittest", "discover", "-s",
                                 "tests", "-t", "."], "unittest"),
                               (["pnpm", "-r", "test"], "exit")):
            plan, err = gate._declared_command(
                "p", {"command": argv, "protocol": protocol})
            self.assertIsNone(err, argv)
            self.assertEqual(plan["argv"], argv)


class RunnerImportCostTest(unittest.TestCase):
    def test_importing_gate_does_not_load_unittest_mock(self):
        """gate imports gateslice for its script path; the runner's leak audit
        is the only user of unittest.mock, so importing gate must not load it."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); "
             "import helm.gate; print('unittest.mock' in sys.modules)" % repo],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "False")
        self.assertIn(gateslice.SCRIPT, gate._DIAGNOSTIC_RUNNERS)


class LeakAuditTest(_SliceHarness):
    """Run-time state is the channel slices do NOT reproduce, so it is the
    one the audit watches. A planted leak must be named, and must FAIL the
    run in fail mode; a clean tree must stay silent in both modes."""

    def _plant(self, leak):
        self.write("_helper", """
            def real():
                return "real"
        """)
        self.write("test_a", """
            import builtins, unittest
            from unittest import mock
            from tests import _helper
            class Leaky(unittest.TestCase):
                def test_leaves_state(self):
                    if %r == "builtins":
                        builtins._planted_leak = True
                    if %r == "mock":
                        mock.patch.object(_helper, "real",
                                          return_value="fake").start()
                    if %r == "patcher":
                        global PATCHER
                        PATCHER = mock.patch.object(_helper, "real")
                        PATCHER.start()
                        PATCHER.stop()
                    if %r == "out-of-order":
                        # Two patches of one attribute that exit out of
                        # order: the second restores the FIRST one's double.
                        first = mock.patch.object(_helper, "real",
                                                  return_value="first")
                        second = mock.patch.object(_helper, "real",
                                                   return_value="second")
                        first.start()
                        second.start()
                        first.stop()
                        second.stop()
                    if %r == "thread":
                        import threading, time
                        threading.Thread(target=time.sleep, args=(30,),
                                         name="planted-sleeper",
                                         daemon=True).start()
                    if %r == "reload":
                        import importlib
                        importlib.reload(_helper)
            PATCHER = None
        """ % ((leak,) * 6))
        self.write("test_b", """
            import builtins, unittest
            from tests import _helper
            class Victim(unittest.TestCase):
                def test_sees_no_leak(self):
                    self.assertFalse(getattr(builtins, "_planted_leak", False))
                    self.assertEqual(_helper.real(), "real")
        """)

    def test_a_planted_builtins_leak_is_named_and_fails_in_fail_mode(self):
        self._plant("builtins")
        serial, _text = self.serial()
        self.assertEqual(serial["status"], "FAILED")
        parsed, text, _rc, _a = self.sliced(2, "report")
        self.assertIn("gateslice leak: tests.test_a: builtins: _planted_leak",
                      text)
        parsed, text, rc, _a = self.sliced(2, "fail")
        self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
        # The victim also fails whenever claiming put it after the leaker in
        # the same worker, as it does serially; the leak itself fails always.
        failed = [row["test"] for row in parsed["failures"]]
        self.assertIn("tests.test_a." + gateslice.LEAK_TEST, failed)
        self.assertLessEqual(set(failed), {
            "tests.test_a." + gateslice.LEAK_TEST,
            "tests.test_b.Victim.test_sees_no_leak"})
        self.assertFalse(parsed["failures_unreadable"], text)

    def test_an_unstopped_mock_is_named_as_a_rebound_callable(self):
        self._plant("mock")
        serial, _text = self.serial()
        self.assertEqual(serial["status"], "FAILED")
        parsed, text, rc, _a = self.sliced(2, "fail")
        self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
        self.assertIn("tests._helper rebound: real", text)

    def _caught_in_fail_mode(self, kind, finding, serial_status):
        self._plant(kind)
        serial, text = self.serial()
        self.assertEqual(serial["status"], serial_status, text)
        parsed, text, rc, _a = self.sliced(2, "fail")
        self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
        self.assertIn("tests.test_a." + gateslice.LEAK_TEST,
                      [row["test"] for row in parsed["failures"]], text)
        self.assertIn(finding, text)
        self.assertIsNotNone(self.evidence)
        self.assertEqual((self.evidence["leak_mode"], self.evidence["leaks"]),
                         ("fail", 1))

    def test_a_double_restored_by_out_of_order_patch_exits_is_caught(self):
        """The shape test_beacons had: two patches of one attribute, exited
        out of order, leave the first one's double bound for good."""
        self._caught_in_fail_mode("out-of-order", "tests._helper rebound: real",
                                  "FAILED")

    def test_a_thread_alive_past_its_module_is_caught(self):
        """The shape test_resumeturn had: a worker thread that outlives the
        module that started it, into every module after it."""
        self._caught_in_fail_mode("thread", "threads alive: planted-sleeper",
                                  "OK")

    def test_a_module_reload_is_caught_as_rebound_callables(self):
        """The shape test_web_split_contract had: a reload rebinds every
        class and function the module defines, for the rest of the process."""
        self._caught_in_fail_mode("reload", "tests._helper rebound: real",
                                  "OK")

    def test_a_stopped_patcher_kept_in_a_global_is_not_a_leak(self):
        """setUpModule/tearDownModule keep their patcher in a module global;
        once stopped it touches nothing, so it must not read as a leak."""
        self._plant("patcher")
        serial, _text = self.serial()
        self.assertEqual(serial["status"], "OK")
        parsed, text, rc, _a = self.sliced(2, "fail")
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertIn("gateslice: 2 workers, 3 units, 2 planned tests", text)
        self.assertNotIn("gateslice leak:", text)

    def test_a_clean_tree_is_silent_and_green_in_both_modes(self):  # noqa: VACUOUS_ASSERTION — the leak line is asserted PRESENT on the same observable by the planted-builtins arm above; this arm also asserts the report block it would sit in
        self._plant("none")
        serial, _text = self.serial()
        self.assertEqual(serial["status"], "OK")
        for mode in ("report", "fail"):
            parsed, text, rc, _a = self.sliced(2, mode)
            self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
            self.assertNotIn("gateslice leak:", text)
        # The report block is where a leak line would sit; it printed.
        self.assertIn("gateslice: 2 workers, 3 units, 2 planned tests", text)

    def test_module_fixtures_run_exactly_as_serially_under_the_audit(self):
        log = os.path.join(self.repo, "fixtures.log")
        for name in ("test_a", "test_b"):
            self.write(name, """
                import unittest
                def _log(line):
                    with open(%r, "a") as fh:
                        fh.write(line + "\\n")
                def setUpModule(): _log("%s up")
                def tearDownModule(): _log("%s down")
                class One(unittest.TestCase):
                    @classmethod
                    def tearDownClass(cls): _log("%s class down")
                    def test_one(self): pass
            """ % (log, name, name, name))
        serial, text = self.serial()
        self.assertEqual(serial["status"], "OK", text)
        with open(log, encoding="utf-8") as fh:
            expected = fh.read().splitlines()
        self.assertEqual(expected, [
            "test_a up", "test_a class down", "test_a down",
            "test_b up", "test_b class down", "test_b down"])
        os.unlink(log)
        parsed, text, rc, _a = self.sliced(1, "fail")
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(), expected)


class DataAuditTest(_SliceHarness):
    """Module DATA one unit leaves for another is the channel slices split:
    two modules on two workers never see each other's writes. The review's
    FIX on the v10 lane (row a07a76d46427) measured it on a clean
    two-module tree, rebuilt here from that verdict's own words:
    test_a sets tests._shared.VALUE = 1, test_b expects 0, and a weighted
    plan puts the two on different workers."""

    # Equal weights: longest-first gives each its own worker of the two.
    SPLIT = {"tests": 0.1, "tests.test_a": 5.0, "tests.test_b": 5.0}

    def _codex_tree(self, restore=False):
        self.write("_shared", "VALUE = 0\n")
        self.write("test_a", """
            import unittest
            from tests import _shared
            class A(unittest.TestCase):
                def test_sets_the_shared_value(self):
                    if %r:
                        self.addCleanup(setattr, _shared, "VALUE",
                                        _shared.VALUE)
                    _shared.VALUE = 1
        """ % restore)
        self.write("test_b", """
            import unittest
            from tests import _shared
            class B(unittest.TestCase):
                def test_expects_zero(self):
                    self.assertEqual(_shared.VALUE, 0)
        """)

    def test_the_codex_tree_is_not_ok_in_fail_mode_and_names_the_mutation(self):
        self._codex_tree()
        serial, text = self.serial()
        self.assertEqual(serial["status"], "FAILED", text)
        parsed, text, rc, assignment = self.sliced(2, "fail",
                                                   seconds=self.SPLIT)
        owners = {label: w for w, labels in assignment.items()
                  for label in labels}
        self.assertNotEqual(owners["tests.test_a"], owners["tests.test_b"],
                            "fixture broken: the plan did not split a and b")
        self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
        self.assertEqual([row["test"] for row in parsed["failures"]],
                         ["tests.test_a." + gateslice.LEAK_TEST], text)
        self.assertIn("module data mutated: tests._shared.VALUE (rebound)",
                      text)
        self.assertEqual((self.evidence["leak_mode"], self.evidence["leaks"],
                          self.evidence["outcome"]["ok"]), ("fail", 1, False))

    def test_without_the_audit_the_codex_tree_reads_ok_where_serial_is_red(self):  # noqa: VACUOUS_ASSERTION — the absent line under `off` is asserted PRESENT under `report` on the same tree two lines later
        """The plant, kept in the suite: with the audit off the split run is
        the divergence the review measured, so the arm above fails for the
        audit's reason and no other."""
        self._codex_tree()
        parsed, text, rc, _a = self.sliced(2, "off", seconds=self.SPLIT)
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertNotIn("module data mutated", text)
        parsed, text, rc, _a = self.sliced(2, "report", seconds=self.SPLIT)
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertIn("gateslice leak: tests.test_a: module data mutated: "
                      "tests._shared.VALUE (rebound)", text)

    def test_a_clean_two_module_tree_reads_ok(self):  # noqa: VACUOUS_ASSERTION — the same line is asserted PRESENT by the codex arm above; this arm also asserts the report block it would sit in
        self.write("_shared", "VALUE = 0\nCALLS = []\n")
        self.write("_lazy", "LOADED = True\n")
        for name in ("test_a", "test_b"):
            self.write(name, """
                import unittest
                from unittest import mock
                from tests import _shared
                class Reads(unittest.TestCase):
                    def test_reads_and_restores(self):
                        import tests._lazy
                        self.assertTrue(tests._lazy.LOADED)
                        self.assertEqual(_shared.VALUE, 0)
                        with mock.patch.object(_shared, "VALUE", 7):
                            self.assertEqual(_shared.VALUE, 7)
                        with mock.patch.object(_shared, "CALLS", ["x"]):
                            _shared.CALLS.append("y")
            """)
        serial, text = self.serial()
        self.assertEqual((serial["status"], serial["ran"]), ("OK", 2), text)
        parsed, text, rc, _a = self.sliced(2, "fail", seconds=self.SPLIT)
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertNotIn("module data mutated", text)
        self.assertEqual(self.evidence["leaks"], 0)
        self.assertIn("gateslice: 2 workers, 3 units, 2 planned tests", text)

    def _content_tree(self, declaration):
        self.write("_shared", "CACHE = {}\nclass Config:\n    LIMIT = 3\n"
                   + declaration)
        self.write("test_a", """
            import unittest
            from tests import _shared
            class A(unittest.TestCase):
                def test_fills(self):
                    _shared.CACHE["k"] = 1
                    _shared.Config.LIMIT = 5
        """)
        self.write("test_b", _CASE % "b")

    def test_a_filled_container_and_a_class_attribute_are_named(self):
        self._content_tree("")
        parsed, text, rc, _a = self.sliced(1, "fail")
        self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
        self.assertIn("tests._shared.CACHE (content)", text)
        self.assertIn("tests._shared.Config.LIMIT (rebound)", text)

    def test_only_a_declared_name_with_a_reason_is_exempt(self):  # noqa: VACUOUS_ASSERTION — test_a_filled_container_and_a_class_attribute_are_named plants the same tree without the declaration and asserts both names PRESENT
        self._content_tree(
            '_GATESLICE_MUTABLE = {"CACHE": "a memo; every fill equals a '
            'recompute", "Config.LIMIT": "tuned per process by design"}\n')
        parsed, text, rc, _a = self.sliced(1, "fail")
        self.assertEqual((rc, parsed["status"]), (0, "OK"), text)
        self.assertNotIn("module data mutated", text)

    def test_a_declaration_without_reasons_exempts_nothing(self):
        self._content_tree('_GATESLICE_MUTABLE = ("CACHE", "Config.LIMIT")\n')
        parsed, text, rc, _a = self.sliced(1, "fail")
        self.assertEqual((rc, parsed["status"]), (1, "FAILED"), text)
        self.assertIn("tests._shared.CACHE (content)", text)

    def test_the_serial_diagnostic_names_the_same_mutation(self):
        self._codex_tree()
        env = dict(os.environ, HELM_GATESLICE_LEAKS="fail")
        proc = subprocess.run(
            [sys.executable, os.path.abspath(gateslice.__file__), "--serial",
             "tests.test_a", "tests.test_b"],
            cwd=self.repo, env=env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stderr.split("\n", 1)[0],
                         gateslice.DIAGNOSTIC_MARKER)
        self.assertIn("gateslice leak: tests.test_a: module data mutated: "
                      "tests._shared.VALUE (rebound)", proc.stderr)
        self._codex_tree(restore=True)
        proc = subprocess.run(
            [sys.executable, os.path.abspath(gateslice.__file__), "--serial",
             "tests.test_a", "tests.test_b"],
            cwd=self.repo, env=env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("module data mutated", proc.stderr)


class DataMutationRulesTest(unittest.TestCase):
    """The diff itself, over planted module objects: no runner, no import."""

    def _module(self, name, **values):
        module = type(sys)(name)
        vars(module).update(values)
        return module

    def _diff(self, module, change):
        modules = {module.__name__: module}
        before = gateslice.data_snapshot(modules)
        change(module)
        return gateslice.data_mutations(before, gateslice.data_snapshot(
            modules), "tests.test_x")

    def test_every_change_kind_is_named_with_owner_and_attribute(self):
        import collections
        import types
        mod = self._module("tests._m", D={}, L=[], S=set(), B=bytearray(b"a"),
                           Q=collections.deque(), N=types.SimpleNamespace(x=1),
                           GONE=1, V=0)

        def change(m):
            m.D["k"] = 1
            m.L.append(1)
            m.S.add(1)
            m.B.extend(b"b")
            m.Q.append(1)
            m.N.x = 2
            del m.GONE
            m.V = 1
            m.NEW = []
        records = self._diff(mod, change)
        self.assertEqual(sorted((r["attr"], r["change"]) for r in records), [
            ("B", "content"), ("D", "content"), ("GONE", "removed"),
            ("L", "content"), ("N", "content"), ("NEW", "added"),
            ("Q", "content"), ("S", "content"), ("V", "rebound")])
        self.assertTrue(all(r["owner"] == "tests._m" and r["excluded"] is None
                            and r["unit"] == "tests.test_x" for r in records))

    def test_an_equal_immutable_and_a_restored_object_are_the_same_state(self):
        mod = self._module("tests._m", S="ab", T=(1, 2), D={"k": 1})

        def change(m):
            m.S = "".join(["a", "b"])       # equal, not identical
            m.T = tuple([1, 2])
            saved = m.D
            m.D = {"k": 1}                   # an equal dict is not the dict
            m.D = saved
        self.assertEqual(self._diff(mod, change), [])
        # Positive control on the same observable: the equal dict, left
        # bound, is a different object and is named.
        records = self._diff(mod, lambda m: setattr(m, "D", {"k": 1}))
        self.assertEqual([(r["attr"], r["change"]) for r in records],
                         [("D", "rebound")])

    def test_lazy_submodules_and_code_rebinds_are_recorded_not_reported(self):
        mod = self._module("tests._m", f=len)

        def change(m):
            m.sub = type(sys)("tests._m.sub")
            m.f = abs
        records = self._diff(mod, change)
        self.assertEqual(sorted((r["attr"], bool(r["excluded"]))
                                for r in records),
                         [("f", True), ("sub", True)])
        self.assertIsNone(gateslice.data_findings(records))

    def test_a_declaration_exempts_only_its_names_and_needs_reasons(self):
        mod = self._module("tests._m", A={}, B={},
                           _GATESLICE_MUTABLE={"A": "memo"})

        def fill(m):
            m.A["k"] = 1
            m.B["k"] = 1
        records = self._diff(mod, fill)
        self.assertEqual(gateslice.data_findings(records),
                         "module data mutated: tests._m.B (content)")
        for bad in (("A",), {"A": ""}, {"A": "two\nlines"}, {"A": 1}):
            mod = self._module("tests._m", A={}, B={}, _GATESLICE_MUTABLE=bad)
            records = self._diff(mod, lambda m: m.A.__setitem__("k", 1))
            self.assertEqual(gateslice.data_findings(records),
                             "module data mutated: tests._m.A (content)", bad)

    def test_class_attributes_of_the_modules_own_classes_are_watched(self):
        mod = self._module("tests._m")
        exec("class Own:\n    LIMIT = 3\n    SEEN = []\n", vars(mod))
        mod.Own.__module__ = "tests._m"
        mod.Borrowed = dict                # not defined here: not watched

        def change(m):
            m.Own.LIMIT = 4
            m.Own.SEEN.append(1)
            m.Own.added = True
        records = self._diff(mod, change)
        self.assertEqual(sorted((r["attr"], r["change"], r["scope"])
                                for r in records),
                         [("Own.LIMIT", "rebound", "class"),
                          ("Own.SEEN", "content", "class"),
                          ("Own.added", "added", "class")])

    def _unit_diff(self, modules, change):
        before = gateslice.data_snapshot(modules)
        change()
        return gateslice.data_mutations(before, gateslice.data_snapshot(
            modules), "tests.test_x")

    def test_the_units_own_module_is_private_until_another_module_reaches_it(self):
        own = self._module("tests.test_x", PATCHER=None, CALLS=[])
        exec("import unittest\nclass Case(unittest.TestCase):\n"
             "    def test_x(self): pass\n", vars(own))
        own.Case.__module__ = "tests.test_x"
        other = self._module("tests.test_y")

        def change():
            own.PATCHER = object()
            own.CALLS.append(1)
            own.Case.home = "/tmp/x"
            own.Case.tearDown_exceptions = []
        modules = {"tests.test_x": own, "tests.test_y": other}
        records = self._unit_diff(modules, change)
        self.assertEqual(len(records), 4)
        self.assertIsNone(gateslice.data_findings(records))
        self.assertTrue(all(r["own"] for r in records))
        # Another module holding the very list: its content is shared, and
        # both holders are named.
        own.PATCHER, own.CALLS = None, []
        other.CALLS = own.CALLS
        del own.Case.home, own.Case.tearDown_exceptions
        records = self._unit_diff(modules, change)
        self.assertEqual(gateslice.data_findings(records),
                         "module data mutated: tests.test_x.CALLS (content)")
        # Both holders are recorded; the second as the first one's alias.
        self.assertEqual([r["excluded"] for r in records
                          if r["attr"] == "CALLS"],
                         [None, "the same object as tests.test_x.CALLS, "
                                "named there"])
        # Another module holding a class the unit defines reaches all of it,
        # except what unittest writes on the class and what the class's own
        # fixtures set on it, which its setUpClass makes again on every run.
        own.PATCHER, own.CALLS = None, []
        del other.CALLS, own.Case.home, own.Case.tearDown_exceptions
        other.Case = own.Case
        records = self._unit_diff(modules, change)
        self.assertEqual(gateslice.data_findings(records),
                         "module data mutated: tests.test_x.PATCHER (rebound), "
                         "tests.test_x.CALLS (content)")
        self.assertIn("class-fixture state", next(
            r["excluded"] for r in records if r["attr"] == "Case.home"))

    def test_a_shared_object_is_one_finding_and_its_declaration_travels(self):
        cache, seen = {}, []
        owner = self._module("helm.x._common", _CACHE=cache,
                             _GATESLICE_MUTABLE={"_CACHE": "a memo"})
        facade = self._module("helm.x", _CACHE=cache)
        left = self._module("helm.y", SEEN=seen)
        right = self._module("helm.z", SEEN=seen)
        modules = {m.__name__: m for m in (owner, facade, left, right)}

        def change():
            cache["k"] = 1
            seen.append(1)
        records = self._unit_diff(modules, change)
        self.assertEqual([(r["owner"], r["excluded"]) for r in records], [
            ("helm.x._common", "declared: a memo"),
            ("helm.x", "declared as helm.x._common._CACHE: a memo"),
            ("helm.y", None),
            ("helm.z", "the same object as helm.y.SEEN, named there")])
        self.assertEqual(gateslice.data_findings(records),
                         "module data mutated: helm.y.SEEN (content)")

    def test_equal_scalars_and_a_restored_inherited_attribute_change_nothing(self):  # noqa: VACUOUS_ASSERTION — the same diff is asserted to NAME the one real change planted beside them
        roots = ["/a"]

        class Base:
            proto = "HTTP/1.0"

        class Handler(Base):
            pass
        Handler.__module__, Handler.__qualname__ = "helm.w", "Handler"
        mod = self._module("helm.w", ROOTS=roots, Handler=Handler, N=[])

        def change():
            roots[:] = ["".join(["/", "a"])]   # equal, not identical
            real = Handler.proto               # the inherited value...
            Handler.proto = "HTTP/1.1"
            Handler.proto = real               # ...now the class's own
            mod.N.append(1)                    # the one real change
        records = self._unit_diff({"helm.w": mod}, change)
        self.assertEqual([(r["attr"], r["change"]) for r in records],
                         [("N", "content")])

    def test_a_module_held_as_a_global_is_audited_as_itself(self):
        class Facade(type(sys)):
            pass
        facade = Facade("helm.f")
        facade.STATE = 1
        holder = self._module("helm.h", facade=facade)
        records = self._unit_diff({"helm.h": holder, "helm.f": facade},
                                  lambda: setattr(facade, "STATE", 2))
        self.assertEqual([(r["owner"], r["attr"]) for r in records],
                         [("helm.f", "STATE")])

    def test_a_spoofed_class_or_hostile_container_runs_no_code(self):  # noqa: VACUOUS_ASSERTION — the same diff is asserted to NAME the content change, so the audit read the container it ran no code on
        calls = []

        class Hostile(dict):
            def items(self):
                calls.append("items")
                return super().items()

            def __eq__(self, other):
                calls.append("eq")
                return True
            __hash__ = dict.__hash__
        mod = self._module("tests._m", H=Hostile(a=1))

        def change(m):
            m.H["b"] = 2
        records = self._diff(mod, change)
        self.assertEqual([(r["attr"], r["change"]) for r in records],
                         [("H", "content")])
        self.assertEqual(calls, [])

    def test_the_audit_is_off_only_when_the_leak_audit_is(self):
        """The data audit rides the leak audit's switch: `off` is the one
        mode in which a worker does not open a window at all."""
        with mock.patch.dict(os.environ, {"HELM_GATESLICE_LEAKS": "off"}):
            self.assertEqual(gateslice.leak_mode(), "off")
        with mock.patch.dict(os.environ, {"HELM_GATESLICE_LEAKS": "bogus"}):
            self.assertEqual(gateslice.leak_mode(), "report")


class MutableDeclarationTest(unittest.TestCase):
    """Every `_GATESLICE_MUTABLE` in the tree is a literal dict of names this
    module binds to a one-line reason. Read from source, so a declaration
    that names a removed global or drops its reason fails here, before any
    run trusts it."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _declarations(self):
        import ast
        for top in ("helm", "tests"):
            for here, dirs, names in os.walk(os.path.join(self.ROOT, top)):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(names):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(here, name)
                    with open(path, encoding="utf-8") as fh:
                        source = fh.read()
                    if gateslice.MUTABLE_DECLARATION not in source:
                        continue
                    tree = ast.parse(source)
                    if any(isinstance(node, ast.Assign) and any(
                            isinstance(t, ast.Name)
                            and t.id == gateslice.MUTABLE_DECLARATION
                            for t in node.targets) for node in tree.body):
                        yield os.path.relpath(path, self.ROOT), tree

    def test_every_declaration_names_what_its_module_binds_and_why(self):  # noqa: VACUOUS_ASSERTION — assertGreater(seen, 0) after the walk is unconditional
        import ast
        seen = 0
        for rel, tree in self._declarations():
            bound, classes = set(), set()
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) \
                        else [node.target]
                    bound |= {t.id for t in targets if isinstance(t, ast.Name)}
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    # A facade may declare what it re-exports: the audit
                    # carries a declaration to every holder of the object.
                    bound |= {(a.asname or a.name).split(".")[0]
                              for a in node.names}
                elif isinstance(node, ast.ClassDef):
                    classes.add(node.name)
            decls = [node for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and
                             t.id == gateslice.MUTABLE_DECLARATION
                             for t in node.targets)]
            self.assertEqual(len(decls), 1, rel)
            value = decls[0].value
            self.assertIsInstance(value, ast.Dict, rel)
            for key, reason in zip(value.keys, value.values):
                self.assertTrue(isinstance(key, ast.Constant)
                                and isinstance(key.value, str), rel)
                self.assertTrue(isinstance(reason, ast.Constant)
                                and isinstance(reason.value, str)
                                and reason.value.strip()
                                and "\n" not in reason.value,
                                "%s: %s has no one-line reason"
                                % (rel, key.value))
                head, _dot, attr = key.value.partition(".")
                self.assertTrue(head in classes if attr else head in bound,
                                "%s declares %s, which it does not bind"
                                % (rel, key.value))
                seen += 1
        # Positive control: the walk found declarations to judge.
        self.assertGreater(seen, 0)


if __name__ == "__main__":
    unittest.main()
