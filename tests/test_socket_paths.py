#!/usr/bin/env python3
"""An AF_UNIX socket a test binds must not get longer when TMPDIR does.

THE MEASURED DEFECT (task/2539). A Linux AF_UNIX address is `sun_path`: 108
bytes, including the terminating NUL. The socket fixtures in this suite made
their socket paths under the test process's temp directory, and that directory
is a chain: the ambient TMPDIR, then `helm-gate-scratch-<8>` from the gate
(27 bytes), then `helm-suite-env-<8>/tmp` from tests/__init__.py (28 bytes),
then the fixture's own directories and the socket name (45 bytes or more for
the codexhomes fake orca). Measured on a build node:

    TMPDIR=/tmp, no gate               longest path  80 bytes
    TMPDIR=/tmp, real gate nesting     longest path 107 bytes (1 byte of room)
    TMPDIR=<38-byte fab job root>      114 to 141 bytes, 34 tests error with
                                       "OSError: AF_UNIX path too long"

So any node whose TMPDIR is longer than `/tmp` (/tmp/user/1000, systemd
PrivateTmp, a fab job root) broke the gate for a reason no test was about.

THE CURE THESE ARMS HOLD: every test-fixture socket directory comes from ONE
door, `tests.socket_dir()`, which returns a directory under a short fixed base
whose length does not depend on TMPDIR.

HOW THE ARMS MEASURE. A parent cannot move its own TMPDIR after
tests/__init__.py has routed it, so each arm spawns a child that imports the
`tests` package under the TMPDIR the arm chose (a REAL directory), spies the
real `socket.socket.bind`/`connect`/`connect_ex` calls (the spy records the
address and then calls the original), and runs representative socket tests
from the modules that bind or connect. The child writes what it saw. The
parent asserts on that report and on the filesystem after the child exits.

THE FORKSERVER BAND. multiprocessing's forkserver binds its listener socket
under the process's tempdir, so a Pool is a socket fixture too. CPython moves
that socket to /tmp only when the tempdir is long, and its length estimate is
four bytes short, so a narrow band of TMPDIR lengths breaks it (see
FORKSERVER_BAND_TMPDIR_BYTES). The band arms run the same representative set,
test_board's Pool among them, in a child whose ambient TMPDIR is inside the
band and whose default start method is forkserver.

THE REFUSAL IN EVERY SUITE PROCESS. The child arms prove the listed sites at
chosen TMPDIR lengths; they cannot see a fixture nobody listed, and the static
scan behind test_every_socket_fixture_file_is_represented sees only the
spellings in UNIX_SOCKET_NAMES. The in-process arms of TheSocketPathHookTest
hold the guard that does not depend on either: tests/__init__.py refuses an
AF_UNIX address that is over the budget or lies under this process's suite
root, at whatever TMPDIR the suite runs under.
"""
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)

# THE DOCUMENTED BUDGET, defined once at the door in tests/__init__.py.
# sun_path is 108 bytes and one of them is the NUL, so 107 is the kernel's
# hard limit. The budget keeps a margin of 16 bytes below that, so a fixture
# that grows a longer socket name, or a base that moves, does not reach the
# limit before a guard reports it.
SUN_PATH_BYTES = tests.SUN_PATH_BYTES
SOCKET_PATH_MARGIN = tests.SOCKET_PATH_MARGIN
SOCKET_PATH_BUDGET = tests.SOCKET_PATH_BUDGET

# A path this long is over the budget and still inside sun_path, so only a
# guard that enforces the budget refuses it; the kernel would accept it.
OVER_BUDGET_BYTES = 100

# THE FORKSERVER BAND (task/2539 round 1, measured through fab on CPython
# 3.14.6, the gate's interpreter). The forkserver's listener is
# `<tempdir>/pymp-XXXXXXXX/sock-NNNNNNNNNNNN`. multiprocessing.util
# _get_base_temp_dir keeps the tempdir while `len(tempdir) + 14 + 14 < 108`
# and falls back to /tmp otherwise, but the measured suffix is 32 bytes, so a
# tempdir of 76 to 79 bytes gives a 108 to 111 byte path and the bind fails
# with "AF_UNIX path too long". In the child the tempdir is the ambient TMPDIR
# plus the 28 bytes tests/__init__.py adds, so the band is an ambient TMPDIR
# of 48 to 51 bytes (measured: 47 OK, 48 to 51 FAIL, 52 and 60 OK). An
# interpreter with a 28-byte suffix puts the same socket at 106 bytes, inside
# sun_path and over the budget, so the budget arm sees it there too.
FORKSERVER_BAND_TMPDIR_BYTES = 50

# Long enough that the old chain goes past sun_path for every fixture: the
# shortest old socket path was TMPDIR + 57 bytes (tests/test_harness.py's
# server), so a 100-byte TMPDIR puts it at 157.
LONG_TMPDIR_BYTES = 100

# One test per socket site the whole-suite census found. A top-level tests/
# file whose code names a unix socket in a spelling the scan sees
# (UNIX_SOCKET_NAMES), and whose sockets no test here reaches, fails
# test_every_socket_fixture_file_is_represented. A socket the scan cannot see
# (a numeric family, a stdlib helper that makes the socket internally as
# multiprocessing does) is not held by this list; the audit hook in
# tests/__init__.py refuses it at run time when its path can grow with TMPDIR.
REPRESENTATIVE = (
    # tests/_fakeorca.py FakeDaemon bind, and harness.OrcaAdapter's connect
    "tests.test_codexhomes.SyncOrcaTest.test_selected_account_pools_exactly_one_file",
    # a planted dead endpoint that production connects to
    "tests.test_codexhomes.SyncOrcaTest.test_dead_socket_exits_2_naming_the_socket",
    "tests.test_codexhomes.SyncOrcaCliFallbackTest.test_both_routes_dead_rc2_names_both_failures_and_socket",
    "tests.test_codexhomes.SyncOrcaCliFallbackTest.test_cli_roster_without_active_refuses_honestly",
    # the same FakeDaemon, through tests/test_orcaadopt.py's own temp dir
    "tests.test_orcaadopt.RpcFailOpenTest.test_a_working_daemon_answers_and_carries_the_auth_token",
    "tests.test_orcaadopt.RpcFailOpenTest.test_runtime_metadata_naming_a_dead_socket",
    # tests/test_harness.py's own server, through harness and through fleet
    "tests.test_harness.TheRpcDeadlineIsWallClockNotPerRecv.test_a_valid_reply_is_returned_green",
    "tests.test_harness.TheRpcDeadlineIsWallClockNotPerRecv.test_fleet_sibling_is_bounded_and_returns_none_per_its_contract",
    # multiprocessing.Pool: the forkserver start method binds a listener
    # socket under the process's tempdir
    "tests.test_board.ConcurrencyTest.test_concurrent_writers_lose_nothing",
)

# Runs in the child. `import tests` FIRST: that import routes TMPDIR under
# the ambient directory exactly as a gate run does. `start` is a
# multiprocessing start method to make the default, or "-" to keep the
# interpreter's own.
CHILD = r'''
import json, os, socket, sys, unittest
report, start, names = sys.argv[1], sys.argv[2], sys.argv[3:]
ambient = os.environ.get("TMPDIR")
import tests
if start != "-":
    import multiprocessing
    multiprocessing.set_start_method(start, force=True)
seen = []
real = {op: getattr(socket.socket, op) for op in ("bind", "connect", "connect_ex")}

def spy(op):
    def call(self, address):
        if self.family == socket.AF_UNIX and isinstance(address, (str, bytes)):
            stack, frame = [], sys._getframe(1)
            while frame is not None:
                stack.append(frame.f_code.co_filename)
                frame = frame.f_back
            seen.append({"op": op, "path": os.fsdecode(address),
                         "bytes": len(os.fsencode(address)),
                         "caller": stack[0], "stack": stack})
        return real[op](self, address)
    return call

for op in real:
    setattr(socket.socket, op, spy(op))
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
with open(report, "w") as f:
    json.dump({"ambient": ambient, "tmpdir": os.environ.get("TMPDIR"),
               "ran": result.testsRun,
               "problems": [[t.id(), text] for t, text in
                            result.errors + result.failures],
               "skipped": [t.id() for t, _ in result.skipped],
               "seen": seen,
               "socket_root": getattr(tests, "_SOCKROOT", None)}, f)
'''


# The names the scan treats as making a unix socket: the address family, the
# socketserver unix server classes, the asyncio unix server and connection
# helpers, and multiprocessing.connection.Listener.
UNIX_SOCKET_NAMES = frozenset((
    "AF_UNIX",
    "UnixStreamServer", "UnixDatagramServer",
    "ThreadingUnixStreamServer", "ThreadingUnixDatagramServer",
    "ForkingUnixStreamServer", "ForkingUnixDatagramServer",
    "start_unix_server", "open_unix_connection",
    "create_unix_server", "create_unix_connection",
    "Listener",
))


def _names_a_unix_socket(tree):
    """Whether a parsed module's CODE names one of UNIX_SOCKET_NAMES: as an
    attribute (`socket.AF_UNIX`), a bare name (`AF_UNIX` after a from-import)
    or an imported name, aliased or not. A docstring or string mention is not
    a socket. It reads names, not behaviour: a family passed as a number, a
    getattr by string, or a socket a stdlib helper makes internally is
    invisible to it."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        else:
            continue
        if name in UNIX_SOCKET_NAMES:
            return True
    return False


def _socket_fixture_files():
    """Top-level tests/*.py files whose code the scan sees naming a unix
    socket. Excluded: this module, whose spy compares the family, and
    tests/__init__.py, whose audit hook does."""
    found = set()
    skip = {os.path.abspath(__file__), os.path.join(TESTS, "__init__.py")}
    for name in sorted(os.listdir(TESTS)):
        path = os.path.join(TESTS, name)
        if not name.endswith(".py") or path in skip:
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        if _names_a_unix_socket(tree):
            found.add(name)
    return found


class SocketPathsDoNotGrowWithTmpdirTest(unittest.TestCase):

    _runs = {}

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="helm-test-sockpaths-")
        base = os.path.join(cls.work, "ambient")
        os.mkdir(base)
        pad = max(1, LONG_TMPDIR_BYTES - len(os.fsencode(base)) - 1)
        cls.long_tmpdir = os.path.join(base, "x" * pad)
        os.mkdir(cls.long_tmpdir)
        cls._runs = {}
        cls._ambients = {"long": cls.long_tmpdir}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    @classmethod
    def _ambient(cls, kind):
        """The ambient TMPDIR a child run of `kind` gets, made once per class:
        "long" (LONG_TMPDIR_BYTES), "band" (FORKSERVER_BAND_TMPDIR_BYTES) or
        "short". None when this host cannot make that directory."""
        if kind not in cls._ambients:
            cls._ambients[kind] = cls._make_ambient(kind)
        return cls._ambients[kind]

    @classmethod
    def _make_ambient(cls, kind):
        if kind not in ("short", "band"):
            raise KeyError(kind)
        # Both start from a socket directory. Under the suite's own TMPDIR no
        # directory is short enough (a gate nests it 55 bytes deep). Literal
        # /tmp is short, but a child killed there keeps its suite root and
        # socket root forever: nothing reaps /tmp/helm-suite-env-*, and the
        # socket root's owner never dangles. A socket directory is reaped at
        # this process's exit, or by the orphan sweep once this process's
        # suite root is gone, and takes the child's roots with it.
        try:
            base = tests.socket_dir()
        except OSError:
            return None
        if kind == "short":
            return base
        pad = FORKSERVER_BAND_TMPDIR_BYTES - len(os.fsencode(base)) - 1
        if pad < 1:
            return base    # the band arms fail naming the length
        path = os.path.join(base, "x" * pad)
        os.mkdir(path)
        return path

    def _run(self, kind, start="-"):
        """Run REPRESENTATIVE in a child whose ambient TMPDIR is
        `self._ambient(kind)` and whose default multiprocessing start method
        is `start` ("-" keeps the interpreter's). One child per (kind, start)
        for the whole class; the arms read its report."""
        key = (kind, start)
        if key in self._runs:
            return self._runs[key]
        report = os.path.join(self.work, "report-%d.json" % len(self._runs))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HELM_", "MELD_"))
               and k != "ORCA_USER_DATA_PATH"}
        env["TMPDIR"] = self._ambient(kind)
        out = subprocess.run(
            [sys.executable, "-c", CHILD, report, start] + list(REPRESENTATIVE),
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        self.assertTrue(os.path.exists(report),
                        "the child wrote no report (rc=%s):\n%s"
                        % (out.returncode, out.stderr[-2000:]))
        with open(report) as f:
            got = json.load(f)
        got["stderr"] = out.stderr
        # A socket root the child left behind is removed here whatever the
        # arms decide, so a failing run does not leak onto the short base.
        if got["socket_root"]:
            self.addClassCleanup(shutil.rmtree, got["socket_root"],
                                 ignore_errors=True)
        self._runs[key] = got
        return got

    def _assert_all_passed(self, got):
        self.assertEqual(got["ran"], len(REPRESENTATIVE), got["stderr"][-2000:])
        self.assertEqual(got["skipped"], [])
        self.assertEqual(
            got["problems"], [],
            "representative socket tests failed under TMPDIR=%s (%d bytes):\n%s"
            % (got["tmpdir"], len(os.fsencode(got["tmpdir"] or "")),
               "\n".join(text[-600:] for _tid, text in got["problems"])))

    def _assert_within_budget(self, got):
        self.assertTrue(got["seen"], "the spy saw no AF_UNIX call at all, so "
                                     "a budget check would say nothing")
        over = sorted({(s["bytes"], s["op"], s["path"]) for s in got["seen"]
                       if s["bytes"] > SOCKET_PATH_BUDGET})
        self.assertEqual(
            over, [],
            "AF_UNIX paths over the %d-byte budget (sun_path %d minus the NUL "
            "and a %d-byte margin) under TMPDIR=%s:\n%s"
            % (SOCKET_PATH_BUDGET, SUN_PATH_BYTES, SOCKET_PATH_MARGIN,
               got["tmpdir"], "\n".join("%d %s %s" % o for o in over)))

    def test_representative_socket_tests_pass_under_a_long_tmpdir(self):  # noqa: VACUOUS_ASSERTION — _assert_all_passed opens with an unconditional assertEqual on the child's test count, so an empty run cannot pass
        self._assert_all_passed(self._run("long"))

    def test_no_socket_path_exceeds_the_budget_under_a_long_tmpdir(self):  # noqa: VACUOUS_ASSERTION — _assert_within_budget asserts the spy saw AF_UNIX calls before it asserts none is over budget
        got = self._run("long")
        self.assertGreaterEqual(len(os.fsencode(got["tmpdir"])),
                                LONG_TMPDIR_BYTES - 1, got["tmpdir"])
        self._assert_within_budget(got)

    def test_every_socket_fixture_file_is_represented(self):
        """MUST-HIT on the sample. Every top-level tests/*.py file the scan
        sees naming a unix socket must be on the stack of an AF_UNIX bind or
        connect in the child, so a fixture file in a spelling the scan knows
        cannot sit outside REPRESENTATIVE. A fixture the scan cannot see is
        the audit hook's to refuse (TheSocketPathHookTest), not this arm's."""
        fixtures = _socket_fixture_files()
        self.assertIn("_fakeorca.py", fixtures)   # the scan itself sees
        got = self._run("long")
        here = os.path.realpath(TESTS)
        callers = {os.path.basename(p) for s in got["seen"] for p in s["stack"]
                   if os.path.realpath(os.path.dirname(os.path.join(ROOT, p)))
                   == here}
        self.assertEqual(sorted(fixtures - callers), [],
                         "socket fixture files with no representative test "
                         "in REPRESENTATIVE: saw calls from %s"
                         % sorted(callers))

    def _band(self):
        if self._ambient("band") is None:
            self.skipTest("the socket base %s is not writable, so no ambient "
                          "short enough for the forkserver band exists"
                          % tests.SOCKET_BASE)
        got = self._run("band", start="forkserver")
        self.assertEqual(len(os.fsencode(got["ambient"])),
                         FORKSERVER_BAND_TMPDIR_BYTES, got["ambient"])
        return got

    def test_representative_socket_tests_pass_in_the_forkserver_band(self):  # noqa: VACUOUS_ASSERTION — _band asserts the ambient's exact length and _assert_all_passed opens with an unconditional assertEqual on the child's test count
        self._assert_all_passed(self._band())

    def test_no_socket_path_exceeds_the_budget_in_the_forkserver_band(self):  # noqa: VACUOUS_ASSERTION — _assert_within_budget asserts the spy saw AF_UNIX calls before it asserts none is over budget
        self._assert_within_budget(self._band())

    def test_the_same_tests_pass_under_a_short_tmpdir(self):  # noqa: VACUOUS_ASSERTION — both helpers open with unconditional positive controls: the child's test count, and a non-empty spy record
        """CONTROL: under a short TMPDIR the same tests pass and the same
        budget holds, so the long-TMPDIR arms fail on length and nothing else."""
        if self._ambient("short") is None:
            self.skipTest("no short writable ambient directory on this host")
        got = self._run("short")
        self._assert_all_passed(got)
        self._assert_within_budget(got)

    def test_every_child_ambient_lies_where_this_process_reaps_it(self):
        """A child killed with its parent never runs its atexit reap, so its
        suite root and socket root stay wherever its ambient TMPDIR put them.
        Every ambient must lie under this process's suite root (removed by the
        gate's scratch cleanup) or its socket root (removed by the orphan
        sweep once that suite root is gone), never in a shared directory."""
        reaped = (tests._testroot(), tests._socketroot())
        made = 0
        for kind in ("long", "band", "short"):
            ambient = self._ambient(kind)
            if ambient is None:
                continue
            made += 1
            with self.subTest(kind=kind):
                self.assertTrue(
                    any(ambient.startswith(r + os.sep) for r in reaped),
                    "the %s child's ambient %s is outside %s"
                    % (kind, ambient, " and ".join(reaped)))
        self.assertGreater(made, 0, "no ambient was made, so nothing was "
                                    "checked")

    def test_nothing_is_left_behind_after_the_child_exits(self):
        got = self._run("long")
        self.assertEqual(os.listdir(self.long_tmpdir), [],
                         "the child left entries under the long TMPDIR")
        self.assertIsNotNone(
            got["socket_root"],
            "the child minted no socket root: no fixture went through "
            "tests.socket_dir()")
        self.assertFalse(os.path.lexists(got["socket_root"]),
                         "the socket root %s outlived the child"
                         % got["socket_root"])
        binds = {os.path.dirname(s["path"]) for s in got["seen"]
                 if s["op"] == "bind"}
        self.assertTrue(binds, "the child bound no socket, so there was "
                               "nothing to leave behind")
        self.assertEqual(sorted(d for d in binds if os.path.lexists(d)), [])


class TheSocketDirectoryDoorTest(unittest.TestCase):
    """The mechanism behind the arms above, in this process."""

    def test_a_socket_dir_is_short_and_its_root_names_its_owner(self):
        d = tests.socket_dir(self)
        root = tests._SOCKROOT
        self.assertEqual(os.path.dirname(d), root)
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(os.path.dirname(root), tests.SOCKET_BASE)
        self.assertEqual(os.readlink(os.path.join(root, tests.SOCKET_OWNER_LINK)),
                         tests._testroot())
        self.assertFalse(d.startswith(tempfile.gettempdir() + os.sep), d)
        self.assertLessEqual(
            len(os.fsencode(d)) + 1 + tests.SOCKET_NAME_BYTES,
            SOCKET_PATH_BUDGET, d)

    def test_an_orphaned_socket_root_is_reaped_and_nothing_else(self):
        base = tempfile.mkdtemp(prefix="helm-test-sockbase-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        live = tempfile.mkdtemp(prefix="helm-test-live-owner-")
        self.addCleanup(shutil.rmtree, live, ignore_errors=True)
        prefix = tests.SOCKET_ROOT_PREFIX

        def plant(name, owner):
            root = os.path.join(base, name)
            os.makedirs(os.path.join(root, "tmpabc"))
            if owner is not None:
                os.symlink(owner, os.path.join(root, tests.SOCKET_OWNER_LINK))
            return root

        dead = plant(prefix + "dead", os.path.join(base, "gone-suite-root"))
        alive = plant(prefix + "alive", live)
        anonymous = plant(prefix + "anonymous", None)
        foreign = plant("someone-else-dead", os.path.join(base, "gone"))
        with mock.patch.object(tests, "SOCKET_BASE", base):
            tests._reap_orphan_socket_roots()
        self.assertFalse(os.path.lexists(dead), "a root whose owner is gone "
                                                "was not reaped")
        self.assertTrue(os.path.isdir(alive), "a live run's root was reaped")
        self.assertTrue(os.path.isdir(anonymous))
        self.assertTrue(os.path.isdir(foreign))
        self.assertTrue(os.path.isdir(live))

    def test_a_case_s_socket_dir_goes_at_that_case_s_cleanup(self):
        """`socket_dir(case)` removes the directory when that case cleans up,
        not only when the process's socket root is reaped at exit."""
        class Throwaway(unittest.TestCase):
            def runTest(self):
                pass

        case = Throwaway()
        d = tests.socket_dir(case)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.assertTrue(os.path.isdir(d), d)
        case.doCleanups()
        self.assertFalse(os.path.lexists(d),
                         "the case's socket directory survived its cleanup")
        self.assertTrue(os.path.isdir(tests._SOCKROOT),
                        "the cleanup took the whole socket root, not the "
                        "case's directory")

    def test_an_unusable_base_refuses_naming_sun_path(self):
        missing = os.path.join(tempfile.mkdtemp(prefix="helm-test-nobase-"),
                               "absent")
        self.addCleanup(shutil.rmtree, os.path.dirname(missing),
                        ignore_errors=True)
        with mock.patch.object(tests, "SOCKET_BASE", missing), \
                mock.patch.object(tests, "_SOCKROOT", None):
            with self.assertRaises(OSError) as cm:
                tests.socket_dir()
        self.assertIn("sun_path", str(cm.exception))
        self.assertIn(missing, str(cm.exception))


class TheSocketPathHookTest(unittest.TestCase):
    """The audit hook tests/__init__.py installs in every process that imports
    the tests package, measured in this process through real sockets."""

    def _unix(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        return s

    def _path_of(self, total):
        """A socket path of exactly `total` bytes in a real directory under
        this case's socket directory, so a bind the guard lets through
        succeeds rather than failing on a missing directory."""
        d = tests.socket_dir(self)
        name = "s.sock"
        pad = total - len(os.fsencode(d)) - 2 - len(name)
        self.assertGreater(pad, 0, d)
        sub = os.path.join(d, "p" * pad)
        os.mkdir(sub)
        path = os.path.join(sub, name)
        self.assertEqual(len(os.fsencode(path)), total, path)
        return path

    def _assert_refused(self, cm, path, reason):
        self.assertNotIsInstance(
            cm.exception, OSError,
            "the refusal is an OSError, which production's `except OSError` "
            "turns into a handled failure, so a bypassing test passes on the "
            "wrong error: %r" % (cm.exception,))
        text = str(cm.exception)
        self.assertIn(path, text)
        self.assertIn(reason, text)
        self.assertIn("tests.socket_dir()", text)

    def test_a_bind_over_the_budget_is_refused_naming_the_budget(self):  # noqa: VACUOUS_ASSERTION — assertRaises is unconditional and _assert_refused asserts the refusal text names the path, the budget and the door
        path = self._path_of(OVER_BUDGET_BYTES)
        with self.assertRaises(BaseException) as cm:
            self._unix().bind(path)
        self._assert_refused(cm, path, "%d-byte" % SOCKET_PATH_BUDGET)
        self.assertFalse(os.path.lexists(path),
                         "the socket file exists, so the refusal came after "
                         "the bind")

    def test_a_connect_over_the_budget_is_refused_before_it_fails_as_an_oserror(self):  # noqa: VACUOUS_ASSERTION — the loop is over a two-item literal, and each pass asserts a raise and its text through _assert_refused
        path = self._path_of(OVER_BUDGET_BYTES)    # nothing listens here
        for op in ("connect", "connect_ex"):
            with self.subTest(op=op):
                with self.assertRaises(BaseException) as cm:
                    getattr(self._unix(), op)(path)
                self._assert_refused(cm, path, "%d-byte" % SOCKET_PATH_BUDGET)

    def test_a_socket_under_the_suite_root_is_refused_well_inside_the_budget(self):  # noqa: VACUOUS_ASSERTION — assertRaises is unconditional and _assert_refused asserts the refusal text names the path and TMPDIR
        # CONFINED: the suite root the hook reads is patched to this case's
        # own short directory, so the path is far inside the budget and only
        # the TMPDIR rule can refuse it.
        d = tests.socket_dir(self)
        path = os.path.join(d, "s.sock")
        self.assertLess(len(os.fsencode(path)), SOCKET_PATH_BUDGET, path)
        with mock.patch.object(tests, "_TESTROOT", d):
            with self.assertRaises(BaseException) as cm:
                self._unix().bind(path)
        self._assert_refused(cm, path, "TMPDIR")
        self.assertFalse(os.path.lexists(path))

    def test_a_fixture_socket_under_tempfile_is_refused(self):  # noqa: VACUOUS_ASSERTION — assertRaises is unconditional and the refusal text must name the path before the absent socket file is asserted
        """The bypass itself: a fixture that makes its socket directory with
        tempfile instead of tests.socket_dir(), at this run's TMPDIR."""
        d = tempfile.mkdtemp(prefix="sk")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "s.sock")
        with self.assertRaises(BaseException) as cm:
            self._unix().bind(path)
        # WHICH REFUSAL IS CORRECT DEPENDS ON THE AMBIENT TMPDIR, and the arm
        # must say so rather than assume a short one: at
        # 108 bytes or more CPython refuses while parsing the address, BEFORE
        # the audit event, so the ordinary OSError is the right answer and the
        # hook never sees the path. Inside the window the hook owns it.
        if len(os.fsencode(path)) >= SUN_PATH_BYTES:
            self.assertIsInstance(cm.exception, OSError, repr(cm.exception))
            self.assertNotIsInstance(cm.exception, tests.SocketPathRefused)
        else:
            self.assertNotIsInstance(cm.exception, OSError, repr(cm.exception))
            self.assertIn(path, str(cm.exception))
        self.assertFalse(os.path.lexists(path))
        # AND THE HOOK IS EXERCISED WHATEVER THE AMBIENT LENGTH IS: the same
        # bypass under a short base lands inside the window, so this arm can
        # never degrade into a test of CPython's own length check.
        short = tempfile.mkdtemp(prefix="sk", dir=tests.SOCKET_BASE)
        self.addCleanup(shutil.rmtree, short, ignore_errors=True)
        inside = os.path.join(short, "s.sock")
        self.assertLess(len(os.fsencode(inside)), SUN_PATH_BYTES, inside)
        with mock.patch.object(tests, "_TESTROOT", short):
            with self.assertRaises(tests.SocketPathRefused) as cm2:
                self._unix().bind(inside)
        self.assertIn(inside, str(cm2.exception))

    def test_an_unreadable_owner_is_not_read_as_a_gone_one(self):
        """An owner link the sweep cannot READ is UNKNOWN, never gone.
        os.path.exists() answers False for ANY failure — including EACCES on a
        directory above the owner — so a live run whose suite root sits under a
        directory its operator temporarily closed would read as an orphan, and
        another run's first socket_dir() would delete its live socket tree.
        Only ENOENT means gone; anything else leaves the root alone."""
        if os.geteuid() == 0:
            self.skipTest("root traverses a 0-mode directory, so the denial "
                          "this arm needs cannot be constructed")
        base = tempfile.mkdtemp(prefix="sockbase")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        closed = os.path.join(base, "closed")
        os.makedirs(os.path.join(closed, "suite"))
        live = self._planted_root(base, os.path.join(closed, "suite"))
        gone = self._planted_root(base, os.path.join(base, "never-existed"))
        alive = self._planted_root(base, base)    # an owner that plainly exists
        os.chmod(closed, 0)
        self.addCleanup(os.chmod, closed, 0o700)
        # MUST-HIT: the denial is real for this process, so the sweep below
        # really does face the ambiguous answer this arm is about.
        with self.assertRaises(OSError):
            os.stat(os.path.join(closed, "suite"))
        with mock.patch.object(tests, "SOCKET_BASE", base):
            tests._reap_orphan_socket_roots()
        self.assertTrue(os.path.isdir(live),
                        "a live run's socket root was reaped because its "
                        "owner could not be read")
        self.assertTrue(os.path.isdir(alive), "control: a readable owner")
        self.assertFalse(os.path.isdir(gone),
                         "control: a genuinely gone owner is still reaped")

    def _planted_root(self, base, owner_target):
        """One socket root in `base`, whose owner link names `owner_target` —
        the shape _socketroot() mints, planted so the sweep can be asked."""
        root = tempfile.mkdtemp(prefix=tests.SOCKET_ROOT_PREFIX, dir=base)
        os.symlink(owner_target, os.path.join(root, tests.SOCKET_OWNER_LINK))
        return root

    def test_a_path_within_the_budget_and_an_abstract_name_still_bind(self):  # noqa: VACUOUS_ASSERTION — each bind's getsockname must equal the exact address bound, a positive observable
        """CONTROL: the guard refuses on length and place, not on AF_UNIX."""
        path = self._path_of(SOCKET_PATH_BUDGET)
        s = self._unix()
        s.bind(path)
        self.assertEqual(s.getsockname(), path)
        if not sys.platform.startswith("linux"):
            return
        head = b"\0helm-test-abstract-%d-" % os.getpid()
        abstract = head + b"a" * (OVER_BUDGET_BYTES - len(head))
        a = self._unix()
        a.bind(abstract)
        self.assertEqual(a.getsockname(), abstract)


class TheFixtureScanTest(unittest.TestCase):
    """The static scan behind test_every_socket_fixture_file_is_represented."""

    SPELLINGS = {
        "socket.AF_UNIX": "import socket\ns = socket.socket(socket.AF_UNIX)\n",
        "from-import": "from socket import AF_UNIX, socket as sk\ns = sk(AF_UNIX)\n",
        "aliased from-import": "from socket import AF_UNIX as U\n",
        "socketserver": "import socketserver\nsocketserver.ThreadingUnixStreamServer(p, h)\n",
        "socketserver from-import": "from socketserver import UnixStreamServer\n",
        "asyncio": "import asyncio\nasyncio.start_unix_server(cb, path=p)\n",
        "event loop": "loop.create_unix_server(f, path=p)\n",
        "multiprocessing listener": "from multiprocessing.connection import Listener\n",
    }

    def test_the_scan_sees_each_spelling_of_a_unix_socket(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty SPELLINGS class literal and every pass asserts the scan returns True
        for label, source in self.SPELLINGS.items():
            with self.subTest(label):
                self.assertTrue(_names_a_unix_socket(ast.parse(source)), source)

    def test_a_mention_in_prose_or_a_string_is_not_a_socket(self):
        self.assertTrue(_names_a_unix_socket(ast.parse(
            self.SPELLINGS["socket.AF_UNIX"])))
        self.assertFalse(_names_a_unix_socket(ast.parse(
            '"""Binds an AF_UNIX socket through a Listener."""\n'
            'x = "UnixStreamServer"\n')))


if __name__ == "__main__":
    unittest.main()
