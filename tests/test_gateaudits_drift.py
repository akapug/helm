#!/usr/bin/env python3
"""The audit list holds itself to the tree, and to a budget.

`helm/gateaudits.py` names the test modules every lane runs before a gate. The
list is kept by hand, because the gate reads it from the tree under selection
by parsing literals and never running code. A hand list drifts two ways:

  * a lane adds a test module that walks the tree and imports nothing of what
    it judges, nothing tells the lane to list it, and its first red is at a
    train, on another lane's change;
  * a listed module stops reading the tree, and every lane keeps paying its
    runtime for nothing.

`TheListIsTheScans` holds the list against `gateaudits.scan` in both
directions: every tree reader is listed or exempt with a reason, and every
listed module is either seen reading the tree or declared with what it reads.
`TheListFitsItsBudget` holds the list's measured cost to `BUDGET_SHARE` of the
whole suite's module-seconds and every listed module over `SLOW_SECONDS` to a
reason. Each live arm has a planted twin that must fail it and a control that
must pass it, so a green arm is not a blind one.

AN AUDIT (`gateaudits.RUNG_ARMS`): a lane adding a tree-walking test module
imports nothing of this one, so only the list runs it before a gate.
"""
import os
import shutil
import tempfile
import textwrap
import unittest

from helm import gateaudits

_TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.join(_TREE, "tests")

#: What `scan` must FIND, one spelling each: the idioms the tree's readers
#: actually use, plus the derivations that carry a root from `__file__` to the
#: call (a chain of assignments, a parameter default, a walrus, a loop target).
_MUST_HIT = {
    "test_hit_walk": """
        import os
        ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        def test_x():
            for d, _s, f in os.walk(os.path.join(ROOT, "helm")):
                pass
        """,
    "test_hit_chain": """
        import os
        HERE = os.path.dirname(os.path.abspath(__file__))
        PKG = os.path.join(os.path.dirname(HERE), "helm")
        def test_x():
            names = sorted(os.listdir(PKG))
        """,
    "test_hit_glob": """
        import glob, os
        def test_x():
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            paths = glob.glob(os.path.join(here, "helm", "web_*.py"))
        """,
    "test_hit_path": """
        import pathlib
        TOP = pathlib.Path(__file__).resolve().parent.parent
        def test_x():
            found = list(TOP.rglob("*.py"))
        """,
    "test_hit_default": """
        import os
        def census(root=os.path.dirname(os.path.dirname(__file__))):
            return os.listdir(root)
        """,
    "test_hit_walrus": """
        import os
        def test_x():
            if (d := os.path.dirname(os.path.abspath(__file__))):
                os.listdir(d)
        """,
    "test_hit_module_file": """
        import os
        from helm import seats
        def test_x():
            os.listdir(os.path.dirname(seats.__file__))
        """,
    "test_hit_git_ls_files": """
        import subprocess
        def test_x():
            subprocess.run(["git", "ls-files", "-z"], capture_output=True)
        """,
    "test_hit_git_grep_rooted": """
        import os, subprocess
        ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        def test_x():
            subprocess.run(["git", "grep", "-l", "-F"] + ["x"] + ["--", "."],
                           cwd=ROOT, capture_output=True)
        """,
}

#: What `scan` must MISS: a walk of a root this module made, an AST walk, a
#: local rebinding that shadows the derived root, a git read of one named file
#: or of another repository, and `__file__` only where it binds nothing.
_MUST_MISS = {
    "test_miss_ast_walk": """
        import ast, os
        ROOT = os.path.dirname(os.path.abspath(__file__))
        def test_x(tree):
            return list(ast.walk(tree))
        """,
    "test_miss_tmp": """
        import os, sys, tempfile
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        def test_x():
            d = tempfile.mkdtemp()
            return os.listdir(d)
        """,
    "test_miss_shadowed": """
        import os, tempfile
        ROOT = os.path.dirname(os.path.abspath(__file__))
        def test_x():
            ROOT = tempfile.mkdtemp()
            return os.listdir(ROOT)
        """,
    "test_miss_named_file": """
        import subprocess
        def test_x():
            subprocess.run(["git", "ls-files", "--", "helm/gate.py"])
            subprocess.run(["git", "ls-files", "--error-unmatch", "x"])
        """,
    "test_miss_other_repo": """
        import os, subprocess, tempfile
        ROOT = os.path.dirname(os.path.abspath(__file__))
        def test_x():
            repo = tempfile.mkdtemp()
            subprocess.run(["git", "grep", "x"], cwd=repo)
        """,
    "test_miss_file_in_prose": """
        import os
        TEXT = "reads nothing through __file__"
        def test_x(d):
            return os.listdir(d)
        """,
}


def _plant(case, sources):
    """A tests directory holding exactly `sources` -> its path."""
    root = tempfile.mkdtemp(prefix="helm-test-gateaudits-drift-")
    case.addCleanup(shutil.rmtree, root, ignore_errors=True)
    for name, text in sources.items():
        with open(os.path.join(root, name + ".py"), "w",
                  encoding="utf-8") as fh:
            fh.write(textwrap.dedent(text))
    return root


def _hold(case, found):
    """THE DRIFT ARM, over a scan's findings: the live arm below and the
    planted arm that must fail it both call exactly this."""
    case.assertGreater(len(found), 20, "the scan found almost no tree reader")
    gaps = gateaudits.drift(found)
    case.assertEqual(gaps, [], "the audit list and the scan disagree:\n  "
                     + "\n  ".join(gaps))


def _judge(case, seconds, why=None, **tables):
    """THE BUDGET ARM, over measured per-module seconds. Unknown is never a
    pass: no timings, or timings measuring none of the listed modules, skip as
    UNKNOWN; a breach among the measured modules fails whatever else is
    unknown. -> the verdict."""
    if not seconds:
        raise unittest.SkipTest("UNKNOWN: no measured module seconds (%s)"
                                % why)
    verdict = gateaudits.budget(seconds, **tables)
    listed = tables.get("audits", gateaudits.AUDITS)
    if len(verdict["unmeasured"]) == len(listed):
        raise unittest.SkipTest("UNKNOWN: the timings measure none of the %d "
                                "listed audits" % len(listed))
    case.assertEqual(verdict["over"], [], "the audit list is over budget:\n  "
                     + "\n  ".join(verdict["over"]))
    return verdict


class TheListIsTheScans(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.found = gateaudits.scan(_TESTS)

    def test_every_tree_reader_is_listed_and_every_listed_module_is_seen(self):  # noqa: VACUOUS_ASSERTION — the floor and the equality live in _hold, and test_a_planted_unlisted_reader_fails_the_arm drives the same _hold red
        _hold(self, self.found)

    def test_a_planted_unlisted_reader_fails_the_arm(self):
        """The live findings plus ONE planted reader, through the same arm."""
        planted = gateaudits.scan(_plant(self, {
            "test_planted_reader": _MUST_HIT["test_hit_walk"]}))
        self.assertEqual(sorted(planted), ["test_planted_reader"])
        with self.assertRaises(AssertionError) as caught:
            _hold(self, dict(self.found, **planted))
        self.assertIn("UNLISTED test_planted_reader reads the tree (walk:5)",
                      str(caught.exception))

    def test_a_planted_reader_with_an_exemption_passes_the_drift(self):
        """CONTROL: the same planted reader, exempt with a reason, is in step,
        and an exemption with no reason is not."""
        found = {"test_planted_reader": ["walk:5"]}
        self.assertEqual(gateaudits.drift(
            found, audits=(), declared={},
            exempt={"test_planted_reader": "reads only its own fixture"}), [])
        self.assertEqual(gateaudits.drift(
            found, audits=(), declared={},
            exempt={"test_planted_reader": " "}),
            ["EXEMPT gives test_planted_reader no reason"])

    def test_a_listed_module_the_scan_cannot_see_must_be_declared(self):
        self.assertEqual(
            gateaudits.drift({}, audits=("test_reads_a_doc",), declared={},
                             exempt={}),
            ["UNSEEN test_reads_a_doc is listed, the scan finds no tree read "
             "in it and DECLARED does not say what it reads"])
        self.assertEqual(gateaudits.drift(
            {}, audits=("test_reads_a_doc",),
            declared={"test_reads_a_doc": "reads docs/VERBS.md by name"},
            exempt={}), [])

    def test_a_stale_declaration_or_exemption_is_named(self):
        found = {"test_seen": ["walk:3"]}
        self.assertEqual(gateaudits.drift(
            found, audits=("test_seen",), declared={"test_seen": "why",
                                                    "test_gone": "why"},
            exempt={"test_seen": "why", "test_quiet": "why"}),
            ["DECLARED names test_gone, which no list carries",
             "DECLARED names test_seen, which the scan now sees (walk:3): "
             "drop the declaration",
             "EXEMPT names test_seen, which a list carries",
             "EXEMPT names test_quiet, which the scan no longer sees reading "
             "the tree: drop the exemption"])


class TheScanSeesWhatItClaims(unittest.TestCase):

    def test_every_must_hit_spelling_is_found(self):  # noqa: VACUOUS_ASSERTION — the equality is against the sorted keys of a non-empty literal dict, so an empty scan fails it
        found = gateaudits.scan(_plant(self, _MUST_HIT))
        self.assertEqual(sorted(found), sorted(_MUST_HIT))

    def test_no_must_miss_spelling_is_found(self):
        """CONTROL for the arm above: the same directory shape, the same
        scan, and nothing found. Paired so neither passes by the planting."""
        planted = _plant(self, dict(_MUST_MISS, test_hit_walk=_MUST_HIT[
            "test_hit_walk"]))
        self.assertEqual(sorted(gateaudits.scan(planted)), ["test_hit_walk"])

    def test_the_prefilter_never_drops_a_module_the_parse_would_find(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal dict, each entry asserted positively
        """The prefilter decides which modules are parsed at all. Every
        must-hit spelling passes it, and the one line every test module opens
        with does not make a module a candidate on its own."""
        for name, text in sorted(_MUST_HIT.items()):
            self.assertTrue(gateaudits._hot(textwrap.dedent(text)), name)
        self.assertIsNone(gateaudits._hot(textwrap.dedent(
            _MUST_MISS["test_miss_tmp"])))

    def test_a_non_test_file_is_never_scanned(self):
        root = _plant(self, {"helper_walks": _MUST_HIT["test_hit_walk"],
                             "test_walks": _MUST_HIT["test_hit_walk"]})
        self.assertEqual(sorted(gateaudits.scan(root)), ["test_walks"])


class TheListFitsItsBudget(unittest.TestCase):

    def setUp(self):
        self.seconds, self.why = gateaudits.timings(_TREE)

    def test_the_measured_audits_fit_the_budget(self):  # noqa: VACUOUS_ASSERTION — the arm lives in _judge, which skips as UNKNOWN on no measurement; the planted over-budget and slow arms drive the same _judge red
        _judge(self, self.seconds, self.why)

    def test_every_listed_audit_is_measured(self):  # noqa: VACUOUS_ASSERTION — the empty unmeasured list is the finding, and the closing equality sums every listed module's measured seconds
        """The arm above judges the modules the timings measure. A module the
        list gained after the newest sliced whole suite has no measured cost,
        and that is UNKNOWN until `TIMINGS` is refreshed, never a pass."""
        if not self.seconds:
            self.skipTest("UNKNOWN: no measured module seconds (%s)"
                          % self.why)
        verdict = gateaudits.budget(self.seconds)
        if verdict["unmeasured"]:
            self.skipTest("UNKNOWN: %d listed audits have no measured seconds "
                          "in %s: %s" % (len(verdict["unmeasured"]),
                                         gateaudits.TIMINGS,
                                         ", ".join(verdict["unmeasured"])))
        self.assertEqual(verdict["listed"], sum(
            self.seconds["tests." + n] for n in gateaudits.AUDITS))

    def test_a_planted_over_budget_list_fails_the_arm(self):
        """The measured timings, and the list grown by the five slowest
        modules it does not carry: over 20% of the suite, so the same arm
        fails naming the share."""
        self.assertTrue(self.seconds, self.why)
        extra = sorted((s, m[len("tests."):]) for m, s in self.seconds.items()
                       if m[len("tests."):] not in gateaudits.AUDITS)[-5:]
        grown = gateaudits.AUDITS + tuple(name for _s, name in extra)
        slow = dict(gateaudits.SLOW, **{name: "planted" for _s, name in extra})
        with self.assertRaises(AssertionError) as caught:
            _judge(self, self.seconds, audits=grown, slow=slow)
        self.assertIn("over the 20% budget", str(caught.exception))

    def test_a_planted_slow_module_without_a_reason_fails_the_arm(self):  # noqa: VACUOUS_ASSERTION — assertRaises is unconditional, and the control's share is asserted exactly
        seconds = {"tests.test_a": 16.0, "tests.test_b": 1.0,
                   "tests.test_c": 200.0}
        with self.assertRaises(AssertionError) as caught:
            _judge(self, seconds, audits=("test_a", "test_b"), slow={})
        self.assertIn("test_a is measured at 16.0 s, over 15 s",
                      str(caught.exception))
        # CONTROL: the same timings, the reason given, pass.
        verdict = _judge(self, seconds, audits=("test_a", "test_b"),
                         slow={"test_a": "the walk is the cost"})
        self.assertAlmostEqual(verdict["share"], 17.0 / 217.0)

    def test_no_timings_is_unknown_and_never_a_pass(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple, each case asserted to raise SkipTest
        for seconds in (None, {}, {"tests.test_elsewhere": 3.0}):
            with self.assertRaises(unittest.SkipTest) as caught:
                _judge(self, seconds, "planted", audits=("test_a",), slow={})
            self.assertTrue(str(caught.exception).startswith("UNKNOWN: "),
                            seconds)

    def test_a_slow_reason_for_an_unlisted_module_is_named(self):
        verdict = gateaudits.budget({"tests.test_a": 1.0, "tests.test_z": 9.0},
                                    audits=("test_a",),
                                    slow={"test_gone": "why"})
        self.assertEqual(verdict["over"],
                         ["SLOW names test_gone, which no list carries"])


if __name__ == "__main__":
    unittest.main()
