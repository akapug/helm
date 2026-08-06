"""Tests for helm.clearspan — re-measure dispatch row claims against trunk."""

import os
import subprocess
import unittest

from helm import clearspan


class ParseClaimsTest(unittest.TestCase):
    """Claim parsing from dispatch row note text."""

    def test_file_line_ref(self):
        fls, counts, shas = clearspan.parse_claims(
            "dispatches.py:306 has no deadline guard")
        self.assertEqual(fls, [("dispatches.py", 306)])
        self.assertEqual(counts, [])
        self.assertEqual(shas, [])

    def test_file_line_with_path_prefix(self):
        fls, _, _ = clearspan.parse_claims(
            "helm/dispatches.py:1405 and tests/test_dispatches.py:42")
        self.assertEqual(fls, [("helm/dispatches.py", 1405),
                               ("tests/test_dispatches.py", 42)])

    def test_full_40_char_sha(self):
        _, _, shas = clearspan.parse_claims(
            "tip b84b064920749a25db7912a7f6bc43f9cea3e458")
        self.assertEqual(shas, ["b84b064920749a25db7912a7f6bc43f9cea3e458"])

    def test_count_claim(self):
        _, counts, _ = clearspan.parse_claims(
            "43 rows sit in CHANGES_REQUESTED, 28 mine")
        self.assertEqual(counts, [(43, "rows sit in CHANGES_REQUESTED"),
                                  (28, "mine")])

    def test_empty_note(self):  # noqa: VACUOUS_ASSERTION — empty-feed negative: parser must return zeros, not hallucinate
        fls, counts, shas = clearspan.parse_claims("")
        self.assertEqual(fls, [])
        self.assertEqual(counts, [])
        self.assertEqual(shas, [])

    def test_prose_only(self):  # noqa: VACUOUS_ASSERTION — prose-feed negative: parser must not hallucinate claims from unstructured text
        fls, counts, shas = clearspan.parse_claims(
            "wire owner triage properly and clean up the display")
        self.assertEqual(fls, [])
        self.assertEqual(counts, [])
        self.assertEqual(shas, [])

    def test_lane_in_note_combined(self):
        fls, _, _ = clearspan.parse_claims(
            "file gate.py:42 is broken", "lane/fix-gate")
        self.assertEqual(fls, [("gate.py", 42)])

    def test_file_line_not_parsed_as_count(self):  # noqa: VACUOUS_ASSERTION — negative test: parse_claims must NOT produce count(306) from file:line ref 'dispatches.py:306'
        """dispatches.py:306 should not produce a count claim for '306'."""
        _, counts, _ = clearspan.parse_claims(
            "dispatches.py:306 has no deadline guard")
        self.assertEqual(counts, [])

    def test_multiple_shas_deduped(self):
        _, _, shas = clearspan.parse_claims(
            "b84b064920749a25db7912a7f6bc43f9cea3e458 "
            "b84b064920749a25db7912a7f6bc43f9cea3e458")
        self.assertEqual(len(shas), 1)
        self.assertEqual(shas, ["b84b064920749a25db7912a7f6bc43f9cea3e458"])


class ReMeasureSmokeTest(unittest.TestCase):
    """The re_measure verdict integrates parsing and git checks."""

    def test_no_repo_returns_unknown(self):
        row = {"repo_id": "/no/such/path", "note": "dispatches.py:306",
               "lane": "test"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.UNKNOWN)
        self.assertIn("no git repository", detail)

    def test_no_measurable_claims_returns_unknown(self):
        """A prose-only row has nothing to re-measure."""
        row = {"repo_id": os.getcwd(), "note": "wire owner triage",
               "lane": "triage"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.UNKNOWN)
        self.assertIn("no measurable claims", detail)

    def test_file_line_fresh(self):
        """A file:line that exists at HEAD is fresh."""
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        row = {"repo_id": os.getcwd(), "note": "helm/dispatches.py:1 is the shebang",
               "lane": "test", "id": "d" * 32, "status": "open"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.FRESH, detail)
        self.assertIn("fresh", detail)

    def test_file_line_stale_bad_file(self):
        """A file:line pointing to a nonexistent file is stale."""
        row = {"repo_id": os.getcwd(),
               "note": "nonexistent_file.py:999 is broken",
               "lane": "test"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.STALE, detail)
        self.assertIn("STALE", detail)
        self.assertIn("no such file", detail)

    def test_file_line_stale_bad_line(self):
        """A file:line with out-of-range line number is stale."""
        row = {"repo_id": os.getcwd(),
               "note": "helm/dispatches.py:99999 is broken",
               "lane": "test"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.STALE, detail)
        self.assertIn("out of range", detail)


class StatusColumnTest(unittest.TestCase):
    """The status_column symbol maps re-measure verdicts to display chars."""

    def test_closed_row_returns_dash(self):
        row = {"status": "closed", "id": "a" * 32}
        self.assertEqual(clearspan.status_column(row), "—")

    def test_no_repo_open_row_returns_dot(self):
        row = {"repo_id": "/no/such/path", "note": "wire owner triage",
               "lane": "triage", "id": "b" * 32, "status": "open"}
        self.assertEqual(clearspan.status_column(row), "·")

    def test_fresh_row_returns_check(self):
        row = {"repo_id": os.getcwd(), "note": "helm/dispatches.py:1",
               "lane": "test", "id": "c" * 32, "status": "open"}
        self.assertEqual(clearspan.status_column(row), "✓")

    def test_stale_row_returns_x(self):
        row = {"repo_id": os.getcwd(),
               "note": "nonexistent_file.py:999 is broken",
               "lane": "test", "id": "d" * 32, "status": "open"}
        self.assertEqual(clearspan.status_column(row), "✗")


class ReMeasureIntegrationTest(unittest.TestCase):
    """End-to-end: a row measured at write time, then re-measured at read time.

    THE LIVING-PIPELINE PATTERN: a measurable claim tracks a file:line or
    count that can decay. This test simulates the lifecycle: write a claim
    against a real file, modify the file to make the claim stale, and
    verify that re_measure catches it.
    """

    def setUp(self):
        import subprocess
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="clearspan-test-")
        subprocess.run(["git", "init", "-q", self.tmp],
                       capture_output=True)
        # Configure git identity so we can commit
        subprocess.run(["git", "-C", self.tmp, "config", "user.email",
                        "test@clearspan.local"], capture_output=True)
        subprocess.run(["git", "-C", self.tmp, "config", "user.name",
                        "clearspan test"], capture_output=True)
        # Create a file to claim against
        self.claimed_file = os.path.join(self.tmp, "claimed.py")
        with open(self.claimed_file, "w") as f:
            f.write("# line 1\n# line 2\n# line 3 (the target)\n")
        subprocess.run(["git", "-C", self.tmp, "add", "-A"],
                       capture_output=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q",
                        "-m", "initial"], capture_output=True)
        # Get the initial sha
        self.initial_sha = subprocess.run(
            ["git", "-C", self.tmp, "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_at_write_stale_after_edit(self):
        """Write a claim about line 4, then shrink to 2 lines — claim goes stale."""
        # Claim is fresh at write time
        row = {"repo_id": self.tmp,
               "note": "claimed.py:4 is the target line",
               "lane": "test", "id": "e" * 32, "status": "open"}
        # Add a 4th line so the claim is real
        with open(self.claimed_file, "a") as f:
            f.write("# line 4 (the target)\n")
        subprocess.run(["git", "-C", self.tmp, "add", "-A"],
                       capture_output=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q",
                        "-m", "add line 4"], capture_output=True)
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.FRESH,
                         "fresh claim must read fresh: %s" % detail)
        # Now shrink to 2 lines — claimed line 4 is out of range
        with open(self.claimed_file, "w") as f:
            f.write("# line 1\n# line 2 (shrunk)\n")
        subprocess.run(["git", "-C", self.tmp, "add", "-A"],
                       capture_output=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q",
                        "-m", "stale shrink"], capture_output=True)
        # Re-measure on read → stale
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.STALE,
                         "stale claim must read stale: %s" % detail)
        self.assertIn("STALE", detail)

    def test_sha_claim_fresh_after_landing(self):  # noqa: VACUOUS_ASSERTION — positive is assertEqual(FRESH); assertionNonIn/NotEqual covers the failure path
        """A sha that is an ancestor of HEAD is fresh."""
        row = {"repo_id": self.tmp,
               "note": "fix landed as %s" % self.initial_sha,
               "lane": "test", "id": "f" * 32, "status": "open"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.FRESH,
                         "ancestor sha must read fresh: %s" % detail)

    def test_sha_claim_stale_for_nonexistent_commit(self):  # noqa: VACUOUS_ASSERTION — positive is assertEqual(STALE); sha 'a000…' is guaranteed nonexistent
        """A sha that does not exist and is not on trunk is stale."""
        row = {"repo_id": self.tmp,
               "note": "fix landed as a" + "0" * 39,
               "lane": "test", "id": "g" * 32, "status": "open"}
        verdict, detail = clearspan.re_measure(row)
        # A nonexistent sha -> merge-base exits 128 -> could-not-look is
        # UNAVAILABLE, never a confident STALE (the cherry rung's contract:
        # an unreadable measurement invents no verdict).
        self.assertEqual(verdict, clearspan.UNEVALUABLE, detail)

    def test_bare_filename_resolves_via_prefix_search(self):  # noqa: VACUOUS_ASSERTION — positive is assertEqual(FRESH); bare filename dispatches.py resolves via helm/ prefix
        """'dispatches.py:1' without helm/ prefix still resolves."""
        row = {"repo_id": os.getcwd(),
               "note": "dispatches.py:1 has a shebang",
               "lane": "test", "id": "h" * 32, "status": "open"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.FRESH, detail)

    def test_count_claims_re_measure_against_the_ledger(self):
        """The v1 gap is closed: a count claim re-runs the LEDGER count."""
        row = {"repo_id": os.getcwd(),
               "note": "999999 rows sit in CHANGES_REQUESTED",
               "lane": "test", "id": "i" * 32, "status": "open"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.STALE,
                         "a count the ledger contradicts must read stale: %s"
                         % detail)


if __name__ == "__main__":
    unittest.main()


class ContentNotExistenceTest(unittest.TestCase):
    """The rework's center: a line is its CONTENT, not its existence.

    The first cut read any in-range line as 'fresh', so a claim whose code
    had moved read FRESH — measured live 2026-08-04 on the fleet ledger: a
    row citing rearm.py:353 read fresh while the line sat empty. The row
    carries its own predicate: the line's text at the row's ts. Same text
    then and now is FRESH; any difference is STALE, because the claim was
    about what the line SAID."""

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="helm-test-clearspan-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def _git(self, *args, date=None):
        env = dict(os.environ)
        if date:
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = date
        subprocess.run(["git", "-C", self.repo] + list(args),
                       capture_output=True, text=True, check=True, env=env)

    def _write(self, path, text, date=None):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        self._git("add", "-A")
        self._git("commit", "-qm", "write " + path, date=date)

    def _row(self, note, ts):
        return {"repo_id": os.path.join(self.repo, ".git"),
                "note": note, "lane": "probe", "ts": ts,
                "status": "open", "id": "p" * 32}

    # The fixtures pin commit DATES so the row's ts can sit BETWEEN the
    # original write and the change — that is what "the line at filing"
    # means, and a far-future ts would file the row AFTER the change
    # (measured: --before=2027 returned the CHANGED commit, and every
    # content test passed vacuously on already-moved text).
    OLD = "2026-01-01T00:00:00Z"
    MID = "2026-06-01T00:00:00Z"

    def test_an_unchanged_line_reads_FRESH(self):
        self._write("helm/mod.py", "one\nTWO three\nfive\n", date=self.OLD)
        row = self._row("mod.py:2 has no guard", self.MID)
        verdict, _d = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.FRESH)
        results, _h = clearspan._check_file_lines(
            [("mod.py", 2)], self.repo, filed_ts=self.MID)
        self.assertIn("unchanged since filing", results[0][1])

    def test_a_MOVED_line_reads_STALE_with_both_texts(self):
        self._write("helm/mod.py", "one\nTWO three\nfive\n", date=self.OLD)
        # the row is filed at MID; then the line's content changes
        self._write("helm/mod.py", "one\nsomething ELSE entirely\nfive\n")
        row = self._row("mod.py:2 has no guard", self.MID)
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.STALE,
                         "a line whose content moved must not read fresh")
        self.assertIn("CHANGED since the row was filed", detail)

    def test_a_line_that_is_now_EMPTY_reads_STALE(self):
        """The live specimen's shape: the code left, the line number remains."""
        self._write("helm/mod.py", "one\nTWO three\nfive\n", date=self.OLD)
        self._write("helm/mod.py", "one\n\nfive\n")
        row = self._row("mod.py:2 has no guard", self.MID)
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.STALE)

    def test_no_row_ts_means_existence_only_and_says_so(self):
        self._write("helm/mod.py", "one\nTWO three\nfive\n")
        row = self._row("mod.py:2 has no guard", None)
        verdict, _d = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.FRESH)
        results, _h = clearspan._check_file_lines(
            [("mod.py", 2)], self.repo, filed_ts=None)
        self.assertIn("existence only: no row ts", results[0][1],
                      "a downgrade must be printed, never silent")


class LedgerCountArmTest(unittest.TestCase):
    """A count claim is about ROWS — only the ledger answers it, never grep."""

    def test_a_matching_open_count_reads_FRESH(self):
        # POSITIVE CONTROL: the ledger really holds this many open rows.
        from helm import dispatches
        open_n = sum(1 for r in dispatches.rows().values()
                     if isinstance(r, dict) and r.get("status") == "open")
        results = clearspan._check_counts([(open_n, "rows are open")])
        self.assertEqual(results[0][0], "fresh")

    def test_a_moved_count_reads_STALE_with_the_new_number(self):
        results = clearspan._check_counts([(999999, "rows are open")])
        self.assertEqual(results[0][0], "stale")
        self.assertIn("claimed 999999", results[0][1])


class CherryRungTest(unittest.TestCase):
    """Ancestry alone false-strands a squash-merged land (37% measured):
    the reviewed object is reachable from nothing, and the CHANGE is on
    trunk under a new sha. git cherry answers the content question."""

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cherry-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.repo] + list(args),
                              capture_output=True, text=True, check=True)

    def _commit(self, path, text, msg):
        with open(os.path.join(self.repo, path), "a") as f:
            f.write(text)
        self._git("add", "-A")
        self._git("commit", "-qm", msg)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def test_a_squash_landed_sha_reads_FRESH_not_stranded(self):
        self._commit("f", "one\n", "base")
        self._git("checkout", "-qb", "lane")
        lane_tip = self._commit("f", "the lane's change\n", "lane work")
        self._git("checkout", "-q", "main")
        # squash-land: same change, NEW object — the lane tip is reachable
        # from nothing
        with open(os.path.join(self.repo, "f"), "a") as fh:
            fh.write("the lane's change\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "land lane work (squashed)")
        # CONTROL: ancestry really does say unreachable — the cherry rung is
        # what rescues this, not a loose first leg.
        self.assertFalse(clearspan._sha_is_ancestor(self.repo, lane_tip))
        results = clearspan._check_shas([lane_tip], self.repo)
        self.assertEqual(results[0][0], "fresh")
        self.assertIn("squash-landed", results[0][1])

    def test_a_genuinely_unlanded_sha_stays_STALE(self):
        self._commit("f", "one\n", "base")
        self._git("checkout", "-qb", "lane")
        lane_tip = self._commit("f", "never landed\n", "lane work")
        self._git("checkout", "-q", "main")
        self._commit("g", "unrelated\n", "other work")
        # CONTROL: the lane IS unlanded — ancestry false, cherry no match.
        self.assertFalse(clearspan._sha_is_ancestor(self.repo, lane_tip))
        results = clearspan._check_shas([lane_tip], self.repo)
        self.assertEqual(results[0][0], "stale")


class TriageVerbTest(unittest.TestCase):
    """The on-demand surface: re_measure has a real caller, not an export."""

    def test_triage_prints_a_verdict_per_open_row(self):  # noqa: VACUOUS_ASSERTION — the assertIn(row id) line IS the unconditional positive control on the same output text
        import io, contextlib, os, tempfile
        from helm import dispatches
        # An isolated ledger with ONE open row, so the assertion reads this
        # test's row and not whatever the ambient board holds (the live
        # board's open rows are all no-measurable-claims — a different
        # verdict word than FRESH/STALE, and a test that depends on the
        # board's contents is not a test).
        tmp = tempfile.mkdtemp(prefix="helm-test-triage-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        # The fleet suites share ONE process across files, so env isolation
        # must cover every key the dispatch ledger and its companions read —
        # HELM_HOME alone left the ledger pointing at the AMBIENT store on
        # the fab (measured: the test passed locally and failed there).
        prior = {k: os.environ.get(k)
                 for k in ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
                           "HELM_CHAT_NAME", "HELM_PROC")}
        for k in prior:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "triage-fixture"
        os.environ["HELM_PROC"] = os.path.join(tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        def _restore(prior=prior):
            for k, v in prior.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        self.addCleanup(_restore)
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        import subprocess as _sp
        _sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
                capture_output=True)
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            _sp.run(["git"] + cmd, cwd=repo, check=True, capture_output=True)
        open(os.path.join(repo, "f"), "w").write("one\n")
        _sp.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        _sp.run(["git", "commit", "-qm", "one"], cwd=repo, check=True,
                capture_output=True)
        tip = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                      capture_output=True, text=True).stdout.strip()
        row, why = dispatches.add("ds4pro", "probe-lane", ref=tip,
                                  repo=repo, kind="review", notify=False,
                                  new_work=True, _reason=True)
        self.assertIsNone(why)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dispatches.cmd_dispatch(["triage"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        # POSITIVE CONTROL: the fixture's row IS in the output...
        self.assertIn(row["id"][:12], text)
        # ...and it carries a verdict WORD, not a bare line.
        self.assertTrue(
            any(w in text for w in
                ("FRESH", "STALE", "UNAVAILABLE", "NO-MEASURABLE-CLAIMS")),
            "the triage surface must print a verdict per row, not a bare list")
