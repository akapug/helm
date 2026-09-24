#!/usr/bin/env python3
"""capability self-index tests — the AX meta-gap fix (owner 2026-07-23).

THE test that matters (owner's words): an agent reasoning about a2a/whisper/
converge gets 'you have helm chat deliver / meld, live' AT THE MOMENT, and a
deep-code-analysis prompt gets code-analysis (WHEN WIRED) — the same salience
gate + per-turn budget + cooldown as store entries, never a parallel injector.
Also pins: generic-only never fires (salience law), a powerpack surfaces ONLY
when wired (probe/flag), a private-hold powerpack surfaces to its OWN wired
agent but is WITHHELD from the public set, and the `helm capabilities` browse
verb.

The private-hold powerpack is authored HOST-LOCAL (no name ships in the tree):
these tests MOCK `helm.registry.authored_host` to return a generic powerpack so
they exercise the runtime-load mechanism, never a real deployment's config.

Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR are tmp; the HELM_CAP_*
flags are pinned per-test so no assertion depends on this machine's real MCP
wiring (a dev box that actually has cv/code-analysis would otherwise poison the
'absent' cases)."""
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

from helm import capability, inject, store  # noqa: E402

# The generic private-hold powerpack the mocked authored-host accessor returns —
# a full capability dict shaped like a CAPABILITIES row. Referencing the module
# constants (not hardcoded strings) keeps it honest to the real tier/visibility.
GENERIC_PP = {
    "id": "code-analysis",
    "verb": "code-analysis (scan / trace)",
    "tool": "mcp__code_analysis__scan",
    "what": "deep cross-language code analysis — run/trace/step-debug/bug-scan",
    "wired_via": "the code-analysis MCP server",
    "keywords": "code analysis,deep code analysis,cross-language,cross language,"
                "polyglot,step-debug,bug scan,find impls,goto def,"
                "semantic navigation,trace across languages",
    "tier": capability.TIER_POWERPACK,
    "visibility": capability.VIS_HOLD,
    "mcp": "code-analysis",
}

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR",
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_CF_TOKEN", "MELD_CF_TOKEN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "HELM_CAP_RECALL", "HELM_CAP_CODE_ANALYSIS")


class CapBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cap-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        # powerpacks default OFF so a dev box with real cv/code-analysis never
        # leaks in; each test that wants one wired sets its flag explicitly.
        os.environ["HELM_CAP_RECALL"] = "0"
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "0"
        # the private-hold powerpack is authored HOST-LOCAL — mock the accessor
        # so the runtime-load path returns our generic pack (never disk/config).
        self._host_patch = mock.patch("helm.registry.authored_host",
                                      return_value={"private_powerpacks": [GENERIC_PP]})
        self._host_patch.start()

    def tearDown(self):
        self._host_patch.stop()
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def jit(self, text):
        """The JIT lane's surfaced lines for `text` (capabilities included)."""
        return inject.gather(text)["jit"]

    def jit_ids(self, text):
        entries = inject._lane_entries(None)
        hits = store.resolve_prompt(text, entries=entries, cap=len(entries))
        return [str(e["id"]) for e in hits]


class SurfacingTest(CapBase):
    def test_a2a_converge_surfaces_meld_and_deliver(self):
        # THE owner test: reasoning about a2a/converge -> meld + chat-deliver.
        ids = self.jit_ids("let's a2a converge with another seat on this")
        self.assertIn("meld", ids)
        self.assertIn("chat-deliver", ids)
        # and it renders the owner's exact "you have ... (wired via ..., live)".
        lines = self.jit("let's a2a converge with another seat on this")
        blob = "\n".join(lines)
        self.assertIn("CAP you have helm chat meld:", blob)
        self.assertIn("wired via", blob)
        self.assertIn("live)", blob)

    def test_natural_converge_vocabulary_surfaces_meld(self):
        # The #1-feature discoverability owner-canon (reach for meld WITHOUT
        # being told): an agent must surface the meld lever at a converge moment
        # in the WORDS it actually uses, not only literal "converge/a2a". All
        # four MISSED before the keyword enrichment (verified on baseline).
        for text in ("reach consensus with another agent",
                     "we disagree, how do we resolve this",
                     "get on the same page with the other seat",
                     "bounce this off another agent"):
            self.assertIn("meld", self.jit_ids(text), text)

    def test_did_they_see_it_surfaces_the_pending_ladder(self):
        # substrate-awareness: reasoning about whether a message was SEEN /
        # ACTED / stranded must surface the ack-ladder pending view — a silent
        # reply must never be mistaken for a lost one. The lever did not exist
        # in the catalog before this lane.
        for text in ("did they see my message or act on it",
                     "is my dispatch stranded with no reply"):
            self.assertIn("pending", self.jit_ids(text), text)

    def test_ordinary_work_prompts_do_not_surface_meld(self):
        # No over-fire: solo-work vocabulary must NOT drag in the meld lever —
        # the enriched keywords stay specific, the specificity gate still holds.
        for text in ("fix the failing test in seats.py",
                     "refactor this function to be cleaner"):
            self.assertNotIn("meld", self.jit_ids(text), text)

    def test_dregg_protocol_vocab_does_not_false_fire_a2a_levers(self):
        # Cross-family gate catch (2026-07-23): this fleet debugs dregg CONSTANTLY
        # ("consensus root", "converge with the finalized root"), so bare
        # "consensus"/"converge" in the meld+deliver keywords was a relevance
        # regression in the index whose whole job is relevance. Scoped to a2a.
        for text in ("debug the Lean consensus root disagreement",
                     "make the faucet turn converge with the finalized root",
                     "the full-consensus node smoke fails on the faucet turn"):
            ids = self.jit_ids(text)
            self.assertNotIn("meld", ids, text)
            self.assertNotIn("chat-deliver", ids, text)

    def test_a_common_word_cap_id_does_not_broad_fire(self):
        # The SYSTEMIC fix (_probes): a capability id is a short verb-slug, and
        # 'pending' is everywhere in dev work — the id must NOT be a match probe
        # or the lever broad-fires on any "pending" turn. It fires on its curated
        # SEEN/ACTED keywords only. Protects every present + future cap.
        for text in ("is the deploy pending", "the PR is pending review",
                     "pending migrations to run"):
            self.assertNotIn("pending", self.jit_ids(text), text)

    def test_per_toolcall_whisper_meld_reaches_chat_deliver(self):
        # the ORIGINAL failure: reasoning about the per-toolcall whisper meld
        # and NOT reaching for helm chat deliver. It must now surface.
        ids = self.jit_ids("how do I do the per-toolcall whisper meld")
        self.assertIn("chat-deliver", ids)
        self.assertIn("meld", ids)

    def test_have_we_solved_this_surfaces_recall_when_wired(self):
        os.environ["HELM_CAP_RECALL"] = "1"
        ids = self.jit_ids("have we solved this before somewhere?")
        self.assertIn("recall", ids)

    def test_deep_code_analysis_surfaces_code_analysis_when_wired(self):
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        ids = self.jit_ids("do a deep cross-language code analysis of this module")
        self.assertIn("code-analysis", ids)

    def test_a2a_prompt_does_not_surface_code_analysis_or_recall(self):
        # salience is DISJOINT: the converge moment must not drag in code-analysis
        # or cold-recall levers.
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        os.environ["HELM_CAP_RECALL"] = "1"
        ids = self.jit_ids("let's a2a converge with another seat on this")
        self.assertNotIn("code-analysis", ids)
        self.assertNotIn("recall", ids)


class PowerpackWiredGateTest(CapBase):
    def test_powerpack_absent_does_not_surface_even_on_trigger(self):
        # code-analysis OFF (setUp default): its trigger prompt fires NOTHING.
        ids = self.jit_ids("do a deep cross-language code analysis of this module")
        self.assertNotIn("code-analysis", ids)

    def test_flag_true_wires_powerpack(self):
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        live = {e["id"] for e in capability.live_entries()}
        self.assertIn("code-analysis", live)

    def test_flag_false_holds_powerpack_dark(self):
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "0"
        live = {e["id"] for e in capability.live_entries()}
        self.assertNotIn("code-analysis", live)

    def test_core_capabilities_always_live(self):
        # core needs no flag/probe — helm's own substrate is always wired.
        live = {e["id"] for e in capability.live_entries()}
        for cid in ("chat-deliver", "meld", "dispatch", "work-claims", "store"):
            self.assertIn(cid, live)


class SalienceLawTest(CapBase):
    def test_generic_only_prompt_fires_no_capability(self):
        # a bland build/run/fix prompt touches no capability trigger -> silent.
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        os.environ["HELM_CAP_RECALL"] = "1"
        ids = self.jit_ids("build and run the code then fix the test")
        for cid in ("meld", "chat-deliver", "recall", "code-analysis", "council",
                    "dispatch", "work-claims", "store"):
            self.assertNotIn(cid, ids)

    def test_empty_prompt_fires_nothing(self):
        self.assertEqual(self.jit_ids("   "), [])


class BudgetAndCooldownTest(CapBase):
    def test_capabilities_obey_jit_cap(self):
        # capabilities ride the SAME cap-4 lane — never an unbounded wallpaper.
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        os.environ["HELM_CAP_RECALL"] = "1"
        # a prompt that brushes many triggers at once
        lines = self.jit("meld converge a2a whisper dispatch delegate claim a lane "
                         "recall prior art deep code analysis panel of models")
        self.assertLessEqual(len(lines), inject.JIT_CAP)

    def test_capability_cools_after_firing(self):
        sid = "cap-cooldown-sess"
        first = inject.gather("let's a2a converge with another seat", session=sid)
        self.assertTrue(any("helm chat meld" in l for l in first["jit"]))
        second = inject.gather("let's a2a converge with another seat", session=sid)
        # same prompt, same session, next turn -> meld is cooled off (not re-fired)
        self.assertFalse(any("helm chat meld" in l for l in second["jit"]),
                         "a fired capability must cool like any JIT entry")


class VisibilityHoldTest(CapBase):
    def test_authored_powerpack_is_private_hold(self):
        # proves the authored powerpack LOADS into the runtime set AND is held —
        # CAPABILITIES no longer ships it; it arrives via authored_host().
        row = next(c for c in capability._capabilities() if c["id"] == "code-analysis")
        self.assertEqual(row["visibility"], capability.VIS_HOLD)

    def test_public_set_withholds_private_hold(self):
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"  # wired, yet still withheld from public
        pub = {c["id"] for c in capability.public_set()}
        self.assertNotIn("code-analysis", pub)
        self.assertIn("meld", pub)

    def test_private_hold_still_surfaces_to_own_wired_agent(self):
        # the hold bars the PUBLIC export, NOT the local reasoning moment.
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        ids = self.jit_ids("do a deep cross-language code analysis of this module")
        self.assertIn("code-analysis", ids)


class BrowseVerbTest(CapBase):
    def _run(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = capability.cmd_capabilities(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_full_index_groups_core_and_powerpack(self):
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        rc, out, _ = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("CORE", out)
        self.assertIn("POWERPACK", out)
        self.assertIn("helm chat deliver", out)
        self.assertIn("wired via", out)
        self.assertIn("[hold: private]", out)   # code-analysis tagged
        self.assertIn("●", out)                 # a live dot present

    def test_absent_powerpack_shows_open_dot(self):
        rc, out, _ = self._run([])  # code-analysis OFF (setUp)
        self.assertEqual(rc, 0)
        self.assertIn("○", out)     # an absent dot present
        self.assertIn("absent", out)

    def test_public_flag_withholds_private_hold(self):
        os.environ["HELM_CAP_CODE_ANALYSIS"] = "1"
        rc, out, _ = self._run(["--public"])
        self.assertEqual(rc, 0)
        self.assertNotIn("code-analysis", out)
        self.assertIn("helm chat meld", out)

    def test_json_shape(self):
        import json
        rc, out, _ = self._run(["--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        ids = {r["id"] for r in rows}
        self.assertIn("code-analysis", ids)
        for r in rows:
            self.assertIn("live", r)
            self.assertIn("wired_via", r)
            self.assertIn("tier", r)

    def test_unknown_arg_refuses(self):
        rc, _, err = self._run(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg 'frobnicate'", err)


if __name__ == "__main__":
    unittest.main()
