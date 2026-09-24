#!/usr/bin/env python3
"""The property a LEDGER SATELLITE exists to preserve: CALL-TIME lookup.

WHY THIS IS SHARED RATHER THAN WRITTEN TWICE. `landreq_cli`, `landreq_close`
and `dispatches_close` are the same shape and owe the same proof, and two
copies of a proof are how two halves of one answer come to disagree about what
they check. The arms that drive each module live in its own test file; what
they consult is here.

THE DEFECT THIS CLASS EXISTS FOR IS SILENT. A name moved across a module
boundary can be spelled three ways: a `from` import, a bare global, or through
the owner module. The first two bind the object ONCE, at import, so an arm
that patches an attribute on `landreq` and then drives the moved code reaches
the ORIGINAL and measures nothing -- and every structural check stays green,
because name resolution is a property of the RUNTIME and the guards read TEXT.
That is not hypothetical: it cost eleven arms in task/2862 and, in this split,
nine module-level constants bound by tuple unpacking.
"""
import ast
import io
import os
import symtable


def owner_globals(owner_src):
    """Every top-level name `landreq` OWNS -- imports excluded, and EVERY
    Store name in an assignment target, not just a bare `Name`.

    THE TUPLE-UNPACKING ARM IS THE POINT. The ledger binds nine constants as
    `R_NONE, R_LOCAL, ... = (...)`, and a collector that only recognises simple
    targets silently omits all nine: they are then left as bare globals in the
    satellite and raise `NameError` the first time a verb runs.
    """
    tree = ast.parse(owner_src)
    owned, imported = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owned.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        owned.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            owned.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
    return owned - imported


def bare_owner_globals(path, owned):
    """[(lineno, name)] -- every owner name the satellite reads as a BARE
    global instead of through `landreq`.

    RESOLVED WITH `symtable`, NEVER BY EYE OR BY NAME MATCHING. A local
    variable is allowed to share a top-level name, and a checker that flagged
    it would either refuse a correct file or, worse, teach someone to spell a
    real global as a local to quiet it.
    """
    src = io.open(path, encoding="utf-8").read()
    stack = [symtable.symtable(src, os.path.basename(path), "exec")]
    found = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPED):
                stack.append(_child_scope(stack[-1], child) or stack[-1])
                walk(child)
                stack.pop()
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) \
                    and child.id in owned and _reads_module_global(stack, child.id):
                found.append((child.lineno, child.id))
            walk(child)

    walk(ast.parse(src))
    return found


#: Every expression and statement that opens a scope of its own. The
#: comprehensions are listed even though 3.12 INLINED them: `_child_scope`
#: answers None for an inlined one and the walk then keeps the enclosing
#: scope, which is exactly what inlining means.
_SCOPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
           ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_ANONYMOUS = {ast.ListComp: "<listcomp>", ast.SetComp: "<setcomp>",
              ast.DictComp: "<dictcomp>", ast.GeneratorExp: "<genexpr>",
              ast.Lambda: "<lambda>"}


def _child_scope(scope, node):
    """`node`'s own symtable scope under `scope`, or None when it has none.

    KEYED BY NAME, NOT BY LINE ALONE, AND THAT IS A CURE NOT A STYLE. Under
    PEP 649 every annotated def gets a sibling `__annotate__` scope reporting
    the SAME line number as the def, so a `{lineno: child}` dict keeps
    whichever came last and the walk descends into an EMPTY annotation scope.
    Every `lookup` inside the body then raises KeyError, every local reads as
    a module global, and the checker reports the whole owned set as bare.
    Measured on this tree at 3.14: the `dispatches_close` split saw eight
    plain locals -- `stable`, `declares`, `values` -- reported as ledger
    globals, which is a checker that refuses correct files.
    """
    want = getattr(node, "name", None) or _ANONYMOUS[type(node)]
    fallback = None
    for child in scope.get_children():
        if child.get_lineno() != node.lineno or child.get_type() == "annotation":
            continue
        if child.get_name() == want:
            return child
        fallback = fallback or child
    return fallback


def _reads_module_global(stack, name):
    """True when `name`, read at the innermost scope of `stack`, is the
    MODULE's global rather than someone's local.

    THE SEARCH GOES OUTWARD BECAUSE A NESTED SCOPE MAY NOT KNOW THE NAME AT
    ALL. A generator expression evaluates its OUTERMOST iterable in the
    ENCLOSING scope, so `sorted(k for k, _p in declares)` puts `declares`
    nowhere in the genexpr's own table; a `lookup` that raises there and
    defaults to "global" accuses the enclosing function's local. Absent is
    not an answer -- it is a reason to ask the next scope out. Only running
    off the top means the name really is the module's.
    """
    for scope in reversed(stack):
        try:
            symbol = scope.lookup(name)
        except KeyError:
            continue
        if symbol.is_global():
            return True
        if symbol.is_local() or symbol.is_free():
            return False
    return True


def ledger_sources(module):
    """[(path, source)] for `module` AND every satellite it declares.

    A TREE-WIDE AUDIT THAT OPENS `module.__file__` MODELS A FILE, NOT A
    MODULE'S SURFACE, and a size ceiling forces exactly the move that breaks
    that assumption. Two such audits went red when `landreq` was split -- one
    looked for `_close_ladder_resolved` and one for `_cmd_compose` -- and
    neither was wrong about its question: the functions were still reachable,
    still called, still `landreq.NAME`; they had simply stopped being TEXT in
    that one file.

    RELOCATION IS NOT THE PROPERTY, REACHABILITY IS. So this follows the same
    `_OWNER_NAMES` declaration the retired-name rung reads, which means an
    audit written against it keeps working across the NEXT split without
    anybody remembering to come back. A module that declares nothing yields
    just itself, so this is safe to use unconditionally.
    """
    import importlib
    import os

    paths = [module.__file__]
    for satellite, _names in getattr(module, "_OWNER_NAMES", ()):
        package = module.__name__.rsplit(".", 1)[0]
        paths.append(importlib.import_module(
            "%s.%s" % (package, satellite)).__file__)
    out = []
    for path in paths:
        if path and os.path.exists(path):
            out.append((path, io.open(path, encoding="utf-8").read()))
    return out
