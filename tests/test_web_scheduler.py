"""The scheduler model /api/lr carries: who waits on whom, the owner asks
and holds, read per response and never a healthy zero."""
import unittest
from unittest import mock

from helm import dispatches, landreq, scheduler, web, web_land
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_web_lr as _web_lr


class SchedulerApiTest(_web_lr.LrApiBase):
    def test_api_adds_graph_from_same_projection_and_hides_private_rows(self):
        parent = self.dispatch(lane="lane/old")
        child = self.dispatch(lane="lane/new", supersedes=parent["id"])
        ask = {"ask": "relogin", "why": "device auth",
               "state": "waiting-owner", "hold_kind": "owner-input",
               "since": "2026-08-04T19:15Z"}
        original = landreq.project_raw
        with mock.patch.object(landreq, "project_raw", wraps=original) as project_raw, \
                mock.patch.object(scheduler, "read_owner_asks",
                                  return_value=([ask], None)):
            body = self.lr()
        self.assertEqual(project_raw.call_count, 1)
        self.assertNotIn("_scheduler_rows", body)
        self.assertNotIn("_scheduler_active_ids", body)
        self.assertEqual([row["id"] for row in body["loops"]], [child["id"]])
        graphed = {row["id"] for group in body["scheduler"]["groups"]
                   for row in group["rows"]}
        # THE ABSORBED PARENT OWES NOBODY A MOVE: counted on the superseded
        # line of the same model, never drawn as a wait (task/2381)
        self.assertEqual(graphed, {child["id"]})
        self.assertEqual([(c["class"], c["count"])
                          for c in body["scheduler"]["collapsed"]],
                         [("superseded", 1)])
        self.assertEqual(body["scheduler"]["row_count"], 2)
        self.assertEqual(body["scheduler"]["owner_asks"][0]["plain_title"],
                         "relogin")

    def test_owner_gated_lr_uses_authoritative_owner_holder_and_edge(self):
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-owner")
        dispatches.mark_hold(row["id"], "choose publication", owner_gated=True)
        body = self.lr()
        projected = {r["id"]: r for g in body["scheduler"]["groups"]
                     for r in g["rows"]}
        owner = projected[row["id"]]
        self.assertEqual(owner["holder_role"], "owner")
        self.assertEqual(owner["detail"], "choose publication")
        self.assertIn(row["id"], [e["from"] for e in body["scheduler"]["edges"]
                                  if e["kind"] == "waits_on"])
        self.assertEqual(body["scheduler"]["owner_hold_count"], 1)

    def test_owner_asks_are_read_per_response_not_frozen_in_warm_body(self):
        self.dispatch()
        first = {"ask": "first", "state": "waiting-owner",
                 "hold_kind": "owner-input", "since": "2020-01-01"}
        second = {"ask": "second", "state": "waiting-owner",
                  "hold_kind": "owner-input", "since": "2020-01-02"}
        with mock.patch.object(scheduler, "read_owner_asks",
                              side_effect=[([first], None), ([second], None)]):
            one = self.lr()
            two, status = web.QUERY_API["/api/lr"]({})
        self.assertEqual(status, 200)
        self.assertEqual(one["scheduler"]["owner_asks"][0]["plain_title"], "first")
        self.assertEqual(two["scheduler"]["owner_asks"][0]["plain_title"], "second")

    def test_malformed_or_partial_active_membership_is_unknown_not_zero(self):
        row = {"id": "a", "terminal": False, "state": "OPEN", "lane": "a",
               "owed_by": "integrator", "holder_role": "integrator",
               "holder_seat": None, "dwell_s": 1, "dwell_known": True,
               "honored": False}
        base = {"read_ts": 1, "ledger_mtime": 1, "_scheduler_rows": [row],
                "loops": [row], "stalled_ids": [], "unmeasurable": []}
        with mock.patch.object(web_land, "_cached_swr", return_value=dict(base)):
            missing, _status = web_land._api_lr({})
        self.assertIn("predates scheduler membership",
                      missing["scheduler"]["unavailable"])
        partial = dict(base, _scheduler_active_ids=[])
        with mock.patch.object(web_land, "_cached_swr", return_value=partial):
            mismatched, _status = web_land._api_lr({})
        self.assertIn("disagrees", mismatched["scheduler"]["unavailable"])
        self.assertIsNone(mismatched["scheduler"]["active_count"])
        self.assertIsNone(mismatched["scheduler"]["suc"]["zero"])

    def test_response_clock_is_sampled_after_warm_body_and_owner_ask_reads(self):
        body = {"read_ts": 100, "ledger_mtime": 90, "_scheduler_rows": [],
                "_scheduler_active_ids": [], "loops": [], "stalled_ids": [],
                "unmeasurable": []}
        ask = {"ask": "choose", "why": "decision",
               "state": "waiting-owner", "hold_kind": "owner-input",
               "since": "1970-01-01T00:02:30Z"}
        with mock.patch.object(web_land, "_cached_swr", return_value=body), \
                mock.patch.object(scheduler, "read_owner_asks",
                                  return_value=([ask], None)), \
                mock.patch.object(web_land.time, "time", side_effect=[100, 160]):
            got, _status = web_land._api_lr({})
        self.assertEqual(got["read_age_s"], 60)
        self.assertEqual(got["ledger_age_s"], 70)
        self.assertEqual(got["scheduler"]["owner_asks"][0]["age_s"], 10)

    def test_production_caps_are_reachable_and_disclose_every_drop(self):
        rows = [{"id": "r%d" % i, "terminal": False, "state": "OPEN",
                 "lane": "row %d" % i, "owed_by": "integrator",
                 "holder_role": "integrator", "holder_seat": None,
                 "dwell_s": i, "dwell_known": True, "honored": False}
                for i in range(web_land._SCHEDULER_GROUP_LIMIT + 1)]
        asks = [{"ask": "ask %d" % i, "why": "human input",
                 "state": "waiting-owner", "hold_kind": "owner-input",
                 "since": "2020-01-01"}
                for i in range(web_land._SCHEDULER_OWNER_LIMIT + 1)]
        body = {"read_ts": 1, "ledger_mtime": 1,
                "_scheduler_rows": rows,
                "_scheduler_active_ids": [row["id"] for row in rows],
                "loops": rows, "stalled_ids": [], "unmeasurable": []}
        with mock.patch.object(web_land, "_cached_swr", return_value=body), \
                mock.patch.object(scheduler, "read_owner_asks",
                                  return_value=(asks, None)):
            got, _status = web_land._api_lr({})
        model = got["scheduler"]
        self.assertEqual(len(model["groups"][0]["rows"]),
                         web_land._SCHEDULER_GROUP_LIMIT)
        self.assertEqual(model["groups"][0]["dropped"], 1)
        self.assertEqual(len(model["owner_asks"]),
                         web_land._SCHEDULER_OWNER_LIMIT)
        self.assertEqual(model["owner_asks_dropped"], 1)
        self.assertEqual(model["owner_ask_count"], len(asks))

    def test_warming_and_unavailable_scheduler_never_report_healthy_zero(self):
        with mock.patch.object(web_land, "_cached_swr",
                               return_value={"warming": True}):
            warming, status = web_land._api_lr({})
        self.assertEqual(status, 200)
        self.assertEqual(warming["scheduler"]["unavailable"],
                         "pipeline is warming")
        self.assertIsNone(warming["scheduler"]["suc"]["total"])
        reason = "ledger refused"
        with mock.patch.object(web_land, "_cached_swr", return_value={
                "read_ts": 1, "ledger_mtime": None, "unavailable": reason,
                "loops": [], "stalled_ids": [], "unmeasurable": []}):
            failed, _status = web_land._api_lr({})
        self.assertEqual(failed["scheduler"]["unavailable"], reason)
        self.assertIsNone(failed["scheduler"]["row_count"])


# THE PAGE'S SCHEDULER WENT WITH THE PIPELINE PAGE (task/3643). "Who waits
# on whom" is the Work page's List lens grouped by who has it, each card's
# whose move read from /api/work (tests/test_web_work_page.py,
# LensesTest.test_the_list_is_the_backlogs_rows_with_stage_and_whose_move);
# its fold, its renderer and its node harness were retired with it. The
# server's scheduler model on /api/lr, which the owner asks and holds still
# ride, is the class above.

if __name__ == "__main__":
    unittest.main()
