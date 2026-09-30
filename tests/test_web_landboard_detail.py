#!/usr/bin/env python3
"""WHAT THE ONE LAND BOARD'S JOIN CARRIES (task/3585, cure 3), SERVER HALF.

The integrator's feature-parity read over the old wall found nine behaviours
the one land board had dropped, and the board's cure carried each one: a
card's marks and detail, the succession-aware stall, the building detail, a
land's proof and chain, and the rows behind each count line. The board that
drew them was retired with the pipeline page (task/3643): the Work page reads
/api/work, one card per piece of work, and a card's detail is its drawer's
crossings. What stays true is the server half, and these arms pin it: the
fields `web_board`'s join sends on each card, which /api/board and the one
work reader over it still read. The browser half, and its node runner, went
with the board.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-landboard-detail-", var="HELM_HOME")

from tests import test_web_landboard as LB  # noqa: E402

from helm import landreq, web_board  # noqa: E402

_loop = LB._loop


# ---------------------------------------------------------------------------
# 1. THE FIELDS A CARD'S MARKS ARE READ OFF

_RICH = dict(
    owed_seat_standing={"state": "ORPHANED",
                        "why": "no roster row answers to codex-old"},
    abandoned=True, abandon_reason="the reviewed work was written off",
    contrary=True, contrary_state="landed", contrary_provenance="observed",
    polarity="fix", polarity_source="dispatch-store",
    ungated="the gate_caps stamp on its receipt is unreadable",
    attest_state="unverifiable", attest_detail="the sidecar binds other "
    "verdict evidence", attest_source="attest-sidecar",
    ledger_refused=["verdict"],
    advisory_lines=["ADVISORY read by kimi-1: no regression seen "
                    "(discharges nothing)"],
    author="claude-a", reviewer="codex-b", branch="lane/rich-1",
    review_sha="0123456789ab", review_sha_full="0123456789ab" + "c" * 28,
    timeline=[{"state": "AWAITING_REVIEW", "ts": "2026-09-28T01:00:00Z"},
              {"state": "CHANGES_REQUESTED", "ts": "2026-09-28T02:00:00Z"}],
    receipt_state="none", succession_state="held")



class CardFieldsTest(unittest.TestCase):
    """Item 1: every honest annotation the old wall printed rides the card
    the join sends."""

    @classmethod
    def setUpClass(cls):
        cls.rich = web_board._kanban_card(_loop("rich", "rich-1", **_RICH), 600)

    def test_the_card_carries_the_fields_its_marks_are_read_off(self):
        self.assertEqual(len(self.rich["timeline"]), 2)
        for k in ("owed_seat_standing", "abandoned", "abandon_reason",
                  "contrary_state", "contrary_provenance", "polarity",
                  "ungated", "attest_state", "attest_detail",
                  "ledger_refused", "advisory_lines", "author", "reviewer",
                  "review_sha_full", "timeline", "branch"):
            with self.subTest(field=k):
                self.assertEqual(self.rich.get(k), _RICH[k])

    def test_a_held_card_carries_its_source_clean_tip(self):  # noqa: VACUOUS_ASSERTION — the absent tip on the plain card sits beside the held card's unconditional positive on the same field
        """A source-clean car rides at its HELD tip, and the work reader
        matches a pushed car to its request by that tip (task/3723): the
        card carries it; a row with no hold carries none."""
        held = web_board._kanban_card(_loop("h", "held-1",
                                            source_clean_tip="cd" * 20))
        self.assertEqual(held.get("source_clean_tip"), "cd" * 20)
        self.assertNotIn("source_clean_tip",
                         web_board._kanban_card(_loop("p", "plain-1")))

    def test_a_nonbillable_card_says_why_it_is_not_measurable(self):
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": [_loop("n1", "hold-1")],
                "unmeasurable": [{"id": "n1", "kind": "pre-tier",
                                  "reason": "APPROVE, held — PRE-TIER"}],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [], "total": 0,
                                 "unavailable": None}}
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        card = rec["proj"]["lanes"]["loops"][0]
        self.assertEqual(card.get("unmeasurable_reason"),
                         "APPROVE, held — PRE-TIER")



# ---------------------------------------------------------------------------
# 2. A ROW A SUCCESSOR CARRIED IS NOT STALLED

class SuccessionQuietsTheStallTest(unittest.TestCase):
    """Item 2. `web_board._kanban_card` copied the raw `stalled` flag, so a
    row cured on a proved successor alarmed STALLED on the board — counted,
    marked and filterable — while `helm lr list` printed nothing for it."""

    ROWS = (("moved", dict(stalled=True, succession_state="moved"), False),
            ("unknown", dict(stalled=True, succession_state="unknown"), True),
            ("plain", dict(stalled=True, succession_state="held"), True),
            ("honored", dict(stalled=True, contrary=True,
                             contrary_discharge="a"), False),
            ("fresh", dict(stalled=False), False))

    def test_the_card_and_the_tally_carry_the_succession_aware_stall(self):
        cards = [web_board._kanban_card(_loop(rid, rid + "-1", **kw))
                 for rid, kw, _want in self.ROWS if rid != "honored"]
        self.assertEqual({c["id"]: c["stalled"] for c in cards},
                         {"moved": False, "unknown": True, "plain": True,
                          "fresh": False})
        feed = web_board._kanban_feed(cards)
        self.assertEqual(feed["tally"]["marks"]["stalled"], 2)
        self.assertEqual({c["id"]: c["mark"] for c in feed["loops"]}["moved"],
                         "moving")

    def test_the_board_and_the_terminal_ask_one_predicate(self):  # noqa: VACUOUS_ASSERTION — the False answers sit beside an unconditional True on the same predicate (a stalled row) and the True rows of the same loop, so a predicate answering False everywhere fails
        self.assertIs(landreq.stall_alarm(_loop("x", "x-1", stalled=True)),
                      True)
        for rid, kw, want in self.ROWS:
            row = _loop(rid, rid + "-1", **kw)
            with self.subTest(row=rid):
                self.assertIs(landreq.stall_alarm(row), want)
                if rid != "honored":
                    self.assertIs(web_board._kanban_card(row)["stalled"],
                                  want)


# ---------------------------------------------------------------------------
# 5. THE ROWS THE CAP CUT ARE SENT

class CapCutTest(unittest.TestCase):
    """Item 5: the rows the server's cap cut are sent as `loops_cut`, never
    only counted."""

    def test_the_rows_the_cap_cut_are_sent_not_only_counted(self):
        feed = web_board._kanban_feed(
            [web_board._kanban_card(_loop("r%02d" % i, "l-%d" % i))
             for i in range(web_board.KANBAN_ROWS + 5)])
        self.assertEqual(len(feed["loops_cut"]), 5)
        self.assertEqual([c["id"] for c in feed["loops_cut"]],
                         ["r%02d" % i for i in range(
                             web_board.KANBAN_ROWS, web_board.KANBAN_ROWS + 5)])


# ---------------------------------------------------------------------------
# 6. WHAT IS BUILDING, IN DETAIL, AND UNKNOWN WHEN UNREAD

class BuildingDetailTest(unittest.TestCase):
    """Item 6. The old band printed each building lane's holder, commits
    ahead, lease left and uncommitted edits, and the lanes git could not
    measure; a failed lane read drew no lanes, as if none were building."""

    ROW = {"lane": "a-build-0", "holder": "claude-z", "ahead": 3,
           "lease_remaining_s": 7200, "dirty": True, "liveness": "live"}

    def body(self, building):
        return {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": [], "building": building,
                "recent_lands": {"rows": [], "total": 0,
                                 "unavailable": None}}

    def test_the_join_sends_each_building_rows_detail(self):
        _s, rec = web_board._lands_join(lambda _qs: (self.body(
            {"rows": [self.ROW], "total": 1, "unmeasured": 2,
             "unavailable": None}), 200))
        lanes = rec["proj"]["lanes"]
        self.assertEqual(lanes.get("building_rows"), [
            {k: self.ROW[k] for k in ("lane", "holder", "ahead",
                                      "lease_remaining_s", "dirty")}])
        self.assertEqual(lanes.get("building_unmeasured"), 2)
        _s, rec = web_board._lands_join(lambda _qs: (self.body(
            {"rows": [], "total": None, "unmeasured": 0,
             "unavailable": "git refused the lane rooms"}), 200))
        self.assertEqual(rec["proj"]["lanes"].get("building_unavailable"),
                         "git refused the lane rooms")


# ---------------------------------------------------------------------------
# 7. A LANDED CARD'S PROOF AND ITS CHAIN

class LandedProofTest(unittest.TestCase):
    """Item 7. A land carries how it was proven, its gate token and its
    chain, the land proof a card's drawer names."""

    def test_the_join_sends_the_proof_and_the_chain(self):  # noqa: VACUOUS_ASSERTION — every field is asserted EQUAL to a non-empty value, and the lane and tip of the same landed card are an unconditional control
        body = {"withheld": {"scope": "proj"}, "read_age_s": 5,
                "unavailable": None, "loops": [],
                "building": {"rows": [], "total": 0, "unavailable": None},
                "recent_lands": {"rows": [
                    {"lane": "done", "task": "task/9", "age_s": 60,
                     "reviewed_tip": "f" * 40, "gate": "",
                     "on_trunk": True, "how": "ancestor",
                     "chain_root": "R9", "trunk_sha": "f" * 40,
                     "trunk_ref": "refs/remotes/origin/main",
                     "ts": "2026-09-28T02:00:00Z", "ts_unreadable": False}],
                    "total": 1, "unavailable": None}}
        _sec, rec = web_board._lands_join(lambda _qs: (body, 200))
        land = rec["proj"]["landed"][0]
        self.assertEqual((land["lane"], land["tip"]), ("done", "f" * 12))
        for k, want in (("on_trunk", True), ("how", "ancestor"),
                        ("chain_root", "R9"), ("trunk_sha", "f" * 12)):
            with self.subTest(field=k):
                self.assertEqual(land.get(k), want)


# ---------------------------------------------------------------------------
# CURE 4 (a). THE ON-MAIN AND OFF-FRONTIER LINES CARRY THEIR ROWS

_FOLDED = [_loop("onmain1", "reviewed-in-chat", trunk_contains_tip=True,
                 dwell_s=3 * 86400),
           _loop("onmain2", "also-in-history", trunk_contains_tip=True),
           _loop("left1", "left-over-1", frontier="landed-by-ancestry",
                 trunk_contains_tip=True),
           _loop("left2", "left-over-2", frontier="landed-by-ancestry",
                 trunk_contains_tip=True),
           _loop("live", "under-review", trunk_contains_tip=False)]


class CountLinesOpenTheirRowsTest(unittest.TestCase):
    """Item 5, cure 4: the on-main line and the off-frontier line carry
    exactly the rows they count, which the work reader types one by one."""

    def test_the_join_sends_the_rows_each_line_counts(self):
        live, on_main, folded = web_board._kanban_split(_FOLDED, 0)
        self.assertEqual([c["id"] for c in live], ["live"])
        self.assertEqual(on_main["count"], 2)
        self.assertEqual(sorted(r["id"] for r in on_main.get("rows") or []),
                         ["onmain1", "onmain2"])
        off = {c["class"]: c for c in folded}["off_frontier"]
        self.assertEqual(off["count"], 2)
        self.assertEqual(sorted(r["id"] for r in off.get("rows") or []),
                         ["left1", "left2"])


if __name__ == "__main__":
    unittest.main()
