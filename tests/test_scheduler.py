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
    def test_every_nonterminal_row_appears_once_and_terminal_never_appears(self):
        rows = [card("a"), card("b", honored=True, holder_role="nobody"),
                card("closed", terminal=True)]
        got = scheduler.project(rows, active_ids=["a"], now=NOW,
                                projection_age_s=5)
        shown = [row["id"] for group in got["groups"] for row in group["rows"]]
        self.assertEqual(sorted(shown), ["a", "b"])
        self.assertEqual(got["row_count"], 2)
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
        self.assertEqual(by["nobody"]["total"], 0)

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
        self.assertFalse(projected["old"]["active"])
        self.assertEqual(waits, ["new"])
        self.assertEqual(got["suc"]["stalled"], 0)

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
