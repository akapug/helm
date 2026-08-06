#!/usr/bin/env python3
"""`helm lr refs` — the recorded proofs that no longer resolve.

WHY THIS EXISTS: helm binds its proofs to git SHAs, and a SHA is
content-addressed over HISTORY. Rewrite history — git filter-repo, a
squash-root, a rebased trunk — and every stored ref dangles at once. A land
request still names its reviewed tip, a verdict still names what it attested,
and not one of them can be verified again. Nothing detected that, so the first
symptom would have been a gate quietly unable to prove something it had
already proven.

THE TESTS ARE MOSTLY NEGATIVE CONTROLS, because writing this audit produced
THREE false-positive classes in a row, none caught by reading the code and all
caught by disbelieving a number:

  * iterating a {id: row} mapping yields id STRINGS — the audit inspected
    nothing and would have reported a clean bill over zero rows;
  * `cat-file --batch-check` echoes the RESOLVED FULL sha for a valid
    abbreviation and only echoes the input verbatim for `<input> missing`, so
    keying results by name lost every abbreviated ref that resolved — 119
    false dangles on a repository whose history had never been rewritten;
  * `delivery_ref` is delivery EVIDENCE (a chat row id, 12 hex) and is
    indistinguishable by shape from an abbreviated commit — 78 more.

So the load-bearing assertion is that a repository with intact history reports
ZERO, paired with a positive control proving the audit can still say otherwise.
A zero that cannot detect a real dangle is worth nothing.
"""
import os
import subprocess
import tempfile
import shutil
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import landreq  # noqa: E402

IMPOSSIBLE = "deadbeef" * 5          # 40 hex, cannot exist


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


class RefsAuditTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="helm-test-refs-")
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        open(os.path.join(self.repo, "f"), "w").write("x")
        _git(self.repo, "add", "f")
        _git(self.repo, "commit", "-qm", "one")
        self.sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _audit(self, lrs=None, drows=None):
        with mock.patch.object(landreq, "project", return_value=(lrs or {}, None)), \
                mock.patch.object(landreq.dispatches, "rows",
                                  return_value=(drows or {})):
            return landreq.dangling_refs(repo=self.repo)

    def test_a_resolvable_ref_is_never_reported_dangling(self):
        r, err = self._audit(drows={"d1": {"id": "d1", "reviewed_tip": self.sha}})
        self.assertIsNone(err)
        self.assertEqual(r["dangling"], [])
        self.assertEqual(r["unknown"], 0)

    def test_an_ABBREVIATED_resolvable_ref_is_not_dangling(self):
        """batch-check answers an abbreviation with the RESOLVED FULL sha, so a
        name-keyed lookup loses it. 119 false dangles came from exactly this."""
        r, err = self._audit(drows={"d1": {"id": "d1", "tip": self.sha[:12]}})
        self.assertIsNone(err)
        self.assertEqual(r["dangling"], [], "an abbreviation that resolves is not dangling")

    def test_an_impossible_commit_IS_reported(self):
        """The positive control. Without it a zero proves nothing."""
        r, err = self._audit(drows={"d1": {"id": "d1", "reviewed_tip": IMPOSSIBLE}})
        self.assertIsNone(err)
        self.assertEqual(len(r["dangling"]), 1)
        self.assertEqual(r["dangling"][0]["field"], "reviewed_tip")
        self.assertEqual(r["by_field"], {"reviewed_tip": 1})

    def test_delivery_evidence_is_not_audited_as_a_commit(self):
        """delivery_ref holds a chat row id — 12 hex, shaped exactly like an
        abbreviated commit, and it will NEVER resolve. Auditing it produced 78
        permanent false dangles."""
        r, err = self._audit(drows={"d1": {"id": "d1", "delivery_ref": "aaaabbbbcccc"}})
        self.assertIsNone(err)
        self.assertEqual(r["dangling"], [])
        self.assertEqual(r["checked"], 0, "it must not even be collected")

    def test_rows_are_read_from_the_mapping_VALUES(self):
        """Both ledgers hand back {id: row}. Walking the mapping yields id
        strings, which have no fields — the audit would inspect nothing and
        report a clean bill over zero rows."""
        r, err = self._audit(drows={"abcdef01": {"id": "abcdef01",
                                                 "reviewed_tip": IMPOSSIBLE}})
        self.assertIsNone(err)
        self.assertEqual(len(r["dangling"]), 1,
                         "the row's fields must be inspected, not its key")

    def test_lr_rows_are_audited_too_not_only_dispatches(self):
        r, err = self._audit(lrs={"x": {"id": "x", "review_sha": IMPOSSIBLE}})
        self.assertIsNone(err)
        self.assertEqual(len(r["dangling"]), 1)
        self.assertEqual(r["dangling"][0]["source"], "lr")

    def test_an_unreadable_ledger_reports_a_note_never_a_clean_bill(self):
        """A pass whose input was missing reports the opposite of the truth."""
        with mock.patch.object(landreq, "project", return_value=({}, "ledger gone")), \
                mock.patch.object(landreq.dispatches, "rows", return_value={}):
            r, err = landreq.dangling_refs(repo=self.repo)
        self.assertIsNone(err)
        self.assertIn("nothing was checked", r["note"])
        self.assertEqual(r["checked"], 0)

    def test_git_that_cannot_be_asked_is_UNKNOWN_not_resolved(self):
        """Tri-state: a repo we cannot query must never read as 'all resolve'."""
        with mock.patch.object(landreq, "_git", return_value=None):
            out = landreq._batch_exists("/nonexistent.git", [self.sha])
        self.assertIsNone(out, "unaskable git is UNKNOWN, never True")

    def test_a_non_repo_path_is_refused(self):
        r, err = landreq.dangling_refs(repo=tempfile.gettempdir() + "/definitely-not-a-repo")
        self.assertIsNone(r)
        self.assertIn("not a readable Git working tree", err)


if __name__ == "__main__":
    unittest.main()
