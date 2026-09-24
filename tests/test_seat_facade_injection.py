"""A LEXICAL census of the seat facade family. NOT a runtime claim.

helm/seat.py is a FACADE over twelve impl modules and names travel BOTH ways:
`seat_compat.EXPORTS` gathers impl definitions onto the facade, and
`_seed_impl_modules` copies the facade's whole namespace back into every impl
module. So an impl module may USE a name it neither defines nor imports. That
is the design; what was missing is the rung that keeps it true, because a name
no one supplies fails as a NameError at CALL time on whichever branch first
reaches it — no ImportError at startup, no red arm.

WHY THE ANALYSIS IS SHAPED THIS WAY. Four properties are load-bearing and each
one is a way a census like this reads GREEN while measuring nothing:

  · SCOPE. Collecting bindings across all lexical scopes and subtracting them
    module-wide lets a local in one function mask a genuinely free name in
    another, and lets a bare `global X` hide its unresolved load. That
    under-reports, which is the one direction that makes a zero meaningless.
  · SUPPLY. The union of every impl module's definitions is the WRONG supply
    set: a name an impl module defines and the facade never exports does not
    reach its siblings.
  · MODULE-LEVEL BINDING. Supply is what the source BINDS at module scope,
    read lexically. An annotation-only statement or a false branch counts
    here, because this census is lexical — and that is exactly why its
    result is a claim about SOURCE and not about runtime; the limits section
    below states the consequence rather than pretending otherwise.
  · CONTROLS. A control that plants its fixture AFTER the free-name derivation
    cannot reach the analyzer at all, so it certifies whatever the analyzer
    says.

SO THE ANALYZER IS `symtable`, NOT AN AST WALK, and it is the same instrument
tests/test_seats_split_contract.py already uses in `_undefined_globals` for the
sibling family — scope-aware by construction, folds `global X; X = ...`
correctly, and treats a SyntaxError as a FINDING rather than a skip, because a
scanner that silently declines to look has its green read as coverage.

AND SUPPLY IS TWO SOURCES, NAMED FOR WHAT THEY ARE: the names seat.py binds at
module level, read lexically from its source, UNION the observed key set of
`seat_compat.EXPORTS`. The first is static; the SECOND IS NOT — it is read from
the live import, because EXPORTS is assembled at import time and there is no
purely textual reading of it. That is the trade and it is a limit of this
instrument, not a detail. What is deliberately NOT used is the post-import
namespace of the impl modules themselves: everything injected is present there
precisely because the import succeeded, so reading it would feed this guard its
own answer.

WHAT THIS FILE DOES AND DOES NOT CLAIM, and the distinction is the whole
point. IT ASSERTS: every global an impl module REFERENCES is DEFINED
somewhere in the family. That is a statement about SOURCE. IT DOES NOT ASSERT
that the value EXISTS AT THE MOMENT OF USE, which is a statement about RUNTIME
and is not derivable from this analysis — an annotation-only statement, a false
branch, an uncalled global assignment, a deletion before the seed and a binding
after it all satisfy the census here while none of them guarantees the value
is there when a call reaches it. So this file does not say "no latent
NameError". Runtime availability is outside this census's scope.

FURTHER LIMITS, stated rather than implied. A name bound by `globals()[...] =
...`, by an exec, or by an import performed inside a function that has not run
is invisible here in BOTH directions — the facade's own injection is exactly
such a dynamic write, which is why supply is derived from its two SOURCES
rather than from the effect. Only ONE of those is static: seat.py's lexical
module-level bindings. The other is the OBSERVED key set of
`seat_compat.EXPORTS`, read from the live import because EXPORTS is assembled
at import time and has no purely textual reading. That is the trade, and
calling both of them static would misstate it.
"""
import ast
import builtins
import io
import os
import shutil
import symtable
import tempfile
import unittest

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import seat_compat  # noqa: E402

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_HELM = _os.path.join(_ROOT, "helm")

# Module dunders are supplied by the interpreter, not by the facade, and are
# named here rather than pattern-matched on a leading underscore so that a
# genuinely missing private name can never slip through as "looks like a
# dunder".
_INTERPRETER = frozenset(("__file__", "__name__", "__doc__", "__package__",
                          "__spec__", "__loader__", "__builtins__",
                          "__debug__"))
_KNOWN = frozenset(dir(builtins)) | _INTERPRETER
_FACADE = frozenset(("seat", "seat_compat"))


def _module_bound(top):
    """Names bound at MODULE scope, including `global X; X = ...` anywhere."""
    out = {s.get_name() for s in top.get_symbols()
           if s.is_local() or s.is_imported()}

    def declared_assigned(table):
        for s in table.get_symbols():
            if s.is_declared_global() and s.is_assigned():
                out.add(s.get_name())
        for child in table.get_children():
            declared_assigned(child)

    declared_assigned(top)
    return out


def _undefined_globals(src, filename):
    """{name: scope} for every load symtable resolves to a module global that
    the module does not bind and the interpreter does not supply."""
    top = symtable.symtable(src, filename, "exec")
    known = _module_bound(top) | _KNOWN
    bad = {}

    def walk(table):
        for s in table.get_symbols():
            if (s.is_referenced() and s.is_global()
                    and s.get_name() not in known):
                bad.setdefault(s.get_name(), table.get_name())
        for child in table.get_children():
            walk(child)

    walk(top)
    return bad


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


class FacadeInjectionTest(unittest.TestCase):

    @staticmethod
    def _impl():
        return sorted(m.__name__.split(".")[-1]
                      for m in seat_compat.IMPL_MODULES)

    @staticmethod
    def _supplied(seat_src):
        """The two supply sources: seat.py's LEXICAL module-level bindings,
        union the OBSERVED key set of seat_compat.EXPORTS.

        NOT the union of impl definitions — a name an impl module defines but
        the facade never exports does not reach its siblings. And not the
        post-import namespace of an impl module, which contains the answer.
        The EXPORTS half is observed rather than parsed; see the module
        docstring for why that is a stated limit.
        """
        top = symtable.symtable(seat_src, "seat.py", "exec")
        return _module_bound(top) | set(seat_compat.EXPORTS)

    def _survey(self, sources=None):
        """{module: {unsupplied name: scope}} over the twelve impl modules.

        `sources` overrides individual module texts so a control can plant a
        fixture and drive THE WHOLE DERIVATION, including the analyzer. A
        control that plants AFTER the free-name step cannot reach the analyzer
        at all, so it certifies whatever the analyzer says.
        """
        sources = dict(sources or {})
        seat_src = sources.get("seat", _read(_os.path.join(_HELM, "seat.py")))
        supplied = self._supplied(seat_src)
        out = {}
        for name in self._impl():
            src = sources.get(
                name, _read(_os.path.join(_HELM, name + ".py")))
            free = _undefined_globals(src, name + ".py")
            gap = {n: scope for n, scope in free.items() if n not in supplied}
            if gap:
                out[name] = gap
        return out

    def test_the_analyzer_is_scope_aware_and_the_supply_is_the_facades(self):
        """MUST-HIT ON BOTH HALVES OF THE INSTRUMENT before anything trusts it.

        The two ways such a census reads green while measuring nothing are an
        analyzer that cannot see scope and a supply set that is not the
        facade's. Each gets a positive here that fails if that half regresses.
        """
        # SCOPE: a local in one function must NOT mask a free global in
        # another. An AST walk that collects bindings across every scope and
        # subtracts them module-wide returns {} for this source.
        scoped = ("def a():\n"
                  "    helper = 1\n"
                  "    return helper\n"
                  "def b():\n"
                  "    return helper\n")
        self.assertEqual(sorted(_undefined_globals(scoped, "x.py")), ["helper"])
        # ...and a bare `global X` declaration alone does not bind it.
        declared = "def a():\n    global X\n    return X\n"
        self.assertEqual(sorted(_undefined_globals(declared, "x.py")), ["X"])
        # ...while `global X; X = ...` anywhere DOES.
        assigned = "def a():\n    global X\n    X = 1\ndef b():\n    return X\n"
        self.assertEqual(_undefined_globals(assigned, "x.py"), {})

        # SUPPLY: seat.py's own bindings and EXPORTS, and NOT the union of impl
        # definitions. _proxy_binary_probe is defined in seat_proxy and is NOT
        # exported, so a union-of-definitions supply would certify it as
        # reaching every sibling when it reaches none of them.
        supplied = self._supplied(_read(_os.path.join(_HELM, "seat.py")))
        self.assertGreater(len(supplied), 200)
        self.assertIn("_register_spawn", supplied)
        self.assertNotIn("_proxy_binary_probe", supplied)
        self.assertEqual(len(self._impl()), 12)

    def test_every_referenced_global_is_DEFINED_somewhere_in_the_family(self):
        """THE LEXICAL INVARIANT — defined somewhere, not available at use.

        Named for what it measures. "The facade SUPPLIES the name" would read
        as a runtime guarantee this analysis cannot give; see the module
        docstring.
        """
        self.assertEqual(self._survey(), {})  # noqa: VACUOUS_ASSERTION — the two CATCHES arms below are this arm's controls: they drive the SAME _survey over planted sources and require it to report, so an instrument that returned {} unconditionally fails there

    def test_the_invariant_CATCHES_a_real_deleted_import(self):
        """A REAL COUNTEREXAMPLE, planted in SOURCE and driven through the
        whole derivation rather than injected after it.

        helm/seat_health.py imports _proxy_binary_probe directly from
        seat_proxy. The facade does not export it, so deleting that import
        makes the name genuinely unresolvable at runtime — the exact shape a
        union-of-definitions supply set reads as healthy.
        """
        path = _os.path.join(_HELM, "seat_health.py")
        original = _read(path)
        line = "from .seat_proxy import _proxy_binary_probe\n"
        self.assertIn(line, original,
                      "the fixture's anchor is gone; this control is measuring "
                      "nothing until it is re-pointed")
        planted = original.replace(line, "", 1)
        self.assertNotEqual(planted, original)
        gap = self._survey({"seat_health": planted})
        self.assertEqual(sorted(gap), ["seat_health"])
        self.assertIn("_proxy_binary_probe", gap["seat_health"])
        # ...and the untouched tree is still clean, so the arm above is the
        # plant and not a pre-existing gap.
        self.assertEqual(self._survey(), {})  # noqa: VACUOUS_ASSERTION — the assertIn on the planted gap directly above is the unconditional positive; this line is the paired negative that makes it discriminating

    def test_the_invariant_CATCHES_a_name_no_module_defines(self):
        """The other direction: a name nothing anywhere binds."""
        path = _os.path.join(_HELM, "seat_paths.py")
        planted = _read(path).replace(
            "def _proxy_bin():\n",
            "def _proxy_bin():\n    _ = NOT_A_REAL_NAME\n", 1)
        gap = self._survey({"seat_paths": planted})
        self.assertEqual(sorted(gap), ["seat_paths"])
        self.assertIn("NOT_A_REAL_NAME", gap["seat_paths"])


# ---------------------------------------------------------------------------
# THE SECOND WAY IN, AND THE ONLY ONE A CALLER CONTROLS.
#
# The contract below is PURELY SYNTACTIC, and it says so because the two
# natural ways to write this check both smuggle an EXECUTION claim into a
# LEXICAL one — comparing line numbers, or arguing that a module-body import
# "runs first". Neither is derivable from source. So:
#
#   AN ACCEPTED FACADE IMPORT ANYWHERE IN THE SAME OR A SYNTACTICALLY ANCESTOR
#   SCOPE QUALIFIES, REGARDLESS OF ORDER, REACHABILITY OR EXECUTION.
#
# That sentence is the whole contract. A PASS means only that every impl import
# is lexically accompanied by an accepted facade import in its own or an
# enclosing scope. It is not a claim that the facade ran, that the name is
# bound, or that any call is safe.
# ---------------------------------------------------------------------------

#: The ACCEPTED set: exact dotted module identities whose import qualifies.
#: helm.seat only. seat_compat is the facade's IMPLEMENTATION, not a second
#: door, and listing it would be a textual convention with no property behind
#: it. A helm.seat_compat import does not qualify an implementation import;
#: only helm.seat qualifies.
_ACCEPTED = frozenset(("helm.seat",))

#: The SELF-EXEMPT set: the FACADE'S OWN two files, by repo-relative path. A
#: DIFFERENT SET FROM _ACCEPTED, and keeping them separate is the point: one
#: frozenset answering both questions makes helm/seat_compat.py — which
#: imports nine impl modules — exempt for the accidental reason that it shares
#: a NAME with the accepted set. Exempt by IDENTITY: the facade's own
#: implementation cannot import itself for protection. This carries no safety
#: claim.
_SELF_EXEMPT = frozenset(("helm/seat.py", "helm/seat_compat.py"))


def _family_files(root):
    """The impl modules' OWN files, exempt because the seed reaches all of them
    together — a sibling importing a sibling is not the class this guard is
    about, which is a module OUTSIDE the family importing INTO it.

    DERIVED FROM `__file__`, NEVER FROM A BASENAME. Skipping any file whose
    stem matches an impl module's would hand `tests/seat_paths.py` the
    exemption by NAME. Identity, not spelling."""
    out = set()
    for mod in seat_compat.IMPL_MODULES:
        path = getattr(mod, "__file__", None)
        if path:
            out.add(_os.path.relpath(_os.path.abspath(path), root)
                    .replace(_os.sep, "/"))
    return frozenset(out)

#: Scopes, per the agreed contract: module, function, async function and class
#: bodies define ancestry; if/for/while/try/with do NOT, because they are not
#: scopes. Class ancestry is this contract's convention and deliberately not
#: Python's name lookup, which does not let a method see a class-body binding.
#: Lambdas and comprehensions cannot contain an import statement, so they never
#: need to be a scope here.
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _package_of(path):
    """The dotted package a file lives in, by walking __init__.py upward.

    "" when the file is not inside a package, which makes every relative
    import in it UNRESOLVABLE rather than silently absolute."""
    parts = []
    d = _os.path.dirname(_os.path.abspath(path))
    while _os.path.isfile(_os.path.join(d, "__init__.py")):
        parts.insert(0, _os.path.basename(d))
        parent = _os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ".".join(parts)


_UNRESOLVED = object()


def _resolved_modules(node, pkg):
    """Every dotted module identity an import statement names.

    EXACT IDENTITIES, NEVER A PREFIX OR A TAIL. `helm.other.seat` is not
    `helm.seat`, and a module whose basename happens to match an impl module's
    is not that module. Returns `_UNRESOLVED` inside the set when a relative
    import cannot be resolved, so an unresolvable import can neither qualify
    as the facade nor vanish as a non-import.
    """
    out = set()
    if isinstance(node, ast.Import):
        # `import helm.seat` / `import helm.seat as s` — always absolute.
        for alias in node.names:
            out.add(alias.name)
        return out
    if not isinstance(node, ast.ImportFrom):
        return out
    if node.level:
        if not pkg:
            return {_UNRESOLVED}
        base = pkg.split(".")
        if node.level - 1 > len(base):
            return {_UNRESOLVED}
        base = base[:len(base) - (node.level - 1)]
        if not base:
            return {_UNRESOLVED}
        prefix = ".".join(base)
        module = "%s.%s" % (prefix, node.module) if node.module else prefix
    else:
        if not node.module:
            return {_UNRESOLVED}
        module = node.module
    out.add(module)
    # `from helm import seat` and `from . import seat` name SUBMODULES in
    # their alias list; `from helm.seat import x` names an attribute. Both
    # candidates are emitted and only an exact match against a known set is
    # ever acted on, so an attribute that happens to share a module's name
    # cannot be mistaken for one unless it IS that dotted path.
    for alias in node.names:
        out.add("%s.%s" % (module, alias.name))
    return out


def _scope_imports(tree, pkg):
    """[(chain, modules)] — one entry per import, with the chain of scopes
    enclosing it from the module body outward-in.

    `ast.walk` IS DELIBERATELY NOT USED. It descends through function and class
    bodies, so a scan of the module body would already see every import in the
    file — a file-wide answer to a per-scope question, and indistinguishable
    from the right one until a sibling scope is tested.
    """
    found = []

    def descend(body, chain):
        for stmt in body:
            _visit(stmt, chain)

    def _visit(node, chain):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append((chain, _resolved_modules(node, pkg)))
            return
        if isinstance(node, _SCOPE_NODES):
            # A NEW SCOPE. Decorators and default arguments are evaluated in
            # the ENCLOSING scope, so they are visited with the current chain
            # rather than the new one.
            for sub in node.decorator_list:
                _visit(sub, chain)
            descend(node.body, chain + (node,))
            return
        # Plain statement nesting — if/for/while/try/with/match — does NOT
        # leave the current scope.
        for sub in ast.iter_child_nodes(node):
            _visit(sub, chain)

    descend(tree.body, ())
    return found


def _unprotected(src, path, impl):
    """Why this source is unprotected, or None.

    An impl import in scope S is protected when an accepted facade import
    appears in S or in any scope enclosing S. Order is never consulted.
    """
    pkg = _package_of(path)
    entries = _scope_imports(ast.parse(src), pkg)
    accepting = [chain for chain, mods in entries if mods & _ACCEPTED]
    problems = []
    for chain, mods in entries:
        used = sorted(m for m in mods if m in impl)
        if _UNRESOLVED in mods and not used:
            # NON-QUALIFICATION, NOT REPORTING. An import this walker cannot
            # resolve never counts as an accepted facade import — it is
            # excluded from `accepting` above by never matching _ACCEPTED —
            # and it names no impl module either, so there is nothing here to
            # report. The contract asks that it not qualify silently; it does
            # not ask for a new refusal.
            continue
        if not used:
            continue
        if any(chain[:len(a)] == a for a in accepting):
            continue
        problems.append("imports %s with no %s import in the same or an "
                        "enclosing scope"
                        % (", ".join(used), " or ".join(sorted(_ACCEPTED))))
    if problems:
        return problems[0]
    return None


class ImplImportsNeedTheFacadeTest(unittest.TestCase):
    """The lexical co-occurrence guard, on the agreed syntactic contract."""

    @staticmethod
    def _impl():
        return {m.__name__ for m in seat_compat.IMPL_MODULES}

    @classmethod
    def _offenders(cls, root, bases):
        """{relpath: reason} plus the count of Python files ENCOUNTERED.

        Encountered, not parsed: the counter increments before the exemption
        check, so an exempt file is counted and never read. The count exists
        to prove the walk reached the tree, and it would be a wrong number for
        any other question."""
        impl = cls._impl()
        family = _family_files(root)
        out, scanned = {}, 0
        for base in bases:
            for dirpath, _dirs, files in _os.walk(_os.path.join(root, base)):
                for fn in sorted(files):
                    if not fn.endswith(".py"):
                        continue
                    path = _os.path.join(dirpath, fn)
                    rel = _os.path.relpath(path, root).replace(_os.sep, "/")
                    scanned += 1
                    if rel in _SELF_EXEMPT or rel in family:
                        continue
                    reason = _unprotected(_read(path), path, impl)
                    if reason:
                        out[rel] = reason
        return out, scanned

    # TWO FILES THIS LEXICAL CENSUS DOES NOT SPEAK FOR. helm/composers.py and
    # helm/harness.py import an impl module without the facade, and both are
    # imported by helm/seat.py at CALL time.
    #
    # THIS CARVE-OUT CLAIMS NO SAFETY. It is an IDENTITY exception — two
    # filenames — and an identity exception is what you write when you do NOT
    # have a property. A bounded analysis that leaves aliases or indirect
    # calls unresolved cannot establish runtime safety. They belong to the
    # runtime-availability scope, which also owns order semantics and the
    # deferred-body question.
    OUT_OF_SCOPE = {
        "helm/composers.py": "runtime-availability scope",
        "helm/harness.py": "runtime-availability scope",
    }

    def test_the_out_of_scope_list_is_exactly_the_two_named_files(self):
        """A carve-out that can grow silently is not a carve-out."""
        self.assertEqual(sorted(self.OUT_OF_SCOPE),
                         ["helm/composers.py", "helm/harness.py"])
        for path, why in self.OUT_OF_SCOPE.items():
            self.assertIn("scope", why,
                          "%s is carved out without naming the SCOPE that "
                          "owns it" % path)

    def test_acceptance_and_exemption_are_separate_and_by_IDENTITY(self):
        """Three sets, three questions. One frozenset answering two of them
        makes helm/seat_compat.py — nine impl imports — exempt for the
        accidental reason that it shares a name with the accepted set."""
        self.assertEqual(sorted(_ACCEPTED), ["helm.seat"])
        self.assertEqual(sorted(_SELF_EXEMPT),
                         ["helm/seat.py", "helm/seat_compat.py"])
        self.assertEqual(_ACCEPTED & _SELF_EXEMPT, frozenset())
        # The family exemption is DERIVED from each module's own __file__, so
        # a same-basename file elsewhere in the tree cannot inherit it.
        family = _family_files(_ROOT)
        self.assertEqual(len(family), 12)
        self.assertIn("helm/seat_paths.py", family)
        self.assertNotIn("tests/seat_paths.py", family)
        self.assertEqual(family & _SELF_EXEMPT, frozenset())

    def test_a_SAME_BASENAME_file_is_scanned_by_the_real_offender_walk(self):
        """SET MEMBERSHIP IS NOT THE BEHAVIOUR. A basename skip reintroduced
        inside `_offenders` would leave the set above correct and still let
        the file through, so this drives the real walk over a real tree."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        base = _os.path.join(root, "tests")
        _os.makedirs(base)
        offender = _os.path.join(base, "seat_paths.py")
        with io.open(offender, "w", encoding="utf-8") as fh:
            fh.write("from helm import seat_ports\n")
        # MUST-HIT: a plainly-named file in the same tree IS reported, so a
        # zero below would be the walk failing rather than the skip working.
        control = _os.path.join(base, "ordinary.py")
        with io.open(control, "w", encoding="utf-8") as fh:
            fh.write("from helm import seat_ports\n")
        offenders, scanned = self._offenders(root, ("tests",))
        self.assertEqual(scanned, 2)
        self.assertEqual(sorted(offenders),
                         ["tests/ordinary.py", "tests/seat_paths.py"],
                         "a file was skipped by BASENAME rather than by "
                         "identity")

    def test_nothing_imports_an_impl_module_without_the_facade(self):
        offenders, scanned = self._offenders(_ROOT, ("helm", "tests"))
        self.assertGreater(scanned, 300,
                           "the walk did not reach the tree")
        # MUST-HIT ON THE CARVE-OUT: the carved-out files must still be FOUND
        # by the scanner, or the allowlist is covering a scanner that stopped
        # looking rather than a known exposure.
        self.assertEqual(sorted(set(self.OUT_OF_SCOPE) & set(offenders)),
                         sorted(self.OUT_OF_SCOPE),
                         "the scanner no longer sees the carved-out files")
        unexpected = {p: why for p, why in offenders.items()
                      if p not in self.OUT_OF_SCOPE}
        self.assertEqual(unexpected, {},
                         "unprotected impl imports: %s" % unexpected)

    # ---- the agreed must-hits, one group per rule -------------------------

    def _reason(self, src, path="helm/probe.py"):
        return _unprotected(src, _os.path.join(_ROOT, path), self._impl())

    def test_SCOPE_a_sibling_or_child_facade_import_does_NOT_qualify(self):  # noqa: VACUOUS_ASSERTION — every assertion here is assertIsNotNone on a live _reason call, and test_THE_SCANNER_CAN_SEE proves the same reporter answers None for a protected source
        """RULE 1. `ast.walk` crossing scope boundaries makes every one of
        these read clean, which is why each is here."""
        sibling = ("def a():\n"
                   "    from helm import seat\n"
                   "def b():\n"
                   "    from helm import seat_paths\n"
                   "    return seat_paths\n")
        self.assertIsNotNone(self._reason(sibling),
                             "a facade import in a SIBLING function qualified")
        child = ("def outer():\n"
                 "    from helm import seat_paths\n"
                 "    def inner():\n"
                 "        from helm import seat\n"
                 "        return seat\n"
                 "    return seat_paths, inner\n")
        self.assertIsNotNone(self._reason(child),
                             "a facade import in a NESTED function qualified")
        class_sibling = ("class A:\n"
                         "    from helm import seat\n"
                         "class B:\n"
                         "    from helm import seat_paths\n")
        self.assertIsNotNone(self._reason(class_sibling),
                             "a facade import in a SIBLING class qualified")

    def test_SCOPE_the_same_or_an_ancestor_scope_qualifies_at_any_order(self):  # noqa: VACUOUS_ASSERTION — the paired sibling/child arm asserts the same reporter DOES report, so these silences are the ancestor rule and not a scanner that answers None to everything
        """RULE 2, including the order half: an ancestor import that appears
        LATER in the file still qualifies, because the contract is syntactic
        and says nothing about execution."""
        same = ("def b():\n"
                "    from helm import seat\n"
                "    from helm import seat_paths\n"
                "    return seat_paths\n")
        self.assertIsNone(self._reason(same))
        ancestor = ("from helm import seat\n"
                    "def b():\n"
                    "    from helm import seat_paths\n"
                    "    return seat_paths\n")
        self.assertIsNone(self._reason(ancestor))
        later = ("def b():\n"
                 "    from helm import seat_paths\n"
                 "    return seat_paths\n"
                 "from helm import seat\n")
        self.assertIsNone(self._reason(later),
                          "a LATER module-level facade import must still "
                          "qualify; order is not part of this contract")
        class_ancestor = ("from helm import seat\n"
                          "class A:\n"
                          "    from helm import seat_paths\n")
        self.assertIsNone(self._reason(class_ancestor))
        nested = ("from helm import seat\n"
                  "def outer():\n"
                  "    def inner():\n"
                  "        from helm import seat_paths\n"
                  "        return seat_paths\n"
                  "    return inner\n")
        self.assertIsNone(self._reason(nested))
        # A FUNCTION ancestor, not just the module. An implementation that
        # only ever consulted the module body would pass every case above.
        outer_fn = ("def outer():\n"
                    "    from helm import seat\n"
                    "    def inner():\n"
                    "        from helm import seat_paths\n"
                    "        return seat_paths\n"
                    "    return inner\n")
        self.assertIsNone(self._reason(outer_fn),
                          "a facade import in the ENCLOSING FUNCTION did not "
                          "reach its nested scope")
        # A CLASS ancestor, which this contract accepts by convention and
        # Python's own name lookup does not.
        class_method = ("class A:\n"
                        "    from helm import seat\n"
                        "    def m(self):\n"
                        "        from helm import seat_paths\n"
                        "        return seat_paths\n")
        self.assertIsNone(self._reason(class_method),
                          "a class-body facade import did not reach its "
                          "method")
        # ASYNC bodies are scopes on the same terms, in both directions.
        async_child = ("from helm import seat\n"
                       "async def a():\n"
                       "    from helm import seat_paths\n"
                       "    return seat_paths\n")
        self.assertIsNone(self._reason(async_child))
        async_sibling = ("async def a():\n"
                         "    from helm import seat\n"
                         "async def b():\n"
                         "    from helm import seat_paths\n"
                         "    return seat_paths\n")
        self.assertIsNotNone(self._reason(async_sibling),
                             "a facade import in a SIBLING async function "
                             "qualified")

    def test_SCOPE_a_conditional_block_is_not_a_scope(self):  # noqa: VACUOUS_ASSERTION — the third case in this arm asserts the reporter DOES report when the block sits inside a real scope, unconditionally, on the same reporter
        """if/for/while/try/with do not define ancestry, so an import inside
        one is in the scope that contains the block."""
        conditional = ("import os\n"
                       "if os.environ:\n"
                       "    from helm import seat\n"
                       "from helm import seat_paths\n")
        self.assertIsNone(self._reason(conditional))
        in_function = ("def b():\n"
                       "    try:\n"
                       "        from helm import seat\n"
                       "    except ImportError:\n"
                       "        pass\n"
                       "    from helm import seat_paths\n"
                       "    return seat_paths\n")
        self.assertIsNone(self._reason(in_function))
        # ...and the block does not carry a facade import ACROSS a real scope.
        across = ("def a():\n"
                  "    if True:\n"
                  "        from helm import seat\n"
                  "def b():\n"
                  "    from helm import seat_paths\n"
                  "    return seat_paths\n")
        self.assertIsNotNone(self._reason(across))

    def test_IDENTITY_a_prefix_or_tail_collision_does_NOT_qualify(self):  # noqa: VACUOUS_ASSERTION — the wrong-package loop asserts the reporter DOES report before the exact-package case asserts it does not, on the same reporter
        """RULE 3. Exact dotted identities. `helmet.seat` and `helm.other.seat`
        are not `helm.seat`. A prefix test spelled `startswith("helm")`
        accepts all three."""
        for wrong in ("from helmet.seat import x\n",
                      "from helm_other.seat import x\n",
                      "from helm.other.seat import x\n",
                      "import helmet.seat\n"):
            with self.subTest(facade=wrong.strip()):
                self.assertIsNotNone(
                    self._reason(wrong + "from helm import seat_paths\n"),
                    "a wrong-package facade import qualified")
        self.assertIsNone(
            self._reason("from helm.seat import _spawn_record\n"
                         "from helm import seat_paths\n"))
        # The same exactness on the IMPL side: a module that merely shares a
        # basename with an impl module is not one.
        self.assertIsNone(self._reason("from unrelated.seat_paths import x\n"),
                          "only helm's own impl modules are in scope")
        self.assertIsNone(self._reason("import unrelated_package.seat_paths\n"))

    def test_IDENTITY_relative_imports_resolve_through_the_real_package(self):  # noqa: VACUOUS_ASSERTION — the outside-helm case asserts the reporter DOES report, so the two silences above it are the resolution rule
        """`.seat` inside helm/ is helm.seat; `.seat` one package deeper is
        NOT, and `..seat` from there is."""
        self.assertIsNone(self._reason("from . import seat\n"
                                       "from . import seat_paths\n",
                                       path="helm/probe.py"))
        self.assertIsNone(self._reason("from .seat import _spawn_record\n"
                                       "from .seat_paths import _proxy_bin\n",
                                       path="helm/probe.py"))
        # A file OUTSIDE the helm package: `from . import seat` resolves to
        # tests.seat, which is not the facade, so it cannot qualify.
        self.assertIsNotNone(self._reason("from . import seat\n"
                                          "from helm import seat_paths\n",
                                          path="tests/probe.py"),
                             "a relative import outside helm qualified")

    def _package_tree(self, *parts):
        """A REAL package tree on disk, because `_package_of` walks
        `__init__.py` upward and a synthetic path cannot exercise that."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        path = root
        for part in parts:
            path = _os.path.join(path, part)
            _os.makedirs(path, exist_ok=True)
            with io.open(_os.path.join(path, "__init__.py"), "w",
                         encoding="utf-8") as fh:
                fh.write("")
        return root, _os.path.join(path, "consumer.py")

    def test_IDENTITY_a_NESTED_package_resolves_at_its_real_depth(self):
        """RULE 3 at depth. A resolver that only ever prepended the FIRST
        package component would pass every one-level fixture and fail here."""
        root, consumer = self._package_tree("helm", "sub")
        self.assertEqual(_package_of(consumer), "helm.sub")
        impl = self._impl()
        # `.seat` one package deep is helm.sub.seat — NOT the facade.
        self.assertIsNotNone(
            _unprotected("from . import seat\n"
                         "from helm import seat_paths\n", consumer, impl),
            "a one-dot import at depth resolved to the facade")
        # `..seat` from that depth IS helm.seat.
        self.assertIsNone(
            _unprotected("from .. import seat\n"
                         "from helm import seat_paths\n", consumer, impl),
            "a two-dot import at depth did not resolve to the facade")
        # ...and the dotted-module spelling of the same thing.
        self.assertIsNone(
            _unprotected("from ..seat import _spawn_record\n"
                         "from helm import seat_paths\n", consumer, impl))
        # AN EXCESSIVE LEVEL climbs past the package root and resolves to
        # nothing, so it cannot qualify.
        self.assertIsNotNone(
            _unprotected("from ... import seat\n"
                         "from helm import seat_paths\n", consumer, impl),
            "a level deeper than the package qualified")
        # THE PACKAGE `__init__.py` ITSELF is inside its own package, so a
        # one-dot import there names a sibling module of that package.
        init = _os.path.join(root, "helm", "sub", "__init__.py")
        self.assertEqual(_package_of(init), "helm.sub")
        self.assertIsNone(
            _unprotected("from .. import seat\n"
                         "from helm import seat_paths\n", init, impl))

    def test_IDENTITY_an_unresolvable_import_never_qualifies_silently(self):  # noqa: VACUOUS_ASSERTION — the arm asserts a reason IS returned; the assertEqual on the empty package is the fixture precondition it depends on
        """A relative import in a file that is not inside a package cannot be
        resolved, and an unresolved import is not the facade."""
        outside = _os.path.join(tempfile.mkdtemp(), "loose.py")
        self.addCleanup(shutil.rmtree, _os.path.dirname(outside), True)
        with io.open(outside, "w", encoding="utf-8") as fh:
            fh.write("")
        self.assertEqual(_package_of(outside), "")
        reason = _unprotected("from . import seat\n"
                              "from helm import seat_paths\n",
                              outside, self._impl())
        self.assertIsNotNone(reason,
                             "an unresolvable relative import qualified as "
                             "the facade")

    def test_THE_SCANNER_CAN_SEE_the_shapes_it_must_report(self):  # noqa: VACUOUS_ASSERTION — every assertion is assertIsNotNone on a live call; this arm IS the positive control the silence arms depend on
        """MUST-HIT on the reporter itself: a scanner that returned None for
        everything would pass every arm above that asserts silence."""
        self.assertIsNotNone(self._reason("from helm import seat_paths\n"))
        self.assertIsNotNone(
            self._reason("from helm.seat_paths import _proxy_bin\n"),
            "a dotted impl import is still an impl import")
        self.assertIsNotNone(self._reason("import helm.seat_paths\n"))


def _seed_lines(tree):
    """Line numbers of every module-level `globals().update(...)` call.

    THE SEED IS LOCATED, NEVER TYPED. The line moves whenever anything above
    it in seat.py changes, and a hard-coded number turns this whole property
    into an assertion about an old revision that keeps passing.
    """
    out = []
    for node in tree.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Attribute) and func.attr == "update" \
                and isinstance(func.value, ast.Call) \
                and isinstance(func.value.func, ast.Name) \
                and func.value.func.id == "globals":
            out.append(node.lineno)
    return out


def _eager_loads(tree):
    """(name, line) for every Name LOAD that runs while the module runs.

    WHAT COUNTS AS EAGER, and each exclusion is a behaviour rather than a
    convenience. A function or lambda BODY is skipped because it runs when it
    is CALLED, which for these modules is after import. Everything else is
    kept, and two of those are easy to miss: a CLASS BODY executes at its
    `class` statement, and decorators, default arguments and base-class
    expressions all evaluate at definition time.

    ANNOTATIONS ARE KEPT TOO, deliberately, and that is the STRICT reading.
    Measured on CPython 3.14: a module-level annotation naming an undefined
    global raises nothing, because annotations are evaluated lazily there. So
    keeping them can only OVER-report on this interpreter — and it is the
    version-independent choice, since on an interpreter that evaluates them
    eagerly the same reference is a genuine import-time failure. Dropping
    them would make this arm silently weaker on exactly the interpreters
    where the hazard is real.
    """
    out = []

    def walk(node, skip=()):
        for field, value in ast.iter_fields(node):
            if field in skip:
                continue
            for child in (value if isinstance(value, list) else [value]):
                if not isinstance(child, ast.AST):
                    continue
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    out.append((child.id, child.lineno))
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, skip=("body",))
                elif isinstance(child, ast.Lambda):
                    walk(child, skip=("body",))
                else:
                    walk(child)

    walk(tree)
    return out


class ASeededNameReadBeforeItsSeedLineTest(unittest.TestCase):
    """THE ORDER HALF OF THE RUNTIME CLAIM the census above cannot make.

    The census in this file asserts that every global an impl module
    references is DEFINED somewhere in the family. It says nothing about
    WHEN, and the facade's supply arrives at one instant: `globals().update(
    <x>_compat.EXPORTS)`. The module-level IMPORT of the compat module one
    line earlier is NECESSARY AND NOT SUFFICIENT — it binds the compat
    MODULE; the 290 names exist only after the update on the next line. And
    `_seed_impl_modules()`, twenty-odd lines further down, is a DIFFERENT
    step seeding the impl modules and must never be read as this event.

    SO THE PROPERTY IS ABOUT ORDER: no statement that executes while the
    module executes may reference a seeded name before the line that seeds
    it. Where it holds, no IMPORT-TIME use of a seeded name can fail, and
    deferred bodies are safe for the seed by construction because they run
    after import. This is the half of the runtime question that IS decidable
    from source; the other half — whether every REACHABLE used global is
    supplied — is not, because getattr, globals()[name], dispatch tables and
    decorator indirection all defeat a static walk.

    IT IS LOAD-BEARING RATHER THAN DECORATIVE: seat.py runs two of its own
    functions at import, immediately after the seed, and both read names that
    exist only because the seed already ran. Move the seed below them and
    both raise at import.

    WHAT THIS DOES NOT CLAIM, so it is never cited as more: nothing about
    names the seed never supplies at all, nothing about indirect references
    an `ast.Name` load cannot see, and nothing about whether the seed's
    CONTENT is right — only about ORDER.
    """

    @staticmethod
    def _violations(src, seeded, filename="<probe>"):
        """Seeded names read eagerly BEFORE the seed line.

        Refuses rather than answers when the seed cannot be located: a
        locator that finds nothing makes every module hold vacuously, and an
        empty violation list from a blind walker is byte-identical to an
        empty one from a clean module.
        """
        tree = ast.parse(src, filename)
        lines = _seed_lines(tree)
        if not lines:
            raise AssertionError("no globals().update seed found in %s"
                                 % filename)
        first = min(lines)
        return sorted({(n, ln) for n, ln in _eager_loads(tree)
                       if n in seeded and ln < first})

    def test_the_detector_FIRES_on_the_shapes_it_must_report(self):
        """MUST-HIT BEFORE ANY HOLD IS BELIEVED, on four shapes.

        A detector that answered "no violations" to everything would certify
        both facades and every future edit to them. The third and fourth
        cases decide whether this measures ORDER or merely counts
        occurrences: a deferred body must NOT be a violation, and a class
        body MUST be one.

        ALL FOUR READINGS LAND IN ONE LIST, and the firing one comes FIRST,
        so each silence is constrained by a report from the same binding
        rather than by a sibling's.
        """
        seeded = {"SEEDED"}
        head = "from . import x_compat as _c\n"
        seed = "globals().update(_c.EXPORTS)\n"
        found = []

        found.extend(self._violations(head + "y = SEEDED\n" + seed, seeded))
        self.assertEqual(1, len(found),
                         "a read ABOVE the seed was not reported, so every "
                         "hold below is a silence from a blind walker: %r"
                         % (found,))
        self.assertEqual("SEEDED", found[0][0],
                         "the report does not name WHICH name was read early")

        del found[:]
        found.extend(self._violations(head + seed + "y = SEEDED\n", seeded))
        self.assertEqual([], found,
                         "a read BELOW the seed was reported, so this arm "
                         "measures occurrence rather than order: %r" % (found,))

        del found[:]
        found.extend(self._violations(
            head + "def f():\n    return SEEDED\n" + seed, seeded))
        self.assertEqual([], found,
                         "a DEFERRED body was reported; a detector that "
                         "flags function bodies would 'hold' only on modules "
                         "that happen to define nothing: %r" % (found,))

        del found[:]
        found.extend(self._violations(
            head + "class K:\n    v = SEEDED\n" + seed, seeded))
        self.assertEqual(1, len(found),
                         "a CLASS BODY was not reported, and a class body "
                         "runs at its class statement exactly like any other "
                         "module-level statement: %r" % (found,))

    def test_a_MISSING_seed_REFUSES_AND_SAYS_WHY(self):
        """THE VACUOUS PATH CLOSED EXPLICITLY. If the seed spelling ever
        changes, a locator returning nothing would report zero violations for
        every module forever, and that zero is indistinguishable from a clean
        tree. The refusal is what keeps the holds below meaningful.

        THE MESSAGE IS ASSERTED, NOT ONLY THE RAISE: a refusal whose text
        does not say WHAT was not found sends the next reader hunting, and
        `assertRaises` alone would also be satisfied by an unrelated
        AssertionError from anywhere inside the helper.
        """
        try:
            self._violations("y = SEEDED\n", {"SEEDED"}, "no_seed.py")
            raised = ""
        except AssertionError as exc:
            raised = str(exc)
        self.assertIn("globals().update", raised,
                      "a source with NO seed did not refuse, or refused "
                      "without naming what it could not find: %r" % raised)
        self.assertIn("no_seed.py", raised,
                      "the refusal does not name WHICH source it was reading")
        # AND THE SAME HELPER ANSWERS NORMALLY WHEN A SEED IS PRESENT, so the
        # refusal above is about the missing seed rather than a helper that
        # raises on everything it is handed.
        self.assertEqual(
            1, len(self._violations("y = SEEDED\nglobals().update({})\n",
                                    {"SEEDED"}, "with_seed.py")),
            "the helper reports nothing even for a read above a real seed")

    def test_NO_facade_reads_a_seeded_name_above_its_own_seed(self):
        """THE PROPERTY, on both live facades, each with its own control.

        The seeded set is the OBSERVED key set of each compat module's
        EXPORTS, read from the live import because EXPORTS is assembled at
        import time and has no purely textual reading.

        AND THAT READ IS THE DESIGN HOLDING, NOT A WEAKNESS TO APOLOGISE FOR.
        The observed set and the checked source are DIFFERENT MODULES, and
        neither compat module imports the facade it supplies: importing
        helm.seat_compat leaves helm.seat absent from sys.modules, and
        helm.web_compat leaves helm.web absent -- seat_compat reaches only the
        standard library, web_compat reaches web_common, web_core,
        web_configs, web_cache and web_quota and never web itself. So a
        facade that DID read a seeded name early could not corrupt the set it
        is measured against: this arm reasons about an import it does not
        depend on, which is the property that makes an observed yardstick
        safe rather than circular.

        THE TWO FACADES ARE WRITTEN OUT RATHER THAN LOOPED, because a control
        inside a loop is a control that MAY NOT RUN: an empty sequence
        would skip every assertion here and the method would pass having
        measured nothing. Written out, each planted control is unconditional.

        EACH CONTROL IS BUILT FROM ITS OWN FACADE'S SEEDED SET and runs
        immediately before that facade's real reading, through one list. A
        control assembled from the other facade's set would prove the
        detector works on some other input.

        SEAT IS WHERE THIS IS LOAD-BEARING: two of its own functions run at
        import immediately after the seed and read names that exist only
        because the seed already ran. Move the seed below them and both raise
        at import.
        """
        import importlib
        found = []

        seeded = set(importlib.import_module("helm.seat_compat").EXPORTS)
        self.assertTrue(seeded, "seat_compat exports nothing, so the seat "
                                "check below is against an empty set")
        planted = sorted(seeded)[0]
        found.extend(self._violations(
            "y = %s\nglobals().update({})\n" % planted, seeded,
            "planted_seat.py"))
        self.assertTrue(found,
                        "the detector did not fire on a planted early read "
                        "of %r, so seat's reading below is a silence from a "
                        "dead instrument" % (planted,))
        del found[:]
        found.extend(self._violations(
            _read(_os.path.join(_HELM, "seat.py")), seeded, "seat.py"))
        self.assertEqual(
            [], found,
            "helm/seat.py reads a seeded name ABOVE globals().update, so "
            "importing it raises NameError before any test runs: %r" % (found,))

        seeded = set(importlib.import_module("helm.web_compat").EXPORTS)
        self.assertTrue(seeded, "web_compat exports nothing, so the web "
                                "check below is against an empty set")
        planted = sorted(seeded)[0]
        found.extend(self._violations(
            "y = %s\nglobals().update({})\n" % planted, seeded,
            "planted_web.py"))
        self.assertTrue(found,
                        "the detector did not fire on a planted early read "
                        "of %r, so web's reading below is a silence from a "
                        "dead instrument" % (planted,))
        del found[:]
        found.extend(self._violations(
            _read(_os.path.join(_HELM, "web.py")), seeded, "web.py"))
        self.assertEqual(
            [], found,
            "helm/web.py reads a seeded name ABOVE globals().update, so "
            "importing it raises NameError before any test runs: %r" % (found,))

    def test_the_yardstick_comes_from_a_module_that_does_not_import_the_facade(self):
        """THE PROPERTY THE ARM ABOVE RESTS ON, ASSERTED RATHER THAN CLAIMED.

        The seeded set is read from a live import, so the question a reader
        will ask is whether the module under test could corrupt its own
        yardstick. It cannot, because neither compat module imports the
        facade it supplies — and a safety that lives only in a docstring is
        deleted by the next person who finds it inconvenient.

        EACH IMPORT RUNS IN ITS OWN INTERPRETER. Deleting helm modules out of
        this process's sys.modules to measure an import would make every
        later arm in the suite run against a re-imported tree, which is a
        cross-test channel minted to answer one question.
        """
        import subprocess

        def imported(target, probe):
            out = subprocess.run(
                [_sys.executable, "-c",
                 "import sys, %s; print(%r in sys.modules)" % (target, probe)],
                cwd=_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, out.returncode,
                             "the probe interpreter failed, so its answer is "
                             "about nothing: %r" % (out.stderr[-400:],))
            return out.stdout.strip()

        # THE POSITIVE CONTROL, ON THE SAME OBSERVABLE: this probe DOES report
        # True for a module web_compat really imports, so a False below is the
        # absence of an import and not a probe that answers False to
        # everything.
        self.assertEqual("True", imported("helm.web_compat", "helm.web_common"),
                         "the probe cannot see an import that IS there, so "
                         "every False below is meaningless")
        self.assertEqual("False", imported("helm.seat_compat", "helm.seat"),
                         "helm.seat_compat pulls in helm.seat, so the seeded "
                         "set is produced by importing the very module the "
                         "arm above measures against it")
        self.assertEqual("False", imported("helm.web_compat", "helm.web"),
                         "helm.web_compat pulls in helm.web, so the seeded "
                         "set is produced by importing the very module the "
                         "arm above measures against it")


# ---------------------------------------------------------------------------
# THE RUNTIME-AVAILABILITY RUNG, FOR ONE MODULE, ON ONE MEASURED FAILURE.
#
# The census at the top of this file states its own limit plainly: it asserts
# that a referenced name is DEFINED somewhere in the family, never that the
# value EXISTS AT THE MOMENT OF USE. That gap had a live instance, and this is
# the rung for it.
#
# helm/seat_launch_assets.py referenced fifteen module globals it never bound —
# `seat_dir` among them, at five call sites — and relied entirely on the
# facade's seed to supply them. A process that imported
# helm.seat_lifecycle_sessions and called registered_seat_family, which reaches
# _instance_dir through `from .seat_launch_assets import _instance_dir`, raised
# `NameError: name 'seat_dir' is not defined`. The module imported clean; only
# the call failed.
#
# WHY NOTHING WAS RED. The function is not untested — the suite calls
# _instance_dir from a dozen files. Every one of those calls spells it
# `seat._instance_dir`, and `seat` is the one import that runs the seed. The
# defect lived on the door no arm used, so coverage of the FUNCTION said
# nothing about the DOOR, and a NameError needs only the door to be wrong.
#
# SO THE ARMS BELOW ENTER THROUGH THAT DOOR. One is lexical and covers the
# whole module, so a sixteenth unbound name cannot be added in silence; the
# other is a real interpreter that never imports the facade, because the
# lexical arm cannot speak for runtime and says so.
# ---------------------------------------------------------------------------


class SeatLaunchAssetsAnswersWithoutTheFacadeTest(unittest.TestCase):
    """helm.seat_launch_assets, entered directly rather than through seat.py."""

    MODULE = "seat_launch_assets"

    def test_the_module_binds_every_global_it_references(self):
        """LEXICAL, with the supply set deliberately reduced to this module.

        `_undefined_globals` resolves against `_module_bound(top) | _KNOWN` and
        nothing else, so this is the same analyzer the facade census uses with
        the facade's supply withheld — which is exactly the question a direct
        importer asks. Blast radius: one source file parsed; nothing runs.
        """
        src = _read(_os.path.join(_HELM, self.MODULE + ".py"))
        # THE POSITIVE CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: the
        # same analyzer, the same source, one planted free name, driven through
        # the whole derivation rather than injected after it. If this reports
        # nothing then the analyzer is dead and the emptiness below is a
        # silence, not a finding.
        planted = src.replace("def _key_base_url(fam, api_key):\n",
                              "def _key_base_url(fam, api_key):\n"
                              "    _ = NOT_A_BOUND_NAME\n", 1)
        self.assertNotEqual(planted, src,
                            "the plant's anchor is gone; this control is "
                            "measuring nothing until it is re-pointed")
        self.assertIn("NOT_A_BOUND_NAME",
                      _undefined_globals(planted, self.MODULE + ".py"))
        self.assertEqual(_undefined_globals(src, self.MODULE + ".py"), {})  # noqa: VACUOUS_ASSERTION — the assertIn on the planted source directly above is the unconditional positive control on this same analyzer and this same source

    def test_the_module_answers_in_an_interpreter_that_never_imports_seat(self):
        """RUNTIME, which the arm above cannot reach and does not claim.

        Two interpreters, one call, differing only in whether helm.seat was
        imported first. THE FACADE DOOR IS THE POSITIVE CONTROL: it is the path
        every other arm in the suite takes, so its concrete answer is the one
        the direct door has to match. Both doors are asserted on a CONCRETE
        PATH — never on the absence of an exception, which would pass for a
        probe that computed nothing.

        Each probe also reports whether helm.seat reached its sys.modules, so
        the direct arm proves it really skipped the seed instead of importing
        the facade by some transitive edge and measuring the easy case.
        """
        import subprocess

        def probe(prelude):
            out = subprocess.run(
                [_sys.executable, "-c",
                 "%simport sys\n"
                 "import helm.seat_launch_assets as assets\n"
                 "print(assets._instance_dir('codex', 'seat-under-test'))\n"
                 "print('helm.seat' in sys.modules)" % prelude],
                cwd=_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, out.returncode,
                             "the probe interpreter failed, so its answer is "
                             "about nothing: %r" % (out.stderr[-400:],))
            path, seen = out.stdout.strip().splitlines()
            return path, seen

        tail = _os.path.join("seats", "codex", "instances", "seat-under-test")
        seeded_path, seeded_seen = probe("import helm.seat\n")
        self.assertEqual(seeded_seen, "True")
        self.assertTrue(seeded_path.endswith(tail), seeded_path)

        direct_path, direct_seen = probe("")
        self.assertEqual(direct_seen, "False",
                         "the direct probe imported the facade after all, so "
                         "it measured the seeded path twice")
        self.assertTrue(direct_path.endswith(tail), direct_path)
        self.assertEqual(direct_path, seeded_path)
