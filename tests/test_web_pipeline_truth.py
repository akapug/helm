#!/usr/bin/env python3
"""ONE PIPELINE READING, AND NO ZERO FOR A COUNT NOBODY READ (task/3631,
task/3632).

The owner read the land pipeline four ways at one instant (console walk 2,
finding 1, 13:55:36Z): the nav badge said 37, Home "46 in flight · 28
contrary · 9 stalled", the land board "79 in flight · 26 stalled · 9
contrary", the scheduler "S 9 · C 9". Each surface counted its own row set
with its own predicate.

THE SERVER HALF, here: a joined section's staleness bound covers the bounds
its own age adds up from (the leg's and the land pipeline's), the pipeline
body names its own bound, the first board after a restart waits for its
fleet read, and the scheduler counts by the board's one mark.

THE READING ITSELF is one server function now, `work_model.pipe_counts`,
and every surface that printed a pipeline number reads it through
`/api/work` (task/3643): the worlds below drive it in tests/test_work_model.py,
and the page's never-zero rule is tests/test_web_work_page.py.
"""
import copy
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-pipeline-truth-", var="HELM_HOME")

from tests import test_web_landboard as _lb  # noqa: E402

from helm import web  # noqa: E402,F401  (web_land splices its globals)
from helm import (scheduler, web_board, web_land,  # noqa: E402
                  web_land_model)


# ---------------------------------------------------------------------------
# THE SERVER HALF

def _row(rid, **kw):
    """One non-terminal land request, as `landreq_cli.card` sends it."""
    row = {"id": rid, "lane": "lane-" + rid, "state": "AWAITING_REVIEW",
           "kind": "review", "terminal": False, "honored": False,
           "chain_root": rid, "owed_by": "reviewer",
           "holder_role": "reviewer", "holder_seat": "codex",
           "dwell_s": 600, "dwell_known": True, "contrary": False,
           "stalled": False, "review_sha": "a" * 12, "gate": "",
           "trunk_contains_tip": None, "frontier": None,
           "frontier_rung": None}
    row.update(kw)
    return row


class SectionBoundTest(unittest.TestCase):
    """A joined section's age is the leg's own age plus the land pipeline's
    read age, and each of those is bounded on its own (`SECTION_LIMIT_S` for
    the leg, `_LR_HARD_TTL_S` for the pipeline body). A bound of the leg's
    alone sat inside that envelope: a body the pipeline was still serving,
    and refreshing, read STALE, and the board flipped "helm 24 → helm 0
    marked STALE → helm 24" in ten minutes (walk 2, finding 2)."""

    BOUND = web_board.SECTION_LIMIT_S + web_land_model._LR_HARD_TTL_S

    def lands(self, read_age_s):
        body = {"withheld": {"scope": "alpha"}, "read_age_s": read_age_s,
                "read_at": time.time() - read_age_s,
                "unavailable": None, "loops": [_row("r1")],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}
        sec, _rec = web_board._lands_join(lambda _qs: (body, 200))
        return web_board._aged(sec, time.time())

    def fleet(self, read_age_s):
        body = {"withheld": {"scope": "alpha"}, "read_age_s": read_age_s,
                "read_at": time.time() - read_age_s,
                "unavailable": None,
                "loops": [_row("b1", foreign_project="beta")],
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}
        with mock.patch.object(web_board, "_fleet_kick", lambda: None), \
                mock.patch.dict(web_board._FLEET,
                                {"done": (time.time(), body, None)}):
            sec, _out = web_board._fleet_now(["alpha", "beta"], time.time())
        return web_board._aged(sec, time.time())

    def test_a_reading_the_pipeline_still_serves_is_not_stale(self):
        """700 s: past the leg's 600 s, inside the pipeline's own serving
        envelope — the reading a restart restores, or one a slow rebuild is
        replacing."""
        lands, fleet = self.lands(700), self.fleet(700)
        self.assertEqual((lands["limit_s"], fleet["limit_s"]),
                         (self.BOUND, self.BOUND))
        self.assertEqual((lands["stale"], fleet["stale"]), (False, False))
        self.assertGreaterEqual(min(lands["age_s"], fleet["age_s"]), 700)

    def test_a_reading_past_both_bounds_is_stale(self):
        self.assertEqual(self.BOUND, 1200)
        lands = self.lands(self.BOUND + 60)
        fleet = self.fleet(self.BOUND + 60)
        self.assertEqual((lands["stale"], fleet["stale"]), (True, True))


class FleetFirstReadTest(unittest.TestCase):
    """THE FIRST BOARD AFTER A RESTART WAITS FOR ITS FLEET READ, a little:
    the all-projects read restores its body from disk in well under a
    second, and a board answered before it read every other project "still
    being read" — 238 rows of it (walk 2, finding 2)."""

    def test_the_fleet_read_kicked_by_this_board_is_waited_for(self):
        body = {"withheld": {"scope": "alpha"}, "read_age_s": 30,
                "unavailable": None,
                "loops": [_row("b1", foreign_project="beta")],
                "recent_lands": {"rows": [], "total": 0, "unavailable": None}}

        def kick():
            def read():
                time.sleep(0.2)
                web_board._FLEET["done"] = (time.time(), body, None)
            worker = threading.Thread(target=read, daemon=True)
            web_board._FLEET["thread"] = worker
            worker.start()

        with mock.patch.object(web_board, "_fleet_kick", kick), \
                mock.patch.dict(web_board._FLEET, {"thread": None,
                                                   "done": None}):
            sec, out = web_board._fleet_now(["alpha", "beta"], time.time())
        self.assertFalse(sec.get("loading"), sec)
        self.assertEqual(out["beta"]["tally"]["live"], 1)


class ResponseAgesTest(unittest.TestCase):
    """A card's age is its age at the response (walk 2, finding 18: one row
    read "5m" on its board card and "9m" in the scheduler under it). The
    land pipeline's leg keeps its reading for minutes, and its ages were the
    ages the leg stamped; the board moves every one on by the time since."""

    def test_a_kept_leg_moves_its_ages_on_to_the_response(self):
        body = {"withheld": {"scope": "alpha"}, "read_age_s": 30,
                "unavailable": None, "loops": [_row("r1", dwell_s=600)],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [{"lane": "done", "task": "task/9",
                                           "age_s": 60}],
                                 "total": 1, "unavailable": None}}
        sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        alpha = rec["alpha"]
        self.assertEqual(alpha["lanes"]["loops"][0]["age_s"], 630)
        sections = {name: web_board._section(src, None, None, unavailable="x")
                    for name, src in web_board._SECTIONS if name != "lights"}
        sections["lands"] = dict(sec, ages_at=sec["ages_at"] - 240)
        built = {"sections": sections, "joins": {"alpha": alpha},
                 "fleet": None}
        fleet = (web_board._section("x", None, None, loading=True), {})
        with mock.patch.object(web_board, "_board_build", return_value=built), \
                mock.patch.object(web_board, "_fleet_now", return_value=fleet):
            got = web_board._api_board()
        shown = got["projects"]["alpha"]
        self.assertIn(shown["lanes"]["loops"][0]["age_s"], range(870, 880))
        self.assertIn(shown["landed"][0]["age_s"], range(300, 310))
        # the kept leg itself is untouched: the next response ages it again
        self.assertEqual(alpha["lanes"]["loops"][0]["age_s"], 630)
        self.assertEqual(alpha["landed"][0]["age_s"], 60)

    def test_every_age_a_record_carries_moves_once(self):
        rec = {"lanes": {"loops": [{"age_s": 1}], "loops_cut": [{"age_s": 2}],
                         "rehold": {"oldest_age_s": 3,
                                    "rows": [{"lr": {"age_s": 3}}]},
                         "on_main": {"oldest_age_s": 4,
                                     "rows": [{"age_s": 4}]},
                         "collapsed": [{"oldest_age_s": 5,
                                        "rows": [{"age_s": 5}]}]},
               "landed": [{"age_s": 6}, {"age_s": None}],
               "waits": [{"oldest_age_s": 7, "rows": [{"age_s": 7}]}],
               "waits_collapsed": [{"oldest_age_s": 8}]}
        got = web_board._reaged(rec, 100)
        lanes = got["lanes"]
        self.assertEqual([lanes["loops"][0]["age_s"],
                          lanes["loops_cut"][0]["age_s"],
                          lanes["rehold"]["oldest_age_s"],
                          lanes["rehold"]["rows"][0]["lr"]["age_s"],
                          lanes["on_main"]["oldest_age_s"],
                          lanes["on_main"]["rows"][0]["age_s"],
                          lanes["collapsed"][0]["oldest_age_s"],
                          lanes["collapsed"][0]["rows"][0]["age_s"],
                          got["landed"][0]["age_s"], got["landed"][1]["age_s"],
                          got["waits"][0]["oldest_age_s"],
                          got["waits"][0]["rows"][0]["age_s"],
                          got["waits_collapsed"][0]["oldest_age_s"]],
                         [101, 102, 103, 103, 104, 104, 105, 105, 106, None,
                          107, 107, 108])
        self.assertEqual(rec["lanes"]["loops"][0]["age_s"], 1)
        self.assertIs(web_board._reaged(rec, 0), rec)


class ApiLrTest(unittest.TestCase):
    """The pipeline body names its own staleness bound, and its scheduler
    counts by the board's one mark."""

    def body(self, rows, stalled_ids=()):
        return {"read_ts": time.time(), "ledger_mtime": time.time(),
                "withheld": {"scope": "alpha"}, "unavailable": None,
                "_scheduler_rows": rows,
                "_scheduler_active_ids": [r["id"] for r in rows],
                "loops": rows, "stalled_ids": list(stalled_ids),
                "unmeasurable": [],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [], "total": 0, "source": "x",
                                 "unavailable": None}}

    def lr(self, body):
        with mock.patch.object(web_land, "_cached_swr",
                               return_value=copy.deepcopy(body)), \
                mock.patch.object(scheduler, "read_owner_asks",
                                  return_value=([], None)), \
                mock.patch.object(web_land, "_lr_building",
                                  return_value={"rows": [], "total": 0,
                                                "unavailable": None}):
            got, status = web_land._api_lr({})
        self.assertEqual(status, 200)
        return got

    def test_the_body_names_the_bound_its_readers_judge_it_by(self):
        """The cards judged /api/lr's reading by 4x the page's poll (180 s)
        while the server serves a body up to its hard bound while it
        rebuilds: measured read ages ran 62-407 s, so Home's tile read
        "stale" most of the time (walk 2, finding 21)."""
        got = self.lr(self.body([_row("r1")]))
        self.assertEqual(got["limit_s"], web_land_model._LR_HARD_TTL_S)

    def test_the_scheduler_counts_what_the_board_counts(self):
        """A stall a proved successor carried alarms nowhere, and a row both
        stalled and contrary is counted once, as contrary: the board's one
        mark. The scheduler counted the enforcement stall set, non-disjoint,
        so its "S n · C n" could not be the board's numbers."""
        rows = [_row("s1", stalled=True),
                _row("s2", stalled=True, succession_state="moved"),
                _row("c1", stalled=True, contrary=True),
                _row("m1")]
        got = self.lr(self.body(rows, stalled_ids=("s1", "s2", "c1")))
        suc = got["scheduler"]["suc"]
        _sec, rec = web_board._lands_join(lambda _qs: (got, 200))
        marks = rec["alpha"]["lanes"]["tally"]["marks"]
        self.assertEqual((marks["stalled"], marks["contrary"]), (1, 1))
        self.assertEqual((suc["stalled"], suc["contrary"],
                          suc["unmeasurable"]),
                         (marks["stalled"], marks["contrary"],
                          marks["nonbillable"]))
        by = {g["label"]: g["suc"] for g in got["scheduler"]["groups"]}
        self.assertEqual(sum(g["stalled"] for g in by.values()), 1)
        self.assertEqual(sum(g["contrary"] for g in by.values()), 1)

    def test_one_mark_function_decides_both(self):  # noqa: VACUOUS_ASSERTION — the unconditional contrary answer is the control; the loop pins each of the four marks on both functions
        self.assertEqual(scheduler.mark({"id": "z", "contrary": True}, ()),
                         "contrary")
        for card, want in (({"id": "x", "contrary": True, "stalled": True},
                            "contrary"),
                           ({"id": "x", "stalled": True}, "stalled"),
                           ({"id": "x"}, "nonbillable"),
                           ({"id": "y"}, "moving")):
            with self.subTest(card=card):
                self.assertEqual(scheduler.mark(card, {"x"}), want)
                self.assertEqual(web_board._mark(card, {"x"}), want)


# ---------------------------------------------------------------------------
# THE WORLDS THE ONE READING IS READ OVER
#
# The browser half went with the pages that drew it (task/3643): the land
# board's strip, Home's pipeline tile, the nav badge's `pipeNav` and the
# scheduler's head were four projections of the page's `pipeCounts`, and the
# Work page counts nothing of its own — it reads `/api/work`, whose reading
# is the server's `work_model.pipe_counts` over the same boards. These worlds
# drive that server reading (tests/test_work_model.py,
# OneReadingOverTheWorldsTest), and the page's never-zero rule over it is
# tests/test_web_work_page.py, OneCountEverywhereTest.

def _worlds():
    board = _lb._world()
    loading = copy.deepcopy(board)
    loading["sections"]["fleet"] = {"loading": True}
    restart = copy.deepcopy(board)
    restart["sections"]["lands"] = {"loading": True, "scope": None}
    restart["sections"]["fleet"] = {"loading": True}
    stale = copy.deepcopy(board)
    stale["sections"]["lands"] = dict(_lb._SEC, scope="alpha", age_s=1300,
                                      limit_s=1200, stale=True)
    fleet_stale = copy.deepcopy(board)
    fleet_stale["sections"]["fleet"] = dict(_lb._SEC, scope="alpha",
                                            age_s=1300, limit_s=1200,
                                            stale=True)
    down = copy.deepcopy(board)
    down["sections"]["fleet"] = {"unavailable": "the all-projects read "
                                 "raised (OSError)"}
    whole = copy.deepcopy(board)
    alpha = whole["projects"]["alpha"]["lanes"]
    alpha["loops_cut"] = [_lb._card("a-cut-%d" % i, "a-cut-%d" % i,
                                    "AWAITING_REVIEW", stalled=i < 4)
                          for i in range(10)]
    alpha["loops_more"] = {}
    gate = copy.deepcopy(board)
    gate["projects"]["alpha"]["gate"] = [{
        "label": "train463", "train": "train463", "host": "box-a",
        "age_s": 23, "head": "abc", "running": "running", "lanes": []}]
    # ONE PROJECT READ WITH NOTHING IN FLIGHT, the others still being read
    # (review 3631, F1): a floor of 0 over a partial scope is not a count
    partial_zero = copy.deepcopy(loading)
    partial_zero["projects"]["alpha"]["lanes"].update(
        loops=[], loops_more={}, tally={
            "live": 0, "marks": {"contrary": 0, "stalled": 0,
                                 "nonbillable": 0, "moving": 0},
            "holders": {}})
    # STALE WITH NO MEASURED CLOCK (review 3631, round 2): `_aged` marks a
    # readable section with no `measured_at` stale with no age, and the
    # other projects' section beside it was read 5 s ago
    noclock = copy.deepcopy(board)
    noclock["sections"]["lands"] = dict(_lb._SEC, scope="alpha",
                                        measured_at=None, age_s=None,
                                        stale=True)
    return {"partial_zero": partial_zero, "noclock": noclock,
            "loading": loading, "restart": restart, "stale": stale,
            "fleet_stale": fleet_stale, "down": down, "gate": gate,
            "whole": whole}


if __name__ == "__main__":
    unittest.main()
