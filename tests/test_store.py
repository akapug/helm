#!/usr/bin/env python3
"""store tests — hermetic: every root (adopted + helm-global + project) points
at a tempdir via HELM_HOME + HELM_ADOPTED_DIR. The real ~/.claude, ~/.helm and
~/.mc are never read or written."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, pk, store  # noqa: E402

TS = "2026-07-18T00:00:00Z"


def prem_text(pid, statement, status="live"):
    """A legacy prem-*.md in the live-store shape (no confidence field)."""
    return ("---\nname: prem-%s\ndescription: \"premise: %s\"\nmetadata:\n"
            "  node_type: memory\n  type: premise\n  id: %s\n  statement: %s\n"
            "  status: %s\n  stated_ts: 2026-06-01\n---\n\nPREMISE: %s\n"
            % (pid, pid, pid, statement, status, statement))


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-store-")
        self.adopted = os.path.join(self.tmp, "adopted")
        os.makedirs(self.adopted)
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = self.adopted

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- seed helpers -------------------------------------------------------
    def global_dir(self, sub):
        return os.path.join(home.global_dir(), sub)

    def project_dir(self, name, sub):
        return os.path.join(home.project_dir(name), sub)

    def seed_prior(self, pid, statement, conf=0.6, keywords="", root_dir=None, **kw):
        e = {"id": pid, "statement": statement, "confidence": conf,
             "keywords": keywords, "stated_ts": TS, "source": "human"}
        e.update(kw)
        return store.write_prior(e, root_dir=root_dir or self.global_dir("premises"))

    def one(self, es, eid):
        hits = [e for e in es if e["id"] == eid]
        self.assertEqual(len(hits), 1, "expected exactly one '%s' in %r"
                         % (eid, [x["id"] for x in es]))
        return hits[0]


class RootsTest(StoreBase):
    def test_roots_order_and_injection(self):
        r = store.roots()
        self.assertEqual([t[0] for t in r], ["adopted", "helm-global"])
        self.assertEqual(r[0][2], self.adopted)
        r = store.roots(project="p1")
        self.assertEqual([t[0] for t in r], ["adopted", "helm-global", "project"])
        self.assertEqual(r[2][1], "project:p1")

    def test_new_writes_never_hit_adopted_by_default(self):
        p = self.seed_prior("x-law", "Always do X.")
        self.assertTrue(p.startswith(home.global_dir()))
        self.assertEqual(os.listdir(self.adopted), [])


class PriorTest(StoreBase):
    def test_round_trip_and_byte_shape(self):
        p = self.seed_prior("x-law", "Always do X.", conf=0.7,
                            keywords="xlaw,zeta", domain="dev")
        expected = """---
name: prior-x-law
description: "prior: x-law - Always do X."
metadata:
  node_type: memory
  type: prior
  id: x-law
  statement: Always do X.
  confidence: 0.70
  class: prior
  load_class: jit
  status: live
  domain: dev
  keywords: xlaw,zeta
  stated_ts: %s
  last_updated: %s
  source: human
  evidence_log: []
  confidence_history: []
---

PRIOR: Always do X.

**Why a prior:** human-stated standing belief; agent flags, never silently overrides; confidence updates on logged evidence.
""" % (TS, TS)
        with open(p) as f:
            self.assertEqual(f.read(), expected)
        e = self.one(store.load_all(), "x-law")
        self.assertEqual((e["type"], e["class"], e["load_class"]), ("prior", "prior", "jit"))
        self.assertAlmostEqual(e["confidence"], 0.7)
        self.assertEqual((e["root"], e["scope"], e["status"]), ("helm-global", "global", "live"))
        self.assertEqual(e["path"], p)
        self.assertFalse(e["pinned"])
        self.assertEqual(e["evidence_log"], [])

    def test_legacy_prem_dual_read_prior_wins(self):
        pk.atomic_write(os.path.join(self.adopted, "prem-old-truth.md"),
                        prem_text("old-truth", "the legacy statement"))
        es = store.load_all()
        e = self.one(es, "old-truth")
        # a legacy premise missing confidence IS a certain-prior, jit-not-always
        self.assertEqual((e["confidence"], e["class"], e["load_class"]),
                         (1.0, "certain", "jit"))
        store.write_prior({"id": "old-truth", "statement": "the migrated statement",
                          "confidence": 0.8, "stated_ts": TS}, root_dir=self.adopted)
        e = self.one(store.load_all(), "old-truth")
        self.assertEqual(e["statement"], "the migrated statement")
        self.assertTrue(e["path"].endswith("prior-old-truth.md"))

    def test_confidence_coercion_and_clamp(self):
        self.assertEqual(store._coerce_conf(""), 1.0)
        self.assertEqual(store._coerce_conf(None), 1.0)
        self.assertEqual(store._coerce_conf("garbled"), 1.0)
        self.assertEqual(store._coerce_conf("1.7"), 1.0)
        self.assertEqual(store._coerce_conf(-2), 0.0)
        self.assertAlmostEqual(store._coerce_conf("0.35"), 0.35)

    def test_belief_never_rounds_up_to_certainty(self):
        # audit MEDIUM: %.2f serialized 0.995..0.999 as "confidence: 1.00"
        # beside "class: prior" — the next read minted a certain premise from
        # an agent-suppliable belief. A belief clamps to 0.99 BEFORE writing.
        p = self.seed_prior("almostsure", "strong belief", conf=0.999)
        with open(p) as f:
            raw = f.read()
        self.assertIn("  confidence: 0.99", raw)
        self.assertIn("  class: prior", raw)
        self.assertNotIn("1.00", raw)
        e = self.one(store.load_all(), "almostsure")
        self.assertEqual(e["class"], "prior")
        self.assertAlmostEqual(e["confidence"], 0.99)
        # an explicit human certainty (exactly 1.0) still writes as a premise
        p = self.seed_prior("truth", "human truth", conf=1.0)
        with open(p) as f:
            raw = f.read()
        self.assertIn("  confidence: 1.00", raw)
        self.assertIn("  class: certain", raw)

    def test_dormant_derivation(self):
        self.seed_prior("weak-belief", "barely held", conf=0.3)
        e = self.one(store.load_all(), "weak-belief")
        self.assertEqual(e["load_class"], "dormant")
        self.assertEqual(store.load_all(include_dormant=False), [])

    def test_retired_excluded_unless_asked(self):
        self.seed_prior("gone", "was true once", conf=0.9)
        e, err = store.retire("gone", TS, "no longer holds")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "retired")
        self.assertEqual(store.load_all(), [])
        e = self.one(store.load_all(include_retired=True), "gone")
        self.assertEqual((e["status"], e["retired_why"]), ("retired", "no longer holds"))


class EvidenceTest(StoreBase):
    def test_belief_move_appends_receipts(self):
        self.seed_prior("belief", "probably true", conf=0.6, keywords="zork")
        e, msg = store.apply_evidence("belief", TS, "0.2", "confirmed in the field")
        self.assertIsNone(msg)
        self.assertAlmostEqual(e["confidence"], 0.8)
        # persisted: reread from disk carries the receipt + the snapshot
        e = self.one(store.load_all(), "belief")
        self.assertAlmostEqual(e["confidence"], 0.8)
        self.assertEqual(len(e["evidence_log"]), 1)
        self.assertEqual(e["evidence_log"][0]["type"], "support")
        self.assertEqual(e["evidence_log"][0]["by"], "agent")
        self.assertEqual(len(e["confidence_history"]), 1)
        self.assertAlmostEqual(e["confidence_history"][0]["value"], 0.8)

    def test_belief_clamp_never_reaches_certainty(self):
        self.seed_prior("belief", "probably true", conf=0.6)
        e, _ = store.apply_evidence("belief", TS, 10, "overwhelming support")
        self.assertEqual(e["confidence"], 0.99)
        e, _ = store.apply_evidence("belief", TS, -10, "overwhelming contradiction")
        self.assertEqual(e["confidence"], 0.05)

    def test_unreasoned_move_refused(self):
        self.seed_prior("belief", "probably true", conf=0.6)
        e, err = store.apply_evidence("belief", TS, 0.1, "   ")
        self.assertIsNone(e)
        self.assertIn("reason", err)
        e, err = store.apply_evidence("belief", TS, "not-a-number", "why")
        self.assertIsNone(e)
        self.assertEqual(err, "delta not numeric")
        e, err = store.apply_evidence("missing", TS, 0.1, "why")
        self.assertIsNone(e)
        self.assertEqual(err, "not found")

    def test_certain_prior_logs_but_does_not_move(self):
        self.seed_prior("truth", "human-stated truth", conf=1.0)
        e, msg = store.apply_evidence("truth", TS, -0.3, "agent saw a counterexample")
        self.assertIsNotNone(msg)
        self.assertIn("certain-prior", msg)
        self.assertEqual(e["confidence"], 1.0)
        e = self.one(store.load_all(), "truth")
        self.assertEqual(e["confidence"], 1.0)      # pinned at certainty
        self.assertEqual(len(e["evidence_log"]), 1)  # but the contradiction is RECORDED
        self.assertEqual(e["evidence_log"][0]["type"], "contradict")

    def test_human_evidence_moves_a_certainty(self):
        self.seed_prior("truth", "human-stated truth", conf=1.0)
        e, msg = store.apply_evidence("truth", TS, -0.5, "the human recanted", by="human")
        self.assertIsNone(msg)
        self.assertAlmostEqual(e["confidence"], 0.5)

    def test_evidence_writes_in_place_on_adopted(self):
        store.write_prior({"id": "adopted-belief", "statement": "lives in adopted",
                          "confidence": 0.6, "stated_ts": TS}, root_dir=self.adopted)
        e, _ = store.apply_evidence("adopted-belief", TS, 0.1, "still true")
        self.assertEqual(os.path.dirname(e["path"]), self.adopted)
        self.assertAlmostEqual(self.one(store.load_all(), "adopted-belief")["confidence"], 0.7)


class SupersedeTest(StoreBase):
    def test_tombstone_backpointer_and_stops_injecting(self):
        self.seed_prior("old-way", "the old belief", conf=0.8, keywords="zorkway")
        self.seed_prior("new-way", "the new belief", conf=0.9, keywords="zorkway")
        old, err = store.mark_superseded("old-way", "new-way", TS, "replaced by new-way")
        self.assertIsNone(err)
        self.assertEqual((old["status"], old["replaced_by"]),
                         ("delete_eligible", "new-way"))
        # the tombstone stops injecting but the FILE stays
        self.assertEqual([e["id"] for e in store.load_all()], ["new-way"])
        self.assertTrue(os.path.exists(old["path"]))
        self.assertEqual([e["id"] for e in store.resolve_prompt("zorkway plan")],
                         ["new-way"])
        all_es = store.load_all(include_retired=True)
        self.assertEqual(self.one(all_es, "new-way")["supersedes"], "old-way")
        self.assertEqual(self.one(all_es, "old-way")["retired_why"], "replaced by new-way")

    def test_refusals(self):
        self.seed_prior("only", "the only one", conf=0.8)
        _, err = store.mark_superseded("only", "only", TS)
        self.assertIn("supersede itself", err)
        _, err = store.mark_superseded("only", "ghost", TS)
        self.assertIn("must exist first", err)
        _, err = store.mark_superseded("ghost", "only", TS)
        self.assertIn("not found", err)
        _, err = store.mark_superseded("", "only", TS)
        self.assertIn("both", err)


class ResolveTest(StoreBase):
    def test_specificity_guard(self):
        self.seed_prior("zeta-law", "specific belief", conf=0.9, keywords="build,test")
        # generic-only match (build/test are wallpaper words) -> rejected
        self.assertEqual(store.resolve_prompt("let's build a test"), [])
        # the id itself is a specific probe
        self.assertEqual([e["id"] for e in store.resolve_prompt("apply zeta-law here")],
                         ["zeta-law"])
        # a specific keyword fires
        self.seed_prior("kw-belief", "kw belief", conf=0.9, keywords="flimflam,build")
        self.assertEqual([e["id"] for e in store.resolve_prompt("pure flimflam")],
                         ["kw-belief"])

    def test_salience_law_empty(self):
        self.seed_prior("zeta-law", "specific belief", conf=0.9, keywords="zetaness")
        self.assertEqual(store.resolve_prompt("nothing relevant here"), [])
        self.assertEqual(store.resolve_prompt(""), [])
        self.assertEqual(store.resolve_prompt(None), [])

    def test_confidence_weighted_ranking_and_cap(self):
        for i, conf in enumerate((0.5, 0.9, 0.7)):
            self.seed_prior("belief-%d" % i, "b%d" % i, conf=conf, keywords="glorp")
        got = [e["id"] for e in store.resolve_prompt("glorp time")]
        self.assertEqual(got, ["belief-1", "belief-2", "belief-0"])
        self.assertEqual(len(store.resolve_prompt("glorp time", cap=2)), 2)

    def test_word_boundary(self):
        self.seed_prior("zeta-law", "specific", conf=0.9, keywords="ram")
        self.assertEqual(store.resolve_prompt("the program crashed"), [])
        self.assertEqual([e["id"] for e in store.resolve_prompt("out of ram again")],
                         ["zeta-law"])

    def test_dormant_and_pinned_not_in_jit(self):
        self.seed_prior("weak", "weak", conf=0.3, keywords="glorp")
        self.seed_prior("pinned-one", "always on", conf=0.9, keywords="glorp", pin="true")
        self.assertEqual(store.resolve_prompt("glorp time"), [])


class DFRankTest(StoreBase):
    """DF-weighted JIT scoring: a matched probe contributes 1/df (df = entries
    carrying it), confidence-weighted — rare keywords are strong signals, and
    the old (hits, id-desc) ranking noise is gone."""

    def test_rare_keyword_outranks_generic_with_more_hits(self):
        # six entries share alpha/beta/gamma (df=6 each); one entry alone
        # carries glorpx (df=1). Old ranking: 3 hits beat 1 -> a shared-keyword
        # entry won. DF: 3/6 = 0.5 < 1/1 = 1.0 -> the rare match wins the cap.
        for i in range(5):
            self.seed_prior("filler-%d" % i, "s", conf=0.8, keywords="alpha,beta,gamma")
        self.seed_prior("many-hits", "s", conf=0.8, keywords="alpha,beta,gamma")
        self.seed_prior("rare-one", "s", conf=0.8, keywords="glorpx")
        got = [e["id"] for e in store.resolve_prompt("alpha beta gamma glorpx report")]
        self.assertEqual(got[0], "rare-one")

    def test_alphabetical_order_no_longer_decides(self):
        # same confidence, same ts: the old tiebreak ranked ids DESCENDING, so
        # zzz-* won the cap arbitrarily. DF puts the rare match first instead.
        self.seed_prior("zzz-shared", "s", conf=0.8, keywords="commonkw")
        self.seed_prior("yyy-shared", "s", conf=0.8, keywords="commonkw")
        self.seed_prior("aaa-rare", "s", conf=0.8, keywords="rarekw")
        got = [e["id"] for e in store.resolve_prompt("commonkw rarekw both")]
        self.assertEqual(got[0], "aaa-rare")
        # and a FULL tie (score AND ts) falls to stable load order, never the
        # old id-descending rank (which would put zzz-shared first)
        self.assertEqual(got[1:], ["yyy-shared", "zzz-shared"])

    def test_tie_breaks_most_recently_updated(self):
        self.seed_prior("aaa-stale", "s", conf=0.8, keywords="glorp",
                        last_updated="2026-01-01T00:00:00Z")
        self.seed_prior("zzz-fresh", "s", conf=0.8, keywords="glorp",
                        last_updated="2026-06-01T00:00:00Z")
        self.assertEqual([e["id"] for e in store.resolve_prompt("glorp time")],
                         ["zzz-fresh", "aaa-stale"])

    def test_df_computed_over_candidates_only(self):
        # dormant/pinned entries are outside the JIT candidate set, so they
        # must not dilute df either
        self.seed_prior("live-one", "s", conf=0.8, keywords="sharedkw")
        self.seed_prior("dorm-one", "s", conf=0.3, keywords="sharedkw")
        self.seed_prior("pin-one", "s", conf=1.0, keywords="sharedkw", pin="true")
        df = store._df_map(store._jit_candidates(store.load_all()))
        self.assertEqual(df["sharedkw"], 1)


class LexiconTest(StoreBase):
    def test_write_and_term_match(self):
        p = store.write_lexicon({"term": "youable", "definition": "able to be you",
                                 "updated_ts": TS})
        self.assertTrue(p.endswith("lex-youable.md"))
        with open(p) as f:
            raw = f.read()
        self.assertIn("name: lex-youable", raw)
        self.assertIn("  type: lexicon", raw)
        self.assertIn("  scope: global", raw)
        self.assertIn("  definition: able to be you", raw)
        e = self.one(store.load_all(types=("lexicon",)), "youable")
        self.assertEqual((e["term"], e["definition"], e["load_class"]),
                         ("youable", "able to be you", "jit"))
        got = store.resolve_prompt("is this youable at all")
        self.assertEqual([x["id"] for x in got], ["youable"])
        self.assertEqual(store.resolve_prompt("unyouable is not the term"), [])

    def test_scoped_filename_never_clobbers_global(self):
        store.write_lexicon({"term": "youable", "definition": "global sense"})
        p = store.write_lexicon({"term": "youable", "definition": "project sense",
                                 "term_scope": "project:p1"},
                                root_dir=self.project_dir("p1", "lexicon"))
        self.assertTrue(p.endswith("lex-project-p1--youable.md"))
        e = self.one(store.load_all(project="p1", types=("lexicon",)), "youable")
        self.assertEqual(e["definition"], "project sense")


class HeuristicTest(StoreBase):
    def test_always_jit_conf_one(self):
        p = store.write_heuristic({"id": "swarmify", "move": "split into waves",
                                   "trigger": "swarm,waves", "stated_ts": TS,
                                   "load_class": "always"})  # authored always is IGNORED
        with open(p) as f:
            self.assertIn("  load_class: jit", f.read())
        e = self.one(store.load_all(types=("heuristic",)), "swarmify")
        self.assertEqual((e["confidence"], e["load_class"], e["statement"]),
                         (1.0, "jit", "split into waves"))
        got = store.resolve_prompt("the swarm is restless")
        self.assertEqual([x["id"] for x in got], ["swarmify"])

    def test_heuristic_generic_trigger_guard(self):
        store.write_heuristic({"id": "vague-move", "move": "do the thing",
                               "trigger": "strategy,move", "stated_ts": TS})
        self.assertEqual(store.resolve_prompt("what strategy should we move on"), [])

    def test_description_fallback_for_live_store_shape(self):
        # the real adopted store carries heuristics whose move lives ONLY in the
        # description line — they must still resolve, not silently vanish
        pk.atomic_write(os.path.join(self.adopted, "heuristic-hand-authored.md"),
                        '---\nname: heuristic-hand-authored\n'
                        'description: "compose primitives, always"\nmetadata:\n'
                        '  node_type: memory\n  type: heuristic\n  id: hand-authored\n'
                        '  trigger: composeall\n  status: live\n---\nbody\n')
        e = self.one(store.load_all(types=("heuristic",)), "hand-authored")
        self.assertEqual(e["statement"], "compose primitives, always")
        self.assertEqual([x["id"] for x in store.resolve_prompt("composeall now")],
                         ["hand-authored"])


class ReferenceTest(StoreBase):
    def test_round_trip_and_resolve(self):
        p = store.write_reference({"id": "dregg-mandates", "statement":
                                   "Lean-proven mandate crown", "url": "https://x.test/d",
                                   "keywords": "dregg,mandate", "stated_ts": TS})
        with open(p) as f:
            raw = f.read()
        self.assertIn("name: ref-dregg-mandates", raw)
        self.assertIn("  type: reference", raw)
        self.assertIn("  url: https://x.test/d", raw)
        self.assertIn("  load_class: jit", raw)
        e = self.one(store.load_all(types=("reference",)), "dregg-mandates")
        self.assertEqual((e["type"], e["confidence"], e["url"]),
                         ("reference", 1.0, "https://x.test/d"))
        got = store.resolve_prompt("the dregg weld")
        self.assertEqual([x["id"] for x in got], ["dregg-mandates"])

    def test_live_store_shape_name_description_only(self):
        # the one real ref-* file today has NO id/summary fields — name +
        # description must carry it
        pk.atomic_write(os.path.join(self.adopted, "ref-bare.md"),
                        '---\nname: ref-bare\ndescription: "a harvested nugget"\n'
                        'metadata: \n  node_type: memory\n  type: reference\n---\nbody\n')
        e = self.one(store.load_all(types=("reference",)), "bare")
        self.assertEqual(e["statement"], "a harvested nugget")
        self.assertEqual(e["load_class"], "jit")


class EpisodicTest(StoreBase):
    def test_episodic_dormant_never_resolves(self):
        pk.atomic_write(os.path.join(self.adopted, "war-story.md"),
                        '---\nname: war-story\ndescription: "the glorp incident"\n'
                        'metadata:\n  node_type: memory\n  type: project\n---\nbody\n')
        pk.atomic_write(os.path.join(self.adopted, "MEMORY.md"), "# index\n- stuff\n")
        pk.atomic_write(os.path.join(self.adopted, "reflex-someone-elses.md"),
                        '---\nname: reflex-someone-elses\ndescription: "not ours"\n---\n')
        es = store.load_all()
        e = self.one(es, "war-story")
        self.assertEqual((e["type"], e["load_class"], e["memory_type"]),
                         ("episodic", "dormant", "project"))
        self.assertNotIn("MEMORY", [x["id"] for x in es])
        self.assertEqual([x for x in es if x["id"].startswith("reflex")], [])
        self.assertEqual(store.resolve_prompt("the glorp incident war-story"), [])
        self.assertEqual(store.load_all(include_dormant=False), [])


class TypedFallbackTest(StoreBase):
    def test_typed_prefix_without_typed_fields_reads_as_episodic(self):
        # the live store carries prem-/lex- named files that are really bulk
        # memory (name+description, type: project, no id/statement) — mc drops
        # them; helm keeps them visible as episodic, never injected
        pk.atomic_write(os.path.join(self.adopted, "prem-bulk-note.md"),
                        '---\nname: prem-bulk-note\ndescription: "canon paragraph"\n'
                        'metadata:\n  node_type: memory\n  type: project\n---\nbody\n')
        e = self.one(store.load_all(), "prem-bulk-note")
        self.assertEqual((e["type"], e["load_class"]), ("episodic", "dormant"))
        self.assertEqual(store.resolve_prompt("prem-bulk-note canon paragraph"), [])


class ScopeTest(StoreBase):
    def test_project_shadows_global_shadows_adopted(self):
        store.write_prior({"id": "foo", "statement": "adopted sense",
                          "confidence": 0.9}, root_dir=self.adopted)
        e = self.one(store.load_all(), "foo")
        self.assertEqual((e["statement"], e["root"]), ("adopted sense", "adopted"))
        self.seed_prior("foo", "global sense", conf=0.9)
        e = self.one(store.load_all(), "foo")
        self.assertEqual((e["statement"], e["root"]), ("global sense", "helm-global"))
        self.seed_prior("foo", "project sense", conf=0.9,
                        root_dir=self.project_dir("p1", "premises"))
        e = self.one(store.load_all(project="p1"), "foo")
        self.assertEqual((e["statement"], e["root"], e["scope"]),
                         ("project sense", "project", "project:p1"))
        # without the project lens the project root is not in play
        e = self.one(store.load_all(), "foo")
        self.assertEqual(e["root"], "helm-global")

    def test_retiring_shadow_winner_never_resurrects_the_shadowed(self):
        # audit HIGH: a stale wide-scope belief shadowed by a narrow-scope
        # override must STAY buried when the override is retired/superseded —
        # per-root status filtering resurrected it as live.
        store.write_prior({"id": "shipfast", "statement": "old stale belief",
                          "confidence": 0.6, "keywords": "shipfast"},
                          root_dir=self.adopted)
        self.seed_prior("shipfast", "NEW corrected belief", conf=0.9,
                        keywords="shipfast",
                        root_dir=self.project_dir("myproj", "premises"))
        e = self.one(store.load_all(project="myproj"), "shipfast")
        self.assertEqual(e["root"], "project")
        _, err = store.retire("shipfast", TS, "no longer holds", project="myproj")
        self.assertIsNone(err)
        # the retired project override does NOT un-bury the adopted copy
        self.assertEqual(store.load_all(project="myproj"), [])
        self.assertEqual(store.resolve_prompt("shipfast plan", project="myproj"), [])
        # asked-for retired view shows the shadow winner, tombstoned
        e = self.one(store.load_all(project="myproj", include_retired=True), "shipfast")
        self.assertEqual((e["root"], e["status"]), ("project", "retired"))
        # the GLOBAL lens (no project root in play) still sees the adopted copy
        e = self.one(store.load_all(), "shipfast")
        self.assertEqual((e["root"], e["status"]), ("adopted", "live"))

    def test_superseding_shadow_winner_never_resurrects_the_shadowed(self):
        store.write_prior({"id": "shipfast", "statement": "old stale belief",
                          "confidence": 0.6}, root_dir=self.adopted)
        self.seed_prior("shipfast", "project override", conf=0.9,
                        root_dir=self.project_dir("myproj", "premises"))
        self.seed_prior("shipslow", "the replacement", conf=0.9,
                        root_dir=self.project_dir("myproj", "premises"))
        _, err = store.mark_superseded("shipfast", "shipslow", TS,
                                       "replaced", project="myproj")
        self.assertIsNone(err)
        self.assertEqual([e["id"] for e in store.load_all(project="myproj")],
                         ["shipslow"])

    def test_no_cross_type_shadowing(self):
        self.seed_prior("shared-slug", "the belief", conf=0.9)
        store.write_lexicon({"term": "shared-slug", "definition": "the term"})
        self.assertEqual(len(store.load_all()), 2)


class PinnedTest(StoreBase):
    def test_ordering_and_flag_survival(self):
        self.seed_prior("pin-b", "belief pin", conf=0.9, pin="true")
        self.seed_prior("pin-a", "certain pin", conf=1.0, pin="true")
        self.seed_prior("unpinned", "not pinned", conf=1.0)
        got = store.pinned()
        self.assertEqual([e["id"] for e in got], ["pin-a", "pin-b"])
        self.assertTrue(all(e["load_class"] == "always" for e in got))
        # the flag survives the file round-trip
        with open(got[1]["path"]) as f:
            self.assertIn("  pin: true", f.read())

    def test_walk_order_confidence_recency_id(self):
        # the deterministic budget-walk law: confidence desc, then recency desc
        # (mixed ISO/epoch/blank timestamps all comparable), then id asc — the
        # old (-conf, id) key made an all-1.0 lane alphabetical (starvation)
        self.seed_prior("b-old", "iso ts", conf=1.0, pin="true",
                        last_updated="2026-06-01")
        self.seed_prior("a-new", "epoch ts newer", conf=1.0, pin="true",
                        last_updated="1782000000")  # ~2026-06-21 > 2026-06-01
        self.seed_prior("t-x", "iso tie", conf=1.0, pin="true",
                        last_updated="2026-06-01")
        self.seed_prior("z-blank", "no ts ranks last", conf=1.0, pin="true",
                        stated_ts="", last_updated="")
        self.seed_prior("c-low", "freshest but lower conf", conf=0.9, pin="true",
                        last_updated="2026-07-01")
        want = ["a-new", "b-old", "t-x", "z-blank", "c-low"]
        self.assertEqual([e["id"] for e in store.pinned()], want)
        self.assertEqual([e["id"] for e in store.pinned()], want)  # deterministic

    def test_explicit_unpin_beats_pinned_slugs_tuple(self):
        self.seed_prior("drift-is-the-enemy", "founding pin", conf=1.0)
        e = self.one(store.load_all(), "drift-is-the-enemy")
        self.assertTrue(e["pinned"])  # the tuple pins it with no flag at all
        store.write_prior({"id": "drift-is-the-enemy", "statement": "founding pin",
                           "confidence": 1.0, "pin": "false", "load_class": "jit",
                           "stated_ts": TS}, path=e["path"])
        e = self.one(store.load_all(), "drift-is-the-enemy")
        self.assertFalse(e["pinned"])
        self.assertEqual(e["load_class"], "jit")
        with open(e["path"]) as f:
            self.assertIn("  pin: false", f.read())  # the un-pin survives rewrites


class DemoteTest(StoreBase):
    def test_demote_flips_with_receipt_never_silent(self):
        self.seed_prior("pin-a", "always on", conf=0.9, pin="true")
        e, err = store.demote("pin-a", TS, "starved under the byte budget")
        self.assertIsNone(err)
        e = self.one(store.load_all(), "pin-a")  # reload: the flip is on disk
        self.assertEqual((e["load_class"], e["pinned"], e["status"]),
                         ("jit", False, "live"))
        self.assertEqual(store.pinned(), [])
        last = e["evidence_log"][-1]
        self.assertEqual((last["type"], last["by"], last["reason"]),
                         ("demoted", "human", "starved under the byte budget"))
        self.assertEqual(last["was"], {"load_class": "always", "pin": True})

    def test_demote_founding_pin_sticks(self):
        self.seed_prior("drift-is-the-enemy", "founding", conf=1.0)
        e, err = store.demote("drift-is-the-enemy", TS, "measured starved")
        self.assertIsNone(err)
        e = self.one(store.load_all(), "drift-is-the-enemy")
        self.assertEqual((e["load_class"], e["pinned"]), ("jit", False))

    def test_undo_restores_via_the_receipt(self):
        self.seed_prior("pin-a", "always on", conf=0.9, pin="true")
        store.demote("pin-a", TS, "starved")
        e, err = store.demote("pin-a", TS, "load-bearing after all", undo=True)
        self.assertIsNone(err)
        e = self.one(store.load_all(), "pin-a")
        self.assertEqual((e["load_class"], e["pinned"]), ("always", True))
        self.assertEqual([r["type"] for r in e["evidence_log"]],
                         ["demoted", "undemoted"])

    def test_demote_guards(self):
        self.seed_prior("pin-a", "always on", conf=0.9, pin="true")
        self.seed_prior("plain", "jit entry", conf=0.8)
        for args, msg in ((("pin-a", TS, " "), "reason is required"),
                          (("plain", TS, "why"), "not in the always lane"),
                          (("ghost", TS, "why"), "not found")):
            e, err = store.demote(*args)
            self.assertIsNone(e)
            self.assertIn(msg, err)
        e, err = store.demote("plain", TS, "why", undo=True)
        self.assertIn("no demote receipt", err)
        e, err = store.demote("pin-a", TS, "why", undo=True)
        self.assertIn("already in the always lane", err)

    def test_demote_reference_flip_and_undo(self):
        store.write_reference({"id": "ref-a", "statement": "harvested",
                               "load_class": "always", "stated_ts": TS})
        e, err = store.demote("ref-a", TS, "starved")
        self.assertIsNone(err)
        self.assertEqual(self.one(store.load_all(), "ref-a")["load_class"], "jit")
        e, err = store.demote("ref-a", TS, "restore", undo=True)
        self.assertIsNone(err)
        self.assertEqual(self.one(store.load_all(), "ref-a")["load_class"], "always")


class PinnedStatsTest(StoreBase):
    """pinned --stats: the budget walk as inject renders it now + made-it
    counts over the fire-ledger window — read-only against a planted ledger."""

    def setUp(self):
        super().setUp()
        # 4 pins, each line truncating to exactly LINE_CAP (400B) -> 3 fit the
        # 1200B budget, pin-3 (same conf, same ts, id-last) starves
        for i in range(4):
            self.seed_prior("pin-%d" % i, "x" * 400, conf=1.0, pin="true")

    def ledger(self, rows):
        from helm import inject
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(rows) + "\n")
        return path

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue()

    def test_stats_walk_and_ledger_window_read_only(self):
        row = lambda ids: json.dumps(
            {"v": 1, "ts": "2026-07-19T00:00:00Z", "fired": {"pinned": ids}})
        path = self.ledger([row(["pin-0"]), row(["pin-0"]), row(["pin-0"]),
                            '{"v":1,"ts":"x","silent":true}', "not json",
                            row(["pin-1", "ghost-id"])])
        with open(path, "rb") as f:
            before = f.read()
        rc, out = self.run_cli(["pinned", "--stats"])
        self.assertEqual(rc, 0)
        self.assertIn("4 always, budget 1200B", out)
        self.assertIn("+ pin-0  made 3/4", out)
        self.assertIn("+ pin-1  made 1/4", out)  # ghost-id ignored, row counted
        self.assertIn("+ pin-2  made 0/4", out)
        self.assertIn("- pin-3  made 0/4", out)  # over budget NOW and starved
        self.assertIn("2 of 4 always-entries NEVER made the budget", out)
        self.assertIn("pin-2, pin-3", out)
        self.assertIn("helm store demote <id>", out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)  # stats never writes the ledger

    def test_stats_without_ledger(self):
        rc, out = self.run_cli(["pinned", "--stats"])
        self.assertEqual(rc, 0)
        self.assertIn("no fire-ledger rows with a pinned lane yet", out)
        self.assertIn("- pin-3", out)  # the walk still names the over-budget entry


class EntriesSeamTest(StoreBase):
    """pinned/resolve_prompt entries= — inject's parsed-entry cache feeds both
    lanes through this seam with ZERO load_all() calls (the durable one-parse
    fix), and the supplied list gets exactly the load_all-path post-filters."""

    def synth(self, eid, load_class="jit", etype="prior", keywords="", conf=0.8):
        return {"id": eid, "type": etype, "statement": "s", "confidence": conf,
                "load_class": load_class, "keywords": keywords, "last_updated": ""}

    def test_supplied_entries_never_touch_disk(self):
        entries = [self.synth("pin-a", load_class="always", conf=1.0),
                   self.synth("jit-a", keywords="fluxcap"),
                   self.synth("dorm-a", load_class="dormant", keywords="fluxcap"),
                   self.synth("epi-a", etype="episodic", keywords="fluxcap")]
        with mock.patch.object(store, "load_all",
                               side_effect=AssertionError("entries= must skip load_all")):
            got_pinned = store.pinned(entries=entries)
            got_jit = store.resolve_prompt("tune the fluxcap", entries=entries)
        self.assertEqual([e["id"] for e in got_pinned], ["pin-a"])
        # dormant/episodic/always filtered exactly as the load_all path would
        self.assertEqual([e["id"] for e in got_jit], ["jit-a"])

    def test_df_scoring_stable_through_seam(self):
        # the seam path scores identically to the disk path: DF over the
        # supplied candidate list, rare probe outranks the shared one
        entries = [self.synth("shared-a", keywords="commonkw"),
                   self.synth("shared-b", keywords="commonkw"),
                   self.synth("rare-c", keywords="rarekw")]
        with mock.patch.object(store, "load_all",
                               side_effect=AssertionError("entries= must skip load_all")):
            got = store.resolve_prompt("commonkw rarekw mix", entries=entries)
        self.assertEqual([e["id"] for e in got], ["rare-c", "shared-a", "shared-b"])


class CountsTest(StoreBase):
    def test_per_root_type_counts(self):
        store.write_prior({"id": "a", "statement": "s", "confidence": 0.9},
                          root_dir=self.adopted)
        pk.atomic_write(os.path.join(self.adopted, "war-story.md"),
                        '---\nname: war-story\ndescription: "d"\n---\n')
        self.seed_prior("b", "s", conf=0.9)
        store.write_lexicon({"term": "youable", "definition": "d"})
        store.write_heuristic({"id": "h", "move": "m", "trigger": "t"})
        store.write_reference({"id": "r", "statement": "s"})
        self.seed_prior("c", "s", conf=0.9,
                        root_dir=self.project_dir("p1", "premises"))
        c = store.counts(project="p1")
        self.assertEqual(c["adopted"], {"prior": 1, "episodic": 1})
        self.assertEqual(c["helm-global"],
                         {"prior": 1, "lexicon": 1, "heuristic": 1, "reference": 1})
        self.assertEqual(c["project"], {"prior": 1})


class CliTest(StoreBase):
    def run_cli(self, args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = store.cmd_store(args)
        finally:
            sys.stdin = old_stdin
        return rc, out.getvalue()

    def test_add_list_get(self):
        rc, out = self.run_cli(["add", "prior",
                                "cli-x | the statement | 0.7 | clikw | dev",
                                "--source", "human", "--rationale", "seen twice"])
        self.assertEqual(rc, 0)
        self.assertIn("helm store: LIVE 'cli-x' [prior 0.70]", out)
        # default target is helm-global, never adopted
        self.assertEqual(os.listdir(self.adopted), [])
        e = self.one(store.load_all(), "cli-x")
        self.assertEqual(e["root"], "helm-global")
        self.assertEqual(len(e["evidence_log"]), 1)  # rationale seeded the receipt
        rc, out = self.run_cli(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("cli-x", out)
        rc, out = self.run_cli(["get", "cli-x"])
        self.assertEqual(rc, 0)
        self.assertIn("the statement", out)
        rc, _ = self.run_cli(["get", "ghost"])
        self.assertEqual(rc, 1)

    def test_add_prior_clamps_to_belief_rail(self):
        # the prior verb mints beliefs — 0.999 (or an outright 1.0) must never
        # become a certain premise through the agent-facing lane
        for i, raw_conf in enumerate(("0.999", "1.0")):
            rc, out = self.run_cli(["add", "prior",
                                    "clamp-%d | nearly sure | %s" % (i, raw_conf)])
            self.assertEqual(rc, 0)
            self.assertIn("[prior 0.99]", out)
            e = self.one(store.load_all(), "clamp-%d" % i)
            self.assertEqual(e["class"], "prior")
            self.assertAlmostEqual(e["confidence"], 0.99)

    def test_add_premise_and_typed_adds(self):
        rc, out = self.run_cli(["add", "premise", "cli-truth | always true | truthkw"])
        self.assertEqual(rc, 0)
        self.assertIn("[certain 1.00]", out)
        rc, _ = self.run_cli(["add", "lexicon", "youable | able to be you"])
        self.assertEqual(rc, 0)
        rc, _ = self.run_cli(["add", "heuristic", "swarmify | split it | swarm"])
        self.assertEqual(rc, 0)
        rc, _ = self.run_cli(["add", "reference",
                              "dregg | mandate crown | https://x.test | dregg"])
        self.assertEqual(rc, 0)
        c = store.counts()["helm-global"]
        self.assertEqual(c, {"prior": 1, "lexicon": 1, "heuristic": 1, "reference": 1})

    def test_add_project_targets_project_root(self):
        rc, _ = self.run_cli(["add", "prior", "proj-x | scoped", "--project", "p1"])
        self.assertEqual(rc, 0)
        e = self.one(store.load_all(project="p1"), "proj-x")
        self.assertEqual((e["root"], e["scope"]), ("project", "project:p1"))

    def test_resolve_pinned_counts(self):
        self.run_cli(["add", "prior", "cli-x | the statement | 0.7 | clikw"])
        rc, out = self.run_cli(["resolve"], stdin="clikw ahoy")
        self.assertEqual(rc, 0)
        self.assertIn("PRIOR cli-x [0.70]: the statement", out)
        rc, out = self.run_cli(["resolve"], stdin="nothing relevant")
        self.assertEqual(out, "")  # salience law: silent on no match
        self.seed_prior("pin-a", "always on", conf=1.0, pin="true")
        rc, out = self.run_cli(["pinned"])
        self.assertIn("PREMISE pin-a", out)
        rc, out = self.run_cli(["counts"])
        self.assertIn("helm-global", out)

    def test_resolve_keeps_query_words_equal_to_project_name(self):
        # audit MEDIUM: resolve --project P stripped EVERY query word equal to
        # P (the flag pair is already deleted from args) — false "no JIT match"
        # exactly when the query names the project
        self.run_cli(["add", "premise", "myproj-rule | follow the myproj law | myproj",
                      "--project", "myproj"])
        rc, out = self.run_cli(["resolve", "--project", "myproj",
                                "deploy", "myproj", "now"])
        self.assertEqual(rc, 0)
        self.assertIn("myproj-rule", out)
        self.assertNotIn("no JIT match", out)

    def test_evidence_supersede_retire(self):
        self.run_cli(["add", "prior", "cli-x | the statement | 0.7 | clikw"])
        rc, out = self.run_cli(["evidence", TS, "cli-x", "0.1", "held up in practice"])
        self.assertEqual(rc, 0)
        self.assertIn("confidence -> 0.80", out)
        rc, _ = self.run_cli(["evidence", TS, "ghost", "0.1", "why"])
        self.assertEqual(rc, 1)
        self.run_cli(["add", "prior", "cli-y | the replacement | 0.8 | clikw"])
        rc, out = self.run_cli(["supersede", TS, "cli-x", "cli-y", "sharper form"])
        self.assertEqual(rc, 0)
        self.assertIn("TOMBSTONED 'cli-x'", out)
        rc, out = self.run_cli(["retire", TS, "cli-y", "done with it"])
        self.assertEqual(rc, 0)
        self.assertIn("RETIRED 'cli-y'", out)
        self.assertEqual(store.load_all(), [])

    def test_usage_paths(self):
        rc, _ = self.run_cli([])
        self.assertEqual(rc, 2)
        rc, _ = self.run_cli(["add", "nonsense", "x | y"])
        self.assertEqual(rc, 2)
        rc, _ = self.run_cli(["frobnicate"])
        self.assertEqual(rc, 2)


class AddGuardTest(StoreBase):
    """supersede-not-duplicate at add time: a live same-id is a hard refuse
    with the exact follow-up commands (never a silent overwrite — the drain
    conflict law); a near-identical statement under another id warns loudly
    and proceeds (never blocked on similarity alone)."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_same_id_live_refuses_with_commands(self):
        rc, _, _ = self.add("prior", "x-law | the original | 0.7 | xkw")
        self.assertEqual(rc, 0)
        rc, out, err = self.add("prior", "x-law | a different statement")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("'x-law' is already LIVE [prior helm-global]", err)
        self.assertIn("helm store evidence", err)
        self.assertIn("helm store supersede", err)
        # never a silent overwrite: the original statement survives
        self.assertEqual(self.one(store.load_all(), "x-law")["statement"],
                         "the original")
        # cross-type same slug stays legal (no cross-type shadowing)
        rc, _, _ = self.add("heuristic", "x-law | do the move | movekw")
        self.assertEqual(rc, 0)
        # premise re-add of a live prior hits the same rail (premise IS prior)
        rc, _, err = self.add("premise", "x-law | now certain")
        self.assertEqual(rc, 1)
        self.assertIn("helm store evidence", err)

    def test_retired_or_superseded_id_may_be_re_minted(self):
        self.add("prior", "gone | old sense | 0.7")
        store.retire("gone", TS, "over")
        rc, out, _ = self.add("prior", "gone | new sense | 0.7")
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.load_all(), "gone")["statement"], "new sense")

    def test_near_duplicate_warns_and_proceeds(self):
        self.add("premise", "small-prs | small reviewable prs land faster and cleaner | prkw")
        rc, out, err = self.add(
            "prior", "tiny-prs | small reviewable prs land faster and cleaner today | 0.7")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("possible duplicate of 'small-prs'", out)
        self.assertIn("88% statement overlap", out)  # 7/8 tokens shared
        self.assertIn("helm store supersede", out)
        self.assertIn("LIVE 'tiny-prs'", out)  # the add went through
        self.assertEqual(len(store.load_all(types=("prior",))), 2)

    def test_distinct_adds_stay_silent(self):
        self.add("prior", "one-law | ship small slices deliberately | 0.7")
        rc, out, _ = self.add(
            "prior", "other-law | measure quota before dispatching agents | 0.7")
        self.assertEqual(rc, 0)
        self.assertNotIn("WARNING", out)

    def test_refuse_sees_live_entry_across_roots(self):
        # live in ADOPTED; the add targets helm-global — the lens still
        # refuses (a global add would silently shadow the adopted truth)
        store.write_prior({"id": "adopted-law", "statement": "adopted sense",
                           "confidence": 0.9}, root_dir=self.adopted)
        rc, _, err = self.add("prior", "adopted-law | shadowing attempt")
        self.assertEqual(rc, 1)
        self.assertIn("[prior adopted]", err)
        # a PROJECT-scoped entry is outside the global lens: global add proceeds
        self.add("prior", "proj-law | project sense | 0.6", "--project", "p1")
        rc, _, err = self.add("prior", "proj-law | global sense | 0.6")
        self.assertEqual(rc, 0)
        # but through the project lens the (narrower) live entry refuses it
        rc, _, err = self.add("prior", "proj-law | another try | 0.6",
                              "--project", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("[prior project]", err)

    def test_lexicon_redefine_stays_legal(self):
        self.add("lexicon", "youable | able to be you")
        rc, out, err = self.add("lexicon", "youable | able to be you, sharpened")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(self.one(store.load_all(types=("lexicon",)), "youable")
                         ["definition"], "able to be you, sharpened")


class AdoptProjectMemdirsTest(StoreBase):
    """Per-project claude memory dirs adopted as project-scoped store roots:
    roots(project) gains ('adopted-project', ...) triples, shadow order is
    project > adopted-project > helm-global > adopted. Hermetic: the claude
    memdir resolver is patched to a tmp dir (never the real ~/.claude)."""

    def setUp(self):
        super().setUp()
        store._ADOPTED_PROJECT_CACHE.clear()
        self.projmem = os.path.join(self.tmp, "projmem")
        os.makedirs(self.projmem)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "polyana": {"name": "polyana", "path": "/dev/polyana", "kind": "git",
                        "sessions": {}, "cwds": []}}})

    def tearDown(self):
        store._ADOPTED_PROJECT_CACHE.clear()
        super().tearDown()

    def _patch(self):
        return mock.patch.object(
            home, "claude_memory_dir_for",
            side_effect=lambda p: self.projmem if p == "/dev/polyana" else "/nonexistent-xyz")

    def test_project_adopted_root_fires_its_own_priors(self):
        store.write_prior({"id": "polyana-law", "statement": "polyana's own prior",
                           "confidence": 0.9, "keywords": "polyanaword"},
                          root_dir=self.projmem)
        with self._patch():
            store._ADOPTED_PROJECT_CACHE.clear()
            self.assertIn("adopted-project", [t[0] for t in store.roots(project="polyana")])
            e = self.one(store.load_all(project="polyana"), "polyana-law")
            self.assertEqual((e["root"], e["scope"]), ("adopted-project", "project:polyana"))
            self.assertEqual([x["id"] for x in
                              store.resolve_prompt("polyanaword now", project="polyana")],
                             ["polyana-law"])
        # without the project lens the adopted-project root is NOT in play
        self.assertEqual(store.load_all(), [])

    def test_shadow_order_project_over_adopted_project_over_global(self):
        self.seed_prior("foo", "global sense", conf=0.9)
        store.write_prior({"id": "foo", "statement": "adopted-project sense",
                           "confidence": 0.9}, root_dir=self.projmem)
        with self._patch():
            store._ADOPTED_PROJECT_CACHE.clear()
            e = self.one(store.load_all(project="polyana"), "foo")
            self.assertEqual((e["statement"], e["root"]),
                             ("adopted-project sense", "adopted-project"))
        self.seed_prior("foo", "authored project sense", conf=0.9,
                        root_dir=self.project_dir("polyana", "premises"))
        with self._patch():
            store._ADOPTED_PROJECT_CACHE.clear()
            e = self.one(store.load_all(project="polyana"), "foo")
            self.assertEqual((e["statement"], e["root"]),
                             ("authored project sense", "project"))

    def test_no_registry_falls_back_to_single_store(self):
        # a project lens with no registry file adds no adopted-project root
        os.remove(home.registry_path())
        store._ADOPTED_PROJECT_CACHE.clear()
        self.assertEqual([t[0] for t in store.roots(project="polyana")],
                         ["adopted", "helm-global", "project"])


class IndexCapTest(StoreBase):
    """helm index cap: demote MEMORY.md link lines whose backing entry stays
    jit-resolvable (lossless), oldest first, until under budget; never
    always/untyped; archive-first net + receipt; re-read + atomic-write."""

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_index(list(args))
        return rc, out.getvalue(), err.getvalue()

    def _index(self, lines):
        pk.atomic_write(os.path.join(self.adopted, "MEMORY.md"), "\n".join(lines) + "\n")

    def test_demotes_only_lossless_typed_lines_oldest_first(self):
        # three typed jit priors backing three index links; one always-pin; one
        # untyped link with no backing entry
        self.seed_prior("old-one", "old belief", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted, last_updated="2026-01-01T00:00:00Z")
        self.seed_prior("mid-one", "mid belief", conf=0.8, keywords="midkw",
                        root_dir=self.adopted, last_updated="2026-03-01T00:00:00Z")
        self.seed_prior("new-one", "new belief", conf=0.8, keywords="newkw",
                        root_dir=self.adopted, last_updated="2026-06-01T00:00:00Z")
        self.seed_prior("pinned-one", "pinned", conf=1.0, keywords="pinkw",
                        pin="true", root_dir=self.adopted)
        self._index([
            "# MEMORY index",
            "- [old](prior-old-one.md) old",
            "- [mid](prior-mid-one.md) mid",
            "- [new](prior-new-one.md) new",
            "- [pinned](prior-pinned-one.md) pinned — always, never demote",
            "- [untyped](random-note.md) no typed backing — never demote",
            "- freeform line, not a link",
        ])
        # budget 4: 7 lines -> 3 over; only 3 are lossless-demotable (old/mid/new);
        # oldest first
        r = store.index_cap(budget_lines=4, apply=False)
        self.assertEqual((r["lines"], r["over"], r["demotable"]), (7, 3, 3))
        self.assertEqual([b for _l, b in r["plan"]],
                         ["prior-old-one.md", "prior-mid-one.md", "prior-new-one.md"])
        # apply demotes them, keeps the pin + untyped + freeform
        r = store.index_cap(budget_lines=4, apply=True)
        self.assertEqual(r["demoted"], 3)
        with open(os.path.join(self.adopted, "MEMORY.md")) as f:
            kept = f.read()
        self.assertNotIn("prior-old-one.md", kept)
        self.assertIn("prior-pinned-one.md", kept)   # always: never demoted
        self.assertIn("random-note.md", kept)        # untyped: never demoted
        self.assertIn("freeform line", kept)
        # the demoted content stays reachable via inject (lossless)
        self.assertTrue(store.resolve_prompt("oldkw here"))
        # net + receipt exist
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "MEMORY-demoted.md")))
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "RECEIPT.json")))

    def test_under_budget_noop(self):
        self.seed_prior("a", "s", conf=0.8, keywords="akw", root_dir=self.adopted)
        self._index(["# idx", "- [a](prior-a.md) a"])
        r = store.index_cap(budget_lines=60, apply=True)
        self.assertEqual((r["over"], r["demoted"]), (0, 0))

    def test_over_budget_but_nothing_safe(self):
        # over budget, but every line is untyped -> nothing lossless-demotable
        self._index(["# idx", "- [x](random-x.md) x", "- [y](random-y.md) y", "- z"])
        r = store.index_cap(budget_lines=1, apply=True)
        self.assertEqual((r["over"], r["demotable"], r["demoted"]), (3, 0, 0))
        rc, out, _ = self.run_cli(["cap", "--budget-lines", "1", "--apply"])
        self.assertIn("NOTHING safely demotable", out)

    def test_concurrent_append_preserved_on_apply(self):
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted, last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail1", "- tail2"])
        # the re-read + line-set rewrite path drops ONLY the demoted line; every
        # other line (incl. a concurrent native append) survives
        r = store.index_cap(budget_lines=2, apply=True)
        self.assertEqual(r["demoted"], 1)
        with open(os.path.join(self.adopted, "MEMORY.md")) as f:
            kept = f.read()
        self.assertIn("tail1", kept)   # non-demoted lines survive
        self.assertIn("tail2", kept)
        self.assertNotIn("prior-old-one.md", kept)

    def test_no_memory_file(self):
        r = store.index_cap(apply=True)
        self.assertFalse(r["exists"])
        rc, out, _ = self.run_cli(["cap"])
        self.assertIn("no MEMORY.md", out)

    def test_cli_usage(self):
        rc, _, err = self.run_cli(["frob"])
        self.assertEqual(rc, 2)


class CandidateTierTest(StoreBase):
    """Candidate tier: safe inferred capture — a candidate is a non-live
    status EXCLUDED from every injecting lane (the hard law), surfaced only
    in list --candidates, promoted by confirm, rejected in place by reject.
    v2 (autolearn): every capturable type may be born a candidate — capture
    everything, canonize nothing automatically; premise stays human-only."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_candidate_write_shape_and_excluded_from_inject(self):
        rc, out, _ = self.add("lexicon", "glorpterm | a coined word", "--candidate")
        self.assertEqual(rc, 0)
        self.assertIn("CANDIDATE 'glorpterm'", out)
        self.assertIn("src=inferred", out)
        # the file carries the candidate status line + inferred source
        e = self.one(store.candidates(), "glorpterm")
        with open(e["path"]) as f:
            raw = f.read()
        self.assertIn("  status: candidate", raw)
        self.assertIn("  source: inferred", raw)
        # the hard law: excluded from load_all default, resolve, and pinned
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.resolve_prompt("is this glorpterm at all"), [])
        self.assertEqual(store.resolve_prompt("glorpterm here"), [])
        # but visible in the include_retired view and candidates()
        self.assertEqual([e["id"] for e in store.candidates()], ["glorpterm"])
        self.assertEqual(self.one(store.load_all(include_retired=True),
                                  "glorpterm")["status"], "candidate")

    def test_candidate_all_capturable_types(self):
        # capture-everything: prior/heuristic/reference candidates land as
        # non-live files with inferred source, invisible to every inject lane
        for args in (("prior", "x-law | inferred belief | 0.7 | glorpwork"),
                     ("heuristic", "x-move | try the glorp first | glorpwork"),
                     ("reference", "x-ref | the glorp paper | https://x.example")):
            rc, out, _ = self.add(*args, "--candidate")
            self.assertEqual(rc, 0, args[0])
            self.assertIn("CANDIDATE", out)
            self.assertIn("src=inferred", out)
        self.assertEqual(sorted(e["id"] for e in store.candidates()),
                         ["x-law", "x-move", "x-ref"])
        for e in store.candidates():
            with open(e["path"]) as f:
                raw = f.read()
            self.assertIn("  status: candidate", raw)
            self.assertIn("  source: inferred", raw)
        # the hard law holds across types: nothing loads, resolves, or pins
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.resolve_prompt("glorpwork x-law x-move x-ref"), [])
        self.assertEqual(store.pinned(), [])

    def test_premise_candidate_refused(self):
        # the certainty rail is human-only — an inference cannot claim 1.0
        # even in escrow; the refusal routes to the prior-candidate lane
        rc, _, err = self.add("premise", "x-truth | inferred certainty", "--candidate")
        self.assertEqual(rc, 2)
        self.assertIn("human-only", err)
        self.assertIn("add prior", err)
        self.assertEqual(store.candidates(), [])

    def test_confirm_prior_receipt_and_confidence_untouched(self):
        self.add("prior", "x-law | glorp before zork | 0.7 | glorpwork", "--candidate")
        e, err = store.confirm("x-law", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        # confirm ratifies the capture, never inflates the belief
        e = self.one(store.load_all(), "x-law")
        self.assertAlmostEqual(e["confidence"], 0.7)
        # who/when receipt lives in the prior's own evidence_log
        r = e["evidence_log"][-1]
        self.assertEqual((r["type"], r["by"]), ("confirmed", "human"))
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpwork now")],
                         ["x-law"])

    def test_confirm_edit_swaps_move(self):
        self.add("heuristic", "x-move | first guess | glorpwork", "--candidate")
        e, err = store.confirm("x-move", TS, new_statement="the sharpened move")
        self.assertIsNone(err)
        e = self.one(store.load_all(), "x-move")
        self.assertEqual((e["move"], e["statement"]),
                         ("the sharpened move", "the sharpened move"))

    def test_reject_retires_in_place(self):
        self.add("lexicon", "glorpterm | a wrong guess", "--candidate")
        path = self.one(store.candidates(), "glorpterm")["path"]
        e, err = store.reject("glorpterm", TS, why="not a real coinage")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "retired")
        # the record law: the file STAYS, carrying the receipt
        self.assertTrue(os.path.isfile(path))
        with open(path) as f:
            raw = f.read()
        self.assertIn("  status: retired", raw)
        self.assertIn("  retired_why: not a real coinage", raw)
        # gone from every surface: candidates, live load, resolve
        self.assertEqual(store.candidates(), [])
        self.assertEqual(store.load_all(), [])
        e = self.one(store.load_all(include_retired=True), "glorpterm")
        self.assertEqual((e["status"], e["retired_why"]),
                         ("retired", "not a real coinage"))
        self.assertTrue(any(r.get("verb") == "store.reject"
                            and r.get("target") == "glorpterm"
                            for r in pk.read_events(50)))

    def test_reject_guards(self):
        e, err = store.reject("ghost", TS)
        self.assertIsNone(e)
        self.assertIn("not found", err)
        self.add("lexicon", "liveterm | a live one")
        e, err = store.reject("liveterm", TS)
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)

    def test_reject_cli(self):
        self.add("prior", "x-law | wrong inference | 0.6", "--candidate")
        rc, out, _ = self.run_cli(["reject", "x-law", "misread", "the", "log"])
        self.assertEqual(rc, 0)
        self.assertIn("REJECTED 'x-law'", out)
        self.assertEqual(self.one(store.load_all(include_retired=True),
                                  "x-law")["retired_why"], "misread the log")
        rc, _, err = self.run_cli(["reject"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_list_candidates_surface(self):
        self.add("lexicon", "glorpterm | a coined word", "--candidate")
        self.add("lexicon", "realterm | a live one")   # live, not a candidate
        rc, out, _ = self.run_cli(["list", "--candidates"])
        self.assertEqual(rc, 0)
        self.assertIn("glorpterm", out)
        self.assertNotIn("realterm", out)
        self.assertIn("helm store confirm glorpterm", out)
        # a store with no candidates says so
        store.confirm("glorpterm", TS)
        rc, out, _ = self.run_cli(["list", "--candidates"])
        self.assertIn("no candidates", out)

    def test_confirm_promotes_and_fires(self):
        self.add("lexicon", "glorpterm | a coined word", "--candidate")
        e, err = store.confirm("glorpterm", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        # reload from disk: live, the candidate status line is gone, fires JIT
        e = self.one(store.load_all(), "glorpterm")
        self.assertEqual(e["status"], "live")
        with open(e["path"]) as f:
            self.assertNotIn("status: candidate", f.read())
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpterm now")],
                         ["glorpterm"])
        # the promotion left an events receipt
        self.assertTrue(any(r.get("verb") == "store.confirm"
                            and r.get("target") == "glorpterm"
                            for r in pk.read_events(50)))

    def test_confirm_edit_swaps_definition(self):
        self.add("lexicon", "glorpterm | first guess", "--candidate")
        e, err = store.confirm("glorpterm", TS, new_statement="the sharpened sense")
        self.assertIsNone(err)
        self.assertEqual(self.one(store.load_all(), "glorpterm")["definition"],
                         "the sharpened sense")

    def test_confirm_guards(self):
        e, err = store.confirm("ghost", TS)
        self.assertIsNone(e)
        self.assertIn("not found", err)
        self.add("lexicon", "liveterm | a live one")
        e, err = store.confirm("liveterm", TS)
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)

    def test_confirm_cli_edit(self):
        self.add("lexicon", "glorpterm | first", "--candidate")
        rc, out, _ = self.run_cli(["confirm", "glorpterm", "--edit", "the real sense"])
        self.assertEqual(rc, 0)
        self.assertIn("CONFIRMED 'glorpterm'", out)
        self.assertIn("edited", out)
        self.assertEqual(self.one(store.load_all(), "glorpterm")["definition"],
                         "the real sense")

    def test_candidate_over_live_lexicon_refused(self):
        # lexicon's redefine-freely exemption must not let a CANDIDATE add
        # de-canonize a LIVE term (writing status:candidate in place destroys
        # the human-confirmed definition and drops it out of inject)
        self.add("lexicon", "glorpterm | the confirmed sense")
        rc, _, err = self.add("lexicon", "glorpterm | an agent guess", "--candidate")
        self.assertEqual(rc, 1)
        self.assertIn("already LIVE", err)
        self.assertIn("de-canonize", err)
        # the confirmed definition is untouched and still fires
        e = self.one(store.load_all(), "glorpterm")
        self.assertEqual((e["status"], e["definition"]),
                         ("live", "the confirmed sense"))
        self.assertEqual(store.candidates(), [])
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpterm now")],
                         ["glorpterm"])
        # candidate-over-candidate stays a legal guess update...
        self.add("lexicon", "newterm | first guess", "--candidate")
        rc, _, _ = self.add("lexicon", "newterm | better guess", "--candidate")
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.candidates(), "newterm")["definition"],
                         "better guess")
        # ...and a rejected term re-captures cleanly (retired != live)
        store.reject("newterm", TS, why="off")
        rc, _, _ = self.add("lexicon", "newterm | third guess", "--candidate")
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.candidates(), "newterm")["status"],
                         "candidate")

    def test_confirm_reject_ambiguous_cross_type_refused(self):
        # candidates mint in all four types now — a bare id shared across
        # types must never silently ratify/retire _find's typed-first winner
        self.add("prior", "dupx | a belief guess | 0.6", "--candidate")
        self.add("lexicon", "dupx | a term guess", "--candidate")
        for verb in (store.confirm, store.reject):
            e, err = verb("dupx", TS)
            self.assertIsNone(e)
            self.assertIn("ambiguous", err)
            self.assertIn("lexicon", err)
            self.assertIn("prior", err)
        self.assertEqual(len(store.candidates()), 2)  # nothing moved
        # the type qualifier resolves it — and confirms the RIGHT entry
        e, err = store.confirm("dupx", TS, ctype="lexicon")
        self.assertIsNone(err)
        self.assertEqual(e["type"], "lexicon")
        self.assertEqual(self.one(store.load_all(), "dupx")["type"], "lexicon")
        # one candidate left -> the bare id is unambiguous again, and the
        # candidate-first pick beats _find's typed-first live-lexicon shadow
        e, err = store.reject("dupx", TS, why="wrong lane")
        self.assertIsNone(err)
        self.assertEqual((e["type"], e["status"]), ("prior", "retired"))
        # bad qualifier is refused before anything resolves
        e, err = store.confirm("dupx", TS, ctype="episodic")
        self.assertIsNone(e)
        self.assertIn("unknown --type", err)

    def test_ambiguous_candidates_cli_type_flag_and_hints(self):
        self.add("prior", "dupx | a belief guess | 0.6", "--candidate")
        self.add("lexicon", "dupx | a term guess", "--candidate")
        self.add("heuristic", "solo | lone move | glorpwork", "--candidate")
        # list hints carry the qualifier ONLY where the slug is shared
        rc, out, _ = self.run_cli(["list", "--candidates"])
        self.assertIn("helm store confirm dupx --type prior", out)
        self.assertIn("helm store reject dupx --type lexicon", out)
        self.assertNotIn("solo --type", out)
        # bare CLI confirm refuses with the disambiguation
        rc, _, err = self.run_cli(["confirm", "dupx"])
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", err)
        rc, _, _ = self.run_cli(["confirm", "dupx", "--type", "lexicon"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.load_all(), "dupx")["type"], "lexicon")
        rc, _, _ = self.run_cli(["reject", "dupx", "--type", "prior", "not", "real"])
        self.assertEqual(rc, 0)
        self.assertEqual([x["id"] for x in store.candidates()], ["solo"])
        st = {e["type"]: e["status"]
              for e in store.load_all(include_retired=True) if e["id"] == "dupx"}
        self.assertEqual(st, {"lexicon": "live", "prior": "retired"})
        # a dangling --type is a usage error, not a silent bare-id fall-through
        rc, _, err = self.run_cli(["confirm", "solo", "--type"])
        self.assertEqual(rc, 2)
        self.assertIn("--type needs", err)

    def test_readd_after_reject_scrubs_tombstone_receipt(self):
        # rejected -> re-add mints a FRESH lifecycle: the heuristic/reference
        # branches must scrub the retire receipt exactly like the prior branch
        # ("live but retired_ts X" corrupts provenance)
        for typ, first, again in (
                ("heuristic", "h1 | bad move | glorpwork",
                 "h1 | good move | glorpwork"),
                ("reference", "r1 | wrong paper | https://x.example",
                 "r1 | right paper | https://x.example")):
            eid = typ[0] + "1"
            rc, _, _ = self.add(typ, first, "--candidate")
            self.assertEqual(rc, 0, typ)
            _, err = store.reject(eid, TS, why="bad " + typ)
            self.assertIsNone(err, typ)
            rc, _, _ = self.add(typ, again)
            self.assertEqual(rc, 0, typ)
            e = self.one(store.load_all(), eid)
            self.assertEqual(e["status"], "live", typ)
            with open(e["path"]) as f:
                raw = f.read()
            self.assertNotIn("retired", raw, typ)


class ProvisionalTierTest(StoreBase):
    """Provisional tier (owner canon 2026-07-22): a candidate a cross-family /x
    review has cleared goes PROVISIONALLY LIVE — it FIRES through the resolver
    like live but stays visibly [provisional]-tagged until the owner ratifies
    (confirm) or rejects it. xrev-clear is the graduation gate; an un-cleared
    candidate still fires NOTHING (the hard law never weakens)."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_three_state_resolver_law(self):
        # the whole point pinned in one place: live fires, provisional fires
        # (usable knowledge), candidate fires NOTHING.
        self.seed_prior("live-law", "the confirmed truth", keywords="glorpwork")
        self.add("prior", "prov-law | the cleared belief | 0.7 | glorpwork", "--candidate")
        self.add("prior", "cand-law | the raw guess | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("prov-law", TS, by="codex-seat")
        got = [e["id"] for e in store.resolve_prompt("glorpwork now")]
        self.assertIn("live-law", got)
        self.assertIn("prov-law", got, "provisional must fire like live")
        self.assertNotIn("cand-law", got, "candidate must stay fully excluded")
        # load_all default surfaces live + provisional, never the candidate
        ids = {e["id"]: e["status"] for e in store.load_all()}
        self.assertEqual(ids, {"live-law": "live", "prov-law": "provisional"})

    def test_xrev_clear_records_reviewer_and_persists(self):
        self.add("prior", "x-law | inferred belief | 0.7 | glorpwork", "--candidate")
        e, err = store.xrev_clear("x-law", TS, by="codex-seat")
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["xrev_by"], e["xrev_ts"]),
                         ("provisional", "codex-seat", TS))
        # reload from disk: status + who/when receipt persisted in the file
        e = self.one(store.load_all(), "x-law")
        self.assertEqual((e["status"], e["xrev_by"]), ("provisional", "codex-seat"))
        with open(e["path"]) as f:
            raw = f.read()
        self.assertIn("  status: provisional", raw)
        self.assertIn("  xrev_by: codex-seat", raw)
        # a prior logs the clearance to its own evidence_log (who/when)
        r = e["evidence_log"][-1]
        self.assertEqual((r["type"], r["by"]), ("xrev-cleared", "codex-seat"))
        # the events journal carries the mutation receipt
        self.assertTrue(any(row.get("verb") == "store.xrev_clear"
                            and row.get("target") == "x-law"
                            for row in pk.read_events(50)))

    def test_xrev_clear_all_types_carry_the_file_receipt(self):
        # the non-prior types have no evidence_log, so the durable receipt is the
        # xrev_by/xrev_ts file fields (what the web panel/CLI display reads)
        for args in (("lexicon", "glorpterm | a cleared coinage"),
                     ("heuristic", "x-move | try glorp first | glorpwork"),
                     ("reference", "x-ref | the glorp paper | https://x.example")):
            self.add(*args, "--candidate")
        for eid in ("glorpterm", "x-move", "x-ref"):
            e, err = store.xrev_clear(eid, TS, by="opus-seat")
            self.assertIsNone(err, eid)
            self.assertEqual(e["status"], "provisional", eid)
            e = self.one(store.load_all(), eid)
            self.assertEqual(e["xrev_by"], "opus-seat", eid)
            with open(e["path"]) as f:
                self.assertIn("xrev_by: opus-seat", f.read(), eid)

    def test_xrev_clear_guards(self):
        self.add("prior", "x-law | guess | 0.6", "--candidate")
        # a reviewer is mandatory — the verb attests a review happened
        e, err = store.xrev_clear("x-law", TS, by="")
        self.assertIsNone(e)
        self.assertIn("--by", err)
        # not found
        e, err = store.xrev_clear("ghost", TS, by="r")
        self.assertIsNone(e)
        self.assertIn("not found", err)
        # a live entry is not a candidate
        self.add("lexicon", "liveterm | a live one")
        e, err = store.xrev_clear("liveterm", TS, by="r")
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)
        # already provisional -> refused (graduate candidates only, once)
        store.xrev_clear("x-law", TS, by="r1")
        e, err = store.xrev_clear("x-law", TS, by="r2")
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)
        self.assertIn("provisional", err)

    def test_xrev_clear_ambiguous_cross_type_refused(self):
        self.add("prior", "dupx | belief guess | 0.6", "--candidate")
        self.add("lexicon", "dupx | a term guess", "--candidate")
        e, err = store.xrev_clear("dupx", TS, by="r")
        self.assertIsNone(e)
        self.assertIn("ambiguous", err)
        e, err = store.xrev_clear("dupx", TS, by="r", ctype="lexicon")
        self.assertIsNone(err)
        self.assertEqual((e["type"], e["status"]), ("lexicon", "provisional"))
        # the prior sibling is untouched — still a candidate
        self.assertEqual(self.one(store.candidates(), "dupx")["type"], "prior")

    def test_confirm_ratifies_a_provisional(self):
        self.add("prior", "x-law | cleared belief | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("x-law", TS, by="codex-seat")
        self.assertEqual([e["id"] for e in store.resolve_prompt("glorpwork")], ["x-law"])
        e, err = store.confirm("x-law", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        e = self.one(store.load_all(), "x-law")
        self.assertEqual(e["status"], "live")
        # the promotion receipt names the prior state; xrev provenance survives
        r = e["evidence_log"][-1]
        self.assertEqual(r["type"], "confirmed")
        self.assertIn("provisional -> live", r["reason"])
        self.assertEqual(e["xrev_by"], "codex-seat")
        # and it now fires UNtagged (the [provisional] mark is gone)
        rc, out, _ = self.run_cli(["resolve", "glorpwork"])
        self.assertIn("x-law", out)
        self.assertNotIn("[provisional]", out)

    def test_reject_retires_a_provisional_in_place(self):
        self.add("lexicon", "glorpterm | a cleared coinage", "--candidate")
        store.xrev_clear("glorpterm", TS, by="r")
        path = self.one(store.load_all(), "glorpterm")["path"]
        e, err = store.reject("glorpterm", TS, why="wrong after all")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "retired")
        self.assertTrue(os.path.isfile(path), "the record law: the file stays")
        # gone from every injecting surface
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.resolve_prompt("is glorpterm here"), [])
        e = self.one(store.load_all(include_retired=True), "glorpterm")
        self.assertEqual((e["status"], e["retired_why"]), ("retired", "wrong after all"))

    def test_provisional_marked_in_cli_list_and_resolve(self):
        self.add("prior", "x-law | cleared | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("x-law", TS, by="r")
        rc, out, _ = self.run_cli(["list"])
        self.assertIn("x-law", out)
        self.assertIn("[provisional]", out)
        rc, out, _ = self.run_cli(["resolve", "glorpwork here"])
        self.assertIn("[provisional]", out)
        self.assertIn("x-law", out)

    def test_xrev_clear_cli(self):
        self.add("prior", "x-law | guess | 0.7 | glorpwork", "--candidate")
        rc, out, _ = self.run_cli(["xrev-clear", "x-law", "--by", "codex-seat"])
        self.assertEqual(rc, 0)
        self.assertIn("XREV-CLEARED 'x-law'", out)
        self.assertIn("provisional", out)
        self.assertEqual(self.one(store.load_all(), "x-law")["status"], "provisional")
        # missing --by is a usage error (rc 2), not a silent clear
        self.add("prior", "y-law | guess | 0.7", "--candidate")
        rc, _, err = self.run_cli(["xrev-clear", "y-law"])
        self.assertEqual(rc, 2)
        self.assertIn("--by", err)
        self.assertEqual(self.one(store.candidates(), "y-law")["status"], "candidate")

    def test_readd_scrubs_stale_xrev_receipt(self):
        # cleared -> rejected -> re-added: the fresh candidate must NOT carry the
        # prior xrev clearance ("provisional receipt on a raw candidate" corrupts
        # provenance — same law as the retire-receipt scrub)
        self.add("prior", "x-law | first | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("x-law", TS, by="r")
        store.reject("x-law", TS, why="wrong")
        self.add("prior", "x-law | second guess | 0.7 | glorpwork", "--candidate")
        e = self.one(store.candidates(), "x-law")
        self.assertEqual((e["status"], e["xrev_by"]), ("candidate", ""))
        with open(e["path"]) as f:
            self.assertNotIn("xrev_by", f.read())


if __name__ == "__main__":
    unittest.main()
