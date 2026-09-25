"""Holder standing must reach the renderer and the projection's read witness."""
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from helm import landreq, pk, seats_claims, seats_lineage, seats_roster
from helm import web, web_cache, web_land_model as model  # noqa: F401
from tests.test_seat_lineage import Base


def row():
    return dict(id="review-only-row", state="OPEN", lane="review-only-lane",
                branch="refs/heads/review-only-lane", review_sha="a" * 40,
                author="alpha", reviewer="beta", owed_by="author", dwell_s=0,
                stalled=False, terminal=False, landed=False, merged_local=False,
                observable=True)


class BoardProjectionTest(Base):
    def render(self, **cases):
        from tests.test_web_lr import CardRuntimeBase
        # A PRIVATE SUBCLASS carries the harness's class fixtures, so
        # CardRuntimeBase itself -- the base every card class in test_web_lr
        # inherits -- never keeps this module's tmp dir and node path after
        # it (task/3039: the slice runner's data audit named them).
        harness = type("BoardRenderHarness", (CardRuntimeBase,), {})
        harness.setUpClass()
        self.addCleanup(harness.tearDownClass)
        return harness().render(**cases)

    def test_actual_browser_renderer_shows_orphan_standing(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        live = landreq.card(row())
        self.plant({"beta": {"session": "s-beta"}})
        orphan = landreq.card(row())
        self.assertIsNone(live["owed_seat_standing"])
        self.assertEqual(orphan["owed_seat_standing"]["state"], "ORPHANED")
        rendered = self.render(live={"__row": live}, orphan={"__row": orphan},
                               positive={"__row": dict(live, stalled=True)})
        self.assertIn("STALLED", rendered["positive"]["html"])
        self.assertNotIn("ORPHANED", rendered["live"]["html"])
        self.assertIn("ORPHANED", rendered["orphan"]["html"])

    def test_roster_only_change_ages_the_restored_holder(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        with model._lr_snapshot() as reads:
            model._lr_newest_mtime()
            body = {"read_ts": time.time(), "loops": [landreq.card(row())]}
            witness = reads.witness()
        self.assertTrue(witness)
        key = "lineage-roster"
        web_cache._persist_store(key, time.time(), body, witness)
        with model._lr_snapshot() as reads:
            unchanged = web_cache.persist_load(key, 30, reads)
        self.assertIsNotNone(unchanged)
        self.assertLess(time.time() - unchanged[0], 30)
        self.assertEqual(unchanged[1], body)
        self.plant({"beta": {"session": "s-beta",
                             "seat_keys": [self.key("alpha")]}})
        self.assertEqual(landreq.card(row())["holder_seat"], "beta")
        with model._lr_snapshot() as reads:
            changed = web_cache.persist_load(key, 30, reads)
        self.assertIsNotNone(changed)
        self.assertEqual(changed[1]["loops"][0]["holder_seat"], "alpha")
        self.assertGreaterEqual(time.time() - changed[0], 30)

    def test_recycled_name_does_not_restore_a_departed_successor(self):
        self.plant({"beta": {"session": "old-session",
                             "seat_keys": [self.key("alpha")]}})
        with model._lr_snapshot() as reads:
            model._lr_newest_mtime()
            body = {"read_ts": time.time(), "loops": [landreq.card(row())]}
            witness = reads.witness()
        self.assertEqual(body["loops"][0]["holder_seat"], "beta")
        self.assertTrue(witness)
        key = "lineage-recycled"
        web_cache._persist_store(key, time.time(), body, witness)
        with model._lr_snapshot() as reads:
            unchanged = web_cache.persist_load(key, 30, reads)
        self.assertEqual(unchanged[1], body)
        self.assertLess(time.time() - unchanged[0], 30)
        self.plant({"alpha": {"session": "new-session"}})
        self.assertEqual(landreq.card(row())["holder_seat"], "alpha")
        with model._lr_snapshot() as reads:
            restored = web_cache.persist_load(key, 30, reads)
        self.assertEqual(restored[1]["loops"][0]["holder_seat"], "beta")
        self.assertGreaterEqual(time.time() - restored[0], 30)

    def test_one_snapshot_keeps_one_answer_for_the_same_holder(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        with model._lr_snapshot():
            first = landreq.card(row())
            self.plant({"beta": {"session": "s-beta",
                                 "seat_keys": [self.key("alpha")]}})
            second = landreq.card(row())
        self.assertEqual(first["holder_seat"], "alpha")
        self.assertEqual(second["holder_seat"], first["holder_seat"])
        self.assertEqual(landreq.card(row())["holder_seat"], "beta")

    def test_snapshot_is_nested_thread_local_and_restores_after_exception(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        with self.assertRaisesRegex(RuntimeError, "review unwind"):
            with model._lr_snapshot() as outer:
                self.assertEqual(landreq.card(row())["holder_seat"], "alpha")
                self.plant({"beta": {"session": "s-beta",
                                     "seat_keys": [self.key("alpha")]}})
                with model._lr_snapshot() as inner:
                    self.assertIs(inner, outer)
                    self.assertEqual(landreq.card(row())["holder_seat"], "alpha")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    current = pool.submit(lambda: landreq.card(row())["holder_seat"])
                    self.assertEqual(current.result(timeout=2), "beta")
                raise RuntimeError("review unwind")
        self.assertEqual(landreq.card(row())["holder_seat"], "beta")

    def test_snapshot_restores_the_previous_lens(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        prior = ("prior", "CURRENT", "outer lens")
        with seats_lineage.lineage_lens(lambda seat: prior):
            self.assertEqual(seats_lineage.seat_lineage("alpha"), prior)
            with model._lr_snapshot():
                self.assertEqual(seats_lineage.seat_lineage("alpha")[0], "alpha")
            self.assertEqual(seats_lineage.seat_lineage("alpha"), prior)
        self.assertEqual(seats_lineage.seat_lineage("alpha")[0], "alpha")

    def test_unknown_lineage_blinds_persistence_not_the_card(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        with model._lr_snapshot() as reads:
            model._lr_newest_mtime()
            self.assertEqual(landreq.card(row())["holder_seat"], "alpha")
            self.assertTrue(reads.witness())
        self.plant({"beta": {"session": "s-beta", "seat_keys": 7}})
        with model._lr_snapshot() as reads:
            model._lr_newest_mtime()
            current = landreq.card(row())
            witness = reads.witness()
        self.assertIsNone(current["owed_seat_standing"])
        self.assertEqual(current["holder_seat"], "alpha")
        self.assertIsNone(witness)

    def test_browser_known_states_are_visible_and_escaped(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        current = landreq.card(row())
        cases = {state: {"__row": dict(current, owed_seat_standing={
            "state": state, "why": "<script>review</script>"})}
            for state in ("RENAMED", "ORPHANED", "UNKNOWN", "FUTURE")}
        rendered = self.render(**cases)
        for state in ("RENAMED", "ORPHANED"):
            html = rendered[state]["html"]
            self.assertIn(state, html)
            self.assertIn("&lt;script&gt;review&lt;/script&gt;", html)
            self.assertNotIn("<script>", html)
        self.assertIn("NOT expired", rendered["ORPHANED"]["html"])
        self.assertEqual(rendered["UNKNOWN"]["html"], rendered["FUTURE"]["html"])
        self.assertNotIn("ORPHANED", rendered["UNKNOWN"]["html"])

    def test_pre_lineage_persisted_schema_is_not_restored(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        with model._lr_snapshot() as reads:
            model._lr_newest_mtime()
            body = {"read_ts": time.time(), "loops": [landreq.card(row())]}
            witness = reads.witness()
        key = "lineage-schema"
        web_cache._persist_store(key, time.time(), body, witness)
        with model._lr_snapshot() as reads:
            self.assertEqual(web_cache.persist_load(key, 30, reads)[1], body)
        with mock.patch.object(web_cache, "_PERSIST_SCHEMA", 2):
            web_cache._persist_store(key, time.time(), body, witness)
        with model._lr_snapshot() as reads:
            self.assertIsNone(web_cache.persist_load(key, 30, reads))

    def test_real_rename_carries_a_real_lease(self):
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        with mock.patch.dict("os.environ", {"HELM_CHAT_NAME": "alpha"}):
            ok, msg, _lease = seats_claims.claim("lineage-resource", "alpha")
            self.assertTrue(ok, msg)
            before = pk.read_json(seats_claims.claims_path(), {})["lineage-resource"]
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        after = pk.read_json(seats_claims.claims_path(), {})["lineage-resource"]
        self.assertEqual(after["holder"], "beta", msg)
        for field in ("lease", "fence", "exp_mono"):
            self.assertEqual(after[field], before[field], field)
        self.assertIn("Carried 1 open holding", msg)
        self.assertEqual(seats_lineage.seat_lineage("alpha")[:2], ("beta", "RENAMED"))
