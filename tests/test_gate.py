#!/usr/bin/env python3
"""helm gate: a suite result is a claim only when MINTED, and a verdict either
binds one or says out loud that it did not.

Every test here plants a REAL git repo and runs a REAL child process, because
the thing under test is precisely the difference between a claim and a model of
one — mocking the run would rebuild the defect inside the test."""
import ast
import contextlib
import hashlib
import inspect
import io
import textwrap
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from helm import dispatches, gate, landreq

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "HELM_CHAT_NODE_URL", "HELM_VERDICT_ROOM", "HELM_PROC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            # BOTH CROSS-TREE KNOBS ARE SCRATCH. They change whether the mint
            # refuses, so a suite that inherited them from the operator's shell
            # would pass or fail by what the host happened to export — the same
            # host-coupled fixture class that split local-green from fab-red.
            "HELM_NO_TREE_WARNING", "HELM_CROSS_TREE_GATE")

# A child that prints a unittest summary and nothing else — the shape the
# parser must read, without paying 5115 tests to produce it.
def _emit(*lines):
    return ["-c", "import sys; print(%r, file=sys.stderr)" % "\n".join(lines)]


def _emit_exit(rc, *lines):
    return ["-c", "import sys; print(%r, file=sys.stderr); raise SystemExit(%d)"
            % ("\n".join(lines), rc)]


def _verdicts(order, anchors=None):
    """{id: (append index, content anchor)} — the shape _snapshot builds.

    The anchor defaults to a stable digest of the id, so a test that moves a
    row's POSITION keeps its IDENTITY (that is compaction) while a test that
    wants a DIFFERENT event passes a different anchor (that is recreation)."""
    anchors = anchors or {}
    return {rid: (pos, anchors.get(rid) or hashlib.blake2b(
        rid.encode("utf-8"), digest_size=16).hexdigest())
        for rid, pos in order.items()}


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_ROOM"] = "main"
        # THE CHAT DIR IS SCRATCH, LIKE HELM_HOME ALWAYS WAS. Popping the key
        # without setting one left every verdict's attestation posting into the
        # REAL fleet room: measured — one `python3 -m unittest
        # tests.test_gate` appended 9 lines to /dev/shm/helm-chat/main.jsonl.
        # A suite that writes to the thing the fleet is reading is not a suite,
        # it is a second agent — and the noise lands on whoever is on watch.
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        # dispatches.add/send now REFUSE an identityless author (the family
        # floor is never an author) — the fixture declares one,
        # exactly like a real reviewer's pane does.
        os.environ["HELM_CHAT_NAME"] = "gate-fixture"
        # The fleet suite cap counts REAL processes off /proc; an empty fake
        # proc tree keeps these runs deterministic under any box load.
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "gate@test")
        self._git("config", "user.name", "gate test")
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("one\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "first")
        # THE FIXTURE STAYS ON A LANE BRANCH, and `main` stays behind it. A
        # reviewed tip that is already ON trunk is LANDED, and landreq reports
        # that instead of the review state — the land-path tests below would
        # have been asserting against a lifecycle they never reached.
        self._git("checkout", "-q", "-b", "lane/probe")
        with open(os.path.join(self.repo, "b.txt"), "w") as fh:
            fh.write("lane\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "lane work")
        self.head = self._git("rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self.prior.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val

    def _git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.repo, text=True,
                              capture_output=True).stdout.strip()

    def _dirty(self):
        with open(os.path.join(self.repo, "a.txt"), "a") as fh:
            fh.write("edit\n")

    def gitdir(self):
        return os.path.realpath(os.path.join(
            self.repo, self._git("rev-parse", "--git-common-dir")))

    def descendant_receipt(self, receipt_ts="2026-08-02T00:00:02Z"):
        """A real whole-suite receipt at integration B containing lane tip A."""
        reviewed = self.head
        self._git("checkout", "-q", "-b", "integration/gate", reviewed)
        with open(os.path.join(self.repo, "descendant.txt"), "w") as fh:
            fh.write("later integration tree\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "descendant gate tree")
        descendant = self._git("rev-parse", "HEAD")
        self.assertNotEqual(descendant, reviewed)
        self.assertEqual(subprocess.run(
            ("git", "merge-base", "--is-ancestor", reviewed, descendant),
            cwd=self.repo).returncode, 0)
        with mock.patch.object(
                gate, "SUITE", tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))), \
                mock.patch.object(gate.pk, "now_ts", return_value=receipt_ts):
            row, err = gate.run(repo=self.repo)
        self._git("checkout", "-q", "lane/probe")
        self.assertIsNone(err, err)
        self.assertEqual(row["head"], descendant)
        self.assertEqual(self._git("rev-parse", "lane/probe"), reviewed)
        self.assertEqual(self._git("rev-parse", "integration/gate"), descendant)
        return reviewed, descendant, row

    def mint(self, *lines, **kw):
        """A custom-argv run, so the child is cheap. Custom means the recorded
        interpreter is UNKNOWN by design — the suite path is covered separately
        by test_default_run_records_the_running_interpreter."""
        row, err = gate.run(repo=self.repo,
                            argv=[sys.executable] + _emit(*lines), **kw)
        self.assertIsNone(err, err)
        return row

    def fixture_suite(self, source):
        """Commit one real discoverable test module and return its path."""
        tests = os.path.join(self.repo, "tests")
        os.makedirs(tests, exist_ok=True)
        with open(os.path.join(tests, "__init__.py"), "w") as fh:
            fh.write("")
        path = os.path.join(tests, "test_gate_receipt_fixture.py")
        with open(path, "w") as fh:
            fh.write(source)
        self._git("add", "-A")
        self._git("commit", "-qm", "gate receipt fixture")
        self.head = self._git("rev-parse", "HEAD")
        return path


class ParseResult(GateBase):
    def test_ok_with_skips(self):
        got = gate.parse_result("Ran 5115 tests in 154.773s\n\nOK (skipped=8)\n")
        self.assertEqual(got["status"], "OK")
        self.assertEqual(got["ran"], 5115)
        self.assertEqual(got["skipped"], 8)
        self.assertEqual(got["elapsed"], 154.773)

    def test_failed_keeps_the_detail(self):
        got = gate.parse_result("Ran 12 tests in 1.0s\n\nFAILED (failures=2)\n")
        self.assertEqual(got["status"], "FAILED")
        self.assertEqual(got["detail"], "failures=2")
        self.assertTrue(got["failures_unreadable"])

    def test_failure_blocks_yield_ids_and_first_traceback_frames(self):
        text = """======================================================================
ERROR: test_boom (tests.test_probe.Probe.test_boom)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in test_boom
    raise ValueError("boom")
ValueError: boom

======================================================================
FAIL: test_nope (tests.test_probe.Probe.test_nope)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 11, in test_nope
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 2 tests in 0.1s

FAILED (failures=1, errors=1)
"""
        got = gate.parse_result(text)
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(got["failures"], [
            {"kind": "ERROR", "test": "tests.test_probe.Probe.test_boom",
             "traceback": "File \"/tmp/test_probe.py\", line 7, in test_boom"},
            {"kind": "FAIL", "test": "tests.test_probe.Probe.test_nope",
             "traceback": "File \"/tmp/test_probe.py\", line 11, in test_nope"},
        ])

    def test_failure_capture_is_capped_with_an_honest_marker(self):
        blocks = []
        for i in range(gate.FAILURE_CAP + 5):
            blocks.append("""======================================================================
ERROR: test_%d (tests.test_many.Many.test_%d)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_many.py", line %d, in test_%d
    raise RuntimeError()
RuntimeError
""" % (i, i, i + 1, i))
        text = "\n".join(blocks) + ("\nRan 25 tests in 0.1s\n\n"
                                     "FAILED (errors=25)\n")
        got = gate.parse_result(text)
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(len(got["failures"]), gate.FAILURE_CAP + 1)
        self.assertEqual(got["failures"][-1], {"truncated": 5})

    def test_log_lines_with_dotted_parens_are_not_unittest_blocks(self):
        text = ("ERROR: backend unavailable (evil.Case.test_forged)\n"
                "FAIL: retry exhausted (evil.Case.test_other)\n"
                "Ran 1 test in 0.1s\n\nFAILED (errors=1)\n")
        got = gate.parse_result(text)
        self.assertEqual(got["failures"], [])
        self.assertTrue(got["failures_unreadable"])

    def test_separator_backed_log_prose_cannot_mimic_a_test_identity(self):
        mimics = [
            "ERROR: proxywatch retry exhausted (urllib.error.URLError)",
            "ERROR: lookup (api.example.com)",
            "FAIL: import_hook (pkg.module.Loader)",
        ]
        blocks = []
        for header in mimics:
            blocks.append("=" * 70 + "\n" + header + "\n" + "-" * 70 +
                          "\nTraceback (most recent call last):\n"
                          "  File \"/tmp/log.py\", line 1, in emit\n")
        blocks.append("""======================================================================
FAIL: test_real (tests.test_probe.Probe.test_real) (value=1)
real failure description
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 9, in test_real
    self.assertEqual(1, 2)
AssertionError: 1 != 2
""")
        text = "\n".join(blocks) + \
            "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n"
        got = gate.parse_result(text)
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(got["failures"], [{
            "kind": "FAIL",
            "test": "tests.test_probe.Probe.test_real",
            "traceback": "File \"/tmp/test_probe.py\", line 9, in test_real",
        }])

    def test_fixture_error_cannot_be_replaced_by_dotted_exception_prose(self):
        text = """======================================================================
ERROR: URLError (urllib.error.URLError)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
ERROR: setUpClass (__main__.Probe)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 4, in setUpClass
    raise RuntimeError()
RuntimeError

Ran 0 tests in 0.1s

FAILED (errors=1)
"""
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [{
            "kind": "ERROR", "test": "__main__.Probe.setUpClass",
            "traceback": ("File \"/tmp/test_probe.py\", line 4, "
                          "in setUpClass"),
        }])

    def test_standard_fixture_and_loader_headers_have_canonical_ids(self):  # noqa: VACUOUS_ASSERTION — cases bind positive canonical ids before prose absences
        cases = {
            "setUpClass (__main__.Probe)": "__main__.Probe.setUpClass",
            "tearDownClass (tests.test_probe.Probe)":
                "tests.test_probe.Probe.tearDownClass",
            "setUpModule (tests.test_probe)": "tests.test_probe.setUpModule",
            "tearDownModule (test_probe)": "test_probe.tearDownModule",
            "runTest (__main__.Probe.runTest)": "__main__.Probe.runTest",
            "test_local (__main__.make.<locals>.Probe.test_local)":
                "__main__.make.<locals>.Probe.test_local",
            "tests.test_bad (unittest.loader._FailedTest.tests.test_bad)":
                "unittest.loader._FailedTest.tests.test_bad",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(gate._failure_identity(header), expected)
        for prose in ("URLError (urllib.error.URLError)",
                      "lookup (api.example.com)",
                      "import_hook (pkg.module.Loader)"):
            with self.subTest(prose=prose):
                self.assertIsNone(gate._failure_identity(prose))

    def test_log_candidate_cannot_replace_a_real_runTest_failure(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: runTest (__main__.Probe.runTest)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in runTest
    self.fail("boom")
AssertionError: boom

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["__main__.Probe.runTest"])

    def test_log_candidate_cannot_replace_an_unsupported_test_object(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: unittest.case.FunctionTestCase (boom)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 3, in boom
    raise AssertionError("boom")
AssertionError: boom

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [])

    def test_log_candidate_cannot_replace_an_unsupported_doctest(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: probe ()
Doctest: probe
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/usr/lib/python/doctest.py", line 1, in runTest
    raise self.failureException(report)
DocTestFailure

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [])
        self.assertTrue(gate._failure_protocol_like("probe ()"))

    def test_log_candidate_cannot_replace_an_unsupported_docfile(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: /tmp/sample.txt
Doctest: sample.txt
----------------------------------------------------------------------
AssertionError: Failed doctest test for sample.txt
  File "/tmp/sample.txt", line 0

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual(got["failures"], [])
        self.assertTrue(gate._failure_protocol_like(
            "/tmp/sample.txt", "Doctest: sample.txt"))

    def test_log_candidate_cannot_replace_a_local_TestCase_method(self):
        text = """======================================================================
FAIL: test_retry (logs.Case.test_retry)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/log.py", line 1, in emit
======================================================================
FAIL: test_local (__main__.make.<locals>.Probe.test_local)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in test_local
    self.fail("boom")
AssertionError: boom

Ran 1 test in 0.1s

FAILED (failures=1)
"""
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["__main__.make.<locals>.Probe.test_local"])

    def test_earlier_protocol_shaped_logs_cannot_evict_the_real_failure(self):
        fake = []
        for i in range(gate.FAILURE_CAP + 5):
            fake.append("""======================================================================
ERROR: test_%d (evil.Case.test_%d)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/fake.py", line 1, in fake
    raise RuntimeError()
RuntimeError
""" % (i, i))
        real = """======================================================================
ERROR: test_real (tests.test_probe.Probe.test_real)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 9, in test_real
    raise ValueError("real")
ValueError: real
"""
        text = "\n".join(fake) + real + \
            "\nRan 1 test in 0.1s\n\nFAILED (errors=1)\n"
        got = gate.parse_result(text)
        self.assertTrue(got["failures_unreadable"])
        self.assertEqual([item["test"] for item in got["failures"]],
                         ["tests.test_probe.Probe.test_real"])

    def test_the_LAST_summary_footer_is_authoritative(self):
        got = gate.parse_result(
            "FAILED (failures=1)\nRan 1 test in 0.1s\n\nOK\n")
        self.assertEqual(got["status"], "OK")
        self.assertEqual(got["failures"], [])
        self.assertFalse(got["failures_unreadable"])

    def test_expected_failures_do_not_invent_FAIL_blocks(self):
        text = """======================================================================
ERROR: test_boom (tests.test_probe.Probe.test_boom)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/test_probe.py", line 7, in test_boom
    raise ValueError("boom")
ValueError: boom

Ran 2 tests in 0.1s

FAILED (errors=1, expected failures=1)
"""
        got = gate.parse_result(text)
        self.assertEqual(got["status"], "FAILED")
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual([item["kind"] for item in got["failures"]], ["ERROR"])

    def test_exception_group_traceback_keeps_its_first_frame(self):
        text = """======================================================================
ERROR: test_group (tests.test_probe.Probe.test_group)
----------------------------------------------------------------------
  + Exception Group Traceback (most recent call last):
  |   File "/tmp/test_probe.py", line 9, in test_group
  |     raise ExceptionGroup("many", [ValueError("x")])
  | ExceptionGroup: many (1 sub-exception)
  +-+---------------- 1 ----------------

Ran 1 test in 0.1s

FAILED (errors=1)
"""
        got = gate.parse_result(text)
        self.assertFalse(got["failures_unreadable"])
        self.assertEqual(got["failures"][0]["traceback"],
                         "File \"/tmp/test_probe.py\", line 9, in test_group")

    def test_no_summary_is_unknown_never_ok(self):
        """A run killed mid-flight prints no verdict line. Reading that as
        anything but UNKNOWN is how a missing input becomes a pass — the exact
        shape of the GraalPy hang this module exists for."""
        for text in ("", "Traceback (most recent call last):\n  boom\n"):
            self.assertEqual(gate.parse_result(text)["status"], "UNKNOWN")

    def test_truncated_run_keeps_the_count_and_withholds_the_verdict(self):
        got = gate.parse_result("Ran 300 tests in 90.0s\n")
        self.assertEqual(got["ran"], 300)          # truthful
        self.assertEqual(got["status"], "UNKNOWN")  # not observed

    def test_malformed_elapsed_cannot_turn_an_unread_count_into_OK(self):
        got = gate.parse_result("Ran 1 test in 1..2s\n\nOK\n")
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertIsNone(got["ran"])
        self.assertIn("run count is unreadable", got["detail"])
        self.assertTrue(got["failures_unreadable"])

    def test_a_later_malformed_count_invalidates_an_earlier_valid_one(self):
        got = gate.parse_result(
            "Ran 1 test in 0.1s\nRan 999 tests in 1..2s\n\nOK\n")
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertIsNone(got["ran"])
        self.assertIn("terminal footer", got["detail"])

    def test_post_summary_protocol_cannot_replace_the_terminal_count(self):
        got = gate.parse_result(
            "Ran 1 test in 0.1s\n\nOK\nRan 999 tests in 9.999s\n")
        self.assertEqual(got["status"], "UNKNOWN")
        self.assertIsNone(got["ran"])
        self.assertIn("terminal footer", got["detail"])


class VersionGatedReaders(unittest.TestCase):
    """Every membership test on a receipt's version must admit the version the
    gate currently MINTS.

    THIS CLASS HAS NOW BITTEN FOUR TIMES IN ONE FILE and each time it looked
    like a different bug: `_receipt_id`'s failure-identity tuple, its
    base-check tuple, `_show_base_check`, and `_show_failures` — which printed
    "receipt predates failure identities" about a v4 receipt that carries them.
    Every one is spelled as a set of versions that HAVE some field, so the
    newest version always belongs and omitting it always fails SILENTLY: the
    reader does not crash, it serves the legacy answer.

    A membership test is the shape that recurs, so that is what is checked.
    `== 1` / `!= 1` are deliberate legacy discriminators and are listed rather
    than asserted — a version-specific branch is a decision, not an omission.
    Read from the SOURCE, because the bug is in code that never runs until a
    receipt of the new version exists, which is exactly when it is too late."""

    def _minted_version(self, tree):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "v"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, int)):
                    return value.value
        return None

    def test_every_version_gated_reader_admits_the_minted_version(self):  # noqa: VACUOUS_ASSERTION — two unconditional controls on the real input: assertIsNotNone(minted) proves the source parse found the minted version, and assertGreaterEqual(len(memberships), 3) proves the walk found gates. The rung cannot credit the second because `memberships` is a local accumulator rather than a production call's result, which its own docstring says it will not count.
        tree = ast.parse(inspect.getsource(gate))
        minted = self._minted_version(tree)
        self.assertIsNotNone(minted, "no literal receipt version found in gate")

        memberships, discriminators = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            left = node.left
            if not (isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "get"
                    and left.args and isinstance(left.args[0], ast.Constant)
                    and left.args[0].value == "v"):
                continue
            op, right = node.ops[0], node.comparators[0]
            if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Tuple):
                versions = [e.value for e in right.elts
                            if isinstance(e, ast.Constant)]
                memberships.append((node.lineno, versions))
            else:
                discriminators.append(node.lineno)

        # POSITIVE CONTROL, unconditional: if the walk found nothing, every
        # assertion below is vacuous and the guard is decoration.
        self.assertGreaterEqual(len(memberships), 3, "the AST walk found no "
                                "version membership tests — it is not looking "
                                "at what it thinks it is")
        for lineno, versions in memberships:
            self.assertIn(
                minted, versions,
                "gate.py line %d gates on version %s and the gate MINTS v%d. "
                "Whether the test is `in` or `not in`, the tuple enumerates "
                "the versions that HAVE the field, so the newest one belongs "
                "in it. Omitting it does not raise — the reader quietly serves "
                "the legacy answer for every receipt minted from now on."
                % (lineno, versions, minted))
        self.assertTrue(discriminators)   # the `== 1` legacy arms still exist


class Minting(GateBase):
    def test_receipt_binds_the_real_tree_state(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertEqual(row["head"], self.head)
        self.assertEqual(row["tree"], self._git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(row["dirty"])
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["ran"], 3)

    def test_a_dirty_worktree_is_recorded_not_warned_about(self):
        self._dirty()
        self.assertTrue(self.mint("Ran 1 test in 0.1s", "", "OK")["dirty"])

    def test_the_receipt_is_on_disk_before_run_returns(self):
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        found, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertIn(row["id"], [r["id"] for r in found])

    def test_default_run_records_the_running_interpreter(self):
        """THE CORE GUARANTEE. helm spawns the suite as a child of ITSELF, so
        the recorded identity cannot drift from the one that ran. Asserted
        against sys.implementation directly: a receipt that merely CONTAINS an
        interpreter-shaped string would pass a weaker check while naming the
        wrong python."""
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 1 test in 0.0s", "", "OK"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertTrue(row["suite"])
        self.assertEqual(row["interpreter"]["name"], sys.implementation.name)
        self.assertEqual(row["interpreter"]["language"],
                         "%d.%d.%d" % sys.version_info[:3])
        self.assertEqual(row["interpreter"]["executable"],
                         os.path.realpath(sys.executable))
        self.assertIn(gate.interpreter_label(row["interpreter"]),
                      gate.evidence_line(row))

    def test_real_runTest_failure_records_its_identity(self):
        code = """import unittest

class ReceiptRunTest(unittest.TestCase):
    def runTest(self):
        self.fail("boom")

result = unittest.TextTestRunner().run(ReceiptRunTest())
raise SystemExit(not result.wasSuccessful())
"""
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "__main__.ReceiptRunTest.runTest")

    def test_real_local_TestCase_method_records_its_qualname(self):
        code = """import unittest

def make():
    class ReceiptLocal(unittest.TestCase):
        def test_local(self):
            self.fail("boom")
    return ReceiptLocal

suite = unittest.defaultTestLoader.loadTestsFromTestCase(make())
result = unittest.TextTestRunner().run(suite)
raise SystemExit(not result.wasSuccessful())
"""
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "__main__.make.<locals>.ReceiptLocal.test_local")

    def test_real_doctest_failure_is_explicitly_unreadable(self):
        code = """import doctest
import types
import unittest

probe = types.ModuleType("probe")
probe.__doc__ = ">>> 1 + 1\\n3\\n"
result = unittest.TextTestRunner().run(doctest.DocTestSuite(probe))
raise SystemExit(not result.wasSuccessful())
"""
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["failures_unreadable"])
        self.assertEqual(row["failures"], [])

    def test_real_docfile_failure_is_explicitly_unreadable(self):
        path = os.path.join(self.repo, "sample.txt")
        with open(path, "w") as fh:
            fh.write(">>> 1 + 1\n3\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "doctest fixture")
        self.head = self._git("rev-parse", "HEAD")
        code = ("import doctest,unittest; "
                "r=unittest.TextTestRunner().run("
                "doctest.DocFileSuite(%r,module_relative=False)); "
                "raise SystemExit(not r.wasSuccessful())" % path)
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["failures_unreadable"])
        self.assertEqual(row["failures"], [])

    def test_real_class_fixture_error_records_a_canonical_identity(self):
        self.fixture_suite("""import unittest

class ReceiptFixtureError(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("fixture boom")

    def test_never_runs(self):
        self.fail("unreachable")
""")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "tests.test_gate_receipt_fixture."
                         "ReceiptFixtureError.setUpClass")

    def test_real_import_error_records_the_loader_identity(self):
        self.fixture_suite("raise RuntimeError('import boom')\n")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "unittest.loader._FailedTest."
                         "tests.test_gate_receipt_fixture")

    def test_a_real_failing_suite_records_and_shows_the_test_identity(self):
        path = self.fixture_suite("""import unittest

class ReceiptFailure(unittest.TestCase):
    def test_known_failure(self):
        '''receipt should name this test despite its description'''
        self.assertEqual(1, 2)
""")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(len(row["failures"]), 1)
        failure = row["failures"][0]
        self.assertEqual(failure["test"],
                         "tests.test_gate_receipt_fixture.ReceiptFailure."
                         "test_known_failure")
        self.assertIn(os.path.basename(path), failure["traceback"])
        evidence = gate.evidence_line(row)
        self.assertNotIn("test_known_failure", evidence,
                         "the one-line evidence protocol stays counts-only")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(gate.cmd_gate(["show", row["id"]]), 0)
        shown = stdout.getvalue()
        self.assertIn(failure["test"], shown)
        self.assertIn(failure["traceback"], shown)

    def test_a_real_passing_suite_records_a_confirmed_empty_list(self):
        self.fixture_suite("""import unittest

class ReceiptPass(unittest.TestCase):
    def test_known_pass(self):
        self.assertTrue(True)
""")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["failures"], [])
        self.assertFalse(row["failures_unreadable"])

    def test_post_summary_atexit_output_cannot_forge_a_verified_count(self):
        self.fixture_suite("""import atexit
import sys
import unittest

atexit.register(lambda: print("Ran 999 tests in 9.999s", file=sys.stderr))

class ReceiptPass(unittest.TestCase):
    def test_one_real_pass(self):
        self.assertTrue(True)
""")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["rc"], 0)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIsNone(row["ran"])
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_mangled_failure_stream_records_unreadable_not_empty(self):
        with mock.patch.object(gate, "SUITE", tuple(_emit_exit(
                1, "ERROR: this is not a unittest identity",
                "Ran 1 test in 0.0s", "", "FAILED (errors=1)"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["failures"], [])
        self.assertTrue(row["failures_unreadable"])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(gate.cmd_gate(["show", row["id"]]), 0)
        self.assertIn("failures   UNREADABLE", stdout.getvalue())

    def test_stdout_cannot_forge_unittest_failure_protocol(self):
        self.fixture_suite("""import unittest

class StdoutNoise(unittest.TestCase):
    def test_passes_while_logging(self):
        for i in range(25):
            print("=" * 70)
            print("ERROR: fake (evil.Case.test_%d)" % i)
            print("-" * 70)
            print("Traceback (most recent call last):")
            print("  File \\"/tmp/fake.py\\", line 1, in fake")
        print("Ran 25 tests in 0.1s")
        print("FAILED (errors=25)")
        self.assertTrue(True)
""")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["failures"], [])
        self.assertFalse(row["failures_unreadable"])

    def test_FAILED_summary_with_zero_exit_is_UNKNOWN(self):
        with mock.patch.object(gate, "SUITE", tuple(_emit(
                "Ran 1 test in 0.0s", "", "FAILED (failures=1)"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["rc"], 0)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIn("sources disagree", row["detail"])
        self.assertTrue(row["failures_unreadable"])

    def test_FAILED_summary_before_timeout_is_UNKNOWN(self):
        code = ("import sys,time; "
                "print('Ran 1 test in 0.0s\\n\\nFAILED (errors=1)', "
                "file=sys.stderr, flush=True); time.sleep(30)")
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            row, err = gate.run(repo=self.repo, timeout=0.2)
        self.assertIsNone(err, err)
        self.assertIsNone(row["rc"])
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIn("timeout", row["detail"])
        self.assertTrue(row["failures_unreadable"])

    def test_inherited_color_flags_cannot_change_the_receipt_grammar(self):
        self.fixture_suite("""import unittest

class ColoredFailure(unittest.TestCase):
    def test_colored_failure(self):
        self.fail("red")
""")
        with mock.patch.dict(os.environ, {"PYTHON_COLORS": "1",
                                          "FORCE_COLOR": "1"}):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "tests.test_gate_receipt_fixture.ColoredFailure."
                         "test_colored_failure")

    def test_child_emitted_ansi_is_normalized_after_environment_suppression(self):
        lines = ["=" * 70,
                 "ERROR: test_red (tests.test_probe.Probe.test_red)",
                 "-" * 70,
                 "Traceback (most recent call last):",
                 "  File \"/tmp/test_probe.py\", line 7, in test_red",
                 "    raise ValueError('red')",
                 "ValueError: red", "", "-" * 70,
                 "Ran 1 test in 0.1s", "", "FAILED (errors=1)"]
        protocol = "".join("\x1b[31m%s\x1b[0m\n" % line
                           for line in lines)
        code = ("import os,sys; os.environ['PYTHON_COLORS']='1'; "
                "sys.stderr.write(%r); raise SystemExit(1)" % protocol)
        with mock.patch.object(gate, "SUITE", ("-c", code)):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        self.assertFalse(row["failures_unreadable"])
        self.assertEqual(row["failures"][0]["test"],
                         "tests.test_probe.Probe.test_red")

    def test_interpreter_is_read_from_the_runtime_never_baked_in(self):
        """THE ASSERTION ABOVE IS NOT ENOUGH, and the mutation pass proved it:
        replacing `impl.name` with the literal "cpython" left every test GREEN,
        because on this box sys.implementation.name IS "cpython" and an
        expected value that equals the machine's own constant cannot see a
        constant in the code. The property that actually matters is that the
        identity is READ from the runtime — so move the runtime and require the
        receipt to follow it."""
        fake = types.SimpleNamespace(name="graalpy", version=(24, 1, 0))
        with mock.patch.object(sys, "implementation", fake), \
                mock.patch.object(sys, "version_info", (3, 12, 8, "final", 0)), \
                mock.patch.object(sys, "executable", os.path.join(self.tmp, "gp")):
            ident = gate.interpreter()
        self.assertEqual(ident["name"], "graalpy")
        self.assertEqual(ident["version"], "24.1.0")
        self.assertEqual(ident["language"], "3.12.8")
        self.assertEqual(ident["executable"], os.path.join(self.tmp, "gp"))
        self.assertEqual(gate.interpreter_label(ident), "graalpy-3.12.8")
        # and the one rename the label makes, so the two pythons this module
        # exists for read as `graalpy-3.12.8` and `CPython-3.14.4`
        self.assertEqual(gate.interpreter_label(
            {"name": "cpython", "language": "3.14.4"}), "CPython-3.14.4")

    def test_a_timeout_is_unknown_and_still_minted(self):
        row, err = gate.run(repo=self.repo, timeout=0.4,
                            argv=[sys.executable, "-c",
                                  "import time; time.sleep(30)"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIsNone(row["rc"])

    def test_ambiguous_prefix_refuses_rather_than_picking_one(self):
        """Two real receipts, forced to share a prefix. The earlier version of
        this branched on whether two content hashes happened to collide in
        their first four characters — which is essentially never, so the
        assertion it guarded had never once run."""
        rows = [self.mint("Ran %d tests in 0.1s" % n, "", "OK") for n in (1, 2)]
        pair = [dict(rows[0], id="abcd" + "1" * 12),
                dict(rows[1], id="abcd" + "2" * 12)]
        with mock.patch.object(gate, "receipts", return_value=(pair, None, 0)):
            row, err = gate.by_id("abcd")
            self.assertIsNone(row)
            self.assertIn("matches 2 receipts", err)
            # and the CONTROL: a prefix that names exactly one still resolves
            got, err = gate.by_id("abcd1")
            self.assertIsNone(err, err)
            self.assertEqual(got["id"], pair[0]["id"])


class CrossFamilyRound1(GateBase):
    """The three blockers a cross-family review reproduced against a live tip.
    Each is a way to reach VERIFIED without having run a green suite on the
    named tree."""

    def test_a_plausible_footer_and_a_nonzero_exit_is_UNKNOWN(self):
        """The runner's SUMMARY and the runner's EXIT CODE are two independent
        signals, and a receipt that reads only the first believes text over the
        process that produced it. The review's repro: a child printing `OK` then
        exiting 9 minted status=OK, rc=9, bind=VERIFIED."""
        row, err = gate.run(repo=self.repo, argv=[
            sys.executable, "-c",
            "print('Ran 3 tests in 0.1s'); print(); print('OK'); "
            "raise SystemExit(9)"])
        self.assertIsNone(err, err)
        self.assertEqual(row["rc"], 9)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIn("two sources disagree", row["detail"])
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_run_that_DIRTIES_the_tree_cannot_bind(self):
        """One tree read cannot witness a change made during the thing it
        describes. The review's repro: a child modified a tracked file and printed
        OK; the receipt recorded dirty=False from BEFORE the child and bound."""
        row, err = gate.run(repo=self.repo, argv=[
            sys.executable, "-c",
            "open(%r, 'a').write('mutated\\n'); print('Ran 1 test in 0.1s'); "
            "print(); print('OK')" % os.path.join(self.repo, "a.txt")])
        self.assertIsNone(err, err)
        self.assertFalse(row["dirty"])          # before: genuinely clean
        self.assertTrue(row["dirty_after"])     # after: the run made it dirty
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("DIRTY", why)

    def test_a_run_that_MOVES_HEAD_cannot_bind(self):
        """The other half of the bracket: a clean tree at both ends is not the
        SAME tree at both ends."""
        row, err = gate.run(repo=self.repo, argv=[
            sys.executable, "-c",
            "import subprocess as s; "
            "open(%r,'a').write('x\\n'); "
            "s.run(['git','add','-A'],cwd=%r); "
            "s.run(['git','commit','-qm','moved'],cwd=%r); "
            "print('Ran 1 test in 0.1s'); print(); print('OK')"
            % (os.path.join(self.repo, "a.txt"), self.repo, self.repo)])
        self.assertIsNone(err, err)
        self.assertNotEqual(row["head"], row["head_after"])
        self.assertFalse(row["dirty_after"])    # clean, but a DIFFERENT tree
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED", why)
        self.assertIn("MOVED", why)

    def test_a_HAND_WRITTEN_receipt_line_does_not_resolve(self):
        """The review's addendum: `_receipt_id` is a content hash that nothing ever
        recomputed, so one appended JSON line with an invented id, the target
        head, dirty=false, status=OK and a made-up interpreter bound VERIFIED
        without any command having run."""
        self.mint("Ran 1 test in 0.1s", "", "OK")          # create the ledger
        forged = {"v": 1, "event": "gate", "ts": "2026-07-31T00:00:00Z",
                  "id": "deadbeefcafebabe", "repo_id": self.repo,
                  "head": self.head, "tree": "f" * 40, "dirty": False,
                  "head_after": self.head, "tree_after": "f" * 40,
                  "dirty_after": False, "rc": 0, "suite": True,
                  "interpreter": {"name": "cpython", "version": "3.14.4",
                                  "language": "3.14.4", "executable": "/x"},
                  "argv": ["/x", "-m", "unittest"], "status": "OK",
                  "ran": 5115, "skipped": 8, "detail": "skipped=8"}
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(forged) + "\n")
        self.assertNotEqual(forged["id"], gate._receipt_id(forged))
        row, err = gate.by_id("deadbeefcafebabe")
        self.assertIsNone(row)
        # The REFUSAL is unchanged; only the SENTENCE is honest now. This used
        # to read "no minted gate receipt deadbeefcafebabe — run `helm gate
        # run`", which is false about a row sitting in the ledger and sends the
        # reader to re-run a suite that was never the problem.
        self.assertIn("IS in the ledger", err)
        self.assertIn("does NOT recompute", err)
        self.assertIn(gate._receipt_id(forged), err)   # what helm computed
        self.assertNotIn("no minted gate receipt", err)
        state, _rid, why = gate.bind("gate:deadbeefcafebabe ok", self.head)
        self.assertEqual(state, "REFUSED", why)

    def test_a_TRULY_ABSENT_id_still_says_absent_and_advises_the_gate(self):
        """The control for the arm above: when nothing in the ledger carries
        the token, the old sentence is the RIGHT one and must survive. A change
        that made every miss say "IS in the ledger" would be worse than the
        conflation it replaced."""
        self.mint("Ran 1 test in 0.1s", "", "OK")
        row, err = gate.by_id("a" * 16)
        self.assertIsNone(row)
        self.assertIn("no minted gate receipt", err)
        self.assertIn("run `helm gate run`", err)
        self.assertNotIn("IS in the ledger", err)

    def test_a_receipt_from_a_NEWER_helm_says_update_not_re_run(self):
        """The live case, and the reason the sentence matters more than the
        verdict. A receipt minted by a helm whose id grammar this one does not
        know recomputes differently and is dropped — indistinguishable, before
        this change, from a receipt that does not exist. The advice mattered:
        "run `helm gate run`" mints ANOTHER receipt this helm cannot read, so
        the reader loops. Measured live: a reviewer traced three functions to
        find out why an APPROVE could not bind."""
        self.mint("Ran 1 test in 0.1s", "", "OK")   # a real, resolvable row
        future = {"v": 99, "event": "gate", "ts": "2026-08-04T00:00:00Z",
                  "id": "b" * 16, "repo_id": self.repo,
                  "head": self.head, "tree": "f" * 40, "dirty": False,
                  "head_after": self.head, "tree_after": "f" * 40,
                  "dirty_after": False, "rc": 0, "suite": True,
                  "interpreter": {"name": "cpython", "version": "3.14.4",
                                  "language": "3.14.4", "executable": "/x"},
                  "argv": ["/x", "-m", "unittest"], "status": "OK",
                  "ran": 5115, "skipped": 8, "detail": "skipped=8"}
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(future) + "\n")
        row, err = gate.by_id("b" * 16)
        self.assertIsNone(row)
        self.assertIn("IS in the ledger", err)
        self.assertIn("NEWER helm", err)
        self.assertIn("99", err)                   # names the version it saw
        self.assertIn("WRONG move", err)           # do not just re-run
        # POSITIVE CONTROL on the same ledger, unconditional: the honest row
        # minted above still resolves, so the ledger is readable and the
        # refusal above is about THIS row rather than a broken lookup.
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(rows), 1)

    def test_ONE_malformed_row_does_not_wedge_the_whole_ledger(self):
        """Cross-family review round 2. eventledger strict mode accepts any JSON object with
        a non-empty id, and the content-id recompute then reached into it — so
        a single line carrying `"interpreter": "not-an-object"` made receipts()
        and every bind() raise AttributeError. One bad row hid every honest
        receipt behind it, which is worse than the forgery it was added to
        stop."""
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 4 tests in 0.1s", "", "OK"))):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        with open(gate.receipts_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "deadbeef",
                                 "interpreter": "not-an-object"}) + "\n")
            # A SECOND, DIFFERENT breakage on purpose. `_ident_of` cannot
            # save this one — the argv list holds non-strings, so the join
            # inside _receipt_id raises TypeError — which is what binds the
            # BROAD except rather than a per-shape one. With only the
            # interpreter row here, the coercion and the guard each covered
            # for the other and the mutation pass showed BOTH surviving.
            fh.write(json.dumps({"id": "beefdead", "argv": [1, 2],
                                 "interpreter": {"name": "x"}}) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 2)
        self.assertEqual([r["id"] for r in rows], [good["id"]])
        # the honest receipt still resolves and still binds THROUGH the mess
        state, rid, why = gate.bind(gate.evidence_line(good), self.head)
        self.assertEqual(rid, good["id"])
        self.assertEqual(state, "VERIFIED", why)
        # and the malformed ids resolve to nothing rather than raising
        # and the malformed ids resolve to nothing rather than raising —
        # PRESENT AND UNJUDGEABLE, which is a different answer from absent.
        # These two are dropped for DIFFERENT reasons and get different
        # sentences: deadbeef's id disagrees, beefdead cannot be computed at
        # all (a non-string in argv makes the join raise). A resolver that
        # only handled the first would still call the second missing.
        row, err = gate.by_id("deadbeef")
        self.assertIsNone(row)
        self.assertIn("IS in the ledger", err)
        self.assertIn("does NOT recompute", err)
        row, err = gate.by_id("beefdead")
        self.assertIsNone(row)
        self.assertIn("cannot compute an id for it at all", err)
        self.assertIn("unjudgeable", err)
        for token in ("deadbeef", "beefdead"):
            _row, err = gate.by_id(token)
            self.assertNotIn("no minted gate receipt", err)

    def test_ident_coercion_is_TYPE_checked_not_truthiness_checked(self):
        """Binds the coercion on its OWN, not through the loop's guard. The
        mutation pass caught this: reverting `_ident_of` to `or {}` left every
        test green, because the broad except in receipts() swallowed the
        AttributeError and skipped the row for a different reason than the one
        under test. Two guards covering for each other means neither is
        measured."""
        for junk in ("not-an-object", ["nor", "this"], 7, None, ""):
            self.assertEqual(gate._ident_of({"interpreter": junk}), {}, junk)
        real = {"name": "cpython", "language": "3.14.4"}
        self.assertEqual(gate._ident_of({"interpreter": real}), real)
        self.assertEqual(gate._ident_of({}), {})

    def test_an_UNREADABLE_ledger_REPORTS_rather_than_raising(self):
        """Cross-family review round 3. Widening receipts() to three values and leaving the
        UNAVAILABLE early exit at two made both callers raise ValueError — the
        ledger said "I cannot be read" and helm crashed instead of saying so.

        THIRD TIME IN THIS LANE THE ERROR PATH WAS THE BROKEN ONE: the
        AttributeError refusal, the raising rejection, now the arity. Each was
        a guard whose success path every test exercised and whose failure path
        none did — so this one asserts the failure path directly."""
        with mock.patch.object(gate.eventledger, "checked_events",
                               return_value=([], "EIO")):
            rows, unavailable, skipped = gate.receipts()
            self.assertEqual((rows, unavailable, skipped), ([], "EIO", 0))
            row, err = gate.by_id("abcd")           # must not raise
            self.assertIsNone(row)
            self.assertIn("EIO", err)
            state, _rid, why = gate.bind("gate:abcd x", self.head)
            self.assertEqual(state, "REFUSED", why)
            self.assertEqual(gate.cmd_gate(["list"]), 1)

    def test_an_EDITED_receipt_stops_resolving(self):
        """The same guard from the other side, and the reason it is worth
        having even though no content hash can stop a determined forger: a row
        that no longer matches what was minted is rejected rather than read."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        lines[-1]["status"] = "OK"
        lines[-1]["ran"] = 99999                      # a number nobody ran
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, err)

    def test_failure_diagnostics_are_bound_into_the_receipt_identity(self):
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        lines[-1]["failures"] = [{"kind": "ERROR", "test": "invented.test",
                                  "traceback": "invented frame"}]
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, err)
        # v3 is a version this helm KNOWS, so the row was CHANGED after
        # minting — the message must say tamper, never "re-run the gate".
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED", err)
        self.assertNotIn("no minted gate receipt", err)

    def test_original_pre_bracket_v1_receipt_keeps_its_frozen_hash(self):  # noqa: VACUOUS_ASSERTION — frozen id and round-trip are positive controls
        legacy = {
            "v": 1, "event": "gate", "ts": "2026-07-30T12:00:00Z",
            "head": "a" * 40, "tree": "b" * 40, "dirty": False,
            "repo_id": "/tmp/repo",
            "interpreter": {"name": "cpython", "language": "3.14.4",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 7, "skipped": 1, "rc": 0,
            "id": "dc0cbc2edaa370ab",
        }
        self.assertEqual(gate._receipt_id(legacy), legacy["id"])
        self.assertTrue(gate.eventledger.append(gate.receipts_path(), legacy))
        back, err = gate.by_id(legacy["id"])
        self.assertIsNone(err, err)
        self.assertEqual(back["id"], legacy["id"])
        partial = dict(legacy, head_after=legacy["head"])
        with self.assertRaisesRegex(ValueError, "partial v1"):
            gate._receipt_id(partial)

    def test_v1_receipts_keep_their_FROZEN_content_identity_grammar(self):
        legacy = {
            "v": 1, "event": "gate", "ts": "2026-07-31T12:34:56Z",
            "head": "a" * 40, "tree": "b" * 40, "dirty": False,
            "head_after": "a" * 40, "tree_after": "b" * 40,
            "dirty_after": False, "repo_id": "/tmp/repo",
            "interpreter": {"name": "cpython", "language": "3.14.4",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 7, "skipped": 1, "rc": 0,
            "id": "a2b8be1a4f65aa62",
        }
        self.assertEqual(gate._receipt_id(legacy), "a2b8be1a4f65aa62")
        self.assertTrue(gate.eventledger.append(gate.receipts_path(), legacy))
        back, err = gate.by_id(legacy["id"])
        self.assertIsNone(err, err)
        self.assertEqual(back["id"], legacy["id"])
        injected = dict(legacy, failures=[{
            "kind": "ERROR", "test": "invented.test", "traceback": "fake"}],
                        failures_unreadable=False)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_failures(injected)
        self.assertIn("UNAVAILABLE", stdout.getvalue())
        self.assertNotIn("invented.test", stdout.getvalue())

    def test_hash_valid_malformed_fields_render_safely_on_one_line(self):  # noqa: VACUOUS_ASSERTION — each receipt id is the positive rendered control
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            base = [json.loads(line) for line in fh if line.strip()][-1]
        rows = [dict(base, skipped="many"),
                dict(base, interpreter="bad"),
                dict(base, tree=7),
                dict(base, interpreter={"name": "cp\nforged",
                                        "language": "3.14.4"}),
                dict(base, status="OK\nFORGED")]
        for stored in rows:
            stored["id"] = gate._receipt_id(stored)
        with open(path, "w", encoding="utf-8") as fh:
            for stored in rows:
                fh.write(json.dumps(stored) + "\n")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(gate.cmd_gate(["list"]), 0)
            for stored in rows:
                self.assertEqual(gate.cmd_gate(["show", stored["id"]]), 0)
        shown = stdout.getvalue()
        for stored in rows:
            self.assertIn(stored["id"], shown)
            self.assertNotIn("\n", gate.evidence_line(stored))
        self.assertNotIn("(skipped=", gate.evidence_line(rows[0]))
        self.assertIn("UNKNOWN", gate.evidence_line(rows[1]))
        self.assertIn("tree=7", gate.evidence_line(rows[2]))
        self.assertIn("cp forged-3.14.4", gate.evidence_line(rows[3]))
        self.assertIn("OK FORGED", gate.evidence_line(rows[4]))
        self.assertNotIn("\nFORGED", shown)
        self.assertNotEqual(row["id"], rows[0]["id"])

    def test_a_REAL_receipt_survives_the_disk_round_trip(self):
        """THE CONTROL FOR BOTH ABOVE. Without it, a recompute that never
        matches anything would pass every forgery test by rejecting the whole
        ledger — the census-reports-zero failure, one layer down."""
        row = self.mint("Ran 7 tests in 0.3s", "", "OK")
        back, err = gate.by_id(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(back["id"], row["id"])
        self.assertEqual(back["ran"], 7)

    def test_a_receipt_with_no_post_run_read_REFUSES(self):
        """A pre-bracket receipt was never bracketed, and reading its missing
        half as 'unchanged' is the pass-whose-input-was-missing this module
        exists to refuse."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        stripped = dict(row)
        stripped.pop("head_after")
        with mock.patch.object(gate, "receipts",
                               return_value=([stripped], None, 0)):
            state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no post-run tree read", why)


class Binding(GateBase):
    def test_no_token_is_unverified_not_refused(self):
        state, rid, why = gate.bind("whole-suite Ran 5115 OK", self.head)
        self.assertEqual(state, "UNVERIFIED")
        self.assertIsNone(rid)

    def test_a_token_naming_nothing_is_refused(self):
        state, _, why = gate.bind("gate:deadbeefdeadbeef ok", self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no minted gate receipt", why)

    def test_a_receipt_for_another_commit_is_refused(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self._dirty()
        self._git("add", "-A")
        self._git("commit", "-qm", "second")
        moved = self._git("rev-parse", "HEAD")
        self.assertNotEqual(moved, self.head)
        state, _, why = gate.bind(gate.evidence_line(row), moved)
        self.assertEqual(state, "REFUSED")
        self.assertIn("re-run the gate on the reviewed tip", why)

    def test_a_dirty_receipt_is_refused(self):
        self._dirty()
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        state, _, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("DIRTY", why)

    def test_a_failing_receipt_is_refused(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "FAILED (failures=1)")
        state, _, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("FAILED", why)

    def test_a_custom_command_can_never_bind(self):
        """helm did not choose the interpreter, so it cannot name it, so the
        receipt does not answer the question the gate exists to answer."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertIsNone(row["interpreter"])
        state, _, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("custom command", why)

    def test_a_clean_green_suite_receipt_verifies(self):
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        state, rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "VERIFIED")
        self.assertEqual(rid, row["id"])
        self.assertIn(sys.implementation.name.replace("cpython", "CPython"), why)

    def test_a_later_receipt_that_contains_the_reviewed_tip_verifies(self):
        reviewed, descendant, row = self.descendant_receipt()
        state, rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, row["id"])
        self.assertIn(reviewed[:12], why)
        self.assertIn(descendant[:12], why)

    def test_descendant_binding_uses_the_dispatch_repo_not_receipt_scratch(self):
        reviewed, _descendant, row = self.descendant_receipt()
        scratch_gone = dict(row, repo_id=os.path.join(self.tmp, "gone-scratch.git"))
        with mock.patch.object(gate, "receipts",
                               return_value=([scratch_gone], None, 0)):
            state, rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, row["id"])

    def test_a_descendant_receipt_must_postdate_the_review(self):
        reviewed, _descendant, row = self.descendant_receipt(
            receipt_ts="2026-08-02T00:00:01Z")
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:02Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not postdate", why)

    def test_a_divergent_receipt_does_not_bind(self):
        reviewed = self.head
        self._git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "sibling.txt"), "w") as fh:
            fh.write("divergent review\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "divergent reviewed tip")
        sibling = self._git("rev-parse", "HEAD")
        self._git("checkout", "-q", "lane/probe")
        with mock.patch.object(
                gate, "SUITE", tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))), \
                mock.patch.object(
                    gate.pk, "now_ts", return_value="2026-08-02T00:00:02Z"):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["head"], reviewed)
        state, _rid, why = gate.bind(
            gate.evidence_line(row), sibling, repo_id=self.gitdir(),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not contain", why)

    def test_unknown_descendant_ancestry_refuses(self):
        reviewed, _descendant, row = self.descendant_receipt()
        backend = mock.Mock()
        backend.ancestry.return_value = gate.vcs.UNKNOWN
        with mock.patch.object(gate.vcs, "backend", return_value=backend):
            state, _rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("cannot be proven to contain", why)

    def test_descendant_binding_needs_the_standing_repository(self):
        reviewed, _descendant, row = self.descendant_receipt()
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed,
            repo_id=os.path.join(self.tmp, "missing.git"),
            reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("repository is unreadable", why)

    def test_descendant_binding_needs_two_canonical_timestamps(self):
        reviewed, _descendant, row = self.descendant_receipt()
        state, _rid, why = gate.bind(
            gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
            reviewed_ts="not-a-time")
        self.assertEqual(state, "REFUSED")
        self.assertIn("no canonical opening timestamp", why)
        broken = dict(row, ts="not-a-time")
        with mock.patch.object(gate, "receipts",
                               return_value=([broken], None, 0)):
            state, _rid, why = gate.bind(
                gate.evidence_line(row), reviewed, repo_id=self.gitdir(),
                reviewed_ts="2026-08-02T00:00:01Z")
        self.assertEqual(state, "REFUSED")
        self.assertIn("no canonical timestamp", why)


class VerdictBinding(GateBase):
    """The chokepoint: every gate claim in helm enters through mark_verdict."""

    def _dispatch(self, tip):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True, new_work=True)
        self.assertIsNone(err, err)
        self.assertIsNotNone(row, "fixture dispatch was not persisted")
        return row

    def test_a_verdict_citing_another_commits_receipt_is_refused(self):
        """MUTATION-BOUND: delete the gate.bind call in mark_verdict and this
        test goes red. The verdict below is well-formed in every way the old
        code checked — right dispatch, right reviewed tip, evidence under 256
        printable characters — and is a lie only about the thing nothing used
        to read."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")   # minted at self.head
        self._dirty()
        self._git("add", "-A")
        self._git("commit", "-qm", "second")
        moved = self._git("rev-parse", "HEAD")
        d = self._dispatch(moved)
        got, err = dispatches.mark_verdict(
            d["id"], moved, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("gate evidence does not bind", err)

    def test_untokened_FIX_records_unverified_and_is_not_blocked(self):
        """A negative verdict authorizes no land, so it remains actionable
        without making the reviewer pay a whole-suite gate first."""
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, "3 blockers", "fix")
        self.assertIsNone(err, err)
        self.assertEqual(got["polarity"], "fix")
        self.assertEqual(got["gate"], "")
        self.assertEqual(dispatches.gate_state(got), "UNVERIFIED")

    def test_a_bound_verdict_records_the_receipt_id(self):
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])
        self.assertEqual(dispatches.gate_state(got), "VERIFIED " + row["id"])

    def test_same_lane_movement_refuses_before_gate_bind_or_epoch(self):  # noqa: VACUOUS_ASSERTION — verified receipt and named movement positively control the intentional no-write assertions
        reviewed = self.head
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:01Z"):
            d = self._dispatch(reviewed)
        self.assertEqual(d.get("ref_branch"), "refs/heads/lane/probe")
        with open(os.path.join(self.repo, "same-lane.txt"), "w") as fh:
            fh.write("same lane moved\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "same lane moved")
        moved = self._git("rev-parse", "HEAD")
        self.assertNotEqual(moved, reviewed)
        self.assertEqual(self._git("rev-parse", "lane/probe"), moved)
        with mock.patch.object(
                gate, "SUITE", tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))), \
                mock.patch.object(
                    gate.pk, "now_ts",
                    return_value="2026-08-02T00:00:02Z"):
            receipt, receipt_err = gate.run(repo=self.repo)
        self.assertIsNone(receipt_err, receipt_err)
        state, rid, why = gate.bind(
            gate.evidence_line(receipt), reviewed, repo_id=d["repo_id"],
            reviewed_ts=d["ts"])
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(rid, receipt["id"])
        before = dispatches.history(d["id"])
        self.assertEqual([e.get("event") for e in before], ["dispatch"])
        self.assertFalse(os.path.exists(dispatches.epoch_path()))
        with mock.patch.object(gate, "bind", wraps=gate.bind) as bind, \
                mock.patch.object(
                    dispatches, "record_gate_epoch",
                    wraps=dispatches.record_gate_epoch) as epoch:
            got, err = dispatches.mark_verdict(
                d["id"], reviewed, gate.evidence_line(receipt), "approve")
        self.assertIsNone(got)
        self.assertIn("moved under this review", err)
        bind.assert_not_called()
        epoch.assert_not_called()
        after = dispatches.history(d["id"])
        self.assertEqual(after, before)
        self.assertFalse(any(e.get("event") == "verdict" for e in after))
        self.assertFalse(os.path.exists(dispatches.epoch_path()))

    def test_a_verdict_binds_a_later_receipt_containing_its_tip(self):  # noqa: VACUOUS_ASSERTION — stored gate id plus exactly-one appended verdict are positive controls on the successful writer
        reviewed = self.head
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:01Z"):
            d = self._dispatch(reviewed)
        _reviewed, descendant, row = self.descendant_receipt()
        before = len(dispatches.history(d["id"]))
        got, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])
        self.assertNotEqual(descendant, reviewed)
        self.assertEqual(len(dispatches.history(d["id"])), before + 1)
        again, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)
        self.assertEqual(again["gate"], row["id"])
        self.assertEqual(len(dispatches.history(d["id"])), before + 1)

    def test_a_predated_descendant_receipt_appends_no_verdict(self):
        reviewed = self.head
        with mock.patch.object(
                dispatches.pk, "now_ts",
                return_value="2026-08-02T00:00:02Z"):
            d = self._dispatch(reviewed)
        _reviewed, _descendant, row = self.descendant_receipt(
            receipt_ts="2026-08-02T00:00:01Z")
        before = len(dispatches.history(d["id"]))
        got, err = dispatches.mark_verdict(
            d["id"], reviewed, gate.evidence_line(row), "approve")
        self.assertIsNone(got)
        self.assertIn("does not postdate", err)
        self.assertEqual(len(dispatches.history(d["id"])), before)

    def test_an_idempotent_retry_answers_in_the_SAME_shape(self):
        """The first draft returned gate_state/gate_why beside `gate`, and the
        retry path — which early-returns the REPLAYED row — could not carry
        them, so a retry answered in a different shape than the original call.
        tests/test_dispatches caught it at the land gate. `gate` is the one
        recorded field and gate_state() derives the rest, so there is nothing
        left to disagree."""
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        ev = gate.evidence_line(row)
        first, err = dispatches.mark_verdict(
            d["id"], self.head, ev, "approve")
        self.assertIsNone(err, err)
        again, err = dispatches.mark_verdict(
            d["id"], self.head, ev, "approve")
        self.assertIsNone(err, err)
        self.assertEqual(set(again), set(first))
        self.assertEqual(again["gate"], first["gate"])

    def test_replay_never_invents_a_binding_history_lacks(self):
        """A pre-gate verdict event carries no `gate` key, and "" is the TRUE
        reading of it. If replay defaulted to anything else, every verdict
        recorded before today would retroactively claim a run."""
        d = self._dispatch(self.head)
        self.assertTrue(dispatches.eventledger.append(
            dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": d["seq"] + 1,
                "id": d["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.head, "verdict_ref": "prose only",
                "polarity": "approve"}))
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        replayed = current[d["id"]]
        self.assertEqual(replayed["status"], "verdict")
        self.assertEqual(replayed.get("gate"), "")


class PolicyKindCaseTest(GateBase):
    """A live bypass found by a cross-family review, reproduced THROUGH THE
    DOCUMENTED WRITER.

    `policy_kind` was compared after .strip() and never casefolded, on both the
    write and the read side. So a policy written as `Approval-Tier` — one
    capital, every validation passing, through the real writer — is INVISIBLE
    to a reader asking for `approval-tier`. The land gate then reads "none",
    and none PERMITS. The operator believes the tier is enforced while the gate
    sees an empty store: a silent bypass of the whole mechanism, from a
    keystroke.

    ONE PLACE IS NOT ENOUGH IN EITHER DIRECTION, which is the part worth
    keeping. Fixing only the WRITE leaves every row already on disk invisible;
    fixing only the READ lets new mixed-case rows keep arriving. A
    normalization has to be shared by the writer and the reader or it is not a
    normalization, it is a convention."""

    def _write(self, kind):
        from helm import store
        store.write_prior({
            "id": "tier-case-probe", "type": "prior", "class": "certain",
            "confidence": 1.0, "status": "live", "policy_kind": kind,
            "policy_members": ["seat:kimi"], "policy_reason": "probe",
            "statement": "probe", "source": "test", "keywords": "probe",
        })

    def test_a_MIXED_CASE_kind_is_still_found(self):
        from helm import store
        self._write("Approval-Tier")
        self.assertTrue(store.policy_declared("approval-tier"),
                        "a policy written as Approval-Tier is invisible to a "
                        "reader asking for approval-tier — the land gate then "
                        "reads 'none', and none PERMITS")

    def test_the_lookup_is_case_insensitive_from_BOTH_ends(self):
        from helm import store
        self._write("approval-tier")
        for asked in ("approval-tier", "Approval-Tier", "APPROVAL-TIER"):
            self.assertTrue(store.policy_declared(asked), asked)

    def test_a_LEGACY_mixed_case_row_ON_DISK_is_found(self):
        """THIS IS WHAT THE READ-SIDE CASEFOLD ALONE BUYS, and the mutation
        pass proved I needed it: with both sides folding, reverting EITHER left
        every test green because the other covered it — the belt-and-braces
        trap the review named in the same breath as the bug.

        A row written BEFORE the writer canonicalized keeps its mixed case on
        disk forever. Only the reader can save it, so this writes past the
        writer's normalization to make one."""
        from helm import store
        self._write("approval-tier")
        # rewrite the stored row's kind in place — a legacy artifact the
        # canonicalizing writer will never produce again
        import glob, os as _os
        # WHERE THE WRITER ACTUALLY PUT IT, measured rather than assumed: my
        # first version globbed adopted_memory_dir(), which resolves to the
        # OWNER'S real memory dir and not this test's scratch HELM_HOME — the
        # fixture row was never there, so the test failed for a reason that had
        # nothing to do with the property under test.
        hits = glob.glob(_os.path.join(_os.environ["HELM_HOME"], "_global",
                                       "premises", "*tier-case-probe*"))
        self.assertTrue(hits, "fixture row not on disk")
        for path in hits:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body.replace("policy_kind: approval-tier",
                                      "policy_kind: Approval-Tier"))
        self.assertTrue(store.policy_declared("approval-tier"),
                        "a legacy mixed-case row on disk is invisible — only "
                        "the READ-side casefold can reach it")

    def test_the_WRITER_canonicalizes_so_the_store_stops_drifting(self):
        """AND THIS IS WHAT THE WRITE SIDE ALONE BUYS. Reading tolerantly makes
        lookups work; it does not stop the store accumulating a variant per
        keystroke. The stored row must be canonical."""
        from helm import store
        self._write("Approval-Tier")
        rows = [e for e in store.load_all(include_retired=True, types=("prior",))
                if str(e.get("id")) == "tier-case-probe"]
        self.assertTrue(rows, "fixture row did not load back")
        self.assertEqual(str(rows[0].get("policy_kind")), "approval-tier",
                         "the writer stored the kind uncanonicalized")

    def test_an_UNRELATED_kind_is_still_absent(self):
        """THE CONTROL. Without it the two above pass for a version where
        policy_declared always says yes — which would 'fix' the bypass by
        removing the answer."""
        from helm import store
        self._write("Approval-Tier")
        self.assertFalse(store.policy_declared("no-such-policy-kind"))


class ApprovalTierAtLandTest(GateBase):
    """The tier was a SEND-TIME warning nothing ever read again.

    MEASURED live: an out-of-tier APPROVE was RECORDED on the ledger and
    the row read READY. Only one agent noticing held the land. A policy
    enforced socially is a review council's single finding — helm's claims about
    itself are not checked — operating on helm's own governance."""

    def _dispatch(self, tip, recipient="reviewer"):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add(recipient, "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True, new_work=True)
        self.assertIsNone(err, err)
        return row

    def _land_state(self, tier):
        d = self._dispatch(self.head)
        with mock.patch.object(dispatches, "GATE_CAPS", ()):
            _got, err = dispatches.mark_verdict(d["id"], self.head, "ok",
                                                polarity="approve")
        self.assertIsNone(err, err)
        with mock.patch.object(dispatches, "approval_tier", return_value=tier):
            lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        return lr

    def test_an_OUTSIDE_reviewer_never_reaches_READY(self):
        lr = self._land_state(("outside", "@gemini is outside the tier"))
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("approval tier does not permit", lr["ungated"])
        self.assertIn("outside the tier", lr["ungated"])

    def test_an_UNEVALUABLE_tier_never_reaches_READY(self):
        """UNKNOWN never authorizes — a tier we cannot read cannot permit a
        merge, and this is the same law the gate epoch and the gc fetch both
        settled on."""
        lr = self._land_state(("unknown", "policy p1 has malformed selector"))
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("malformed selector", lr["ungated"])

    def test_NO_TIER_permits_and_IN_TIER_permits(self):
        """THE CONTROL, both halves. Without it the two refusals above pass for
        a version that never lets anything be READY — 'safe' and useless. A
        fleet with no declared tier has nothing to be outside of; that is an
        answer, not an absence."""
        self.assertEqual(self._land_state(("none", "no policy"))["state"],
                         "READY")
        self.assertEqual(self._land_state(("ok", None))["state"], "READY")

    def test_a_RAISING_tier_check_refuses_rather_than_breaking_the_board(self):
        with mock.patch.object(dispatches, "approval_tier",
                               side_effect=RuntimeError("boom")):
            d = self._dispatch(self.head)
            with mock.patch.object(dispatches, "GATE_CAPS", ()):
                dispatches.mark_verdict(d["id"], self.head, "ok",
                                        polarity="approve")
            lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        # the except supplies its OWN reason; "could not be evaluated" is the
        # fallback for a None why and is never reached here. Asserting the
        # unreachable branch would have passed only if the guard had failed.
        self.assertIn("approval-tier check raised", lr["ungated"])


class LandPathEnforcement(GateBase):
    """The review's third blocker, and the one that decided the shape of this lane.

    Recording a verdict UNVERIFIED changes nothing about whether the work can
    land: `landreq` derived READY from APPROVE polarity alone, so a brand-new
    untokened approve came back gate=UNVERIFIED **and** land_state=READY. The
    review council asked for mechanical enforcement AT THE LAND PATH; marking a claim
    unverified is not enforcing anything.

    Permitting untokened approves forever so nothing in flight breaks would
    preserve the unchecked path as a permanent option. Grandfathering happens
    per ROW instead, and by the WRITER's own capability: a verdict written by
    code that had no gate to run keeps the old derivation forever.
    """

    def _lr_for(self, evidence, gate_capable=True):
        """Record an approve as a GATE-CAPABLE writer (the default) or as an
        old one, then read the land state it produces."""
        d = self._dispatch(self.head)
        with mock.patch.object(
                dispatches, "GATE_CAPS",
                (dispatches.GATE_CAP_RECEIPT,) if gate_capable else ()):
            got, err = dispatches.mark_verdict(d["id"], self.head, evidence,
                                               polarity="approve")
        self.assertIsNone(err, err)
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        return lr

    def _dispatch(self, tip):
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True, new_work=True)
        self.assertIsNone(err, err)
        return row

    def test_an_untokened_APPROVE_does_not_reach_READY(self):
        d = self._dispatch(self.head)
        self.assertTrue(dispatches.eventledger.append(
            dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": d["seq"] + 1,
                "id": d["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.head,
                "verdict_ref": "whole-suite Ran 5115 OK",
                "polarity": "approve", "gate": "",
                "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertEqual(lr["gate"], "")
        self.assertIn("no minted gate receipt", lr["ungated"])

    def test_a_BOUND_approve_reaches_READY(self):
        """The control. Without it the test above passes for a version that
        never lets anything be READY."""
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        lr = self._lr_for(gate.evidence_line(row))
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["gate"], row["id"])
        self.assertIsNone(lr["ungated"])

    def test_a_verdict_from_an_OLD_WRITER_keeps_its_READY(self):
        """Grandfathering ROWS, not the path: a verdict written by code that
        had no gate to run is not retroactively un-readied. The property is
        the WRITER's, stamped on the event — not the hour it was written."""
        lr = self._lr_for("whole-suite Ran 5115 OK", gate_capable=False)
        self.assertEqual(lr["state"], "READY")
        self.assertIsNone(lr["ungated"])

    def test_ABSENCE_is_legacy_only_BEFORE_the_epoch(self):
        """The review's design call, and the third time in this lane the answer was
        "stop using a clock". GATE_CAPS stamps the WRITER, which answers "does
        THIS CHECKOUT have the feature?" — and a reviewer running ./bin/helm
        from a worktree that has not rebased answers NO while the fleet answers
        YES. Measured live: 9 of 12 live worktrees could not stamp, and
        two verdicts written MINUTES AFTER the gate landed were grandfathered
        into READY carrying no receipt.

        The cutover is the APPEND INDEX of the earliest verdict that carries a
        recognized stamp. Before it, absence is a writer that had no gate.
        At or after it, absence is UNKNOWN — the fleet had the gate by then."""
        req = landreq.gate_requirement
        # no epoch at all: a fleet that has never run a gate-capable writer
        self.assertEqual(req({}, index=5, epoch=None), "none")
        # before the cutover — genuinely legacy
        self.assertEqual(req({}, index=3, epoch=7), "none")
        # AT and AFTER the cutover — the bypass this closes
        self.assertEqual(req({}, index=7, epoch=7), "unknown")
        self.assertEqual(req({}, index=9, epoch=7), "unknown")
        # an unknown position cannot convict: no index is not evidence
        self.assertEqual(req({}, index=None, epoch=7), "none")
        # and a PRESENT stamp is judged on its own, epoch irrelevant
        self.assertEqual(req({"gate_caps": ["receipt-v1"]}, index=1, epoch=7),
                         "required")

    def test_END_TO_END_a_stale_writer_after_the_epoch_never_reaches_READY(self):
        """THE WIRING, not the predicate. The mutation pass caught this gap:
        cutting the epoch out of project()'s call left every unit test green,
        because none of them drove a row with an ABSENT stamp through the real
        projection. That is the whole bypass — a reviewer on a stale worktree
        writes a verdict with no gate_caps and the board reads READY."""
        first = self._dispatch(self.head)
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)):
            _got, err = dispatches.mark_verdict(first["id"], self.head,
                                                "stamped round", polarity="fix")
        self.assertIsNone(err, err)          # founds the epoch

        stale = self._dispatch(self.head)
        # a writer with NO gate_caps at all — the pre-gate code path, appended
        # AFTER the epoch, which is exactly the stale-worktree reviewer
        with mock.patch.object(dispatches, "GATE_CAPS", None), \
                mock.patch.object(dispatches, "GATE_POLICY", None, create=True):
            def _no_caps(rid, tip, ev, polarity=None):
                path = dispatches.ledger_path()
                cur, unavailable = dispatches.snapshot()
                row = cur[rid]
                dispatches.eventledger.append(path, {
                    "v": 3, "event": "verdict", "seq": row["seq"] + 1,
                    "id": rid, "ts": dispatches.pk.now_ts(),
                    "reviewed_tip": tip, "verdict_ref": ev,
                    "polarity": polarity})
            _no_caps(stale["id"], self.head, "unstamped approve", "approve")

        lr, err = landreq.get(stale["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        # This fixture stages a STALE WRITER (gate_caps ABSENT), and
        # the refusal now names that cause instead of blaming the row —
        # asserting "unreadable" here was asserting the wrong world.
        self.assertIn("stamped no gate_caps", lr["ungated"])
        self.assertIn("INTACT", lr["ungated"])

    def test_a_LOST_marker_is_UNKNOWN_and_never_re_derived(self):
        """The review's block, and the reason was measured not theoretical: replay
        SILENTLY SKIPS complete malformed rows, so one unparseable founder
        makes the epoch recompute to a LATER position and every unstamped
        verdict in between flips from UNKNOWN back to none — retroactively
        authorized by a corrupted byte, with nothing announcing it.

        So the marker is FROZEN on first stamped write and read thereafter. A
        marker that should exist and does not is EPOCH_LOST: total, loud, and
        never a fresh derivation."""
        d = dispatches
        # stamped verdicts on the ledger, no marker -> LOST, not re-derived
        current = {"a": {"gate_caps": ["receipt-v1"]}}
        with mock.patch.object(d, "_read_epoch", return_value=None):
            self.assertEqual(d.gate_epoch(current, _verdicts({"a": 5})),
                             d.EPOCH_LOST)
        # and nothing reaches READY while the boundary is unknown
        self.assertEqual(landreq.gate_requirement({}, index=1,
                                                  epoch=d.EPOCH_LOST), "unknown")
        self.assertEqual(landreq.gate_requirement({}, index=99,
                                                  epoch=d.EPOCH_LOST), "unknown")
        # a fleet that NEVER gated is still honestly legacy, not LOST
        with mock.patch.object(d, "_read_epoch", return_value=None):
            self.assertIsNone(d.gate_epoch({"a": {}}, _verdicts({"a": 5})))
        # AND AN UNREADABLE LEDGER IS LOST, NOT LEGACY. The mutation pass
        # caught this branch untested: returning None there would let an
        # unreadable ledger authorize every unstamped approve on the board —
        # a pass whose input was missing, in the one function whose job is
        # refusing exactly that.
        with mock.patch.object(d, "_read_epoch", return_value=None), \
                mock.patch.object(d, "_snapshot",
                                  return_value=({}, {}, "EIO")):
            self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)

    # ------------------------------------------------------------------
    # THE REPLAYED LEDGER. Everything below drives a REAL ledger through a
    # real removal and a real recreation, because the tests these replace
    # injected a founder->position map and the review named exactly that as the
    # gap: an injected map cannot recreate a row, and recreation is the bug.
    # ------------------------------------------------------------------

    def _send(self, key, message="review this"):
        """A dispatch through send(), whose id is DERIVED from
        sender/repo/operation key — so the same key yields the same id, which
        is the whole mechanism of the review's HIGH finding.

        `new_work=True` satisfies the work-identity requirement and does NOT
        participate in the id: with an EXPLICIT operation key the namespace is
        sender/repo_id/key alone, so recreation under the same key still
        reproduces the same id. Declaring a chain here would say these
        independent founders continue one another, which they do not."""
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, why, _sent = dispatches.send("codex-3", "a-lane", message,
                                          self.head, repo=self.repo, key=key,
                                          sign=False, new_work=True)
        self.assertIsNotNone(row, why)
        return row

    def _stamped(self, rid, evidence):
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)):
            _got, err = dispatches.mark_verdict(rid, self.head, evidence,
                                                polarity="fix")
        self.assertIsNone(err, err)

    def _unstamped(self, rid, evidence):
        """The pre-gate writer: a verdict event with NO gate_caps key at all."""
        row = dispatches.snapshot()[0][rid]
        self.assertTrue(dispatches.eventledger.append(
            dispatches.ledger_path(),
            {"v": 3, "event": "verdict", "seq": row["seq"] + 1, "id": rid,
             "ts": dispatches.pk.now_ts(), "reviewed_tip": self.head,
             "verdict_ref": evidence, "polarity": "approve"}))

    def _marker(self):
        with open(dispatches.epoch_path(), encoding="utf-8") as fh:
            return json.load(fh)

    def _ledger(self):
        with open(dispatches.ledger_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _rewrite(self, rows):
        with open(dispatches.ledger_path(), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def _requirement(self, rid):
        """What the REAL projection decides about one row's missing stamp."""
        current, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable, unavailable)
        return landreq.gate_requirement(
            current[rid], index=dispatches.verdict_index(verdicts, rid),
            epoch=dispatches.gate_epoch(current, verdicts))

    def test_a_RECREATED_row_with_the_SAME_ID_cannot_move_the_epoch(self):
        """The review's HIGH finding, replayed exactly as reported.

            founder verdict index 2, later unstamped approval index 5 =>
            UNKNOWN; remove founder rows while retaining marker, retry the same
            operation key (same id), verdict it at index 5 while stale is now
            index 2 => gate_epoch=5 and the stale approval flips to none.

        The marker froze the dispatch WORK-ITEM ID because that id survives
        compaction. But `send()` DERIVES that id from sender/repo/operation
        key, so it also survives DELETION: retry the same operation and the
        same id comes back, at a later position, with the marker following it
        there — and every unstamped approval the boundary stepped over is
        retroactively authorized. Fail-open, which is what makes it a blocker.

        A work-item id says WHICH LOOP. The epoch is an EVENT, so the anchor is
        over the accepted verdict event's content: a legitimately recreated row
        is a DIFFERENT event, and it does not satisfy it."""
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")

        marker = self._marker()
        self.assertEqual(marker["founder"], founder["id"])
        self.assertEqual(marker["index"], 2)            # the review's index 2
        _c, verdicts, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(dispatches.verdict_index(verdicts, stale["id"]), 5)
        self.assertEqual(self._requirement(stale["id"]), "unknown")

        # remove the founder's rows, RETAIN the marker
        self._rewrite([r for r in self._ledger() if r.get("id") != founder["id"]])
        _c, verdicts, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(dispatches.verdict_index(verdicts, stale["id"]), 2)

        # ...and retry the same operation key. This is a LEGITIMATE retry, not
        # a forgery: helm hands the same id back by design.
        again = self._send("op-FOUNDER", "founding review")
        self.assertEqual(again["id"], founder["id"])    # retry_same_id True
        self._stamped(again["id"], "a second stamped round")
        _c, verdicts, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(dispatches.verdict_index(verdicts, founder["id"]), 5)

        # THE ASSERTION. Before the anchor this was epoch=5 and "none".
        self.assertEqual(dispatches.gate_epoch(), dispatches.EPOCH_LOST)
        self.assertEqual(self._requirement(stale["id"]), "unknown")
        lr, err = landreq.get(stale["id"])
        self.assertIsNone(err, err)
        self.assertNotEqual(lr["state"], "READY")

    def test_the_marker_survives_a_REAL_COMPACTION_of_earlier_rows(self):
        """The control for the test above, and it must stay green or the fix is
        just "refuse everything". Removing rows BEFORE the founder moves its
        position and changes nothing about its identity, so the boundary
        follows it down rather than being stranded at a stale number.

        The version this replaces injected {founder: 5} and asserted 5 — which
        is a test of dict.get, not of compaction."""
        early = self._send("op-EARLY", "an earlier loop")
        self._unstamped(early["id"], "old unstamped approve")
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        marker = self._marker()
        self.assertEqual(marker["index"], 5)
        self.assertEqual(dispatches.gate_epoch(), 5)
        # the early loop is compacted away — three rows vanish from BEFORE the
        # founder, and the frozen 5 is now a position the founder does not hold
        self._rewrite([r for r in self._ledger() if r.get("id") != early["id"]])
        self.assertEqual(dispatches.gate_epoch(), 2)
        self.assertNotEqual(dispatches.gate_epoch(), marker["index"])

    def test_a_founder_compacted_AWAY_is_EPOCH_LOST(self):
        """We cannot place any row against a boundary we can no longer locate,
        and re-deriving one is the retroactive authorization this mechanism
        exists to prevent."""
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")
        self.assertEqual(dispatches.gate_epoch(), 2)
        self._rewrite([r for r in self._ledger() if r.get("id") != founder["id"]])
        self.assertEqual(dispatches.gate_epoch(), dispatches.EPOCH_LOST)
        self.assertEqual(self._requirement(stale["id"]), "unknown")
        # and an unreadable ledger under a valid marker is LOST too, never the
        # stale stored index
        with mock.patch.object(dispatches, "_snapshot",
                               return_value=({}, {}, "EIO")):
            self.assertEqual(dispatches.gate_epoch(), dispatches.EPOCH_LOST)

    def test_the_marker_is_FROZEN_and_never_overwritten(self):
        """A marker that already exists IS the answer, even when today's ledger
        would compute a different one. That is the whole point: a number no
        longer being calculated cannot be moved by corrupting its inputs."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        current = {"aaaaaaaa": {"gate_caps": ["receipt-v1"]}}
        five = _verdicts({"aaaaaaaa": 5})
        self.assertIsNotNone(d.record_gate_epoch(current, five))
        self.assertEqual(d.gate_epoch(current, five), 5)
        # A LATER, EARLIER-POSITIONED founder appears. The marker does NOT move
        # to it — a marker that exists IS the answer, even when today's ledger
        # would compute a different one.
        both = {"aaaaaaaa": {"gate_caps": ["receipt-v1"]},
                "bbbbbbbb": {"gate_caps": ["receipt-v1"]}}
        pair = _verdicts({"aaaaaaaa": 5, "bbbbbbbb": 2})
        self.assertIsNone(d.record_gate_epoch(both, pair))
        self.assertEqual(d.gate_epoch(both, pair), 5)
        # and the position is DERIVED from the frozen founder, so compacting
        # the list moves the answer with it rather than stranding it
        self.assertEqual(d.gate_epoch(current, _verdicts({"aaaaaaaa": 1})), 1)

    def test_the_epoch_is_the_EARLIEST_stamped_verdict(self):
        """Not the latest, and not a timestamp. Append order is the only clock
        this needs, and the ledger is append-only so it cannot drift."""
        ep = lambda c, o: dispatches._scan_epoch(c, _verdicts(o))[0]
        self.assertEqual(ep({"a": {"gate_caps": ["receipt-v1"]},
                             "b": {},
                             "c": {"gate_caps": ["receipt-v1"]}},
                            {"a": 10, "b": 4, "c": 22}), 10)
        # a fleet with no stamped verdict has NO epoch — everything is legacy,
        # which is the true reading and not a default
        self.assertIsNone(ep({"b": {}}, {"b": 4}))

    def test_only_the_RECEIPT_capability_founds_the_epoch(self):
        """The review reproduced both of these founding an epoch in my first draft.
        A writer that never advertised the capability cannot mark the moment
        the fleet gained it — an EMPTY set and an unrelated one are honest
        writers WITHOUT the receipt capability, not evidence of it."""
        ep = lambda c, o: dispatches._scan_epoch(c, _verdicts(o))[0]
        self.assertIsNone(ep({"a": {"gate_caps": []}}, {"a": 3}))
        self.assertIsNone(ep({"a": {"gate_caps": ["other-cap"]}}, {"a": 3}))
        # ...and they do not BLOCK a real founder further along either
        self.assertEqual(ep({"a": {"gate_caps": []},
                             "b": {"gate_caps": ["receipt-v1"]}},
                            {"a": 3, "b": 9}), 9)

    def test_a_MALFORMED_founder_fails_closed_AT_ITS_OWN_POSITION(self):
        """The first draft SKIPPED a corrupt stamp, which moved the boundary
        LATER and laundered every unstamped approval between the corrupt
        founder and the next valid receipt into legacy/READY.

        Skipping past a thing you cannot read is the `.get()` collapse one
        layer up: an unreadable value treated as an absent one. We cannot know
        what that writer could do, so the boundary starts THERE."""
        ep = lambda c, o: dispatches._scan_epoch(c, _verdicts(o))[0]
        current = {"bad": {"gate_caps": "not-a-list"},
                   "good": {"gate_caps": ["receipt-v1"]}}
        # corrupt at 4, real receipt at 11 -> the boundary is 4, NOT 11
        self.assertEqual(ep(current, {"bad": 4, "good": 11}), 4)
        # so an unstamped verdict at 7 — between them — is UNKNOWN, not legacy
        self.assertEqual(landreq.gate_requirement({}, index=7, epoch=4),
                         "unknown")
        # and a corrupt stamp alone still founds, rather than leaving no epoch
        self.assertEqual(ep({"bad": {"gate_caps": 7}}, {"bad": 2}), 2)

    def test_EVERY_authoritative_marker_FIELD_is_validated(self):
        """The review's MED finding. The first version validated `index` and nothing else,
        so `founder: []` was accepted as a marker and `verdict_order.get([])`
        raised TypeError — out of a function whose entire contract is to answer
        "may this land?" without ever raising.

        ONE FIELD WRONG AT A TIME, so each check is separately measurable: a
        row malformed in two places would stay refused when either check is
        reverted, and neither would then be bound to anything."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        good = {"v": d.EPOCH_V, "index": 2, "founder": "a" * 32,
                "anchor": "b" * 32, "ts": "2026-07-31T00:00:00Z"}
        verdicts = {"a" * 32: (2, "b" * 32)}
        current = {"a" * 32: {"gate_caps": ["receipt-v1"]}}

        def epoch_for(marker):
            with open(d.epoch_path(), "w", encoding="utf-8") as fh:
                json.dump(marker, fh)
            return d.gate_epoch(current, verdicts)

        self.assertEqual(epoch_for(good), 2)            # the control
        for field, junk in (("v", d.LEGACY_EPOCH_V), ("v", "2"), ("v", True),
                            ("v", None),
                            ("index", -1), ("index", "2"), ("index", True),
                            ("index", None),
                            ("founder", []), ("founder", {}), ("founder", 7),
                            ("founder", None), ("founder", "not-hex!"),
                            ("founder", "abc"),
                            ("anchor", []), ("anchor", None), ("anchor", 7),
                            ("anchor", "b" * 31), ("anchor", "zz" * 16),
                            ("ts", 5), ("ts", None), ("ts", "yesterday")):
            self.assertEqual(epoch_for(dict(good, **{field: junk})),
                             d.EPOCH_LOST, "%s=%r" % (field, junk))
        for missing in ("v", "index", "founder", "anchor", "ts"):
            self.assertEqual(
                epoch_for({k: v for k, v in good.items() if k != missing}),
                d.EPOCH_LOST, "missing " + missing)

    def test_a_marker_PRESENT_but_unreadable_is_LOST_never_ABSENT(self):
        """The same three states as `gate_caps`, one layer up. A marker file
        that exists and cannot be parsed is not an absent one: SOMETHING froze
        a boundary here, so falling through to a fresh derivation re-authorizes
        exactly what the unreadable marker was refusing.

        `pk.read_json` collapses missing and corrupt into its default, which is
        how the distinction was lost the first time."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        current = {"a" * 32: {"gate_caps": ["receipt-v1"]}}
        verdicts = {"a" * 32: (2, "b" * 32)}
        for junk in ("{not json", "", "[]", "null", '"a string"', "7"):
            with open(d.epoch_path(), "w", encoding="utf-8") as fh:
                fh.write(junk)
            self.assertEqual(d.gate_epoch(current, verdicts), d.EPOCH_LOST, junk)
        # ABSENT is different, and is NOT the same answer: with no stamped
        # verdict anywhere there is honestly nothing to freeze
        os.remove(d.epoch_path())
        self.assertIsNone(d.gate_epoch({"a" * 32: {}},
                                       {"a" * 32: (2, "b" * 32)}))

    def test_a_boundary_that_cannot_be_PROVEN_is_never_frozen(self):
        """Freezing a founder whose event we cannot hash would recreate the
        exact state this version ends: a marker that names a founder and cannot
        say which event it is."""
        d = dispatches
        os.makedirs(os.path.dirname(d.epoch_path()), exist_ok=True)
        current = {"a" * 32: {"gate_caps": ["receipt-v1"]}}
        self.assertIsNone(d.record_gate_epoch(current, {"a" * 32: (5, None)}))
        self.assertFalse(os.path.exists(d.epoch_path()))

    def test_a_v1_marker_is_UPGRADED_and_its_founder_never_moves(self):
        """The migration, and it must not become a re-derivation. A v1 marker
        froze a founder id with no proof of which event it names. The upgrade
        carries that id over verbatim and adds only the anchor: a fresh scan
        would pick today's earliest stamped verdict, which after any compaction
        is a LATER row — the retroactive authorization this file prevents.

        Until it is upgraded the boundary is unprovable, so it reads LOST."""
        d = dispatches
        early = self._send("op-EARLY", "an earlier loop")
        self._stamped(early["id"], "the FIRST stamped round")
        self.assertEqual(d.gate_epoch(), 2)

        def rollback():                 # a v1 marker: frozen founder, no proof
            with open(d.epoch_path(), "w", encoding="utf-8") as fh:
                json.dump({"v": d.LEGACY_EPOCH_V, "index": 2,
                           "founder": early["id"],
                           "ts": "2026-07-31T00:00:00Z"}, fh)

        rollback()
        self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)
        self.assertIsNotNone(d.record_gate_epoch())
        upgraded = self._marker()
        self.assertEqual(upgraded["v"], d.EPOCH_V)
        self.assertEqual(upgraded["founder"], early["id"])
        self.assertEqual(d.gate_epoch(), 2)

        # AND THE HALF THAT ACTUALLY BINDS IT. Above, a fresh re-derivation
        # would have picked `early` too, so it proves nothing about which rule
        # ran — the mutation pass caught exactly that and this second half is
        # the repair. Compact the frozen founder away and add a LATER stamped
        # verdict: re-deriving now lands on `later`, moving the boundary
        # forward and re-authorizing everything between. The upgrade must
        # refuse instead, because a founder it cannot find is one it cannot
        # prove.
        later = self._send("op-LATER", "a later loop")
        self._stamped(later["id"], "a later stamped round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")
        self._rewrite([r for r in self._ledger() if r.get("id") != early["id"]])
        rollback()
        self.assertIsNone(d.record_gate_epoch())
        self.assertEqual(self._marker()["v"], d.LEGACY_EPOCH_V)   # untouched
        self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)
        self.assertEqual(self._requirement(stale["id"]), "unknown")

    def test_an_UNREADABLE_marker_is_repaired_by_a_human_never_overwritten(self):
        """Overwriting a marker we cannot read is indistinguishable from
        overwriting one we can — and that is a re-derivation with the audit
        trail deleted."""
        d = dispatches
        founder = self._send("op-FOUNDER", "founding review")
        self._stamped(founder["id"], "stamped founding round")
        with open(d.epoch_path(), "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertIsNone(d.record_gate_epoch())
        with open(d.epoch_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{corrupt")
        self.assertEqual(d.gate_epoch(), d.EPOCH_LOST)

    def test_the_ANCHOR_ignores_re_serialization_and_nothing_else(self):
        """The two properties the frozen founder needs, asserted directly.
        Key order and whitespace must not matter — a compaction rewrites the
        file — while any VALUE change must, including the stamp that made this
        row the founder in the first place."""
        anchor = dispatches.verdict_anchor
        event = {"v": 3, "event": "verdict", "seq": 2, "id": "a" * 32,
                 "ts": "2026-07-31T00:00:00Z", "reviewed_tip": self.head,
                 "verdict_ref": "round one", "polarity": "fix", "gate": "",
                 "gate_caps": ["receipt-v1"]}
        base = anchor(event)
        self.assertEqual(base, anchor(dict(reversed(list(event.items())))))
        self.assertEqual(base, anchor(json.loads(json.dumps(event))))
        for field, changed in (("ts", "2026-07-31T00:00:01Z"),
                               ("verdict_ref", "round two"),
                               ("polarity", "approve"), ("gate", "abc123"),
                               ("seq", 3), ("id", "b" * 32),
                               ("reviewed_tip", "c" * 40),
                               ("gate_caps", [])):
            self.assertNotEqual(base, anchor(dict(event, **{field: changed})),
                                field)
        self.assertNotEqual(base, anchor({k: v for k, v in event.items()
                                          if k != "gate_caps"}))
        self.assertIsNone(anchor("not a dict"))
        self.assertIsNone(anchor(None))

    def test_project_DEGRADES_to_EPOCH_LOST_when_the_epoch_read_THROWS(self):
        """A cross-family review found: the gate_epoch call sat OUTSIDE project()'s per-row try, so
        a marker holding `founder: []` raised TypeError and took the WHOLE
        board with it — every land loop gone, where one refused row was the
        designed worst case.

        Bound to a THROWN epoch rather than to a malformed marker on purpose:
        gate_epoch validates its own marker now, so a marker-shaped test would
        be caught by the validator and this wall would never be measured."""
        first = self._send("op-FOUNDER", "founding review")
        self._stamped(first["id"], "stamped founding round")
        stale = self._send("op-STALE", "the stale one")
        self._unstamped(stale["id"], "unstamped approve")
        with mock.patch.object(dispatches, "gate_epoch",
                               side_effect=RuntimeError("marker exploded")):
            out, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        self.assertIn(stale["id"], out)             # the board SURVIVED
        # ...and it degraded to refusal, never to a permissive default
        self.assertNotEqual(out[stale["id"]]["state"], "READY")
        # stale writer -> absent field -> writer-blaming sentence
        self.assertIn("stamped no gate_caps",
                      out[stale["id"]]["ungated"] or "")

    def test_the_requirement_reads_NAMED_CAPABILITIES_in_three_states(self):
        """MUTATION-BOUND against every design this went through, because each
        one shipped a defect the next one found.

        A wall-time boundary could not say whether a receipt was OBTAINABLE —
        the review read the real ledger and found verdicts stamped after the
        boundary that were written while the feature was still unlanded. An
        integer policy then invited `2` to mean "newer" and "stricter" at once,
        so `>= 1` would accept a row that never met the stricter rule. And the
        third state is the one that decides the lane: a field PRESENT but
        unreadable must be UNKNOWN, never quietly demoted to "old writer"."""
        req = landreq.gate_requirement
        self.assertEqual(req({}), "none")                       # old writer
        self.assertEqual(req(None), "none")                     # no row at all
        # PRESENT-BUT-NULL is the shape the review grammar-fuzzed out of this: it
        # is UNKNOWN, and `.get()` used to hand it back identically to an
        # ABSENT key, silencing the requirement. One null would have laundered
        # an ungated approve into READY.
        self.assertEqual(req({"gate_caps": None}), "unknown")
        self.assertEqual(req({"gate_caps": []}), "none")        # honest, none
        self.assertEqual(req({"gate_caps": ["other-cap"]}), "none")
        self.assertEqual(req({"gate_caps": ["receipt-v1"]}), "required")
        self.assertEqual(req({"gate_caps": ("receipt-v1", "x")}), "required")
        # PRESENT but unreadable -> UNKNOWN, never "none"
        for junk in ("receipt-v1", 1, True, {"receipt-v1": 1}, [1], [None],
                     ["Receipt-V1 bad!"], dispatches.GATE_CAPS_UNKNOWN):
            self.assertEqual(req({"gate_caps": junk}), "unknown", junk)

    def test_a_CORRUPT_capability_stamp_never_reaches_READY(self):
        """Even beside a VALID bound receipt. A row whose stamp cannot be read
        cannot say which rules its approval was written under, so no reading of
        the receipt next to it is trustworthy either — repair the row, do not
        route around it."""
        with mock.patch.object(gate, "SUITE",
                               tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(d["id"], self.head,
                                           gate.evidence_line(row),
                                           polarity="approve")
        self.assertIsNone(err, err)
        self.assertEqual(got["gate"], row["id"])        # a REAL bound receipt
        path = dispatches.ledger_path()
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        for r in rows:
            if r.get("event") == "verdict":
                r["gate_caps"] = "not-a-list"
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        lr, err = landreq.get(d["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("unreadable", lr["ungated"])


# A real discoverable probe whose verdict is decided by a sibling data file —
# the same shape as a prior incident, where a guard test's outcome was
# decided by tree content the lane never touched.
_PROBE_SRC = """import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            got = fh.read().strip()
        self.assertEqual(got, %r)
"""

_PROBE_ID = "tests.test_stale_probe.StaleProbe.test_probe"

# A cross-family review's dual-cause shape: ONE untouched test, TWO
# independent reasons to fail — cause A decided by app.txt (the base's),
# cause B decided by poison.txt (the lane's). Trunk fixes only A.
_DUAL_PROBE_SRC = """import os
import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
        self.assertFalse(os.path.exists("poison.txt"))
"""

# An INTERACTION probe: red only when the lane's code.txt meets the OLD
# base's app.txt — green at the base alone, green on trunk, green with the
# lane applied to trunk. The reproduction guard exists for exactly this.
_INTERACTION_PROBE_SRC = """import os
import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            app = fh.read().strip()
        if os.path.exists("code.txt"):
            self.assertNotEqual(app, "v1")
"""


# The false-exculpation shapes. A live lane was told
# "the failing tests ALSO fail on current trunk" and two of those
# failures were then run on that trunk by hand and PASSED: the trunk leg read the
# reference run's EXIT STATUS and never asked which ids failed there. These
# three sources stage the ways a trunk run can end non-zero without the
# failures under investigation being trunk's at all.
#
# (1) TWO failing tests, of which trunk fixes only one.
_SPLIT_PROBE_SRC = """import unittest

class StaleProbe(unittest.TestCase):
    def test_probe(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")

    def test_other(self):
        with open("other.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
"""

_OTHER_ID = "tests.test_stale_probe.StaleProbe.test_other"

# (2) trunk renamed the failing test out of existence, so `python -m unittest
# <id>` there mints a `unittest.loader._FailedTest` and exits non-zero without
# the id under investigation ever running. This is the incident's own shape.
_RENAMED_PROBE_SRC = """import unittest

class StaleProbe(unittest.TestCase):
    def test_probe_renamed_by_trunk(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
"""

# (3) the TRUE case that a naive `ids & failed` would lose: on trunk the class
# fixture explodes, so every test under it is red there while unittest prints
# ONE header named setUpClass.
_FIXTURE_PROBE_SRC = """import os
import unittest

class StaleProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists("trunkonly.txt"):
            raise RuntimeError("this fixture cannot run on trunk")

    def test_probe(self):
        with open("app.txt") as fh:
            self.assertEqual(fh.read().strip(), "v2")
"""


class StaleBase(GateBase):
    """A FAILED receipt now says WHOSE failure it is — and every verdict here
    is asserted off the stored field and the evidence text, never off the
    absence of a complaint. The negative controls are the point: each one is
    a way a wrong STALE BASE would tell an author to ignore a failure they
    own."""

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def _commit(self, msg):
        self._git("add", "-A")
        self._git("commit", "-qm", msg)
        return self._git("rev-parse", "HEAD")

    def _seed_main(self, expect, app="v1"):
        """MAIN gets app.txt plus a real probe suite asserting its content."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", app + "\n")
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % expect)
        return self._commit("probe suite")

    def _lane(self, name="lane/stale"):
        """A lane off the CURRENT main tip, touching only code.txt."""
        self._git("checkout", "-q", "-b", name)
        self._write("code.txt", "lane work\n")
        return self._commit("lane change")

    def _advance_main(self, app=None, note="drift"):
        """Trunk moves on after the fork; the checkout returns to the lane."""
        lane = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self._git("checkout", "-q", "main")
        self._write("app.txt" if app is not None else "elsewhere.txt",
                    (app if app is not None else note) + "\n")
        sha = self._commit(note)
        self._git("checkout", "-q", lane)
        return sha

    def test_a_failure_the_base_owns_is_named_STALE_BASE(self):
        """The incident, reproduced: red at the fork, fixed on trunk since,
        lane touched neither the test nor its data — and the verdict still
        AUTHORIZES nothing."""
        base = self._seed_main(expect="v2", app="v1")   # red at the base
        self._lane()
        trunk = self._advance_main(app="v2", note="trunk fixes the data")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.STALE_BASE)
        self.assertEqual(check["trunk"], trunk)
        self.assertEqual(check["merge_base"], base)
        self.assertEqual(check["failing_files"],
                         ["tests/test_stale_probe.py"])
        self.assertIn("rebase", check["reason"])
        # The causal leg RAN and is part of the claim, not an inference.
        self.assertIn("STAY green", check["reason"])
        self.assertIn("STALE BASE", gate.evidence_line(row))
        # SAYS, never AUTHORIZES: the run did not go green, so the binding
        # refuses exactly as it would have before this verdict existed.
        state, rid, _why = gate.bind(gate.evidence_line(row), row["head"])
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, row["id"])

    def test_a_failure_in_the_lanes_own_diff_stays_plain_FAILED(self):
        """Negative control (a): the failing test file is IN the diff. The
        pre-ladder early return said LANE_OWNED from the overlap alone; the
        causal ladder answers the same attribution by MEASUREMENT — the
        lane's rewrite fails on trunk too, so NOT_STALE with the lane named
        as owner. No STALE_BASE, no extra authorization."""
        self._seed_main(expect="v1", app="v1")          # green base
        self._git("checkout", "-q", "-b", "lane/owns")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v9")
        self._commit("lane rewrites the probe")
        self._advance_main(note="unrelated trunk drift")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))
        self.assertNotIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_an_UNCOMMITTED_edit_to_the_failing_test_is_lane_owned(self):
        """The changed set reads the worktree, not just the commits — a dirty
        edit to the failing test cannot hide behind a clean branch diff.
        The causal ladder still measures it: the dirty state fails with the
        lane applied, so NOT_STALE, and the dirty-tree caution rides."""
        self._seed_main(expect="v1", app="v1")
        self._lane()
        self._advance_main(note="unrelated trunk drift")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v9")                  # dirty, never committed
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertIn("DIRTY", check["reason"])

    def test_a_NONCAUSAL_same_file_touch_earns_STALE_BASE(self):
        """The distinguishing repro: red at the base, trunk fixes it,
        and the lane touches the SAME FILE but only a comment — no test body.
        The pre-ladder early return said LANE_OWNED and stopped the author; the
        ladder now runs the legs, and the verdict is STALE_BASE with the
        overlap carried as a caution, not a cause."""
        self._seed_main(expect="v2", app="v1")          # red at the base
        self._git("checkout", "-q", "-b", "lane/comment")
        with open(os.path.join(self.repo, "tests", "test_stale_probe.py"),
                  "a") as fh:
            fh.write("\n# a comment the lane adds — no test body touched\n")
        self._commit("lane adds a comment to the probe file")
        trunk = self._advance_main(app="v2", note="trunk fixes the data")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.STALE_BASE, check["reason"])
        self.assertEqual(check["trunk"], trunk)
        self.assertIn("caution, not a cause", check["reason"])
        self.assertIn("rebase", check["reason"])

    def test_a_lane_edit_to_the_FAILING_TEST_body_can_never_read_STALE_BASE(self):
        """The hunk guard, direct: the causal legs CANNOT see this case —
        a lane that edits the failing test's own body while the base owns
        the failure. The run-level fixture cannot stage it (an edit strong
        enough to mask makes the suite green and no base_check fires at
        all — measured), so the guard is pinned at the _base_check layer
        where the legs' outputs are the inputs: every leg green/readable,
        the lane's diff touching the failing test's hunk => NOT_STALE,
        never STALE_BASE, and the reason names the edit."""
        base = self._seed_main(expect="v2", app="v1")   # red at the base
        self._git("checkout", "-q", "-b", "lane/masker")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v2" + "\n# lane adds an assertion-free "
                    "helper the probe never calls\n"
                    "def _lane_helper():\n    return 1\n")
        self._commit("lane touches the failing file")
        trunk = self._advance_main(app="v2", note="trunk fixes the data")
        git = gate.vcs.backend(self.repo)
        mb = self._git("merge-base", "main", "HEAD").strip().lower()
        files = {_PROBE_ID: "tests/test_stale_probe.py"}
        # direct: lane added a NON-test def to the failing file — the hunk
        # guard must NOT fire on a helper the probe never calls...
        touched = gate._lane_touched_test_ids(
            self.repo, git, mb, files, {_PROBE_ID})
        self.assertEqual(touched, set())
        # ...and MUST fire when the probe body itself is edited. The base
        # probe already asserts "v2", so a rewrite to the same text is an
        # EMPTY commit with no hunk — the guard must not fire on it, and
        # the fixture only discriminates if the edit lands INSIDE the def.
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v2")
        self._commit("content-identical rewrite — no hunk")
        self.assertEqual(gate._lane_touched_test_ids(
            self.repo, git, mb, files, {_PROBE_ID}), set())
        import re as _re
        path = os.path.join(self.repo, "tests", "test_stale_probe.py")
        with open(path) as fh:
            src = fh.read()
        src = src.replace("got = fh.read().strip()",
                          "got = fh.read().strip().lower()")
        with open(path, "w") as fh:
            fh.write(src)
        self._commit("lane edits inside test_probe's body")
        touched = gate._lane_touched_test_ids(
            self.repo, git, mb, files, {_PROBE_ID})
        self.assertEqual(touched, {_PROBE_ID})

    def test_a_failure_that_ALSO_fails_on_trunk_is_not_laundered(self):
        """Negative control (b): a red trunk is a red trunk. Calling it a
        stale base would tell the whole fleet 'not your problem'."""
        self._seed_main(expect="v2", app="v1")          # red then, red now
        self._lane()
        self._advance_main(note="unrelated trunk drift")   # trunk still red
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("ALSO fails on current trunk", check["reason"])
        # NAMED, not counted: the claim is about THESE ids, so
        # the receipt has to be able to show them.
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_only_the_failures_trunk_ACTUALLY_reproduces_are_called_trunks(self):
        """THE FALSE EXCULPATION. Two failing tests; trunk has
        since fixed exactly one of them. The old trunk leg saw a non-zero
        reference run and told the author both were trunk's — the one message
        that makes an author stop investigating a failure that is really
        theirs (here: the base's, and only a rebase fixes it). The verdict is
        still NOT_STALE, because a rebase onto a trunk that is red for the
        OTHER test cannot make this lane green, but the split is now said."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")                  # red at the base
        self._write("other.txt", "v1\n")                # red at the base too
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _SPLIT_PROBE_SRC)
        self._commit("probe suite, two failing tests")
        self._lane("lane/split")                        # touches code.txt only
        self._advance_main(app="v2", note="trunk fixes ONE of the two")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        # The test trunk really does own is named as trunk's...
        self.assertIn(_OTHER_ID, check["reason"])
        # ...and the one it does NOT is named as still the author's, which is
        # exactly the sentence the old code could not say.
        self.assertIn("%s did NOT fail there" % _PROBE_ID, check["reason"])
        self.assertIn("still this lane's to answer for", check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_test_trunk_cannot_even_RUN_is_never_reported_as_red_trunk(self):
        """The incident's own mechanism: trunk renamed the failing test away,
        so the reference run exits non-zero on a `_FailedTest` while the id
        under investigation never executes there. Exit status alone called
        that a red trunk. There is no evidence either way now, and UNKNOWN in
        words is what the author gets — an honest 'could not determine' beats
        a confident exculpation every time."""
        self._seed_main(expect="v2", app="v1")          # red at the base
        self._lane("lane/renamed-away")
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v2\n")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _RENAMED_PROBE_SRC)
        self._commit("trunk renames the probe out from under the lane")
        self._git("checkout", "-q", "lane/renamed-away")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertIn("UNKNOWN", check["verdict"])
        self.assertIn("no evidence that trunk owns these failures",
                      check["reason"])
        self.assertNotIn("ALSO fail", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_a_trunk_run_red_on_ids_we_never_asked_about_proves_nothing(self):
        """The same refusal one layer in: a READABLE non-zero trunk run whose
        failures are not the ones under investigation. Staged directly on the
        leg, because a reference run only ever runs the ids it was given —
        the branch is real (a loader failure reaches it whenever unittest
        prints it readably) and must not be left to chance."""
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/alien")
        self._advance_main(app="v2", note="trunk fixes the data")
        alien = {"status": "FAILED", "rc": 1, "unreadable": False,
                 "failed": {"unittest.loader._FailedTest.StaleProbe"}}
        with mock.patch.object(gate, "_reference_run",
                               return_value=(alien, None)):
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertIn("UNKNOWN", check["verdict"])
        self.assertIn("none of the 1 failure under investigation",
                      check["reason"])
        self.assertIn("unittest.loader._FailedTest.StaleProbe",
                      check["reason"])
        self.assertNotIn("ALSO fail", check["reason"])

    def test_a_class_fixture_that_explodes_on_trunk_IS_a_red_trunk(self):
        """THE TRUE CASE, kept. Tightening the leg to named ids must not lose
        the red trunk it exists to report: when trunk's setUpClass raises,
        every test under that class is red there while unittest prints ONE
        header named `setUpClass`. A plain `ids & failed` reads that as
        reproducing nothing and downgrades a real red trunk to UNKNOWN."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")                  # red at the base
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _FIXTURE_PROBE_SRC)
        self._commit("probe suite with a class fixture")
        self._lane("lane/fixture")                      # touches code.txt only
        self._git("checkout", "-q", "main")
        self._write("trunkonly.txt", "arms the fixture\n")
        self._commit("trunk arms the fixture that cannot run")
        self._git("checkout", "-q", "lane/fixture")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("ALSO fails on current trunk", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_lane_that_breaks_an_untouched_test_file_is_NOT_stale_base(self):
        """The failing test file is outside the diff AND the test passes on
        current trunk — both of the obvious legs hold — yet the lane caused
        the failure by editing app.txt. A file-set-plus-trunk predicate would
        mint a wrong STALE BASE here and tell the author to ignore a failure
        they own. The CAUSAL leg answers it now: applied onto trunk, still
        red, the lane's."""
        self._seed_main(expect="v1", app="v1")          # green base
        self._git("checkout", "-q", "-b", "lane/breaks")
        self._write("app.txt", "v3\n")
        self._commit("lane breaks the data the probe reads")
        self._advance_main(note="unrelated trunk drift")   # trunk still green
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("applied onto current trunk", check["reason"])
        self.assertIn("the lane owns", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_DUAL_CAUSE_failure_is_NEVER_stale_base(self):
        """A cross-family review's finding, verbatim shape: test T fails for
        cause A living in the old base AND cause B introduced by the lane;
        trunk fixes only A. The file is untouched (a holds), T reproduces at
        the merge-base (a' holds — via A), trunk is green (b holds) — and the
        pre-causal-leg predicate said STALE_BASE while applying the lane to
        trunk stays red. SAME TEST ID IS NOT CAUSAL PROOF."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")                  # cause A: base data
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _DUAL_PROBE_SRC)
        self._commit("probe suite, red at the base for cause A")
        self._git("checkout", "-q", "-b", "lane/dual")
        self._write("poison.txt", "cause B\n")          # the lane's own
        self._commit("lane plants cause B, test file untouched")
        self._advance_main(app="v2", note="trunk fixes cause A only")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("STILL fail", check["reason"])
        self.assertIn("survives the rebase", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_a_lane_that_cannot_APPLY_onto_trunk_is_UNKNOWN_never_stale(self):
        """An unresolvable rebase means nobody can answer the causal question
        yet — and the refusal is said in words, never a silent verdict either
        way."""
        self._seed_main(expect="v2", app="v1")          # red at the base
        self._git("checkout", "-q", "-b", "lane/conflicts")
        self._write("app.txt", "vL\n")                  # collides with trunk's fix
        self._commit("lane edits the same data trunk will fix")
        self._advance_main(app="v2", note="trunk fixes the data")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("does not apply cleanly", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_a_failure_NEVER_REPRODUCED_without_the_lane_is_not_stale(self):
        """Why the merge-base leg SURVIVES the causal leg: green at the base,
        green on trunk, green with the lane applied to trunk — two green
        reference runs and still not STALE_BASE, because the failure never
        reproduced anywhere WITHOUT the lane. Drop (a') and this interaction
        (or any flake) rides straight into a stale verdict."""
        self._git("checkout", "-q", "main")
        self._write("app.txt", "v1\n")
        self._write(os.path.join("tests", "__init__.py"), "")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _INTERACTION_PROBE_SRC)
        self._commit("probe green until the lane meets the OLD base")
        self._lane("lane/interacts")                    # adds code.txt
        self._advance_main(app="v2", note="trunk changes the data")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "FAILED")
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        self.assertIn("merge-base", check["reason"])
        self.assertIn("implicated", check["reason"])
        self.assertNotIn("STALE BASE", gate.evidence_line(row))

    def test_an_unbuildable_lane_snapshot_is_UNKNOWN_in_words(self):
        """The stale topology is REAL — only the snapshot build is broken —
        and the verdict still refuses to guess, naming the leg."""
        self._seed_main(expect="v2", app="v1")
        self._lane()
        self._advance_main(app="v2", note="trunk fixes the data")
        with mock.patch.object(gate, "_lane_snapshot", return_value=(
                None, "cannot stage the lane's working state: boom")):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("lane-on-trunk leg", check["reason"])
        self.assertIn("boom", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_lane_snapshot_captures_dirty_and_untracked_without_touching_index(self):  # noqa: VACUOUS_ASSERTION — snapshot blob contents are unconditional positive controls for the no-error and no-mutation claims
        """The causal leg promises the WHOLE working state, not HEAD alone.

        Keep three states distinct at once: staged content, newer unstaged
        content over the same path, and an untracked file. The dangling
        snapshot must carry the final worktree bytes while the caller's staged
        index and porcelain status remain byte-for-byte unchanged.
        """
        self._write("a.txt", "staged-a\n")
        self._git("add", "a.txt")
        staged = self._git("diff", "--cached", "--binary")
        self._write("a.txt", "working-a\n")
        self._write("untracked.txt", "untracked\n")
        status = self._git("status", "--porcelain=v1")

        snap, err = gate._lane_snapshot(self.repo, gate.vcs.backend(self.repo))
        self.assertIsNone(err, err)
        self.assertEqual(self._git("show", snap + ":a.txt"), "working-a")
        self.assertEqual(self._git("show", snap + ":untracked.txt"),
                         "untracked")
        self.assertEqual(self._git("diff", "--cached", "--binary"), staged)
        self.assertEqual(self._git("status", "--porcelain=v1"), status)

    def test_a_truncated_failure_list_is_UNKNOWN_and_says_skipped(self):
        check = gate._base_check(self.repo, [
            {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"},
            {"truncated": 3}], False)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("SKIPPED", check["reason"])
        self.assertIn("3", check["reason"])

    def test_unreadable_failure_identities_are_UNKNOWN_in_words(self):
        check = gate._base_check(self.repo, [], True)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("unreadable", check["reason"])

    def test_an_unresolvable_trunk_is_UNKNOWN_never_stale(self):
        stub = types.SimpleNamespace(
            trunk_ref=lambda repo: "origin/main",
            head_sha=lambda repo, ref="HEAD", **kw: None)
        with mock.patch.object(gate.vcs, "backend", return_value=stub):
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("trunk", check["reason"])

    def test_an_unreadable_changed_set_is_UNKNOWN_never_stale(self):
        """Negative control (c): the stale topology is REAL — only the
        changed-set read is broken — and the verdict still refuses to guess."""
        self._seed_main(expect="v2", app="v1")
        self._lane()
        self._advance_main(app="v2", note="trunk fixes the data")
        with mock.patch.object(gate, "_changed_files", return_value=None):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("changed-file set", check["reason"])
        self.assertIn("base-check UNKNOWN", gate.evidence_line(row))

    def test_a_breached_reference_bound_reports_what_was_SKIPPED(self):
        """The time cap exists, and breaching it is said in words — a cap
        that silently drops the trunk run would let a stale verdict rest on
        a measurement that never happened."""
        self._seed_main(expect="v2", app="v1")
        self._lane()
        self._advance_main(app="v2", note="trunk fixes the data")
        with mock.patch.object(gate, "RECHECK_TIMEOUT", 0.001):
            row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        check = row["base_check"]
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("SKIPPED", check["reason"])
        self.assertIn("exceeded", check["reason"])

    def test_the_base_verdict_is_bound_into_the_receipt_identity(self):
        """An edited STALE_BASE pasted into a lane-owned receipt stops
        resolving — the verdict is a field a reader relies on. (Pre-ladder the
        lane-owned verdict read LANE_OWNED; the causal ladder now answers
        the same attribution NOT_STALE — the receipt-identity law under
        test is unchanged.)"""
        self._seed_main(expect="v1", app="v1")
        self._git("checkout", "-q", "-b", "lane/owns-tamper")
        self._write(os.path.join("tests", "test_stale_probe.py"),
                    _PROBE_SRC % "v9")
        self._commit("lane rewrites the probe")
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(row["base_check"]["verdict"], gate.NOT_STALE)
        path = gate.receipts_path()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        lines[-1]["base_check"] = dict(lines[-1]["base_check"],
                                       verdict=gate.STALE_BASE)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got)
        # same: a KNOWN version whose stored id disagrees is tamper,
        # and the reader is told to investigate rather than re-gate.
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED", err)
        self.assertNotIn("no minted gate receipt", err)

    def test_gate_show_prints_the_base_verdict(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            gate._show_base_check({"v": 3, "base_check": {
                "verdict": gate.STALE_BASE, "reason": "the base is stale"}})
        self.assertIn("STALE BASE", stdout.getvalue())
        self.assertIn("the base is stale", stdout.getvalue())

    def test_the_absent_test_arm_fires_a_peek_with_a_stale_inventory_is_UNKNOWN(self):
        """A peek whose failing test does not exist on trunk reads UNKNOWN,
        not red trunk. Mock _reference_run to return
        _FailedTest-wrapped IDs that never intersect clean peek IDs."""
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files",
                               return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                # trunk leg: FAILED with _FailedTest-mangled IDs
                ({"status": "FAILED", "rc": 1, "failed": {
                    "unittest.loader._FailedTest.tests.test_probe.StaleProbe.test_probe"},
                 "unreadable": False}, None),
                # lane-on-trunk leg: OK (won't be reached since trunk leg fires first)
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False}, None),
                # merge-base leg: won't be reached
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertEqual(check["verdict"], gate.BASE_UNKNOWN)
        self.assertIn("do not exist on current trunk", check["reason"])

    def test_a_mixed_trunk_failure_is_not_a_stale_inventory(self):
        """A MUTATION FOUND THIS HOLE: `all(_absent_id(...))` -> `any(...)`
        survived, because nothing exercised a MIXED set. If trunk failed on a
        real id AS WELL as on a name that would not load, it is not a stale
        inventory — trunk genuinely broke on something, and calling that "the
        tests do not exist here" hands the author a false alibi."""
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                ({"status": "FAILED", "rc": 1, "failed": {
                    "unittest.loader._FailedTest.tests.test_probe.X.test_a",
                    "tests.test_other.RealTest.test_real"},
                  "unreadable": False}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertIn("UNKNOWN", check["verdict"])
        self.assertNotIn("do not exist on current trunk", check["reason"])
        self.assertIn("none of the 1 failure under investigation",
                      check["reason"])
        self.assertIn("tests.test_other.RealTest.test_real", check["reason"])

    def test_the_merge_base_leg_reads_a_fixture_explosion_too(self):
        """A MUTATION FOUND THIS ONE AS WELL: swapping `_reproduced` for a
        plain intersection AT THE MERGE-BASE survived, because only the trunk
        leg was covered. The lane's whole claim is that EVERY leg reads its
        reference run by id through one function — an untested leg is where
        the next false exculpation lands."""
        cls_id = _PROBE_ID.rsplit(".", 1)[0] + ".setUpClass"
        # THE MERGE-BASE MUST DIFFER FROM TRUNK OR THE LEG NEVER RUNS: there is
        # an earlier `if mb == trunk` short-circuit that answers NOT_STALE
        # ("the lane is based on current trunk"). My first fixture skipped this
        # and asserted a verdict the leg had no chance to produce — the same
        # mistake twice in one test, both times a leg I never reached.
        self._seed_main(expect="v2", app="v1")
        self._lane("lane/fixture-explosion")
        self._advance_main(app="v2", note="trunk moves on")
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files", return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False}, None),        # trunk leg green
                # LANE-ON-TRUNK MUST BE GREEN TO REACH THE MERGE-BASE LEG.
                # A red one short-circuits to NOT_STALE ("still fails with
                # this lane's changes, so the lane owns it") and the leg under
                # test never runs — my first fixture made exactly that mistake
                # and asserted a verdict the leg had no chance to produce.
                ({"status": "OK", "rc": 0, "failed": set(),
                  "unreadable": False}, None),        # lane-on-trunk green
                ({"status": "FAILED", "rc": 1, "failed": {cls_id},
                  "unreadable": False}, None),        # merge-base: setUpClass
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        # The fixture id COVERS the probe, so the base reproduced it — a stale
        # base. A plain intersection sees no match and calls the names absent.
        self.assertEqual(check["verdict"], gate.STALE_BASE)
        # POSITIVE CONTROL ON check["reason"] ITSELF, so the absence below is
        # about the WORDS and not about a reason that is empty or unset.
        self.assertIn("outside this lane's diff", check["reason"])
        self.assertNotIn("do not exist at the merge-base", check["reason"])

    def test_the_absent_test_arm_does_not_over_fire_a_genuine_red_trunk_stays_NOT_STALE(self):
        """Negative control: a genuinely failing test on trunk still reads
        NOT_STALE, not UNKNOWN — the new arm only fires on absent tests."""
        with mock.patch.object(gate, "_test_file",
                               return_value="tests/test_probe.py"), \
             mock.patch.object(gate, "_changed_files",
                               return_value=set()), \
             mock.patch.object(gate, "_reference_run") as ref:
            ref.side_effect = [
                # trunk leg: FAILED with the SAME ID as the peek
                ({"status": "FAILED", "rc": 1, "failed": {_PROBE_ID},
                 "unreadable": False}, None),
                # lane-on-trunk leg: won't be reached (trunk is red, short-circuits)
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False}, None),
                ({"status": "OK", "rc": 0, "failed": set(),
                 "unreadable": False}, None),
            ]
            check = gate._base_check(self.repo, [
                {"kind": "FAIL", "test": _PROBE_ID, "traceback": "frame"}],
                False)
        self.assertEqual(check["verdict"], gate.NOT_STALE)
        # WORDING UPDATED WITH THE BEHAVIOUR, and STRENGTHENED while here.
        # This arm landed after the lane that made this leg name its ids, so
        # it still pinned the old sentence. Asserting the id too is the point
        # of that change: "a red trunk" without WHICH tests is the sentence
        # that makes an author stop investigating.
        self.assertIn("ALSO fails on current trunk", check["reason"])
        self.assertIn(_PROBE_ID, check["reason"])


class ExitCodeDiscardedByAPipeTest(unittest.TestCase):
    """THE TOOL TELLS THE CALLER THE ANSWER THEY ARE ABOUT TO READ IS NOT THE
    ANSWER IT GAVE.

    A shell pipeline takes its LAST stage's status, so `helm gate run | tail`
    reports tail's success no matter what the gate did. MEASURED live:
    two seats hit this independently and repeatedly in one night, every time
    with the premise naming it live in their context. One announced "gating
    now" off a refused gate that exited 0 through the harness; the other ran
    every gate that night through the same shape and was saved only by reading
    receipts instead of exit codes, which is luck dressed as discipline.

    A rule two seats violated WHILE HOLDING IT does not need restating — it
    needs to stop being a rule. A pipe and a redirect are distinguishable at
    the fd level, so the tool can know at the moment it matters."""

    def rc_and_stderr(self, rc, isfifo):
        err = io.StringIO()
        with mock.patch.object(gate, "_gate_dispatch", return_value=rc), \
             mock.patch.object(gate, "_exit_code_will_be_discarded",
                               return_value=isfifo), \
             contextlib.redirect_stderr(err):
            got = gate.cmd_gate(["run"])
        return got, err.getvalue()

    def test_a_nonzero_through_a_PIPE_is_warned(self):
        rc, err = self.rc_and_stderr(2, True)
        self.assertEqual(rc, 2, "the real status must still be RETURNED")
        self.assertIn("stdout is a PIPE", err)
        self.assertIn("exited 2", err)

    def test_a_nonzero_that_is_NOT_piped_is_silent(self):
        """UNCONDITIONAL CONTROL on the same observable: identical failure,
        honest invocation, no warning. Without this the check would also pass
        for code that warns on every nonzero — which is noise everyone learns
        to skim past, and then it is worth nothing when it is right."""
        rc, err = self.rc_and_stderr(2, False)
        self.assertEqual(rc, 2)
        self.assertEqual(err, "")

    def test_a_SUCCESS_through_a_pipe_is_silent(self):
        """Both conditions are required. A piped success discards a zero, and
        nothing was lost — warning there would train the reader to ignore it
        before the case that matters ever arrives."""
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the SAME
        # helper with a nonzero DOES write. Without it, `err == ""` would also
        # hold for a wiring that never warns at all.
        _rc2, loud = self.rc_and_stderr(2, True)
        self.assertIn("stdout is a PIPE", loud)
        rc, err = self.rc_and_stderr(0, True)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_the_detector_keys_on_ISFIFO_not_on_not_a_file(self):
        """A TTY is neither ISFIFO nor ISREG. An operator reading a refusal on
        screen sees the exit code their shell reports and is NOT at risk, so
        keying on 'not a regular file' would warn every interactive user."""
        import stat as _stat
        class FakeStat:
            st_mode = _stat.S_IFCHR | 0o620          # a terminal
        with mock.patch.object(gate.os, "fstat", return_value=FakeStat()):
            self.assertFalse(gate._exit_code_will_be_discarded())
        class FifoStat:
            st_mode = _stat.S_IFIFO | 0o600
        with mock.patch.object(gate.os, "fstat", return_value=FifoStat()):
            self.assertTrue(gate._exit_code_will_be_discarded(),
                            "control: the detector DOES fire on a real pipe")

    def test_an_unreadable_fd_does_not_manufacture_noise(self):
        import stat as _stat
        # CONTROL FIRST: the detector is capable of returning True through
        # this exact path, so False below measures the OSError and not a
        # function that never fires.
        class FifoStat:
            st_mode = _stat.S_IFIFO | 0o600
        with mock.patch.object(gate.os, "fstat", return_value=FifoStat()):
            self.assertTrue(gate._exit_code_will_be_discarded())
        with mock.patch.object(gate.os, "fstat", side_effect=OSError("bad fd")):
            self.assertFalse(gate._exit_code_will_be_discarded())


class HelpAndFlagGuardTest(GateBase):
    """`gate import` got this guard first; show and list did not, and the
    class stayed live one function away in the same file. Measured on
    trunk: `gate show --help` exited 1 with "not a receipt id: '--help'",
    and `gate list --help` produced output BYTE-IDENTICAL to a bare `gate list`
    — the flag silently dropped and the ACTION RUN. An existence probe that
    performs the action is worse than one that refuses; it is the same shape as
    `seat down codex --bogus` stopping the seat and exiting 0, the incident
    guard_tail was written for."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = gate.cmd_gate(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_help_prints_usage_and_never_acts(self):  # noqa: VACUOUS_ASSERTION — three unconditional controls run before the loop: a help call is honoured (rc 0 + usage), a malformed flag refuses (rc 2), and "not a receipt id" is proven EMITTABLE, which is the exact string the loop asserts absent
        # UNCONDITIONAL CONTROL, outside the loop: one help call really is
        # honoured. Without it an empty loop would satisfy every assertion
        # below by running none of them.
        rc, text = self._run(["show", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("gate show", text)
        # AND THE CONTROL FOR THE ABSENCE ASSERTION BELOW: "not a receipt id"
        # is a string this verb really can emit — it is what trunk said about
        # --help itself. Asserting it is ABSENT means nothing unless something
        # unconditionally proves it can be PRESENT.
        rc_bad, bad = self._run(["show", "-not-a-flag-or-id"])
        self.assertEqual(rc_bad, 2)
        rc_shape, shape = self._run(["show", "zzz"])
        self.assertIn("not a receipt id", shape)
        for argv in (["show", "--help"], ["show", "-h"],
                     ["list", "--help"], ["list", "-h"]):
            with self.subTest(argv=argv):
                rc, text = self._run(argv)
                self.assertEqual(rc, 0)
                self.assertIn("gate %s" % argv[0], text)
                self.assertNotIn("not a receipt id", text)

    def test_help_returns_before_any_ledger_read(self):
        with mock.patch.object(gate, "by_id",
                               side_effect=AssertionError("show acted")):
            rc, text = self._run(["show", "0123456789abcdef", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("gate show", text)
        with mock.patch.object(gate, "receipts",
                               side_effect=AssertionError("list acted")):
            rc, text = self._run(["list", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("gate list", text)

    def test_list_help_is_not_a_listing(self):
        """THE POINTED ONE: --help must not render what a bare list renders.
        The control is the bare call — it has to produce the listing, or
        'they differ' would be satisfied by a verb that does nothing at all."""
        rc_bare, bare = self._run(["list"])
        self.assertEqual(rc_bare, 0)
        self.assertTrue(bare.strip(), "CONTROL: a bare list must render something")
        rc_help, helped = self._run(["list", "--help"])
        self.assertEqual(rc_help, 0)
        self.assertNotEqual(bare, helped, "--help was dropped and the action ran")

    def test_unknown_flags_refuse_before_acting(self):
        with mock.patch.object(gate, "by_id",
                               side_effect=AssertionError("show acted")):
            rc, _ = self._run(["show", "0123456789abcdef", "--bogus"])
        self.assertEqual(rc, 2)
        with mock.patch.object(gate, "receipts",
                               side_effect=AssertionError("list acted")):
            rc, _ = self._run(["list", "--bogus"])
        self.assertEqual(rc, 2)
        # UNCONDITIONAL CONTROL, outside the loop, on the same observable.
        rc, text = self._run(["show", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", text)
        for argv in (["show", "--bogus"], ["list", "--bogus"],
                     ["show", "abc", "--bogus"]):
            with self.subTest(argv=argv):
                rc, text = self._run(argv)
                self.assertEqual(rc, 2)
                self.assertIn("unknown arg", text)

    def test_a_non_numeric_limit_refuses_instead_of_raising(self):
        """int(_opt(...)) raised ValueError straight out of the verb. guard_tail
        proves --limit CARRIES a value; only this proves the value is a count."""
        with mock.patch.object(gate, "receipts",
                               side_effect=AssertionError("ledger read first")):
            rc, text = self._run(["list", "--limit", "abc"])
        self.assertEqual(rc, 2)
        self.assertIn("wants a number", text)
        rc_ok, _ = self._run(["list", "--limit", "2"])  # CONTROL: a real one works
        self.assertEqual(rc_ok, 0)

    def test_run_help_returns_before_suite_or_receipt_effects(self):  # noqa: VACUOUS_ASSERTION — a planted receipt proves the ledger is writable; exact line-count stability and the forbidden owner call prove help has no effects
        self.mint("Ran 1 test in 0.1s", "", "OK")
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            before = len(fh.readlines())
        with mock.patch.object(gate, "run",
                               side_effect=AssertionError("suite started")):
            rc, text = self._run(["run", "--help"])
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            after = len(fh.readlines())
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm gate run", text)
        self.assertEqual(after, before, "help minted a stray gate receipt")

    def test_run_help_precedes_an_empty_custom_command(self):  # noqa: VACUOUS_ASSERTION — rc0 and usage prove help won; the forbidden owner call proves the empty separator did not start work
        with mock.patch.object(gate, "run",
                               side_effect=AssertionError("suite started")):
            rc, text = self._run(["run", "--help", "--"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm gate run", text)

    def test_run_unknown_and_bad_timeout_refuse_before_suite(self):  # noqa: VACUOUS_ASSERTION — each refusal asserts rc2 and its exact parser reason while the owner call is forbidden
        for argv, reason in ((["run", "--bogus"], "unknown arg"),
                             (["run", "--timeout", "abc"], "wants a number"),
                             (["run", "--timeout", "nan"], "finite number")):
            with self.subTest(argv=argv), \
                    mock.patch.object(gate, "run",
                                      side_effect=AssertionError("suite started")):
                rc, text = self._run(argv)
            self.assertEqual(rc, 2)
            self.assertIn(reason, text)

    def test_run_preserves_command_flags_after_the_separator(self):
        with mock.patch.object(gate, "run",
                               return_value=(None, "CONTROL: owner reached")) as run:
            rc, text = self._run(["run", "--", "python3", "--help"])
        self.assertEqual(rc, 1)
        self.assertIn("CONTROL: owner reached", text)
        run.assert_called_once_with(repo=None, argv=["python3", "--help"],
                                    label=None, timeout=None)

    def test_show_still_resolves_a_real_id(self):
        """The regression control for the whole class: the positional still
        works, so the refusals above are the guard and not a broken verb."""
        rc, text = self._run(["show", "0123456789abcdef"])
        self.assertEqual(rc, 1)
        self.assertIn("no minted gate receipt", text,
                      "a WELL-FORMED id must reach the ledger lookup, not the "
                      "shape check — otherwise this passes without the "
                      "positional ever being resolved")


class ReceiptNamesWhereItRan(GateBase):
    """THE THIRD AXIS. A run happens in a TREE, under an INTERPRETER, on a
    HOST, and a receipt that cannot name all three reads exactly like proof.

    The first fab-minted receipt: CPython 3.14.6,
    Ran 7017, FAILED with 14 — and all 14 passed on the laptop, because they
    are coupled to the OTHER box. Nothing in the receipt said which box, so a
    reviewer spent a turn on phantom failures and nearly reworked a clean lane.
    """

    def _ledger(self):
        with open(gate.receipts_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _rewrite(self, rows):
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_a_minted_receipt_NAMES_the_machine_it_ran_on(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertEqual(row["host"]["node"], platform.uname().node)
        self.assertEqual(row["host"]["system"], platform.uname().system)
        self.assertIn("host=" + platform.uname().node[:24],
                      gate.evidence_line(row))

    def test_a_CUSTOM_command_receipt_still_names_its_host(self):
        """The interpreter is UNKNOWN for a custom command because helm did not
        CHOOSE it. The host has no such caveat — helm ran that command on this
        machine whatever it was — so there is no run whose box is honestly
        unknowable, and therefore none allowed to omit the field."""
        row = self.mint("Ran 1 test in 0.1s", "", "OK")
        self.assertIsNone(row["interpreter"])
        self.assertEqual(row["host"]["node"], platform.uname().node)
        self.assertIn("UNKNOWN", gate.evidence_line(row))       # interpreter
        self.assertNotIn("host=UNKNOWN", gate.evidence_line(row))

    def test_a_receipt_that_cannot_name_its_host_REFUSES_to_bind(self):
        """POSITIVE CONTROL FIRST, on the same observable: a real receipt at
        this tip BINDS. Only then is the hostless twin's refusal a measurement
        of the host field rather than of some unrelated door."""
        with mock.patch.object(
                gate, "SUITE", tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        state, _rid, why = gate.bind(gate.evidence_line(good), self.head)
        self.assertEqual(state, "VERIFIED", why)
        self.assertIn(platform.uname().node[:24], why)

        rows = self._ledger()
        blind = dict(rows[-1], host={})
        blind["id"] = gate._receipt_id(blind)       # self-consistent, hostless
        self._rewrite(rows + [blind])
        state, rid, why = gate.bind("gate:" + blind["id"], self.head)
        self.assertEqual(state, "REFUSED")
        self.assertEqual(rid, blind["id"])
        self.assertIn("does not name the HOST", why)
        self.assertIn("re-run", why.lower())

    def test_a_LEGACY_receipt_minted_before_the_host_existed_REFUSES(self):
        """Absence is refused, never grandfathered: "it was probably this box"
        is exactly the inference a receipt exists to make unnecessary."""
        with mock.patch.object(
                gate, "SUITE", tuple(_emit("Ran 9 tests in 0.2s", "", "OK"))):
            good, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        rows = self._ledger()
        legacy = {k: v for k, v in rows[-1].items() if k != "host"}
        legacy["v"] = 3
        legacy["id"] = gate._receipt_id(legacy)
        self._rewrite(rows + [legacy])
        state, _rid, why = gate.bind("gate:" + legacy["id"], self.head)
        self.assertEqual(state, "REFUSED")
        self.assertIn("does not name the HOST", why)

    def test_the_host_is_BOUND_into_the_receipt_identity(self):
        """An edited `node` must stop resolving, or "which box ran this" is a
        field anyone can rewrite after the fact — the same law the base-check
        verdict earned."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        rows = self._ledger()
        rows[-1]["host"] = dict(rows[-1]["host"], node="some-other-box")
        self._rewrite(rows)
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got)
        # PRESENT-AND-UNRECOMPUTABLE, not absent. These rows ARE in the
        # ledger; only their bound field was edited. "no minted gate
        # receipt" is the ABSENT sentence, and by-id-conflates-absent-
        # with-unverifiable split the two on purpose — asserting the
        # tamper text here means this arm now fails if anyone ever
        # re-conflates them, which the old assertion could not do.
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

    def test_the_v4_bump_did_not_UNBIND_the_earlier_fields(self):
        """A version bump that WEAKENS the hash is this file's own defect
        class. v4 must still bind the v2 failure list and the v3 base-check."""
        row = self.mint("Ran 3 tests in 0.1s", "", "FAILED (failures=1)")
        self.assertEqual(row["v"], 4)
        stored = self._ledger()
        untouched, err = gate.by_id(row["id"])
        self.assertEqual(untouched["id"], row["id"], err)   # resolves as minted

        self._rewrite([dict(stored[-1], failures=[
            {"kind": "FAIL", "test": "x.y.z", "traceback": "frame"}])])
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, "the failure list is not bound into the v4 id")
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

        self._rewrite([dict(stored[-1], failures_unreadable=not row[
            "failures_unreadable"])])
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, "failures_unreadable is not bound into v4")
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

        self._rewrite([dict(stored[-1], base_check={
            "verdict": gate.STALE_BASE, "reason": "forged"})])
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got, "the base check is not bound into the v4 id")
        self.assertIn("IS in the ledger", err)
        self.assertIn("CHANGED after it was minted", err)

    def test_the_host_id_is_a_HASH_and_never_the_machine_id_itself(self):
        """An evidence line travels — into commit messages, into chat, onto a
        public remote. A raw /etc/machine-id there is a host fingerprint nobody
        chose to publish, the same shape hostpath_guard refuses for paths."""
        secret = "0123456789abcdef0123456789abcdef"
        path = os.path.join(self.tmp, "machine-id")
        with open(path, "w") as fh:
            fh.write(secret + "\n")
        with mock.patch.object(gate, "_MACHINE_ID_PATHS", (path,)):
            ident = gate.host()
        blob = json.dumps(ident)
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
        # The blob DOES carry the discriminator, and it is the hash: the same
        # observable says what is present before it is asked what is absent.
        self.assertIn(digest, blob)
        self.assertTrue(ident["id"])
        self.assertTrue(digest)
        self.assertEqual(ident["id"], digest)
        self.assertNotIn(secret, blob)

    def test_a_box_with_no_machine_id_says_so_instead_of_inventing_one(self):
        path = os.path.join(self.tmp, "machine-id")
        with open(path, "w") as fh:
            fh.write("aaaa\n")
        with mock.patch.object(gate, "_MACHINE_ID_PATHS", (path,)):
            present = gate.host()
        self.assertTrue(present["id"])          # the field DOES populate
        with mock.patch.object(gate, "_MACHINE_ID_PATHS",
                               (os.path.join(self.tmp, "absent"),)):
            ident = gate.host()
        self.assertEqual(ident["id"], "")
        self.assertEqual(ident["node"], platform.uname().node)

    def test_gate_show_NAMES_the_host_and_says_so_when_it_cannot(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate._cmd_show([row["id"]])
        self.assertIn("host", out.getvalue())
        self.assertIn(platform.uname().node, out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate._cmd_show([row["id"], "--json"])
        self.assertIn(platform.uname().node, out.getvalue())
        rows = self._ledger()
        legacy = {k: v for k, v in rows[-1].items() if k != "host"}
        legacy["v"] = 3
        legacy["id"] = gate._receipt_id(legacy)
        self._rewrite(rows + [legacy])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate._cmd_show([legacy["id"]])
        self.assertIn("predates host recording", out.getvalue())

    # ---- the checkout the run happened in (the axis on the line that
    # travels). The integrator had to ask seats in open chat, twice in one day,
    # to "confirm each gate receipt was produced FROM THE LANE, not from a
    # shell in the shared checkout". The line itself should answer that.

    def test_the_evidence_line_NAMES_the_checkout_the_run_happened_in(self):
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        self.assertIn("room=repo", gate.evidence_line(row))

    def test_two_rooms_at_ONE_tree_are_told_apart_only_by_the_room(self):
        """The control that shows why `tree=` alone cannot answer it: a lane
        room and the shared checkout sitting at the same commit produce the
        SAME tree id, so a reader comparing tree hashes learns nothing about
        which room ran the suite."""
        room = os.path.join(self.tmp, "lane-room")
        subprocess.run(("git", "worktree", "add", "--detach", room, self.head),
                       cwd=self.repo, capture_output=True, text=True)
        here = self.mint("Ran 3 tests in 0.1s", "", "OK")
        there, err = gate.run(repo=room, argv=[sys.executable]
                              + _emit("Ran 3 tests in 0.1s", "", "OK"))
        self.assertIsNone(err, err)
        self.assertEqual(here["tree"], there["tree"])        # identical trees
        self.assertIn("room=repo", gate.evidence_line(here))
        self.assertIn("room=lane-room", gate.evidence_line(there))

    def test_the_room_is_a_basename_and_never_a_host_path(self):
        """`hostpath_guard` REFUSES a public push carrying `/home/<user>/`, and
        an evidence line ends up in commit messages. The full path stays in
        repo_id where `gate show` reads it; the line carries the basename."""
        row = self.mint("Ran 3 tests in 0.1s", "", "OK")
        line = gate.evidence_line(row)
        self.assertIn("room=repo", line)        # the field IS on the line
        self.assertNotIn("/", line.split("room=")[1].split(" |")[0])
        self.assertNotIn(self.tmp, line)
        self.assertTrue(row["repo_id"])
        self.assertEqual(row["repo_id"], os.path.realpath(self.repo))

    def test_the_whole_line_still_PASSES_the_verdict_evidence_door(self):
        """Every field added here spends a reviewer's evidence budget, so the
        bar is the REAL door and not a number invented in this file: the line
        must survive `mark_verdict`'s own length rule at its worst case — long
        room name, skip count, stale-base suffix — with the gate token excluded
        exactly as that rule excludes it."""
        row = dict(self.mint("Ran 5115 tests in 154.7s", "", "OK"),
                   repo_id="/tmp/" + "lane-with-a-very-long-name" * 4,
                   skipped=8, status="FAILED",
                   base_check={"verdict": gate.STALE_BASE, "trunk": "a" * 40,
                               "reason": "x"})
        line = gate.evidence_line(row)
        self.assertIn("gate:" + row["id"], line)    # a real, whole line
        self.assertIn("STALE BASE", line)
        self.assertNotIn("\n", line)
        budgeted = dispatches._GATE_TOKEN_RE.sub("", line)
        self.assertGreater(len(budgeted), 0)
        self.assertLessEqual(len(budgeted), 256, budgeted)
        # And the two fields this lane added are a small part of that: the
        # cost lives in the stale-base sentence, which predates them.
        without = gate.evidence_line(dict(row, host={}, repo_id=""))
        self.assertLess(len(line) - len(without), 50)


class TheMintRefusesACrossTreeRun(GateBase):
    """`cli.which_helm_warning` is deliberately detect-and-report. The MINT is
    the one place that is not enough, because the receipt authorises a land.

    Every arm drives `_cross_tree_refusal` through the resolved repo, and the
    cross-tree condition is injected at `which_helm_warning` — the single
    place that decides it — so no test has to build two checkouts to describe
    a two-checkout world.
    """

    WARN = "helm on PATH is /other/tree, you are standing in /here"

    def _cross(self, warning=WARN):
        # `gate` imports cli INSIDE the function, so there is no `gate.cli`
        # attribute to patch — the owner of the name is helm.cli itself.
        return mock.patch("helm.cli.which_helm_warning", return_value=warning)

    def test_a_cross_tree_mint_refuses_and_says_why(self):
        with self._cross():
            err = gate._cross_tree_refusal("/here")
        self.assertIn("refusing to mint a receipt", err)
        self.assertIn("authorise a land it did not test", err)
        self.assertIn("/here", err)                 # the EFFECTIVE repo, named
        self.assertIn(self.WARN, err)               # the detector's own words

    def test_a_same_tree_mint_proceeds(self):
        """POSITIVE CONTROL. Without it every arm here would pass against a
        function that refused unconditionally."""
        with self._cross(warning=None):
            self.assertIsNone(gate._cross_tree_refusal("/here"))
        with self._cross():                     # same observable, refuses
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")

    def test_the_refusal_reads_the_resolved_repo_not_a_flag_token(self):
        """Cross-family review finding 1. The first draft asked whether the string
        "--repo" was in argv; `run()` resolves `repo or os.getcwd()`, so a
        bare `--repo` or `--repo ""` presented a token while gating the cwd.
        The function takes the RESOLVED value and derives nothing itself."""
        # PIN THE AST, NOT THE TEXT. The first version of this arm asserted
        # "--repo" was absent from the source and RED immediately — the
        # docstring above explains the old bug using that exact literal. A
        # source pin that can match its own explanation is not a pin. Parsing
        # and dropping the docstring node asks about the CODE.
        fn = ast.parse(textwrap.dedent(
            inspect.getsource(gate._cross_tree_refusal))).body[0]
        body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                               and isinstance(fn.body[0].value, ast.Constant)
                               and isinstance(fn.body[0].value.value, str)
                               ) else fn.body
        code = "\n".join(ast.dump(n) for n in body)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE FIRST: the dump really does
        # carry this function's code, so the two absences below are facts about
        # the body and not about an empty string.
        self.assertIn("which_helm_warning", code)
        self.assertNotIn("--repo", code)     # derives no repo from argv
        self.assertNotIn("getcwd", code)     # and none from the process
        # AND THE DOCSTRING REALLY DOES CONTAIN IT — so the two assertions
        # above are about the body, proven, not about a literal that happens
        # to be absent everywhere.
        self.assertIn("--repo", ast.get_docstring(fn) or "")
    def test_the_decision_follows_the_repo_it_was_handed(self):
        """THE BEHAVIOURAL HALF, kept in its OWN test so it fails apart from
        the pin above — folded together, one failure could not say which check
        bit, and a pin that is never exercised alone is a pin nobody trusts.

        The first draft compared two reason strings while the detector returned
        a CONSTANT, so `cwd=repo` -> `cwd=os.getcwd()` left it green and only
        the AST pin caught the swap. Here the detector answers per-tree, so the
        argument changes the OUTCOME rather than only the wording."""
        def per_tree(cwd=None, package_dir=None):
            return self.WARN if cwd == "/tree-a" else None
        with mock.patch("helm.cli.which_helm_warning", per_tree):
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/tree-a") or "")
            self.assertIsNone(gate._cross_tree_refusal("/tree-b"))

    def test_the_override_admits_but_cannot_run_silently(self):
        """Review finding 3. An escape hatch nobody can see is not one."""
        with self._cross():                     # CONTROL: unset, it refuses
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")
        os.environ["HELM_CROSS_TREE_GATE"] = "1"
        buf = io.StringIO()
        with self._cross(), contextlib.redirect_stderr(buf):
            self.assertIsNone(gate._cross_tree_refusal("/here"))
        self.assertIn("CROSS-TREE RUN ADMITTED", buf.getvalue())
        self.assertIn("/here", buf.getvalue())

    def test_the_quiet_flag_cannot_silence_the_override_notice(self):
        """THE BYPASS ITSELF: the two knobs together used to admit in silence.
        They answer different questions and one must not disable the other."""
        os.environ["HELM_CROSS_TREE_GATE"] = "1"
        os.environ["HELM_NO_TREE_WARNING"] = "1"
        buf = io.StringIO()
        with self._cross(), contextlib.redirect_stderr(buf):
            gate._cross_tree_refusal("/here")
        self.assertIn("CROSS-TREE RUN ADMITTED", buf.getvalue())

    def test_the_quiet_flag_does_not_admit_the_run_by_itself(self):
        """HELM_NO_TREE_WARNING quiets an advisory; it never spends the
        authority of a receipt."""
        os.environ["HELM_NO_TREE_WARNING"] = "1"
        with self._cross():
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")

    def test_only_the_exact_value_one_admits(self):
        # UNCONDITIONAL POSITIVE FIRST, then the admit, then the loop. The
        # loop's own assertions all live inside subTest, so on their own a
        # guard that refused EVERYTHING — including the legitimate "1" — would
        # satisfy every iteration. Both halves are pinned here, outside it.
        with self._cross():
            self.assertIn("refusing to mint",
                          gate._cross_tree_refusal("/here") or "")
        os.environ["HELM_CROSS_TREE_GATE"] = "1"
        with self._cross(), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(gate._cross_tree_refusal("/here"))
        for value in ("0", "", "true", "yes", "1 "):
            with self.subTest(value=value):
                os.environ["HELM_CROSS_TREE_GATE"] = value
                with self._cross():
                    self.assertIn("refusing to mint",
                                  gate._cross_tree_refusal("/here") or "")

    def test_run_refuses_before_it_takes_a_fifo_position(self):
        """PLACEMENT IS THE POINT. A refused cross-tree run must start nothing
        and queue nothing — the fleet cap is 2 and a wrong-tree run that took a
        slot would starve a right-tree one."""
        acquired = []
        with self._cross(), \
                mock.patch.object(gate, "_acquire_gate",
                                  side_effect=lambda r: acquired.append(r)
                                  or (1, None)):
            row, err = gate.run(repo=self.tmp)
        self.assertIsNone(row)
        self.assertIn("refusing to mint", err)
        self.assertEqual(acquired, [])          # never queued
        # SAME OBSERVABLE, POSITIVE: the identical call DOES queue when the
        # trees agree — so the empty list above is refused-early, not a fixture
        # whose _acquire_gate is simply never reachable.
        with self._cross(warning=None), \
                mock.patch.object(gate, "_acquire_gate",
                                  side_effect=lambda r: acquired.append(r)
                                  or (None, "stop here")):
            _row, err2 = gate.run(repo=self.tmp)
        self.assertEqual(err2, "stop here")
        self.assertEqual(len(acquired), 1)

    def test_a_same_tree_run_still_reaches_the_fifo(self):
        """The control for the arm above: the same call DOES queue when the
        trees agree, so `acquired == []` there means refused-early and not a
        fixture that never queues."""
        acquired = []
        with self._cross(warning=None), \
                mock.patch.object(gate, "_acquire_gate",
                                  side_effect=lambda r: acquired.append(r)
                                  or (None, "stop here")):
            _row, err = gate.run(repo=self.tmp)
        self.assertEqual(err, "stop here")
        self.assertEqual(len(acquired), 1)

    def test_the_reviews_three_exact_repros_through_the_cli(self):
        """The review's repros, driven end-to-end and answered in their terms.

        THE TRIPWIRE ON THIS TEST: `_acquire_gate` is stubbed to a refusal, so
        a REGRESSION cannot spawn a real suite from a unit test. Without it,
        the day this guard breaks is the day the suite launches a whole-suite
        run on someone's laptop — the exact cost the guard exists to prevent.

        TWO OF THE THREE MOVED SINCE THAT REVIEW and it is worth being exact:
        `--repo` with no value and `--label --repo` are now refused a rung
        EARLIER, by `cli.guard_tail`'s valued-flag check, which landed after
        that review. `--repo ""` is the one that still reaches `run()` — an
        empty string is not a flag token, so it passes the tail check and
        resolves to the cwd. All three are pinned; only their reasons differ.
        """
        from helm import cli
        # UNCONDITIONAL CONTROL: the stub string IS reachable. A same-tree run
        # walks past the guard into `_acquire_gate` and prints it, so its
        # ABSENCE in each repro below is the guard biting — not a marker that
        # could never have appeared.
        with self._cross(warning=None), \
                mock.patch.object(gate, "_acquire_gate",
                                  return_value=(None, "SUITE WOULD HAVE RUN")):
            ctl = io.StringIO()
            with contextlib.redirect_stderr(ctl):
                cli.main(["gate", "run", "--repo", self.tmp])
        self.assertIn("SUITE WOULD HAVE RUN", ctl.getvalue())
        with self._cross(), \
                mock.patch.object(gate, "_acquire_gate",
                                  return_value=(None, "SUITE WOULD HAVE RUN")):
            for argv, expect in (
                    (["gate", "run", "--repo"], "wants a value"),
                    (["gate", "run", "--label", "--repo"], "wants a value"),
                    (["gate", "run", "--repo", ""], None)):
                with self.subTest(argv=argv):
                    buf, ebuf = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(buf), \
                            contextlib.redirect_stderr(ebuf):
                        rc = cli.main(argv)
                    out = buf.getvalue() + ebuf.getvalue()
                    self.assertNotIn("SUITE WOULD HAVE RUN", out)
                    self.assertNotEqual(rc, 0)
                    self.assertIn(expect or "refusing to mint", out)

    def test_the_refusal_travels_as_json_when_json_was_asked_for(self):
        """Review finding 2, answered by PLACEMENT: returning a reason lets
        `_cmd_run`'s existing error path render it. A second printer inside the
        guard would have re-introduced the prose/rc2 shape."""
        buf = io.StringIO()
        with self._cross(), contextlib.redirect_stdout(buf):
            rc = gate._cmd_run(["--json", "--repo", self.tmp])
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        self.assertIs(payload["minted"], False)
        self.assertIn("refusing to mint", payload["reason"])


class InflightCensusTest(GateBase):
    """The TYPED gate census. `inflight()` swallowed an
    unreadable marker directory into None, so a status read printed "no
    gate running" about a directory it never saw — a cross-family review's
    reproduction, and the third instance of one class in one night (`seat_homes.walk`,
    `dispatches.stop_candidate`): a primitive that swallows "could not look"
    turns every honest consumer into a fabricator. The census carries THREE
    states — a live owner, a TRUE empty, UNREADABLE-with-reason — and no two
    may ever collapse; `inflight()` stays as the byte-compatible projection
    so the old callers keep their old observable.

    THE BLIND ARMS PATCH THE READ, NEVER chmod 000: chmod does not bind
    root, so under a root-run suite a chmod arm is vacuous — the patch
    raises the same PermissionError for every uid."""

    def deny_listdir(self):
        """os.listdir raises PermissionError for THIS repo's marker dir and
        answers honestly everywhere else — the review's exact probe."""
        d = gate.inflight_dir(self.repo)
        real = os.listdir

        def deny(path):
            if str(path) == str(d):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        return mock.patch.object(gate.os, "listdir", side_effect=deny)

    def deny_open(self, target):
        """builtins.open raises PermissionError for ONE path only."""
        real = open

        def deny(file, *args, **kwargs):
            if str(file) == str(target):
                raise PermissionError(13, "Permission denied", str(target))
            return real(file, *args, **kwargs)
        return mock.patch("builtins.open", side_effect=deny)

    def test_THREE_states_and_NO_TWO_COLLAPSE(self):
        """LIVE / EMPTY / UNREADABLE driven from real room state, pairwise
        distinct. Both shapes of TRUE empty land on GATE_EMPTY: a marker dir
        that has never been created (every never-gated room in the fleet)
        and one left behind by a closed owner (`_inflight_close` removes
        only its file) — calling either UNREADABLE would print "could not
        measure" on every clean stop."""
        missing = gate.inflight_census(self.repo)
        self.assertEqual(missing, (gate.GATE_EMPTY, None, None))
        nonce = gate._inflight_open(self.repo)
        self.assertIsNotNone(nonce, "the fixture could not register an owner")
        try:
            live = gate.inflight_census(self.repo)
        finally:
            gate._inflight_close(self.repo, nonce)
        closed = gate.inflight_census(self.repo)
        self.assertTrue(os.path.isdir(gate.inflight_dir(self.repo)),
                        "the close must leave the dir for the empty-dir shape")
        self.assertEqual(closed, (gate.GATE_EMPTY, None, None))
        self.assertEqual(live.state, gate.GATE_LIVE)
        self.assertEqual(live.live[0], os.getpid())
        self.assertIsNone(live.reason, "a fully-read census carries no reason")
        with self.deny_listdir():
            blind = gate.inflight_census(self.repo)
        self.assertEqual(blind.state, gate.GATE_UNREADABLE)
        self.assertIsNone(blind.live)
        self.assertIn("could not be listed", blind.reason)
        self.assertIn("Permission denied", blind.reason)
        self.assertEqual(len({missing.state, live.state, blind.state}), 3,
                         "two census states collapsed")

    def test_an_unreadable_OWNER_FILE_is_UNREADABLE_not_a_measured_empty(self):
        """The dir lists fine; ONE owner file inside refuses to open. The
        old code `continue`d — an unreadable owner was no owner — which let
        a permissions accident un-guard a room with a running gate. Bytes
        READ and REJECTED (garbage JSON) stay a measured no-owner: that is
        the stale-marker law, not blindness."""
        nonce = gate._inflight_open(self.repo)
        self.assertIsNotNone(nonce)
        target = os.path.join(gate.inflight_dir(self.repo), nonce + ".json")
        try:
            with self.deny_open(target):
                blind = gate.inflight_census(self.repo)
        finally:
            gate._inflight_close(self.repo, nonce)
        self.assertEqual(blind.state, gate.GATE_UNREADABLE)
        self.assertIsNone(blind.live)
        self.assertIn(nonce, blind.reason)
        self.assertIn("Permission denied", blind.reason)
        # the CONTROL pair: garbage bytes in the same slot are MEASURED
        d = gate.inflight_dir(self.repo)
        with open(os.path.join(d, "junk.json"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        try:
            measured = gate.inflight_census(self.repo)
        finally:
            os.remove(os.path.join(d, "junk.json"))
        self.assertEqual(measured, (gate.GATE_EMPTY, None, None),
                         "rejected bytes are a stale marker, never blindness")

    def test_an_unreadable_LEGACY_marker_degrades_too(self):
        """The single-file source is a second read with the same law; an
        absent legacy file (the normal state, FileNotFoundError) stays a
        TRUE empty."""
        legacy = gate.inflight_path(self.repo)
        with self.deny_open(legacy):
            blind = gate.inflight_census(self.repo)
        self.assertEqual(blind.state, gate.GATE_UNREADABLE)
        self.assertIn("legacy marker", blind.reason)
        self.assertEqual(gate.inflight_census(self.repo),
                         (gate.GATE_EMPTY, None, None))

    def test_a_LIVE_owner_outranks_a_blind_source_and_the_blindness_is_NAMED(self):
        """Precedence with a reason riding along: a legacy owner proves the
        room is gating even while the marker dir refuses to list, so the
        state is LIVE — a positive proof outranks an unproven absence — and
        the reason still names the source that could not be read, because a
        partial census is a fact worth reporting even when the answer is
        already YES."""
        legacy = gate.inflight_path(self.repo)
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "ts": "2026-08-05T00:00:00Z"}, fh)
        try:
            with self.deny_listdir():
                partial = gate.inflight_census(self.repo)
        finally:
            os.remove(legacy)
        self.assertEqual(partial.state, gate.GATE_LIVE)
        self.assertEqual(partial.live, (os.getpid(), "2026-08-05T00:00:00Z"))
        self.assertIn("could not be listed", partial.reason)

    def test_inflight_is_a_BYTE_COMPATIBLE_projection_for_old_callers(self):
        """THE PROJECTION CONTRACT, proven not asserted: every old caller of
        `inflight()` (test_inflight_gate.py's hook among them) must observe
        exactly the pre-census behaviour — LIVE -> the (pid, ts) tuple,
        empty -> None, and UNREADABLE -> None. That last None is the very
        conflation the census escapes, preserved HERE deliberately:
        the review's probe measured None pre-fix and this pins None post-fix,
        so no caller of the projection changes behaviour by one byte."""
        self.assertIsNone(gate.inflight(self.repo))       # empty -> None
        nonce = gate._inflight_open(self.repo)
        self.assertIsNotNone(nonce)
        try:
            live = gate.inflight(self.repo)
            census = gate.inflight_census(self.repo)
        finally:
            gate._inflight_close(self.repo, nonce)
        self.assertIsInstance(live, tuple)
        self.assertEqual(len(live), 2)
        self.assertIsInstance(live[0], int)
        self.assertIsInstance(live[1], str)
        self.assertEqual(live[0], os.getpid())
        self.assertEqual(live, census.live,
                         "the projection and the census disagree on LIVE")
        with self.deny_listdir():
            self.assertEqual(gate.inflight_census(self.repo).state,
                             gate.GATE_UNREADABLE)  # the state IS unreadable
            self.assertIsNone(gate.inflight(self.repo),
                              "unreadable must project to None — the OLD "
                              "observable — never raise or leak the census")
        self.assertIsNone(gate.inflight(self.repo))       # and back to empty


if __name__ == "__main__":
    unittest.main()
