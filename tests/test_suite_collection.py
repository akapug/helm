"""Every test in tests/ must actually be COLLECTED by the gate.

The gate mints its receipt from `python -m unittest discover` (helm/gate.py's
SUITE), and unittest collects `unittest.TestCase` subclasses ONLY. A file
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
"""

import ast
import os
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Below this, an empty result means the scan broke, not that the tree is clean.
# Absence of a match is only evidence when a hit was possible.
_MIN_FILES = 50


def collectible_test_files(root):
    """Every test module `discover` would collect, INCLUDING in subpackages.

    `unittest discover` DESCENDS into subdirectories, so a flat os.listdir
    census is blind to precisely the files a future refactor would add — it
    would keep reporting a clean tree while a whole subpackage went
    uncollected. There are no test subdirectories today, which is exactly why
    this is worth closing now: the blind spot is invisible until the day
    someone makes one, and on that day the census still says green.

    Found by @ds4pro reviewing this file, who proposed documenting the limit.
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


def bare_test_functions(source):
    """Module-level `def test*` names — the ones unittest discover never sees.

    A function nested inside a TestCase is a METHOD and runs fine; only
    module-level ones are invisible, so this walks the top level exactly.
    Parsed with ast rather than matched with a regex on purpose: `^def test`
    also matches inside a triple-quoted string, and this file is a census whose
    whole value is that its answer can be trusted.
    """
    tree = ast.parse(source)
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")]


class SuiteCollectionTest(unittest.TestCase):
    def test_the_detector_finds_a_planted_bare_function(self):
        """MUST-HIT control. Without this, a detector that silently returned []
        for everything would make the census below pass while proving nothing —
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

    def test_a_docstring_that_looks_like_a_def_is_not_counted(self):
        """The regex version of this census would report this file as broken."""
        decoy = '"""Example:\n\ndef test_not_real(tmp_path): ...\n"""\n'
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

    def test_every_test_file_is_collectible_by_unittest_discover(self):
        paths = collectible_test_files(TESTS_DIR)
        self.assertGreaterEqual(
            len(paths), _MIN_FILES,
            "only %d test files found under %s — the scan is broken, and an "
            "empty result from a broken scan reads exactly like a clean tree"
            % (len(paths), TESTS_DIR))

        offenders = {}
        for path in paths:
            name = os.path.relpath(path, TESTS_DIR)
            with open(path, encoding="utf-8") as f:
                source = f.read()
            try:
                bare = bare_test_functions(source)
            except SyntaxError as exc:      # a file that cannot parse cannot run
                offenders[name] = ["<unparseable: %s>" % exc]
                continue
            if bare:
                offenders[name] = bare

        self.assertEqual(
            offenders, {},
            "these test functions are module-level, so `unittest discover` "
            "never collects them and the gate receipt counts them as zero — "
            "make each one a method on a unittest.TestCase subclass: %s"
            % offenders)


if __name__ == "__main__":
    unittest.main()
