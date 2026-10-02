#!/usr/bin/env python3
"""The web console's task BACKLOG, GitHub-Issues grade (task/3445 L1b).

The owner asked for "all tasks separated by project" and "pagination and
other best practices that at least github would have". A fresh walk of the
live console measured the opposite: every project but helm showed "0 open"
on its Tasks tab while its own row said 59, the backlog withheld 261 rows
from other projects, and one page drew 1,409 cards at once (316,000 px).

The SERVER view (`/api/backlog`) is pinned here: every project reachable,
the project-less rows as the `none` bucket, a facet count for every filter,
and one definition of "open". The client that drew it, and the page that
held it, were retired for the Work page's List lens (task/3643).
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_web_tasks import TasksBase, _row, _stub_tasks  # noqa: E402

DAY = 86400.0


def _axis(stub):
    """The store's project and holder readers, mirrored exactly."""
    stub.project_of_row = lambda row: (
        str(row.get("project") or "").strip() or None)
    stub.owner_of = lambda row: (
        "" if str(row.get("owner") or "").strip().casefold()
        in ("unowned", "-") else str(row.get("owner") or "").strip())
    return stub


class BacklogViewBase(TasksBase):
    def get(self, path, **qs):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if qs:
            url += "?" + urlencode(qs)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def ids(self, **qs):
        status, d = self.get("/api/backlog", **qs)
        self.assertEqual(status, 200, d)
        return [e["id"] for e in d["entries"]]


LEDGER = {
    "task/1": _row("task/1", "helm route work", "open", project="helm",
                   priority="P0", owner="seat-alpha"),
    "task/2": _row("task/2", "helm review", "in_progress", project="helm",
                   priority="P1", owner="seat-beta"),
    "task/3": _row("task/3", "projb importer", "open", project="projb",
                   priority="P1"),
    "task/4": _row("task/4", "projb unranked", "open", project="projb"),
    "task/5": _row("task/5", "legacy row", "open"),
    "task/6": _row("task/6", "helm done", "closed", project="helm",
                   priority="P0", closed_reason="landed"),
    "task/7": _row("task/7", "he asked", "open", project="projc",
                   priority="P2", origin="owner", owner="UNOWNED"),
}


class EveryProjectIsReachableTest(BacklogViewBase):
    """Finding 1 of the walk: other projects' tasks could not be reached."""

    def test_each_project_and_the_none_bucket_is_a_filter(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        self.assertEqual(self.ids(status="open,in_progress", project="projb"),
                         ["task/3", "task/4"])
        self.assertEqual(self.ids(status="open,in_progress", project="none"),
                         ["task/5"])
        # no project named: every project, the bucket included
        self.assertEqual(sorted(self.ids(status="open,in_progress")),
                         ["task/1", "task/2", "task/3", "task/4", "task/5",
                          "task/7"])

    def test_the_project_facet_counts_every_project_and_the_bucket(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        _s, d = self.get("/api/backlog", status="open,in_progress")
        self.assertEqual(d["facets"]["project"],
                         {"helm": 2, "projb": 2, "none": 1, "projc": 1})

    def test_a_projects_count_here_is_its_rows_count_on_the_board(self):
        """ONE DEFINITION OF "OPEN". The project row counts a project's live
        rows (open or in progress) through `web_board._tasks_join`; the Tasks
        tab and the backlog ask this route for status open,in_progress and
        that project. The same ledger must give the same number."""
        from helm import web_board
        stub = _axis(_stub_tasks(dict(LEDGER)))
        stub.OPEN_STATUSES = ("open", "in_progress")
        plain = stub.snapshot

        def snapshot(accept=None):
            rows, why = plain()
            for row in rows.values():
                if accept:
                    accept(row, None)
            return rows, why
        stub.snapshot = snapshot
        self.install(stub)
        _sec, by_project, _flow = web_board._tasks_join(None)
        self.assertEqual(sorted(by_project), ["helm", "projb", "projc"])
        self.assertEqual(by_project["helm"]["open"], 2)   # the control
        for key, rec in by_project.items():
            _s, d = self.get("/api/backlog", status="open,in_progress",
                             project=key)
            self.assertEqual(d["total_count"], rec["open"], key)
            self.assertEqual(d["scope_count"], rec["open"], key)

    def test_the_task_route_withholds_no_project_any_more(self):
        """The cwd scoping that withheld other projects is retired: nothing
        reads the withholding now, and it hid their rows from their own
        Tasks tabs."""
        stub = _axis(_stub_tasks(dict(LEDGER)))
        stub.current_project = lambda: "helm"
        self.install(stub)
        _s, d = self.get("/api/tasks")
        self.assertEqual(d["withheld_foreign"], 0)
        self.assertIsNone(d["project"])
        self.assertEqual(sorted(e["id"] for e in d["entries"]),
                         sorted(LEDGER))
        self.assertEqual({e["id"]: e["project"] for e in d["entries"]}["task/3"],
                         "projb")


class FiltersTest(BacklogViewBase):
    def test_a_rank_filter_never_lets_an_unranked_row_through(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        self.assertEqual(self.ids(status="all", priority="P0"),
                         ["task/1", "task/6"])
        self.assertEqual(self.ids(status="open,in_progress",
                                  priority="unranked"), ["task/4", "task/5"])
        self.assertEqual(self.ids(status="open,in_progress",
                                  priority="P0,P1"),
                         ["task/1", "task/2", "task/3"])

    def test_owner_is_a_seat_or_unowned_never_a_substring(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        self.assertEqual(self.ids(status="all", owner="Seat-Alpha"),
                         ["task/1"])                  # the positive control
        self.assertEqual(self.ids(status="all", owner="seat"), [],
                         "a seat name matched as a substring")
        # an UNOWNED placeholder is no holder (tasks.owner_of)
        self.assertEqual(self.ids(status="open,in_progress", owner="unowned"),
                         ["task/3", "task/7", "task/4", "task/5"])

    def test_asked_and_stale_select_by_the_stores_own_readings(self):
        now = time.time()
        rows = dict(LEDGER)
        rows["task/8"] = _row("task/8", "old and silent", "open",
                              project="helm", ts=now - 30 * DAY)
        rows["task/9"] = _row("task/9", "old but noted", "open",
                              project="helm", ts=now - 30 * DAY,
                              comments=[{"ts": now - 3600, "by": "a",
                                         "text": "still on it"}])
        rows["task/10"] = _row("task/10", "migrated", "open",
                               origin="corpus-2026-08-05")
        self.install(_axis(_stub_tasks(rows)))
        self.assertEqual(self.ids(status="open,in_progress", asked="1"),
                         ["task/7"])
        self.assertEqual(self.ids(status="open,in_progress", stale="1"),
                         ["task/8"])

    def test_a_present_flag_that_is_not_on_selects_nothing(self):  # noqa: VACUOUS_ASSERTION — asked=1, stale=1 and a blank status are the positive controls; the empty lists are the typos
        """Absence is no filter. An on-token narrows. Any other present
        value selects nothing, so a typo cannot read as every row."""
        now = time.time()
        rows = dict(LEDGER)
        rows["task/8"] = _row("task/8", "old and silent", "open",
                              project="helm", ts=now - 30 * DAY)
        self.install(_axis(_stub_tasks(rows)))
        self.assertEqual(self.ids(status="open,in_progress", asked="1"),
                         ["task/7"])
        self.assertEqual(self.ids(status="open,in_progress", asked="yes"),
                         ["task/7"])
        self.assertEqual(self.ids(status="open,in_progress", stale="1"),
                         ["task/8"])
        self.assertEqual(self.ids(status="open,in_progress", asked="y"), [])
        self.assertEqual(self.ids(status="open,in_progress", asked="0"), [])
        self.assertEqual(self.ids(status="open,in_progress", stale="2"), [])
        self.assertIn("task/6", self.ids(status=""))

    def test_one_unknown_status_selects_nothing(self):  # noqa: VACUOUS_ASSERTION — status=closed and a blank status are the positive controls; the empty lists are the typos
        """One member outside the known set selects nothing, the known
        members of that same list included. A blank status is no filter."""
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        self.assertEqual(self.ids(status="closed"), ["task/6"])
        self.assertIn("task/6", self.ids(status=""))
        self.assertEqual(self.ids(status="open,closedd"), [])
        self.assertEqual(self.ids(status="nope"), [])

    def test_search_reads_the_id_title_and_note_and_needs_every_word(self):
        rows = dict(LEDGER)
        rows["task/11"] = _row("task/11", "a title", "open",
                               note="the route table moved")
        self.install(_axis(_stub_tasks(rows)))
        self.assertEqual(self.ids(status="all", q="route"),
                         ["task/1", "task/11"])
        self.assertEqual(self.ids(status="all", q="helm route"), ["task/1"])
        self.assertEqual(self.ids(status="all", q="task/3"), ["task/3"])


class FacetsTest(BacklogViewBase):
    def test_each_facet_counts_its_choices_with_every_other_filter_kept(self):
        """A menu shows what each choice WOULD select: picking P0 must not
        make P1 read zero, and the other filters still narrow the counts."""
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        _s, d = self.get("/api/backlog", status="open,in_progress",
                         priority="P0", project="helm")
        f = d["facets"]
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(f["priority"], {"P0": 1, "P1": 1})     # helm only
        self.assertEqual(f["project"], {"helm": 1})             # P0 only
        self.assertEqual(f["status"], {"open": 1, "closed": 1})  # P0 in helm
        self.assertEqual(f["owner"], {"seat-alpha": 1})
        self.assertEqual((f["asked"], f["stale"]), (0, 0))

    def test_the_headline_and_the_scope_are_counted_over_their_own_rows(self):
        """`queue` is the headline over every filter but status and rank;
        `scope_count` is the "of N": the rows status and project keep."""
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        _s, d = self.get("/api/backlog", status="open,in_progress",
                         project="helm", priority="P1")
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(d["scope_count"], 2)
        self.assertEqual((d["queue"]["P0"], d["queue"]["P1"],
                          d["queue"]["live"]), (1, 1, 2))


class GroupedByProjectTest(BacklogViewBase):
    """The owner's default view: "all tasks separated by project" — a group
    per project with its count and its first rows, the rows with no project
    a visible group of their own, never merged into another."""

    def test_group_answers_every_project_largest_first_with_its_first_rows(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        _s, d = self.get("/api/backlog", status="open,in_progress",
                         group="project", per_group="1")
        self.assertEqual([(g["key"], g["count"]) for g in d["groups"]],
                         [("helm", 2), ("projb", 2), ("none", 1),
                          ("projc", 1)])
        self.assertEqual([[e["id"] for e in g["entries"]] for g in d["groups"]],
                         [["task/1"], ["task/3"], ["task/5"], ["task/7"]])
        self.assertEqual(d["total_count"], 6)
        self.assertEqual(d["entries"], [])

    def test_the_groups_keep_every_filter(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        _s, d = self.get("/api/backlog", status="open,in_progress",
                         priority="P1", group="project")
        self.assertEqual([(g["key"], g["count"]) for g in d["groups"]],
                         [("helm", 1), ("projb", 1)])


class RowsAndOrderTest(BacklogViewBase):
    def test_a_row_is_the_projection_the_task_route_serves(self):
        rows = {"task/1": _row("task/1", "long", "open", project="helm",
                               note="word " * 200,
                               comments=[{"ts": "t", "by": "seat-c",
                                          "text": "first line\nmore"}]),
                "task/2": _row("task/2", "shut", "closed", project="helm",
                               closed_reason="done")}
        self.install(_axis(_stub_tasks(rows)))
        _s, d = self.get("/api/backlog", status="all")
        by = {e["id"]: e for e in d["entries"]}
        self.assertEqual(by["task/1"]["last_note"],
                         {"ts": "t", "by": "seat-c", "line": "first line"})
        self.assertIs(by["task/1"]["note_more"], True)
        self.assertLessEqual(len(by["task/1"]["note"]), 240)
        self.assertEqual(by["task/1"]["comments"], 1)
        self.assertIs(by["task/2"]["tombstone"], True)
        _s, t = self.get("/api/tasks")
        self.assertEqual({e["id"]: e for e in t["entries"]}["task/1"],
                         by["task/1"])

    def test_every_sort_the_menu_offers(self):
        now = time.time()
        rows = {
            "task/1": _row("task/1", "a", "open", priority="P1",
                           ts=now - 9 * DAY),
            "task/2": _row("task/2", "b", "open", priority="P0",
                           ts=now - 2 * DAY,
                           comments=[{"ts": now - 60, "by": "x", "text": "y"},
                                     {"ts": now - 30, "by": "x", "text": "z"}]),
            "task/3": _row("task/3", "c", "open", ts=now - 5 * DAY,
                           comments=[{"ts": now - 4 * DAY, "by": "x",
                                      "text": "y"}]),
        }
        self.install(_axis(_stub_tasks(rows)))
        self.assertEqual(self.ids(sort="priority"),
                         ["task/2", "task/1", "task/3"])
        # created is newest first (GitHub's order); oldest is the reverse
        self.assertEqual(self.ids(sort="created"),
                         ["task/2", "task/3", "task/1"])
        self.assertEqual(self.ids(sort="oldest"),
                         ["task/1", "task/3", "task/2"])
        self.assertEqual(self.ids(sort="noted"),
                         ["task/1", "task/3", "task/2"])
        self.assertEqual(self.ids(sort="updated"),
                         ["task/2", "task/3", "task/1"])
        self.assertEqual(self.ids(sort="comments"),
                         ["task/2", "task/3", "task/1"])

    def test_by_project_is_a_sort_and_the_none_bucket_sorts_last(self):
        self.install(_axis(_stub_tasks(dict(LEDGER))))
        self.assertEqual(self.ids(status="open,in_progress", sort="project"),
                         ["task/1", "task/2", "task/3", "task/4", "task/7",
                          "task/5"])

    def test_a_page_is_the_slice_and_the_totals_describe_the_whole(self):
        rows = {"task/%d" % i: _row("task/%d" % i, "r", "open",
                                    project="helm")
                for i in range(1, 121)}
        self.install(_axis(_stub_tasks(rows)))
        _s, d = self.get("/api/backlog", status="open,in_progress",
                         per_page="50", page="3")
        self.assertEqual((d["total_count"], d["total_pages"], d["page"],
                          d["per_page"]), (120, 3, 3, 50))
        self.assertEqual([e["id"] for e in d["entries"]][:2],
                         ["task/101", "task/102"])
        self.assertEqual(len(d["entries"]), 20)


# ---------------------------------------------------------------------------
# THE CLIENT AND ITS WIRING WENT WITH THE BACKLOG PAGE (task/3643, slice 6).
# Work › backlog is the Work page's List lens filtered to To do, read from
# /api/work, and a project's Tasks tab is that page locked to the project
# (its Work tab). The client arms moved with it to tests/test_web_work_page.py:
# the address that holds the view and reads back the same, the old backlog
# addresses landing on their filter (`priority`, `owner`, `group=none`), the
# rows with their drawer, a project's tab locked to it, and Home's one work
# tile in place of the backlog tile. The server view above, `/api/backlog`,
# stays for the agents' tools and is pinned by the classes above.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
