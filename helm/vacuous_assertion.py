#!/usr/bin/env python3
"""Warn when a staged Python test can pass without proving its observable.

Three green tests cost three review rounds on 2026-07-31 for one reason: their
assertions were satisfiable by an empty result, or the assertion that could say
otherwise sat behind a guard that never ran. This is the mechanical rung for
that class. It reads the staged blob, parses the whole module with ``ast``, and
only reports collected test functions touched by the staged diff. Prose cannot
fire it.

A positive control must constrain the SAME observable as the absence assertion.
``assertEqual(calls, [1])`` proves the spy ran; it does not prove ``rows`` was
non-empty. A test that asserts only a local spy list while discarding the
production call's result is therefore still warned. Assertions under an
if/loop/try/comprehension are guarded and cannot clear the warning because
their execution is optional.

"Same observable" follows straight-line PROVENANCE, not spelling. In
``out, why = mark_verdict(...)`` the two names are channels of ONE result, so
``assertIn("required", why)`` is a positive control for ``assertIsNone(out)``
— refusing to see that trained 31 real guard tests toward ``# noqa`` in a
single night. Every call in a binding RHS mints a FRESH producer identity:
two calls never share merely because their callee or arguments have the same
spelling (``got = load(); other = load()`` are two observables), a receiver
chain flows (``f.read()`` is ``f``'s data; ``landreq.get()`` carries landreq
lineage), rebinding REPLACES provenance, and a ``with ... as`` target is a
view of its whole expression. Element-wise packs (``a, b = x, y``) stay
separate. ``rc == 0`` against a bare name is an exact scalar contract —
NEUTRAL, neither positive credit nor an absence demanding cover — while
``len(rows) == 0`` keeps absence semantics.

WARN, NEVER BLOCK. Absence is often exactly the contract under test, so a
pattern detector cannot refuse the commit. An intentional negative test names
its decision on the test definition line or the assertion it exempts:

    def test_refusal_writes_nothing():  # noqa: VACUOUS_ASSERTION — product law
        assert writes == []

The reason is required. A lookalike in a string, docstring, or nested helper is
not the named test/assertion and grants nothing.

Stdlib-only, no Helm imports: the installed pre-commit hook executes a stable
snapshot of this file as ``python3 <snapshot> --staged`` before the refusing
never-track scan.
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_ESCAPE = re.compile(
    r"^#\s*noqa:\s*VACUOUS_ASSERTION\s+(?:—|-)\s*(\S.*)$")
_TEST_PATH = re.compile(r"(?:^|/)tests/(?:.*/)?test_[^/]+\.py$")
_CLASSIFIER_CONTROL = """\
def test_empty_result():
    got = run_probe()
    assert got == []
"""
_BUILTINS = {"all", "any", "bool", "dict", "len", "list", "open", "set",
             "tuple"}


def _git(root, *args, binary=False):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, text=not binary)
    return p.returncode, p.stdout, p.stderr


def _staged_paths(root):
    rc, out, err = _git(root, "diff", "--cached", "--name-only", "-z",
                        "--diff-filter=ACMRT")
    if rc != 0:
        raise RuntimeError("git diff --cached failed: %s" % err.strip())
    return [p for p in out.split("\0") if p and _TEST_PATH.search(p)]


def _staged_blob(root, rel):
    rc, out, _err = _git(root, "cat-file", "blob", ":0:" + rel, binary=True)
    return out if rc == 0 else None


def _added_ranges(root, rel):
    """Inclusive new-file line ranges touched by the staged diff.

    A deletion-only hunk has a zero new-line count. Its new-side anchor is still
    marked: removing the sole positive assertion is exactly a change this rung
    must see, and an advisory may conservatively inspect the enclosing test.
    """
    # ALGORITHM PINNED -- helm/conflict_marker.py owns the measurement: myers
    # re-adds identical text to shorten a big deletion's edit script, so an
    # unchanged line reads as this commit's. Histogram does not, and the pin
    # also neutralises an ambient `diff.algorithm`.
    rc, out, err = _git(root, "-c", "diff.algorithm=histogram",
                        "diff", "--cached", "-U0", "--no-color",
                        "--no-ext-diff", "--", rel)
    if rc != 0:
        raise RuntimeError("git diff --cached failed for %s: %s"
                           % (rel, err.strip()))
    ranges = []
    for line in out.splitlines():
        m = _HUNK.match(line)
        if not m:
            continue
        start = max(1, int(m.group(1)))
        count = int(m.group(2) or 1)
        ranges.append((start, start + max(1, count) - 1))
    return ranges


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if not isinstance(node, ast.Attribute):
        return ""
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _empty(node):
    if isinstance(node, ast.Constant):
        return node.value in (None, False, 0, "", b"")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return (isinstance(node, ast.Call) and not node.args and not node.keywords
            and _call_name(node.func) in ("dict", "list", "set", "tuple"))


# A NAMED CONSTANT IS A LITERAL WITH A NAME ON IT, AND THIS RUNG COULD NOT
# SEE THROUGH THE NAME. `assertEqual(got, mod.FAILED)` gave an arm no positive
# credit while the identical `assertEqual(got, "failed")` did, so a codebase
# that NAMES its states -- which is the practice a separate rule asks for --
# could not write a provable arm at all. The two escapes left were inlining
# the literal, which is the transcription defect, and a noqa, which is a claim
# the author does not believe. Neither is a fix.
#
# CREDIT IS EARNED BY RESOLUTION, NEVER BY NODE TYPE. A named constant can
# resolve to the empty string, and crediting a bare Attribute would make this
# rung assert something it had not measured. So the name is followed to its
# module and its value is read; if it cannot be followed, or resolves to
# something empty, nothing is credited and the old behaviour stands.
import symtable


_COMPREHENSIONS = {ast.ListComp: "listcomp", ast.SetComp: "setcomp",
                   ast.DictComp: "dictcomp", ast.GeneratorExp: "genexpr"}
_COMP_NODES = tuple(_COMPREHENSIONS)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                ast.ClassDef) + _COMP_NODES
_UNKNOWN_SCOPE = object()


class _Scopes:
    """AST evaluation ownership, paired only to unambiguous compiler scopes.

    Source containment is not evaluation ownership: defaults/decorators and
    the first comprehension iterable execute outside the scope they introduce.
    Neither AST order nor source columns identify symtable siblings sharing a
    (type, name, line) key. Such scopes remain unpaired, without poisoning an
    unrelated read. Symtable remains the owner of ordinary binding semantics.
    """

    def __init__(self, tree, rel, source):
        self.unknown = False
        self._owners = {}
        self._parents = {}
        self._children = {None: []}
        self._by_node = {}
        self._targets = {}
        self._inlined = set()
        self._walk(tree, None)
        try:
            root = symtable.symtable(source, rel, "exec")
        except (SyntaxError, ValueError, MemoryError):
            self.unknown = True
            return
        self._by_node[None] = root
        self._pair(root, None)

    def _walk(self, node, owner):
        self._owners[node] = owner
        if not isinstance(node, _SCOPE_NODES):
            for child in ast.iter_child_nodes(node):
                self._walk(child, owner)
            return
        self._parents[node] = owner
        self._children.setdefault(owner, []).append(node)
        self._children[node] = []
        if isinstance(node, _COMP_NODES):
            # Comprehension targets have their own lexical scope even when
            # PEP 709 inlines the compiler table. Only target Store names bind;
            # reads in target subscripts do not become iteration variables.
            self._targets[node] = {
                n.id for gen in node.generators for n in ast.walk(gen.target)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            for index, gen in enumerate(node.generators):
                self._walk(gen.iter, node if index else owner)
                self._walk(gen.target, node)
                for condition in gen.ifs:
                    self._walk(condition, node)
            values = (node.key, node.value) if isinstance(node, ast.DictComp) \
                else (node.elt,)
            for value in values:
                self._walk(value, node)
            return
        if isinstance(node, ast.ClassDef):
            outer = list(node.decorator_list) + list(node.bases) + list(node.keywords)
            annotations = list(getattr(node, "type_params", ()))
        else:
            a = node.args
            outer = list(a.defaults) + [d for d in a.kw_defaults if d is not None]
            outer += list(getattr(node, "decorator_list", ()))
            params = a.posonlyargs + a.args + a.kwonlyargs
            params += [p for p in (a.vararg, a.kwarg) if p is not None]
            annotations = [p.annotation for p in params if p.annotation is not None]
            annotations += [n for n in (getattr(node, "returns", None),)
                            if n is not None]
            annotations += list(getattr(node, "type_params", ()))
        for expression in outer:
            self._walk(expression, owner)
        # Annotation/type-parameter execution differs across supported Python
        # versions. It cannot supply literal authority through a guessed scope.
        for annotation in annotations:
            for child in ast.walk(annotation):
                self._owners[child] = _UNKNOWN_SCOPE
        body = [node.body] if isinstance(node, ast.Lambda) else node.body
        for statement in body:
            self._walk(statement, node)

    @staticmethod
    def _key(node):
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        name = _COMPREHENSIONS.get(type(node), getattr(node, "name", "lambda"))
        return kind, name, node.lineno

    def _pair(self, table, owner):
        entries = {}
        for child in table.get_children():
            key = (str(child.get_type()), child.get_name(), child.get_lineno())
            entries.setdefault(key, []).append(child)
        scopes = {}
        pending = list(self._children[owner])
        for node in pending:
            key = self._key(node)
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)) \
                    and sys.version_info >= (3, 12) and key not in entries:
                self._inlined.add(node)
                pending.extend(self._children[node])
                continue
            scopes.setdefault(key, []).append(node)
        for key, nodes in scopes.items():
            candidates = entries.get(key, ())
            if len(nodes) != 1 or len(candidates) != 1:
                continue                    # ambiguous or unsupported: UNKNOWN
            node, entry = nodes[0], candidates[0]
            self._by_node[node] = entry
            self._pair(entry, node)

    def owner(self, node):
        return self._owners.get(node, _UNKNOWN_SCOPE)

    def parent(self, node):
        return self._parents.get(node, _UNKNOWN_SCOPE)

    def table_for(self, node):
        return self._by_node.get(node)

    def shadowed(self, name, scope):
        """True: a different binding; False: module import; None: UNKNOWN."""
        while True:
            if scope is _UNKNOWN_SCOPE:
                return None
            if isinstance(scope, _COMP_NODES):
                if name in self._targets[scope]:
                    return True
                if scope in self._inlined:
                    scope = self.parent(scope)
                    continue
            table = self.table_for(scope)
            if table is None:
                return None
            try:
                symbol = table.lookup(name)
            except KeyError:
                return None
            if scope is None:
                if symbol.is_assigned():
                    return True
                return False if symbol.is_imported() else None
            if symbol.is_global():
                scope = None                # global skips intervening bindings
                continue
            if symbol.is_parameter() or symbol.is_assigned():
                return True
            if not (symbol.is_free() or symbol.is_nonlocal()):
                # A FUNCTION-BODY IMPORT IS A MODULE IMPORT, AND THIS
                # ANSWERED UNKNOWN FOR IT. The same discrimination the
                # module-scope branch makes above: an imported binding IS the
                # module, so it is False (not shadowed); anything else local
                # and unresolvable stays UNKNOWN.
                #
                # MEASURED through symtable, which is what makes the split
                # safe: a function-body `from helm import council` reports
                # is_assigned()=False, is_imported()=True, while a real local
                # `council = 5` reports is_assigned()=True, is_imported()=
                # False and has already returned True two lines above. So no
                # genuine shadow can reach this line and be called a module.
                return False if symbol.is_imported() else None
            scope = self.parent(scope)
            while isinstance(scope, ast.ClassDef):
                scope = self.parent(scope)  # method/free lookup skips class locals

    def namedexpr_global_writes(self, names):
        """PEP 572 writes escape comprehensions, not functions or lambdas.

        Inlined module comprehensions can leave the root symbol imported but
        not assigned, with no child table carrying the write. Use evaluation
        ownership and explicit declarations instead. A nonlocal destination
        cannot be the module; a default belongs to its enclosing scope, not
        the function whose signature contains it. Unpaired lambda tables do
        not make a syntactically local write global.
        """
        declarations = {}
        for node, owner in self._owners.items():
            if isinstance(node, ast.Global):
                declarations.setdefault(owner, set()).update(node.names)
        written = set()
        for node, owner in self._owners.items():
            if not isinstance(node, ast.NamedExpr) or node.target.id not in names:
                continue
            while isinstance(owner, _COMP_NODES):
                owner = self.parent(owner)
            name = node.target.id
            if owner is None or owner is _UNKNOWN_SCOPE \
                    or name in declarations.get(owner, ()):
                # Unsupported evaluation ownership cannot prove the module
                # untouched. Keep that uncertainty local to the written name.
                written.add(name)
        return written

    def global_writes(self, names):
        """Global assignment/import in a helper writes the module binding.

        Walk compiler tables, including unpaired helpers: the write's module
        destination is explicit in the symbol, not inferred from call reachability.
        """
        root = self.table_for(None)
        pending = [root] if root is not None else []
        written = self.namedexpr_global_writes(names)
        for table in pending:
            pending.extend(table.get_children())
            for name in names.intersection(table.get_identifiers()):
                symbol = table.lookup(name)
                if table is not root and symbol.is_declared_global() \
                        and (symbol.is_assigned() or symbol.is_imported()):
                    written.add(name)
        return written


class _Resolver:
    """Resolve a module literal at the read's semantic scope, or UNKNOWN."""

    def __init__(self, tree, rel, source_of, source):
        self._scopes = _Scopes(tree, rel, source)
        self.unknown = self._scopes.unknown
        self._shadowed_reads = set()        # cleared before each collected test
        self._source_of = source_of
        self._aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    if alias.name.isidentifier():
                        self._aliases.setdefault(alias.asname or alias.name,
                                                 "%s.%s" % (node.module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self._aliases.setdefault(alias.asname or alias.name.split(".")[0],
                                             alias.name)
        self._reimported, seen = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name in seen:
                        self._reimported.add(name)
                    seen.add(name)
        self._mutated = self._scopes.global_writes(set(self._aliases))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) \
                    and isinstance(node.ctx, (ast.Store, ast.Del)) \
                    and isinstance(node.value, ast.Name) \
                    and node.value.id in self._aliases:
                # Keep the conservative receiver-mutation boundary: even a
                # parameter can receive the module object (states=states).
                # Lexical binding alone cannot prove a different receiver.
                self._mutated.add(node.value.id)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in self._aliases \
                            and self._scopes.owner(target) is None:
                        self._mutated.add(target.id)

    def _enclosing(self, tree, node):
        return self._scopes.owner(node)

    def _shadowed_at(self, name, fn, tree):
        return self._scopes.shadowed(name, fn)

    def constant(self, node, fn=None, tree=None):
        """Return (resolved, value); the read node owns its evaluation scope."""
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            return False, None
        if self.unknown or self._source_of is None:
            return False, None
        name = node.value.id
        dotted = self._aliases.get(name)
        if not dotted or name in self._mutated or name in self._reimported:
            return False, None
        if self._scopes.shadowed(name, self._scopes.owner(node)) is not False:
            return False, None
        source = self._source_of(dotted.replace(".", os.sep) + ".py")
        if source is None:
            return False, None
        try:
            mod = ast.parse(source)
        except SyntaxError:
            return False, None
        # Retain the conservative PEP 562 boundary: this module can supply
        # values dynamically, so the source alone is not literal authority.
        if any(isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
               and stmt.name == "__getattr__" for stmt in mod.body):
            return False, None
        for stmt in mod.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name) or target.id != node.attr:
                continue
            try:
                return True, ast.literal_eval(stmt.value)
            except (ValueError, TypeError, SyntaxError, MemoryError):
                return False, None
        return False, None


_RESOLVER = None        # _Resolver for the file under analysis
_SITE = (None, None)    # (evaluation scope of the assertion, tree)

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_RESOLVER": "set for the file under analysis when each analysis starts",
    "_SITE": "set for the file under analysis when each analysis starts",
}


def _module_constant(node, fn=None, tree=None):
    """-> (resolved, value) for `alias.NAME` naming a module-level literal."""
    if _RESOLVER is None:
        return False, None
    return _RESOLVER.constant(node, fn, tree)


def _nonempty_named(node):
    """Does this name resolve to a NONEMPTY module-level literal, AT the
    assertion site under analysis? The site context is threaded through
    _SITE (set by _analyze_source per test function), never a global map —
    a name that is a module alias at one assertion can be shadowed at
    another, and one answer for the file was the round-1 lie."""
    resolved, value = _module_constant(node, _SITE[0], _SITE[1])
    return bool(resolved and value)


def _nonempty_literal(node):
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    return False


def _number(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) \
            and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, (int, float)):
        return -node.operand.value
    return None


def _roots(node):
    """Observable names one assertion constrains; helpers/builtins are not
    data, and neither is an alias whose resolution FAILED at this site — a
    shadowed `states` in `states.FAILED` was replaced by the test, so the
    assertion never observed the module, and leaving the name in the roots
    would let a sibling's unrelated positive cover it (the same-line
    sibling-lambda probe)."""
    out = set()
    # AN INSTANCE ATTRIBUTE IS AN OBSERVABLE AND `self` ALONE IS NOT.
    # Skipping every Name called `self` left `self.warned` contributing NO
    # ROOTS AT ALL, so an absence over it was uncovered by construction and
    # no positive control could ever reach it — the method was reported for
    # having no unconditional positive control while holding one on the very
    # same attribute. `self.warned` yields the stable root `self.warned`,
    # which the positive and the absence then SHARE, exactly as two reads of
    # a local do.
    #
    # ONLY THE ONE HOP. A deeper chain stays unrooted: `self.a.b` says
    # nothing about what `b` is, and a fixture can rebind it between the two
    # assertions without either spelling changing. One hop is the part whose
    # identity the method itself controls.
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id == "self" and isinstance(n.ctx, ast.Load):
            out.add("self.%s" % n.attr)
            continue
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)) \
                or n.id in _BUILTINS or n.id == "self":
            continue
        if _RESOLVER is not None and n.id in _RESOLVER._aliases:
            # An alias root is observable only when the name is the module
            # HERE. A shadowed `states` was replaced by the test: drop it
            # from the ABSENCE side so a sibling's unrelated positive cannot
            # cover it, and pin it into the shadow set so the method still
            # warns — the assertion claimed a value nobody wrote down.
            shadowed = _RESOLVER._shadowed_at(
                n.id, _RESOLVER._scopes.owner(n), _SITE[1])
            if shadowed is not False:
                _RESOLVER._shadowed_reads.add(n.id)
                continue
        out.add(n.id)
    return out


def _scalar_zero(node):
    """A non-bool numeric zero literal — an exact scalar contract, not emptiness."""
    if isinstance(node, ast.Constant) and type(node.value) is bool:
        return False
    return _number(node) == 0


def _comparison(left, right, op):
    if isinstance(op, ast.In):
        return not _empty(left), _roots(right)
    if isinstance(op, ast.NotIn):
        return False, _roots(right)
    if isinstance(op, (ast.Is, ast.IsNot)):
        return False, _roots(left) | _roots(right)
    if isinstance(op, ast.Eq):
        if _nonempty_literal(left) or _nonempty_named(left):
            return True, _roots(right)
        if _nonempty_literal(right) or _nonempty_named(right):
            return True, _roots(left)
        # `rc == 0` pins a bare name to one exact scalar (codex ae7a5d6f):
        # NEUTRAL — no data-presence proof, but no absence demanding cover.
        # Only a bare Name earns this; len(rows) == 0 and d["n"] == 0 keep
        # absence semantics so the emptiness idiom cannot be laundered.
        if (_scalar_zero(left) and isinstance(right, ast.Name)) \
                or (_scalar_zero(right) and isinstance(left, ast.Name)):
            return None, set()
        return False, _roots(left) | _roots(right)
    if isinstance(op, ast.NotEq):
        if _empty(left):
            return True, _roots(right)
        if _empty(right):
            return True, _roots(left)
        return False, _roots(left) | _roots(right)
    if isinstance(op, ast.Gt):
        n = _number(right)
        return n is not None and n >= 0, _roots(left)
    if isinstance(op, ast.GtE):
        n = _number(right)
        return n is not None and n >= 1, _roots(left)
    if isinstance(op, ast.Lt):
        n = _number(left)
        return n is not None and n >= 0, _roots(right)
    if isinstance(op, ast.LtE):
        n = _number(left)
        return n is not None and n >= 1, _roots(right)
    return False, _roots(left) | _roots(right)


def _expr_info(node):
    """(requires-present/truthy-observable, observable roots)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return False, _roots(node.operand)
    if isinstance(node, ast.BoolOp):
        info = [i for i in (_expr_info(v) for v in node.values)
                if i[0] is not None]        # neutral legs claim nothing
        if not info:
            return None, set()
        positive = (all(p for p, _r in info) if isinstance(node.op, ast.Or)
                    else any(p for p, _r in info))
        return positive, set().union(*(r for _p, r in info))
    if isinstance(node, ast.Compare):
        lefts = (node.left,) + tuple(node.comparators[:-1])
        info = [i for i in (_comparison(left, right, op)
                            for left, right, op
                            in zip(lefts, node.comparators, node.ops))
                if i[0] is not None]        # neutral legs claim nothing
        if not info:
            return None, set()
        return any(p for p, _r in info), set().union(*(r for _p, r in info))
    if isinstance(node, ast.Constant):
        return False, set()
    return (isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)),
            _roots(node))


def _call_info(node):
    """None for non-assertions, else (positive, observable roots)."""
    name = _call_name(node.func)
    leaf = name.rsplit(".", 1)[-1]
    low = leaf.lower()
    if not (leaf.startswith("assert") or low.startswith("assert_")):
        return None
    args = node.args
    # A MOCK'S OWN ASSERTION OBSERVES ITS RECEIVER, NOT ITS ARGUMENTS, in BOTH
    # polarities. `spy.assert_not_called()` takes no argument, so rooting it
    # on `args[0]` gave it NO roots: an absence no positive control could
    # ever reach, warned even beside `spy.send.assert_called_once_with(...)`
    # on the very same double. Rooted on the receiver it roots exactly as
    # `assertFalse(spy.called)` does, and still warns when nothing positive
    # constrains that spy (task/3039).
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else node
    if low in ("assert_not_called", "assert_not_awaited"):
        return False, _roots(receiver)
    if low in ("assertfalse", "assertisnone", "assertnotregex"):
        return False, _roots(args[0]) if args else set()
    if low in ("assertraises", "assertraisesregex"):
        # AN EXPECTED RAISE IS AN EVENT THAT HAPPENED, NOT AN ABSENCE.
        # Listed among the absence forms, it was uncoverable BY
        # CONSTRUCTION: its roots are the exception CLASS, nothing ever
        # asserts positively about `ValueError`, so no positive control in
        # any method could intersect them and a method whose only assertion
        # was a raise-expectation could never be cleared. The message it drew
        # then said the method had no unconditional positive control, while
        # the raise-expectation IS one.
        #
        # THE ROOTS STAY THE EXCEPTION NAME, deliberately, and it is the
        # arguable half. They must be NON-EMPTY or the positive does not
        # register at all — `seen.positive` is a set and an empty
        # contribution is falsy — so this cannot contribute nothing. The
        # consequence: `assertRaises(mod.Refused)` contributes {"mod"} and
        # can therefore cover an absence over `mod` elsewhere in the method.
        # That is defensible rather than accidental — a method that raised
        # mod.Refused did exercise mod — and the arm below PINS it so the
        # next reader meets a decision instead of a side effect.
        return True, _roots(args[0]) if args else set()
    if low == "assertnotin":
        return False, _roots(args[1]) if len(args) > 1 else set()
    if low == "assertisnotnone":
        # `x is not None` IS A POSITIVE CONTROL, and leaving it out of this
        # whitelist cost the rung twice, not once: the terminal default both
        # withheld the credit AND recorded an ABSENCE claim over the argument,
        # so a present value manufactured a demand for cover. Measured over
        # the whole test tree, restoring it silenced 85 warnings and raised
        # none; a sample read by hand was genuine controls throughout,
        # including arms whose own comments named the line "CONTROL FIRST".
        # The dominant shape is the two-channel refusal the module docstring
        # already builds provenance for -- `assertIsNone(out)` beside
        # `assertIsNotNone(why)` is the same contract as `assertIn("required",
        # why)`, one notch weaker.
        #
        # AND THE PRICE, PINNED RATHER THAN DISCOVERED LATER: not-None does
        # not mean non-empty, so `assertIsNotNone(rows)` now covers
        # `assertEqual(rows, [])` on the same name -- the rung's own founding
        # shape. That trade is deliberate. A whole-tree census for it found no
        # arm relying on the accident, and withholding the credit to keep the
        # narrow catch trained 85 honest tests toward `# noqa` instead.
        return True, _roots(args[0]) if args else set()
    if low == "assertin":
        return (len(args) > 1 and not _empty(args[0]),
                _roots(args[1]) if len(args) > 1 else set())
    if low == "assertgreater":
        n = _number(args[1]) if len(args) > 1 else None
        return n is not None and n >= 0, _roots(args[0]) if args else set()
    if low == "assertgreaterequal":
        n = _number(args[1]) if len(args) > 1 else None
        return n is not None and n >= 1, _roots(args[0]) if args else set()
    if low in ("assert_called", "assert_called_once", "assert_called_with",
               "assert_called_once_with", "assert_has_calls", "assert_awaited",
               "assert_awaited_once", "assert_awaited_with",
               "assert_awaited_once_with"):
        return True, _roots(receiver)
    if low == "asserttrue":
        return _expr_info(args[0]) if args else (False, set())
    if low in ("assertequal", "assertsequenceequal", "assertlistequal",
               "asserttupleequal", "assertsetequal", "assertdictequal",
               "assertcountequal") and len(args) > 1:
        return _comparison(args[0], args[1], ast.Eq())
    if low == "assertnotequal" and len(args) > 1:
        return _comparison(args[0], args[1], ast.NotEq())
    roots = set().union(*(_roots(a) for a in args)) if args else set()
    return False, roots              # unknown assertion cannot clear the warning


#: Direct call readings shared by default Mock/MagicMock/AsyncMock doubles.
#: No AsyncMock type is established here: await_* names on a synchronous
#: MagicMock synthesize child mocks, not AsyncMock's None/zero/empty defaults.
#: Aggregate mock_calls can record CHILD calls while the parent is uncalled.
#: Unsupported readings therefore stay UNKNOWN rather than earning credit.
SPY_NEVER_FIRED = {
    "called": False,
    "call_count": 0,
    "call_args": None,
    "call_args_list": [],
}

#: Assertion methods on the mock itself whose outcome against a NEVER-FIRED
#: double is fixed by mock's own semantics rather than by any expression.
#:   True  -> it RAISES, so the arm cannot pass: the double is on the path.
#:   False -> it PASSES, so it proves nothing about reachability.
#: `assert_any_call` RAISES whatever its arguments are, because a mock with no
#: calls at all can match no expected call — INCLUDING the zero-argument and
#: empty-list forms, which name a call rather than an empty expectation.
_SPY_METHOD_FAILS_WHEN_NEVER_FIRED = {
    "assert_called": True, "assert_called_once": True,
    "assert_called_with": True, "assert_called_once_with": True,
    "assert_awaited": True, "assert_awaited_once": True,
    "assert_awaited_with": True, "assert_awaited_once_with": True,
    "assert_any_call": True, "assert_any_await": True,
    "assert_not_called": False, "assert_not_awaited": False,
}

_UNDECIDABLE = object()

#: EVALUATION RAISED at the never-fired valuation, which is a DIFFERENT answer
#: from "the assertion came out False" and from "I could not tell". A statement
#: that raises cannot pass either, so it proves the double fired just as a
#: failed assertion does — `spy.call_args.kwargs` is an AttributeError on None,
#: while `spy.call_args_list.copy` is an ordinary bound method and proves
#: nothing.
_RAISES = object()


def _never_fired_value(node, handle):
    """Only direct readings, literals, negation and one comparison are known.

    This is not a Python interpreter. In particular, Boolean operators return
    operands, slices need their actual bounds, and names/calls need binding
    resolution. All of those stay UNKNOWN rather than inventing a value.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == handle:
            return SPY_NEVER_FIRED.get(node.attr, _UNDECIDABLE)
        return _UNDECIDABLE
    if isinstance(node, ast.Attribute) and node.attr in ("args", "kwargs"):
        inner = node.value
        if isinstance(inner, ast.Attribute) and inner.attr == "call_args" \
                and isinstance(inner.value, ast.Name) and inner.value.id == handle:
            return _RAISES       # None has neither args nor kwargs; it HAS __class__
    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return [] if isinstance(node, ast.List) else ()
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _never_fired_value(node.operand, handle)
        if inner is _UNDECIDABLE or inner is _RAISES:
            return inner
        return not inner
    if isinstance(node, ast.Subscript):
        base = _never_fired_value(node.value, handle)
        if base is _RAISES:
            return _RAISES
        if isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, int) \
                and (base is None or isinstance(base, list) and not base):
            return _RAISES       # a literal integer index, never a slice
    if isinstance(node, ast.Compare):
        return _never_fired_compare(node, handle)
    return _UNDECIDABLE


_COMPARE_OPS = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
}


def _never_fired_compare(node, handle):
    # Chained comparisons short-circuit; do not pretend they are one operation.
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return _UNDECIDABLE
    fn = _COMPARE_OPS.get(type(node.ops[0]))
    left = _never_fired_value(node.left, handle)
    right = _never_fired_value(node.comparators[0], handle)
    if fn is None or left is _UNDECIDABLE or right is _UNDECIDABLE:
        return _UNDECIDABLE
    if left is _RAISES or right is _RAISES:
        return _RAISES
    try:
        return fn(left, right)
    except (TypeError, ValueError):
        return _UNDECIDABLE


#: unittest assertions whose TRUTH-BEARING operands are known, given as the
#: argument indices that decide the outcome. Everything past them is the
#: optional `msg`, which is why a spy in the MESSAGE POSITION earns nothing.
_ASSERT_TRUTH = {
    "assertTrue": (lambda v: bool(v[0]), 1),
    "assertFalse": (lambda v: not v[0], 1),
    "assertIsNone": (lambda v: v[0] is None, 1),
    "assertIsNotNone": (lambda v: v[0] is not None, 1),
    "assertEqual": (lambda v: v[0] == v[1], 2),
    "assertNotEqual": (lambda v: v[0] != v[1], 2),
    "assertGreater": (lambda v: v[0] > v[1], 2),
    "assertGreaterEqual": (lambda v: v[0] >= v[1], 2),
    "assertLess": (lambda v: v[0] < v[1], 2),
    "assertLessEqual": (lambda v: v[0] <= v[1], 2),
}


def spy_assertion_proves_it_fired(node, handle):
    """Does this statement PROVE `handle`'s double was invoked?

    THE ONE QUESTION, and it is not "is this assertion positive" nor "does
    this assertion mention the handle": it is WOULD THIS FAIL IF THAT DOUBLE
    HELD ITS NEVER-FIRED VALUES. Only a decided failure earns an exemption, so
    a disjunction over TWO spies exempts NEITHER — each is asked separately,
    and an assertion the other spy could satisfy is undecidable for this one.

    The caller must select an executing, non-suppressing statement context.
    Harmless statements such as `unused = spy.call_count > 0` constrain no
    outcome; directly known-raising record access cannot pass. Unsupported
    shapes prove nothing, which is the direction an advisory census errs in.
    """
    if isinstance(node, ast.Assert):
        # A BARE `assert` IS AN ASSERTION. Judged by the same rule: it proves
        # the double fired when its test comes out FALSE, or raises, with that
        # double held at its never-fired values.
        if not _mentions_handle(node.test, handle):
            return False
        got = _never_fired_value(node.test, handle)
        if got is _RAISES:
            return True
        return got is not _UNDECIDABLE and not got
    if isinstance(node, (ast.Expr, ast.Assign)):
        if _mentions_handle(node.value, handle) \
                and _never_fired_value(node.value, handle) is _RAISES:
            return True
    # Only a direct call STATEMENT is an assertion call. A Call found beneath
    # a lambda, Boolean operand, comprehension or arbitrary wrapper is not.
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    node = node.value
    leaf = _call_name(node.func).rsplit(".", 1)[-1]
    fixed = _SPY_METHOD_FAILS_WHEN_NEVER_FIRED.get(leaf)
    if fixed is not None:
        receiver = node.func.value if isinstance(node.func, ast.Attribute) \
            else None
        if not (isinstance(receiver, ast.Name) and receiver.id == handle):
            return False
        return fixed
    if leaf in ("assert_has_calls", "assert_has_awaits"):
        receiver = node.func.value if isinstance(node.func, ast.Attribute) \
            else None
        if not (isinstance(receiver, ast.Name) and receiver.id == handle):
            return False
        if not node.args:
            return False
        # A starred element can expand to NOTHING. Reject that unsupported
        # shape, even when the literal has a nonzero AST element count.
        want = node.args[0]
        if not isinstance(want, (ast.List, ast.Tuple)) or not want.elts \
                or any(isinstance(e, ast.Starred) for e in want.elts):
            return False
        if leaf == "assert_has_awaits":
            return True                 # await_args_list belongs to this double
        # assert_has_calls reads aggregate mock_calls, which includes CHILD
        # calls without invoking the parent. Require a direct root call(...)
        # expectation; call.child(...) and unresolved values prove nothing.
        return any(isinstance(e, ast.Call)
                   and _call_name(e.func) in ("mock.call", "call")
                   for e in want.elts)
    rule = _ASSERT_TRUTH.get(leaf)
    if rule is None or not (isinstance(node.func, ast.Attribute)
                           and isinstance(node.func.value, ast.Name)
                           and node.func.value.id == "self"):
        return False                    # only the standard unittest spelling
    decide, arity = rule
    if len(node.args) < arity:
        return False
    values = [_never_fired_value(a, handle) for a in node.args[:arity]]
    if any(v is _UNDECIDABLE for v in values):
        return False
    if any(v is _RAISES for v in values):
        return True                      # the assertion itself blows up
    if not any(_mentions_handle(a, handle) for a in node.args[:arity]):
        return False
    try:
        return not decide(values)
    except Exception:                    # noqa: BLE001 — undecidable
        return False


def _mentions_handle(node, handle):
    return any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
               and n.value.id == handle for n in ast.walk(node))


def _spy_roots(fn):
    """Local list-valued instrumentation mutated by a nested spy helper."""
    out = set()
    for stmt in fn.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "append" \
                    and isinstance(node.func.value, ast.Name):
                out.add(node.func.value.id)
    return out


class _Bindings:
    """name -> provenance of its LATEST binding (straight-line, one scope).

    Identity is PRODUCER-based, not spelling-based (review of ae7a5d6f):
    every call in a binding RHS mints a fresh token, so two calls never share
    merely because callee or argument names match; tuple channels of ONE call
    share that call's token (``out, why = f()`` splits one result); a receiver
    chain flows because its data is the receiver's data; rebinding REPLACES
    provenance rather than accreting it. A ``with ... as`` target is a view
    of its whole expression, so full name lineage flows there — that is what
    keeps ``open(dispatches.ledger_path()) as f`` bound to ``dispatches``.
    An element-wise pack (``a, b = x, y``) binds each name to its own element.

    NOT a visitor: _Assertions drives this store from its own single
    document-order walk, so every binding lands exactly when the source
    reaches it — a with-body assignment binds before the assertion on the
    next line, never after the whole statement.
    """

    def __init__(self):
        self.aliases = {}
        self._minted = 0

    def _fresh(self):
        self._minted += 1
        return "call#%d" % self._minted      # '#' never appears in a real Name

    def _producer_roots(self, value):
        roots, stack = set(), [value]
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Call):
                roots.add(self._fresh())
                if isinstance(node.func, ast.Attribute):
                    stack.append(node.func.value)    # receiver lineage flows
                continue                             # callee/args never do
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load) and node.id != "self" \
                        and node.id not in _BUILTINS:
                    roots.add(node.id)
                continue
            stack.extend(ast.iter_child_nodes(node))
        return roots

    def _resolve_now(self, roots):
        """Assignment copies VALUES, so capture provenance eagerly.

        r6: storing the edge `got -> source` by name and reading
        it lazily let source's LATER rebind rewrite got's provenance — the
        bound spelling survived one level down, in the alias graph. Every
        bound RHS name is resolved to its provenance AT ASSIGNMENT TIME, so
        the stored map is FLAT: only producer tokens and unbound spellings,
        at every level, by construction.
        """
        out = set()
        for root in roots:
            if root in self.aliases:
                out |= self.aliases[root]    # already flat: one hop suffices
            else:
                out.add(root)
        return out

    def _bind(self, target, roots):
        roots = self._resolve_now(roots)
        for n in ast.walk(target):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                # rebind REPLACES, and every binding is an EPOCH: `x = []`
                # has no producers but still denotes THIS binding, so it
                # mints its own token rather than vanishing or keeping an
                # older producer identity
                self.aliases[n.id] = (roots - {n.id}) or {self._fresh()}

    def _assign(self, target, value):
        if isinstance(target, (ast.Tuple, ast.List)) \
                and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts):
            for t, v in zip(target.elts, value.elts):
                self._assign(t, v)
            return
        self._bind(target, self._producer_roots(value))

    def assign(self, node):
        if len(node.targets) == 1:
            self._assign(node.targets[0], node.value)
        else:
            roots = self._producer_roots(node.value)
            for target in node.targets:
                self._bind(target, roots)

    def _context_roots(self, expr):
        """Producer identity PLUS name lineage, minus bare-callee spellings.

        A context target is a view of its whole expression (round 3), so the
        names inside flow — that is what keeps ``open(dispatches.ledger_path())
        as f`` bound to ``dispatches``. But the meld's residual (1) showed two
        ``with acquire() as x`` sharing the callee spelling ``acquire``: each
        call must still mint its own producer, and a bare callee is an
        operation's name, never its data.
        """
        callees = {n.func.id for n in ast.walk(expr)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        return self._producer_roots(expr) | (_roots(expr) - callees)

    def with_items(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars,
                           self._context_roots(item.context_expr))


def _expand(roots, aliases):
    """Resolve names to the provenance of their CURRENT binding.

    A BOUND name contributes its producers and never its own spelling
    (r4: keeping the raw name let two binding epochs of `got`
    intersect at the literal token `got`, re-linking provenance by spelling
    one level down). An UNBOUND name keeps its spelling — for a module or a
    fixture the scope never rebinds, the spelling IS the stable identity,
    which is exactly the pinned receiver-lineage boundary.
    """
    out, walked = set(), set()
    frontier = list(roots)
    while frontier:
        name = frontier.pop()
        if name in aliases:
            if name not in walked:
                walked.add(name)
                frontier.extend(aliases[name])
        else:
            out.add(name)
    return out


class _DiscardedCalls(ast.NodeVisitor):
    def __init__(self):
        self.roots = []

    def visit_FunctionDef(self, node):
        return

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Call) and _call_info(node.value) is None:
            roots = _roots(node.value.func)
            if roots:
                self.roots.append(roots)
        self.generic_visit(node)


class _Assertions(ast.NodeVisitor):
    """Assertions with PER-ASSERTION provenance snapshots (r3).

    Roots are expanded through the binding map AT RECORD TIME, so an
    assertion is judged against the bindings that existed when it ran: a
    later rebind can neither launder an earlier empty result through its new
    producer nor erase a tuple relationship that held when both channels
    were asserted. Assignment kill affects only LATER assertions.
    """

    def __init__(self, binds, fn, tree):
        self.binds = binds
        self.guarded = 0
        self.assertion_lines = set()
        self.absence = []
        self.positive = set()
        self.guarded_positive = set()
        # Evaluation ownership comes from the resolver's semantic AST tree,
        # including defaults and comprehension iteration scopes.
        self._tree = tree

    def visit_FunctionDef(self, node):
        return                       # nested functions are separate lexical scopes

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        # A lambda's body EXECUTES and its assertions count; its parameters
        # bind at call time, so the site moves INTO the lambda's scope for
        # the duration of the body. Defaults sit outside it.
        for d in node.args.defaults + [d for d in node.args.kw_defaults
                                       if d is not None]:
            self.visit(d)
        self.visit(node.body)

    def _record(self, node, info):
        positive, roots = info
        self.assertion_lines.add(node.lineno)
        if positive is None:
            return                   # exact scalar pin: neither proof nor claim
        expanded = _expand(roots, self.binds.aliases)  # snapshot NOW, not at end
        if positive:
            if self.guarded:
                self.guarded_positive.update(expanded)
            else:
                self.positive.update(expanded)
        else:
            # THE LINE TRAVELS WITH THE CLAIM. Without it a per-assertion
            # exemption is unrepresentable, so every noqa had to be read as
            # method-wide, because the escape had no line to compare against.
            self.absence.append((node.lineno, expanded))

    def visit_Assert(self, node):
        global _SITE
        _SITE = (_RESOLVER._scopes.owner(node), self._tree)
        self._record(node, _expr_info(node.test))

    def visit_Call(self, node):
        global _SITE
        _SITE = (_RESOLVER._scopes.owner(node), self._tree)
        info = _call_info(node)
        if info is not None:
            self._record(node, info)
        self.generic_visit(node)

    def visit_Assign(self, node):
        self.binds.assign(node)      # bindings land in document order
        self.generic_visit(node)

    def visit_With(self, node):
        self.binds.with_items(node)  # the as-target exists before the body
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def _guard(self, node):
        # Guarded control flow may not have run, so a binding made inside it
        # cannot rewrite unconditional provenance (meld residual 3: an
        # if-branch rebind laundered a rotten green for the branch-not-taken
        # run). Branch-local assertions see branch state; exit restores.
        saved = dict(self.binds.aliases)
        self.guarded += 1
        self.generic_visit(node)
        self.guarded -= 1
        self.binds.aliases.clear()
        self.binds.aliases.update(saved)

    def visit_Try(self, node):
        # Assertion guarding is unchanged — anything inside may be jumped by
        # an exception. Binding persistence depends on whether a raise can be
        # SWALLOWED: with no handlers (try/finally), post-try code proves the
        # body completed, so its bindings persist — the corpus monkeypatch
        # idiom. With handlers, a caught raise may have cut the body at any
        # point, and "completed or unbound" only holds for FRESH names — a
        # REBIND cut short leaves the OLD binding, not a NameError (a
        # fresh chain off fdd6ed2 "vacuous rung: the meld's complete
        # residual list, one pass": try-body `got = other` behind a possible
        # raise laundered the branch-not-taken run). So a handled body
        # restores like a branch; handlers and orelse always restore;
        # finally runs on every path and persists.
        self.guarded += 1
        saved = dict(self.binds.aliases) if node.handlers else None
        for stmt in node.body:
            self.visit(stmt)
        if saved is not None:
            self.binds.aliases.clear()
            self.binds.aliases.update(saved)
        for sub in list(node.handlers) + list(node.orelse):
            sub_saved = dict(self.binds.aliases)
            self.visit(sub)
            self.binds.aliases.clear()
            self.binds.aliases.update(sub_saved)
        for stmt in node.finalbody:
            self.visit(stmt)
        self.guarded -= 1

    visit_If = _guard
    visit_For = _guard
    visit_AsyncFor = _guard
    visit_While = _guard
    visit_Match = _guard
    visit_ListComp = _guard
    visit_SetComp = _guard
    visit_DictComp = _guard
    visit_GeneratorExp = _guard


def _comments(source):
    out = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                m = _ESCAPE.match(tok.string)
                if m:
                    out[tok.start[0]] = m.group(1)
    except tokenize.TokenError:
        pass
    return out


def _intersects(node, ranges):
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", None) or start
    return any(start <= hi and end >= lo for lo, hi in ranges)


def _test_functions(tree):
    """Collected identities only: module functions and direct class methods."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                out.append(node)
        elif isinstance(node, ast.ClassDef):
            out += [n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name.startswith("test")]
    return out


def _analyze_source(source, rel, ranges, source_of=None):
    tree = ast.parse(source, filename=rel)
    comments = _comments(source)
    global _RESOLVER, _SITE
    _RESOLVER = _Resolver(tree, rel, source_of, source)
    findings = []
    functions = [n for n in _test_functions(tree) if _intersects(n, ranges)]
    for fn in sorted(functions, key=lambda n: (n.lineno, n.name)):
        _RESOLVER._shadowed_reads.clear()
        binds = _Bindings()
        seen = _Assertions(binds, fn, tree)
        # Document order IS the provenance clock: _Assertions' single walk
        # drives the binding store, so an assertion snapshots exactly the
        # bindings the source had reached — including inside with/if/loop
        # bodies. A later assignment can only affect later assertions.
        for stmt in fn.body:
            seen.visit(stmt)
        instrumentation_only = False
        spies = _spy_roots(fn)
        # Instrumentation is compared by BINDING IDENTITY, not spelling (meld
        # residual 2: `alias = calls` evaded a spelling comparison while
        # sharing the spy list's epoch). Spy names expand under the final map;
        # spy lists are bound once at the top of a test, so their epoch is
        # stable — recorded positive snapshots meet them on equal terms.
        spy_identity = _expand(spies, binds.aliases)
        if spies and seen.positive and seen.positive <= spy_identity:
            dropped = _DiscardedCalls()
            for stmt in fn.body:
                dropped.visit(stmt)
            if dropped.roots:
                # discarded production calls, judged under the final map — an
                # Expr statement binds nothing, so timing cannot matter here
                seen.absence += [(fn.lineno, _expand(r, binds.aliases))
                                 for r in dropped.roots]
                instrumentation_only = True
        # A NOQA ON THE DEFINITION EXEMPTS THE METHOD. A noqa on ONE
        # ASSERTION EXEMPTS THAT ASSERTION.
        #
        # These used to be the same thing: every assertion line was folded
        # into `escape_lines` and any hit skipped the whole function. So an
        # author marking a single deliberate absence silently exempted every
        # other assertion in the method — including ones added later, by
        # someone else, who had no idea an exemption was already in force.
        # The narrower marking was not merely unsupported, it read as if it
        # had worked.
        if fn.lineno in comments:
            continue
        kept = [(line, roots) for line, roots in seen.absence
                if line not in comments]
        # AND THE TRAP IN NARROWING IT. If exempting the marked assertions
        # empties the list, the no-absence branch reads "no absence at all" and can
        # report the method under a DIFFERENT rule — so a correct, explicit
        # exemption would have produced a new finding instead of silence.
        # Every absence here was named by the author; there is nothing left to
        # demand cover for.
        if seen.absence and not kept:
            continue
        seen.absence = kept
        if not seen.absence:
            if seen.positive:
                continue
            why = ("its only positive control is behind if/loop/try and may "
                   "not run" if seen.guarded_positive else
                   "it has no unconditional structural assertion")
        else:
            # Roots were snapshot-expanded at record time, so coverage
            # compares them directly. The spy check above asks which
            # assertions were WRITTEN, not what their data reaches.
            uncovered = [roots for _line, roots in seen.absence
                         if not roots or roots.isdisjoint(seen.positive)]
            if not uncovered and not (_RESOLVER is not None
                                      and _RESOLVER._shadowed_reads):
                continue
            guarded = any(roots and not roots.isdisjoint(seen.guarded_positive)
                          for roots in uncovered)
            if instrumentation_only:
                why = "it proves only spy instrumentation while discarding the call result"
            elif guarded:
                why = "its matching positive control is behind if/loop/try and may not run"
            else:
                why = "an empty/absent observable has no unconditional positive control"
        findings.append((rel, fn.lineno, fn.name, why))
    return findings


def _source_reader(root):
    """Read a module's source: the STAGED blob when it has one, else the file.

    The staged blob is preferred because that is the tree being committed;
    falling back to the worktree covers a module this commit does not touch.
    A path that resolves outside the repository is refused rather than read.
    """
    def read(rel):
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(os.path.realpath(root) + os.sep):
            return None
        blob = _staged_blob(root, rel)
        if blob is not None:
            try:
                return blob.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError):
            return None
    return read


def scan(root):
    """(findings, issues, staged_test_paths, classifier_control_ok)."""
    findings, issues = [], []
    paths = _staged_paths(root)
    for rel in paths:
        blob = _staged_blob(root, rel)
        if blob is None:
            issues.append("%s: staged entry is not a readable blob" % rel)
            continue
        try:
            source = blob.decode("utf-8")
        except UnicodeDecodeError:
            issues.append("%s: staged Python test is not UTF-8" % rel)
            continue
        ranges = _added_ranges(root, rel)
        if not ranges:
            continue
        try:
            findings += _analyze_source(source, rel, ranges,
                                        source_of=_source_reader(root))
        except SyntaxError as e:
            issues.append("%s:%s: staged Python cannot be parsed (%s)"
                          % (rel, e.lineno or "?", e.msg))
    control = bool(_analyze_source(_CLASSIFIER_CONTROL, "control.py", [(1, 99)]))
    return findings, issues, paths, control


def main(args):
    try:
        findings, issues, _paths, control = scan(os.getcwd())
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        print("[helm vacuous-assertion-rung] scan FAILED: %s — WARN-only, "
              "commit allowed; an unscanned staged test set is UNKNOWN"
              % e, file=sys.stderr)
        return 0
    if not control:
        print("[helm vacuous-assertion-rung] CLASSIFIER CONTROL FAILED — the "
              "analyzer cannot see its planted empty-result test; commit "
              "allowed, but classifier silence is not a clean bill", file=sys.stderr)
        return 0
    for issue in issues:
        print("[helm vacuous-assertion-rung] scan UNKNOWN: %s — commit allowed"
              % issue, file=sys.stderr)
    for rel, line, name, why in findings:
        print("[helm vacuous-assertion-rung] %s:%d %s — %s" %
              (rel, line, name, why), file=sys.stderr)
    if findings:
        print("[helm vacuous-assertion-rung] %d staged test%s may pass "
              "vacuously. WARN only. Add `# noqa: VACUOUS_ASSERTION — "
              "<reason>` on the test definition or intentional absence "
              "assertion; otherwise add an unconditional positive control on "
              "the same observable." %
              (len(findings), "s"[:len(findings) != 1]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
