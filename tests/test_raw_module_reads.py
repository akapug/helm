#!/usr/bin/env python3
"""The one channel the load recorder cannot see: a raw `sys.modules` read.

task/3039 lane 2. A focused plan selects the test modules whose recorded run
reached a changed file (helm/gateloads.py). The recorder sees every import
the import system resolves and every repository file a test opens, but a
module fetched straight out of `sys.modules` by a key nothing can resolve
reaches code without either. So such a read is refused tree-wide, here,
unless `gateloads.DECLARED_RAW_READS` says why the recorder does not need to
see it. A self-read (`sys.modules[__name__]`), a read of a module outside the
tree and a statically named repository module stay allowed: the record's
closure carries the last as an edge.

AN AUDIT (`gateaudits.RUNG_ARMS`): it reads the tree instead of importing
what it judges, so it runs in every focused round.
"""
import os
import unittest

from helm import gateloads

_TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _census():
    """-> (findings, files scanned)"""
    found, files = [], 0
    for base in ("helm", "tests"):
        for here, dirs, names in os.walk(os.path.join(_TREE, base)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(here, name)
                files += 1
                rel = os.path.relpath(path, _TREE).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                found += ["%s:%d: %s" % (rel, line, source)
                          for line, source in gateloads.raw_reads(rel, text)]
    return found, files


class RawModuleReadCensus(unittest.TestCase):
    def test_no_undeclared_raw_sys_modules_read_in_helm_or_tests(self):  # noqa: VACUOUS_ASSERTION — the plants arm below proves the same census finds each spelling, and the scan's floor is asserted here
        found, files = _census()
        self.assertGreater(files, 600)
        self.assertEqual(found, [], (
            "a raw sys.modules read the load recorder cannot see; import the "
            "module instead, or declare the read in "
            "gateloads.DECLARED_RAW_READS with why the recorder need not see "
            "it:\n  " + "\n  ".join(found)))

    def test_every_declaration_names_a_live_line(self):
        self.assertTrue(gateloads.DECLARED_RAW_READS)
        for (rel, source), why in gateloads.DECLARED_RAW_READS.items():
            with open(os.path.join(_TREE, rel), encoding="utf-8") as fh:
                lines = [line.strip() for line in fh]
            self.assertIn(source, lines, "stale declaration: %s" % rel)
            self.assertTrue(why.strip())

    def test_the_census_sees_every_spelling_of_an_unresolvable_read(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal four-spelling tuple, each asserted to yield exactly one finding
        """THE PLANTS: each must be found, or the green arm above is
        vacuous."""
        for text in ("import sys\ndef f(n):\n    return sys.modules[n]\n",
                     "import sys\ndef f(n):\n    return sys.modules.get(n)\n",
                     "import sys as S\ndef f(n):\n    return S.modules.get(n)\n",
                     "from sys import argv, modules as m\n"
                     "def f(n):\n    return m[n]\n"):
            self.assertEqual(len(gateloads.raw_reads("helm/x.py", text)), 1,
                             text)

    def test_visible_reads_are_not_refused(self):  # noqa: VACUOUS_ASSERTION — the plants arm above feeds the same function unresolvable reads and asserts each FOUND
        text = ("import sys\n"
                "SELF = sys.modules[__name__]\n"
                "WEB = sys.modules.get(__package__ + '.web')\n"
                "GATE = sys.modules['helm.gate']\n"
                "MAIN = sys.modules['__main__']\n"
                "sys.modules['helm.fake'] = SELF\n")
        self.assertEqual(gateloads.raw_reads("helm/x.py", text), [])

    def test_a_declaration_covers_only_its_own_file(self):
        (rel, source), _why = next(iter(
            gateloads.DECLARED_RAW_READS.items()))
        text = "import sys\ndef f(label, _pkg, stem):\n    %s\n" % source
        self.assertEqual(gateloads.raw_reads(rel, text), [])
        self.assertEqual(len(gateloads.raw_reads("helm/other.py", text)), 1)


if __name__ == "__main__":
    unittest.main()
