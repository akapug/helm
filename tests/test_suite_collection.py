"""Every test in tests/ must actually be COLLECTED by the gate.

The gate mints its authoritative receipt from literal same-process unittest
discovery (`tests`, `test*.py`, top-level `.`), and unittest collects
`unittest.TestCase` subclasses ONLY. A file
written pytest-style — bare `def test_*(tmp_path)` functions — imports cleanly,
reads as protection, and contributes ZERO assertions to the receipt that gates
landing. Nothing fails. The count just quietly does not include it.

Measured 2026-07-30: `python3 -m unittest tests.test_board` printed `Ran 0
tests`. Fifteen guards over the board's lock-and-atomic-write path — the module
that exists because a hand-rolled json.dump had already lost one update — had
never run in any gate. tests/test_seat_identity_cli.py held five more.

A rule cannot notice this; only a census can. That is the same argument
helm/board.py makes for `helm wiring`, which is what found the board's own
unused write path: the safe path shipped with no way to use it, and no rule
could see it because nothing was wrong with any single line.

THE SECOND ROUTE TO THE SAME NOTHING: SHADOWING. A test can also be skipped by
being DEFINED TWICE. Python binds the later definition and discards the earlier
object, so a duplicated class or method name deletes tests silently — no error,
no warning, and a receipt whose shape does not change. Measured 2026-08-11:
tests/test_landreq.py bound `LandingProofLadderOrderTest` twice with different
bodies, and the first class's two arms had run ZERO times since the day they
were written, because the second definition landed a day later in an unrelated
commit that simply reused a good name.

Both routes end in the same place — a test that reads as protection and
contributes nothing — so both are censused here, from the same file list.

THE CENSUS HAS TO SURVIVE ITS OWN SUBJECT. Found by a review of this file
on 2026-08-11, on reading alone: the only census that touched the real tree
lived inside `ShadowedDefinitionTest`, a module-level class. Duplicate THAT
name and Python discards the first binding — the census stops existing, nothing
errors, and the suite stays green over a tree that has a duplicate sitting in
it. The guard was not immune to the defect it guards against, and the deletion
would have left no trace, which is why it would eventually have happened.

That is now MEASURED rather than read: `CensusSelfImmunityTest` builds the
arrangement and records `unittest discover` exiting 0 over a tree that contains
a duplicate. The cure is that the real-tree verdict is a MODULE-LEVEL statement
executed at import (`_REACH = verify_real_tree()`), guarded in turn by inline
statements that re-check this file's own module scope before any name in it is
trusted. A statement cannot be shadowed. It can only be deleted, and a deletion
is a visible diff, whereas a duplicate reads like someone reusing a good name.

AND THE CENSUS NOW DESCENDS INTO CONTROL FLOW. It used to walk direct children
of a scope only, so a `def` or `class` inside an `if`, `try`, `with` or `for`
was invisible — although such a definition binds in the enclosing scope exactly
like a plain one. The old reason for the limit was real: an `if/else` pair
defines ONE name on two branches and must not be flagged. The limit was the
wrong instrument for it. Nesting depth is not what makes those two definitions
legitimate; MUTUAL EXCLUSIVITY is. So each definition now carries the branch it
sits on, and two definitions clash unless some construct puts them on different
arms — which flags a def in a `try` body against one after the try, and stays
silent on `if/else`, `elif` chains, sibling `except` handlers and `match` cases.

THE THIRD ROUTE IS THE MIRROR IMAGE: A TEST COLLECTED TWICE. `unittest
discover` loads a module by taking every TestCase subclass in `dir(module)`,
and a test's id names the class's DEFINING module. So a module that writes
`from tests.test_x import SomeTest` collects SomeTest's arms again, under the
same ids, and every failure among them is counted twice. Nothing errors, and
the receipt's shape does not change; only its count grows. Measured with a real
discover over the whole tree before the cure: 21,854 collected, 21,814
distinct, 35 ids collected more than once. This census names every binding of
that shape, from the same parse, and it names a binding only when the bound
class CARRIES arms, because a suite that binds a fixture base with no arm
collects nothing extra and the tree does that on purpose in many places. The
day such a base gains an arm, every module binding it goes red here by name.

A SUBCLASS IS NOT A BINDING. `class MineTest(_x.SomeTest)` runs SomeTest's
arms again under MineTest's own ids, which is how a suite re-asks inherited
questions on purpose. The cure for a doubled binding keeps every subclass and
changes only how the base is reached: through its module
(`from tests import test_x as _x`), which shares the one class object without
putting it in `dir()`.
"""

import ast
import os
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SELF = os.path.abspath(__file__)

# Below this, an empty result means the scan broke, not that the tree is clean.
# Absence of a match is only evidence when a hit was possible.
_MIN_FILES = 50

# Set in the child of every subprocess arm below. Those arms run `unittest
# discover` over a synthetic tree that CONTAINS A COPY OF THIS FILE, so without
# a stop bit the copy's own arms would spawn their own children, forever.
_CHILD_ENV = "HELM_SUITE_COLLECTION_CHILD"

# ─── BOOTSTRAP: THIS FILE'S MODULE SCOPE, CHECKED BY INLINE STATEMENTS ───────
# Deliberately duplicated logic rather than a call to the real detector below,
# and deliberately not inside a function or a class. Everything else in this
# file — the detectors, the census, the containers, the arms — is a NAME, and a
# name is exactly what a second definition silently rebinds. These
# statements are not a name. Nothing can shadow them; the only way to remove
# them is to delete them, and a deletion shows up in a diff while a duplicate
# reads like someone reusing a good name. This is the bootstrap the rest of the
# file stands on: it fires BEFORE `verify_real_tree` is called, so a duplicated
# (and therefore hollowed-out) census helper cannot be the thing that decides
# whether the census helper is intact.
#
# Module scope only, on purpose — a duplicated METHOD inside one of the classes
# here is caught by the full census below, which reads this file like any other.
_SELF_DEFS = [n.name for n in ast.parse(open(_SELF, encoding="utf-8").read()).body
              if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
_SELF_DUPES = sorted({n for n in _SELF_DEFS if _SELF_DEFS.count(n) > 1})
if _SELF_DUPES:
    raise RuntimeError(
        "tests/test_suite_collection.py defines %s twice at module scope. This "
        "file IS the shadowing census: Python discards the earlier binding, so "
        "a duplicate here can DELETE THE CENSUS ITSELF with no error and no "
        "warning, leaving the suite green over a tree that has duplicates in "
        "it. Rename one of each pair." % ", ".join(_SELF_DUPES))

_DEFS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_TRY = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_WITHS = (ast.With, ast.AsyncWith)
_MATCH = getattr(ast, "Match", None)


def collectible_test_files(root):
    """Every test module `discover` would collect, INCLUDING in subpackages.

    `unittest discover` DESCENDS into subdirectories, so a flat os.listdir
    census is blind to precisely the files a future refactor would add — it
    would keep reporting a clean tree while a whole subpackage went
    uncollected. There are no test subdirectories today, which is exactly why
    this is worth closing now: the blind spot is invisible until the day
    someone makes one, and on that day the census still says green.

    Found by a review of this file, which proposed documenting the limit.
    A census with a documented blind spot is weaker than one without the blind
    spot, and walking is three lines.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != "__pycache__"]
        out.extend(os.path.join(dirpath, f) for f in filenames
                   if f.startswith("test_") and f.endswith(".py"))
    return sorted(out)


def compound_arms(stmt):
    """The statement lists a compound statement runs IN THE ENCLOSING SCOPE,
    each tagged with the ARM it belongs to (None = no alternative exists).

    A `def` nested in control flow binds in the enclosing scope just like a
    plain one, so the census has to walk in here. What it must NOT do is call
    every nested pair a clash: two arms of one `if` are ALTERNATIVES, and only
    one of them ever executes. So each body is labelled, and the clash test
    downstream is "no construct puts these two on different arms".

      * `if` — body is arm 0, `else` is arm 1. `elif` is a nested `if` inside
        `orelse`, so the chain falls out of the same rule for free.
      * `try` — body is arm 0, each handler its own arm. `else` runs IFF the
        body completed, so it is tagged as the body's arm: it clashes with the
        body (both bind) and not with the handlers (never both). `finally`
        always runs, so it is an alternative to NOTHING — untagged, which makes
        it clash with the body and with every handler.
      * loops and `with` — no alternatives at all; the body just runs.
      * `match` — one arm per case, mutually exclusive like `if`/`else`.
    """
    if isinstance(stmt, ast.If):
        return ((0, stmt.body), (1, stmt.orelse))
    if isinstance(stmt, _TRY):
        return (((0, stmt.body),)
                + tuple((i + 1, h.body) for i, h in enumerate(stmt.handlers))
                + ((0, stmt.orelse), (None, stmt.finalbody)))
    if isinstance(stmt, _LOOPS):
        return ((None, stmt.body), (None, stmt.orelse))
    if isinstance(stmt, _WITHS):
        return ((None, stmt.body),)
    if _MATCH is not None and isinstance(stmt, _MATCH):
        return tuple((i, case.body) for i, case in enumerate(stmt.cases))
    return ()


def scope_definitions(stmts, branch=()):
    """Yield (node, branch-path) for every name bound in THIS scope.

    The branch path is a tuple of (construct, arm) pairs, one per conditional
    the definition is nested inside. Nested `def`/`class` bodies are NOT walked
    — they are their own scopes, and the caller recurses into them separately.
    """
    for stmt in stmts:
        if isinstance(stmt, _DEFS):
            yield stmt, branch
            continue
        for arm, body in compound_arms(stmt):
            yield from scope_definitions(
                body, branch if arm is None else branch + ((id(stmt), arm),))


def exclusive(one, other):
    """True iff some construct puts these two branch paths on DIFFERENT arms —
    i.e. no execution binds both names, so neither definition is discarded."""
    arms = dict(one)
    return any(k in arms and arms[k] != v for k, v in other)


def bare_test_functions(source):
    """Module-level `def test*` names — the ones unittest discover never sees.

    A function nested inside a TestCase is a METHOD and runs fine; only
    module-level ones are invisible. "Module-level" means BINDING AT MODULE
    SCOPE, not "written flush left": a `def test_x` inside a module-level `if`
    or `try` is just as invisible to discover, so this walks compound bodies
    for the same reason the shadowing census does.

    Parsed with ast rather than matched with a regex on purpose: `^def test`
    also matches inside a triple-quoted string, and this file is a census whose
    whole value is that its answer can be trusted.
    """
    return _bare_in_tree(ast.parse(source))


def _bare_in_tree(tree):
    return [n.name for n, _ in scope_definitions(tree.body)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")]


def shadowed_definitions(source):
    """[(scope, name, [lines])] for every name DEFINED TWICE in one scope.

    Two definitions of one name CLASH unless they are mutually exclusive — see
    compound_arms for the branch algebra. That is the whole rule, and it is
    what replaced an older direct-children-only walk that skipped compound
    bodies entirely: the old walk stayed quiet on a def in
    an `if` body shadowing its sibling, which is a real deletion of tests.

    Parsed with ast, never a regex, for the reason the module docstring gives:
    a `class Foo` inside a triple-quoted fixture is not a definition, and this
    file's entire value is that its answer can be trusted.
    """
    return _shadowed_in_tree(ast.parse(source))


def _shadowed_in_tree(tree):
    found = []

    def scope(node, path):
        seen = {}
        for stmt, branch in scope_definitions(node.body):
            seen.setdefault(stmt.name, []).append((stmt.lineno, branch))
            scope(stmt, path + "." + stmt.name)
        for name, defs in seen.items():
            clash = set()
            for i in range(len(defs)):
                for j in range(i + 1, len(defs)):
                    if not exclusive(defs[i][1], defs[j][1]):
                        clash.update((defs[i][0], defs[j][0]))
            if clash:
                found.append((path, name, sorted(clash)))

    scope(tree, "<module>")
    return sorted(found)


# ─── THE THIRD ROUTE: COLLECTED TWICE ───────────────────────────────────────
_TESTCASE_ROOTS = ("TestCase", "IsolatedAsyncioTestCase")

# The must-hit pair: a TestCase with one arm, and a module that binds it by
# name, planted as a package called `tests` beside the real one.
_PLANTED_BASE = ("import unittest\n\n\n"
                 "class PlantedArmTest(unittest.TestCase):\n"
                 "    def test_planted_arm(self):\n"
                 "        self.assertTrue(True)\n")
_PLANTED_BINDER = ("from tests.test_planted_base import PlantedArmTest"
                   "  # noqa: F401\n")


def module_name(path):
    """(dotted name, top dir) that `discover` imports `path` under: the stem,
    prefixed by every enclosing directory that is a package."""
    parts = [os.path.splitext(os.path.basename(path))[0]]
    top = os.path.dirname(os.path.abspath(path))
    while os.path.isfile(os.path.join(top, "__init__.py")):
        parts.insert(0, os.path.basename(top))
        top = os.path.dirname(top)
    return ".".join(parts), top


def scope_statements(stmts):
    """Every statement that runs in THIS scope, compound bodies included and
    nested def/class bodies not. No branch algebra: a binding on any arm may be
    the one that executes, and one execution is enough to collect a class."""
    for stmt in stmts:
        yield stmt
        if not isinstance(stmt, _DEFS):
            for _arm, body in compound_arms(stmt):
                yield from scope_statements(body)


class _TestTree:
    """A static model of the module namespaces `discover` would see.

    Modules are keyed by (top, dotted name), so a planted package beside the
    real one is a separate universe. A module outside the importer's own
    package is EXTERNAL and never parsed: a TestCase is defined under tests/,
    and resolving `from helm.gate import x` would parse helm/ for nothing.
    """

    def __init__(self, trees):
        self.trees = dict(trees)
        self.spaces = {}

    def tree(self, top, name, importer):
        key = (top, name)
        if key not in self.trees:
            package = importer.rpartition(".")[0]
            inside = (name.startswith(package + ".") if package
                      else "." not in name)
            self.trees[key] = None
            base = os.path.join(top, *name.split("."))
            for path in (base + ".py", os.path.join(base, "__init__.py")):
                if inside and os.path.isfile(path):
                    with open(path, encoding="utf-8") as f:
                        self.trees[key] = ast.parse(f.read())
                    break
        return self.trees[key]

    def space(self, top, name, importer):
        """(bindings, star sources) of one module scope, or None if external."""
        key = (top, name)
        if key not in self.spaces:
            tree = self.tree(top, name, importer)
            self.spaces[key] = None if tree is None else self._bindings(
                name, tree)
        return self.spaces[key]

    @staticmethod
    def _bindings(name, tree):
        bound, stars = {}, []
        for stmt in scope_statements(tree.body):
            if isinstance(stmt, ast.ClassDef):
                bound[stmt.name] = ("class", stmt)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound[stmt.name] = ("def",)
            elif isinstance(stmt, ast.ImportFrom):
                source = stmt.module or ""
                if stmt.level:
                    head = name.split(".")[:-stmt.level]
                    source = ".".join(head + ([source] if source else []))
                for alias in stmt.names:
                    if alias.name == "*":
                        stars.append(source)
                    else:
                        bound[alias.asname or alias.name] = (
                            "from", source, alias.name)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    head = alias.asname or alias.name.partition(".")[0]
                    bound[head] = ("module", alias.asname and alias.name
                                   or head)
            elif (isinstance(stmt, ast.Assign)
                  and isinstance(stmt.value, (ast.Name, ast.Attribute))):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        bound[target.id] = ("expr", stmt.value)
        return bound, stars

    def is_module(self, top, name, importer):
        return self.tree(top, name, importer) is not None

    def resolve(self, top, module, name, importer, seen=()):
        """(module, ClassDef) that `name` binds in `module`, or None when it
        binds anything else: a function, a module, an external name."""
        if (module, name) in seen:
            return None
        seen += ((module, name),)
        space = self.space(top, module, importer)
        if space is None:
            return None
        bound, stars = space
        hit = bound.get(name)
        if hit is None:
            for source in stars:
                found = self.resolve(top, source, name, importer, seen)
                if found:
                    return found
            return None
        if hit[0] == "class":
            return module, hit[1]
        if hit[0] == "from":
            if self.is_module(top, "%s.%s" % hit[1:], importer):
                return None
            return self.resolve(top, hit[1], hit[2], importer, seen)
        if hit[0] == "expr":
            return self.resolve_expr(top, module, hit[1], importer, seen)
        return None

    def resolve_expr(self, top, module, node, importer, seen=()):
        if isinstance(node, ast.Name):
            return self.resolve(top, module, node.id, importer, seen)
        if isinstance(node, ast.Attribute):
            owner = self.module_of(top, module, node.value, importer)
            if owner:
                return self.resolve(top, owner, node.attr, importer, seen)
        return None

    def module_of(self, top, module, node, importer):
        """The module a dotted expression names in `module`'s scope, or None."""
        if isinstance(node, ast.Attribute):
            parent = self.module_of(top, module, node.value, importer)
            child = parent and "%s.%s" % (parent, node.attr)
            return child if child and self.is_module(top, child, importer) \
                else None
        if not isinstance(node, ast.Name):
            return None
        space = self.space(top, module, importer)
        hit = space and space[0].get(node.id)
        if hit and hit[0] == "module":
            return hit[1]
        if hit and hit[0] == "from":
            child = "%s.%s" % hit[1:]
            return child if self.is_module(top, child, importer) else None
        return None

    def arms(self, top, module, cls, importer, seen=()):
        """(is a TestCase, {arm names}) through every base defined in the tree.

        An arm is what the loader collects: a `test*` def, or a `test*` name
        bound to a reference, which is how a class re-exports another's arm."""
        testcase = False
        names = {n.name for n in cls.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("test")}
        names.update(t.id for n in cls.body if isinstance(n, ast.Assign)
                     and isinstance(n.value, (ast.Name, ast.Attribute,
                                              ast.Lambda))
                     for t in n.targets
                     if isinstance(t, ast.Name) and t.id.startswith("test"))
        for base in cls.bases:
            hit = self.resolve_expr(top, module, base, importer)
            if hit is None:
                tail = getattr(base, "attr", getattr(base, "id", ""))
                testcase = testcase or tail in _TESTCASE_ROOTS
            elif id(hit[1]) not in seen:
                inherited, more = self.arms(top, hit[0], hit[1], importer,
                                            seen + (id(hit[1]),))
                testcase = testcase or inherited
                names |= more
        return testcase, names


def _doubled_in_trees(trees):
    """({path: [(bound name, 'module.Class', arm count)]}, bindings examined).

    THE MECHANISM. `unittest discover` loads a module by taking every TestCase
    subclass in `dir(module)`, and a test's id is `<class.__module__>.<class>.
    <method>` — the DEFINING module, whatever module it was found in. So a
    module that binds another module's TestCase at module scope collects that
    class's arms a second time under the SAME ids, and every failure among them
    is counted twice. Binding the MODULE (`from tests import test_x as _x`,
    then `_x.Base`) shares the same class object without putting it in `dir()`.

    Only a class that CARRIES arms is a finding. Binding a fixture base with no
    arm collects nothing, and the tree does it in many places on purpose; the
    count of those comes back as the second value, so a resolver that had
    stopped resolving anything reads as a collapse, not as a clean tree.
    """
    tree = _TestTree({module_name(p)[::-1]: t for p, t in trees.items()})
    found, foreign = {}, 0
    for path in trees:
        module, top = module_name(path)
        bound, stars = tree.space(top, module, module)
        names = list(bound)
        for source in stars:
            space = tree.space(top, source, module)
            names += [n for n in (space[0] if space else ())
                      if not n.startswith("_") and n not in bound]
        for name in names:
            hit = tree.resolve(top, module, name, module)
            if hit is None or hit[0] == module:
                continue
            testcase, arms = tree.arms(top, hit[0], hit[1], module)
            if not testcase:
                continue
            foreign += 1
            if arms:
                found.setdefault(path, []).append(
                    (name, "%s.%s" % (hit[0], hit[1].name), len(arms)))
    return found, foreign


def census(paths, root):
    """{'shadow', 'bare', 'definitions', 'doubled', 'foreign'} over a file list.

    ONE parse per file feeding EVERY detector — used by the module-level
    real-tree verdict and by its planted controls alike, so a control exercises
    the code the verdict actually comes from rather than a sibling that
    resembles it.
    """
    shadow, bare, defs, trees = {}, {}, {}, {}
    for path in paths:
        name = os.path.relpath(path, root)
        with open(path, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:      # a file that cannot parse cannot run
            shadow[name] = bare[name] = ["<unparseable: %s>" % exc]
            defs[name] = 0
            continue
        trees[path] = tree
        defs[name] = sum(1 for n in ast.walk(tree) if isinstance(n, _DEFS))
        dupes = _shadowed_in_tree(tree)
        if dupes:
            shadow[name] = dupes
        loose = _bare_in_tree(tree)
        if loose:
            bare[name] = loose
    doubled, foreign = _doubled_in_trees(trees)
    return {"shadow": shadow, "bare": bare, "definitions": defs,
            "doubled": {os.path.relpath(p, root): v
                        for p, v in doubled.items()},
            "foreign": foreign}


def verify_real_tree():
    """THE REAL-TREE VERDICT. Raises on any finding; returns the census reach.

    Called as a MODULE-LEVEL STATEMENT (below), never from a test method, and
    that placement is the fix for a defect this file had against its own
    subject: a check that lives inside `class Foo` is deleted, silently, by a
    second `class Foo`. At module level it runs at import, so a finding is an
    ImportError that `unittest discover` reports as an error — red, loud, and
    unshadowable. The cost is one AST pass over tests/ per suite run (~2s),
    which is LESS than the two passes the two test methods used to make.

    THE MUST-HITS RIDE IN THE SAME PASS as the real census, so the observable
    is never an empty set whose emptiness might mean the pipeline died. Two
    planted files join the real list and the expected answer is NON-EMPTY,
    naming exactly the plants: that one equality says both things at once — the
    census reads files and reports findings, AND not one real file has any.
    A census that read nothing scores identically against `== {}`.

    The plants live in a temp dir, never under TESTS_DIR: a probe that writes
    into the tree it is measuring is how a control becomes damage.
    """
    paths = collectible_test_files(TESTS_DIR)
    # A COUNT IS NOT A MUST-HIT. `len(paths) >= N` still passes on a list of N
    # paths that name nothing real, so the walk is pinned to a file known to
    # exist: the one this function is written in.
    if _SELF not in paths:
        raise RuntimeError(
            "the walk did not return %s, the file it is written in, so it is "
            "not reading this tree and its verdict is about nothing" % _SELF)
    if len(paths) < _MIN_FILES:
        raise RuntimeError(
            "only %d test files found under %s — the scan is broken, and an "
            "empty result from a broken scan reads exactly like a clean tree"
            % (len(paths), TESTS_DIR))

    with tempfile.TemporaryDirectory() as tmp:
        shadow_plant = os.path.join(tmp, "test_planted_shadow.py")
        with open(shadow_plant, "w", encoding="utf-8") as f:
            f.write("class Dup: pass\nclass Dup: pass\n")
        bare_plant = os.path.join(tmp, "test_planted_bare.py")
        with open(bare_plant, "w", encoding="utf-8") as f:
            f.write("def test_i_am_invisible(tmp_path):\n    assert True\n")
        # A PACKAGE named like the real one, so the binder imports its base
        # by the same dotted shape a real satellite uses.
        package = os.path.join(tmp, "tests")
        os.makedirs(package)
        planted = {"__init__.py": "",
                   "test_planted_base.py": _PLANTED_BASE,
                   "test_planted_binder.py": _PLANTED_BINDER}
        for name, source in planted.items():
            with open(os.path.join(package, name), "w", encoding="utf-8") as f:
                f.write(source)
        base_plant = os.path.join(package, "test_planted_base.py")
        binder_plant = os.path.join(package, "test_planted_binder.py")
        result = census(paths + [shadow_plant, bare_plant, base_plant,
                                 binder_plant], TESTS_DIR)
    shadow_rel = os.path.relpath(shadow_plant, TESTS_DIR)
    bare_rel = os.path.relpath(bare_plant, TESTS_DIR)
    base_rel = os.path.relpath(base_plant, TESTS_DIR)
    binder_rel = os.path.relpath(binder_plant, TESTS_DIR)

    if result["shadow"] != {shadow_rel: [("<module>", "Dup", [1, 2])]}:
        raise RuntimeError(
            "%r\n\nEither the census cannot see a PLANTED duplicate, or a real "
            "test file has a name defined twice in one scope. A duplicate makes "
            "Python discard the earlier definition at import, so every test "
            "inside it runs ZERO times — no error, no warning, and no change in "
            "the receipt's shape. Rename one of each pair."
            % (result["shadow"],))
    if result["bare"] != {bare_rel: ["test_i_am_invisible"]}:
        raise RuntimeError(
            "%r\n\nEither the census cannot see a PLANTED bare function, or a "
            "real file has module-level test functions — `unittest discover` "
            "never collects those, and the gate receipt counts them as zero. "
            "Make each one a method on a unittest.TestCase subclass."
            % (result["bare"],))
    if result["doubled"] != {binder_rel: [
            ("PlantedArmTest", "tests.test_planted_base.PlantedArmTest", 1)]}:
        raise RuntimeError(
            "%r\n\nEither the census cannot see a PLANTED binding, or a real "
            "test file binds another module's TestCase that carries arms. "
            "`unittest discover` collects every TestCase in `dir(module)` "
            "under the id of the module that DEFINES it, so each arm listed "
            "is collected a second time under the same id and every failure "
            "among them is counted twice. Bind the module instead "
            "(`from tests import test_x as _x`, then `_x.Base`), or move the "
            "arm off a base that other suites subclass for its fixture."
            % (result["doubled"],))

    counts = result["definitions"]
    return {
        "root": TESTS_DIR,
        "files": len(paths),
        "definitions": (sum(counts.values())
                        - counts[shadow_rel] - counts[bare_rel]
                        - counts[base_rel] - counts[binder_rel]),
        # Fixture bases bound by name from another module, which collect
        # nothing. Nonzero is the resolver's must-hit on real input.
        "foreign_bases": result["foreign"] - 1,
    }


# ─── THE REAL-TREE VERDICT, AT MODULE LEVEL, ON PURPOSE ─────────────────────
# REACH, measured 2026-08-12: 241 files under tests/, 16,268 definitions, 0
# offenders, ~3s of the suite's wall clock. The traversal was reconciled
# against helm's existing authority on test identities — vacuous_assertion's
# `_test_functions` — over all 241 files: 10,654 identities each, zero
# disagreements, which is what a second parser over one tree is for.
# WHAT IT STILL CANNOT SEE — stated here, in the code, because a blind spot
# that lives only in a report is a blind spot nobody will meet again:
#
#   * helm/ ITSELF. Only tests/ is censused. Measured before deciding: the
#     compound-descent census over helm/ (219 files, 4,544 definitions) returns
#     exactly one hit — helm/pi.py binds a local `opt` three times inside
#     cmd_pi, each in a branch that returns before the next reaches it — so
#     extending this to source today would open with a false positive. The
#     machinery takes any file list; widening the root is not this lane's call.
#   * TWO SEPARATE `if` STATEMENTS with complementary conditions (`if X: def f`
#     … later … `if not X: def f`) have no common construct, so they read as
#     unconditional and ARE flagged. Deciding that pair needs the conditions'
#     semantics, which is undecidable in general. Write it as `if/else`.
#   * A def in a `try` BODY vs one in its HANDLER is called an alternative,
#     because `try: from fast import loads / except ImportError: def loads` is
#     the idiom this file must not break. If the body's def is followed by a
#     raise, the handler's def really does discard it — invisible here.
#   * RUNTIME rebinding: `Foo = Bar`, `setattr`, a decorator that returns a
#     different object, `globals()[name] = …`. This is a census of DEFINITIONS.
#   * A name defined in two different FILES, which is not shadowing at all, and
#     a name imported over a definition (`def f` then `from x import f`).
#   * Tests deleted by any route other than these two — skipped, mis-named,
#     asserting nothing. tests/test_vacuous_assertion.py covers that third one.
#   * COLLECTED TWICE is a STATIC model of `dir(module)`, so it cannot follow a
#     binding made at runtime (`setattr`, `globals()`, a `load_tests`
#     protocol, `sys.modules`), it reads an import under `if TYPE_CHECKING:`
#     as bound, it counts an inherited arm that a subclass switched off with
#     `test_x = None`, and it never reads a module outside the importer's own
#     package, so a TestCase defined under helm/ is invisible to it. It was
#     reconciled against a real discover over the uncured tree: the same 35
#     doubled ids, the same 40 extra collections.
#
# `verify_real_tree` is a NAME and could in principle be rebound by a hollow
# duplicate; the inline bootstrap at the top of this file fires first and
# refuses exactly that, which is why the bootstrap does not call anything.
_REACH = verify_real_tree()


def stub_out_class(source, name):
    """`source` as it would BEHAVE once `class <name>` has been shadowed.

    Shadowing does not edit a file — Python evaluates both definitions and
    throws the first object away — so the faithful model of the post-shadow
    state is the ORIGINAL BODY GONE, with the name bound to something else.
    Appending a literal second definition would model it too, but it would also
    trip the module-scope bootstrap and mask which defence actually fired; this
    isolates the one under test.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            lines = source.splitlines(True)
            return ("".join(lines[:node.lineno - 1])
                    + "class %s(unittest.TestCase):\n    pass\n" % name
                    + "".join(lines[node.end_lineno:]))
    raise AssertionError(
        "no top-level class %r to stub out — the arm using this is measuring "
        "nothing at all" % name)


def synthetic_tests_dir(tmp, files):
    """A tests/ dir standing in for the real one: `files`, plus enough filler
    to clear _MIN_FILES — which any copy of this module reads from its own
    source and would otherwise refuse the tree over, a red for the wrong
    reason — plus ONE ARM THAT PASSES.

    That last file is not decoration. Since 3.12 `unittest` exits 5 when no
    test ran, so a tree of nothing but filler is NON-ZERO, and an arm asserting
    "this arrangement stays green" could never tell a surviving check from a
    suite that collected nothing at all. One passing arm makes rc 0 mean tests
    ran and were happy.
    """
    for i in range(_MIN_FILES + 5):
        with open(os.path.join(tmp, "test_filler_%03d.py" % i), "w",
                  encoding="utf-8") as f:
            f.write("")
    with open(os.path.join(tmp, "test_baseline_ok.py"), "w",
              encoding="utf-8") as f:
        f.write("import unittest\n\n\nclass BaselineTest(unittest.TestCase):\n"
                "    def test_the_runner_collected_something(self):\n"
                "        self.assertTrue(True)\n")
    for name, source in files.items():
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(source)
    return tmp


def discover(tmp, *flags):
    """(rc, output) from `unittest discover` over a synthetic tree — the exact
    observable the gate reads. A claim about what the SUITE does is worth
    having only if a suite produced it."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", *flags,
         "-s", tmp, "-t", tmp],
        cwd=tmp, env=dict(os.environ, **{_CHILD_ENV: "1"}),
        capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


# The shape this file HAD before 2026-08-12, reduced to its load-bearing part:
# one census, reading the real tree, living inside a TestCase method.
_CENSUS_IN_A_CLASS = '''\
"""Model of the pre-cure shape: the ONLY real-tree census is inside a class."""
import ast
import os
import unittest


def dupes(source):
    seen = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            seen.setdefault(node.name, []).append(node.lineno)
    return sorted(name for name, lines in seen.items() if len(lines) > 1)


class CensusTest(unittest.TestCase):
    def test_no_test_file_has_a_duplicate_definition(self):
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = {}
        for name in sorted(os.listdir(here)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            if name == os.path.basename(__file__):
                continue
            with open(os.path.join(here, name), encoding="utf-8") as f:
                found = dupes(f.read())
            if found:
                offenders[name] = found
        self.assertEqual(offenders, {})
'''

_CENSUS_CONTAINER_DUPLICATED = '''

class CensusTest(unittest.TestCase):
    """A second binding of a good name, in an unrelated commit. Nothing about
    this reads as a deletion, and the census above is now unreachable."""
'''


class CensusSelfImmunityTest(unittest.TestCase):
    """The guard measured against ITS OWN defect.

    A review of this file reported that the sole
    real-tree census sat inside a class that shadowing could delete, so the
    exact defect could stay green. The basis was INFERRED — nothing was run.
    These arms execute it.
    """

    def setUp(self):
        if os.environ.get(_CHILD_ENV):
            self.skipTest("child of a self-immunity arm: no grandchildren")

    def test_a_census_inside_a_class_is_silently_deleted_by_a_duplicate(self):
        """THE EXACT-DEFECT-STAYS-GREEN CASE, executed rather than reasoned.

        Two runs over a tree that HAS a duplicate-definition file in it. The
        pair is the measurement: identical trees, identical census logic, one
        extra class definition — and the suite's verdict flips from red to
        GREEN. That is what "a guard that can be deleted silently" means, and
        it is why the real verdict in this file is a module-level statement.
        """
        plant = {"test_shadowed_plant.py": "class Dup: pass\nclass Dup: pass\n"}

        with tempfile.TemporaryDirectory() as tmp:
            synthetic_tests_dir(
                tmp, dict(plant, **{"test_shape.py": _CENSUS_IN_A_CLASS}))
            intact_rc, intact_out = discover(tmp)
        self.assertNotEqual(
            intact_rc, 0,
            "POSITIVE CONTROL: with its container intact, the modelled census "
            "must FIND the planted duplicate. If this is green the arm below "
            "proves nothing — it would be measuring a census that never "
            "worked. Output:\n%s" % intact_out)
        self.assertIn("test_shadowed_plant.py", intact_out)

        with tempfile.TemporaryDirectory() as tmp:
            synthetic_tests_dir(tmp, dict(plant, **{
                "test_shape.py": (_CENSUS_IN_A_CLASS
                                  + _CENSUS_CONTAINER_DUPLICATED)}))
            shadowed_rc, shadowed_out = discover(tmp)
        self.assertEqual(
            shadowed_rc, 0,
            "THE DEFECT: duplicating the census's container must leave the "
            "suite GREEN over a tree that still contains the plant — that is "
            "the failure this file's module-level verdict exists to make "
            "impossible. A red here means Python stopped discarding the "
            "earlier binding, and the whole premise needs re-reading. "
            "Output:\n%s" % shadowed_out)
        # POSITIVE CONTROL ON THE ABSENCE ASSERTION BELOW. "The plant is not
        # named" is satisfied by an empty string, a crashed runner, or a tree
        # that collected nothing — so first pin what the run DID do. Exactly
        # one arm survives in that tree (the baseline), and it passed: the
        # suite is green because the census is GONE, not because nothing ran.
        self.assertIn("Ran 1 test", shadowed_out, shadowed_out)
        self.assertIn("OK", shadowed_out, shadowed_out)
        self.assertNotIn("test_shadowed_plant.py", shadowed_out,
                         "green AND silent: the check did not merely pass, it "
                         "no longer exists")

    def test_this_files_census_survives_its_own_container_being_shadowed(self):
        """THE SELF-IMMUNITY ARM, on the real file rather than a model.

        THIS file's source, with `ShadowedDefinitionTest` reduced to the empty
        class a second definition would leave behind, run over a tree that has
        a duplicate in it. Before 2026-08-12 that stubbing deleted the only
        real-tree census and the child exited 0. The verdict is a module-level
        statement now, so it still fires and the child must be RED — and red
        NAMING THE PLANT, not red for some unrelated reason.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with open(_SELF, encoding="utf-8") as f:
                source = f.read()
            synthetic_tests_dir(tmp, {
                "test_shadowed_plant.py": "class Dup: pass\nclass Dup: pass\n",
                "test_suite_collection.py": stub_out_class(
                    source, "ShadowedDefinitionTest"),
            })
            rc, out = discover(tmp)
        self.assertNotEqual(
            rc, 0,
            "deleting the class that USED to hold the census left the suite "
            "green over a tree containing a duplicate — the census is back "
            "inside something shadowable. Output:\n%s" % out)
        self.assertIn(
            "test_shadowed_plant.py", out,
            "the child is red, but not about the plant, so this arm is not "
            "measuring the census. Output:\n%s" % out)

    def test_a_duplicate_of_a_container_is_itself_reported(self):
        """The bootstrap, measured: a literal second definition of a class in
        THIS file is refused at import even when the tree is otherwise clean.
        The arm above proves the census survives being shadowed; this one
        proves the shadowing does not even get to be quiet about it."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(_SELF, encoding="utf-8") as f:
                source = f.read()
            synthetic_tests_dir(tmp, {
                "test_suite_collection.py": (
                    source + "\n\nclass ShadowedDefinitionTest(unittest."
                             "TestCase):\n    pass\n"),
            })
            rc, out = discover(tmp)
        self.assertNotEqual(rc, 0,
                            "a duplicated container passed unnoticed over an "
                            "otherwise clean tree. Output:\n%s" % out)
        self.assertIn("ShadowedDefinitionTest", out,
                      "red, but it does not name the duplicate it found, so "
                      "nobody reading it will know what to rename. "
                      "Output:\n%s" % out)


class ShadowedDefinitionTest(unittest.TestCase):
    def test_the_detector_finds_a_planted_shadowed_class_and_method(self):
        """MUST-HIT control. A detector that returned [] for everything would
        make the census pass while proving nothing — the same failure one
        level up that this whole file exists to name."""
        planted = (
            "import unittest\n"
            "class Dup(unittest.TestCase):\n"
            "    def test_a(self): pass\n"
            "class Dup(unittest.TestCase):\n"
            "    def test_b(self): pass\n"
            "class Ok(unittest.TestCase):\n"
            "    def test_c(self): pass\n"
            "    def test_c(self): pass\n"
        )
        self.assertEqual(
            shadowed_definitions(planted),
            [("<module>", "Dup", [2, 4]), ("<module>.Ok", "test_c", [7, 8])],
            "both routes must be caught: a shadowed CLASS drops a whole file's "
            "worth of arms, a shadowed METHOD drops one")

    def test_a_definition_in_a_compound_body_shadows_and_is_caught(self):
        """The census walked DIRECT CHILDREN of a scope, so
        a def or class inside an `if`, `try`, `with` or `for` was invisible —
        and such a definition binds in the enclosing scope exactly like a plain
        one, so it deletes its sibling just as silently."""
        # UNCONDITIONAL POSITIVE CONTROL, outside the loop on purpose: every
        # assertion below is inside `for`, so an empty or mistyped `cases`
        # would make this arm pass while measuring nothing at all — the same
        # shape of nothing the whole file is about.
        self.assertEqual(
            shadowed_definitions("if FLAG:\n    def f(): pass\ndef f(): pass\n"),
            [("<module>", "f", [2, 3])])
        cases = {
            "if-body vs sibling": (
                "if FLAG:\n    def f(): pass\ndef f(): pass\n",
                [("<module>", "f", [2, 3])]),
            "try-body vs sibling after the try": (
                "try:\n    def f(): pass\nexcept Exception:\n    pass\n"
                "def f(): pass\n",
                [("<module>", "f", [2, 5])]),
            "with-body vs sibling": (
                "with ctx:\n    def f(): pass\ndef f(): pass\n",
                [("<module>", "f", [2, 3])]),
            "for-body vs sibling": (
                "for i in xs:\n    def f(): pass\ndef f(): pass\n",
                [("<module>", "f", [2, 3])]),
            "twice in the SAME branch": (
                "if FLAG:\n    def f(): pass\n    def f(): pass\n",
                [("<module>", "f", [2, 3])]),
            "a CLASS in an if-body, the lane's own defect nested": (
                "import unittest\nif FLAG:\n"
                "    class Dup(unittest.TestCase): pass\n"
                "class Dup(unittest.TestCase): pass\n",
                [("<module>", "Dup", [3, 4])]),
            "try-body vs its own else, which runs when the body completed": (
                "try:\n    def f(): pass\nexcept Exception:\n    pass\n"
                "else:\n    def f(): pass\n",
                [("<module>", "f", [2, 6])]),
            "handler vs finally, which always runs": (
                "try:\n    pass\nexcept Exception:\n    def f(): pass\n"
                "finally:\n    def f(): pass\n",
                [("<module>", "f", [4, 6])]),
            "inside a METHOD, not only at module scope": (
                "class T:\n    def test_x(self):\n        if FLAG:\n"
                "            def g(): pass\n        def g(): pass\n",
                [("<module>.T.test_x", "g", [4, 5])]),
        }
        for label, (source, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(shadowed_definitions(source), expected)

    def test_mutually_exclusive_branches_are_alternatives_not_shadowing(self):
        """FALSE-POSITIVE control, and the reason the census reasons about
        BRANCHES rather than nesting depth. Exactly one definition in each of
        these binds per execution, so nothing is discarded — and a census that
        cries wolf on `if/else` gets disabled, which is worse than one that
        misses a try-body."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: lift the very same two defs
        # to the top level and they ARE shadowing. Without this, a detector
        # that had stopped seeing functions entirely would satisfy every
        # assertion below, and the arm would be proving branch exclusivity
        # while actually measuring a broken parse.
        self.assertEqual(shadowed_definitions("def f(): pass\ndef f(): pass\n"),
                         [("<module>", "f", [1, 2])])
        cases = {
            "if/else": "if FLAG:\n    def f(): pass\nelse:\n    def f(): pass\n",
            "elif chain": ("if A:\n    def f(): pass\nelif B:\n"
                           "    def f(): pass\nelse:\n    def f(): pass\n"),
            "try-body vs handler, the import-fallback idiom": (
                "try:\n    def f(): pass\nexcept ImportError:\n"
                "    def f(): pass\n"),
            "two sibling handlers": (
                "try:\n    pass\nexcept A:\n    def f(): pass\n"
                "except B:\n    def f(): pass\n"),
            "handler vs else, which never both run": (
                "try:\n    pass\nexcept A:\n    def f(): pass\n"
                "else:\n    def f(): pass\n"),
            "a CLASS on two branches": (
                "import unittest\nif FLAG:\n"
                "    class T(unittest.TestCase): pass\nelse:\n"
                "    class T(unittest.TestCase): pass\n"),
        }
        if _MATCH is not None:
            cases["match cases"] = ("match v:\n    case 1:\n"
                                    "        def f(): pass\n    case _:\n"
                                    "        def f(): pass\n")
        for label, source in cases.items():
            with self.subTest(label):
                self.assertEqual(shadowed_definitions(source), [])

    def test_a_docstring_that_looks_like_two_classes_is_not_counted(self):
        """tests/test_vacuous_assertion.py holds 41 `class T` fixtures inside
        string literals; a regex census would call that file catastrophic."""
        body = "class Dup: pass\nclass Dup: pass\n"
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the identical bytes, not
        # wrapped in quotes, ARE two definitions. The pair is the measurement —
        # one text, two meanings, and only the parser can tell them apart.
        self.assertEqual(shadowed_definitions(body),
                         [("<module>", "Dup", [1, 2])])
        decoy = '"""Example:\n\n' + body + '"""\n'
        self.assertEqual(shadowed_definitions(decoy), [])

    def test_the_census_reads_a_file_list_and_reports_per_file(self):
        """The census function itself, on a planted pair — the same call the
        module-level verdict makes, so this arm and that verdict cannot drift
        apart into a detector that works and a census that drops findings."""
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (("test_dup.py", "class D: pass\nclass D: pass\n"),
                               ("test_bare.py", "def test_x(): pass\n"),
                               ("test_clean.py", "class C:\n    def test_y(self): pass\n")):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(body)
            result = census(collectible_test_files(tmp), tmp)
        self.assertEqual(result["shadow"],
                         {"test_dup.py": [("<module>", "D", [1, 2])]})
        self.assertEqual(result["bare"], {"test_bare.py": ["test_x"]})
        self.assertEqual(result["definitions"],
                         {"test_dup.py": 2, "test_bare.py": 1, "test_clean.py": 2})


class SuiteCollectionTest(unittest.TestCase):
    def test_the_detector_finds_a_planted_bare_function(self):
        """MUST-HIT control. Without this, a detector that silently returned []
        for everything would make the census pass while proving nothing —
        which is the exact failure mode the census exists to catch, one level
        up."""
        planted = (
            "import unittest\n"
            "def test_i_am_invisible(tmp_path):\n"
            "    assert True\n"
            "class RealTest(unittest.TestCase):\n"
            "    def test_i_do_run(self):\n"
            "        pass\n"
        )
        found = bare_test_functions(planted)
        self.assertEqual(found, ["test_i_am_invisible"])

    def test_a_bare_function_nested_in_control_flow_is_still_invisible(self):
        """It binds at module scope wherever it is WRITTEN, so `discover` skips
        it the same way — the compound-body blind spot cost this census the
        same coverage it cost the shadowing one."""
        self.assertEqual(
            bare_test_functions("if FLAG:\n    def test_hidden(): pass\n"),
            ["test_hidden"])
        self.assertEqual(
            bare_test_functions("try:\n    def test_hidden(): pass\n"
                                "except Exception:\n    pass\n"),
            ["test_hidden"])
        # A def inside a TestCase METHOD is a local, not a module binding, and
        # must stay unflagged: the walk stops at nested scopes.
        self.assertEqual(
            bare_test_functions("import unittest\n"
                                "class T(unittest.TestCase):\n"
                                "    def test_real(self):\n"
                                "        def test_local(): pass\n"),
            [])

    def test_a_docstring_that_looks_like_a_def_is_not_counted(self):
        """The regex version of this census would report this file as broken."""
        body = "def test_not_real(tmp_path): ...\n"
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the identical bytes, not
        # wrapped in quotes, ARE a bare test function. The pair is the
        # measurement — one text, two meanings, and only a parser separates
        # them. Alone, the empty assertion below is also satisfied by a
        # detector that had stopped finding anything at all.
        self.assertEqual(bare_test_functions(body), ["test_not_real"])
        decoy = '"""Example:\n\n' + body + '"""\n'
        self.assertEqual(bare_test_functions(decoy), [])

    def test_the_walk_reaches_a_subpackage(self):
        """MUST-HIT control for the RECURSION, separate from the one for the
        detector. A flat listdir passes every other test in this file — the
        only thing that can catch it is a file that exists one level down."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub", "__pycache__"))
            for rel in (("test_top.py",), ("sub", "test_nested.py"),
                        ("sub", "__pycache__", "test_stale.py"),
                        ("sub", "helper.py")):
                with open(os.path.join(tmp, *rel), "w", encoding="utf-8") as f:
                    f.write("")
            found = [os.path.relpath(p, tmp) for p in collectible_test_files(tmp)]
        self.assertEqual(found, [os.path.join("sub", "test_nested.py"),
                                 "test_top.py"],
                         "the walk must reach a subpackage, skip __pycache__, "
                         "and ignore non-test files")

    def test_the_real_tree_verdict_ran_and_reports_its_reach(self):
        """The verdict itself is NOT this arm — it already ran, at import, and
        a finding would have made this module fail to load rather than fail a
        test. This reports what it covered, so the reach is a number somebody
        can read in a receipt instead of a claim in a commit message."""
        if os.environ.get(_CHILD_ENV):
            # A copy of this file run over a synthetic tree, where the reach
            # is small by construction; the arm that needs the copy GREEN
            # asserts the copy's verdict passed, which is all a child can say.
            self.skipTest("a synthetic tree's reach is not the real tree's")
        self.assertEqual(_REACH["root"], TESTS_DIR)
        self.assertGreaterEqual(_REACH["files"], _MIN_FILES)
        self.assertGreater(
            _REACH["definitions"], 1000,
            "the census reports %r definitions over %r files; a collapse to a "
            "handful means it is parsing something other than this tree"
            % (_REACH["definitions"], _REACH["files"]))
        # The collected-twice census found nothing in the real tree, and that
        # empty answer means something only if its resolver reads this tree's
        # imports at all: the tree binds fixture bases across modules on
        # purpose, and each one it resolved is counted here.
        self.assertGreater(
            _REACH["foreign_bases"], 0,
            "the collected-twice census resolved no fixture base bound across "
            "modules, which this tree does in many places, so its clean answer "
            "is about nothing")


def planted_census(files):
    """(doubled, foreign) from census() over a throwaway package `tests`."""
    with tempfile.TemporaryDirectory() as tmp:
        package = os.path.join(tmp, "tests")
        os.makedirs(package)
        for name, source in dict(files, **{"__init__.py": ""}).items():
            with open(os.path.join(package, name), "w", encoding="utf-8") as f:
                f.write(source)
        result = census(collectible_test_files(package), package)
    return result["doubled"], result["foreign"]


_BOUND = [("PlantedArmTest", "tests.test_planted_base.PlantedArmTest", 1)]


class CollectedTwiceTest(unittest.TestCase):
    """The third route, on planted trees: what binds a collectable class by
    name, and what reaches the same class without collecting it again."""

    def test_the_detector_finds_a_planted_binding(self):
        """MUST-HIT control, the pair the real-tree verdict plants too."""
        doubled, foreign = planted_census({
            "test_planted_base.py": _PLANTED_BASE,
            "test_planted_binder.py": _PLANTED_BINDER})
        self.assertEqual(doubled, {"test_planted_binder.py": _BOUND})
        self.assertEqual(foreign, 1)

    def test_every_spelling_of_a_module_scope_binding_is_found(self):
        """A regex over `from X import Y` cannot read an alias, a relative
        import, a star, an assignment or a relay; each binds the class in
        `dir(module)` all the same."""
        # UNCONDITIONAL POSITIVE CONTROL, outside the loop: every assertion
        # below is inside `for`, so an empty `cases` would measure nothing.
        self.assertEqual(planted_census({
            "test_planted_base.py": _PLANTED_BASE,
            "test_planted_binder.py": _PLANTED_BINDER})[0],
            {"test_planted_binder.py": _BOUND})
        target = "tests.test_planted_base.PlantedArmTest"
        cases = {
            "an alias": (
                "from tests.test_planted_base import PlantedArmTest as Fix\n",
                [("Fix", target, 1)]),
            "a relative import": (
                "from .test_planted_base import PlantedArmTest\n", _BOUND),
            "a star import": (
                "from tests.test_planted_base import *\n", _BOUND),
            "an assignment through the module": (
                "from tests import test_planted_base as b\n"
                "Alias = b.PlantedArmTest\n",
                [("Alias", target, 1)]),
            "an import inside a module-scope try": (
                "try:\n"
                "    from tests.test_planted_base import PlantedArmTest\n"
                "except ImportError:\n    pass\n",
                _BOUND),
        }
        for label, (source, expected) in cases.items():
            with self.subTest(label):
                doubled, _ = planted_census({
                    "test_planted_base.py": _PLANTED_BASE,
                    "test_planted_binder.py": source})
                self.assertEqual(doubled,
                                 {"test_planted_binder.py": expected})

    def test_a_relay_doubles_at_every_module_it_passes_through(self):
        """A class re-exported by a second test module is bound in both."""
        doubled, _ = planted_census({
            "test_planted_base.py": _PLANTED_BASE,
            "test_planted_relay.py": _PLANTED_BINDER,
            "test_planted_binder.py":
                "from tests.test_planted_relay import PlantedArmTest\n"})
        self.assertEqual(doubled, {"test_planted_relay.py": _BOUND,
                                   "test_planted_binder.py": _BOUND})

    def test_an_arm_inherited_from_a_third_module_is_counted(self):
        """The binder's class has no arm of its own; its base, two modules
        away, has two. The arms are what gets collected twice, so the count
        follows the bases across files."""
        doubled, _ = planted_census({
            "test_planted_root.py": (
                "import unittest\n\n\nclass RootTest(unittest.TestCase):\n"
                "    def test_one(self):\n        self.assertTrue(True)\n\n"
                "    def test_two(self):\n        self.assertTrue(True)\n"),
            "test_planted_mid.py": (
                "from tests import test_planted_root as r\n\n\n"
                "class MidBase(r.RootTest):\n    pass\n"),
            "test_planted_binder.py":
                "from tests.test_planted_mid import MidBase\n"})
        self.assertEqual(doubled, {"test_planted_binder.py": [
            ("MidBase", "tests.test_planted_mid.MidBase", 2)]})

    def test_what_collects_nothing_again_is_not_a_finding(self):
        """FALSE-POSITIVE controls. Each of these reaches or names the class
        and none puts an arm-carrying TestCase into `dir()` of a second
        module. A census that cried wolf on a fixture base would be read as
        noise within a week; the tree binds those by name on purpose."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the same base, bound by
        # name, IS a finding. Without it every `{}` below is also what a
        # detector that had stopped finding anything would return.
        self.assertEqual(planted_census({
            "test_planted_base.py": _PLANTED_BASE,
            "test_planted_binder.py": _PLANTED_BINDER})[0],
            {"test_planted_binder.py": _BOUND})
        fixture = ("import unittest\n\n\n"
                   "class PlantedArmTest(unittest.TestCase):\n"
                   "    def setUp(self):\n        self.ready = True\n")
        cases = {
            "the module, then a subclass through it": (_PLANTED_BASE, (
                "from tests import test_planted_base as b\n\n\n"
                "class MineTest(b.PlantedArmTest):\n    pass\n"), 0),
            "a fixture base with no arm, bound by name": (
                fixture, _PLANTED_BINDER, 1),
            "an import inside a method, which binds a local": (
                _PLANTED_BASE, (
                    "import unittest\n\n\nclass UseTest(unittest.TestCase):\n"
                    "    def test_use(self):\n"
                    "        from tests.test_planted_base import "
                    "PlantedArmTest\n"
                    "        self.assertTrue(PlantedArmTest)\n"), 0),
            "a plain class whose methods merely start with test": (
                "class PlantedArmTest:\n    def test_like(self):\n"
                "        return 1\n", _PLANTED_BINDER, 0),
        }
        for label, (base, binder, foreign) in cases.items():
            with self.subTest(label):
                self.assertEqual(planted_census({
                    "test_planted_base.py": base,
                    "test_planted_binder.py": binder}), ({}, foreign))


class CollectedTwiceByDiscoverTest(unittest.TestCase):
    """The mechanism and the verdict, measured with a real `unittest discover`
    rather than asserted from the static model."""

    _FLAT_BINDER = "from test_planted_base import PlantedArmTest  # noqa\n"
    _FLAT_MODULE = "import test_planted_base  # noqa: F401\n"

    def setUp(self):
        if os.environ.get(_CHILD_ENV):
            self.skipTest("child of a discover arm: no grandchildren")

    def test_discover_runs_a_bound_arm_twice_under_one_id(self):
        """The pair is the measurement: one tree binds the class by name, the
        other binds its module, and only the count and the id list differ."""
        # The class part of the id, which every Python spells the same in a
        # verbose line; the method part moved inside it in 3.11.
        arm_id = "test_planted_base.PlantedArmTest"
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_tests_dir(tmp, {
                "test_planted_base.py": _PLANTED_BASE,
                "test_planted_binder.py": self._FLAT_BINDER})
            rc, bound = discover(tmp, "-v")
        self.assertEqual(rc, 0, bound)
        self.assertIn("Ran 3 tests", bound)
        self.assertEqual(bound.count(arm_id), 2, bound)
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_tests_dir(tmp, {
                "test_planted_base.py": _PLANTED_BASE,
                "test_planted_binder.py": self._FLAT_MODULE})
            rc, reached = discover(tmp, "-v")
        self.assertEqual(rc, 0, reached)
        self.assertIn("Ran 2 tests", reached)
        self.assertEqual(reached.count(arm_id), 1, reached)

    def test_this_files_census_is_red_on_a_binding_and_green_without(self):
        """THIS file, copied into a synthetic tree beside the planted pair: the
        import-time verdict must refuse the tree and name the binder, and the
        same tree with the module bound instead must pass."""
        with open(_SELF, encoding="utf-8") as f:
            source = f.read()
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_tests_dir(tmp, {
                "test_planted_base.py": _PLANTED_BASE,
                "test_planted_binder.py": self._FLAT_BINDER,
                "test_suite_collection.py": source})
            rc, red = discover(tmp)
        self.assertNotEqual(rc, 0, red)
        self.assertIn("test_planted_binder.py", red, red)
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_tests_dir(tmp, {
                "test_planted_base.py": _PLANTED_BASE,
                "test_planted_binder.py": self._FLAT_MODULE,
                "test_suite_collection.py": source})
            rc, green = discover(tmp)
        self.assertEqual(rc, 0, green)
        self.assertIn("OK", green, green)
        self.assertNotIn("test_planted_binder.py", green, green)


if __name__ == "__main__":
    unittest.main()
