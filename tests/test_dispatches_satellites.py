#!/usr/bin/env python3
"""The six ledger satellites of task/3407 -- where a moved name resolves.

WHAT THIS FILE PROVES is the SPLIT, not the behaviour. The arms that drive the
review spiral, the approval tier, the carriage proof, the verdict
announcement, the rebind and the retraction live where they always did, in
the suites that drive `dispatches` and patch its attributes by the hundred.
This file proves the property those suites silently depend on: a name moved
out of the ledger still resolves the way it did before it moved -- declared,
defined, published, looked up at CALL time, and importable in either order.
"""
import ast
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from helm import (dispatches, dispatches_announce, dispatches_carriage,
                  dispatches_rebind, dispatches_retract, dispatches_spiral,
                  dispatches_tier)
from tests._satellite_resolution import bare_owner_globals, owner_globals

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OWNER = os.path.join(_REPO, "helm", "dispatches.py")
SATELLITES = {m.__name__.rsplit(".", 1)[-1]: m for m in (
    dispatches_spiral, dispatches_tier, dispatches_carriage,
    dispatches_announce, dispatches_rebind, dispatches_retract)}

#: THE READS THAT RUN AT IMPORT, per satellite: a default argument, or a
#: module constant built from another, naming a constant the satellite itself
#: defines. Such a read binds once in either spelling, and at that moment only
#: the satellite's own global exists. NAMED, NEVER DERIVED: a new bare read of
#: an owned name is red here until someone decides it is one of these.
IMPORT_TIME = {
    "dispatches_spiral": {"SPIRAL_UNREAD", "SPIRAL_WINDOW_H"},
    "dispatches_tier": {"TIER_TRANSIENT", "TIER_DARK", "TIER_DAMAGED",
                        "TIER_UNNAMED", "TIER_UNCLASSIFIED", "TIER_PRE_TIER",
                        "_LIVE_APPROVAL_FAMILIES"},
    "dispatches_carriage": set(),
    "dispatches_announce": set(),
    "dispatches_rebind": {"_PARENT_REQUIRED"},
    "dispatches_retract": set(),
}

#: How many names the six declarations hand over, measured at the cut. An
#: absence below proves nothing about an empty declaration, so each arm first
#: asserts the population it searches is this size.
DECLARED = {"dispatches_spiral": 21, "dispatches_tier": 30,
            "dispatches_carriage": 11, "dispatches_announce": 34,
            "dispatches_rebind": 13, "dispatches_retract": 15}

_POPULATED = ("the set under test is not the size measured at the cut, so an "
              "absence proves nothing here -- check _OWNER_NAMES")


class Planted(Exception):
    """Raised by a planted double, so reaching it is the observable."""


def _declared(name):
    return dict(dispatches._OWNER_NAMES).get(name, ())


def _path(name):
    return os.path.join(_REPO, "helm", name + ".py")


def _tree(path):
    with io.open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _top_level_bindings(tree):
    """Every name a module binds at column zero, imports excluded."""
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                out |= {s.id for s in ast.walk(target)
                        if isinstance(s, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            out.add(node.target.id)
    return out


def _import_time_reads(tree):
    """{(lineno, name)} for every Name read evaluated when the module body
    runs: module statements, decorators, defaults and class bodies -- never a
    function body, which runs at call time."""
    out = set()

    def expr(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                out.add((sub.lineno, sub.id))

    def stmt(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list + node.args.defaults + [
                    k for k in node.args.kw_defaults if k is not None]:
                expr(d)
        elif isinstance(node, ast.ClassDef):
            for d in node.decorator_list + node.bases + [
                    k.value for k in node.keywords]:
                expr(d)
            for child in node.body:
                stmt(child)
        else:
            expr(node)
    for node in tree.body:
        stmt(node)
    return out


def _ledger_owned():
    """Every name a read inside a satellite must spell through the ledger."""
    with io.open(_OWNER, encoding="utf-8") as fh:
        owned = owner_globals(fh.read())
    owned |= {n for _m, names in dispatches._OWNER_NAMES for n in names}
    return owned - {"dispatches"}


def _bare_reads(path, own, owned):
    """(ledger reads spelled bare, own reads spelled bare at CALL time)."""
    hits = bare_owner_globals(path, owned)
    at_import = _import_time_reads(_tree(path))
    ledger = [h for h in hits if h[1] not in own]
    call_time = [h for h in hits if h[1] in own and h not in at_import]
    return ledger, call_time, [h for h in hits if h[1] in own]


class TheDeclarationTheDefinitionAndTheBindingAreOneThingTest(
        unittest.TestCase):

    def test_every_declared_name_is_DEFINED_here_and_nothing_else_is(self):  # noqa: VACUOUS_ASSERTION — each satellite's declared population is asserted to its measured size (DECLARED) before either set difference is read as empty
        """Both directions. The retired-name rung clears a moved name only
        when the satellite DEFINES it at column zero, so a declaration cannot
        vouch for a name nobody wrote; and a binding the satellite makes but
        never declares is a name `dispatches.NAME` no longer answers for."""
        self.assertEqual(set(SATELLITES), set(DECLARED))
        for name in SATELLITES:
            declared = _declared(name)
            self.assertEqual(DECLARED[name], len(declared), _POPULATED)
            defined = _top_level_bindings(_tree(_path(name)))
            self.assertEqual(sorted(set(declared) - defined), [],
                             "%s: declared but not defined" % name)
            self.assertEqual(sorted(defined - set(declared)
                                    - {"owned", "_publish"}), [],
                             "%s: defined but not declared" % name)

    def test_the_ledger_keeps_no_copy_of_a_moved_name(self):  # noqa: VACUOUS_ASSERTION — the moved population is asserted to its measured size and the ledger's own `snapshot` must be found before the intersection is read as empty
        """A moved name still DEFINED in the ledger text would be a stale
        twin: the tail binding overwrites it, and a reader of the file would
        be reading code that never runs."""
        remaining = _top_level_bindings(_tree(_OWNER))
        moved = {n for name in SATELLITES for n in _declared(name)}
        self.assertEqual(sum(DECLARED.values()), len(moved), _POPULATED)
        self.assertEqual(sorted(moved & remaining), [])
        self.assertIn("snapshot", remaining,
                      "the ledger's own definitions were not read")

    def test_every_declared_name_is_PUBLISHED_as_this_modules_object(self):  # noqa: VACUOUS_ASSERTION — the carriage_proof identity is an unconditional must-hit, and each declared population is asserted to its measured size before the published set is compared to it
        """`dispatches.NAME` keeps working because every consumer, and every
        patch in the suites, spells it that way."""
        self.assertIs(dispatches.carriage_proof,
                      dispatches_carriage.carriage_proof)
        for name, module in SATELLITES.items():
            declared = sorted(_declared(name))
            self.assertEqual(DECLARED[name], len(declared), _POPULATED)
            same = sorted(n for n in declared
                          if getattr(dispatches, n, None)
                          is getattr(module, n, None))
            self.assertEqual(declared, same,
                             "%s: not the same object on the ledger: %s"
                             % (name, sorted(set(declared) - set(same))))
            self.assertEqual(tuple(module.owned()), tuple(_declared(name)),
                             "%s: the publish loop and the declaration "
                             "disagree" % name)


class EveryLedgerNameIsLookedUpAtCallTimeTest(unittest.TestCase):

    def test_NO_LEDGER_NAME_IS_READ_AS_A_BARE_GLOBAL(self):  # noqa: VACUOUS_ASSERTION — zero bare ledger reads IS the product law; the bare own reads must EQUAL a non-empty named set on three satellites, and test_the_checker_goes_RED_on_a_planted_bare_read drives the same checker to both defects
        """THE MUST-HIT. A bare global, or a `from` import, binds once at
        import, so an arm that patches `dispatches.NAME` and then drives this
        code would reach the original and measure nothing, while every
        structural guard stayed green. The only bare reads of an owned name
        are the import-time ones named in IMPORT_TIME, and each must really
        run at import."""
        owned = _ledger_owned()
        self.assertIn("_TRUNK_REF_KEY", owned,
                      "the ledger binds this one by TUPLE UNPACKING; a "
                      "collector that misses that form misses real names")
        for name in SATELLITES:
            own = set(_declared(name))
            ledger, call_time, bare_own = _bare_reads(_path(name), own, owned)
            self.assertEqual(ledger, [], "%s reads ledger names bare" % name)
            self.assertEqual(call_time, [],
                             "%s reads its own names bare at call time" % name)
            self.assertEqual({n for _l, n in bare_own}, IMPORT_TIME[name],
                             "%s: the import-time reads changed" % name)

    def test_the_checker_goes_RED_on_a_planted_bare_read(self):
        """THE CONTROL on the same instrument: plant both defects in a copy
        of a real satellite -- a ledger name read bare, and an owned name
        read bare inside a function -- and require both to be found."""
        with io.open(_path("dispatches_retract"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("dispatches._TOKEN.fullmatch", source)
        self.assertIn("dispatches._RETRACT_PROOF_V", source)
        planted = source.replace("dispatches._TOKEN.fullmatch",
                                 "_TOKEN.fullmatch", 1)
        planted = planted.replace("dispatches._RETRACT_PROOF_V",
                                  "_RETRACT_PROOF_V", 1)
        handle, path = tempfile.mkstemp(suffix=".py")
        try:
            with io.open(handle, "w", encoding="utf-8") as out:
                out.write(planted)
            ledger, call_time, _bare = _bare_reads(
                path, set(_declared("dispatches_retract")), _ledger_owned())
        finally:
            os.unlink(path)
        self.assertEqual([n for _l, n in ledger], ["_TOKEN"])
        self.assertEqual([n for _l, n in call_time], ["_RETRACT_PROOF_V"])


class APatchOnTheLedgerReachesTheMovedCodeTest(unittest.TestCase):
    """THE RUNTIME HALF: one arm per satellite patches an attribute ON THE
    LEDGER, the way the consumer suites do, and drives the moved code."""

    def test_spiral(self):
        obs = {"lane": [{"status": "open", "patch_tip": "ABC"}]}
        self.assertEqual(dispatches._patch_tips_of(obs), set())
        with mock.patch.object(dispatches, "_is_fix", lambda row: True):
            self.assertEqual(dispatches._patch_tips_of(obs), {"abc"})

    def test_tier(self):
        text = types.SimpleNamespace(kind="planted-kind")
        self.assertIsNone(dispatches._kind_of(text))
        with mock.patch.object(dispatches, "TIER_UNKNOWN_KINDS",
                               ("planted-kind",)):
            self.assertEqual(dispatches._kind_of(text), "planted-kind")

    def test_carriage(self):
        with mock.patch.object(dispatches, "_CarrierView",
                               mock.Mock(side_effect=Planted)) as view:
            with self.assertRaises(Planted):
                dispatches._work_tip_of({}, {})
        self.assertEqual(view.call_count, 1)

    def test_announce(self):
        with mock.patch.object(dispatches, "_has_verdict", lambda row: True), \
                mock.patch.object(dispatches, "_attest_rows",
                                  mock.Mock(side_effect=Planted)) as rows:
            with self.assertRaises(Planted):
                dispatches.attest_projections([{"id": "planted"}])
        self.assertEqual(rows.call_count, 1)

    def test_rebind(self):
        with mock.patch.object(dispatches, "_proxy_evidence",
                               lambda seat: ("planted-starved", None)):
            self.assertEqual(dispatches._recipient_evidence("seat-a"),
                             ("planted-starved", None))

    def test_retract(self):
        state = {"status": "verdict", "polarity": "approve", "id": "a" * 32}
        with mock.patch.object(dispatches, "_replay_polarity",
                               lambda polarity: None):
            why = dispatches._retract_admission_error(state)
        self.assertIn("declared no polarity", why or "")


class NeitherImportOrderDeadlocksTest(unittest.TestCase):

    def test_each_satellite_first_and_the_ledger_first(self):  # noqa: VACUOUS_ASSERTION — every run's published total is asserted to be at least the measured population, and the order count is asserted to be seven
        """Each satellite imports the ledger eagerly and the ledger imports
        each at its tail, so the module a process reaches first is a real
        question. Importing NAMES at the tail deadlocks the reverse order;
        importing the MODULE and publishing back is the cure, kept here."""
        firsts = ["helm.dispatches"] + ["helm." + n for n in sorted(SATELLITES)]
        for first in firsts:
            out = subprocess.run(
                [sys.executable, "-c",
                 "import importlib;importlib.import_module(%r);"
                 "from helm import dispatches;"
                 "bad=[(m,n) for m,names in dispatches._OWNER_NAMES "
                 "for n in names if getattr(dispatches,n,None) is not "
                 "getattr(importlib.import_module('helm.'+m),n,0)];"
                 "print(len(bad), sum(len(v) for _m,v in "
                 "dispatches._OWNER_NAMES))" % first],
                capture_output=True, text=True, cwd=_REPO, timeout=120)
            self.assertEqual(out.returncode, 0, "importing %s first failed:\n%s"
                             % (first, out.stderr[-900:]))
            unbound, total = map(int, out.stdout.split())
            self.assertEqual(unbound, 0, "importing %s first left names "
                             "unpublished" % first)
            self.assertGreaterEqual(total, sum(DECLARED.values()), _POPULATED)
        self.assertEqual(len(firsts), 7, "every import order was exercised")


if __name__ == "__main__":
    unittest.main()
