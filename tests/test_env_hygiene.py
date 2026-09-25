#!/usr/bin/env python3
"""A test suite must restore every environment variable it sets.

THE CLASS. A suite's setUp pops a tuple of keys and tearDown restores them, so
a key that is SET in the file but ABSENT from that tuple is never cleaned and
leaks into every later test in the same process. Measured twice:

  * f45b3c3 — test_chat_v2.py set HELM_CELL_PROFILE=p1 and omitted it from
    ENV_KEYS. A sibling test was green ONLY because it drank the leaked value:
    it asserts st["profile"] == "p1" and never set that itself.
  * this lane — test_seat_port_owner.py set HELM_PROC to a tmp tree that
    tearDown then rmtree'd, so the variable outlived the directory it named.
    Nothing was red, because test order happens to put every reader of that
    key either earlier or inside a suite that pops it. Order is not a
    guarantee; it is the reason this went unnoticed.

WHY A TEST AND NOT A NOTE. The first instance was fixed by hand and the class
survived in 168 other files. A hand fix does not generalize; this does. It runs
in-process with everything else, so a new leak fails the suite that introduced
it rather than some unrelated suite three files later.

WHAT COUNTS AS RESTORATION, deliberately generous — over-counting coverage
means UNDER-reporting leaks, and a false accusation against a clean file is
worse here than a miss, because it trains authors to append to the allowlist:
a key named in ANY tuple/list/set of strings, popped from os.environ, deleted
with `del os.environ[...]`, or named in a dict passed to mock.patch.dict.
"""
import ast
import os
import shutil
import tempfile
import unittest

_TESTS = os.path.dirname(os.path.abspath(__file__))

# Module-level process-wide defaults, set ON PURPOSE and documented at their
# site. These are the harness declaring the world every test runs in, not a
# suite forgetting to clean up — restoring them would defeat their point.
# Additions here need a reason in the same breath.
INTENTIONAL_GLOBAL = {
    "tests/__init__.py": {"HELM_METAHARNESS", "HELM_STOP_TIMING_AFTER",
                          # no suite filing may start a real model read
                          "HELM_QWEN27_FINDINGS",
                          # no arm may read this box's seat memory unasked
                          "HELM_SEAT_PRESSURE",
                          # no arm may change this host's scheduler
                          "HELM_AUTOCOMPACT_TIMER"},
    "tests/_tmphome.py": {"HELM_CHAT_ROOM"},
    "tests/test_configs.py": {"HELM_HOME", "HELM_CONFIG_ROOTS"},
}


def _literal(node):
    return node.value if isinstance(node, ast.Constant) \
        and isinstance(node.value, str) else None


def _env_base(value):
    """'environ' when this expression is os.environ (or a bare environ)."""
    if isinstance(value, ast.Attribute):
        return value.attr
    return getattr(value, "id", "")


def assignments(tree):
    """{ENV_NAME: lineno} for every os.environ write in the module."""
    out = {}
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Subscript) and _env_base(t.value) == "environ":
                key = _literal(t.slice)
                if key:
                    out.setdefault(key, node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("setdefault", "update") \
                and _env_base(node.func.value) == "environ":
            if node.args and _literal(node.args[0]):
                out.setdefault(_literal(node.args[0]), node.lineno)
            for a in node.args:
                if isinstance(a, ast.Dict):
                    for k in a.keys:
                        if _literal(k):
                            out.setdefault(_literal(k), node.lineno)
    return out


_REGISTRARS = ("addCleanup", "register")
_REMOVERS = ("pop", "__delitem__")


def _registers_cleanup(func):
    """Is this call a CLEANUP REGISTRAR — addCleanup, or atexit.register?

    The role is what makes a stored callable a restoration. A bare reference
    handed to anything else is a value, not a cleanup, and crediting it hides
    a real leak (measured on this lane: a handler table, a get-only
    reference and an unregistered partial all read as clean).
    """
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "addCleanup":
        return True
    return func.attr == "register" and _env_base(func.value) != "environ" \
        and getattr(func.value, "id", None) == "atexit"


def _cleanup_callables(call):
    """The key this registration actually removes, or an empty set.

    ONLY args[0] IS THE CALLABLE. `addCleanup(fn, *args)` registers fn and
    forwards the rest AS DATA, so scanning every argument credits a key the
    registration never removes: `addCleanup(print, os.environ.pop, KEY)`
    registers PRINT and hands it the pop as a value. Measured on
    this lane — the same hole for atexit.register and for a forwarded partial.

    Two accepted shapes, both requiring a REMOVING attribute on environ:
    `reg(os.environ.pop, KEY, ...)` and `reg(partial(os.environ.pop, KEY))`.
    """
    if not call.args:
        return set()
    head = call.args[0]
    if isinstance(head, ast.Attribute):
        return _keyed(head, call.args[1:])
    if isinstance(head, ast.Call) and _is_partial(head):
        return _keyed(head.args[0] if head.args else None, head.args[1:])
    return set()


def _keyed(fn, args):
    """The env key `fn(*args)` would REMOVE, if fn removes at all."""
    if isinstance(fn, ast.Attribute) and fn.attr in _REMOVERS \
            and _env_base(fn.value) == "environ" \
            and args and _literal(args[0]):
        return {_literal(args[0])}
    return set()


def _is_partial(node):
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr == "partial") or \
        (isinstance(f, ast.Name) and f.id == "partial")


def restorations(tree):
    """Every env name the module mentions in a cleanup-capable position."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out |= {s for s in map(_literal, node.elts) if s}
        if isinstance(node, ast.Dict):
            out |= {s for s in map(_literal, node.keys) if s}
        if isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Subscript) \
                        and _env_base(t.value) == "environ":
                    key = _literal(t.slice)
                    if key:
                        out.add(key)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("pop", "get") \
                and _env_base(node.func.value) == "environ" \
                and node.args and _literal(node.args[0]):
            out.add(_literal(node.args[0]))
        # THE POP PASSED AS A REFERENCE TO A CLEANUP REGISTRAR.
        # `self.addCleanup(os.environ.pop, KEY, None)` is a Call whose func is
        # `addCleanup` — the clause above looks at the FUNC and never fires —
        # while `os.environ.pop` sits in the ARGUMENTS as a bare Attribute.
        # That accused every module using the form unittest documents, and
        # addCleanup is STRICTLY BETTER than tearDown here because it still
        # runs when setUp raises partway. Measured live 2026-09-09 as one of
        # nine failures on a composed train gate (task/1352).
        #
        # THE ROLE IS BOUND, NOT THE SHAPE, and a probe measured why it must
        # be: crediting a key from ANY call's arguments hides three real
        # leaks. `register_handler(os.environ.pop, KEY)` STORES a callable
        # nobody invokes; `addCleanup(os.environ.get, KEY)` reads and restores
        # nothing; an unregistered `partial` is never called. All three are
        # syntactically identical to a cleanup and none of them cleans up. So
        # this requires a REGISTRAR (addCleanup / atexit.register) and a
        # REMOVING attribute (pop / __delitem__) — `get` is a read and is
        # deliberately absent.
        if isinstance(node, ast.Call) and _registers_cleanup(node.func):
            out |= _cleanup_callables(node)
    return out


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _lifecycle_call(node, name):
    """Does `node` call THIS instance's `name` (setUp or tearDown)?

    Three spellings reach it: self.setUp(), super().setUp() in either form,
    and Base.setUp(self). A call on ANOTHER instance -- `case =
    Fixture("run"); case.setUp()` -- drives a separate fixture on purpose,
    usually to test that fixture, and is not this hazard.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == name):
        return False
    recv = node.func.value
    if isinstance(recv, ast.Name) and recv.id == "self":
        return True
    if isinstance(recv, ast.Call) and getattr(recv.func, "id", None) == "super":
        return True
    first = node.args[0] if node.args else None
    return isinstance(first, ast.Name) and first.id == "self"


def _stmt_calls(stmt, name):
    return isinstance(stmt, ast.Expr) and _lifecycle_call(stmt.value, name)


def _own_nodes(fn):
    """Every node of fn's own body. A function or class nested in it is
    scanned as a function of its own, so it is not walked twice."""
    stack = [c for c in fn.body if not isinstance(c, _SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node)
                     if not isinstance(c, _SCOPES))


def reentrant_setups(tree):
    """[(line, function)] for every call that runs THIS instance's setUp
    while its fixture is still live.

    The runner calls setUp before the test body, so a setUp called from the
    body (or from any helper it reaches) builds a second fixture over a live
    one. Every save-and-restore setUp then re-saves the environment the first
    fixture already rewrote, and tearDown writes the temporary values back as
    the originals. One shape is exempt: `self.tearDown()` as the statement
    IMMEDIATELY before, in the same block, because the fixture is gone when
    setUp saves again. A conditional or distant tearDown does not qualify.

    Bound: the instance is recognised by the name `self`. A helper that takes
    it under another name is not seen.
    """
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or fn.name == "setUp":
            continue
        nodes = list(_own_nodes(fn))
        balanced = set()
        for owner in [fn] + nodes:
            for field in ("body", "orelse", "finalbody"):
                block = getattr(owner, field, None)
                if not isinstance(block, list):
                    continue
                for prev, stmt in zip(block, block[1:]):
                    if _stmt_calls(stmt, "setUp") \
                            and _stmt_calls(prev, "tearDown"):
                        balanced.add(stmt.value)
        hits += [(n.lineno, fn.name) for n in nodes
                 if _lifecycle_call(n, "setUp") and n not in balanced]
    return sorted(hits)


def _dotted(node):
    """'a.b.c' for a Name/Attribute chain, None for anything computed."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return head + "." + node.attr if head else None
    return None


def plants_live_seats(fn):
    """Does this function stand `proxywatch._live_seats` in?

    Three spellings do: `pin_live_seats(...)` (the shared door in
    tests/_tmphome.py), `patch.object(<...>proxywatch, "_live_seats", ...)`
    and `patch("<...>proxywatch._live_seats", ...)`. A patch of another
    module's `_live_seats` (seats_work_offer has one) is not this census."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = (_dotted(node.func) or "").rpartition(".")[2]
        if name == "pin_live_seats":
            return True
        if name == "object" and len(node.args) >= 2 \
                and (_dotted(node.args[0]) or "").rpartition(".")[2] \
                == "proxywatch" and _literal(node.args[1]) == "_live_seats":
            return True
        if name == "patch" and node.args and (
                _literal(node.args[0]) or "").endswith("proxywatch._live_seats"):
            return True
    return False


_WRITERS = ("add", "send")
_SETUPS = ("setUp", "setUpClass", "asyncSetUp")


def live_seat_facts(tree):
    """What the live-seat census needs from one module, and nothing else.

    `writes`: the module calls `dispatches.add` or `dispatches.send` itself,
    under any name it bound helm.dispatches to. `module_plant`: its
    setUpModule stands the census in. `classes`: {name: (base expressions,
    its own setUp plants, it defines a test)}. `imports`: {local name:
    (tests module, name or None for the module itself)}."""
    aliases, imports, receivers = {"helm.dispatches"}, {}, set()
    for node in ast.walk(tree):                  # ONE walk: the census cost
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _WRITERS:
            receivers.add(_dotted(node.func.value))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and mod == "helm":
                aliases |= {a.asname or a.name for a in node.names
                            if a.name == "dispatches"}
            if (node.level, mod) in ((0, "tests"), (1, "")):
                # `from tests import test_x` binds the MODULE
                imports.update({a.asname or a.name: (a.name, None)
                                for a in node.names})
                continue
            if node.level == 0 and mod.startswith("tests."):
                mod = mod.split(".", 1)[1]
            elif node.level != 1:
                continue
            imports.update({a.asname or a.name: (mod, a.name)
                            for a in node.names})
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "helm.dispatches" and a.asname:
                    aliases.add(a.asname)
                if a.name.startswith("tests.") and a.asname:
                    imports[a.asname] = (a.name.split(".", 1)[1], None)
    writes = bool(receivers & aliases)
    classes, module_plant = {}, False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "setUpModule":
            module_plant = plants_live_seats(node)
        if isinstance(node, ast.ClassDef):
            methods = [m for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes[node.name] = (
                tuple(_dotted(b) for b in node.bases),
                any(m.name in _SETUPS and plants_live_seats(m) for m in methods),
                any(m.name.startswith("test") for m in methods))
    return {"writes": writes, "module_plant": module_plant,
            "classes": classes, "imports": imports}


_SUITE = []


def suite_census():
    """[(rel, facts)] for every tests/*.py, each file parsed ONCE per process.

    Every census arm in this module reads the same files, and the parse was
    the cost: 12.9 s of the module's 12.9 s, two full parses of 27 MB of
    tests plus `restorations(tree)` re-walked once per assigned key inside
    the filter below (896 whole-tree walks where 208 suffice; task/3039).
    Only the FACTS are kept: the trees of 459 files would hold about a
    gigabyte for the rest of the process."""
    if not _SUITE:
        for name in sorted(os.listdir(_TESTS)):
            if not name.endswith(".py"):
                continue
            rel = "tests/" + name
            with open(os.path.join(_TESTS, name), encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel)
            restored = restorations(tree)
            _SUITE.append((rel, {
                "leaks": {k: ln for k, ln in assignments(tree).items()
                          if k not in restored
                          and k not in INTENTIONAL_GLOBAL.get(rel, ())},
                "reentry": reentrant_setups(tree),
                "live_seats": live_seat_facts(tree)}))
    return _SUITE


class EnvHygieneTest(unittest.TestCase):
    def _scan(self):
        """[(rel, {key: line})] for every test module that leaks."""
        census = suite_census()
        return ([(rel, facts["leaks"]) for rel, facts in census
                 if facts["leaks"]], len(census))

    def test_every_test_module_restores_the_env_vars_it_sets(self):
        leaks, scanned = self._scan()
        self.assertGreater(scanned, 100,
                           "the scan must actually reach the suite — a walk "
                           "that finds nothing proves nothing")
        self.assertEqual(leaks, [], "\n".join(
            "%s sets %s and never restores it" %
            (rel, ", ".join("%s (line %d)" % (k, ln)
                            for k, ln in sorted(m.items())))
            for rel, m in leaks))

    def test_the_detector_SEES_a_planted_leak(self):
        """The control. A scanner that silently stopped parsing would report a
        clean estate forever, which is the same green as a clean one."""
        src = ("import os\n"
               "ENV_KEYS = ('KEPT',)\n"
               "os.environ['KEPT'] = 'x'\n"
               "os.environ['LEAKED'] = 'y'\n")
        tree = ast.parse(src)
        found = assignments(tree)
        self.assertEqual(sorted(found), ["KEPT", "LEAKED"])
        kept = restorations(tree)
        self.assertIn("KEPT", kept)
        self.assertNotIn("LEAKED", kept)

    def test_a_reference_that_is_not_a_CLEANUP_is_still_a_leak(self):
        """THE THREE SHAPES A SHAPE-ONLY RULE LAUNDERS, all found in review
        reviewing the first cut of this clause, and a wrong-KEY arm cannot
        reach any of them — each uses the RIGHT key and still restores
        nothing. Crediting a bare `environ.pop` wherever it appears is not a
        smaller version of this rule, it is a different rule that happens to
        agree on the example I had in mind.

        A handler table STORES a callable nobody invokes. `environ.get` READS.
        An unregistered partial is never called. All three are syntactically
        indistinguishable from a cleanup; the ROLE is what separates them.
        """
        # THE POSITIVE CONTROL RUNS UNCONDITIONALLY, ahead of the loop: a
        # control inside a subTest body proves nothing if the body never
        # executes, and an empty case tuple would make every assertion below
        # vacuous while the arm stayed green.
        live = ast.parse("import os\n"
                         "os.environ['K'] = '1'\n"
                         "self.addCleanup(os.environ.pop, 'K', None)\n")
        self.assertIn("K", restorations(live))
        for why, src in (
                ("handler table", "register_handler(os.environ.pop, 'K')\n"),
                ("get-only", "self.addCleanup(os.environ.get, 'K')\n"),
                ("unregistered partial",
                 "f = functools.partial(os.environ.pop, 'K', None)\n"),
                # THE POP IN A CALLBACK'S ARGUMENT LIST. `addCleanup(fn,
                # *args)` registers fn and forwards the rest AS DATA, so the
                # pop here is a value handed to print — the registration
                # removes nothing. Found in review after the role binding
                # cured the first three; scanning every argument rather than
                # args[0] was still crediting this.
                ("callback argument",
                 "self.addCleanup(print, os.environ.pop, 'K')\n"),
                ("atexit callback argument",
                 "atexit.register(print, os.environ.pop, 'K')\n"),
                ("forwarded partial",
                 "self.addCleanup(print, "
                 "functools.partial(os.environ.pop, 'K'))\n")):
            with self.subTest(shape=why):
                tree = ast.parse("import os, functools, atexit\n"
                                 "os.environ['K'] = '1'\n" + src)
                self.assertNotIn("K", restorations(tree))

    def test_a_reference_cleanup_for_ANOTHER_key_is_still_a_leak(self):
        """THE MUST-MISS THE REFERENCE CLAUSE OWES. Widening a detector of
        ABSENCE is exactly where you stop detecting, so the arm that matters
        is not "does it see the new form" but "does it still catch what it
        caught before". A module that hands `os.environ.pop` to addCleanup
        for ONE key has restored that key and NOTHING ELSE — the presence of
        the idiom must not launder every other variable the module sets.
        """
        src = ("import os\n"
               "os.environ['RESTORED'] = '1'\n"
               "self.addCleanup(os.environ.pop, 'RESTORED', None)\n"
               "os.environ['LEAKED'] = '2'\n")
        tree = ast.parse(src)
        self.assertEqual(sorted(assignments(tree)), ["LEAKED", "RESTORED"])
        covered = restorations(tree)
        self.assertIn("RESTORED", covered)
        self.assertNotIn("LEAKED", covered)

    def test_the_detector_accepts_every_restoration_FORM_in_use(self):
        """del, pop, a named tuple, and patch.dict all really do clean up, so
        counting any of them as a leak would drive authors to the allowlist —
        which is how a guard becomes a rubber stamp. `del` is the form the
        first version of this scan missed, on a file that was already clean."""
        src = ("import os\n"
               "from unittest import mock\n"
               "TUPLE_FORM = ('A',)\n"
               "os.environ['A'] = '1'\n"
               "os.environ['B'] = '2'\n"
               "del os.environ['B']\n"
               "os.environ['C'] = '3'\n"
               "os.environ.pop('C', None)\n"
               "os.environ['D'] = '4'\n"
               "mock.patch.dict(os.environ, {'D': '4'})\n"
               # THE POP PASSED AS A REFERENCE. This is what unittest
               # documents and it is strictly better than tearDown here,
               # because it still runs when setUp raises partway. The scan
               # could not see it, so it accused modules whose restore was
               # present and running — measured live 2026-09-09 as one of
               # nine failures on a composed train gate (task/1352).
               "os.environ['E'] = '5'\n"
               "self.addCleanup(os.environ.pop, 'E', None)\n"
               "os.environ['F'] = '6'\n"
               "self.addCleanup(functools.partial("
               "os.environ.pop, 'F', None))\n"
               # atexit is the other registrar in use; without an arm the
               # branch is live code no mutation could redden.
               "os.environ['G'] = '7'\n"
               "atexit.register(os.environ.pop, 'G', None)\n")
        tree = ast.parse(src)
        covered = restorations(tree)
        self.assertEqual(sorted(covered & {"A", "B", "C", "D", "E", "F", "G"}),
                         ["A", "B", "C", "D", "E", "F", "G"],
                         "a restoration form in live use reads as a leak: "
                         "A=tuple, B=del, C=pop, D=patch.dict")



class ReentrantSetUpTest(unittest.TestCase):
    """A test must never call its own setUp while the fixture is live.

    THE CLASS, and the key-tuple scan above cannot see it, because every key
    here IS restored -- to the wrong value. setUp saves the environment into
    one attribute; a second setUp overwrites that attribute with the values
    the first one set; tearDown then restores those. Nothing in the test that
    did it goes red. The damage lands in whatever runs next in the process,
    so a whole-suite gate can pass while a focused run fails.

    Measured in this tree more than once: a leaked HELM_HOME, a leaked
    HELM_CHAT_DIR naming a directory that did not exist (11 claim-lock errors
    and 5 steer failures in two unrelated modules), and a second
    mock.patch.multiple that tearDown stopped only once. A per-case scope, or
    a test of its own, gives a fresh fixture without a second lifecycle.
    """

    def test_no_test_runs_its_own_setUp_over_a_live_fixture(self):  # noqa: VACUOUS_ASSERTION — the scanned floor proves the walk reached the suite, and the planted-reentry arm below proves reentrant_setups reports on this parser path; this arm is the census they license
        census = suite_census()
        scanned = len(census)
        hits = ["%s:%d in %s()" % (rel, line, fn)
                for rel, facts in census for line, fn in facts["reentry"]]
        self.assertGreater(scanned, 100,
                           "the scan must actually reach the suite -- a walk "
                           "that finds nothing proves nothing")
        self.assertEqual(hits, [], "setUp called over a live fixture (its "
                         "tearDown restores the fixture's own values): "
                         + "; ".join(hits))

    @staticmethod
    def _method(body):
        return ast.parse("class T(Base):\n    def test_x(self):\n"
                         + "".join("        %s\n" % ln
                                   for ln in body.splitlines()))

    def test_the_detector_SEES_every_spelling_of_a_planted_reentry(self):
        """The control. Each shape runs setUp over the live fixture, and a
        scanner that stopped parsing would pass all of them as clean."""
        # THE POSITIVE CONTROL RUNS UNCONDITIONALLY, ahead of the loop: an
        # empty case tuple would otherwise make every arm below vacuous.
        self.assertEqual(reentrant_setups(self._method("self.setUp()")),
                         [(3, "test_x")])
        for why, body in (
                ("super", "super().setUp()"),
                ("two-argument super", "super(T, self).setUp()"),
                ("named base", "Base.setUp(self)"),
                ("first in a loop", "for _ in range(2):\n"
                                    "    self.setUp()\n"
                                    "    self.tearDown()"),
                ("inside a subTest", "with self.subTest():\n"
                                     "    self.setUp()"),
                ("conditional tearDown", "if flag:\n"
                                         "    self.tearDown()\n"
                                         "self.setUp()"),
                ("in a nested helper", "def fresh():\n"
                                       "    self.setUp()\n"
                                       "fresh()")):
            with self.subTest(shape=why):
                self.assertEqual(len(reentrant_setups(self._method(body))), 1)

    def test_a_torn_down_fixture_or_another_instance_is_not_reentry(self):
        """The must-miss. Each shape is clean, and accusing it would drive
        authors to work around the guard rather than fix a leak."""
        # POSITIVE CONTROL on the same parser path, so an empty result below
        # means "exempt" and never "not parsed".
        self.assertEqual(len(reentrant_setups(self._method(
            "x = 1\nself.setUp()"))), 1)
        for why, src in (
                ("torn down first", self._method(
                    "self.tearDown()\nself.setUp()")),
                ("torn down first, in a loop", self._method(
                    "for _ in range(2):\n"
                    "    self.tearDown()\n"
                    "    self.setUp()")),
                ("another instance", self._method(
                    "case = T('run')\ncase.setUp()\ncase.tearDown()")),
                ("another class's class fixture", self._method(
                    "Other.setUpClass()")),
                ("the setUp chain itself", ast.parse(
                    "class T(Base):\n"
                    "    def setUp(self):\n"
                    "        super().setUp()\n"))):
            with self.subTest(shape=why):
                self.assertEqual(reentrant_setups(src), [])


def _resolve(census, module, dotted, seen=frozenset()):
    """(module, class) that a base expression in `module` names, or None
    when it is not a class defined under tests/ (unittest.TestCase, say)."""
    facts = census.get(module)
    if facts is None or not dotted or (module, dotted) in seen:
        return None
    if dotted in facts["classes"]:
        return module, dotted
    seen = seen | {(module, dotted)}
    head, _, rest = dotted.partition(".")
    if head == "tests" and rest:                 # `import tests.test_x`
        owner, _, name = rest.partition(".")
        return _resolve(census, owner, name, seen)
    bound = facts["imports"].get(head)
    if bound is None:
        return None
    owner, name = bound
    if name is None:                             # a module: `test_x.Base`
        return _resolve(census, owner, rest, seen)
    return None if rest else _resolve(census, owner, name, seen)


def _inherits(census, module, cls, field, seen=frozenset()):
    """Does `cls` or any base it names under tests/ carry `field`
    (1: its setUp plants the stand-in, 2: it defines a test)?"""
    if (module, cls) in seen:
        return False
    row = census[module]["classes"][cls]
    if row[field]:
        return True
    seen = seen | {(module, cls)}
    return any(_inherits(census, hit[0], hit[1], field, seen)
               for hit in (_resolve(census, module, b) for b in row[0])
               if hit)


def unplanted_writers(census):
    """{module: [test classes]} for every module that writes a dispatch row
    and runs a test class without the live-seat stand-in.

    `census` is {module name: live_seat_facts}. A module is clean when its
    setUpModule plants, or when every class that runs tests plants in its
    own setUp or inherits a base that does."""
    out = {}
    for module, facts in sorted(census.items()):
        if not facts["writes"] or facts["module_plant"]:
            continue
        bare = sorted(cls for cls in facts["classes"]
                      if _inherits(census, module, cls, 2)
                      and not _inherits(census, module, cls, 1))
        if bare:
            out[module] = bare
    return out


class LiveSeatStandInCensusTest(unittest.TestCase):
    """Every module that writes a dispatch row runs under the live-seat
    stand-in (task/3039).

    THE COST. `dispatches.add` and `dispatches.send` ask
    `_validate_recipient_usable`, which walks the whole process table through
    `proxywatch._live_seats` once per row: about 870 processes on a build
    node, and more on a busy one. 22 of the 31 modules that write rows did not
    stand it in, so each paid the walk on every row and its answer depended
    on what else ran on the box. `tests._tmphome.pin_live_seats` stands in a
    MEASURED EMPTY fleet, which is what a build node answers anyway.

    THE CENSUS KEYS ON THE WRITERS, not on every importer of
    helm.dispatches. 88 modules import it; the walk is paid only where a row
    is written, and some importers exist to measure the real census (the
    proxywatch suite drives `_live_seats` over synthetic /proc rows). A module
    that reaches the writers only through an imported fixture helper takes
    that fixture's plant: LandReqBase and DispatchBase both call
    `pin_live_seats`.
    """

    def _census(self):
        return {rel[len("tests/"):-len(".py")]: facts["live_seats"]
                for rel, facts in suite_census()}

    def test_every_row_writing_module_plants_the_stand_in(self):  # noqa: VACUOUS_ASSERTION — the writer floor proves the census read the suite, and the planted-writer arm below proves unplanted_writers reports on this path
        census = self._census()
        writers = sorted(m for m, f in census.items() if f["writes"])
        self.assertGreater(len(writers), 20,
                           "control: the census must find the row writers")
        bare = unplanted_writers(census)
        self.assertEqual(bare, {}, "\n".join(
            "%s writes dispatch rows and runs %s without the live-seat "
            "stand-in: call tests._tmphome.pin_live_seats() from setUpModule "
            "(or pin_live_seats(self) in the class's setUp)"
            % (module, ", ".join(classes)) for module, classes in bare.items()))

    @staticmethod
    def _facts(**modules):
        return {name: live_seat_facts(ast.parse(src))
                for name, src in modules.items()}

    def test_the_census_SEES_a_writer_without_the_stand_in(self):
        """The control, then each way of being clean, then the look-alike
        that is not a plant."""
        writer = ("from helm import dispatches as d\n"
                  "class T(unittest.TestCase):\n"
                  "    def test_x(self):\n"
                  "        d.add('seat', 'one')\n")
        self.assertEqual(unplanted_writers(self._facts(test_w=writer)),
                         {"test_w": ["T"]})
        module = writer + ("def setUpModule():\n"
                           "    pin_live_seats()\n")
        self.assertEqual(unplanted_writers(self._facts(test_w=module)), {})
        based = self._facts(
            test_base=("class Base(unittest.TestCase):\n"
                       "    def setUp(self):\n"
                       "        mock.patch.object(proxywatch, '_live_seats',"
                       " lambda: (set(), None, {})).start()\n"),
            test_w=("from tests.test_base import Base\n"
                    "from helm import dispatches\n"
                    "class T(Base):\n"
                    "    def test_x(self):\n"
                    "        dispatches.send('seat', 'one')\n"))
        self.assertEqual(unplanted_writers(based), {})
        other = writer + ("def setUpModule():\n"
                          "    mock.patch.object(seats_work_offer, "
                          "'_live_seats', lambda: set()).start()\n")
        self.assertEqual(unplanted_writers(self._facts(test_w=other)),
                         {"test_w": ["T"]},
                         "another module's _live_seats is not this stand-in")

    def test_the_shared_door_stands_in_and_puts_the_census_back(self):
        """`pin_live_seats` for one case and for one module. The production
        predicate itself is never edited: after each scope it is the same
        function object again."""
        from helm import proxywatch
        from tests._tmphome import pin_live_seats
        real = proxywatch._live_seats
        case = unittest.TestCase()
        pin_live_seats(case)
        self.assertIsNot(proxywatch._live_seats, real)
        self.assertEqual(proxywatch._live_seats(), (set(), None, {}),
                         "the stand-in is a MEASURED empty fleet, never blind")
        case.doCleanups()
        self.assertIs(proxywatch._live_seats, real)
        pin_live_seats()
        self.assertIsNot(proxywatch._live_seats, real)
        unittest.doModuleCleanups()
        self.assertIs(proxywatch._live_seats, real)

    def test_a_module_that_writes_no_row_owes_nothing(self):
        reader = ("from helm import dispatches\n"
                  "class T(unittest.TestCase):\n"
                  "    def test_x(self):\n"
                  "        dispatches.snapshot()\n")
        writer = ("from helm import dispatches\n"
                  "class T(unittest.TestCase):\n"
                  "    def test_x(self):\n"
                  "        dispatches.add('seat', 'one')\n")
        facts = self._facts(test_r=reader, test_w=writer)
        self.assertFalse(facts["test_r"]["writes"])
        self.assertEqual(unplanted_writers(facts), {"test_w": ["T"]},
                         "only the writer owes the stand-in")


def deleted_tmp_paths(environ, tmp=None):
    """{KEY: value} for every environment value naming a path inside a temp
    directory that no longer exists.

    A path is judged by the directory directly under the temp root, which is
    the one a fixture's mkdtemp made: a leaf the fixture had not created yet
    is not a leak, a path whose whole fixture directory is gone is. A value
    is split on os.pathsep, so a search-path variable is read entry by
    entry. `tmp` defaults to this process's temp root (tests/__init__.py
    routes it under the suite root)."""
    root = tmp or tempfile.gettempdir()
    roots = {root, os.path.realpath(root)}
    out = {}
    for key, value in environ.items():
        for part in value.split(os.pathsep):
            for base in roots:
                if not part.startswith(base + os.sep):
                    continue
                first = part[len(base) + 1:].split(os.sep, 1)[0]
                if first and not os.path.lexists(os.path.join(base, first)):
                    out[key] = value
    return out


class DeletedTempPathTest(unittest.TestCase):
    """No environment variable may name a path into a temp directory that
    is gone (task/3039).

    MEASURED on trunk 04b8131fddd: after tests.test_codexhomes, HELM_HOME and
    HELM_CODEX_HOMES_DIR both named paths inside its deleted temp directory.
    The static arms above could not see it, because every key WAS restored:
    the fixture restored it in tearDown, and a cleanup a test registered then
    ran after tearDown and put the fixture's paths back.

    THE BOUND: this arm reads the process when it runs, so it sees what every
    module that ran BEFORE it in the same process left behind. The gateslice
    leak audit diffs the environment at every unit boundary and is the
    instrument that sees each unit.
    """

    def test_no_variable_names_a_deleted_temp_directory(self):
        gone = tempfile.mkdtemp(prefix="helm-test-gone-")
        shutil.rmtree(gone)
        env = dict(os.environ, HELM_PLANTED_3039=gone)
        self.assertEqual(deleted_tmp_paths(env), {"HELM_PLANTED_3039": gone},
                         "an earlier test left these variables naming a temp "
                         "directory it removed (the planted one is the "
                         "control)")

    def test_the_detector_SEES_a_path_into_a_deleted_temp_directory(self):
        gone = tempfile.mkdtemp(prefix="helm-test-gone-")
        live = tempfile.mkdtemp(prefix="helm-test-live-")
        self.addCleanup(shutil.rmtree, live, True)
        shutil.rmtree(gone)
        env = {"GONE_LEAF": os.path.join(gone, "helm-home"),
               "GONE_DIR": gone,
               "SEARCH": os.pathsep.join(("/usr/bin", os.path.join(gone, "x"))),
               "LIVE_LEAF": os.path.join(live, "not-made-yet"),
               "LIVE_DIR": live,
               "PLAIN": "value",
               "OUTSIDE": "/nonexistent-3039/x"}
        self.assertEqual(sorted(deleted_tmp_paths(env)),
                         ["GONE_DIR", "GONE_LEAF", "SEARCH"])


if __name__ == "__main__":
    unittest.main()
