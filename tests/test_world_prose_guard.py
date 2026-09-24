#!/usr/bin/env python3
"""End-to-end controls for the public-bound source-prose pre-commit rung."""
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

from helm import world_prose_guard                                  # noqa: E402
from helm.work import _guard                                       # noqa: E402


RUNG = os.path.abspath(world_prose_guard.__file__)
REAL_RESIDUE = (
    "# The two fields below travel TOGETHER or not at all. Three successive review\n"
    "# findings on this subject were all the same defect wearing a new face — an\n")
TIMELESS = (
    "# The fields travel together because accepting either one alone lets\n"
    "# consumers disagree about whether the record is valid.\n")


class RungBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-world-prose-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"),
                     # THIS FIXTURE SIMULATES HELM-THE-SHARED-CHECKOUT, so it
                     # DECLARES the rail: the world-prose rung is a rail-only
                     # leg, and an undeclared PROJECT repo now resolves to the
                     # leak legs (task/2441), which would install no rung at
                     # all. A fixture states which law it is under rather than
                     # inheriting a product-wide default.
                     ("config", "--local", "helm.guard.profile", "rail")):
            self.assertEqual(self.git(*args).returncode, 0)
        self.stage("README", "seed\n")
        self.assertEqual(self.git("commit", "-qm", "seed").returncode, 0)

    def git(self, *args, env=None):
        return subprocess.run(("git",) + args, cwd=self.root,
                              capture_output=True, text=True, timeout=90,
                              env=dict(os.environ, **(env or {})))

    def stage(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        r = self.git("add", "--", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def rung(self, *args):
        return subprocess.run((sys.executable, RUNG) + (args or ("--staged",)),
                              cwd=self.root, capture_output=True, text=True,
                              timeout=90)


class SourceClassifierTest(unittest.TestCase):
    def test_module_contract_names_the_bounded_clearance(self):
        self.assertIn("four rule labels below are the complete promise",
                      world_prose_guard.__doc__)
        self.assertIn("does not certify the absence of every possible chronology",
                      world_prose_guard.__doc__)

    def test_real_residue_is_a_must_hit_verbatim(self):
        found = world_prose_guard.scan_source(
            "helm/onboarding.py", REAL_RESIDUE.encode())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][:3],
                         ("helm/onboarding.py", 1, "review chronology"))
        self.assertIn("Three successive review findings", found[0][3])

    def test_superseded_nouns_are_refused_in_singular_and_plural(self):  # noqa: VACUOUS_ASSERTION — both loops are fixed nonempty tuples and every cell asserts the exact refusing category
        for noun in ("version", "draft", "implementation", "cure", "fix",
                     "attempt"):
            singular = "# An earlier %s clamped the value.\n" % noun
            plural = "# Earlier %s clamped the value.\n" % (
                "fixes" if noun == "fix" else noun + "s")
            first_two = "# The first two %s were wrong.\n" % (
                "fixes" if noun == "fix" else noun + "s")
            for prose in (singular, plural, first_two):
                found = world_prose_guard.scan_source(
                    "helm/history.py", prose.encode())
                self.assertEqual(found[0][2], "superseded implementation")

    def test_verbal_fixes_is_not_superseded_implementation(self):  # noqa: VACUOUS_ASSERTION — the exact false-positive nonhit is paired with four unconditional chronology hits
        current = "# Whichever rung asks first fixes the answer for both.\n"
        self.assertEqual(world_prose_guard.scan_source(
            "helm/foldcheck.py", current.encode()), [])
        for historical in ("# First fix failed.\n",
                           "# The first fixes both failed.\n",
                           "# My first fixes both failed.\n",
                           "# The first two fixes both failed.\n",
                           "# Earlier fixes both failed.\n"):
            found = world_prose_guard.scan_source(
                "helm/foldcheck.py", historical.encode())
            self.assertEqual(found[0][2], "superseded implementation")

    def test_timeless_property_rationale_is_a_must_not_hit(self):
        self.assertEqual(world_prose_guard.scan_source(
            "tests/test_record.py", TIMELESS.encode()), [])
        # The scanner is alive on the same syntax and path.
        self.assertTrue(world_prose_guard.scan_source(
            "tests/test_record.py", REAL_RESIDUE.encode()))

    def test_executable_string_data_is_not_prose(self):  # noqa: VACUOUS_ASSERTION — the must-hit arm above proves this scanner is live; this arm pins the opposite data/prose classification
        source = "MESSAGE = %r\n" % REAL_RESIDUE
        self.assertEqual(world_prose_guard.scan_source(
            "helm/messages.py", source.encode()), [])

    def test_named_reviewer_attribution_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal three-item tuple and every iteration asserts a nonempty exact category
        for prose in ("# @codex's MED finding belongs in the private record.\n",
                      "# helm-claude-4 flagged this detail.\n",
                      "# Cross-family review measured the bypass.\n"):
            found = world_prose_guard.scan_source(
                "helm/review.py", prose.encode())
            self.assertEqual(found[0][2], "named internal reviewer")

    def test_javascript_comment_is_prose_but_string_data_is_not(self):
        bad = b"// An earlier version returned an empty object.\n"
        self.assertTrue(world_prose_guard.scan_source(
            "tests/runtime_harness.js", bad))
        data = b'const msg = "An earlier version returned an empty object.";\n'
        self.assertEqual(world_prose_guard.scan_source(
            "tests/runtime_harness.js", data), [])

    def test_history_exemptions_are_path_identity(self):
        for rel in ("docs/history.py", "journal/2026-09-10.py",
                    "helm/journal/2026-09-10.py"):
            self.assertTrue(world_prose_guard.historical(rel), rel)
            self.assertFalse(world_prose_guard.public_bound(rel), rel)
        self.assertFalse(world_prose_guard.historical("helm/history.py"))
        self.assertTrue(world_prose_guard.public_bound("helm/history.py"))


class StagedRungTest(RungBase):
    def test_added_real_residue_is_refused_with_path_and_line(self):
        self.stage("helm/onboarding.py", REAL_RESIDUE + "X = 1\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("[helm world-prose] REFUSED", r.stderr)
        self.assertIn("matching the bounded internal-history vocabulary", r.stderr)
        self.assertIn("helm/onboarding.py:1", r.stderr)
        self.assertIn("review chronology", r.stderr)

    def test_added_range_failure_uses_the_unknown_refusal_diagnostic(self):
        self.stage("helm/clean.py", TIMELESS + "X = 1\n")
        err = io.StringIO()
        with mock.patch.object(world_prose_guard, "_root", return_value=self.root), \
                mock.patch("helm.conflict_marker._added_ranges",
                           side_effect=RuntimeError("range read failed")), \
                contextlib.redirect_stderr(err):
            rc = world_prose_guard.main(["--staged"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED: staged prose scan is UNKNOWN", err.getvalue())
        self.assertIn("range read failed", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_timeless_rationale_passes_with_live_positive_control(self):  # noqa: VACUOUS_ASSERTION — the second staged scan is an unconditional must-hit on the same rung
        self.stage("tests/test_record.py", TIMELESS + "X = 1\n")
        self.assertEqual(self.rung().returncode, 0)
        self.stage("tests/test_record.py", REAL_RESIDUE + "X = 1\n")
        self.assertEqual(self.rung().returncode, 1)

    def test_preexisting_history_does_not_block_an_unrelated_edit(self):  # noqa: VACUOUS_ASSERTION — the same file is first committed with the must-hit residue before the unchanged passage is admitted
        self.stage("helm/legacy.py", REAL_RESIDUE + "X = 1\n")
        self.assertEqual(self.git("commit", "--no-verify", "-qm", "legacy").returncode,
                         0)
        self.stage("helm/legacy.py", REAL_RESIDUE + "X = 2\n")
        self.assertEqual(self.rung().returncode, 0)

    def test_historical_documents_pass_by_path_not_phrase(self):  # noqa: VACUOUS_ASSERTION — the identical residue staged under helm/ immediately afterward is the unconditional refusing control
        for rel in ("docs/history.py", "journal/history.py",
                    "helm/journal/history.py"):
            self.stage(rel, REAL_RESIDUE)
        self.assertEqual(self.rung().returncode, 0)
        self.stage("helm/history.py", REAL_RESIDUE)
        self.assertEqual(self.rung().returncode, 1)

    def test_rename_into_public_boundary_scans_the_whole_file(self):  # noqa: VACUOUS_ASSERTION — the asserted refusal names the moved path after a successful private commit
        self.stage("private/history.py", REAL_RESIDUE + "X = 1\n")
        self.assertEqual(self.git("commit", "--no-verify", "-qm", "private").returncode,
                         0)
        os.makedirs(os.path.join(self.root, "helm"), exist_ok=True)
        self.assertEqual(self.git("mv", "private/history.py", "helm/history.py").returncode,
                         0)
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("helm/history.py:1", r.stderr)

    def test_population_mode_counts_committed_debt(self):  # noqa: VACUOUS_ASSERTION — the exact nonzero count and path are asserted on a committed must-hit fixture
        self.stage("helm/legacy.py", REAL_RESIDUE + "X = 1\n")
        self.assertEqual(self.git("commit", "--no-verify", "-qm", "legacy").returncode,
                         0)
        r = self.rung("--all")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("1 passage(s) matching the bounded internal-history vocabulary",
                      r.stderr)



class MovedProseIsNotAddedProseTest(RungBase):
    """A line that travels between two PUBLIC-BOUND files publishes nothing.

    The module contract promises three times that this rung judges prose
    ADDED under the boundary -- "judges only prose ADDED", "refuses only
    newly added history", and a rename from OUTSIDE the boundary "scans the
    whole destination because unchanged bytes are newly published THERE".
    That last sentence models the only move it knew: outside-to-inside, where
    the bytes really do reach the public tree for the first time.

    Bytes moved from one public-bound file to ANOTHER were already published:
    already under helm/ or tests/, already on trunk, already readable by
    anyone reading the tree. Judging them as added leaves a module at the
    size ceiling with two exits -- an owner override token, or rewriting
    prose inside a mechanical move -- and the second destroys the one
    property that makes a move reviewable AS a move.
    """

    def _commit(self, message):
        self.assertEqual(
            self.git("commit", "--no-verify", "-qm", message).returncode, 0)

    def test_a_line_MOVED_between_public_bound_files_is_not_added(self):  # noqa: VACUOUS_ASSERTION — the second staged scan, on the same rung and fixture, is an unconditional must-hit that refuses untravelled residue
        """The property. Red against a rung that only knows added lines."""
        self.stage("helm/origin.py", REAL_RESIDUE + "X = 1\n")
        self._commit("the residue is already published here")
        # ONE staged set: gone from origin, arrived verbatim in destination.
        self.stage("helm/origin.py", "X = 1\n")
        self.stage("helm/destination.py", REAL_RESIDUE + "Y = 2\n")
        r = self.rung()
        self.assertEqual(r.returncode, 0,
                         "a moved line was judged as added:\n%s" % r.stderr)
        # UNCONDITIONAL POSITIVE CONTROL on the SAME rung and fixture: it does
        # still refuse residue that did NOT travel, so the pass above is a
        # measured exemption rather than a rung that stopped judging.
        self.stage("helm/destination.py",
                   REAL_RESIDUE + "# LIVE 2026-07-27, by me, one hour later\n"
                   + "Y = 2\n")
        self.assertEqual(self.rung().returncode, 1,
                         "the rung passed everything, so the exemption above "
                         "proved nothing")

    def test_a_NEW_dated_line_in_the_SAME_commit_still_refuses(self):
        """THE CONTROL that keeps the rung a rung. The exemption is per LINE,
        so residue that did not travel is judged even while other lines in
        the same staged set did."""
        self.stage("helm/origin.py", REAL_RESIDUE + "X = 1\n")
        self._commit("the residue is already published here")
        self.stage("helm/origin.py", "X = 1\n")
        self.stage("helm/destination.py",
                   REAL_RESIDUE
                   + "# LIVE 2026-07-27, by me, one hour after the same call\n"
                   + "Y = 2\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "a genuinely new dated line passed because another "
                         "line in the same commit had moved")
        self.assertIn("helm/destination.py", r.stderr)

    def test_a_line_moved_from_OUTSIDE_the_boundary_still_refuses(self):
        """The docstring's own promise, unchanged: docs/ is exempt BY FILE
        IDENTITY, so prose leaving it for helm/ is published for the first
        time and is judged."""
        self.stage("docs/history.py", REAL_RESIDUE + "X = 1\n")
        self._commit("history lives outside the boundary")
        self.stage("docs/history.py", "X = 1\n")
        self.stage("helm/destination.py", REAL_RESIDUE + "Y = 2\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "prose crossing INTO the boundary was treated as a "
                         "move; it is a first publication")

    def test_a_moved_line_that_was_EDITED_is_added_text(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted positively by path and by REFUSED in stderr, not by the exit code alone
        """Byte-identical or it did not move. An edited line is new prose
        wearing a familiar shape, and no reviewer saw the edit."""
        self.stage("helm/origin.py", REAL_RESIDUE + "X = 1\n")
        self._commit("the residue is already published here")
        self.stage("helm/origin.py", "X = 1\n")
        edited = REAL_RESIDUE.replace("Three successive", "Four successive")
        self.assertNotEqual(edited, REAL_RESIDUE, "the fixture did not edit")
        self.stage("helm/destination.py", edited + "Y = 2\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1,
                         "an EDITED line was exempted as if it had moved")
        # It refused for the RIGHT reason, at the destination, rather than
        # exiting non-zero for an unrelated failure this arm cannot see.
        self.assertIn("helm/destination.py", r.stderr)
        self.assertIn("REFUSED", r.stderr)

class HookWiringTest(RungBase):
    def test_install_snapshots_and_invokes_the_refusing_rung(self):  # noqa: VACUOUS_ASSERTION — snapshot byte equality and the end-to-end commit refusal are unconditional positive controls
        rc, _lines = _guard.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0)
        assets = _guard._scanner_assets(self.root)
        installed = next(p for p in assets if p.endswith("/world_prose_guard.py"))
        with open(installed, "rb") as got, open(RUNG, "rb") as want:
            self.assertEqual(got.read(), want.read())
        with open(_guard.hook_path(self.root, "pre-commit")) as f:
            body = f.read()
        self.assertEqual(body.count('python3 "$world_prose" --staged'), 1)
        self.assertNotIn(RUNG, body)

        self.stage("helm/onboarding.py", REAL_RESIDUE + "X = 1\n")
        r = self.git("commit", "-m", "bad", env={
            "HELM_LANE_DISCIPLINE_SKIP": "1",
            "HELM_INFLIGHT_GATE_SKIP": "1",
            "HELM_NEVER_TRACK_SKIP": "1",
            "HELM_DOCREF_SKIP": "1",
            "HELM_SEATNAME_SKIP": "1",
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm world-prose] REFUSED", r.stderr)


if __name__ == "__main__":
    unittest.main()
