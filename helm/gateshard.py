#!/usr/bin/env python3
"""Diagnose unittest outcomes in fresh module-preserving worker processes.

This runner is observational only. It cannot preserve arbitrary state created by
serial discovery before execution, so its protocol never authorizes a land.
"""
import collections
import hashlib
import importlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import unittest


# The standalone runner loads these siblings without importing the Helm package.
# Receipt authority derives its executable bundle from this declaration, so a
# new executed sibling cannot appear without changing the manifest population.
STANDALONE_DEPENDENCIES = ("gatetestrecord.py", "gatechild.py", "pathenv.py")

# THE OUTPUT SAYS WHAT IT IS. The parent's first stderr line, however it was
# launched (a shell, runpy, a joined -m): the gate reads the output and never
# mints a suite receipt from a diagnostic runner's result.
DIAGNOSTIC_MARKER = "HELM-DIAGNOSTIC-RUNNER gateshard"


_FOOTER = re.compile(
    r"^-{70}\nRan (?P<ran>\d+) tests? in \d+(?:\.\d+)?s\n\n"
    r"(?P<summary>OK(?: \([^\n]*\))?|FAILED \([^\n]*\)|NO TESTS RAN)"
    r"\n?\Z", re.M)
_RECORD = None
_MEASURE_ENV_KEYS = (
    "HELM_GATE_RECORD_DIR", "HELM_GATE_RECORD_TOKEN", "HELM_GATE_RECORD_ROLE",
    "HELM_GATE_RECORD_ROOT_PID", "HELM_GATE_RECORD_ROOT_START",
    "HELM_GATE_RECORD_SHARD",
)


def _recorder():
    global _RECORD
    if _RECORD is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            STANDALONE_DEPENDENCIES[0])
        spec = importlib.util.spec_from_file_location(
            "_helm_gate_test_record", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("gate outcome recorder is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RECORD = module
    return _RECORD


def _available_cpus():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _suite_capacity():
    try:
        capacity = int(os.environ.get("HELM_GATE_SUITE_CAP", ""))
    except ValueError:
        capacity = 0
    return max(1, capacity)


def _reap_tree(proc):
    """Kill a worker's whole PROCESS GROUP and WAIT for it. -> None.

    TWO SEPARATE HAZARDS, and the tests that missed them missed them for the
    same reason: they never ran the measured branch.

    REAPING. `Popen.kill()` only SIGNALS. Until someone waits, `returncode`
    stays None -- and the measured path builds its rows from `proc.returncode`
    directly rather than `wait()`, so an unreaped kill reaches `_wait_status(
    None)` and dies with `TypeError: NoneType < int`. The unmeasured path
    calls `wait()` and papers over it, which is exactly why every arm passed
    while the path that mints receipt evidence crashed.

    THE TREE. A worker spawns children of its own. Killing the direct pid
    leaves descendants alive holding the resources the freed slot is about to
    hand to someone else, so the pool refills into a machine still busy with
    work it believes it stopped. Workers are started with
    `start_new_session=True`, which makes the worker a process-group leader
    and lets one `killpg` take the whole tree."""
    # DESCENDANTS FIRST, ROOT LAST. Once the root dies its children are
    # reparented and the tree that named them is gone, so anything not
    # already killed becomes unattributable.
    #
    # A PROCESS-GROUP KILL IS NOT THE TREE AUTHORITY. A descendant started
    # with setsid/start_new_session leaves the worker's group entirely, so
    # killpg cannot see it, measured on this lane: the
    # worker died on schedule, the runner named the module, and the grandchild
    # was still alive afterwards. A freed pool slot must never coexist with
    # work from the module that vacated it.
    #
    # gatechild._kill_descendants walks /proc by PID+starttime and freezes
    # each generation to quiescence before killing, so a fork racing the
    # snapshot cannot escape it. killpg stays as a cheap arm for the common
    # in-group case; it is an optimisation, not the guarantee.
    #
    # THE ORDER IS LOAD-BEARING AND KILLING THE GROUP FIRST DEFEATS THE
    # SWEEP ENTIRELY: the worker is its own group leader, so killpg kills
    # it, its setsid grandchild is reparented to init, and _descendants of
    # a dead root then returns nothing. Measured -- the sweep ran, returned
    # cleanly, and swept an empty set while the grandchild lived on.
    try:
        _gatechild()._kill_descendants(proc.pid)
    except Exception:
        # Best effort: a missing or failing descendant sweep must not stop
        # the root kill below, which is what frees the slot.
        pass

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None and pgid == proc.pid and pgid != os.getpgid(0):
        # Never this process's own group: for a child NOT in a new session
        # getpgid() returns OUR group, and an unguarded killpg SIGKILLs the
        # runner and everything beside it (measured: a whole module run died
        # with 137).
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _worker_deadline():
    """Seconds one module's process may run before it is killed and named.

    A hung child is the ONE failure a bounded pool makes worse than the code
    it replaced: the old runner launched every bin and waited once, so a hang
    stalled a wait that was already unbounded; a pool polls, and without a
    deadline it polls forever while holding a slot no other module can use.
    Fail-open by construction -- the gate never returns, so it never refuses,
    and a silent hang renders as in-progress rather than as the failure it is.

    Generous by default because a slow module is not a hung one and killing a
    legitimate long test is its own false red. HELM_GATE_WORKER_DEADLINE
    overrides. EXACTLY ONE VALUE DISABLES THE DEADLINE: literal 0, which
    restores the old unbounded wait and is kept reachable so a bisect can turn
    it off. AN UNPARSEABLE VALUE FALLS BACK TO THE 900s DEFAULT -- it does NOT
    disable, because a typo in an env var must never silently remove a
    fail-closed guard."""
    try:
        seconds = int(os.environ.get("HELM_GATE_WORKER_DEADLINE", ""))
    except ValueError:
        seconds = 900
    return max(0, seconds)


def worker_count(groups, suite_capacity=None):
    """Per-gate CPU budget derived from this node and gate-provided capacity."""
    cores = max(1, _available_cpus())
    capacity = _suite_capacity() if suite_capacity is None else \
        max(1, suite_capacity)
    workers = max(1, cores // capacity)
    return min(max(1, groups), workers)


def _tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _tests(test)
        else:
            yield test


def module_groups(suite):
    """Group an arbitrary suite by the module that defines each test class."""
    grouped = collections.OrderedDict()
    for test in _tests(suite):
        name = getattr(test.__class__, "__module__", "") or "unittest.loader"
        grouped.setdefault(name, []).append(test)
    return list(grouped.items())


class _PlanningLoader(unittest.TestLoader):
    """Record which module exported every test instance discovery collected."""

    def __init__(self):
        super().__init__()
        self.owners = {}

    def loadTestsFromModule(self, module, pattern=None):
        suite = super().loadTestsFromModule(module, pattern=pattern)
        for test in _tests(suite):
            self.owners[id(test)] = module.__name__
        return suite


def discover_plan():
    """Return serial discovery plus exporter-owned worker groups.

    unittest collects imported TestCase bindings under every module that exports
    them. Their class still names the defining module, so reconstructing module
    ownership from the flattened test object loses both multiplicity and the
    fixture under which serial discovery ran it. The loader records successful
    export boundaries; discovery-generated failure tests retain their existing
    unittest.loader ownership and protocol instead of becoming planner crashes.
    """
    loader = _PlanningLoader()
    suite = loader.discover("tests", pattern="test*.py", top_level_dir=".")
    grouped = collections.OrderedDict()
    for test in _tests(suite):
        name = loader.owners.get(id(test)) \
            or getattr(test.__class__, "__module__", "") \
            or "unittest.loader"
        grouped.setdefault(name, []).append(test)
    return suite, list(grouped.items())


def execution_groups(groups):
    """Central same-process plan, in discovery order, or raise if inconsistent.

    ONE MODULE PER EXECUTION GROUP, AND NO ESCAPE FROM THAT. There is no module
    linking here and deliberately no allowlist to add one to.

    THIS IS NOT A PROCESS GUARANTEE AND MUST NOT BE READ AS ONE. `shard_bins`
    re-packs these singleton groups into BINS, and `run_bins` launches one fresh
    interpreter PER BIN — so two modules that are separate groups here still
    share a process downstream. An earlier version of this docstring claimed
    "one module, one process"; that was FALSE ON THIS ARTIFACT, and asserting
    the stronger property here would have let a lane inherit a guarantee no
    code in this file provides. Process isolation is the canonical successor's
    contract, not this function's.

    What IS true, and what the arms pin: a module appears in exactly one group,
    a group carries exactly one module, and no declarative mechanism exists to
    join two.

    This function used to carry `_LINKED_MODULES`, a pairing of
    tests.test_configs with a tests.test_configs_cleanup module that consumed
    state the first module's tearDownModule released. That pairing was a
    MEASURED description of a real dependency, and it was still the wrong shape:
    it made a cross-module contract legal, so the two tests only passed because
    something else ran first and had never tested their claim standing alone.
    The isolation audit found them serial-green and isolated-red.

    The cure was to move that module's postcondition INTO its own
    tearDownModule, where the obligation lives, and delete the consumer. With
    the last pair gone the allowlist is empty — and an EMPTY allowlist is worse
    than none, because it is a working extension point for semantics the
    contract forbids. So the mechanism goes with its last member.

    Returns (order, [name], count) per module: the list stays a list so callers
    keep their shape, and it is always exactly one member.
    """
    rows = {name: (order, tests)
            for order, (name, tests) in enumerate(groups)}
    if len(rows) != len(groups):
        raise RuntimeError("duplicate module group in unittest discovery")
    return [(order, [name],
             tests if type(tests) is int else len(tests))
            for order, (name, tests) in enumerate(groups)]


def _plan_counts(groups):
    if type(groups) is not list or not groups:
        raise ValueError("discovery plan is not a non-empty list")
    counts = {}
    for row in groups:
        if type(row) not in (list, tuple) or len(row) != 2:
            raise ValueError("discovery plan row is not a pair")
        name, count = row
        if not isinstance(name, str) or not name:
            raise ValueError("discovery plan module is not a name")
        if type(count) is not int or count <= 0:
            raise ValueError("discovery plan count is not positive")
        if name in counts:
            raise ValueError("duplicate module in discovery plan")
        counts[name] = count
    return counts


def shard_bins(groups, workers):
    """Balance planned units; preserve discovery order inside every worker."""
    bins = [[] for _ in range(max(1, workers))]
    sizes = [0] * len(bins)
    for order, names, size in sorted(
            execution_groups(groups), key=lambda row: -row[2]):
        i = min(range(len(bins)), key=lambda n: (sizes[n], n))
        bins[i].append((order, names))
        sizes[i] += size
    return [[name for _order, names in sorted(grouped) for name in names]
            for grouped in bins]


def canonical_bins(groups):
    """One module per process: the canonical execution model.

    shard_bins PACKS modules together to balance a fixed worker count, and
    that packing is exactly what hides a producer/consumer pair when the two
    co-locate in one interpreter. Canonical bins refuse it: every module gets
    its own interpreter, so a module's result depends on nothing that ran
    before it.

    That property is what LICENSES SELECTION. If no module inherits state from
    another, running a subset is exactly as valid as running all of them, and
    a focused receipt means the same thing a whole-suite one does for the
    modules it covers.

    THE UNIT IS THE EXECUTION GROUP, NOT THE MODULE, even though every group
    is a singleton today. execution_groups used to pin tests.test_configs to
    tests.test_configs_cleanup via _LINKED_MODULES, because the second
    consumed state the first established; the establish-not-inherit cure
    dissolved that on trunk dc2ceb0afbbe and the function now refuses linking
    with no allowlist to add one to.

    Binning by GROUP rather than by module is still the correct shape, and
    not for hypothetical futures: it means this function asserts nothing
    about grouping that execution_groups does not already decide. A bin is a
    whole group. If grouping ever returns, this code is already right, and if
    it never does, nothing here is dead -- the two simply agree.
    """
    return [list(names) for _order, names, _size
            in sorted(execution_groups(groups), key=lambda row: row[0])]


def _verbosity():
    """Verbose protocol only when an equivalence measurement asks for ids."""
    return 2 if os.environ.get("HELM_GATESHARD_VERBOSE") == "1" else 1


def _assigned_groups(modules):
    """Load only planner-assigned modules, once each, in planner order."""
    root = os.path.abspath(os.getcwd())
    if root not in sys.path:
        sys.path.insert(0, root)
    groups = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            raise RuntimeError(
                "assigned module could not load: %s" % name) from exc
        tests = list(_tests(
            unittest.defaultTestLoader.loadTestsFromModule(module)))
        groups.append((name, tests))
    return groups


def _gatechild():
    """gatechild, loaded WITHOUT putting `helm` into sys.modules. -> module

    The worker must not import the helm PACKAGE. An adopter repo ships its
    own `helm`, and the diagnostic-path contract says the adopter's package
    wins inside its own tests; importing ours first poisons sys.modules for
    every module that runs afterwards in this process. The arm for that
    caught two successive attempts here -- insert-at-0 and then plain
    `from helm import gatechild` -- which is exactly what it is for.

    gatechild is pure stdlib (ctypes/os/signal/subprocess/sys/time), so it
    loads standalone from its own path with no package context and no
    sys.path mutation at all. Cached because the worker asks once per run and
    the sweep asks once per overdue child.
    """
    global _GATECHILD
    if _GATECHILD is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            STANDALONE_DEPENDENCIES[1])
        spec = importlib.util.spec_from_file_location(
            "_helm_gatechild_standalone", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GATECHILD = module
    return _GATECHILD


_GATECHILD = None


def _sweep_own_descendants():
    """Kill anything this worker still owns and PROVE none survives. -> bool

    Returns True only when a post-sweep census finds no live descendant.
    False means the caller must publish NOTHING, which the parent reads as
    UNKNOWN naming this module.

    THE PROOF IS THE POINT AND AN EARLIER VERSION HAD NONE. It swallowed
    every exception, returned nothing, and the worker wrote its authoritative
    meta regardless -- fail-OPEN dressed as cleanup. Its comment excused this
    by invoking the parent's outer arm, and THAT SENTENCE WAS FALSE: the
    parent reaps only OVERDUE live workers, so on normal completion there is
    no outer arm at all. A reassuring sentence about a guarantee nobody
    provides is worse than no comment.

    _kill_descendants returns None whether it swept a tree or exhausted its
    passes, so it cannot be asked whether it succeeded; the census here is
    what turns it into an answer.
    """
    child = _gatechild()
    try:
        child._kill_descendants(os.getpid())
        return not [row for row in child._descendants(os.getpid())
                    if row[2] != "Z"]
    except Exception:
        return False


def _subreaper_armed():
    """Arm PR_SET_CHILD_SUBREAPER for this process. -> bool.

    THE WORKER RUNS AS A SCRIPT, NOT AS A PACKAGE MEMBER: the parent spawns
    `python gateshard.py --worker`, so `from . import gatechild` raises
    ImportError with no parent package. Measured -- it failed closed exactly
    as designed and refused every module, which is the correct behaviour for
    a containment guard that cannot arm and the wrong outcome for a run.

    Loaded via _gatechild(), which reaches the primitive WITHOUT importing
    the helm package -- see that function for why the worker must not.
    """
    try:
        return bool(_gatechild()._set_subreaper())
    except Exception:
        return False


def _worker(index, modules, expected, protocol_path, meta_path,
            expected_ids):
    try:
        # CONTAINMENT BEFORE EXECUTION. A test that double-forks orphans its
        # grandchild out of this process's tree the moment the middle process
        # exits, so the parent's descendants-first sweep cannot see it and the
        # daemon outlives the deadline -- free to mutate the shared repo while
        # LATER modules run in the slot this one vacated. That is a direct
        # breach of one-module isolation, not untidy cleanup.
        #
        # PR_SET_CHILD_SUBREAPER makes THIS worker the reaper for its own
        # orphans, so a double-forked grandchild reparents to us instead of
        # init and the existing sweep finds it by ordinary descent.
        #
        # FAIL CLOSED IF IT DOES NOT ARM: a worker without containment
        # authority must publish NOTHING, which the parent already reads as
        # UNKNOWN naming this module. Running the tests anyway would produce
        # a result we cannot bound, and a bindable-looking one at that.
        if not _subreaper_armed():
            raise RuntimeError(
                "could not arm PR_SET_CHILD_SUBREAPER; refusing to run "
                "assigned modules without containment authority")
        # CONTAINMENT IS A LIFECYCLE, NOT A STEP. From this line the worker
        # owns every process it creates, INCLUDING ones created before any
        # test runs: _assigned_groups IMPORTS the assigned modules, and an
        # import can spawn. An earlier version swept only after a successful
        # run, so an exception during import, census or runner setup exited
        # here with orphans never swept and nothing to say so.
        swept = False
        try:
            wanted = set(modules)
            groups = _assigned_groups(modules)
            found = dict(groups)
            if set(found) != wanted:
                raise RuntimeError("assigned modules vanished: %s" %
                                   ", ".join(sorted(wanted - set(found))))
            changed = [name for name in modules
                       if len(found[name]) != expected.get(name)]
            if changed:
                raise RuntimeError("assigned module census changed: %s" %
                                   ", ".join(changed))
            changed = [name for name in modules
                       if _recorder().planned_ids(unittest.TestSuite(found[name]))
                       != expected_ids[name]]
            if changed:
                raise RuntimeError("assigned module ids changed: %s" %
                                   ", ".join(changed))
            tests = [test for name in modules for test in found[name]]
            with open(protocol_path, "w", encoding="utf-8") as stream:
                runner = unittest.TextTestRunner(
                    stream=stream, verbosity=_verbosity())
                suite = unittest.TestSuite(tests)
                context = _recorder().consume_context("worker") \
                    if os.environ.get("HELM_GATE_RECORD_ROLE") == "worker" else None
                if context:
                    context.update(modules=list(modules))
                    # THE CONTROLLED ADAPTER, NEVER run_recorded, IN THE
                    # CANONICAL WORKER. run_recorded captures through
                    # sys.setprofile, a single global slot, so it cannot
                    # observe the module that installs a recorder -- measured
                    # on tests.test_gateequiv: four nested OutcomeRecorderTest
                    # arms lost their inner record path, four ERRORs, no
                    # artifact, correctly UNKNOWN. An instrument that breaks
                    # on its own tests cannot carry authority for a suite that
                    # contains them. run_recorded is untouched and keeps its
                    # callers; this is the second capture adapter over the
                    # same ledger, schema and validation.
                    result, record_path = _recorder().run_controlled(
                        suite, context["directory"], context,
                        stream=stream, verbosity=_verbosity())
                    # BOTH HALVES, NOT JUST THE RESULT. run_controlled returns
                    # (result, None) for ledger invalidity, artifact validation
                    # failure AND write failure -- three distinct ways the
                    # RECORDING failed while the RUN succeeded. Checking only
                    # `result is None` certifies a module whose evidence was
                    # never written, which is precisely the measured-mode
                    # evidence the canonical path exists to produce.
                    #
                    # An earlier version named this `_record` and then honoured
                    # the name: a leading underscore is a convention, not a
                    # reason, and here it advertised the exact defect.
                    if result is None or record_path is None:
                        raise RuntimeError(
                            "controlled recording produced no validated "
                            "artifact; refusing to certify %s"
                            % ", ".join(modules))
                else:
                    result = runner.run(suite)
        finally:
            swept = _sweep_own_descendants()
        if not swept:
            # PUBLISH NOTHING. A surviving descendant means this module's
            # containment failed, and the parent reads a missing artifact as
            # UNKNOWN naming the module -- which is the honest verdict. The
            # alternative is certifying a module whose work is still running
            # into the slot that replaces it.
            raise RuntimeError(
                "descendants survived the owned sweep; refusing to certify "
                "%s" % ", ".join(modules))
        meta = {
            "ran": result.testsRun,
            "skipped": len(result.skipped),
            "failures": len(result.failures),
            "errors": len(result.errors),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "ok": result.wasSuccessful(),
        }
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        return 0 if result.wasSuccessful() else 1
    except BaseException:  # the parent turns this into one named shard error
        traceback.print_exc()
        return 70


def _protocol_body(text):
    hit = _FOOTER.search(str(text or ""))
    return str(text or "")[:hit.start()] if hit else str(text or "")


def _wait_status(status):
    """How a worker's exit reads. -> str

    TWO SPELLINGS OF A SIGNAL DEATH reach this reader. A process the pool
    itself waits on and that dies of signal N waits as -N (Popen's form),
    and `_supervise` reports its inner worker's signal death as 128+N, the
    shell's form (task/3075). Both read as the signal, so a TERM death never
    reads as the exit code 143 or, before that cure, 241. Every exit code
    the shard and slice runners choose is below 128; only a test that calls
    `os._exit` with a larger number reads as a signal, as a shell reads it."""
    if status < 0:
        return "died on signal %s" % _signal_label(-status)
    if 128 < status < 128 + signal.NSIG:
        return "died on signal %s (reported as exit %d)" % (
            _signal_label(status - 128), status)
    return "exited %d" % status


def _signal_label(number):
    try:
        return "%d (%s)" % (number, signal.Signals(number).name)
    except ValueError:
        return "%d" % number


_COUNT_FIELDS = ("ran", "skipped", "failures", "errors",
                 "expected_failures", "unexpected_successes")
_TEST_ENV_KEYS = ("HELM_CONFIG_ROOTS", "HELM_METAHARNESS",
                  "HELM_PROXYJOURNAL_LOG", "HELM_SEAT_NAMES",
                  "MELD_SEAT_NAMES", "HELM_TURNSTAMP_DIR",
                  "MELD_TURNSTAMP_DIR", "HELM_MULTIPLAYER_DIR",
                  "MELD_MULTIPLAYER_DIR")

_ROLE_ENV_KEYS = ("HELM_GATESHARD_PLANNER", "HELM_GATESHARD_WORKER")
_PATH_ENV_KEYS = None

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_GATECHILD": (
        "a sibling module loaded by path once; loading it again yields the "
        "same code"),
    "_RECORD": (
        "a sibling module loaded by path once; loading it again yields the "
        "same code"),
    "_PATH_ENV_KEYS": "a constant tuple read once from pathenv",
}


def _identity_path_env_keys():
    """The shared identity-estate path set, without importing ``helm``.

    This file is also executed directly as the canonical shard runner.  A
    package import would run ``helm.__init__`` before the fresh worker has
    scrubbed its inherited paths, so load the declared standalone sibling by
    file instead.  It is part of ``STANDALONE_DEPENDENCIES`` and therefore part
    of the signed runner bundle rather than an ambient helper.
    """
    global _PATH_ENV_KEYS
    if _PATH_ENV_KEYS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pathenv.py")
        spec = importlib.util.spec_from_file_location(
            "_helm_identity_path_env", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("identity path environment declaration unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PATH_ENV_KEYS = tuple(module.IDENTITY_PATH_ENV_KEYS)
    return _PATH_ENV_KEYS


def scrubbed_env(base):
    """`base` without any store path, test root or runner role a launcher
    chose. -> a new dict

    THE ONE ENV CONTRACT for every process that runs helm's suite as a
    verdict: each shard or slice worker (through `_fresh_env`) and the serial
    child (through gate._suite_env). tests/__init__ then plants every one of
    these itself, so the verdict never depends on what the launching shell
    exported.
    """
    env = dict(base)
    keys = (_TEST_ENV_KEYS + _identity_path_env_keys()
            + _ROLE_ENV_KEYS + _MEASURE_ENV_KEYS)
    for key in keys:
        env.pop(key, None)
    # EVERY SPELLING OF THE CONFIG ROOTS. helm/configs reads a declared
    # predecessor's `<NAME>_CONFIG_ROOTS` as a fallback, and which name that is
    # lives in the host's local names. This file also runs standalone, before
    # any helm import, so it drops the whole family rather than read them.
    for key in [k for k in env if k.endswith("_CONFIG_ROOTS")]:
        env.pop(key)
    return env


def _fresh_env(marker):
    env = scrubbed_env(os.environ)
    env[marker] = "1"
    return env


_DETAIL_FIELDS = {
    "skipped": "skipped",
    "failures": "failures",
    "errors": "errors",
    "expected_failures": "expected failures",
    "unexpected_successes": "unexpected successes",
}


def _detail_counts(detail):
    counts = {}
    for part in str(detail or "").split(","):
        hit = re.fullmatch(
            r"\s*(failures|errors|skipped|expected failures|"
            r"unexpected successes)=(\d+)\s*", part)
        if hit:
            counts[hit.group(1)] = int(hit.group(2))
    return counts


def _validated_result(protocol, result, status):
    """Return trustworthy worker counts or raise with a named diagnosis."""
    if type(result) is not dict:
        raise ValueError("metadata is not an object")
    counts = {}
    for name in _COUNT_FIELDS:
        value = result.get(name)
        if type(value) is not int or value < 0:
            raise ValueError("metadata %s is not a non-negative integer" % name)
        counts[name] = value
    ok = result.get("ok")
    if type(ok) is not bool:
        raise ValueError("metadata ok is not a boolean")
    expected_ok = not (counts["failures"] or counts["errors"]
                       or counts["unexpected_successes"])
    if ok != expected_ok:
        raise ValueError("metadata ok disagrees with its result counts")
    expected_exit = 0 if ok else 1
    if status != expected_exit:
        raise ValueError("metadata says %s but worker %s" % (
            "OK" if ok else "FAILED", _wait_status(status)))

    footer = _FOOTER.search(protocol)
    if not footer:
        raise ValueError("protocol has no terminal unittest footer")
    summary = footer.group("summary")
    protocol_ok = summary.startswith("OK") or summary == "NO TESTS RAN"
    if protocol_ok != ok:
        raise ValueError("protocol says %s but metadata says %s" % (
            "OK" if protocol_ok else "FAILED", "OK" if ok else "FAILED"))
    if int(footer.group("ran")) != counts["ran"]:
        raise ValueError("protocol ran %s but metadata ran %d" % (
            footer.group("ran"), counts["ran"]))
    detail = _detail_counts(
        summary.partition("(")[2].rpartition(")")[0])
    for name, label in _DETAIL_FIELDS.items():
        if detail.get(label, 0) != counts[name]:
            raise ValueError("protocol %s=%d but metadata %s=%d" % (
                label, detail.get(label, 0), name, counts[name]))
    counts["ok"] = ok
    return counts


def _crash_block(index, diagnosis, modules=()):
    """Name the MODULES that went unobserved, never only the bin index.

    A shard index is an arithmetic accident of binning; the module is the
    thing a reader can act on. Under canonical one-module-per-process bins
    the two carry the same information, so a child that exits without
    publishing an artifact can never cost the census the module's name.

    THE NAME GOES IN THE DIAGNOSIS, NEVER IN THE IDENTITY. The `ERROR:` line
    and the `_FailedTest.shard_N` parenthetical are a PARSED TEST ID; putting
    module text there changes the id from `...shard_N` to
    `...shard_N.tests.test_x` and breaks every consumer that matches on it
    (measured 2026-08-26: 2 existing arms went red on exactly this). The
    RuntimeError message is free text and is where a reader looks anyway.
    """
    named = ", ".join(modules) if modules else "shard_%d" % index
    return (
        "======================================================================\n"
        "ERROR: shard_%d (unittest.loader._FailedTest.shard_%d)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        "  File \"helm/gateshard.py\", line 1, in shard_%d\n"
        "    worker process %s\n"
        "RuntimeError: unittest shard %d (%s) %s\n\n"
        % (index, index, index, diagnosis, index, named, diagnosis))


def plan_digest(expected_counts, expected_ids):
    """A stable fingerprint of WHAT WAS PLANNED. -> hex str

    Binds the ordered module census AND the full test-id multiset, so two
    plans agree only if they enumerated the same tests the same number of
    times in the same module order.

    A MULTISET, NEVER A SET, and this is not pedantry: duplicate test ids are
    legitimate here and pinned by OutcomeRecorderTest.test_duplicate_ids_
    remain_a_multiset. Set equality would silently accept a plan that dropped
    a duplicate, which is exactly the kind of shrinkage a digest exists to
    catch. A set view of the same plan collapses them and cannot.

    MODULE ORDER IS CANONICAL (SORTED), NOT DISCOVERY ORDER, and saying so
    matters because an earlier version of this docstring claimed the census
    order was bound while the code sorted -- and the arm that pins dict-order
    independence would have contradicted the doc, not the code. A digest that
    varied with dict iteration order would be useless for comparing two
    independent enumerations of the same tree, which is the entire job.

    Order WITHIN a module is preserved: discovery order inside a module is a
    real property of the plan and two plans that run the same ids in a
    different order are not the same plan.
    """
    # LENGTH-PREFIX EVERY ELEMENT; NEVER JOIN ON A SEPARATOR. A separator is
    # only unambiguous if it cannot occur inside an element, and a test id is
    # an arbitrary string. The collision was measured on the exact tree:
    # ids ["a\x1fb", "c"] and ["a", "b\x1fc"] joined on \x1f produce the
    # SAME sha256, so two different plans get one digest -- the precise
    # failure a digest exists to prevent. Prefixing each element with its
    # byte length makes the encoding injective for any content.
    chunks = []
    def _put(text):
        raw = text.encode("utf-8", "surrogatepass")
        chunks.append(b"%d:" % len(raw))
        chunks.append(raw)
    for name in sorted(expected_counts):
        _put(name)
        _put(str(expected_counts[name]))
        for test_id in expected_ids.get(name, ()):
            _put(test_id)
    return hashlib.sha256(b"".join(chunks)).hexdigest()


def run_bins(bins, work_dir, repo=".", expected_counts=None,
             expected_ids=None, record_context=None, max_parallel=None,
             deadline=None):
    """Run explicit module bins in fresh interpreters, then aggregate."""
    try:
        expected_counts = dict(expected_counts or {})
        expected_ids = dict(expected_ids or {})
    except (TypeError, ValueError):
        return "gateshard: worker plan does not match discovery census\n", 2
    assigned = [name for modules in bins for name in modules]
    valid_counts = all(
        isinstance(name, str) and name
        and type(count) is int and count > 0
        for name, count in expected_counts.items())
    valid_ids = set(expected_ids) == set(expected_counts) \
        and all(type(rows) is list and len(rows) == expected_counts[name]
                and all(isinstance(test_id, str) and test_id for test_id in rows)
                for name, rows in expected_ids.items())
    if not valid_counts or not valid_ids or set(assigned) != set(expected_counts) \
            or len(assigned) != len(set(assigned)):
        return "gateshard: worker plan does not match discovery census\n", 2
    started, children, assignments, samples = time.monotonic(), [], [], []
    worker_context = None
    if record_context:
        ident = _recorder().process_identity()
        worker_context = dict(record_context, root_pid=ident["pid"],
                              root_start=ident["start"])
    runner = os.path.abspath(__file__)
    cap = max(1, int(max_parallel or len(bins) or 1))
    pending, inflight, observed = list(enumerate(bins, 1)), [], []
    excluded = [os.getpid()]
    limit = _worker_deadline() if deadline is None else max(0, int(deadline))
    started_at, overdue = {}, []
    while pending or inflight:
        while pending and len(inflight) < cap:
            i, modules = pending.pop(0)
            manifest = os.path.join(work_dir, "shard-%d.modules.json" % i)
            protocol = os.path.join(work_dir, "shard-%d.protocol" % i)
            meta = os.path.join(work_dir, "shard-%d.json" % i)
            noise = os.path.join(work_dir, "shard-%d.noise" % i)
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump({
                    "modules": modules,
                    "counts": {name: expected_counts[name] for name in modules},
                    "ids": {name: expected_ids[name] for name in modules},
                }, fh, sort_keys=True)
            env = _fresh_env("HELM_GATESHARD_WORKER")
            if worker_context:
                env = _recorder().child_env(
                    worker_context, "worker", shard=i, base=env)
            with open(noise, "w", encoding="utf-8") as err:
                try:
                    proc = subprocess.Popen(
                        [sys.executable, runner, "--supervise", str(i),
                         manifest, protocol, meta], cwd=repo,
                        stdout=subprocess.DEVNULL, stderr=err, env=env,
                        start_new_session=True)
                except OSError as exc:
                    proc = None
                    err.write("worker launch failed: %s\n" % exc)
            item = (i, proc, protocol, meta, noise, list(modules))
            children.append(item)
            if proc:
                inflight.append(item)
                started_at[i] = time.monotonic()
                excluded.append(proc.pid)
            if worker_context:
                assignments.append({
                    "index": i,
                    "modules": list(modules),
                    "pid": proc.pid if proc else None,
                    "start": _recorder().process_start(proc.pid)
                             if proc else None,
                })
        if worker_context:
            observed.append(_recorder().system_sample(
                "steady", [item[1].pid for item in inflight],
                exclude=excluded))
        alive = []
        for item in inflight:
            if item[1].poll() is not None:
                continue
            if limit and time.monotonic() - started_at[item[0]] > limit:
                # Kill it and let the ordinary crash path name its modules:
                # a killed child publishes no artifact, which is already the
                # fail-closed branch. Never drop it silently -- an overdue
                # module that vanishes is indistinguishable from one that
                # was never planned.
                overdue.append(item[0])
                _reap_tree(item[1])
                continue
            alive.append(item)
        inflight = alive
        if pending or inflight:
            time.sleep(0.05)

    if worker_context:
        launched = [item[1].pid for item in children if item[1]]
        samples.append(_recorder().system_sample(
            "launch", launched, exclude=excluded))
        if not observed:
            observed.append(_recorder().system_sample(
                "steady", [], exclude=excluded))
        peak = max(observed, key=lambda row: (
            len(row["workers"]), row["load"][0] if row["load"] else -1))
        samples.extend((observed[0], dict(peak, phase="peak")))
        rows = [item + (item[1].returncode if item[1] else 70,)
                for item in children]
    else:
        rows = [item + (item[1].wait() if item[1] else 70,)
                for item in children]

    noises, bodies, ran, unwitnessed = [], [], 0, set()
    skipped = failures = errors = crashes = failed_workers = 0
    expected_failures = unexpected_successes = 0
    for i, _pid, protocol, meta, noise, modules, status in rows:
        try:
            with open(noise, encoding="utf-8") as fh:
                diagnostic = fh.read()
        except OSError:
            diagnostic = ""
        if diagnostic:
            noises.append(diagnostic + ("" if diagnostic.endswith("\n") else "\n"))
        try:
            with open(protocol, encoding="utf-8") as fh:
                protocol_text = fh.read()
            with open(meta, encoding="utf-8") as fh:
                result = json.load(fh)
            result = _validated_result(protocol_text, result, status)
        except (OSError, ValueError, TypeError) as exc:
            diagnosis = "%s; %s" % (_wait_status(status), exc)
            if i in overdue:
                diagnosis = ("exceeded the %ds worker deadline and was killed; "
                             "%s" % (limit, diagnosis))
            tail = " | ".join(diagnostic.splitlines()[-2:])
            if tail:
                diagnosis += ": " + tail
            bodies.append(_crash_block(i, diagnosis, modules))
            unwitnessed.update(modules)
            errors += 1
            crashes += 1
            continue
        bodies.append(_protocol_body(protocol_text))
        ran += result["ran"]
        skipped += result["skipped"]
        failures += result["failures"]
        errors += result["errors"]
        expected_failures += result["expected_failures"]
        unexpected_successes += result["unexpected_successes"]
        failed_workers += not result["ok"]

    elapsed = time.monotonic() - started
    detail = []
    if failures:
        detail.append("failures=%d" % failures)
    if errors:
        detail.append("errors=%d" % errors)
    if crashes:
        detail.append("shard crashes=%d; test count partial" % crashes)
    if skipped:
        detail.append("skipped=%d" % skipped)
    if expected_failures:
        detail.append("expected failures=%d" % expected_failures)
    if unexpected_successes:
        detail.append("unexpected successes=%d" % unexpected_successes)
    failed = failed_workers or crashes
    if not failed and not ran:
        return "gateshard: unittest discovery completed zero tests; result refused\n", 2
    summary = "FAILED (%s)" % ", ".join(detail) if failed else \
        "OK%s" % (" (%s)" % ", ".join(detail) if detail else "")
    text = "".join(noises + bodies)
    if text and not text.endswith("\n"):
        text += "\n"
    text += "-" * 70 + "\nRan %d tests in %.3fs\n\n%s\n" % (
        ran, elapsed, summary)
    if unwitnessed:
        # A MODULE WITH NO VALIDATED RESULT IS UNKNOWN, NOT FAILED.
        # FAILED is a COMPLETE verdict -- it asserts we know each module's
        # outcome and some were red. A vanished child asserts the opposite:
        # its outcome is exactly what we do not have. Reporting FAILED there
        # claims knowledge we never held, and because FAILED parses as a
        # well-formed terminal footer a consumer can bind it as authoritative.
        #
        # The cure is to DENY THE FOOTER, not to add a warning beside it:
        # gate.parse_result reads the runner's own summary and yields UNKNOWN
        # when no parseable terminal footer exists, so appending this block
        # after the footer is what makes the status honest. The modules are
        # named because a module with no validated result is otherwise
        # indistinguishable from one that was never planned.
        #
        # THE TAXONOMY IS UNIFORM AND THAT IS DELIBERATE. This branch is
        # reached for an ABSENT artifact, a MALFORMED one, an artifact whose
        # test identities DRIFTED from the plan, an exit code that disagrees
        # with the body, and a worker that never launched. Those differ in
        # CAUSE and not in what they leave us holding: in every one of them we
        # do not have this module's outcome. An earlier draft of this comment
        # claimed the branch meant "published nothing", which was narrower
        # than the code and would have let a malformed artifact keep reporting
        # FAILED -- a confident answer manufactured from an untrusted one.
        text += ("\nUNKNOWN: no validated result for %d module(s); their "
                 "outcome is unknown, not failed: %s\n"
                 % (len(unwitnessed), ", ".join(sorted(unwitnessed))))
    rc = 1 if failed else 0
    if record_context:
        ident = _recorder().process_identity()
        row = {
            # v2 CARRIES `plan`. A version int that means two different
            # shapes is not a version -- a v1 root could omit plan or carry
            # anything under it and still pass an exact-schema reader, which
            # is producer-era ambiguity encoded inside one number. Rows that
            # bear planning evidence are v2 and the reader validates them
            # separately.
            "v": 2,
            "type": "sharded-root",
            "token": record_context["token"],
            "pid": ident["pid"],
            "start": ident["start"],
            "owner_pid": record_context["root_pid"],
            "owner_start": record_context["root_start"],
            "bins": [list(modules) for modules in bins],
            # (a) OF THE AUTHORITY CONTRACT: record WHAT WAS PLANNED, in a
            # form a later recognizer can compare against an independent
            # re-derivation. Recording it widens nothing today -- no
            # recognizer reads it and v7 stays withdrawn -- which is exactly
            # why it is safe to land before the recognizer is designed.
            "plan": {
                "digest": plan_digest(expected_counts, expected_ids),
                "modules": sorted(expected_counts),
                "planned": sum(expected_counts.values()),
            },
            "assignments": assignments,
            "samples": samples,
            "counts": {
                "ran": ran,
                "skipped": skipped,
                "failures": failures,
                "errors": errors,
                "expected_failures": expected_failures,
                "unexpected_successes": unexpected_successes,
                "ok": not failed,
            },
            "rc": rc,
        }
        try:
            _recorder().write_json(
                record_context["directory"],
                _recorder().artifact_basename(row)[:-len(".json")], row)
        except Exception:
            pass
    return text, rc


def discover_suite():
    return unittest.defaultTestLoader.discover(
        "tests", pattern="test*.py", top_level_dir=".")


def _inner_artifact_digest(context, inner_pid, inner_start, assigned):
    """Locate, verify and hash the inner worker's OWN evidence row. -> hex|None

    Exactly one artifact must exist for this token+shard+inner pid, it must
    validate, and every identity field must match the chain this supervisor
    is about to attest. None means REFUSE: a missing row, an extra row, a row
    that fails validation, or one whose identity disagrees are all cases
    where the hop cannot be witnessed, and an unwitnessed hop is UNKNOWN.
    """
    rec = _recorder()
    directory = context.get("directory")
    if not directory:
        return None
    want = "%s-worker-%s-%s.json" % (
        context["token"], context.get("shard") or 0, inner_pid)
    path = os.path.join(directory, want)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        row = rec.validate_artifact(json.loads(raw.decode("utf-8")),
                                    context["token"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if row.get("pid") != inner_pid or row.get("start") != inner_start:
        return None
    # ROLE AND SHARD, NOT JUST IDENTITY. Measured: a producer-minted row with
    # role="serial" or shard=2 validated cleanly and received a digest, so the
    # supervisor would have attested a row that belongs to a different run
    # shape or a different shard of the same run. The filename carries both,
    # but a filename is a convention and the row is the evidence.
    if row.get("role") != "worker":
        return None
    if row.get("shard") != context.get("shard"):
        return None
    ident = rec.process_identity()
    if row.get("root_pid") != ident["pid"] \
            or row.get("root_start") != ident["start"]:
        return None
    if sorted(row.get("modules") or ()) != sorted(assigned):
        return None
    return hashlib.sha256(raw).hexdigest()


def _supervise(index, manifest_path, protocol_path, meta_path):
    """Own an inner test process; certify only after it is dead and swept.

    WHY A THIRD PROCESS EXISTS. Containment inside the worker cannot close the
    last door: a test may register an atexit handler that spawns, and that
    handler runs STRICTLY AFTER the worker's final sweep, after its meta is
    written, on the way out. Measured: survived=True rc=0 ok=True.

    The obvious cure, os._exit past interpreter shutdown, is REFUSED BY THIS
    SUITE: tests/test_gateshard.py pins that a worker RUNS test-registered
    atexit handlers and JOINS non-daemon threads, and real tests rely on both.
    Bypassing shutdown would close a hostile door by breaking an honest
    contract, so the containment moves OUT instead of the shutdown being cut
    short.

    THIS PROCESS NEVER IMPORTS A TEST. That is its entire authority: nothing a
    test can monkeypatch, register or spawn into has ever run here. It arms
    PR_SET_CHILD_SUBREAPER BEFORE the inner process exists, so everything the
    inner orphans -- including anything its atexit handlers spawn on the way
    out -- reparents HERE rather than to init, and is still attributable after
    the inner interpreter is gone.

    The inner worker writes its meta to a STAGING path. This process promotes
    staging to the real meta only after inner death AND a proven sweep, so a
    module whose work outlived it is never certified; the promotion is a
    rename, so the parent either sees a complete meta or none.
    """
    staging = meta_path + ".staging"
    for stale in (staging, meta_path):
        try:
            os.unlink(stale)
        except OSError:
            pass
    if not _subreaper_armed():
        # REFUSE BEFORE RUNNING, NOT AFTER. An earlier version computed
        # `armed`, launched the inner anyway, and only declined to
        # CERTIFY at the end -- so tests EXECUTED without containment
        # authority and anything they spawned was unattributable from
        # the first instant. The worker already refuses on exactly this
        # condition; the supervisor computed the answer and ignored it.
        sys.stderr.write("supervisor could not arm containment; "
                         "refusing to run shard %d\n" % index)
        return 70
    # The supervisor must NAME the modules it supervised: the chain requires
    # the same module multiset at every hop, and the inherited record context
    # carries a shard but not an assignment. Read it from the manifest the
    # parent already wrote rather than inventing a second channel.
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            assigned = list(json.load(fh).get("modules") or ())
    except (OSError, ValueError, AttributeError):
        assigned = []
    rec = _recorder()
    context = rec.consume_context("worker") \
        if os.environ.get("HELM_GATE_RECORD_ROLE") == "worker" else None
    env = dict(os.environ)
    env["HELM_GATESHARD_WORKER"] = "1"
    if context:
        # RE-PARENT THE RECORD IDENTITY. The inner process answers to THIS
        # process, not to the pool, so its row must name the supervisor as
        # root -- that is what makes the chain exact immediate-parent identity
        # at every hop instead of two rows both pointing at the pool.
        ident = rec.process_identity()
        context = dict(context,
                       modules=list(context.get("modules") or assigned))
        env = rec.child_env(dict(context, root_pid=ident["pid"],
                                 root_start=ident["start"]),
                            "worker", shard=context.get("shard"), base=env)
    if os.environ.get("HELM_GATE_RECORD_ROLE") == "worker" and not context:
        # A DECLARED MEASURED ROLE THAT CANNOT BE CONSUMED IS A REFUSAL, NOT A
        # DOWNGRADE. Measured 2026-08-26: HELM_GATE_RECORD_ROLE=worker with
        # the rest of the context absent ran the module and returned rc=0 --
        # a run that ANNOUNCED it would produce evidence, produced none, and
        # certified anyway. Silently falling back to unmeasured is the worst
        # of the three options: the caller asked for evidence and got a green
        # answer instead.
        sys.stderr.write("shard %d declares a measured role with an "
                         "unusable record context; refusing to run\n" % index)
        return 70
    inner = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker", str(index),
         manifest_path, protocol_path, staging],
        env=env, start_new_session=True)
    inner_start = rec.process_start(inner.pid)
    limit = _worker_deadline()
    started, overdue = time.monotonic(), False
    while inner.poll() is None:
        if limit and time.monotonic() - started > limit:
            overdue = True
            _reap_tree(inner)
            break
        time.sleep(0.02)
    if inner.poll() is None:
        inner.wait()
    # THE EXIT THIS PROCESS REPORTS FOR THE INNER. A worker killed by signal
    # N waits as -N, and `SystemExit(-N)` exits 256-N, so a TERM death reached
    # the pool as "exited 241" (task/3075). 128+N is the encoding
    # gatechild's supervisor and guard give a signal death, and the one
    # `_wait_status` reads back as a signal. The witness below keeps the raw
    # wait status: it records what the inner did, not what this exit says.
    code = inner.returncode if inner.returncode >= 0 \
        else 128 - inner.returncode
    # AFTER inner death, never before: only now is every atexit handler it
    # owned finished, so only now is a survivor certainly not this run's.
    swept = _sweep_own_descendants()
    # BIND THE WORKER ARTIFACT, NOT THE META FILE. `staging` is the shard
    # meta JSON this process promotes; the worker's EVIDENCE is a separate
    # row run_controlled writes under the record directory. An earlier
    # version hashed staging and called the result inner_artifact_digest --
    # the field name promised the evidence and the value was the summary, so
    # a tampered or replaced worker row was entirely unwitnessed.
    digest = _inner_artifact_digest(context, inner.pid, inner_start, assigned) \
        if context else None
    if context:
        try:
            row = rec.validate_supervisor_artifact(
                rec.supervisor_artifact(context, inner.pid, inner_start,
                                        digest, inner.returncode, swept),
                context["token"])
            rec.write_supervisor_artifact(context["directory"], row)
        except Exception as exc:
            # A witness that cannot be written is a hop nobody can verify;
            # refuse rather than certify an unwitnessed generation -- AND SAY
            # WHY. A refusal that does not name its origin sent me hunting a
            # sweep failure that was really a schema error.
            sys.stderr.write("supervisor witness refused for shard %d: %s: "
                             "%s\n" % (index, type(exc).__name__, exc))
            swept = False
    if overdue:
        sys.stderr.write("shard %d exceeded the %ds worker deadline\n"
                         % (index, limit))
    # The digest is REQUIRED only where evidence exists to bind: an
    # unmeasured run writes no worker artifact, so demanding one there would
    # refuse every ordinary shard. In MEASURED mode a missing digest is a
    # missing hop and refuses -- that is the case authority actually rests on.
    # EVIDENCE CLAUSES ARE NOT CHECKED HERE. sweep_ok and a bound digest are
    # the CERTIFYING contract and they are enforced once, by
    # certifying_supervisor_artifact at the promotion below. Checking them
    # here too made that call a TAUTOLOGY -- it could never fire, which reads
    # to the next person as protection while providing none. This guard keeps
    # only the OPERATIONAL preconditions: a deadline kill, and nothing staged
    # to promote.
    # `staging` absent is its OWN precondition, not a corollary of the digest.
    # Folding it into the digest check made the digest carry two meanings, and
    # scoping the digest to measured mode silently dropped the second one --
    # a crashed shard then reached os.replace with nothing to promote.
    staged = os.path.exists(staging)
    # `not swept` GATES EVERY SHARD, MEASURED OR NOT. Consolidating the
    # evidence clauses into certifying_supervisor_artifact was right for the
    # DIGEST -- which only exists in measured mode -- and WRONG for the sweep:
    # that validator runs only under `if context`, so moving the sweep check
    # behind it makes containment FAIL-OPEN for every ordinary shard.
    # Removing a tautology and opening a hole in the same edit is the shape
    # of every repair that goes further than the defect.
    if overdue or not staged or not swept:
        try:
            os.unlink(staging)
        except OSError:
            pass
        # PROPAGATE THE INNER EXIT WHEN CONTAINMENT HELD. The supervisor's own
        # rc is about CONTAINMENT; the inner's is about the RUN, and the
        # parent's crash block quotes it to say what actually happened
        # ("exited 7"). Returning a supervisor code over a healthy-containment
        # failure computes the real answer and then discards it one frame
        # before anyone can read it -- the reader is told the process died
        # of the wrong thing.
        if not swept:
            return 70
        if not overdue:
            return code
        return 1
    # THE CERTIFYING CLAUSES ARE ENFORCED HERE, AT THE PROMOTION, and until
    # now they were only DEFINED. certifying_supervisor_artifact demands
    # sweep_ok True and a bound digest -- the difference between an honest
    # witness that records its own failure and a row fit to authorize the
    # module it describes. A verb that is defined and never called reads to
    # the next person exactly like a check that runs.
    if context:
        try:
            rec.certifying_supervisor_artifact(
                rec.supervisor_artifact(context, inner.pid, inner_start,
                                        digest, inner.returncode, swept),
                context["token"])
        except Exception as exc:
            sys.stderr.write(
                "shard %d refused certification: %s: %s\n"
                % (index, type(exc).__name__, exc))
            try:
                os.unlink(staging)
            except OSError:
                pass
            return 70
    os.replace(staging, meta_path)
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--worker":
        if len(argv) != 5 or os.environ.get("HELM_GATESHARD_WORKER") != "1":
            return 2
        try:
            index = int(argv[1])
            with open(argv[2], encoding="utf-8") as fh:
                manifest = json.load(fh)
            modules = manifest.get("modules")
            expected = manifest.get("counts")
            expected_ids = manifest.get("ids")
        except (AttributeError, OSError, ValueError, TypeError):
            return 2
        if type(modules) is not list or not all(
                isinstance(name, str) and name for name in modules) \
                or type(expected) is not dict or set(expected) != set(modules) \
                or not all(type(value) is int and value >= 0
                           for value in expected.values()) \
                or type(expected_ids) is not dict \
                or set(expected_ids) != set(modules) \
                or not all(type(rows) is list
                           and len(rows) == expected[name]
                           and all(isinstance(test_id, str) and test_id
                                   for test_id in rows)
                           for name, rows in expected_ids.items()):
            return 2
        return _worker(index, modules, expected, argv[3], argv[4],
                       expected_ids=expected_ids)
    if argv and argv[0] == "--supervise":
        if len(argv) != 5 or os.environ.get("HELM_GATESHARD_WORKER") != "1":
            return 2
        try:
            index = int(argv[1])
        except ValueError:
            return 2
        return _supervise(index, argv[2], argv[3], argv[4])
    if argv and argv[0] == "--plan":
        if len(argv) != 3 \
                or os.environ.get("HELM_GATESHARD_PLANNER") != "1":
            return 2
        _suite, groups = discover_plan()
        counts = [[name, len(tests)] for name, tests in groups]
        try:
            _plan_counts(counts)
            execution_groups(counts)
            ids = {name: _recorder().planned_ids(unittest.TestSuite(tests))
                   for name, tests in groups}
            for path, content in ((argv[1], counts), (argv[2], ids)):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(content, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
        except (OSError, RuntimeError, ValueError):
            return 2
        return 0
    if argv:
        return 2
    sys.stderr.write(DIAGNOSTIC_MARKER + "\n")
    sys.stderr.flush()

    import tempfile
    record_context = _recorder().consume_context("sharded") \
        if os.environ.get("HELM_GATE_RECORD_ROLE") == "sharded" else None
    with tempfile.TemporaryDirectory(prefix="helm-gate-shards-") as tmp:
        plan = os.path.join(tmp, "plan.json")
        ids_path = os.path.join(tmp, "plan-ids.json")
        noise = os.path.join(tmp, "planner.noise")
        env = _fresh_env("HELM_GATESHARD_PLANNER")
        planner_tmp = os.path.join(tmp, "planner-tmp")
        os.makedirs(planner_tmp)
        env["TMPDIR"] = planner_tmp
        planner_argv = [
            sys.executable, os.path.abspath(__file__), "--plan", plan, ids_path,
        ]
        with open(noise, "w", encoding="utf-8") as err:
            planner = subprocess.run(
                planner_argv, stdout=subprocess.DEVNULL, stderr=err, env=env)
        if planner.returncode:
            try:
                with open(noise, encoding="utf-8") as fh:
                    sys.stderr.write(fh.read())
            except OSError:
                pass
            sys.stderr.write(
                "gateshard: unittest discovery plan unavailable; result refused\n")
            return 2
        try:
            with open(plan, encoding="utf-8") as fh:
                groups = json.load(fh)
            expected = _plan_counts(groups)
            with open(ids_path, encoding="utf-8") as fh:
                expected_ids = json.load(fh)
            bins = canonical_bins(groups)
        except (OSError, TypeError, ValueError, RuntimeError):
            sys.stderr.write(
                "gateshard: unittest discovery plan unreadable; result refused\n")
            return 2
        protocol, rc = run_bins(
            bins, tmp, expected_counts=expected, expected_ids=expected_ids,
            record_context=record_context,
            max_parallel=worker_count(len(groups)))
    sys.stderr.write(protocol)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
