#!/usr/bin/env python3
"""Opt-in, process-owned unittest outcome records for gate equivalence."""
import collections
import inspect
import json
import os
import re
import string
import sys
import tempfile
import time
import unittest


_DIR = "HELM_GATE_RECORD_DIR"
_TOKEN = "HELM_GATE_RECORD_TOKEN"
_ROLE = "HELM_GATE_RECORD_ROLE"
_ROOT_PID = "HELM_GATE_RECORD_ROOT_PID"
_ROOT_START = "HELM_GATE_RECORD_ROOT_START"
_SHARD = "HELM_GATE_RECORD_SHARD"
_TIMING = "HELM_GATE_RECORD_TIMING"
ENV_KEYS = (_DIR, _TOKEN, _ROLE, _ROOT_PID, _ROOT_START, _SHARD, _TIMING)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_ROLES = frozenset(("serial", "sharded", "worker"))
_EVENT_KINDS = frozenset((
    "start", "stop", "ok", "failure", "error", "skipped",
    "expected-failure", "unexpected-success", "subtest-ok",
    "subtest-failure", "subtest-error", "subtest-skipped",
))
_SUBTEST_KINDS = frozenset(kind for kind in _EVENT_KINDS
                           if kind.startswith("subtest-"))
_FIXTURE_ID = re.compile(
    r"^(setUpClass|tearDownClass|setUpModule|tearDownModule) \(([^()]+)\)$")
COUNT_FIELDS = (
    "ran", "skipped", "failures", "errors", "expected_failures",
    "unexpected_successes",
)
TIMING_TOP = 20
# The census names a few of whatever it could not account for and counts the
# rest. Both caps exist so the diagnosis cannot grow with the suite and push
# the advisory event over the ledger's per-event byte limit.
TIMING_NAME_CAP = 6
TIMING_REASON_CAP = 2000
# The class-skip detail rides the same advisory event and is printed whole by
# `helm gate show`, so it is held under the gate's one-line display cap.
TIMING_SKIP_CAP = 480
TIMING_SKIP_REASON_CAP = 80
_SETUP_FIXTURES = ("setUpClass", "setUpModule")


def _stat(pid):
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return None
    _head, marker, tail = text.rpartition(")")
    fields = tail.split() if marker else []
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def process_start(pid=None):
    row = _stat(os.getpid() if pid is None else pid)
    return row[1] if row else None


def process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    start = process_start(pid)
    return {"pid": pid, "start": start} if start is not None else None


def _memory():
    rows = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                name, _colon, value = line.partition(":")
                if name in ("MemAvailable", "MemFree"):
                    rows[name] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return rows


def _competing_suites(exclude=()):
    rows = []
    excluded = set(exclude)
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                argv = [part.decode("utf-8", "replace")
                        for part in fh.read().split(b"\0") if part]
        except OSError:
            continue
        joined = " ".join(argv)
        if pid not in excluded and (
                "gateshard.py" in joined and "--worker" not in argv
                or "-m unittest discover -s tests" in joined):
            rows.append({"pid": pid, "start": process_start(pid), "argv": argv})
    return sorted(rows, key=lambda row: row["pid"])


def system_sample(phase=None, workers=None, exclude=None):
    try:
        load = list(os.getloadavg())
    except OSError:
        load = []
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    row = {
        "load": load,
        "affinity_cores": affinity,
        "host_cores": os.cpu_count(),
        "memory": _memory(),
        "competing_suites": _competing_suites(exclude or ()),
    }
    if phase is not None:
        row["phase"] = phase
    if workers is not None:
        row["workers"] = list(workers)
    return row


def _descends_from(pid, root_pid, root_start):
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        row = _stat(pid)
        if not row:
            return False
        parent, start = row
        if pid == root_pid:
            return start == root_start
        pid = parent
    return False


def context_from_env(role=None):
    values = {name: os.environ.get(name, "") for name in ENV_KEYS}
    if not any(values.values()):
        return None
    try:
        root_pid = int(values[_ROOT_PID])
        root_start = int(values[_ROOT_START])
        shard = int(values[_SHARD]) if values[_SHARD] else None
    except ValueError:
        return None
    token = values[_TOKEN]
    selected_role = values[_ROLE]
    directory = values[_DIR]
    if selected_role not in _ROLES or role and selected_role != role \
            or not _TOKEN_RE.fullmatch(token) \
            or not os.path.isabs(directory) or not os.path.isdir(directory) \
            or root_pid <= 1 or root_start <= 0 \
            or values[_TIMING] not in ("", "1") \
            or not _descends_from(os.getpid(), root_pid, root_start):
        return None
    return {
        "directory": directory,
        "token": token,
        "role": selected_role,
        "root_pid": root_pid,
        "root_start": root_start,
        "shard": shard,
        "timing": values[_TIMING] == "1",
    }


def child_env(context, role, shard=None, base=None):
    if role not in _ROLES:
        raise ValueError("unknown measurement role")
    env = dict(os.environ if base is None else base)
    for key in ENV_KEYS:
        env.pop(key, None)
    env.update({
        _DIR: context["directory"],
        _TOKEN: context["token"],
        _ROLE: role,
        _ROOT_PID: str(context["root_pid"]),
        _ROOT_START: str(context["root_start"]),
    })
    if shard is not None:
        env[_SHARD] = str(shard)
    return env


def _test_id(test):
    try:
        value = test.id()
    except (AttributeError, TypeError) as exc:
        raise ValueError("unittest identity is unreadable") from exc
    value = str(value or "")
    fixture = _FIXTURE_ID.fullmatch(value)
    if fixture:
        return "%s.%s" % (fixture.group(2), fixture.group(1))
    if not value:
        raise ValueError("unittest identity is empty")
    return value


def planned_ids(suite):
    """Discovery-order test ids; ordinary outcome evidence needs no module."""
    rows = []
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            rows.extend(planned_ids(test))
        else:
            rows.append(_test_id(test))
    return rows


def planned_tests(suite):
    """Discovery-order (test id, owning module) pairs for timing only."""
    rows = []
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            rows.extend(planned_tests(test))
        else:
            module = getattr(test.__class__, "__module__", "")
            if not isinstance(module, str) or not module:
                raise ValueError("unittest test module is unreadable")
            rows.append((_test_id(test), module))
    return rows


class _Ledger:
    def __init__(self, planned, planned_modules=None):
        self.planned = list(planned)
        self.timing = planned_modules is not None
        self.planned_modules = list(planned_modules or ())
        self.started = []
        self.stopped = []
        self.events = []
        self.valid = True
        self._running = {}
        self._timings = collections.defaultdict(
            lambda: {"tests": 0, "wall": 0.0, "process_cpu": 0.0})

    def _event(self, name, kind, subtest=None, detail=None):
        row = {"test": name, "kind": kind}
        if subtest is not None:
            row["subtest"] = subtest
        if detail is not None:
            row["detail"] = " ".join(str(detail).split())
        self.events.append(row)

    def start(self, test, name):
        module = getattr(test.__class__, "__module__", "")
        if self.timing and (not isinstance(module, str) or not module):
            self.valid = False
        clocks = (time.perf_counter(), time.process_time()) \
            if self.timing else (None, None)
        self._running[id(test)] = (name, module) + clocks
        self.started.append(name)
        self._event(name, "start")

    def stop(self, test, name):
        running = self._running.pop(id(test), None)
        if running is None:
            self.valid = False
        else:
            name = running[0]
            if self.timing:
                module, wall, cpu = running[1:]
                timing = self._timings[module]
                timing["tests"] += 1
                timing["wall"] += max(0.0, time.perf_counter() - wall)
                timing["process_cpu"] += max(0.0, time.process_time() - cpu)
        self.stopped.append(name)
        self._event(name, "stop")

    def event(self, name, kind, subtest=None, detail=None):
        self._event(name, kind, subtest=subtest, detail=detail)

    def skip_owner(self, test, name):
        running = self._running.get(id(test))
        if running is not None:
            return running[0], None
        if len(self._running) == 1:
            return next(iter(self._running.values()))[0], name
        return name, None


def _prepare_skip(ledger, values):
    test = values["test"]
    name = _test_id(test)
    owner, subtest = ledger.skip_owner(test, name)
    return "event", owner, "subtest-skipped" if subtest else "skipped", \
        subtest, values["reason"]


def _prepare_subtest(_ledger, values):
    test = values["test"]
    err = values["err"]
    if err is None:
        kind = "subtest-ok"
    else:
        kind = "subtest-failure" if issubclass(
            err[0], test.failureException) else "subtest-error"
    return "event", _test_id(test), kind, _test_id(values["subtest"]), None


_CALLBACKS = {
    "startTest": ("start", None),
    "stopTest": ("stop", None),
    "addSuccess": ("event", "ok"),
    "addFailure": ("event", "failure"),
    "addError": ("event", "error"),
    "addExpectedFailure": ("event", "expected-failure"),
    "addUnexpectedSuccess": ("event", "unexpected-success"),
    "addSkip": _prepare_skip,
    "addSubTest": _prepare_subtest,
}
_MISSING = object()


def _prepare_callback(handler, ledger, values):
    if callable(handler):
        return handler(ledger, values)
    operation, kind = handler
    test = values["test"]
    name = _test_id(test)
    if operation == "event":
        return operation, name, kind, None, None
    return operation, test, name, None, None


def _commit_callback(ledger, prepared):
    operation, target, value, subtest, detail = prepared
    if operation == "start":
        ledger.start(target, value)
    elif operation == "stop":
        ledger.stop(target, value)
    elif operation == "event":
        ledger.event(target, value, subtest=subtest, detail=detail)
    else:
        ledger.valid = False


def _callback_identity(result, name):
    value = getattr(result, name)
    return (
        getattr(value, "__self__", None),
        getattr(value, "__func__", value),
        getattr(result, "__dict__", {}).get(name, _MISSING),
    )


def _same_callback(left, right):
    return all(a is b for a, b in zip(left, right))


class _Observer:
    def __init__(self, result, ledger):
        if sys.getprofile() is not None:
            raise RuntimeError("unittest outcome profiling is already occupied")
        self.result = result
        self.ledger = ledger
        self.callbacks = {
            name: _callback_identity(result, name) for name in _CALLBACKS
        }
        codes = {}
        for name in _CALLBACKS:
            callback = getattr(result, name)
            code = getattr(getattr(callback, "__func__", callback),
                           "__code__", None)
            if code is None or code in codes:
                raise TypeError("unittest result callbacks are not observable")
            codes[code] = name
        self.pending = {}

        def profile(frame, event, _arg):
            if event == "call" and not self.pending:
                name = codes.get(frame.f_code)
                if name is None or frame.f_locals.get("self") is not result:
                    return
                names = frame.f_code.co_varnames
                count = frame.f_code.co_argcount + frame.f_code.co_kwonlyargcount
                values = dict(zip(names[:count], (
                    frame.f_locals.get(key, _MISSING) for key in names[:count])))
                positional = names[1:]
                canonical = {"test": values.get(positional[0], _MISSING)}
                if name == "addSkip":
                    canonical["reason"] = values.get(positional[1], _MISSING)
                elif name == "addSubTest":
                    canonical["subtest"] = values.get(positional[1], _MISSING)
                    canonical["err"] = values.get(positional[2], _MISSING)
                try:
                    prepared = _prepare_callback(
                        _CALLBACKS[name], ledger, canonical)
                except Exception:
                    ledger.valid = False
                    prepared = None
                self.pending[id(frame)] = prepared
            elif event == "return" and id(frame) in self.pending:
                prepared = self.pending.pop(id(frame))
                if prepared is not None:
                    _commit_callback(ledger, prepared)

        self.profile = profile
        sys.setprofile(profile)

    def stop(self):
        if sys.getprofile() is self.profile:
            sys.setprofile(None)
        else:
            self.ledger.valid = False
        if self.pending:
            self.ledger.valid = False
        for name, before in self.callbacks.items():
            if not _same_callback(before, _callback_identity(self.result, name)):
                self.ledger.valid = False


def _try_observe(result, ledger):
    try:
        observer = _Observer(result, ledger)
    except Exception:
        return None
    return observer.stop


def _counts(result):
    return {
        "ran": result.testsRun,
        "skipped": len(getattr(result, "skipped", ())),
        "failures": len(getattr(result, "failures", ())),
        "errors": len(getattr(result, "errors", ())),
        "expected_failures": len(getattr(result, "expectedFailures", ())),
        "unexpected_successes": len(getattr(result, "unexpectedSuccesses", ())),
        "ok": result.wasSuccessful(),
    }


def _remaining(planned, started):
    remaining = collections.Counter(started)
    rows = []
    for name in planned:
        if remaining[name]:
            remaining[name] -= 1
        else:
            rows.append(name)
    return rows


def _bounded_reason(headline, gaps, cap=TIMING_REASON_CAP):
    """The unchanged headline, then the diagnosis, clipped to a hard cap.

    The HEADLINE IS NEVER CLIPPED: it is the sentence consumers already match
    on, and a reason that lost it would be a different refusal rather than a
    better-explained one."""
    detail = "; ".join(gaps)
    room = cap - len(headline) - 2
    if room <= 0 or not detail:
        return headline
    if len(detail) > room:
        detail = detail[:max(0, room - 1)] + "…"
    return "%s: %s" % (headline, detail)


def _naming(names, cap=TIMING_NAME_CAP):
    """A BOUNDED sample of names plus how many were not named.

    The event carries a ledger byte limit and this text is the only place a
    partial census explains itself, so it names a few and counts the rest
    rather than growing with the suite."""
    shown = sorted(names)[:cap]
    tail = len(names) - len(shown)
    return ", ".join(shown) + (" and %d more" % tail if tail else "")


def _setup_skips(ledger):
    """{fixture id: skip reason} for each class or module whose SETUP raised
    SkipTest, and raised nothing else.

    unittest reports that as ONE skip on a stand-in (`_ErrorHolder`) named
    `setUpClass (mod.Class)` or `setUpModule (mod)`, never starts the tests
    under it, and never counts them in testsRun. `_test_id` spells the
    stand-in `mod.Class.setUpClass`. An ERROR on that same stand-in (the
    setup itself, or a cleanup after the skip) disqualifies it: an error is a
    failure to cover, never a skip."""
    skipped, broken = {}, set()
    for row in ledger.events:
        name = row.get("test")
        if not isinstance(name, str) or row.get("subtest") is not None \
                or name.rpartition(".")[2] not in _SETUP_FIXTURES:
            continue
        if row.get("kind") == "skipped":
            skipped.setdefault(name, row.get("detail") or "")
        else:
            broken.add(name)
    return {name: why for name, why in skipped.items() if name not in broken}


def _planned_modules(ledger):
    """{test id: planned module} — only when the two plan lists are parallel,
    which is how the serial timing runner builds them."""
    if len(ledger.planned) != len(ledger.planned_modules):
        return {}
    return dict(zip(ledger.planned, ledger.planned_modules))


def skipped_by_setup(ledger):
    """Split the planned-but-unstarted tests. -> (held, unstarted, fixtures)

    `held` maps each setup fixture id to the tests its SkipTest kept from
    starting. `unstarted` is every other planned test that never started, and
    only those still make a census partial. A test belongs to a class when its
    id is `<class>.<method>`, and to a module through the module it was
    planned under; the class is asked first, because a module skip means no
    class setup ever ran."""
    fixtures = _setup_skips(ledger)
    modules = _planned_modules(ledger)
    held = collections.defaultdict(list)
    unstarted = []
    for name in _remaining(ledger.planned, ledger.started):
        owners = [name.rpartition(".")[0] + ".setUpClass"]
        if name in modules:
            owners.append(modules[name] + ".setUpModule")
        owner = next((o for o in owners if o in fixtures), None)
        if owner is None:
            unstarted.append(name)
        else:
            held[owner].append(name)
    return dict(held), unstarted, fixtures


def _skip_detail(held, fixtures):
    """One bounded line naming each class (or module) whose setup skipped.

    It names classes while they FIT and counts the rest, so the count of
    unnamed classes is never the part that gets cut. Measured on a fab node:
    a plain clip cut the fifth class name in half and lost the tail count."""
    if not held:
        return None
    head = "%d test(s) in %d class(es) skipped by setup: " % (
        sum(len(names) for names in held.values()), len(held))
    parts = []
    for fixture in sorted(held)[:TIMING_NAME_CAP]:
        owner, _dot, kind = fixture.rpartition(".")
        why = " ".join(str(fixtures.get(fixture) or "no reason given").split())
        if len(why) > TIMING_SKIP_REASON_CAP:
            why = why[:TIMING_SKIP_REASON_CAP - 1] + "…"
        part = "%s%s (%d: %s)" % (
            owner, " [module]" if kind == "setUpModule" else "",
            len(held[fixture]), why)
        left = len(held) - len(parts) - 1
        suffix = " and %d more" % left if left else ""
        if parts and len(head + ", ".join(parts + [part]) + suffix) \
                > TIMING_SKIP_CAP:
            break
        parts.append(part)
    more = len(held) - len(parts)
    text = head + ", ".join(parts) + (" and %d more" % more if more else "")
    if len(text) > TIMING_SKIP_CAP:
        text = text[:TIMING_SKIP_CAP - 1] + "…"
    return text


def census_gaps(ledger, expected, measured_tests):
    """WHICH completeness inputs failed, in the census's own words.

    Completeness is a conjunction, and while it reported one fixed sentence
    the instrument could say a census was partial but never which input made
    it so. Every whole-suite receipt since the census landed carried that one
    sentence, so the ranking was withheld on evidence nobody could act on. A
    rung must name the input that failed.

    A TEST HELD BACK BY ITS CLASS'S SKIP IS NOT A GAP. When setUpClass raises
    SkipTest (tests.test_landreq_rewrite.ChainedRewriteTest, on a host with no
    git filter-repo), unittest records one skip and starts none of the class's
    tests. Counting them as never started made EVERY whole-suite timing
    UNKNOWN on that host. They are reported as skipped by class instead
    (`timing_summary`), and a module none of whose tests could start is not
    expected to contribute timing. A setup that raised an ERROR is still a
    gap, and so is a test with no recorded setup skip."""
    gaps = []
    if not expected:
        gaps.append("no test module was planned")
    if not ledger.valid:
        gaps.append("the outcome ledger is not valid")
    if ledger._running:
        gaps.append("%d test(s) started and never stopped"
                    % len(ledger._running))
    _held, unstarted, _fixtures = skipped_by_setup(ledger)
    if unstarted:
        gaps.append("%d planned test(s) never started (%s)"
                    % (len(unstarted), _naming(unstarted)))
    if measured_tests != len(ledger.started):
        gaps.append("timed %d test(s) against %d started"
                    % (measured_tests, len(ledger.started)))
    untimed = set(expected) - set(ledger._timings)
    if untimed:
        gaps.append("%d planned module(s) contributed no timing (%s)"
                    % (len(untimed), _naming(untimed)))
    unplanned = set(ledger._timings) - set(expected)
    if unplanned:
        gaps.append("%d timed module(s) were never planned (%s)"
                    % (len(unplanned), _naming(unplanned)))
    return gaps


def _expected_modules(ledger, held):
    """Planned modules that owe timing: every one with a test that could run.

    A module whose EVERY planned test was held back by a class or module
    setup skip has nothing to time, and demanding its timing would turn that
    skip back into UNKNOWN through the module census instead."""
    modules = _planned_modules(ledger)
    skipped = collections.Counter(modules[name] for names in held.values()
                                  for name in names if name in modules)
    planned = collections.Counter(ledger.planned_modules)
    return sorted(module for module in planned
                  if skipped[module] < planned[module])


def timing_summary(ledger):
    """Bounded module aggregates from the same callbacks as outcome evidence."""
    held, _unstarted, fixtures = skipped_by_setup(ledger)
    expected = _expected_modules(ledger, held)
    measured_tests = sum(row["tests"] for row in ledger._timings.values())
    gaps = census_gaps(ledger, expected, measured_tests)
    complete = not gaps
    values = list(ledger._timings.items())
    rows = [{"module": module, "tests": timing["tests"],
             "wall": round(timing["wall"], 6),
             "process_cpu": round(timing["process_cpu"], 6)}
            for module, timing in values]
    rows.sort(key=lambda row: (-row["wall"], row["module"]))
    reason = None if complete else _bounded_reason(
        "module timing census did not cover every planned test module", gaps)
    detail = _skip_detail(held, fixtures)
    # PRESENT ONLY WHEN SOMETHING WAS SKIPPED BY CLASS, so a census without
    # one is byte-identical to the version-1 record every reader already has.
    extra = {"skipped_by_class": detail} if detail else {}
    return dict(extra, **{
        "state": "COMPLETE" if complete else "UNKNOWN",
        "reason": reason,
        "planned_modules": len(expected),
        "measured_modules": len(rows),
        "measured_tests": measured_tests,
        "module_wall": round(sum(timing["wall"]
                                 for _module, timing in values), 6),
        "process_cpu": round(sum(timing["process_cpu"]
                                 for _module, timing in values), 6),
        # A partial census may name what it measured, but never presents a
        # confident ranking. UNKNOWN therefore carries no top list at all.
        "top": rows[:TIMING_TOP] if complete else [],
    })


def artifact(result, ledger, context):
    ident = process_identity()
    return {
        "v": 1,
        "token": context["token"],
        "role": context["role"],
        "pid": ident["pid"],
        "start": ident["start"],
        "root_pid": context["root_pid"],
        "root_start": context["root_start"],
        "shard": context.get("shard"),
        "modules": list(context.get("modules") or ()),
        "planned": ledger.planned,
        "started": ledger.started,
        "stopped": ledger.stopped,
        "unexecuted": _remaining(ledger.planned, ledger.started),
        "events": ledger.events,
        "counts": _counts(result),
    }


def write_json(directory, stem, row):
    fd, tmp = tempfile.mkstemp(prefix=".%s-" % stem, dir=directory)
    path = os.path.join(directory, stem + ".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(row, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_artifact(directory, row):
    return write_json(directory, artifact_basename(row)[:-len(".json")], row)


def timing_artifact(ledger, context):
    ident = process_identity()
    row = {"v": 1, "type": "module-timings", "token": context["token"],
           "role": context["role"], "pid": ident["pid"],
           "start": ident["start"], "root_pid": context["root_pid"],
           "root_start": context["root_start"]}
    row.update(timing_summary(ledger))
    return validate_timing_artifact(row, context["token"])


def skip_detail_ok(value):
    """The optional `skipped_by_class` field: one bounded, non-empty line."""
    return isinstance(value, str) and bool(value.strip()) \
        and len(value) <= TIMING_SKIP_CAP and "\n" not in value


def validate_timing_artifact(row, token=None):
    required = {"v", "type", "token", "role", "pid", "start",
                "root_pid", "root_start", "state", "reason",
                "planned_modules", "measured_modules", "measured_tests",
                "module_wall", "process_cpu", "top"}
    if type(row) is not dict or set(row) - {"skipped_by_class"} != required \
            or row.get("v") != 1 or row.get("type") != "module-timings":
        raise ValueError("module timing artifact schema is not version 1")
    if "skipped_by_class" in row and not skip_detail_ok(row["skipped_by_class"]):
        raise ValueError("module timing skipped-by-class detail is invalid")
    if not _TOKEN_RE.fullmatch(row["token"]) \
            or token is not None and row["token"] != token:
        raise ValueError("module timing artifact token does not match")
    if row["role"] != "serial":
        raise ValueError("module timing artifact is not serial")
    for name in ("pid", "start", "root_pid", "root_start"):
        if type(row[name]) is not int or row[name] <= 0:
            raise ValueError("module timing artifact %s is not positive" % name)
    for name in ("planned_modules", "measured_modules", "measured_tests"):
        if type(row[name]) is not int or row[name] < 0:
            raise ValueError("module timing artifact %s is invalid" % name)
    for name in ("module_wall", "process_cpu"):
        if type(row[name]) not in (int, float) or row[name] < 0:
            raise ValueError("module timing artifact %s is invalid" % name)
    if row["state"] not in ("COMPLETE", "UNKNOWN") \
            or (row["state"] == "COMPLETE") != (row["reason"] is None):
        raise ValueError("module timing completeness is invalid")
    if row["reason"] is not None and (not isinstance(row["reason"], str)
                                      or not row["reason"]):
        raise ValueError("module timing reason is invalid")
    top = row["top"]
    if type(top) is not list or len(top) > TIMING_TOP \
            or row["state"] == "UNKNOWN" and top:
        raise ValueError("module timing top list is invalid")
    for item in top:
        if type(item) is not dict or set(item) != {
                "module", "tests", "wall", "process_cpu"} \
                or not isinstance(item["module"], str) or not item["module"] \
                or len(item["module"]) > 512 \
                or type(item["tests"]) is not int or item["tests"] <= 0 \
                or type(item["wall"]) not in (int, float) or item["wall"] < 0 \
                or type(item["process_cpu"]) not in (int, float) \
                or item["process_cpu"] < 0:
            raise ValueError("module timing row is invalid")
    expected = sorted(top, key=lambda item: (-item["wall"], item["module"]))
    if top != expected or len({item["module"] for item in top}) != len(top):
        raise ValueError("module timing order is not deterministic")
    if row["state"] == "COMPLETE" and (
            not top or row["planned_modules"] != row["measured_modules"]
            or row["measured_modules"] < len(top)
            or row["measured_tests"] < row["measured_modules"]):
        raise ValueError("complete module timing census is inconsistent")
    return row


def timing_basename(row):
    return "%s-serial-%s.timing" % (row["token"], row["pid"])


def write_timing_artifact(directory, row):
    fd, tmp = tempfile.mkstemp(prefix=".module-timings-", dir=directory)
    path = os.path.join(directory, timing_basename(row))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(row, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_timing_artifact(path, token=None):
    with open(path, encoding="utf-8") as fh:
        return validate_timing_artifact(json.load(fh), token)


def _arm_runner(runner, ledger):
    results = []
    uninstrument = []
    try:
        had_own = "_makeResult" in getattr(runner, "__dict__", {})
        previous = runner._makeResult
    except (AttributeError, TypeError):
        return results, uninstrument, lambda: None

    def restore():
        if had_own:
            runner._makeResult = previous
        else:
            runner.__dict__.pop("_makeResult", None)

    def make_result():
        restore()
        result = previous()
        cleanup = _try_observe(result, ledger)
        if cleanup is not None:
            results.append(result)
            uninstrument.append(cleanup)
        return result

    runner._makeResult = make_result
    return results, uninstrument, restore


SUPERVISOR_FIELDS = (
    "v", "type", "token", "shard", "modules", "pid", "start",
    "root_pid", "root_start", "inner_pid", "inner_start",
    "inner_artifact_digest", "inner_rc", "sweep_ok",
)


def supervisor_artifact(context, inner_pid, inner_start, inner_digest,
                        inner_rc, sweep_ok):
    """PROCESS-CONTAINMENT evidence for one supervised module. -> dict

    A THIRD WITNESS, NOT A FAKE TestResult. The supervisor never imports a
    test and never runs one, so it has no planned/started/events to report;
    stuffing empty ones into a role="worker" row would make the artifact
    vocabulary lie about what observed what. This row says only what this
    process can witness: which inner generation it launched, what that
    generation returned, whether the post-death sweep came back clean, and
    which parent it answers to.

    Every hop carries EXACT IMMEDIATE-PARENT IDENTITY as pid+start, so the
    chain root -> supervisor -> worker is verifiable in both directions and
    a reused pid cannot impersonate a generation at any link.
    """
    ident = process_identity()
    return {
        "v": 1,
        "type": "worker-supervisor",
        "token": context["token"],
        "shard": context.get("shard"),
        "modules": list(context.get("modules") or ()),
        "pid": ident["pid"],
        "start": ident["start"],
        "root_pid": context["root_pid"],
        "root_start": context["root_start"],
        "inner_pid": inner_pid,
        "inner_start": inner_start,
        "inner_artifact_digest": inner_digest,
        "inner_rc": inner_rc,
        "sweep_ok": bool(sweep_ok),
    }


def validate_supervisor_artifact(row, token=None):
    """Typed validation for the supervisor witness. -> row (raises ValueError)

    Separate from validate_artifact on purpose: the two rows answer different
    questions and share only their token and identity discipline. A validator
    that accepted both would accept a worker row missing its whole ledger.
    """
    if not isinstance(row, dict) or set(row) != set(SUPERVISOR_FIELDS):
        raise ValueError("supervisor artifact fields do not match the contract")
    if row["type"] != "worker-supervisor" or row["v"] != 1:
        raise ValueError("supervisor artifact is not a v1 worker-supervisor")
    if token is not None and row["token"] != token:
        raise ValueError("supervisor artifact token does not match")
    for field in ("pid", "start", "root_pid", "root_start",
                  "inner_pid", "inner_start"):
        if type(row[field]) is not int:
            raise ValueError("supervisor artifact %s is not an int" % field)
    if not isinstance(row["modules"], list) or not row["modules"]:
        raise ValueError("supervisor artifact names no modules")
    if type(row["sweep_ok"]) is not bool:
        raise ValueError("supervisor artifact sweep_ok is not a bool")
    for field in ("pid", "start", "root_pid", "root_start",
                  "inner_pid", "inner_start"):
        if row[field] <= 0:
            raise ValueError("supervisor artifact %s is not positive" % field)
    # THE REPO ALREADY HAS A TOKEN GRAMMAR. A stricter one invented here
    # (32 hex) refuses the suite's own tokens -- a validator is the wrong
    # place to introduce a rule nothing else honours.
    if not isinstance(row["token"], str) \
            or not _TOKEN_RE.fullmatch(row["token"]):
        raise ValueError("supervisor artifact token is malformed")
    if type(row["shard"]) is not int or row["shard"] <= 0:
        raise ValueError("supervisor artifact shard is not a positive int")
    if not all(isinstance(name, str) and name for name in row["modules"]):
        raise ValueError("supervisor artifact modules are not names")
    if type(row["inner_rc"]) is not int:
        raise ValueError("supervisor artifact inner_rc is not an int")
    digest = row["inner_artifact_digest"]
    # string.hexdigits rather than a literal alphabet: a 16-char hex string
    # in source reads to the docref rung as a CITED COMMIT, and it refused
    # this very commit for a token that was never a citation.
    if digest is not None and (not isinstance(digest, str)
                               or len(digest) != 64
                               or any(ch not in string.hexdigits
                                      for ch in digest)
                               or digest != digest.lower()):
        raise ValueError("supervisor artifact digest is not 64 hex")
    return row


def certifying_supervisor_artifact(row, token=None):
    """validate_supervisor_artifact PLUS the clauses a CERTIFYING row owes.

    A witness row may honestly record a failure -- sweep_ok False, no digest --
    and still be a valid witness. A row used to CERTIFY may not: containment
    must have been proven and the inner evidence must have been bound. Two
    verbs, because conflating them would let a row that documents its own
    failure authorize the module it failed to contain.
    """
    validate_supervisor_artifact(row, token)
    if row["sweep_ok"] is not True:
        raise ValueError("a certifying supervisor row must witness a sweep")
    if not isinstance(row["inner_artifact_digest"], str):
        raise ValueError("a certifying supervisor row must bind a digest")
    return row


def artifact_basename(row):
    """The file a row IS written to, derived from the row. -> str

    Lives beside the writers on purpose: a name and the code that mints it
    drift the moment they live apart, and a reader that re-derives a filename
    slightly differently accepts a file the producer never wrote. Measured --
    a fixture invented "<token>-sharded-0-<pid>" for roots while the producer
    writes "<token>-sharded-root-<pid>": wrong in both halves, agreeing with
    itself, and green across a hundred arms.

    NOTE THE ASYMMETRY THIS CENTRALISES: the worker stem is built from
    row["role"], the root and supervisor stems from row["type"]. That is
    exactly the sort of thing two call sites re-derive differently.
    """
    kind = row.get("type")
    if kind == "sharded-root":
        return "%s-sharded-root-%s.json" % (row["token"], row["pid"])
    if kind == "worker-supervisor":
        return "%s-supervisor-%s-%s.json" % (
            row["token"], row.get("shard") or 0, row["pid"])
    return "%s-%s-%s-%s.json" % (
        row["token"], row["role"], row.get("shard") or 0, row["pid"])


def write_supervisor_artifact(directory, row):
    return write_json(directory, artifact_basename(row)[:-len(".json")], row)


_CALLBACK_ARGS = {
    "startTest": ("test",),
    "stopTest": ("test",),
    "addSuccess": ("test",),
    "addFailure": ("test", "err"),
    "addError": ("test", "err"),
    "addExpectedFailure": ("test", "err"),
    "addUnexpectedSuccess": ("test",),
    "addSkip": ("test", "reason"),
    "addSubTest": ("test", "subtest", "err"),
}


def controlled_result_class(ledger, base=None):
    """A TextTestResult that ledgers through unittest's OWN callbacks.

    THE CONTROLLED ADAPTER, and the point is what it does NOT do: it never
    touches sys.setprofile. The profile seam is a single global slot, so a
    recorder occupying it cannot observe the module that installs a recorder
    -- measured on tests.test_gateequiv, where four nested OutcomeRecorderTest
    arms had their inner record path go None, producing four ERRORs, no
    artifact, and a correctly UNKNOWN receipt. An instrument that breaks on
    its own tests cannot carry authority for a suite that contains them.

    IT SHARES THE EVENT VOCABULARY RATHER THAN RESTATING IT. Every method is
    generated from _CALLBACKS and routed through _prepare_callback and
    _commit_callback -- the same table and the same two functions the profile
    adapter uses. An earlier version hand-wrote all nine methods, which read
    fine and was a second implementation of one question: the day someone
    adds an outcome kind or changes how a skip finds its owner, a hand-written
    twin agrees until it silently does not, and the two adapters would then
    disagree about what a run contained while both looked correct.

    ONE recorder, ONE schema, ONE event vocabulary, TWO capture seams.
    """
    base = base or unittest.TextTestResult

    def _method(name, params):
        def hook(self, *args):
            # PREPARE BEFORE THE BASE MUTATES, COMMIT AFTER -- the SAME order
            # the profile seam uses (prepare on `call`, commit on `return`).
            # Preparing afterwards reads state the base has already changed,
            # so the two seams would answer differently about the same run
            # while sharing one table, which is the exact drift this adapter
            # was generated from one vocabulary to prevent.
            values = dict(zip(params, args))
            try:
                prepared = _prepare_callback(_CALLBACKS[name], ledger, values)
            except Exception:
                ledger.valid = False
                prepared = None
            # AND THE BASE'S RETURN IS THE HOOK'S RETURN. Discarding it makes
            # every generated method answer None, so any result method whose
            # caller reads its value silently loses it.
            outcome = getattr(super(cls, self), name)(*args)
            if prepared is not None:
                _commit_callback(ledger, prepared)
            return outcome
        hook.__name__ = name
        return hook

    body = {name: _method(name, _CALLBACK_ARGS[name]) for name in _CALLBACKS}
    cls = type("GateControlled%s" % base.__name__, (base,), body)
    return cls


def run_controlled(suite, directory, context, stream=None, verbosity=1):
    """Run `suite` under the controlled adapter. -> (result, artifact path)

    The callback sibling of run_recorded, which stays exactly as it is: this
    does not replace the profile recorder, it is the second capture adapter
    over the SAME ledger, schema and artifact validation, so a canonical
    worker never needs a recorder of its own.

    Returns path None when the ledger came out invalid or the artifact does
    not validate -- the caller publishes nothing and the parent reads that as
    UNKNOWN naming the module, which is the correct reading of an
    uninstrumented run.
    """
    try:
        ledger = _Ledger(planned_ids(suite))
    except Exception:
        return None, None
    runner = unittest.TextTestRunner(
        stream=stream, verbosity=verbosity,
        resultclass=controlled_result_class(ledger))
    result = runner.run(suite)
    if not ledger.valid:
        return result, None
    try:
        row = validate_artifact(artifact(result, ledger, context),
                                context["token"])
    except (ValueError, KeyError):
        return result, None
    try:
        return result, write_artifact(directory, row)
    except Exception:
        return result, None


def run_recorded(runner, suite, directory, context):
    try:
        ledger = _Ledger(planned_ids(suite))
    except Exception:
        return runner.run(suite), None
    results, uninstrument, restore = _arm_runner(runner, ledger)
    try:
        result = runner.run(suite)
    finally:
        restore()
        for cleanup in uninstrument:
            cleanup()
    if not results or results[0] is not result or not ledger.valid:
        return result, None
    try:
        row = validate_artifact(artifact(result, ledger, context),
                                context["token"])
    except ValueError:
        return result, None
    try:
        path = write_artifact(directory, row)
    except Exception:
        path = None
    return result, path


def _recording_runner(base, ledger, results, uninstrument):
    class RecordingRunner(base):
        def _makeResult(self):
            result = super()._makeResult()
            cleanup = _try_observe(result, ledger)
            if cleanup is not None:
                results.append(result)
                uninstrument.append(cleanup)
            return result

    RecordingRunner.__name__ = "GateRecording%s" % base.__name__
    return RecordingRunner


def consume_context(role):
    context = context_from_env(role)
    if context:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
    return context


def _find_program():
    frame = inspect.currentframe()
    try:
        while frame:
            for value in tuple(frame.f_locals.values()):
                if type(value) is unittest.TestProgram:
                    return value
            frame = frame.f_back
    finally:
        del frame
    return None


def arm_serial_from_env():
    context = context_from_env("serial")
    target = _find_program()
    if not context or target is None:
        return False
    original = unittest.TestProgram.runTests
    if getattr(original, "_helm_gate_record_armed", False):
        return False

    def run_tests(program):
        if program is not target:
            return original(program)
        unittest.TestProgram.runTests = original
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        if program.testRunner is not None:
            return original(program)
        try:
            if context.get("timing"):
                planned = planned_tests(program.test)
                ledger = _Ledger([name for name, _module in planned],
                                 [module for _name, module in planned])
            else:
                ledger = _Ledger(planned_ids(program.test))
            results = []
            uninstrument = []
            if context.get("timing"):
                class TimingRunner(unittest.TextTestRunner):
                    resultclass = controlled_result_class(ledger)
                program.testRunner = TimingRunner
            else:
                program.testRunner = _recording_runner(
                    unittest.TextTestRunner, ledger, results, uninstrument)
        except Exception:
            program.testRunner = None
            return original(program)
        completed = False
        try:
            value = original(program)
            completed = True
            return value
        except SystemExit:
            completed = getattr(program, "result", None) is not None
            raise
        finally:
            program.testRunner = None
            for cleanup in uninstrument:
                cleanup()
            result = getattr(program, "result", None)
            observed = result is not None if context.get("timing") else (
                bool(results) and results[0] is result)
            if completed and observed and ledger.valid:
                try:
                    if context.get("timing"):
                        timing = timing_artifact(ledger, context)
                        write_timing_artifact(context["directory"], timing)
                    else:
                        row = validate_artifact(artifact(result, ledger, context),
                                                context["token"])
                        write_artifact(context["directory"], row)
                except Exception:
                    pass

    run_tests._helm_gate_record_armed = True
    unittest.TestProgram.runTests = run_tests
    return True


def _string_list(value, name):
    if type(value) is not list or not all(isinstance(item, str) and item
                                           for item in value):
        raise ValueError("artifact %s is not a string list" % name)
    return value


def _fixture_owner(name):
    suffix = name.rsplit(".", 1)[-1]
    if suffix in ("setUpClass", "tearDownClass",
                  "setUpModule", "tearDownModule"):
        return name.rsplit(".", 1)[0]
    return None


def _validate_event_state(row):
    active = collections.defaultdict(list)
    fixtures = []
    for event in row["events"]:
        name = event["test"]
        kind = event["kind"]
        if kind == "start":
            active[name].append([])
        elif kind == "stop":
            if not active[name]:
                raise ValueError("stop event has no matching start")
            terminal = active[name].pop(0)
            if not terminal:
                raise ValueError("test has no terminal outcome")
        elif active[name]:
            active[name][0].append(kind)
        else:
            owner = _fixture_owner(name)
            if owner is None:
                raise ValueError("outcome event has no active test")
            fixtures.append(owner)
    if any(active.values()):
        raise ValueError("started test has no matching stop")
    if row["counts"]["ok"]:
        unexplained = [name for name in row["unexecuted"]
                       if not any(name.startswith(owner + ".")
                                  for owner in fixtures)]
        if unexplained:
            raise ValueError("successful run left tests unexecuted")


def validate_artifact(row, token=None):
    required = {
        "v", "token", "role", "pid", "start", "root_pid", "root_start",
        "shard", "modules", "planned", "started", "stopped", "unexecuted",
        "events", "counts",
    }
    if type(row) is not dict or set(row) != required or row.get("v") != 1:
        raise ValueError("artifact schema is not version 1")
    if not _TOKEN_RE.fullmatch(row["token"]) \
            or token is not None and row["token"] != token:
        raise ValueError("artifact token does not match this run")
    if row["role"] not in _ROLES:
        raise ValueError("artifact role is unknown")
    for name in ("pid", "start", "root_pid", "root_start"):
        if type(row[name]) is not int or row[name] <= 0:
            raise ValueError("artifact %s is not positive" % name)
    if row["shard"] is not None and (type(row["shard"]) is not int
                                     or row["shard"] <= 0):
        raise ValueError("artifact shard is invalid")
    for name in ("modules", "planned", "started", "stopped", "unexecuted"):
        _string_list(row[name], name)
    events = row["events"]
    if type(events) is not list:
        raise ValueError("artifact events are not a list")
    for event in events:
        if type(event) is not dict or set(event) - {
                "test", "kind", "subtest", "detail"} \
                or not isinstance(event.get("test"), str) \
                or not event["test"] or event.get("kind") not in _EVENT_KINDS \
                or (event.get("kind") in _SUBTEST_KINDS) != (
                    "subtest" in event) \
                or "subtest" in event and (not isinstance(event["subtest"], str)
                                             or not event["subtest"]) \
                or "detail" in event and not isinstance(event["detail"], str):
            raise ValueError("artifact event is malformed")
    counts = row["counts"]
    if type(counts) is not dict or set(counts) != set(COUNT_FIELDS) | {"ok"}:
        raise ValueError("artifact counts are malformed")
    for name in COUNT_FIELDS:
        if type(counts[name]) is not int or counts[name] < 0:
            raise ValueError("artifact count %s is invalid" % name)
    if type(counts["ok"]) is not bool:
        raise ValueError("artifact count ok is invalid")
    if collections.Counter(row["planned"]) != collections.Counter(
            row["started"] + row["unexecuted"]):
        raise ValueError("artifact planned census is incomplete")
    if collections.Counter(row["started"]) != collections.Counter(row["stopped"]):
        raise ValueError("artifact start/stop census disagrees")
    starts = collections.Counter(event["test"] for event in events
                                 if event["kind"] == "start")
    stops = collections.Counter(event["test"] for event in events
                                if event["kind"] == "stop")
    if starts != collections.Counter(row["started"]) \
            or stops != collections.Counter(row["stopped"]):
        raise ValueError("artifact event census disagrees with starts")
    if counts["ran"] != len(row["started"]):
        raise ValueError("artifact ran count disagrees with starts")
    event_counts = {
        "failures": sum(event["kind"] in ("failure", "subtest-failure")
                        for event in events),
        "errors": sum(event["kind"] in ("error", "subtest-error")
                      for event in events),
        "skipped": sum(event["kind"] in ("skipped", "subtest-skipped")
                       for event in events),
        "expected_failures": sum(event["kind"] == "expected-failure"
                                 for event in events),
        "unexpected_successes": sum(event["kind"] == "unexpected-success"
                                    for event in events),
    }
    for name, value in event_counts.items():
        if counts[name] != value:
            raise ValueError("artifact %s count disagrees with events" % name)
    expected_ok = not (counts["failures"] or counts["errors"]
                       or counts["unexpected_successes"])
    if counts["ok"] != expected_ok:
        raise ValueError("artifact ok disagrees with counts")
    _validate_event_state(row)
    return row


def read_artifact(path, token=None):
    with open(path, encoding="utf-8") as fh:
        return validate_artifact(json.load(fh), token)
