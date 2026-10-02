#!/usr/bin/env python3
"""Recorded loads (task/3039 lane 2): the recorder, the record, the recorded
selection, the planner that uses it and the binder that re-derives it.

THE RECORDER ARMS RUN IN A CHILD INTERPRETER. The recorder replaces
`builtins.__import__` and adds an audit hook, and neither belongs in the
process running this suite (which may itself be a recorded run), so each arm
drives a scratch project through a real unittest run in its own python.

MUST-MISS DISCIPLINE: every inclusion arm has an exclusion beside it, and the
lazy-import arm has a PLANT: the same run with the recorder off must lose the
lazy import, or the inclusion arm proves nothing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from helm import dispatches, foldcheck, gate, gateaudits, gateauthority, \
    gateimport, gateloads, gatetestrecord, landgate, vcs
from tests._tmphome import pin_live_seats
from tests.test_gate_focus import FocusBase, GAMMA_TEST

_TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GATELOADS = os.path.join(_TREE, "helm", "gateloads.py")

# The scratch project: pkg.a imports c at module scope and b LAZILY inside a
# function; b reads a data file when called; d is imported by a module setup;
# e only by a child process a test starts.
_PROJECT = {
    "pkg/__init__.py": "",
    "pkg/a.py": """\
        from . import c
        def lazy():
            from . import b
            return b.value()
        """,
    "pkg/b.py": """\
        import os
        def value():
            with open(os.path.join(os.path.dirname(__file__), "data.txt")) as fh:
                return fh.read().strip()
        """,
    "pkg/data.txt": "7\n",
    "pkg/c.py": "C = 1\n",
    "pkg/d.py": "D = 4\n",
    "pkg/e.py": "E = 5\n",
    "pkg/g.py": "G = 7\n",
    "pkg2/__init__.py": "",
    "pkg2/x.py": """\
        import os
        with open(os.path.join(os.path.dirname(__file__), "x.txt")) as fh:
            X = fh.read()
        """,
    "pkg2/x.txt": "x\n",
    "pkg2/y.py": "Y = 1\n",
    "tests/__init__.py": "",
    # eight imports the package and two submodules in ONE fresh statement;
    # nine, after it, imports only the package.
    "tests/test_eight.py": """\
        import unittest
        from pkg2 import x, y
        class T(unittest.TestCase):
            def test_eight(self):
                self.assertEqual((x.X, y.Y), ("x\\n", 1))
        """,
    "tests/test_nine.py": """\
        import unittest
        import pkg2
        class T(unittest.TestCase):
            def test_nine(self):
                self.assertTrue(pkg2)
        """,
    "tests/test_one.py": """\
        import unittest
        from pkg import a
        class T(unittest.TestCase):
            def test_one(self):
                self.assertEqual(a.lazy(), "7")
        """,
    # twelve starts a child whose environment lost every recording key, as
    # a test that runs `bin/helm` under a clean environment does.
    "tests/test_twelve.py": """\
        import os, subprocess, sys, unittest
        CHILD = (
            "import importlib.util, sys\\n"
            "spec = importlib.util.spec_from_file_location('_gl', %r)\\n"
            "gl = importlib.util.module_from_spec(spec)\\n"
            "spec.loader.exec_module(gl)\\n"
            "gl.arm_child(environ={}, root=%r)\\n"
            "sys.path.insert(0, %r)\\n"
            "import pkg.g\\n")
        class T(unittest.TestCase):
            def test_twelve(self):
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                subprocess.run([sys.executable, "-c",
                                CHILD % (os.environ["GATELOADS_UNDER_TEST"],
                                         root, root)],
                               env={"PATH": os.environ.get("PATH", "")},
                               check=True)
        """,
    "tests/test_two.py": """\
        import unittest
        def setUpModule():
            from pkg import d
        class T(unittest.TestCase):
            def test_two(self):
                import pkg
                self.assertTrue(pkg)
        """,
    "tests/test_three.py": """\
        import os, subprocess, sys, unittest
        CHILD = (
            "import importlib.util, sys\\n"
            "spec = importlib.util.spec_from_file_location('_gl', %r)\\n"
            "gl = importlib.util.module_from_spec(spec)\\n"
            "spec.loader.exec_module(gl)\\n"
            "gl.arm_child()\\n"
            "sys.path.insert(0, %r)\\n"
            "import pkg.e\\n")
        class T(unittest.TestCase):
            def test_three(self):
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                subprocess.run([sys.executable, "-c",
                                CHILD % (os.environ["GATELOADS_UNDER_TEST"], root)],
                               check=True)
        """,
}
_TAMPER = """\
    import builtins, unittest
    class T(unittest.TestCase):
        def test_tamper(self):
            real = builtins.__import__
            builtins.__import__ = lambda *a, **k: real(*a, **k)
    """
_DRIVER = """\
import importlib.util, os, sys, unittest
path, mode, out = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("_gateloads_under_test", path)
gl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl)
root = os.getcwd()
sys.path.insert(0, root)
directory = os.path.join(out, "loads")
os.makedirs(directory)
if mode == "off":
    rec = gl.Recorder(root)
else:
    os.environ.update({gl.DIR_ENV: directory, gl.ROOT_ENV: root,
                       gl.RUNNER_ENV: "1"})
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    gl.write_json(os.path.join(root, ".git"), gl.MARKER, {"dir": directory})
    rec = gl.arm_runner(root)
    assert gl.RUNNER_ENV not in os.environ
suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
rec.begin_run()
result = unittest.TextTestRunner(
    stream=sys.stderr,
    resultclass=rec.result_class(unittest.TextTestResult)).run(suite)
if mode == "off":
    gl.write_json(directory, "runner-%d.json" % os.getpid(), rec.finish())
else:
    gl.write_runner(rec)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(text))


def _clean_env(**extra):
    """This process's environment without an OUTER recording's keys: the
    scratch run records into its own directory, never this suite's."""
    env = {k: v for k, v in os.environ.items()
           if k not in gateloads.ENV_KEYS}
    env.update(extra)
    return env


def setUpModule():
    # The verdict arms write dispatch rows (tests/test_env_hygiene.py).
    pin_live_seats()


class _Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gateloads-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "project")
        for rel, text in _PROJECT.items():
            _write(self.root, rel, text)

    def files(self):
        out = []
        for here, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            out += [os.path.relpath(os.path.join(here, n), self.root)
                    for n in names if n.endswith(".py")]
        return sorted(out)

    def drive(self, mode="on"):
        """One real unittest run of the project under the recorder. ->
        (record, reason, children)"""
        out = os.path.join(self.tmp, "out-%s" % mode)
        os.makedirs(out)
        driver = os.path.join(self.tmp, "driver.py")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER)
        proc = subprocess.run(
            [sys.executable, driver, _GATELOADS, mode, out], cwd=self.root,
            env=_clean_env(GATELOADS_UNDER_TEST=_GATELOADS),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        runners, children, errors = gateloads.read_artifacts(
            os.path.join(out, "loads"))
        self.assertEqual(errors, [])
        compact, _stats, reason = gateloads.build_record(
            runners, children, self.root, self.files())
        record = gateloads.expand(compact) if compact else None
        return record, reason, children


class RecorderArms(_Project):
    def test_a_lazy_import_and_a_file_read_are_charged_to_their_test(self):
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        one, two = record["tests.test_one"], record["tests.test_two"]
        self.assertIn("pkg/b.py", one)
        for path in ("pkg/b.py", "pkg/data.txt", "pkg/a.py", "pkg/c.py",
                     "tests/test_one.py", "tests/__init__.py"):
            self.assertIn(path, one)
        # MUST-MISS: the module that never called a.lazy() reached neither.
        self.assertNotIn("pkg/b.py", two)
        self.assertNotIn("pkg/data.txt", two)

    def test_a_package_body_is_not_charged_with_its_submodules(self):
        """One fresh `from pkg2 import x, y` runs three bodies in one call.
        Each is its own frame, so a test that reaches only the package does
        not inherit x's module-scope read."""
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertIn("pkg2/x.txt", record["tests.test_eight"])
        self.assertIn("pkg2/__init__.py", record["tests.test_nine"])
        self.assertNotIn("pkg2/x.txt", record["tests.test_nine"])
        self.assertNotIn("pkg2/x.py", record["tests.test_nine"])

    def test_a_module_read_as_text_is_a_leaf(self):
        """A census reads source; it does not run what that source imports.
        Following a read file's import edges put every hub in every census's
        record (measured: a suite census reached helm/chat.py that way)."""
        _write(self.root, "tests/test_eleven.py", """\
            import os, unittest
            class T(unittest.TestCase):
                def test_eleven(self):
                    root = os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__)))
                    with open(os.path.join(root, "pkg", "a.py")) as fh:
                        self.assertIn("lazy", fh.read())
            """)
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertIn("pkg/a.py", record["tests.test_eleven"])
        self.assertNotIn("pkg/c.py", record["tests.test_eleven"])
        # CONTROL: the module that IMPORTS a still reaches c through it.
        self.assertIn("pkg/c.py", record["tests.test_one"])

    def test_the_import_systems_own_listing_is_no_directory_read(self):
        """A path finder lists a package's directory to fill its cache; a
        test that only imports the package listed nothing. A test that DOES
        list a directory keeps it (test_three lists its tests/)."""
        _write(self.root, "tests/test_ten.py", """\
            import os, unittest
            class T(unittest.TestCase):
                def test_ten(self):
                    here = os.path.dirname(os.path.abspath(__file__))
                    self.assertIn("pkg", os.listdir(os.path.dirname(here)))
            """)
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertNotIn("pkg2/", record["tests.test_nine"])
        self.assertNotIn("tests/", record["tests.test_nine"])
        self.assertIn(gateloads.ROOT_DIR, record["tests.test_ten"])

    def test_the_recorder_off_loses_the_lazy_import(self):
        """THE PLANT. Without the wrappers only the module-scope closure is
        left, so the lazy import and the file read vanish: the inclusion arm
        above is only green because the recorder saw them."""
        record, reason, _children = self.drive(mode="off")
        self.assertIsNone(reason)
        one = record["tests.test_one"]
        self.assertIn("pkg/c.py", one)      # module scope: still there
        self.assertNotIn("pkg/b.py", one)
        self.assertNotIn("pkg/data.txt", one)

    def test_setup_between_tests_is_charged_to_the_module_it_sets_up(self):
        """Discovery order ends three, twelve, two: two's setUpModule runs
        between twelve's last test and two's first, and is two's alone.
        Charging that time to both neighbours made a record depend on which
        module a slice worker ran next (measured: 70 of 461 test modules
        differed between two recordings of one tree)."""
        record, _reason, _children = self.drive()
        self.assertIn("pkg/d.py", record["tests.test_two"])
        # MUST-MISS: the neighbour whose last test ran just before.
        self.assertNotIn("pkg/d.py", record["tests.test_twelve"])
        self.assertNotIn("pkg/d.py", record["tests.test_three"])

    def test_a_teardown_run_at_the_next_modules_first_test_is_its_own(self):
        """unittest tears a module down when the NEXT module's first test
        arrives: the lazy import in eight's tearDownModule is eight's, not
        five's, which runs next."""
        _write(self.root, "pkg/t.py", "T = 3\n")
        _write(self.root, "tests/test_eight.py",
               textwrap.dedent(_PROJECT["tests/test_eight.py"])
               + "def tearDownModule():\n    from pkg import t\n")
        _write(self.root, "tests/test_five.py", """\
            import unittest
            class T(unittest.TestCase):
                def test_five(self):
                    self.assertTrue(True)
            """)
        record, _reason, _children = self.drive()
        self.assertIn("pkg/t.py", record["tests.test_eight"])
        self.assertNotIn("pkg/t.py", record["tests.test_five"])

    def test_a_suite_a_test_runs_itself_keeps_its_fixtures_in_that_test(self):
        """Only the runner's own results switch windows: a test running a
        suite of its own (a census, a fixture runner) charges that suite's
        setUpModule to itself."""
        _write(self.root, "tests/test_four.py", """\
            import io, sys, types, unittest
            class T(unittest.TestCase):
                def test_four(self):
                    inner = types.ModuleType("inner_fixture_module")
                    def setUpModule():
                        from pkg import g
                    inner.setUpModule = setUpModule
                    sys.modules["inner_fixture_module"] = inner
                    class Inner(unittest.TestCase):
                        def test_inner(self):
                            pass
                    Inner.__module__ = "inner_fixture_module"
                    suite = unittest.TestSuite([Inner("test_inner")])
                    got = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                    self.assertTrue(got.wasSuccessful())
            """)
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertIn("pkg/g.py", record["tests.test_four"])
        self.assertNotIn("inner_fixture_module", record)

    def test_a_child_process_is_charged_to_the_test_that_started_it(self):
        record, _reason, children = self.drive()
        self.assertEqual(len(children), 2, children)
        self.assertIn("pkg/e.py", record["tests.test_three"])
        self.assertNotIn("pkg/e.py", record["tests.test_one"])
        self.assertNotIn("pkg/e.py", record["tests.test_two"])

    def test_a_child_is_charged_to_the_test_that_spawned_it_not_the_one_running(self):
        """MEASURED: two recordings of one tree disagreed about 74 of 461
        test modules, because a child read the runner's window when it got
        to importing helm, and a test that did not wait for its child had
        moved on by then. five starts a child and does not wait; the child
        imports only after six has started, and six is not five's
        neighbour (nine and one run between them)."""
        _write(self.root, "pkg/h.py", "H = 8\n")
        _write(self.root, "tests/test_five.py", """\
            import os, subprocess, sys, unittest
            CHILD = (
                "import sys\\n"
                "sys.stdin.read()\\n"
                "import importlib.util\\n"
                "spec = importlib.util.spec_from_file_location('_gl', %r)\\n"
                "gl = importlib.util.module_from_spec(spec)\\n"
                "spec.loader.exec_module(gl)\\n"
                "gl.arm_child()\\n"
                "sys.path.insert(0, %r)\\n"
                "import pkg.h\\n")
            PROC = []
            class T(unittest.TestCase):
                def test_five(self):
                    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    PROC.append(subprocess.Popen(
                        [sys.executable, "-c",
                         CHILD % (os.environ["GATELOADS_UNDER_TEST"], root)],
                        stdin=subprocess.PIPE))
            """)
        _write(self.root, "tests/test_six.py", """\
            import unittest
            from tests.test_five import PROC
            class T(unittest.TestCase):
                def test_six(self):
                    proc = PROC.pop()
                    proc.stdin.close()
                    self.assertEqual(proc.wait(timeout=60), 0)
            """)
        record, _reason, children = self.drive()
        self.assertEqual(len(children), 3, children)
        self.assertIn("pkg/h.py", record["tests.test_five"])
        # MUST-MISS: the test running when the child imported is not the one
        # that caused it (the pre-fix recorder charged six and not five).
        self.assertNotIn("pkg/h.py", record["tests.test_six"])
        self.assertNotIn("pkg/h.py", record["tests.test_one"])

    def test_a_statement_repeated_in_a_later_window_is_charged_again(self):
        """A repeated import statement is skipped only within ONE window: the
        memo of what a window was charged is the window's own, so the second
        module's lazy import of an already-loaded module still counts."""
        _write(self.root, "tests/test_rep_a.py", """\
            import unittest
            class T(unittest.TestCase):
                def test_rep_a(self):
                    for _ in range(3):
                        from pkg import g
                    self.assertEqual(g.G, 7)
            """)
        _write(self.root, "tests/test_rep_b.py", """\
            import unittest
            class T(unittest.TestCase):
                def test_rep_b(self):
                    from pkg import g
                    self.assertEqual(g.G, 7)
            """)
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertIn("pkg/g.py", record["tests.test_rep_a"])
        self.assertIn("pkg/g.py", record["tests.test_rep_b"])
        self.assertNotIn("pkg/g.py", record["tests.test_one"])

    def test_a_memo_reused_by_a_later_module_is_charged_with_what_it_read(self):
        """A process-wide memo reads its file for its FIRST caller only; the
        memo event charges every later caller too, so which module a worker
        runs first no longer decides whose record holds the read."""
        _write(self.root, "pkg/memo.txt", "m\n")
        _write(self.root, "pkg/memo.py", """\
            import os, sys
            _REAL = []
            def census():
                if _REAL:
                    sys.audit(%r, "pkg.memo", "hit")
                else:
                    sys.audit(%r, "pkg.memo", "miss")
                    try:
                        with open(os.path.join(os.path.dirname(__file__),
                                               "memo.txt")) as fh:
                            _REAL.append(fh.read())
                    finally:
                        sys.audit(%r, "pkg.memo", "done")
                return _REAL[0]
            """ % ((gateloads.MEMO_EVENT,) * 3))
        for name in ("memo_a", "memo_b"):
            _write(self.root, "tests/test_%s.py" % name, """\
                import unittest
                class T(unittest.TestCase):
                    def test_%s(self):
                        from pkg import memo
                        self.assertEqual(memo.census(), "m\\n")
                """ % name)
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertIn("pkg/memo.txt", record["tests.test_memo_a"])
        self.assertIn("pkg/memo.txt", record["tests.test_memo_b"])
        self.assertNotIn("pkg/memo.txt", record["tests.test_one"])

    def test_a_process_forked_from_the_runner_is_charged_to_its_test(self):
        """A fork leaves through os._exit, which runs no atexit writer, so
        what it imports is written as it goes. The parent never imports h."""
        _write(self.root, "pkg/h.py", "H = 8\n")
        _write(self.root, "tests/test_fork.py", """\
            import os, unittest
            class T(unittest.TestCase):
                def test_fork(self):
                    pid = os.fork()
                    if pid == 0:
                        try:
                            from pkg import h
                        finally:
                            os._exit(0)
                    _pid, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            """)
        record, reason, _children = self.drive()
        self.assertIsNone(reason)
        self.assertIn("pkg/h.py", record["tests.test_fork"])
        self.assertNotIn("pkg/h.py", record["tests.test_one"])

    def test_a_child_that_lost_the_environment_is_charged_through_the_marker(self):
        """MEASURED miss: a clean-environment `bin/helm doctor` child left
        its test module's record at 9 paths. The marker in the checkout's git
        dir and the child's own ancestry find its runner's window."""
        record, _reason, _children = self.drive()
        self.assertIn("pkg/g.py", record["tests.test_twelve"])
        self.assertNotIn("pkg/g.py", record["tests.test_one"])

    def test_a_replaced_import_hook_breaks_the_record(self):
        _write(self.root, "tests/test_a_tamper.py", _TAMPER)
        record, reason, _children = self.drive()
        self.assertIsNone(record)
        self.assertIn("broken", reason)
        self.assertIn("replaced", reason)


class SerialRunnerArm(_Project):
    """The serial gate's own path: tests/__init__ arms gatetestrecord, which
    arms the load recorder during discovery and writes it after the run."""

    def test_the_serial_timing_runner_writes_a_load_record(self):  # noqa: VACUOUS_ASSERTION — the empty error list and None reason sit beside unconditional positives on the same record (pkg/b.py and pkg/e.py PRESENT)
        _write(self.root, "tests/__init__.py", """\
            import os
            if os.environ.get("HELM_GATE_RECORD_ROLE") == "serial":
                from helm import gatetestrecord
                gatetestrecord.arm_serial_from_env()
            """)
        timing = os.path.join(self.tmp, "timing")
        loads = os.path.join(self.tmp, "loads")
        os.makedirs(timing)
        os.makedirs(loads)
        owner = gatetestrecord.process_identity()
        env = _clean_env(
            GATELOADS_UNDER_TEST=_GATELOADS,
            PYTHONPATH=_TREE + os.pathsep + os.environ.get("PYTHONPATH", ""),
            HELM_GATE_RECORD_DIR=timing,
            HELM_GATE_RECORD_TOKEN="a" * 32,
            HELM_GATE_RECORD_ROLE="serial",
            HELM_GATE_RECORD_ROOT_PID=str(owner["pid"]),
            HELM_GATE_RECORD_ROOT_START=str(owner["start"]),
            HELM_GATE_RECORD_TIMING="1")
        env.update({gateloads.DIR_ENV: loads, gateloads.ROOT_ENV: self.root,
                    gateloads.RUNNER_ENV: "1"})
        proc = subprocess.run([sys.executable] + list(gate.SUITE),
                              cwd=self.root, env=env, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        runners, children, errors = gateloads.read_artifacts(loads)
        self.assertEqual((len(runners), errors), (1, []))
        self.assertEqual(runners[0]["planned"], [
            "tests.test_eight", "tests.test_nine", "tests.test_one",
            "tests.test_three", "tests.test_twelve", "tests.test_two"])
        compact, _stats, reason = gateloads.build_record(
            runners, children, self.root, self.files())
        self.assertIsNone(reason)
        record = gateloads.expand(compact)
        self.assertIn("pkg/b.py", record["tests.test_one"])
        self.assertNotIn("pkg/b.py", record["tests.test_two"])
        self.assertIn("pkg/e.py", record["tests.test_three"])


class MemoEventArms(unittest.TestCase):
    def test_every_real_tree_memo_raises_the_recorders_event(self):
        """The memo owners spell the event out rather than import the
        recorder; a rename on either side would silently stop the charge."""
        from helm import foldckpt, wiring
        self.assertEqual({wiring._MEMO_EVENT, foldckpt._MEMO_EVENT},
                         {gateloads.MEMO_EVENT})


class WindowLogArms(unittest.TestCase):
    """A child's window from its runner's log: the switches within the
    clock's tick of its line's start, narrowed by its pid."""

    def log(self, rows):
        fd, path = tempfile.mkstemp(prefix="helm-test-window-log-")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join("%d %d %d\n" % row for row in rows))
        return path

    def test_the_pid_picks_one_window_among_many_in_one_tick(self):
        path = self.log([(0, 5, -1), (100, 10, 0), (102, 20, 1),
                         (104, 30, 2), (106, 40, 3)])
        self.assertEqual(gateloads._windows_at(path, 25, 103, 10), [1])
        # CONTROL: the same span without a usable pid keeps every window in
        # it, so the narrowing above is the pid's doing.
        self.assertEqual(gateloads._windows_at(path, 3, 103, 10),
                         [0, 1, 2, 3])

    def test_a_falling_watermark_keeps_the_whole_span(self):
        """Pids wrapped inside the span: order says nothing, keep it all."""
        path = self.log([(0, 5, -1), (100, 4194000, 0), (102, 9, 1),
                         (104, 30, 2)])
        self.assertEqual(gateloads._windows_at(path, 12, 103, 10), [0, 1, 2])

    def test_a_switch_outside_the_span_is_not_charged(self):
        path = self.log([(0, 5, 0), (50, 10, 1), (1000, 90, 2)])
        self.assertEqual(gateloads._windows_at(path, 99, 60, 10), [1])


class ForkFileArms(unittest.TestCase):
    def test_a_line_cut_short_by_a_kill_is_skipped_and_the_rest_kept(self):  # noqa: VACUOUS_ASSERTION — the equality pins the NON-empty loads and reads read from the same file; the cut line's absence from them is the point
        fd, path = tempfile.mkstemp(prefix="helm-test-fork-", suffix=".jsonl")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": gateloads.VERSION, "type": "fork",
                                 "window_file": "/w/window-7",
                                 "windows": [3]}) + "\n")
            fh.write('["l", "pkg/a.py"]\n["r", "pkg/data.txt"]\n["l", "pkg/b')
        row = gateloads._read_fork(path)
        self.assertEqual((row["windows"], row["loads"], row["reads"]),
                         ([3], ["pkg/a.py"], ["pkg/data.txt"]))

    def test_a_file_without_its_header_is_unreadable(self):
        fd, path = tempfile.mkstemp(prefix="helm-test-fork-", suffix=".jsonl")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write('["l", "pkg/a.py"]\n')
        self.assertIsNone(gateloads._read_fork(path))
        # CONTROL: the same line under a header reads.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": gateloads.VERSION, "type": "fork",
                                 "window_file": "/w/window-7",
                                 "windows": [0]}) + '\n["l", "pkg/a.py"]\n')
        self.assertEqual(gateloads._read_fork(path)["loads"], ["pkg/a.py"])


class ForkArtifactArms(unittest.TestCase):
    def test_a_fork_killed_before_its_header_costs_one_child_not_the_run(self):
        directory = tempfile.mkdtemp(prefix="helm-test-fork-dir-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        gateloads.write_json(directory, "runner-1.json", {
            "v": gateloads.VERSION, "type": "runner", "window_file": None,
            "windows": [], "bodies": {}, "broken": None,
            "loads": {"tests.test_m": ["a.py"]}})
        open(os.path.join(directory, "child-9-1.jsonl"), "w").close()
        runners, children, errors = gateloads.read_artifacts(directory)
        self.assertEqual((len(runners), len(children), errors), (1, 1, []))
        compact, stats, reason = gateloads.build_record(
            runners, children, "/nowhere", [])
        self.assertIsNone(reason)
        self.assertEqual(stats["children_unattributed"], 1)
        self.assertEqual(gateloads.expand(compact)["tests.test_m"],
                         ["a.py", "tests/test_m.py"])


class RootsArms(unittest.TestCase):
    def test_an_undecodable_name_is_no_recorded_path(self):
        roots = gateloads.Roots("/repo")
        self.assertEqual(roots.rel("/repo/helm/x.py"), "helm/x.py")
        self.assertEqual(roots.rel("/repo/helm/__pycache__/x.cpython-312.pyc"),
                         "helm/x.py")
        self.assertEqual(roots.rel("/repo/docs", directory=True), "docs/")
        self.assertIsNone(roots.rel(os.fsdecode(b"/repo/bad-\xff.txt")))
        self.assertIsNone(roots.rel("/repo/.git/HEAD"))
        self.assertIsNone(roots.rel("/elsewhere/x.py"))


class StaticClosureArms(_Project):
    def test_module_scope_imports_are_edges_and_function_bodies_are_not(self):
        edges, err = gateloads.static_edges(self.root, self.files())
        self.assertIsNone(err)
        self.assertEqual(edges["pkg/a.py"], {"pkg/__init__.py", "pkg/c.py"})
        self.assertIn("pkg/__init__.py", edges["pkg/b.py"])

    def test_a_named_sys_modules_read_is_an_edge(self):
        _write(self.root, "pkg/f.py", """\
            import sys
            def peer():
                return sys.modules.get(__package__ + ".e")
            """)
        edges, err = gateloads.static_edges(self.root, self.files())
        self.assertIsNone(err)
        self.assertIn("pkg/e.py", edges["pkg/f.py"])
        self.assertNotIn("pkg/d.py", edges["pkg/f.py"])


class CompactRecordArms(unittest.TestCase):
    def test_roots_shrink_to_what_nothing_else_reaches_and_expand_back(self):
        """A cycle (a <-> b) and a tail (b -> c): the three roots shrink to
        one, and the expansion is the same closed set."""
        edges = {"a.py": {"b.py"}, "b.py": {"a.py", "c.py"}}
        runner = {"v": 1, "type": "runner", "window_file": None,
                  "windows": [], "bodies": edges, "broken": None,
                  "loads": {"tests.test_m": ["a.py", "b.py", "c.py"],
                            "tests.test_n": ["c.py"]}}
        compact, stats, reason = gateloads.build_record(
            [runner], [], "/nowhere", [])
        self.assertIsNone(reason)
        self.assertEqual(len([p for p in compact["roots"]["tests.test_m"]
                              if p.endswith(".py") and "/" not in p]), 1)
        record = gateloads.expand(compact)
        self.assertEqual(record["tests.test_m"],
                         ["a.py", "b.py", "c.py", "tests/test_m.py"])
        # MUST-MISS: n reached only the tail.
        self.assertEqual(record["tests.test_n"], ["c.py", "tests/test_n.py"])
        self.assertEqual(gateloads.expand(gateloads.decode(
            gateloads.encode(compact))), record)


    def test_slice_workers_are_unioned_by_module(self):
        """One record from every worker: a module whose windows ran in two
        workers keeps both, and a module one worker ran is in it too."""
        def runner(loads):
            return {"v": 1, "type": "runner", "window_file": None,
                    "windows": [], "bodies": {}, "broken": None,
                    "loads": loads}
        compact, _stats, reason = gateloads.build_record(
            [runner({"tests.test_m": ["a.py"]}),
             runner({"tests.test_m": ["b.py"], "tests.test_n": ["c.py"]})],
            [], "/nowhere", [])
        self.assertIsNone(reason)
        record = gateloads.expand(compact)
        self.assertEqual(record["tests.test_m"],
                         ["a.py", "b.py", "tests/test_m.py"])
        self.assertEqual(record["tests.test_n"], ["c.py", "tests/test_n.py"])


class RecordEncodingArms(unittest.TestCase):
    RECORD = {"tests.test_x": ["helm/a.py", "tests/test_x.py"],
              "tests.test_y": ["helm/a.py", "helm/b.py", "tests/test_y.py"]}

    def event(self):
        return gateloads.event("0123456789abcdef", "1" * 40, "2" * 40,
                               "serial", self.RECORD, 1.25)

    def test_a_record_round_trips_through_its_event(self):  # noqa: VACUOUS_ASSERTION — the None error is controlled by the next arm, where an edited payload names its content id and its digest
        row = self.event()
        record, err = gateloads.read_event(row)
        self.assertIsNone(err)
        self.assertEqual(record, self.RECORD)
        self.assertEqual((row["tests"], row["paths"]), (2, 4))

    def test_an_edited_payload_no_longer_resolves(self):
        row = self.event()
        row["payload"] = gateloads.encode({"tests.test_x": ["helm/a.py"]})
        self.assertIn("content id", gateloads.event_error(row))
        row["id"] = gateloads.event_id(row)
        _record, err = gateloads.read_event(row)
        self.assertIn("digest", err)


class SelectionArms(unittest.TestCase):
    RECORD = {"tests.test_a": ["helm/a.py", "tests/test_a.py"],
              "tests.test_b": ["helm/b.py", "tests/test_b.py", "docs/"],
              "tests.test_audit": ["tests/test_audit.py"]}
    UNIVERSE = {"tests.test_a", "tests.test_b", "tests.test_audit",
                "tests.test_new"}

    def select(self, changed, structural=(), texts=None):
        texts = texts or {}
        return gateloads.select(
            self.RECORD, self.UNIVERSE, changed, structural,
            lambda m: texts.get(m, ""), ["tests.test_audit"])

    def test_a_changed_file_selects_the_tests_that_reached_it(self):
        self.assertEqual(self.select({"helm/a.py"}),
                         ["tests.test_a", "tests.test_audit",
                          "tests.test_new"])

    def test_an_unrecorded_module_and_the_audits_always_ride(self):
        self.assertEqual(self.select({"helm/zzz.py"}),
                         ["tests.test_audit", "tests.test_new"])

    def test_a_test_naming_a_changed_path_rides(self):
        got = self.select({"docs/x.md"},
                          texts={"tests.test_a": "open('x.md')"})
        self.assertIn("tests.test_a", got)
        self.assertNotIn("tests.test_b", got)

    def test_a_listed_directory_sees_an_added_file_not_an_edited_one(self):
        self.assertIn("tests.test_b", self.select({"docs/new.md"},
                                                  {"docs/new.md"}))
        self.assertNotIn("tests.test_b", self.select({"docs/old.md"}))

    def test_a_changed_test_file_selects_itself(self):
        self.assertIn("tests.test_a", self.select({"tests/test_a.py"}))
        self.assertNotIn("tests.test_b", self.select({"tests/test_a.py"}))


class RecordedPlanBase(FocusBase):
    """FocusBase's project, with the tree-wide audit list and its modules and
    a hub whose function-local import of alpha no test calls, recorded on
    main (T0)."""

    #: The running helm's list, grown by an audit no fixture tree ships: the
    #: helm a lane branched before that audit joined is gated by.
    GROWN = gateaudits.AUDITS + ("test_joined_after_this_base",)

    def setUp(self):
        super().setUp()
        self._git("checkout", "-q", "main")
        # THE TREE SHIPS ITS OWN LIST (`gate._tree_audits`), equal to the
        # running helm's until an arm makes the two differ.
        with open(gateaudits.__file__, encoding="utf-8") as fh:
            self._write("helm/gateaudits.py", fh.read())
        for name in gateaudits.AUDITS:
            self._write("tests/%s.py" % name, GAMMA_TEST)
        self._write("pkg/hub.py", "def later():\n"
                                  "    from . import alpha\n"
                                  "    return alpha.X\n")
        self._write("tests/test_delta.py",
                    "import unittest\nfrom pkg import hub\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_d(self):\n"
                    "        self.assertTrue(hub.later)\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "audits and a hub")
        self.t0_head = self._git("rev-parse", "HEAD")
        self.t0 = self._git("rev-parse", "HEAD^{tree}")
        self._git("checkout", "-q", "lane/focus")
        self._git("rebase", "-q", "main")
        self.head = self._git("rev-parse", "HEAD")

    def record(self):
        common = ["pkg/__init__.py", "tests/__init__.py"]
        record = {
            "tests.test_alpha": common + ["pkg/alpha.py",
                                          "tests/test_alpha.py"],
            "tests.test_beta": common + ["pkg/alpha.py", "pkg/beta.py",
                                         "tests/test_beta.py"],
            "tests.test_gamma": common + ["tests/test_gamma.py"],
            "tests.test_delta": common + ["pkg/hub.py",
                                          "tests/test_delta.py"],
        }
        for name in gateaudits.AUDITS:
            record["tests." + name] = common + ["tests/%s.py" % name]
        return record

    def plant(self, status="OK"):
        """A whole-suite receipt of T0 and its load record, as a recorded
        run would leave them."""
        ident = gate.interpreter()
        receipt = {
            "v": 4, "event": "gate", "ts": "2026-09-24T00:00:00Z",
            "repo_id": self.repo, "head": self.t0_head, "tree": self.t0,
            "dirty": False, "head_after": self.t0_head, "tree_after": self.t0,
            "dirty_after": False, "interpreter": ident, "host": gate.host(),
            "argv": [ident["executable"]] + list(gateauthority.SERIAL_ARGV),
            "suite": True, "label": None, "rc": 0 if status == "OK" else 1,
            "wall": 9.0, "status": status, "ran": 40, "skipped": 0,
            "detail": "", "elapsed": 8.0, "failures": [],
            "failures_unreadable": False, "base_check": None}
        receipt["id"] = gate._receipt_id(receipt)
        loads = gateloads.event(receipt["id"], self.t0_head, self.t0,
                                "serial", self.record(), 2.0)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(loads) + "\n" + json.dumps(receipt) + "\n")
        return receipt, loads

    def _dispatch(self):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        from tests._tmphome import review_task
        d, err = dispatches.add("reviewer", "a-lane", self.head,
                                kind="review", repo=self.repo, notify=False,
                                _reason=True, new_work=True,
                                task=review_task(self))
        self.assertIsNone(err, err)
        return d


class TreeAuditListArms(unittest.TestCase):
    """The audit list a selection runs is read from the tree it selects in
    (`gate._tree_audits`), parsed and never executed."""

    def test_the_reader_reads_this_trees_own_list(self):  # noqa: VACUOUS_ASSERTION — the equality is against the live list, which the gateaudits arms hold non-empty and in step with its doc
        with open(gateaudits.__file__, encoding="utf-8") as fh:
            self.assertEqual(gate._tree_audits(fh.read()),
                             ["tests." + name for name in gateaudits.AUDITS])

    def test_a_list_it_cannot_read_is_no_list(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal six-source tuple and the arm above pins a readable one
        for text in (None, "", "AUDITS = build()\n", "AUDITS = A + B\n",
                     "AUDITS = ('test_a', 3)\n", "AUDITS = (\n"):
            self.assertIsNone(gate._tree_audits(text), text)


class RecordedPlanArms(RecordedPlanBase):
    def test_a_lane_branched_before_an_audit_joined_still_plans_by_record(self):
        """THE LIST IS THE TREE'S: a running helm whose list grew an audit
        after this lane's base asks nothing of a tree that neither lists nor
        ships it (measured: every lane branched before `test_raw_module_reads`
        joined refused `--focus` once one load record existed)."""
        _receipt, loads = self.plant()
        with mock.patch.object(gateaudits, "AUDITS", self.GROWN):
            plan, err, note = gate.focus_plan_explained(self.repo)
        self.assertIsNone(err, err)
        self.assertIsNone(note)
        self.assertEqual((plan["policy"], plan["record"]),
                         (gate.FOCUS_POLICY_V3, loads["id"]))
        self.assertIn("tests." + gateaudits.AUDITS[-1], plan["selected"])
        self.assertNotIn("tests.test_joined_after_this_base",
                         plan["selected"])

    def test_a_doc_change_on_that_lane_runs_the_audits_its_tree_lists(self):  # noqa: VACUOUS_ASSERTION — the loop is over the live, non-empty audit list, and the None error sits beside it
        """The static closure reads the same list: a change outside the
        import graph selects the tree's own audits, never a refusal for one
        the tree never shipped."""
        self._write("docs/NOTE.md", "a note\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "a doc")
        with mock.patch.object(gateaudits, "AUDITS", self.GROWN):
            plan, err = gate.focus_plan(self.repo, policy=gate.FOCUS_POLICY)
        self.assertIsNone(err, err)
        for name in gateaudits.AUDITS:
            self.assertIn("tests." + name, plan["selected"])
        self.assertNotIn("tests.test_joined_after_this_base",
                         plan["selected"])

    def test_a_tree_missing_a_listed_audit_falls_back_to_the_static_closure(self):
        """A tree that lists an audit it does not ship cannot run the floor
        every recorded selection runs, and the static closure still serves a
        change inside the import graph: the plan falls back and says why, as
        it does for any record that cannot serve, and is never refused where
        the static closure is not."""
        self.plant()
        self._git("rm", "-q", "tests/%s.py" % gateaudits.AUDITS[0])
        self._git("commit", "-qm", "drop one audit's module")
        plan, err, note = gate.focus_plan_explained(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY)
        self.assertIn("tests." + gateaudits.AUDITS[0], note)

    def test_the_plan_selects_by_record_and_names_it(self):
        _receipt, loads = self.plant()
        plan, err, note = gate.focus_plan_explained(self.repo)
        self.assertIsNone(err, err)
        self.assertIsNone(note)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY_V3)
        self.assertEqual((plan["record"], plan["t0"]), (loads["id"], self.t0))
        self.assertEqual(plan["changed"], ["pkg/alpha.py"])
        for module in ("tests.test_alpha", "tests.test_beta"):
            self.assertIn(module, plan["selected"])
        for module in gateaudits.AUDITS:
            self.assertIn("tests." + module, plan["selected"])
        # MUST-MISS: the hub's lazy edge to alpha is never taken by delta's
        # run, and gamma touches nothing.
        self.assertNotIn("tests.test_delta", plan["selected"])
        self.assertNotIn("tests.test_gamma", plan["selected"])

    def test_the_static_closure_takes_the_lazy_edge_the_record_does_not(self):
        """CONTROL on the must-miss above: the same tree under the static
        policy selects delta, so the record is what excluded it."""
        self.plant()
        plan, err = gate.focus_plan(self.repo, policy=gate.FOCUS_POLICY)
        self.assertIsNone(err, err)
        self.assertIn("tests.test_delta", plan["selected"])

    def test_no_record_falls_back_to_the_static_closure_and_says_why(self):
        plan, err, note = gate.focus_plan_explained(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY)
        self.assertIn("no load record", note)

    def test_a_record_of_a_red_run_is_not_used(self):
        self.plant(status="FAILED")
        plan, _err, note = gate.focus_plan_explained(self.repo)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY)
        self.assertIn("green whole-suite receipt", note)

    def test_the_plan_verb_names_the_record_and_t0(self):
        _receipt, loads = self.plant()
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gate.cmd_gate(["run", "--repo", self.repo, "--focus",
                                "--plan"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("focus policy %s" % gate.FOCUS_POLICY_V3,
                      out.getvalue())
        self.assertIn("record    %s  T0 %s" % (loads["id"], self.t0),
                      out.getvalue())


class RecordedBindArms(RecordedPlanBase):
    def setUp(self):
        super().setUp()
        self.receipt, self.loads = self.plant()

    def bind(self, row):
        with mock.patch.object(gate, "receipts",
                               return_value=([row], None, 0)):
            return gate.bind(gate.evidence_line(row), self.head,
                             repo_id=self.repo, need=gate.NEED_FOCUSED)

    def test_a_recorded_focused_receipt_binds_a_cure_round(self):
        row = self.mint_focused()
        self.assertEqual(row["focus"]["policy"], gate.FOCUS_POLICY_V3)
        state, rid, why = gate.bind(gate.evidence_line(row), self.head,
                                    repo_id=self.repo, need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)
        self.assertIn("load record %s" % self.loads["id"], why)

    def test_a_tampered_selection_is_refused(self):
        """THE PLANT: a selection one module short, re-hashed so it
        resolves, must not bind."""
        row = self.mint_focused()
        short = [m for m in row["focus"]["selected"]
                 if m != "tests.test_beta"]
        crafted = self.reresolve(row, focus=dict(row["focus"],
                                                 selected=short))
        state, _rid, why = self.bind(crafted)
        self.assertEqual(state, "REFUSED")
        self.assertIn("never ran: tests.test_beta", why)

    def test_a_tampered_tree_or_diff_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal two-plant tuple, so every REFUSED and its needle is asserted every run
        """A record id edited to name nothing is a record this host does not
        hold, and answers UNVERIFIED (RecordedDoorArms)."""
        row = self.mint_focused()
        for edit, needle in (({"t0": "3" * 40}, "T0"),
                             ({"changed": []}, "changed-set differs")):
            crafted = self.reresolve(row, focus=dict(row["focus"], **edit))
            state, _rid, why = self.bind(crafted)
            self.assertEqual(state, "REFUSED", edit)
            self.assertIn(needle, why)

    def test_the_approve_and_land_doors_refuse_a_recorded_focused_receipt(self):
        row = self.mint_focused()
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("FOCUSED", why)
        state, why = landgate.gate_binds_tree(row["id"], None,
                                              repo=self.repo, tip=self.head)
        self.assertEqual(state, landgate.REFUSE, why)

    def test_a_fix_verdict_binds_a_recorded_focused_receipt(self):  # noqa: VACUOUS_ASSERTION — the None error sits beside an unconditional positive: the verdict's gate state reads VERIFIED with the receipt id
        row = self.mint_focused()
        d = self._dispatch()
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row) + " 1 blocker",
            "fix")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.gate_state(got), "VERIFIED " + row["id"])

    def test_an_approve_verdict_refuses_a_recorded_focused_receipt(self):
        row = self.mint_focused()
        d = self._dispatch()
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("FOCUSED", err)
        self.assertFalse(any(e.get("event") == "verdict"
                             for e in dispatches.history(d["id"])))


class RecordedDoorArms(RecordedPlanBase):
    """THE SAFETY DOOR, asked of the recorded kind at every door that reads a
    receipt as land authority — bind at NEED_LAND, foldcheck's tree-vs-gate
    rung with land=True, landgate clause (ii); the two compose doors ask the
    first and the third in sequence — and at the APPROVE door (bind at
    NEED_SUITE). A recorded focused receipt binds a cure round and nothing
    stronger, whatever its kind fields say; a whole-suite receipt that keeps
    a load record beside it is not demoted by the sibling.

    THE FOCUSED ROW IS ASSEMBLED, NOT MINTED (CounterfeitArms._assemble's
    shape, for the recorded plan): a real mint needs the guard's cgroup
    supervisor, which `fab test` does not grant, and an assembled row is the
    stronger probe here — the doors must refuse the KIND, not a run that
    went wrong. The cure-round bind is the positive control: the same row
    VERIFIES there, so a refusal above it is about the door."""

    RUNNER = {"v": 1, "type": "runner", "pid": 1, "window_file": None,
              "windows": [], "loads": {"tests.test_beta": ["pkg/beta.py"]},
              "bodies": {}, "planned": ["tests.test_beta"], "broken": None,
              "calls": 1}

    def setUp(self):
        super().setUp()
        self.receipt, self.loads = self.plant()

    def assemble(self, **focus_edits):
        """A no-run v6 carrying the recorded plan this tree derives, its RAN
        set written to match, a self-computed id so the row resolves, and
        appended straight into the ledger past every verb."""
        plan, err = gate.focus_plan(self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(plan["policy"], gate.FOCUS_POLICY_V3)
        plan["executed"] = list(plan["selected"])
        plan["executed_ids"] = 2
        plan.update(focus_edits)
        tree = self._git("rev-parse", "HEAD^{tree}")
        row = {"v": 6, "event": "gate", "ts": "2026-09-25T00:00:00Z",
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
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def doors(self, row):
        """{door: (state, why)} across every door that reads a receipt as
        land or approve authority, each resolving the row through the
        store as production does."""
        line = gate.evidence_line(row)
        out = {}
        for need in (gate.NEED_LAND, gate.NEED_SUITE):
            state, _rid, why = gate.bind(line, self.head, repo_id=self.repo,
                                         need=need)
            out["bind " + need] = (state, why)
        rung = foldcheck._tree_matches_gate(
            vcs.backend(self.repo), self.repo, self.head,
            "gate:" + row["id"], land=True)
        out["foldcheck tree-vs-gate"] = (rung.verdict, rung.discriminator)
        state, why = landgate.gate_binds_tree(row["id"], None, repo=self.repo,
                                              tip=self.head)
        out["landgate (ii)"] = (state, why)
        return out

    REFUSALS = {"bind land": "REFUSED", "bind suite": "REFUSED",
                "foldcheck tree-vs-gate": foldcheck.REFUSE,
                "landgate (ii)": landgate.REFUSE}

    def assert_every_door_refuses(self, row, label):
        for door, (state, why) in self.doors(row).items():
            self.assertEqual(state, self.REFUSALS[door], (label, door, why))

    def test_a_recorded_focused_receipt_binds_the_cure_round_and_no_door_above_it(self):
        row = self.assemble()
        state, rid, why = gate.bind(gate.evidence_line(row), self.head,
                                    repo_id=self.repo,
                                    need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)
        self.assertIn("load record %s" % self.loads["id"], why)
        self.assert_every_door_refuses(row, "recorded focused")

    def test_a_recorded_receipt_binds_under_a_helm_whose_audit_list_grew(self):
        """The binder re-derives with the TREE's list as the planner does,
        so a helm that grew an audit between the mint and the verdict
        re-derives the selection the mint made."""
        row = self.assemble()
        with mock.patch.object(gateaudits, "AUDITS", self.GROWN):
            state, rid, why = gate.bind(gate.evidence_line(row), self.head,
                                        repo_id=self.repo,
                                        need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]), why)

    def test_a_record_this_host_does_not_hold_is_unverified_not_refused(self):  # noqa: VACUOUS_ASSERTION — each None rid sits beside an UNVERIFIED state and the record or tree named in its reason, and the None verdict error beside its UNVERIFIED gate state
        """A routed mint selects from ITS box's ledger, and this host may
        never have imported that record, or holds it about a tree this
        repository lacks. Nothing here can re-derive the scope and nothing
        here proves it false, so the answer is UNVERIFIED bound to no
        receipt: a FIX verdict records what a tokenless one records, and
        every door above the cure round still refuses the row."""
        row = self.assemble()
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        with open(path, "w", encoding="utf-8") as fh:
            for kept in rows:
                if kept.get("event") != gateloads.EVENT:
                    fh.write(json.dumps(kept) + "\n")
        line = gate.evidence_line(row)
        state, rid, why = gate.bind(line, self.head, repo_id=self.repo,
                                    need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("UNVERIFIED", None), why)
        self.assertIn(self.loads["id"], why)
        d = self._dispatch()
        got, err = dispatches.mark_verdict(d["id"], self.head,
                                           line + " 1 blocker", "fix")
        self.assertIsNone(err, err)
        self.assertEqual(dispatches.gate_state(got), "UNVERIFIED")
        self.assert_every_door_refuses(row, "record not here")
        # THE RECORD HERE, ITS TREE NOT: a green whole suite of a tree this
        # repository never held vouches for a record this host imported.
        away = {"head": "5" * 40, "tree": "4" * 40}
        receipt = dict(self.receipt, head_after=away["head"],
                       tree_after=away["tree"], **away)
        receipt["id"] = gate._receipt_id(receipt)
        loads = gateloads.event(receipt["id"], away["head"], away["tree"],
                                "serial", self.record(), 2.0)
        crafted = self.reresolve(row, focus=dict(
            row["focus"], record=loads["id"], t0=away["tree"]))
        with open(path, "a", encoding="utf-8") as fh:
            for planted in (loads, receipt, crafted):
                fh.write(json.dumps(planted) + "\n")
        state, rid, why = gate.bind(gate.evidence_line(crafted), self.head,
                                    repo_id=self.repo,
                                    need=gate.NEED_FOCUSED)
        self.assertEqual((state, rid), ("UNVERIFIED", None), why)
        self.assertIn(away["tree"][:12], why)

    def test_a_recorded_focused_receipt_wearing_another_kind_still_refuses(self):  # noqa: VACUOUS_ASSERTION — every refusal carries its needle; the absences are the refusals themselves
        """THE KIND FIELDS, tampered after the mint, re-hashed so the row
        resolves, and appended to the ledger past every verb, with the
        provenance flip ACTIVE as the fleet's is: the suite flag gone or
        the policy renamed reaches no door at all, and the renamed policy
        cannot even bind the cure round it came from. The suite flag
        FLIPPED reaches no door either: bind refuses the kind itself
        (`gate.row_refusal`: a v6 row's flag reads false), at the approve
        door and the cure round alike, and the two land doors that ask no
        row predicate refuse it for want of an authenticated placement.

        THE RESIDUE THIS ARM ONCE ASSERTED OPEN is closed: a v6 row wearing
        `suite: true` bound at NEED_SUITE, because no reader asked whether
        the version and the flag agree."""
        _record, err = gateimport.activate(ts="2000-01-01T00:00:00Z")
        self.assertIsNone(err, err)
        row = self.assemble()
        flagless = json.loads(json.dumps(row))
        del flagless["suite"]
        flagless["id"] = gate._receipt_id(flagless)
        renamed = self.reresolve(row, focus=dict(
            row["focus"], policy="changed+recorded-loads-v9"))
        flipped = self.reresolve(row, suite=True)
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            for crafted in (flagless, renamed, flipped):
                fh.write(json.dumps(crafted) + "\n")
        self.assert_every_door_refuses(flagless, "suite flag missing")
        self.assert_every_door_refuses(renamed, "policy renamed")
        state, _rid, why = gate.bind(gate.evidence_line(renamed), self.head,
                                     repo_id=self.repo,
                                     need=gate.NEED_FOCUSED)
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("changed+recorded-loads-v9", why)
        answers = self.doors(flipped)
        answers["bind " + gate.NEED_FOCUSED] = gate.bind(
            gate.evidence_line(flipped), self.head, repo_id=self.repo,
            need=gate.NEED_FOCUSED)[::2]
        for door in ("bind land", "bind suite", "bind " + gate.NEED_FOCUSED):
            state, why = answers[door]
            self.assertEqual(state, "REFUSED", (door, why))
            self.assertIn("the focused kind (v6) and records suite True", why)
        for door in ("foldcheck tree-vs-gate", "landgate (ii)"):
            state, why = answers[door]
            self.assertEqual(state, self.REFUSALS[door], (door, why))
            self.assertIn("no authenticated door placed it", why)

    def mint_recorded_suite(self):
        """A whole-suite receipt minted through gate.run whose runner left a
        loads artifact, so the mint keeps a load record beside it."""
        def child(repo, cmd, position, timeout, identity=None, env=None):
            gateloads.write_json(env[gateloads.DIR_ENV], "runner-1.json",
                                 self.RUNNER)
            return "", "Ran 3 tests in 0.0s\n\nOK\n", 0, None
        with mock.patch.object(gate, "SUITE", gateauthority.SERIAL_ARGV), \
                mock.patch.object(gate, "_queued_process", side_effect=child):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        return row

    def test_a_whole_suite_receipt_beside_its_load_record_is_not_demoted(self):  # noqa: VACUOUS_ASSERTION — the same row is asserted VERIFIED/PASS/OK at every door and PRESENT in receipts() beside the sibling's absence
        row = self.mint_recorded_suite()
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        siblings = [r for r in rows if r.get("event") == gateloads.EVENT
                    and r.get("receipt") == row["id"]]
        self.assertEqual(len(siblings), 1)
        admitted = {"bind land": "VERIFIED", "bind suite": "VERIFIED",
                    "foldcheck tree-vs-gate": foldcheck.PASS,
                    "landgate (ii)": landgate.OK}
        for door, (state, why) in self.doors(row).items():
            self.assertEqual(state, admitted[door], (door, why))
        # The sibling is no receipt to any reader: not listed, not resolved.
        ids = [r["id"] for r in gate.receipts()[0]]
        self.assertIn(row["id"], ids)
        self.assertNotIn(siblings[0]["id"], ids)
        found, err = gate.by_id(siblings[0]["id"][:16])
        self.assertIsNone(found)
        self.assertIsNotNone(err)


class MintArms(FocusBase):
    """The gate keeps a record from a whole-suite run's runner artifacts, as
    an advisory sibling of the receipt, and only for a clean tree."""

    RUNNER = {"v": 1, "type": "runner", "pid": 1, "window_file": None,
              "windows": [], "loads": {"tests.test_beta": ["pkg/beta.py"]},
              "bodies": {}, "planned": ["tests.test_beta"], "broken": None,
              "calls": 1}

    def run_suite(self):
        seen = {}

        def child(repo, cmd, position, timeout, identity=None, env=None):
            seen.update(env)
            marker = os.path.join(repo, ".git", gateloads.MARKER)
            with open(marker, encoding="utf-8") as fh:
                seen["marker"] = json.load(fh)["dir"]
            gateloads.write_json(env[gateloads.DIR_ENV], "runner-1.json",
                                 self.RUNNER)
            return "", "Ran 3 tests in 0.0s\n\nOK\n", 0, None
        with mock.patch.object(gate, "SUITE", gateauthority.SERIAL_ARGV), \
                mock.patch.object(gate, "_queued_process", side_effect=child):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        return row, seen

    def loads_rows(self):
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if r.get("event") == gateloads.EVENT]

    def test_a_recorded_whole_suite_keeps_its_load_record(self):
        row, env = self.run_suite()
        self.assertEqual(env[gateloads.RUNNER_ENV], "1")
        # The marker named this run's directory while it ran, and is gone.
        self.assertEqual(env["marker"], env[gateloads.DIR_ENV])
        self.assertFalse(os.path.exists(
            os.path.join(self.repo, ".git", gateloads.MARKER)))
        self.assertEqual(env[gateloads.ROOT_ENV], self.repo)
        loads = self.loads_rows()
        self.assertEqual(len(loads), 1)
        self.assertEqual((loads[0]["receipt"], loads[0]["tree"]),
                         (row["id"], row["tree"]))
        record, err = gateloads.read_event(loads[0])
        self.assertIsNone(err)
        # The recorded file, closed under beta's module-scope import.
        self.assertIn("pkg/alpha.py", record["tests.test_beta"])
        self.assertIn("tests/test_beta.py", record["tests.test_beta"])
        # The receipt readers skip it like the other siblings.
        rows, _unavailable, skipped = gate.receipts()
        self.assertEqual(([r["id"] for r in rows], skipped), ([row["id"]], 0))

    def test_a_record_that_cannot_be_written_never_costs_the_receipt(self):  # noqa: VACUOUS_ASSERTION — the receipt is asserted PRESENT and OK; the absent record is controlled by the arm above, which keeps one
        """MEASURED on a build node: an unencodable recorded path raised
        inside the mint and the whole suite's receipt was lost. The record
        is advisory; the receipt stands without it."""
        boom = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "planted")
        with mock.patch.object(gateloads, "event", side_effect=boom):
            row, _env = self.run_suite()
        self.assertEqual(row["status"], "OK")
        self.assertEqual([r["id"] for r in gate.receipts()[0]], [row["id"]])
        self.assertEqual(self.loads_rows(), [])

    def test_a_dirty_run_keeps_no_record(self):  # noqa: VACUOUS_ASSERTION — the arm above runs the same seam on the clean tree and asserts exactly one record PRESENT
        self._write("pkg/alpha.py", "X = 1\nZ = 3\n")
        self.run_suite()
        self.assertEqual(self.loads_rows(), [])


if __name__ == "__main__":
    unittest.main()
