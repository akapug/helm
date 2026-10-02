#!/usr/bin/env python3
"""Stories: a task's sub-tasks, its "N of M done", and the open-story count.

A row that `continues` another row is its sub-task, and the row at the head of
the chain is its story. A row that continues nothing is a story of one. These
arms carry the contract of the CLI substrate that every later surface reads:

  show     A row lists its children (id, status, title), then "N of M done"
           over every task below it. A child names its parent and its story
           root.
  list     The first line counts open stories next to open rows. A parent line
           shows "[N of M done]". An open child of a closed parent lists under
           a "(closed) task/N title" line, never at the top level.
  close    A row with open sub-tasks refuses, naming them, unless
           --open-children-stay is given. The close records the ids it left
           open.
  --json   show and list carry parent, story_root, story_broken, children,
           done and total on every row.

One class per story state, and one arm per surface that applies:
  (1) a root with no children     (2) a root, all children open
  (3) a root, some children done  (4) a root, all children done
  (5) an open child, open parent  (6) an open child of a parent closed with
                                      --open-children-stay
  (7) a grandchild                (8) a broken chain: a dangling parent or a
                                      ring, listed flat and never dropped
  (9) a parent outside the shown set (another owner or another project)

Titles are placeholders. Every arm runs against a temp HELM_HOME.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import tasks  # noqa: E402
from tests.test_tasks import CliBase, ProjectAxisBase  # noqa: E402

# The column where a listed row's id starts at depth 0: glyph, origin mark,
# a space, a two-character rank and a space. Each depth adds two spaces.
ID_COLUMN = 6


class _TtyBuffer(io.StringIO):
    """A captured stdout that says it is a terminal."""

    def isatty(self):
        return True


class StoryBase(CliBase):
    """Fixture verbs. Rows are filed through the API with fixed numbers so
    each arm can name its ids as literals."""

    def put(self, num, title, parent=None, status="open", owner="seat-a",
            project=None):
        row, err = tasks.add(
            title, owner, tid=num, continues=parent, status=status,
            closed_reason=("placeholder finished" if status == "closed"
                           else None),
            force_new=True, project=project)
        self.assertIsNone(err, err)
        return row["id"]

    def raw(self, num, title, parent):
        """Append a row the write doors refuse today (a dangling parent, one
        half of a ring): the shape older helms left in the ledger."""
        row = {"id": "task/%d" % num, "ts": 1_700_000_000.0,
               "last_updated": 1_700_000_000.0, "title": title,
               "status": "open", "owner": "seat-a", "note": None, "refs": [],
               "source": "seat-a", "origin": None, "closed_reason": None,
               "comments": [], "continues": parent}
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def ok(self, *args):
        rc, out, err = self.cli(*args)
        self.assertEqual(0, rc, "%s -> rc %s\n%s%s" % (args, rc, out, err))
        return out

    def show_json(self, tid):
        return json.loads(self.ok("show", tid, "--json"))

    def list_json(self, *args):
        return {r["id"]: r for r in json.loads(self.ok("list", "--json",
                                                       *args))}

    def row_line(self, out, tid):
        """The one listed ROW line for `tid` (never a "(closed)" line)."""
        got = [l for l in out.splitlines()
               if tid in l.split() and ("(closed) " + tid) not in l]
        self.assertEqual(1, len(got), "%s rows for %s in:\n%s"
                         % (len(got), tid, out))
        return got[0]

    def depth(self, out, tid):
        column = self.row_line(out, tid).index(tid)
        self.assertEqual(0, (column - ID_COLUMN) % 2, out)
        return (column - ID_COLUMN) // 2

    def labelled(self, out, label):
        """The lines of `show` output under one field label, in order."""
        head = "    %-14s " % label
        return [l for l in out.splitlines() if l.startswith(head)]

    def stored(self, tid):
        return tasks.get(tid, path=tasks.ledger_path())


# (1) ------------------------------------------------------------------------

class RootAloneTest(StoryBase):
    """A row that continues nothing and has nothing below it: a story of
    one."""

    def setUp(self):
        super().setUp()
        self.put(101, "placeholder lone root")

    def test_show_text_prints_no_story_lines(self):
        out = self.ok("show", "task/101")
        self.assertIn("placeholder lone root", out)
        for label in ("parent", "story root", "child", "sub-tasks", "story"):
            with self.subTest(label):
                self.assertEqual([], self.labelled(out, label), out)

    def test_show_json_carries_the_empty_story(self):
        got = self.show_json("task/101")
        self.assertEqual("task/101", got["story_root"])
        self.assertEqual([], got["children"])
        self.assertEqual((0, 0), (got["done"], got["total"]))
        self.assertIsNone(got["parent"])
        self.assertIsNone(got["story_broken"])

    def test_list_text_counts_a_story_of_one(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 1 open row", out.splitlines()[0])
        self.assertEqual(0, self.depth(out, "task/101"))
        self.assertNotIn(" done]", self.row_line(out, "task/101"))

    def test_list_json_carries_the_story_keys(self):
        got = self.list_json()["task/101"]
        self.assertEqual("task/101", got["story_root"])
        self.assertEqual((0, 0), (got["done"], got["total"]))

    def test_close_accepts_and_records_nothing_left_open(self):
        out = self.ok("close", "task/101", "placeholder", "finished")
        self.assertIn("task/101 closed", out)
        row = self.stored("task/101")
        self.assertEqual("closed", row["status"])
        self.assertNotIn("open_children_at_close", row)


# (2) ------------------------------------------------------------------------

class AllChildrenOpenTest(StoryBase):

    def setUp(self):
        super().setUp()
        self.put(101, "placeholder root two")
        self.put(102, "placeholder kid a", parent="task/101")
        self.put(103, "placeholder kid b", parent="task/101")

    def test_show_text_lists_children_then_the_count(self):
        out = self.ok("show", "task/101")
        kids = self.labelled(out, "child")
        self.assertEqual(["task/102", "task/103"],
                         [l.split()[1] for l in kids])
        self.assertEqual(["open", "open"], [l.split()[2] for l in kids])
        self.assertIn("placeholder kid a", kids[0])
        count = self.labelled(out, "sub-tasks")
        self.assertEqual(["    sub-tasks      0 of 2 done"], count)
        lines = out.splitlines()
        self.assertLess(lines.index(kids[-1]), lines.index(count[0]))

    def test_show_json_lists_children_and_counts(self):
        got = self.show_json("task/101")
        self.assertEqual(
            [{"id": "task/102", "status": "open",
              "title": "placeholder kid a"},
             {"id": "task/103", "status": "open",
              "title": "placeholder kid b"}], got["children"])
        self.assertEqual((0, 2), (got["done"], got["total"]))

    def test_list_text_counts_one_story_and_badges_the_parent(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 3 open rows", out.splitlines()[0])
        self.assertIn("[0 of 2 done]", self.row_line(out, "task/101"))
        self.assertEqual((0, 1, 1), tuple(self.depth(out, t) for t in
                                          ("task/101", "task/102",
                                           "task/103")))

    def test_list_json_counts_on_the_parent(self):
        got = self.list_json()
        self.assertEqual((0, 2), (got["task/101"]["done"],
                                  got["task/101"]["total"]))
        self.assertEqual("task/101", got["task/102"]["story_root"])

    def test_close_refuses_naming_every_open_child(self):
        before = self.ledger_lines()
        self.assertEqual(3, before)
        rc, out, err = self.cli("close", "task/101", "placeholder", "reason")
        self.assertEqual(2, rc, out + err)
        for needle in ("task/102", "task/103", "--open-children-stay",
                       "nothing was closed"):
            with self.subTest(needle):
                self.assertIn(needle, err)
        self.assertEqual(before, self.ledger_lines())
        self.assertEqual("open", self.stored("task/101")["status"])


# (3) ------------------------------------------------------------------------

class SomeChildrenDoneTest(StoryBase):

    def setUp(self):
        super().setUp()
        self.put(101, "placeholder root three")
        self.put(102, "placeholder kid done", parent="task/101",
                 status="closed")
        self.put(103, "placeholder kid open c", parent="task/101")
        self.put(104, "placeholder kid open d", parent="task/101")

    def test_show_text_counts_the_closed_child_as_done(self):
        out = self.ok("show", "task/101")
        kids = {l.split()[1]: l.split()[2] for l in
                self.labelled(out, "child")}
        self.assertEqual({"task/102": "closed", "task/103": "open",
                          "task/104": "open"}, kids)
        self.assertEqual(["    sub-tasks      1 of 3 done"],
                         self.labelled(out, "sub-tasks"))

    def test_show_json_counts_one_of_three(self):
        got = self.show_json("task/101")
        self.assertEqual((1, 3), (got["done"], got["total"]))
        self.assertEqual(["closed", "open", "open"],
                         [c["status"] for c in got["children"]])

    def test_list_text_badges_one_of_three(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 3 open rows", out.splitlines()[0])
        self.assertIn("[1 of 3 done]", self.row_line(out, "task/101"))
        self.assertNotIn("task/102", out.split())

    def test_list_json_counts_one_of_three(self):
        got = self.list_json()["task/101"]
        self.assertEqual((1, 3), (got["done"], got["total"]))

    def test_close_refusal_names_only_the_open_children(self):
        rc, out, err = self.cli("close", "task/101", "placeholder", "reason")
        self.assertEqual(2, rc, out + err)
        self.assertIn("task/103", err)
        self.assertIn("task/104", err)
        self.assertNotIn("task/102", err)


# (4) ------------------------------------------------------------------------

class AllChildrenDoneTest(StoryBase):

    def setUp(self):
        super().setUp()
        self.put(101, "placeholder root four")
        self.put(102, "placeholder kid finished e", parent="task/101",
                 status="closed")
        self.put(103, "placeholder kid finished f", parent="task/101",
                 status="closed")

    def test_show_text_counts_all_done(self):
        out = self.ok("show", "task/101")
        self.assertEqual(["closed", "closed"],
                         [l.split()[2] for l in self.labelled(out, "child")])
        self.assertEqual(["    sub-tasks      2 of 2 done"],
                         self.labelled(out, "sub-tasks"))

    def test_show_json_counts_all_done(self):
        got = self.show_json("task/101")
        self.assertEqual((2, 2), (got["done"], got["total"]))

    def test_list_text_badges_the_parent_whose_children_are_not_listed(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 1 open row", out.splitlines()[0])
        self.assertIn("[2 of 2 done]", self.row_line(out, "task/101"))

    def test_list_json_counts_all_done(self):
        got = self.list_json()["task/101"]
        self.assertEqual((2, 2), (got["done"], got["total"]))

    def test_close_needs_no_flag_when_nothing_below_is_open(self):
        out = self.ok("close", "task/101", "placeholder", "all", "done")
        self.assertIn("task/101 closed", out)
        row = self.stored("task/101")
        self.assertEqual("closed", row["status"])
        self.assertNotIn("open_children_at_close", row)


# (5) ------------------------------------------------------------------------

class OpenChildOfOpenParentTest(StoryBase):

    def setUp(self):
        super().setUp()
        self.put(101, "placeholder open parent")
        self.put(102, "placeholder open child", parent="task/101")

    def test_show_text_child_names_its_parent_and_story_root(self):
        out = self.ok("show", "task/102")
        parent = self.labelled(out, "parent")
        root = self.labelled(out, "story root")
        self.assertEqual(1, len(parent), out)
        self.assertEqual(["task/101", "open"], parent[0].split()[1:3])
        self.assertIn("placeholder open parent", parent[0])
        self.assertEqual(1, len(root), out)
        self.assertEqual("task/101", root[0].split()[2])

    def test_show_json_child_names_its_parent_and_story_root(self):
        got = self.show_json("task/102")
        self.assertEqual("task/101", got["parent"])
        self.assertEqual("task/101", got["story_root"])
        self.assertIsNone(got["story_broken"])

    def test_list_text_indents_the_child_under_its_parent(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 2 open rows", out.splitlines()[0])
        self.assertEqual((0, 1), (self.depth(out, "task/101"),
                                  self.depth(out, "task/102")))
        lines = out.splitlines()
        self.assertEqual(lines.index(self.row_line(out, "task/101")) + 1,
                         lines.index(self.row_line(out, "task/102")))

    def test_list_json_child_carries_its_story_root(self):
        got = self.list_json()["task/102"]
        self.assertEqual("task/101", got["story_root"])
        self.assertEqual("task/101", got["parent"])

    def test_close_of_the_child_leaves_the_parent_counting_it_done(self):
        self.ok("close", "task/102", "placeholder", "child", "finished")
        got = self.show_json("task/101")
        self.assertEqual("open", got["status"])
        self.assertEqual((1, 1), (got["done"], got["total"]))


# (6) ------------------------------------------------------------------------

class OpenChildOfClosedParentTest(StoryBase):
    """The parent closed with --open-children-stay: its open child stays
    listed under a "(closed)" line, never at the top level."""

    REASON = ("placeholder reason the child outlives the parent")

    def setUp(self):
        super().setUp()
        self.put(201, "placeholder closing root")
        self.put(202, "placeholder surviving kid", parent="task/201")
        self.put(203, "placeholder finished kid", parent="task/201",
                 status="closed")

    def close_keeping_children(self):
        return self.ok("close", "task/201", "--open-children-stay",
                       *self.REASON.split())

    def test_close_accepts_with_the_flag_and_records_the_open_children(self):
        out = self.close_keeping_children()
        self.assertIn("task/201 closed", out)
        self.assertIn("task/202", out)
        row = self.stored("task/201")
        self.assertEqual("closed", row["status"])
        self.assertEqual(self.REASON, row["closed_reason"])
        self.assertEqual(["task/202"], row["open_children_at_close"])

    def test_list_text_lists_the_child_under_a_closed_line(self):
        self.close_keeping_children()
        out = self.ok("list")
        lines = out.splitlines()
        self.assertEqual("1 open story, 1 open row", lines[0])
        header = ("(closed) task/201  [1 of 2 done] "
                  "placeholder closing root")
        self.assertIn(header, lines)
        self.assertEqual(1, self.depth(out, "task/202"))
        self.assertEqual(lines.index(header) + 1,
                         lines.index(self.row_line(out, "task/202")))
        footer = [l for l in lines if " shown — " in l]
        self.assertEqual(["1 shown — closed 2, open 1"], footer)

    def list_on_a_terminal(self, no_color=None):
        """`list` to a stdout that says it is a terminal, with NO_COLOR
        pinned: unset when `no_color` is None, else set to it. The dim rule
        reads both, so neither may come from the runner's environment."""
        self.close_keeping_children()
        out, err = _TtyBuffer(), io.StringIO()
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            os.environ.pop("NO_COLOR", None)
            if no_color is not None:
                os.environ["NO_COLOR"] = no_color
            rc = tasks.cmd_task(["list"])
        self.assertEqual(0, rc, err.getvalue())
        return out.getvalue()

    def test_list_text_dims_the_closed_line_on_a_terminal(self):
        text = self.list_on_a_terminal()
        self.assertIn("\033[2m(closed) task/201", text)
        self.assertNotIn("\033[2m", self.row_line(text, "task/202"))

    def test_NO_COLOR_keeps_the_closed_line_plain_on_a_terminal(self):
        text = self.list_on_a_terminal(no_color="1")
        self.assertIn("(closed) task/201  [1 of 2 done] "
                      "placeholder closing root", text.splitlines())
        self.assertNotIn("\033[", text)

    def test_show_text_parent_names_what_it_left_open(self):
        self.close_keeping_children()
        out = self.ok("show", "task/201")
        self.assertEqual(["task/202"],
                         self.labelled(out, "left open")[0].split()[2:3])
        kids = {l.split()[1]: l.split()[2] for l in
                self.labelled(out, "child")}
        self.assertEqual({"task/202": "open", "task/203": "closed"}, kids)
        self.assertEqual(["    sub-tasks      1 of 2 done"],
                         self.labelled(out, "sub-tasks"))
        child = self.ok("show", "task/202")
        self.assertEqual(["task/201", "closed"],
                         self.labelled(child, "parent")[0].split()[1:3])

    def test_show_json_carries_the_record_and_the_story(self):
        self.close_keeping_children()
        parent = self.show_json("task/201")
        self.assertEqual(["task/202"], parent["open_children_at_close"])
        self.assertEqual((1, 2), (parent["done"], parent["total"]))
        child = self.show_json("task/202")
        self.assertEqual("task/201", child["parent"])
        self.assertEqual("task/201", child["story_root"])

    def test_list_json_child_keeps_its_closed_story_root(self):
        self.close_keeping_children()
        got = self.list_json()
        self.assertEqual(["task/202"], sorted(got))
        self.assertEqual("task/201", got["task/202"]["story_root"])
        every = self.list_json("--all")
        self.assertEqual((1, 2), (every["task/201"]["done"],
                                  every["task/201"]["total"]))


# (7) ------------------------------------------------------------------------

class GrandchildTest(StoryBase):

    def setUp(self):
        super().setUp()
        self.put(101, "placeholder top story")
        self.put(102, "placeholder middle task", parent="task/101")
        self.put(103, "placeholder grandchild task", parent="task/102")

    def test_show_text_lists_direct_children_and_counts_every_depth(self):
        out = self.ok("show", "task/101")
        self.assertEqual(["task/102"],
                         [l.split()[1] for l in self.labelled(out, "child")])
        self.assertEqual(["    sub-tasks      0 of 2 done"],
                         self.labelled(out, "sub-tasks"))
        leaf = self.ok("show", "task/103")
        self.assertEqual("task/102",
                         self.labelled(leaf, "parent")[0].split()[1])
        self.assertEqual("task/101",
                         self.labelled(leaf, "story root")[0].split()[2])

    def test_show_json_grandchild_roots_at_the_top(self):
        top = self.show_json("task/101")
        self.assertEqual(["task/102"], [c["id"] for c in top["children"]])
        self.assertEqual(2, top["total"])
        leaf = self.show_json("task/103")
        self.assertEqual("task/102", leaf["parent"])
        self.assertEqual("task/101", leaf["story_root"])

    def test_list_text_indents_two_levels(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 3 open rows", out.splitlines()[0])
        self.assertEqual((0, 1, 2), tuple(self.depth(out, t) for t in
                                          ("task/101", "task/102",
                                           "task/103")))
        self.assertIn("[0 of 2 done]", self.row_line(out, "task/101"))
        self.assertIn("[0 of 1 done]", self.row_line(out, "task/102"))

    def test_list_json_grandchild_roots_at_the_top(self):
        got = self.list_json()
        self.assertEqual("task/101", got["task/103"]["story_root"])
        self.assertEqual(2, got["task/101"]["total"])

    def test_close_of_the_top_names_the_grandchild(self):
        rc, out, err = self.cli("close", "task/101", "placeholder", "reason")
        self.assertEqual(2, rc, out + err)
        self.assertIn("task/102", err)
        self.assertIn("task/103", err)

    def test_a_closed_middle_lists_as_a_closed_line_inside_the_story(self):
        self.ok("close", "task/102", "--open-children-stay", "placeholder",
                "middle", "reason")
        out = self.ok("list")
        lines = out.splitlines()
        self.assertEqual("1 open story, 2 open rows", lines[0])
        header = [l for l in lines if "(closed) task/102" in l]
        self.assertEqual(1, len(header), out)
        self.assertTrue(header[0].startswith("  (closed) task/102"),
                        header[0])
        self.assertEqual(2, self.depth(out, "task/103"))
        rc, out, err = self.cli("close", "task/101", "placeholder", "reason")
        self.assertEqual(2, rc, out + err)
        self.assertIn("task/103", err)


# (8) ------------------------------------------------------------------------

class BrokenChainTest(StoryBase):
    """A dangling parent and a ring, written the way older helms wrote them.
    Each renders flat, is never dropped, and nothing crashes."""

    def setUp(self):
        super().setUp()
        self.put(304, "placeholder healthy row")
        self.raw(301, "placeholder dangling row", "task/999")
        self.raw(302, "placeholder ring left", "task/303")
        self.raw(303, "placeholder ring right", "task/302")

    def test_show_text_says_the_chain_is_broken(self):
        out = self.ok("show", "task/301")
        parent = self.labelled(out, "parent")
        self.assertEqual(1, len(parent), out)
        self.assertIn("task/999", parent[0])
        self.assertIn("NOT IN THE LEDGER", parent[0])
        ring = self.ok("show", "task/302")
        self.assertIn("placeholder ring left", ring)
        story = self.labelled(ring, "story")
        self.assertEqual(1, len(story), ring)
        self.assertIn("RING", story[0])
        self.assertEqual([], self.labelled(ring, "sub-tasks"))

    def test_show_json_names_the_break_and_roots_the_row_at_itself(self):
        got = self.show_json("task/301")
        self.assertEqual("task/999", got["parent"])
        self.assertEqual("task/301", got["story_root"])
        self.assertEqual({"kind": "dangling", "at": "task/999"},
                         got["story_broken"])
        ring = self.show_json("task/302")
        self.assertEqual("task/302", ring["story_root"])
        self.assertEqual({"kind": "ring", "at": "task/302"},
                         ring["story_broken"])
        self.assertEqual((0, 0), (ring["done"], ring["total"]))

    def test_list_text_lists_every_row_flat(self):
        out = self.ok("list")
        self.assertEqual("4 open stories, 4 open rows", out.splitlines()[0])
        for tid in ("task/301", "task/302", "task/303", "task/304"):
            with self.subTest(tid):
                self.assertEqual(0, self.depth(out, tid))
                self.assertNotIn(" done]", self.row_line(out, tid))
        self.assertNotIn("(closed)", out)

    def test_list_json_lists_every_row(self):
        got = self.list_json()
        self.assertEqual(["task/301", "task/302", "task/303", "task/304"],
                         sorted(got))
        self.assertEqual("task/303", got["task/303"]["story_root"])

    def test_close_of_a_ring_member_neither_crashes_nor_drops_its_partner(
            self):
        out = self.ok("close", "task/302", "placeholder", "ring", "cut")
        self.assertIn("task/302 closed", out)
        listed = self.ok("list")
        self.assertIn("placeholder ring right", listed)
        self.assertEqual(0, self.depth(listed, "task/303"))
        self.assertNotIn("(closed)", listed)


# (9) ------------------------------------------------------------------------

class ParentFilteredOutTest(StoryBase):
    """The parent is another seat's, so `--owner` leaves it out of the
    shown set: the child renders at the top level, while the story fields
    stay ledger-wide."""

    def setUp(self):
        super().setUp()
        self.put(401, "placeholder other seats parent", owner="seat-b")
        self.put(402, "placeholder my child", parent="task/401")

    def test_list_text_renders_the_child_at_the_top(self):
        out = self.ok("list", "--owner", "seat-a")
        self.assertEqual("1 open story, 1 open row", out.splitlines()[0])
        self.assertEqual(0, self.depth(out, "task/402"))
        self.assertNotIn("task/401", out.split())
        self.assertNotIn("(closed)", out)
        whole = self.ok("list")
        self.assertEqual(1, self.depth(whole, "task/402"))

    def test_list_json_keeps_the_ledger_wide_story(self):
        got = self.list_json("--owner", "seat-a")
        self.assertEqual(["task/402"], sorted(got))
        self.assertEqual("task/401", got["task/402"]["story_root"])
        self.assertEqual("task/401", got["task/402"]["parent"])

    def test_show_text_names_the_parent_across_the_filter(self):
        out = self.ok("show", "task/402")
        self.assertEqual(["task/401", "open"],
                         self.labelled(out, "parent")[0].split()[1:3])

    def test_show_json_counts_the_child_on_the_parent(self):
        got = self.show_json("task/401")
        self.assertEqual(["task/402"], [c["id"] for c in got["children"]])
        self.assertEqual((0, 1), (got["done"], got["total"]))


class ParentInAnotherProjectTest(ProjectAxisBase, StoryBase):
    """The parent lives in another project, so the scoped listing leaves it
    out: the child renders at the top level, and close still sees it."""

    def setUp(self):
        super().setUp()
        self.put(411, "placeholder foreign parent", project=self.OTHER)
        self.put(412, "placeholder local child", parent="task/411",
                 project=self.PROJECT)
        os.chdir(self.proj_dir)

    def test_list_text_renders_the_child_at_the_top(self):
        out = self.ok("list")
        self.assertEqual("1 open story, 1 open row", out.splitlines()[0])
        self.assertEqual(0, self.depth(out, "task/412"))
        every = self.ok("list", "--all-projects")
        self.assertEqual(1, self.depth(every, "task/412"))

    def test_list_json_keeps_the_ledger_wide_story(self):
        got = self.list_json()
        self.assertEqual(["task/412"], sorted(got))
        self.assertEqual("task/411", got["task/412"]["story_root"])

    def test_close_counts_a_child_in_another_project(self):
        rc, out, err = self.cli("close", "task/411", "placeholder", "reason")
        self.assertEqual(2, rc, out + err)
        self.assertIn("task/412", err)


if __name__ == "__main__":
    unittest.main()
