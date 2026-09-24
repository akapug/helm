#!/usr/bin/env python3

import contextlib
import io
import os
import tempfile
import unittest

from helm import landreq, seat_ledger as sl


INC = "1" * 32


class RenameLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="seat-rename-")
        self.path = os.path.join(self.tmp, "ledger.jsonl")

    def test_terminal_transitions_always_refuse_and_write_nothing(self):  # noqa: VACUOUS_ASSERTION — unconditional rename append above the terminal loop proves the same writer can grow this ledger
        control, err = sl.record(
            "control-old", sl.RENAMED, successor="control-new",
            incarnation=INC, evidence_ref="session:control", path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(control["transition"], sl.RENAMED)
        before = os.path.getsize(self.path)
        for transition in (sl.DISOWNED, sl.DECOMMISSIONED):
            with self.subTest(transition=transition):
                row, refusal = sl.record("old", transition, path=self.path)
                self.assertIsNone(row)
                self.assertEqual(refusal, sl.ABSENCE_REFUSAL)
        self.assertEqual(os.path.getsize(self.path), before)
        self.assertIn(sl.ABSENCE_SUCCESSOR_LANE, sl.ABSENCE_REFUSAL)

    def test_one_measured_rename_round_trips_by_incarnation(self):
        row, refusal = sl.record(
            "@Old", sl.RENAMED, successor="New", incarnation=INC,
            evidence_ref="session:abc123", path=self.path)
        self.assertIsNone(refusal, refusal)
        self.assertEqual((row["seat"], row["successor"]), ("old", "new"))
        rows, unavailable = sl.snapshot(self.path)
        self.assertIsNone(unavailable)
        final, state, detail = sl.resolve("old", INC, rows=rows)
        self.assertEqual((final, state), ("new", sl.MOVED), detail)

    def test_label_reuse_does_not_inherit_an_old_incarnation_edge(self):
        sl.record("old", sl.RENAMED, successor="new", incarnation=INC,
                  evidence_ref="session:abc123", path=self.path)
        final, state, _detail = sl.resolve("old", "2" * 32, path=self.path)
        self.assertEqual((final, state), ("old", sl.ACTIVE))

    def test_conflicting_or_cyclic_onward_facts_refuse_under_the_write_door(self):
        got, err = sl.record("a", sl.RENAMED, successor="b", incarnation=INC,
                             evidence_ref="session:one", path=self.path)
        self.assertIsNone(err, err)
        got, err = sl.record("a", sl.RENAMED, successor="c", incarnation=INC,
                             evidence_ref="session:two", path=self.path)
        self.assertIsNone(got)
        self.assertIn("one onward rename", err)
        got, err = sl.record("b", sl.RENAMED, successor="a", incarnation=INC,
                             evidence_ref="session:three", path=self.path)
        self.assertIsNone(got)
        self.assertIn("cycle", err)

    def test_strict_shared_reader_rejects_a_complete_malformed_row(self):
        with io.open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        rows, unavailable = sl.snapshot(self.path)
        self.assertEqual(rows, ())
        self.assertIn("corrupt ledger line", unavailable)
        got, refusal = sl.record(
            "a", sl.RENAMED, successor="b", incarnation=INC,
            evidence_ref="session:abc", path=self.path)
        self.assertIsNone(got)
        self.assertTrue(refusal)

    def test_unterminated_tail_is_outside_the_durability_boundary(self):
        with io.open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        rows, unavailable = sl.snapshot(self.path)
        self.assertEqual(rows, ())
        self.assertIsNone(unavailable)
        written, refusal = sl.record(
            "old", sl.RENAMED, successor="new", incarnation=INC,
            evidence_ref="session:tail-control", path=self.path)
        self.assertIsNone(refusal, refusal)
        self.assertEqual(sl.snapshot(self.path)[0], (written,))

    def test_shape_validation_uses_the_canonical_seat_token(self):
        control, err = sl.record(
            "valid-old", sl.RENAMED, successor="valid-new", incarnation=INC,
            evidence_ref="session:valid", path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(control["seat"], "valid-old")
        cases = (("bad seat", "new", INC, "session:a"),
                 ("old", "bad seat", INC, "session:a"),
                 ("old", "new", "ABC", "session:a"),
                 ("old", "new", INC, "bad evidence!"))
        for seat, successor, incarnation, evidence in cases:
            with self.subTest(seat=seat, successor=successor):
                got, refusal = sl.record(
                    seat, sl.RENAMED, successor=successor,
                    incarnation=incarnation, evidence_ref=evidence,
                    path=self.path)
                self.assertIsNone(got)
                self.assertTrue(refusal)

    def test_irreversible_cli_rejects_unknown_duplicate_and_missing_flags(self):  # noqa: VACUOUS_ASSERTION — the unconditional valid CLI call reaches the writer before any refusal loop
        calls = []
        real = sl.record

        def record(*args, **kwargs):
            calls.append((args, kwargs))
            return ({"seat": "old", "successor": "new",
                     "incarnation": INC}, None)
        sl.record = record
        try:
            valid = ["record", "old", "renamed", "--successor", "new",
                     "--incarnation", INC, "--evidence", "session:a"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sl.cmd_lifecycle(valid), 0)
            self.assertEqual(len(calls), 1)
            invalid = (
                valid + ["--dry-run"],
                ["record", "old", "renamed", "--successor", "new",
                 "--successor", "evil", "--incarnation", INC,
                 "--evidence", "session:a"],
                ["record", "old", "renamed", "--successor", "--incarnation", INC,
                 "--evidence", "session:a"],
            )
            for args in invalid:
                with self.subTest(args=args), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(sl.cmd_lifecycle(args), 2)
            self.assertEqual(len(calls), 1,
                             "a refused tail reached the irreversible writer")
        finally:
            sl.record = real

    def test_retirement_has_no_lifecycle_authority_probe(self):
        probes = [row[0] for row in landreq._REACH_PROBES]
        self.assertIn("roster", probes,
                      "the control probe table was unexpectedly empty")
        self.assertNotIn("lifecycle", probes)


if __name__ == "__main__":
    unittest.main()
