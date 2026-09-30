#!/usr/bin/env python3
"""The friction tax on a task row (helm/tasks.py).

Every step an agent repeats by hand, every guard workaround, wait, re-read,
re-ask or false refusal is a tax paid on every later task that meets it. A row
that removes one records it: `tax` is the agent steps per day it removes (the
steps each time x the times a day), `tax_cost` is the build cost in steps, and
PAYBACK DAYS = tax_cost / tax. A cut that pays back within two days goes ahead
of the other rows of its rank.

These arms carry the contract: the field round-trips, an old row without it
still reads, the doors refuse a tax that is not a positive number, payback is
cost over tax, the orderings put a fast cut first inside its rank, and `show`
prints the line.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import cli_help, tasks  # noqa: E402
from tests.test_tasks import CliBase, TasksBase  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TaxRoundTripTest(TasksBase):
    """The field is stored, read back and published."""

    def test_add_stores_the_tax_and_the_cost_and_reads_them_back(self):
        row, err = tasks.add("retire the hand steps of every land",
                             owner="s", tax=300, tax_cost=150, path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(300, row["tax"])
        self.assertEqual(150, row["tax_cost"])
        back = tasks.get(row["id"], path=self.path)
        self.assertEqual(300, back["tax"])
        self.assertEqual(150, back["tax_cost"])
        self.assertEqual((300, 150, 0.5), tasks.tax_of(back))
        self.assertEqual(0.5, tasks.public_row(back)["payback_days"])

    def test_update_sets_the_tax_and_an_empty_clear_removes_the_key(self):  # noqa: VACUOUS_ASSERTION — the assertEqual on the SAME row key two lines up proves the field was written before the clear removes it
        row, err = tasks.add("measure the hook re-reads", owner="s",
                             path=self.path)
        self.assertIsNone(err, err)
        got, err = tasks.update(row["id"], tax=40, tax_cost=80,
                                path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(40, tasks.get(row["id"], path=self.path)["tax"])
        got, err = tasks.update(row["id"], tax=None, tax_cost=None,
                                path=self.path)
        self.assertIsNone(err, err)
        cleared = tasks.get(row["id"], path=self.path)
        self.assertEqual("measure the hook re-reads", cleared["title"])
        self.assertNotIn("tax", cleared)
        self.assertNotIn("tax_cost", cleared)

    def test_an_untaxed_row_keeps_the_shape_it_had(self):  # noqa: VACUOUS_ASSERTION — the taxed row beside it is the control on the SAME keys: the door does write them when asked
        """Only when present, like `posture_na`: a reader that predates the
        field sees the same row shape for every row nobody taxed."""
        taxed, err = tasks.add("shorten the gate loop", owner="s", tax=12,
                               path=self.path)
        self.assertIsNone(err, err)
        self.assertIn("tax", taxed)
        plain, err = tasks.add("draw a colour legend on the board",
                               owner="s", path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual("draw a colour legend on the board", plain["title"])
        self.assertNotIn("tax", plain)
        self.assertNotIn("tax_cost", plain)
        self.assertNotIn("payback_days", tasks.public_row(plain))

    def test_an_old_record_without_the_field_still_reads_and_updates(self):
        """A row written before the field existed: every reader answers
        UNKNOWN for its tax, and an ordinary edit does not invent one."""
        legacy = {"id": "task/7", "ts": 1_700_000_000.0,
                  "last_updated": 1_700_000_000.0, "title": "an old row",
                  "status": "open", "owner": "s", "note": None, "refs": [],
                  "source": None, "origin": None, "closed_reason": None,
                  "continues": None, "priority": "P2", "comments": []}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(legacy) + "\n")
        row = tasks.get("task/7", path=self.path)
        self.assertEqual("an old row", row["title"])
        self.assertEqual((None, None, None), tasks.tax_of(row))
        self.assertIsNone(tasks.tax_line(row))
        self.assertIn("an old row", tasks._fmt(row))
        self.assertEqual("an old row", tasks.public_row(row)["title"])
        got, err = tasks.update("task/7", title="an old row, retitled",
                                path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual("an old row, retitled", got["title"])
        self.assertNotIn("tax", got)

    def test_a_junk_stored_value_reads_as_absent_and_never_raises(self):
        """A hand-edited ledger can hold anything. The one reader answers
        None for a value no door would store rather than crash a listing."""
        for junk in ("300", 0, -4, True, float("nan"), [1]):
            with self.subTest(junk=junk):
                self.assertEqual((None, 10, None),
                                 tasks.tax_of({"tax": junk, "tax_cost": 10}))
                self.assertEqual((10, None, None),
                                 tasks.tax_of({"tax": 10, "tax_cost": junk}))
        self.assertEqual((10, None, None), tasks.tax_of({"tax": 10}))
        self.assertEqual((None, None, None), tasks.tax_of(None))


class TaxValidationTest(CliBase):
    """A tax is a positive number of steps. Zero, a negative, a non-number
    and a cost with no tax are refused at the API door and at the CLI."""

    def test_the_API_door_refuses_what_is_not_a_positive_number(self):
        ok, err = tasks.add("cut the land steps down", owner="s", tax=300,
                            path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(300, ok["tax"])
        cases = ((0, "more than zero"), (-5, "more than zero"),
                 ("300", "number"), (True, "number"),
                 (float("nan"), "finite"), (float("inf"), "finite"),
                 (10 ** 400, "finite"))
        for i, (bad, why) in enumerate(cases):
            with self.subTest(tax=bad):
                row, err = tasks.add("bad tax row number %d" % i, owner="s",
                                     tax=bad, path=self.path)
                self.assertIsNone(row)
                self.assertIn(why, err)
        row, err = tasks.add("bad cost row", owner="s", tax=5, tax_cost=0,
                             path=self.path)
        self.assertIsNone(row)
        self.assertIn("more than zero", err)

    def test_a_cost_with_no_tax_is_refused_at_both_doors(self):
        row, err = tasks.add("a cost alone", owner="s", tax_cost=100,
                             path=self.path)
        self.assertIsNone(row)
        self.assertIn("no tax", err)
        base, err = tasks.add("a row to tax later", owner="s", path=self.path)
        self.assertIsNone(err, err)
        row, err = tasks.update(base["id"], tax_cost=100, path=self.path)
        self.assertIsNone(row)
        self.assertIn("no tax", err)
        taxed, err = tasks.update(base["id"], tax=10, tax_cost=100,
                                  path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(10.0, tasks.tax_of(taxed)[2])
        # clearing the tax and leaving the cost is the same state
        row, err = tasks.update(base["id"], tax=None, path=self.path)
        self.assertIsNone(row)
        self.assertIn("no tax", err)

    def test_the_library_error_names_no_flag_and_the_cli_adds_it(self):  # noqa: VACUOUS_ASSERTION — the assertIn("no tax") on the SAME err is the positive control: the refusal text is there, only the flag is not
        """add() and update() are API doors too, and an error naming a flag
        tells an API caller to use a surface it cannot reach (the rule
        `_cli_error` states). The library says the fact; the CLI adds the
        spelling."""
        row, err = tasks.add("a cost with nothing to divide", owner="s",
                             tax_cost=100, path=self.path)
        self.assertIsNone(row)
        self.assertIn("no tax", err)
        self.assertNotIn("--tax", err)
        rc, _out, err = self.cli("add", "a taxed row the count starts at",
                                 "--tax", "5")
        self.assertEqual(0, rc, err)
        before = self.ledger_lines()
        rc, _out, err = self.cli("add", "a cli cost with no tax",
                                 "--tax-cost", "5")
        self.assertEqual(2, rc, err)
        self.assertIn("no tax", err)
        self.assertIn("--tax N", err)
        self.assertEqual(before, self.ledger_lines())

    def test_update_refuses_zero_and_negative(self):
        base, err = tasks.add("a row to retax", owner="s", path=self.path)
        self.assertIsNone(err, err)
        for bad in (0, -1, "x"):
            with self.subTest(tax=bad):
                row, err = tasks.update(base["id"], tax=bad, path=self.path)
                self.assertIsNone(row)
                self.assertIn("tax must", err)
        row, err = tasks.update(base["id"], tax=2.5, path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(2.5, row["tax"])

    def test_the_cli_refuses_zero_negative_and_non_numbers(self):
        rc, _out, err = self.cli("add", "a real taxed row", "--tax", "300",
                                 "--tax-cost", "150")
        self.assertEqual(0, rc, err)
        filed = self.filed("a real taxed row")
        self.assertEqual(300, filed["tax"])
        self.assertEqual(150, filed["tax_cost"])
        before = self.ledger_lines()
        self.assertGreater(before, 0, "the ledger never received the row")
        for argv, why in ((("--tax", "0"), "more than zero"),
                          (("--tax=-5",), "more than zero"),
                          (("--tax", "abc"), "not a number"),
                          (("--tax",), "needs a value"),
                          (("--tax", "5", "--tax-cost", "0"),
                           "more than zero"),
                          (("--tax-cost", "5"), "no tax")):
            with self.subTest(argv=argv):
                rc, _out, err = self.cli("add", "a refused row", *argv)
                self.assertEqual(2, rc, err)
                self.assertIn(why, err)
                self.assertEqual(before, self.ledger_lines())
        rc, _out, err = self.cli("update", filed["id"], "--tax", "0")
        self.assertEqual(2, rc, err)
        self.assertIn("more than zero", err)
        self.assertEqual(before, self.ledger_lines())

    def test_the_cli_update_sets_and_clears(self):
        rc, _out, err = self.cli("add", "tax me later please")
        self.assertEqual(0, rc, err)
        tid = self.filed("tax me later please")["id"]
        rc, out, err = self.cli("update", tid, "--tax", "20",
                                "--tax-cost", "10")
        self.assertEqual(0, rc, err)
        self.assertIn("friction tax: 20 steps/day; payback 0.5 days", out)
        self.assertEqual(20, self.filed("tax me later please")["tax"])
        rc, _out, err = self.cli("update", tid, "--tax=", "--tax-cost=")
        self.assertEqual(0, rc, err)
        cleared = self.filed("tax me later please")
        self.assertEqual("tax me later please", cleared["title"])
        self.assertNotIn("tax", cleared)


class PaybackMathTest(unittest.TestCase):
    """PAYBACK DAYS = build cost / tax removed per day."""

    def test_payback_is_cost_over_tax(self):
        self.assertEqual((300, 300, 1.0),
                         tasks.tax_of({"tax": 300, "tax_cost": 300}))
        self.assertEqual((40, 200, 5.0),
                         tasks.tax_of({"tax": 40, "tax_cost": 200}))
        self.assertEqual((300, None, None), tasks.tax_of({"tax": 300}))

    def test_the_two_day_line_is_inclusive(self):
        self.assertEqual(2.0, tasks.PAYBACK_AHEAD_DAYS)
        self.assertTrue(tasks.fast_tax_cut({"tax": 50, "tax_cost": 100}))
        self.assertFalse(tasks.fast_tax_cut({"tax": 50, "tax_cost": 101}))
        self.assertFalse(tasks.fast_tax_cut({"tax": 50}))
        self.assertFalse(tasks.fast_tax_cut({}))

    def test_a_shown_payback_never_rounds_down_onto_the_line(self):
        """The board compares the exact payback and the line prints it. To
        the nearest tenth, 2.04 days printed "payback 2 days" while the row
        stayed behind the fast cuts, so a reader saw a 2-day cut that did
        not go first. Rounded UP, a shown payback of 2 days or less is one."""
        slow = {"tax": 49, "tax_cost": 100}       # 2.04 days
        self.assertFalse(tasks.fast_tax_cut(slow))
        self.assertIn("payback 2.1 days", tasks.tax_line(slow))
        self.assertIn("payback 2.1 days", tasks._tax_cell(slow))
        fast = {"tax": 50, "tax_cost": 100}       # exactly 2 days
        self.assertTrue(tasks.fast_tax_cut(fast))
        self.assertIn("payback 2 days", tasks.tax_line(fast))
        # float noise is not a tenth more: 2.1 / 0.3 is 7.000000000000001
        self.assertIn("payback 7 days",
                      tasks.tax_line({"tax": 0.3, "tax_cost": 2.1}))
        self.assertEqual("under 0.1 days", tasks._days(0.05))
        # a payback past float range reads as text, never a traceback
        self.assertIn("days", tasks._days(float("inf")))

    def test_the_show_line_says_the_tax_and_the_payback(self):
        self.assertEqual("friction tax: 300 steps/day; payback 1 day "
                         "(build cost 300 steps)",
                         tasks.tax_line({"tax": 300, "tax_cost": 300}))
        self.assertEqual("friction tax: 40 steps/day; payback 5 days "
                         "(build cost 200 steps)",
                         tasks.tax_line({"tax": 40, "tax_cost": 200}))
        self.assertIn("payback UNKNOWN", tasks.tax_line({"tax": 12}))
        self.assertIsNone(tasks.tax_line({"title": "untaxed"}))


class TaxOrderingTest(TasksBase):
    """Inside one rank a fast tax cut goes first. Rank still wins."""

    NOW = 1_700_000_000.0

    def row(self, tid, priority=None, ago_days=0, **tax):
        r = {"id": tid, "priority": priority, "status": "open",
             "ts": self.NOW - ago_days * 86400.0}
        r.update(tax)
        return r

    def test_board_key_puts_a_fast_cut_ahead_of_older_rows_of_its_rank(self):
        rows = [self.row("task/1", "P1", ago_days=90),
                self.row("task/2", "P1", ago_days=1, tax=300, tax_cost=300),
                self.row("task/3", "P1", ago_days=40, tax=10, tax_cost=500),
                self.row("task/4", "P0", ago_days=2),
                self.row("task/5", "P2", ago_days=0, tax=900, tax_cost=10)]
        got = [r["id"] for r in sorted(rows, key=tasks.board_key)]
        # P0 first whatever the tax; inside P1 the fast cut first, then the
        # rest oldest first (a slow cut is not ahead of anything).
        self.assertEqual(["task/4", "task/2", "task/1", "task/3", "task/5"],
                         got)

    def test_two_fast_cuts_order_by_the_shorter_payback(self):
        slow = self.row("task/8", "P2", ago_days=9, tax=100, tax_cost=150)
        fast = self.row("task/9", "P2", ago_days=1, tax=100, tax_cost=20)
        self.assertEqual(["task/9", "task/8"],
                         [r["id"] for r in sorted([slow, fast],
                                                  key=tasks.board_key)])

    def test_untaxed_rows_keep_their_oldest_first_order(self):
        """The control: the new key part is constant for untaxed rows, so the
        order every existing reader had is unchanged."""
        young, old = self.row("task/4", "P1", ago_days=1), \
            self.row("task/40", "P1", ago_days=60)
        self.assertEqual(["task/40", "task/4"],
                         [r["id"] for r in sorted([young, old],
                                                  key=tasks.board_key)])

    def test_story_children_follow_board_order_inside_their_rank(self):
        """A parent's children print oldest first inside one rank, and a fast
        tax cut child first. They printed NEWEST first before: the walk
        reversed the sort for its stack and so reversed every tie."""
        rows = [{"id": "task/1", "continues": None, "priority": None,
                 "status": "open", "ts": self.NOW - 99 * 86400.0},
                self.row("task/2", None, ago_days=30),
                self.row("task/3", None, ago_days=20),
                self.row("task/4", None, ago_days=10, tax=60, tax_cost=30)]
        for r in rows[1:]:
            r["continues"] = "task/1"
        got = [r["id"] for r, _i in tasks._story_order(
            tasks.board_order(rows))]
        self.assertEqual(["task/1", "task/4", "task/2", "task/3"], got)


class TaxCliTest(CliBase):
    """`list --by-tax`, `show`, the default listing and `triage`."""

    def file(self, title, *argv):
        rc, _out, err = self.cli("add", title, *argv)
        self.assertEqual(0, rc, err)
        return self.filed(title)["id"]

    def test_list_by_tax_ranks_highest_tax_first_and_shows_payback(self):
        small = self.file("shave the review retry wait", "--tax", "40",
                          "--tax-cost", "400")
        big = self.file("automate the whole land train", "--tax", "300",
                        "--tax-cost", "300")
        plain = self.file("paint the board header blue")
        rc, out, err = self.cli("list", "--by-tax")
        self.assertEqual(0, rc, err)
        self.assertIn(big, out)
        self.assertIn(small, out)
        self.assertLess(out.index(big), out.index(small))
        self.assertIn("300/day", out)
        self.assertIn("payback 1 day", out)
        self.assertIn("payback 10 days", out)
        self.assertNotIn(plain, out)
        self.assertIn("1 open row(s) carry no tax", out)

    def test_list_by_tax_json_is_in_tax_order_with_payback(self):
        small = self.file("trim the stop hook reads", "--tax", "5",
                          "--tax-cost", "5")
        big = self.file("fold the gate audits into one call", "--tax",
                        "90", "--tax-cost", "45")
        self.file("rename the settings tab")
        rc, out, err = self.cli("list", "--by-tax", "--json")
        self.assertEqual(0, rc, err)
        got = json.loads(out)
        self.assertEqual([big, small], [r["id"] for r in got])
        self.assertEqual([0.5, 1.0], [r["payback_days"] for r in got])

    def test_the_default_listing_puts_a_fast_cut_first_in_its_rank(self):
        older = self.file("add a sparkline to the burn card")
        cut = self.file("stop re-reading the brief every turn", "--tax",
                        "120", "--tax-cost", "60")
        rc, out, err = self.cli("list")
        self.assertEqual(0, rc, err)
        self.assertIn(older, out)
        self.assertLess(out.index(cut), out.index(older))

    def test_show_prints_the_tax_line_and_an_untaxed_row_prints_none(self):
        cut = self.file("cache the roster read", "--tax", "300",
                        "--tax-cost", "300")
        plain = self.file("widen the chat column")
        rc, out, err = self.cli("show", cut)
        self.assertEqual(0, rc, err)
        self.assertIn("friction tax: 300 steps/day; payback 1 day", out)
        rc, out, err = self.cli("show", plain)
        self.assertEqual(0, rc, err)
        self.assertIn("widen the chat column", out)
        self.assertNotIn("friction tax", out)

    def test_triage_takes_a_fast_cut_first(self):  # noqa: VACUOUS_ASSERTION — the older row's missing rank is the contract; the cut's "P2" on the same ledger read proves the pass wrote
        self.scope()
        older = self.file("sort the goal cards by age")
        cut = self.file("drop the double gate read", "--tax", "200",
                        "--tax-cost", "100")
        rc, out, err = self.cli("triage", "--limit", "1")
        self.assertEqual(0, rc, err)
        self.assertIn("this pass covers 1 of them", out)
        self.assertIn(cut, out)
        self.assertNotIn(older, out)
        self.assertIn("tax cut", out)
        self.admit()
        rc, out, err = self.cli("triage", "--apply", "--limit", "1")
        self.assertEqual(0, rc, err)
        self.assertEqual("P2", self.filed("drop the double gate read")
                         ["priority"])
        self.assertIsNone(self.filed("sort the goal cards by age")
                          .get("priority"))


class TaxSurfaceTest(unittest.TestCase):
    """Every door and every help surface names the flags."""

    def test_every_tax_flag_reaches_the_parser_and_the_usage(self):
        self.assertEqual(("--tax", "--tax-cost"), tasks.TAX_FLAGS)
        for flag in tasks.TAX_FLAGS:
            with self.subTest(flag):
                self.assertIn(flag, tasks._VALUED_FLAGS)
                self.assertIn(flag, tasks.USAGE)
        self.assertIn("--by-tax", tasks.USAGE)

    def test_the_verb_help_and_the_reference_name_the_flags(self):
        entry = cli_help._VERB_HELP["task"]
        with open(os.path.join(REPO, "docs", "VERBS.md"),
                  encoding="utf-8") as f:
            doc = f.read()
        head = next(line for line in doc.splitlines()
                    if line.startswith("### `helm task add"))
        for flag in ("--tax N", "--tax-cost N", "--by-tax"):
            with self.subTest(flag):
                self.assertIn(flag, entry)
                self.assertIn(flag, head)
        self.assertIn("friction tax", entry)
        self.assertIn("**THE FRICTION TAX", doc)
        self.assertIn("payback days", doc)
        # THE CANON'S MEASURE, not only its unit: steps each time x the
        # times a day. A reader told "steps per day" alone counts one of the
        # two factors.
        para = doc[doc.index("**THE FRICTION TAX"):]
        para = para[:para.index("\n\n")]
        for surface in (entry, para, tasks.USAGE):
            with self.subTest(surface=surface[:30]):
                self.assertIn("times a day", " ".join(surface.split()))


if __name__ == "__main__":
    unittest.main()
