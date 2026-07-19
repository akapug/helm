#!/usr/bin/env python3
"""store tests — hermetic: every root (adopted + helm-global + project) points
at a tempdir via HELM_HOME + HELM_ADOPTED_DIR. The real ~/.claude, ~/.helm and
~/.mc are never read or written."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
