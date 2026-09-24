#!/usr/bin/env python3
"""`helm store gloss` — the door to the built-but-unwired lever.

The gloss field had a writer (write._gloss_lines), an oracle (_commit's
LINE_CAP refusal), an injector reader (a gloss fires WHOLE) and docs — and no
verb set it. Measured 2026-08-22 on the live store: the three specimen
entries of task/1346 all fired at 400 truncated bytes, so the JIT lane's
ceiling dropped one of the two gates with a loud marker; the cure was a
gloss and there was no door to write one.

Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR / HELM_CHAT_DIR are tmp
dirs. The replay arm rebuilds the specimen lane in a FIXTURE store — the
three live ids, their live keywords, 1000+-byte statements, the gates link,
and the two unrelated entries that outranked them on the live prompt — and
drives inject.gather (the real lane walk) on the specimen prompt; it is not
a read of the live store. Each arm names the mutation it kills.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import inject, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_CF_TOKEN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_AGENT_HARNESS",
            "PI_CODING_AGENT", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
            "CLAUDE_PID", "CLAUDE_CONFIG_DIR", "CODEX_HOME")

TS = "2026-08-22T12:00:00Z"
ORCA = "orca-accommodations-live-in-helm-never-fork-or-upstream"
TWO = "upstream-only-what-is-proven-and-only-where-welcome"
EMBER = "ember-repos-always-upstream-no-gate"
PROMPT = "a post mentioning an upstream PR to orca"

# THE EXACT TEXTS WRITTEN TO THE LIVE STORE 2026-08-22 (the integrator's
# owner-canon compressions, filler-trimmed to fit LINE_CAP; no clause cut).
GLOSS = {
    ORCA: ("Owner ruling 2026-08-22: every Orca accommodation lives HELM-side, "
           "on Orca stable public surfaces, CLI verbs and the env it stamps: no "
           "fork fixes, no upstream PRs to stablyai/orca. Idempotent against "
           "whatever Orca does; every seam assumption detectable by a doctor "
           "line. The Ember fork-is-PR-staging pattern does NOT apply to Orca."),
    TWO: ("TWO GATES before a PR is considered, both respect for the "
          "maintainer: 1. PROVEN IN OUR OWN USE, locally green is not proven; "
          "2. THE PROJECT WELCOMES CONTRIBUTIONS, herdr is hostile to AI-era "
          "PRs. An unproven AI-authored PR is indistinguishable from slop to "
          "the maintainer. Ember is the exception: see "
          "ember-repos-always-upstream-no-gate."),
    EMBER: ("Ember repos, emberian/*: dregg, cv, DreggNet, mediateor, chetgpt, "
            "claurdvoyant, graphplay, svenvs, are an UNCONDITIONAL upstream: "
            "always push the fix up so Ember can eval, no proving gate, no "
            "culture check, no asking first. A local workaround never offered "
            "upstream is unfinished work. Third parties = gate; Ember = go."),
}


def run_store(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


class GlossBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gloss-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, pid, statement, keywords, conf="1.0", **kw):
        e = {"id": pid, "statement": statement, "confidence": conf,
             "keywords": keywords, "stated_ts": TS, "source": "human"}
        e.update(kw)
        return store.write_prior(e)

    def entry(self, eid):
        hits = [e for e in store.load_all() if e["id"] == eid]
        self.assertEqual(len(hits), 1, eid)
        return hits[0]

    def disk(self, eid, types=None):
        """The on-disk bytes of the entry, retired ones included (_find)."""
        with open(store._find(eid, types=types)["path"], encoding="utf-8") as f:
            return f.read()


class VerbTest(GlossBase):
    def test_set_then_print_round_trips(self):
        # kills: the verb not writing; the writer not emitting gloss:; the
        # parser dropping it; print reading another field
        self.plant("r1", "L" * 900, "fork,staging")
        rc, out, _err = run_store(["gloss", "r1"])
        self.assertEqual(rc, 0)
        self.assertIn("no gloss", out)
        rc, out, err = run_store(["gloss", "r1", "--set", "the short", "line",
                                  "that fires"])
        self.assertEqual(rc, 0, err)
        self.assertIn("the line that fires is 37 of 400 bytes", out)
        self.assertEqual(self.entry("r1")["gloss"], "the short line that fires")
        self.assertIn("  gloss: the short line that fires\n", self.disk("r1"))
        rc, out, _err = run_store(["gloss", "r1"])
        self.assertEqual(rc, 0)
        self.assertIn("gloss: the short line that fires", out)
        # the statement is still the durable record, and the gloss FIRES
        self.assertEqual(len(self.entry("r1")["statement"]), 900)
        self.assertEqual(inject._entry_line(self.entry("r1")),
                         "PREMISE r1: the short line that fires")

    def test_an_oversized_gloss_is_refused_with_the_overage_and_the_file_unchanged(self):
        # kills: a bypass of _commit (a direct atomic_write); the overage
        # number dropped from the refusal; a partial write before the refusal
        self.plant("r1", "L" * 900, "fork,staging")
        run_store(["gloss", "r1", "--set", "the old gloss"])
        before = self.disk("r1")
        big = "x" * 400                      # PREMISE r1: + 400 = 412 bytes
        rc, _out, err = run_store(["gloss", "r1", "--set", big])
        self.assertEqual(rc, 1)
        self.assertIn("gloss too long: the rendered line is 412 UTF-8 bytes, "
                      "limit 400 — shorten the gloss by 12", err)
        self.assertEqual(self.disk("r1"), before, "a refusal touches nothing")
        self.assertEqual(self.entry("r1")["gloss"], "the old gloss")
        # measured in UTF-8 BYTES, not characters (the emoji class)
        rc, _out, err = run_store(["gloss", "r1", "--set", "—" * 131])
        self.assertEqual(rc, 1)
        self.assertIn("405 UTF-8 bytes", err)
        self.assertEqual(self.entry("r1")["gloss"], "the old gloss")
        # and the positive control: exactly AT the cap writes
        rc, _out, err = run_store(["gloss", "r1", "--set", "y" * 388])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry("r1")["gloss"], "y" * 388)

    def test_clear_empties_it(self):  # noqa: VACUOUS_ASSERTION — the gloss is asserted on disk before --clear, and the cut line is asserted to end in the ellipsis
        # kills: --clear ignored; the writer emitting an empty gloss: line
        self.plant("r1", "L" * 900, "fork,staging")
        run_store(["gloss", "r1", "--set", "short"])
        self.assertIn("gloss:", self.disk("r1"))
        rc, out, err = run_store(["gloss", "r1", "--clear"])
        self.assertEqual(rc, 0, err)
        self.assertIn("gloss cleared", out)
        self.assertEqual(self.entry("r1").get("gloss", ""), "")
        self.assertNotIn("gloss:", self.disk("r1"))
        # WITH NO GLOSS THE LONG STATEMENT IS CUT AGAIN — that is the
        # contract. The route to the full entry is the LANE'S footer now
        # (task/2980), so the line itself ends in the ellipsis.
        cut = inject._entry_line(self.entry("r1"))
        self.assertTrue(cut.endswith("…"), cut)
        self.assertNotIn("helm store get", cut)
        self.assertLessEqual(len(cut), inject.LINE_CAP)

    def test_the_derived_law_still_scrubs_a_stale_gloss(self):
        # the door must not weaken write.confirm's invalidation: an edit of
        # the statement drops the gloss the verb wrote
        store.write_prior({"id": "c1", "statement": "draft", "confidence": "0.7",
                           "keywords": "fork,staging", "stated_ts": TS,
                           "status": "candidate"})
        e, err = store.regloss("c1", TS, text="a gloss of the draft")
        self.assertIsNone(err)
        e, err = store.confirm("c1", TS, new_statement="the corrected statement")
        self.assertIsNone(err, err)
        self.assertEqual(e.get("gloss", ""), "")

    def test_the_other_types_and_the_refusals(self):
        # kills: a type branch the verb cannot reach; --set with no text; the
        # two flags combined
        store.write_heuristic({"id": "h1", "move": "M" * 600,
                               "trigger": "seam,strategy", "stated_ts": TS})
        rc, _out, err = run_store(["gloss", "h1", "--type", "heuristic",
                                   "--set", "ask the three questions"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(inject._entry_line(self.entry("h1")),
                         "MOVE h1: ask the three questions")
        rc, _out, err = run_store(["gloss", "h1", "--set"])
        self.assertEqual(rc, 1)
        self.assertIn("--set needs the gloss text", err)
        rc, _out, err = run_store(["gloss", "h1", "--set", "x", "--clear"])
        self.assertEqual(rc, 1)
        self.assertIn("do not combine", err)
        rc, _out, err = run_store(["gloss", "nope", "--set", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)

    def test_a_lexicon_reminted_over_a_rejected_term_sheds_gloss_and_retirement(self):
        # P1 (d): the lexicon add door merged from the dead term,
        # so a re-mint over a rejected id carried its retired_ts/retired_why
        # AND the stale gloss into the new LIVE entry. kills: the merge base
        # not scrubbed for a non-live prev
        store.write_lexicon({"term": "seam", "definition": "a dependency edge",
                             "keywords": "seamword", "updated_ts": TS,
                             "status": "candidate", "gloss": "the old gloss"})
        e, err = store.reject("seam", TS, "wrong inference", ctype="lexicon")
        self.assertIsNone(err, err)
        dead = self.disk("seam", types=("lexicon",))
        self.assertIn("retired_ts:", dead)
        self.assertIn("gloss: the old gloss", dead)
        rc, _out, err = run_store(["add", "lexicon",
                                   "seam | a dependency edge | phrase | seamword"])
        self.assertEqual(rc, 0, err)
        fresh = self.entry("seam")
        self.assertEqual(fresh["status"], "live")
        self.assertEqual(fresh.get("gloss", ""), "", "a dead term's gloss is stale")
        self.assertEqual(fresh.get("retired_ts", ""), "")
        self.assertEqual(fresh.get("retired_why", ""), "")
        body = self.disk("seam", types=("lexicon",))
        self.assertNotIn("gloss:", body)
        self.assertNotIn("retired_ts:", body)
        # the positive control: a LIVE redefine with the same definition
        # still keeps its gloss (the merge law is for live terms)
        run_store(["gloss", "seam", "--type", "lexicon", "--set", "a fresh gloss"])
        rc, _out, err = run_store(["add", "lexicon",
                                   "seam | a dependency edge | phrase | seamword"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry("seam")["gloss"], "a fresh gloss")

    def test_a_refused_lexicon_remint_leaves_the_dead_term_byte_identical(self):
        # the other half of P1 (d): a REFUSED re-mint (the keyword lint
        # refuses a comma-less salad) changes neither the stale gloss nor the
        # retirement state — measured as on-disk bytes
        store.write_lexicon({"term": "seam", "definition": "a dependency edge",
                             "keywords": "seamword", "updated_ts": TS,
                             "status": "candidate", "gloss": "the old gloss"})
        _e, err = store.reject("seam", TS, "wrong inference", ctype="lexicon")
        self.assertIsNone(err, err)
        path = store._find("seam", types=("lexicon",))["path"]
        with open(path, "rb") as f:
            before = f.read()
        rc, _out, err = run_store(["add", "lexicon",
                                   "seam | a dependency edge | phrase | "
                                   "one long salad of five words"])
        self.assertEqual(rc, 1)
        self.assertIn("comma-less", err)
        with open(path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after, "a refusal touches nothing")
        self.assertIn(b"gloss: the old gloss", after)
        self.assertIn(b"retired_ts:", after)

    def test_help_names_the_verb(self):
        rc, out, err = run_store(["--help"])
        self.assertIn("gloss <id>", out + err)


class SpecimenReplayTest(GlossBase):
    """The arm that proves the lever does what the gap needed."""

    def plant_specimen(self):
        self.plant(ORCA, "ALL ORCA ACCOMMODATIONS ARE MADE ON THE HELM SIDE " +
                   "o" * 1000,
                   "orca, fork, upstream PR, stablyai, orca upgrade broke, keep "
                   "aligned with upstream, orca update, patch orca, rebuild "
                   "orca, helm side, upgrade, broke, orca upgrade, upgrade "
                   "broke, keep, aligned, upstream, keep aligned, aligned with, "
                   "with upstream")
        self.plant(TWO, "TWO GATES MUST PASS BEFORE A PR IS EVEN CONSIDERED " +
                   "t" * 1000,
                   "upstream,pr,oss,culture,herdr,ai-slop,proven,dogfood,"
                   "maintainer,ember")
        store.write_heuristic({
            "id": EMBER, "move": "EMBER REPOS ARE AN UNCONDITIONAL UPSTREAM "
            + "e" * 1000, "stated_ts": TS,
            "trigger": "upstream,ember,emberian,dregg,cv,pr,open a pr,push "
            "upstream,contribute back,fork fix,should i push,do i need "
            "permission,public repo,outward facing,ask first,mediateor,"
            "dreggnet,fix a dependency,local workaround"})
        _e, err = store.regate(EMBER, TS, add=TWO + "," + ORCA, ctype="heuristic")
        self.assertIsNone(err)
        # THE RULE IS ROUTED (task/2980): a gate rides only on a deterministic
        # line, and the live specimen's riders are what these arms are about
        e = next(x for x in store.load_all() if x["id"] == EMBER)
        e["trigger"] = e["trigger"] + ",notice:t"
        store.write_heuristic(e, path=e["path"])
        # the two unrelated 400-byte hits that outranked them on the live prompt
        self.plant("mute-busy-home-room-trust-mentions", "MUTE " + "m" * 1000,
                   "beacon,wake,mute,home-room,mention,noise,quota")
        self.plant("coordinated-step-post-before-apply", "POST " + "p" * 1000,
                   "coordinated, destructive, apply, ack, op-claim, post")

    def test_glossing_the_three_entries_makes_both_gates_ride_whole(self):  # noqa: VACUOUS_ASSERTION — the severed count and each whole gloss line are asserted unconditionally
        # kills: the verb writing nowhere the injector reads; the injector
        # reading statement over gloss; a rider allowance too small for two
        # glossed gates beside three full base lines
        self.plant_specimen()
        routed = mock.patch("helm.promptshape.notice_kinds",
                            lambda _prompt: frozenset({"t"}))
        with routed:
            before = inject.gather(PROMPT)["jit"]
        # SEVERED means the cap CUT it. The cut now backs off to a word
        # boundary and spends its tail on `helm store get <id>`, so the
        # ellipsis is inside the line rather than its last character.
        severed = [l for l in before
                   if l.startswith("GATE of ") and "…" in l]
        self.assertEqual(len(severed), 2,
                         "the fixture must reproduce the live shape — both gates "
                         "arrive SEVERED at LINE_CAP:\n%s"
                         % "\n".join(l[:90] for l in before))
        self.assertTrue(any(l.startswith("MOVE %s:" % EMBER) and "…" in l
                            for l in before))
        for eid, text in GLOSS.items():
            args = ["gloss", eid]
            if eid == EMBER:
                args += ["--type", "heuristic"]
            rc, _out, err = run_store(args + ["--set", text])
            self.assertEqual(rc, 0, err)
        with routed:
            after = inject.gather(PROMPT)["jit"]
        joined = "\n".join(after)
        self.assertNotIn("DROPPED", joined, joined)
        for eid in (ORCA, TWO, EMBER):
            self.assertIn(eid + ":", joined, eid)
        # the glosses themselves are what fired, whole
        self.assertIn("GATE of %s: PREMISE %s: %s" % (EMBER, TWO, GLOSS[TWO]), after)
        self.assertIn("GATE of %s: PREMISE %s: %s" % (EMBER, ORCA, GLOSS[ORCA]), after)
        self.assertIn("MOVE %s: %s" % (EMBER, GLOSS[EMBER]), after)
        self.assertFalse(any("…" in l for l in after
                             if l.startswith(("GATE of ", "MOVE "))), after)
        self.assertLessEqual(sum(len(l) for l in after),
                             inject.JIT_CAP * inject.LINE_CAP + inject.GATE_BUDGET)


if __name__ == "__main__":
    unittest.main()
