"""The orphaned-mock census: a double planted on a name nothing can reach.

EVERY ARM HERE IS ABOUT A DISCRIMINATION, not about a count. A census that
reports everything and one that reports nothing both look like work; what
makes this one usable is that each exclusion below was a FALSE POSITIVE in an
earlier run against the live tree, read and understood before it was excluded.
"""
import ast
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import orphaned_mock as om                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PlantedControlsTest(unittest.TestCase):
    """The walker must find its own orphan AND stay quiet on its own live one.

    ONE CONTROL CANNOT DO THIS. A walker that reports NOTHING passes a
    findings-only check trivially, and one that reports EVERYTHING passes a
    plant-an-orphan check trivially. Both directions or neither.
    """

    def _staged(self):
        """The SAME root the rung builds, through the SAME function it calls.

        Scanning these arms against the helm checkout would make them true
        only HERE: reachability resolves an imported module to a file under
        the scanned root, so a control importing helm answers a question
        about whichever repository the commit is in (task/2248)."""
        root = tempfile.mkdtemp(prefix="planted-controls-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return om.stage_controls(root)

    def test_the_planted_ORPHAN_is_found(self):
        found, issues = om.scan_source(self._staged(), "<control>",
                                       om._CONTROL)
        self.assertEqual(issues, [])
        self.assertEqual([(t, a, g) for _l, t, a, g, _w in found],
                         [("test_planted_orphan_control",
                           om._CONTROL_MODULE, "_never_reached")])

    def test_the_planted_LIVE_fixture_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — the orphan control on the same walker is found unconditionally in the sibling arm and again on the last line here
        root = self._staged()
        found, _issues = om.scan_source(root, "<control>", om._QUIET_CONTROL)
        self.assertEqual(found, [],
                         "a double on a name the entry point DOES reach was "
                         "reported as orphaned")
        self.assertTrue(om.scan_source(root, "<control>", om._CONTROL)[0])

    def test_both_controls_are_asserted_together_by_the_runner(self):
        held, why = om._controls_hold()
        self.assertTrue(held, why)
        self.assertEqual(why, "")


class WhatItRefusesToCallAFindingTest(unittest.TestCase):
    """Each of these was a false positive in a real run before it was cured."""

    def _scan(self, body):
        return om.scan_source(ROOT, "<probe>", body)[0]

    def test_a_patched_CONSTANT_is_not_a_reachability_question(self):  # noqa: VACUOUS_ASSERTION — the same probe patching a FUNCTION is reported unconditionally on the last lines
        """Three of the first eighteen findings were module-level data. A call
        graph over function names can never contain a constant, so reporting
        one is tripping a clause that does not apply."""
        self.assertEqual(self._scan('''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "ANCESTRY_BUDGET"):
        landreq._landed_index("/g", "t")
'''), [])
        self.assertTrue(self._scan('''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_stored_patch_index"):
        landreq._landed_index("/g", "t")
'''))

    #: EVERY SHAPE, BOTH POLARITIES, and the pairs are the point: each REPORTED
    #: row has a QUIET row that differs only in the property under test, so no
    #: row can pass because the probe is inert. Every one of the twelve
    #: reported shapes below was MEASURED slipping past a token-pattern
    #: classifier before that classifier was deleted.
    FIRING_TRUTH_TABLE = (
        # (expression, must_be_reported, why)
        ("unused = (idx.call_count > 0)", True,
         "not an assertion at all: it constrains no outcome"),
        ("assert idx.call_count > 0", False,
         "the same expression AS an assertion is false at zero"),
        ("assert not (idx.call_count > 0)", True,
         "negation makes it true at zero"),
        ("x = idx.mock_calls[:]", True,
         "a slice of an empty record list is [] and raises nothing"),
        ("x = idx.call_args_list[:0]", True, "an empty slice, same"),
        ("x = idx.call_args_list[0]", False,
         "an INDEX into an empty record list raises, so the arm cannot pass"),
        ("x = idx.call_args_list.copy", True,
         "an ordinary bound method on [], no raise"),
        ("x = idx.call_args.kwargs", False,
         "call_args is None when never fired, so the attribute raises"),
        ("self.assertIsNotNone(idx.call_args_list)", True,
         "the record LIST is empty, not None"),
        ("self.assertIsNotNone(idx.mock_calls)", True, "same"),
        ("self.assertIsNotNone(idx.call_args)", False,
         "call_args IS None when never fired"),
        ("self.assertTrue(True, idx.called)", True,
         "the spy is in the MESSAGE position and decides nothing"),
        ("self.assertTrue(idx.called)", False,
         "the same attribute in the TRUTH position is False at zero"),
        ("self.assertEqual(1, 1, idx.call_args)", True, "message position"),
        ("self.assertFalse(idx.called)", True, "passes at zero"),
        ("idx.assert_has_calls([], False)", True,
         "an EMPTY expectation is satisfied by a mock with no calls"),
        ("idx.assert_has_calls(expected)", True,
         "an expectation this walker cannot read is undecidable"),
        ("idx.assert_has_calls([mock.call('/g', 't')])", False,
         "a non-empty literal expectation cannot match zero calls"),
        ("idx.assert_any_call()", False,
         "mock RAISES this on a never-called double: it names a zero-argument "
         "CALL, never an empty expectation"),
        ("idx.assert_any_call([])", False,
         "names a call carrying an empty list, and still raises"),
        ("idx.assert_called_once_with('/g', 't')", False, "raises"),
        ("idx.assert_not_called()", True, "passes at zero, by construction"),
        ("assert idx.call_count == 1", False, "false at zero"),
        ("assert idx.call_count == 0", True, "true at zero"),
        ("assert idx.call_count < 1", True, "true at zero"),
        ("assert idx.call_count != 1", True, "true at zero"),
        ("self.assertGreaterEqual(idx.call_count, 0)", True,
         "true at zero: the OPERATOR decides, not the literal"),
        ("self.assertGreater(1, idx.call_count)", True,
         "true at zero: the DIRECTION decides"),
        ("self.assertGreater(idx.call_count, 0)", False, "false at zero"),
        ("self.assertIsNone(idx.call_args)", True, "passes at zero"),
        ("self.assertEqual(idx.call_args_list, [])", True, "passes at zero"),
    )

    _SHAPE = '''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_stored_patch_index") as idx:
        landreq._landed_index("/g", "t")
    %s
'''

    def test_the_FIRING_JUDGEMENT_is_the_never_fired_valuation(self):
        """An exemption is earned by an assertion that CANNOT PASS with THIS
        double held at its never-fired values, and nothing else earns one.

        THE JUDGEMENT IS NOT MADE IN THIS MODULE. It is
        `vacuous_assertion.spy_assertion_proves_it_fired`, and the table above
        is the contract that door owes: every REPORTED row is a shape that
        passes against a double that never fired, every QUIET row is a shape
        that cannot.
        """
        reported = quiet = 0
        for expr, must_report, why in self.FIRING_TRUTH_TABLE:
            with self.subTest(expr=expr):
                found = self._scan(self._SHAPE % expr)
                self.assertEqual(bool(found), must_report,
                                 "%s -- %s" % (expr, why))
            reported += 1 if must_report else 0
            quiet += 0 if must_report else 1
        # MUST-HIT ON THE TABLE ITSELF: a table that had drifted to one
        # polarity would make every subTest above agree with a broken door.
        self.assertGreater(reported, 5, "the table lost its reported rows")
        self.assertGreater(quiet, 5, "the table lost its quiet rows")

    def test_unsupported_values_stay_warned_with_a_direct_firing_twin(self):
        shapes = (
            "assert (idx.call_count or 2) == 2",
            "assert len([idx.call_count][:]) == 1",
            "x = idx.call_args.__class__",
            "self.assertCountEqual([idx.call_count], [False])",
            "idx.assert_has_calls([*[]])",
            "idx.assert_has_calls([*expected])",
            "idx.assert_has_awaits([*[]])",
            "assert idx.call_count == 0 < 1",
            "assert idx.call_args_list is idx.call_args_list",
            "x = idx.call_args_list.copy",
            "self.assertTrue(idx.mock_awaits)",
            "other.assertTrue(idx.called)",
        )
        for statement in shapes:
            with self.subTest(statement=statement):
                source = self._SHAPE % statement
                self.assertEqual([r[3] for r in self._scan(source)],
                                 ["_stored_patch_index"])
                twin = self._SHAPE % ("idx.assert_called_once()\n    " + statement)
                self.assertEqual(self._scan(twin), [])

    def test_child_call_records_do_not_prove_the_parent_double_fired(self):
        for assertion in ("self.assertTrue(idx.mock_calls)",
                          "x = idx.mock_calls[0]",
                          "idx.assert_has_calls([mock.call.child()])"):
            with self.subTest(assertion=assertion):
                statement = "idx.child()\n    idx.assert_not_called()\n    " + assertion
                source = self._SHAPE % statement
                self.assertEqual([r[3] for r in self._scan(source)],
                                 ["_stored_patch_index"])
                self.assertEqual(self._scan(self._SHAPE % (
                    "idx.assert_has_calls([mock.call()])\n    " + statement)), [])
                # Isolate the aggregate classifier from the prior child use:
                # these readings/expectations alone still prove no root call.
                self.assertEqual([r[3] for r in self._scan(self._SHAPE % assertion)],
                                 ["_stored_patch_index"])

    def test_async_only_readings_need_type_proof_not_a_never_called_sync_mock(self):
        # _stored_patch_index is synchronous: patch.object creates MagicMock,
        # whose await_* attributes are child mocks, not AsyncMock defaults.
        for statement in ("assert idx.await_args", "assert idx.await_count",
                          "assert idx.await_args_list",
                          "self.assertIsNotNone(idx.await_args)",
                          "x = idx.await_args.args", "x = idx.await_args.kwargs",
                          "x = idx.await_args[0]", "x = idx.await_args_list[0]"):
            with self.subTest(statement=statement):
                source = self._SHAPE % statement
                self.assertEqual([r[3] for r in self._scan(source)],
                                 ["_stored_patch_index"])
                self.assertEqual(self._scan(self._SHAPE % (
                    "idx.assert_called_once()\n    " + statement)), [])

    def test_any_await_names_an_actual_await_even_with_no_or_empty_arguments(self):
        shape = self._SHAPE.replace('"_stored_patch_index")',
                                   '"_stored_patch_index", new_callable=mock.AsyncMock)')
        for assertion in ("idx.assert_any_await()", "idx.assert_any_await([])"):
            with self.subTest(assertion=assertion):
                self.assertEqual(self._scan(shape % assertion), [])
                self.assertEqual([r[3] for r in self._scan(
                    shape % "idx.assert_has_awaits([*[]])")],
                    ["_stored_patch_index"])

    def test_context_cannot_turn_an_uncalled_assertion_into_evidence(self):
        # Each negative can complete without calling idx. Its direct twin uses
        # precisely the same otherwise-reportable patched target and entry.
        shapes = (
            "def unused():\n        idx.assert_called_once()",
            "async def unused():\n        idx.assert_called_once()",
            "unused = lambda: idx.assert_called_once()",
            "False and idx.assert_called_once()",
            "True or idx.assert_called_once()",
            "assert True or idx.assert_called_once()",
            "[idx.assert_called_once() for _ in []]",
            "try:\n        idx.assert_called_once()\n    except AssertionError:\n        pass",
            "with self.assertRaises(AssertionError):\n        idx.assert_called_once()",
            "if False:\n        idx.assert_called_once()",
            "for _ in []:\n        idx.assert_called_once()",
            "return\n    idx.assert_called_once()",
            "with suppress(AssertionError):\n        idx.assert_called_once()",
            "try:\n        x = idx.call_args.kwargs\n    except AttributeError:\n        pass",
        )
        for statement in shapes:
            with self.subTest(statement=statement):
                self.assertEqual([r[3] for r in self._scan(self._SHAPE % statement)],
                                 ["_stored_patch_index"])
                self.assertEqual(self._scan(self._SHAPE % "idx.assert_called_once()"), [])

    def test_the_handle_binding_and_patch_context_are_part_of_the_proof(self):
        source = self._SHAPE % "idx.assert_called_once()"
        self.assertEqual(self._scan(source), [])
        # Removing the helper loses quietness: the graph alone cannot exempt it.
        with mock.patch.object(om, "_spy_fired", return_value=False):
            self.assertEqual([r[3] for r in self._scan(source)],
                             ["_stored_patch_index"])
        rebound = self._SHAPE % ("idx = mock.Mock()\n    idx()\n"
                                "    idx.assert_called_once()")
        self.assertEqual([r[3] for r in self._scan(rebound)],
                         ["_stored_patch_index"])
        overwritten = self._SHAPE % "idx.called = True\n    self.assertTrue(idx.called)"
        self.assertEqual([r[3] for r in self._scan(overwritten)],
                         ["_stored_patch_index"])
        # A direct assertion inside the non-suppressing patch body is supported.
        inside = self._SHAPE % "    idx.assert_called_once()"
        self.assertEqual(self._scan(inside), [])
        # A nested non-test definition cannot donate an earlier handle's proof.
        reused = self._SHAPE % (
            "def unused():\n        with mock.patch.object(landreq, "
            "'_stored_patch_index') as idx:\n            idx.assert_called_once()")
        self.assertTrue(self._scan(reused))

    def test_unknown_handle_use_or_escape_ends_default_value_credit(self):
        shapes = (
            "idx.configure_mock(called=True)\n    assert idx.called",
            "alias = idx\n    alias.configure_mock(called=True)\n    assert idx.called",
            "calls = idx.call_args_list\n    calls.append(mock.call())\n    assert idx.call_args_list",
            "captured = [idx]\n    captured[0].configure_mock(called=True)\n    assert idx.called",
            "def prime(m):\n        m.configure_mock(called=True)\n    prime(idx)\n    assert idx.called",
            "def prime():\n        idx.configure_mock(called=True)\n    prime()\n    assert idx.called",
            "idx.assert_called_with(setattr(idx, 'call_args', mock.call(None)))",
            "def prime(m):\n        m.configure_mock(call_args=mock.call(None))\n"
            "    idx.assert_called_with(prime(idx))",
        )
        for statement in shapes:
            with self.subTest(statement=statement):
                self.assertEqual([r[3] for r in self._scan(self._SHAPE % statement)],
                                 ["_stored_patch_index"])
                # A proof established BEFORE the unsupported operation stands;
                # a later assertion cannot rehabilitate the escaped binding.
                self.assertEqual(self._scan(self._SHAPE % (
                    "idx.assert_called_once()\n    " + statement)), [])
        direct = self._SHAPE % "idx()\n    idx.assert_called_once()"
        self.assertEqual(self._scan(direct), [])

    def test_nondefault_patch_configuration_is_not_a_default_valuation(self):
        for configuration, assertion in (
                ("called=True", "assert idx.called"),
                ("call_count=1", "idx.assert_called_once()"),
                ("new=mock.Mock(called=True)", "assert idx.called"),
                ("new_callable=make_mock", "assert idx.called"),
                ("**options", "assert idx.called")):
            with self.subTest(configuration=configuration):
                shape = self._SHAPE.replace('"_stored_patch_index")',
                                           '"_stored_patch_index", ' + configuration + ')')
                self.assertEqual([r[3] for r in self._scan(shape % assertion)],
                                 ["_stored_patch_index"])
                self.assertEqual(self._scan(self._SHAPE % assertion), [])

    def test_raising_records_are_not_ordinary_truthy_sentinel_values(self):
        for statement in ("assert not idx.call_args.kwargs",
                          "self.assertIsNotNone(idx.call_args.kwargs)",
                          "assert idx.call_args.kwargs == 1",
                          "x = idx.call_args[0][1]"):
            with self.subTest(statement=statement):
                self.assertEqual(self._scan(self._SHAPE % statement), [])
                self.assertEqual([r[3] for r in self._scan(
                    self._SHAPE % "x = idx.call_args.__class__")],
                    ["_stored_patch_index"])

    def test_a_disjunction_over_TWO_spies_exempts_NEITHER(self):
        """The amendment that sharpened the contract: an assertion mentioning
        two doubles proves nothing about either, because it can pass with this
        one never fired."""
        both = '''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_stored_patch_index") as left, \\
            mock.patch.object(landreq, "_stored_patch_index") as idx:
        landreq._landed_index("/g", "t")
    self.assertTrue(left.called or idx.called)
'''
        # Both patch instances are otherwise reportable; a graph-reachable
        # sibling would make the assertion about NEITHER untestable.
        targets = sorted(row[3] for row in self._scan(both))
        self.assertEqual(targets, ["_stored_patch_index"] * 2)
        alone = both.replace("self.assertTrue(left.called or idx.called)",
                             "idx.assert_called_once_with('/g', 't')")
        self.assertEqual([row[3] for row in self._scan(alone)],
                         ["_stored_patch_index"])
        each = alone + "    left.assert_called_once()\n"
        self.assertEqual(self._scan(each), [])

    def test_a_FIRING_proof_exempts_ONLY_ITS_OWN_double(self):
        """A sibling double firing says nothing about this one.

        THE SHAPE THAT CAUGHT ME: an arm holding `git.call_args[0][1]` beside
        `idx.assert_not_called()` had its INERT `idx` double exempted by its
        LIVE `git` double, and the must-hit this census exists to make
        vanished from the output. The exemption is per-HANDLE, not per-arm.
        """
        both = '''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_git") as git, \\
            mock.patch.object(landreq, "_stored_patch_index") as idx:
        landreq._landed_index("/g", "t")
    git.call_args[0][1]
    idx.assert_not_called()
'''
        targets = sorted(row[3] for row in self._scan(both))
        self.assertIn("_stored_patch_index", targets,
                      "a sibling double's firing proof exempted this one")
        self.assertNotIn("_git", targets,
                         "a double reached INTO for its call record was "
                         "reported as unreachable")

    def test_a_FAILED_staged_enumeration_reports_UNKNOWN_never_zero(self):
        """A scan that could not read its own input is not a clean bill.

        `git diff --cached` exits non-zero when the index is unreadable and
        prints nothing either way, so taking the empty list would report ZERO
        candidates for a run that examined NOTHING — the exact collapse this
        census names in other people's tests. It stays rc 0, because a warn
        rung must not block a commit on its own blindness.
        """
        real = om._git

        def refuse(root, *args):
            if args[:2] == ("diff", "--cached"):
                return 3, ""
            return real(root, *args)

        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "helm"))
            shutil.copyfile(os.path.join(ROOT, "helm", "landreq.py"),
                            os.path.join(root, "helm", "landreq.py"))
            os.makedirs(os.path.join(root, "tests"))
            rel = "tests/test_known_staged.py"
            path = os.path.join(root, rel)
            with open(path, "w") as f:
                f.write(self._SHAPE % "idx.assert_not_called()")
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_SYSTEM=os.devnull)
            for args in (("init", "-q"), ("add", "--", rel)):
                p = subprocess.run(("git",) + args, cwd=root, env=env,
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr)
            # The worktree twin would be quiet. The positive must consume the
            # known INDEX blob, not current source or an ambient empty index.
            with open(path, "w") as f:
                f.write(self._SHAPE % "idx.assert_called_once()")
            err, ok_err = io.StringIO(), io.StringIO()
            with mock.patch.object(om.os, "getcwd", return_value=root):
                with mock.patch.object(om, "_git", refuse), \
                        contextlib.redirect_stderr(err):
                    rc = om.main(["--staged"])
                self.assertEqual(rc, 0)
                self.assertIn("scan UNKNOWN", err.getvalue())
                self.assertIn("examined NOTHING", err.getvalue())
                with contextlib.redirect_stderr(ok_err):
                    rc = om.main(["--staged"])
            self.assertEqual(rc, 0)
            lines = [line for line in ok_err.getvalue().splitlines()
                     if line.startswith("[helm orphaned-mock] " + rel + ":")]
            self.assertEqual(len(lines), 1, ok_err.getvalue())
            self.assertIn("test_probe — patches landreq._stored_patch_index, "
                          "NOT REACHED BY THE MODELED GRAPH", lines[0])
            self.assertNotIn("CONTROL FAILED", ok_err.getvalue())
            self.assertNotIn("scan UNKNOWN", ok_err.getvalue())

    def test_a_side_effect_is_an_INSTRUMENT_not_a_stub(self):  # noqa: VACUOUS_ASSERTION — the same probe without the side_effect is reported unconditionally on the last lines
        """An arm can assert non-invocation through a LOCAL LIST the side
        effect appends to, with no spy handle for a walker to find."""
        self.assertEqual(self._scan('''
from unittest import mock
from helm import landreq
def test_probe():
    called = []
    def recorder(*a, **k):
        called.append(a)
    with mock.patch.object(landreq, "_stored_patch_index",
                           side_effect=recorder):
        landreq._landed_index("/g", "t")
    assert called == []
'''), [])
        self.assertTrue(self._scan('''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_stored_patch_index"):
        landreq._landed_index("/g", "t")
'''))

    def test_NO_ENTRY_POINT_is_unknown_and_never_a_finding(self):  # noqa: VACUOUS_ASSERTION — adding the entry point to the same probe makes it a finding unconditionally on the last lines
        """An arm that never calls INTO the patched module may reach the code
        through a helper, another module, or a class this walker does not
        follow. Reporting it would be asserting absence from a probe that
        cannot see."""
        self.assertEqual(self._scan('''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_stored_patch_index"):
        pass
'''), [])
        self.assertTrue(self._scan('''
from unittest import mock
from helm import landreq
def test_probe():
    with mock.patch.object(landreq, "_stored_patch_index"):
        landreq._landed_index("/g", "t")
'''))

    def test_a_DECLARED_exemption_is_honoured_by_identity(self):  # noqa: VACUOUS_ASSERTION — the identical probe without the marker is reported unconditionally on the last lines
        marked = '''
from unittest import mock
from helm import landreq
def test_probe():  # noqa: ORPHANED_MOCK — deliberately unreachable
    with mock.patch.object(landreq, "_stored_patch_index"):
        landreq._landed_index("/g", "t")
'''
        self.assertEqual(self._scan(marked), [])
        self.assertTrue(self._scan(marked.replace(
            "  # noqa: ORPHANED_MOCK — deliberately unreachable", "")))


class ReachabilityTest(unittest.TestCase):
    def test_a_nested_def_counts_as_the_enclosing_function_s_calls(self):
        """`_landed_index` reaches its builder through a nested `_build`, so a
        walker that did not descend would call every such fixture orphaned."""
        graph = om.call_graph('''
def outer():
    def inner():
        return deep()
    return inner()
''')
        self.assertIn("deep", om.reachable(graph, ["outer"]))

    def test_reachability_is_TRANSITIVE(self):
        graph = om.call_graph('''
def a():
    return b()
def b():
    return c()
def c():
    return 1
''')
        self.assertEqual(om.reachable(graph, ["a"]), {"a", "b", "c"})
        self.assertNotIn("a", om.reachable(graph, ["c"]))

    def test_a_CYCLE_terminates(self):
        graph = om.call_graph('''
def a():
    return b()
def b():
    return a()
''')
        self.assertEqual(om.reachable(graph, ["a"]), {"a", "b"})


if __name__ == "__main__":
    unittest.main()


class TheFiredProofSurvivesUnrelatedSetupTest(unittest.TestCase):
    """A fired-proof is defeated by two details that say nothing about firing.

    BOTH WERE FOUND THROUGH ONE REAL ARM and neither alone explains it:
    tests/test_gate.py's faulty-VERIFIED reader arm PROVES both its doubles
    fired, with `binding.assert_called_once()` and `retained.assert_called_
    once()`, and the census reported it anyway. Curing either cause alone
    leaves that arm still reported, which is why they land together.

    ONE: A CLOSED BLOCK CANNOT SWALLOW WHAT FOLLOWS IT. The prefix walk
    stopped at any `with` it could not interpret -- correct about never
    DESCENDING, since such a context could suppress a failure raised inside
    it, and wrong about everything AFTER it, where `__exit__` has already run
    and cannot reach a later raise. The real arm opens with `with
    serial_process(ran=9):`, and fixture-scope-then-patch is an ordinary
    shape: a tempdir, a chdir, a captured stream.

    TWO: `return_value=` AND `wraps=` PRESERVE WHAT THE DOUBLE RECORDS. The
    binding predicate admitted only a bare patch or `new_callable=mock.Mock`,
    so the real arm's `return_value=(...)` and `wraps=capture` were read as
    unknown configuration. Both write `called`, `call_count` and `call_args`
    through the same machinery a bare Mock uses, before either is consulted.

    EVERY ROW BELOW HAS ITS OPPOSITE, which is this file's own standard: a
    newly QUIET shape is worthless beside a REPORTED one that differs only in
    the property under test, or the probe could simply have gone inert.
    """

    HEAD = "from unittest import mock\nfrom helm import landreq\n"

    def _scan(self, body):
        return om.scan_source(ROOT, "<probe>", body)[0]

    def _probe(self, preamble="", kwargs="", proof=True):
        body = self.HEAD + "def test_probe():\n" + preamble
        body += ('    with mock.patch.object(landreq, "_stored_patch_index"%s)'
                 ' as h:\n        landreq._landed_index("/g","t")\n' % kwargs)
        if proof:
            body += "    h.assert_called_once()\n"
        return body

    def test_a_fired_proof_is_not_a_finding_and_its_absence_is(self):
        """THE BASELINE PAIR. Without this the quiet rows below could all be
        a probe that reports nothing at all."""
        self.assertEqual(self._scan(self._probe()), [],
                         "a proven-fired double is reported, so every quiet "
                         "row in this class is meaningless")
        self.assertTrue(self._scan(self._probe(proof=False)),
                        "the SAME probe with no fired-proof is quiet too, so "
                        "this class cannot tell an exemption from an inert "
                        "census")

    def test_setup_scope_before_the_patch_does_not_cost_the_exemption(self):
        """PAIRED IN PLACE. The must-report row uses the SAME preamble and
        differs only by deleting the fired-proof, so the quiet row above it
        cannot pass because this preamble made the census inert."""
        setup = '    with open("/x") as f:\n        pass\n'
        self.assertEqual(
            self._scan(self._probe(setup)), [],
            "an unrelated context manager that has already CLOSED defeats a "
            "fired-proof for every handle bound after it — the ordinary "
            "fixture-scope-then-patch shape loses an exemption it earned")
        self.assertTrue(
            self._scan(self._probe(setup, proof=False)),
            "the same preamble with NO fired-proof is quiet too, so the "
            "assertion above is about a census that reports nothing here")

    def test_the_walk_still_refuses_to_enter_a_block_it_cannot_read(self):
        """THE OPPOSITE ROW, and the reason the cure SKIPS rather than
        descends: inside an uninterpretable context the failure may never
        escape, so a proof written there proves nothing."""
        self.assertTrue(self._scan(self.HEAD + '''def test_probe():
    with open("/x") as f:
        with mock.patch.object(landreq, "_stored_patch_index") as h:
            landreq._landed_index("/g","t")
        h.assert_called_once()
'''), "a fired-proof INSIDE a context manager nothing can interpret was "
      "credited — that context may suppress the very failure the proof "
      "depends on")

    def test_a_block_that_touches_the_handle_is_still_refused(self):
        self.assertTrue(
            self._scan(self._probe("    with holder(h) as g:\n        pass\n")),
            "a context manager that RECEIVES the handle was skipped as "
            "unrelated — it is an alias or a capture, and interpreting those "
            "is exactly what this walk declines to do")

    def test_configuration_that_preserves_the_record_keeps_the_exemption(self):
        """PAIRED IN PLACE, AND DELIBERATELY FLAT. Each quiet row is checked
        against the SAME patch spelling with the fired-proof deleted, because
        a keyword that silenced the census outright would otherwise read as an
        earned exemption. Written out rather than looped: `self.subTest` is a
        context manager that SUPPRESSES the failures inside it, so a control
        placed under one does not unconditionally guard anything."""
        self.assertEqual(
            self._scan(self._probe(kwargs=", return_value=7")), [],
            "return_value= was read as unknown configuration, but it changes "
            "what the double DOES and not what it RECORDS: called, call_count "
            "and call_args are written before it is consulted")
        self.assertTrue(
            self._scan(self._probe(kwargs=", return_value=7", proof=False)),
            "return_value= with NO fired-proof is quiet as well, so that "
            "keyword is silencing the census rather than earning an exemption")
        self.assertEqual(
            self._scan(self._probe(kwargs=", wraps=cap")), [],
            "wraps= was read as unknown configuration, but a wrapping double "
            "records every call it passes through")
        self.assertTrue(
            self._scan(self._probe(kwargs=", wraps=cap", proof=False)),
            "wraps= with NO fired-proof is quiet as well, so that keyword is "
            "silencing the census rather than earning an exemption")

    def test_replacing_the_double_outright_is_still_reported(self):
        """THE OPPOSITE ROW for the pair above. `new=` does not hand back a
        Mock at all, so nothing about the never-fired valuation is known."""
        self.assertTrue(
            self._scan(self._probe(kwargs=", new=thing")),
            "a patch that REPLACES the object was credited as a spy binding "
            "— the handle is whatever `new` names and may record nothing")


class TheControlsSubjectIsStagedNotBorrowedTest(unittest.TestCase):
    """task/2248. The census's own controls were written against real helm
    functions, and reachability resolves an imported module to a FILE UNDER
    THE SCANNED ROOT — so the arms answered a question about whatever
    repository the commit happened to be in.

    OBSERVED, not theorised: with helm's guard rail armed in another checkout,
    every commit there printed CONTROL FAILED, and the message blamed the
    walker or a moved premise when the real answer was that it was asking
    about a tree it was not written for.

    AND THE INSIDE-helm HALF IS THE ONE THAT WOULD HAVE BITTEN LATER: a
    control whose subject is live source can be retired by an ordinary
    refactor of that source, silently disarming the census. The arms now run
    against a package this module WRITES, so the only thing that can break
    them is a change to the walker — which is what a control is for.
    """

    def test_the_controls_hold_with_no_helm_package_in_sight(self):
        """THE DEFECT, and its must-hit beside it. Running from a directory
        that is not a helm checkout is exactly the condition that printed
        CONTROL FAILED on every commit in another repository."""
        held, why = om._controls_hold()
        # assertTrue RATHER THAN assertIs(held, True), and on purpose: this
        # arm's subject is that the controls HOLD, and both other answers —
        # False and the UNKNOWN None — must fail it. The None-versus-False
        # distinction has its own arm below, where it is the subject rather
        # than a side condition.
        self.assertTrue(held,
                        "the controls do not hold in helm's own tree, so the "
                        "row below says nothing about roots: %r" % (why,))
        tmp = tempfile.mkdtemp(prefix="not-a-helm-checkout-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            held_elsewhere, why_elsewhere = om._controls_hold()
        finally:
            os.chdir(cwd)
        self.assertTrue(held_elsewhere,
                        "the controls fail from a root with no helm package, "
                        "so this rung still reports CONTROL FAILED on every "
                        "commit in another repository: %r" % (why_elsewhere,))

    def test_no_control_names_a_function_somebody_else_maintains(self):
        """THE DURABLE HALF. A control that borrows live source is a control a
        refactor can retire, and nothing would say so — the census would go on
        printing a number."""
        planted = "".join((om._CONTROL, om._QUIET_CONTROL,
                           om._ABSENCE_ORPHAN_CONTROL,
                           om._ABSENCE_LIVE_CONTROL))
        self.assertIn("mock.patch.object", planted,
                      "the control sources are empty, so the absence below is "
                      "about nothing at all")
        self.assertNotIn("landreq", planted,
                         "a control still patches a real helm module, so an "
                         "ordinary refactor there can disarm this census")
        self.assertNotIn("from helm import", planted,
                         "a control still imports helm, so it cannot resolve "
                         "outside a helm checkout")

    def test_staging_that_fails_is_UNKNOWN_and_not_a_control_FAILURE(self):
        """THE THIRD ANSWER. A rung that cannot set up its own experiment has
        learned nothing about the walker, and reporting that as CONTROL FAILED
        would put an alarm where an admission belongs — the same distinction
        the scanner already makes for an unreadable index."""
        with mock.patch.object(om.tempfile, "mkdtemp",
                               side_effect=OSError("no space")):
            held, why = om._controls_hold()
        self.assertIsNone(held,
                          "unstageable controls report a definite verdict, so "
                          "a rung having a bad day is indistinguishable from "
                          "a broken walker: %r" % (held,))
        self.assertIn("could not stage", why,
                      "the UNKNOWN does not say what went wrong, so a reader "
                      "cannot tell whether to act: %r" % (why,))
        # THE MUST-HIT: the same call without the failure is a real verdict,
        # so the None above is caused by the staging and not by the arm.
        recovered, _ = om._controls_hold()
        self.assertIs(recovered, True,
                      "the controls do not hold once staging works, so the "
                      "None above is not attributable to the staging failure")
