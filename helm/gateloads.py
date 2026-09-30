#!/usr/bin/env python3
"""Recorded loads: which repository files each test module's run reached.

task/3039 lane 2. A focused run is cheap only when its selection is small, and
the static import closure cannot make it small: 312 of 355 helm modules form
one cycle through function-local imports, so a change to almost any module
reaches almost every test through SOME lazy edge. A whole-suite run already
executes every test once, so it can simply WRITE DOWN what each test module
reached. The focused planner then selects by that record instead of by the
closure (`gate.focus_plan`, policy `POLICY`).

WHAT A RECORD HOLDS, per test module M, as repository-relative paths:
  * every repository module the import system resolved while M's tests ran,
    cached or not (`builtins.__import__` and `importlib.import_module` are
    wrapped, because an import STATEMENT reaches `__import__` even when the
    module is already loaded, and serial discovery loads everything first);
  * every repository file M opened and every repository directory it listed
    (an audit hook on `open`, `os.listdir` and `os.scandir`), so a test that
    reads source or data by path records that path;
  * the same from every helm child process a test started: `helm/__init__`
    writes the child's loaded modules and opened paths at exit when
    `DIR_ENV` is set, under the window the runner was in when it spawned
    that child's line (`WINDOW_ENV`);
  * the module and class fixtures BETWEEN tests, charged to the module whose
    fixture runs (unittest tears the previous module down at the next
    module's first test, so a window per test alone would charge a lazy
    teardown import to the wrong one), and the rest of the time between
    tests to the module before it. Charging that time to BOTH neighbours
    made the record depend on which module a slice worker happened to run
    next (measured: two recordings of one tree disagreed about 70 modules,
    each by up to 249 paths);
  * closed under module-scope loading: what each module body reached when it
    executed (recorded) and its module-scope imports (read from source), so a
    module that another test had already loaded still counts in full.

THE SOUNDNESS CLAIM, and its limits. A test that did not reach file F at T0
reaches changed code in F at T1 only through a file it did reach, and that
file's change puts the test in the selection. The recorder cannot see: a
raw `sys.modules[...]` read (refused tree-wide by a census arm,
`raw_reads`); an import through an `import_module` bound before the
wrapper existed; a child process that is not a helm python (a script run by
path, git) or that ends without atexit; process state one test module leaves
for another (the slice runner's data audit owns that channel). Each miss
costs localization only: a focused receipt binds no approve and no land.

STANDALONE ON PURPOSE. The slice runner loads this file by path in each
worker before discovery (it may not import `helm` first, `gateslice`'s own
rule), and the serial runner does the same through gatetestrecord, so the
recorder is never `helm.gateloads` inside the process it observes. Stdlib
only; git is the caller's business. A helm CHILD loads it by path too, and
only `os` and `sys` at module scope, so a child pays for the record it
writes and nothing else (every heavier module is imported where it is used).
"""
import builtins
import importlib
import os
import sys


DIR_ENV = "HELM_GATE_LOADS_DIR"
ROOT_ENV = "HELM_GATE_LOADS_ROOT"
# The runner's arm token. The runner that arms POPS it, so a runner a test
# starts inside the recorded run (a nested suite, a nested slice run) never
# arms a second recorder; DIR/ROOT/WINDOW stay so its processes are still
# charged to the outer test as children.
RUNNER_ENV = "HELM_GATE_LOADS_RUNNER"
# The path of the armed runner's WINDOW LOG: one line per window switch, the
# boot-clock time, the kernel's last-allocated pid and the window's index. A
# child finds the window that was open when the runner SPAWNED its line of
# processes: the switches near that process's start time (the clock's tick,
# 10 ms, spans many quick tests), narrowed to one by its pid, which the
# kernel hands out in order. Reading "the window now" when helm is imported
# charged a child a test did not wait for to whatever test ran next
# (measured: two runs of one tree disagreed about 74 of 461 test modules).
# NOT the window itself in
# the environment: an environment variable that changes at every test would
# be a leak at every unit boundary of the slice runner's audit, and a diff in
# every class-level environment snapshot.
WINDOW_ENV = "HELM_GATE_LOADS_WINDOW"
ENV_KEYS = (DIR_ENV, ROOT_ENV, RUNNER_ENV, WINDOW_ENV)

# THE MARKER FOR A CHILD WHOSE ENVIRONMENT LOST THE KEYS. A test that runs
# `bin/helm doctor` under a clean environment starts a helm the variables
# never reach (measured: a lane red in exactly such a child was missed by the
# replay, its test module's record held 9 paths). The gate writes this file
# into the checkout's GIT dir (never the worktree, so no tree changes), naming
# the loads directory; such a child finds it through its own package's
# checkout and its runner through its own ancestry.
MARKER = "helm-gate-loads"
EVENT = "gate-module-loads"
VERSION = 1
POLICY = "changed+recorded-loads-v3"
ENCODING = "zlib-base64-roots-edges-reads-v1"
ID_LEN = 32
REASON_CAP = 400
_TAIL = 16384        # bytes of a window log a child reads per step back
_LAST_PID = "/proc/sys/kernel/ns_last_pid"
_EVENTS = frozenset(("open", "os.listdir", "os.scandir"))
# THE MEMO PROTOCOL. A process-wide memo of the real tree (a census computed
# once per process) reads its files for the FIRST caller only, so every later
# test that uses it looked as if it read nothing (measured: 13 of 462 test
# modules swapped a ~250-file read set between two recordings of one tree,
# by which module a slice worker happened to run first). Such a memo raises
# `sys.audit(MEMO_EVENT, key, phase)`: "miss" before computing, "done" after,
# "hit" on every reuse; the recorder keeps what each key's computation
# reached and charges it again to every window that hits it. A process with
# no recorder pays one C-level check per call.
MEMO_EVENT = "helm.memo"
ROOT_DIR = "./"


# ----------------------------------------------------------------- paths


def _normal(path):
    if "/./" in path or "/../" in path or "//" in path \
            or path.endswith(("/.", "/..")):
        return os.path.normpath(path)
    return path


def _source_of_cache(rel):
    """`pkg/__pycache__/mod.cpython-312.pyc` -> `pkg/mod.py`: a cached module
    is read from its bytecode, and the change that matters is its source.
    Every name there maps, including the temporary `mod.cpython-312.pyc.<n>`
    the import system writes before renaming it (measured: 5,308 of one
    run's 14,385 recorded paths were bytecode names). None for the directory
    itself, which is no source."""
    head, _sep, name = ("/" + rel).rpartition("/__pycache__/")
    stem = name.split(".", 1)[0]
    if not stem or "/" in name:
        return None
    return (head[1:] + "/" if head else "") + stem + ".py"


class Roots:
    """The repository root in both spellings (as given and real), and the
    relative path of anything under it."""

    def __init__(self, root):
        root = os.path.abspath(root)
        self.given = root.rstrip("/")
        self.real = os.path.realpath(root).rstrip("/")
        self.prefixes = tuple({self.given + "/", self.real + "/"})
        self._memo = {}

    def rel(self, path, directory=False):
        if not isinstance(path, str):
            try:
                path = os.fsdecode(os.fspath(path))
            except TypeError:
                return None
        if not path:
            return None
        if path[0] == "/":
            # An absolute path's answer depends on nothing but the path, and
            # the audit hook asks it for every open in the process.
            key = (path, directory)
            memo = self._memo
            if key in memo:
                return memo[key]
            if len(memo) > 200000:
                memo.clear()
            memo[key] = found = self._rel(path, directory)
            return found
        return self._rel(path, directory)

    def _rel(self, path, directory):
        if path[0] != "/":
            # RELATIVE TO WHAT is not in the audit event: an `os.open(name,
            # dir_fd=fd)` names a path inside the fd's directory, and read
            # against cwd it became a stray repository path (measured: 8,000
            # of one run's recorded paths, `.claiming-*` names among them). So
            # a relative name counts only where it really stands under cwd.
            try:
                path = os.path.join(os.getcwd(), path)
            except OSError:
                return None
            if not os.path.lexists(path):
                return None
        path = _normal(path)
        if path in (self.given, self.real):
            return ROOT_DIR if directory else None
        for prefix in self.prefixes:
            if path.startswith(prefix):
                rel = path[len(prefix):]
                break
        else:
            return None
        if rel == ".git" or rel.startswith(".git/"):
            return None
        try:
            # A name that is not text (a test probing undecodable bytes) is
            # no path a diff names, and no ledger line may carry it.
            rel.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if "/__pycache__" in "/" + rel:
            rel = _source_of_cache(rel.rstrip("/") + ("/" if directory
                                                      else ""))
            return rel
        return rel.rstrip("/") + "/" if directory else rel


def _import_system_listing():
    """Is the directory listing being audited the IMPORT SYSTEM's own (a
    path finder filling its cache)? Its caller is two frames up: the audit
    hook, then the Python frame that called the listing. Such a listing
    reads no directory for the test: counting it put `helm/` and `tests/`
    in every record, so any added module selected nearly every test."""
    try:
        caller = sys._getframe(2)
    except ValueError:
        return False
    return caller.f_code.co_filename.startswith("<frozen importlib")


def top_packages(root):
    """The top-level import names this repository defines: a directory
    holding `__init__.py`."""
    out = set()
    try:
        names = os.listdir(root)
    except OSError:
        return frozenset()
    for name in names:
        if name.isidentifier() \
                and os.path.isfile(os.path.join(root, name, "__init__.py")):
            out.add(name)
    return frozenset(out)


def predicted(name):
    """The two files a dotted name could be, for an import that FAILED: a
    module that appears later changes what the probing test does."""
    base = name.replace(".", "/")
    return (base + ".py", base + "/__init__.py")


def module_path(name):
    """The path of a TEST module name (`tests.test_x` -> `tests/test_x.py`)."""
    return name.replace(".", "/") + ".py"


# ------------------------------------------------------------- recorder


class _Reach:
    """What one window or one module body reached: the modules the import
    system resolved (`loads`, whose own module-scope reach the record
    follows) and the files or directories merely READ (`reads`, leaves: a
    test that reads a module's text did not run what that module imports)."""
    __slots__ = ("loads", "reads", "seen")

    def __init__(self):
        self.loads, self.reads = set(), set()
        # import statements (absolute name, fromlist) already charged here:
        # the second run of one in the same window changes nothing
        self.seen = set()

    def update(self, other):
        self.loads |= other.loads
        self.reads |= other.reads


# unittest's fixture steps, each run between two tests, and the module each
# one runs code of -> None when the step would do nothing (same module or
# class as before), so an ordinary test costs no window switch. These are
# TestSuite's private methods, stable since Python 3.2; a Python without one
# of them breaks the record (`install`) rather than quietly charging its
# fixtures to the wrong module.
def _previous_module(result):
    previous = getattr(result, "_previousTestClass", None)
    return getattr(previous, "__module__", None)


def _next_module(test, result):
    previous = getattr(result, "_previousTestClass", None)
    if test is None or test.__class__ is previous:
        return None
    return test.__class__.__module__


_FIXTURES = {
    "_tearDownPreviousClass": lambda test, result: None
    if test is not None and test.__class__ is getattr(
        result, "_previousTestClass", None) else _previous_module(result),
    "_handleModuleTearDown": lambda result: _previous_module(result),
    "_handleModuleFixture": lambda test, result: None
    if test.__class__.__module__ == _previous_module(result)
    else test.__class__.__module__,
    "_handleClassSetUp": _next_module,
}


class _WriteThrough(set):
    """A set that also appends each new member to a file at once."""

    def __init__(self, fd, tag):
        super().__init__()
        self.fd, self.tag = fd, tag

    def add(self, item):
        if item in self:
            return
        super().add(item)
        try:
            os.write(self.fd, self._line(item))
        except (OSError, TypeError, ValueError):
            pass

    def _line(self, item):
        import json
        return (json.dumps([self.tag, item]) + "\n").encode("utf-8")


class _ForkSink:
    """The sink of a process FORKED from a runner: it usually leaves through
    `os._exit`, which runs no atexit writer, so every path it reaches goes to
    its file the moment it is noted. Two recordings of one tree differed by
    ~200 paths in 13 test modules; the traced cause is the relevance scorer,
    which forks when its process has one thread and starts a fresh
    interpreter otherwise, so only the fresh interpreter was recorded."""
    __slots__ = ("loads", "reads", "seen")

    def __init__(self, fd):
        self.loads, self.reads = _WriteThrough(fd, "l"), _WriteThrough(fd, "r")
        self.seen = set()


class Recorder:
    """The in-process recorder of ONE runner (the serial suite process or
    one slice worker). -> install(), begin_run(), result_class(base),
    finish() -> artifact dict.

    The sink is the set the next noted path goes to: nothing during
    discovery (a module body's own reads go to `bodies` instead), the test
    module's window while one of its tests runs or one of its fixtures does
    (`_FIXTURES`), and the previous module's window for the rest of the
    time between tests (a thread it left finishing, the runner's own
    bookkeeping)."""

    def __init__(self, root, directory=None, window_file=None):
        self.roots = Roots(root)
        self.tops = top_packages(self.roots.given)
        self.directory = directory
        self.window_file = window_file
        self.reach = {}
        self.bodies = {}
        self.windows = []
        self.current = None
        self.gap = _Reach()
        self.sink = _Reach()
        import threading
        self._local = threading.local()
        self._cache = {}
        self.broken = None
        self.calls = 0
        self.on = False
        self._window_fd = None
        self._pid_fd = None
        self._results = []
        self._fixture_originals = {}
        self._memo_open = []        # [(key, _Reach)] computations in flight
        self._memo_sets = {}        # key -> _Reach a computation reached
        self._orig_import = None
        self._orig_import_module = None

    # -- noting

    def _stack(self):
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _note(self, rel):
        self.sink.loads.add(rel)
        stack = getattr(self._local, "stack", None)
        if stack:
            stack[-1].loads.add(rel)
        for _key, reach in self._memo_open:
            reach.loads.add(rel)

    def _note_read(self, rel):
        self.sink.reads.add(rel)
        stack = getattr(self._local, "stack", None)
        if stack:
            stack[-1].reads.add(rel)
        for _key, reach in self._memo_open:
            reach.reads.add(rel)

    def _memo(self, args):
        """One MEMO_EVENT: open a key's capture, keep it, or charge it."""
        if len(args) != 2 or not isinstance(args[0], str):
            return
        key, phase = args
        if phase == "miss":
            self._memo_open.append((key, _Reach()))
        elif phase == "done":
            for i in range(len(self._memo_open) - 1, -1, -1):
                if self._memo_open[i][0] == key:
                    self._memo_sets[key] = self._memo_open.pop(i)[1]
                    break
        elif phase == "hit":
            reach = self._memo_sets.get(key)
            if reach is not None:
                for rel in reach.loads:
                    self._note(rel)
                for rel in reach.reads:
                    self._note_read(rel)

    def _note_name(self, name):
        module = sys.modules.get(name)
        hit = self._cache.get(name)
        if hit is not None and hit[0] is module:
            rel = hit[1]
        else:
            rel = None
            path = getattr(module, "__file__", None) if module is not None \
                else None
            if isinstance(path, str):
                rel = self.roots.rel(path)
            self._cache[name] = (module, rel)
        if rel:
            self._note(rel)
        elif module is None:
            for path in predicted(name):
                self._note(path)

    def _note_resolved(self, absname, fromlist):
        parts = absname.split(".")
        for i in range(1, len(parts) + 1):
            self._note_name(".".join(parts[:i]))
        for item in fromlist or ():
            if isinstance(item, str) and item != "*":
                sub = absname + "." + item
                if sub in sys.modules:
                    self._note_name(sub)

    # -- the wrappers

    @staticmethod
    def _absolute(name, globals_, level):
        if not level:
            return name
        package = None
        if globals_:
            package = globals_.get("__package__")
            if not package:
                spec = globals_.get("__spec__")
                package = getattr(spec, "parent", None)
            if not package:
                package = globals_.get("__name__") if "__path__" in globals_ \
                    else (globals_.get("__name__") or "").rpartition(".")[0]
        if not package:
            return None
        bits = package.rsplit(".", level - 1)
        if len(bits) < level:
            return None
        base = bits[0]
        return base + "." + name if name else base

    def _in_frame(self, names, call):
        """Run `call` with a fresh body frame; charge what it noted to each
        of `names` that it loaded. -> value"""
        stack = self._stack()
        stack.append(_Reach())
        try:
            value = call()
        finally:
            body = stack.pop()
            for name in names:
                path = getattr(sys.modules.get(name), "__file__", None)
                rel = self.roots.rel(path) if isinstance(path, str) else None
                if rel:
                    self.bodies.setdefault(rel, _Reach()).update(body)
        # NOT copied into the importer's frame: the importer noted the
        # module itself, and the record's closure follows the body from
        # there. Copying it made every body carry its whole subtree as
        # edges (measured: 15,321 edges in one run's record).
        return value

    def _passes(self, absname, fromlist):
        """Is this import statement one the recorder has nothing to note
        for? Outside the repository's packages, or THE SECOND RUN OF ONE
        STATEMENT IN ONE WINDOW: a function-local import in a loop ran the
        whole resolution per call (~5 us each), which a timing-bound test on
        a slow build node felt. The repeat passes only outside every module
        body (a body's frame must see it) and only while the module is still
        loaded (a test that drops it from sys.modules re-runs its body, which
        must get its frame)."""
        if absname is None or absname.partition(".")[0] not in self.tops:
            return True
        if getattr(self._local, "stack", None):
            return False
        seen = getattr(self.sink, "seen", None)
        return seen is not None and absname in sys.modules \
            and (absname, tuple(fromlist) if fromlist else ()) in seen

    def _run_import(self, call, absname, fromlist, direct):
        if absname is None or absname.partition(".")[0] not in self.tops:
            return call()
        self.calls += 1
        stack = getattr(self._local, "stack", None)
        key = (absname, tuple(fromlist) if fromlist else ())
        seen = getattr(self.sink, "seen", None)
        items = [i for i in fromlist or () if isinstance(i, str) and i != "*"]
        try:
            # EACH NEW MODULE GETS ITS OWN FRAME. The import system runs `a`,
            # then `a.b`, then `a.b.c`, then each fromlist submodule, one
            # after another inside ONE call, so one frame charged a package's
            # `__init__` with everything its submodules reach, and every test
            # that touches the package inherited it (measured: the first
            # `from helm import X` in a worker put X's reach in helm's own
            # body, so in every record). Importing each first, alone, is the
            # same work in the same order.
            parts = absname.split(".")
            for i in range(1, len(parts) + 1):
                prefix = ".".join(parts[:i])
                if prefix not in sys.modules:
                    self._in_frame((prefix,), lambda p=prefix: direct(p))
            package = sys.modules.get(absname)
            for item in items:
                sub = absname + "." + item
                # The fromlist's own rule: a name the package already
                # binds is no submodule import, and a missing one is left to
                # the real call, which decides whether it is an error.
                if sub in sys.modules or package is None \
                        or not hasattr(package, "__path__") \
                        or hasattr(package, item):
                    continue
                try:
                    self._in_frame((sub,), lambda s=sub: direct(s))
                except ModuleNotFoundError as exc:
                    if exc.name != sub:
                        raise
            value = call()
        except BaseException:
            for path in predicted(absname):
                self._note(path)
            for item in items:
                for path in predicted(absname + "." + item):
                    self._note(path)
            raise
        self._note_resolved(absname, fromlist)
        if not stack and seen is not None:
            seen.add(key)
        return value

    def install(self):
        original = builtins.__import__
        original_module = importlib.import_module

        def import_(name, globals=None, locals=None, fromlist=(), level=0):
            if not self.on:
                return original(name, globals, locals, fromlist, level)
            absname = self._absolute(name, globals, level)
            if self._passes(absname, fromlist):
                return original(name, globals, locals, fromlist, level)
            return self._run_import(
                lambda: original(name, globals, locals, fromlist, level),
                absname, fromlist,
                lambda prefix: original(prefix, None, None, (), 0))

        def import_module(name, package=None):
            if not self.on:
                return original_module(name, package)
            absname = name
            if name.startswith("."):
                dots = len(name) - len(name.lstrip("."))
                if package:
                    bits = package.rsplit(".", dots - 1)
                    absname = bits[0] + ("." + name[dots:] if name[dots:]
                                         else "") \
                        if len(bits) >= dots else None
                else:
                    absname = None
            return self._run_import(
                lambda: original_module(name, package), absname, (),
                original_module)

        import_.__wrapped__ = original
        import_module.__wrapped__ = original_module
        self._orig_import, self._orig_import_module = original, original_module
        self._import, self._import_module = import_, import_module
        builtins.__import__ = import_
        importlib.import_module = import_module
        sys.addaudithook(self._audit)
        self._hook_fixtures()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._forked)
        self.on = True
        if self.window_file:
            try:
                self._pid_fd = os.open(_LAST_PID, os.O_RDONLY | os.O_CLOEXEC)
            except OSError:
                self._pid_fd = None
            try:
                self._window_fd = os.open(
                    self.window_file,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
                    0o600)
                self._write_window(-1)
            except OSError:
                self._window_fd = None
        return self

    def _forked(self):
        """In a process forked from this runner: charge what it reaches to
        the window it was forked in, written as it goes (`_ForkSink`)."""
        if not self.on or not self.directory:
            return
        import json
        import time
        try:
            fd = os.open(os.path.join(self.directory, "child-%d-%d.jsonl" % (
                os.getpid(), time.monotonic_ns())),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
            os.write(fd, (json.dumps({
                "v": VERSION, "type": "fork", "window_file": self.window_file,
                "windows": [len(self.windows) - 1]}) + "\n").encode("utf-8"))
        except OSError:
            return
        self.sink = _ForkSink(fd)
        self._window_fd = None      # the runner's log is the runner's alone

    def _audit(self, event, args):
        if event not in _EVENTS or not self.on:
            if event == MEMO_EVENT and self.on:
                try:
                    self._memo(args)
                except Exception:               # noqa: BLE001 -- never break
                    pass
            return
        if event != "open" and _import_system_listing():
            return
        try:
            path = args[0] if args else "."
            if path is None:
                path = "."
            if isinstance(path, int):
                return
            rel = self.roots.rel(path, directory=event != "open")
            if rel:
                self._note_read(rel)
        except Exception:                       # noqa: BLE001 -- never break I/O
            pass

    def _write_window(self, index):
        if self._window_fd is None:
            return
        last = -1
        if self._pid_fd is not None:
            try:
                last = int(os.pread(self._pid_fd, 32, 0) or b"-1")
            except (OSError, ValueError):
                last = -1
        try:
            os.write(self._window_fd,
                     b"%d %d %d\n" % (_boot_ns(), last, index))
        except OSError:
            pass

    def intact(self):
        return builtins.__import__ is getattr(self, "_import", None) \
            and importlib.import_module is getattr(self, "_import_module", None)

    # -- windows

    def _switch(self, window):
        self.windows.append(window)
        self._write_window(len(self.windows) - 1)

    def begin_run(self):
        """Discovery is over: what runs next belongs to tests."""
        self.gap = _Reach()
        self.sink = self.gap
        self._switch(["gap", None, None])

    def start(self, test):
        module = getattr(test.__class__, "__module__", None)
        if not isinstance(module, str) or not module:
            self.broken = self.broken or "a test's module is unreadable"
            return
        if self.broken is None and self.on and not self.intact():
            self.broken = ("the import wrappers were replaced before a test of "
                           "%s started" % module)
        self.current = module
        self.sink = self.reach.setdefault(module, _Reach())
        self._switch(["test", module, module])

    def stop(self, _test):
        self.sink = self.reach.setdefault(self.current, _Reach())
        self._switch(["gap", self.current, None])

    def _hook_fixtures(self):
        """Wrap unittest's fixture steps so what each runs is charged to the
        module it runs code of, for THIS runner's results only: a test that
        runs a suite of its own keeps its fixtures in its own window."""
        import unittest.suite
        suite = unittest.suite.TestSuite
        recorder = self
        for name, owner in _FIXTURES.items():
            original = suite.__dict__.get(name)
            if original is None:
                self.broken = self.broken or (
                    "this Python's unittest has no TestSuite.%s" % name)
                continue

            def wrapper(this, *args, _original=original, _owner=owner):
                if not recorder.on \
                        or not any(r is args[-1] for r in recorder._results):
                    return _original(this, *args)
                try:
                    module = _owner(*args)
                except Exception:               # noqa: BLE001 -- unittest's
                    module = None
                if not isinstance(module, str) or not module:
                    return _original(this, *args)
                return recorder._fixture(module, lambda: _original(this, *args))
            wrapper.__wrapped__ = original
            self._fixture_originals[name] = (original, wrapper)
            setattr(suite, name, wrapper)

    def _unhook_fixtures(self):
        import unittest.suite
        suite = unittest.suite.TestSuite
        for name, (original, wrapper) in self._fixture_originals.items():
            if suite.__dict__.get(name) is wrapper:
                setattr(suite, name, original)
        self._fixture_originals = {}

    def _fixture(self, module, call):
        """Run one fixture step in `module`'s window, then go back to the
        window it interrupted (a module fixture tears the previous module
        down inside itself)."""
        sink, back = self.sink, self.windows[-1] if self.windows else None
        self.sink = self.reach.setdefault(module, _Reach())
        self._switch(["fixture", module, module])
        try:
            return call()
        finally:
            self.sink = sink
            if back is not None:
                self._switch(list(back))

    def result_class(self, base):
        recorder = self

        class LoadsResult(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                recorder._results.append(self)

            def startTest(self, test):
                recorder.start(test)
                return super().startTest(test)

            def stopTest(self, test):
                outcome = super().stopTest(test)
                recorder.stop(test)
                return outcome

        LoadsResult.__name__ = "GateLoads%s" % base.__name__
        return LoadsResult

    def finish(self, planned=()):
        """Stop recording. -> the runner artifact."""
        self.on = False
        self._unhook_fixtures()
        if builtins.__import__ is getattr(self, "_import", None):
            builtins.__import__ = self._orig_import
        if importlib.import_module is getattr(self, "_import_module", None):
            importlib.import_module = self._orig_import_module
        for name in ("_window_fd", "_pid_fd"):
            if getattr(self, name) is not None:
                try:
                    os.close(getattr(self, name))
                except OSError:
                    pass
                setattr(self, name, None)
        return {
            "v": VERSION, "type": "runner", "pid": os.getpid(),
            "window_file": self.window_file,
            "windows": self.windows,
            "loads": {m: sorted(r.loads) for m, r in self.reach.items()},
            "reads": {m: sorted(r.reads) for m, r in self.reach.items()},
            "bodies": {p: sorted(r.loads) for p, r in self.bodies.items()},
            "body_reads": {p: sorted(r.reads) for p, r in self.bodies.items()
                           if r.reads},
            "planned": sorted(set(planned)),
            "broken": self.broken, "calls": self.calls,
        }


def write_json(directory, name, row):
    import json
    tmp = os.path.join(directory, ".%s.%d.tmp" % (name, os.getpid()))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(row, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, os.path.join(directory, name))


def arm_runner(root=None, environ=None):
    """Arm this process as a recording runner when the gate asked. ->
    Recorder or None. Pops RUNNER_ENV and sets WINDOW_ENV, both BEFORE any
    test can snapshot the environment."""
    environ = os.environ if environ is None else environ
    if environ.get(RUNNER_ENV) != "1":
        return None
    environ.pop(RUNNER_ENV, None)
    directory = environ.get(DIR_ENV) or ""
    root = root or environ.get(ROOT_ENV) or ""
    if not (os.path.isabs(directory) and os.path.isdir(directory)
            and os.path.isabs(root) and os.path.isdir(root)):
        return None
    window_file = os.path.join(directory, "window-%d" % os.getpid())
    environ[WINDOW_ENV] = window_file
    recorder = Recorder(root, directory, window_file).install()
    # A helm child state armed by helm/__init__ in THIS process (it ran
    # before the recorder existed) must not also write this runner's whole
    # sys.modules as one child record.
    package = sys.modules.get("helm")
    child = getattr(package, "_GATE_LOADS", None) if package else None
    if isinstance(child, dict):
        child["on"] = False
    return recorder


def git_dir(root):
    """The git directory of the checkout at `root` (a worktree's `.git` is
    a file naming it), or None."""
    dotgit = os.path.join(root, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    try:
        with open(dotgit, encoding="utf-8") as fh:
            line = fh.read(4096)
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    path = line[len("gitdir:"):].strip()
    return path if os.path.isabs(path) else os.path.normpath(
        os.path.join(root, path))


def marker_path(root):
    found = git_dir(root)
    return os.path.join(found, MARKER) if found else None


def _boot_ns():
    """The boot clock, which /proc/<pid>/stat's start time counts too."""
    import time
    return time.clock_gettime_ns(getattr(time, "CLOCK_BOOTTIME",
                                         time.CLOCK_MONOTONIC))


def _stat(pid):
    """(parent pid, start time in ns since boot) from /proc, or None."""
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            fields = fh.read().rpartition(")")[2].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return int(fields[1]), int(fields[19]) * 1000000000 // ticks
    except (OSError, ValueError, IndexError):
        return None


def _runner_window(directory):
    """The window log of the nearest ancestor that is a recording runner,
    or "" (Linux /proc; a process under no runner has none)."""
    pid = os.getppid()
    for _hop in range(64):
        if pid <= 1:
            return ""
        candidate = os.path.join(directory, "window-%d" % pid)
        if os.path.exists(candidate):
            return candidate
        row = _stat(pid)
        if row is None:
            return ""
        pid = row[0]
    return ""


def _spawned_at(runner):
    """When the runner's test spawned this process's line: this process's
    OLDEST ancestor that started after the runner (the one the runner
    spawned, or, for a daemon whose parent died and left it to init, the
    first of its line still alive). -> (its pid, its start time in boot ns,
    the clock's tick in ns) or None."""
    top = _stat(runner)
    if top is None:
        return None
    pid, found = os.getpid(), None
    for _hop in range(64):
        row = _stat(pid)
        if row is None or pid == runner or row[1] < top[1]:
            break
        found, pid = (pid, row[1]), row[0]
    if found is None:
        return None
    return found[0], found[1], 1000000000 // os.sysconf("SC_CLK_TCK")


def _windows_at(log, pid, when, slack):
    """The windows of the log that could have spawned process `pid`, which
    started at `when` give or take `slack` (the clock's tick): the switches
    inside that span and the last one before it, then, when their pid
    watermarks rise in order, only the last switch the pid came after (the
    kernel hands out pids in order, so a process forked after a switch has a
    larger pid than the watermark read at it). A watermark that is missing
    or falls (pids wrapped) keeps the whole span. The log is read from its
    end backwards, a tail at a time: a child reads it moments after its line
    was spawned."""
    try:
        fd = os.open(log, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        size = os.fstat(fd).st_size
        need = _TAIL
        while True:
            start = max(0, size - need)
            data = os.pread(fd, size - start, start)
            lines = data.split(b"\n")
            if start:
                lines = lines[1:]
            rows = []
            for line in lines:
                bits = line.split()
                if len(bits) == 3 and bits[0].isdigit() \
                        and all(b.lstrip(b"-").isdigit() for b in bits[1:]):
                    rows.append((int(bits[0]), int(bits[1]), int(bits[2])))
            if not rows:
                return []
            if rows[0][0] <= when - slack or start == 0:
                break
            need *= 4
    finally:
        os.close(fd)
    span = [row for row in rows if row[0] <= when - slack][-1:]
    span += [row for row in rows if when - slack < row[0] <= when + slack]
    marks = [last for _at, last, _index in span]
    if span and min(marks) >= 0 and marks == sorted(marks):
        after = [row for row in span if row[1] < pid]
        if after:
            span = after[-1:]
    return sorted(set(index for _at, _last, index in span if index >= 0))


def arm_child(environ=None, root=None):
    """The CHILD half, called by `helm/__init__` in any process that imports
    helm while DIR_ENV is set, or while its checkout carries the gate's
    MARKER (`root` is that checkout). -> state dict, or None when this
    process is not a child of a recording runner.

    At exit it writes the repository files of every module this process
    loaded plus the repository paths it opened or listed, under the windows
    its runner was in when it spawned this process's line (`_spawned_at`),
    never the window it is in when this process gets to importing helm: a
    test that does not wait for its child has moved on by then. A runner process
    itself (its window file carries its own pid) records in-process instead,
    and a state armed before the runner existed is switched off by
    `arm_runner`."""
    environ = os.environ if environ is None else environ
    directory = environ.get(DIR_ENV) or ""
    window_file = environ.get(WINDOW_ENV) or ""
    if directory:
        root = environ.get(ROOT_ENV) or ""
    elif root:
        import json
        try:
            with open(marker_path(root) or "", encoding="utf-8") as fh:
                directory = json.load(fh).get("dir") or ""
        except (OSError, ValueError, AttributeError):
            return None
        if not (os.path.isabs(directory) and os.path.isdir(directory)):
            return None
        window_file = _runner_window(directory)
        if not window_file:
            return None
    if not (os.path.isabs(directory) and os.path.isabs(root or "")) \
            or window_file.endswith("-%d" % os.getpid()):
        return None
    windows = []
    try:
        runner = int(window_file.rpartition("-")[2])
    except ValueError:
        runner = None
    spawned = _spawned_at(runner) if runner else None
    if spawned:
        windows = _windows_at(window_file, *spawned)
    roots = Roots(root)
    state = {"on": True, "reads": set()}

    def hook(event, args):
        if event not in _EVENTS or not state["on"]:
            return
        if event != "open" and _import_system_listing():
            return
        try:
            path = args[0] if args else "."
            if path is None:
                path = "."
            if isinstance(path, int):
                return
            rel = roots.rel(path, directory=event != "open")
            if rel:
                state["reads"].add(rel)
        except Exception:                       # noqa: BLE001 -- never break I/O
            pass

    def write():
        if not state["on"]:
            return
        state["on"] = False
        try:
            loads = set()
            for module in list(sys.modules.values()):
                path = getattr(module, "__file__", None)
                if isinstance(path, str):
                    rel = roots.rel(path)
                    if rel:
                        loads.add(rel)
            import time
            write_json(directory, "child-%d-%d.json" % (
                os.getpid(), time.monotonic_ns()), {
                    "v": VERSION, "type": "child",
                    "window_file": window_file, "windows": windows,
                    "loads": sorted(loads),
                    "reads": sorted(state["reads"] - loads)})
        except Exception:                       # noqa: BLE001 -- at exit
            pass

    import atexit
    atexit.register(write)
    sys.addaudithook(hook)
    return state


def write_runner(recorder, planned=()):
    """Finish and write the runner artifact. -> path or None; never raises."""
    try:
        row = recorder.finish(planned)
        name = "runner-%d.json" % os.getpid()
        write_json(recorder.directory, name, row)
        return os.path.join(recorder.directory, name)
    except Exception:                           # noqa: BLE001
        return None


# ---------------------------------------------------------------- merging


def read_artifacts(directory):
    """(runners, children, errors) from one recording run's directory."""
    import json
    runners, children, errors = [], [], []
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        return [], [], ["the loads directory is unreadable: %s" % exc]
    for name in names:
        if name.endswith(".jsonl") and not name.startswith("."):
            # A fork killed before its header landed wrote nothing it could
            # be charged with: it counts as one child no window was found
            # for, never as a broken run (forks are killed at will by tests).
            row = _read_fork(os.path.join(directory, name))
            children.append(row if row is not None else {
                "v": VERSION, "type": "child", "window_file": None,
                "windows": None, "loads": [], "reads": []})
            continue
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                row = json.load(fh)
        except (OSError, ValueError) as exc:
            errors.append("%s is unreadable: %s" % (name, exc))
            continue
        if not isinstance(row, dict) or row.get("v") != VERSION:
            errors.append("%s is not a version-%d artifact" % (name, VERSION))
        elif row.get("type") == "runner":
            runners.append(row)
        elif row.get("type") == "child":
            children.append(row)
        else:
            errors.append("%s has an unknown type" % name)
    return runners, children, errors


def _read_fork(path):
    """A forked process's file (`Recorder._forked`) as a child row, or None.
    A last line cut short by a kill is skipped, not an error: every line
    before it was written whole."""
    import json
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None
    try:
        head = json.loads(lines[0])
    except ValueError:
        return None
    if not isinstance(head, dict) or head.get("v") != VERSION \
            or head.get("type") != "fork":
        return None
    loads, reads = set(), set()
    for line in lines[1:]:
        try:
            tag, item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(item, str):
            (loads if tag == "l" else reads).add(item)
    return {"v": VERSION, "type": "child",
            "window_file": head.get("window_file"),
            "windows": head.get("windows"),
            "loads": sorted(loads), "reads": sorted(reads - loads)}


def _children_by_window(runners, children):
    """{test module: _Reach} from child records, through the windows the
    child's runner was in when it spawned the child's line. -> (that,
    the number of children no window could be found for)."""
    tables = {row.get("window_file"): row.get("windows") or []
              for row in runners}
    out, lost = {}, 0
    for child in children:
        table = tables.get(child.get("window_file"))
        indices = child.get("windows")
        loads, reads = child.get("loads"), child.get("reads")
        if table is None or not isinstance(indices, list) or not indices \
                or not all(type(i) is int and 0 <= i < len(table)
                           for i in indices) \
                or not isinstance(loads, list) or not isinstance(reads, list):
            lost += 1
            continue
        modules = set()
        for index in indices:
            _kind, previous, following = table[index]
            modules |= {previous, following} - {None}
        for module in modules:
            reach = out.setdefault(module, _Reach())
            reach.loads.update(p for p in loads if isinstance(p, str))
            reach.reads.update(p for p in reads if isinstance(p, str))
    return out, lost


def _scope_nodes(tree):
    """Every node that executes when the module body runs: function and
    lambda BODIES are skipped (they run when called); class bodies are not."""
    import ast
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(node.decorator_list)
            stack.extend(d for d in node.args.defaults + node.args.kw_defaults
                         if d is not None)
            continue
        if isinstance(node, ast.Lambda):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _modules_alias_names(tree):
    """(names bound to the `sys` module, names bound to `sys.modules`)."""
    import ast
    sys_names, modules_names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    sys_names.add(alias.asname or "sys")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys" \
                and not node.level:
            for alias in node.names:
                if alias.name == "modules":
                    modules_names.add(alias.asname or "modules")
    return sys_names, modules_names


def _is_sys_modules(node, sys_names, modules_names):
    import ast
    if isinstance(node, ast.Attribute) and node.attr == "modules" \
            and isinstance(node.value, ast.Name) \
            and node.value.id in sys_names:
        return True
    return isinstance(node, ast.Name) and node.id in modules_names


def _key_name(node, module, package):
    """The module a `sys.modules` key names, statically, or None."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id == "__name__":
        return module
    if isinstance(node, ast.Name) and node.id == "__package__":
        return package
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _key_name(node.left, module, package)
        right = _key_name(node.right, module, package)
        if left is not None and right is not None:
            return left + right
    return None


def module_reads(tree, module, package):
    """Every raw `sys.modules` READ in one parsed file. -> [(line, key name
    or None)]: a subscript that loads, and a `.get(...)` call."""
    import ast
    sys_names, modules_names = _modules_alias_names(tree)
    if not sys_names and not modules_names:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) \
                and isinstance(node.ctx, ast.Load) \
                and _is_sys_modules(node.value, sys_names, modules_names):
            out.append((node.lineno, _key_name(node.slice, module, package)))
        elif isinstance(node, ast.Call) \
                and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args \
                and _is_sys_modules(node.func.value, sys_names, modules_names):
            out.append((node.lineno,
                        _key_name(node.args[0], module, package)))
    return out


def _module_of(rel):
    if not rel.endswith(".py"):
        return None, False
    parts = rel[:-3].split("/")
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None, False
    return ".".join(parts), is_pkg


def static_edges(root, files, texts=None):
    """{path: set(paths)} -- what executing each module BODY reaches, read
    from source: its module-scope imports (resolved against this tree, each
    with the package `__init__` chain it executes), its statically named
    `sys.modules` reads (anywhere in the file: a conservative superset), and
    for every file the `__init__` of each package above it. -> (edges, err)

    `texts` maps a path to its source when the caller already holds it."""
    import ast
    names = {}
    for rel in files:
        name, _pkg = _module_of(rel)
        if name:
            names[name] = rel

    def chain(dotted):
        parts = dotted.split(".")
        return {names[".".join(parts[:i])] for i in range(1, len(parts) + 1)
                if ".".join(parts[:i]) in names}

    edges = {}
    for rel in files:
        found = set()
        # The NEAREST package `__init__` above: its own edge reaches the
        # next one up, so the closure still runs the whole chain.
        parent = rel.rpartition("/")[0]
        if rel.endswith("/__init__.py"):
            parent = parent.rpartition("/")[0]
        while parent:
            init = parent + "/__init__.py"
            if init in files:
                found.add(init)
                break
            parent = parent.rpartition("/")[0]
        name, is_pkg = _module_of(rel)
        if name:
            text = texts.get(rel) if texts else None
            if text is None:
                try:
                    with open(os.path.join(root, rel), encoding="utf-8") as fh:
                        text = fh.read()
                except (OSError, UnicodeDecodeError) as exc:
                    return None, "%s is unreadable: %s" % (rel, exc)
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError) as exc:
                return None, "%s does not parse: %s" % (rel, exc)
            package = name if is_pkg else name.rpartition(".")[0]
            pkg_parts = package.split(".") if package else []
            for node in _scope_nodes(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found |= chain(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        if node.level - 1 > len(pkg_parts):
                            continue
                        head = pkg_parts[:len(pkg_parts) - (node.level - 1)]
                    else:
                        head = []
                    target = head + (node.module.split(".")
                                     if node.module else [])
                    if not target:
                        continue
                    found |= chain(".".join(target))
                    for alias in node.names:
                        found |= chain(".".join(target + [alias.name]))
            for _line, key in module_reads(tree, name, package):
                if key:
                    found |= chain(key)
        found.discard(rel)
        edges[rel] = found
    return edges, None


def _reach(edges):
    """{node: int bitmask of every node it reaches, itself included} and the
    node order the bits index. Strongly connected components first (the
    module-scope graph is full of cycles), then one pass in the order Tarjan
    finishes them, which is reverse topological, so each component's reach is
    its own members OR its successors' reach, already computed."""
    nodes = sorted(set(edges) | {n for targets in edges.values()
                                 for n in targets})
    bit = {n: 1 << i for i, n in enumerate(nodes)}
    index, low, on, stack, reach = {}, {}, set(), [], {}
    counter = 0
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    top = stack.pop()
                    on.discard(top)
                    members.append(top)
                    if top == node:
                        break
                mask = 0
                for member in members:
                    mask |= bit[member]
                    for child in edges.get(member, ()):
                        if child in reach:
                            mask |= reach[child]
                for member in members:
                    reach[member] = mask
    return reach, nodes


def expand(compact):
    """{test module: sorted paths} from a compact record: each module's
    roots closed under the record's import edges, plus what every module in
    that closure READ, plus the module's own leaves."""
    compact = compact_of(compact)
    edges, reads = compact["edges"], compact["reads"]
    reach, nodes = _reach(edges)
    out = {}
    for module, roots in compact["roots"].items():
        mask = 0
        found = set()
        for path in roots:
            if path in reach:
                mask |= reach[path]
            else:
                found.add(path)
        found |= {n for i, n in enumerate(nodes) if mask >> i & 1}
        for node in list(found):
            found.update(reads.get(node, ()))
        found.update(compact["leaves"].get(module, ()))
        out[module] = sorted(found)
    return out


def compact_of(record):
    """The compact form of a record: a compact one completed with empty
    parts, or a plain {test module: paths} one as roots with no edges (its
    expansion is itself)."""
    if "roots" in record and "edges" in record:
        return {"edges": record["edges"], "reads": record.get("reads") or {},
                "roots": record["roots"],
                "leaves": record.get("leaves") or {}}
    return {"edges": {}, "reads": {},
            "roots": {m: sorted(set(p)) for m, p in record.items()},
            "leaves": {}}


def build_record(runners, children, root, files, texts=None):
    """One run's COMPACT record from its artifacts. -> (compact, stats,
    reason); `reason` is None only for a COMPLETE record.

    A test module is recorded from its windows (its own, the gaps beside it,
    its children) plus its own file and `tests/__init__.py`. What it LOADED
    is closed under module-body reach: the bodies the runners watched
    execute, and the module-scope imports read from source. What it READ is
    a leaf, and so is what each body read. The closed sets overlap so much
    and compress so badly (measured: 191 KB for one whole suite, three times
    a ledger event) that the record keeps the reach graph ONCE and, per test
    module, only the roots nothing else in its set already reaches;
    `expand` gives back the full sets exactly."""
    stats = {"runners": len(runners), "children": len(children)}
    broken = [row.get("broken") for row in runners if row.get("broken")]
    if not runners:
        return None, stats, "no runner wrote a loads record"
    if broken:
        return None, stats, "a runner's record is broken: %s" % broken[0]
    windows, bodies, body_reads = {}, {}, {}
    for row in runners:
        for module, paths in (row.get("loads") or {}).items():
            windows.setdefault(module, _Reach()).loads.update(paths)
        for module, paths in (row.get("reads") or {}).items():
            windows.setdefault(module, _Reach()).reads.update(paths)
        for path, reached in (row.get("bodies") or {}).items():
            bodies.setdefault(path, set()).update(reached)
        for path, reached in (row.get("body_reads") or {}).items():
            body_reads.setdefault(path, set()).update(reached)
    by_child, lost = _children_by_window(runners, children)
    stats["children_unattributed"] = lost
    for module, reach in by_child.items():
        if module in windows:
            windows[module].update(reach)
    edges, err = static_edges(root, files, texts)
    if err:
        return None, stats, "the module-scope import graph is unreadable: %s" \
            % err
    adjacency = {}
    for source in set(edges) | set(bodies):
        targets = (edges.get(source, set()) | bodies.get(source, set())) \
            - {source}
        if targets:
            adjacency[source] = targets
    reach, nodes = _reach(adjacency)
    count = {n: bin(reach[n]).count("1") for n in nodes}
    files = set(files)
    roots, leaves, used = {}, {}, 0
    for module, window in windows.items():
        start = set(window.loads) | {module_path(module)}
        if "tests/__init__.py" in files:
            start.add("tests/__init__.py")
        covered, kept = 0, []
        for path in sorted(start, key=lambda p: (-count.get(p, 1), p)):
            mask = reach.get(path)
            if mask is None:
                kept.append(path)
                continue
            if covered & mask == mask:
                continue
            kept.append(path)
            covered |= mask
        used |= covered
        roots[module] = sorted(kept)
        closed = {n for i, n in enumerate(nodes) if covered >> i & 1} \
            | set(kept)
        inherited = set()
        for node in closed:
            inherited |= body_reads.get(node, set())
        leaves[module] = sorted(set(window.reads) - closed - inherited)
    live = {n for i, n in enumerate(nodes) if used >> i & 1} \
        | {p for kept in roots.values() for p in kept}
    compact = {"edges": {src: sorted(dst) for src, dst in adjacency.items()
                         if src in live},
               "reads": {src: sorted(dst) for src, dst in body_reads.items()
                         if src in live and dst},
               "roots": roots,
               "leaves": {m: v for m, v in leaves.items() if v}}
    stats["tests"] = len(roots)
    stats["calls"] = sum(int(row.get("calls") or 0) for row in runners)
    stats["edges"] = sum(len(v) for v in compact["edges"].values())
    stats["read_edges"] = sum(len(v) for v in compact["reads"].values())
    stats["roots"] = sum(len(v) for v in roots.values())
    stats["leaves"] = sum(len(v) for v in compact["leaves"].values())
    return compact, stats, None


# ------------------------------------------------------ encode / digest


def canonical(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def digest(record):
    import hashlib
    return hashlib.sha256(canonical(record).encode(
        "utf-8", "surrogatepass")).hexdigest()


def encode(compact):
    """The compact record as text: paths once, edges and roots as indices,
    zlib, base64."""
    import base64
    import zlib
    compact = compact_of(compact)
    paths = sorted({p for part in ("edges", "reads")
                    for src, dst in compact[part].items()
                    for p in [src] + list(dst)}
                   | {p for part in ("roots", "leaves")
                      for v in compact[part].values() for p in v})
    index = {p: i for i, p in enumerate(paths)}

    def rows(graph):
        return sorted([index[src]] + sorted(index[d] for d in dst)
                      for src, dst in graph.items())

    def lists(table):
        return {m: sorted(index[p] for p in v) for m, v in table.items()}
    body = {"paths": paths, "edges": rows(compact["edges"]),
            "reads": rows(compact["reads"]), "roots": lists(compact["roots"]),
            "leaves": lists(compact["leaves"])}
    raw = canonical(body).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode(payload):
    """-> the compact record, or raise ValueError."""
    import base64
    import json
    import zlib
    try:
        raw = zlib.decompress(base64.b64decode(payload.encode("ascii"),
                                               validate=True))
        body = json.loads(raw.decode("utf-8"))
        paths = body["paths"]
        graphs = {part: {paths[row[0]]: sorted(paths[i] for i in row[1:])
                         for row in body[part]}
                  for part in ("edges", "reads")}
        tables = {part: {m: sorted(paths[i] for i in v)
                         for m, v in body[part].items()}
                  for part in ("roots", "leaves")}
    except (AttributeError, KeyError, IndexError, TypeError, ValueError,
            zlib.error, UnicodeError) as exc:
        raise ValueError("loads payload does not decode: %s" % exc)
    return dict(graphs, **tables)


def event_id(row):
    import hashlib
    payload = {k: row.get(k) for k in (
        "v", "event", "receipt", "tree", "head", "kind", "tests", "paths",
        "digest", "encoding", "payload", "seconds")}
    return hashlib.sha256(canonical(payload).encode(
        "utf-8", "surrogatepass")).hexdigest()[:ID_LEN]


def _canonical_compact(compact):
    compact = compact_of(compact)
    graph = {part: {s: sorted(set(d)) for s, d in compact[part].items() if d}
             for part in ("edges", "reads")}
    return dict(graph, roots={m: sorted(set(v))
                              for m, v in compact["roots"].items()},
                leaves={m: sorted(set(v))
                        for m, v in compact["leaves"].items() if v})


def event(receipt, head, tree, kind, record, seconds):
    """The sibling event carrying one run's record (compact, or a plain
    {test module: paths} one). The digest is over the compact form as a
    reader DECODES it."""
    compact = _canonical_compact(record)
    row = {"v": VERSION, "event": EVENT, "receipt": receipt, "tree": tree,
           "head": head, "kind": kind, "tests": len(compact["roots"]),
           "paths": len({p for v in expand(compact).values() for p in v}),
           "digest": digest(compact), "encoding": ENCODING,
           "payload": encode(compact), "seconds": round(float(seconds), 3)}
    row["id"] = event_id(row)
    return row


def event_error(row):
    """Why this row is not a readable loads record, or None."""
    fields = {"v", "event", "receipt", "tree", "head", "kind", "tests",
              "paths", "digest", "encoding", "payload", "seconds", "id"}
    if not isinstance(row, dict) or set(row) != fields \
            or row.get("v") != VERSION or row.get("event") != EVENT:
        return "loads record schema is unreadable"
    import re
    for name in ("tree", "head"):
        value = row.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}",
                                                          value):
            return "loads record %s is not a full sha" % name
    if row.get("encoding") != ENCODING:
        return "loads record encoding is unknown"
    if row.get("id") != event_id(row):
        return "loads record content id does not match"
    return None


def read_event(row):
    """-> (record, err): the decoded record, its digest and counts checked."""
    err = event_error(row)
    if err:
        return None, err
    try:
        compact = decode(row["payload"])
    except ValueError as exc:
        return None, str(exc)
    if digest(compact) != row["digest"]:
        return None, "loads record digest does not match its payload"
    if len(compact["roots"]) != row["tests"]:
        return None, "loads record test count does not match its payload"
    return expand(compact), None


# -------------------------------------------------------------- selection


def names_path(text, path):
    """Does this source NAME the repo path: the path itself, or its last
    component as a whole quoted string literal (gate._names_path's rule)."""
    base = path.rsplit("/", 1)[-1]
    return path in text or ('"%s"' % base) in text or ("'%s'" % base) in text


def select(record, universe, changed, structural, text_of, audits):
    """THE ONE v3 SELECTION RULE, asked by the planner and by the binder over
    their own inputs. -> sorted test modules.

    `record` {test module: paths reached at T0}; `universe` the test modules
    at the tip; `changed` every path that differs T0 -> tip; `structural` the
    changed paths that were added or deleted (a directory listing sees only
    those); `text_of(module)` the tip source of a test module; `audits` the
    tree-wide audit modules (always selected).

    A test module is selected when: its record meets a changed path, or lists
    the directory of an added or deleted one; it has no record, or its own
    file changed; it names a changed path; it is an audit."""
    changed = set(changed)
    parents = {(p.rpartition("/")[0] + "/") if "/" in p else ROOT_DIR
               for p in structural}
    touched = changed | parents
    out = set(m for m in audits if m in universe)
    for module in universe:
        if module in out:
            continue
        reached = record.get(module)
        if reached is None or module_path(module) in changed \
                or not touched.isdisjoint(reached):
            out.add(module)
            continue
        text = text_of(module)
        if text is None or any(names_path(text, p) for p in changed):
            out.add(module)
    return sorted(out)


# ---------------------------------------------------------------- census


#: Raw `sys.modules` reads whose key no reader can resolve, each with why the
#: recorder does not need to see it. Keyed by (path, the stripped source
#: line), so a moved line keeps its declaration and a changed one loses it.
DECLARED_RAW_READS = {
    ("helm/gateloads.py", "package = sys.modules.get(absname)"): (
        "the recorder itself, reading the package the import system just "
        "loaded to apply the fromlist's own rule"),
    ("helm/gateloads.py", "module = sys.modules.get(name)"): (
        "the recorder itself, mapping a name the import system just resolved "
        "to that module's file"),
    ("helm/gateloads.py",
     'path = getattr(sys.modules.get(name), "__file__", None)'): (
        "the recorder itself, charging a module body it just watched execute "
        "to that module's file"),
    ("tests/test_web_split_contract.py",
     "self.assertIs(value, sys.modules[value.__name__])"): (
        "an identity check: the object compared is a module this test file "
        "already imported, never code the read reaches"),
    ("helm/gateslice.py", "module = sys.modules.get(label)"): (
        "the discovery check reads the test module discovery just imported: "
        "the unit's own module, which is its own record"),
    ("helm/seats.py", 'mod = _sys.modules.get("%s.%s" % (_pkg, stem))'): (
        "the seats facade fans a patch out to the siblings already loaded; "
        "every one is a module-scope import of helm/seats.py, so the "
        "static closure carries it"),
}


def raw_reads(rel, text):
    """The raw `sys.modules` reads in one file the recorder cannot see and
    no declaration covers. -> [(line, source)]; a self-read
    (`sys.modules[__name__]`), a read of a module outside the tree
    (`"__main__"`, `"site"`) and a statically named repository module (the
    static closure carries it as an edge) are all visible, so none is
    returned."""
    import ast
    # Only a file that spells a read can hold one: `modules[`, `.get(` on
    # it, or any import from sys (which may bind `modules` by name).
    if "modules[" not in text and "modules.get(" not in text \
            and "from sys import" not in text:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    name, is_pkg = _module_of(rel)
    package = (name if is_pkg else (name or "").rpartition(".")[0]) or None
    lines = text.splitlines()
    out = []
    for line, key in module_reads(tree, name, package):
        if key is not None:
            continue
        source = lines[line - 1].strip() if line <= len(lines) else ""
        if (rel, source) in DECLARED_RAW_READS:
            continue
        out.append((line, source))
    return out
