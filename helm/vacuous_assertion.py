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
    rc, out, err = _git(root, "diff", "--cached", "-U0", "--no-color",
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
    """Observable names one assertion constrains; helpers/builtins are not data."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            and isinstance(n.ctx, ast.Load)
            and n.id not in _BUILTINS and n.id != "self"}


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
        if _nonempty_literal(left):
            return True, _roots(right)
        if _nonempty_literal(right):
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
    if low in ("assertfalse", "assertisnone", "assertnotregex",
               "assert_not_called", "assert_not_awaited", "assertraises",
               "assertraisesregex"):
        return False, _roots(args[0]) if args else set()
    if low == "assertnotin":
        return False, _roots(args[1]) if len(args) > 1 else set()
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
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else node
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

    Identity is PRODUCER-based, not spelling-based (codex review ae7a5d6f):
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

        codex round 6: storing the edge `got -> source` by name and reading
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
    (codex round 4: keeping the raw name let two binding epochs of `got`
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
    """Assertions with PER-ASSERTION provenance snapshots (codex round 3).

    Roots are expanded through the binding map AT RECORD TIME, so an
    assertion is judged against the bindings that existed when it ran: a
    later rebind can neither launder an earlier empty result through its new
    producer nor erase a tuple relationship that held when both channels
    were asserted. Assignment kill affects only LATER assertions.
    """

    def __init__(self, binds):
        self.binds = binds
        self.guarded = 0
        self.assertion_lines = set()
        self.absence = []
        self.positive = set()
        self.guarded_positive = set()

    def visit_FunctionDef(self, node):
        return                       # nested functions are separate lexical scopes

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

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
            self.absence.append(expanded)

    def visit_Assert(self, node):
        self._record(node, _expr_info(node.test))

    def visit_Call(self, node):
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
        # REBIND cut short leaves the OLD binding, not a NameError (codex,
        # fresh chain off "vacuous rung: the meld's complete
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


def _analyze_source(source, rel, ranges):
    tree = ast.parse(source, filename=rel)
    comments = _comments(source)
    findings = []
    functions = [n for n in _test_functions(tree) if _intersects(n, ranges)]
    for fn in sorted(functions, key=lambda n: (n.lineno, n.name)):
        binds = _Bindings()
        seen = _Assertions(binds)
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
                seen.absence += [_expand(r, binds.aliases)
                                 for r in dropped.roots]
                instrumentation_only = True
        escape_lines = {fn.lineno} | seen.assertion_lines
        if any(line in comments for line in escape_lines):
            continue
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
            uncovered = [roots for roots in seen.absence
                         if not roots or roots.isdisjoint(seen.positive)]
            if not uncovered:
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
            findings += _analyze_source(source, rel, ranges)
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
