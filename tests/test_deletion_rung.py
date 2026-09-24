#!/usr/bin/env python3
"""The deletion rung: symbols a lane removes from trunk, named before the gate.

THE FIXTURE IS THE REAL INCIDENT. On 2026-08-04 a rebase auto-merge dropped
`_recipient_fields` and `_recipient_label` out of a lane — file status M, no
conflict markers, every suite green, because the lane's own tests never touched
them. The `parse` arm below replays that exact diff shape, so the test fails if
the rung ever stops seeing the thing it was built for.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import deletion_rung


# The incident, as a unified diff. Verbatim in shape: a modified file, no
# conflict markers, two functions gone and nothing announcing it.
INCIDENT = """diff --git a/helm/landreq.py b/helm/landreq.py
--- a/helm/landreq.py
+++ b/helm/landreq.py
@@ -1200,8 +1200,0 @@
-def _recipient_fields(row):
-    return row.get("recipient")
-
-def _recipient_label(row):
-    return row.get("recipient_display")
"""


class ParseTest(unittest.TestCase):
    """Pure over text, so the incident is a fixture and not a repository."""

    def test_the_INCIDENT_shape_is_seen(self):
        rows = deletion_rung.parse(INCIDENT)
        self.assertEqual(
            rows,
            [("helm/landreq.py", "def", "_recipient_fields"),
             ("helm/landreq.py", "def", "_recipient_label")])

    def test_a_RENAME_is_not_a_deletion(self):
        """A symbol that leaves while the same name arrives in the same lane is
        a move. Counting it would drown the signal in its own noise."""
        moved = ("--- a/a.py\n+++ b/a.py\n"
                 "-def helper(x):\n"
                 "--- a/b.py\n+++ b/b.py\n"
                 "+def helper(x):\n")
        self.assertEqual(deletion_rung.parse(moved), [])
        # CONTROL on the same observable: without the re-add it IS reported,
        # so the empty list above is the rename rule and not a dead parser.
        self.assertEqual(
            deletion_rung.parse("--- a/a.py\n+++ b/a.py\n-def helper(x):\n"),
            [("a.py", "def", "helper")])

    def test_classes_and_async_defs_count(self):
        d = ("--- a/m.py\n+++ b/m.py\n"
             "-class Widget(object):\n"
             "-    async def fetch(self):\n")
        self.assertEqual(
            deletion_rung.parse(d),
            [("m.py", "class", "Widget"), ("m.py", "def", "fetch")])

    def test_an_ADDED_symbol_alone_reports_nothing(self):
        d = "--- a/m.py\n+++ b/m.py\n+def brand_new(self):\n"
        self.assertEqual(deletion_rung.parse(d), [])

    def test_a_deleted_CALL_is_not_a_deleted_DEFINITION(self):
        """The rung names definitions. A removed call site is ordinary editing
        and reporting it would make the warning worthless."""
        d = "--- a/m.py\n+++ b/m.py\n-    result = _recipient_fields(row)\n"
        self.assertEqual(deletion_rung.parse(d), [])

    def test_empty_and_garbage_never_raise(self):
        for text in ("", None, "not a diff at all\n\x00\n"):
            self.assertEqual(deletion_rung.parse(text), [])
        # unconditional control: the SAME parser returns rows for a real diff,
        # so the empties above are the inputs and not a parser that died.
        self.assertEqual(len(deletion_rung.parse(INCIDENT)), 2)


class ReportTest(unittest.TestCase):
    def test_it_NAMES_the_symbol_and_the_file(self):
        """Naming IS the contract — the author settles each by recognition."""
        lines = deletion_rung.report(deletion_rung.parse(INCIDENT))
        joined = "\n".join(lines)
        self.assertIn("_recipient_fields", joined)
        self.assertIn("_recipient_label", joined)
        self.assertIn("helm/landreq.py", joined)
        self.assertIn("2", lines[0])          # the count is stated

    def test_a_clean_lane_says_NOTHING(self):
        """Silence is forbidden only when there is something to say. A rung
        that speaks on every gate is one nobody reads."""
        # control first: report() DOES speak when there is something to say.
        self.assertTrue(deletion_rung.report(deletion_rung.parse(INCIDENT)))
        self.assertEqual(deletion_rung.report([]), [])


class ScanTest(unittest.TestCase):
    """The git arm, against a real repository built for the purpose."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-deletion-rung-")
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", self.tmp], check=False))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.write("m.py", "def kept():\n    pass\n\n\ndef doomed():\n    pass\n")
        self.git("add", "m.py")
        self.git("commit", "-qm", "trunk")

    def git(self, *a):
        return subprocess.run(("git", "-C", self.tmp) + a,
                              capture_output=True, text=True)

    def write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_lane_that_drops_a_trunk_symbol_is_named(self):
        self.git("checkout", "-q", "-b", "lane")
        self.write("m.py", "def kept():\n    pass\n")     # doomed() vanishes
        self.git("commit", "-qam", "lane work")
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="lane")
        self.assertIsNone(err)
        self.assertEqual(rows, [("m.py", "def", "doomed")])

    def test_a_lane_that_drops_NOTHING_is_silent(self):
        self.git("checkout", "-q", "-b", "clean")
        self.write("m.py",
                   "def kept():\n    pass\n\n\ndef doomed():\n    pass\n\n\n"
                   "def added():\n    pass\n")
        self.git("commit", "-qam", "pure addition")
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="clean")
        self.assertIsNone(err)
        self.assertEqual(rows, [])
        # control in the same repo: a lane that DOES drop one is reported, so
        # the empty result above is this lane and not a scan returning [].
        self.git("checkout", "-q", "-b", "drops", "main")
        self.write("m.py", "def kept():\n    pass\n")
        self.git("commit", "-qam", "drops doomed")
        rows2, err2 = deletion_rung.scan(self.tmp, base="main", tip="drops")
        self.assertIsNone(err2)
        self.assertEqual(rows2, [("m.py", "def", "doomed")])

    def test_trunk_moving_AFTER_the_branch_is_not_a_lane_deletion(self):
        """THREE-DOT, and this is why. Two-dot would accuse this lane of
        removing a function it never saw, which is a false alarm on every lane
        the moment anything else lands."""
        self.git("checkout", "-q", "-b", "old")
        self.write("m.py",
                   "def kept():\n    pass\n\n\ndef doomed():\n    pass\n\n\n"
                   "def mine():\n    pass\n")
        self.git("commit", "-qam", "lane work")
        self.git("checkout", "-q", "main")
        self.write("m.py",
                   "def kept():\n    pass\n\n\ndef doomed():\n    pass\n\n\n"
                   "def landed_later():\n    pass\n")
        self.git("commit", "-qam", "trunk moves on")
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="old")
        self.assertIsNone(err)
        self.assertEqual(rows, [])
        # CONTROL: the same repo DOES report a real drop, so the empty result
        # above is the three-dot base and not a scan that stopped working.
        self.git("checkout", "-q", "-b", "dropper", "old")
        self.write("m.py", "def kept():\n    pass\n\n\ndef mine():\n    pass\n")
        self.git("commit", "-qam", "drops doomed")
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="dropper")
        self.assertIsNone(err)
        self.assertEqual(rows, [("m.py", "def", "doomed")])

    def test_a_repo_with_NO_TRUNK_REF_is_not_applicable_and_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — the control is the second half: the same repo answers normally for a ref it HAS
        """The first cut hardcoded origin/main and printed a multi-line git
        error into EVERY gate in every fixture repo and every remote-less
        repository. There is no lane-versus-trunk question without a trunk, and
        a rung that shouts on unrelated runs is one people learn to ignore."""
        rows, err = deletion_rung.scan(self.tmp, base="origin/main")
        self.assertIsNone(rows)
        self.assertEqual(err, deletion_rung.NOT_APPLICABLE)
        # CONTROL: the same repo answers normally when asked about a ref it HAS
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="main")
        self.assertIsNone(err)
        self.assertEqual(rows, [])

    def test_the_trunk_name_comes_from_the_SEAM_when_unspecified(self):
        """No remote here, so the seam falls back to the local trunk name and
        the scan runs — rather than dying on a literal that does not exist."""
        self.git("checkout", "-q", "-b", "lane2")
        self.write("m.py", "def kept():\n    pass\n")
        self.git("commit", "-qam", "drops doomed")
        rows, err = deletion_rung.scan(self.tmp)          # base defaulted
        self.assertIsNone(err)
        self.assertEqual(rows, [("m.py", "def", "doomed")])

    def test_a_ref_git_cannot_resolve_is_an_ERROR_never_a_clean_scan(self):
        """A warning that silently stops warning is the failure this rung
        exists to end, so an unanswerable question says so."""
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="no-such-ref")
        self.assertIsNone(rows)
        self.assertIn("deletion rung", err)
        self.assertNotEqual(err, deletion_rung.NOT_APPLICABLE)
        # CONTROL: the same call shape SUCCEEDS on a resolvable ref.
        rows, err = deletion_rung.scan(self.tmp, base="main", tip="main")
        self.assertIsNone(err)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
