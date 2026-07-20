"""helm coach — the capture front door with the 4-step GATE.

Hermetic: HELM_HOME + HELM_ADOPTED_DIR point at fresh tmp dirs so the router,
the dedupe search, and every landing path run against an isolated store (never
the real ~/.claude corpus). Attestation is native + offline; the OPTIONAL
dregg anchor is stubbed at cell.anchor_submit so --apply never touches a node.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-coach-home-"))
os.environ.setdefault("HELM_ADOPTED_DIR", tempfile.mkdtemp(prefix="helm-coach-adopt-"))

from helm import coach, home, pk, store  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-coach-")
        self.prev = {k: os.environ.get(k)
                     for k in ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_NODE_URL")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_NODE_URL"] = "http://127.0.0.1:1"  # dead: anchor fails open
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        home.scaffold_global()

    def tearDown(self):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = coach.cmd_coach(argv)
        return rc, out.getvalue()


class RouterTest(_Base):
    """PLACE — deterministic shape rules, no model call."""

    def test_certain_truth_routes_premise(self):
        r = coach.route("always scrub planning docs before any push")
        self.assertEqual(r["layer"], "premise")
        self.assertGreaterEqual(r["confidence"], coach.APPLY_MIN)

    def test_hedged_routes_prior_with_scaled_confidence(self):
        strong = coach.route("the prefilter might be faster than the regex")
        self.assertEqual(strong["layer"], "prior")
        self.assertEqual(strong["extra"]["belief_conf"], 0.5)   # strong hedge
        soft = coach.route("the resolver usually beats the naive scan")
        self.assertEqual(soft["extra"]["belief_conf"], 0.7)     # soft hedge

    def test_definition_routes_lexicon(self):
        for text in ("quorumward = the load-bearing word",
                     "quorumward means the load-bearing word",
                     "define quorumward as the load-bearing word"):
            r = coach.route(text)
            self.assertEqual(r["layer"], "lexicon", text)
            self.assertEqual(r["id"], "quorumward")
            self.assertTrue(r["extra"]["definition"])

    def test_bare_colon_is_not_a_definition(self):
        # a colon alone is too ambiguous to route to lexicon (anti-misfile)
        r = coach.route("correction: capture it durably or it is lost")
        self.assertNotEqual(r["layer"], "lexicon")

    def test_fires_unbidden_routes_reflex_prompt_pick(self):
        r = coach.route("when a compaction marker appears, re-read the now snapshot")
        self.assertEqual(r["layer"], "reflex")
        self.assertEqual(r["extra"]["signal"], "prompt")
        self.assertIn("compaction", r["extra"]["pattern"])
        self.assertIn("re-read the now snapshot", r["extra"]["steer"])

    def test_reflex_marker_pick_on_concrete_path(self):
        r = coach.route("whenever ~/.afk-flag exists, batch owner surfaces")
        self.assertEqual(r["layer"], "reflex")
        self.assertEqual(r["extra"]["signal"], "marker-file")
        self.assertEqual(r["extra"]["marker"], "~/.afk-flag")

    def test_every_turn_reflex(self):
        r = coach.route("every turn, prefer data structures over control flow")
        self.assertEqual(r["layer"], "reflex")
        self.assertEqual(r["extra"]["signal"], "every-turn")

    def test_procedure_routes_skill_pointer(self):
        for text in ("how to prepare a home: run prepare then verify then archive",
                     "the steps: 1. prepare 2. verify 3. archive"):
            self.assertEqual(coach.route(text)["layer"], "skill", text)

    def test_default_belief_is_low_confidence(self):
        r = coach.route("meld send deposits into the profile whisper slots")
        self.assertEqual(r["layer"], "prior")
        self.assertLess(r["confidence"], coach.APPLY_MIN)   # -> intake on apply

    def test_as_layer_override(self):
        r = coach.route("a plain statement", as_layer="heuristic")
        self.assertEqual(r["layer"], "heuristic")
        self.assertGreaterEqual(r["confidence"], coach.APPLY_MIN)

    def test_id_override(self):
        r = coach.route("always X", id_override="my-explicit-id")
        self.assertEqual(r["id"], "my-explicit-id")

    def test_empty_is_fail_open(self):
        r = coach.route("   ")
        self.assertEqual(r["confidence"], 0.0)


class SearchTest(_Base):
    """NO-CRUFT — resolve + fuzzy search including retired/tombstoned."""

    def _seed(self):
        store.cmd_store(["add", "prior",
                         "scrub-before-push | scrub internal planning docs before push | 0.8"])
        store.cmd_store(["add", "prior",
                         "old-scrub | scrub the planning docs before any git push | 0.7"])
        e, _ = store.retire("old-scrub", pk.now_ts(), "coach-test")
        self.assertEqual(e["status"], store.STATUS_RETIRED)

    def test_near_search_includes_retired(self):
        self._seed()
        near = coach.near_matches(
            "scrub internal planning docs before every push", "prior")
        ids = {str(e["id"]) for e, _ov in near}
        self.assertIn("scrub-before-push", ids)   # live near-dup
        self.assertIn("old-scrub", ids)           # RETIRED still surfaces

    def test_plan_flags_subsumable_live_entry(self):
        self._seed()
        p = coach.plan("scrub internal planning docs before push")
        subsumed = {str(e["id"]) for e, _ in p["subsumes"]}
        self.assertIn("scrub-before-push", subsumed)   # >= SUBSUME, live, same type
        self.assertNotIn("old-scrub", subsumed)        # retired never "could retire"

    def test_empty_store_search_is_clean(self):
        self.assertEqual(coach.near_matches("brand new topic nobody stored", "prior"), [])


class LandingVerbTest(_Base):
    def test_verbs_per_layer(self):
        self.assertIn('helm premise "',
                      coach.landing_verb(coach.route("always never edit prod")))
        self.assertIn('helm store add lexicon "',
                      coach.landing_verb(coach.route("foo = bar baz")))
        self.assertIn('helm store add prior "',
                      coach.landing_verb(coach.route("probably true thing")))
        rf = coach.landing_verb(coach.route("when x happens, do y quickly"))
        self.assertIn("helm reflex add ", rf)
        self.assertIn("--signal prompt", rf)
        self.assertIn("procedure ->",
                      coach.landing_verb(coach.route("how to do the thing steps")))

    def test_project_flag_threads_through(self):
        v = coach.landing_verb(coach.route("probably true"), project="myproj")
        self.assertIn("--project myproj", v)


class ApplyTest(_Base):
    def test_apply_prior_lands_live_entry(self):
        r = coach.plan("the prefilter might be faster than the regex scan")
        _rc, outcome = coach.apply(r)
        self.assertEqual(outcome, "new")
        got = store._find(r["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], store.STATUS_LIVE)
        self.assertEqual(got["type"], "prior")
        self.assertEqual(got["confidence"], 0.5)   # strong-hedge belief_conf

    def test_apply_lexicon_lands(self):
        r = coach.plan("frobnicate = to tweak needlessly")
        coach.apply(r)
        got = store._find("frobnicate", types=("lexicon",))
        self.assertIsNotNone(got)
        self.assertEqual(got["type"], "lexicon")

    def test_apply_reflex_lands(self):
        from helm import reflex
        r = coach.plan("when the deploy breaks, roll back before debugging")
        coach.apply(r)
        ids = {e["id"] for e in reflex.load_all()}
        self.assertIn(r["id"], ids)

    def test_apply_premise_lands_certain(self):
        r = coach.plan("never commit secrets to any remote, no exceptions")
        self.assertEqual(r["layer"], "premise")
        with mock.patch("helm.cell.anchor_submit", return_value=(None, "hermetic")):
            _rc, outcome = coach.apply(r)
        self.assertEqual(outcome, "new")
        got = store._find(r["id"], types=("prior",))
        self.assertEqual(got["class"], "certain")
        self.assertEqual(got["confidence"], 1.0)

    def test_low_confidence_drops_to_intake_lossless(self):
        r = coach.plan("meld send deposits into the profile whisper slots")
        self.assertLess(r["confidence"], coach.APPLY_MIN)
        path, outcome = coach.apply(r)
        self.assertEqual(outcome, "intake")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.basename(path).startswith("feedback-"))
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn("type: feedback", raw)
        self.assertIn("source: coach", raw)
        # it lands in the drain intake dir, so drain can route it later
        self.assertTrue(path.startswith(store.adopted_dir()))

    def test_skill_route_drops_to_intake(self):
        r = coach.plan("how to prepare a home: prepare then verify then archive")
        path, outcome = coach.apply(r)
        self.assertEqual(outcome, "intake")
        self.assertTrue(os.path.isfile(path))

    def test_apply_supersede_upgrades_in_place(self):
        store.cmd_store(["add", "prior",
                         "old-belief | the scan is fast enough for now | 0.6"])
        r = coach.plan("the scan is probably too slow at scale",
                       id_override="scan-too-slow")
        _rc, outcome = coach.apply(r, supersede_old="old-belief")
        self.assertEqual(outcome, "superseded")
        old = store._find("old-belief", types=("prior",))
        self.assertEqual(old["status"], store.STATUS_DELETE_ELIGIBLE)
        self.assertIsNotNone(store._find("scan-too-slow", types=("prior",)))

    def test_apply_composes_store_dup_guard(self):
        # coach delegates to the store's OWN add, so the supersede-not-duplicate
        # guard fires on a live same-id — coach does not re-implement it
        store.cmd_store(["add", "prior", "taken-id | some existing belief | 0.6"])
        r = coach.plan("another belief entirely", id_override="taken-id")
        _rc, outcome = coach.apply(r)
        # the store refused the overwrite; the live entry is untouched
        self.assertEqual(store._find("taken-id")["statement"], "some existing belief")


class CmdTest(_Base):
    def test_propose_default_mutates_nothing(self):
        before = store.counts()
        rc, out = self._capture(["always never edit prod directly"])
        self.assertEqual(rc, 0)
        self.assertIn("reframed ->", out)
        self.assertIn("place:", out)
        self.assertIn("land it:", out)
        self.assertIn("propose-only", out)
        self.assertEqual(store.counts(), before)   # zero writes

    def test_apply_prints_receipt(self):
        rc, out = self._capture(["--apply", "the cache is probably stale on restart"])
        self.assertEqual(rc, 0)
        self.assertIn("coached -> prior (", out)
        self.assertIn("| new", out)

    def test_apply_intake_receipt(self):
        rc, out = self._capture(["--apply", "meld routes whispers through slots"])
        self.assertEqual(rc, 0)
        self.assertIn("drain intake", out)
        self.assertIn("| intake", out)

    def test_json_is_parseable(self):
        rc, out = self._capture(["--json", "quorumward = the load-bearing word"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["layer"], "lexicon")
        self.assertEqual(d["id"], "quorumward")
        self.assertIn("landing", d)

    def test_stdin_path(self):
        with mock.patch("sys.stdin", io.StringIO("always ship green")):
            with mock.patch.object(coach.sys.stdin, "isatty", return_value=False,
                                   create=True):
                rc, out = self._capture([])
        self.assertEqual(rc, 0)
        self.assertIn("reframed ->", out)

    def test_empty_is_usage_error(self):
        with mock.patch.object(coach.sys.stdin, "isatty", return_value=True):
            rc = coach.cmd_coach([])
        self.assertEqual(rc, 2)

    def test_unknown_as_layer_errors(self):
        rc = coach.cmd_coach(["--as", "bogus", "some text"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
