#!/usr/bin/env python3
"""Run the serial discovery in parallel slices of one tree.

WHY A SECOND RUNNER. gateshard runs each module in a fresh interpreter that
imports only that module. Serial `python -m unittest discover -s tests -t .`
imports EVERY test module before the first test runs, so an import-time side
effect of any module is visible to all of them. A fresh worker cannot
reproduce that state, which is why sharded receipts were withdrawn.

This runner reproduces it by construction. Every worker performs the SAME
discovery the serial command performs, on the untouched default loader, with
the same start directory, pattern and top level, so each worker holds the
identical process state serial discovery builds. Workers then run discovery
units (one per module, in discovery order) under the TextTestRunner settings
`unittest.main` uses. Which units a worker runs is either claimed from a
shared locked counter or fixed in advance by longest-recorded-first
scheduling; either way a worker runs its units in ascending order, so the
units it has run before unit N are a subset of the units serial runs before
N, in serial order.

WHAT MAKES A RESULT READABLE AT ALL:
  * every worker's complete ordered test-id inventory has one digest, so the
    collected tests are exactly the serial discovery's, duplicates included;
  * the units line up with the module list the parent derived without
    importing anything (`discovery_modules`);
  * every unit ran exactly once and every worker's own unittest footer
    agrees with its recorded counts;
  * nothing the run started outlives it: the parent is the subreaper for
    every worker and sweeps what a dead worker leaves behind.
Anything else prints no terminal footer, which `gate.parse_result` reads as
UNKNOWN, never as a verdict.

WHAT IT DOES NOT REPRODUCE, named so nobody infers it: run-time state left by a
unit that ran in ANOTHER worker. The leak audit (HELM_GATESLICE_LEAKS = report
| fail | off, default report) watches the process-wide channels a test can
leave behind -- environment, cwd, umask, sys.path, builtins, signal handlers,
threads, warning and logging configuration, default socket timeout,
tempfile.tempdir, standard streams, replaced sys.modules entries, and any
callable or live test double rebound in a `helm.*` or `tests.*` module -- and
the data audit below watches the module DATA a unit leaves: every global and
class attribute of those modules re-bound, added, removed or changed in
content. In `fail` mode each leaking unit is one more ERROR. A cache that is
process-wide by design is named, with its reason, by the module that owns it
(`_GATESLICE_MUTABLE`); nothing else is exempt. `--serial MODULE...` runs the
same audit over named modules in one process, as a diagnostic.

On a readable result the run's evidence (counts, digests, schedule, leak
census, per-module seconds) is written where HELM_GATESLICE_EVIDENCE names;
the gate binds it into a receipt. The runner is a script, like gateshard, so
no `helm` import precedes the suite's own bootstrap in `tests/__init__.py`.
"""
import collections
import fcntl
import fnmatch
import hashlib
import importlib.util
import inspect
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import types
import unittest


_SHARD = None

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_SHARD": (
        "gateshard loaded by path once; loading it again yields the same "
        "code"),
}
_MISSING = object()
_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP", "SIGCHLD", "SIGALRM", "SIGUSR1",
            "SIGUSR2", "SIGPIPE")
LEAK_MODES = ("report", "fail", "off")
# THE OUTPUT SAYS WHAT IT IS. The parent's first stderr line, whatever wrapped
# or launched it (a shell, runpy, a joined -m): the gate reads the OUTPUT and
# refuses to mint a suite receipt from it except through its own sliced door,
# which is the one place this runner's evidence is bound and re-derived.
DIAGNOSTIC_MARKER = "HELM-DIAGNOSTIC-RUNNER gateslice"
# One progress record per worker: "<phase> <module>", padded to a fixed width
# so each write replaces the last in place. Its mtime is the last moment the
# worker moved.
_PROGRESS_WIDTH = 256
# The launch path's last two components. The gate's host census reads this to
# count a running parent as one whole-suite occupant; workers are its children.
SCRIPT = ("helm", "gateslice.py")
DEFAULT_WORKERS = 16
_EXIT_INVENTORY = 3
_EXIT_CRASH = 70


def _shard():
    """gateshard, loaded by path: its footer grammar, env scrub and reaper."""
    global _SHARD
    if _SHARD is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "gateshard.py")
        spec = importlib.util.spec_from_file_location(
            "_helm_gateshard_for_slices", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SHARD = module
    return _SHARD


def scrubbed_env(base):
    """gateshard's scrub over `base`, from the copy this runner loads."""
    return _shard().scrubbed_env(base)


# The parent's own contract keys. A worker reads LEAKS and WORKERS; it never
# reads these, and a test that starts a nested runner with dict(os.environ)
# must not hand that runner the outer run's evidence, summary or timings.
PARENT_ONLY_ENV = ("HELM_GATESLICE_EVIDENCE", "HELM_GATESLICE_KEEP",
                   "HELM_GATESLICE_TIMINGS")


def leak_mode():
    mode = os.environ.get("HELM_GATESLICE_LEAKS", "report")
    return mode if mode in LEAK_MODES else "report"


def worker_count(units, suite_capacity=None):
    """Workers for one run: the node's per-suite share, capped.

    A worker pays one full discovery (about ten CPU-seconds and one GiB on the
    current suite), so past the point where the longest unit bounds the wall,
    more workers only add discovery. HELM_GATESLICE_WORKERS overrides the cap.
    """
    shard = _shard()
    capacity = shard._suite_capacity() if suite_capacity is None \
        else max(1, suite_capacity)
    try:
        cap = int(os.environ.get("HELM_GATESLICE_WORKERS", ""))
    except ValueError:
        cap = DEFAULT_WORKERS
    share = max(1, shard._available_cpus() // capacity)
    return max(1, min(max(1, cap), share, max(1, units)))


def _leaves(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _leaves(test)
        else:
            yield test


START_DIR, PATTERN = "tests", "test*.py"


def discovery_modules(files, start=START_DIR, pattern=PATTERN):
    """Ordered module names `discover -s tests -t .` loads, from a listing.

    `files` is every regular file under the repository as a relative posix
    path. The walk is unittest's own (`TestLoader._find_tests` and
    `_find_test_path` for a top level of `.`): the start package first, then
    its sorted entries, where a file loads when its name is a valid module
    name matching the pattern and a directory loads and recurses when it
    holds `__init__.py`. Nothing is imported, so the same function answers
    over a working tree at mint and over a git tree at bind.

    A package that defines `load_tests` stops unittest's recursion, and no
    listing can see that; a walk that disagrees with real discovery is caught
    downstream, because the unit count or a unit's tests then disagree with
    these names and the run is refused.
    """
    files = set(files)
    entries = {}
    for path in files:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            entries.setdefault("/".join(parts[:depth]), set()).add(parts[depth])
    valid = unittest.loader.VALID_MODULE_NAME
    names, loading = [], set()

    def dotted(path):
        return path[:-3].replace("/", ".") if path.endswith(".py") \
            else path.replace("/", ".")

    def find_test_path(path):
        base = path.rsplit("/", 1)[-1]
        if path in files:
            if not valid.match(base) or not fnmatch.fnmatch(base, pattern):
                return None, False
            return dotted(path), False
        if path in entries:
            if path + "/__init__.py" not in files:
                return None, False
            return dotted(path), True
        return None, False

    def find_tests(path):
        name = dotted(path)
        if name not in loading:
            found, recurse = find_test_path(path)
            if found is not None:
                names.append(found)
            if not recurse:
                return
        for entry in sorted(entries.get(path, ())):
            full = path + "/" + entry
            found, recurse = find_test_path(full)
            if found is not None:
                names.append(found)
            if recurse:
                child = dotted(full)
                loading.add(child)
                try:
                    find_tests(full)
                finally:
                    loading.discard(child)

    find_tests(start)
    return names


def working_tree_files(root=".", start=START_DIR):
    """Every regular file under `start`, as discovery's `isfile` sees it."""
    out = []
    for here, dirs, names in os.walk(os.path.join(root, start)):
        dirs.sort()
        for name in names:
            path = os.path.join(here, name)
            if os.path.isfile(path):
                out.append(os.path.relpath(path, root).replace(os.sep, "/"))
    return out


def discover(labels):
    """Units exactly as `unittest discover -s tests -t .` builds them, paired
    with the module each came from. -> units, or raise ValueError.

    THE LOADER IS NOT TOUCHED. Discovery runs on `unittest.defaultTestLoader`,
    the instance TestProgram uses, exactly as the serial command runs it: a
    test module that inspects the loader while it is imported sees what it
    sees under serial. The module names come from `discovery_modules`, which
    the parent computed without importing anything, and every unit is checked
    against its name: each of its tests either carries the name in its id or
    belongs to a class the named module binds. A misaligned pairing raises.
    """
    top = unittest.defaultTestLoader.discover(
        START_DIR, pattern=PATTERN, top_level_dir=".")
    units = list(top)
    if len(units) != len(labels):
        raise ValueError("discovery produced %d units for %d modules"
                         % (len(units), len(labels)))
    for unit, label in zip(units, labels):
        leaves = list(_leaves(unit)) if isinstance(unit, unittest.TestSuite) \
            else [unit]
        module = sys.modules.get(label)
        bound = {id(value) for value in vars(module).values()} \
            if module is not None else set()
        stray = [test.id() for test in leaves
                 if label not in test.id() and id(type(test)) not in bound]
        if stray:
            raise ValueError("unit %s holds tests it does not export: %s"
                             % (label, ", ".join(stray[:3])))
    return units


def inventory(units, labels):
    """Ordered (label, [test ids]) per unit, and one digest over all of it.

    Length-prefixed JSON of the whole ordered structure: two inventories share
    a digest only if every unit carries the same ids in the same order with
    the same multiplicity.
    """
    rows = [[label, [test.id() for test in _leaves(unit)]
             if isinstance(unit, unittest.TestSuite) else [unit.id()]]
            for unit, label in zip(units, labels)]
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()
    return rows, digest


def publish_reference(work_dir, index, digest, rows):
    """Publish this worker's inventory as THE reference, or agree with the
    one already published. -> True when the digests agree.

    THE REFERENCE APPEARS WHOLE OR NOT AT ALL. Created in place, the name is
    visible while its first writer is still writing megabytes, and a second
    worker reads it half-written. A private file linked into place is
    complete before any name reaches it, and the link refuses if another
    worker published first.
    """
    reference = os.path.join(work_dir, "inventory.json")
    private = os.path.join(work_dir, "inventory.%d.tmp" % index)
    with open(private, "w", encoding="utf-8") as fh:
        json.dump({"digest": digest, "rows": rows}, fh)
    try:
        os.link(private, reference)
        return True
    except FileExistsError:
        with open(reference, encoding="utf-8") as fh:
            return json.load(fh)["digest"] == digest
    finally:
        os.unlink(private)


def _claim(path):
    """The next unit index, atomically across every worker. -> int"""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.pread(fd, 32, 0).decode("ascii").strip()
        index = int(raw or "0")
        os.pwrite(fd, b"%020d" % (index + 1), 0)
        return index
    finally:
        os.close(fd)


def _is_mock(value):
    """A live test double. A patcher object (`mock.patch(...)`) kept in a
    module global after `.stop()` is inert and is not one."""
    import unittest.mock        # only the leak audit needs it; importers of the
    try:                        # runner's SCRIPT path should not pay for it
        return isinstance(value, unittest.mock.NonCallableMock)
    except Exception:           # noqa: BLE001 -- a hostile __class__
        return False


def _is_code(value):
    try:
        return inspect.isroutine(value) or inspect.isclass(value) \
            or inspect.ismodule(value) or _is_mock(value)
    except Exception:           # noqa: BLE001 -- a hostile __class__
        return False


def _owned(name):
    return name in ("helm", "tests") or name.startswith(("helm.", "tests."))


def snapshot():
    """The process-wide state one unit can leave for the next. Objects, not
    ids, are kept so an identity comparison cannot be fooled by id reuse."""
    import builtins
    import logging
    import signal
    import socket
    import threading
    import warnings
    mask = os.umask(0)
    os.umask(mask)
    seen = set()
    path = [p for p in sys.path if not (p in seen or seen.add(p))]
    code = {}
    for name, module in list(sys.modules.items()):
        if module is None or not _owned(name):
            continue
        try:
            items = list(vars(module).items())
        except TypeError:
            continue
        code[name] = (module, {key: value for key, value in items
                               if not key.startswith("__")
                               and _is_code(value)})
    return {
        "environ": dict(os.environ),
        "cwd": os.getcwd(),
        "umask": mask,
        "sys.path": path,
        "builtins": dict(vars(builtins)),
        "signals": {name: signal.getsignal(getattr(signal, name))
                    for name in _SIGNALS if hasattr(signal, name)},
        "threads": set(threading.enumerate()),
        "warnings": list(warnings.filters),
        "logging": (logging.root.level, list(logging.root.handlers)),
        "socket timeout": socket.getdefaulttimeout(),
        "tempfile.tempdir": tempfile.tempdir,
        "streams": (sys.stdin, sys.stdout, sys.stderr),
        "sys.modules": dict(sys.modules),
        "code": code,
    }


def leaks(before, after):
    """[finding] for state `after` holds that `before` did not. -> list[str]"""
    found = []
    for key in ("cwd", "umask", "sys.path", "warnings", "logging",
                "socket timeout", "tempfile.tempdir"):
        if before[key] != after[key]:
            found.append("%s changed" % key)
    env = sorted(k for k in set(before["environ"]) | set(after["environ"])
                 if before["environ"].get(k) != after["environ"].get(k))
    if env:
        found.append("environ: %s" % ", ".join(env))
    names = sorted(k for k in set(before["builtins"]) | set(after["builtins"])
                   if before["builtins"].get(k, _MISSING)
                   is not after["builtins"].get(k, _MISSING))
    if names:
        found.append("builtins: %s" % ", ".join(names))
    signals = sorted(k for k in before["signals"]
                     if before["signals"][k] is not after["signals"].get(k))
    if signals:
        found.append("signal handlers: %s" % ", ".join(signals))
    threads = sorted(t.name for t in after["threads"] - before["threads"]
                     if t.is_alive())
    if threads:
        found.append("threads alive: %s" % ", ".join(threads))
    if any(a is not b for a, b in zip(before["streams"], after["streams"])):
        found.append("standard streams replaced")
    swapped = sorted(k for k, v in before["sys.modules"].items()
                     if after["sys.modules"].get(k, _MISSING) is not v)
    if swapped:
        found.append("sys.modules replaced or removed: %s"
                     % ", ".join(swapped[:8]))
    for name, (module, code) in before["code"].items():
        if after["sys.modules"].get(name) is not module:
            continue
        current = vars(module)
        rebound = sorted(k for k, v in code.items()
                         if current.get(k, _MISSING) is not v)
        rebound += sorted(k for k, v in current.items()
                          if k not in code and not k.startswith("__")
                          and _is_mock(v))
        if rebound:
            found.append("%s rebound: %s" % (name, ", ".join(rebound[:8])))
    return found


# ---------------------------------------------------------------------------
# THE DATA AUDIT (task/3039). The process audit above watches channels that
# are not module data, so a unit that leaves DATA in a module object another
# unit reads was invisible to it. A review measured the consequence on a
# clean two-module tree: test_a sets tests._shared.VALUE = 1, test_b expects
# 0; serial FAILED, a two-worker sliced run came back OK with no leak, and
# bind answered VERIFIED. Serial and slices disagree only when a unit leaves
# module-level state that a later unit reads, and that is a test-isolation bug
# whichever runner is used, so the cure is to see every such mutation.
#
# WHAT IS FINGERPRINTED, around every unit, for every loaded helm.* and tests.*
# module: each global name and the object bound to it; for a mutable container
# (dict, list, set, deque, bytearray) and for the attribute dict of an object
# whose class a helm or tests module defines, a content fingerprint -- the
# length and a hash over at most _CONTENT_ITEMS entries; and the same for
# every attribute a class the module defines holds in its own __dict__. An
# entry is hashed by identity, except a builtin scalar (str, int, ...), which
# is hashed by value: hashing or comparing any other value runs its code, and
# a fingerprint must not. An immutable value re-bound to an EQUAL immutable
# value is the same state, so it is not a change; nor is a class attribute
# set to, or removed back to, the value its bases already give it.
#
# WHAT IS A MUTATION: a global or class attribute re-bound, added or removed,
# or a container whose content fingerprint changed, between the start of a
# unit and the end of its module teardown. It is named by the unit, the owning
# module and the attribute.
#
# WHAT IS NOT, each for a reason the census below keeps beside the record:
# a submodule appearing on its package (a lazy import binds it there, and the
# process audit owns sys.modules); a function or class re-bound to another
# function or class at module level (the process audit already reports it as
# `rebound`); the attributes unittest itself writes on a TestCase class while
# it runs class fixtures; a change to the unit's OWN test module that no other
# module can reach (no other module holds that module, a class or function it
# defines, or the very container that changed), and, even when one can, an
# attribute the unit's own TestCase class gains or re-binds, which its
# setUpClass makes again whenever the class runs; and a name a module
# declares in _GATESLICE_MUTABLE, a dict of name -> the one-line reason that
# state is process-wide by design ("Class.attr" for a class attribute). The
# declaration lives beside the state, so a reviewer reading the cache reads
# its exemption; there is no wildcard, and it covers the declared OBJECT
# wherever it is held (a re-export, a facade's fan-out). One object held by
# several modules is one finding, named at its first holder.
# ---------------------------------------------------------------------------
MUTABLE_DECLARATION = "_GATESLICE_MUTABLE"
_CONTENT_ITEMS = 4096
_IMMUTABLE = (int, float, complex, str, bytes, bool, type(None), tuple,
              frozenset, range)
# Entries of these exact types are fingerprinted by VALUE, not identity: an
# equal string or number put back in a list is the same content, and hashing
# them runs no code a test could plant.
_SCALARS = (int, float, complex, str, bytes, bool, type(None))


def _token(value):
    kind = type(value)
    return (kind, value) if any(kind is t for t in _SCALARS) else id(value)


def _content(value):
    """(kind, length, identity hash) of a container or helm-owned object's
    attributes, or None when the value has no content this audit reads.

    Every read goes through the BASE type's own slot (dict.items, list
    iteration, ...), so a subclass or a spoofed __class__ runs no code here,
    and every walk is a C-level slice, so another thread cannot change the
    container half-way through one."""
    kind = type(value)
    try:
        if issubclass(kind, types.ModuleType):
            return None             # a module's globals are audited as its own
        if issubclass(kind, dict):
            items = list(itertools.islice(dict.items(value), _CONTENT_ITEMS))
            return ("dict", dict.__len__(value), hash(frozenset(
                (_token(k), _token(v)) for k, v in items)))
        if issubclass(kind, (list, collections.deque)):
            base = list if issubclass(kind, list) else collections.deque
            items = list(itertools.islice(base.__iter__(value),
                                          _CONTENT_ITEMS))
            return ("list", base.__len__(value),
                    hash(tuple(map(_token, items))))
        if issubclass(kind, set):
            items = list(itertools.islice(set.__iter__(value), _CONTENT_ITEMS))
            return ("set", set.__len__(value),
                    hash(frozenset(map(_token, items))))
        if issubclass(kind, bytearray):
            return ("bytes", bytearray.__len__(value), hash(bytes(
                bytearray.__getitem__(value, slice(0, _CONTENT_ITEMS * 16)))))
        if issubclass(kind, types.SimpleNamespace) or (
                _owned(getattr(kind, "__module__", "") or "")
                and not issubclass(kind, BaseException)):
            try:
                attrs = object.__getattribute__(value, "__dict__")
            except AttributeError:
                return None
            if type(attrs) is not dict:
                return None
            items = list(itertools.islice(dict.items(attrs), _CONTENT_ITEMS))
            return ("object", dict.__len__(attrs), hash(frozenset(
                (_token(k), _token(v)) for k, v in items)))
    except Exception:           # noqa: BLE001 -- hostile or half-built state
        return ("unreadable", 0, 0)
    return None


# The slots `type` itself defines, read through their descriptors, so a
# metaclass that overrides __dict__, __qualname__ or __name__ (tests/test_hookrun
# plants one whose __name__ raises) runs none of its own code here.
_CLASS_DICT = type.__dict__["__dict__"]
_CLASS_QUALNAME = type.__dict__["__qualname__"]
_CLASS_MODULE = type.__dict__["__module__"]
_CLASS_MRO = type.__dict__["__mro__"]


def _class_items(cls):
    """[(attr, value)] of a class's OWN __dict__, dunders dropped."""
    return [(k, v) for k, v in list(_CLASS_DICT.__get__(cls, type(cls))
                                    .items())
            if not str(k).startswith("__")]


def _defined_classes(name, items):
    """[(qualname, class)] for the classes module `name` defines itself."""
    out = []
    for _key, value in items:
        try:
            if not issubclass(type(value), type):
                continue
            own = _CLASS_DICT.__get__(value, type(value))
            qual = _CLASS_QUALNAME.__get__(value, type(value))
        except Exception:           # noqa: BLE001 -- a hostile metaclass
            continue
        if own.get("__module__") == name and isinstance(qual, str) \
                and "<locals>" not in qual:
            out.append((qual, value))
    return out


def data_snapshot(modules=None):
    """{module name: (module, {name: (value, content)},
    {class qualname: (class, {attr: (value, content)})})} for every loaded
    helm.* and tests.* module (of `modules`, default sys.modules). Objects are
    kept, not ids, so a re-binding is judged by identity and cannot be fooled
    by id reuse."""
    out = {}
    for name, module in list((sys.modules if modules is None
                              else modules).items()):
        if module is None or not _owned(name):
            continue
        try:
            items = list(vars(module).items())
        except TypeError:
            continue
        items = [(k, v) for k, v in items if not str(k).startswith("__")]
        classes = {}
        for qual, cls in _defined_classes(name, items):
            if qual in classes:
                continue
            try:
                attrs = _class_items(cls)
            except Exception:       # noqa: BLE001 -- a hostile metaclass
                continue
            classes[qual] = (cls, {k: (v, _content(v)) for k, v in attrs})
        out[name] = (module, {k: (v, _content(v)) for k, v in items},
                     classes)
    return out


def _same(before, after):
    """Is `after` the state `before` was? Identity, or an equal immutable."""
    if before is after:
        return True
    kind = type(before)
    return kind is type(after) and any(kind is t for t in _IMMUTABLE) \
        and _equal(before, after)


def _equal(a, b):
    try:
        return bool(a == b)
    except Exception:           # noqa: BLE001 -- a tuple holding a hostile item
        return False


def _type_name(value):
    kind = type(value)
    try:
        return "%s.%s" % (_CLASS_MODULE.__get__(kind, type(kind)),
                          _CLASS_QUALNAME.__get__(kind, type(kind)))
    except Exception:               # noqa: BLE001 -- a hostile metaclass
        return "?"


def _declared(module):
    """{attribute: reason} the module declares process-wide by design, or {}.

    Anything but a dict of non-empty one-line strings declares nothing, so a
    malformed declaration reports every mutation instead of hiding one."""
    try:
        declared = vars(module).get(MUTABLE_DECLARATION)
    except TypeError:
        return {}
    if type(declared) is not dict or not all(
            type(k) is str and type(v) is str and v.strip() and "\n" not in v
            for k, v in declared.items()):
        return {}
    return declared


def _inherited(cls, attr):
    """What `cls.attr` resolves to through its bases alone, or _MISSING."""
    try:
        for base in _CLASS_MRO.__get__(cls, type(cls))[1:]:
            own = _CLASS_DICT.__get__(base, type(base))
            if attr in own:
                return own[attr]
    except Exception:               # noqa: BLE001 -- a hostile metaclass
        pass
    return _MISSING


def _changes(before, after, cls=None):
    """[(attr, change, old, new)] between two {attr: (value, content)}.

    For a class, an attribute added with the value its bases already gave it,
    or removed so that its bases give the value it held, changes nothing a
    reader of `cls.attr` can see (a `real = C.x; C.x = v; C.x = real` restore
    of an inherited attribute leaves exactly that behind)."""
    out = []
    for key, (old, content) in before.items():
        if key not in after:
            if cls is None or not _same(old, _inherited(cls, key)):
                out.append((key, "removed", old, _MISSING))
            continue
        new, now = after[key]
        if not _same(old, new):
            out.append((key, "rebound", old, new))
        elif old is new and content != now:
            out.append((key, "content", old, new))
    for key, (new, _content_now) in after.items():
        if key not in before and (cls is None or not _same(
                _inherited(cls, key), new)):
            out.append((key, "added", _MISSING, new))
    return out


# What unittest itself writes on a TestCase class while running its fixtures
# (TestCase.doClassCleanups and TestSuite._handleClassSetUp): every class that
# runs carries them, so they are the runner's state, never a test's.
_UNITTEST_CLASS_STATE = frozenset(("tearDown_exceptions", "_classSetupFailed",
                                   "_class_cleanups"))


def _reached(snapshot):
    """({module name: {OTHER owned modules holding it, or a class or function
    it defines}}, {id(container): {modules holding that container}}).

    A package's binding of its own submodule is the import system's, not a
    reader's, so it does not count. The second map answers `from tests.test_a
    import CALLS`: the reader then holds the list itself, not its module."""
    out, held = {}, {}
    for holder, (_module, glob, _classes) in snapshot.items():
        for value, content in glob.values():
            if content is not None:
                held.setdefault(id(value), set()).add(holder)
            kind = type(value)
            try:
                if issubclass(kind, types.ModuleType):
                    target = vars(value).get("__name__")
                elif issubclass(kind, type):
                    target = _CLASS_DICT.__get__(value, kind).get("__module__")
                elif kind is types.FunctionType:
                    target = value.__module__
                else:
                    continue
            except Exception:       # noqa: BLE001 -- a hostile object
                continue
            if isinstance(target, str) and target != holder \
                    and not target.startswith(holder + "."):
                out.setdefault(target, set()).add(holder)
    return out, held


def _declared_objects(snapshots):
    """{id(object): "module.attr: reason"} for every non-scalar object a
    declared name holds in any of `snapshots`, so a re-export of the same
    object (`from ._common import _CACHE`, a facade's fan-out) inherits the
    declaration its owner wrote."""
    out = {}
    for snapshot in snapshots:
        for name, (module, glob, classes) in snapshot.items():
            for attr, reason in _declared(module).items():
                head, _dot, tail = attr.partition(".")
                pair = (classes.get(head, (None, {}))[1].get(tail)
                        if tail else glob.get(attr))
                if pair is not None and not any(
                        type(pair[0]) is t for t in _IMMUTABLE):
                    out.setdefault(id(pair[0]), "%s.%s: %s"
                                   % (name, attr, reason))
    return out


def data_mutations(before, after, unit=None):
    """[record] for module data `after` holds that `before` did not.

    Every record carries `excluded`: None when it is a finding, else the
    reason it is not, so the census keeps what the audit decided not to
    report beside what it did. A module replaced or removed from sys.modules
    is the process audit's finding, not this one's.

    THE UNIT'S OWN MODULE is data another unit can read only through a
    reference to it: the module object, or a class or function it defines,
    held by another module (`from tests.test_gate import GateBase`). A test
    module that nobody else references keeps its setUpModule globals and its
    classes' fixture attributes to itself, so those records are kept and not
    reported; once another module references it, they are findings -- except
    an attribute its own TestCase classes gain or re-bind while it runs,
    which is class-fixture state its setUpClass makes again on every run.

    ONE OBJECT, ONE FINDING. A container several modules hold (a re-export, a
    facade that fans a write out to its siblings) changes once; the first
    holder names it and the rest are recorded as its aliases."""
    records = []
    reached = None
    declared_objects = None
    named = {}
    for name, (module, glob, classes) in before.items():
        now = after.get(name)
        if now is None or now[0] is not module:
            continue
        declared = _declared(module)
        rows = [(key, "module", False, change, old, new)
                for key, change, old, new in _changes(glob, now[1])]
        for qual, (cls, attrs) in classes.items():
            later = now[2].get(qual)
            if later is None or later[0] is not cls:
                continue            # the class itself was re-bound: above
            test_case = unittest.TestCase in _CLASS_MRO.__get__(cls,
                                                                 type(cls))
            rows += [("%s.%s" % (qual, key), "class", test_case, change,
                      old, new)
                     for key, change, old, new in _changes(attrs, later[1],
                                                           cls)]
        own = unit is not None and (name == unit
                                    or name.startswith(unit + "."))
        if own and rows and reached is None:
            reached = _reached(after)
        referenced = own and bool(reached[0].get(name))
        for attr, scope, test_case, change, old, new in rows:
            excluded = None
            subject = old if change in ("content", "removed") else new
            shared = own and change == "content" and bool(
                reached[1].get(id(old), set()) - {name})
            if test_case and attr.rsplit(".", 1)[-1] in _UNITTEST_CLASS_STATE:
                excluded = "unittest's own class-fixture state"
            elif own and not referenced and not shared:
                excluded = ("the unit's own module, which no other module "
                            "references")
            elif own and test_case and change in ("added", "rebound"):
                excluded = ("class-fixture state of the unit's own TestCase "
                            "class, made again whenever the class runs")
            elif change == "added" and issubclass(type(new), types.ModuleType):
                excluded = "a submodule bound on its package by an import"
            elif scope == "module" and change == "rebound" \
                    and _is_code(old) and _is_code(new):
                excluded = "code re-bound: the process audit reports it"
            elif attr in declared:
                excluded = "declared: %s" % declared[attr]
            elif not any(type(subject) is t for t in _IMMUTABLE):
                if declared_objects is None:
                    declared_objects = _declared_objects((before, after))
                if id(subject) in declared_objects:
                    excluded = "declared as %s" % declared_objects[id(subject)]
                elif (id(subject), change) in named:
                    excluded = "the same object as %s, named there" \
                        % named[(id(subject), change)]
                else:
                    named[(id(subject), change)] = "%s.%s" % (name, attr)
            records.append({
                "unit": unit, "owner": name, "attr": attr, "change": change,
                "scope": scope, "test_case": test_case, "own": own,
                "old": None if old is _MISSING else _type_name(old),
                "new": None if new is _MISSING else _type_name(new),
                "excluded": excluded})
    return records


def data_findings(records):
    """The finding line for the records that are findings, or None."""
    named = ["%s.%s (%s)" % (r["owner"], r["attr"], r["change"])
             for r in records if r["excluded"] is None]
    if not named:
        return None
    return "module data mutated: %s" % ", ".join(named[:12]) + (
        "" if len(named) <= 12 else " and %d more" % (len(named) - 12))


THREAD_GRACE_S = 1.0


class ClaimedSuite(unittest.TestSuite):
    """A top-level suite whose children are chosen while the run iterates.

    TextTestRunner and TestSuite.run are unchanged: this only decides WHICH
    discovery units this worker yields, always in ascending discovery order.
    `plan` is this worker's fixed list of unit indices when the parent
    scheduled the run; without one, units are claimed from the shared counter.

    When the leak audit is on, a unit boundary is also where the previous
    unit's class and module fixtures come down -- exactly the calls
    TestSuite.run makes at the next unit's first test, made a moment earlier
    and only when that test belongs to a different module, so nothing runs
    between them and nothing runs twice. The last window closes the same way
    at the end of iteration, still inside the runner.
    """

    def __init__(self, units, labels, claim_path, audit, plan=None,
                 progress=None):
        super().__init__()
        self._cleanup = False
        self.units, self.labels = units, labels
        self.claim_path, self.audit = claim_path, audit
        self.plan = None if plan is None else sorted(plan)
        self.progress = progress or (lambda phase, name: None)
        self.claimed, self.findings, self._result = [], {}, None
        self.mutations = []
        self._open = None

    def run(self, result, debug=False):
        self._result = result
        return super().run(result, debug)

    def _indices(self):
        if self.plan is not None:
            yield from self.plan
            return
        while True:
            index = _claim(self.claim_path)
            if index >= len(self.units):
                return
            yield index

    def __iter__(self):
        for index in self._indices():
            unit = self.units[index]
            first = next(iter(_leaves(unit)), None) \
                if isinstance(unit, unittest.TestSuite) else unit
            if first is not None:
                self.close(first)
            self.claimed.append({"index": index, "started": time.monotonic()})
            self.progress("run", self.labels[index])
            if self.audit and first is not None and self._open is None:
                try:
                    data = data_snapshot()
                except Exception:   # noqa: BLE001 -- judged at close
                    data = {}
                self._open = (index, snapshot(), data)
            yield unit
        self.close(None)
        self.progress("done", "")

    def close(self, first):
        """Bring the previous module down now iff the next test would, then
        read what the window left behind. `first` None is the end of the run.

        A thread the unit started gets THREAD_GRACE_S to finish before it
        counts: a helper that is still unwinding when its test returns is not
        state the next unit inherits, and a thread alive after the grace is.
        """
        import threading
        result = self._result
        if not self.audit or result is None or self._open is None:
            return
        previous = getattr(result, "_previousTestClass", None)
        if previous is None:
            return
        if first is not None \
                and previous.__module__ == first.__class__.__module__:
            return
        self._tearDownPreviousClass(first, result)
        self._handleModuleTearDown(result)
        result._previousTestClass = None
        index, before, data = self._open
        self._open = None
        deadline = time.monotonic() + THREAD_GRACE_S
        for thread in set(threading.enumerate()) - before["threads"]:
            thread.join(max(0.0, deadline - time.monotonic()))
        found = leaks(before, snapshot())
        try:
            records = data_mutations(data, data_snapshot(), self.labels[index])
        except Exception as exc:    # noqa: BLE001 -- the audit, not the unit
            records = []
            found.append("module data unreadable: %s: %s"
                         % (type(exc).__name__, exc))
        self.mutations.extend(records)
        line = data_findings(records)
        if line:
            found.append(line)
        if found:
            self.findings.setdefault(self.labels[index], []).extend(found)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return default


def progress_path(work_dir, index):
    return os.path.join(work_dir, "progress-%d" % index)


def _progress_writer(work_dir, index):
    """-> progress(phase, name): one fixed-width record rewritten in place.

    os.pwrite raises no audit event, so it is safe to call from the import
    audit hook below without re-entering it."""
    fd = os.open(progress_path(work_dir, index),
                 os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)

    def progress(phase, name):
        record = ("%s %s" % (phase, name)).encode("utf-8", "replace")
        os.pwrite(fd, record[:_PROGRESS_WIDTH].ljust(_PROGRESS_WIDTH), 0)
    return progress


def _watch_imports(progress):
    """Record the start of every import while discovery runs. -> stop()

    THE DEADLINE IS A STALL DETECTOR, and discovery imports every module
    before the first test, so without this a worker's progress would not
    move until discovery ended and a slow-but-fine suite would read as
    stalled. An audit hook, not an import-system patch: it observes imports
    without changing what any module sees on sys.meta_path or the loader.
    Hooks cannot be removed, so after discovery it returns at once."""
    state = {"on": True}

    def hook(event, args):
        if state["on"] and event == "import":
            try:
                progress("import", str(args[0]))
            except Exception:           # noqa: BLE001 -- never break an import
                pass
    sys.addaudithook(hook)

    def stop():
        state["on"] = False
    return stop


def _worker(index, work_dir):
    shard = _shard()
    if not shard._subreaper_armed():
        raise RuntimeError("could not arm PR_SET_CHILD_SUBREAPER")
    progress = _progress_writer(work_dir, index)
    progress("start", "")
    swept = False
    try:
        # The process state `python -m unittest discover -s tests -t .` has
        # when its discovery starts: cwd first on sys.path, argv as unittest
        # sees it. Not the script's own directory, which would shadow stdlib
        # names with helm/*.py.
        sys.path[0] = os.getcwd()
        sys.argv = [os.path.join(os.path.dirname(unittest.__file__),
                                 "__main__.py"),
                    "discover", "-s", START_DIR, "-t", "."]
        labels = _read_json(os.path.join(work_dir, "labels.json"))
        stop_watching = _watch_imports(progress)
        try:
            units = discover(labels)
        finally:
            stop_watching()
        rows, digest = inventory(units, labels)
        with open(os.path.join(work_dir, "inventory-%d.json" % index), "w",
                  encoding="utf-8") as fh:
            json.dump({"digest": digest, "units": len(rows),
                       "tests": sum(len(ids) for _l, ids in rows)}, fh)
        if not publish_reference(work_dir, index, digest, rows):
            return _EXIT_INVENTORY
        plan = _read_json(os.path.join(work_dir, "plan.json"))
        audit = leak_mode() != "off"
        suite = ClaimedSuite(units, labels, os.path.join(work_dir, "claim"),
                             audit, None if plan is None else plan[str(index)],
                             progress=progress)
        warnings = None if sys.warnoptions else "default"
        verbosity = 2 if os.environ.get("HELM_GATESLICE_VERBOSE") == "1" else 1
        with open(os.path.join(work_dir, "worker-%d.protocol" % index), "w",
                  encoding="utf-8") as stream:
            result = unittest.TextTestRunner(
                stream=stream, verbosity=verbosity,
                warnings=warnings).run(suite)
    finally:
        swept = shard._sweep_own_descendants()
    if not swept:
        raise RuntimeError("descendants survived the worker's owned sweep")
    now = time.monotonic()
    ends = [row["started"] for row in suite.claimed[1:]] + [now]
    meta = {
        "ran": result.testsRun, "skipped": len(result.skipped),
        "failures": len(result.failures), "errors": len(result.errors),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "ok": result.wasSuccessful(), "digest": digest,
        "claimed": [{"index": row["index"], "label": labels[row["index"]],
                     "seconds": round(end - row["started"], 3)}
                    for row, end in zip(suite.claimed, ends)],
        "leaks": suite.findings,
        "mutations": suite.mutations,
    }
    path = os.path.join(work_dir, "worker-%d.json" % index)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    return 0 if result.wasSuccessful() else 1


def canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def schedule(labels, seconds, workers):
    """{worker: [unit indices, ascending]} by longest recorded unit first.

    Each unit goes to the worker with the least estimated load, heaviest
    first (the LPT rule), and each worker then runs its units in ascending
    discovery order, so what a worker has run before a unit is still a subset
    of what serial runs before it, in serial order. A unit with no recorded
    time is estimated at the median recorded time.
    """
    known = sorted(v for v in (seconds.get(label) for label in labels)
                   if isinstance(v, (int, float)) and v >= 0)
    default = known[len(known) // 2] if known else 1.0
    loads = [0.0] * workers
    plan = {str(w): [] for w in range(workers)}
    weights = [(float(seconds.get(label, default))
                if isinstance(seconds.get(label), (int, float)) else default,
                index) for index, label in enumerate(labels)]
    for weight, index in sorted(weights, key=lambda row: (-row[0], row[1])):
        w = min(range(workers), key=lambda n: (loads[n], n))
        loads[w] += weight
        plan[str(w)].append(index)
    return {w: sorted(indices) for w, indices in plan.items()}


LEAK_TEST = "LeakAudit.test_leaves_no_process_state"


def _leak_block(label, found):
    """One ERROR block per leaking unit, in unittest's own grammar, so the
    failure identity is `<unit>.LeakAudit.test_leaves_no_process_state` and a
    receipt reads it like any other failing test."""
    name = LEAK_TEST.rsplit(".", 1)[1]
    return (
        "======================================================================\n"
        "ERROR: %s (%s.%s)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        "  File \"helm/gateslice.py\", line 1, in %s\n"
        "    unit %s\n"
        "RuntimeError: unit %s left process state other units can see: %s\n\n"
        % (name, label, LEAK_TEST, name, label, label, "; ".join(found)))


def _summarize(work_dir, metas, broken):
    """What each worker ran, how long each unit took and what it left
    behind, kept beside the protocol for whoever plans the next run."""
    with open(os.path.join(work_dir, "summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump({
            "workers": {index: {key: meta[key] for key in (
                "ran", "skipped", "failures", "errors", "claimed", "leaks",
                "mutations")}
                for index, meta in metas.items()},
            "broken": broken,
        }, fh, sort_keys=True)


def _refuse(text):
    return "gateslice: %s; result refused\n" % text, 2


# 2: the leak census counts module DATA mutations too (task/3039), so a v2
# `leaks: 0` in fail mode says no unit left module data another could read.
# A v1 census watched process state only; a door that needs the data audit
# asks for v2.
EVIDENCE_VERSION = 2


_ARMED = False


def run(work_dir, repo=".", workers=None, deadline=None, seconds=None):
    """Launch the workers, check the whole-run proofs, merge. -> (text, rc,
    evidence or None)

    ONLY THE RUNNER'S OWN PROCESS MAY CALL THIS, after `main` has armed
    PR_SET_CHILD_SUBREAPER on it: the sweep below kills every descendant of
    the CALLING process, which is right for the runner's own process and
    wrong for any other. Called in-process from a test runner it would kill
    that runner's unrelated children, so it refuses unless `main` armed it.
    """
    if not _ARMED:
        raise RuntimeError(
            "gateslice.run runs only inside `python helm/gateslice.py`, "
            "which arms the subreaper its sweep relies on")
    shard = _shard()
    started = time.monotonic()
    with open(os.path.join(work_dir, "claim"), "w", encoding="ascii") as fh:
        fh.write("%020d" % 0)
    labels = discovery_modules(working_tree_files(repo))
    with open(os.path.join(work_dir, "labels.json"), "w",
              encoding="utf-8") as fh:
        json.dump(labels, fh)
    count = max(1, int(workers or worker_count(len(labels))))
    plan = None
    if seconds:
        plan = schedule(labels, seconds, count)
        with open(os.path.join(work_dir, "plan.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(plan, fh, sort_keys=True)
    # THE DEADLINE IS A STALL DETECTOR, NOT A WALL. gateshard's value is PER
    # MODULE, and a worker reports progress at the start of every import
    # during discovery and every module it runs, so one rule covers both
    # phases: a worker is killed, and its module named, only when it has not
    # moved for longer than one module is allowed to take.
    per_module = shard._worker_deadline() if deadline is None \
        else max(0, int(deadline))
    children = []
    launched = time.time()
    for index in range(count):
        noise = os.path.join(work_dir, "worker-%d.noise" % index)
        env = shard._fresh_env("HELM_GATESLICE_WORKER")
        for key in PARENT_ONLY_ENV:
            env.pop(key, None)
        with open(noise, "w", encoding="utf-8") as err:
            try:
                proc = subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--worker",
                     str(index), work_dir], cwd=repo,
                    stdout=subprocess.DEVNULL, stderr=err, env=env,
                    start_new_session=True)
            except OSError as exc:
                proc = None
                err.write("worker launch failed: %s\n" % exc)
        children.append((index, proc, noise))
    overdue = {}
    while True:
        alive = [row for row in children if row[1] and row[1].poll() is None
                 and row[0] not in overdue]
        if not alive:
            break
        now = time.time()
        for index, proc, _noise in alive:
            try:
                path = progress_path(work_dir, index)
                moved = max(launched, os.stat(path).st_mtime)
                with open(path, "rb") as fh:
                    at = fh.read().decode("utf-8", "replace").strip()
            except OSError:
                moved, at = launched, "start"
            if per_module and now - moved > per_module:
                overdue[index] = at or "start"
                shard._reap_tree(proc)
        time.sleep(0.1)
    statuses = {index: (proc.wait() if proc else _EXIT_CRASH)
                for index, proc, _noise in children}
    # A WORKER THAT DIED CANNOT SWEEP. Its sweep runs in a `finally`, and
    # os._exit or a signal skips that; the orphans it contained as subreaper
    # then reparent to THIS process, the nearest living subreaper. Every
    # worker has exited here, so anything still below this process is left
    # over from the run, and a run whose leftovers survive is not reported.
    if not shard._sweep_own_descendants():
        return _refuse("processes started by the run survived its sweep") \
            + (None,)
    noises, bodies, metas, broken = [], [], {}, []
    for index, _proc, noise in children:
        try:
            with open(noise, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        if text:
            noises.append(text if text.endswith("\n") else text + "\n")
        try:
            with open(os.path.join(work_dir, "worker-%d.protocol" % index),
                      encoding="utf-8") as fh:
                protocol = fh.read()
            with open(os.path.join(work_dir, "worker-%d.json" % index),
                      encoding="utf-8") as fh:
                meta = json.load(fh)
            shard._validated_result(protocol, meta, statuses[index])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            why = ("made no progress for %ds at %s; " % (per_module, overdue[index])
                   if index in overdue else "")
            broken.append("worker %d %s%s; %s" % (
                index, why, shard._wait_status(statuses[index]), exc))
            continue
        metas[index] = meta
        bodies.append(shard._protocol_body(protocol))
    try:
        with open(os.path.join(work_dir, "inventory.json"),
                  encoding="utf-8") as fh:
            reference = json.load(fh)
    except (OSError, ValueError):
        reference = None
    if reference is None:
        return _refuse("no worker completed discovery"
                       + ("" if not broken else " (%s)" % "; ".join(broken))) \
            + (None,)
    digests = set()
    for index in range(count):
        try:
            with open(os.path.join(work_dir, "inventory-%d.json" % index),
                      encoding="utf-8") as fh:
                digests.add(json.load(fh)["digest"])
        except (OSError, ValueError, KeyError):
            broken.append("worker %d wrote no inventory" % index)
    if digests != {reference["digest"]}:
        return _refuse("workers discovered %d different test inventories"
                       % len(digests)) + (None,)
    rows = reference["rows"]
    claims = sorted(row["index"] for meta in metas.values()
                    for row in meta["claimed"])
    _summarize(work_dir, metas, broken)
    if broken:
        text = "".join(noises + bodies)
        return text + ("UNKNOWN: no validated result from %s\n"
                       % "; ".join(broken)), 1, None
    if claims != list(range(len(rows))):
        return _refuse("units run %d times for %d planned units"
                       % (len(claims), len(rows))) + (None,)
    total = {key: sum(meta[key] for meta in metas.values())
             for key in ("ran", "skipped", "failures", "errors",
                         "expected_failures", "unexpected_successes")}
    findings = {}
    for meta in metas.values():
        for label, found in meta["leaks"].items():
            findings.setdefault(label, []).extend(found)
    mode = leak_mode()
    blocks = []
    if mode == "fail":
        blocks = [_leak_block(label, findings[label]) for label in
                  sorted(findings)]
        total["errors"] += len(blocks)
    report = ["gateslice: %d workers, %d units, %d planned tests, "
              "inventory sha256 %s\n"
              % (count, len(rows), sum(len(ids) for _l, ids in rows),
                 reference["digest"])]
    report += ["gateslice leak: %s: %s\n" % (label, "; ".join(findings[label]))
               for label in sorted(findings)]
    order = {index: [row["label"] for row in meta["claimed"]]
             for index, meta in metas.items()}
    with open(os.path.join(work_dir, "assignment.json"), "w",
              encoding="utf-8") as fh:
        json.dump(order, fh, sort_keys=True)
    detail = []
    for key, label in (("failures", "failures"), ("errors", "errors"),
                       ("skipped", "skipped"),
                       ("expected_failures", "expected failures"),
                       ("unexpected_successes", "unexpected successes")):
        if total[key]:
            detail.append("%s=%d" % (label, total[key]))
    failed = total["failures"] or total["errors"] \
        or total["unexpected_successes"]
    summary = "FAILED (%s)" % ", ".join(detail) if failed else \
        "OK%s" % (" (%s)" % ", ".join(detail) if detail else "")
    text = "".join(noises + bodies + blocks + report)
    text += "-" * 70 + "\nRan %d tests in %.3fs\n\n%s\n" % (
        total["ran"], time.monotonic() - started, summary)
    run_order = {str(index): [row["index"] for row in meta["claimed"]]
                 for index, meta in metas.items()}
    evidence = {
        "v": EVIDENCE_VERSION,
        "kind": "gateslice",
        "workers": count,
        "units": len(rows),
        "planned": sum(len(ids) for _l, ids in rows),
        "modules_digest": canonical_digest(labels),
        "inventory_digest": reference["digest"],
        "assignment_digest": canonical_digest(run_order),
        "schedule": "recorded-longest-first" if plan else "ascending-claim",
        "leak_mode": mode,
        "leaks": len(findings),
        "swept": "empty-after-exit",
        "seconds": {row["label"]: row["seconds"] for meta in metas.values()
                    for row in meta["claimed"]},
        "outcome": dict(total, ok=not failed),
    }
    return text, 1 if failed else 0, evidence


def _recorded_seconds():
    """Per-unit seconds from an earlier run, or None. HELM_GATESLICE_TIMINGS
    names a JSON object {module: seconds}; anything unreadable schedules by
    ascending claim instead, which is slower and never wrong."""
    path = os.environ.get("HELM_GATESLICE_TIMINGS")
    seconds = _read_json(path) if path else None
    if not isinstance(seconds, dict):
        return None
    return {k: v for k, v in seconds.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and v >= 0}


def _serial(names):
    """`gateslice.py --serial MODULE...`: the named modules in ONE process,
    in the order given, under the same leak and data audit a slice worker
    runs. A diagnostic for a module's hygiene: it imports only what the named
    modules import, so it is not the serial discovery's state, and its output
    opens with the marker, so no gate mints from it. Named modules only: a
    whole-suite audit in one process is `HELM_GATESLICE_WORKERS=1` on the
    runner itself, which the host census counts as the suite it is."""
    if not names or any(not n or n.startswith("-") for n in names):
        sys.stderr.write("usage: gateslice.py --serial MODULE...\n")
        return 2
    sys.stderr.write(DIAGNOSTIC_MARKER + "\n")
    sys.stderr.flush()
    sys.path[0] = os.getcwd()
    loader = unittest.defaultTestLoader
    units = [loader.loadTestsFromName(name) for name in names]
    mode = leak_mode()
    suite = ClaimedSuite(units, list(names), None, mode != "off",
                         plan=range(len(units)))
    warnings = None if sys.warnoptions else "default"
    result = unittest.TextTestRunner(stream=sys.stderr,
                                     warnings=warnings).run(suite)
    for label in sorted(suite.findings):
        sys.stderr.write("gateslice leak: %s: %s\n"
                         % (label, "; ".join(suite.findings[label])))
    if os.environ.get("HELM_GATESLICE_VERBOSE") == "1":
        # The whole census, kept and reported alike, with why each record
        # is or is not a finding.
        for r in suite.mutations:
            sys.stderr.write("gateslice data: %s: %s.%s (%s) %s\n" % (
                r["unit"], r["owner"], r["attr"], r["change"],
                r["excluded"] or "FINDING"))
    return 0 if result.wasSuccessful() and not (
        mode == "fail" and suite.findings) else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--worker":
        if len(argv) != 3 or os.environ.get("HELM_GATESLICE_WORKER") != "1":
            return 2
        try:
            return _worker(int(argv[1]), argv[2])
        except BaseException:           # noqa: BLE001 -- parent names it
            traceback.print_exc()
            return _EXIT_CRASH
    if argv and argv[0] == "--serial":
        return _serial(argv[1:])
    if argv:
        return 2
    sys.stderr.write(DIAGNOSTIC_MARKER + "\n")
    sys.stderr.flush()
    # CONTAINMENT BEFORE ANY WORKER EXISTS. As subreaper this process
    # inherits whatever a dead worker leaves behind, which is what lets
    # `_run` sweep it; without the flag those orphans go to init and outlive
    # the run, so a runner that cannot arm it refuses to run at all.
    global _ARMED
    if not _shard()._subreaper_armed():
        sys.stderr.write("gateslice: could not arm PR_SET_CHILD_SUBREAPER; "
                         "result refused\n")
        return 2
    _ARMED = True
    with tempfile.TemporaryDirectory(prefix="helm-gate-slices-") as tmp:
        text, rc, evidence = run(tmp, seconds=_recorded_seconds())
        for env_key, name, value in (
                ("HELM_GATESLICE_KEEP", "summary.json", None),
                ("HELM_GATESLICE_EVIDENCE", None, evidence)):
            target = os.environ.get(env_key)
            if not target:
                continue
            try:
                if name:
                    with open(os.path.join(tmp, name), encoding="utf-8") as src:
                        data = src.read()
                elif value is not None:
                    data = json.dumps(value, sort_keys=True)
                else:
                    continue
                with open(target, "w", encoding="utf-8") as dst:
                    dst.write(data)
            except OSError:
                pass
    sys.stderr.write(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
