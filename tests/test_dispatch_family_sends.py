#!/usr/bin/env python3
"""A review row sent to a model family says how many that family was sent,
beside the family's burn reading.

The recipient rungs print a family's burn colour on a row sent to one of its
seats. Each of those lines is true about one send and silent about all of
them together: a family can be sent a day's worth of review rows in one night
while every line reads the same.

These arms pin the sum and the reading beside it. Every review row whose
recipient resolves to a model family gets one RECIPIENT line: the review rows
that family's seats were sent in the last 24 h and in the last 5 h, then the
family's burn reading off the CACHED fold (`helm burn`), or "burn unread" when
there is no fresh reading. Nothing here refuses a send: the count has no cap,
and the burn flags are what measure a family's budget.

ITS OWN MODULE BECAUSE `tests/test_dispatches.py` IS AT ITS BLOB CEILING. The
fixture (temp home, scratch repo, pinned project) is `DispatchBase`'s, reached
through the module so discovery does not run that suite twice.
"""
import json
import os
import time
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import burnflags, dispatches, family_sends, pk, proxywatch
from tests import test_dispatches as td

run = td.run
# THE RETIRED CAP KNOB, set only as a hostile input: a per-family daily cap
# once refused sends through it, and no send may be refused through it now.
RETIRED_CAP = "HELM_DISPATCH_FAMILY_DAILY_CAP"
COUNT = "in the last 24 h"
NOTES = dispatches._ADMISSION_NOTES
RATIONS = "the owner rations this family"


class FamilySendCountTest(td.DispatchBase):

    def setUp(self):
        super().setUp()
        self._knob_prior = os.environ.get(RETIRED_CAP)
        os.environ.pop(RETIRED_CAP, None)
        self._lane = 0

    def tearDown(self):
        if self._knob_prior is None:
            os.environ.pop(RETIRED_CAP, None)
        else:
            os.environ[RETIRED_CAP] = self._knob_prior
        super().tearDown()

    def declare(self, family, colour="ORANGE"):
        ok, err = burnflags.declare(family, colour, time.time() + 6 * 3600,
                                    why=RATIONS)
        self.assertTrue(ok, err)

    def fold(self, upstream=None, now=None):
        """The burn snapshot the send reads, written by the fold's own writer
        from the declarations on disk and the upstream records given."""
        self.assertTrue(burnflags.write_snapshot({
            "ceiling": 90.0, "money": {}, "money_measured_at": {},
            "upstream": upstream or {}, "anthropic_history": None,
            "declarations": burnflags.read_declarations()}, now=now))

    @staticmethod
    def quota_wall():
        """The upstream record of a family whose vendor refused on its usage
        quota an hour ago: the fold reads it as RED on the money axis."""
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 3600))
        return {"state": proxywatch._QUOTA_WALL, "dark": True,
                "since": since}

    def review(self, recipient, **kwargs):
        """(row, refusal) for one row through add(), a review by default."""
        self._lane += 1
        args = {"lane": "family-lane-%d" % self._lane, "ref": self.a,
                "repo": self.repo, "kind": "review", "new_work": True,
                "notify": False, "_reason": True}
        args.update(kwargs)
        if args["kind"] == "review" and args.get("new_work"):
            args.setdefault("task", self.review_task["id"])
        return dispatches.add(recipient, **args)

    def admit(self, recipient, **kwargs):
        """The row one admitted write returned; a refusal fails the arm."""
        row, why = self.review(recipient, **kwargs)
        self.assertIsNotNone(row, why)
        return row

    def send(self, recipient, *flags):
        """(rc, stdout, stderr) for a review row sent through the CLI."""
        self._lane += 1
        return run(dispatches.cmd_dispatch,
                   ["send", recipient, "family-send-%d" % self._lane,
                    "read the delta at this tip and cure what you find",
                    "--ref", self.a, "--kind", "review", "--new-work",
                    "--task", self.review_task["id"], "--part", "--repo", self.repo,
                    *flags])

    def counts(self, row):
        return [n for n in row.get(NOTES, ()) if COUNT in n]

    def line(self, row):
        """The one count line on a row; none, or two, fails the arm."""
        counted = self.counts(row)
        self.assertEqual(len(counted), 1, row.get(NOTES))
        return counted[0]

    # -- the sum, and the burn reading beside it ------------------------------

    def test_the_count_line_sits_under_the_colour_and_carries_the_reading(self):  # noqa: VACUOUS_ASSERTION — the row's colour line and count line are each pinned present, exact and adjacent
        self.declare("kimi")
        self.fold()
        for recipient, kind in (("kimi", "review"), ("kimi", "review"),
                                ("grok", "review"), ("kimi", "build")):
            self.admit(recipient, kind=kind)
        row = self.admit("kimi")
        notes = list(row[NOTES])
        orange = [n for n in notes if "kimi is ORANGE" in n]
        self.assertEqual(len(orange), 1, notes)
        counted = self.line(row)
        # THE SUM SITS DIRECTLY UNDER THE COLOUR LINE, and it is one line.
        self.assertEqual(notes.index(counted), notes.index(orange[0]) + 1)
        self.assertEqual(
            counted, "kimi: 3 review rows sent to kimi in the last 24 h "
                     "(3 in the last 5 h) · burn ORANGE declared: " + RATIONS)

    def test_the_line_carries_the_cached_measured_reading_and_its_reason(self):
        """(b) A fresh fold that MEASURED the family (its vendor refused on
        its usage quota) is on the line with its colour, axis and reason."""
        self.fold(upstream={"kimi": self.quota_wall()})
        flag = burnflags.family_flag("kimi")
        self.assertEqual((flag["colour"], flag["axis"]),
                         (burnflags.RED, "money"))
        # A MONEY-RED family is refused by the budget rung unless forced;
        # that refusal is the burn flag's, not this line's.
        counted = self.line(self.admit("kimi", force=True))
        self.assertEqual(
            counted, "kimi: 1 review row sent to kimi in the last 24 h "
                     "(1 in the last 5 h) · burn RED money: " + flag["cause"])
        self.assertIn("the vendor refused on its usage quota", counted)
        rc, _out, err = self.send("kimi", "--force", "--reason",
                                   "read the quota wall's repair")
        self.assertEqual(rc, 0, err)
        lines = [l for l in err.splitlines() if COUNT in l]
        self.assertEqual(lines, [
            "helm dispatch: RECIPIENT: kimi: 2 review rows sent to kimi in "
            "the last 24 h (2 in the last 5 h) · burn RED money: "
            + flag["cause"]], err)

    def test_an_absent_stale_or_unreadable_reading_says_burn_unread(self):  # noqa: VACUOUS_ASSERTION — the same send on a fresh fold in this method prints the ORANGE reading the stale fold must not
        """(c) No snapshot, one past its bound, and one that cannot be read
        each print "burn unread": never GREEN, never the old colour, and
        never a line with the reading left off."""
        ended = " · burn unread"
        self.assertTrue(self.line(self.admit("kimi")).endswith(ended))
        # STALE: a fold of this declaration, taken past the bound.
        self.declare("kimi")
        self.fold(now=time.time() - burnflags.max_age_s() - 60)
        stale = self.line(self.admit("kimi"))
        self.assertTrue(stale.endswith(ended), stale)
        self.assertNotIn("ORANGE", stale)
        # CONTROL: the same declaration folded now is on the line, so the
        # absence above is the bound and not a reading this line never shows.
        self.fold()
        self.assertTrue(self.line(self.admit("kimi")).endswith(
            " · burn ORANGE declared: " + RATIONS))
        # UNREADABLE: not JSON, and JSON whose instant is not a number (the
        # cached reader raises on that one rather than answering None).
        for body in ("{not json", json.dumps({
                "v": 1, "fold_version": burnflags.FOLD_VERSION,
                "ts": "yesterday", "families": {}})):
            pk.atomic_write(burnflags.snapshot_path(), body)
            unread = self.line(self.admit("kimi"))
            self.assertTrue(unread.endswith(ended), unread)
            self.assertNotIn("GREEN", unread)
            self.assertNotIn("ORANGE", unread)

    def test_the_send_path_never_measures_the_burn(self):  # noqa: VACUOUS_ASSERTION — the zero call counts sit beside the cached reading positively asserted on the same send's line
        """(d) The reading is the cached fold. Every door that MEASURES the
        burn (read the inputs, fold them, write the snapshot) raises here,
        and the send still succeeds with the cached reading on its line."""
        self.declare("kimi")
        self.fold()
        doors = {}
        with mock.patch.object(
                burnflags, "read_inputs",
                side_effect=AssertionError("a send read the burn inputs")) \
                as doors["read_inputs"], mock.patch.object(
                burnflags, "fold",
                side_effect=AssertionError("a send folded the burn")) \
                as doors["fold"], mock.patch.object(
                burnflags, "write_snapshot",
                side_effect=AssertionError("a send wrote the burn")) \
                as doors["write_snapshot"]:
            rc, _out, err = self.send("kimi")
            row = self.admit("kimi")
        self.assertEqual(rc, 0, err)
        self.assertEqual({name: door.call_count
                          for name, door in doors.items()},
                         {"read_inputs": 0, "fold": 0, "write_snapshot": 0})
        lines = [l for l in err.splitlines() if COUNT in l]
        self.assertEqual(lines, [
            "helm dispatch: RECIPIENT: kimi: 1 review row sent to kimi in "
            "the last 24 h (1 in the last 5 h) · burn ORANGE declared: "
            + RATIONS], err)
        self.assertTrue(self.line(row).endswith(
            " · burn ORANGE declared: " + RATIONS))

    def test_every_seat_of_the_family_counts_and_no_other_familys_seat(self):
        for recipient in ("codex-42", "codex", "kimi", "grok"):
            self.admit(recipient)
        self.assertTrue(self.line(self.admit("codex-42")).startswith(
            "codex-42: 3 review rows sent to codex in the last 24 h "
            "(3 in the last 5 h) · "))

    def test_the_cli_prints_the_sum_on_the_recipient_line(self):
        for _sent in (1, 2):
            rc, out, err = self.send("kimi")
            self.assertEqual(rc, 0, err)
            self.assertIn("REVIEW PROCEDURE", out)
        lines = [l for l in err.splitlines() if COUNT in l]
        self.assertEqual(lines, [
            "helm dispatch: RECIPIENT: kimi: 2 review rows sent to kimi in "
            "the last 24 h (2 in the last 5 h) · burn unread"], err)

    def test_the_sum_adds_no_ledger_read(self):  # noqa: VACUOUS_ASSERTION — the counted write after the uncounted one in this method is asserted to carry the line
        """THE COST CONTRACT: counting reuses the snapshot the ledger writer
        already reads for its duplicate check, so a counted write reads the
        ledger exactly as often as one whose recipient has no family."""
        self.admit("kimi")
        with mock.patch.object(family_sends, "_family_resolver",
                               return_value=lambda name: None), \
                mock.patch.object(dispatches, "snapshot",
                                  wraps=dispatches.snapshot) as snap:
            row = self.admit("kimi")
        self.assertEqual(self.counts(row), [])
        uncounted = snap.call_count
        self.assertGreaterEqual(uncounted, 1)
        with mock.patch.object(dispatches, "snapshot",
                               wraps=dispatches.snapshot) as snap:
            row = self.admit("kimi")
        self.assertIn("3 review rows sent to kimi", self.line(row))
        self.assertEqual(snap.call_count, uncounted)

    def test_a_recipient_with_no_family_and_a_build_row_get_no_line(self):  # noqa: VACUOUS_ASSERTION — the review row to kimi in this method carries the line the other two lack
        self.assertEqual(self.counts(self.admit("target-seat")), [])
        self.assertEqual(self.counts(self.admit("kimi", kind="build")), [])
        self.assertIn("1 review row sent to kimi",
                      self.line(self.admit("kimi")))

    # -- (a) no send is ever refused ------------------------------------------

    def test_no_send_is_refused_whatever_the_count(self):  # noqa: VACUOUS_ASSERTION — every write in this method is positively asserted admitted and counted
        """The retired cap knob, set to zero for the family, is a hostile
        input: every review row is admitted and counted, and the line never
        names the knob."""
        os.environ[RETIRED_CAP] = "kimi=0,grok=0,codex=1"
        for sent in range(1, 5):
            counted = self.line(self.admit("kimi"))
            self.assertIn("%d review row%s sent to kimi"
                          % (sent, "" if sent == 1 else "s"), counted)
            self.assertNotIn(RETIRED_CAP, counted)
            self.assertNotIn("cap", counted)
        for recipient in ("codex", "codex-42", "grok"):
            self.admit(recipient)
        for sent in (5, 6):
            rc, _out, err = self.send("kimi")
            self.assertEqual(rc, 0, err)
            self.assertIn("%d review rows sent to kimi" % sent, err)
        self.assertEqual(len(dispatches.snapshot()[0]), 9)

    def test_a_count_that_raises_admits_the_row_and_says_unknown(self):  # noqa: VACUOUS_ASSERTION — the stored row's absence of the note sits beside the returned row's exact UNKNOWN line from the same write
        """THE HOOK RUNS INSIDE THE LEDGER WRITER, so a count that raises
        must never reach it. A roster read that fails and a count that fails
        each leave the row written, stored as an uncounted row would be, and
        the note says UNKNOWN: never a number, never nothing, never a
        refusal. Without the tally's own guard these writes raise out of
        `_append_dispatch` instead."""
        broken = RuntimeError("roster torn mid-read")
        failed = ("the count itself failed (RuntimeError: roster torn "
                  "mid-read); this row was admitted uncounted · burn unread")
        with mock.patch.object(family_sends, "_family_resolver",
                               side_effect=broken):
            unresolved = self.admit("kimi")
            rc, _out, err = self.send("kimi")
        with mock.patch.object(family_sends, "count", side_effect=broken):
            uncounted = self.admit("kimi")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            [n for n in unresolved[NOTES] if "UNKNOWN" in n],
            ["kimi: review rows sent to this family UNKNOWN — " + failed])
        self.assertEqual(
            [n for n in uncounted[NOTES] if "UNKNOWN" in n],
            ["kimi: review rows sent to kimi UNKNOWN — " + failed])
        self.assertEqual(
            [l for l in err.splitlines() if "UNKNOWN" in l],
            ["helm dispatch: RECIPIENT: kimi: review rows sent to this "
             "family UNKNOWN — " + failed], err)
        stored = dispatches.snapshot()[0]
        self.assertEqual(len(stored), 3)
        self.assertNotIn("count itself failed",
                         json.dumps(stored[uncounted["id"]], default=str))

    def test_two_writers_racing_both_land_and_count_each_other(self):
        """The count is the snapshot each try appends against: a ledger that
        moved between that read and the append sends the try round again
        from a fresh read, so the second writer counts the first and neither
        is refused."""
        import threading
        barrier = threading.Barrier(2)
        original = dispatches._append_dispatch
        results = []

        def append(row, **kwargs):
            barrier.wait()
            return original(row, **kwargs)

        def write():
            results.append(self.review("kimi"))

        with mock.patch.object(dispatches, "_append_dispatch",
                               side_effect=append):
            threads = [threading.Thread(target=write) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        rows = [row for row, _why in results]
        self.assertEqual(len([r for r in rows if r is not None]), 2, results)
        self.assertEqual(sorted(self.line(r).split(" sent to ")[0]
                                for r in rows),
                         ["kimi: 1 review row", "kimi: 2 review rows"])
        self.assertEqual(len(dispatches.snapshot()[0]), 2)

    # -- the window -----------------------------------------------------------

    def test_rows_outside_the_window_do_not_count(self):
        old = self.admit("kimi")
        self.age(old["id"], 25 * 3600)
        mid = self.admit("kimi")
        self.age(mid["id"], 6 * 3600)
        self.assertIn("2 review rows sent to kimi in the last 24 h "
                      "(1 in the last 5 h)", self.line(self.admit("kimi")))


if __name__ == "__main__":
    unittest.main()
