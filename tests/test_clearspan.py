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
        # "28 mine" is NOT a count: a bare magnitude with no counting noun
        # and no counting lead is structurally identical to the "7" in
        # "codex-7", and the elision a human reads (28 of those rows) is not
        # in the text. The parser does not guess at it.
        _, counts, _ = clearspan.parse_claims(
            "43 rows sit in CHANGES_REQUESTED, 28 mine")
        self.assertEqual(counts, [(43, "rows sit in CHANGES_REQUESTED")])

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

    # ------------------------------------------------------------------
    # Identifier digits are not magnitudes.
    #
    # A dispatch note is written in a notation full of numbered labels, and a
    # count claim parsed out of one is compared against the live open-row
    # population by _check_counts — so a label admitted here becomes a rot
    # verdict about a number that was never a quantity. Every arm below
    # carries its own MUST-HIT twin, because a parser that returns [] for
    # everything passes every must-not-hit arm ever written.
    # ------------------------------------------------------------------

    def test_a_task_identifier_is_not_a_count(self):  # noqa: VACUOUS_ASSERTION — negative arm: the twin below is the must-hit that makes this [] a measurement
        """task/1945 is a label; 1945 open rows is a claim.

        This is the case the counting-noun rule cannot decide on its own:
        "open rows" IS a counting noun, so only the slash separates the label
        from the claim.
        """
        _, refused, _ = clearspan.parse_claims("task/1945 open rows")
        self.assertEqual(refused, [])
        # MUST-HIT twin: the same words with the slash gone still parse, so
        # the [] above measures the separator and not the vocabulary.
        _, admitted, _ = clearspan.parse_claims("1945 open rows")
        self.assertEqual(admitted, [(1945, "open rows")])

    def test_no_sigil_at_all_can_precede_a_magnitude(self):  # noqa: VACUOUS_ASSERTION — the must-hit block below runs unconditionally on the same accessor
        """THE CLASS, not an enumeration of the sigils anyone thought of.

        The rule is an allow-list — start of string, whitespace, or an opening
        bracket or quote — and this arm walks characters no example in this
        file uses, because a deny-list passes its own examples forever while
        admitting every notation it does not name.
        """
        for sigil in "#:/.-@%=~+&!*^|\\":
            text = "%s1945 open rows" % sigil
            _, counts, _ = clearspan.parse_claims(text)
            self.assertEqual(counts, [], "%r admitted a label as a count" % text)
        # MUST-HIT block through the same accessor: the ONLY things that may
        # precede a magnitude do, and each still parses. Without these the loop
        # above would pass against a parser that returns [] for everything.
        for prefix in ("", " ", "landed ", "(", "[", "{", '"', "'"):
            text = "%s1945 open rows" % prefix
            _, counts, _ = clearspan.parse_claims(text)
            self.assertEqual(counts, [(1945, "open rows")],
                             "%r refused a real count" % text)

    def test_a_hash_prefixed_row_id_is_not_a_count(self):  # noqa: VACUOUS_ASSERTION — the twin below is the must-hit that makes this [] a measurement
        """The hash-prefixed form gets an arm of its own, not only a place in
        the class loop above."""
        for text in ("task #1945 open rows", "#1945 open rows"):
            _, counts, _ = clearspan.parse_claims(text)
            self.assertEqual(counts, [], "%r parsed as a count" % text)
        _, admitted, _ = clearspan.parse_claims("task 1945 open rows")
        self.assertEqual(admitted, [(1945, "open rows")])

    def test_a_seat_suffix_is_not_a_count(self):  # noqa: VACUOUS_ASSERTION — negative arm: the twin below is the must-hit that makes this [] a measurement
        """The 7 in a hyphenated name is a suffix, not seven of anything."""
        _, refused, _ = clearspan.parse_claims("codex-7 attestation claims")
        self.assertEqual(refused, [])
        # MUST-HIT twin: hyphen swapped for a space, everything else identical.
        _, admitted, _ = clearspan.parse_claims("codex 7 attestation claims")
        self.assertEqual(admitted, [(7, "attestation claims")])

    def test_a_hex_row_id_is_not_a_count(self):  # noqa: VACUOUS_ASSERTION — negative arm: the twin below is the must-hit that makes this [] a measurement
        """Row and dispatch ids are hex runs; their digits are not quantities."""
        _, refused, _ = clearspan.parse_claims("row 0f1e2d3c4b5a is held")
        self.assertEqual(refused, [])
        _, admitted, _ = clearspan.parse_claims("6 held rows")
        self.assertEqual(admitted, [(6, "held rows")])

    def test_a_magnitude_needs_something_it_counted(self):  # noqa: VACUOUS_ASSERTION — negative arm: the twin below is the must-hit that makes this [] a measurement
        """A number followed by arbitrary words is not a count claim.

        _check_counts answers every count by re-counting OPEN LEDGER ROWS, so
        admitting a bare magnitude publishes a rot verdict derived from a
        number that was never a quantity.
        """
        _, refused, _ = clearspan.parse_claims("28 mine")
        self.assertEqual(refused, [])
        _, admitted, _ = clearspan.parse_claims("28 rows mine")
        self.assertEqual(admitted, [(28, "rows mine")])

    def test_a_leading_count_word_licenses_an_unlisted_subject(self):
        """The noun list is closed; the lead-word door is how an unlisted
        subject still reads as a count."""
        _, admitted, _ = clearspan.parse_claims("total 5 whatevers")
        self.assertEqual(admitted, [(5, "whatevers")])
        # ...and only as a LEAD: the same unlisted subject with no lead word
        # is refused, so the pass above measures the lead and not the subject.
        _, refused, _ = clearspan.parse_claims("saw 5 whatevers")
        self.assertEqual(refused, [])

    def test_a_duration_is_not_a_row_count(self):  # noqa: VACUOUS_ASSERTION — negative arm: the twin below is the must-hit that makes this [] a measurement
        """Time units are absent from the noun list on purpose: the only
        population _check_counts can answer about is open rows, and "945
        minutes" compared against 165 open rows is a verdict about nothing."""
        _, refused, _ = clearspan.parse_claims("some-seat-2 held 945 minutes")
        self.assertEqual(refused, [])
        _, admitted, _ = clearspan.parse_claims("945 rows held")
        self.assertEqual(admitted, [(945, "rows held")])

    def test_a_genuine_zero_count_still_parses(self):
        """A zero is a measurement, and a countable subject is a count.

        "0 proof matches" is a real count claim about a real countable thing,
        and the parser admits it. That _check_counts then compares it against
        the open-row population is that function's declared subject fuzziness,
        a separate question; treating it as part of the identifier rule would
        be the false precision this module exists to remove. The arm exists so
        a reader can see this case was measured and left, not missed.
        """
        _, counts, _ = clearspan.parse_claims("0 proof matches")
        self.assertEqual(counts, [(0, "proof matches")])

    def test_multiple_shas_deduped(self):
        _, _, shas = clearspan.parse_claims(
            "b84b064920749a25db7912a7f6bc43f9cea3e458 "
            "b84b064920749a25db7912a7f6bc43f9cea3e458")
        self.assertEqual(len(shas), 1)
        self.assertEqual(shas, ["b84b064920749a25db7912a7f6bc43f9cea3e458"])


class ReMeasureSmokeTest(unittest.TestCase):
    """The re_measure verdict integrates parsing and git checks."""

    def test_no_repo_is_UNEVALUABLE_not_no_measurable_claims(self):
        """CANNOT-LOOK IS NOT FOUND-NOTHING. This row carries a real file:line
        claim, so "no-measurable-claims" is not merely imprecise about it — it
        is false about bytes the parser has already read."""
        row = {"repo_id": "/no/such/path", "note": "dispatches.py:306",
               "lane": "test"}
        verdict, detail = clearspan.re_measure(row)
        self.assertIn("no git repository", detail)
        self.assertEqual(verdict, clearspan.UNEVALUABLE,
                         "an unresolvable repo answered with the verdict "
                         "spelled no-measurable-claims, about a row whose "
                         "claim this reader parsed: %r" % detail)
        self.assertIn("1 claim(s)", detail,
                      "the refusal did not say how much it failed to "
                      "measure: %r" % detail)

    def test_no_measurable_claims_returns_unknown(self):
        """A prose-only row has nothing to re-measure — and the refusal says
        which surface it read, never "row body"."""
        row = {"repo_id": os.getcwd(), "note": "wire owner triage",
               "lane": "triage"}
        verdict, detail = clearspan.re_measure(row)
        self.assertEqual(verdict, clearspan.UNKNOWN)
        self.assertIn("note and lane hold no file:line, count, or sha", detail)
        self.assertIn("UNREAD, not unrotted", detail)

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

    def test_prose_only_open_row_returns_dot(self):
        """The dot is the UNKNOWN stamp, and this row earns it by its CONTENT:
        a prose note holds nothing measurable. The repo is unresolvable too,
        but that is no longer what decides — the parse answers first."""
        row = {"repo_id": "/no/such/path", "note": "wire owner triage",
               "lane": "triage", "id": "b" * 32, "status": "open"}
        self.assertEqual(clearspan.status_column(row), "·")

    def test_no_repo_on_a_CLAIM_BEARING_open_row_returns_question(self):
        """The arm the one above used to be. Same dead repo, but a real claim
        in the note, so the row reaches the repository check — and a reader
        that could not look must render '?' (unavailable), never '·' (found
        nothing). The two rows differ ONLY in the note, which is what makes
        this pair discriminate the verdict rather than the fixture."""
        row = {"repo_id": "/no/such/path", "note": "dispatches.py:306 has no guard",
               "lane": "triage", "id": "e" * 32, "status": "open"}
        self.assertEqual(clearspan.status_column(row), "?")

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


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()


class ContentNotExistenceTest(unittest.TestCase):
    """The rework's center: a line is its CONTENT, not its existence.

    The first cut read any in-range line as 'fresh', so a claim whose code
    had moved read FRESH — measured live on the fleet ledger: a
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
        # THIS FIXTURE IS ITS OWN PROJECT: without the pin the dispatch
        # write door refuses every row here as FOREIGN — a true refusal
        # that says nothing about what these arms test.
        from tests._tmphome import pin_dispatch_home
        self._real_home_repo_id = pin_dispatch_home(self, self.repo)
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
        results = clearspan._check_file_lines(
            [("mod.py", 2)], self.repo, clearspan._head_sha(self.repo),
            filed_ts=self.MID)
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
        results = clearspan._check_file_lines(
            [("mod.py", 2)], self.repo, clearspan._head_sha(self.repo),
            filed_ts=None)
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
        self.assertFalse(clearspan._sha_is_ancestor(self.repo, lane_tip,
                                                    clearspan._head_sha(self.repo)))
        results = clearspan._check_shas([lane_tip], self.repo,
                                        clearspan._head_sha(self.repo))
        self.assertEqual(results[0][0], "fresh")
        self.assertIn("squash-landed", results[0][1])

    def test_a_genuinely_unlanded_sha_stays_STALE(self):
        self._commit("f", "one\n", "base")
        self._git("checkout", "-qb", "lane")
        lane_tip = self._commit("f", "never landed\n", "lane work")
        self._git("checkout", "-q", "main")
        self._commit("g", "unrelated\n", "other work")
        # CONTROL: the lane IS unlanded — ancestry false, cherry no match.
        self.assertFalse(clearspan._sha_is_ancestor(self.repo, lane_tip,
                                                    clearspan._head_sha(self.repo)))
        results = clearspan._check_shas([lane_tip], self.repo,
                                        clearspan._head_sha(self.repo))
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
        # This arm mints its OWN scratch repository rather than the base
        # fixture's, so the pinned home does not cover it — declare the repo
        # the row speaks for, or the door refuses a ref that is perfectly
        # valid inside it.
        from tests._tmphome import dispatch_home
        with dispatch_home(repo):
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


class VerdictNamesItsScopeTest(unittest.TestCase):
    """EVERY re-measure verdict names the tree it was measured against.

    A re-measurement IS a derived fact, so it owes the same scope every other
    derived fact owes: WHICH tree answered. Until this arm existed the head was
    resolved ONLY inside the file-lines branch, so a row whose claims were
    SHAS or COUNTS produced "…at head -" — an accusation that a recorded claim
    has rotted, with no statement of the tree it rotted against. Live specimen
    from the dispatch ledger: "1 STALE of 1 claims at head -: 17 orphaned —
    the ledger now says 52 open rows (claimed 17)".

    The reader of that sentence cannot tell a genuinely rotted claim from one
    measured against a different HEAD than the one they are looking at, and
    cannot re-run the measurement to find out. That is the scope half of the
    council's step 4, and it was missing on two of three claim kinds.
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="clearspan-scope-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        run = lambda *a: subprocess.run(["git", "-C", self.tmp] + list(a),
                                        capture_output=True, text=True)
        run("init", "-q", ".")
        run("config", "user.email", "scope@clearspan.local")
        run("config", "user.name", "scope test")
        with open(os.path.join(self.tmp, "claimed.py"), "w") as f:
            f.write("# 1\n# 2\n# 3 the target\n")
        run("add", "-A")
        run("commit", "-q", "-m", "initial")
        self.head = run("rev-parse", "HEAD").stdout.strip()

    def _detail(self, note):
        _verdict, detail = clearspan.re_measure(
            {"repo_id": self.tmp, "note": note, "lane": "scope-lane"})
        return detail

    def test_a_COUNT_only_row_names_the_head(self):
        """The specimen. Counts never touched the file-lines branch, so this
        verdict used to say "at head -" while accusing a claim of rotting."""
        detail = self._detail("17 orphaned rows remain")
        self.assertIn(self.head[:12], detail,
                      "a count-claim verdict did not name the tree it "
                      "measured against: %r" % detail)
        self.assertNotIn("at head -", detail)

    def test_a_SHA_only_row_names_the_head(self):
        detail = self._detail("landed at %s" % self.head)
        self.assertIn(self.head[:12], detail,
                      "a sha-claim verdict did not name its tree: %r" % detail)

    def test_a_FILE_LINE_row_still_names_the_head(self):
        """The path that always worked — the pole. If only this one passes, the
        fix did nothing; if this one BREAKS, the fix regressed the case that
        was already right."""
        detail = self._detail("claimed.py:3 is the target")
        self.assertIn(self.head[:12], detail)

    def test_ONE_WIDTH_across_every_claim_kind(self):
        """The field is called head_short in one place and carried a FULL
        40-char sha from another, so two verdicts on the SAME TREE printed
        scopes that do not look alike — and a reader comparing them has to
        already know they are the same commit to know they agree.

        The tell was that only the FALLBACK was sliced: `"-"[:12]` slices a
        one-character string and does nothing, so the truncation was written
        where it had no effect and omitted where it did."""
        import re
        widths = set()
        for note in ("17 orphaned rows remain",
                     "landed at %s" % self.head,
                     "claimed.py:3 is the target"):
            m = re.search(r"at head ([0-9a-f]+)", self._detail(note))
            if m:
                widths.add(len(m.group(1)))
        self.assertEqual(widths, {12},
                         "the same field printed more than one width: %s"
                         % sorted(widths))


class VerdictNamesItsReaderAndMethodTest(unittest.TestCase):
    """Council step 4, slice two: WHICH READER decided, and BY WHAT MEANS.

    Slice one gave every verdict its SCOPE (the tree it measured against). This
    adds the other two fields the council's record shape needs, and the reason
    is the argument I put to the council itself: recording a derived fact with
    no way to tell which reader produced it FREEZES that reader's bugs — nothing
    later can disagree, because nothing knows the answer predates a fix.

    A stored verdict is only as good as the reader that made it. Naming the
    version means a consumer holding an old answer can tell it is old; naming
    the METHOD means they can tell an answer that checked ancestry from one that
    only counted rows, which decay differently and are not interchangeable.
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="clearspan-method-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        run = lambda *a: subprocess.run(["git", "-C", self.tmp] + list(a),
                                        capture_output=True, text=True)
        run("init", "-q", ".")
        run("config", "user.email", "m@clearspan.local")
        run("config", "user.name", "method test")
        with open(os.path.join(self.tmp, "claimed.py"), "w") as f:
            f.write("# 1\n# 2\n# 3 the target\n")
        run("add", "-A")
        run("commit", "-q", "-m", "initial")
        self.head = run("rev-parse", "HEAD").stdout.strip()

    def _detail(self, note):
        return clearspan.re_measure(
            {"repo_id": self.tmp, "note": note, "lane": "method-lane"})[1]

    def test_every_DECIDED_verdict_names_the_reader_version(self):
        """Unrolled deliberately. A loop over three notes reads tidier, but
        every assertion then sits behind it, so an empty iterable — or a
        _detail that returned nothing — is indistinguishable from three passes.
        Three named observables, each asserted unconditionally, is the shape
        that cannot go green by not running."""
        v = "v%d" % clearspan.RE_MEASURE_VERSION
        counts = self._detail("17 orphaned rows remain")
        self.assertIn("claims", counts)
        self.assertIn(v, counts, "a counts verdict named no reader: %r" % counts)
        sha = self._detail("landed at %s" % self.head)
        self.assertIn("claims", sha)
        self.assertIn(v, sha, "a sha verdict named no reader: %r" % sha)
        fl = self._detail("claimed.py:3 is the target")
        self.assertIn("claims", fl)
        self.assertIn(v, fl, "a file-line verdict named no reader: %r" % fl)

    def test_the_METHOD_names_exactly_the_instruments_that_RAN(self):
        """Not a label for the row — the kinds actually exercised. A mixed row
        must name both, or a reader cannot tell which half decided."""
        self.assertIn("counts", self._detail("17 orphaned rows remain"))
        self.assertIn("file-lines", self._detail("claimed.py:3 is the target"))
        mixed = self._detail("claimed.py:3 and 17 orphaned rows")
        self.assertIn("file-lines", mixed)
        self.assertIn("counts", mixed)

    def test_a_method_NOT_exercised_is_not_claimed(self):
        """The pole. A method string that listed every kind regardless would
        satisfy the arms above and tell a reader nothing — worse, it would
        assert that ancestry was checked on a row that only counted."""
        counts_only = self._detail("17 orphaned rows remain")
        # THE CONTROL COMES FIRST AND IS NOT DECORATION: both assertions below
        # are satisfied by the empty string, so without this line a _detail that
        # returned nothing at all would prove "the method is honest".
        self.assertIn("counts", counts_only,
                      "no verdict text to make a claim about: %r" % counts_only)
        self.assertNotIn("file-lines", counts_only,
                         "a counts-only verdict claimed it checked file lines")
        self.assertNotIn("shas", counts_only,
                         "a counts-only verdict claimed it checked shas")

    def test_an_UNKNOWN_verdict_names_its_reader_TOO(self):
        """THE PATH I FIRST SHIPPED UNSTAMPED, kept as an arm because the
        omission was so easy to defend: UNKNOWN feels like the absence of an
        answer, so it feels like it owes no provenance.

        It is the opposite. "no file:line, count, or sha" is a statement about
        THIS PARSER and THIS READER'S REACH, not about the row — a later reader
        that understands a new claim kind returns a real verdict on identical
        bytes. So an unstamped UNKNOWN is precisely the verdict a consumer would
        most wrongly treat as settled, and it is the one my own commit rationale
        claimed was covered while three of six return paths were not.
        """
        v = "v%d" % clearspan.RE_MEASURE_VERSION
        no_claims = self._detail("nothing measurable here at all")
        self.assertIn(v, no_claims,
                      "an UNKNOWN froze this reader's parser into a verdict "
                      "nothing later can date: %r" % no_claims)
        # THE PROBE CARRIES A CLAIM ON PURPOSE. With a prose note ("x") the
        # parse now answers first and this row never reaches the repository
        # check at all — the arm would still go green while measuring the
        # path above a second time. A file:line is what makes it arrive.
        no_repo = clearspan.re_measure(
            {"repo_id": os.path.join(self.tmp, "nope"),
             "note": "claimed.py:3 is the target", "lane": "l"})[1]
        self.assertIn("no git repository", no_repo,
                      "the probe did not reach the cannot-look path: %r"
                      % no_repo)
        self.assertIn(v, no_repo,
                      "a cannot-look verdict named no reader: %r" % no_repo)

    def test_EVERY_return_in_re_measure_carries_the_stamp(self):
        """The structural pole, and the reason this arm is AST rather than
        behavioural: the three arms above pass by naming paths I already know
        about, which is exactly how the original omission survived. A return
        added next month is invisible to all of them and caught by this one.
        """
        import ast
        src = open(clearspan.__file__.replace(".pyc", ".py")).read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "re_measure")
        lines = src.splitlines()
        found = [(n.lineno, "\n".join(lines[n.lineno - 1:n.end_lineno]))
                 for n in ast.walk(fn) if isinstance(n, ast.Return)]
        # THE CONTROL THIS ARM ITSELF NEEDED, and the joke is on the arm: it was
        # written to catch a verdict path that forgets its stamp, and it would
        # have gone green if re_measure were RENAMED out from under it — zero
        # returns found, zero unstamped, "pass". An enumerating arm has to prove
        # it enumerated something before its emptiness means anything.
        self.assertGreaterEqual(len(found), 6,
                                "found %d return paths in re_measure — the "
                                "walk saw nothing, so a clean result below "
                                "would be vacuous" % len(found))
        bare = [ln for ln, txt in found
                if "_vtag" not in txt and "method" not in txt]
        self.assertEqual(bare, [],
                         "re_measure returns a verdict naming no reader "
                         "version at line(s) %s — a consumer holding it cannot "
                         "tell whether it predates a fix" % bare)


class ScopeIsBOUNDNotMerelyPrintedTest(unittest.TestCase):
    """The finding on the dispatched tip, and it is deeper than the one
    slice one cured.

    Slice one made every verdict PRINT the tree it measured against. That is
    worth nothing if the rungs then ask git "what is trunk NOW" a second time:
    the label was resolved once, the file reads dereferenced HEAD again per
    file, and the ancestry and cherry rungs dereferenced it again after that.
    A fold landing mid-measurement labels tree X over readings taken from tree
    Y. EVERY INDIVIDUAL ANSWER STAYS HONEST, which is exactly what makes the
    composite unfalsifiable from the output — there is no field in which the
    disagreement can appear.

    The discriminator is real git, not a mock: move HEAD between the resolve
    and the read. On the reviewed code both calls answered from the NEW tree,
    so the arm below would have gone red on it.
    """

    def setUp(self):
        import shutil
        import tempfile
        self.repo = tempfile.mkdtemp(prefix="clearspan-bound-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self._git("init", "-q", ".")
        self._git("config", "user.email", "b@clearspan.local")
        self._git("config", "user.name", "bound test")

    def _git(self, *a):
        return subprocess.run(["git", "-C", self.repo] + list(a),
                              capture_output=True, text=True)

    def _commit(self, text, msg):
        with open(os.path.join(self.repo, "mod.py"), "w") as f:
            f.write(text)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg)
        return clearspan._head_sha(self.repo)

    def test_a_read_bound_to_a_rev_survives_HEAD_moving_under_it(self):
        first = self._commit("# 1\nALPHA\n", "alpha")
        second = self._commit("# 1\nBETA\n", "beta")
        # THE FIXTURE PROVES ITSELF BEFORE THE TEST LEANS ON IT. assertNotEqual
        # alone is satisfied by one sha and one None — a half-broken _commit
        # (git not configured, commit refused) would sail through it and every
        # assertion below would then be measuring nothing. The vacuity rung
        # caught this on the arm I had just written a non-vacuity message about.
        self.assertEqual(len(first or ""), 40, "no first sha: %r" % first)
        self.assertEqual(len(second or ""), 40, "no second sha: %r" % second)
        self.assertNotEqual(first, second, "the fixture did not move HEAD")
        # CONTROL FIRST: the function reads the tree it is given at all.
        now_text, err = clearspan._file_at_line(self.repo, "mod.py", 2, second)
        self.assertIsNone(err)
        self.assertEqual(now_text.strip(), "BETA")
        # THE FINDING: handed the OLD sha it must answer from the OLD tree.
        # Dereferencing "HEAD" here returns BETA and the verdict still prints
        # the first sha as its scope — a label and a measurement from two trees.
        then_text, err2 = clearspan._file_at_line(self.repo, "mod.py", 2, first)
        self.assertIsNone(err2)
        self.assertEqual(then_text.strip(), "ALPHA",
                         "the read followed the moving ref instead of the sha "
                         "it was bound to, so the printed scope is decorative")

    def test_the_sha_rungs_are_bound_to_the_same_tree(self):
        first = self._commit("# 1\nALPHA\n", "alpha")
        second = self._commit("# 1\nBETA\n", "beta")
        # CONTROL: against the CURRENT tree the later commit is an ancestor.
        self.assertTrue(clearspan._sha_is_ancestor(self.repo, second, second))
        # BOUND: asked about the EARLIER tree, the later commit is not yet in
        # it. A rung that reached for HEAD would answer True and disagree with
        # the scope its own verdict prints.
        self.assertFalse(clearspan._sha_is_ancestor(self.repo, second, first),
                         "the ancestry rung answered about a tree other than "
                         "the one the verdict names")

    def test_an_unresolvable_HEAD_refuses_instead_of_judging_counts(self):
        """The second half of the finding. Count claims need no tree at all, so
        before this an empty repo produced a confident FRESH/STALE with a scope
        of "-" — a verdict about trunk from a reader that could not find trunk.
        """
        empty = self.repo          # git init with NO commits: HEAD resolves to nothing
        self.assertIsNone(clearspan._head_sha(empty), "fixture has a commit")
        verdict, detail = clearspan.re_measure(
            {"repo_id": empty, "note": "17 orphaned rows remain", "lane": "l"})
        self.assertEqual(verdict, clearspan.UNEVALUABLE,
                         "a reader that cannot resolve HEAD still judged: %r"
                         % detail)
        self.assertIn("v%d" % clearspan.RE_MEASURE_VERSION, detail)


class TheBASELINELookupIsBoundTooTest(unittest.TestCase):
    """The reason the first cure missed it is the lesson.

    Every OTHER rung named "HEAD" in its argv, so a grep for the string found
    them all. `_rev_at_ts` ran `git log -1 --before=<ts> --format=%H` with NO
    REVISION AT ALL, and git dereferences the moving HEAD by default. THE
    DEFECT WAS THE ABSENCE OF AN ARGUMENT, which no search for a token can see.

    So the verdict named one tree, read "now" from it, and walked the
    filing-era baseline from a different one — producing the inverted
    "Then: BETA | now: ALPHA", measured: a comparison between two
    histories, blamed on the row.
    """

    def setUp(self):
        import shutil
        import tempfile
        self.repo = tempfile.mkdtemp(prefix="clearspan-baseline-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self._git("init", "-q", ".")
        self._git("config", "user.email", "b@clearspan.local")
        self._git("config", "user.name", "baseline test")
        # The ts must fall strictly between the commits. Git stamps whole
        # seconds and `_rev_at_ts` reads only the committer date, so the dates
        # are SET, ten seconds apart, rather than bought with a sleep.
        self.alpha = self._commit("# 1\nALPHA\n", "alpha",
                                  "2026-01-01T00:00:00+00:00")
        self.filed = self._git("log", "-1", "--format=%cI",
                               self.alpha).stdout.strip()
        # epoch, not %cI: git 2.51 spells UTC "Z", older git "+00:00"
        self.assertEqual(self._git("log", "-1", "--format=%ct",
                                   self.alpha).stdout.strip(), "1767225600",
                         "git did not take the pinned committer date")
        self.beta = self._commit("# 1\nBETA\n", "beta",
                                 "2026-01-01T00:00:10+00:00")

    def _git(self, *a, env=None):
        return subprocess.run(["git", "-C", self.repo] + list(a),
                              capture_output=True, text=True, env=env)

    def _commit(self, text, msg, when):
        with open(os.path.join(self.repo, "mod.py"), "w") as f:
            f.write(text)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg,
                  env=dict(os.environ, GIT_AUTHOR_DATE=when,
                           GIT_COMMITTER_DATE=when))
        return clearspan._head_sha(self.repo)

    def test_the_filing_era_baseline_is_walked_from_the_BOUND_head(self):
        """THE ARM THAT GOES RED ON THE UNBOUND READ. Bind ALPHA while the real
        HEAD is BETA: at ALPHA the line says ALPHA and it said ALPHA at filing,
        so the only coherent verdict is FRESH. The unbound read walked the
        baseline from BETA
        and reported STALE 'Then: BETA | now: ALPHA' — an inversion, because
        'then' came from a later tree than 'now'."""
        self.assertNotEqual(self.alpha, self.beta, "fixture did not move HEAD")
        res = clearspan._check_file_lines([("mod.py", 2)], self.repo,
                                          self.alpha, filed_ts=self.filed)
        self.assertEqual(len(res), 1, "no verdict to judge: %r" % (res,))
        result, detail = res[0]
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, before any absence claim:
        # assertNotIn is satisfied by a detail string that is empty, or about
        # some other file entirely.
        self.assertIn("mod.py:2", detail)
        self.assertNotIn("Then: BETA", detail,
                         "the baseline was walked from the UNBOUND head: %r"
                         % detail)
        self.assertEqual(result, "fresh",
                         "bound to the filing-era tree the line is unchanged, "
                         "so anything but fresh compares two histories: %r"
                         % detail)

    def test_real_staleness_is_STILL_detected_against_the_real_head(self):
        """The control, and it is what stops the arm above being satisfied by a
        reader that simply stopped answering STALE."""
        res = clearspan._check_file_lines([("mod.py", 2)], self.repo,
                                          self.beta, filed_ts=self.filed)
        self.assertEqual(len(res), 1)
        result, detail = res[0]
        self.assertEqual(result, "stale", detail)
        self.assertIn("Then: ALPHA", detail)
        self.assertIn("now: BETA", detail)


class _SpyProc:
    """Stands in for clearspan's own `subprocess` module attribute.

    Patched onto the MODULE (``clearspan.subprocess``), never into
    ``sys.modules``: every git call in this file is written ``subprocess.run``
    and resolves that name from clearspan's globals at call time, so the module
    attribute is the seam that is actually in effect. A sys.modules fake would
    be bypassed and every spawn assertion below would be measuring the live
    system while reporting on a fixture.
    """

    def __init__(self, real):
        self._real = real
        self.argv = []
        self.TimeoutExpired = real.TimeoutExpired
        self.CalledProcessError = real.CalledProcessError

    def run(self, argv, **kw):
        self.argv.append(tuple(argv))
        return self._real.run(argv, **kw)


class TheFreeAnswerComesBeforeTheSubprocessTest(unittest.TestCase):
    """`_repo_root` spawned `git rev-parse --show-toplevel` BEFORE the parse
    that decides whether a toplevel is wanted at all.

    MEASURED on the live dispatch ledger: 2167 dispatch creations,
    of which 2138 yield no measurable claim — so 98.7% of rows bought a git
    lookup and then discovered they had nothing to spend it on. At 0.0024s per
    spawn (200-spawn mean, same host, same measurement session) that is ~5.1s
    of pure
    subprocess for an answer the parser already held, and it is paid on the
    LIST path, per row, per render.

    The reorder is behaviour-preserving for every row that has a claim: nothing
    between the parse and the repo resolution reads `root`. What it changes is
    who pays, and it changes the refusal a claimless row gets from a statement
    about the repository to a statement about the row.
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="clearspan-order-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        run = lambda *a: subprocess.run(["git", "-C", self.tmp] + list(a),
                                        capture_output=True, text=True)
        run("init", "-q", ".")
        run("config", "user.email", "o@clearspan.local")
        run("config", "user.name", "order test")
        with open(os.path.join(self.tmp, "claimed.py"), "w") as f:
            f.write("# 1\n# 2\n# 3 the target\n")
        run("add", "-A")
        run("commit", "-q", "-m", "initial")

    def _control_then(self, note):
        """(control_argv, measured_argv, verdict, detail) through ONE spy.

        THE CONTROL RIDES IN THE SAME METHOD AS THE ZERO, and that is the whole
        shape of this helper rather than a convenience. "argv == []" is an
        absence claim, and an absence claim is satisfied by a spy that was never
        wired at all — which is not hypothetical here: clearspan's git calls
        read the name `subprocess` out of the module's globals, so rewriting
        one of them as `from subprocess import run` would detach this double
        silently and turn every zero-spawn arm green forever. So each arm first
        measures a row KNOWN to spawn, through the same spy object, and asserts
        that count unconditionally before believing its own zero.
        """
        real = clearspan.subprocess
        spy = _SpyProc(real)
        clearspan.subprocess = spy
        try:
            clearspan.re_measure(
                {"repo_id": self.tmp, "note": "claimed.py:3 is the target",
                 "lane": "spy-lane"})
            control = list(spy.argv)
            del spy.argv[:]
            verdict, detail = clearspan.re_measure(
                {"repo_id": self.tmp, "note": note, "lane": "spy-lane"})
            measured = list(spy.argv)
        finally:
            clearspan.subprocess = real
        return control, measured, verdict, detail

    def test_the_SPY_is_in_effect_and_a_claim_bearing_row_still_spawns(self):
        """The reorder must not make a REAL measurement free — that would be a
        reader that stopped looking, which is the failure this module exists to
        refuse.

        Exactly three, pinned as an equality and not a floor: `--show-toplevel`,
        `rev-parse HEAD`, and one `show` of the claimed file. A floor would
        happily accept a fourth spawn reappearing.
        """
        real = clearspan.subprocess
        spy = _SpyProc(real)
        clearspan.subprocess = spy
        try:
            verdict, detail = clearspan.re_measure(
                {"repo_id": self.tmp, "note": "claimed.py:3 is the target",
                 "lane": "spy-lane"})
            argv = list(spy.argv)
        finally:
            clearspan.subprocess = real
        # The verdict channel gets its own positive control before it is
        # compared to a constant: `assertEqual(verdict, FRESH)` is satisfied by
        # a reader that returned FRESH for no reason, and the detail is where
        # the reason has to appear.
        self.assertIn("claims fresh at head", detail,
                      "the row did not produce a measured FRESH sentence: %r"
                      % detail)
        self.assertEqual(verdict, clearspan.FRESH, detail)
        self.assertEqual(len(argv), 3,
                         "the spy saw %d git calls on a claim-bearing row; if "
                         "this is 0 the double is not in effect: %r"
                         % (len(argv), argv))
        self.assertIn("--show-toplevel", argv[0],
                      "a claim-bearing row must still resolve its repo: %r"
                      % (argv,))

    def test_a_row_with_NO_measurable_claim_spawns_EXACTLY_ZERO(self):
        """THE ARM THAT REDDENS ON REVERT. Move `_repo_root` back above the
        parse and this row buys a `rev-parse --show-toplevel` it discards, so
        the count is 1 and the equality fails. The fixture repo is REAL and
        resolvable on purpose: a dead path would make the old code refuse early
        for the wrong reason and hide the spawn it still paid for."""
        control, argv, verdict, detail = self._control_then(
            "wire owner triage properly")
        self.assertEqual(len(control), 3,
                         "the spy recorded %d calls on a row that MUST spawn, "
                         "so the zero below would mean nothing: %r"
                         % (len(control), control))
        self.assertEqual(verdict, clearspan.UNKNOWN, detail)
        self.assertEqual(argv, [],
                         "a row the parser had already answered still paid "
                         "for git: %r" % (argv,))

    def test_a_NOTELESS_row_spawns_EXACTLY_ZERO_too(self):
        """The 2081-row population, measured: rows carrying no note at all.
        They are the bulk of the ledger and they were the bulk of the waste."""
        control, argv, verdict, detail = self._control_then(None)
        self.assertEqual(len(control), 3,
                         "the spy recorded %d calls on a row that MUST spawn, "
                         "so the zero below would mean nothing: %r"
                         % (len(control), control))
        self.assertEqual(verdict, clearspan.UNKNOWN, detail)
        self.assertEqual(argv, [],
                         "a note-less row paid for git: %r" % (argv,))


class TheRefusalNamesWhatItActuallyReadTest(unittest.TestCase):
    """The refusal said "no measurable claims in row BODY" and the body is the
    one surface it never touched.

    `re_measure` parses the row's NOTE and LANE. The dispatched message is not
    among them and is not reachable from here: `dispatches.send` stores a
    blake2b-128 of the text and nothing else, and the chat store that held the
    text is tmpfs whose ``dm-*`` rooms the disk journal excludes by design.
    MEASURED: of 2094 delivered events carrying a delivery_ref, 9
    still resolve against the whole live chat store — 0.4%. So the sentence
    asserted a read that never happened AND could not be made true by trying
    harder; a reader that reached for the body would answer from RAM
    volatility, measurable before a reboot and unmeasurable after.

    The second half is the evidence column. "claims no-measurable-claims" is
    what every stale-bot KEEP line carries, and it fired identically on 2138 of
    2167 rows — uniform by construction, so it discriminates nothing. The two
    populations it merged are different and free to tell apart.
    """

    def _detail(self, note):
        return clearspan.re_measure(
            {"repo_id": os.getcwd(), "note": note, "lane": "surface-lane"})[1]

    def test_an_EMPTY_note_and_a_PROSE_note_get_DIFFERENT_refusals(self):
        """THE ARM THAT REDDENS ON REVERT, and the one the digest needs. Both
        rows are UNKNOWN and always were; the defect is that both got the same
        sentence, so a reader could not tell 'nobody wrote anything down' from
        'somebody wrote prose'. Restore the single string and these two become
        equal."""
        empty = self._detail(None)
        prose = self._detail("wire owner triage properly")
        self.assertIn("records no note", empty,
                      "an empty-note row did not say so: %r" % empty)
        self.assertIn("note and lane hold no", prose,
                      "a prose-note row did not name the surfaces read: %r"
                      % prose)
        self.assertNotEqual(
            empty, prose,
            "both populations got one sentence, so the evidence column is "
            "uniform by construction: %r" % empty)

    def test_the_refusal_never_claims_to_have_read_the_MESSAGE_BODY(self):
        """The pole. `assertNotIn` alone is satisfied by an empty string, by a
        crash-turned-blank, or by a refusal about something else entirely — so
        the positive identification comes first and the absence claim rides on
        top of a detail already proven to be THIS refusal.

        UNROLLED DELIBERATELY, the same reason as the version-stamp arm above:
        behind a loop, every control is conditional on the iterable, and an
        empty one reads exactly like two passes. Two populations, two named
        observables, six unconditional assertions.
        """
        empty = self._detail(None)
        self.assertIn("UNREAD, not unrotted", empty,
                      "empty-note: not the refusal under test: %r" % empty)
        self.assertIn("stored nowhere", empty,
                      "empty-note: the refusal did not say the message text "
                      "is unreachable: %r" % empty)
        self.assertNotIn("row body", empty,
                         "empty-note: the refusal still names a body it never "
                         "read: %r" % empty)
        prose = self._detail("wire owner triage")
        self.assertIn("UNREAD, not unrotted", prose,
                      "prose-note: not the refusal under test: %r" % prose)
        self.assertIn("stored nowhere", prose,
                      "prose-note: the refusal did not say the message text "
                      "is unreachable: %r" % prose)
        self.assertNotIn("row body", prose,
                         "prose-note: the refusal still names a body it never "
                         "read: %r" % prose)

    def test_the_UNKNOWN_docstring_no_longer_says_nothing_could_have_rotted(self):
        """The module docstring taught the opposite of the truth: UNKNOWN meant
        "not stale, because nothing measurable could have rotted", which is a
        claim about the ROW made from a reader that read 1.3% of the rows'
        evidence. A consumer reading the module to learn what the verdict means
        would have inherited exactly the overstatement the string carried."""
        doc = clearspan.__doc__ or ""
        self.assertIn("UNKNOWN", doc, "no docstring to judge: %r" % doc[:80])
        self.assertNotIn("nothing measurable could have rotted", doc,
                         "the docstring still promises the row is intact")
        self.assertIn("UNREAD, NOT UNROTTED", doc.upper(),
                      "the docstring does not state what UNKNOWN means now")


class ACountIsAnsweredOnlyByThePopulationItNamesTest(unittest.TestCase):
    """_check_counts re-counts OPEN DISPATCH ROWS and nothing else.

    THE DEFECT. It answered EVERY count claim against that one population, so
    a legitimate count of something else — "0 proof matches", which the parser
    is right to admit and an arm above deliberately pins — was published as
    `stale`: a rot verdict derived from a number that was never that quantity.
    Every disposition surface reads the VERDICT, not the prose beside it that
    names both populations.

    THE MODULE ALREADY STATED THE RULE and the counting-noun list grew past
    it: "a noun belongs here only if a row population is a sane thing to
    compare it against". TIME UNITS were excluded for exactly this reason.
    """

    def test_a_row_population_is_still_answered(self):
        got = clearspan._check_counts([(5, "stalled rows")])
        self.assertEqual(len(got), 1, got)
        self.assertIn(got[0][0], (clearspan.FRESH, clearspan.STALE))
        self.assertIn("open rows", got[0][1])

    def test_a_NON_row_population_is_DROPPED_not_verdicted(self):
        """DROPPED, NOT MARKED UNAVAILABLE, and the difference is the whole
        design: re_measure short-circuits on `unavailable and not stale`, so a
        marker here would take a row whose FILE-LINE claims are genuinely
        fresh and report the row as unreachable. A claim this check has no
        instrument for is an ABSENT measurement, not a failed one."""
        # POSITIVE CONTROL FIRST, same call, same shape: a row population IS
        # answered, so the emptiness below is about the subject and not about
        # the function declining everything.
        self.assertEqual(len(clearspan._check_counts([(5, "stalled rows")])), 1)
        for subject in ("proof matches", "failing tests", "changed files",
                        "commits ahead", "minutes elapsed"):
            with self.subTest(subject=subject):
                self.assertEqual(clearspan._check_counts([(3, subject)]), [],
                                 "%r was given a verdict against open rows"
                                 % subject)

    def test_the_predicate_reads_the_subject_the_parser_produced(self):
        self.assertTrue(clearspan._counts_rows("stalled rows"))
        self.assertTrue(clearspan._counts_rows("open dispatch rows"))
        self.assertFalse(clearspan._counts_rows("proof matches"))
        self.assertFalse(clearspan._counts_rows("minutes"))
        self.assertFalse(clearspan._counts_rows(""))

    def test_a_NOUN_naming_ANOTHER_LEDGER_is_not_this_population(self):
        """Every subject below names a SEPARATE live ledger — the task rows,
        the lane rows, the store entries, the advisory chat claims — whose
        population is counted by a different instrument and diverges from the
        open-dispatch-row count by an order of magnitude. Answering any of
        them against the dispatch count publishes the same rot verdict from
        the same wrong population that this class exists to remove.

        `total` is here for a different reason: it is a quantifier, not a
        noun naming anything, so admitting it re-opens the whole class
        through one word that can modify any subject at all."""
        # POSITIVE CONTROL, unconditional and on the same predicate: the one
        # population this check CAN answer still answers, so a False below is
        # about the subject and not about the predicate having gone dark.
        self.assertTrue(clearspan._counts_rows("open dispatch rows"))
        for subject in ("open tasks", "unlanded lanes", "entries in the store",
                        "items on the checklist", "open claims",
                        "total failures", "total"):
            with self.subTest(subject=subject):
                self.assertFalse(clearspan._counts_rows(subject),
                                 "%r names a different ledger and was "
                                 "admitted anyway" % subject)

    def test_the_HEAD_noun_decides_not_mere_presence(self):
        """PRESENCE IS NOT HEADSHIP, and the discriminating pair below shares
        the word `dispatch`: only its POSITION differs. A set intersection
        cannot tell them apart, so it answered a count of commits against the
        dispatch ledger."""
        self.assertTrue(clearspan._counts_rows("dispatch rows"))
        self.assertFalse(clearspan._counts_rows("commits touching dispatch"))
        self.assertFalse(clearspan._counts_rows("test files under tasks"))
        self.assertEqual(clearspan._head_noun("commits touching dispatch"),
                         "commits")
        self.assertEqual(clearspan._head_noun("dispatch rows"), "dispatch")

    def test_a_subject_naming_NO_population_yields_no_head(self):
        """A count admitted by the LEADING-WORD rule ("all 43 remaining")
        says a magnitude was counted and not WHAT was counted. There is no
        population to compare against, so the head is None and the caller
        must not invent one."""
        self.assertIsNone(clearspan._head_noun("remaining"))
        self.assertIsNone(clearspan._head_noun(""))
        self.assertEqual(clearspan._check_counts([(43, "remaining")]), [])
        # POSITIVE CONTROL on the same call: a headed subject still lands.
        self.assertEqual(len(clearspan._check_counts([(43, "stalled rows")])), 1)

    def test_a_MIXED_claim_set_keeps_the_answerable_half(self):
        got = clearspan._check_counts([(5, "stalled rows"), (0, "proof matches")])
        self.assertEqual(len(got), 1, got)
        self.assertIn("stalled rows", got[0][1])
        self.assertNotIn("proof matches", got[0][1])

    def test_the_PARSE_is_untouched_so_the_pinned_arm_above_still_holds(self):
        """The cure lives in the CHECK, not the parser, precisely so the
        deliberate ruling that a zero is a measurement stays true."""
        _, counts, _ = clearspan.parse_claims("0 proof matches")
        self.assertEqual(counts, [(0, "proof matches")])
