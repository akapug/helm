#!/usr/bin/env python3
"""THE ONE LAND BOARD (task/3585) — its row set, its counts and its caps.

The owner, reading Work › pipeline: "why the land requests board and the land
pipeline board are two different things? shouldnt they be one ... by
composition?" The page drew the same land requests three times: the band's
in-flight and owed-by rows off `/api/lr`, the fleet kanban off `/api/board`,
and the project's own wall off `/api/lr` again. One board replaces them, drawn
from ONE row set: the live cards `/api/board` sends per project
(`web_board._kanban_card`, the land pipeline's own rows).

THE SERVER HALF asks the board's join what each card carries (lane, task,
holder, tip, gate and the two marks) and what the per-project tally counts —
every live row, the cards the cap cut and the re-hold line included, so no
number is the cap's. The board itself was retired with the pipeline page
(task/3643): the Work page reads these cards through /api/work.
"""
import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-landboard-", var="HELM_HOME")

from helm import web_board  # noqa: E402


def _loop(rid, lane, state="AWAITING_REVIEW", **kw):
    """One in-flight land request, in the fields `landreq_cli.card` sends."""
    row = {"id": rid, "lane": lane, "state": state, "kind": "review",
           "terminal": False, "honored": False, "chain_root": rid,
           "owed_by": "reviewer", "holder_role": "reviewer",
           "holder_seat": "codex", "dwell_s": 3600, "dwell_known": True,
           "contrary": False, "stalled": False, "review_sha": "a" * 12,
           "base_sha": "b" * 40, "gate": "", "trunk_contains_tip": None,
           "frontier": None, "frontier_rung": None}
    row.update(kw)
    return row


class CardWireTest(unittest.TestCase):
    """What one card carries: the fields the board draws, copied off the
    pipeline's row, never looked up a second time."""

    def test_a_card_carries_lane_task_holder_tip_gate_and_its_marks(self):
        card = web_board._kanban_card(_loop(
            "r1", "task-3585-one-board", gate="gate:abc123def456",
            review_sha="0123456789ab", stalled=True), 120)
        self.assertEqual(
            {k: card.get(k) for k in ("lane", "task", "holder", "tip",
                                      "gate", "contrary", "stalled",
                                      "age_s")},
            {"lane": "task-3585-one-board", "task": "task/3585",
             "holder": "codex", "tip": "0123456789ab",
             "gate": "gate:abc123def456", "contrary": False,
             "stalled": True, "age_s": 120})

    def test_the_holder_is_the_seat_else_the_role_word_else_unknown(self):
        role = web_board._kanban_card(_loop(
            "r", "x", holder_seat=None,
            holder_role="unknown (declared verdict held)"))
        bare = web_board._kanban_card(_loop("r", "x", holder_seat=None,
                                            holder_role="integrator"))
        none = web_board._kanban_card(_loop("r", "x", holder_seat=None,
                                            holder_role=None))
        self.assertEqual((role["holder"], bare["holder"], none["holder"]),
                         ("unknown", "integrator", "unknown"))

    def test_a_lane_naming_no_task_or_two_names_none(self):
        """The task is read off the lane the row carries, and only when it
        names exactly one: two named is ambiguous, not the first of them."""
        for lane in ("fix-the-thing", "task-12-and-task/13",
                     # a trailing number is never a task (task/3643)
                     "canary-seeded-red-0926", "work-key-3643"):
            with self.subTest(lane=lane):
                self.assertIsNone(web_board._kanban_card(
                    _loop("r", lane))["task"])
        self.assertEqual(web_board._kanban_card(_loop(
            "r", "lane/task-77-x"))["task"], "task/77")

    def test_the_projections_task_is_never_contradicted_by_the_label(self):  # noqa: VACUOUS_ASSERTION — the stored task is asserted drawn on the same card field
        """FINDING 5: a card the pipeline joined (`task` on the wire, the
        one join over the chain row and the lane record) draws that answer;
        the label's literal is read only from an older server's card."""
        self.assertEqual(web_board._kanban_card(_loop(
            "r", "task-12-x", task="task/7"))["task"], "task/7")
        self.assertIsNone(web_board._kanban_card(_loop(
            "r", "task-12-x", task=None,
            task_why="contradictory: lane records task/7"))["task"])
        self.assertEqual(web_board._kanban_card(_loop(
            "r", "task-12-x"))["task"], "task/12")

    def test_a_build_with_no_reviewed_tip_has_no_tip_not_its_base(self):
        card = web_board._kanban_card(_loop("b", "build-1", kind="build",
                                            state="AWAITING_BUILD",
                                            review_sha=""))
        self.assertIsNone(card["tip"])


class TallyTest(unittest.TestCase):
    """The header strip's numbers, counted over EVERY live row of a project:
    the cards sent, the cards the cap cut and the re-hold line."""

    def feed(self, cards, nonbillable=frozenset()):
        live, _on_main, _folded = web_board._kanban_split(cards, 0)
        return web_board._kanban_feed(live, nonbillable)

    def test_a_nonbillable_hold_is_its_own_mark_never_moving_too(self):
        """CURE2 3585, P3 5: the old band's partition was disjoint, contrary >
        stalled > nonbillable > moving. The strip's "moving" counted the
        nonbillable holds its /api/lr line also printed, so one row counted
        twice."""
        cards = [_loop("n1", "hold-1"), _loop("n2", "hold-2", stalled=True),
                 _loop("m1", "move-1")]
        feed = self.feed(cards, frozenset(("n1", "n2")))
        self.assertEqual(feed["tally"]["marks"], {
            "contrary": 0, "stalled": 1, "nonbillable": 1, "moving": 1})
        self.assertEqual({c["id"]: c["mark"] for c in feed["loops"]},
                         {"n1": "nonbillable", "n2": "stalled", "m1": "moving"})
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": cards,
                "unmeasurable": [{"id": "n1", "kind": "pre-tier"}],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        marks = rec["proj"]["lanes"]["tally"]["marks"]
        self.assertEqual((marks["nonbillable"], marks["moving"]), (1, 1))

    def test_the_tally_counts_every_live_row_past_the_cap(self):
        cards = ([_loop("c%d" % i, "con-%d" % i, contrary=True, stalled=True)
                  for i in range(2)]
                 + [_loop("s%d" % i, "stall-%d" % i, stalled=True,
                          holder_seat="kimi") for i in range(3)]
                 + [_loop("m%d" % i, "move-%d" % i, holder_seat=None,
                          holder_role="integrator") for i in range(5)])
        with mock.patch.object(web_board, "KANBAN_ROWS", 4):
            feed = self.feed(cards)
        self.assertEqual(len(feed["loops"]), 4)
        tally = feed["tally"]
        # CONTRARY OUTRANKS STALLED: one row, one mark, counted once
        self.assertEqual(tally["marks"], {"contrary": 2, "stalled": 3,
                                          "nonbillable": 0, "moving": 5})
        self.assertEqual(tally["live"], 10)
        self.assertEqual(tally["holders"], {"codex": 2, "kimi": 3,
                                            "integrator": 5})

    def test_the_rehold_line_is_counted_in_the_tally(self):
        door = "helm dispatch release x; helm dispatch hold x <reason>"
        held = _loop("h1", "held-1", trunk_contains_tip=True,
                     source_clean_tip="c" * 40, holder_seat="kimi",
                     source_clean_rehold={"kind": "NO HOLDER", "door": door,
                                          "why": door},
                     source_clean_on_main="on main · RE-HOLD owed by @kimi")
        feed = self.feed([held, _loop("a1", "awaiting")])
        self.assertEqual(feed["rehold"]["count"], 1)
        self.assertEqual(feed["tally"]["live"], 2)
        self.assertEqual(feed["tally"]["holders"], {"kimi": 1, "codex": 1})

    def test_each_rehold_row_carries_the_mark_and_holder_it_is_counted_by(self):
        """A filtered board keeps the re-hold line with exactly the rows its
        number counts, so each row says what the tally counted it as."""
        door = "helm dispatch release x; helm dispatch hold x <reason>"
        rows = [_loop("h%d" % i, "held-%d" % i, trunk_contains_tip=True,
                      source_clean_tip="c" * 40, holder_seat=seat,
                      stalled=stalled, contrary=contrary,
                      source_clean_rehold={"kind": "NO HOLDER", "door": door,
                                           "why": door},
                      source_clean_on_main="on main · RE-HOLD owed")
                for i, (seat, stalled, contrary) in enumerate(
                    (("zed", True, False), ("kimi", False, False),
                     ("kimi", True, True)))]
        feed = self.feed(rows)
        self.assertEqual(
            [(r["lane"], r["holder"], r["mark"]) for r in feed["rehold"]["rows"]],
            [("held-0", "zed", "stalled"), ("held-1", "kimi", "moving"),
             ("held-2", "kimi", "contrary")])
        self.assertEqual(feed["tally"]["marks"],
                         {"contrary": 1, "stalled": 1, "nonbillable": 0,
                          "moving": 1})

    def test_the_join_sends_the_tally_and_landed_tips_and_gates(self):
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None,
                "loops": [_loop("a1", "awaiting", stalled=True)],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [
                    {"lane": "done", "task": "task/9", "age_s": 60,
                     "reviewed_tip": "f" * 40, "gate": "gate:0123abcd"}],
                    "total": 1, "unavailable": None}}
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        proj = rec["proj"]
        self.assertEqual(proj["lanes"]["tally"]["marks"],
                         {"contrary": 0, "stalled": 1, "nonbillable": 0,
                          "moving": 0})
        self.assertEqual(proj["landed"], [{"lane": "done", "task": "task/9",
                                           "age_s": 60, "tip": "f" * 12,
                                           "gate": "gate:0123abcd"}])


# ---------------------------------------------------------------------------
# THE BROWSER HALF: 92 live land requests across three projects.

_SEC = {"source": "x", "measured_at": 1, "age_s": 5, "limit_s": 600,
        "stale": False, "unavailable": None}


def _tip(rid):
    """A fixture card's own reviewed tip: distinct per request, as on the
    live board, so only a variant that says so shares one."""
    return hashlib.sha1(rid.encode()).hexdigest()[:12]


def _card(rid, lane, state, holder="codex", contrary=False, stalled=False):
    return {"id": rid, "lane": lane, "state": state, "task": None,
            "holder": holder, "tip": _tip(rid), "gate": "",
            "contrary": contrary, "stalled": stalled,
            "trunk_contains_tip": None, "on_main_unverdicted": False,
            "source_clean_on_main": None, "owes_rehold": False,
            "age_s": 600}


def _tally(cards, cut=()):
    """The server's tally over the cards sent and the rows the cap cut."""
    marks = {"contrary": 0, "stalled": 0, "moving": 0}
    holders = {}
    for c in list(cards) + list(cut):
        mark = "contrary" if c["contrary"] else "stalled" if c["stalled"] \
            else "moving"
        marks[mark] += 1
        holders[c["holder"]] = holders.get(c["holder"], 0) + 1
    return {"live": len(cards) + len(cut), "marks": marks, "holders": holders}


def _project(prefix, n_review, n_ready, n_fix, stalled_every=0,
             contrary=(), holder="codex", cut=()):
    cards = []
    for i in range(n_review):
        cards.append(_card("%s-r%d" % (prefix, i), "%s-rev-%d" % (prefix, i),
                           "AWAITING_REVIEW", holder=holder,
                           stalled=bool(stalled_every)
                           and i % stalled_every == 0,
                           contrary=i in contrary))
    for i in range(n_ready):
        cards.append(_card("%s-y%d" % (prefix, i), "%s-ready-%d" % (prefix, i),
                           "READY", holder="integrator"))
    for i in range(n_fix):
        cards.append(_card("%s-f%d" % (prefix, i), "%s-fix-%d" % (prefix, i),
                           "CHANGES_REQUESTED", holder="author"))
    more = {}
    for c in cut:
        more[c["state"]] = more.get(c["state"], 0) + 1
    return cards, more, _tally(cards, cut)


def _world():
    # ALPHA is the project this helm serves: 40 cards sent, 10 cut by the cap
    a_cut = [_card("a-cut-%d" % i, "a-cut-%d" % i, "AWAITING_REVIEW",
                   stalled=i < 4) for i in range(10)]
    a_cards, a_more, a_tally = _project("a", 30, 6, 4, stalled_every=5,
                                        contrary=(1,), cut=a_cut)
    b_cards, b_more, b_tally = _project("b", 20, 6, 4, holder="kimi")
    g_cards, g_more, g_tally = _project("g", 8, 2, 0, contrary=(0,))
    board = {"sections": {"seats": dict(_SEC), "lands": dict(_SEC, scope="alpha"),
                          "fleet": dict(_SEC, scope="alpha"),
                          "gate": dict(_SEC)},
             "projects": {
                 "alpha": {"running": [], "gate": [],
                           "lanes": {"loops": a_cards, "loops_more": a_more,
                                     "tally": a_tally, "building_lanes": [],
                                     "on_main": None, "collapsed": [],
                                     "rehold": None},
                           "landed": [{"lane": "a-landed", "task": "task/9",
                                       "age_s": 3600, "tip": "c" * 12,
                                       "gate": "gate:feedbeef"}],
                           "landed_more": 0},
                 "beta": {"running": [], "gate": [],
                          "pipeline": {"loops": b_cards, "loops_more": b_more,
                                       "tally": b_tally, "landed": [],
                                       "on_main": None, "collapsed": [],
                                       "rehold": None, "landed_more": 0}},
                 "gamma": {"running": [], "gate": [],
                           "pipeline": {"loops": g_cards, "loops_more": g_more,
                                        "tally": g_tally, "landed": [],
                                        "on_main": None, "collapsed": [],
                                        "rehold": None, "landed_more": 0}}}}
    return board


_ROWS = [{"name": n, "light": {"key": n}} for n in ("alpha", "beta", "gamma")]


# THE BROWSER HALF WENT WITH THE BOARD (task/3643, slice 6). The land board's
# model and renderer (`landBoard`, `landBoardHTML`, the strip, the cells, the
# cards and their detail) were retired with the pipeline page: the Work page
# draws one card per piece of work off /api/work, and its arms are
# tests/test_web_work_page.py. The world above stays: the one pipeline
# reading is read over it on the server (tests/test_work_model.py,
# OneReadingOverTheWorldsTest), as the card wire and the tally above read the
# join that builds it.

if __name__ == "__main__":
    unittest.main()
