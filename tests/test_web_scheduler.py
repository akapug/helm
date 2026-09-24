"""Scheduler tab assembly and real JavaScript renderer runtime contracts."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import dispatches, landreq, scheduler, web, web_land, web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_web_lr as _web_lr


HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "scheduler_runtime_harness.js")


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
        self.assertEqual(graphed, {parent["id"], child["id"]})
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


class SchedulerAssemblyTest(unittest.TestCase):
    def test_the_scheduler_is_a_fold_of_the_work_page(self):
        """The scheduler was its own Fleet tab; the owner merged it into the
        Work page, where it folds under the land pipeline's kanban. A #scheduler
        bookmark lands on Work and opens that fold."""
        src = web_ui_loader.read_text()
        self.assertNotIn('data-v="scheduler"', src)
        self.assertNotIn('id="view-scheduler"', src)
        work = src[src.index('id="view-work"'):src.index('id="view-quota"')]
        self.assertIn('<details class="grp" id="schedfold">', work)
        self.assertIn('<section id="schedulersec">', work)
        self.assertIn("function schedulerHTML", src)
        self.assertIn(".schgraph", src)
        self.assertIn(".schgroupsuc.alarm", src)
        self.assertIn('scheduler: "work"', _extract_fn(src, "canonView"))
        self.assertIn('scheduler: "schedfold"', _extract_fn(src, "goOldSection"))

    def test_lrShow_feeds_scheduler_from_the_same_answer(self):  # noqa: VACUOUS_ASSERTION — exact one-call count is the positive control; fetch absence proves the same answer is reused rather than fetched again
        body = _extract_fn(web_ui_loader.read_text(), "lrShow")
        self.assertIn("lrNav(d)", body)  # control: this is the live LR consumer
        self.assertIn("schedulerShow(d)", body)
        self.assertEqual(body.count("schedulerShow(d)"), 1)
        self.assertNotIn("fetch", _extract_fn(web_ui_loader.read_text(),
                                               "schedulerShow"))

    def test_home_owner_strip_reads_known_asks_before_pipeline_unknown(self):
        body = _extract_fn(web_ui_loader.read_text(), "dashOwner")
        self.assertLess(body.index("const model = d.scheduler"),
                        body.index("if (!board || model.unavailable)"))
        self.assertIn("OWNER ASK", body)
        self.assertIn("model.owner_holds", body)
        self.assertIn("owner_asks_dropped", body)
        self.assertNotIn("unmeasurable", body)
        self.assertNotIn("/\\bheld\\b/i", body)
        self.assertIn('showView("work"); goOldSection("scheduler")', body)

    def test_renderer_has_no_second_endpoint_or_client_side_holder_grouping(self):
        src = web_ui_loader.read_text()
        body = _extract_fn(src, "schedulerHTML")
        self.assertNotIn("/api/", body)
        self.assertNotIn("holder_role", body)
        self.assertIn("model.groups", body)
        row = _extract_fn(src, "schedulerRowHTML")
        ask = _extract_fn(src, "schedulerAskHTML")
        self.assertIn('e.kind === "waits_on"', row)
        self.assertIn('e.kind === "supersedes"', row)
        self.assertNotIn("row.supersedes", row)
        self.assertIn("waits_on_owner_", ask)
        poll = _extract_fn(src, "pollLr")
        self.assertEqual(poll.count('j("/api/lr"'), 1)


class SchedulerRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        # `cardStale` and `cardSource` are REAL here because the owner strip is
        # one of the home cards that must name its read and withhold an expired
        # value; stubbing them would let this harness render a strip the page
        # cannot produce.
        names = ("schedulerSUC", "schedulerEdgeMap", "schedulerAskHTML",
                 "schedulerRowHTML", "schedulerAsksHTML", "schedulerHTML",
                 "cardBoundS", "cardStale", "cardSource", "dashOwner")
        functions = "\n\n".join(_extract_fn(src, name) for name in names)
        with open(HARNESS, encoding="utf-8") as stream:
            script = stream.read().replace("/*__INJECT__*/", functions)
        cls.tmp = tempfile.TemporaryDirectory(prefix="helm-scheduler-runtime-")
        cls.addClassCleanup(cls.tmp.cleanup)
        path = os.path.join(cls.tmp.name, "run.js")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(script)
        checked = subprocess.run([node, "--check", path], capture_output=True,
                                 text=True)
        if checked.returncode:
            raise AssertionError("node --check failed:\n" + checked.stderr)
        run = subprocess.run([node, path], capture_output=True, text=True,
                             timeout=60)
        if run.returncode:
            raise AssertionError("scheduler harness failed:\n" + run.stderr)
        cls.detail = json.loads(run.stdout)[0]["detail"]

    def test_real_renderer_groups_edges_ages_suc_owner_first_and_freshness(self):  # noqa: VACUOUS_ASSERTION — literal non-empty key tuple plus hasAllGroups membership binds the loop to the real harness result
        self.assertIn("hasAllGroups", self.detail)  # control: harness returned shape
        for key in ("hasAllGroups", "ownerBeforeGraph", "hasSUC",
                    "hasBlocking", "hasSupersession", "hasAges",
                    "hasFreshness", "hasListDoor", "usageResetEdge",
                    "ordinaryOwnerEdge"):
            self.assertTrue(self.detail[key], key)

    def test_real_renderer_keeps_unknown_and_measured_empty_distinct(self):  # noqa: VACUOUS_ASSERTION — unavailableLoud key membership is the structural control; each following boolean is a distinct rendered-state observation
        self.assertIn("unavailableLoud", self.detail)  # control: harness returned shape
        self.assertTrue(self.detail["unavailableLoud"])
        self.assertTrue(self.detail["unavailableKeepsAsk"])
        self.assertTrue(self.detail["missingLoud"])
        self.assertTrue(self.detail["emptyMeasured"])
        self.assertTrue(self.detail["ownerEmptyUnreadableLoud"])
        self.assertTrue(self.detail["ownerKnownUnreadableVisible"])
        self.assertTrue(self.detail["ownerCapDisclosed"])
        self.assertTrue(self.detail["allCappedDisclosed"])
        self.assertTrue(self.detail["canonicalOwnerHold"])


if __name__ == "__main__":
    unittest.main()
