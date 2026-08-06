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
import unittest

_TESTS = os.path.dirname(os.path.abspath(__file__))

# Module-level process-wide defaults, set ON PURPOSE and documented at their
# site. These are the harness declaring the world every test runs in, not a
# suite forgetting to clean up — restoring them would defeat their point.
# Additions here need a reason in the same breath.
INTENTIONAL_GLOBAL = {
    "tests/__init__.py": {"HELM_METAHARNESS"},
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
    return out


class EnvHygieneTest(unittest.TestCase):
    def _scan(self):
        """[(rel, {key: line})] for every test module that leaks."""
        leaks, scanned = [], 0
        for name in sorted(os.listdir(_TESTS)):
            if not name.endswith(".py"):
                continue
            rel = "tests/" + name
            with open(os.path.join(_TESTS, name), encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel)
            scanned += 1
            missing = {k: ln for k, ln in assignments(tree).items()
                       if k not in restorations(tree)
                       and k not in INTENTIONAL_GLOBAL.get(rel, ())}
            if missing:
                leaks.append((rel, missing))
        return leaks, scanned

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
               "mock.patch.dict(os.environ, {'D': '4'})\n")
        tree = ast.parse(src)
        covered = restorations(tree)
        self.assertEqual(sorted(covered & {"A", "B", "C", "D"}),
                         ["A", "B", "C", "D"],
                         "a restoration form in live use reads as a leak: "
                         "A=tuple, B=del, C=pop, D=patch.dict")


if __name__ == "__main__":
    unittest.main()
