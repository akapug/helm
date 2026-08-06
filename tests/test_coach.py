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

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-coach-home-", var="HELM_HOME")
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-coach-adopt-", var="HELM_ADOPTED_DIR")

from helm import coach, home, pk, premise, store  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-coach-")
        self.prev = {k: os.environ.get(k)
                     for k in ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_NODE_URL",
                               "HELM_STORE_FORCE_NEW")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_NODE_URL"] = "http://127.0.0.1:1"  # dead: anchor fails open
        os.environ.pop("HELM_STORE_FORCE_NEW", None)
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
                         "scrub-before-push | scrub internal planning docs before push "
                         "| 0.8 | scrub planning"])
        store.cmd_store(["add", "prior",
                         "old-scrub | scrub the planning docs before any git push "
                         "| 0.7 | docs push"])
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
            rc, outcome = coach.apply(r)
        self.assertEqual((rc, outcome), (0, "new"))
        got = store._find(r["id"], types=("prior",))
        self.assertEqual(got["class"], "certain")
        self.assertEqual(got["confidence"], 1.0)
        self.assertTrue(got["keywords"])

    def test_apply_premise_refusal_never_supersedes(self):  # noqa: VACUOUS_ASSERTION — seeded old and sibling rows positively control the no-write check
        ts = pk.now_ts()
        store.write_prior({
            "id": "old-law", "statement": "the old production law",
            "confidence": 1.0, "keywords": "production law, old guard",
            "status": store.STATUS_LIVE, "source": "test", "stated_ts": ts,
            "last_updated": ts})
        store.write_prior({
            "id": "secret-sibling", "statement": "secrets stay local",
            "confidence": 1.0, "keywords": "commit, secrets, remote, registry, nightly, rotation",
            "status": store.STATUS_LIVE, "source": "test", "stated_ts": ts,
            "last_updated": ts})
        r = coach.plan("always commit secrets to a remote registry during nightly rotation",
                       id_override="new-secret-law")
        rc, outcome = coach.apply(r, supersede_old="old-law")
        self.assertEqual((rc, outcome), (1, "refused"))
        self.assertIsNone(store._find("new-secret-law"))
        self.assertEqual(store._find("old-law")["status"], store.STATUS_LIVE)
        self.assertEqual(premise.chain_records(), [])

    def test_apply_premise_supersede_uses_native_attested_route(self):  # noqa: VACUOUS_ASSERTION — old and new native records positively control linkage
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = premise.cmd_premise(
                ["old-law | the old production law | production law, old guard"])
        self.assertEqual(rc, 0)
        old_record = store._find("old-law")["attest_record"]
        r = coach.plan("always require a reviewed production deploy",
                       id_override="reviewed-production-law")
        with contextlib.redirect_stdout(io.StringIO()):
            rc, outcome = coach.apply(r, supersede_old="old-law")
        self.assertEqual((rc, outcome), (0, "superseded"))
        new = store._find("reviewed-production-law")
        self.assertEqual(store._find("old-law")["status"],
                         store.STATUS_DELETE_ELIGIBLE)
        self.assertEqual(new["attest_supersedes_record"], old_record)
        self.assertEqual(premise.chain_records()[-1]["op"], "supersede")

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
                         "old-belief | the scan is fast enough for now | 0.6 | scanspeed"])
        r = coach.plan("the scan is probably too slow at scale",
                       id_override="scan-too-slow")
        _rc, outcome = coach.apply(r, supersede_old="old-belief")
        self.assertEqual(outcome, "superseded")
        old = store._find("old-belief", types=("prior",))
        self.assertEqual(old["status"], store.STATUS_DELETE_ELIGIBLE)
        self.assertIsNotNone(store._find("scan-too-slow", types=("prior",)))

    def test_apply_composes_store_dup_guard_exactly_once(self):  # noqa: VACUOUS_ASSERTION — positive guard count and stored row prove the delegated path ran
        # Non-premise typed routes still delegate to store add: no coach pre-guard
        # and no second guard around the store's one semantic mint gate.
        r = coach.plan("another belief might be true", id_override="new-belief")
        from helm.store import cli as store_cli
        with mock.patch.object(store_cli, "guard_entry_keywords",
                               wraps=store_cli.guard_entry_keywords) as guard:
            rc, outcome = coach.apply(r)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual((rc, outcome), (0, "new"))
        self.assertIsNotNone(store._find("new-belief"))

    def test_generic_supersede_failure_propagates_without_false_receipt(self):  # noqa: VACUOUS_ASSERTION — replacement existence positively controls the partial-state outcome
        r = coach.plan("the resolver might need a wider probe",
                       id_override="wider-probe")
        rc, outcome = coach.apply(r, supersede_old="missing-old")
        self.assertEqual((rc, outcome), (1, "supersede-refused"))
        self.assertIsNotNone(store._find("wider-probe"))

    def test_unsupported_lexicon_supersede_refuses_before_landing(self):  # noqa: VACUOUS_ASSERTION — explicit refusal text controls the intentional pre-mint absence
        r = coach.plan("quorumward = the load-bearing word")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, outcome = coach.apply(r, supersede_old="old-term")
        self.assertEqual((rc, outcome), (1, "refused"))
        self.assertIn("native evolution path", err.getvalue())
        self.assertIsNone(store._find("quorumward", types=("lexicon",)))

    def test_supersede_that_cannot_semantically_land_refuses_before_intake(self):  # noqa: VACUOUS_ASSERTION — adopted-dir and event-ledger equality prove both mutation channels stayed untouched
        cases = (
            "how to prepare a home: prepare then verify then archive",
            "meld send deposits into the profile whisper slots",
        )
        for text in cases:
            with self.subTest(text=text):
                r = coach.plan(text)
                before_files = sorted(os.listdir(store.adopted_dir()))
                before_events = pk.read_events(50)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc, outcome = coach.apply(r, supersede_old="old-entry")
                self.assertEqual((rc, outcome), (1, "refused"))
                self.assertIn("cannot land semantically", err.getvalue())
                self.assertEqual(sorted(os.listdir(store.adopted_dir())), before_files)
                self.assertEqual(pk.read_events(50), before_events)


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

    def test_apply_refusal_rc_and_receipt_are_truthful(self):
        ts = pk.now_ts()
        store.write_prior({
            "id": "secret-sibling", "statement": "secrets stay local",
            "confidence": 1.0, "keywords": "commit, secrets, remote, registry, nightly, rotation",
            "status": store.STATUS_LIVE, "source": "test", "stated_ts": ts,
            "last_updated": ts})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self._capture([
                "--apply", "--id", "new-secret-law",
                "always commit secrets to a remote registry during nightly rotation"])
        self.assertEqual(rc, 1)
        self.assertIn("| refused", out)
        self.assertNotIn("| new", out)
        self.assertIn("secret-sibling", err.getvalue())

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
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = coach.cmd_coach(["--as", "bogus", "some text"])
        self.assertEqual(rc, 2)
        self.assertIn("lexicon|premise|prior|heuristic|reference|reflex|skill)",
                      err.getvalue())
        self.assertNotIn("skill|skill", err.getvalue())


if __name__ == "__main__":
    unittest.main()
