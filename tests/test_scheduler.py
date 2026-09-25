"""Canonical who-waits-on-whom model: judgement shared by Web and ASCII."""
import os
import tempfile
import unittest
from unittest import mock

from helm import pk, scheduler


NOW = 2_000_000_000


def card(rid, **kw):
    row = {"id": rid, "terminal": False, "chain_root": rid,
           "supersedes": None, "lane": "lane " + rid, "state": "OPEN",
           "owed_by": "integrator", "holder_role": "integrator",
           "holder_seat": None, "dwell_s": 60, "dwell_known": True,
           "hold_ts": None, "stalled": False, "contrary": False,
           "honored": False}
    row.update(kw)
    return row


class SchedulerProjectionTest(unittest.TestCase):
    def test_every_nonterminal_row_is_accounted_once_and_terminal_never_is(self):
        """A LIVE row is listed in one holder group; every other non-terminal
        row is COUNTED in exactly one collapsed line; a terminal row is in
        neither. The two halves partition `row_count`."""
        rows = [card("a"), card("b", honored=True, holder_role="nobody"),
                card("closed", terminal=True)]
        got = scheduler.project(rows, active_ids=["a"], now=NOW,
                                projection_age_s=5)
        shown = [row["id"] for group in got["groups"] for row in group["rows"]]
        self.assertEqual(shown, ["a"])
        self.assertEqual([(c["class"], c["count"]) for c in got["collapsed"]],
                         [("superseded", 1)])
        self.assertEqual(got["row_count"], 2)
        self.assertEqual(got["listed_count"], 1)
        self.assertEqual(got["collapsed_count"], 1)
        self.assertEqual(got["active_count"], 1)

    def test_resolved_holder_groups_without_reinterpreting_owed_by(self):
        rows = [
            card("ownerish", owed_by="builder", holder_role="owner"),
            card("review", owed_by="reviewer", holder_role="reviewer",
                 holder_seat="codex"),
            card("integrate"),
            card("missing", owed_by="author", holder_role=None,
                 holder_seat="someone"),
        ]
        got = scheduler.project(rows, active_ids=[r["id"] for r in rows],
                                now=NOW, projection_age_s=0)
        self.assertEqual([g["label"] for g in got["groups"]],
                         ["integrator", "owner", "reviewer @codex", "unknown"])
        projected = {r["id"]: r for g in got["groups"] for r in g["rows"]}
        self.assertEqual(projected["ownerish"]["owed_by"], "builder")
        self.assertEqual(projected["missing"]["holder_role"], "unknown")

    def test_suc_counts_only_active_rows_globally_and_per_group(self):
        rows = [card("s"), card("u", holder_role="reviewer", holder_seat="r"),
                card("c", contrary=True),
                card("settled", honored=True, contrary=True,
                     holder_role="nobody")]
        got = scheduler.project(
            rows, stalled_ids=["s", "settled"],
            unmeasurable=[{"id": "u", "reason": "age unreadable"}],
            active_ids=["s", "u", "c"], now=NOW, projection_age_s=0)
        self.assertEqual(got["suc"], {"stalled": 1, "unmeasurable": 1,
                                      "contrary": 1, "total": 3,
                                      "zero": False})
        by = {g["label"]: g["suc"] for g in got["groups"]}
        self.assertEqual(by["integrator"]["stalled"], 1)
        self.assertEqual(by["reviewer @r"]["unmeasurable"], 1)
        # the settled row owes nobody a move, so it is not a group at all:
        # it is counted on the superseded line, and its stall bills no one
        self.assertNotIn("nobody", by)
        self.assertEqual([(c["class"], c["count"]) for c in got["collapsed"]],
                         [("superseded", 1)])

    def test_known_age_adds_projection_age_and_unknown_stays_unknown(self):
        got = scheduler.project(
            [card("old", dwell_s=120), card("blind", dwell_known=False,
                                             dwell_s=0)],
            active_ids=["old", "blind"], now=NOW, projection_age_s=30)
        rows = {r["id"]: r for g in got["groups"] for r in g["rows"]}
        self.assertEqual(rows["old"]["age_s"], 150)
        self.assertTrue(rows["old"]["age_known"])
        self.assertIsNone(rows["blind"]["age_s"])
        self.assertFalse(rows["blind"]["age_known"])
        self.assertEqual(got["projection_age_s"], 30)

    def test_missing_projection_age_refuses_all_lr_ages(self):
        got = scheduler.project([card("a", dwell_s=120)], active_ids=["a"],
                                now=NOW, projection_age_s=None)
        row = got["groups"][0]["rows"][0]
        self.assertEqual(row["id"], "a")  # control: the row was projected
        self.assertIsNone(row["age_s"])
        self.assertIsNone(got["projection_age_s"])

    def test_malformed_or_negative_dwell_is_unknown_not_zero(self):
        got = scheduler.project(
            [card("alien", dwell_s="not-a-number"), card("future", dwell_s=-4)],
            active_ids=["alien", "future"], now=NOW, projection_age_s=7)
        rows = {r["id"]: r for g in got["groups"] for r in g["rows"]}
        self.assertEqual(set(rows), {"alien", "future"})  # control: both rows ran
        self.assertIsNone(rows["alien"]["age_s"])
        self.assertIsNone(rows["future"]["age_s"])

    def test_declared_wait_and_supersession_edges_include_dangling_truth(self):
        got = scheduler.project(
            [card("new", supersedes="old"), card("peer")],
            active_ids=["new", "peer"], now=NOW, projection_age_s=0)
        supersedes = [e for e in got["edges"] if e["kind"] == "supersedes"]
        self.assertEqual(supersedes, [{"kind": "supersedes", "from": "new",
                                       "to": "old", "target_known": False}])
        waits = [e for e in got["edges"] if e["kind"] == "waits_on"]
        self.assertEqual({e["from"] for e in waits}, {"new", "peer"})
        self.assertTrue(all(e["stage_class"] == "open" for e in waits))

    def test_absorbed_predecessor_has_no_blocking_edge_or_suc_debt(self):
        rows = [card("old", stalled=True), card("new", supersedes="old")]
        got = scheduler.project(rows, stalled_ids=["old"], active_ids=["new"],
                                now=NOW, projection_age_s=0)
        projected = {r["id"]: r for g in got["groups"] for r in g["rows"]}
        waits = [e["from"] for e in got["edges"] if e["kind"] == "waits_on"]
        self.assertEqual(sorted(projected), ["new"])
        self.assertEqual(waits, ["new"])
        self.assertEqual(got["suc"]["stalled"], 0)
        # the predecessor is still ACCOUNTED — counted, never dropped — and
        # the successor's declared edge still knows it exists
        self.assertEqual([(c["class"], c["count"]) for c in got["collapsed"]],
                         [("superseded", 1)])
        sup = [e for e in got["edges"] if e["kind"] == "supersedes"]
        self.assertEqual(sup, [{"kind": "supersedes", "from": "new",
                                "to": "old", "target_known": True}])

    def test_active_membership_is_typed_complete_and_names_known_rows(self):
        missing = scheduler.project([card("a")], active_ids=None, now=NOW,
                                    projection_age_s=0)
        duplicate = scheduler.project([card("a")], active_ids=["a", "a"],
                                      now=NOW, projection_age_s=0)
        alien = scheduler.project([card("a")], active_ids=["missing"],
                                  now=NOW, projection_age_s=0)
        untyped = scheduler.project([card("a")], active_ids=[1], now=NOW,
                                    projection_age_s=0)
        self.assertIn("not a list", missing["unavailable"])
        self.assertIn("duplicate", duplicate["unavailable"])
        self.assertIn("unknown row", alien["unavailable"])
        self.assertIn("untyped id", untyped["unavailable"])
        self.assertIsNone(missing["active_count"])

    def test_owner_hold_and_absent_owed_by_remain_distinct_truth(self):
        got = scheduler.project(
            [card("owner", holder_role="owner", owed_by=None,
                  owner_gated=True, hold_reason="choose publication")],
            active_ids=["owner"], now=NOW, projection_age_s=0)
        row = got["owner_holds"][0]
        self.assertEqual(got["owner_hold_count"], 1)
        self.assertFalse(row["owed_by_known"])
        self.assertTrue(row["owed_by_present"])
        self.assertIsNone(row["owed_by"])
        absent = scheduler.project(
            [{k: v for k, v in card("absent").items() if k != "owed_by"}],
            active_ids=["absent"], now=NOW, projection_age_s=0)
        absent_row = absent["groups"][0]["rows"][0]
        self.assertFalse(absent_row["owed_by_present"])
        self.assertEqual(row["detail"], "choose publication")

    def test_owner_hold_age_starts_at_hold_not_stage_entry(self):
        got = scheduler.project(
            [card("owner", holder_role="owner", owner_gated=True,
                  dwell_s=86400, hold_ts="2033-05-18T03:32:50Z")],
            active_ids=["owner"], now=NOW, projection_age_s=90)
        row = got["owner_holds"][0]
        self.assertEqual(row["age_s"], 30)
        self.assertNotEqual(row["age_s"], 86490)
        malformed = scheduler.project(
            [card("owner", holder_role="owner", owner_gated=True,
                  dwell_s=86400, hold_ts="broken")],
            active_ids=["owner"], now=NOW, projection_age_s=90)
        self.assertIsNone(malformed["owner_holds"][0]["age_s"])

    def test_unknown_stage_is_not_laundered_to_ready(self):
        got = scheduler.project(
            [card("odd", state="FUTURE_WORD"),
             card("readyish", state="READY_FROM_UNKNOWN_PRODUCER")],
            active_ids=["odd", "readyish"], now=NOW, projection_age_s=0)
        rows = {r["id"]: r for g in got["groups"] for r in g["rows"]}
        self.assertEqual(rows["odd"]["stage_class"], "unknown")
        self.assertEqual(rows["readyish"]["stage_class"], "unknown")

    def test_plain_titles_strip_fleet_hash_and_arrow_tail(self):
        title = "a class of bug deadbee -> f00ba47 extra"
        got = scheduler.project([card("a", lane=title)], active_ids=["a"],
                                now=NOW, projection_age_s=0)
        self.assertEqual(got["groups"][0]["rows"][0]["plain_title"],
                         "a class of bug")
        legit = scheduler.project(
            [card("b", lane="defaced output renders fix output")],
            active_ids=["b"], now=NOW, projection_age_s=0)
        self.assertEqual(legit["groups"][0]["rows"][0]["plain_title"],
                         "defaced output renders fix output")

    def test_owner_asks_are_oldest_first_actionable_and_pinned_outside_groups(self):
        asks = [
            {"ask": "new decision", "why": "options: a | b",
             "state": "waiting-owner-decision", "hold_kind": "decision",
             "since": "2033-05-18T03:32:50Z"},
            {"ask": "old input", "why": "relogin",
             "state": "waiting-owner", "hold_kind": "usage-reset",
             "since": "2033-05-18T03:31:20Z"},
            {"ask": "broken age", "why": "repair stamp",
             "state": "waiting-owner", "hold_kind": "owner-input",
             "since": "future"},
        ]
        got = scheduler.project([], active_ids=[], owner_asks=asks,
                                now=2_000_000_000, projection_age_s=0)
        self.assertEqual([a["plain_title"] for a in got["owner_asks"]],
                         ["old input", "new decision", "broken age"])
        self.assertEqual(got["owner_asks"][0]["hold_kind"], "usage-reset")
        self.assertEqual(got["owner_asks"][1]["hold_kind"], "decision")
        self.assertEqual(got["owner_asks"][2]["hold_kind"], "owner-input")
        self.assertIsNone(got["owner_asks"][2]["age_s"])
        self.assertEqual([e["kind"] for e in got["edges"]],
                         ["waits_on_owner_usage_reset",
                          "waits_on_owner_decision",
                          "waits_on_owner_owner_input"])

    def test_a_malformed_owner_queue_is_unknown_not_a_clean_ok(self):
        healthy = scheduler.project(
            [], active_ids=[], owner_asks=[
                {"ask": "choose", "why": "blocked",
                 "state": "waiting-owner", "hold_kind": "owner-input",
                 "since": "2020-01-01"}], now=NOW, projection_age_s=0)
        self.assertIsNone(healthy["owner_asks_unavailable"])
        self.assertEqual(healthy["owner_ask_count"], 1)
        got = scheduler.project(
            [], active_ids=[], owner_asks=[
                {"ask": ["not", "words"], "why": "blocked",
                 "state": "waiting-owner", "hold_kind": "owner-input",
                 "since": "2020-01-01"}], now=NOW, projection_age_s=0)
        self.assertIsNotNone(got["owner_asks_unavailable"])
        self.assertIsNone(got["owner_ask_count"])
        self.assertEqual(got["owner_asks"], [])

    def test_owner_ask_kind_is_typed_and_structured_fields_never_stringify(self):
        missing_kind = scheduler.project(
            [], active_ids=[], owner_asks=[
                {"ask": "relogin", "why": "device auth",
                 "state": "waiting-owner", "since": "2020-01-01"}],
            now=NOW, projection_age_s=0)
        self.assertEqual(missing_kind["owner_asks"][0]["hold_kind"], "unknown")
        self.assertIn("typed hold_kind", missing_kind["owner_asks_unavailable"])
        self.assertEqual(missing_kind["edges"][0]["kind"],
                         "waits_on_owner_unknown")
        malformed = scheduler.project(
            [], active_ids=[], owner_asks=[
                {"ask": ["not", "words"], "why": {"not": "words"},
                 "state": "waiting-owner", "hold_kind": "owner-input",
                 "since": ["not", "a stamp"]}],
            now=NOW, projection_age_s=0)
        self.assertEqual(malformed["owner_asks"], [])
        self.assertIn("non-string", malformed["owner_asks_unavailable"])

    def test_caps_disclose_dropped_rows_and_asks(self):
        got = scheduler.project(
            [card("r%d" % i, dwell_s=i) for i in range(4)],
            owner_asks=[{"ask": "a%d" % i, "state": "waiting-owner",
                         "hold_kind": "owner-input",
                         "since": "2020-01-%02d" % (i + 1)} for i in range(3)],
            active_ids=["r%d" % i for i in range(4)], now=NOW,
            projection_age_s=0, limit_per_group=2, owner_limit=1)
        self.assertEqual(got["groups"][0]["count"], 4)
        self.assertEqual(got["groups"][0]["dropped"], 2)
        self.assertEqual(len(got["groups"][0]["rows"]), 2)
        self.assertEqual(got["owner_ask_count"], 3)
        self.assertEqual(got["owner_asks_dropped"], 2)
        none = scheduler.project(
            [], active_ids=[], owner_asks=[{"ask": "only", "state": "waiting-owner",
                                            "hold_kind": "owner-input"}],
            now=NOW, projection_age_s=0, owner_limit=0)
        self.assertEqual(none["owner_ask_count"], 1)
        self.assertEqual(none["owner_asks"], [])
        self.assertEqual(none["owner_asks_dropped"], 1)

    def test_duplicate_or_untyped_identity_fails_loud(self):
        dup = scheduler.project([card("a"), card("a")], active_ids=["a"],
                                now=NOW, projection_age_s=0)
        self.assertIn("duplicated", dup["unavailable"])
        missing = scheduler.project([{"id": "a"}], active_ids=["a"],
                                    now=NOW, projection_age_s=0)
        self.assertIn("terminal truth", missing["unavailable"])
        self.assertIsNone(missing["suc"]["total"])


class LiveObligationsTest(unittest.TestCase):
    """THE OWNER BOARD LISTS LIVE OBLIGATIONS; EVERYTHING ELSE IS ONE LINE.

    The owner read a waits list of 900 rows up to 60 days old — ledger debris
    whose lane is gone and whose work is on trunk, next to the few rows that
    need a move. The frontier classification those rows carry is the census
    `helm lr retire --off-frontier` runs (one classifier, no second oracle);
    this model only DECIDES, per row, listed or which collapsed line. Every
    arm pairs a collapsed row with a listed control in the SAME projection."""

    def project(self, rows, active=None, **kw):
        ids = [r["id"] for r in rows if not r.get("terminal")]
        return scheduler.project(rows, active_ids=ids if active is None
                                 else active, now=NOW, projection_age_s=10,
                                 **kw)

    @staticmethod
    def listed(got):
        return sorted(r["id"] for g in got["groups"] for r in g["rows"])

    @staticmethod
    def lines(got):
        return {c["class"]: c for c in got["collapsed"]}

    def test_on_frontier_rows_are_listed_and_off_frontier_rows_are_one_line(self):
        got = self.project([
            card("live", frontier="on-frontier", frontier_rung="lane-family"),
            card("anc", frontier="landed-by-ancestry", frontier_rung="landing",
                 dwell_s=86400 * 52),
            card("pid", frontier="landed-by-patch-id", frontier_rung="landing",
                 dwell_s=86400 * 9),
            card("gone", frontier="abandoned-unreachable",
                 frontier_rung="reachability", dwell_s=3600)])
        self.assertEqual(self.listed(got), ["live"])
        off = self.lines(got)["off_frontier"]
        self.assertEqual(off["count"], 3)
        self.assertEqual(off["by_reason"], {"landed-by-ancestry": 1,
                                            "landed-by-patch-id": 1,
                                            "abandoned-unreachable": 1})
        # the OLDEST age is the oldest row's, projection age included
        self.assertEqual(off["oldest_age_s"], 86400 * 52 + 10)
        self.assertEqual(off["command"], "helm lr retire --off-frontier")
        self.assertIn("off the live frontier", off["label"])
        self.assertEqual(got["row_count"], 4)
        self.assertEqual(got["listed_count"] + got["collapsed_count"],
                         got["row_count"], "the lines no longer partition "
                         "the non-terminal rows")

    def test_unclassified_rows_whose_work_is_gone_get_their_own_line(self):
        got = self.project([
            card("pruned", frontier="unclassified", frontier_rung="object",
                 dwell_s=86400 * 20),
            card("unroutable", frontier="unclassified",
                 frontier_rung="polarity", dwell_s=86400 * 3),
            card("control", frontier="on-frontier",
                 frontier_rung="lane-family")])
        self.assertEqual(self.listed(got), ["control"])
        line = self.lines(got)["unclassified"]
        self.assertEqual(line["count"], 2)
        self.assertEqual(line["oldest_age_s"], 86400 * 20 + 10)
        self.assertEqual(line["command"], "helm lr retire --off-frontier")
        self.assertNotIn("off_frontier", self.lines(got),
                         "unplaced rows were counted as placed residue")

    def test_every_other_unclassified_row_is_work_owed_and_stays_listed(self):
        """UNCLASSIFIED IS COUNTED AS WORK OWED on the header, and a review
        dispatched minutes ago answers the `tip` rung whenever its label
        matches no branch — it has no verdict tip to place. A failed reading of the
        lane, room, lease, trunk, hold or landing, and a ref outside the lane
        family still holding the tip, may all be live work too. Each stays on
        the list; only the work-gone rungs fold (the control in this same
        projection)."""
        kept = ("lane-family", "room", "lease", "tip", "trunk", "live-hold",
                "landing", "reachability", "a-rung-from-later")
        got = self.project([
            card(rung, frontier="unclassified", frontier_rung=rung)
            for rung in kept] + [
            card("gone", frontier="unclassified", frontier_rung="object")])
        self.assertEqual(self.listed(got), sorted(kept))
        self.assertEqual(self.lines(got)["unclassified"]["count"], 1)

    def test_a_live_READY_whose_content_is_not_on_trunk_is_listed(self):
        got = self.project([
            card("ready", state="READY", holder_role="lander",
                 owed_by="lander", frontier="on-frontier",
                 frontier_rung="lane-family", dwell_s=86400 * 13),
            card("ready-left-over", state="READY", holder_role="lander",
                 owed_by="lander", frontier="landed-by-patch-id",
                 frontier_rung="landing", dwell_s=86400 * 8)])
        self.assertEqual(self.listed(got), ["ready"])
        lander = [g for g in got["groups"] if g["label"] == "lander"]
        self.assertEqual([g["count"] for g in lander], [1])
        self.assertEqual(lander[0]["oldest_age_s"], 86400 * 13 + 10)
        self.assertEqual(self.lines(got)["off_frontier"]["count"], 1)

    def test_a_row_the_census_never_classified_is_listed_as_before(self):
        """An older warm body, a held REVIEWED row, a foreign row: no
        frontier field, so no frontier claim. Listed while it is live."""
        got = self.project([card("old-body"),
                            card("held", state="REVIEWED", frontier=None),
                            card("future", frontier="a-word-from-later")])
        self.assertEqual(self.listed(got), ["future", "held", "old-body"])
        self.assertEqual(got["collapsed"], [])
        self.assertEqual(got["collapsed_count"], 0)

    def test_an_absorbed_or_settled_row_owes_nobody_and_is_one_line(self):
        got = self.project([
            card("new", supersedes="old", frontier="on-frontier",
                 frontier_rung="lane-family"),
            card("old", holder_role="author", holder_seat="s",
                 frontier="on-frontier", frontier_rung="lane-family",
                 dwell_s=86400 * 60),
            card("settled", honored=True, holder_role="nobody",
                 dwell_s=86400 * 2)], active=["new"])
        self.assertEqual(self.listed(got), ["new"])
        line = self.lines(got)["superseded"]
        self.assertEqual(line["count"], 2)
        self.assertEqual(line["oldest_age_s"], 86400 * 60 + 10)
        self.assertEqual(line["command"], "helm lr list --all")

    def test_the_frontier_line_wins_over_superseded_for_one_row(self):
        """ONE LINE PER ROW. A predecessor whose lane is gone and whose work
        landed is named by the frontier line, because that is the line whose
        command acts on it."""
        got = self.project([
            card("new", frontier="on-frontier", frontier_rung="lane-family"),
            card("old", frontier="landed-by-ancestry", frontier_rung="landing")],
            active=["new"])
        self.assertEqual({k: v["count"] for k, v in self.lines(got).items()},
                         {"off_frontier": 1})
        self.assertEqual([c["class"] for c in got["collapsed"]],
                         ["off_frontier"])

    def test_suc_owner_holds_and_edges_bill_only_listed_rows(self):
        got = self.project(
            [card("live", stalled=True, frontier="on-frontier",
                  frontier_rung="lane-family"),
             card("gone", stalled=True, holder_role="owner",
                  owner_gated=True, hold_ts="2033-05-18T03:32:50Z",
                  frontier="landed-by-ancestry", frontier_rung="landing")],
            stalled_ids=["live", "gone"])
        self.assertEqual(got["suc"]["stalled"], 1)
        self.assertEqual(got["owner_hold_count"], 0)
        waits = sorted(e["from"] for e in got["edges"]
                       if e["kind"] == "waits_on")
        self.assertEqual(waits, ["live"])
        self.assertEqual(got["active_count"], 2)

    def test_the_vocabulary_is_the_census_own(self):
        """A reason added to the census with no home here would read as
        on-frontier and stay listed; one renamed would stop collapsing. Both
        directions are pinned against the census constants themselves."""
        from helm import landreq
        for reason in landreq.OFF_FRONTIER_REASONS:
            self.assertEqual(scheduler.collapse_class(
                {"frontier": reason, "frontier_rung": "landing"}),
                "off_frontier", reason)
        for rung in landreq.FRONTIER_GONE_RUNGS:
            self.assertEqual(scheduler.collapse_class(
                {"frontier": landreq.OFF_FRONTIER_UNCLASSIFIED,
                 "frontier_rung": rung}), "unclassified", rung)
        self.assertIsNone(scheduler.collapse_class(
            {"frontier": landreq.OFF_FRONTIER_UNCLASSIFIED,
             "frontier_rung": "tip"}))
        self.assertIsNone(scheduler.collapse_class(
            {"frontier": landreq.ON_FRONTIER}))
        self.assertEqual(scheduler.collapse_class(
            {"frontier": landreq.ON_FRONTIER}, active=False), "superseded")

    def test_an_unavailable_model_claims_no_collapse(self):
        got = scheduler.project([card("a")], active_ids=None, now=NOW,
                                projection_age_s=0)
        self.assertIsNotNone(got["unavailable"])
        self.assertEqual(got["collapsed"], [])
        self.assertIsNone(got["collapsed_count"])
        self.assertIsNone(got["listed_count"])

    # -- task/2381 round 2: one rule on both surfaces -------------------------

    def test_on_main_with_no_verdict_is_one_line_never_a_listed_wait(self):
        """FINDING 3, RULED. The kanban folded a row whose work trunk holds
        with no verdict recorded; the waits never read the mark, so 8 of the
        integrator's 16 listed waits were the same rows the kanban counted on
        main. They are one line here too: count, oldest age, and the listing
        that marks each ALREADY ON TRUNK. THE CONTROLS in this projection: a
        PROVEN not-on-trunk row and an unasked one stay listed."""
        got = self.project([
            card("held-clean", state="AWAITING_REVIEW", owed_by="integrator",
                 trunk_contains_tip=True, frontier="unclassified",
                 frontier_rung="tip", dwell_s=86400 * 2),
            card("build-landed", state="AWAITING_BUILD", kind="build",
                 holder_role="builder", trunk_contains_tip=True,
                 frontier="on-frontier", frontier_rung="lane-family",
                 dwell_s=3600),
            card("loose", state="AWAITING_REVIEW", trunk_contains_tip=False,
                 frontier="on-frontier", frontier_rung="lane-family"),
            card("unasked", state="AWAITING_REVIEW", trunk_contains_tip=None)])
        self.assertEqual(self.listed(got), ["loose", "unasked"])
        line = self.lines(got)["on_main"]
        self.assertEqual(line["count"], 2)
        self.assertEqual(line["oldest_age_s"], 86400 * 2 + 10)
        self.assertEqual(line["command"], "helm lr list")
        self.assertEqual(line["label"], "on main with no verdict recorded")
        self.assertEqual(got["listed_count"] + got["collapsed_count"],
                         got["row_count"])

    def test_a_row_on_main_under_a_recorded_verdict_stays_listed(self):
        """MEASURED on the live trunk board: a verdicted row whose own git leg
        came back unobserved is measured for containment too, and seven such
        rows — APPROVE, CONCUR and two FIX owed by their author — carried the
        mark. A FIX whose tip is on main is a contradiction somebody owes; it
        is never "no verdict recorded". THE CONTROL: the unverdicted row on
        the same mark folds in this same projection."""
        got = self.project([
            card("fix", state="CHANGES_REQUESTED", polarity="fix",
                 holder_role="author", holder_seat="s",
                 trunk_contains_tip=True),
            card("approve", state="REVIEWED", polarity="approve",
                 holder_role="unknown (declared verdict held)",
                 trunk_contains_tip=True),
            card("none", state="AWAITING_REVIEW", trunk_contains_tip=True)])
        self.assertEqual(self.listed(got), ["approve", "fix"])
        self.assertEqual({k: v["count"] for k, v in self.lines(got).items()},
                         {"on_main": 1})

    def test_an_owner_gated_row_on_main_stays_on_the_owners_queue(self):
        """An owner-gated hold is a decision only the owner can make, and the
        landing does not make it: folding it would take an ask off the one
        queue whose whole job is to show him what he owes (`owner_holds`
        counts listed rows). THE CONTROL: an ungated row on the same mark
        folds."""
        got = self.project([
            card("gated", state="AWAITING_REVIEW", holder_role="owner",
                 owner_gated=True, hold_ts="2033-05-18T03:30:00Z",
                 trunk_contains_tip=True),
            card("plain", state="AWAITING_REVIEW", trunk_contains_tip=True)])
        self.assertEqual(self.listed(got), ["gated"])
        self.assertEqual(got["owner_hold_count"], 1)
        self.assertEqual(self.lines(got)["on_main"]["count"], 1)

    def test_one_line_per_row_with_on_main_between_frontier_and_superseded(self):
        """A row the census placed off the frontier is named by the frontier
        line, whose command acts on it; a round a later round absorbed is
        named by the superseded line, because `helm lr list` — the on-main
        line's command — prints the chain frontier only."""
        got = self.project([
            card("live", trunk_contains_tip=False),
            card("left", trunk_contains_tip=True,
                 frontier="landed-by-ancestry", frontier_rung="landing"),
            card("absorbed", trunk_contains_tip=True),
            card("on-main", trunk_contains_tip=True)],
            active=["live", "left", "on-main"])
        self.assertEqual(self.listed(got), ["live"])
        self.assertEqual([(c["class"], c["count"]) for c in got["collapsed"]],
                         [("off_frontier", 1), ("on_main", 1),
                          ("superseded", 1)])

    def test_the_on_main_line_speaks_the_kanbans_words(self):  # noqa: VACUOUS_ASSERTION — every pre-verdict state is asserted True by identity on the same predicate before the non-True marks are asserted False
        """ONE LINE, ONE WORDING: the kanban's on-main line is this model's
        own line (`web_board._kanban_split` reads it), so the label and the
        command are pinned once, here."""
        from helm import landreq
        classes = [klass for klass, _label, _cmd in scheduler.COLLAPSE_LINES]
        self.assertEqual(classes, ["off_frontier", "unclassified", "on_main",
                                   "superseded"])
        self.assertEqual(scheduler.collapse_class(
            {"state": "AWAITING_REVIEW", "trunk_contains_tip": True}),
            "on_main")
        for state in landreq.PRE_VERDICT_STATES:
            self.assertIs(landreq.on_main_unverdicted(
                {"state": state, "trunk_contains_tip": True}), True, state)
        for mark in (False, None, "yes", 1):
            self.assertIs(landreq.on_main_unverdicted(
                {"state": "AWAITING_REVIEW", "trunk_contains_tip": mark}),
                False, mark)


class OwnerAskReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp.name, "helm"),
            "HELM_BOARD": os.path.join(self.tmp.name, "board.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_absent_is_empty_but_unreadable_and_alien_are_unknown(self):
        self.assertEqual(scheduler.read_owner_asks(), ([], None))
        os.symlink(os.path.join(self.tmp.name, "missing-target"),
                   os.environ["HELM_BOARD"])
        self.assertIn("unreadable", scheduler.read_owner_asks()[1])
        os.unlink(os.environ["HELM_BOARD"])
        with open(os.environ["HELM_BOARD"], "w") as stream:
            stream.write("not json")
        self.assertIn("unreadable", scheduler.read_owner_asks()[1])
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": "none"})
        self.assertIn("not a list", scheduler.read_owner_asks()[1])

    def test_stat_failure_and_blank_actionable_row_are_unknown(self):
        with mock.patch.object(scheduler.os, "lstat", side_effect=PermissionError):
            rows, error = scheduler.read_owner_asks()
        self.assertEqual(rows, [])
        self.assertIn("cannot be inspected", error)
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": [
            {"ask": "", "why": "still blocks", "state": "waiting-owner",
             "since": "2026-08-04T19:15Z"}]})
        rows, error = scheduler.read_owner_asks()
        self.assertEqual(rows, [])
        self.assertIn("malformed actionable row", error)

    def test_structured_actionable_fields_are_malformed_not_stringified(self):
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": [
            {"ask": "choose", "why": "because", "state": "waiting-owner",
             "hold_kind": "owner-input", "since": "2026-08-04T19:15Z"}]})
        healthy, error = scheduler.read_owner_asks()
        self.assertIsNone(error)
        self.assertEqual([row["ask"] for row in healthy], ["choose"])
        for field, value in (("ask", ["choose"]), ("why", {"because": "x"}),
                             ("since", ["2026-08-04"]),
                             ("state", ["waiting-owner"])):
            with self.subTest(field=field):
                row = {"ask": "choose", "why": "because",
                       "state": "waiting-owner", "hold_kind": "owner-input",
                       "since": "2026-08-04T19:15Z"}
                row[field] = value
                pk.write_json(os.environ["HELM_BOARD"],
                              {"owner_gated_queue": [row]})
                rows, error = scheduler.read_owner_asks()
                self.assertEqual(rows, [])
                self.assertIn("malformed actionable row", error)

    def test_only_actionable_rows_survive_and_oldest_is_first(self):
        pk.write_json(os.environ["HELM_BOARD"], {"owner_gated_queue": [
            {"ask": "later", "why": "choose", "state": "waiting-owner-decision",
             "hold_kind": "decision", "since": "2026-08-26T09:14:38Z"},
            {"ask": "earlier", "why": "login", "state": "waiting-owner",
             "hold_kind": "owner-input", "since": "2026-08-04T19:15Z"},
            {"ask": "fyi", "state": "fyi", "since": "2020-01-01"},
        ]})
        rows, err = scheduler.read_owner_asks()
        self.assertIsNone(err)
        self.assertEqual([r["ask"] for r in rows], ["earlier", "later"])


if __name__ == "__main__":
    unittest.main()
