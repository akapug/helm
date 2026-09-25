import collections
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from unittest import mock

from helm import gate, gateequiv, gateshard, gatetestrecord


SHARD = [sys.executable, os.path.abspath(gateshard.__file__)]


def probe(module):
    cls = type("Probe", (unittest.TestCase,), {
        "__module__": module,
        "test_probe": lambda case: case.assertTrue(True),
    })
    return cls("test_probe")


class WorkerCountTest(unittest.TestCase):
    def test_gateshard_is_suite_scale_but_never_landing_authority(self):
        serial = [sys.executable] + list(gate.SUITE)
        self.assertTrue(gate._suite_shaped(serial))
        self.assertEqual(gate._suite_root_kind(serial), "suite")
        self.assertFalse(gate._suite_shaped(SHARD))
        self.assertEqual(gate._suite_root_kind(SHARD), "suite")
        worker = SHARD + ["--worker", "1", "manifest", "protocol", "meta"]
        self.assertFalse(gate._suite_shaped(worker))
        self.assertIsNone(gate._suite_root_kind(worker))
        self.assertFalse(gate._suite_shaped(
            [sys.executable, "-m", "helm.gateshard"]))
        self.assertFalse(gate._suite_shaped(
            [sys.executable, "/other-worktree/helm/gateshard.py"]))
        self.assertEqual(gate._suite_root_kind(
            [sys.executable, "/other-worktree/helm/gateshard.py"]), "suite")
        self.assertFalse(gate._suite_shaped(
            [sys.executable, "helm/gateshard.py"]))

    def test_serial_and_historical_stored_runner_sets_compose(self):
        serial = [sys.executable] + list(gate.SUITE)
        gaterunner = [sys.executable, "-m", "helm.gaterunner", "discover"]
        gaterunner_full = [sys.executable, "-m", "helm.gaterunner",
                           "discover", "-s", "tests", "-t", "."]
        arbitrary = [sys.executable, "-m", "not.a.test.runner", "discover"]
        self.assertTrue(gate._suite_shaped(serial))
        self.assertTrue(gate._suite_shaped(serial, runner=None))
        self.assertFalse(gate._suite_shaped(gaterunner))
        self.assertTrue(gate._suite_shaped(gaterunner, runner=None))
        self.assertTrue(gate._suite_shaped(gaterunner_full, runner=None))
        self.assertFalse(gate._suite_shaped(arbitrary))
        self.assertFalse(gate._suite_shaped(arbitrary, runner=None))

    def test_the_legacy_gaterunner_is_accepted_and_never_produced(self):
        """`helm.gaterunner` stays in the stored runner set so the receipts it
        minted on a lane that never merged keep reading as they did; this tree
        must never mint another. Both halves, each against a control:

          ACCEPTED   the set still names it, beside unittest.
          NEVER      no module of that name is importable (the same probe
          PRODUCED   finds a real helm module), the writer spawns `-m
                     unittest`, and no helm source outside the three readers
                     that accept or guard the name spells it."""
        self.assertEqual(gate._STORED_SUITE_RUNNERS,
                         frozenset(("unittest", "helm.gaterunner")))
        self.assertIsNotNone(importlib.util.find_spec("helm.gateshard"))
        self.assertIsNone(importlib.util.find_spec("helm.gaterunner"))
        self.assertEqual(gate.SUITE[:2], ("-m", "unittest"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spelled = set()
        for dp, _dn, fn in os.walk(os.path.join(root, "helm")):
            for n in fn:
                if n.endswith(".py"):
                    p = os.path.join(dp, n)
                    with open(p, encoding="utf-8") as f:
                        if "gaterunner" in f.read():
                            spelled.add(os.path.relpath(p, root))
        self.assertEqual(spelled, {"helm/gate.py", "helm/gateroute.py",
                                   os.path.join("helm", "work", "_gc.py")})

    def test_worker_budget_comes_from_affinity_and_gate_capacity(self):
        with mock.patch.object(gateshard, "_available_cpus", return_value=40):
            self.assertEqual(gateshard.worker_count(258, suite_capacity=8), 5)
        cases = ((16, 4, 4), (2, 2, 1))
        for cores, capacity, expected in cases:
            with self.subTest(cores=cores, capacity=capacity), \
                    mock.patch.object(gateshard, "_available_cpus",
                                      return_value=cores):
                self.assertEqual(
                    gateshard.worker_count(258, suite_capacity=capacity),
                    expected)

    def test_gate_exports_its_admitted_capacity_to_the_runner(self):  # noqa: VACUOUS_ASSERTION — the injected grant's exact call and rendered cap are positive controls before worker_count consumes the exported value
        capacity = {
            "cap": 8,
            "reason": "paneless build host, 16 host-online CPUs",
        }
        grant = mock.Mock(return_value=capacity)
        with mock.patch.object(gate, "suite_capacity") as live_capacity:
            env = gate._suite_env(grant())
        self.assertEqual(grant.call_args, mock.call())
        live_capacity.assert_not_called()
        self.assertEqual(env["HELM_GATE_SUITE_CAP"], "8")
        with mock.patch.dict(os.environ, {"HELM_GATE_SUITE_CAP": "4"}), \
                mock.patch.object(gateshard, "_available_cpus", return_value=16):
            self.assertEqual(gateshard.worker_count(258), 4)

    def test_planner_and_worker_markers_are_exclusive_and_roots_are_fresh(self):
        planted = {
            "HELM_GATESHARD_PLANNER": "1",
            "HELM_GATESHARD_WORKER": "1",
            "HELM_GATE_RECORD_TOKEN": "foreign",
            "HELM_PROXYJOURNAL_LOG": "/parent/proxywatch.log",
            "HELM_SEAT_NAMES": "/parent/seat-names.txt",
            "MELD_SEAT_NAMES": "/parent/legacy-seat-names.txt",
            "HELM_TURNSTAMP_DIR": "/parent/turnstamps",
            "MELD_TURNSTAMP_DIR": "/parent/legacy-turnstamps",
            "HELM_MULTIPLAYER_DIR": "/parent/multiplayer",
            "MELD_MULTIPLAYER_DIR": "/parent/legacy-multiplayer",
        }
        planted.update({key: "/shared/" + key.lower()
                        for key in gateshard._identity_path_env_keys()})
        with mock.patch.dict(os.environ, planted, clear=False):
            env = gateshard._fresh_env("HELM_GATESHARD_WORKER")
        self.assertEqual(env["HELM_GATESHARD_WORKER"], "1")
        # KEY VIEWS, NEVER THE MAPPING: _fresh_env() opens with
        # dict(os.environ), so a failure against the mapping renders every
        # ambient value into the report and the fab artifact (task/2370).
        self.assertNotIn("HELM_GATESHARD_PLANNER", tuple(env))
        self.assertFalse(set(gateshard._identity_path_env_keys()) & set(env),
                         "fresh workers inherited an identity-estate path")
        self.assertNotIn("HELM_GATE_RECORD_TOKEN", tuple(env))
        self.assertNotIn("HELM_PROXYJOURNAL_LOG", tuple(env))
        self.assertFalse({"HELM_SEAT_NAMES", "MELD_SEAT_NAMES"} & set(env),
                         "fresh workers inherited a seat-name authority")
        stores = {"HELM_TURNSTAMP_DIR", "MELD_TURNSTAMP_DIR",
                  "HELM_MULTIPLAYER_DIR", "MELD_MULTIPLAYER_DIR"}
        self.assertFalse(stores & set(env),
                         "fresh workers inherited a writable parent store")

    def test_worker_count_never_exceeds_the_module_count(self):
        with mock.patch.object(gateshard, "_available_cpus", return_value=128):
            self.assertEqual(gateshard.worker_count(3, suite_capacity=2), 3)
            self.assertEqual(gateshard.worker_count(0, suite_capacity=2), 1)

    def test_discovery_contract_keeps_tests_package_as_the_import_root(self):
        sentinel = unittest.TestSuite()
        with mock.patch.object(
                unittest.defaultTestLoader, "discover",
                return_value=sentinel) as discover:
            self.assertIs(gateshard.discover_suite(), sentinel)
        discover.assert_called_once_with(
            "tests", pattern="test*.py", top_level_dir=".")

    def test_import_failure_remains_a_planned_failing_test(self):  # noqa: VACUOUS_ASSERTION — the written count and id artifacts are unconditional positive controls that the planner ran and preserved both modules
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "tests"))
            with open(os.path.join(repo, "tests", "__init__.py"), "w") as fh:
                fh.write("")
            with open(os.path.join(repo, "tests", "test_broken.py"), "w") as fh:
                fh.write("import this_module_does_not_exist_anywhere\n")
            with open(os.path.join(repo, "tests", "test_ok.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    import unittest
                    class OkTest(unittest.TestCase):
                        def test_a(self): pass
                """))
            counts = os.path.join(repo, "counts.json")
            ids = os.path.join(repo, "ids.json")
            env = {**os.environ, "HELM_GATESHARD_PLANNER": "1"}
            run = subprocess.run(
                SHARD + ["--plan", counts, ids], cwd=repo, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            with open(counts, encoding="utf-8") as fh:
                planned_counts = json.load(fh)
            with open(ids, encoding="utf-8") as fh:
                planned_ids = json.load(fh)
        self.assertEqual(planned_counts,
                         [["unittest.loader", 1], ["tests.test_ok", 1]])
        self.assertEqual(len(planned_ids["unittest.loader"]), 1)
        self.assertIn("test_broken", planned_ids["unittest.loader"][0])
        self.assertEqual(planned_ids["tests.test_ok"],
                         ["tests.test_ok.OkTest.test_a"])

    def test_diagnostic_runner_refuses_zero_discovery(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "tests"))
            with open(os.path.join(repo, "tests", "__init__.py"), "w") as fh:
                fh.write("")
            run = subprocess.run(
                SHARD, cwd=repo,
                capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(gate.parse_result(run.stderr)["status"], "UNKNOWN")
        self.assertNotIn("Ran 0 tests", run.stderr)

    def test_current_planner_and_workers_assign_the_same_test_ids(self):  # noqa: VACUOUS_ASSERTION — each module's non-empty exact planned-id list is unconditionally compared with its independently loaded worker list
        suite, groups = gateshard.discover_plan()
        planned = {name: gatetestrecord.planned_ids(unittest.TestSuite(tests))
                   for name, tests in groups}
        self.assertEqual(
            gatetestrecord.planned_ids(suite),
            [test_id for name, _tests in groups for test_id in planned[name]])
        modules = (
            "tests.test_landreq", "tests.test_lr_close",
            "tests.test_lr_project_scope", "tests.test_lr_retire",
            "tests.test_web_lr", "tests.test_web_scheduler",
        )
        for name in modules:
            with self.subTest(module=name):
                assigned = dict(gateshard._assigned_groups([name]))[name]
                worker_ids = gatetestrecord.planned_ids(
                    unittest.TestSuite(assigned))
                self.assertEqual(len(worker_ids), len(planned[name]))
                self.assertEqual(collections.Counter(worker_ids),
                                 collections.Counter(planned[name]))
                self.assertEqual(worker_ids, planned[name])

    def test_planner_preserves_an_imported_test_bearing_base(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "tests"))
            with open(os.path.join(repo, "tests", "__init__.py"), "w") as fh:
                fh.write("")
            with open(os.path.join(repo, "tests", "test_owner.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    import unittest
                    class ImportedBase(unittest.TestCase):
                        def test_owned(self): self.assertTrue(True)
                """))
            with open(os.path.join(repo, "tests", "test_consumer.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    from tests.test_owner import ImportedBase
                """))
            run = subprocess.run(
                SHARD, cwd=repo, capture_output=True, text=True, timeout=30,
                env={**os.environ, "HELM_GATESHARD_VERBOSE": "1"})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(gate.parse_result(run.stderr)["ran"], 2)
        self.assertEqual(run.stderr.count(
            "tests.test_owner.ImportedBase.test_owned"), 2)

    def test_imported_helper_base_with_no_tests_is_not_foreign(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "tests"))
            with open(os.path.join(repo, "tests", "__init__.py"), "w") as fh:
                fh.write("")
            with open(os.path.join(repo, "tests", "test_owner.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    import unittest
                    class ImportedBase(unittest.TestCase):
                        def helper(self): return True
                """))
            with open(os.path.join(repo, "tests", "test_consumer.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    import unittest
                    from tests.test_owner import ImportedBase
                    class Probe(ImportedBase):
                        def test_probe(self): self.assertTrue(self.helper())
                """))
            run = subprocess.run(
                SHARD, cwd=repo, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(gate.parse_result(run.stderr)["ran"], 1)

    def test_diagnostic_path_cannot_be_shadowed_by_an_adopter_helm_package(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "helm"))
            os.makedirs(os.path.join(repo, "tests"))
            with open(os.path.join(repo, "helm", "__init__.py"), "w") as fh:
                fh.write("ORIGIN = 'adopter'\n")
            with open(os.path.join(repo, "tests", "__init__.py"), "w") as fh:
                fh.write("")
            with open(os.path.join(repo, "tests", "test_adopter.py"), "w") as fh:
                fh.write(textwrap.dedent("""
                    import helm
                    import unittest

                    class Adopter(unittest.TestCase):
                        def test_own_package_wins(self):
                            self.assertEqual(helm.ORIGIN, "adopter")
                """))
            env = dict(os.environ, HELM_GATE_SUITE_CAP="1")
            run = subprocess.run(
                SHARD, cwd=repo, env=env,
                capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(gate.parse_result(run.stderr)["status"], "OK")


class _ShardHarness(unittest.TestCase):
    """Fixture + helpers only, NO test methods.

    Split out so a second suite can drive the REAL worker without
    subclassing ShardExecutionTest, which would re-run every one of
    its arms a second time under the subclass name.
    """

    def _alive(self, pid, start):
        """Live means RUNNING, and a ZOMBIE is not running.

        pid+start alone counts Z and X as alive. The canonical worker is a
        SUBREAPER, so a killed adopted descendant is reaped INTO it and
        lingers as a zombie rather than vanishing — which makes the naive
        check report a successful kill as a survivor.

        ONE DEFINITION FOR EVERY ARM THAT ASKS, on the shared harness: a
        liveness predicate duplicated per class is cured per class, so the
        copies drift apart silently.
        """
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            return fields[19] == start and fields[0] not in ("Z", "X")
        except OSError:
            return False


    def setUp(self):
        self.artifact_digests = {}
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        os.makedirs(os.path.join(self.repo, "tests"))
        self.write("__init__", """
            import atexit, os, shutil, tempfile
            _ROOT = tempfile.mkdtemp(prefix="gateshard-worker-root-")
            atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
            for _name, _leaf in (
                    ("HELM_CONFIG_ROOTS", "configs"),
                    ("HELM_HOME", "home"),
                    ("HELM_CHAT_DIR", "chat")):
                _path = os.path.join(_ROOT, _leaf)
                os.makedirs(_path)
                os.environ[_name] = _path
            os.environ["HELM_METAHARNESS"] = "none"
        """)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, source):
        path = os.path.join(self.repo, "tests", name + ".py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(source))
        return "tests." + name

    def run_bins(self, bins, record_context=None):
        expected = {name: 1 for row in bins for name in row}
        ids = {name: [name + ".Probe.test_probe"] for name in expected}
        with tempfile.TemporaryDirectory() as work:
            context = dict(record_context, directory=work) \
                if record_context else None
            result = gateshard.run_bins(
                bins, work, repo=self.repo, expected_counts=expected,
                expected_ids=ids, record_context=context)
            artifacts = {}
            for name in os.listdir(work):
                if context and name.startswith(context["token"]):
                    # RAW BYTES AND THEIR DIGEST, not just the parsed row.
                    # An arm named "...matches the worker bytes" that only
                    # sees parsed rows cannot compute a hash and cannot
                    # compare one -- it can only assert the claim is
                    # 64 characters long, which is a shape check wearing a
                    # verification's name. The directory is destroyed on exit,
                    # so this is the only moment the bytes exist.
                    with open(os.path.join(work, name), "rb") as fh:
                        raw = fh.read()
                    artifacts[name] = json.loads(raw.decode("utf-8"))
                    self.artifact_digests[name] = hashlib.sha256(
                        raw).hexdigest()
            return result if context is None else result + (artifacts,)


class ShardExecutionTest(_ShardHarness):
    def test_empty_shard_and_failure_only_in_shard_three_aggregate(self):
        alpha = self.write("test_alpha", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        beta = self.write("test_beta", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.fail("third shard owns this failure")
        """)
        text, rc = self.run_bins([[alpha], [], [beta]])
        parsed = gate.parse_result(text)
        self.assertEqual((rc, parsed["status"], parsed["ran"]), (1, "FAILED", 2))
        self.assertEqual(parsed["failures"][0]["test"],
                         "tests.test_beta.Probe.test_probe")
        self.assertFalse(parsed["failures_unreadable"])

    def _serial(self):
        return subprocess.run(
            [sys.executable] + list(gate.SUITE), cwd=self.repo,
            capture_output=True, text=True, timeout=30)

    def test_late_import_can_make_serial_fail_while_assigned_shards_pass(self):
        alpha = self.write("test_alpha", """
            import builtins, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    self.assertFalse(getattr(builtins, "_late_tripwire", False))
        """)
        late = self.write("test_z_late", """
            import builtins, unittest
            builtins._late_tripwire = True
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        serial = self._serial()
        sharded, shard_rc = self.run_bins([[alpha], [late]])
        self.assertEqual(gate.parse_result(serial.stderr)["status"], "FAILED")
        self.assertEqual((shard_rc, gate.parse_result(sharded)["status"]),
                         (0, "OK"))

    def test_late_import_can_make_serial_pass_while_assigned_shards_fail(self):
        alpha = self.write("test_alpha", """
            import builtins, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    self.assertTrue(getattr(builtins, "_late_ready", False))
        """)
        late = self.write("test_z_late", """
            import builtins, unittest
            builtins._late_ready = True
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        serial = self._serial()
        sharded, shard_rc = self.run_bins([[alpha], [late]])
        self.assertEqual((serial.returncode,
                          gate.parse_result(serial.stderr)["status"]),
                         (0, "OK"))
        self.assertEqual(gate.parse_result(sharded)["status"], "FAILED")
        self.assertEqual(shard_rc, 1)

    def test_one_worker_still_reimports_after_the_planner(self):
        marker = os.path.join(self.repo, "imports")
        self.write("test_import_count", """
            import unittest
            with open(%r, "a") as _fh: _fh.write("imported\\n")
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """ % marker)
        serial = self._serial()
        self.assertEqual(serial.returncode, 0, serial.stderr)
        with open(marker) as fh:
            self.assertEqual(fh.read().splitlines(), ["imported"])
        os.unlink(marker)
        env = dict(os.environ, HELM_GATE_SUITE_CAP="999999")
        sharded = subprocess.run(
            SHARD, cwd=self.repo, env=env, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(sharded.returncode, 0, sharded.stderr)
        with open(marker) as fh:
            self.assertEqual(fh.read().splitlines(), ["imported", "imported"])

    def test_verbose_measurement_names_every_passing_test(self):
        alpha = self.write("test_alpha", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        verbose = "test_probe (tests.test_alpha.Probe.test_probe) ... ok"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HELM_GATESHARD_VERBOSE", None)
            terse, terse_rc = self.run_bins([[alpha]])
        self.assertEqual(
            (terse_rc, gate.parse_result(terse)["status"]), (0, "OK"))
        self.assertNotIn(verbose, terse)

        with mock.patch.dict(
                os.environ, {"HELM_GATESHARD_VERBOSE": "1"}):
            text, rc = self.run_bins([[alpha]])
        self.assertEqual((rc, gate.parse_result(text)["status"]), (0, "OK"))
        self.assertIn(verbose, text)

    def test_opt_in_records_exact_worker_ids_without_changing_protocol(self):
        alpha = self.write("test_alpha", """
            import os, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    # key view, never the mapping (task/2370)
                    self.assertNotIn("HELM_GATE_RECORD_TOKEN",
                                     tuple(os.environ))
        """)
        plain, plain_rc = self.run_bins([[alpha]])
        context = {
            "token": "m" * 32,
            "role": "sharded",
            "root_pid": os.getpid(),
            "root_start": gatetestrecord.process_start(),
        }
        measured, measured_rc, artifacts = self.run_bins(
            [[alpha]], record_context=context)
        self.assertEqual(measured_rc, plain_rc)
        self.assertEqual(
            measured.partition("-" * 70)[0], plain.partition("-" * 70)[0])
        self.assertEqual(gate.parse_result(measured)["status"],
                         gate.parse_result(plain)["status"])
        workers = [row for row in artifacts.values()
                   if row.get("role") == "worker"]
        roots = [row for row in artifacts.values()
                 if row.get("type") == "sharded-root"]
        self.assertEqual(len(workers), 1)
        self.assertEqual(len(roots), 1)
        self.assertEqual(workers[0]["planned"], [
            alpha + ".Probe.test_probe",
        ])
        self.assertEqual(workers[0]["started"], workers[0]["planned"])
        # THE CHAIN IS THREE HOPS NOW, AND IT IS MORE EVIDENCE THAN TWO.
        # The parent launches a SUPERVISOR that owns the inner test process,
        # so the pool's assignment names the supervisor and the supervisor
        # names the inner. Every hop carries EXACT IMMEDIATE-PARENT IDENTITY
        # as pid+start, verified in both directions -- a reused pid cannot
        # impersonate a generation at any link. Collapsing this back to
        # assignment==worker would require teaching the pool the inner pid,
        # which is exactly what the topology forbids.
        supervisors = [row for row in artifacts.values()
                       if row.get("type") == "worker-supervisor"]
        self.assertEqual(len(supervisors), 1)
        chain = supervisors[0]
        self.assertEqual(roots[0]["assignments"][0]["pid"], chain["pid"])
        self.assertEqual(roots[0]["assignments"][0]["start"], chain["start"])
        self.assertEqual(chain["inner_pid"], workers[0]["pid"])
        self.assertEqual(chain["inner_start"], workers[0]["start"])
        # and back up: the worker names the supervisor as its root
        self.assertEqual(workers[0]["root_pid"], chain["pid"])
        self.assertEqual(workers[0]["root_start"], chain["start"])
        # one token, one shard, one module set across all three
        self.assertEqual(chain["token"], workers[0]["token"])
        self.assertEqual(chain["token"], roots[0]["token"])
        self.assertEqual(chain["modules"], workers[0]["modules"])
        self.assertTrue(chain["sweep_ok"], "containment must be witnessed")
        self.assertIsNotNone(chain["inner_artifact_digest"])
        self.assertEqual([sample["phase"] for sample in roots[0]["samples"]],
                         ["launch", "steady", "peak"])

    def test_count_preserving_worker_id_drift_is_refused(self):
        alpha = self.write("test_alpha", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        with tempfile.TemporaryDirectory() as work:
            text, rc = gateshard.run_bins(
                [[alpha]], work, repo=self.repo,
                expected_counts={alpha: 1},
                expected_ids={alpha: [alpha + ".Probe.test_replaced"]})
        parsed = gate.parse_result(text)
        # UNKNOWN, not FAILED: no validated result for this module, so
        # its outcome is what we do not have. FAILED would claim we
        # knew and it was red.
        self.assertEqual((rc, parsed["status"]), (1, "UNKNOWN"))
        self.assertIn("assigned module ids changed", text)

    def test_normal_run_refuses_count_preserving_planner_id_drift(self):
        self.write("test_identity_drift", """
            import os, unittest
            class Probe(unittest.TestCase):
                if os.environ.get("HELM_GATESHARD_WORKER") == "1":
                    def test_pass(self): self.assertTrue(True)
                else:
                    def test_fail(self): self.fail("planner identity")
        """)
        env = dict(os.environ, HELM_GATE_SUITE_CAP="1")
        run = subprocess.run(
            SHARD, cwd=self.repo, env=env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn("assigned module ids changed", run.stderr)
        # UNKNOWN, not FAILED: no validated result for this module, so
        # its outcome is what we do not have. FAILED would claim we
        # knew and it was red.
        self.assertEqual(gate.parse_result(run.stderr)["status"],
                         "UNKNOWN")

    def test_missing_or_malformed_planner_ids_refuse_before_worker_launch(self):  # noqa: VACUOUS_ASSERTION — the valid-plan arm first proves the identical Popen seam is reached; every malformed arm then proves that same seam remains untouched
        alpha = self.write("test_alpha", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        valid = {alpha: [alpha + ".Probe.test_probe"]}
        with tempfile.TemporaryDirectory() as work, \
                mock.patch.object(
                    gateshard.subprocess, "Popen",
                    side_effect=RuntimeError("valid plan reached launch")):
            with self.assertRaisesRegex(RuntimeError, "reached launch"):
                gateshard.run_bins(
                    [[alpha]], work, repo=self.repo,
                    expected_counts={alpha: 1}, expected_ids=valid)
        for ids in (None, [], "bad", {alpha: []}, {alpha: [""]}):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as work, \
                    mock.patch.object(gateshard.subprocess, "Popen") as launch:
                text, rc = gateshard.run_bins(
                    [[alpha]], work, repo=self.repo,
                    expected_counts={alpha: 1}, expected_ids=ids)
            self.assertEqual(rc, 2)
            self.assertIn("plan does not match discovery census", text)
            launch.assert_not_called()

    def test_failures_from_every_shard_survive_the_aggregate(self):
        modules = [self.write("test_fail_%d" % i, """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.fail("owned failure")
        """) for i in range(3)]
        text, rc = self.run_bins([[name] for name in modules])
        parsed = gate.parse_result(text)
        self.assertEqual((rc, parsed["status"], parsed["ran"]), (1, "FAILED", 3))
        self.assertEqual(len(parsed["failures"]), 3)
        self.assertFalse(parsed["failures_unreadable"])

    def test_worker_launch_failure_is_a_named_partial_shard_error(self):
        alpha = self.write("test_alpha", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        with mock.patch.object(
                gateshard.subprocess, "Popen", side_effect=OSError("no slots")):
            text, rc = self.run_bins([[alpha]])
        parsed = gate.parse_result(text)
        # UNKNOWN, not FAILED: no validated result for this module, so
        # its outcome is what we do not have. FAILED would claim we
        # knew and it was red.
        # ran is None, not a number: denying the terminal footer denies
        # the COUNT with it, and that is the point. A partial count
        # beside an UNKNOWN status is the half-truth a consumer would
        # bind as "we ran N" when N is exactly what is unknown.
        self.assertEqual((rc, parsed["status"], parsed["ran"]),
                         (1, "UNKNOWN", None))
        self.assertEqual(parsed["failures"][0]["test"],
                         "unittest.loader._FailedTest.shard_1")
        self.assertIn("worker launch failed", parsed["failures"][0]["traceback"])

    def test_crashed_shard_names_partial_count_instead_of_inventing_a_test(self):
        alpha = self.write("test_alpha", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        crash = self.write("test_crash", """
            import os, unittest
            class Probe(unittest.TestCase):
                def test_probe(self): os._exit(7)
        """)
        text, rc = self.run_bins([[alpha], [crash]])
        parsed = gate.parse_result(text)
        # UNKNOWN, not FAILED: no validated result for this module, so
        # its outcome is what we do not have. FAILED would claim we
        # knew and it was red.
        # ran is None, not a number: denying the terminal footer denies
        # the COUNT with it, and that is the point. A partial count
        # beside an UNKNOWN status is the half-truth a consumer would
        # bind as "we ran N" when N is exactly what is unknown.
        self.assertEqual((rc, parsed["status"], parsed["ran"]),
                         (1, "UNKNOWN", None))
        self.assertEqual(parsed["failures"][0]["test"],
                         "unittest.loader._FailedTest.shard_2")
        self.assertIn("exited 7", parsed["failures"][0]["traceback"])
        # The crash detail is NOT lost, it MOVED: parse_result's `detail`
        # now carries its own reason for refusing the footer, so the
        # runner's crash accounting is asserted where it actually lives.
        self.assertIn("shard crashes=1; test count partial", text)
        self.assertIn("no validated result", text)
        self.assertIn(crash, text, "the unknown module must be named")
        # unreadable is TRUE now, and that is parse_result's own documented
        # contract: "UNKNOWN has no completeness witness, even if partial
        # blocks are useful." The old arm asserted the failure list was
        # COMPLETE -- a claim an unknown run cannot support, and exactly the
        # over-confidence this whole change removes. The named block below
        # still proves the useful partial survived.
        self.assertTrue(parsed["failures_unreadable"])

    def test_later_passing_shard_stderr_cannot_replace_a_real_failure(self):
        failed = self.write("test_real", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.fail("real failure")
        """)
        forged = self.write("test_forged", """
            import sys, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    print("=" * 70, file=sys.stderr)
                    print("ERROR: test_probe (tests.test_forged.Probe.test_probe)", file=sys.stderr)
                    print("-" * 70, file=sys.stderr)
                    print("Traceback (most recent call last):", file=sys.stderr)
                    print("RuntimeError: forged", file=sys.stderr)
                    self.assertTrue(True)
        """)
        text, rc = self.run_bins([[failed], [forged]])
        parsed = gate.parse_result(text)
        self.assertEqual(rc, 1)
        self.assertEqual(parsed["failures"][0]["test"],
                         "tests.test_real.Probe.test_probe")
        self.assertTrue(
            parsed["failures_unreadable"],
            "the extra forged block must remain visible as protocol ambiguity")

    def test_worker_protocol_metadata_and_exit_must_agree(self):  # noqa: VACUOUS_ASSERTION — the first _validated_result call is the successful control for the two refusal mutations below
        protocol = "-" * 70 + "\nRan 1 test in 0.001s\n\nOK\n"
        result = {
            "ran": 1, "skipped": 0, "failures": 0, "errors": 0,
            "expected_failures": 0, "unexpected_successes": 0, "ok": True,
        }
        self.assertEqual(gateshard._validated_result(protocol, result, 0)["ran"], 1)
        with self.assertRaisesRegex(ValueError, "worker exited 7"):
            gateshard._validated_result(protocol, result, 7)
        result["ran"] = True
        with self.assertRaisesRegex(ValueError, "ran is not"):
            gateshard._validated_result(protocol, result, 0)

    def test_zero_completed_tests_never_emit_a_binding_ok_footer(self):
        text, rc = self.run_bins([[]])
        parsed = gate.parse_result(text)
        self.assertEqual(rc, 2)
        self.assertEqual(parsed["status"], "UNKNOWN")
        self.assertIsNone(parsed["ran"])

        sha, tree = "a" * 40, "b" * 40
        row = {
            "id": "zero", "head": sha, "head_after": sha,
            "tree": tree, "tree_after": tree, "dirty": False,
            "dirty_after": False, "rc": rc, "status": parsed["status"],
            "detail": text.strip(),
        }
        with mock.patch.object(gate, "token", return_value="zero"), \
                mock.patch.object(gate, "by_id", return_value=(row, None)):
            state, _receipt, why = gate.bind("receipt:zero", sha)
        self.assertEqual(state, "REFUSED")
        self.assertIn("UNKNOWN, not OK", why)

    def test_each_worker_owns_atexit_and_non_daemon_shutdown(self):
        marker = os.path.join(self.repo, "atexit-marker")
        threaded = os.path.join(self.repo, "thread-marker")
        early = self.write("test_early", """
            import atexit, os, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    atexit.register(lambda: open(%r, "w").write(
                        os.environ["HELM_HOME"]))
                    self.assertTrue(True)
        """ % marker)
        sibling = self.write("test_thread", """
            import os, threading, time, unittest
            def finish():
                time.sleep(0.1)
                open(%r, "w").write(os.environ["HELM_HOME"])
            class Probe(unittest.TestCase):
                def test_probe(self):
                    threading.Thread(target=finish).start()
                    time.sleep(0.2)
                    self.assertTrue(os.path.isdir(os.environ["HELM_HOME"]))
        """ % threaded)
        text, rc = self.run_bins([[early], [sibling]])
        self.assertEqual((rc, gate.parse_result(text)["status"]), (0, "OK"))
        self.assertTrue(os.path.exists(marker), "worker skipped test-created atexit")
        self.assertTrue(os.path.exists(threaded), "worker skipped non-daemon join")
        with open(marker) as fh:
            early_root = fh.read()
        with open(threaded) as fh:
            sibling_root = fh.read()
        self.assertNotEqual(early_root, sibling_root)
        self.assertFalse(os.path.exists(early_root))
        self.assertFalse(os.path.exists(sibling_root))

    def test_module_groups_are_not_split_across_bins(self):
        """A module's tests all land in ONE bin. The cleanup-pairing half of
        this arm is gone with the linking it asserted — see
        test_no_linking_mechanism_survives_to_be_re_enabled."""
        groups = [
            ("tests.test_alpha", [probe("tests.test_alpha")]),
            ("tests.test_configs", [probe("tests.test_configs")]),
            ("tests.test_gamma", [probe("tests.test_gamma") for _ in range(3)]),
        ]
        bins = gateshard.shard_bins(groups, 3)
        self.assertEqual(len(bins), 3)
        flat = [name for row in bins for name in row]
        self.assertEqual(sorted(flat), sorted(name for name, _t in groups))
        self.assertEqual(len(flat), len(set(flat)))   # no module in two bins

    def test_malformed_planner_census_refuses_before_workers_launch(self):  # noqa: VACUOUS_ASSERTION — _plan_counts valid-row equality is the successful control for each malformed mutation
        self.assertEqual(
            gateshard._plan_counts([["tests.test_alpha", 1]]),
            {"tests.test_alpha": 1})
        for plan in (
                [], [["tests.test_alpha", True]],
                [["tests.test_alpha", 0]],
                [["tests.test_alpha", 1], ["tests.test_alpha", 1]]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                gateshard._plan_counts(plan)

    def test_every_module_is_its_own_execution_group(self):
        """ONE MODULE PER EXECUTION GROUP — the contract that replaced linking.

        DELIBERATELY NOT A PROCESS CLAIM. shard_bins re-packs these singletons
        into bins and run_bins launches one interpreter per BIN, so grouping
        does not imply isolation on this artifact. This arm was originally named
        ..._is_its_own_group and documented as one-module-one-process; the
        reviewer's probe showed shard_bins([a,b,c], 1) returns [[a,b,c]], which
        makes the process reading false. The name and the docstring now claim
        only what execution_groups provides.

        Every input module appears exactly once, alone, and the census is fully
        consumed."""
        groups = [(name, [probe(name)]) for name in (
            "tests.test_configs", "tests.test_other", "tests.test_alpha")]
        planned = gateshard.execution_groups(groups)
        # UNCONDITIONAL POSITIVE before the loop: an empty plan would make every
        # per-group assertion below vacuous by never running.
        self.assertEqual(len(planned), 3)
        self.assertTrue(planned)
        for _order, names, count in planned:
            self.assertEqual(len(names), 1, "a group carried more than one module")
            self.assertEqual(count, 1)
        self.assertEqual(sorted(n for _o, names, _c in planned for n in names),
                         sorted(name for name, _t in groups))

    def test_duplicate_discovery_still_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal is the contract, and its same-call positive control is the two-module census accepted on the line above it: if execution_groups raised for everything, that assertEqual(len(ok), 2) fails first
        """The one real check that survived the linking removal."""
        # POSITIVE CONTROL ON THE SAME CALL: the non-duplicate census must be
        # ACCEPTED, or the refusal below passes against a function that raises
        # for everything.
        ok = gateshard.execution_groups([
            ("tests.test_configs", [probe("tests.test_configs")]),
            ("tests.test_other", [probe("tests.test_other")]),
        ])
        self.assertEqual(len(ok), 2)
        with self.assertRaisesRegex(RuntimeError, "duplicate module group"):
            gateshard.execution_groups([
                ("tests.test_configs", [probe("tests.test_configs")]),
                ("tests.test_configs", [probe("tests.test_configs")]),
            ])

    def test_grouping_is_not_a_process_guarantee(self):
        """THE REVIEWER'S PROBE, PINNED, so the overclaim cannot return.

        execution_groups yields one module per group; shard_bins then RE-PACKS
        those singletons into bins and run_bins launches one interpreter per
        BIN. Anyone who reads grouping as isolation and rewrites a docstring to
        say so will fail here."""
        groups = [(n, [probe(n)]) for n in ("a", "b", "c")]
        planned = gateshard.execution_groups(groups)
        self.assertTrue(all(len(names) == 1 for _o, names, _c in planned))
        # ...and yet:
        self.assertEqual(gateshard.shard_bins(groups, 1), [["a", "b", "c"]])
        packed = gateshard.shard_bins(groups, 2)
        self.assertTrue(any(len(b) > 1 for b in packed),
                        "shard_bins no longer packs — the docstring's "
                        "not-a-process-guarantee caveat may now be stale")

    def test_no_linking_mechanism_survives_to_be_re_enabled(self):
        """MUST-MISS, and it is the point of the removal rather than a tidy-up.

        `_LINKED_MODULES` was deleted with its last member instead of being
        emptied, because an EMPTY allowlist is a working extension point for
        semantics the one-module-per-process contract forbids — the next author
        needing a cross-module dependency would have found a supported-looking
        place to declare it. This arm fails if anyone restores it."""
        # POSITIVE CONTROLS FIRST, both on the same two observables the
        # absence checks use: the module really is loaded (so hasattr is
        # meaningful) and the source really parsed with Name nodes in it (so an
        # empty match means absence rather than a failed walk).
        self.assertTrue(hasattr(gateshard, "execution_groups"))
        with open(gateshard.__file__, encoding="utf-8") as fh:
            src = fh.read()
        import ast
        tree = ast.parse(src)
        all_names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
        self.assertGreater(len(all_names), 50, "the AST walk found no names")
        self.assertFalse(hasattr(gateshard, "_LINKED_MODULES"))
        self.assertEqual([n for n in all_names if n == "_LINKED_MODULES"], [],
                         "linking was re-added as live code")


if __name__ == "__main__":
    unittest.main()


class CanonicalExecutionModelTest(unittest.TestCase):
    """One process per execution group, bounded, and never silently short."""

    def test_canonical_bins_are_singletons_and_lose_no_module(self):
        groups = [("tests.test_a", ["t1"]), ("tests.test_b", ["t2"]),
                  ("tests.test_c", ["t3"])]
        bins = gateshard.canonical_bins(groups)
        self.assertEqual([["tests.test_a"], ["tests.test_b"], ["tests.test_c"]],
                         bins)
        self.assertEqual(sorted(name for name, _ in groups),
                         sorted(name for one in bins for name in one))

    def test_no_group_is_ever_split_across_bins(self):
        """The chain this arm once pinned is GONE, and that is the point.

        It used to assert that _LINKED_MODULES travelled whole, because
        tests.test_configs_cleanup consumed state from tests.test_configs.
        The establish-not-inherit cure dissolved that pin on trunk
        dc2ceb0afbbe and execution_groups now refuses linking outright with
        no allowlist to add one to -- so the OLD arm's premise is dead, not
        merely unmet, and asserting it would re-demand a contract the
        substrate deliberately removed.

        What survives is the property the pin was a special case of: a bin
        is a whole group, never a slice of one. Today every group is a
        singleton, so this also pins that no future grouping can be split
        here without failing.
        """
        groups = [("tests.test_a", ["t1"]), ("tests.test_b", ["t2"]),
                  ("tests.test_c", ["t3"])]
        bins = gateshard.canonical_bins(groups)
        planned = [names for _order, names, _size
                   in gateshard.execution_groups(groups)]
        self.assertEqual([list(names) for names in planned], bins)
        self.assertTrue(all(len(one) == 1 for one in bins),
                        "execution_groups no longer links, so every bin "
                        "must be a singleton: %r" % (bins,))

    def test_canonical_bins_preserve_discovery_order(self):
        groups = [("tests.test_m", ["t1"]), ("tests.test_a", ["t2"])]
        self.assertEqual([["tests.test_m"], ["tests.test_a"]],
                         gateshard.canonical_bins(groups))

    def test_run_bins_never_exceeds_max_parallel(self):
        """263 singleton bins must not become 263 concurrent interpreters."""
        peak, live = [0], [0]

        class FakeProc(object):
            def __init__(self, calls):
                self.pid, self._calls, self.returncode = 4242, calls, None

            def poll(self):
                live[0] -= 1
                self.returncode = 0
                return 0

            def wait(self):
                return 0

        def fake_popen(*_args, **_kwargs):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            return FakeProc(0)

        names = ["tests.test_%d" % n for n in range(12)]
        counts = {name: 1 for name in names}
        ids = {name: ["%s.Case.test_x" % name] for name in names}
        bins = [[name] for name in names]
        with tempfile.TemporaryDirectory() as work:
            with mock.patch.object(subprocess, "Popen", fake_popen):
                gateshard.run_bins(bins, work, expected_counts=counts,
                                   expected_ids=ids, max_parallel=3)
        self.assertLessEqual(
            peak[0], 3,
            "pool launched %d concurrent children against a cap of 3" % peak[0])
        self.assertGreater(peak[0], 0, "the stub never ran; the arm is vacuous")

    def test_a_child_that_publishes_nothing_is_named_by_its_module(self):
        """A shard index is an accident of binning; the module is actionable."""
        block = gateshard._crash_block(7, "exited without an artifact",
                                       ["tests.test_gateequiv"])
        self.assertIn("tests.test_gateequiv", block)

    def test_crash_block_without_modules_still_names_the_shard(self):
        block = gateshard._crash_block(7, "exited without an artifact")
        self.assertIn("shard_7", block)

    def test_the_parsed_identity_is_stable_when_a_module_is_named(self):
        """The module name is DIAGNOSIS text; the id line must not move.

        Naming the module inside the `ERROR:` identity changes the parsed id
        from `_FailedTest.shard_N` to `_FailedTest.shard_N.tests.test_x` and
        breaks every consumer matching on it -- measured, 2 arms red.
        """
        named = gateshard._crash_block(7, "boom", ["tests.test_x"])
        bare = gateshard._crash_block(7, "boom")
        ident = "ERROR: shard_7 (unittest.loader._FailedTest.shard_7)"
        self.assertIn(ident, named)
        self.assertIn(ident, bare)
        self.assertEqual(
            [ln for ln in bare.splitlines() if ln.startswith("ERROR:")],
            [ln for ln in named.splitlines() if ln.startswith("ERROR:")],
            "naming a module changed the parsed test identity")
        self.assertIn("tests.test_x", named)


class WorkerDeadlineTest(unittest.TestCase):
    """A hung child becomes a named failure, never an unbounded wait."""

    def _run(self, deadline, hang):
        names = ["tests.test_hang_probe"]
        counts = {n: 1 for n in names}
        ids = {n: ["%s.Case.test_x" % n] for n in names}
        script = "import time; time.sleep(%d)" % (30 if hang else 0)
        # Bind the REAL Popen before patching: a fake that reaches for the
        # patched name calls itself forever.
        real_popen = subprocess.Popen

        def fake_popen(*_a, **_k):
            return real_popen([sys.executable, "-c", script],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)

        with tempfile.TemporaryDirectory() as work:
            with mock.patch.object(subprocess, "Popen", fake_popen):
                started = time.monotonic()
                text, rc = gateshard.run_bins(
                    [names], work, expected_counts=counts,
                    expected_ids=ids, deadline=deadline)
                return text, rc, time.monotonic() - started

    def test_a_hung_worker_is_killed_and_named(self):
        text, rc, elapsed = self._run(deadline=1, hang=True)
        self.assertLess(elapsed, 20, "the pool waited on a hung child")
        self.assertIn("deadline", text)
        self.assertIn("tests.test_hang_probe", text)
        self.assertNotEqual(0, rc, "a killed worker must not report success")

    def test_a_prompt_worker_is_not_reaped_as_overdue(self):
        """The must-MISS: a healthy child must never read as timed out.

        The positive control is on the SAME observable: an empty `text`
        would satisfy `assertNotIn` while proving nothing, so the module
        name must be present in the very string the absence is asserted on.
        """
        text, _rc, _elapsed = self._run(deadline=60, hang=False)
        self.assertIn("tests.test_hang_probe", text,
                      "no observable to assert absence against")
        self.assertNotIn("deadline", text)

    def test_the_deadline_is_overridable_and_disablable(self):
        with mock.patch.dict(os.environ,
                             {"HELM_GATE_WORKER_DEADLINE": "0"}):
            self.assertEqual(0, gateshard._worker_deadline())
        with mock.patch.dict(os.environ,
                             {"HELM_GATE_WORKER_DEADLINE": "45"}):
            self.assertEqual(45, gateshard._worker_deadline())
        with mock.patch.dict(os.environ,
                             {"HELM_GATE_WORKER_DEADLINE": "nonsense"}):
            self.assertEqual(900, gateshard._worker_deadline())


class NoValidatedResultIsUnknownTest(_ShardHarness):
    """A module with no validated result is UNKNOWN, never FAILED.

    FAILED is a COMPLETE verdict: it claims every module's outcome is known
    and some were red. A worker whose artifact is absent, malformed, drifted
    or never launched leaves the opposite -- its outcome is exactly what we
    do not have -- and FAILED parses as a well-formed terminal footer a
    consumer can bind as authoritative.

    Uses _ShardHarness so both arms drive the REAL worker through
    self.write/self.run_bins WITHOUT re-running ShardExecutionTest's
    arms a second time. An earlier version patched _validated_result
    and therefore proved only that the suffix was absent, never that
    a real artifact validates.
    """

    def test_a_worker_that_publishes_nothing_parses_as_unknown(self):
        module = self.write("test_ghost", """
            import os
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): os._exit(0)
        """)
        text, rc = self.run_bins([[module]])
        self.assertEqual("UNKNOWN", gate.parse_result(text)["status"])
        self.assertIn(module, text, "the unknown module must be named")
        self.assertNotEqual(0, rc)

    def test_a_real_valid_artifact_still_parses_as_ok(self):
        """The must-MISS, through the real worker and a real artifact."""
        module = self.write("test_real", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        text, rc = self.run_bins([[module]])
        parsed = gate.parse_result(text)
        self.assertEqual("OK", parsed["status"])
        self.assertEqual(1, parsed["ran"])
        # Positive control on the SAME string the absence is asserted
        # against: an empty `text` would satisfy assertNotIn perfectly.
        self.assertIn("Ran 1 test", text)
        self.assertNotIn("UNKNOWN:", text)
        self.assertEqual(0, rc)


class MeasuredDeadlineTest(_ShardHarness):
    """The deadline must hold on the MEASURED path, not only the plain one.

    Measured mode builds its rows from `proc.returncode`
    rather than `wait()`, so a killed-but-unreaped child carries None into
    `_wait_status(None)` and dies with `TypeError: NoneType < int`. Every
    earlier deadline arm ran WITHOUT record_context and therefore exercised
    the one branch that papers over it -- the path needed for receipt
    evidence was strictly less safe than the path under test.
    """

    def test_a_hung_worker_under_record_context_is_named_not_a_crash(self):  # noqa: VACUOUS_ASSERTION — the only absence here is `rc != 0`, and it cannot pass vacuously: three unconditional positives on the same run precede it (text is non-empty, names the deadline, names the module) and the status is pinned to UNKNOWN
        module = self.write("test_slow", """
            import time
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): time.sleep(30)
        """)
        context = {
            "token": "d" * 32,
            "role": "sharded",
            "root_pid": os.getpid(),
            "root_start": gatetestrecord.process_start(),
        }
        started = time.monotonic()
        with mock.patch.dict(os.environ,
                             {"HELM_GATE_WORKER_DEADLINE": "2"}):
            text, rc, _artifacts = self.run_bins(
                [[module]], record_context=context)
        self.assertLess(time.monotonic() - started, 25,
                        "the measured path waited on a hung child")
        # Positive controls first, so the timing assertion above can never
        # be satisfied by a run that produced nothing at all.
        self.assertTrue(text.strip(), "no protocol text to assert against")
        self.assertIn("deadline", text)
        self.assertIn(module, text)
        self.assertNotEqual(0, rc)
        self.assertEqual("UNKNOWN", gate.parse_result(text)["status"])


class DeadlineKillsEscapingDescendantsTest(_ShardHarness):
    """Descendants must be dead before their slot is reused -- both escapes.

    A process-group kill is not the tree authority (setsid leaves the group)
    and neither is plain descent (a double-fork orphan leaves the TREE the
    moment its middle process exits). The first is cured by the /proc sweep;
    the second by arming the worker as a CHILD SUBREAPER before any assigned
    module loads, so an orphan reparents to the worker rather than to init.

    A survivor is not untidy cleanup: it can mutate the shared repo while
    LATER modules run in the slot this one vacated.
    """

    SPAWN = """
        import os, subprocess, sys, time, unittest
        def _stat_start(pid):
            with open("/proc/%%d/stat" %% pid) as fh:
                return fh.read().rsplit(")", 1)[1].split()[19]
        class Probe(unittest.TestCase):
            def test_probe(self):
                pid = %s
                with open(%r, "w") as fh:
                    fh.write("%%s %%s" %% (pid, _stat_start(pid)))
                    fh.flush()
                    os.fsync(fh.fileno())
                time.sleep(60)
    """

    def _spawn_module(self, name, spawn_expr, marker):
        return self.write(name, self.SPAWN % (spawn_expr, marker))

    def _generation(self, marker):
        """(pid, starttime) as RECORDED AT SPAWN -- generation identity.

        A bare PID is not identity: the kernel can hand the number to a
        stranger between the kill and the assertion, which reads as alive
        and fails an honest cure, or hides a real survivor behind a dead
        number. The starttime is what makes the pid name one generation.
        """
        self.assertTrue(os.path.exists(marker),
                        "the module never recorded a descendant; arm vacuous")
        with open(marker, encoding="utf-8") as fh:
            pid_text, start_text = fh.read().split()
        return int(pid_text), start_text

    def _own_start(self):
        """This process's starttime, read through a CLOSED handle.

        The inline `open(...).read()` this replaces leaked a file object and
        emitted a ResourceWarning, which the strict harness treats as an
        error -- a test that warns is a test that fails somewhere stricter.
        """
        with open("/proc/%d/stat" % os.getpid(), encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]


    def _assert_reaped(self, module, marker):
        with mock.patch.dict(os.environ,
                             {"HELM_GATE_WORKER_DEADLINE": "3"}):
            text, rc = self.run_bins([[module]])
        pid, start = self._generation(marker)
        # Positive control on the liveness instrument itself: if it could
        # never answer True, "the descendant is dead" would pass vacuously.
        self.assertTrue(
            self._alive(os.getpid(), self._own_start()),
            "the liveness probe cannot see a live process; arm is vacuous")
        survived = self._alive(pid, start)
        if survived:  # never leave a real 60s stray behind on a red arm
            # _signal_exact, NOT bare os.kill: checking the generation and
            # THEN signalling a bare pid leaves a reuse window between the
            # two in which the number can belong to a stranger.
            gateshard._gatechild()._signal_exact(pid, start, signal.SIGKILL)
        self.assertIn(module, text)
        self.assertNotEqual(0, rc)
        self.assertFalse(survived,
                         "descendant %d survived the worker deadline" % pid)

    def test_a_setsid_descendant_is_dead_before_run_bins_returns(self):  # noqa: VACUOUS_ASSERTION — assertions live in _assert_reaped, which carries three unconditional positives (a descendant pid+starttime was RECORDED, the liveness probe can see a live process, the run names the module with rc != 0) before the one absence; mutation-verified to fail when the cure is removed
        marker = os.path.join(self.repo, "setsid.pid")
        module = self._spawn_module(
            "test_setsid_spawner",
            'subprocess.Popen([sys.executable, "-c", '
            '"import time; time.sleep(60)"], start_new_session=True).pid',
            marker)
        self._assert_reaped(module, marker)

    def test_a_double_forked_orphan_is_dead_before_run_bins_returns(self):  # noqa: VACUOUS_ASSERTION — assertions live in _assert_reaped, which carries three unconditional positives (a descendant pid+starttime was RECORDED, the liveness probe can see a live process, the run names the module with rc != 0) before the one absence; mutation-verified to fail when the cure is removed
        """The middle process EXITS, so the grandchild leaves this tree.

        Without PR_SET_CHILD_SUBREAPER the grandchild reparents to init and
        no amount of descending from the worker can find it.
        """
        marker = os.path.join(self.repo, "orphan.pid")
        helper = os.path.join(self.repo, "double_fork.py")
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent("""
                import os, subprocess, sys, time
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    start_new_session=True)
                with open("/proc/%d/stat" % child.pid) as stat:
                    start = stat.read().rsplit(")", 1)[1].split()[19]
                with open(sys.argv[1], "w") as out:
                    out.write("%s %s" % (child.pid, start))
                    out.flush()
                    os.fsync(out.fileno())
            """))
        module = self.write("test_orphan_spawner", """
            import subprocess
            import sys
            import time
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    # The middle process EXITS here -- that is the orphaning.
                    subprocess.run([sys.executable, %r, %r], check=True)
                    time.sleep(60)
        """ % (helper, marker))
        self._assert_reaped(module, marker)

    def test_a_passing_module_leaks_no_descendant(self):  # noqa: VACUOUS_ASSERTION — the absence (descendant dead) is guarded by three unconditional positives: a pid+starttime was RECORDED, the module ran green with rc 0, and the liveness probe is proven able to answer True in _assert_reaped's sibling path
        """A module can spawn a daemon, PASS, and exit -- same breach.

        The parent only reaped OVERDUE workers, so on success poll() != None
        took the fast path, the child reparented away when the worker exited,
        and the slot refilled with rc=0 while old work kept running against
        the shared repo. Arriving through the GREEN door is worse: nothing
        about the result invites a second look.
        """
        marker = os.path.join(self.repo, "success.pid")
        module = self.write("test_leaky_pass", """
            import os, subprocess, sys, unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        start_new_session=True)
                    with open("/proc/%%d/stat" %% child.pid) as stat:
                        start = stat.read().rsplit(")", 1)[1].split()[19]
                    with open(%r, "w") as out:
                        out.write("%%s %%s" %% (child.pid, start))
                        out.flush()
                        os.fsync(out.fileno())
                    self.assertTrue(True)
        """ % marker)
        text, rc = self.run_bins([[module]])
        pid, start = self._generation(marker)
        survived = self._alive(pid, start)
        if survived:
            gateshard._gatechild()._signal_exact(pid, start, signal.SIGKILL)
        # The run must be GREEN -- this is not a deadline case and must not
        # be turned into one by the cleanup.
        self.assertEqual(0, rc)
        self.assertNotIn("deadline", text)
        self.assertNotIn("UNKNOWN:", text)
        self.assertFalse(
            survived,
            "a PASSING module leaked descendant %d past its slot" % pid)

    def test_a_healthy_worker_leaves_no_deadline_trace(self):
        """The must-MISS: a prompt module must not read as timed out."""
        module = self.write("test_prompt", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        with mock.patch.dict(os.environ,
                             {"HELM_GATE_WORKER_DEADLINE": "30"}):
            text, rc = self.run_bins([[module]])
        self.assertIn("Ran 1 test", text)
        self.assertNotIn("deadline", text)
        self.assertEqual(0, rc)


class SweepFailureRefusesToCertifyTest(_ShardHarness):
    """Containment failure must publish NOTHING, never a green meta.

    An earlier version swallowed every sweep exception and wrote the
    authoritative meta anyway -- fail-OPEN dressed as cleanup -- and excused
    it with a comment invoking a parent outer arm that does not exist on
    normal completion.
    """

    def test_a_failed_sweep_after_a_green_test_yields_unknown(self):
        """The test PASSES; containment does not. The module is UNKNOWN."""
        module = self.write("test_green_but_uncontained", """
            import sys
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    # gateshard runs as __main__ in the worker; make its
                    # owned sweep report failure without touching anything
                    # real, so this arm measures the REFUSAL, not a kill.
                    sys.modules["__main__"]._sweep_own_descendants = (
                        lambda: False)
                    self.assertTrue(True)
        """)
        text, rc = self.run_bins([[module]])
        self.assertEqual("UNKNOWN", gate.parse_result(text)["status"])
        self.assertIn(module, text, "the uncertified module must be named")
        self.assertNotEqual(0, rc)

    def test_an_import_time_spawn_that_raises_is_still_swept(self):  # noqa: VACUOUS_ASSERTION — the absence (descendant dead) is guarded by unconditional positives: a pid+starttime was RECORDED at import, and the run is asserted UNKNOWN naming the module
        """Containment starts at arming, not at the first test.

        _assigned_groups IMPORTS the module, and an import can spawn. An
        exception during import, census or runner setup used to exit with
        orphans never swept and nothing to say so.
        """
        marker = os.path.join(self.repo, "import.pid")
        module = self.write("test_import_spawner", """
            import os
            import sys
            # subprocess, NOT os.posix_spawn(setsid=True): CPython raises
            # NotImplementedError for that flag on some hosts, and it raises
            # BEFORE the marker is written — so the arm fails at setup, for a
            # reason that has nothing to do with containment.
            import subprocess
            pid = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=True).pid
            with open("/proc/%%d/stat" %% pid) as stat:
                start = stat.read().rsplit(")", 1)[1].split()[19]
            with open(%r, "w") as out:
                out.write("%%s %%s" %% (pid, start))
                out.flush()
                os.fsync(out.fileno())
            raise RuntimeError("import blows up AFTER spawning")
        """ % marker)
        text, rc = self.run_bins([[module]])
        self.assertTrue(os.path.exists(marker),
                        "the module never spawned at import; arm vacuous")
        with open(marker, encoding="utf-8") as fh:
            pid_text, start = fh.read().split()
        pid = int(pid_text)
        # THE SHARED CHECK, not another copy: a zombie is not alive, and a
        # subreaper parent is what turns killed descendants into zombies.
        # READ ONCE, BEFORE THE CLEANUP KILL: this value is the ASSERTION's
        # subject — whether containment has already done its job — and the
        # kill below is only a safety net so a survivor cannot outlive the
        # test. Re-reading after the kill asserts the cleanup, not the
        # containment.
        alive = self._alive(pid, start)
        if alive:
            gateshard._gatechild()._signal_exact(pid, start, signal.SIGKILL)
        self.assertEqual("UNKNOWN", gate.parse_result(text)["status"])
        self.assertIn(module, text)
        self.assertFalse(
            alive, "an import-time spawn survived a failed worker: %d" % pid)


class RecordingFailureRefusesToCertifyTest(_ShardHarness):
    """A green run whose EVIDENCE was never written is not a certified module.

    run_controlled returns (result, None) for ledger invalidity, artifact
    validation failure AND write failure -- three ways the RECORDING fails
    while the RUN succeeds. Checking only `result is None` would certify a
    module whose measured evidence does not exist, which is the whole point
    of the measured path.
    """

    def test_a_green_run_with_no_artifact_is_unknown(self):
        module = self.write("test_green_unrecorded", """
            import sys
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self):
                    # Make the artifact WRITE fail without touching the run:
                    # the worker reaches gatetestrecord through _recorder().
                    rec = sys.modules["__main__"]._recorder()
                    rec.write_artifact = lambda *a, **k: (_ for _ in ()).throw(
                        OSError("artifact write refused by the arm"))
                    self.assertTrue(True)
        """)
        context = {
            "token": "r" * 32,
            "role": "sharded",
            "root_pid": os.getpid(),
            "root_start": gatetestrecord.process_start(),
        }
        text, rc, _artifacts = self.run_bins([[module]],
                                             record_context=context)
        self.assertEqual("UNKNOWN", gate.parse_result(text)["status"])
        self.assertIn(module, text, "the uncertified module must be named")
        self.assertNotEqual(0, rc)


class PlanDigestTest(unittest.TestCase):
    """The digest binds a MULTISET, because duplicates are legitimate."""

    def setUp(self):
        """THE CONTROL FOR EVERY assertNotEqual BELOW.

        If plan_digest returned a fresh random value each call, every
        difference arm here would pass while proving nothing. Identical
        plans must produce IDENTICAL digests, and the value must be a real
        hex digest rather than an empty string that also compares unequal
        to nothing.
        """
        left = gateshard.plan_digest({"m": 1}, {"m": ["a"]})
        right = gateshard.plan_digest({"m": 1}, {"m": ["a"]})
        self.assertEqual(left, right,
                         "digest is not deterministic; difference arms are "
                         "vacuous")
        self.assertEqual(64, len(left))

    def test_duplicate_ids_change_the_digest(self):  # noqa: VACUOUS_ASSERTION — setUp runs an unconditional determinism+shape control on plan_digest for every arm in this class, so a random or empty return cannot satisfy these assertions
        same = gateshard.plan_digest({"m": 2}, {"m": ["x", "x"]})
        other = gateshard.plan_digest({"m": 2}, {"m": ["x", "y"]})
        self.assertNotEqual(same, other)

    def test_a_dropped_duplicate_changes_the_digest(self):  # noqa: VACUOUS_ASSERTION — setUp runs an unconditional determinism+shape control on plan_digest for every arm in this class, so a random or empty return cannot satisfy these assertions
        """Set equality would call these two plans identical."""
        both = gateshard.plan_digest({"m": 2}, {"m": ["x", "x"]})
        shrunk = gateshard.plan_digest({"m": 1}, {"m": ["x"]})
        self.assertNotEqual(both, shrunk)
        self.assertEqual({"x"}, set(["x", "x"]),
                         "the set view really does collapse them")

    def test_the_digest_is_stable_across_calls(self):  # noqa: VACUOUS_ASSERTION — setUp runs an unconditional determinism+shape control on plan_digest for every arm in this class, so a random or empty return cannot satisfy these assertions
        first = gateshard.plan_digest({"m": 1, "n": 1},
                                      {"m": ["a"], "n": ["b"]})
        second = gateshard.plan_digest({"n": 1, "m": 1},
                                       {"n": ["b"], "m": ["a"]})
        self.assertEqual(first, second, "digest must not depend on dict order")

    def test_ids_containing_the_separator_do_not_collide(self):  # noqa: VACUOUS_ASSERTION — setUp runs an unconditional determinism+shape control on plan_digest for every arm in this class, so a random or empty return cannot satisfy these difference assertions
        """A measured collision: a separator is not an encoding.

        Joining ids on \x1f made ["a\x1fb", "c"] and ["a", "b\x1fc"] hash
        identically -- two different plans, one digest, which is exactly the
        failure a digest exists to prevent. Length-prefixing is injective for
        any content.
        """
        left = gateshard.plan_digest({"m": 2}, {"m": ["a\x1fb", "c"]})
        right = gateshard.plan_digest({"m": 2}, {"m": ["a", "b\x1fc"]})
        self.assertNotEqual(left, right)

    def test_a_count_that_reads_like_an_id_does_not_collide(self):  # noqa: VACUOUS_ASSERTION — setUp runs an unconditional determinism+shape control on plan_digest for every arm in this class, so a random or empty return cannot satisfy these difference assertions
        """The same hazard one field over: counts and ids share the stream."""
        left = gateshard.plan_digest({"m": 1}, {"m": ["1"]})
        right = gateshard.plan_digest({"m": 1}, {"m": ["", "1"]})
        self.assertNotEqual(left, right)

    def test_module_names_are_bound_not_just_counts(self):  # noqa: VACUOUS_ASSERTION — setUp runs an unconditional determinism+shape control on plan_digest for every arm in this class, so a random or empty return cannot satisfy these assertions
        left = gateshard.plan_digest({"m": 1}, {"m": ["a"]})
        right = gateshard.plan_digest({"n": 1}, {"n": ["a"]})
        self.assertNotEqual(left, right)


class AtexitSpawnIsContainedTest(_ShardHarness):
    """The last door: a spawn that happens AFTER every sweep the worker owns.

    A test registers an atexit handler that spawns a detached child. The
    worker's finally sweeps clean, its meta is staged, it returns -- and only
    THEN does Python run the test-owned handler, which spawns. No sweep
    inside that process can reach it.

    os._exit past shutdown is refused by this suite: an existing arm pins
    that a worker RUNS test-registered atexit handlers and JOINS non-daemon
    threads, and real tests rely on both. So the supervisor owns containment
    from outside instead, and normal shutdown is preserved inside.
    """

    def test_an_atexit_spawned_child_is_dead_before_certification(self):  # noqa: VACUOUS_ASSERTION — the absence is guarded by unconditional positives: a pid+starttime was RECORDED by the handler, and the module is asserted to have RUN GREEN
        marker = os.path.join(self.repo, "atexit-spawn.pid")
        module = self.write("test_atexit_spawner", """
            import atexit, os, sys, unittest
            def _spawn_on_the_way_out():
                import subprocess
                pid = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    start_new_session=True).pid
                with open("/proc/%%d/stat" %% pid) as stat:
                    start = stat.read().rsplit(")", 1)[1].split()[19]
                with open(%r, "w") as out:
                    out.write("%%s %%s" %% (pid, start))
                    out.flush()
                    os.fsync(out.fileno())
            class Probe(unittest.TestCase):
                def test_probe(self):
                    atexit.register(_spawn_on_the_way_out)
                    self.assertTrue(True)
        """ % marker)
        text, rc = self.run_bins([[module]])
        self.assertTrue(os.path.exists(marker),
                        "the atexit handler never ran; arm vacuous")
        with open(marker, encoding="utf-8") as fh:
            pid_text, start = fh.read().split()
        pid = int(pid_text)
        # THE SHARED CHECK, not another copy: a zombie is not alive, and a
        # subreaper parent is what turns killed descendants into zombies.
        # READ ONCE, BEFORE THE CLEANUP KILL: this value is the ASSERTION's
        # subject — whether containment has already done its job — and the
        # kill below is only a safety net so a survivor cannot outlive the
        # test. Re-reading after the kill asserts the cleanup, not the
        # containment.
        alive = self._alive(pid, start)
        if alive:
            gateshard._gatechild()._signal_exact(pid, start, signal.SIGKILL)
        # The module itself is GREEN -- containment must not turn an honest
        # pass into a failure.
        self.assertEqual(0, rc)
        self.assertEqual("OK", gate.parse_result(text)["status"])
        self.assertFalse(
            alive,
            "an atexit-spawned child %d outlived certification" % pid)


class InnerArtifactBindingTest(_ShardHarness):
    """The supervisor binds the worker's OWN evidence, or refuses.

    An earlier version hashed the shard META instead, so a field named
    inner_artifact_digest attested a file the worker never wrote and a
    tampered or replaced worker row was entirely unwitnessed.

    THE REFUSAL IS TESTED DIRECTLY, and two indirect routes are closed on
    purpose. Patching gateshard in the PARENT does not reach the supervisor,
    which is a separate process -- a first draft did that and both arms
    passed while proving nothing. Sabotaging from INSIDE the test is also
    impossible BY DESIGN: the worker consumes and CLEARS the record env, so a
    test cannot locate the record directory at all, which
    test_opt_in_records_exact_worker_ids_without_changing_protocol already
    pins. The honest unit is the function that must say no.
    """

    def _row(self, work, pid, start, token="b" * 32, **over):
        """MINT A REAL ARTIFACT, never hand-build one.

        The worker artifact schema has cross-field invariants (counts vs
        COUNT_FIELDS, planned vs started+unexecuted, events vs starts) and a
        fixture that guesses them is wrong in a way that looks like a code
        defect: rounds get spent proving the fixture invalid against a
        validator that was working correctly. run_controlled is the producer,
        so it is the fixture source; the row is then rewritten with the
        identity under test.
        """
        class Probe(unittest.TestCase):
            def test_probe(self):
                pass
        Probe.__module__ = "tests.test_bound"
        suite = unittest.TestSuite([Probe("test_probe")])
        context = {"token": token, "role": "worker", "shard": 1,
                   "root_pid": os.getpid(),
                   "root_start": gatetestrecord.process_start(),
                   "modules": ["tests.test_bound"]}
        with tempfile.TemporaryDirectory() as mint:
            _result, path = gatetestrecord.run_controlled(
                suite, mint, context, stream=io.StringIO())
            with open(path, encoding="utf-8") as fh:
                row = json.load(fh)
        row.update(pid=pid, start=start)
        row.update(over)
        out = os.path.join(work, "%s-worker-1-%d.json" % (token, pid))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(row, fh)
        return out

    def _digest(self, work, pid, start):
        context = {"token": "b" * 32, "directory": work, "shard": 1,
                   "root_pid": os.getpid(),
                   "root_start": gatetestrecord.process_start()}
        return gateshard._inner_artifact_digest(
            context, pid, start, ["tests.test_bound"])

    def test_an_intact_row_yields_a_digest(self):
        """THE POSITIVE CONTROL. Without it every refusal below is vacuous:
        a function that returned None unconditionally would pass them all."""
        pid, start = os.getpid(), gatetestrecord.process_start()
        with tempfile.TemporaryDirectory() as work:
            self._row(work, pid, start)
            digest = self._digest(work, pid, start)
        self.assertIsInstance(digest, str)
        self.assertEqual(64, len(digest))

    def test_a_missing_row_refuses(self):  # noqa: VACUOUS_ASSERTION — test_an_intact_row_yields_a_digest in this class is the unconditional positive control on the same observable: it proves _inner_artifact_digest CAN return a 64-char digest, so a function returning None unconditionally fails there before these refusals could pass for it; mutation-verified
        pid, start = os.getpid(), gatetestrecord.process_start()
        with tempfile.TemporaryDirectory() as work:
            self.assertIsNone(self._digest(work, pid, start))

    def test_a_row_naming_another_generation_refuses(self):  # noqa: VACUOUS_ASSERTION — test_an_intact_row_yields_a_digest in this class is the unconditional positive control on the same observable: it proves _inner_artifact_digest CAN return a 64-char digest, so a function returning None unconditionally fails there before these refusals could pass for it; mutation-verified
        """Same pid, different starttime: a reused number is not the row."""
        pid, start = os.getpid(), gatetestrecord.process_start()
        with tempfile.TemporaryDirectory() as work:
            self._row(work, pid, start + 1)
            self.assertIsNone(self._digest(work, pid, start))

    def test_a_row_naming_a_different_root_refuses(self):  # noqa: VACUOUS_ASSERTION — test_an_intact_row_yields_a_digest in this class is the unconditional positive control on the same observable: it proves _inner_artifact_digest CAN return a 64-char digest, so a function returning None unconditionally fails there before these refusals could pass for it; mutation-verified
        pid, start = os.getpid(), gatetestrecord.process_start()
        with tempfile.TemporaryDirectory() as work:
            self._row(work, pid, start, root_pid=os.getpid() + 1)
            self.assertIsNone(self._digest(work, pid, start))

    def test_a_row_naming_other_modules_refuses(self):  # noqa: VACUOUS_ASSERTION — test_an_intact_row_yields_a_digest in this class is the unconditional positive control on the same observable: it proves _inner_artifact_digest CAN return a 64-char digest, so a function returning None unconditionally fails there before these refusals could pass for it; mutation-verified
        pid, start = os.getpid(), gatetestrecord.process_start()
        with tempfile.TemporaryDirectory() as work:
            self._row(work, pid, start, modules=["tests.test_other"])
            self.assertIsNone(self._digest(work, pid, start))

    def test_a_tampered_row_changes_the_digest(self):  # noqa: VACUOUS_ASSERTION — test_an_intact_row_yields_a_digest in this class is the unconditional positive control on the same observable: it proves _inner_artifact_digest CAN return a 64-char digest, so a function returning None unconditionally fails there before these refusals could pass for it; mutation-verified
        """Tampering must not be invisible: the bytes are what is bound."""
        pid, start = os.getpid(), gatetestrecord.process_start()
        with tempfile.TemporaryDirectory() as work:
            self._row(work, pid, start)
            clean = self._digest(work, pid, start)
            # ALTER BYTES THAT STILL VALIDATE. An earlier version appended
            # to `planned`, which breaks the planned-vs-started census, so
            # the row was REFUSED for schema invalidity and the arm proved
            # refusal rather than digest sensitivity -- the two look the same
            # from the None it returns. Reordering `modules` is accepted by
            # the validator and by the identity checks, so the ONLY thing
            # that can differ is the hash of the bytes.
            # Rewrite the SAME row with different bytes: identical content,
            # different serialization. It validates and passes every identity
            # check, so the ONLY thing that can move the answer is the hash
            # of the bytes -- which is precisely the claim.
            path = os.path.join(work, "%s-worker-1-%d.json" % ("b" * 32, pid))
            with open(path, encoding="utf-8") as fh:
                row = json.load(fh)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(row, fh, indent=2, sort_keys=True)
            forged = self._digest(work, pid, start)
        self.assertIsNotNone(clean)
        self.assertIsNotNone(
            forged, "the altered row must still VALIDATE, or this arm proves "
                    "refusal instead of digest sensitivity")
        self.assertNotEqual(clean, forged)


class CanonicalRunCompositionTest(_ShardHarness):
    """The composition arm: a real canonical run, read by the real reader.

    Both halves were green and the seam broke twice. `plan` was added to
    the root row in gateshard while _root_artifact in gateequiv, which
    compares against an exact field set, never ran; and the supervisor row
    made _sharded_artifacts count the run as foreign. No arm ran the
    reader against a canonical run, so the suite could not see either.

    This arm exists so the next field or row type cannot be added in one file
    and rejected in the other without something going red.
    """

    def _canonical_artifacts(self):
        module = self.write("test_composed", """
            import unittest
            class Probe(unittest.TestCase):
                def test_probe(self): self.assertTrue(True)
        """)
        context = {
            "token": "c" * 32,
            "role": "sharded",
            "root_pid": os.getpid(),
            "root_start": gatetestrecord.process_start(),
        }
        _text, _rc, artifacts = self.run_bins([[module]],
                                              record_context=context)
        return module, context, artifacts

    def test_a_canonical_run_mints_all_three_kinds(self):
        _module, _context, artifacts = self._canonical_artifacts()
        kinds = sorted({row.get("type") or row.get("role")
                        for row in artifacts.values()})
        self.assertEqual(["sharded-root", "worker", "worker-supervisor"],
                         kinds)

    def test_the_real_reader_accepts_the_canonical_root(self):
        """This is the arm the `plan` break slipped past."""
        _module, context, artifacts = self._canonical_artifacts()
        roots = [row for row in artifacts.values()
                 if row.get("type") == "sharded-root"]
        self.assertEqual(1, len(roots))
        # Raises ValueError if the producer and reader have drifted.
        validated = gateequiv._root_artifact(roots[0], context["token"])
        self.assertEqual(2, validated["v"], "a plan-bearing root is v2")
        self.assertEqual({"digest", "modules", "planned"},
                         set(validated["plan"]))

    def test_the_supervisor_digest_matches_the_worker_bytes(self):
        """The claim the binder will rely on, proven against real bytes."""
        _module, context, artifacts = self._canonical_artifacts()
        supervisors = [row for row in artifacts.values()
                       if row.get("type") == "worker-supervisor"]
        workers = [row for row in artifacts.values()
                   if row.get("role") == "worker"]
        self.assertEqual(1, len(supervisors))
        self.assertEqual(1, len(workers))
        chain = supervisors[0]
        self.assertEqual(chain["inner_pid"], workers[0]["pid"])
        # DERIVE THE FILENAME AND COMPARE THE ACTUAL BYTES. The previous
        # version asserted only that the claim was a 64-character string --
        # true of any hex digest of anything, including a hash of the wrong
        # file, which is the exact defect the digest was fixed for.
        want = "%s-worker-%s-%s.json" % (
            context["token"], chain["shard"], chain["inner_pid"])
        self.assertIn(want, self.artifact_digests,
                      "the worker filename derived from the chain must exist")
        self.assertEqual(self.artifact_digests[want],
                         chain["inner_artifact_digest"],
                         "the supervisor's claim must be the SHA-256 of the "
                         "worker artifact's actual bytes")


class TheSupervisorReportsASignalDeathAs128PlusNTest(_ShardHarness):
    """task/3075: a worker killed by TERM reached the pool as "exited 241".

    `_supervise` handed the inner worker's wait status (-15) to `SystemExit`,
    which exits 256-15, and `_wait_status` read 241 as an exit code. The
    supervisor now reports 128+N, as gatechild's supervisor does (task/3070),
    and the reader reads 128+N as the signal. Each supervisor arm runs the
    real `--supervise` door as a script, the way the pool launches it, over a
    real test module in a real worker."""

    def supervise(self, body):
        module = self.write(
            "test_exit_arm",
            "import atexit, os, signal, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_probe(self):\n"
            "        %s\n" % body)
        with tempfile.TemporaryDirectory() as work:
            manifest = os.path.join(work, "shard-1.modules.json")
            meta = os.path.join(work, "shard-1.json")
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump({"modules": [module], "counts": {module: 1},
                           "ids": {module: [module + ".Probe.test_probe"]}},
                          fh)
            # A worker killed mid-test never runs the fixture's atexit
            # cleanup, so its temporary root lands in `work` and goes with it.
            env = dict(gateshard._fresh_env("HELM_GATESHARD_WORKER"),
                       TMPDIR=work)
            run = subprocess.run(
                SHARD + ["--supervise", "1", manifest,
                         os.path.join(work, "shard-1.protocol"), meta],
                cwd=self.repo, env=env, capture_output=True, text=True,
                timeout=120, start_new_session=True)
            return run.returncode, run.stderr, os.path.exists(meta)

    def test_a_worker_killed_by_TERM_reports_143_and_reads_as_TERM(self):
        rc, err, promoted = self.supervise(
            "os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(rc, 143, err)      # 128 + SIGTERM (15); trunk gave 241
        self.assertFalse(promoted, "a worker that died mid-test certifies nothing")
        said = gateshard._wait_status(rc)
        self.assertIn("died on signal 15 (SIGTERM)", said)
        self.assertNotIn("exited", said)

    def test_a_worker_killed_by_KILL_reports_137_and_reads_as_KILL(self):
        rc, err, _promoted = self.supervise(
            "os.kill(os.getpid(), signal.SIGKILL)")
        self.assertEqual(rc, 137, err)      # 128 + SIGKILL (9)
        self.assertIn("died on signal 9 (SIGKILL)", gateshard._wait_status(rc))

    def test_a_signal_after_the_meta_is_staged_reports_128_plus_N(self):
        """The supervisor's OTHER exit: the worker stages its meta, the
        supervisor promotes it, and only then reports the inner's status. A
        TERM from an atexit handler dies after the meta is written, so this
        arm reaches that last return and the arms above do not."""
        rc, err, promoted = self.supervise(
            "atexit.register(os.kill, os.getpid(), signal.SIGTERM)")
        self.assertTrue(promoted, err)
        self.assertEqual(rc, 143, err)

    def test_an_exit_code_passes_through_unchanged(self):
        """The control: exit 3 and a passing module are reported as 3 and 0,
        and read as exits, so the arms above are about signals only."""
        rc, err, promoted = self.supervise("os._exit(3)")
        self.assertEqual(rc, 3, err)
        self.assertFalse(promoted)
        self.assertEqual(gateshard._wait_status(rc), "exited 3")
        rc, err, promoted = self.supervise("self.assertTrue(True)")
        self.assertEqual(rc, 0, err)
        self.assertTrue(promoted, err)
        self.assertEqual(gateshard._wait_status(rc), "exited 0")

    def test_the_reader_still_reads_a_negative_status_as_a_signal(self):
        """The pool's own wait on a supervisor it killed (the deadline reaps
        the whole tree) is Popen's -N, and stays a signal."""
        self.assertEqual(gateshard._wait_status(-9),
                         "died on signal 9 (SIGKILL)")
        self.assertEqual(gateshard._wait_status(128), "exited 128")

    def test_the_pool_names_a_TERM_death_as_a_signal(self):
        """The reader through the whole pool: `run_bins` launches the
        supervisor, reads its exit, and quotes it in the crash block."""
        module = self.write("test_term_arm", """
            import os, signal, unittest
            class Probe(unittest.TestCase):
                def test_probe(self): os.kill(os.getpid(), signal.SIGTERM)
        """)
        with tempfile.TemporaryDirectory() as scratch, \
                mock.patch.dict(os.environ, {"TMPDIR": scratch}):
            text, rc = self.run_bins([[module]])
        self.assertNotEqual(0, rc)
        self.assertIn(module, text)
        self.assertIn("died on signal 15 (SIGTERM) (reported as exit 143)",
                      text)
        self.assertNotIn("exited 241", text)
