#!/usr/bin/env python3
"""The seats.py split's standing contract: the facade owes callers what they USE.

seats.py is being split into sibling modules. Every extraction moves names out
of the file and re-exports them from it, and the failure mode is always the
same shape: a name that LOOKED internal turns out to have an external caller,
so the module still imports, the suite still mostly passes, and one production
path dies with AttributeError.

That happened on the very first extraction. helm/gate.py calls
`seats._gate_repo_id` and `seats._gate_pid_state` — underscored names that
every convention says are private — and the gate-queue re-export listed only
the eight public `gate_queue_*` verbs. `gate.run()` broke while every name I
had reasoned about resolved perfectly.

SO THE CURE IS NOT A LONGER RE-EXPORT LIST, IT IS THIS TEST. A per-extraction
checklist can never be completed by adding cases, because the next extraction
is always outside the set you enumerated. This asks the question once, for
every name, against the real callers — and it keeps asking it for the eleven
modules still to come.
"""
import ast
import builtins
import os
import symtable
import sys
import types as _types
import unittest
from unittest import mock

from helm import seats

HELM = os.path.dirname(os.path.abspath(seats.__file__))
ROOT = os.path.dirname(HELM)
TESTS = os.path.join(ROOT, "tests")

def _read(path):
    """Read a file and CLOSE it in the same breath.

    `open(p).read()` hands the fd to the garbage collector, and every scan in
    this file opens EVERY module in the package — so a single run emitted
    fifteen ResourceWarnings and a full suite emits hundreds. The cost is not
    the fds; it is that a warning channel nobody can read is a warning channel
    where a real one goes unseen."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _seats_aliases(tree):
    """What this file calls the seats module — often not "seats".

    `import helm.seats as _s`, `from helm import seats as S`,
    `... as seats_mod`, `as _seats`, `as seatsmod` are all live in this repo,
    and a scan that only matches the literal spelling `seats.` is blind to
    every one of them. Returns the empty set for a file that rebinds `seats`
    as a local (tests/test_presence.py builds a LIST called seats), because
    there every `seats.X` is ambiguous and proves nothing about our module.
    """
    names, rebound = set(), False
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                # n.level > 0 IS THE RELATIVE CASE and it is the common one:
                # `from . import seats` and `from .. import seats` both have
                # module=None, so a check on the module string alone silently
                # skips helm/gate.py and helm/work/_gc.py — the two files that
                # motivated widening this scan in the first place. The must-hit
                # control caught it; nothing else would have.
                if a.name == "seats" and (n.level > 0
                                          or (n.module or "").startswith("helm")):
                    names.add(a.asname or "seats")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name in ("helm.seats", "seats"):
                    names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            if n.id == "seats":
                rebound = True
    if rebound:
        names.discard("seats")
    return names


def _optional(src, name):
    """A caller that wrote `hasattr(seats, "x")` has DECLARED x optional and
    already handles its absence. Demanding it would make the facade owe a
    name its only caller is prepared to live without."""
    return ('hasattr(seats, "%s")' % name) in src or \
           ("hasattr(seats, '%s')" % name) in src


def _py_files():
    """RECURSIVE. The first version used os.listdir and therefore skipped
    helm/work/ and helm/inject/ entirely — and helm/work/_gc.py calls three
    names this split has already moved (_now_mono, _sweep, claims_path) in
    the exact literal spelling the scan was built to catch. A scan that
    silently declines to look somewhere is worse than one that cannot: its
    green is read as coverage."""
    for root in (HELM, TESTS):
        for base, _dirs, files in os.walk(root):
            if "__pycache__" in base:
                continue
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                if base == HELM and fn.startswith("seats"):
                    continue        # the split's own modules are not callers
                yield os.path.join(base, fn)


def _callers():
    """Every name reached for on the seats module from outside it.

    AST, NOT REGEX, and the difference was measured: a regex matching
    seats-dot-name matched the STRING "seats.py" in eleven files and reported
    `py` as a missing export. This scan's first run produced three findings
    and all three were bugs in itself.
    """
    found = {}
    for path in _py_files():
        try:
            src = _read(path)
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        aliases = _seats_aliases(tree)
        if not aliases:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in aliases
                    and not _optional(src, n.attr)):
                found.setdefault(n.attr, path)
    return found


class FacadeCompletenessTest(unittest.TestCase):
    def test_every_name_a_caller_reaches_for_resolves_on_seats(self):
        """THE ONE THAT WOULD HAVE CAUGHT IT, and will catch the next eleven."""
        callers = _callers()

        # MUST-HIT CONTROL, unconditional. A scan that silently found nothing
        # passes this test perfectly while proving nothing at all, and an
        # empty result is exactly what a typo in _REF or a renamed directory
        # produces. These three are known-live external callers.
        self.assertGreater(len(callers), 50,
                           "the caller scan found almost nothing, so its "
                           "green says nothing about the facade")
        # _now_mono is reached ONLY from helm/work/_gc.py — a subpackage the
        # first version of this scan never walked. It is in the control
        # list precisely so the recursion cannot silently regress.
        for known in ("roster", "stop_guard", "_gate_repo_id", "_now_mono"):
            self.assertIn(known, callers,
                          "%s has a real external caller and the scan missed "
                          "it — the scan is broken, not the facade" % known)

        # noqa: VACUOUS_ASSERTION — the observable IS an absence (no name
        # unresolved), and its positive control is the unconditional
        # assertGreater/assertIn block directly above: the scan is proven
        # non-empty AND proven to contain three known-live names before the
        # emptiness of `missing` is allowed to mean anything.
        # KNOWN PRE-EXISTING OFFENDER, pinned rather than ignored, and the
        # staleness assert below is what keeps that honest. is_rostered_name
        # is called at helm/work/_gc.py:241 and has NEVER existed — not here
        # and not on origin/main. It is not split fallout; this scan simply
        # became the first thing to look in helm/work/ at all. It is being
        # fixed on lane gc-triage-rostered-check-never-ran, and when that
        # lands the assert below FAILS and tells whoever is here to delete
        # this pin. A known-issues list that cannot expire becomes the place
        # findings go to be forgotten.
        KNOWN = {"is_rostered_name"}
        self.assertTrue(
            KNOWN <= set(callers),
            "a pinned known-offender is no longer reached by any caller — it "
            "was fixed or removed. DELETE it from KNOWN: %s"
            % sorted(KNOWN - set(callers)))
        for name in sorted(KNOWN):
            self.assertFalse(
                hasattr(seats, name),
                "%s now RESOLVES, so the defect this pin records is fixed — "
                "delete it from KNOWN so the scan goes back to strict" % name)

        missing = sorted((n, p) for n, p in callers.items()
                         if not hasattr(seats, n) and n not in KNOWN)
        self.assertEqual(missing, [], "names reached for through `seats.` that "
                                      "the facade no longer exports: %s"
                                      % [n for n, _ in missing])

    def test_the_facade_only_ever_shrinks(self):
        """A RATCHET, because the split is mid-flight and honesty beats a
        green that means nothing.

        The finish line is 1000 lines for every file including seats.py, and
        seats.py is not there yet — asserting the finish line today would be
        a test that fails on purpose, which is a test nobody can gate on.
        Asserting nothing until the end is worse: the facade could grow all
        the way through the split and no one would see it.

        So this pins the CURRENT size and refuses growth. Every extraction
        lowers CEILING; the last one lowers it to 1000 and this becomes the
        finish-line test it was always going to be. A ratchet cannot be
        satisfied by standing still, and it cannot be quietly reversed."""
        # THE RATCHET REACHED ITS FINISH LINE. Every extraction lowered
        # this; the last one lowered it to FINISH, and the two assertions
        # below are now the same assertion. It stays a ratchet rather than
        # collapsing into one check, because the facade can still GROW —
        # and if a future change legitimately moves code back in, the
        # failure should say so in the language of the split.
        CEILING = 1000          # arrived: seats.py is 580
        FINISH = 1000

        sizes = {}
        for fn in sorted(os.listdir(HELM)):
            if fn == "seats.py" or fn.startswith("seats_"):
                sizes[fn] = len(_read(os.path.join(HELM, fn)).splitlines())
        # CONTROL: an empty scan would satisfy every assertion below.
        self.assertGreaterEqual(len(sizes), 3,
                                "the module scan found fewer files than have "
                                "already been extracted, so it is not looking "
                                "where the split is happening")
        self.assertIn("seats.py", sizes, "the facade itself was not scanned")

        over = {f: n for f, n in sizes.items()
                if f != "seats.py" and n > FINISH}
        self.assertEqual(over, {}, "an EXTRACTED module is over the %d-line "
                                   "budget, which defeats the split: %s"
                                   % (FINISH, over))
        self.assertLessEqual(
            sizes["seats.py"], CEILING,
            "seats.py GREW past the ratchet (%d). The split is supposed to "
            "drain this file; if an extraction legitimately moves code back "
            "in, say why in the commit and lower CEILING deliberately."
            % CEILING)


# THE SCOPE MODEL IS DELETED, NOT VERSIONED. Its history is the argument:
# seven versions, twenty-two verified defect classes (seven found by one
# review, nine by the next, each fix flipping which direction it lied in),
# and ZERO real findings ever — every bug it surfaced was a bug in itself.
# A hand-rolled reimplementation of Python's scoping rules is a second,
# unverified interpreter living in the test suite, and its review cost had
# already exceeded the value of the question it asked.
#
# THE QUESTION SURVIVES; THE ANSWERER IS NOW CPYTHON'S OWN. `symtable` is
# the stdlib front-end to the compiler's symbol-table pass — the same
# analysis that decides LOAD_GLOBAL vs LOAD_FAST when the module really
# runs. Defs, classes, lambdas, comprehensions (including their 3.12 PEP 709
# inlining), walrus targets, match captures, decorators, defaults,
# annotations, `global`/`nonlocal`: none of that is modelled here anymore,
# because it is asked of the implementation that defines it. What remains
# ours is one set-membership question per symbol.
#
# THE CONTRACT, stated so its edges are deliberate: every bare-name LOAD
# that CPython resolves to MODULE scope must exist at module scope — bound
# at module level, bound by a `global` declaration that assigns, or a
# builtin. TWO EDGES, and every one of them is MEASURED rather than waved
# at (OldDefectRuntimeWitnessTest, one witness per old-model defect class):
#   OUT OF CONTRACT — anything runtime decides by ORDER or by SUCCESS,
#   which a symbol table structurally cannot see: use-after-`del`, an
#   `except` target read after its handler's implicit delete, a match
#   subject read before its own capture binds, a failed optional import
#   whose alias never bound. Each is checker-clean while runtime raises
#   NameError, and each disagreement is pinned by a witness that re-runs
#   the old repro against the interpreter.
#   STRICTER THAN RUNTIME, deliberately — a flagged name runtime tolerates:
#   `from x import *` (unenumerable statically; witnessed unreachable in
#   the scanned tree), and annotations wherever the running interpreter's
#   symtable still records them (everywhere through 3.13; 3.14 defers
#   evaluation and its symtable drops local/future ones). A name only an
#   annotation uses must still resolve at module scope.
# A checker that answers a narrower question correctly beats one that
# answers a wider question wrongly.
DUNDERS = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
           "__qualname__", "__module__", "__class__", "__dict__",
           "__builtins__", "__loader__", "__annotations__"}
BUILTIN = set(dir(builtins)) | DUNDERS

# CPython 3.14's PEP 649 machinery: symtable reports the compiler-internal
# cell __conditional_annotations__ as a referenced global in the TOP table
# for ANY module-scope annotated assignment — `x: int = 1`, fully bound,
# flags a name nobody wrote. THE EXCUSAL FOR IT IS RETIRED, NOT CURED.
# Three rounds each lied in one direction: membership in DUNDERS blinded
# the checker to a REAL load of the name anywhere in the tree (false
# negative); tree-wide source-mention subtraction (`- mentioned`) turned
# the excusal off for four shapes where the mention is not a module-global
# load — function-local assignment, lambda parameter, comprehension
# target, class-local binding — plus stringized future annotations,
# flagging a bound module on 3.14 (false positives, @codex-2, every case
# reproduced), and let the synthetic top entry win setdefault over a real
# nested load (misattribution). Curing THAT means deciding "is this
# mention an EVALUATED module-global bare load?" — a hand-rolled scope-
# and-evaluation model, the same second interpreter this file already
# deleted once at a cost of twenty-two defect classes.
#
# MEASURED before retiring (all six interpreters, 2026-08-05):
#   - ZERO AnnAssign nodes of any kind in helm/ + tests/ — the excusal
#     protected a hypothetical;
#   - only a MODULE-SCOPE AnnAssign emits the cell, only on 3.14, always
#     in the top table; class-level, parameter, and function-local
#     annotations never do, any version, future import or not;
#   - with no module-scope annotation there is nothing to discriminate: a
#     hand-written load of the cell is an ordinary undefined global —
#     flagged, attributed to its own table, NameError at runtime — on all
#     six interpreters identically.
# So the checker carries no excusal, and the one condition that would
# need one is BANNED where the checker scans: ModuleAnnotationBanTest
# fails on every interpreter, with task/359 as the pointer, the day a
# seats module gains a module-scope annotation. No excusal means no false
# negative and no false positive; the cost, chosen deliberately, is that
# a legitimate module-scope annotation stays blocked until task/359
# builds the discriminator properly.


def _undefined_globals(src, filename):
    """{name: scope} for every load symtable resolves to a module global
    that the module does not bind and builtins do not supply.

    A SyntaxError here is a FINDING, never a skip — the caller scan's old
    `except SyntaxError: continue` is the shape of a scanner that silently
    declines to look, and its green gets read as coverage."""
    top = symtable.symtable(src, filename, "exec")
    module_bound = {s.get_name() for s in top.get_symbols()
                    if s.is_local() or s.is_imported()}

    def declared_assigned(t):
        # `global X; X = ...` anywhere binds X at module level at runtime.
        for s in t.get_symbols():
            if s.is_declared_global() and s.is_assigned():
                module_bound.add(s.get_name())
        for c in t.get_children():
            declared_assigned(c)
    declared_assigned(top)

    known = module_bound | BUILTIN
    bad = {}

    def walk(t):
        for s in t.get_symbols():
            if (s.is_referenced() and s.is_global()
                    and s.get_name() not in known):
                bad.setdefault(s.get_name(), t.get_name())
        for c in t.get_children():
            walk(c)
    walk(top)
    return bad


def _module_scope_annassigns(src, filename):
    """Line numbers of every AnnAssign whose nearest enclosing SCOPE is the
    module — the one shape that makes 3.14's symtable emit the compiler
    cell (see the retirement note above). Plain statement nesting (if/try/
    with/for/match) does not leave module scope and DOES emit the cell
    (measured — PEP 649's conditional-annotations machinery exists for
    exactly that case); a class or function body leaves module scope and
    never emits it, so those annotations stay legal and the ban is exactly
    as wide as the defect."""
    hits = []

    def walk(node, in_scope):
        for child in ast.iter_child_nodes(node):
            if in_scope and isinstance(child, ast.AnnAssign):
                hits.append(child.lineno)
            walk(child, in_scope and not isinstance(
                child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
    walk(ast.parse(src, filename), True)
    return hits


# MUST-HIT PROBES, BOTH DIRECTIONS. These are not tests of symtable —
# CPython tests symtable. They are the positive control on
# _undefined_globals' own set arithmetic: a checker whose module_bound
# collection breaks returns {} on everything, and the real-module scan
# below would go green while proving nothing. Each triple is (label,
# source, names that MUST be flagged — empty means MUST be clean).
#
# This table once claimed "one per defect class the old model had", and
# @codex-2 measured that claim false: nine exact old repros still
# disagreed with runtime, unwitnessed — the deleted scanner's disease one
# level up, a confident coverage statement nobody re-ran. The per-defect
# claim now lives where it can be measured: OldDefectRuntimeWitnessTest
# below runs every old repro against BOTH oracles — the interpreter and
# the checker — and asserts the relation between their answers.
_PROBES = [
    ("moved fn calls a helper that stayed behind",
     "def f(x):\n    return _stayed_behind(x)\n", {"_stayed_behind"}),
    ("helper present: clean",
     "def _h(x):\n    return x\n\ndef f(x):\n    return _h(x)\n", set()),
    ("def and class names bind",
     "class C:\n    pass\n\ndef g():\n    return C()\n", set()),
    ("class local does NOT serve a method",
     "class C:\n    LOCAL = 1\n    def m(self):\n        return LOCAL\n",
     {"LOCAL"}),
    ("class body sees the module",
     "K = 2\nclass C:\n    V = K\n", set()),
    ("comprehension target does not leak outward",
     "def f(y):\n    r = [h for h in y]\n    return h\n", {"h"}),
    ("comprehension iterable resolves outward",
     "def f():\n    return [v for v in missing_src]\n", {"missing_src"}),
    ("lambda param does not leak outward",
     "def f():\n    g = lambda hidden: hidden\n    return hidden\n",
     {"hidden"}),
    ("except target serves its own handler",
     "def f():\n    try:\n        pass\n    except OSError as e:\n"
     "        return str(e)\n", set()),
    ("import, dotted import, and alias all bind",
     "import os\nimport os.path\nimport types as _t\n\n"
     "def f():\n    return os.sep, os.path.sep, _t.ModuleType\n", set()),
    ("global declared and assigned elsewhere serves a reader",
     "def init():\n    global G\n    G = 1\n\ndef use():\n    return G\n",
     set()),
    ("global declared but never assigned anywhere",
     "def use():\n    global G2\n    return G2\n", {"G2"}),
    ("walrus in a comprehension binds in the enclosing scope",
     "def f(y):\n    vals = [v for v in y if (m := v)]\n    return m\n",
     set()),
    ("defaults evaluate in the enclosing scope",
     "def f(a=DEFAULT_MISSING):\n    return a\n", {"DEFAULT_MISSING"}),
    ("decorators evaluate in the enclosing scope",
     "@missing_deco\ndef f():\n    pass\n", {"missing_deco"}),
    ("annotations reference real names (no future import)",
     "def f(x: MissingType):\n    return x\n", {"MissingType"}),
    ("closures see enclosing locals",
     "def f():\n    x = 1\n    def g():\n        return x\n    return g\n",
     set()),
]
if sys.version_info >= (3, 10):
    # match/case sources cannot PARSE below 3.10, so these probes are gated
    # — and so is any production use: a match statement in a seats module
    # would fail the 3.9 CI leg at import, loudly, long before this test.
    _PROBES += [
        ("match captures bind",
         "def f(v):\n    match v:\n        case [a, *rest]:\n"
         "            return a, rest\n    return None\n", set()),
        ("a match arm's own load still resolves",
         "def f(v):\n    match v:\n        case int():\n"
         "            return match_helper(v)\n    return None\n",
         {"match_helper"}),
        ("mapping rest binds",
         "def f(v):\n    match v:\n        case {**rest}:\n"
         "            return rest\n    return None\n", set()),
    ]


class ExtractedModuleHealthTest(unittest.TestCase):
    def test_the_checker_discriminates_before_the_tree_is_trusted(self):  # noqa: VACUOUS_ASSERTION — the flagging half of the probe table is the unconditional positive control, and assertGreaterEqual(flagged, 7) pins that the table keeps it
        """Every defect class the hand-rolled model ever exhibited, asked of
        the symtable-backed checker in BOTH directions. This is the must-hit
        control: it fails loudly if _undefined_globals' own arithmetic — the
        only part that is still ours — stops flagging or starts lying."""
        for label, src, expect in _PROBES:
            got = set(_undefined_globals(src, "<probe>"))
            self.assertEqual(got, expect,
                             "probe %r: flagged %s, expected %s"
                             % (label, sorted(got), sorted(expect)))
        flagged = sum(1 for _l, _s, e in _PROBES if e)
        self.assertGreaterEqual(flagged, 7,
                                "the probe table lost its flagging half — "
                                "an all-clean table cannot catch a checker "
                                "that returns {} for everything")

    def test_no_extracted_module_references_an_undefined_global(self):  # noqa: VACUOUS_ASSERTION — absence observable; the probe-table test above proves the same checker flags every known defect class, and checked>=3 proves this scan visited the modules
        """A split's characteristic break: a moved function keeps calling a
        name that stayed behind, and nothing notices until that branch runs.

        Import does NOT catch this (the reference lives inside a function
        body), and neither does a green suite — measured, not asserted:
        helm/work/_gc.py calls seats.is_rostered_name, a name that has NEVER
        existed, inside `except Exception`, and the whole suite passed 9271
        tests around it. Only a scan that asks the resolution question
        directly can see this class before a production path dies."""
        checked, bad = 0, {}
        for fn in sorted(os.listdir(HELM)):
            if not (fn == "seats.py" or fn.startswith("seats_")):
                continue
            path = os.path.join(HELM, fn)
            found = _undefined_globals(_read(path), path)
            if found:
                bad[fn] = sorted(found)
            checked += 1

        self.assertGreaterEqual(checked, 3, "fewer modules checked than have "
                                            "been extracted — the scan is "
                                            "looking in the wrong place")
        # noqa: VACUOUS_ASSERTION — absence observable; the probe control
        # above proves the checker flags every known defect class, and the
        # assertGreaterEqual proves the scan visited the extracted modules,
        # before this emptiness is allowed to mean anything.
        self.assertEqual(bad, {}, "undefined globals after extraction: %s" % bad)


class ModuleAnnotationBanTest(unittest.TestCase):
    def test_no_scanned_module_has_a_module_scope_annotation(self):  # noqa: VACUOUS_ASSERTION — the detector must-hit block is the unconditional positive control, and checked>=3 pins that the real scan looked
        """THE CONTRACT THAT REPLACED AN EXCUSAL. On 3.14 a module-scope
        annotated assignment makes symtable emit __conditional_annotations__
        as a referenced global the checker cannot tell from a hand-written
        load; three excusal rounds each lied in one direction (retirement
        note above _undefined_globals). So the condition is forbidden where
        the checker scans, and forbidden LOUDLY: this fails on every
        interpreter — not just the 3.14 gate box — the day the shape lands,
        with the task that owns the proper cure in the message."""
        # MUST-HIT first, both directions. A detector that returns [] for
        # everything satisfies the real scan below perfectly, so it is
        # proven to fire — and proven NOT to fire on the class/function
        # shapes that never emit the cell, so the ban cannot silently
        # widen past the defect it exists for.
        for label, src, lines in (
                ("plain",       "x: int = 1\n",              [1]),
                ("bare",        "x: int\n",                  [1]),
                ("under if",    "if True:\n    x: int = 1\n", [2]),
                ("with future", "from __future__ import annotations\n"
                                "x: int = 1\n",              [2])):
            self.assertEqual(_module_scope_annassigns(src, "<ban>"), lines,
                             "ban detector missed the %s module-scope "
                             "annotation — the real scan below is blind"
                             % label)
        for label, src in (
                ("class-level",    "class C:\n    y: int = 2\n"),
                ("parameter",      "def f(x: int):\n    return x\n"),
                ("function-local", "def f():\n    z: int = 3\n    return z\n")):
            self.assertEqual(_module_scope_annassigns(src, "<ban>"), [],
                             "the ban widened to a %s annotation, which "
                             "never emits the compiler cell on any "
                             "interpreter (measured) — it must stay legal"
                             % label)

        checked, offenders = 0, {}
        for fn in sorted(os.listdir(HELM)):
            if not (fn == "seats.py" or fn.startswith("seats_")):
                continue
            path = os.path.join(HELM, fn)
            lines = _module_scope_annassigns(_read(path), path)
            if lines:
                offenders[fn] = lines
            checked += 1
        self.assertGreaterEqual(checked, 3, "the ban scan visited fewer "
                                            "modules than are extracted")
        self.assertEqual(
            offenders, {},
            "a seats module gained a MODULE-SCOPE annotated assignment. "
            "3.14's symtable emits a compiler cell "
            "(__conditional_annotations__) for that shape which the split "
            "contract cannot yet distinguish from a hand-written load — "
            "the checker scan on 3.14 will also name that cell; it is this "
            "same issue. Either hoist the annotation into a class/function "
            "or build the discriminator first — see task/359. Offenders "
            "(file: line numbers): %s" % offenders)


def _run_module(src):
    """Execute src as a fresh synthetic module namespace.

    Returns ("ok", namespace) or (exception-type-name, str(exc)).
    In-process exec against a fresh dict was CROSS-CHECKED against real
    python3.9 and python3.14 file runs for every witness source below:
    identical outcomes, including PEP 649 deferral (a function's
    __annotate__ closes over the exec namespace exactly as it closes over
    real module globals)."""
    ns = {"__name__": "witness_mod"}
    try:
        exec(compile(src, "<witness>", "exec"), ns)
    except Exception as e:              # SyntaxError included, deliberately
        return type(e).__name__, str(e)
    return "ok", ns


class OldDefectRuntimeWitnessTest(unittest.TestCase):
    """THE OLD REPROS, RE-RUN — against the INTERPRETER, not against any
    model. @codex-2 measured the gap this closes: the deletion commit
    claimed one probe per old defect class while nine exact old repros
    still disagreed with runtime, unwitnessed. So each witness executes the
    old repro and asks BOTH oracles — what does this interpreter actually
    do, and what does the checker say — and the assertion is the RELATION
    between the two answers. Where they agree, the class is in contract and
    symtable catches what the hand-rolled model missed. Where they
    structurally cannot agree (liveness, conditional binding, star imports,
    annotation timing), the disagreement ITSELF is pinned, so the
    contract's exclusion clauses are measurements rather than prose.

    Version forks are explicit if/else with BOTH arms asserting — a
    witness that silently skips on the gate's interpreter is worse than no
    witness. CI runs 3.9-3.13, the gate box runs 3.14; every arm below was
    measured on all six interpreters before it was written down."""

    def _both(self, src):
        return set(_undefined_globals(src, "<witness>")), _run_module(src)

    def test_comprehension_later_target_order_is_caught_now(self):
        """`[y for y in ys for ys in data]`: the FIRST iterable evaluates
        in the enclosing scope, before the later `for` binds ys. The old
        model ordered targets first and called it clean. symtable agrees
        with runtime on every interpreter: flagged, and NameError live —
        this class is IN contract now. Unrolled — module scope, then
        function scope — so every assertion is unconditional."""
        flagged, (kind, detail) = self._both(
            "r = [y for y in ys for ys in [[1]]]\n")
        self.assertEqual(flagged, {"ys"})
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'ys'", detail)
        flagged, (kind, detail) = self._both(
            "def f(data):\n    return [y for y in ys for ys in data]\n\n"
            "r = f([[1]])\n")
        self.assertEqual(flagged, {"ys"})
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'ys'", detail)

    def test_except_target_deletion_after_handler_is_liveness_measured(self):
        """The handler's implicit `del e` is a LIVENESS fact: symtable sees
        a module-scope binding and no symbol table can see the delete.
        Checker-clean, runtime-NameError — the exclusion the contract
        declares, held up against the interpreter."""
        src = ("try:\n    raise OSError('synthetic')\n"
               "except OSError as e:\n    pass\nr = e\n")
        flagged, (kind, detail) = self._both(src)
        self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — the emptiness is the measured half of a pinned DISAGREEMENT; its same-source positive control is the NameError asserted on the next line
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'e'", detail)

    def test_match_subject_before_capture_disagrees_or_both_refuse(self):
        """A capture pattern binds its name at the match's scope, so the
        SUBJECT reading that same name looks resolved to symtable — order
        is invisible to a symbol table. Runtime NameErrors on the subject.
        On 3.9 the source cannot parse: BOTH oracles refuse with
        SyntaxError, and the checker's docstring promise (a SyntaxError is
        a finding, never a skip) is what this arm exercises."""
        src = "match w:\n    case w:\n        pass\nr = w\n"
        if sys.version_info >= (3, 10):
            flagged, (kind, detail) = self._both(src)
            self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — the emptiness is the measured half of a pinned DISAGREEMENT; its same-source positive control is the NameError asserted on the next line
            self.assertEqual(kind, "NameError", detail)
            self.assertIn("'w'", detail)
        else:
            with self.assertRaises(SyntaxError):
                _undefined_globals(src, "<witness>")
            kind, _detail = _run_module(src)
            self.assertEqual(kind, "SyntaxError")

    def test_a_failed_optional_import_alias_binds_statically_only(self):
        """An import statement IS a module-scope binding to symtable
        whether or not it succeeds at runtime — conditional binding is
        liveness's sibling, equally invisible, equally out of contract.
        The old model's defect and the checker's edge are the same shape;
        the difference is this one is pinned."""
        src = ("try:\n    import synthetic_absent_module_xyz as opt\n"
               "except ImportError:\n    pass\n\n"
               "def f():\n    return opt\n\nr = f()\n")
        flagged, (kind, detail) = self._both(src)
        self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — the emptiness is the measured half of a pinned DISAGREEMENT; its same-source positive control is the NameError asserted on the next line
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'opt'", detail)
        # The cure-shape — bind the alias in the except arm — is clean on
        # BOTH oracles, so the witness shows the repair, not just the wound.
        cured = src.replace("    pass\n", "    opt = None\n")
        flagged, (kind, ns) = self._both(cured)
        self.assertEqual(kind, "ok")
        self.assertIsNone(ns["r"])
        self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — agreement arm; the ok/None asserts above are the same-source positive control

    def test_star_import_is_the_one_false_positive_and_is_unreachable(self):
        """The single direction where the checker OVER-reports: a star
        import's bindings cannot be enumerated without importing, so `sin`
        is flagged while runtime resolves it fine. Safe only while no
        scanned module star-imports — so that unreachability is asked of
        the real tree every run, not assumed. The day a seats module gains
        `from x import *`, this fails and says the scan would lie there."""
        src = "from math import *\n\ndef f():\n    return sin(0)\n\nr = f()\n"
        flagged, (kind, ns) = self._both(src)
        self.assertEqual(flagged, {"sin"})
        self.assertEqual(kind, "ok")
        self.assertEqual(ns["r"], 0.0)
        offenders, scanned = [], 0
        for fn in sorted(os.listdir(HELM)):
            if not (fn == "seats.py" or fn.startswith("seats_")):
                continue
            scanned += 1
            for n in ast.walk(ast.parse(_read(os.path.join(HELM, fn)))):
                if (isinstance(n, ast.ImportFrom)
                        and any(a.name == "*" for a in n.names)):
                    offenders.append(fn)
        self.assertGreaterEqual(scanned, 3, "the star-import sweep visited "
                                            "fewer files than are extracted")
        self.assertEqual(offenders, [])  # noqa: VACUOUS_ASSERTION — the flagged {'sin'} above proves the false-positive class is real, and scanned>=3 proves this sweep looked; the emptiness is the unreachability claim itself

    def test_load_after_del_is_the_liveness_exclusion_measured(self):
        src = "X = 1\ndel X\n\ndef f():\n    return X\n\nr = f()\n"
        flagged, (kind, detail) = self._both(src)
        self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — the emptiness is the measured half of a pinned DISAGREEMENT; its same-source positive control is the NameError asserted on the next line
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'X'", detail)

    def test_deferred_function_annotations_agree_at_evaluation_time(self):
        """PEP 649 (default in 3.14) moved WHEN a bare function annotation
        evaluates — def-time through 3.13, first-__annotations__-access
        from 3.14. The checker flags on every interpreter, and runtime
        raises on every interpreter too, at that version's own evaluation
        point: the flag is AGREEMENT at evaluation time, and deliberately
        stricter than 3.14's import time."""
        defcall = "def f(x: MissingAnno):\n    return x\n\nr = f(1)\n"
        flagged, (kind, detail) = self._both(defcall)
        self.assertEqual(flagged, {"MissingAnno"})
        if sys.version_info >= (3, 14):
            self.assertEqual(kind, "ok", detail)      # deferred past the call
        else:
            self.assertEqual(kind, "NameError", detail)   # eager at def
            self.assertIn("'MissingAnno'", detail)
        introspect = ("def f(x: MissingAnno):\n    return x\n\n"
                      "r = f.__annotations__\n")
        flagged, (kind, detail) = self._both(introspect)
        self.assertEqual(flagged, {"MissingAnno"})
        self.assertEqual(kind, "NameError", detail)   # EVERY interpreter
        self.assertIn("'MissingAnno'", detail)

    def test_variable_annotations_across_interpreters(self):
        """Two variable-annotation shapes whose runtime/checker relation
        moves with the interpreter, both arms explicit.

        LOCAL: PEP 526 never evaluates a local variable annotation on ANY
        version — runtime is ok everywhere. symtable through 3.13 still
        records the name as a load (checker STRICTER than runtime, the
        deliberate edge); 3.14's lazy annotation scopes drop it (checker
        agrees)."""
        local = "def f():\n    x: MissingAnno = 1\n    return x\n\nr = f()\n"
        flagged, (kind, ns) = self._both(local)
        self.assertEqual(kind, "ok")
        self.assertEqual(ns["r"], 1)
        if sys.version_info >= (3, 14):
            self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — 3.14 agreement arm; the ok/1 asserts above are the same-source positive control, and the <3.14 arm below pins the non-empty direction
        else:
            self.assertEqual(flagged, {"MissingAnno"})
        # MODULE-LEVEL, no future import: eagerly evaluated through 3.13 —
        # module exec raises, the "Py3.9 semantics", true up to 3.13 —
        # and deferred on 3.14. The checker flags MissingAnno on every
        # interpreter: agreement through 3.13, evaluation-time strictness
        # on 3.14 — where the compiler cell ALSO appears, unexcused: this
        # is a module-scope annotation, a BANNED shape, and the raw
        # limitation is pinned rather than papered over (retirement note
        # above _undefined_globals).
        mod = "x: MissingAnno = 1\nr = x\n"
        flagged, (kind, detail) = self._both(mod)
        if sys.version_info >= (3, 14):
            self.assertEqual(flagged,
                             {"MissingAnno", "__conditional_annotations__"})
            self.assertEqual(kind, "ok", detail)
        else:
            self.assertEqual(flagged, {"MissingAnno"})
            self.assertEqual(kind, "NameError", detail)
            self.assertIn("'MissingAnno'", detail)

    def test_future_annotations_are_strings_and_the_checker_knows_from_310(self):
        """PEP 563 stringifies every annotation: runtime never evaluates
        one, even under introspection — __annotations__ returns SOURCE
        TEXT, asserted below so "never evaluated" is a measurement.
        symtable stopped recording stringified annotation names in 3.10;
        on 3.9 the checker still flags them — strict side only, and the
        3.9 arm is the "Py3.9 future annotations" defect class re-run."""
        modvar = ("from __future__ import annotations\n"
                  "x: MissingAnno = 1\nr = x\n")
        fnann = ("from __future__ import annotations\n\n"
                 "def f(x: MissingAnno):\n    return x\n\n"
                 "r = f(1)\nann = f.__annotations__\n")
        for src in (modvar, fnann):
            flagged, (kind, ns) = self._both(src)
            self.assertEqual(kind, "ok")
            self.assertEqual(ns["r"], 1)
            if sys.version_info >= (3, 14) and src is modvar:
                # A MODULE-SCOPE annotation emits the compiler cell even
                # stringized (@codex-2's case, measured: 3.14 tracks which
                # conditional annotations ran regardless of PEP 563) — the
                # banned shape again, raw limitation pinned; the fnann arm
                # below proves a function annotation never drags the cell
                # in, so the ban's narrowness holds under future imports.
                self.assertEqual(flagged, {"__conditional_annotations__"})
            elif sys.version_info >= (3, 10):
                self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — agreement arm; the ok/1 asserts above are the same-source positive control, and the 3.9 arm below pins the non-empty direction
            else:
                self.assertEqual(flagged, {"MissingAnno"}, src)
        _f, (_k, ns) = self._both(fnann)
        self.assertEqual(ns["ann"], {"x": "MissingAnno"})

    def test_a_bound_module_annotation_is_why_the_ban_exists(self):
        """THE LIMITATION, PINNED RAW — the reason ModuleAnnotationBanTest
        exists. `x: int = 1` is fully bound and runs clean on every
        interpreter, but 3.14's symtable emits the compiler cell as a
        referenced global in top, by flags IDENTICAL to a hand-written
        load (measured: global, referenced, unassigned, same table). The
        checker carries no excusal — three rounds of one lied in both
        directions — so the 3.14 arm asserts the flag IS reported, and the
        ban is what keeps the shape out of the scanned tree. This arm is
        the mutation tripwire: restoring either shipped excusal (the
        round-1 DUNDERS membership or the round-2 `- mentioned`
        subtraction) returns {} for this source on 3.14 and goes red
        here."""
        flagged, (kind, ns) = self._both("x: int = 1\nr = x\n")
        self.assertEqual(kind, "ok")
        self.assertEqual(ns["r"], 1)
        if sys.version_info >= (3, 14):
            self.assertEqual(
                flagged, {"__conditional_annotations__"},
                "3.14 reported nothing for a module-scope annotation: "
                "either an excusal crept back into _undefined_globals, or "
                "the interpreter stopped emitting the cell and the ban's "
                "reason needs re-measuring — task/359 either way")
        else:
            self.assertEqual(flagged, set())  # noqa: VACUOUS_ASSERTION — pre-3.14 arm of an explicit fork whose 3.14 arm above pins the non-empty direction; the ok/1 asserts are the same-source positive control

    def test_a_hand_written_load_of_the_synthetic_cell_is_flagged_and_raises(self):
        """THE MUST-HIT — @codex-2's round-1 repro, kept red, now carrying
        ATTRIBUTION. The round-1 excusal put the cell in DUNDERS, making
        it universally known: this exact source came back {} from the
        checker while runtime NameError'd — a false negative bought to
        cure a false positive. With NO excusal a hand-written load is
        ordinary arithmetic: flagged, reported against the table that
        contains the load — round 2 let a synthetic top entry win the
        setdefault over a real nested load, so the ATTRIBUTION is asserted
        here, not just the flag — and NameError, both scopes, every
        interpreter, no version fork (measured on all six: without a
        module-scope annotation nothing binds the cell, 3.14 included).
        Unrolled — function scope, then module scope — so every assertion
        is unconditional."""
        # function scope — the repro as filed, attributed to f, not top
        src = "def f():\n    return __conditional_annotations__\n\nr = f()\n"
        self.assertEqual(_undefined_globals(src, "<witness>"),
                         {"__conditional_annotations__": "f"},
                         "the load lives in f and must be reported there")
        _flagged, (kind, detail) = self._both(src)
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'__conditional_annotations__'", detail)
        # module scope — a bare load in top itself
        src = "r = __conditional_annotations__\n"
        self.assertEqual(_undefined_globals(src, "<witness>"),
                         {"__conditional_annotations__": "top"})
        _flagged, (kind, detail) = self._both(src)
        self.assertEqual(kind, "NameError", detail)
        self.assertIn("'__conditional_annotations__'", detail)

    def test_every_shape_the_excusal_lied_about_is_screened_by_the_ban(self):
        """@codex-2's round-2 false-positive table, rendered impossible
        rather than cured. Each banned source pairs a module-scope
        annotation (the one shape that emits the compiler cell) with a
        mention of the cell that is NOT a module-global load; under the
        round-2 `- mentioned` excusal the checker flagged top on 3.14
        while runtime ran clean — measured, every case, plus the nested-
        load case whose real finding was misattributed to top. Retiring
        the excusal does not make the checker right about these sources;
        the BAN makes them unreachable: every one carries a module-scope
        AnnAssign, so the detector ModuleAnnotationBanTest gates the real
        tree on fires for each. The same shapes WITHOUT the annotation are
        clean on both oracles on all six interpreters — asserted below, so
        the screen is proven no wider than the defect."""
        banned = [
            ("fn-local assign/return",
             "x: int = 1\n\ndef f():\n"
             "    __conditional_annotations__ = 5\n"
             "    return __conditional_annotations__\n\nr = f()\n"),
            ("lambda parameter",
             "x: int = 1\ng = lambda __conditional_annotations__: "
             "__conditional_annotations__\nr = g(7)\n"),
            ("comprehension target",
             "x: int = 1\nr = [__conditional_annotations__ for "
             "__conditional_annotations__ in [3]]\n"),
            ("class-local binding",
             "x: int = 1\n\nclass C:\n    __conditional_annotations__ = 9\n\n"
             "r = C.__conditional_annotations__\n"),
            ("stringized future annotation",
             "from __future__ import annotations\n"
             "x: __conditional_annotations__ = 1\nr = x\n"),
            ("real nested load beside the annotation (misattributed to top)",
             "x: int = 1\n\ndef f():\n"
             "    return __conditional_annotations__\n\nr = f()\n"),
        ]
        for label, src in banned:
            self.assertTrue(
                _module_scope_annassigns(src, "<witness>"),
                "%s: the ban does not screen this source, so the checker's "
                "wrong answer about it is reachable again — task/359"
                % label)
        clean = [
            ("fn-local assign/return",
             "def f():\n    __conditional_annotations__ = 5\n"
             "    return __conditional_annotations__\n\nr = f()\n", 5),
            ("lambda parameter",
             "g = lambda __conditional_annotations__: "
             "__conditional_annotations__\nr = g(7)\n", 7),
            ("comprehension target",
             "r = [__conditional_annotations__ for "
             "__conditional_annotations__ in [3]]\n", [3]),
            ("class-local binding",
             "class C:\n    __conditional_annotations__ = 9\n\n"
             "r = C.__conditional_annotations__\n", 9),
        ]
        for label, src, want in clean:
            flagged, (kind, ns) = self._both(src)
            self.assertEqual(kind, "ok", label)
            self.assertEqual(ns["r"], want, label)
            self.assertEqual(flagged, set(), label)  # noqa: VACUOUS_ASSERTION — the banned half above is the must-hit direction, and the ok/r asserts are this source's own positive control; the emptiness is the no-wider-than-the-defect claim


class FacadeAndSiblingsAgreeTest(unittest.TestCase):
    """A `global` REBIND IS INVISIBLE TO THE FANOUT, BY CONSTRUCTION.

    _SeatsFacade intercepts the SETATTR PROTOCOL — an external `seats.X = v`
    or a mock.patch. `global X; X = v` inside a sibling compiles to
    STORE_GLOBAL against that module's own __dict__ and never touches
    __setattr__, so the facade cannot see it and no amount of fanout logic
    could. That is not a bug in the fanout; it is the one thing the mechanism
    structurally cannot cover, which is exactly why it needs a test instead.

    MEASURED after the split: owner_name() returned the derived handle while
    seats._OWNER_NAME was still the None it had been imported with. Four test
    classes use seats._OWNER_NAME as a save/restore handle — they read None,
    set a value (which DID fan out, that path is a real setattr), then
    restored the None, WIPING the cache instead of restoring it. Same syntax
    as before the split, opposite meaning."""

    def test_no_sibling_rebinds_a_module_global(self):
        """THE CLASS, not the instance. One offender existed; the next one
        would be just as silent, and a reviewer cannot see it by reading the
        facade."""
        offenders, checked = {}, 0
        for fn in sorted(os.listdir(HELM)):
            if not fn.startswith("seats_"):
                continue
            checked += 1
            tree = ast.parse(_read(os.path.join(HELM, fn)))
            declared = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.Global):
                    for name in n.names:
                        declared.setdefault(name, n.lineno)
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    if n.id in declared:
                        offenders.setdefault(fn, []).append(
                            "%s:L%d" % (n.id, n.lineno))
        self.assertGreaterEqual(checked, 3, "the sibling scan found almost "
                                            "nothing, so its green is empty")
        self.assertEqual(
            offenders, {},
            "a sibling REBINDS a module global. The facade fanout cannot see "
            "a STORE_GLOBAL, so seats.<name> will silently diverge from the "
            "sibling's live value. Write through the facade instead: "
            "`from . import seats as _f; setattr(_f, \"name\", value)`. "
            "Offenders: %s" % offenders)

    def test_the_owner_name_pin_is_one_cell_not_two(self):
        """The instance the class guard was written for — kept because a
        structural rule can be satisfied while the behaviour is still wrong.

        TRUNK DELETED THE MEMO (its key could not be stated), so the cell
        under test is now the PIN: five test files set seats._OWNER_NAME to
        hold the owner handle still, and after the split the READER of that
        pin lives in seats_identity. If the facade write does not reach the
        defining module, all five patch a value nothing reads — the same
        silent two-cells split, one semantics over.

        This test's previous version was itself a leak (@codex-2): it wrote
        None into both cells and left owner_name()'s side effects behind for
        its neighbours. Both cells are saved and restored DIRECTLY — plain
        module setattr, not through the facade — so the cleanup cannot
        depend on the mechanism under test."""
        from helm import seats_identity
        for cell in (seats, seats_identity):
            saved = cell.__dict__["_OWNER_NAME"]
            self.addCleanup(_types.ModuleType.__setattr__,
                            cell, "_OWNER_NAME", saved)

        seats._OWNER_NAME = "synthpin"       # through the facade door
        self.assertEqual(
            seats.owner_name(), "synthpin",
            "owner_name() ignored the pin set through the facade — the five "
            "test files that pin seats._OWNER_NAME are patching a value "
            "nothing reads")
        self.assertEqual(
            seats_identity._OWNER_NAME, "synthpin",
            "the defining module's cell did not take the facade write — one "
            "pin became two cells")


class FacadeMachineryTest(unittest.TestCase):
    """The fanout's own moving parts, under the same patches it exists to
    serve. Both arms are @codex-2 findings, reproduced before they were
    cured, and both failure modes were silent-by-shape: a restore path that
    misfires leaves no conflict marker and no red test of its own."""

    def test_a_created_name_never_fans_out_to_a_private_sibling(self):  # noqa: VACUOUS_ASSERTION — the __dict__[name] subscript is the unconditional positive control (KeyError if the sibling lost the name), and both arms are identity asserts against that concrete object
        """`mock.patch.object(seats, X, create=True)` where the facade never
        exported X but a sibling privately owns it. The old owners scan asked
        only "who holds this name?", so the patch CLOBBERED the sibling's
        private global and the create-case restore — a delattr — then
        deleted it there outright. A name the facade never held is
        facade-only: no reader resolves it through the facade, so the
        sibling must stay untouched during AND after."""
        from helm import seats_gate_queue
        name = "_GATE_STATES"
        self.assertFalse(name in seats.__dict__,
                         "%s is now exported by the facade — this probe "
                         "needs a sibling-PRIVATE name; pick another" % name)
        before = seats_gate_queue.__dict__[name]
        with mock.patch.object(seats, name, "synthetic-patch", create=True):
            self.assertIs(
                seats_gate_queue.__dict__.get(name), before,
                "the create-case patch reached a sibling global the facade "
                "never owned")
        self.assertIs(
            seats_gate_queue.__dict__.get(name), before,
            "the create-case restore DELETED a sibling's private global")

    def test_restoration_survives_a_patched__types(self):
        """The facade's set/del machinery is itself reachable through the
        facade (`seats._types` is an attribute like any other), and it used
        to resolve `_types.ModuleType.__setattr__` at call time — so one
        patch of _types broke the RESTORE of every other patch on the stack
        (TypeError through the Mock, measured). The machinery is captured at
        def time now; this pins that a patched _types cannot reach it.

        getattr-by-string on purpose: the probe name exists only at runtime,
        and a `seats._machinery_probe` attribute expression would (rightly)
        be counted by the caller scan as a name the facade owes forever."""
        with mock.patch.object(seats, "_machinery_probe", "before",
                               create=True):
            with mock.patch.object(seats, "_types"):
                # the INNER restore runs while _types is still a Mock —
                # the exact shape that used to raise TypeError out of exit
                with mock.patch.object(seats, "_machinery_probe", "during"):
                    self.assertEqual(getattr(seats, "_machinery_probe"),
                                     "during")
                self.assertEqual(
                    getattr(seats, "_machinery_probe"), "before",
                    "the nested restore did not survive a patched "
                    "seats._types")
    def test_a_DIVERGED_sibling_keeps_its_own_direct_patch(self):
        """The fanout skips a sibling that has DIVERGED from the facade —
        two lines whose deletion left every arm in this class green
        (@kimi, measured by deleting them). The suite saw the CLASS and had
        no probe for the INSTANCE, which is the same silent-by-shape hole
        the other two arms exist for.

        The probe must be a name the facade ALREADY exports and a sibling
        already holds. A synthetic one cannot work, and the reason is the
        create-case cure itself: the first facade set of a name the facade
        never held computes `facade_owned=False` and CACHES the empty owner
        set, so no later set can ever make that name shared. Measured, not
        assumed — the first draft of this arm built a synthetic pair and its
        control failed exactly there.

        The in-sync CONTROL is the load-bearing half: "the sibling still
        holds its own value" is equally true of a fanout that reaches
        nothing at all, so the arm first proves the facade's write DOES
        land there."""
        from helm import seats_gate_queue as sib
        name = "GATE_QUEUE_CAPACITY"
        self.assertIn(name, seats.__dict__,
                      "%s is no longer a facade export — this probe needs a "
                      "SHARED name; pick another" % name)
        original = sib.__dict__[name]
        self.assertIs(seats.__dict__[name], original,
                      "setup: facade and sibling must start in sync")

        # CONTROL — in sync, the facade's write MUST reach the sibling.
        # Without this, every assertion below is equally satisfied by a
        # fanout that has stopped working entirely.
        with mock.patch.object(seats, name, 111111):
            self.assertEqual(
                sib.__dict__[name], 111111,
                "control: an IN-SYNC sibling must receive the facade's "
                "write, or divergence below proves nothing")
        self.assertIs(sib.__dict__[name], original,
                      "control: the restore must reach the sibling too")

        # THE FINDING — a live DIRECT patch on the sibling means something
        # else owns that name, and the facade must keep its hands off it
        # for the write AND for the restore.
        with mock.patch.object(sib, name, 222222):
            with mock.patch.object(seats, name, 333333):
                self.assertEqual(
                    sib.__dict__[name], 222222,
                    "the facade CLOBBERED a sibling's live direct patch — "
                    "the divergence guard is unarmed")
            self.assertEqual(
                sib.__dict__[name], 222222,
                "the facade's RESTORE overwrote a sibling's live direct "
                "patch")
        self.assertIs(sib.__dict__[name], original,
                      "the probe must leave the sibling as it found it")


if __name__ == "__main__":
    unittest.main()
