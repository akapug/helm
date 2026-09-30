#!/usr/bin/env python3
"""An assertion never holds ELAPSED WALL TIME under a ceiling: the audit for it.

THE CLASS. ``assertLess(time.monotonic() - t0, 2)`` asks how fast the machine
was, not what the code did. The operation under test is correct or it is not;
the number of seconds it took on this run is a property of the host, and a
ceiling on that number turns the host's load into the test's verdict.

WHY A LOADED HOST MAKES IT FLAKE. A sliced whole suite runs many modules at
once on one build node, beside other lanes' suites. A thread that is ready to
run can wait hundreds of milliseconds, sometimes seconds, before the scheduler
gives it a core. Nothing in the product is wrong, and the arm still goes red:
the tree records a 3 s ceiling that failed at 3.021 s and took four unrelated
lanes red in one hour (tests/test_gate_fifo.py). Load only ever LENGTHENS an
elapsed time, so a ceiling is the side it breaks.

WHAT COUNTS. The scan parses every ``.py`` under ``tests/`` and reports a
comparison that holds a SPAN under a bound:

* ``assertLess`` / ``assertLessEqual`` whose FIRST argument is a span;
* the mirrored ``assertGreater`` / ``assertGreaterEqual`` whose SECOND
  argument is a span (``assertGreater(5, elapsed)``);
* ``assertTrue(span < bound)``, ``assertFalse(span > bound)`` and a bare
  ``assert span < bound``, including a chained ``0.4 <= span < 5``;
* ``assertAlmostEqual(span, x, delta=d)``: a band carries a ceiling. Without
  ``delta`` it is equality to seven places, which only a deterministic value
  can pass, so it is not this class.

A SPAN is one of:

* a CLOCK DIFFERENCE written in the test: ``time.monotonic() - x``,
  ``time.time() - x``, ``time.perf_counter() - x`` and their ``_ns`` forms,
  under any alias the module imports (``import time as _t``,
  ``from time import monotonic``), where ``x`` is an earlier reading or a
  stored stamp, and the same difference taken between two stored readings
  (``t1 - t0`` once ``t1 = time.monotonic()``);
* a name, attribute, dict key or callee whose words include ``elapsed``,
  ``took``, ``waited``, ``spent`` or ``duration``;
* a local bound to either of those in the same function
  (``filed = time.monotonic() - started``), and a span scaled or rounded
  (``(time.monotonic() - t0) * 1000``, ``round(elapsed, 2)``). A rebind
  clears what a binding taught; a vocabulary name stays a span whatever it
  is bound to, since ``elapsed = self._run()`` is the tree's own spelling.

THE VOCABULARY IS THE TREE'S OWN. Every name that holds a measured span in a
ceiling here is spelled with one of the first four words (``elapsed``,
``elapsed_s``, ``waited``, ``waited_ms``, ``took``, ``spent``,
``hook_elapsed``); ``duration`` is the conventional synonym and names nothing
else in the tree. Three candidates are left out on purpose, each by
measurement: ``wall`` names the vendor quota wall far more often than the
clock (``hours_to_wall``, ``walls[0].pos``, a pane's wall event); ``dt`` is
the datetime spelling; ``wait`` without the past tense names a configured
bound (``PRECOMPACT_WAIT_S``, ``wait_s``), not a measurement. An ALL-CAPS
constant is a configured value and is never a span.

WHAT DOES NOT COUNT.

* A FLOOR: ``assertGreaterEqual(elapsed, 0.4)`` proves the wait happened, and
  load only makes it truer.
* An EPOCH POINT: ``time.time() - 3600`` or ``time.time() - FRESH_H * 3600``
  subtracts a fixed offset (a literal, an ALL-CAPS constant, arithmetic of
  those, or a local bound to one) and names a moment, not a span. An mtime
  compared with it is a staleness fixture.
* Ordering and counting: ``walls[0].pos < walls[1].pos``,
  ``order.index(a) < order.index(b)``, ``len(polls) <= 3``.
* A BARE clock reading: ``assertGreaterEqual(stamp, before)`` brackets a stamp
  between two readings, which holds under any load.
* A span the code under test reports from a clock THIS FUNCTION INJECTED: a
  function that passes a ``clock=``, ``sleep=`` or ``monotonic=`` seam reads
  the fake it handed over, so a reported ``out["waited_ms"]`` is
  deterministic there. A clock difference the test spells itself still reads
  the real clock and still counts.

THE REMEDY. Assert the operation's own EVENTS: the number of polls it made,
the number of lock attempts it took, the phase it was in when an event fired,
the event it waited on (``done.wait(HANG_S)``). Or hand it a fake clock
through a deterministic seam and assert the arithmetic. A floor that proves
the wait happened stays.

THE ALLOWLIST is the standing debt: every site the tree carries, keyed
``tests/<file>.py::<Class>.<method>`` with the number of sites in that
function, its kind, and why. It only shrinks: a site that is not listed, a
function with more sites than its count, and an entry that does not match
the scan (a converted site leaves its count too high) are each refused. Two
kinds:

* HANG: the bound leaves seconds of headroom, at least three seconds and at
  least five times what the operation itself costs, so it fails on a hang or
  on the defect the arm names and no plausible load reaches it.
* SPEED: less headroom than that. The bound sits within a small multiple of,
  or a second or two above, the operation's own time. A loaded host can
  cross it with no defect. It is DEBT: convert it to an event count or a fake
  clock when it flakes, and lower its count here.

WHAT THE SCAN CANNOT SEE, so a clean result is not a proof of absence. A
span unpacked from a call under a word outside the vocabulary
(``outcome, secs = wait_boot(...)``); an age the code under test reports
(``row["age_s"] < 5``), because ages are computed against an injected clock
as often as the real one and the name cannot tell which; a deadline form
(``assertLess(time.monotonic(), t0 + 5)``); a tolerance through ``abs()``;
a difference of readings stored on an attribute (``self.t1 - self.t0``); a
short event wait used as the verdict (``assertTrue(done.wait(1))``, or
``join(1)`` and then ``is_alive()``), which is the same load-sensitive
ceiling spelled as a timeout; and a program this suite writes out and runs
as a child, which is text to this parse.

WHY A TEST MODULE. The class lives in assertions that pass on a quiet host,
in modules no change imports, so it is listed in ``helm/gateaudits.py``
``RUNG_ARMS`` and runs with every focused plan. Stdlib only, and it reads
files without importing any of them.
"""
import ast
import collections
import os
import re
import tempfile
import textwrap
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))

#: The `time` functions whose reading, minus an earlier reading, is a span.
CLOCKS = frozenset(("monotonic", "monotonic_ns", "perf_counter",
                    "perf_counter_ns", "time", "time_ns"))

#: The words that name a measured span, read off the tree (module docstring,
#: "THE VOCABULARY IS THE TREE'S OWN").
SPAN_WORDS = frozenset(("elapsed", "took", "waited", "spent", "duration"))

#: Keyword seams that hand the code under test its clock. A function passing
#: one reads the fake it passed.
CLOCK_SEAMS = frozenset(("clock", "sleep", "monotonic"))

#: verb -> the argument that must stay SMALL for the assertion to pass.
CEILING_SIDE = {"assertLess": 0, "assertLessEqual": 0,
                "assertGreater": 1, "assertGreaterEqual": 1}
#: The unittest parameter names, by position, for a keyword call.
PARAMS = ("first", "second")
#: Calls that hand back the value they are given, in the same unit or a
#: coarser one.
PRESERVING = frozenset(("round", "int", "float"))

HANG, SPEED = "HANG", "SPEED"

REMEDY = ("Count the operation's own events or polls, or give it a fake "
          "clock through a deterministic seam: a wall-clock ceiling goes red "
          "on a loaded host with no defect. If it is a genuine HANG bound "
          "(seconds of headroom over a sub-second operation), add it to "
          "ALLOWLIST in tests/test_elapsed_ceilings.py with its kind and a "
          "reason. If the value is not wall time, the scan's vocabulary is "
          "wrong here: narrow it, never allowlist a non-span.")

Finding = collections.namedtuple("Finding", "path line form scope why")


def _words(name):
    if name.isupper():
        return frozenset()
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return frozenset(w for w in spaced.split("_") if w)


def _named(kind, name):
    if _words(name) & SPAN_WORDS:
        return ("%s %r" % (kind, name), False)
    return None


def _callee(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class _Env:
    """What one function knows at a statement: the locals bound to a span
    (name -> (why, spelled)), the locals bound to a fixed offset, the locals
    bound to a bare clock reading, and whether the function injects a
    clock."""

    def __init__(self, spans=None, fixed=(), injected=False, readings=()):
        self.spans = dict(spans or {})
        self.fixed = set(fixed)
        self.injected = injected
        self.readings = set(readings)

    def child(self, injected=None):
        return _Env(self.spans, self.fixed,
                    self.injected if injected is None else injected,
                    self.readings)


class _Scan:
    def __init__(self, path, tree):
        self.path = path
        self.findings = []
        self.modules, self.bare = {"time"}, set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.modules.update(a.asname or a.name for a in node.names
                                    if a.name == "time")
            elif isinstance(node, ast.ImportFrom) and node.module == "time":
                self.bare.update(a.asname or a.name for a in node.names
                                 if a.name in CLOCKS)

    # ---- what a span is -------------------------------------------------

    def clock(self, node):
        if not isinstance(node, ast.Call) or node.args or node.keywords:
            return False
        f = node.func
        if isinstance(f, ast.Attribute):
            return (f.attr in CLOCKS and isinstance(f.value, ast.Name)
                    and f.value.id in self.modules)
        return isinstance(f, ast.Name) and f.id in self.bare

    def offset(self, node, env):
        """A fixed span: `clock() - offset` is a MOMENT, not an elapsed one."""
        if isinstance(node, ast.Constant):
            return (isinstance(node.value, (int, float))
                    and not isinstance(node.value, bool))
        if isinstance(node, ast.UnaryOp):
            return self.offset(node.operand, env)
        if isinstance(node, ast.BinOp):
            return self.offset(node.left, env) and self.offset(node.right, env)
        if isinstance(node, ast.Name):
            return node.id.isupper() or node.id in env.fixed
        if isinstance(node, ast.Attribute):
            return node.attr.isupper()
        return False

    def span(self, node, env):
        """(why, spelled) when `node` is an elapsed span, else None. SPELLED
        is True for a clock difference written in the test, which reads the
        real clock whatever the function injects."""
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Sub):
                # A reading stored first (`t1 = time.monotonic()`) is the
                # same clock as one read in place: `t1 - t0` is a span.
                if self.clock(node.left) or (isinstance(node.left, ast.Name)
                                             and node.left.id in env.readings):
                    return (None if self.offset(node.right, env)
                            else ("a clock difference", True))
                return self.span(node.left, env)
            if isinstance(node.op, (ast.Mult, ast.Div, ast.FloorDiv)):
                got = self.span(node.left, env)
                if got is None and isinstance(node.op, ast.Mult) \
                        and self.offset(node.left, env):
                    got = self.span(node.right, env)
                return got
            return None
        if isinstance(node, ast.Name):
            if node.id in env.spans:
                why, spelled = env.spans[node.id]
                return ("%s bound to %r" % (why, node.id), spelled)
            return _named("named", node.id)
        if isinstance(node, ast.Attribute):
            return _named("attribute", node.attr)
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return _named("keyed", key.value)
            return self.span(node.value, env)
        if isinstance(node, ast.Call):
            name = _callee(node.func)
            if name in PRESERVING and node.args:
                return self.span(node.args[0], env)
            return _named("returned by", name) if name else None
        return None

    # ---- where a span meets a ceiling -----------------------------------

    def argument(self, call, index):
        if index < len(call.args):
            return call.args[index]
        for kw in call.keywords:
            if kw.arg == PARAMS[index]:
                return kw.value
        return None

    def compared(self, test, env, negated=False):
        """The span a comparison holds under a bound, or None."""
        left = test.left
        for op, right in zip(test.ops, test.comparators):
            small = isinstance(op, (ast.Lt, ast.LtE))
            large = isinstance(op, (ast.Gt, ast.GtE))
            if negated:
                small, large = large, small
            if small and self.span(left, env):
                return left
            if large and self.span(right, env):
                return right
            left = right
        return None

    def assertion(self, call, env, scope):
        verb = _callee(call.func)
        subject = None
        if verb in CEILING_SIDE:
            side = CEILING_SIDE[verb]
            subject = self.argument(call, side)
            form = "%s(%s)" % (verb, "span, bound" if side == 0
                               else "bound, span")
        elif verb == "assertAlmostEqual" and any(k.arg == "delta"
                                                 for k in call.keywords):
            form = "assertAlmostEqual(span, x, delta=...)"
            for index in (0, 1):
                arg = self.argument(call, index)
                if arg is not None and self.span(arg, env):
                    subject = arg
                    break
        elif verb in ("assertTrue", "assertFalse") and call.args \
                and isinstance(call.args[0], ast.Compare):
            subject = self.compared(call.args[0], env,
                                    negated=verb == "assertFalse")
            form = "%s(span %s bound)" % (verb, ">" if verb == "assertFalse"
                                          else "<")
        self.report(call.lineno, subject, form if subject else "", env, scope)

    def report(self, line, subject, form, env, scope):
        if subject is None:
            return
        got = self.span(subject, env)
        if got is None:
            return
        why, spelled = got
        if env.injected and not spelled:
            return
        self.findings.append(Finding(self.path, line, form, _key(scope), why))

    # ---- the walk --------------------------------------------------------

    def bind(self, stmt, env):
        if isinstance(stmt, ast.Assign):
            pairs = []
            for target in stmt.targets:
                if isinstance(target, (ast.Tuple, ast.List)):
                    values = (stmt.value.elts
                              if isinstance(stmt.value, (ast.Tuple, ast.List))
                              and len(stmt.value.elts) == len(target.elts)
                              else [None] * len(target.elts))
                    pairs.extend(zip(target.elts, values))
                else:
                    pairs.append((target, stmt.value))
        elif isinstance(stmt, ast.AnnAssign):
            pairs = [(stmt.target, stmt.value)]
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            pairs = [(t, None) for t in ast.walk(stmt.target)]
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            pairs = [(t, None) for item in stmt.items if item.optional_vars
                     for t in ast.walk(item.optional_vars)]
        else:
            return
        for target, value in pairs:
            if not isinstance(target, ast.Name):
                continue
            # REBINDING REPLACES what a name held, it never adds to it.
            env.spans.pop(target.id, None)
            env.fixed.discard(target.id)
            env.readings.discard(target.id)
            if value is None:
                continue
            if self.clock(value):
                env.readings.add(target.id)
                continue
            got = self.span(value, env)
            if got is not None:
                env.spans[target.id] = got
            elif self.offset(value, env):
                env.fixed.add(target.id)

    def block(self, body, env, scope):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                self.enter(stmt, env, scope)
                continue
            for node in _own_expressions(stmt):
                if isinstance(node, ast.Call):
                    self.assertion(node, env, scope)
            if isinstance(stmt, ast.Assert) \
                    and isinstance(stmt.test, ast.Compare):
                self.report(stmt.lineno, self.compared(stmt.test, env),
                            "assert span < bound", env, scope)
            self.bind(stmt, env)
            for inner in _blocks(stmt):
                self.block(inner, env, scope)

    def enter(self, node, env, scope):
        inner = scope + [(isinstance(node, ast.ClassDef), node.name)]
        if isinstance(node, ast.ClassDef):
            self.block(node.body, _Env(), inner)
            return
        # THE SEAM IS JUDGED ON THE KEYED FUNCTION, the test method a finding
        # is filed under, so a helper nested inside it shares its answer.
        keyed = _key(inner) != _key(scope)
        injected = (any(kw.arg in CLOCK_SEAMS for n in ast.walk(node)
                        if isinstance(n, ast.Call) for kw in n.keywords)
                    if keyed else None)
        self.block(node.body, env.child(injected), inner)


def _key(scope):
    """`Class.method` for a method, `func` for a module function; a def
    nested in either is filed under it."""
    if not scope:
        return "<module>"
    if scope[0][0] and len(scope) > 1:
        return "%s.%s" % (scope[0][1], scope[1][1])
    return scope[0][1]


def _blocks(stmt):
    for _field, value in ast.iter_fields(stmt):
        if not isinstance(value, list) or not value:
            continue
        if isinstance(value[0], ast.stmt):
            yield value
        elif isinstance(value[0], (ast.excepthandler, ast.match_case)):
            for part in value:
                yield part.body


def _own_expressions(stmt):
    """The expression nodes of `stmt` that are not in a nested statement."""
    stack = list(ast.iter_child_nodes(stmt))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.stmt, ast.excepthandler, ast.match_case)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def scan_source(source, path="<memory>"):
    """Every elapsed ceiling in one module's text, in source order: the one
    instrument the controls and the tree audit both drive."""
    tree = ast.parse(source)
    scan = _Scan(path, tree)
    scan.block(tree.body, _Env(), [])
    return sorted(scan.findings, key=lambda f: f.line)


def tree_files(root=HERE):
    """Every `.py` under `root`: test modules, helpers and fixtures alike,
    since a helper's assertion runs inside whichever arm calls it."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def scan_tree(root=HERE):
    found = []
    for path in tree_files(root):
        rel = "tests/" + os.path.relpath(path, root).replace(os.sep, "/")
        with open(path, encoding="utf-8") as fh:
            found.extend(scan_source(fh.read(), rel))
    return found


def audit(findings, allowlist):
    """What the findings owe the allowlist: an unlisted site, a function past
    its count, and an entry the scan does not match. Empty means clean."""
    by_key = collections.defaultdict(list)
    for f in findings:
        by_key["%s::%s" % (f.path, f.scope)].append(f)
    problems = []
    for key, sites in sorted(by_key.items()):
        entry = allowlist.get(key)
        if entry is None:
            problems.extend(
                "%s:%d %s holds %s under a ceiling, and %s is not on the "
                "allowlist. %s" % (f.path, f.line, f.form, f.why, key, REMEDY)
                for f in sites)
        elif len(sites) > entry[0]:
            problems.append(
                "%s holds %d elapsed ceilings and is allowlisted for %d; one "
                "is new (lines %s). %s" % (
                    key, len(sites), entry[0],
                    ", ".join(str(f.line) for f in sites), REMEDY))
    for key, entry in sorted(allowlist.items()):
        have = len(by_key.get(key, ()))
        if have < entry[0]:
            problems.append(
                "%s is allowlisted for %d elapsed ceilings and the scan finds "
                "%d: STALE. Lower its count, or remove the entry at zero; the "
                "list only shrinks." % (key, entry[0], have))
    return problems


# key -> (sites in that function, HANG or SPEED, why). Numbers in a reason are
# the bound over what the operation itself costs.
ALLOWLIST = {
    "tests/test_chat_v2.py::NodeSendLockTest.test_a_held_lock_delays_a_send_only_up_to_its_bound": (
        1, HANG, "5 s over a send whose lock wait is patched to 0.3 s; the "
                 "floor beside it proves the wait happened"),
    "tests/test_chat_v2.py::NodeSendLockTest.test_the_lock_is_per_node_so_another_node_never_waits_on_it": (
        1, HANG, "5 s over an unqueued send; queueing behind the other "
                 "node's lock would cost its patched 30 s wait"),
    "tests/test_chatnode_identity.py::PrepareLockTest.test_a_prepare_while_the_lock_is_held_refuses_and_names_it": (
        1, HANG, "5 s over a refusal that returns at once; a wait on the held "
                 "lock never ends"),
    "tests/test_chatnode_lock.py::UpClockExcludesCeremony.test_a_running_process_still_spends_the_wait": (
        1, SPEED, "4 s over a 1 s boot wait polled every 0.5 s: 3 s of "
                  "headroom over a timed wait"),
    "tests/test_chatnode_lock.py::UpClockExcludesCeremony.test_a_prepare_that_never_ends_is_still_bounded": (
        1, SPEED, "5 s over a wait capped near 1-1.5 s (START_TIMEOUT_S "
                  "patched to 1, 0.5 s polls): under five times the wait"),
    "tests/test_chatnode_lock.py::WaitBootIsMonotonic.test_a_wall_clock_set_back_does_not_stretch_the_wait": (
        1, SPEED, "3.5 s over a 1 s wait, below the 4 s valve that ends a "
                  "stretched one, so the headroom cannot widen here"),
    "tests/test_chatnode_posture.py::BootWaitTest.test_an_exited_unit_ends_the_wait_at_once": (
        1, SPEED, "3 s over an exit seen on the first 0.5 s poll, telling it "
                  "apart from the 4 s wait"),
    "tests/test_chatnode_posture.py::BootWaitTest.test_a_new_run_that_fails_inside_the_grace_is_an_exit_at_once": (
        1, HANG, "START_GRACE_S (10 s) over an exit seen within two 0.5 s "
                 "polls; the defect waits the 30 s boot wait"),
    "tests/test_chatnode_posture.py::BootWaitTest.test_up_against_a_hung_node_says_hung_at_once_in_status_words": (
        1, HANG, "5 s over a hung verdict at the first look; the defect waits "
                 "out the 8 s boot wait"),
    "tests/test_chatnode_posture.py::BootWaitTest.test_up_relays_a_refusal_at_once_with_its_cure": (
        1, HANG, "10 s over a refusal relayed at once; the defect waits the "
                 "30 s boot wait"),
    "tests/test_delivery_truth.py::TheOrcaDoorOutsideHelmTest.test_nothing_addressed_is_silent_and_inside_the_budget_cold": (
        1, SPEED, "the deliver hook's own 2 s spec timeout, which the hook "
                  "runner enforces as wall time, over a cold boundary "
                  "measured at 0.79 s: the budget is the product's contract"),
    "tests/test_dispatches.py::StorageSafetyTest.test_lock_contention_distinguishes_local_from_ambient_expiry": (
        1, SPEED, "0.5 s over a 0.05 s ambient deadline"),
    "tests/test_dispatches.py::TheLedgerRungStaysWithinItsBudgetTest.test_a_twenty_thousand_event_fold_stays_under_one_second": (
        1, SPEED, "1 s throughput budget on a 20,000-event fold, measured on "
                  "one laptop"),
    "tests/test_findingspass.py::EachOutcomeRecordsItsLineTest.test_a_timeout_stops_the_script_and_is_one_line": (
        1, HANG, "60 s over a 1 s script timeout; the fixture sleeps 120 s"),
    "tests/test_findingspass.py::DetachedAndQueuedTest.test_filing_returns_promptly_while_the_pass_sleeps": (
        1, HANG, "12 s over a sub-second filing; the pass it must not wait on "
                 "is held until released"),
    "tests/test_foldckpt.py::TheFingerprintsNameTheFile.test_every_restore_refreshes_recency": (
        1, HANG, "a refreshed mtime within 5 s of now against a planted one "
                 "60 s old; the gap is the restore alone"),
    "tests/test_foldckpt.py::TheFingerprintsNameTheFile.test_a_restore_racing_a_prune_is_linearised": (
        1, HANG, "10 s over a restore that must not wait on a prune paused "
                 "for up to 20 s"),
    "tests/test_foldckpt.py::TheFingerprintsNameTheFile.test_a_refresh_that_finds_the_lock_busy_skips_it": (
        1, SPEED, "REFRESH_WAIT_S + 1 s over a refresh that itself waits "
                  "REFRESH_WAIT_S (1 s) for the busy lock"),
    "tests/test_foldckpt.py::TheFingerprintsNameTheFile.test_a_refresher_that_moves_every_victim_ends_at_the_pass_cap": (
        1, HANG, "10 s over a capped prune pass; the defect is a pass that "
                 "never ends"),
    "tests/test_gate_fifo.py::GateQueueTest.test_timeout_kills_descendants_after_the_group_leader_exits": (
        1, HANG, "20 s over a 0.5 s gate timeout; waiting on the fixture "
                 "costs its 60 s sleep"),
    "tests/test_gate_fifo.py::GateQueueTest.test_timeout_kills_detached_descendants_without_waiting_for_pipe_eof": (
        1, HANG, "20 s over a 0.5 s gate timeout; waiting on the fixture "
                 "costs its 60 s sleep"),
    "tests/test_gate_fifo.py::GateQueueTest.test_completed_suite_reaps_an_adopted_detached_descendant": (
        1, HANG, "20 s over a gate run with a 2 s timeout; waiting on the "
                 "fixture costs its 60 s sleep"),
    "tests/test_gate_fifo.py::GateQueueTest.test_completed_suite_quiesces_a_forking_detached_tree": (
        1, HANG, "20 s over a gate run with a 2 s timeout; waiting on the "
                 "fixture costs its 60 s sleep"),
    "tests/test_gate_route.py::SshTransportTest.test_local_transport_timeout_kills_ssh_and_returns_named": (
        1, SPEED, "2 s over a 0.05 s transport timeout; the stub ssh sleeps "
                  "10 s"),
    "tests/test_gate_route.py::RemoteScriptShellTest.test_the_far_side_deadline_cancels_a_blocking_uname_stage": (
        1, HANG, "4 s over a 0.4 s session ceiling; the blocking stage never "
                 "returns on its own"),
    "tests/test_gate_route.py::RemoteScriptShellTest.test_TERM_in_the_launcher_start_tick_gap_is_bounded": (
        1, SPEED, "2 s over a shell that signals itself once its launcher is "
                  "up"),
    "tests/test_gate_route.py::RemoteScriptShellTest.test_TERM_before_helper_readiness_kills_the_wrapper_group": (
        1, SPEED, "2 s over a TERM cleanup; the unready wrapper sleeps 30 s"),
    "tests/test_gatechild_refusals.py::TheSupervisorReapsWhatItAdoptsTest.test_the_final_drain_does_not_block_on_a_LIVE_orphan": (
        1, SPEED, "1 s over a WNOHANG drain; the orphan it must not wait on "
                  "lives 30 s"),
    "tests/test_gateshard.py::WorkerDeadlineTest.test_a_hung_worker_is_killed_and_named": (
        1, HANG, "20 s over a 1 s worker deadline; the hung worker sleeps "
                 "30 s"),
    "tests/test_gateshard.py::MeasuredDeadlineTest.test_a_hung_worker_under_record_context_is_named_not_a_crash": (
        1, HANG, "25 s over a 2 s worker deadline; the hung probe sleeps "
                 "30 s"),
    "tests/test_handoff.py::CheckTest.test_a_PreCompact_waits_out_a_short_hold_on_the_state_lock_and_records": (
        1, SPEED, "the producer's own 0.4 s bound over a 0.1 s released "
                  "hold plus a hook run"),
    "tests/test_handoff.py::CheckTest.test_a_PreCompact_refused_past_its_bound_says_so_and_writes_nothing": (
        1, HANG, "the hook's 5 s timeout over a 0.4 s bounded wait"),
    "tests/test_harness.py::TheRpcDeadlineIsWallClockNotPerRecv.test_a_keepalive_trickle_is_bounded_by_wall_clock_and_named": (
        1, HANG, "5 s over a 0.5 s RPC deadline; the trickle never answers"),
    "tests/test_harness.py::TheRpcDeadlineIsWallClockNotPerRecv.test_send_and_recv_SHARE_one_budget_not_one_each": (
        1, HANG, "5 s over a 0.5 s RPC budget; assertBudgetIsShared carries "
                 "the claim"),
    "tests/test_harness.py::TheRpcDeadlineIsWallClockNotPerRecv.test_a_per_phase_implementation_is_REJECTED_by_the_budget_arithmetic": (
        1, HANG, "5 s over a 0.5 s RPC budget; the arithmetic rejects the "
                 "mutant"),
    "tests/test_harness.py::TheRpcDeadlineIsWallClockNotPerRecv.test_fleet_shares_one_budget_across_send_and_recv_too": (
        1, HANG, "5 s over a 0.5 s RPC budget; assertBudgetIsShared carries "
                 "the claim"),
    "tests/test_harness.py::TheRpcDeadlineIsWallClockNotPerRecv.test_a_send_that_eats_the_budget_never_reaches_recv": (
        1, HANG, "5 s over a 0.5 s send budget; sent['timed_out'] carries "
                 "the claim"),
    "tests/test_harness.py::TheRpcDeadlineIsWallClockNotPerRecv.test_fleet_sibling_is_bounded_and_returns_none_per_its_contract": (
        1, HANG, "5 s over a 0.5 s RPC deadline; the trickle never answers"),
    "tests/test_honest_presence.py::OneProjectionTest.test_last_seen_prefers_the_beat_file_and_only_falls_back": (
        1, HANG, "a fallback stamp within 5 s of now against a beat planted "
                 "3200 s old"),
    "tests/test_hooklatency.py::HookLatencyTest.test_a_budgeted_wait_skips_a_held_lock_instead_of_being_killed": (
        1, SPEED, "2 s over a 0.2 s lock deadline"),
    "tests/test_hookrun.py::HookRunTest.test_hung_handler_is_cut_at_its_own_budget": (
        1, HANG, "10 s over a 1 s handler budget; the hung handler sleeps "
                 "30 s"),
    "tests/test_hookrun.py::HookRunTest.test_one_handlers_timeout_does_not_consume_the_next_ones": (
        1, HANG, "10 s over two 1 s handler budgets; each hung handler "
                 "sleeps 30 s"),
    "tests/test_hookrun.py::TimerCancellationTest._probe": (
        1, SPEED, "0.32 s (0.8 of the 0.4 s sleep) over a 0.04 s alarm "
                  "budget"),
    "tests/test_meld.py::TestRecv.test_a_timeout_shorter_than_one_poll_is_honoured": (
        1, SPEED, "1 s over a 0.05 s recv timeout, telling it apart from a "
                  "2 s poll"),
    "tests/test_mutation_matrix.py::MutationMatrixRepo.test_forked_child_cannot_hold_receipt_past_deadline": (
        1, SPEED, "3 s over a matrix subprocess with a 0.5 s timeout; the "
                  "forked holder sleeps 1.5 s"),
    "tests/test_proxywatch.py::TimerUnitTest.test_a_held_outbox_lock_never_blocks_the_dark_cadence": (
        1, SPEED, "1 s over a dark pass that must stand down on a held lock "
                  "without waiting"),
    "tests/test_relevance.py::DetachedWorkerTest.test_a_failed_second_fork_is_no_worker_and_writes_nothing_to_the_hook": (
        1, SPEED, "1000 ms over a wait a child process ends at once, telling "
                  "it apart from its 2 s bound"),
    "tests/test_resumeturn.py::NativeAutocompactionTest.refused_manual_precompact": (
        1, HANG, "the hook's 5 s timeout over a 0.4 s bounded wait"),
    "tests/test_resumeturn.py::NativeAutocompactionTest.test_the_producer_waits_out_a_short_hold_and_writes": (
        1, SPEED, "the producer's own 0.4 s bound over a 0.1 s released "
                  "hold plus a hook run"),
    "tests/test_resumeturn.py::NativeAutocompactionTest.test_a_fifo_at_the_transcript_path_declines_at_once_and_records_no_position": (
        1, SPEED, "1 s over a decline that happens at once, against a FIFO "
                  "open that would block"),
    "tests/test_resumeturn.py::WithdrawalTest.test_absence_of_evidence_costs_no_time": (
        1, SPEED, "1 s over an instant answer, against the 30 s deadline the "
                  "defect spends"),
    "tests/test_resumeturn.py::OneActDoorTest.test_a_stalled_final_capture_refuses_inside_its_deadline": (
        1, SPEED, "2.5 s over three 0.2 s act deadlines; the stall is 3 s"),
    "tests/test_route.py::NoProbeTest.test_f7_no_probe_door_is_touched_and_the_answer_is_fast": (
        1, HANG, "5 s over an in-process answer with every network and "
                 "spawn door refused"),
    "tests/test_seat_lineage_board.py::BoardProjectionTest.test_roster_only_change_ages_the_restored_holder": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against an aged one 30 s or more back"),
    "tests/test_seat_lineage_board.py::BoardProjectionTest.test_recycled_name_does_not_restore_a_departed_successor": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against an aged one 30 s or more back"),
    "tests/test_seats.py::JoinTest.test_a_stalled_stdin_returns_fast_instead_of_blocking_forever": (
        1, SPEED, "HOOK_STDIN_DEADLINE_S + 2 s over a read that waits "
                  "HOOK_STDIN_DEADLINE_S (2 s)"),
    "tests/test_seats.py::WaitTest.test_a_timeout_shorter_than_one_poll_is_honoured": (
        1, SPEED, "1 s over a 0.05 s wait timeout, telling it apart from a "
                  "2 s poll"),
    "tests/test_seats_final_capture.py::TheFinalCaptureIsTheLastWordTest.test_a_stalled_dependency_read_refuses_inside_its_deadline": (
        1, SPEED, "3.5 s over three 0.3 s act deadlines; the stall is 4 s"),
    "tests/test_seats_stop_delegation.py::DelegationHolderAncestryTest.test_nonblocking_evidence_lock_returns_unknown_immediately": (
        1, HANG, "5 s over a non-blocking acquire; the assertIsNone above it "
                 "is the proof"),
    "tests/test_stopfacts.py::HookClockTest.test_elapsed_is_read_against_proc_uptime": (
        1, HANG, "60 s over a reading planted 2.5 s back"),
    "tests/test_web_board.py::BoardJoinTest.test_other_projects_pipeline_is_loading_and_never_holds_the_board": (
        1, HANG, "5 s over a board read that must not wait on a cold "
                 "projection held open"),
    "tests/test_web_board.py::BoardJoinTest.test_a_slow_leg_answers_inside_its_budget_as_still_being_read": (
        1, HANG, "5 s over a 0.3 s leg budget; the slow leg is held until "
                 "released"),
    "tests/test_web_board.py::BoardWarmStartTest.test_the_server_serves_while_the_board_warms": (
        1, HANG, "5 s over a server start that must not wait on a 10 s board "
                 "warm-up"),
    "tests/test_web_ledger_native.py::TestSseDoorbellWire.test_dead_watcher_ends_the_stream_promptly": (
        1, SPEED, "3 s over a stream close on watcher death, against the "
                  "8 s socket timeout"),
    "tests/test_web_ledger_native.py::TestSseDoorbellWire.test_viewer_connect_wakes_watcher_through_real_handler": (
        1, SPEED, "0.10 s, tight by design, over a doorbell against a 2 s "
                  "idle cadence"),
    "tests/test_web_lr.py::ServeStaleWhileRevalidatingTest.test_a_slow_cold_build_answers_warming_instead_of_blocking": (
        1, HANG, "5 s over a 0.3 s cold wait; the build is held up to 30 s"),
    "tests/test_web_lr.py::ProjectionSurvivesARestartTest.test_a_body_whose_INPUT_IS_UNCHANGED_is_admitted_current": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against a body aged 30 s or more"),
    "tests/test_web_lr.py::ProjectionSurvivesARestartTest.test_a_body_the_LEDGER_OUTRAN_is_admitted_STALE_though_young": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against a body aged 30 s or more"),
    "tests/test_web_lr.py::TheBuildBindsToOneTrunkCommitTest.test_an_UNCHANGED_trunk_still_witnesses_IDENTICALLY_and_reads_FRESH": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against a body aged 30 s or more"),
    "tests/test_web_lr.py::TheWitnessCoversTheAPPROVALTIERTest.test_a_tier_that_goes_UNKNOWN_ages_an_APPROVED_body_OUT": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against a body aged 30 s or more"),
    "tests/test_web_lr.py::OneReadSetTwoProductsTest.test_a_CHANGED_read_set_REJECTS_the_restore": (
        1, HANG, "a stamp stored moments before, read within 30 s of now, "
                 "against a body aged 30 s or more"),
    "tests/test_web_lr.py::ARestoredBodyIsDatedNotAncientTest.test_the_first_read_of_a_server_life_answers_DATED_not_a_blocking_build": (
        1, SPEED, "1 s over serving a restored body, against a rebuild held "
                  "up to 30 s"),
}

#: Arms under conversion to an event count. They leave the scan by being
#: converted, never by an entry here.
NEVER_ALLOWLISTED = (
    "tests/test_seats_claims.py::",
    "tests/test_resumeturn.py::AlertWakeDmTest."
    "test_the_legs_are_independent_under_a_stalled_dm",
)


# ---- the controls. A guard is unproven until it has failed.

_PLANTED = """\
import time
import time as _t
from time import monotonic as mono
class Probe:
    def test_forms(self, got, t0, bound):
        self.assertLess(time.monotonic() - t0, 5)
        self.assertLessEqual(got["elapsed"], 1.0)
        took = _t.perf_counter() - t0
        self.assertLess(took, 2)
        self.assertGreater(5, time.time() - t0)
        self.assertGreaterEqual(bound, got["waited_ms"])
        self.assertTrue(mono() - t0 < 5)
        self.assertFalse(got["elapsed_s"] > 5)
        self.assertLess(first=round((mono() - t0) * 1000), second=bound)
        self.assertAlmostEqual(took, 1.0, delta=0.5)
        assert 0.4 <= time.time_ns() - t0 < 5
        t1 = mono()
        self.assertLess(t1 - t0, 5)
"""
_PLANTED_LINES = [(6, "assertLess"), (7, "assertLessEqual"), (9, "assertLess"),
                  (10, "assertGreater"), (11, "assertGreaterEqual"),
                  (12, "assertTrue"), (13, "assertFalse"), (14, "assertLess"),
                  (15, "assertAlmostEqual"), (16, "assert"),
                  (18, "assertLess")]

# Every line here compares a time, a position or a count, and none holds an
# elapsed span under a ceiling.
_QUIET = """\
import os, time
FRESH_H = 24
class Probe:
    def test_quiet(self, done, walls, order, polls, row, stamp, p, t0):
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertGreater(time.monotonic() - t0, 0.1)
        self.assertTrue(done.wait(60))
        self.assertLessEqual(len(polls), 3)
        self.assertLess(walls[0].pos, walls[1].pos)
        self.assertLess(order.index("start"), order.index("paste"))
        self.assertLess(os.stat(p).st_mtime, time.time() - 3600)
        floor = time.time() - FRESH_H * 3600
        self.assertGreater(os.stat(p).st_mtime, floor)
        self.assertAlmostEqual(row["elapsed"], 74.0)
        before = time.time()
        self.assertGreaterEqual(stamp, before)
        self.assertGreater(os.stat(p).st_mtime, before - 3600)
        before = len(polls)
        self.assertLess(before - t0, 3)
        self.assertLess(self.MAX_ELAPSED_S, 10)
        got = time.monotonic() - t0
        got = len(polls)
        self.assertLess(got, 3)
"""

# One reported span, twice: the second function hands the code under test a
# fake clock, and a clock difference it spells itself still counts there.
_INJECTED = """\
import time
class Probe:
    def test_real(self, run, t0):
        out = run()
        self.assertLess(out["waited_ms"], 100)
    def test_fake(self, run, clock, sleep, t0):
        out = run(clock=clock, sleep=sleep)
        self.assertLess(out["waited_ms"], 100)
        self.assertLess(time.monotonic() - t0, 5)
"""

_FUNCTION = "tests/test_probe.py::Probe.test_forms"


def _site(line, scope="Probe.test_forms"):
    return Finding("tests/test_probe.py", line, "assertLess(span, bound)",
                   scope, "a clock difference")


class TheScannerSeesTheShape(unittest.TestCase):
    """POSITIVE CONTROLS FIRST: an empty finding list over the tree is only a
    clean tree if this instrument can fire at all."""

    def test_every_ceiling_form_is_seen(self):
        found = scan_source(_PLANTED, "tests/test_probe.py")
        self.assertEqual([(f.line, f.form.split("(")[0].split(" ")[0])
                          for f in found], _PLANTED_LINES, found)
        self.assertEqual({f.scope for f in found}, {"Probe.test_forms"})

    def test_floors_waits_positions_counts_and_moments_are_not_ceilings(self):  # noqa: VACUOUS_ASSERTION — the must-hit on the planted forms is the FIRST statement, through the same scan_source door; each call mints a fresh producer the rung cannot link
        self.assertEqual(len(scan_source(_PLANTED)), len(_PLANTED_LINES))
        self.assertEqual(scan_source(_QUIET), [])

    def test_an_injected_clock_quiets_a_reported_span_never_a_spelled_one(self):
        found = scan_source(_INJECTED)
        self.assertEqual([(f.line, f.scope, f.why) for f in found],
                         [(5, "Probe.test_real", "keyed 'waited_ms'"),
                          (9, "Probe.test_fake", "a clock difference")])

    def test_a_rebind_drops_what_a_binding_taught(self):  # noqa: VACUOUS_ASSERTION — the must-hit on the same source without the rebind is the FIRST statement, through the same scan_source door; each call mints a fresh producer the rung cannot link
        """A vocabulary name is a span whatever it holds; a local that is a
        span only by its binding stops being one when it is rebound."""
        source = ("import time\n"
                  "def t(self, t0, polls):\n"
                  "    got = time.monotonic() - t0\n"
                  "%s"
                  "    self.assertLess(got, 3)\n")
        self.assertEqual(len(scan_source(source % "")), 1)
        self.assertEqual(scan_source(source % "    got = len(polls)\n"), [])

    def test_a_helper_nested_in_a_test_is_filed_under_the_test(self):
        source = textwrap.dedent("""\
            import time
            class Probe:
                def test_outer(self, t0):
                    def run():
                        self.assertLess(time.monotonic() - t0, 1)
                    run()
            def test_module_level(t0):
                assert time.monotonic() - t0 < 1
            """)
        self.assertEqual([(f.line, f.scope) for f in scan_source(source)],
                         [(5, "Probe.test_outer"), (8, "test_module_level")])


class TheAuditRefuses(unittest.TestCase):
    """Each refusal on a synthetic finding list, beside the exact list it
    leaves clean."""

    ENTRY = {_FUNCTION: (1, HANG, "a planted entry")}

    def test_an_exact_list_is_clean(self):  # noqa: VACUOUS_ASSERTION — the must-hit on the same entry with one site too many is the FIRST statement, through the same audit door; each audit call mints a fresh producer the rung cannot link
        self.assertEqual(len(audit([_site(6), _site(9)], self.ENTRY)), 1)
        self.assertEqual(audit([_site(6)], self.ENTRY), [])

    def test_an_unlisted_site_names_its_line_its_form_and_the_remedy(self):
        (problem,) = audit([_site(6)], {})
        self.assertIn("tests/test_probe.py:6 assertLess(span, bound)", problem)
        self.assertIn("a clock difference", problem)
        self.assertIn("Count the operation's own events or polls", problem)
        self.assertIn("fake clock", problem)
        self.assertIn("allowlist", problem.lower())

    def test_a_function_past_its_count_is_refused(self):
        (problem,) = audit([_site(6), _site(9)], self.ENTRY)
        self.assertIn(_FUNCTION, problem)
        self.assertIn("allowlisted for 1", problem)
        self.assertIn("lines 6, 9", problem)

    def test_a_stale_entry_is_refused_so_the_list_only_shrinks(self):
        (gone,) = audit([], self.ENTRY)
        self.assertIn("STALE", gone)
        self.assertIn("finds 0", gone)
        (fewer,) = audit([_site(6)], {_FUNCTION: (2, HANG, "planted")})
        self.assertIn("STALE", fewer)
        self.assertIn("allowlisted for 2 elapsed ceilings and the scan finds 1",
                      fewer)


class TheTestsTreeHoldsNoNewCeiling(unittest.TestCase):

    def test_the_tree_door_finds_a_planted_ceiling_at_every_depth(self):
        """MUST-HIT THROUGH THE SAME DOOR the real audit uses: a module, a
        helper that is not a test module, and a nested package."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pkg"))
            for rel in ("test_planted.py", "_helper.py",
                        os.path.join("pkg", "test_nested.py")):
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as fh:
                    fh.write(_PLANTED)
            found = scan_tree(tmp)
        self.assertEqual(
            sorted({f.path for f in found}),
            ["tests/_helper.py", "tests/pkg/test_nested.py",
             "tests/test_planted.py"])
        self.assertEqual(len(found), 3 * len(_PLANTED_LINES))

    def test_no_elapsed_ceiling_is_unlisted_growing_or_stale(self):  # noqa: VACUOUS_ASSERTION — the must-hit through the same audit door and the same ALLOWLIST is the FIRST statement, and the tree door's own must-hit is the arm above; each call mints a fresh producer the rung cannot link
        # MUST-HIT FIRST, on the same audit door: the real allowlist refuses
        # a planted site it does not name.
        planted = scan_source(_PLANTED, "tests/test_probe.py")
        self.assertEqual(len(audit(planted, ALLOWLIST)),
                         len(_PLANTED_LINES) + len(ALLOWLIST))
        problems = audit(scan_tree(), ALLOWLIST)
        self.assertEqual(problems, [], "\n\n".join(problems))

    def test_every_entry_says_which_kind_it_is_and_why(self):  # noqa: VACUOUS_ASSERTION — the loop runs over the literal ALLOWLIST, asserted non-empty unconditionally before it
        shape = re.compile(r"^tests/[\w/]+\.py::\w+(\.\w+)?$")
        self.assertGreater(len(ALLOWLIST), 0, "the allowlist is empty")
        for key, (count, kind, why) in ALLOWLIST.items():
            with self.subTest(key=key):
                self.assertRegex(key, shape)
                self.assertGreaterEqual(count, 1)
                self.assertIn(kind, (HANG, SPEED))
                self.assertGreaterEqual(len(why), 30, why)

    def test_the_arms_under_conversion_are_never_excused(self):
        self.assertTrue(ALLOWLIST, "the allowlist is empty")
        excused = [key for key in ALLOWLIST
                   if any(key.startswith(p) for p in NEVER_ALLOWLISTED)]
        self.assertEqual(excused, [])


if __name__ == "__main__":
    unittest.main()
